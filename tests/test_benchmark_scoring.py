from core.costs import enrich_cost_fields, usd_to_rmb
from fastapi.testclient import TestClient
import gateway.api as api_module
from scripts import benchmark_scoring


def test_usd_to_rmb_uses_fixed_rate():
    assert usd_to_rmb(0.1) == 0.72


def test_enrich_cost_fields_adds_rmb_and_total():
    payload = enrich_cost_fields(
        {
            "llm_cost_rmb": 0.5,
            "external_cost_usd_est": 0.1,
            "serper_cost_usd_est": 0.02,
            "tavily_cost_usd_est": 0.08,
        }
    )
    assert payload["external_cost_rmb_est"] == 0.72
    assert payload["serper_cost_rmb_est"] == 0.144
    assert payload["tavily_cost_rmb_est"] == 0.576
    assert payload["total_cost_rmb_est"] == 1.22


def test_annotate_results_falls_back_when_local_judge_is_unavailable(monkeypatch):
    monkeypatch.setattr(benchmark_scoring, "local_judge_scores", lambda report_text, model_name=None: None)
    sample = [
        {
            "research_mode": "medium",
            "query": "sample query",
            "status": "SUCCESS",
            "report": "\n".join(
                [
                    "# Title",
                    "## Introduction",
                    "A cited statement [HASH:abc123] with support.",
                    "## Analysis",
                    "Another cited statement [HASH:def456] with support.",
                    "## Conclusion",
                    "Final takeaways.",
                    "## References",
                    "https://example.com/source",
                ]
            ),
            "llm_cost_rmb": 0.1,
            "external_cost_usd_est": 0.05,
            "serper_cost_usd_est": 0.01,
            "tavily_cost_usd_est": 0.04,
            "elapsed_seconds": 12.3,
        }
    ]
    scored = benchmark_scoring.annotate_results(sample)
    assert len(scored) == 1
    item = scored[0]
    assert item["judge_mode"] == "heuristic_fallback"
    assert 1.0 <= item["fact_score"] <= 10.0
    assert 1.0 <= item["race_score"] <= 10.0
    assert 1.0 <= item["quality_score"] <= 10.0
    assert 0.0 <= item["cost_efficiency_score"] <= 10.0
    assert 0.0 <= item["overall_score"] <= 10.0
    assert item["total_cost_rmb_est"] == 0.46


def test_summarize_payload_builds_mode_summary(monkeypatch):
    monkeypatch.setattr(benchmark_scoring, "local_judge_scores", lambda report_text, model_name=None: None)
    payload = {
        "benchmark_max_task_rmb": 0.6,
        "benchmark_max_total_rmb": 3.0,
        "results": [
            {
                "research_mode": "low",
                "query": "q1",
                "status": "SUCCESS",
                "report": "# Title\n## Introduction\n[HASH:a]\n## Conclusion\nhttps://a.com",
                "llm_cost_rmb": 0.05,
                "external_cost_usd_est": 0.01,
                "serper_cost_usd_est": 0.01,
                "tavily_cost_usd_est": 0.0,
                "elapsed_seconds": 20,
            },
            {
                "research_mode": "medium",
                "query": "q2",
                "status": "SUCCESS",
                "report": "# Title\n## Introduction\n[HASH:a][HASH:b]\n## Analysis\n## Conclusion\n## References\nhttps://b.com",
                "llm_cost_rmb": 0.08,
                "external_cost_usd_est": 0.02,
                "serper_cost_usd_est": 0.01,
                "tavily_cost_usd_est": 0.01,
                "elapsed_seconds": 15,
            },
            {
                "research_mode": "high",
                "query": "q3",
                "status": "SUCCESS",
                "report": "# Title\n## Introduction\n[HASH:a][HASH:b][HASH:c]\n## Analysis\n## Cost\n## Conclusion\n## References\nhttps://c.com",
                "llm_cost_rmb": 0.12,
                "external_cost_usd_est": 0.08,
                "serper_cost_usd_est": 0.0,
                "tavily_cost_usd_est": 0.08,
                "elapsed_seconds": 30,
            },
        ],
    }
    scored_payload = benchmark_scoring.summarize_payload(payload)
    assert scored_payload["usd_to_rmb_rate"] == 7.2
    assert scored_payload["actual_total_cost_rmb_est"] > scored_payload["actual_total_llm_cost_rmb"]
    summary = scored_payload["mode_summary"]
    assert summary["recommended_default_mode"] in {"low", "medium", "high"}
    assert summary["highest_quality_mode"] in {"low", "medium", "high"}
    assert summary["best_value_mode"] in {"low", "medium", "high"}
    assert summary["slowest_mode"] == "high"


def test_recommended_default_mode_uses_quality_gate(monkeypatch):
    monkeypatch.setattr(benchmark_scoring, "local_judge_scores", lambda report_text, model_name=None: None)
    payload = {
        "results": [
            {
                "research_mode": "low",
                "query": "cheap but shallow",
                "status": "SUCCESS",
                "report": "# Title\n## Intro\nShort text [HASH:a]\n## End\nhttps://a.com",
                "llm_cost_rmb": 0.02,
                "external_cost_usd_est": 0.001,
                "elapsed_seconds": 20,
            },
            {
                "research_mode": "medium",
                "query": "balanced",
                "status": "SUCCESS",
                "report": "\n".join(
                    [
                        "# Title",
                        "## Introduction",
                        "Supported point [HASH:a] [HASH:b] [HASH:c]",
                        "## Analysis",
                        "Longer analysis paragraph with multiple supported claims and structure.",
                        "## Cost",
                        "Details with evidence https://example.com/source",
                        "## Conclusion",
                        "Final synthesis.",
                        "## References",
                        "https://example.com/ref",
                    ]
                ),
                "llm_cost_rmb": 0.08,
                "external_cost_usd_est": 0.02,
                "elapsed_seconds": 15,
            },
            {
                "research_mode": "high",
                "query": "good but expensive",
                "status": "SUCCESS",
                "report": "\n".join(
                    [
                        "# Title",
                        "## Introduction",
                        "Supported point [HASH:a] [HASH:b] [HASH:c]",
                        "## Analysis",
                        "Longer analysis paragraph with multiple supported claims and structure.",
                        "## Cost",
                        "Details with evidence https://example.com/source",
                        "## Conclusion",
                        "Final synthesis.",
                        "## References",
                        "https://example.com/ref",
                    ]
                ),
                "llm_cost_rmb": 0.12,
                "external_cost_usd_est": 0.20,
                "elapsed_seconds": 40,
            },
        ]
    }
    summary = benchmark_scoring.summarize_payload(payload)["mode_summary"]
    assert summary["recommended_default_mode"] == "medium"


def test_research_status_returns_rmb_fields(monkeypatch):
    monkeypatch.setattr(
        api_module,
        "get_task",
        lambda task_id: {
            "task_id": task_id,
            "status": "SUCCESS",
            "detail": "done",
            "research_mode": "medium",
            "llm_cost_rmb": 0.5,
            "external_cost_usd_est": 0.1,
            "serper_queries": 2,
            "serper_cost_usd_est": 0.02,
            "tavily_credits_est": 4.0,
            "tavily_cost_usd_est": 0.08,
            "elapsed_seconds": 12.0,
            "attempt_count": 2,
            "resume_count": 1,
            "resumed_from_checkpoint": 1,
            "last_checkpoint_id": "cp-123",
            "last_checkpoint_node": "executor",
            "interruption_state": "resuming",
            "created_at": 100,
            "started_at": 110,
            "completed_at": 122,
            "report": "hello",
            "last_error": None,
        },
    )
    client = TestClient(api_module.app)
    response = client.get("/research/test-task")
    assert response.status_code == 200
    payload = response.json()
    assert payload["external_cost_rmb_est"] == 0.72
    assert payload["serper_cost_rmb_est"] == 0.144
    assert payload["tavily_cost_rmb_est"] == 0.576
    assert payload["total_cost_rmb_est"] == 1.22
    assert payload["attempt_count"] == 2
    assert payload["resume_count"] == 1
    assert payload["resumed_from_checkpoint"] is True
    assert payload["last_checkpoint_id"] == "cp-123"
