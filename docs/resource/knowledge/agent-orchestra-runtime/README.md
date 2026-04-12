# agent-orchestra-runtime 知识包

## 1. 一句话结论

这个知识包对应的是本仓库正在落地的 Python `agent_orchestra` 运行时：它不是 Claude Code 的 UI 复刻，而是在尽量复用 Claude team runtime 的前提下，已经同时落下了协调内核、bounded dynamic planning、连续 leader/superleader runtime、共享 resident coordination shell、canonical mailbox/message-pool、worker reliability/session layer，以及 2026-04-05 接上的 `ACTIVE` process reattach / durable supervisor session / reconnector / protocol bus 主线；到 2026-04-07，authority 主线也已经进一步收口：不只是 `grant / deny / reroute / escalate` 与 durable outbox/policy 已落地，self-hosting `authority_completion` gate 和共享 `authority_reactor.py` 也已进入主线，leader/superleader/teammate/session/export 共用同一 authority completion 与 resident reactor truth。当前主线状态是：四种 backend 都声明显式 capability，`GroupRuntime` 会在 launch 前做 contract/profile/backend compatibility gate，`codex_cli`、`worker_process`、`subprocess`、`tmux` 都已经能产出 `protocol_state / protocol_events / final_report`，`DefaultWorkerSupervisor` 已支持 `ACTIVE` session durable truth、lease renew、reclaim + `reattach(...)`，`ProtocolBus / RedisProtocolBus` 已进入主线，`Leader` / `SuperLeader` 使用共享 resident coordination shell 做 activation / convergence，而 team 内 live path 已经下沉到 `TeammateWorkSurface.run(...)` + per-slot `TeammateOnlineLoop` 的 host-owned slot loop，leader mailbox helper 只处理 leader 自己的 inbox；正式 team budget 只保留 `max_teammates`，旧 `max_concurrency` 输入会被显式拒绝，`leader_output_protocol` 只接受 `sequential_slices / parallel_slices`，旧 `teammate_tasks` 协议已停止接受；`WorkerRoleProfile` 已真正透传到 leader/teammate assignment、runtime request metadata 和 self-hosting bootstrap 配置，teammate code-edit assignment 也已经把 required verification set 与 `requested_command -> command` fallback mapping 收口成 authoritative `final_report.verification_results`。2026-04-09 之后，这条线的 runtime truth 又固定了一层更窄的边界：`ResidentSessionHost` 已成为 continuation read / host projection 的统一入口，`GroupRuntime` 与 `DefaultWorkerSupervisor` 的 authoritative session read/write 会优先走 host-projected truth，而不是继续让 raw persisted `AgentSession`、worker reclaim path 和 coordinator projection 各自拼装 session reality。2026-04-09 起，superleader resident live view 又进一步加深：lane delivery metadata 里的 digest/mailbox follow-up 信号、host-owned leader session projection、以及 objective `coordination / message_runtime / resident_live_view` 都开始作为 live input 被直接消费，stale `PENDING` lane snapshot 不会再压过 host-owned `WAITING_FOR_MAILBOX` resident lane，而 self-hosting packet 也会继续导出 `superleader_runtime_status`。2026-04-10 又把 task-surface governance 接进这些共享 read model：`GroupRuntime.read_resident_lane_live_views(...)` 会为每条 lane 导出结构化 `task_surface_authority` snapshot，`resident_live_view` 会继续向上汇总 `task_surface_authority_lane_ids / lane_task_surface_authority_waiting_task_ids`，而 `build_leader_revision_context_bundle(...)` 也会自动注入 `authority_notices / project_item_notices` 与对应 metadata，从而让 planning/runtime consumer 明确把 task surface 当成 governed runtime state，而不是 generic editable graph。2026-04-11 夜间，这条 continuity 面又补上了 full-context session-memory first-cut：`contracts/session_memory.py`、store-backed `agent_turn_records / tool_invocation_records / artifact_refs / session_memory_items`、`runtime/session_memory.py`、`SessionInspectSnapshot.hydration_bundles`，以及 `apply_assignment_continuity(...)` 的 hydration prompt injection 都已进入主线；worker assignment 现在不只会回写 `ConversationHead`，还会把 structured turn/artifact/memory evidence 固化下来，并在 degraded resume 场景里导出并消费 `HydrationBundle`。当前代码事实已经不应再概括成 `leader turn -> mailbox follow-up -> result drain` 的 team 主执行面；剩余主线已经收缩到把当前 activation/convergence shell 进一步推进成更彻底的 host-owned resident hierarchy、把 `WorkerSession / AgentSession / ResidentCoordinatorSession` 的 broader single-owner write boundary 再继续收口，以及 sticky provider routing、planner feedback、permission broker、postgres hardening，和更深一层的 long-lived resident worker / reconnect hardening。与 runtime truth 收口并列但更靠后的，是 bootstrap/self-hosting 的 host-owned evidence rewrite：当前 delegation validation 仍依赖 host-owned leader session、runtime-native teammate evidence、单 leader turn 与零 mailbox follow-up 这组 heuristic，因此它应继续被视为 runtime truth 稳定后的 follow-on，而不是 session/coordination 的主真相。

## 2. 这个知识包解决什么问题

这个知识包主要回答下面几类问题：

- 本仓库里的 Python runtime 现在有哪些一等抽象
- Claude Code 现有 team agent 的哪些机制已经被提炼，哪些还只是设计目标
- `PostgreSQL + Redis + OpenAI Runner` 在当前代码中的边界是什么
- 未来 agent 要继续扩展 group 级、authority 级能力时，应该从哪些文件继续进入

如果任务涉及本仓库 Python 版多 team / 多 group runtime、runner 抽象、存储/消息总线边界、Claude Code team 映射，应先读本知识包。

## 3. 范围与资料来源

- 当前实现目录：`src/agent_orchestra/`
- 设计与计划：
  - `docs/superpowers/specs/2026-04-02-agent-orchestra-group-runtime-design.md`
  - `docs/superpowers/plans/2026-04-02-agent-orchestra-group-runtime.md`
  - `docs/superpowers/specs/2026-04-03-agent-orchestra-core-coordination-api.md`
  - `docs/superpowers/plans/2026-04-03-agent-orchestra-core-coordination-phase-1.md`
  - `docs/superpowers/specs/2026-04-03-agent-orchestra-launch-backend-supervisor-design.md`
  - `docs/superpowers/plans/2026-04-03-agent-orchestra-launch-backend-supervisor.md`
  - `docs/superpowers/specs/2026-04-03-agent-orchestra-codex-cli-backend-api.md`
  - `docs/superpowers/plans/2026-04-03-agent-orchestra-codex-cli-backend.md`
  - `docs/superpowers/specs/2026-04-03-agent-orchestra-autonomous-runtime-api.md`
  - `docs/superpowers/plans/2026-04-03-agent-orchestra-autonomous-runtime.md`
  - `docs/superpowers/specs/2026-04-10-agent-orchestra-session-continuity-and-forking-design.md`
  - `docs/superpowers/specs/2026-04-11-agent-orchestra-resident-team-shell-and-attach-first-design.md`
  - `docs/superpowers/specs/2026-04-11-agent-orchestra-full-context-session-memory-and-hydration-design.md`
  - `docs/superpowers/specs/2026-04-12-agent-orchestra-session-domain-and-durable-persistence-design.md`
- 上游抽象来源：
  - `resource/knowledge/claude-code-main/agent-team.md`
  - `resource/knowledge/multi-agent-team-delivery/delivery-contracts.md`
  - `resource/knowledge/multi-agent-team-delivery/runbook-and-checklists.md`

## 4. 推荐阅读顺序

如果目的是先理解“这套 Python runtime 是从哪里抽象出来的”，建议按下面顺序阅读：

1. `implementation-status.md`
2. `current-priority-gap-list.md`
3. `current-goal-gap-checklist.md`
4. `first-batch-online-collaboration-execution-pack.md`
5. `target-gap-roadmap-and-task-subtree-authority.md`
6. `task-review-slots-and-claim-context-fusion.md`
7. `hierarchical-review-and-cross-team-knowledge-fusion.md`
8. `multi-leader-draft-review-and-revision.md`
9. `hierarchical-review-residual-gap-list.md`
10. `authority-escalation-and-scope-extension.md`
11. `authority-system-cutover-plan.md`
12. `authority-completion-gate-and-resident-reactor.md`
13. `hierarchical-online-agent-runtime-gap-list.md`
14. `active-reattach-and-protocol-bus.md`
15. `session-continuity-and-runtime-branching.md`
16. `full-context-session-memory-and-hydration.md`
17. `session-domain-and-durable-persistence.md`
18. `resident-daemon-backend-and-session-attach.md`
19. `online-collaboration-runtime-migration.md`
20. `resident-team-collaboration-and-slice-planning.md`
21. `resident-teammate-online-loop-first-wave.md`
22. `durable-teammate-loop-and-mailbox-consistency.md`
23. `leader-loop-teammate-work-surface-boundary-audit.md`
24. `host-owned-teammate-loop-cutover-audit.md`
25. `team-parallel-execution-gap.md`
26. `resident-hierarchical-runtime.md`
27. `agent-abstraction-skill-and-prompt-system.md`
28. `execution-guard-failure-patterns.md`
29. `data-contracts-and-finalization.md`
30. `worker-lifecycle-protocol-and-lease.md`
31. `runtime-fragility-decomposition-and-hardening-roadmap.md`
32. `agent-orchestra-gap-handoff.md`
33. `remaining-work-backlog.md`
34. `autonomous-runtime.md`
35. `self-hosting-bootstrap.md`
36. `claude-team-mapping.md`
37. `claude-comparison-and-system-logic.md`
38. `agent-orchestra-framework.md`
39. `parallel-team-coordination.md`
40. `architecture.md`
41. `resident-team-shell-and-attach-first.md`

如果目的是直接把当前系统当作可操作的 session/attach CLI 使用，再补读：

1. `resident-team-shell-and-attach-first.md`
2. `agent-orchestra-cli-operations-and-agent-handoff.md`

如果目的是继续下钻当前代码，可按下面顺序进入源码入口：

1. `src/agent_orchestra/contracts/runner.py`
2. `src/agent_orchestra/contracts/objective.py`
3. `src/agent_orchestra/contracts/blackboard.py`
4. `src/agent_orchestra/contracts/execution.py`
5. `src/agent_orchestra/planning/template.py`
6. `src/agent_orchestra/planning/io.py`
7. `src/agent_orchestra/planning/template_planner.py`
补充：如果任务涉及 bounded dynamic planning、seed-based self-hosting template，继续读 `src/agent_orchestra/planning/dynamic_superleader.py`
8. `src/agent_orchestra/runtime/backends/base.py`
9. `src/agent_orchestra/runtime/backends/in_process.py`
10. `src/agent_orchestra/runtime/backends/subprocess_backend.py`
11. `src/agent_orchestra/runtime/backends/tmux_backend.py`
12. `src/agent_orchestra/runtime/backends/codex_cli_backend.py`
13. `src/agent_orchestra/runtime/worker_process.py`
14. `src/agent_orchestra/runtime/worker_supervisor.py`
15. `src/agent_orchestra/runtime/bootstrap_round.py`
16. `src/agent_orchestra/runtime/leader_output_protocol.py`
17. `src/agent_orchestra/contracts/delivery.py`
18. `src/agent_orchestra/runtime/evaluator.py`
补充：如果任务涉及 mailbox / protocol mainline，先读 `src/agent_orchestra/runtime/protocol_bridge.py`，再把 `src/agent_orchestra/runtime/mailbox_bridge.py` 视为 legacy in-memory reference
19. `src/agent_orchestra/runtime/mailbox_bridge.py`
20. `src/agent_orchestra/runtime/leader_loop.py`
21. `src/agent_orchestra/runtime/teammate_work_surface.py`
22. `src/agent_orchestra/runtime/superleader.py`
23. `src/agent_orchestra/contracts/team.py`
24. `src/agent_orchestra/contracts/task.py`
25. `src/agent_orchestra/contracts/events.py`
26. `src/agent_orchestra/storage/base.py`
27. `src/agent_orchestra/runtime/team_runtime.py`
28. `src/agent_orchestra/runtime/group_runtime.py`
29. `src/agent_orchestra/runtime/reducer.py`
30. `src/agent_orchestra/storage/postgres/models.py`
31. `src/agent_orchestra/storage/postgres/store.py`
32. `src/agent_orchestra/bus/redis_bus.py`
33. `src/agent_orchestra/runtime/protocol_bridge.py`
34. `src/agent_orchestra/runners/openai/adapter.py`
35. `src/agent_orchestra/runtime/orchestrator.py`
36. `src/agent_orchestra/cli/main.py`

## 5. 文件地图

- `claude-team-mapping.md`
  - Claude Code team agent 到 Python runtime 抽象的映射
  - 哪些点已经落地，哪些点仍是后续升级目标
- `claude-comparison-and-system-logic.md`
  - 逐项比较 Claude team agent 与 Agent Orchestra 当前能力的异同
  - 同时给出 Agent Orchestra 当前系统逻辑的总表，适合作为“先理解现在这套系统到底怎么运转”的入口
- `first-batch-online-collaboration-execution-pack.md`
  - 把当前最值得先交给 Agent Orchestra 的首批 4 个主线任务收敛成一份可直接执行的 handoff
  - 明确执行顺序、不要重做的旧项、最低验证，以及“先稳定 runtime truth，再改写 bootstrap/self-hosting evidence gate”的 follow-on 边界
- `session-continuity-and-runtime-branching.md`
  - 固化为什么 Agent Orchestra 不应把 Claude 式 transcript resume 直接升成 runtime 唯一真相，而应显式拆成 `WorkSession / RuntimeGeneration / ConversationHead / runtime truth`
  - 明确 `exact_wake / warm_resume / fork_session / new_session` 这四种正式连续性语义，以及它们和 `ResidentSessionHost`、`WorkerSession`、assignment-level `previous_response_id` 的边界关系
  - 现已补入 continuity contracts/store surface、`SessionContinuityService` inspect/list/bundle read model、`GroupRuntime` resume/exact-wake bridge、真实 store-backed `session list/inspect/new/resume/fork` CLI，以及后续 exact-wake hardening 边界
  - 适合作为“要给系统补用户可见会话层，但又不能破坏现有 host-owned runtime truth”时的直接入口
- `full-context-session-memory-and-hydration.md`
  - 固化为什么 continuity first-cut 之后，系统仍然缺少 full-context 自动保存层，以及为什么这层不应退化成 Claude 式 transcript-first ownership
  - 明确 `turn ledger / artifact ledger / semantic memory / hydration bundle` 四层模型、推荐新增的数据契约、authoritative capture 点，以及 `attach / exact_wake / warm_resume / fork` 与 hydration 的职责边界
  - 现已补入 first-cut 与 role-scoped 落地状态：worker-led turn/artifact/memory capture、leader/teammate/superleader/session-host 的 turn/tool/artifact 接线、`inspect_session(...).hydration_bundles`，以及 `apply_assignment_continuity(...)` 的 hydration injection
  - 适合作为“要把 Agent Orchestra 提升到更接近全量上下文自动保存，但又保留 runtime-first truth”时的直接入口
- `session-domain-and-durable-persistence.md`
  - 固化为什么当前系统的问题已经不再是“有没有 session 持久化”，而是“session durable truth、写入 owner、事务边界和 attach-first 产品语义是否真正收口”
  - 明确长期正确路线应是把 session 提升成独立 domain：以 PostgreSQL 作为唯一生产 durable truth，以单一 `SessionDomainService` 收口 `WorkSession / RuntimeGeneration / ConversationHead / ResidentTeamShell / SessionEvent`
  - 现已补入 first-cut 落地状态：`SessionDomainService` 已进入代码主线，`GroupRuntime` 的 user-visible session methods 与 worker continuity bridge 已经改为统一委托，CLI `session` 默认后端已切到 `postgres`，`SessionContinuityService` 的 `new_session / warm_resume / fork_session` 也已切到 `SessionTransactionStoreCommit`
  - 同时强调 `AgentSession / WorkerSession / ResidentCoordinatorSession / DeliveryState` 仍应保持 runtime-owned truth，而 `HydrationBundle` 继续只作为 continuity read model
  - 适合作为“要彻底重构 session persistence、并避免继续在 CLI/default/in-memory 或 scattered write path 上累积技术债”时的直接入口
- `resident-daemon-backend-and-session-attach.md`
  - 固化为什么 AO 的长期正确形态不应继续是前台 bounded orchestration run，而应变成“常驻 daemon backend + thin CLI client + session attach + slot/incarnation supervision”
  - 明确 daemon 才是 live runtime owner、session 是用户主操作面、异常失败时被替换的是 incarnation 而不是 slot 身份，以及为什么这条线比单纯 watchdog patch 更完整
  - 适合作为“要把 AO 做成类似 codex/claude 的本地常驻后端，并用 session attach 交互”的直接入口
- `resident-team-shell-and-attach-first.md`
  - 固化为什么在 continuity first-cut 之后，系统的下一目标不应继续围绕 `exact_wake` 做产品设计，而应把可 attach 的单 team 常驻 shell 提升成默认 operating surface
  - 明确 `attach -> wake -> recover -> warm_resume` 的优先顺序、`ResidentTeamShell` 在 continuity 层与 host-owned runtime truth 之间的位置，以及“先做单 team resident shell、再 lift 到 superleader”的实施顺序
  - 适合作为“要把 Agent Orchestra 做成更接近 Claude 的常驻协作体验，但又不想放弃 AO 的层级治理与 host-owned truth”时的直接入口
- `agent-orchestra-cli-operations-and-agent-handoff.md`
  - 面向操作者和其他 agent 的实操 runbook，专门说明当前 CLI 应如何启动、持久化、inspect、attach、wake、fork，以及应保存哪些 continuity ID
  - 明确当前 CLI 是 store-backed session control plane，而不是完整交互式 shell reattach；同时把 `postgres`、`tmux`、approval/reject、以及 `group/team` thin wrapper 边界写成可直接执行的规则
- `online-collaboration-runtime-migration.md`
  - 专门固化“把系统从 `Leader` 产生活、runtime 派发执行，迁移成长期在线 agent 协作运行时”的 gap list 与实现方案
  - 明确 `Agent Coordination Layer / ResidentSessionHost / TransportAdapter` 三层模型，以及建议的投喂包与验证 gate
- `authority-escalation-and-scope-extension.md`
  - 固化 teammate 在执行中发现真实 blocker 后，如何向 leader/superleader 申请扩展编辑范围或更高 authority，而不是直接折叠成 `blocked -> failed`
  - 记录这条线已经落地成完整主语义：`AuthorityRequest`、`WAITING_FOR_AUTHORITY`、`AuthorityDecision`、`grant / deny / reroute / escalate`、protected boundary policy、leader/superleader resident authority decision，以及 self-hosting closure evidence
  - 现在还包含 task-surface authority intent 如何复用同一条 request/decision/outbox 流程，以及 resident live view / revision bundle 如何继续暴露这条 authority truth，而不是让 cross-subtree / cross-team / protected-field mutation 停在 classifier 层
  - 同时明确它与 `task-subtree-authority`、`permission-broker`、Claude team agent permission flow 的边界关系
- `authority-system-cutover-plan.md`
  - 归档 authority 从 first-cut request/wait 收口到完整系统主语义时，contracts、runtime、mailbox、session、store、leader/superleader、self-hosting 一起修改过的点
  - 适合作为“authority 主线是怎么被系统级切进去的，以及后续哪些只属于 broader cleanup”的直接入口
- `authority-completion-gate-and-resident-reactor.md`
  - 专门固化 authority completion gate 与 shared resident authority reactor 如何落地到 leader/superleader/teammate/session/self-hosting
  - 适合作为“authority 这条线当前已经补到哪、closure semantics 是什么、接下来还剩哪些 broader cleanup”的直接入口
- `resident-team-collaboration-and-slice-planning.md`
  - 固化最新 team 级设计决议：取消 `max_concurrency` 作为正式 team budget、把 leader 输出升级成 `sequential / parallel slices`，以及把 team runtime 继续推进成真正的 resident collaboration 主语义
  - 适合作为“如何把当前 bounded team runtime 收敛到 Claude 式长期在线 team”时的直接入口
- `resident-teammate-online-loop-first-wave.md`
  - 聚焦 `resident-teammate-online-loop` 这个实现切片的当前 leader-driven 假设、first-wave 最小实现路径、证明测试与 merge contention 热点
  - 适合作为“先把 teammate 从 leader turn execution shell 推成真正在线 loop”时的直接入口
- `durable-teammate-loop-and-mailbox-consistency.md`
  - 固化 durable teammate slot session、authoritative mailbox consumer cursor、canonical `task.directive|receipt|result`，以及 teammate-owned verification loop 进入 assignment execution contract 的主线设计与已落地实现
  - 明确当前已经接上的代码入口、兼容语义、verification evidence 收口方式，以及后续仍待 harden 的边界
  - 适合作为“继续推进 protocol-native teammate online runtime”时的直接入口
- `leader-loop-teammate-work-surface-boundary-audit.md`
  - 专门审计 `leader_loop.py` 里仍残留的 teammate mailbox/task/session truth，与仍应保留的 leader convergence 逻辑
  - 明确如果引入 `teammate_work_surface`，最小的代码切分边界和集成顺序应是什么
- `host-owned-teammate-loop-cutover-audit.md`
  - 专门盘点为了把 teammate recipient cursor/session 真相完全并入 work surface / host、并把 leader-led activation 推向真正 host-owned long-lived teammate loop，还需要动哪些模块、接口、状态和测试
  - 适合作为“下一轮继续做 host-owned teammate loop / leader activation cutover”时的直接入口
- `src/agent_orchestra/runtime/teammate_work_surface.py`
  - 当前已经成为真实代码入口：负责 teammate-owned 的 slot identity、direct mailbox cursor、directed/autonomous claim、初始 assignment reserve、slot session truth 写回，以及 code-edit assignment 的 verification contract 注入
  - 适合作为“继续把 leader 收缩成 activation center + convergence center”时的直接源码入口
- `implementation-status.md`
  - 截至当前仓库状态，系统已经实现到哪一层、还缺哪一层
  - 适合作为“先看现状再决定下一步”的入口文档
- `execution-guard-failure-patterns.md`
  - 记录 raw backend result 与 authoritative `WorkerRecord` 状态不一致时的排障链路
  - 当前已固化 2026-04-04 self-hosting round 中的 `path_violation` 根因：snapshot ignore 不对称、`resource -> docs/resource` 符号链接路径失配、`__pycache__` 噪音
- `data-contracts-and-finalization.md`
  - 固化 `worker -> leader loop -> superleader -> self-hosting round` 这条链上的分层数据契约问题
  - 明确区分 `Domain Live Model / Store Snapshot Model / Transport DTO / Artifact Report Model`
  - 适合作为“为什么 instruction packet 能落盘、但 final round report 仍会脆弱”的直接入口
- `worker-lifecycle-protocol-and-lease.md`
  - 固化“worker 应按生命周期协议、租约模型和 final-report verification evidence 返回结果，而不是主要靠 supervisor 猜测 transport 症状”这条新设计
  - 明确区分 `WorkerExecutionContract` 的执行/verification contract、`WorkerLease` 的活性约束、`WorkerFinalReport` 的终态返回，以及 `VerificationCommandResult.requested_command -> command` 的 fallback 映射
  - 现已补入统一 failure attribution 语义：`backend_cancel_invoked`、`supervisor_timeout_path`、`last_protocol_progress_at`、`last_message_exists_at_failure`、`termination_signal(_name)` 与 `failure_tags`
  - 适合作为“为什么 timeout 不是系统主语义、而只是协议下安全阀”的直接入口
- `runtime-fragility-decomposition-and-hardening-roadmap.md`
  - 专门把“AO 为什么看起来经常断”拆成 transport 选型、provider/network/plugin-sync 波动、continuity truth 与 live truth 混淆、以及 resident runtime residual hardening 这 4 层
  - 明确旧 execution guard 假失败已基本退出主矛盾，真正的根治路线应是 `full resident transport + sticky provider routing + honest run/session finalization + deeper resident reconnect hardening`
  - 适合作为“要判断当前脆弱性的主因是什么、以及彻底解决时该先改哪一层”的直接入口
- `current-priority-gap-list.md`
  - 把当前知识层优先级、bootstrap `gap_id`、以及下一轮推荐投喂组合对齐到一张清单上
  - 明确 single-owner session truth 是当前 backlog 主线，bootstrap/self-hosting evidence rewrite 仍是 follow-on
  - 适合作为“现在该让 Agent Orchestra 继续做什么”的直接入口
- `current-goal-gap-checklist.md`
  - 把“当前系统和目标状态之间到底还差什么”收敛成一份权威 gap 清单
  - 明确哪些已经完成且不应重开为主 gap，哪些仍是核心差距，适合作为“现在离目标还有多远”的直接入口
- `hierarchical-online-agent-runtime-gap-list.md`
  - 把 `online-collaboration-runtime` 正式拆成 4 段连续主线：`agent-contract-convergence`、`team-primary-semantics-switch`、`superleader-isomorphic-runtime`、`cross-team-collaboration-layer`
  - 适合作为“如何把这条主线分阶段交给 Agent Orchestra 实现”的直接入口
- `target-gap-roadmap-and-task-subtree-authority.md`
  - 把当前离预定目标状态的真实剩余 gap 收敛成统一路线图
  - 现已进一步补入 `authority-escalation-and-scope-extension` 与 `task-subtree-authority` 这两条治理主线，分别覆盖 teammate 动态扩权链和 task list 结构权限治理
  - 适合作为“现在离预定目标还差什么、以及下一轮优先做什么”的直接入口
- `task-review-slots-and-claim-context-fusion.md`
  - 固化如何把 task surface 从纯 activation/ownership 面板推进成 claim 前知识融合面
  - 明确 `TaskReviewSlot / TaskReviewRevision / TaskReviewDigest`、每 agent 唯一 review slot、update-only 对外语义、experience context，以及它与 `task-subtree-authority / TeamBlackboard / cross-team digest` 的边界关系
- `hierarchical-review-and-cross-team-knowledge-fusion.md`
  - 固化从 teammate review、leader team synthesis、leader cross-team review 到 superleader final synthesis 的分层 review 主语义
  - 现已进入 first-cut 代码主线：contracts、in-memory/PostgreSQL store、`GroupRuntime` API、leader convergence helper、superleader finalize helper 都已落地
  - 明确 `TaskReviewSlot / TeamPositionReview / CrossTeamLeaderReview / SuperLeaderSynthesis`、`task item` 与 `project item` 的区别，以及多 team 如何真正形成 leader-owned 的知识融合而不是消息洪水
- `multi-leader-draft-review-and-revision.md`
  - 固化多 team planning 层的新正式主语义：`ObjectiveBrief -> LeaderDraftPlan -> all-to-all LeaderPeerReview -> SuperLeaderGlobalReview -> LeaderRevisionContextBundle -> LeaderRevisedPlan -> ActivationGate`
  - 明确 peer review 默认对所有 leader 可见、summary-first 自动注入 revision 上下文、authority/project-item notices 如何由 runtime 自动补齐，以及 revised plan 不做逐条 disposition 与 `project item / activation gate / resource lease` 的边界关系
- `hierarchical-review-residual-gap-list.md`
  - 专门收口 hierarchical review 进入 first-cut 主线之后，真正还没完成的 residual gap
  - 适合作为“下一步应该继续补什么、执行顺序是什么、哪些不要重复做”的直接入口
- `team-parallel-execution-gap.md`
  - 专门比较 Claude team agent 与 Agent Orchestra 在 team 内外并行能力上的差异
  - 记录 `team-parallel-execution` 如何从开放 gap 进入主线，以及接下来剩余的 deeper durable/reconnect 差异
- `active-reattach-and-protocol-bus.md`
  - 固化 `ACTIVE` mid-turn process reattach、durable-supervisor-sessions、reconnector 与 protocol bus 的主线实现边界
  - 明确 PostgreSQL durable truth、Redis protocol bus、supervisor lease、transport locator、backend `reattach(...)` 与 self-hosting evidence gate 的职责分工
- `resident-hierarchical-runtime.md`
  - 规划如何把 `SuperLeader` 和 `Leader` 统一提升成 Claude 风格的常驻协调实体
  - 明确 resident coordination kernel、role adapter、以及 transport 与 coordination 语义的分层边界
- `agent-abstraction-skill-and-prompt-system.md`
  - 规划如何把 `Agent` 抽象成统一自治执行体，并把 `RolePolicy / SkillSet / PromptProfile` 分层
  - 当前前两层已经落地，第三层已完成 durable idle session / reconnect v1，后续重点转向更深的 reconnect 与 richer abstraction
- `remaining-work-backlog.md`
  - 当前系统剩余任务的扩展版清单
  - 现已同步区分“旧 gap 已关闭的 first-cut”与“真正还没完成的 residual hardening”，并显式标记 bootstrap/self-hosting evidence rewrite 的后置依赖
  - 适合作为“下一轮该继续做什么”的直接入口
- `agent-orchestra-gap-handoff.md`
  - 专门给 Agent Orchestra 自举轮次使用的 residual-gap relay 执行包
  - 把“旧 gap 已关闭 / 新 residual gap 接力表”、对应知识文档、以及可直接投喂文本集中到一处
  - 明确 `self-hosting-evidence-rewrite` 仍是 runtime truth 稳定后的 follow-on
  - 适合作为“把哪些 gap 连同哪些知识一起交给 Agent Orchestra”的直接入口
- `autonomous-runtime.md`
  - 新增的 delivery state、mailbox、permission、leader loop 和 superleader 运行层
  - 适合作为“继续做自治 runtime 扩展”时的直接入口
- `self-hosting-bootstrap.md`
  - 如何从知识库优先级生成下一轮 self-hosting objective 与 instruction packet
  - 已包含 explicit workstream mode 与 `use_dynamic_planning=True` 的 seed-based mode
  - 适合作为“让系统继续告诉自己下一轮做什么”的入口
- `agent-orchestra-framework.md`
  - `SuperLeader -> Leader -> Teammate` 的层级框架
  - `Spec DAG`、全局统一 task list、层级 Blackboard、节点权限、planner / launch / supervisor / reducer 的职责边界
- `parallel-team-coordination.md`
  - 多 team / 多 lane 的推荐并行关系、硬依赖/软依赖、Blackboard 信息流与 DAG/task/interface contract 的落点
  - 适合作为“如何让多个 lane 并行推进但不混乱”的直接入口
- `architecture.md`
  - 当前 Python runtime 的模块结构、第一阶段协调内核、第一版本地执行层、连续 leader/superleader 协调层、基础设施边界与 v1 限制
  - 现已包含 `TemplatePlanner`、`DynamicSuperLeaderPlanner`、template I/O、`BackendRegistry`、四种本地 backend、`WorkerExecutionPolicy`、`WorkerProviderRoute`、`WorkerSession`、`DefaultWorkerSupervisor.run_assignment_with_policy(...)`、`bootstrap_round`、`leader_output_protocol`、`DeliveryState` / `DefaultDeliveryEvaluator`、canonical `runtime.protocol_bridge.*MailboxBridge`、covered-slice PostgreSQL CRUD、`LeaderLoopSupervisor` 和 `SuperLeaderRuntime`

## 6. 适用范围

适用于以下任务：

- 继续扩展 `agent_orchestra` Python 库
- 设计 group / team / task list / blackboard / reducer / authority 语义
- 接入 PostgreSQL、Redis、OpenAI provider
- 把 Claude Code 的 team 机制继续升级成 group-ready、多 team 协作内核

## 7. 维护要求

维护本知识包时，应保持：

- 包内 `README.md` 的阅读顺序和文件地图同步更新
- `claude-team-mapping.md` 中的“已落地 / 未落地”状态与真实代码同步
- `architecture.md` 和 `agent-orchestra-framework.md` 中的框架边界、关键入口和 v1 限制保持可追溯
- 具体 backend / supervisor / worker harness 的已实现状态必须与源码同步，不得继续停留在“只有抽象接口”的旧表述
- worker reliability / execution guard 的 contract、retry/resume/fallback/escalation/session 语义和 worker record metadata 必须与真实实现同步
- dynamic planner / self-hosting template mode 的边界必须与 `src/agent_orchestra/planning/dynamic_superleader.py` 和 `src/agent_orchestra/self_hosting/bootstrap.py` 保持同步
- `data-contracts-and-finalization.md` 中定义的 live/store/transport/artifact 边界、以及 finalization/export 语义，必须和真实导出链保持同步
- `worker-lifecycle-protocol-and-lease.md` 中定义的 lifecycle/lease/final-report/role-profile 语义，必须和 `contracts/execution.py`、`runtime/worker_supervisor.py`、`runtime/leader_loop.py` 的真实行为保持同步
- 如果新增专题文档，回填到本文件的文件地图与阅读顺序
