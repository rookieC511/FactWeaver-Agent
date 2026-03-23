from __future__ import annotations

from typing import Any

from core.config import DEFAULT_RESEARCH_MODE
from core.memory import get_current_km, get_current_session_id
from core.multi_agent_runtime import (
    build_progress_ledger,
    compute_completion_policy,
    load_json_artifact,
    save_json_artifact,
    update_progress_ledger,
)
from core.runtime_control import crash_process, should_interrupt_task
from core.writer_graph import _extract_sections, get_writer_thread_id, resolve_writer_context_mode, writer_app
from gateway.state_store import get_task, save_knowledge_snapshot, upsert_task


def build_writer_team_result(
    *,
    report: str,
    draft_ref: str,
    draft_audit: dict[str, Any] | None,
    report_verifier: dict[str, Any] | None,
    coverage_summary: dict[str, Any] | None,
    required_constraints: list[str] | None,
    unresolved_gaps_summary: list[str] | None,
    output_mode: str,
    direct_answer: str,
) -> dict[str, Any]:
    audit = dict(draft_audit or {})
    verifier = dict(report_verifier or {})
    coverage = dict(coverage_summary or {})
    required = [str(item).strip() for item in required_constraints or [] if str(item).strip()]
    report_lower = (report or "").lower()
    covered_constraints = [item for item in required if item.lower() in report_lower]
    direct_answer_supported = bool(audit.get("direct_answer_citation_backed")) and float(
        coverage.get("direct_answer_support_rate", 0.0) or 0.0
    ) >= 1.0
    return {
        "draft_ref": draft_ref,
        "direct_answer": direct_answer,
        "coverage_report": {
            "coverage_ratio": round(float(audit.get("task_clause_coverage_rate", coverage.get("task_clause_coverage_rate", 0.0)) or 0.0), 4),
            "must_answer_satisfied": float(audit.get("task_clause_coverage_rate", coverage.get("task_clause_coverage_rate", 0.0)) or 0.0) >= 1.0,
            "direct_answer_supported": direct_answer_supported,
            "answer_coverage": verifier.get("answer_coverage") or "unknown",
        },
        "citation_support_report": {
            "direct_answer_supported": direct_answer_supported,
            "direct_answer_citation_backed": bool(audit.get("direct_answer_citation_backed")),
            "citation_support": verifier.get("citation_support") or "unknown",
        },
        "constraint_satisfaction": {
            "covered_constraints": covered_constraints,
            "required_constraints_satisfied": len(covered_constraints) == len(required),
            "constraint_satisfaction": verifier.get("constraint_satisfaction") or "unknown",
        },
        "analysis_gap": {
            "missing_requirements": list(audit.get("missing_requirements") or []),
            "analysis_signal_count": int(audit.get("analysis_signal_count", 0)),
            "analysis_gap": verifier.get("analysis_gap") or "unknown",
        },
        "needs_research_backfill": bool(verifier.get("needs_research_backfill")),
        "output_mode": output_mode,
        "unresolved_gaps_summary": list(unresolved_gaps_summary or []),
        "report_verifier": verifier,
    }


def resolve_writer_team_outcome(
    *,
    completion: dict[str, Any],
    writer_team_result: dict[str, Any],
    progress_ledger: dict[str, Any] | None,
) -> tuple[str, str]:
    progress = dict(progress_ledger or {})
    output_mode = str(writer_team_result.get("output_mode") or "normal").lower()
    if output_mode == "degraded":
        return "DONE", "Writer Team produced a degraded but explicit output."
    needs_backfill = bool(writer_team_result.get("needs_research_backfill"))
    if completion.get("passed"):
        return "DONE", "Writer Team satisfied completion policy."
    if needs_backfill:
        if int(progress.get("consecutive_no_improvement_backfills") or 0) >= 2:
            return "REPLAN", "Writer Team requested research backfill after repeated no-improvement cycles."
        return "RESEARCH", "Writer Team requested structured research backfill."
    return "FAIL_HARD", "Writer Team could not satisfy completion policy or request a safe backfill."


def build_degradation_appendix(state: dict[str, Any]) -> str:
    lines: list[str] = []
    missing_sources = list(state.get("missing_sources") or [])
    degraded_items = list(state.get("degraded_items") or [])
    if not missing_sources and not degraded_items:
        return ""

    lines.append("\n\n## Missing Evidence And Degradation Notes")
    if missing_sources:
        lines.append("\n### Missing Sources")
        for item in missing_sources[:20]:
            task = item.get("task", "unknown task")
            query = item.get("query", "n/a")
            reason = item.get("reason", "unknown reason")
            url = item.get("url")
            suffix = f" | {url}" if url else ""
            lines.append(f"- {task} | query={query} | reason={reason}{suffix}")
    if degraded_items:
        lines.append("\n### Degraded Steps")
        for item in degraded_items[:20]:
            stage = item.get("stage", "unknown")
            reason = item.get("reason", "unknown reason")
            task = item.get("task", "unknown task")
            lines.append(f"- {task} | stage={stage} | reason={reason}")
    lines.append("\n### Confidence Boundary")
    lines.append(
        "- This report continued with bounded evidence. Some conclusions may be limited by missing sources or degraded retrieval paths."
    )
    return "\n".join(lines)


async def run_writer_team(state: dict[str, Any]) -> dict[str, Any]:
    architecture_mode = str(state.get("architecture_mode") or "legacy_workflow")
    task_id = str(state.get("task_id") or get_current_session_id() or "unknown-task")
    writer_thread_id = get_writer_thread_id(f"{architecture_mode}:{task_id}")
    writer_config = {"configurable": {"thread_id": writer_thread_id}}
    writer_context_mode = resolve_writer_context_mode()
    bundle_payload = load_json_artifact(state.get("bundle_ref")) or {}
    task_contract = dict(state.get("task_contract") or bundle_payload.get("task_contract") or {})
    evidence_slots = dict(state.get("evidence_slots") or bundle_payload.get("evidence_slots") or {})
    progress_ledger = build_progress_ledger(state.get("progress_ledger"))
    writer_inputs = {
        "query": state.get("query", ""),
        "outline": state.get("outline", []),
        "sections": {},
        "final_doc": "",
        "iteration": 0,
        "user_feedback": "SKIP_REVIEW",
        "task_id": task_id,
        "writer_context_mode": writer_context_mode,
        "task_contract": task_contract,
        "evidence_slots": evidence_slots,
        "draft_audit": {},
        "report_verifier": {},
        "audit_revision_count": 0,
        "required_analysis_modes": list(task_contract.get("required_analysis_modes") or []),
    }
    try:
        interrupt_before = ["editor"] if should_interrupt_task(task_id, "writer.before_editor") else None
        existing_writer_state = writer_app.get_state(writer_config)
        if existing_writer_state.values.get("final_doc") and not existing_writer_state.next:
            result = existing_writer_state.values
        else:
            writer_input = None if existing_writer_state.next else writer_inputs
            latest_result: dict[str, Any] = {}
            async for event in writer_app.astream(
                writer_input,
                config=writer_config,
                interrupt_before=interrupt_before,
            ):
                if "__interrupt__" in event:
                    writer_state = writer_app.get_state(writer_config)
                    checkpoint_config = dict(writer_state.config.get("configurable", {}))
                    save_knowledge_snapshot(
                        task_id,
                        thread_id=task_id,
                        checkpoint_id=checkpoint_config.get("checkpoint_id"),
                        checkpoint_ns=checkpoint_config.get("checkpoint_ns"),
                        checkpoint_node="writer.section_writer",
                        snapshot=get_current_km().snapshot(),
                    )
                    task = get_task(task_id) or {}
                    upsert_task(
                        task_id,
                        str(state.get("query") or ""),
                        "INTERRUPTED",
                        detail="Writer paused before editor for checkpoint recovery testing",
                        thread_id=task_id,
                        backend=task.get("backend") or "resume",
                        research_mode=str(state.get("research_mode") or DEFAULT_RESEARCH_MODE),
                        llm_cost_rmb=float(task.get("llm_cost_rmb") or 0.0),
                        external_cost_usd_est=float(task.get("external_cost_usd_est") or 0.0),
                        serper_queries=int(task.get("serper_queries") or 0),
                        serper_cost_usd_est=float(task.get("serper_cost_usd_est") or 0.0),
                        tavily_credits_est=float(task.get("tavily_credits_est") or 0.0),
                        tavily_cost_usd_est=float(task.get("tavily_cost_usd_est") or 0.0),
                        elapsed_seconds=float(task.get("elapsed_seconds") or 0.0),
                        attempt_count=int(task.get("attempt_count") or 1),
                        resume_count=int(task.get("resume_count") or 0),
                        resumed_from_checkpoint=bool(task.get("resumed_from_checkpoint") or 0),
                        started_at=task.get("started_at"),
                        last_checkpoint_id=checkpoint_config.get("checkpoint_id"),
                        last_checkpoint_ns=checkpoint_config.get("checkpoint_ns"),
                        last_checkpoint_node="writer.section_writer",
                        interruption_state="writer.before_editor",
                        architecture_mode=architecture_mode,
                    )
                    crash_process()
                latest_result.update(event)
            result = writer_app.get_state(writer_config).values if latest_result else existing_writer_state.values
        report = result.get("final_doc", "Writing Failed")
        draft_audit = dict(result.get("draft_audit") or {})
        report_verifier = dict(result.get("report_verifier") or {})
    except Exception as exc:
        report = f"Writer Subgraph Error: {exc}"
        draft_audit = {
            "passed": False,
            "missing_requirements": ["writer_subgraph_error"],
        }
        report_verifier = {
            "answer_coverage": "insufficient_evidence",
            "citation_support": "insufficient_evidence",
            "constraint_satisfaction": "insufficient_evidence",
            "analysis_gap": "insufficient_evidence",
            "should_revise": False,
            "needs_research_backfill": False,
            "should_degrade": True,
            "reason": "Writer subgraph failed before report verification.",
        }
    report += build_degradation_appendix(state)
    coverage_summary = dict(state.get("coverage_summary") or bundle_payload.get("coverage_summary") or {})
    required_constraints = [str(item).strip() for item in task_contract.get("required_constraints") or [] if str(item).strip()]
    output_mode = (
        "degraded"
        if str(state.get("current_phase") or "").upper() == "DEGRADED_WRITE" or bool(report_verifier.get("should_degrade"))
        else "normal"
    )
    unresolved_gaps_summary = [
        str(item.get("question") or item.get("gap_reason") or "").strip()
        for item in list(progress_ledger.get("open_gaps") or [])
        if str(item.get("question") or item.get("gap_reason") or "").strip()
    ]
    writer_team_result = build_writer_team_result(
        report=report,
        draft_ref="",
        draft_audit=draft_audit,
        report_verifier=report_verifier,
        coverage_summary=coverage_summary,
        required_constraints=required_constraints,
        unresolved_gaps_summary=unresolved_gaps_summary,
        output_mode=output_mode,
        direct_answer=_extract_sections(report).get("direct answer / core conclusion", ""),
    )
    draft_ref = save_json_artifact(
        task_id=task_id,
        architecture_mode=architecture_mode,
        artifact_name="draft_report.json",
        payload={
            "report": report,
            "draft_audit": draft_audit,
            "report_verifier": report_verifier,
            "writer_team_result": writer_team_result,
        },
    )
    writer_team_result["draft_ref"] = draft_ref
    completion = compute_completion_policy(
        task_ledger=dict(state.get("task_ledger") or {}),
        research_team_result=dict(state.get("research_team_result") or {}),
        writer_team_result=writer_team_result,
    )
    next_phase, next_reason = resolve_writer_team_outcome(
        completion=completion,
        writer_team_result=writer_team_result,
        progress_ledger=progress_ledger,
    )
    progress_ledger = update_progress_ledger(
        progress_ledger,
        last_team_called="Writer Team",
        next_action_rationale=next_reason,
    )
    return {
        "final_report": report,
        "draft_audit": draft_audit,
        "report_verifier": report_verifier,
        "writer_team_result": writer_team_result,
        "task_contract": task_contract,
        "evidence_slots": evidence_slots,
        "draft_ref": draft_ref,
        "progress_ledger": progress_ledger,
        "current_phase": next_phase,
        "retrieval_failed": bool(writer_team_result.get("needs_research_backfill")) or output_mode == "degraded",
        "cost_breakdown": state.get("cost_breakdown", {}),
        "retrieval_metrics": {
            **dict(state.get("retrieval_metrics") or {}),
            "task_clause_coverage_rate": float(draft_audit.get("task_clause_coverage_rate", dict(state.get("retrieval_metrics") or {}).get("task_clause_coverage_rate", 0.0))),
            "direct_answer_present": 1 if draft_audit.get("direct_answer_present") else 0,
            "direct_answer_citation_backed": 1 if draft_audit.get("direct_answer_citation_backed") else 0,
            "analysis_signal_count": int(draft_audit.get("analysis_signal_count", 0)),
            "comparison_present": 1 if draft_audit.get("comparison_present") else 0,
            "causal_present": 1 if draft_audit.get("causal_present") else 0,
            "risk_present": 1 if draft_audit.get("risk_present") else 0,
        },
    }


__all__ = [
    "build_degradation_appendix",
    "build_writer_team_result",
    "resolve_writer_team_outcome",
    "run_writer_team",
]
