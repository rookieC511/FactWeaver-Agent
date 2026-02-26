"""Download GAIA and build golden subset (Level 2, text-only)."""
import sys
import json
import os

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from datasets import load_dataset

print("Loading GAIA validation set...")
ds = load_dataset("gaia-benchmark/GAIA", "2023_all", split="validation")
print(f"Total tasks: {len(ds)}")

# Filter text-only tasks (no file required)
# IMPORTANT: Level is a STRING field, not int!
text_only = [item for item in ds if item.get("file_name", "X") == ""]
print(f"Text-only tasks: {len(text_only)}")

for lv in ["1", "2", "3"]:
    count = len([t for t in text_only if t.get("Level") == lv])
    print(f"  Level {lv}: {count}")

# Get Level 2 text-only tasks
level2 = [t for t in text_only if t.get("Level") == "2"]
print(f"\nLevel 2 text-only: {len(level2)} tasks")

# Show preview
for i, t in enumerate(level2[:5]):
    q = t["Question"][:150]
    a = t["Final answer"]
    print(f"  [{i}] Q: {q}")
    print(f"      A: {a}")
    print()

# Save golden subset (first 5 Level 2 text-only)
os.makedirs(os.path.join(os.path.dirname(__file__), "data"), exist_ok=True)
subset = level2[:5]

clean = []
for i, t in enumerate(subset):
    meta = t.get("Annotator Metadata", {}) or {}
    clean.append({
        "task_id": t.get("task_id", f"gaia_{i}"),
        "question": t["Question"],
        "gold_answer": t["Final answer"],
        "level": int(t["Level"]),
        "annotator_steps": meta.get("Steps", ""),
    })

outpath = os.path.join(os.path.dirname(__file__), "data", "gaia_subset.json")
with open(outpath, "w", encoding="utf-8") as f:
    json.dump(clean, f, ensure_ascii=False, indent=2)

print(f"Saved {len(clean)} golden tasks to {outpath}")
