from __future__ import annotations

import argparse
import json
import statistics
import time
import uuid
from pathlib import Path
from typing import Any

import requests

ROOT_DIR = Path(__file__).resolve().parents[1]
REPORTS_DIR = ROOT_DIR / "reports"


def _percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * q))))
    return round(ordered[idx], 4)


def _poll_task(base_url: str, task_id: str, timeout_seconds: float) -> dict[str, Any]:
    started = time.perf_counter()
    last_payload: dict[str, Any] = {}
    while (time.perf_counter() - started) < timeout_seconds:
        response = requests.get(f"{base_url}/research/{task_id}", timeout=5)
        response.raise_for_status()
        payload = response.json()
        last_payload = payload
        if payload.get("publish_status") in {"PUBLISHED", "FAILED", "LOCAL_FALLBACK"}:
            return payload
        if payload.get("status") in {"STARTED", "SUCCESS", "FAILED"}:
            return payload
        time.sleep(0.2)
    return last_payload


def run_submit_latency_smoke(
    *,
    base_url: str,
    samples: int,
    research_mode: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    for idx in range(samples):
        query = f"Outbox submit latency probe #{idx + 1} {uuid.uuid4().hex[:8]}"
        started = time.perf_counter()
        response = requests.post(
            f"{base_url}/research",
            json={"query": query, "research_mode": research_mode, "disable_cache": True},
            timeout=10,
        )
        response.raise_for_status()
        submit_ms = round((time.perf_counter() - started) * 1000.0, 4)
        accepted = response.json()
        task_id = str(accepted["task_id"])
        final_status = _poll_task(base_url, task_id, timeout_seconds=timeout_seconds)
        created_at = final_status.get("created_at")
        queued_at = final_status.get("queued_at")
        publish_latency_ms = None
        if created_at is not None and queued_at is not None:
            publish_latency_ms = round((float(queued_at) - float(created_at)) * 1000.0, 4)
        results.append(
            {
                "task_id": task_id,
                "submit_latency_ms": submit_ms,
                "publish_latency_ms": publish_latency_ms,
                "status": final_status.get("status"),
                "publish_status": final_status.get("publish_status"),
                "publish_attempt_count": final_status.get("publish_attempt_count"),
                "publish_last_error": final_status.get("publish_last_error"),
                "detail": final_status.get("detail"),
            }
        )

    submit_latencies = [float(item["submit_latency_ms"]) for item in results]
    publish_latencies = [float(item["publish_latency_ms"]) for item in results if item["publish_latency_ms"] is not None]
    publish_failures = [item for item in results if item.get("publish_status") == "FAILED"]
    return {
        "base_url": base_url,
        "samples": samples,
        "research_mode": research_mode,
        "results": results,
        "summary": {
            "avg_submit_latency_ms": round(statistics.mean(submit_latencies), 4) if submit_latencies else 0.0,
            "p95_submit_latency_ms": _percentile(submit_latencies, 0.95),
            "avg_publish_latency_ms": round(statistics.mean(publish_latencies), 4) if publish_latencies else 0.0,
            "p95_publish_latency_ms": _percentile(publish_latencies, 0.95),
            "publish_failure_rate": round(len(publish_failures) / max(1, len(results)), 4),
        },
    }


def write_report(payload: dict[str, Any]) -> tuple[Path, Path]:
    REPORTS_DIR.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    json_path = REPORTS_DIR / f"submit_latency_smoke_{stamp}.json"
    md_path = REPORTS_DIR / f"submit_latency_smoke_{stamp}.md"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = payload["summary"]
    lines = [
        "# Submit Latency Smoke",
        "",
        f"- Base URL: `{payload['base_url']}`",
        f"- Samples: `{payload['samples']}`",
        f"- Mode: `{payload['research_mode']}`",
        "",
        "| Metric | Value |",
        "| --- | ---: |",
        f"| Avg submit latency (ms) | {summary['avg_submit_latency_ms']:.4f} |",
        f"| P95 submit latency (ms) | {summary['p95_submit_latency_ms']:.4f} |",
        f"| Avg publish latency (ms) | {summary['avg_publish_latency_ms']:.4f} |",
        f"| P95 publish latency (ms) | {summary['p95_publish_latency_ms']:.4f} |",
        f"| Publish failure rate | {summary['publish_failure_rate']:.4f} |",
    ]
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, md_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Measure API submit latency after outbox decoupling.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--samples", type=int, default=10)
    parser.add_argument("--mode", default="medium")
    parser.add_argument("--poll-timeout", type=float, default=30.0)
    args = parser.parse_args()
    payload = run_submit_latency_smoke(
        base_url=args.base_url.rstrip("/"),
        samples=args.samples,
        research_mode=args.mode,
        timeout_seconds=args.poll_timeout,
    )
    json_path, md_path = write_report(payload)
    print(f"[submit-latency] json={json_path}")
    print(f"[submit-latency] markdown={md_path}")


if __name__ == "__main__":
    main()
