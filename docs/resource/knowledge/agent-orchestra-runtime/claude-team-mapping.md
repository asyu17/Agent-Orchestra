# Claude Code Team Agent 到 agent_orchestra 的抽象映射

## 1. 一句话结论

Claude Code 里最值得迁移的不是 REPL 和 pane UI，而是它成熟的 `Leader -> Teammate` 本地 team runtime：`task list`、`spawn/backend routing`、`worker supervision`、`mailbox / inbox`、`permission bridge`、`identity/reconnect` 都值得沿用；而 `agent_orchestra` 现在已经把其中相当一部分落成了 Python 版运行时：`BackendRegistry`、`InProcess / Subprocess / Tmux / CodexCli` 四 backend、`DefaultWorkerSupervisor`、`bootstrap_round`、`leader_output_protocol`、`MailboxBridge`、`PermissionBroker`、`DefaultDeliveryEvaluator`、`LeaderLoopSupervisor` 和 `SuperLeaderRuntime` 都已经到位。新增的核心仍然是 `Spec DAG`、统一 task list 的 scoped view、`LeaderLaneBlackboard`、`TeamBlackboard` 和 reducer 驱动的状态收敛。

## 2. 范围与资料来源

- Claude Code 证据：
  - [TeamCreateTool.ts#L128](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/tools/TeamCreateTool/TeamCreateTool.ts#L128)
  - [teamHelpers.ts#L64](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/swarm/teamHelpers.ts#L64)
  - [AgentTool.tsx#L266](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/tools/AgentTool/AgentTool.tsx#L266)
  - [spawnMultiAgent.ts#L305](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/tools/shared/spawnMultiAgent.ts#L305)
  - [spawnMultiAgent.ts#L840](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/tools/shared/spawnMultiAgent.ts#L840)
  - [spawnInProcess.ts#L104](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/swarm/spawnInProcess.ts#L104)
  - [inProcessRunner.ts#L883](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/swarm/inProcessRunner.ts#L883)
  - [teammateMailbox.ts#L43](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/teammateMailbox.ts#L43)
  - [permissionSync.ts#L112](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/swarm/permissionSync.ts#L112)
  - [leaderPermissionBridge.ts#L25](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/swarm/leaderPermissionBridge.ts#L25)
  - [teammate.ts#L44](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/teammate.ts#L44)
  - [reconnection.ts#L23](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/swarm/reconnection.ts#L23)
- 当前 Python 落点：
  - [team.py#L7](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/contracts/team.py#L7)
  - [task.py#L8](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/contracts/task.py#L8)
  - [execution.py#L10](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/contracts/execution.py#L10)
  - [storage/base.py#L14](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/storage/base.py#L14)
  - [base.py#L41](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/backends/base.py#L41)
  - [in_process.py#L6](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/backends/in_process.py#L6)
  - [subprocess_backend.py#L46](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/backends/subprocess_backend.py#L46)
  - [tmux_backend.py#L37](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/backends/tmux_backend.py#L37)
  - [codex_cli_backend.py#L190](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/backends/codex_cli_backend.py#L190)
  - [worker_process.py#L15](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/worker_process.py#L15)
  - [worker_supervisor.py#L26](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/worker_supervisor.py#L26)
  - [bootstrap_round.py#L138](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/bootstrap_round.py#L138)
  - [leader_output_protocol.py#L95](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_output_protocol.py#L95)
  - [leader_output_protocol.py#L158](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_output_protocol.py#L158)
  - [group_runtime.py#L47](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/group_runtime.py#L47)
  - [team_runtime.py#L11](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/team_runtime.py#L11)
  - [reducer.py#L8](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/reducer.py#L8)
  - [events.py#L10](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/contracts/events.py#L10)
  - [redis_bus.py#L17](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/bus/redis_bus.py#L17)
  - [runner.py#L51](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/contracts/runner.py#L51)
  - [adapter.py#L34](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runners/openai/adapter.py#L34)
- 关联知识：
  - `resource/knowledge/claude-code-main/agent-team.md`
  - `resource/knowledge/multi-agent-team-delivery/delivery-contracts.md`
  - `resource/knowledge/agent-orchestra-runtime/agent-orchestra-framework.md`

## 3. 映射关系

### 3.1 `TeamFile` / roster 持久化 -> `Group`、`Team`、`OrchestrationStore`

Claude Code 的 roster 核心事实源是 `TeamFile`，由 team 创建、删除和 cleanup 逻辑共同维护: [TeamCreateTool.ts#L157](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/tools/TeamCreateTool/TeamCreateTool.ts#L157), [teamHelpers.ts#L64](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/swarm/teamHelpers.ts#L64)。

在当前 Python 代码里，这个抽象已经被拆成：

- 结构契约：`Group`、`Team`: [team.py#L7](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/contracts/team.py#L7)
- durable store 边界：`OrchestrationStore`: [storage/base.py#L14](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/storage/base.py#L14)
- 当前最小实现：`InMemoryOrchestrationStore`: [in_memory.py#L13](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/storage/in_memory.py#L13)

判断：这是正确的升级方向，因为 group-ready 版本不能再只维护一个 team 的扁平 `members[]`。

### 3.2 `AgentTool -> spawnMultiAgent` -> `BackendRegistry + LaunchBackend + GroupRuntime`

Claude Code 真正的 teammate 启动入口不在 team create，而在 `AgentTool -> spawnMultiAgent`: [AgentTool.tsx#L284](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/tools/AgentTool/AgentTool.tsx#L284), [spawnMultiAgent.ts#L305](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/tools/shared/spawnMultiAgent.ts#L305), [spawnMultiAgent.ts#L840](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/tools/shared/spawnMultiAgent.ts#L840)。

当前 Python 代码已经把这一层落成了本地可运行版本，但仍保持了和 Claude 相同的拆层思路：先分离 backend 路由，再让 orchestration 调用它。

- `BackendRegistry`：backend 名称查找与注册: [base.py#L41](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/backends/base.py#L41)
- `GroupRuntime`：协调层只做 `launch / resume / cancel / run_worker_assignment`: [group_runtime.py#L130](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/group_runtime.py#L130), [group_runtime.py#L151](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/group_runtime.py#L151)
- `TeamRuntime`: [team_runtime.py#L11](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/team_runtime.py#L11)
- `LaunchBackend` / `WorkerHandle`: [execution.py#L31](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/contracts/execution.py#L31), [execution.py#L96](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/contracts/execution.py#L96)
- `InProcessLaunchBackend`、`SubprocessLaunchBackend`、`TmuxLaunchBackend`、`CodexCliLaunchBackend`: [in_process.py#L6](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/backends/in_process.py#L6), [subprocess_backend.py#L46](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/backends/subprocess_backend.py#L46), [tmux_backend.py#L37](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/backends/tmux_backend.py#L37), [codex_cli_backend.py#L190](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/backends/codex_cli_backend.py#L190)

判断：这一步和 Claude 很贴近。真正值得复用的是“spawn/backend routing 与 orchestration 解耦”这个形状，而不是具体照搬 UI 或 pane。

### 3.3 `spawnInProcess + inProcessRunner` -> `DefaultWorkerSupervisor`

Claude Code 的 in-process teammate 已经体现出 supervisor 雏形：spawn、abort、idle、reactivate、下一轮 prompt 接力都在这层完成: [spawnInProcess.ts#L104](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/swarm/spawnInProcess.ts#L104), [inProcessRunner.ts#L883](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/swarm/inProcessRunner.ts#L883)。

当前 Python 代码已经把“最小可用 supervisor”落下来了，但它明确只覆盖 one-turn 生命周期：

- `TaskCard`：team 内工作单元: [task.py#L8](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/contracts/task.py#L8)
- `AgentRunner`：单轮执行抽象: [runner.py#L51](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/contracts/runner.py#L51)
- `OpenAIResponsesAgentRunner`：第一版 provider adapter: [adapter.py#L34](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runners/openai/adapter.py#L34)
- `WorkerSupervisor`：生命周期接口: [execution.py#L110](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/contracts/execution.py#L110)
- `DefaultWorkerSupervisor`：当前实现，负责落 `WorkerRecord`，in-process 调 `AgentRunner.run_turn()`，out-of-process 轮询 result file: [worker_supervisor.py#L26](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/worker_supervisor.py#L26), [worker_supervisor.py#L42](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/worker_supervisor.py#L42), [worker_supervisor.py#L114](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/worker_supervisor.py#L114)
- `worker_process`：subprocess / tmux 共享的最小 worker harness: [worker_process.py#L15](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/worker_process.py#L15)
- `CodexCliLaunchBackend`：直接把 `WorkerAssignment` 渲染为 prompt，并调用本地 `codex exec` 产出真实 code-edit worker 结果: [codex_cli_backend.py#L33](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/backends/codex_cli_backend.py#L33), [codex_cli_backend.py#L224](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/backends/codex_cli_backend.py#L224)

所以这里的状态是：one-turn supervisor 已实现，但 Claude 那种长生命周期 idle/reactivate/reconnect 语义仍未移植。

### 3.3.1 `leader` 输出到 `teammate` 任务桥

Claude Code 的 leader 在局部 team runtime 里，本质上不仅要“看消息”，还要把 teammate 要做的工作持续转成 task list 和下一轮指令。

当前 Python 代码已经补上了一条最小 bridge：

- `materialize_planning_result()` 会把 `LeaderTaskCard` 物化成 `LeaderRound`、team 实体、leader-lane runtime task 和 bootstrap blackboard directive: [bootstrap_round.py#L138](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/bootstrap_round.py#L138)
- `compile_leader_assignments()` 会在 leader assignment 里明确要求返回 JSON 协议，而不是只返回自由文本: [bootstrap_round.py#L66](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/bootstrap_round.py#L66), [bootstrap_round.py#L233](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/bootstrap_round.py#L233)
- `parse_leader_turn_output()` 与 `ingest_leader_turn_output()` 会把 leader 的结构化输出校验后转成 team-scope `TaskCard`、leader-lane `execution_report`、overflow `proposal` 和 teammate `WorkerAssignment`: [leader_output_protocol.py#L95](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_output_protocol.py#L95), [leader_output_protocol.py#L158](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_output_protocol.py#L158)

判断：

- 这已经是第一条真正可运行的 `Leader -> Teammate` 桥
- 它现在已经继续被 `LeaderLoopSupervisor` 接成 inbox/mailbox 驱动的连续多轮 leader loop，但 worker 进程本身仍不是 Claude 那种长驻 idle/reactivate 形态

### 3.4 Claude task list -> 全局统一 task list + scoped views

Claude 的 task list 是最值得直接借用的局部运行面之一。

关键证据：

- 新任务默认 `pending` 且无 owner: [prompt.ts#L12](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/tools/TaskCreateTool/prompt.ts#L12), [TaskCreateTool.ts#L80](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/tools/TaskCreateTool/TaskCreateTool.ts#L80)
- teammate 在将任务标记为 `in_progress` 时会自动 claim owner: [TaskUpdateTool.ts#L185](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/tools/TaskUpdateTool/TaskUpdateTool.ts#L185)
- teammates 可以在发现额外工作时直接创建新任务: [prompt.ts#L30](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/tools/TaskCreateTool/prompt.ts#L30), [prompt.ts#L97](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/tools/TeamCreateTool/prompt.ts#L97)

在 `agent_orchestra` 里，这块已经落下第一阶段实现：

- `TaskCard` 增加了 `scope / owner_id / blocked_by / created_by / derived_from / reason`: [task.py#L8](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/contracts/task.py#L8)
- `GroupRuntime.update_task_status()` 实现 `in_progress` auto-claim: [group_runtime.py#L241](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/group_runtime.py#L241)
- `GroupRuntime.list_visible_tasks()` 实现 scoped visibility: [group_runtime.py#L260](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/group_runtime.py#L260)

已确认的 scope 规则是：

- `SuperLeader` 可以看到下层全部任务
- `Leader` 可以看到自己 lane 与自己 team 的全部任务
- `Teammate` 只能看到自己 `team scope` 的任务

判断：

- task list 可以高度借鉴 Claude
- 但它在新体系里只能是运行时操作面，不能替代 `Spec DAG` 真相源

### 3.5 `teammateMailbox + permissionSync` -> 未来 `ProtocolBus + Blackboard`，当前先落 `OrchestraEvent + RedisEventBus`

Claude Code 现在的控制面主要靠 mailbox 和 permission sync 协议: [teammateMailbox.ts#L43](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/teammateMailbox.ts#L43), [permissionSync.ts#L112](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/swarm/permissionSync.ts#L112), [leaderPermissionBridge.ts#L25](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/swarm/leaderPermissionBridge.ts#L25)。

当前 Python 代码先落了更薄的事件层：

- `OrchestraEvent`: [events.py#L10](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/contracts/events.py#L10)
- `RedisEventBus`: [redis_bus.py#L17](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/bus/redis_bus.py#L17)

当前已确认的新框架里，这部分已经有第一阶段代码落点：

- `BlackboardEntry` / `BlackboardSnapshot`: [blackboard.py#L9](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/contracts/blackboard.py#L9)
- `LeaderLaneBlackboardReducer` / `TeamBlackboardReducer`: [reducer.py#L29](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/reducer.py#L29)
- `GroupRuntime.append_blackboard_entry()` / `reduce_blackboard()`: [group_runtime.py#L306](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/group_runtime.py#L306), [group_runtime.py#L343](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/group_runtime.py#L343)

但这条线现在已经继续往前走了一段：

- `MailboxEnvelope / MailboxCursor / MailboxBridge` 契约已经独立出来: [mailbox.py#L21](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/tools/mailbox.py#L21)
- `InMemoryMailboxBridge` 已接到新的 `LeaderLoopSupervisor`，leader 已经能在下一轮消费 teammate 结果: [mailbox_bridge.py#L4](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/mailbox_bridge.py#L4), [leader_loop.py#L183](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py#L183)
- `PermissionRequest / PermissionBroker` 也已经接入 leader / teammate assignment 执行前检查: [permission_protocol.py#L8](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/tools/permission_protocol.py#L8), [leader_loop.py#L178](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py#L178)

判断：

- 协议消息类型、idle / permission / shutdown 等流程仍然非常值得继续沿用 Claude
- 但新的 Python 代码已经不再只有 Blackboard；它已经有第一版 directed mailbox 与 permission hook
- 真正还没落的是 Redis-backed typed protocol bus 和 UI-level approval bridge

### 3.6 `teammate + reconnection` -> 未来 `IdentityScope + Reconnector`

Claude Code 用 `teammate.ts` 和 `reconnection.ts` 兜住 actor identity 与 team attachment 恢复: [teammate.ts#L44](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/teammate.ts#L44), [reconnection.ts#L23](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/swarm/reconnection.ts#L23)。

当前 Python 代码里仍然没有 Claude 那种完整 reconnect 层，但已经多了一个 runtime-level resume 入口：

- `DeliveryState.mailbox_cursor` 已作为 lane state 的一部分持久化: [delivery.py#L29](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/contracts/delivery.py#L29)
- `LeaderLoopSupervisor.run(...)` 会优先读取已存在的 delivery state，并从 `iteration + 1` 和上次 mailbox cursor 继续: [leader_loop.py#L377](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py#L377)

这意味着：

- 现在已经不再是“完全没有恢复语义”
- 但 actor scope、pending approvals、pane/process 级 reconnect 仍无承载层

这部分是后续升级 group-ready runtime 时必须补齐的空缺。

### 3.7 `team complete` 不是终点 -> `Reducer / AuthorityState`

`multi-agent-team-delivery` 的核心要求是：没有 authority merge，不算完成。

当前 Python 代码已经显式把这个收敛点放进：

- `record_handoff()`：写出交付: [team_runtime.py#L38](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/team_runtime.py#L38)
- `Reducer.apply()`：把 handoff 收敛成 authority state: [reducer.py#L8](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/reducer.py#L8)
- `reduce_group()`：触发 reducer 并发布 authority 更新事件: [group_runtime.py#L299](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/group_runtime.py#L299)

这说明当前骨架最有价值的地方不是“能起 agent”，而是先把 `handoff -> authority` 这条完成语义链显式化。

## 4. 不应原样照搬的点

下面这些是基于 Claude Code 证据做出的设计判断，不是当前 Python 代码已经完成的事实：

- 不应照搬“一个 leader 只能管一个 team”的结构；group 应该是一层独立拓扑。
- 不应照搬扁平 `members[]`；至少要能表达 `group_id / team_id / worker_id / role / lane`。
- 不应照搬 leader 通过特殊环境身份隐式识别的做法；所有 actor 都应有稳定 ID。
- 不应照搬把名字当地址；消息地址应该是稳定的 worker/channel 标识。
- 不应照搬 human message 和 protocol message 混在一个 inbox 的做法；应升级为 typed bus。
- 不应照搬只按 `isActive` / idle 做 cleanup；清理应受 authority/objective gate 驱动。
- 不应把 Claude 的 task list 当最终真相源；在新体系里它只是 `Spec DAG` 的运行时操作投影。

这些判断与 `claude-code-main/agent-team.md` 和 `multi-agent-team-delivery/delivery-contracts.md` 的结论保持一致。

## 5. 哪些可以高度复用

下面这些是可以尽量贴近 Claude 继续实现的部分：

- `Leader -> Teammate` 的局部协作模型
- task list 的简单语义：
  - `pending`
  - `in_progress`
  - `completed`
  - `blocked`
  - `owner`
- teammate 在 team scope 内直接创建新任务
- teammate idle / wake-up 语义
- permission / shutdown / plan approval 等协议消息
- leader 自动收到 teammate 消息并继续下一轮协调

判断：

- 这部分最适合做成 Claude-compatible core
- 新增复杂性应尽量放在 `SuperLeader`、scope 和 blackboard 这一层，而不是重写本地 team runtime

## 6. 当前已落地与缺口

当前已落地：

- group/team/task/handoff/authority 的最小契约
- `ObjectiveSpec / SpecNode / SpecEdge`
- scoped task list 的 `scope`、auto-claim 和可见性规则
- `LeaderLaneBlackboard` / `TeamBlackboard` 契约与 snapshot reducer
- `ObjectiveTemplate`、template I/O、deterministic `TemplatePlanner`
- `WorkerAssignment / WorkerResult / WorkerRecord` 契约
- `BackendRegistry`
- `InProcessLaunchBackend`、`SubprocessLaunchBackend`、`TmuxLaunchBackend`
- `DefaultWorkerSupervisor`
- `worker_process` 共享 harness
- `bootstrap_round`：`LeaderTaskCard -> LeaderRound -> leader WorkerAssignment`
- `leader_output_protocol`：`LeaderTurnOutput -> team-scope TaskCard -> teammate WorkerAssignment`
- `MailboxEnvelope / MailboxBridge / PermissionBroker` 契约
- `DeliveryState` 与新的 `DefaultDeliveryEvaluator`
- `LeaderLoopSupervisor`：连续 leader/team loop
- `SuperLeaderRuntime`：多 lane objective runtime
- `GroupRuntime.run_worker_assignment()` 和 worker record persistence
- store / bus / reducer 的明确边界
- OpenAI `AgentRunner` 抽象
- CLI 薄封装

当前仍缺：

- LLM-assisted or adaptive `Planner`
- 进程级 / pane 级 `IdentityScope + Reconnector`
- 多轮 `WorkerSupervisor` 生命周期，包括 idle/reactivate 和崩溃恢复
- remote/cloud backend、sticky fallback 和更成熟的 backend routing 策略
- Redis-backed typed mailbox / protocol bus
- authority-aware 的更完整 `DeliveryStateMachine`

因此这版代码已经不只是“协议内核骨架”，而是“带第一版本地执行层的协调内核”；但它还不是完整集团级 agent runtime。

## 7. 相关文档

- `README.md`
- `architecture.md`
- `agent-orchestra-framework.md`
- `../claude-code-main/agent-team.md`
- `../multi-agent-team-delivery/delivery-contracts.md`
