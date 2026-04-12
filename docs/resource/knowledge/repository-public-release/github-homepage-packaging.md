# Agent-Orchestra GitHub 首页包装规则

## 1. 一句话结论

Agent-Orchestra 的公开首页应采用“工具入口优先，但用真实架构深度建立信任”的双语包装方式：根目录 `README.md` 作为英文主入口，`README.zh-CN.md` 作为中文入口，首页坚持 `usable alpha` 口径，不夸大稳定性，也不把内部协作目录当成公开文档面。

## 2. 范围与资料来源

- 根目录首页文档：`/README.md`、`/README.zh-CN.md`
- 包元信息：`/pyproject.toml`
- 公开发布知识包：`/docs/resource/knowledge/repository-public-release/README.md`
- 公开边界规则：`/docs/resource/knowledge/repository-public-release/public-clean-export-ignore-rules.md`
- 运行时知识入口：`/docs/resource/knowledge/agent-orchestra-runtime/README.md`
- 仓库级约束：`/AGENTS.md`

## 3. 当前首页定位

当前公开定位应保持以下稳定表达：

- 项目类型：`usable alpha`
- 目标读者：Agent Infra / Framework 开发者
- 第一卖点：`multi-team / multi-agent orchestration runtime`
- 语气要求：克制、专业、可验证，不做夸张营销
- 认知锚点：可以明确说明项目受到 Claude Code 风格 team runtime 思路启发，但不能写成官方关系、授权衍生或简单复刻

判断：

- 这个仓库的强项不是“已经完全产品化”，而是“已经有足够深的 runtime、continuity 与 control-plane 实现，值得研究和继续试验”

## 4. README 结构规则

### 4.1 双语入口结构

- 根目录 `README.md` 是英文主首页
- 根目录 `README.zh-CN.md` 是中文首页
- 两份 README 顶部都要提供对方链接
- `pyproject.toml` 的 `project.readme` 应对齐到 `README.md`

### 4.2 首页信息顺序

首页默认顺序应保持：

1. 项目标题与副标题
2. 一小段项目简介
3. 能力亮点
4. 最短 quickstart
5. 为什么值得研究
6. 架构概览
7. 当前状态
8. 仓库结构
9. 文档入口

判断：

- Agent-Orchestra 的首页更适合“先说明能做什么、怎么开始，再解释为什么它在架构上值得看”，而不是一开始就压上过长的架构宣言

## 5. 文案诚实边界

公开首页必须遵守这些表达约束：

- 可以写 `usable alpha`，不能写 `production-ready`
- 可以写已有 CLI、backend、session continuity 控制面，不能把它写成“完整交互式 shell 重连系统”除非已被单独验证
- 可以写仓库拥有较深的 runtime 与知识库文档，不能写“全面稳定”或“所有环境已验证”
- 如果测试、Python 版本兼容性或运行环境存在未完全收口的现实边界，应在首页保持克制，而不是靠模糊措辞掩盖

## 6. Quickstart 规则

首页 quickstart 应优先选择最短、最不依赖外部基础设施的路径：

- 首选 `in-memory` 路径展示最小 session flow
- `postgres` 应作为“需要 durable continuity 时的进阶路径”出现
- CLI 调用应优先给出仓库内最可靠入口，例如 `PYTHONPATH=src python -m agent_orchestra.cli.main ...`

判断：

- 对首次访问者来说，先要求 DSN、schema 和外部服务会明显降低 GitHub 首页转化率，因此不应把 PostgreSQL 初始化放在首页的最前面

## 7. 公开文档与内部文档边界

README 中应优先链接这些公开入口：

- `docs/resource/knowledge/README.md`
- `docs/resource/knowledge/agent-orchestra-runtime/README.md`
- 直接相关的公开架构/操作文档

README 不应把这些目录作为公开首页重点暴露：

- `docs/superpowers/`
- 其他内部协作痕迹、spec、plan 或 run artifacts 目录

这条规则需要和 `public-clean-export-ignore-rules.md` 保持一致。

## 8. 相关文档

- `README.md`
- `README.zh-CN.md`
- `public-clean-export-ignore-rules.md`
- `resource/knowledge/agent-orchestra-runtime/README.md`
