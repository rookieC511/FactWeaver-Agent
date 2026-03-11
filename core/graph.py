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
from core.memory import get_current_km, get_current_session_id
from core.models import llm_fast
from core.runtime_control import crash_process, should_interrupt_task
from core.tools import (
    LLMFormatError,
    ToolExecutionError,
    clean_json_output,
    default_cost_breakdown,
    default_retrieval_metrics,
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
    return response.get("results", []), cost_breakdown, retrieval_metrics


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
    return response.get("results", []), cost_breakdown, retrieval_metrics


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
  "search_tasks": [{{"task": "query", "reason": "why", "section_id": "1"}}]
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
    if isinstance(plan_data, dict):
        outline = plan_data.get("outline", [])
        plan = plan_data.get("search_tasks", [])

    if not isinstance(outline, list) or not outline:
        outline = _fallback_outline(query)
    if not isinstance(plan, list) or not plan:
        plan = _fallback_plan(query, outline)

    metrics = state.get("metrics", {"tool_calls": 0, "backtracking": 0})
    if state.get("user_feedback"):
        metrics["backtracking"] = metrics.get("backtracking", 0) + 1

    history = state.get("history", [])
    history.append(
        {
            "role": "planner_cot",
            "content": {"query": query, "outline": outline, "search_tasks": plan},
        }
    )
    log_trajectory(task_id, "planner", {"outline": outline, "search_tasks": plan})

    return {
        "plan": plan,
        "outline": outline,
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
    history = list(state.get("history", []))
    missing_sources = list(state.get("missing_sources", []))
    degraded_items = list(state.get("degraded_items", []))
    cost_breakdown = dict(state.get("cost_breakdown") or default_cost_breakdown())
    retrieval_metrics = dict(state.get("retrieval_metrics") or default_retrieval_metrics())

    async def process_task(task_item: dict[str, Any]) -> dict[str, Any]:
        nonlocal cost_breakdown, retrieval_metrics

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

        async def scrape_with_fallback(
            item: dict[str, Any],
            *,
            allow_visual: bool,
            stage: str,
            count_as_fallback: bool = True,
        ) -> bool:
            nonlocal retrieval_metrics

            url = item.get("url", "")
            title = item.get("title", "")
            if not url or km.is_duplicate(url):
                return False

            if count_as_fallback:
                retrieval_metrics = _merge_metrics(retrieval_metrics, {"fallback_count": 1})

            try:
                content = await scrape_jina_ai(url)
            except ToolExecutionError as exc:
                local_missing.append(
                    {
                        "task": task_desc,
                        "url": exc.url,
                        "reason": f"scrape failed with HTTP {exc.status_code}",
                    }
                )
                return False
            except Exception as exc:
                local_missing.append({"task": task_desc, "url": url, "reason": str(exc)})
                return False

            if content and content.startswith("[VLM_REQUIRED"):
                if not allow_visual:
                    local_degraded.append(
                        {
                            "task": task_desc,
                            "url": url,
                            "stage": stage,
                            "reason": f"visual fallback disabled for mode={mode}",
                        }
                    )
                    return False
                retrieval_metrics = _merge_metrics(retrieval_metrics, {"visual_browse_calls": 1})
                visual_result = await visual_browse(
                    url,
                    f"Extract the charts, tables, formulas and visual facts relevant to: {task_desc}",
                )
                if visual_result.startswith("[VISUAL_BROWSE_UNAVAILABLE]") or visual_result.startswith("Error:"):
                    local_degraded.append(
                        {
                            "task": task_desc,
                            "url": url,
                            "stage": "visual_browse",
                            "reason": visual_result,
                        }
                    )
                    return False

                inserted = km.add_compact_document(
                    f"[VLM Extracted]\n{visual_result}",
                    url,
                    title,
                    section_id=section_id,
                )
                if inserted:
                    local_logs.append(
                        {
                            "role": "visual_fallback",
                            "content": {"task": task_desc, "title": title, "url": url},
                        }
                    )
                    return True
                return False

            compact = _compact_text(content)
            if not compact or len(compact) <= 120 or "Access Denied" in compact:
                local_missing.append(
                    {
                        "task": task_desc,
                        "url": url,
                        "reason": "content too short or blocked",
                    }
                )
                return False

            inserted = km.add_compact_document(compact, url, title, section_id=section_id)
            if inserted:
                local_logs.append(
                    {
                        "role": stage,
                        "content": {"task": task_desc, "title": title, "url": url},
                    }
                )
                return True
            return False

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

            local_logs.append(
                {
                    "role": "search_tool",
                    "content": {
                        "task": task_desc,
                        "query": query,
                        "mode": mode,
                        "urls": [item.get("url", "") for item in search_results],
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

                    inserted = km.add_compact_document(compact, url, item.get("title", ""), section_id=section_id)
                    if inserted:
                        inserted_any = True
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
                for item in search_results[:3]:
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
                            )
                            if inserted:
                                extracted_urls.add(url)
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
                        )
                        if inserted:
                            extracted_urls.add(url)
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
                    inserted = km.add_compact_document(crawl_content, url, title, section_id=section_id)
                    if inserted:
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

    for task_item in state["plan"]:
        result = await process_task(task_item)
        history.extend(result["logs"])
        missing_sources.extend(result["missing_sources"])
        degraded_items.extend(result["degraded_items"])

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
            "retrieval_metrics": retrieval_metrics,
        },
    )

    return {
        "metrics": metrics,
        "history": history,
        "conflict_detected": state.get("conflict_detected", False),
        "conflict_count": state.get("conflict_count", 0),
        "missing_sources": missing_sources,
        "degraded_items": degraded_items,
        "cost_breakdown": cost_breakdown,
        "retrieval_metrics": retrieval_metrics,
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
    except Exception as exc:
        report = f"Writer Subgraph Error: {exc}"
    report += _build_degradation_appendix(state)
    return {
        "final_report": report,
        "cost_breakdown": state.get("cost_breakdown", {}),
        "retrieval_metrics": state.get("retrieval_metrics", {}),
    }


def router_feedback(state: ResearchState):
    if state.get("final_report") == "User Terminated":
        return END
    if state.get("user_feedback"):
        return "planner"
    return "executor"


def router_conflict(state: ResearchState):
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
workflow.add_conditional_edges("executor", router_conflict, ["writer", "planner"])
workflow.add_edge("writer", END)

checkpointer = get_sqlite_checkpointer(CHECKPOINT_DB_PATH)
app = workflow.compile(checkpointer=checkpointer)
