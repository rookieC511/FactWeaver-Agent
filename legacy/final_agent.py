import os
import json
import asyncio
import re
import sys
import requests
from typing import TypedDict, List
from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, SystemMessage
from tavily import TavilyClient
from langgraph.graph import StateGraph, END

# 强制将标准输出设置为 UTF-8，解决 Windows 下打印 Emoji 和中文报错的问题
sys.stdout.reconfigure(encoding='utf-8')

# ==========================================
# 1. 工程配置与初始化
# ==========================================
load_dotenv()

# 检查环境变量
t_key = os.environ.get("TAVILY_API_KEY", "")
print(f"🔑 Tavily Key 状态: {'✅ 已加载' if len(t_key) > 10 else '❌ 未找到或无效'}")

if len(t_key) < 10:
    print("⚠️  请检查 .env 文件！Tavily Key 似乎没填对。")

# --- 初始化双模型架构 ---

# 1. "快脑" (The Workforce) - DeepSeek-V3.2 (Updated!)
# 用于：Executor (海量阅读), Writer (长文生成)
llm_fast = ChatOpenAI(
    model="deepseek-ai/DeepSeek-V3.2", # ✅ 已修正为 V3.2
    base_url="https://api.siliconflow.cn/v1",
    api_key=os.environ["OPENAI_API_KEY"],
    temperature=0.3
)

# 2. "深脑" (The Executive) - DeepSeek-R1
# 用于：Planner (复杂规划), Reviewer (深度反思)
llm_smart = ChatOpenAI(
    model="deepseek-ai/DeepSeek-R1", 
    base_url="https://api.siliconflow.cn/v1",
    api_key=os.environ["OPENAI_API_KEY"],
    temperature=0.6 
)

tavily = TavilyClient(api_key=os.environ["TAVILY_API_KEY"])

# ==========================================
# 2. 状态定义 (State Schema)
# ==========================================
class ResearchState(TypedDict):
    query: str                # 用户原始查询
    plan: List[str]           # 搜索计划
    gathered_info: List[str]  # 笔记仓库
    iteration: int            # 迭代计数器
    critique: str             # 审查意见
    needs_more: bool          # 循环控制标志
    final_report: str         # 最终产出

# ==========================================
# 3. 工具函数
# ==========================================
def extract_json_content(text: str):
    """
    [手术刀] 强力提取字符串中的 JSON 部分
    解决 R1 在 JSON 前后输出废话导致解析失败的问题
    """
    # 1. 先去思考标签
    text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL)
    # 2. 尝试提取 Markdown 代码块中的 JSON
    code_block = re.search(r'```json\s*(.*?)\s*```', text, re.DOTALL)
    if code_block:
        text = code_block.group(1)
    
    # 3. [核心] 无论有没有代码块，利用正则寻找最外层的 [] 或 {}
    # 寻找列表 [...]
    list_match = re.search(r'\[.*\]', text, re.DOTALL)
    if list_match:
        try:
            return json.loads(list_match.group())
        except:
            pass
            
    # 寻找对象 {...}
    obj_match = re.search(r'\{.*\}', text, re.DOTALL)
    if obj_match:
        try:
            return json.loads(obj_match.group())
        except:
            pass
            
    # 如果都失败了，抛出异常让外层处理
    raise ValueError(f"无法提取 JSON, 原始内容: {text[:100]}...")

def scrape_jina_ai(url: str) -> str:
    """
    利用 Jina Reader 将网页 URL 转换为干净的 Markdown 全文
    """
    print(f"    📖 [Deep Read] 正在深度阅读: {url} ...")
    jina_url = f"https://r.jina.ai/{url}"
    try:
        response = requests.get(jina_url, timeout=15)
        if response.status_code == 200:
            return response.text
        else:
            return f"Error reading {url}: {response.status_code}"
    except Exception as e:
        return f"Failed to scrape {url}: {str(e)}"

# ==========================================
# 4. 核心节点 (Agent Nodes)
# ==========================================

# --- Node: Planner (Deep Think using R1) ---
def plan_node(state: ResearchState):
    print(f"\n🧠 [Planner] R1 深度思考中... (迭代: {state.get('iteration', 1)})")
    
    context = ""
    if state.get('critique'):
        context = f"⚠️ 上一轮反馈: {state['critique']}。请针对性补充缺失信息。"
        print(f"  -> 收到反馈: {state['critique']}")

    prompt = f"""
    你是 Deep Research 的首席架构师。
    任务: 为 "{state['query']}" 制定搜索计划。
    {context}
    
    要求:
    1. 生成 2-3 个具体的搜索引擎查询词。
    2. 确保查询词多样化，覆盖技术原理、数据表现等不同维度。
    3. 严格输出 JSON 数组格式，例如: ["query1", "query2"]。
    """
    
    try:
        response = llm_smart.invoke([HumanMessage(content=prompt)])
        
        # ⚠️ 修改这里：使用 extract_json_content
        plan = extract_json_content(response.content)
        if not isinstance(plan, list): plan = [state['query']]
    except Exception as e:
        print(f"  ❌ Plan 解析失败: {e}")
        plan = [state['query']]
        
    return {"plan": plan, "iteration": state.get("iteration", 0) + 1}

# --- Node: Executor (Deep Read using V3.2) ---
async def execute_node(state: ResearchState):
    plan = state['plan']
    print(f"🕵️ [Executor] 启动 V3.2 进行深度搜索与阅读...")
    
    gathered_info = []
    
    for q in plan:
        try:
            print(f"  🔍 搜索关键词: {q}")
            search_result = tavily.search(query=q, max_results=2)
            
            for result in search_result['results']:
                url = result['url']
                title = result['title']
                
                # Jina 深度阅读
                full_content = scrape_jina_ai(url)
                
                # V3.2 进行信息压缩
                summary_prompt = f"""
                你是专业的研究助理。以下是从网页 "{title}" 抓取的**全文内容**。
                
                请针对用户问题 "{state['query']}"，提取最核心的：
                1. 数据表格 / 具体数值
                2. 算法公式 / 技术实现细节
                3. 专家观点
                
                如果内容无关，请输出 "无关"。
                
                网页内容片段 (前 8000 字):
                {full_content[:8000]} 
                """
                
                # ✅ 使用 llm_fast (V3.2)
                summary = await llm_fast.ainvoke([HumanMessage(content=summary_prompt)])
                
                if "无关" not in summary.content:
                    note = f"### 来源: [{title}]({url})\n{summary.content}\n"
                    gathered_info.append(note)
                    
        except Exception as e:
            print(f"  ⚠️ 处理查询 '{q}' 时出错: {e}")

    old_info = state.get('gathered_info', [])
    return {"gathered_info": old_info + gathered_info}

# --- Node: Reviewer (Devil Mode using R1) ---
def review_node(state: ResearchState):
    print("⚖️ [Reviewer] R1 开启深度审查模式...")
    
    context_window = "\n".join(state['gathered_info'][-5:])
    current_iter = state.get('iteration', 1)
    
    prompt = f"""
    用户问题: "{state['query']}"
    目前收集到的信息摘要:
    {context_window}
    
    你是极其严苛的博士生导师。请评估信息是否**完美**？
    当前是第 {current_iter} 轮搜索。
    
    规则:
    1. 如果缺少具体的**数据指标**、**技术细节**或**论文引用**，必须返回 INCOMPLETE。
    2. 只有当信息量足以写出一篇深度技术博客时，才返回 SUFFICIENT。
    
    返回 JSON:
    {{
        "status": "SUFFICIENT" 或 "INCOMPLETE",
        "critique": "缺什么信息..."
    }}
    """
    
    try:
        response = llm_smart.invoke([HumanMessage(content=prompt)])
        
        # ⚠️ 修改这里：使用 extract_json_content
        res = extract_json_content(response.content)
        
        status = res.get("status", "SUFFICIENT")
        critique = res.get("critique", "")
    except Exception as e:
        # ⚠️ 策略变更：如果解析报错，默认认为是不完整的，强制再跑一轮
        print(f"  ⚠️ Reviewer 解析异常 ({e})，强制继续搜索...")
        status = "INCOMPLETE"
        critique = "上一轮审查结果解析失败，请继续补充更多细节以确保完整性。"
        
    print(f"  -> R1 评估: {status}")
    if status == "INCOMPLETE":
         print(f"  -> R1 意见: {critique[:100]}...")

    return {"needs_more": status == "INCOMPLETE", "critique": critique}

# --- Node: Writer (Using V3.2) ---
def write_node(state: ResearchState):
    print("\n✍️ [Writer] V3.2 正在撰写深度报告...")
    all_info = "\n\n".join(state['gathered_info'])
    
    prompt = f"""
    你是世界级的研究员。请基于以下调研笔记，撰写一份结构严谨的 Markdown 报告。
    
    用户问题: {state['query']}
    
    要求:
    1. 包含摘要、目录、详细分析、结论。
    2. 引用来源（基于笔记中的 '来源'）。
    3. 逻辑清晰，数据详实。
    
    调研笔记:
    {all_info}
    """
    # ✅ 修复 Bug: 这里原本是 llm.invoke，已修正为 llm_fast (V3.2)
    response = llm_fast.invoke([HumanMessage(content=prompt)])
    return {"final_report": response.content}

# ==========================================
# 5. 图编排 (Graph Orchestration)
# ==========================================
def should_continue(state: ResearchState):
    if state['iteration'] > 2:
        print("🛑 达到最大迭代次数，强制生成报告。")
        return "writer"
    if state['needs_more']:
        return "planner"
    return "writer"

workflow = StateGraph(ResearchState)
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

# ==========================================
# 6. 主程序入口
# ==========================================
async def main():
    query = "深入对比 DeepSeek-R1 的 GRPO 算法与传统 PPO 算法的数学原理差异，并分析为何 GRPO 能降低训练成本？请列出具体的显存节省比例。"
    
    print(f"🚀 [System] 启动 Deep Research Agent (Hybrid: R1 + V3.2)...")
    print(f"🎯 目标: {query}\n")
    
    inputs = {"query": query, "iteration": 0, "gathered_info": []}
    
    try:
        result = await app.ainvoke(inputs)
        
        filename = "deep_research_report.md"
        with open(filename, "w", encoding="utf-8") as f:
            f.write(result['final_report'])
            
        print("\n" + "="*50)
        print(f"✅ 任务完成！报告已生成: {filename}")
        print("="*50)
        
    except Exception as e:
        print(f"\n❌ 程序运行出错: {e}")

if __name__ == "__main__":
    asyncio.run(main())