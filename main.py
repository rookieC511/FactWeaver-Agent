import asyncio
import sys
import uuid
from core.graph import app

# Windows 下 AsyncIO 的补丁 (防止报错)
if sys.platform.startswith("win"):
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

async def main():
    # 示例 Query：测试 GLM-4V 的读图能力
    # 示例 Query：测试 GLM-4V 的读图能力 + 多轮迭代
    query = "DeepSeek-R1 和 OpenAI o1 的推理范式有什么区别？对 2025 年算力成本有什么影响？"
    
    print(f"🚀 [Deep Research Agent] 启动...")
    print(f"🧠 Model: DeepSeek-R1 + GLM-4V")
    
    # Fix Windows GBK encoding issue for emoji output
    if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
        try:
            sys.stdout.reconfigure(encoding='utf-8', errors='replace')
            sys.stderr.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            pass
    
    try:
        # 启动 Graph (P0: 传入 thread_id 以启用 Checkpointer 状态快照)
        thread_id = str(uuid.uuid4())
        print(f"🧵 [Session] Thread ID: {thread_id}")
        res = await app.ainvoke(
            {
                "query": query, 
                "iteration": 1, 
                "plan": [], 
                "critique": "", 
                "needs_more": True
            },
            config={"configurable": {"thread_id": thread_id}}
        )
        
        filename = "deep_research_report.md"
        with open(filename, "w", encoding="utf-8") as f:
            f.write(res['final_report'])
        print(f"\n✅ 报告生成完毕: {filename}")
        
    except Exception as e:
        print(f"❌ 运行中断: {e}")

if __name__ == "__main__":
    asyncio.run(main())