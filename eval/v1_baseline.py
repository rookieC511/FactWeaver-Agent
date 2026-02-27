"""
V1.0 Baseline 体检报告 — 核心评测引擎
======================================
直接调用 memory.py 的 KnowledgeManager.aadd_document() 管线，
用三维指标（Recall / Precision / Latency+VRAM）量化 V1.0 表现。

Usage:
    F:\\Conda_Envs\\agent_env\\python.exe eval/v1_baseline.py [--cases N]
"""

import sys
import os
import asyncio
import time
import subprocess
import json
import re
import argparse
from datetime import datetime

# Ensure project root importable
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, PROJECT_ROOT)

from eval.test_cases import get_all_cases


# ============================================================
# GPU / VRAM 监控
# ============================================================

def get_gpu_vram_mib() -> int:
    """调用 nvidia-smi 获取当前 GPU 显存占用 (MiB)，失败返回 -1"""
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
            encoding="utf-8", timeout=5
        ).strip()
        # 可能多卡，取第一张
        return int(out.split("\n")[0].strip())
    except Exception:
        return -1


def get_gpu_info() -> str:
    """获取 GPU 型号"""
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            encoding="utf-8", timeout=5
        ).strip()
        return out.split("\n")[0].strip()
    except Exception:
        return "Unknown (nvidia-smi unavailable)"


# ============================================================
# 指标计算
# ============================================================

def calc_recall(extracted_facts: str, ground_truth: list[str]) -> tuple[float, list[bool]]:
    """
    两阶段 Recall 计算:
    Stage 1: 快速关键词/子串匹配 (免费、即时)
    Stage 2: 对 Stage 1 未命中的项，调用 LLM 语义匹配 (更准确但耗时)
    返回 (recall_rate, per_item_hit_list)
    """
    # 预处理提取结果：统一去除多余空白
    clean_facts = re.sub(r'[\s\u3000]+', '', extracted_facts)
    
    hits = []
    for gt in ground_truth:
        # Stage 1: 关键词匹配
        clean_gt = re.sub(r'[\s\u3000]+', '', gt)
        keywords = _extract_keywords(clean_gt)
        hit = any(kw in clean_facts for kw in keywords)
        hits.append(hit)
    
    recall = sum(hits) / len(hits) if hits else 0.0
    return recall, hits


async def calc_recall_with_llm(extracted_facts: str, ground_truth: list[str]) -> tuple[float, list[bool]]:
    """
    增强版 Recall：先跑关键词匹配，对未命中的项再用 LLM Judge 做语义匹配。
    """
    from models import llm_extractor
    from langchain_core.messages import HumanMessage
    
    # Stage 1: 关键词匹配
    clean_facts = re.sub(r'[\s\u3000]+', '', extracted_facts)
    hits = []
    needs_llm = []  # (index, gt_text) — 需要 LLM 二次判断的项
    
    for i, gt in enumerate(ground_truth):
        clean_gt = re.sub(r'[\s\u3000]+', '', gt)
        keywords = _extract_keywords(clean_gt)
        hit = any(kw in clean_facts for kw in keywords)
        hits.append(hit)
        if not hit:
            needs_llm.append((i, gt))
    
    # Stage 2: LLM 语义匹配 (仅对未命中项)
    if needs_llm:
        gt_list_str = "\n".join([f"{idx+1}. {gt}" for idx, (_, gt) in enumerate(needs_llm)])
        prompt = f"""你是一个严格的事实核查员。请判断下面的"待查事实"是否在"提取结果"中有直接对应的表述。
注意：表述方式可以不同，只要语义一致即可判为命中。

【提取结果】:
{extracted_facts}

【待查事实列表】:
{gt_list_str}

请对每条待查事实判断：如果提取结果中包含了该事实的核心信息（数字、实体、结论一致），输出 HIT，否则输出 MISS。

严格按以下格式逐行输出，不要多余文字:
1. HIT
2. MISS
..."""
        
        try:
            resp = await llm_extractor.ainvoke([HumanMessage(content=prompt)])
            lines = resp.content.strip().split("\n")
            for j, (orig_idx, _) in enumerate(needs_llm):
                if j < len(lines) and "HIT" in lines[j].upper():
                    hits[orig_idx] = True
                    print(f"     🔁 LLM 语义匹配翻转: {ground_truth[orig_idx][:40]}... → ✅")
        except Exception as e:
            print(f"     ⚠️ LLM 语义匹配失败: {e}")
    
    recall = sum(hits) / len(hits) if hits else 0.0
    return recall, hits


def _extract_keywords(text: str) -> list[str]:
    """从一条 ground truth 中提取多个可匹配的关键词片段"""
    keywords = []
    
    # 完整匹配（去空白后）
    keywords.append(text)
    
    # 提取数字+单位 (如 "6000亿美元", "55.2%", "350万个")
    num_patterns = re.findall(r'\d[\d.]*[%亿万个台座GB秒MiB]+', text)
    keywords.extend(num_patterns)
    
    # 提取纯数字 (如 "6000", "500", "350")
    pure_nums = re.findall(r'\d{3,}', text)
    keywords.extend(pure_nums)
    
    # 提取专有名词 (英文片段, 至少2字符)
    en_patterns = re.findall(r'[A-Za-z][\w-]{1,}(?:\s+[A-Za-z][\w-]*)*', text)
    keywords.extend(en_patterns)
    
    # 取中文子串 — 更细粒度切分 (4字符滑动窗口)
    cn_only = re.sub(r'[^\u4e00-\u9fff]', '', text)
    if len(cn_only) >= 4:
        # 取多组 4 字符片段
        for start in range(0, len(cn_only) - 3, 4):
            keywords.append(cn_only[start:start+4])
    
    return [k for k in keywords if len(k) >= 2]


async def calc_needle_recall_llm(extracted_facts: str, needle: str) -> bool:
    """用 LLM 语义匹配检查 needle 的核心信息是否被提取"""
    from models import llm_extractor
    from langchain_core.messages import HumanMessage
    
    # Stage 1: 快速关键词检查
    clean_facts = re.sub(r'[\s\u3000]+', '', extracted_facts)
    keywords = _extract_keywords(re.sub(r'[\s\u3000]+', '', needle))
    if keywords:
        hit_count = sum(1 for kw in keywords if kw in clean_facts)
        if hit_count / len(keywords) >= 0.3:
            return True
    
    # Stage 2: LLM 语义检查
    prompt = f"""请判断以下"Needle事实"的核心信息是否在"提取结果"中有体现。
只要核心实体和数据一致即可，不要求措辞完全相同。

【提取结果】:
{extracted_facts[:3000]}

【Needle事实】:
{needle}

如果提取结果中包含了Needle的核心信息，请回答 HIT。否则回答 MISS。
只输出一个词: HIT 或 MISS"""
    
    try:
        resp = await llm_extractor.ainvoke([HumanMessage(content=prompt)])
        return "HIT" in resp.content.strip().upper()
    except Exception:
        return False



async def calc_precision_llm(extracted_facts: str, original_text: str) -> tuple[float, str]:
    """
    用本地 Llama-3.1 Judge 检查提取结果中的幻觉比例。
    返回 (precision_score, judge_reasoning)
    """
    from models import llm_extractor
    from langchain_core.messages import HumanMessage
    
    # 截取原文前 8000 字供 Judge 参考（Judge 本身也受限于窗口）
    ref_text = original_text[:8000]
    
    prompt = f"""你是一个严格的事实核查员。下面有两段文本：
    
【原文摘录（前8000字）】:
{ref_text}

【Agent 提取的事实摘要】:
{extracted_facts}

请你逐条检查"Agent 提取的事实摘要"中的每一条陈述：
1. 如果该陈述可以在原文中找到依据，标记为 ✅ 有据
2. 如果该陈述在原文中找不到任何依据（属于模型自行编造/幻觉），标记为 ❌ 幻觉

最后统计有据条数和幻觉条数。

请严格按以下 JSON 格式输出，不要加任何多余文字:
{{"total_claims": 10, "supported": 8, "hallucinated": 2, "reasoning": "简要说明哪些是幻觉"}}
"""
    
    try:
        resp = await llm_extractor.ainvoke([HumanMessage(content=prompt)])
        content = resp.content.strip()
        # 尝试从回复中提取 JSON
        json_match = re.search(r'\{[^{}]+\}', content, re.DOTALL)
        if json_match:
            result = json.loads(json_match.group())
            total = result.get("total_claims", 1)
            supported = result.get("supported", 0)
            precision = supported / total if total > 0 else 0.0
            return precision, result.get("reasoning", "N/A")
        else:
            return -1.0, f"JSON parse failed: {content[:200]}"
    except Exception as e:
        return -1.0, f"LLM Judge error: {str(e)[:200]}"


# ============================================================
# 主评测流程
# ============================================================

async def run_single_case(case: dict, km) -> dict:
    """对单个 Case 执行完整评测"""
    case_id = case["case_id"]
    print(f"\n{'='*70}")
    print(f"  🧪 评测 {case_id}: {case['title']}")
    print(f"  📏 文本长度: {case['text_length']} chars | Needle@{case['needle_position']}")
    print(f"{'='*70}")
    
    # 清除上一轮的记忆
    km.clear()
    
    # 记录 VRAM 基线
    vram_before = get_gpu_vram_mib()
    
    # 计时：调用 V1.0 提纯管线
    t_start = time.perf_counter()
    count = await km.aadd_document(
        content=case["text"],
        source_url=f"https://eval.test/{case_id}",
        title=case["title"],
        task_desc=case["task_desc"]
    )
    t_end = time.perf_counter()
    latency = t_end - t_start
    
    # 记录 VRAM 峰值（注：这是近似值，精确采集需要异步轮询）
    vram_after = get_gpu_vram_mib()
    vram_peak = max(vram_before, vram_after)
    
    # 提取结果
    if km.fact_blocks:
        extracted = km.fact_blocks[-1].page_content
    else:
        extracted = "(无提取结果)"
    
    print(f"  ⏱️ 耗时: {latency:.1f}s | VRAM: {vram_before}→{vram_after} MiB")
    print(f"  📝 提取结果长度: {len(extracted)} chars")
    
    # 指标 1: Recall (两阶段：关键词 + LLM 语义匹配)
    print(f"  📊 [Stage 1] 关键词匹配...")
    recall, hits = calc_recall(extracted, case["ground_truth"])
    print(f"     关键词命中: {sum(hits)}/{len(hits)}")
    
    # Stage 2: 对未命中项用 LLM 语义匹配
    if sum(hits) < len(hits):
        print(f"  📊 [Stage 2] LLM 语义匹配 ({len(hits) - sum(hits)} 项待判)...")
        recall, hits = await calc_recall_with_llm(extracted, case["ground_truth"])
    
    needle_hit = await calc_needle_recall_llm(extracted, case["needle"])

    print(f"  📊 最终 Recall: {recall:.2%} ({sum(hits)}/{len(hits)})")
    print(f"  🎯 Needle Recall: {'✅ 命中' if needle_hit else '❌ 丢失'}")
    for i, (gt, hit) in enumerate(zip(case["ground_truth"], hits)):
        print(f"     {'✅' if hit else '❌'} {gt[:50]}")
    
    # 指标 2: Precision (LLM Judge)
    print(f"  🤖 正在调用 LLM Judge 检查幻觉...")
    precision, precision_reason = await calc_precision_llm(extracted, case["text"])
    if precision >= 0:
        print(f"  🎯 Precision: {precision:.2%} | {precision_reason[:100]}")
    else:
        print(f"  ⚠️ Precision 评估失败: {precision_reason[:100]}")
    
    return {
        "case_id": case_id,
        "title": case["title"],
        "text_length": case["text_length"],
        "needle_position": case["needle_position"],
        "recall": recall,
        "recall_hits": hits,
        "needle_recall": needle_hit,
        "precision": precision,
        "precision_reason": precision_reason,
        "latency_s": round(latency, 2),
        "vram_before_mib": vram_before,
        "vram_after_mib": vram_after,
        "vram_peak_mib": vram_peak,
        "extracted_length": len(extracted),
        "extracted_preview": extracted[:500],
        "ground_truth": case["ground_truth"],
    }


def generate_report(results: list[dict], gpu_name: str) -> str:
    """生成 Markdown 格式的 Baseline 体检报告"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    lines = [
        "# FactWeaver V1.0 Baseline 体检报告",
        "",
        "## 环境信息",
        f"- **生成时间**: {now}",
        f"- **Extractor 模型**: `llama3.1:latest` (Ollama, 本地)",
        f"- **MAX_CHARS**: 25000 (memory.py 硬截断)",
        f"- **GPU**: {gpu_name}",
        f"- **测试用例数**: {len(results)}",
        "",
        "## 汇总结果",
        "",
        "| Case | 文本长度 | Needle位置 | Recall | Needle命中 | Precision | 耗时(s) | VRAM峰值(MiB) |",
        "|------|----------|------------|--------|-----------|-----------|---------|---------------|",
    ]
    
    total_recall = 0
    total_precision = 0
    total_latency = 0
    needle_hits = 0
    precision_count = 0
    
    for r in results:
        needle_str = "✅" if r["needle_recall"] else "❌"
        precision_str = f"{r['precision']:.0%}" if r["precision"] >= 0 else "N/A"
        lines.append(
            f"| {r['case_id']} | {r['text_length']} | {r['needle_position']} | "
            f"{r['recall']:.0%} | {needle_str} | {precision_str} | "
            f"{r['latency_s']:.1f} | {r['vram_peak_mib']} |"
        )
        total_recall += r["recall"]
        total_latency += r["latency_s"]
        if r["needle_recall"]:
            needle_hits += 1
        if r["precision"] >= 0:
            total_precision += r["precision"]
            precision_count += 1
    
    n = len(results)
    avg_recall = total_recall / n if n else 0
    avg_precision = total_precision / precision_count if precision_count else 0
    avg_latency = total_latency / n if n else 0
    
    lines.extend([
        "",
        "### 平均指标",
        f"- **平均 Recall**: {avg_recall:.1%}",
        f"- **Needle 命中率**: {needle_hits}/{n} ({needle_hits/n:.0%})",
        f"- **平均 Precision**: {avg_precision:.1%}" + (" (部分Case评估失败)" if precision_count < n else ""),
        f"- **平均耗时**: {avg_latency:.1f}s",
        "",
    ])
    
    # 诊断结论
    lines.extend([
        "## 诊断结论",
        "",
    ])
    
    if avg_recall < 0.6:
        lines.append("> ⚠️ **Recall 偏低** (<60%) — V1.0 的 25K 截断 + 单次提取可能遗漏大量事实，升级 V2.0 滚动窗口有明确收益空间。")
    elif avg_recall < 0.8:
        lines.append("> 📊 **Recall 中等** (60-80%) — V1.0 基本可用但有提升空间，V2.0 滚动窗口可望进一步改善。")
    else:
        lines.append("> ✅ **Recall 较高** (>80%) — V1.0 在当前文本长度下表现良好。")
    
    lines.append("")
    
    if needle_hits < n:
        lines.append(f"> 🎯 **Needle 丢失 {n - needle_hits}/{n} 例** — 这是『中间遗忘 (Lost in the Middle)』效应的直接证据。V2.0 的滚动窗口可以将模型锁定在最优 8K 窗口内，预期可以显著改善。")
    else:
        lines.append("> 🎯 **Needle 全部命中** — 在当前文本长度下，V1.0 的召回能力完好。")
    
    lines.append("")
    
    # 各 Case 详情
    lines.extend([
        "## 各 Case 详情",
        "",
    ])
    
    for r in results:
        lines.extend([
            f"### {r['case_id']}: {r['title']}",
            "",
            f"- **Needle 位置**: {r['needle_position']}",
            f"- **Recall**: {r['recall']:.0%} | **Needle**: {'✅' if r['needle_recall'] else '❌'}",
            f"- **Precision**: {r['precision']:.0%}" if r['precision'] >= 0 else f"- **Precision**: N/A",
            f"- **耗时**: {r['latency_s']:.1f}s | **VRAM**: {r['vram_before_mib']}→{r['vram_after_mib']} MiB",
            "",
            "**Ground Truth 命中情况**:",
            "",
        ])
        for gt, hit in zip(r["ground_truth"], r["recall_hits"]):
            lines.append(f"- {'✅' if hit else '❌'} {gt}")
        
        lines.extend([
            "",
            "<details><summary>提取结果预览 (点击展开)</summary>",
            "",
            f"```",
            r["extracted_preview"],
            f"```",
            "",
            "</details>",
            "",
            f"**Precision Judge 理由**: {r['precision_reason'][:300]}",
            "",
            "---",
            "",
        ])
    
    return "\n".join(lines)


async def main():
    parser = argparse.ArgumentParser(description="V1.0 Baseline 体检报告")
    parser.add_argument("--cases", type=int, default=0, help="只跑前 N 个 Case (0=全部)")
    args = parser.parse_args()
    
    print("=" * 70)
    print("  🏥 FactWeaver V1.0 Baseline 体检报告")
    print("  " + "=" * 66)
    print(f"  📅 时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    
    # GPU 信息
    gpu_name = get_gpu_info()
    print(f"  🖥️ GPU: {gpu_name}")
    print(f"  📊 VRAM 基线: {get_gpu_vram_mib()} MiB")
    print("=" * 70)
    
    # 加载测试用例
    cases = get_all_cases()
    if args.cases > 0:
        cases = cases[:args.cases]
    print(f"\n📦 加载了 {len(cases)} 个测试用例")
    
    # 导入 KnowledgeManager (lazy, 避免 Ollama 未启动时报错)
    from memory import km
    
    # 逐个运行
    results = []
    for case in cases:
        result = await run_single_case(case, km)
        results.append(result)
    
    # 生成报告
    report_md = generate_report(results, gpu_name)
    report_path = os.path.join(PROJECT_ROOT, "eval", "baseline_report.md")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report_md)
    
    print(f"\n{'='*70}")
    print(f"  ✅ 体检报告已保存至: {report_path}")
    print(f"{'='*70}")
    
    # 同时输出 JSON (方便程序化对比)
    json_path = os.path.join(PROJECT_ROOT, "eval", "baseline_results.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"  📊 原始数据已保存至: {json_path}")


if __name__ == "__main__":
    # Windows asyncio compat
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
