from core.multi_agent_runtime import (
    append_team_route_trace,
    build_clause_statuses,
    build_progress_ledger,
    build_slot_statuses,
    build_task_ledger,
    compute_completion_policy,
    default_retrieval_plan,
    default_supervisor_decision,
    normalize_retrieval_plan,
    normalize_supervisor_decision,
)


def test_build_slot_statuses_and_clause_statuses():
    evidence_slots = {
        "1": {
            "question": "What happened?",
            "section_id": "1",
            "high_authority_source_count": 1,
            "covered": True,
            "source_urls": ["https://example.com/a"],
        },
        "2": {
            "question": "Why?",
            "section_id": "2",
            "high_authority_source_count": 0,
            "covered": False,
            "source_urls": [],
        },
    }
    slot_statuses = build_slot_statuses(evidence_slots)
    clause_statuses = build_clause_statuses(
        task_contract={
            "must_answer_points": [
                {"id": "1", "section_id": "1", "question": "What happened?"},
                {"id": "2", "section_id": "2", "question": "Why?"},
            ]
        },
        slot_statuses=slot_statuses,
    )

    assert slot_statuses["1"]["status"] == "satisfied"
    assert slot_statuses["2"]["status"] == "unsupported"
    assert clause_statuses["1"]["status"] == "satisfied"
    assert clause_statuses["2"]["status"] == "unsupported"


def test_compute_completion_policy_uses_code_aggregated_fields():
    task_ledger = build_task_ledger(
        query="Compare A vs B in 2026",
        task_contract={
            "must_answer_points": [
                {"id": "1", "section_id": "1", "question": "Compare A vs B in 2026"},
            ],
            "comparison_targets": ["A", "B"],
            "required_constraints": ["2026"],
        },
        plan=[{"task": "compare", "section_id": "1"}],
    )
    research_team_result = {
        "slot_statuses": {
            "1": {
                "status": "satisfied",
                "high_authority_source_count": 1,
                "supporting_evidence_ids": ["https://example.com/a"],
                "gap_reason": "",
            }
        },
        "clause_statuses": {
            "1": {
                "section_id": "1",
                "question": "Compare A vs B in 2026",
                "status": "satisfied",
                "high_authority_source_count": 1,
            }
        },
        "coverage_summary": {"task_clause_coverage_rate": 1.0},
    }
    writer_team_result = {
        "coverage_report": {"direct_answer_supported": True},
        "citation_support_report": {"direct_answer_supported": True},
        "constraint_satisfaction": {"covered_constraints": ["2026"]},
    }

    policy = compute_completion_policy(
        task_ledger=task_ledger,
        research_team_result=research_team_result,
        writer_team_result=writer_team_result,
    )

    assert policy["must_answer_satisfied"] is True
    assert policy["comparison_targets_covered"] is True
    assert policy["required_constraints_satisfied"] is True
    assert policy["direct_answer_supported"] is True
    assert policy["passed"] is True


def test_append_team_route_trace_assigns_monotonic_step_ids():
    progress = build_progress_ledger({"team_stall_count": 1, "global_stall_count": 2})
    trace = append_team_route_trace(
        [],
        phase="RESEARCH",
        team="Research Supervisor",
        action="route:research_team",
        reason="continue research",
        progress_ledger=progress,
    )
    trace = append_team_route_trace(
        trace,
        phase="WRITE",
        team="Research Supervisor",
        action="route:writer_team",
        reason="coverage satisfied",
        progress_ledger=progress,
    )

    assert [item["step_id"] for item in trace] == [1, 2]
    assert trace[0]["stall_snapshot"]["global_stall_count"] == 2


def test_normalize_supervisor_decision_rejects_invalid_phase():
    fallback = default_supervisor_decision(
        "RESEARCH",
        reason="fallback",
        decision_basis="coverage_gap",
    )
    decision = normalize_supervisor_decision(
        {"next_phase": "PLAN", "reason": "bad", "decision_basis": "coverage_gap", "replan_strategy": "none"},
        fallback=fallback,
    )

    assert decision == fallback


def test_normalize_retrieval_plan_requires_query_intents():
    fallback = default_retrieval_plan(
        target_clauses=["1"],
        source_type_priority=["official"],
        query_intents=["baseline query"],
    )
    plan = normalize_retrieval_plan(
        {"target_clauses": ["2"], "source_type_priority": ["academic"], "query_intents": []},
        fallback=fallback,
    )

    assert plan == fallback
