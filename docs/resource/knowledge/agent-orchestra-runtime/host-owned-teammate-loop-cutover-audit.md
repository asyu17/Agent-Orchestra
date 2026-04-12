# host-owned teammate loop cutover 审计

## 1. 一句话结论

截至 2026-04-09，这份 audit 里最核心的第一段 cutover 已经真正落到主线：`leader_loop` 的 mailbox helper 现在只处理 leader 自己的 inbox，teammate recipient 的 cursor/session truth 已经完全收口到 `TeammateWorkSurface + ResidentSessionHost`；`TeammateWorkSurface.run(...)` 也不再走 `ResidentTeammateRuntime` 的 live path，而是通过 host-owned teammate slot online loop 来驱动 directed mailbox、task-surface claim、permission gate、worker execution、blackboard/result publish 和 idle/wake 状态。与此同时，ready lane 也已经通过 `LeaderLoopSupervisor.ensure_or_step_session(...)` + `ResidentSessionHost` 进入 host-owned `leader_session_host` launch boundary，而且定向回归已经证明 teammate activation 后与 directed mailbox consume 都可以在无新增 leader prompt turn 的前提下继续推进。2026-04-09 又补上了这条主线里最后一层明显的 leader-owned activation shell：`LeaderLoopSupervisor._ensure_or_step_teammates(...)` 现在只保留构造 `TeammateWorkSurface` 并调用 `ensure_or_step_sessions(...)` 的薄 host-facing step，不再在 leader 侧 pre-prime teammate acquisition/execute，也不再把当前 turn 的 `backend / working_dir / role_profile` 作为 surface activation context 注入。相应地，若当前既没有 leader-seeded assignment，也没有已持久化的 slot activation profile，teammate continuation 会显式 no-op，而不是创建 placeholder slot session。当前这一刀又进一步固定了 continuation truth：一旦 slot session 已经持久化 runnable activation profile，后续 bounded step 会继续沿用 host profile，而不会被当前 surface context 改写；同时纯 continuation 也不再额外补写一轮 leader-sourced activation / wake metadata。当前还没一起完成的，已经收缩到更深层的 store transaction、真正的 host-owned leader/session graph runtime，以及 self-hosting evidence gate 的继续改写。

## 2. 范围与资料来源

补充事实：

- 2026-04-09 这轮又收掉了一层残留 fallback：work surface 自身携带的 `backend / working_dir / role_profile` 现在只用于 seeded slot 的 host metadata 补齐，不再足以单独创建 runnable slot、启动 mailbox poll，或驱动 autonomous claim。

本审计只针对下面两个目标：

1. 把 teammate recipient 的 cursor/session 真相完全并入 work surface / host。
2. 把 leader-led activation 推向真正的 host-owned long-lived teammate loop。

主要依据：

- 知识文档：
  - [implementation-status.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/implementation-status.md)
  - [online-collaboration-runtime-migration.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/online-collaboration-runtime-migration.md)
  - [resident-teammate-online-loop-first-wave.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/resident-teammate-online-loop-first-wave.md)
  - [durable-teammate-loop-and-mailbox-consistency.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/durable-teammate-loop-and-mailbox-consistency.md)
  - [leader-loop-teammate-work-surface-boundary-audit.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/leader-loop-teammate-work-surface-boundary-audit.md)
- 直接审计源码：
  - [leader_loop.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py)
  - [teammate_work_surface.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/teammate_work_surface.py)
  - [teammate_runtime.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/teammate_runtime.py)
  - [teammate_online_loop.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/teammate_online_loop.py)
  - [session_host.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/session_host.py)
  - [group_runtime.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/group_runtime.py)
  - [protocol_bridge.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/protocol_bridge.py)
  - [worker_supervisor.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/worker_supervisor.py)
  - [superleader.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/superleader.py)
  - [bootstrap.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/self_hosting/bootstrap.py)
- 直接审计测试：
  - [test_leader_loop.py](/Volumes/disk1/Document/code/Agent-Orchestra/tests/test_leader_loop.py)
  - [test_teammate_work_surface.py](/Volumes/disk1/Document/code/Agent-Orchestra/tests/test_teammate_work_surface.py)
  - [test_session_host.py](/Volumes/disk1/Document/code/Agent-Orchestra/tests/test_session_host.py)
  - [test_self_hosting_round.py](/Volumes/disk1/Document/code/Agent-Orchestra/tests/test_self_hosting_round.py)

## 3. 这次 cutover 的完成标准

这两条目标真正完成，不应该只看“代码看起来更像 Claude”，而应满足下面这些硬条件：

1. `leader_loop` 不再读取或写入任何 `team_id:teammate:N` 的 private cursor/session 真相。
2. teammate 的 direct mailbox consume、cursor commit、directive tracking、slot session 状态更新，都只能通过 work surface / session host 主线完成。
3. leader 不再显式“跑一次 teammate runtime”；它只能发 activation intent，或者让 host `ensure_or_step` 某个 teammate session。
4. teammate session 有稳定的 activation profile，不再依赖每轮 leader turn 注入 `backend / working_dir / role_profile / turn_index`。
5. teammate loop 在没有新 leader turn 的情况下也能持续 poll mailbox / claim task / publish result / 进入 idle / 被重新唤醒。
6. mailbox cursor、session 状态、task claim、blackboard/outbox 至少要有明确的单一 owner；更理想的是提供协调提交接口，避免多处各写一半。
7. self-hosting gap 和验证不能只停留在“有 teammate_execution_evidence”；它应继续前推到直接证明 host-owned online loop 真的在运行。

## 4. 2026-04-06 实施结果与剩余缺口

### 4.1 `leader_loop.py` 的 teammate recipient truth 泄漏已收口

这一项已经完成。当前 `LeaderLoopSupervisor` 里原来的 generic helper 已被收缩成 leader-only mailbox helper：

- [_acknowledge_leader_messages](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py#L446)
- [_load_authoritative_leader_mailbox_cursor](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py#L492)

而 teammate cursor/session 相关逻辑不再从 `leader_loop.py` 读取或写入。对应的 live path 现在统一落在：

- [teammate_work_surface.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/teammate_work_surface.py)
- [session_host.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/session_host.py)

当前新增验证事实：

- ready lane 的 host-owned leader-session launch boundary 已有定向回归覆盖：[test_leader_loop.py#L1806](/Volumes/disk1/Document/code/Agent-Orchestra/tests/test_leader_loop.py#L1806)
- teammate activation 后的 promptless continuation 与 directed mailbox consume 也已有定向回归覆盖：[test_leader_loop.py#L1960](/Volumes/disk1/Document/code/Agent-Orchestra/tests/test_leader_loop.py#L1960), [test_leader_loop.py#L2038](/Volumes/disk1/Document/code/Agent-Orchestra/tests/test_leader_loop.py#L2038)
- host-owned continuation 对 activation truth 的定向回归也已补上：当 slot session 已经持久化 activation profile 时，`ensure_or_step_sessions(...)` 会继续使用 host profile 的 `working_dir / role_profile`，而不会把当前 surface fallback 覆盖进 host metadata，也不会在纯 continuation step 里追加新的 activation/wake 记录：[test_teammate_work_surface.py](/Volumes/disk1/Document/code/Agent-Orchestra/tests/test_teammate_work_surface.py)

当前剩余判断：

- leader lane cycle 仍会显式调用 `_ensure_or_step_teammates(...) -> TeammateWorkSurface.ensure_or_step_sessions(...)` 触发 bounded teammate step，因此“完全脱离 leader cycle 的 host daemon / external wake loop”还没完成
- 但 leader 文件本身已经不再持有 teammate private recipient truth，也不再自己 pre-prime teammate acquisition/execute

### 4.2 `teammate_work_surface.py` 已从 per-call drain adapter 推进到 host-owned slot work surface

这一项已部分完成。当前 [TeammateWorkSurface](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/teammate_work_surface.py) 的 live path 已经具备下面这些 host-owned 语义：

- `ensure_slot_session(...)` 会通过 `ResidentSessionHost.load_or_create_slot_session(...)` 建立稳定 slot session，并把 activation profile 写入 session metadata
- 一旦 host/session 中已经存在 activation profile，`ensure_slot_session(...)` 现在会把 host profile 当作 continuation truth；当前 surface 最多只补齐缺失字段，而不能覆盖已持久化的 `backend / working_dir / role_profile`
- `acknowledge_messages(...)` 会优先走 `ResidentSessionHost.commit_mailbox_consume(...)`
- `run(...)` 已经改成 slot-owned online loop，而不是把 `ResidentTeammateRuntime.run(...)` 作为 live path
- `_run_slot_online_loop(...)` 会驱动 directed mailbox、autonomous claim、idle/wake、execution semaphore 和 slot 级 coordinator state
- `_run_slot_online_loop(...)` 现在只在 fresh activation 场景写 host-side activation intent / wake request：即 leader-seeded assignment 明确点火，或 slot 之前还没有 runnable host profile。纯 continuation step 不再合成一轮新的 leader-sourced activation/wake metadata
- `ensure_or_step_sessions(...)` 现在只会 step 两类 slot：本轮 leader-seeded assignment 明确激活的 slot，或者已经在 host/session 中持久化了 runnable activation profile 的 slot；如果两者都不存在，continuation 会显式 no-op，而不是创建 placeholder resident slot session
- `TeammateWorkSurface.run(...)` 这条独立 API 现在也遵守同一条 activation boundary：surface activation context 本身不再足以点火 mailbox/task polling；如果没有 seeded assignment 或 host-owned runnable profile，它也必须保持 no-op

仍然没有彻底完成的点：

- `TeammateWorkSurface` 仍由 leader activation 时构造，不是脱离 leader 进程独立存活的 host daemon
- work surface 作为独立 API 仍允许显式 surface activation context，但 `leader_loop` 主线已经不再依赖它；真正长期态仍应继续收口成由 host/session graph 唯一持有 activation profile

### 4.3 `ResidentTeammateRuntime` 已退出 live path，但仍保留为 legacy bounded engine

这项的主结论已经落地：`ResidentTeammateRuntime` 不再是 `leader_loop -> teammate` 主线上的真实运行入口。当前 live path 已经变成：

- `leader_loop -> TeammateWorkSurface.run(...)`
- `TeammateWorkSurface.run(...) -> per-slot TeammateOnlineLoop`
- `TeammateOnlineLoop -> execute_assignment(...) / session_host / mailbox / task surface`

`ResidentTeammateRuntime` 仍保留在仓库里，因此这个模块级 gap 的剩余部分变成了：

- 是否继续作为测试/兼容层保留
- 是否在后续继续下沉成单纯 inner execution engine，还是最终删除

### 4.4 `teammate_online_loop.py` 已被主线吸收为 slot online loop 调度骨架

这一项已经部分关闭。当前 [teammate_online_loop.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/teammate_online_loop.py#L1) 仍然保留 callback 形状，但它已经不再只是孤立测试 scaffold，而是被 `TeammateWorkSurface._run_slot_online_loop(...)` 直接消费为 live path 的 step loop。

因此这部分的剩余问题不再是“有没有接主线”，而是：

- `task.receipt` 虽已从 live path 发出，但 receipt/result 仍不是 store-level 单事务提交
- reconnect / resident process continuation 还没有和这条 slot loop 真正打通
- 仍然是 bounded online loop，而不是长期驻留到外部 host/supervisor 之上的独立常驻 agent

### 4.5 `ResidentSessionHost` 已完成第一段强化，但还不是最终唯一 truth owner

这一项已完成第一段收敛。当前 [ResidentSessionHost](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/session_host.py) 已新增：

- `load_or_create_session(...)`
- `load_or_create_slot_session(...)`
- `list_runnable_teammate_slot_sessions(...)`
- `commit_mailbox_consume(...)`
- `record_activation_intent(...)`
- `record_wake_request(...)`
- typed helper [TeammateSlotSessionState](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/contracts/agent.py#L211)
- 真实 ISO 时间戳语义的 `last_active_at / idle_since`

当前仍未完成的部分：

- 还没有 `list_sessions(...) / find_sessions_by_role(...)`
- 还没有 compare-and-swap / expected-cursor / version 语义
- 默认实现仍然主要是 supervisor-local host，而不是真正跨重建 durable control plane

### 4.6 `GroupRuntime / OrchestrationStore / MailboxBridge` 缺少协调提交接口

当前 direct mailbox consume 到 result publish 的关键状态仍是分散调用的：

- task claim / next claim：
  - [claim_task(...)](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/group_runtime.py#L761)
  - [claim_next_task(...)](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/group_runtime.py#L788)
- task status：
  - [update_task_status(...)](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/group_runtime.py#L717)
- blackboard append：
  - [append_blackboard_entry(...)](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/group_runtime.py#L953)
- protocol/mailbox cursor：
  - [save_protocol_bus_cursor(...)](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/storage/base.py#L185)
  - [get_protocol_bus_cursor(...)](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/storage/base.py#L195)
- mailbox bridge ack：
  - [acknowledge(...)](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/protocol_bridge.py#L831)

这表示系统还没有一个“coordinator commit”接口去统一提交：

1. directive consume / cursor commit
2. task claim / task receipt
3. session update
4. blackboard append
5. mailbox result / outbox
6. optional delivery snapshot

这一项现在已经前推到 runtime-owned first-cut：teammate directed consume 不再由 work surface 手写 `store cursor -> bridge ack -> session update` 三段式，而是优先统一走 `ResidentSessionHost.commit_mailbox_consume(...)`；随后 `GroupRuntime.commit_directed_task_receipt(...)` 会落 blackboard 和 lane delivery snapshot，并由 work surface 发出正式的 `task.receipt` envelope；assignment 完成后又会进入 `GroupRuntime.commit_teammate_result(...)`。

但更深一层的协调提交仍未完成，因此下面这些判断仍然成立：

- `task claim / status / blackboard append / mailbox result / delivery snapshot` 仍不是单事务提交
- 崩溃恢复时仍可能出现“claim 已发生但 blackboard/result 未落”之类的偏斜
- 当前的统一提交面仍在 runtime helper，而不是 store-level coordination transaction

建议目标 owner：

- `OrchestrationStore`：新增 coordination commit API
- `GroupRuntime`：暴露 session-scoped teammate coordination methods
- `MailboxBridge`：退回为 transport/bridge，而不是最终 consume truth

### 4.7 `worker_supervisor.py` 已经补上 worker/agent reconnect truth，但还没收敛成单一 owner

[DefaultWorkerSupervisor](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/worker_supervisor.py#L107) 当前非常强，这一轮也已经把 worker/agent 双层 session truth 显式化了：

- `AgentSession` 现在新增 `current_worker_session_id / last_worker_session_id`
- `AgentWorkerSessionTruth` 提供了统一派生逻辑
- `ResidentSessionHost.read_worker_session_truth(...)` 提供 host 侧统一读口
- `_write_host_session(...)` 和 active/recover/finalize 路径都会保持 host agent session 与 worker session truth 对齐

但它的 durable truth 主线仍围绕 worker session：

- `_ActiveWorkerSession`
- `WorkerSession`
- transport binding
- reconnect / reclaim / reattach

这表示“worker/agent truth 缺失”这个 gap 已经关闭，但“谁是唯一 truth owner”这个更深一层的问题还没关闭：当前更像是 worker session 被显式映射进 agent session，而不是已经完全切成“host-owned teammate agent session 驱动 subordinate worker”。

如果要走向真正 host-owned teammate loop，应该进一步补齐：

- teammate agent session 和 subordinate worker session 的显式绑定关系
- leader / superleader 能看到“这个 teammate agent 当前是否 idle / waiting / running / waking”
- reconnect 不只是 worker 级别，也包括 teammate agent loop 级别

建议目标 owner：

- `worker_supervisor.py`
- `contracts/agent.py`
- `session_host.py`

### 4.8 `SuperLeaderRuntime` 已经有 lane session graph，但还没有切到真正的 host/session runtime

[SuperLeaderRuntime.run_planning_result(...)](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/superleader.py#L304) 这轮已经补上了第一层 host/session 视角：

- `SuperLeaderLaneSessionState`
- `SuperLeaderLaneCoordinationState.session`
- `SuperLeaderCoordinationState.coordinator_id`
- cycle metadata 中的 `active_lane_session_ids`

但它当前仍然是：

- 物化 planning result
- 创建 `LeaderLoopSupervisor`
- 直接 `leader_loop.run(...)`
- 等 leader lane result 回来

这意味着：

- superleader 已经能看到 subordinate lane session graph
- 但它调的仍然是 `leader_loop.run(...)`
- 它还不是在调 leader session / leader host

对你要的终态来说，这块后面必须继续改成：

- superleader 只管理 leader session graph
- leader 再管理 teammate session graph
- runtime 不再是“显式调用 loop.run(...)”的风格

所以这一项也应理解成“first-cut 已进主线，但真正同构化还没完成”。

### 4.9 `self_hosting/bootstrap` 的 gap 与 evidence gate 需要改写

[bootstrap.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/self_hosting/bootstrap.py#L646) 现在对这条主线的验证已经先发生了一次语义切换：runtime-native `teammate_execution_evidence` 已经成为 primary signal，`leader_consumed_mailbox` 退回诊断 metadata；但 `validated` 仍没有完全切到 host-owned 终态。当前它仍然主要围绕：

- `teammate_execution_evidence`
- `created_task_count`
- `leader_turn_count > 1`
- resident claim evidence

这对“teammate 执行副作用是否由 leader 持有”已经有用，但对“host-owned teammate loop 是否成立”还不够，因为它没有直接验证：

- leader 在 activation 后是否不再需要继续显式触发 teammate run
- teammate session 是否能在没有新 leader turn 的情况下继续推进
- cursor/session 真相是否已经完全不从 leader helper 读取/写入
- teammate agent session 是否可恢复、可唤醒、可继续 poll
- 目前 `_delegation_validation_metadata(...)` 直接把 `leader_turn_count > 1` 当作 `validated` 的组成条件，因此 self-hosting 仍在奖励 leader follow-up，而不是“leader 一次 activation 后 teammate 自主继续推进”。

### 4.10 知识与测试 framing 也需要同步收口

除了 runtime 本身，知识和测试也有两类需要同步改的地方：

- 一些旧边界文档仍在描述“helper/claim/execute 主要还在 leader_loop”，而真实代码已经进一步下沉到 work surface。
- 现有测试虽然已经覆盖了 `surface.run(...)` 与 leader thin delegation，但对“leader 不再触碰 teammate recipient cursor/session”和“host-owned teammate loop”这两类新主语义的证明还不够完整。

如果这部分不跟上，后续自举与人工排查都容易被旧边界误导。

如果要让 Agent Orchestra 自举继续做这条主线，bootstrap gap 需要新增更精确的 gap id 和 evidence gate，例如：

- `teammate-host-owned-session-truth`
- `leader-to-host-activation-cutover`
- `host-owned-teammate-online-loop`

否则 self-hosting 还会以旧的 `team-parallel-execution` 完成态为依据，而不是继续向 Claude 式长期在线协作推进。

### 4.11 测试体系还缺几类必须补的证明

当前测试已经能证明：

- work surface 拿活和执行
- leader 对 surface 的 thin delegation
- bounded resident refill

但还缺下面这些“下一阶段一定要补”的证明：

1. `leader_loop` 对 teammate recipient 的 cursor/session helper 彻底归零
   - leader 自己的 `_acknowledge_messages / _load_authoritative_mailbox_cursor` 不再触碰 teammate recipient
2. teammate host-owned mailbox consume 的恢复与 restart
   - session 重载后能继续从 committed cursor 之后消费
3. teammate activation profile 是 host-owned 的
   - 不再依赖每轮 leader turn 注入 `backend / working_dir / role_profile / turn_index`
4. leader activation 后，无需新增 leader prompt turn，teammate 仍持续推进
5. self-hosting 明确验证：
   - host-owned teammate loop 在线
   - leader 只是 activation/convergence，不是 teammate runtime launcher

## 5. 建议的修改顺序

这份顺序里原先前四步已经在 2026-04-06 之前进入主线，当前推荐顺序应更新为：

1. 把 `GroupRuntime / session_host` 这组 runtime-owned commit 再往下推成 store-level coordination transaction。
   - 把 claim / status / blackboard / outbox / cursor 继续收口，避免只在 runtime helper 层局部统一
2. leader 从 `run_teammates` 继续切到更明确的 `ensure_or_step_teammate_session`。
   - leader 不再自己显式触发 teammate drain
3. superleader 从 `leader_loop.run(...)` 切到 leader session graph。
   - 让层级结构真正同构
4. 改写 self-hosting gap 与 evidence gate。
   - 让系统下一轮能自动盯准新的 host-owned 终态，而不是已经过时的 leader-stepped 完成态
5. 最后再做 host/supervisor/worker 的 reconnect 同构化。
   - 让 teammate slot loop 和 subordinate worker 一起进入真正的 durable resident 主线

## 6. 建议新增或重点修改的文件

优先级最高：

- [leader_loop.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py)
- [teammate_work_surface.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/teammate_work_surface.py)
- [teammate_runtime.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/teammate_runtime.py)
- [teammate_online_loop.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/teammate_online_loop.py)
- [session_host.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/session_host.py)

紧随其后：

- [group_runtime.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/group_runtime.py)
- [protocol_bridge.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/protocol_bridge.py)
- [worker_supervisor.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/worker_supervisor.py)
- [superleader.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/superleader.py)
- [bootstrap.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/self_hosting/bootstrap.py)

测试必须同步：

- [test_leader_loop.py](/Volumes/disk1/Document/code/Agent-Orchestra/tests/test_leader_loop.py)
- [test_teammate_work_surface.py](/Volumes/disk1/Document/code/Agent-Orchestra/tests/test_teammate_work_surface.py)
- [test_session_host.py](/Volumes/disk1/Document/code/Agent-Orchestra/tests/test_session_host.py)
- [test_self_hosting_round.py](/Volumes/disk1/Document/code/Agent-Orchestra/tests/test_self_hosting_round.py)

## 7. 对下一轮实现的直接建议

如果下一轮继续写代码，最值得先开的已经不是“leader 去掉 teammate cursor/session”或“给 work surface 补 activation profile”，因为这两刀已经落了。下一轮更值得先开的，是下面三个切口：

1. 把现有的 `commit_directed_task_receipt(...) / commit_teammate_result(...) / session_host.commit_mailbox_consume(...)` 再往下推成更完整的 coordination commit。
   - 把 `task claim / task.receipt / blackboard / result / delivery snapshot` 串成更少分裂点的提交面。
2. leader activation 继续收敛成 `ensure_or_step_teammate_session`。
   - 让 leader 更像 activation center + convergence center，而不是当前仍在 lane cycle 内显式触发 `_ensure_or_step_teammates(...)` 的 bounded shell。
3. 把 superleader 的 lane session graph 从 metadata 继续推进成真正的 leader host/session runtime。
   - 让 superleader 不再只是“看见 lane session”，而是直接驱动 subordinate leader session。
4. 把这条 slot online loop 和 `worker_supervisor` 的 durable worker session 真正绑定起来。
   - 只有这样，host-owned teammate loop 才能和 reconnect / resident worker 成为同一条主线。

判断：

- 当前最大的结构性收益已经拿到：leader mailbox truth 与 teammate mailbox truth 已分开，slot session 也有了更像真的 host owner 的 API。
- 下一轮最容易出错的地方不再是“谁读 cursor”，而是“谁提交一整组 side effect”“谁拥有唯一 session truth”，以及“superleader 何时真正不再调用 `leader_loop.run(...)`”。
