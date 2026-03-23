import json
from pathlib import Path

from scripts import deepresearch_bench_scoring as drb_scoring
from scripts import public_benchmark_deepresearch_bench as drb_runner
import pytest
from scripts.public_benchmark_deepresearch_bench import (
    align_deepresearch_bench_rows,
    compare_deepresearch_runs,
    freeze_deepresearch_fixture_from_run,
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
            "strict_success": item["status"] == "SUCCESS" and item.get("current_phase") == "DONE",
            "degraded_completion": False,
        },
    )
    payload = drb_scoring.summarize_deepresearch_results(
        {
            "stage": "pilot",
            "results": [
                {"status": "SUCCESS", "current_phase": "DONE", "language": "zh", "topic": "A", "mock_drb": 6.8, "mock_fact": 6.0},
                {"status": "SUCCESS", "current_phase": "DONE", "language": "zh", "topic": "B", "mock_drb": 6.7, "mock_fact": 6.1},
                {"status": "SUCCESS", "current_phase": "DONE", "language": "en", "topic": "C", "mock_drb": 6.6, "mock_fact": 6.0},
                {"status": "SUCCESS", "current_phase": "DONE", "language": "en", "topic": "D", "mock_drb": 6.5, "mock_fact": 5.9},
                {"status": "SUCCESS", "current_phase": "DONE", "language": "zh", "topic": "E", "mock_drb": 6.9, "mock_fact": 6.2},
                {"status": "FAILED", "current_phase": "FAIL_HARD", "language": "en", "topic": "F", "mock_drb": 6.4, "mock_fact": 5.8, "failure_tags": ["degraded_to_unknown"]},
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


def test_summarize_deepresearch_results_ignores_missing_coverage_for_support_rate(monkeypatch):
    monkeypatch.setattr(
        drb_scoring,
        "score_deepresearch_result",
        lambda item, judge_model=None, allow_local_judge=True, require_local_judge=False: item,
    )
    payload = drb_scoring.summarize_deepresearch_results(
        {
            "stage": "pilot",
            "results": [
                {
                    "status": "SUCCESS",
                    "language": "en",
                    "topic": "Law",
                    "drb_report_score": 7.0,
                    "fact_score": 6.5,
                    "dimension_scores": {key: 7.0 for key in drb_scoring.DIMENSIONS},
                    "failure_tags": [],
                    "llm_cost_rmb": 0.1,
                    "external_cost_rmb_est": 0.2,
                    "total_cost_rmb_est": 0.3,
                    "elapsed_seconds": 10.0,
                    "current_phase": "DONE",
                    "strict_success": True,
                    "degraded_completion": False,
                    "task_clause_coverage_rate": 1.0,
                    "direct_answer_support_rate": 0.8,
                    "blocked_source_rate": 0.0,
                    "blocked_attempt_rate": 0.2,
                    "authority_source_rate": 0.4,
                    "weak_source_hit_rate": 0.2,
                },
                {
                    "status": "FAILED",
                    "language": "zh",
                    "topic": "Finance",
                    "drb_report_score": 6.0,
                    "fact_score": 6.0,
                    "dimension_scores": {key: 6.0 for key in drb_scoring.DIMENSIONS},
                    "failure_tags": ["retrieval_miss"],
                    "llm_cost_rmb": 0.1,
                    "external_cost_rmb_est": 0.2,
                    "total_cost_rmb_est": 0.3,
                    "elapsed_seconds": 10.0,
                    "current_phase": "FAIL_HARD",
                    "strict_success": False,
                    "degraded_completion": False,
                    "task_clause_coverage_rate": 0.5,
                    "direct_answer_support_rate": None,
                    "blocked_source_rate": None,
                    "blocked_attempt_rate": None,
                    "authority_source_rate": None,
                    "weak_source_hit_rate": None,
                },
            ],
        }
    )
    summary = payload["summary"]
    assert summary["coverage_averages"]["direct_answer_support_rate"] == 0.8
    assert summary["coverage_averages"]["blocked_source_rate"] == 0.0
    assert summary["coverage_metric_sample_size"] == 1
    assert summary["coverage_metric_missing_count"] == 1


def test_freeze_deepresearch_fixture_from_run_writes_stable_hash(tmp_path, monkeypatch):
    monkeypatch.setattr(drb_runner, "ROOT_DIR", tmp_path)
    monkeypatch.setattr(drb_runner, "FIXTURE_ROOT", tmp_path / "reports" / "deepresearch_bench" / "fixtures")
    run_dir = tmp_path / "reports" / "deepresearch_bench" / "base-run"
    run_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "results": [
            {
                "id": 37,
                "topic": "Art",
                "language": "zh",
                "prompt": "p37",
                "reference_article": "r37",
                "dimension_weight": {"comprehensiveness": 0.25},
                "criterions": {"comprehensiveness": []},
            },
            {
                "id": 90,
                "topic": "Law",
                "language": "en",
                "prompt": "p90",
                "reference_article": "r90",
                "dimension_weight": {"comprehensiveness": 0.25},
                "criterions": {"comprehensiveness": []},
            },
            {
                "id": 22,
                "topic": "Education",
                "language": "en",
                "prompt": "p22",
                "reference_article": "r22",
                "dimension_weight": {"comprehensiveness": 0.25},
                "criterions": {"comprehensiveness": []},
            },
        ]
    }
    (run_dir / "raw_results.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    fixture = freeze_deepresearch_fixture_from_run(base_run_id="base-run", sample_ids=[37, 90, 22])
    assert fixture["sample_ids"] == [37, 90, 22]
    assert fixture["content_sha256"]
    assert Path(fixture["fixture_path"]).exists()


def test_resolve_smoke_profile_supports_quick_and_full():
    quick = drb_runner._resolve_smoke_profile("quick")  # type: ignore[attr-defined]
    full = drb_runner._resolve_smoke_profile("full")  # type: ignore[attr-defined]
    assert quick["name"] == "quick"
    assert quick["sample_ids"] == [90]
    assert quick["max_task_duration_seconds"] == 600
    assert full["name"] == "full"
    assert full["sample_ids"] == [37, 90, 22]
    assert full["max_task_duration_seconds"] == 900


def test_recover_benchmark_research_prefers_draft_artifact(monkeypatch):
    monkeypatch.setattr(
        drb_runner,
        "get_task",
        lambda task_id: {
            "status": "FAILED",
            "detail": "任务执行失败: Task timed out after 1000s (limit 900s)",
            "llm_cost_rmb": 0.23,
            "external_cost_usd_est": 0.09,
            "elapsed_seconds": 1000.0,
            "last_checkpoint_node": "supervisor",
            "architecture_mode": "supervisor_team",
        },
    )
    monkeypatch.setattr(
        drb_runner,
        "_artifact_ref",
        lambda **kwargs: Path(kwargs["artifact_name"]),
    )
    monkeypatch.setattr(
        drb_runner,
        "_load_json_if_exists",
        lambda path: {
            "evidence_bundle.json": {
                "task_contract": {"must_answer_points": [{"id": "1", "question": "q1"}]},
                "evidence_slots": {"1": {"question": "q1", "covered": True, "high_authority_source_count": 1}},
                "slot_statuses": {"1": {"status": "satisfied", "high_authority_source_count": 1}},
                "clause_statuses": {"1": {"status": "satisfied"}},
                "coverage_summary": {"task_clause_coverage_rate": 1.0, "direct_answer_support_rate": 1.0},
                "evidence_digest": {"open_gaps": []},
                "fetch_results": [{"url": "https://example.com"}],
                "source_candidates": [{"url": "https://example.com"}],
            },
            "draft_report.json": {
                "report": "## Direct Answer / Core Conclusion\nRecovered answer [HASH:abc]",
                "draft_audit": {"direct_answer_present": True},
                "writer_team_result": {
                    "draft_ref": "",
                    "needs_research_backfill": True,
                    "output_mode": "normal",
                },
            },
        }.get(path.name, {}),
    )
    recovered = drb_runner._recover_benchmark_research(
        task_id="demo-task",
        query="demo query",
        architecture_mode="supervisor_team",
    )
    assert recovered is not None
    assert recovered["report"].startswith("## Direct Answer / Core Conclusion")
    assert recovered["llm_cost_rmb"] == 0.23
    assert recovered["elapsed_seconds"] == 1000.0
    assert recovered["current_phase"] == "RESEARCH"
    assert recovered["draft_ref"].endswith("draft_report.json")
    assert recovered["team_route_trace"]


def test_recover_benchmark_research_builds_provisional_report_from_bundle(monkeypatch):
    monkeypatch.setattr(
        drb_runner,
        "get_task",
        lambda task_id: {
            "status": "FAILED",
            "detail": "任务执行失败: Task timed out after 950s (limit 900s)",
            "llm_cost_rmb": 0.1,
            "external_cost_usd_est": 0.02,
            "elapsed_seconds": 950.0,
            "last_checkpoint_node": "research_team",
            "architecture_mode": "supervisor_team",
        },
    )
    monkeypatch.setattr(
        drb_runner,
        "_artifact_ref",
        lambda **kwargs: Path(kwargs["artifact_name"]),
    )
    monkeypatch.setattr(
        drb_runner,
        "_load_json_if_exists",
        lambda path: {
            "evidence_bundle.json": {
                "task_contract": {"must_answer_points": [{"id": "1", "question": "q1"}]},
                "evidence_slots": {"1": {"question": "q1", "covered": False, "high_authority_source_count": 0}},
                "slot_statuses": {"1": {"status": "unsupported", "high_authority_source_count": 0}},
                "clause_statuses": {"1": {"status": "unsupported"}},
                "coverage_summary": {"task_clause_coverage_rate": 0.0, "direct_answer_support_rate": 0.0},
                "evidence_digest": {"open_gaps": [{"slot_id": "1", "gap_reason": "missing"}]},
            }
        }.get(path.name, {}),
    )
    recovered = drb_runner._recover_benchmark_research(
        task_id="bundle-only-task",
        query="bundle only query",
        architecture_mode="supervisor_team",
    )
    assert recovered is not None
    assert "Current evidence is incomplete" in recovered["report"]
    assert recovered["current_phase"] == "RESEARCH"
    assert recovered["retrieval_failed"] is True
    assert recovered["bundle_ref"].endswith("evidence_bundle.json")


def test_run_deepresearch_benchmark_uses_fixture_and_architecture(monkeypatch, tmp_path):
    fixture_path = tmp_path / "fixture.json"
    fixture_payload = {
        "base_run_id": "base",
        "sample_ids": [37],
        "fixture_path": str(fixture_path),
        "source_path": "unused",
        "dataset_revision": "local fixture frozen from current workspace at execution time",
        "samples": [
            {
                "id": 37,
                "topic": "Art",
                "language": "zh",
                "prompt": "fixture prompt",
                "reference_article": "fixture ref",
                "dimension_weight": {"comprehensiveness": 0.25},
                "criterions": {"comprehensiveness": []},
            }
        ],
    }
    fixture_payload["content_sha256"] = drb_runner._content_sha256(fixture_payload["samples"])  # type: ignore[attr-defined]
    fixture_path.write_text(json.dumps(fixture_payload, ensure_ascii=False, indent=2), encoding="utf-8")

    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        drb_runner,
        "judge_preflight",
        lambda model_name=None: {
            "judge_health": "ready",
            "judge_mode": "local_ollama:qwen3:8b",
            "scoring_reliability": "local_judge",
            "reason": "",
        },
    )

    def fake_run(task_id, query, **kwargs):
        calls.append({"task_id": task_id, "query": query, **kwargs})
        return {
            "task_id": task_id,
            "status": "SUCCESS",
            "report": "## Direct Answer / Core Conclusion\nok [HASH:x]",
            "architecture_mode": kwargs["architecture_mode"],
            "llm_cost_rmb": 0.1,
            "external_cost_usd_est": 0.0,
            "elapsed_seconds": 1.5,
            "progress_ledger": {"last_team_called": "Writer Team"},
            "writer_team_result": {"output_mode": "normal"},
            "current_phase": "DONE",
        }

    monkeypatch.setattr(drb_runner, "run_research_job_sync", fake_run)
    monkeypatch.setattr(
        drb_runner,
        "summarize_deepresearch_results",
        lambda payload, **_: {
            **payload,
            "summary": {
                "sample_size": 1,
                "success_rate": 1.0,
                "degraded_completion_rate": 0.0,
                "avg_drb_report_score": 7.0,
                "avg_fact_score": 7.0,
                "avg_total_cost_rmb_est": 0.1,
                "avg_elapsed_seconds": 1.5,
                "coverage_averages": {"direct_answer_support_rate": 1.0},
            },
        },
    )
    monkeypatch.setattr(
        drb_runner,
        "write_deepresearch_report",
        lambda payload, output_dir=None: (
            tmp_path / "results.json",
            tmp_path / "report.md",
        ),
    )

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
        architecture_mode="legacy_workflow",
        sample_fixture_path=fixture_path,
        disable_cache=True,
        resume_from_checkpoint=False,
    )
    assert calls[0]["query"] == "fixture prompt"
    assert calls[0]["architecture_mode"] == "legacy_workflow"
    assert calls[0]["disable_cache"] is True
    assert calls[0]["resume_from_checkpoint"] is False


def test_compare_deepresearch_runs_writes_per_sample_and_judge_summary(tmp_path):
    run_a = tmp_path / "a.json"
    run_b = tmp_path / "b.json"
    payload_a = {
        "judge_model": "qwen3:8b",
        "judge_mode": "local_ollama:qwen3:8b",
        "judge_health": "ready",
        "scoring_reliability": "local_judge",
        "summary": {
            "avg_drb_report_score": 6.5,
            "avg_fact_score": 7.0,
            "success_rate": 0.6667,
            "degraded_completion_rate": 0.0,
            "avg_total_cost_rmb_est": 0.2,
            "avg_elapsed_seconds": 10.0,
            "coverage_averages": {
                "direct_answer_support_rate": 0.7,
                "authority_source_rate": 0.3,
                "blocked_source_rate": 0.2,
            },
            "audit_averages": {},
        },
        "results": [
            {
                "id": 37,
                "strict_success": True,
                "drb_report_score": 6.5,
                "fact_score": 7.0,
                "total_cost_rmb_est": 0.2,
                "elapsed_seconds": 10.0,
                "direct_answer_support_rate": 0.7,
                "retrieval_failed": False,
                "status": "SUCCESS",
                "current_phase": "DONE",
            }
        ],
    }
    payload_b = {
        "judge_model": "qwen3:8b",
        "judge_mode": "local_ollama:qwen3:8b",
        "judge_health": "ready",
        "scoring_reliability": "local_judge",
        "summary": {
            "avg_drb_report_score": 6.8,
            "avg_fact_score": 7.2,
            "success_rate": 1.0,
            "degraded_completion_rate": 0.0,
            "avg_total_cost_rmb_est": 0.22,
            "avg_elapsed_seconds": 12.0,
            "coverage_averages": {
                "direct_answer_support_rate": 0.9,
                "authority_source_rate": 0.35,
                "blocked_source_rate": 0.1,
            },
            "audit_averages": {"team_stall_count": 1.0, "global_stall_count": 0.0},
        },
        "results": [
            {
                "id": 37,
                "strict_success": True,
                "drb_report_score": 6.8,
                "fact_score": 7.2,
                "total_cost_rmb_est": 0.22,
                "elapsed_seconds": 12.0,
                "direct_answer_support_rate": 0.9,
                "retrieval_failed": False,
                "status": "SUCCESS",
                "current_phase": "DONE",
                "team_route_trace": [{"step_id": 1}],
            }
        ],
    }
    run_a.write_text(json.dumps(payload_a, ensure_ascii=False, indent=2), encoding="utf-8")
    run_b.write_text(json.dumps(payload_b, ensure_ascii=False, indent=2), encoding="utf-8")
    comparison = compare_deepresearch_runs(run_a, run_b, output_dir=tmp_path / "cmp")
    assert comparison["judge_consistent"] is True
    md_text = Path(comparison["artifacts"]["comparison_md"]).read_text(encoding="utf-8")
    assert "Judge Model" in md_text
    assert "| sample_id |" in md_text
    assert "legacy_workflow.team_stall_count" in md_text


def test_run_smoke_architecture_ab_uses_quick_profile_defaults(monkeypatch, tmp_path):
    fixture_path = tmp_path / "fixture.json"
    fixture_payload = {
        "base_run_id": "base",
        "sample_ids": [90],
        "fixture_path": str(fixture_path),
        "source_path": "unused",
        "dataset_revision": "local fixture frozen from current workspace at execution time",
        "content_sha256": "abc",
        "samples": [
            {
                "id": 90,
                "topic": "Law",
                "language": "en",
                "prompt": "fixture prompt",
                "reference_article": "fixture ref",
                "dimension_weight": {"comprehensiveness": 0.25},
                "criterions": {"comprehensiveness": []},
            }
        ],
    }
    fixture_path.write_text(json.dumps(fixture_payload, ensure_ascii=False, indent=2), encoding="utf-8")

    captured: list[dict[str, object]] = []

    monkeypatch.setattr(
        drb_runner,
        "freeze_deepresearch_fixture_from_run",
        lambda base_run_id, sample_ids, output_path=None: {
            **fixture_payload,
            "sample_ids": list(sample_ids),
        },
    )

    def fake_run_benchmark(**kwargs):
        captured.append(kwargs)
        return {
            "artifacts": {"json_path": str(tmp_path / f"{kwargs['architecture_mode']}.json")},
            "summary": {},
        }

    monkeypatch.setattr(drb_runner, "run_deepresearch_benchmark", fake_run_benchmark)
    monkeypatch.setattr(
        drb_runner,
        "compare_deepresearch_runs",
        lambda a, b, output_dir=None: {"artifacts": {"comparison_md": str(tmp_path / "comparison.md")}},
    )

    payload = drb_runner.run_smoke_architecture_ab(
        base_run_id="base-run",
        sample_ids=None,
        research_mode="medium",
        judge_model="qwen3:8b",
        max_allin_rmb=1.0,
        max_task_duration_seconds=None,
        prompt_dataset="unused",
        prompt_split="test",
        reference_dataset="unused",
        reference_split="test",
        criteria_dataset="unused",
        criteria_split="test",
        allow_heuristic=False,
        smoke_profile="quick",
    )

    assert payload["smoke_profile"] == "quick"
    assert payload["sample_ids"] == [90]
    assert payload["max_task_duration_seconds"] == 600
    assert len(captured) == 2
    assert all(call["sample_ids"] == [90] for call in captured)
    assert all(call["max_task_duration_seconds"] == 600 for call in captured)


def test_summarize_deepresearch_results_tracks_degraded_completion(monkeypatch):
    monkeypatch.setattr(
        drb_scoring,
        "score_deepresearch_result",
        lambda item, judge_model=None, allow_local_judge=True, require_local_judge=False: item,
    )
    payload = drb_scoring.summarize_deepresearch_results(
        {
            "stage": "pilot",
            "results": [
                {
                    "status": "SUCCESS",
                    "current_phase": "DONE",
                    "strict_success": True,
                    "degraded_completion": False,
                    "language": "en",
                    "topic": "A",
                    "drb_report_score": 7.0,
                    "fact_score": 7.0,
                    "dimension_scores": {key: 7.0 for key in drb_scoring.DIMENSIONS},
                    "failure_tags": [],
                    "llm_cost_rmb": 0.1,
                    "external_cost_rmb_est": 0.2,
                    "total_cost_rmb_est": 0.3,
                    "elapsed_seconds": 10.0,
                },
                {
                    "status": "FAILED",
                    "current_phase": "DONE",
                    "strict_success": False,
                    "degraded_completion": True,
                    "language": "zh",
                    "topic": "B",
                    "drb_report_score": 6.0,
                    "fact_score": 6.0,
                    "dimension_scores": {key: 6.0 for key in drb_scoring.DIMENSIONS},
                    "failure_tags": [],
                    "llm_cost_rmb": 0.1,
                    "external_cost_rmb_est": 0.2,
                    "total_cost_rmb_est": 0.3,
                    "elapsed_seconds": 11.0,
                },
            ],
        }
    )
    assert payload["summary"]["success_rate"] == 0.5
    assert payload["summary"]["degraded_completion_rate"] == 0.5
