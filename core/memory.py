import asyncio
import contextvars
import hashlib
import random
import re
import time
from typing import Any, List, Set

from langchain_core.documents import Document
from langchain_core.messages import HumanMessage

from core.models import llm_extractor


class KnowledgeManager:
    def __init__(self, session_id: str = "default"):
        self.session_id = session_id
        print(f"[System] Initialize KnowledgeManager session={session_id}")
        self.seen_urls: Set[str] = set()
        self.fact_blocks: List[Document] = []
        self.semaphore = asyncio.Semaphore(4)
        self._write_lock = asyncio.Lock()

    def is_duplicate(self, url: str) -> bool:
        return url in self.seen_urls

    async def aclear(self):
        async with self._write_lock:
            self.seen_urls.clear()
            self.fact_blocks.clear()

    def clear(self):
        self.seen_urls.clear()
        self.fact_blocks.clear()

    def snapshot(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "seen_urls": sorted(self.seen_urls),
            "fact_blocks": [
                {
                    "page_content": doc.page_content,
                    "metadata": dict(doc.metadata),
                }
                for doc in self.fact_blocks
            ],
        }

    def restore(self, snapshot: dict[str, Any] | None) -> None:
        data = dict(snapshot or {})
        self.clear()
        self.session_id = str(data.get("session_id") or self.session_id)
        self.seen_urls = set(data.get("seen_urls") or [])
        self.fact_blocks = [
            Document(
                page_content=str(item.get("page_content") or ""),
                metadata=dict(item.get("metadata") or {}),
            )
            for item in data.get("fact_blocks") or []
        ]

    def add_raw_document(
        self,
        content: str,
        source_url: str,
        title: str,
        section_id: str = "global",
        extra_metadata: dict[str, Any] | None = None,
    ):
        if len(content or "") < 10:
            return 0
        self.seen_urls.add(source_url)
        metadata = {
            "source_url": source_url,
            "url": source_url,
            "title": title,
            "timestamp": time.time(),
            "citation_hash": hashlib.md5(source_url.encode()).hexdigest()[:6],
            "section_id": section_id,
        }
        metadata.update(dict(extra_metadata or {}))
        doc = Document(
            page_content=content,
            metadata=metadata,
        )
        self.fact_blocks.append(doc)
        return 1

    def add_document(
        self,
        content: str,
        source_url: str,
        title: str,
        section_id: str = "global",
        extra_metadata: dict[str, Any] | None = None,
    ):
        return self.add_raw_document(content, source_url, title, section_id=section_id, extra_metadata=extra_metadata)

    def add_compact_document(
        self,
        content: str,
        source_url: str,
        title: str,
        section_id: str = "global",
        max_chars: int = 4000,
        extra_metadata: dict[str, Any] | None = None,
    ):
        compact = re.sub(r"\n{3,}", "\n\n", content or "").strip()
        compact = re.sub(r" {4,}", " ", compact)
        if len(compact) > max_chars:
            compact = compact[:max_chars]
        return self.add_raw_document(
            compact,
            source_url,
            title,
            section_id=section_id,
            extra_metadata=extra_metadata,
        )

    def _split_extracted_text(self, text: str, max_chars: int = 1200) -> List[str]:
        normalized = re.sub(r"\n{3,}", "\n\n", (text or "").strip())
        if len(normalized) <= max_chars:
            return [normalized] if normalized else []

        paragraphs = [segment.strip() for segment in normalized.split("\n\n") if segment.strip()]
        chunks: list[str] = []
        current = ""
        for paragraph in paragraphs:
            candidate = f"{current}\n\n{paragraph}".strip() if current else paragraph
            if len(candidate) <= max_chars:
                current = candidate
                continue
            if current:
                chunks.append(current)
            if len(paragraph) <= max_chars:
                current = paragraph
                continue
            start = 0
            while start < len(paragraph):
                chunks.append(paragraph[start : start + max_chars])
                start += max_chars
            current = ""
        if current:
            chunks.append(current)
        return [chunk for chunk in chunks if chunk.strip()]

    def add_extracted_chunks(
        self,
        chunks: List[str] | str,
        source_url: str,
        title: str,
        section_id: str = "global",
        provider: str = "tavily_extract",
        extra_metadata: dict[str, Any] | None = None,
    ) -> int:
        if isinstance(chunks, str):
            chunk_list = self._split_extracted_text(chunks)
        else:
            chunk_list = []
            for chunk in chunks:
                chunk_list.extend(self._split_extracted_text(chunk))

        if not chunk_list:
            return 0

        self.seen_urls.add(source_url)
        inserted = 0
        for index, chunk in enumerate(chunk_list, start=1):
            doc = Document(
                page_content=chunk,
                metadata={
                    "source_url": source_url,
                    "url": source_url,
                    "title": title,
                    "timestamp": time.time(),
                    "citation_hash": hashlib.md5(source_url.encode()).hexdigest()[:6],
                    "section_id": section_id,
                    "extraction_version": provider,
                    "chunk_index": index,
                    "num_chunks": len(chunk_list),
                    **dict(extra_metadata or {}),
                },
            )
            self.fact_blocks.append(doc)
            inserted += 1
        return inserted

    def _split_into_chunks(
        self,
        text: str,
        target_size: int = 10000,
        max_size: int = 12000,
        chunk_overlap: int = 500,
    ) -> List[str]:
        if len(text) <= target_size:
            return [text]

        separators = [
            (r"\n\n", 2, 100),
            (r"\n", 1, 95),
            (r"[。！？；]", 1, 85),
            (r"[.!?;]\s", 2, 75),
            (r"\s", 1, 60),
        ]
        chunks: list[str] = []
        start = 0

        while start < len(text):
            remainder_len = len(text) - start
            if remainder_len <= max_size:
                chunks.append(text[start:])
                break

            if remainder_len <= 2 * max_size:
                current_target = remainder_len // 2
                current_search_start = start + current_target - 1500
                current_search_end = start + current_target + 1500
            else:
                current_target = target_size
                current_search_start = start + target_size - 1500
                current_search_end = start + max_size

            current_search_start = max(start, current_search_start)
            current_search_end = min(len(text), current_search_end)
            window = text[current_search_start:current_search_end]

            best_pos = -1
            best_weight = -1000
            ideal_offset_in_window = min(1500, len(window))

            for pattern, offset, weight in separators:
                matches = list(re.finditer(pattern, window))
                for match in matches:
                    pos = match.start() + offset
                    dist_penalty = abs(pos - ideal_offset_in_window) // 20
                    score = weight - dist_penalty
                    if score > best_weight:
                        best_weight = score
                        best_pos = pos

            split_pos = (
                start + current_target
                if best_pos == -1
                else current_search_start + best_pos
            )
            split_pos = max(start + 1, min(split_pos, start + max_size))
            chunks.append(text[start:split_pos])
            start = max(start + 1, split_pos - chunk_overlap)

        return [chunk for chunk in chunks if chunk.strip()]

    async def aadd_document(
        self,
        content: str,
        source_url: str,
        title: str,
        task_desc: str,
        section_id: str = "global",
    ):
        if len(content or "") < 50:
            return 0

        self.seen_urls.add(source_url)
        content = re.sub(r"\n{3,}", "\n\n", content)
        content = re.sub(r" {4,}", " ", content)

        if len(content) < 1500:
            return self.add_raw_document(
                content,
                source_url,
                title,
                section_id=section_id,
            )

        chunks = self._split_into_chunks(content)
        print(
            f"[Memory] source={source_url} split into {len(chunks)} chunks "
            f"{[len(chunk) for chunk in chunks]}"
        )

        async def extract_chunk(index: int, chunk: str) -> str:
            async with self.semaphore:
                prompt = f"""你是一个冷静的情报提取器。请从下面文本块中提取与研究任务相关的事实、数字和逻辑关系。

研究任务: {task_desc}
原始标题: {title}
来源 URL: {source_url}
当前块: {index + 1}/{len(chunks)}

{chunk}

要求:
- 每条事实以 [Source: {source_url} | Chunk_ID: {index + 1}] 开头
- 保留具体数字、日期、专有名词
- 使用简洁 Markdown 列表输出
"""
                for attempt in range(3):
                    try:
                        resp = await llm_extractor.ainvoke([HumanMessage(content=prompt)])
                        return resp.content.strip()
                    except Exception as exc:
                        if attempt == 2:
                            print(f"[Memory] chunk {index + 1} extraction failed: {exc}")
                            return f"<FETCH_FAILED>\nURL: {source_url}\nChunk_ID: {index + 1}"
                        sleep_time = (2**attempt) + random.uniform(0, 1)
                        await asyncio.sleep(sleep_time)
                return ""

        map_results = await asyncio.gather(
            *[extract_chunk(index, chunk) for index, chunk in enumerate(chunks)]
        )
        all_facts = "\n\n".join(result for result in map_results if result)
        if not all_facts:
            return self.add_raw_document(
                f"(map extraction failed)\n{content[:2000]}",
                source_url,
                title,
                section_id=section_id,
            )

        reduce_prompt = f"""你是高级情报编辑。请合并、去重并压缩下面的事实列表。

研究任务: {task_desc}
待合并事实:
{all_facts}

规则:
- 保留 [Source: ... | Chunk_ID: ...] 血缘标记
- 如果发现严重冲突，在输出开头标记 [CONFLICT_DETECTED]
- 使用简洁 Markdown 列表输出
"""
        try:
            reduce_resp = await llm_extractor.ainvoke([HumanMessage(content=reduce_prompt)])
            final_facts = reduce_resp.content.strip()
            doc = Document(
                page_content=final_facts,
                metadata={
                    "source_url": source_url,
                    "url": source_url,
                    "title": title,
                    "timestamp": time.time(),
                    "citation_hash": hashlib.md5(source_url.encode()).hexdigest()[:6],
                    "extraction_version": "v2.1_map_reduce",
                    "num_chunks": len(chunks),
                    "section_id": section_id,
                },
            )
            self.fact_blocks.append(doc)
            return final_facts
        except Exception as exc:
            print(f"[Memory] reduce failed: {exc}")
            return self.add_raw_document(
                f"(reduce failed)\n{all_facts[:3000]}",
                source_url,
                title,
                section_id=section_id,
            )

    def retrieve(
        self,
        query: str = None,
        k: int = 100,
        section_id: str = None,
    ) -> List[Document]:
        docs = self.fact_blocks
        if section_id:
            docs = [
                doc
                for doc in docs
                if doc.metadata.get("section_id") in (section_id, "global")
            ]
        docs = sorted(
            docs,
            key=lambda doc: (
                float(doc.metadata.get("authority_score") or 0.0),
                1 if str(doc.metadata.get("source_tier") or "") == "high_authority" else 0,
                float(doc.metadata.get("timestamp") or 0.0),
            ),
            reverse=True,
        )
        return docs[:k]


km = KnowledgeManager()

_session_registry: dict[str, KnowledgeManager] = {}
_active_session_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "factweaver_session_id",
    default=None,
)


def get_session_km(session_id: str) -> KnowledgeManager:
    if session_id not in _session_registry:
        _session_registry[session_id] = KnowledgeManager(session_id=session_id)
    return _session_registry[session_id]


def cleanup_session_km(session_id: str):
    if session_id in _session_registry:
        del _session_registry[session_id]


def restore_session_km(session_id: str, snapshot: dict[str, Any] | None) -> KnowledgeManager:
    km = get_session_km(session_id)
    km.restore(snapshot)
    return km


def activate_session(session_id: str):
    return _active_session_id.set(session_id)


def reset_active_session(token) -> None:
    _active_session_id.reset(token)


def get_current_session_id() -> str | None:
    return _active_session_id.get()


def get_current_km() -> KnowledgeManager:
    session_id = _active_session_id.get()
    if not session_id:
        return km
    return get_session_km(session_id)
