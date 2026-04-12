# Agent-Orchestra

[English](README.md)

<p align="center">
  <img src="assets/agent-orchestra-mascot-4x.png" alt="Agent-Orchestra mascot" width="180" />
</p>

> A usable alpha runtime for multi-team agent orchestration in Python.  
> 一个可用的 alpha 阶段 Python 运行时：面向多团队（multi-team）Agent 编排（orchestration）。

Agent-Orchestra 是一个以 **multi-team / multi-agent orchestration runtime** 为核心的 Python 项目，提供可组合的运行时抽象与 CLI 控制面，用于组织多个 team 在同一 objective 下协作、交接与持续推进。

它受到 Claude Code 风格的 team runtime 思路启发（尤其是 “team + session continuity + host-owned coordination” 的方向），但并非官方关系，也不是简单复刻；目标是在 Python 侧把这些机制抽象为可复用的 runtime 与控制面，方便 infra / framework 工程师研究、验证与迭代。

## 能力亮点（面向 Agent Infra / Framework）

- **包名与 CLI 入口**：项目包名为 `agent-orchestra`，CLI 入口包含 `group`、`team`、`session`、`schema`、`self-host`。
- **多 team 编排运行时（Group/Team/Session）**：以 group 为上层容器组织 objective 与执行单元，支持跨 team 的协作与交接语义落地为结构化对象，而不是仅靠日志或约定。
- **Session control plane（连续性优先）**：通过 `session new/list/inspect/attach/wake/fork` 管理 work session，在持续迭代、跨人/跨进程接力时更易保持上下文与状态边界。
- **Store backend 可切换**：CLI 可见 `--store-backend {in-memory,postgres}`，在“快速试跑”和“需要跨进程 continuity”之间做清晰取舍。
- **可扩展的执行/transport backend**：运行时 builder 中已注册 `in_process`、`subprocess`、`tmux`、`codex_cli`，便于把“如何启动/承载 worker”从上层编排逻辑中解耦。
- **Self-hosting bootstrap 辅助面**：`self-host inventory` / `self-host seed-template` 用于从知识库导出 gap 清单与模板，支持把“下一步要做什么”固化为可被 runtime 消费的结构化输入。

## 最短上手

### 安装（本地开发）

```bash
python -m pip install -e .
```

### CLI 概览

本项目的包名是 `agent-orchestra`（见 `pyproject.toml`），并提供 CLI：

```bash
agent-orchestra {group,team,session,schema,self-host} --help
```

在仓库内更稳妥的调用方式是：

```bash
export PYTHONPATH=src
python -m agent_orchestra.cli.main --help
```

### 1) 先用 `in-memory` 跑通一个最小 session 流程

```bash
export PYTHONPATH=src

python -m agent_orchestra.cli.main session new \
  --store-backend in-memory \
  --group-id demo-group \
  --objective-id demo-objective \
  --title "AO demo" \
  --output pretty

python -m agent_orchestra.cli.main session list \
  --store-backend in-memory \
  --group-id demo-group \
  --objective-id demo-objective \
  --output pretty

python -m agent_orchestra.cli.main session inspect \
  --store-backend in-memory \
  --work-session-id <work_session_id> \
  --output pretty
```

### 2) continuity 操作：attach / wake / fork

Session 子命令当前包括：

```bash
agent-orchestra session {list,inspect,new,attach,wake,fork} --help
```

典型流程（建议先 inspect 再 attach）：

```bash
export PYTHONPATH=src

python -m agent_orchestra.cli.main session attach \
  --store-backend in-memory \
  --work-session-id <work_session_id> \
  --output pretty

python -m agent_orchestra.cli.main session wake \
  --store-backend in-memory \
  --work-session-id <work_session_id> \
  --output pretty

python -m agent_orchestra.cli.main session fork \
  --store-backend in-memory \
  --work-session-id <work_session_id> \
  --title "fork for follow-up" \
  --output pretty
```

说明（避免误解）：

- `in-memory` 更适合单进程/单次 CLI 试跑；它不应被当成跨进程 continuity 的持久化方案。
- 当前 `attach/wake` 返回的是结构化控制面结果；不要假设它等价于“回到一个完整交互式 TTY/聊天 shell”。

如果你要验证更持久的 continuity，再切换到 `--store-backend postgres`，先用 `agent-orchestra schema --schema agent_orchestra` 初始化 schema，并通过 `AGENT_ORCHESTRA_DSN` 或 `--dsn` 提供连接串。

### 3) Self-hosting 辅助命令

```bash
agent-orchestra self-host {inventory,seed-template} --help
```

## 为什么值得研究

如果你在做 agent infra / framework，这个仓库提供了一组相对“可落地”的切面，用来讨论并验证多团队编排的工程问题：

- 把 **协作与交接（handoff）** 从“提示词约定”推进到 **结构化契约与运行时状态**，便于审计、回放与自动化工具接入。
- 明确区分 **control plane（session/store/inspect/attach）** 与 **data plane（worker execution / protocol / artifacts）**，减少“跑得起来但无法接手”的黑盒风险。
- 用可切换的 backend 把“执行载体/transport”隔离成边界清晰的扩展点，方便在 `subprocess/tmux/codex_cli` 等路径上做能力对比与渐进增强。
- 把“下一轮要补齐的缺口”通过 self-hosting 输出成可消费的模板与 inventory，有利于多人协作时的任务收敛与进度追踪。

## 架构概览（高层）

以下是面向阅读源码的最小心智模型：

- **CLI 层**：`agent_orchestra.cli` 提供控制面入口（group/team/session/schema/self-host），主要负责参数解析与输出格式。
- **Runtime 层**：`agent_orchestra.runtime` 承载编排主语义（group/team/leader/superleader 等运行时对象），并通过可插拔 backend 触发实际执行。
- **Store / Bus 层**：`agent_orchestra.storage` / `agent_orchestra.bus` 提供持久化与协作通道的实现切面；当前 CLI 明确支持 `in-memory` 与 `postgres` 两类 store backend。

执行 backend（运行时 builder 已注册）：

- `in_process`：同进程内执行，便于快速验证与测试。
- `subprocess`：子进程执行，隔离运行环境。
- `tmux`：更贴近“常驻 shell / 复连”的形态（具体能力边界仍以实现为准）。
- `codex_cli`：面向特定 CLI 适配的 backend（用于把外部工具链纳入统一运行时边界）。

更细的架构与边界说明建议直接进入知识库（见下方链接）。

## 当前状态（usable alpha）

已经具备：

- `agent-orchestra` CLI 与基本控制面命令集合：`group/team/session/schema/self-host`。
- `session` 的核心子命令：`list/inspect/new/attach/wake/fork`，并对 `--store-backend {in-memory,postgres}` 做显式选择。
- self-hosting 辅助能力：`inventory/seed-template`，用于导出 gap 与模板。
- runtime backend 的注册与边界化：`in_process/subprocess/tmux/codex_cli`。

仍处于 alpha 的部分（不应过度承诺）：

- attach/wake 的体验与语义仍以“控制面动作 + 结构化结果”为主，不应默认等价于“完整交互式会话重连”。
- 持久化、跨进程、长期常驻等能力的完整性与鲁棒性仍在演进中；请以 `docs/resource/knowledge/agent-orchestra-runtime/` 的架构与实现状态文档为准。

## 仓库结构（入口地图）

```text
Agent-Orchestra/
  src/agent_orchestra/            # 核心 Python 包（runtime/cli/storage/self_hosting 等）
  docs/resource/knowledge/        # 可复用知识库（架构、运行手册、契约与失败模式）
  tests/                          # 单元/集成测试（覆盖范围以实际为准）
  pyproject.toml                  # 包名/CLI entrypoint/extras
```

## 文档与知识库入口

本仓库把可复用运行时知识沉淀在 `docs/resource/knowledge/`，建议按顺序阅读：

- 知识库总入口：[docs/resource/knowledge/README.md](docs/resource/knowledge/README.md)
- 本项目运行时主包（架构最完整的一组）：[docs/resource/knowledge/agent-orchestra-runtime/README.md](docs/resource/knowledge/agent-orchestra-runtime/README.md)
- CLI 操作与 session handoff 手册：[docs/resource/knowledge/agent-orchestra-runtime/agent-orchestra-cli-operations-and-agent-handoff.md](docs/resource/knowledge/agent-orchestra-runtime/agent-orchestra-cli-operations-and-agent-handoff.md)
- 架构细化文档：[docs/resource/knowledge/agent-orchestra-runtime/architecture.md](docs/resource/knowledge/agent-orchestra-runtime/architecture.md)

## English README

[README.md](README.md)
