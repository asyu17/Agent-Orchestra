# agent_orchestra team-parallel-execution gap

## 1. 一句话结论

截至 2026 年 4 月 6 日，`team-parallel-execution` 已不再是“leader turn -> mailbox follow-up -> result drain”的主执行语义。当前 mainline 已切到：`Leader` 输出协议只接受 `sequential_slices / parallel_slices`（旧 `teammate_tasks` 显式拒绝）；team 执行面由 `TeammateWorkSurface.run(...)` + per-slot `TeammateOnlineLoop` 承担；promptless convergence 与 teammate-owned work surface/evidence 已是 baseline；`max_concurrency` 退出 live team-budget path 且旧输入会报错；code-edit teammate assignment 的 verification 由 assignment execution contract + `final_report.verification_results` 收口，runtime execution guard 退回 fallback/safety-net。

## 2. 范围与资料来源

- Agent Orchestra 当前实现与知识：
  - [implementation-status.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/implementation-status.md)
  - [current-priority-gap-list.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/current-priority-gap-list.md)
  - [resident-team-collaboration-and-slice-planning.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/resident-team-collaboration-and-slice-planning.md)
  - [parallel-team-coordination.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/parallel-team-coordination.md)
  - [leader_output_protocol.py#L164](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_output_protocol.py#L164)
  - [leader_output_protocol.py#L238](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_output_protocol.py#L238)
  - [resident_kernel.py#L12](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/resident_kernel.py#L12)
  - [leader_loop.py#L1015](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py#L1015)
  - [leader_loop.py#L1060](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py#L1060)
  - [leader_loop.py#L1417](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py#L1417)
  - [teammate_work_surface.py#L226](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/teammate_work_surface.py#L226)
  - [teammate_work_surface.py#L1330](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/teammate_work_surface.py#L1330)
  - [teammate_work_surface.py#L1418](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/teammate_work_surface.py#L1418)
  - [group_runtime.py#L1217](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/group_runtime.py#L1217)
  - [group_runtime.py#L1308](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/group_runtime.py#L1308)
  - [worker-lifecycle-protocol-and-lease.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/worker-lifecycle-protocol-and-lease.md)
  - [superleader.py#L375](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/superleader.py#L375)
  - [test_leader_loop.py#L1009](/Volumes/disk1/Document/code/Agent-Orchestra/tests/test_leader_loop.py#L1009)
  - [test_teammate_work_surface.py#L163](/Volumes/disk1/Document/code/Agent-Orchestra/tests/test_teammate_work_surface.py#L163)
  - [test_teammate_work_surface.py#L388](/Volumes/disk1/Document/code/Agent-Orchestra/tests/test_teammate_work_surface.py#L388)
  - [test_worker_supervisor_protocol.py#L713](/Volumes/disk1/Document/code/Agent-Orchestra/tests/test_worker_supervisor_protocol.py#L713)
  - [test_execution_guard.py#L225](/Volumes/disk1/Document/code/Agent-Orchestra/tests/test_execution_guard.py#L225)
- Claude team agent 对照来源：
  - [agent-team.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/claude-code-main/agent-team.md)
  - [AgentTool.tsx#L266](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/tools/AgentTool/AgentTool.tsx#L266)
  - [spawnMultiAgent.ts#L840](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/tools/shared/spawnMultiAgent.ts#L840)
  - [spawnMultiAgent.ts#L910](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/tools/shared/spawnMultiAgent.ts#L910)
  - [inProcessRunner.ts#L624](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/swarm/inProcessRunner.ts#L624)
  - [inProcessRunner.ts#L689](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/swarm/inProcessRunner.ts#L689)
  - [inProcessRunner.ts#L853](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/swarm/inProcessRunner.ts#L853)
  - [inProcessRunner.ts#L1015](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/swarm/inProcessRunner.ts#L1015)
  - [useInboxPoller.ts#L843](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/hooks/useInboxPoller.ts#L843)

## 3. Team 内部并行差异

### 3.1 Claude team agent 的 team 内部并行

Claude 的 team 内部并行单元是“独立 teammate 实体”，不是 leader 里的临时任务槽位。

直接证据：

- `AgentTool` 在 team 场景下会进入 `spawnTeammate()` 路径，而 teammate 不能再 spawn teammate，说明 team roster 是 flat team，不是递归子 team：[AgentTool.tsx#L266](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/tools/AgentTool/AgentTool.tsx#L266)
- in-process teammate 先 `spawnInProcessTeammate()`，再 `startInProcessTeammate()`，并且启动是 fire-and-forget，因此 leader 不会阻塞在单个 teammate 上：[spawnMultiAgent.ts#L840](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/tools/shared/spawnMultiAgent.ts#L840), [spawnMultiAgent.ts#L910](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/tools/shared/spawnMultiAgent.ts#L910)
- teammate idle 时不会退出，而是持续轮询 mailbox，并在空闲时继续从 task list claim `pending + unowned + unblocked` 任务：[inProcessRunner.ts#L624](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/swarm/inProcessRunner.ts#L624), [inProcessRunner.ts#L689](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/swarm/inProcessRunner.ts#L689), [inProcessRunner.ts#L853](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/swarm/inProcessRunner.ts#L853), [inProcessRunner.ts#L1015](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/swarm/inProcessRunner.ts#L1015)
- leader 侧通过 inbox poller 异步接收 teammate 结果；leader 空闲时立即提交为新 turn，leader 忙时排队稍后投递：[useInboxPoller.ts#L843](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/hooks/useInboxPoller.ts#L843)

结论：

- Claude 的 team 内部并行是“常驻 teammate 并发 + mailbox/task-list 混合驱动”
- Claude 的 leader 更像调度者与收件者，不是 team 内部唯一的主动执行中枢

### 3.2 Agent Orchestra 当前的 team 内部并行

`agent_orchestra` 当前的 team 内部并行单元，已经分成两层，但内层已经从旧 `ResidentTeammateRuntime` 过渡到 teammate-owned work surface 主线：

- 外层协调壳：`Leader` 通过共享 `ResidentCoordinatorKernel` 运行
- 内层执行面：`TeammateWorkSurface` 通过 per-slot `TeammateOnlineLoop` 负责 directed mailbox 消费、autonomous claim、assignment 执行、`task.receipt|task.result` side effect 与 slot session truth 写回

直接证据：

- leader prompt turn 输出会经过 `leader_output_protocol` 解析成 slice-aware teammate assignments，并显式拒绝旧 `teammate_tasks` 输入：[leader_output_protocol.py#L164](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_output_protocol.py#L164), [leader_output_protocol.py#L238](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_output_protocol.py#L238)
- `LeaderLoopSupervisor` 已把 promptless convergence 作为标准路径；在无新增 prompt turn 时也会按 open team tasks 触发 teammate work surface 继续推进：[leader_loop.py#L1060](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py#L1060)
- `TeammateWorkSurface.run(...)` 已成为 host-owned slot loop 入口，并在 directed claim materialization 时发 `task.receipt`、在 assignment finalize 时发 `task.result`：[teammate_work_surface.py#L1330](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/teammate_work_surface.py#L1330), [teammate_work_surface.py#L1418](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/teammate_work_surface.py#L1418)
- runtime-owned coordination commit 已通过 `commit_directed_task_receipt(...) / commit_teammate_result(...)` 收口到同一路径，并同步 lane delivery snapshot 的 teammate coordination metadata：[group_runtime.py#L1217](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/group_runtime.py#L1217), [group_runtime.py#L1308](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/group_runtime.py#L1308)
- `GroupRuntime` / store 已经把 `claim_task` 与 `claim_next_task` 做成正式原子 surface，`TaskCard` 也新增 `claim_session_id / claimed_at / claim_source`，因此 resident/autonomous claim 不再只是隐含在 leader 局部变量里的行为：[group_runtime.py#L755](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/group_runtime.py#L755), [group_runtime.py#L782](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/group_runtime.py#L782), [task.py#L9](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/contracts/task.py#L9)
- leader mailbox helper 当前只处理 leader 自己的 inbox；teammate recipient cursor/session truth 不再由 leader 保管：[leader_loop.py#L1417](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py#L1417), [teammate_work_surface.py#L226](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/teammate_work_surface.py#L226)
- teammate-owned verification loop 已进入主线：code-edit assignment 会注入 required verification set，worker `final_report.verification_results` 是 authoritative evidence；execution guard 只在协议异常或证据缺失时作为 fallback/safety-net：[worker-lifecycle-protocol-and-lease.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/worker-lifecycle-protocol-and-lease.md), [test_worker_supervisor_protocol.py#L713](/Volumes/disk1/Document/code/Agent-Orchestra/tests/test_worker_supervisor_protocol.py#L713), [test_execution_guard.py#L225](/Volumes/disk1/Document/code/Agent-Orchestra/tests/test_execution_guard.py#L225)

结论：

- Agent Orchestra 现在已经具备 team 内并行与 resident teammate online loop，不再是 leader-owned bounded async dispatch
- `team-parallel-execution` 对应的基础主线已经完成
- 当前剩余差距已转移到更深的 control-plane hardening，而不是 base team runtime capability 或旧协议迁移

### 3.3 当前差距的本质

判断：

- Claude 的 team runtime 主语义是“独立 teammate 常驻存在，并持续从 mailbox / task list 吸收新工作”
- Agent Orchestra 当前已经切到“promptless convergence + teammate-owned work surface”主语义，leader turn 不再是 team 内执行副作用的唯一入口

因此当前更准确的判断是：

- `team-parallel-execution` 与 `leader protocol slices` 已是 mainline baseline，不应重开成“从 flat task list 迁移”的 0->1 gap
- 后续差异聚焦在更深 hardening：store-level coordination transaction、standalone teammate host、可重入 leader session runtime、cross-transport durable reconnect、以及更强的 host-owned evidence gate cleanup

这也与更新后的 [implementation-status.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/implementation-status.md) 和 [current-priority-gap-list.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/current-priority-gap-list.md) 保持一致。

### 3.4 当前真正缺的不是“更多任务”，而是“更强在线协作”

如果按 Claude 的运行感受来对齐，那么 `task` 在 team 内的理想位置应该是：

- 来自 leader 的初始推力
- 用来定义边界、约束、验收
- 但不是持续推动 team 前进的唯一引擎

当前 Agent Orchestra 的 residual 主要在“更彻底在线协作”，不是“基础并行能力缺失”。当前剩余缺口可以直接从知识主线看到：

- `task.receipt` 与 `task.result` 已由 runtime-owned helper 收口，但 claim/cursor/session/blackboard/outbox/delivery snapshot 仍不是 store-level 单事务提交，仍存在崩溃窗口
- leader 已是 activation/convergence shell，但 teammate standalone host 与更彻底的 host-owned session truth 仍待继续推进
- `sequential_slices / parallel_slices` 已接入 ingest 与 metadata，但 runtime/evaluator 对 `depends_on / parallel_group` 的更深消费仍可继续放大
- verification contract 已 teammate-owned；后续应继续加强 fallback 路径一致性与 evidence gate 的 host-owned 收敛，而不是把 execution guard 重新抬成主语义

判断：

- 这正是当前与 Claude 式长期在线协作相比的 residual gap
- 下一步应继续强化在线协作面的 durability/recovery/transaction 与 evidence 收口，而不是重开旧 delegation 语义

## 4. Team 外部并行差异

### 4.1 Claude team agent 的 team 外部边界

Claude team agent 的强项主要停留在单 team 内部。

直接证据：

- team roster 是 flat team，teammate 不能再 spawn teammate：[AgentTool.tsx#L266](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/tools/AgentTool/AgentTool.tsx#L266)
- 当前知识分析中，Claude team 的核心层次是 `TeamFile / AppState.teamContext / spawn route / mailbox / REPL`，并没有形成 `SuperLeader -> Leader -> Team` 这种多层级 group runtime：[agent-team.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/claude-code-main/agent-team.md)

判断：

- 从我们目前掌握的 Claude team 代码范围看，它没有把“多 team / 多 lane / DAG 依赖调度”做成一等 runtime 抽象
- 它更像一个“单 team 内高自治协作系统”，而不是“集团级多 team orchestration 内核”

### 4.2 Agent Orchestra 当前的 team 外部并行

`agent_orchestra` 在 team 外部已经具备比 Claude team 更强的结构化并行骨架。

直接证据：

- `SuperLeaderRuntime` 会先计算 `max_active_lanes`
- 只挑选依赖已经满足的 `ready_lane_ids`
- 对 ready lanes 直接 `asyncio.create_task(...)`
- 以 `FIRST_COMPLETED` 回收 lane，并继续调度后续 ready lanes：[superleader.py#L336](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/superleader.py#L336)

这意味着当前系统已经拥有：

- group 级 `Spec DAG`
- lane dependency gate
- lane budget
- ready-lane 并发调度
- lane 级状态聚合

知识层也已将 `superleader-parallel-scheduler` 标记为已进入主线：[current-priority-gap-list.md#L56](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/current-priority-gap-list.md#L56)

### 4.3 当前 team 外部并行的差异结论

结论：

- Claude：`team 外弱`，没有正式的多 team orchestration 主语义
- Agent Orchestra：`team 外强`，已经有 superleader / lane / dependency / budget / scope 的外层并行骨架

因此当前两个系统的能力分布可以概括成：

- Claude：更成熟的单 team runtime
- Agent Orchestra：更成熟的多 team runtime 骨架

## 5. 哪些能力可以直接借鉴 Claude

### 5.1 可以直接借鉴的 team 内部能力

下面这些能力适合优先按 Claude 模式继续吸收，而不是重新发明新范式：

- teammate 作为长生命周期执行体，而不是一次性 assignment 结果容器
- idle teammate 持续轮询 mailbox
- idle teammate 主动 claim team task list 中 `pending/unowned/unblocked` 工作
- leader 通过异步 inbox/message routing 收敛结果，并在 promptless convergence 下尽量避免额外 prompt turn
- team work surface 以“共享 task list + 按收件人隔离 mailbox + 摘要/全文引用”共同构成

### 5.2 不应直接照搬的部分

下面这些地方不应简单复刻 Claude，而应继续保留 Agent Orchestra 的主语义：

- `SuperLeader -> Leader -> Teammate` 的层级结构
- `Spec DAG` 与 lane dependency gate
- scoped task list / scope visibility / authority 限制
- owned paths / verification contract / role profile 这些 runtime-owned gate
- execution guard 作为 fallback/safety-net 的防线职责
- group 级 blackboard / protocol bridge / delivery evaluator

判断：

- Claude 的 team runtime 很适合作为 Agent Orchestra 的“team 内核”
- 但 Agent Orchestra 不能退化回“只有 flat team，没有 group control plane”的模型

### 5.3 推荐的吸收方式

推荐做法不是“把 Claude team 整体拷过来”，而是分层吸收：

1. 外层继续保留 Agent Orchestra 的 `SuperLeader + Spec DAG + scoped control plane`
2. team 内部 runtime 逐步改造成更像 Claude 的 resident teammate 协作模型
3. mailbox / task list / team blackboard 的消息面与工作面逐步从“turn 驱动补位”升级到“持续订阅 + 自主 claim”

## 6. Agent Orchestra 下一步的 team runtime 升级路线图

### 6.1 已完成并应保持关闭的语义迁移

以下迁移已进入主线，不应重开为“基础能力缺失”：

- `Leader` 输出协议从 flat `teammate_tasks` 切到 `sequential_slices / parallel_slices`
- `max_concurrency` 退出正式 team budget，旧输入显式拒绝
- promptless convergence + teammate-owned work surface 成为 team 内 live execution baseline
- completion gate 以 teammate-owned execution evidence 为主，`leader_consumed_mailbox` 仅保留回退诊断价值

### 6.2 当前优先 hardening（一）：coordination transaction 与 session truth

目标：

- 把 claim/cursor/session/blackboard/outbox/delivery snapshot 进一步收敛到更强事务边界
- 把 teammate/leader/superleader session truth 更彻底并入 host-owned 单一 owner 路径

### 6.3 当前优先 hardening（二）：resident runtime 深化

目标：

- 把 leader session 从“host projection 的 bounded run”继续推进到可重入/可恢复 runtime
- 把 teammate standalone host 与 cross-transport long-lived resident worker/reconnect 再向前推一层

### 6.4 当前优先 hardening（三）：slice metadata 与 verification evidence 放大

目标：

- 在 runtime/evaluator/convergence 中更深消费 `depends_on / parallel_group`，放大 slice graph 的调度价值
- 持续加强 teammate-owned verification 证据链一致性，保持 execution guard 仅承担 fallback/safety-net

## 7. 相关文档

- [implementation-status.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/implementation-status.md)
- [current-priority-gap-list.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/current-priority-gap-list.md)
- [resident-team-collaboration-and-slice-planning.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/resident-team-collaboration-and-slice-planning.md)
- [remaining-work-backlog.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/remaining-work-backlog.md)
- [parallel-team-coordination.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/parallel-team-coordination.md)
- [agent-orchestra-framework.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/agent-orchestra-framework.md)
- [agent-abstraction-skill-and-prompt-system.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/agent-abstraction-skill-and-prompt-system.md)
- [agent-team.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/claude-code-main/agent-team.md)
