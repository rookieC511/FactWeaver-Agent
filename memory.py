import time
import hashlib
import re
from typing import List, Set
from langchain_core.documents import Document
from langchain_core.messages import HumanMessage
from models import llm_extractor

class KnowledgeManager:
    def __init__(self):
        print(f"⚙️ [System] 初始化知识库 (长上下文全文提纯模式)")
        self.seen_urls: Set[str] = set()
        self.fact_blocks: List[Document] = []

    def is_duplicate(self, url: str) -> bool:
        return url in self.seen_urls
        
    def clear(self):
        """清除当前任务的积累记忆，在新的 Benchmark Query 开始时调用"""
        self.seen_urls.clear()
        self.fact_blocks.clear()
        
    def add_raw_document(self, content: str, source_url: str, title: str):
        """直接保存短小文本 (如搜索结果的 snippet)，不经过大模型提纯"""
        if len(content) < 10: return 0
        self.seen_urls.add(source_url)
        doc = Document(
            page_content=content,
            metadata={
                "source_url": source_url, 
                "title": title, 
                "timestamp": time.time(),
                "citation_hash": hashlib.md5(source_url.encode()).hexdigest()[:6]
            }
        )
        self.fact_blocks.append(doc)
        return 1

    def _split_into_chunks(self, text: str, chunk_size: int = 6000) -> List[str]:
        """
        将长文本按段落边界切分为 ~chunk_size 字符的块。
        优先在段落分隔符 (\\n\\n) 处切割，避免把一句话切成两半。
        """
        if len(text) <= chunk_size:
            return [text]
        
        chunks = []
        start = 0
        while start < len(text):
            end = start + chunk_size
            if end >= len(text):
                chunks.append(text[start:])
                break
            
            # 在 [end-500, end] 范围内寻找最近的段落分隔符
            split_pos = text.rfind('\n\n', start + chunk_size - 500, end)
            if split_pos == -1:
                # 没有段落分隔符，退而求其次找句号
                split_pos = text.rfind('。', start + chunk_size - 500, end)
            if split_pos == -1:
                # 都没有，硬切
                split_pos = end
            else:
                split_pos += 1  # 包含分隔符
            
            chunks.append(text[start:split_pos])
            start = split_pos
        
        return [c for c in chunks if len(c.strip()) > 0]

    async def aadd_document(self, content: str, source_url: str, title: str, task_desc: str):
        """
        V2.0 滚动快照压缩 (Rolling Snapshot Compression)
        ================================================
        将长文本分块 (~6K 字符/块)，逐块喂给 Llama-3.1 8B，
        每次携带前文的压缩快照，强行将模型锁定在最优 8K token 窗口内。
        
        核心优势 vs V1.0:
        - 消除"中间遗忘 (Lost in the Middle)"效应
        - 每次 LLM 调用显存稳定 (~4GB vs V1.0 的 6.6GB)
        - 覆盖全文，不再硬截断
        """
        if len(content) < 50: return 0
        self.seen_urls.add(source_url)
        
        # 1. 预清洗：压缩无意义空白符
        content = re.sub(r'\n{3,}', '\n\n', content)
        content = re.sub(r' {4,}', ' ', content)
        
        # 如果文本本身就不长，直接存，省力
        if len(content) < 1500:
            return self.add_raw_document(content, source_url, title)
        
        # 2. 分块 (每块 ~6000 字符，按段落边界切分)
        CHUNK_SIZE = 6000
        chunks = self._split_into_chunks(content, chunk_size=CHUNK_SIZE)
        
        print(f"    🧠 [V2.0 Rolling Extractor] 原文 {len(content)} 字符 → 切分为 {len(chunks)} 块 (每块 ~{CHUNK_SIZE} 字符)")
        
        # 3. 滚动提取核心循环
        memory_snapshot = ""  # 全局记忆快照，初始为空
        
        for i, chunk in enumerate(chunks):
            print(f"    🔄 [Rolling] 第 {i+1}/{len(chunks)} 块 ({len(chunk)} 字符)...")
            
            # 组装 Prompt — 携带前文快照 + 当前文本块
            if memory_snapshot:
                snapshot_section = f"""【全局记忆快照 (前文已提取的浓缩事实)】:
{memory_snapshot}
"""
            else:
                snapshot_section = "【全局记忆快照】: (这是第一个文本块，尚无前文快照)\n"
            
            prompt = f"""你是一个冷酷的情报提纯机。你的唯一任务是从超长文本中提取与研究任务相关的核心事实。

当前研究任务: {task_desc}
原文标题: {title}

{snapshot_section}
【当前阅读的文本块 (第 {i+1}/{len(chunks)} 块)】:
{chunk}

=== 任务指令 ===
1. 仔细阅读【当前文本块】，提取出与研究任务直接相关的新事实、数据和关键结论。
2. 将新提取的事实与【全局记忆快照】中已有的事实进行融合压缩。
3. 输出一份更新后的浓缩快照，要求：
   - 字数严格控制在 1500 字以内
   - 保留所有具体数字、百分比、年份、专有名词
   - 绝对不要遗忘之前快照中的关键事实
   - 如果新文本块中没有与任务相关的信息，原样返回旧快照
   - 丢弃所有与研究任务无关的废话
   - 以纯文本 Markdown 输出，不要加寒暄语
"""
            
            try:
                resp = await llm_extractor.ainvoke([HumanMessage(content=prompt)])
                memory_snapshot = resp.content.strip()
                print(f"    📸 [Snapshot] 更新完成 ({len(memory_snapshot)} 字符)")
            except Exception as e:
                print(f"    ⚠️ [Rolling] 第 {i+1} 块提取失败: {e}，跳过此块")
                # 失败则跳过当前块，保留旧快照继续
                continue
        
        # 4. 最终快照就是这篇文章的浓缩事实
        if not memory_snapshot:
            print(f"    ❌ [Fact Extractor] 所有块均提取失败")
            return self.add_raw_document(f"(提取失败截取)\n{content[:2000]}", source_url, title)
        
        doc = Document(
            page_content=memory_snapshot,
            metadata={
                "source_url": source_url, 
                "title": title, 
                "timestamp": time.time(),
                "citation_hash": hashlib.md5(source_url.encode()).hexdigest()[:6],
                "extraction_version": "v2.0_rolling_snapshot",
                "num_chunks": len(chunks),
            }
        )
        self.fact_blocks.append(doc)
        print(f"    ✅ [V2.0] 滚动提取完成！最终快照 {len(memory_snapshot)} 字符 (经 {len(chunks)} 轮压缩)")
        return 1

    def retrieve(self, query: str = None, k: int = 100) -> List[Document]:
        """因为大模型长上下文容量大，直接把所有提纯过的 Fact Block 都返回给 Writer"""
        return self.fact_blocks

# 全局单例
km = KnowledgeManager()