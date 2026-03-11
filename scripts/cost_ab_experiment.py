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

from core.costs import enrich_cost_fields
from gateway.executor import run_research_job_sync
from gateway.state_store import get_task

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
        try:
            result = run_research_job_sync(
                task_id,
                query,
                backend="cost_ab",
                research_mode=mode,
                disable_cache=True,
            )
        except Exception as exc:
            task = get_task(task_id) or {}
            result = enrich_cost_fields(
                {
                    "task_id": task_id,
                    "status": task.get("status", "FAILED"),
                    "detail": task.get("detail", str(exc)),
                    "last_error": task.get("last_error", repr(exc)),
                    "research_mode": mode,
                    "llm_cost_rmb": float(task.get("llm_cost_rmb") or 0.0),
                    "external_cost_usd_est": float(task.get("external_cost_usd_est") or 0.0),
                    "serper_queries": int(task.get("serper_queries") or 0),
                    "serper_cost_usd_est": float(task.get("serper_cost_usd_est") or 0.0),
                    "tavily_credits_est": float(task.get("tavily_credits_est") or 0.0),
                    "tavily_cost_usd_est": float(task.get("tavily_cost_usd_est") or 0.0),
                    "elapsed_seconds": float(task.get("elapsed_seconds") or 0.0),
                }
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


def _is_budget_abort(item: dict[str, Any]) -> bool:
    text = " ".join(
        str(item.get(key) or "")
        for key in ("detail", "last_error")
    ).lower()
    return "exceeded budget" in text


def run_experiment(mode: str, *, query_limit: int, max_allin_rmb: float | None) -> dict[str, Any]:
    runs: list[dict[str, Any]] = []
    cumulative_allin_rmb_est = 0.0
    stop_reason = "completed"
    consecutive_budget_aborts = 0
    processed_queries = 0
    for query in QUERY_SET[:query_limit]:
        processed_queries += 1
        legacy = _run_variant(query, mode, "legacy_full_context")
        runs.append(legacy)
        cumulative_allin_rmb_est = round(cumulative_allin_rmb_est + float(legacy.get("total_cost_rmb_est", 0.0)), 6)
        if _is_budget_abort(legacy):
            consecutive_budget_aborts += 1
        else:
            consecutive_budget_aborts = 0
        if max_allin_rmb is not None and cumulative_allin_rmb_est >= max_allin_rmb:
            stop_reason = f"budget_cap_reached:{max_allin_rmb}"
            break
        if consecutive_budget_aborts >= 2:
            stop_reason = "consecutive_budget_aborts"
            break

        current = _run_variant(query, mode, "section_scoped")
        runs.append(current)
        cumulative_allin_rmb_est = round(cumulative_allin_rmb_est + float(current.get("total_cost_rmb_est", 0.0)), 6)
        if _is_budget_abort(current):
            consecutive_budget_aborts += 1
        else:
            consecutive_budget_aborts = 0
        if max_allin_rmb is not None and cumulative_allin_rmb_est >= max_allin_rmb:
            stop_reason = f"budget_cap_reached:{max_allin_rmb}"
            break
        if consecutive_budget_aborts >= 2:
            stop_reason = "consecutive_budget_aborts"
            break

    grouped: dict[str, list[dict[str, Any]]] = {
        "legacy_full_context": [item for item in runs if item["context_mode"] == "legacy_full_context"],
        "section_scoped": [item for item in runs if item["context_mode"] == "section_scoped"],
    }
    summary = {}
    for context_mode, items in grouped.items():
        if not items:
            summary[context_mode] = {
                "avg_llm_cost_rmb": 0.0,
                "avg_external_cost_rmb_est": 0.0,
                "avg_total_cost_rmb_est": 0.0,
                "avg_elapsed_seconds": 0.0,
            }
            continue
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
        "query_limit": query_limit,
        "processed_queries": processed_queries,
        "runs": runs,
        "summary": summary,
        "total_cost_reduction_percent": reduction,
        "cumulative_allin_rmb_est": cumulative_allin_rmb_est,
        "stop_reason": stop_reason,
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
        f"- Query limit: `{payload.get('query_limit', 0)}`",
        f"- Processed queries: `{payload.get('processed_queries', 0)}`",
        f"- Total cost reduction: `{payload['total_cost_reduction_percent']}%`",
        f"- Cumulative all-in cost estimate: `{payload.get('cumulative_allin_rmb_est', 0.0):.4f} RMB`",
        f"- Stop reason: `{payload.get('stop_reason', 'completed')}`",
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
    parser.add_argument("--query-limit", type=int, default=5)
    parser.add_argument("--max-allin-rmb", type=float, default=None)
    args = parser.parse_args()
    payload = run_experiment(args.mode, query_limit=args.query_limit, max_allin_rmb=args.max_allin_rmb)
    json_path, md_path = write_report(payload)
    print(f"[cost_ab] json={json_path}")
    print(f"[cost_ab] markdown={md_path}")


if __name__ == "__main__":
    main()
