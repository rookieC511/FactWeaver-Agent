import os
import json
import asyncio
import re
import sys
import requests
import hashlib
from typing import TypedDict, List, Dict, Any
from dotenv import load_dotenv

# --- LangChain & Core ---
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from langchain_core.messages import HumanMessage
from tavily import TavilyClient
from langgraph.graph import StateGraph, END

# --- Vector DB ---
from langchain_qdrant import QdrantVectorStore
from qdrant_client import QdrantClient
from qdrant_client.http import models
from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter

# 强制 UTF-8
sys.stdout.reconfigure(encoding='utf-8')
load_dotenv()

# ==========================================
# 1. 核心类定义：KnowledgeManager (无延迟版)
# ==========================================
class KnowledgeManager:
    def __init__(self, db_path="./deep_research_db", collection_name="research_memory"):
        print(f"⚙️ [System] 初始化知识库，存储路径: {db_path}")
        
        # 1. Embedding (硅基流动 BAAI)
        self.embeddings = OpenAIEmbeddings(
            model="BAAI/bge-m3",
            openai_api_base="https://api.siliconflow.cn/v1",
            openai_api_key=os.environ["OPENAI_API_KEY"],
            check_embedding_ctx_length=False
        )
        
        # 2. Qdrant 客户端 (持久化)
        self.client = QdrantClient(path=db_path) 
        self.collection_name = collection_name

        # 3. 确保集合存在 (1024维)
        if not self.client.collection_exists(collection_name):
            self.client.create_collection(
                collection_name=collection_name,
                vectors_config=models.VectorParams(
                    size=1024, 
                    distance=models.Distance.COSINE
                )
            )

        # 4. 连接 Store
        self.vector_store = QdrantVectorStore(
            client=self.client,
            collection_name=collection_name,
            embedding=self.embeddings,
        )
        
        # 5. 切分器
        self.splitter = RecursiveCharacterTextSplitter(chunk_size=1200, chunk_overlap=200)

    def generate_citation_hash(self, text: str) -> str:
        """生成引用哈希"""
        return hashlib.md5(text.encode()).hexdigest()[:8]

    def add_document(self, content: str, source_url: str, title: str):
        """极速写入"""
        if len(content) < 50: return 0
        if len(content) > 30000: content = content[:30000] # 放宽长度限制

        doc = Document(
            page_content=content,
            metadata={
                "source_url": source_url,
                "title": title,
            }
        )
        
        chunks = self.splitter.split_documents([doc])
        
        for chunk in chunks:
            chunk.metadata["citation_hash"] = self.generate_citation_hash(chunk.page_content)
            
        if chunks:
            try:
                # 🚀 移除所有 sleep，全速写入
                self.vector_store.add_documents(chunks)
            except Exception as e:
                print(f"  ⚠️ 存储失败: {e}")
            
        return len(chunks)

    def retrieve(self, query: str, k: int = 20) -> List[Document]:
        return self.vector_store.similarity_search(query, k=k)

# ==========================================
# 2. 初始化全局对象
# ==========================================
km = KnowledgeManager()
tavily = TavilyClient(api_key=os.environ["TAVILY_API_KEY"])

# 模型初始化
llm_fast = ChatOpenAI(
    model="deepseek-ai/DeepSeek-V3.2", 
    base_url="https://api.siliconflow.cn/v1",
    api_key=os.environ["OPENAI_API_KEY"],
    temperature=0.3
)

llm_smart = ChatOpenAI(
    model="deepseek-ai/DeepSeek-R1", 
    base_url="https://api.siliconflow.cn/v1",
    api_key=os.environ["OPENAI_API_KEY"],
    temperature=0.6 
)

# ==========================================
# 3. 辅助函数 (Utils)
# ==========================================
def safe_invoke(llm, prompt, retries=2):
    """
    极速调用版：去除了调用前的等待，仅在报错时进行最小退避
    """
    for i in range(retries):
        try:
            return llm.invoke([HumanMessage(content=prompt)])
        except Exception as e:
            # 只有真的报错了才休息一下，否则全速前进
            print(f"  ⚠️ 调用异常: {e}, 重试中...")
            import time
            time.sleep(2) 
    raise Exception("LLM 调用彻底失败")

def extract_json_content(text: str):
    text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL)
    match = re.search(r'\[.*\]', text, re.DOTALL)
    if match: return json.loads(match.group())
    match = re.search(r'\{.*\}', text, re.DOTALL)
    if match: return json.loads(match.group())
    return [text.strip()]

def scrape_jina_ai(url: str) -> str:
    try:
        print(f"    📖 [Deep Read] {url[:50]}...")
        # 缩短超时时间，读不到就过，不等待
        resp = requests.get(f"https://r.jina.ai/{url}", timeout=8) 
        return resp.text if resp.status_code == 200 else ""
    except: return ""

# ==========================================
# 4. State & Nodes
# ==========================================
class ResearchState(TypedDict):
    query: str
    plan: List[str]
    iteration: int
    critique: str
    needs_more: bool
    final_report: str

# --- Node 1: Planner ---
def plan_node(state: ResearchState):
    iter_count = state.get('iteration', 1)
    print(f"\n🧠 [Planner] R1 规划中... (Iter: {iter_count})")
    
    context = f"⚠️ 反馈: {state['critique']}" if state.get('critique') else ""
    prompt = f"""
    任务: 为 "{state['query']}" 制定搜索计划。{context}
    要求: 生成 2-3 个具体的搜索关键词。JSON数组格式。
    """
    try:
        resp = safe_invoke(llm_smart, prompt)
        plan = extract_json_content(resp.content)
        if not isinstance(plan, list): plan = [state['query']]
    except:
        plan = [state['query']]
        
    return {"plan": plan, "iteration": iter_count + 1}

# --- Node 2: Executor ---
async def execute_node(state: ResearchState):
    print(f"🕵️ [Executor] 极速搜索 & 存储...")
    plan = state['plan']
    
    # 想要更快？可以使用 asyncio.gather 实现这一步的并发
    # 这里保持简单循环，但去除了所有 sleep
    for q in plan:
        print(f"  🔍 搜: {q}")
        try:
            results = tavily.search(query=q, max_results=2)
            for res in results['results']:
                content = scrape_jina_ai(res['url'])
                if not content: continue
                
                count = km.add_document(content, res['url'], res['title'])
                if count > 0:
                    print(f"    💾 [Persist] 存入 {count} 片段")
                
        except Exception as e: print(f"  ⚠️ Error: {e}")
    return {}

# --- Node 3: Reviewer ---
def review_node(state: ResearchState):
    print("⚖️ [Reviewer] 快速审查...")
    
    docs = km.retrieve(state['query'], k=5)
    context = "\n".join([f"- {d.page_content[:200]}..." for d in docs])
    
    if not context:
        return {"needs_more": True, "critique": "知识库为空"}

    prompt = f"""
    用户问题: "{state['query']}"
    现有片段: {context}
    评估信息是否足够写深度博客？包含数据/公式？
    JSON: {{"status": "SUFFICIENT" or "INCOMPLETE", "critique": "..."}}
    """
    try:
        resp = safe_invoke(llm_smart, prompt)
        res = extract_json_content(resp.content)
        status, critique = res.get("status", "SUFFICIENT"), res.get("critique", "")
    except: status, critique = "SUFFICIENT", ""

    print(f"  -> R1 评估: {status}")
    return {"needs_more": status == "INCOMPLETE", "critique": critique}

# --- Node 4: Writer ---
def write_node(state: ResearchState):
    print("\n✍️ [Writer] 全局检索 -> 撰写报告...")
    
    # 全速模式下，我们可以尝试召回更多
    docs = km.retrieve(state['query'], k=50) # 提升到 50
    
    context_list = []
    for d in docs:
        meta = d.metadata
        ref_id = meta.get('citation_hash', 'N/A')
        context_list.append(f"引用ID [{ref_id}] (来源: {meta['source_url']}):\n{d.page_content}")
    
    all_info = "\n\n".join(context_list)
    
    prompt = f"""
    你是世界级技术作家。请基于以下知识库片段，撰写一份**超长篇、深度**技术报告。
    
    用户问题: {state['query']}
    
    【严格引用要求】
    文中每一处事实陈述，必须在句末标注引用ID，格式为 `[citation_hash]`。
    
    【内容要求】
    1. 必须包含详细的数学公式推导（使用 LaTeX）。
    2. 必须包含具体的数据对比表格。
    3. 报告长度目标：尽可能长，细节尽可能多。
    
    知识库片段:
    {all_info}
    """
    
    resp = safe_invoke(llm_fast, prompt)
    return {"final_report": resp.content}

# ==========================================
# 5. Graph
# ==========================================
def should_continue(state: ResearchState):
    if state['iteration'] > 2: return "writer"
    if state['needs_more']: return "planner"
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

async def main():
    query = "深入对比 DeepSeek-R1 的 GRPO 算法与传统 PPO 算法的数学原理差异"
    print(f"🚀 [Turbo Mode] 启动极速持久化 Agent...")
    print(f"📂 数据库路径: ./deep_research_db")
    
    try:
        res = await app.ainvoke({"query": query, "iteration": 0})
        with open("deep_research_turbo.md", "w", encoding="utf-8") as f:
            f.write(res['final_report'])
        print(f"\n✅ 报告已生成: deep_research_turbo.md")
    except Exception as e: print(f"❌ Error: {e}")

if __name__ == "__main__":
    asyncio.run(main())