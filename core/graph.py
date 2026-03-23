import asyncio
import datetime
import json
from typing import Any, List, TypedDict

from langchain_core.messages import HumanMessage
from langgraph.graph import END, StateGraph

from core.checkpoint import get_sqlite_checkpointer
from core.config import CHECKPOINT_DB_PATH, DEFAULT_ARCHITECTURE_MODE, DEFAULT_RESEARCH_MODE
from core.evidence_acquisition import fetch_source_candidate
from core.memory import get_current_km, get_current_session_id
from core.models import llm_fast
from core.multi_agent_runtime import (
    build_task_ledger,
    normalize_architecture_mode,
    update_progress_ledger,
)
from core.research_supervisor_runtime import decide_supervisor_decision, run_supervisor
from core.research_team_runtime import run_research_team
from core.source_policy import rank_search_results
from core.tools import (
    LLMFormatError,
    ToolExecutionError,
    clean_json_output,
    default_cost_breakdown,
    default_retrieval_metrics,
    record_serper_query,
    record_tavily_credits,
    scrape_jina_ai,
    serper_client,
    tavily_crawl_client,
    tavily_crawl_credits,
    tavily_extract_client,
    tavily_extract_credits,
    tavily_map_client,
    tavily_map_credits,
    tavily_search_client,
    tavily_search_credits,
)
from core.writer_team_runtime import run_writer_team


class ResearchState(TypedDict):
    query: str
    research_mode: str
    architecture_mode: str
    plan: List[dict]
    outline: List[dict]
    task_contract: dict
    task_ledger: dict
    progress_ledger: dict
    evidence_slots: dict
    draft_audit: dict
    research_team_result: dict
    writer_team_result: dict
    supervisor_decision: dict
    retrieval_plan: dict
    evidence_digest: dict
    team_route_trace: List[dict]
    current_phase: str
    supervisor_next_node: str
    bundle_ref: str
    draft_ref: str
    user_feedback: str
    iteration: int
    final_report: str
    metrics: dict
    task_id: str
    history: List[dict]
    conflict_detected: bool
    conflict_count: int
    missing_sources: List[dict]
    degraded_items: List[dict]
    cost_breakdown: dict
    retrieval_metrics: dict
    source_candidates: List[dict]
    fetch_results: List[dict]
    coverage_summary: dict
    backfill_attempts: int
    retrieval_failed: bool


TRAJECTORY_FILE = "trajectory_log.jsonl"


def log_trajectory(task_id: str | None, event_type: str, data: dict) -> None:
    if not task_id:
        return
    record = {
        "timestamp": datetime.datetime.now().isoformat(),
        "task_id": str(task_id),
        "event": event_type,
        "data": data,
    }
    try:
        with open(TRAJECTORY_FILE, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass


async def safe_ainvoke(llm, prompt: str, max_retries: int = 3):
    for attempt in range(max_retries):
        try:
            return await llm.ainvoke([HumanMessage(content=prompt)])
        except Exception as exc:
            if attempt == max_retries - 1:
                print(f"[Graph] LLM call failed after {max_retries} attempts: {exc}")
                return None
            await asyncio.sleep(2**attempt)


def _fallback_outline(query: str) -> list[dict[str, str]]:
    return [
        {"id": "1", "title": "背景概览", "description": f"介绍 {query} 的背景与范围"},
        {"id": "2", "title": "关键发现", "description": f"梳理 {query} 的核心事实与结论"},
    ]


def _fallback_plan(query: str, outline: list[dict[str, str]]) -> list[dict[str, str]]:
    plan = []
    for section in outline:
        plan.append(
            {
                "task": f"{query} {section['title']}",
                "reason": section["description"],
                "section_id": section["id"],
            }
        )
    return plan


def _extract_comparison_targets(query: str) -> list[str]:
    normalized = " ".join((query or "").replace(" vs. ", " vs ").replace(" versus ", " vs ").split())
    match = __import__("re").search(r"(.+?)\s+vs\s+(.+)", normalized, __import__("re").IGNORECASE)
    if not match:
        return []
    return [match.group(1).strip(), match.group(2).strip()]


def _extract_required_constraints(query: str) -> list[str]:
    lowered = (query or "").lower()
    constraints: list[str] = []
    for keyword in ("latest", "current", "today", "2024", "2025", "2026", "china", "us", "global"):
        if keyword in lowered:
            constraints.append(keyword)
    return constraints


def _fallback_task_contract(query: str, outline: list[dict[str, Any]], plan: list[dict[str, Any]]) -> dict[str, Any]:
    must_answer_points: list[dict[str, Any]] = []
    for index, task in enumerate(plan[:5], start=1):
        section_id = str(task.get("section_id") or outline[min(index - 1, len(outline) - 1)].get("id") if outline else index)
        must_answer_points.append(
            {
                "id": str(index),
                "section_id": section_id,
                "question": str(task.get("task") or task.get("reason") or query),
            }
        )
    comparison_targets = _extract_comparison_targets(query)
    required_analysis_modes = ["risk", "causal"]
    if comparison_targets:
        required_analysis_modes.insert(0, "comparison")
    return {
        "direct_question": query,
        "must_answer_points": must_answer_points,
        "comparison_targets": comparison_targets,
        "required_constraints": _extract_required_constraints(query),
        "required_analysis_modes": list(dict.fromkeys(required_analysis_modes)),
    }


def _normalize_task_contract(
    task_contract: dict[str, Any] | None,
    *,
    query: str,
    outline: list[dict[str, Any]],
    plan: list[dict[str, Any]],
) -> dict[str, Any]:
    base = _fallback_task_contract(query, outline, plan)
    incoming = dict(task_contract or {})
    must_answer_points: list[dict[str, Any]] = []
    for index, point in enumerate(incoming.get("must_answer_points") or [], start=1):
        if isinstance(point, dict):
            must_answer_points.append(
                {
                    "id": str(point.get("id") or index),
                    "section_id": str(point.get("section_id") or point.get("id") or base["must_answer_points"][min(index - 1, len(base["must_answer_points"]) - 1)]["section_id"]),
                    "question": str(point.get("question") or point.get("title") or ""),
                }
            )
        elif str(point).strip():
            must_answer_points.append(
                {
                    "id": str(index),
                    "section_id": base["must_answer_points"][min(index - 1, len(base["must_answer_points"]) - 1)]["section_id"],
                    "question": str(point).strip(),
                }
            )
    if len(must_answer_points) < 2:
        must_answer_points = base["must_answer_points"]
    must_answer_points = must_answer_points[:5]
    comparison_targets = [str(item).strip() for item in (incoming.get("comparison_targets") or base["comparison_targets"]) if str(item).strip()]
    required_constraints = [str(item).strip() for item in (incoming.get("required_constraints") or base["required_constraints"]) if str(item).strip()]
    required_analysis_modes = [str(item).strip().lower() for item in (incoming.get("required_analysis_modes") or base["required_analysis_modes"]) if str(item).strip()]
    if comparison_targets and "comparison" not in required_analysis_modes:
        required_analysis_modes.insert(0, "comparison")
    for default_mode in ("causal", "risk"):
        if default_mode not in required_analysis_modes:
            required_analysis_modes.append(default_mode)
    return {
        "direct_question": str(incoming.get("direct_question") or query).strip(),
        "must_answer_points": must_answer_points,
        "comparison_targets": comparison_targets,
        "required_constraints": required_constraints,
        "required_analysis_modes": list(dict.fromkeys(required_analysis_modes)),
    }


def _research_mode(state: ResearchState) -> str:
    return (state.get("research_mode") or DEFAULT_RESEARCH_MODE).strip().lower()


def _merge_metrics(base: dict[str, int], updates: dict[str, int]) -> dict[str, int]:
    merged = dict(base)
    for key, value in updates.items():
        merged[key] = int(merged.get(key, 0)) + int(value)
    return merged


async def _mode_presearch(
    mode: str,
    query: str,
    *,
    cost_breakdown: dict[str, float | int],
    retrieval_metrics: dict[str, int],
) -> tuple[list[dict[str, Any]], dict[str, float | int], dict[str, int]]:
    if mode == "high":
        response = await tavily_search_client.asearch(query=query, max_results=3, search_depth="advanced")
        cost_breakdown = record_tavily_credits(cost_breakdown, tavily_search_credits("advanced"))
    else:
        response = await serper_client.asearch(query=query, max_results=3, search_depth="basic")
        cost_breakdown = record_serper_query(cost_breakdown, 1)
    retrieval_metrics = _merge_metrics(retrieval_metrics, {"search_calls": 1})
    ranked_results = rank_search_results(response.get("results", []), query, limit=3)
    retrieval_metrics = _update_search_result_metrics(retrieval_metrics, ranked_results)
    return ranked_results, cost_breakdown, retrieval_metrics


async def _mode_search(
    mode: str,
    query: str,
    *,
    max_results: int,
    cost_breakdown: dict[str, float | int],
    retrieval_metrics: dict[str, int],
) -> tuple[list[dict[str, Any]], dict[str, float | int], dict[str, int]]:
    if mode == "high":
        response = await tavily_search_client.asearch(query=query, max_results=max_results, search_depth="advanced")
        cost_breakdown = record_tavily_credits(cost_breakdown, tavily_search_credits("advanced"))
    else:
        response = await serper_client.asearch(query=query, max_results=max_results, search_depth="basic")
        cost_breakdown = record_serper_query(cost_breakdown, 1)
    retrieval_metrics = _merge_metrics(retrieval_metrics, {"search_calls": 1})
    ranked_results = rank_search_results(response.get("results", []), query, limit=max_results)
    retrieval_metrics = _update_search_result_metrics(retrieval_metrics, ranked_results)
    return ranked_results, cost_breakdown, retrieval_metrics






def _update_search_result_metrics(
    retrieval_metrics: dict[str, int],
    ranked_results: list[dict[str, Any]],
) -> dict[str, int]:
    updates = {
        "search_result_count": len(ranked_results),
        "authority_hits": sum(1 for item in ranked_results if item.get("source_tier") == "high_authority"),
        "weak_source_hits": sum(1 for item in ranked_results if item.get("source_tier") == "weak"),
    }
    return _merge_metrics(retrieval_metrics, updates)






def _architecture_mode(state: ResearchState) -> str:
    return normalize_architecture_mode(state.get("architecture_mode") or DEFAULT_ARCHITECTURE_MODE)








async def node_init_search(state: ResearchState):
    query = state["query"]
    mode = _research_mode(state)
    architecture_mode = _architecture_mode(state)
    task_id = state.get("task_id") or get_current_session_id()
    km = get_current_km()
    await km.aclear()
    cost_breakdown = dict(state.get("cost_breakdown") or default_cost_breakdown())
    retrieval_metrics = dict(state.get("retrieval_metrics") or default_retrieval_metrics())

    context = ""
    presearch_results = []
    for attempt in range(3):
        try:
            presearch_results, cost_breakdown, retrieval_metrics = await _mode_presearch(
                mode,
                query,
                cost_breakdown=cost_breakdown,
                retrieval_metrics=retrieval_metrics,
            )
            for item in presearch_results:
                km.add_compact_document(item.get("content", ""), item.get("url", ""), item.get("title", ""))
            context = "\n".join(
                f"- {item.get('title', '')}: {item.get('content', '')}"
                for item in presearch_results
            )
            break
        except ToolExecutionError as exc:
            log_trajectory(task_id, "presearch_error", {"query": query, "error": str(exc)})
            await asyncio.sleep(2**attempt)
        except Exception as exc:
            log_trajectory(task_id, "presearch_error", {"query": query, "error": str(exc)})
            await asyncio.sleep(2**attempt)

    prompt = f"""
# Context
User query: {query}
Current environment:
{context[:2000]}

# Objective
Return strict JSON with:
{{
  "outline": [{{"id": "1", "title": "Section", "description": "What to cover"}}],
  "search_tasks": [{{"task": "query", "reason": "why", "section_id": "1"}}],
  "task_contract": {{
    "direct_question": "the exact user question to answer",
    "must_answer_points": [
      {{"id": "1", "section_id": "1", "question": "specific, checkable sub-question"}}
    ],
    "comparison_targets": ["optional entity A", "optional entity B"],
    "required_constraints": ["time, region, or scope constraints"],
    "required_analysis_modes": ["comparison", "causal", "risk"]
  }}
}}
"""
    resp = await safe_ainvoke(llm_fast, prompt)
    plan_data: Any = {}
    if resp:
        try:
            plan_data = clean_json_output(resp.content, strict=True)
        except LLMFormatError as exc:
            log_trajectory(
                task_id,
                "planner_json_fallback",
                {"query": query, "error": exc.parse_error, "raw": exc.raw_text[:300]},
            )

    outline = []
    plan = []
    task_contract: dict[str, Any] = {}
    if isinstance(plan_data, dict):
        outline = plan_data.get("outline", [])
        plan = plan_data.get("search_tasks", [])
        task_contract = plan_data.get("task_contract", {})

    if not isinstance(outline, list) or not outline:
        outline = _fallback_outline(query)
    if not isinstance(plan, list) or not plan:
        plan = _fallback_plan(query, outline)
    task_contract = _normalize_task_contract(task_contract, query=query, outline=outline, plan=plan)

    metrics = state.get("metrics", {"tool_calls": 0, "backtracking": 0})
    if state.get("user_feedback"):
        metrics["backtracking"] = metrics.get("backtracking", 0) + 1

    history = state.get("history", [])
    history.append(
        {
            "role": "planner_cot",
            "content": {"query": query, "outline": outline, "search_tasks": plan, "task_contract": task_contract},
        }
    )
    log_trajectory(task_id, "planner", {"outline": outline, "search_tasks": plan, "task_contract": task_contract})

    return {
        "architecture_mode": architecture_mode,
        "plan": plan,
        "outline": outline,
        "task_contract": task_contract,
        "task_ledger": build_task_ledger(query=query, task_contract=task_contract, plan=plan),
        "progress_ledger": update_progress_ledger(
            state.get("progress_ledger"),
            last_team_called="Planner",
            next_action_rationale="Planner generated outline and search plan.",
            reset_team_stall=True,
        ),
        "evidence_slots": state.get("evidence_slots", {}),
        "draft_audit": state.get("draft_audit", {}),
        "research_team_result": {},
        "writer_team_result": {},
        "supervisor_decision": {},
        "retrieval_plan": {},
        "evidence_digest": {},
        "iteration": state.get("iteration", 0) + 1,
        "metrics": metrics,
        "history": history,
        "conflict_detected": False,
        "missing_sources": state.get("missing_sources", []),
        "degraded_items": state.get("degraded_items", []),
        "research_mode": mode,
        "cost_breakdown": cost_breakdown,
        "retrieval_metrics": retrieval_metrics,
        "current_phase": "HUMAN_REVIEW",
    }


async def node_human_feedback(state: ResearchState):
    if __import__("os").environ.get("FACTWEAVER_API_MODE") == "1":
        return {
            "user_feedback": "",
            "current_phase": "RESEARCH",
            "progress_ledger": update_progress_ledger(
                state.get("progress_ledger"),
                last_team_called="Human Review",
                next_action_rationale="Human review skipped in API mode; proceed to Research Team.",
            ),
        }

    print("\n[Human Review] Outline:")
    for section in state["outline"]:
        print(f"- [{section.get('id')}] {section.get('title')}")
    print("\n[Human Review] Search tasks:")
    for index, item in enumerate(state["plan"], start=1):
        print(f"- [{index}] {item.get('task')}")

    user_input = input("\nEnter to continue, text to revise, q to quit: ").strip()
    if user_input.lower() == "q":
        return {
            "final_report": "User Terminated",
            "current_phase": "FAIL_HARD",
            "progress_ledger": update_progress_ledger(
                state.get("progress_ledger"),
                last_team_called="Human Review",
                next_action_rationale="User terminated task during human review.",
            ),
        }
    if user_input:
        return {
            "user_feedback": user_input,
            "current_phase": "PLAN",
            "progress_ledger": update_progress_ledger(
                state.get("progress_ledger"),
                last_team_called="Human Review",
                next_action_rationale="Human review requested planner revision.",
            ),
        }
    return {
        "user_feedback": "",
        "current_phase": "RESEARCH",
        "progress_ledger": update_progress_ledger(
            state.get("progress_ledger"),
            last_team_called="Human Review",
            next_action_rationale="Human review accepted plan; proceed to Research Team.",
        ),
    }


async def node_deep_research(state: ResearchState):
    return await run_research_team(
        state,
        safe_ainvoke_fn=safe_ainvoke,
        mode_search_fn=_mode_search,
        fallback_task_contract_fn=_fallback_task_contract,
        log_trajectory_fn=log_trajectory,
        fetch_source_candidate_fn=fetch_source_candidate,
        tavily_extract_client_obj=tavily_extract_client,
        tavily_extract_credits_fn=tavily_extract_credits,
        tavily_map_client_obj=tavily_map_client,
        tavily_map_credits_fn=tavily_map_credits,
        tavily_crawl_client_obj=tavily_crawl_client,
        tavily_crawl_credits_fn=tavily_crawl_credits,
    )


async def node_writer(state: ResearchState):
    return await run_writer_team(state)


async def node_supervisor(state: ResearchState):
    return await run_supervisor(
        state,
        decide_supervisor_decision_fn=decide_supervisor_decision,
    )


def router_feedback(state: ResearchState):
    if state.get("final_report") == "User Terminated":
        return END
    if state.get("user_feedback"):
        return "planner"
    return "executor"


def router_conflict(state: ResearchState):
    if state.get("retrieval_failed"):
        return END
    if state.get("conflict_detected"):
        if state.get("conflict_count", 0) >= 2:
            return "writer"
        return "planner"
    return "writer"


def supervisor_router(state: ResearchState):
    phase = str(state.get("current_phase") or "").upper()
    if phase in {"DONE", "FAIL_HARD"}:
        return END
    return state.get("supervisor_next_node") or END


legacy_workflow = StateGraph(ResearchState)
legacy_workflow.add_node("planner", node_init_search)
legacy_workflow.add_node("human_review", node_human_feedback)
legacy_workflow.add_node("executor", node_deep_research)
legacy_workflow.add_node("writer", node_writer)
legacy_workflow.set_entry_point("planner")
legacy_workflow.add_edge("planner", "human_review")
legacy_workflow.add_conditional_edges("human_review", router_feedback, ["planner", "executor", END])
legacy_workflow.add_conditional_edges("executor", router_conflict, ["writer", "planner", END])
legacy_workflow.add_edge("writer", END)

workflow = StateGraph(ResearchState)
workflow.add_node("supervisor", node_supervisor)
workflow.add_node("planner", node_init_search)
workflow.add_node("human_review", node_human_feedback)
workflow.add_node("research_team", node_deep_research)
workflow.add_node("writer_team", node_writer)
workflow.set_entry_point("supervisor")
workflow.add_conditional_edges(
    "supervisor",
    supervisor_router,
    ["planner", "human_review", "research_team", "writer_team", END],
)
workflow.add_edge("planner", "supervisor")
workflow.add_edge("human_review", "supervisor")
workflow.add_edge("research_team", "supervisor")
workflow.add_edge("writer_team", "supervisor")

checkpointer = get_sqlite_checkpointer(CHECKPOINT_DB_PATH)
legacy_app = legacy_workflow.compile(checkpointer=checkpointer)
app = workflow.compile(checkpointer=checkpointer)
