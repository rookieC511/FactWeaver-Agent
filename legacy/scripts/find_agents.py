
import langchain
print(f"LangChain Version: {langchain.__version__}")
import inspect

def find_class(module, class_name):
    if hasattr(module, class_name):
        return f"Found {class_name} in {module.__name__}"
    return None

import langchain.agents
print(find_class(langchain.agents, 'AgentExecutor'))
print(find_class(langchain.agents, 'create_react_agent'))

import langchain.agents.agent
print(find_class(langchain.agents.agent, 'AgentExecutor'))

import langchain.agents.react.agent
print(find_class(langchain.agents.react.agent, 'create_react_agent'))
