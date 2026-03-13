from __future__ import annotations

from typing import Any, Awaitable, Callable, TypedDict

from core.source_policy import infer_topic_family

from .qualification import (
    SourceCandidate,
    admit_source_candidates,
    merge_candidates,
    needs_authority_recall,
    qualify_search_results,
    topic_requires_authority,
)


class StagedRecallResult(TypedDict):
    candidates: list[SourceCandidate]
    all_candidates: list[SourceCandidate]
    high_value_sources: list[str]
    topic_family: str
    backfill_attempts: int
    search_queries: list[str]
    strict_topic: bool


SearchFn = Callable[[str, int], Awaitable[list[dict[str, Any]]]]


def build_authority_queries(task_desc: str) -> list[str]:
    family = infer_topic_family(task_desc)
    if family == "finance_business":
        return [
            f"{task_desc} official report pdf",
            f"{task_desc} investor relations annual report",
        ]
    if family == "crime_law":
        return [
            f"{task_desc} official court ruling report",
            f"{task_desc} site:gov legal report pdf",
        ]
    if family == "education_jobs":
        return [
            f"{task_desc} site:edu official report",
            f"{task_desc} site:gov education employment report pdf",
        ]
    return [
        f"{task_desc} official report pdf",
        f"{task_desc} authoritative source analysis",
    ]


async def staged_candidate_recall(
    *,
    query: str,
    task_desc: str,
    max_results: int,
    search_fn: SearchFn,
) -> StagedRecallResult:
    topic_family = infer_topic_family(task_desc or query)
    strict_topic = topic_requires_authority(topic_family)
    authority_queries = build_authority_queries(task_desc or query)
    search_queries: list[str] = []
    backfill_attempts = 0

    primary_query = authority_queries[0] if strict_topic else query
    search_queries.append(primary_query)
    primary_results = await search_fn(primary_query, max_results)
    all_candidates = qualify_search_results(primary_results, task_desc or query, limit=max_results * 2)

    admitted = admit_source_candidates(all_candidates, strict_topic=strict_topic, max_main=max_results)
    if needs_authority_recall(admitted, strict_topic=strict_topic):
        for authority_query in authority_queries:
            if authority_query in search_queries:
                continue
            search_queries.append(authority_query)
            authority_results = await search_fn(authority_query, max_results)
            qualified = qualify_search_results(authority_results, task_desc or query, limit=max_results * 2)
            all_candidates = merge_candidates(all_candidates, qualified)
            admitted = admit_source_candidates(all_candidates, strict_topic=strict_topic, max_main=max_results)
            backfill_attempts += 1
            break

    high_value_sources = [
        str(item.get("url") or "")
        for item in admitted
        if item.get("source_tier") == "high_authority" and str(item.get("url") or "")
    ]
    return {
        "candidates": admitted,
        "all_candidates": all_candidates,
        "high_value_sources": high_value_sources,
        "topic_family": topic_family,
        "backfill_attempts": backfill_attempts,
        "search_queries": search_queries,
        "strict_topic": strict_topic,
    }
