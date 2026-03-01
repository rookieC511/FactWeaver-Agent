import asyncio
import time
import hashlib
import re
from typing import List, Set
from langchain_core.documents import Document
from langchain_core.messages import HumanMessage
from models import llm_extractor

class KnowledgeManager:
    def __init__(self):
        print(f"⚙️ [System] 初始化知识库 (V2.1 Map-Reduce 模式)")
        self.seen_urls: Set[str] = set()
        self.fact_blocks: List[Document] = []
        # V2.2.4 并发优化：将并发提升至 4，配合重试机制榨取本地显卡性能
        self.semaphore = asyncio.Semaphore(4)

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

    def _split_into_chunks(self, text: str, target_size: int = 10000, max_size: int = 12000) -> List[str]:
        """
        智能分块算法 (V2.2 鲁棒性增强)
        =================================
        1. 采用多级分隔符权重，优先在语义完整处切割。
        2. 双界限机制：
           - target_size (Soft Limit): 目标大小
           - max_size (Hard Limit): 强制上限
        3. 权重等级:
           - Level 1 (Weight 100): \n\n (段落)
           - Level 2 (Weight 80): \n (换行)
           - Level 3 (Weight 60): 。！？； (中文字句)
           - Level 4 (Weight 40): . ! ? (英文字句)
           - Level 5 (Weight 20): " " (空格)
        """
        if len(text) <= target_size:
            return [text]
        
        chunks = []
        start = 0
        
        # 权重表：(模式, 偏移修正, 权重)
        separators = [
            (r'\n\n', 2, 100),
            (r'\n', 1, 95),
            (r'[。！？；]', 1, 85),
            (r'[.!?;]\s', 2, 75),
            (r'\s', 1, 60)
        ]
        
        while start < len(text):
            remainder_len = len(text) - start
            
            # 如果剩余部分已经可以塞进一块（加上少许超限容忍），就此结束
            if remainder_len <= max_size:
                chunks.append(text[start:])
                break
            
            # 核心改进：均衡负载 (Balanced Slicing)
            # 如果剩余部分在 (max_size, 2*max_size] 之间，我们将其平分为两块
            if remainder_len <= 2 * max_size:
                current_target = remainder_len // 2
                # 动态调整搜索窗口
                current_search_start = start + current_target - 1500
                current_search_end = start + current_target + 1500
            else:
                current_target = target_size
                current_search_start = start + target_size - 1500
                current_search_end = start + max_size
            
            window = text[current_search_start : current_search_end]
            
            best_pos = -1
            best_weight = -1000 
            
            for pattern, offset, weight in separators:
                matches = list(re.finditer(pattern, window))
                if not matches:
                    continue
                
                for m in matches:
                    pos = m.start() + offset
                    # 惩罚项相对于当前动态 target 的中心点
                    dist_penalty = abs(pos - (current_target - (current_target - 1500 if current_target == target_size else current_target - 1500))) 
                    # 简化逻辑：直接计算在 window 里的相对位置与理想偏移的差距
                    ideal_offset_in_window = 1500
                    dist_penalty = abs(pos - ideal_offset_in_window) // 20
                    current_score = weight - dist_penalty
                    
                    if current_score > best_weight:
                        best_weight = current_score
                        best_pos = pos
            
            if best_pos == -1:
                split_pos = start + current_target
            else:
                split_pos = current_search_start + best_pos
            
            split_pos = max(start + 1, min(split_pos, start + max_size))
            chunks.append(text[start:split_pos])
            start = split_pos
        
        return [c for c in chunks if len(c.strip()) > 0]

    async def aadd_document(self, content: str, source_url: str, title: str, task_desc: str):
        """
        V2.1 Map-Reduce 并行提纯
        ==========================
        1. Map Phase: 全文分块 (~6K 字符/块)，使用 asyncio.gather 并发提纯每一个块。
        2. Reduce Phase: 将所有块的提纯结果合在一起，做一次最终的合并、去重、浓缩。
        
        优点: 极大地降低了长文本的处理延迟 (Latnecy)，充分利用本地多显卡或并发推理能力。
        缺点: 失去了滚动快照的上下文引导，对分块交界处的逻辑理解可能略弱。
        """
        if len(content) < 50: return 0
        self.seen_urls.add(source_url)
        
        # 1. 预清洗
        content = re.sub(r'\n{3,}', '\n\n', content)
        content = re.sub(r' {4,}', ' ', content)
        
        if len(content) < 1500:
            return self.add_raw_document(content, source_url, title)
            
        CHUNK_SIZE = 10000
        MAX_CHUNK_SIZE = 12000
        chunks = self._split_into_chunks(content, target_size=CHUNK_SIZE, max_size=MAX_CHUNK_SIZE)
        chunk_lens = [len(c) for c in chunks]
        print(f"    🧠 [V2.2.2 Balanced-Split] 原文 {len(content)} 字符 → 切分为 {len(chunks)} 块: {chunk_lens}")
        
        # 3. Map Phase: 串行提取 (受 Semaphore(1) 控制以保证稳定性)
        async def extract_chunk(i: int, chunk: str) -> str:
            async with self.semaphore:
                prompt = f"""你是一个冷酷的情报提纯机。请从以下文本块中提取出与研究任务相关的核心事实、数据和逻辑关联。

当前研究任务: {task_desc}
原文标题: {title}
当前文本块 (第 {i+1}/{len(chunks)} 块):
{chunk}

要求:
- 提取出的信息必须高信息密度
- 保留具体数字、日期、专有名词
- 以纯文本 Markdown 列表输出，不要寒暄
"""
                for attempt in range(3):
                    try:
                        resp = await llm_extractor.ainvoke([HumanMessage(content=prompt)])
                        return resp.content.strip()
                    except Exception as e:
                        if attempt < 2:
                            print(f"    ⚠️ [Map] 第 {i+1} 块尝试 {attempt+1} 失败: {e}，正在重试...")
                            await asyncio.sleep(5) # 增加等待时间
                        else:
                            print(f"    ❌ [Map] 第 {i+1} 块最终提取失败: {e}")
                            return ""
                return ""

        t_map_start = time.perf_counter()
        map_results = await asyncio.gather(*[extract_chunk(i, c) for i, c in enumerate(chunks)])
        t_map_end = time.perf_counter()
        print(f"    ⚡ [Map] 并发提取完成，耗时: {t_map_end - t_map_start:.1f}s")
        
        # 4. Reduce Phase: 合并去重
        all_facts = "\n\n".join([r for r in map_results if r])
        if not all_facts:
            return self.add_raw_document(f"(Map提取全失败)\n{content[:2000]}", source_url, title)
            
        print(f"    📝 [Reduce] 正在合并 {len(map_results)} 份提取结果...")
        reduce_prompt = f"""你是一个高级情报编辑。请将下面多份从同一篇文章不同章节提取的事实简讯进行合并、去重、和完善。

研究任务: {task_desc}
待合并的事实列表:
{all_facts}

要求:
1. 合并重复的事实，修正由于分块导致的信息碎片。
2. 保持高度简洁，但不要丢失具体的数字、数据和专有名词。
3. 按照逻辑顺序组织成一份最终的浓缩报告。
4. 字数严格控制在 1500 字以内。
5. 以纯文本 Markdown 输出。
"""
        try:
            reduce_resp = await llm_extractor.ainvoke([HumanMessage(content=reduce_prompt)])
            final_facts = reduce_resp.content.strip()
            
            doc = Document(
                page_content=final_facts,
                metadata={
                    "source_url": source_url, 
                    "title": title, 
                    "timestamp": time.time(),
                    "citation_hash": hashlib.md5(source_url.encode()).hexdigest()[:6],
                    "extraction_version": "v2.1_map_reduce",
                    "num_chunks": len(chunks),
                }
            )
            self.fact_blocks.append(doc)
            print(f"    ✅ [V2.1] Map-Reduce 提取完成！最终 {len(final_facts)} 字符")
            return 1
        except Exception as e:
            print(f"    ❌ [Reduce] 合并失败: {e}")
            return self.add_raw_document(f"(Reduce失败)\n{all_facts[:3000]}", source_url, title)


    def retrieve(self, query: str = None, k: int = 100) -> List[Document]:
        """因为大模型长上下文容量大，直接把所有提纯过的 Fact Block 都返回给 Writer"""
        return self.fact_blocks

# 全局单例
km = KnowledgeManager()