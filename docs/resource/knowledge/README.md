# Repository Knowledge Base

## 1. 一句话结论

`resource/knowledge/` 是本仓库所有 agent 的执行前必读入口，也是可复用知识的唯一固化目录。

## 2. 所有 Agent 执行前必读

所有 agent 在开始实质性分析、实现、迁移、重构、复盘或文档工作前，都必须按下面顺序阅读：

1. 先读本文件。
2. 根据任务主题匹配相关知识包。
3. 进入对应知识包目录，先读该目录下的 `README.md`。
4. 再读其中直接相关的专题文档后，才能开始执行。

如果任务跨越多个主题，就读取多个知识包。

如果当前没有匹配的知识包，允许先执行任务，但在结束前必须按标准创建或补齐知识。

## 3. 标准化知识固化流程

知识固化的唯一规范文件是：

- `resource/knowledge/knowledge-solidification-standard.md`

执行时至少遵循这条最小流程：

1. 判断这次工作是否产生了可复用知识。
2. 决定应更新现有知识包还是创建新知识包。
3. 按统一格式写入知识包 `README.md` 或专题文档。
4. 回填相关链接、阅读顺序和文件地图。
5. 完成前做一次结构校验。

## 4. Knowledge Package 结构

每个知识主题都必须放在独立目录中：

- 路径：`resource/knowledge/<topic>/`
- 必备文件：`README.md`
- 可选文件：若干专题文档，例如架构分析、运行手册、契约、失败模式、目录盘点

统一约束：

- 包级入口由 `README.md` 承担
- 专题文档负责承载细分主题
- 所有文档统一使用 `#`、`##`、`###`
- 所有专题文档都要有 `## 1. 一句话结论` 和 `## 2. 范围与资料来源`

## 5. 现有知识包索引

| 知识包 | 主题 | 何时应该先读 | 入口文件 |
| --- | --- | --- | --- |
| `claude-code-main` | Claude Code 源码快照结构、运行时、扩展体系、agent team | 任务涉及 Claude Code 主结构、Query/Tool 运行时、swarm/team、MCP、plugin、目录盘点 | `resource/knowledge/claude-code-main/README.md` |
| `multi-agent-team-delivery` | 多 agent / 多 team 交付模型、失败模式、运行清单、交付契约 | 任务涉及多 team 协作、authority root、handoff、reducer、完成语义、运行手册 | `resource/knowledge/multi-agent-team-delivery/README.md` |
| `agent-orchestra-runtime` | 本仓库 Python `agent_orchestra` 运行时架构、Claude Code 映射、group/team/runtime 边界，以及 host-owned coordination/session truth | 任务涉及本仓库 Python runtime、PostgreSQL + Redis 协作层、OpenAI runner、group/team 抽象、single-owner session truth 与后续扩展 | `resource/knowledge/agent-orchestra-runtime/README.md` |
| `local-dev-runtime` | 本仓库开发机的本地宿主运行环境，尤其是 Docker / Colima 的目录迁移与空间管理 | 任务涉及 macOS 本地 Docker / Colima、`~/.colima` 迁移、外置盘放置、宿主环境排障 | `resource/knowledge/local-dev-runtime/README.md` |
| `repository-public-release` | 仓库公开版 / 干净版本导出规则，尤其是 `.ignores` 边界、内部痕迹排除与公开文档保留规则 | 任务涉及准备公开仓库、导出 clean version、维护排除清单、判断 `docs/` 下哪些内容应公开 | `resource/knowledge/repository-public-release/README.md` |
| `synthetic-sensor-dataset-review` | 合成传感器点云/数据集论文的 reviewer 检查框架，重点是 evaluation design、sim-to-real、dataset claim 与可复现性 | 任务涉及审阅或分析视频/mesh 到 mmWave/LiDAR/radar 点云生成项目，判断论文故事是否成立 | `resource/knowledge/synthetic-sensor-dataset-review/README.md` |

## 6. 维护要求

新增或更新知识时，必须同时维护这些内容：

- 知识包 `README.md` 的阅读顺序
- 知识包 `README.md` 的文件地图
- 相关专题文档之间的交叉引用
- 本文件中的知识包索引，如果新增了知识包

## 7. 相关规范

- 规范源：`resource/knowledge/knowledge-solidification-standard.md`
- 仓库级入口：`AGENTS.md`
