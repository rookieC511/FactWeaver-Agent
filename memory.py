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

    async def aadd_document(self, content: str, source_url: str, title: str, task_desc: str):
        """使用大模型对数万字的网页原文进行摘要提纯，抽取核心事实和数据"""
        if len(content) < 50: return 0
        self.seen_urls.add(source_url)
        
        # 成本优化核心 (Cost Control):
        # 1. 压缩无意义的空白符、换行符（长尾抓取死角清理）
        content = re.sub(r'\n{3,}', '\n\n', content)
        content = re.sub(r' {4,}', ' ', content)
        
        # 2. 严格的上下文窗口控制 (截断至前 25000 字符)
        # 即使是非常长的 PDF (200+页)，前 25000 字通常包含了“执行摘要 (Executive Summary)、目录和第一/二章”，
        # 足够提取绝大多数战略性、方向性事实与核心数据。这直接将 API 消耗砍掉约 60% 以上。
        MAX_CHARS = 25000
        truncated_content = content[:MAX_CHARS]
        
        # 如果文本本身就不是很长，直接存，省力
        if len(truncated_content) < 1500:
            return self.add_raw_document(truncated_content, source_url, title)
            
        print(f"    🧠 [Fact Extractor] 正在由 LLM 浓缩提取全文事实 (原文 {len(content)} 字符 -> 截断清理至 {len(truncated_content)} 字符，成本砍半)...")
        # Prompt limits the raw text length vaguely, assuming DeepSeek handles up to 64k robustly
        prompt = f"""
        你是一个专业的情报分析提取员。
        这是一篇来源于互联网的原始长文本 (受限于成本与长度，文本可能在末尾被截断)。
        当前的研究任务是: {task_desc}
        
        请你仔细阅读原文内容，提取出与当前任务**直接相关的所有事实、数据和逻辑关联**，浓缩成高信息密度的简讯（Fact Block）。
        
        要求:
        1. 必须客观陈述，不能加入你的主观臆断或过度发散。
        2. 保留具体的数字、年份、专有名词。
        3. 全程使用中文回答。
        4. 以纯文本 Markdown 输出，不要加任何多余的寒暄语。
        
        原文标题: {title}
        ===================原文内容开始===================
        {truncated_content}
        ===================原文内容结束===================
        """
        
        try:
            resp = await llm_extractor.ainvoke([HumanMessage(content=prompt)])
            condensed_fact = resp.content.strip()
            
            # 记录到内存储备
            doc = Document(
                page_content=condensed_fact,
                metadata={
                    "source_url": source_url, 
                    "title": title, 
                    "timestamp": time.time(),
                    "citation_hash": hashlib.md5(source_url.encode()).hexdigest()[:6]
                }
            )
            self.fact_blocks.append(doc)
            print(f"    ✅ [Fact Extractor] 成功浓缩事实 ({len(condensed_fact)} 字符)")
            return 1
        except Exception as e:
            print(f"    ❌ [Fact Extractor] 提取失败: {e}")
            # 作为兜底，如果 LLM 摘要失败，我们至少保存前面的一部分硬截断作为 Fact
            return self.add_raw_document(f"(摘要失败截取)\n{content[:2000]}", source_url, title)

    def retrieve(self, query: str = None, k: int = 100) -> List[Document]:
        """因为大模型长上下文容量大，直接把所有提纯过的 Fact Block 都返回给 Writer"""
        return self.fact_blocks

# 全局单例
km = KnowledgeManager()