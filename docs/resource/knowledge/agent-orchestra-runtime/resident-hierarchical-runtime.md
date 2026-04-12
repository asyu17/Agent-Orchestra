# agent_orchestra Resident Hierarchical Runtime

## 1. 一句话结论

截至 2026 年 4 月 5 日，`agent_orchestra` 已经把共享 `ResidentCoordinatorKernel` 真正接进了 `LeaderLoopSupervisor` 和 `SuperLeaderRuntime`：`Leader` 的多轮 prompt/teammate 协调以及 `SuperLeader` 的 ready-lane 并发调度，现在都通过同一套 resident coordination shell 运行，并统一产出 `ResidentCoordinatorSession`；但系统的主求解模式仍偏向“leader turn 产生活、runtime 补派发”。因此剩余 gap 已经明确收敛成两件事：一是把 teammate/worker 进一步演进成 Claude 风格的自治常驻执行体，二是把执行主语义从“任务/turn 驱动”切到“任务只是第一引擎推力，后续持续在线协作才是主求解方式”。

## 2. 范围与资料来源

- 当前 Agent Orchestra 代码与知识：
  - [agent-orchestra-framework.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/agent-orchestra-framework.md)
  - [team-parallel-execution-gap.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/team-parallel-execution-gap.md)
  - [parallel-team-coordination.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/parallel-team-coordination.md)
  - [architecture.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/architecture.md)
  - [resident_kernel.py#L12](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/resident_kernel.py#L12)
  - [leader_loop.py#L1015](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py#L1015)
  - [leader_loop.py#L1417](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py#L1417)
  - [superleader.py#L375](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/superleader.py#L375)
  - [superleader.py#L509](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/superleader.py#L509)
  - [worker_supervisor.py#L560](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/worker_supervisor.py#L560)
  - [group_runtime.py#L323](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/group_runtime.py#L323)
  - [protocol_bridge.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/protocol_bridge.py)
  - [test_leader_loop.py#L491](/Volumes/disk1/Document/code/Agent-Orchestra/tests/test_leader_loop.py#L491)
  - [test_superleader_runtime.py#L359](/Volumes/disk1/Document/code/Agent-Orchestra/tests/test_superleader_runtime.py#L359)
- Claude team agent 参考：
  - [agent-team.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/claude-code-main/agent-team.md)
  - [spawnMultiAgent.ts#L840](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/tools/shared/spawnMultiAgent.ts#L840)
  - [spawnMultiAgent.ts#L910](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/tools/shared/spawnMultiAgent.ts#L910)
  - [inProcessRunner.ts#L624](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/swarm/inProcessRunner.ts#L624)
  - [inProcessRunner.ts#L689](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/swarm/inProcessRunner.ts#L689)
  - [inProcessRunner.ts#L853](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/swarm/inProcessRunner.ts#L853)
  - [useInboxPoller.ts#L843](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/hooks/useInboxPoller.ts#L843)

## 3. 当前差距到底在哪里

### 3.1 现在已经有的基础

当前 Agent Orchestra 已经具备下列基础，它们足以支撑“常驻协调器”而不需要完全推倒重来：

- `Spec DAG + scoped task list + 层级 Blackboard` 已经是明确的控制面和工作面：[agent-orchestra-framework.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/agent-orchestra-framework.md)
- `LeaderLoopSupervisor` 已经不是纯 one-shot turn：它具备 bounded multi-turn lane loop、mailbox follow-up、team task refill、slot reuse：[leader_loop.py#L742](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py#L742), [leader_loop.py#L1154](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py#L1154)
- `SuperLeaderRuntime` 已经有 ready-lane 并发调度与 dependency gate：[superleader.py#L336](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/superleader.py#L336)
- `WorkerSupervisor` 已经有 `launch / resume / reactivate / retry / fallback / escalate` 的上层 lifecycle 决策面：[worker_supervisor.py#L560](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/worker_supervisor.py#L560)
- `MailboxBridge`、cursor、digest、visibility policy、subscription 语义已经作为消息主线存在：[protocol_bridge.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/protocol_bridge.py)
- 统一的 `ResidentCoordinatorKernel` 与 `ResidentCoordinatorSession` 已经落地，负责累加 cycle / prompt / claim / subordinate dispatch / mailbox poll 计数，并导出 phase/metadata：[resident_kernel.py#L12](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/resident_kernel.py#L12)
- `LeaderLoopSupervisor.run(...)` 已经把 leader turn 变成 resident kernel 中的一个 action，并在结果里返回 `coordinator_session`：[leader_loop.py#L1015](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py#L1015), [leader_loop.py#L1515](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py#L1515)
- `SuperLeaderRuntime.run_planning_result(...)` 也已经把 ready-lane scheduler 包进 resident kernel，并在 finalize 阶段统一写回 objective state 与 `coordinator_session`：[superleader.py#L375](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/superleader.py#L375), [superleader.py#L509](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/superleader.py#L509)
- `ResidentTeammateRuntime` 已经能在 resident kernel 上做 slot fill、任一 assignment 完成后的 refill claim、以及 claim evidence 聚合，但它当前仍是由 `LeaderLoopSupervisor._run_teammates(...)` 注入 `execute_assignment` 与 `claim_refill_assignments` 回调后启动的：[leader_loop.py#L799](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py#L799), [teammate_runtime.py#L52](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/teammate_runtime.py#L52), [teammate_runtime.py#L103](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/teammate_runtime.py#L103)

判断：

- 这些基础已经足以支撑“常驻协调器语义”
- 共享 resident coordination shell 已不再停留在设计层，而是主线实现

### 3.2 真正没做成的东西

当前真正没做成的是下面这层统一形态：

- `Leader` 和 `SuperLeader` 虽然共享 resident shell，但 role adapter 仍以内联闭包/局部状态形式存在，尚未提炼成显式 `LeaderScopeAdapter` / `SuperLeaderScopeAdapter`
- teammate/worker 仍主要是 assignment/session 语义，而不是 Claude 那种“空闲即继续 mailbox poll + autonomous claim”的自治常驻实体
- `codex_cli` transport 仍主要是 one-shot `codex exec` worker，不是可持续驻留的 teammate/leader session
- 当前 resident teammate runtime 仍是“被 leader loop 拉起并注入补位规则”的执行壳，还不是“自己长期在线、自己看 mailbox/task list 决定继续拿活”的主体：[leader_loop.py#L1077](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py#L1077), [leader_loop.py#L1208](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py#L1208), [teammate_runtime.py#L129](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/teammate_runtime.py#L129)

判断：

- 现在最容易犯的错，是把“resident runtime”误解成“先让所有 backend 都支持 idle/reactivate”
- 更稳的顺序应该相反：先抽象 resident coordination 语义，再决定哪些 transport 真正能承载它
- 还要再补一条：不要把“多发几次 task、多跑几轮 leader”误判成 Claude 式长期协作。真正的差异不在循环次数，而在执行主语义是否已经从“leader turn 触发一切”切成“team 自身持续在线推进”

## 4. 统一设计原则：先抽协调内核，再选 transport

### 4.1 不要把 resident runtime 绑死在 backend reactivation 上

当前 `codex_cli` backend 的事实很明确：

- `supports_resume=True`
- `supports_reactivate=False`：[codex_cli_backend.py#L343](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/backends/codex_cli_backend.py#L343)

因此：

- `codex_cli` 现在能做的是“继续等待同一个还活着的 one-shot process”
- 它不能直接承担 Claude 式 resident teammate / resident leader 语义

判断：

- resident coordination 是上层 runtime 语义
- backend reactivation 只是实现 resident coordination 的一种 transport 能力
- 这两层必须拆开，不然 runtime 设计会被当前 `codex_cli` transport 能力上限卡死

### 4.2 统一抽象：`ResidentCoordinatorKernel`

推荐把 `SuperLeader` 和 `Leader` 都视为同一种东西：

- 一个会持续等待、持续收敛、持续 claim 工作、必要时继续派发下级的“常驻协调器”

建议抽出统一内核：

- `ResidentCoordinatorKernel`

它至少负责：

1. 维护 coordinator 自身 identity / session / cursor
2. 持续轮询控制消息、业务 mailbox、task list、blackboard digest
3. 在 idle 时决定：
   - 继续等待
   - claim 新任务
   - 发起一个新的 prompt turn
   - 派发下级 worker
   - 汇总并推进 delivery/evaluator
4. 维持 quiescent/idle/running/shutdown 的状态机
5. 向上层导出统一的 runtime record / delivery state / handoff

这个内核不区分它是在扮演 `SuperLeader` 还是 `Leader`。

### 4.3 角色差异通过 adapter 注入，而不是复制两份 runtime

推荐为 `ResidentCoordinatorKernel` 提供 role-specific adapter：

- `SuperLeaderScopeAdapter`
- `LeaderScopeAdapter`

它们负责提供：

- 可见 task scope
- 可消费 mailbox scope
- 可读 blackboard scope
- 可 claim 的工作对象
- 可 spawn 的下级类型
- 可写回的 summary / decision / proposal 位置

判断：

- `SuperLeader` 和 `Leader` 的差异主要在 scope 与 subordinate 类型
- 它们的等待、claim、poll、emit、drain、evaluate 模式高度同构
- 因此应复用同一 resident kernel，而不是做两套 loop

## 5. 统一 resident kernel 应该长什么样

### 5.1 统一状态机

建议统一 coordinator 状态机：

- `booting`
- `running`
- `idle`
- `waiting_for_mailbox`
- `waiting_for_dependencies`
- `waiting_for_subordinates`
- `quiescent`
- `shutdown_requested`
- `failed`

其中：

- `idle` 表示当前无立即可执行工作，但 session 仍存活
- `quiescent` 表示当前 scope 内确实没有可 claim 工作、无待消费邮箱、无待处理下级结果，因此允许 evaluator 决定是否完成/等待

### 5.2 统一输入面

内核每次循环读四类输入：

1. control mailbox
   - shutdown
   - budget update
   - authority decision
2. business mailbox
   - 下级结果
   - directed message
   - interface request
3. scoped task surface
   - `pending + unowned + unblocked`
4. blackboard digest / reducer snapshot
   - blocker/proposal/verification summary

这个结构正好对应 Claude team agent 的：

- mailbox poller
- task claim loop
- 长期 prompt loop

### 5.3 统一输出面

内核每次循环只做有限几类输出：

- 写 mailbox
- 更新 task 状态
- 写 blackboard entry
- 派发下级 assignment
- 触发一个新的 coordinator prompt turn
- 写 delivery/evaluator state

这保证 `SuperLeader` 和 `Leader` 两层行为模式统一，只是目标 scope 不同。

## 6. 如何让 `Leader` 达到这种形态

### 6.1 `Leader` 的 role adapter

`Leader` 需要的 adapter 很直接：

- 任务面：`team scope` task list
- 消息面：leader inbox + teammate result mailbox
- 证据面：`TeamBlackboard` + 自己的 `LeaderLaneBlackboard`
- 下级派发：`Teammate`

它的 claim 规则应接近 Claude：

1. 先处理 control/private mailbox
2. 再处理 teammate result mailbox
3. 再 claim `team scope` 里 `pending + unowned + unblocked` 任务
4. 决定是否需要发起新的 leader prompt turn
5. 若需要拆解或协调，再产生新的 teammate task / directive

### 6.2 `Leader` 的 prompt loop 语义

这一步已经进入真实主线：

- `LeaderLoopSupervisor.run(...)` 先创建 `ResidentCoordinatorSession`，再把 turn 执行、mailbox follow-up、teammate dispatch、delivery finalize 包进 `resident_kernel.run(...)`：[leader_loop.py#L1015](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py#L1015), [leader_loop.py#L1529](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py#L1529)
- 新回归测试已显式验证可注入 kernel 且结果带 `coordinator_session`：[test_leader_loop.py#L491](/Volumes/disk1/Document/code/Agent-Orchestra/tests/test_leader_loop.py#L491)

判断：

- `Leader` 现在已经不再是“只有 turn orchestration 的 runtime”
- 它已经变成“resident coordinator whose occasional action is to run a new prompt turn”

## 7. 如何让 `SuperLeader` 达到这种形态

### 7.1 `SuperLeader` 的 role adapter

`SuperLeader` 的 adapter 应该与 `Leader` 同构，只是 scope 更高：

- 任务面：`leader_lane` / `objective` scope 的待办节点与调度项
- 消息面：各 lane 的 summary mailbox、control mailbox
- 证据面：所有 `LeaderLaneBlackboard` digest、objective-level reducer summary
- 下级派发：`Leader`

### 7.2 `SuperLeader` 的 claim 规则

这一步也已经进入真实主线：

1. 先根据 dependency gate 计算 ready lanes
2. 在 budget 内启动新的 leader lanes
3. 等待 active leaders 至少一个完成
4. 重新计算 ready lanes，决定继续下一 cycle、等待依赖，还是 quiesce finalize

2026-04-09 的更深一层进展：

- ready-lane 判定不再只看本地 `pending/active` 账本；superleader 会先读取 lane delivery metadata 里的 digest/mailbox follow-up 信号，再和 host-owned leader session projection 一起决定 lane 是否已经处于 resident wait/running 态
- stale `PENDING` lane snapshot 会让位给 host-owned `WAITING_FOR_MAILBOX` leader session，因此 resident lane 不会被错误 relaunch
- 即使 objective shared subscription 暂时没有 digests，只要 lane delivery metadata 仍显示 `pending_shared_digest_count`，cycle 也会继续进入 `WAITING_FOR_MAILBOX`

直接实现位置：

- ready-lane cycle：[superleader.py#L382](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/superleader.py#L382)
- objective finalize + `coordinator_session`：[superleader.py#L509](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/superleader.py#L509)
- 新回归测试：[test_superleader_runtime.py#L359](/Volumes/disk1/Document/code/Agent-Orchestra/tests/test_superleader_runtime.py#L359)

判断：

- `SuperLeader` 已不再只是函数式 while-loop scheduler
- 但它当前仍主要把这层更强的 resident live view 用在 launch guard、mailbox wait、finalize export；再往前一层的 replan / rebalance / directive emission 仍未完成
- 它已经成为“共享 resident shell 驱动的 leader-of-leaders”

### 7.3 `SuperLeader` 不应直接退化成全局聊天室调度器

需要坚持两条边界：

- `SuperLeader` 仍只给 `Leader` 发任务或 directive
- team 间协作仍优先走 lane blackboard / summary digest / controlled routing，而不是让所有 lane 全量互相看原始消息

### 7.4 任务只是第一引擎推力，不是 team 的主执行模式

如果目标是贴近 Claude team agent，那么 `task` 在 Agent Orchestra 里应该承担的角色需要重新表述：

- `task` 的作用是点火，把 team 拉进一个有方向、有约束、有验收的运行态
- 真正持续推动问题解决的，不应是“leader 再跑一轮 turn”，而应是 team 内长期在线的 mailbox、task list、blackboard 与自主 claim
- `Leader` 的职责也不应主要理解为派工器，而应理解为持续收敛中心：收消息、看证据、调资源、在必要时才发起新的高价值 prompt turn

当前代码已经部分具备这个方向的基础，但主语义还没有完全切过去：

- `LeaderLoopSupervisor.run(...)` 每个 cycle 仍会先组装 `compile_leader_turn_assignment(...)`，然后执行 leader assignment，再 ingest 输出并决定 teammate 派发：[leader_loop.py#L1035](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py#L1035), [leader_loop.py#L1121](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py#L1121)
- `ResidentTeammateRuntime` 虽然已经 resident 化，但它的 work source 仍来自 leader 注入的 assignment 队列与 refill callback，而不是独立 mailbox/task-surface subscriber：[leader_loop.py#L799](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py#L799), [teammate_runtime.py#L64](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/teammate_runtime.py#L64), [teammate_runtime.py#L108](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/teammate_runtime.py#L108)
- `SuperLeaderRuntime` 现在已经像 resident 的 lane scheduler，但它依旧在“lane 可启动时拉起 leader loop，lane 结束后回收结果”的框架里运行：[superleader.py#L382](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/superleader.py#L382)

判断：

- 这不意味着当前方向错了；相反，这说明当前骨架已经足够支撑下一次语义切换
- 下一步真正要升级的不是“派发得更快”，而是“让 task 退回 activation surface，让在线协作升成 primary execution surface”

## 8. 复用路线：哪些代码可直接复用

### 8.1 可以直接复用的现有模块

- `LeaderLoopSupervisor` 中的 mailbox consume、delivery-state persistence、task refill 逻辑：[leader_loop.py#L742](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py#L742)
- `SuperLeaderRuntime` 中的 dependency-gated ready-lane scheduling 骨架：[superleader.py#L336](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/superleader.py#L336)
- `MailboxBridge` / cursor / digest 机制：[protocol_bridge.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/protocol_bridge.py)
- `WorkerSupervisor` 的 lifecycle / resume / fallback / escalate 决策层：[worker_supervisor.py#L560](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/worker_supervisor.py#L560)
- scoped task list 与 blackboard/reducer 结构

### 8.2 不应直接复用的部分

不应把下面这些当前实现直接等价成 resident runtime：

- `codex_cli` one-shot worker transport
- “一个 turn 一个 leader assignment”的主循环结构
- `keep_session_idle=True` 与 resident runtime 完全等价的假设

## 9. 推荐分阶段路线图

### 9.1 Phase 1：抽 `ResidentCoordinatorKernel`

目标：

- 把 `Leader` 和 `SuperLeader` 的常驻循环骨架抽成同一套内核

最小交付：

- 统一状态机
- 统一 poll/claim/drain loop
- role adapter 接口
- resident session record

### 9.2 Phase 2：先让 `Leader` 切到 resident kernel

目标：

- 把现有 `LeaderLoopSupervisor` 改造成 resident kernel 的 `LeaderScopeAdapter`

原因：

- `Leader` 距当前代码最近
- `team-parallel-execution` 的剩余 gap 也正集中在这里

### 9.3 Phase 3：让 `SuperLeader` 切到同一 kernel

目标：

- 让 `SuperLeaderRuntime` 不再只是函数式 lane scheduler
- 改成同一个 resident coordinator kernel 的 `SuperLeaderScopeAdapter`

### 9.4 Phase 4：先用真正支持 reactivation 的 transport 跑通 resident 模式

建议先用：

- `in_process`
- 或新的 resident host transport

而不是强行用当前 one-shot `codex_cli` 去假装 resident teammate / leader。

### 9.5 这里最稳的抽象不是一层，而是两层

你刚才提出“是不是可以抽象出专门一个或两个层，负责多 session 操作以及和 tmux 等接口统一”这个方向是对的，而且建议明确拆成两层，而不是只做一个笼统的 backend manager。

推荐拆法：

1. `ResidentSessionHost`
2. `TransportAdapter`

两者的职责必须分开。

`ResidentSessionHost` 负责系统级、跨 backend 统一的多 session 语义：

- 管理多个长期 agent session
- 负责 session registry、identity、lease、ownership、takeover、cursor
- 负责 attach / detach / reclaim / reconnect
- 负责把 mailbox / task list / blackboard 的刺激变成对某个 session 的调度动作
- 负责决定某个 agent 当前是 `active`、`idle`、`waiting_for_mailbox`、`quiescent` 还是 `shutdown_requested`

它处理的是“谁活着、谁归谁管、现在该唤醒谁、是否还能接回”的问题。

`TransportAdapter` 则只负责具体宿主或执行介质：

- `tmux`
- `codex_cli`
- `subprocess`
- `in_process`

它应统一暴露的能力大致是：

- `launch`
- `resume`
- `reactivate`
- `reattach`
- `interrupt`
- `read_progress`
- `read_final`
- `terminate`

它处理的是“这个 session 在具体介质上怎么被拉起、怎么继续、怎么接回、怎么关闭”的问题。

判断：

- `tmux` 不应该直接知道 team mailbox、task claim、blackboard digest 这些上层协作语义
- `ResidentSessionHost` 也不应该直接知道 tmux pane 命令、pid 细节、CLI 参数拼接这些宿主细节
- 这两层一旦混在一起，后面做 `codex_cli resident`、`tmux-hosted session`、`ACTIVE reattach` 时就会重新耦合回当前 one-shot worker 模型

对你当前目标来说，最关键的不是“先把 codex_cli 变 resident”，而是先让 `ResidentSessionHost` 成立：

- 上层的 `SuperLeader / Leader / Teammate` 都通过 host 被视为长期 session
- host 再根据 backend capability 决定该 session 目前落在 `in_process`、`tmux` 还是 one-shot `codex_cli` 上
- 当某个 transport 不支持真 resident 时，host 仍可以维持 agent 的长期 identity，只是把具体执行动作降级成按需 one-shot turn

这样系统就不会被单一 transport 的能力上限绑死。

### 9.6 Phase 5：再决定 `codex_cli` 如何接入 resident 形态

这一步有两个合理方向：

1. 保持 `codex_cli` 继续只做 one-shot code-edit worker，由 resident coordinator 在上层反复调它
2. 单独做 `resident codex bridge` / `tmux-hosted codex session`，让 `codex_cli` 真正获得常驻 transport 语义

判断：

- 这一步不应阻塞 resident coordination kernel 的设计
- 否则团队 runtime 会被当前 transport 能力上限反向绑死

## 10. 对你当前目标的直接建议

如果目标是“让 `SuperLeader`、`Leader`、以及后续 `Teammate` 都达到 Claude 式长期协作状态”，当前顺序已经变成：

1. `Leader` 与 `SuperLeader` 的 shared resident coordination shell 已完成
2. 下一步优先把统一 `Agent` 抽象、`RolePolicy / SkillSet / PromptProfile` 分层固化下来
3. 再把 `Teammate` 做成真正常驻执行体
4. 把 `mailbox + task list + blackboard` 推成 team 的 primary execution surface，让 task 只保留 activation/contract 角色
5. 再把 `Leader` 推成持续收敛中心，而不是主要靠 turn 派工的执行中心
6. 最后才讨论 durable session / reconnect / resident bridge

一句话判断：

- 现在最该继续复用的是“统一 agent 核心 + 角色策略分层 + 持续协作 surface”
- 而不是回头重复讨论 leader/superleader 的 resident shell 本身，或者继续把任务派发当成主求解模式

## 11. 相关文档

- [team-parallel-execution-gap.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/team-parallel-execution-gap.md)
- [parallel-team-coordination.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/parallel-team-coordination.md)
- [agent-orchestra-framework.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/agent-orchestra-framework.md)
- [agent-abstraction-skill-and-prompt-system.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/agent-abstraction-skill-and-prompt-system.md)
- [architecture.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/architecture.md)
- [agent-team.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/claude-code-main/agent-team.md)
