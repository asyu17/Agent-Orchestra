# claude-code-main 中的 Agent Team 实现

## 1. 一句话结论

Claude Code 的 agent team 不是单纯“起多个 agent”。

它实际上由 5 个层次组成:

1. 持久化团队元数据: `TeamFile`
2. 运行态前端上下文: `AppState.teamContext`
3. teammate 启动与后端选择: `AgentTool -> spawnMultiAgent -> swarm backends`
4. 控制面通信: mailbox + permissionSync + plan/shutdown 协议
5. 前台可视化与恢复: `REPL`、`useInboxPoller()`、`useSwarmInitialization()`

这套实现的关键特征是:

- 团队 roster 是扁平的, 不允许 teammate 再派生 teammate
- in-process teammate 和 tmux/iTerm2 teammate 共用同一套 team file / mailbox / UI
- leader 是一个特殊角色: 它拥有 `teamContext`, 但不会被当成普通 teammate 进环境变量身份链

## 2. 范围与资料来源

- 分析对象：`resource/others/claude-code-main/src` 中与 `team`、`swarm`、`teammate` 有关的实现
- 关注范围：team file、spawn 路径、backend 选择、mailbox、permission sync、REPL 接点
- 关联文档：`README.md`、`runtime-and-ui.md`、`extensibility-and-integrations.md`

## 3. 核心代码索引

- Team 创建: [TeamCreateTool.ts#L74](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/tools/TeamCreateTool/TeamCreateTool.ts#L74)
- Team 删除: [TeamDeleteTool.ts#L32](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/tools/TeamDeleteTool/TeamDeleteTool.ts#L32)
- Team 磁盘模型: [teamHelpers.ts#L64](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/swarm/teamHelpers.ts#L64)
- AgentTool 接入 team spawn: [AgentTool.tsx#L284](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/tools/AgentTool/AgentTool.tsx#L284)
- 统一 teammate spawn 入口: [spawnMultiAgent.ts#L1088](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/tools/shared/spawnMultiAgent.ts#L1088)
- 后端选择: [registry.ts#L136](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/swarm/backends/registry.ts#L136)
- in-process spawn: [spawnInProcess.ts#L104](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/swarm/spawnInProcess.ts#L104)
- in-process runner: [inProcessRunner.ts#L883](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/swarm/inProcessRunner.ts#L883)
- teammate 身份工具: [teammate.ts#L41](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/teammate.ts#L41)
- mailbox 协议: [teammateMailbox.ts#L84](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/teammateMailbox.ts#L84)
- 权限同步: [permissionSync.ts#L112](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/swarm/permissionSync.ts#L112)
- leader UI 桥: [leaderPermissionBridge.ts#L28](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/swarm/leaderPermissionBridge.ts#L28)
- inbox 轮询: [useInboxPoller.ts#L126](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/hooks/useInboxPoller.ts#L126)
- REPL team 接点: [REPL.tsx#L633](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/screens/REPL.tsx#L633)

## 4. 三个事实源

理解 team 实现时, 最容易混淆的是“团队信息到底存在哪”。

### 4.1 磁盘事实源: `TeamFile`

`TeamFile` 定义在 [teamHelpers.ts#L64](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/swarm/teamHelpers.ts#L64)。

它保存的是团队的持久化元数据, 典型字段包括:

- `name`
- `leadAgentId`
- `leadSessionId`
- `hiddenPaneIds`
- `teamAllowedPaths`
- `members[]`

成员项里又保存:

- `agentId`
- `name`
- `agentType`
- `model`
- `prompt`
- `color`
- `planModeRequired`
- `tmuxPaneId`
- `cwd`
- `worktreePath`
- `backendType`
- `isActive`
- `mode`

Team 文件位置通过 [teamHelpers.ts#L115](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/swarm/teamHelpers.ts#L115) 和 [teamHelpers.ts#L122](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/swarm/teamHelpers.ts#L122) 计算, 最终写到:

- `~/.claude/teams/<sanitized-team-name>/config.json`

### 4.2 前端事实源: `AppState.teamContext`

REPL 里的运行态 team 上下文定义在 [AppStateStore.ts#L323](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/state/AppStateStore.ts#L323)。

它保存的是“当前会话在 UI 中如何理解这个团队”:

- `teamName`
- `teamFilePath`
- `leadAgentId`
- `selfAgentId`
- `selfAgentName`
- `isLeader`
- `selfAgentColor`
- `teammates`

旁边还有两组非常 team-specific 的 UI 状态:

- 收件箱: [AppStateStore.ts#L351](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/state/AppStateStore.ts#L351)
- worker sandbox 权限队列: [AppStateStore.ts#L363](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/state/AppStateStore.ts#L363)

### 4.3 teammate task 事实源: `InProcessTeammateTaskState`

单个 in-process teammate 的运行态保存在 [types.ts#L20](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/tasks/InProcessTeammateTask/types.ts#L20)。

它和 `TeamFile` / `teamContext` 不同, 关注的是单 agent 的执行生命周期:

- `identity`
- `prompt`
- `model`
- `abortController`
- `currentWorkAbortController`
- `awaitingPlanApproval`
- `permissionMode`
- `messages`
- `pendingUserMessages`
- `isIdle`
- `shutdownRequested`

结论:

- `TeamFile` 解决持久化与跨进程共享
- `teamContext` 解决 leader/worker 当前 UI 视角
- `InProcessTeammateTaskState` 解决单 teammate 的执行控制

## 5. 团队创建与销毁

### 5.1 创建 team: `TeamCreateTool`

`TeamCreateTool.call()` 在 [TeamCreateTool.ts#L117](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/tools/TeamCreateTool/TeamCreateTool.ts#L117)。

它做的事是按固定顺序完成的:

1. 检查 leader 当前是否已经有 team: [TeamCreateTool.ts#L134](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/tools/TeamCreateTool/TeamCreateTool.ts#L134)
2. 若 team 名冲突则生成唯一名: [TeamCreateTool.ts#L64](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/tools/TeamCreateTool/TeamCreateTool.ts#L64)
3. 生成 team lead 的 deterministic `leadAgentId`
4. 组装并写入 `TeamFile`: [TeamCreateTool.ts#L155](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/tools/TeamCreateTool/TeamCreateTool.ts#L155)
5. 注册 session shutdown cleanup: [TeamCreateTool.ts#L180](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/tools/TeamCreateTool/TeamCreateTool.ts#L180)
6. 重置并创建任务目录: [TeamCreateTool.ts#L184](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/tools/TeamCreateTool/TeamCreateTool.ts#L184)
7. 设置 leader 的 task list 绑定: [TeamCreateTool.ts#L191](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/tools/TeamCreateTool/TeamCreateTool.ts#L191)
8. 初始化 `AppState.teamContext`: [TeamCreateTool.ts#L196](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/tools/TeamCreateTool/TeamCreateTool.ts#L196)

一个特别关键的实现细节在 [TeamCreateTool.ts#L224](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/tools/TeamCreateTool/TeamCreateTool.ts#L224):

- team lead 不会被设置为普通 teammate 的环境身份
- team 名主要存储在 `AppState.teamContext`, 而不是 process env

这解释了为什么 leader 在判断逻辑上始终是特殊角色。

### 5.2 删除 team: `TeamDeleteTool`

`TeamDeleteTool.call()` 在 [TeamDeleteTool.ts#L71](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/tools/TeamDeleteTool/TeamDeleteTool.ts#L71)。

删除逻辑不是“直接删目录”, 而是有显式保护:

- 先读取 `TeamFile`
- 过滤掉 lead, 只看非 lead 成员
- 若还有 `isActive !== false` 的成员, 拒绝 cleanup: [TeamDeleteTool.ts#L87](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/tools/TeamDeleteTool/TeamDeleteTool.ts#L87)

通过检查后才会:

1. 调 `cleanupTeamDirectories()`: [TeamDeleteTool.ts#L101](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/tools/TeamDeleteTool/TeamDeleteTool.ts#L101)
2. 取消 session cleanup 注册: [TeamDeleteTool.ts#L103](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/tools/TeamDeleteTool/TeamDeleteTool.ts#L103)
3. 清空颜色分配与 leader team 绑定
4. 清掉 `AppState.teamContext` 和 inbox: [TeamDeleteTool.ts#L120](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/tools/TeamDeleteTool/TeamDeleteTool.ts#L120)

### 5.3 非正常退出 cleanup

如果 leader 没有显式 `TeamDelete`, session 结束时还会走:

- 注册: [teamHelpers.ts#L560](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/swarm/teamHelpers.ts#L560)
- 执行: [teamHelpers.ts#L576](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/swarm/teamHelpers.ts#L576)

这里先杀 pane-backed teammate, 再删 team/tasks/worktree 目录: [teamHelpers.ts#L598](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/swarm/teamHelpers.ts#L598), [teamHelpers.ts#L641](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/swarm/teamHelpers.ts#L641)。

## 6. 谁负责真正 spawn teammate

### 6.1 入口不在 `TeamCreateTool`, 而在 `AgentTool`

真正的“起 teammate”入口在 `AgentTool`。

当 `teamName && name` 成立时, `AgentTool` 不再走普通 subagent 路线, 而是改走 `spawnTeammate()`: [AgentTool.tsx#L284](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/tools/AgentTool/AgentTool.tsx#L284), [AgentTool.tsx#L290](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/tools/AgentTool/AgentTool.tsx#L290)。

同时有两个硬限制:

- teammate 不能再 spawn teammate: [AgentTool.tsx#L266](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/tools/AgentTool/AgentTool.tsx#L266)
- in-process teammate 不能再起 background agents: [AgentTool.tsx#L277](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/tools/AgentTool/AgentTool.tsx#L277)

这说明 roster 被设计成 flat team, 不支持 team 中再分层 team。

### 6.2 统一 spawn 入口: `spawnMultiAgent.ts`

统一入口是 [spawnMultiAgent.ts#L1088](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/tools/shared/spawnMultiAgent.ts#L1088)。

它内部按三种实现路径分流:

- split-pane / native panes: [spawnMultiAgent.ts#L305](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/tools/shared/spawnMultiAgent.ts#L305)
- separate tmux window: [spawnMultiAgent.ts#L545](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/tools/shared/spawnMultiAgent.ts#L545)
- in-process teammate: [spawnMultiAgent.ts#L840](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/tools/shared/spawnMultiAgent.ts#L840)

### 6.2.1 pane-based spawn

pane-based 分支做的核心动作是:

1. 解析/生成唯一名字与 `agentId`
2. 调 backend 创建 pane
3. 拼接 teammate CLI 参数
4. 把命令注入 pane
5. 更新 `AppState.teamContext`
6. 注册一个 out-of-process teammate task: [spawnMultiAgent.ts#L760](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/tools/shared/spawnMultiAgent.ts#L760)
7. 把成员写入 `TeamFile`
8. 通过 mailbox 发送初始 prompt

对应代码点:

- split-pane: [spawnMultiAgent.ts#L340](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/tools/shared/spawnMultiAgent.ts#L340), [spawnMultiAgent.ts#L450](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/tools/shared/spawnMultiAgent.ts#L450), [spawnMultiAgent.ts#L509](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/tools/shared/spawnMultiAgent.ts#L509)
- separate-window: [spawnMultiAgent.ts#L664](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/tools/shared/spawnMultiAgent.ts#L664), [spawnMultiAgent.ts#L723](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/tools/shared/spawnMultiAgent.ts#L723)

### 6.2.2 in-process spawn

in-process 分支不发 CLI 到外部 pane, 而是:

1. 调 `spawnInProcessTeammate()`
2. 立刻 `startInProcessTeammate()`
3. 更新 `AppState.teamContext`
4. 将成员写入 `TeamFile`
5. 不再通过 mailbox 发送初始 prompt

对应代码点:

- in-process 分支: [spawnMultiAgent.ts#L840](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/tools/shared/spawnMultiAgent.ts#L840)
- 启动 runner: [spawnMultiAgent.ts#L907](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/tools/shared/spawnMultiAgent.ts#L907)
- leader 自动补位到 `teamContext`: [spawnMultiAgent.ts#L943](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/tools/shared/spawnMultiAgent.ts#L943)

## 7. teammate backend 选择

backend 选择在 [registry.ts#L136](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/swarm/backends/registry.ts#L136)。

它的优先级大致是:

1. 已在 tmux 中则用 tmux
2. 在 iTerm2 中且 `it2` CLI 可用则用 iTerm2 backend
3. iTerm2 不可用但 tmux 可用则退回 tmux
4. 否则抛错误

同时它还有一个重要的 fallback 机制:

- 一旦 pane backend 不可用且自动模式触发 fallback, 就标记 in-process fallback: [registry.ts#L326](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/swarm/backends/registry.ts#L326)
- 之后 `isInProcessEnabled()` 会返回 true: [registry.ts#L351](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/swarm/backends/registry.ts#L351)
- 对外的“当前 teammate 模式”通过 [registry.ts#L396](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/swarm/backends/registry.ts#L396) 暴露

统一执行器由 [registry.ts#L425](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/swarm/backends/registry.ts#L425) 提供。

## 8. in-process teammate 的运行逻辑

### 8.1 spawn 阶段

`spawnInProcessTeammate()` 在 [spawnInProcess.ts#L104](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/swarm/spawnInProcess.ts#L104)。

它负责:

- 生成 deterministic `agentId`
- 创建 `TeammateIdentity`: [spawnInProcess.ts#L128](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/swarm/spawnInProcess.ts#L128)
- 创建 AsyncLocalStorage context: [spawnInProcess.ts#L139](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/swarm/spawnInProcess.ts#L139)
- 组装 `InProcessTeammateTaskState`
- 将 plan teammate 的默认 permission mode 设为 `plan`: [spawnInProcess.ts#L173](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/swarm/spawnInProcess.ts#L173)
- 注册为 task: [spawnInProcess.ts#L191](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/swarm/spawnInProcess.ts#L191)

强杀逻辑在 [spawnInProcess.ts#L227](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/swarm/spawnInProcess.ts#L227), 它会:

- abort controller
- 从 `teamContext.teammates` 删除成员
- 从 team file 删除成员: [spawnInProcess.ts#L303](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/swarm/spawnInProcess.ts#L303)

### 8.2 run 阶段

真正的 agent loop 在 [inProcessRunner.ts#L883](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/swarm/inProcessRunner.ts#L883)。

它和普通 background agent 的差异主要有 4 个:

1. 会把 `TEAMMATE_SYSTEM_PROMPT_ADDENDUM` 加进系统提示词: [inProcessRunner.ts#L937](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/swarm/inProcessRunner.ts#L937)
2. 自定义 `CanUseTool` 逻辑, 优先走 leader UI 批准: [inProcessRunner.ts#L128](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/swarm/inProcessRunner.ts#L128)
3. 完成一轮后不会退出, 而是进入 idle 等待下一个 prompt: [inProcessRunner.ts#L689](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/swarm/inProcessRunner.ts#L689)
4. idle / interrupted / failed 时都会向 leader 发 idle notification: [inProcessRunner.ts#L569](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/swarm/inProcessRunner.ts#L569), [inProcessRunner.ts#L1334](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/swarm/inProcessRunner.ts#L1334), [inProcessRunner.ts#L1516](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/swarm/inProcessRunner.ts#L1516)

shutdown 请求不会自动接受, 而是被包装成下一轮 prompt 交给模型自己决定: [inProcessRunner.ts#L1364](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/swarm/inProcessRunner.ts#L1364)。

启动入口只是一个 fire-and-forget 包装: [inProcessRunner.ts#L1544](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/swarm/inProcessRunner.ts#L1544)。

### 8.3 team 内部并行是怎么发生的

Claude Code 的 team 内部并行, 不是“leader 开一个共享线程池, 再把任务塞进去”。

它更接近:

1. leader 多次调用 `AgentTool`
2. 每次调用都独立 spawn 一个 teammate
3. 每个 teammate 都有自己独立的执行状态和长生命周期 prompt loop
4. leader 通过 mailbox 和 task list 异步接收结果, 而不是同步等待单个 worker 返回

源码证据有 5 个关键点:

1. `AgentTool` 在 `team_name && name` 时直接切到 `spawnTeammate()` 路径, 说明并行单元是“一个个被独立拉起的 teammate”, 不是 team 内部的隐式子线程池: [AgentTool.tsx#L284](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/tools/AgentTool/AgentTool.tsx#L284)。
2. in-process 路径里, `handleSpawnInProcess()` 会先 `spawnInProcessTeammate()`, 然后马上 `startInProcessTeammate()`; 启动函数本身又是 fire-and-forget, 所以 leader 不会阻塞在当前 teammate 上: [spawnMultiAgent.ts#L899](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/tools/shared/spawnMultiAgent.ts#L899), [spawnMultiAgent.ts#L910](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/tools/shared/spawnMultiAgent.ts#L910), [inProcessRunner.ts#L1544](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/swarm/inProcessRunner.ts#L1544)。
3. 每个 in-process teammate 在 spawn 时都会拿到独立的 `AbortController`、`TeammateIdentity`、`AsyncLocalStorage context` 和 `InProcessTeammateTaskState`, 说明它们是彼此独立的执行体, 不是共享状态机上的“任务槽位”: [spawnInProcess.ts#L120](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/swarm/spawnInProcess.ts#L120), [spawnInProcess.ts#L127](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/swarm/spawnInProcess.ts#L127), [spawnInProcess.ts#L137](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/swarm/spawnInProcess.ts#L137), [spawnInProcess.ts#L157](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/swarm/spawnInProcess.ts#L157)。
4. teammate 跑完一轮不会退出, 而是进入 idle 状态并持续轮询 mailbox / shutdown / task list; 这使得多个 teammate 可以同时驻留, 并在后续继续并行接活: [inProcessRunner.ts#L689](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/swarm/inProcessRunner.ts#L689), [inProcessRunner.ts#L853](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/swarm/inProcessRunner.ts#L853), [inProcessRunner.ts#L1334](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/swarm/inProcessRunner.ts#L1334)。
5. leader 侧不是同步 `await teammate result`, 而是通过 `useInboxPoller()` 异步收消息: leader 空闲时立即提交为新 turn, leader 忙时先排队, 等空闲再统一投递。这让多个 teammate 可以并发工作并异步回流结果: [useInboxPoller.ts#L843](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/hooks/useInboxPoller.ts#L843), [useInboxPoller.ts#L875](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/hooks/useInboxPoller.ts#L875)。

还要注意一个经常被忽略的点:

- 并行协作不只靠 leader 显式发消息
- in-process teammate 的 idle loop 还会主动从 task list 里 claim `pending + unowned + unblocked` 的任务
- 这意味着 team 内部有一套“消息驱动 + task list 拉取驱动”的混合并行机制: [inProcessRunner.ts#L592](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/swarm/inProcessRunner.ts#L592), [inProcessRunner.ts#L621](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/swarm/inProcessRunner.ts#L621), [inProcessRunner.ts#L1015](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/swarm/inProcessRunner.ts#L1015)。

因此可以把 Claude team 的“内部并行”总结成一句更精确的话:

- leader 负责生成和调度多个独立 teammate
- teammate 以常驻 loop 方式并发运行
- task list 和 mailbox 共同承担工作分发与结果回流
- REPL/inbox poller 负责把异步回流结果重新注入 leader 的主对话

## 9. teammate 身份与恢复

身份解析统一在 [teammate.ts#L41](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/teammate.ts#L41) 开始的 helpers 中。

优先级是:

1. AsyncLocalStorage teammate context
2. dynamic team context
3. `AppState.teamContext` 兜底

几个关键判断:

- `getAgentId()`: [teammate.ts#L78](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/teammate.ts#L78)
- `getAgentName()`: [teammate.ts#L88](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/teammate.ts#L88)
- `getTeamName()`: [teammate.ts#L111](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/teammate.ts#L111)
- `isTeammate()`: [teammate.ts#L123](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/teammate.ts#L123)
- `isTeamLead()`: [teammate.ts#L171](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/teammate.ts#L171)

恢复与首次初始化有两条路径:

- 计算首屏前的 `teamContext`: [reconnection.ts#L23](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/swarm/reconnection.ts#L23)
- 从 resume transcript 恢复: [reconnection.ts#L75](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/swarm/reconnection.ts#L75)

REPL mount 后再通过 [useSwarmInitialization.ts#L30](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/hooks/useSwarmInitialization.ts#L30) 补 hooks 初始化。

## 10. 消息面与控制面

### 10.1 mailbox 是统一总线

所有 teammate, 不论 in-process 还是 pane-based, 都共享 mailbox 机制。

但这里的“共享”不是指“全 team 共用一个所有人都能直接读到的群聊频道”。

更准确地说:

- mailbox 是同一套协议
- inbox 是按 recipient 单独隔离
- 默认消息是点对点投递
- 只有显式 broadcast 时, 才会复制写入多个 teammate 的 inbox

源码证据:

- inbox 文件路径本身就是 `~/.claude/teams/{team_name}/inboxes/{agent_name}.json`, 说明 mailbox 的物理落点是“每个 agent 一个 inbox 文件”, 不是单个 team shared log: [teammateMailbox.ts#L4](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/teammateMailbox.ts#L4), [teammateMailbox.ts#L53](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/teammateMailbox.ts#L53)。
- 普通 `SendMessage` 调用的是 `writeToMailbox(recipientName, ...)`, 直接写入指定收件人的 inbox: [SendMessageTool.ts#L150](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/tools/SendMessageTool/SendMessageTool.ts#L150), [teammateMailbox.ts#L128](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/teammateMailbox.ts#L128)。
- `broadcast` 也不是写到某个共享 team channel, 而是遍历 `teamFile.members`, 对每个 recipient 分别 `writeToMailbox(...)`: [SendMessageTool.ts#L188](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/tools/SendMessageTool/SendMessageTool.ts#L188), [SendMessageTool.ts#L220](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/tools/SendMessageTool/SendMessageTool.ts#L220), [SendMessageTool.ts#L238](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/tools/SendMessageTool/SendMessageTool.ts#L238)。

因此, Claude team 的“全员可见”更准确地发生在这些层面:

- roster / team membership 是全员可知的
- task list 在同一 team 内是共享工作面
- 某些广播消息可以一次发给所有成员

但默认消息可见性不是“team 内全文自动互相看到”, 而是“recipient-scoped inbox + 可选 broadcast”。

核心读写函数:

- 读: [teammateMailbox.ts#L84](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/teammateMailbox.ts#L84)
- 写: [teammateMailbox.ts#L134](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/teammateMailbox.ts#L134)

mailbox 协议里除了普通消息, 还定义了控制消息:

- idle notification: [teammateMailbox.ts#L410](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/teammateMailbox.ts#L410)
- plan approval request/response: [teammateMailbox.ts#L684](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/teammateMailbox.ts#L684), [teammateMailbox.ts#L702](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/teammateMailbox.ts#L702)
- shutdown request: [teammateMailbox.ts#L720](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/teammateMailbox.ts#L720)
- helper for发送 shutdown: [teammateMailbox.ts#L831](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/teammateMailbox.ts#L831)

### 10.2 `SendMessageTool` 是 team 控制协议的模型入口

`SendMessageTool` 不只是 DM 工具, 也是 team 协议的工具化入口: [SendMessageTool.ts#L494](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/tools/SendMessageTool/SendMessageTool.ts#L494)。

它支持:

- 普通点对点消息: [SendMessageTool.ts#L148](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/tools/SendMessageTool/SendMessageTool.ts#L148)
- 广播: [SendMessageTool.ts#L192](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/tools/SendMessageTool/SendMessageTool.ts#L192)
- shutdown request / approve / reject: [SendMessageTool.ts#L266](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/tools/SendMessageTool/SendMessageTool.ts#L266), [SendMessageTool.ts#L300](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/tools/SendMessageTool/SendMessageTool.ts#L300), [SendMessageTool.ts#L388](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/tools/SendMessageTool/SendMessageTool.ts#L388)
- plan approval / rejection: [SendMessageTool.ts#L428](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/tools/SendMessageTool/SendMessageTool.ts#L428), [SendMessageTool.ts#L474](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/tools/SendMessageTool/SendMessageTool.ts#L474)

而且它还会先尝试把消息路由给本地 agent/subagent, 再回退到 ambient team mailbox 路由: [SendMessageTool.ts#L797](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/tools/SendMessageTool/SendMessageTool.ts#L797)。

### 10.3 inbox 轮询把控制消息接回 REPL

REPL 挂载 `useInboxPoller()` 的位置在 [REPL.tsx#L4034](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/screens/REPL.tsx#L4034)。

这个 hook 在 [useInboxPoller.ts#L126](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/hooks/useInboxPoller.ts#L126) 定义, 做了几件关键事:

- leader/worker 按身份选择轮询哪个 inbox: [useInboxPoller.ts#L81](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/hooks/useInboxPoller.ts#L81)
- worker 处理 plan approval response: [useInboxPoller.ts#L156](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/hooks/useInboxPoller.ts#L156)
- leader 将 permission request 推入 `ToolUseConfirmQueue`: [useInboxPoller.ts#L250](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/hooks/useInboxPoller.ts#L250)
- worker 消费 permission response: [useInboxPoller.ts#L382](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/hooks/useInboxPoller.ts#L382)
- leader 处理 sandbox permission request: [useInboxPoller.ts#L399](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/hooks/useInboxPoller.ts#L399)
- leader auto-approve plan approval request: [useInboxPoller.ts#L599](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/hooks/useInboxPoller.ts#L599)
- leader 处理 shutdown approval: [useInboxPoller.ts#L679](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/hooks/useInboxPoller.ts#L679)

因此, mailbox 是 transport, `useInboxPoller()` 是 REPL 侧的协议调度器。

## 11. 权限桥与 permission sync

### 11.1 leader UI 桥

in-process teammate 不能直接弹出自己的 React permission dialog, 所以通过 module-level bridge 复用 leader UI:

- `registerLeaderToolUseConfirmQueue()`: [leaderPermissionBridge.ts#L28](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/swarm/leaderPermissionBridge.ts#L28)
- `registerLeaderSetToolPermissionContext()`: [leaderPermissionBridge.ts#L42](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/swarm/leaderPermissionBridge.ts#L42)

REPL 注册这些桥接点的位置:

- confirm queue: [REPL.tsx#L1179](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/screens/REPL.tsx#L1179)
- permission context setter: [REPL.tsx#L2379](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/screens/REPL.tsx#L2379)

### 11.2 disk-level permission sync

`permissionSync.ts` 解决的是“worker 向 leader 发权限请求/响应”的另一条控制面。

关键入口:

- permission 请求目录: [permissionSync.ts#L112](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/swarm/permissionSync.ts#L112)
- requestId 生成: [permissionSync.ts#L160](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/swarm/permissionSync.ts#L160)
- 创建请求对象: [permissionSync.ts#L167](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/swarm/permissionSync.ts#L167)
- 团队领导/worker 判断: [permissionSync.ts#L581](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/swarm/permissionSync.ts#L581), [permissionSync.ts#L596](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/swarm/permissionSync.ts#L596)

worker 侧响应回调注册则在 [useSwarmPermissionPoller.ts#L82](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/hooks/useSwarmPermissionPoller.ts#L82)。

## 12. teammate 初始化、激活与恢复

`initializeTeammateHooks()` 在 [teammateInit.ts#L28](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/swarm/teammateInit.ts#L28)。

它主要做两件事:

- 应用 team-wide allowed paths
- 注册 Stop hook, 在 teammate 停止时把自己标记为 idle 并通知 leader: [teammateInit.ts#L105](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/swarm/teammateInit.ts#L105)

而 teammate 开始工作时, REPL 会主动把成员状态标为 active: [REPL.tsx#L2862](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/screens/REPL.tsx#L2862)。

初始化 hook 在 REPL 里统一触发:

- [REPL.tsx#L808](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/screens/REPL.tsx#L808)

## 13. UI 里的 team 视角

从 UI 层看, team 体系的主要可见物是:

- `expandedView: 'teammates'`: [AppStateStore.ts#L95](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/state/AppStateStore.ts#L95)
- `teamContext`：当前团队结构: [AppStateStore.ts#L323](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/state/AppStateStore.ts#L323)
- teammate transcript view 切换: [teammateViewHelpers.ts#L46](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/state/teammateViewHelpers.ts#L46), [teammateViewHelpers.ts#L88](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/state/teammateViewHelpers.ts#L88)
- REPL 中的 teamContext 订阅: [REPL.tsx#L633](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/screens/REPL.tsx#L633)

特别要注意:

- in-process teammate 也会被包装成 task 行, 这样 UI 不需要区分 backend
- transcript 视图和 background task 视图都建立在 task 层, 不是直接建立在 team file 上

## 14. 这套实现最值得记住的设计点

### 14.1 roster 是扁平的

team 成员统一写在 `TeamFile.members` 中, 没有层级 provenance。于是 `AgentTool` 才会显式禁止 teammate 再 spawn teammate: [AgentTool.tsx#L266](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/tools/AgentTool/AgentTool.tsx#L266)。

### 14.2 transport 统一, backend 可替换

不管 teammate 是:

- in-process
- tmux pane
- iTerm2 pane

最终都尽量统一在:

- `TeamFile`
- mailbox
- `AppState.teamContext`
- task 可视化

这让 UI、协议、恢复链都不需要强依赖具体 backend。

### 14.3 leader 永远是协议中心

leader 拥有:

- team 创建/删除权限
- permission dialog 主 UI
- inbox 调度器
- plan approval 权限
- teammate 状态汇总视角

worker/teammate 主要负责:

- 执行具体任务
- 通过 mailbox 请求权限、发送 idle、回应 shutdown、提交 plan

### 14.4 in-process 模式不是“简化版”, 而是正式 backend

从 [registry.ts#L425](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/swarm/backends/registry.ts#L425)、[spawnInProcess.ts#L104](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/swarm/spawnInProcess.ts#L104)、[inProcessRunner.ts#L883](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/swarm/inProcessRunner.ts#L883) 可以看出:

- in-process 不是测试 fallback 而已
- 它有完整的 task、权限、mailbox、idle/shutdown、prompt loop 和 UI 集成

### 14.5 Claude 不是用显式 Policy 类, 而是用结构约束 + 运行时约定

如果从“`RolePolicy / AuthorityPolicy / ClaimPolicy` 有没有独立实现对象”这个角度看, Claude Code 的答案基本是: 没有。

它更接近一种隐式治理模型, 由下面几类机制共同拼出来:

- team 拓扑本身就是权限边界
- leader 身份在 `teamContext` 和 REPL 中天然特殊
- mailbox 默认按收件人隔离, 不是全员共享聊天室
- teammate 通过持续 loop 和 task list claim 获得自治
- 权限和 plan/shutdown 之类的高风险动作, 仍然回到 leader UI 或 leader inbox 做收敛

因此 Claude 解决的不是“显式策略对象怎么写”, 而是“如何用固定拓扑和持续运行时约定, 让 team 自然表现出角色分工”。

### 14.6 Claude 如何处理“谁能向谁发任务”

Claude 主要不是靠一个显式的 `AssignmentPolicy` 做这件事, 而是靠 team 结构本身限制派工关系。

第一层约束是 roster 结构:

- `TeamFile` 只有一个 `leadAgentId`
- `members` 是扁平数组, 不带父子 provenance: [teamHelpers.ts#L64](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/swarm/teamHelpers.ts#L64)

第二层约束是 spawn 入口:

- teammate 不能再 spawn teammate: [AgentTool.tsx#L266](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/tools/AgentTool/AgentTool.tsx#L266)
- in-process teammate 不能再起 background agents: [AgentTool.tsx#L277](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/tools/AgentTool/AgentTool.tsx#L277)

这意味着 Claude 的 team 默认是:

- 一个 leader
- 多个平级 teammate
- 没有 leader 之下再嵌套 leader 或 teammate 的层级树

所以 Claude 里“谁能向谁发任务”的真实答案更接近:

- leader 可以创建 team、spawn teammate、把 prompt 发给 teammate
- teammate 可以和其他成员发消息, 也可以在共享 task list 上创建/认领工作
- 但 teammate 不能继续扩展 roster 结构本身

也就是说, Claude 把“派工边界”大部分固化在 team 拓扑里, 而不是在一个单独的权限图里。

### 14.7 Claude 如何处理“谁能改什么”

Claude 这部分也不是正式的 authority graph, 而是几种软硬约束叠加:

第一类是 team 级共享编辑边界:

- `TeamFile` 里直接持久化 `teamAllowedPaths`: [teamHelpers.ts#L70](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/swarm/teamHelpers.ts#L70)
- teammate 初始化时会应用这些 team-wide allowed paths: [teammateInit.ts#L28](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/swarm/teammateInit.ts#L28)

第二类是运行时权限申请/解析:

- `PermissionResolution` 明确定义 `resolvedBy: 'worker' | 'leader'`: [permissionSync.ts#L95](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/swarm/permissionSync.ts#L95)
- permission request 创建时会带上 worker identity: [permissionSync.ts#L167](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/swarm/permissionSync.ts#L167)
- leader 侧通过 `leaderPermissionBridge` 把权限 UI 暴露给 in-process teammate 复用: [leaderPermissionBridge.ts#L28](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/swarm/leaderPermissionBridge.ts#L28)

第三类是协议级强约束:

- plan approval response 只有来自 `team-lead` 才会被 teammate 接受: [useInboxPoller.ts#L156](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/hooks/useInboxPoller.ts#L156)
- shutdown、sandbox permission、tool confirm 这些高风险动作最终都回收到 leader inbox / leader UI 路径

所以 Claude 的“谁能改什么”不是一个纯静态矩阵, 而是:

- 常规范围靠 `teamAllowedPaths`
- 越权操作靠 permission sync / leader bridge
- 高风险协议靠 leader 身份校验

### 14.8 Claude 如何处理“谁负责收敛”

Claude 的收敛中心非常明确, 就是 leader。

证据主要有三类:

- team metadata 中只有一个 `leadAgentId`: [teamHelpers.ts#L64](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/swarm/teamHelpers.ts#L64)
- leader 的 `useInboxPoller()` 会持续轮询 unread mailbox, 并在空闲时直接把消息提交成新 turn, 忙时先排队: [useInboxPoller.ts#L119](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/hooks/useInboxPoller.ts#L119)
- in-process teammate 的 idle loop 会先看 mailbox, 再看 task list 是否有可 claim 工作: [inProcessRunner.ts#L853](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/swarm/inProcessRunner.ts#L853)

这形成的运行形态是:

- teammate 常驻执行、持续接消息、持续 claim 工作
- leader 不必同步阻塞等待某个 worker 结束
- 但 leader 仍然是最终的 inbox convergence center

因此 Claude 的 team 很强, 但它并没有取消收敛中心, 而是把 team 做成:

- teammate 负责并行执行和局部自治
- leader 负责异步收消息、处理权限、做最终收束

这也是为什么 Claude 可以在 team 内实现很强的协作感, 但系统级仍然保持清楚的收敛中心。

## 15. 后续精读建议

如果还要继续深挖 team 体系, 建议下一轮按这个顺序细读:

1. [spawnMultiAgent.ts](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/tools/shared/spawnMultiAgent.ts)
2. [backends/registry.ts](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/swarm/backends/registry.ts)
3. [spawnInProcess.ts](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/swarm/spawnInProcess.ts)
4. [inProcessRunner.ts](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/swarm/inProcessRunner.ts)
5. [useInboxPoller.ts](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/hooks/useInboxPoller.ts)
6. [teammateMailbox.ts](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/teammateMailbox.ts)
7. [teamHelpers.ts](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/swarm/teamHelpers.ts)
