import asyncio

from core.graph import node_supervisor, supervisor_router
from gateway import executor
from scripts import deepresearch_bench_scoring as drb_scoring


def test_graph_input_seeds_supervisor_team_state():
    payload = executor._graph_input("task-1", "demo query", "medium", "supervisor_team")  # type: ignore[attr-defined]
    assert payload["architecture_mode"] == "supervisor_team"
    assert payload["current_phase"] == "PLAN"
    assert payload["task_ledger"] == {}
    assert payload["progress_ledger"] == {}
    assert payload["bundle_ref"] == ""
    assert payload["draft_ref"] == ""


def test_supervisor_team_uses_higher_node_budget():
    assert executor._max_node_budget_for_architecture("supervisor_team") >= executor.MAX_TASK_NODE_COUNT  # type: ignore[attr-defined]
    assert executor._max_node_budget_for_architecture("legacy_workflow") == executor.MAX_TASK_NODE_COUNT  # type: ignore[attr-defined]


def test_terminal_node_state_allows_final_overtime_snapshot():
    assert executor._is_terminal_node_state({"current_phase": "DONE"}) is True  # type: ignore[attr-defined]
    assert executor._is_terminal_node_state({"current_phase": "FAIL_HARD"}) is True  # type: ignore[attr-defined]
    assert executor._is_terminal_node_state({"current_phase": "RESEARCH"}) is False  # type: ignore[attr-defined]


def test_supervisor_replan_routes_back_to_planner():
    state = {
        "query": "demo query",
        "architecture_mode": "supervisor_team",
        "task_ledger": {"current_plan": [{"task": "a"}]},
        "progress_ledger": {
            "consecutive_no_improvement_backfills": 2,
            "last_team_called": "Research Team",
        },
        "current_phase": "REPLAN",
        "team_route_trace": [],
    }
    result = asyncio.run(node_supervisor(state))  # type: ignore[arg-type]
    assert result["current_phase"] == "PLAN"
    assert result["supervisor_next_node"] == "planner"
    assert result["progress_ledger"]["consecutive_no_improvement_backfills"] == 0
    assert result["team_route_trace"][0]["step_id"] == 1


def test_supervisor_router_ends_on_terminal_phase():
    assert supervisor_router({"current_phase": "DONE"}) == "__end__"
    assert supervisor_router({"current_phase": "FAIL_HARD"}) == "__end__"
    assert supervisor_router({"current_phase": "RESEARCH", "supervisor_next_node": "research_team"}) == "research_team"


def test_supervisor_uses_llm_guided_decision_for_writer_phase(monkeypatch):
    import core.graph as graph

    async def fake_decision(**kwargs):
        return {
            "next_phase": "REPLAN",
            "reason": "Need authority-first recovery.",
            "decision_basis": "authority_gap",
            "replan_strategy": "authority_first",
        }

    monkeypatch.setattr(graph, "decide_supervisor_decision", fake_decision)
    result = asyncio.run(
        graph.node_supervisor(
            {
                "query": "demo query",
                "architecture_mode": "supervisor_team",
                "task_ledger": {"current_plan": [{"task": "a"}]},
                "progress_ledger": {"last_team_called": "Writer Team"},
                "research_team_result": {"verifier_decision": "insufficient_authority"},
                "writer_team_result": {"needs_research_backfill": True},
                "current_phase": "WRITE",
                "team_route_trace": [],
            }
        )
    )

    assert result["supervisor_next_node"] == "planner"
    assert result["current_phase"] == "PLAN"
    assert result["supervisor_decision"]["decision_basis"] == "authority_gap"


def test_scoring_summary_tracks_architecture_distribution(monkeypatch):
    monkeypatch.setattr(
        drb_scoring,
        "score_deepresearch_result",
        lambda item, judge_model=None, allow_local_judge=True, require_local_judge=False: {
            **item,
            "drb_report_score": 7.0,
            "fact_score": 6.5,
            "dimension_scores": {key: 7.0 for key in drb_scoring.DIMENSIONS},
            "dimension_reasons": {key: "ok" for key in drb_scoring.DIMENSIONS},
            "failure_tags": [],
            "llm_cost_rmb": 0.1,
            "external_cost_rmb_est": 0.1,
            "total_cost_rmb_est": 0.2,
            "elapsed_seconds": 10.0,
            "team_stall_count": item.get("team_stall_count", 0),
            "global_stall_count": item.get("global_stall_count", 0),
            "architecture_mode": item.get("architecture_mode", "supervisor_team"),
        },
    )
    payload = drb_scoring.summarize_deepresearch_results(
        {
            "stage": "pilot",
            "results": [
                {"status": "SUCCESS", "language": "zh", "topic": "A", "architecture_mode": "legacy_workflow", "team_stall_count": 1},
                {"status": "SUCCESS", "language": "en", "topic": "B", "architecture_mode": "supervisor_team", "global_stall_count": 2},
            ],
        }
    )
    assert payload["summary"]["architecture_distribution"] == {
        "legacy_workflow": 1,
        "supervisor_team": 1,
    }
    assert payload["summary"]["audit_averages"]["team_stall_count"] == 0.5
