import os

from dotenv import load_dotenv

load_dotenv()

# API keys
SILICONFLOW_API_KEY = os.getenv("OPENAI_API_KEY")
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY")
SERPER_API_KEY = os.getenv("SERPER_API_KEY", "")
SEARCH_PROVIDER = os.getenv("SEARCH_PROVIDER", "serper").strip().lower()
SEARCH_FALLBACK_PROVIDER = os.getenv("SEARCH_FALLBACK_PROVIDER", "").strip().lower()
DEFAULT_RESEARCH_MODE = os.getenv("DEFAULT_RESEARCH_MODE", "medium").strip().lower()

# Public pricing snapshots used for benchmark estimation only
SERPER_USD_PER_QUERY = float(os.getenv("SERPER_USD_PER_QUERY", "0.001"))
TAVILY_USD_PER_CREDIT = float(os.getenv("TAVILY_USD_PER_CREDIT", "0.008"))

# Legacy placeholders kept for compatibility
DB_PATH = ":memory:"
COLLECTION_NAME = "research_memory"

# Model routing
MODEL_FAST = "deepseek-ai/DeepSeek-V3.2"
MODEL_SMART = "deepseek-ai/DeepSeek-R1"
MODEL_WORKER = "deepseek-ai/DeepSeek-V3.2"
MODEL_CHIEF = "deepseek-ai/DeepSeek-R1"
MODEL_EXTRACTOR = "llama3.1:latest"
OLLAMA_BASE_URL = "http://localhost:11434/v1"
MODEL_VISION = "Pro/zai-org/GLM-4.7"

# Redis / Celery
REDIS_BROKER_URL = os.getenv("REDIS_BROKER_URL", "redis://localhost:6379/0")
REDIS_RESULT_BACKEND = os.getenv("REDIS_RESULT_BACKEND", "redis://localhost:6379/1")

# Durable runtime state
STATE_DB_PATH = os.getenv("FACTWEAVER_STATE_DB", "factweaver_state.sqlite3")
CHECKPOINT_DB_PATH = os.getenv("FACTWEAVER_CHECKPOINT_DB", "checkpoints.sqlite3")
SEMANTIC_CACHE_TTL_SECONDS = int(os.getenv("SEMANTIC_CACHE_TTL_SECONDS", "300"))

# Circuit breakers
MAX_TASK_DURATION_SECONDS = int(os.getenv("MAX_TASK_DURATION_SECONDS", "300"))
MAX_TASK_NODE_COUNT = int(os.getenv("MAX_TASK_NODE_COUNT", "15"))
MAX_TASK_RMB_COST = float(os.getenv("MAX_TASK_RMB_COST", "1.0"))
