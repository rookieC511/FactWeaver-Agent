
try:
    from langchain.agents import initialize_agent, AgentType
    print("Found initialize_agent")
except ImportError:
    print("initialize_agent NOT found")

try:
    from langchain_core.prompts import PromptTemplate
    print("Found PromptTemplate in core")
except ImportError:
    print("PromptTemplate NOT found in core")
