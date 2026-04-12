# agent_orchestra authority 系统级改造方案

## 1. 一句话结论

`authority-escalation-and-scope-extension` 当前已经不只是 request/wait first-cut：`AuthorityDecision`、`commit_authority_decision(...)`、leader 的 `grant / deny / reroute / escalate` promptless 控制面、superleader 的 escalated `grant / deny / reroute`、subordinate leader writeback、runtime-level reroute commit，以及 teammate authority control envelope live consumer 都已进入主线。`AuthorityPolicy / AuthorityBoundaryClass / AuthorityPolicyAction` 也已接入 runtime，并由 leader/superleader 共用，不再依赖 hard-coded prefix + `retry_hint` 字符串 marker。authority request/decision 的 outbox 已经升级为 public `CoordinationOutboxRecord`，并在 PostgreSQL `coordination_outbox` 中与 task/blackboard/delivery/session 同事务提交。到 2026-04-07，这条方案里最后两个核心残项也已落地：self-hosting authority completion gate 已转为结构化 closure contract，authority relay 也已经进入共享 resident authority reactor 主线。当前这份 cutover plan 可以视为本阶段完成稿，后续 authority 相关工作不再属于这条主线的未完成 blocker。

## 2. 范围与资料来源

- 当前 authority 主线基线：
  - [authority-escalation-and-scope-extension.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/authority-escalation-and-scope-extension.md)
  - [implementation-status.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/implementation-status.md)
  - [current-priority-gap-list.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/current-priority-gap-list.md)
  - [remaining-work-backlog.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/remaining-work-backlog.md)
- 当前源码入口：
  - [authority.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/contracts/authority.py)
  - [task.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/contracts/task.py)
  - [delivery.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/contracts/delivery.py)
  - [worker_protocol.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/contracts/worker_protocol.py)
  - [group_runtime.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/group_runtime.py)
  - [teammate_work_surface.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/teammate_work_surface.py)
  - [leader_loop.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py)
  - [superleader.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/superleader.py)
  - [session_host.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/session_host.py)
  - [storage/base.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/storage/base.py)
- 相关上游与并行知识：
  - [resource/knowledge/claude-code-main/agent-team.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/claude-code-main/agent-team.md)
  - [hierarchical-online-agent-runtime-gap-list.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/hierarchical-online-agent-runtime-gap-list.md)
  - [target-gap-roadmap-and-task-subtree-authority.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/target-gap-roadmap-and-task-subtree-authority.md)
  - [self-hosting-bootstrap.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/self-hosting-bootstrap.md)

## 3. 当前基线与为什么要单独做系统方案

当前已经存在的主线基线不再只是 request/wait，而是至少包括下面这几段：

- teammate 返回结构化 `authority_request`
- runtime 将 task 持久化为 `WAITING_FOR_AUTHORITY`
- leader 会消费 `authority.request`，对 `soft_scope` 做 `grant`，对受保护边界做 `escalate`
- superleader 会对 escalated request 做最小正式裁决：`protected_runtime -> grant`，`global_contract -> deny`
- runtime 已支持 `commit_authority_decision(...)`，并能把 `reroute` 写成 superseded task + replacement task

这条链已经解决了最初的“越界即终态”问题，也把 decision 主链推进到了 partial mainline。当前这条线已经补齐跨层一致性与主要分支完整性：

- mailbox outbox 已升级成 durable authority coordination transaction（public `CoordinationOutboxRecord` + PostgreSQL `coordination_outbox`）
- protected boundary policy contract 已落地，并已由 shared resident authority reactor 消费
- self-hosting authority decision/resume/reroute completion gate 已落地

判断：

- 这次 cutover 之后，authority 不再依赖散落的 loop-level heuristics 作为正式完成语义。
- 后续如果继续修改 authority，更合适的归类是 broader runtime cleanup 或上层 permission control-plane。

补充：

- 下面第 4 到第 7 节保留的是这次切换时的系统级设计底稿，现阶段应按“已完成的 cutover 归档”阅读，而不是当作仍待执行的开放实现清单。

## 4. 归档的目标与边界（现已完成）

### 4.1 目标

authority 改造完成后的正式目标应是：

- teammate 可以申请 authority，但不能自己扩权
- leader 是 team 内第一 authority center
- superleader 是跨 team / protected boundary 的 authority root
- authority request、authority decision、resume/re-activation 都是正式 runtime contract，而不是 prompt 约定
- task、delivery、blackboard、mailbox、session、store 对 authority 的状态变化保持同一事实源

### 4.2 非目标

这次 authority 主线不应和下面几类事情混在一起做：

- 不把它泛化成全局 `permission-broker` 一次做完
- 不把它和 `task-subtree-authority` 的结构权限一次揉在一起
- 不引入新的“万能任务状态枚举”去掩盖缺失的 authority object model
- 不回退成“遇到异常就让 leader 重新发一个 task”这种弱语义

### 4.3 不变约束

下面这些约束应在整个改造过程中保持不变：

- `SuperLeader` 不直接给 teammate 发 task
- `Leader` 不能改写上层目标，只能在已授权 team contract 内裁决与拆解
- 只有 protocol-valid、终态为 `blocked`、并且身份匹配的 authority request 才能进入 authority 主链
- protected boundary 默认不能由 leader 直接放行

## 5. 归档的系统级改造点（现已落地）

### 5.1 Contract 层

需要把 authority contract 从“request first-cut”推进到完整三段：

- `ScopeExtensionRequest`
  - 保留当前 shape，但增加更强的 contract 语义：它必须表达“继续推进需要额外 authority”，而不是泛化 blocker。
- `AuthorityDecision`
  - 当前 dataclass 已存在，但语义太薄。
  - 需要补成正式决策对象，至少要能表达：
    - `request_id`
    - `decision`
    - `actor_id`
    - `scope_class`
    - `granted_paths`
    - `reroute_task_id`
    - `escalated_to`
    - `summary`
    - `reason`
    - `resume_mode`
- `AuthorityBoundaryClass`
  - 建议新增显式 boundary 分类，而不是只靠路径字符串判断。
  - 最少区分：
    - `soft_scope`
    - `protected_runtime`
    - `cross_team_shared`
    - `global_contract`

建议主文件：

- [authority.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/contracts/authority.py)
- [task.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/contracts/task.py)
- [delivery.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/contracts/delivery.py)
- [worker_protocol.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/contracts/worker_protocol.py)

### 5.2 Task / Delivery / Session 状态模型

authority 不能只落在 mailbox 文本里，必须进入 live model。

task 需要补的字段建议包括：

- `authority_request_id`
- `authority_status`
- `authority_boundary_class`
- `authority_waiting_since`
- `authority_last_decision`
- `authority_resume_target`

delivery 需要补的内容：

- lane/objective metadata 中显式保留：
  - `waiting_for_authority_task_ids`
  - `authority_request_count`
  - `escalated_authority_request_ids`
  - `denied_authority_request_ids`

session 需要补的内容：

- teammate slot session 要能知道自己是否在等待 authority
- grant 后应能被显式 wake/reactivate
- reroute/deny 后要能给出干净的 stop reason，而不是继续占着旧 task

这里的关键判断是：

- `WAITING_FOR_AUTHORITY` 仍应保留为 task/lane/objective 的正式中间态
- 但真正的 authority 事实不应只编码在这个状态名里，而应有单独 authority fields 做关联和恢复

### 5.3 Mailbox / Blackboard 协议

authority 需要正式的消息面，而不是继续混在普通 `task.result` 里。

建议 authority 协议固定成 3 类 envelope：

- `authority.request`
  - teammate -> leader
- `authority.decision`
  - leader/superleader -> teammate 或 -> leader
- `authority.escalated`
  - leader -> superleader

默认可见性：

- `authority.request`
  - `CONTROL_PRIVATE`
  - `SUMMARY_PLUS_REF`
- `authority.decision`
  - 对相关 actor `SUMMARY_PLUS_REF`
  - 对上层 digest 可见 summary/index
- `authority.escalated`
  - lane/private control path

blackboard 建议固定成：

- request 使用 `PROPOSAL`
- decision 使用 `DECISION`
- deny 或 hard block 可再追加 `BLOCKER`

原因：

- request 不是 blocker 本身，它是一个待裁决 proposal
- decision 需要单独可审计，不能只靠覆盖旧 entry

建议主文件：

- [teammate_work_surface.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/teammate_work_surface.py)
- [leader_loop.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py)
- [superleader.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/superleader.py)
- [protocol_bridge.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/protocol_bridge.py)

### 5.4 GroupRuntime / Store Commit 面

这是这条线最关键的系统改造点之一。

当前 authority request/decision 已进入 transaction-grade commit：task、blackboard、delivery、session、mailbox outbox 已在 store commit path 同事务提交。后续重点是围绕 resume/reactivation 与 authority reactor 的收口。

authority coordination commit bundle 包括：

- task update
- blackboard entry
- delivery snapshot
- session snapshot
- mailbox outbox

要么一起成功，要么一起失败。

建议新增或收口的 runtime API：

- `commit_authority_request(...)`
  - 从当前 first-cut helper 升级为 store-commit bundle
- `commit_authority_decision(...)`
  - 决策落盘的正式入口
- `commit_authority_resume(...)`
  - grant 后唤醒/恢复同一 task 的正式入口
- `commit_authority_reroute(...)`
  - reroute 场景下旧 task 和新 task 的统一提交
- `list_pending_authority_requests(...)`
  - leader/superleader 决策循环的读取面

建议主文件：

- [group_runtime.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/group_runtime.py)
- [storage/base.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/storage/base.py)
- [storage/postgres/store.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/storage/postgres/store.py)

### 5.5 Leader Runtime

leader 侧不应只是“看到 wait 就停下”，而要进入正式 authority 决策面。

leader 需要补的能力：

- inbox poller 能识别 `authority.request`
- 基于 policy 把 request 分类成：
  - 可直接 grant 的 soft scope
  - 需要 reroute 的 task mismatch
  - 需要 escalate 的 protected boundary
  - 应直接 deny 的无效请求
- 决策后调用 runtime commit API
- grant 时触发 teammate resume/wake
- reroute 时创建 follow-up slice 或 repair task

这里的关键变化是：

- leader 的职责从“等待 blocker 自然消失”升级成“authority first-line decision center”
- 但 leader 仍不改写上层目标，也不直接越过 superleader 碰 protected boundary

建议主文件：

- [leader_loop.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py)
- [group_runtime.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/group_runtime.py)

### 5.6 SuperLeader Runtime

superleader 侧需要承接 escalation，而不是只看到 lane 处于 `WAITING_FOR_AUTHORITY`。

需要补的能力：

- lane digest 中显式暴露 escalated authority requests
- 处理 `authority.escalated`
- 基于 protected boundary policy 做最终裁决
- 决策后回写 subordinate leader，而不是直接去驱动 teammate
- finalize 时能区分：
  - authority pending
  - authority denied
  - blocked dependency graph
  - real execution failure

这部分的主语义应保持：

- superleader 是 leader-of-leaders
- authority escalation 也应通过 leader lane 收敛，而不是打破层级边界

建议主文件：

- [superleader.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/superleader.py)
- [leader_loop.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py)

### 5.7 Teammate / Resident Session Host

grant 之后如果没有 resume，authority 只有“等”和“终止”，就还不算闭环。

需要补的 teammate/host 语义：

- teammate session 能感知 `authority.decision`
- `grant`
  - 优先恢复同一 task、同一 slot、同一 claim chain
- `reroute`
  - 当前 task 退出 authority wait，转向新 task
- `deny`
  - 当前 task 进入真正 blocked
- `escalate`
  - 保持 wait，但等待上层决策

`ResidentSessionHost` 需要成为 authority resume 的 truth owner：

- 记录 wait token / wake intent
- 记录当前 authority request 与对应 session
- 在决策落盘后发 wake/reactivation signal

建议主文件：

- [session_host.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/session_host.py)
- [teammate_online_loop.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/teammate_online_loop.py)
- [teammate_work_surface.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/teammate_work_surface.py)

### 5.8 Policy 层

authority 的真正边界，不应写死在 prompt 文案里。

建议抽出正式 policy：

- `AuthorityPolicy`
  - 决定 soft/protected boundary
- `AuthorityRoutingPolicy`
  - 决定 grant/reroute/escalate/deny 的默认路由
- `AuthorityResumePolicy`
  - 决定 grant 后是继续 attach-first path（attach/wake）、重发 assignment，还是转成新 slice

和其他治理面的边界要明确：

- 与 `permission-broker` 的关系：
  - authority 先解决 team resident runtime 内的动态扩权链
  - permission-broker 再解决更泛化的 sandbox / approval control plane
- 与 `task-subtree-authority` 的关系：
  - authority 决定“能不能继续碰更多 scope”
  - task-subtree-authority 决定“能不能增删改 task subtree”

### 5.9 Self-Hosting / Bootstrap / Evaluator

这条线如果不进入 self-hosting，就会一直停留在“代码有、系统不会自举推进”的状态。

要补的点包括：

- bootstrap catalog 当前仍保留 targeted `authority-integration` 验证入口，但默认 backlog 不再把这条线作为开放主项
- self-hosting 关闭条件要从“出现 authority wait”升级成：
  - request evidence
  - decision evidence
  - resume/reroute/deny evidence
- evaluator 与 exported round report 要显式表达：
  - pending authority
  - denied authority
  - resumed after authority

建议主文件：

- [bootstrap.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/self_hosting/bootstrap.py)
- [evaluator.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/evaluator.py)

### 5.10 测试与验收面

authority 正式切换后，至少要补齐下面 5 类测试：

- contract tests
  - request / decision / resume payload shape
- negative protocol tests
  - protocol-invalid request 不得进入 authority wait
- leader decision tests
  - grant / reroute / escalate / deny 四分支
- superleader escalation tests
  - protected boundary、cross-team、finalize 语义
- transaction / failure injection tests
  - request/decision commit 失败时不得留下半提交状态

## 6. 归档的实施顺序

建议严格按下面顺序推进，不要乱序：

1. `AuthorityDecision + policy contract`
   - 先把 decision/boundary/resume contract 定下来
2. `transaction-grade authority commit`
   - 先补 request/decision 的 store bundle
3. `leader decision path`
   - 做 grant / reroute / escalate / deny
4. `superleader escalation path`
   - 做 protected boundary 与 objective finalize 收口
5. `teammate resume / reactivation`
   - 接回 resident session host
6. `self-hosting adoption`
   - gap catalog、evidence gate、round report

原因：

- 如果先做 leader/superleader 决策，而 store commit 还不是原子化，后面排障会非常脆。
- 如果先做 resume，而 decision contract 还没定清楚，session 恢复会直接做成补丁。

## 7. 完成判据

authority 这条系统级改造完成时，至少要满足下面这些事实：

- teammate 遇到真实 scope blocker 时，不会直接终态失败
- leader 可以对 soft scope 做第一层正式裁决
- superleader 可以对 protected boundary 做正式 escalation 裁决
- grant 后能恢复推进，而不是只会停在 `WAITING_FOR_AUTHORITY`
- deny 后会形成清晰的 blocked 终态，而不是 authority wait 悬空
- request / decision / delivery / blackboard / session / mailbox 不会出现半提交分裂
- self-hosting 能把 authority 相关工作识别成可验证的完成证据

## 8. 相关文档

- [authority-escalation-and-scope-extension.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/authority-escalation-and-scope-extension.md)
- [authority-completion-gate-and-resident-reactor.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/authority-completion-gate-and-resident-reactor.md)
- [current-priority-gap-list.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/current-priority-gap-list.md)
- [remaining-work-backlog.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/remaining-work-backlog.md)
- [target-gap-roadmap-and-task-subtree-authority.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/target-gap-roadmap-and-task-subtree-authority.md)
- [hierarchical-online-agent-runtime-gap-list.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/hierarchical-online-agent-runtime-gap-list.md)
- [self-hosting-bootstrap.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/self-hosting-bootstrap.md)
