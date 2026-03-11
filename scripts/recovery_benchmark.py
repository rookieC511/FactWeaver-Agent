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

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from gateway.state_store import get_task

RUNNER = ROOT_DIR / "scripts" / "run_task_process.py"
REPORTS_DIR = ROOT_DIR / "reports"
POINTS = ["planner", "executor", "writer.before_editor"]
DEFAULT_QUERY = "LangGraph durable checkpoint recovery behavior with sqlite and task resume"


def _run_subprocess(task_id: str, query: str, mode: str, *, resume: bool, interrupt_point: str | None) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    if interrupt_point:
        env["FACTWEAVER_INTERRUPT_POINT"] = interrupt_point
        env["FACTWEAVER_INTERRUPT_TASK_ID"] = task_id
        env["FACTWEAVER_INTERRUPT_EXIT_CODE"] = "95"
    else:
        env.pop("FACTWEAVER_INTERRUPT_POINT", None)
        env.pop("FACTWEAVER_INTERRUPT_TASK_ID", None)
        env.pop("FACTWEAVER_INTERRUPT_EXIT_CODE", None)

    command = [
        sys.executable,
        str(RUNNER),
        "--task-id",
        task_id,
        "--query",
        query,
        "--mode",
        mode,
        "--backend",
        "recovery_harness",
        "--disable-cache",
    ]
    if resume:
        command.append("--resume")
    return subprocess.run(command, capture_output=True, text=True, env=env, cwd=ROOT_DIR)


def _run_one(point: str, run_index: int, mode: str, query: str) -> dict[str, Any]:
    task_id = f"recovery-{point.replace('.', '-')}-{run_index}-{uuid.uuid4().hex[:6]}"
    interrupted_started = time.time()
    first = _run_subprocess(task_id, query, mode, resume=False, interrupt_point=point)
    interrupted_elapsed = time.time() - interrupted_started

    resume_started = time.time()
    second = _run_subprocess(task_id, query, mode, resume=True, interrupt_point=None)
    resume_elapsed = time.time() - resume_started
    task = get_task(task_id) or {}

    return {
        "task_id": task_id,
        "interrupt_point": point,
        "first_exit_code": first.returncode,
        "resume_exit_code": second.returncode,
        "interrupt_elapsed_seconds": round(interrupted_elapsed, 4),
        "resume_elapsed_seconds": round(resume_elapsed, 4),
        "status": task.get("status"),
        "resume_count": int(task.get("resume_count") or 0),
        "resumed_from_checkpoint": bool(task.get("resumed_from_checkpoint") or 0),
        "last_checkpoint_id": task.get("last_checkpoint_id"),
        "last_checkpoint_node": task.get("last_checkpoint_node"),
        "same_thread_id": task.get("thread_id") == task_id,
        "success": (
            first.returncode == 95
            and second.returncode == 0
            and task.get("status") == "SUCCESS"
            and bool(task.get("last_checkpoint_id"))
        ),
        "first_stderr": first.stderr[-500:],
        "resume_stderr": second.stderr[-500:],
    }


def run_recovery_benchmark(*, repeats: int, mode: str, query: str) -> dict[str, Any]:
    runs: list[dict[str, Any]] = []
    for point in POINTS:
        for repeat_idx in range(1, repeats + 1):
            print(f"[recovery] point={point} run={repeat_idx}/{repeats}")
            runs.append(_run_one(point, repeat_idx, mode, query))

    grouped: dict[str, list[dict[str, Any]]] = {point: [item for item in runs if item["interrupt_point"] == point] for point in POINTS}
    summary = {}
    for point, items in grouped.items():
        summary[point] = {
            "runs": len(items),
            "success_rate": round(sum(1 for item in items if item["success"]) / max(1, len(items)), 4),
            "avg_resume_elapsed_seconds": round(
                statistics.mean(item["resume_elapsed_seconds"] for item in items),
                4,
            ),
        }

    report = {
        "mode": mode,
        "query": query,
        "total_runs": len(runs),
        "runs": runs,
        "summary": summary,
        "overall_success_rate": round(sum(1 for item in runs if item["success"]) / max(1, len(runs)), 4),
    }
    return report


def write_report(payload: dict[str, Any]) -> tuple[Path, Path]:
    REPORTS_DIR.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    json_path = REPORTS_DIR / f"checkpoint_recovery_{stamp}.json"
    md_path = REPORTS_DIR / f"checkpoint_recovery_{stamp}.md"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "# Checkpoint Recovery Benchmark",
        "",
        f"- Mode: `{payload['mode']}`",
        f"- Query: `{payload['query']}`",
        f"- Runs: {payload['total_runs']}",
        f"- Overall success rate: {payload['overall_success_rate']:.2%}",
        "",
        "| Point | Runs | Success Rate | Avg Resume Time (s) |",
        "| --- | ---: | ---: | ---: |",
    ]
    for point, summary in payload["summary"].items():
        lines.append(
            f"| {point} | {summary['runs']} | {summary['success_rate']:.2%} | {summary['avg_resume_elapsed_seconds']:.2f} |"
        )
    md_path.write_text("\n".join(lines), encoding="utf-8")
    return json_path, md_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Run checkpoint recovery benchmark.")
    parser.add_argument("--repeats", type=int, default=4)
    parser.add_argument("--mode", type=str, default="medium")
    parser.add_argument("--query", type=str, default=DEFAULT_QUERY)
    args = parser.parse_args()
    payload = run_recovery_benchmark(repeats=args.repeats, mode=args.mode, query=args.query)
    json_path, md_path = write_report(payload)
    print(f"[recovery] json={json_path}")
    print(f"[recovery] markdown={md_path}")


if __name__ == "__main__":
    main()
