from __future__ import annotations

from io import BytesIO
import logging
import re
import time
from typing import Any, TypedDict
from urllib.parse import unquote, urlparse

from bs4 import BeautifulSoup
import httpx
import pdfplumber

from core.tools import heuristic_dom_probe, tavily_extract_client, tavily_extract_credits, visual_browse

logger = logging.getLogger(__name__)


class FetchAttempt(TypedDict, total=False):
    provider: str
    status: str
    content_length: int
    elapsed_ms: float
    final_url: str
    error_class: str
    http_status: int
    content_type: str
    attempt_order: int
    page_type: str
    host: str
    salvaged_by_fallback: bool
    blocked_stage: str
    authority_preserved: bool


class FetchResult(TypedDict, total=False):
    provider: str
    status: str
    content: str
    content_length: int
    fetch_wall_seconds: float
    final_url: str
    error_class: str
    http_status: int
    content_type: str
    page_type: str
    authority_preserved: bool
    host: str
    blocked_stage: str
    salvaged_by_fallback: bool
    attempts: list[FetchAttempt]
    credits_est: float


def _path(url: str) -> str:
    try:
        return urlparse(url).path.lower()
    except Exception:
        return ""


def _looks_like_pdf_candidate(candidate: dict[str, Any]) -> bool:
    return _infer_page_type(candidate) in {"pdf", "filing"}


def _is_pdf_like_url(url: str) -> bool:
    return _path(url).endswith(".pdf")


def _title_hint(candidate: dict[str, Any]) -> str:
    title = str(candidate.get("title") or "").strip()
    if title:
        return title
    stem = unquote(_path(str(candidate.get("url") or ""))).rsplit("/", 1)[-1]
    stem = re.sub(r"\.pdf$", "", stem, flags=re.IGNORECASE)
    stem = re.sub(r"[_\-]+", " ", stem)
    stem = re.sub(r"\s+", " ", stem).strip()
    return stem[:120]


def should_force_access_backfill(
    candidate: dict[str, Any],
    *,
    quarantined_pdf_hosts: set[str] | None = None,
) -> bool:
    if not _looks_like_pdf_candidate(candidate):
        return False
    host = str(candidate.get("host") or _host(str(candidate.get("url") or ""))).lower()
    if host in PDF_FORCE_ACCESS_BACKFILL_HOSTS or host in PDF_OBJECT_STORAGE_HOSTS:
        return True
    return host in (quarantined_pdf_hosts or set())


def should_prefer_non_pdf_alternative(candidate: dict[str, Any]) -> bool:
    host = str(candidate.get("host") or _host(str(candidate.get("url") or ""))).lower()
    return _looks_like_pdf_candidate(candidate) and host in PDF_ALT_HOSTS


def should_quarantine_pdf_host(candidate: dict[str, Any], fetched: FetchResult) -> bool:
    if not _looks_like_pdf_candidate(candidate):
        return False
    host = str(candidate.get("host") or _host(str(candidate.get("url") or ""))).lower()
    if host not in PDF_ALT_HOSTS:
        return False
    attempts = list(fetched.get("attempts") or [])
    return any(str(attempt.get("error_class") or "") in PDF_HOST_QUARANTINE_ERRORS for attempt in attempts)


def build_access_backfill_query(candidate: dict[str, Any], task_desc: str) -> str:
    host = str(candidate.get("host") or _host(str(candidate.get("url") or ""))).lower()
    hint = _title_hint(candidate)
    if host == "rosap.ntl.bts.gov":
        return f'site:{host}/view/dot "{hint}" -filetype:pdf'
    if host in {"hub.hku.hk", "ir.nptu.edu.tw"}:
        return f'site:{host}/handle "{hint}" -filetype:pdf'
    if host in PDF_OBJECT_STORAGE_HOSTS:
        return f'"{hint}" {task_desc}'
    if not _looks_like_pdf_candidate(candidate):
        query_parts = [f"site:{host}"]
        if hint:
            query_parts.append(f'"{hint}"')
        query_parts.append(task_desc)
        query_parts.append("-filetype:pdf")
        return " ".join(part for part in query_parts if part)
    return f'site:{host} "{hint}" -filetype:pdf'


def rank_access_backfill_candidates(
    original_candidate: dict[str, Any],
    candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    host = str(original_candidate.get("host") or _host(str(original_candidate.get("url") or ""))).lower()
    original_url = str(original_candidate.get("url") or "")
    prefer_non_pdf = should_prefer_non_pdf_alternative(original_candidate)

    def sort_key(item: dict[str, Any]) -> tuple[int, int, int, float]:
        url = str(item.get("url") or "")
        path = _path(url)
        return (
            1 if prefer_non_pdf and not bool(item.get("is_pdf")) and not _is_pdf_like_url(url) else 0,
            1 if any(hint in path for hint in LANDING_PATH_HINTS) else 0,
            1 if not prefer_non_pdf and any(hint in path for hint in HTML_ALT_PATH_HINTS) else 0,
            1 if host in {"hub.hku.hk", "ir.nptu.edu.tw"} and "/handle/" in path else 0,
            float(item.get("fit_score") or 0.0),
        )

    filtered = [
        candidate
        for candidate in candidates
        if str(candidate.get("url") or "")
        and str(candidate.get("url") or "") != original_url
        and str(candidate.get("source_tier") or "") != "weak"
        and _host(str(candidate.get("url") or "")) == host
        and (not prefer_non_pdf or (not bool(candidate.get("is_pdf")) and not _is_pdf_like_url(str(candidate.get("url") or ""))))
    ]
    return sorted(filtered, key=sort_key, reverse=True)


def should_force_non_pdf_access_backfill(
    candidate: dict[str, Any],
    fetched: FetchResult,
    *,
    attempted_hosts: set[str] | None = None,
) -> bool:
    if _looks_like_pdf_candidate(candidate):
        return False
    if str(candidate.get("source_tier") or "") != "high_authority":
        return False
    host = str(candidate.get("host") or _host(str(candidate.get("url") or ""))).lower()
    if host in (attempted_hosts or set()):
        return False
    if host not in NON_PDF_FORCE_ACCESS_BACKFILL_HOSTS:
        return False
    attempts = list(fetched.get("attempts") or [])
    return any(str(attempt.get("error_class") or "") in BLOCKED_ERROR_CLASSES for attempt in attempts)


MAIN_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Referer": "https://www.google.com/",
}
JINA_BLOCK_PATTERNS = (
    "Access Denied",
    "Please wait while we verify",
    "Checking if the site connection is secure",
    "captcha",
)
BLOCKED_ERROR_CLASSES = {
    "http_401_403",
    "http_412",
    "http_429",
    "ssl_error",
    "redirect_loop",
    "js_only",
    "pdf_unreadable",
}
PDF_FORCE_ACCESS_BACKFILL_HOSTS = {
    "rosap.ntl.bts.gov",
    "hub.hku.hk",
}
PDF_ALT_AFTER_FAILURE_HOSTS = {
    "ed.arte.gov.tw",
    "www.hkbu.edu.hk",
    "ir.nptu.edu.tw",
    "esg.tsmc.com",
    "musiccollege.tnua.edu.tw",
}
PDF_OBJECT_STORAGE_HOSTS = {
    "s3.us-west-1.wasabisys.com",
}
PDF_HOST_QUARANTINE_ERRORS = {
    "http_401_403",
    "pdf_unreadable",
    "empty_content",
}
PDF_ALT_HOSTS = PDF_FORCE_ACCESS_BACKFILL_HOSTS | PDF_ALT_AFTER_FAILURE_HOSTS | PDF_OBJECT_STORAGE_HOSTS
LANDING_PATH_HINTS = (
    "/handle/",
    "/view/dot/",
    "/annual-report",
    "/report",
    "/repository",
    "/metadata",
)
HTML_ALT_PATH_HINTS = (
    "/insight",
    "/insights",
    "/article",
    "/articles",
    "/news",
    "/update",
    "/updates",
    "/publication",
    "/publications",
    "/practice",
    "/practice-area",
    "/practice-areas",
    "/resource",
    "/resources",
    "/blog",
)
NON_PDF_FORCE_ACCESS_BACKFILL_HOSTS = {
    "www.wshblaw.com",
}
EXTRACT_FIRST_NON_PDF_HOSTS = {
    "arxiv.org",
    "doi.org",
    "aaafoundation.org",
}


def _host(url: str) -> str:
    try:
        return urlparse(url).netloc.lower()
    except Exception:
        return ""


def _infer_page_type(candidate: dict[str, Any]) -> str:
    url = str(candidate.get("url") or "")
    host = _host(url)
    path = urlparse(url).path.lower() if url else ""
    if bool(candidate.get("is_pdf")) or path.endswith(".pdf"):
        return "pdf"
    if any(token in path for token in ("/investor", "/earnings", "/filing", "/annual-report", "/reports/")):
        return "filing"
    if bool(candidate.get("is_official")) or host.endswith(".gov") or host.endswith(".edu") or ".gov." in host:
        return "official_html"
    return "general_html"


def _classify_fetch_error(*, status_code: int | None = None, content_type: str = "", message: str = "") -> str:
    lowered_message = (message or "").lower()
    lowered_type = (content_type or "").lower()
    if status_code in (401, 403):
        return "http_401_403"
    if status_code == 412:
        return "http_412"
    if status_code == 429:
        return "http_429"
    if status_code == 404:
        return "http_404"
    if "ssl" in lowered_message or "certificate" in lowered_message:
        return "ssl_error"
    if "redirect" in lowered_message:
        return "redirect_loop"
    if "javascript" in lowered_message or "vlm_required" in lowered_message:
        return "js_only"
    if "pdf" in lowered_type:
        return "pdf_unreadable"
    return "empty_content"


def _build_attempt(
    *,
    provider: str,
    page_type: str,
    host: str,
    authority_preserved: bool,
    attempt_order: int,
    status: str,
    final_url: str,
    content: str = "",
    error_class: str = "",
    http_status: int = 0,
    content_type: str = "",
) -> FetchAttempt:
    blocked_stage = provider if error_class in BLOCKED_ERROR_CLASSES else ""
    return {
        "provider": provider,
        "status": status,
        "content_length": len(content or ""),
        "final_url": final_url,
        "error_class": error_class,
        "http_status": int(http_status or 0),
        "content_type": content_type or "",
        "attempt_order": attempt_order,
        "page_type": page_type,
        "host": host,
        "salvaged_by_fallback": False,
        "blocked_stage": blocked_stage,
        "authority_preserved": authority_preserved,
    }


def _html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for node in soup(["script", "style", "nav", "footer", "header"]):
        node.extract()
    paragraphs = soup.find_all(["p", "h1", "h2", "h3", "h4", "li"])
    content_lines = []
    for paragraph in paragraphs:
        text = paragraph.get_text(separator=" ", strip=True)
        if len(text) > 20:
            content_lines.append(text)
    return "\n\n".join(content_lines) or soup.get_text(separator="\n", strip=True)


async def _attempt_jina(url: str, *, page_type: str, host: str, attempt_order: int, authority_preserved: bool) -> tuple[FetchAttempt, str]:
    final_url = url
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            resp = await client.get(f"https://r.jina.ai/{url}", headers=MAIN_HEADERS)
        final_url = str(resp.url)
        content_type = resp.headers.get("content-type", "")
        if resp.status_code != 200:
            return (
                _build_attempt(
                    provider="jina",
                    page_type=page_type,
                    host=host,
                    authority_preserved=authority_preserved,
                    attempt_order=attempt_order,
                    status="failed",
                    final_url=final_url,
                    error_class=_classify_fetch_error(status_code=resp.status_code, content_type=content_type),
                    http_status=resp.status_code,
                    content_type=content_type,
                ),
                "",
            )
        text = resp.text
        if len(text) > 300 and not any(pattern.lower() in text.lower() for pattern in JINA_BLOCK_PATTERNS):
            return (
                _build_attempt(
                    provider="jina",
                    page_type=page_type,
                    host=host,
                    authority_preserved=authority_preserved,
                    attempt_order=attempt_order,
                    status="ok",
                    final_url=final_url,
                    content=text,
                    http_status=resp.status_code,
                    content_type=content_type,
                ),
                text,
            )
        return (
            _build_attempt(
                provider="jina",
                page_type=page_type,
                host=host,
                authority_preserved=authority_preserved,
                attempt_order=attempt_order,
                status="failed",
                final_url=final_url,
                error_class="empty_content",
                http_status=resp.status_code,
                content_type=content_type,
            ),
            "",
        )
    except Exception as exc:
        return (
            _build_attempt(
                provider="jina",
                page_type=page_type,
                host=host,
                authority_preserved=authority_preserved,
                attempt_order=attempt_order,
                status="failed",
                final_url=final_url,
                error_class=_classify_fetch_error(message=str(exc)),
            ),
            "",
        )


async def _attempt_direct_http(url: str, *, page_type: str, host: str, attempt_order: int, authority_preserved: bool) -> tuple[FetchAttempt, str]:
    final_url = url
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            resp = await client.get(url, headers=MAIN_HEADERS)
        final_url = str(resp.url)
        content_type = resp.headers.get("content-type", "")
        if resp.status_code in (401, 403, 404, 412, 429, 500, 502, 503):
            return (
                _build_attempt(
                    provider="direct_http",
                    page_type=page_type,
                    host=host,
                    authority_preserved=authority_preserved,
                    attempt_order=attempt_order,
                    status="failed",
                    final_url=final_url,
                    error_class=_classify_fetch_error(status_code=resp.status_code, content_type=content_type),
                    http_status=resp.status_code,
                    content_type=content_type,
                ),
                "",
            )
        resp.raise_for_status()
        if "pdf" in content_type.lower():
            return (
                _build_attempt(
                    provider="direct_http",
                    page_type=page_type,
                    host=host,
                    authority_preserved=authority_preserved,
                    attempt_order=attempt_order,
                    status="failed",
                    final_url=final_url,
                    error_class="pdf_unreadable",
                    http_status=resp.status_code,
                    content_type=content_type,
                ),
                "",
            )
        fallback_text = _html_to_text(resp.text)
        probe_result = heuristic_dom_probe(resp.text, fallback_text)
        if probe_result:
            return (
                _build_attempt(
                    provider="direct_http",
                    page_type="js_heavy",
                    host=host,
                    authority_preserved=authority_preserved,
                    attempt_order=attempt_order,
                    status="needs_visual",
                    final_url=final_url,
                    content=probe_result,
                    error_class="js_only",
                    http_status=resp.status_code,
                    content_type=content_type,
                ),
                probe_result,
            )
        if not fallback_text or len(fallback_text.strip()) <= 120:
            return (
                _build_attempt(
                    provider="direct_http",
                    page_type=page_type,
                    host=host,
                    authority_preserved=authority_preserved,
                    attempt_order=attempt_order,
                    status="failed",
                    final_url=final_url,
                    error_class="empty_content",
                    http_status=resp.status_code,
                    content_type=content_type,
                ),
                "",
            )
        return (
            _build_attempt(
                provider="direct_http",
                page_type=page_type,
                host=host,
                authority_preserved=authority_preserved,
                attempt_order=attempt_order,
                status="ok",
                final_url=final_url,
                content=fallback_text,
                http_status=resp.status_code,
                content_type=content_type,
            ),
            fallback_text,
        )
    except Exception as exc:
        return (
            _build_attempt(
                provider="direct_http",
                page_type=page_type,
                host=host,
                authority_preserved=authority_preserved,
                attempt_order=attempt_order,
                status="failed",
                final_url=final_url,
                error_class=_classify_fetch_error(message=str(exc)),
            ),
            "",
        )


async def _attempt_pdf_parser(url: str, *, page_type: str, host: str, attempt_order: int, authority_preserved: bool) -> tuple[FetchAttempt, str]:
    final_url = url
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            resp = await client.get(url, headers=MAIN_HEADERS)
        final_url = str(resp.url)
        content_type = resp.headers.get("content-type", "")
        if resp.status_code in (401, 403, 404, 412, 429, 500, 502, 503):
            return (
                _build_attempt(
                    provider="pdf_parser",
                    page_type=page_type,
                    host=host,
                    authority_preserved=authority_preserved,
                    attempt_order=attempt_order,
                    status="failed",
                    final_url=final_url,
                    error_class=_classify_fetch_error(status_code=resp.status_code, content_type=content_type),
                    http_status=resp.status_code,
                    content_type=content_type,
                ),
                "",
            )
        resp.raise_for_status()
        with pdfplumber.open(BytesIO(resp.content)) as pdf:
            texts = []
            for page in pdf.pages[:25]:
                try:
                    page_text = page.extract_text() or ""
                except Exception:
                    page_text = ""
                if page_text.strip():
                    texts.append(page_text.strip())
        combined = "\n\n".join(texts)
        if len(combined.strip()) <= 120:
            return (
                _build_attempt(
                    provider="pdf_parser",
                    page_type=page_type,
                    host=host,
                    authority_preserved=authority_preserved,
                    attempt_order=attempt_order,
                    status="failed",
                    final_url=final_url,
                    error_class="pdf_unreadable",
                    http_status=resp.status_code,
                    content_type=content_type,
                ),
                "",
            )
        return (
            _build_attempt(
                provider="pdf_parser",
                page_type=page_type,
                host=host,
                authority_preserved=authority_preserved,
                attempt_order=attempt_order,
                status="ok",
                final_url=final_url,
                content=combined,
                http_status=resp.status_code,
                content_type=content_type,
            ),
            combined,
        )
    except Exception as exc:
        return (
            _build_attempt(
                provider="pdf_parser",
                page_type=page_type,
                host=host,
                authority_preserved=authority_preserved,
                attempt_order=attempt_order,
                status="failed",
                final_url=final_url,
                error_class=_classify_fetch_error(message=str(exc), content_type="application/pdf"),
            ),
            "",
        )


async def _attempt_tavily_extract(
    url: str,
    *,
    query: str,
    page_type: str,
    host: str,
    attempt_order: int,
    authority_preserved: bool,
) -> tuple[FetchAttempt, str, float]:
    if not tavily_extract_client.is_configured():
        return (
            _build_attempt(
                provider="tavily_extract",
                page_type=page_type,
                host=host,
                authority_preserved=authority_preserved,
                attempt_order=attempt_order,
                status="failed",
                final_url=url,
                error_class="empty_content",
            ),
            "",
            0.0,
        )
    try:
        response = await tavily_extract_client.aextract(
            [url],
            query=query,
            chunks_per_source=3,
            extract_depth="basic",
        )
        result = (response.get("results") or [{}])[0] if response.get("results") else {}
        raw_content = str(result.get("raw_content") or result.get("content") or "")
        final_url = str(result.get("url") or url)
        if len(raw_content.strip()) <= 120:
            return (
                _build_attempt(
                    provider="tavily_extract",
                    page_type=page_type,
                    host=host,
                    authority_preserved=authority_preserved,
                    attempt_order=attempt_order,
                    status="failed",
                    final_url=final_url,
                    error_class="empty_content",
                ),
                "",
                0.0,
            )
        return (
            _build_attempt(
                provider="tavily_extract",
                page_type=page_type,
                host=host,
                authority_preserved=authority_preserved,
                attempt_order=attempt_order,
                status="ok",
                final_url=final_url,
                content=raw_content,
                content_type="text/plain",
            ),
            raw_content,
            float(tavily_extract_credits(1, "basic")),
        )
    except Exception as exc:
        return (
            _build_attempt(
                provider="tavily_extract",
                page_type=page_type,
                host=host,
                authority_preserved=authority_preserved,
                attempt_order=attempt_order,
                status="failed",
                final_url=url,
                error_class=_classify_fetch_error(message=str(exc)),
            ),
            "",
            0.0,
        )


async def _attempt_visual(
    url: str,
    *,
    goal: str,
    page_type: str,
    host: str,
    attempt_order: int,
    authority_preserved: bool,
) -> tuple[FetchAttempt, str]:
    try:
        content = await visual_browse(url, goal)
        if not content or content.startswith("Error:") or content.startswith("[VISUAL_BROWSE_UNAVAILABLE]"):
            return (
                _build_attempt(
                    provider="visual_browse",
                    page_type="js_heavy",
                    host=host,
                    authority_preserved=authority_preserved,
                    attempt_order=attempt_order,
                    status="failed",
                    final_url=url,
                    error_class="js_only",
                ),
                "",
            )
        return (
            _build_attempt(
                provider="visual_browse",
                page_type="js_heavy",
                host=host,
                authority_preserved=authority_preserved,
                attempt_order=attempt_order,
                status="ok",
                final_url=url,
                content=content,
                content_type="text/plain",
            ),
            content,
        )
    except Exception:
        return (
            _build_attempt(
                provider="visual_browse",
                page_type="js_heavy",
                host=host,
                authority_preserved=authority_preserved,
                attempt_order=attempt_order,
                status="failed",
                final_url=url,
                error_class="js_only",
            ),
            "",
        )


def _main_provider_order(page_type: str, host: str) -> list[str]:
    if page_type in {"pdf", "filing"}:
        return ["pdf_parser", "tavily_extract"]
    if host in EXTRACT_FIRST_NON_PDF_HOSTS:
        return ["tavily_extract", "direct_http"]
    if page_type == "official_html":
        return ["direct_http", "tavily_extract", "jina"]
    return ["jina", "direct_http"]


def _should_allow_visual(candidate: dict[str, Any], attempts: list[FetchAttempt], allow_visual: bool) -> bool:
    if not allow_visual:
        return False
    if _looks_like_pdf_candidate(candidate):
        return False
    if str(candidate.get("source_tier") or "") != "high_authority":
        return False
    return any(
        attempt.get("error_class") in {"js_only", "http_401_403", "http_412", "http_429"}
        for attempt in attempts
    )


async def fetch_source_candidate(
    candidate: dict[str, Any],
    *,
    allow_visual: bool,
    goal: str,
) -> FetchResult:
    url = str(candidate.get("url") or "")
    host = str(candidate.get("host") or _host(url))
    page_type = _infer_page_type(candidate)
    authority_preserved = bool(candidate.get("source_tier") == "high_authority")
    attempts: list[FetchAttempt] = []
    final_content = ""
    final_attempt: FetchAttempt | None = None
    credits_est = 0.0

    for attempt_order, provider in enumerate(_main_provider_order(page_type, host), start=1):
        started = time.perf_counter()
        if provider == "pdf_parser":
            attempt, content = await _attempt_pdf_parser(
                url,
                page_type=page_type,
                host=host,
                attempt_order=attempt_order,
                authority_preserved=authority_preserved,
            )
        elif provider == "tavily_extract":
            attempt, content, credits = await _attempt_tavily_extract(
                url,
                query=goal,
                page_type=page_type,
                host=host,
                attempt_order=attempt_order,
                authority_preserved=authority_preserved,
            )
            credits_est += credits
        elif provider == "direct_http":
            attempt, content = await _attempt_direct_http(
                url,
                page_type=page_type,
                host=host,
                attempt_order=attempt_order,
                authority_preserved=authority_preserved,
            )
        else:
            attempt, content = await _attempt_jina(
                url,
                page_type=page_type,
                host=host,
                attempt_order=attempt_order,
                authority_preserved=authority_preserved,
            )
        attempt["elapsed_ms"] = round((time.perf_counter() - started) * 1000.0, 3)
        attempts.append(attempt)
        if attempt["status"] == "ok":
            final_attempt = attempt
            final_content = content
            break

    if final_attempt is None and _should_allow_visual(candidate, attempts, allow_visual):
        started = time.perf_counter()
        attempt, content = await _attempt_visual(
            url,
            goal=goal,
            page_type=page_type,
            host=host,
            attempt_order=len(attempts) + 1,
            authority_preserved=authority_preserved,
        )
        attempt["elapsed_ms"] = round((time.perf_counter() - started) * 1000.0, 3)
        attempts.append(attempt)
        if attempt["status"] == "ok":
            final_attempt = attempt
            final_content = content

    salvaged = bool(final_attempt and final_attempt.get("attempt_order", 1) > 1)
    if salvaged:
        for attempt in attempts[:-1]:
            if attempt.get("status") != "ok":
                attempt["salvaged_by_fallback"] = True
        if final_attempt:
            final_attempt["salvaged_by_fallback"] = True

    if final_attempt is None:
        final_attempt = attempts[-1] if attempts else _build_attempt(
            provider="pipeline",
            page_type=page_type,
            host=host,
            authority_preserved=authority_preserved,
            attempt_order=1,
            status="failed",
            final_url=url,
            error_class="empty_content",
        )

    blocked_stage = next((attempt.get("provider", "") for attempt in attempts if attempt.get("blocked_stage")), "")
    result: FetchResult = {
        "provider": str(final_attempt.get("provider") or ""),
        "status": str(final_attempt.get("status") or "failed"),
        "content": final_content if final_attempt.get("status") == "ok" else str(final_attempt.get("content") or ""),
        "content_length": int(len(final_content or "")),
        "fetch_wall_seconds": round(sum(float(attempt.get("elapsed_ms") or 0.0) for attempt in attempts) / 1000.0, 4),
        "final_url": str(final_attempt.get("final_url") or url),
        "error_class": str(final_attempt.get("error_class") or ""),
        "http_status": int(final_attempt.get("http_status") or 0),
        "content_type": str(final_attempt.get("content_type") or ""),
        "page_type": str(final_attempt.get("page_type") or page_type),
        "authority_preserved": bool(final_attempt.get("authority_preserved")),
        "host": host,
        "blocked_stage": blocked_stage,
        "salvaged_by_fallback": salvaged,
        "attempts": attempts,
        "credits_est": credits_est,
    }
    return result
