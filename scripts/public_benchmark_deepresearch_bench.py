from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from datasets import load_dataset

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import gateway.executor as executor_module
from gateway.executor import run_research_job_sync
from gateway.state_store import get_task
from core.config import RUNTIME_ARTIFACT_DIR
from core.multi_agent_runtime import build_progress_ledger, build_task_ledger, normalize_architecture_mode
from scripts.deepresearch_bench_scoring import (
    compute_report_metrics,
    judge_preflight,
    summarize_deepresearch_results,
    write_deepresearch_report,
)


DEFAULT_PROMPT_DATASET = "rl-research/deep_research_bench_eval"
DEFAULT_PROMPT_SPLIT = "test"
DEFAULT_REFERENCE_DATASET = "lee64/deepresearch-bench-reference-clean"
DEFAULT_REFERENCE_SPLIT = "train"
DEFAULT_CRITERIA_DATASET = "lee64/deepresearch-bench-criteria"
DEFAULT_CRITERIA_SPLIT = "train"
DEFAULT_STAGE = "pilot"
DEFAULT_MODE = "medium"
FIXTURE_ROOT = ROOT_DIR / "reports" / "deepresearch_bench" / "fixtures"
DEFAULT_SMOKE_AB_BASE_RUN = "drb-pilot-20260314_220109"
DEFAULT_SMOKE_AB_SAMPLE_IDS = (37, 90, 22)
DEFAULT_QUICK_SMOKE_SAMPLE_IDS = (90,)
SMOKE_PROFILE_DEFAULT_TIMEOUTS = {
    "quick": 600,
    "full": 900,
}


def _persist_raw_payload(payload: dict[str, Any], *, output_dir: str | Path | None = None) -> Path:
    run_dir = Path(output_dir) if output_dir else ROOT_DIR / "reports" / "deepresearch_bench" / str(payload["run_id"])
    run_dir.mkdir(parents=True, exist_ok=True)
    raw_path = run_dir / "raw_results.json"
    raw_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return raw_path


def _normalize_language(value: str) -> str:
    lowered = str(value or "").strip().lower()
    return "zh" if lowered in {"zh", "cn", "chinese", "中文"} else "en"


def _stable_json_dumps(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _content_sha256(samples: list[dict[str, Any]]) -> str:
    return hashlib.sha256(_stable_json_dumps(samples).encode("utf-8")).hexdigest()


def _default_fixture_path(base_run_id: str, sample_ids: list[int]) -> Path:
    match = None
    try:
        import re

        match = re.search(r"(\d{8})", base_run_id)
    except Exception:
        match = None
    stamp = match.group(1) if match else base_run_id.replace("-", "_")
    sample_id_slug = "_".join(str(sample_id) for sample_id in sample_ids)
    return FIXTURE_ROOT / f"drb_smoke_ab_{stamp}_ids_{sample_id_slug}.json"


def _parse_sample_ids(value: str | None) -> list[int]:
    if not value:
        return []
    sample_ids: list[int] = []
    for chunk in str(value).split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        sample_ids.append(int(chunk))
    return sample_ids


def _resolve_smoke_profile(profile: str) -> dict[str, Any]:
    normalized = str(profile or "full").strip().lower()
    if normalized not in {"quick", "full"}:
        raise ValueError(f"Unsupported smoke profile: {profile}")
    if normalized == "quick":
        return {
            "name": "quick",
            "sample_ids": list(DEFAULT_QUICK_SMOKE_SAMPLE_IDS),
            "max_task_duration_seconds": SMOKE_PROFILE_DEFAULT_TIMEOUTS["quick"],
            "description": "Daily sanity check for graph/runtime changes; use a single frozen sample and a shorter timeout.",
        }
    return {
        "name": "full",
        "sample_ids": list(DEFAULT_SMOKE_AB_SAMPLE_IDS),
        "max_task_duration_seconds": SMOKE_PROFILE_DEFAULT_TIMEOUTS["full"],
        "description": "Architecture-level smoke A/B; use the full frozen 3-sample fixture before major merges or sign-off.",
    }


def _required_fixture_fields() -> tuple[str, ...]:
    return (
        "id",
        "topic",
        "language",
        "prompt",
        "reference_article",
        "dimension_weight",
        "criterions",
    )


def _row_to_fixture_sample(row: dict[str, Any]) -> dict[str, Any]:
    sample = {
        "id": int(row["id"]),
        "topic": row.get("topic"),
        "language": _normalize_language(str(row.get("language") or "")),
        "prompt": str(row.get("prompt") or "").strip(),
        "reference_article": str(row.get("reference_article") or row.get("article") or ""),
        "dimension_weight": dict(row.get("dimension_weight") or {}),
        "criterions": dict(row.get("criterions") or {}),
    }
    missing = [field for field in _required_fixture_fields() if sample.get(field) in (None, "")]
    missing = [field for field in missing if field not in {"topic", "language"}]
    if missing:
        raise ValueError(f"fixture row missing required fields for id={sample['id']}: {missing}")
    return sample


def _load_saved_run_payload(run_id: str) -> tuple[dict[str, Any], Path]:
    run_dir = ROOT_DIR / "reports" / "deepresearch_bench" / run_id
    raw_path = run_dir / "raw_results.json"
    results_path = run_dir / "results.json"
    source_path = raw_path if raw_path.exists() else results_path
    if not source_path.exists():
        raise FileNotFoundError(f"No raw_results.json or results.json found for run {run_id}")
    return json.loads(source_path.read_text(encoding="utf-8")), source_path


def _artifact_ref(*, task_id: str, architecture_mode: str, artifact_name: str) -> Path:
    return Path(RUNTIME_ARTIFACT_DIR) / normalize_architecture_mode(architecture_mode) / str(task_id) / artifact_name


def _load_json_if_exists(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _build_recovered_insufficiency_report(
    *,
    query: str,
    coverage_summary: dict[str, Any] | None,
    evidence_slots: dict[str, Any] | None,
) -> str:
    coverage = dict(coverage_summary or {})
    slots = dict(evidence_slots or {})
    lines = [
        f"# {query}",
        "",
        "## Direct Answer / Core Conclusion",
        "Current evidence is incomplete. Use the strongest recovered findings below as a provisional answer.",
        "",
        "## Key Evidence",
        "The task timed out before final report materialization, but the recovered evidence bundle contains these supported clauses:",
    ]
    if slots:
        for slot_id, slot in list(slots.items())[:8]:
            lines.append(
                f"- Clause {slot_id}: covered={slot.get('covered')} | "
                f"high_authority_sources={slot.get('high_authority_source_count', 0)} | "
                f"question={slot.get('question', '')}"
            )
    else:
        lines.append("- No structured evidence slots were recovered.")
    lines.extend(
        [
            "",
            "## Analysis",
            (
                "This report was reconstructed from persisted runtime artifacts after task failure. "
                f"task_clause_coverage_rate={coverage.get('task_clause_coverage_rate', 0.0)}, "
                f"direct_answer_support_rate={coverage.get('direct_answer_support_rate', 0.0)}, "
                f"authority_source_rate={coverage.get('authority_source_rate', 0.0)}."
            ),
            "",
            "## Uncertainty / Missing Evidence",
            "Treat this output as provisional until stronger authoritative evidence or a complete writer pass is available.",
        ]
    )
    return "\n".join(lines)


def _infer_recovered_phase(
    *,
    task_status: str,
    task_detail: str,
    last_checkpoint_node: str,
    bundle_payload: dict[str, Any],
    draft_payload: dict[str, Any],
) -> str:
    writer_team_result = dict(draft_payload.get("writer_team_result") or {})
    if str(task_status or "") == "SUCCESS":
        return "DONE"
    if writer_team_result:
        if str(writer_team_result.get("output_mode") or "").lower() == "degraded":
            return "DONE"
        if bool(writer_team_result.get("needs_research_backfill")):
            return "RESEARCH"
        return "WRITE"
    if bundle_payload:
        return "RESEARCH"
    lowered_detail = str(task_detail or "").lower()
    if "budget" in lowered_detail:
        return "FAIL_HARD"
    if "timed out" in lowered_detail or "timeout" in lowered_detail:
        if "writer" in str(last_checkpoint_node or "").lower():
            return "WRITE"
        if "research" in str(last_checkpoint_node or "").lower():
            return "RESEARCH"
        return "REPLAN"
    return "FAIL_HARD"


def _synthetic_team_route_trace(
    *,
    task_detail: str,
    current_phase: str,
    bundle_ref: str,
    draft_ref: str,
) -> list[dict[str, Any]]:
    trace: list[dict[str, Any]] = []

    def append(step_id: int, phase: str, team: str, action: str, reason: str) -> None:
        trace.append(
            {
                "step_id": step_id,
                "phase": phase,
                "team": team,
                "action": action,
                "reason": reason,
                "timestamp": "",
                "stall_snapshot": {
                    "team_stall_count": 0,
                    "global_stall_count": 0,
                    "consecutive_no_improvement_backfills": 0,
                },
            }
        )

    step = 1
    append(step, "PLAN", "Planner", "recovered_state", "Recovered benchmark result from persisted task state.")
    step += 1
    if bundle_ref:
        append(step, "RESEARCH", "Research Team", "artifact_recovered", "Recovered evidence bundle artifact.")
        step += 1
    if draft_ref:
        append(step, "WRITE", "Writer Team", "artifact_recovered", "Recovered draft report artifact.")
        step += 1
    append(step, current_phase or "FAIL_HARD", "Supervisor", "terminal_recovery", str(task_detail or "Recovered failed task output."))
    return trace


def _recover_benchmark_research(
    *,
    task_id: str,
    query: str,
    architecture_mode: str,
) -> dict[str, Any] | None:
    task = get_task(task_id) or {}
    mode = normalize_architecture_mode(architecture_mode or task.get("architecture_mode"))
    bundle_ref_path = _artifact_ref(task_id=task_id, architecture_mode=mode, artifact_name="evidence_bundle.json")
    draft_ref_path = _artifact_ref(task_id=task_id, architecture_mode=mode, artifact_name="draft_report.json")
    bundle_payload = _load_json_if_exists(bundle_ref_path)
    draft_payload = _load_json_if_exists(draft_ref_path)
    if not task and not bundle_payload and not draft_payload:
        return None

    report = str(task.get("report") or draft_payload.get("report") or "").strip()
    if not report and bundle_payload:
        report = _build_recovered_insufficiency_report(
            query=query,
            coverage_summary=bundle_payload.get("coverage_summary"),
            evidence_slots=bundle_payload.get("evidence_slots"),
        )

    task_contract = dict(bundle_payload.get("task_contract") or {})
    task_ledger = build_task_ledger(query=query, task_contract=task_contract, plan=[])
    current_phase = _infer_recovered_phase(
        task_status=str(task.get("status") or "FAILED"),
        task_detail=str(task.get("detail") or task.get("last_error") or ""),
        last_checkpoint_node=str(task.get("last_checkpoint_node") or ""),
        bundle_payload=bundle_payload,
        draft_payload=draft_payload,
    )
    progress_ledger = build_progress_ledger(
        {
            "last_team_called": {
                "RESEARCH": "Research Team",
                "WRITE": "Writer Team",
                "DONE": "Writer Team",
                "REPLAN": "Supervisor",
                "FAIL_HARD": "Supervisor",
            }.get(current_phase, "Supervisor")
        }
    )
    writer_team_result = dict(draft_payload.get("writer_team_result") or {})
    retrieval_failed = bool(writer_team_result.get("needs_research_backfill")) or str(
        writer_team_result.get("output_mode") or ""
    ).lower() == "degraded" or not bool(draft_payload)
    bundle_ref = str(bundle_ref_path) if bundle_payload else ""
    draft_ref = str(draft_ref_path) if draft_payload else ""
    return {
        "task_id": task_id,
        "status": str(task.get("status") or "FAILED"),
        "report": report,
        "architecture_mode": mode,
        "llm_cost_rmb": float(task.get("llm_cost_rmb") or 0.0),
        "external_cost_usd_est": float(task.get("external_cost_usd_est") or 0.0),
        "elapsed_seconds": float(task.get("elapsed_seconds") or 0.0),
        "task_contract": task_contract,
        "task_ledger": task_ledger,
        "progress_ledger": progress_ledger,
        "evidence_slots": dict(bundle_payload.get("evidence_slots") or {}),
        "draft_audit": dict(draft_payload.get("draft_audit") or {}),
        "research_team_result": {
            "status": "ok" if bundle_payload else "runtime_error",
            "slot_statuses": dict(bundle_payload.get("slot_statuses") or {}),
            "clause_statuses": dict(bundle_payload.get("clause_statuses") or {}),
            "coverage_summary": dict(bundle_payload.get("coverage_summary") or {}),
            "open_gaps": list(dict(bundle_payload.get("evidence_digest") or {}).get("open_gaps") or []),
            "bundle_ref": bundle_ref,
            "recommended_next_step": current_phase,
            "team_confidence": 0.0,
            "verifier_decision": "needs_backfill" if retrieval_failed else "ready_for_writer",
        },
        "writer_team_result": writer_team_result,
        "team_route_trace": _synthetic_team_route_trace(
            task_detail=str(task.get("detail") or task.get("last_error") or ""),
            current_phase=current_phase,
            bundle_ref=bundle_ref,
            draft_ref=draft_ref,
        ),
        "retrieval_metrics": {},
        "coverage_summary": dict(bundle_payload.get("coverage_summary") or {}),
        "fetch_results": list(bundle_payload.get("fetch_results") or []),
        "source_candidates": list(bundle_payload.get("source_candidates") or []),
        "retrieval_failed": retrieval_failed,
        "bundle_ref": bundle_ref,
        "draft_ref": draft_ref,
        "current_phase": current_phase,
        "detail": str(task.get("detail") or task.get("last_error") or ""),
    }


def _enrich_benchmark_research_result(
    *,
    research: dict[str, Any],
    task_id: str,
    query: str,
    architecture_mode: str,
) -> dict[str, Any]:
    recovered = _recover_benchmark_research(
        task_id=task_id,
        query=query,
        architecture_mode=architecture_mode,
    )
    if not recovered:
        return research
    merged = dict(recovered)
    for key, value in dict(research or {}).items():
        if value in (None, "", [], {}):
            continue
        merged[key] = value
    return merged


def freeze_deepresearch_fixture_from_run(
    *,
    base_run_id: str,
    sample_ids: list[int],
    output_path: str | Path | None = None,
) -> dict[str, Any]:
    payload, source_path = _load_saved_run_payload(base_run_id)
    rows = [dict(item) for item in payload.get("results", [])]
    by_id = {int(row["id"]): row for row in rows}
    missing_ids = [sample_id for sample_id in sample_ids if sample_id not in by_id]
    if missing_ids:
        raise ValueError(f"Sample ids not found in base run {base_run_id}: {missing_ids}")
    samples = [_row_to_fixture_sample(by_id[sample_id]) for sample_id in sample_ids]
    fixture_path = Path(output_path) if output_path else _default_fixture_path(base_run_id, sample_ids)
    fixture_path.parent.mkdir(parents=True, exist_ok=True)
    fixture = {
        "base_run_id": base_run_id,
        "sample_ids": list(sample_ids),
        "fixture_path": str(fixture_path),
        "source_path": str(source_path),
        "dataset_revision": "local fixture frozen from current workspace at execution time",
        "content_sha256": _content_sha256(samples),
        "samples": samples,
    }
    fixture_path.write_text(json.dumps(fixture, ensure_ascii=False, indent=2), encoding="utf-8")
    return fixture


def load_frozen_fixture(path: str | Path) -> dict[str, Any]:
    fixture_path = Path(path)
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    samples = [dict(item) for item in fixture.get("samples", [])]
    expected_hash = str(fixture.get("content_sha256") or "")
    actual_hash = _content_sha256(samples)
    if not expected_hash or expected_hash != actual_hash:
        raise ValueError(f"Fixture hash mismatch for {fixture_path}")
    fixture["fixture_path"] = str(fixture_path)
    fixture["samples"] = samples
    return fixture


def align_deepresearch_bench_rows(
    prompt_rows: list[dict[str, Any]],
    reference_rows: list[dict[str, Any]],
    criteria_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    reference_by_id = {int(row["id"]): dict(row) for row in reference_rows}
    criteria_by_id = {int(row["id"]): dict(row) for row in criteria_rows}
    aligned: list[dict[str, Any]] = []
    for prompt_row in prompt_rows:
        row_id = int(prompt_row["id"])
        reference_row = reference_by_id.get(row_id)
        criteria_row = criteria_by_id.get(row_id)
        if not reference_row or not criteria_row:
            raise ValueError(f"Missing reference or criteria row for id={row_id}")
        prompt_value = str(prompt_row.get("prompt") or "").strip()
        if prompt_value != str(reference_row.get("prompt") or "").strip():
            raise ValueError(f"Prompt mismatch for reference row id={row_id}")
        if prompt_value != str(criteria_row.get("prompt") or "").strip():
            raise ValueError(f"Prompt mismatch for criteria row id={row_id}")
        aligned.append(
            {
                "id": row_id,
                "topic": prompt_row.get("topic"),
                "language": _normalize_language(str(prompt_row.get("language") or "")),
                "prompt": prompt_value,
                "reference_article": reference_row.get("article") or "",
                "dimension_weight": dict(criteria_row.get("dimension_weight") or {}),
                "criterions": dict(criteria_row.get("criterions") or {}),
            }
        )
    return aligned


def _round_robin_by_topic(rows: list[dict[str, Any]], *, quota: int, rng: random.Random) -> list[dict[str, Any]]:
    topic_buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        topic_buckets[str(row.get("topic") or "unknown")].append(row)
    for bucket in topic_buckets.values():
        rng.shuffle(bucket)
    ordered_topics = sorted(topic_buckets)
    selected: list[dict[str, Any]] = []
    while ordered_topics and len(selected) < quota:
        next_topics: list[str] = []
        for topic in ordered_topics:
            bucket = topic_buckets[topic]
            if bucket and len(selected) < quota:
                selected.append(bucket.pop())
            if bucket:
                next_topics.append(topic)
        ordered_topics = next_topics
    return selected


def _pick_with_topic_preference(
    rows: list[dict[str, Any]],
    *,
    quota: int,
    used_topics: set[str],
    rng: random.Random,
) -> list[dict[str, Any]]:
    if quota <= 0:
        return []
    ordered = _round_robin_by_topic(rows, quota=len(rows), rng=rng)
    preferred = [row for row in ordered if str(row.get("topic") or "unknown") not in used_topics]
    fallback = [row for row in ordered if str(row.get("topic") or "unknown") in used_topics]
    selected = (preferred + fallback)[:quota]
    used_topics.update(str(row.get("topic") or "unknown") for row in selected)
    return selected


def stratified_deepresearch_sample(rows: list[dict[str, Any]], sample_size: int, seed: int) -> list[dict[str, Any]]:
    rng = random.Random(seed)
    language_buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        language_buckets[_normalize_language(str(row.get("language") or ""))].append(row)
    for bucket in language_buckets.values():
        rng.shuffle(bucket)

    zh_quota = min(len(language_buckets["zh"]), sample_size // 2)
    en_quota = min(len(language_buckets["en"]), sample_size // 2)
    used_topics: set[str] = set()
    selected = _pick_with_topic_preference(language_buckets["zh"], quota=zh_quota, used_topics=used_topics, rng=rng)
    selected.extend(
        _pick_with_topic_preference(language_buckets["en"], quota=en_quota, used_topics=used_topics, rng=rng)
    )

    used_ids = {int(row["id"]) for row in selected}
    remaining = [row for row in rows if int(row["id"]) not in used_ids]
    remaining = _pick_with_topic_preference(
        remaining,
        quota=max(0, sample_size - len(selected)),
        used_topics=used_topics,
        rng=rng,
    )
    selected.extend(remaining)
    return selected[:sample_size]


def load_deepresearch_bench_tasks(
    *,
    prompt_dataset: str = DEFAULT_PROMPT_DATASET,
    prompt_split: str = DEFAULT_PROMPT_SPLIT,
    reference_dataset: str = DEFAULT_REFERENCE_DATASET,
    reference_split: str = DEFAULT_REFERENCE_SPLIT,
    criteria_dataset: str = DEFAULT_CRITERIA_DATASET,
    criteria_split: str = DEFAULT_CRITERIA_SPLIT,
) -> list[dict[str, Any]]:
    prompt_rows = [dict(item) for item in load_dataset(prompt_dataset, split=prompt_split)]
    reference_rows = [dict(item) for item in load_dataset(reference_dataset, split=reference_split)]
    criteria_rows = [dict(item) for item in load_dataset(criteria_dataset, split=criteria_split)]
    return align_deepresearch_bench_rows(prompt_rows, reference_rows, criteria_rows)


def _default_sample_size(stage: str) -> int:
    if stage == "calibration":
        return 12
    if stage == "full":
        return 30
    return 6


def _default_budget(stage: str) -> float:
    if stage == "calibration":
        return 15.0
    if stage == "full":
        return 30.0
    return 8.0


def run_deepresearch_benchmark(
    *,
    stage: str,
    sample_size: int,
    research_mode: str,
    seed: int,
    judge_model: str | None,
    max_allin_rmb: float,
    max_task_duration_seconds: int,
    prompt_dataset: str,
    prompt_split: str,
    reference_dataset: str,
    reference_split: str,
    criteria_dataset: str,
    criteria_split: str,
    architecture_mode: str = "supervisor_team",
    sample_fixture_path: str | Path | None = None,
    sample_ids: list[int] | None = None,
    run_id: str | None = None,
    output_dir: str | Path | None = None,
    disable_cache: bool = True,
    resume_from_checkpoint: bool = False,
    allow_heuristic: bool = False,
) -> dict[str, Any]:
    preflight = judge_preflight(judge_model)
    if preflight["judge_health"] != "ready" and not allow_heuristic:
        raise RuntimeError(
            f"DeepResearch Bench pilot requires a healthy local judge. "
            f"judge_health={preflight['judge_health']} reason={preflight['reason']}"
        )
    original_duration = executor_module.MAX_TASK_DURATION_SECONDS
    executor_module.MAX_TASK_DURATION_SECONDS = max_task_duration_seconds
    run_id = run_id or f"drb-{stage}-{time.strftime('%Y%m%d_%H%M%S')}"
    results: list[dict[str, Any]] = []
    stopped_early = False
    input_fixture: dict[str, Any] | None = None
    requested_ids = list(sample_ids or [])
    if sample_fixture_path:
        input_fixture = load_frozen_fixture(sample_fixture_path)
        fixture_rows = [dict(item) for item in input_fixture.get("samples", [])]
        if requested_ids:
            fixture_by_id = {int(row["id"]): row for row in fixture_rows}
            missing_ids = [sample_id for sample_id in requested_ids if sample_id not in fixture_by_id]
            if missing_ids:
                raise ValueError(f"Sample ids not present in fixture: {missing_ids}")
            sample_rows = [fixture_by_id[sample_id] for sample_id in requested_ids]
        else:
            sample_rows = fixture_rows
    else:
        all_rows = load_deepresearch_bench_tasks(
            prompt_dataset=prompt_dataset,
            prompt_split=prompt_split,
            reference_dataset=reference_dataset,
            reference_split=reference_split,
            criteria_dataset=criteria_dataset,
            criteria_split=criteria_split,
        )
        if requested_ids:
            row_by_id = {int(row["id"]): row for row in all_rows}
            missing_ids = [sample_id for sample_id in requested_ids if sample_id not in row_by_id]
            if missing_ids:
                raise ValueError(f"Sample ids not found in dataset rows: {missing_ids}")
            sample_rows = [row_by_id[sample_id] for sample_id in requested_ids]
        else:
            sample_rows = stratified_deepresearch_sample(
                all_rows,
                sample_size=sample_size,
                seed=seed,
            )
    try:
        for row in sample_rows:
            projected_total = sum(float(item.get("total_cost_rmb_est") or 0.0) for item in results)
            if projected_total >= max_allin_rmb:
                stopped_early = True
                break
            task_id = f"{run_id}-{architecture_mode}-{int(row['id'])}"
            detail = ""
            try:
                research = run_research_job_sync(
                    task_id,
                    row["prompt"],
                    backend="drb_public_benchmark",
                    research_mode=research_mode,
                    architecture_mode=architecture_mode,
                    disable_cache=disable_cache,
                    resume_from_checkpoint=resume_from_checkpoint,
                )
            except Exception as exc:
                detail = repr(exc)
                research = _recover_benchmark_research(
                    task_id=task_id,
                    query=row["prompt"],
                    architecture_mode=architecture_mode,
                ) or {
                    "task_id": task_id,
                    "status": "FAILED",
                    "report": "",
                    "architecture_mode": architecture_mode,
                    "llm_cost_rmb": 0.0,
                    "external_cost_usd_est": 0.0,
                    "elapsed_seconds": 0.0,
                }
            research = _enrich_benchmark_research_result(
                research=research,
                task_id=task_id,
                query=row["prompt"],
                architecture_mode=architecture_mode,
            )
            report_text = str(research.get("report") or "")
            metrics = compute_report_metrics(report_text)
            results.append(
                {
                    **row,
                    "task_id": task_id,
                    "research_mode": research_mode,
                    "architecture_mode": research.get("architecture_mode", "supervisor_team"),
                    "status": research.get("status", "FAILED"),
                    "detail": detail,
                    "report": report_text,
                    "llm_cost_rmb": float(research.get("llm_cost_rmb") or 0.0),
                    "external_cost_usd_est": float(research.get("external_cost_usd_est") or 0.0),
                    "elapsed_seconds": float(research.get("elapsed_seconds") or 0.0),
                    "missing_sources": metrics["missing_sources"],
                    "degraded_items": metrics["degraded_items"],
                    "citation_count": metrics["citation_count"],
                    "hash_count": metrics["citation_count"],
                    "url_count": metrics["url_count"],
                    "task_contract": dict(research.get("task_contract") or {}),
                    "evidence_slots": dict(research.get("evidence_slots") or {}),
                    "draft_audit": dict(research.get("draft_audit") or {}),
                    "task_ledger": dict(research.get("task_ledger") or {}),
                    "progress_ledger": dict(research.get("progress_ledger") or {}),
                    "research_team_result": dict(research.get("research_team_result") or {}),
                    "writer_team_result": dict(research.get("writer_team_result") or {}),
                    "team_route_trace": list(research.get("team_route_trace") or []),
                    "retrieval_metrics": dict(research.get("retrieval_metrics") or {}),
                    "coverage_summary": dict(research.get("coverage_summary") or {}),
                    "fetch_results": list(research.get("fetch_results") or []),
                    "retrieval_failed": bool(research.get("retrieval_failed")),
                    "bundle_ref": str(research.get("bundle_ref") or ""),
                    "draft_ref": str(research.get("draft_ref") or ""),
                    "current_phase": str(research.get("current_phase") or ""),
                    "judge_mode": preflight["judge_mode"],
                    "judge_health": preflight["judge_health"],
                    "scoring_reliability": preflight["scoring_reliability"],
                }
            )
    finally:
        executor_module.MAX_TASK_DURATION_SECONDS = original_duration

    payload = {
        "run_id": run_id,
        "stage": stage,
        "requested_sample_size": sample_size,
        "sample_ids": [int(row["id"]) for row in sample_rows],
        "research_mode": research_mode,
        "architecture_mode": architecture_mode,
        "seed": seed,
        "judge_model": judge_model,
        "judge_mode": preflight["judge_mode"],
        "judge_health": preflight["judge_health"],
        "scoring_reliability": preflight["scoring_reliability"],
        "disable_cache": disable_cache,
        "resume_from_checkpoint": resume_from_checkpoint,
        "stopped_early": stopped_early,
        "max_allin_rmb": max_allin_rmb,
        "input_fixture": input_fixture,
        "results": results,
    }
    raw_path = _persist_raw_payload(payload, output_dir=output_dir)
    try:
        summarized = summarize_deepresearch_results(
            payload,
            judge_model=judge_model,
            allow_local_judge=preflight["judge_health"] == "ready",
            require_local_judge=preflight["judge_health"] == "ready" and not allow_heuristic,
        )
    except Exception as exc:
        raise RuntimeError(f"{exc} (raw results saved to {raw_path})") from exc
    json_path, md_path = write_deepresearch_report(summarized, output_dir=output_dir)
    summarized["artifacts"] = {
        "raw_path": str(raw_path),
        "json_path": str(json_path),
        "md_path": str(md_path),
    }
    return summarized


def compare_deepresearch_runs(
    results_a_path: str | Path,
    results_b_path: str | Path,
    *,
    output_dir: str | Path,
    label_a: str = "legacy_workflow",
    label_b: str = "supervisor_team",
) -> dict[str, Any]:
    payload_a = json.loads(Path(results_a_path).read_text(encoding="utf-8"))
    payload_b = json.loads(Path(results_b_path).read_text(encoding="utf-8"))
    summary_a = dict(payload_a.get("summary") or {})
    summary_b = dict(payload_b.get("summary") or {})
    judge_summary = {
        "judge_model": payload_a.get("judge_model"),
        "judge_mode": payload_a.get("judge_mode"),
        "judge_health": payload_a.get("judge_health"),
        "scoring_reliability": payload_a.get("scoring_reliability"),
    }
    judge_consistent = all(
        payload_a.get(field) == payload_b.get(field)
        for field in ("judge_model", "judge_mode", "judge_health", "scoring_reliability")
    )
    headline_metrics = (
        "avg_drb_report_score",
        "avg_fact_score",
        "success_rate",
        "direct_answer_support_rate",
        "avg_total_cost_rmb_est",
        "avg_elapsed_seconds",
    )
    headline: dict[str, Any] = {}
    for metric in headline_metrics:
        a_value = summary_a.get(metric)
        if a_value is None:
            a_value = (summary_a.get("coverage_averages") or {}).get(metric)
        b_value = summary_b.get(metric)
        if b_value is None:
            b_value = (summary_b.get("coverage_averages") or {}).get(metric)
        delta = None
        delta_pct = None
        if a_value is not None and b_value is not None:
            delta = round(float(b_value) - float(a_value), 4)
            if float(a_value) != 0.0:
                delta_pct = round((delta / float(a_value)) * 100.0, 2)
        headline[metric] = {
            "a_value": a_value,
            "b_value": b_value,
            "delta": delta,
            "delta_pct": delta_pct,
        }

    by_id_a = {int(item["id"]): item for item in payload_a.get("results", [])}
    by_id_b = {int(item["id"]): item for item in payload_b.get("results", [])}
    sample_ids = sorted(set(by_id_a) | set(by_id_b))
    per_sample: list[dict[str, Any]] = []
    for sample_id in sample_ids:
        item_a = by_id_a.get(sample_id, {})
        item_b = by_id_b.get(sample_id, {})
        per_sample.append(
            {
                "sample_id": sample_id,
                "a_success": bool(item_a.get("strict_success", str(item_a.get("status") or "") == "SUCCESS" and str(item_a.get("current_phase") or "").upper() == "DONE")),
                "b_success": bool(item_b.get("strict_success", str(item_b.get("status") or "") == "SUCCESS" and str(item_b.get("current_phase") or "").upper() == "DONE")),
                "a_score": item_a.get("drb_report_score"),
                "b_score": item_b.get("drb_report_score"),
                "a_fact": item_a.get("fact_score"),
                "b_fact": item_b.get("fact_score"),
                "a_cost": item_a.get("total_cost_rmb_est"),
                "b_cost": item_b.get("total_cost_rmb_est"),
                "a_elapsed": item_a.get("elapsed_seconds"),
                "b_elapsed": item_b.get("elapsed_seconds"),
                "a_direct_answer_support": item_a.get("direct_answer_support_rate"),
                "b_direct_answer_support": item_b.get("direct_answer_support_rate"),
                "a_retrieval_failed": item_a.get("retrieval_failed"),
                "b_retrieval_failed": item_b.get("retrieval_failed"),
            }
        )

    supervisor_only_diagnostics = {
        "legacy_workflow": {"team_stall_count": None, "global_stall_count": None},
        "supervisor_team": {
            "team_stall_count": (summary_b.get("audit_averages") or {}).get("team_stall_count"),
            "global_stall_count": (summary_b.get("audit_averages") or {}).get("global_stall_count"),
            "team_route_trace": {
                str(item.get("id")): len(list(item.get("team_route_trace") or []))
                for item in payload_b.get("results", [])
            },
        },
    }

    smoke_passed = (
        judge_consistent
        and float(summary_b.get("avg_drb_report_score") or 0.0) >= float(summary_a.get("avg_drb_report_score") or 0.0)
        and float(summary_b.get("success_rate") or 0.0) >= float(summary_a.get("success_rate") or 0.0)
        and float((summary_b.get("coverage_averages") or {}).get("direct_answer_support_rate") or 0.0)
        >= float((summary_a.get("coverage_averages") or {}).get("direct_answer_support_rate") or 0.0)
        and float(summary_b.get("avg_total_cost_rmb_est") or 0.0) <= float(summary_a.get("avg_total_cost_rmb_est") or 0.0) * 1.25
    )

    comparison = {
        "architecture_a": label_a,
        "architecture_b": label_b,
        "sample_ids": sample_ids,
        "judge_summary": judge_summary,
        "judge_consistent": judge_consistent,
        "headline_metrics": headline,
        "auxiliary_metrics": {
            "authority_source_rate": {
                "a_value": (summary_a.get("coverage_averages") or {}).get("authority_source_rate"),
                "b_value": (summary_b.get("coverage_averages") or {}).get("authority_source_rate"),
            },
            "blocked_source_rate": {
                "a_value": (summary_a.get("coverage_averages") or {}).get("blocked_source_rate"),
                "b_value": (summary_b.get("coverage_averages") or {}).get("blocked_source_rate"),
            },
            "retrieval_failed": {
                "a_value": sum(1 for item in payload_a.get("results", []) if item.get("retrieval_failed")),
                "b_value": sum(1 for item in payload_b.get("results", []) if item.get("retrieval_failed")),
            },
            "degraded_completion_rate": {
                "a_value": summary_a.get("degraded_completion_rate"),
                "b_value": summary_b.get("degraded_completion_rate"),
            },
        },
        "per_sample": per_sample,
        "supervisor_only_diagnostics": supervisor_only_diagnostics,
        "smoke_result": "supervisor_team smoke passed" if smoke_passed else "supervisor_team smoke inconclusive",
    }

    output_root = Path(output_dir)
    output_root.mkdir(parents=True, exist_ok=True)
    comparison_json = output_root / "comparison.json"
    comparison_md = output_root / "comparison.md"
    comparison_json.write_text(json.dumps(comparison, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "# Legacy vs Supervisor Team Smoke A/B",
        "",
        f"- Architecture A: `{label_a}`",
        f"- Architecture B: `{label_b}`",
        f"- Sample IDs: `{sample_ids}`",
        f"- Smoke Result: `{comparison['smoke_result']}`",
        f"- Judge Model: `{judge_summary['judge_model']}`",
        f"- Judge Mode: `{judge_summary['judge_mode']}`",
        f"- Judge Health: `{judge_summary['judge_health']}`",
        f"- Scoring Reliability: `{judge_summary['scoring_reliability']}`",
        f"- Judge Consistent: `{judge_consistent}`",
        "",
        "## Headline Metrics",
        "",
        "| metric | A | B | delta | delta_pct |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for metric, values in headline.items():
        lines.append(
            f"| {metric} | {values['a_value']} | {values['b_value']} | {values['delta']} | {values['delta_pct']} |"
        )
    lines.extend(
        [
            "",
            "## Per-Sample",
            "",
            "| sample_id | a_success | b_success | a_score | b_score | a_fact | b_fact | a_cost | b_cost | a_elapsed | b_elapsed | a_direct_answer_support | b_direct_answer_support | a_retrieval_failed | b_retrieval_failed |",
            "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |",
        ]
    )
    for row in per_sample:
        lines.append(
            "| {sample_id} | {a_success} | {b_success} | {a_score} | {b_score} | {a_fact} | {b_fact} | {a_cost} | {b_cost} | {a_elapsed} | {b_elapsed} | {a_direct_answer_support} | {b_direct_answer_support} | {a_retrieval_failed} | {b_retrieval_failed} |".format(
                **row
            )
        )
    lines.extend(
        [
            "",
            "## Supervisor-only Diagnostics",
            "",
            "- `legacy_workflow.team_stall_count`: `NA`",
            "- `legacy_workflow.global_stall_count`: `NA`",
            f"- `supervisor_team.team_stall_count`: `{supervisor_only_diagnostics['supervisor_team']['team_stall_count']}`",
            f"- `supervisor_team.global_stall_count`: `{supervisor_only_diagnostics['supervisor_team']['global_stall_count']}`",
            f"- `supervisor_team.team_route_trace_summary`: `{supervisor_only_diagnostics['supervisor_team']['team_route_trace']}`",
        ]
    )
    comparison_md.write_text("\n".join(lines), encoding="utf-8")
    comparison["artifacts"] = {
        "comparison_json": str(comparison_json),
        "comparison_md": str(comparison_md),
    }
    return comparison


def run_smoke_architecture_ab(
    *,
    base_run_id: str,
    sample_ids: list[int] | None,
    research_mode: str,
    judge_model: str | None,
    max_allin_rmb: float,
    max_task_duration_seconds: int | None,
    prompt_dataset: str,
    prompt_split: str,
    reference_dataset: str,
    reference_split: str,
    criteria_dataset: str,
    criteria_split: str,
    allow_heuristic: bool,
    fixture_path: str | Path | None = None,
    run_id: str | None = None,
    smoke_profile: str = "full",
) -> dict[str, Any]:
    ab_run_id = run_id or f"drb-smoke-ab-{time.strftime('%Y%m%d_%H%M%S')}"
    profile = _resolve_smoke_profile(smoke_profile)
    resolved_sample_ids = list(sample_ids or profile["sample_ids"])
    resolved_timeout = (
        int(max_task_duration_seconds)
        if max_task_duration_seconds is not None
        else int(profile["max_task_duration_seconds"])
    )
    fixture = (
        load_frozen_fixture(fixture_path)
        if fixture_path
        else freeze_deepresearch_fixture_from_run(base_run_id=base_run_id, sample_ids=resolved_sample_ids)
    )
    ab_root = ROOT_DIR / "reports" / "deepresearch_bench" / ab_run_id
    legacy = run_deepresearch_benchmark(
        stage=DEFAULT_STAGE,
        sample_size=len(resolved_sample_ids),
        research_mode=research_mode,
        seed=42,
        judge_model=judge_model,
        max_allin_rmb=max_allin_rmb,
        max_task_duration_seconds=resolved_timeout,
        prompt_dataset=prompt_dataset,
        prompt_split=prompt_split,
        reference_dataset=reference_dataset,
        reference_split=reference_split,
        criteria_dataset=criteria_dataset,
        criteria_split=criteria_split,
        architecture_mode="legacy_workflow",
        sample_fixture_path=fixture["fixture_path"],
        sample_ids=resolved_sample_ids,
        run_id=f"{ab_run_id}-legacy",
        output_dir=ab_root / "legacy",
        disable_cache=True,
        resume_from_checkpoint=False,
        allow_heuristic=allow_heuristic,
    )
    supervisor = run_deepresearch_benchmark(
        stage=DEFAULT_STAGE,
        sample_size=len(resolved_sample_ids),
        research_mode=research_mode,
        seed=42,
        judge_model=judge_model,
        max_allin_rmb=max_allin_rmb,
        max_task_duration_seconds=resolved_timeout,
        prompt_dataset=prompt_dataset,
        prompt_split=prompt_split,
        reference_dataset=reference_dataset,
        reference_split=reference_split,
        criteria_dataset=criteria_dataset,
        criteria_split=criteria_split,
        architecture_mode="supervisor_team",
        sample_fixture_path=fixture["fixture_path"],
        sample_ids=resolved_sample_ids,
        run_id=f"{ab_run_id}-supervisor",
        output_dir=ab_root / "supervisor",
        disable_cache=True,
        resume_from_checkpoint=False,
        allow_heuristic=allow_heuristic,
    )
    comparison = compare_deepresearch_runs(
        legacy["artifacts"]["json_path"],
        supervisor["artifacts"]["json_path"],
        output_dir=ab_root,
    )
    return {
        "smoke_profile": profile["name"],
        "smoke_profile_description": profile["description"],
        "sample_ids": resolved_sample_ids,
        "max_task_duration_seconds": resolved_timeout,
        "fixture": fixture,
        "legacy": legacy,
        "supervisor": supervisor,
        "comparison": comparison,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run local-compatible DeepResearch Bench evaluation.")
    parser.add_argument("--stage", choices=["pilot", "calibration", "full"], default=DEFAULT_STAGE)
    parser.add_argument("--sample-size", type=int, default=None)
    parser.add_argument("--mode", default=DEFAULT_MODE)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--judge-model", default=None)
    parser.add_argument("--max-allin-rmb", type=float, default=None)
    parser.add_argument("--max-task-duration-seconds", type=int, default=None)
    parser.add_argument("--prompt-dataset", default=DEFAULT_PROMPT_DATASET)
    parser.add_argument("--prompt-split", default=DEFAULT_PROMPT_SPLIT)
    parser.add_argument("--reference-dataset", default=DEFAULT_REFERENCE_DATASET)
    parser.add_argument("--reference-split", default=DEFAULT_REFERENCE_SPLIT)
    parser.add_argument("--criteria-dataset", default=DEFAULT_CRITERIA_DATASET)
    parser.add_argument("--criteria-split", default=DEFAULT_CRITERIA_SPLIT)
    parser.add_argument("--architecture-mode", choices=["legacy_workflow", "supervisor_team"], default="supervisor_team")
    parser.add_argument("--sample-fixture-path", default=None)
    parser.add_argument("--sample-ids", default=None)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--resume-from-checkpoint", action="store_true")
    parser.add_argument("--freeze-from-run-id", default=None)
    parser.add_argument("--fixture-output-path", default=None)
    parser.add_argument("--compare-results-a", default=None)
    parser.add_argument("--compare-results-b", default=None)
    parser.add_argument("--compare-output-dir", default=None)
    parser.add_argument("--run-smoke-ab", action="store_true")
    parser.add_argument("--smoke-profile", choices=["quick", "full"], default="full")
    parser.add_argument("--allow-heuristic", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    sample_ids = _parse_sample_ids(args.sample_ids)
    if args.freeze_from_run_id:
        fixture = freeze_deepresearch_fixture_from_run(
            base_run_id=args.freeze_from_run_id,
            sample_ids=sample_ids or list(DEFAULT_SMOKE_AB_SAMPLE_IDS),
            output_path=args.fixture_output_path,
        )
        print(fixture["fixture_path"])
        return
    if args.compare_results_a and args.compare_results_b:
        comparison = compare_deepresearch_runs(
            args.compare_results_a,
            args.compare_results_b,
            output_dir=args.compare_output_dir or (ROOT_DIR / "reports" / "deepresearch_bench" / f"compare-{time.strftime('%Y%m%d_%H%M%S')}"),
        )
        print(comparison["artifacts"]["comparison_md"])
        return
    if args.run_smoke_ab:
        ab_payload = run_smoke_architecture_ab(
            base_run_id=DEFAULT_SMOKE_AB_BASE_RUN,
            sample_ids=sample_ids or None,
            research_mode=args.mode,
            judge_model=args.judge_model,
            max_allin_rmb=args.max_allin_rmb if args.max_allin_rmb is not None else _default_budget(DEFAULT_STAGE),
            max_task_duration_seconds=args.max_task_duration_seconds,
            prompt_dataset=args.prompt_dataset,
            prompt_split=args.prompt_split,
            reference_dataset=args.reference_dataset,
            reference_split=args.reference_split,
            criteria_dataset=args.criteria_dataset,
            criteria_split=args.criteria_split,
            allow_heuristic=args.allow_heuristic,
            fixture_path=args.sample_fixture_path,
            run_id=args.run_id,
            smoke_profile=args.smoke_profile,
        )
        print(ab_payload["comparison"]["artifacts"]["comparison_md"])
        return
    sample_size = args.sample_size or _default_sample_size(args.stage)
    budget = args.max_allin_rmb if args.max_allin_rmb is not None else _default_budget(args.stage)
    payload = run_deepresearch_benchmark(
        stage=args.stage,
        sample_size=sample_size,
        research_mode=args.mode,
        seed=args.seed,
        judge_model=args.judge_model,
        max_allin_rmb=budget,
        max_task_duration_seconds=args.max_task_duration_seconds or 900,
        prompt_dataset=args.prompt_dataset,
        prompt_split=args.prompt_split,
        reference_dataset=args.reference_dataset,
        reference_split=args.reference_split,
        criteria_dataset=args.criteria_dataset,
        criteria_split=args.criteria_split,
        architecture_mode=args.architecture_mode,
        sample_fixture_path=args.sample_fixture_path,
        sample_ids=sample_ids or None,
        run_id=args.run_id,
        output_dir=args.output_dir,
        disable_cache=True,
        resume_from_checkpoint=args.resume_from_checkpoint,
        allow_heuristic=args.allow_heuristic,
    )
    print(payload["artifacts"]["md_path"])


if __name__ == "__main__":
    main()
