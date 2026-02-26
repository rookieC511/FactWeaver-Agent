import os
from dotenv import load_dotenv

load_dotenv()

# API Keys
SILICONFLOW_API_KEY = os.getenv("OPENAI_API_KEY")
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY")
SERPER_API_KEY = "82c6fd9ec91daf0c28b6be763e8933ee1091aee6"

# 数据库路径
DB_PATH = ":memory:"
COLLECTION_NAME = "research_memory"

# 模型名称配置 (方便随时切换)
MODEL_FAST = "deepseek-ai/DeepSeek-V3.2"
MODEL_SMART = "deepseek-ai/DeepSeek-R1"

# 高低配路由模型
MODEL_WORKER = "pro/zai-org/glm-4.7.online"
MODEL_CHIEF = "deepseek-ai/DeepSeek-R1"

# 专门用于长文本“蓝领”提纯任务的本地极速模型 (Ollama 实装)
MODEL_EXTRACTOR = "llama3.1:latest" 
OLLAMA_BASE_URL = "http://localhost:11434/v1"
# 硅基流动的 GLM-4V 模型 ID，请以控制台实际为准，这里用 GLM-4.7 或 Plus
MODEL_VISION = "Pro/zai-org/GLM-4.7"