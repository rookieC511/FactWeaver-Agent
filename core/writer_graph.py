import json
import operator
import os
from typing import Annotated, Dict, List, TypedDict

from langchain_core.messages import HumanMessage
from langgraph.graph import END, StateGraph
try:
    from langgraph.types import Send
except Exception:  # pragma: no cover - compatibility for lightweight test stubs
    class Send(dict):
        def __init__(self, node: str, arg):
            super().__init__(node=node, arg=arg)

from core.charts import generate_chart
from core.memory import get_current_km
from core.models import llm_chief, llm_smart, llm_worker
from core.tools import LLMFormatError, clean_json_output

try:
    from core.checkpoint import get_sqlite_checkpointer
    from core.config import CHECKPOINT_DB_PATH
except Exception:  # pragma: no cover - lightweight test imports
    get_sqlite_checkpointer = None  # type: ignore[assignment]
    CHECKPOINT_DB_PATH = ""


class Section(TypedDict):
    id: str
    title: str
    description: str
    content: str


class WriterState(TypedDict):
    query: str
    outline: List[Section]
    sections: Annotated[Dict[str, str], operator.ior]
    final_doc: str
    iteration: int
    user_feedback: str
    task_id: str
    writer_context_mode: str


WRITER_CONTEXT_SECTION_SCOPED = "section_scoped"
WRITER_CONTEXT_LEGACY_FULL = "legacy_full_context"


def resolve_writer_context_mode(explicit_mode: str | None = None) -> str:
    mode = (explicit_mode or os.getenv("FACTWEAVER_WRITER_CONTEXT_MODE", WRITER_CONTEXT_SECTION_SCOPED)).strip().lower()
    if mode not in {WRITER_CONTEXT_SECTION_SCOPED, WRITER_CONTEXT_LEGACY_FULL}:
        return WRITER_CONTEXT_SECTION_SCOPED
    return mode


def get_writer_thread_id(task_id: str) -> str:
    return f"{task_id}:writer"


def _default_outline(query: str) -> list[dict]:
    return [
        {"id": "1", "title": "背景", "description": f"介绍 {query} 的背景与问题定义"},
        {"id": "2", "title": "关键发现", "description": f"总结 {query} 的主要事实与证据"},
        {"id": "3", "title": "结论", "description": f"归纳 {query} 的最终判断与剩余问题"},
    ]


async def skeleton_node(state: WriterState):
    if state.get("outline"):
        return {"outline": state["outline"], "sections": {}}

    km = get_current_km()
    docs = km.retrieve(k=8)
    context_preview = "\n".join(f"- {doc.page_content[:120]}..." for doc in docs)
    prompt = f"""
为下面的研究主题生成写作大纲，输出 JSON 数组，每项包含 id/title/description。

主题: {state['query']}
用户反馈: {state.get('user_feedback', '')}
资料摘要:
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
    import os

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


async def chart_scout_node(state: WriterState):
    km = get_current_km()
    docs = km.retrieve(k=8)
    context_str = "\n".join(doc.page_content[:200] for doc in docs)
    prompt = f"""
根据以下大纲和上下文，判断是否需要生成 0-2 个图表。
只返回 JSON:
{{
  "charts": [
    {{
      "target_section_id": "1",
      "type": "line",
      "title": "标题",
      "filename": "chart.png",
      "data": {{"labels": ["A"], "datasets": [{{"label": "Metric", "data": [1]}}]}}
    }}
  ]
}}

大纲:
{json.dumps(state['outline'], ensure_ascii=False)}

上下文:
{context_str}
"""
    charts = []
    try:
        resp = await llm_smart.ainvoke([HumanMessage(content=prompt)])
        parsed = clean_json_output(resp.content, strict=True)
        if isinstance(parsed, dict):
            charts = parsed.get("charts", [])
    except Exception:
        charts = []

    updated_outline = [dict(section) for section in state["outline"]]
    for chart in charts:
        try:
            path = generate_chart(
                chart_type=chart["type"],
                data=chart["data"],
                title=chart["title"],
                filename=chart["filename"],
            )
        except Exception:
            path = ""
        if not path:
            continue
        relative_path = f"./public/charts/{__import__('os').path.basename(path)}"
        for section in updated_outline:
            if section["id"] == chart.get("target_section_id"):
                section["description"] += (
                    f"\n[IMPORTANT] Must embed generated chart: ![{chart['title']}]({relative_path})"
                )
    return {"outline": updated_outline}


async def section_writer_node(state: Section):
    km = get_current_km()
    section_id = state["id"]
    context_mode = resolve_writer_context_mode(state.get("writer_context_mode"))
    if context_mode == WRITER_CONTEXT_LEGACY_FULL:
        docs = km.retrieve()
    else:
        docs = km.retrieve(section_id=section_id)
    context = ""
    for doc in docs:
        citation_hash = doc.metadata.get("citation_hash", "unknown")
        url = doc.metadata.get("url") or doc.metadata.get("source_url", "")
        context += f"[HASH:{citation_hash} | URL:{url}] {doc.page_content}\n\n"

    prompt = f"""
你正在并行撰写一个研究报告章节。

章节标题: {state['title']}
章节说明: {state['description']}

可用事实:
{context}

要求:
- 只输出该章节正文，不要标题
- 每个事实句尽量保留链接
- 如果缺少数据，明确说明
- 在最后追加 `### Dependency Declaration`
"""
    try:
        resp = await llm_worker.ainvoke([HumanMessage(content=prompt)])
        content = resp.content
    except Exception as exc:
        content = f"写作失败: {exc}"
    return {"sections": {section_id: content}}


async def editor_node(state: WriterState):
    outline = state["outline"]
    sections = state["sections"]

    raw_draft = ""
    for section in outline:
        raw_draft += f"### {section['title']}\n{sections.get(section['id'], '(missing)')}\n\n"

    prompt = f"""
将下面的分章节材料整理成一篇完整研究报告。

主题: {state['query']}
大纲:
{json.dumps(outline, ensure_ascii=False)}

材料:
{raw_draft}

要求:
- 生成一篇结构完整的 Markdown 报告
- 保留引用链接
- 不要重复相同事实
"""
    try:
        resp = await llm_chief.ainvoke(
            [
                {"role": "system", "content": "You are the chief editor of a research team."},
                {"role": "user", "content": prompt},
            ]
        )
        final_doc = resp.content
    except Exception:
        final_doc = f"# {state['query']}\n\n{raw_draft}"
    return {"final_doc": final_doc}


def continue_to_writers(state: WriterState):
    if state.get("iteration", 0) > 0 and not state["outline"]:
        return "skeleton_generator"
    if state.get("user_feedback") and not state["outline"]:
        return "skeleton_generator"
    return "chart_scout"


def map_to_writers(state: WriterState):
    return [Send("section_writer", item) for item in state["outline"]]


writer_workflow = StateGraph(WriterState)
writer_workflow.add_node("skeleton_generator", skeleton_node)
writer_workflow.add_node("human_review", human_review_node)
writer_workflow.add_node("chart_scout", chart_scout_node)
writer_workflow.add_node("section_writer", section_writer_node)
writer_workflow.add_node("editor", editor_node)

writer_workflow.set_entry_point("skeleton_generator")
writer_workflow.add_edge("skeleton_generator", "human_review")
writer_workflow.add_conditional_edges(
    "human_review",
    continue_to_writers,
    ["chart_scout", "skeleton_generator"],
)
writer_workflow.add_conditional_edges("chart_scout", map_to_writers, ["section_writer"])
writer_workflow.add_edge("section_writer", "editor")
writer_workflow.add_edge("editor", END)

if get_sqlite_checkpointer and CHECKPOINT_DB_PATH:
    writer_app = writer_workflow.compile(checkpointer=get_sqlite_checkpointer(CHECKPOINT_DB_PATH))
else:  # pragma: no cover - compatibility path for import-only tests
    writer_app = writer_workflow.compile()
