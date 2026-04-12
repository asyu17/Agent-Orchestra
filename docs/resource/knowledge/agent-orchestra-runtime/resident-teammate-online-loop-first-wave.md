# resident teammate online loop 第一波审计

## 1. 一句话结论

`ResidentTeammateRuntime` 已经不再只是 leader-owned refill shell：当前代码已经把 refill/acquisition sequencing 下放到 runtime 自己，通过 `ResidentTeammateAcquireResult` / `acquire_assignments(...)` 聚合 directed/autonomous claim 和 mailbox progress，并把 `teammate_execution_evidence` 提升成 runtime-native 结果与 leader turn metadata。同时，新引入的 `teammate_work_surface` 已经接住 slot identity、direct mailbox cursor、初始 assignment reserve，以及主线 `execute_assignment(...)` 里的 permission gate、task status、blackboard result、`task.result` mailbox publish 和 slot session finalize。现在剩下的 gap 不再是“runtime 会不会继续拿活”，而是“teammate 的 mailbox/task/cursor/acquisition provider 何时能彻底脱离 leader-provided helper，变成真正 host-owned 的长期在线 agent loop”。

## 2. 范围与资料来源

- 目标知识：
  - `resource/knowledge/agent-orchestra-runtime/online-collaboration-runtime-migration.md`
  - `resource/knowledge/agent-orchestra-runtime/team-parallel-execution-gap.md`
- 审计源码：
  - `src/agent_orchestra/runtime/teammate_runtime.py`
  - `src/agent_orchestra/runtime/leader_loop.py`
  - `src/agent_orchestra/runtime/group_runtime.py`
  - `src/agent_orchestra/runtime/protocol_bridge.py`
  - `src/agent_orchestra/contracts/task.py`
- 相关测试：
  - `tests/test_leader_loop.py`
  - `tests/test_runtime.py`
  - `tests/test_protocol_bridge.py`
  - `tests/test_self_hosting_round.py`

## 3. 当前 slice 为什么仍是 leader-driven

### 3.1 `ResidentTeammateRuntime` 已经开始拥有 acquisition 主语义，但还不是完整 agent loop

源码事实：

- `ResidentTeammateRuntime.run(...)` 入口现在拿到的是 `assignments`、`execute_assignment`、`acquire_assignments` 与 `coordinator_id`，其中 `acquire_assignments` 不再只是“给我下一批 assignment”，而是显式返回 `ResidentTeammateAcquireResult`，带有 `processed_mailbox_envelope_ids / directed_claimed_task_ids / autonomous_claimed_task_ids / teammate_execution_evidence` 等 runtime-native teammate 证据：`src/agent_orchestra/runtime/teammate_runtime.py`
- 它内部只维护 `pending_assignments / active_assignments / running_assignments`，当没有 runnable assignment 时立刻返回 `QUIESCENT + stop=True`，并不会继续 idle poll mailbox 或 task surface：`src/agent_orchestra/runtime/teammate_runtime.py`
- session 的 `role` 是 `teammate_runtime`，`coordinator_id` 也是外部提供的运行壳标识，而不是稳定 teammate identity：`src/agent_orchestra/runtime/teammate_runtime.py`

判断：

- 当前对象已经不是“只会 drain 既有 assignment 的壳”，因为它现在自己驱动 refill/acquisition sequencing。
- 但它仍然不是最终形态的“长期在线 teammate agent”，因为 mailbox/task/cursor 的真正 truth source 还没有完全内建到 runtime/host 自身。

### 3.2 teammate 的工作来源已从 leader-owned loop 降到 leader-provided work surface

源码事实：

- `_run_teammates(...)` 仍在 leader loop 内作为 activation/convergence 入口被调用，但 resident teammate runtime 的组装已经进入 `teammate_work_surface.run(...)`；leader 不再自己拼 `ResidentTeammateRuntime` 的 execute/acquire 闭包，也不再保留第二套 teammate execution fallback：`src/agent_orchestra/runtime/leader_loop.py`, `src/agent_orchestra/runtime/teammate_work_surface.py`
- `leader_loop` 现在不再直接运行 `TeammateOnlineLoop`，也不再在 leader 层维护 refill iteration/stop policy；这些节奏已经下放给 `ResidentTeammateRuntime` 自己的 `_fill_slots()` / `_step()`：`src/agent_orchestra/runtime/leader_loop.py`, `src/agent_orchestra/runtime/teammate_runtime.py`
- `leader_loop` 里的 acquisition helper 仍负责 slot 计算、directed mailbox consume、claim session 生成和 assignment 构造，所以 work surface ownership 还没有完全迁到 teammate host：`src/agent_orchestra/runtime/leader_loop.py`

判断：

- teammate 现在已经自己决定“何时继续拿活”，leader 不再拥有 refill loop，主线执行副作用也不再由 leader 落盘。
- 但 teammate 还没有完全自己持有 work surface，因为 directed inbox/task claim 的最终 activation 入口仍由 leader cycle 驱动，它还不是完全 autonomous 的 standalone teammate host。

### 3.3 mailbox 当前主要是 teammate -> leader 的结果通道，不是 teammate 的工作入口

源码事实：

- `_run_teammate_assignment(...)` 结束时固定把 envelope 发给 `leader_round.leader_task.leader_id`，消息主题也是 `task.completed` / `task.failed`，没有 teammate 消费 directed mailbox 的路径：`src/agent_orchestra/runtime/leader_loop.py`
- `MailboxBridge` 已经支持 recipient cursor、append-only message pool、subscription cursor 等在线协作所需的最小邮箱能力：`src/agent_orchestra/runtime/protocol_bridge.py`
- 但 `ResidentTeammateRuntime` 完全没有 mailbox 依赖，也没有 cursor state：`src/agent_orchestra/runtime/teammate_runtime.py`

判断：

- mailbox 基础设施已经够用，但 teammate runtime 还没有接上。
- 当前 mailbox 主语义仍是“结果回投给 leader”，不是“给 teammate 自主投喂新工作”。

### 3.4 leader 每个 cycle 仍默认执行 prompt turn，team primary execution surface 还没切走

源码事实：

- `LeaderLoopSupervisor.run(...)` 每个 cycle 先 poll leader mailbox，再总是构造 `compile_leader_turn_assignment(...)` 并执行 leader assignment：`src/agent_orchestra/runtime/leader_loop.py`
- leader turn 完成后才 `ingest_leader_turn_output(...)`，再调用 `_run_teammates(...)` 执行 teammate work：`src/agent_orchestra/runtime/leader_loop.py`
- 即使 base turn budget 用完，只要有 mailbox，新 cycle 也会消耗 mailbox follow-up budget 并再次执行 leader turn：`src/agent_orchestra/runtime/leader_loop.py`
- 当 evaluator 判定已完成但 teammate 本轮产出了 mailbox envelope 时，状态会被强制改回 `RUNNING`，摘要明确写成 “Waiting for the leader to consume fresh teammate mailbox results.”：`src/agent_orchestra/runtime/leader_loop.py`

判断：

- 当前系统把 leader prompt 仍当作默认推进器，而不是按需触发动作。
- teammate 的结果要真正进入 delivery 完成链，仍依赖 leader 再跑一轮来消费 mailbox。

### 3.5 `TaskCard` 目前只有 claim metadata，没有 teammate online-loop state

源码事实：

- `TaskCard` 只记录 `owner_id / claim_session_id / claimed_at / claim_source / blocked_by / derived_from / status` 这类 task ownership 字段：`src/agent_orchestra/contracts/task.py`
- `GroupRuntime` 已经提供原子 `claim_task(...)` 与 `claim_next_task(...)`，并且对 `pending + unowned + unblocked` 过滤有正式测试：`src/agent_orchestra/runtime/group_runtime.py`, `tests/test_runtime.py`

判断：

- 当前 task surface 已经能支撑 teammate 自主 claim。
- 但 “哪个 teammate loop 正在线、看到哪里、是否已被 directed mailbox 唤醒” 这些在线状态还没有正式落点，所以 task 仍像 leader turn 派发后的执行载体，而不是 activation 之后的持续协作面的一部分。

## 4. 第一波最小实现路径

### 4.1 先新增 teammate-owned online loop，不先重做统一 Agent 契约

建议新增独立模块，例如：

- `src/agent_orchestra/runtime/teammate_online_loop.py`

职责只做一件事：

- 代表一个稳定 teammate slot，例如 `team-x:teammate:1`
- 自己维护 direct mailbox cursor
- 自己循环执行 `directed mailbox -> claim_next_task -> execute -> publish result`
- idle 时保持在线等待，不再把 “没有当前 assignment” 当作终止条件

第一波明确不做：

- 不引入完整 `AgentSessionHost`
- 不重构 `SuperLeader`
- 不先做 task/message/evidence 的全量契约升级

### 4.2 复用现有 surface，而不是重写 store / mailbox

第一波可直接复用的现有能力已经足够：

- 任务 claim：`GroupRuntime.claim_next_task(...)`
- task visibility：`GroupRuntime.list_visible_tasks(... viewer_role=\"teammate\")`
- direct recipient cursor：`MailboxBridge.get_cursor(...) / acknowledge(...)`
- append-only shared view：`MailboxBridge.list_message_pool(...) / subscription cursor`
- teammate result publication：沿用当前 `_run_teammate_assignment(...)` 生成的 shared mailbox envelope 和 blackboard entry

关键收敛点：

- 不再在 teammate autonomous claim 里直接 `store.list_tasks(...) + Python filter`，而改成真正调用 `claim_next_task(...)`
- 不再让 teammate runtime 依赖 leader turn 注入 `turn_index / backend / working_dir`
- teammate 自己持有这些配置，leader 只在 activation 时传一次

### 4.3 把 leader 对 teammate 的责任缩到 “activate once + converge later”

第一波 leader 只需要做两个动作：

1. 第一次 turn 产出 team activation/task contract。
2. 启动或确保 teammate slot online loop 已存在。

之后：

- teammate loop 自己继续 claim 和执行 team task
- leader loop 在没有高价值判断需求时，不应再为了“让 teammate 继续干活”而重跑 prompt turn
- leader 后续 prompt 只用于：
  - 消费关键 mailbox 结果并做收敛
  - 处理 blocker / 冲突 / replan
  - 判断 lane completion

这一步并不要求一次性把 `leader-continuous-convergence-loop` 全做完，只要先消除 “teammate 继续工作必须靠 leader 再 turn 一次” 这个硬依赖即可。

### 4.4 第一波最小代码形态

推荐的最小代码形态是：

- 保留 `ResidentTeammateRuntime` 作为“drain 当前 runnable assignments”的低层执行壳
- 在新建的 `teammate_online_loop.py` 里包一层长期循环：
  - poll teammate inbox
  - 解析 directed message
  - 若无 directed work，则 `claim_next_task(...)`
  - 若拿到 assignment，则调用 `ResidentTeammateRuntime.run(...)` drain 当前 runnable set
  - drain 完成后回到 idle wait，而不是退出整个 teammate identity

这样可以最大化复用现有 resident kernel、worker supervisor、claim metadata 与 teammate result publication，而不必先重写整个 runtime 分层。

## 5. 第一波文件建议

### 5.1 已落地 / 建议补充

- `src/agent_orchestra/runtime/teammate_online_loop.py`
  - 当前 slice已新增这个纯 teammate loop scaffold，接口通过注入的 mailbox poll / task claim / idle wait 回调保持隔离，返回 metrics 供测试断言。loop 的主要行为为：先尝试 poll mailbox，没消息时才 claim 任务，只有在两者都无结果时才触发 idle backoff。该 loop 处于首波 isolation，并为 leader / protocol integration留出 thin adapter 机会。
- `tests/test_teammate_online_loop.py`
  - 新增纯 loop 单元测试，先活跃 mailbox，再 claim 多任务，验证 idle wait 仅在完全空闲时触发。提供小批量 iterations + metrics 验证，确保 loop 不依赖 leader 或 mailbox bridge 初始化。

### 5.2 建议修改

- `src/agent_orchestra/runtime/leader_loop.py`
  - 从 “每 turn 创建 `ResidentTeammateRuntime`” 改成 “启动/管理长期 teammate loop”
  - 把 teammate assignment build / result publish 的共享逻辑抽出成可复用 helper
- `tests/test_leader_loop.py`
  - 增加 leader activation 后 teammate 仍能继续工作、且不需要新 leader turn 的集成测试

### 5.3 第一波尽量不改

- `src/agent_orchestra/runtime/protocol_bridge.py`
  - 现有 direct cursor / subscription cursor 已足够第一波 teammate inbox 使用
- `src/agent_orchestra/contracts/task.py`
  - 现有 `claim_*` 字段已足够承载 first-wave evidence

### 5.4 只在需要时再改

- `src/agent_orchestra/runtime/group_runtime.py`
  - 如果不想让 online loop 直接自己拼 `claim_next_task(...)` 参数，可以加一个薄封装，例如 `claim_next_team_task_for_teammate(...)`
  - 否则第一波也可以保持不动

## 6. 证明测试应该长什么样

### 6.1 纯 teammate loop 证明

新增 `tests/test_teammate_online_loop.py`，至少覆盖：

- teammate loop 以 1 个 slot 启动后，先执行一个初始 task
- 初始 task 完成后，无需 leader 再 turn，loop 自己继续 `claim_next_task(...)`
- 同一 teammate session/identity 连续处理多个 team task
- mailbox cursor 在 directed message 被消费后正确前进

这个测试是最直接的“没有 leader 参与，teammate 也能继续工作”的证据。

### 6.2 leader integration 证明

在 `tests/test_leader_loop.py` 增加一个最小集成回归：

- leader turn 1 产出多个 team task
- leader 只执行这 1 次 activation turn
- 后续 2~3 个 teammate task 由同一 online teammate slot 连续 claim / execute / publish
- 断言：
  - `leader_requests == 1`
  - `teammate_requests > 1`
  - 所有 team task 最终 `COMPLETED`
  - 至少一个后续 task 的开始时间晚于 leader turn 1 结束

这条测试才是真正证明“teammate can continue claiming/processing work without a new leader turn”。

### 6.3 mailbox-driven follow-up 证明

新增一个 directed mailbox 回归：

- 在 leader turn 1 之后，向 `team-x:teammate:1` 的 inbox 发送 directive
- 不触发任何新 leader turn
- teammate loop 消费该 directive、ack cursor、执行对应工作

这条测试能证明 first-wave online loop 不是“只有 task-list claim”，而是已经具备 mailbox 工作入口。

### 6.4 现有测试会如何变化

当前这些测试会继续成立，但语义需要重写说明：

- `tests/test_leader_loop.py`
  - 现有 resident slot refill / overflow claim 测试证明的是 “leader-owned resident shell”
  - 第一波完成后，应新增更强断言，避免它们仍然允许 `leader_requests == 2`
- `tests/test_self_hosting_round.py`
  - 当前主 gate 已切到 `teammate_execution_evidence`，`leader_consumed_mailbox` 仅保留为诊断性 metadata
  - 后续更强测试仍应继续验证 teammate 是否在没有新增上游 prompt turn 的情况下继续 drain team task、回写结果，并提交 durable cursor/session

## 7. merge contention 热点

高冲突文件：

- `src/agent_orchestra/runtime/leader_loop.py`
  - 当前 teammate helper、mailbox follow-up、delivery convergence 都集中在同一文件
  - `resident-teammate-online-loop` 和 `leader-continuous-convergence-loop` 几乎必然同时碰这里
- `tests/test_leader_loop.py`
  - 当前所有 resident teammate / mailbox follow-up 断言也都集中在这里

中冲突文件：

- `src/agent_orchestra/runtime/group_runtime.py`
  - 如果 leader-loop work 也在推进 activation/task semantics，这里会和 teammate claim helper 产生交叉
- `src/agent_orchestra/runtime/protocol_bridge.py`
  - 如果另一条线同时改 shared subscription / mailbox cursor / protocol bus routing，这里也会冲突

低冲突但容易形成微冲突的文件：

- `src/agent_orchestra/contracts/task.py`
  - 文件很小，任何新增字段都容易形成行级冲突

降低冲突的建议：

- 把 teammate-specific orchestration 尽量放进新文件 `teammate_online_loop.py`
- 把 `leader_loop.py` 里的 teammate assignment build / teammate result publish 抽到小 helper 文件
- 把新的证明测试优先放到 `tests/test_teammate_online_loop.py`，不要继续把所有行为都压进 `tests/test_leader_loop.py`

## 8. 相关文档

- `resource/knowledge/agent-orchestra-runtime/online-collaboration-runtime-migration.md`
- `resource/knowledge/agent-orchestra-runtime/team-parallel-execution-gap.md`
- `resource/knowledge/agent-orchestra-runtime/resident-hierarchical-runtime.md`
