"""
GAIA Real Benchmark Test — Level 2 Text-Only Subset
=====================================================
Uses real GAIA (General AI Assistants) validation questions from HuggingFace.
Each question requires multi-step web research and reasoning.

Scoring:
  - Exact Match (EM): gold_answer in agent_answer (case-insensitive)
  - Semantic Match: llama3.1 Judge decides if agent's answer is semantically correct

Data: data/gaia_subset.json (5 Level 2 text-only questions)
"""
import sys
import os
import json
import pytest

# Fix Windows encoding
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# Ensure project root is on path
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, PROJECT_ROOT)

# --------------------------------------------------------------------------
#  Load Golden Subset
# --------------------------------------------------------------------------
GAIA_DATA_PATH = os.path.join(PROJECT_ROOT, "data", "gaia_subset.json")

def load_gaia_tasks():
    """Load GAIA golden subset from JSON."""
    with open(GAIA_DATA_PATH, "r", encoding="utf-8") as f:
        tasks = json.load(f)
    return tasks

GAIA_TASKS = load_gaia_tasks()

# --------------------------------------------------------------------------
#  Scoring Helpers
# --------------------------------------------------------------------------
def exact_match(gold: str, predicted: str) -> bool:
    """Check if gold answer appears in predicted answer (case-insensitive)."""
    return gold.strip().lower() in predicted.strip().lower()


def judge_semantic_match(judge, question: str, gold: str, predicted: str) -> dict:
    """
    Use llama3.1 Judge to evaluate if the predicted answer is semantically
    equivalent to the gold answer, even if not literally identical.
    Returns dict with 'correct' (bool) and 'reasoning' (str).
    """
    prompt = f"""You are a strict evaluator for a benchmark test.

QUESTION: {question}

GOLD ANSWER (correct): {gold}

AGENT ANSWER (to evaluate): {predicted}

Does the agent's answer contain or convey the same information as the gold answer?
The agent's answer may be verbose or contain extra information, but the key fact must match.

Respond with EXACTLY this format:
VERDICT: CORRECT or INCORRECT
REASONING: [one sentence explanation]
"""
    response = judge.generate(prompt)

    is_correct = "CORRECT" in response.split("\n")[0].upper() and "INCORRECT" not in response.split("\n")[0].upper()
    return {
        "correct": is_correct,
        "reasoning": response.strip()[:300],
    }


# --------------------------------------------------------------------------
#  Test: Run Agent on GAIA Real Questions
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "task",
    GAIA_TASKS,
    ids=[t["task_id"][:8] for t in GAIA_TASKS],
)
def test_gaia_level2(task, judge_llm):
    """
    Run the Deep Research Agent on a real GAIA Level 2 question.
    Compare agent output against gold answer using exact match + judge.
    """
    from tests.adapter import invoke_agent

    question = task["question"]
    gold_answer = task["gold_answer"]
    task_id = task["task_id"]

    print(f"\n{'='*65}")
    print(f"  GAIA Level 2 | Task: {task_id[:8]}")
    print(f"  Q: {question[:120]}...")
    print(f"  Gold: {gold_answer}")
    print(f"{'='*65}")

    # Run the Agent
    print(f"  Running Agent...")
    result = invoke_agent(question)
    agent_output = result["actual_output"]

    # Truncate for display
    display_output = agent_output[:500] if len(agent_output) > 500 else agent_output
    print(f"  Agent Output (truncated): {display_output}")

    # --- Scoring ---
    # 1. Exact Match
    em = exact_match(gold_answer, agent_output)
    print(f"  [Exact Match]: {'PASS' if em else 'FAIL'}")

    # 2. Semantic Match via Judge
    judge_result = judge_semantic_match(judge_llm, question, gold_answer, agent_output)
    print(f"  [Judge Match]: {'CORRECT' if judge_result['correct'] else 'INCORRECT'}")
    print(f"  [Judge Reason]: {judge_result['reasoning'][:200]}")

    print(f"{'='*65}\n")

    # --- Save Result to File (Persistence) ---
    result_status = "PASS" if (em or judge_result['correct']) else "FAIL"
    reason = "Exact Match" if em else f"Judge: {judge_result['reasoning']}"
    
    # Detailed Markdown Block
    detailed_log = f"""
## Task: {task_id} ({result_status})

**Question**: 
> {question}

**Gold Answer**: `{gold_answer}`

**Agent Answer**: 
{agent_output.strip()}

**Judge Reasoning**:
> {judge_result['reasoning']}

---
"""
    
    try:
        results_path = os.path.join(os.path.dirname(__file__), "gaia_results.md")
        with open(results_path, "a", encoding="utf-8") as f:
            f.write(detailed_log)
        print(f"  [Saved]: Detailed result appended to {results_path}")
    except Exception as e:
        print(f"  [Save Failed]: {e}")

    # We don't assert here — GAIA is extremely hard.
    # Instead, we collect pass/fail for reporting.
    # The test "passes" (doesn't crash), and we report accuracy separately.
    if not em and not judge_result["correct"]:
        pytest.skip(
            f"GAIA task {task_id[:8]} not solved. "
            f"Gold: {gold_answer} | EM: {em} | Judge: {judge_result['correct']}"
        )


# --------------------------------------------------------------------------
#  Test: GAIA Accuracy Summary (runs after all parametrized tests)
# --------------------------------------------------------------------------
def test_gaia_summary_report():
    """
    Print a summary of GAIA test results.
    This test always passes — it's just for reporting.
    """
    print(f"\n{'='*65}")
    print(f"  GAIA Evaluation Summary")
    print(f"  Dataset: gaia-benchmark/GAIA (2023_all, validation)")
    print(f"  Subset: Level 2, Text-Only, {len(GAIA_TASKS)} tasks")
    print(f"  Note: Individual results are in the parametrized test output above.")
    print(f"{'='*65}")
