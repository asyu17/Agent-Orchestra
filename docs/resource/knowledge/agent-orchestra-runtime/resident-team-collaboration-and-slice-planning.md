# agent_orchestra 常驻 Team 协作与 Slice 规划

## 1. 一句话结论

截至 2026 年 4 月 6 日，本文记录的 3 条 team 级语义已经是当前 mainline，而非仅“后续设计目标”：一，leader protocol 已切到 `sequential_slices / parallel_slices`，旧 `teammate_tasks` 已停止接受；二，`max_concurrency` 已退出 live team-budget path，旧输入会被显式拒绝；三，promptless convergence + teammate-owned work surface 已成为 baseline，leader mailbox helper 仅处理 leader inbox，teammate-owned verification loop 进入 assignment execution contract 主线，execution guard 退回 fallback/safety-net。并且从当前版本起，resident collaboration 的 primary evidence 也已经前移到 runtime-native `teammate_execution_evidence` 与 authoritative `final_report.verification_results`；只有 host-owned evidence rewrite 这一层仍残留旧 follow-up heuristic。

## 2. 范围与资料来源

- 直接相关知识文档：
  - [online-collaboration-runtime-migration.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/online-collaboration-runtime-migration.md)
  - [team-parallel-execution-gap.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/team-parallel-execution-gap.md)
  - [parallel-team-coordination.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/parallel-team-coordination.md)
  - [hierarchical-online-agent-runtime-gap-list.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/hierarchical-online-agent-runtime-gap-list.md)
  - [implementation-status.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/implementation-status.md)
  - [current-priority-gap-list.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/current-priority-gap-list.md)
- 当前实现中与本次设计决议直接相关的代码入口：
  - [bootstrap.py#L377](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/self_hosting/bootstrap.py#L377)
  - [leader_output_protocol.py#L164](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_output_protocol.py#L164)
  - [leader_output_protocol.py#L238](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_output_protocol.py#L238)
  - [leader_loop.py#L1060](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py#L1060)
  - [leader_loop.py#L1417](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py#L1417)
  - [teammate_work_surface.py#L226](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/teammate_work_surface.py#L226)
  - [teammate_work_surface.py#L1330](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/teammate_work_surface.py#L1330)
  - [teammate_work_surface.py#L1418](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/teammate_work_surface.py#L1418)
  - [group_runtime.py#L1217](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/group_runtime.py#L1217)
  - [group_runtime.py#L1308](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/group_runtime.py#L1308)
  - [worker_supervisor.py#L875](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/worker_supervisor.py#L875)
- 对照来源：
  - [agent-team.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/claude-code-main/agent-team.md)

## 3. 设计决议一：取消 `max_concurrency` 作为 Team Budget 主语义

### 3.1 正式决议

当前在规划层、self-hosting bootstrap、leader output 和 team runtime 语义里，`max_concurrency` 已不是合法的正式字段。team 级预算只保留：

- `max_teammates`
- `max_iterations`
- 其他与 authority、owned paths、verification、time/token 相关的约束

默认目标上限改为：

- `max_teammates = 20`

这里的 `20` 表示 team 可拥有的最大 teammate 数量上限，不表示系统承诺任何时刻一定会物理并发 20 个 transport worker。实际运行中仍可存在底层 backpressure，例如：

- provider / transport 可用性
- session host 资源压力
- permission / verification gate
- `owned_paths` 冲突
- 同一 slice 的依赖尚未满足

这些压力由 runtime 实际激活状态与控制面策略约束，不再通过 `team budget.max_concurrency` 表达。

### 3.2 设计原因

这条切换的原因是：

1. `max_concurrency` 会把 team 误导成“批处理执行器”，而不是“长期在线协作体”。
2. 它会让 planner/leader 在高层就预设串行或低并发批次，过早把执行形状固化。
3. 旧语义会把 planner 与 leader 过早锁进批处理心智，弱化 promptless convergence 与 resident collaboration 的连续执行面。

### 3.3 当前落地状态与残留

截至 2026 年 4 月 6 日，设计决议一已经进入代码主线并完成行为切换：

- self-hosting bootstrap 默认已经改成 `max_teammates = 20`，并且 workstream/dynamic seed 的正式预算导出不再包含 `max_concurrency`：[bootstrap.py#L375](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/self_hosting/bootstrap.py#L375)
- `WorkerBudget / WorkstreamTemplate / template io / dynamic planner` 的正式预算字段都已经收敛到 `max_teammates / max_iterations / max_tokens / max_seconds`，旧 `max_concurrency` 输入会被显式拒绝：[execution.py#L42](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/contracts/execution.py#L42), [template.py#L10](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/planning/template.py#L10), [io.py#L10](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/planning/io.py#L10), [dynamic_superleader.py#L314](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/planning/dynamic_superleader.py#L314)
- `bootstrap_round` 的 leader 指令与 lane/team directive budget payload 也已经不再向 leader 暴露 `max_concurrency`：[bootstrap_round.py#L31](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/bootstrap_round.py#L31), [bootstrap_round.py#L67](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/bootstrap_round.py#L67)
- `TeammateWorkSurface` 已不再从 `leader_task.budget.max_concurrency` 读取并发值；resident teammate slot 的并行上限现在跟随被激活的 slot 数，也就是 `max_teammates` 所决定的 team 容量，而不是额外的 team-level concurrency 字段：[teammate_work_surface.py#L226](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/teammate_work_surface.py#L226), [teammate_work_surface.py#L1023](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/teammate_work_surface.py#L1023)

当前 runtime 行为面已经没有旧语义兼容路径：

- `WorkerBudget` 与 `WorkstreamTemplate` 的旧参数入口已经删除
- template I/O 与 dynamic planner 如果再收到 `max_concurrency`，会直接报错而不是静默吞掉
- 当前残留只剩文档/测试命名层的历史痕迹，不再属于 runtime 行为面

判断：

- 当前代码事实已经不再是“team budget 由 `max_concurrency` 驱动”
- 当前正式主语义已经切到“最大 team 数量 + resident collaboration + dependency-driven activation”

## 4. 设计决议二：Leader 输出协议已切到 Slice 规划

### 4.1 当前协议基线

当前 `Leader` 输出协议基线是：

- 一段 summary
- 一个或多个 `sequential_slices`
- 一个或多个 `parallel_slices`

并且 `leader_output_protocol` 已显式拒绝旧 `teammate_tasks` 字段。这个切换已经不是“建议”，而是 live parser 行为。

这组协议的作用是显式表达：

- 哪些工作必须顺序推进
- 哪些工作可以并行展开
- 哪些工作依赖前一个 slice 的结果、接口或黑板证据
- 哪些工作是同一并行组里的互补任务，而不是互不相干的两条随机 task

### 4.2 新的规划语义

`Leader` 输出当前应具备的最小 slice 语义如下：

- `slice_id`
- `title`
- `goal`
- `reason`
- `mode`
  - `sequential`
  - `parallel`
- `depends_on`
- `parallel_group`
- `owned_paths`
- `verification_commands`
- `acceptance_checks`

字段名可以等价映射，但必须表达两类信息：

1. 依赖关系
   - 哪个 slice 依赖哪个 slice
2. 并行标签
   - 哪几个 slice 可以作为同一批可并行工作一起点火

### 4.3 Slice 与 Task List 的关系

这里需要明确 3 层面，不要再混在一起：

- `Slice graph`
  - 是 leader/team 的局部执行规划面
  - 负责表达依赖关系和并行关系
- `Task list`
  - 是 runtime 的 live work surface
  - 负责 claim、status、owner、verification、delivery
- `Blackboard`
  - 是证据与协作面
  - 负责 summary、proposal、blocker、result、schema notice、handoff evidence

正式判断：

- `slice` 不是替代 task list
- `slice` 是 task list 之上的规划和激活结构
- task 仍然全局统一，当前 runtime 已把 `slice_id / depends_on / parallel_group` 落到 assignment metadata 与 execution evidence 链路，后续重点是放大调度消费深度而非重做协议切换

### 4.4 顺序与并行的正式语义

当前 team runtime 的 slice 语义是：

- `sequential slice`
  - 只有在依赖 slice 满足后，才进入可激活状态
- `parallel slice`
  - 可与同组其他 slice 一起进入可激活状态
  - 允许多个 teammate 自主 claim 并并行推进

这意味着 leader 不再是“列 flat task 让 runtime 猜顺序”，而是显式提供局部工作图并把收敛责任留给持续运行的 team execution surface。

## 5. 设计决议三：Team Runtime 切到真正的 Resident Collaboration

### 5.1 主语义切换

当前 team runtime 的正式 baseline 为：

- `task` 只负责 activation / contract
- 真正持续推进解题的是 promptless convergence 下的常驻在线 agent loop
- 主工作面由 `mailbox + task list + TeamBlackboard + runtime-owned coordination commit` 共同构成
- leader mailbox helper 只处理 leader inbox，teammate recipient cursor/session truth 归 teammate work surface + session host

### 5.2 Teammate 的目标语义

`Teammate` 的正式语义已落到主线：

- 被独立视为长期在线 agent 实体
- 持续 polling：
  - mailbox
  - team task list
  - TeamBlackboard summary stream
- 空闲时自主 claim：
  - `pending`
  - `unowned`
  - `unblocked`
  - 且满足自身 scope / authority / owned_paths 的工作
- 持续发布：
  - summary
  - result
  - blocker
  - proposal
- code-edit assignment 下持续执行 teammate-owned verification loop，并把 `requested_command -> command` fallback 映射后的结构化结果写入 authoritative `final_report.verification_results`

判断：

- teammate 不是“leader turn 里的临时批次 worker”
- teammate 是“被 activation 点火后长期存在的自治执行体”，execution guard 仅在协议异常/证据不一致时承担 fallback/safety-net

### 5.3 Leader 的目标语义

`Leader` 的正式目标已收敛成两件事：

- activation center
- convergence center

它当前不再承担：

- team 内 assignment shell
- team 内唯一主动执行中枢
- 用 flat task list 隐式代替团队局部工作图

Leader 当前主要负责：

- 生成或修正 slice graph
- 激活 team
- 处理高价值判断、冲突消解、重分解
- 聚合 mailbox / blackboard / task state
- 在需要高价值判断时再触发 prompt turn，避免把 team 主执行面退回 prompt-step 驱动

### 5.4 Team 内可见性语义

当前 team 内消息面应至少满足：

- directed mailbox 仍按 recipient 维护权威投递、ACK 和 cursor
- 但同 team 所有 agent 默认都能看到 team activity 的摘要索引
- 如果 policy 允许，可以基于引用索引拉取更完整正文

这意味着：

- 默认不是“所有全文都自动广播”
- 也不是“除了收件人外其他 teammate 完全不知道发生了什么”
- 而是“同 team 默认共享 summary index，全文按 policy/ref 再拉”

### 5.5 与 SuperLeader 的边界

当前设计不会改变已有层级边界：

- `SuperLeader` 仍不直接给 teammate 发任务
- `SuperLeader` 组织的是多个 team / leader
- cross-team 协作仍通过受控 mailbox / subscription / circle 完成

所以这次设计是：

- 把 Claude 式长期在线协作引入到每一层 agent 的底座
- 但继续保留 `SuperLeader -> Leader -> Teammate` 的分层收敛结构

### 5.6 Host-Owned Activation Boundary 与 Resident Evidence

当前 team runtime 的 host-owned boundary 已经不是“后续目标”，而是已经进入 mainline 的 first-cut：

- ready lane 会先通过 `LeaderLoopSupervisor.ensure_or_step_session(...)` 进入 `leader_session_host` launch boundary，而不是直接把 lane 视作裸 `leader_loop.run(...)` task：[test_leader_loop.py#L1806](/Volumes/disk1/Document/code/Agent-Orchestra/tests/test_leader_loop.py#L1806)
- team 内 live teammate path 已切到 `TeammateWorkSurface.run(...) -> per-slot TeammateOnlineLoop`，因此 host 已经拥有 teammate recipient cursor/session truth 的主线入口
- promptless continuation 已经能够在 activation 之后继续推进 teammate work，并且 directed mailbox 可以在不增加 fresh leader prompt turn 的条件下被优先消费：[test_leader_loop.py#L1960](/Volumes/disk1/Document/code/Agent-Orchestra/tests/test_leader_loop.py#L1960), [test_leader_loop.py#L2038](/Volumes/disk1/Document/code/Agent-Orchestra/tests/test_leader_loop.py#L2038)

当前 resident collaboration 的证据语义也已经发生切换：

- team 级 completion/validation 现在优先消费 runtime-native `teammate_execution_evidence`，而不是把 `leader_consumed_mailbox` 继续当成唯一关闭条件：[bootstrap.py#L664](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/self_hosting/bootstrap.py#L664), [bootstrap.py#L690](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/self_hosting/bootstrap.py#L690)
- code-edit teammate assignment 的 authoritative verification evidence 现在是 `final_report.verification_results`，并允许通过 `requested_command -> command` 表达环境兼容 fallback；execution guard 只在 authoritative evidence 缺失或不等价时回退：[test_worker_supervisor_protocol.py#L758](/Volumes/disk1/Document/code/Agent-Orchestra/tests/test_worker_supervisor_protocol.py#L758), [test_worker_supervisor_protocol.py#L826](/Volumes/disk1/Document/code/Agent-Orchestra/tests/test_worker_supervisor_protocol.py#L826), [test_execution_guard.py#L284](/Volumes/disk1/Document/code/Agent-Orchestra/tests/test_execution_guard.py#L284)

但这条线还没有完全收口成最终形态：

- `_delegation_validation_metadata(...)` 仍把 `leader_turn_count > 1` 当作 `validated` 的组成条件之一：[bootstrap.py#L702](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/self_hosting/bootstrap.py#L702)
- 因此当前应把“resident collaboration evidence 已切换”与“host-owned evidence rewrite 已完成”分开理解；前者已经是主线事实，后者仍是 residual backlog

## 6. 对现有知识库的冲突裁决（2026-04-06）

从本文件生效起，下面几类旧表述需要按“当前代码事实”和“正式目标”分开理解：

1. 任何把 `max_concurrency` 写成后续推荐 team budget 语义的表述
   - 现在应视为旧建议；当前 runtime 会拒绝这条输入路径
2. 任何把 `leader turn -> mailbox follow-up -> result drain` 写成 team 主执行语义的表述
   - 现在应视为过时框架；当前 baseline 是 promptless convergence + teammate-owned work surface
3. 任何只用扁平 `teammate_tasks` 讨论 team 拆解的表述
   - 现在应升级为 `sequential_slices / parallel_slices + task list + blackboard` 三层分工

## 7. 对后续 gap / 实现计划的直接影响

这 3 条决议对后续 `online-collaboration-runtime` 主线的直接影响是：

1. `team-primary-semantics-switch`
   - 已完成 team budget 与 leader protocol 切换；后续重点是 store-level coordination transaction 和 host-owned execution truth 收敛
2. `leader-convergence-first-loop`（若继续推进）
   - 不再讨论 flat-task 到 slice 的 0->1 切换，改为提升 promptless convergence 与 slice metadata（`depends_on / parallel_group`）的 runtime/evaluator 消费深度
3. `resident-teammate-online-loop`
   - 已进入 resident collaboration-first；后续重点是 standalone teammate host、long-lived worker/reconnect hardening、以及 host-owned evidence gate cleanup
4. `cross-team-collaboration-layer`
   - 需要和本文件定义的“同 team 摘要索引默认可见”保持一致

不建议再重开的旧子 gap：

- `team-budget-simplification`
- `leader-slice-graph-output`

推荐继续推进的 residual 子 gap：

- `store-level-coordination-transaction`
- `host-owned-session-truth-convergence`
- `leader-session-runtime-reentrant-recovery`
- `verification-evidence-gate-hardening`
- `self-hosting-evidence-rewrite`

## 8. 相关文档

- [online-collaboration-runtime-migration.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/online-collaboration-runtime-migration.md)
- [team-parallel-execution-gap.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/team-parallel-execution-gap.md)
- [parallel-team-coordination.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/parallel-team-coordination.md)
- [hierarchical-online-agent-runtime-gap-list.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/hierarchical-online-agent-runtime-gap-list.md)
- [current-priority-gap-list.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/current-priority-gap-list.md)
- [remaining-work-backlog.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/remaining-work-backlog.md)
