import json

with open('data/submission_v2_chief_editor.jsonl', 'r', encoding='utf-8') as f:
    lines = f.readlines()

print(f"Total entries: {len(lines)}")
for line in lines:
    e = json.loads(line)
    print(f"\n--- ID={e['id']} | Score={e.get('score','N/A')} | Length={len(e.get('article',''))} chars ---")

# Show full article for the latest entry
last = json.loads(lines[-1])
print(f"\n{'='*60}")
print(f"LATEST ENTRY (ID={last['id']}) FULL ARTICLE:")
print(f"{'='*60}")
print(last['article'][:3000])
print(f"\n... (truncated, total {len(last['article'])} chars)")
print(f"\nScore: {last.get('score')}")
print(f"Reason: {last.get('reason','N/A')}")
