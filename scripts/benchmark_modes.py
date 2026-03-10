import json
import os
import sqlite3
import sys
import time
import uuid
from pathlib import Path

BENCHMARK_MAX_TASK_RMB = float(os.getenv("BENCHMARK_MAX_TASK_RMB", "0.80"))
BENCHMARK_MAX_TOTAL_RMB = float(os.getenv("BENCHMARK_MAX_TOTAL_RMB", "4.00"))

os.environ.setdefault("FACTWEAVER_API_MODE", "1")
os.environ.setdefault("MAX_TASK_DURATION_SECONDS", "1200")
os.environ.setdefault("MAX_TASK_NODE_COUNT", "50")
os.environ["MAX_TASK_RMB_COST"] = str(BENCHMARK_MAX_TASK_RMB)

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from core.config import STATE_DB_PATH
from gateway.executor import run_research_job_sync


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


def _fetch_task_snapshot(task_id: str) -> dict:
    conn = sqlite3.connect(STATE_DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        return dict(row) if row else {}
    finally:
        conn.close()


def _run_once(query: str, mode: str) -> dict:
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


def _to_markdown(results: list[dict], total_llm_cost_rmb: float) -> str:
    lines = [
        "# 三档检索模式基准报告",
        "",
        f"- 运行时间: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 样本数: {len(results)}",
        f"- 单任务熔断上限: RMB {BENCHMARK_MAX_TASK_RMB:.2f}",
        f"- 批次总熔断上限: RMB {BENCHMARK_MAX_TOTAL_RMB:.2f}",
        f"- 本次实际 LLM 总成本: RMB {total_llm_cost_rmb:.4f}",
        "",
        "| Query | Mode | Status | LLM RMB | External USD | Serper | Tavily Credits | Time(s) |",
        "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for item in results:
        query = item.get("query", "")
        lines.append(
            "| {query} | {mode} | {status} | {llm:.4f} | {ext:.4f} | {serper} | {credits:.1f} | {time_s:.2f} |".format(
                query=query[:48].replace("|", " "),
                mode=item.get("research_mode", ""),
                status=item.get("status", ""),
                llm=float(item.get("llm_cost_rmb", 0.0)),
                ext=float(item.get("external_cost_usd_est", 0.0)),
                serper=int(item.get("serper_queries", 0)),
                credits=float(item.get("tavily_credits_est", 0.0)),
                time_s=float(item.get("elapsed_seconds", item.get("wall_clock_seconds", 0.0))),
            )
        )

    lines.append("")
    for item in results:
        lines.append(f"## {item.get('research_mode', '')} | {item.get('query', '')}")
        lines.append(f"- 状态: {item.get('status')}")
        lines.append(f"- LLM 成本: RMB {float(item.get('llm_cost_rmb', 0.0)):.4f}")
        lines.append(f"- 外部检索成本: USD {float(item.get('external_cost_usd_est', 0.0)):.4f}")
        lines.append(f"- Serper 请求数: {int(item.get('serper_queries', 0))}")
        lines.append(f"- Tavily Credits: {float(item.get('tavily_credits_est', 0.0)):.1f}")
        lines.append(f"- 用时: {float(item.get('elapsed_seconds', item.get('wall_clock_seconds', 0.0))):.2f}s")
        if item.get("success"):
            lines.append(f"- 预览: {(item.get('report_preview') or '').replace(chr(10), ' ')[:220]}")
        else:
            lines.append(f"- 失败: {item.get('error')}")
        lines.append("")
    return "\n".join(lines)


def main():
    results = []
    total_llm_cost_rmb = 0.0
    stopped_early = False
    queries = _selected_queries()
    modes = _selected_modes()

    for query in queries:
        if stopped_early:
            break
        for mode in modes:
            if total_llm_cost_rmb >= BENCHMARK_MAX_TOTAL_RMB:
                stopped_early = True
                break
            print(
                f"[benchmark] running mode={mode} query={query[:60]} "
                f"(spent={total_llm_cost_rmb:.4f}/{BENCHMARK_MAX_TOTAL_RMB:.2f} RMB)"
            )
            result = _run_once(query, mode)
            results.append(result)
            total_llm_cost_rmb += float(result.get("llm_cost_rmb", 0.0))
            if total_llm_cost_rmb >= BENCHMARK_MAX_TOTAL_RMB:
                print("[benchmark] total LLM budget reached, stopping remaining runs")
                stopped_early = True
                break

    reports_dir = _reports_dir()
    stamp = time.strftime("%Y%m%d_%H%M%S")
    json_path = reports_dir / f"mode_benchmark_{stamp}.json"
    md_path = reports_dir / f"mode_benchmark_{stamp}.md"
    payload = {
        "benchmark_max_task_rmb": BENCHMARK_MAX_TASK_RMB,
        "benchmark_max_total_rmb": BENCHMARK_MAX_TOTAL_RMB,
        "actual_total_llm_cost_rmb": round(total_llm_cost_rmb, 6),
        "stopped_early": stopped_early,
        "results": results,
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(_to_markdown(results, total_llm_cost_rmb), encoding="utf-8")
    print(f"[benchmark] json={json_path}")
    print(f"[benchmark] markdown={md_path}")


if __name__ == "__main__":
    main()
