"""
Architecture Stress Test — 3 Dimensions
========================================
Dim 1: GAIA (Planning Efficiency) — Step Efficiency + Backtracking Rate + Judge
Dim 2: LongBench (Distributed Logic) — See test_longbench_real.py
Dim 3: DeepResearch (Output Quality) — FACT Score + RACE Score
"""

import pytest
import json
import os
import sys
import re

# Fix Windows encoding
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# Add project root to path
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, PROJECT_ROOT)

from tests.adapter import invoke_agent, invoke_agent_with_custom_context

# --- Data Loading Helpers ---
def load_json_data(path):
    full = os.path.join(PROJECT_ROOT, "data", path)
    if not os.path.exists(full):
        return []
    with open(full, "r", encoding="utf-8") as f:
        return json.load(f)

# Use REAL GAIA L2 tasks (with annotator_steps for efficiency calc)
GAIA_TASKS = load_json_data("gaia_subset.json")
LONGBENCH_TASKS = load_json_data("longbench_subset.json")
DEEPRESEARCH_TASKS = load_json_data(os.path.join("stress_test", "deepresearch_5.json"))

REPORT_PATH = os.path.join(os.path.dirname(__file__), "stress_test_report.md")


# --- Reporting Helper ---
def save_section(title: str, content: str):
    """Append a markdown section to the stress test report."""
    with open(REPORT_PATH, "a", encoding="utf-8") as f:
        f.write(f"\n## {title}\n")
        f.write(content)
        f.write("\n---\n")


# --- Scoring Helpers ---
def judge_semantic_match(judge, question: str, gold: str, predicted: str) -> dict:
    """Use Judge to evaluate if predicted answer conveys gold answer."""
    prompt = f"""You are a strict evaluator for a benchmark test.

QUESTION: {question}
GOLD ANSWER (correct): {gold}
AGENT ANSWER (to evaluate): {predicted}

Does the agent's answer contain or convey the same information as the gold answer?
The agent's answer may be verbose, but the key fact must match.

Respond with EXACTLY:
VERDICT: CORRECT or INCORRECT
REASONING: [one sentence]
"""
    response = judge.generate(prompt)
    first_line = response.split("\n")[0].upper()
    is_correct = "CORRECT" in first_line and "INCORRECT" not in first_line
    return {"correct": is_correct, "reasoning": response.strip()[:300]}


def judge_fact_score(judge, report: str, citations: list) -> dict:
    """FACT Score: Judge evaluates citation quality and support."""
    # Extract URLs from report text
    urls_in_report = re.findall(r'https?://[^\s\)]+', report)
    
    prompt = f"""You are a research report quality evaluator.

REPORT:
{report}

CITATIONS PROVIDED: {len(citations)} URLs
URLs found in report text: {len(urls_in_report)}

Evaluate the FACT (Factual Accuracy & Citation Trust) quality:
1. Are claims supported by citations/references?
2. Is the density of citations appropriate for a PhD-level report?
3. Are there unsupported claims that should have citations?

Score from 1-10 (10 = every claim is well-cited, 1 = no citations at all).

Respond with EXACTLY:
FACT_SCORE: [number]/10
REASONING: [2-3 sentences explaining the score]
"""
    response = judge.generate(prompt)
    score_match = re.search(r'FACT_SCORE:\s*(\d+)', response)
    score = int(score_match.group(1)) if score_match else 0
    return {"fact_score": score, "reasoning": response.strip()[:400]}


def judge_race_score(judge, report: str, required_sections: list) -> dict:
    """RACE Score: Judge evaluates report structure and completeness."""
    prompt = f"""You are a research report structure evaluator.

REPORT:
{report}

REQUIRED SECTIONS: {json.dumps(required_sections)}

Evaluate the RACE (Report Architecture, Coherence & Exhaustiveness) quality:
1. Does the report cover ALL required sections listed above?
2. Is the logical flow between sections coherent?
3. Is each section substantive (not just one paragraph)?
4. Does the structure demonstrate PhD-level depth?

Score from 1-10 (10 = perfectly structured and exhaustive, 1 = missing most sections).

Respond with EXACTLY:
RACE_SCORE: [number]/10
SECTIONS_FOUND: [comma-separated list of found sections]
SECTIONS_MISSING: [comma-separated list of missing sections, or "none"]
REASONING: [2-3 sentences]
"""
    response = judge.generate(prompt)
    score_match = re.search(r'RACE_SCORE:\s*(\d+)', response)
    score = int(score_match.group(1)) if score_match else 0
    return {"race_score": score, "raw_response": response.strip()[:500]}


def count_annotator_steps(task: dict) -> int:
    """Count number of steps in GAIA annotator_steps field."""
    steps_text = task.get("annotator_steps", "")
    if not steps_text:
        return 0
    # Count numbered steps (e.g., "1.", "2.", etc.)
    return len(re.findall(r'^\d+\.', steps_text, re.MULTILINE))


# =====================================================================
#  Dimension 1: GAIA (Planning Efficiency)
# =====================================================================
@pytest.mark.parametrize(
    "task", GAIA_TASKS,
    ids=[t["task_id"][:8] for t in GAIA_TASKS]
)
def test_gaia_stress(task, judge_llm):
    """
    GAIA Level 2 Stress Test.
    Metrics: Step Efficiency, Backtracking Rate, Correctness.
    """
    question = task["question"]
    gold = task["gold_answer"]
    tid = task["task_id"][:8]
    human_steps = count_annotator_steps(task)
    
    print(f"\n{'='*65}")
    print(f"  ⚡ GAIA Stress | Task: {tid}")
    print(f"  Q: {question[:100]}...")
    print(f"  Gold: {gold} | Human Steps: {human_steps}")
    print(f"{'='*65}")
    
    try:
        result = invoke_agent(question)
        output = result["actual_output"]
        metrics = result.get("metrics", {"tool_calls": 0, "backtracking": 0})
        
        agent_steps = metrics.get("tool_calls", 0)
        backtrack = metrics.get("backtracking", 0)
        
        # Exact match check
        em = gold.strip().lower() in output.strip().lower()
        
        # Judge semantic match (graceful)
        judge_result = {"correct": em, "reasoning": "Exact match" if em else "N/A"}
        try:
            judge_result = judge_semantic_match(judge_llm, question, gold, output)
        except Exception as e:
            print(f"  ⚠️ Judge unavailable: {e}")
        
        # Step Efficiency
        efficiency = f"{human_steps}/{agent_steps}" if agent_steps > 0 else "N/A"
        status = "PASS" if (em or judge_result["correct"]) else "FAIL"
        
        print(f"  Result: {status}")
        print(f"  Steps: Agent={agent_steps}, Human={human_steps}, Efficiency={efficiency}")
        print(f"  Backtracking: {backtrack}")
        print(f"  Judge: {judge_result['reasoning'][:150]}")
        
        # Save to report
        report_content = (
            f"- **Task**: `{tid}` | **Status**: {status}\n"
            f"- **Question**: {question[:120]}...\n"
            f"- **Gold**: `{gold}` | **EM**: {em}\n"
            f"- **Agent Steps**: {agent_steps} | **Human Steps**: {human_steps} | "
            f"**Efficiency**: {efficiency}\n"
            f"- **Backtracking**: {backtrack}\n"
            f"- **Judge**: {judge_result['reasoning'][:200]}\n"
            f"\n<details><summary>Agent Output (click)</summary>\n\n"
            f"{output[:2000]}\n\n...(truncated)\n</details>\n"
        )
        save_section(f"GAIA: {tid} ({status})", report_content)
        
    except Exception as e:
        save_section(f"GAIA: {tid} (ERROR)", f"- Error: `{str(e)[:300]}`\n")
        raise e


# =====================================================================
#  Dimension 2: LongBench (Distributed Logic) — see test_longbench_real.py
# =====================================================================
@pytest.mark.parametrize(
    "task", LONGBENCH_TASKS,
    ids=lambda t: t.get("task_id", "unknown")
)
def test_longbench_stress(task):
    if not task:
        pytest.skip("No LongBench tasks loaded")
    context = task["context"]
    query = task["question"]
    tid = task.get("task_id", "unknown")
    print(f"\n⚡ [Stress] LongBench Task: {tid} (Len: {len(context)})")
    try:
        result = invoke_agent_with_custom_context(query, context)
        metrics = result.get("metrics", {})
        save_section(f"LongBench: {tid}", f"- Status: DONE\n- Report: {len(result.get('actual_output',''))} chars\n")
    except Exception as e:
        save_section(f"LongBench: {tid}", f"- Status: ERROR\n- Error: `{str(e)[:300]}`\n")
        raise e


# =====================================================================
#  Dimension 3: DeepResearch (Output Quality)
# =====================================================================
@pytest.mark.parametrize(
    "task", DEEPRESEARCH_TASKS,
    ids=lambda t: t.get("task_id", "unknown")
)
def test_deepresearch_quality(task, judge_llm):
    """
    DeepResearch PhD-level Stress Test.
    Metrics: FACT Score (citation quality) + RACE Score (structure quality).
    """
    if not task:
        pytest.skip("No DeepResearch tasks loaded")
    
    topic = task["topic"]
    tid = task["task_id"]
    required_sections = task.get("required_sections", [])
    min_citations = task.get("min_citations", 5)
    
    print(f"\n{'='*65}")
    print(f"  ⚡ DeepResearch Stress | Task: {tid}")
    print(f"  Topic: {topic}")
    print(f"  Required Sections: {required_sections}")
    print(f"  Min Citations: {min_citations}")
    print(f"{'='*65}")
    
    try:
        result = invoke_agent(f"Write a PhD-level research report on: {topic}")
        report = result["actual_output"]
        citations = result.get("citations", [])
        metrics = result.get("metrics", {})
        
        print(f"  Report Length: {len(report)} chars")
        print(f"  Citations Found: {len(citations)}")
        
        # Core assertion
        assert len(report) > 1000, f"Report too short: {len(report)} chars"
        
        # FACT Score (graceful)
        fact_result = {"fact_score": 0, "reasoning": "Judge unavailable"}
        try:
            fact_result = judge_fact_score(judge_llm, report, citations)
            print(f"  FACT Score: {fact_result['fact_score']}/10")
            print(f"  FACT Reasoning: {fact_result['reasoning'][:200]}")
        except Exception as e:
            print(f"  ⚠️ FACT Judge failed: {e}")
        
        # RACE Score (graceful)
        race_result = {"race_score": 0, "raw_response": "Judge unavailable"}
        try:
            race_result = judge_race_score(judge_llm, report, required_sections)
            print(f"  RACE Score: {race_result['race_score']}/10")
            print(f"  RACE Detail: {race_result['raw_response'][:200]}")
        except Exception as e:
            print(f"  ⚠️ RACE Judge failed: {e}")
        
        # Citation count check (soft, doesn't fail test)
        citation_met = len(citations) >= min_citations
        print(f"  Citation Threshold: {'MET' if citation_met else 'NOT MET'} "
              f"({len(citations)}/{min_citations})")
        
        # Save to report
        # Save to report summary
        report_content = (
            f"- **Topic**: {topic}\n"
            f"- **Report Length**: {len(report)} chars\n"
            f"- **Citations**: {len(citations)} (min: {min_citations}) "
            f"{'✅' if citation_met else '⚠️'}\n"
            f"- **FACT Score**: {fact_result['fact_score']}/10\n"
            f"- **RACE Score**: {race_result['race_score']}/10\n"
            f"- **Agent Steps**: {metrics.get('tool_calls', 0)}\n"
            f"\n**FACT Reasoning**:\n> {fact_result['reasoning'][:300]}\n"
            f"\n**RACE Detail**:\n> {race_result['raw_response'][:300]}\n"
            f"\n<details><summary>Report Preview (click)</summary>\n\n"
            f"{report[:3000]}\n\n...(truncated, total {len(report)} chars)\n</details>\n"
            f"\n[Full Report Saved]: `output/reports/{tid}.md`\n"
        )
        save_section(f"DeepResearch: {tid}", report_content)
        
        # Save FULL report to separate file
        os.makedirs(os.path.join(PROJECT_ROOT, "output", "reports"), exist_ok=True)
        report_path = os.path.join(PROJECT_ROOT, "output", "reports", f"{tid}.md")
        with open(report_path, "w", encoding="utf-8") as f:
            f.write(f"# Report: {topic}\n\n")
            f.write(report)
            f.write(f"\n\n---\nMetrics:\nFACT: {fact_result['fact_score']}\nRACE: {race_result['race_score']}\n")
        
    except Exception as e:
        save_section(f"DeepResearch: {tid} (ERROR)", f"- Error: `{str(e)[:300]}`\n")
        raise e


if __name__ == "__main__":
    pass
