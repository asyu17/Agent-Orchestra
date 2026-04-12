# agent_orchestra 当前目标对照 Gap 清单

## 1. 一句话结论

截至 2026 年 4 月 10 日，`agent_orchestra` 已经不再主要缺“基础 runtime 零件”，而是仍然缺“最终主语义切换”。当前最核心的差距不是再补几个 isolated feature，而是把系统彻底从“`Leader` 产生活、runtime 派发执行”切换成“`Teammate / Leader / SuperLeader` 都是长期在线 agent，`task` 只负责 activation/contract，真正推进问题解决的是 `mailbox + task list + blackboard + digest/subscription` 持续协作”。在这条总 gap 里，当前最硬的底层边界已经变成 host-owned coordination/session truth 的 single-owner 收口；与之相比，bootstrap/self-hosting evidence rewrite 明确只是 runtime truth 稳定后的 follow-on。

## 2. 范围与资料来源

- 当前现状与 backlog：
  - [implementation-status.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/implementation-status.md)
  - [current-priority-gap-list.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/current-priority-gap-list.md)
  - [remaining-work-backlog.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/remaining-work-backlog.md)
- 目标态与系统逻辑：
  - [online-collaboration-runtime-migration.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/online-collaboration-runtime-migration.md)
  - [resident-hierarchical-runtime.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/resident-hierarchical-runtime.md)
  - [agent-abstraction-skill-and-prompt-system.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/agent-abstraction-skill-and-prompt-system.md)
  - [claude-comparison-and-system-logic.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/claude-comparison-and-system-logic.md)
  - [hierarchical-online-agent-runtime-gap-list.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/hierarchical-online-agent-runtime-gap-list.md)
  - [target-gap-roadmap-and-task-subtree-authority.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/target-gap-roadmap-and-task-subtree-authority.md)
- 当前热点代码：
  - [leader_loop.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py)
  - [teammate_work_surface.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/teammate_work_surface.py)
  - [superleader.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/superleader.py)
  - [group_runtime.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/group_runtime.py)
  - [session_host.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/session_host.py)
  - [transport_adapter.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/transport_adapter.py)

## 3. 目标状态定义

### 3.1 长期在线 Agent 底座

目标状态不是“leader 更会派工”，而是：

- `Teammate` 是长期在线的自治执行体
- `Leader` 也是同底座 agent，但有更高收敛权限
- `SuperLeader` 与前两者同构，只是组织的是多个 team / lane

### 3.2 Task 降级为 Activation Surface

目标状态要求：

- `task` 只负责 activation / contract / visible work surface
- `task` 不再是 team 的主执行模式
- 真正持续推进问题解决的是在线 agent 协作

### 3.3 Team 内部主求解面

目标状态要求：

- team 内主要依靠 `mailbox + task list + TeamBlackboard`
- `Leader` 更像 activation center + convergence center
- `Teammate` 能自己持续 poll、claim、执行、回写、协作

### 3.4 SuperLeader 的同构化

目标状态要求：

- `SuperLeader` 不是外层 while-loop 调度器
- 它是同一底座上的 `leader-of-leaders`
- 它主要消费 lane digest、mailbox、objective blackboard 做持续收敛

### 3.5 跨 Team 受控协作

目标状态要求：

- 跨 team 协作通过 `subscription / circle / directed mailbox`
- 默认不是全局聊天室
- 默认是摘要索引优先、按 policy 拉全文

### 3.6 治理和权限边界

目标状态要求：

- 下层 agent 不能改写上层定义的总目标/硬约束
- task list 的状态权限、结构权限、只读边界和 escalation 规则必须正式 contract 化
- runtime 必须有正式的 session truth、completion semantics 和 authority chain

## 4. 已完成且不应重开为主 Gap 的内容

下面这些能力已经进入主线，不应继续按“主要缺口”重复计算：

- `protocol-native-lifecycle-wait`
- `durable-supervisor-sessions`
- `reconnector`
- `protocol-bus`
- `role-profile-unification`
- `message-pool-subscriptions`
- `message-visibility-policy`
- `superleader-parallel-scheduler`
- `team-parallel-execution` first-cut
- `leader-teammate-delegation-validation`
- `runtime-owned coordination commit` first-cut
- `task.receipt` live path
- `worker-agent-session-truth` first-cut
- `superleader-session-graph` first-cut
- `multi-leader-planning-review` default first-cut
- `authority-escalation-and-scope-extension` 的 request/wait/decision/closure first-cut

判断：

- 这些内容不是“全部彻底完工”
- 但它们已经进入 mainline first-cut
- 后续如果继续修改，通常属于更深的 hardening / convergence，而不是 0 到 1 缺失

## 5. 当前权威 Gap 清单

### 5.1 P0 总 Gap `online-collaboration-runtime`

这是当前最大的总 gap，也是其他多数 gap 的上位主线。它的本质不是再补一个 isolated feature，而是把系统主求解模式彻底切到长期在线协作。

它当前包含 5 个内部必须完成的子 gap。

### 5.2 P0-1 `team-primary-semantics-switch`

这是最接近你目标的核心差距。

当前仍缺：

- `Teammate` 真正脱离 leader activation shell 的独立长期在线 loop
- `Leader` 真正 convergence-first 的 team shell，而不是 assignment-first 的 turn executor
- `task -> activation surface` 的正式主语义切换
- `mailbox + task list + TeamBlackboard` 成为 primary execution surface，而不是 leader turn 的附属物

当前虽然已经有：

- `TeammateWorkSurface.run(...)`
- per-slot `TeammateOnlineLoop`
- promptless convergence
- directed mailbox / autonomous claim

但判断上仍然不能说它已经是 Claude 式成熟 team shell，只能说它已经进入这条主线的 first-cut。

### 5.3 P0-2 `coordination-transaction-and-session-truth-convergence`

这是底层真相收敛层。

当前仍缺：

- `task / mailbox / blackboard / delivery / cursor / session` 的 store-level coordination transaction
- `WorkerSession / AgentSession / ResidentCoordinatorSession` 的 single-owner convergence
- `ResidentSessionHost` 成为更完整的 session truth owner，而不只是越来越重要的 host helper
- crash / reconnect 后 continuity 的更彻底收口

当前虽然已经有：

- runtime-owned coordination commit first-cut
- receipt/result/authority/mailbox consume 的 durable outbox + authoritative persisted snapshot boundary first-cut
- worker/agent session reconnect truth first-cut
- host-owned lane/session graph

但它们还没有形成彻底单一主真相；截至 2026-04-09，teammate live path 的 post-commit `save_session(...) / save_blackboard_entry(...)` 写回已经删除，authority request / decision 也已补上 authoritative persisted readback。2026-04-09 同日，StoreBacked `ResidentSessionHost` 也已经开始在 continuation read path 上把 persisted `AgentSession + WorkerSession` 投影成 host truth，`DefaultWorkerSupervisor` 的 reclaim/finalize/reattach 也改成通过 host projection 写回，而 `GroupRuntime` authoritative session readback 同步优先走 host。2026-04-10 又把 coordination write 自己再收紧了一层：`commit_directed_task_receipt(...) / commit_teammate_result(...) / commit_authority_request(...) / commit_mailbox_consume(...)` 现在都会把 `WorkerSession` snapshot 一起推进 commit bundle，PostgreSQL 会同事务提交，其他 store 至少会立即补 worker-session fallback save，因此 stale `ACTIVE` worker session 与 stale mailbox cursor 已不再继续把 result/authority/mailbox 的 authoritative host readback 拉回旧状态。判断：当前真正剩余的收口点因而又缩了一层，不再是 host read/projection 缺位，也不再是 coordination family 完全缺 worker-session write，而是更广义的 single-owner write/ownership 仍并存，以及 broader host-owned continuation / replay 语义还没有完全统一。

同时，bootstrap/self-hosting 当前仍不是 session truth owner：`_delegation_validation_metadata(...)` 还是在 `host_owned_leader_session && teammate_execution_evidence && leader_turn_count == 1 && mailbox_followup_turns_used == 0` 成立时，才把 delegation validation 判成 `validated`。这说明 host-owned evidence rewrite 仍应视为 runtime truth 收稳后的 follow-on，而不是拿 bootstrap heuristic 反过来定义 runtime 主真相。

### 5.4 P0-3 `superleader-isomorphic-runtime`

这是把上层正式切到同一底座的差距。

当前仍缺：

- `SuperLeader` 成为真正长期在线的 `leader-of-leaders`
- 通过 digest / lane mailbox / objective blackboard 持续推进
- 从当前 resident scheduler 继续推进到更完整的 continuous convergence runtime

当前虽然已经有：

- shared resident kernel
- planning-review 默认主路径
- lane session graph
- dependency gate
- runtime-owned resident live view 已经开始直接消费 lane digest/mailbox metadata、host-owned leader session projection，以及 objective `coordination / message_runtime / resident_live_view`
- stale `PENDING` lane snapshot 不会再压过 host-owned `WAITING_FOR_MAILBOX` leader session，lane digest metadata 也能让 superleader 继续进入 `WAITING_FOR_MAILBOX`
- self-hosting bootstrap 已开始把这层更强的 resident truth 透传成 `superleader_runtime_status`

但它仍更像“很强的上层 scheduler”，还不是完全等价于你要的上层在线 agent。

### 5.5 P0-4 `cross-team-collaboration-layer`

这是你和 Claude team agent 最大的扩展目标之一。

当前仍缺：

- cross-team directed mailbox 正式 runtime API
- `subscription / circle / summary index / full-text fetch` 进入主线
- 更完整的默认可见性、摘要索引和全文拉取规则
- 跨 team 协作从“基础设施可用”升级成“系统默认工作方式”

当前虽然已经有：

- message pool
- subscription
- visibility policy
- full-text access gate

但还没有形成你想要的“多个 team 在线协作、动态订阅、受控形成消息圈”的成熟形态。

### 5.6 P0-5 `task-surface-authority-contract`

这是治理层里已经进入 first-cut 主线、但仍未彻底完成的一段。

当前已完成：

- `TaskCard` 已显式区分 local status authority 与 local structure authority，并把 provenance parent 变成 runtime 可消费 truth
- `TaskCard.surface_authority_view()` 与 `GroupRuntime.read_task_surface_authority_view(...)` 已把 local authority、ancestor lineage、subtree root/lineage、protected read-only field surface 收口成共享 authority view；`classify_task_submission_authority(...) / classify_task_status_update_authority(...) / classify_task_surface_mutation_authority(...) / classify_task_protected_field_write(...)` 现在会直接产出 `local_allow / escalation_needed / forbidden` 判定，而不是让 caller 重复推导
- `GroupRuntime.submit_task(...) / update_task_status(...) / commit_task_surface_mutation(...)` 已统一校验 teammate root write、subtree ownership、`reason + derived_from`，以及 direct cancel 必须走 non-destructive mutation API
- authority reroute 现在也会补齐 structure reason，并复用同一条 task-surface mutation / replacement gate
- `commit_task_surface_mutation(...)` 现在会保留 local subtree mutation 的直写路径，但把 cross-subtree / cross-team mutation 自动转成带 `task_surface_authority` intent 的 `authority.request`；`commit_task_protected_field_write(...)` 也已进入主线，并把 teammate 对 protected task fields 的写入送进同一条 authority request/decision/outbox 路径
- `commit_authority_decision(...)`、leader reactor 与 superleader root reactor 现在会在 `grant` 时真正应用 task-surface mutation / protected-field write intent，而不是只把 authority grant 解释成额外 `owned_paths`
- resident lane live view 与 planning revision bundle 现在也开始消费同一组 governed task-surface truth：`read_resident_lane_live_views(...)` 会导出 per-lane `task_surface_authority` snapshot，`resident_live_view` 会继续汇总 authority lane/task ids，而 `build_leader_revision_context_bundle(...)` 会自动补齐 `authority_notices / project_item_notices` 与 structured metadata；review/project-item 信号仍保持 advisory，不直接越过 authority/runtime gate

当前仍缺：

- protected read-only field 在当前 `goal / owned_paths / allowed_inputs / output_artifacts / verification_commands / handoff_to / merge_target` 直写表面之外的更完整 policy/contract
- review/project-item 驱动的 escalation 输入面虽然已进入 `authority_notices / project_item_notices` first-cut，但还没有成为更硬的 coordination/replan/authority driver，且 richer actor/read-mode policy 仍待 formalize
- leader / superleader 之外更细粒度 actor class、read mode 和 task surface policy 仍未彻底收口

### 5.7 P1 `review-driven-knowledge-fusion-as-runtime-driver`

你要的目标不只是“有 review artifact”，而是“review 真的驱动群体智能融合”。

当前仍缺：

- review slot / hierarchical review / planning-review 继续收口成 runtime 主驱动
- claim / replan / activation gate 更强依赖 review 结果
- leader review、cross-team review、superleader synthesis 不再只是 first-cut 记录，而变成强驱动信号

当前这些能力已经进入 first-cut，但还没有真正成为系统默认靠它做判断的主求解方式。

### 5.8 P1 `sticky-provider-routing`

当前 per-assignment fallback / backoff / exhaustion 已有；仍缺：

- provider health memory
- sticky route 选择
- cross-round provider preference / score

### 5.9 P1 `planner-feedback`

当前 deterministic template planning 与 bounded dynamic planning 已有；仍缺：

- planner memory
- evaluator feedback 驱动的 replan
- runtime feedback 驱动的 decomposition 调整

### 5.10 P1 `permission-broker`

当前已有静态 allow/deny contract；仍缺：

- authority / sandbox / approval control plane
- 更正式的 permission contract
- runtime permission orchestration

### 5.11 P1 `postgres-persistence-hardening`

当前 covered CRUD 已有；仍缺：

- query / index discipline
- migration strategy
- production hardening

### 5.12 P2 `long-lived-resident-worker-reconnect-hardening`

这是更深一层的长期运行控制面。

当前仍缺：

- 更长生命周期的 resident worker session
- reconnect after active crash 的更细粒度 control / takeover path
- 更强的 resident worker / mailbox / task-list 持续驱动

判断：

- 这条不是当前主求解模式切换的第一优先级
- 它建立在前面的 session truth / coordination transaction / online collaboration 更稳定之后

## 6. 推荐执行顺序

推荐顺序如下：

1. `team-primary-semantics-switch`
2. `coordination-transaction-and-session-truth-convergence`
3. `superleader-isomorphic-runtime`
4. `cross-team-collaboration-layer`
5. `task-surface-authority-contract`
6. `review-driven-knowledge-fusion-as-runtime-driver`
7. `sticky-provider-routing`
8. `planner-feedback`
9. `permission-broker`
10. `postgres-persistence-hardening`
11. `long-lived-resident-worker-reconnect-hardening`

原因：

- 前 4 项决定系统是否真正进入你要的长期在线协作主语义
- 第 5 项决定长期在线协作下的治理边界是否稳固
- 第 6 项决定多 agent / 多 team 是否真的形成知识融合，而不是只共享日志
- 最后 5 项主要是自适应与生产化

## 7. 与现有文档的分工

这份文档的定位是：

- 回答“当前系统和目标状态之间还差什么”
- 给出一份面向目标的权威 gap 清单

其他几份文档的分工应理解为：

- [current-priority-gap-list.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/current-priority-gap-list.md)
  - 回答“self-hosting 下一轮优先投喂什么”
- [remaining-work-backlog.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/remaining-work-backlog.md)
  - 回答“扩展版 backlog 和当前已完成/未完成列表是什么”
- [hierarchical-online-agent-runtime-gap-list.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/hierarchical-online-agent-runtime-gap-list.md)
  - 回答“如果按大阶段推进，主线怎么拆”

如果后续再次讨论“现在还差什么”，优先以本文为准。

## 8. 相关文档

- [implementation-status.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/implementation-status.md)
- [current-priority-gap-list.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/current-priority-gap-list.md)
- [remaining-work-backlog.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/remaining-work-backlog.md)
- [online-collaboration-runtime-migration.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/online-collaboration-runtime-migration.md)
- [resident-hierarchical-runtime.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/resident-hierarchical-runtime.md)
- [agent-abstraction-skill-and-prompt-system.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/agent-abstraction-skill-and-prompt-system.md)
- [claude-comparison-and-system-logic.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/claude-comparison-and-system-logic.md)
- [hierarchical-online-agent-runtime-gap-list.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/hierarchical-online-agent-runtime-gap-list.md)
- [target-gap-roadmap-and-task-subtree-authority.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/target-gap-roadmap-and-task-subtree-authority.md)
