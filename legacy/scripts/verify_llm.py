import os
import builtins
import asyncio

import sys

# Fix Windows GBK encoding issue for emoji output
if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

# Patch input to avoid blocking
builtins.input = lambda prompt="": ""

print("Importing graph...")
try:
    from graph import app
    print("Graph imported successfully.")
except Exception as e:
    print(f"Graph import failed: {e}")
    exit(1)

inputs = {
    "query": "DeepSeek-R1 architecture overview",
    "iteration": 1, 
    "plan": [], 
    "critique": "", 
    "needs_more": True
}

async def main():
    print("Invoking Agent App (Async)...")
    try:
        # Use ainvoke for async graph execution
        result = await app.ainvoke(inputs)
        print("Agent Execution Completed.")
        print(f"Report length: {len(result.get('final_report', ''))}")
    except Exception as e:
        print(f"Agent Execution Failed: {e}")

if __name__ == "__main__":
    asyncio.run(main())
