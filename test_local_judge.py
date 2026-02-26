from local_judge import get_llm_structure_score

test_text = """
# Abstract
This is a test report about the history of Paris.

# Introduction
Paris is the capital of France.

# History
It has a long history. [Wikipedia](https://en.wikipedia.org)

# Conclusion
Therefore, Paris is a great city.
"""

print("🚀 Testing local Llama-3 judge...")
score, reason = get_llm_structure_score(test_text)
print(f"得分 (Score): {score}")
print(f"理由 (Reason): {reason}")
