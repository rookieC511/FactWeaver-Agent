import json

# Remove the bad Task 2 entry
with open('data/submission_v2_chief_editor.jsonl', 'r', encoding='utf-8') as f:
    lines = f.readlines()

good_lines = [l for l in lines if json.loads(l)['id'] != 2]

with open('data/submission_v2_chief_editor.jsonl', 'w', encoding='utf-8') as f:
    f.writelines(good_lines)

print(f"Removed Task 2. Remaining entries: {len(good_lines)}")
for l in good_lines:
    e = json.loads(l)
    print(f"  ID={e['id']}, score={e.get('score')}, len={len(e.get('article',''))} chars")
