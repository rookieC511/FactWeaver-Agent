import asyncio
import json
import operator
import os
import re
from typing import Annotated, Any, Dict, List, TypedDict

from langchain_core.messages import HumanMessage
from langgraph.graph import END, StateGraph

try:
    from langgraph.types import Send
except Exception:  # pragma: no cover - compatibility for lightweight test stubs
    class Send(dict):
        def __init__(self, node: str, arg):
            super().__init__(node=node, arg=arg)

from core.memory import get_current_km
from core.models import llm_chief, llm_smart, llm_worker
from core.tools import LLMFormatError, clean_json_output

try:
    from core.checkpoint import get_sqlite_checkpointer
    from core.config import CHECKPOINT_DB_PATH
except Exception:  # pragma: no cover - lightweight test imports
    get_sqlite_checkpointer = None  # type: ignore[assignment]
    CHECKPOINT_DB_PATH = ""


ANALYSIS_SIGNAL_MAP = {
    "comparison": ("compare", "versus", "vs", "better than", "relative to", "comparison", "相比", "对比", "更高", "更低"),
    "causal": ("because", "driven by", "caused by", "due to", "therefore", "原因", "导致", "因为", "驱动"),
    "risk": ("risk", "limitation", "constraint", "uncertainty", "caveat", "风险", "限制", "不确定", "隐患"),
}
STOPWORDS = {
    "the",
    "and",
    "for",
    "with",
    "that",
    "this",
    "from",
    "what",
    "when",
    "which",
    "into",
    "about",
    "their",
    "there",
    "have",
    "has",
    "will",
    "does",
    "how",
    "why",
}


class Section(TypedDict):
    id: str
    title: str
    description: str
    content: str


class WriterState(TypedDict):
    query: str
    outline: List[Section]
    sections: Annotated[Dict[str, str], operator.ior]
    writer_runtime: Annotated[Dict[str, dict[str, Any]], operator.ior]
    final_doc: str
    iteration: int
    user_feedback: str
    task_id: str
    writer_context_mode: str
    task_contract: dict[str, Any]
    evidence_slots: dict[str, Any]
    draft_audit: dict[str, Any]
    report_verifier: dict[str, Any]
    audit_revision_count: int
    required_analysis_modes: list[str]


WRITER_CONTEXT_SECTION_SCOPED = "section_scoped"
WRITER_CONTEXT_LEGACY_FULL = "legacy_full_context"
SECTION_RE = re.compile(r"(?ms)^##\s+(.+?)\n(.*?)(?=^##\s+|\Z)")


def resolve_writer_context_mode(explicit_mode: str | None = None) -> str:
    mode = (explicit_mode or os.getenv("FACTWEAVER_WRITER_CONTEXT_MODE", WRITER_CONTEXT_SECTION_SCOPED)).strip().lower()
    if mode not in {WRITER_CONTEXT_SECTION_SCOPED, WRITER_CONTEXT_LEGACY_FULL}:
        return WRITER_CONTEXT_SECTION_SCOPED
    return mode


def get_writer_thread_id(task_id: str) -> str:
    return f"{task_id}:writer"


def _default_outline(query: str) -> list[dict]:
    return [
        {"id": "1", "title": "Background", "description": f"Define the problem space and context for {query}."},
        {"id": "2", "title": "Key Findings", "description": f"Summarize the strongest supported findings for {query}."},
        {"id": "3", "title": "Conclusion", "description": f"State the best-supported conclusion and remaining uncertainty for {query}."},
    ]


def _extract_sections(report_text: str) -> dict[str, str]:
    sections: dict[str, str] = {}
    for title, body in SECTION_RE.findall(report_text or ""):
        sections[title.strip().lower()] = body.strip()
    return sections


def _keyword_tokens(text: str) -> list[str]:
    return [
        token
        for token in re.findall(r"[A-Za-z0-9\u4e00-\u9fff]+", (text or "").lower())
        if len(token) >= 2 and token not in STOPWORDS
    ]


def _clause_text(clause: Any) -> str:
    if isinstance(clause, dict):
        return str(clause.get("question") or clause.get("title") or "").strip()
    return str(clause or "").strip()


def _analysis_flags(text: str) -> dict[str, bool]:
    lowered = (text or "").lower()
    flags = {}
    for name, patterns in ANALYSIS_SIGNAL_MAP.items():
        flags[name] = any(pattern in lowered for pattern in patterns)
    return flags


def _is_transient_writer_error(exc: Exception) -> bool:
    lowered = str(exc or "").lower()
    transient_tokens = (
        "connection error",
        "connection reset",
        "timed out",
        "timeout",
        "temporarily unavailable",
        "service unavailable",
        "502",
        "503",
        "504",
        "gateway",
        "network",
        "read timeout",
    )
    return any(token in lowered for token in transient_tokens)


def _compact_doc_excerpt(doc: Any, limit: int = 180) -> str:
    text = re.sub(r"\s+", " ", str(doc.page_content or "")).strip()
    if len(text) > limit:
        text = text[:limit].rstrip() + "..."
    citation_hash = str(doc.metadata.get("citation_hash") or "unknown")
    url = str(doc.metadata.get("url") or doc.metadata.get("source_url") or "")
    return f"- [HASH:{citation_hash} | URL:{url}] {text}"


def _fallback_section_from_docs(
    *,
    docs: list[Any],
    must_answer_points: list[dict[str, Any]],
    required_modes: list[str],
) -> str:
    evidence_lines = [_compact_doc_excerpt(doc) for doc in docs[:3] if str(doc.page_content or "").strip()]
    if not evidence_lines:
        evidence_lines = ["- No reliable section-specific evidence was available during fallback synthesis."]
    point_summary = "; ".join(
        _clause_text(point) for point in must_answer_points[:3] if _clause_text(point)
    ) or "the section question"
    analysis_sentences: list[str] = []
    if "comparison" in required_modes:
        analysis_sentences.append(
            "Comparison: the retrieved evidence is too fragmented to support a strong side-by-side conclusion."
        )
    if "causal" in required_modes:
        analysis_sentences.append(
            "Causal analysis: available sources indicate partial drivers, but the chain of evidence is incomplete."
        )
    if "risk" in required_modes:
        analysis_sentences.append(
            "Risk and limitations: conclusions should be treated as provisional until stronger sources are recovered."
        )
    if not analysis_sentences:
        analysis_sentences.append(
            "Analysis: the section fallback is grounded only in currently retrieved evidence and should be read as provisional."
        )
    return "\n".join(
        [
            f"Direct Answer: Based on the currently retrieved evidence, this section can only provide a bounded answer about {point_summary}.",
            "",
            "Key Evidence:",
            *evidence_lines,
            "",
            "Analysis:",
            *[f"- {sentence}" for sentence in analysis_sentences],
            "",
            "Uncertainty:",
            "- This section used an evidence-only fallback after a transient writer failure; no new facts were introduced.",
        ]
    )


def audit_final_doc(final_doc: str, query: str, task_contract: dict[str, Any] | None, evidence_slots: dict[str, Any] | None) -> dict[str, Any]:
    text = final_doc or ""
    sections = _extract_sections(text)
    direct_answer_text = sections.get("direct answer / core conclusion", "")
    analysis_text = sections.get("analysis", "")
    direct_answer_present = bool(direct_answer_text.strip())
    direct_answer_citation_backed = "[HASH:" in direct_answer_text or "http://" in direct_answer_text or "https://" in direct_answer_text

    must_answer_points = list((task_contract or {}).get("must_answer_points") or [])
    clause_hits = 0
    for clause in must_answer_points:
        clause_text = _clause_text(clause)
        if not clause_text:
            continue
        keywords = _keyword_tokens(clause_text)
        if clause_text.lower() in text.lower():
            clause_hits += 1
            continue
        if not keywords:
            continue
        matched = sum(1 for keyword in keywords[:6] if keyword in text.lower())
        if matched >= max(1, min(2, len(keywords) // 2)):
            clause_hits += 1
    clause_total = max(1, len(must_answer_points))
    task_clause_coverage_rate = round(clause_hits / clause_total, 4)

    flags = _analysis_flags(analysis_text)
    analysis_signal_count = sum(1 for value in flags.values() if value)
    high_value_slots = sum(
        1
        for slot in (evidence_slots or {}).values()
        if slot.get("covered") and slot.get("high_authority_source_count", 0) >= 1
    )
    missing_requirements: list[str] = []
    if not direct_answer_present:
        missing_requirements.append("missing_direct_answer")
    if not direct_answer_citation_backed:
        missing_requirements.append("direct_answer_not_citation_backed")
    if task_clause_coverage_rate < 0.8:
        missing_requirements.append("insufficient_task_clause_coverage")
    if analysis_signal_count < 2:
        missing_requirements.append("analysis_signals_too_weak")
    if must_answer_points and high_value_slots < max(1, len(must_answer_points) // 2):
        missing_requirements.append("evidence_slots_not_grounded")

    return {
        "direct_answer_present": direct_answer_present,
        "direct_answer_citation_backed": direct_answer_citation_backed,
        "task_clause_coverage_rate": task_clause_coverage_rate,
        "analysis_signal_count": analysis_signal_count,
        "comparison_present": flags["comparison"],
        "causal_present": flags["causal"],
        "risk_present": flags["risk"],
        "missing_requirements": missing_requirements,
        "passed": not missing_requirements,
    }


def _ensure_report_contract(final_doc: str, query: str, raw_draft: str) -> str:
    required_sections = (
        "## Direct Answer / Core Conclusion",
        "## Key Evidence",
        "## Analysis",
        "## Uncertainty / Missing Evidence",
    )
    if all(section in final_doc for section in required_sections):
        return final_doc
    return "\n".join(
        [
            f"# {query}",
            "",
            "## Direct Answer / Core Conclusion",
            "Evidence is incomplete. Use the synthesis below as the current best-supported answer.",
            "",
            "## Key Evidence",
            "The strongest cited evidence available from the draft sections is summarized below.",
            "",
            "## Analysis",
            raw_draft.strip(),
            "",
            "## Uncertainty / Missing Evidence",
            "Some sub-questions still lack authoritative support. Treat conclusions as provisional until stronger sources are fetched.",
        ]
    )


async def skeleton_node(state: WriterState):
    if state.get("outline"):
        return {"outline": state["outline"], "sections": {}}

    km = get_current_km()
    docs = km.retrieve(k=8)
    context_preview = "\n".join(f"- {doc.page_content[:120]}..." for doc in docs)
    prompt = f"""
Generate a writing outline for the research topic below.

Return strict JSON as a list or object containing sections with:
- id
- title
- description

Topic: {state['query']}
User feedback: {state.get('user_feedback', '')}
Evidence preview:
{context_preview}
"""
    outline_data = []
    try:
        resp = await llm_smart.ainvoke([HumanMessage(content=prompt)])
        parsed = clean_json_output(resp.content, strict=True)
        if isinstance(parsed, dict):
            outline_data = parsed.get("outline") or parsed.get("sections") or []
        elif isinstance(parsed, list):
            outline_data = parsed
    except LLMFormatError:
        outline_data = []
    except Exception:
        outline_data = []

    if not isinstance(outline_data, list) or not outline_data:
        outline_data = _default_outline(state["query"])

    return {"outline": outline_data, "sections": {}}


def human_review_node(state: WriterState):
    if os.environ.get("FACTWEAVER_API_MODE") == "1":
        return {"user_feedback": ""}

    print("\n[Writer Review] Outline:")
    for section in state["outline"]:
        print(f"  [{section.get('id')}] {section.get('title')}: {section.get('description')[:60]}")

    if state.get("user_feedback") == "SKIP_REVIEW":
        return {"user_feedback": ""}

    user_input = input("\nPress Enter to continue, type feedback to regenerate, q to quit: ").strip()
    if user_input.lower() == "q":
        raise KeyboardInterrupt("User terminated task")
    if user_input:
        return {"user_feedback": user_input, "outline": [], "iteration": state.get("iteration", 0) + 1}
    return {"user_feedback": ""}


async def section_writer_node(state: dict[str, Any]):
    km = get_current_km()
    section_id = state["id"]
    context_mode = resolve_writer_context_mode(state.get("writer_context_mode"))
    docs = km.retrieve() if context_mode == WRITER_CONTEXT_LEGACY_FULL else km.retrieve(section_id=section_id)
    context = ""
    for doc in docs:
        citation_hash = doc.metadata.get("citation_hash", "unknown")
        url = doc.metadata.get("url") or doc.metadata.get("source_url", "")
        context += f"[HASH:{citation_hash} | URL:{url}] {doc.page_content}\n\n"

    must_answer_points = state.get("must_answer_points") or []
    required_modes = state.get("required_analysis_modes") or []
    prompt = f"""
You are writing one section of a deep research report.

Section title: {state['title']}
Section brief: {state['description']}
Direct question: {state.get('direct_question', '')}
Must-answer points for this section:
{json.dumps(must_answer_points, ensure_ascii=False)}
Required analysis modes:
{json.dumps(required_modes, ensure_ascii=False)}

Available evidence:
{context}

Requirements:
- Write Markdown only, without repeating the section title.
- Start with a short direct answer sentence for this section.
- Then provide:
  - Key Evidence
  - Analysis
  - Uncertainty
- In Analysis, include at least two of: comparison, causality, risk/limitations whenever relevant.
- Every important claim must keep the citation hashes or source links from the evidence.
- If the evidence is weak or missing, say that explicitly instead of guessing.
"""
    retry_count = 0
    transient_error_count = 0
    last_exc: Exception | None = None
    for attempt in range(2):
        try:
            resp = await llm_worker.ainvoke([HumanMessage(content=prompt)])
            content = resp.content
            return {
                "sections": {section_id: content},
                "writer_runtime": {
                    section_id: {
                        "retry_count": retry_count,
                        "transient_error_count": transient_error_count,
                        "fallback_used": False,
                    }
                },
            }
        except Exception as exc:
            last_exc = exc
            if _is_transient_writer_error(exc):
                transient_error_count += 1
                if attempt == 0:
                    retry_count += 1
                    await asyncio.sleep(0.75)
                    continue
            break
    content = _fallback_section_from_docs(
        docs=docs,
        must_answer_points=must_answer_points,
        required_modes=required_modes,
    )
    return {
        "sections": {section_id: content},
        "writer_runtime": {
            section_id: {
                "retry_count": retry_count,
                "transient_error_count": transient_error_count,
                "fallback_used": True,
                "last_error": str(last_exc or ""),
            }
        },
    }


async def editor_node(state: WriterState):
    outline = state["outline"]
    sections = state["sections"]
    task_contract = state.get("task_contract") or {}
    evidence_slots = state.get("evidence_slots") or {}

    raw_draft = ""
    for section in outline:
        raw_draft += f"### {section['title']}\n{sections.get(section['id'], '(missing)')}\n\n"

    prompt = f"""
Turn the section drafts below into a final research report in Markdown.

Topic: {state['query']}
Task contract:
{json.dumps(task_contract, ensure_ascii=False)}

Evidence slots summary:
{json.dumps(evidence_slots, ensure_ascii=False)}

Draft material:
{raw_draft}

Output contract:
1. ## Direct Answer / Core Conclusion
2. ## Key Evidence
3. ## Analysis
4. ## Uncertainty / Missing Evidence

Requirements:
- The direct answer must respond to the user query immediately and explicitly.
- Cover each must-answer point from the task contract.
- Analysis must include comparison, causality, and risks or limitations when relevant.
- Every core conclusion must be backed by citations already present in the drafts.
- Do not invent evidence.
- Preserve source links and citation hashes.
"""
    try:
        resp = await llm_chief.ainvoke(
            [
                {"role": "system", "content": "You are the chief editor of a research team."},
                {"role": "user", "content": prompt},
            ]
        )
        final_doc = _ensure_report_contract(resp.content, state["query"], raw_draft)
    except Exception:
        final_doc = _ensure_report_contract("", state["query"], raw_draft)
    return {"final_doc": final_doc}


async def report_verifier_node(state: WriterState):
    audit = state.get("draft_audit") or {}
    prompt = f"""
You are the Report Verifier Agent.

Review the final report and return strict JSON:
{{
  "answer_coverage": "pass|needs_improvement|insufficient_evidence",
  "citation_support": "pass|needs_improvement|insufficient_evidence",
  "constraint_satisfaction": "pass|needs_improvement|insufficient_evidence",
  "analysis_gap": "pass|needs_improvement|insufficient_evidence",
  "should_revise": true,
  "needs_research_backfill": false,
  "should_degrade": false,
  "reason": "short explanation"
}}

Rules:
- Only decide whether one more revise should happen.
- If the report lacks evidence and cannot be fixed by style/editing alone, set needs_research_backfill=true.
- If the report can only safely ship as a limited answer, set should_degrade=true.

Query:
{state["query"]}

Task contract:
{json.dumps(state.get("task_contract") or {}, ensure_ascii=False)}

Code audit:
{json.dumps(audit, ensure_ascii=False)}

Final report:
{state.get("final_doc", "")}
"""
    fallback = {
        "answer_coverage": "needs_improvement" if audit.get("missing_requirements") else "pass",
        "citation_support": "needs_improvement" if not audit.get("direct_answer_citation_backed") else "pass",
        "constraint_satisfaction": "needs_improvement" if audit.get("task_clause_coverage_rate", 0.0) < 1.0 else "pass",
        "analysis_gap": "needs_improvement" if audit.get("analysis_signal_count", 0) < 2 else "pass",
        "should_revise": bool(audit.get("missing_requirements")) and int(state.get("audit_revision_count", 0)) < 1,
        "needs_research_backfill": "evidence_slots_not_grounded" in list(audit.get("missing_requirements") or []),
        "should_degrade": bool(audit.get("missing_requirements")) and "evidence_slots_not_grounded" in list(audit.get("missing_requirements") or []),
        "reason": "Fallback verifier derived decision from deterministic audit.",
    }
    try:
        resp = await llm_smart.ainvoke([HumanMessage(content=prompt)])
        parsed = clean_json_output(resp.content, strict=True)
        if isinstance(parsed, dict):
            fallback.update(
                {
                    "answer_coverage": str(parsed.get("answer_coverage") or fallback["answer_coverage"]),
                    "citation_support": str(parsed.get("citation_support") or fallback["citation_support"]),
                    "constraint_satisfaction": str(parsed.get("constraint_satisfaction") or fallback["constraint_satisfaction"]),
                    "analysis_gap": str(parsed.get("analysis_gap") or fallback["analysis_gap"]),
                    "should_revise": bool(parsed.get("should_revise")) and int(state.get("audit_revision_count", 0)) < 1,
                    "needs_research_backfill": bool(parsed.get("needs_research_backfill")),
                    "should_degrade": bool(parsed.get("should_degrade")),
                    "reason": str(parsed.get("reason") or fallback["reason"]),
                }
            )
    except Exception:
        pass
    return {"report_verifier": fallback}


def draft_audit_node(state: WriterState):
    audit = audit_final_doc(
        state.get("final_doc", ""),
        state["query"],
        state.get("task_contract") or {},
        state.get("evidence_slots") or {},
    )
    writer_runtime = dict(state.get("writer_runtime") or {})
    audit["writer_section_retry_count"] = sum(
        int(section_state.get("retry_count") or 0) for section_state in writer_runtime.values()
    )
    audit["writer_transient_error_count"] = sum(
        int(section_state.get("transient_error_count") or 0) for section_state in writer_runtime.values()
    )
    audit["writer_section_fallback_count"] = sum(
        1 for section_state in writer_runtime.values() if section_state.get("fallback_used")
    )
    audit["writer_section_fallback_used"] = audit["writer_section_fallback_count"] > 0
    return {"draft_audit": audit}


async def revision_node(state: WriterState):
    audit = state.get("draft_audit") or {}
    prompt = f"""
Revise the final report below so it satisfies the failed audit requirements.

Query: {state['query']}
Task contract:
{json.dumps(state.get('task_contract') or {}, ensure_ascii=False)}

Audit result:
{json.dumps(audit, ensure_ascii=False)}

Current final report:
{state.get('final_doc', '')}

Requirements:
- Fix every missing requirement.
- Keep the same citation hashes and links.
- Do not invent evidence that is not already present.
- Preserve the 4 required sections exactly.
"""
    try:
        resp = await llm_chief.ainvoke(
            [
                {"role": "system", "content": "You revise research reports to satisfy explicit audit constraints."},
                {"role": "user", "content": prompt},
            ]
        )
        revised = _ensure_report_contract(resp.content, state["query"], state.get("final_doc", ""))
    except Exception:
        revised = state.get("final_doc", "")
    return {
        "final_doc": revised,
        "audit_revision_count": int(state.get("audit_revision_count", 0)) + 1,
    }


def continue_to_writers(state: WriterState):
    if state.get("iteration", 0) > 0 and not state["outline"]:
        return "skeleton_generator"
    if state.get("user_feedback") and not state["outline"]:
        return "skeleton_generator"
    return map_to_writers(state)


def map_to_writers(state: WriterState):
    task_contract = state.get("task_contract") or {}
    points_by_section: dict[str, list[str]] = {}
    for point in task_contract.get("must_answer_points") or []:
        section_id = str(point.get("section_id") or "global")
        points_by_section.setdefault(section_id, []).append(str(point.get("question") or ""))
    return [
        Send(
                "section_writer",
                {
                    **item,
                    "direct_question": task_contract.get("direct_question", state.get("query", "")),
                    "must_answer_points": points_by_section.get(item["id"], []),
                    "required_analysis_modes": state.get("required_analysis_modes") or [],
                    "writer_context_mode": state.get("writer_context_mode"),
                },
        )
        for item in state["outline"]
    ]


def route_after_audit(state: WriterState):
    return "report_verifier"


def route_after_verifier(state: WriterState):
    verifier = state.get("report_verifier") or {}
    if verifier.get("should_revise") and int(state.get("audit_revision_count", 0)) < 1:
        return "revision"
    return END


writer_workflow = StateGraph(WriterState)
writer_workflow.add_node("skeleton_generator", skeleton_node)
writer_workflow.add_node("human_review", human_review_node)
writer_workflow.add_node("section_writer", section_writer_node)
writer_workflow.add_node("editor", editor_node)
writer_workflow.add_node("draft_audit", draft_audit_node)
writer_workflow.add_node("report_verifier", report_verifier_node)
writer_workflow.add_node("revision", revision_node)

writer_workflow.set_entry_point("skeleton_generator")
writer_workflow.add_edge("skeleton_generator", "human_review")
writer_workflow.add_conditional_edges(
    "human_review",
    continue_to_writers,
    ["section_writer", "skeleton_generator"],
)
writer_workflow.add_edge("section_writer", "editor")
writer_workflow.add_edge("editor", "draft_audit")
writer_workflow.add_conditional_edges("draft_audit", route_after_audit, ["report_verifier", END])
writer_workflow.add_conditional_edges("report_verifier", route_after_verifier, ["revision", END])
writer_workflow.add_edge("revision", "draft_audit")

if get_sqlite_checkpointer and CHECKPOINT_DB_PATH:
    writer_app = writer_workflow.compile(checkpointer=get_sqlite_checkpointer(CHECKPOINT_DB_PATH))
else:  # pragma: no cover - compatibility path for import-only tests
    writer_app = writer_workflow.compile()
