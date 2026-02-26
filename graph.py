
import asyncio
import json
import re
import datetime
import os
from typing import TypedDict, List
from langchain_core.messages import HumanMessage
from langgraph.graph import StateGraph, END

from models import llm_fast, llm_smart
from memory import km
from tools import tavily_client, scrape_jina_ai, visual_browse, clean_json_output
from writer_graph import writer_app, WriterState

# --- 状态定义 ---
class ResearchState(TypedDict):
    query: str
    plan: List[dict]    # "search_tasks": [{"task": "...", "reason": "..."}]
    outline: List[dict] # "report_structure": [{"id": "...", "title": "...", "desc": "..."}]
    user_feedback: str
    iteration: int
    final_report: str
    metrics: dict  # {"tool_calls": int, "backtracking": int}
    task_id: str   # For trajectory logging
    history: List[dict] # For SFT Trajectory Recording


# --- 辅助函数 ---
def safe_invoke(llm, prompt):
    try:
        return llm.invoke([HumanMessage(content=prompt)])
    except Exception as e:
        print(f"  ❌ LLM Invoke Failed: {e}")
        return None

# --- Logging Helper ---
TRAJECTORY_FILE = "trajectory_log.jsonl"

def log_trajectory(task_id: str, event_type: str, data: dict):
    if not task_id: return
    
    record = {
        "timestamp": datetime.datetime.now().isoformat(),
        "task_id": str(task_id),
        "event": event_type,
        "data": data
    }
    try:
        with open(TRAJECTORY_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"⚠️ Logging failed: {e}")



# --- Nodes ---

def node_init_search(state: ResearchState):
    """
    [Phase 1] 快速调研 (Pre-Search) & 生成大纲
    """
    query = state['query']
    print(f"\n🚀 [Planner] 启动预调研 (Scanning): {query}...")
    
    # 清理遗留事实快区，为本题隔离状态
    km.clear()
    
    # 1. 快速搜索 Top-3 获取语境
    context = ""
    try:
        res = tavily_client.search(query=query, max_results=3, search_depth="basic")
        results = res.get('results', [])
        context = "\n".join([f"- {r['title']}: {r['content']}" for r in results])
        # 顺便存入记忆，反正不亏 (作为简短 Snippet)
        for r in results:
            km.add_raw_document(r['content'], r['url'], r['title'])
    except Exception as e:
        print(f"  ⚠️ Pre-search failed: {e}")

    
    # 3. 生成原子搜索计划 (CO-STAR Layout)
    print(f"🧠 [Planner] 正在拆解搜索任务 (Search Tasks)...")
    
    feedback_context = f"User Feedback: {state.get('user_feedback')}" if state.get('user_feedback') else ""
    
    # Using the new CO-STAR prompt from PROMPTS.md (Augmented for Dual Output)
    prompt = f"""
    # [C] Context
    User wants to research a complex topic: "{query}".
    Current Env: {context[:2000]}
    User Feedback: {feedback_context}

    # [O] Objective
    Design a comprehensive TWO-PART plan:
    1. **Report Outline**: A logical structure for the final report (Sections).
    2. **Search Tasks**: A set of atomic search actions to gather necessary data.

    # [S] Style
    Logical, MECE, Insightful.

    # [R] Response Format (CRITICAL)
    Strict JSON format only.
    Target Structure: 
    {{
        "outline": [
            {{"id": "1", "title": "Section Title", "description": "What to write about"}}
        ],
        "search_tasks": [
            {{"task": "Specific search query or action", "reason": "Why we need this"}}
        ]
    }}

    # [E] Examples (Few-Shot)
    Input: "DeepSeek vs OpenAI revenue"
    Output: 
    {{
        "outline": [
            {{"id": "1", "title": "Revenue Comparison", "description": "Compare 2024 fiscal data"}},
            {{"id": "2", "title": "Market Share", "description": "Analyze user growth"}}
        ],
        "search_tasks": [
            {{"task": "Search DeepSeek 2024 revenue report", "reason": "Official data"}}, 
            {{"task": "Search OpenAI 2024 revenue breakdown", "reason": "Comparison logic"}}
        ]
    }}
    """
    
    resp = safe_invoke(llm_fast, prompt)
    if not resp:
        print("  ❌ Planner LLM returned None. Using fallback.")
        plan_data = {}
    else:
        plan_data = clean_json_output(resp.content)
    
    # [LOGGING] Planner CoT
    log_trajectory(
        state.get("task_id"), 
        "planner_cot", 
        {
            "input_context": context[:500],
            "user_feedback": state.get("user_feedback"),
            "llm_output": plan_data
        }
    )
    
    # [SFT] Append to History
    history = state.get("history", [])
    history.append({
        "role": "planner_cot",
        "content": {
            "input_context": context[:500],
            "llm_output": plan_data
        }
    })
    
    search_tasks = []
    outline = []

    # Normalize output
    if isinstance(plan_data, dict):
        search_tasks = plan_data.get("search_tasks", [])
        outline = plan_data.get("outline", [])
    
    # Validation
    if not search_tasks:
        print("  ⚠️ Search tasks missing, adding fallback.")
        search_tasks = [{"task": f"Search comprehensive info about {query}", "reason": "Fallback"}]
    
    if not outline:
        print("  ⚠️ Outline missing, adding fallback.")
        outline = [{"id": "1", "title": "Overview", "description": f"Analysis of {query}"}]
    
    # --- Metrics Init ---
    metrics = state.get("metrics", {"tool_calls": 0, "backtracking": 0})
    if state.get("user_feedback"):
        metrics["backtracking"] += 1
        print(f"  🔄 Backtracking detected. Current Count: {metrics['backtracking']}")

    return {
        "plan": search_tasks, 
        "outline": outline,
        "iteration": state.get("iteration", 0) + 1,
        "iteration": state.get("iteration", 0) + 1,
        "metrics": metrics,
        "history": history
    }

def node_human_feedback(state: ResearchState):
    """
    [Phase 2] 人工介入 (Human-in-the-Loop)
    """
    outline = state['plan']
    print("\n" + "="*50)
    print("🚦 [Human Review] 请确认调研大纲与搜索方向:")
    print("="*50)
    
    outline = state['outline']
    plan = state['plan']
    
    print("\n" + "="*50)
    print("🚦 [Human Review] 请审查调研大纲 (Structure) 与 搜索计划 (Execution):")
    print("="*50)
    
    print(f"\n📑 [Proposed Outline] ({len(outline)} Sections)")
    for sec in outline:
        print(f"  - [{sec.get('id')}] {sec.get('title')}")
        print(f"    Desc: {sec.get('description')[:60]}...")

    print(f"\n🕵️ [Search Tasks] ({len(plan)} Steps)")
    for i, item in enumerate(plan):
        print(f"  - [Task {i+1}] {item.get('task')}")


        print("-" * 30)
        
    print("\n(按回车确认继续，或输入修改意见，输入 'q' 退出)")
    user_input = input("> ").strip()
    
    if user_input.lower() == 'q':
        print("用户终止任务。")
        return {"final_report": "User Terminated"}
        
    if user_input:
        print(f"  🔄 收到反馈，正在重构计划...")
        return {"user_feedback": user_input}
        
    print("✅ 计划已确认，启动深度搜索 (Deep Research)...")
    return {"user_feedback": ""}

async def node_deep_research(state: ResearchState):
    """
    [Phase 3] 深度执行 (Executor)
    基于确认的大纲，对每个章节进行针对性搜索
    """
    """
    [Phase 3] 深度执行 (Executor)
    遍历搜索计划，对每个 Task 动态生成 Query 并执行
    """
    plan = state['plan']
    # [SFT] Init local history accumulator if we want to batch update, 
    # but since it's async gather, we might need a thread-safe way or just return a new history list.
    # Simpler: Each process_task returns its logs, then we aggregate.
    
    print(f"\n🕵️ [Deep Executor] executing {len(plan)} search tasks...")
    
    # BROKE Prompt template for Query Generation
    query_gen_template = """
    # [C] Context
    We are executing a research plan. Current Task: "{task_description}"

    # [R] Role
    You are a Search Engine Optimization (SEO) expert and Google Search Power User.

    # [O] Objective
    Generate 3-5 precise search queries to gather necessary information.

    # [K] Key Constraints
    1. **Keywords only**. Do not use full sentences. (Bad: "How do I find..." | Good: "DeepSeek vs OpenAI revenue 2024")
    2. Use operators if helpful (site: , filetype:pdf).
    3. Return JSON: {{"queries": ["query1", "query2", ...]}}
    """

    async def process_task(task_item):
        task_desc = task_item.get("task")
        print(f"  🤖 Generating queries for: {task_desc}...")
        logs = [] # Local logs for this task
        
        # 1. Generate Queries
        try:
            prompt = query_gen_template.replace("{task_description}", task_desc)
            # Use fast model for query gen
            resp = await llm_fast.ainvoke([HumanMessage(content=prompt)])
            data = clean_json_output(resp.content)
            queries = data.get("queries", [])
            
            # [LOGGING] Query Generation
            log_trajectory(
                state.get("task_id"),
                "query_gen",
                {
                    "task_desc": task_desc,
                    "generated_queries": queries
                }
            )
        except Exception as e:
            print(f"    ⚠️ Query Gen failed: {e}")
            queries = [task_desc] # Fallback
            
        # [SFT] Log Query Gen
        logs.append({
            "role": "query_gen",
            "content": {
                "task_desc": task_desc,
                "generated_queries": queries
            }
        })
            
        # 2. Execute Search for each query
        for q in queries[:3]: # Limit to top 3 queries per task
            print(f"    🔍 Executing: {q}...")
            try:
                res = tavily_client.search(query=q, max_results=2, search_depth="advanced")
                results = res.get('results', [])
                
                # Simple scrape & store
                for item in results:
                    if km.is_duplicate(item['url']): continue
                    
                    content = scrape_jina_ai(item['url'])
                    if len(content) > 300 and "Access Denied" not in content:
                        await km.aadd_document(content, item['url'], item['title'], task_desc)
                        # [SFT] Log Search Result (Observation)
                        logs.append({
                            "role": "search_tool",
                            "content": f"Query: {q}\nTitle: {item['title']}\nContent: {content[:1000]}"
                        })
            except Exception as e:
                print(f"       ❌ Search error: {e}")
                # [LOGGING] Search Failure (Reflection)
                log_trajectory(
                    state.get("task_id"),
                    "search_error",
                    {
                        "query": q,
                        "error": str(e)
                    }
                )
                # [SFT] Log Error (Reflection)
                logs.append({
                    "role": "search_error",
                    "content": {"query": q, "error": str(e)}
                })

        return {"logs": logs} 

    # Concurrently execute tasks
    # Doing 3 at a time
    chunk_size = 3
    new_history_logs = []
    
    for i in range(0, len(plan), chunk_size):
        chunk = plan[i:i+chunk_size]
        results = await asyncio.gather(*(process_task(t) for t in chunk))
        for r in results:
            if "logs" in r:
                new_history_logs.extend(r["logs"])
        
    # [SFT] Update State History
    # We append the new logs from this execution phase
    current_history = state.get("history", [])
    updated_history = current_history + new_history_logs
    
    # Update Metrics
    current_metrics = state.get("metrics", {"tool_calls": 0, "backtracking": 0})
    current_metrics["tool_calls"] += len(plan) * 3
        
    return {
        "metrics": current_metrics,
        "history": updated_history
    }

async def node_writer(state: ResearchState):
    """
    [Phase 4] 写作 (Writer)
    直接调用 writer_graph, 传入我们已经确认好的 outline
    """
    print(f"\n✍️ [Writer] 资料收集完毕，开始写作...")
    
    # Old mapping logic removed because state['plan'] is now Tasks, not Sections.
    # We pass empty outline to force Writer to generate its own structure.
    writer_outline = [] 
    # for sec in state['plan']: ... (removed)
    
    writer_inputs = {
        "query": state['query'],
        "outline": state.get('outline', []),  # Pass the approved outline
        "sections": {},
        "iteration": 0,
        "user_feedback": "SKIP_REVIEW" 
    }
    
    try:
        res = await writer_app.ainvoke(writer_inputs)
        return {"final_report": res.get("final_doc", "Writing Failed")}
    except Exception as e:
        return {"final_report": f"Writer Subgraph Error: {e}"}

# --- 路由逻辑 ---

def router_feedback(state: ResearchState):
    if state.get("final_report") == "User Terminated":
        return END
    
    if state.get("user_feedback"):
        return "planner" # 重做计划
        
    return "executor"

# --- Graph 构建 ---

workflow = StateGraph(ResearchState)
workflow.add_node("planner", node_init_search)
workflow.add_node("human_review", node_human_feedback)
workflow.add_node("executor", node_deep_research)
workflow.add_node("writer", node_writer)

workflow.set_entry_point("planner")

workflow.add_edge("planner", "human_review")
workflow.add_conditional_edges("human_review", router_feedback, ["planner", "executor", END])
workflow.add_edge("executor", "writer")
workflow.add_edge("writer", END)

app = workflow.compile()