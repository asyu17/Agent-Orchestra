# multi-leader draft review 与 revision planning

## 1. 一句话结论

对于 `agent_orchestra` 的多 team planning，正式推荐主语义不再是“各 leader 本地切完 slice 就直接 activation”，而是：`SuperLeaderObjectiveBrief -> LeaderDraftPlan -> all-to-all LeaderPeerReview -> SuperLeaderGlobalReview -> runtime-built LeaderRevisionContextBundle -> LeaderRevisedPlan -> ActivationGate -> runtime resource lease`。其中 peer review 默认由每个 leader review 其他所有 leader，review 默认对所有 leader 可见，但 leader 在 revision 阶段消费的是 runtime 自动整理好的 summary-first context bundle，而不是手工去拉 review 或逐条回执 disposition。到当前代码状态，这条链已经不只存在于 runtime/store：它已经切成 superleader/default self-hosting 主路径，`ActivationGateDecision` 也已经被提升成 objective live truth 与 self-hosting packet truth。

## 2. 范围与资料来源

- 直接相关知识：
  - [hierarchical-review-and-cross-team-knowledge-fusion.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/hierarchical-review-and-cross-team-knowledge-fusion.md)
  - [parallel-team-coordination.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/parallel-team-coordination.md)
  - [resident-team-collaboration-and-slice-planning.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/resident-team-collaboration-and-slice-planning.md)
  - [task-review-slots-and-claim-context-fusion.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/task-review-slots-and-claim-context-fusion.md)
  - [target-gap-roadmap-and-task-subtree-authority.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/target-gap-roadmap-and-task-subtree-authority.md)
- 对应 formal spec：
  - [2026-04-08-multi-leader-draft-review-revision-planning.md](/Volumes/disk1/Document/code/Agent-Orchestra/docs/superpowers/specs/2026-04-08-multi-leader-draft-review-revision-planning.md)
- 背景实现入口：
  - [leader_output_protocol.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_output_protocol.py)
  - [leader_loop.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py)
  - [superleader.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/superleader.py)
  - [group_runtime.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/group_runtime.py)

## 3. 为什么这条新主语义是必要的

### 3.1 只靠 leader 本地切 slice 不够

当前 leader 已经能输出 `sequential_slices / parallel_slices`，但这仍然主要是 team-local 视角。它适合表达：

- 本 team 内部哪些工作串行
- 本 team 内部哪些工作可并行
- 本 team 认为自己会写哪些 path

但它不擅长发现跨 team 的这些问题：

- 两个 team 的第一刀其实都要碰同一 shared hotspot
- 共享 test / 共享 contract 会在不同 lane 中重复改
- interface contract 的假设不一致
- 验证路径分布不合理，最后集中撞到同一组共享测试

### 3.2 只靠 runtime lease 也不够

即使后续引入 `resource lease / path lease`，那也只是执行前的最后一道硬防线。它能阻止冲突同时发生，但不能提前把明显不合理的 planning 形状改掉。

如果没有 planning review，系统会退化成：

- 各 leader 都觉得自己能并行
- activation 后才在 shared file 前排队
- 冲突不再失败，但吞吐和协作质量都下降

### 3.3 只靠 superleader 单审也不够

仅依赖 superleader 去读所有 draft plan，横向知识融合不足。不同 leader 各自看到的 shared module、接口风险、测试冲突和 authority 细节，往往正是对方最应该知道的东西。

因此更稳的做法是：

- 先让 leader 独立 draft
- 再让 leader 互评
- 最后由 superleader 汇总仲裁

## 4. 当前推荐形态

### 4.1 先独立 draft

每个 leader 先提交自己的 `LeaderDraftPlan`。这一阶段不要求先看其他 leader 的 plan 细节，目的是避免一开始就 herd effect。

### 4.2 all-to-all peer review

每个 leader 默认 review 其他所有 leader。这是新的正式默认语义，而不是可选优化项。

判断：

- 这会带来更强的横向冲突发现
- 也会把不同 team 的局部知识带到 planning 层融合

### 4.3 review 默认全 leader 可见

每条 `LeaderPeerReview` 默认对所有 leader 可见，superleader 也可见。

但这里的“可见”不是指无差别全文广播。更准确的正式理解是：

- store truth 默认全 leader 可读
- revision 阶段自动注入的是 summary-first digest
- 正文通过引用索引按需下钻

### 4.4 superleader 做 global review

superleader 读取：

- 全部 draft
- 全部 peer review

然后产出 `SuperLeaderGlobalReview`，集中处理：

- 真正成立的全局冲突
- 必须串行化的 slice
- 需要升级为 `project item` 的共享对象
- authority / scope / interface 的裁决

### 4.5 revised plan 不做逐条回执

leader 在提交 `LeaderRevisedPlan` 前，不需要像工单系统一样对每条 review 单独写：

- accepted
- rejected
- partially accepted

这里的正式判断是：

- leader 的 revised plan 自身就是它对本轮 review 网络的回应
- 系统只需保证 revised plan 生成时已经拥有完整 review context

## 5. runtime 自动注入 revision context

这是本轮新决议中最重要的细节之一。

leader 不需要在 revision 阶段手工先去“读取所有 review”。更正式的主语义是：

- runtime 收齐本轮 peer review 与 superleader review
- runtime 为每个 leader 构建一个 `LeaderRevisionContextBundle`
- revision turn 启动时，系统自动把该 bundle 注入 leader 上下文

这样，“leader 在 revised plan 前已阅读所有 review”这件事，变成了系统保证的上下文前提，而不是 leader 的额外流程动作。

### 5.1 为什么这样更自洽

原因有 3 个：

- 它更符合长期在线 agent 的主语义，减少流程性噪声
- 它把 review truth 与 leader 实际消费的 revision view 分开，便于控制上下文体积
- 它更利于后续接入 mailbox / digest / protocol bus，而不是要求 leader 手工执行 review 拉取动作

### 5.2 为什么不能默认注入所有全文

如果直接把所有 review 原文无差别塞进 revision prompt，会出现两个问题：

- 上下文爆炸
- herd effect 增强

因此正式推荐的是：

- 默认注入结构化 digest
- 高严重度、直接相关的冲突可附带更长正文
- 其余内容只暴露引用索引

## 6. 这条主语义与现有知识主线的关系

### 6.1 与 hierarchical review 的关系

`hierarchical review` 关注的是 knowledge fusion：

- `TaskReviewSlot`
- `TeamPositionReview`
- `CrossTeamLeaderReview`
- `SuperLeaderSynthesis`

而本文固化的是 planning reconciliation：

- `LeaderDraftPlan`
- `LeaderPeerReview`
- `SuperLeaderGlobalReview`
- `LeaderRevisedPlan`

两条线高度相关，但不相同。

判断：

- 前者偏“项目判断与立场融合”
- 后者偏“activation 前的 planning 冲突排查与计划修正”

### 6.2 与 resident collaboration 的关系

这条 planning/review 主语义不会替代 resident collaboration。它只是 activation 前的 planning 层。

执行阶段的主语义仍然是：

- task 只负责 activation / contract
- runtime 负责持续协作推进
- teammate 常驻执行
- leader 负责 activation 和 convergence

### 6.3 与 runtime lease 的关系

planning review 和 resource lease 的关系应该理解为：

- planning review：提前发现可预见冲突
- runtime lease：处理实际执行时仍残留的争用

顺序不能反：

1. planning review
2. activation gate
3. runtime lease

## 7. 约束与边界

### 7.1 允许的事

- leader review 所有其他 leader
- 所有 leader 看所有 peer review 的摘要
- superleader 基于 draft + peer review 做最终仲裁
- revised plan 在系统自动注入的 review context 中生成

### 7.2 不允许的事

- leader 直接改写别人的 draft plan
- 把 peer review 做成无结构自由聊天
- 在 revised plan 阶段再对 review 本身做无限 review
- 默认把所有 review 全文广播给所有 leader

### 7.3 默认回合数

正式推荐：

- 一轮 draft
- 一轮 all-to-all peer review
- 一轮 superleader global review
- 一轮 revised plan

如果此时仍有重大冲突，应升级成显式问题对象，而不是继续开无穷 planning 会议：

- `project item`
- `coordination issue`
- `authority issue`
- `runtime resource conflict`

## 8. 后续实现建议

截至当前代码状态，下面这些对象已经进入 first-cut 主线：

- `LeaderDraftPlan`
- `LeaderPeerReview`
- `SuperLeaderGlobalReview`
- `LeaderRevisionContextBundle`
- `LeaderRevisedPlan`
- `ActivationGateDecision`

已经落地的事实包括：

- contracts、in-memory/PostgreSQL store CRUD、`GroupRuntime` 发布/查询 API 已具备
- `SuperLeaderRuntime` 已支持 pre-activation `draft -> peer review -> global review -> revision`
- `ActivationGateDecision` 已真实发布到 store，并同步写进 objective live metadata
- self-hosting bootstrap 已能把 `multi-leader-planning-review` 当成 gap inventory 项，并在 instruction packet 中导出 `planning_review_status`

后续重点实现方向已经从 0 到 1 切换为 hardening：

- 让 `ActivationGateDecision` 不只是 objective finalize truth，也进入更实时的 activation/live coordination gate
- 把 `required_authority_attention / required_project_item_promotion / required_serialization` 这些裁决从当前 first-cut 空位推进成真实全局 review 产物
- 与 `project item`、`resource lease`、`authority issue` 的接线继续变强，而不是停留在 blocker 计数

## 9. 相关文档

- [2026-04-08-multi-leader-draft-review-revision-planning.md](/Volumes/disk1/Document/code/Agent-Orchestra/docs/superpowers/specs/2026-04-08-multi-leader-draft-review-revision-planning.md)
- [hierarchical-review-and-cross-team-knowledge-fusion.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/hierarchical-review-and-cross-team-knowledge-fusion.md)
- [parallel-team-coordination.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/parallel-team-coordination.md)
- [resident-team-collaboration-and-slice-planning.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/resident-team-collaboration-and-slice-planning.md)
