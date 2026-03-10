import asyncio
import sys
import builtins
from unittest.mock import MagicMock, patch

# Mock memory to avoid database dependencies
sys.modules['memory'] = MagicMock()
mock_km = MagicMock()
sys.modules['memory'].km = mock_km

# Mock tavily to avoid import errors
sys.modules['tavily'] = MagicMock()
sys.modules['browser_use'] = MagicMock()

# Mock retrieve to return something usable
class MockDoc:
    def __init__(self, content, metadata):
        self.page_content = content
        self.metadata = metadata

mock_km.retrieve.return_value = [
    MockDoc("NVIDIA reported record revenue in Data Center.", {'citation_hash': 'hash1'}),
    MockDoc("Data center revenue grew 427% year-over-year.", {'citation_hash': 'hash2'})
]

# Function to patch input
def mock_input(prompt=""):
    print(f"[MockInput] Prompt: {prompt}")
    print("[MockInput] Auto-reply: '' (Enter)")
    return ""

# Import the app AFTER mocking
# We need to ensure we don't import 'memory' inside writer_graph before our mock is in sys.modules
# (which we did above)
from core import writer_graph

async def run_test():
    print("🚀 Starting Writer Graph Test...")
    
    initial_state = {
        "query": "Analysis of NVIDIA Data Center Revenue",
        "outline": [],
        "sections": {},
        "final_doc": "",
        "iteration": 0
    }
    
    # Run user input patch
    with patch('builtins.input', side_effect=mock_input):
        result = await writer_graph.writer_app.ainvoke(initial_state)
    
    print("\n" + "="*50)
    print("RESULT DOCUMENT:")
    print("="*50)
    print(result['final_doc'])
    print("="*50)
    
    # Check for numbered headers
    lines = result['final_doc'].split('\n')
    numbered_headers = [line for line in lines if line.startswith('#') and any(char.isdigit() for char in line.split()[1] if len(line.split()) > 1)]
    
    # This check is heuristic. "## 1. Intro" -> split()[1] is "1." which has digit.
    # "## Intro" -> split()[1] is "Intro", no digit.
    
    possible_violations = []
    for line in lines:
        if line.strip().startswith("#"):
            parts = line.split()
            if len(parts) > 1:
                first_word = parts[1]
                # Check if first word looks like "1." or "1.1"
                if first_word[0].isdigit() and (first_word.endswith('.') or '.' in first_word):
                    possible_violations.append(line)
    
    if possible_violations:
        print(f"⚠️  WARNING: Found possible numbered headers: {possible_violations}")
    else:
        print("✅ SUCCESS: No numbered headers detected.")

if __name__ == "__main__":
    if sys.platform.startswith("win"):
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    asyncio.run(run_test())
