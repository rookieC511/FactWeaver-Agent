import json

file_path = "DeepResearch-Agent-v1.jsonl"  # 替换成你的答卷路径

with open(file_path, 'r', encoding='utf-8') as f:
    lines = f.readlines()

print(f"📊 答卷总数: {len(lines)}")
assert len(lines) == 100, "❌ 警告：答卷数量不足 100 题，请检查是否断点了！"

# 抽查第一题
first_answer = json.loads(lines[0])
assert "id" in first_answer, "❌ 缺少 id 字段"
assert "prompt" in first_answer, "❌ 缺少 prompt 字段"
assert "article" in first_answer, "❌ 缺少 article 字段"

print(f"📝 第一题字数: {len(first_answer['article'])} 字符")
if len(first_answer['article']) < 1000:
    print("⚠️ 警告：第一题报告字数太少，可能在 RACE 结构分上吃亏！")
else:
    print("✅ 格式检查完美通过，可以交卷！")
