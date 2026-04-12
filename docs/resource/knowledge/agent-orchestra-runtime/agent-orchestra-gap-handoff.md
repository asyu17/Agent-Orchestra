# agent_orchestra gap handoff

## 1. 一句话结论

截至 2026 年 4 月 10 日，这份 handoff 的主语义已经变化：上一轮审计中挂着的 4 个中层 gap 已经全部进入主线 first-cut，不应再作为“缺失能力”重复实现；并且其中 `superleader-session-runtime` 这一支也已经再前推一步，ready lane 不再直接变成 `leader_loop.run(...)` task，而是先进入 host-owned `leader_session_host` 边界。下一轮真正要投喂给 `Agent Orchestra` 的，是“旧 gap 已关闭后的 residual gap 接力包”，重点是把 runtime-owned first-cut 收敛成真正的 store-level / host-owned / single-owner 主语义，并把 bootstrap/self-hosting evidence rewrite 明确压到 runtime truth 稳定之后。

## 2. 范围与资料来源

- 当前状态与残余缺口来源：
  - [implementation-status.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/implementation-status.md)
  - [current-priority-gap-list.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/current-priority-gap-list.md)
  - [remaining-work-backlog.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/remaining-work-backlog.md)
  - [resident-team-collaboration-and-slice-planning.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/resident-team-collaboration-and-slice-planning.md)
  - [host-owned-teammate-loop-cutover-audit.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/host-owned-teammate-loop-cutover-audit.md)
  - [durable-teammate-loop-and-mailbox-consistency.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/durable-teammate-loop-and-mailbox-consistency.md)
- 相关架构文档：
  - [online-collaboration-runtime-migration.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/online-collaboration-runtime-migration.md)
  - [resident-hierarchical-runtime.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/resident-hierarchical-runtime.md)
  - [agent-abstraction-skill-and-prompt-system.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/agent-abstraction-skill-and-prompt-system.md)
  - [self-hosting-bootstrap.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/self-hosting-bootstrap.md)
- 关键验证事实：
  - 定向验证：`uv run pytest tests/test_session_host.py tests/test_teammate_work_surface.py tests/test_teammate_runtime.py tests/test_leader_loop.py tests/test_worker_supervisor_protocol.py tests/test_superleader_runtime.py tests/test_directed_mailbox_protocol.py -q`
  - 全量回归入口：`uv run pytest -q`
  - 当前 workspace 未附带可追溯的 full-suite artifact；若要把“全量已通过”写成 authority 结论，需要重新执行并记录结果，在未重跑前不固化精确通过数

## 3. 旧 gap 已关闭表

| 旧 gap | 之前审计里的含义 | 当前状态 | 现在残留的真实问题 |
| --- | --- | --- | --- |
| `runtime-owned coordination commit` | 缺少统一的 teammate coordination commit 表面 | 已进入主线 first-cut：`GroupRuntime.commit_directed_task_receipt(...) / commit_teammate_result(...)` 已接上 | 还不是 store-level 单事务，仍存在崩溃窗口 |
| `task-receipt-live-path` | directed consume 之后没有正式 receipt 中间态 | 已进入主线 first-cut：`task.receipt` 会在 directed claim materialization 时真实发出 | 还需要和 claim/cursor/session/blackboard/outbox 做更强事务性收口 |
| `worker-agent-session-truth` | worker session 和 agent session 之间没有显式 reconnect truth | 已进入主线 first-cut：`AgentSession.current_worker_session_id / last_worker_session_id`、`AgentWorkerSessionTruth`、`ResidentSessionHost.read_worker_session_truth(...)` 都已落地 | 还没有收敛成 single-owner truth |
| `superleader-session-graph` | `SuperLeaderRuntime` 几乎没有 host/session 视角 | 已进入更深一层 first-cut：`SuperLeaderLaneSessionState`、`lane_state.session`、`active_lane_session_ids` 已进入协调状态，ready lane 也已通过 `ensure_or_step_session(...)` 进入 `leader_session_host` 边界 | 还没有切成可重入 / 可恢复的 host-stepped leader runtime |

## 4. 新 residual gap 接力表

| residual_gap_id | 来自哪条旧 gap | 当前为什么仍未完成 | 建议主文件 |
| --- | --- | --- | --- |
| `coordination-transaction` | `runtime-owned coordination commit` | 现在已经不只是 runtime helper：receipt/result/authority/mailbox consume 都能通过 durable outbox + persisted snapshot boundary first-cut 提交，teammate live path 的第二套 commit family 也已删除；剩余缺口转成 `WorkerSession / AgentSession / ResidentCoordinatorSession` 仍未收敛成 single-owner truth。2026-04-09 新状态是：StoreBacked `ResidentSessionHost` 已接管 persisted `AgentSession + WorkerSession` continuation read projection，`DefaultWorkerSupervisor` reclaim/finalize/reattach 也已改成通过 host projection 写回，因此 read/recovery projection 已不再是主要偏斜源。2026-04-10 又把 worker-session write 本身纳入 coordination family：PostgreSQL 会把 worker-session snapshot 与 task/blackboard/delivery/cursor/agent-session/outbox 同事务提交，其他 store 则立即补 fallback save，因此 stale `ACTIVE` worker session / stale mailbox cursor 已不再是这里的主要偏斜源；当前最真实的残余边界已经变成 broader single-owner write ownership | `src/agent_orchestra/runtime/group_runtime.py`, `src/agent_orchestra/runtime/session_host.py`, `src/agent_orchestra/storage/base.py`, `src/agent_orchestra/storage/postgres/store.py` |
| `leader-activation-cutover` | `task-receipt-live-path`, team host-owned cutover | leader 仍显式调用 `_ensure_or_step_teammates(...) / _run_teammates(...)` 这一层薄 shell，还不是 `ensure_or_step_teammate_session` 式 activation center | `src/agent_orchestra/runtime/leader_loop.py`, `src/agent_orchestra/runtime/teammate_work_surface.py`, `src/agent_orchestra/runtime/teammate_online_loop.py` |
| `teammate-durable-resident-loop` | host-owned teammate loop | teammate 仍是 bounded online loop，不是跨 host rebuild / 跨 leader run 的真正常驻 agent | `src/agent_orchestra/runtime/teammate_work_surface.py`, `src/agent_orchestra/runtime/teammate_online_loop.py`, `src/agent_orchestra/runtime/session_host.py` |
| `session-truth-convergence` | `worker-agent-session-truth` | 现在有 `WorkerSession / AgentSession / ResidentCoordinatorSession` 多层状态，但还没有唯一 owner 和统一恢复路径 | `src/agent_orchestra/contracts/agent.py`, `src/agent_orchestra/runtime/session_host.py`, `src/agent_orchestra/runtime/worker_supervisor.py` |
| `superleader-session-runtime` | `superleader-session-graph` | superleader 现在已经通过 `LeaderLoopSupervisor.ensure_or_step_session(...)` + `ResidentSessionHost` 管理 ready-lane launch boundary，但 leader session 仍主要是单次 bounded `run(...)` 的 host projection，还不是可重入 / 可恢复 / 可跨 superleader cycle step 的 leader runtime | `src/agent_orchestra/runtime/superleader.py`, `src/agent_orchestra/runtime/leader_loop.py`, `src/agent_orchestra/runtime/session_host.py` |
| `self-hosting-evidence-rewrite` | self-hosting handoff / validation | bootstrap 还没完全盯住“host-owned teammate loop 在线、leader 只是 activation/convergence center”的新终态；当前 `_delegation_validation_metadata(...)` 仍以 `host_owned_leader_session + teammate_execution_evidence + leader_turn_count == 1 + mailbox_followup_turns_used == 0` 作为 validated gate，因此它应继续滞后于 runtime truth 收口 | `src/agent_orchestra/self_hosting/bootstrap.py`, `tests/test_self_hosting_round.py` |

### 4.1 2026-04-09 针对 `coordination-transaction-and-session-truth-convergence` 的代码热点收口

基于当前源码，下面两类偏斜窗口已经比知识层旧表述更具体，后续实现应直接以它们为入口，而不是继续把问题抽象成“事务还不够强”：

1. `task.receipt / task.result / authority.request / authority.decision` 已进入 durable `coordination_outbox`，并返回 authoritative persisted snapshot first-cut
   - `GroupRuntime.commit_directed_task_receipt(...) / commit_teammate_result(...) / commit_authority_request(...) / commit_authority_decision(...)` 现在都已经对齐：outbox 记录会随 blackboard/task/delivery/cursor/session 同事务提交，返回值也会直接带回 persisted task/blackboard/delivery/cursor/session truth。
   - 判断：这条热点已经从“receipt/result/authority 不在 durable outbox 内”切换成“single-owner session truth 还有多层 owner 并存，恢复语义还没完全统一”。

2. teammate live path 上一轮残留的 post-commit patch 已关闭
   - `TeammateWorkSurface._commit_activation_receipt(...)` 与 `execute_assignment(...)` 不再在 runtime commit 返回后补 `session_host.save_session(...)`。
   - `execute_assignment(...)` 也不再在 `commit_teammate_result(...)` 之后补写默认字段并再次 `save_blackboard_entry(...)`。
   - 判断：这说明当前 store transaction 已经真正成为 receipt/result/session/blackboard 的唯一 live commit 面；本 lane 后续应继续转向 `WorkerSession / AgentSession / ResidentCoordinatorSession` 的 single-owner 收口，以及 host-owned continuation / replay 语义。

3. 2026-04-09 新收口：`ResidentSessionHost` 开始拥有 continuation read / recovery projection
   - StoreBacked host 读取 session 时会把 persisted `AgentSession` 与同 id `WorkerSession` 重新投影成 host-owned truth，避免 raw `AgentSession` payload 把 stale `current_worker_session_id`、lease 或 mailbox/session fields 重新带回上层。
   - `DefaultWorkerSupervisor` 的 active persist / reclaim / finalize host write path 也改成先产出 `WorkerSession` snapshot，再通过 `ResidentSessionHost.project_worker_session_state(...)` 回写；`GroupRuntime` authoritative session readback 同步优先走 supervisor host。
   - 判断：当前 residual gap 继续缩窄，剩下更像“single-owner ownership 仍并存”，而不是“谁来负责 continuation read / recovery projection 还没决定”。

4. 2026-04-10 新收口：coordination commit 自己也开始同步推进 `WorkerSession`
   - `StoreBackedResidentSessionHost.commit_mailbox_consume(...)`、`GroupRuntime.commit_directed_task_receipt(...)`、`commit_teammate_result(...)`、以及 `commit_authority_request(...)` 现在都会把 worker-session snapshot 一并带进 coordination commit。
   - PostgreSQL `commit_coordination_transaction(...)` 已把 worker session 纳入同事务提交；不支持这条扩展事务面的 store 也会立即补 `save_worker_session(...)` fallback。
   - 判断：这说明 residual gap 已不再主要卡在“authority/result/readback 仍会被 stale ACTIVE worker session 拉回旧真相”或“mailbox consume 之后 worker session cursor 还停在旧 envelope”，而是继续收缩到 broader single-owner owner boundary。

5. bootstrap/self-hosting evidence rewrite 仍明确是后置 follow-on
   - 当前 `_delegation_validation_metadata(...)` 会在 `host_owned_leader_session`、runtime-native `teammate_execution_evidence`、`leader_turn_count == 1` 与 `mailbox_followup_turns_used == 0` 同时成立时，才把 delegation validation 判为 `validated`。
   - 判断：这说明 bootstrap 仍然在 runtime truth 之上叠了一层 host-owned evidence heuristic；在 coordination transaction 与 single-owner session truth 没彻底切稳前，不应把这层 heuristic 反过来当作 runtime owner。

### 4.2 当前 lane 的推荐执行顺序细化

对本轮 `coordination-transaction-and-session-truth-convergence`，更稳的顺序应是：

1. 先把 `task.receipt / task.result / mailbox consume` 的 cursor、session、blackboard、delivery、outbox 收口成真正的 coordination transaction。
2. 再把 `WorkerSession / AgentSession / ResidentCoordinatorSession` 的读写统一到 `ResidentSessionHost` 主语义，减少 `worker_supervisor / leader_loop / superleader` 各自拼装恢复状态。
3. `self-hosting-evidence-rewrite` 仍应视为跨 lane 后续，而不是这个 lane 的主实现面；只有在前两步落地后，它才值得从当前 `leader_turn_count == 1 / mailbox_followup_turns_used == 0` heuristic 切换到真正依附 runtime truth 的 host-owned evidence。

## 5. 不要重复做的事

下一轮交给 `Agent Orchestra` 时，必须显式约束下面这些点，避免系统回到旧目标：

1. 不要再把 `task.receipt` 当成“尚未接入”的 gap。
2. 不要再把 worker/agent session truth 当成“完全缺失”的 gap。
3. 不要再把 superleader session 视角当成“完全不存在”的 gap。
4. 不要把 `team-parallel-execution`、`durable-supervisor-sessions`、`reconnector`、`protocol-bus` 重新当作 base blocker。
5. 不要为了追求“更像 Claude”而回退已经进入主线的 runtime-owned helper。

## 6. 建议执行顺序

建议下一轮按下面顺序推进，而不是并行无约束地四处补丁：

1. `coordination-transaction`
2. `leader-activation-cutover`
3. `superleader-session-runtime`
4. `session-truth-convergence`
5. `self-hosting-evidence-rewrite`
6. `teammate-durable-resident-loop`

并且这轮之后的主线约束新增为：

7. team budget 不再保留 `max_concurrency`，只保留 `max_teammates`
8. `Leader` 输出要从 flat `teammate_tasks` 升级到显式 `sequential / parallel slices`
9. team runtime 要继续收敛成 resident collaboration，而不是 bounded batch dispatch

原因：

- 第一步先消灭 side effect 分裂点，不然后面 host-owned 化会继续建立在脆弱提交面上。
- 第二步、第三步分别收缩 `leader` 和 `superleader` 的旧调用模型。
- 第四步把多层 session truth 收敛，否则 reconnect 会一直语义分裂。
- 第五步让 self-hosting 能自动盯住新目标，而不是继续奖励旧路径。
- 第六步再把 teammate 推到更强的 durable resident loop，避免顺序倒置。

## 7. 可直接投喂给 Agent Orchestra 的执行包

下面这段可以直接交给 `Agent Orchestra` 作为下一轮输入。

```md
你现在接到的是 residual-gap relay round，而不是 base capability build round。

先读这些知识文档：

1. resource/knowledge/README.md
2. resource/knowledge/agent-orchestra-runtime/README.md
3. resource/knowledge/agent-orchestra-runtime/implementation-status.md
4. resource/knowledge/agent-orchestra-runtime/current-priority-gap-list.md
5. resource/knowledge/agent-orchestra-runtime/host-owned-teammate-loop-cutover-audit.md
6. resource/knowledge/agent-orchestra-runtime/durable-teammate-loop-and-mailbox-consistency.md
7. resource/knowledge/agent-orchestra-runtime/agent-orchestra-gap-handoff.md
8. resource/knowledge/agent-orchestra-runtime/resident-team-collaboration-and-slice-planning.md

这轮不要重复实现已经进入主线 first-cut 的旧 gap：

- task.receipt live path
- worker/agent session reconnect truth
- superleader lane session graph
- runtime-owned coordination commit helper

这轮真正要完成的是下面 6 个 residual gaps：

1. coordination-transaction
2. leader-activation-cutover
3. superleader-session-runtime
4. session-truth-convergence
5. self-hosting-evidence-rewrite
6. teammate-durable-resident-loop

同时把下面 3 条新设计决议作为约束一起吸收：

1. 移除 `max_concurrency` 作为正式 team budget 语义，只保留 `max_teammates`
2. 把 `Leader` 输出升级成显式 `sequential / parallel slices`
3. 把 team runtime 继续推向 resident collaboration 主语义

执行顺序要求：

1. 先把 `GroupRuntime / session_host` 这组 runtime-owned commit 下推成更强的 store-level coordination transaction。
2. 再把 leader 从 `_run_teammates(...)` 推向 `ensure_or_step_teammate_session` 风格。
3. 再把 superleader 从当前的 `leader_session_host` launch boundary 推向可重入 / 可恢复的 leader session graph runtime。
4. 再收敛 `WorkerSession / AgentSession / ResidentCoordinatorSession` 的 single-owner truth。
5. 在前 4 步稳定之后，再改写 self-hosting evidence gate，并补 durable teammate resident loop 的继续推进。

硬约束：

- 不要回退已进入主线的 `task.receipt` live path。
- 不要回退已进入主线的 worker/agent truth 字段。
- 不要回退已进入主线的 lane session graph metadata。
- 不要把 base resident teammate capability 重新当 blocker。

建议主文件：

- src/agent_orchestra/runtime/group_runtime.py
- src/agent_orchestra/runtime/session_host.py
- src/agent_orchestra/storage/base.py
- src/agent_orchestra/storage/postgres/store.py
- src/agent_orchestra/runtime/leader_loop.py
- src/agent_orchestra/runtime/teammate_work_surface.py
- src/agent_orchestra/runtime/teammate_online_loop.py
- src/agent_orchestra/runtime/worker_supervisor.py
- src/agent_orchestra/runtime/superleader.py
- src/agent_orchestra/self_hosting/bootstrap.py

至少补这些验证：

- uv run pytest tests/test_teammate_work_surface.py tests/test_session_host.py tests/test_worker_supervisor_protocol.py tests/test_superleader_runtime.py tests/test_leader_loop.py tests/test_self_hosting_round.py -q
- uv run pytest -q

完成后必须同步更新知识文档，尤其：

- resource/knowledge/agent-orchestra-runtime/implementation-status.md
- resource/knowledge/agent-orchestra-runtime/current-priority-gap-list.md
- resource/knowledge/agent-orchestra-runtime/host-owned-teammate-loop-cutover-audit.md
- resource/knowledge/agent-orchestra-runtime/agent-orchestra-gap-handoff.md
```

## 8. 下一轮最值得补的证明

如果下一轮除了实现还要补测试，优先补下面这些证明：

1. leader 自己的 cursor helper 不再触碰 teammate recipient。
2. session reload 后，teammate 能从 committed mailbox cursor 继续消费。
3. activation profile 是 host-owned 的，而不是每轮 leader 注入的。
4. leader activation 后，无需新增 leader prompt turn，teammate 仍持续推进。
5. self-hosting 能直接验证“host-owned teammate loop 在线”而不是只看旧式 execution evidence。

## 9. 相关文档

- [implementation-status.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/implementation-status.md)
- [current-priority-gap-list.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/current-priority-gap-list.md)
- [remaining-work-backlog.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/remaining-work-backlog.md)
- [host-owned-teammate-loop-cutover-audit.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/host-owned-teammate-loop-cutover-audit.md)
- [durable-teammate-loop-and-mailbox-consistency.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/durable-teammate-loop-and-mailbox-consistency.md)
- [online-collaboration-runtime-migration.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/online-collaboration-runtime-migration.md)
- [resident-hierarchical-runtime.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/resident-hierarchical-runtime.md)
- [agent-abstraction-skill-and-prompt-system.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/agent-abstraction-skill-and-prompt-system.md)
