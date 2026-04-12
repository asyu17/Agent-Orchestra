# authority completion gate 与 resident authority reactor

## 1. 一句话结论

截至 2026-04-07，`authority-escalation-and-scope-extension` 这条线已经补完了此前剩下的两个关键残项：self-hosting 不再只靠 authority side-effect 摘要来关 gap，而是改为消费结构化 `authority_completion`；与此同时，leader 的 authority request/writeback 以及 superleader 的 root decision/writeback 也已经收口到共享 `authority_reactor.py` 主线。当前 authority 不再属于默认 backlog 的开放主项，后续若还有改动，主要属于 broader `online-collaboration-runtime` 清理或更上层的 `permission-broker` 扩展。

## 2. 范围与资料来源

- 当前主线代码：
  - [authority.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/contracts/authority.py)
  - [authority_reactor.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/authority_reactor.py)
  - [leader_loop.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py)
  - [superleader.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/superleader.py)
  - [teammate_work_surface.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/teammate_work_surface.py)
  - [session_host.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/session_host.py)
  - [bootstrap.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/self_hosting/bootstrap.py)
- 相关测试：
  - [test_runtime.py](/Volumes/disk1/Document/code/Agent-Orchestra/tests/test_runtime.py)
  - [test_leader_loop.py](/Volumes/disk1/Document/code/Agent-Orchestra/tests/test_leader_loop.py)
  - [test_superleader_runtime.py](/Volumes/disk1/Document/code/Agent-Orchestra/tests/test_superleader_runtime.py)
  - [test_teammate_work_surface.py](/Volumes/disk1/Document/code/Agent-Orchestra/tests/test_teammate_work_surface.py)
  - [test_session_host.py](/Volumes/disk1/Document/code/Agent-Orchestra/tests/test_session_host.py)
  - [test_self_hosting_round.py](/Volumes/disk1/Document/code/Agent-Orchestra/tests/test_self_hosting_round.py)
  - [test_self_hosting_exports.py](/Volumes/disk1/Document/code/Agent-Orchestra/tests/test_self_hosting_exports.py)
- 相关设计与背景：
  - [authority-system-cutover-plan.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/authority-system-cutover-plan.md)
  - [authority-escalation-and-scope-extension.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/authority-escalation-and-scope-extension.md)
  - [self-hosting-bootstrap.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/self-hosting-bootstrap.md)

## 3. 这次补齐了什么

### 3.1 completion gate 从摘要推断切到结构化 closure

现在 self-hosting 会通过 `collect_lane_authority_completion_snapshot(...)` 生成 lane 级 authority closure 快照，而不是只看：

- `decision_counts`
- `resume_task_ids`
- `reroute_links`
- `waiting_for_authority_task_ids`

新的 closure 主语义会把每个 authority request 分类成：

- `waiting`
- `grant_resumed`
- `reroute_closed`
- `deny_closed`
- `relay_pending`
- `incomplete`

对应聚合结果写进 lane metadata 的 `authority_completion`，以及 instruction packet metadata 的 `authority_completion_status`。

### 3.2 team authority 进入 shared reactor

leader 侧原本分散在 `leader_loop.py` 里的：

- `authority.request` 消费
- soft-scope `grant / deny / reroute`
- protected boundary `escalate`
- writeback 转发给 teammate

现在已经收口到共享 `TeamAuthorityReactor`。

`LeaderLoopSupervisor` 不再自己实现一套 authority 分支判断，而是调用 reactor，然后把 reactor 结果通过 resident cycle metadata 投影到 `coordinator_session.metadata`。

### 3.3 authority root 进入 shared reactor

superleader 侧对 escalated authority 的 root decision / writeback 现在已经完全切到共享 `ObjectiveAuthorityRootReactor`。`superleader.py` 不再保留本地 fallback 或 duplicate authority branch，shared reactor 已成为唯一 authority root 语义面。

### 3.4 teammate/session truth 具备 closure 证据

`teammate_work_surface.py` 与 `session_host.py` 现在会把 authority relay/decision/wake 写成可判定 closure 的 session metadata，例如：

- `authority_last_relay_subject`
- `authority_last_relay_envelope_id`
- `authority_last_relay_consumed_at`
- `authority_relay_consumed`
- `authority_wake_recorded`
- `authority_completion_status`

这意味着 completion gate 不再只能从 task/delivery 看 authority，也能从 recipient session 看到 relay 是否真的被消费。

## 4. 现在 authority 的正式完成语义

### 4.1 grant

一个 request 只有在下面这些条件都满足时才算 `grant_resumed`：

- decision 已 commit 为 `grant`
- durable outbox 中存在 `authority.decision`
- teammate session 记录到了同一个 request 的 decision 消费
- wake/reactivation 证据已经写入 session
- task 已回到可推进状态

### 4.2 reroute

一个 request 只有在下面这些条件都满足时才算 `reroute_closed`：

- 原 task 已 commit 为 `reroute`
- 原 task 已 `superseded_by_task_id`
- replacement task 已存在并进入 lane task surface
- relay 已发布并被相关 session 消费

### 4.3 deny

一个 request 只有在下面这些条件都满足时才算 `deny_closed`：

- decision 已 commit 为 `deny`
- task 已进入 authority terminal blocked
- relay 已发布并被相关 session 消费

### 4.4 waiting / relay_pending / incomplete

- `waiting`
  - request 已 commit，但还没有最终决策
- `relay_pending`
  - decision 已有，但 relay 还没 durable 发布
- `incomplete`
  - decision 与 relay 存在，但闭环恢复证据不完整

只有 `grant_resumed / reroute_closed / deny_closed` 会进入 closed request 集合。

## 5. 当前代码入口

- [authority.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/contracts/authority.py)
  - authority completion 的 contract 类型：`AuthorityCompletionStatus`、`AuthorityCompletionRequestSnapshot`、`AuthorityCompletionLaneSnapshot`、`AuthorityReactorCycleOutput`
- [authority_reactor.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/authority_reactor.py)
  - shared `TeamAuthorityReactor`
  - shared `ObjectiveAuthorityRootReactor`
  - `collect_lane_authority_completion_snapshot(...)`
- [leader_loop.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py)
  - leader resident authority reactor 接线点
- [superleader.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/superleader.py)
  - objective root authority reactor 接线点
- [bootstrap.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/self_hosting/bootstrap.py)
  - self-hosting authority completion gate 与导出渲染

## 6. 对 backlog 的影响

这次补齐后，`authority-escalation-and-scope-extension` 不再作为默认 P0 开放 gap 保留。

后续如果 authority 还要继续推进，通常应归到下面两类：

- `online-collaboration-runtime`
  - 例如进一步把 authority reactor 更深地并入 host-owned resident hierarchy，或继续压缩 broader runtime cleanup 中的重复真相
- `permission-broker`
  - 例如把现有 authority policy 与更正式的 sandbox/policy control plane 合并

## 7. 相关文档

- [authority-escalation-and-scope-extension.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/authority-escalation-and-scope-extension.md)
- [authority-system-cutover-plan.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/authority-system-cutover-plan.md)
- [self-hosting-bootstrap.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/self-hosting-bootstrap.md)
- [implementation-status.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/implementation-status.md)
- [current-priority-gap-list.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/current-priority-gap-list.md)
- [remaining-work-backlog.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/remaining-work-backlog.md)
