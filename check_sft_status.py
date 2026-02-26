
import json
import os

root_dir = "d:/Projects/deepresearch-agent"
sft_file = os.path.join(root_dir, "data/sft_training_data.jsonl")
benchmark_file = os.path.join(root_dir, "DeepResearch-Agent-v1.jsonl")

print(f"--- Checking {sft_file} ---")
try:
    with open(sft_file, "r", encoding="utf-8") as f:
        lines = f.readlines()
        if not lines:
            print("File is empty.")
        else:
            last_line = lines[-1].strip()
            if not last_line: last_line = lines[-2] # Handle trailing newline
            data = json.loads(last_line)
            print(f"Total SFT Samples: {len(lines)}")
            print(f"Last Sample ID: {data.get('id')}")
            print(f"Last Sample Timestamp: {data.get('timestamp')}")
            messages = data.get('messages', [])
            cot_found = False
            for m in messages:
                if "<|begin_of_thought|>" in m.get("content", ""):
                    cot_found = True
                    break
            print(f"Llama-3 CoT Tag Found: {cot_found}")
            
except Exception as e:
    print(f"Error reading SFT file: {e}")

print(f"\n--- Checking {benchmark_file} ---")
try:
    with open(benchmark_file, "r", encoding="utf-8") as f:
        lines = f.readlines()
        if not lines:
            print("File is empty.")
        else:
            real_count = len([l for l in lines if l.strip()])
            print(f"Total Benchmark Tasks Completed: {real_count}")
            try:
                last_line = lines[-1].strip()
                if last_line:
                    data = json.loads(last_line)
                    print(f"Last Task ID: {data.get('id')}")
            except:
                print("Last line might be incomplete (writing in progress).")
except Exception as e:
    print(f"Error reading Benchmark file: {e}")
