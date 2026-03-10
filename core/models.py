import os
from langchain_openai import ChatOpenAI
from langchain_core.callbacks import BaseCallbackHandler
from typing import Dict, Any
from core.config import SILICONFLOW_API_KEY, MODEL_FAST, MODEL_SMART, MODEL_VISION, MODEL_EXTRACTOR, OLLAMA_BASE_URL, MODEL_WORKER, MODEL_CHIEF

# 通用配置
BASE_URL = "https://api.siliconflow.cn/v1"

# --- Cost Tracking Singleton ---
class CostTracker(BaseCallbackHandler):
    """
    [V4.3 成本追踪] 全局回调处理器，自动根据大模型 Token 用量计算 RMB 花费
    """
    def __init__(self):
        super().__init__()
        # 价格字典 (RMB / 1M tokens)
        self.pricing = {
            "deepseek-ai/DeepSeek-V3.2": {"input": 1.0, "output": 2.0},
            "deepseek-ai/DeepSeek-R1": {"input": 4.0, "output": 16.0},
            "Pro/zai-org/GLM-4.7": {"input": 10.0, "output": 10.0},
            "Pro/MiniMaxAI/MiniMax-M2.5": {"input": 1.5, "output": 5.0},
            "Qwen/Qwen3.5-397B-A17B": {"input": 1.2, "output": 2.0},
            "Qwen/Qwen3.5-122B-A10B": {"input": 0.8, "output": 1.2},
            "Qwen/Qwen3.5-35B-A3B": {"input": 0.3, "output": 0.6},
        }
        self.total_cost_rmb = 0.0
        self.total_tokens = 0
        
    def reset(self):
        self.total_cost_rmb = 0.0
        self.total_tokens = 0

    def on_llm_end(self, response: Any, **kwargs: Any) -> None:
        """每次 LLM 调用结束时，累加 Token 数量并算钱"""
        try:
            # 提取 LLMResult 中的 token info
            llm_output = response.llm_output or {}
            token_usage = llm_output.get("token_usage", {})
            model_name = llm_output.get("model_name", "")
            
            prompt_tokens = token_usage.get("prompt_tokens", 0)
            completion_tokens = token_usage.get("completion_tokens", 0)
            
            self.total_tokens += (prompt_tokens + completion_tokens)
            
            # 查找定价
            rates = self.pricing.get(model_name)
            # 兼容带有在线后缀或版本的名称
            if not rates:
                for k in self.pricing:
                    if k.lower() in model_name.lower():
                        rates = self.pricing[k]
                        break
                        
            if rates:
                cost = (prompt_tokens / 1_000_000) * rates["input"] + (completion_tokens / 1_000_000) * rates["output"]
                self.total_cost_rmb += cost
                print(f"💰 [Cost] {model_name}: {prompt_tokens}+{completion_tokens} tokens = ¥{cost:.5f} (Total: ¥{self.total_cost_rmb:.4f})")
        except Exception as e:
            print(f"⚠️ [Cost Tracker Error] {e}")

global_cost_tracker = CostTracker()

# 1. DeepSeek-V3 (快思考)
llm_fast = ChatOpenAI(
    model=MODEL_FAST,
    base_url=BASE_URL,
    api_key=SILICONFLOW_API_KEY,
    temperature=0.3,
    max_retries=5,
    request_timeout=300,
    callbacks=[global_cost_tracker]
)

# 2. DeepSeek-R1 (深思考)
llm_smart = ChatOpenAI(
    model=MODEL_SMART,
    base_url=BASE_URL,
    api_key=SILICONFLOW_API_KEY,
    temperature=0.6,
    max_retries=5,
    request_timeout=180,  # R1 thinking takes longer
    callbacks=[global_cost_tracker]
)

# 2.5 V3.0 提纯模型 — 切换到云端 API (极具性价比的长文本抽取)
# 之前: Pro/zai-org/GLM-4.7 (¥10/1M)
# 现在: Qwen/Qwen3.5-397B-A17B (¥1.2/1M), 性价比极高且支持超长上下文
llm_extractor = ChatOpenAI(
    model="Qwen/Qwen3.5-397B-A17B",
    base_url=BASE_URL,            # SiliconFlow API
    api_key=SILICONFLOW_API_KEY,
    temperature=0.1,  # 事实提取需要低温度
    max_retries=5,
    request_timeout=60,
    callbacks=[global_cost_tracker]
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
    callbacks=[global_cost_tracker]
)

# 4. 蓝领矿工 (便宜、跑得快) -> 给 Writer 节点用
llm_worker = ChatOpenAI(
    model=MODEL_WORKER,
    base_url=BASE_URL,
    api_key=SILICONFLOW_API_KEY,
    temperature=0.3,
    max_retries=5,
    request_timeout=120,
    callbacks=[global_cost_tracker]
)

# 5. 资本家总编 (DeepSeek-R1, 极其聪明、逻辑强) -> 只给 Chief Editor 节点用
llm_chief = ChatOpenAI(
    model=MODEL_CHIEF,
    base_url=BASE_URL,
    api_key=SILICONFLOW_API_KEY,
    temperature=0.6,  # R1 推理模型适合稍高温度
    max_retries=5,
    request_timeout=300,  # R1 thinking 需要更多时间
    callbacks=[global_cost_tracker]
)