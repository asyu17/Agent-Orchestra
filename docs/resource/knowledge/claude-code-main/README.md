# claude-code-main 知识包

## 1. 一句话结论

这份知识包对应的不是“一个普通 CLI 仓库”，而是一套围绕 `Query + Tool + AppState` 组织起来的终端 Agent 运行时。

## 2. 这个知识包解决什么问题

这个知识包用于回答下面几类问题：

- Claude Code 源码快照的整体结构是什么
- REPL、Query、Tool、Task、MCP、plugin、skills 分别落在哪里
- `agent team` / `swarm` 机制是如何接入主运行时的
- 如果要继续阅读源码，应该先看哪些文件

如果任务涉及 Claude Code 主结构、运行时链路、扩展体系、agent team 或目录盘点，应先读本知识包。

## 3. 范围与资料来源

- 源目录：`resource/others/claude-code-main`
- 快照说明：仓库 `README.md` 标注这是一个通过 npm 包 source map 暴露出来的 Claude Code `src/` 快照，公开暴露时间为 `2026-03-31`
- 代码规模：
  - 本地扫描：`1902` 个文件，`513237` 行
  - `README.md` 描述：TypeScript + Bun + React/Ink 的完整 CLI 工程

## 4. 推荐阅读顺序

如果目的是快速建立整体心智模型，建议按下面顺序阅读：

1. `runtime-and-ui.md`
2. `extensibility-and-integrations.md`
3. `agent-team.md`
4. `directory-inventory.md`

如果目的是继续下钻源码，可按下面顺序进入源码入口：

1. `src/entrypoints/cli.tsx`
2. `src/main.tsx`
3. `src/interactiveHelpers.tsx`
4. `src/replLauncher.tsx`
5. `src/components/App.tsx`
6. `src/state/AppStateStore.ts`
7. `src/state/AppState.tsx`
8. `src/screens/REPL.tsx`
9. `src/Tool.ts`
10. `src/tools.ts`
11. `src/query.ts`
12. `src/services/tools/toolExecution.ts`
13. `src/QueryEngine.ts`
14. `src/commands.ts`
15. `src/skills/loadSkillsDir.ts`
16. `src/utils/plugins/loadPluginCommands.ts`
17. `src/services/mcp/client.ts`
18. `src/bridge/bridgeMain.ts`

## 5. 文件地图

- `runtime-and-ui.md`
  - 启动链路、REPL/UI、AppState、`query`/tool loop 主执行流
- `extensibility-and-integrations.md`
  - `commands`、`skills`、`plugins`、MCP、bridge、remote、server、task 系统
- `agent-team.md`
  - `agent swarm` / `team` 的实现逻辑，覆盖 team file、spawn 路由、backend、mailbox、权限桥和 REPL 接点
- `directory-inventory.md`
  - 目录规模、热点子目录、关键大文件清单

## 6. 适用范围

适用于以下任务：

- 阅读或仿制 Claude Code 主架构
- 分析 REPL、Query、Tool、Task 的协同关系
- 理解 `agent team`、`swarm`、`teammate` 的运行机制
- 查找 Claude Code 目录热点、阅读顺序和关键文件

## 7. 维护要求

维护本知识包时，应保持：

- 包内 `README.md` 的阅读顺序和文件地图同步更新
- 所有专题文档使用统一标题层级
- 新增专题文档时，回填到本文件的文件地图
- 结论、判断和来源信息保持可追溯
