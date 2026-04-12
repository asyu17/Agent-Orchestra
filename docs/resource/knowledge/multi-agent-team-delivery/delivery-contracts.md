# 多 Team 交付契约

## 1. 一句话结论

多 team 系统的最小正确性不在于起了多少 agent，而在于产出是否被 authority root 吸收、验证并绑定到真正的完成语义。

## 2. 范围与资料来源

- 来源：针对多 team 运行复盘抽象出的交付契约模型
- 关注范围：完成语义、TaskCard、HandoffRecord、Reducer、Completion Gate、Verification
- 关联文档：`README.md`、`failure-patterns.md`、`runbook-and-checklists.md`

## 3. 设计原则

多 team 系统的最小正确性不在于“起了多少 agent”，而在于：

- 输出是否落成结构化 artifact
- artifact 是否被 authority root 消费
- authority root 是否通过统一验证

所以交付契约必须围绕 `authority root` 组织，而不是围绕 worker 本地状态组织。

## 4. 四层完成语义

### 4.1 Worker Complete

定义：

- worker 已完成自己负责的局部实现或验证
- 本地 worktree 中存在 commit 或明确“无改动但已验证”的结论

必要条件：

- task result 已写入
- 本地 verification 已执行
- leader 可读取 worker 结果

禁止误解：

- `worker_complete` 不能等价于 team 完成

### 4.2 Team Complete

定义：

- team 内部所有 worker task 已闭合
- team leader 已汇总并产出 team-level artifact

必要条件：

- 所有 worker task 为 `completed`
- team summary artifact 已写出
- 若存在向外部 lane 的交付，则 `handoff_record` 已落地

禁止误解：

- `team_complete` 不能等价于 authority root 完成

### 4.3 Authority Complete

定义：

- team 的有效产出已被 reducer / integrator 吸收到 authority root
- authority root 上的对应验证已通过

必要条件：

- 有可追踪的 merge / cherry-pick 记录
- authority root 上存在对应文件改动
- authority root verification command 通过
- 结果 summary 已更新

这是最关键的一层完成语义。

### 4.4 Objective Complete

定义：

- 本轮目标对应的端到端结果已经满足验收标准

必要条件：

- 所有必须 lane 的 `authority_complete` 为真
- 最终 run summary 达到预期
- 无阻塞级 failure

## 5. TaskCard 最小契约

建议统一字段：

```json
{
  "task_id": "T-C-001",
  "goal": "Improve knowledge bundle and history discovery",
  "lane": "track-c",
  "owned_paths": [
    "core/knowledge_bundle.py",
    "core/metadata_sync.py",
    "core/template_seed.py",
    "core/pipeline_nodes/history_nodes.py"
  ],
  "allowed_inputs": [
    "summary/runtime_preparation.json",
    "coordination/registry/team_registry.json"
  ],
  "output_artifacts": [
    "runs/.../history/canonical_history.jsonl",
    "runs/.../templates/history_candidates.jsonl",
    "runs/.../knowledge/knowledge_bundle.json"
  ],
  "verification_commands": [
    "python3 -m unittest tests.test_knowledge_metadata tests.test_history_discovery -v"
  ],
  "handoff_to": [
    "track-d",
    "reducer"
  ],
  "merge_target": "authority-root",
  "done_when": [
    "leader root contains accepted commit",
    "authority root verification passes",
    "handoff_record written"
  ]
}
```

重点不是字段多，而是这些字段必须进入自动状态机，而不是只存在文档里。

## 6. HandoffRecord 最小契约

跨 lane 交付不能只靠自然语言消息。

最小结构应包含：

```json
{
  "schema_version": "agent-handoff/v1",
  "handoff_id": "handoff-track-c-001",
  "from_lane": "track-c",
  "to_lane": "track-d",
  "authority_ref": "feature/authority-root@<sha>",
  "accepted_commit_ids": [
    "abc123"
  ],
  "artifacts": [
    "runs/.../knowledge/knowledge_bundle.json",
    "runs/.../history/canonical_history.jsonl"
  ],
  "contract_assertions": [
    "history canonicalization passes",
    "template seed snapshot present"
  ],
  "verification_summary": {
    "status": "passed",
    "commands": [
      "python3 -m unittest tests.test_knowledge_metadata tests.test_history_discovery -v"
    ]
  }
}
```

没有 handoff，就不应认为跨 lane 交付已经发生。

## 7. Reducer / Integrator 契约

Reducer 必须是独立角色，而不是“某个 leader 顺手做一下”。

它的职责是：

1. 收集 team-level 结果
2. 识别哪些 commit 值得集成
3. 决定集成顺序
4. 在 authority root 进行 merge / cherry-pick
5. 运行统一验证
6. 产出 `integration_report`
7. 更新 `authority_complete`

建议 reducer 至少写出：

- `accepted_changes.json`
- `rejected_changes.json`
- `integration_report.json`
- `authority_verification_report.json`

## 8. Completion Gate

一个 lane 只有满足下面所有条件，才能算真正完成：

### Team gate

- worker tasks 全部完成
- leader summary 已生成
- 无未处理 dispatch error

### Integration gate

- reducer 已接收
- authority root 已合并
- authority verification 通过

### Objective gate

- 被下游消费成功
- run summary 更新成功

## 9. Verification 契约

验证必须分三层：

1. `local verification`
   - worker 本地或 team root
2. `authority verification`
   - authority root 上统一执行
3. `objective verification`
   - 针对最终 run / pipeline 的端到端检验

只有第一层通过，不足以宣称目标推进。

## 10. Ownership 与 Testing 的关系

推荐规则：

- 实现 lane 允许改最小必要测试
- verification lane 负责扩大覆盖和交叉验证
- 不要把 `tests/` 完整独占给 verification lane

否则会出现：

- 实现方只能写临时脚本
- 正式回归测试无法入库
- 结果验证与代码演化脱节

## 11. 通用约束

多 team 交付系统必须把以下三件事写成硬约束：

1. `没有 authority merge，不算完成`
2. `没有 handoff artifact，不算协作发生`
3. `没有 objective gate 通过，不算目标完成`

## 12. 相关文档

- `README.md`
- `failure-patterns.md`
- `runbook-and-checklists.md`
