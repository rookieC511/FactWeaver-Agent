import asyncio
import sys
import os

# Ensure we can import from current directory
sys.path.append(os.getcwd())

from core.tools import visual_browse

async def test_interactive():
    target_url = "https://www.google.com/finance/quote/NVDA:NASDAQ"
    # Explicit instruction to interact
    goal = "Click the '5Y' button to view the 5-year stock trend. Then extract the max price in this period."
    
    print(f"🚀 Testing Interactive Browser on: {target_url}")
    print(f"🎯 Goal: {goal}")
    
    try:
        result = await visual_browse(target_url, goal)
        print("\n✅ Result from GLM-4V:")
        print(result[:500] + "...")
        
        # Check for screenshots
        debug_dir = "./debug_screenshots"
        if os.path.exists(debug_dir):
            files = sorted([os.path.join(debug_dir, f) for f in os.listdir(debug_dir) if f.endswith(".png")], key=os.path.getmtime)
            if files:
                latest = files[-1]
                print(f"\n📸 Latest verification screenshot: {latest}")
                print("   (Please open this file to verify the '5Y' button is active)")
            else:
                print(f"\n⚠️ No screenshots found in {debug_dir}.")
        
    except Exception as e:
        print(f"\n❌ Test failed: {e}")

if __name__ == "__main__":
    if sys.platform.startswith("win"):
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    asyncio.run(test_interactive())
