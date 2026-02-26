
try:
    import langchain
    print(f"LangChain path: {langchain.__file__}")
    from langchain_core.prompts import PromptTemplate
    print("SUCCESS: langchain_core.prompts")
    import tavily
    print("SUCCESS: tavily")
except ImportError as e:
    print(f"FAIL: {e}")
