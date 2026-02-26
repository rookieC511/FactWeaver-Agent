
import asyncio
from typing import List
from langchain_core.messages import HumanMessage
from tools import tavily_client
from models import llm_smart

class NaiveRAG:
    def __init__(self):
        self.llm = llm_smart
        
    async def run(self, query: str) -> str:
        print(f"📉 [Baseline] Running Naive RAG for: {query}")
        
        # 1. Simple Retrieval (One-shot Search)
        print("  🔍 Searching (Top-5)...")
        try:
            search_result = tavily_client.search(query=query, max_results=5)
            results = search_result.get('results', [])
        except Exception as e:
            print(f"  ❌ Search failed: {e}")
            results = []
            
        # 2. Context Construction
        context = ""
        for i, res in enumerate(results):
            context += f"Source {i+1} ({res.get('url')}):\n{res.get('content')}\n\n"
            
        if not context:
            context = "No information found."
            
        # 3. Generation (One-shot)
        print("  ✍️  Generating response...")
        prompt = f"""
        Question: {query}
        
        Retrieved Context:
        {context}
        
        Instructions:
        Answer the question based ONLY on the provided context. 
        If the context is insufficient, state that you don't know.
        Do not make up information.
        """
        
        response = await self.llm.ainvoke([HumanMessage(content=prompt)])
        return response.content

# Singleton for easy import
naive_rag = NaiveRAG()
