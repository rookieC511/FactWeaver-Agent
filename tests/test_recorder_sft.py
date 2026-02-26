
import json
import os
import sys

# Add project root to path
sys.path.append(os.getcwd())

from training_recorder import TrainingDataRecorder

def test_sft_recorder():
    test_file = "data/test_sft_data.jsonl"
    if os.path.exists(test_file):
        os.remove(test_file)
        
    recorder = TrainingDataRecorder(filename=test_file)
    
    # Test 1: Low Score (Should be skipped)
    print("Testing Low Score Filter (< 7.0)...")
    recorder.save_trajectory(
        task_id="test_low_score",
        query="test query",
        history=[],
        final_report="bad report",
        score=6.9
    )
    
    if os.path.exists(test_file):
        print("❌ Failed: Low score task was saved!")
    else:
        print("✅ Passed: Low score task was skipped.")
        
    # Test 2: High Score & Formatting
    print("\nTesting High Score & Formatting...")
    history = [
        {
            "role": "planner_cot",
            "content": {"search_tasks": ["task1"], "outline": ["sec1"]}
        },
        {
            "role": "query_gen",
            "content": {"task_desc": "find revenue", "generated_queries": ["revenue 2024"]}
        },
        {
            "role": "search_error",
            "content": {"query": "bad query", "error": "404 Not Found"}
        }
    ]
    
    recorder.save_trajectory(
        task_id="test_high_score",
        query="analyze deepseek revenue",
        history=history,
        final_report="Good report",
        score=8.5
    )
    
    if not os.path.exists(test_file):
        print("❌ Failed: High score task was NOT saved!")
        return

    with open(test_file, "r", encoding="utf-8") as f:
        line = f.readline()
        data = json.loads(line)
        
    # Verify Content
    messages = data["messages"]
    
    # Check CoT
    cot_msg = next((m for m in messages if "<|begin_of_thought|>" in m["content"]), None)
    if cot_msg:
        print("✅ Passed: CoT formatting detected.")
        if "Here is the research plan breakdown" in cot_msg["content"]:
             print("✅ Passed: CoT natural language wrapper detected.")
    else:
        print("❌ Failed: CoT formatting missing.")
        
    # Check Query Gen
    qgen_msg = next((m for m in messages if "To find detailed information about" in m["content"]), None)
    if qgen_msg:
        print("✅ Passed: Query Gen formatting detected.")
    else:
        print("❌ Failed: Query Gen formatting missing.")

    # Check Error Reflection
    err_msg = next((m for m in messages if "Search failed for" in m["content"] and "<|begin_of_thought|>" in m["content"]), None)
    if err_msg:
        print("✅ Passed: Error Refection formatting detected.")
    else:
        print("❌ Failed: Error Refection formatting missing.")
        
    print("\nVerification Complete.")
    
if __name__ == "__main__":
    test_sft_recorder()
