import asyncio
from writer_graph import editor_node, WriterState

async def test_editor():
    state: WriterState = {
        "query": "巴黎的历史与标志性建筑",
        "outline": [
            {"id": "1", "title": "巴黎简介", "description": "巴黎的地理与历史地位"},
            {"id": "2", "title": "埃菲尔铁塔", "description": "铁塔的建造历史片段"}
        ],
        "sections": {
            "1": "巴黎是法国的首都，位于法兰西岛大区。[Wikipedia](https://en.wikipedia.org/wiki/Paris) 它是全球主要的商业和文化中心之一。\n\n另外，我们要再次说明：巴黎是法国的首都，这非常重要。[Factbook](https://www.cia.gov/the-world-factbook/)",
            "2": "埃菲尔铁塔建于1889年，是为了世界博览会而建。[Eiffel Official](https://www.toureiffel.paris/en) 铁塔全高约330米。[TourInfo](https://fake.url/height)\n\n重复一下刚才的信息以免你忘记：该铁塔在1889年刚好建成。[Eiffel Official](https://www.toureiffel.paris/en) 它真的是330米高。"
        },
        "final_doc": "",
        "iteration": 0,
        "user_feedback": ""
    }
    
    print("🚀 [Prompt Unit Test] 正在测试 Chief Editor (测试点: 去重能力, Markdown标题格式, URL保留)...")
    res = await editor_node(state)
    print("\n" + "="*50)
    print("=== FINAL REPORT OUTPUT ===")
    print("="*50)
    print(res["final_doc"])
    print("="*50)

if __name__ == "__main__":
    asyncio.run(test_editor())
