import sys
import os
import json
sys.path.append(os.getcwd())
from tests.adapter import invoke_agent
from training_recorder import TrainingDataRecorder

# Output file
sft_file = "data/sft_training_data.jsonl"
if os.path.exists(sft_file):
    os.remove(sft_file) # Clean start

print(f"🚀 Running agent for SFT verification...")

# 1. Run Agent
try:
    # Use a dummy Query
    query = "What is the capital of France?"
    result_dict = invoke_agent(query, task_id="sft_test_001")
    
    # 2. Save Trajectory
    recorder = TrainingDataRecorder(filename=sft_file)
    recorder.save_trajectory(
        task_id="sft_test_001",
        query=query,
        history=result_dict.get("history", []),
        final_report=result_dict.get("actual_output", ""),
        score=5.0
    )
    
except Exception as e:
    print(f"❌ Agent failed: {e}")

# 3. Check File
if os.path.exists(sft_file):
    print(f"📂 SFT file found: {sft_file}")
    with open(sft_file, "r", encoding="utf-8") as f:
        line = f.readline()
        data = json.loads(line)
        
    print(f"📊 Sample Keys: {list(data.keys())}")
    print(f"Messages Count: {len(data['messages'])}")
    
    roles = [m['role'] for m in data['messages']]
    print(f"Roles found: {roles}")
    
    if "thought" in roles and "tool" in roles:
        print("✅ SFT Data Structure Valid (Contains Thoughts & Tools)")
    else:
        print("⚠️ Warning: Missing thoughts or tools in history. Detailed roles: ", roles)
        
else:
    print("❌ SFT file NOT found!")
