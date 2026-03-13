from __future__ import annotations

import re
from urllib.parse import urlparse


SOCIAL_HOST_HINTS = (
    "reddit.com",
    "youtube.com",
    "youtu.be",
    "facebook.com",
    "instagram.com",
    "x.com",
    "twitter.com",
    "tiktok.com",
    "bilibili.com",
    "weibo.com",
    "linkedin.com",
)

AGGREGATOR_HOST_HINTS = (
    "zhihu.com",
    "sohu.com",
    "medium.com",
    "fandom.com",
    "wikia.com",
    "pinterest.com",
    "substack.com",
    "blogspot.",
    "wordpress.",
    "163.com",
)

AUTHORITY_HOST_HINTS = (
    "reuters.com",
    "apnews.com",
    "bloomberg.com",
    "ft.com",
    "wsj.com",
    "economist.com",
    "bbc.com",
    "sec.gov",
    "justice.gov",
    "oecd.org",
    "worldbank.org",
    "imf.org",
    "un.org",
    "who.int",
)

ACADEMIC_HOST_HINTS = (
    "arxiv.org",
    "nature.com",
    "science.org",
    "springer.com",
    "ieee.org",
    "acm.org",
    "doi.org",
)

IR_HINTS = (
    "investor",
    "annual report",
    "earnings",
    "10-k",
    "10q",
    "proxy statement",
    "press release",
)

TOPIC_HINTS: dict[str, tuple[str, ...]] = {
    "finance_business": (
        "finance",
        "business",
        "revenue",
        "earnings",
        "gross margin",
        "market share",
        "valuation",
        "cash flow",
        "资产",
        "财报",
        "营收",
        "利润",
        "公司",
        "中产",
    ),
    "crime_law": (
        "law",
        "crime",
        "court",
        "judge",
        "lawsuit",
        "legal",
        "regulation",
        "法规",
        "法院",
        "判决",
        "律师",
        "司法",
    ),
    "education_jobs": (
        "education",
        "school",
        "university",
        "job",
        "salary",
        "employment",
        "career",
        "教育",
        "大学",
        "就业",
        "薪资",
        "岗位",
    ),
}


def _host(url: str) -> str:
    try:
        return urlparse(url).netloc.lower()
    except Exception:
        return ""


def _path(url: str) -> str:
    try:
        return urlparse(url).path.lower()
    except Exception:
        return ""


def infer_topic_family(query: str) -> str:
    lowered = (query or "").lower()
    best_family = "general"
    best_score = 0
    for family, hints in TOPIC_HINTS.items():
        score = sum(1 for hint in hints if hint in lowered)
        if score > best_score:
            best_family = family
            best_score = score
    return best_family


def _host_matches(host: str, hints: tuple[str, ...]) -> bool:
    return any(hint in host for hint in hints)


def is_pdf_url(url: str, title: str = "") -> bool:
    lowered_title = (title or "").lower()
    return _path(url).endswith(".pdf") or lowered_title.endswith(".pdf") or "[pdf]" in lowered_title


def classify_source(
    url: str,
    *,
    title: str = "",
    snippet: str = "",
    query: str = "",
) -> dict[str, object]:
    host = _host(url)
    lowered_title = (title or "").lower()
    lowered_snippet = (snippet or "").lower()
    topic_family = infer_topic_family(query)
    is_social = _host_matches(host, SOCIAL_HOST_HINTS)
    is_aggregator = _host_matches(host, AGGREGATOR_HOST_HINTS)
    is_pdf = is_pdf_url(url, title)
    is_gov = host.endswith(".gov") or ".gov." in host or host.endswith(".gov.cn") or "court" in host
    is_edu = host.endswith(".edu") or host.endswith(".edu.cn") or ".ac." in host or _host_matches(host, ACADEMIC_HOST_HINTS)
    is_authority_media = _host_matches(host, AUTHORITY_HOST_HINTS)
    is_ir = any(hint in lowered_title or hint in lowered_snippet or hint in _path(url) for hint in IR_HINTS)

    authority_score = 0.35
    if is_gov:
        authority_score += 0.7
    if is_edu:
        authority_score += 0.55
    if is_authority_media:
        authority_score += 0.35
    if is_ir:
        authority_score += 0.45
    if is_pdf:
        authority_score += 0.2
    if is_social:
        authority_score -= 0.75
    if is_aggregator:
        authority_score -= 0.45

    if topic_family == "finance_business":
        if is_ir or is_gov or is_pdf:
            authority_score += 0.35
        if is_social or is_aggregator:
            authority_score -= 0.2
    elif topic_family == "crime_law":
        if is_gov or "law" in host or "court" in host:
            authority_score += 0.4
        if is_social or is_aggregator:
            authority_score -= 0.25
    elif topic_family == "education_jobs":
        if is_gov or is_edu:
            authority_score += 0.35
        if is_social or is_aggregator:
            authority_score -= 0.2

    authority_score = round(max(0.0, min(1.6, authority_score)), 4)
    source_tier = "high_authority" if authority_score >= 0.95 else ("standard" if authority_score >= 0.55 else "weak")
    return {
        "host": host,
        "topic_family": topic_family,
        "source_tier": source_tier,
        "authority_score": authority_score,
        "is_pdf": is_pdf,
        "is_social": is_social,
        "is_aggregator": is_aggregator,
        "is_authority_media": is_authority_media,
        "is_official": is_gov or is_edu or is_ir,
    }


def annotate_search_results(results: list[dict], query: str) -> list[dict]:
    annotated: list[dict] = []
    for item in results:
        meta = classify_source(
            str(item.get("url") or ""),
            title=str(item.get("title") or ""),
            snippet=str(item.get("content") or ""),
            query=query,
        )
        annotated.append({**item, **meta})
    return annotated


def rank_search_results(results: list[dict], query: str, *, limit: int | None = None) -> list[dict]:
    annotated = annotate_search_results(results, query)
    ranked = sorted(
        annotated,
        key=lambda item: (
            float(item.get("authority_score") or 0.0),
            1 if item.get("is_pdf") else 0,
            0 if item.get("is_social") else 1,
            0 if item.get("is_aggregator") else 1,
            len(str(item.get("content") or "")),
        ),
        reverse=True,
    )
    if limit is not None:
        return ranked[:limit]
    return ranked


def high_authority_only(results: list[dict]) -> list[dict]:
    return [item for item in results if str(item.get("source_tier")) == "high_authority"]

