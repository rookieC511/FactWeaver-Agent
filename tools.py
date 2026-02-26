import asyncio
import requests
import logging
import json
import re
import os
import time
import base64
import shutil
from bs4 import BeautifulSoup
from tavily import TavilyClient

# --- Browser-Use 核心组件 ---
from browser_use import Agent as BrowserAgent
from browser_use import Browser

from config import TAVILY_API_KEY, SERPER_API_KEY
from models import llm_vision

# --- Serper Client (Drop-in Replacement for Tavily due to Limit) ---
class SerperClient:
    def __init__(self, api_key):
        self.api_key = api_key
        self.url = "https://google.serper.dev/search"

    def search(self, query: str, max_results: int = 5, search_depth: str = "basic") -> dict:
        """
        Mimics TavilyClient.search() but uses Serper.dev
        """
        payload = json.dumps({
            "q": query,
            "num": max_results
        })
        headers = {
            'X-API-KEY': self.api_key,
            'Content-Type': 'application/json'
        }

        try:
            response = requests.request("POST", self.url, headers=headers, data=payload)
            response.raise_for_status()
            data = response.json()
            
            # Convert Serper 'organic' results to Tavily 'results' format
            results = []
            if "organic" in data:
                for item in data["organic"]:
                    results.append({
                        "title": item.get("title", ""),
                        "url": item.get("link", ""),
                        "content": item.get("snippet", ""),
                        "score": 1.0 # Mock score
                    })
            return {"results": results}
        except Exception as e:
            logging.error(f"Serper API Error: {e}")
            return {"results": []}

# tavily_client = TavilyClient(api_key=TAVILY_API_KEY)
tavily_client = SerperClient(api_key=SERPER_API_KEY)
logging.getLogger("qdrant_client").setLevel(logging.ERROR)

def scrape_jina_ai(url: str) -> str:
    """Level 1: Jina Reader API w/ BeautifulSoup Fallback"""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Referer": "https://www.google.com/"
    }
    
    # Attempt 1: Jina API
    try:
        resp = requests.get(f"https://r.jina.ai/{url}", headers=headers, timeout=10)
        if resp.status_code == 200:
            text = resp.text
            # Basic validation: Jina sometimes returns "Access Denied" or very short errors for protected sites
            if len(text) > 300 and "Access Denied" not in text and "Please wait while we verify you are a real person" not in text:
                return text
            else:
                logging.warning(f"Jina API returned suspicious/empty content for {url}. Triggering fallback.")
        else:
            logging.warning(f"Jina API status code {resp.status_code} for {url}. Triggering fallback.")
    except Exception as e:
        logging.warning(f"Jina API exception for {url}: {e}. Triggering fallback.")

    # Attempt 2: BeautifulSoup Fallback
    logging.info(f"🔄 Executing BeautifulSoup fallback scraper for {url}...")
    try:
        fallback_resp = requests.get(url, headers=headers, timeout=15)
        fallback_resp.raise_for_status()
        soup = BeautifulSoup(fallback_resp.text, 'html.parser')
        
        # Remove script and style elements
        for script_or_style in soup(["script", "style", "nav", "footer", "header"]):
            script_or_style.extract()
            
        # Extract <p> tags primarily, as they contain the article body usually
        # To avoid being too restrictive, we extract text and try to format it nicely
        paragraphs = soup.find_all(['p', 'h1', 'h2', 'h3', 'h4', 'li'])
        content_lines = []
        for p in paragraphs:
            text = p.get_text(separator=' ', strip=True)
            if len(text) > 20: # Filter out very short UI fragments
                content_lines.append(text)
                
        fallback_text = "\n\n".join(content_lines)
        if len(fallback_text) < 100:
             # If still nothing, just dump all text
             fallback_text = soup.get_text(separator='\n', strip=True)
             
        logging.info(f"✅ Fallback scraper successful for {url} (Length: {len(fallback_text)} chars)")
        return fallback_text
    except Exception as e:
        logging.error(f"❌ Fallback scraper also failed for {url}: {e}")
        return ""

async def visual_browse(url: str, goal: str) -> str:
    """Level 2: GLM-4V 视觉浏览器 (增强稳健版：有头模式 + 主动滚动 + 耐心等待)"""
    print(f"  👁️ [GLM-4V] 启动视觉解析: {url}")
    
    # --- 1. 伪装 Provider (解决 'no attribute' 报错) ---
    class LLMProviderWrapper:
        def __init__(self, obj):
            self.obj = obj
            self.provider = "openai" 
        def __getattr__(self, name):
            # 关键映射：browser-use 找 .model，LangChain 存 .model_name
            if name == "model": return self.obj.model_name
            return getattr(self.obj, name)

    wrapped_llm = LLMProviderWrapper(llm_vision)

    # --- 2. 浏览器初始化 (增加耐心与视窗配置) ---
    browser = None
    try:
        # 尝试导入高级配置类 (针对 0.11.x 版本)
        from browser_use.browser.context import BrowserContextConfig
        
        # ⚙️ 高级配置：模拟大屏 + 强制等待网络空闲
        config = BrowserContextConfig(
            browser_window_size={'width': 1920, 'height': 1080}, # 1080P大屏，看图更全
            wait_for_network_idle_page_load_time=3.0,          # 页面加载后强制多等3秒 (等广告/特效)
            minimum_wait_page_load_time=1.0,                   # 每步操作最少等1秒
        )
        
        browser = Browser(
            headless=False,         # 👁️ 有头模式：你会看到浏览器弹出来
            disable_security=True,  # 禁用安全策略 (CORS等)，减少加载报错
            new_context_config=config 
        )
    except ImportError:
        # 如果版本不对找不到配置类，回退到基础模式
        print("  ⚠️ [系统] 未找到 BrowserContextConfig，切换回基础模式。")
        browser = Browser(headless=False, disable_security=True)
    except Exception as e:
        print(f"  ❌ 浏览器初始化失败: {e}")
        return "浏览器初始化失败"

    try:
        # --- 3. 构建任务 Prompt (关键：教 AI 像人一样滚动) ---
        # 显式指令：先滚动触发懒加载，再找图表
        task_prompt = f"""
        导航至 {url}。
        
        **交互目标**: {goal}
        
        步骤 1: 🛑 如果页面包含 cookie 弹窗或广告，请先关闭它们。
        步骤 2: 严格执行交互目标中的操作（例如：“点击 5Y 按钮”、“点击 Maximize”）。如果需要，滚动页面找到该按钮。
        步骤 3: 🛑 重要：在交互完成后，缓慢向下滚动到底部并返回顶部，以确保所有内容（特别是图表变化）已渲染。
        步骤 4: 从最终的视觉状态中提取数据。
        
        要求：
        1. **视觉验证**：首先描述你看到的页面核心视觉元素（例如：“我看到页面左侧有导航栏，中间是...图表”），证明你真正看到了页面。
        2. **数据提取**：用中文详细返回发现结果。
        3. **操作记录**：明确说明你执行了哪些点击操作。
        """

        agent = BrowserAgent(
            task=task_prompt, 
            llm=wrapped_llm,
            browser=browser,
        )
        
        # ⚙️ 执行
        history = await agent.run()
        
        # --- 4. 安全关闭 ---
        try: await browser.close()
        except: pass
        
        final_result = str(history.history[-1].result)
        
        if history and history.history:
            # --- 🛡️ 调试：智能保存最佳截图 ---
            try:
                debug_dir = "./debug_screenshots"
                os.makedirs(debug_dir, exist_ok=True)
                
                best_screenshot_path = None
                max_size = 0
                
                # 遍历所有步骤，寻找最佳截图 (以文件大小为启发式，通常内容越丰富越大)
                for i, step in enumerate(history.history):
                    candidates = []
                    
                    # 1. 检查 state.screenshot_path (0.11.x)
                    if hasattr(step, 'state') and hasattr(step.state, 'screenshot_path'):
                        p = step.state.screenshot_path
                        if p and os.path.exists(p): candidates.append(p)
                        
                    # 2. 检查 state.screenshot (base64)
                    if hasattr(step, 'state') and hasattr(step.state, 'screenshot'):
                        b64 = step.state.screenshot
                        if b64:
                            # 临时保存 decode
                            tmp_name = f"{debug_dir}/tmp_{int(time.time())}_{i}.png"
                            with open(tmp_name, "wb") as f:
                                f.write(base64.b64decode(b64))
                            candidates.append(tmp_name)
                    
                    # 评估候选者
                    for cand in candidates:
                        try:
                            size = os.path.getsize(cand)
                            if size > max_size:
                                max_size = size
                                # 转移为正式存储
                                final_name = f"{debug_dir}/browse_{int(time.time())}_step{i}.png"
                                # 如果文件名已经是我们想要的，可以跳过复制? 还是统一重命名比较好
                                if cand != final_name:
                                    shutil.copy(cand, final_name)
                                best_screenshot_path = final_name
                        except: pass

                if best_screenshot_path:
                    print(f"    📸 [Debug] 已保存最佳截图 (Size: {max_size}): {best_screenshot_path}")
                    final_result += f"\n\n[SNAPSHOT_PATH: {best_screenshot_path}]"
                else:
                    print(f"    ⚠️ 未找到有效截图 (Steps: {len(history.history)})")

            except Exception as e_shot:
                print(f"    ⚠️ 保存截图失败: {e_shot}")

            return final_result
        return "无视觉结果。"
        
    except Exception as e:
        print(f"  ❌ 视觉解析失败: {e}")
        # --- 崩溃后的安全清理 ---
        if browser:
            try: await browser.close()
            except Exception: pass 
        return f"Error (错误): {e}"

# --- JSON 清洗工具 ---
def clean_json_output(raw_text: str) -> dict:
    if not raw_text: return {}
    try:
        json_match = re.search(r'```json\s*(.*?)\s*```', raw_text, re.DOTALL)
        if json_match: return json.loads(json_match.group(1))
        json_match = re.search(r'(\{.*\})', raw_text, re.DOTALL)
        if json_match: return json.loads(json_match.group(1))
        return json.loads(raw_text)
    except:
        return {}