from __future__ import annotations

import argparse
import json
import random
import sys
import time
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Any

from datasets import load_dataset

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import gateway.executor as executor_module
from gateway.executor import run_research_job_sync
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


def _persist_raw_payload(payload: dict[str, Any]) -> Path:
    run_dir = ROOT_DIR / "reports" / "deepresearch_bench" / str(payload["run_id"])
    run_dir.mkdir(parents=True, exist_ok=True)
    raw_path = run_dir / "raw_results.json"
    raw_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return raw_path


def _normalize_language(value: str) -> str:
    lowered = str(value or "").strip().lower()
    return "zh" if lowered in {"zh", "cn", "chinese", "中文"} else "en"


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
    run_id = f"drb-{stage}-{time.strftime('%Y%m%d_%H%M%S')}"
    results: list[dict[str, Any]] = []
    stopped_early = False
    sample_rows = stratified_deepresearch_sample(
        load_deepresearch_bench_tasks(
            prompt_dataset=prompt_dataset,
            prompt_split=prompt_split,
            reference_dataset=reference_dataset,
            reference_split=reference_split,
            criteria_dataset=criteria_dataset,
            criteria_split=criteria_split,
        ),
        sample_size=sample_size,
        seed=seed,
    )
    try:
        for idx, row in enumerate(sample_rows):
            projected_total = sum(float(item.get("total_cost_rmb_est") or 0.0) for item in results)
            if projected_total >= max_allin_rmb:
                stopped_early = True
                break
            task_id = f"drb-{stage}-{idx}-{uuid.uuid4().hex[:8]}"
            detail = ""
            try:
                research = run_research_job_sync(
                    task_id,
                    row["prompt"],
                    backend="drb_public_benchmark",
                    research_mode=research_mode,
                    disable_cache=True,
                )
            except Exception as exc:
                detail = repr(exc)
                research = {
                    "task_id": task_id,
                    "status": "FAILED",
                    "report": "",
                    "llm_cost_rmb": 0.0,
                    "external_cost_usd_est": 0.0,
                    "elapsed_seconds": 0.0,
                }
            report_text = str(research.get("report") or "")
            metrics = compute_report_metrics(report_text)
            results.append(
                {
                    **row,
                    "task_id": task_id,
                    "research_mode": research_mode,
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
                    "retrieval_metrics": dict(research.get("retrieval_metrics") or {}),
                    "coverage_summary": dict(research.get("coverage_summary") or {}),
                    "fetch_results": list(research.get("fetch_results") or []),
                    "retrieval_failed": bool(research.get("retrieval_failed")),
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
        "research_mode": research_mode,
        "seed": seed,
        "judge_model": judge_model,
        "judge_mode": preflight["judge_mode"],
        "judge_health": preflight["judge_health"],
        "scoring_reliability": preflight["scoring_reliability"],
        "stopped_early": stopped_early,
        "max_allin_rmb": max_allin_rmb,
        "results": results,
    }
    raw_path = _persist_raw_payload(payload)
    try:
        summarized = summarize_deepresearch_results(
            payload,
            judge_model=judge_model,
            allow_local_judge=preflight["judge_health"] == "ready",
            require_local_judge=preflight["judge_health"] == "ready" and not allow_heuristic,
        )
    except Exception as exc:
        raise RuntimeError(f"{exc} (raw results saved to {raw_path})") from exc
    json_path, md_path = write_deepresearch_report(summarized)
    summarized["artifacts"] = {
        "raw_path": str(raw_path),
        "json_path": str(json_path),
        "md_path": str(md_path),
    }
    return summarized


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run local-compatible DeepResearch Bench evaluation.")
    parser.add_argument("--stage", choices=["pilot", "calibration", "full"], default=DEFAULT_STAGE)
    parser.add_argument("--sample-size", type=int, default=None)
    parser.add_argument("--mode", default=DEFAULT_MODE)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--judge-model", default=None)
    parser.add_argument("--max-allin-rmb", type=float, default=None)
    parser.add_argument("--max-task-duration-seconds", type=int, default=900)
    parser.add_argument("--prompt-dataset", default=DEFAULT_PROMPT_DATASET)
    parser.add_argument("--prompt-split", default=DEFAULT_PROMPT_SPLIT)
    parser.add_argument("--reference-dataset", default=DEFAULT_REFERENCE_DATASET)
    parser.add_argument("--reference-split", default=DEFAULT_REFERENCE_SPLIT)
    parser.add_argument("--criteria-dataset", default=DEFAULT_CRITERIA_DATASET)
    parser.add_argument("--criteria-split", default=DEFAULT_CRITERIA_SPLIT)
    parser.add_argument("--allow-heuristic", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    sample_size = args.sample_size or _default_sample_size(args.stage)
    budget = args.max_allin_rmb if args.max_allin_rmb is not None else _default_budget(args.stage)
    payload = run_deepresearch_benchmark(
        stage=args.stage,
        sample_size=sample_size,
        research_mode=args.mode,
        seed=args.seed,
        judge_model=args.judge_model,
        max_allin_rmb=budget,
        max_task_duration_seconds=args.max_task_duration_seconds,
        prompt_dataset=args.prompt_dataset,
        prompt_split=args.prompt_split,
        reference_dataset=args.reference_dataset,
        reference_split=args.reference_split,
        criteria_dataset=args.criteria_dataset,
        criteria_split=args.criteria_split,
        allow_heuristic=args.allow_heuristic,
    )
    print(payload["artifacts"]["md_path"])


if __name__ == "__main__":
    main()
