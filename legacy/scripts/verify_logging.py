import sys
import os
import json
sys.path.append(os.getcwd())
from tests.adapter import invoke_agent

# Use a dummy task ID
task_id = "test_log_001"
query = "What is the capital of France?" # Simple query

print(f"🚀 Running agent with task_id={task_id}...")
# This will trigger the graph, which should log to trajectory_log.jsonl
try:
    # We expect this to run the planner and maybe some search steps
    # It might fail later due to missing tools or whatever, but planner should run.
    invoke_agent(query, task_id=task_id)
except Exception as e:
    print(f"Agent finished (possibly with error, which is fine for log check): {e}")

# Check log
log_file = "trajectory_log.jsonl"
if os.path.exists(log_file):
    print(f"📂 Log file found: {log_file}")
    with open(log_file, "r", encoding="utf-8") as f:
        logs = [json.loads(line) for line in f]
    
    my_logs = [l for l in logs if l.get("task_id") == task_id]
    print(f"📊 Found {len(my_logs)} logs for {task_id}")
    
    found_planner = False
    found_query = False
    
    for l in my_logs:
        event = l['event']
        print(f" - Event: {event}")
        if event == "planner_cot": found_planner = True
        if event == "query_gen": found_query = True
        
    if found_planner:
        print("✅ Planner CoT logged successfully.")
    else:
        print("❌ Planner CoT NOT found.")
        
    # Query gen might not happen if planner fails or if we stop early, but let's see.
    
else:
    print("❌ Log file NOT found!")
