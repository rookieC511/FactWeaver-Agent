"""
fetch_real_data.py — 下载真实 MultiNews 数据集并过滤高 token 样本
=================================================================
用途：替代合成噪音数据，为 LongBench 压力测试提供真实的多文档新闻语料。

数据源：HuggingFace `multi_news` (validation split, 直接下载原始文件)
过滤条件：token 数 > 10,000 (使用 tiktoken cl100k_base)
输出：data/longbench_subset.json

用法：
    F:\\Conda_Envs\\agent_env\\python.exe fetch_real_data.py
"""

import json
import os
import sys

# Fix Windows encoding
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

OUTPUT_PATH = os.path.join("data", "longbench_subset.json")
MIN_TOKENS = 8_000  # MultiNews 单篇很长，8k 即可保证质量
MAX_SAMPLES = 5


def count_tokens(text: str) -> int:
    """使用 tiktoken cl100k_base 估算 token 数。"""
    import tiktoken
    enc = tiktoken.get_encoding("cl100k_base")
    return len(enc.encode(text))


def download_raw_files():
    """
    从 HuggingFace Hub 直接下载 multi_news 原始文件。
    datasets v4 不再支持 loading script, 改用 hf_hub_download。
    """
    from huggingface_hub import hf_hub_download

    print("  下载 val.src.cleaned (文档)...")
    src_path = hf_hub_download(
        repo_id="multi_news",
        filename="data/val.src.cleaned",
        repo_type="dataset",
    )
    print("  下载 val.tgt (摘要)...")
    tgt_path = hf_hub_download(
        repo_id="multi_news",
        filename="data/val.tgt",
        repo_type="dataset",
    )
    return src_path, tgt_path


def parse_raw_files(src_path: str, tgt_path: str):
    """解析原始 multi_news 文件。每行一个样本，文档间以 ||||| 分隔。"""
    with open(src_path, "r", encoding="utf-8") as f:
        documents = f.readlines()
    with open(tgt_path, "r", encoding="utf-8") as f:
        summaries = f.readlines()

    assert len(documents) == len(summaries), (
        f"文档数 ({len(documents)}) 与摘要数 ({len(summaries)}) 不匹配"
    )
    return documents, summaries


def main():
    print("=" * 60)
    print("  MultiNews 真实数据集下载 & 过滤")
    print("=" * 60)

    # 1. 下载
    print("\n[1/4] 下载 multi_news 原始文件...")
    src_path, tgt_path = download_raw_files()
    print(f"       文档文件: {src_path}")
    print(f"       摘要文件: {tgt_path}")

    # 2. 解析
    print("\n[2/4] 解析原始文件...")
    documents, summaries = parse_raw_files(src_path, tgt_path)
    print(f"       验证集总样本数: {len(documents)}")

    # 3. 计算 token 数并过滤
    print(f"\n[3/4] 过滤 token > {MIN_TOKENS} 的样本...")
    candidates = []
    for i, (doc, summary) in enumerate(zip(documents, summaries)):
        doc = doc.strip()
        summary = summary.strip()
        if not doc:
            continue
        n_tokens = count_tokens(doc)
        if n_tokens > MIN_TOKENS:
            n_articles = doc.count("|||||") + 1
            candidates.append({
                "idx": i,
                "document": doc,
                "summary": summary,
                "n_tokens": n_tokens,
                "n_articles": n_articles,
            })
        # 进度提示
        if (i + 1) % 1000 == 0:
            print(f"       已扫描 {i+1}/{len(documents)} 条，符合条件: {len(candidates)} 条")

    print(f"       扫描完成。符合条件的样本: {len(candidates)} 条")

    if not candidates:
        print("  ⚠️  没有找到符合条件的样本，降低阈值取 top-5 最长...")
        all_with_tokens = []
        for i, (doc, summary) in enumerate(zip(documents, summaries)):
            doc = doc.strip()
            summary = summary.strip()
            if not doc:
                continue
            n_tokens = count_tokens(doc)
            all_with_tokens.append({
                "idx": i, "document": doc, "summary": summary,
                "n_tokens": n_tokens, "n_articles": doc.count("|||||") + 1
            })
        all_with_tokens.sort(key=lambda x: x["n_tokens"], reverse=True)
        candidates = all_with_tokens[:MAX_SAMPLES]

    # 取 top N 最长
    candidates.sort(key=lambda x: x["n_tokens"], reverse=True)
    selected = candidates[:MAX_SAMPLES]

    print(f"\n       选中 {len(selected)} 条样本:")
    for s in selected:
        print(f"       idx={s['idx']}, tokens={s['n_tokens']}, articles={s['n_articles']}")

    # 4. 保存为兼容格式
    output_data = []
    for s in selected:
        output_data.append({
            "id": f"multinews_val_{s['idx']}",
            "input_docs": s["document"],
            "gold_summary": s["summary"],
            "length": s["n_tokens"],
            # 兼容 stress_test.py 的字段
            "task_id": f"multinews_val_{s['idx']}",
            "context": s["document"],
            "question": (
                f"Synthesize a comprehensive, unified report from these {s['n_articles']} "
                f"news articles. Identify key events, conflicting perspectives, and provide "
                f"a coherent narrative."
            ),
            "gold_answer": s["summary"][:500],
        })

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output_data, f, ensure_ascii=False, indent=2)

    total_size = os.path.getsize(OUTPUT_PATH)
    print(f"\n[4/4] 已保存到 {OUTPUT_PATH}")
    print(f"       文件大小: {total_size / 1024:.1f} KB")
    print(f"       样本数: {len(output_data)}")
    print("\n✅ 完成！可用于 pytest -k longbench")


if __name__ == "__main__":
    main()
