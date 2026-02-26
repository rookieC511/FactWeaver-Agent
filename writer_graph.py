import json
import re
import asyncio
from typing import TypedDict, List, Dict, Any, Annotated
import operator
from langchain_core.messages import HumanMessage
from langgraph.graph import StateGraph, END
from langgraph.types import Send

from models import llm_fast, llm_smart, llm_worker, llm_chief
from memory import km
from tools import clean_json_output
from charts import generate_chart
import time

# ==========================================
# 1. 状态定义 (State Definition)
# ==========================================

class Section(TypedDict):
    id: str           # 章节 ID (e.g., "1.1")
    title: str        # 标题 (e.g., "市场规模")
    description: str  # 写作指导/上下文
    content: str      # 生成的内容

class WriterState(TypedDict):
    query: str                  # 原始用户问题
    outline: List[Section]      # 大纲 (待写作任务列表)
    sections: Annotated[Dict[str, str], operator.ior] # 并行写入的容器 {id: content}
    final_doc: str              # 最终聚合的文档
    iteration: int              # 轮次 (用于重试)
    user_feedback: str          # 用户对大纲的反馈意见

# ==========================================
# 2. 核心节点 (Nodes)
# ==========================================

def skeleton_node(state: WriterState):
    """
    [Planner] DeepSeek-R1 生成层级化大纲
    """
    # 如果上游已经传来了大纲 (Human-Guided Exploration 模式)，则直接复用
    if state.get("outline") and len(state["outline"]) > 0:
        print(f"\n🧠 [Writer-Planner] 检测到已批准的大纲，跳过生成: {len(state['outline'])} 章")
        return {"outline": state["outline"]}

    print(f"\n🧠 [Writer-Planner] R1 正在构建思维骨架 (Skeleton)...")
    
    # 获取全局提纯后的 Fact Blocks 作为上下文
    docs = km.retrieve()
    context_preview = "\n".join([f"- {d.page_content[:100]}..." for d in docs])

    prompt = f"""
    你是专业的技术报告架构师。
    任务: 为 "{state['query']}" 设计一个详细的写作大纲。

    用户反馈 (如有):
    {state.get('user_feedback', '无')}
    
    背景片段:
    {context_preview}

    要求:
    1. 结构清晰，包含 3-5 个主要章节，每个章节包含 1-2 个子章节。
    2. **标题严禁包含数字编号** (例如: 不要写 "1. 引言"，只写 "引言")。章节编号仅在 JSON 的 "id" 字段中体现。
    3. 返回 JSON 列表格式，每个元素包含:
       - "id": 章节编号 (e.g., "1", "2.1")
       - "title": 标题 (简洁、描述性强，无编号)
       - "description": 该章节应包含的核心内容简述 (作为给写手的指令，必须包含具体的分析要求)
    
    示例:
    [
        {{"id": "1", "title": "引言", "description": "介绍背景和核心问题"}},
        {{"id": "2.1", "title": "技术原理", "description": "详细解释架构细节"}}
    ]
    """
    
    try:
        resp = llm_smart.invoke([HumanMessage(content=prompt)])
        outline_data = clean_json_output(resp.content)
        
        # 简单清洗: 确保是列表
        if isinstance(outline_data, dict):
            # 兼容处理: 如果 R1 返回了 {"sections": [...] }
            outline_data = outline_data.get("sections", outline_data.get("outline", []))
            
        if not isinstance(outline_data, list):
            print("  ⚠️ 大纲解析失败，使用默认结构")
            outline_data = [{"id": "1", "title": "综述", "description": f"回答 {state['query']}"}]
            
    except Exception as e:
        print(f"  ❌ 大纲生成错误: {e}")
        outline_data = [{"id": "1", "title": "错误恢复", "description": "自动生成失败，请直接回答。"}]

    print(f"  📋 生成了 {len(outline_data)} 个章节任务")
    for sec in outline_data:
        print(f"    - [{sec.get('id')}] {sec.get('title')}")

    return {"outline": outline_data, "sections": {}}

def human_review_node(state: WriterState):
    """
    [HITL] 人工介入节点
    在控制台打印大纲，并允许用户决定是否继续或重试。
    """
    print("\n" + "="*40)
    print("👀 [人工审查] 请确认生成的大纲:")
    print("="*40)
    for sec in state['outline']:
        print(f"  [{sec.get('id')}] {sec.get('title')}: {sec.get('description')[:50]}...")
    print("="*40)
    
    # [Optimization] 如果上游标记为自动通过 (Human-Guided Mode)，则跳过人工确认
    if state.get("user_feedback") == "SKIP_REVIEW":
        print("🚀 [Writer] 检测到预批准指令，自动跳过二次确认...")
        return {"user_feedback": ""}

    # 在真实 Agent 部署中，这里会是一个 interrupt，等待 API 恢复。
    # 本地脚本演示使用 input()
    print("\n直接回车继续，或者输入您的修改意见 (输入 'q' 退出): ")
    user_input = input("> ").strip()
    
    if user_input.lower() == 'q':
        raise KeyboardInterrupt("用户终止任务")
    
    # 如果用户输入了内容 (不仅仅是回车)，则视为反馈意见，触发重生成
    if user_input:
        print(f"  🔄 收到反馈: '{user_input}'，正在调整大纲...")
        return {
            "user_feedback": user_input, 
            "outline": [], 
            "iteration": state.get("iteration", 0) + 1
        }
        
    print("✅ 大纲已确认，开始并行写作...")
    # 清空反馈，避免影响后续流程 (如果需要)
    return {"user_feedback": ""}

def chart_scout_node(state: WriterState):
    """
    [Scout] 扫描大纲，识别是否有机会插入图表
    """
    print("\n📊 [Chart-Scout] 正在分析可视化机会...")
    outline = state['outline']
    query = state['query']
    
    # 获取提纯后的事实块上下文
    docs = km.retrieve()
    context_str = "\n".join([d.page_content[:200] for d in docs])
    
    prompt = f"""
    你是数据可视化专家。
    任务: 分析以下写作大纲和背景信息，判断是否需要生成图表 (Line/Bar) 来增强报告的说服力。
    
    大纲:
    {json.dumps(outline, ensure_ascii=False)}
    
    背景片段:
    {context_str}
    
    要求:
    1. 最多生成 1-2 个最关键的图表 (如果数据不足或不需要，返回空列表)。
    2. 必须基于背景片段中的真实数据 (数字)。不要编造数据。
    3. 返回 JSON 格式:
       {{
           "charts": [
               {{
                   "target_section_id": "关联的章节ID",
                   "type": "line" 或 "bar",
                   "title": "图表标题",
                   "filename": "chart_name.png",
                   "data": {{
                       "labels": ["2021", "2022"...],
                       "datasets": [{{"label": "Metric", "data": [10, 20...]}}]
                   }}
               }}
           ]
       }}
    """
    
    try:
        resp = llm_smart.invoke([HumanMessage(content=prompt)])
        res = clean_json_output(resp.content)
        charts = res.get("charts", [])
        
        updated_outline = outline.copy()
        
        for chart in charts:
            print(f"  🎨 生成图表: {chart['title']}")
            # Generate the image
            path = generate_chart(
                chart_type=chart['type'],
                data=chart['data'],
                title=chart['title'],
                filename=chart['filename']
            )
            
            if path:
                # Update outline description to force embedding
                # 修复: 使用相对路径，方便 Markdown 预览 (假设报告在根目录, charts 在 public/charts)
                # path 是绝对路径，我们需要 ./public/charts/xxx.png
                relative_path = f"./public/charts/{os.path.basename(path)}"
                
                sec_id = chart['target_section_id']
                for sec in updated_outline:
                    if sec['id'] == sec_id:
                        sec['description'] += f"\n[IMPORTANT] Must embed generated chart: ![{chart['title']}]({relative_path})"
                        print(f"    -> 已注入指令到章节 {sec_id}")
                        
        return {"outline": updated_outline}
        
    except Exception as e:
        print(f"  ⚠️ 图表生成跳过: {e}")
        return {}

async def section_writer_node(state: Section):
    """
    [Worker] DeepSeek-V3.2 并行撰写单章节
    注意: 入参是 Section (通过 Send 传递)，而不是整个 State
    """
    sec_id = state['id']
    title = state['title']
    desc = state['description']
    
    # 1. 提取提纯后的全局 Fact Blocks (长上下文模式下直接全量塞给大模型)
    docs = km.retrieve()
    
    # 构造上下文，带 Citation URL
    context_str = ""
    for d in docs:
        ref_id = d.metadata.get('citation_hash', 'unknown')
        url = d.metadata.get('url', d.metadata.get('source', ''))
        # 提供 hash 和 url 供 writer 使用
        context_str += f"[HASH:{ref_id} | URL:{url}] {d.page_content}\n\n"
        
    print(f"  ✍️ [Writer-{sec_id}] 正在撰写: {title} (Ref: {len(docs)})")

    prompt = f"""
    # [C] Context
    We have gathered raw data about "{title}".
    Raw Data:
    {context_str}

    # [O] Objective
    Extract and organize the factual findings for "{title}".
    Description: {desc}

    # [R] Rules
    1. **Format**: Do not give it a title, do not break it down into sections, and do not provide any of your own conclusions/analysis.
    2. **Content**: Keep it free of formatting and focus on the facts only.
    3. **Completeness**: Be extremely thorough and comprehensive. Ensure you do not lose any details from the raw data.
    4. **Citation**: Preserve all URLs and facts. End every factual sentence with a Markdown link: `[Source Name](URL)`. Do NOT use `[HASH]`. Use the actual URL provided in the context.
    5. **Missing Data**: If data is missing, state it clearly.
    6. **Images**: If raw data contains `[SNAPSHOT_PATH: ...]`, preserve it exactly as is so the editor can use it.
    """
    
    try:
        resp = await llm_worker.ainvoke([HumanMessage(content=prompt)])
        content = resp.content
        print(f"\n✅ [Writer-{sec_id} 提纯结果] 完成撰写 ({len(content)} 字符) -> 传递至状态机")
    except Exception as e:
        content = f"写作失败: {e}"
        print(f"\n❌ [Writer-{sec_id} 失败] {e}")
        
    return {"sections": {sec_id: content}}

async def editor_node(state: WriterState):
    """
    [Editor] 聚合所有章节，通过大模型重写生成连贯的、结构化的最终报告
    """
    print(f"\n📄 [Editor] 正在由总编 (Chief Editor) 重新组织并汇编全文...")
    
    outline = state['outline']
    sections = state['sections']
    query = state['query']
    
    outline_str = "\n".join([f"- {s['title']}: {s['description']}" for s in outline])
    
    # 1. 组装碎片的初稿 (Raw Draft) -> 现在是无格式事实清单 (Fact Sheets)
    raw_draft = ""
    for sec in outline:
        sec_id = sec['id']
        title = sec['title']
        content = sections.get(sec_id, "(该章节生成失败)")
        raw_draft += f"### Section: {title}\n{content}\n\n"
        
    print("  🔗 [Editor] 正在收集原始报告中的引用信息...")
    # 收集所有的 Markdown 链接 [text](url) 确保不会丢失
    used_links = list(set(re.findall(r'\[.*?\]\(https?://[^\s\)]+\)', raw_draft)))
    
    # 2. 调用大模型进行重写与融合 (Rewrite and Synthesize)
    system_prompt = f"""
    You are the Chief Academic Editor for a research agency. You have received an approved Outline and a set of raw, unformatted fact sheets from your researchers regarding the topic: "{query}".
    Your task is to synthesize these fact sheets into a single, cohesive, and professional academic report that follows the Outline and flows naturally.

    CRITICAL RULES:
    1. DO NOT just concatenate the fact sheets. You must weave all the facts into a unified narrative with smooth transitions.
    2. FOLLOW THE OUTLINE: You MUST structure the "Main Analysis" part of your report based on this approved outline:
       {outline_str}
       
    3. MANDATORY OVERALL STRUCTURE: Your final report MUST strictly follow this exact markdown structure:
       # {query}
       
       ## Abstract
       (A 200-word summary of the entire findings)
       
       ## 1. Introduction
       (Introduce the topic smoothly)
       
       ## 2. Main Analysis 
       (Use the provided Outline's sub-headings to intelligently group and present the facts)
       
       ## 3. Conclusion
       (Conclude the report with sophisticated analysis)
       
       ## 4. References
       (Consolidate all citations)
       
    4. PRESERVE ALL URLs AND CITATIONS: This is the most critical rule. The raw fact sheets contain markdown links like `[source](https://...)`. You MUST NOT delete any URLs or links. Every factual claim in your synthesized report must end with its original markdown link to maintain traceability.
    """
    
    user_prompt = f"Here are the raw fact sheets from the researchers. Please synthesize them according to the Outline into the final coherent report:\n\n{raw_draft}"
    
    try:
        resp = await llm_chief.ainvoke([
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ])
        final_text = resp.content
        print(f"\n🌟 [Chief Editor 最终合成] 获得成品报告 ({len(final_text)} 字符) -> 交付使用")
    except Exception as e:
        print(f"  ❌ 汇总重写失败: {e}")
        # 降级方案：回退到简单的拼接
        final_text = f"# {query}\n\n## Abstract\n(Synthesis failed, displaying raw sections)\n\n" + raw_draft
        print("\n⚠️ [Chief Editor 降级] 合成失败，回退至碎片拼接输出。")
        
    # 3. 补充参考链接列表 (作为后备，防 LLM 丢失链接)
    if used_links and "## 4. References" not in final_text and "## References" not in final_text and "## 参考文献" not in final_text:
        final_text += "\n\n## 4. References\n"
        for link in used_links:
            final_text += f"- {link}\n"
            
    return {"final_doc": final_text}

# ==========================================
# 3. 路由逻辑 (Map-Reduce)
# ==========================================

def continue_to_writers(state: WriterState):
    """
    将大纲拆解为并行任务
    """
    if state.get("iteration", 0) > 0 and not state['outline']:
        return "skeleton_generator"
    
    # 如果有用户反馈导致大纲被清空 (human_review 返回了 outline=[])，也需要重新生成
    if state.get("user_feedback") and not state['outline']:
        return "skeleton_generator"
        
    return "chart_scout"

def map_to_writers(state: WriterState):
    """
    Map 步骤
    """
    return [
        Send("section_writer", item) for item in state['outline']
    ]

# ==========================================
# 4. 图构建
# ==========================================

writer_workflow = StateGraph(WriterState)

writer_workflow.add_node("skeleton_generator", skeleton_node)
writer_workflow.add_node("human_review", human_review_node)
writer_workflow.add_node("section_writer", section_writer_node)
writer_workflow.add_node("editor", editor_node)

writer_workflow.add_node("chart_scout", chart_scout_node)

writer_workflow.set_entry_point("skeleton_generator")

writer_workflow.add_edge("skeleton_generator", "human_review")

# Flow: Review -> (Retry? -> Skeleton) OR (OK? -> ChartScout -> Writers)
writer_workflow.add_conditional_edges(
    "human_review",
    continue_to_writers, # determines next step name
    ["chart_scout", "skeleton_generator"]
)

writer_workflow.add_conditional_edges(
    "chart_scout",
    map_to_writers,
    ["section_writer"]
)

writer_workflow.add_edge("section_writer", "editor")
writer_workflow.add_edge("editor", END)



writer_app = writer_workflow.compile()

