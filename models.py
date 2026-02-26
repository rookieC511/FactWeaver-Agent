import os
from langchain_openai import ChatOpenAI
from config import SILICONFLOW_API_KEY, MODEL_FAST, MODEL_SMART, MODEL_VISION, MODEL_EXTRACTOR, OLLAMA_BASE_URL, MODEL_WORKER, MODEL_CHIEF

# 通用配置
BASE_URL = "https://api.siliconflow.cn/v1"

# 1. DeepSeek-V3 (快思考)
llm_fast = ChatOpenAI(
    model=MODEL_FAST,
    base_url=BASE_URL,
    api_key=SILICONFLOW_API_KEY,
    temperature=0.3,
    max_retries=5,
    request_timeout=300,
)

# 2. DeepSeek-R1 (深思考)
llm_smart = ChatOpenAI(
    model=MODEL_SMART,
    base_url=BASE_URL,
    api_key=SILICONFLOW_API_KEY,
    temperature=0.6,
    max_retries=5,
    request_timeout=180,  # R1 thinking takes longer
)

# 2.5 廉价极速提纯模型 (专门处理数万字长文本切分，成本极低或免费)
# 这里切换为本地 Ollama，使用 Llama 3.1 8B 专门负责苦力干活
llm_extractor = ChatOpenAI(
    model=MODEL_EXTRACTOR,
    base_url=OLLAMA_BASE_URL,
    api_key="ollama", # 任意非空字符串以绕过检查
    temperature=0.1,  # 事实提取需要低温度
    max_retries=3,
    request_timeout=300, # 本地 CPU 跑得慢的话，稍微加长一点超时时间
)

# 3. GLM-4V (视觉之眼)
# 修复：移除 model_kwargs，直接传 extra_body
llm_vision = ChatOpenAI(
    model=MODEL_VISION,
    base_url=BASE_URL,
    api_key=SILICONFLOW_API_KEY,
    temperature=0.1,
    max_tokens=2048,
    extra_body={ "top_p": 0.7 },
    max_retries=5,
    request_timeout=60,
)

# 4. 蓝领矿工 (便宜、跑得快) -> 给 Writer 节点用
llm_worker = ChatOpenAI(
    model=MODEL_WORKER,
    base_url=BASE_URL,
    api_key=SILICONFLOW_API_KEY,
    temperature=0.3,
    max_retries=5,
    request_timeout=120,
)

# 5. 资本家总编 (DeepSeek-R1, 极其聪明、逻辑强) -> 只给 Chief Editor 节点用
llm_chief = ChatOpenAI(
    model=MODEL_CHIEF,
    base_url=BASE_URL,
    api_key=SILICONFLOW_API_KEY,
    temperature=0.6,  # R1 推理模型适合稍高温度
    max_retries=5,
    request_timeout=300,  # R1 thinking 需要更多时间
)