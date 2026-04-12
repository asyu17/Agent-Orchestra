# agent_orchestra 首批在线协作主线执行包

## 1. 一句话结论

这份执行包把当前离目标态最近、也最适合先交给 `Agent Orchestra` 的首批 4 个主线任务收敛成一份可直接运行的 handoff：先完成 `team-primary-semantics-switch`，再收敛 `coordination-transaction-and-session-truth-convergence`，接着补 `task-surface-authority-contract`，最后只推进 `superleader-isomorphic-runtime` 的第一段，从而把系统继续从“leader turn 驱动派发”推向“长期在线 agent 持续协作”。

## 2. 范围与资料来源

- 当前权威 gap 视图：
  - [current-goal-gap-checklist.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/current-goal-gap-checklist.md)
  - [current-priority-gap-list.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/current-priority-gap-list.md)
  - [remaining-work-backlog.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/remaining-work-backlog.md)
- 主线迁移与层级设计：
  - [online-collaboration-runtime-migration.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/online-collaboration-runtime-migration.md)
  - [resident-hierarchical-runtime.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/resident-hierarchical-runtime.md)
  - [resident-team-collaboration-and-slice-planning.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/resident-team-collaboration-and-slice-planning.md)
- 具体 residual gap 审计：
  - [agent-orchestra-gap-handoff.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/agent-orchestra-gap-handoff.md)
  - [host-owned-teammate-loop-cutover-audit.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/host-owned-teammate-loop-cutover-audit.md)
  - [target-gap-roadmap-and-task-subtree-authority.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/target-gap-roadmap-and-task-subtree-authority.md)
- 当前代码入口：
  - [leader_loop.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py)
  - [teammate_work_surface.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/teammate_work_surface.py)
  - [session_host.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/session_host.py)
  - [group_runtime.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/group_runtime.py)
  - [superleader.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/superleader.py)
  - [bootstrap.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/self_hosting/bootstrap.py)

## 3. 为什么先做这 4 项

这 4 项不是随意拼出来的，它们刚好对应当前目标态最短的闭环切换链：

- `team-primary-semantics-switch` 决定 team 内主语义是不是已经从 leader assignment shell 切到 resident collaboration。
- `coordination-transaction-and-session-truth-convergence` 决定 mailbox / task / blackboard / session / delivery 的真相能不能收敛到更少的崩溃窗口与更单一的 owner。
- `task-surface-authority-contract` 决定 task list 能不能从“派工清单”真正升级成“长期协作 surface”，而不会在结构权限和状态权限上失控。
- `superleader-isomorphic-runtime` 第一段决定上层能不能开始脱离“外层 while-loop 调度器”，朝着真正 `leader-of-leaders` 的 resident runtime 走。

判断：

- 这批任务完成后，系统不会立刻达到最终目标。
- 但会形成一个更自洽的主干：team 内长期在线协作、session/coordination 真相收口、task surface 治理进入契约、superleader 开始同构化。
- 这比继续优先做 `sticky-provider-routing`、`planner-feedback`、`postgres-persistence` 更接近当前用户目标。

## 4. 首批任务定义

### 4.1 `team-primary-semantics-switch`

目标：

- 继续把 team 主执行面收口到 `mailbox + task list + TeamBlackboard`。
- 让 `Leader` 更像 activation center + convergence center。
- 让 `Teammate` 更像长期在线 agent，而不是 leader turn 的附属 assignment shell。

重点修改点：

- [leader_loop.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py)
- [teammate_work_surface.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/teammate_work_surface.py)
- [teammate_online_loop.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/teammate_online_loop.py)
- [session_host.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/session_host.py)

最低完成标准：

- `leader_loop` 不再保留第二套 teammate execution truth。
- teammate 的 mailbox/task polling、claim、result publish、wake path 进一步向 teammate-owned / host-owned surface 收缩。
- `task` 在 team 内更明确只承担 activation/contract 语义，而不是成为默认求解面。

### 4.2 `coordination-transaction-and-session-truth-convergence`

目标：

- 把 `task / mailbox / blackboard / delivery / cursor / session` 的写回继续推进到更强的一致性边界。
- 把 `WorkerSession / AgentSession / ResidentCoordinatorSession` 继续收敛到更单一的主真相。

重点修改点：

- [group_runtime.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/group_runtime.py)
- [session_host.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/session_host.py)
- [storage/base.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/storage/base.py)
- [storage/postgres/store.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/storage/postgres/store.py)
- [worker_supervisor.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/worker_supervisor.py)

最低完成标准：

- receipt/result/authority/mailbox consume 至少先共享 durable outbox 与 authoritative persisted snapshot boundary，且后续不再需要靠第二套 commit family 才能补齐 session/blackboard truth。
- 2026-04-10 起，这条最低标准又补上了 worker-session 这一层：coordination commit 会同步推进 worker-session snapshot，PostgreSQL 同事务提交，其他 store 立即补 fallback save，因此 stale `ACTIVE` worker session / stale mailbox cursor 已不再是 first-cut coordination family 的主要 skew。
- crash/reconnect 后 session continuation 的 truth source 更少、更清晰。
- self-hosting 与 runtime 导出的 session/coordination 事实不再明显分裂。
- bootstrap/self-hosting evidence rewrite 仍视为后续：当前 `_delegation_validation_metadata(...)` 还是依赖 `host_owned_leader_session + teammate_execution_evidence + leader_turn_count == 1 + mailbox_followup_turns_used == 0` 这组 gate，因此这条 lane 先稳住 runtime truth，再改写 evidence。

### 4.3 `task-surface-authority-contract`

目标：

- 正式定义 task item 的状态权限、结构权限、只读边界、subtree ownership、以及 escalation 规则。
- 让 teammate 在自己的 task subtree 内能安全增减 task item，而不会越权改上游结构。

重点修改点：

- [contracts/task.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/contracts/task.py)
- [group_runtime.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/group_runtime.py)
- [authority_reactor.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/authority_reactor.py)
- [leader_loop.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py)
- [superleader.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/superleader.py)

最低完成标准：

- `derived_from / reason / superseded / merged_into / not_needed` 这类结构语义进入 runtime 可消费主线。
- teammate 的局部结构修改与 leader/superleader 的上层治理边界分清。
- task surface authority 不再只停留在知识文档层。

2026-04-09 状态更新：

- `TaskCard` / `GroupRuntime` 已补入 first-cut task-surface authority validator：runtime 现在会区分 local status authority 与 local structure authority，并沿 provenance parent 强制 teammate subtree ownership。
- 这条线又进一步收口成共享 authority view：`TaskCard.surface_authority_view()` 与 `GroupRuntime.read_task_surface_authority_view(...)` 现在会直接导出 ancestor lineage、subtree root/lineage 与 protected field surface，而 `classify_task_*authority(...)` 会统一返回 `local_allow / escalation_needed / forbidden`，避免 caller 再各自重推规则。
- `submit_task(...) / update_task_status(...) / commit_task_surface_mutation(...)` 已统一要求 teammate direct write 带上 `reason + derived_from`，并禁止 direct cancel 绕过 non-destructive mutation API。
- authority reroute 现在也会补齐 mutation/replacement reason，并复用同一条 task-surface gate。
- `commit_task_surface_mutation(...)` 现在会把 allowed local subtree mutation 留在现有 runtime 直写路径，但会把 cross-subtree / cross-team mutation 自动转成 authority request；新增的 `commit_task_protected_field_write(...)` 也已接上，并把 teammate 对 protected task fields 的改动送进同一条 leader/superleader authority reactor 流程。
- 2026-04-10 起，这条 governed task-surface truth 又被继续投影到共享 read model：`read_resident_lane_live_views(...)` 会导出 per-lane `task_surface_authority` snapshot，而 `build_leader_revision_context_bundle(...)` 会自动补齐 `authority_notices / project_item_notices` 与 structured metadata。这样 review/project-item 仍是 advisory input，但 superleader / planning consumer 已不再把 task surface 当 generic editable graph。
- 这意味着本项已不再停留在“只有知识文档里的目标描述”；后续剩余工作应理解成 richer policy hardening，而不是从 0 到 1 的 runtime gate。

### 4.4 `superleader-isomorphic-runtime`

这一轮只做第一段，不扩散到完整 cross-team collaboration layer。

目标：

- 让 `SuperLeader` 更接近长期在线 `leader-of-leaders`。
- 开始更明确地消费 lane digest、lane mailbox、objective blackboard，而不是继续主要像 scheduler。

重点修改点：

- [superleader.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/superleader.py)
- [session_host.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/session_host.py)
- [group_runtime.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/group_runtime.py)
- [bootstrap.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/self_hosting/bootstrap.py)

最低完成标准：

- `SuperLeader` 明显少一些“拉起一次 leader loop 看结果”的调度器味道。
- 上层 cycle 对 digest / objective live metadata / lane session graph 的消费更强。
- 这一轮不要把 scope 扩散到完整 circle/subscription overlay。

2026-04-09 状态更新：

- superleader resident live view 已经不再只 join lane delivery snapshot + objective shared digest；它还会把 lane-level `pending_shared_digest_count` / mailbox follow-up metadata，以及 host-owned leader session projection 一并读成上层 live input
- stale `PENDING` lane snapshot 现在会让位给 host-owned `WAITING_FOR_MAILBOX` leader session，因此 resident lane 不会被错误 relaunch
- 即使 objective subscription 暂时没有 shared digest，只要 lane delivery metadata 仍显示 digest/mailbox pressure，cycle 也会继续进入 `WAITING_FOR_MAILBOX`
- objective metadata 的 `resident_live_view` 已扩到 `lane_digest_counts / lane_mailbox_followup_turns / lane_live_inputs / objective_message_runtime / objective_coordination`，self-hosting bootstrap 也会把这些字段继续导出成 `superleader_runtime_status`
- 2026-04-10 起，这条导出线又多了一层明确 contract：`resident_live_view` 还会显式导出 `lane_truth_sources`、`runtime_native_lane_ids / fallback_lane_ids`、以及 `primary_*` lane/session 集合；self-hosting packet/render 会优先消费这些 primary resident fields，而不是把 fallback scheduler bookkeeping 当 primary signal
- 同一天，resident live view 还会继续导出 `task_surface_authority` 相关 lane/task 聚合，因此上层 coordination 至少已经能看见“哪条 lane 正在等待 governed task-surface authority”，而不是只看 digest/mailbox/live session 三类信号

## 5. 执行顺序与依赖

建议按下面顺序推进，不要无约束并行扩散：

1. `team-primary-semantics-switch`
2. `coordination-transaction-and-session-truth-convergence`
3. `task-surface-authority-contract`
4. `superleader-isomorphic-runtime`

依赖判断：

- 第 2 项依赖第 1 项，因为 team 主语义不先切稳，transaction/session truth 会继续围着旧 activation shell 打补丁。
- 第 3 项依赖前两项，因为 task surface authority 只有在 task 已经被当成 activation surface、而 coordination/session truth 也更稳定时，契约才不会马上漂移。
- 第 4 项只做第一段，并依赖第 1 项与第 2 项；否则 superleader 仍只能消费一个还没切稳的下层 runtime。
- bootstrap/self-hosting evidence rewrite 不在这 4 项的并行实现面上；它应等待第 2 项先把 single-owner session truth 与 coordination transaction 切稳。

## 6. 不要重复做的事

这轮必须显式避免重开下面这些已进入主线的旧 gap：

- 不要重做 `team-parallel-execution` 的 baseline。
- 不要重做 `durable-supervisor-sessions`、`reconnector`、`protocol-bus` 的 base capability。
- 不要把 `authority-escalation-and-scope-extension` 当作从 0 到 1 未完成。
- 不要回到 `max_concurrency` 或扁平 `teammate_tasks` 的旧协议。
- 不要把 `leader_consumed_mailbox` 回退成主要 completion evidence。

## 7. 建议主文件与最低验证

建议主文件：

- [leader_loop.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py)
- [teammate_work_surface.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/teammate_work_surface.py)
- [teammate_online_loop.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/teammate_online_loop.py)
- [session_host.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/session_host.py)
- [group_runtime.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/group_runtime.py)
- [superleader.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/superleader.py)
- [contracts/task.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/contracts/task.py)
- [worker_supervisor.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/worker_supervisor.py)
- [storage/postgres/store.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/storage/postgres/store.py)
- [bootstrap.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/self_hosting/bootstrap.py)

最低验证命令：

- `uv run pytest tests/test_teammate_work_surface.py tests/test_teammate_online_loop.py tests/test_leader_loop.py tests/test_session_host.py tests/test_superleader_runtime.py -q`
- `uv run pytest tests/test_worker_supervisor_protocol.py tests/test_runtime.py tests/test_self_hosting_round.py -q`
- `uv run pytest -q`

### 7.1 Bootstrap Scope Rule

2026-04-09 起，这 4 个 gap 不只是知识文档里的 handoff 名称，它们已经进入 self-hosting bootstrap 的正式 gap catalog。

这意味着：

- 这 4 个 gap 必须有正式 `_GAP_DEFINITIONS`
- 每个定义都必须同时带上代码热点、知识文档 scope、最低验证命令
- `SelfHostingGap.metadata` 必须把这些默认值透传到 `LeaderTaskCard`
- leader prompt 必须能直接看到 lane-level defaults
- descendant team task 在 slice 未显式声明时，必须自动继承 lane-owned knowledge scope

但 default `verification_commands` 的继承边界不能无限外扩：

- 当前只对这 4 个 first-batch gap 开启
- 不要把它回灌到 `team-parallel-execution`、`leader-teammate-delegation-validation` 这类 validation/self-hosting lane
- 否则 fake validation teammate 会被误要求跑真实 gap-level 验证，反而把 validation lane 自己打成 blocked

补充边界：

- 这条 bootstrap catalog 接线不等于 host-owned evidence rewrite 已完成；当前 delegation validation 仍使用 `host_owned_leader_session + teammate_execution_evidence + leader_turn_count == 1 + mailbox_followup_turns_used == 0` 的 heuristic
- 因而 self-hosting/validation lane 的后续改写，必须在 runtime truth 稳定之后再做统一切换

## 8. 建议优先级

1. `team-primary-semantics-switch`
2. `coordination-transaction-and-session-truth-convergence`
3. `task-surface-authority-contract`
4. `superleader-isomorphic-runtime`

## 9. 可直接投喂给 Agent Orchestra 的执行文本

下面这段可以直接作为本轮投喂文本：

```md
你这轮不是去重做已经进主线的旧 capability，而是执行在线协作主线的首批切换包。

先读：

1. resource/knowledge/README.md
2. resource/knowledge/agent-orchestra-runtime/README.md
3. resource/knowledge/agent-orchestra-runtime/current-goal-gap-checklist.md
4. resource/knowledge/agent-orchestra-runtime/current-priority-gap-list.md
5. resource/knowledge/agent-orchestra-runtime/online-collaboration-runtime-migration.md
6. resource/knowledge/agent-orchestra-runtime/resident-team-collaboration-and-slice-planning.md
7. resource/knowledge/agent-orchestra-runtime/agent-orchestra-gap-handoff.md
8. resource/knowledge/agent-orchestra-runtime/first-batch-online-collaboration-execution-pack.md

这轮只做首批 4 项，并按顺序推进：

1. team-primary-semantics-switch
2. coordination-transaction-and-session-truth-convergence
3. task-surface-authority-contract
4. superleader-isomorphic-runtime

注意：

- 第 4 项这轮只做第一段，不扩散到完整 cross-team collaboration layer。
- 不要重做 team-parallel-execution baseline、durable-supervisor-sessions、reconnector、protocol-bus。
- 不要回到 max_concurrency、flat teammate_tasks、leader_consumed_mailbox 主语义。

这轮目标不是“再补一个 isolated feature”，而是继续把系统切成：

- task 只负责 activation/contract
- team 内由 mailbox + task list + TeamBlackboard 持续协作
- teammate 更像长期在线 agent
- leader 更像 activation center + convergence center
- superleader 开始变成 leader-of-leaders resident runtime

最低验证：

- uv run pytest tests/test_teammate_work_surface.py tests/test_teammate_online_loop.py tests/test_leader_loop.py tests/test_session_host.py tests/test_superleader_runtime.py -q
- uv run pytest tests/test_worker_supervisor_protocol.py tests/test_runtime.py tests/test_self_hosting_round.py -q
- uv run pytest -q

完成后必须同步更新知识文档，至少包括：

- resource/knowledge/agent-orchestra-runtime/implementation-status.md
- resource/knowledge/agent-orchestra-runtime/current-priority-gap-list.md
- resource/knowledge/agent-orchestra-runtime/current-goal-gap-checklist.md
- resource/knowledge/agent-orchestra-runtime/remaining-work-backlog.md
- resource/knowledge/agent-orchestra-runtime/first-batch-online-collaboration-execution-pack.md
```

## 10. 相关文档

- [current-goal-gap-checklist.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/current-goal-gap-checklist.md)
- [current-priority-gap-list.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/current-priority-gap-list.md)
- [online-collaboration-runtime-migration.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/online-collaboration-runtime-migration.md)
- [resident-team-collaboration-and-slice-planning.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/resident-team-collaboration-and-slice-planning.md)
- [agent-orchestra-gap-handoff.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/agent-orchestra-gap-handoff.md)
- [target-gap-roadmap-and-task-subtree-authority.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/target-gap-roadmap-and-task-subtree-authority.md)
