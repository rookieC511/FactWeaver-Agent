# Deep Research Agent 开发总结报告

本报告记录了在 Deep Research Agent "阶段 4" (报告与图表生成) 调试过程中遇到的关键技术问题、解决方案以及遗留事项。

## 1. 遇到的问题与解决方案

### 1.1 中文图表乱码 (The "Tofu" Problem)
**问题描述**: 使用 `matplotlib` 和 `seaborn` 生成图表时，中文标题和坐标轴显示为方块 (□□□)。这是由于默认字体库不支持中文字符。
**解决方案**:
- 修改 `charts.py`，显式配置支持中文的字体族。
- 代码变更:
  ```python
  plt.rcParams['font.sans-serif'] = ['SimHei', 'Microsoft YaHei', 'SimSun', 'Arial Unicode MS']
  plt.rcParams['axes.unicode_minus'] = False # 解决负号显示问题
  sns.set_theme(style="whitegrid", font="SimHei")
  ```
- **验证**: 创建并通过了 `test_chart_chinese.py` 单元测试。

### 1.2 报告图片预览失效
**问题描述**: 生成的 Markdown 报告中使用绝对路径 (如 `d:\Projects\...`) 引用图片。在 VS Code 等编辑器中，直接点击预览往往因安全策略或路径格式解析问题失效。
**解决方案**:
- 修改 `writer_graph.py` 中的 `chart_scout_node`。
- 将图片引用路径改为相对于报告的相对路径：`./public/charts/filename.png`。
- **效果**: 报告中的图片现在可以在 Markdown 预览中正确加载，且支持 Ctrl+点击跳转。

### 1.3 缺乏交互式反馈
**问题描述**: 原始流程中，人工审查 (Human Review) 仅允许 "回车继续" 或 "重试"。用户无法具体指导大纲的修改（如增加特定章节）。
**解决方案**:
- 重构 `writer_graph.py` 中的 `WriterState`，增加 `user_feedback` 字段。
- 更新 `human_review_node`，允许用户输入文本反馈。
- 更新 `skeleton_node` (Planner)，使其 prompt 能接收并处理用户的反馈意见。
- **效果**: 成功演示了通过输入指令 "增加 AI 伦理章节"，系统自动重构大纲并生成包含新章节的报告。

### 1.4 环境与依赖
**问题描述**: 初期使用 `conda run -n env_agent` 失败，因为环境实际名称为 `agent_env` 且存在 Conda 路径配置问题。
**解决方案**:
- 改用绝对路径直接调用 Python 解释器：`F:\Conda_Envs\agent_env\python.exe main.py`。

## 2. 遗留与建议

### 2.1 LLM 图表嵌入的不稳定性
**观察**: 尽管 Chart-Scout 节点成功生成图表并在大纲中注入了 `[IMPORTANT] Must embed generated chart` 指令，负责撰写正文的 Writer 节点 (DeepSeek-V3/Fast Model) 偶尔会忽略此指令，导致图表文件存在但未显示在最终 Markdown 中。
**建议**:
- **Prompt 强化**: 在 `section_writer_node` 的 System Prompt 中加强对 `[IMPORTANT]` 标记的权重。
- **后处理 (Post-processing)**: 增加一个 `Editor` 后处理步骤，扫描大纲中未被引用的图表，强制将其追加到对应章节末尾。

### 2.2 运行耗时
**观察**: 完整的 Deep Research 流程 (包含搜索、浏览、规划、写作) 耗时较长 (5-10分钟)。
**建议**: 开发调试时可增加 "Mock Search" 模式，跳过真实的网页浏览，直接使用缓存数据，以加速迭代。

---
**生成时间**: 2026-01-18
