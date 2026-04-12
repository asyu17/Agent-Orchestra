# Agent Orchestra 与 Claude Team Agent 对比及系统逻辑

## 1. 一句话结论

当前的 `agent_orchestra` 已经不再是“模仿 Claude team agent 的一个 Python 骨架”，而是一个在局部 team 协作形状上大量借鉴 Claude、但在全局治理层明显上推了一层的系统：Claude team agent 更强在“单 team 常驻协作体验、UI/inbox/plan approval 驱动的高密度 leader-teammate runtime”，而 Agent Orchestra 当前更强在“显式 `Spec DAG`、scope 化全局 task list、`SuperLeader -> Leader -> Teammate` 分层治理、durable teammate slot session、authoritative mailbox cursor、canonical directed mailbox protocol、execution guard、durable session / reattach / protocol bus、以及 authority/evaluator 收敛链”。如果一句话概括差异：Claude 更像成熟的单 team 常驻操作系统，Agent Orchestra 现在更像面向多 team / 多 lane / 更高治理层的可编排 runtime。

## 2. 范围与资料来源

- Claude Code 侧知识与证据：
  - [agent-team.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/claude-code-main/agent-team.md)
  - [TeamCreateTool.ts#L74](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/tools/TeamCreateTool/TeamCreateTool.ts#L74)
  - [AgentTool.tsx#L266](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/tools/AgentTool/AgentTool.tsx#L266)
  - [AgentTool.tsx#L284](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/tools/AgentTool/AgentTool.tsx#L284)
  - [spawnMultiAgent.ts#L840](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/tools/shared/spawnMultiAgent.ts#L840)
  - [inProcessRunner.ts#L883](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/swarm/inProcessRunner.ts#L883)
  - [teammateMailbox.ts#L84](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/teammateMailbox.ts#L84)
  - [permissionSync.ts#L112](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/swarm/permissionSync.ts#L112)
  - [useInboxPoller.ts#L126](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/hooks/useInboxPoller.ts#L126)
- Agent Orchestra 侧知识与证据：
  - [claude-team-mapping.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/claude-team-mapping.md)
  - [parallel-team-coordination.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/parallel-team-coordination.md)
  - [implementation-status.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/implementation-status.md)
  - [agent-orchestra-framework.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/agent-orchestra-framework.md)
  - [bootstrap_round.py#L130](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/bootstrap_round.py#L130)
  - [leader_output_protocol.py#L164](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_output_protocol.py#L164)
  - [leader_loop.py#L438](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py#L438)
  - [leader_loop.py#L1015](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py#L1015)
  - [superleader.py#L382](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/superleader.py#L382)
  - [worker_supervisor.py#L1303](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/worker_supervisor.py#L1303)
  - [worker_supervisor.py#L1699](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/worker_supervisor.py#L1699)
  - [protocol_bridge.py#L374](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/protocol_bridge.py#L374)
  - [protocol_bridge.py#L471](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/protocol_bridge.py#L471)
  - [redis_bus.py#L99](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/bus/redis_bus.py#L99)
  - [bootstrap.py#L1012](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/self_hosting/bootstrap.py#L1012)

## 3. 对比总览

### 3.1 可以把两者先粗分成四句话

1. Claude team agent 的中心是单个 team 的常驻协作体验，Agent Orchestra 的中心是多层级、多 lane、多约束的可编排治理。
2. Claude 把 `leader -> teammate` 协作做得更像一个长期在线的 team shell，Agent Orchestra 把 `SuperLeader -> Leader -> Teammate` 的职责边界做得更显式。
3. Claude 更依赖 `team file + teamContext + mailbox + UI poller` 维持运行态，Agent Orchestra 更依赖 `Spec DAG + scoped task list + blackboard + delivery/evaluator + durable store` 维持运行态。
4. Claude 当前更强在“单 team 常驻交互形态”，Agent Orchestra 当前更强在“显式契约、可恢复性、authority 收敛、self-hosting 和群体级治理”。

### 3.2 快速矩阵

| 维度 | Claude team agent | Agent Orchestra 当前状态 | 判断 |
| --- | --- | --- | --- |
| 组织层级 | `Leader -> Teammate` 扁平单 team | `SuperLeader -> Leader -> Teammate` 分层 | Agent Orchestra 明显上推一层 |
| team 拓扑 | flat roster，不允许 teammate 再拉 teammate | 逻辑上允许上层 leader 再派下层，但受 scope/budget 限制 | Agent Orchestra 更像集团级 |
| 任务真相源 | task list + team runtime 协作面 | `Spec DAG` 是契约真相源，task list 是操作面 | Agent Orchestra 更显式 |
| team 内协作 | mailbox/inbox/poller 驱动，常驻感更强 | 已有 durable teammate slot session、mailbox follow-up、resident/autonomous claim、canonical directed mailbox protocol，但整体自治常驻感仍弱于 Claude | Claude 仍更成熟 |
| 多 team 协调 | 原生不强调多 team/多 lane DAG 治理 | 原生面向多 lane / 多 team / dependency gate | Agent Orchestra 更强 |
| 生命周期/恢复 | 有 team attach/reconnect、UI 侧恢复语义 | 已有 durable session、lease、reclaim、reattach、protocol bus | Agent Orchestra 当前更强在显式恢复 |
| 权限/审批 | permissionSync + UI bridge + plan approval | `PermissionBroker` 已有，但仍偏静态 | Claude 更成熟 |
| 完成语义 | 更偏 team/runtime 行为收敛 | 有 `Reducer / Evaluator / AuthorityState / DeliveryState` 显式链路 | Agent Orchestra 更强 |
| 执行约束 | 主要靠 team 模式与工具协议 | 额外有 `execution_contract / lease_policy / owned_paths / verification_commands / execution_guard` | Agent Orchestra 更强 |
| durable bus | mailbox 很成熟，但不是我们这套显式 store/bus 契约 | `ProtocolBus / RedisProtocolBus / stream-family bus` 已主线化 | Agent Orchestra 更显式 |

## 4. 逐项异同清单

### 4.1 团队拓扑

相同点：

- 两者都把局部协作单元建立在 `leader -> teammate` 这条主轴上。
- 两者都把 leader 当成特殊角色，而不是普通 teammate 的一个别名。

不同点：

- Claude 的 roster 本质上是 flat team，teammate 不能继续 spawn teammate：[AgentTool.tsx#L266](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/tools/AgentTool/AgentTool.tsx#L266)
- Agent Orchestra 从一开始就把 `SuperLeader` 作为独立治理层，`Leader` 只是中间层，不是系统顶层：[agent-orchestra-framework.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/agent-orchestra-framework.md), [superleader.py#L382](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/superleader.py#L382)

判断：

- 这不是“谁先进谁落后”的关系，而是目标不同。
- Claude 的模型更适合一个 leader 带一组 teammate 的高密度协作。
- Agent Orchestra 的模型更适合你想要的“多个 agent team 彼此协作”的集团级运行。

### 4.2 真相源与任务面

相同点：

- 两者都有 task/work surface，让 leader 把工作下放给 teammate。
- 两者都支持 leader 在 team 范围内不断派发子任务。

不同点：

- Claude 的 task list 更接近局部运行事实源，team 协作高度围绕它展开：[agent-team.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/claude-code-main/agent-team.md)
- Agent Orchestra 把 `Spec DAG` 和 task list 明确分开：
  - `Spec DAG` 表示任务契约和依赖
  - task list 表示运行时操作面
  - blackboard 表示执行证据与提案
- 这个分离在当前实现里已经是主线，不是设计草图：[bootstrap_round.py#L194](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/bootstrap_round.py#L194), [leader_output_protocol.py#L164](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_output_protocol.py#L164)

判断：

- Claude 更像“task/work-driven team runtime”。
- Agent Orchestra 更像“contract-driven orchestration runtime”。

### 4.3 消息与协作面

相同点：

- 两者都不是简单的同步 RPC 调用，而是通过消息面来实现上下级协作。
- 两者都承认 mailbox / inbox 在团队协作里的核心地位。

不同点：

- Claude 更偏 recipient-scoped inbox + UI poller + permission bridge 的协作形态：[teammateMailbox.ts#L84](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/teammateMailbox.ts#L84), [useInboxPoller.ts#L126](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/hooks/useInboxPoller.ts#L126)
- Agent Orchestra 已经把消息面进一步拆成：
  - directed mailbox
  - typed `task.directive|task.receipt|task.result` protocol
  - append-only message pool / subscription 语义
  - authoritative mailbox consumer cursor
  - `ProtocolBus / RedisProtocolBus`
  - lane/team blackboard
- 也就是说，Agent Orchestra 现在不是单一消息面，而是“mailbox + blackboard + protocol bus” 三层：[parallel-team-coordination.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/parallel-team-coordination.md), [protocol_bridge.py#L471](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/protocol_bridge.py#L471)

判断：

- Claude 的消息面更贴近日常协作体验。
- Agent Orchestra 的消息面更贴近控制面和可恢复性。

### 4.4 team 内部并行

相同点：

- 两者都不是“leader 串行一条条做完”，而是把工作下发给 teammate 并再收敛。

不同点：

- Claude 的并行更像常驻 team 中多个 teammate 的持续活跃协作，leader 通过 inbox poller 异步收件，整体更接近真正的长期 team shell。
- Agent Orchestra 现在的 team 内并行主线是：
  - leader 输出 `sequential_slices / parallel_slices`
  - runtime 把它们落成 team task
  - `TeammateWorkSurface` 按 resident slot 激活数与 `max_teammates` 推进并行执行
  - teammate slot 会拥有稳定 resident session id，并把 `activation_epoch / current_task_id / claim_session_id / last_worker_session_id` 写入 session host
  - directed mailbox 已经支持 target-slot 路由和 canonical `task.directive`
  - leader 用 authoritative mailbox cursor 继续 follow-up，再配合 resident/autonomous claim 继续收敛：[leader_loop.py#L438](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py#L438), [leader_loop.py#L1015](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py#L1015)
- 这已经明显超出“单次 refill helper”，但仍没有完全达到 Claude 那种 teammate 自己长期在线、自主持续 claim、leader 纯 inbox poller 异步收敛的成熟常驻程度。

判断：

- Agent Orchestra 现在已经有 team 内并行能力。
- 但如果对标 Claude，差距主要不在“能否并行”，而在“是否已经是常驻自治式并行”。

### 4.5 team 外部并行

相同点：

- 两者都能在概念上管理多个工作单元。

不同点：

- Claude team agent 的中心仍然是单 team 运行体验，天然不强调一个系统里多个 team/lane 的正式 dependency graph 治理。
- Agent Orchestra 则天然把多 lane / 多 team 当成一等对象：
  - `SuperLeaderRuntime` 做 dependency-gated ready-lane 调度
  - `LeaderLaneBlackboard` 做 lane 级共享证据
  - `Spec DAG` 做 lane 依赖和 authority 边界：[superleader.py#L382](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/superleader.py#L382), [parallel-team-coordination.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/parallel-team-coordination.md)

判断：

- 这部分 Agent Orchestra 明显强于 Claude 的 team runtime。
- 这是因为你要解决的问题已经超出“一个 team 里的协作”。

### 4.6 生命周期与恢复

相同点：

- 两者都承认运行态恢复和 reconnect 是必要能力。

不同点：

- Claude 的恢复更多绑定在 team attach、UI 轮询、in-process teammate state 和 REPL team context 上。
- Agent Orchestra 当前已经把恢复做成显式控制面：
  - durable `WorkerSession`
  - lease
  - reclaim
  - backend `reattach(...)`
  - protocol cursor
  - protocol bus cursor
  - self-hosting evidence gate：[worker_supervisor.py#L1303](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/worker_supervisor.py#L1303), [worker_supervisor.py#L1699](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/worker_supervisor.py#L1699), [bootstrap.py#L1049](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/self_hosting/bootstrap.py#L1049)

判断：

- 如果比较“恢复能力的显式性和可验证性”，Agent Orchestra 当前已经比 Claude team runtime 更工程化。
- 如果比较“单 team 的使用体验和前台连续感”，Claude 仍然更成熟。

### 4.7 权限与审批

相同点：

- 两者都不把权限当作外部附属物，而是纳入 runtime。

不同点：

- Claude 这里已经有较完整的 permission sync / leader bridge / plan approval 体验。
- Agent Orchestra 当前只有 `PermissionBroker` 主线和 runtime request hook，但还没有同等级的审批体验面。

判断：

- 这块 Claude 更成熟，Agent Orchestra 仍是 backlog。

### 4.8 完成语义

相同点：

- 两者都不应该只靠 agent 自报“我做完了”来判定系统级完成。

不同点：

- Claude 更偏向 team/runtime 行为收敛与 UI/会话层结果。
- Agent Orchestra 明确把完成语义拆成：
  - `DeliveryState`
  - `Evaluator`
  - `Reducer`
  - `AuthorityState`
  - `ObjectiveState`
- 这条链是系统显式主语义，而不是隐含流程：[agent-orchestra-framework.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/agent-orchestra-framework.md), [superleader.py#L509](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/superleader.py#L509)

判断：

- Agent Orchestra 的“完成”更像工程交付收敛。
- Claude 的“完成”更像一个 team 运行时的协作收敛。

## 5. Agent Orchestra 当前系统逻辑清单

### 5.1 逻辑总图

当前系统的主线逻辑可以压缩成 10 步：

1. 用户目标被写成 template/objective。
2. planner 把目标编译成 `ObjectiveSpec + Spec DAG + LeaderTaskCard`。
3. `bootstrap_round` 把 planning result 物化成 team、lane runtime task 和 `LeaderRound`。
4. `SuperLeaderRuntime` 按依赖与并发预算挑选 ready lanes。
5. 每个 lane 进入 `LeaderLoopSupervisor` 的 resident coordination cycle。
6. leader 读取可见 task、blackboard snapshot、mailbox，生成本轮 leader assignment。
7. leader 输出结构化 `LeaderTurnOutput`，runtime 把它转成 team task 与 teammate assignment。
8. teammate 执行并返回 mailbox/result/protocol evidence。
9. leader loop 继续基于 authoritative mailbox cursor consume mailbox、claim overflow task、并更新 lane `DeliveryState`。
10. `SuperLeaderRuntime` 汇总 lane 结果，经 `Reducer / Evaluator / AuthorityState` 推进 objective。

### 5.2 目标与规划层

- 目标入口不是直接“起 agent”，而是先进入 `ObjectiveSpec`、`SpecNode`、`SpecEdge` 这组契约层。
- 当前 `TemplatePlanner` 和 `DynamicSuperLeaderPlanner` 负责把高层目标变成可执行 planning result：[implementation-status.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/implementation-status.md)
- 这意味着 Agent Orchestra 的第一层主语义是“先把目标编译成图”，而不是“先把 agent 拉起来再即兴协作”。

### 5.3 物化层

- `bootstrap_round` 负责把 planning result 落成 `LeaderRound`、team、leader runtime task、lane/team directive entry：[bootstrap_round.py#L194](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/bootstrap_round.py#L194)
- 这一层做的不是执行，而是把高层设计转成可以运行的 lane/team 面。

### 5.4 SuperLeader 层

- `SuperLeaderRuntime` 的核心不是直接执行工作，而是：
  - 看 lane 依赖
  - 启动 ready lane
  - 等至少一个 lane 完成
  - 更新 lane state
  - 最终统一收敛 objective：[superleader.py#L382](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/superleader.py#L382)
- 这层是全局调度层，不直接向 teammate 发任务。

### 5.5 Leader 层

- `LeaderLoopSupervisor` 负责某个 lane/team 的连续协调。
- 它在每个 cycle 里会读取：
  - visible tasks
  - leader lane blackboard snapshot
  - team blackboard snapshot
  - mailbox messages
  - store 中 authoritative mailbox consumer cursor
- 然后编译 leader assignment 去执行：[leader_loop.py#L1089](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py#L1089)

### 5.6 Leader 输出协议层

- leader 不是直接改 store，而是产出结构化 `LeaderTurnOutput`。
- `ingest_leader_turn_output(...)` 再把这个输出转成：
  - team-scope `TaskCard`
  - teammate assignment
  - lane execution report
  - overflow proposal entry：[leader_output_protocol.py#L164](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_output_protocol.py#L164)
- 这层的作用是把 leader 的“语言行为”转成系统级可验证动作。

### 5.7 Teammate 层

- teammate 当前已经可以通过 `in_process`、`subprocess`、`tmux`、`codex_cli` 四类 backend 执行。
- `WorkerRoleProfile` 决定：
  - backend
  - execution contract
  - lease policy
  - idle/reactivate/fallback 配置：[leader_loop.py#L174](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py#L174)
- teammate slot 现在还会拥有稳定 resident session id，并把 slot state 写入 `AgentSession.metadata`。
- teammate 的输出不直接判定全局成功，而是变成 `WorkerRecord`、protocol event、`task.result` mailbox message、artifact evidence。

### 5.8 Worker / Supervisor 层

- `DefaultWorkerSupervisor` 现在负责：
  - bounded retry/resume/relaunch/fallback/escalation
  - protocol-native wait
  - `ACTIVE` session 持久化
  - lease renew
  - reclaim + `reattach(...)`
  - terminal persistence：[worker_supervisor.py#L1303](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/worker_supervisor.py#L1303), [worker_supervisor.py#L1699](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/worker_supervisor.py#L1699)
- 这一层已经从“只是 wait 一下进程”升级成真正的 worker control plane。

### 5.9 消息 / 黑板 / 总线层

- directed mailbox 负责上下级消息传递，并且当前已经有 canonical `task.directive|task.receipt|task.result` payload 形状。
- `LeaderLaneBlackboard` 和 `TeamBlackboard` 负责共享执行证据、proposal、decision、blocker。
- `ProtocolBus / RedisProtocolBus` 负责 lifecycle/session/control/takeover/mailbox 五类控制面事件：[protocol_bridge.py#L471](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/protocol_bridge.py#L471), [redis_bus.py#L99](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/bus/redis_bus.py#L99)
- authoritative mailbox consumer cursor 当前先复用 store 的 `protocol_bus_cursor` persistence surface，而不是把 `DeliveryState.mailbox_cursor` 当唯一事实源。
- 这三层一起构成现在系统的 communication/control surface。

### 5.10 收敛与完成层

- 每个 lane 都有 `DeliveryState`。
- `SuperLeaderRuntime` 在 finalize 阶段读取 lane result，经 reducer/handoff/authority 继续推进 objective。
- self-hosting bootstrap 还会把部分能力变成 evidence gate，避免“看起来完成，其实没有真实运行证据”的假完成：[bootstrap.py#L1049](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/self_hosting/bootstrap.py#L1049)

## 6. 结论性清单

### 6.1 现在我们已经和 Claude 对齐的部分

- `leader -> teammate` 的局部协作主轴
- team 内 task/work surface
- mailbox 驱动的上下级消息
- canonical directed mailbox protocol 与 legacy `payload.task_id` 兼容入口
- 多 backend worker 路由
- resident coordination shell 的基本形状
- team 内受 budget 约束的并行 teammate 执行
- durable teammate slot session 与 authoritative mailbox cursor 的基本形状

### 6.2 现在我们强于 Claude 的部分

- `SuperLeader` 分层治理
- `Spec DAG` + scoped global task list + layered blackboard 的三层模型
- `Reducer / Evaluator / AuthorityState` 显式完成链
- `execution_contract / lease_policy / owned_paths / verification_commands / execution_guard`
- `ACTIVE` durable session / reclaim / `reattach(...)`
- Redis stream-family protocol bus
- self-hosting evidence-gated bootstrap

### 6.3 现在 Claude 仍强于我们的部分

- 单 team 常驻协作体验
- inbox/UI/plan approval 一体化程度
- teammate 更强的长期在线自治感
- `task.receipt` 驱动的更完整 mailbox causal workflow
- permission sync / approval bridge 的成熟度

### 6.4 现在我们的系统本质是什么

- 它不是一个单 team agent shell 的 Python 翻版。
- 它也不是一个只有 DAG 的静态编排器。
- 它当前本质上是：
  - 一个 contract-driven
  - multi-layer
  - recoverable
  - message-and-blackboard coordinated
  - authority-aware
  的 agent runtime。

## 7. 相关文档

- [claude-team-mapping.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/claude-team-mapping.md)
- [parallel-team-coordination.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/parallel-team-coordination.md)
- [implementation-status.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/implementation-status.md)
- [agent-orchestra-framework.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/agent-orchestra-framework.md)
- [active-reattach-and-protocol-bus.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/active-reattach-and-protocol-bus.md)
- [../claude-code-main/agent-team.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/claude-code-main/agent-team.md)
