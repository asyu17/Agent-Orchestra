# agent_orchestra Agent 抽象、Skill 与 Prompt 系统

## 1. 一句话结论

`agent_orchestra` 的下一阶段不应继续分别堆 `LeaderRuntime`、`SuperLeaderRuntime`、`TeammateLoop`，而应收敛成“统一 `Agent` 作为自治执行体，`RolePolicy` 决定权限与作用域，`SkillSet` 决定能力模块，`PromptProfile` 决定认知与表达框架”的体系；并且推进顺序必须固定为三层：先把 `Teammate` 做成真正的常驻执行体，再让它通过 `mailbox + task list` 持续拿活，最后才把这个执行体做成 durable session + reconnect。更关键的是：`task` 在这个体系里只应承担 activation/contract 角色，而不应继续作为 team 的主执行模式；真正的主求解面应是长期在线的协作 agent、持续流动的 mailbox/task list/blackboard，以及 leader 的持续收敛。

## 2. 范围与资料来源

- 当前知识与讨论来源：
  - [agent-orchestra-framework.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/agent-orchestra-framework.md)
  - [resident-hierarchical-runtime.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/resident-hierarchical-runtime.md)
  - [team-parallel-execution-gap.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/team-parallel-execution-gap.md)
  - [parallel-team-coordination.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/parallel-team-coordination.md)
  - [implementation-status.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/implementation-status.md)
- 当前代码入口：
  - [team.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/contracts/team.py)
  - [execution.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/contracts/execution.py)
  - [resident_kernel.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/resident_kernel.py)
  - [leader_loop.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py)
  - [superleader.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/superleader.py)
  - [worker_supervisor.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/worker_supervisor.py)
  - [protocol_bridge.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/protocol_bridge.py)
  - [task.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/contracts/task.py)
- Claude team agent 参考：
  - [agent-team.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/claude-code-main/agent-team.md)
  - [inProcessRunner.ts#L624](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/swarm/inProcessRunner.ts#L624)
  - [inProcessRunner.ts#L853](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/swarm/inProcessRunner.ts#L853)
  - [useInboxPoller.ts#L843](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/hooks/useInboxPoller.ts#L843)

## 3. 为什么要引入统一 Agent 抽象

当前代码已经完成了一个关键前提：

- `LeaderLoopSupervisor` 与 `SuperLeaderRuntime` 已经共享 `ResidentCoordinatorKernel` 作为常驻协调壳：[resident_kernel.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/resident_kernel.py), [leader_loop.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py), [superleader.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/superleader.py)

判断：

- 既然 `Leader` 和 `SuperLeader` 已经共享“长期 loop + cycle state + finalize”的核心语义，那么下一步继续往下推进时，再分别造一套 `TeammateRuntime` 会让体系重新分叉
- 更自洽的做法是抽象出统一 `Agent`：它就是一个长期存在、会轮询、会 claim、会产出动作、会进入 idle / quiescent / shutdown 的自治执行体
- `SuperLeader / Leader / Teammate` 的差异不应主要通过不同 runtime 类型表达，而应通过同一个 agent 核心上的不同角色配置表达

## 4. 统一 Agent 模型

### 4.1 `Agent` 是自治执行体，不是角色枚举

建议把 `Agent` 定义成统一自治执行体，它至少拥有下面这些运行语义：

- 唯一 `agent_id`
- 长期 `session`
- mailbox cursor / subscription cursor
- 当前 claim 的 task / lease
- 一个持续循环：
  - 读取 control mailbox
  - 读取 business mailbox
  - 读取 task surface
  - 读取 blackboard digest
  - 决定下一步 action

典型 action 应统一为有限集合：

- `run_prompt_turn`
- `claim_task`
- `update_task`
- `append_blackboard_entry`
- `send_message`
- `spawn_subordinate`
- `idle`
- `shutdown`

判断：

- 这样定义后，`Teammate` 就不再只是“一次 assignment 的容器”，而是真正长期存在的 worker agent
- `Leader` 与 `SuperLeader` 也不再是特殊 while-loop，而只是不同 role policy 下的 agent 实例

### 4.2 `AgentSession` 是长期实体状态

`AgentSession` 应作为统一的长期状态面，承载：

- `session_id`
- `agent_id`
- `role_kind`
- `phase`
- `mailbox_cursor`
- `subscription_cursors`
- `claimed_task_ids`
- `lease`
- `last_reason`
- `metadata`

当前代码里的 `ResidentCoordinatorSession` 已经是这一层的上半部分：[execution.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/contracts/execution.py)

判断：

- 后续不应为 `LeaderSession`、`SuperLeaderSession`、`TeammateSession` 各自发明完全不同的数据形状
- 更稳的做法是统一 session 主语义，再让不同角色只在 metadata 和 policy 上有差异

### 4.3 `AgentKernel` 是统一 loop 驱动器

统一 agent 核心应继续复用当前 `ResidentCoordinatorKernel` 的方向，但角色从“只服务 coordinator”扩展到“一切自治执行体”。

它负责：

1. 驱动 cycle
2. 维护 phase/state transition
3. 统一累加 progress counters
4. 在 stop 时调用 finalize
5. 导出统一 runtime session 结果

判断：

- 当前 `ResidentCoordinatorKernel` 不是终点，而是未来统一 `AgentKernel` 的第一段实现
- 后续应继续把 `Teammate` 也接进这一条主语义，而不是另起一套 loop

### 4.4 `Task` 是 activation surface，不是 runtime 本体

如果把未来的 `Agent Orchestra` 做成你想要的 Claude 式长期在线协作系统，那么统一 `Agent` 抽象里必须显式写死一条原则：

- `task` 负责激活 agent、声明目标、约束 scope、挂接验收
- `agent loop` 才负责持续推进问题解决
- `mailbox + task list + blackboard` 是长期协作 surface，不是 leader turn 的附属品

这条原则和当前代码现状是相容的，但还没有完全落地：

- `ResidentCoordinatorKernel` 已经能驱动持续 cycle、phase、mailbox poll、claim 计数：[resident_kernel.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/resident_kernel.py)
- `ResidentTeammateRuntime` 已经能常驻 drain 当前 assignment 队列并自动 refill claim：[teammate_runtime.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/teammate_runtime.py)
- 但 team 的工作推进仍主要由 leader turn 驱动，而不是由自治 agent loop 直接以 mailbox/task surface 为事实输入进行推进：[leader_loop.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py)

判断：

- 因此统一 `Agent` 抽象不只是把几类角色并到一套类层次里
- 它更是在系统主语义上完成一次切换：从“任务驱动 agent”切到“任务激活 agent，agent 持续协作解决问题”

## 5. 角色差异不靠类层级，而靠策略层

### 5.1 `RolePolicy`

`RolePolicy` 决定角色硬边界：

- 它能改什么
- 它能看什么
- 它能 claim 什么
- 它能不能拉下级
- 它能把结果写回哪里

建议拆成三块：

- `ScopePolicy`
- `AuthorityPolicy`
- `ClaimPolicy`

### 5.2 `ScopePolicy`

`ScopePolicy` 决定可见性：

- 可见 task scope
- 可见 mailbox
- 可见 blackboard
- 可消费哪些 digest / 全文引用

例如：

- `SuperLeader`：可看 objective 与下层 lane/team 摘要
- `Leader`：可看本 lane、本 team
- `Teammate`：只看本 team scope 和 directed/control message

### 5.3 `AuthorityPolicy`

`AuthorityPolicy` 决定可修改能力：

- 能否创建 task
- 能否 claim / complete / block task
- 能否 spawn subordinate
- 能否改 budget
- 能否改 spec node
- 能否给谁发 directive

这层必须承载我们已确认的硬约束：

- `SuperLeader` 不能直接给 `Teammate` 发任务
- `Leader` 不能改写总目标
- 每一级只能修改自己创建的节点
- 上层可读下层全部任务，下层只能看本 scope

### 5.4 `ClaimPolicy`

`ClaimPolicy` 决定 agent 如何拿活：

- 只接 directed task，还是允许 autonomous claim
- claim 的候选条件是什么
- 是否允许派生创建新 task
- 是否必须携带 `derived_from` / `reason`
- lease 超时后的 reclaim 规则是什么

判断：

- `Teammate autonomous claim` 的能力不应只写在 prompt 里，而应首先出现在 `ClaimPolicy`
- 后续 reconnect / reclaim 也必须以这层 policy 为事实基础

### 5.5 同一运行时底座，不是同一权限

这一点需要明确区分：

- `SuperLeader`
- `Leader`
- `Teammate`

应该共享同一套长期在线 `Agent` 运行时底座。

但这不等于三者“同权”。

更准确的说法应是：

- 同一底座
- 同一长期 session / mailbox / subscription / blackboard / claim / idle 主语义
- 不同的 `RolePolicy / AuthorityPolicy / ClaimPolicy / PromptProfile`

判断：

- 如果把它们做成完全不同 runtime，最终会重新退回“leader loop 一套、teammate worker 一套、superleader scheduler 一套”的分叉系统
- 如果把它们做成真正同权，又会失去收敛中心和层级治理边界
- 因此最稳的模型是：运行时同构，权限与职责异构

在这个模型里：

- team 内部的 `Leader + Teammate` 应该像 Claude team 一样，是同一个长期在线协作共同体
- `SuperLeader` 则是在上层再包一层同样的 agent substrate，用来组织多个 team 的更大规模团队
- 也就是说，差异主要不在 runtime 类型，而在可见 scope、可改写对象、可发 directive 的下级范围、以及最终收敛职责

## 6. Skill 与 Prompt 的位置

### 6.1 `SkillSet` 负责“会不会做”

`Skill` 不应只是 prompt 片段，而应是正式能力模块。

建议每个 skill 至少包含：

- `skill_id`
- `description`
- `trigger_kinds`
- `required_inputs`
- `output_schema`
- `tool_capabilities`
- `prompt_fragment`
- `constraints`

建议优先固化成正式 skill 的包括：

- `planning`
- `task_decomposition`
- `code_edit`

## 7. Team 与跨 Team 消息共同体

### 7.1 Team 是任务共同体，不是一次性 worker 池

在你要的目标里，team 不应被理解成“某一轮 leader 暂时调用的一组 worker”。

更合适的定义是：

- team 是围绕某一组相似任务长期存在的共同体
- `Leader` 和 `Teammate` 都属于这个共同体
- 它内部默认共享更高密度的 task/work surface、roster、team blackboard 与订阅面

判断：

- 这正是 Claude team agent 最值得借鉴的地方
- 对 Agent Orchestra 来说，应该把 team 内长期在线协作视为基础能力，而不是 leader loop 的附属优化
- `task` 在这里更多只是 activation/contract，真正持续解决问题的是在线 team 本身

### 7.2 跨 Team 不应默认全互通，而应走受控订阅与消息圈

在多 team / 多 group 的系统里，不能简单把 Claude 的 team 内高密度消息模型直接放大成全局群聊。

更稳的做法是：

- team 内默认高密度 subscription
- cross-team 默认 summary-only / index-first
- 需要更深协作时，再通过显式 subscription 或 mail circle 拉起局部协作面

这里的 `mail circle` 更适合作为：

- 一层建立在 mailbox/subscription 之上的协作 overlay
- 而不是新的 task 真相源

建议它至少具备：

- `circle_id`
- `owner_agent_id`
- `scope`
- `members`
- `visibility_policy`
- `summary_policy`
- `full_text_policy`
- `ttl`
- `derived_from`
- `reason`

判断：

- team 内可以默认存在“全员可见”的协作圈
- cross-team 的 circle 必须是显式创建、可审计、可回收的
- 它只能扩展 communication surface，不能绕开 `Spec DAG / task list / authority` 去偷偷改写系统真相

### 7.3 推荐的最终形态

最终更接近你目标的形态应是：

1. 所有角色都统一成长期在线 `Agent`
2. `Leader` 与 `Teammate` 在 team 内像 Claude team 一样长期协作
3. `SuperLeader` 在 team 之上组织多个 team
4. cross-team 默认走摘要、索引和受控订阅
5. 需要更强协作时，通过显式 mail circle 建立局部消息共同体

如果一句话概括：

- team 内部要像 Claude
- team 之间要比 Claude 更可治理
- `verification`
- `review`
- `handoff_summary`

判断：

- 如果 skill 只有自然语言说明，而没有输入/输出/工具边界，后续 agent 之间很难稳定消费彼此结果
- skill 应成为 `AgentProfile` 的组成部分，而不是运行时临时附加的自由文本

### 6.2 `PromptProfile` 负责“怎么思考和表达”

`PromptProfile` 不应承担硬约束，而应承担认知框架和表达风格。

建议分层渲染：

1. global prompt
2. framework prompt
3. role prompt
4. team/lane prompt
5. turn/task prompt

判断：

- prompt 告诉 agent “应该怎么做”
- runtime policy 决定 agent “只能怎么做”
- 两层必须同时存在，不能互相替代

### 6.3 `RolePolicy`、`SkillSet`、`PromptProfile` 的分工

统一分工应固定为：

- `RolePolicy` 决定“能不能做”
- `SkillSet` 决定“会不会做”
- `PromptProfile` 决定“怎么想、怎么说”

这是后续避免系统退化成“大 prompt + 隐性越权”的关键边界。

## 7. 三层推进顺序

这是当前最重要的顺序约束，不能乱。

### 7.1 第一层：先把 `Teammate` 做成真正的常驻执行体

目标：

- `Teammate` 被 leader spawn 后不因一次 assignment 结束而退出
- 它拥有长期 session
- 空闲时持续轮询 mailbox / control signal

这一层解决的是“它是不是一个活着的 agent”。

当前复用入口：

- [resident_kernel.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/resident_kernel.py)
- [worker_supervisor.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/worker_supervisor.py)
- [leader_loop.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py)

### 7.2 第二层：再让它通过 `mailbox + task list` 持续拿活

目标：

- idle teammate 持续读 directed/control mailbox
- 在 team scope 内自主 claim `pending + unowned + unblocked` task
- 结果回写到 mailbox、task list、blackboard

这一层解决的是“它是不是一个会持续工作的 agent”。

判断：

- 这一步现在已经完成，`team-parallel-execution` 也已因此退出开放 gap
- 当前 leader/superleader shared shell 与 resident teammate runtime 都已完成，后续重点转向更深的 durable/reconnect hardening
- 但这里还要补一条更高优先级的语义升级：即便具备了持续拿活能力，也不能再把 `task` 本身当成 team 的主执行面；后续要继续把 `mailbox + task list + blackboard` 推成 primary execution surface，把 leader turn 压缩成“需要高价值认知时才触发”的动作

### 7.3 第三层：最后才把它做成 durable session + reconnect

目标：

- session / lease / cursor 可持久化
- 旧 claim 可恢复或回收
- runtime 可把长期实体重新接回主流程

这一层解决的是“它崩了或切走后能不能续上”。

判断：

- reconnect 不应先于 resident worker 主语义落地
- 否则只会得到“能重连一个其实不会持续工作的空壳”

### 7.4 第四层的长期目标：让 `Leader` 变成持续收敛中心

虽然本文件固定的落地顺序仍是三层，但当三层做完后，下一目标应明确为：

- `Leader` 不再主要充当派工器
- `Leader` 主要充当 team 的持续收敛中心
- 它在 mailbox、task list、blackboard 上持续感知团队状态，只在必要时发起高价值 prompt turn

这一步和 `SuperLeader` 的长期目标也是同构的：

- `SuperLeader` 也应是上层收敛中心，而不是单纯“批量启动 leader loop”的外层调度器

判断：

- 这正是统一 `Agent + RolePolicy + SkillSet + PromptProfile` 模型的真正价值
- 它不是为了统一类名，而是为了让 `SuperLeader / Leader / Teammate` 最终都成为同一种长期在线协作实体，只在权限、作用域、技能包上不同

## 8. 角色实例化建议

建议未来统一为：

- `SuperLeader = Agent + SuperLeaderRolePolicy + SuperLeaderSkillSet + SuperLeaderPromptProfile`
- `Leader = Agent + LeaderRolePolicy + LeaderSkillSet + LeaderPromptProfile`
- `Teammate = Agent + TeammateRolePolicy + TeammateSkillSet + TeammatePromptProfile`

以后还可扩展：

- `Evaluator`
- `Reviewer`
- `AuthorityRoot`

当前 [team.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/contracts/team.py) 里的 `AgentProfile` 还只是最小形状；后续应判断为：

- 当前：最小标识 contract
- 未来：统一 agent profile 装配入口

## 9. 与现有知识的关系

这份文档与其他知识文档的关系应固定为：

- `resident-hierarchical-runtime.md`
  聚焦 leader/superleader shared kernel 与 resident coordination shell
- `team-parallel-execution-gap.md`
  聚焦 team 内外并行差异与剩余执行 gap
- 本文
  聚焦下一阶段统一 agent 抽象、role/skill/prompt 分层，以及三层推进顺序

判断：

- 后续如果继续讨论“Teammate 如何变成自治常驻执行体”，应优先回到本文，而不是重新从 `LeaderLoopSupervisor` 和 `SuperLeaderRuntime` 分别出发

## 10. 相关文档

- [README.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/README.md)
- [resident-hierarchical-runtime.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/resident-hierarchical-runtime.md)
- [team-parallel-execution-gap.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/team-parallel-execution-gap.md)
- [agent-orchestra-framework.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/agent-orchestra-framework.md)
- [parallel-team-coordination.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/parallel-team-coordination.md)
