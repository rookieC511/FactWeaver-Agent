from __future__ import annotations

from collections import Counter
import time
from urllib.parse import urlparse
from typing import Any, Callable

from langchain_core.messages import HumanMessage

from core.config import DEFAULT_RESEARCH_MODE
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
    should_quarantine_pdf_host,
)
from core.memory import get_current_km, get_current_session_id
from core.models import llm_fast, llm_smart
from core.multi_agent_runtime import (
    EvidenceDigest,
    RetrievalPlan,
    backfill_made_no_improvement,
    build_clause_statuses,
    build_progress_ledger,
    build_slot_statuses,
    build_task_ledger,
    default_retrieval_plan,
    normalize_architecture_mode,
    normalize_retrieval_plan,
    save_json_artifact,
    update_progress_ledger,
)
from core.source_policy import infer_topic_family
from core.tools import (
    LLMFormatError,
    ToolExecutionError,
    clean_json_output,
    default_cost_breakdown,
    default_retrieval_metrics,
    record_tavily_credits,
    tavily_crawl_client,
    tavily_crawl_credits,
    tavily_extract_client,
    tavily_extract_credits,
    tavily_map_client,
    tavily_map_credits,
)
from gateway.state_store import get_task


def default_source_type_priority(task_desc: str) -> list[str]:
    family = infer_topic_family(task_desc)
    if family == "finance_business":
        return ["official", "regulator", "academic", "institutional"]
    if family == "crime_law":
        return ["regulator", "official", "academic", "institutional"]
    if family == "education_jobs":
        return ["official", "academic", "institutional", "regulator"]
    return ["official", "academic", "institutional", "media"]


def build_authority_queries_from_plan(task_desc: str, retrieval_plan: RetrievalPlan | None) -> list[str]:
    plan = dict(retrieval_plan or {})
    query_intents = [str(item).strip() for item in plan.get("query_intents") or [] if str(item).strip()]
    source_types = [str(item).strip().lower() for item in plan.get("source_type_priority") or [] if str(item).strip()]
    queries: list[str] = []
    for source_type in source_types[:3]:
        if source_type == "official":
            queries.append(f"{task_desc} official report")
        elif source_type == "regulator":
            queries.append(f"{task_desc} site:gov official filing report")
        elif source_type == "academic":
            queries.append(f"{task_desc} site:edu research paper")
        elif source_type == "institutional":
            queries.append(f"{task_desc} institutional report pdf")
        elif source_type == "media":
            queries.append(f"{task_desc} authoritative analysis")
    for intent in query_intents[:2]:
        queries.append(intent)
    deduped: list[str] = []
    for item in queries:
        if item and item not in deduped:
            deduped.append(item)
    return deduped


async def _safe_json_invoke(llm, prompt: str) -> dict[str, Any] | None:
    for attempt in range(2):
        try:
            response = await llm.ainvoke([HumanMessage(content=prompt)])
            parsed = clean_json_output(response.content, strict=True)
            if isinstance(parsed, dict):
                return parsed
            return None
        except (LLMFormatError, Exception):
            if attempt == 1:
                return None
    return None


async def build_retrieval_plan(
    *,
    task_desc: str,
    clause_statuses: dict[str, dict[str, Any]] | None,
    slot_statuses: dict[str, dict[str, Any]] | None,
    open_gaps: list[dict[str, Any]] | None,
    progress_ledger: dict[str, Any] | None,
) -> RetrievalPlan:
    fallback = default_retrieval_plan(
        target_clauses=[str(item.get("slot_id") or item.get("question") or "").strip() for item in open_gaps or [] if str(item.get("slot_id") or item.get("question") or "").strip()],
        source_type_priority=default_source_type_priority(task_desc),
        query_intents=[str(item.get("question") or "").strip() for item in open_gaps or [] if str(item.get("question") or "").strip()] or [task_desc],
        backfill_mode="targeted_backfill" if open_gaps else "authority_first",
        authority_requirement="at_least_one_high_authority_per_slot",
        stop_after_slots=[str(item.get("slot_id") or "").strip() for item in open_gaps or [] if str(item.get("slot_id") or "").strip()],
    )
    prompt = f"""
You are the Evidence Scout Agent.

Produce a strict JSON RetrievalPlan:
{{
  "target_clauses": ["1", "2"],
  "source_type_priority": ["official", "academic", "institutional"],
  "query_intents": ["specific search query", "another query"],
  "backfill_mode": "authority_first|broad_recall|targeted_backfill|same_host_access_backfill",
  "authority_requirement": "short requirement string",
  "stop_after_slots": ["1", "2"]
}}

Rules:
- Keep source_type_priority short and concrete.
- Query intents should be executable searches, not explanations.
- Prefer authority_first or targeted_backfill for unresolved gaps.

Task:
{task_desc}

Clause statuses:
{clause_statuses or {}}

Slot statuses:
{slot_statuses or {}}

Open gaps:
{open_gaps or []}

Progress ledger:
{progress_ledger or {}}
"""
    parsed = await _safe_json_invoke(llm_fast, prompt)
    return normalize_retrieval_plan(parsed, fallback=fallback)


def build_evidence_digest(
    *,
    task_contract: dict[str, Any] | None,
    evidence_slots: dict[str, Any] | None,
    slot_statuses: dict[str, dict[str, Any]] | None,
    clause_statuses: dict[str, dict[str, Any]] | None,
    open_gaps: list[dict[str, Any]] | None,
    coverage_summary: dict[str, Any] | None,
    km: Any,
    max_refs_per_slot: int = 2,
    snippet_chars: int = 180,
) -> EvidenceDigest:
    task_contract = dict(task_contract or {})
    refs: dict[str, list[dict[str, str]]] = {}
    for point in task_contract.get("must_answer_points") or []:
        slot_id = str(point.get("id") or "")
        if not slot_id:
            continue
        section_id = str(point.get("section_id") or "global")
        docs = km.retrieve(section_id=section_id, k=max_refs_per_slot * 2)
        slot_refs: list[dict[str, str]] = []
        for doc in docs:
            url = str(doc.metadata.get("url") or doc.metadata.get("source_url") or "").strip()
            if not url:
                continue
            snippet = " ".join(str(doc.page_content or "").split()).strip()
            if len(snippet) > snippet_chars:
                snippet = snippet[:snippet_chars].rstrip() + "..."
            slot_refs.append(
                {
                    "url": url,
                    "citation_hash": str(doc.metadata.get("citation_hash") or ""),
                    "snippet": snippet,
                }
            )
            if len(slot_refs) >= max_refs_per_slot:
                break
        refs[slot_id] = slot_refs
    authority_summary = {
        "high_authority_source_count": int((coverage_summary or {}).get("high_authority_source_count", 0) or 0),
        "authority_source_rate": float((coverage_summary or {}).get("authority_source_rate", 0.0) or 0.0),
        "direct_answer_support_rate": float((coverage_summary or {}).get("direct_answer_support_rate", 0.0) or 0.0),
    }
    return {
        "slot_statuses": dict(slot_statuses or {}),
        "clause_statuses": dict(clause_statuses or {}),
        "open_gaps": list(open_gaps or []),
        "authority_summary": authority_summary,
        "coverage_summary": dict(coverage_summary or {}),
        "supporting_evidence_refs": refs,
        "direct_answer_support_snapshot": {
            "required_support_rate": 1.0,
            "current_support_rate": float((coverage_summary or {}).get("direct_answer_support_rate", 0.0) or 0.0),
        },
    }


def fallback_verifier_assessment(
    *,
    coverage_summary: dict[str, Any] | None,
    open_gaps: list[dict[str, Any]] | None,
    code_gate_passed: bool,
) -> dict[str, Any]:
    coverage = dict(coverage_summary or {})
    if code_gate_passed and float(coverage.get("direct_answer_support_rate", 0.0) or 0.0) >= 1.0:
        return {
            "verifier_decision": "ready_for_writer",
            "open_gaps": [],
            "semantic_sufficiency": True,
            "reason": "Fallback semantic verifier accepted the evidence digest.",
        }
    if int(coverage.get("high_authority_source_count", 0) or 0) < 2:
        return {
            "verifier_decision": "insufficient_authority",
            "open_gaps": list(open_gaps or []),
            "semantic_sufficiency": False,
            "reason": "Fallback semantic verifier detected weak authority support.",
        }
    return {
        "verifier_decision": "needs_backfill",
        "open_gaps": list(open_gaps or []),
        "semantic_sufficiency": False,
        "reason": "Fallback semantic verifier detected unresolved evidence gaps.",
    }


async def verify_evidence_digest(
    *,
    task_desc: str,
    evidence_digest: EvidenceDigest,
    code_gate_passed: bool,
) -> dict[str, Any]:
    fallback = fallback_verifier_assessment(
        coverage_summary=evidence_digest.get("coverage_summary"),
        open_gaps=evidence_digest.get("open_gaps"),
        code_gate_passed=code_gate_passed,
    )
    prompt = f"""
You are the Evidence Verifier Agent.

Judge whether the current evidence is semantically sufficient.
Return strict JSON:
{{
  "verifier_decision": "ready_for_writer|needs_backfill|insufficient_authority|degrade",
  "semantic_sufficiency": true,
  "open_gaps": [{{"slot_id": "1", "gap_reason": "short reason"}}],
  "reason": "short explanation"
}}

Definition:
- Formal coverage can pass while semantic sufficiency still fails.
- semantic_sufficiency means the evidence content is strong enough to support a direct answer or safe writing.
- If the evidence is formally present but still too shallow, indirect, or off-target, mark semantic_sufficiency=false.

Task:
{task_desc}

Code gate passed:
{code_gate_passed}

Evidence digest:
{evidence_digest}
"""
    parsed = await _safe_json_invoke(llm_smart, prompt)
    if not isinstance(parsed, dict):
        return fallback
    verifier_decision = str(parsed.get("verifier_decision") or "").strip().lower()
    if verifier_decision not in {"ready_for_writer", "needs_backfill", "insufficient_authority", "degrade"}:
        return fallback
    return {
        "verifier_decision": verifier_decision,
        "open_gaps": list(parsed.get("open_gaps") or fallback["open_gaps"]),
        "semantic_sufficiency": bool(parsed.get("semantic_sufficiency")),
        "reason": str(parsed.get("reason") or fallback["reason"]).strip(),
    }


def _merge_metrics(base: dict[str, int], updates: dict[str, int]) -> dict[str, int]:
    merged = dict(base)
    for key, value in updates.items():
        merged[key] = int(merged.get(key, 0)) + int(value)
    return merged


def _add_duration_metric(
    metrics: dict[str, float | int],
    key: str,
    elapsed_seconds: float,
) -> dict[str, float | int]:
    merged = dict(metrics)
    merged[key] = round(float(merged.get(key, 0.0) or 0.0) + max(0.0, float(elapsed_seconds)), 4)
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


def _needs_strict_evidence_gate(task_id: str | None) -> bool:
    task = get_task(task_id or "") or {}
    return str(task.get("backend") or "") == "drb_public_benchmark"


def build_open_gaps(
    *,
    task_contract: dict[str, Any],
    evidence_slots: dict[str, Any],
    slot_statuses: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    return [
        {
            "slot_id": slot_id,
            "question": evidence_slots.get(slot_id, {}).get("question")
            or next(
                (
                    point.get("question")
                    for point in task_contract.get("must_answer_points") or []
                    if str(point.get("id") or "") == slot_id
                ),
                "",
            ),
            "gap_reason": slot.get("gap_reason", ""),
        }
        for slot_id, slot in slot_statuses.items()
        if str(slot.get("status") or "") != "satisfied"
    ]


def route_for_research_outcome(
    *,
    gate_passed: bool,
    slot_statuses: dict[str, dict[str, Any]],
    progress_ledger: dict[str, Any],
    coverage: dict[str, Any],
    verifier_decision: str,
    semantic_sufficiency: bool,
) -> tuple[str, str]:
    if gate_passed and semantic_sufficiency:
        return "WRITE", "Research Team satisfied evidence gate; hand off to Writer Team."
    supported_slots = sum(1 for slot in slot_statuses.values() if str(slot.get("status") or "") != "unsupported")
    if int(progress_ledger.get("consecutive_no_improvement_backfills") or 0) >= 2:
        return "REPLAN", "Repeated backfills failed to improve coverage; replan research strategy."
    if int(progress_ledger.get("global_stall_count") or 0) >= 2:
        return "REPLAN", "Global stall threshold reached; replan before more research."
    if verifier_decision == "insufficient_authority":
        return "REPLAN", "Research Team found content coverage but insufficient high-authority support."
    if verifier_decision == "degrade":
        return "DEGRADED_WRITE", "Research Team found only bounded semantic support; prefer degraded writing."
    if supported_slots:
        return "DEGRADED_WRITE", "Some clauses are supported but high-authority coverage is incomplete; write degraded output."
    if float(coverage.get("task_clause_coverage_rate") or 0.0) <= 0.0:
        return "FAIL_HARD", "Research Team could not support any required clause."
    return "RESEARCH", "Research Team found open evidence gaps; continue bounded research."


def build_evidence_insufficiency_report(
    *,
    query: str,
    coverage_summary: dict[str, float | int],
    evidence_slots: dict[str, Any],
) -> str:
    supported_slots = [
        (slot_id, slot)
        for slot_id, slot in evidence_slots.items()
        if slot.get("covered") and int(slot.get("high_authority_source_count") or 0) >= 1
    ]
    if supported_slots:
        supported_lines: list[str] = []
        for slot_id, slot in supported_slots[:2]:
            source_url = next((str(url) for url in slot.get("source_urls") or [] if str(url).strip()), "")
            clause = str(slot.get("question") or f"Clause {slot_id}").strip()
            if source_url:
                supported_lines.append(f"- Best-supported clause {slot_id}: {clause} (Source: {source_url})")
            else:
                supported_lines.append(f"- Best-supported clause {slot_id}: {clause}")
        direct_answer_text = "\n".join(
            [
                "Current evidence is incomplete, but the strongest supported findings are:",
                *supported_lines,
                "Any broader conclusion should remain provisional until the missing clauses are supported by additional high-authority evidence.",
            ]
        )
    else:
        direct_answer_text = "Current evidence is insufficient to produce a reliable final answer."
    lines = [
        f"# {query}",
        "",
        "## Direct Answer / Core Conclusion",
        direct_answer_text,
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


async def run_research_team(
    state: dict[str, Any],
    *,
    safe_ainvoke_fn,
    mode_search_fn,
    fallback_task_contract_fn: Callable[[str, list[dict[str, Any]], list[dict[str, Any]]], dict[str, Any]],
    log_trajectory_fn,
    fetch_source_candidate_fn=fetch_source_candidate,
    tavily_extract_client_obj=tavily_extract_client,
    tavily_extract_credits_fn=tavily_extract_credits,
    tavily_map_client_obj=tavily_map_client,
    tavily_map_credits_fn=tavily_map_credits,
    tavily_crawl_client_obj=tavily_crawl_client,
    tavily_crawl_credits_fn=tavily_crawl_credits,
) -> dict[str, Any]:
    evidence_acquisition_started = time.perf_counter()
    km = get_current_km()
    task_id = str(state.get("task_id") or get_current_session_id())
    mode = str(state.get("research_mode") or DEFAULT_RESEARCH_MODE).strip().lower()
    architecture_mode = normalize_architecture_mode(state.get("architecture_mode"))
    task_contract = dict(
        state.get("task_contract")
        or fallback_task_contract_fn(
            str(state.get("query") or ""),
            state.get("outline", []),
            state.get("plan", []),
        )
    )
    task_ledger = dict(
        state.get("task_ledger")
        or build_task_ledger(
            query=str(state.get("query") or ""),
            task_contract=task_contract,
            plan=state.get("plan", []),
        )
    )
    previous_coverage_summary = dict(state.get("coverage_summary") or {})
    progress_ledger = build_progress_ledger(state.get("progress_ledger"))
    history = list(state.get("history") or [])
    missing_sources = list(state.get("missing_sources") or [])
    degraded_items = list(state.get("degraded_items") or [])
    cost_breakdown = dict(state.get("cost_breakdown") or default_cost_breakdown())
    retrieval_metrics = dict(state.get("retrieval_metrics") or default_retrieval_metrics())
    source_candidates = list(state.get("source_candidates") or [])
    fetch_results = list(state.get("fetch_results") or [])
    backfill_attempts_total = int(state.get("backfill_attempts") or 0)
    pdf_host_quarantine: set[str] = set()
    same_host_access_backfill_hosts: set[str] = set()
    prior_evidence_slots = dict(state.get("evidence_slots") or {})
    prior_slot_statuses = build_slot_statuses(prior_evidence_slots)
    prior_clause_statuses = build_clause_statuses(task_contract=task_contract, slot_statuses=prior_slot_statuses)
    prior_open_gaps = build_open_gaps(
        task_contract=task_contract,
        evidence_slots=prior_evidence_slots,
        slot_statuses=prior_slot_statuses,
    )
    if architecture_mode == "supervisor_team":
        retrieval_plan = await build_retrieval_plan(
            task_desc=str(state.get("query") or ""),
            clause_statuses=prior_clause_statuses,
            slot_statuses=prior_slot_statuses,
            open_gaps=prior_open_gaps,
            progress_ledger=progress_ledger,
        )
    else:
        retrieval_plan = default_retrieval_plan(
            target_clauses=[str(item.get("slot_id") or "") for item in prior_open_gaps if str(item.get("slot_id") or "").strip()],
            source_type_priority=[],
            query_intents=[str(state.get("query") or "")],
            backfill_mode="broad_recall",
            stop_after_slots=[],
        )

    async def process_task(task_item: dict[str, Any]) -> dict[str, Any]:
        nonlocal cost_breakdown, retrieval_metrics, source_candidates, fetch_results, backfill_attempts_total

        local_logs: list[dict[str, Any]] = []
        local_missing: list[dict[str, Any]] = []
        local_degraded: list[dict[str, Any]] = []
        task_desc = task_item.get("task", state.get("query", ""))
        section_id = task_item.get("section_id", "global")

        query_prompt = f"""
Return strict JSON only:
{{"queries": ["search keywords"]}}

Task: {task_desc}
"""
        queries = [str(item).strip() for item in retrieval_plan.get("query_intents") or [] if str(item).strip()] or [task_desc]
        resp = await safe_ainvoke_fn(llm_fast, query_prompt)
        if resp:
            try:
                parsed = clean_json_output(resp.content, strict=True)
                if isinstance(parsed, dict) and isinstance(parsed.get("queries"), list) and parsed["queries"]:
                    merged_queries: list[str] = []
                    for candidate_query in list(queries) + list(parsed["queries"]):
                        normalized_query = str(candidate_query).strip()
                        if normalized_query and normalized_query not in merged_queries:
                            merged_queries.append(normalized_query)
                    queries = merged_queries[:3]
            except LLMFormatError as exc:
                local_degraded.append(
                    {
                        "task": task_desc,
                        "stage": "query_generation",
                        "reason": f"JSON repair fallback: {exc.parse_error}",
                    }
                )

        local_logs.append({"role": "query_gen", "content": {"task_desc": task_desc, "generated_queries": queries}})

        search_seed = next((str(query).strip() for query in queries if str(query).strip()), task_desc)
        authority_queries = build_authority_queries_from_plan(task_desc, retrieval_plan) or _build_backfill_queries(task_desc)
        max_results = 5 if mode == "high" else 4

        async def search_once(search_query: str, max_results: int) -> list[dict[str, Any]]:
            nonlocal cost_breakdown, retrieval_metrics
            results, cost_breakdown, retrieval_metrics = await mode_search_fn(
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
                        "elapsed_ms": round(float(fetched.get("fetch_wall_seconds") or 0.0) * 1000.0, 3),
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
                        "elapsed_ms": float(attempt.get("elapsed_ms") or 0.0),
                    }
                )
            blocked_count = sum(1 for attempt in attempts if _is_blocked_fetch_error(str(attempt.get("error_class") or "")))
            return len(attempts), blocked_count

        async def access_backfill(item: dict[str, Any], *, stage: str) -> bool:
            nonlocal backfill_attempts_total, retrieval_metrics
            started = time.perf_counter()
            try:
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
                    local_degraded.append({"task": task_desc, "stage": "access_backfill", "reason": f"same-host recall failed for {host}: {exc}"})
                    return False
                qualified_same_host = qualify_search_results(same_host_results, task_desc, limit=4)
                alternatives = rank_access_backfill_candidates(item, qualified_same_host)[:2]
                if not alternatives:
                    local_degraded.append({"task": task_desc, "stage": "access_backfill", "reason": f"no same-host fallback candidate for {host} via query={query}"})
                    return False
                for candidate in alternatives:
                    success = await ingest_candidate(
                        candidate,
                        allow_visual=bool(candidate.get("source_tier") == "high_authority"),
                        stage=f"{stage}_access_backfill",
                        allow_access_backfill=False,
                    )
                    if success:
                        local_logs.append({"role": "access_backfill", "content": {"task": task_desc, "host": host, "url": candidate.get("url", "")}})
                        retrieval_metrics = _merge_metrics(retrieval_metrics, {"same_host_backfill_successes": 1})
                        return True
                retrieval_metrics = _merge_metrics(retrieval_metrics, {"blocked_after_same_host_backfill": 1})
                return False
            finally:
                retrieval_metrics = _add_duration_metric(retrieval_metrics, "access_backfill_wall_seconds", time.perf_counter() - started)

        async def ingest_candidate(
            item: dict[str, Any],
            *,
            allow_visual: bool,
            stage: str,
            count_as_fallback: bool = True,
            allow_access_backfill: bool = True,
        ) -> bool:
            nonlocal retrieval_metrics, cost_breakdown

            url = item.get("url", "")
            title = item.get("title", "")
            host = str(item.get("host") or _result_host(str(url))).strip().lower()
            if not url or km.is_duplicate(url):
                return False

            if allow_access_backfill and should_force_access_backfill(item, quarantined_pdf_hosts=pdf_host_quarantine):
                if await access_backfill(item, stage=f"{stage}_pdf_access_backfill"):
                    return True
                local_missing.append({"task": task_desc, "url": url, "reason": "pdf_access_backfill_failed", "provider": "access_backfill", "final_url": url})
                return False

            if count_as_fallback:
                retrieval_metrics = _merge_metrics(retrieval_metrics, {"fallback_count": 1})
            fetched = await fetch_source_candidate_fn(
                item,
                allow_visual=allow_visual,
                goal=f"Extract the charts, tables, formulas and visual facts relevant to: {task_desc}",
            )
            if should_quarantine_pdf_host(item, fetched) and host:
                pdf_host_quarantine.add(host)
            attempt_count, blocked_count = record_fetch_attempts(item, fetched, stage=stage)
            retrieval_metrics = _merge_metrics(retrieval_metrics, {"fetch_attempts": attempt_count, "blocked_fetches": blocked_count})
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
                if allow_access_backfill and not forced_html_backfill and generic_backfill_allowed and item.get("source_tier") == "high_authority":
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
                local_missing.append({"task": task_desc, "url": url, "reason": fetched.get("error_class") or "content too short or blocked"})
                return False

            inserted = km.add_compact_document(
                compact,
                url,
                title,
                section_id=section_id,
                extra_metadata={
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
                },
            )
            if not inserted:
                return False

            retrieval_metrics = _merge_metrics(retrieval_metrics, {"high_value_evidence_count": 1 if float(item.get("authority_score") or 0.0) >= 0.75 else 0})
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

        recall_started = time.perf_counter()
        try:
            recall = await staged_candidate_recall(
                query=search_seed,
                task_desc=task_desc,
                max_results=max_results,
                search_fn=search_once,
                primary_query=search_seed,
                authority_queries=authority_queries,
                strict_topic_override=(True if str(retrieval_plan.get("backfill_mode") or "").lower() == "authority_first" else None),
            )
        except ToolExecutionError as exc:
            local_missing.append({"task": task_desc, "query": search_seed, "url": exc.url, "reason": f"Search failed with HTTP {exc.status_code}"})
            local_degraded.append({"task": task_desc, "stage": "search", "reason": "deterministic skip after tool failure"})
            return {"logs": local_logs, "missing_sources": local_missing, "degraded_items": local_degraded}
        except Exception as exc:
            local_missing.append({"task": task_desc, "query": search_seed, "reason": str(exc)})
            return {"logs": local_logs, "missing_sources": local_missing, "degraded_items": local_degraded}
        finally:
            retrieval_metrics = _add_duration_metric(retrieval_metrics, "retrieval_recall_wall_seconds", time.perf_counter() - recall_started)

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
            local_degraded.append({"task": task_desc, "stage": "qualification", "reason": f"no admissible sources for topic_family={recall.get('topic_family', 'general')}"})
            return {"logs": local_logs, "missing_sources": local_missing, "degraded_items": local_degraded}

        if mode == "low":
            for item in admitted_candidates[:3]:
                await ingest_candidate(item, allow_visual=False, stage="low_authority_fetch", count_as_fallback=False)
            return {"logs": local_logs, "missing_sources": local_missing, "degraded_items": local_degraded}

        if mode == "medium":
            urls = [str(item.get("url") or "") for item in admitted_candidates if str(item.get("url") or "")]
            extracted_urls: set[str] = set()
            if urls and tavily_extract_client_obj.is_configured():
                try:
                    extract_response = await tavily_extract_client_obj.aextract(
                        urls,
                        query=task_desc,
                        chunks_per_source=3,
                        extract_depth="basic",
                    )
                    extract_results = extract_response.get("results", [])
                    if extract_results:
                        cost_breakdown = record_tavily_credits(
                            cost_breakdown,
                            tavily_extract_credits_fn(len(extract_results), "basic"),
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
                            retrieval_metrics = _merge_metrics(retrieval_metrics, {"high_value_evidence_count": 1 if float(candidate.get("authority_score", 0.0)) >= 0.75 else 0})
                except ToolExecutionError as exc:
                    local_degraded.append({"task": task_desc, "query": search_seed, "stage": "extract", "reason": f"Tavily extract failed with HTTP {exc.status_code}"})
            for item in admitted_candidates:
                if item.get("url") in extracted_urls:
                    continue
                await ingest_candidate(item, allow_visual=True, stage="medium_authority_fetch")
            return {"logs": local_logs, "missing_sources": local_missing, "degraded_items": local_degraded}

        search_results = list(all_candidates or admitted_candidates)
        primary_domain, primary_url = _select_primary_domain(search_results)
        mapped_urls: list[str] = []
        crawled_results: list[dict[str, Any]] = []
        if primary_domain and primary_url and tavily_map_client_obj.is_configured():
            try:
                map_response = await tavily_map_client_obj.amap(
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
                        tavily_map_credits_fn(len(mapped_urls), False),
                    )
                    retrieval_metrics = _merge_metrics(retrieval_metrics, {"map_calls": 1})
            except ToolExecutionError as exc:
                local_degraded.append({"task": task_desc, "query": search_seed, "stage": "map", "reason": f"Tavily map failed with HTTP {exc.status_code}"})
        if len(mapped_urls) >= 3 and primary_url and tavily_crawl_client_obj.is_configured():
            try:
                crawl_response = await tavily_crawl_client_obj.acrawl(
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
                        tavily_crawl_credits_fn(len(crawled_results), extract_depth="advanced"),
                    )
                    retrieval_metrics = _merge_metrics(retrieval_metrics, {"crawl_calls": 1})
            except ToolExecutionError as exc:
                local_degraded.append({"task": task_desc, "query": search_seed, "stage": "crawl", "reason": f"Tavily crawl failed with HTTP {exc.status_code}"})
        selected_urls = _choose_high_value_urls(search_results, mapped_urls, crawled_results, limit=5)
        search_lookup = {item.get("url", ""): item for item in search_results if item.get("url")}
        crawl_lookup = {item.get("url", ""): item for item in crawled_results if item.get("url")}
        extracted_urls: set[str] = set()
        if selected_urls and tavily_extract_client_obj.is_configured():
            try:
                extract_response = await tavily_extract_client_obj.aextract(
                    selected_urls,
                    query=task_desc,
                    chunks_per_source=5,
                    extract_depth="advanced",
                )
                extract_results = extract_response.get("results", [])
                if extract_results:
                    cost_breakdown = record_tavily_credits(
                        cost_breakdown,
                        tavily_extract_credits_fn(len(extract_results), "advanced"),
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
                        retrieval_metrics = _merge_metrics(retrieval_metrics, {"high_value_evidence_count": 1 if float(candidate.get("authority_score", 0.0)) >= 0.75 else 0})
            except ToolExecutionError as exc:
                local_degraded.append({"task": task_desc, "query": search_seed, "stage": "extract", "reason": f"Tavily extract failed with HTTP {exc.status_code}"})
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
                    retrieval_metrics = _merge_metrics(retrieval_metrics, {"high_value_evidence_count": 1 if float(candidate.get("authority_score", 0.0)) >= 0.75 else 0})
                    continue
            await ingest_candidate(candidate, allow_visual=True, stage="high_authority_fetch")
        return {"logs": local_logs, "missing_sources": local_missing, "degraded_items": local_degraded}

    async def targeted_backfill(task_item: dict[str, Any]) -> dict[str, Any]:
        nonlocal cost_breakdown, retrieval_metrics, backfill_attempts_total, fetch_results
        targeted_backfill_started = time.perf_counter()

        try:
            logs: list[dict[str, Any]] = []
            local_missing: list[dict[str, Any]] = []
            local_degraded: list[dict[str, Any]] = []
            section_id = task_item.get("section_id", "global")
            task_desc = task_item.get("task", state.get("query", ""))
            inserted_any = False

            async def targeted_access_backfill(item: dict[str, Any], *, stage: str) -> bool:
                nonlocal backfill_attempts_total, retrieval_metrics, cost_breakdown, fetch_results
                started = time.perf_counter()
                try:
                    host = str(item.get("host") or _result_host(str(item.get("url") or ""))).strip().lower()
                    if not host:
                        return False
                    backfill_attempts_total += 1
                    same_host_access_backfill_hosts.add(host)
                    retrieval_metrics = _merge_metrics(retrieval_metrics, {"same_host_backfill_attempts": 1})
                    query = build_access_backfill_query(item, task_desc)
                    try:
                        search_results, cost_breakdown, retrieval_metrics = await mode_search_fn(
                            mode,
                            query,
                            max_results=4,
                            cost_breakdown=cost_breakdown,
                            retrieval_metrics=retrieval_metrics,
                        )
                    except Exception as exc:
                        local_degraded.append({"task": task_desc, "stage": "access_backfill", "reason": f"same-host recall failed for {host}: {exc}"})
                        return False
                    qualified_same_host = qualify_search_results(search_results, task_desc, limit=4)
                    alternatives = rank_access_backfill_candidates(item, qualified_same_host)[:2]
                    if not alternatives:
                        local_degraded.append({"task": task_desc, "stage": "access_backfill", "reason": f"no same-host fallback candidate for {host} via query={query}"})
                        return False
                    for candidate in alternatives:
                        url = candidate.get("url", "")
                        if not url or km.is_duplicate(url):
                            continue
                        fetched = await fetch_source_candidate_fn(
                            candidate,
                            allow_visual=bool(candidate.get("source_tier") == "high_authority"),
                            goal=f"Fetch same-host fallback support for {task_desc}",
                        )
                        attempts = list(fetched.get("attempts") or [])
                        if not attempts:
                            attempts = [
                                {
                                    "provider": fetched.get("provider") or "access_backfill",
                                    "status": fetched.get("status") or "failed",
                                    "page_type": fetched.get("page_type") or "",
                                    "host": fetched.get("host") or candidate.get("host") or _result_host(str(url)),
                                    "error_class": fetched.get("error_class") or "",
                                    "http_status": int(fetched.get("http_status") or 0),
                                    "content_type": fetched.get("content_type") or "",
                                    "content_length": int(fetched.get("content_length") or 0),
                                    "authority_preserved": bool(fetched.get("authority_preserved")),
                                    "attempt_order": 1,
                                    "salvaged_by_fallback": bool(fetched.get("salvaged_by_fallback")),
                                    "blocked_stage": fetched.get("blocked_stage") or "",
                                    "final_url": fetched.get("final_url") or url,
                                    "elapsed_ms": round(float(fetched.get("fetch_wall_seconds") or 0.0) * 1000.0, 3),
                                }
                            ]
                        for attempt in attempts:
                            fetch_results.append(
                                {
                                    "task": task_desc,
                                    "url": url,
                                    "provider": attempt.get("provider") or "access_backfill",
                                    "status": attempt.get("status") or "failed",
                                    "page_type": attempt.get("page_type") or fetched.get("page_type") or "",
                                    "host": attempt.get("host") or candidate.get("host") or _result_host(str(url)),
                                    "error_class": attempt.get("error_class") or "",
                                    "http_status": int(attempt.get("http_status") or 0),
                                    "content_type": attempt.get("content_type") or "",
                                    "content_length": int(attempt.get("content_length") or 0),
                                    "authority_preserved": bool(attempt.get("authority_preserved")),
                                    "source_tier": candidate.get("source_tier", "standard"),
                                    "attempt_order": int(attempt.get("attempt_order") or 0),
                                    "salvaged_by_fallback": bool(attempt.get("salvaged_by_fallback")),
                                    "blocked_stage": attempt.get("blocked_stage") or "",
                                    "final_url": attempt.get("final_url") or fetched.get("final_url") or url,
                                    "elapsed_ms": float(attempt.get("elapsed_ms") or 0.0),
                                }
                            )
                        retrieval_metrics = _merge_metrics(
                            retrieval_metrics,
                            {
                                "fetch_attempts": len(attempts),
                                "blocked_fetches": sum(1 for attempt in attempts if _is_blocked_fetch_error(str(attempt.get("error_class") or ""))),
                            },
                        )
                        if fetched.get("credits_est"):
                            cost_breakdown = record_tavily_credits(cost_breakdown, float(fetched.get("credits_est") or 0.0))
                            retrieval_metrics = _merge_metrics(retrieval_metrics, {"extract_calls": 1})
                        if fetched.get("status") != "ok":
                            continue
                        compact = _compact_text(str(fetched.get("content") or ""))
                        if not compact or len(compact) <= 120:
                            continue
                        inserted = km.add_compact_document(
                            compact,
                            url,
                            candidate.get("title", ""),
                            section_id=section_id,
                            extra_metadata={
                                "source_tier": candidate.get("source_tier", "standard"),
                                "authority_score": float(candidate.get("authority_score", 0.0)),
                                "fetch_provider": fetched.get("provider") or "access_backfill",
                                "fetch_status": fetched.get("status") or "ok",
                            },
                        )
                        if inserted:
                            retrieval_metrics = _merge_metrics(
                                retrieval_metrics,
                                {
                                    "same_host_backfill_successes": 1,
                                    "successful_authority_fetches": 1 if candidate.get("source_tier") == "high_authority" else 0,
                                    "high_value_evidence_count": 1 if float(candidate.get("authority_score", 0.0)) >= 0.75 else 0,
                                },
                            )
                            logs.append({"role": "access_backfill", "content": {"task": task_desc, "host": host, "url": url}})
                            return True
                    retrieval_metrics = _merge_metrics(retrieval_metrics, {"blocked_after_same_host_backfill": 1})
                    return False
                finally:
                    retrieval_metrics = _add_duration_metric(retrieval_metrics, "access_backfill_wall_seconds", time.perf_counter() - started)

            backfill_queries = build_authority_queries_from_plan(task_desc, retrieval_plan) or _build_backfill_queries(task_desc)
            for query in backfill_queries[:1]:
                backfill_attempts_total += 1
                try:
                    search_results, cost_breakdown, retrieval_metrics = await mode_search_fn(
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
                        if await targeted_access_backfill(item, stage="targeted_backfill_pdf_access_backfill"):
                            inserted_any = True
                            break
                        local_missing.append({"task": task_desc, "query": query, "url": url, "reason": "pdf_access_backfill_failed"})
                        continue
                    retrieval_metrics = _merge_metrics(retrieval_metrics, {"fallback_count": 1})
                    fetched = await fetch_source_candidate_fn(
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
                                "elapsed_ms": round(float(fetched.get("fetch_wall_seconds") or 0.0) * 1000.0, 3),
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
                                "elapsed_ms": float(attempt.get("elapsed_ms") or 0.0),
                            }
                        )
                    retrieval_metrics = _merge_metrics(
                        retrieval_metrics,
                        {
                            "fetch_attempts": len(attempts),
                            "blocked_fetches": sum(1 for attempt in attempts if _is_blocked_fetch_error(str(attempt.get("error_class") or ""))),
                        },
                    )
                    if fetched.get("credits_est"):
                        cost_breakdown = record_tavily_credits(cost_breakdown, float(fetched.get("credits_est") or 0.0))
                        retrieval_metrics = _merge_metrics(retrieval_metrics, {"extract_calls": 1})
                    if fetched.get("status") != "ok":
                        forced_html_backfill = should_force_non_pdf_access_backfill(item, fetched, attempted_hosts=same_host_access_backfill_hosts)
                        if forced_html_backfill and await targeted_access_backfill(item, stage="targeted_backfill_html_access_backfill"):
                            inserted_any = True
                            break
                        local_missing.append({"task": task_desc, "query": query, "url": url, "reason": fetched.get("error_class") or "backfill_fetch_failed"})
                        continue
                    compact = _compact_text(str(fetched.get("content") or ""))
                    if not compact or len(compact) <= 120:
                        local_missing.append({"task": task_desc, "query": query, "url": url, "reason": "backfill_empty_content"})
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
                                "successful_authority_fetches": 1 if item.get("source_tier") == "high_authority" else 0,
                                "high_value_evidence_count": 1 if float(item.get("authority_score", 0.0)) >= 0.75 else 0,
                            },
                        )
                        logs.append({"role": "targeted_backfill", "content": {"task": task_desc, "query": query, "url": url, "source_tier": item.get("source_tier", "standard")}})
                if inserted_any:
                    break
                local_degraded.append({"task": task_desc, "stage": "targeted_backfill", "reason": f"no authoritative support added for query={query}"})
            return {"logs": logs, "missing_sources": local_missing, "degraded_items": local_degraded, "success": inserted_any}
        finally:
            retrieval_metrics = _add_duration_metric(retrieval_metrics, "targeted_backfill_wall_seconds", time.perf_counter() - targeted_backfill_started)

    for task_item in state.get("plan", []):
        result = await process_task(task_item)
        history.extend(result["logs"])
        missing_sources.extend(result["missing_sources"])
        degraded_items.extend(result["degraded_items"])

    backfill_successes = 0
    evidence_slots = build_evidence_slots(task_contract=task_contract, km=km)
    coverage = compute_coverage_summary(
        plan=state.get("plan", []),
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
        uncovered_sections = {str(slot.get("section_id") or "global") for slot in evidence_slots.values() if not slot.get("covered")}
        for task_item in state.get("plan", []):
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
            plan=state.get("plan", []),
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
                "task": state.get("query", ""),
                "stage": "evidence_gate",
                "reason": (
                    f"coverage={coverage['evidence_coverage_rate']}, "
                    f"high_authority_sources={coverage['high_authority_source_count']}, "
                    f"task_clause_coverage={coverage['task_clause_coverage_rate']}"
                ),
            }
        )

    metrics = state.get("metrics", {"tool_calls": 0, "backtracking": 0})
    retrieval_metrics = _add_duration_metric(retrieval_metrics, "evidence_acquisition_wall_seconds", time.perf_counter() - evidence_acquisition_started)
    metrics["tool_calls"] = (
        metrics.get("tool_calls", 0)
        + int(retrieval_metrics.get("search_calls", 0))
        + int(retrieval_metrics.get("extract_calls", 0))
        + int(retrieval_metrics.get("map_calls", 0))
        + int(retrieval_metrics.get("crawl_calls", 0))
        + int(retrieval_metrics.get("fallback_count", 0))
        + int(retrieval_metrics.get("visual_browse_calls", 0))
    )

    log_trajectory_fn(
        task_id,
        "executor",
        {
            "mode": mode,
            "tasks": len(state.get("plan", [])),
            "missing_sources": len(missing_sources),
            "degraded_items": len(degraded_items),
            "cost_breakdown": cost_breakdown,
            "retrieval_metrics": {**retrieval_metrics, **coverage},
            "evidence_slots": evidence_slots,
        },
    )

    slot_statuses = build_slot_statuses(evidence_slots)
    clause_statuses = build_clause_statuses(task_contract=task_contract, slot_statuses=slot_statuses)
    open_gaps = build_open_gaps(task_contract=task_contract, evidence_slots=evidence_slots, slot_statuses=slot_statuses)
    evidence_digest = build_evidence_digest(
        task_contract=task_contract,
        evidence_slots=evidence_slots,
        slot_statuses=slot_statuses,
        clause_statuses=clause_statuses,
        open_gaps=open_gaps,
        coverage_summary=coverage,
        km=km,
    )
    if architecture_mode == "supervisor_team":
        verifier_assessment = await verify_evidence_digest(task_desc=str(state.get("query") or ""), evidence_digest=evidence_digest, code_gate_passed=gate["passed"])
        verifier_decision = str(verifier_assessment.get("verifier_decision") or "needs_backfill")
        semantic_sufficiency = bool(verifier_assessment.get("semantic_sufficiency"))
    else:
        if gate["passed"]:
            verifier_decision = "ready_for_writer"
        elif gate.get("missing_slot_ids"):
            verifier_decision = "needs_backfill"
        elif (
            int(coverage.get("high_authority_source_count") or 0) < int(gate.get("required_high_authority_sources") or 2)
            or float(coverage.get("direct_answer_support_rate") or 0.0) < float(gate.get("required_direct_answer_support_rate") or 1.0)
        ):
            verifier_decision = "insufficient_authority"
        else:
            verifier_decision = "unsupported"
        verifier_assessment = {
            "verifier_decision": verifier_decision,
            "open_gaps": open_gaps,
            "semantic_sufficiency": gate["passed"],
            "reason": "Legacy workflow preserved code-only evidence gate semantics.",
        }
        semantic_sufficiency = gate["passed"]
    team_status = "ok" if gate["passed"] and semantic_sufficiency else "team_stalled"

    no_improvement = (not gate["passed"] or not semantic_sufficiency) and backfill_made_no_improvement(
        previous_coverage_summary=previous_coverage_summary,
        current_coverage_summary=coverage,
    )
    progress_ledger = update_progress_ledger(
        progress_ledger,
        last_team_called="Research Team",
        next_action_rationale=(
            "Evidence gate passed; prepare writer handoff."
            if gate["passed"] and semantic_sufficiency
            else "Evidence gate passed formally, but semantic sufficiency is still missing."
            if gate["passed"]
            else "Research Team returned unresolved evidence gaps."
        ),
        clause_statuses=clause_statuses,
        open_gaps=open_gaps,
        team_stall_delta=0 if gate["passed"] else 1,
        global_stall_delta=0 if gate["passed"] else 1,
        reset_team_stall=gate["passed"],
        reset_global_stall=gate["passed"],
        no_improvement_increment=no_improvement,
        reset_no_improvement=gate["passed"],
        verifier_decision=verifier_decision,
    )
    verification_status = dict(progress_ledger.get("verification_status") or {})
    verification_status.update(
        {
            "semantic_sufficiency": semantic_sufficiency,
            "semantic_verifier_reason": str(verifier_assessment.get("reason") or ""),
        }
    )
    progress_ledger["verification_status"] = verification_status
    next_phase, _next_reason = route_for_research_outcome(
        gate_passed=gate["passed"],
        slot_statuses=slot_statuses,
        progress_ledger=progress_ledger,
        coverage=coverage,
        verifier_decision=verifier_decision,
        semantic_sufficiency=semantic_sufficiency,
    )
    research_team_result = {
        "status": team_status,
        "slot_statuses": slot_statuses,
        "clause_statuses": clause_statuses,
        "coverage_summary": coverage,
        "open_gaps": open_gaps,
        "bundle_ref": "",
        "recommended_next_step": next_phase.lower(),
        "team_confidence": round(float(coverage.get("task_clause_coverage_rate") or 0.0), 4),
        "verifier_decision": verifier_decision,
        "semantic_sufficiency": semantic_sufficiency,
        "semantic_verifier_reason": str(verifier_assessment.get("reason") or ""),
    }
    bundle_ref = save_json_artifact(
        task_id=task_id or "unknown-task",
        architecture_mode=architecture_mode,
        artifact_name="evidence_bundle.json",
        payload={
            "query": state.get("query", ""),
            "task_contract": task_contract,
            "evidence_slots": evidence_slots,
            "slot_statuses": slot_statuses,
            "clause_statuses": clause_statuses,
            "coverage_summary": coverage,
            "source_candidates": source_candidates,
            "fetch_results": fetch_results,
            "backfill_attempts": backfill_attempts_total,
            "retrieval_plan": retrieval_plan,
            "evidence_digest": evidence_digest,
        },
    )
    research_team_result["bundle_ref"] = bundle_ref
    final_report = state.get("final_report", "")
    if architecture_mode == "legacy_workflow":
        if not gate["passed"]:
            final_report = build_evidence_insufficiency_report(query=str(state.get("query") or ""), coverage_summary=coverage, evidence_slots=evidence_slots)
            next_phase = "DONE"
        else:
            next_phase = "WRITE"
    elif next_phase == "FAIL_HARD":
        final_report = build_evidence_insufficiency_report(query=str(state.get("query") or ""), coverage_summary=coverage, evidence_slots=evidence_slots)

    return {
        "metrics": metrics,
        "history": history,
        "conflict_detected": bool(state.get("conflict_detected", False)),
        "conflict_count": int(state.get("conflict_count", 0)),
        "missing_sources": missing_sources,
        "degraded_items": degraded_items,
        "task_ledger": task_ledger,
        "progress_ledger": progress_ledger,
        "task_contract": task_contract,
        "evidence_slots": evidence_slots,
        "research_team_result": research_team_result,
        "retrieval_plan": retrieval_plan,
        "evidence_digest": evidence_digest,
        "draft_audit": state.get("draft_audit", {}),
        "cost_breakdown": cost_breakdown,
        "retrieval_metrics": {**retrieval_metrics, **coverage},
        "source_candidates": source_candidates,
        "fetch_results": fetch_results,
        "coverage_summary": coverage,
        "backfill_attempts": backfill_attempts_total,
        "retrieval_failed": (not gate["passed"]) or (not semantic_sufficiency),
        "bundle_ref": bundle_ref,
        "current_phase": next_phase,
        "final_report": final_report,
    }


__all__ = [
    "EvidenceDigest",
    "RetrievalPlan",
    "build_authority_queries_from_plan",
    "build_evidence_digest",
    "build_evidence_insufficiency_report",
    "build_open_gaps",
    "build_retrieval_plan",
    "default_source_type_priority",
    "fallback_verifier_assessment",
    "route_for_research_outcome",
    "run_research_team",
    "verify_evidence_digest",
]
