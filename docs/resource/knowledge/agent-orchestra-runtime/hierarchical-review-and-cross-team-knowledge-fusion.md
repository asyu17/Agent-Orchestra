# hierarchical review 与 cross-team 知识融合

## 1. 一句话结论

如果要让多 team 不只是放大并行执行规模，而是真正放大认知质量，系统不能只停留在“teammate review + leader 收敛 + superleader 汇总”的单层结构；更合理的主语义是分层 review：team 内由 teammate 写 `TaskReviewSlot`、leader 产出 `TeamPositionReview`，跨 team 由 leader 对 project item 写 `CrossTeamLeaderReview`，最后由 superleader 产出 `SuperLeaderSynthesis`。这样 team 内保持高带宽协作，跨 team 保持 leader-owned 的低噪声知识融合，而不是退化成所有 agent 互相广播或多个 team 彼此隔离。

## 2. 范围与资料来源

- 直接相关知识：
  - [task-review-slots-and-claim-context-fusion.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/task-review-slots-and-claim-context-fusion.md)
  - [parallel-team-coordination.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/parallel-team-coordination.md)
  - [target-gap-roadmap-and-task-subtree-authority.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/target-gap-roadmap-and-task-subtree-authority.md)
  - [resident-team-collaboration-and-slice-planning.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/resident-team-collaboration-and-slice-planning.md)
- 对应 formal spec：
  - [2026-04-07-hierarchical-review-and-cross-team-knowledge-fusion.md](/Volumes/disk1/Document/code/Agent-Orchestra/docs/superpowers/specs/2026-04-07-hierarchical-review-and-cross-team-knowledge-fusion.md)
- 当前已存在的底座：
  - [task_review.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/contracts/task_review.py)
  - [group_runtime.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/group_runtime.py)
  - [teammate_work_surface.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/teammate_work_surface.py)

## 3. 为什么只靠单 team review 不够

当前已经有的 task review surface 主要解决的是：

- team 内 claim 前知识融合
- per-task、per-agent 的局部判断沉淀

但如果系统继续停在这里，多 team 很容易退化成：

- 多个 team 各做各的
- 最后由 superleader 做结果级汇总

这能放大吞吐，但不一定能放大智能。

判断：

- 单个强协作 team 的优势在于高带宽共享上下文
- 多个 team 的优势在于异质探索和错误去相关
- 如果跨 team 没有 leader-owned 的知识融合层，多 team 常常会被一个高密度单 team 取代

## 4. 这条新主语义的关键分层

### 4.1 TaskReviewSlot

这是当前已经进入主线的底层。

作用：

- 记录 teammate 对 task item 的一手判断
- 作为 claim-time context

它不是跨 team review 本身。

### 4.2 TeamPositionReview

这是 leader 在 team 内做的第一次综合。

作用：

- 把多个 teammate review 压缩成一个 team 级立场
- 表达本 team 对 item 的当前结论、风险、依赖和推荐动作

判断：

- 跨 team 不应直接传播大量原始 teammate review
- 更稳的方式是先传播 team leader 的 position

### 4.3 CrossTeamLeaderReview

这是 leader 对其他 team 立场的 review。

作用：

- 让不同 team 的知识在 leader 层互相修正
- 把跨 team 交流控制在低噪声层

这也是“多 team 放大智能”的关键层。

### 4.4 SuperLeaderSynthesis

这是 superleader 的最终合成层。

作用：

- 读取各 team 的 position
- 读取跨 team leader review
- 形成 objective / project item 级结论

## 5. Task Item 与 Project Item 的区别

这条主语义必须显式区分两类对象。

### 5.1 Task Item

偏 team-scoped 执行面。

主要由：

- teammate review
- 本 team leader synthesis

处理。

### 5.2 Project Item

偏跨 team 或 objective-significant 的协调面。

典型包括：

- 架构决策
- interface contract
- shared module / shared file
- authority-sensitive item
- 关键 blocker
- team 间依赖项

这些 item 才应进入 cross-team leader review。

判断：

- 不是所有底层 task 都值得做跨 team review
- 跨 team review 应集中在 project item 上，否则会导致成本过高和消息洪水

## 6. 推荐的 review phase

### 6.1 team 内 phase

推荐顺序：

1. teammate 写各自 `TaskReviewSlot`
2. leader 汇总成 `TeamPositionReview`

### 6.2 跨 team phase

推荐顺序：

1. leader 读取其他 team 的 `TeamPositionReview`
2. leader 写 `CrossTeamLeaderReview`
3. superleader 做 `SuperLeaderSynthesis`

### 6.3 为什么这样分层

这样做的好处：

- team 内保持高带宽
- 跨 team 保持低噪声
- superleader 读取的是已经过一层压缩和对抗后的知识，而不是原始海量消息

## 7. 可见性与治理边界

### 7.1 teammate

可做：

- 写自己的 task review
- 读取 team 内 task review context

不可做：

- 改写他人 review
- 直接产出跨 team review

### 7.2 leader

可做：

- 产出 `TeamPositionReview`
- 产出 `CrossTeamLeaderReview`
- 阅读其他 team 的 leader-level position

不可做：

- 直接改写别的 team 的内部 teammate review
- 用 cross-team review 直接替代 authority / task-structure path

### 7.3 superleader

可做：

- 读取 leader-level synthesis
- 做最终合成

不可做：

- 直接越过 leader 触碰 teammate tasking 边界

## 8. 它和现有主线的关系

### 8.1 和 task review slots 的关系

这条主线是上一层，不是替代层。

- `TaskReviewSlot`
  - 解决 task 级个人判断与 claim context
- `TeamPositionReview / CrossTeamLeaderReview`
  - 解决 team 级与跨 team 的知识融合

### 8.2 和 `task-subtree-authority` 的关系

review 仍然只是判断面，不是结构权限面。

即使 leader 在 cross-team review 里写了：

- `needs_split`
- `needs_authority`
- `duplicate`

也不能直接替代：

- task split
- reroute
- authority grant/deny

这些仍应走正式的 task/authority 主线。

### 8.3 和跨 team collaboration layer 的关系

这条主线本质上是 `cross-team-collaboration-layer` 的一个具体知识融合子面。

它要求：

- 跨 team 默认不是全员全文互通
- 更推荐 leader-owned summary-first review exchange

## 9. 现阶段状态与下一步

当前状态：

- `TaskReviewSlot` first-cut 已落地
- hierarchical review first-cut 现在也已进入代码主线
- 已新增 [hierarchical_review.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/contracts/hierarchical_review.py)
- `GroupRuntime` 已支持：
  - `create_review_item(...)`
  - `publish_team_position_review(...)`
  - `publish_cross_team_leader_review(...)`
  - `publish_superleader_synthesis(...)`
  - `get_project_item_review_context(...)`
- in-memory 与 PostgreSQL store 都已支持：
  - `ReviewItemRef`
  - `TeamPositionReview`
  - `CrossTeamLeaderReview`
  - `SuperLeaderSynthesis`
- `LeaderLoopSupervisor` 已在 lane convergence seam 自动发布：
  - 基于 `source_task_id + TaskReviewSlot` 的 `TeamPositionReview`
  - 基于其他 team 的 leader-level position 的 `CrossTeamLeaderReview`
- `SuperLeaderRuntime` 已在 objective finalize seam 自动发布 `SuperLeaderSynthesis`

这轮 first-cut 的主语义：

- team 内 review 继续从 `TaskReviewSlot` 起步
- leader 综合时默认使用 task review 的 revision / digest 计数，不直接把 raw teammate summary 透传成跨 team surface
- cross-team review 默认走 leader-level summary-first
- superleader synthesis 默认读取 leader-level positions 和 cross-team leader reviews，而不是直接扫描 raw teammate review

这轮仍然没有完成的部分：

- `project item` 还不是像 `TaskCard` 一样的一等协调实体，目前仍是 review-layer artifact
- review phase 还没有单独做成显式 state machine，目前 phase 仍更适合先放在 item metadata / runtime policy 中理解
- leader / superleader 还没有把 review artifact 正式接到 shared digest / subscription surface 上，目前 first-cut 主要先落 store + runtime + convergence helper
- leader/superleader 的 review 写权限与跨 team 读权限还没有独立收口成更完整的 policy/authority contract

这条线的详细 residual gap 已单独整理到：

- [hierarchical-review-residual-gap-list.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/hierarchical-review-residual-gap-list.md)

推荐的下一步顺序：

1. 对 `project item` 的 phase / visibility / policy 做正式收口
2. 把 leader / superleader review artifact 接进 shared digest / subscription surface
3. 给 review layer 加显式 staleness / freshness / changed-understanding policy
4. 再考虑是否把 `project item` 推进成更强的一等协调实体

## 10. 相关文档

- [2026-04-07-hierarchical-review-and-cross-team-knowledge-fusion.md](/Volumes/disk1/Document/code/Agent-Orchestra/docs/superpowers/specs/2026-04-07-hierarchical-review-and-cross-team-knowledge-fusion.md)
- [hierarchical-review-residual-gap-list.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/hierarchical-review-residual-gap-list.md)
- [task-review-slots-and-claim-context-fusion.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/task-review-slots-and-claim-context-fusion.md)
- [parallel-team-coordination.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/parallel-team-coordination.md)
- [target-gap-roadmap-and-task-subtree-authority.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/target-gap-roadmap-and-task-subtree-authority.md)
