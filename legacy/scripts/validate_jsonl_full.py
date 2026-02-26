
import json
import os

filename = "DeepResearch-Agent-v1.jsonl"
valid_count = 0
corrupted = False

if not os.path.exists(filename):
    print("❌ File not found.")
    exit(1)

with open(filename, "r", encoding="utf-8") as f:
    lines = f.readlines()

print(f"Total lines read: {len(lines)}")

valid_lines = []
for i, line in enumerate(lines):
    line = line.strip()
    if not line: continue
    try:
        data = json.loads(line)
        valid_lines.append(line)
        valid_count += 1
    except json.JSONDecodeError as e:
        print(f"❌ Corruption found at line {i+1}: {e}")
        print(f"   Content: {line[:100]}...")
        corrupted = True
        # If the last line is corrupted, we can truncate it.
        if i == len(lines) - 1:
            print("   This is the last line. Suggest truncating.")
        else:
            print("   WARNING: Corruption in the middle of file!")

print(f"✅ Valid records: {valid_count}")

if corrupted and valid_count > 0:
    # Rewrite file with only valid lines
    print("⚠️  Rewriting file to remove corrupted lines...")
    with open(filename, "w", encoding="utf-8") as f:
        for line in valid_lines:
            f.write(line + "\n")
    print("✅ File repaired.")
elif valid_count == 0:
    print("⚠️  No valid records found.")
else:
    print("✅ File integrity verified.")
