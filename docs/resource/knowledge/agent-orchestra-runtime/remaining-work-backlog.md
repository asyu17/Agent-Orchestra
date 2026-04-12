# agent_orchestra 剩余任务清单

## 1. 一句话结论

截至 2026 年 4 月 10 日，`durable-supervisor-sessions`、`reconnector`、`protocol-bus` 已经从 backlog 主项里移出：`ACTIVE` session durability、lease reclaim、backend `reattach(...)`、Redis stream-family protocol bus，以及 self-hosting evidence gate 都已进入主线。与此同时，上一轮审计里挂着的 4 个中层 gap 也已经从“缺失能力”推进到“主线 first-cut”：runtime-owned coordination commit helper、`task.receipt` live path、worker/agent session reconnect truth、以及 superleader lane session graph 都已接上；其中 superleader 这一支现在已经不再把 ready lane 直接变成 `leader_loop.run(...)` task，而是通过 host-owned `leader_session_host` 边界启动 leader session。再往前，这轮还新增补齐了 multi-leader planning-review first-cut：`planning_review.py` contract、in-memory/PostgreSQL store truth、`GroupRuntime` publish/list/bundle APIs、`SuperLeaderRuntime` pre-activation `draft -> peer review -> global review -> revision` round，以及 seeded lane activation 都已接上；而且现在它已经是默认主路径，不再保留旧的“默认关闭 planning-review”分支，因此已经不再属于“从 0 到 1 的开放 gap”，而是 `online-collaboration-runtime` 下的一条 residual hardening 线。结合最新目标，当前 backlog 的第一主项已经切换成 `online-collaboration-runtime` 的 residual hardening：把这些 first-cut helper 继续收敛成真正的 transaction-grade / host-owned / single-owner 主语义。authority 这条线也已经从 backlog 主项里移出：`AuthorityPolicy`、durable `CoordinationOutboxRecord`、shared `authority_reactor.py`、resident relay metadata、以及 self-hosting `authority_completion` gate 都已进入主线。当前正式剩余任务已经集中在 `online-collaboration-runtime`、`sticky-provider-routing`、`planner-feedback`、`permission-broker`、`postgres-persistence`，以及更深一层的 long-lived resident worker / reconnect hardening；而 bootstrap/self-hosting evidence rewrite 也已被明确下沉成 runtime truth 稳定后的 follow-on，而不是默认并行的主 backlog。

## 2. 范围与资料来源

- 当前状态与缺口来源：
  - [implementation-status.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/implementation-status.md)
  - [current-priority-gap-list.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/current-priority-gap-list.md)
  - [resident-team-collaboration-and-slice-planning.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/resident-team-collaboration-and-slice-planning.md)
  - [self-hosting-bootstrap.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/self-hosting-bootstrap.md)
- 关键代码入口：
  - [leader_loop.py#L742](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py#L742)
  - [leader_loop.py#L782](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py#L782)
  - [worker_supervisor.py#L1047](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/worker_supervisor.py#L1047)
  - [protocol_bridge.py#L725](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/protocol_bridge.py#L725)
  - [superleader.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/superleader.py)
- 当前验证基线：
  - `uv run pytest -q`
  - 当前 workspace 未附带可追溯的 full-suite artifact；若要把“当前 backlog 基线已跑完全量”写成 authority 结论，需要重新执行并保留结果，在未重跑前不固化精确通过数

## 3. 当前基线

### 3.1 已完成的主线

当前已经完成并验证进入主线的包括：

- `protocol-native-lifecycle-wait`
- `role-profile-unification`
- `message-pool-subscriptions`
- `message-visibility-policy`
- `superleader-parallel-scheduler`
- `leader-teammate-delegation-validation`
- `runtime-owned coordination commit` first-cut
- `task-receipt-live-path`
- `worker-agent-session-truth` first-cut
- `superleader-session-graph` first-cut
- `multi-leader-planning-review` first-cut

### 3.2 已新增进入主线的能力

本轮新进入主线的包括：

- `team-parallel-execution`
- durable idle session persistence / hydrate/reactivate v1
- store-backed reconnect cursor preference
- `ACTIVE` session durable persistence / lease renewal / terminal persistence
- `recover_active_sessions(...)` + backend `reattach(...)`
- `ProtocolBus / RedisProtocolBus / RedisEventBus` stream-family bus
- self-hosting 对 `durable-supervisor-sessions` / `reconnector` / `protocol-bus` 的 evidence gate
- `GroupRuntime.commit_directed_task_receipt(...) / commit_teammate_result(...)`
- directed teammate claim 的 `task.receipt` live emit
- receipt/result 与 authority 同步 durable `CoordinationOutboxRecord`，并回传 persisted task/blackboard/delivery/cursor/session truth
- `AgentSession.current_worker_session_id / last_worker_session_id`
- `ResidentSessionHost.read_worker_session_truth(...)`
- `ResidentSessionHost.project_worker_session_state(...)` 与 `GroupRuntime` authoritative host readback
- `SuperLeaderLaneSessionState / lane_state.session / active_lane_session_ids`
- `LeaderLoopSupervisor.ensure_or_step_session(...)` + `leader_session_host` launch boundary
- `authority-escalation-and-scope-extension` first-cut
- `authority-completion-gate-and-resident-reactor`
- `WorkerFinalReport.authority_request`
- `GroupRuntime.commit_authority_request(...)`
- `TaskStatus / DeliveryStatus.WAITING_FOR_AUTHORITY`
- leader authority-wait stop
- superleader authority-wait finalize

## 4. 正式剩余任务

### 4.1 `online-collaboration-runtime`

当前 resident shell、resident teammate runtime、protocol bus、reattach，以及 runtime-owned coordination helper 已有；缺的是：

- receipt/result/authority/mailbox consume 已共享 durable outbox + authoritative persisted snapshot boundary first-cut；其中 teammate live path 的 post-commit `save_session(...) / save_blackboard_entry(...)` 二次写回已在 2026-04-09 收回同一主语义，authority request / decision 也已补上 persisted readback。2026-04-09 同日，StoreBacked `ResidentSessionHost` 已在 continuation read path 上统一 `AgentSession + WorkerSession` 投影，`DefaultWorkerSupervisor` reclaim/finalize/reattach host write path 也已切到 worker-session-first projection。2026-04-10 又进一步把 `WorkerSession` 自己纳入这条 coordination write family：`commit_directed_task_receipt(...) / commit_teammate_result(...) / commit_authority_request(...) / commit_mailbox_consume(...)` 现在会连同 cursor/session/outbox 一起推进 worker-session snapshot，PostgreSQL 同事务提交，其他 store 至少立即补 fallback save。因此剩余工作进一步缩到更窄的 single-owner ownership，而不是继续被 stale `ACTIVE` worker session / stale mailbox cursor 这类 first-cut skew 卡住
- 正式 team budget 与 leader slice 主协议已经切到 `max_teammates` 与 `sequential_slices / parallel_slices`；当前剩余工作不再是重做这些切换，而是更深地消费 `depends_on_task_ids / parallel_group / slice_id` 这类 slice metadata，并避免任何 runtime/backlog 回到 `max_concurrency` 或 flat `teammate_tasks` 语义
- `task -> activation surface` 的正式主语义切换
- multi-leader planning-review 已切成默认主路径：当前 `draft -> peer review -> global review -> revision -> seeded activation` 会默认先于 lane activation 运行，后续真正剩下的是把 activation gate / review round truth 继续向更实时的 coordination gate 与更强的 review裁决收口
- `teammate` 真正常驻自主 online loop 的 end-to-end 接线；当前已经有 `task.receipt`、最小 directed mailbox work 和 slot online loop，但更完整的 envelope -> work protocol、以及更持久的 teammate mailbox state 还未完成
- `leader` 持续收敛 loop 的更深一层 cutover，使其不再只是 lane 内的 promptless convergence，而是真正的 `ensure_or_step_teammate_session` 式常驻协调中心
- `ResidentSessionHost` 继续从 supervisor-owned helper 推到更完整的 session truth owner，而不只是 supervisor 主链的 host API
- `WorkerSession / AgentSession / ResidentCoordinatorSession` 的 single-owner convergence
- bootstrap/self-hosting 的 host-owned evidence rewrite 仍未完成：当前 `_delegation_validation_metadata(...)` 会在 `host_owned_leader_session && teammate_execution_evidence && leader_turn_count == 1 && mailbox_followup_turns_used == 0` 时才判定 `validated`，因此这层应继续被视为 runtime truth 稳定后的 follow-on，而不是反向定义 coordination/session owner
- `SuperLeaderRuntime` 从当前的 host-owned launch boundary 继续推进到可重入 / 可恢复 / 可跨 superleader cycle step 的 leader session runtime
- superleader resident live view 已经会消费 lane digest/mailbox metadata、host-owned `WAITING_FOR_MAILBOX` leader session projection，以及 objective `coordination / message_runtime / resident_live_view`，并由 bootstrap 继续导出成 `superleader_runtime_status`；2026-04-10 起，它还会显式导出 `task_surface_authority` snapshot 与 `task_surface_authority_lane_ids / lane_task_surface_authority_waiting_task_ids`。与此同时，缺失 fresher live projection 时，superleader 现在会保留当前 lane coordination truth，而不是把 lane 错误打回 `PENDING`，并且 `RUNNING` delivery snapshot 也不会再被 `WAITING_FOR_MAILBOX` host phase 降级成非活动 lane。当前剩余工作因此不再是“补一层 live read”，而是把这层 digest/task-surface live input 从 launch-guard/wait/finalize export 推进成更完整的 replan / rebalance / directive runtime driver
- summary-first planning revision bundle 也已经不再只携带 peer-review digest：runtime 会自动注入 `authority_notices / project_item_notices` 与 structured metadata，把 governed task surface 与 advisory review/project-item signal 接进 planning consumer。当前剩余工作因此也不是“让 revision bundle 至少看见这些信号”，而是决定这些 advisory signal 何时、以多强的力度真正驱动 activation gate / replan / authority routing
- `TransportAdapter` 与 host 的更彻底分层，把目前 supervisor 内的 thin adapter 继续上提成更广义 runtime transport 语义

当前已经部分落地的 wave 1 包括：

- 统一 `AgentSession / SessionBinding / RolePolicy / ClaimPolicy / PromptTriggerPolicy` 契约
- 扩展后的 `ResidentSessionHost` 以及 worker-supervisor host write path
- thin `TransportAdapter` 与 supervisor 主链集成
- leader continuous convergence：除 teammate mailbox 外，也能在 open team task 仍存在时做 promptless drain
- superleader 对 lane promptless convergence 的配置透传，以及既有 blocked-dependency observation cycle
- `TeammateOnlineLoop` 已接进 leader 的 resident refill/claim 路径，并已支持最小的 directed mailbox teammate work：envelope payload 携带 `task_id` 时，会按 teammate slot inbox 路由、优先于 autonomous claim 进入 teammate 执行路径，且只有成功物化成 directed claim 才 ACK；但更完整的 teammate mailbox protocol 仍未完成
- `GroupRuntime` 已经开始提供 runtime-owned first-cut coordination commit，`task.receipt` 也已进入 live path；receipt/result 现在还已接上 durable outbox 与 authoritative persisted readback，但它们还不是最终的 single-owner coordination transaction
- superleader 已开始通过 `LeaderLoopSupervisor.ensure_or_step_session(...)` + `ResidentSessionHost` 直接管理 ready-lane launch boundary，并把 `launch_mode = leader_session_host` 投影回 lane graph；但它还不是可重入 / 可恢复的 leader session runtime

建议首先阅读：

- [online-collaboration-runtime-migration.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/online-collaboration-runtime-migration.md)
- [resident-team-collaboration-and-slice-planning.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/resident-team-collaboration-and-slice-planning.md)
- [resident-hierarchical-runtime.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/resident-hierarchical-runtime.md)
- [team-parallel-execution-gap.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/team-parallel-execution-gap.md)

### 4.2 authority 相关后续不再单列 backlog

`authority-escalation-and-scope-extension` 当前已经完成本阶段主语义：

- request / wait / decision / reroute / writeback 主线已进入 runtime
- policy 已收口到 `AuthorityPolicy`
- durable outbox 已收口到 `CoordinationOutboxRecord`
- shared `authority_reactor.py` 已进入 leader/superleader 主线
- teammate/session 已暴露 closure truth
- self-hosting 已改为消费 `authority_completion`

因此 authority 不再作为正式剩余任务单列。后续 authority 相关工作通常会并入：

- `online-collaboration-runtime`
- `permission-broker`
- `task-subtree-authority`

### 4.3 `sticky-provider-routing`

当前 per-assignment fallback/backoff/exhaustion 已有；缺的是：

- provider health scoring
- sticky route 选择
- cross-round provider memory

### 4.4 `planner-feedback`

当前 deterministic template planning 与 bounded dynamic planning 已有；缺的是：

- planner memory
- evaluator feedback 驱动的 replan
- runtime feedback 驱动的 decomposition 调整

### 4.5 `permission-broker`

当前已有静态 allow/deny contract；缺的是：

- 更正式的 authority / sandbox / policy 规则
- runtime permission orchestration

### 4.6 `postgres-persistence`

当前 covered CRUD 已有；缺的是：

- query/index discipline
- migration strategy
- production hardening

### 4.7 long-lived resident worker / reconnect hardening

当前主线已经具备 active reattach、protocol bus、worker/agent session truth first-cut，但更深一层的长期运行控制面还没做完。缺的是：

- 更长生命周期的 resident worker session
- reconnect after active crash 的更细粒度 control/takeover path
- 更强的 resident worker / mailbox / task-list 持续驱动

## 5. 已从 backlog 退出的条目

### 5.1 `protocol-native-lifecycle-wait`

已退出原因：

- `GroupRuntime` 已做 capability gate
- `codex_cli` / `worker_process` / `subprocess` / `tmux` 都已产出 protocol artifacts
- self-hosting codex path 也已有 protocol-native 回归

### 5.2 `role-profile-unification`

已退出原因：

- 当前 runtime 主语义就是 contract-layer `WorkerRoleProfile`
- leader/teammate assignment、runtime config、自举 instruction packet 都已使用同一形状

### 5.3 消息与并行类已关闭条目

下面这些都已进入主线：

- `message-pool-subscriptions`
- `message-visibility-policy`
- `superleader-parallel-scheduler`
- `leader-teammate-delegation-validation`
- `durable-supervisor-sessions`
- `reconnector`
- `protocol-bus`

## 6. 推荐的下一阶段执行顺序

推荐顺序：

1. `online-collaboration-runtime`
2. `sticky-provider-routing`
3. `planner-feedback`
4. `permission-broker`
5. `postgres-persistence`
6. long-lived resident worker / reconnect hardening

原因：

- 第一项先把系统主语义从“leader 派工”切到“长期在线协作”
- 第二、三项再补 adaptive/default routing 和 planner feedback
- 中间两项补 production control plane
- 最后一项继续把已落地的 durable runtime 往长期驻留推进

## 7. 与 self-hosting gap inventory 的关系

当前知识与 bootstrap gap inventory 的关系应固定为：

- `implementation-status.md` 的“建议优先级”是默认权威输入
- bootstrap gap catalog 负责把这些条目映射成稳定 `gap_id / owned_paths / verification_commands`

因此后续更新 backlog 时，优先维护知识文档里的优先级顺序和关键词，而不是先改 bootstrap 代码常量。

## 8. 相关文档

- [implementation-status.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/implementation-status.md)
- [current-priority-gap-list.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/current-priority-gap-list.md)
- [authority-escalation-and-scope-extension.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/authority-escalation-and-scope-extension.md)
- [authority-completion-gate-and-resident-reactor.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/authority-completion-gate-and-resident-reactor.md)
- [self-hosting-bootstrap.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/self-hosting-bootstrap.md)
- [worker-lifecycle-protocol-and-lease.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/worker-lifecycle-protocol-and-lease.md)
- [agent-abstraction-skill-and-prompt-system.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/agent-abstraction-skill-and-prompt-system.md)
- [online-collaboration-runtime-migration.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/online-collaboration-runtime-migration.md)
