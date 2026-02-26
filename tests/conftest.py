
import pytest
import os
import torch
import gc
from deepeval.models.base_model import DeepEvalBaseLLM
from langchain_openai import ChatOpenAI

# Constants
JUDGE_MODEL_NAME = "llama3.1"
OLLAMA_BASE_URL = "http://localhost:11434/v1"

class LocalOllamaJudge(DeepEvalBaseLLM):
    """
    Custom LLM Wrapper for DeepEval to use a local Ollama instance 
    hosting the Prometheus 7B Judge model.
    """
    def __init__(self, model_name=JUDGE_MODEL_NAME):
        self.model_name = model_name
        self.llm = ChatOpenAI(
            model=model_name,
            base_url=OLLAMA_BASE_URL,
            api_key="ollama",  # Ollama doesn't require a real key
            temperature=0,
        )

    def load_model(self):
        return self.llm

    def generate(self, prompt: str) -> str:
        res = self.llm.invoke(prompt)
        return res.content

    async def a_generate(self, prompt: str) -> str:
        res = await self.llm.ainvoke(prompt)
        return res.content

    def get_model_name(self):
        return self.model_name

@pytest.fixture(scope="session")
def judge_llm():
    """
    Fixture to provide the local Judge LLM instance.
    """
    return LocalOllamaJudge()

@pytest.fixture(autouse=True)
def cleanup_vram():
    """
    Fixture explicitly run before and after each test to attempt VRAM cleanup.
    Crucial for 12GB VRAM limits when switching between Agent and Judge interactions.
    """
    # Pre-test cleanup
    gc.collect()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()
    
    yield
    
    # Post-test cleanup
    gc.collect()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()
