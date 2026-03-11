# ADR: Checkpoint Recovery Must Include KM Snapshot

日期：`2026-03-11`

## 决策

从这一轮开始，项目对外宣称的“可恢复执行”不再只依赖 LangGraph checkpoint。

恢复合同固定为：
1. 持久化任务状态
2. 持久化最近 checkpoint 元信息
3. 持久化最近 `KnowledgeManager` snapshot
4. 用相同 `task_id/thread_id` 从最近 checkpoint 继续执行

## 原因

此前只有图状态进入 checkpoint，而检索得到的事实块仍停留在内存里的 `KnowledgeManager`。

这会导致一个问题：
- 图可以恢复
- 但检索证据可能丢失
- 恢复后仍然需要重跑检索，无法算作真正的断点续跑

因此，只有 `checkpoint + KM snapshot` 同时存在时，恢复能力才成立。

## 落地约束

- `gateway/state_store.py` 新增 `knowledge_snapshots` 表
- `gateway/executor.py` 在每个安全点同步刷新 KM snapshot
- `POST /research/{task_id}/resume` 只从最近安全点恢复
- `writer` 子图也必须接 checkpoint，才能覆盖 `editor` 前恢复点

## 结果

后续所有“恢复成功率”指标都以这套合同为准，不再接受“只有 checkpoint、没有证据快照”的口径。
