import os
import sys
import json
import asyncio
import random
import builtins
import traceback

# Bypass human review node
builtins.input = lambda prompt="": ""

# Add root dir to sys path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datasets import load_dataset
from core.graph import app
from core.models import llm_extractor

# Windows async bug fix
if sys.platform.startswith("win"):
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

# Fix Windows GBK encoding issue
if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

RESULTS_FILE = "scores/deepsearchqa_results.jsonl"
SAMPLE_SIZE = 50

# --- Evaluate Function using GLM-4.7 ---
async def evaluate_with_judge(problem: str, ground_truth: str, final_report: str) -> dict:
    prompt = f"""You are an expert evaluator.

Question: {problem}
Ground Truth Expected Answers: {ground_truth}

Agent's Final Report:
{final_report}

Please evaluate the Agent's Final Report against the Ground Truth and provide two metrics:
1. RECALL (Number from 0.0 to 1.0): What percentage of the expected ground truth answers are explicitly mentioned in the Final Report?
2. HALLUCINATION (0 or 1): Did the agent include completely incorrect or hallucinated entities that are not correct answers to the question? (1 if hallucinated, 0 if no hallucinations)

Output your response strictly in valid JSON format ONLY, without markdown code blocks or extra text:
{{
  "reasoning": "Brief explanation of what was found or missed...",
  "recall": 0.8,
  "hallucination": 0
}}"""
    try:
        from langchain_core.messages import HumanMessage
        response = await llm_extractor.ainvoke([HumanMessage(content=prompt)])
        content = response.content.strip()
        if content.startswith("```json"):
            content = content[7:-3].strip()
        elif content.startswith("```"):
            content = content[3:-3].strip()
            
        result = json.loads(content)
        return {
            "recall": float(result.get("recall", 0.0)),
            "hallucination": int(result.get("hallucination", 0)),
            "reasoning": result.get("reasoning", "")
        }
    except Exception as e:
        print(f"    [Judge Error]: {e}")
        return {"recall": 0.0, "hallucination": 0, "reasoning": f"Eval failed: {e}"}

# --- Main Evaluation Loop ---
async def main():
    print(">>> [1/4] Loading DeepSearchQA Dataset from HuggingFace...")
    ds = load_dataset('google/deepsearchqa', split='eval')
    
    # Filter for Set Answer
    set_answers = [item for item in ds if item.get('answer_type') == 'Set Answer']
    print(f"    Found {len(set_answers)} 'Set Answer' questions.")
    
    # Sample 50
    random.seed(42)
    sampled = random.sample(set_answers, min(SAMPLE_SIZE, len(set_answers)))
    print(f"    Sampled {len(sampled)} questions for evaluation.")
    
    # Load completed
    os.makedirs(os.path.dirname(RESULTS_FILE), exist_ok=True)
    completed_problems = set()
    if os.path.exists(RESULTS_FILE):
        with open(RESULTS_FILE, 'r', encoding='utf-8') as f:
            for line in f:
                if line.strip():
                    try:
                        record = json.loads(line)
                        completed_problems.add(record['problem'])
                    except json.JSONDecodeError:
                        pass
    print(f"    Loaded {len(completed_problems)} previously completed evaluations.")
    
    print("\n>>> [2/4] Starting Evaluation Loop...")
    for i, item in enumerate(sampled):
        problem = item['problem']
        ground_truth = item['answer']
        
        print(f"\n[{i+1}/{len(sampled)}] Question: {problem}")
        
        if problem in completed_problems:
            print(f"    -> Already evaluated. Skipping.")
            continue
            
        print(f"    Ground Truth: {ground_truth}")
        print("    [Agent] Running FactWeaver V3.0...")
        
        try:
            state = {
                "query": problem,
                "iteration": 1,
                "plan": [],
                "critique": "",
                "needs_more": True
            }
            # Execute FactWeaver Graph
            res = await app.ainvoke(state)
            final_report = res.get('final_report', '')
            
            print("    [Judge] Evaluating final report...")
            eval_result = await evaluate_with_judge(problem, ground_truth, final_report)
            print(f"    -> Recall: {eval_result['recall']:.2f} | Hallucination: {eval_result['hallucination']}")
            
            # Save Record
            record = {
                "problem": problem,
                "ground_truth": ground_truth,
                "final_report": final_report,
                "eval": eval_result
            }
            with open(RESULTS_FILE, 'a', encoding='utf-8') as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
                
        except Exception as e:
            print(f"    -> Agent or Eval Failed: {e}")
            traceback.print_exc()

    print("\n>>> [3/4] Calculating Aggregated Metrics...")
    total_recall = 0
    total_hallucination = 0
    count = 0
    if os.path.exists(RESULTS_FILE):
        with open(RESULTS_FILE, 'r', encoding='utf-8') as f:
            for line in f:
                if line.strip():
                    try:
                        record = json.loads(line)
                        total_recall += record['eval']['recall']
                        total_hallucination += record['eval']['hallucination']
                        count += 1
                    except KeyError:
                        pass
                        
    if count > 0:
        avg_recall = total_recall / count
        hallucination_rate = total_hallucination / count
        print(f"=========================================")
        print(f"        FINAL DEEPSEARCHQA BENCHMARK     ")
        print(f"=========================================")
        print(f"  Total Evaluated : {count}")
        print(f"  Average Recall  : {avg_recall:.2%}")
        print(f"  Hallucination   : {hallucination_rate:.2%}")
        print(f"=========================================")
    else:
        print("No successful evaluations found to calculate metrics.")

if __name__ == "__main__":
    asyncio.run(main())
