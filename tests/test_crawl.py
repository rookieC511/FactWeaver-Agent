import asyncio
import sys
import os

# Ensure we can import from current directory
sys.path.append(os.getcwd())

from core.tools import visual_browse

async def test_browse():
    target_url = "https://finance.yahoo.com/quote/NVDA/"
    goal = "latest stock price, market cap, and chart trend"
    
    print(f"🚀 Testing Visual Browser on: {target_url}")
    print("⏳ This may take 30-60 seconds...")
    
    try:
        result = await visual_browse(target_url, goal)
        print("\n✅ Result from GLM-4V:")
        print(result[:500] + "...") # Print first 500 chars
        
        # Check for screenshots
        debug_dir = "./debug_screenshots"
        if os.path.exists(debug_dir):
            files = os.listdir(debug_dir)
            if files:
                print(f"\n📸 Found {len(files)} screenshots in {debug_dir}:")
                for f in files:
                    print(f"   - {f}")
            else:
                print(f"\n⚠️ {debug_dir} exists but is empty.")
        else:
            print(f"\n⚠️ {debug_dir} not found.")
            
    except Exception as e:
        print(f"\n❌ Test failed: {e}")

if __name__ == "__main__":
    if sys.platform.startswith("win"):
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    asyncio.run(test_browse())
