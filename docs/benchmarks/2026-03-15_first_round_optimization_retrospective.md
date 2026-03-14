# 第一轮优化复盘：从低质检索到可控 Evidence Acquisition

## 1. 这轮优化想解决什么

这轮优化一开始并不是为了“刷 benchmark 分数”，而是为了回答一个更根本的问题：

- 为什么系统能写出长报告，但公开 benchmark 上的报告质量并不稳定
- 为什么 `fact_score` 不低，但 `drb_report_score` 过不了线
- 为什么很多任务最后会退化成“证据不足”或 `retrieval_failed`

结论最后证明，这个问题本质上不是 writer 单点问题，而是：

1. 检索到的来源质量不够高
2. 高价值来源命中了，但抓取经常失败
3. 证据门控不够硬，导致证据不足时系统仍继续写报告
4. 评测链自己也有一部分噪声，放大了问题

这轮优化最后把问题真正收敛成了一个独立的 `Evidence Acquisition` 问题。

---

## 2. 一开始我们是怎么构建指标的

### 2.1 主评分：`drb_report_score`

公开长报告评测走的是本地兼容版 `DeepResearch Bench` 风格打分。

主分是：

- `drb_report_score`

它由 4 个维度加权构成：

- `comprehensiveness`
  - 报告覆盖是否完整，关键方面有没有漏
- `insight`
  - 是否有真正的分析、比较、因果、判断，而不只是资料堆砌
- `instruction_following`
  - 是否直接回答了任务要求，是否逐条覆盖题目
- `readability`
  - 结构、表达、层次是否清晰

这 4 维分数来自每题的 criteria 和维度权重，汇总成最终的 `drb_report_score`。

### 2.2 辅助评分：`fact_score`

除了主分，我们还保留：

- `fact_score`

它不是“报告整体好不好”，而是看：

- 引用是否充足
- 事实支撑是否可靠
- 是否存在 unsupported claim 风险

所以这两个分是分工不同的：

- `drb_report_score` 回答“这篇报告整体像不像一篇高质量研究报告”
- `fact_score` 回答“这篇报告在事实支撑上是否站得住”

#### `fact_score` 的实际打分口径

`fact_score` 在实现上有两条路径：

1. 本地 judge 可用时
   - 走 `local_judge_scores(...)`
   - judge 只看 3 件事：
     - citation support
     - traceability
     - unsupported-claim risk
2. 本地 judge 不可用时
   - 走 heuristic fallback
   - 用启发式规则估算事实支撑强度

heuristic 版本不是拍脑袋，而是按一组可数的信号算出来的：

- `hash_count`
  - `[HASH:...]` 的数量
- `markdown_links`
  - markdown 链接数量
- `bare_urls`
  - 裸 URL 数量
- `word_count`
- `missing_signals`
  - 比如：
    - `HTTP 403`
    - `HTTP 429`
    - `blocked`
    - `content too short or blocked`
    - `Unavailable`

启发式里会先构造一个“引用强度”：

- `citation_units = hash_count + 0.6 * markdown_links + 0.25 * bare_urls`

再算一个“引用密度”：

- `density = citation_units / (word_count / 250)`

然后用这些信号加减分：

- 引用越多、越密，`fact_score` 越高
- 缺失来源、blocked、Unavailable 这类信号越多，`fact_score` 越低
- 如果几乎没有引用，会有额外重罚

所以 `fact_score` 的本质不是“写得像不像一篇好文章”，而是：

- 这篇报告的事实支撑是否充足
- 读者能否沿着引用追回证据
- unsupported claim 的风险高不高

### 2.3 过程指标：检索、抓取、证据覆盖

为了定位真正问题，我们又补了一组过程指标：

- `authority_source_rate`
  - 候选来源里高权威来源的比例
- `weak_source_hit_rate`
  - 弱来源命中比例
- `blocked_source_rate`
  - 真正进入 fetch pipeline 后，最终没被救回的 blocked URL 比例
- `blocked_attempt_rate`
  - provider 层 blocked 尝试比例，用于诊断 pipeline 脆弱点
- `successful_authority_fetch_rate`
  - 高权威来源成功抓取率
- `evidence_coverage_rate`
  - evidence slot 覆盖率
- `direct_answer_support_rate`
  - `Direct Answer` 是否有高权威证据支持
- `retrieval_failed`
  - 不是简单的“某次抓取失败”，而是经过限定 fallback 后仍拿不到足够证据

### 2.4 第一轮 gate

正式 pilot 一开始的通过门槛是：

- `success_rate >= 0.83`
- `avg_drb_report_score >= 6.5`
- `avg_fact_score >= 5.8`
- “无法确定 / 证据不足”型失败不能过多

这套门槛的意义是：不让我们“先跑完整批实验，最后才发现结果其实不可用”。

#### `success_rate` 的实际定义

`success_rate` 的定义非常直接：

- `status == "SUCCESS"` 的任务数 / 总任务数

也就是说，它不是：

- `drb_report_score` 超过多少就算成功
- `fact_score` 超过多少就算成功
- 生成了报告文本就算成功

它只看任务最终状态。

对 `DeepResearch Bench` 这条链来说，任务状态又和 `retrieval_failed` 紧密相关：

- benchmark 任务如果 `retrieval_failed == true`
  - 最终状态会写成 `FAILED`
- 只有没有触发 retrieval failure，任务才有机会记成 `SUCCESS`

所以在这条评测链里：

- `success_rate` 更像“系统有没有把任务真正做成”
- `drb_report_score` 更像“做成之后，报告质量好不好”

这也是为什么后期经常会出现一种看起来有点反直觉的情况：

- `drb_report_score` 已经不低
- `fact_score` 也不低
- 但 `success_rate` 仍然上不去

原因不是分数错了，而是：

- evidence gate 变硬了
- 系统开始更诚实地把“证据不够”的任务打成失败
- 不再像早期那样在证据不足时也硬写成“成功样本”

---

## 3. 初始基线：问题是怎么暴露出来的

### 3.1 第一版 6 题 pilot

运行：

- `drb-pilot-20260312_175533`

结果：

- `avg_drb_report_score = 6.209`
- `avg_fact_score = 7.71`
- 四维均分：
  - `comprehensiveness = 6.525`
  - `insight = 5.5417`
  - `instruction_following = 6.2667`
  - `readability = 7.1167`
- `failure_tag_counts`
  - `blocked_source = 6`
  - `instruction_miss = 1`

这一版立刻暴露出两个信号：

1. `fact_score` 不低，说明系统不是“胡写”
2. `insight` 最弱，而且 `blocked_source = 6/6`

这说明：

- 问题不是纯 writer 风格
- 更像是“证据拿得不够好，所以分析深度起不来”

### 3.2 第二版 6 题 pilot：本地 judge 出现明显异常

运行：

- `drb-pilot-20260313_012418`

表面结果：

- `avg_drb_report_score = 3.7294`
- `avg_fact_score = 8.4`

这个结果很不合理，因为它同时满足：

- `fact_score` 很高
- 主分却极低

进一步排查后发现，问题不在系统本身，而在评分链：

1. 本地 judge 有时返回缺字段 JSON
2. 解析逻辑把缺失维度默认成 `0`
3. 又被 `_clamp_score()` 夹成 `1.0`
4. 于是出现了“四维全 1 分”的假低分

所以原始 `3.7294` 不能作为真实基线。

### 3.3 6 题离线重评分：修正后的可信基线

重评分后：

- `avg_drb_report_score = 6.2274`
- `avg_fact_score = 7.8783`
- 四维均分：
  - `comprehensiveness = 5.7417`
  - `insight = 6.1367`
  - `instruction_following = 6.7`
  - `readability = 6.8833`
- `failure_tag_counts`
  - `retrieval_miss = 4`
  - `structure_weak = 1`
  - `degraded_to_unknown = 1`
  - `instruction_miss = 1`

这一版给出了更可信的结论：

- 当前系统并不是 3 分水平
- 更合理的真实基线在 `6.2` 左右
- 真正最低的是 `comprehensiveness`
- 真正的失败标签是 `retrieval_miss`

也就是：

**当前主问题不是“不会写”，而是“证据不够全、不够硬”**

---

## 4. 我们一开始判断“哪些指标不好”

基于基线复盘，第一轮真正不好的指标是：

### 4.1 `retrieval_miss`

这是结果层最重要的失败标签。

它说明的不是“写差了”，而是：

- 关键证据没找到
- 或找到了但抓不下来
- 或抓下来了但不够支撑结论

### 4.2 `blocked_source`

第一轮 6 题里，`blocked_source = 6/6`，这个非常刺眼。

它告诉我们：

- 很多页面不是没搜到
- 而是搜到了、却拿不到有效正文

### 4.3 `insight` 与 `comprehensiveness`

这两个维度先后暴露了两种问题：

- `insight` 低
  - 说明报告更像总结，不像分析
- `comprehensiveness` 低
  - 说明证据覆盖不完整，关键点没拿全

### 4.4 评测链本身不稳定

这不算业务指标，但也是第一轮必须先修的东西：

- 本地 judge schema 不稳
- mixed judge mode 污染结果
- `Analysis` 的嵌套 section 没被正确统计

如果不修这一层，我们会持续被假问题误导。

---

## 5. 第一轮优化是怎么做的

整轮优化不是一次完成的，而是分阶段收敛出来的。

### 阶段 A：先修评测链，避免被假低分带偏

主要修了：

1. 正式 pilot 前增加 judge preflight
2. 不再允许正式结果静默混入 `heuristic_fallback`
3. 解析逻辑不再把坏 JSON 直接压成 `1.0`
4. `Analysis` 审计器修复嵌套小节解析

这一阶段的意义不是提高系统质量，而是先确保“看到的问题是真的”。

### 阶段 B：把检索链收敛成 Evidence Acquisition

这是第一轮最关键的架构调整。

我们把原来散在 `graph` 里的搜索/抓取逻辑，收成了 4 层：

1. `retrieval`
2. `qualification`
3. `fetch_pipeline`
4. `evidence_gate`

目标是让系统从：

- “搜到什么先抓什么”

变成：

- “先判断哪些来源值得信”
- “按页面类型决定抓取路径”
- “证据不够时先补，不够就不写”

### 阶段 C：把 authority-first 从排序升级成准入

一开始我们只是做“排序优化”，后面发现不够。

真正有效的是：

- 高权威来源优先进入主证据集合
- 弱来源默认不进主证据链
- strict topic 下必须优先官方/高权威来源

这一步不是为了“多搜”，而是为了：

- 少抓错
- 少在弱来源上浪费抓取和生成成本

### 阶段 D：把 gate 从提示型改成阻断型

最开始的 gate 更像警告灯：

- 发现 coverage 不够
- 记一条日志
- 但还是继续写报告

后来我们把它改成真正的 hard gate：

- evidence slot 不够，不让 writer 启动
- 先走 targeted backfill
- benchmark 下仍不够就标 `retrieval_failed`

这一步非常关键，因为它让系统开始“承认自己证据不足”，而不是继续输出伪完整报告。

### 阶段 E：专项分析 blocked，定位主失败层

当 Evidence Acquisition 跑起来之后，结果又暴露了一个更具体的问题：

运行：

- `drb-pilot-20260313_232745`

结果：

- `avg_drb_report_score = 7.0553`
- `avg_fact_score = 8.4`
- `success_rate = 0.0`
- `blocked_source_rate = 0.3232`
- `retrieval_failed = 3/3`
- `blocked_by_provider = {'pdf_parser': 13, 'visual_browse': 3}`
- `blocked_by_page_type = {'pdf': 13, 'js_heavy': 3}`

这一步让我们第一次非常明确地看到：

**当前主失败层不是 Google recall，不是 writer，而是 PDF 类高价值来源的可达性和可解析性。**

### 阶段 F：专项修 PDF blocked

针对 PDF，我们做了 host policy 和 fallback 限制：

- `403` 型 PDF 不再重复打原始 PDF
- `pdf_unreadable` host 只允许一次替代页回补
- 裸对象存储 PDF 直接回查上游来源站
- 同题内对失败 host 做 quarantine，避免反复撞墙

修完后的 `3` 题结果：

运行：

- `drb-pilot-20260314_001705`

变化：

- `blocked_source_rate: 0.3232 -> 0.1667`
- `retrieval_failed: 3/3 -> 2/3`
- `blocked_by_provider` 从 PDF 主导变成：
  - `{'jina': 1, 'direct_http': 1}`
- `blocked_by_host` 只剩：
  - `www.wshblaw.com: 2`

这一步说明：

**PDF blocked 专项本身是修成了的。**

### 阶段 G：修非 PDF blocked + writer 稳定性

PDF 不是主问题后，新的主问题变成：

- 非 PDF blocked host
- writer 子图偶发连接失败，把错误字符串直接写进正文

这一轮主要做了：

- same-host access backfill
- `section_writer` 连接类错误重试
- 重试后改为 deterministic fallback section

运行：

- `drb-pilot-20260314_193708`

结果：

- `avg_drb_report_score = 7.0553`
- `weak_source_hit_rate: 0.6384 -> 0.2484`
- `blocked_by_host` 新主导变成：
  - `arxiv.org`
- 报告里不再出现 `Section writing failed: ...`

这一步说明：

- writer 稳定性问题被压住了
- 但新的 host 级 blocked 问题又暴露出来

### 阶段 H：修 `arxiv.org` 可达性与 `direct_answer_support_rate`

最后一轮，我们锁定了两个明确问题：

1. `arxiv.org` 这类学术 HTML 页会先在 `direct_http` 命中 `js_only`
2. `direct_answer_support_rate` 既有真实支持不足，也有统计口径偏保守的问题

因此做了：

- `arxiv.org`、`doi.org`、`aaafoundation.org` 改成 extract-first
- strict topic authority boost
- `blocked_source_rate` 改成“最终未救回 URL 比例”
- `blocked_attempt_rate` 单独作为诊断口径
- `direct_answer_support_rate` 改成只对有效 coverage 样本求平均

运行：

- `drb-pilot-20260314_220109`

结果：

- `blocked_source_rate: 0.1667 -> 0.0`
- `blocked_attempt_rate = 0.0`
- `blocked_by_host` 不再出现 `arxiv.org`
- `direct_answer_support_rate: 0.4 -> 0.8333`
- `coverage_metric_missing_count = 0`
- `retrieval_failed = 1/3`
- `success_rate: 0.3333 -> 0.6667`
- `avg_drb_report_score = 7.0553`
- `avg_fact_score = 8.4`

这一步意味着：

**第一轮 blocked/access 支线问题基本收敛完成。**

---

## 6. 上次修复为什么没有成功

这个问题后来其实已经看得很清楚了。

上次修复没成功，不是方向完全错了，而是：

### 6.1 修得太靠后

我们最开始先动了：

- `Direct Answer`
- writer 结构
- draft audit

但真正的主问题其实在更上游：

- 来源质量
- 页面可达性
- 证据覆盖

所以前面证据不够时，后面再怎么调 writer，也只是“在不完整证据上写得更像报告”。

### 6.2 gate 太软

最开始的 evidence gate 失败后并不会真正阻断 writer。

所以系统会出现一种很危险的状态：

- 自己已经知道证据不够
- 但仍继续生成报告

这会导致：

- 报告形式上像成功
- 质量上其实是伪完整

### 6.3 authority-first 只是排序，不是准入

早期我们以为把高权威来源排前面就够了，后来发现不够。

因为只要弱来源仍然能顺着主链路流进来：

- 抓取成本就还会浪费
- 证据噪声就还是会污染 writer

真正有效的是：

- 把 authority-first 从“排序”升级成“准入”

### 6.4 评测链把问题放大了

包括：

- judge schema 缺失时被压成 `1.0`
- mixed judge mode
- `Analysis` 嵌套 section 未被统计

所以一部分“分很低”的信号，其实不是系统本身那么差，而是观测器先坏了。

---

## 7. 这轮优化最终结果怎么样

### 7.1 已经明显改善的部分

第一轮优化完成后，已经明确改善的指标有：

- `drb_report_score`
  - 从可信基线 `6.2274` 提升到 `7.0553`
- `instruction_following`
  - 从原始低分恢复到稳定 `6.7+`
- `blocked_source_rate`
  - 从 `0.3232` 压到 `0.0`
- `direct_answer_support_rate`
  - 从 `0.4` 拉到 `0.8333`
- `retrieval_failed`
  - 从 `3/3` 压到 `1/3`

### 7.2 第一轮已经完成了什么

可以认为已经完成的专项包括：

1. 评分链完整性修复
2. Evidence Acquisition 架构化
3. PDF blocked 专项
4. 非 PDF blocked 与 same-host backfill
5. `arxiv` / 学术 HTML host 可达性
6. `direct_answer_support_rate` 统计口径修复

### 7.3 还没完全解决的瓶颈

到第一轮结束时，剩余主瓶颈已经很清楚了：

- `authority_source_rate` 仍不算高
- `weak_source_hit_rate` 仍偏高
- 还有 `1/3` 的任务在 high-authority evidence slot 上不够完整

也就是说：

**现在系统的主问题已经不再是 blocked，也不再是 writer 报错，而是“高权威证据覆盖仍不够完整”。**

---

## 8. 我们在过程中遇到了哪些典型问题，又是怎么解的

### 问题 1：本地 judge 把四维分打成全 1 分

原因：

- 模型输出缺字段
- 解析逻辑把坏输出压成 `1.0`

解决：

- 强化 judge preflight
- 修 schema 校验
- 缺字段不再偷偷记分
- 对旧报告离线重评分

### 问题 2：`Analysis` 明明有内容，指标却显示 `0`

原因：

- 解析器只看到了 `### Analysis`
- 没把 `#### Comparative / Causal / Risk` 子段统计进去

解决：

- 修嵌套 section 解析
- 审计和评分都改成读取子段内容

### 问题 3：系统一直在和同一个 PDF 死磕

原因：

- 缺少 host 级策略
- 失败后仍反复打原始 PDF

解决：

- host policy
- quarantine
- landing / metadata / repository page 替代页策略

### 问题 4：writer 掉线时把错误字符串直接写进正文

原因：

- `section_writer` 没有合适的连接错误兜底

解决：

- 短退避重试
- deterministic fallback section
- 不再把原始错误字符串暴露给最终报告

### 问题 5：`arxiv` 这种 URL 被“先 blocked 后救回”，却仍污染主 blocked 指标

原因：

- blocked 统计口径把 provider 级失败和最终 URL 失败混在了一起

解决：

- 新增双口径：
  - `blocked_source_rate`
  - `blocked_attempt_rate`

---

## 9. 第一轮优化的结论

如果用一句话总结这轮优化：

**我们把问题从“报告质量看起来不稳定”一步步定位到了“Evidence Acquisition 质量不够”，然后把 blocked、writer 稳定性和 direct-answer 支撑这些第一层瓶颈基本打掉了。**

这轮最重要的变化不是“多了几个 patch”，而是：

- 指标开始可信
- 失败原因开始可解释
- 检索链开始被模块化管理
- 系统不再在证据不足时伪装成完整成功

第一轮结束后，可以比较明确地说：

- 默认 `medium` 模式的主链路已经完成第一轮质量修复
- 当前剩余的主要矛盾已经收敛到：
  - 高权威 evidence slot 覆盖率
  - `authority_source_rate`
  - `weak_source_hit_rate`

也就是说，第二轮优化不该再回去大修 writer，而是继续打：

- 高权威证据召回
- 高权威证据覆盖
- 更少的弱来源依赖

---

## 10. 关键运行节点速览

| 阶段 | Run | 核心结果 | 说明 |
| --- | --- | --- | --- |
| 初始 6 题基线 | `20260312_175533` | `avg_drb=6.209` | `blocked_source=6/6`，最早暴露 retrieval 问题 |
| 本地 judge 异常 | `20260313_012418` | `avg_drb=3.7294` | 假低分，后续被证伪 |
| 6 题重评分基线 | `20260313_012418-rescored` | `avg_drb=6.2274` | 第一份可信基线 |
| 第一轮 3 题验证 | `20260313_193204` | `avg_drb=7.0833` | writer/结构已有改善，但成功率不够 |
| blocked 专项分析 | `20260313_232745` | `blocked=0.3232`，`retrieval_failed=3/3` | 明确定位 PDF blocked 是主失败层 |
| PDF blocked 修复 | `20260314_001705` | `blocked=0.1667`，`retrieval_failed=2/3` | PDF 专项修成 |
| 非 PDF + writer 稳定性 | `20260314_193708` | `avg_drb=7.0553` | 把 writer 报错和非 PDF blocked 压住一部分 |
| `arxiv` + support 修复 | `20260314_220109` | `blocked=0.0`，`direct_answer_support=0.8333`，`retrieval_failed=1/3` | 第一轮 blocked/access 修复收口完成 |

---

## 11. 这份复盘对应的关键产物

- 6 题初始 pilot：
  - `reports/deepresearch_bench/drb-pilot-20260312_175533/`
- 6 题重评分：
  - `reports/deepresearch_bench/drb-pilot-20260313_012418-rescored/`
- 3 题 blocked 分析：
  - `reports/deepresearch_bench/drb-pilot-20260313_232745/`
- 3 题 PDF blocked 修复后：
  - `reports/deepresearch_bench/drb-pilot-20260314_001705/`
- 3 题 `arxiv` / support 修复后：
  - `reports/deepresearch_bench/drb-pilot-20260314_220109/`
- 阶段纪要：
  - `docs/benchmarks/2026-03-14_non_pdf_blocked_round.md`
