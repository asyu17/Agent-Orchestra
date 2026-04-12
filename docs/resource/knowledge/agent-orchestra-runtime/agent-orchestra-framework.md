# agent_orchestra Framework

## 1. 一句话结论

`agent_orchestra` 的目标不应只是“把 Claude Code 的 team agent 搬到 Python”，而应升级成一个分层协作框架：`Spec DAG` 负责任务契约，全局统一 task list 负责运行时操作面，`SuperLeader -> Leader -> Teammate` 负责层级执行，层级 `Blackboard` 负责共享执行证据与协作态，`LaunchBackend` 负责拉起执行体，`WorkerSupervisor` 负责生命周期，`Reducer / Evaluator` 负责根据共享证据而不是根据自报状态判定完成。

## 2. 范围与资料来源

- 当前 Python 代码：
  - [runner.py#L11](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/contracts/runner.py#L11)
  - [execution.py#L10](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/contracts/execution.py#L10)
  - [team.py#L7](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/contracts/team.py#L7)
  - [task.py#L8](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/contracts/task.py#L8)
  - [events.py#L10](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/contracts/events.py#L10)
  - [base.py#L41](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/backends/base.py#L41)
  - [in_process.py#L6](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/backends/in_process.py#L6)
  - [subprocess_backend.py#L46](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/backends/subprocess_backend.py#L46)
  - [tmux_backend.py#L37](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/backends/tmux_backend.py#L37)
  - [worker_supervisor.py#L26](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/worker_supervisor.py#L26)
  - [group_runtime.py#L47](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/group_runtime.py#L47)
  - [team_runtime.py#L11](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/team_runtime.py#L11)
  - [reducer.py#L8](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/reducer.py#L8)
  - [redis_bus.py#L17](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/bus/redis_bus.py#L17)
  - [adapter.py#L34](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runners/openai/adapter.py#L34)
- Claude Code team agent 证据：
  - [TeamCreateTool.ts#L128](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/tools/TeamCreateTool/TeamCreateTool.ts#L128)
  - [prompt.ts#L37](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/tools/TeamCreateTool/prompt.ts#L37)
  - [AgentTool.tsx#L266](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/tools/AgentTool/AgentTool.tsx#L266)
  - [AgentTool.tsx#L284](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/tools/AgentTool/AgentTool.tsx#L284)
  - [spawnMultiAgent.ts#L840](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/tools/shared/spawnMultiAgent.ts#L840)
  - [useInboxPoller.ts#L118](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/hooks/useInboxPoller.ts#L118)
  - [useInboxPoller.ts#L802](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/hooks/useInboxPoller.ts#L802)
  - [attachments.ts#L3532](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/attachments.ts#L3532)
  - [inProcessRunner.ts#L1328](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/swarm/inProcessRunner.ts#L1328)
  - [inProcessRunner.ts#L1353](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/swarm/inProcessRunner.ts#L1353)
  - [SendMessageTool.ts#L434](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/tools/SendMessageTool/SendMessageTool.ts#L434)
- 已固化知识：
  - `architecture.md`
  - `claude-team-mapping.md`
  - `../claude-code-main/agent-team.md`
  - `../multi-agent-team-delivery/delivery-contracts.md`
- 讨论来源：
  - 本轮关于 `Planner`、`LaunchBackend`、`WorkerSupervisor`、`SuperLeader`、`Spec DAG`、全局统一 task list、层级 `Blackboard` 和节点修改权限的设计讨论

## 3. 框架目标

这个框架的目标是把“用户提供模板化目标，系统自动拆解、执行、收敛”的链路变成一套稳定机制，而不是把所有智能都堆进一个大 agent。

目标链路应为：

1. 用户目录生成 `objective.yaml`
2. 用户填报总体描述、硬性指标、约束、预算和完成定义
3. `Planner` 将模板编译成可执行 DAG
4. `SuperLeader` 根据 DAG 启动多个 `Leader`
5. 每个 `Leader` 在自己的 team 内继续拆解并拉起 `Teammate`
6. `Reducer / Evaluator` 根据 Blackboard 中的共享证据推进 authority / objective gate
7. 必要时触发重规划或结束

判断：

- 这不是 Claude Code 现有 team agent 的直接能力
- 但它可以复用 Claude Code 已成熟的 `leader + teammate` 局部协作模式

## 4. 三层职责模型

### 4.1 `SuperLeader`

`SuperLeader` 负责全局，但不直接管理具体 worker。

它的职责应包括：

- 读取 `ObjectiveSpec`
- 构建总目标、总指标、总预算
- 创建 `LeaderTaskNode`
- 为每个 `Leader` 分配 team contract
- 决定要不要新增 / 缩减 / 重组 leader team
- 根据 `Reducer / Evaluator` 的结果决定是否进入下一轮

硬约束：

- `SuperLeader` 不能直接给 `Teammate` 下任务
- 它只能给 `Leader` 分配 `LeaderTaskCard` / `LeaderTaskNode`

### 4.2 `Leader`

`Leader` 复用 Claude Code 现有成熟模式，但职责边界要被正式化。

从 Claude Code 的 prompt 和消息链路看，现有 leader 已经承担这些职责：

- 创建 team 内任务：见 [prompt.ts#L39](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/tools/TeamCreateTool/prompt.ts#L39)
- 拉起 teammate：见 [AgentTool.tsx#L284](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/tools/AgentTool/AgentTool.tsx#L284)
- 自动接收 teammate 消息：见 [prompt.ts#L51](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/tools/TeamCreateTool/prompt.ts#L51), [useInboxPoller.ts#L802](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/hooks/useInboxPoller.ts#L802)
- 处理审批和收尾：见 [SendMessageTool.ts#L434](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/tools/SendMessageTool/SendMessageTool.ts#L434)

在 `agent_orchestra` 里，`Leader` 应进一步被约束为：

- 读取 `SuperLeader` 分配给自己的 `LeaderTaskNode`
- 在本 team 内创建 `TeammateTaskNode`
- 为 team 内 worker 分派任务和 budget
- 汇总本 team 的 artifact、verification、blocker
- 向 `SuperLeader` 提交 `TeamResult` 或 `ReplanProposal`

硬约束：

- `Leader` 不能改写总目标
- `Leader` 不能修改不属于自己 lane 的 `LeaderTaskNode`
- `Leader` 是否允许继续拉人，要受 `SuperLeader` 下发的 team budget 限制

### 4.3 `Teammate`

`Teammate` 只负责局部执行。

Claude Code 已经体现出这一点：

- teammate 输出不会自动回流成 leader 的结论，只会通过消息、idle 通知、协议消息告知 leader：见 [inProcessRunner.ts#L1328](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/swarm/inProcessRunner.ts#L1328)
- teammate 做完一轮后会 idle，等待 leader 下一次输入，而不是自己持续规划：见 [inProcessRunner.ts#L1353](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/swarm/inProcessRunner.ts#L1353)

在新体系里，`Teammate` 应只负责：

- 执行 `TeammateTaskNode`
- 产出 artifact / verification / execution report
- 必要时上报 blocker
- 向 `Leader` 提交建议，而不是直接改全局任务图

### 4.4 统一 `Agent` 抽象

判断：

- 后续不应继续为 `SuperLeader`、`Leader`、`Teammate` 维护三套完全不同的 runtime 类型
- 更稳的做法是抽象出统一 `Agent`：它是一个自治执行体，而不同层级主要通过 `RolePolicy`、`SkillSet` 和 `PromptProfile` 来装配

建议分层：

- `Agent`
  - 统一长期 loop、mailbox poll、task claim、blackboard digest 消费与 action 执行
- `RolePolicy`
  - 决定 `scope / authority / claim / spawn` 的硬边界
- `SkillSet`
  - 决定能力模块，例如 `planning / code_edit / verification / review`
- `PromptProfile`
  - 决定角色认知框架与表达风格，但不承担硬约束

这意味着：

- `SuperLeader` 不是“另一种内核”，而是统一 `Agent` 在 objective/lane scope 下的实例
- `Leader` 是统一 `Agent` 在 team coordination scope 下的实例
- `Teammate` 则是统一 `Agent` 在局部执行 scope 下的实例

后续完整方案见：

- [agent-abstraction-skill-and-prompt-system.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/agent-abstraction-skill-and-prompt-system.md)

## 5. `Spec DAG + 全局统一 Task List + 层级 Blackboard` 模型

这是当前讨论里最重要的升级点。

判断：

- 单一可变 DAG 会把“任务契约”和“执行证据”混在一起
- `Spec DAG` 适合表达上下级任务契约
- task list 适合表达运行时可操作工作面
- `Blackboard` 更适合表达层级共享的执行证据、blocker、proposal 和决策
- 如果每一级只能修改自己创建的节点，那么更稳的做法不是“契约图也做执行证据”，而是保留 `Spec DAG`，同时用统一 task list 承载操作面，再按层级引入 Blackboard

### 5.1 `Spec DAG`

`Spec DAG` 表示“应该做什么”。

节点类型建议包括：

- `ObjectiveNode`
- `LeaderTaskNode`
- `TeammateTaskNode`
- `ObjectiveGateNode`
- `TeamGateNode`

这张图的特点是：

- 节点是契约，不是运行日志
- 只有创建者或其所属 reducer 能修改状态
- 下级不能直接改上级任务定义

### 5.2 全局统一 task list，按 scope 暴露视图

task list 不再拆成两套完全独立的数据结构，而是：

- 物理上：一个全局统一的 task list 存储层
- 逻辑上：多个按 `scope` 暴露的视图

scope 至少包括：

- `objective`
- `leader_lane`
- `team`

task 运行时语义尽量与 Claude 保持一致，核心仍然是：

- `subject`
- `description`
- `owner`
- `status`
- `blockedBy`

直接证据：

- 新任务默认 `pending` 且无 owner: [prompt.ts#L12](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/tools/TaskCreateTool/prompt.ts#L12), [TaskCreateTool.ts#L80](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/tools/TaskCreateTool/TaskCreateTool.ts#L80)
- teammate 将任务置为 `in_progress` 时，若无 owner 则自动认领给自己: [TaskUpdateTool.ts#L185](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/tools/TaskUpdateTool/TaskUpdateTool.ts#L185)

当前已确认的可见性规则是：

- 上层可以看到下层全部任务
- 下层只能看到本 scope 任务

也就是：

- `SuperLeader` 可以看全部 `leader_lane` 和 `team` 任务
- `Leader` 可以看自己 lane 与自己 team 的任务
- `Teammate` 只能看自己 `team scope` 的任务

这意味着：

- `Spec DAG` 继续做契约真相源
- task list 只是它的运行时操作投影

### 5.3 `LeaderLaneBlackboard`

`LeaderLaneBlackboard` 由 `SuperLeader <-> 某一个 Leader` 共享。

它承载的不是任务契约，而是 lane 级共享工作面，建议包括：

- `directive`
- `artifact_ref`
- `execution_report`
- `verification_result`
- `blocker`
- `proposal`
- `decision`
- `budget_update`
- `summary_snapshot`

这块黑板的特点是：

- `SuperLeader` 和对应 `Leader` 共享同一工作面
- `Leader` 可以持续上报 lane 内执行证据和重规划建议
- `SuperLeader` 可以下发 directive、decision 和 budget 调整
- 它不替代 `LeaderTaskNode`，只是承载围绕 `LeaderTaskNode` 的执行态

### 5.4 `TeamBlackboard`

`TeamBlackboard` 由 `Leader <-> 该 team 全体 Teammate` 共享。

它承载 team 内共享工作面，建议包括：

- `directive`
- `artifact_ref`
- `execution_report`
- `verification_result`
- `blocker`
- `proposal`
- `decision`
- `budget_update`
- `summary_snapshot`

当前讨论已经确认：

- `TeamBlackboard` 对同 team 的所有 `Teammate` 全量可见
- 不做“leader 全量、teammate 裁剪版”的权限裁剪

判断：

- 这比“leader 与每个 teammate 各有一块私有黑板”更适合 team 内协作
- 也更接近 Claude Code 目前 task list + mailbox + 自动消息投递形成的共享协调面

### 5.5 Team 可见性与消息模型

推荐把 team 级协作面拆成 3 层, 尽量贴近 Claude 的局部模式:

1. `roster` 是 team 内全员可知的
2. `task/work surface` 在同 team 内共享得更多
3. `mailbox` 默认按收件人隔离, 而不是全员共享聊天室

也就是:

- `Leader` 对 team 成员表应完整可见
- `Teammate` 至少应知道本 team 中有哪些成员和各自角色, 但不等于能读到所有私信
- team scope 的 task list / TeamBlackboard 是共享工作面
- team mailbox 默认仍是 recipient-scoped inbox

判断：

- 这和 Claude 的 team 语义更接近
- 也更适合把“共享执行上下文”和“点对点控制消息”分开
- 否则 team 内很容易退化成全文噪音流

### 5.6 摘要优先、全文按引用查看

推荐再加一层默认规则:

- 所有 mailbox / blackboard / cross-team 通知都默认带简短摘要
- 全文内容默认不直接跨层泛洪, 而是通过引用索引按需查看

在本框架里, 可以把它理解成:

- `summary`：给上层或旁路协作者快速判断“这条信息值不值得进一步看”
- `full_text_ref` / `artifact_ref` / `entry_id` / `task_id`：给需要深挖的人继续追全文

推荐默认行为：

- 同 team 内:
  - `TeamBlackboard` 仍可放较完整的共享执行信息
  - `TeamMailbox` 默认仍只投递给 recipient
  - 但 envelope 里应带摘要, leader 可先看摘要决定是否展开全文
- 跨 team:
  - 默认只暴露摘要和全文引用索引
  - 不直接把全文推送给别的 team
  - 如需全文, 由接收方显式 follow reference 查看

这条规则尤其适合 `SuperLeader -> Leader -> Teammate` 的两层结构：

- `Leader` 可以处理本 team 的较细粒度信息
- `SuperLeader` 默认只看 lane 级摘要与引用
- 只有遇到 `blocker / contract break / escalation / decision_request` 时, 才值得沿引用继续看全文

### 5.7 进一步抽象成“订阅式消息池”

在上一条规则基础上, 更推荐把 mailbox 从“写给谁的离散信封”再提升一层, 变成：

- 消息池是真相源
- 订阅是可见性视图
- 摘要和全文引用是默认载荷

推荐模型：

1. 所有消息先进入 append-only `MessagePool`
2. 每条消息都带：
   - `summary`
   - `full_text_ref` / `artifact_ref`
   - `source_entry_id`
   - `source_scope`
   - `visibility_scope`
   - `severity`
   - `tags`
3. agent 不直接“拥有消息副本”，而是通过 subscription 看到属于自己的视图

这个模型的好处是：

- 不必在多 team 场景里把同一条消息复制很多份
- “谁默认能看到什么”变成 subscription / visibility policy 问题
- 后续接 Redis / PostgreSQL 时, 更适合做 cursor、ack、replay、digest 和范围检索

### 5.8 Team 内默认订阅，Cross-Team 默认摘要订阅

如果采用订阅式消息池, 推荐默认行为如下：

- 同 team 内:
  - 全员默认订阅本 team 的共享协作消息
  - 因而会形成高密度协作面, 接近你想要的 Claude 风格 team 工作感
- 非本 team:
  - 默认不订阅全文流
  - 只接收摘要卡片和引用索引

这意味着:

- team 内部默认是“高密度订阅”
- team 外部默认是“低密度摘要感知”

判断：

- 这比纯点对点 inbox 更适合集团级多 team 协作
- 也比“全局所有人共享一个消息池全文流”更稳

### 5.9 仍然要区分 `shared` 和 `control/private`

即使采用订阅池, 也不建议把所有 team 内消息都默认全文广播。

推荐至少分成两类：

- `shared`
  - `execution_report`
  - `artifact_ref`
  - `verification_summary`
  - `blocker`
  - `proposal`
  - `summary_snapshot`
- `control/private`
  - `permission_request`
  - `permission_decision`
  - `shutdown`
  - 仅针对某个 worker 的直接控制消息

推荐默认可见性：

- `shared`：同 team 默认全订阅
- `control/private`：默认仅收件人和上层协调者可见

判断：

- 如果不做这个区分, team 内很快会被 permission / control 噪音淹没
- 也会让“高密度协作”退化成“高密度打扰”

### 5.10 现有契约里可直接复用的字段

当前代码里已经有一部分字段很适合承载这套规则：

- `BlackboardEntry.summary` 和 `BlackboardSnapshot.summary`: [blackboard.py#L9](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/contracts/blackboard.py#L9)
- `TaskCard.output_artifacts`、`TaskCard.derived_from`、`TaskCard.reason`: [task.py#L8](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/contracts/task.py#L8)
- `MailboxEnvelope.subject`、`MailboxEnvelope.payload`、`MailboxEnvelope.metadata`: [mailbox.py#L16](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/tools/mailbox.py#L16)

推荐扩展：

- 在 mailbox envelope 级别增加显式 `summary`
- 在 `payload` 或 `metadata` 中统一放 `full_text_ref` / `artifact_ref` / `source_entry_id`
- 把“跨层默认只看摘要”的规则做成 runtime / UI / prompt 的共同约束, 而不是只靠使用习惯

### 5.11 为什么保留 `Spec DAG`，同时用 task list 和 Blackboard

如果只保留 `Spec DAG`，运行时操作会太重，执行证据也会不断污染任务契约。

保留 `Spec DAG`，再引入 task list 和 Blackboard，可以同时满足：

- 上层节点是契约
- task list 承载可操作工作项
- 执行层共享的是“围绕契约的工作态”
- 完成状态由 reducer 推导，而不是由执行者自报
- 层级之间的共享范围天然清楚：`SuperLeader <-> Leader` 一块，`Leader <-> Team` 一块

### 5.12 `Teammate` 直接创建 team-scope 任务

这条规则明确沿用 Claude 的使用方式：

- `Teammate` 可以在当前 `team scope` 内直接创建新任务
- 新任务创建后立即可执行，采用 Claude-compatible 的 `pending + no owner`
- 不能跨 scope 创建上层任务

为了增强可追溯性，再增加两条必须字段：

- `derived_from`
- `reason`

判断：

- 这保留了 Claude 的流畅度
- 也避免 team 内任务膨胀为“无来源的临时想法”

## 6. 节点权限与 task list 规则

### 6.1 创建权限

先区分三类创建面：

- `Spec DAG`：只承载任务契约和 gate
- 全局统一 task list：承载运行时工作项
- `Blackboard`：承载执行证据、proposal、blocker 和决策记录

在 `Spec DAG` 层：

- `SuperLeader` 可以创建：
  - `ObjectiveNode`
  - `LeaderTaskNode`
  - `ObjectiveGateNode`
- `Leader` 可以创建：
  - `TeammateTaskNode`
  - `TeamGateNode`
- `Teammate` 默认不直接创建新的 `Spec DAG` 契约节点

在 task list 层，对应规则是：

- `SuperLeader` 可以创建 `leader_lane scope` 任务
- `Leader` 可以创建 `team scope` 任务
- `Teammate` 可以在当前 `team scope` 内直接创建新任务
- `Teammate` 不能直接创建 `leader_lane scope` 或 `objective scope` 任务

在 Blackboard 层，对应规则是：

- `SuperLeader` 和对应 `Leader` 可在各自 `LeaderLaneBlackboard` 追加 lane 级 entry
- `Leader` 和本 team 的 `Teammate` 可在当前 `TeamBlackboard` 追加 team 级 entry
- `Teammate` 发现额外工作时，先创建 `team scope` task list 条目；如果需要提升为正式 team contract，再通过 `proposal` / `blocker` 驱动 `Leader` 创建或修订对应的 `TeammateTaskNode`

### 6.2 修改权限

硬规则建议写成：

1. 每一级只能修改自己创建的契约节点
2. 对于其他层级创建的契约节点，只允许读取
3. 执行者不直接写“完成”，只通过 Blackboard 追加共享证据
4. 完成状态由上级 reducer / evaluator 根据 Blackboard 中的证据推导

在 task list 层，额外规则是：

1. task list 允许按 Claude 风格更新 `owner`、`status`、`blockedBy`
2. 但这些状态最终仍只是运行时操作面，不替代 `Spec DAG` 的完成语义
3. 下级不能通过 task list 越权创建上层 scope 的任务

### 6.3 迭代与版本化

不建议原地重写旧节点。

更稳的做法是：

- 保留旧节点
- 创建新 revision
- 用 `supersedes` 连接旧节点和新节点
- reducer 只认最新 active revision

这样后续更适合：

- 审计
- 回放
- 失败恢复
- 复盘“为什么当时会重规划”

## 7. `SuperLeader`、`Leader`、`Teammate` 可复用 Claude 的部分

最值得尽量复用 Claude 的是：

- `Leader -> Teammate` 的局部 team 协作模式
- team 内 task list 的简单语义和工作方式
- teammate 在执行中补充 team 内任务
- teammate idle / wake-up 机制
- permission / shutdown / plan approval 等协议消息
- leader 自动收到 teammate 消息并继续下一轮协调

判断：

- Claude-compatible core 是这套框架最稳的底座
- 新增复杂性应尽量压在 `SuperLeader`、scope 和 Blackboard 层，而不是重写局部 team runtime

## 8. `Spec DAG` 的边类型建议

按当前已经确认的分层，`Spec DAG` 只描述契约关系，不再吸收执行证据关系。

最少建议保留这些边语义：

- `decomposes_to`
- `depends_on`
- `gated_by`
- `supersedes`

如果后续需要把“哪个任务最终满足了哪个 gate”的判定也显式化，可以再补一个可选的 `satisfies_gate`。

它们分别回答：

- 上级任务如何拆解为下级契约
- 哪些契约节点存在前置依赖
- 哪些 gate 控制当前节点是否进入下一阶段
- 新 revision 替代了哪一个旧 revision
- 可选的 `satisfies_gate` 则回答哪个执行结果最终满足了哪个 gate 的判定

像 `artifact_ref`、`verification_result`、`proposal`、`blocker` 这类执行证据与协作态，不再作为 `Spec DAG` 边语义，而统一进入 Blackboard entry log。

## 9. `Planner`、`LaunchBackend`、`WorkerSupervisor` 的关系

### 9.1 `Planner`

`Planner` 不应该混进 backend 或 supervisor。

它负责：

- 读取用户模板
- 编译 `Spec DAG`
- 决定 leader 数量和职责
- 决定是否重规划

未来可以继续拆成：

- `GlobalPlanner`：归 `SuperLeader`
- `LocalPlanner`：归 `Leader`

### 9.2 `LaunchBackend`

`LaunchBackend` 只负责“把谁拉起来”。

它应该统一拉两类 actor：

- `Leader`
- `Teammate`

但它不决定：

- 总目标
- team 数量
- 任务图
- 是否重规划

当前 v1 已经落下的具体形状是：

- `BackendRegistry`
- `InProcessLaunchBackend`
- `SubprocessLaunchBackend`
- `TmuxLaunchBackend`
- `CodexCliLaunchBackend`

统一执行模型先收敛为 one-turn `WorkerAssignment -> WorkerResult`，也就是先把“transport 如何承载 worker”固定下来，而不是一开始就实现完整多轮自治 loop。

### 9.3 `WorkerSupervisor`

`WorkerSupervisor` 只负责“让已经分配到的工作稳定跑完”。

当前 v1 已落下 `DefaultWorkerSupervisor`，职责先收敛为：

- 持久化 `WorkerRecord`
- 驱动单次 `launch -> completion/failure`
- 统一结果归一化
- 为 in-process / subprocess / tmux / codex_cli 四种 backend 提供共同 supervisor 边界

最终形态至少应覆盖：

- task claim
- turn loop
- heartbeat
- idle
- timeout
- cancel
- retry
- handoff 上报
- failure escalation

进一步建议拆成：

- `LeaderSupervisor`
- `TeammateSupervisor`

理由是：

- `SuperLeader` 不应直接盯所有 teammate
- 每个 `Leader` 只盯自己 team 内 worker

### 9.4 `BlackboardReducer`

在当前方案里，`Blackboard` 不能是自由可编辑文档，而应采用：

- append-only entry log
- reducer snapshot

判断：

- entry log 负责保留审计轨迹
- reducer snapshot 负责给模型和 runtime 提供压缩后的共享态
- 这比 mutable shared document 更稳，也更符合“`Spec DAG` 管契约，`Blackboard` 管证据”的当前分层

因此至少应有两类 reducer：

- `LeaderLaneBlackboardReducer`
- `TeamBlackboardReducer`

## 10. Claude Code 模式在这里的复用点

判断：

- Claude Code 已成熟的是 `Leader -> Teammate` 这一层
- 还不具备 `SuperLeader -> Leader` 这一层

关键证据：

- 一个 leader 只能有一个 team：见 [TeamCreateTool.ts#L132](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/tools/TeamCreateTool/TeamCreateTool.ts#L132)
- teammate 不能再生 teammate，team roster 是 flat 的：见 [AgentTool.tsx#L266](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/tools/AgentTool/AgentTool.tsx#L266)
- leader 的行为更像主会话 coordinator，而不是后台全局 planner：见 [useInboxPoller.ts#L118](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/hooks/useInboxPoller.ts#L118) 和 [attachments.ts#L3532](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/attachments.ts#L3532)

因此在 `agent_orchestra` 里应采取：

- 复用 Claude Code 的局部 team 协作模式
- 在其上增加 `SuperLeader` 层、`Spec DAG`、全局统一 task list、层级 Blackboard 和 reducer / evaluator 机制

## 11. 对当前代码的影响

当前 Python 骨架已经有下面这些落点：

- `Group` / `Team` / `TaskCard` / `AuthorityState` 契约
- `ObjectiveSpec` / `SpecNode` / `SpecEdge`
- scoped task list 与 scope visibility 规则
- `LeaderLaneBlackboard` / `TeamBlackboard`
- `BlackboardEntry` / `BlackboardSnapshot` / `BlackboardReducer`
- `LeaderTaskCard`
- `WorkerAssignment` / `WorkerResult` / `WorkerRecord`
- `GroupRuntime` / `TeamRuntime` / `Reducer`
- `TemplatePlanner`
- `BackendRegistry`
- `InProcessLaunchBackend` / `SubprocessLaunchBackend` / `TmuxLaunchBackend`
- `DefaultWorkerSupervisor`
- `worker_process` 共享 harness
- `GroupRuntime.run_worker_assignment()`
- `materialize_planning_result()`：`LeaderTaskCard -> LeaderRound -> leader runtime task / bootstrap blackboards`
- `compile_leader_assignments()`：要求 leader 输出 machine-readable `sequential_slices / parallel_slices` JSON
- `LeaderTurnOutput -> team-scope TaskCard -> teammate WorkerAssignment` ingestion bridge
- `RedisEventBus`
- `AgentRunner`

但还没有：

- `TeammateTaskNode`
- `SuperLeader` 真实运行时
- 自动持续运行的 leader execution layer：当前 bridge 仍需要外部驱动 leader turn，再 ingest 其 JSON 输出
- 多轮 `LeaderSupervisor` / `TeammateSupervisor` 状态机
- `PermissionBroker`
- `IdentityScope + Reconnector`
- typed `ProtocolBus` / mailbox bridge
- remote / cloud worker backend
- `Evaluator`

判断：

- 当前代码已经不只是协议底座，而是“协调内核 + 第一版本地执行层”
- 下一阶段应优先补 `SuperLeader`、leader execution layer、多轮 supervisor、protocol bus、permission 与 reconnect，而不是重复建设已经存在的 backend 抽象

## 12. 下一步设计优先级

如果继续推进，这套框架最值得优先固化的对象是：

1. `SuperLeader` runtime 与 group-level DAG execution
2. 自动持续运行的 leader loop：从 leader assignment 执行、到 JSON 输出、到 teammate assignment ingest 的闭环
3. `LeaderTeamContract`
4. `TeammateTaskNode`
5. 多轮 `LeaderSupervisor` / `TeammateSupervisor` 状态机
6. typed `ProtocolBus` / mailbox bridge
7. `PermissionBroker`
8. `IdentityScope + Reconnector`
9. `Evaluator`
10. 更完整的 `DeliveryStateMachine`
11. remote / cloud worker backend
12. 重规划 `proposal / decision` 的 blackboard entry schema

## 13. 相关文档

- `README.md`
- `architecture.md`
- `parallel-team-coordination.md`
- `claude-team-mapping.md`
- `../claude-code-main/agent-team.md`
- `../multi-agent-team-delivery/delivery-contracts.md`
