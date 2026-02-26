
import asyncio
import sys
import os
import json
import time

# Add parent directory to path to import modules
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from benchmark.naive_rag import naive_rag
from benchmark.react_agent import react_agent
from graph import app as dra_app
from models import llm_smart
from langchain_core.messages import HumanMessage

class Evaluator:
    def __init__(self):
        self.judge_model = llm_smart

    async def get_naive_rag_output(self, query: str):
        start = time.time()
        response = await naive_rag.run(query)
        duration = time.time() - start
        return response, duration

    async def get_react_agent_output(self, query: str):
        start = time.time()
        response = await react_agent.run(query)
        duration = time.time() - start
        return response, duration

    async def get_agent_output(self, query: str):
        print(f"\n🤖 [Agent] Running Deep Research Agent for: {query}")
        start = time.time()
        initial_state = {
            "query": query,
            "plan": [],
            "iteration": 0,
            "critique": "",
            "needs_more": True,
            "final_report": ""
        }
        
        # Run the graph
        final_state = await dra_app.ainvoke(initial_state)
        duration = time.time() - start
        
        # The agent output is in final_report
        return final_state.get("final_report", "No report generated."), duration

    async def judge(self, query: str, baseline_resp: str, react_resp: str, agent_resp: str):
        print("\n⚖️  [Judge] Evaluating results...")
        
        prompt = f"""
        You are an impartial and expert judge evaluating three AI systems answering a complex research query.
        
        Query: {query}
        
        [System A: Naive RAG (Baseline)]
        {baseline_resp}

        [System B: Standard ReAct Agent (Strong Baseline)]
        {react_resp}
        
        [System C: Deep Research Agent (Ours)]
        {agent_resp}
        
        ---
        
        Please evaluate three systems based on the following three criteria:
        
        1. **Breadth**: Coverage of aspects.
        2. **Depth**: Technical depth and data support.
        3. **Faithfulness**: Citation practices and logic.
        
        Output Format:
        {{
            "analysis": {{
                "breadth": "Compare A, B, C...",
                "depth": "Compare A, B, C...",
                "faithfulness": "Compare..."
            }},
            "scores": {{
                "system_a": <0-10>,
                "system_b": <0-10>,
                "system_c": <0-10>
            }},
            "winner": "System A/B/C",
            "reason": "Why?"
        }}
        """
        
        response = await self.judge_model.ainvoke([HumanMessage(content=prompt)])
        content = response.content
        
        # Attempt to clean json
        try:
            import re
            # Remove thinking part
            content = re.sub(r'<think>.*?</think>', '', content, flags=re.DOTALL)
            match = re.search(r'\{.*\}', content, re.DOTALL)
            if match:
                return json.loads(match.group(0))
            else:
                return {"error": "Could not parse JSON", "raw": content}
        except Exception as e:
            return {"error": f"JSON Parse Error: {e}", "raw": content}

async def main():
    # Use the query from the user's recent main.py change
    query = "DeepSeek-R1 和 OpenAI o1 的推理范式有什么区别？对 2025 年算力成本有什么影响？"
    
    # 1. Run Baseline
    print("="*60)
    print("running Baseline (Naive RAG)...")
    base_out, base_time = await Evaluator().get_naive_rag_output(query)

    # 2. Run ReAct
    print("\n" + "="*60)
    print("running Strong Baseline (ReAct)...")
    react_out, react_time = await Evaluator().get_react_agent_output(query)
    
    # 3. Run Agent
    print("\n" + "="*60)
    print("running Deep Research Agent...")
    agent_out, agent_time = await Evaluator().get_agent_output(query)
    
    # 4. Judge
    result = await Evaluator().judge(query, base_out, react_out, agent_out)
    
    # 5. Save Report
    report = f"""# Benchmark Report

## Query
{query}

## Executive Summary
- **Winner**: {result.get('winner', 'Unknown')}
- **Naive RAG Duration**: {base_time:.2f}s
- **ReAct Agent Duration**: {react_time:.2f}s
- **Deep Research Agent Duration**: {agent_time:.2f}s

## Detailed Scores
| Metric | System A (Naive) | System B (ReAct) | System C (Ours) |
| :--- | :--- | :--- | :--- |
| **Score** | {result.get('scores', {}).get('system_a', 'N/A')} | {result.get('scores', {}).get('system_b', 'N/A')} | {result.get('scores', {}).get('system_c', 'N/A')} |

## Judge Analysis
{json.dumps(result.get('analysis', {}), indent=2, ensure_ascii=False)}

### Verdict Reasoning
{result.get('reason', 'N/A')}

---

## Appendix: Outputs

### System A Output (Naive RAG)
{base_out}

### System B Output (ReAct Agent)
{react_out}

### System C Output (Deep Research Agent)
{agent_out}
"""
    
    with open("benchmark/benchmark_result.md", "w", encoding="utf-8") as f:
        f.write(report)
        
    print("\n✅ Benchmark Complete! Result saved to benchmark/benchmark_result.md")

if __name__ == "__main__":
    asyncio.run(main())
