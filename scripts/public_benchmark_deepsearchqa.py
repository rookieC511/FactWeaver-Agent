from __future__ import annotations

import argparse
import json
import random
import re
import string
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

from core.config import USD_TO_RMB_RATE
from core.costs import usd_to_rmb
import gateway.executor as executor_module
from gateway.executor import run_research_job_sync
from gateway.state_store import get_task
from scripts.benchmark_scoring import LOCAL_JUDGE_BASE_URL, LOCAL_JUDGE_MODEL, score_result

try:
    from openai import OpenAI
except Exception:  # pragma: no cover
    OpenAI = None  # type: ignore[assignment]


REPORTS_DIR = ROOT_DIR / "reports"

DEFAULT_DATASET_ID = "google/deepsearchqa"
DEFAULT_SPLIT = "eval"
DEFAULT_MODE = "medium"
DEFAULT_SAMPLE_SIZE = 30
ANSWER_HEADING_RE = re.compile(
    r"(?:^|\n)(?:#+\s*final answer\s*|final answer\s*:)(.+?)(?:\n#|\Z)",
    re.IGNORECASE | re.DOTALL,
)
TOKEN_RE = re.compile(r"\w+")


def stratified_sample(rows: list[dict[str, Any]], sample_size: int, seed: int) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row.get("problem_category") or "unknown")].append(row)
    rng = random.Random(seed)
    for items in grouped.values():
        rng.shuffle(items)
    categories = sorted(grouped)
    selected: list[dict[str, Any]] = []
    while categories and len(selected) < sample_size:
        next_categories: list[str] = []
        for category in categories:
            bucket = grouped[category]
            if bucket and len(selected) < sample_size:
                selected.append(bucket.pop())
            if bucket:
                next_categories.append(category)
        categories = next_categories
    return selected


def _normalize_text(value: str) -> str:
    lowered = value.lower()
    lowered = lowered.translate(str.maketrans("", "", string.punctuation))
    lowered = re.sub(r"\b(a|an|the)\b", " ", lowered)
    lowered = re.sub(r"\s+", " ", lowered)
    return lowered.strip()


def _token_f1(prediction: str, answer: str) -> float:
    pred_tokens = TOKEN_RE.findall(_normalize_text(prediction))
    gold_tokens = TOKEN_RE.findall(_normalize_text(answer))
    if not pred_tokens and not gold_tokens:
        return 1.0
    if not pred_tokens or not gold_tokens:
        return 0.0
    common = 0
    gold_counts: dict[str, int] = defaultdict(int)
    for token in gold_tokens:
        gold_counts[token] += 1
    for token in pred_tokens:
        if gold_counts[token] > 0:
            common += 1
            gold_counts[token] -= 1
    if common == 0:
        return 0.0
    precision = common / len(pred_tokens)
    recall = common / len(gold_tokens)
    return round((2 * precision * recall) / (precision + recall), 4)


def _split_set_answers(value: str) -> list[str]:
    parts = re.split(r"[,\n;]+|\s+\band\b\s+", value)
    return [part.strip() for part in parts if part.strip()]


def _set_f1(predicted: list[str], gold: list[str]) -> float:
    pred_set = {_normalize_text(item) for item in predicted if _normalize_text(item)}
    gold_set = {_normalize_text(item) for item in gold if _normalize_text(item)}
    if not pred_set and not gold_set:
        return 1.0
    if not pred_set or not gold_set:
        return 0.0
    tp = len(pred_set & gold_set)
    if tp == 0:
        return 0.0
    precision = tp / len(pred_set)
    recall = tp / len(gold_set)
    return round((2 * precision * recall) / (precision + recall), 4)


def _heuristic_extract_answer(report: str, answer_type: str) -> dict[str, Any]:
    match = ANSWER_HEADING_RE.search(report)
    if match:
        content = match.group(1).strip()
    else:
        paragraphs = [block.strip() for block in re.split(r"\n\s*\n", report) if block.strip()]
        content = paragraphs[-1] if paragraphs else report.strip()
    if answer_type == "Set Answer":
        final_answers = _split_set_answers(content)
        return {"final_answer": ", ".join(final_answers), "final_answers": final_answers, "extractor_mode": "heuristic"}
    return {"final_answer": content.splitlines()[0].strip(), "final_answers": [], "extractor_mode": "heuristic"}


def extract_answer_from_report(
    report: str,
    *,
    answer_type: str,
    extractor_model: str | None = None,
) -> dict[str, Any]:
    heuristic = _heuristic_extract_answer(report, answer_type)
    if OpenAI is None:
        return heuristic

    model_name = extractor_model or LOCAL_JUDGE_MODEL
    prompt = f"""Extract the benchmark answer from this research report.

Return strict JSON only.
- For Single Answer: {{"final_answer":"...", "final_answers":[]}}
- For Set Answer: {{"final_answer":"comma separated", "final_answers":["a","b"]}}

Do not explain.
Answer type: {answer_type}

Report:
{report[:12000]}
"""
    try:
        client = OpenAI(api_key="ollama", base_url=LOCAL_JUDGE_BASE_URL)
        response = client.chat.completions.create(
            model=model_name,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
            response_format={"type": "json_object"},
        )
        parsed = json.loads(response.choices[0].message.content)
        final_answer = str(parsed.get("final_answer") or "").strip()
        final_answers = parsed.get("final_answers") or []
        if isinstance(final_answers, list):
            normalized_list = [str(item).strip() for item in final_answers if str(item).strip()]
        else:
            normalized_list = []
        if answer_type == "Set Answer" and not normalized_list and final_answer:
            normalized_list = _split_set_answers(final_answer)
        if not final_answer and normalized_list:
            final_answer = ", ".join(normalized_list)
        if not final_answer:
            return heuristic
        return {
            "final_answer": final_answer,
            "final_answers": normalized_list,
            "extractor_mode": f"local_ollama:{model_name}",
        }
    except Exception:
        return heuristic


def evaluate_prediction(predicted: dict[str, Any], gold_answer: str, answer_type: str) -> dict[str, Any]:
    gold_normalized = _normalize_text(gold_answer)
    if answer_type == "Set Answer":
        predicted_items = predicted.get("final_answers") or _split_set_answers(str(predicted.get("final_answer") or ""))
        gold_items = _split_set_answers(gold_answer)
        exact_match = 1.0 if {_normalize_text(item) for item in predicted_items} == {_normalize_text(item) for item in gold_items} else 0.0
        f1 = _set_f1(predicted_items, gold_items)
        return {"exact_match": exact_match, "f1": f1}

    final_answer = str(predicted.get("final_answer") or "")
    exact_match = 1.0 if _normalize_text(final_answer) == gold_normalized else 0.0
    f1 = _token_f1(final_answer, gold_answer)
    return {"exact_match": exact_match, "f1": f1}


def load_deepsearchqa_sample(*, sample_size: int, seed: int, dataset_id: str, split: str) -> list[dict[str, Any]]:
    dataset = load_dataset(dataset_id, split=split)
    rows = [dict(item) for item in dataset]
    return stratified_sample(rows, sample_size=sample_size, seed=seed)


def build_benchmark_query(problem: str, answer_type: str) -> str:
    answer_contract = (
        "At the end of the report, add a markdown section titled 'Final Answer'. "
        "That section must contain only the direct final answer."
    )
    if answer_type == "Set Answer":
        answer_contract += " If there are multiple answers, list them as a comma-separated set in that section."
    return f"{problem}\n\n[Benchmark Output Contract]\n{answer_contract}"


def run_public_benchmark(
    *,
    sample_size: int,
    research_mode: str,
    dataset_id: str,
    split: str,
    seed: int,
    judge_model: str | None,
    extractor_model: str | None,
    max_allin_rmb: float,
    max_task_duration_seconds: int,
) -> dict[str, Any]:
    original_max_task_duration_seconds = executor_module.MAX_TASK_DURATION_SECONDS
    executor_module.MAX_TASK_DURATION_SECONDS = max_task_duration_seconds
    sample_rows = load_deepsearchqa_sample(
        sample_size=sample_size,
        seed=seed,
        dataset_id=dataset_id,
        split=split,
    )
    results: list[dict[str, Any]] = []
    stopped_early = False
    try:
        for idx, row in enumerate(sample_rows):
            projected_total = sum(float(item["total_cost_rmb_est"]) for item in results)
            if projected_total >= max_allin_rmb:
                stopped_early = True
                break
            task_id = f"deepsearchqa-{research_mode}-{idx}-{uuid.uuid4().hex[:8]}"
            research: dict[str, Any]
            failure_error = ""
            try:
                research = run_research_job_sync(
                    task_id,
                    build_benchmark_query(str(row["problem"]), str(row["answer_type"])),
                    backend="public_benchmark",
                    research_mode=research_mode,
                    disable_cache=True,
                )
            except Exception as exc:
                failure_error = repr(exc)
                persisted = get_task(task_id) or {}
                research = {
                    "task_id": task_id,
                    "status": persisted.get("status", "FAILED"),
                    "report": persisted.get("report", ""),
                    "llm_cost_rmb": float(persisted.get("llm_cost_rmb") or 0.0),
                    "external_cost_usd_est": float(persisted.get("external_cost_usd_est") or 0.0),
                    "elapsed_seconds": float(persisted.get("elapsed_seconds") or 0.0),
                    "detail": persisted.get("detail") or failure_error,
                }
            prediction = extract_answer_from_report(
                str(research.get("report") or ""),
                answer_type=str(row["answer_type"]),
                extractor_model=extractor_model,
            )
            answer_score = evaluate_prediction(prediction, str(row["answer"]), str(row["answer_type"]))
            quality_scored = score_result(dict(research), judge_model=judge_model, allow_local_judge=True)
            results.append(
                {
                    "task_id": task_id,
                    "problem": row["problem"],
                    "problem_category": row["problem_category"],
                    "gold_answer": row["answer"],
                    "answer_type": row["answer_type"],
                    "research_mode": research_mode,
                    "status": research.get("status", "FAILED"),
                    "error": failure_error or research.get("detail", ""),
                    "report": research.get("report"),
                    "predicted_answer": prediction["final_answer"],
                    "predicted_answers": prediction["final_answers"],
                    "extractor_mode": prediction["extractor_mode"],
                    "exact_match": answer_score["exact_match"],
                    "f1": answer_score["f1"],
                    "llm_cost_rmb": float(research.get("llm_cost_rmb", 0.0)),
                    "external_cost_usd_est": float(research.get("external_cost_usd_est", 0.0)),
                    "external_cost_rmb_est": usd_to_rmb(research.get("external_cost_usd_est", 0.0)),
                    "total_cost_rmb_est": float(research.get("llm_cost_rmb", 0.0))
                    + usd_to_rmb(research.get("external_cost_usd_est", 0.0)),
                    "elapsed_seconds": float(research.get("elapsed_seconds", 0.0)),
                    "fact_score": quality_scored.get("fact_score"),
                    "race_score": quality_scored.get("race_score"),
                    "quality_score": quality_scored.get("quality_score"),
                    "judge_mode": quality_scored.get("judge_mode"),
                }
            )
    finally:
        executor_module.MAX_TASK_DURATION_SECONDS = original_max_task_duration_seconds

    exact_matches = [float(item["exact_match"]) for item in results]
    f1_scores = [float(item["f1"]) for item in results]
    total_costs = [float(item["total_cost_rmb_est"]) for item in results]
    elapsed = [float(item["elapsed_seconds"]) for item in results]
    return {
        "dataset_id": dataset_id,
        "dataset_split": split,
        "sample_size": len(results),
        "requested_sample_size": sample_size,
        "research_mode": research_mode,
        "stopped_early": stopped_early,
        "max_allin_rmb": max_allin_rmb,
        "usd_to_rmb_rate": USD_TO_RMB_RATE,
        "results": results,
        "summary": {
            "exact_match": round(sum(exact_matches) / max(1, len(exact_matches)), 4),
            "f1": round(sum(f1_scores) / max(1, len(f1_scores)), 4),
            "avg_total_cost_rmb_est": round(sum(total_costs) / max(1, len(total_costs)), 4),
            "avg_elapsed_seconds": round(sum(elapsed) / max(1, len(elapsed)), 4),
            "task_success_rate": round(sum(1 for item in results if item.get("status") == "SUCCESS") / max(1, len(results)), 4),
            "answer_extraction_failure_rate": round(
                sum(1 for item in results if not str(item.get("predicted_answer") or "").strip()) / max(1, len(results)),
                4,
            ),
        },
    }


def write_report(payload: dict[str, Any]) -> tuple[Path, Path]:
    REPORTS_DIR.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    json_path = REPORTS_DIR / f"public_benchmark_deepsearchqa_{stamp}.json"
    md_path = REPORTS_DIR / f"public_benchmark_deepsearchqa_{stamp}.md"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = payload["summary"]
    lines = [
        "# DeepSearchQA Public Benchmark",
        "",
        f"- Dataset: `{payload['dataset_id']}` / `{payload['dataset_split']}`",
        f"- Sample Size: `{payload['sample_size']}`",
        f"- Requested Sample Size: `{payload['requested_sample_size']}`",
        f"- Mode: `{payload['research_mode']}`",
        f"- Max All-in Budget (RMB): `{payload['max_allin_rmb']}`",
        f"- Stopped Early: `{payload['stopped_early']}`",
        "",
        "| Metric | Value |",
        "| --- | ---: |",
        f"| Exact Match | {summary['exact_match']:.4f} |",
        f"| F1 | {summary['f1']:.4f} |",
        f"| Avg Total Cost (RMB) | {summary['avg_total_cost_rmb_est']:.4f} |",
        f"| Avg Elapsed (s) | {summary['avg_elapsed_seconds']:.4f} |",
        f"| Task Success Rate | {summary['task_success_rate']:.4f} |",
        f"| Answer Extraction Failure Rate | {summary['answer_extraction_failure_rate']:.4f} |",
    ]
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, md_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a public DeepSearchQA benchmark sample.")
    parser.add_argument("--dataset-id", default=DEFAULT_DATASET_ID)
    parser.add_argument("--split", default=DEFAULT_SPLIT)
    parser.add_argument("--sample-size", type=int, default=DEFAULT_SAMPLE_SIZE)
    parser.add_argument("--mode", default=DEFAULT_MODE)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--judge-model", default=LOCAL_JUDGE_MODEL)
    parser.add_argument("--extractor-model", default=LOCAL_JUDGE_MODEL)
    parser.add_argument("--max-allin-rmb", type=float, default=30.0)
    parser.add_argument("--max-task-duration-seconds", type=int, default=900)
    args = parser.parse_args()
    payload = run_public_benchmark(
        sample_size=args.sample_size,
        research_mode=args.mode,
        dataset_id=args.dataset_id,
        split=args.split,
        seed=args.seed,
        judge_model=args.judge_model or None,
        extractor_model=args.extractor_model or None,
        max_allin_rmb=args.max_allin_rmb,
        max_task_duration_seconds=args.max_task_duration_seconds,
    )
    json_path, md_path = write_report(payload)
    print(f"[public-benchmark] json={json_path}")
    print(f"[public-benchmark] markdown={md_path}")


if __name__ == "__main__":
    main()
