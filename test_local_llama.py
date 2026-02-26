import time
import asyncio
from models import llm_extractor
from langchain_core.messages import HumanMessage

async def main():
    # Load the big text we scraped earlier
    with open('debug_scraper_output.txt', 'r', encoding='utf-8') as f:
        content = f.read()
        
    truncated_content = content[:25000]
        
    prompt = f"""
    你是一个专业提取员。从下面的长文中提取关于中国经济的 5 个数据事实。要求使用 Markdown 列表。
    ================
    {truncated_content}
    """
    
    start = time.time()
    try:
        resp = await llm_extractor.ainvoke([HumanMessage(content=prompt)])
        elapsed = time.time() - start
        
        with open('test_llama_time.txt', 'w', encoding='utf-8') as out:
            out.write(f"Elapsed Time: {elapsed:.2f} seconds\n")
            out.write(resp.content)
            
        print(f"Done in {elapsed:.2f}s")
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    asyncio.run(main())
