# repository-public-release 知识包

## 1. 一句话结论

这个知识包沉淀 Agent-Orchestra 仓库在“公开仓库 / 导出干净版本”场景下的稳定规则：仓库根目录 `.ignores` 使用 gitignore 风格维护要从公开版中排除的本地缓存、内部协作痕迹与运行产物；当前明确把 `docs/superpowers/` 整体视为内部目录直接排除，而 `docs/resource/knowledge`、源码、测试和基础构建文件继续保留。

## 2. 这个知识包解决什么问题

- 准备公开仓库时，哪些目录和文件应该从干净版本中排除
- `.ignores` 应该采用什么语法和维护边界
- `docs/` 下哪些内容属于可公开文档，哪些属于内部协作痕迹
- 后续新增本地产物或内部运行目录时，应更新哪里

## 3. 范围与资料来源

- 仓库根目录：`.ignores`
- 当前仓库目录结构：`docs/`、`.history/`、`.learnings/`、`.venv/`、`src/agent_orchestra.egg-info/`
- 仓库约束：`AGENTS.md`
- 相关知识：`resource/knowledge/agent-orchestra-runtime/execution-guard-failure-patterns.md`

## 4. 推荐阅读顺序

1. `public-clean-export-ignore-rules.md`
2. `github-homepage-packaging.md`

## 5. 文件地图

- `public-clean-export-ignore-rules.md`
  - 记录公开版导出时的保留 / 排除边界、`.ignores` 的规则意图，以及后续维护清单
- `github-homepage-packaging.md`
  - 记录仓库公开首页的双语结构、对外定位、文案诚实边界，以及 README 与包元信息的对齐规则

## 6. 适用范围

- 准备创建公开仓库、公开镜像、公开分支或发布用压缩包
- 需要新增或调整本地缓存、内部运行痕迹、协作产物的排除规则
- 需要判断 `docs/` 下某个目录应保留还是应从公开版中剔除
- 需要更新 GitHub 首页、README 结构、双语入口或包元信息，使公开仓库表达与真实能力保持一致

## 7. 维护要求

- 公开边界变化时，必须同时更新 `.ignores` 和本知识包
- 新增内部协作目录、运行产物目录或本地缓存目录时，优先补入 `.ignores`
- 如果 `docs/` 的公开 / 内部边界变化，必须同步更新 `public-clean-export-ignore-rules.md`
- 如果对外定位、README 双语结构或首页表达策略发生变化，必须同步更新 `github-homepage-packaging.md`
