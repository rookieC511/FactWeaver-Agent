
import sys
import os
import asyncio

# Ensure the project root is in sys.path so we can import the agent
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if project_root not in sys.path:
    sys.path.insert(0, project_root) # Insert at 0 to prioritize local imports


from typing import Dict, Any, List
import builtins

# PATCH: Mock input to avoid hanging during automated tests
# This allows the 'Human Review' node to be skipped/passed automatically
builtins.input = lambda prompt="": ""

def invoke_agent(query: str, task_id: str = None) -> Dict[str, Any]:
    """
    Standardized entry point for the test harness to interact with the Agent.
    
    Args:
        query (str): The research query (e.g., "DeepSeek-R1 architecture").
        task_id (str): Optional ID for trajectory logging.
        
    Returns:
        dict: A standardized dictionary containing:
            - 'actual_output': The generated final report string.
            - 'retrieval_context': A list of strings (chunks) retrieved from memory.
            - 'citations': A list of source URLs used.
            - 'history': The execution history for SFT data.
    """
    # Lazy imports to avoid import errors during test collection
    # Lazy imports to avoid import errors during test collection
    try:
        from graph import app
        from memory import km
    except ImportError as e:
        # Re-raise with clear message if dependencies are missing (like browser-use)
        raise ImportError(f"Failed to import Agent graph or memory. Check dependencies (e.g., browser-use). Error: {e}")

    
    # 1. Prepare standardized input for the graph
    inputs = {
        "query": query,
        "task_id": task_id,
        "iteration": 1, 
        "plan": [], 
        "critique": "", 
        "needs_more": True
    }
    
    # 2. Invoke the agent asynchronously
    # graph.py nodes (node_deep_research, node_writer) are async def,
    # so we must use ainvoke. asyncio.run() creates a new event loop for this.
    result = asyncio.run(app.ainvoke(inputs))
    
    # 3. Extract outputs
    final_report = result.get("final_report", "NO REPORT GENERATED")
    metrics = result.get("metrics", {"tool_calls": 0, "backtracking": 0})
    history = result.get("history", [])
    
    # 4. Retrieve Context & Citations
    # 'retrieval_context' helps the Judge checks if the Answer is supported by Context (Faithfulness)
    # We retrieve top-k chunks relevant to the query to simulate what the agent "saw".
    docs = km.retrieve(query, k=5)
    retrieval_context = [d.page_content for d in docs]
    
    # Citations are tracked in the memory manager's sets or could be parsed from the report.
    # For now, we use the 'seen_urls' from the KnowledgeManager, which tracks everything scraped.
    citations = list(km.seen_urls)
    
    return {
        "actual_output": final_report,
        "retrieval_context": retrieval_context,
        "citations": citations,
        "retrieval_context": retrieval_context,
        "citations": citations,
        "metrics": metrics,
        "history": history
    }

def invoke_agent_with_custom_context(query: str, context_str: str) -> Dict[str, Any]:
    """
    Invoke the agent but bypass external search tools, forcing it to use the provided context.
    Used for LongBench stress testing (Dispatching & Aggregation Logic).
    """
    from unittest.mock import patch
    
    # Lazy imports
    try:
        from graph import app
        from memory import km
    except ImportError:
        raise ImportError("Failed to import Agent graph or memory.")

    # 1. Inject Context
    # We add a massive chunk of text pretending to be a source.
    # The retriever inside the writer node will pick this up.
    print(f"  💉 Injecting {len(context_str)} chars into KnowledgeManager...")
    km.add_document(context_str, "injected_context_url", "Injected LongBench Source")

    # 2. Patch Search & Scrape to be no-ops
    # We want the agent to rely ONLY on what we injected.
    print("  🚫 Mocking external search tools (Tavily & Jina)...")
    with patch("tools.tavily_client.search") as mock_search, \
         patch("tools.scrape_jina_ai") as mock_scrape:
        
        # Return empty results so the Executor finds nothing new
        mock_search.return_value = {"results": []} 
        mock_scrape.return_value = "" 

        # 3. Invoke standard logic
        inputs = {
            "query": query,
            "iteration": 1, 
            "plan": [], 
            "critique": "", 
            "needs_more": True
        }
        
        # Run async loop
        try:
            result = asyncio.run(app.ainvoke(inputs))
        except Exception as e:
            print(f"  ❌ Agent execution failed: {e}")
            return {"actual_output": f"Error: {e}"}
        
        # 4. Extract
        final_report = result.get("final_report", "NO REPORT GENERATED")
        docs = km.retrieve(query, k=5)
        retrieval_context = [d.page_content for d in docs]
        citations = list(km.seen_urls)
        
        return {
            "actual_output": final_report,
            "retrieval_context": retrieval_context,
            "citations": citations
        }

