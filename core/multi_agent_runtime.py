from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypedDict

from core.config import DEFAULT_ARCHITECTURE_MODE, RUNTIME_ARTIFACT_DIR


class SupervisorDecision(TypedDict):
    next_phase: str
    reason: str
    decision_basis: str
    replan_strategy: str


class RetrievalPlan(TypedDict, total=False):
    target_clauses: list[str]
    source_type_priority: list[str]
    query_intents: list[str]
    backfill_mode: str
    authority_requirement: str
    stop_after_slots: list[str]


class EvidenceDigest(TypedDict, total=False):
    slot_statuses: dict[str, dict[str, Any]]
    clause_statuses: dict[str, dict[str, Any]]
    open_gaps: list[dict[str, Any]]
    authority_summary: dict[str, Any]
    coverage_summary: dict[str, Any]
    supporting_evidence_refs: dict[str, list[dict[str, str]]]
    direct_answer_support_snapshot: dict[str, Any]


def normalize_architecture_mode(mode: str | None) -> str:
    value = str(mode or DEFAULT_ARCHITECTURE_MODE).strip().lower()
    if value not in {"legacy_workflow", "supervisor_team"}:
        return DEFAULT_ARCHITECTURE_MODE
    return value


def build_task_ledger(
    *,
    query: str,
    task_contract: dict[str, Any] | None,
    plan: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    contract = dict(task_contract or {})
    return {
        "goal": query,
        "must_answer_points": list(contract.get("must_answer_points") or []),
        "comparison_targets": list(contract.get("comparison_targets") or []),
        "required_constraints": list(contract.get("required_constraints") or []),
        "evidence_requirements": {
            "minimum_high_authority_sources": 2,
            "minimum_direct_answer_support_rate": 1.0,
        },
        "stop_conditions": {
            "must_answer_satisfied": True,
            "comparison_targets_covered": True,
            "required_constraints_satisfied": True,
            "direct_answer_supported": True,
        },
        "current_plan": list(plan or []),
    }


def build_progress_ledger(existing: dict[str, Any] | None = None) -> dict[str, Any]:
    data = dict(existing or {})
    return {
        "completed_clauses": list(data.get("completed_clauses") or []),
        "open_gaps": list(data.get("open_gaps") or []),
        "coverage_status_by_clause": dict(data.get("coverage_status_by_clause") or {}),
        "verification_status": dict(data.get("verification_status") or {}),
        "last_team_called": str(data.get("last_team_called") or ""),
        "next_action_rationale": str(data.get("next_action_rationale") or ""),
        "team_stall_count": int(data.get("team_stall_count") or 0),
        "global_stall_count": int(data.get("global_stall_count") or 0),
        "consecutive_no_improvement_backfills": int(data.get("consecutive_no_improvement_backfills") or 0),
    }


def update_progress_ledger(
    existing: dict[str, Any] | None,
    *,
    last_team_called: str,
    next_action_rationale: str,
    clause_statuses: dict[str, Any] | None = None,
    open_gaps: list[dict[str, Any]] | None = None,
    team_stall_delta: int = 0,
    global_stall_delta: int = 0,
    reset_team_stall: bool = False,
    reset_global_stall: bool = False,
    no_improvement_increment: bool = False,
    reset_no_improvement: bool = False,
    verifier_decision: str | None = None,
) -> dict[str, Any]:
    progress = build_progress_ledger(existing)
    if reset_team_stall:
        progress["team_stall_count"] = 0
    progress["team_stall_count"] = int(progress.get("team_stall_count") or 0) + int(team_stall_delta)
    if reset_global_stall:
        progress["global_stall_count"] = 0
    progress["global_stall_count"] = int(progress.get("global_stall_count") or 0) + int(global_stall_delta)
    if reset_no_improvement:
        progress["consecutive_no_improvement_backfills"] = 0
    if no_improvement_increment:
        progress["consecutive_no_improvement_backfills"] = int(progress.get("consecutive_no_improvement_backfills") or 0) + 1
    progress["last_team_called"] = str(last_team_called or "")
    progress["next_action_rationale"] = str(next_action_rationale or "")
    if clause_statuses is not None:
        normalized_clause_statuses = dict(clause_statuses)
        progress["coverage_status_by_clause"] = normalized_clause_statuses
        progress["completed_clauses"] = [
            clause_id
            for clause_id, clause in normalized_clause_statuses.items()
            if str(clause.get("status") or "") in {"satisfied", "partially_satisfied"}
        ]
    if open_gaps is not None:
        progress["open_gaps"] = list(open_gaps)
    verification_status = dict(progress.get("verification_status") or {})
    if verifier_decision:
        verification_status["research_verifier_decision"] = str(verifier_decision)
    progress["verification_status"] = verification_status
    return progress


def default_supervisor_decision(next_phase: str, *, reason: str, decision_basis: str, replan_strategy: str = "none") -> SupervisorDecision:
    return {
        "next_phase": str(next_phase or "FAIL_HARD").upper(),
        "reason": str(reason or "").strip(),
        "decision_basis": str(decision_basis or "completion_ready").strip().lower(),
        "replan_strategy": str(replan_strategy or "none").strip().lower(),
    }


def default_retrieval_plan(
    *,
    target_clauses: list[str] | None = None,
    source_type_priority: list[str] | None = None,
    query_intents: list[str] | None = None,
    backfill_mode: str = "broad_recall",
    authority_requirement: str = "at_least_one_high_authority_per_slot",
    stop_after_slots: list[str] | None = None,
) -> RetrievalPlan:
    return {
        "target_clauses": list(target_clauses or []),
        "source_type_priority": list(source_type_priority or []),
        "query_intents": list(query_intents or []),
        "backfill_mode": str(backfill_mode or "broad_recall"),
        "authority_requirement": str(authority_requirement or "at_least_one_high_authority_per_slot"),
        "stop_after_slots": list(stop_after_slots or []),
    }


def normalize_supervisor_decision(payload: dict[str, Any] | None, *, fallback: SupervisorDecision) -> SupervisorDecision:
    data = dict(payload or {})
    next_phase = str(data.get("next_phase") or "").strip().upper()
    if next_phase not in {"RESEARCH", "WRITE", "REPLAN", "DEGRADED_WRITE", "FAIL_HARD", "DONE"}:
        return fallback
    decision_basis = str(data.get("decision_basis") or "").strip().lower()
    replan_strategy = str(data.get("replan_strategy") or "none").strip().lower() or "none"
    reason = str(data.get("reason") or "").strip()
    if not decision_basis:
        return fallback
    return {
        "next_phase": next_phase,
        "reason": reason or fallback["reason"],
        "decision_basis": decision_basis,
        "replan_strategy": replan_strategy,
    }


def normalize_retrieval_plan(payload: dict[str, Any] | None, *, fallback: RetrievalPlan) -> RetrievalPlan:
    data = dict(payload or {})
    raw_query_intents = [str(item).strip() for item in data.get("query_intents") or [] if str(item).strip()]
    if not raw_query_intents:
        return fallback
    plan = default_retrieval_plan(
        target_clauses=[str(item).strip() for item in data.get("target_clauses") or fallback.get("target_clauses") or [] if str(item).strip()],
        source_type_priority=[str(item).strip().lower() for item in data.get("source_type_priority") or fallback.get("source_type_priority") or [] if str(item).strip()],
        query_intents=raw_query_intents,
        backfill_mode=str(data.get("backfill_mode") or fallback.get("backfill_mode") or "broad_recall").strip().lower() or "broad_recall",
        authority_requirement=str(data.get("authority_requirement") or fallback.get("authority_requirement") or "at_least_one_high_authority_per_slot").strip().lower(),
        stop_after_slots=[str(item).strip() for item in data.get("stop_after_slots") or fallback.get("stop_after_slots") or [] if str(item).strip()],
    )
    return plan


def build_slot_statuses(evidence_slots: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    slots = dict(evidence_slots or {})
    slot_statuses: dict[str, dict[str, Any]] = {}
    for slot_id, slot in slots.items():
        high_authority = int(slot.get("high_authority_source_count") or 0)
        if slot.get("covered") and high_authority >= 1:
            status = "satisfied"
        elif slot.get("covered"):
            status = "partially_satisfied"
        else:
            status = "unsupported"
        gap_reason = ""
        if status == "partially_satisfied":
            gap_reason = "missing_high_authority_support"
        elif status == "unsupported":
            gap_reason = "missing_direct_evidence"
        slot_statuses[str(slot_id)] = {
            "status": status,
            "high_authority_source_count": high_authority,
            "supporting_evidence_ids": list(slot.get("source_urls") or []),
            "gap_reason": gap_reason,
        }
    return slot_statuses


def build_clause_statuses(
    *,
    task_contract: dict[str, Any] | None,
    slot_statuses: dict[str, dict[str, Any]] | None,
) -> dict[str, dict[str, Any]]:
    statuses = dict(slot_statuses or {})
    clause_statuses: dict[str, dict[str, Any]] = {}
    for point in list((task_contract or {}).get("must_answer_points") or []):
        clause_id = str(point.get("id") or len(clause_statuses) + 1)
        slot_status = statuses.get(clause_id, {})
        clause_statuses[clause_id] = {
            "section_id": str(point.get("section_id") or "global"),
            "question": str(point.get("question") or ""),
            "status": str(slot_status.get("status") or "unsupported"),
            "high_authority_source_count": int(slot_status.get("high_authority_source_count") or 0),
        }
    return clause_statuses


def compute_completion_policy(
    *,
    task_ledger: dict[str, Any] | None,
    research_team_result: dict[str, Any] | None,
    writer_team_result: dict[str, Any] | None,
) -> dict[str, Any]:
    ledger = dict(task_ledger or {})
    research = dict(research_team_result or {})
    writer = dict(writer_team_result or {})
    clause_statuses = dict(research.get("clause_statuses") or {})
    slot_statuses = dict(research.get("slot_statuses") or {})
    coverage_summary = dict(research.get("coverage_summary") or {})
    coverage_ratio = float(coverage_summary.get("task_clause_coverage_rate") or 0.0)
    high_confidence_slots = sum(
        1
        for slot in slot_statuses.values()
        if str(slot.get("status") or "") == "satisfied"
        and int(slot.get("high_authority_source_count") or 0) >= 1
    )
    comparison_targets = list(ledger.get("comparison_targets") or [])
    comparison_targets_covered = True
    if comparison_targets:
        lowered = json.dumps(clause_statuses, ensure_ascii=False).lower()
        comparison_targets_covered = all(str(target).strip().lower() in lowered for target in comparison_targets if str(target).strip())

    required_constraints = list(ledger.get("required_constraints") or [])
    constraint_report = dict(writer.get("constraint_satisfaction") or {})
    required_constraints_satisfied = True
    if required_constraints:
        covered = {str(item).strip().lower() for item in (constraint_report.get("covered_constraints") or []) if str(item).strip()}
        required_constraints_satisfied = all(str(item).strip().lower() in covered for item in required_constraints if str(item).strip())

    must_answer_satisfied = bool(clause_statuses) and all(
        str(item.get("status") or "") in {"satisfied", "partially_satisfied"} for item in clause_statuses.values()
    )
    direct_answer_supported = bool(
        dict(writer.get("citation_support_report") or {}).get("direct_answer_supported")
        or dict(writer.get("coverage_report") or {}).get("direct_answer_supported")
    )
    passed = all(
        [
            must_answer_satisfied,
            coverage_ratio >= 1.0,
            high_confidence_slots >= max(1, len(slot_statuses)),
            comparison_targets_covered,
            required_constraints_satisfied,
            direct_answer_supported,
        ]
    )
    return {
        "must_answer_satisfied": must_answer_satisfied,
        "coverage_ratio": round(coverage_ratio, 4),
        "high_confidence_slots": high_confidence_slots,
        "comparison_targets_covered": comparison_targets_covered,
        "required_constraints_satisfied": required_constraints_satisfied,
        "direct_answer_supported": direct_answer_supported,
        "passed": passed,
    }


def backfill_made_no_improvement(
    *,
    previous_coverage_summary: dict[str, Any] | None,
    current_coverage_summary: dict[str, Any] | None,
    previous_writer_team_result: dict[str, Any] | None = None,
    current_writer_team_result: dict[str, Any] | None = None,
) -> bool:
    previous = dict(previous_coverage_summary or {})
    current = dict(current_coverage_summary or {})
    previous_writer = dict(previous_writer_team_result or {})
    current_writer = dict(current_writer_team_result or {})
    previous_direct = bool(
        dict(previous_writer.get("citation_support_report") or {}).get("direct_answer_supported")
        or dict(previous_writer.get("coverage_report") or {}).get("direct_answer_supported")
        or previous.get("direct_answer_support_rate", 0.0) >= 1.0
    )
    current_direct = bool(
        dict(current_writer.get("citation_support_report") or {}).get("direct_answer_supported")
        or dict(current_writer.get("coverage_report") or {}).get("direct_answer_supported")
        or current.get("direct_answer_support_rate", 0.0) >= 1.0
    )
    previous_metrics = (
        float(previous.get("task_clause_coverage_rate") or 0.0),
        int(previous.get("high_authority_source_count") or 0),
        previous_direct,
    )
    current_metrics = (
        float(current.get("task_clause_coverage_rate") or 0.0),
        int(current.get("high_authority_source_count") or 0),
        current_direct,
    )
    return current_metrics <= previous_metrics


def append_team_route_trace(
    trace: list[dict[str, Any]] | None,
    *,
    phase: str,
    team: str,
    action: str,
    reason: str,
    progress_ledger: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    items = list(trace or [])
    items.append(
        {
            "step_id": len(items) + 1,
            "phase": phase,
            "team": team,
            "action": action,
            "reason": reason,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "stall_snapshot": {
                "team_stall_count": int((progress_ledger or {}).get("team_stall_count") or 0),
                "global_stall_count": int((progress_ledger or {}).get("global_stall_count") or 0),
                "consecutive_no_improvement_backfills": int((progress_ledger or {}).get("consecutive_no_improvement_backfills") or 0),
            },
        }
    )
    return items


def artifact_path(*, task_id: str, architecture_mode: str, artifact_name: str) -> str:
    mode = normalize_architecture_mode(architecture_mode)
    root = Path(RUNTIME_ARTIFACT_DIR) / mode / str(task_id)
    root.mkdir(parents=True, exist_ok=True)
    return str(root / artifact_name)


def save_json_artifact(*, task_id: str, architecture_mode: str, artifact_name: str, payload: Any) -> str:
    path = artifact_path(task_id=task_id, architecture_mode=architecture_mode, artifact_name=artifact_name)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    return path


def load_json_artifact(ref: str | None) -> Any:
    if not ref:
        return None
    path = Path(ref)
    if not path.exists():
        return None
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


__all__ = [
    "EvidenceDigest",
    "RetrievalPlan",
    "SupervisorDecision",
    "append_team_route_trace",
    "artifact_path",
    "backfill_made_no_improvement",
    "build_clause_statuses",
    "build_progress_ledger",
    "build_slot_statuses",
    "build_task_ledger",
    "compute_completion_policy",
    "default_retrieval_plan",
    "default_supervisor_decision",
    "load_json_artifact",
    "normalize_architecture_mode",
    "normalize_retrieval_plan",
    "normalize_supervisor_decision",
    "save_json_artifact",
    "update_progress_ledger",
]
