from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import sys
import time
import uuid
from pathlib import Path
from typing import Any

BENCHMARK_MAX_TASK_RMB = float(os.getenv("BENCHMARK_MAX_TASK_RMB", "0.80"))
BENCHMARK_MAX_TOTAL_RMB = float(os.getenv("BENCHMARK_MAX_TOTAL_RMB", "4.00"))
BENCHMARK_MAX_EXTERNAL_RMB_EST = float(os.getenv("BENCHMARK_MAX_EXTERNAL_RMB_EST", "8.00"))
BENCHMARK_MAX_ALLIN_RMB_EST = float(os.getenv("BENCHMARK_MAX_ALLIN_RMB_EST", "12.00"))
BENCHMARK_JUDGE_MODEL = os.getenv("BENCHMARK_JUDGE_MODEL", "")

os.environ.setdefault("FACTWEAVER_API_MODE", "1")
os.environ.setdefault("MAX_TASK_DURATION_SECONDS", "1200")
os.environ.setdefault("MAX_TASK_NODE_COUNT", "50")
os.environ["MAX_TASK_RMB_COST"] = str(BENCHMARK_MAX_TASK_RMB)

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from core.config import STATE_DB_PATH, USD_TO_RMB_RATE
from core.costs import usd_to_rmb
from gateway.executor import run_research_job_sync
from scripts.benchmark_scoring import summarize_payload

QUERIES = [
    "DeepSeek R1 vs OpenAI o1 reasoning differences and 2025 compute cost",
    "LangGraph durable checkpointer sqlite postgres documentation",
    "NVIDIA Blackwell gross margin first quarter fiscal 2026 earnings guidance",
]
MODES = ["low", "medium", "high"]


def _selected_queries() -> list[str]:
    limit_raw = os.getenv("BENCHMARK_QUERY_LIMIT", "").strip()
    if limit_raw:
        try:
            limit = max(1, min(len(QUERIES), int(limit_raw)))
            return QUERIES[:limit]
        except ValueError:
            pass
    return QUERIES


def _selected_modes() -> list[str]:
    raw = os.getenv("BENCHMARK_MODES", "").strip()
    if not raw:
        return MODES
    selected = [item.strip().lower() for item in raw.split(",") if item.strip()]
    allowed = [mode for mode in MODES if mode in selected]
    return allowed or MODES


def _reports_dir() -> Path:
    path = ROOT_DIR / "reports"
    path.mkdir(exist_ok=True)
    return path


def _fetch_task_snapshot(task_id: str) -> dict[str, Any]:
    conn = sqlite3.connect(STATE_DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        return dict(row) if row else {}
    finally:
        conn.close()


def _run_once(query: str, mode: str) -> dict[str, Any]:
    task_id = f"bench-{mode}-{uuid.uuid4().hex[:8]}"
    started = time.time()
    try:
        result = run_research_job_sync(
            task_id,
            query,
            backend="benchmark",
            research_mode=mode,
            disable_cache=True,
        )
        result["query"] = query
        result["success"] = True
        result["wall_clock_seconds"] = round(time.time() - started, 2)
        result["report_preview"] = (result.get("report") or "")[:300]
        return result
    except Exception as exc:
        snapshot = _fetch_task_snapshot(task_id)
        return {
            "task_id": task_id,
            "query": query,
            "research_mode": mode,
            "status": snapshot.get("status", "FAILED"),
            "success": False,
            "error": repr(exc),
            "llm_cost_rmb": float(snapshot.get("llm_cost_rmb") or 0.0),
            "external_cost_usd_est": float(snapshot.get("external_cost_usd_est") or 0.0),
            "serper_queries": int(snapshot.get("serper_queries") or 0),
            "serper_cost_usd_est": float(snapshot.get("serper_cost_usd_est") or 0.0),
            "tavily_credits_est": float(snapshot.get("tavily_credits_est") or 0.0),
            "tavily_cost_usd_est": float(snapshot.get("tavily_cost_usd_est") or 0.0),
            "elapsed_seconds": float(snapshot.get("elapsed_seconds") or round(time.time() - started, 2)),
            "wall_clock_seconds": round(time.time() - started, 2),
            "report_preview": (snapshot.get("report") or "")[:300],
        }


def _run_matrix() -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    total_llm_cost_rmb = 0.0
    total_external_rmb = 0.0
    total_allin_rmb = 0.0
    stopped_early = False
    stop_reason = ""
    queries = _selected_queries()
    modes = _selected_modes()

    for query in queries:
        if stopped_early:
            break
        for mode in modes:
            if total_llm_cost_rmb >= BENCHMARK_MAX_TOTAL_RMB:
                stopped_early = True
                stop_reason = "llm_budget"
                break
            if total_external_rmb >= BENCHMARK_MAX_EXTERNAL_RMB_EST:
                stopped_early = True
                stop_reason = "external_budget"
                break
            if total_allin_rmb >= BENCHMARK_MAX_ALLIN_RMB_EST:
                stopped_early = True
                stop_reason = "allin_budget"
                break

            print(
                f"[benchmark] mode={mode} spent llm={total_llm_cost_rmb:.4f}/{BENCHMARK_MAX_TOTAL_RMB:.2f} "
                f"ext={total_external_rmb:.4f}/{BENCHMARK_MAX_EXTERNAL_RMB_EST:.2f} "
                f"allin={total_allin_rmb:.4f}/{BENCHMARK_MAX_ALLIN_RMB_EST:.2f}"
            )
            result = _run_once(query, mode)
            results.append(result)

            llm_cost = float(result.get("llm_cost_rmb", 0.0))
            external_rmb = usd_to_rmb(result.get("external_cost_usd_est", 0.0))
            total_llm_cost_rmb += llm_cost
            total_external_rmb += external_rmb
            total_allin_rmb += llm_cost + external_rmb

            if total_llm_cost_rmb >= BENCHMARK_MAX_TOTAL_RMB:
                stopped_early = True
                stop_reason = "llm_budget"
                break
            if total_external_rmb >= BENCHMARK_MAX_EXTERNAL_RMB_EST:
                stopped_early = True
                stop_reason = "external_budget"
                break
            if total_allin_rmb >= BENCHMARK_MAX_ALLIN_RMB_EST:
                stopped_early = True
                stop_reason = "allin_budget"
                break

    return {
        "benchmark_max_task_rmb": BENCHMARK_MAX_TASK_RMB,
        "benchmark_max_total_rmb": BENCHMARK_MAX_TOTAL_RMB,
        "benchmark_max_external_rmb_est": BENCHMARK_MAX_EXTERNAL_RMB_EST,
        "benchmark_max_allin_rmb_est": BENCHMARK_MAX_ALLIN_RMB_EST,
        "actual_total_llm_cost_rmb": round(total_llm_cost_rmb, 6),
        "actual_total_external_cost_rmb_est": round(total_external_rmb, 6),
        "actual_total_cost_rmb_est": round(total_allin_rmb, 6),
        "stopped_early": stopped_early,
        "stop_reason": stop_reason,
        "results": results,
    }


def _mode_summary_table(payload: dict[str, Any]) -> list[str]:
    summary = payload.get("mode_summary", {})
    modes = summary.get("modes", {})
    lines = [
        "## Mode Summary",
        "",
        "| Mode | Avg LLM Cost (RMB) | Avg External Cost (RMB) | Avg Total Cost (RMB) | Avg Quality | Avg Value | Avg Overall | Avg Time (s) |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for mode in ["low", "medium", "high"]:
        data = modes.get(mode)
        if not data:
            continue
        lines.append(
            "| {mode} | {llm:.4f} | {ext:.4f} | {total:.4f} | {quality:.2f} | {value:.2f} | {overall:.2f} | {time_s:.2f} |".format(
                mode=mode,
                llm=float(data.get("avg_llm_cost_rmb", 0.0)),
                ext=float(data.get("avg_external_cost_rmb_est", 0.0)),
                total=float(data.get("avg_total_cost_rmb_est", 0.0)),
                quality=float(data.get("avg_quality_score", 0.0)),
                value=float(data.get("avg_cost_efficiency_score", 0.0)),
                overall=float(data.get("avg_overall_score", 0.0)),
                time_s=float(data.get("avg_elapsed_seconds", 0.0)),
            )
        )
    return lines


def _run_detail_table(results: list[dict[str, Any]]) -> list[str]:
    lines = [
        "## Run Details",
        "",
        "| Mode | Query | Status | LLM Cost (RMB) | External Cost (RMB) | Total Cost (RMB) | FACT | RACE | Quality | Value | Overall | Time (s) |",
        "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for item in results:
        lines.append(
            "| {mode} | {query} | {status} | {llm:.4f} | {ext:.4f} | {total:.4f} | {fact:.2f} | {race:.2f} | {quality:.2f} | {value:.2f} | {overall:.2f} | {time_s:.2f} |".format(
                mode=item.get("research_mode", ""),
                query=str(item.get("query", "")).replace("|", " ")[:52],
                status=item.get("status", ""),
                llm=float(item.get("llm_cost_rmb", 0.0)),
                ext=float(item.get("external_cost_rmb_est", 0.0)),
                total=float(item.get("total_cost_rmb_est", 0.0)),
                fact=float(item.get("fact_score", 0.0)),
                race=float(item.get("race_score", 0.0)),
                quality=float(item.get("quality_score", 0.0)),
                value=float(item.get("cost_efficiency_score", 0.0)),
                overall=float(item.get("overall_score", 0.0)),
                time_s=float(item.get("elapsed_seconds", item.get("wall_clock_seconds", 0.0))),
            )
        )
    return lines


def _to_markdown(payload: dict[str, Any]) -> str:
    summary = payload.get("mode_summary", {})
    lines = [
        "# Benchmark Summary",
        "",
        f"- Generated At: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"- Runs: {len(payload.get('results', []))}",
        f"- USD->RMB Rate: {float(payload.get('usd_to_rmb_rate', USD_TO_RMB_RATE)):.2f}",
        f"- Task Budget: RMB {float(payload.get('benchmark_max_task_rmb', BENCHMARK_MAX_TASK_RMB)):.2f}",
        f"- Batch LLM Budget: RMB {float(payload.get('benchmark_max_total_rmb', BENCHMARK_MAX_TOTAL_RMB)):.2f}",
        f"- Batch External Budget: RMB {float(payload.get('benchmark_max_external_rmb_est', BENCHMARK_MAX_EXTERNAL_RMB_EST)):.2f}",
        f"- Batch All-in Budget: RMB {float(payload.get('benchmark_max_allin_rmb_est', BENCHMARK_MAX_ALLIN_RMB_EST)):.2f}",
        f"- Total LLM Cost: RMB {float(payload.get('actual_total_llm_cost_rmb', 0.0)):.4f}",
        f"- Total External Cost: RMB {float(payload.get('actual_total_external_cost_rmb_est', 0.0)):.4f}",
        f"- Total Cost: RMB {float(payload.get('actual_total_cost_rmb_est', 0.0)):.4f}",
        f"- Stopped Early: {payload.get('stopped_early', False)} ({payload.get('stop_reason', '')})",
        "",
        "## Key Conclusions",
        "",
        f"- Default recommendation: `{summary.get('recommended_default_mode', 'n/a')}`",
        f"- Highest quality mode: `{summary.get('highest_quality_mode', 'n/a')}`",
        f"- Best value mode: `{summary.get('best_value_mode', 'n/a')}`",
        f"- Slowest mode: `{summary.get('slowest_mode', 'n/a')}`",
        f"- Most expensive mode: `{summary.get('most_expensive_mode', 'n/a')}`",
        "",
    ]
    lines.extend(_mode_summary_table(payload))
    lines.append("")
    lines.extend(_run_detail_table(payload.get("results", [])))
    return "\n".join(lines)


def _write_payload(payload: dict[str, Any], *, base_name: str) -> tuple[Path, Path]:
    reports_dir = _reports_dir()
    json_path = reports_dir / f"{base_name}.json"
    md_path = reports_dir / f"{base_name}.md"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(_to_markdown(payload), encoding="utf-8")
    return json_path, md_path


def _rescore_existing(path: Path, judge_model: str | None) -> tuple[Path, Path]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    rescored = summarize_payload(payload, judge_model=judge_model, allow_local_judge=True)
    base_name = f"{path.stem}_scored"
    return _write_payload(rescored, base_name=base_name)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run or rescore the three-mode benchmark.")
    parser.add_argument("--rescore", type=str, default="", help="Existing benchmark JSON payload to rescore.")
    parser.add_argument("--judge-model", type=str, default=BENCHMARK_JUDGE_MODEL, help="Override local judge model.")
    args = parser.parse_args()

    judge_model = args.judge_model or None
    if args.rescore:
        json_path, md_path = _rescore_existing(Path(args.rescore).resolve(), judge_model)
        print(f"[benchmark] rescored_json={json_path}")
        print(f"[benchmark] rescored_markdown={md_path}")
        return

    payload = _run_matrix()
    scored_payload = summarize_payload(payload, judge_model=judge_model, allow_local_judge=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    json_path, md_path = _write_payload(scored_payload, base_name=f"mode_benchmark_{stamp}")
    print(f"[benchmark] json={json_path}")
    print(f"[benchmark] markdown={md_path}")


if __name__ == "__main__":
    main()
