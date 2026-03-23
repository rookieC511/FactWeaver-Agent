from __future__ import annotations

from typing import Any

from langchain_core.messages import HumanMessage

from core.models import llm_smart
from core.multi_agent_runtime import (
    SupervisorDecision,
    append_team_route_trace,
    build_progress_ledger,
    build_task_ledger,
    default_supervisor_decision,
    normalize_supervisor_decision,
    update_progress_ledger,
)
from core.tools import LLMFormatError, clean_json_output


async def _safe_json_decision(prompt: str) -> dict[str, Any] | None:
    for attempt in range(2):
        try:
            response = await llm_smart.ainvoke([HumanMessage(content=prompt)])
            parsed = clean_json_output(response.content, strict=True)
            if isinstance(parsed, dict):
                return parsed
            return None
        except (LLMFormatError, Exception):
            if attempt == 1:
                return None
    return None


def fallback_supervisor_decision(
    *,
    current_phase: str,
    progress_ledger: dict[str, Any] | None,
    research_team_result: dict[str, Any] | None,
    writer_team_result: dict[str, Any] | None,
) -> SupervisorDecision:
    progress = dict(progress_ledger or {})
    research = dict(research_team_result or {})
    writer = dict(writer_team_result or {})
    phase = str(current_phase or "").upper()

    if phase == "REPLAN":
        return default_supervisor_decision(
            "REPLAN",
            reason="Fallback supervisor decision triggered replanning.",
            decision_basis="stall_recovery",
            replan_strategy="targeted_backfill",
        )
    if phase == "DEGRADED_WRITE" or str(writer.get("output_mode") or "").lower() == "degraded":
        return default_supervisor_decision(
            "DEGRADED_WRITE",
            reason="Fallback supervisor decision preserved degraded writing mode.",
            decision_basis="writer_backfill",
            replan_strategy="degrade",
        )
    if phase == "WRITE":
        return default_supervisor_decision(
            "WRITE",
            reason="Fallback supervisor decision accepted write handoff.",
            decision_basis="completion_ready",
        )
    if phase == "FAIL_HARD":
        return default_supervisor_decision(
            "FAIL_HARD",
            reason="Fallback supervisor decision respected hard failure.",
            decision_basis="budget_guardrail",
        )
    if int(progress.get("global_stall_count") or 0) >= 2:
        return default_supervisor_decision(
            "REPLAN",
            reason="Fallback supervisor decision detected repeated global stalls.",
            decision_basis="stall_recovery",
            replan_strategy="targeted_backfill",
        )
    if str(research.get("verifier_decision") or "") == "insufficient_authority":
        return default_supervisor_decision(
            "REPLAN",
            reason="Fallback supervisor decision detected authority gap.",
            decision_basis="authority_gap",
            replan_strategy="authority_first",
        )
    if str(research.get("verifier_decision") or "") == "needs_backfill":
        return default_supervisor_decision(
            "RESEARCH",
            reason="Fallback supervisor decision requested more research coverage.",
            decision_basis="coverage_gap",
            replan_strategy="targeted_backfill",
        )
    return default_supervisor_decision(
        "RESEARCH",
        reason="Fallback supervisor decision continues research.",
        decision_basis="coverage_gap",
        replan_strategy="broad_recall",
    )


async def decide_supervisor_decision(
    *,
    query: str,
    current_phase: str,
    task_ledger: dict[str, Any] | None,
    progress_ledger: dict[str, Any] | None,
    research_team_result: dict[str, Any] | None,
    writer_team_result: dict[str, Any] | None,
) -> SupervisorDecision:
    fallback = fallback_supervisor_decision(
        current_phase=current_phase,
        progress_ledger=progress_ledger,
        research_team_result=research_team_result,
        writer_team_result=writer_team_result,
    )
    prompt = f"""
You are the Research Supervisor of a team-based multi-agent research system.

Decide the next phase using ONLY lightweight control information.
Return strict JSON:
{{
  "next_phase": "RESEARCH|WRITE|REPLAN|DEGRADED_WRITE|FAIL_HARD|DONE",
  "reason": "short explanation",
  "decision_basis": "coverage_gap|authority_gap|writer_backfill|stall_recovery|budget_guardrail|completion_ready",
  "replan_strategy": "narrow_scope|broaden_scope|authority_first|targeted_backfill|degrade|none"
}}

Rules:
- Use REPLAN when the current approach is stale or authority coverage is weak.
- Use DEGRADED_WRITE only when limited but explicit output is safer than more exploration.
- Use DONE only when writing is complete and no more research is needed.
- Prefer concise reasons.

Query:
{query}

Current phase:
{current_phase}

Task ledger:
{task_ledger or {}}

Progress ledger:
{progress_ledger or {}}

ResearchTeamResult:
{research_team_result or {}}

WriterTeamResult:
{writer_team_result or {}}
"""
    parsed = await _safe_json_decision(prompt)
    return normalize_supervisor_decision(parsed, fallback=fallback)


async def run_supervisor(
    state: dict[str, Any],
    *,
    decide_supervisor_decision_fn=decide_supervisor_decision,
) -> dict[str, Any]:
    task_ledger = dict(state.get("task_ledger") or {})
    if not task_ledger:
        task_ledger = build_task_ledger(
            query=str(state.get("query") or ""),
            task_contract=dict(state.get("task_contract") or {}),
            plan=list(state.get("plan") or []),
        )
    progress_ledger = build_progress_ledger(state.get("progress_ledger"))
    current_phase = str(state.get("current_phase") or "PLAN").upper()
    next_node = ""
    reason = ""
    supervisor_decision = dict(state.get("supervisor_decision") or {})
    if current_phase in {"", "PLAN"}:
        next_node = "planner"
        reason = "Initialize or refresh the research plan."
    elif current_phase == "HUMAN_REVIEW":
        next_node = "human_review"
        reason = "Collect human review feedback before research execution."
    elif current_phase == "REPLAN":
        supervisor_decision = default_supervisor_decision(
            "REPLAN",
            reason="Supervisor triggered replanning after stalls or repeated no-improvement backfills.",
            decision_basis="stall_recovery",
            replan_strategy="targeted_backfill",
        )
        task_ledger["current_plan"] = list(task_ledger.get("current_plan") or state.get("plan") or [])
        progress_ledger = update_progress_ledger(
            progress_ledger,
            last_team_called="Research Supervisor",
            next_action_rationale=supervisor_decision["reason"],
            reset_no_improvement=True,
        )
        next_node = "planner"
        current_phase = "PLAN"
        reason = supervisor_decision["reason"]
    elif current_phase in {"RESEARCH", "WRITE", "DEGRADED_WRITE"}:
        if current_phase == "RESEARCH" and not state.get("research_team_result") and not state.get("writer_team_result"):
            next_node = "research_team"
            reason = "Run Research Team to gather or validate evidence."
        else:
            supervisor_decision = await decide_supervisor_decision_fn(
                query=str(state.get("query") or ""),
                current_phase=current_phase,
                task_ledger=task_ledger,
                progress_ledger=progress_ledger,
                research_team_result=dict(state.get("research_team_result") or {}),
                writer_team_result=dict(state.get("writer_team_result") or {}),
            )
            decided_phase = str(supervisor_decision.get("next_phase") or current_phase).upper()
            reason = str(supervisor_decision.get("reason") or "").strip()
            if decided_phase == "REPLAN":
                task_ledger["current_plan"] = list(task_ledger.get("current_plan") or state.get("plan") or [])
                progress_ledger = update_progress_ledger(
                    progress_ledger,
                    last_team_called="Research Supervisor",
                    next_action_rationale=reason or "Supervisor requested replanning.",
                    reset_no_improvement=True,
                )
                next_node = "planner"
                current_phase = "PLAN"
            elif decided_phase in {"WRITE", "DEGRADED_WRITE"}:
                current_phase = decided_phase
                next_node = "writer_team"
                reason = reason or "Run Writer Team with the current evidence bundle."
            elif decided_phase == "RESEARCH":
                current_phase = "RESEARCH"
                next_node = "research_team"
                reason = reason or "Run Research Team to gather or validate evidence."
            else:
                current_phase = decided_phase
    trace = append_team_route_trace(
        state.get("team_route_trace"),
        phase=current_phase,
        team="Research Supervisor",
        action=f"route:{next_node or current_phase.lower()}",
        reason=reason or f"Supervisor observed terminal phase {current_phase}.",
        progress_ledger=progress_ledger,
    )
    return {
        "task_ledger": task_ledger,
        "progress_ledger": progress_ledger,
        "team_route_trace": trace,
        "current_phase": current_phase,
        "supervisor_next_node": next_node,
        "supervisor_decision": supervisor_decision,
    }


__all__ = [
    "SupervisorDecision",
    "decide_supervisor_decision",
    "fallback_supervisor_decision",
    "run_supervisor",
]
