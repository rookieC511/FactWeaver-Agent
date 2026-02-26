
import json
import os

file_path = "DeepResearch-Agent-v1.jsonl"

if not os.path.exists(file_path):
    print("File not found.")
    exit()

print(f"Checking {file_path}...")
count = 0
last_id = None
try:
    with open(file_path, "r", encoding="utf-8") as f:
        for i, line in enumerate(f):
            line = line.strip()
            if not line: continue
            try:
                data = json.loads(line)
                print(f"Line {i+1}: ID {data.get('id')}")
                last_id = data.get('id')
                count += 1
            except json.JSONDecodeError as e:
                print(f"Line {i+1}: JSON Error - {e}")
except Exception as e:
    print(f"Error reading file: {e}")

print(f"Total valid records: {count}")
print(f"Last valid ID: {last_id}")
