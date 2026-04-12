# durable-teammate-loop-and-mailbox-consistency

## 1. 一句话结论

截至 2026-04-05，这条主线已经从“有 durable teammate 设计”推进到“live path 真正切过去”：`leader_loop` 的 mailbox helper 现在只处理 leader 自己的 inbox，teammate recipient 的 cursor/session truth 统一下沉到 `TeammateWorkSurface + ResidentSessionHost`；`TeammateWorkSurface.run(...)` 也已经改成 host-owned teammate slot online loop，并通过 `ResidentSessionHost.commit_mailbox_consume(...)`、`record_activation_intent(...)`、`record_wake_request(...)`、`record_teammate_slot_state(...)` 驱动 directed mailbox、task-surface claim、worker execution 和 idle/wake 状态。这轮原本仍挂着的 4 个点也已经进入 mainline first-cut：`GroupRuntime` 已有 runtime-owned coordination commit 表面，directed claim 会真实发出 `task.receipt`，worker session 与 agent session 已有显式 reconnect truth，superleader 也开始持有 lane session graph。到 2026-04-06，这条主线又补上了 teammate-owned verification loop：code-edit teammate assignment 不再只把 `verification_commands` 当作 metadata 传给 execution guard，而是会把 required verification set 编进 assignment execution contract，并要求 worker 在自己的 loop 里完成 `implement -> test -> fix -> retest` 后，再用 authoritative `final_report.verification_results` 收束。当前剩余重点已经收缩到更深一层的 transaction-grade commit、真正单一的 session truth owner、以及更彻底的 host-owned resident hierarchy。

## 2. 范围与资料来源

本专题文档最初针对下面三个 gap，现在同时记录它们已经落到哪一层：

- `teammate_online_loop` 不能继续停留在 leader-owned refill window，而需要进入 durable teammate-owned resident 主线
- mailbox ACK / consumer cursor / `DeliveryState` 不能继续各说各话，而需要明确 authoritative cursor 与 snapshot 的边界
- directed mailbox protocol 不能长期停留在最小 `payload.task_id` 兼容路径，而需要进入 versioned envelope/work 语义

本结论主要基于以下资料：

- 设计 spec：
  - `docs/superpowers/specs/2026-04-05-agent-orchestra-durable-teammate-loop-and-mailbox-consistency-design.md`
- 既有 runtime 知识：
  - `resource/knowledge/agent-orchestra-runtime/online-collaboration-runtime-migration.md`
  - `resource/knowledge/agent-orchestra-runtime/resident-teammate-online-loop-first-wave.md`
  - `resource/knowledge/agent-orchestra-runtime/resident-hierarchical-runtime.md`
  - `resource/knowledge/agent-orchestra-runtime/agent-abstraction-skill-and-prompt-system.md`
  - `resource/knowledge/agent-orchestra-runtime/data-contracts-and-finalization.md`
  - `resource/knowledge/agent-orchestra-runtime/worker-lifecycle-protocol-and-lease.md`
- 当前设计依赖的源码主语义入口：
  - `src/agent_orchestra/contracts/agent.py`
  - `src/agent_orchestra/runtime/session_host.py`
  - `src/agent_orchestra/runtime/transport_adapter.py`
  - `src/agent_orchestra/runtime/teammate_online_loop.py`
  - `src/agent_orchestra/runtime/teammate_runtime.py`
  - `src/agent_orchestra/runtime/leader_loop.py`
  - `src/agent_orchestra/runtime/directed_mailbox_protocol.py`
  - `src/agent_orchestra/runtime/protocol_bridge.py`
  - `src/agent_orchestra/storage/postgres/store.py`

如果后续源码实现与本文存在偏差，以源码和更晚的 spec 为准，并回写本知识文档。

## 3. 当前落地状态

这一轮之后，三条主线的当前状态分别是：

- durable teammate-owned loop：
  - `leader_loop` 已经会为 teammate slot 建立稳定 session id，例如 `team_id:teammate:1:resident`
  - slot 级状态通过 `ResidentSessionHost.record_teammate_slot_state(...)` 写入 `AgentSession.metadata`，而且 `last_active_at / idle_since` 已经变成真实 ISO 时间戳
  - `ResidentSessionHost` 现在已具备 `load_or_create_slot_session(...)`、`commit_mailbox_consume(...)`、`record_activation_intent(...)`、`record_wake_request(...)`
  - `contracts/agent.py` 已新增 `TeammateSlotSessionState`，让 slot metadata 有了 typed helper，而不再只是散落的 loosely-typed key/value
  - 初始 leader delegation 和后续 resident refill 现在都会尽量复用同一个 resident claim session id，self-hosting 的 `team-parallel-execution` completion gate 也已经重新闭合；当前关闭证明以 `teammate_execution_evidence` 为主语义，验证 resident claim/result/cursor/session 闭环，而不是单独依赖 `leader_consumed_mailbox`
  - live path 已不再是 `TeammateWorkSurface.run(...) -> ResidentTeammateRuntime.run(...)`。当前真正的主线是：`TeammateWorkSurface.run(...) -> pre-prime directed mailbox -> per-slot TeammateOnlineLoop -> execute_assignment(...)`
  - `teammate_work_surface` 现在直接负责 slot identity、activation profile、direct mailbox cursor、mailbox commit、slot wake/idle、初始 assignment reserve，以及主线 `execute_assignment(...)` 里的 permission gate、task status、blackboard result、`task.result` mailbox publish
  - directed teammate claim 现在会通过 `GroupRuntime.commit_directed_task_receipt(...)` 生成 runtime-owned receipt commit，并立即向 leader 发出 `task.receipt` envelope；receipt payload 已包含 `directive_id / claim_session_id / consumer_cursor / delivery_id / status_summary`
  - teammate result 路径现在也不再散落在 work surface 内部：`execute_assignment(...)` 会在 worker 完成后调用 `GroupRuntime.commit_teammate_result(...)`，把 task status、team blackboard 与 lane delivery snapshot 一起推进
- mailbox ACK / DeliveryState：
  - runtime 现在优先从 `OrchestrationStore.get_protocol_bus_cursor(stream=\"mailbox\", consumer=...)` 读取 authoritative consumer cursor
  - `leader_loop` 现在只处理 leader 自己的 inbox cursor；teammate directed consume 则优先统一走 `ResidentSessionHost.commit_mailbox_consume(...)`
  - `DeliveryState.mailbox_cursor` 已支持结构化 cursor payload round-trip，尤其 PostgreSQL 读回时不再把 dict 强行降回字符串
- worker/agent session truth：
  - `AgentSession` 已显式包含 `current_worker_session_id / last_worker_session_id`
  - `AgentWorkerSessionTruth` 与 `ResidentSessionHost.read_worker_session_truth(...)` 已成为统一读口
  - `DefaultWorkerSupervisor` 的 host-session 写回路径会持续把 worker session 绑定真相同步到 agent session
- directed mailbox protocol：
  - 新增 [directed_mailbox_protocol.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/directed_mailbox_protocol.py)
  - `leader_loop` 已改成先把 envelope `payload + subject` 规范化成 typed directive，再决定是否 claim/consume
  - canonical `task.directive` 和 legacy `task.directed + payload.task_id` 两条路径都可用
  - `task.receipt` 与 `task.result` 两类 envelope 都已进入 live path，同时继续保留 `compat.task_id`

还没有一起做完的是更深一层 hardening，例如把当前 runtime-owned coordination commit 下推成 store-level 单事务、把 worker session / agent session / coordinator session 进一步收敛成更单一的 reconnect truth，以及让 superleader 从 session graph metadata 继续推进到真正 host-owned leader session runtime。

## 4. durable teammate-owned loop 的设计结论

### 4.1 不新造第二套 runtime 主语义

这部分不应该重写成全新的 teammate runtime 家族，而应该直接复用当前已经存在的三层基座：

- `AgentSession`
- `ResidentSessionHost`
- `TransportAdapter`

也就是说，teammate 的 durable loop 不是 leader loop 的一个复杂 helper，而是 host 管理下的 resident agent session。

### 4.2 Leader 只做 activation 和 convergence

这一轮的边界非常明确：

- `Leader` 仍然负责 team activation、task decomposition、team-level convergence
- `Leader` 不再继续拥有 teammate slot 的 durable liveness 和 direct inbox cursor
- `Leader` 当前已经收缩到调用 `TeammateWorkSurface.run(...)` 这类 activation shell；下一步才是把这层继续推成更明确的 `ensure_or_step_teammate_session(...)`

这和 Claude team agent 的长期在线协作形态更接近，也和我们当前知识里“task 只负责点火，后续由在线 agent 持续推进”的方向一致。

### 4.3 teammate session 需要补齐的稳定语义

现有 `AgentSession` 已经覆盖了 session/binding/lease 的主语义，而这轮实现已经把下面这些字段固定成 slot metadata 的正式语义：

- `last_active_at`
- `idle_since`
- `current_task_id`
- `current_claim_session_id`
- `last_claim_source`
- `current_worker_session_id`
- `last_worker_session_id`
- `activation_epoch`

这些字段当前确实是通过 `AgentSession.metadata` 落盘的，但它们已经不再只是调试字段，而是 `leader_loop -> session_host -> self-hosting validation` 之间共享的 durable slot state。

### 4.4 teammate 的标准循环

durable teammate-owned loop 的标准循环应当是：

1. 读取自己的 durable `AgentSession`
2. 从 committed cursor 之后读取 direct/private mailbox
3. 先尝试 materialize directed work
4. 如果当前没有 directed work，再尝试 team scope 下的 autonomous claim
5. 一旦确定 work，先写 durable dispatch intent
6. 通过 `WorkerSupervisor / TransportAdapter` 执行
7. 发布 blackboard、mailbox result、session checkpoint
8. 提交 cursor 与状态
9. 回到 idle wait，而不是退出

这里最关键的不是“会不会 claim task”，而是“没有马上可执行的任务时，teammate 也仍然是活着的 resident agent，而不是本轮就结束掉”。

当前源码判断：

- 这条循环已经在 `TeammateWorkSurface._run_slot_online_loop(...)` 中进入 live path
- 但它仍然是 bounded online loop，而不是跨 leader run / 跨 host rebuild 持续常驻的独立 daemon

## 5. mailbox ACK 与 DeliveryState 的一致性设计

### 5.1 ACK 的重新定义

本轮必须固定一个更严格的定义：

- ACK 不是“message 被看到了”
- ACK 不是“bridge cursor 先动了”
- ACK 必须表示“durable side effect 已完成后，consumer cursor 已提交”

因此，真正的 ACK truth 应该是 committed consumer cursor，而不是 envelope 上的某个 convenience 字段。

### 5.2 durable mailbox truth 的两张主表

设计上建议把 mailbox durable truth 抽成两类实体。当前实现里，authoritative consumer cursor 先复用了 store 里的 `protocol_bus_cursor` 持久化表面，而不是再发明第二套 cursor store。

#### MailboxLog

append-only envelope log，主字段包括：

- `stream`
- `offset`
- `envelope_id`
- `sender`
- `recipient`
- `group_id`
- `lane_id`
- `team_id`
- `kind`
- `subject`
- `payload`
- `summary`
- `full_text_ref`
- `created_at`
- `metadata`

其中真正的顺序事实是 `offset`，`envelope_id` 主要用于幂等、关联和调试引用。

#### MailboxConsumerCursor

每个 consumer/view 的 committed cursor，主字段包括：

- `consumer_id`
- `view_kind`
- `view_id`
- `stream`
- `offset`
- `event_id`
- `last_envelope_id`
- `metadata`

这张表才是 consume/ACK 的 authoritative truth。

### 5.3 Coordination Commit Transaction

设计目标仍然是 directed teammate consume 通过一条统一事务提交以下内容。当前已落地的第一步是：consumer cursor 的 authoritative read/write 已进入 store 主线，bridge ACK 退化成镜像/兼容层，不再是唯一事实。

1. task claim 变更
2. dispatch intent 或 receipt
3. teammate session update
4. committed consumer cursor
5. `DeliveryState` 更新
6. blackboard append
7. protocol outbox events

因此设计上要明确：

- ACK = 同事务中提交完成的 consumer cursor
- `DeliveryState` = 同事务里的 runtime snapshot

不能再接受旧语义：

- 先 ACK
- 之后“希望”再写 `DeliveryState`

### 5.4 对 `DeliveryState` 的定位修正

`DeliveryState` 仍然有价值，但它不应再被当成 mailbox consume truth ledger。

更合适的定位是：

- 它记录 runtime 当前交付链路的快照
- 它和 task/team/worker session 状态一起参与 reducer / evaluator / diagnostics
- 它应该和 committed cursor 同事务写入
- 但 mailbox 真正的 consume truth 仍属于 `mailbox_consumer_cursor`

这能避免恢复时出现“`DeliveryState` 说已经消费了，但 mailbox cursor 其实没提交”或反过来的偏斜。

## 6. directed mailbox protocol 的升级结论

### 6.1 从兼容 payload 过渡到 versioned protocol

当前 `payload.task_id` 已经降级成迁移兼容层，runtime 主入口已经切换到 `parse_directed_task_directive(...)`。

后续 directed mailbox 的主协议建议固定为 versioned envelope，至少包含三种消息类型：

- `task.directive`
- `task.receipt`
- `task.result`

### 6.2 `task.directive` 应承载的主语义

`task.directive` 不应只包含一个 `task_id`，而应显式承载下面几类信息：

- protocol header
  - `name`
  - `version`
  - `message_type`
- directive identity
  - `directive_id`
  - `correlation_id`
  - `in_reply_to`
- task/work shape
  - `task_id`
  - `goal`
  - `reason`
  - `scope`
  - `derived_from`
  - `owned_paths`
  - `verification_commands`
- target identity
  - `worker_id`
  - `slot`
  - `group_id`
  - `lane_id`
  - `team_id`
  - `delivery_id`
- claim contract
  - `mode`
  - `claim_source`
  - `claim_session_id`
  - `if_unclaimed`
  - `expires_at`
- intent
  - `action`
  - `priority`
  - `requires_ack_stage`
- context
  - `leader_turn_index`
  - `leader_assignment_id`
  - `parent_task_id`
  - `source_blackboard_ref`

迁移期仍然保留：

- `compat.task_id`

这样当前最小消费路径不会被立刻打断。

### 6.3 `task.receipt` 与 `task.result`

为了让 directed mailbox 具备可恢复、可观察、可因果追踪的能力，receipt/result 已经需要作为标准协议进入主线；当前 runtime 也确实已经这样做了。

`task.receipt` 至少应包含：

- `directive_id`
- `receipt_type`
- `task_id`
- `claim_session_id`
- `consumer_cursor`
- `delivery_id`
- `status_summary`

`task.result` 至少应包含：

- `task_id`
- `status`
- `summary`
- `artifact_refs`
- `verification_summary`
- `correlation_id`
- `in_reply_to`

这样 directed mailbox 就不再只是“把任务 ID 通知一下”，而是形成可回放、可关联的工作协议。

## 7. 分阶段落地建议

### 7.1 Phase 0：冻结 canonical foundation

先明确 canonical foundation 是：

- `AgentSession`
- `ResidentSessionHost`
- `TransportAdapter`

不要在这个切片里再发明第二套 teammate runtime 主语义。

### 7.2 Phase 1：先把 teammate 变成 host-owned durable loop

第一阶段先解决 ownership 问题：

- teammate slot 变成 durable resident session
- leader 只负责 activate/step
- teammate 自己保持 idle wait、mailbox poll、task claim、child worker wait

### 7.3 Phase 2：做 transactional directed consume

第二阶段的 first-cut 已经进入主线，但“一致性窗口完全关闭”还没完成。当前已经有：

- `ResidentSessionHost.commit_mailbox_consume(...)`
- `GroupRuntime.commit_directed_task_receipt(...)`
- `GroupRuntime.commit_teammate_result(...)`

仍未完成的是把这些 runtime-owned helper 下推成 store-level 的协调事务。

最终目标仍然是：

- `mailbox_log`
- `mailbox_consumer_cursor`
- coordination commit transaction

### 7.4 Phase 3：升级 richer directed protocol

第三阶段目前也已经进入主线 first-cut。当前已经有：

- `task.directive`
- `task.receipt`
- `task.result`

并保留 `compat.task_id` 作为迁移过渡层。后续剩余工作主要是把 envelope/work schema 继续做丰富，而不是再去补“有没有 receipt/result 协议”。

### 7.5 Phase 4：继续缩减 leader ownership

当 durable teammate loop 稳定后，再把 leader 从“repeated dispatcher”进一步收缩成：

- activation center
- convergence center

当前代码已经完成了这一步的前半段：

- leader 不再自己运行 teammate refill online loop
- leader turn 现在会落盘 `teammate_execution_evidence / directed_claimed_task_ids / autonomous_claimed_task_ids`
- self-hosting validation 也开始优先消费这些 runtime-native teammate 证据

但后半段仍未完成：

- leader 仍然显式调用 `_run_teammates(...)` 做 activation shell
- standalone teammate host-owned online loop 仍未彻底形成

然后才适合继续把更上层的 superleader 推成 digest/mailbox-driven convergence runtime。

## 8. 设计验收标准

这轮实现完成后，下面几项已经变成真实验收项，而不是纸面目标：

1. teammate slot identity 是 durable 且 host-owned 的
2. 同一个 teammate slot 能跨多个 directed receive / claim 持续工作
3. ACK 的定义已经变成 committed consumer cursor after durable side effect
4. directed mailbox routing 能尊重 target slot 或 target worker 语义
5. 当前无法 claim 的 directive 不会被静默 ACK
6. 可以出现 `leader_requests == 1` 但 `teammate_requests > 1` 的场景
7. 系统恢复时能够基于 committed cursor 和 current child worker session 继续推进

截至当前代码，这几项已经通过针对性验证；知识层同时保留了一条历史全量回归入口：

- `uv run pytest tests/test_delivery_store.py tests/test_postgres_store.py tests/test_session_host.py tests/test_directed_mailbox_protocol.py tests/test_leader_loop.py -q`
- `uv run pytest tests/test_protocol_bridge.py tests/test_teammate_online_loop.py tests/test_delivery_contracts.py tests/test_delivery.py tests/test_worker_supervisor_protocol.py tests/test_superleader_runtime.py -q`
- `uv run pytest -q`

其中前两条是本文直接依赖的针对性验证；最后一条只保留为全量回归入口。若要把 full-suite 结论写成当前 authority，需要在当前 workspace 重新执行并保留结果 artifact，在未重跑前不继续固化精确通过数。

## 9. 相关文档

- `docs/superpowers/specs/2026-04-05-agent-orchestra-durable-teammate-loop-and-mailbox-consistency-design.md`
- `resource/knowledge/agent-orchestra-runtime/online-collaboration-runtime-migration.md`
- `resource/knowledge/agent-orchestra-runtime/resident-teammate-online-loop-first-wave.md`
- `resource/knowledge/agent-orchestra-runtime/resident-hierarchical-runtime.md`
- `resource/knowledge/agent-orchestra-runtime/agent-abstraction-skill-and-prompt-system.md`
- `resource/knowledge/agent-orchestra-runtime/data-contracts-and-finalization.md`
- `resource/knowledge/agent-orchestra-runtime/worker-lifecycle-protocol-and-lease.md`
