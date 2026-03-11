from __future__ import annotations

import argparse
import json
import os
import statistics
import time
import uuid
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[1]
import sys

if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from gateway.executor import run_research_job_sync

REPORTS_DIR = ROOT_DIR / "reports"
QUERY_SET = [
    "DeepSeek R1 versus OpenAI o1 compute cost and reasoning tradeoffs",
    "LangGraph durable checkpoint resume with sqlite and worker recovery",
    "NVIDIA Blackwell fiscal 2026 margin and earnings guidance analysis",
    "Redis Celery dead letter queue patterns for long running research tasks",
    "Tavily extract versus direct scraping tradeoffs for deep research agents",
]


def _run_variant(query: str, mode: str, context_mode: str) -> dict[str, Any]:
    task_id = f"costab-{context_mode}-{uuid.uuid4().hex[:8]}"
    started = time.time()
    previous = os.getenv("FACTWEAVER_WRITER_CONTEXT_MODE")
    try:
        os.environ["FACTWEAVER_WRITER_CONTEXT_MODE"] = context_mode
        result = run_research_job_sync(
            task_id,
            query,
            backend="cost_ab",
            research_mode=mode,
            disable_cache=True,
        )
        result["wall_clock_seconds"] = round(time.time() - started, 2)
        result["context_mode"] = context_mode
        result["query"] = query
        return result
    finally:
        if previous is None:
            os.environ.pop("FACTWEAVER_WRITER_CONTEXT_MODE", None)
        else:
            os.environ["FACTWEAVER_WRITER_CONTEXT_MODE"] = previous


def run_experiment(mode: str) -> dict[str, Any]:
    runs: list[dict[str, Any]] = []
    for query in QUERY_SET:
        legacy = _run_variant(query, mode, "legacy_full_context")
        current = _run_variant(query, mode, "section_scoped")
        runs.extend([legacy, current])

    grouped: dict[str, list[dict[str, Any]]] = {
        "legacy_full_context": [item for item in runs if item["context_mode"] == "legacy_full_context"],
        "section_scoped": [item for item in runs if item["context_mode"] == "section_scoped"],
    }
    summary = {}
    for context_mode, items in grouped.items():
        summary[context_mode] = {
            "avg_llm_cost_rmb": round(statistics.mean(float(item.get("llm_cost_rmb", 0.0)) for item in items), 6),
            "avg_external_cost_rmb_est": round(
                statistics.mean(float(item.get("external_cost_rmb_est", 0.0)) for item in items),
                6,
            ),
            "avg_total_cost_rmb_est": round(
                statistics.mean(float(item.get("total_cost_rmb_est", 0.0)) for item in items),
                6,
            ),
            "avg_elapsed_seconds": round(
                statistics.mean(float(item.get("elapsed_seconds", 0.0)) for item in items),
                4,
            ),
        }

    legacy_total = summary["legacy_full_context"]["avg_total_cost_rmb_est"]
    current_total = summary["section_scoped"]["avg_total_cost_rmb_est"]
    reduction = 0.0 if legacy_total <= 0 else round(((legacy_total - current_total) / legacy_total) * 100.0, 2)
    return {
        "mode": mode,
        "runs": runs,
        "summary": summary,
        "total_cost_reduction_percent": reduction,
    }


def write_report(payload: dict[str, Any]) -> tuple[Path, Path]:
    REPORTS_DIR.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    json_path = REPORTS_DIR / f"cost_ab_{stamp}.json"
    md_path = REPORTS_DIR / f"cost_ab_{stamp}.md"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [
        "# Cost A/B Experiment",
        "",
        f"- Mode: `{payload['mode']}`",
        f"- Total cost reduction: `{payload['total_cost_reduction_percent']}%`",
        "",
        "| Context Mode | Avg LLM Cost (RMB) | Avg External Cost (RMB) | Avg Total Cost (RMB) | Avg Time (s) |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for name, summary in payload["summary"].items():
        lines.append(
            f"| {name} | {summary['avg_llm_cost_rmb']:.4f} | {summary['avg_external_cost_rmb_est']:.4f} | "
            f"{summary['avg_total_cost_rmb_est']:.4f} | {summary['avg_elapsed_seconds']:.2f} |"
        )
    md_path.write_text("\n".join(lines), encoding="utf-8")
    return json_path, md_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Run writer legacy-vs-current cost A/B experiment.")
    parser.add_argument("--mode", default="medium")
    args = parser.parse_args()
    payload = run_experiment(args.mode)
    json_path, md_path = write_report(payload)
    print(f"[cost_ab] json={json_path}")
    print(f"[cost_ab] markdown={md_path}")


if __name__ == "__main__":
    main()
