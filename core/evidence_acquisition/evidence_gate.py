from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any, TypedDict


class EvidenceSlot(TypedDict, total=False):
    question: str
    section_id: str
    fact_count: int
    source_urls: list[str]
    high_authority_source_count: int
    covered: bool


class EvidenceBundle(TypedDict, total=False):
    candidates: list[dict[str, Any]]
    fetch_results: list[dict[str, Any]]
    high_value_sources: list[str]
    evidence_slots: dict[str, EvidenceSlot]
    coverage_summary: dict[str, float | int]
    backfill_attempts: int


BLOCKED_ERROR_CLASSES = {
    "http_401_403",
    "http_412",
    "http_429",
    "ssl_error",
    "redirect_loop",
    "js_only",
    "pdf_unreadable",
}


def _is_blocked_fetch(item: dict[str, Any]) -> bool:
    return str(item.get("error_class") or "") in BLOCKED_ERROR_CLASSES


def build_evidence_slots(
    *,
    task_contract: dict[str, Any],
    km: Any,
) -> dict[str, EvidenceSlot]:
    slots: dict[str, EvidenceSlot] = {}
    for point in task_contract.get("must_answer_points") or []:
        point_id = str(point.get("id") or len(slots) + 1)
        section_id = str(point.get("section_id") or "global")
        docs = km.retrieve(section_id=section_id, k=12)
        direct_facts = 0
        non_weak_urls: set[str] = set()
        high_authority_sources: set[str] = set()
        evidence_urls: list[str] = []
        for doc in docs:
            source_tier = str(doc.metadata.get("source_tier") or "")
            authority_score = float(doc.metadata.get("authority_score") or 0.0)
            url = str(doc.metadata.get("url") or doc.metadata.get("source_url") or "")
            if len(doc.page_content.strip()) >= 80:
                direct_facts += 1
            if url and url not in evidence_urls:
                evidence_urls.append(url)
            if source_tier != "weak" and url:
                non_weak_urls.add(url)
            if (source_tier == "high_authority" or authority_score >= 0.95) and url:
                high_authority_sources.add(url)
        slots[point_id] = {
            "question": str(point.get("question") or ""),
            "section_id": section_id,
            "fact_count": direct_facts,
            "source_urls": evidence_urls[:5],
            "high_authority_source_count": len(high_authority_sources),
            "covered": direct_facts >= 1 and bool(non_weak_urls),
        }
    return slots


def compute_coverage_summary(
    *,
    plan: list[dict[str, Any]],
    km: Any,
    retrieval_metrics: dict[str, int],
    evidence_slots: dict[str, EvidenceSlot],
    fetch_results: list[dict[str, Any]] | None = None,
    backfill_attempts: int = 0,
    backfill_successes: int = 0,
) -> dict[str, float | int]:
    section_ids = [item.get("section_id", "global") for item in plan] or ["global"]
    covered_sections = 0
    high_value_evidence_count = 0
    strong_sources: set[str] = set()
    weak_sources: set[str] = set()

    for section_id in section_ids:
        docs = km.retrieve(section_id=section_id, k=20)
        has_support = False
        for doc in docs:
            source_tier = str(doc.metadata.get("source_tier") or "")
            authority_score = float(doc.metadata.get("authority_score") or 0.0)
            url = str(doc.metadata.get("url") or doc.metadata.get("source_url") or "")
            if source_tier == "high_authority" or authority_score >= 0.95:
                if url:
                    strong_sources.add(url)
                high_value_evidence_count += 1
            if source_tier == "weak" and url:
                weak_sources.add(url)
            if len(doc.page_content.strip()) >= 80 and (source_tier == "high_authority" or authority_score >= 0.75):
                has_support = True
        if has_support:
            covered_sections += 1

    search_result_count = max(1, int(retrieval_metrics.get("search_result_count", 0)))
    authority_hits = int(retrieval_metrics.get("authority_hits", 0))
    weak_hits = int(retrieval_metrics.get("weak_source_hits", 0))
    fetch_attempt_rows = list(fetch_results or [])
    if fetch_attempt_rows:
        fetch_attempts = len(fetch_attempt_rows)
        blocked_fetches = sum(1 for item in fetch_attempt_rows if _is_blocked_fetch(item))
    else:
        blocked_fetches = int(retrieval_metrics.get("blocked_fetches", 0))
        fetch_attempts = max(1, int(retrieval_metrics.get("fetch_attempts", 0)))
    successful_authority_fetches = int(retrieval_metrics.get("successful_authority_fetches", 0))
    total_sections = max(1, len(section_ids))
    direct_answer_supported = sum(
        1
        for slot in evidence_slots.values()
        if slot.get("covered") and int(slot.get("high_authority_source_count") or 0) >= 1
    )
    blocked_by_provider = Counter()
    blocked_by_page_type = Counter()
    blocked_by_host = Counter()
    by_url: dict[str, list[dict[str, Any]]] = defaultdict(list)
    pdf_attempts = 0
    pdf_successes = 0
    visual_attempts = 0
    visual_successes = 0
    for item in fetch_attempt_rows:
        url = str(item.get("url") or item.get("final_url") or "")
        if url:
            by_url[url].append(item)
        provider = str(item.get("provider") or "")
        if provider == "pdf_parser":
            pdf_attempts += 1
            if str(item.get("status") or "") == "ok":
                pdf_successes += 1
        if provider == "visual_browse":
            visual_attempts += 1
            if str(item.get("status") or "") == "ok":
                visual_successes += 1
        if not _is_blocked_fetch(item):
            continue
        blocked_by_provider[provider or "unknown"] += 1
        blocked_by_page_type[str(item.get("page_type") or "unknown")] += 1
        blocked_by_host[str(item.get("host") or "unknown")] += 1

    blocked_after_jina_but_direct_ok = 0
    blocked_after_direct_http = 0
    for attempts in by_url.values():
        providers = [(str(item.get("provider") or ""), str(item.get("status") or ""), str(item.get("error_class") or "")) for item in attempts]
        if any(provider == "jina" and error in BLOCKED_ERROR_CLASSES for provider, _, error in providers) and any(
            provider == "direct_http" and status == "ok" for provider, status, _ in providers
        ):
            blocked_after_jina_but_direct_ok += 1
        if any(provider == "direct_http" and error in BLOCKED_ERROR_CLASSES for provider, _, error in providers):
            blocked_after_direct_http += 1
    return {
        "authority_source_rate": round(authority_hits / search_result_count, 4),
        "blocked_source_rate": round(blocked_fetches / fetch_attempts, 4),
        "high_value_evidence_count": high_value_evidence_count,
        "evidence_coverage_rate": round(covered_sections / total_sections, 4),
        "weak_source_hit_rate": round(weak_hits / search_result_count, 4),
        "successful_authority_fetch_rate": round(successful_authority_fetches / max(1, authority_hits), 4),
        "evidence_sections_total": total_sections,
        "evidence_sections_covered": covered_sections,
        "high_authority_source_count": len(strong_sources),
        "weak_source_count": len(weak_sources),
        "task_clause_coverage_rate": round(
            sum(1 for slot in evidence_slots.values() if slot.get("covered")) / max(1, len(evidence_slots)),
            4,
        ),
        "direct_answer_support_rate": round(direct_answer_supported / max(1, len(evidence_slots)), 4),
        "backfill_success_rate": round(backfill_successes / max(1, backfill_attempts), 4) if backfill_attempts else 0.0,
        "blocked_by_provider": dict(blocked_by_provider),
        "blocked_by_page_type": dict(blocked_by_page_type),
        "blocked_by_host": dict(blocked_by_host),
        "blocked_after_jina_but_direct_ok": blocked_after_jina_but_direct_ok,
        "blocked_after_direct_http": blocked_after_direct_http,
        "pdf_parser_salvage_rate": round(pdf_successes / max(1, pdf_attempts), 4) if pdf_attempts else 0.0,
        "visual_fallback_salvage_rate": round(visual_successes / max(1, visual_attempts), 4) if visual_attempts else 0.0,
    }


def evaluate_evidence_gate(
    *,
    coverage_summary: dict[str, float | int],
    evidence_slots: dict[str, EvidenceSlot],
) -> dict[str, Any]:
    missing_slot_ids = [slot_id for slot_id, slot in evidence_slots.items() if not slot.get("covered")]
    high_authority_sources = int(coverage_summary.get("high_authority_source_count", 0))
    direct_answer_support_rate = float(coverage_summary.get("direct_answer_support_rate", 0.0))
    passed = (
        not missing_slot_ids
        and high_authority_sources >= 2
        and direct_answer_support_rate >= 1.0
        and float(coverage_summary.get("evidence_coverage_rate", 0.0)) >= 1.0
    )
    return {
        "passed": passed,
        "missing_slot_ids": missing_slot_ids,
        "needs_backfill": not passed,
        "required_high_authority_sources": 2,
        "required_direct_answer_support_rate": 1.0,
    }
