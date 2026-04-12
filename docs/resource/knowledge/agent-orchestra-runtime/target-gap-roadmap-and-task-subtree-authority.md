# agent_orchestra 目标差距路线图与 Task Subtree Authority

## 1. 一句话结论

截至 2026 年 4 月 10 日，`Agent Orchestra` 距离你要的目标状态，已经不再主要缺“基础 runtime 能力”，而是还差“主语义彻底切换”和“治理约束正式化”。当前最核心的剩余主线仍是把系统从“`Leader` 产生活、runtime 派发执行”推进成“`SuperLeader / Leader / Teammate` 都是长期在线 agent，`task` 只负责 activation，之后由 `mailbox + task list + blackboard` 持续协作推进”。但这条路线图现在必须从真实 mainline baseline 起步：`max_concurrency` 已退出 live path、扁平 `teammate_tasks` 已被 `sequential_slices / parallel_slices` 取代、而且 teammate execution close signal 已以 `teammate_execution_evidence` 为主，`leader_consumed_mailbox` 只剩辅助诊断含义；`authority-escalation-and-scope-extension` 也已经有了 request/wait/finalize first-cut，不再是空白能力。因此剩余 gap 的核心不是再补 pre-mainline delegation semantics，而是把 host-owned resident convergence、coordination transaction、single-owner session truth、动态 authority 升级链，以及 task-surface authority contract 继续收口成主线。与此同时，bootstrap/self-hosting 的 host-owned evidence rewrite 也已被进一步收口成明确后置项：当前 delegation validation 仍依赖 host-owned leader session、runtime-native teammate evidence、单 leader turn 与零 mailbox follow-up gate，所以它应跟在 runtime truth 稳定之后，而不是并行地反向定义 runtime owner。在这条主线下，`authority-escalation-and-scope-extension` 已作为正式 gap 留在路线图里，但它现在代表的是 decision/resume/policy residual hardening；`task-subtree-authority` 则继续承担 task list item 的状态权限、结构权限、只读边界和 subtree escalation 规则。

## 2. 范围与资料来源

- 当前状态与 backlog：
  - [implementation-status.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/implementation-status.md)
  - [current-priority-gap-list.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/current-priority-gap-list.md)
  - [remaining-work-backlog.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/remaining-work-backlog.md)
  - [hierarchical-online-agent-runtime-gap-list.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/hierarchical-online-agent-runtime-gap-list.md)
  - [claude-comparison-and-system-logic.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/claude-comparison-and-system-logic.md)
- 当前 team / resident collaboration 主线：
  - [resident-team-collaboration-and-slice-planning.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/resident-team-collaboration-and-slice-planning.md)
  - [durable-teammate-loop-and-mailbox-consistency.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/durable-teammate-loop-and-mailbox-consistency.md)
  - [agent-abstraction-skill-and-prompt-system.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/agent-abstraction-skill-and-prompt-system.md)
- 当前验证与 contract 主线：
  - [worker-lifecycle-protocol-and-lease.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/worker-lifecycle-protocol-and-lease.md)
  - [execution-guard-failure-patterns.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/execution-guard-failure-patterns.md)
- 关键代码入口：
  - [leader_loop.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py)
  - [teammate_work_surface.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/teammate_work_surface.py)
  - [superleader.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/superleader.py)
  - [group_runtime.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/group_runtime.py)
  - [worker_supervisor.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/worker_supervisor.py)
  - [task.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/contracts/task.py)

## 3. 预定目标的正式定义

这里的“预定目标”不是指再补几个 isolated feature，而是指下面这组整体状态：

### 3.1 层级在线 agent 状态

- `Teammate` 是长期在线的自治执行体
- `Leader` 也是长期在线 agent，但有更高的收敛权限
- `SuperLeader` 与前两者共用同一底座，但组织的是多个 team / lane

### 3.2 task 的降级语义

- `task` 不再是 team 的主执行模式
- `task` 只负责 activation / contract / visible work surface
- 真正持续推进问题解决的是在线 agent 协作

### 3.3 team 内主求解面

- team 内持续依靠 `mailbox + task list + TeamBlackboard`
- leader 更像 activation center + convergence center
- teammate 能自己持续 poll、claim、执行、回写、协作

### 3.4 跨 team 受控协作

- 跨 team 协作通过受控 `mailbox / subscription / circle`
- 默认不是全局聊天室
- 默认是摘要索引优先、按 policy 拉全文

### 3.5 治理和权限边界

- 下层 agent 不能改写上层定义的总目标/硬约束
- task list 的结构修改必须受 scope / subtree / authority 限制
- runtime 有正式的 contract、session truth、completion semantics

## 4. 已经不再是核心 gap 的内容

下面这些能力已经进入主线，不应继续按“主要缺口”重复计算：

1. `durable-supervisor-sessions`
2. `reconnector`
3. `protocol-bus`
4. `runtime-owned coordination commit helper` first-cut
5. `task.receipt` live path
6. `worker-agent-session-truth` first-cut
7. `superleader lane session graph` first-cut
8. `team-parallel-execution` first-cut
9. teammate-owned verification loop / protocol-first verification ownership
10. `team-budget-simplification` / `max_concurrency` 退出 live path
11. `leader-slice-graph-output` / flat `teammate_tasks` 退出 live path
12. `teammate_execution_evidence` 作为主关闭证据

判断：

- 这些能力并不是“全部彻底完工”
- 但它们已经进入 mainline first-cut，后续更多属于 hardening / deeper cut，而不是 0 到 1 缺失
- 尤其 `max_concurrency`、扁平 `teammate_tasks`、以及把 `leader_consumed_mailbox` 当唯一关闭证明的 framing，都不应再作为剩余主线重新立项

## 5. 真实剩余 gap 清单

下面这 11 项才是当前离目标状态真正还没完成的差距。

### 5.1 Gap 1 `online-collaboration-runtime`

这是最大的总 gap，也是其他多数 gap 的上位主线。

当前缺的是：

- 把系统彻底从 `leader turn -> runtime dispatch` 推到 `resident collaboration`
- 让 `task` 明确降级为 activation surface
- 让 `mailbox + task list + blackboard` 成为 primary execution surface
- 把 runtime-owned first-cut helper 继续收口成 store-level coordination transaction、host-owned resident hierarchy、以及 single-owner session truth

### 5.2 Gap 2 `team-primary-semantics-switch`

这是最接近 Claude team agent 的 team 内主语义切换。

当前缺的是：

- teammate 真正脱离 leader `_run_teammates(...)` activation shell 的 host-owned agent loop
- leader 真正 convergence-first 的 team shell，并更深地消费 `sequential_slices / parallel_slices` 已经落地主协议里的依赖与并行元数据
- 更少依赖 leader turn 边界，更强依赖持续在线协作与 host-owned wake/idle/session continuation

### 5.3 Gap 3 `superleader-isomorphic-runtime`

这是把 `SuperLeader` 正式切到同一底座。

当前缺的是：

- `SuperLeader` 成为真正长期在线 agent
- 通过 digest / lane mailbox / objective blackboard 持续推进
- 成为真正的 `leader-of-leaders`

### 5.4 Gap 4 `cross-team-collaboration-layer`

这是多 team 受控协作层。

当前缺的是：

- cross-team directed mailbox 正式 runtime API
- `subscription / circle / summary index / full-text fetch` 正式主线
- 更完整的默认可见性和跨 team policy gate

### 5.5 Gap 5 `session-truth-and-coordination-transaction-convergence`

这是底层真相收敛层。

当前缺的是：

- store-level coordination transaction
- session truth owner 进一步收口
- 更彻底的 crash / reconnect 后 continuity
- bootstrap/self-hosting evidence rewrite 继续滞后于这条 gap：当前 `_delegation_validation_metadata(...)` 仍要求 `host_owned_leader_session && teammate_execution_evidence && leader_turn_count == 1 && mailbox_followup_turns_used == 0` 才会 `validated`

补充现状：

- 2026-04-10 起，`task.receipt / task.result / authority.request / mailbox consume` 已不再只提交 task/blackboard/delivery/cursor/agent-session：worker-session snapshot 也会进入同一条 coordination write family；PostgreSQL 同事务提交，其他 store 立即补 fallback save。
- 因而这条 gap 当前不再主要是“receipt/result/authority/mailbox consume 还没有 worker-session write”，而是更深层的 `WorkerSession / AgentSession / ResidentCoordinatorSession` broader single-owner ownership 仍未彻底统一。

### 5.6 Gap 6 `planner-feedback`

当前已有 deterministic template planning 和 bounded dynamic planning。

仍缺：

- planner memory
- evaluator feedback 驱动的 replan
- decomposition 自适应调整

### 5.7 Gap 7 `sticky-provider-routing`

当前已有 per-assignment fallback / backoff / exhaustion。

仍缺：

- provider health memory
- sticky route 选择
- cross-round provider preference / score

### 5.8 Gap 8 `permission-broker`

当前已有基础 hook，但还没有正式 production-grade permission orchestration。

仍缺：

- authority / sandbox / approval control plane
- 更正式的 permission contract 和 runtime 路径

### 5.9 Gap 9 `postgres-persistence-hardening`

当前并不是没有 Postgres，而是还没有 production hardening。

仍缺：

- query/index discipline
- migration strategy
- 持久化结构的生产级收口

### 5.10 Gap 10 `authority-escalation-and-scope-extension`

这是本轮新明确补上的 runtime 动态协作缺口。

核心问题不是“teammate 有没有 blocker 文本”，而是：

- teammate 在执行中发现真实 blocker 且修复它需要额外 scope/authority 时，first-cut 虽然已经能提交正式 `AuthorityRequest`，但 authority 决策链还没闭合
- leader 也没有正式的 `grant / reroute / escalate / deny` 裁决面
- authority decision 之后的 teammate 恢复推进路径还没接回 runtime 主线
- protected boundary policy 还没有正式 contract 化
- self-hosting 与 superleader 虽然已经能把 authority wait graph 作为 first-cut 的正式中间态收尾，但 authority denied graph 与后续恢复语义还没一起收口

这条 gap 的直接目标是把下面这组规则收敛成 runtime 主语义：

- teammate 不能自己扩权，只能申请扩权
- leader 是 team 内第一 authority center，可以对普通 repo 内文件做 scope 扩展裁决
- 共享核心文件、跨 team 文件、全局 contract 文件默认属于 protected boundary，需要升级到 superleader 或更高 authority
- `WAITING_FOR_AUTHORITY` 是正式中间状态，不应再被折叠成 `blocked -> failed`
- objective finalize 必须能区分 authority wait graph、authority denied graph 和真实执行失败

判断：

- 这条 gap 不是 `permission-broker` 的替身；后者更偏全局 authority/sandbox control plane
- 它也不是 `task-subtree-authority` 的替身；后者更偏 task list 结构权限
- 它建立在 `team-primary-semantics-switch` 之上，但优先级应高于 `task-subtree-authority`

### 5.11 Gap 11 `task-subtree-authority`

这是这轮新明确补上的 gap。

核心问题不是“teammate 能不能碰 task list”，而是：

- task list item 的结构修改权限还没有正式 contract
- 不同层级 agent 对 task item 的 `读 / 状态改动 / 结构改动 / escalation` 边界还没正式化

这条 gap 的直接目标是把下面这组规则收敛成 runtime 主语义：

- `Teammate` 对 leader 分给它的 task item，拥有状态权限
- `Teammate` 对自己创建的 child task subtree，拥有结构权限
- `Teammate` 不能改写上层 task 的 `goal / hard_constraints / acceptance_checks / owned_paths`
- 结构变更不做硬删除，而做 `superseded / cancelled / merged_into / not_needed`
- 所有结构变更必须携带 `reason + derived_from`
- 超出 subtree 或跨 team 的变更必须走 escalation，而不是直接写 task list

截至 2026-04-09，这条 gap 已完成第一段 runtime cut：

- `TaskCard` 已把 local status authority、local structure authority 与 provenance parent 收口成可消费 contract
- `TaskCard.surface_authority_view()` 与 `GroupRuntime.read_task_surface_authority_view(...)` 已把 ancestor lineage、subtree root/lineage、protected field surface 收口成共享 authority projection；runtime 现在还能通过 `classify_task_*authority(...)` 直接区分 `local_allow / escalation_needed / forbidden`
- `GroupRuntime.submit_task(...) / update_task_status(...) / commit_task_surface_mutation(...)` 已统一消费这组 truth，能区分 teammate 的 allowed local action 与 forbidden upper-scope edit
- teammate root write 现在必须挂到已存在 parent task；child write 必须带 `reason + derived_from`
- direct cancel 已不再允许绕过 `commit_task_surface_mutation(...)` 这类 non-destructive 结构语义
- authority reroute 也已对齐到同一条 mutation / replacement reason gate
- `commit_task_surface_mutation(...)` 现在会把 local subtree mutation 保留在现有 runtime 直写路径上，但会把 cross-subtree / cross-team mutation 自动转成 authority request；`commit_task_protected_field_write(...)` 也已经进入主线，并把 teammate 对 protected task fields 的改动接进同一条 authority request/decision/outbox 流程
- `commit_authority_decision(...)`、leader reactor 与 superleader root reactor 现在会在 `grant` 时真正应用 task-surface mutation / protected-field write intent，而不是只把这类 request 当成抽象 scope extension
- resident live view / leader revision bundle 也已开始消费这组 governed truth：per-lane `task_surface_authority` snapshot、`task_surface_authority_lane_ids / lane_task_surface_authority_waiting_task_ids`，以及 `authority_notices / project_item_notices` first-cut 都已进入主线，因此 runtime/planning consumer 至少已经能看见“task surface 受治理约束”这一层事实

当前剩余缺口已经从“完全没有 task surface authority validator”收缩成更深一层：

- protected read-only field 在当前 `goal / owned_paths / allowed_inputs / output_artifacts / verification_commands / handoff_to / merge_target` 直写表面之外的更完整 policy 面
- richer actor/read mode policy，而不只是 teammate vs upper-scope 的 first-cut 区分
- review-driven / project-item-driven escalation 虽然已经有 `authority_notices / project_item_notices` advisory wiring，但更完整的 authority/replan/runtime driver 接线仍未完成

判断：

- 这条 gap 是建立在 slice graph / live task surface 已经进入主线的 baseline 之上
- 它要解决的是“长期在线协作下谁能怎样改 task surface”，而不是回到旧的 flat delegation list 语义

## 6. 这 11 项之间的依赖关系

这 11 项不是平铺 backlog，而是有明显依赖关系。

### 6.1 顶层强依赖链

推荐按下面主链理解：

1. `online-collaboration-runtime`
2. `session-truth-and-coordination-transaction-convergence`
3. `team-primary-semantics-switch`
4. `superleader-isomorphic-runtime`
5. `cross-team-collaboration-layer`

### 6.2 底层支撑 gap

下面几项是主链的支撑层：

- `session-truth-and-coordination-transaction-convergence`
- `authority-escalation-and-scope-extension`
- `task-subtree-authority`

判断：

- 不把这三层 formalize，主语义就容易停留在 prompt 约定或局部 helper
- `authority-escalation-and-scope-extension` 直接决定 teammate 遇到真实 blocker 时，是继续协作收敛，还是被硬折叠成终态失败
- `task-subtree-authority` 直接决定 `task list` 能不能安全地从“派工清单”升级成“协作 surface”
- 这三层支撑 gap 的目标都是把现有 mainline baseline 收口成单一 owner / 正式 contract，不是回退去重做 pre-mainline delegation semantics

### 6.3 工程化增强层

下面这些更偏 production / adaptivity：

- `planner-feedback`
- `sticky-provider-routing`
- `permission-broker`
- `postgres-persistence-hardening`

## 7. 推荐执行顺序

如果目标是尽快逼近你要的“Claude 式长期在线协作 + SuperLeader 扩展”状态，推荐顺序是：

1. `online-collaboration-runtime`
2. `session-truth-and-coordination-transaction-convergence`
3. `team-primary-semantics-switch`
4. `authority-escalation-and-scope-extension`
5. `task-subtree-authority`
6. `superleader-isomorphic-runtime`
7. `cross-team-collaboration-layer`
8. `planner-feedback`
9. `sticky-provider-routing`
10. `permission-broker`
11. `postgres-persistence-hardening`

判断：

- 这里把 `session-truth-and-coordination-transaction-convergence` 提前，是因为当前主线最真实的残余难点已经是 host-owned resident convergence / single-owner truth，而不是 `max_concurrency` 或扁平 `teammate_tasks` 这类已切走的旧协议
- bootstrap/self-hosting evidence rewrite 要放在这条 host-owned truth 稳定之后；否则 validation heuristic 会继续追着未切稳的 runtime semantics 漂移
- `authority-escalation-and-scope-extension` 应该先于 `task-subtree-authority`，因为如果 teammate 连“遇到真实 blocker 后如何申请更高 authority”都没有正式语义，后面的 task surface 治理会不断被临时补丁打断
- `task-subtree-authority` 仍然应该比很多人直觉里更靠前，因为一旦 team 真开始长期在线协作，task list 的结构权限如果没有 formalize，系统很快就会变乱

## 8. 前 5 Gap 执行清单

如果要把这些内容直接交给 `Agent Orchestra`，当前最值得连续推进的不是一串松散 feature，而是前 5 个最高优先级 gap 的执行清单。

这 5 项的排序不是“哪个更酷”，而是“哪个不先做，后面的主语义就会继续分叉”：

1. `online-collaboration-runtime`
2. `session-truth-and-coordination-transaction-convergence`
3. `team-primary-semantics-switch`
4. `authority-escalation-and-scope-extension`
5. `task-subtree-authority`

判断：

- 前 3 项决定系统能不能真正从“派工 runtime”切到“长期在线协作 runtime”
- 第 4 项决定 teammate 遇到真实 blocker 时，是继续协作还是被系统硬折叠成终态失败
- 第 5 项决定 task list 能不能安全升级成长期协作面，而不是越做越乱

### 8.1 Gap 1 `online-collaboration-runtime`

目标：

- 把 `task` 正式降级成 activation surface
- 把 `mailbox + task list + blackboard` 正式提升成 primary execution surface
- 把 `SuperLeader / Leader / Teammate` 往同一套长期在线 agent 底座继续收敛

本轮应完成：

- 明确 runtime live path 不再以 `leader turn -> runtime dispatch` 为默认心智
- 把 leader/teammate/superleader 的 runtime 主语义继续收口到 resident collaboration
- 让后续 gap 都建立在“在线协作已经是 baseline”这个前提上，而不是继续兼容旧派工心智

重点主文件：

- [leader_loop.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py)
- [teammate_work_surface.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/teammate_work_surface.py)
- [teammate_online_loop.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/teammate_online_loop.py)
- [superleader.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/superleader.py)
- [session_host.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/session_host.py)

完成判据：

- 新工作不再默认要求重新触发 leader prompt turn 才能推进
- team 内推进的默认路径是 resident collaboration，而不是单轮 dispatch
- self-hosting / gap 关闭证据不再奖励旧 delegation 语义

### 8.2 Gap 2 `session-truth-and-coordination-transaction-convergence`

目标：

- 把 coordination side effect 收敛成 store-level truth
- 把 session 语义收敛成 single-owner truth

本轮应完成：

- 在 receipt/result/authority/mailbox consume 已共享 durable outbox + authoritative persisted snapshot boundary first-cut 的基础上，继续把残余 coordination skew 收口到同一 coordination 主语义；截至 2026-04-09，teammate live path 的 caller-side 第二 commit family 已删除，剩余重点转成多层 session owner 的 single-owner 收口
- 缩小 crash window 和 state skew
- 让恢复、重连、继续执行都站在统一 session truth 上
- 明确 bootstrap/self-hosting evidence rewrite 仍是这条 gap 之后的 follow-on，而不是当前 lane 里反向定义 truth 的前置 gate

重点主文件：

- [group_runtime.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/group_runtime.py)
- [session_host.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/session_host.py)
- [storage/base.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/storage/base.py)
- [store.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/storage/postgres/store.py)
- [worker_supervisor.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/worker_supervisor.py)

完成判据：

- `WorkerSession / AgentSession / ResidentCoordinatorSession` 不再各自维持半套真相
- mailbox consume、task receipt/result、session 写回、delivery snapshot 更新的偏斜窗口明显缩小
- reconnect / recover 路径不再依赖多层状态拼接推断

### 8.3 Gap 3 `team-primary-semantics-switch`

目标：

- 把 team 内主语义正式切成 Claude 风格的长期在线协作面
- 让 leader 更像 activation center + convergence center
- 让 teammate 更像常驻在线执行体

本轮应完成：

- 进一步削弱 leader `_run_teammates(...)` 作为主执行壳的地位
- 推进 teammate 从 leader-owned activation shell 走向 host-owned online loop
- 更深消费 `sequential_slices / parallel_slices`、`depends_on`、`parallel_group` 这些已进入主协议的 slice 元数据

重点主文件：

- [leader_loop.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py)
- [teammate_work_surface.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/teammate_work_surface.py)
- [teammate_online_loop.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/teammate_online_loop.py)
- [leader_output_protocol.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_output_protocol.py)
- [task.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/contracts/task.py)

完成判据：

- teammate 在 idle 时仍可继续 poll / claim / execute / publish，而不是只在 leader 激活边界上活一下
- leader 的主要职责进一步收敛为 convergence，而不是 task dispatch shell
- team runtime 默认站在 `mailbox + task list + TeamBlackboard` 主语义上

### 8.4 Gap 4 `authority-escalation-and-scope-extension`

目标：

- 把 teammate 在执行中发现真实 blocker 后的 authority 请求、leader 裁决和恢复推进语义正式 contract 化

已完成的 first-cut：

- 形式化 `AuthorityRequest / ScopeExtensionRequest`
- 引入 `WAITING_FOR_AUTHORITY` 的 task / lane / objective 语义
- teammate blocked final report 到 authority wait 的 runtime commit
- superleader 对 authority wait graph 的 first-cut finalize

本轮剩余应完成：

- 形式化 `AuthorityDecision`
- 明确 `grant / reroute / escalate / deny` 这 4 种 leader 裁决动作
- 明确普通 repo 内文件与 protected authority boundary 的分层规则
- 把 authority decision 之后的 resume / continue path 接回 runtime

重点主文件：

- [teammate_work_surface.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/teammate_work_surface.py)
- [leader_loop.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py)
- [group_runtime.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/group_runtime.py)
- [evaluator.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/evaluator.py)
- [superleader.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/superleader.py)

完成判据：

- teammate 遇到真实 blocker 时可以申请 authority / scope extension，而不是只能把问题折叠成终态失败
- leader 可以对普通 repo 内扩权做第一层裁决
- protected boundary 会正式升级，而不是继续隐式失败
- self-hosting round 不会再因为 authority wait graph 而无限 resident 等待不落盘

### 8.5 Gap 5 `task-subtree-authority`

目标：

- 把 task list item 的状态权限、结构权限、只读边界与 escalation 规则正式 contract 化

本轮应完成：

- 形式化 `Teammate` 对被分配 task item 的状态权限
- 形式化 `Teammate` 对自己创建 child task subtree 的结构权限
- 禁止下层直接改写上层的 `goal / hard_constraints / acceptance_checks / owned_paths`
- 把 `derived_from`、`reason`、`superseded / cancelled / merged_into / not_needed` 变成正式结构语义

重点主文件：

- [task.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/contracts/task.py)
- [group_runtime.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/group_runtime.py)
- [leader_output_protocol.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_output_protocol.py)
- [teammate_work_surface.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/teammate_work_surface.py)

完成判据：

- task list 从“派工清单”升级为“受治理的协作面”
- 跨 subtree、跨 scope、跨 team 的结构修改必须走 escalation
- runtime 能正式区分谁能读、谁能改状态、谁能改结构

### 8.6 Gap 6 `superleader-isomorphic-runtime`

目标：

- 把 `SuperLeader` 推进成与 `Leader / Teammate` 同底座的长期在线 agent
- 让它通过 digest / lane mailbox / objective blackboard 持续推进多 team 收敛

本轮应完成：

- 把 `SuperLeader` 从“ready lane 调度器”继续推进成“leader-of-leaders”
- 推进 lane session graph 从 launch boundary 走向可重入 / 可恢复 / 可跨 cycle step 的 leader runtime
- 让 superleader 的 prompt turn 进一步降级为 replan / rebalance / topology 调整动作

重点主文件：

- [superleader.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/superleader.py)
- [leader_loop.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py)
- [session_host.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/session_host.py)
- [objective.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/contracts/objective.py)

完成判据：

- `SuperLeader` 不再主要像外层 while-loop 调度器
- 它能长期在线观察多个 lane/team 的 digest 和收敛状态
- 它只在需要 team 数量调整、职责改写、重规划时才强依赖 prompt turn

### 8.7 建议执行顺序

建议按下面顺序交给 `Agent Orchestra`，不要并行乱投：

1. `online-collaboration-runtime`
2. `session-truth-and-coordination-transaction-convergence`
3. `team-primary-semantics-switch`
4. `authority-escalation-and-scope-extension`
5. `task-subtree-authority`

原因：

- 先切主语义，再收 session/transaction 真相，再收 team 内协作，再补动态 authority 升级链，再 formalize task 权限
- 如果顺序反过来，系统会继续在旧 dispatch 语义上叠补丁

### 8.8 可直接投喂给 Agent Orchestra 的文本

下面这段可以直接作为下一轮输入：

```md
先读这些知识文档：

1. resource/knowledge/README.md
2. resource/knowledge/agent-orchestra-runtime/README.md
3. resource/knowledge/agent-orchestra-runtime/implementation-status.md
4. resource/knowledge/agent-orchestra-runtime/current-priority-gap-list.md
5. resource/knowledge/agent-orchestra-runtime/hierarchical-online-agent-runtime-gap-list.md
6. resource/knowledge/agent-orchestra-runtime/target-gap-roadmap-and-task-subtree-authority.md
7. resource/knowledge/agent-orchestra-runtime/resident-team-collaboration-and-slice-planning.md
8. resource/knowledge/agent-orchestra-runtime/durable-teammate-loop-and-mailbox-consistency.md

这轮只做前 5 个最高优先级 gap，不要扩散到 planner-feedback / sticky-provider-routing / permission-broker / postgres-persistence-hardening。

执行顺序固定为：

1. online-collaboration-runtime
2. session-truth-and-coordination-transaction-convergence
3. team-primary-semantics-switch
4. authority-escalation-and-scope-extension
5. task-subtree-authority

硬约束：

- 不要回退已经进入主线的 `sequential_slices / parallel_slices`
- 不要回退 `max_concurrency` 已退出 live path 这条裁决
- 不要回退 teammate-owned verification / protocol-first evidence
- 不要把已完成 first-cut 的 durable-supervisor-sessions / reconnector / protocol-bus 重新当主 blocker

本轮目标不是再补零散 feature，而是把系统继续从“leader 派工 runtime”切到“层级在线协作 runtime”。

完成后必须同步更新：

- resource/knowledge/agent-orchestra-runtime/implementation-status.md
- resource/knowledge/agent-orchestra-runtime/current-priority-gap-list.md
- resource/knowledge/agent-orchestra-runtime/authority-escalation-and-scope-extension.md
- resource/knowledge/agent-orchestra-runtime/target-gap-roadmap-and-task-subtree-authority.md
```

## 9. 相关文档

- [implementation-status.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/implementation-status.md)
- [current-priority-gap-list.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/current-priority-gap-list.md)
- [authority-escalation-and-scope-extension.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/authority-escalation-and-scope-extension.md)
- [remaining-work-backlog.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/remaining-work-backlog.md)
- [hierarchical-online-agent-runtime-gap-list.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/hierarchical-online-agent-runtime-gap-list.md)
- [resident-team-collaboration-and-slice-planning.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/resident-team-collaboration-and-slice-planning.md)
- [durable-teammate-loop-and-mailbox-consistency.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/durable-teammate-loop-and-mailbox-consistency.md)
- [agent-abstraction-skill-and-prompt-system.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/agent-abstraction-skill-and-prompt-system.md)
- [claude-comparison-and-system-logic.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/claude-comparison-and-system-logic.md)
