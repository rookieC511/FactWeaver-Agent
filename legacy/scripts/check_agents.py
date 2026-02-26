
import langchain.agents
print("Attributes in langchain.agents:", dir(langchain.agents))
try:
    from langchain.agents import AgentExecutor
    print("Found AgentExecutor")
except ImportError:
    print("AgentExecutor NOT found in langchain.agents")

try:
    from langchain.agents import create_react_agent
    print("Found create_react_agent")
except ImportError:
    print("create_react_agent NOT found in langchain.agents")
