# agent_orchestra 常驻 Team Shell 与 Attach-First 设计判断

## 1. 一句话结论

如果 Agent Orchestra 要真正接近 Claude team agent 的常驻体验，那么当前主设计不应继续把 `exact_wake` 或 `runtime_generation resume` 当成用户的第一心智，而应把“可 attach 的单 team 常驻 shell”提升成默认 operating surface：`attach` 成为一等动作，`Leader` 主要负责收件与收敛，`Teammate` 主要负责长期在线 claim/执行/回写，而 `exact_wake` 退回成 attach/recover 流程里的内部恢复手段。

## 2. 范围与资料来源

- 当前 Agent Orchestra 知识与设计：
  - `resource/knowledge/agent-orchestra-runtime/session-continuity-and-runtime-branching.md`
  - `resource/knowledge/agent-orchestra-runtime/resident-hierarchical-runtime.md`
  - `resource/knowledge/agent-orchestra-runtime/online-collaboration-runtime-migration.md`
  - `resource/knowledge/agent-orchestra-runtime/resident-team-collaboration-and-slice-planning.md`
  - `resource/knowledge/agent-orchestra-runtime/hierarchical-online-agent-runtime-gap-list.md`
  - `docs/superpowers/specs/2026-04-11-agent-orchestra-resident-team-shell-and-attach-first-design.md`
- 相关代码入口：
  - `src/agent_orchestra/runtime/session_host.py`
  - `src/agent_orchestra/runtime/group_runtime.py`
  - `src/agent_orchestra/runtime/leader_loop.py`
  - `src/agent_orchestra/runtime/teammate_work_surface.py`
  - `src/agent_orchestra/runtime/teammate_online_loop.py`
  - `src/agent_orchestra/runtime/worker_supervisor.py`
  - `src/agent_orchestra/cli/app.py`
- Claude Code 对照：
  - `resource/knowledge/claude-code-main/agent-team.md`
  - `resource/others/claude-code-main/src/utils/swarm/teamHelpers.ts`
  - `resource/others/claude-code-main/src/utils/swarm/reconnection.ts`
  - `resource/others/claude-code-main/src/utils/swarm/inProcessRunner.ts`
  - `resource/others/claude-code-main/src/hooks/useInboxPoller.ts`

下面出现的“判断”是基于以上代码与文档做出的架构推断，不表示仓库当前已经完成该设计。

## 3. 为什么 continuity first-cut 还不够

`WorkSession / RuntimeGeneration / ConversationHead` 这套 continuity 分层是必要的，但它解决的是：

- 会话 root 是什么
- 这一轮执行代际是什么
- 哪些 provider continuity 可以继承
- fork/new/warm resume 的边界是什么

它没有直接解决 Claude 那种体感最强的问题：

- 用户回来时到底 attach 的是什么
- 团队是否还“在线”
- leader 是否已经退到主要收件和协调
- teammate 是否已经能自己长期在线推进

如果没有额外的 resident shell 层，系统仍然容易把这些动作折叠成：

- resume generation
- exact wake runtime
- warm resume runtime

这些都很工程化，但不够像一个“还活着的 team shell”。

## 4. 新的主语义

### 4.1 `ResidentTeamShell`

建议在 continuity 层和 transport/runtime details 之间补一层：

- `ResidentTeamShell`

它是用户默认重新进入系统时 attach 的对象。

它回答：

- 当前哪个单 team shell 仍是 live operating surface
- shell 是 attached、idle、waiting、quiescent，还是 recovering
- leader slot 和 teammate slots 的稳定身份是什么
- 当前最合适的 attach target 是谁

### 4.2 Attach-first

新的用户动作优先级应改成：

1. `attach`
2. `wake`
3. `recover`
4. `warm_resume`
5. `fork_session` / `new_session`

判断：

- `exact_wake` 仍保留，但它应降级成 attach/recover 内部可能使用的一条实现路径
- 用户默认不该被迫先理解 generation 是否 exact-wakeable，用户首先只需要知道“我的 team 还在不在”

### 4.3 单 team resident 优先于 superleader resident

要尽量接近 Claude team agent 的常驻体感，优先级应该是：

1. 先把单 team resident shell 做对
2. 再把同一底座上推给 `SuperLeader`

判断：

- 如果 team 内还没有稳定的 resident shell，先做 superleader attach 只会把旧的 leader-turn 语义放大
- 单 team resident 稳定后，再 lift 到 `SuperLeader` 才不会把上层做成“更复杂的 scheduler”

## 5. 对现有对象边界的影响

### 5.1 Continuity 层保留

下面这些对象仍保留：

- `WorkSession`
- `RuntimeGeneration`
- `ConversationHead`

但它们的角色发生变化：

- continuity 层负责 lineage、resume/fork/new 语义
- resident shell 层负责默认 live operating surface

### 5.2 Host-owned truth 进一步收口

`ResidentSessionHost` 应继续收口成 live session truth owner。

判断：

- `WorkerSession` 应保留 transport/lease/process truth
- `AgentSession` 应更接近 agent-facing projection
- `ResidentCoordinatorSession` 应更接近 coordination-facing projection
- shell/slot/current owner/attach metadata 不应再由多个 runtime 模块并行维护

### 5.3 Teammate loop 不再由 leader 显式 step

新的 team 主执行面应是：

- teammate 自己 drain directed mailbox
- teammate 自己 claim runnable team work
- teammate 自己 publish result/blocker/proposal
- teammate 保持 idle-attached，而不是重新掉回 leader-owned stepping

leader 只保留：

- activation intent
- wake request
- convergence decision

## 6. 推荐实施顺序

### 6.1 第一阶段

先做：

1. `ResidentTeamShell` contracts/store/read model
2. `ResidentSessionHost` single-owner truth 收口
3. standalone teammate host loop
4. leader convergence-first resident loop
5. attach-first runtime/CLI

### 6.2 第二阶段

再做：

1. transport capability split
2. resident evidence gate rewrite
3. permission broker attach/idle approval queue
4. `SuperLeader` attach-first lift

判断：

- 第一阶段先把单 team resident 体感做出来
- 第二阶段再继续补生产化和上层推广

## 7. 与旧 continuity 设计的关系

2026-04-10 continuity 设计不是被推翻，而是被重新定位：

- 旧设计回答“会话与代际的正式语义是什么”
- 新设计回答“用户默认 attach 的 live surface 是什么”

两者关系应理解为：

- continuity 是 formal model
- resident shell 是 primary experience model

## 8. 2026-04-11 当前已落地的 first-cut

到 2026-04-11，这条 resident-shell 主线已经先落了 4 个底座切片，并补上了 3 个最小主线切片：

1. `RTS-01`
   - `ResidentTeamShell`、`ShellAttachDecision`、对应 status/mode enum，以及 `resident_team_shell_id` contract 已经进入 `session_continuity` contracts。
2. `RTS-02`
   - store contract 已补上 `save/get/list/find_latest_resident_team_shell(...)`
   - `InMemoryOrchestrationStore` 与 `PostgresOrchestrationStore` 都已经能持久化 shell record
   - PostgreSQL 已新增 `resident_team_shells` 表，保存 shell lookup/list 所需的标量列，以及完整 `payload`
   - `attach_state`、`teammate_slot_session_ids`、leader slot session 等 shell 细节当前先作为 durable payload truth 落盘，而不是提前扩成 runtime-owned read model
   - `find_latest_resident_team_shell(...)` 已按 `last_progress_at -> updated_at -> created_at` 选择当前更“活”的 shell，而不是只按初始创建时间
   - shell update path 会保留原始 `created_at`，避免 update 把历史创建时间冲掉
3. `RTS-03`
   - `ResidentSessionHost` 已补上 host-owned resident shell read model：`list_resident_team_shells(...)`、`inspect_resident_team_shell(...)`、`build_shell_attach_view(...)`、`find_preferred_attach_target(...)`
   - host 在 `save_session(...)` 驱动的 coordinator / teammate slot / worker projection写路径上会自动刷新 `ResidentTeamShell`，而不是让 runtime 在读路径上临时拼 shell truth
   - shell projection 会优先消费 host-owned leader `CoordinatorSessionState`、stable teammate slot roster、当前 binding / lease / phase / last_progress，而不是直接信 stale shell payload
   - runtime 侧目前只做一层很薄的透传：`GroupRuntime.read_resident_lane_live_views(...)` 会附带 host-projected `resident_team_shell` 和 attach recommendation，但不会自己重建 shell state
   - worker-supervisor 如果复用稳定 slot session id，会继续写回同一个 resident shell roster，而不是另造 competing synthetic owner session
4. `RTS-04`
   - `TeammateWorkSurface` 现在已经有了显式的 bounded host sweep 入口：`step_runnable_host_slots(...)` 会在不提供新 surface activation context 的情况下，仅依赖 host-owned runnable slot truth 继续推进 directed mailbox drain、autonomous claim、result publish 与 idle persistence
   - `GroupRuntime` 已补上独立 runtime facade：`run_resident_teammate_host_sweep(...)`，让外部 caller 可以不经 `LeaderLoop` 细节，直接触发同一条 host-owned continuation sweep
   - `LeaderLoop._ensure_or_step_teammates(...)` 也已收口成“fresh assignments 仍走同一个 work surface；纯 continuation 时改走 runtime facade”的薄 handoff，而不是继续持有额外的 per-slot stepping 逻辑
   - 当前 landed 的是 bounded host-owned continuation sweep，不是 detached teammate daemon：host 现在已经能独立推进 runnable slots，但长期 external wake loop / detached teammate host service 仍属于后续 `RTS-05/RTS-06`
5. `RTS-05`
   - `LeaderCoordinator` 与 `leader_loop` 已进入最小的 convergence-first resident 语义：当 leader mailbox 只包含 routine teammate evidence，或 lane 已经带着 resident open work 时，leader 会先走 promptless convergence，而不是无条件新开 prompt turn
   - 这意味着 resident lane 的“收件/收敛”已经开始独立于 fresh decomposition prompt；authority / planning / non-routine mailbox 仍然会显式触发 prompt turn
6. `RTS-06`
   - `GroupRuntime.attach_session(...)` 已经进入主线，live resident shell 会优先返回 `attached`
   - 现有 reclaim + `exact_wake(...)` 路径已被产品面降级成 attach flow 里的 `recovered`
   - durable continuity fallback 则暴露为 `warm_resumed`
7. `RTS-07` 的最小 CLI 面
   - CLI 已新增 `session attach` 与 `session wake`
   - `session wake` 现在采用 honest semantics，而不是假装已经有 detached wake daemon：live resident binding 会返回 `attached`，`exact_wake` reclaim path 会返回 `recovered`，其余情况会明确返回 `rejected`
   - `session inspect` / continuity inspect 也会继续附带 richer resident-shell view，包括 scope IDs、slot summary、attach recommendation 与 wake capability
8. `RTS-08`
   - `WorkerTransportClass` 与 `WorkerBackendCapabilities.transport_class` 已把 transport 正式拆成 `full_resident_transport` 与 `ephemeral_worker_transport`
   - `in_process / tmux` 现在会显式声明 full-resident；`subprocess / codex_cli` 会显式声明 ephemeral-worker
   - launch / resume / reattach handle metadata，以及 `DefaultTransportAdapter` 的 session hydrate path，也都会保留同一条 transport-class hint
   - 判断：这关闭了“只靠 resume/reactivate/reattach capability bits 猜 transport resident 程度”的旧问题，attach-first runtime 可以更稳定地区分 resident shell host 和可恢复但非 resident 的 bounded worker transport
9. `RTS-11`
   - permission broker 已不再只是 detached follow-on 词，而是进入 resident shell attach/idle 的 product read model：`ResidentTeamShell.metadata["approval_queue"]` 成为 attach / idle approval 的 canonical queue
   - `ResidentSessionHost.build_shell_attach_view(...)` 会同时导出 raw `approval_queue` 与 derived `attach_state.attach_approval_status / idle_wait_approval_status`
   - `find_preferred_attach_target(...)` 会先计算物理 attach target，再叠加 attach approval；当 attach approval 为 `pending/denied` 时，对外 attach decision 会直接变成 `rejected`
   - `LeaderLoopSupervisor.ensure_or_step_session(...)` 与 `TeammateWorkSurface._run_slot_online_loop(...)` 现在也会在 keep-idle finalization 时显式请求 `resident.idle_wait`；`denied` 会把最终 session 压成 `QUIESCENT`，`pending/approved` 则保留自然 idle-attached phase
10. `RTS-12`
   - `SuperLeader` resident live view 已经开始 lift 同一套 lane-level attach-first truth，而不是再造一层 superleader-owned shell：每条 lane 现在都会导出 `lane_shell_statuses / lane_attach_modes / lane_attach_targets`
   - per-lane live input 也会继续附带 lane-level `resident_team_shell` 与 `shell_attach` payload，因此 superleader cycle / inspect surface 可以直接消费 single-team attach-first base

判断：

- 这意味着 resident shell 已经不再只是设计词；它现在至少已经有正式 contract 和 durable store surface。
- 这意味着 resident shell 现在已经有了 contract、store、host-owned projection、runtime-readable inspect surface、bounded standalone teammate host sweep、convergence-first leader path、attach-first runtime/CLI，以及 attach/idle approval queue 与 superleader lane-level attach lift。
- 这意味着 `exact_wake` 已经不再是主产品词；对外心智已经开始变成 `attach -> recovered -> warm_resumed`，而不是先问 generation 是否 exact-wakeable；`session wake` 也已经以 honest reject/recover 语义落地，而不是继续拿未来 detached daemon 占位。
- 当前真正还没完成的已经收缩成两类更深的 follow-on：detached external wake loop / standalone teammate host service，以及 `WorkerSession / AgentSession / ResidentCoordinatorSession` single-owner truth 和更生产化的 permission broker / sticky routing / planner feedback / PostgreSQL hardening。

## 9. 相关文档

- `docs/superpowers/specs/2026-04-10-agent-orchestra-session-continuity-and-forking-design.md`
- `docs/superpowers/specs/2026-04-11-agent-orchestra-resident-team-shell-and-attach-first-design.md`
- `resource/knowledge/agent-orchestra-runtime/session-continuity-and-runtime-branching.md`
- `resource/knowledge/agent-orchestra-runtime/resident-hierarchical-runtime.md`
- `resource/knowledge/agent-orchestra-runtime/online-collaboration-runtime-migration.md`
- `resource/knowledge/agent-orchestra-runtime/resident-team-collaboration-and-slice-planning.md`
