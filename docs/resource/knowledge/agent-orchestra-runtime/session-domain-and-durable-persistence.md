# agent_orchestra session 域与 durable persistence 重构判断

## 1. 一句话结论

当前仓库并不是“没有 session 持久化”，而是“已经有一组 session/continuity/shell/memory durable 对象，但默认入口、写入 owner、事务边界和产品语义还没有完全收口”；如果目标是不留技术债，最合理的方向不是继续补更多 session 字段，而是把 session 提升成独立 domain：以 PostgreSQL 作为唯一生产 durable truth，以单一 session-domain 写入口收口 `WorkSession / RuntimeGeneration / ConversationHead / ResidentTeamShell / SessionEvent`，同时继续把 `AgentSession / WorkerSession / ResidentCoordinatorSession / DeliveryState` 保持为 runtime-owned truth。

## 2. 范围与资料来源

- 当前知识与设计：
  - `resource/knowledge/agent-orchestra-runtime/session-continuity-and-runtime-branching.md`
  - `resource/knowledge/agent-orchestra-runtime/full-context-session-memory-and-hydration.md`
  - `resource/knowledge/agent-orchestra-runtime/resident-team-shell-and-attach-first.md`
  - `resource/knowledge/agent-orchestra-runtime/active-reattach-and-protocol-bus.md`
  - `resource/knowledge/agent-orchestra-runtime/implementation-status.md`
  - `docs/superpowers/specs/2026-04-10-agent-orchestra-session-continuity-and-forking-design.md`
  - `docs/superpowers/specs/2026-04-11-agent-orchestra-resident-team-shell-and-attach-first-design.md`
  - `docs/superpowers/specs/2026-04-11-agent-orchestra-full-context-session-memory-and-hydration-design.md`
  - `docs/superpowers/specs/2026-04-12-agent-orchestra-session-domain-and-durable-persistence-design.md`
- 当前代码入口：
  - `src/agent_orchestra/cli/main.py`
  - `src/agent_orchestra/cli/app.py`
  - `src/agent_orchestra/runtime/group_runtime.py`
  - `src/agent_orchestra/runtime/session_continuity.py`
  - `src/agent_orchestra/runtime/session_host.py`
  - `src/agent_orchestra/runtime/worker_supervisor.py`
  - `src/agent_orchestra/storage/base.py`
  - `src/agent_orchestra/storage/in_memory.py`
  - `src/agent_orchestra/storage/postgres/store.py`
- 当前验证入口：
  - `tests/test_session_continuity_store.py`
  - `tests/test_session_memory_store.py`
  - `tests/test_group_runtime_resident_shell.py`
  - `tests/test_worker_supervisor_protocol.py`

下面出现的“判断”是基于这些代码与文档做出的架构归纳，不表示仓库当前已经完全实现了对应目标状态。

## 3. 当前已经存在的 durable session 面

当前仓库已经有明确的 durable session 相关对象与写口：

- `WorkSession`
- `RuntimeGeneration`
- `WorkSessionMessage`
- `ConversationHead`
- `SessionEvent`
- `ResidentTeamShell`
- `WorkerSession`
- `AgentTurnRecord`
- `ToolInvocationRecord`
- `ArtifactRef`
- `SessionMemoryItem`

这些对象不仅存在 contract，而且 `InMemoryOrchestrationStore` 与 `PostgresOrchestrationStore` 都已有对应写口；`tests/test_session_continuity_store.py` 与 `tests/test_session_memory_store.py` 也已经证明了 first-cut round-trip。

判断：

- 从代码事实看，系统已经越过“session 完全没设计、也没落库”的阶段。
- 当前真正的问题不是缺少概念，而是这些概念之间还没有形成足够窄的单 owner 边界。

## 4. 为什么用户仍然会觉得“session 没持久化”

### 4.1 默认 CLI 路径仍然是内存态

`session` CLI 目前默认 `--store-backend=in-memory`。

这意味着：

- 用户如果直接跑默认命令，会得到看起来可用、但进程退出后就消失的 session 行为。
- 即使底层已经有 Postgres durable model，默认产品体验仍然会伪装成“session 没持久化”。

判断：

- 这是当前最表层、也最容易误导操作者的产品债。
- 只要这个默认值不改，后续再补更多 session 表，也仍然会持续制造“系统没有持久化”的错觉。

### 4.2 session 语义仍然散在多个 owner 上

当前与 session 邻域直接相关的 owner 至少包括：

- `SessionContinuityService`
- `ResidentSessionHost`
- `GroupRuntime`
- `DefaultWorkerSupervisor`

它们各自都在碰 session 邻域，但职责并没有完全收口成：

- 谁负责用户可见会话
- 谁负责 runtime 执行真相
- 谁负责 attach/shell read model
- 谁负责 continuity/hydration

判断：

- 这会导致“已经 durable，但不够 coherent”的现象。
- 用户感知到的不是某一张表缺失，而是系统缺少统一的 session 主心骨。

### 4.3 attach-first 产品面还没完全闭环

知识库已经明确了：

- `ResidentTeamShell` 应成为 attach-first 的默认 operating surface
- `exact_wake` 应该退回成内部恢复手段

但 detached wake service、single-owner attach orchestration、以及更完整的 resident host service 仍然属于后续主线。

判断：

- 如果 shell attach/wake/recover 还没有完全成为统一产品面，用户就仍然会把“能不能继续之前那个 session”理解成若干不同实现细节的拼接，而不是单一能力。

## 5. 不留技术债时的正确方向

### 5.1 把 session 提升成独立 domain

长期正确路线不是继续在 `GroupRuntime` 或 CLI 层堆 session 逻辑，而是新增独立 session domain。

这个 domain 至少应该统一负责：

- `WorkSession`
- `RuntimeGeneration`
- `ConversationHead`
- `ResidentTeamShell`
- `SessionEvent`

推荐的一等写入口是：

- `SessionDomainService`

判断：

- 这比“再加一些 helper”更重，但这是避免 session 技术债继续扩散的最小正确方案。

### 5.2 PostgreSQL 作为唯一生产 durable truth

生产语义下，session 不应继续允许 silent `in-memory` fallback。

应该明确：

- `in-memory` 只服务测试和极少数实验
- operator-facing `session new/list/inspect/attach/wake/fork` 默认必须走 Postgres
- 没有 DSN 时直接 fail-fast

判断：

- 这是最应该优先切的产品规则。
- 不切这条，session 的产品面永远会和底层 durable 设计相互打架。

### 5.3 runtime truth 与 hydration read model 必须继续分离

应继续维持下面边界：

- `WorkSession / RuntimeGeneration / ConversationHead / ResidentTeamShell / SessionEvent`
  - session domain
- `AgentSession / WorkerSession / ResidentCoordinatorSession / DeliveryState / mailbox / authority`
  - runtime truth
- `HydrationBundle / turn ledger / artifact ledger / semantic memory`
  - continuity read model

判断：

- 这一条不能为了“恢复体验更强”而倒退。
- 如果让 transcript 或 hydration 反过来定义 runtime 真相，系统会重新掉回 transcript-first ownership 的错误路线。

### 5.4 引入显式 session graph，而不是 flat session record

长期模型更适合理解成一张 session graph：

- `WorkSession`
  - 管用户会话 root
- `RuntimeGeneration`
  - 管每一代执行 epoch
- `ConversationHead`
  - 管每个 role scope 的 provider continuity
- `ResidentTeamShell`
  - 管 attach-first 的 live operating surface
- `SessionEvent`
  - 管 append-only audit trail

判断：

- 这比“再往 `WorkSession.metadata` 里塞东西”干净得多。
- 它也更符合当前仓库已经逐步形成的 continuity + shell + hydration 分层。

## 6. 推荐的事务与模块边界

### 6.1 单一 session-domain 写入口

不留债的方案里，下面这些模块不应该再直接定义更广义的用户可见 session 转移：

- `GroupRuntime`
- `ResidentSessionHost`
- `DefaultWorkerSupervisor`
- CLI handler

它们可以：

- 发起 command
- 报告 runtime fact
- 产出 projection input

但真正的 session durable write 应统一交给 session domain service。

### 6.2 新增 session transaction commit

当前代码已经有 `CoordinationTransactionStoreCommit` 这类思路。

建议继续抽出：

- `SessionTransactionStoreCommit`

用来原子提交：

- `WorkSession`
- `RuntimeGeneration`
- `ConversationHead`
- `ResidentTeamShell`
- `SessionEvent`

必要时再和 coordination commit 组合。

判断：

- 如果不把 session transaction 明确成一等概念，后续很容易继续出现“状态 durable 了，但边界不一致”的双真相窗口。

### 6.3 推荐的模块重切

建议新增：

- `src/agent_orchestra/runtime/session_domain/`

至少包括：

- `service.py`
- `commands.py`
- `models.py`
- `projection.py`
- `attach.py`
- `validation.py`

同时收缩现有模块：

- `session_continuity.py`
  - 保留 continuity 与 inspect 主逻辑
- `session_host.py`
  - 保留 host projection 与 attach view
- `group_runtime.py`
  - 退回高层 facade
- `worker_supervisor.py`
  - 只报 lifecycle/runtime 事实，不再隐式拥有 broader session policy

## 7. 当前已落地的 first-cut

截至本轮代码状态，下面这批 first-cut 已经进入主线：

- `src/agent_orchestra/runtime/session_domain.py` 已新增 `SessionDomainService` 与 `SessionResumeResult`，形成 user-visible session command 的统一委托边界。
- `src/agent_orchestra/runtime/group_runtime.py` 的 `new_session / list_work_sessions / inspect_session / warm_resume / fork_session / resume_gate / exact_wake / attach_session / wake_session` 已改为通过 `SessionDomainService` 委托，而不是继续把这组用户可见 session orchestration 完全留在 `GroupRuntime` 自己体内。
- `GroupRuntime.run_worker_assignment(...)` 里的 continuity hook 也已进一步改成通过 `SessionDomainService.apply_assignment_continuity(...)` 与 `record_worker_turn(...)` 委托，从而让 `GroupRuntime` 不再直接持有 `SessionContinuityService`。
- `src/agent_orchestra/cli/main.py` 里 `session` 子命令的 `--store-backend` 默认值已从 `in-memory` 切到 `postgres`；如果没有 `--dsn` 或 `AGENT_ORCHESTRA_DSN`，CLI 会显式失败，而不是再给出伪持久化体验。
- `src/agent_orchestra/cli/app.py` 已改为从新的 session-domain 模块导入 `SessionResumeResult`，从而把 CLI 的 attach/wake 结果类型与新的 domain 边界对齐。
- `src/agent_orchestra/storage/base.py` 已新增 `SessionTransactionStoreCommit`，`src/agent_orchestra/storage/in_memory.py` 与 `src/agent_orchestra/storage/postgres/store.py` 都已支持 `commit_session_transaction(...)`。
- `SessionTransactionStoreCommit` 现在不只覆盖 `WorkSession / RuntimeGeneration / ConversationHead / SessionEvent / ResidentTeamShell`，也已经继续扩到 `AgentTurnRecord / ToolInvocationRecord / ArtifactRef / SessionMemoryItem`，从而把 session graph 与 worker-turn ledger 合并进同一个 durable commit 边界。
- `src/agent_orchestra/runtime/session_continuity.py` 的 `new_session / warm_resume / fork_session / record_worker_turn` 都已切到 `commit_session_transaction(...)`；尤其 `record_worker_turn(...)` 现在会把 `ConversationHead` 更新、`conversation_head_updated` event、以及 turn/tool/artifact/memory ledger 一起原子提交，不再继续散写。
- `src/agent_orchestra/runtime/session_memory.py` 的 `record_role_turn / record_worker_turn / upsert_conversation_head` 现在也都改成构造 `SessionTransactionStoreCommit` 后一次提交，而不是各自直接调用 `save_conversation_head / append_turn_record / append_tool_invocation_record / save_artifact_ref / save_session_memory_item`。
- `src/agent_orchestra/runtime/resident_wake_service.py` 已新增 `ResidentWakeService`；`SessionDomainService.wake_session(...)` 现在会对 quiescent resident shell 记录 durable wake request，并回写 `resident_shell_wake_requested` session event，而不是再一律 honest reject。
- `src/agent_orchestra/runtime/session_host.py` 的 attach recommendation 也已同步把 quiescent shell 视为 `woken` 候选，而不是直接把它们全部压成 `warm_resumed`。
- `tests/test_cli.py` 已补上默认 durable backend 与显式 volatile opt-in 的回归，并把 `main(["session", ...])` 的行为改成：默认无 DSN 时失败，显式 `--store-backend in-memory` 时才允许跑内存态。
- `tests/test_runtime.py` 已补上 `GroupRuntime(session_domain_service=...)` 的委托边界测试，证明 user-visible session calls 可以经由注入的 domain service 统一接管。
- `tests/test_session_domain.py`、`tests/test_group_runtime_resident_shell.py` 与 `tests/test_cli.py` 现在都已直接覆盖 quiescent resident shell 的 `wake -> woken` 行为，以及 wake-request metadata，而不是继续只覆盖 reject path。
- `tests/test_session_continuity_service.py`、`tests/test_session_continuity_store.py` 与 `tests/test_postgres_store.py` 现在也已直接覆盖扩展后的 session transaction commit：不仅 round-trip session graph，也 round-trip worker turn / tool / artifact / memory ledger。
- 2026-04-12 又补上了一轮更底层的 PostgreSQL hardening，专门修复 3 条 durable write failure pattern：
  - `src/agent_orchestra/storage/postgres/store.py` 的 blackboard append 不再把 `BlackboardEntry.created_at=None` 原样写进 `blackboard_entries.created_at`；store 会在落库前显式补时间戳，并把同一时间戳写回 payload，从而避免 runtime 构造默认值为空时出现 durable row/payload 偏斜。
  - 同一个 Postgres store 的 JSON serializer 不再假设所有 metadata 都天然可 JSON 化；未知运行时对象现在会在 durable write 前降成字符串，因此 `WorkerRecord.handle.metadata` 里像 `_process` 这类 opaque runtime handle 不会再把 `worker_records` 持久化直接打成 `TypeError`。
  - `ResidentSessionHost` 与 `PostgresOrchestrationStore.save_resident_team_shell(...)` 现在共同收紧了 `resident_team_shells.work_session_id` 的 durable 前置条件：host 在缺失或失配 continuity root 时不再尝试持久化 shell，而 PostgreSQL store 也会拒绝引用不存在 `WorkSession` 的 shell 写入；`SessionTransactionStoreCommit` 同时允许“本事务内新建 work session + resident shell”一起提交，避免把 attach-first shell 再次变成游离于 session graph 之外的弱引用。
- 同一天的 second-pass scan 又确认并修复了同一家族的更广泛问题：真实 PostgreSQL 里，`runtime_generations / work_session_messages / conversation_heads / session_events / agent_turn_records / tool_invocation_records / artifact_refs / session_memory_items / resident_team_shells` 都依赖 `work_sessions.work_session_id`，但 fake store 不会自动模拟这层 foreign-key owner。当前 `PostgresOrchestrationStore` 已把这些 session 子记录统一收口到同一条显式前置校验：
  - direct save/append path 会在落库前拒绝缺失或空的 `work_session_id`
  - `SessionTransactionStoreCommit` 允许“本事务内新增 work session + 其子记录”一起提交，不会误伤合法 first-write path
  - 相应的 Postgres 回归测试也已补上“非法 direct save 必须显式报错”与“原本缺失 root fixture 的测试必须先创建 `WorkSession`”这两类覆盖
  - 判断：这说明 session-domain 的 durable 正确性不能只依赖数据库层最终报错；store 自己也需要把 owner invariant 显式化，否则 fake store / unit test 会长期掩盖真实 Postgres 失败模式
- 下一轮 hardening 又继续把这条规则推进到更细粒度的 durable sort key 与二级引用：
  - `WorkSession.created_at / updated_at`、`SessionEvent.created_at`，以及同类 required timestamp 现在会在 Postgres store 落库前被标准化：空值会补当前 ISO 时间，非空但非 ISO 的字符串会被显式拒绝，而不是继续把空串写进 `TEXT NOT NULL` 列后再污染 `ORDER BY created_at` 的语义。
  - `ConversationHead / WorkSessionMessage / SessionEvent / AgentTurnRecord / ToolInvocationRecord / ArtifactRef / SessionMemoryItem / ResidentTeamShell` 这些挂在 generation 上的实体，现在不只校验 `work_session_id`，也会校验 `runtime_generation_id` 是否真实存在且属于同一个 session；`SessionTransactionStoreCommit` 同样允许“本事务内新增 generation + 其子记录”一起提交。
  - `ToolInvocationRecord.turn_record_id` 与 `ArtifactRef.turn_record_id / tool_invocation_id` 现在也已进入显式 application-level guard，而不是只依赖 PostgreSQL FK 或等 fake store 漏过去以后再在 consumer 侧暴露脏数据。
  - 这条 hardening 还向 review/task 侧扩了一小步：`TaskReviewSlot / TaskReviewRevision` 现在会先校验 `task_id`，`ReviewItemRef` 会先校验 `objective_id` 与可选 `source_task_id`，而 `TeamPositionReview / CrossTeamLeaderReview / SuperLeaderSynthesis` 会先校验 `item_id`；对应测试 fixture 也都改成先创建 owner record，再写 dependent row。

判断：

- 这还不是 session-domain 重构的终态；当前 landed 的是 detached wake 的 first-cut durable command/service，而不是完整的长期外部 wake sweep owner。真正独立长期运行的 resident wake loop、以及 `ResidentSessionHost` 里剩余 shell direct-write path 的继续收口，仍属于后续主线。
- 但系统已经越过“只有文档没有落地”的阶段：operator-facing durable default、统一 session command facade、worker-turn atomic session transaction、以及 quiescent shell 的 durable wake-request path 都已进入主线。
- 新补上的这轮 Postgres hardening 说明：session-domain 的正确性不只取决于“有没有对应表”，还取决于每条 durable write path 是否先把默认值、opaque runtime state 与 foreign-key owner 前置条件收口干净。否则表结构已经存在，runtime 仍会在最细的持久化边界上继续泄露 host-only truth。

## 8. 推荐迁移顺序

### 8.1 第一阶段

先切产品面最危险的默认值：

- session CLI 不再默认 `in-memory`
- 没有 DSN 时 fail-fast

### 8.2 第二阶段

引入 `SessionDomainService`：

- 新 session
- fork
- attach
- wake
- recover
- continuity invalidation

全部收口到统一 command path。

### 8.3 第三阶段

补上 session transaction commit，并让 attach-first 主路径走同一 durable session graph。

### 8.4 第四阶段

把 detached wake / resident host service 真正拉成独立长期运行 owner，而不是继续让 CLI 代管。

当前状态：

- first-cut `ResidentWakeService` 已 landed，并且 `session wake` 已不再对 quiescent shell 一律 reject
- 但它现在仍然主要负责 durable wake request 与 host-owned phase bump，不是完整的 detached host sweep daemon

### 8.5 第五阶段

删掉 legacy direct-write path，避免新旧双写长期并存。

## 9. 推荐 checklist

### 9.1 立刻可执行 checklist

- [ ] 先确认当前问题到底是默认 `in-memory` 路径导致的产品假象，还是 deeper session-domain ownership 问题。
- [ ] 任何 operator-facing `session` 命令都先确认是否真的走了 Postgres。
- [ ] 在继续补 session 功能前，先确认不会再新增散落的 direct-write path。
- [ ] 在继续补恢复体验前，先确认不会让 hydration/transcript 反向定义 runtime truth。

### 9.2 设计收口 checklist

- [ ] 把 `WorkSession / RuntimeGeneration / ConversationHead / ResidentTeamShell / SessionEvent` 视为同一 session graph。
- [ ] 把 `AgentSession / WorkerSession / ResidentCoordinatorSession / DeliveryState` 明确留在 runtime truth 层。
- [ ] 只允许一个 session-domain 写入口定义用户可见 session 转移。
- [ ] attach-first 的产品决策只从 `ResidentTeamShell` 出发，不再从多个 helper 临时拼装。
- [ ] `exact_wake` 继续作为内部恢复路径，而不是产品主心智。

### 9.3 交付完成 checklist

- [ ] CLI 不再默默落回 `in-memory`。
- [ ] session inspect 能稳定展示 durable session graph。
- [ ] attach/wake/recover/warm-resume 路径都有明确测试与失败语义。
- [ ] worker turn 的 `ConversationHead + SessionEvent + turn/tool/artifact/memory` 已进入同一个 commit 边界。
- [ ] quiescent resident shell 会留下 durable wake request 与 `resident_shell_wake_requested` event，而不是继续只给 transient reject。
- [ ] 新代码没有把 broader session policy 再塞回 `GroupRuntime`、`SessionHost` 或 `WorkerSupervisor`。

## 10. 对后续 agent 的直接建议

如果后续任务再涉及 “session 为什么没持久化” 或 “session 该怎么持久化”，应优先判断两件事：

1. 是不是只是走了 `in-memory` 默认路径，导致产品面看起来没持久化。
2. 是不是已经进入 deeper architecture 话题，即需要继续做 session-domain 收口，而不是继续补字段或补一层 summary。

优先读：

1. `session-continuity-and-runtime-branching.md`
2. `full-context-session-memory-and-hydration.md`
3. `resident-team-shell-and-attach-first.md`
4. `session-domain-and-durable-persistence.md`

## 11. 相关文档

- `session-continuity-and-runtime-branching.md`
- `full-context-session-memory-and-hydration.md`
- `resident-team-shell-and-attach-first.md`
- `active-reattach-and-protocol-bus.md`
- `docs/superpowers/specs/2026-04-12-agent-orchestra-session-domain-and-durable-persistence-design.md`
