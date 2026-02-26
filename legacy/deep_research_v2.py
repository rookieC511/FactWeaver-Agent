import os
import asyncio
import json
from typing import TypedDict, List
from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage
from tavily import TavilyClient
from langgraph.graph import StateGraph, END

load_dotenv()
tavily = TavilyClient(api_key=os.environ["TAVILY_API_KEY"])
llm = ChatOpenAI(
    model="deepseek-ai/DeepSeek-V3", 
    base_url="https://api.siliconflow.cn/v1",
    api_key=os.environ["OPENAI_API_KEY"]
)

class AgentState(TypedDict):
    query: str
    plan: List[str]
    gathered_info: List[str]
    iteration: int
    needs_more: bool
    critique: str

# [Planner]: 支持根据反馈修正计划
def plan_node(state: AgentState):
    iter_num = state.get("iteration", 0)
    print(f"\n🧠 [Planner] 第 {iter_num + 1} 轮规划...")
    
    context = f"前一轮反馈: {state['critique']}" if state.get('critique') else ""
    prompt = f"""
    目标: {state['query']}
    {context}
    生成 1-2 个新的搜索词。只返回 JSON 数组。
    """
    res = llm.invoke([HumanMessage(content=prompt)])
    try:
        plan = json.loads(res.content.replace("```json", "").replace("```", "").strip())
    except:
        plan = [state['query']]
    return {"plan": plan, "iteration": iter_num + 1}

# [Executor]
async def execute_node(state: AgentState):
    print(f"🕵️ [Executor] 执行搜索: {state['plan']}")
    new_info = []
    for q in state['plan']:
        try:
            res = tavily.search(query=q, max_results=2)
            content = "\n".join([r['content'] for r in res['results']])
            new_info.append(f"Query: {q}\nContent: {content}")
        except: pass
    
    current = state.get('gathered_info', []) + new_info
    return {"gathered_info": current}

# [Reviewer]: 核心反思节点
def review_node(state: AgentState):
    print("⚖️ [Reviewer] 正在审查...")
    info = "\n".join(state['gathered_info'])
    prompt = f"""
    问题: {state['query']}
    已知信息: {info}
    信息是否足够？足够返回 "YES"，不足返回 "NO" 并说明缺什么。
    格式 JSON: {{"status": "YES", "critique": ""}}
    """
    res = llm.invoke([HumanMessage(content=prompt)])
    try:
        review = json.loads(res.content.replace("```json", "").replace("```", "").strip())
    except:
        review = {"status": "YES"}
        
    return {"needs_more": review['status'] == "NO", "critique": review.get('critique', '')}

# [Writer]
def write_node(state: AgentState):
    print("✍️ [Writer] 生成最终报告")
    info = "\n".join(state['gathered_info'])
    res = llm.invoke([HumanMessage(content=f"基于以下信息回答 {state['query']}:\n{info}")])
    return {"report": res.content}

# [Router]: 路由逻辑
def should_continue(state: AgentState):
    if state['iteration'] > 2: return "writer" # 强制止损
    if state['needs_more']: return "planner"
    return "writer"

workflow = StateGraph(AgentState)
workflow.add_node("planner", plan_node)
workflow.add_node("executor", execute_node)
workflow.add_node("reviewer", review_node)
workflow.add_node("writer", write_node)

workflow.set_entry_point("planner")
workflow.add_edge("planner", "executor")
workflow.add_edge("executor", "reviewer")
workflow.add_conditional_edges("reviewer", should_continue, {"planner": "planner", "writer": "writer"})
workflow.add_edge("writer", END)

app = workflow.compile()

async def main():
    # 问一个复杂点的问题，触发循环
    inputs = {"query": "DeepSeek R1 的训练使用了什么强化学习算法？与 PPO 有何不同？", "iteration": 0, "gathered_info": []}
    result = await app.ainvoke(inputs)
    print("\nFINAL:\n", result.get('report', 'Error'))

if __name__ == "__main__":
    asyncio.run(main())