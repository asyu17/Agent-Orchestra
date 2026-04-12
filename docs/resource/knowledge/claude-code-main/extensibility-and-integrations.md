# claude-code-main 扩展体系与外部集成

## 1. 一句话结论

Claude Code 的扩展体系不是“命令 + 插件”两层，而是统一 `command/tool/task` 抽象与 markdown skills、plugin markdown、MCP 总线、bridge/remote 运行时共同组成的可装配系统。

## 2. 范围与资料来源

- 分析对象：`resource/others/claude-code-main/src` 中与扩展、集成、远程运行时有关的模块
- 关注范围：`commands`、`skills`、`plugins`、tools、MCP、bridge、remote、server、task
- 关联文档：`README.md`、`runtime-and-ui.md`、`agent-team.md`

## 3. 命令、技能、插件其实是一套统一模型

### 3.1 `types/command.ts` 是总契约

`src/types/command.ts` 把 command 定义成三类:

- `prompt`: 展开成一段 prompt 内容, 给模型使用
- `local`: 本地逻辑命令
- `local-jsx`: 会渲染 Ink UI 的本地命令

关键字段还包括:

- `source`: `builtin | mcp | plugin | bundled | settingSource`
- `loadedFrom`: `skills | plugin | bundled | mcp | ...`
- `whenToUse`
- `allowedTools`
- `disableModelInvocation`
- `context: inline | fork`
- `agent`
- `effort`

结论:

- Claude Code 把 skill 不是做成单独 DSL
- skill 本质上是 command 的一个子集, 主要是 `prompt` 型 command

## 4. `commands.ts`: 把多来源 command 装起来

### 4.1 内建命令

`src/commands.ts` 先声明大批 builtin slash commands, 再通过 `COMMANDS()` 形成内建列表。

### 4.2 动态来源

`loadAllCommands(cwd)` 会把这些来源合并:

1. bundled skills
2. builtin plugin skills
3. skill dir commands
4. workflow commands
5. plugin commands
6. plugin skills
7. 内建 commands

### 4.3 重要判断

- command 列表是动态拼接的
- builtin 只是其中一种来源
- dynamic skills 会被插到 builtins 之前

这意味着 Claude Code 的 slash command 系统本质上是一个“可合并的命令目录”。

## 5. skill 系统

### 5.1 入口文件

最关键的文件是 `src/skills/loadSkillsDir.ts`。

### 5.2 skill 来源

它支持从不同 source 解析 skills:

- `userSettings`
- `projectSettings`
- `policySettings`
- `plugin`
- `bundled`
- `mcp`

### 5.3 skill 解析内容

它不仅仅读 markdown, 还会解析:

- frontmatter
- description
- allowed-tools
- argument-hint / arguments
- when_to_use
- version / model / effort
- hooks
- context 是否为 `fork`
- agent 类型
- paths 限制

### 5.4 架构意义

skill 在 Claude Code 里是“带 frontmatter 的 prompt command”, 而不是孤立文本模板。

## 6. plugin 系统

### 6.1 命令/技能来源

`src/utils/plugins/loadPluginCommands.ts` 负责把 plugin 目录里的 markdown 文件转成 commands 或 skills。

### 6.2 关键行为

- 递归收集 markdown
- 若某个目录有 `SKILL.md`, 该目录按 skill 语义处理
- 生成命名空间化的 command name
- 支持插件变量替换, 例如 `CLAUDE_PLUGIN_ROOT`
- 从 frontmatter 提取 allowed-tools、model、effort、hooks 等信息

### 6.3 架构判断

plugin 并不只是“加载 JS 模块”:

- 它也能通过 markdown 成为 skill / command 来源
- plugin 在系统里首先表现为“可装配的能力包”

## 7. tool 体系与外部接入

### 7.1 `tools.ts` 是内建工具注册总表

内建工具集中注册在 `src/tools.ts`, 包括:

- `BashTool`
- `FileReadTool`
- `FileEditTool`
- `FileWriteTool`
- `NotebookEditTool`
- `WebFetchTool`
- `WebSearchTool`
- `AgentTool`
- `SkillTool`
- `LSPTool`
- `Task*Tool`
- `EnterPlanModeTool`
- `EnterWorktreeTool`
- `ListMcpResourcesTool`
- `ReadMcpResourceTool`
- 以及一批 feature-gated 工具

### 7.2 工具热点目录

局部扫描显示工具复杂度最高的几个桶是:

- `tools/BashTool`: `18` 文件, `12414` 行
- `tools/PowerShellTool`: `14` 文件, `8961` 行
- `tools/AgentTool`: `20` 文件, `6784` 行
- `tools/LSPTool`: `6` 文件, `2006` 行
- `tools/SkillTool`: `4` 文件, `1478` 行

这说明 shell、sub-agent、LSP、skill 都不是“薄包装”, 而是重逻辑模块。

## 8. MCP 系统

### 8.1 核心入口

`src/services/mcp/client.ts` 是最核心的 MCP 适配层。

### 8.2 它做什么

- 连接多种 transport:
  - stdio
  - SSE
  - streamable HTTP
  - WebSocket
- 拉取并构造:
  - MCP tools
  - MCP prompts
  - MCP resources
- 处理:
  - OAuth / auth refresh
  - elicitation
  - tool result truncation / persistence
  - MCP tool naming 规范化
  - session expired / auth error

### 8.3 系统意义

MCP 在 Claude Code 里不是“额外工具插件”, 而是正式的一等扩展总线:

- tools 可来自 MCP
- commands / skills 也可来自 MCP
- resources 通过专门 tool 暴露给模型

## 9. bridge / remote / server 三层关系

### 9.1 `bridge/*`: 把本机暴露成远程控制环境

`src/bridge/bridgeMain.ts` 是 remote-control 模式的核心:

- 创建 bridge API client
- 轮询 work items
- 维护 active sessions
- 生成 worktree
- spawn 子 claude 进程
- 心跳和 token refresh
- session reconnect / timeout / cleanup

最准确的理解是:

- `bridge` 是“把本机包装成可被远端调度的执行环境”

### 9.2 `remote/*`: 消费远端 session

`src/remote/RemoteSessionManager.ts` 的职责是:

- 建立 WebSocket 订阅
- 接收 SDK message
- 处理 control request / permission request
- 通过 HTTP 把用户消息发回远端 session

准确地说:

- `remote` 是“本地 UI 作为远端 session 的客户端”

### 9.3 `server/*`: direct-connect 协议入口

`src/server/createDirectConnectSession.ts` 表明 direct-connect 是另一种接入方式:

- 向 `${serverUrl}/sessions` POST
- 解析 `session_id`、`ws_url`、`work_dir`
- 返回 `DirectConnectConfig`

这层比 bridge 更薄, 更像“指定 server 的直连协议适配器”。

## 10. task 系统

### 10.1 关键入口

- `src/Task.ts`
- `src/tasks.ts`

### 10.2 任务类型

`Task.ts` 中定义的 `TaskType` 包括:

- `local_bash`
- `local_agent`
- `remote_agent`
- `in_process_teammate`
- `local_workflow`
- `monitor_mcp`
- `dream`

### 10.3 任务注册

`tasks.ts` 负责把以下任务装起来:

- `LocalShellTask`
- `LocalAgentTask`
- `RemoteAgentTask`
- `DreamTask`
- feature-gated 的 workflow / monitor task

### 10.4 设计含义

Claude Code 不是把后台能力全塞进 tool 调用, 而是额外抽了一层 task runtime 用于:

- 长生命周期子 agent
- shell 任务
- 远端 agent
- workflow / monitor

### 10.5 与 team/swarm 的关系

agent team 不是独立脱离 task 系统运行的。

- 团队创建/删除在 [TeamCreateTool.ts](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/tools/TeamCreateTool/TeamCreateTool.ts) 和 [TeamDeleteTool.ts](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/tools/TeamDeleteTool/TeamDeleteTool.ts)
- teammate spawn 主入口在 [spawnMultiAgent.ts](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/tools/shared/spawnMultiAgent.ts)
- in-process teammate 运行态落在 [spawnInProcess.ts](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/swarm/spawnInProcess.ts) 和 [inProcessRunner.ts](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/swarm/inProcessRunner.ts)
- leader/worker 的控制面统一走 [teammateMailbox.ts](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/utils/teammateMailbox.ts) 与 [useInboxPoller.ts](/Volumes/disk1/Document/code/Agent-Orchestra/resource/others/claude-code-main/src/hooks/useInboxPoller.ts)

team 体系的详细拆解已经单独写在 [agent-team.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/claude-code-main/agent-team.md)。

## 11. 如果要移植或仿制, 应先研究哪些锚点

### 第一优先级: 先吃透宿主和执行内核

- `src/main.tsx`
- `src/screens/REPL.tsx`
- `src/QueryEngine.ts`
- `src/query.ts`
- `src/Tool.ts`
- `src/tools.ts`

### 第二优先级: 吃透扩展接入

- `src/commands.ts`
- `src/types/command.ts`
- `src/skills/loadSkillsDir.ts`
- `src/utils/plugins/loadPluginCommands.ts`
- `src/services/mcp/client.ts`

### 第三优先级: 吃透远程控制

- `src/bridge/bridgeMain.ts`
- `src/remote/RemoteSessionManager.ts`
- `src/server/createDirectConnectSession.ts`

## 12. 一句话模型

Claude Code 的扩展体系不是“命令 + 插件”两层, 而是:

`统一 command/tool/task 抽象 + markdown skills + plugin markdown + MCP 总线 + bridge/remote 远程运行时`

## 13. 相关文档

- `README.md`
- `runtime-and-ui.md`
- `agent-team.md`
- `directory-inventory.md`
