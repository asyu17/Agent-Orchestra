# agent_orchestra Python Runtime 架构

## 1. 一句话结论

当前的 `agent_orchestra` 已经不只剩下最小 `group / team / task / handoff / authority / runner` 骨架了；除了第一阶段的协调内核和第一版本地执行层之外，现在执行层里还多了一块 runtime-owned reliability + execution guard + 第一版 session lifecycle + 第一版 provider fallback routing：`WorkerExecutionPolicy / WorkerAttemptDecision / WorkerFailureKind / WorkerProviderRoute / WorkerEscalation / WorkerSessionStatus / WorkerSession`、`ExecutionGuardResult / ExecutionGuardStatus`、`DefaultWorkerSupervisor.run_assignment_with_policy(...)`、`GroupRuntime.apply_execution_guard(...)`、以及 backends 的显式 `resume()` / `reactivate_supported` 能力边界都已经进了代码。与此同时，bounded dynamic planning 也已经落地：`DynamicSuperLeaderPlanner` 可以从 higher-level brief 或 seed metadata 合成标准 `PlanningResult`，而 authority-aware completion 的最后一跳也已经落主线：lane 完成会写 authority-root handoff、经 reducer 收敛并最终关闭 objective。本轮又把基础设施主线推进了一段：`runtime.protocol_bridge` 现在已经统一 `InMemoryMailboxBridge / RedisMailboxBridge` 的 mailbox cursor/list/ack 语义，并被 `LeaderLoopSupervisor` / `SuperLeaderRuntime` 默认消费；`PostgresOrchestrationStore` 也已经不再是 placeholder，而是为 covered orchestration entities 提供正式 CRUD。现在真正还缺的是跨 transport / 跨进程的 durable reconnect、超出当前 covered slice 的生产级 Redis/PostgreSQL control plane，以及 planner feedback / adaptive replan 上方的 sticky provider health routing。

如果再往前走一步看，它现在还多了一层“自开发入口”：`self_hosting.bootstrap` 已经能把当前知识包里的优先缺口转成下一轮开发 objective，并导出下一轮 instruction packet。

## 2. 范围与资料来源

- 当前代码：
  - [objective.py#L9](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/contracts/objective.py#L9)
  - [blackboard.py#L9](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/contracts/blackboard.py#L9)
  - [execution.py#L10](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/contracts/execution.py#L10)
  - [enums.py#L21](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/contracts/enums.py#L21)
  - [template.py#L10](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/planning/template.py#L10)
  - [io.py#L28](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/planning/io.py#L28)
  - [template_planner.py#L33](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/planning/template_planner.py#L33)
  - [dynamic_superleader.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/planning/dynamic_superleader.py)
  - [runner.py#L11](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/contracts/runner.py#L11)
  - [team.py#L7](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/contracts/team.py#L7)
  - [task.py#L8](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/contracts/task.py#L8)
  - [events.py#L10](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/contracts/events.py#L10)
  - [base.py#L41](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/backends/base.py#L41)
  - [in_process.py#L6](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/backends/in_process.py#L6)
  - [subprocess_backend.py#L46](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/backends/subprocess_backend.py#L46)
  - [tmux_backend.py#L37](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/backends/tmux_backend.py#L37)
  - [worker_process.py#L15](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/worker_process.py#L15)
  - [worker_supervisor.py#L26](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/worker_supervisor.py#L26)
  - [delivery.py#L8](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/contracts/delivery.py#L8)
  - [mailbox.py#L21](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/tools/mailbox.py#L21)
  - [permission_protocol.py#L8](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/tools/permission_protocol.py#L8)
  - [bootstrap_round.py#L138](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/bootstrap_round.py#L138)
  - [leader_output_protocol.py#L95](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_output_protocol.py#L95)
  - [leader_output_protocol.py#L158](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_output_protocol.py#L158)
  - [evaluator.py#L48](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/evaluator.py#L48)
  - [mailbox_bridge.py#L4](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/mailbox_bridge.py#L4)
  - [leader_loop.py#L144](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py#L144)
  - [superleader.py#L38](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/superleader.py#L38)
  - [bootstrap.py#L338](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/self_hosting/bootstrap.py#L338)
  - [bootstrap.py#L480](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/self_hosting/bootstrap.py#L480)
  - [group_runtime.py#L47](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/group_runtime.py#L47)
  - [team_runtime.py#L11](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/team_runtime.py#L11)
  - [reducer.py#L8](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/reducer.py#L8)
  - [orchestrator.py#L17](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/orchestrator.py#L17)
  - [storage/base.py#L14](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/storage/base.py#L14)
  - [in_memory.py#L13](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/storage/in_memory.py#L13)
  - [models.py#L4](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/storage/postgres/models.py#L4)
  - [store.py#L24](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/storage/postgres/store.py#L24)
  - [redis_bus.py#L17](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/bus/redis_bus.py#L17)
  - [adapter.py#L34](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runners/openai/adapter.py#L34)
  - [main.py#L10](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/cli/main.py#L10)
- 上游知识来源：
  - `resource/knowledge/claude-code-main/agent-team.md`
  - `resource/knowledge/multi-agent-team-delivery/delivery-contracts.md`
  - `docs/superpowers/specs/2026-04-03-agent-orchestra-launch-backend-supervisor-design.md`
  - `docs/superpowers/plans/2026-04-03-agent-orchestra-launch-backend-supervisor.md`

## 3. 模块分层

### 3.1 契约层

契约层把运行时最稳定的语义先抽出来：

- `ObjectiveSpec`、`SpecNode`、`SpecEdge` 定义在 [objective.py#L9](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/contracts/objective.py#L9)
- `BlackboardEntry`、`BlackboardSnapshot` 定义在 [blackboard.py#L9](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/contracts/blackboard.py#L9)
- `WorkerBudget`、`LeaderTaskCard`、`WorkerHandle`、`WorkerAssignment`、`WorkerResult`、`WorkerRecord`、`WorkerAttemptDecision`、`WorkerFailureKind`、`WorkerProviderRoute`、`WorkerExecutionPolicy`、`WorkerEscalation`、`WorkerSessionStatus`、`WorkerSession`、`ExecutionGuardStatus`、`VerificationCommandResult`、`ExecutionGuardResult` 以及 `Planner / LaunchBackend / WorkerSupervisor` 定义在 [execution.py#L12](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/contracts/execution.py#L12)、[execution.py#L44](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/contracts/execution.py#L44)、[execution.py#L65](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/contracts/execution.py#L65)、[execution.py#L71](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/contracts/execution.py#L71) 和 [execution.py#L80](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/contracts/execution.py#L80)
- `WorkerStatus` 定义在 [enums.py#L21](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/contracts/enums.py#L21)
- `ObjectiveTemplate`、`WorkstreamTemplate`、`PlanningResult` 定义在 [template.py#L10](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/planning/template.py#L10)
- `DynamicPlanningConfig` 与 `DynamicSuperLeaderPlanner` 定义在 [dynamic_superleader.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/planning/dynamic_superleader.py)
- `Group`、`AgentProfile`、`Team` 定义在 [team.py#L7](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/contracts/team.py#L7)
- `TaskCard` 定义在 [task.py#L8](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/contracts/task.py#L8)
- `OrchestraEvent` 定义在 [events.py#L10](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/contracts/events.py#L10)
- `AgentRunner` 与 turn/request/result/health 定义在 [runner.py#L11](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/contracts/runner.py#L11) 和 [runner.py#L51](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/contracts/runner.py#L51)
- 新的自治层契约包括 `DeliveryStateKind / DeliveryStatus / DeliveryDecision / DeliveryState`: [delivery.py#L8](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/contracts/delivery.py#L8)
- mailbox / permission 控制面契约分别在 [mailbox.py#L21](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/tools/mailbox.py#L21) 和 [permission_protocol.py#L8](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/tools/permission_protocol.py#L8)

这一步的意义是把 provider、存储、UI 都挡在契约之外。后续无论接 web service、queue worker 还是 terminal runtime，都能复用同一套类型语义。

### 3.2 运行时层

运行时层现在分成三层：

- 旧的最小协作闭环：
  - `TeamRuntime` 负责 team 内 task 提交与 handoff 写出: [team_runtime.py#L11](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/team_runtime.py#L11)
  - `GroupRuntime` 负责 group 级创建、team 组织、handoff 汇总与 reducer 调用: [group_runtime.py#L47](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/group_runtime.py#L47)
  - `Reducer` 负责把 handoff 收敛成 authority state: [reducer.py#L12](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/reducer.py#L12)
- 新增的第一阶段协调能力：
  - `GroupRuntime.create_objective()`、`add_spec_node()`、`add_spec_edge()` 把 `Spec DAG` 接入运行时: [group_runtime.py#L87](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/group_runtime.py#L87), [group_runtime.py#L158](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/group_runtime.py#L158), [group_runtime.py#L192](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/group_runtime.py#L192)
  - `GroupRuntime.update_task_status()`、`list_visible_tasks()` 实现 scoped task list 的可见性与 auto-claim: [group_runtime.py#L241](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/group_runtime.py#L241), [group_runtime.py#L260](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/group_runtime.py#L260)
  - `GroupRuntime.append_blackboard_entry()`、`reduce_blackboard()` 加上 `LeaderLaneBlackboardReducer` / `TeamBlackboardReducer`，把黑板做成 append-only log + snapshot: [group_runtime.py#L306](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/group_runtime.py#L306), [group_runtime.py#L343](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/group_runtime.py#L343), [reducer.py#L29](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/reducer.py#L29)
  - `TemplatePlanner` 和 `GroupRuntime.plan_from_template()` 现在可以把 `objective.yaml` 风格模板编译并持久化为 `ObjectiveSpec + LeaderTaskCard + Spec DAG`；`DynamicSuperLeaderPlanner` 则能在没有显式 workstream 时先合成 bounded `WorkstreamTemplate`，再复用同一条编译链: [template_planner.py#L33](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/planning/template_planner.py#L33), [dynamic_superleader.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/planning/dynamic_superleader.py), [group_runtime.py#L126](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/group_runtime.py#L126)
- 第一版执行层：
  - `BackendRegistry` 把 backend 路由从 orchestration 中拆开，和 Claude 的 backend registry 思路一致: [base.py#L41](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/backends/base.py#L41)
  - `InProcessLaunchBackend`、`SubprocessLaunchBackend`、`TmuxLaunchBackend`、`CodexCliLaunchBackend` 提供四种本地 worker transport，其中 `codex_cli` 已经是直接调用本机 `codex exec` 的真实 code-edit transport；`in_process` 现在还显式声明了 `resume_supported + reactivate_supported`，因此它成为第一条真正可走 idle/reactivate 主线路径的 backend: [in_process.py#L6](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/backends/in_process.py#L6), [subprocess_backend.py#L46](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/backends/subprocess_backend.py#L46), [tmux_backend.py#L37](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/backends/tmux_backend.py#L37), [codex_cli_backend.py#L190](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/backends/codex_cli_backend.py#L190)
  - `GroupRuntime.launch_worker()`、`resume_worker()`、`cancel_worker()` 仍只负责 transport 操作，但 `run_worker_assignment()` 已经改为先把 assignment 交给 supervisor-owned policy，再对最终 record 做 runtime-owned execution guard，而不是自己拼 `launch -> wait`: [group_runtime.py#L143](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/group_runtime.py#L143), [group_runtime.py#L158](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/group_runtime.py#L158)
- `DefaultWorkerSupervisor` 现在不只是 `launched -> running -> completed/failed` 的 one-turn 生命周期；它还会围绕一次逻辑 assignment 决定 `resume / retry / fallback / escalate`，并在支持 `reactivate_supported` 的 transport 上把 `idle -> reactivate` 也纳入 runtime-owned policy。provider slice 的边界也已经清楚：supervisor 会先构造 primary route + `provider_fallbacks`，在每个 route 内复用同一套 bounded retry/resume/relaunch 规则，再在 `failure_kind=provider_unavailable` 时显式切到下一个 route；attempt history、provider route、`provider_routing.route_history`、`provider_exhausted` escalation 和 session snapshot 都会回写到最终 `WorkerRecord`: [worker_supervisor.py#L116](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/worker_supervisor.py#L116), [worker_supervisor.py#L439](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/worker_supervisor.py#L439), [worker_supervisor.py#L507](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/worker_supervisor.py#L507), [worker_supervisor.py#L593](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/worker_supervisor.py#L593)
  - `GroupRuntime.apply_execution_guard(...)` 会在 launch 前拍工作目录快照，在 worker 返回后计算 `modified_paths`、比较 `owned_paths`、执行 `verification_commands`、忽略 backend 自己的 spool/result artifacts，并把 `guard_status / out_of_scope_paths / verification_results / guard_summary` 归一到最终 `WorkerRecord.metadata`: [group_runtime.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/group_runtime.py)
  - `worker_process` 是 subprocess / tmux 共享的最小 worker harness：读 assignment JSON，写 result JSON，当前支持 deterministic success/failure 模式: [worker_process.py#L15](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/worker_process.py#L15)
  - `CodexCliLaunchBackend` 则不走 `worker_process`，而是直接把 `WorkerAssignment` 渲染成 prompt，生成 `prompt/stdout/stderr/last_message/result` artifacts，并由后台 watcher 把 `codex exec` 的最终消息归一成标准 `result.json`: [codex_cli_backend.py#L33](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/backends/codex_cli_backend.py#L33), [codex_cli_backend.py#L104](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/backends/codex_cli_backend.py#L104), [codex_cli_backend.py#L224](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/backends/codex_cli_backend.py#L224)
  - `Subprocess / Tmux / CodexCli` backend 的 `resume()` 现在显式只做 transport 可接管性校验：`subprocess` / `codex_cli` 要求底层 process 仍活着，`tmux` 要求 session 仍存在；retry policy 继续留在 runtime/supervisor 层：[subprocess_backend.py#L109](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/backends/subprocess_backend.py#L109), [tmux_backend.py#L100](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/backends/tmux_backend.py#L100), [codex_cli_backend.py#L304](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/backends/codex_cli_backend.py#L304)
  - `materialize_planning_result()` 会把 `PlanningResult.leader_tasks` 物化成 `LeaderRound`、team 实体、leader-lane runtime task，以及 leader-lane / team 的 bootstrap directive blackboard entry: [bootstrap_round.py#L138](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/bootstrap_round.py#L138)
  - `compile_leader_assignments()` 会把 `LeaderRound` 编译成带 JSON 协议要求的 leader `WorkerAssignment`，要求 leader 返回结构化 `sequential_slices / parallel_slices`: [bootstrap_round.py#L233](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/bootstrap_round.py#L233), [bootstrap_round.py#L66](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/bootstrap_round.py#L66)
  - `parse_leader_turn_output()` 与 `ingest_leader_turn_output()` 已经把 leader 的结构化输出接成下一层运行时动作：它会校验 payload、创建 team-scope `TaskCard`、写入 leader-lane `execution_report`，并进一步编译 teammate `WorkerAssignment`: [leader_output_protocol.py#L95](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_output_protocol.py#L95), [leader_output_protocol.py#L158](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_output_protocol.py#L158)
- `runtime.protocol_bridge.InMemoryMailboxBridge / RedisMailboxBridge` 现在共用 canonical `MailboxBridge` cursor/list/ack 语义；`LeaderLoopSupervisor` / `SuperLeaderRuntime` 默认已经切到这里，而 `runtime.mailbox_bridge` 退回成 legacy in-memory reference
  - `DefaultDeliveryEvaluator` 负责 lane/objective 级 `continue / complete / block / fail` 判定；当所有 lane 已完成时，它现在会等待 `authority_state`，而不是直接把 objective 标记为完成: [evaluator.py#L50](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/evaluator.py#L50), [evaluator.py#L162](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/evaluator.py#L162)
  - `LeaderLoopSupervisor` 把 leader turn、teammate turn、mailbox consume 和 delivery-state persistence 串成 bounded multi-turn lane loop；它还会默认对 leader worker 打开 `keep_leader_session_idle`，因此当前 in-process leader 已经能在相邻 turn 之间复用同一 session: [leader_loop.py#L77](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py#L77), [leader_loop.py#L155](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py#L155)
  - `TeamRuntime.record_handoff()` / `GroupRuntime.record_handoff()` 会把 `contract_assertions` 与 `verification_summary` 一并落到 handoff 契约里: [team_runtime.py#L50](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/team_runtime.py#L50), [group_runtime.py#L282](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/group_runtime.py#L282)
  - `SuperLeaderRuntime` 负责 materialize planning result 后按 lane 驱动 loop、为已完成 lane 写 authority-root handoff、调用 `reduce_group()`，再按 authority gate 汇总 objective-level `DeliveryState`: [superleader.py#L58](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/superleader.py#L58), [superleader.py#L105](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/superleader.py#L105)
  - `self_hosting.bootstrap` 则负责把 runtime 知识包里的优先缺口编译成 self-hosting `ObjectiveTemplate`；当 `use_dynamic_planning=True` 时，它会改为写入 `dynamic_workstream_seeds`，再调用默认的 `DynamicSuperLeaderPlanner` + `SuperLeaderRuntime` 跑一轮并导出 instruction packet: [bootstrap.py#L338](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/self_hosting/bootstrap.py#L338), [dynamic_superleader.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/planning/dynamic_superleader.py), [bootstrap.py#L480](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/self_hosting/bootstrap.py#L480)

`AgentOrchestra` 现在不只组装 store、bus、reducer、runner，也会在 `build_in_memory_orchestra()` 中预装默认四 backend 和 `DefaultWorkerSupervisor`: [orchestrator.py#L23](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/orchestrator.py#L23), [orchestrator.py#L43](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/orchestrator.py#L43)。

### 3.3 存储与总线层

当前已经把“真相”和“传输”拆开：

- `OrchestrationStore` 是 durable state 的统一边界，并且现在已经把 worker lifecycle 持久化纳入其中，新增 `save_worker_record()`、`get_worker_record()`、`list_worker_records()`: [storage/base.py#L14](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/storage/base.py#L14), [storage/base.py#L110](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/storage/base.py#L110)
- `InMemoryOrchestrationStore` 现在已经覆盖 `objective / spec node / spec edge / blackboard entry / blackboard snapshot / worker record`: [in_memory.py#L13](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/storage/in_memory.py#L13), [in_memory.py#L111](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/storage/in_memory.py#L111)
- PostgreSQL schema 形状现在已经预留 `objectives / spec_nodes / spec_edges / blackboard_entries / blackboard_snapshots / worker_records`: [models.py#L24](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/storage/postgres/models.py#L24), [models.py#L102](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/storage/postgres/models.py#L102)
- PostgreSQL 适配器本身在 [store.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/storage/postgres/store.py)，当前已经以 JSONB payload + typed columns 的方式为 group / team / objective / spec / task / handoff / authority / blackboard / worker / delivery 这些 covered entities 提供正式 CRUD
- Redis 事件总线在 [redis_bus.py#L17](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/bus/redis_bus.py#L17)

这与 `multi-agent-team-delivery` 里强调的 authority-root / handoff / reducer 分工是一致的：durable 状态不应该落在消息通道里，消息通道也不应该替代事实源。

### 3.4 Provider 与 CLI 层

`OpenAIResponsesAgentRunner` 是第一版 provider，负责把标准 `RunnerTurnRequest` 映射到 Responses API 请求形状，并把返回值压平为统一 `RunnerTurnResult`: [adapter.py#L34](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runners/openai/adapter.py#L34)。

CLI 则保持为薄封装，只做参数解析和轻量 handler 分发: [main.py#L10](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/cli/main.py#L10)。

## 4. 当前关键执行链路

当前代码里，协调链路和第一版执行链路都已经闭合到可测试程度：

1. `create_group()` 写 group 并发布 `group.created`: [group_runtime.py#L65](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/group_runtime.py#L65)
2. `create_team()` 要求 group 先存在，然后写入 team 并发布 `team.created`: [group_runtime.py#L71](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/group_runtime.py#L71)
3. `create_objective()`、`add_spec_node()`、`add_spec_edge()` 把 `Spec DAG` 契约图写进 store: [group_runtime.py#L87](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/group_runtime.py#L87), [group_runtime.py#L158](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/group_runtime.py#L158), [group_runtime.py#L192](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/group_runtime.py#L192)
4. `render_objective_template()`、`load_objective_template()`、`plan_from_template()` 把用户模板接到 planning 和持久化链路；这里既可以走 deterministic `TemplatePlanner`，也可以走先合成 bounded workstream 的 `DynamicSuperLeaderPlanner`: [io.py#L46](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/planning/io.py#L46), [template_planner.py#L33](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/planning/template_planner.py#L33), [dynamic_superleader.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/planning/dynamic_superleader.py), [group_runtime.py#L126](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/group_runtime.py#L126)
5. `submit_task()` 进入 `TeamRuntime.submit_task()`，生成 `TaskCard`、落库、发 `task.submitted`: [group_runtime.py#L216](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/group_runtime.py#L216), [team_runtime.py#L19](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/team_runtime.py#L19)
6. `update_task_status()` 和 `list_visible_tasks()` 把 scoped task list、auto-claim 和上视下 / 下视本 scope 规则落地: [group_runtime.py#L241](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/group_runtime.py#L241), [group_runtime.py#L260](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/group_runtime.py#L260)
7. `append_blackboard_entry()` 和 `reduce_blackboard()` 把 append-only entry log 与 snapshot reducer 接通: [group_runtime.py#L306](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/group_runtime.py#L306), [group_runtime.py#L343](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/group_runtime.py#L343)
8. `record_handoff()` 生成 `HandoffRecord` 并发 `handoff.recorded`，`reduce_group()` 再把 handoff 收敛为 `AuthorityState`: [group_runtime.py#L282](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/group_runtime.py#L282), [team_runtime.py#L50](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/team_runtime.py#L50), [group_runtime.py#L299](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/group_runtime.py#L299), [reducer.py#L12](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/reducer.py#L12)
9. `run_worker_assignment()` 把一个 `WorkerAssignment` 交给指定 backend，但真正的 reliability decision 现在落在 `DefaultWorkerSupervisor.run_assignment_with_policy(...)`：它会先做 route 级 worker policy，再把最终 success/failure 归一交给 `GroupRuntime.apply_execution_guard(...)`，因此 provider fallback 仍是 runtime-owned，而不是 backend-owned: [group_runtime.py#L158](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/group_runtime.py#L158), [worker_supervisor.py#L116](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/worker_supervisor.py#L116)
10. `DefaultWorkerSupervisor.start()` 仍先落 `launched / running` 状态；in-process 直接调 `AgentRunner.run_turn()`，out-of-process backend 则统一进入“等待进程结束 -> 读取 result 文件”的收敛路径；如果本轮来自 idle session 的 `reactivate`，这一步仍会在 runtime 层重新发起 turn，而不是把多轮控制权塞回 backend: [worker_supervisor.py#L147](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/worker_supervisor.py#L147), [worker_supervisor.py#L534](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/worker_supervisor.py#L534)
11. `SubprocessLaunchBackend` 和 `TmuxLaunchBackend` 共用 `worker_process` harness，统一走 assignment/result JSON 文件协议: [subprocess_backend.py#L58](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/backends/subprocess_backend.py#L58), [tmux_backend.py#L51](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/backends/tmux_backend.py#L51), [worker_process.py#L15](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/worker_process.py#L15)
12. `Subprocess / Tmux / CodexCli` 的 `resume()` 只负责验证 transport 仍可接管；一旦 resume 不可用，runtime policy 才会决定是否 relaunch: [subprocess_backend.py#L109](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/backends/subprocess_backend.py#L109), [tmux_backend.py#L100](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/backends/tmux_backend.py#L100), [codex_cli_backend.py#L304](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/backends/codex_cli_backend.py#L304)
13. `DefaultWorkerSupervisor.wait()` / `complete()` / `fail()` 会把 timeout、backend failure、成功结果都规范化成 `WorkerRecord`，再由 `_finalize_record(...)` 持久化 policy、attempt history、escalation metadata、`provider_routing` 和 `WorkerSession` snapshot；随后 `GroupRuntime.apply_execution_guard(...)` 会补上 guard 归一元数据并在需要时把 nominal success 改写成 authoritative failure: [worker_supervisor.py#L485](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/worker_supervisor.py#L485), [worker_supervisor.py#L593](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/worker_supervisor.py#L593), [group_runtime.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/group_runtime.py)
14. `materialize_planning_result()` 把 planning 结果桥接成一轮真实 leader/team round，创建 team、leader-lane task 和 bootstrap directive blackboard entry: [bootstrap_round.py#L138](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/bootstrap_round.py#L138)
15. `compile_leader_assignments()` 现在会在 leader prompt 中要求返回 machine-readable JSON，而不是纯自然语言: [bootstrap_round.py#L66](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/bootstrap_round.py#L66), [bootstrap_round.py#L233](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/bootstrap_round.py#L233)
16. `ingest_leader_turn_output()` 进一步把 leader 的 JSON 输出落成 team-scope `TaskCard`、leader-lane `execution_report`、overflow `proposal`，以及 runnable teammate assignments: [leader_output_protocol.py#L158](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_output_protocol.py#L158)
17. 这一条 hybrid round 已经实际跑过一轮，产物示例在 [2026-04-03-hybrid-team-round-1-result.json](/Volumes/disk1/Document/code/Agent-Orchestra/docs/superpowers/runs/2026-04-03-hybrid-team-round-1-result.json)，可以直接看到 leader assignment、leader output、ingested team tasks 和 teammate assignments: [2026-04-03-hybrid-team-round-1-result.json#L1](/Volumes/disk1/Document/code/Agent-Orchestra/docs/superpowers/runs/2026-04-03-hybrid-team-round-1-result.json#L1)

这条链路还不等于“生产可用”，但已经把多 team 协作里最容易丢失的 handoff -> authority 收敛链条先做成显式结构。

## 5. 已确认的上层框架基线

下面这些内容已经形成第一阶段代码事实，主要落在契约层、`GroupRuntime` 和 `InMemoryOrchestrationStore`；但它们仍不是完整生产运行时。

### 5.1 `Spec DAG` 保持为任务契约真相源

任务契约继续由 `Spec DAG` 承载，而不是由 task list 或 blackboard 替代。

它至少覆盖：

- `ObjectiveNode`
- `LeaderTaskNode`
- `TeammateTaskNode`
- 各类 gate 和依赖边

### 5.2 全局统一 task list，按 scope 暴露视图

task list 将尽量贴近 Claude 现有语义，但不再是单 team 私有目录，而是一个全局统一存储层。

逻辑上按 `scope` 暴露视图：

- `objective`
- `leader_lane`
- `team`

可见性规则已确认：

- 上层可以看到下层全部任务
- 下层只能看到本 scope 任务

也就是说：

- `SuperLeader` 可看全部 `leader_lane` 和 `team` 任务
- `Leader` 可看自己 lane 及自己 team 的全部任务
- `Teammate` 只能看自己 team scope 的任务

### 5.3 任务语义尽量与 Claude 保持一致

task list 的运行时操作面将尽量沿用 Claude 的简单语义：

- `pending`
- `in_progress`
- `completed`
- `blocked`
- `owner`
- `blockedBy`

关键参考：

- 新任务默认 `pending` 且无 owner: [prompt.ts#L12](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/tools/TaskCreateTool/prompt.ts#L12), [TaskCreateTool.ts#L80](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/tools/TaskCreateTool/TaskCreateTool.ts#L80)
- teammate 在把任务置为 `in_progress` 时，若无 owner 会自动 claim 给自己: [TaskUpdateTool.ts#L185](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/tools/TaskUpdateTool/TaskUpdateTool.ts#L185)

判断：

- 用户口头所说的“立即 open 生效”，在 Claude-compatible 语义里可理解成“创建后立即可做”，对应的实际状态仍使用 `pending + no owner`
- 这些语义现在已经进入 [task.py#L8](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/contracts/task.py#L8) 和 [group_runtime.py#L241](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/group_runtime.py#L241)，但仍是第一阶段实现，还没有完整多人竞争、审批和恢复流程

### 5.4 `LeaderLaneBlackboard` 与 `TeamBlackboard`

已确认的 Blackboard 分层是：

- `SuperLeader <-> Leader`: 一块 `LeaderLaneBlackboard`
- `Leader <-> whole team`: 一块 `TeamBlackboard`

其中：

- `TeamBlackboard` 对同 team 的所有 `Teammate` 全量可见
- Blackboard 底层已经按 `append-only entry log + reducer snapshot` 落在 [blackboard.py#L9](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/contracts/blackboard.py#L9)、[reducer.py#L29](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/reducer.py#L29) 和 [group_runtime.py#L306](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/group_runtime.py#L306)

### 5.5 `Teammate` 可直接创建 team-scope 任务

这条规则明确沿用 Claude 的用法：

- `Teammate` 可在当前 `team scope` 内直接创建新任务
- 新任务创建后立即可执行，采用 Claude-compatible 的 `pending + no owner`
- 但不能跨 scope 直接创建上层任务

新增增强约束是：

- 必须带 `derived_from`
- 必须带 `reason`

如果发现的是跨 scope 工作，则只能写入 `TeamBlackboard` 的 `proposal` / `blocker`

## 6. 基础设施边界

### 6.1 PostgreSQL 是 durable truth

PostgreSQL 相关代码当前已经覆盖：

- schema 生成：groups / teams / objectives / spec_nodes / spec_edges / tasks / handoffs / blackboard_* / authority_states: [models.py#L4](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/storage/postgres/models.py#L4)
- `worker_records` 表也已经一起预留: [models.py#L102](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/storage/postgres/models.py#L102)
- bootstrap 与 healthcheck: [store.py#L50](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/storage/postgres/store.py#L50), [store.py#L57](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/storage/postgres/store.py#L57)

判断：PostgreSQL 已经不再只是 schema-first skeleton；当前这层已经能为 covered runtime path 提供正式 persistence implementation。真正还没落地的是 migration discipline、query/index hardening、以及超出当前 covered slice 的 production durability 约束。

### 6.2 Redis 是 transient coordination

Redis 这层现在分成两层：

- channel 命名使用 `channel_prefix:event_kind`
- payload 序列化来自 `OrchestraEvent.to_dict()`
- `runtime.protocol_bridge.InMemoryMailboxBridge / RedisMailboxBridge` 已经把 mailbox cursor/list/ack contract 拉进主线路径，leader loop 与 superleader runtime 可以在不改 delivery 语义的前提下切换到 Redis-backed mailbox

核心入口在 [redis_bus.py#L39](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/bus/redis_bus.py#L39) 和 [redis_bus.py#L45](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/bus/redis_bus.py#L45)。

判断：Redis mailbox 主线路径已经进入 covered slice，但这一层还不是完整 mailbox/router/control-plane 系统，只是先把 canonical mailbox contract 和 transport 边界固定下来。

### 6.3 InMemory 是测试用最小落点

`InMemoryOrchestrationStore` 是当前测试通过的关键，因为它保证契约、runtime、blackboard reducer 和 worker lifecycle 都能先在无外部依赖条件下验证闭环: [in_memory.py#L13](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/storage/in_memory.py#L13)。

### 6.4 LaunchBackend 是本地 transport 层

这一层现在已经有清楚边界：

- `BackendRegistry` 只负责 backend 名称到实现的查找，不掺杂 orchestration 语义: [base.py#L41](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/backends/base.py#L41)
- `InProcessLaunchBackend` 现在会显式声明 `reactivate_supported`，并成为第一条可测试的 idle/reactivate transport；但它仍只覆盖当前 supervisor 进程内的 session，不是 durable reconnect: [in_process.py#L6](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/backends/in_process.py#L6)
- `SubprocessLaunchBackend` 和 `TmuxLaunchBackend` 都通过 assignment/result spool 文件把 worker transport 与 supervisor 解耦: [subprocess_backend.py#L58](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/backends/subprocess_backend.py#L58), [tmux_backend.py#L51](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/backends/tmux_backend.py#L51)
- runtime policy 现在明确压在 `GroupRuntime + DefaultWorkerSupervisor`，而不是压回 backend；backend 只声明“能不能 resume”，不声明“要不要 retry”: [group_runtime.py#L158](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/group_runtime.py#L158), [worker_supervisor.py#L224](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/worker_supervisor.py#L224)
- 这意味着当前 backend 层更接近“本地 transport 选择器”，还不是分布式 worker fabric

## 7. Runner 抽象与 OpenAI 落点

当前 `AgentRunner` 的统一生命周期是：

- `run_turn(...)`
- `stream_turn(...)`
- `cancel(...)`
- `healthcheck(...)`

具体定义在 [runner.py#L51](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/contracts/runner.py#L51)。

OpenAI 落点则分两部分：

- payload 组装：把 `previous_response_id`、conversation、tools 统一映射进 Responses 风格请求体: [adapter.py#L50](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runners/openai/adapter.py#L50)
- 返回值归一化：把 provider response 收敛成 `RunnerTurnResult`: [adapter.py#L64](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runners/openai/adapter.py#L64)

需要注意的 v1 限制是：`stream_turn()` 现在还是 single-shot fallback，不是真正的 provider-native streaming bridge: [adapter.py#L77](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runners/openai/adapter.py#L77)。

## 8. 当前 v1 限制

当前代码已经有清楚边界，但还没有这些能力：

- 当前已经支持第一版 `SuperLeaderRuntime` 与 `LeaderLoopSupervisor`，但还不是多 lane 并发 scheduler，也还没有 adaptive replan
- 当前 concrete backend 只覆盖本地 `in_process / subprocess / tmux / codex_cli` 四种 transport；remote/cloud worker、sticky fallback 和 backend health routing 还没有
- 当前 `DefaultWorkerSupervisor` 已经覆盖“一次逻辑 assignment 的 bounded retry/resume/relaunch/escalation”，并在 `in_process` 主线路径上补上了第一版 idle/reactivate；但它还没有 Claude 那种跨 transport 的长生命周期 worker loop、mailbox 驱动继续执行和 crash reconnect
- `subprocess` / `tmux` 现在通过 `worker_process` 走 deterministic harness；它们还没有直接接入真实 provider runner
- advanced planner logic：当前已经有 deterministic `TemplatePlanner` + bounded `DynamicSuperLeaderPlanner`，但还没有 LLM-assisted decomposition、planner memory、quality feedback loop 和 runtime feedback 驱动的 team 数量优化
- permission broker 当前只到 runtime-level hook，还没有 leader approval / sandbox policy / UI bridge
- identity / reconnect 当前只到 `DeliveryState.mailbox_cursor` 这种 runtime-level resume，还没有 actor scope、pending approval 恢复和进程级重连
- richer delivery state machine：现在已经有 lane/objective `DeliveryState`，但还没有 authority-aware `worker_complete / team_complete / objective_complete` 最终闭环

这里的判断来自对 Claude Code team 内核和当前代码落点的对照，不是当前仓库源码中直接存在的事实陈述。

## 9. 相关文档

- `README.md`
- `agent-orchestra-framework.md`
- `parallel-team-coordination.md`
- `claude-team-mapping.md`
- `../claude-code-main/agent-team.md`
- `../multi-agent-team-delivery/delivery-contracts.md`
