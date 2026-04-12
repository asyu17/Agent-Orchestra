# agent_orchestra 数据契约与最终落盘

## 1. 一句话结论

当前 `agent_orchestra` 在 `Spec DAG / task / blackboard` 和 `mailbox / protocol` 这两条线上已经有了比较清楚的数据边界，但在 `worker -> leader loop -> superleader -> self-hosting round report` 这条导出链上，live runtime object 和最终 artifact object 仍然混在一起；后续要稳定落盘，必须把这条链重构成显式的四层模型：`Domain Live Model`、`Store Snapshot Model`、`Transport / Protocol Model`、`Artifact / Report Model`。

## 2. 范围与资料来源

- 当前问题的直接代码入口：
  - [bootstrap.py#L525](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/self_hosting/bootstrap.py#L525)
  - [bootstrap.py#L551](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/self_hosting/bootstrap.py#L551)
  - [bootstrap.py#L860](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/self_hosting/bootstrap.py#L860)
  - [superleader.py#L97](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/superleader.py#L97)
  - [leader_loop.py#L151](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py#L151)
  - [execution.py#L200](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/contracts/execution.py#L200)
  - [execution.py#L217](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/contracts/execution.py#L217)
- 正面对照样本：
  - [mailbox.py#L27](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/tools/mailbox.py#L27)
  - [mailbox.py#L52](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/tools/mailbox.py#L52)
  - [mailbox.py#L76](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/tools/mailbox.py#L76)
  - [protocol_bridge.py#L61](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/protocol_bridge.py#L61)
  - [protocol_bridge.py#L114](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/protocol_bridge.py#L114)
  - [protocol_bridge.py#L175](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/protocol_bridge.py#L175)
  - [protocol_bridge.py#L214](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/protocol_bridge.py#L214)
- 关联测试与设计：
  - [test_self_hosting_exports.py#L1](/Volumes/disk1/Document/code/Agent-Orchestra/tests/test_self_hosting_exports.py#L1)
  - [test_self_hosting_round.py#L1](/Volumes/disk1/Document/code/Agent-Orchestra/tests/test_self_hosting_round.py#L1)
  - [2026-04-04-agent-orchestra-data-contracts-and-finalization-design.md](/Volumes/disk1/Document/code/Agent-Orchestra/docs/superpowers/specs/2026-04-04-agent-orchestra-data-contracts-and-finalization-design.md)
- 相关知识：
  - [self-hosting-bootstrap.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/self-hosting-bootstrap.md)
  - [implementation-status.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/implementation-status.md)

## 3. 为什么这不是单点 serializer bug

最直接的证据是同一文件里已经同时存在两种完全不同的导出风格：

- `SelfHostingInstructionPacket` 在 [bootstrap.py#L525](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/self_hosting/bootstrap.py#L525) 已经是标准 artifact 型对象，自带 `to_dict()`，字段也都是 JSON-safe。
- `SelfHostingRoundReport` 在 [bootstrap.py#L551](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/self_hosting/bootstrap.py#L551) 仍然把 `run_result: SuperLeaderRunResult` 直接放进去。

而 `SuperLeaderRunResult` 本身又继续持有：

- `LeaderLoopResult`：[superleader.py#L97](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/superleader.py#L97)
- `LeaderTurnRecord / LeaderLoopResult`：[leader_loop.py#L151](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py#L151)
- `WorkerRecord`：[execution.py#L217](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/contracts/execution.py#L217)
- `WorkerSession`：[execution.py#L200](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/contracts/execution.py#L200)

问题不在于某一次 `json.dumps(...)` 恰好失败，而在于现在的“最终 round 产物”仍然依赖 runtime-rich object graph。只要继续这么走，后面就还会反复遇到：

- handle 不能安全序列化
- session / backend metadata 形状不稳定
- `dataclass` 递归导出时穿透到不该穿透的 live object
- instruction packet 能落盘，但 final round report 不能稳定落盘

判断：

- 这是层级之间的数据契约没有完全做完，而不是简单的“把某个字段过滤掉”就彻底解决。

## 4. 现在哪条链是清楚的，哪条链还是混的

### 4.1 已经相对清楚的链

- `Spec DAG / task / blackboard`
  - 任务、节点、证据面的职责边界已经比较清楚，适合继续扩展
- `mailbox / subscription / protocol bridge`
  - `MailboxEnvelope / MailboxCursor / MailboxSubscription / MailboxDigest` 在 [mailbox.py#L27](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/tools/mailbox.py#L27) 开始定义
  - `protocol_bridge.py` 明确做了 normalize / coerce / deserialize，而不是偷懒地直接递归导出 runtime object

### 4.2 还没彻底清楚的链

- `WorkerRecord -> LeaderLoopResult -> SuperLeaderRunResult -> SelfHostingRoundReport`

这条链当前同时承担了两种职责：

- 运行中的 orchestration state
- 最终要写盘的 durable artifact

这两种职责混在一个对象图里，就是当前不稳定的根因。

## 5. 推荐的四层数据模型

### 5.1 Domain Live Model

用途：

- 进程内运行时协调
- backend-aware 执行
- leader / teammate / supervisor 的实时状态流转

当前代表对象：

- [WorkerRecord](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/contracts/execution.py#L217)
- [LeaderLoopResult](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py#L163)
- [SuperLeaderRunResult](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/superleader.py#L97)

规则：

- 可以 rich
- 可以 runtime-aware
- 但不能直接当最终 artifact 写盘

### 5.2 Store Snapshot Model

用途：

- orchestration store 的持久化
- 进程恢复后的再构造
- durable state，但仍偏运行控制面

规则：

- 必须去 live handle 化
- 必须只保留 durable metadata

### 5.3 Transport / Protocol Model

用途：

- mailbox、permission、subscription、reconnect cursor

当前代表对象：

- [MailboxEnvelope](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/tools/mailbox.py#L27)
- [MailboxCursor](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/tools/mailbox.py#L52)
- [MailboxSubscription](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/tools/mailbox.py#L60)
- [MailboxDigest](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/tools/mailbox.py#L76)

规则：

- 显式 normalize
- 显式 deserialize
- payload / metadata 必须 JSON-safe

### 5.4 Artifact / Report Model

用途：

- round report
- self-hosting final artifact
- handoff artifact
- 诊断与复盘产物

当前正面样本：

- [SelfHostingInstructionPacket](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/self_hosting/bootstrap.py#L525)

下一步应该补的对象：

- `WorkerRecordSnapshot`
- `LeaderTurnArtifact`
- `LeaderLoopArtifact`
- `SuperLeaderRunArtifact`
- `SelfHostingRoundArtifact`

## 6. 必须收紧的边界规则

后续只要跨下面这些边界，就必须坚持 JSON-safe 规则：

- runtime -> store
- runtime -> mailbox payload
- runtime -> artifact
- artifact -> disk

允许的值类型：

- `str`
- `int`
- `float`
- `bool`
- `None`
- JSON-safe list
- JSON-safe dict

不允许直接跨边界的内容：

- live handle
- lock
- subprocess / thread object
- runtime-bound session object
- 没有显式 mapper 的 dataclass graph

对于“人会直接打开查看”的 JSON artifact 或 spool 文件，还要额外满足一条落盘约束：

- 文件编码使用 `utf-8`
- JSON 序列化使用 `ensure_ascii=False`

原因是：

- 仅仅 `encoding="utf-8"` 还不够；如果 `json.dumps(...)` 保持默认 `ensure_ascii=True`，中文仍会被写成 `\uXXXX`
- 这不会破坏机器反序列化，但会直接降低 run artifact、instruction packet、worker spool result 这类调试文件的可读性
- 因此 user-facing artifact 与 machine-only transport 要区分：前者优先可读性，后者可以继续采用更保守的 wire/storage JSON 约束

这条规则本质上是在说：

- `metadata` 和 `payload` 也不能再当“兜底垃圾桶”乱塞对象，它们同样必须遵守 JSON-safe 契约。

## 7. 建议增加的最终落盘状态机

目前的 self-hosting round 更像是：

- 先跑完
- 再尝试顺手写几个文件

更稳的方式应该是显式 finalization state machine：

1. `run_started`
2. `planning_materialized`
3. `lane_execution_completed`
4. `objective_evaluated`
5. `instruction_packet_materialized`
6. `run_artifact_materialized`
7. `round_artifact_materialized`
8. `artifacts_written`
9. `finalized`
10. `finalization_failed`

这样以后再出现“instruction packet 已写出，但 final JSON 缺失”时，系统不会只留下一个模糊现象，而会明确告诉你：

- 卡在第几步
- 哪个 artifact 已生成
- 哪个 artifact 未生成
- 错误摘要是什么

## 8. 最值得优先补的实现点

### 8.1 先做 snapshot mapper

最先要补的是 runtime object 到 artifact object 的显式转换器，例如：

- `snapshot_worker_record(...)`
- `snapshot_leader_loop_result(...)`
- `snapshot_superleader_run_result(...)`
- `snapshot_self_hosting_round(...)`

### 8.2 再做 final artifact writer

最终 round JSON 应由新的 `SelfHostingRoundArtifact` 写出，而不是对 runtime-rich `SelfHostingRoundReport` 做递归导出。

### 8.3 测试要锁住哪几类回归

- artifact `to_dict()` 产物全量 JSON-safe
- `from_dict()` round-trip
- “instruction packet 已写出但 final round artifact 缺失”回归
- 不允许 export path 直接递归碰到 `WorkerRecord.handle`

## 9. 对现有 gap 语义的修正

知识层之前把这个问题比较像是记成：

- `final round artifact export`

这个描述不算错，但还不够深。

更准确的说法应该是：

- `final round artifact export` 的直接表象
- 根因是 `data contracts and finalization` 这一层还没有完全标准化

所以后续不应该再把它当成一个孤立的 exporter 小修，而应该把它视为：

- self-hosting / runtime / export 交界面的系统性契约补完

## 10. 后续阅读

- [worker-lifecycle-protocol-and-lease.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/worker-lifecycle-protocol-and-lease.md)
- [self-hosting-bootstrap.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/self-hosting-bootstrap.md)
- [agent-orchestra-gap-handoff.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/agent-orchestra-gap-handoff.md)
- [implementation-status.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/implementation-status.md)
- [2026-04-04-agent-orchestra-data-contracts-and-finalization-design.md](/Volumes/disk1/Document/code/Agent-Orchestra/docs/superpowers/specs/2026-04-04-agent-orchestra-data-contracts-and-finalization-design.md)
