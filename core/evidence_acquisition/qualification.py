from __future__ import annotations

from typing import Any, TypedDict

from core.source_policy import annotate_search_results, infer_topic_family


STRICT_AUTHORITY_TOPICS = {"finance_business", "crime_law", "education_jobs"}
OBJECT_STORAGE_HOSTS = {"s3.us-west-1.wasabisys.com"}


class SourceCandidate(TypedDict, total=False):
    url: str
    title: str
    content: str
    host: str
    source_tier: str
    authority_score: float
    is_official: bool
    is_social: bool
    is_aggregator: bool
    is_pdf: bool
    topic_family: str
    fit_score: float


def topic_requires_authority(topic_family: str) -> bool:
    return str(topic_family or "").strip().lower() in STRICT_AUTHORITY_TOPICS


def _fit_score(item: dict[str, Any]) -> float:
    score = float(item.get("authority_score") or 0.0)
    if item.get("is_official"):
        score += 0.3
    if item.get("is_pdf"):
        score += 0.15
    if item.get("is_social"):
        score -= 0.75
    if item.get("is_aggregator"):
        score -= 0.45
    score += min(0.15, len(str(item.get("content") or "")) / 6000.0)
    return round(score, 4)


def qualify_search_results(
    results: list[dict[str, Any]],
    query: str,
    *,
    limit: int | None = None,
) -> list[SourceCandidate]:
    annotated = annotate_search_results(results, query)
    qualified: list[SourceCandidate] = []
    for item in annotated:
        url = str(item.get("url") or "").strip()
        if not url:
            continue
        candidate: SourceCandidate = {
            "url": url,
            "title": str(item.get("title") or ""),
            "content": str(item.get("content") or ""),
            "host": str(item.get("host") or ""),
            "source_tier": str(item.get("source_tier") or "standard"),
            "authority_score": float(item.get("authority_score") or 0.0),
            "is_official": bool(item.get("is_official")),
            "is_social": bool(item.get("is_social")),
            "is_aggregator": bool(item.get("is_aggregator")),
            "is_pdf": bool(item.get("is_pdf")),
            "topic_family": str(item.get("topic_family") or infer_topic_family(query)),
        }
        candidate["fit_score"] = _fit_score(candidate)
        qualified.append(candidate)
    qualified.sort(
        key=lambda item: (
            2 if item.get("source_tier") == "high_authority" else (1 if item.get("source_tier") == "standard" else 0),
            float(item.get("fit_score") or 0.0),
            len(str(item.get("content") or "")),
        ),
        reverse=True,
    )
    return qualified[:limit] if limit is not None else qualified


def merge_candidates(*groups: list[SourceCandidate]) -> list[SourceCandidate]:
    merged: dict[str, SourceCandidate] = {}
    for group in groups:
        for item in group:
            url = str(item.get("url") or "")
            if not url:
                continue
            existing = merged.get(url)
            if existing is None or float(item.get("fit_score") or 0.0) > float(existing.get("fit_score") or 0.0):
                merged[url] = item
    return sorted(
        merged.values(),
        key=lambda item: (
            2 if item.get("source_tier") == "high_authority" else (1 if item.get("source_tier") == "standard" else 0),
            float(item.get("fit_score") or 0.0),
        ),
        reverse=True,
    )


def candidate_summary(candidates: list[SourceCandidate]) -> dict[str, int]:
    return {
        "total": len(candidates),
        "high_authority": sum(1 for item in candidates if item.get("source_tier") == "high_authority"),
        "standard": sum(1 for item in candidates if item.get("source_tier") == "standard"),
        "weak": sum(1 for item in candidates if item.get("source_tier") == "weak"),
    }


def admit_source_candidates(
    candidates: list[SourceCandidate],
    *,
    strict_topic: bool,
    max_main: int,
) -> list[SourceCandidate]:
    high_authority = [
        item
        for item in candidates
        if item.get("source_tier") == "high_authority" and str(item.get("host") or "") not in OBJECT_STORAGE_HOSTS
    ]
    standard = [
        item
        for item in candidates
        if item.get("source_tier") == "standard" and not item.get("is_social") and not item.get("is_aggregator")
        and str(item.get("host") or "") not in OBJECT_STORAGE_HOSTS
    ]
    weak_fallback = [
        item
        for item in candidates
        if item.get("source_tier") == "weak" and not item.get("is_social") and not item.get("is_aggregator")
    ]

    if strict_topic:
        admitted = high_authority[:max_main]
    else:
        admitted = (high_authority + standard)[:max_main]
        if not admitted:
            admitted = weak_fallback[:max_main]

    return admitted


def needs_authority_recall(candidates: list[SourceCandidate], *, strict_topic: bool) -> bool:
    summary = candidate_summary(candidates)
    if strict_topic:
        return summary["high_authority"] < 2
    return summary["high_authority"] < 1
