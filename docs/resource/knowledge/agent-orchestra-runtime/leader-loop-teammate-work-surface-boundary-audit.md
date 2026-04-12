# leader_loop teammate work surface 边界审计

## 1. 一句话结论

截至 2026-04-05，`leader_loop` 里最关键的两轮外移已经完成：主线上的 teammate `execute/publish` side effect 已经进入 `teammate_work_surface.execute_assignment(...)`，而 resident teammate runtime 的组装也已经进入 `teammate_work_surface.run(...)`。因此 `permission gate -> task status -> blackboard result -> task.result mailbox publish` 不再由 leader 主线直接落盘，`_run_teammates(...)` 也不再保留第二套 teammate execution truth。当前剩余最该继续外移的部分，已经收缩到 teammate mailbox/task/session truth 的更深层 host-owned 语义。相对地，leader-specific mailbox、prompt turn 触发、lane delivery evaluation、shared digest/subscription、以及 runtime task 完成语义，仍属于 `LeaderLoopSupervisor` 的 team-level convergence 主线。

## 2. 范围与资料来源

本审计面向“把 leader 收缩成 activation center + convergence center”这一条主线，只讨论 `src/agent_orchestra/runtime/leader_loop.py` 中剩余的 teammate-owned 逻辑边界，以及它和现有 `ResidentTeammateRuntime`、`ResidentSessionHost`、mailbox cursor/directed protocol 的关系。

主要依据：

- 现有知识文档：
  - [online-collaboration-runtime-migration.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/online-collaboration-runtime-migration.md)
  - [resident-teammate-online-loop-first-wave.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/resident-teammate-online-loop-first-wave.md)
  - [durable-teammate-loop-and-mailbox-consistency.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/durable-teammate-loop-and-mailbox-consistency.md)
  - [implementation-status.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/implementation-status.md)
- 直接审计源码：
  - [leader_loop.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py)
  - [teammate_runtime.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/teammate_runtime.py)
  - [teammate_online_loop.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/teammate_online_loop.py)
  - [session_host.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/session_host.py)
  - [group_runtime.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/group_runtime.py)
  - [leader_coordinator.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_coordinator.py)
  - [leader_output_protocol.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_output_protocol.py)
  - [directed_mailbox_protocol.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/directed_mailbox_protocol.py)

## 3. 当前边界总览

### 3.1 当前已经下放给 teammate runtime 的部分

源码事实：

- `ResidentTeammateRuntime.run(...)` 已经接管 runnable assignment drain、slot refill sequencing、claim evidence 聚合，以及 idle/quiescent 判断：[teammate_runtime.py#L77](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/teammate_runtime.py#L77)
- `LeaderLoopSupervisor` 当前不再自己跑 `TeammateOnlineLoop`；它只把 `execute_assignment` 与 `acquire_assignments` 注入给 teammate runtime：[leader_loop.py#L1051](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py#L1051)

判断：

- “何时继续拿活”的节奏已经不是主要问题。
- 真正还黏在 `leader_loop` 里的，是“拿什么活、根据哪个 cursor、用哪个 slot/session identity、执行后往哪里回写”这组 teammate truth。

### 3.2 当前仍卡在 leader_loop 里的 teammate-owned truth

源码事实：

- teammate slot identity 与 claim/session id helper 仍定义在 `leader_loop.py`：[_teammate_slot_from_worker_id](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py#L317)、[_resident_claim_session_id](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py#L329)、[_resident_teammate_session_id](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py#L341)、[_resident_teammate_session_id_for_recipient](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py#L349)
- teammate pending assignment 物化仍在 `leader_loop.py`：[_build_pending_teammate_assignment](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py#L356)
- teammate slot session 建立、slot metadata 写入、task claim、task status 更新、result blackboard append、result mailbox publish 仍在 `leader_loop.py`：[_ensure_teammate_slot_session](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py#L516)、[_run_teammate_assignment](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py#L824)
- teammate direct mailbox consume、authoritative cursor load、directed/autonomous claim、ACK、pending assignment build 仍在 `leader_loop.py`：[_acquire_teammate_assignments](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py#L1095)

### 3.3 当前明确属于 leader convergence 的部分

源码事实：

- leader prompt 输入构造与 leader assignment 编译仍是 leader 专属动作：[_build_turn_input](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py#L112)、[compile_leader_turn_assignment](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py#L457)
- leader promptless convergence 的开关和状态机已抽到 `LeaderCoordinator`，但运行仍由 `LeaderLoopSupervisor.run(...)` 主持：[leader_coordinator.py#L10](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_coordinator.py#L10), [leader_loop.py#L1378](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py#L1378)
- lane delivery evaluation、runtime task status 更新、delivery state 持久化、mailbox follow-up budget 都在 `run(...)` 中完成：[leader_loop.py#L1441](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py#L1441), [leader_loop.py#L1600](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py#L1600), [leader_loop.py#L1880](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py#L1880)
- shared subscription/digest 是 leader-superleader convergence 视角，不是 teammate direct inbox 视角：[_ensure_default_shared_subscriptions](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py#L738)、[_collect_shared_digests](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py#L701)、[_collect_subscription_cursors](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py#L720)

## 4. 不应长期留在 leader_loop 的 teammate-owned 逻辑

### 4.1 slot identity 与 resident session truth helper

应迁出条目：

- [_teammate_slot_from_worker_id](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py#L317)
- [_resident_claim_session_id](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py#L329)
- [_resident_teammate_session_id](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py#L341)
- [_resident_teammate_session_id_for_recipient](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py#L349)
- [_ensure_teammate_slot_session](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py#L516)

原因：

- 这些 helper 定义的是 teammate slot 的稳定身份、claim session 命名和 resident session existence，不是 leader convergence 规则。
- `session_host.record_teammate_slot_state(...)` 已经把 slot state 定位成 durable `AgentSession.metadata`，说明这层 truth owner 理应靠近 teammate runtime/host，而不是 leader loop：[session_host.py#L104](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/session_host.py#L104)

建议切分边界：

- 放进 `teammate_work_surface` 的 `slot_identity` / `slot_session` 小节。
- leader 只保留“调用某个 teammate slot”所需的 opaque surface，例如 `surface.ensure_slot(worker_id)` 或 `surface.session_ref(worker_id)`。

### 4.2 teammate direct mailbox cursor 与 directed consume

应迁出条目：

- `leader_loop` 中“以 teammate recipient 为主语义”的 cursor/ACK 调用链：
  - [_load_authoritative_mailbox_cursor](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py#L657)
  - [_acknowledge_messages](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py#L600)
  - [_list_mailbox_messages](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py#L579)
  - [_acquire_teammate_assignments](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py#L1151)

原因：

- 对 `team_id:teammate:N` recipient 的 cursor 加载、私有 inbox 轮询、directed directive 解析与 ACK，本质上是 teammate direct mailbox truth。
- 当前实现里，leader 只是代 teammate 做 direct inbox consumer；这和“leader 只做 activation + convergence”的目标不一致。

建议切分边界：

- `teammate_work_surface` 直接拥有 `poll_direct_mailbox(slot)`、`load_slot_cursor(slot)`、`commit_direct_consume(slot, envelope_ids)`。
- 如果担心一次抽走 `_acknowledge_messages` / `_load_authoritative_mailbox_cursor` 会连带 leader mailbox 路径一起变动，可以先把它们保留为 generic mailbox/session utility，再由 `teammate_work_surface` 调用；但 `leader_loop` 不应继续直接写 teammate recipient cursor。

### 4.3 directed/autonomous claim 与 pending teammate assignment 物化

应迁出条目：

- [_build_pending_teammate_assignment](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py#L356)
- [_acquire_teammate_assignments](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py#L1095) 中的：
  - slot 可用性判断
  - `task.directive` 解析与 target worker 校验
  - `runtime.claim_task(...)` directed claim
  - `runtime.claim_next_task(...)` autonomous claim
  - claim session/source 选择
  - pending assignment 列表构造

原因：

- “directed message -> materialize claimed work -> fallback autonomous claim -> build runnable assignment” 是 teammate work acquisition surface，而不是 leader convergence surface。
- `ResidentTeammateRuntime` 已经把 `acquire_assignments(...)` 当作 runtime-native acquisition contract；因此更合理的做法是把这个 contract 的 provider 放到 `teammate_work_surface`，而不是继续嵌在 leader_loop 里：[teammate_runtime.py#L48](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/teammate_runtime.py#L48)

建议切分边界：

- 新模块暴露 `acquire_assignments(existing_assignments, limit, activation_context)`。
- `activation_context` 第一阶段可以继续带 `backend / working_dir / role_profile / turn_index` 以保持行为不变；后续再把这些变成 slot-level activation config，而不是每轮 leader turn 注入。

### 4.4 teammate 执行后的 task/blackboard/mailbox result 回写

当前状态：

- 这一步已经在主线完成：`teammate_work_surface.execute_assignment(...)` 现在会自己处理 teammate worker execution side effect：
  - permission gate / blocked path
  - `TaskStatus.IN_PROGRESS / COMPLETED / FAILED / BLOCKED`
  - blackboard execution report / blocker
  - `task.result` mailbox envelope publish
- `leader_loop` 主线现在只消费返回的 `WorkerRecord / MailboxEnvelope` 作为 teammate execution evidence。

剩余问题：

- `leader_loop` 中的 compatibility fallback 已经去掉，因此 teammate execution truth 的主线 owner 已经明确收口到 work surface。
- 当前剩下的不是 execute/publish fallback，而是更深一层的 host-owned acquisition / long-lived online loop 语义。

### 4.5 _run_teammates 本体不应长期继续留在 leader_loop

当前状态：

- 这一步也已经继续前推：`_run_teammates(...)` 现在本质上只负责构造 `TeammateWorkSurface` 并调用 `surface.run(...)`。
- 初始 leader-created assignment 的 reserve、resident teammate runtime 的组装、以及 execute/acquire provider wiring 都已经进入 work surface。

剩余问题：

- 长期形态里，这个入口仍然可以继续收缩，最终更接近 `surface.ensure_or_step(...)` 或 host-owned 的长期 teammate agent loop，而不是由 leader cycle 显式调用一次 `run(...)`。

## 5. 仍必须暂时留在 leader_loop 的逻辑

### 5.1 leader-specific mailbox 与 prompt turn 触发

应保留条目：

- [_build_turn_input](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py#L112)
- [compile_leader_turn_assignment](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py#L457)
- `run(...)` 中针对 `leader_round.leader_task.leader_id` 的 mailbox poll/ack 路径：[leader_loop.py#L1576](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py#L1576), [leader_loop.py#L1722](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py#L1722)

原因：

- leader mailbox 是 leader 自己的 inbox，不是 teammate inbox。
- visible tasks、team snapshot、leader-lane snapshot、leader mailbox 的汇总 prompt，本身就是 leader 认知与收敛动作。

### 5.2 team-level continuous convergence

应保留条目：

- `_handle_promptless_convergence(...)` 及其在 `run(...)` 中的调度：[leader_loop.py#L1378](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py#L1378)
- `LeaderCoordinator` 的 promptless convergence 状态机：[leader_coordinator.py#L10](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_coordinator.py#L10)
- `run(...)` 中的 mailbox follow-up budget / base turn budget / should_run_prompt_turn 分支：[leader_loop.py#L1592](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py#L1592)

原因：

- “什么时候不跑新 prompt、什么时候继续等待 mailbox、什么时候由于 open team task 保持 lane running” 是 team-level convergence，而不是 teammate mailbox truth。
- 即便 teammate 执行面完全下沉，leader 仍必须决定 lane 何时需要 prompt、何时只做等待、何时完成/阻塞/失败。

### 5.3 leader activation 与 lane delivery completion

应保留条目：

- `run(...)` 中 leader prompt 执行后对 output 的 ingest、team task 创建、lane evaluation、runtime task status 更新：[leader_loop.py#L1772](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py#L1772), [leader_loop.py#L1834](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py#L1834)
- [_save_delivery_state](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py#L1285)
- `_finalize(...)` 中 `LeaderLoopResult`、shared digest metadata、delivery status 封口：[leader_loop.py#L2000](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py#L2000)

原因：

- leader 作为 activation center，仍要负责把 prompt 输出收敛成 team activation/task contract。
- leader 作为 convergence center，仍要负责 lane delivery state、runtime task completion、以及对 superleader 可见的 summary/digest 面。

### 5.4 shared subscription / digest 面

应保留条目：

- [_ensure_default_shared_subscriptions](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py#L738)
- [_collect_shared_digests](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py#L701)
- [_collect_subscription_cursors](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py#L720)

原因：

- 这些接口面向 leader lane shared summary 与 superleader digest consumption。
- 它们属于跨角色 convergence 视图，不是 teammate 私有 inbox/work surface。

## 6. 引入 teammate_work_surface 的最小集成顺序

### 6.1 第一步：先抽纯 helper，不改行为

最小外移：

- `_teammate_slot_from_worker_id`
- `_resident_claim_session_id`
- `_resident_teammate_session_id`
- `_resident_teammate_session_id_for_recipient`
- `_build_pending_teammate_assignment`

目标：

- 先把 slot identity、claim/session id 规则和 pending assignment 物化从 `leader_loop.py` 文件级移走。
- 这一步不改变任何 runtime 行为，只是把最稳定、最明显的 teammate-owned helper 抽成 `teammate_work_surface` 的纯函数层。

### 6.2 第二步：让 acquire path 先委托给 teammate_work_surface

最小替换：

- 保留 `LeaderLoopSupervisor._run_teammates(...)` 入口。
- 先把 [_acquire_teammate_assignments](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py#L1095) 的 body 全部迁到 `teammate_work_surface.acquire_assignments(...)`。

建议：

- 第一阶段允许 `teammate_work_surface` 继续接收 `backend / working_dir / turn_index / role_profile`，避免一次性改 activation config 传递方式。
- 如果不想立刻动 leader mailbox 的通用 ACK helper，就把 `_list_mailbox_messages / _load_authoritative_mailbox_cursor / _acknowledge_messages` 作为依赖注入给 surface，而不是让 leader_loop 继续亲自处理 teammate recipient。

### 6.3 第三步：再把 execute/publish path 委托给 teammate_work_surface

最小替换：

- 迁出 [_append_teammate_result_entry](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py#L781)
- 迁出 [_run_teammate_assignment](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py#L824)

状态：

- 这一步已经完成，当前主线就是 `teammate_work_surface.execute_assignment(...)` 自己完成：
  - slot session state 写入
  - task status 变更
  - blackboard append
  - `task.result` mailbox publish
- leader 主线现在只消费结果对象，不再拥有 teammate side effect 明细。

### 6.4 第四步：把 _run_teammates 压成薄适配器

最小替换：

- `LeaderLoopSupervisor._run_teammates(...)` 只保留一层调用，例如：
  - `surface.run(assignments=initial_assignments, keep_session_idle=..., execution_policy=...)`

目标：

- 让 `ResidentTeammateRuntime` 的 `execute_assignment` / `acquire_assignments` 闭包构造也下沉到 `teammate_work_surface`。
- 到这一步，leader_loop 还可以保留“何时 step teammate surface”的判断，但不再拥有 teammate mailbox/task/session truth 的实现细节。

### 6.5 第五步：再做第一处语义切口，而不是继续机械搬运

建议的第一处语义切口：

- 把 `backend / working_dir / role_profile` 从“每轮 leader turn 注入”改成“activation 时写入 teammate slot activation config”。

原因：

- 当前 `_acquire_teammate_assignments(...)` 仍要求 leader turn 提供 `backend / working_dir / turn_index` 才能物化 refill assignment：[leader_loop.py#L1065](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py#L1065)
- 如果这层不改，leader 只是把代码移动到新文件，语义上仍然是 teammate work surface 的事实拥有者。

最小结果：

- leader turn 只负责 activation 一次。
- 后续 promptless convergence 或 future standalone teammate loop 只需要 `surface.ensure_or_step(team_id)`，不再依赖新 leader turn 重新注入 refill tuple。

## 7. 建议切分边界

### 7.1 `leader_loop.py` 应保留的最小主语义

- leader mailbox poll / ack
- leader prompt turn compile / execute / ingest
- promptless convergence 决策
- lane delivery evaluation / state persistence / finalize
- shared subscription / digest / superleader-facing summary

### 7.2 `teammate_work_surface.py` 应接住的最小主语义

- slot identity 与 slot session
- teammate recipient direct inbox cursor / ACK
- directed claim 与 autonomous claim
- pending teammate assignment 物化
- teammate task execution side effect
- teammate result publication
- `ResidentTeammateRuntime` 所需 provider 组装

### 7.3 一个值得刻意避免的错误切法

不要把 `teammate_work_surface` 做成“新的大而全 leader helper”。

更合理的边界是：

- leader 只知道自己在调用一个 teammate surface
- teammate surface 自己知道 slot/session/mailbox/task 的细节
- generic mailbox cursor/ACK 若同时服务 leader 与 teammate，可单独沉到更底层 utility，但不要再让 `leader_loop` 成为两边 truth 的拥有者

## 8. 相关文档

- [online-collaboration-runtime-migration.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/online-collaboration-runtime-migration.md)
- [resident-teammate-online-loop-first-wave.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/resident-teammate-online-loop-first-wave.md)
- [durable-teammate-loop-and-mailbox-consistency.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/durable-teammate-loop-and-mailbox-consistency.md)
- [resident-hierarchical-runtime.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/resident-hierarchical-runtime.md)
