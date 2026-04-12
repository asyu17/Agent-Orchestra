# agent_orchestra Autonomous Runtime

## 1. 一句话结论

截至 2026 年 4 月 3 日，`agent_orchestra` 已经不再只有“单轮 bridge + 手动驱动”能力了；它现在有了一条可测试的自治协调层：`DeliveryState`、`MailboxEnvelope / MailboxCursor / MailboxBridge`、`PermissionRequest / PermissionBroker`、`DefaultDeliveryEvaluator`、`LeaderLoopSupervisor` 和 `SuperLeaderRuntime` 已经把 `LeaderTaskCard -> LeaderRound -> leader turn -> teammate turn -> objective aggregate` 串成了真正的多轮 lane loop 与多 lane objective loop。

## 2. 范围与资料来源

- 当前代码：
  - [delivery.py#L8](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/contracts/delivery.py#L8)
  - [mailbox.py#L14](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/tools/mailbox.py#L14)
  - [permission_protocol.py#L8](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/tools/permission_protocol.py#L8)
  - [evaluator.py#L14](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/evaluator.py#L14)
  - [leader_loop.py#L77](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py#L77)
  - [leader_loop.py#L144](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py#L144)
  - [superleader.py#L21](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/superleader.py#L21)
  - [superleader.py#L38](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/superleader.py#L38)
  - [base.py#L14](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/storage/base.py#L14)
  - [in_memory.py#L13](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/storage/in_memory.py#L13)
- 当前测试：
  - [test_delivery_contracts.py#L1](/Volumes/disk1/Document/code/Agent-Orchestra/tests/test_delivery_contracts.py#L1)
  - [test_mailbox_bridge.py#L1](/Volumes/disk1/Document/code/Agent-Orchestra/tests/test_mailbox_bridge.py#L1)
  - [test_evaluator.py#L1](/Volumes/disk1/Document/code/Agent-Orchestra/tests/test_evaluator.py#L1)
  - [test_leader_loop.py#L1](/Volumes/disk1/Document/code/Agent-Orchestra/tests/test_leader_loop.py#L1)
  - [test_superleader_runtime.py#L1](/Volumes/disk1/Document/code/Agent-Orchestra/tests/test_superleader_runtime.py#L1)
- 关联设计：
  - `docs/superpowers/specs/2026-04-03-agent-orchestra-autonomous-runtime-api.md`
  - `docs/superpowers/plans/2026-04-03-agent-orchestra-autonomous-runtime.md`

## 3. 新增抽象

### 3.1 `DeliveryState` 是自治 loop 的派生状态

新的 delivery 契约在 [delivery.py#L8](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/contracts/delivery.py#L8) 定义：

- `DeliveryStateKind`
- `DeliveryStatus`
- `DeliveryDecision`
- `DeliveryState`

这里的重点不是“再加一个状态对象”，而是把 runtime 自己对 lane / objective 进度的判断显式持久化下来。

这意味着现在已经有一个独立于 raw evidence 的派生状态层：

- raw evidence 仍在 task / blackboard / worker record / mailbox
- 但 lane 是否继续、是否完成、是否阻塞，已经不再只是调用方脑补

### 3.2 mailbox 和 permission 已经从“未来概念”变成接口

mailbox 与 permission 现在已经有稳定契约：

- `MailboxEnvelope / MailboxCursor / MailboxBridge`: [mailbox.py#L21](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/tools/mailbox.py#L21), [mailbox.py#L38](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/tools/mailbox.py#L38), [mailbox.py#L45](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/tools/mailbox.py#L45)
- `PermissionRequest / PermissionDecision / PermissionBroker / StaticPermissionBroker`: [permission_protocol.py#L8](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/tools/permission_protocol.py#L8), [permission_protocol.py#L23](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/tools/permission_protocol.py#L23), [permission_protocol.py#L31](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/tools/permission_protocol.py#L31)

当前具体实现是 `InMemoryMailboxBridge`，入口在 [mailbox_bridge.py#L4](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/mailbox_bridge.py#L4)。

判断：

- 这还不是完整生产 control plane
- 但已经不再只是 `RedisEventBus` 那种“只有 publish” 的薄 transport

### 3.3 `DefaultDeliveryEvaluator` 负责把证据收敛成决策

`DefaultDeliveryEvaluator` 在 [evaluator.py#L48](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/evaluator.py#L48)。

它当前做两件事：

1. `evaluate_lane(...)`
2. `evaluate_objective(...)`

lane 级判定会看：

- team-scope task 状态: [evaluator.py#L63](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/evaluator.py#L63)
- team blackboard blocker / proposal snapshot: [evaluator.py#L78](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/evaluator.py#L78)
- 是否还有待 leader 消费的 teammate mailbox 结果: [evaluator.py#L122](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/evaluator.py#L122)

这意味着当前系统已经第一次具备了“runtime 自己判断是否继续下一轮”的能力，而不是永远由外部脚本决定。

## 4. 关键执行链

当前自治协调链路是：

1. `materialize_planning_result(...)` 把 plan 物化成 `LeaderRound`: [bootstrap_round.py#L152](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/bootstrap_round.py#L152)
2. `LeaderLoopSupervisor.run(...)` 从已有 `DeliveryState` 或 turn 1 开始推进 lane: [leader_loop.py#L358](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py#L358)
3. `compile_leader_turn_assignment(...)` 把 visible tasks、blackboard snapshot 和 mailbox 输入拼到 leader turn 里: [leader_loop.py#L109](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py#L109)
4. leader 输出继续走 `ingest_leader_turn_output(...)`，落成 team task 和 teammate assignment: [leader_output_protocol.py#L158](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_output_protocol.py#L158)
5. `LeaderLoopSupervisor._run_teammates(...)` 执行 teammate assignment，并把结果同时写回：
   - task status: [leader_loop.py#L305](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py#L305)
   - team blackboard: [leader_loop.py#L228](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py#L228)
   - leader mailbox: [leader_loop.py#L333](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py#L333)
6. `DefaultDeliveryEvaluator` 评估 lane 是否继续 / 完成 / 阻塞 / 失败: [leader_loop.py#L499](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py#L499)
7. `SuperLeaderRuntime.run_planning_result(...)` 负责对多个 lane 逐个跑 loop，并把结果聚合成 objective 级 state: [superleader.py#L52](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/superleader.py#L52), [superleader.py#L99](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/superleader.py#L99)

## 5. 当前已经解决的问题

这轮之后，系统已经不再欠缺下面这些“全空白”能力：

- `SuperLeader` runtime：已落地第一版 sequential 多 lane runtime
- 自动持续运行的 leader loop：已落地 bounded multi-turn lane loop
- mailbox 驱动 leader 下一轮输入：已落地 in-memory mailbox cursor / acknowledge 模型
- delivery state machine：已落地 lane/objective 级派生状态与 evaluator 决策
- permission hook：已落地统一 request/decision 接口，并接入 leader / teammate 执行前检查

## 6. 仍然没解决的问题

这不等于所有自治问题都做完了，当前仍有明确限制：

- `LeaderLoopSupervisor` 现在已经能在支持 `reactivate_supported` 的 transport 上保留 leader session 为 idle 并在下一轮 turn 上 reactivate；但这仍只覆盖当前 supervisor 进程内、当前已实现 backend 的第一版 lifecycle，不是 pane/process 级 crash reconnect
- reconnect 只到“runtime-level resume by delivery state + mailbox cursor”，还不是 pane/process 级 crash reconnect
- `SuperLeaderRuntime` 当前是顺序推进 lane，不是多 lane 并发调度器
- PostgreSQL 只把 `delivery_states` schema 预留出来了，还没有真实 CRUD
- Redis mailbox / protocol transport 仍主要停留在旧 `protocol_bridge` 的 compatibility 路径，没有把新自治 loop 的 mailbox 真正落到 Redis

## 7. 后续入口建议

如果后续 agent 要继续扩展这层，最值得优先进入的源码入口是：

1. [leader_loop.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py)
2. [evaluator.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/evaluator.py)
3. [superleader.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/superleader.py)
4. [mailbox.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/tools/mailbox.py)
5. [permission_protocol.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/tools/permission_protocol.py)
