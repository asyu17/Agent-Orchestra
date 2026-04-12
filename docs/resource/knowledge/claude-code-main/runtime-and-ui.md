# claude-code-main 运行时与 UI 主链路

## 1. 一句话结论

Claude Code 的交互态主架构可以概括为：

`CLI 分发 -> 主线程装配 -> trust/onboarding 边界 -> AppState + REPL 宿主 -> processUserInput -> query kernel -> tool orchestration`

## 2. 范围与资料来源

- 分析对象：`resource/others/claude-code-main/src` 的运行时与 UI 主链路
- 关注范围：CLI 入口、初始化、REPL 宿主、AppState、`processUserInput`、`query` 与 tool orchestration
- 关联文档：`README.md`、`extensibility-and-integrations.md`、`directory-inventory.md`

## 3. 启动总览

可以把主线程启动链看成下面这条路径:

```text
entrypoints/cli.tsx
  -> main.tsx
     -> init()
     -> setup()
     -> getCommands() / getAgentDefinitionsWithOverrides()
     -> showSetupScreens()
     -> launchRepl()
        -> <App>
           -> <REPL>
```

其中每一层都不是薄封装, 而是明确的职责边界。

## 4. `entrypoints/cli.tsx`: 快路径分发器

### 4.1 角色

`src/entrypoints/cli.tsx` 负责在“还没加载整个应用”之前处理各种高频或特化入口, 目的是减少模块求值成本。

### 4.2 它做的事

- 设置若干启动期环境变量, 例如 corepack 和 remote 环境的 `NODE_OPTIONS`
- 对以下模式走 fast-path:
  - `--version`
  - `--dump-system-prompt`
  - `--claude-in-chrome-mcp`
  - `--chrome-native-host`
  - `--daemon-worker`
  - `remote-control` / `bridge`
  - `daemon`
  - `ps/logs/attach/kill`
  - `templates`
  - `environment-runner`
  - `self-hosted-runner`
  - `--worktree --tmux`

### 4.3 架构意义

这说明 Claude Code 把 CLI 看成“多产品入口容器”, 而不是单 REPL 二进制。`cli.tsx` 是入口级路由器。

## 5. `main.tsx`: 主装配器

### 5.1 启动前置 side-effect

`src/main.tsx` 文件顶部先触发两类预热:

- `startMdmRawRead()`
- `startKeychainPrefetch()`

目的是把 MDM / keychain 等慢 I/O 与后续 import 并行化。

### 5.2 主职责

`main.tsx` 同时负责:

- 解析 CLI 参数
- 决定 permission mode / model / session / remote / resume 等主配置
- 触发 `init()`
- 在 `setup()` 时处理 cwd / worktree / socket 等环境准备
- 预先注册 bundled skills / builtin plugins
- 并行加载 commands 与 agent definitions
- 在交互态创建 Ink root, 显示 setup screens, 最终启动 REPL
- 在 headless/SDK 模式下创建 `QueryEngine` 或相应执行上下文

### 5.3 关键设计

- `setup()` 与 `getCommands()` / `getAgentDefinitionsWithOverrides()` 被显式并行
- `getTools()` 在很多路径都提前调用, 说明 tool pool 是非常核心的运行时前置条件
- setup screen 之前会创建 Ink root, 但只有通过 trust / onboarding / config 检查后才真正进入 REPL

## 6. `entrypoints/init.ts`: 初始化的是平台, 不是 UI

`src/entrypoints/init.ts` 的 `init()` 更接近“平台基础设施初始化”, 其重点是:

- 启用配置系统 `enableConfigs()`
- 应用 safe env 和 CA cert
- 注册 graceful shutdown
- 启动 analytics / 1P event logging
- 初始化 OAuth 账户信息
- 预热 JetBrains 检测和仓库检测
- 初始化 remote managed settings 与 policy limits 的加载 Promise
- 配置 mTLS / proxy / API preconnect
- 在 remote 场景初始化 upstream proxy
- 注册 LSP manager 和 team cleanup 的清理逻辑

结论:

- `init()` 不是单纯的“读配置”
- 它实际上把 auth、network、telemetry、settings、cleanup、proxy 这些基础设施都拉起来了

## 7. `interactiveHelpers.tsx`: trust/onboarding/telemetry 关口

### 7.1 `showSetupScreens()`

`src/interactiveHelpers.tsx` 是主线程进入 REPL 前的交互式安全边界。其顺序大致是:

1. onboarding
2. trust dialog
3. GrowthBook reset + re-init
4. system context 预热
5. mcp.json server approval
6. CLAUDE.md external include approval
7. 应用 full env
8. 初始化 trust 之后的 telemetry
9. API key / bypassPermissions / auto mode 等额外确认

### 7.2 架构意义

- “权限模式” 不等于 “trust”
- trust 被单独当作 workspace 边界
- 配置环境变量分为 trust 前和 trust 后两阶段应用

## 8. `replLauncher.tsx` + `components/App.tsx`: 最薄的 REPL 外壳

### 8.1 `replLauncher.tsx`

它的职责很单纯:

- 动态 import `App`
- 动态 import `REPL`
- 通过 `renderAndRun()` 挂到 Ink root 上

### 8.2 `components/App.tsx`

`App` 只做顶层 provider 包装:

- `FpsMetricsProvider`
- `StatsProvider`
- `AppStateProvider`

说明真正的复杂度不在 `App`, 而在 `REPL` 与各类 hooks。

## 9. `state/AppStateStore.ts`: 中央状态模型

`src/state/AppStateStore.ts` 是理解 Claude Code 前台行为的关键锚点。`AppState` 至少包含这些核心区块:

- settings / verbose / model / effort / thinking
- `toolPermissionContext`
- tasks / foregroundedTaskId / viewingAgentTaskId
- mcp clients / tools / commands / resources
- plugins enabled / disabled / errors / installationStatus
- agentDefinitions / agentNameRegistry
- notifications / elicitation
- fileHistory / attribution / todos
- remote session 和 repl bridge 的连接状态
- 一些 feature-specific UI 状态, 如 `bagelActive`, `tungstenPanelVisible`

### 9.1 设计信号

- AppState 不只是 UI state, 也承载运行时集成状态
- `mcp`、`plugins`、`tasks`、`toolPermissionContext` 都进入中央 store
- 这让 `REPL` 能把 UI、权限、远程连接、任务树、扩展能力统一协调

## 10. `state/AppState.tsx`: 自定义 store, 避免全树重渲染

`src/state/AppState.tsx` 的关键点:

- `AppStateProvider` 基于 `createStore()` 构造外部 store
- 读取使用 `useSyncExternalStore`
- 提供 `useAppState(selector)` 和 `useSetAppState()`
- 通过 `useSettingsChange()` 把外部 settings 变化同步进 AppState

这套设计说明:

- 他们刻意没有把 AppState 建在 React `useReducer` 上
- 重点是降低大体量终端 UI 的重渲染成本

## 11. `state/onChangeAppState.ts`: 全局副作用同步点

这是一个非常关键的“单向副作用出口”。当 AppState 变化时, 它负责:

- 把 permission mode 同步给外部 session metadata / SDK
- 持久化 model 变更
- 持久化 expanded view / verbose / tungsten panel 等 UI 配置
- 在 settings 变化时清理 auth cache 并重应用 env

理解这一点很重要:

- AppState 改变不是局部 React 事件
- 它往往会触发会话元数据、配置、环境变量和远端桥接状态同步

## 12. `screens/REPL.tsx`: 真正的前台控制器

### 12.1 定位

`src/screens/REPL.tsx` 是整个产品交互态的总控文件。它的职责明显超出“聊天输入框”:

- 组装工具池、命令池、MCP 客户端、remote session、IDE 集成
- 管理 prompt 输入、消息历史、搜索、高亮、message selector
- 管理 permission request、elicitation、task list、teammate view
- 执行用户输入处理和 query loop
- 管理 transcript、file history、compact、resume、rewind

### 12.2 关键组合点

- `useMergedTools()` 合并 initial tools 与 MCP tools
- `useMergedCommands()` 合并 initial commands 与 MCP commands
- `getToolUseContext(...)` 构造 query 与 tool 执行时共享的运行上下文
- `query()` 承担模型与工具执行循环

### 12.3 架构结论

Claude Code 的 REPL 实际上是一个“UI + orchestrator”组合体, 不是只负责渲染消息列表。

## 13. Tool/Query 主执行流

### 13.1 工具抽象: `Tool.ts`

`src/Tool.ts` 定义了核心 `Tool` 契约。一个 tool 至少要回答这些问题:

- 输入 schema 是什么
- 如何执行 `call()`
- 是否只读 / destructive / concurrency safe
- 如何检查权限 `checkPermissions()`
- 如何渲染 tool use / progress / result / error
- 是否可被中断 `interruptBehavior()`

这说明 Claude Code 把 tool 视为“可执行能力 + 权限语义 + UI 表现”的统一对象。

### 13.2 工具注册与装配: `tools.ts`

`src/tools.ts` 的结构值得单独记住:

- `getAllBaseTools()` 是静态来源总表
- `getTools(permissionContext)` 负责按环境、feature gate、deny rules、REPL 模式筛内建工具
- `assembleToolPool(permissionContext, mcpTools)` 负责把内建工具和 MCP 工具合并成完整工具池

这意味着:

- 内建 tool 和 MCP tool 在最终运行时是同一等级的
- tool pool 不是写死的, 而是运行时拼装的

### 13.3 用户输入预处理: `processUserInput.ts`

`src/utils/processUserInput/processUserInput.ts` 负责:

- 判断 slash command
- 处理 pasted content / attachments / IDE selection
- 执行 UserPromptSubmit hooks
- 决定这次输入是本地命令、需要查询的普通消息, 还是只产生系统消息

所以 query 前并不是直接把文本发给模型, 而是先做一次“命令/附件/钩子分流”。

### 13.4 系统提示词构建: `queryContext.ts`

`src/utils/queryContext.ts` 的 `fetchSystemPromptParts()` 把 cache-key 相关上下文拆成三块:

- `defaultSystemPrompt`
- `userContext`
- `systemContext`

这为 query 层做 prompt cache、custom prompt 替换和 fallback 提供了统一入口。

### 13.5 Headless 主线: `QueryEngine.ts`

`src/QueryEngine.ts` 的定位是“非 UI 宿主”。它负责:

- 维护 `mutableMessages`
- 构造 `ProcessUserInputContext`
- 调用 `processUserInput()`
- 记录 transcript
- 构建 system prompt
- 调用 `query()`
- 生成 SDK message / result / permission denial 输出

注意:

- `QueryEngine` 自己不做真正的模型/tool loop
- 它是 `query()` 的宿主和适配器

### 13.6 核心 query loop: `query.ts`

`src/query.ts` 是模型调用和工具循环的核心。其职责包括:

- 规范化消息与上下文
- 调模型流式返回
- compact / microcompact / reactive compact
- tool use summary
- token budget 和 max output token 恢复
- stop hooks
- 在需要时调用 `runTools()` 或 `StreamingToolExecutor`

可以把它理解成 Claude Code 的“turn execution kernel”。

### 13.7 工具执行层: `services/tools/*`

### `toolExecution.ts`

负责单个工具调用的完整生命周期:

- permission decision
- telemetry
- pre/post tool hooks
- 错误分类
- tool result block 处理

### `toolOrchestration.ts`

负责把多个 tool use block 分批:

- concurrency safe 的工具尽量并发
- 非只读/非并发安全工具串行

### `StreamingToolExecutor.ts`

负责“工具边 streaming 边执行”的队列控制:

- 按收到顺序缓存 tool
- 并发安全的工具并行跑
- 非并发安全工具独占
- 结果按接收顺序 yield

这几层叠起来, 才是 Claude Code 真正的 agentic tool runtime。

## 14. 一句话模型

如果只用一句话总结本文件:

Claude Code 的交互态主架构是:

`CLI 分发 -> 主线程装配 -> trust/onboarding 边界 -> AppState + REPL 宿主 -> processUserInput -> query kernel -> tool orchestration`

## 15. 相关文档

- `README.md`
- `extensibility-and-integrations.md`
- `directory-inventory.md`
- `agent-team.md`
