
import re
from typing import List
from langchain_core.messages import HumanMessage
from core.tools import tavily_client
from core.models import llm_smart

class SimpleReActAgent:
    def __init__(self, tools):
        self.tools = {t.name: t for t in tools}
        self.llm = llm_smart
        self.max_steps = 5
        
    async def run(self, query: str):
        print(f"🤖 [Baseline: ReAct] Running Custom ReAct Agent for: {query}")
        
        # Initial Prompt
        tool_desc = "\n".join([f"{name}: {t.description}" for name, t in self.tools.items()])
        tool_names = ", ".join(self.tools.keys())
        
        system_prompt = f"""Andswer the following questions as best you can. You have access to the following tools:

{tool_desc}

Use the following format:

Question: the input question you must answer
Thought: you should always think about what to do
Action: the action to take, should be one of [{tool_names}]
Action Input: the input to the action
Observation: the result of the action
... (this Thought/Action/Action Input/Observation can repeat N times)
Thought: I now know the final answer
Final Answer: the final answer to the original input question

Begin!

Question: {query}
"""
        history = system_prompt
        
        for i in range(self.max_steps):
            # Invoke LLM
            response = await self.llm.ainvoke([HumanMessage(content=history)])
            output = response.content
            print(f"  💭 Step {i+1} Output (len={len(output)})...")
            
            # Extract Action
            # Regex to find Action: ... and Action Input: ...
            action_match = re.search(r"Action:\s*(.*?)\nAction Input:\s*(.*)", output)
            
            if "Final Answer:" in output:
                # Found answer
                final_answer = output.split("Final Answer:")[-1].strip()
                return final_answer
            
            if not action_match:
                # LLM didn't follow format or wants to stop
                print("  ⚠️ No action found, stopping.")
                history += f"\n{output}\nObservation: Invalid format. Please use Action: and Action Input: or Final Answer:"
                continue
                
            action = action_match.group(1).strip()
            action_input = action_match.group(2).strip()
            
            print(f"  🛠️ Action: {action}('{action_input}')")
            
            # Execute Tool
            params = action_input.strip('"').strip("'")
            
            observation = f"Error: Tool {action} not found."
            if action in self.tools:
                try:
                    # Specific to our tools wrapper
                    observation = self.tools[action].func(params)
                except Exception as e:
                    observation = f"Error executing tool: {e}"
            
            str_obs = str(observation)
            if len(str_obs) > 500: str_obs = str_obs[:500] + "..."
            print(f"    -> Observation: {str_obs}")
            
            # Append interaction to history for next turn
            # We assume the LLM output is just the Thought + Action + Input
            # We append the Observation
            
            # Note: We need to be careful not to double append if LLM output included hallucinations
            # A simple way is just to take what LLM generated, cut off after Action Input, and append our Observation.
            
            # Let's trust the LLM output for now but append Observation
            history += f"\n{output}\nObservation: {observation}\n"

        return "Agent Reached Max Steps without Final Answer."

# --- Setup ---

class ToolWrapper:
    def __init__(self, name, func, description):
        self.name = name
        self.func = func
        self.description = description

def search_func(query):
    try:
        res = tavily_client.search(query=query, max_results=5)
        return "\n".join([f"({r['url']}) {r['content']}" for r in res.get('results', [])])
    except Exception as e:
        return f"Search Error: {e}"

react_agent = SimpleReActAgent(tools=[
    ToolWrapper("Search", search_func, "Search the web for information.")
])
