import asyncio
from collections import Counter
import datetime
import json
from urllib.parse import urlparse
from typing import Any, List, TypedDict

from langchain_core.messages import HumanMessage
from langgraph.graph import END, StateGraph

from core.checkpoint import get_sqlite_checkpointer
from core.config import CHECKPOINT_DB_PATH, DEFAULT_RESEARCH_MODE
from core.evidence_acquisition import (
    build_access_backfill_query,
    build_evidence_slots,
    compute_coverage_summary,
    evaluate_evidence_gate,
    fetch_source_candidate,
    qualify_search_results,
    rank_access_backfill_candidates,
    staged_candidate_recall,
    should_force_access_backfill,
    should_force_non_pdf_access_backfill,
    should_prefer_non_pdf_alternative,
    should_quarantine_pdf_host,
)
from core.memory import get_current_km, get_current_session_id
from core.models import llm_fast
from core.runtime_control import crash_process, should_interrupt_task
from core.source_policy import infer_topic_family, rank_search_results
from core.tools import (
    LLMFormatError,
    ToolExecutionError,
    clean_json_output,
    default_cost_breakdown,
    default_retrieval_metrics,
    fetch_url_with_pipeline,
    record_serper_query,
    record_tavily_credits,
    scrape_jina_ai,
    serper_client,
    tavily_crawl_client,
    tavily_crawl_credits,
    tavily_extract_client,
    tavily_extract_credits,
    tavily_map_client,
    tavily_map_credits,
    tavily_search_client,
    tavily_search_credits,
    visual_browse,
)
from core.writer_graph import get_writer_thread_id, resolve_writer_context_mode, writer_app
from gateway.state_store import get_task, save_knowledge_snapshot, upsert_task


class ResearchState(TypedDict):
    query: str
    research_mode: str
    plan: List[dict]
    outline: List[dict]
    task_contract: dict
    evidence_slots: dict
    draft_audit: dict
    user_feedback: str
    iteration: int
    final_report: str
    metrics: dict
    task_id: str
    history: List[dict]
    conflict_detected: bool
    conflict_count: int
    missing_sources: List[dict]
    degraded_items: List[dict]
    cost_breakdown: dict
    retrieval_metrics: dict
    source_candidates: List[dict]
    fetch_results: List[dict]
    coverage_summary: dict
    backfill_attempts: int
    retrieval_failed: bool


TRAJECTORY_FILE = "trajectory_log.jsonl"


def log_trajectory(task_id: str | None, event_type: str, data: dict) -> None:
    if not task_id:
        return
    record = {
        "timestamp": datetime.datetime.now().isoformat(),
        "task_id": str(task_id),
        "event": event_type,
        "data": data,
    }
    try:
        with open(TRAJECTORY_FILE, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass


async def safe_ainvoke(llm, prompt: str, max_retries: int = 3):
    for attempt in range(max_retries):
        try:
            return await llm.ainvoke([HumanMessage(content=prompt)])
        except Exception as exc:
            if attempt == max_retries - 1:
                print(f"[Graph] LLM call failed after {max_retries} attempts: {exc}")
                return None
            await asyncio.sleep(2**attempt)


def _fallback_outline(query: str) -> list[dict[str, str]]:
    return [
        {"id": "1", "title": "背景概览", "description": f"介绍 {query} 的背景与范围"},
        {"id": "2", "title": "关键发现", "description": f"梳理 {query} 的核心事实与结论"},
    ]


def _fallback_plan(query: str, outline: list[dict[str, str]]) -> list[dict[str, str]]:
    plan = []
    for section in outline:
        plan.append(
            {
                "task": f"{query} {section['title']}",
                "reason": section["description"],
                "section_id": section["id"],
            }
        )
    return plan


def _extract_comparison_targets(query: str) -> list[str]:
    normalized = " ".join((query or "").replace(" vs. ", " vs ").replace(" versus ", " vs ").split())
    match = __import__("re").search(r"(.+?)\s+vs\s+(.+)", normalized, __import__("re").IGNORECASE)
    if not match:
        return []
    return [match.group(1).strip(), match.group(2).strip()]


def _extract_required_constraints(query: str) -> list[str]:
    lowered = (query or "").lower()
    constraints: list[str] = []
    for keyword in ("latest", "current", "today", "2024", "2025", "2026", "china", "us", "global"):
        if keyword in lowered:
            constraints.append(keyword)
    return constraints


def _fallback_task_contract(query: str, outline: list[dict[str, Any]], plan: list[dict[str, Any]]) -> dict[str, Any]:
    must_answer_points: list[dict[str, Any]] = []
    for index, task in enumerate(plan[:5], start=1):
        section_id = str(task.get("section_id") or outline[min(index - 1, len(outline) - 1)].get("id") if outline else index)
        must_answer_points.append(
            {
                "id": str(index),
                "section_id": section_id,
                "question": str(task.get("task") or task.get("reason") or query),
            }
        )
    comparison_targets = _extract_comparison_targets(query)
    required_analysis_modes = ["risk", "causal"]
    if comparison_targets:
        required_analysis_modes.insert(0, "comparison")
    return {
        "direct_question": query,
        "must_answer_points": must_answer_points,
        "comparison_targets": comparison_targets,
        "required_constraints": _extract_required_constraints(query),
        "required_analysis_modes": list(dict.fromkeys(required_analysis_modes)),
    }


def _normalize_task_contract(
    task_contract: dict[str, Any] | None,
    *,
    query: str,
    outline: list[dict[str, Any]],
    plan: list[dict[str, Any]],
) -> dict[str, Any]:
    base = _fallback_task_contract(query, outline, plan)
    incoming = dict(task_contract or {})
    must_answer_points: list[dict[str, Any]] = []
    for index, point in enumerate(incoming.get("must_answer_points") or [], start=1):
        if isinstance(point, dict):
            must_answer_points.append(
                {
                    "id": str(point.get("id") or index),
                    "section_id": str(point.get("section_id") or point.get("id") or base["must_answer_points"][min(index - 1, len(base["must_answer_points"]) - 1)]["section_id"]),
                    "question": str(point.get("question") or point.get("title") or ""),
                }
            )
        elif str(point).strip():
            must_answer_points.append(
                {
                    "id": str(index),
                    "section_id": base["must_answer_points"][min(index - 1, len(base["must_answer_points"]) - 1)]["section_id"],
                    "question": str(point).strip(),
                }
            )
    if len(must_answer_points) < 2:
        must_answer_points = base["must_answer_points"]
    must_answer_points = must_answer_points[:5]
    comparison_targets = [str(item).strip() for item in (incoming.get("comparison_targets") or base["comparison_targets"]) if str(item).strip()]
    required_constraints = [str(item).strip() for item in (incoming.get("required_constraints") or base["required_constraints"]) if str(item).strip()]
    required_analysis_modes = [str(item).strip().lower() for item in (incoming.get("required_analysis_modes") or base["required_analysis_modes"]) if str(item).strip()]
    if comparison_targets and "comparison" not in required_analysis_modes:
        required_analysis_modes.insert(0, "comparison")
    for default_mode in ("causal", "risk"):
        if default_mode not in required_analysis_modes:
            required_analysis_modes.append(default_mode)
    return {
        "direct_question": str(incoming.get("direct_question") or query).strip(),
        "must_answer_points": must_answer_points,
        "comparison_targets": comparison_targets,
        "required_constraints": required_constraints,
        "required_analysis_modes": list(dict.fromkeys(required_analysis_modes)),
    }


def _research_mode(state: ResearchState) -> str:
    return (state.get("research_mode") or DEFAULT_RESEARCH_MODE).strip().lower()


def _merge_metrics(base: dict[str, int], updates: dict[str, int]) -> dict[str, int]:
    merged = dict(base)
    for key, value in updates.items():
        merged[key] = int(merged.get(key, 0)) + int(value)
    return merged


def _compact_text(content: str, max_chars: int = 4000) -> str:
    compact = (content or "").strip()
    if len(compact) > max_chars:
        compact = compact[:max_chars]
    return compact


def _result_host(url: str) -> str:
    try:
        return urlparse(url).netloc.lower()
    except Exception:
        return ""


BLOCKED_FETCH_ERROR_CLASSES = {
    "http_401_403",
    "http_412",
    "http_429",
    "ssl_error",
    "redirect_loop",
    "js_only",
    "pdf_unreadable",
}


def _is_blocked_fetch_error(error_class: str) -> bool:
    return str(error_class or "") in BLOCKED_FETCH_ERROR_CLASSES


async def _mode_presearch(
    mode: str,
    query: str,
    *,
    cost_breakdown: dict[str, float | int],
    retrieval_metrics: dict[str, int],
) -> tuple[list[dict[str, Any]], dict[str, float | int], dict[str, int]]:
    if mode == "high":
        response = await tavily_search_client.asearch(query=query, max_results=3, search_depth="advanced")
        cost_breakdown = record_tavily_credits(cost_breakdown, tavily_search_credits("advanced"))
    else:
        response = await serper_client.asearch(query=query, max_results=3, search_depth="basic")
        cost_breakdown = record_serper_query(cost_breakdown, 1)
    retrieval_metrics = _merge_metrics(retrieval_metrics, {"search_calls": 1})
    ranked_results = rank_search_results(response.get("results", []), query, limit=3)
    retrieval_metrics = _update_search_result_metrics(retrieval_metrics, ranked_results)
    return ranked_results, cost_breakdown, retrieval_metrics


async def _mode_search(
    mode: str,
    query: str,
    *,
    max_results: int,
    cost_breakdown: dict[str, float | int],
    retrieval_metrics: dict[str, int],
) -> tuple[list[dict[str, Any]], dict[str, float | int], dict[str, int]]:
    if mode == "high":
        response = await tavily_search_client.asearch(query=query, max_results=max_results, search_depth="advanced")
        cost_breakdown = record_tavily_credits(cost_breakdown, tavily_search_credits("advanced"))
    else:
        response = await serper_client.asearch(query=query, max_results=max_results, search_depth="basic")
        cost_breakdown = record_serper_query(cost_breakdown, 1)
    retrieval_metrics = _merge_metrics(retrieval_metrics, {"search_calls": 1})
    ranked_results = rank_search_results(response.get("results", []), query, limit=max_results)
    retrieval_metrics = _update_search_result_metrics(retrieval_metrics, ranked_results)
    return ranked_results, cost_breakdown, retrieval_metrics


def _select_primary_domain(results: list[dict[str, Any]]) -> tuple[str, str]:
    hosts = [_result_host(item.get("url", "")) for item in results if item.get("url")]
    host_counts = Counter(host for host in hosts if host)
    for host, count in host_counts.most_common():
        if count >= 2:
            for item in results:
                if _result_host(item.get("url", "")) == host:
                    return host, item.get("url", "")
    return "", ""


def _choose_high_value_urls(
    search_results: list[dict[str, Any]],
    mapped_urls: list[str],
    crawled_results: list[dict[str, Any]],
    *,
    limit: int = 5,
) -> list[str]:
    ordered_urls: list[str] = []
    for item in search_results:
        url = item.get("url", "")
        if url and url not in ordered_urls:
            ordered_urls.append(url)
    for url in mapped_urls:
        if url and url not in ordered_urls:
            ordered_urls.append(url)
    for item in crawled_results:
        url = item.get("url", "")
        if url and url not in ordered_urls:
            ordered_urls.append(url)
    return ordered_urls[:limit]


def _update_search_result_metrics(
    retrieval_metrics: dict[str, int],
    ranked_results: list[dict[str, Any]],
) -> dict[str, int]:
    updates = {
        "search_result_count": len(ranked_results),
        "authority_hits": sum(1 for item in ranked_results if item.get("source_tier") == "high_authority"),
        "weak_source_hits": sum(1 for item in ranked_results if item.get("source_tier") == "weak"),
    }
    return _merge_metrics(retrieval_metrics, updates)


def _build_backfill_queries(task_desc: str) -> list[str]:
    family = infer_topic_family(task_desc)
    if family == "finance_business":
        return [
            f"{task_desc} official report pdf",
            f"{task_desc} investor relations annual report",
        ]
    if family == "crime_law":
        return [
            f"{task_desc} official court ruling report",
            f"{task_desc} site:gov report pdf",
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


def _compute_evidence_coverage(
    *,
    plan: list[dict[str, Any]],
    km,
    retrieval_metrics: dict[str, int],
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
                strong_sources.add(url)
                high_value_evidence_count += 1
            if source_tier == "weak":
                weak_sources.add(url)
            if len(doc.page_content.strip()) >= 80 and (source_tier == "high_authority" or authority_score >= 0.75):
                has_support = True
        if has_support:
            covered_sections += 1
    search_result_count = max(1, int(retrieval_metrics.get("search_result_count", 0)))
    authority_hits = int(retrieval_metrics.get("authority_hits", 0))
    weak_hits = int(retrieval_metrics.get("weak_source_hits", 0))
    blocked_fetches = int(retrieval_metrics.get("blocked_fetches", 0))
    fetch_attempts = max(1, int(retrieval_metrics.get("fetch_attempts", 0)))
    successful_authority_fetches = int(retrieval_metrics.get("successful_authority_fetches", 0))
    total_sections = max(1, len(section_ids))
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
    }


def _build_evidence_slots(
    *,
    task_contract: dict[str, Any],
    km,
) -> dict[str, dict[str, Any]]:
    slots: dict[str, dict[str, Any]] = {}
    for point in task_contract.get("must_answer_points") or []:
        point_id = str(point.get("id") or len(slots) + 1)
        section_id = str(point.get("section_id") or "global")
        docs = km.retrieve(section_id=section_id, k=12)
        direct_facts = 0
        high_authority_sources: set[str] = set()
        any_non_weak = False
        evidence_urls: list[str] = []
        for doc in docs:
            source_tier = str(doc.metadata.get("source_tier") or "")
            authority_score = float(doc.metadata.get("authority_score") or 0.0)
            url = str(doc.metadata.get("url") or doc.metadata.get("source_url") or "")
            if len(doc.page_content.strip()) >= 80:
                direct_facts += 1
            if source_tier != "weak":
                any_non_weak = True
            if source_tier == "high_authority" or authority_score >= 0.95:
                if url:
                    high_authority_sources.add(url)
            if url and url not in evidence_urls:
                evidence_urls.append(url)
        slots[point_id] = {
            "question": str(point.get("question") or ""),
            "section_id": section_id,
            "fact_count": direct_facts,
            "source_urls": evidence_urls[:5],
            "high_authority_source_count": len(high_authority_sources),
            "covered": direct_facts >= 1 and any_non_weak,
        }
    return slots


def _needs_strict_evidence_gate(task_id: str | None) -> bool:
    task = get_task(task_id or "") or {}
    return str(task.get("backend") or "") == "drb_public_benchmark"


async def node_init_search(state: ResearchState):
    query = state["query"]
    mode = _research_mode(state)
    task_id = state.get("task_id") or get_current_session_id()
    km = get_current_km()
    await km.aclear()
    cost_breakdown = dict(state.get("cost_breakdown") or default_cost_breakdown())
    retrieval_metrics = dict(state.get("retrieval_metrics") or default_retrieval_metrics())

    context = ""
    presearch_results = []
    for attempt in range(3):
        try:
            presearch_results, cost_breakdown, retrieval_metrics = await _mode_presearch(
                mode,
                query,
                cost_breakdown=cost_breakdown,
                retrieval_metrics=retrieval_metrics,
            )
            for item in presearch_results:
                km.add_compact_document(item.get("content", ""), item.get("url", ""), item.get("title", ""))
            context = "\n".join(
                f"- {item.get('title', '')}: {item.get('content', '')}"
                for item in presearch_results
            )
            break
        except ToolExecutionError as exc:
            log_trajectory(task_id, "presearch_error", {"query": query, "error": str(exc)})
            await asyncio.sleep(2**attempt)
        except Exception as exc:
            log_trajectory(task_id, "presearch_error", {"query": query, "error": str(exc)})
            await asyncio.sleep(2**attempt)

    prompt = f"""
# Context
User query: {query}
Current environment:
{context[:2000]}

# Objective
Return strict JSON with:
{{
  "outline": [{{"id": "1", "title": "Section", "description": "What to cover"}}],
  "search_tasks": [{{"task": "query", "reason": "why", "section_id": "1"}}],
  "task_contract": {{
    "direct_question": "the exact user question to answer",
    "must_answer_points": [
      {{"id": "1", "section_id": "1", "question": "specific, checkable sub-question"}}
    ],
    "comparison_targets": ["optional entity A", "optional entity B"],
    "required_constraints": ["time, region, or scope constraints"],
    "required_analysis_modes": ["comparison", "causal", "risk"]
  }}
}}
"""
    resp = await safe_ainvoke(llm_fast, prompt)
    plan_data: Any = {}
    if resp:
        try:
            plan_data = clean_json_output(resp.content, strict=True)
        except LLMFormatError as exc:
            log_trajectory(
                task_id,
                "planner_json_fallback",
                {"query": query, "error": exc.parse_error, "raw": exc.raw_text[:300]},
            )

    outline = []
    plan = []
    task_contract: dict[str, Any] = {}
    if isinstance(plan_data, dict):
        outline = plan_data.get("outline", [])
        plan = plan_data.get("search_tasks", [])
        task_contract = plan_data.get("task_contract", {})

    if not isinstance(outline, list) or not outline:
        outline = _fallback_outline(query)
    if not isinstance(plan, list) or not plan:
        plan = _fallback_plan(query, outline)
    task_contract = _normalize_task_contract(task_contract, query=query, outline=outline, plan=plan)

    metrics = state.get("metrics", {"tool_calls": 0, "backtracking": 0})
    if state.get("user_feedback"):
        metrics["backtracking"] = metrics.get("backtracking", 0) + 1

    history = state.get("history", [])
    history.append(
        {
            "role": "planner_cot",
            "content": {"query": query, "outline": outline, "search_tasks": plan, "task_contract": task_contract},
        }
    )
    log_trajectory(task_id, "planner", {"outline": outline, "search_tasks": plan, "task_contract": task_contract})

    return {
        "plan": plan,
        "outline": outline,
        "task_contract": task_contract,
        "evidence_slots": state.get("evidence_slots", {}),
        "draft_audit": state.get("draft_audit", {}),
        "iteration": state.get("iteration", 0) + 1,
        "metrics": metrics,
        "history": history,
        "conflict_detected": False,
        "missing_sources": state.get("missing_sources", []),
        "degraded_items": state.get("degraded_items", []),
        "research_mode": mode,
        "cost_breakdown": cost_breakdown,
        "retrieval_metrics": retrieval_metrics,
    }


async def node_human_feedback(state: ResearchState):
    if __import__("os").environ.get("FACTWEAVER_API_MODE") == "1":
        return {"user_feedback": ""}

    print("\n[Human Review] Outline:")
    for section in state["outline"]:
        print(f"- [{section.get('id')}] {section.get('title')}")
    print("\n[Human Review] Search tasks:")
    for index, item in enumerate(state["plan"], start=1):
        print(f"- [{index}] {item.get('task')}")

    user_input = input("\nEnter to continue, text to revise, q to quit: ").strip()
    if user_input.lower() == "q":
        return {"final_report": "User Terminated"}
    if user_input:
        return {"user_feedback": user_input}
    return {"user_feedback": ""}


async def node_deep_research(state: ResearchState):
    km = get_current_km()
    task_id = state.get("task_id") or get_current_session_id()
    mode = _research_mode(state)
    task_contract = dict(state.get("task_contract") or _fallback_task_contract(state["query"], state.get("outline", []), state.get("plan", [])))
    history = list(state.get("history", []))
    missing_sources = list(state.get("missing_sources", []))
    degraded_items = list(state.get("degraded_items", []))
    cost_breakdown = dict(state.get("cost_breakdown") or default_cost_breakdown())
    retrieval_metrics = dict(state.get("retrieval_metrics") or default_retrieval_metrics())
    source_candidates = list(state.get("source_candidates") or [])
    fetch_results = list(state.get("fetch_results") or [])
    backfill_attempts_total = int(state.get("backfill_attempts") or 0)
    pdf_host_quarantine: set[str] = set()
    same_host_access_backfill_hosts: set[str] = set()
    strict_evidence_gate = _needs_strict_evidence_gate(task_id)

    async def process_task(task_item: dict[str, Any]) -> dict[str, Any]:
        nonlocal cost_breakdown, retrieval_metrics, source_candidates, fetch_results, backfill_attempts_total

        local_logs: list[dict[str, Any]] = []
        local_missing: list[dict[str, Any]] = []
        local_degraded: list[dict[str, Any]] = []
        task_desc = task_item.get("task", state["query"])
        section_id = task_item.get("section_id", "global")

        query_prompt = f"""
Return strict JSON only:
{{"queries": ["search keywords"]}}

Task: {task_desc}
"""
        queries = [task_desc]
        resp = await safe_ainvoke(llm_fast, query_prompt)
        if resp:
            try:
                parsed = clean_json_output(resp.content, strict=True)
                if isinstance(parsed, dict) and isinstance(parsed.get("queries"), list) and parsed["queries"]:
                    queries = parsed["queries"][:3]
            except LLMFormatError as exc:
                local_degraded.append(
                    {
                        "task": task_desc,
                        "stage": "query_generation",
                        "reason": f"JSON repair fallback: {exc.parse_error}",
                    }
                )

        local_logs.append(
            {"role": "query_gen", "content": {"task_desc": task_desc, "generated_queries": queries}}
        )

        search_seed = next((str(query).strip() for query in queries if str(query).strip()), task_desc)
        max_results = 5 if mode == "high" else 4

        async def search_once(search_query: str, max_results: int) -> list[dict[str, Any]]:
            nonlocal cost_breakdown, retrieval_metrics
            results, cost_breakdown, retrieval_metrics = await _mode_search(
                mode,
                search_query,
                max_results=max_results,
                cost_breakdown=cost_breakdown,
                retrieval_metrics=retrieval_metrics,
            )
            return results

        def record_fetch_attempts(item: dict[str, Any], fetched: dict[str, Any], *, stage: str) -> tuple[int, int]:
            attempts = list(fetched.get("attempts") or [])
            if not attempts:
                attempts = [
                    {
                        "provider": fetched.get("provider") or stage,
                        "status": fetched.get("status") or "failed",
                        "page_type": fetched.get("page_type") or "",
                        "host": fetched.get("host") or item.get("host") or _result_host(str(item.get("url") or "")),
                        "error_class": fetched.get("error_class") or "",
                        "http_status": int(fetched.get("http_status") or 0),
                        "content_length": int(fetched.get("content_length") or 0),
                        "authority_preserved": bool(fetched.get("authority_preserved")),
                        "attempt_order": 1,
                        "salvaged_by_fallback": bool(fetched.get("salvaged_by_fallback")),
                        "blocked_stage": fetched.get("blocked_stage") or "",
                        "final_url": fetched.get("final_url") or item.get("url") or "",
                    }
                ]
            for attempt in attempts:
                fetch_results.append(
                    {
                        "task": task_desc,
                        "url": item.get("url", ""),
                        "provider": attempt.get("provider") or stage,
                        "status": attempt.get("status") or "failed",
                        "page_type": attempt.get("page_type") or fetched.get("page_type") or "",
                        "host": attempt.get("host") or item.get("host") or _result_host(str(item.get("url") or "")),
                        "error_class": attempt.get("error_class") or "",
                        "http_status": int(attempt.get("http_status") or 0),
                        "content_type": attempt.get("content_type") or "",
                        "content_length": int(attempt.get("content_length") or 0),
                        "authority_preserved": bool(attempt.get("authority_preserved")),
                        "source_tier": item.get("source_tier", "standard"),
                        "attempt_order": int(attempt.get("attempt_order") or 0),
                        "salvaged_by_fallback": bool(attempt.get("salvaged_by_fallback")),
                        "blocked_stage": attempt.get("blocked_stage") or "",
                        "final_url": attempt.get("final_url") or fetched.get("final_url") or item.get("url") or "",
                    }
                )
            blocked_count = sum(
                1 for attempt in attempts if _is_blocked_fetch_error(str(attempt.get("error_class") or ""))
            )
            return len(attempts), blocked_count

        async def access_backfill(
            item: dict[str, Any],
            *,
            stage: str,
        ) -> bool:
            nonlocal backfill_attempts_total, retrieval_metrics
            host = str(item.get("host") or _result_host(str(item.get("url") or ""))).strip().lower()
            if not host:
                return False
            backfill_attempts_total += 1
            same_host_access_backfill_hosts.add(host)
            retrieval_metrics = _merge_metrics(retrieval_metrics, {"same_host_backfill_attempts": 1})
            query = build_access_backfill_query(item, task_desc)
            try:
                same_host_results = await search_once(query, 4)
            except Exception as exc:
                local_degraded.append(
                    {
                        "task": task_desc,
                        "stage": "access_backfill",
                        "reason": f"same-host recall failed for {host}: {exc}",
                    }
                )
                return False
            qualified_same_host = qualify_search_results(same_host_results, task_desc, limit=4)
            alternatives = rank_access_backfill_candidates(item, qualified_same_host)[:2]
            if not alternatives:
                local_degraded.append(
                    {
                        "task": task_desc,
                        "stage": "access_backfill",
                        "reason": f"no same-host fallback candidate for {host} via query={query}",
                    }
                )
                return False
            for candidate in alternatives:
                success = await ingest_candidate(
                    candidate,
                    allow_visual=bool(candidate.get("source_tier") == "high_authority"),
                    stage=f"{stage}_access_backfill",
                    allow_access_backfill=False,
                )
                if success:
                    local_logs.append(
                        {
                            "role": "access_backfill",
                            "content": {
                                "task": task_desc,
                                "host": host,
                                "url": candidate.get("url", ""),
                            },
                        }
                    )
                    retrieval_metrics = _merge_metrics(retrieval_metrics, {"same_host_backfill_successes": 1})
                    return True
            retrieval_metrics = _merge_metrics(retrieval_metrics, {"blocked_after_same_host_backfill": 1})
            return False

        async def ingest_candidate(
            item: dict[str, Any],
            *,
            allow_visual: bool,
            stage: str,
            count_as_fallback: bool = True,
            allow_access_backfill: bool = True,
        ) -> bool:
            nonlocal retrieval_metrics, fetch_results, cost_breakdown

            url = item.get("url", "")
            title = item.get("title", "")
            host = str(item.get("host") or _result_host(str(url))).strip().lower()
            if not url or km.is_duplicate(url):
                return False

            if allow_access_backfill and should_force_access_backfill(item, quarantined_pdf_hosts=pdf_host_quarantine):
                if await access_backfill(item, stage=f"{stage}_pdf_access_backfill"):
                    return True
                local_missing.append(
                    {
                        "task": task_desc,
                        "url": url,
                        "reason": "pdf_access_backfill_failed",
                        "provider": "access_backfill",
                        "final_url": url,
                    }
                )
                return False

            if count_as_fallback:
                retrieval_metrics = _merge_metrics(retrieval_metrics, {"fallback_count": 1})
            fetched = await fetch_source_candidate(
                item,
                allow_visual=allow_visual,
                goal=f"Extract the charts, tables, formulas and visual facts relevant to: {task_desc}",
            )
            if should_quarantine_pdf_host(item, fetched) and host:
                pdf_host_quarantine.add(host)
            attempt_count, blocked_count = record_fetch_attempts(item, fetched, stage=stage)
            retrieval_metrics = _merge_metrics(
                retrieval_metrics,
                {"fetch_attempts": attempt_count, "blocked_fetches": blocked_count},
            )
            if fetched.get("credits_est"):
                cost_breakdown = record_tavily_credits(cost_breakdown, float(fetched.get("credits_est") or 0.0))
                retrieval_metrics = _merge_metrics(retrieval_metrics, {"extract_calls": 1})
            if fetched.get("status") != "ok":
                forced_html_backfill = allow_access_backfill and should_force_non_pdf_access_backfill(
                    item,
                    fetched,
                    attempted_hosts=same_host_access_backfill_hosts,
                )
                generic_backfill_allowed = not (
                    host in same_host_access_backfill_hosts and not (str(url).lower().endswith(".pdf") or bool(item.get("is_pdf")))
                )
                if forced_html_backfill:
                    if await access_backfill(item, stage=f"{stage}_html_access_backfill"):
                        return True
                if (
                    allow_access_backfill
                    and not forced_html_backfill
                    and generic_backfill_allowed
                    and item.get("source_tier") == "high_authority"
                ):
                    if await access_backfill(item, stage=stage):
                        return True
                local_missing.append(
                    {
                        "task": task_desc,
                        "url": url,
                        "reason": fetched.get("error_class") or "fetch_failed",
                        "provider": fetched.get("provider") or "pipeline",
                        "final_url": fetched.get("final_url") or url,
                    }
                )
                return False

            if item.get("source_tier") == "high_authority":
                retrieval_metrics = _merge_metrics(retrieval_metrics, {"successful_authority_fetches": 1})

            compact = _compact_text(str(fetched.get("content") or ""))
            if not compact or len(compact) <= 120 or "Access Denied" in compact:
                local_missing.append(
                    {
                        "task": task_desc,
                        "url": url,
                        "reason": fetched.get("error_class") or "content too short or blocked",
                    }
                )
                return False

            extra_metadata = {
                "source_tier": item.get("source_tier", "standard"),
                "authority_score": float(item.get("authority_score") or 0.0),
                "is_pdf": bool(item.get("is_pdf")),
                "is_social": bool(item.get("is_social")),
                "is_aggregator": bool(item.get("is_aggregator")),
                "fetch_provider": fetched.get("provider") or stage,
                "fetch_status": fetched.get("status") or "ok",
                "fetch_error_class": fetched.get("error_class") or "",
                "final_url": fetched.get("final_url") or url,
                "content_length": int(fetched.get("content_length") or 0),
                "page_type": fetched.get("page_type") or "",
            }
            inserted = km.add_compact_document(
                compact,
                url,
                title,
                section_id=section_id,
                extra_metadata=extra_metadata,
            )
            if not inserted:
                return False

            retrieval_metrics = _merge_metrics(
                retrieval_metrics,
                {
                    "high_value_evidence_count": 1 if float(item.get("authority_score") or 0.0) >= 0.75 else 0,
                },
            )
            local_logs.append(
                {
                    "role": stage,
                    "content": {
                        "task": task_desc,
                        "title": title,
                        "url": url,
                        "source_tier": item.get("source_tier", "standard"),
                        "authority_score": item.get("authority_score", 0.0),
                        "provider": fetched.get("provider") or stage,
                    },
                }
            )
            return True

        scrape_with_fallback = ingest_candidate

        try:
            recall = await staged_candidate_recall(
                query=search_seed,
                task_desc=task_desc,
                max_results=max_results,
                search_fn=search_once,
            )
        except ToolExecutionError as exc:
            local_missing.append(
                {
                    "task": task_desc,
                    "query": search_seed,
                    "url": exc.url,
                    "reason": f"Search failed with HTTP {exc.status_code}",
                }
            )
            local_degraded.append(
                {"task": task_desc, "stage": "search", "reason": "deterministic skip after tool failure"}
            )
            return {"logs": local_logs, "missing_sources": local_missing, "degraded_items": local_degraded}
        except Exception as exc:
            local_missing.append({"task": task_desc, "query": search_seed, "reason": str(exc)})
            return {"logs": local_logs, "missing_sources": local_missing, "degraded_items": local_degraded}

        backfill_attempts_total += int(recall.get("backfill_attempts") or 0)
        all_candidates = list(recall.get("all_candidates") or [])
        admitted_candidates = list(recall.get("candidates") or [])
        if all_candidates:
            source_candidates.extend(
                [
                    {
                        "task": task_desc,
                        "query": search_seed,
                        "url": item.get("url", ""),
                        "source_tier": item.get("source_tier", "standard"),
                        "authority_score": float(item.get("authority_score") or 0.0),
                        "topic_family": item.get("topic_family", ""),
                    }
                    for item in all_candidates
                ]
            )

        local_logs.append(
            {
                "role": "search_tool",
                "content": {
                    "task": task_desc,
                    "query": search_seed,
                    "search_queries": list(recall.get("search_queries") or []),
                    "mode": mode,
                    "results": [
                        {
                            "url": item.get("url", ""),
                            "source_tier": item.get("source_tier", "standard"),
                            "authority_score": item.get("authority_score", 0.0),
                        }
                        for item in admitted_candidates
                    ],
                },
            }
        )

        if not admitted_candidates:
            local_missing.append({"task": task_desc, "query": search_seed, "reason": "no admitted candidates"})
            local_degraded.append(
                {
                    "task": task_desc,
                    "stage": "qualification",
                    "reason": f"no admissible sources for topic_family={recall.get('topic_family', 'general')}",
                }
            )
            return {"logs": local_logs, "missing_sources": local_missing, "degraded_items": local_degraded}

        if mode == "low":
            for item in admitted_candidates[:3]:
                await ingest_candidate(item, allow_visual=False, stage="low_authority_fetch", count_as_fallback=False)
            return {"logs": local_logs, "missing_sources": local_missing, "degraded_items": local_degraded}

        if mode == "medium":
            urls = [str(item.get("url") or "") for item in admitted_candidates if str(item.get("url") or "")]
            extracted_urls: set[str] = set()
            if urls and tavily_extract_client.is_configured():
                try:
                    extract_response = await tavily_extract_client.aextract(
                        urls,
                        query=task_desc,
                        chunks_per_source=3,
                        extract_depth="basic",
                    )
                    extract_results = extract_response.get("results", [])
                    if extract_results:
                        cost_breakdown = record_tavily_credits(
                            cost_breakdown,
                            tavily_extract_credits(len(extract_results), "basic"),
                        )
                        retrieval_metrics = _merge_metrics(retrieval_metrics, {"extract_calls": 1})
                    for extract_item in extract_results:
                        url = extract_item.get("url", "")
                        raw_content = extract_item.get("raw_content", "")
                        candidate = next((item for item in admitted_candidates if item.get("url") == url), None)
                        if not url or not raw_content or not candidate:
                            continue
                        inserted = km.add_extracted_chunks(
                            raw_content,
                            url,
                            extract_item.get("title", ""),
                            section_id=section_id,
                            provider="tavily_extract_basic",
                            extra_metadata={
                                "source_tier": candidate.get("source_tier", "standard"),
                                "authority_score": float(candidate.get("authority_score", 0.0)),
                            },
                        )
                        if inserted:
                            extracted_urls.add(url)
                            retrieval_metrics = _merge_metrics(
                                retrieval_metrics,
                                {"high_value_evidence_count": 1 if float(candidate.get("authority_score", 0.0)) >= 0.75 else 0},
                            )
                except ToolExecutionError as exc:
                    local_degraded.append(
                        {
                            "task": task_desc,
                            "query": search_seed,
                            "stage": "extract",
                            "reason": f"Tavily extract failed with HTTP {exc.status_code}",
                        }
                    )
            for item in admitted_candidates:
                if item.get("url") in extracted_urls:
                    continue
                await ingest_candidate(item, allow_visual=True, stage="medium_authority_fetch")
            return {"logs": local_logs, "missing_sources": local_missing, "degraded_items": local_degraded}

        search_results = list(all_candidates or admitted_candidates)
        primary_domain, primary_url = _select_primary_domain(search_results)
        mapped_urls: list[str] = []
        crawled_results: list[dict[str, Any]] = []
        if primary_domain and primary_url and tavily_map_client.is_configured():
            try:
                map_response = await tavily_map_client.amap(
                    primary_url,
                    limit=10,
                    max_depth=2,
                    max_breadth=4,
                    allow_external=False,
                )
                mapped_urls = [url for url in map_response.get("results", []) if isinstance(url, str)]
                if mapped_urls:
                    cost_breakdown = record_tavily_credits(cost_breakdown, tavily_map_credits(len(mapped_urls), False))
                    retrieval_metrics = _merge_metrics(retrieval_metrics, {"map_calls": 1})
            except ToolExecutionError as exc:
                local_degraded.append(
                    {
                        "task": task_desc,
                        "query": search_seed,
                        "stage": "map",
                        "reason": f"Tavily map failed with HTTP {exc.status_code}",
                    }
                )

        if len(mapped_urls) >= 3 and primary_url and tavily_crawl_client.is_configured():
            try:
                crawl_response = await tavily_crawl_client.acrawl(
                    primary_url,
                    limit=5,
                    max_depth=2,
                    max_breadth=4,
                    extract_depth="advanced",
                    allow_external=False,
                    include_images=False,
                )
                crawled_results = crawl_response.get("results", [])
                if crawled_results:
                    cost_breakdown = record_tavily_credits(
                        cost_breakdown,
                        tavily_crawl_credits(len(crawled_results), extract_depth="advanced"),
                    )
                    retrieval_metrics = _merge_metrics(retrieval_metrics, {"crawl_calls": 1})
            except ToolExecutionError as exc:
                local_degraded.append(
                    {
                        "task": task_desc,
                        "query": search_seed,
                        "stage": "crawl",
                        "reason": f"Tavily crawl failed with HTTP {exc.status_code}",
                    }
                )

        selected_urls = _choose_high_value_urls(search_results, mapped_urls, crawled_results, limit=5)
        search_lookup = {item.get("url", ""): item for item in search_results if item.get("url")}
        crawl_lookup = {item.get("url", ""): item for item in crawled_results if item.get("url")}
        extracted_urls: set[str] = set()
        if selected_urls and tavily_extract_client.is_configured():
            try:
                extract_response = await tavily_extract_client.aextract(
                    selected_urls,
                    query=task_desc,
                    chunks_per_source=5,
                    extract_depth="advanced",
                )
                extract_results = extract_response.get("results", [])
                if extract_results:
                    cost_breakdown = record_tavily_credits(
                        cost_breakdown,
                        tavily_extract_credits(len(extract_results), "advanced"),
                    )
                    retrieval_metrics = _merge_metrics(retrieval_metrics, {"extract_calls": 1})
                for extract_item in extract_results:
                    url = extract_item.get("url", "")
                    raw_content = extract_item.get("raw_content", "")
                    candidate = search_lookup.get(url, {})
                    title = extract_item.get("title", "") or candidate.get("title", "")
                    if not url or not raw_content:
                        continue
                    inserted = km.add_extracted_chunks(
                        raw_content,
                        url,
                        title,
                        section_id=section_id,
                        provider="tavily_extract_advanced",
                        extra_metadata={
                            "source_tier": candidate.get("source_tier", "standard"),
                            "authority_score": float(candidate.get("authority_score", 0.0)),
                        },
                    )
                    if inserted:
                        extracted_urls.add(url)
                        retrieval_metrics = _merge_metrics(
                            retrieval_metrics,
                            {"high_value_evidence_count": 1 if float(candidate.get("authority_score", 0.0)) >= 0.75 else 0},
                        )
            except ToolExecutionError as exc:
                local_degraded.append(
                    {
                        "task": task_desc,
                        "query": search_seed,
                        "stage": "extract",
                        "reason": f"Tavily extract failed with HTTP {exc.status_code}",
                    }
                )

        for url in selected_urls:
            if url in extracted_urls or km.is_duplicate(url):
                continue
            crawled_item = crawl_lookup.get(url)
            crawl_content = _compact_text((crawled_item or {}).get("raw_content", ""))
            candidate = search_lookup.get(url, {"url": url, "title": (crawled_item or {}).get("title", "")})
            title = candidate.get("title", "") or (crawled_item or {}).get("title", "")
            if crawl_content and len(crawl_content) > 120:
                inserted = km.add_compact_document(
                    crawl_content,
                    url,
                    title,
                    section_id=section_id,
                    extra_metadata={
                        "source_tier": candidate.get("source_tier", "standard"),
                        "authority_score": float(candidate.get("authority_score", 0.0)),
                        "fetch_provider": "tavily_crawl",
                    },
                )
                if inserted:
                    retrieval_metrics = _merge_metrics(
                        retrieval_metrics,
                        {"high_value_evidence_count": 1 if float(candidate.get("authority_score", 0.0)) >= 0.75 else 0},
                    )
                    continue
            await ingest_candidate(candidate, allow_visual=True, stage="high_authority_fetch")
        return {"logs": local_logs, "missing_sources": local_missing, "degraded_items": local_degraded}

        for query in queries[:3]:
            max_results = 5 if mode == "high" else 3
            try:
                search_results, cost_breakdown, retrieval_metrics = await _mode_search(
                    mode,
                    query,
                    max_results=max_results,
                    cost_breakdown=cost_breakdown,
                    retrieval_metrics=retrieval_metrics,
                )
            except ToolExecutionError as exc:
                local_missing.append(
                    {
                        "task": task_desc,
                        "query": query,
                        "url": exc.url,
                        "reason": f"Search failed with HTTP {exc.status_code}",
                    }
                )
                local_degraded.append(
                    {"task": task_desc, "stage": "search", "reason": "deterministic skip after tool failure"}
                )
                continue
            except Exception as exc:
                local_missing.append({"task": task_desc, "query": query, "reason": str(exc)})
                continue

            search_results = [item for item in search_results if item.get("url")]
            if not search_results:
                local_missing.append({"task": task_desc, "query": query, "reason": "empty search results"})
                continue

            preferred_results = [item for item in search_results if item.get("source_tier") != "weak"]
            if preferred_results:
                search_results = preferred_results

            local_logs.append(
                {
                    "role": "search_tool",
                    "content": {
                        "task": task_desc,
                        "query": query,
                        "mode": mode,
                        "results": [
                            {
                                "url": item.get("url", ""),
                                "source_tier": item.get("source_tier", "standard"),
                                "authority_score": item.get("authority_score", 0.0),
                            }
                            for item in search_results
                        ],
                    },
                }
            )

            if mode == "low":
                inserted_any = False
                vlm_candidates: list[dict[str, Any]] = []
                for item in search_results[:3]:
                    url = item.get("url", "")
                    if not url or km.is_duplicate(url):
                        continue
                    try:
                        content = await scrape_jina_ai(url)
                    except ToolExecutionError as exc:
                        local_missing.append(
                            {
                                "task": task_desc,
                                "query": query,
                                "url": exc.url,
                                "reason": f"scrape failed with HTTP {exc.status_code}",
                            }
                        )
                        continue
                    except Exception as exc:
                        local_missing.append({"task": task_desc, "query": query, "url": url, "reason": str(exc)})
                        continue

                    if content and content.startswith("[VLM_REQUIRED"):
                        vlm_candidates.append(item)
                        continue

                    compact = _compact_text(content)
                    if not compact or len(compact) <= 120 or "Access Denied" in compact:
                        local_missing.append(
                            {
                                "task": task_desc,
                                "query": query,
                                "url": url,
                                "reason": "content too short or blocked",
                            }
                        )
                        continue

                    inserted = km.add_compact_document(
                        compact,
                        url,
                        item.get("title", ""),
                        section_id=section_id,
                        extra_metadata={
                            "source_tier": item.get("source_tier", "standard"),
                            "authority_score": float(item.get("authority_score", 0.0)),
                            "fetch_provider": "low_scrape",
                        },
                    )
                    if inserted:
                        inserted_any = True
                        retrieval_metrics = _merge_metrics(
                            retrieval_metrics,
                            {
                                "fetch_attempts": 1,
                                "successful_authority_fetches": 1
                                if item.get("source_tier") == "high_authority"
                                else 0,
                                "high_value_evidence_count": 1
                                if float(item.get("authority_score", 0.0)) >= 0.75
                                else 0,
                            },
                        )
                        local_logs.append(
                            {
                                "role": "low_scrape",
                                "content": {
                                    "task": task_desc,
                                    "query": query,
                                    "title": item.get("title", ""),
                                    "url": url,
                                },
                            }
                        )

                if not inserted_any and vlm_candidates and len(vlm_candidates) == len(search_results):
                    await scrape_with_fallback(
                        vlm_candidates[0],
                        allow_visual=True,
                        stage="low_visual_fallback",
                        count_as_fallback=False,
                    )
                continue

            if mode == "medium":
                url_items: list[dict[str, Any]] = []
                seen_urls: set[str] = set()
                for item in search_results[:4]:
                    url = item.get("url", "")
                    if url and url not in seen_urls:
                        seen_urls.add(url)
                        url_items.append(item)
                urls = [item["url"] for item in url_items]
                extracted_urls: set[str] = set()

                if urls and tavily_extract_client.is_configured():
                    try:
                        extract_response = await tavily_extract_client.aextract(
                            urls,
                            query=task_desc,
                            chunks_per_source=3,
                            extract_depth="basic",
                        )
                        extract_results = extract_response.get("results", [])
                        if extract_results:
                            cost_breakdown = record_tavily_credits(
                                cost_breakdown,
                                tavily_extract_credits(len(extract_results), "basic"),
                            )
                            retrieval_metrics = _merge_metrics(retrieval_metrics, {"extract_calls": 1})
                        for extract_item in extract_results:
                            url = extract_item.get("url", "")
                            raw_content = extract_item.get("raw_content", "")
                            if not url or not raw_content:
                                continue
                            inserted = km.add_extracted_chunks(
                                raw_content,
                                url,
                                extract_item.get("title", ""),
                                section_id=section_id,
                                provider="tavily_extract_basic",
                                extra_metadata={
                                    "source_tier": next(
                                        (candidate.get("source_tier", "standard") for candidate in url_items if candidate["url"] == url),
                                        "standard",
                                    ),
                                    "authority_score": float(
                                        next(
                                            (candidate.get("authority_score", 0.0) for candidate in url_items if candidate["url"] == url),
                                            0.0,
                                        )
                                    ),
                                },
                            )
                            if inserted:
                                extracted_urls.add(url)
                                retrieval_metrics = _merge_metrics(
                                    retrieval_metrics,
                                    {
                                        "high_value_evidence_count": 1
                                        if float(
                                            next(
                                                (candidate.get("authority_score", 0.0) for candidate in url_items if candidate["url"] == url),
                                                0.0,
                                            )
                                        )
                                        >= 0.75
                                        else 0
                                    },
                                )
                                local_logs.append(
                                    {
                                        "role": "tavily_extract_basic",
                                        "content": {
                                            "task": task_desc,
                                            "query": query,
                                            "url": url,
                                            "chunks": inserted,
                                        },
                                    }
                                )
                    except ToolExecutionError as exc:
                        local_degraded.append(
                            {
                                "task": task_desc,
                                "query": query,
                                "stage": "extract",
                                "reason": f"Tavily extract failed with HTTP {exc.status_code}",
                            }
                        )

                for item in url_items:
                    if item["url"] in extracted_urls:
                        continue
                    await scrape_with_fallback(item, allow_visual=True, stage="medium_scrape_fallback")
                continue

            primary_domain, primary_url = _select_primary_domain(search_results)
            mapped_urls: list[str] = []
            crawled_results: list[dict[str, Any]] = []
            if primary_domain and primary_url and tavily_map_client.is_configured():
                try:
                    map_response = await tavily_map_client.amap(
                        primary_url,
                        limit=10,
                        max_depth=2,
                        max_breadth=4,
                        allow_external=False,
                    )
                    mapped_urls = [url for url in map_response.get("results", []) if isinstance(url, str)]
                    if mapped_urls:
                        cost_breakdown = record_tavily_credits(
                            cost_breakdown,
                            tavily_map_credits(len(mapped_urls), False),
                        )
                        retrieval_metrics = _merge_metrics(retrieval_metrics, {"map_calls": 1})
                        local_logs.append(
                            {
                                "role": "tavily_map",
                                "content": {
                                    "task": task_desc,
                                    "query": query,
                                    "primary_domain": primary_domain,
                                    "mapped_urls": mapped_urls[:5],
                                },
                            }
                        )
                except ToolExecutionError as exc:
                    local_degraded.append(
                        {
                            "task": task_desc,
                            "query": query,
                            "stage": "map",
                            "reason": f"Tavily map failed with HTTP {exc.status_code}",
                        }
                    )

            if len(mapped_urls) >= 3 and primary_url and tavily_crawl_client.is_configured():
                try:
                    crawl_response = await tavily_crawl_client.acrawl(
                        primary_url,
                        limit=5,
                        max_depth=2,
                        max_breadth=4,
                        extract_depth="advanced",
                        allow_external=False,
                        include_images=False,
                    )
                    crawled_results = crawl_response.get("results", [])
                    if crawled_results:
                        cost_breakdown = record_tavily_credits(
                            cost_breakdown,
                            tavily_crawl_credits(len(crawled_results), extract_depth="advanced"),
                        )
                        retrieval_metrics = _merge_metrics(retrieval_metrics, {"crawl_calls": 1})
                        local_logs.append(
                            {
                                "role": "tavily_crawl",
                                "content": {
                                    "task": task_desc,
                                    "query": query,
                                    "primary_domain": primary_domain,
                                    "crawled_urls": [item.get("url", "") for item in crawled_results[:5]],
                                },
                            }
                        )
                except ToolExecutionError as exc:
                    local_degraded.append(
                        {
                            "task": task_desc,
                            "query": query,
                            "stage": "crawl",
                            "reason": f"Tavily crawl failed with HTTP {exc.status_code}",
                        }
                    )

            selected_urls = _choose_high_value_urls(search_results, mapped_urls, crawled_results, limit=5)
            search_lookup = {item.get("url", ""): item for item in search_results if item.get("url")}
            crawl_lookup = {item.get("url", ""): item for item in crawled_results if item.get("url")}
            extracted_urls: set[str] = set()

            if selected_urls and tavily_extract_client.is_configured():
                try:
                    extract_response = await tavily_extract_client.aextract(
                        selected_urls,
                        query=task_desc,
                        chunks_per_source=5,
                        extract_depth="advanced",
                    )
                    extract_results = extract_response.get("results", [])
                    if extract_results:
                        cost_breakdown = record_tavily_credits(
                            cost_breakdown,
                            tavily_extract_credits(len(extract_results), "advanced"),
                        )
                        retrieval_metrics = _merge_metrics(retrieval_metrics, {"extract_calls": 1})
                    for extract_item in extract_results:
                        url = extract_item.get("url", "")
                        raw_content = extract_item.get("raw_content", "")
                        title = extract_item.get("title", "") or search_lookup.get(url, {}).get("title", "")
                        if not url or not raw_content:
                            continue
                        inserted = km.add_extracted_chunks(
                            raw_content,
                            url,
                            title,
                            section_id=section_id,
                            provider="tavily_extract_advanced",
                            extra_metadata={
                                "source_tier": search_lookup.get(url, {}).get("source_tier", "standard"),
                                "authority_score": float(search_lookup.get(url, {}).get("authority_score", 0.0)),
                            },
                        )
                        if inserted:
                            extracted_urls.add(url)
                            retrieval_metrics = _merge_metrics(
                                retrieval_metrics,
                                {
                                    "high_value_evidence_count": 1
                                    if float(search_lookup.get(url, {}).get("authority_score", 0.0)) >= 0.75
                                    else 0
                                },
                            )
                            local_logs.append(
                                {
                                    "role": "tavily_extract_advanced",
                                    "content": {
                                        "task": task_desc,
                                        "query": query,
                                        "url": url,
                                        "chunks": inserted,
                                    },
                                }
                            )
                except ToolExecutionError as exc:
                    local_degraded.append(
                        {
                            "task": task_desc,
                            "query": query,
                            "stage": "extract",
                            "reason": f"Tavily extract failed with HTTP {exc.status_code}",
                        }
                    )

            for url in selected_urls:
                if url in extracted_urls or km.is_duplicate(url):
                    continue
                crawled_item = crawl_lookup.get(url)
                crawl_content = _compact_text((crawled_item or {}).get("raw_content", ""))
                title = search_lookup.get(url, {}).get("title", "") or (crawled_item or {}).get("title", "")
                if crawl_content and len(crawl_content) > 120:
                    retrieval_metrics = _merge_metrics(retrieval_metrics, {"fallback_count": 1})
                    inserted = km.add_compact_document(
                        crawl_content,
                        url,
                        title,
                        section_id=section_id,
                        extra_metadata={
                            "source_tier": search_lookup.get(url, {}).get("source_tier", "standard"),
                            "authority_score": float(search_lookup.get(url, {}).get("authority_score", 0.0)),
                            "fetch_provider": "tavily_crawl",
                        },
                    )
                    if inserted:
                        retrieval_metrics = _merge_metrics(
                            retrieval_metrics,
                            {
                                "high_value_evidence_count": 1
                                if float(search_lookup.get(url, {}).get("authority_score", 0.0)) >= 0.75
                                else 0
                            },
                        )
                        local_logs.append(
                            {
                                "role": "high_crawl_fallback",
                                "content": {"task": task_desc, "query": query, "url": url},
                            }
                        )
                        continue

                await scrape_with_fallback(
                    search_lookup.get(url, {"url": url, "title": title}),
                    allow_visual=True,
                    stage="high_scrape_fallback",
                )

        return {
            "logs": local_logs,
            "missing_sources": local_missing,
            "degraded_items": local_degraded,
        }

    async def targeted_backfill(task_item: dict[str, Any]) -> dict[str, Any]:
        nonlocal cost_breakdown, retrieval_metrics, backfill_attempts_total, fetch_results

        logs: list[dict[str, Any]] = []
        local_missing: list[dict[str, Any]] = []
        local_degraded: list[dict[str, Any]] = []
        section_id = task_item.get("section_id", "global")
        task_desc = task_item.get("task", state["query"])
        inserted_any = False
        for query in _build_backfill_queries(task_desc)[:1]:
            backfill_attempts_total += 1
            try:
                search_results, cost_breakdown, retrieval_metrics = await _mode_search(
                    mode,
                    query,
                    max_results=5,
                    cost_breakdown=cost_breakdown,
                    retrieval_metrics=retrieval_metrics,
                )
            except Exception as exc:
                local_missing.append({"task": task_desc, "query": query, "reason": f"backfill_search_failed: {exc}"})
                continue

            qualified_results = qualify_search_results(search_results, task_desc, limit=5)
            authority_candidates = [item for item in qualified_results if item.get("source_tier") == "high_authority"]
            candidate_results = authority_candidates or [item for item in qualified_results if item.get("source_tier") != "weak"]
            for item in candidate_results[:2]:
                url = item.get("url", "")
                host = str(item.get("host") or _result_host(str(url))).strip().lower()
                if not url or km.is_duplicate(url):
                    continue
                if should_force_access_backfill(item, quarantined_pdf_hosts=pdf_host_quarantine):
                    if await access_backfill(item, stage="targeted_backfill_pdf_access_backfill"):
                        inserted_any = True
                        break
                    local_missing.append(
                        {
                            "task": task_desc,
                            "query": query,
                            "url": url,
                            "reason": "pdf_access_backfill_failed",
                        }
                    )
                    continue
                retrieval_metrics = _merge_metrics(retrieval_metrics, {"fallback_count": 1})
                fetched = await fetch_source_candidate(
                    item,
                    allow_visual=False,
                    goal=f"Fetch authoritative support for {task_desc}",
                )
                if should_quarantine_pdf_host(item, fetched) and host:
                    pdf_host_quarantine.add(host)
                attempts = list(fetched.get("attempts") or [])
                if not attempts:
                    attempts = [
                        {
                            "provider": fetched.get("provider") or "backfill",
                            "status": fetched.get("status") or "failed",
                            "page_type": fetched.get("page_type") or "",
                            "host": fetched.get("host") or item.get("host") or _result_host(str(item.get("url") or "")),
                            "error_class": fetched.get("error_class") or "",
                            "http_status": int(fetched.get("http_status") or 0),
                            "content_type": fetched.get("content_type") or "",
                            "content_length": int(fetched.get("content_length") or 0),
                            "authority_preserved": bool(fetched.get("authority_preserved")),
                            "attempt_order": 1,
                            "salvaged_by_fallback": bool(fetched.get("salvaged_by_fallback")),
                            "blocked_stage": fetched.get("blocked_stage") or "",
                            "final_url": fetched.get("final_url") or item.get("url") or "",
                        }
                    ]
                for attempt in attempts:
                    fetch_results.append(
                        {
                            "task": task_desc,
                            "url": url,
                            "provider": attempt.get("provider") or "backfill",
                            "status": attempt.get("status") or "failed",
                            "page_type": attempt.get("page_type") or fetched.get("page_type") or "",
                            "host": attempt.get("host") or item.get("host") or _result_host(str(item.get("url") or "")),
                            "error_class": attempt.get("error_class") or "",
                            "http_status": int(attempt.get("http_status") or 0),
                            "content_type": attempt.get("content_type") or "",
                            "content_length": int(attempt.get("content_length") or 0),
                            "authority_preserved": bool(attempt.get("authority_preserved")),
                            "source_tier": item.get("source_tier", "standard"),
                            "attempt_order": int(attempt.get("attempt_order") or 0),
                            "salvaged_by_fallback": bool(attempt.get("salvaged_by_fallback")),
                            "blocked_stage": attempt.get("blocked_stage") or "",
                            "final_url": attempt.get("final_url") or fetched.get("final_url") or url,
                        }
                    )
                attempt_count = len(attempts)
                blocked_count = sum(
                    1 for attempt in attempts if _is_blocked_fetch_error(str(attempt.get("error_class") or ""))
                )
                retrieval_metrics = _merge_metrics(
                    retrieval_metrics,
                    {"fetch_attempts": attempt_count, "blocked_fetches": blocked_count},
                )
                if fetched.get("credits_est"):
                    cost_breakdown = record_tavily_credits(cost_breakdown, float(fetched.get("credits_est") or 0.0))
                    retrieval_metrics = _merge_metrics(retrieval_metrics, {"extract_calls": 1})
                if fetched.get("status") != "ok":
                    forced_html_backfill = should_force_non_pdf_access_backfill(
                        item,
                        fetched,
                        attempted_hosts=same_host_access_backfill_hosts,
                    )
                    if forced_html_backfill:
                        if await access_backfill(item, stage="targeted_backfill_html_access_backfill"):
                            inserted_any = True
                            break
                    local_missing.append(
                        {
                            "task": task_desc,
                            "query": query,
                            "url": url,
                            "reason": fetched.get("error_class") or "backfill_fetch_failed",
                        }
                    )
                    continue
                compact = _compact_text(str(fetched.get("content") or ""))
                if not compact or len(compact) <= 120:
                    local_missing.append(
                        {
                            "task": task_desc,
                            "query": query,
                            "url": url,
                            "reason": "backfill_empty_content",
                        }
                    )
                    continue
                inserted = km.add_compact_document(
                    compact,
                    url,
                    item.get("title", ""),
                    section_id=section_id,
                    extra_metadata={
                        "source_tier": item.get("source_tier", "standard"),
                        "authority_score": float(item.get("authority_score", 0.0)),
                        "fetch_provider": fetched.get("provider") or "backfill",
                        "fetch_status": fetched.get("status") or "ok",
                    },
                )
                if inserted:
                    inserted_any = True
                    retrieval_metrics = _merge_metrics(
                        retrieval_metrics,
                        {
                            "successful_authority_fetches": 1
                            if item.get("source_tier") == "high_authority"
                            else 0,
                            "high_value_evidence_count": 1
                            if float(item.get("authority_score", 0.0)) >= 0.75
                            else 0,
                        },
                    )
                    logs.append(
                        {
                            "role": "targeted_backfill",
                            "content": {
                                "task": task_desc,
                                "query": query,
                                "url": url,
                                "source_tier": item.get("source_tier", "standard"),
                            },
                        }
                )
            if inserted_any:
                break
            local_degraded.append(
                {
                    "task": task_desc,
                    "stage": "targeted_backfill",
                    "reason": f"no authoritative support added for query={query}",
                }
            )
        return {"logs": logs, "missing_sources": local_missing, "degraded_items": local_degraded, "success": inserted_any}

    def build_evidence_insufficiency_report(
        coverage_summary: dict[str, float | int],
        evidence_slots: dict[str, Any],
    ) -> str:
        lines = [
            f"# {state['query']}",
            "",
            "## Direct Answer / Core Conclusion",
            "Current evidence is insufficient to produce a reliable final answer.",
            "",
            "## Key Evidence",
            "The search and fetch pipeline did not collect enough high-authority support for every required clause.",
        ]
        if evidence_slots:
            for slot_id, slot in evidence_slots.items():
                lines.append(
                    f"- Clause {slot_id}: covered={slot.get('covered')} | "
                    f"high_authority_sources={slot.get('high_authority_source_count', 0)} | "
                    f"question={slot.get('question', '')}"
                )
        lines.extend(
            [
                "",
                "## Analysis",
                (
                    "The system intentionally stopped before report synthesis because the evidence gate failed. "
                    f"coverage={coverage_summary.get('evidence_coverage_rate', 0.0)}, "
                    f"high_authority_source_rate={coverage_summary.get('authority_source_rate', 0.0)}, "
                    f"direct_answer_support_rate={coverage_summary.get('direct_answer_support_rate', 0.0)}."
                ),
                "",
                "## Uncertainty / Missing Evidence",
                "More authoritative sources are required before a final report should be trusted.",
            ]
        )
        return "\n".join(lines)

    for task_item in state["plan"]:
        result = await process_task(task_item)
        history.extend(result["logs"])
        missing_sources.extend(result["missing_sources"])
        degraded_items.extend(result["degraded_items"])

    backfill_successes = 0
    evidence_slots = build_evidence_slots(task_contract=task_contract, km=km)
    coverage = compute_coverage_summary(
        plan=state["plan"],
        km=km,
        retrieval_metrics=retrieval_metrics,
        evidence_slots=evidence_slots,
        fetch_results=fetch_results,
        backfill_attempts=backfill_attempts_total,
        backfill_successes=backfill_successes,
    )
    gate = evaluate_evidence_gate(coverage_summary=coverage, evidence_slots=evidence_slots)
    if gate["needs_backfill"]:
        uncovered_tasks: list[dict[str, Any]] = []
        uncovered_sections = {
            str(slot.get("section_id") or "global")
            for slot in evidence_slots.values()
            if not slot.get("covered")
        }
        for task_item in state["plan"]:
            section_id = task_item.get("section_id", "global")
            docs = km.retrieve(section_id=section_id, k=20)
            authoritative_docs = [
                doc
                for doc in docs
                if str(doc.metadata.get("source_tier") or "") == "high_authority"
                or float(doc.metadata.get("authority_score") or 0.0) >= 0.95
            ]
            if len(docs) < 2 or not authoritative_docs or str(section_id) in uncovered_sections:
                uncovered_tasks.append(task_item)
        for task_item in uncovered_tasks[:1]:
            backfill = await targeted_backfill(task_item)
            history.extend(backfill["logs"])
            missing_sources.extend(backfill["missing_sources"])
            degraded_items.extend(backfill["degraded_items"])
            if backfill.get("success"):
                backfill_successes += 1
        evidence_slots = build_evidence_slots(task_contract=task_contract, km=km)
        coverage = compute_coverage_summary(
            plan=state["plan"],
            km=km,
            retrieval_metrics=retrieval_metrics,
            evidence_slots=evidence_slots,
            fetch_results=fetch_results,
            backfill_attempts=backfill_attempts_total,
            backfill_successes=backfill_successes,
        )
        gate = evaluate_evidence_gate(coverage_summary=coverage, evidence_slots=evidence_slots)
        if not gate["passed"]:
            degraded_items.append(
                {
                    "task": state["query"],
                    "stage": "evidence_gate",
                    "reason": (
                        f"coverage={coverage['evidence_coverage_rate']}, "
                        f"high_authority_sources={coverage['high_authority_source_count']}, "
                        f"task_clause_coverage={coverage['task_clause_coverage_rate']}"
                    ),
                }
            )
            final_report = build_evidence_insufficiency_report(coverage, evidence_slots)
            metrics = state.get("metrics", {"tool_calls": 0, "backtracking": 0})
            metrics["tool_calls"] = (
                metrics.get("tool_calls", 0)
                + int(retrieval_metrics.get("search_calls", 0))
                + int(retrieval_metrics.get("extract_calls", 0))
                + int(retrieval_metrics.get("map_calls", 0))
                + int(retrieval_metrics.get("crawl_calls", 0))
                + int(retrieval_metrics.get("fallback_count", 0))
                + int(retrieval_metrics.get("visual_browse_calls", 0))
            )
            return {
                "metrics": metrics,
                "history": history,
                "conflict_detected": state.get("conflict_detected", False),
                "conflict_count": state.get("conflict_count", 0),
                "missing_sources": missing_sources,
                "degraded_items": degraded_items,
                "task_contract": task_contract,
                "evidence_slots": evidence_slots,
                "draft_audit": state.get("draft_audit", {}),
                "cost_breakdown": cost_breakdown,
                "retrieval_metrics": {**retrieval_metrics, **coverage},
                "source_candidates": source_candidates,
                "fetch_results": fetch_results,
                "coverage_summary": coverage,
                "backfill_attempts": backfill_attempts_total,
                "retrieval_failed": True,
                "final_report": final_report,
            }

    metrics = state.get("metrics", {"tool_calls": 0, "backtracking": 0})
    metrics["tool_calls"] = (
        metrics.get("tool_calls", 0)
        + int(retrieval_metrics.get("search_calls", 0))
        + int(retrieval_metrics.get("extract_calls", 0))
        + int(retrieval_metrics.get("map_calls", 0))
        + int(retrieval_metrics.get("crawl_calls", 0))
        + int(retrieval_metrics.get("fallback_count", 0))
        + int(retrieval_metrics.get("visual_browse_calls", 0))
    )

    log_trajectory(
        task_id,
        "executor",
        {
            "mode": mode,
            "tasks": len(state["plan"]),
            "missing_sources": len(missing_sources),
            "degraded_items": len(degraded_items),
            "cost_breakdown": cost_breakdown,
            "retrieval_metrics": {**retrieval_metrics, **coverage},
            "evidence_slots": evidence_slots,
        },
    )

    return {
        "metrics": metrics,
        "history": history,
        "conflict_detected": state.get("conflict_detected", False),
        "conflict_count": state.get("conflict_count", 0),
        "missing_sources": missing_sources,
        "degraded_items": degraded_items,
        "task_contract": task_contract,
        "evidence_slots": evidence_slots,
        "draft_audit": state.get("draft_audit", {}),
        "cost_breakdown": cost_breakdown,
        "retrieval_metrics": {**retrieval_metrics, **coverage},
        "source_candidates": source_candidates,
        "fetch_results": fetch_results,
        "coverage_summary": coverage,
        "backfill_attempts": backfill_attempts_total,
        "retrieval_failed": False,
    }


def _build_degradation_appendix(state: ResearchState) -> str:
    lines = []
    missing_sources = state.get("missing_sources", [])
    degraded_items = state.get("degraded_items", [])
    if not missing_sources and not degraded_items:
        return ""

    lines.append("\n\n## 资料缺口与降级说明")
    if missing_sources:
        lines.append("\n### 未完成来源")
        for item in missing_sources[:20]:
            task = item.get("task", "unknown task")
            query = item.get("query", "n/a")
            reason = item.get("reason", "unknown reason")
            url = item.get("url")
            suffix = f" | {url}" if url else ""
            lines.append(f"- {task} | query={query} | reason={reason}{suffix}")
    if degraded_items:
        lines.append("\n### 已执行降级")
        for item in degraded_items[:20]:
            stage = item.get("stage", "unknown")
            reason = item.get("reason", "unknown reason")
            task = item.get("task", "unknown task")
            lines.append(f"- {task} | stage={stage} | reason={reason}")
    lines.append("\n### 置信边界")
    lines.append("- 报告已尽量继续产出，但部分结论可能受缺失来源或降级路径影响。")
    return "\n".join(lines)


async def node_writer(state: ResearchState):
    task_id = state.get("task_id") or get_current_session_id() or "unknown-task"
    writer_thread_id = get_writer_thread_id(task_id)
    writer_config = {"configurable": {"thread_id": writer_thread_id}}
    writer_context_mode = resolve_writer_context_mode()
    writer_inputs = {
        "query": state["query"],
        "outline": state.get("outline", []),
        "sections": {},
        "final_doc": "",
        "iteration": 0,
        "user_feedback": "SKIP_REVIEW",
        "task_id": task_id,
        "writer_context_mode": writer_context_mode,
        "task_contract": dict(state.get("task_contract") or {}),
        "evidence_slots": dict(state.get("evidence_slots") or {}),
        "draft_audit": {},
        "audit_revision_count": 0,
        "required_analysis_modes": list((state.get("task_contract") or {}).get("required_analysis_modes") or []),
    }
    try:
        interrupt_before = ["editor"] if should_interrupt_task(task_id, "writer.before_editor") else None
        existing_writer_state = writer_app.get_state(writer_config)
        if existing_writer_state.values.get("final_doc") and not existing_writer_state.next:
            result = existing_writer_state.values
        else:
            writer_input = None if existing_writer_state.next else writer_inputs
            latest_result: dict[str, Any] = {}
            async for event in writer_app.astream(
                writer_input,
                config=writer_config,
                interrupt_before=interrupt_before,
            ):
                if "__interrupt__" in event:
                    writer_state = writer_app.get_state(writer_config)
                    checkpoint_config = dict(writer_state.config.get("configurable", {}))
                    save_knowledge_snapshot(
                        task_id,
                        thread_id=task_id,
                        checkpoint_id=checkpoint_config.get("checkpoint_id"),
                        checkpoint_ns=checkpoint_config.get("checkpoint_ns"),
                        checkpoint_node="writer.section_writer",
                        snapshot=get_current_km().snapshot(),
                    )
                    task = get_task(task_id) or {}
                    upsert_task(
                        task_id,
                        state["query"],
                        "INTERRUPTED",
                        detail="Writer paused before editor for checkpoint recovery testing",
                        thread_id=task_id,
                        backend=task.get("backend") or "resume",
                        research_mode=state.get("research_mode", DEFAULT_RESEARCH_MODE),
                        llm_cost_rmb=float(task.get("llm_cost_rmb") or 0.0),
                        external_cost_usd_est=float(task.get("external_cost_usd_est") or 0.0),
                        serper_queries=int(task.get("serper_queries") or 0),
                        serper_cost_usd_est=float(task.get("serper_cost_usd_est") or 0.0),
                        tavily_credits_est=float(task.get("tavily_credits_est") or 0.0),
                        tavily_cost_usd_est=float(task.get("tavily_cost_usd_est") or 0.0),
                        elapsed_seconds=float(task.get("elapsed_seconds") or 0.0),
                        attempt_count=int(task.get("attempt_count") or 1),
                        resume_count=int(task.get("resume_count") or 0),
                        resumed_from_checkpoint=bool(task.get("resumed_from_checkpoint") or 0),
                        started_at=task.get("started_at"),
                        last_checkpoint_id=checkpoint_config.get("checkpoint_id"),
                        last_checkpoint_ns=checkpoint_config.get("checkpoint_ns"),
                        last_checkpoint_node="writer.section_writer",
                        interruption_state="writer.before_editor",
                    )
                    crash_process()
                latest_result.update(event)
            result = writer_app.get_state(writer_config).values if latest_result else existing_writer_state.values
        report = result.get("final_doc", "Writing Failed")
        draft_audit = dict(result.get("draft_audit") or {})
    except Exception as exc:
        report = f"Writer Subgraph Error: {exc}"
        draft_audit = {
            "passed": False,
            "missing_requirements": ["writer_subgraph_error"],
        }
    report += _build_degradation_appendix(state)
    return {
        "final_report": report,
        "draft_audit": draft_audit,
        "cost_breakdown": state.get("cost_breakdown", {}),
        "retrieval_metrics": {
            **state.get("retrieval_metrics", {}),
            "task_clause_coverage_rate": float(draft_audit.get("task_clause_coverage_rate", state.get("retrieval_metrics", {}).get("task_clause_coverage_rate", 0.0))),
            "direct_answer_present": 1 if draft_audit.get("direct_answer_present") else 0,
            "direct_answer_citation_backed": 1 if draft_audit.get("direct_answer_citation_backed") else 0,
            "analysis_signal_count": int(draft_audit.get("analysis_signal_count", 0)),
            "comparison_present": 1 if draft_audit.get("comparison_present") else 0,
            "causal_present": 1 if draft_audit.get("causal_present") else 0,
            "risk_present": 1 if draft_audit.get("risk_present") else 0,
        },
    }


def router_feedback(state: ResearchState):
    if state.get("final_report") == "User Terminated":
        return END
    if state.get("user_feedback"):
        return "planner"
    return "executor"


def router_conflict(state: ResearchState):
    if state.get("retrieval_failed"):
        return END
    if state.get("conflict_detected"):
        if state.get("conflict_count", 0) >= 2:
            return "writer"
        return "planner"
    return "writer"


workflow = StateGraph(ResearchState)
workflow.add_node("planner", node_init_search)
workflow.add_node("human_review", node_human_feedback)
workflow.add_node("executor", node_deep_research)
workflow.add_node("writer", node_writer)
workflow.set_entry_point("planner")
workflow.add_edge("planner", "human_review")
workflow.add_conditional_edges("human_review", router_feedback, ["planner", "executor", END])
workflow.add_conditional_edges("executor", router_conflict, ["writer", "planner", END])
workflow.add_edge("writer", END)

checkpointer = get_sqlite_checkpointer(CHECKPOINT_DB_PATH)
app = workflow.compile(checkpointer=checkpointer)
