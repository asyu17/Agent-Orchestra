# agent_orchestra 层级在线 Agent Runtime Gap 清单

## 1. 一句话结论

如果要把 `agent_orchestra` 正式推进到你要的目标状态，那么当前顶层工作不应再拆成零散的“再补一点 resident / 再补一点 mailbox / 再补一点 scheduler”，而应明确收敛成一条连续主线：`统一 Agent 契约 -> Team 内主语义切换 -> SuperLeader 同构化 -> 跨 Team 受控协作层`。这 4 段不是平行 backlog，而是前后有依赖关系的主线 gap。

## 2. 范围与资料来源

- 新 spec：
  - [2026-04-05-agent-orchestra-hierarchical-online-agent-runtime-design.md](/Volumes/disk1/Document/code/Agent-Orchestra/docs/superpowers/specs/2026-04-05-agent-orchestra-hierarchical-online-agent-runtime-design.md)
- 现有知识：
  - [online-collaboration-runtime-migration.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/online-collaboration-runtime-migration.md)
  - [resident-team-collaboration-and-slice-planning.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/resident-team-collaboration-and-slice-planning.md)
  - [resident-hierarchical-runtime.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/resident-hierarchical-runtime.md)
  - [agent-abstraction-skill-and-prompt-system.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/agent-abstraction-skill-and-prompt-system.md)
  - [parallel-team-coordination.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/parallel-team-coordination.md)
  - [implementation-status.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/implementation-status.md)
- 当前热点代码：
  - [team.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/contracts/team.py)
  - [execution.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/contracts/execution.py)
  - [task.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/contracts/task.py)
  - [session_host.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/session_host.py)
  - [transport_adapter.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/transport_adapter.py)
  - [teammate_runtime.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/teammate_runtime.py)
  - [teammate_online_loop.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/teammate_online_loop.py)
  - [leader_loop.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py)
  - [superleader.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/superleader.py)
  - [protocol_bridge.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/protocol_bridge.py)
  - [group_runtime.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/group_runtime.py)

## 3. 当前目标状态

本轮要收敛到的目标状态可以压缩成 4 句话：

1. `Teammate / Leader / SuperLeader` 共享同一套长期在线 agent 底座。
2. `task` 只负责 activation / contract，持续推进问题解决的是在线 agent 协作。
3. team 内协作尽量贴近 Claude team agent，但系统级继续保留分层收敛中心。
4. 跨 team 协作走受控 mailbox/subscription/circle，而不是打破 team 边界。

## 4. 当前进展

截至本轮实现，4 段主线都已经有了明确的第一段代码落点：

- `agent-contract-convergence`
  - `contracts/agent.py` 已显式增加 `Agent`、`AuthorityPolicy`
  - `AgentSession` 已扩到 `current_directive_ids / last_progress_at`
- `team-primary-semantics-switch`
  - `TeammateWorkSurface.run(...)` + per-slot `TeammateOnlineLoop` 已成为 host-owned teammate live path
  - teammate recipient cursor/session truth 已收口到 `TeammateWorkSurface + ResidentSessionHost`
- `superleader-isomorphic-runtime`
  - `SuperLeaderRuntime` cycle 已开始读取 objective shared digests，并可进入 `WAITING_FOR_MAILBOX`
  - ready lane 已通过 `LeaderLoopSupervisor.ensure_or_step_session(...)` 进入 host-owned `leader_session_host` launch boundary
- `cross-team-collaboration-layer`
  - `GroupRuntime` 已新增 runtime-owned 的 message subscription / full-text access / cross-scope directive gate

判断：

- 这说明 4 段主线已经都从“纯文档”进入“部分代码主线”
- 但每一段都还没有达到本文定义的完成态

## 5. 顶层 gap 排序

### 5.1 P0-1 `agent-contract-convergence`

这是整条主线的地基。

目标：

- 收敛统一 `Agent`
- 收敛统一 `AgentSession`
- 收敛统一 `AgentAction`
- 收敛统一 `RolePolicy / AuthorityPolicy / ClaimPolicy / PromptTriggerPolicy`

为什么优先：

- 现在 `Leader / SuperLeader / Teammate` 已经在 runtime 形状上越来越像，但契约层还没有真正统一
- 如果不先统一这层，后面的 team online loop、superleader digest loop、cross-team collaboration 都会继续在不同模型上叠加，最后变成语义分叉

当前缺口：

- [team.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/contracts/team.py) 里的 `AgentProfile` 还偏薄
- [execution.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/contracts/execution.py) 还没有统一承载长期 agent session/action/policy 主语义
- 当前 session truth 还更多偏 worker/coordinator，而不是广义 agent

新增进展：

- `Agent`、`AuthorityPolicy`、`AgentSession.current_directive_ids / last_progress_at` 已经进入主线
- 但 `WorkerSession / ResidentCoordinatorSession / AgentSession` 的唯一 owner 仍未完全收敛

建议子 gap：

1. `agent-contract-model`
2. `agent-session-model`
3. `agent-action-model`
4. `role-authority-claim-policy`
5. `prompt-trigger-policy`

验收标准：

- 同一套契约能完整描述 `Teammate / Leader / SuperLeader`
- prompt turn 被降级成 action，而不是 runtime 主语义
- authority / claim / visibility 规则不再主要写在 prompt 或局部 helper 里

### 5.2 P0-2 `team-primary-semantics-switch`

这是最接近 Claude team agent 形态的关键阶段。

目标：

- 把 team 内部真正切到：
  - `leader / teammate` 都是长期在线 agent
  - `mailbox + task list + TeamBlackboard` 持续驱动
  - `task` 只负责 activation

为什么优先：

- 当前系统虽然已经有 resident teammate、autonomous claim、mailbox-directed work、leader continuous convergence 的基础
- 但主求解模式仍偏 `leader turn -> runtime dispatch`
- 如果不先完成这一步，就算先做 superleader 在线化，底层 team 仍然是半在线、半派工的混合体

当前缺口：

- `teammate` 还不是完全 agent-owned 的长期 loop
- `leader` 还没有彻底从 assignment-first 切到 convergence-first
- team 内 `mailbox / task / blackboard` 的权威分工还没有正式固化成一套 runtime 主语义
- `authority-escalation-and-scope-extension` 的请求-等待 first-cut 已经进入主线：带显式 `authority_request` 的 blocker 会进入 `WAITING_FOR_AUTHORITY`，而不是再统一折叠成 `blocked -> failed`；当前残留缺口是 leader/superleader 的正式裁决面、protected boundary policy，以及 authority decision 之后的 resume path
- self-hosting 对 resident collaboration 的证据面还没完全切到 host-owned online loop；旧 delegation validation 仍保留 `leader_turn_count > 1` 的 follow-up 条件
- team budget 语义已经收敛到只保留 `max_teammates`
- leader 输出主协议已经切到显式 `sequential / parallel slices`

新增进展：

- `TeammateWorkSurface.run(...)` 已把 directed inbox/cursor/session truth 从 leader path 主线里抽走，leader mailbox helper 只再处理 leader inbox
- promptless continuation 已能在 activation 后继续推进 teammate work，而不必额外增加 fresh leader prompt turn
- 但 leader 仍显式承担 `_run_teammates(...)` activation shell，standalone teammate host 与 host-owned evidence rewrite 还没完成

建议子 gap：

1. `teammate-agent-owned-online-loop`
2. `leader-convergence-first-loop`
3. `task-to-activation-surface`
4. `authority-escalation-and-scope-extension`
5. `leader-slice-graph-output`
6. `team-budget-simplification`
7. `team-truth-surface-separation`
8. `team-quiescence-and-completion`

验收标准：

- 不需要每来一个新工作都重新触发 leader prompt
- teammate 能在空闲时继续 poll mailbox / claim task / publish result
- leader 能主要依靠 mailbox/task/blackboard 收敛 team
- teammate 遇到真实 blocker 时可以申请 authority / scope extension，而不是只能把问题折叠成终态失败
- team 不再依赖显式 `max_concurrency` 预算字段表达并发
- leader 输出能显式表达顺序 slice、并行 slice 及其依赖关系

### 5.3 P0-3 `superleader-isomorphic-runtime`

这是把上层正式切到同一底座的阶段。

目标：

- 把 `SuperLeader` 从“外层 while-loop 调度器”推进成真正的 `leader-of-leaders`
- 它和 `Leader` 共用同一套在线 agent 底座与 session 主语义
- 它的主要输入改成 digest、lane mailbox、objective blackboard，而不是反复 procedural 地拉 leader loop

为什么放在 Team 之后：

- 上层如果先同构，而下层 team 仍没有稳定的在线协作主语义，那么 superleader 只能收到不稳定、偏 assignment 的噪音
- 只有 team 内先稳定，superleader 才能真正吃 digest/convergence signal

当前缺口：

- [superleader.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/superleader.py) 已经 resident，但仍更像 scheduler
- 还没有完整的 lane digest-driven convergence 主链
- 还没有真正 formal 的 leader-of-leaders online session model

新增进展：

- cycle 内已经开始读取 objective shared digests
- “无 ready lane 但有 digests” 时可进入 `WAITING_FOR_MAILBOX`
- 但 digests 还没有进入更强的 objective rebalance / replan / topology 调整逻辑

建议子 gap：

1. `superleader-agent-session`
2. `superleader-digest-driven-convergence`
3. `leader-lane-mailbox-surface`
4. `objective-online-convergence`
5. `team-topology-adjustment`

验收标准：

- superleader 可以长期在线观察多个 lane/team，而不是每轮外部重新调度
- superleader 只在需要 team 数量调整、职责改写、重规划时触发 prompt turn
- superleader 不直接触碰 teammate

### 5.4 P1 `cross-team-collaboration-layer`

这是最后一段，因为它最容易把系统搞乱。

目标：

- 正式接上 subscription / circle / cross-team directed mailbox
- 明确默认可见性、摘要索引和全文引用规则
- 让跨 team 协作变成受控网络，而不是自由广播

为什么必须最后做：

- 如果 team 内在线协作还没稳定，先做跨 team 消息层只会放大噪音
- 如果 authority/scope 还没 formalize，跨 team 协作会直接击穿边界

当前缺口：

- 虽然消息池、subscription、visibility policy 主线已经有基础
- 但“跨 team 正式协作层”还没有以 team boundary 为前提重新定义默认行为
- 也还没有把 `summary index -> reference -> full text fetch` 变成正式默认

新增进展：

- `GroupRuntime` 已有 runtime-owned 的 subscription/full-text/directive gate
- 但 `circle` overlay 和正式的 cross-team directed mailbox API 仍未进入主线

建议子 gap：

1. `cross-team-subscription-model`
2. `circle-collaboration-model`
3. `summary-index-and-reference-window`
4. `cross-team-directed-mailbox`
5. `visibility-defaults-and-policy-gates`

验收标准：

- 同 team 协作仍然高密度
- 跨 team 默认只看到摘要索引
- 需要时可以按 policy 拉全文或订阅局部窗口
- 不会形成全局聊天室

## 6. 依赖关系

这 4 段不是并列关系，而是强依赖链：

1. `agent-contract-convergence`
2. `team-primary-semantics-switch`
3. `superleader-isomorphic-runtime`
4. `cross-team-collaboration-layer`

明确禁止的逆序：

- 不能跳过 1 直接做 2，否则语义会继续分叉
- 不能跳过 2 直接做 3，否则 superleader 只能站在旧派工模型上
- 不能跳过 1 和 2 提前做 4，否则消息面会先失控

## 7. 当前代码与 gap 的映射

### 7.1 已有基础，不要重复造轮子

下面这些能力已经进入主线，后续实现应直接复用，而不是推倒重写：

- `ResidentCoordinatorKernel` 共享 leader/superleader resident shell
- `ResidentSessionHost` 与 `TransportAdapter` 的薄分层
- `TeammateWorkSurface.run(...)` + per-slot `TeammateOnlineLoop` 的 host-owned teammate live path，以及 `ResidentTeammateRuntime` 保留下来的 acquisition/evidence reference
- 原子 task claim surface
- canonical directed mailbox protocol
- authoritative mailbox consumer cursor
- durable session / lease / reattach / protocol bus 主线

### 7.2 当前真正需要改的地方

真正需要推进的，是这些“主语义切换点”：

- 契约层从 worker/coordinator 视角切到统一 agent 视角
- teammate 从 leader-owned refill shell 切到 agent-owned online loop
- leader 从 assignment-first 切到 convergence-first
- superleader 从 scheduler-first 切到 digest-first
- 跨 team 协作从“有消息能力”切到“有边界、有默认可见性、有摘要索引”的正式协作层

## 8. 推荐投喂给 Agent Orchestra 的 gap 包

如果下一轮要直接让 Agent Orchestra 接手，建议按下面顺序投喂：

### 8.1 第一轮

1. `agent-contract-convergence`
2. `team-primary-semantics-switch`

原因：

- 这两条是地基和最关键的 runtime 主语义切换
- 没做完它们，后面的 superleader 同构和跨 team 协作都不会稳定

### 8.2 第二轮

1. `superleader-isomorphic-runtime`

原因：

- 这一步需要建立在 team 内在线协作已经稳定的前提上

### 8.3 第三轮

1. `cross-team-collaboration-layer`

原因：

- 这一步本质上是治理层，不是基础 runtime 层

## 9. 与当前优先级清单的关系

这个文档不是要推翻 [current-priority-gap-list.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/current-priority-gap-list.md)，而是把其中的 `online-collaboration-runtime` 顶层 gap 进一步正式拆开。

更准确地说：

- `online-collaboration-runtime` 仍然是总 gap 名
- 本文把它正式收敛成 4 段连续子主线
- 后续 bootstrap / self-hosting / 人工投喂都应尽量按这个顺序分轮执行

## 10. 相关文档

- [current-priority-gap-list.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/current-priority-gap-list.md)
- [implementation-status.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/implementation-status.md)
- [online-collaboration-runtime-migration.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/online-collaboration-runtime-migration.md)
- [resident-hierarchical-runtime.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/resident-hierarchical-runtime.md)
- [agent-abstraction-skill-and-prompt-system.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/agent-abstraction-skill-and-prompt-system.md)
- [parallel-team-coordination.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/parallel-team-coordination.md)
