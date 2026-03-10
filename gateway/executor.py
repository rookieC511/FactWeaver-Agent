import asyncio
import os
import time
from typing import Any

from core.config import MAX_TASK_DURATION_SECONDS, MAX_TASK_NODE_COUNT, MAX_TASK_RMB_COST
from core.memory import activate_session, cleanup_session_km, reset_active_session
from gateway.state_store import cache_report, upsert_task


def _status_detail_for_node(node_name: str, current_cost: float) -> str:
    if node_name == "planner":
        return "正在生成调研大纲与检索计划..."
    if node_name == "human_review":
        return "大纲已确认，准备执行检索..."
    if node_name == "executor":
        return f"深度检索执行中，当前模型成本约 RMB {current_cost:.4f}"
    if node_name == "writer":
        return "正在汇总资料并生成最终报告..."
    return f"节点执行中: {node_name}"


def _coerce_costs(cost_breakdown: dict[str, Any] | None) -> dict[str, float | int]:
    data = dict(cost_breakdown or {})
    return {
        "external_cost_usd_est": float(data.get("external_cost_usd_est", 0.0)),
        "serper_queries": int(data.get("serper_queries", 0)),
        "serper_cost_usd_est": float(data.get("serper_cost_usd_est", 0.0)),
        "tavily_credits_est": float(data.get("tavily_credits_est", 0.0)),
        "tavily_cost_usd_est": float(data.get("tavily_cost_usd_est", 0.0)),
    }


async def run_research_job(
    task_id: str,
    query: str,
    *,
    backend: str,
    research_mode: str,
    disable_cache: bool = False,
) -> dict[str, Any]:
    os.environ["FACTWEAVER_API_MODE"] = "1"

    from core.graph import app as langgraph_app
    from core.models import global_cost_tracker

    token = activate_session(task_id)
    start_time = time.time()
    node_count = 0
    merged_state: dict[str, Any] = {}

    upsert_task(
        task_id,
        query,
        "STARTED",
        detail="系统正在初始化深度研究任务...",
        thread_id=task_id,
        backend=backend,
        research_mode=research_mode,
    )

    try:
        global_cost_tracker.reset()
        async for event in langgraph_app.astream(
            {
                "query": query,
                "task_id": task_id,
                "iteration": 1,
                "plan": [],
                "outline": [],
                "user_feedback": "",
                "final_report": "",
                "metrics": {"tool_calls": 0, "backtracking": 0},
                "history": [],
                "conflict_detected": False,
                "conflict_count": 0,
                "missing_sources": [],
                "degraded_items": [],
                "research_mode": research_mode,
                "cost_breakdown": {},
                "retrieval_metrics": {},
            },
            config={"configurable": {"thread_id": task_id}},
        ):
            for node_name, node_state in event.items():
                node_count += 1
                elapsed = time.time() - start_time
                current_cost = global_cost_tracker.total_cost_rmb
                merged_state.update(node_state)
                external_costs = _coerce_costs(merged_state.get("cost_breakdown"))

                if elapsed > MAX_TASK_DURATION_SECONDS:
                    raise RuntimeError(
                        f"Task timed out after {elapsed:.0f}s (limit {MAX_TASK_DURATION_SECONDS}s)"
                    )
                if node_count > MAX_TASK_NODE_COUNT:
                    raise RuntimeError(
                        f"Task exceeded node budget: {node_count} > {MAX_TASK_NODE_COUNT}"
                    )
                if current_cost > MAX_TASK_RMB_COST:
                    raise RuntimeError(
                        f"Task exceeded budget: RMB {current_cost:.4f} > RMB {MAX_TASK_RMB_COST:.4f}"
                    )

                upsert_task(
                    task_id,
                    query,
                    "STARTED",
                    detail=_status_detail_for_node(node_name, current_cost),
                    thread_id=task_id,
                    backend=backend,
                    research_mode=research_mode,
                    llm_cost_rmb=current_cost,
                    external_cost_usd_est=external_costs["external_cost_usd_est"],
                    serper_queries=external_costs["serper_queries"],
                    serper_cost_usd_est=external_costs["serper_cost_usd_est"],
                    tavily_credits_est=external_costs["tavily_credits_est"],
                    tavily_cost_usd_est=external_costs["tavily_cost_usd_est"],
                    elapsed_seconds=elapsed,
                )

        if not merged_state:
            raise RuntimeError("Research pipeline ended without final state")

        elapsed = time.time() - start_time
        report = merged_state.get("final_report", "报告生成失败")
        external_costs = _coerce_costs(merged_state.get("cost_breakdown"))
        retrieval_metrics = dict(merged_state.get("retrieval_metrics") or {})

        if not disable_cache:
            cache_report(
                query,
                report,
                research_mode=research_mode,
                metadata={
                    "task_id": task_id,
                    "research_mode": research_mode,
                    "llm_cost_rmb": global_cost_tracker.total_cost_rmb,
                    "external_cost_usd_est": external_costs["external_cost_usd_est"],
                    "node_count": node_count,
                    "elapsed_seconds": elapsed,
                },
            )

        upsert_task(
            task_id,
            query,
            "SUCCESS",
            detail="研究任务已完成",
            report=report,
            thread_id=task_id,
            backend=backend,
            research_mode=research_mode,
            llm_cost_rmb=global_cost_tracker.total_cost_rmb,
            external_cost_usd_est=external_costs["external_cost_usd_est"],
            serper_queries=external_costs["serper_queries"],
            serper_cost_usd_est=external_costs["serper_cost_usd_est"],
            tavily_credits_est=external_costs["tavily_credits_est"],
            tavily_cost_usd_est=external_costs["tavily_cost_usd_est"],
            elapsed_seconds=elapsed,
        )
        return {
            "task_id": task_id,
            "thread_id": task_id,
            "status": "SUCCESS",
            "report": report,
            "research_mode": research_mode,
            "llm_cost_rmb": global_cost_tracker.total_cost_rmb,
            "external_cost_usd_est": external_costs["external_cost_usd_est"],
            "serper_queries": external_costs["serper_queries"],
            "serper_cost_usd_est": external_costs["serper_cost_usd_est"],
            "tavily_credits_est": external_costs["tavily_credits_est"],
            "tavily_cost_usd_est": external_costs["tavily_cost_usd_est"],
            "retrieval_metrics": retrieval_metrics,
            "node_count": node_count,
            "elapsed_seconds": elapsed,
        }
    except Exception as exc:
        elapsed = time.time() - start_time
        external_costs = _coerce_costs(merged_state.get("cost_breakdown"))
        upsert_task(
            task_id,
            query,
            "FAILED",
            detail=f"任务执行失败: {exc}",
            thread_id=task_id,
            backend=backend,
            last_error=repr(exc),
            research_mode=research_mode,
            llm_cost_rmb=global_cost_tracker.total_cost_rmb,
            external_cost_usd_est=external_costs["external_cost_usd_est"],
            serper_queries=external_costs["serper_queries"],
            serper_cost_usd_est=external_costs["serper_cost_usd_est"],
            tavily_credits_est=external_costs["tavily_credits_est"],
            tavily_cost_usd_est=external_costs["tavily_cost_usd_est"],
            elapsed_seconds=elapsed,
        )
        raise
    finally:
        cleanup_session_km(task_id)
        reset_active_session(token)


def run_research_job_sync(
    task_id: str,
    query: str,
    *,
    backend: str,
    research_mode: str,
    disable_cache: bool = False,
) -> dict[str, Any]:
    return asyncio.run(
        run_research_job(
            task_id,
            query,
            backend=backend,
            research_mode=research_mode,
            disable_cache=disable_cache,
        )
    )
