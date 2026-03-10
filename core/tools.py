import ast
import base64
import math
import json
import logging
import os
import re
import shutil
import time
from typing import Any

import httpx
import requests
from bs4 import BeautifulSoup

from core.config import (
    SEARCH_FALLBACK_PROVIDER,
    SEARCH_PROVIDER,
    SERPER_API_KEY,
    SERPER_USD_PER_QUERY,
    TAVILY_API_KEY,
    TAVILY_USD_PER_CREDIT,
)
from core.models import llm_vision

logger = logging.getLogger(__name__)


class LLMFormatError(Exception):
    def __init__(self, raw_text: str, parse_error: str):
        self.raw_text = raw_text
        self.parse_error = parse_error
        super().__init__(f"JSON parse failed: {parse_error}")


class ToolExecutionError(Exception):
    def __init__(self, tool_name: str, url: str, status_code: int, message: str):
        self.tool_name = tool_name
        self.url = url
        self.status_code = status_code
        self.message = message
        super().__init__(f"[{tool_name}] {url} -> HTTP {status_code}: {message}")


def default_cost_breakdown() -> dict[str, float | int]:
    return {
        "llm_cost_rmb": 0.0,
        "external_cost_usd_est": 0.0,
        "serper_queries": 0,
        "serper_cost_usd_est": 0.0,
        "tavily_credits_est": 0.0,
        "tavily_cost_usd_est": 0.0,
    }


def default_retrieval_metrics() -> dict[str, int]:
    return {
        "search_calls": 0,
        "extract_calls": 0,
        "map_calls": 0,
        "crawl_calls": 0,
        "fallback_count": 0,
        "visual_browse_calls": 0,
    }


def record_serper_query(cost_breakdown: dict[str, float | int], successful_queries: int = 1) -> dict[str, float | int]:
    cost_breakdown = dict(cost_breakdown)
    cost_breakdown["serper_queries"] = int(cost_breakdown.get("serper_queries", 0)) + successful_queries
    cost_breakdown["serper_cost_usd_est"] = round(
        float(cost_breakdown.get("serper_cost_usd_est", 0.0)) + (successful_queries * SERPER_USD_PER_QUERY),
        6,
    )
    cost_breakdown["external_cost_usd_est"] = round(
        float(cost_breakdown.get("external_cost_usd_est", 0.0)) + (successful_queries * SERPER_USD_PER_QUERY),
        6,
    )
    return cost_breakdown


def record_tavily_credits(cost_breakdown: dict[str, float | int], credits: float) -> dict[str, float | int]:
    usd = credits * TAVILY_USD_PER_CREDIT
    cost_breakdown = dict(cost_breakdown)
    cost_breakdown["tavily_credits_est"] = round(float(cost_breakdown.get("tavily_credits_est", 0.0)) + credits, 4)
    cost_breakdown["tavily_cost_usd_est"] = round(
        float(cost_breakdown.get("tavily_cost_usd_est", 0.0)) + usd,
        6,
    )
    cost_breakdown["external_cost_usd_est"] = round(
        float(cost_breakdown.get("external_cost_usd_est", 0.0)) + usd,
        6,
    )
    return cost_breakdown


def tavily_search_credits(search_depth: str) -> int:
    return 2 if (search_depth or "").strip().lower() == "advanced" else 1


def tavily_extract_credits(successful_urls: int, extract_depth: str) -> int:
    if successful_urls <= 0:
        return 0
    rate = 2 if (extract_depth or "").strip().lower() == "advanced" else 1
    return math.ceil(successful_urls / 5) * rate


def tavily_map_credits(successful_pages: int, has_instructions: bool) -> int:
    if successful_pages <= 0:
        return 0
    rate = 2 if has_instructions else 1
    return math.ceil(successful_pages / 10) * rate


def tavily_crawl_credits(
    successful_pages: int,
    *,
    extract_depth: str,
    has_instructions: bool = False,
) -> int:
    if successful_pages <= 0:
        return 0
    return tavily_map_credits(successful_pages, has_instructions) + tavily_extract_credits(
        successful_pages,
        extract_depth,
    )


class BaseSearchClient:
    provider_name = "base"

    def __init__(self, api_key: str):
        self.api_key = api_key

    def is_configured(self) -> bool:
        return bool(self.api_key)

    def search(self, query: str, max_results: int = 5, search_depth: str = "basic") -> dict:
        raise NotImplementedError

    async def asearch(self, query: str, max_results: int = 5, search_depth: str = "basic") -> dict:
        raise NotImplementedError


class SerperClient(BaseSearchClient):
    provider_name = "serper"

    def __init__(self, api_key: str):
        super().__init__(api_key)
        self.url = "https://google.serper.dev/search"

    def _normalize_results(self, data: dict[str, Any]) -> dict[str, Any]:
        results = []
        for item in data.get("organic", []):
            results.append(
                {
                    "title": item.get("title", ""),
                    "url": item.get("link", ""),
                    "content": item.get("snippet", ""),
                    "score": 1.0,
                }
            )
        return {"results": results}

    def search(self, query: str, max_results: int = 5, search_depth: str = "basic") -> dict:
        if not self.api_key:
            logger.warning("Serper search skipped because SERPER_API_KEY is not configured")
            return {"results": []}
        payload = json.dumps({"q": query, "num": max_results})
        headers = {"X-API-KEY": self.api_key, "Content-Type": "application/json"}
        try:
            response = requests.post(self.url, headers=headers, data=payload, timeout=15)
            response.raise_for_status()
            return self._normalize_results(response.json())
        except Exception as exc:
            logger.error("Serper search failed: %s", exc)
            return {"results": []}

    async def asearch(self, query: str, max_results: int = 5, search_depth: str = "basic") -> dict:
        if not self.api_key:
            logger.warning("Serper async search skipped because SERPER_API_KEY is not configured")
            return {"results": []}
        payload = {"q": query, "num": max_results}
        headers = {"X-API-KEY": self.api_key, "Content-Type": "application/json"}
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.post(self.url, headers=headers, json=payload)
            if response.status_code in (429, 500, 502, 503):
                raise ToolExecutionError(
                    "SerperClient",
                    self.url,
                    response.status_code,
                    f"Search API unavailable for query: {query}",
                )
            response.raise_for_status()
            return self._normalize_results(response.json())
        except ToolExecutionError:
            raise
        except Exception as exc:
            logger.error("Serper async search failed: %s", exc)
            return {"results": []}


class TavilySearchClient(BaseSearchClient):
    provider_name = "tavily"

    def __init__(self, api_key: str):
        super().__init__(api_key)
        self.url = "https://api.tavily.com/search"

    def _normalize_results(self, data: dict[str, Any]) -> dict[str, Any]:
        results = []
        for item in data.get("results", []):
            results.append(
                {
                    "title": item.get("title", ""),
                    "url": item.get("url", ""),
                    "content": item.get("content", "") or item.get("raw_content", "") or "",
                    "score": item.get("score", 1.0),
                }
            )
        return {"results": results}

    def search(self, query: str, max_results: int = 5, search_depth: str = "basic") -> dict:
        if not self.api_key:
            logger.warning("Tavily search skipped because TAVILY_API_KEY is not configured")
            return {"results": []}
        payload = {
            "api_key": self.api_key,
            "query": query,
            "max_results": max_results,
            "search_depth": search_depth,
            "include_answer": False,
            "include_raw_content": False,
        }
        try:
            response = requests.post(self.url, json=payload, timeout=20)
            response.raise_for_status()
            return self._normalize_results(response.json())
        except Exception as exc:
            logger.error("Tavily search failed: %s", exc)
            return {"results": []}

    async def asearch(self, query: str, max_results: int = 5, search_depth: str = "basic") -> dict:
        if not self.api_key:
            logger.warning("Tavily async search skipped because TAVILY_API_KEY is not configured")
            return {"results": []}
        payload = {
            "api_key": self.api_key,
            "query": query,
            "max_results": max_results,
            "search_depth": search_depth,
            "include_answer": False,
            "include_raw_content": False,
        }
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.post(self.url, json=payload)
            if response.status_code in (429, 500, 502, 503):
                raise ToolExecutionError(
                    "TavilySearchClient",
                    self.url,
                    response.status_code,
                    f"Search API unavailable for query: {query}",
                )
            response.raise_for_status()
            return self._normalize_results(response.json())
        except ToolExecutionError:
            raise
        except Exception as exc:
            logger.error("Tavily async search failed: %s", exc)
            return {"results": []}


class TavilyExtractClient:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.url = "https://api.tavily.com/extract"

    def is_configured(self) -> bool:
        return bool(self.api_key)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    async def aextract(
        self,
        urls: list[str],
        *,
        query: str,
        chunks_per_source: int,
        extract_depth: str,
        include_images: bool = False,
        include_favicon: bool = False,
    ) -> dict[str, Any]:
        if not self.api_key or not urls:
            return {"results": [], "failed_results": []}
        payload = {
            "urls": urls,
            "query": query,
            "chunks_per_source": chunks_per_source,
            "extract_depth": extract_depth,
            "include_images": include_images,
            "include_favicon": include_favicon,
        }
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(self.url, headers=self._headers(), json=payload)
            if response.status_code in (429, 500, 502, 503):
                raise ToolExecutionError("TavilyExtractClient", self.url, response.status_code, "Extract unavailable")
            response.raise_for_status()
            return response.json()
        except ToolExecutionError:
            raise
        except Exception as exc:
            logger.error("Tavily extract failed: %s", exc)
            return {"results": [], "failed_results": []}


class TavilyMapClient:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.url = "https://api.tavily.com/map"

    def is_configured(self) -> bool:
        return bool(self.api_key)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    async def amap(
        self,
        url: str,
        *,
        limit: int,
        max_depth: int,
        max_breadth: int,
        allow_external: bool = False,
        instructions: str | None = None,
    ) -> dict[str, Any]:
        if not self.api_key or not url:
            return {"results": []}
        payload: dict[str, Any] = {
            "url": url,
            "limit": limit,
            "max_depth": max_depth,
            "max_breadth": max_breadth,
            "allow_external": allow_external,
        }
        if instructions:
            payload["instructions"] = instructions
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(self.url, headers=self._headers(), json=payload)
            if response.status_code in (429, 500, 502, 503):
                raise ToolExecutionError("TavilyMapClient", url, response.status_code, "Map unavailable")
            response.raise_for_status()
            return response.json()
        except ToolExecutionError:
            raise
        except Exception as exc:
            logger.error("Tavily map failed: %s", exc)
            return {"results": []}


class TavilyCrawlClient:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.url = "https://api.tavily.com/crawl"

    def is_configured(self) -> bool:
        return bool(self.api_key)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    async def acrawl(
        self,
        url: str,
        *,
        limit: int,
        max_depth: int,
        max_breadth: int,
        extract_depth: str,
        allow_external: bool = False,
        include_images: bool = False,
    ) -> dict[str, Any]:
        if not self.api_key or not url:
            return {"results": []}
        payload = {
            "url": url,
            "limit": limit,
            "max_depth": max_depth,
            "max_breadth": max_breadth,
            "extract_depth": extract_depth,
            "allow_external": allow_external,
            "include_images": include_images,
        }
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(self.url, headers=self._headers(), json=payload)
            if response.status_code in (429, 500, 502, 503):
                raise ToolExecutionError("TavilyCrawlClient", url, response.status_code, "Crawl unavailable")
            response.raise_for_status()
            return response.json()
        except ToolExecutionError:
            raise
        except Exception as exc:
            logger.error("Tavily crawl failed: %s", exc)
            return {"results": []}


class SearchClientRouter:
    def __init__(self, providers: list[BaseSearchClient]):
        self.providers = providers
        self.provider_chain = [provider.provider_name for provider in providers]

    def search(self, query: str, max_results: int = 5, search_depth: str = "basic") -> dict:
        last_error: ToolExecutionError | None = None
        for provider in self.providers:
            try:
                result = provider.search(query=query, max_results=max_results, search_depth=search_depth)
                if result.get("results"):
                    return result
            except ToolExecutionError as exc:
                last_error = exc
                logger.warning("Search provider %s failed: %s", provider.provider_name, exc)
        if last_error:
            raise last_error
        return {"results": []}

    async def asearch(self, query: str, max_results: int = 5, search_depth: str = "basic") -> dict:
        last_error: ToolExecutionError | None = None
        for provider in self.providers:
            try:
                result = await provider.asearch(
                    query=query,
                    max_results=max_results,
                    search_depth=search_depth,
                )
                if result.get("results"):
                    return result
            except ToolExecutionError as exc:
                last_error = exc
                logger.warning("Search provider %s failed: %s", provider.provider_name, exc)
        if last_error:
            raise last_error
        return {"results": []}


def _build_search_provider(provider_name: str) -> BaseSearchClient | None:
    provider_name = (provider_name or "").strip().lower()
    if provider_name == "serper":
        return SerperClient(api_key=SERPER_API_KEY)
    if provider_name == "tavily":
        return TavilySearchClient(api_key=TAVILY_API_KEY or "")
    return None


def _resolve_search_provider_chain() -> tuple[SearchClientRouter, str]:
    provider_names = []
    for candidate in [SEARCH_PROVIDER, SEARCH_FALLBACK_PROVIDER]:
        if candidate and candidate not in provider_names:
            provider_names.append(candidate)

    if not provider_names:
        provider_names.append("serper")

    providers = []
    for provider_name in provider_names:
        provider = _build_search_provider(provider_name)
        if provider and provider.is_configured():
            providers.append(provider)

    if not providers:
        for fallback_name in ("serper", "tavily"):
            provider = _build_search_provider(fallback_name)
            if provider and provider.is_configured():
                providers.append(provider)
                break

    if not providers:
        providers.append(SerperClient(api_key=""))

    router = SearchClientRouter(providers)
    resolved_name = " -> ".join(router.provider_chain)
    return router, resolved_name


search_client, search_provider_name = _resolve_search_provider_chain()
tavily_client = search_client
serper_client = SerperClient(api_key=SERPER_API_KEY)
tavily_search_client = TavilySearchClient(api_key=TAVILY_API_KEY or "")
tavily_extract_client = TavilyExtractClient(api_key=TAVILY_API_KEY or "")
tavily_map_client = TavilyMapClient(api_key=TAVILY_API_KEY or "")
tavily_crawl_client = TavilyCrawlClient(api_key=TAVILY_API_KEY or "")


def heuristic_dom_probe(html_content: str, extracted_text: str) -> str | None:
    soup = BeautifulSoup(html_content, "html.parser")
    svg_count = len(soup.find_all(["svg", "canvas", "math"]))
    chart_tags = soup.find_all(class_=re.compile(r"chart|graph|echarts|highcharts|plotly", re.I))
    if svg_count > 3 or len(chart_tags) > 1:
        return (
            f"[VLM_REQUIRED: Detected {svg_count} svg/canvas tags and "
            f"{len(chart_tags)} chart components]"
        )

    lower_text = (extracted_text or "").lower()
    if len(extracted_text or "") < 200 and any(
        kw in lower_text
        for kw in ["please wait while we verify", "checking your browser", "enable javascript", "loading data"]
    ):
        return "[VLM_REQUIRED: Detected anti-scraping challenge or JS-required loading placeholder]"

    body = soup.find("body")
    if body:
        html_len = len(str(body))
        text_len = len((extracted_text or "").strip())
        if html_len > 15000 and text_len < 50:
            return f"[VLM_REQUIRED: Severe text-to-visual ratio mismatch (HTML:{html_len} vs Text:{text_len})]"

    return None


async def scrape_jina_ai(url: str) -> str:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Referer": "https://www.google.com/",
    }
    jina_status = 0
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        try:
            resp = await client.get(f"https://r.jina.ai/{url}", headers=headers)
            jina_status = resp.status_code
            if resp.status_code == 200:
                text = resp.text
                if (
                    len(text) > 300
                    and "Access Denied" not in text
                    and "Please wait while we verify you are a real person" not in text
                ):
                    return text
        except Exception as exc:
            logger.warning("Jina fetch failed for %s: %s", url, exc)

        try:
            fallback_resp = await client.get(url, headers=headers)
            if fallback_resp.status_code in (403, 429, 500, 502, 503):
                raise ToolExecutionError(
                    "scrape_jina_ai",
                    url,
                    fallback_resp.status_code,
                    f"Scraping blocked (Jina={jina_status}, Fallback={fallback_resp.status_code})",
                )
            fallback_resp.raise_for_status()
            soup = BeautifulSoup(fallback_resp.text, "html.parser")
            for node in soup(["script", "style", "nav", "footer", "header"]):
                node.extract()

            paragraphs = soup.find_all(["p", "h1", "h2", "h3", "h4", "li"])
            content_lines = []
            for paragraph in paragraphs:
                text = paragraph.get_text(separator=" ", strip=True)
                if len(text) > 20:
                    content_lines.append(text)
            fallback_text = "\n\n".join(content_lines) or soup.get_text(separator="\n", strip=True)

            probe_result = heuristic_dom_probe(fallback_resp.text, fallback_text)
            if probe_result:
                return probe_result
            return fallback_text
        except ToolExecutionError:
            raise
        except Exception as exc:
            logger.error("Fallback scrape failed for %s: %s", url, exc)
            return ""


def _normalize_json_like_text(raw_text: str) -> str:
    normalized = raw_text.strip().replace("\ufeff", "")
    replacements = {
        "“": '"',
        "”": '"',
        "‘": "'",
        "’": "'",
        "：": ":",
        "，": ",",
        "（": "(",
        "）": ")",
    }
    for source, target in replacements.items():
        normalized = normalized.replace(source, target)
    normalized = re.sub(r",(\s*[}\]])", r"\1", normalized)
    return normalized


def _extract_json_candidates(raw_text: str) -> list[str]:
    candidates: list[str] = []
    text = _normalize_json_like_text(raw_text)

    fenced = re.findall(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.DOTALL | re.IGNORECASE)
    candidates.extend(fenced)

    for open_char, close_char in (("{", "}"), ("[", "]")):
        start = text.find(open_char)
        end = text.rfind(close_char)
        if start != -1 and end != -1 and end > start:
            candidates.append(text[start : end + 1])

    candidates.append(text)
    seen = set()
    deduped = []
    for candidate in candidates:
        candidate = candidate.strip()
        if candidate and candidate not in seen:
            deduped.append(candidate)
            seen.add(candidate)
    return deduped


def _try_complete_brackets(candidate: str) -> str:
    fixed = candidate
    brace_delta = fixed.count("{") - fixed.count("}")
    bracket_delta = fixed.count("[") - fixed.count("]")
    if brace_delta > 0:
        fixed += "}" * brace_delta
    if bracket_delta > 0:
        fixed += "]" * bracket_delta
    return fixed


def _parse_json_candidate(candidate: str) -> Any:
    attempts = [
        candidate,
        _try_complete_brackets(candidate),
        re.sub(r",(\s*[}\]])", r"\1", _try_complete_brackets(candidate)),
    ]
    for attempt in attempts:
        try:
            return json.loads(attempt)
        except Exception:
            continue

    try:
        literal = ast.literal_eval(candidate)
        if isinstance(literal, (dict, list)):
            return literal
    except Exception:
        pass
    raise ValueError("Could not parse candidate as JSON-like payload")


def clean_json_output(raw_text: str, strict: bool = False) -> Any:
    if not raw_text:
        if strict:
            raise LLMFormatError(raw_text or "", "Empty input")
        return {}

    errors = []
    for candidate in _extract_json_candidates(str(raw_text)):
        try:
            return _parse_json_candidate(candidate)
        except Exception as exc:
            errors.append(str(exc))

    parse_error = "; ".join(errors) if errors else "No JSON candidates found"
    if strict:
        raise LLMFormatError(str(raw_text)[:500], parse_error)
    logger.warning("JSON parse failed: %s | Raw: %s", parse_error, str(raw_text)[:200])
    return {}


async def visual_browse(url: str, goal: str) -> str:
    try:
        from browser_use import Agent as BrowserAgent  # type: ignore
        from browser_use import Browser  # type: ignore
    except Exception as exc:
        return f"[VISUAL_BROWSE_UNAVAILABLE] browser_use is not installed: {exc}"

    class LLMProviderWrapper:
        def __init__(self, obj):
            self.obj = obj
            self.provider = "openai"

        def __getattr__(self, name):
            if name == "model":
                return self.obj.model_name
            return getattr(self.obj, name)

    wrapped_llm = LLMProviderWrapper(llm_vision)
    browser = None
    try:
        try:
            from browser_use.browser.context import BrowserContextConfig  # type: ignore

            config = BrowserContextConfig(
                browser_window_size={"width": 1920, "height": 1080},
                wait_for_network_idle_page_load_time=3.0,
                minimum_wait_page_load_time=1.0,
            )
            browser = Browser(headless=False, disable_security=True, new_context_config=config)
        except Exception:
            browser = Browser(headless=False, disable_security=True)

        task_prompt = f"""
Navigate to {url}

Goal: {goal}

1. Close cookie or ad popups if they appear.
2. Execute the goal precisely.
3. Scroll after interaction so dynamic content renders.
4. Extract the relevant data in Chinese.
"""
        agent = BrowserAgent(task=task_prompt, llm=wrapped_llm, browser=browser)
        history = await agent.run()
        final_result = str(history.history[-1].result)

        debug_dir = "./debug_screenshots"
        os.makedirs(debug_dir, exist_ok=True)
        best_screenshot_path = None
        max_size = 0
        for index, step in enumerate(history.history):
            candidates = []
            if hasattr(step, "state") and hasattr(step.state, "screenshot_path"):
                path = step.state.screenshot_path
                if path and os.path.exists(path):
                    candidates.append(path)
            if hasattr(step, "state") and hasattr(step.state, "screenshot"):
                screenshot = step.state.screenshot
                if screenshot:
                    tmp_name = f"{debug_dir}/tmp_{int(time.time())}_{index}.png"
                    with open(tmp_name, "wb") as handle:
                        handle.write(base64.b64decode(screenshot))
                    candidates.append(tmp_name)
            for candidate in candidates:
                try:
                    size = os.path.getsize(candidate)
                    if size > max_size:
                        max_size = size
                        final_name = f"{debug_dir}/browse_{int(time.time())}_step{index}.png"
                        if candidate != final_name:
                            shutil.copy(candidate, final_name)
                        best_screenshot_path = final_name
                except Exception:
                    continue

        if best_screenshot_path:
            final_result += f"\n\n[SNAPSHOT_PATH: {best_screenshot_path}]"
        return final_result
    except Exception as exc:
        return f"Error: {exc}"
    finally:
        if browser is not None:
            try:
                await browser.close()
            except Exception:
                pass
