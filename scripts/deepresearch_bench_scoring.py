from __future__ import annotations

import json
import os
import re
import sys
import time
from collections import Counter
from pathlib import Path
from statistics import mean
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from core.costs import enrich_cost_fields
from scripts.benchmark_scoring import (
    LOCAL_JUDGE_BASE_URL,
    LOCAL_JUDGE_MODEL,
    score_result,
)

try:
    from openai import OpenAI
except Exception:  # pragma: no cover
    OpenAI = None  # type: ignore[assignment]


REPORTS_ROOT = ROOT_DIR / "reports" / "deepresearch_bench"
SECTION_RE = re.compile(r"(?ms)^##\s+(.+?)\n(.*?)(?=^##\s+|\Z)")
URL_RE = re.compile(r"https?://[^\s<>\"]+")
HASH_RE = re.compile(r"\[HASH:[^\]]+\]", re.IGNORECASE)
WORD_RE = re.compile(r"\w+")
BLOCKED_PATTERNS = (
    "HTTP 403",
    "HTTP 429",
    "blocked",
    "captcha",
    "anti-bot",
    "content too short or blocked",
)
UNKNOWN_PATTERNS = (
    "无法确定",
    "cannot determine",
    "unable to verify",
    "insufficient evidence",
    "证据不足",
)
DIMENSIONS = (
    "comprehensiveness",
    "insight",
    "instruction_following",
    "readability",
)
ANALYSIS_SIGNAL_MAP = {
    "comparison": ("compare", "versus", "vs", "better than", "relative to", "comparison", "相比", "对比"),
    "causal": ("because", "driven by", "caused by", "due to", "therefore", "原因", "导致", "因为"),
    "risk": ("risk", "limitation", "constraint", "uncertainty", "caveat", "风险", "限制", "不确定"),
}


def _clamp_score(value: float, low: float = 1.0, high: float = 10.0) -> float:
    return round(max(low, min(high, value)), 2)


def _extract_dimension_scores_from_payload(parsed: dict[str, Any]) -> dict[str, float] | None:
    source = parsed
    if not all(dimension in source for dimension in DIMENSIONS) and isinstance(parsed.get("scores"), dict):
        source = parsed["scores"]
    scores: dict[str, float] = {}
    for dimension in DIMENSIONS:
        raw_value = source.get(dimension)
        if raw_value is None:
            return None
        try:
            numeric = float(raw_value)
        except Exception:
            return None
        if not (1.0 <= numeric <= 10.0):
            return None
        scores[dimension] = _clamp_score(numeric)
    return scores


def _extract_dimension_reasons(parsed: dict[str, Any], judge_model: str) -> dict[str, str]:
    source = parsed.get("reasons")
    if not isinstance(source, dict):
        source = parsed.get("reasoning")
    if not isinstance(source, dict):
        source = {}
    return {
        dimension: str(source.get(dimension) or f"local judge {judge_model}").strip()
        for dimension in DIMENSIONS
    }


def judge_preflight(model_name: str | None = None) -> dict[str, Any]:
    judge_model = model_name or os.getenv("DRB_JUDGE_MODEL") or LOCAL_JUDGE_MODEL
    if OpenAI is None:
        return {
            "judge_health": "unavailable",
            "judge_mode": f"local_ollama:{judge_model}",
            "scoring_reliability": "heuristic_only",
            "reason": "openai_client_missing",
        }
    try:
        client = OpenAI(api_key="ollama", base_url=LOCAL_JUDGE_BASE_URL)
        response = client.chat.completions.create(
            model=judge_model,
            messages=[{"role": "user", "content": 'Return strict JSON only: {"ok": true}'}],
            temperature=0,
            response_format={"type": "json_object"},
        )
        payload = json.loads(response.choices[0].message.content)
        healthy = isinstance(payload, dict) and bool(payload.get("ok")) is True
        return {
            "judge_health": "ready" if healthy else "missing_model",
            "judge_mode": f"local_ollama:{judge_model}",
            "scoring_reliability": "local_judge" if healthy else "heuristic_only",
            "reason": "" if healthy else f"completion_healthcheck_failed:{judge_model}",
        }
    except Exception as exc:
        return {
            "judge_health": "unavailable",
            "judge_mode": f"local_ollama:{judge_model}",
            "scoring_reliability": "heuristic_only",
            "reason": str(exc),
        }


def _report_text(item: dict[str, Any]) -> str:
    return str(item.get("report") or "")


def _compact_text(value: str, *, limit: int) -> str:
    normalized = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 3].rstrip() + "..."


def _normalize_language(value: str) -> str:
    lowered = str(value or "").strip().lower()
    return "zh" if lowered in {"zh", "cn", "chinese", "中文"} else "en"


def _extract_sections(report_text: str) -> dict[str, str]:
    sections: dict[str, str] = {}
    for title, body in SECTION_RE.findall(report_text):
        sections[title.strip().lower()] = body.strip()
    return sections


def _candidate_report_excerpt(report_text: str) -> str:
    sections = _extract_sections(report_text)
    ordered_sections = [
        ("Direct Answer / Core Conclusion", sections.get("direct answer / core conclusion", "")),
        ("Key Evidence", sections.get("key evidence", "")),
        ("Analysis", sections.get("analysis", "")),
        ("Uncertainty / Missing Evidence", sections.get("uncertainty / missing evidence", "")),
    ]
    excerpt_parts = [f"[{title}]\n{body.strip()}" for title, body in ordered_sections if str(body).strip()]
    excerpt = "\n\n".join(excerpt_parts) or report_text
    return _compact_text(excerpt, limit=2200)


def _reference_excerpt(reference_text: str) -> str:
    return _compact_text(reference_text, limit=1400)


def extract_section_bullets(report_text: str, target_titles: tuple[str, ...]) -> list[str]:
    sections = _extract_sections(report_text)
    for title, body in sections.items():
        if any(target in title for target in target_titles):
            lines = [line.strip("-* \t") for line in body.splitlines() if line.strip()]
            return [line for line in lines if line]
    return []


def extract_report_artifacts(report_text: str) -> dict[str, list[str]]:
    missing_sources = extract_section_bullets(
        report_text,
        ("资料缺口", "missing source", "missing evidence", "evidence gap"),
    )
    degraded_items = extract_section_bullets(
        report_text,
        ("降级", "degraded", "fallback", "failed source"),
    )
    return {
        "missing_sources": missing_sources,
        "degraded_items": degraded_items,
    }


def compute_report_metrics(report_text: str) -> dict[str, Any]:
    artifacts = extract_report_artifacts(report_text)
    sections = _extract_sections(report_text)
    headings = list(sections.keys())
    direct_answer = sections.get("direct answer / core conclusion", "")
    analysis = sections.get("analysis", "")
    flags = {
        name: any(pattern in analysis.lower() for pattern in patterns)
        for name, patterns in ANALYSIS_SIGNAL_MAP.items()
    }
    return {
        "word_count": len(WORD_RE.findall(report_text)),
        "citation_count": len(HASH_RE.findall(report_text)),
        "url_count": len(set(URL_RE.findall(report_text))),
        "heading_count": len(headings),
        "headings": headings,
        "direct_answer_present": bool(direct_answer.strip()),
        "direct_answer_citation_backed": "[HASH:" in direct_answer or "http://" in direct_answer or "https://" in direct_answer,
        "analysis_signal_count": sum(1 for value in flags.values() if value),
        "comparison_present": flags["comparison"],
        "causal_present": flags["causal"],
        "risk_present": flags["risk"],
        "missing_sources": artifacts["missing_sources"],
        "degraded_items": artifacts["degraded_items"],
    }


def _format_criteria(criteria: dict[str, list[dict[str, Any]]]) -> str:
    blocks: list[str] = []
    for dimension in DIMENSIONS:
        entries = criteria.get(dimension) or []
        blocks.append(f"[{dimension}]")
        for idx, entry in enumerate(entries, start=1):
            criterion = str(entry.get("criterion") or "").strip()
            explanation = str(entry.get("explanation") or "").strip()
            weight = entry.get("weight", "")
            blocks.append(f"{idx}. ({weight}) {criterion}")
            if explanation:
                blocks.append(f"   - {explanation}")
        if not entries:
            blocks.append("1. No extra criteria provided.")
    return "\n".join(blocks)


def local_deepresearch_judge_scores(
    item: dict[str, Any],
    *,
    model_name: str | None = None,
    max_attempts: int = 3,
) -> dict[str, Any] | None:
    if OpenAI is None:
        return None

    judge_model = model_name or os.getenv("DRB_JUDGE_MODEL") or LOCAL_JUDGE_MODEL
    compact_prompt = _compact_text(str(item.get("prompt", "")), limit=500)
    compact_criteria = _compact_text(_format_criteria(item.get("criterions") or {}), limit=1600)
    compact_reference = _reference_excerpt(str(item.get("reference_article") or ""))
    compact_candidate = _candidate_report_excerpt(str(item.get("report") or ""))
    prompt = f"""You are grading a deep research report against a reference report and explicit criteria.

Return strict JSON only with these fields:
{{
  "comprehensiveness": 7.4,
  "insight": 6.8,
  "instruction_following": 7.1,
  "readability": 6.9,
  "reasons": {{
    "comprehensiveness": "short reason",
    "insight": "short reason",
    "instruction_following": "short reason",
    "readability": "short reason"
  }}
}}

Rules:
- Score each dimension from 1 to 10.
- Be strict.
- Focus on the candidate report, not the reference alone.
- If evidence is missing or the report says it cannot determine, reflect that.

Prompt:
{compact_prompt}

Topic: {item.get("topic", "")}
Language: {item.get("language", "")}

Dimension weights:
{json.dumps(item.get("dimension_weight") or {}, ensure_ascii=False, indent=2)}

Criteria:
{compact_criteria}

Reference report:
{compact_reference}

Candidate report:
{compact_candidate}
"""
    client = OpenAI(api_key="ollama", base_url=LOCAL_JUDGE_BASE_URL)
    for attempt in range(max_attempts):
        try:
            response = client.chat.completions.create(
                model=judge_model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0,
                response_format={"type": "json_object"},
            )
            parsed = json.loads(response.choices[0].message.content)
            if not isinstance(parsed, dict):
                raise ValueError("judge payload is not a JSON object")
            scores = _extract_dimension_scores_from_payload(parsed)
            if scores is None:
                raise ValueError(f"judge payload missing valid dimension scores: {parsed}")
            return {
                "dimension_scores": scores,
                "dimension_reasons": _extract_dimension_reasons(parsed, judge_model),
                "judge_mode": f"local_ollama:{judge_model}",
            }
        except Exception:
            if attempt == max_attempts - 1:
                return None
            time.sleep(1.5 * (attempt + 1))
    return None


def heuristic_dimension_scores(item: dict[str, Any]) -> dict[str, Any]:
    report_text = _report_text(item)
    reference_text = str(item.get("reference_article") or "")
    metrics = compute_report_metrics(report_text)
    reference_words = max(1, len(WORD_RE.findall(reference_text)))
    coverage_ratio = min(1.0, metrics["word_count"] / reference_words)
    has_conclusion = any("conclusion" in title or "总结" in title or "结论" in title for title in metrics["headings"])
    has_analysis = any(
        keyword in title
        for title in metrics["headings"]
        for keyword in ("analysis", "impact", "comparison", "分析", "比较", "评估")
    )

    comprehensiveness = _clamp_score(
        3.5
        + (coverage_ratio * 3.0)
        + min(1.5, metrics["heading_count"] * 0.25)
        + min(1.0, metrics["citation_count"] * 0.08)
        - min(1.5, len(metrics["missing_sources"]) * 0.3)
    )
    insight = _clamp_score(
        3.2
        + (1.0 if has_analysis else 0.0)
        + min(1.8, metrics["word_count"] / 700.0)
        + min(0.8, metrics["citation_count"] * 0.05)
        - min(1.3, len(metrics["degraded_items"]) * 0.25)
    )
    instruction_following = _clamp_score(
        4.0
        + (1.2 if has_conclusion else 0.0)
        + (0.8 if has_analysis else 0.0)
        + min(1.0, metrics["heading_count"] * 0.15)
        - (1.8 if any(token in report_text for token in UNKNOWN_PATTERNS) else 0.0)
    )
    readability = _clamp_score(
        4.2
        + min(2.0, metrics["heading_count"] * 0.2)
        + (0.5 if has_conclusion else 0.0)
        + (0.5 if metrics["url_count"] > 0 else 0.0)
        - (1.2 if metrics["word_count"] < 350 else 0.0)
    )
    return {
        "dimension_scores": {
            "comprehensiveness": comprehensiveness,
            "insight": insight,
            "instruction_following": instruction_following,
            "readability": readability,
        },
        "dimension_reasons": {
            "comprehensiveness": f"heuristic coverage_ratio={coverage_ratio:.2f}, headings={metrics['heading_count']}, citations={metrics['citation_count']}",
            "insight": f"heuristic words={metrics['word_count']}, has_analysis={has_analysis}, degraded={len(metrics['degraded_items'])}",
            "instruction_following": f"heuristic has_conclusion={has_conclusion}, missing_unknown={any(token in report_text for token in UNKNOWN_PATTERNS)}",
            "readability": f"heuristic headings={metrics['heading_count']}, urls={metrics['url_count']}, words={metrics['word_count']}",
        },
        "judge_mode": "heuristic_fallback",
    }


def classify_failure_tags(
    item: dict[str, Any],
    dimension_scores: dict[str, float],
    fact_score: float,
) -> list[str]:
    report_text = _report_text(item)
    metrics = compute_report_metrics(report_text)
    coverage_summary = dict(item.get("coverage_summary") or {})
    lowered = report_text.lower()
    tags: list[str] = []
    if any(pattern.lower() in lowered for pattern in BLOCKED_PATTERNS) or float(coverage_summary.get("blocked_source_rate", 0.0)) > 0.0:
        tags.append("blocked_source")
    if bool(item.get("retrieval_failed")) or metrics["citation_count"] < 3 or len(metrics["missing_sources"]) >= 2:
        tags.append("retrieval_miss")
    if dimension_scores.get("instruction_following", 10.0) < 6.0:
        tags.append("instruction_miss")
    if dimension_scores.get("readability", 10.0) < 6.0 or metrics["heading_count"] < 4:
        tags.append("structure_weak")
    if fact_score < 6.0:
        tags.append("unsupported_claim_risk")
    if any(token in report_text for token in UNKNOWN_PATTERNS) or len(metrics["degraded_items"]) >= 2:
        tags.append("degraded_to_unknown")
    return sorted(set(tags))


def score_deepresearch_result(
    item: dict[str, Any],
    *,
    judge_model: str | None = None,
    allow_local_judge: bool = True,
    require_local_judge: bool = False,
) -> dict[str, Any]:
    enriched = enrich_cost_fields(item)
    fact_scored = score_result(enriched, judge_model=judge_model, allow_local_judge=allow_local_judge)
    judge_payload = None
    if allow_local_judge:
        judge_payload = local_deepresearch_judge_scores(fact_scored, model_name=judge_model)
    if judge_payload is None:
        if require_local_judge:
            raise RuntimeError("local_judge_scoring_failed")
        judge_payload = heuristic_dimension_scores(fact_scored)

    weights = fact_scored.get("dimension_weight") or {}
    dimension_scores = dict(judge_payload["dimension_scores"])
    drb_report_score = round(
        sum(float(weights.get(dimension, 0.0)) * float(dimension_scores.get(dimension, 0.0)) for dimension in DIMENSIONS),
        4,
    )
    metrics = compute_report_metrics(_report_text(fact_scored))
    draft_audit = dict(fact_scored.get("draft_audit") or {})
    task_contract = dict(fact_scored.get("task_contract") or {})
    if draft_audit:
        task_clause_coverage_rate = float(draft_audit.get("task_clause_coverage_rate", 0.0))
    else:
        points = list(task_contract.get("must_answer_points") or [])
        if points:
            covered = 0
            lowered_report = _report_text(fact_scored).lower()
            for point in points:
                clause = str(point.get("question") or "").strip().lower()
                if clause and clause in lowered_report:
                    covered += 1
            task_clause_coverage_rate = round(covered / max(1, len(points)), 4)
        else:
            task_clause_coverage_rate = 0.0
    fact_scored.update(
        {
            "drb_report_score": drb_report_score,
            "dimension_scores": dimension_scores,
            "dimension_reasons": judge_payload["dimension_reasons"],
            "judge_mode": judge_payload["judge_mode"],
            "failure_tags": classify_failure_tags(fact_scored, dimension_scores, float(fact_scored["fact_score"])),
            "missing_sources": fact_scored.get("missing_sources") or metrics["missing_sources"],
            "degraded_items": fact_scored.get("degraded_items") or metrics["degraded_items"],
            "citation_count": int(fact_scored.get("citation_count") or metrics["citation_count"]),
            "hash_count": int(fact_scored.get("hash_count") or metrics["citation_count"]),
            "url_count": int(fact_scored.get("url_count") or metrics["url_count"]),
            "task_clause_coverage_rate": task_clause_coverage_rate,
            "direct_answer_present": bool(draft_audit.get("direct_answer_present", metrics["direct_answer_present"])),
            "direct_answer_citation_backed": bool(
                draft_audit.get("direct_answer_citation_backed", metrics["direct_answer_citation_backed"])
            ),
            "analysis_signal_count": int(draft_audit.get("analysis_signal_count", metrics["analysis_signal_count"])),
            "comparison_present": bool(draft_audit.get("comparison_present", metrics["comparison_present"])),
            "causal_present": bool(draft_audit.get("causal_present", metrics["causal_present"])),
            "risk_present": bool(draft_audit.get("risk_present", metrics["risk_present"])),
            "writer_section_retry_count": int(draft_audit.get("writer_section_retry_count", 0)),
            "writer_transient_error_count": int(draft_audit.get("writer_transient_error_count", 0)),
            "writer_section_fallback_count": int(draft_audit.get("writer_section_fallback_count", 0)),
            "writer_section_fallback_used": bool(draft_audit.get("writer_section_fallback_used", False)),
            "authority_source_rate": float((fact_scored.get("coverage_summary") or {}).get("authority_source_rate", 0.0)),
            "blocked_source_rate": float((fact_scored.get("coverage_summary") or {}).get("blocked_source_rate", 0.0)),
            "blocked_non_pdf_rate": float((fact_scored.get("coverage_summary") or {}).get("blocked_non_pdf_rate", 0.0)),
            "successful_authority_fetch_rate": float(
                (fact_scored.get("coverage_summary") or {}).get("successful_authority_fetch_rate", 0.0)
            ),
            "weak_source_hit_rate": float((fact_scored.get("coverage_summary") or {}).get("weak_source_hit_rate", 0.0)),
            "high_value_evidence_count": int((fact_scored.get("coverage_summary") or {}).get("high_value_evidence_count", 0)),
            "evidence_coverage_rate": float((fact_scored.get("coverage_summary") or {}).get("evidence_coverage_rate", 0.0)),
            "direct_answer_support_rate": float(
                (fact_scored.get("coverage_summary") or {}).get("direct_answer_support_rate", 0.0)
            ),
            "backfill_success_rate": float((fact_scored.get("coverage_summary") or {}).get("backfill_success_rate", 0.0)),
            "same_host_backfill_success_rate": float(
                (fact_scored.get("coverage_summary") or {}).get("same_host_backfill_success_rate", 0.0)
            ),
            "blocked_after_same_host_backfill": int(
                (fact_scored.get("coverage_summary") or {}).get("blocked_after_same_host_backfill", 0)
            ),
            "blocked_by_provider": dict((fact_scored.get("coverage_summary") or {}).get("blocked_by_provider") or {}),
            "blocked_by_page_type": dict((fact_scored.get("coverage_summary") or {}).get("blocked_by_page_type") or {}),
            "blocked_by_host": dict((fact_scored.get("coverage_summary") or {}).get("blocked_by_host") or {}),
            "pdf_parser_salvage_rate": float(
                (fact_scored.get("coverage_summary") or {}).get("pdf_parser_salvage_rate", 0.0)
            ),
            "visual_fallback_salvage_rate": float(
                (fact_scored.get("coverage_summary") or {}).get("visual_fallback_salvage_rate", 0.0)
            ),
        }
    )
    return fact_scored


def _stage_gate(stage: str) -> dict[str, float]:
    if stage == "calibration":
        return {
            "min_success_rate": 0.90,
            "min_drb_report_score": 6.8,
            "min_fact_score": 6.0,
            "max_degraded_unknown": 3.0,
        }
    return {
        "min_success_rate": 0.83,
        "min_drb_report_score": 6.5,
        "min_fact_score": 5.8,
        "max_degraded_unknown": 2.0,
    }


def summarize_deepresearch_results(
    payload: dict[str, Any],
    *,
    judge_model: str | None = None,
    allow_local_judge: bool = True,
    require_local_judge: bool = False,
) -> dict[str, Any]:
    scored_results = [
        score_deepresearch_result(
            item,
            judge_model=judge_model,
            allow_local_judge=allow_local_judge,
            require_local_judge=require_local_judge,
        )
        for item in payload.get("results", [])
    ]
    payload = dict(payload)
    payload["results"] = scored_results
    success_statuses = {"SUCCESS"}
    success_rate = round(
        sum(1 for item in scored_results if str(item.get("status")) in success_statuses) / max(1, len(scored_results)),
        4,
    )
    averages = {
        "drb_report_score": round(mean([float(item.get("drb_report_score") or 0.0) for item in scored_results]), 4)
        if scored_results
        else 0.0,
        "fact_score": round(mean([float(item.get("fact_score") or 0.0) for item in scored_results]), 4)
        if scored_results
        else 0.0,
        "llm_cost_rmb": round(mean([float(item.get("llm_cost_rmb") or 0.0) for item in scored_results]), 4)
        if scored_results
        else 0.0,
        "external_cost_rmb_est": round(mean([float(item.get("external_cost_rmb_est") or 0.0) for item in scored_results]), 4)
        if scored_results
        else 0.0,
        "total_cost_rmb_est": round(mean([float(item.get("total_cost_rmb_est") or 0.0) for item in scored_results]), 4)
        if scored_results
        else 0.0,
        "elapsed_seconds": round(mean([float(item.get("elapsed_seconds") or 0.0) for item in scored_results]), 4)
        if scored_results
        else 0.0,
        "task_clause_coverage_rate": round(mean([float(item.get("task_clause_coverage_rate") or 0.0) for item in scored_results]), 4)
        if scored_results
        else 0.0,
    }
    audit_averages = {
        "direct_answer_present": round(mean([1.0 if item.get("direct_answer_present") else 0.0 for item in scored_results]), 4)
        if scored_results
        else 0.0,
        "direct_answer_citation_backed": round(
            mean([1.0 if item.get("direct_answer_citation_backed") else 0.0 for item in scored_results])
        , 4)
        if scored_results
        else 0.0,
        "analysis_signal_count": round(mean([float(item.get("analysis_signal_count") or 0.0) for item in scored_results]), 4)
        if scored_results
        else 0.0,
        "writer_section_retry_count": round(
            mean([float(item.get("writer_section_retry_count") or 0.0) for item in scored_results]), 4
        )
        if scored_results
        else 0.0,
        "writer_transient_error_count": round(
            mean([float(item.get("writer_transient_error_count") or 0.0) for item in scored_results]), 4
        )
        if scored_results
        else 0.0,
        "writer_section_fallback_count": round(
            mean([float(item.get("writer_section_fallback_count") or 0.0) for item in scored_results]), 4
        )
        if scored_results
        else 0.0,
    }
    coverage_averages = {
        "authority_source_rate": round(mean([float(item.get("authority_source_rate") or 0.0) for item in scored_results]), 4)
        if scored_results
        else 0.0,
        "blocked_source_rate": round(mean([float(item.get("blocked_source_rate") or 0.0) for item in scored_results]), 4)
        if scored_results
        else 0.0,
        "blocked_non_pdf_rate": round(mean([float(item.get("blocked_non_pdf_rate") or 0.0) for item in scored_results]), 4)
        if scored_results
        else 0.0,
        "successful_authority_fetch_rate": round(
            mean([float(item.get("successful_authority_fetch_rate") or 0.0) for item in scored_results]), 4
        )
        if scored_results
        else 0.0,
        "weak_source_hit_rate": round(mean([float(item.get("weak_source_hit_rate") or 0.0) for item in scored_results]), 4)
        if scored_results
        else 0.0,
        "high_value_evidence_count": round(mean([float(item.get("high_value_evidence_count") or 0.0) for item in scored_results]), 4)
        if scored_results
        else 0.0,
        "evidence_coverage_rate": round(mean([float(item.get("evidence_coverage_rate") or 0.0) for item in scored_results]), 4)
        if scored_results
        else 0.0,
        "direct_answer_support_rate": round(
            mean([float(item.get("direct_answer_support_rate") or 0.0) for item in scored_results]), 4
        )
        if scored_results
        else 0.0,
        "backfill_success_rate": round(mean([float(item.get("backfill_success_rate") or 0.0) for item in scored_results]), 4)
        if scored_results
        else 0.0,
        "same_host_backfill_success_rate": round(
            mean([float(item.get("same_host_backfill_success_rate") or 0.0) for item in scored_results]), 4
        )
        if scored_results
        else 0.0,
        "pdf_parser_salvage_rate": round(mean([float(item.get("pdf_parser_salvage_rate") or 0.0) for item in scored_results]), 4)
        if scored_results
        else 0.0,
        "visual_fallback_salvage_rate": round(
            mean([float(item.get("visual_fallback_salvage_rate") or 0.0) for item in scored_results]), 4
        )
        if scored_results
        else 0.0,
        "blocked_after_same_host_backfill": round(
            mean([float(item.get("blocked_after_same_host_backfill") or 0.0) for item in scored_results]), 4
        )
        if scored_results
        else 0.0,
    }
    blocked_by_provider = Counter()
    blocked_by_page_type = Counter()
    blocked_by_host = Counter()
    for item in scored_results:
        blocked_by_provider.update(dict(item.get("blocked_by_provider") or {}))
        blocked_by_page_type.update(dict(item.get("blocked_by_page_type") or {}))
        blocked_by_host.update(dict(item.get("blocked_by_host") or {}))
    dimension_averages = {
        dimension: round(mean([float(item.get("dimension_scores", {}).get(dimension, 0.0)) for item in scored_results]), 4)
        if scored_results
        else 0.0
        for dimension in DIMENSIONS
    }
    weakest_dimension = min(dimension_averages, key=dimension_averages.get) if dimension_averages else ""
    failure_counts = Counter(tag for item in scored_results for tag in item.get("failure_tags", []))
    degraded_unknown_count = sum(1 for item in scored_results if "degraded_to_unknown" in item.get("failure_tags", []))
    gate = _stage_gate(str(payload.get("stage") or "pilot"))
    passed = (
        success_rate >= gate["min_success_rate"]
        and averages["drb_report_score"] >= gate["min_drb_report_score"]
        and averages["fact_score"] >= gate["min_fact_score"]
        and degraded_unknown_count <= gate["max_degraded_unknown"]
    )
    payload["summary"] = {
        "sample_size": len(scored_results),
        "success_rate": success_rate,
        "avg_drb_report_score": averages["drb_report_score"],
        "avg_fact_score": averages["fact_score"],
        "avg_llm_cost_rmb": averages["llm_cost_rmb"],
        "avg_external_cost_rmb_est": averages["external_cost_rmb_est"],
        "avg_total_cost_rmb_est": averages["total_cost_rmb_est"],
        "avg_elapsed_seconds": averages["elapsed_seconds"],
        "avg_task_clause_coverage_rate": averages["task_clause_coverage_rate"],
        "dimension_averages": dimension_averages,
        "audit_averages": audit_averages,
        "coverage_averages": coverage_averages,
        "weakest_dimension": weakest_dimension,
        "language_distribution": Counter(_normalize_language(str(item.get("language"))) for item in scored_results),
        "topic_distribution": Counter(str(item.get("topic") or "unknown") for item in scored_results),
        "failure_tag_counts": dict(failure_counts),
        "blocked_by_provider": dict(blocked_by_provider),
        "blocked_by_page_type": dict(blocked_by_page_type),
        "blocked_by_host": dict(blocked_by_host.most_common(10)),
        "degraded_unknown_count": degraded_unknown_count,
        "gate": {
            **gate,
            "passed": passed,
        },
    }
    return payload


def rescore_saved_deepresearch_run(
    input_path: str | Path,
    *,
    judge_model: str | None = None,
    allow_local_judge: bool = True,
    require_local_judge: bool = False,
    output_dir: str | Path | None = None,
) -> dict[str, Any]:
    payload = json.loads(Path(input_path).read_text(encoding="utf-8"))
    summarized = summarize_deepresearch_results(
        payload,
        judge_model=judge_model,
        allow_local_judge=allow_local_judge,
        require_local_judge=require_local_judge,
    )
    json_path, md_path = write_deepresearch_report(summarized, output_dir=output_dir)
    summarized["artifacts"] = {"json_path": str(json_path), "md_path": str(md_path)}
    return summarized


def write_deepresearch_report(payload: dict[str, Any], output_dir: str | Path | None = None) -> tuple[Path, Path]:
    run_id = str(payload.get("run_id") or "manual")
    target_dir = Path(output_dir) if output_dir else REPORTS_ROOT / run_id
    target_dir.mkdir(parents=True, exist_ok=True)
    json_path = target_dir / "results.json"
    md_path = target_dir / "report.md"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    summary = payload.get("summary") or {}
    dimension_averages = summary.get("dimension_averages") or {}
    audit_averages = summary.get("audit_averages") or {}
    coverage_averages = summary.get("coverage_averages") or {}
    failure_counts = summary.get("failure_tag_counts") or {}
    blocked_by_provider = summary.get("blocked_by_provider") or {}
    blocked_by_page_type = summary.get("blocked_by_page_type") or {}
    blocked_by_host = summary.get("blocked_by_host") or {}
    lines = [
        f"# DeepResearch Bench Local Report: {run_id}",
        "",
        f"- Stage: `{payload.get('stage', 'pilot')}`",
        f"- Judge Mode: `{payload.get('judge_mode', '')}`",
        f"- Judge Health: `{payload.get('judge_health', '')}`",
        f"- Scoring Reliability: `{payload.get('scoring_reliability', '')}`",
        f"- Sample Size: `{summary.get('sample_size', 0)}`",
        f"- Success Rate: `{summary.get('success_rate', 0.0)}`",
        f"- Avg DRB Report Score: `{summary.get('avg_drb_report_score', 0.0)}`",
        f"- Avg FACT Score: `{summary.get('avg_fact_score', 0.0)}`",
        f"- Avg Total Cost (RMB): `{summary.get('avg_total_cost_rmb_est', 0.0)}`",
        f"- Avg Elapsed (s): `{summary.get('avg_elapsed_seconds', 0.0)}`",
        f"- Avg Task Clause Coverage Rate: `{summary.get('avg_task_clause_coverage_rate', 0.0)}`",
        f"- Weakest Dimension: `{summary.get('weakest_dimension', '')}`",
        f"- Gate Passed: `{summary.get('gate', {}).get('passed', False)}`",
        "",
        "## Dimension Averages",
        "",
    ]
    for dimension in DIMENSIONS:
        lines.append(f"- `{dimension}`: `{dimension_averages.get(dimension, 0.0)}`")
    lines.extend(["", "## Audit Averages", ""])
    for key in (
        "direct_answer_present",
        "direct_answer_citation_backed",
        "analysis_signal_count",
        "writer_section_retry_count",
        "writer_transient_error_count",
        "writer_section_fallback_count",
    ):
        lines.append(f"- `{key}`: `{audit_averages.get(key, 0.0)}`")
    lines.extend(["", "## Coverage Averages", ""])
    for key in (
        "authority_source_rate",
        "blocked_source_rate",
        "blocked_non_pdf_rate",
        "successful_authority_fetch_rate",
        "weak_source_hit_rate",
        "high_value_evidence_count",
        "evidence_coverage_rate",
        "direct_answer_support_rate",
        "backfill_success_rate",
        "same_host_backfill_success_rate",
        "blocked_after_same_host_backfill",
        "pdf_parser_salvage_rate",
        "visual_fallback_salvage_rate",
    ):
        lines.append(f"- `{key}`: `{coverage_averages.get(key, 0.0)}`")
    lines.extend(["", "## Failure Tags", ""])
    if failure_counts:
        for tag, count in sorted(failure_counts.items()):
            lines.append(f"- `{tag}`: `{count}`")
    else:
        lines.append("- None")
    lines.extend(["", "## Blocked Breakdown", ""])
    lines.append(f"- `blocked_by_provider`: `{blocked_by_provider}`")
    lines.append(f"- `blocked_by_page_type`: `{blocked_by_page_type}`")
    lines.append(f"- `blocked_by_host`: `{blocked_by_host}`")
    lines.extend(["", "## Per-Task Summary", ""])
    for item in payload.get("results", []):
        lines.extend(
            [
                f"### {item.get('id')} | {item.get('language')} | {item.get('topic')}",
                "",
                f"- Status: `{item.get('status')}`",
                f"- DRB Report Score: `{item.get('drb_report_score')}`",
                f"- FACT Score: `{item.get('fact_score')}`",
                f"- Task Clause Coverage: `{item.get('task_clause_coverage_rate')}`",
                f"- Direct Answer Present: `{item.get('direct_answer_present')}`",
                f"- Analysis Signal Count: `{item.get('analysis_signal_count')}`",
                f"- Blocked Source Rate: `{item.get('blocked_source_rate')}`",
                f"- Blocked Non-PDF Rate: `{item.get('blocked_non_pdf_rate')}`",
                f"- Retrieval Failed: `{item.get('retrieval_failed')}`",
                f"- Writer Section Fallback Count: `{item.get('writer_section_fallback_count')}`",
                f"- Failure Tags: `{', '.join(item.get('failure_tags', [])) or 'none'}`",
                f"- Cost (RMB): `{item.get('total_cost_rmb_est')}`",
                "",
            ]
        )
    md_path.write_text("\n".join(lines), encoding="utf-8")
    return json_path, md_path
