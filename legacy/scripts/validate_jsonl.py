
import json

try:
    with open("DeepResearch-Agent-v1.jsonl", "r", encoding="utf-8") as f:
        line = f.readline()
        data = json.loads(line)
        print(f"✅ JSON Valid")
        print(f"ID: {data.get('id')}")
        print(f"Prompt: {data.get('prompt')[:50]}...")
        print(f"Article Length: {len(data.get('article', ''))}")
        print(f"Format Check: {'article' in data and 'id' in data and 'prompt' in data}")
        print(f"Preview: {data.get('article')[:200]}...")
except Exception as e:
    print(f"❌ Verification Failed: {e}")
