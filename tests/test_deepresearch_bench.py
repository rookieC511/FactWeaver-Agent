from scripts import deepresearch_bench_scoring as drb_scoring
from scripts import public_benchmark_deepresearch_bench as drb_runner
import pytest
from scripts.public_benchmark_deepresearch_bench import (
    align_deepresearch_bench_rows,
    run_deepresearch_benchmark,
    stratified_deepresearch_sample,
)


def test_align_deepresearch_bench_rows_requires_matching_id_and_prompt():
    prompt_rows = [{"id": 1, "topic": "Finance", "language": "zh", "prompt": "same prompt"}]
    reference_rows = [{"id": 1, "prompt": "same prompt", "article": "reference"}]
    criteria_rows = [{"id": 1, "prompt": "same prompt", "dimension_weight": {}, "criterions": {}}]
    aligned = align_deepresearch_bench_rows(prompt_rows, reference_rows, criteria_rows)
    assert aligned[0]["id"] == 1
    assert aligned[0]["reference_article"] == "reference"


def test_stratified_deepresearch_sample_balances_languages_and_topics():
    rows = [
        {"id": 1, "language": "zh", "topic": "Finance", "prompt": "a"},
        {"id": 2, "language": "zh", "topic": "Tech", "prompt": "b"},
        {"id": 3, "language": "zh", "topic": "Health", "prompt": "c"},
        {"id": 4, "language": "en", "topic": "Finance", "prompt": "d"},
        {"id": 5, "language": "en", "topic": "Art", "prompt": "e"},
        {"id": 6, "language": "en", "topic": "Law", "prompt": "f"},
        {"id": 7, "language": "zh", "topic": "Law", "prompt": "g"},
        {"id": 8, "language": "en", "topic": "Tech", "prompt": "h"},
    ]
    sample = stratified_deepresearch_sample(rows, sample_size=6, seed=7)
    assert len(sample) == 6
    assert sum(1 for row in sample if row["language"] == "zh") == 3
    assert sum(1 for row in sample if row["language"] == "en") == 3
    assert len({row["topic"] for row in sample}) >= 4


def test_score_deepresearch_result_uses_weighted_dimension_scores(monkeypatch):
    monkeypatch.setattr(
        drb_scoring,
        "local_deepresearch_judge_scores",
        lambda item, model_name=None: {
            "dimension_scores": {
                "comprehensiveness": 8.0,
                "insight": 7.0,
                "instruction_following": 6.0,
                "readability": 5.0,
            },
            "dimension_reasons": {key: "ok" for key in drb_scoring.DIMENSIONS},
            "judge_mode": "local_ollama:test",
        },
    )
    monkeypatch.setattr(
        drb_scoring,
        "score_result",
        lambda item, judge_model=None, allow_local_judge=True: {
            **item,
            "fact_score": 6.4,
            "race_score": 6.1,
            "quality_score": 6.265,
            "judge_mode": "heuristic_fallback",
        },
    )
    scored = drb_scoring.score_deepresearch_result(
        {
            "prompt": "demo",
            "report": "# Intro\ntext [HASH:abc]\n## Conclusion\ntext",
            "dimension_weight": {
                "comprehensiveness": 0.3,
                "insight": 0.4,
                "instruction_following": 0.2,
                "readability": 0.1,
            },
            "criterions": {},
        }
    )
    assert scored["drb_report_score"] == 6.9
    assert scored["fact_score"] == 6.4
    assert scored["dimension_scores"]["insight"] == 7.0


def test_classify_failure_tags_marks_blocked_and_degraded():
    tags = drb_scoring.classify_failure_tags(
        {
            "report": "\n".join(
                [
                    "# Report",
                    "HTTP 403 blocked by site",
                    "无法确定关键事实",
                    "## 资料缺口",
                    "- missing source",
                    "## 降级说明",
                    "- fallback used",
                ]
            )
        },
        {
            "comprehensiveness": 5.0,
            "insight": 5.0,
            "instruction_following": 5.5,
            "readability": 5.5,
        },
        fact_score=5.2,
    )
    assert "blocked_source" in tags
    assert "degraded_to_unknown" in tags
    assert "unsupported_claim_risk" in tags


def test_summarize_deepresearch_results_applies_pilot_gate(monkeypatch):
    monkeypatch.setattr(
        drb_scoring,
        "score_deepresearch_result",
        lambda item, judge_model=None, allow_local_judge=True, require_local_judge=False: {
            **item,
            "drb_report_score": item["mock_drb"],
            "fact_score": item["mock_fact"],
            "dimension_scores": {
                "comprehensiveness": item["mock_drb"],
                "insight": item["mock_drb"],
                "instruction_following": item["mock_drb"],
                "readability": item["mock_drb"],
            },
            "dimension_reasons": {key: "ok" for key in drb_scoring.DIMENSIONS},
            "failure_tags": item.get("failure_tags", []),
            "llm_cost_rmb": 0.1,
            "external_cost_rmb_est": 0.2,
            "total_cost_rmb_est": 0.3,
            "elapsed_seconds": 10.0,
        },
    )
    payload = drb_scoring.summarize_deepresearch_results(
        {
            "stage": "pilot",
            "results": [
                {"status": "SUCCESS", "language": "zh", "topic": "A", "mock_drb": 6.8, "mock_fact": 6.0},
                {"status": "SUCCESS", "language": "zh", "topic": "B", "mock_drb": 6.7, "mock_fact": 6.1},
                {"status": "SUCCESS", "language": "en", "topic": "C", "mock_drb": 6.6, "mock_fact": 6.0},
                {"status": "SUCCESS", "language": "en", "topic": "D", "mock_drb": 6.5, "mock_fact": 5.9},
                {"status": "SUCCESS", "language": "zh", "topic": "E", "mock_drb": 6.9, "mock_fact": 6.2},
                {"status": "FAILED", "language": "en", "topic": "F", "mock_drb": 6.4, "mock_fact": 5.8, "failure_tags": ["degraded_to_unknown"]},
            ],
        }
    )
    assert payload["summary"]["success_rate"] == 0.8333
    assert payload["summary"]["gate"]["passed"] is True
    assert payload["summary"]["weakest_dimension"] == "comprehensiveness"


def test_extract_dimension_scores_accepts_nested_scores():
    scores = drb_scoring._extract_dimension_scores_from_payload(  # type: ignore[attr-defined]
        {
            "scores": {
                "comprehensiveness": 6.4,
                "insight": 6.1,
                "instruction_following": 6.8,
                "readability": 7.2,
            }
        }
    )
    assert scores == {
        "comprehensiveness": 6.4,
        "insight": 6.1,
        "instruction_following": 6.8,
        "readability": 7.2,
    }


def test_extract_dimension_scores_rejects_missing_dimensions():
    scores = drb_scoring._extract_dimension_scores_from_payload(  # type: ignore[attr-defined]
        {"comprehensiveness": 7.0, "insight": 6.0}
    )
    assert scores is None


def test_public_drb_runner_requires_ready_local_judge(monkeypatch):
    monkeypatch.setattr(
        drb_runner,
        "judge_preflight",
        lambda model_name=None: {
            "judge_health": "unavailable",
            "judge_mode": "local_ollama:qwen3:8b",
            "scoring_reliability": "heuristic_only",
            "reason": "ollama_down",
        },
    )
    with pytest.raises(RuntimeError, match="requires a healthy local judge"):
        run_deepresearch_benchmark(
            stage="pilot",
            sample_size=1,
            research_mode="medium",
            seed=7,
            judge_model="qwen3:8b",
            max_allin_rmb=1.0,
            max_task_duration_seconds=30,
            prompt_dataset="unused",
            prompt_split="test",
            reference_dataset="unused",
            reference_split="test",
            criteria_dataset="unused",
            criteria_split="test",
        )


def test_score_deepresearch_result_can_require_local_judge(monkeypatch):
    monkeypatch.setattr(
        drb_scoring,
        "score_result",
        lambda item, judge_model=None, allow_local_judge=True: {
            **item,
            "fact_score": 6.4,
            "race_score": 6.1,
            "quality_score": 6.265,
        },
    )
    monkeypatch.setattr(drb_scoring, "local_deepresearch_judge_scores", lambda item, model_name=None: None)
    with pytest.raises(RuntimeError, match="local_judge_scoring_failed"):
        drb_scoring.score_deepresearch_result(
            {
                "prompt": "demo",
                "report": "## Direct Answer / Core Conclusion\nanswer\n## Analysis\ntext",
                "dimension_weight": {
                    "comprehensiveness": 0.25,
                    "insight": 0.25,
                    "instruction_following": 0.25,
                    "readability": 0.25,
                },
            },
            require_local_judge=True,
        )
