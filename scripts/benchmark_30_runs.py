from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from core.costs import usd_to_rmb
from gateway.executor import run_research_job_sync
from scripts.benchmark_scoring import summarize_payload

REPORTS_DIR = ROOT_DIR / "reports"
QUERIES = [
    "DeepSeek R1 vs OpenAI o1 reasoning differences and 2025 compute cost",
    "LangGraph durable checkpointer sqlite postgres documentation",
    "NVIDIA Blackwell gross margin first quarter fiscal 2026 earnings guidance",
    "Redis Celery dead letter queue patterns for long-running jobs",
    "Tavily extract versus direct scraping tradeoffs for deep research agents",
    "FastAPI Celery Redis architecture for background research workloads",
    "OpenAI API structured outputs strict JSON repair alternatives",
    "SQLite WAL concurrency caveats with checkpoint-heavy workflows",
    "Agentic report writing section scoped context versus full context",
    "NVIDIA Blackwell supply chain and hyperscaler demand outlook 2026",
]
MODES = ["low", "medium", "high"]
BATCH_SIZE = 15


def _run_once(query: str, mode: str) -> dict[str, Any]:
    task_id = f"bench30-{mode}-{uuid.uuid4().hex[:8]}"
    result = run_research_job_sync(
        task_id,
        query,
        backend="benchmark30",
        research_mode=mode,
        disable_cache=True,
    )
    result["query"] = query
    result["research_mode"] = mode
    return result


def _matrix() -> list[tuple[str, str]]:
    return [(query, mode) for query in QUERIES for mode in MODES]


def run_batch(*, batch_index: int, judge_model: str | None) -> dict[str, Any]:
    matrix = _matrix()
    start = batch_index * BATCH_SIZE
    batch = matrix[start : start + BATCH_SIZE]
    results = []
    total_allin = 0.0
    allin_cap = float(os.getenv("BENCHMARK_MAX_ALLIN_RMB_EST", "40.0"))
    for query, mode in batch:
        if total_allin >= allin_cap:
            break
        result = _run_once(query, mode)
        results.append(result)
        total_allin += float(result.get("llm_cost_rmb", 0.0)) + usd_to_rmb(result.get("external_cost_usd_est", 0.0))
    payload = {
        "batch_index": batch_index,
        "batch_size": BATCH_SIZE,
        "results": results,
    }
    return summarize_payload(payload, judge_model=judge_model, allow_local_judge=True)


def write_report(payload: dict[str, Any]) -> tuple[Path, Path]:
    REPORTS_DIR.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    json_path = REPORTS_DIR / f"benchmark30_batch{payload['batch_index']}_{stamp}.json"
    md_path = REPORTS_DIR / f"benchmark30_batch{payload['batch_index']}_{stamp}.md"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(
        json.dumps(
            {
                "batch_index": payload["batch_index"],
                "runs": len(payload.get("results", [])),
                "summary": payload.get("mode_summary", {}),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return json_path, md_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Run one batch of the 30-run benchmark.")
    parser.add_argument("--batch-index", type=int, default=0, choices=[0, 1])
    parser.add_argument("--judge-model", type=str, default=os.getenv("BENCHMARK_JUDGE_MODEL", ""))
    args = parser.parse_args()
    payload = run_batch(batch_index=args.batch_index, judge_model=args.judge_model or None)
    json_path, md_path = write_report(payload)
    print(f"[benchmark30] json={json_path}")
    print(f"[benchmark30] markdown={md_path}")


if __name__ == "__main__":
    main()
