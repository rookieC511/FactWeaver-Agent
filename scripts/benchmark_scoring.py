from __future__ import annotations

import json
import os
import re
import time
from collections import defaultdict
from statistics import mean
from typing import Any

import requests

from core.config import USD_TO_RMB_RATE
from core.costs import enrich_cost_fields

try:
    from openai import OpenAI
except Exception:  # pragma: no cover
    OpenAI = None  # type: ignore[assignment]


LOCAL_JUDGE_BASE_URL = os.getenv("BENCHMARK_JUDGE_BASE_URL", "http://localhost:11434/v1")
LOCAL_JUDGE_MODEL = os.getenv("BENCHMARK_JUDGE_MODEL", "qwen3:8b")
QUALITY_FACT_WEIGHT = 0.55
QUALITY_RACE_WEIGHT = 0.45
OVERALL_QUALITY_WEIGHT = 0.70
OVERALL_VALUE_WEIGHT = 0.30

HASH_RE = re.compile(r"\[HASH:[^\]]+\]", re.IGNORECASE)
URL_RE = re.compile(r"https?://[^\s<>\"]+")
LINK_RE = re.compile(r"\[[^\]]+\]\((https?://[^\s)]+)\)")
HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*$", re.MULTILINE)
WORD_RE = re.compile(r"\w+")
MOJIBAKE_RE = re.compile(r"閳閿泑閸檤閺佺増|缂傚搫|鐠у嫭|閻梻|閹存劖|瀵洜", re.IGNORECASE)

_judge_availability_cache: dict[str, bool] = {}


def _clamp_score(value: float, low: float = 1.0, high: float = 10.0) -> float:
    return round(max(low, min(high, value)), 2)


def _report_text(item: dict[str, Any]) -> str:
    return str(item.get("report") or item.get("report_preview") or "")


def _missing_signal_count(report_text: str) -> int:
    patterns = [
        "HTTP 403",
        "HTTP 429",
        "content too short or blocked",
        "VISUAL_BROWSE_UNAVAILABLE",
        "deterministic skip",
        "| reason=",
        "| stage=",
        "Unavailable",
    ]
    return sum(report_text.count(pattern) for pattern in patterns)


def _extract_heading_titles(report_text: str) -> list[str]:
    return [match.group(2).strip().lower() for match in HEADING_RE.finditer(report_text)]


def _has_heading(titles: list[str], keywords: tuple[str, ...]) -> bool:
    return any(any(keyword in title for keyword in keywords) for title in titles)


def _heuristic_fact_score(report_text: str) -> tuple[float, str]:
    hash_count = len(HASH_RE.findall(report_text))
    markdown_links = len(set(LINK_RE.findall(report_text)))
    bare_urls = len(set(URL_RE.findall(report_text)))
    citation_units = hash_count + (0.6 * markdown_links) + (0.25 * bare_urls)
    word_count = max(1, len(WORD_RE.findall(report_text)))
    density = citation_units / max(1.0, word_count / 250.0)
    missing_signals = _missing_signal_count(report_text)

    base_score = 2.0 + min(5.5, density * 1.3) + min(1.5, hash_count * 0.08)
    if hash_count >= 10:
        base_score += 0.6
    if markdown_links >= 3:
        base_score += 0.4

    penalties = min(2.8, missing_signals * 0.12)
    if citation_units == 0:
        penalties += 2.5
    if "TODO" in report_text or "TBD" in report_text:
        penalties += 0.5

    score = _clamp_score(base_score - penalties)
    reason = f"heuristic refs={citation_units:.1f}, density={density:.2f}, missing_signals={missing_signals}"
    return score, reason


def _heuristic_race_score(report_text: str) -> tuple[float, str]:
    headings = _extract_heading_titles(report_text)
    unique_headings = len(set(headings))
    paragraphs = [block for block in re.split(r"\n\s*\n", report_text) if len(block.strip()) > 80]
    word_count = len(WORD_RE.findall(report_text))
    has_intro = _has_heading(headings, ("introduction", "overview", "background", "abstract", "summary"))
    has_conclusion = _has_heading(headings, ("conclusion", "recommendation", "takeaway", "outlook"))
    has_references = _has_heading(headings, ("reference", "source", "citation"))
    has_analysis = _has_heading(headings, ("analysis", "comparison", "cost", "impact", "performance"))
    table_count = report_text.count("\n|")
    mojibake_markers = len(MOJIBAKE_RE.findall(report_text))

    base_score = 2.2
    base_score += min(3.2, unique_headings * 0.55)
    base_score += 0.8 if has_intro else 0.0
    base_score += 0.8 if has_conclusion else 0.0
    base_score += 0.5 if has_references else 0.0
    base_score += 0.5 if has_analysis else 0.0
    base_score += min(1.0, len(paragraphs) * 0.08)
    base_score += 0.4 if table_count > 0 else 0.0
    base_score += 0.9 if word_count >= 900 else (0.5 if word_count >= 600 else 0.0)

    penalties = 0.0
    if unique_headings < 4:
        penalties += 1.0
    if len(paragraphs) < 5:
        penalties += 0.8
    if mojibake_markers >= 6:
        penalties += min(1.6, mojibake_markers * 0.08)

    score = _clamp_score(base_score - penalties)
    reason = f"heuristic headings={unique_headings}, paragraphs={len(paragraphs)}, words={word_count}, mojibake={mojibake_markers}"
    return score, reason


def _judge_available(model_name: str) -> bool:
    if OpenAI is None:
        return False
    if model_name in _judge_availability_cache:
        return _judge_availability_cache[model_name]
    try:
        response = requests.get(f"{LOCAL_JUDGE_BASE_URL}/models", timeout=2)
        response.raise_for_status()
        _judge_availability_cache[model_name] = True
    except Exception:
        _judge_availability_cache[model_name] = False
    return _judge_availability_cache[model_name]


def local_judge_scores(report_text: str, *, model_name: str | None = None) -> tuple[float, float, str, str] | None:
    judge_model = model_name or LOCAL_JUDGE_MODEL
    if not _judge_available(judge_model):
        return None

    prompt = f"""You are a strict evaluator for deep research reports.

Evaluate the report on two axes from 1 to 10:
1. FACT: citation support, traceability, and unsupported-claim risk
2. RACE: report architecture, coverage, coherence, and depth

RACE does NOT mean ethnicity or demographics.

Report:
{report_text[:12000]}

Respond with strict JSON only:
{{
  "fact_score": 8.4,
  "race_score": 7.9,
  "fact_reason": "short reason",
  "race_reason": "short reason"
}}
"""
    try:
        client = OpenAI(api_key="ollama", base_url=LOCAL_JUDGE_BASE_URL)
        started = time.perf_counter()
        response = client.chat.completions.create(
            model=judge_model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            response_format={"type": "json_object"},
        )
        raw = response.choices[0].message.content
        parsed = json.loads(raw)
        fact_score = _clamp_score(float(parsed.get("fact_score", 0.0)))
        race_score = _clamp_score(float(parsed.get("race_score", 0.0)))
        _ = time.perf_counter() - started
        return (
            fact_score,
            race_score,
            str(parsed.get("fact_reason", "")).strip() or f"local judge {judge_model}",
            str(parsed.get("race_reason", "")).strip() or f"local judge {judge_model}",
        )
    except Exception:
        _judge_availability_cache[judge_model] = False
        return None


def score_result(
    item: dict[str, Any],
    *,
    judge_model: str | None = None,
    allow_local_judge: bool = True,
) -> dict[str, Any]:
    enriched = enrich_cost_fields(item)
    report_text = _report_text(enriched)
    judge_result = None
    if allow_local_judge:
        judge_result = local_judge_scores(report_text, model_name=judge_model)

    if judge_result is None:
        fact_score, fact_reason = _heuristic_fact_score(report_text)
        race_score, race_reason = _heuristic_race_score(report_text)
        judge_mode = "heuristic_fallback"
    else:
        fact_score, race_score, fact_reason, race_reason = judge_result
        judge_mode = f"local_ollama:{judge_model or LOCAL_JUDGE_MODEL}"

    quality_score = round((fact_score * QUALITY_FACT_WEIGHT) + (race_score * QUALITY_RACE_WEIGHT), 4)
    enriched.update(
        {
            "judge_mode": judge_mode,
            "fact_score": fact_score,
            "race_score": race_score,
            "fact_reason": fact_reason,
            "race_reason": race_reason,
            "quality_score": quality_score,
        }
    )
    return enriched


def _cost_efficiency_index(item: dict[str, Any]) -> float:
    total_cost = float(item.get("total_cost_rmb_est") or 0.0)
    quality_score = float(item.get("quality_score") or 0.0)
    if total_cost <= 0:
        total_cost = 0.000001
    return quality_score / total_cost


def annotate_results(
    results: list[dict[str, Any]],
    *,
    judge_model: str | None = None,
    allow_local_judge: bool = True,
) -> list[dict[str, Any]]:
    scored_results = [
        score_result(item, judge_model=judge_model, allow_local_judge=allow_local_judge)
        for item in results
    ]
    max_index = max((_cost_efficiency_index(item) for item in scored_results), default=0.0)
    for item in scored_results:
        efficiency_index = _cost_efficiency_index(item)
        cost_efficiency_score = 0.0 if max_index <= 0 else round((efficiency_index / max_index) * 10.0, 4)
        overall_score = round(
            (float(item["quality_score"]) * OVERALL_QUALITY_WEIGHT)
            + (cost_efficiency_score * OVERALL_VALUE_WEIGHT),
            4,
        )
        item["cost_efficiency_index"] = round(efficiency_index, 6)
        item["cost_efficiency_score"] = cost_efficiency_score
        item["overall_score"] = overall_score
    return scored_results


def build_mode_summary(results: list[dict[str, Any]]) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in results:
        grouped[str(item.get("research_mode") or "unknown")].append(item)

    mode_summary: dict[str, Any] = {}
    for mode, items in grouped.items():
        mode_summary[mode] = {
            "runs": len(items),
            "avg_llm_cost_rmb": round(mean(float(i.get("llm_cost_rmb", 0.0)) for i in items), 6),
            "avg_external_cost_rmb_est": round(mean(float(i.get("external_cost_rmb_est", 0.0)) for i in items), 6),
            "avg_total_cost_rmb_est": round(mean(float(i.get("total_cost_rmb_est", 0.0)) for i in items), 6),
            "avg_quality_score": round(mean(float(i.get("quality_score", 0.0)) for i in items), 4),
            "avg_cost_efficiency_score": round(mean(float(i.get("cost_efficiency_score", 0.0)) for i in items), 4),
            "avg_overall_score": round(mean(float(i.get("overall_score", 0.0)) for i in items), 4),
            "avg_elapsed_seconds": round(mean(float(i.get("elapsed_seconds", 0.0)) for i in items), 2),
        }

    if not mode_summary:
        return {"modes": {}, "recommended_default_mode": None}

    qualified_default_modes = [
        mode for mode, data in mode_summary.items() if float(data.get("avg_quality_score", 0.0)) >= 8.0
    ] or list(mode_summary)

    return {
        "usd_to_rmb_rate": USD_TO_RMB_RATE,
        "modes": mode_summary,
        "recommended_default_mode": max(qualified_default_modes, key=lambda mode: mode_summary[mode]["avg_overall_score"]),
        "highest_quality_mode": max(mode_summary, key=lambda mode: mode_summary[mode]["avg_quality_score"]),
        "best_value_mode": max(mode_summary, key=lambda mode: mode_summary[mode]["avg_cost_efficiency_score"]),
        "slowest_mode": max(mode_summary, key=lambda mode: mode_summary[mode]["avg_elapsed_seconds"]),
        "most_expensive_mode": max(mode_summary, key=lambda mode: mode_summary[mode]["avg_total_cost_rmb_est"]),
    }


def summarize_payload(
    payload: dict[str, Any],
    *,
    judge_model: str | None = None,
    allow_local_judge: bool = True,
) -> dict[str, Any]:
    results = annotate_results(
        list(payload.get("results") or []),
        judge_model=judge_model,
        allow_local_judge=allow_local_judge,
    )
    summary = build_mode_summary(results)
    total_llm_cost_rmb = round(sum(float(item.get("llm_cost_rmb", 0.0)) for item in results), 6)
    total_external_cost_usd_est = round(sum(float(item.get("external_cost_usd_est", 0.0)) for item in results), 6)
    total_external_cost_rmb_est = round(sum(float(item.get("external_cost_rmb_est", 0.0)) for item in results), 6)
    total_cost_rmb_est = round(sum(float(item.get("total_cost_rmb_est", 0.0)) for item in results), 6)

    enriched_payload = dict(payload)
    enriched_payload["results"] = results
    enriched_payload["usd_to_rmb_rate"] = USD_TO_RMB_RATE
    enriched_payload["actual_total_llm_cost_rmb"] = total_llm_cost_rmb
    enriched_payload["actual_total_external_cost_usd_est"] = total_external_cost_usd_est
    enriched_payload["actual_total_external_cost_rmb_est"] = total_external_cost_rmb_est
    enriched_payload["actual_total_cost_rmb_est"] = total_cost_rmb_est
    enriched_payload["mode_summary"] = summary
    if judge_model:
        enriched_payload["benchmark_judge_model"] = judge_model
    return enriched_payload
