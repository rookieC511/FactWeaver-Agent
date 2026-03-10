
import sys
import os

# Fix Windows GBK encoding issue for emoji output
if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

import json
import re
import asyncio
import pytest
from deepeval.test_case import LLMTestCase, LLMTestCaseParams
from deepeval.metrics import (
    HallucinationMetric,
    FaithfulnessMetric,
    AnswerRelevancyMetric,
    GEval,
)
from tests.adapter import invoke_agent


# =============================================================================
#  Helper: 打印评分报告
# =============================================================================
def print_score_report(test_name: str, metrics: list, judge_model: str = "llama3.1"):
    """统一输出评分报告格式"""
    print(f"\n{'='*65}")
    print(f"  [{test_name}] DeepEval Score Report")
    print(f"  Framework: deepeval v3.8.3 | Judge: {judge_model} (Ollama Local)")
    print(f"{'-'*65}")
    for m in metrics:
        status = "PASS" if m.is_successful() else "FAIL"
        print(f"  [{status}] {m.__class__.__name__}")
        print(f"         Score:     {m.score}")
        print(f"         Threshold: {m.threshold}")
        print(f"         Reason:    {m.reason}")
    print(f"{'='*65}\n")


# =============================================================================
#  TEST 1: 测"良心" (Citation/Conscience) — 模拟 ALCE
#  已有的 Hallucination 测试 + 引用格式校验
# =============================================================================
def test_hallucination(judge_llm):
    """
    [ALCE-Style] 检测幻觉 + 引用完整性。
    - HallucinationMetric: 回答是否与 Context 矛盾？
    - 引用校验: [1] 角标是否对应实际 URL？
    """
    input_query = "What is the capital of France?"
    actual_output = "The capital of France is Paris [1]."
    context = ["Paris is the capital and most populous city of France."]
    retrieval_context = context

    test_case = LLMTestCase(
        input=input_query,
        actual_output=actual_output,
        context=context,
        retrieval_context=retrieval_context,
    )

    metric = HallucinationMetric(threshold=0.5, model=judge_llm)
    metric.measure(test_case)

    # --- 引用格式校验 (ALCE 核心: Citation Recall) ---
    citation_tags = re.findall(r'\[(\d+)\]', actual_output)
    has_citations = len(citation_tags) > 0

    print_score_report("Hallucination / ALCE", [metric], judge_llm.get_model_name())
    print(f"  [Citation Check] Found {len(citation_tags)} citation tag(s): {citation_tags}")
    print(f"  [Citation Check] Has citations: {has_citations}")

    assert metric.is_successful(), (
        f"Hallucination detected! Score: {metric.score}, Reason: {metric.reason}"
    )


# =============================================================================
#  TEST 2: 测"大脑" (Plan/Brain) — 模拟 GAIA
#  直接调用 Planner 节点，不跑全管线
# =============================================================================
def test_planning_capability(judge_llm):
    """
    [GAIA-Style] 测试 Planner 的任务拆解能力。
    直接调用 graph.node_init_search()，检查:
    1. 是否生成了 >= 3 个搜索任务 (search_tasks)
    2. 是否生成了 >= 2 个大纲章节 (outline)
    3. GEval 打分: 任务分解是否合理、MECE
    """
    # Lazy import to avoid module-level import issues
    sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
    from core.graph import node_init_search

    # --- 输入: 一个需要多步拆解的复杂研究 query ---
    complex_query = "对比 DeepSeek-R1 和 OpenAI o1 在数学推理任务上的性能差异，分析各自架构优劣"
    state = {
        "query": complex_query,
        "plan": [],
        "outline": [],
        "user_feedback": "",
        "iteration": 0,
        "final_report": "",
    }

    print(f"\n[Brain/GAIA] Testing planner with: '{complex_query}'")

    # --- 直接调用 Planner 节点 (不跑全管线!) ---
    result = asyncio.run(node_init_search(state))

    search_tasks = result.get("plan", [])
    outline = result.get("outline", [])

    print(f"  Search Tasks Generated: {len(search_tasks)}")
    for i, t in enumerate(search_tasks):
        print(f"    [{i+1}] {t.get('task', 'N/A')}")
    print(f"  Outline Sections: {len(outline)}")
    for i, s in enumerate(outline):
        print(f"    [{i+1}] {s.get('title', 'N/A')}")

    # --- 断言: 基本结构完整性 ---
    assert len(search_tasks) >= 3, (
        f"Planner failed: only {len(search_tasks)} search tasks (expected >= 3). "
        f"The 'brain' cannot decompose complex queries."
    )
    assert len(outline) >= 2, (
        f"Planner failed: only {len(outline)} outline sections (expected >= 2). "
        f"The 'brain' cannot structure a report."
    )

    # --- GEval: 用 llama3.1 Judge 打分 "任务拆解质量" ---
    plan_text = json.dumps(search_tasks, ensure_ascii=False, indent=2)
    outline_text = json.dumps(outline, ensure_ascii=False, indent=2)
    plan_output = f"Search Tasks:\n{plan_text}\n\nOutline:\n{outline_text}"

    test_case = LLMTestCase(
        input=complex_query,
        actual_output=plan_output,
    )

    geval_metric = GEval(
        name="Plan Quality (GAIA-Style)",
        evaluation_params=[
            LLMTestCaseParams.INPUT,
            LLMTestCaseParams.ACTUAL_OUTPUT,
        ],
        criteria=(
            "Evaluate whether the research plan effectively decomposes the input query. "
            "A good plan should: "
            "1) Cover all key aspects of the query (MECE - Mutually Exclusive, Collectively Exhaustive). "
            "2) Have specific, actionable search tasks (not vague). "
            "3) Have a logical report outline that maps to the research goal. "
            "Score 0-1 where 1 is a perfect decomposition."
        ),
        threshold=0.5,
        model=judge_llm,
    )
    geval_metric.measure(test_case)

    print_score_report("Planning / GAIA", [geval_metric], judge_llm.get_model_name())

    assert geval_metric.is_successful(), (
        f"Plan quality too low! Score: {geval_metric.score}, "
        f"Reason: {geval_metric.reason}"
    )


# =============================================================================
#  TEST 3: 测"眼睛" (Context/Eye) — 模拟 LongBench
#  构造长 context，测试忠实度和相关性
# =============================================================================
def test_context_faithfulness(judge_llm):
    """
    [LongBench-Style] 测试模型对长上下文的忠实度。
    构造一段含 "关键数字" 的长文本作为 context，模拟 Agent 回答。
    - FaithfulnessMetric: 回答是否忠实于源文档？(不编造)
    - AnswerRelevancyMetric: 回答是否切题？(不跑题)
    """
    # --- 构造长 Context (模拟搜到的论文/网页内容) ---
    # 关键事实: DeepSeek-R1 有 671B 参数, 在 AIME 2024 上得 79.8%
    key_facts_context = [
        (
            "DeepSeek-R1 is a large language model developed by DeepSeek AI. "
            "The model has 671 billion parameters in total, utilizing a Mixture-of-Experts "
            "(MoE) architecture where only 37 billion parameters are activated per token. "
            "DeepSeek-R1 was trained using a novel reinforcement learning pipeline that "
            "includes Group Relative Policy Optimization (GRPO) instead of traditional PPO. "
            "The training process consists of multiple stages: initial cold-start with "
            "supervised fine-tuning data, followed by large-scale RL training, then "
            "rejection sampling to create new SFT data, and finally another round of RL "
            "with diverse reward signals. "
            "On the AIME 2024 benchmark (math competition problems), DeepSeek-R1 achieved "
            "a pass@1 score of 79.8%, which is comparable to OpenAI's o1 model at 79.2%. "
            "On the MATH-500 benchmark, DeepSeek-R1 scored 97.3%, surpassing all other "
            "open-source models. The model demonstrates emergent chain-of-thought reasoning "
            "capabilities, including self-verification and reflection behaviors that were "
            "not explicitly programmed. DeepSeek-R1 was released as an open-weight model "
            "under the MIT license in January 2025. The distilled versions include "
            "DeepSeek-R1-Distill-Qwen-32B and DeepSeek-R1-Distill-Llama-70B. "
            "The total training cost was estimated at approximately $5.6 million, "
            "significantly lower than comparable frontier models. DeepSeek also released "
            "DeepSeek-R1-Zero, a version trained purely with RL without any SFT, which "
            "demonstrated that reasoning capabilities can emerge from pure reinforcement "
            "learning. However, R1-Zero exhibited readability issues and language mixing "
            "problems that were resolved in the full R1 model through the multi-stage "
            "training pipeline."
        ),
        (
            "OpenAI's o1 model, released in September 2024, represents a different approach "
            "to reasoning. While its exact architecture is not publicly documented, o1 is "
            "believed to use test-time computation scaling, where the model spends more "
            "compute during inference on harder problems. On AIME 2024, o1 achieved 79.2% "
            "pass@1. The key distinction is that o1 is a proprietary, closed-source model "
            "with significantly higher inference costs. The model is accessible only through "
            "OpenAI's API at premium pricing tiers. Unlike DeepSeek-R1's open-weight approach, "
            "o1's training methodology and architecture remain undisclosed."
        ),
    ]

    # --- 模拟 Agent 基于 context 生成的回答 ---
    # 忠实回答 (包含 context 中的关键数字)
    faithful_output = (
        "DeepSeek-R1 是一个拥有 6710 亿参数的大语言模型，采用 MoE 架构，"
        "每个 token 仅激活 370 亿参数。在 AIME 2024 数学竞赛基准测试中，"
        "DeepSeek-R1 达到了 79.8% 的 pass@1 分数，与 OpenAI o1 的 79.2% "
        "相当。在 MATH-500 上更是取得了 97.3% 的成绩。该模型使用了 GRPO "
        "替代传统 PPO 进行强化学习训练，总训练成本约 560 万美元。"
        "DeepSeek-R1 以 MIT 许可证开源发布，而 o1 是闭源商业模型。"
    )

    input_query = "对比 DeepSeek-R1 和 OpenAI o1 的性能和训练成本"

    test_case = LLMTestCase(
        input=input_query,
        actual_output=faithful_output,
        retrieval_context=key_facts_context,
    )

    # --- Metric 1: Faithfulness (忠实度) ---
    faithfulness_metric = FaithfulnessMetric(threshold=0.5, model=judge_llm)
    faithfulness_metric.measure(test_case)

    # --- Metric 2: Answer Relevancy (切题度) ---
    relevancy_metric = AnswerRelevancyMetric(threshold=0.5, model=judge_llm)
    relevancy_metric.measure(test_case)

    print_score_report(
        "Context Faithfulness / LongBench",
        [faithfulness_metric, relevancy_metric],
        judge_llm.get_model_name(),
    )

    assert faithfulness_metric.is_successful(), (
        f"Faithfulness too low! Score: {faithfulness_metric.score}, "
        f"Reason: {faithfulness_metric.reason}"
    )
    assert relevancy_metric.is_successful(), (
        f"Relevancy too low! Score: {relevancy_metric.score}, "
        f"Reason: {relevancy_metric.reason}"
    )


# =============================================================================
#  TEST 4: 端到端集成测试 (保留原有)
# =============================================================================
@pytest.mark.integration
def test_deep_research_integration():
    """
    Integration Test: Run a real research query and verify outputs.
    WARNING: This test will consume tokens and take ~7 minutes.
    """
    query = "DeepSeek-R1 architecture overview"

    print(f"\n[Integration] Running full Agent with query: '{query}'...")

    # 1. Run Agent
    result = invoke_agent(query)

    report = result['actual_output']
    citations = result['citations']
    context = result['retrieval_context']

    # 2. Assertions
    word_count = len(report.split())
    print(f"  Report Word Count: {word_count}")
    assert word_count > 50, f"Report is too short ({word_count} words)."
    assert "DeepSeek" in report, "Report did not mention 'DeepSeek'."

    print(f"  Citations Found: {len(citations)}")
    if len(citations) == 0:
        pytest.skip("No citations found. Skipping.")
    else:
        assert len(citations) > 0

    assert len(context) > 0, "No context retrieved from memory."
