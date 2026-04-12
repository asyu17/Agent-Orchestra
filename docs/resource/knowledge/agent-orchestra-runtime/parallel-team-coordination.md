# agent_orchestra 多 Team 并行协调

## 1. 一句话结论

在 `agent_orchestra` 的 group 级目标模型里，多 team 应当按 `Spec DAG` 的依赖关系并行推进，按层级 `Blackboard` 交换运行证据，并通过 scope/authority 限制防止互相越权修改；当前代码已经具备 leader 创建 teammate task、lane/team blackboard、budgeted parallel teammate batches、leader 在 base turn budget 用尽后继续消费 teammate mailbox 的 bounded linger 能力、leader loop 在空闲 teammate slot 上从 team task list 补位派发 `pending/unowned/unblocked` 任务并复用 teammate session，以及 `SuperLeaderRuntime` 的 dependency-gated ready-lane 并发调度与共享 resident coordination shell。当前真正仍属于推荐运行模型与设计判断的部分，已经收缩到“把 teammate/worker 推进成自治常驻执行体、并继续把 mailbox/task-list 变成持续驱动面”。并且从本轮起，team 级正式目标语义已额外明确为：不再保留 `max_concurrency` 作为正式 budget，leader 输出必须区分 `sequential / parallel slices`，team runtime 必须继续推向 resident collaboration。

## 2. 范围与资料来源

- 当前框架知识：
  - [agent-orchestra-framework.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/agent-orchestra-framework.md)
  - [architecture.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/architecture.md)
  - [implementation-status.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/implementation-status.md)
- 当前代码入口：
  - [superleader.py#L78](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/superleader.py#L78)
  - [leader_output_protocol.py#L170](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_output_protocol.py#L170)
  - [group_runtime.py#L220](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/group_runtime.py#L220)
  - [group_runtime.py#L306](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/group_runtime.py#L306)
- 本轮讨论沉淀：
  - 多 team 是否可以并行
  - 哪些 lane 适合直接并行、哪些 lane 需要 gate
  - 哪些信息应写入 `LeaderLaneBlackboard / TeamBlackboard`
  - 哪些关系应沉淀到 `Spec DAG / task list / interface contract`
  - [resident-team-collaboration-and-slice-planning.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/resident-team-collaboration-and-slice-planning.md)

## 3. 当前代码事实

### 3.1 当前已有的事实

当前代码已经具备下面这些与多 team 协作直接相关的事实：

- `Leader` 如果输出非空 `sequential_slices / parallel_slices`，runtime 会把它们落成 team-scope `TaskCard`，并把 `slice_id / depends_on / parallel_group` 写进 assignment metadata 与 execution report：[leader_output_protocol.py#L162](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_output_protocol.py#L162), [leader_output_protocol.py#L326](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_output_protocol.py#L326)
- `GroupRuntime.apply_execution_guard(...)` 已经是 runtime-owned 的强约束 gate，会在 worker 返回后计算 `modified_paths`、校验 `owned_paths`、执行 `verification_commands`，再决定最终 worker 是否仍算成功：[group_runtime.py#L220](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/group_runtime.py#L220)
- `LeaderLaneBlackboard / TeamBlackboard` 已经按 append-only entry log + reducer snapshot 落到 runtime 主线：[group_runtime.py#L306](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/group_runtime.py#L306)
- `TeammateWorkSurface.run(...)` 现在会按激活的 resident teammate slot 数推进 team 内并行执行，slot 数由 `max_teammates` 和 live activation 共同决定，而不再由额外的 `max_concurrency` budget 字段控制；这一点已有针对性回归测试覆盖：[teammate_work_surface.py#L1046](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/teammate_work_surface.py#L1046), [test_leader_loop.py#L1133](/Volumes/disk1/Document/code/Agent-Orchestra/tests/test_leader_loop.py#L1133)
- `LeaderLoopSupervisor.run(...)` 现在不再是“base leader turn budget 一耗尽就立即失败”的严格 bounded loop：当 unread teammate mailbox 仍存在，或持久化 `DeliveryState.metadata.pending_mailbox_count` 表示上一轮刚产出 teammate result 时，它会继续消耗 bounded `max_mailbox_followup_turns`（默认 `8`）来让 leader 在同一次运行里收敛这些结果，并把 `base_turn_budget`、`mailbox_followup_turns_used`、`mailbox_followup_turn_limit` 写进 lane delivery metadata：[leader_loop.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py)
- `LeaderLoopSupervisor.run(...)` 现在也不再只消费“leader 本轮刚创建出来的 teammate assignment”：当 `budget.max_teammates` 仍有空槽时，它会从 team task list 中继续挑出 `pending + unowned + unblocked` 的 task，补位编译为 teammate assignment；如果 `keep_teammate_session_idle=True`，同一个 teammate slot 还会在这些后续 task 之间复用 session。这意味着 overflow task 已经可以在后续 leader turn 中继续被 drain，而不是只能等 leader 再次显式重吐 assignment：[leader_loop.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py), [test_leader_loop.py](/Volumes/disk1/Document/code/Agent-Orchestra/tests/test_leader_loop.py)

### 3.2 当前还没有的事实

当前代码还没有下面这些东西：

- `SuperLeaderRuntime` 已经是 dependency-gated ready-lane 并发 scheduler，并且外层 shell 也已经进入共享 resident kernel 主线：[superleader.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/superleader.py)
- 当前还没有一个显式的“跨 team 直接通信协议”；team 之间的协调仍应理解为由上层 leader/superleader 读取黑板后再下发新的 directive
- `LeaderLoopSupervisor` 虽然已经有 mailbox、delivery state、teammate assignment ingestion、budgeted parallel teammate batches、mailbox-aware linger，以及“从 team task list 补位派发 pending task + 复用 teammate slot”的第一段 resident coordination，但它还没有把 `leader` 提升成 Claude 式的常驻 team coordinator；现在已经不是“严格一轮 leader turn 结束就必须等下一次 bounded loop”了，也不再只是“leader 本轮新吐出什么就跑什么”，但仍不是“leader 持续等待、订阅、消化未来异步 teammate 流量，且 teammate 自己长期驻留后主动 claim 任务”的长期会话

判断：

- 这意味着“team 内受 budget 约束的并行 teammate 执行”已经从推荐模型进入了当前代码事实
- 也意味着“leader 在同一次运行里把刚完成的 teammate mailbox 结果收敛回来”已经从设计判断进入了当前代码事实
- 还意味着 team 级协调现在已经不是纯 mailbox follow-up：`leader -> teammate` 之间开始具备 mailbox + task-list 混合收敛形态，overflow team task 可以在后续 turn 里继续被 drain
- 但也意味着如果要继续借鉴 Claude 的 team 模式，下一优先级已经不再是“把串行改成并行”，也不再只是“让 leader 至少多跑一轮收件或补位派发 pending task”，而是把 `leader -> teammates` 和 `superleader -> leaders` 两层都继续升级成“统一 agent 抽象 + 可等待新消息的常驻 loop + 订阅/连接语义 + 受控并行 worker”的结构

### 3.3 当前代码事实与新的目标裁决

需要明确区分两件事：

1. 当前代码事实
   - 正式 budget 已不再存在 `max_concurrency`
   - team 内并行上限由 resident slot 激活数和 `max_teammates` 决定
2. 新的正式目标
   - team budget 只保留 `max_teammates`
   - 默认 team 最大 teammate 数量是 `20`
   - `Leader` 输出必须显式区分 `sequential slices` 与 `parallel slices`
   - team 的 primary execution surface 是 resident collaboration，而不是 bounded batch dispatch

判断：

- 后续知识与实现计划里，如果再提到 `max_concurrency`，默认都应理解成“已删除的旧语义”，而不是当前代码事实
- 后续如果讨论 team 并行，核心也不再是“把一批 assignment 批量放大”，而是“如何让常驻 teammate 基于 slice 关系、mailbox、task list 和 TeamBlackboard 持续协作”

## 4. 并行协调总原则

### 4.1 并行的前提

判断：

- lane 可以并行，但不能无条件乱并行
- 能否并行，应由 `Spec DAG` 的 hard dependency、scope 边界和 `owned_paths` 决定
- 共享证据走 Blackboard，共享依赖走 DAG，共享执行工作面走 task list

推荐约束：

1. 只有在 `owned_paths` 不冲突、且不存在 hard dependency 时，lane 才应直接并行推进。
2. 如果 lane 之间只有接口协商、风险提示、schema 对齐这类软耦合，则可以并行，但必须通过 Blackboard 暴露。
3. 如果 lane 的实现需要消费其他 lane 已经落地的 runtime signal 或 interface contract，则应在 DAG 中显式加 `depends_on` 或 gate。

### 4.2 黑板负责交流，DAG 负责知道

判断：

- `Blackboard` 更适合表达“最近发生了什么”
- `Spec DAG` 更适合表达“谁依赖谁、谁有权限改什么”

推荐原则：

- `Blackboard` is for communication
- `Spec DAG` is for dependency and authority

### 4.3 默认不允许 team 之间自由直连

判断：

- 为了避免 cross-team 协作退化成无治理的消息洪水，默认不应让 team 之间直接互相改节点或随意写对方的黑板
- 更稳的做法是：各 team 把 proposal / blocker / interface request 写到自己的 lane blackboard，由 `SuperLeader` 读取后对其他 lane 下发新的 directive 或 decision
- 对 Claude 的借鉴也应注意边界：Claude team 里默认并不是“所有消息全员自动可见”，而是 recipient-scoped inbox + 显式 broadcast；因此在 `agent_orchestra` 里做 cross-team 机制时，也不应默认落成“所有 lane 共享一个全局消息池”

推荐原则：

- 同 team 内可以保留 `TeamBlackboard` 全量可见
- 但同 team 内的 mailbox 仍应默认 recipient-scoped，而不是无差别群聊
- cross-team 默认走 `SuperLeader` 中转
- 只有少数被授权的 interface request / contract notice 才允许进入受控的 cross-team routing

### 4.4 借鉴 Claude 时要先复制“层级调度形状”，不是先复制 transport 细节

判断：

- Claude 的强项不只是 mailbox 本身，而是“每一层 leader 都是异步收敛中心”
- 对 `agent_orchestra` 来说，最值得先借鉴的是这个调度形状：
  - `SuperLeader` 像 Claude 的 team leader 一样，面向多个 `Leader` lane 异步收敛
  - `Leader` 像 Claude 的普通 team leader 一样，面向多个 `Teammate` 异步收敛
- PostgreSQL / Redis / protocol bus 是后续把它做 durable 的载体；当前虽然已经补上 budgeted parallel teammate batches、mailbox-aware linger loop，以及“从 team task list 补位派发 pending task”的第一段 queue-drain 能力，但如果调度形状仍停在“bounded turn 内代为补位派发 + 有未读消息时多跑几轮”，单纯替换 transport 仍然带不来 Claude 那种长期异步 team coordinator 效果

推荐迁移顺序：

1. 先把 `Teammate` 做成真正的常驻执行体。
2. 再把 `mailbox + task list` 推进成持续驱动的 team work surface，让 idle teammate 自主 claim `pending/unowned/unblocked` 工作。
3. 最后再把 mailbox / cursor / lease / reconnect 做成 PostgreSQL + Redis 支撑的 durable resident worker 主线路径。

## 5. 四个 Team 的推荐运行图

下面这张文本运行图对应当前讨论过的四个 team：

- `worker-lifecycle`
- `provider-fallback`
- `runtime-infrastructure`
- `planner-feedback`

它描述的是推荐并行关系，不是当前代码已经实现的 scheduler 行为。

### 5.1 `worker-lifecycle`

职责：

- 扩展 worker 从 bounded one-shot assignment 走向 idle / reactivate / session-aware 生命周期

推荐并行关系：

- 可与 `provider-fallback` 并行开展调查和测试
- 可与 `runtime-infrastructure` 并行开展 schema 设计和存储准备
- 不依赖 `planner-feedback`

推荐依赖：

- hard dependency：无
- soft dependency：`provider-fallback` 需要读取它暴露的 session / exhaustion 语义

应该向 lane blackboard 暴露：

- worker session 状态机 proposal
- idle/reactivate 语义变更摘要
- 与 fallback / persistence 相关的 blocker
- leader->teammate 路径是否仍然 green 的验证摘要

应该沉淀的接口/契约：

- worker session state / session metadata
- reactivate 能力边界
- exhaustion 与普通 failure 的分类信号

### 5.2 `provider-fallback`

职责：

- 在 bounded retry / resume 耗尽后，决定 fallback / backoff / escalation routing

推荐并行关系：

- 可以与 `worker-lifecycle` 并行
- 可以与 `runtime-infrastructure` 并行准备 provider routing 所需的 envelope / persistence 结构
- 不建议先于 `worker-lifecycle` 的状态语义稳定后直接定稿

推荐依赖：

- hard dependency：建议依赖 `worker-lifecycle` 暴露 exhaustion / session 相关 contract 后再进入最终收敛
- soft dependency：读取 `runtime-infrastructure` 提供的 protocol / persistence 约束

应该向 lane blackboard 暴露：

- provider exhaustion taxonomy
- fallback strategy proposal
- 需要 `WorkerSupervisor` 或 backend 暴露的新 metadata
- 当前 provider availability 风险

应该沉淀的接口/契约：

- exhaustion routing metadata schema
- fallback decision / backoff policy contract
- escalation reason 分类

### 5.3 `runtime-infrastructure`

职责：

- 把 Redis mailbox / protocol bus 和 PostgreSQL persistence 推向主线路径

推荐并行关系：

- 可与 `worker-lifecycle` 并行
- 可与 `provider-fallback` 并行
- 可先做 schema / adapter / transport 主线准备，再等其他 lane 的 contract 稳定后接线

推荐依赖：

- hard dependency：无
- soft dependency：读取 `worker-lifecycle` 与 `provider-fallback` 最终需要持久化或传输的信号结构

应该向 lane blackboard 暴露：

- envelope schema proposal
- persistence coverage report
- protocol bus cutover blocker
- 哪些 runtime state 已进入 durable truth、哪些仍在 transient path

应该沉淀的接口/契约：

- mailbox / protocol envelope schema
- PostgreSQL CRUD coverage contract
- reconnect cursor / worker session persistence mapping

### 5.4 `planner-feedback`

职责：

- 在 bounded dynamic planning 基础上引入 runtime feedback / evaluator memory / adaptive replan

推荐并行关系：

- 可以先以只读模式并行调研
- 更适合在 `worker-lifecycle`、`provider-fallback`、`runtime-infrastructure` 至少暴露出第一轮 feedback / metadata 之后再进行实装

推荐依赖：

- hard dependency：建议依赖前面三个 lane 至少产出一轮被接受的 interface contract 或 feedback schema
- soft dependency：读取 evaluator / mailbox / authority 结果

应该向 lane blackboard 暴露：

- replan trigger proposal
- evaluator metadata requirement
- planner memory shape proposal
- adaptive lane budgeting 建议

应该沉淀的接口/契约：

- planning feedback schema
- replan trigger contract
- planner memory / evaluator summary contract

## 6. 哪些信息放到哪里

### 6.1 放到 `LeaderLaneBlackboard`

适合放 lane 级共享证据和跨 team 协调输入：

- `proposal`
- `blocker`
- `interface_request`
- `schema_notice`
- `verification_summary`
- `risk`
- `merge_readiness`
- `decision_request`

判断：

- 这些内容是“围绕契约的运行态”，不应该直接写进 DAG 节点本体
- 对上层默认暴露的应是摘要和引用索引，而不是全文倾倒
- `SuperLeader` 默认消费 `summary + entry_id/artifact_ref/task_id` 即可，只有遇到高优先级问题时再继续追全文

### 6.2 放到 `TeamBlackboard`

适合放 team 内局部执行信息：

- `directive`
- `artifact_ref`
- `execution_report`
- `verification_result`
- `local_blocker`
- `summary_snapshot`

判断：

- `TeamBlackboard` 应优先服务 leader 与 teammate 的局部执行协同，而不是承载跨 team 的正式依赖
- `TeamBlackboard` 可以保持 team 内高可见性，但跨 team 默认不应直接读取其全文

### 6.3 放到 `Spec DAG`

适合沉淀必须被系统长期知道的 hard structure：

- lane 是否存在
- lane 的 `depends_on`
- gate
- `supersedes`
- scope / authority 关系

判断：

- 只要某个依赖必须影响调度顺序、权限边界或完成语义，它就应该进 DAG，而不是只停留在黑板对话里

### 6.4 放到 task list

适合沉淀当前可执行工作项：

- 当前要做的实现任务
- 当前要跑的验证任务
- 当前被 blocker 卡住的工作项
- 当前由 teammate 新发现并创建的 team-scope 任务

判断：

- task list 是“运行时操作投影”，不是事实上的上层契约图

### 6.5 放到 interface contract

当多个 lane 会反复消费同一种 shared schema 或 shared signal 时，应把它从黑板对话提升为显式 interface contract。

典型例子：

- worker session state schema
- provider exhaustion / fallback metadata schema
- mailbox envelope schema
- planner feedback / evaluator summary schema

判断：

- interface contract 不是一次性聊天结果，而是跨 lane 持续复用的共享边界
- 它通常先在 `LeaderLaneBlackboard` 中以 proposal 形式出现，被 `SuperLeader` 接受后，再通过 DAG / metadata / code contract 固化

## 7. 推荐的摘要 / 全文分层规则

### 7.1 同 team 的默认可见性

推荐保持接近 Claude 的 team 结构：

- roster 是全员可知的
- task/work surface 在同 team 内共享得更多
- mailbox 默认 recipient-scoped，而不是全员共享聊天室

同时增加一条增强规则：

- 所有 team 内消息默认带简短摘要
- 需要时再沿引用索引查看全文
- 如果后续升级为订阅式消息池，则同 team 默认应订阅本 team 的共享协作消息流

判断：

- 这既保住了 Claude 式局部协作效率
- 也避免 leader 被所有原始全文淹没

补充判断：

- 如果你希望 team 内形成更高密度协作感，可以把“共享协作消息”默认做成 team-wide subscription
- 但不应把所有 `control/private` 消息也一并默认广播

### 7.2 跨 team 的默认可见性

跨 team 默认不应共享全文，而应共享：

- `summary`
- `artifact_ref`
- `source_entry_id`
- `task_id`
- 必要的 `kind / severity / contract area`

推荐理解为：

- cross-team 默认看到的是“索引卡片”
- 不是“全文消息副本”

这样做的好处是：

1. `SuperLeader` 和其他 lane leader 可以先快速判断相关性。
2. 真正需要时，再按引用进入全文。
3. 多 team 时不会因为广播全文而把 mailbox 变成噪音流。

如果后续改成订阅式消息池，则推荐：

- 非本 team 默认没有全文订阅
- 只自动收到跨 team 摘要卡片
- 如有协作需要，agent 可以显式创建临时订阅或窄范围订阅

### 7.3 适合作为跨 team 默认共享的字段

推荐跨 team 默认共享字段：

- `summary`
- `entry_kind`
- `severity`
- `artifact_ref`
- `task_id`
- `source_entry_id`
- `source_team_id`
- `source_lane_id`

推荐默认不直接共享的内容：

- 长文本执行日志
- 原始大段推理
- 本 team 内部的反复进展消息
- 非 blocker 的原始 stdout / stderr 全文

推荐新增的 subscription 维度：

- 按 team 订阅
- 按单个 agent 订阅
- 按 `entry_kind / severity / tag` 订阅
- 按时间窗口或 cursor 范围做一次性拉取
- 按 delivery mode 订阅：
  - `summary_only`
  - `summary_plus_ref`
  - `full_text`

### 7.4 对实现的含义

如果后续按这个模型实现，则：

- `MailboxEnvelope` 应补显式 `summary`
- `payload/metadata` 应统一承载 `full_text_ref` / `artifact_ref` / `source_entry_id`
- `LeaderLoop` 应先消费摘要，再按需展开
- `SuperLeader` 默认只消费 lane digest 与 cross-team summary card

并且推荐把 transport 进一步抽象成：

- append-only `MessagePool`
- `SubscriptionSpec`
- `SubscriptionCursor`
- materialized `InboxView / DigestView`

这样：

- “消息写入”只做一次
- “谁能看到什么”由 subscription 决定
- “只取单次某个范围消息”也能作为标准能力, 而不是临时查询逻辑

### 7.5 推荐的 subscription 约束

为了避免 cross-team 订阅失控，建议默认约束：

1. 跨 team 订阅默认带 TTL 或单次范围限制。
2. `full_text` 订阅默认需要 authority 允许。
3. 平级 lane 之间默认只允许 `summary_only` 或 `summary_plus_ref`。
4. `control/private` 消息不能因为 team-wide subscription 自动泄露给所有人。
5. `SuperLeader` 对下层可天然拥有更高可见性，但平级 team 不应自动拥有相同权限。

## 8. 推荐执行顺序

推荐的 group 级顺序不是简单串行，而是：

1. 第一批并行：
   - `worker-lifecycle`
   - `provider-fallback`
   - `runtime-infrastructure`
2. 第二批 gated implementation：
   - `provider-fallback` 在 `worker-lifecycle` 的 exhaustion/session contract 稳定后进入最终收敛
   - `runtime-infrastructure` 在 envelope / state schema 稳定后进入主线路径接线
3. 第三批反馈收敛：
   - `planner-feedback` 在前面至少一轮执行证据与接口契约稳定后进入主实现

判断：

- `planner-feedback` 可以早期调研，但不应在没有 runtime feedback signal 的情况下过早定稿

## 9. 后续阅读 / 相关文档

- [agent-orchestra-framework.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/agent-orchestra-framework.md)
- [architecture.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/architecture.md)
- [implementation-status.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/implementation-status.md)
- [self-hosting-bootstrap.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/self-hosting-bootstrap.md)
