
import requests
import os

# Official DeepResearch Bench URL (located via search)
RAW_URL = "https://raw.githubusercontent.com/Ayanami0730/deep_research_bench/main/data/prompt_data/query.jsonl"

data_dir = os.path.join(os.getcwd(), "data", "prompt_data")
os.makedirs(data_dir, exist_ok=True)
file_path = os.path.join(data_dir, "query.jsonl")

print(f"Downloading official benchmark data from: {RAW_URL}")

try:
    headers = {"User-Agent": "Mozilla/5.0"}
    response = requests.get(RAW_URL, headers=headers, timeout=10)
    response.raise_for_status()
    
    content = response.text
    # Validate it's JSONL
    total_lines = 0
    import json
    for line in content.splitlines():
        if line.strip():
            json.loads(line)
            total_lines += 1
            
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(content)
        
    print(f"✅ Successfully downloaded {total_lines} queries to {file_path}")

except Exception as e:
    print(f"❌ Failed to download: {e}")
    # Fallback to dummy data if download fails? No, user wants real data.
    # We will exit with error.
    exit(1)

