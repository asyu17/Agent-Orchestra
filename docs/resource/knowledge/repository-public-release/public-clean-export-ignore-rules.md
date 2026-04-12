# Agent-Orchestra 公开版导出忽略规则

## 1. 一句话结论

仓库根目录 `.ignores` 是 Agent-Orchestra 准备“公开版 / 干净版本”时的路径排除清单，采用 gitignore 风格；当前规则把 `docs/superpowers/` 作为内部协作与运行痕迹整体排除，同时保留 `docs/resource/knowledge`、`src/`、`tests/`、`pyproject.toml` 和 `uv.lock` 这类可公开复用内容。

## 2. 范围与资料来源

- 规则文件：`/.ignores`
- 目录结构扫描：仓库根目录 `ls -la` 与 `find . -maxdepth 3`
- 仓库约束：`/AGENTS.md`
- 相关知识：`resource/knowledge/agent-orchestra-runtime/execution-guard-failure-patterns.md`

## 3. 当前公开边界

### 3.1 应保留的内容

- 源码与测试：`src/`、`tests/`
- 构建与依赖声明：`pyproject.toml`、`uv.lock`
- 公开知识与说明文档：`docs/resource/knowledge/`
- `resource -> docs/resource` 符号链接本身，因为它只是公开知识目录的仓库内别名

### 3.2 应排除的内容

- `docs/superpowers/`
  - 这里包含内部协作 spec、plan、run artifacts 与 agent 执行痕迹；当前公开边界按整棵目录剔除
- `.history/`
  - 本地历史快照目录，不属于公开仓库语义
- `.learnings/`
  - agent 本地学习与运行侧产物，不属于公开仓库语义
- `test.html`
  - 本地浏览器预览 / 视觉草图残留，不属于公开仓库语义
- `.venv/`、`venv/`
  - 本地 Python 虚拟环境
- `.pytest_cache/`、`__pycache__/`、`*.pyc`、`*.pyo`
  - 解释器与测试缓存
- `*.egg-info/`、`build/`、`dist/`、`pip-wheel-metadata/`
  - 打包产物与构建元数据
- `.DS_Store`、`.idea/`、`.vscode/`、`*.swp`、`*.swo`
  - 系统与编辑器噪音
- `failed`
  - 本地运行残留标记文件

## 4. 规则设计判断

### 4.1 为什么整个 `docs/superpowers/` 直接排除

这是当前操作者明确给出的公开边界：`docs/superpowers/` 下内容统一视为内部协作痕迹，而不是对外文档。这样可以避免把 run artifact、计划文档、内部 spec 和过程记录一并带入公开版。

### 4.2 为什么 `docs/resource/knowledge/` 继续保留

`resource/knowledge/` 是仓库级可复用知识入口，也是 `AGENTS.md` 要求的工作上下文。它描述的是稳定的架构认知、运行规则和 failure pattern，而不是一次性的执行痕迹，因此应保留在公开版中。

### 4.3 `.ignores` 的语义边界

`.ignores` 只是仓库内保存的一份 gitignore 风格规则文件，方便后续导出脚本、同步脚本或打包流程复用；它本身不会像 `.gitignore` 那样自动被 Git 作为默认忽略规则加载。后续如果要把这些规则接入自动化流程，应显式让导出工具读取 `.ignores`。

### 4.4 准备独立 GitHub 仓库时应同步 `.gitignore`

如果当前目录要从“嵌在别的仓库里的工作目录”转成独立 GitHub 仓库，应把 `.ignores` 的公开边界同步到根目录 `.gitignore`，否则 Git 不会自动忽略这些本地缓存、内部协作痕迹和预览残留。

## 5. 维护清单

1. 新增内部运行或协作目录时，先判断它是否应进入公开版；如果不应公开，补进 `.ignores`
2. 新增本地缓存、构建目录或编辑器目录时，优先补充对应模式，而不是在导出时临时手工排除
3. 如果当前目录要作为独立 GitHub 仓库初始化，记得同步维护 `.gitignore`
4. 如果后续 `docs/` 目录重新分层，必须同步重审 `docs/superpowers/` 与 `docs/resource/knowledge/` 的公开边界

## 6. 相关文档

- `README.md`
- `resource/knowledge/README.md`
- `resource/knowledge/knowledge-solidification-standard.md`
