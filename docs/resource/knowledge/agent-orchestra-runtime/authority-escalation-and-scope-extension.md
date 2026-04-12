# agent_orchestra authority escalation 与 scope extension

## 1. 一句话结论

截至 2026-04-07，这条主线已经从 request/wait first-cut 收口成完整的 decision + closure mainline：`WorkerFinalReport` 可以携带结构化 `authority_request`，`TeammateWorkSurface.execute_assignment(...)` 会通过 `GroupRuntime.commit_authority_request(...)` 把这类 blocker 持久化成 `TaskStatus.WAITING_FOR_AUTHORITY` 并发出 `authority.request`；`LeaderLoopSupervisor` 已有 `soft_scope -> grant / deny / reroute` 与 protected request `-> escalate` 的 promptless 控制面，并通过共享 `TeamAuthorityReactor` 输出 resident reactor metadata；`SuperLeaderRuntime` 已能对 escalated request 做 `protected_runtime -> grant`、`global_contract -> deny`、`cross_team_shared -> reroute`，并通过共享 `ObjectiveAuthorityRootReactor` 回写 subordinate leader；`GroupRuntime.commit_authority_decision(...)` 也已经支持 `grant / deny / reroute / escalate` 的 task/delivery/blackboard/outbox 写回；`TeammateWorkSurface` 与 `ResidentSessionHost` 现在会把 authority relay / decision / wake 写成可供 completion gate 使用的 closure truth。`AuthorityPolicy / AuthorityBoundaryClass / AuthorityPolicyAction` 已进入主线，authority coordination outbox 也已经升级为 public `CoordinationOutboxRecord`，并在 PostgreSQL `coordination_outbox` 中与 task/blackboard/delivery/session 同事务提交；self-hosting 则改为消费结构化 `authority_completion`，不再只依赖 summary-level authority evidence。2026-04-10 又把 task-surface governance 真正接进了这条 authority 主线：cross-subtree / cross-team task mutation 与 teammate 对 protected task fields 的写入，现在都会通过 `task_surface_authority` intent 复用同一条 authority request/decision/outbox 路径，而 `grant` 决策会把 intent 真实回写成 task-surface mutation 或 protected-field patch，而不是停留在“caller 自己重试”的文档级约定。同一天，这条 authority truth 也继续进入共享 read model：resident lane live view 会导出结构化 `task_surface_authority` snapshot，leader revision bundle 会自动补齐 `authority_notices / project_item_notices`，从而让 planning/runtime consumer 至少看见 governed task surface，而不是只看 review 文本或 generic task list。当前这条 gap 的主语义已经完成，后续 authority 相关改动主要属于 broader runtime cleanup 或 `permission-broker` 扩展。

## 2. 范围与资料来源

- 直接触发样例：
  - [task_0714d29d057a:teammate-turn-1.prompt.md](/Volumes/disk1/Document/code/Agent-Orchestra/docs/superpowers/runs/codex-spool/task_0714d29d057a:teammate-turn-1.prompt.md)
  - [task_0714d29d057a:teammate-turn-1.result.json](/Volumes/disk1/Document/code/Agent-Orchestra/docs/superpowers/runs/codex-spool/task_0714d29d057a:teammate-turn-1.result.json)
  - [task_0714d29d057a:teammate-turn-1.last_message.txt](/Volumes/disk1/Document/code/Agent-Orchestra/docs/superpowers/runs/codex-spool/task_0714d29d057a:teammate-turn-1.last_message.txt)
  - [2026-04-06-agent-orchestra-top-5-gap-launch-brief.md](/Volumes/disk1/Document/code/Agent-Orchestra/docs/superpowers/runs/2026-04-06-agent-orchestra-top-5-gap-launch-brief.md)
- 当前 runtime 代码链路：
  - [worker_protocol.py#L139](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/contracts/worker_protocol.py#L139)
  - [codex_cli_backend.py#L169](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/backends/codex_cli_backend.py#L169)
  - [group_runtime.py#L1480](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/group_runtime.py#L1480)
  - [group_runtime.py#L1547](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/group_runtime.py#L1547)
  - [teammate_work_surface.py#L1537](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/teammate_work_surface.py#L1537)
  - [teammate_work_surface.py#L1661](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/teammate_work_surface.py#L1661)
  - [evaluator.py#L79](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/evaluator.py#L79)
  - [leader_loop.py#L1256](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py#L1256)
  - [leader_loop.py#L1743](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py#L1743)
  - [superleader.py#L246](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/superleader.py#L246)
  - [superleader.py#L678](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/superleader.py#L678)
- 对照来源：
  - [resource/knowledge/claude-code-main/agent-team.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/claude-code-main/agent-team.md)
  - [target-gap-roadmap-and-task-subtree-authority.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/target-gap-roadmap-and-task-subtree-authority.md)
  - [self-hosting-bootstrap.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/self-hosting-bootstrap.md)
 - 当前验证：
  - [test_worker_protocol_contracts.py#L75](/Volumes/disk1/Document/code/Agent-Orchestra/tests/test_worker_protocol_contracts.py#L75)
  - [test_teammate_work_surface.py#L1700](/Volumes/disk1/Document/code/Agent-Orchestra/tests/test_teammate_work_surface.py#L1700)
  - [test_leader_loop.py#L173](/Volumes/disk1/Document/code/Agent-Orchestra/tests/test_leader_loop.py#L173)
  - [test_superleader_runtime.py#L1023](/Volumes/disk1/Document/code/Agent-Orchestra/tests/test_superleader_runtime.py#L1023)

## 3. 历史问题到底是什么

这次 `task_0714d29d057a:teammate-turn-1` 暴露出的，不是“teammate 判断错了”，而是系统语义缺了一层。

事实链路是：

- 这条 teammate assignment 的 `owned_paths` 只包含 5 个知识文档，不包含 [bootstrap.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/self_hosting/bootstrap.py)。
- 它在执行强制验证 `python3 -m unittest tests.test_leader_loop tests.test_self_hosting_round -v` 时，遇到了 `bootstrap.py` 的导入错误。
- worker 自己正确判断出：这是一个真实 blocker，但修它需要改 out-of-scope file，所以只能在 final report 中返回 `terminal_status = "blocked"`。
- `codex_cli_backend` 当前会把任何 `terminal_status != completed` 的 terminal report 统一折成 raw worker `status = failed`。
- 之后 `GroupRuntime.commit_teammate_result(...)`、`DefaultDeliveryEvaluator.evaluate_lane(...)`、以及 `LeaderLoopSupervisor` 又依次把这件事扩散成：
  - teammate task `FAILED`
  - lane `BLOCKED`
  - objective 依赖图进入“上游 lane blocked、下游 lane pending 但无 ready lane”的 resident loop 卡住态

判断：

- worker 没有 transport failure，也没有 timeout failure，更不是它“不能读这个文件”。
- 它的问题是：在 first-cut 落地之前，系统只给了它“越界即终态”这一个出口，没有提供“申请扩权并等待裁决”的正式路径。

## 4. 为什么这应该是独立 gap

这条缺口和现有几个 gap 有关系，但不等价：

- 它不是 `permission-broker`
  - `permission-broker` 更偏全局 authority / sandbox / approval control plane。
  - 当前缺的是 team 内协作运行时里的动态扩权链路，重点是 `teammate -> leader -> superleader` 的运行时裁决，而不是全局 permission policy。
- 它不是 `task-subtree-authority`
  - `task-subtree-authority` 解决的是谁能改 task list 的状态、结构、只读边界。
  - 当前缺的是“当执行中发现真实 blocker 且需要额外改动范围时，如何请求更高 authority”。
- 它也不能只算成 `online-collaboration-runtime` 的一句附注
  - 因为这里缺的已经是完整的协议、状态机、裁决面和 finalize 语义，不是单点 helper。

因此更准确的做法是：把它提升成独立 gap，名称固定为 `authority-escalation-and-scope-extension`。

## 5. 目标主语义

### 5.1 teammate 发现真实 blocker 时，不直接死掉

目标语义应该是：

- teammate 遇到的问题如果是“我执行失败了”，可以直接 `failed`
- 但如果是“我知道怎么继续，可继续动作需要额外 authority 或额外 scope”，则应提交结构化 `authority request`

这类 request 至少要包含：

- `request_id`
- `assignment_id`
- `task_id`
- `worker_id`
- `requested_paths`
- `reason`
- `evidence`
- `blocking_verification_command`
- `retry_hint`

也就是说，worker 返回的不再只是一个模糊 blocker 字符串，而是一条正式的扩权申请。

### 5.2 leader 是第一裁决层，而不是 teammate 自行扩权

正式规则应该是：

- teammate 不能自己扩权
- teammate 只能申请扩权
- `Leader` 是 team 内第一裁决中心

leader 对 request 的标准决策应收敛成 4 种：

- `grant`
- `reroute`
- `escalate`
- `deny`

解释：

- `grant`
  - 当前 teammate 继续做，允许扩展 scope
- `reroute`
  - 不扩给当前 teammate，而是 reopen / create 一个更合适的 repair slice
- `escalate`
  - 问题超出 leader authority，升级到 `SuperLeader` 或更高 authority
- `deny`
  - 明确拒绝，task/lane 进入真正 blocked

### 5.3 软边界和硬边界要分开

建议把边界分成两类：

- `soft scope boundary`
  - 一般 repo 内文件
  - 默认允许 leader 直接评估是否扩给 teammate
- `protected authority boundary`
  - 共享核心 contract、全局协调运行时、跨 team 公共文件
  - 默认不允许 leader 直接放行，必须升级

推荐默认受保护的文件/目录包括：

- `src/agent_orchestra/contracts/`
- `src/agent_orchestra/runtime/superleader.py`
- `src/agent_orchestra/runtime/session_host.py`
- `src/agent_orchestra/self_hosting/bootstrap.py`
- 跨 team 共享知识索引与全局配置

### 5.4 新的中间状态应是 `WAITING_FOR_AUTHORITY`

要避免当前这种 `blocked -> failed` 过早收敛，runtime 需要引入显式等待态。

建议新增：

- `TaskStatus.WAITING_FOR_AUTHORITY`
- `DeliveryStatus.WAITING_FOR_AUTHORITY`

判断规则：

- request 已提交、尚未裁决：`WAITING_FOR_AUTHORITY`
- request 被拒绝或预算耗尽：`BLOCKED`
- request 获批并恢复推进：回到 `IN_PROGRESS / RUNNING`
- 真实执行失败或协议失败：`FAILED`

### 5.5 self-hosting / finalize 必须把“等待 authority”和“真正失败”区分开

这次 round-2 说明了另一个问题：如果上游 lane 因 authority/scope 问题进入阻塞态，而下游 lane 仍在依赖它，superleader 不应无限 resident wait 而不写 report。

目标语义应该是：

- `WAITING_FOR_AUTHORITY` 是可导出的 objective 中间态
- `BLOCKED by denied authority` 是可导出的 objective 终态
- superleader 必须能在“无 ready lanes 且只剩 authority wait / blocked dependency graph”时 finalize，而不是无限等待

## 6. 当前已落地的主线阶段

当前已经真正落地的链路包括：

- `WorkerFinalReport.authority_request`
  - worker final report 现在可以直接携带 `ScopeExtensionRequest`，不再只能塞进模糊的 `blocker / retry_hint` 文本。
- codex teammate prompt / final report parser
  - `codex_cli` teammate prompt 已明确要求：只有当 blocker 真的是“需要更大 scope / 更高 authority 才能继续”时，才返回结构化 `authority_request`；backend 解析后会把它写进 protocol `final_report`。
- protocol-valid blocked-only guard
  - authority request 现在只会从协议有效、`terminal_status = blocked`、并且 assignment/worker/task 身份与当前 assignment 一致的 `final_report` 中提取；带 `protocol_failure_reason`、身份不一致、或终态并非 `blocked` 的 payload，不会再被错误提升成 `WAITING_FOR_AUTHORITY`。
- runtime-owned authority request commit
  - `GroupRuntime.commit_authority_request(...)` 会把 task 状态持久化为 `WAITING_FOR_AUTHORITY`，写入 team blackboard proposal，并更新 lane delivery snapshot 的 waiting metadata。
- teammate-owned publish path
  - `TeammateWorkSurface.execute_assignment(...)` 在看到 blocked final report 且存在 authority request 时，不再走普通 `task.result failed` 路径，而是直接发 `authority.request` mailbox envelope。
- leader convergence stop
  - `LeaderLoopSupervisor` 的 promptless 路径和 prompt-turn 路径现在都会在 evaluation 返回 `WAITING_FOR_AUTHORITY` 时停下，并把 leader runtime task 更新成 `WAITING_FOR_AUTHORITY`。
- leader authority decision mainline
  - `LeaderLoopSupervisor` 现在会消费 `authority.request`，对 `soft_scope` 做 `grant / deny / reroute`，对受保护边界做 `escalate`；grant/reroute 之后会继续走 promptless convergence，而不是强制再开一个 leader prompt turn。
- superleader authority-wait finalize
  - superleader 现在已经能在“没有 ready lane，剩余 pending 只依赖 authority-wait lane”时 stop/finalize，而不是继续 resident wait。
- superleader authority decision mainline
  - `SuperLeaderRuntime` 现在会对已 escalated 的 authority request 做正式裁决：`protected_runtime -> grant`，`global_contract -> deny`，`cross_team_shared -> reroute`，并在 decision 后显式回写 subordinate leader 的控制消息。
- runtime reroute commit
  - `GroupRuntime.commit_authority_decision(...)` 已经支持 `reroute`，并会把旧 task 标成 `CANCELLED`、记录 `superseded_by_task_id`，同时把 replacement task 写成新的 pending repair task，并把 reroute link 投影进 lane delivery metadata。
- teammate authority live consumer
  - `TeammateWorkSurface` 现在会消费 `authority.decision / authority.escalated / authority.writeback`，不再把这类消息误当成 directive work，而是通过 `ResidentSessionHost.record_authority_wait_state(...) / record_authority_decision_state(...)` 更新 authority wait / wake 元数据。
- authority outbox durable mainline
  - `commit_authority_request(...)` 与 `commit_authority_decision(...)` 现在都会返回 public `CoordinationOutboxRecord`；blackboard payload 继续携带 `coordination_outbox`，in-memory store 提供 `list_coordination_outbox_records()`，PostgreSQL 通过 `coordination_outbox` 表把 authority outbox 与 task/blackboard/delivery/session 放进同一事务提交；对应回归见 `tests/test_postgres_store.py` 与 `tests/test_runtime.py`。

### 6.1 后续并入其他主线的工作

这条 gap 当前没有单独的 open blocker。

后续 authority 相关工作主要会落到：

1. `online-collaboration-runtime`
   - 例如继续把 authority reactor 更深地并入 host-owned resident hierarchy，或压缩 broader runtime cleanup 中的重复实现
2. `permission-broker`
   - 例如把 authority policy 与更正式的 sandbox/policy control plane 合并

## 7. 在路线图里的位置

建议把这条 gap 放在：

1. `online-collaboration-runtime`
2. `session-truth-and-coordination-transaction-convergence`
3. `team-primary-semantics-switch`
4. `authority-escalation-and-scope-extension`
5. `task-subtree-authority`
6. `superleader-isomorphic-runtime`

原因：

- 它建立在 team resident collaboration 正在成为主语义这一前提上
- 但它又明显早于 `task-subtree-authority`，因为没有动态 authority path，task surface 很快就会退化成“要么卡死、要么越权”
- 它也早于更泛化的 `permission-broker`，因为这条问题首先发生在 team runtime 主语义里

补充判断：

- bootstrap catalog 仍保留 `authority-integration` 作为 targeted validation gap。
- 但默认知识 backlog 已不再把这条线作为开放 P0 主项。

## 8. 相关文档

- [authority-system-cutover-plan.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/authority-system-cutover-plan.md)
- [authority-completion-gate-and-resident-reactor.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/authority-completion-gate-and-resident-reactor.md)
- [current-priority-gap-list.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/current-priority-gap-list.md)
- [remaining-work-backlog.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/remaining-work-backlog.md)
- [target-gap-roadmap-and-task-subtree-authority.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/target-gap-roadmap-and-task-subtree-authority.md)
- [hierarchical-online-agent-runtime-gap-list.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/hierarchical-online-agent-runtime-gap-list.md)
- [self-hosting-bootstrap.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/self-hosting-bootstrap.md)
- [claude-comparison-and-system-logic.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/claude-comparison-and-system-logic.md)
