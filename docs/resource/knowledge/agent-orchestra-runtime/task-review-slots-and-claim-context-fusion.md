# task review slots 与 claim 前知识融合

## 1. 一句话结论

截至 2026-04-07，task review surface 的 first-cut 已经进入主线：系统没有把 review 混进 `TaskCard`，而是新增了独立的 task-local review surface；每个 task 对每个 agent 有唯一 review slot，slot 对外语义是 update-only，对内语义是 append-only revision + reducer snapshot；runtime 已提供 `upsert_task_review(...) / list_task_reviews(...) / get_task_claim_context(...)`，in-memory 与 PostgreSQL 都支持 slot + revision 持久化，claim 后 assignment 也已经开始携带 `task_review_digest` 与 `task_review_slots`。当前这条线已经是可用的 first-cut，但仍停留在“review-aware claim context”层面，还没有进入“review-driven claim selection heuristics”。

## 2. 范围与资料来源

- 当前任务/协作主线知识：
  - [target-gap-roadmap-and-task-subtree-authority.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/target-gap-roadmap-and-task-subtree-authority.md)
  - [resident-team-collaboration-and-slice-planning.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/resident-team-collaboration-and-slice-planning.md)
  - [parallel-team-coordination.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/parallel-team-coordination.md)
- 当前代码入口：
  - [task.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/contracts/task.py)
  - [task_review.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/contracts/task_review.py)
  - [group_runtime.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/group_runtime.py)
  - [teammate_work_surface.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/teammate_work_surface.py)
  - [storage/base.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/storage/base.py)
  - [storage/in_memory.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/storage/in_memory.py)
  - [storage/postgres/models.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/storage/postgres/models.py)
  - [storage/postgres/store.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/storage/postgres/store.py)
- 当前验证：
  - [test_task_reviews.py](/Volumes/disk1/Document/code/Agent-Orchestra/tests/test_task_reviews.py)
  - [test_teammate_work_surface.py](/Volumes/disk1/Document/code/Agent-Orchestra/tests/test_teammate_work_surface.py)
  - [test_postgres_store.py](/Volumes/disk1/Document/code/Agent-Orchestra/tests/test_postgres_store.py)
- 对应 formal spec：
  - [2026-04-07-task-review-slots-and-claim-context-fusion.md](/Volumes/disk1/Document/code/Agent-Orchestra/docs/superpowers/specs/2026-04-07-task-review-slots-and-claim-context-fusion.md)

## 3. 为什么它应该是独立 surface

当前主线里已有三个不同层次：

- `TaskCard`
  - activation / ownership / dependency / authority / slice contract
- `TeamBlackboard`
  - team 级 proposal / blocker / result / evidence 流
- mailbox / subscription
  - directed communication 与摘要索引

判断：

- 如果把“每个 agent 对 task 的看法”直接塞进 `TaskCard`，task contract 会迅速膨胀
- 如果完全交给 `Blackboard`，task 级 claim 上下文又会过于分散
- 如果完全交给 mailbox，claim 前融合会退化成隐式、偶然的消息消费

因此更合适的中间层是：

- `task-local review surface`

它既不替代 `TaskCard`，也不替代 `Blackboard`，而是围绕单个 task 提供 claim 前知识融合面。

## 4. 正式主语义

### 4.1 每个 task 有一组 review slots

正式规则：

- 每个 `task_id` 对应一组 review slots
- 每个 `agent_id` 在该 task 下最多只有一个当前 review slot
- 唯一键是 `(task_id, reviewer_agent_id)`

### 4.2 review slot 对外是 update-only

产品语义：

- reviewer 只能更新自己的当前 review

存储语义：

- 每次 update 仍然应落成 append-only revision
- reducer 负责生成 `latest slot view`

判断：

- 这是和现有 append-only blackboard / snapshot 思路最一致的实现方式
- 也是保留历史认知变化、回放、恢复和审计能力的必要条件

### 4.3 review 必须带最小经验背景

review 不能只有一句模糊结论，还必须解释 reviewer 的最小背景。

最小结构建议包括：

- `reviewed_at`
- `based_on_task_version`
- `based_on_knowledge_epoch`
- `stance`
- `summary`
- `relation_to_my_work`
- `experience_context`

其中 `experience_context` 至少应支持：

- `touched_paths`

推荐再补入：

- `observed_paths`
- `related_task_ids`
- `related_lane_ids`
- `related_blackboard_entry_ids`

### 4.4 claim 前默认读取 review digest

正式规则：

- agent 在 claim task 前，默认拉取 task review digest
- 如有需要，再展开当前 review slots 的全文

这样可以做到：

- 先快速融合不同 agent 的判断
- 再按需深入看具体背景

而不是一上来就把所有 review 历史全文灌给 claimer。

### 4.5 当前已落地的 first-cut

当前主线已经具备下面这些代码事实：

- `contracts/task_review.py`
  - 已正式定义 `TaskReviewStance / TaskReviewExperienceContext / TaskReviewRevision / TaskReviewSlot / TaskReviewDigest / TaskClaimContext`
- `InMemoryOrchestrationStore`
  - 已支持 `upsert_task_review_slot(...) / list_task_review_slots(...) / list_task_review_revisions(...)`
- `PostgresOrchestrationStore`
  - 已新增 `task_review_slots / task_review_revisions` 两张表，并支持同一 `_run_write(...)` 里原子写入 revision + current slot
- `GroupRuntime`
  - 已支持 `upsert_task_review(...) / list_task_reviews(...) / get_task_claim_context(...)`
  - `upsert_task_review(...)` 现已带 reviewer ownership guard：`actor_id` 不能跨 agent 改写别人的 slot
- `TeammateWorkSurface`
  - 在 autonomous claim 与 directed claim 两条路径上，都会在 claim materialize 后调用 runtime-owned claim context API，并把 digest + latest reviews 注入 assignment `instructions` 与 `metadata`

判断：

- 当前 first-cut 已经满足“task item 能挂一组 per-agent review，并在 claim 时完成知识融合”
- 但它还没有进入“review 影响 claim 选择顺序”这一层
- 也还没有把 digest 单独持久化成存储层对象

## 5. 它和现有治理主线的关系

### 5.1 与 `task-subtree-authority` 的关系

review slot 不等于 task structure authority。

review slot 只允许表达：

- fit / risk / dependency / split suggestion / authority suggestion

review slot 不允许直接做：

- task split
- task reroute
- task cancel
- task supersede
- owner 改写
- hard contract 改写

任何真正的 task 结构动作仍然必须走：

- `task-subtree-authority`
- leader / superleader authority path

### 5.2 与 `TeamBlackboard` 的关系

`TeamBlackboard` 仍负责更广义的 team evidence 流。

review slot 负责的是：

- 单个 task 的局部认知融合
- claim 前判断入口

判断：

- `Blackboard` 是 team 级知识面
- `task review surface` 是 task 级知识面

### 5.3 与 mailbox 的关系

mailbox 仍是 directed communication 面。

review slot 不替代 mailbox，而是减少“必须靠 mailbox 才能知道别人怎么想这个 task”的耦合。

## 6. 推荐的 review contract

### 6.1 推荐的 `stance` 枚举

为了避免 review 退化成自由评论区，建议把 stance 收口为有限枚举：

- `good_fit`
- `not_fit`
- `blocked_by_dependency`
- `needs_split`
- `needs_authority`
- `duplicate`
- `high_risk`
- `uncertain`

### 6.2 推荐的 reducer 产物

至少应有：

- `latest_reviews_by_agent`
- `review_digest`
- `stance_counts`
- `last_reviewed_at`
- `stale_review_agent_ids`

### 6.3 推荐的刷新条件

不建议机械地在每次 claim 前对所有 task 全量重刷。

更合理的刷新触发是：

- task version changed
- reviewer knowledge epoch changed
- reviewer completed related work
- new relevant mailbox / blackboard evidence arrived
- slot became stale by policy

## 7. 对实现路线图的影响

这条能力更接近下面两条主线的交叉点：

1. `team-primary-semantics-switch`
   - 因为它把 task 从“派工面板”推进成“协作前的认知融合面”
2. `task-subtree-authority`
   - 因为它要求正式定义“谁能对 task 做什么写操作”

判断：

- 它不是单独替代黑板或 authority 的大主线
- 它更像 task surface 的关键补层
- 一旦落地，会明显增强 team 内不同 agent 之间的知识互相融合能力

## 8. 推荐实现边界

当前已经完成：

1. review slot / revision / digest contract
2. runtime API：`upsert/list/get_claim_context`
3. in-memory reducer snapshot
4. PostgreSQL slot + revision 持久化
5. claim 前上下文加载

当前仍建议后做：

1. cross-team digest visibility
2. staleness policy
3. review-based claim heuristics
4. PostgreSQL query/index 优化

## 9. 相关文档

- [2026-04-07-task-review-slots-and-claim-context-fusion.md](/Volumes/disk1/Document/code/Agent-Orchestra/docs/superpowers/specs/2026-04-07-task-review-slots-and-claim-context-fusion.md)
- [target-gap-roadmap-and-task-subtree-authority.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/target-gap-roadmap-and-task-subtree-authority.md)
- [resident-team-collaboration-and-slice-planning.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/resident-team-collaboration-and-slice-planning.md)
- [parallel-team-coordination.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/parallel-team-coordination.md)
