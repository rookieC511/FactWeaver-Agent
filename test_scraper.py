from tools import scrape_jina_ai

# Test an easy site
print("======== Test 1: Easy Site ========")
url1 = "https://en.wikipedia.org/wiki/Deep_learning"
res1 = scrape_jina_ai(url1)
print(f"Result length: {len(res1)}")
print(res1[:200])

# Test Bloomberg (often blocks scrapers)
print("\n======== Test 2: Hard Site (Bloomberg) ========")
url2 = "https://www.bloomberg.com/markets"
res2 = scrape_jina_ai(url2)
print(f"Result length: {len(res2)}")
print(res2[:500])
