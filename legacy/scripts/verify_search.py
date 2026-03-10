from core.tools import tavily_client
import json

def test_search():
    print("Testing Serper Client...")
    try:
        results = tavily_client.search("DeepSeek-R1 release date")
        print(json.dumps(results, indent=2, ensure_ascii=False))
        if results.get("results") and len(results["results"]) > 0:
            print("✅ Search Successful!")
        else:
            print("❌ Search returned empty results.")
    except Exception as e:
        print(f"❌ Search Failed: {e}")

if __name__ == "__main__":
    test_search()
