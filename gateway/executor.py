import asyncio
import os
import time
from typing import Any

from core.config import DEFAULT_RESEARCH_MODE, MAX_TASK_DURATION_SECONDS, MAX_TASK_NODE_COUNT, MAX_TASK_RMB_COST
from core.costs import enrich_cost_fields
from core.memory import (
    activate_session,
    cleanup_session_km,
    get_current_km,
    reset_active_session,
    restore_session_km,
)
from core.runtime_control import configured_interrupt_point, crash_process, should_interrupt_task
from gateway.state_store import (
    cache_report,
    get_latest_knowledge_snapshot,
    get_task,
    save_knowledge_snapshot,
    upsert_task,
)


def _status_detail_for_node(node_name: str, current_cost: float, resume: bool = False) -> str:
    prefix = "恢复中，" if resume else ""
    if node_name == "planner":
        return f"{prefix}正在生成研究大纲与检索计划..."
    if node_name == "human_review":
        return f"{prefix}大纲已确认，准备执行检索..."
    if node_name == "executor":
        return f"{prefix}深度检索执行中，当前模型成本约 RMB {current_cost:.4f}"
    if node_name == "writer":
        return f"{prefix}正在汇总资料并生成最终报告..."
    return f"{prefix}节点执行中: {node_name}"


def _coerce_costs(cost_breakdown: dict[str, Any] | None) -> dict[str, float | int]:
    data = dict(cost_breakdown or {})
    return {
        "external_cost_usd_est": float(data.get("external_cost_usd_est", 0.0)),
        "serper_queries": int(data.get("serper_queries", 0)),
        "serper_cost_usd_est": float(data.get("serper_cost_usd_est", 0.0)),
        "tavily_credits_est": float(data.get("tavily_credits_est", 0.0)),
        "tavily_cost_usd_est": float(data.get("tavily_cost_usd_est", 0.0)),
    }


def _graph_input(task_id: str, query: str, research_mode: str) -> dict[str, Any]:
    return {
        "query": query,
        "task_id": task_id,
        "iteration": 1,
        "plan": [],
        "outline": [],
        "task_contract": {},
        "evidence_slots": {},
        "draft_audit": {},
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
        "source_candidates": [],
        "fetch_results": [],
        "coverage_summary": {},
        "backfill_attempts": 0,
        "retrieval_failed": False,
    }


def _graph_config(task_id: str) -> dict[str, Any]:
    return {"configurable": {"thread_id": task_id}}


def _checkpoint_meta(langgraph_app: Any, task_id: str) -> tuple[str | None, str | None, tuple[Any, ...]]:
    try:
        state = langgraph_app.get_state(_graph_config(task_id))
        configurable = dict(state.config.get("configurable", {}))
        checkpoint_id = configurable.get("checkpoint_id")
        checkpoint_ns = configurable.get("checkpoint_ns")
        return checkpoint_id, checkpoint_ns, tuple(state.next or ())
    except Exception:
        return None, None, ()


def _persist_runtime_snapshot(
    *,
    langgraph_app: Any,
    task_id: str,
    query: str,
    backend: str,
    research_mode: str,
    status: str,
    detail: str,
    current_cost: float,
    merged_state: dict[str, Any],
    elapsed_seconds: float,
    attempt_count: int,
    resume_count: int,
    resumed_from_checkpoint: bool,
    started_at: int | None,
    completed_at: int | None,
    checkpoint_node: str | None,
    interruption_state: str | None,
    last_error: str | None = None,
    report: str | None = None,
) -> tuple[str | None, str | None, int]:
    external_costs = _coerce_costs(merged_state.get("cost_breakdown"))
    checkpoint_id, checkpoint_ns, _ = _checkpoint_meta(langgraph_app, task_id)
    snapshot_id = save_knowledge_snapshot(
        task_id,
        thread_id=task_id,
        checkpoint_id=checkpoint_id,
        checkpoint_ns=checkpoint_ns,
        checkpoint_node=checkpoint_node,
        snapshot=get_current_km().snapshot(),
    )
    upsert_task(
        task_id,
        query,
        status,
        detail=detail,
        report=report,
        thread_id=task_id,
        backend=backend,
        last_error=last_error,
        research_mode=research_mode,
        llm_cost_rmb=current_cost,
        external_cost_usd_est=external_costs["external_cost_usd_est"],
        serper_queries=external_costs["serper_queries"],
        serper_cost_usd_est=external_costs["serper_cost_usd_est"],
        tavily_credits_est=external_costs["tavily_credits_est"],
        tavily_cost_usd_est=external_costs["tavily_cost_usd_est"],
        elapsed_seconds=elapsed_seconds,
        attempt_count=attempt_count,
        resume_count=resume_count,
        resumed_from_checkpoint=resumed_from_checkpoint,
        started_at=started_at,
        completed_at=completed_at,
        last_checkpoint_id=checkpoint_id,
        last_checkpoint_ns=checkpoint_ns,
        last_checkpoint_node=checkpoint_node,
        interruption_state=interruption_state,
        last_km_snapshot_id=snapshot_id,
    )
    return checkpoint_id, checkpoint_ns, snapshot_id


async def run_research_job(
    task_id: str,
    query: str,
    *,
    backend: str,
    research_mode: str,
    disable_cache: bool = False,
    resume_from_checkpoint: bool = False,
) -> dict[str, Any]:
    os.environ["FACTWEAVER_API_MODE"] = "1"

    from core.graph import app as langgraph_app
    from core.models import global_cost_tracker

    existing_task = get_task(task_id) or {}
    research_mode = (research_mode or existing_task.get("research_mode") or DEFAULT_RESEARCH_MODE).strip().lower()
    query = query or existing_task.get("query") or ""
    if not query:
        raise ValueError("query is required to run or resume a research job")

    previous_resume_count = int(existing_task.get("resume_count") or 0)
    previous_attempt_count = int(existing_task.get("attempt_count") or 0)
    previous_llm_cost = float(existing_task.get("llm_cost_rmb") or 0.0)
    elapsed_offset = float(existing_task.get("elapsed_seconds") or 0.0)
    started_at = int(existing_task.get("started_at") or time.time())
    resume_count = previous_resume_count + (1 if resume_from_checkpoint else 0)
    attempt_count = previous_attempt_count + 1

    token = activate_session(task_id)
    segment_start = time.time()
    node_count = 0
    merged_state: dict[str, Any] = {}

    try:
        if resume_from_checkpoint:
            latest_snapshot = get_latest_knowledge_snapshot(task_id)
            if not latest_snapshot:
                raise RuntimeError(f"No durable KnowledgeManager snapshot found for task {task_id}")
            restore_session_km(task_id, latest_snapshot.get("snapshot"))
        global_cost_tracker.reset()
        global_cost_tracker.total_cost_rmb = previous_llm_cost

        start_detail = "检测到可恢复状态，正在从最近 checkpoint 恢复..." if resume_from_checkpoint else "系统正在初始化深度研究任务..."
        upsert_task(
            task_id,
            query,
            "STARTED",
            detail=start_detail,
            thread_id=task_id,
            backend=backend,
            research_mode=research_mode,
            llm_cost_rmb=previous_llm_cost,
            elapsed_seconds=elapsed_offset,
            attempt_count=attempt_count,
            resume_count=resume_count,
            resumed_from_checkpoint=resume_from_checkpoint,
            started_at=started_at,
            interruption_state="resuming" if resume_from_checkpoint else "running",
        )

        interrupt_point = configured_interrupt_point()
        top_level_interrupts = None
        if should_interrupt_task(task_id, interrupt_point) and interrupt_point in {"planner", "executor"}:
            top_level_interrupts = [interrupt_point]

        async for event in langgraph_app.astream(
            None if resume_from_checkpoint else _graph_input(task_id, query, research_mode),
            config=_graph_config(task_id),
            interrupt_after=top_level_interrupts,
        ):
            if "__interrupt__" in event:
                elapsed = elapsed_offset + (time.time() - segment_start)
                current_cost = global_cost_tracker.total_cost_rmb
                _persist_runtime_snapshot(
                    langgraph_app=langgraph_app,
                    task_id=task_id,
                    query=query,
                    backend=backend,
                    research_mode=research_mode,
                    status="INTERRUPTED",
                    detail=f"任务在安全点 {interrupt_point} 后被测试注入中断",
                    current_cost=current_cost,
                    merged_state=merged_state,
                    elapsed_seconds=elapsed,
                    attempt_count=attempt_count,
                    resume_count=resume_count,
                    resumed_from_checkpoint=resume_from_checkpoint,
                    started_at=started_at,
                    completed_at=None,
                    checkpoint_node=interrupt_point,
                    interruption_state=interrupt_point,
                )
                crash_process()

            for node_name, node_state in event.items():
                node_count += 1
                elapsed = elapsed_offset + (time.time() - segment_start)
                current_cost = global_cost_tracker.total_cost_rmb
                merged_state.update(node_state)

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

                _persist_runtime_snapshot(
                    langgraph_app=langgraph_app,
                    task_id=task_id,
                    query=query,
                    backend=backend,
                    research_mode=research_mode,
                    status="STARTED",
                    detail=_status_detail_for_node(node_name, current_cost, resume=resume_from_checkpoint),
                    current_cost=current_cost,
                    merged_state=merged_state,
                    elapsed_seconds=elapsed,
                    attempt_count=attempt_count,
                    resume_count=resume_count,
                    resumed_from_checkpoint=resume_from_checkpoint,
                    started_at=started_at,
                    completed_at=None,
                    checkpoint_node=node_name,
                    interruption_state="running",
                )

        if not merged_state:
            raise RuntimeError("Research pipeline ended without final state")

        elapsed = elapsed_offset + (time.time() - segment_start)
        report = merged_state.get("final_report", "报告生成失败")
        external_costs = _coerce_costs(merged_state.get("cost_breakdown"))
        retrieval_metrics = dict(merged_state.get("retrieval_metrics") or {})
        retrieval_failed = bool(merged_state.get("retrieval_failed"))
        final_status = "FAILED" if (backend == "drb_public_benchmark" and retrieval_failed) else "SUCCESS"
        final_detail = "证据获取不足，未进入正式写作阶段" if retrieval_failed else "研究任务已完成"

        if not disable_cache and not retrieval_failed:
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

        _persist_runtime_snapshot(
            langgraph_app=langgraph_app,
            task_id=task_id,
            query=query,
            backend=backend,
            research_mode=research_mode,
            status=final_status,
            detail=final_detail,
            current_cost=global_cost_tracker.total_cost_rmb,
            merged_state=merged_state,
            elapsed_seconds=elapsed,
            attempt_count=attempt_count,
            resume_count=resume_count,
            resumed_from_checkpoint=resume_from_checkpoint,
            started_at=started_at,
            completed_at=int(time.time()),
            checkpoint_node="executor" if retrieval_failed else "writer",
            interruption_state="completed",
            report=report,
        )
        return enrich_cost_fields(
            {
                "task_id": task_id,
                "thread_id": task_id,
                "status": final_status,
                "report": report,
                "research_mode": research_mode,
                "llm_cost_rmb": global_cost_tracker.total_cost_rmb,
                "external_cost_usd_est": external_costs["external_cost_usd_est"],
                "serper_queries": external_costs["serper_queries"],
                "serper_cost_usd_est": external_costs["serper_cost_usd_est"],
                "tavily_credits_est": external_costs["tavily_credits_est"],
                "tavily_cost_usd_est": external_costs["tavily_cost_usd_est"],
                "retrieval_metrics": retrieval_metrics,
                "task_contract": dict(merged_state.get("task_contract") or {}),
                "evidence_slots": dict(merged_state.get("evidence_slots") or {}),
                "draft_audit": dict(merged_state.get("draft_audit") or {}),
                "source_candidates": list(merged_state.get("source_candidates") or []),
                "fetch_results": list(merged_state.get("fetch_results") or []),
                "coverage_summary": dict(merged_state.get("coverage_summary") or {}),
                "backfill_attempts": int(merged_state.get("backfill_attempts") or 0),
                "retrieval_failed": retrieval_failed,
                "node_count": node_count,
                "elapsed_seconds": elapsed,
                "attempt_count": attempt_count,
                "resume_count": resume_count,
                "resumed_from_checkpoint": resume_from_checkpoint,
            }
        )
    except Exception as exc:
        elapsed = elapsed_offset + (time.time() - segment_start)
        _persist_runtime_snapshot(
            langgraph_app=langgraph_app,
            task_id=task_id,
            query=query,
            backend=backend,
            research_mode=research_mode,
            status="FAILED",
            detail=f"任务执行失败: {exc}",
            current_cost=float(getattr(global_cost_tracker, "total_cost_rmb", 0.0)),
            merged_state=merged_state,
            elapsed_seconds=elapsed,
            attempt_count=attempt_count,
            resume_count=resume_count,
            resumed_from_checkpoint=resume_from_checkpoint,
            started_at=started_at,
            completed_at=int(time.time()),
            checkpoint_node=(get_task(task_id) or {}).get("last_checkpoint_node"),
            interruption_state="failed",
            last_error=repr(exc),
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
    resume_from_checkpoint: bool = False,
) -> dict[str, Any]:
    return asyncio.run(
        run_research_job(
            task_id,
            query,
            backend=backend,
            research_mode=research_mode,
            disable_cache=disable_cache,
            resume_from_checkpoint=resume_from_checkpoint,
        )
    )
