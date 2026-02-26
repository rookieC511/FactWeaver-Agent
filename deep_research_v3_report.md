好的，作为一名世界级研究员，我将基于您提供的知识库检索结果，为您撰写一份关于 DeepSeek-R1 的 GRPO 算法与传统 PPO 算法对比的详细报告。

---

# **DeepSeek-R1 GRPO 算法与传统 PPO 算法对比分析：数学原理、效率优势与成本影响**

**报告编号:** DS-RL-2025-01
**撰写日期:** 2025年1月
**关键词:** 强化学习，PPO，GRPO，DeepSeek-R1，训练成本，显存优化

---

## **摘要**

本报告旨在深入对比 DeepSeek AI 在其 R1 系列模型中采用的 **Group Relative Policy Optimization (GRPO)** 算法与业界标准的 **Proximal Policy Optimization (PPO)** 算法。核心分析聚焦于两者在数学原理上的根本差异，并量化评估 GRPO 在降低训练成本方面的显著优势。分析表明，GRPO 通过**消除独立的“评论家”价值函数模型**，将训练架构从 PPO 的“策略-评论家”双模型简化为单一策略模型。这一改变不仅简化了数学目标函数，还直接带来了 **40-60% 的显存节省**，并可将训练成本降低高达 **18 倍**，使得在消费级硬件上训练大型模型成为可能，同时促进了模型自我验证等高级推理能力的涌现。

---

## **目录**

1.  **引言：PPO 的挑战与 GRPO 的提出**
2.  **数学原理深度对比**
    2.1 PPO 算法框架与目标函数
    2.2 GRPO 算法框架与目标函数
    2.3 核心差异：从全局优势估计到组内相对优势
3.  **GRPO 如何降低训练成本：机制与量化分析**
    3.1 显存节省：模型参数的直接削减
    3.2 计算与时间成本降低
    3.3 数据效率与训练稳定性
4.  **实证效果与案例研究**
    4.1 DeepSeek-R1 的训练效能
    4.2 性能基准对比：与 OpenAI o1 的较量
5.  **结论与未来展望**
6.  **参考文献**

---

## **1. 引言：PPO 的挑战与 GRPO 的提出**

传统上，基于人类反馈的强化学习（RLHF）广泛使用 **Proximal Policy Optimization (PPO)** 算法来对齐大型语言模型（LLM）。PPO 框架通常需要训练和维护两个神经网络：一个**策略模型**负责生成文本，一个**评论家模型**负责评估状态或动作的价值以计算优势函数，从而指导策略更新 [[1]](https://www.appypieautomate.ai/blog/comparison/openai-o1-ppo-vs-deepseek-r1-grpo)。这种双模型架构导致了高昂的计算成本、显存占用和更长的训练时间。

DeepSeek AI 在 2024 年提出了 **Group Relative Policy Optimization (GRPO)**，旨在解决 PPO 的上述痛点。GRPO 的核心创新在于**移除了独立的评论家模型**，转而利用**组内归一化奖励**来估计局部优势 [[2]](https://www.arxiv.org/pdf/2508.02833), [[3]](https://www.philschmid.de/deepseek-r1)。这种方法不仅简化了训练流程，更在资源效率和模型推理能力上展现出显著优势。

## **2. 数学原理深度对比**

### **2.1 PPO 算法框架与目标函数**

PPO 通过最大化一个包含裁剪机制的目标函数来稳定策略更新：
\[
L^{PPO}(\theta) = \mathbb{E}_t \left[ \min\left( r_t(\theta) \hat{A}_t, \text{clip}(r_t(\theta), 1-\epsilon, 1+\epsilon) \hat{A}_t \right) \right] - \beta \cdot D_{KL}[\pi_{\theta_{old}} || \pi_{\theta}]
\]
其中：
- \( r_t(\theta) = \frac{\pi_\theta(a_t|s_t)}{\pi_{\theta_{old}}(a_t|s_t)} \) 是新旧策略的概率比。
- \( \hat{A}_t \) 是**优势函数**，是关键组成部分，通常通过 **广义优势估计（GAE）** 计算，这依赖于一个独立训练的**价值函数 \( V_\phi(s_t) \)**（即评论家模型）来估计状态的价值 [[4]](https://www.boozallen.com/content/dam/home/docs/ai/a-technical-primer-on-deepseek.pdf)。
- 评论家模型 \( V_\phi \) 本身需要通过最小化价值损失 \( (V_\phi(s_t) - R_t)^2 \) 来训练。

因此，PPO 的优化涉及两个耦合的模型更新：策略模型 \( \pi_\theta \) 和评论家模型 \( V_\phi \)。

### **2.2 GRPO 算法框架与目标函数**

GRPO 摒弃了独立的评论家模型，其目标函数在形式上与 PPO 相似，但优势估计 \( \hat{A}_t \) 的来源发生了根本变化 [[2]](https://www.arxiv.org/pdf/2508.02833), [[5]](https://www.boozallen.com/content/dam/home/docs/ai/a-technical-primer-on-deepseek.pdf)：

1.  **组生成**：对于给定提示 \( s_0 \)，使用当前策略生成一个包含 \( G \) 个完整响应（轨迹）的组 \( \mathcal{G} = \{\tau^{(1)}, ..., \tau^{(G)}\} \)。
2.  **组内奖励归一化**：使用奖励模型 \( R \) 为每个响应计算标量奖励 \( r_i = R(\tau^{(i)}) \)。然后，计算**组内相对优势**：
    \[
    \hat{A}^{(i)} = r_i - \bar{r}_{\mathcal{G}}
    \]
    其中 \( \bar{r}_{\mathcal{G}} = \frac{1}{G} \sum_{j=1}^{G} r_j \) 是该组响应的平均奖励，作为动态基线。
3.  **目标函数**：GRPO 的目标函数可表示为：
    \[
    L^{GRPO}(\theta) = \mathbb{E}_t \left[ \min\left( r_t(\theta) \hat{A}^{(i)}_t, \text{clip}(r_t(\theta), 1-\epsilon, 1+\epsilon) \hat{A}^{(i)}_t \right) \right] - \beta \cdot D_{KL}[\pi_{\theta_{old}} || \pi_{\theta}]
    \]
    这里的 \( \hat{A}^{(i)}_t \) 是上述组内相对优势在令牌级别的应用 [[2]](https://www.arxiv.org/pdf/2508.02833)。

### **2.3 核心差异：从全局优势估计到组内相对优势**

| **对比维度** | **PPO** | **GRPO** |
| :--- | :--- | :--- |
| **优势估计源** | 依赖独立的**价值函数模型**，通过 GAE 进行**全局**时序差分估计。 | 依赖**组内奖励的归一化**，使用组平均奖励作为基线，进行**局部**相对比较。 |
| **模型数量** | **2个模型**：策略模型 + 评论家模型 [[1]](https://www.appypieautomate.ai/blog/comparison/openai-o1-ppo-vs-deepseek-r1-grpo)。 | **1个模型**：仅策略模型 [[3]](https://www.philschmid.de/deepseek-r1)。 |
| **训练信号性质** | 试图估计每个状态的绝对“好坏”价值。 | 评估一个响应在同一提示下多个候选响应中的**相对质量**。 |
| **与奖励模型对齐** | 间接对齐。评论家模型需要学习拟合奖励信号。 | 直接对齐。优势直接由奖励模型输出的相对差值决定，更贴合奖励模型通常用于排序多个输出的训练方式 [[3]](https://www.philschmid.de/deepseek-r1)。 |

## **3. GRPO 如何降低训练成本：机制与量化分析**

### **3.1 显存节省：模型参数的直接削减**

这是 GRPO 降低硬件门槛最直接、最显著的贡献。
- **PPO**：需要将**策略模型和评论家模型同时加载到显存中**进行训练。对于一个 70 亿参数的模型，这意味着需要存储两份约 14GB（以 FP16 精度计）的模型参数、梯度和优化器状态，显存需求轻松超过 40GB [[6]](https://chessman7.substack.com/p/grpo-group-relative-policy-optimization)。
- **GRPO**：**完全移除了评论家模型**。根据 DeepSeek 的研究，这直接导致**显存需求减少 40-60%** [[6]](https://chessman7.substack.com/p/grpo-group-relative-policy-optimization), [[7]](https://www.boozallen.com/content/dam/home/docs/ai/a-technical-primer-on-deepseek.pdf)。
- **实际影响**：这使得原本需要高端数据中心 GPU（如 A100 40GB）的训练任务，可以在**消费级 GPU（如 RTX 4090 24GB）** 上完成 [[6]](https://chessman7.substack.com/p/grpo-group-relative-policy-optimization)。Booz Allen 的报告也确认，GRPO 通过减少 PPO 三模型框架中的一个，节约了额外的神经网络成本 [[7]](https://www.boozallen.com/content/dam/home/docs/ai/a-technical-primer-on-deepseek.pdf)。

### **3.2 计算与时间成本降低**

1.  **减少计算图复杂度**：无需前向传播和反向传播更新评论家模型，减少了约一半的核心计算操作。
2.  **消除模型同步开销**：PPO 中策略和评论家模型的训练需要协调，可能引入通信或同步开销。GRPO 的单模型架构避免了这一问题。
3.  **成本效益量化**：
    - **总体训练成本**：据称，在某些场景下，GRPO 可比 PPO **节省高达 18 倍的成本**。一项价值 10,000 美元的 PPO 训练，使用 GRPO 可能仅需约 556 美元 [[6]](https://chessman7.substack.com/p/grpo-group-relative-policy-optimization)。
    - **“训练无关”GRPO 的极端案例**：一项研究显示，对 DeepSeek-V3.1 应用无需参数更新的“训练无关 GRPO”，仅花费 **18 美元**的 API 调用成本，就在复杂数学问题上达到了 82.7% 的准确率，而传统微调方法成本超过 10,000 美元，准确率仅为 67% [[8]](https://www.linkedin.com/posts/pascalbiese_training-free-group-relative-policy-optimization-activity-7383436422424289280-LPfc), [[9]](https://arxiv.org/html/2510.08191v1)。

### **3.3 数据效率与训练稳定性**

- **组内归一化的稳定效应**：使用组平均作为基线，可以在一定程度上**减少优势估计的方差**，提供更稳定的更新信号，这可能间接提高数据效率 [[6]](https://chessman7.substack.com/p/grpo-group-relative-policy-optimization), [[10]](https://theses.liacs.nl/pdf/2024-2025-CremerAAlexander.pdf)。
- **促进复杂行为涌现**：GRPO 鼓励模型为同一问题生成多样化的解决方案并进行比较，这自然地培养了**自我验证、反思和探索**的能力。DeepSeek-R1-Zero 甚至跳过了监督微调，直接通过 GRPO 进行纯强化学习，出现了识别并自我纠正推理错误的“顿悟时刻” [[6]](https://chessman7.substack.com/p/grpo-group-relative-policy-optimization), [[11]](https://eu.36kr.com/en/p/3471856369702276)。

## **4. 实证效果与案例研究**

### **4.1 DeepSeek-R1 的训练效能**

DeepSeek-R1 采用多阶段训练流程，其中 GRPO 是核心的强化学习阶段 [[7]](https://www.boozallen.com/content/dam/home/docs/ai/a-technical-primer-on-deepseek.pdf)。它使用基于规则的综合奖励（如答案准确性、格式规范性、语言一致性）来指导训练 [[7]](https://www.boozallen.com/content/dam/home/docs/ai/a-technical-primer-on-deepseek.pdf)。这种方法使得模型能够生成结构化的思维链（如使用 `<think>...` 标签），并强化最佳推理路径 [[1]](https://www.appypieautomate.ai/blog/comparison/openai-o1-ppo-vs-deepseek-r1-grpo)。

### **4.2 性能基准对比：与 OpenAI o1 的较量**

尽管训练成本显著降低，DeepSeek-R1 在推理任务上的性能与使用传统 PPO 训练的 OpenAI o1 模型不相上下。
- 在 DeepSeek 公布的 11 个双方均有分数的基准测试中，o1 在 6 个上领先，R1 在 5 个上领先，差距非常微小 [[12]](https://epochai.substack.com/p/what-went-into-training-deepseek)。
- 学术研究也表明，GRPO 在传统 RL 环境中能取得与 PPO 竞争的结果，考虑到 GRPO 是一个较新且架构更简单的算法，这凸显了其巨大潜力 [[10]](https://theses.liacs.nl/pdf/2024-2025-CremerAAlexander.pdf)。

## **5. 结论与未来展望**

**结论**：
GRPO 通过其创新的**组内相对策略优化**数学框架，在保持与 PPO 相当甚至更优的模型性能（尤其在推理任务上）的同时，实现了训练效率的跨越式提升。其**移除评论家模型**的设计直接带来了 **40-60% 的显存节省**，并将总体训练成本降低了一个数量级（最高可达 18 倍）。这 democratizes 了大型语言模型的高阶对齐训练，使更多研究机构和企业能够负担得起。

**未来展望**：
1.  **算法成熟度**：PPO 经过多年调优和研究，非常成熟。GRPO 作为新算法，在超参数优化、理论理解方面仍有探索空间 [[10]](https://theses.liacs.nl/pdf/2024-2025-CremerAAlexander/pdf/2024-2025-CremerAAlexander.pdf)。
2.  **应用范围**：目前 GRPO 在 STEM 和推理任务上表现突出 [[7]](https://www.boozallen.com/content/dam/home/docs/ai/a-technical-primer-on-deepseek.pdf)。其能否在创意写作、诗歌等更广泛的任务上同样有效，有待验证。
3.  **范式演进**：GRPO 代表了 RLHF 领域向 **更轻量、更高效、更贴近奖励模型本质** 方向的发展趋势。未来的训练方法可能会进一步融合 GRPO 的思想，追求极致的成本效益比 [[1]](https://www.appypieautomate.ai/blog/comparison/openai-o1-ppo-vs-deepseek-r1-grpo)。

总而言之，GRPO 并非意在完全取代 PPO，但它为特定场景（尤其是资源受限下的复杂推理模型训练）提供了一个极具吸引力的高效替代方案，并正在塑造 AI 训练方法的未来图景。

## **6. 参考文献**

1.  Appy Pie Automate. “OpenAI o1 PPO vs. DeepSeek R1 GRPO: A Beginner-Friendly & Technical Breakdown.” (2025). [链接](https://www.appypieautomate.ai/blog/comparison/openai-o1-ppo-vs-deepseek-r1-grpo)
2.  Arxiv. “On the Theory and Practice of GRPO.” (2025). [链接](https://www.arxiv.org/pdf/2508.02833)
3.  Philipp Schmid. “Bite: How Deepseek R1 was trained.” (2025). [链接](https://www.philschmid.de/deepseek-r1)
4.  Booz Allen Hamilton. “A Technical Primer on DeepSeek.” (2025). [链接](https://www.boozallen.com/content/dam/home/docs/ai/a-technical-primer-on-deepseek.pdf)
5.  Booz Allen Hamilton. “A Technical Primer on DeepSeek.” (Figure 5: PPO vs. GRPO comparison). [链接](https://www.boozallen.com/content/dam/home/docs/ai/a-technical-primer-on-deepseek.pdf)
6.  Chessman7 (Substack). “GRPO - Group Relative Policy Optimization: How DeepSeek Trains ...” (2024). [链接](https://chessman7.substack.com/p/grpo-group-relative-policy-optimization)
7.  Booz Allen Hamilton. “A Technical Primer on DeepSeek.” (提及 GRPO 节省成本与内存). [链接](https://www.boozallen.com/content/dam/home/docs/ai/a-technical-primer-on-deepseek.pdf)
8.  Pascal Biese (LinkedIn). “Training-Free Group Relative Policy Optimization.” (2025). [链接](https://www.linkedin.com/posts/pascalbiese_training-free-group-relative-policy-optimization-activity-7383436422424289280-LPfc)
9.  Arxiv. “Training-Free Group Relative Policy Optimization.” (2025). [链接](https://arxiv.org/html/2510.08191v1)
10. Cremer, A. Alexander. “Deepseek vs OpenAI A comparison of GRPO and PPO in ...” (Master‘s Thesis, LIACS). (2025). [链接](https://theses.liacs.nl/pdf/2024-2025-CremerAAlexander.pdf)
11. 36Kr Global. “DeepSeek - R1 on Nature‘s Cover: First Mainstream Large Model ...” (2025). [链接](https://eu.36kr.com/en/p/3471856369702276)
12. Ege Erdil (Epoch AI Substack). “What went into training DeepSeek-R1?” (2025). [链接](https://epochai.substack.com/p/what-went-into-training-deepseek)