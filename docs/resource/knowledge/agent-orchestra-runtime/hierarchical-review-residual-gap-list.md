# hierarchical review residual gap 清单

## 1. 一句话结论

截至 2026 年 4 月 7 日，hierarchical review 这条线已经完成 first-cut 主线：`TaskReviewSlot -> TeamPositionReview -> CrossTeamLeaderReview -> SuperLeaderSynthesis` 的 contracts、store、`GroupRuntime` API、leader convergence helper、superleader finalize helper 都已落地并进入测试主线；下一步真正还没完成的，不再是“有没有这层能力”，而是把它从 first-cut review artifact 推进成更强的正式协调面，包括 `project item` 一等化、phase/state machine、shared digest/subscription surface，以及独立的 review visibility / authority policy。

## 2. 范围与资料来源

- 已落地实现：
  - [hierarchical_review.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/contracts/hierarchical_review.py)
  - [group_runtime.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/group_runtime.py)
  - [leader_loop.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py)
  - [superleader.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/superleader.py)
  - [base.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/storage/base.py)
  - [in_memory.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/storage/in_memory.py)
  - [models.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/storage/postgres/models.py)
  - [store.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/storage/postgres/store.py)
- 相关测试：
  - [test_hierarchical_reviews.py](/Volumes/disk1/Document/code/Agent-Orchestra/tests/test_hierarchical_reviews.py)
  - [test_runtime.py](/Volumes/disk1/Document/code/Agent-Orchestra/tests/test_runtime.py)
  - [test_leader_loop.py](/Volumes/disk1/Document/code/Agent-Orchestra/tests/test_leader_loop.py)
  - [test_superleader_runtime.py](/Volumes/disk1/Document/code/Agent-Orchestra/tests/test_superleader_runtime.py)
  - [test_postgres_store.py](/Volumes/disk1/Document/code/Agent-Orchestra/tests/test_postgres_store.py)
- 上位知识：
  - [hierarchical-review-and-cross-team-knowledge-fusion.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/hierarchical-review-and-cross-team-knowledge-fusion.md)
  - [task-review-slots-and-claim-context-fusion.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/task-review-slots-and-claim-context-fusion.md)
  - [parallel-team-coordination.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/parallel-team-coordination.md)
  - [current-priority-gap-list.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/current-priority-gap-list.md)

## 3. 当前基线

当前已经完成的部分：

- teammate 仍通过 `TaskReviewSlot` 写 task-level judgment
- leader 已能对 review item 自动发布 `TeamPositionReview`
- leader 已能对 `PROJECT_ITEM` 基于其他 team 的 leader-level position 自动发布 `CrossTeamLeaderReview`
- superleader 已能在 objective finalize seam 自动发布 `SuperLeaderSynthesis`
- in-memory 与 PostgreSQL 都已经能持久化这四层 review artifact
- `GroupRuntime` 已经把 review item 创建、review 发布、project-item context 读取收口成正式 API

当前 first-cut 的明确边界：

- 这是 review layer，不是 task/authority layer
- 跨 team 仍坚持 summary-first，不默认暴露 raw teammate review
- review artifact 已能被 runtime 产出和读取，但还没有完全变成更强的持续协调面

## 4. residual gap 总表

### 4.1 P0: review artifact 进入正式协作面

1. `review-digest-subscription-surface`
   - 现状：leader/superleader 已经会产出 review artifact，但这些 artifact 主要还停在 store + runtime helper 里。
   - gap：review artifact 还没有正式进入 shared digest / subscription surface，因此它们还不能像 canonical mailbox summary 那样参与更长期的多 agent 协作。
   - 目标：
     - 为 `TeamPositionReview / CrossTeamLeaderReview / SuperLeaderSynthesis` 定义 canonical digest 形状
     - 明确它们走哪类 subscription
     - 避免把 review envelope 混进现有 teammate mailbox follow-up 计数
   - 入口：
     - [leader_loop.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py)
     - [superleader.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/superleader.py)
     - [protocol_bridge.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/protocol_bridge.py)

2. `review-phase-state-machine`
   - 现状：spec 里已经有 `TEAM_INDEPENDENT_REVIEW / TEAM_SYNTHESIS / CROSS_TEAM_LEADER_REVIEW / SUPERLEADER_SYNTHESIS`，但代码里 phase 还没有独立收口成显式状态机。
   - gap：现在更像“有 artifact，就允许往下写”；还不是“phase 达到某门槛，才能推进下一层 review”。
   - 目标：
     - 定义 review item phase truth
     - 明确 phase gate 与 phase transition
     - 支持 freshness / stale-review 检查
   - 入口：
     - [hierarchical_review.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/contracts/hierarchical_review.py)
     - [group_runtime.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/group_runtime.py)

3. `review-visibility-and-authority-policy`
   - 现状：当前 first-cut 主要靠 runtime 调用面和 summary-first 约定保持边界。
   - gap：leader/superleader 的 review 写权限、跨 team 读取权限、是否允许下钻全文，还没有独立 contract 化。
   - 目标：
     - 明确 teammate / leader / superleader / foreign leader 对 review artifact 的可见性
     - 明确谁能创建、更新、读取哪一层 review
     - 把“summary-only / summary-plus-ref / full-text fetch”正式接进 policy
   - 入口：
     - [group_runtime.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/group_runtime.py)
     - [authority-escalation-and-scope-extension.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/authority-escalation-and-scope-extension.md)

### 4.2 P1: review object 进入更强协调主语义

4. `project-item-first-class-entity`
   - 现状：`project item` 现在还是 review-layer artifact，不像 `TaskCard` 那样是一等协调对象。
   - gap：这使得 phase、ownership、promotion、dependency、shared-file/contract governance 还难以统一挂载。
   - 目标：
     - 明确 `task item -> project item` 的提升路径
     - 让 project item 拥有更稳定的 identity / metadata / lifecycle
     - 使其能承载 interface contract、shared module、authority-sensitive decision
   - 入口：
     - [hierarchical_review.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/contracts/hierarchical_review.py)
     - [task.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/contracts/task.py)
     - [group_runtime.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/group_runtime.py)

5. `review-driven-coordination`
   - 现状：review artifact 已经可写可读，但对 activation / replan / authority / split 的反向驱动还很弱。
   - gap：系统现在更像“把 review 记录下来”，还不是“让 review 改变后续协调决策”。
   - 目标：
     - 定义 review 如何触发 project gate、split、replan、authority escalation
     - 定义 superleader 如何基于 cross-team disagreement 调整后续 team activation
     - 保持 review 与 authority 分离，但允许 review 成为 authority/tasking 的输入
   - 入口：
     - [leader_loop.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py)
     - [superleader.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/superleader.py)
     - [target-gap-roadmap-and-task-subtree-authority.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/target-gap-roadmap-and-task-subtree-authority.md)

### 4.3 P2: production-grade hardening

6. `review-transport-dto-and-self-hosting-evidence`
   - 现状：当前 review context 还是 runtime/store 侧强于 transport/self-hosting 侧。
   - gap：如果要把这层能力真正投喂给 self-hosting Agent Orchestra 或更长链路的 protocol bus，仍缺更正式的 transport DTO、export surface 和 evidence gate。
   - 目标：
     - 定义 review digest / review context 的 transport-safe DTO
     - 把这层能力接进 self-hosting objective / gap package
     - 为 review progression 建立 completion/evidence 语义

7. `review-query-index-hardening`
   - 现状：PostgreSQL first-cut 以 payload JSONB + 少量筛选列为主，足够功能验证。
   - gap：如果 review item 规模扩大，按 `objective_id / item_kind / team_id / target_team_id` 的查询会需要更正式的 index / query discipline。
   - 目标：
     - 补 query/index discipline
     - 明确 migration / rollout 策略
     - 把这层需求并入更大的 `postgres-persistence` 主线

## 5. 推荐执行顺序

建议顺序不要乱：

1. `review-digest-subscription-surface`
2. `review-phase-state-machine`
3. `review-visibility-and-authority-policy`
4. `project-item-first-class-entity`
5. `review-driven-coordination`
6. `review-transport-dto-and-self-hosting-evidence`
7. `review-query-index-hardening`

判断：

- 前三项先做，是因为它们决定这条 review 主线能不能成为正式协作面
- `project item` 一等化必须放在 policy 和 phase 之后，否则会先得到一个边界不清的“更大对象”
- `review-driven-coordination` 必须建立在 phase / policy / project-item 都稳定之后，否则很容易把 review 和 authority 搅在一起
- transport / self-hosting / index hardening 放在最后，更符合当前 first-cut -> productionize 的推进顺序

## 6. 不要重复做的事项

下面这些不应在下一轮被当成“从 0 到 1 的 gap”重开：

- 不要重复实现 `TeamPositionReview / CrossTeamLeaderReview / SuperLeaderSynthesis` contract
- 不要重复实现 in-memory / PostgreSQL review artifact persistence
- 不要重复实现 `GroupRuntime.create_review_item(...)` 和三层 publish API
- 不要把 cross-team 协作重新做成 raw teammate full-text broadcast
- 不要把 review layer 直接写成 authority shortcut

## 7. 与全局 backlog 的关系

这份文档是 hierarchical review 这条子主线的 residual gap 清单，不替代全局 backlog。

它和全局 backlog 的关系应该理解为：

- 在 [current-priority-gap-list.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/current-priority-gap-list.md) 里，这条线主要属于：
  - `online-collaboration-runtime`
  - `cross-team-collaboration-layer`
  - `task-subtree-authority` 的输入面
- 在 [remaining-work-backlog.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/remaining-work-backlog.md) 里，这条线不是独立替代全局 backlog，而是下一步推进“长期在线协作 + 跨 team 知识融合”时的一个明确子包

## 8. 相关文档

- [hierarchical-review-and-cross-team-knowledge-fusion.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/hierarchical-review-and-cross-team-knowledge-fusion.md)
- [task-review-slots-and-claim-context-fusion.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/task-review-slots-and-claim-context-fusion.md)
- [parallel-team-coordination.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/parallel-team-coordination.md)
- [current-priority-gap-list.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/current-priority-gap-list.md)
- [remaining-work-backlog.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/remaining-work-backlog.md)
