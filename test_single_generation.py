import asyncio
import sys
import json
from graph import app
from local_judge import get_llm_structure_score
import builtins

# 跳过人工确认环节
builtins.input = lambda prompt="": ""

# Windows AsyncIO 和编码补丁
if sys.platform.startswith("win"):
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
        try:
            sys.stdout.reconfigure(encoding='utf-8', errors='replace')
            sys.stderr.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            pass

async def main():
    query = "What is the capital of France, and what is its role as the cultural center of Europe? Provide historical context and modern economic data."
    
    print(f"🚀 测试启动: 单样本生成 & 评估 (真实检索测试)")
    print(f"❓ Query: {query}")
    
    try:
        # 1. 生成报告
        print("\n⏳ 正在生成报告 (包含 Chief Editor 重写)...")
        res = await app.ainvoke({
            "query": query, 
            "iteration": 1, 
            "plan": [], 
            "critique": "", 
            "needs_more": True
        })
        
        final_report = res['final_report']
        print(f"\n✅ 报告生成完毕 (长度 {len(final_report)} 字符)")
        print("\n--- 报告开头抢先看 ---")
        print(final_report[:800] + "\n...")
        
        # 2. 裁判评估
        print("\n⚖️ 正在调用裁判模型 (DeepSeek-V3.2) 进行打分...")
        score, reason = get_llm_structure_score(final_report)
        
        result_text = f"🏆 最终得分: {score} / 10.0\n📝 裁判评语: {reason}\n\n=== 报告正文 ===\n{final_report}\n"
        with open("test_report_eval.txt", "w", encoding="utf-8") as f:
            f.write(result_text)
            
        print("\n✅ 测试完成，结果已保存至 test_report_eval.txt")
        
    except Exception as e:
        print(f"❌ 运行中断: {e}")

if __name__ == "__main__":
    asyncio.run(main())
