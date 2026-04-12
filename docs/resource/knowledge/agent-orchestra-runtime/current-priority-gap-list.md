# agent_orchestra 当前优先级 gap 列表

## 1. 一句话结论

截至 2026 年 4 月 10 日，`durable-supervisor-sessions`、`reconnector`、`protocol-bus` 已经从开放 gap 进入主线：`ACTIVE` session durability、lease reclaim、backend `reattach(...)`、Redis stream-family protocol bus，以及 self-hosting evidence gate 的 first-cut 都已落地。本轮原本仍挂着的 4 个实现点也已经从“缺失能力”推进到“主线 first-cut”：runtime-owned coordination commit helper、`task.receipt` live path、worker/agent session reconnect truth、以及 superleader lane session graph 都已接上；其中 superleader 这一支又前推了一段，ready lane 已通过 `LeaderLoopSupervisor.ensure_or_step_session(...)` + `ResidentSessionHost` 进入 host-owned leader-session launch boundary。与此同时，team 级协议切换也已经完成 baseline 收口：`max_concurrency` 已退出 live path、扁平 `teammate_tasks` 已由显式 `sequential_slices / parallel_slices` 取代，`TeammateWorkSurface.run(...)` + per-slot `TeammateOnlineLoop` 已经成为 team live path，self-hosting completion gate 也开始优先消费 runtime-native `teammate_execution_evidence`，`leader_consumed_mailbox` 只剩回退式诊断参考，不再是唯一关闭证明。本轮还新增完成了 `multi-leader-planning-review` 的 default first-cut：planning review contracts/store/runtime APIs、summary-first revision bundle、`SuperLeaderRuntime` pre-activation planning round、`ActivationGateDecision` live/runtime truth、以及 self-hosting gap inventory + instruction packet 接线都已落地，而且旧的“默认关闭 planning-review”路径已经移除，因此这条线不再属于 0 到 1 的开放 gap，而是归入 `online-collaboration-runtime` 的后续 hardening。2026-04-10 又把 task-surface authority truth 接进了这两条共享 read model：resident `lane_live_inputs` 现在会直接暴露 `task_surface_authority` snapshot，而 revision bundle 会自动注入 `authority_notices / project_item_notices` 与 structured metadata；因此当前 backlog 不再需要围绕“planning/runtime consumer 看不到 governed task surface”单独立项，而应继续把注意力放在 deeper runtime driver、policy、以及 host-owned convergence。authority 这条线则已经完成本阶段收口：shared `authority_reactor.py`、resident reactor metadata、teammate/session closure truth、以及 self-hosting `authority_completion` gate 都已进入主线，因此它不再是默认 P0 开放 gap。当前还应显式区分另一条后续边界：bootstrap/self-hosting 的 host-owned evidence rewrite 仍未完成，现有 delegation validation 仍依赖 host-owned leader session、runtime-native teammate evidence、`leader_turn_count == 1` 与 `mailbox_followup_turns_used == 0` 这组 gate，因此它应继续作为 runtime truth 稳定后的 follow-on，而不是拿来反向定义 session/coordination owner。

## 2. 范围与资料来源

- 当前状态来源：
  - [implementation-status.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/implementation-status.md)
  - [remaining-work-backlog.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/remaining-work-backlog.md)
  - [hierarchical-online-agent-runtime-gap-list.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/hierarchical-online-agent-runtime-gap-list.md)
  - [resident-team-collaboration-and-slice-planning.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/resident-team-collaboration-and-slice-planning.md)
  - [self-hosting-bootstrap.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/self-hosting-bootstrap.md)
- self-hosting gap catalog：
  - [bootstrap.py#L55](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/self_hosting/bootstrap.py#L55)
  - [bootstrap.py#L690](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/self_hosting/bootstrap.py#L690)

## 3. 当前优先级列表

下面这份列表服务于两层，但这两层当前并不是完全同速：

- 知识层的真实 backlog 排序
- self-hosting bootstrap 读取“建议优先级”时可直接识别的 gap 匹配语义

补充说明：

- 对已经进入 bootstrap catalog 的 gap，这两层应尽量保持一致。
- `authority-integration` 仍保留在 bootstrap catalog 里用于 targeted validation，但默认 backlog 不再把 authority 作为开放 P0 主项。

### 3.1 P0

1. `online-collaboration-runtime`
   - 保留原因：当前已经不只是 wave-1 骨架；`AgentSession / SessionBinding` 契约、扩展后的 `ResidentSessionHost`、thin `TransportAdapter`、leader continuous convergence、host-owned `TeammateWorkSurface.run(...)` + per-slot `TeammateOnlineLoop` live path、teammate recipient cursor/session truth 收口到 `TeammateWorkSurface + ResidentSessionHost`、runtime-owned `commit_directed_task_receipt(...) / commit_teammate_result(...)`、`task.receipt` live path、worker/agent session reconnect truth，以及 superleader 的 lane session graph 都已进入主线；并且 ready lane 已通过 `LeaderLoopSupervisor.ensure_or_step_session(...)` 进入 `leader_session_host` 边界。`commit_directed_task_receipt(...) / commit_teammate_result(...) / commit_authority_request(...) / commit_authority_decision(...)` 现在都已经对齐成 durable `coordination_outbox` + persisted task/blackboard/delivery/cursor/session readback，teammate live path 的 post-commit `save_session(...) / save_blackboard_entry(...)` 第二提交面也已删除。2026-04-10 之后，这条 commit family 又进一步把 `WorkerSession` 一起并入 receipt/result/authority/mailbox-consume coordination write：PostgreSQL 会在同一 coordination transaction 内提交 worker-session snapshot，而 non-transactional store 会立即补 worker-session fallback save，因此 stale `ACTIVE` worker session 与 stale mailbox cursor 已不再继续污染 authoritative host readback。superleader 这一支也已经从“只看 lane delivery snapshot + objective shared digest”推进成更深一层的 resident live view：lane delivery metadata 里的 `pending_shared_digest_count` / mailbox follow-up 信息、host-owned leader session projection，以及 objective `coordination / message_runtime / resident_live_view` 都会被 join 成 live input；2026-04-10 起，同一份 live input 还会继续暴露 `task_surface_authority` snapshot 与 `task_surface_authority_lane_ids / lane_task_surface_authority_waiting_task_ids`。planning 侧也已经不再把 governed task surface 留在文档语义：`build_leader_revision_context_bundle(...)` 会自动注入 `authority_notices / project_item_notices` 与 structured metadata。stale `PENDING` lane snapshot 也会让位给 host-owned `WAITING_FOR_MAILBOX` projection，而 self-hosting packet 现在会继续导出 `superleader_runtime_status`。因此当前残留缺口已经进一步收窄成更深层的 host-owned resident convergence：single-owner session truth、standalone teammate host、可重入/可恢复的 leader session runtime，以及把 digest/task-surface/review live input 从“wait/finalize export + planning notice”推进到“replan/rebalance/runtime driver”，外加 bootstrap/self-hosting validation 从当前 `host_owned_leader_session + teammate_execution_evidence + leader_turn_count == 1 + mailbox_followup_turns_used == 0` 的 follow-on heuristic，收口到真正依附 runtime truth 的 host-owned evidence gate
   - 正式拆解：后续应优先按 [hierarchical-online-agent-runtime-gap-list.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/hierarchical-online-agent-runtime-gap-list.md) 中定义的 4 段主线推进：
     - `agent-contract-convergence`
     - `team-primary-semantics-switch`
     - `superleader-isomorphic-runtime`
     - `cross-team-collaboration-layer`
   - 本轮新增约束入口：
     - [resident-team-collaboration-and-slice-planning.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/resident-team-collaboration-and-slice-planning.md)
### 3.2 P1

2. `sticky provider routing`
   - 保留原因：当前只有 per-assignment fallback/backoff；跨轮 provider health memory 和 sticky route 还没接上

3. `planner feedback`
   - 目标：把 deterministic template planning + bounded dynamic planning 推到 evaluator feedback 驱动的 replan
4. `permission broker`
   - 目标：把静态 allow/deny 收敛成更正式的 authority-aware runtime permission control plane
5. `PostgreSQL persistence`
   - 目标：补 query/index/migration/hardening，把 covered CRUD 推到更正式的 production path

### 3.3 P2

6. `worker lifecycle`
   - 说明：这里指的是对 `online-collaboration-runtime` 落地后的更深一层 long-lived resident worker / reconnect hardening，不是旧的一次性 worker lifecycle 缺口重开
7. `protocol native lifecycle wait`
   - 说明：它已不是 blocker；如果继续投喂，应理解成 recovery/control-plane hardening
8. `PostgreSQL persistence`
   - 说明：如果要优先做数据库主线加固，这条也可以提前到 P1

### 3.4 已关闭或已退出最高优先级的 gap

- `role-profile-unification`
  - 已关闭：runtime 当前主语义就是 contract-layer `WorkerRoleProfile`
- `runtime-owned coordination commit` / `task-receipt-live-path`
  - 已进入主线 first-cut：`GroupRuntime.commit_directed_task_receipt(...) / commit_teammate_result(...)` 与 `TeammateWorkSurface` live path 已经接上，而且 receipt/result 已对齐 authority path，进入 durable `coordination_outbox` 并返回 persisted task/blackboard/delivery/cursor/session truth；后续再提，应理解成第二 commit family cleanup 与 single-owner convergence，而不是“receipt/result 还没主线化”
- `worker-agent-session-truth`
  - 已进入主线 first-cut：`AgentSession.current_worker_session_id / last_worker_session_id`、`AgentWorkerSessionTruth`、`ResidentSessionHost.read_worker_session_truth(...)`、以及 `DefaultWorkerSupervisor` 的 host 写回路径都已落地；后续再提，应理解成 single-owner convergence，而不是“根本没有 reconnect truth”
- `superleader-session-graph`
  - 已进入更深一层 first-cut：`SuperLeaderLaneSessionState`、`lane_state.session` 与 `active_lane_session_ids` 都已进入协调状态，ready lane 也已通过 `ensure_or_step_session(...)` 进入 `leader_session_host` 边界；后续再提，应理解成“从 host-owned launch boundary 推进到可重入 / 可恢复的 host-stepped leader runtime”
- `message-pool-subscriptions`
  - 已关闭：append-only pool、subscription cursor、digest view 已进入 canonical bridge
- `message-visibility-policy`
  - 已关闭：`shared / control-private` 与 `summary_only / summary_plus_ref / full_text` 已进入 runtime-facing 主线
- `superleader-parallel-scheduler`
  - 已关闭：`SuperLeaderRuntime` 已按 lane budget 并发启动 ready lanes，并保留 dependency gate
- `leader-teammate-delegation-validation`
  - 作为旧 gap 已关闭：bootstrap 已把它收敛成真实 completion gate，并优先消费 runtime-native `teammate_execution_evidence`；`leader_consumed_mailbox` 现在只剩回退式辅助证据，不再是唯一关闭证明。但 host-owned evidence rewrite 还没完全结束：当前 `_delegation_validation_metadata(...)` 仍要求 `host_owned_leader_session && teammate_execution_evidence && leader_turn_count == 1 && mailbox_followup_turns_used == 0` 才会 `validated`。后续如果继续改这条线，应理解成 host-owned evidence gate cleanup，而不是回到旧 delegation heuristic
- `resident-hierarchical-runtime`（leader/superleader shared shell）
  - 已关闭：`LeaderLoopSupervisor` 与 `SuperLeaderRuntime` 都已通过共享 `ResidentCoordinatorKernel` 导出 `ResidentCoordinatorSession`
- `team-parallel-execution`
  - 已关闭：`LeaderLoopSupervisor` 已通过 `TeammateWorkSurface.run(...)` + per-slot `TeammateOnlineLoop` 与原子 task claim surface 落地 resident/autonomous claim，并已接入以 `teammate_execution_evidence` 为主语义的 self-hosting completion gate
- `team-budget-simplification` / `max_concurrency`
  - 已关闭：正式预算导出、self-hosting 默认 team 上限、leader 指令 budget payload，以及 resident teammate slot 并行上限都已切到 `max_teammates` 主语义；`max_concurrency` 已退出 live path，后续不应再按正式 team budget gap 重开
- `leader-slice-graph-output` / flat `teammate_tasks`
  - 已关闭：leader 主协议已经切到 `sequential_slices / parallel_slices`，旧 `teammate_tasks` 协议已停止接受；后续再提，应理解成更深地消费 slice metadata，而不是重新实现从 flat task list 到 slice graph 的 0 到 1 切换
- `durable-supervisor-sessions`
  - 已关闭：`DefaultWorkerSupervisor` 已支持 `ACTIVE` session durable truth、lease renewal、terminal persistence 与 `recover_active_sessions(...)`
- `identityscope / reconnector`
  - 已关闭：主线已支持 `list_reclaimable_worker_sessions -> reclaim_worker_session_lease -> backend.reattach(...) -> protocol wait`
- `protocol-bus`
  - 已关闭：`ProtocolBus / RedisProtocolBus / RedisEventBus stream-family protocol path` 与 self-hosting evidence gate 都已进入主线
- `authority-escalation-and-scope-extension`
  - 已关闭：policy、durable outbox、shared `authority_reactor.py`、teammate/session closure truth、以及 self-hosting `authority_completion` gate 都已进入主线；后续 authority 相关修改应视为 broader runtime cleanup 或 `permission-broker` 扩展，而不是继续作为独立开放 gap

## 4. 下一轮推荐投喂组合

如果下一轮优先补生产控制面，推荐直接投喂：

1. `online-collaboration-runtime`
2. `sticky-provider-routing`
3. `planner-feedback`

如果下一轮优先补存储与长期驻留，推荐投喂：

1. `online-collaboration-runtime`
2. `postgres-persistence`
3. `sticky-provider-routing`

如果下一轮优先补规划与生产化能力，推荐投喂：

1. `online-collaboration-runtime`
2. `planner-feedback`
3. `PostgreSQL persistence`

如果下一轮优先修正 team 内动态协作治理，推荐投喂：

1. `online-collaboration-runtime`
2. `task-subtree-authority`
3. `permission-broker`

## 5. Agent Orchestra 投喂方式

当前 self-hosting runtime 仍支持显式优先 gap 列表：

- `SelfHostingBootstrapConfig.preferred_gap_ids`
- `SelfHostingBootstrapConfig.max_workstreams`

直接入口：

- [bootstrap.py#L430](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/self_hosting/bootstrap.py#L430)
- [bootstrap.py#L842](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/self_hosting/bootstrap.py#L842)

推荐配置示例：

- `preferred_gap_ids = ("sticky-provider-routing", "planner-feedback", "permission-broker")`
  - 如果当前目标是推进 Claude 式长期在线协作，推荐：
  - 把 `online-collaboration-runtime` 继续放在第一位即可，不建议再单独围绕 `team-budget-simplification`、`leader-slice-graph-output`、或 `leader_consumed_mailbox` follow-up 之类旧语义立项
  - 对应 objective / task 文本应显式包含：
    - `store-level coordination transaction / single-owner session truth`
    - `standalone teammate host / host-owned leader session runtime`
    - `bootstrap/self-hosting evidence rewrite 是 runtime truth 稳定后的 follow-on`
    - `host-owned evidence gate cleanup，而不是重开 pre-mainline delegation semantics`
- `max_workstreams = 3`
- `leader_backend = "codex_cli"`
- `teammate_backend = "codex_cli"`

补充判断：

- bootstrap 默认并不读取本文件，而是默认读取 [implementation-status.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/implementation-status.md) 的“建议优先级”
- 对已经进入 bootstrap code catalog 的 gap，本文件与 `implementation-status.md` 的优先级顺序应保持一致
- `authority-integration` 当前更适合作为 targeted validation gap 使用，而不是默认 backlog 主项

## 6. 相关文档

- [implementation-status.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/implementation-status.md)
- [hierarchical-online-agent-runtime-gap-list.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/hierarchical-online-agent-runtime-gap-list.md)
- [resident-team-collaboration-and-slice-planning.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/resident-team-collaboration-and-slice-planning.md)
- [team-parallel-execution-gap.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/team-parallel-execution-gap.md)
- [online-collaboration-runtime-migration.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/online-collaboration-runtime-migration.md)
- [authority-escalation-and-scope-extension.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/authority-escalation-and-scope-extension.md)
- [authority-completion-gate-and-resident-reactor.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/authority-completion-gate-and-resident-reactor.md)
- [remaining-work-backlog.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/remaining-work-backlog.md)
- [self-hosting-bootstrap.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/self-hosting-bootstrap.md)
- [agent-abstraction-skill-and-prompt-system.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/agent-abstraction-skill-and-prompt-system.md)
