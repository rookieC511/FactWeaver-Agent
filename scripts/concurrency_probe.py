from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import requests

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

REPORTS_DIR = ROOT_DIR / "reports"
API_URL = os.getenv("FACTWEAVER_API_URL", "http://127.0.0.1:8000")
QUERY_POOL = [
    "LangGraph sqlite checkpoint best practices and recovery semantics",
    "NVIDIA Blackwell fiscal 2026 margin guidance and commentary",
    "DeepSeek R1 versus OpenAI o1 compute cost and reasoning tradeoffs",
    "Redis Celery dead letter queue patterns for long running tasks",
]


def _wait_ready(url: str, *, timeout_seconds: int = 30) -> None:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            response = requests.get(f"{url}/health", timeout=2)
            if response.ok:
                return
        except Exception:
            pass
        time.sleep(1)
    raise RuntimeError("API did not become ready in time")


def _spawn_api() -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "gateway.api:app", "--host", "127.0.0.1", "--port", "8000"],
        cwd=ROOT_DIR,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )


def _spawn_worker() -> subprocess.Popen[str]:
    env = {
        **os.environ,
        "PYTHONIOENCODING": "utf-8",
        "CELERY_WORKER_POOL": "threads",
        "CELERY_WORKER_CONCURRENCY": "4",
    }
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "celery",
            "-A",
            "gateway.tasks",
            "worker",
            "--pool=threads",
            "--concurrency=4",
            "-Q",
            "research_queue",
            "--loglevel=WARNING",
        ],
        cwd=ROOT_DIR,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )


def _terminate(process: subprocess.Popen[str] | None) -> None:
    if process is None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()


def _submit_task(query: str) -> str:
    response = requests.post(
        f"{API_URL}/research",
        json={"query": query, "research_mode": "medium", "disable_cache": True},
        timeout=10,
    )
    response.raise_for_status()
    return response.json()["task_id"]


def _poll_task(task_id: str, *, timeout_seconds: int = 1800) -> dict[str, Any]:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        response = requests.get(f"{API_URL}/research/{task_id}", timeout=10)
        response.raise_for_status()
        payload = response.json()
        if payload.get("status") in {"SUCCESS", "FAILED"}:
            return payload
        time.sleep(2)
    raise TimeoutError(f"task {task_id} did not finish in time")


def _p95(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(round((len(ordered) - 1) * 0.95))))
    return round(ordered[index], 4)


def _probe_level(concurrency: int) -> dict[str, Any]:
    task_ids = []
    submitted_at = time.time()
    dlq_before = requests.get(f"{API_URL}/dlq", timeout=10).json()["items"]
    for idx in range(concurrency):
        query = f"{QUERY_POOL[idx % len(QUERY_POOL)]} [run:{uuid.uuid4().hex[:8]}]"
        task_ids.append(_submit_task(query))

    results = [_poll_task(task_id) for task_id in task_ids]
    dlq_after = requests.get(f"{API_URL}/dlq", timeout=10).json()["items"]

    successes = [item for item in results if item.get("status") == "SUCCESS"]
    failures = [item for item in results if item.get("status") != "SUCCESS"]
    end_to_end = [
        max(0.0, float((item.get("completed_at") or 0) - (item.get("started_at") or item.get("created_at") or 0)))
        if item.get("completed_at") and item.get("started_at")
        else float(item.get("elapsed_seconds") or 0.0)
        for item in results
    ]
    queue_wait = [
        max(0.0, float(item.get("started_at") or 0) - float(item.get("created_at") or 0))
        for item in results
        if item.get("started_at") is not None and item.get("created_at") is not None
    ]
    retry_successes = [item for item in successes if int(item.get("attempt_count") or 0) > 1]

    return {
        "concurrency": concurrency,
        "submitted_at": submitted_at,
        "task_ids": task_ids,
        "submitted": len(task_ids),
        "success_rate": round(len(successes) / max(1, len(task_ids)), 4),
        "failure_rate": round(len(failures) / max(1, len(task_ids)), 4),
        "retry_success_rate": round(len(retry_successes) / max(1, len(task_ids)), 4),
        "dlq_count": max(0, len(dlq_after) - len(dlq_before)),
        "p50_seconds": round(statistics.median(end_to_end), 4) if end_to_end else 0.0,
        "p95_seconds": _p95(end_to_end),
        "avg_queue_wait_seconds": round(statistics.mean(queue_wait), 4) if queue_wait else 0.0,
        "results": results,
    }


def run_probe(*, manage_services: bool) -> dict[str, Any]:
    api_process = None
    worker_process = None
    try:
        if manage_services:
            api_process = _spawn_api()
            worker_process = _spawn_worker()
            _wait_ready(API_URL)
        else:
            _wait_ready(API_URL)

        levels = [_probe_level(level) for level in [2, 4, 6, 8]]
        baseline = levels[0]["p95_seconds"] or 1.0
        qualified = [
            item
            for item in levels
            if item["success_rate"] >= 0.95
            and item["dlq_count"] == 0
            and item["p95_seconds"] <= (baseline * 2.5)
        ]
        return {
            "service_mode": "managed" if manage_services else "existing",
            "levels": levels,
            "max_supported_concurrency": qualified[-1]["concurrency"] if qualified else 0,
        }
    finally:
        _terminate(worker_process)
        _terminate(api_process)


def write_report(payload: dict[str, Any]) -> tuple[Path, Path]:
    REPORTS_DIR.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    json_path = REPORTS_DIR / f"concurrency_probe_{stamp}.json"
    md_path = REPORTS_DIR / f"concurrency_probe_{stamp}.md"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "# Concurrency Probe",
        "",
        f"- Service mode: `{payload['service_mode']}`",
        f"- Max supported concurrency: `{payload['max_supported_concurrency']}`",
        "",
        "| Concurrency | Success Rate | Failure Rate | Retry Success Rate | DLQ | P50 (s) | P95 (s) | Avg Queue Wait (s) |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for item in payload["levels"]:
        lines.append(
            f"| {item['concurrency']} | {item['success_rate']:.2%} | {item['failure_rate']:.2%} | "
            f"{item['retry_success_rate']:.2%} | {item['dlq_count']} | {item['p50_seconds']:.2f} | "
            f"{item['p95_seconds']:.2f} | {item['avg_queue_wait_seconds']:.2f} |"
        )
    md_path.write_text("\n".join(lines), encoding="utf-8")
    return json_path, md_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Run real FastAPI + Celery concurrency probe.")
    parser.add_argument("--reuse-services", action="store_true", help="Do not start uvicorn/celery; reuse existing ones.")
    args = parser.parse_args()
    payload = run_probe(manage_services=not args.reuse_services)
    json_path, md_path = write_report(payload)
    print(f"[concurrency] json={json_path}")
    print(f"[concurrency] markdown={md_path}")


if __name__ == "__main__":
    main()
