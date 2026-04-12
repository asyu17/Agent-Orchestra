# agent_orchestra 在线协作运行时迁移

## 1. 一句话结论

如果 `agent_orchestra` 要真正达到 Claude 式“长期在线协作”的目标，那么下一条主线不应再理解成“让 `Leader` 更会派工”，而应明确迁移成：`task` 只负责 activation/contract，真正持续推进问题解决的是长期在线的 `Agent`，它们通过 `mailbox + task list + blackboard` 持续协作；对应实现上，系统需要补齐 `online-collaboration-runtime` 这一条新主线，把当前的 `Leader turn -> runtime 派发 assignment` 模式升级成 `Agent Coordination Layer -> ResidentSessionHost -> TransportAdapter` 三层体系。并且从本轮设计决议开始，这条主线还需要额外吸收 3 个明确约束：去掉 `max_concurrency` 作为正式 team budget、把 leader 输出升级成显式 `sequential / parallel slices`、以及把 team runtime 的 primary execution surface 固定为 resident collaboration。

## 2. 范围与资料来源

- 当前知识文档：
  - [resident-hierarchical-runtime.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/resident-hierarchical-runtime.md)
  - [team-parallel-execution-gap.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/team-parallel-execution-gap.md)
  - [resident-team-collaboration-and-slice-planning.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/resident-team-collaboration-and-slice-planning.md)
  - [agent-abstraction-skill-and-prompt-system.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/agent-abstraction-skill-and-prompt-system.md)
  - [parallel-team-coordination.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/parallel-team-coordination.md)
  - [active-reattach-and-protocol-bus.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/active-reattach-and-protocol-bus.md)
  - [implementation-status.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/implementation-status.md)
- 当前代码入口：
  - [leader_loop.py#L1015](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py#L1015)
  - [leader_loop.py#L1035](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py#L1035)
  - [leader_loop.py#L1226](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py#L1226)
  - [teammate_runtime.py#L52](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/teammate_runtime.py#L52)
  - [teammate_runtime.py#L87](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/teammate_runtime.py#L87)
  - [resident_kernel.py#L12](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/resident_kernel.py#L12)
  - [superleader.py#L382](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/superleader.py#L382)
  - [group_runtime.py#L755](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/group_runtime.py#L755)
  - [group_runtime.py#L782](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/group_runtime.py#L782)
  - [protocol_bridge.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/protocol_bridge.py)
  - [execution.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/contracts/execution.py)
  - [team.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/contracts/team.py)
- Claude 对照来源：
  - [agent-team.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/claude-code-main/agent-team.md)
  - [inProcessRunner.ts#L624](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/swarm/inProcessRunner.ts#L624)
  - [inProcessRunner.ts#L853](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/swarm/inProcessRunner.ts#L853)
  - [useInboxPoller.ts#L843](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/hooks/useInboxPoller.ts#L843)

## 3. 目标状态

### 3.1 执行主语义切换

目标状态不是“leader 输出更多 teammate task”，而是：

- `task` 只负责 activation：
  - 激活某个 agent 或 team 进入工作态
  - 提供 scope、contract、budget、acceptance
- `Leader` 输出必须显式区分 `sequential slices` 与 `parallel slices`：
  - 不能只给扁平 `teammate_tasks`
  - 必须带上依赖关系或并行标签
- `mailbox + task list + blackboard` 负责持续协作：
  - mailbox 传递 directed/control/result 信号
  - task list 承载可 claim 的操作面
  - blackboard 承载长期证据、proposal、blocker、summary
- `Leader` 和 `SuperLeader` 负责持续收敛：
  - 持续收消息
  - 观察 team/lane 运行态
  - 只在需要高价值判断时触发 prompt turn
- `Teammate` 负责长期在线执行：
  - 空闲时继续 poll mailbox
  - 空闲时继续 claim team work
  - 结果直接沉淀到 mailbox/task/blackboard
- team budget 只保留 `max_teammates`：
  - 不再把 `max_concurrency` 作为 planning/runtime 主语义
  - 默认目标上限是每个 team 最多 `20` 个 teammate

### 3.2 三层运行模型

建议把最终运行时固定为三层：

1. `Agent Coordination Layer`
   - 负责认知、收敛、决策、claim、prompt turn 触发
2. `ResidentSessionHost`
   - 负责多 session 生命周期、ownership、lease、wake/attach/detach/reconnect
3. `TransportAdapter`
   - 负责 `in_process / subprocess / tmux / codex_cli` 的具体宿主能力

判断：

- `resident` 首先是 session/coordination 语义，不是 `codex_cli` 自身的语义
- transport 可以不支持真正的 resident，但上层 agent identity 仍应长期存在
- 这样系统不会被某个 backend 的能力上限反向绑死

## 4. 当前基线与真实错位

### 4.1 当前已经有的基础

当前主线已经具备下面这些基础：

- `ResidentCoordinatorKernel` 已经统一 leader / superleader 的 cycle、phase、mailbox poll、claim 计数：[resident_kernel.py#L12](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/resident_kernel.py#L12)
- `LeaderLoopSupervisor.run(...)` 已经 resident 化，并返回 `ResidentCoordinatorSession`；ready lane 也已通过 `LeaderLoopSupervisor.ensure_or_step_session(...)` + `ResidentSessionHost` 进入 host-owned `leader_session_host` launch boundary：[leader_loop.py#L1015](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py#L1015), [test_leader_loop.py#L1806](/Volumes/disk1/Document/code/Agent-Orchestra/tests/test_leader_loop.py#L1806)
- `LeaderLoopSupervisor` 已经把 promptless convergence 扩到 lane-local continuous convergence：除了 teammate mailbox 结果之外，也能在 open team task 仍存在时继续 promptlessly drain：[leader_loop.py#L1082](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py#L1082), [leader_coordinator.py#L9](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_coordinator.py#L9)
- `ResidentTeammateRuntime` 已经不再只接受 leader 注入的 refill tuple，而是显式拥有 `ResidentTeammateAcquireResult` / `acquire_assignments(...)` 这套 acquisition/evidence 契约：runtime 自己决定何时继续 refill/acquire，并把 directed/autonomous claim 与 mailbox progress 聚合成 runtime-native teammate evidence；但它现在更接近 legacy bounded engine / inner execution reference，而不是 team live path：[teammate_runtime.py#L38](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/teammate_runtime.py#L38), [teammate_runtime.py#L85](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/teammate_runtime.py#L85)
- `leader_loop` 不再直接运行 `TeammateOnlineLoop` 来决定 teammate refill 节奏；当前 leader 只提供较薄的 activation/convergence shell，而 team 内 live path 已经切到 `TeammateWorkSurface.run(...) -> per-slot TeammateOnlineLoop`。最小 directed mailbox teammate work 仍然保留：promptless continuation 可以在没有新 leader prompt turn 的情况下继续先消费定向 mailbox，再回退到 autonomous claim：[leader_loop.py#L1106](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py#L1106), [test_leader_loop.py#L1960](/Volumes/disk1/Document/code/Agent-Orchestra/tests/test_leader_loop.py#L1960), [test_leader_loop.py#L2038](/Volumes/disk1/Document/code/Agent-Orchestra/tests/test_leader_loop.py#L2038)
- 新增 [teammate_work_surface.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/teammate_work_surface.py) 后，teammate-specific 的 slot identity、direct mailbox cursor、初始 assignment reserve、slot session truth，以及主线 `execute_assignment(...)` 里的 permission gate、task status、blackboard result、`task.result` mailbox publish，都已经从 `leader_loop` 里抽走，因此 leader 的 ownership 已经收缩到 activation/convergence 所需的薄适配层
- `GroupRuntime.claim_task(...) / claim_next_task(...)` 已经提供正式原子 claim surface：[group_runtime.py#L755](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/group_runtime.py#L755), [group_runtime.py#L782](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/group_runtime.py#L782)
- `ResidentSessionHost` 与 `TransportAdapter` 已经在 supervisor 主线形成 thin 分层：host 提供 `register_session / bind_transport / update_session / snapshot_session`，supervisor 通过 adapter 统一做 binding / locator / handle snapshot / session 恢复：[session_host.py#L12](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/session_host.py#L12), [transport_adapter.py#L17](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/transport_adapter.py#L17), [worker_supervisor.py#L138](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/worker_supervisor.py#L138)
- `ProtocolBus / RedisProtocolBus`、lease reclaim、backend `reattach(...)` 已进入主线：[active-reattach-and-protocol-bus.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/active-reattach-and-protocol-bus.md)
- `SuperLeaderRuntime` 已经把 lane 级 `allow_promptless_convergence / max_mailbox_followup_turns` 透传进 `LeaderLoopConfig`，因此 superleader 拉起的 lane 也能吃到当前这一版 leader continuous convergence：[superleader.py#L50](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/superleader.py#L50), [superleader.py#L358](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/superleader.py#L358)
- 2026-04-09 起，`SuperLeaderRuntime` 的 resident cycle/finalize 又多了一层 runtime-native live read：它会先通过 `GroupRuntime.read_resident_lane_live_views(...)` 读取 lane delivery snapshot、lane-level `message_runtime` / mailbox follow-up metadata、以及 host-owned leader `CoordinatorSessionState`，再把 objective shared subscription digests join 进同一份 resident live view。这样 superleader 决策不再主要依赖本地 `pending/active` bookkeeping，而是优先消费 runtime/host 已持久化的 subordinate truth；host 已处于 `RUNNING/WAITING` 的 lane 也不会被错误当成“ready to relaunch”。同一轮又把这层 live view 再向前推了一小步：stale `PENDING` lane snapshot 会让位给 host-owned `WAITING_FOR_MAILBOX` projection，而 lane delivery metadata 里仍保留 `pending_shared_digest_count` 时，即使 objective subscription 暂时为空，superleader 也会继续进入 `WAITING_FOR_MAILBOX`。
- 2026-04-10 又补上了 export/reporting hardening：`resident_live_view` 现在会把每条 lane 的 `truth_source` 和 `primary_*` lane/session 集合显式导出，区分“runtime-native delivery/session truth”与“fallback scheduler state”。因此 self-hosting `superleader_runtime_status` 会优先读取 `resident_live_view` 的 primary fields，而不是把 top-level `coordination / message_runtime` 当作唯一真相来源；rendered instruction packet 也会把这层 resident truth 直接展示出来。

### 4.2 当前真正的问题

当前问题不在“能不能并行”，而在“谁在主导执行”：

- `Leader` 每个 cycle 仍以 `compile_leader_turn_assignment(...) -> run leader assignment -> ingest output -> 派发 teammate` 为主链：[leader_loop.py#L1035](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py#L1035)
- formal budget/runtime protocol 已经不再保留 `max_concurrency`；team 内 resident slot 并行上限现在由 `max_teammates` 和 live slot activation 决定，而不是旧的 team-level concurrency budget：[bootstrap.py#L375](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/self_hosting/bootstrap.py#L375), [teammate_work_surface.py#L226](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/teammate_work_surface.py#L226)
- `leader_output_protocol` 已经正式以 `sequential_slices / parallel_slices` 为主协议，并把 `slice_id / depends_on / parallel_group` 落到 assignment metadata 和 execution report；当前残留缺口不再是“还没切协议”，而是“runtime 还没更深消费这些 slice 元数据”：[leader_output_protocol.py#L162](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_output_protocol.py#L162), [leader_output_protocol.py#L326](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_output_protocol.py#L326)
- runtime-native `teammate_execution_evidence` 与 teammate-owned `final_report.verification_results` 已经进入当前 resident collaboration 的 primary evidence surface；`leader_consumed_mailbox` 只剩回退式诊断价值。但 teammate inbox/cursor 的最终 truth source、长期 mailbox/task 主循环，以及完全 host-owned 的 standalone teammate session 仍没有彻底脱离 leader-led activation 入口，所以它还不是最终形态的长期独立 teammate agent loop：[bootstrap.py#L664](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/self_hosting/bootstrap.py#L664), [bootstrap.py#L702](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/self_hosting/bootstrap.py#L702), [test_worker_supervisor_protocol.py#L826](/Volumes/disk1/Document/code/Agent-Orchestra/tests/test_worker_supervisor_protocol.py#L826), [test_execution_guard.py#L284](/Volumes/disk1/Document/code/Agent-Orchestra/tests/test_execution_guard.py#L284)
- `SuperLeaderRuntime` 已是 resident scheduler，并已透传 lane 级 promptless convergence 配置；2026-04-09 起它也已经开始从 runtime/host 的 resident live view 读 subordinate lane truth，而不是只看本地 scheduler map。更具体地说，它现在已经会直接消费 lane digest/mailbox metadata、host-owned leader session projection、以及 objective live metadata，并把这些 live inputs 导出成 objective `resident_live_view` 与 self-hosting `superleader_runtime_status`。2026-04-10 又把这层 contract 从“原材料导出”推进到“primary export surface”：`resident_live_view` 会显式告诉 bootstrap 哪些 lane status/session 是 runtime-native、哪些只是 fallback bookkeeping，因此 self-hosting packet/report 已不再把 fallback scheduler heuristics 当 primary signal。但它仍没有进入更完整的 digest/mailbox 驱动上层 continuous convergence：当前 lane/objective digests 主要影响 launch-guard、wait/finalize metadata，尚未成为 replan / rebalance / directive emission 的主驱动：[superleader.py#L382](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/superleader.py#L382), [superleader.py#L572](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/superleader.py#L572)

判断：

- 当前系统已经有“resident 外壳”
- 但还没有真正进入“长期在线协作作为 primary execution surface”的主语义

## 5. Gap 列表

### 5.1 P0: `online-collaboration-runtime`

这是当前最核心的新主线 gap，建议作为顶层 gap_id 使用。

它包含 6 个必须完成的子 gap。

### 5.2 P0-1: `agent-contract-upgrade`

缺口：

- 现在 [team.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/contracts/team.py) 里的 `AgentProfile` 仍过薄
- [execution.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/contracts/execution.py) 里也还没有统一 `AgentSession / RolePolicy / ClaimPolicy / PromptTriggerPolicy / AgentAction`

需要补：

- 新增统一 `Agent` 契约层
- 把 `Leader / SuperLeader / Teammate` 的长期语义统一到同一套 session/action/policy 模型

### 5.3 P0-2: `resident-teammate-online-loop`

缺口：

- 当前 live path 虽已切到 `TeammateWorkSurface.run(...) -> per-slot TeammateOnlineLoop`，但它仍通过 leader activation 入口被点火，还不是脱离 leader process / host rebuild 的自主长期在线 teammate

需要补：

- teammate 自己维护 mailbox cursor
- teammate 自己观察 team task surface
- teammate 自己执行 directed message -> autonomous claim -> result publish 的循环

### 5.4 P0-3: `leader-continuous-convergence-loop`

缺口：

- 当前 leader 仍偏向“生成 assignment 的 turn executor”

需要补：

- 把 leader 拆成：
  - 常驻协调 loop
  - 按需触发的 prompt turn
- leader 平时主要消费 mailbox、观察 blackboard、调度 team，而不是默认每轮都做完整 prompt

### 5.5 P0-4: `task-to-activation-surface`

缺口：

- 现在 task 仍被隐式当作主执行载体

需要补：

- 明确 task 仅承担 activation/contract 角色
- 让 mailbox/task-list/blackboard 成为长期 primary execution surface
- 让 slice graph 成为 leader/team 的局部规划面，正式表达顺序与并行关系
- 补清楚 task、message、evidence 三者的权威分工

### 5.6 P0-5: `resident-session-host`

缺口：

- 当前长期 session 语义散落在 supervisor、leader loop、teammate runtime 和 backend handle 中

需要补：

- 独立的 `ResidentSessionHost`
- 统一管理 session registry、lease、wake、attach、detach、reclaim、reconnect
- 统一承接 `ACTIVE`/`IDLE`/`QUIESCENT` 等 session 主语义

### 5.7 P0-6: `transport-host-separation`

缺口：

- 现在 transport capability、session lifecycle、agent 协作语义还没有彻底分层

需要补：

- 固定 `ResidentSessionHost` 与 `TransportAdapter` 的边界
- 避免 tmux/codex_cli 细节渗透进上层 agent coordination

### 5.8 P1: `superleader-online-convergence`

缺口：

- superleader 仍主要是 lane scheduler，而不是长期在线的上层收敛中心

需要补：

- superleader 常驻 mailbox/lane-digest loop
- 只在需要重规划、lane 协调、team 数量调整时触发 prompt turn

### 5.9 P1: `tmux-codex-resident-bridge`

缺口：

- `codex_cli` 当前仍主要是 one-shot transport
- `tmux` 虽支持 session，但还不是标准 resident agent host

需要补：

- `tmux-hosted codex session`
- 或 `resident codex bridge`
- 让真 resident transport 成为可选正式路径，而不是先验前提

### 5.10 P2: `online-collaboration-evidence-gates`

缺口：

- 当前 self-hosting evidence gate 主要验证 resident claim / protocol / reconnect

需要补：

- 验证 leader 是否真的少依赖 turn 边界
- 验证 teammate 是否真的长期自主 claim
- 验证 mailbox/task-list/blackboard 是否真的接管 primary execution

## 6. 实现方案

### 6.1 Phase 1：补统一 Agent 契约

建议新增：

- `src/agent_orchestra/contracts/agent.py`

建议调整：

- [team.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/contracts/team.py)
- [execution.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/contracts/execution.py)

核心内容：

- `AgentProfile`
- `AgentSession`
- `AgentAction`
- `RolePolicy`
- `ClaimPolicy`
- `PromptTriggerPolicy`
- `SessionBinding`

验收标准：

- `Leader / SuperLeader / Teammate` 可统一描述成同一类长期 agent
- prompt turn 被降级为 `AgentAction` 之一，而不是 runtime 主语义

### 6.2 Phase 2：把 teammate 做成真正常驻 agent

建议新增：

- `src/agent_orchestra/runtime/agent_runtime.py`
- `src/agent_orchestra/runtime/teammate_agent.py`

建议调整：

- [teammate_runtime.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/teammate_runtime.py)
- [group_runtime.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/group_runtime.py)
- [protocol_bridge.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/protocol_bridge.py)

核心内容：

- teammate 常驻 mailbox poll
- teammate 自主 `claim_next_task(...)`
- directed mailbox 与 autonomous claim 并列成为工作入口
- 执行结果直接写回 mailbox/task/blackboard

验收标准：

- teammate 被拉起一次后，可连续完成多个 team task
- 不需要 leader 每轮重新为其显式生成 assignment

### 6.3 Phase 3：把 leader 拆成“常驻协调 + 按需 prompt”

建议新增：

- `src/agent_orchestra/runtime/leader_coordinator.py`

建议调整：

- [leader_loop.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py)

核心内容：

- leader 默认持续收 mailbox / blackboard / task digest
- 只有在 blocker、冲突、重分解、总结收敛时触发 prompt turn
- `_run_teammates(...)` 从 leader loop 的内嵌执行逻辑，迁到长期 session host 驱动

验收标准：

- leader 空闲时长期在线，不需要频繁重新编译完整 assignment
- team 协作能在 leader 不跑新 turn 的情况下持续推进

### 6.4 Phase 4：新增 `ResidentSessionHost`

建议新增：

- `src/agent_orchestra/runtime/session_host.py`
- `src/agent_orchestra/runtime/session_registry.py`

建议调整：

- [worker_supervisor.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/worker_supervisor.py)
- [execution.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/contracts/execution.py)

核心内容：

- 统一 session registry
- lease / ownership / wake / attach / detach / reclaim / reconnect
- 持久化 `AgentSession` 与 `SessionBinding`
- 把 supervisor 从“生命周期事实拥有者”降为“具体执行监督者”

验收标准：

- agent identity 在 transport 切换、supervisor 重启、短期 idle 后仍保持稳定
- session 语义不再散落在多个 runtime 局部状态里

### 6.5 首个安全落地波次：先做 `agent-contract-upgrade` + `resident-session-host`

判断：

- 第一波不应该直接改 `leader_loop.py`、`superleader.py` 或 teammate online loop
- 更安全的顺序是先把“长期在线 agent 的契约”和“session 主事实拥有者”固定下来，再让 leader / teammate / superleader 迁到这套语义

建议新增：

- `src/agent_orchestra/contracts/agent.py`
- `src/agent_orchestra/runtime/session_host.py`
- `tests/test_agent_contracts.py`
- `tests/test_session_host.py`

建议调整：

- [team.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/contracts/team.py)
- [execution.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/contracts/execution.py)
- [worker_supervisor.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/worker_supervisor.py)
- [protocol_bridge.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/protocol_bridge.py)
- [runtime/backends/base.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/backends/base.py)
- [tests/test_execution.py](/Volumes/disk1/Document/code/Agent-Orchestra/tests/test_execution.py)
- [tests/test_worker_supervisor_protocol.py](/Volumes/disk1/Document/code/Agent-Orchestra/tests/test_worker_supervisor_protocol.py)
- [tests/test_protocol_bridge.py](/Volumes/disk1/Document/code/Agent-Orchestra/tests/test_protocol_bridge.py)

建议最小契约拆分：

- `contracts/agent.py`
  - `AgentProfile`
  - `RolePolicy`
  - `ClaimPolicy`
  - `PromptTriggerPolicy`
  - `AgentSession`
  - `SessionBinding`
- `runtime/session_host.py`
  - `ResidentSessionHost`
  - `DefaultResidentSessionHost`

建议文件职责：

- `team.py`
  - 保留 `Group` / `Team`
  - 把 `AgentProfile` 改成从 `contracts/agent.py` re-export，避免第一波就打断现有 import surface
- `execution.py`
  - 继续承载 `WorkerAssignment`、`WorkerHandle`、`WorkerTransportLocator`、`WorkerBackendCapabilities`
  - 保留 `WorkerSession` 作为 transport / supervisor 侧兼容快照
  - 新增 `AgentSession` 与 `WorkerSession` 之间的桥接字段或转换函数，避免直接让 leader / teammate 依赖 `WorkerSession`
- `session_host.py`
  - 统一提供 `load_session` / `bind_transport` / `mark_active` / `mark_idle` / `mark_quiescent` / `detach` / `reclaim` / `remember_cursor`
  - 让 host 成为 session registry、lease ownership、binding truth 的唯一入口
- `worker_supervisor.py`
  - 继续负责 launch / resume / wait / complete / fail
  - 不再自己持有长期 session truth；改为通过 host 读写 session / binding
- `protocol_bridge.py`
  - 补 session stream 的标准 payload 组装函数
  - 把 host 的 attach / detach / reclaim / reconnect 变化标准化成 bus event，而不是继续散落在 supervisor metadata
- `runtime/backends/base.py`
  - 只补 host 需要的 capability 读取辅助面
  - 当前真实代码已经落下 thin `runtime/transport_adapter.py`；剩余约束是不让它过早扩成和 `LaunchBackend` 重叠的大抽象

第一波验收标准：

- `AgentProfile / AgentSession / SessionBinding` 已有稳定落点，`team.py` 与 `execution.py` 不再各自演化 session 语义
- `DefaultWorkerSupervisor` 可以通过 `ResidentSessionHost` 持久化 / 取回 active or idle session
- session lifecycle 的事件能通过 `protocol_bridge.py` 产出统一 session bus payload
- leader / teammate 代码还没迁移也没关系，但它们后续不需要再改底层 session 数据模型

### 6.6 Phase 5：固定 `TransportAdapter` 边界

建议新增：

- `src/agent_orchestra/runtime/transports/adapter.py`

建议调整：

- [runtime/backends/base.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/backends/base.py)
- [runtime/backends/in_process.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/backends/in_process.py)
- [runtime/backends/subprocess_backend.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/backends/subprocess_backend.py)
- [runtime/backends/tmux_backend.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/backends/tmux_backend.py)
- [runtime/backends/codex_cli_backend.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/backends/codex_cli_backend.py)

核心内容：

- 固定统一能力：
  - `launch`
  - `resume`
  - `reactivate`
  - `reattach`
  - `interrupt`
  - `read_progress`
  - `read_final`
  - `terminate`
- 上层一律通过 host 调 transport，不直接碰 backend 细节

验收标准：

- tmux、subprocess、codex_cli、in_process 的差异仅体现在 adapter 实现，不再污染上层协作语义

### 6.7 Phase 6：把 superleader 迁到同一模型

建议新增：

- `src/agent_orchestra/runtime/superleader_coordinator.py`

建议调整：

- [superleader.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/superleader.py)

核心内容：

- superleader 常驻 lane mailbox / lane blackboard digest
- team/lane 数量与职责变化，交给上层 prompt turn 决定
- ready-lane scheduler 继续保留，但从“唯一主语义”降为“上层收敛 loop 的一个动作”

验收标准：

- superleader 不再只是在 lane 就绪时临时拉起 leader loop
- 多 team / 多 lane 协调成为长期在线的上层运行态

## 7. 验证与证据 gate

这条主线需要新增正式 evidence gate，至少包括：

1. `teammate-autonomous-online-loop`
   - 验证 teammate 在没有 leader 新 turn 的情况下持续 claim 新任务
2. `leader-continuous-convergence`
   - 验证 leader 在不跑新 prompt turn 时仍持续消费 mailbox 并推进 team
3. `task-as-activation-surface`
   - 验证 task 创建后只承担 activation/contract，后续主推进来自 mailbox/task-list/blackboard
4. `resident-session-host`
   - 验证 session identity 跨 supervisor / transport 生命周期稳定存在
5. `transport-host-separation`
   - 验证 transport 细节不再渗透进 leader/teammate 协作逻辑

## 8. 建议投喂包

如果后续把这条迁移交给 Agent Orchestra 自己做，建议优先投喂：

1. `online-collaboration-runtime`
2. `resident-session-host`
3. `leader-continuous-convergence`
4. `resident-teammate-online-loop`
5. `transport-host-separation`

建议同时阅读：

- [resident-hierarchical-runtime.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/resident-hierarchical-runtime.md)
- [team-parallel-execution-gap.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/team-parallel-execution-gap.md)
- [agent-abstraction-skill-and-prompt-system.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/agent-abstraction-skill-and-prompt-system.md)
- [active-reattach-and-protocol-bus.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/active-reattach-and-protocol-bus.md)

## 9. 2026-04-06 切片审计：host-owned activation boundary + resident evidence semantics

### 9.1 一句话判断

基于当前源码与定向回归，这条主线已经不再是“还没有 resident collaboration”，而是“resident collaboration 的 first-cut 已经进入主线，但 single-owner truth 与 host-owned evidence gate 还没完全收口”：

- `Leader` 已经不必把所有进展都重新收束成 fresh leader prompt turn；promptless convergence 现在可以在 activation 之后继续推进 teammate work
- `SuperLeader` 也不再直接把 ready lane 视作裸 `leader_loop.run(...)` task；ready lane 先进入 `leader_session_host` launch boundary，再回收 bounded lane result
- 真正还没完成的，已经收缩到：leader 仍显式承担 `_run_teammates(...)` activation shell、store-level coordination transaction 还没收口、`WorkerSession / AgentSession / ResidentCoordinatorSession` 还没有唯一 owner、以及 self-hosting 仍残留 old follow-up heuristic

### 9.2 `Leader` 当前已经完成什么，真正还差什么

当前已进入主线的事实：

- `LeaderLoopSupervisor` 已经支持 promptless continuous convergence，而不是“每有新进展就必须再开一个 leader prompt turn”
- 定向回归已经证明：
  - ready lane 可以先持久化 host-owned leader coordinator session，再运行 lane：[test_leader_loop.py#L1806](/Volumes/disk1/Document/code/Agent-Orchestra/tests/test_leader_loop.py#L1806)
  - teammate activation 后，可以在没有 fresh leader prompt turn 的前提下继续推进：[test_leader_loop.py#L1960](/Volumes/disk1/Document/code/Agent-Orchestra/tests/test_leader_loop.py#L1960)
  - directed teammate mailbox work 可以在没有新 leader prompt turn 的条件下先于 autonomous claim 被消费：[test_leader_loop.py#L2038](/Volumes/disk1/Document/code/Agent-Orchestra/tests/test_leader_loop.py#L2038)
- `leader_loop` 的 mailbox helper 现在只处理 leader inbox，teammate recipient cursor/session truth 已经退出 leader 文件主线

当前真正还没切完的点：

- high-value judgement 的主路径仍然是 `compile_leader_turn_assignment(...) -> run leader assignment -> ingest output`
- leader 仍显式调用 `_run_teammates(...)`，所以它还没有彻底降成 `ensure_or_step_teammate_session` 风格的 activation center
- teammate activation profile 虽已写入 slot session metadata，但 standalone teammate host / cross-host rebuild 还没完成

### 9.3 `SuperLeader` 当前已经完成什么，真正还差什么

当前已进入主线的事实：

- ready lane 已通过 `LeaderLoopSupervisor.ensure_or_step_session(...)` + `ResidentSessionHost` 进入 host-owned `leader_session_host` 边界，而不是直接退回完全裸的 subordinate task
- `SuperLeaderRuntime` 已能把 lane 级 promptless convergence 配置透传给 leader lane，并开始读取 shared digest / mailbox 相关状态

当前真正还没切完的点：

- superleader 仍主要以“拉起 bounded leader loop -> 回收 lane result”的方式工作，还没有变成 digest/mailbox 驱动的 leader-session runtime
- `leader_session_host` 当前更接近 host-owned launch boundary / projection，还不是可重入、可恢复、可跨 superleader cycle step 的真正 leader session runtime

### 9.4 coordination truth owner 的当前落点

当前 ownership 已经出现了清晰分层，不再适合继续写成“leader 还在持有大部分真相”：

- `TeammateWorkSurface + ResidentSessionHost`
  - 当前拥有 teammate recipient cursor、slot session、activation profile、mailbox consume/ACK 的 live truth
- `GroupRuntime.commit_directed_task_receipt(...) / commit_teammate_result(...)`
  - 当前拥有 runtime-owned coordination commit 的 first-cut 组合面，负责把 receipt/result、blackboard 与 delivery snapshot 收口成可复用 helper
- `DefaultWorkerSupervisor + ResidentSessionHost.read_worker_session_truth(...)`
  - 当前拥有 worker/agent reconnect truth 的 first-cut 投影

当前真正还没完成的 single-owner convergence 是：

- `claim / cursor / session / blackboard / outbox / delivery snapshot` 还不是 store-level 单事务提交
- `WorkerSession / AgentSession / ResidentCoordinatorSession` 还没有统一成一个最终权威 owner
- host 目前已是 session/cursor truth 的最强 owner，但还不是跨 runtime/store 的唯一 coordination truth owner

### 9.5 resident collaboration 的证据语义已经怎么变了

当前 mainline 里的 resident collaboration evidence 已经不是旧的 leader-owned heuristic：

- self-hosting 现在优先消费 runtime-native `teammate_execution_evidence`，并把它作为旧 `team-parallel-execution` / `leader-teammate-delegation-validation` gap 家族的 primary signal：[bootstrap.py#L664](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/self_hosting/bootstrap.py#L664)
- `leader_consumed_mailbox` 仍然保留，但已经退回诊断 metadata，而不是唯一关闭证明：[bootstrap.py#L690](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/self_hosting/bootstrap.py#L690)
- code-edit teammate assignment 的 authoritative verification evidence 已切到 `final_report.verification_results`；`requested_command -> command` fallback mapping 与 execution guard fallback 也已经进入主线：[test_worker_supervisor_protocol.py#L758](/Volumes/disk1/Document/code/Agent-Orchestra/tests/test_worker_supervisor_protocol.py#L758), [test_worker_supervisor_protocol.py#L826](/Volumes/disk1/Document/code/Agent-Orchestra/tests/test_worker_supervisor_protocol.py#L826), [test_execution_guard.py#L284](/Volumes/disk1/Document/code/Agent-Orchestra/tests/test_execution_guard.py#L284)

但 host-owned evidence rewrite 还没完全做完：

- `_delegation_validation_metadata(...)` 仍把 `leader_turn_count > 1` 作为 `validated` 条件之一：[bootstrap.py#L702](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/self_hosting/bootstrap.py#L702)
- 这意味着 bootstrap 已经“优先看 teammate-owned evidence”，但旧 delegation gap 仍在奖励 leader follow-up，而不是纯粹验证“leader 一次 activation 后 teammate host-owned online loop 继续推进”

### 9.6 更新后的 residual 顺序

按照当前主线事实，后续最值得继续推进的顺序应是：

1. `coordination-transaction`
2. `leader-activation-cutover`
3. `superleader-session-runtime`
4. `session-truth-convergence`
5. `self-hosting-evidence-rewrite`
6. `teammate-durable-resident-loop`

## 10. 相关文档

- [README.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/README.md)
- [implementation-status.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/implementation-status.md)
- [current-priority-gap-list.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/current-priority-gap-list.md)
- [remaining-work-backlog.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/remaining-work-backlog.md)
- [resident-hierarchical-runtime.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/resident-hierarchical-runtime.md)
- [team-parallel-execution-gap.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/team-parallel-execution-gap.md)
- [agent-abstraction-skill-and-prompt-system.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/agent-abstraction-skill-and-prompt-system.md)
