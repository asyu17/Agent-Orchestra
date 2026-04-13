# Agent Orchestra CLI 操作与 Agent Handoff 手册

## 1. 一句话结论

当前这份操作手册默认假设 Agent Orchestra 以“后台常驻 daemon backend + thin CLI client”的形态运行：先用 `server start --no-foreground` 把 AO 挂在本机后台，再围绕 `session new/list/inspect/send/events/attach/wake/fork` 做日常操作；`local` 模式只适合隔离测试或兜底调试，不应再视为主操作面。

## 2. 范围与资料来源

- 当前 CLI 入口：
  - `src/agent_orchestra/cli/main.py`
  - `src/agent_orchestra/cli/app.py`
  - `src/agent_orchestra/cli/daemon_app.py`
- 当前 runtime/session 语义：
  - `src/agent_orchestra/runtime/group_runtime.py`
  - `src/agent_orchestra/runtime/session_host.py`
  - `src/agent_orchestra/runtime/orchestrator.py`
  - `src/agent_orchestra/daemon/server.py`
- 当前测试与已验证行为：
  - `tests/test_cli.py`
  - `tests/test_daemon_server.py`
  - `resource/knowledge/agent-orchestra-runtime/resident-team-shell-and-attach-first.md`
  - `resource/knowledge/agent-orchestra-runtime/implementation-status.md`
- 本文中的 CLI 帮助命令已在 2026-04-13 仓库根目录下用 `PYTHONPATH=src python3 -m agent_orchestra.cli.main ... --help` 校对过。

## 3. 当前操作面的真实边界

### 3.1 后台常驻是默认操作方式

当前推荐的默认心智不是：

- 打开一个 CLI 进程，顺便临时跑一轮 AO

而是：

- 先把 daemon 常驻在后台
- 把 session 当成主操作面
- 用 thin CLI client 去 `inspect / send / events / attach / wake`

这意味着日常操作的默认顺序应该是：

1. `server start --no-foreground`
2. `server status`
3. `session new` 或 `session list`
4. `session inspect`
5. `session send` / `session events`
6. 需要恢复时再 `session attach` 或 `session wake`

补充：

- `session` 子命令默认已经走 `--control-plane daemon`
- `session` 子命令默认 `--store-backend postgres`
- `--control-plane local` 只建议用于隔离测试或调试单进程行为

### 3.2 今天可以依赖的能力

- `session new`
  - 新建 `WorkSession + RuntimeGeneration` continuity root。
- `session list`
  - 按 `group_id` / `objective_id` 列出现有 work session。
- `session inspect`
  - 读取 continuity snapshot、`resume_gate`、`continuation_bundles`、`resident_shell_views`。
- `session attach`
  - attach-first 入口；会优先尝试 live resident shell attach，其次才是 recover / warm resume。
- `session wake`
  - honest wake 入口；只在当前确实存在 live attach 或 exact-wake reclaim 时返回成功，不会伪装成 detached daemon。
- `session fork`
  - 基于现有 session lineage 开新分支。
- `session events`
  - 订阅 daemon 对外发布的 session event stream。
- `session send`
  - 向当前 `WorkSession` 追加 durable 用户消息。
- `server start/status/stop`
  - 管理本地 resident daemon lifecycle。
- `schema`
  - 输出 PostgreSQL schema SQL，方便初始化 `postgres` 持久化面。
- `self-host inventory` / `self-host seed-template`
  - 用于从知识包导出 self-hosting gap inventory 与模板，不是 live resident session 操作面。

### 3.3 现在还不能假设的能力

- 不能假设 `session attach` 会把你重新拉回一个完整交互式 TTY/聊天终端。
- 不能假设系统已经有 detached external wake daemon 或 standalone teammate host service。
- 不能假设 `in-memory` backend 可以支撑“离开再回来”的跨进程 continuity；每次 CLI 进程都会重建一份新的内存 store。
- 不能假设 `session events` 当前已经是完整的 runtime-native push bus。
  - 判断：按当前实现，它已经能消费 daemon 跟踪到的 durable session event 与 command-side event，但更深一层的 non-polling runtime-native publication 仍是后续 hardening。
- 不能假设 `group create` / `team create` 已经是完整的 durable provisioning surface。
  - 判断：按当前 `src/agent_orchestra/cli/main.py` 实现，这两个命令仍然只是 thin wrapper，会打印 request-shaped payload，不应视为完整 control-plane API。

## 4. 推荐运行前提

### 4.1 仓库内推荐调用方式

在当前仓库里，最稳妥的调用方式是从 repo root 直接运行：

```bash
export PYTHONPATH=src
python3 -m agent_orchestra.cli.main --help
```

### 4.2 推荐持久化配置

如果你希望 session 能被其他 agent 继续接手，推荐统一使用：

```bash
export PYTHONPATH=src
export AGENT_ORCHESTRA_DSN='postgresql://<user>:<pass>@<host>:<port>/<db>'
```

理由：

- `postgres` 是当前唯一适合跨 CLI 进程保留 continuity / resident shell truth 的正式路径。
- `--dsn` 可以逐次显式传入；如果不传，CLI 会回退到 `AGENT_ORCHESTRA_DSN`。
- schema 名默认是 `agent_orchestra`；如果你需要隔离多套 runtime，可以改 `--schema <name>`。

### 4.3 推荐先拉起 resident daemon

推荐先启动本地 daemon：

```bash
export PYTHONPATH=src
python3 -m agent_orchestra.cli.main server start \
  --no-foreground \
  --store-backend postgres \
  --schema agent_orchestra \
  --output pretty
```

看状态：

```bash
export PYTHONPATH=src
python3 -m agent_orchestra.cli.main server status --output pretty
```

停止：

```bash
export PYTHONPATH=src
python3 -m agent_orchestra.cli.main server stop --output pretty
```

如果你自定义 socket 路径，后续所有 `server` 与 `session` 命令都要带同一个 `--socket-path`。

### 4.4 默认日常模式

后台常驻模式下，推荐把下面这三条当成固定习惯：

- daemon 只启动一次，不要每次开新 shell 就重拉一份
- 平时先跑 `server status`，确认你在连同一个后台实例
- 日常交互都走 `session ...`，而不是频繁切回 `--control-plane local`

如果你要写 shell alias 或脚本，推荐显式写出：

```bash
python3 -m agent_orchestra.cli.main session inspect \
  --control-plane daemon \
  --store-backend postgres \
  ...
```

虽然 `daemon` 和 `postgres` 当前已经是默认值，但脚本里显式写出来更不容易把上下文用错。

### 4.5 推荐 transport 判断

如果你要启动真实 AO runtime/worker，而不是只调用 session CLI：

- 想要更接近 resident shell 体感，优先选 `tmux` 这类 `full_resident_transport`。
- `subprocess` / `codex_cli` 当前更接近 `ephemeral_worker_transport`，适合 bounded worker，不等于长期常驻 shell。

判断：

- 这是 runtime binding 的建议，不是 session CLI 自己的 flag；session CLI 只消费 store 中已经存在的 resident/binding truth。

### 4.6 初始化 PostgreSQL schema

如果目标库还没有 AO schema，可以先导出 SQL：

```bash
export PYTHONPATH=src
python3 -m agent_orchestra.cli.main schema --schema agent_orchestra
```

这条命令会打印完整 DDL；2026-04-11 已验证其会输出 `CREATE SCHEMA` 与所有核心表定义。

## 5. 标准 CLI 流程

这一节默认假设：

- daemon 已通过 `server start --no-foreground` 在后台运行
- 你正在连接同一套 PostgreSQL durable store
- 你把 `session` 当成长期操作面，而不是一次性命令壳

当前最实用的日常循环通常是：

1. `session list`
2. `session inspect`
3. `session send`
4. `session events`
5. 需要恢复或切回 live shell 时再 `session attach` / `session wake`

### 5.1 新建一个可持续 session

```bash
export PYTHONPATH=src
python3 -m agent_orchestra.cli.main session new \
  --store-backend postgres \
  --group-id demo-group \
  --objective-id demo-objective \
  --title "Resident AO demo" \
  --output pretty
```

执行后至少保存这些字段：

- `work_session_id`
- `group_id`
- `objective_id`
- `current_runtime_generation_id`

其中最关键的是 `work_session_id`；后续 `inspect` / `attach` / `wake` / `fork` 都依赖它。

补充：

- `session new` 创建的是 continuity root，不等于自动启动一个完整 orchestration run
- 如果你要继续围绕这个 session 观察或输入，下一步通常是 `inspect` 或 `send`

### 5.2 列出现有 session

```bash
export PYTHONPATH=src
python3 -m agent_orchestra.cli.main session list \
  --store-backend postgres \
  --group-id demo-group \
  --objective-id demo-objective \
  --output pretty
```

适用场景：

- 你忘了具体 `work_session_id`
- 你要在同一个 objective 下挑最近的 session
- 你要把一组 session 交给其他 agent 继续接手

### 5.3 Inspect 当前 continuity 和 resident shell 状态

```bash
export PYTHONPATH=src
python3 -m agent_orchestra.cli.main session inspect \
  --store-backend postgres \
  --work-session-id <work_session_id> \
  --output pretty
```

重点看这些字段：

- `snapshot.work_session`
- `snapshot.runtime_generations`
- `snapshot.resume_gate`
- `snapshot.continuation_bundles`
- `snapshot.resident_shell_views`

如果 `resident_shell_views` 非空，再继续看：

- `attach_recommendation`
- `wake_capability`
- `slot_summary`
- `leader_slot`

这一步通常应该先于 `attach` 或 `wake`，因为它能告诉你当前 session 是 live shell、quiescent shell、还是只剩 continuity fallback。

### 5.4 离开后回来，优先 attach

```bash
export PYTHONPATH=src
python3 -m agent_orchestra.cli.main session attach \
  --store-backend postgres \
  --work-session-id <work_session_id> \
  --output pretty
```

如果你明确不想做 live attach/recover，只想直接开新的 warm resume generation，可以加：

```bash
--force-warm-resume
```

当前推荐心智是：

1. 先 `inspect`
2. 再 `attach`
3. 只有 `attach` 不合适时，才考虑 `wake`、`fork` 或 `new`

### 5.5 attach 不合适时，再显式 wake

```bash
export PYTHONPATH=src
python3 -m agent_orchestra.cli.main session wake \
  --store-backend postgres \
  --work-session-id <work_session_id> \
  --output pretty
```

这条命令的语义要严格按当前实现理解：

- live resident shell 仍在线时，结果是 `attached`
- 能走旧 `exact_wake` reclaim path 时，结果是 `recovered`
- 其余情况会诚实返回 `rejected`

判断：

- `wake` 不应被当成“后台常驻 daemon 唤醒器”的别名；它现在更接近 attach/recover 流程里一个明确的、有限的 control action。

### 5.6 观察 resident session event stream

持续观察：

```bash
export PYTHONPATH=src
python3 -m agent_orchestra.cli.main session events \
  --work-session-id <work_session_id> \
  --output pretty
```

只看前 20 条：

```bash
export PYTHONPATH=src
python3 -m agent_orchestra.cli.main session events \
  --work-session-id <work_session_id> \
  --limit 20 \
  --output pretty
```

当前适合理解成：

- 看 daemon 已跟踪到的 durable session event
- 看 command-side `inspect/send/attach/wake` 等控制面变化
- 看 `slot.restart_queued` 这类 daemon supervision event

不应理解成：

- 完整的 low-latency runtime-native event bus

### 5.7 给当前 session 追加用户消息

```bash
export PYTHONPATH=src
python3 -m agent_orchestra.cli.main session send \
  --work-session-id <work_session_id> \
  --content "continue from latest state and report restart cause" \
  --output pretty
```

这条命令会：

- 追加一条 `WorkSessionMessage`
- 同时写一条 `SessionEvent`
- 让后续 `inspect` / daemon event stream 能看到这次输入

### 5.8 需要分叉或完全新开时

分叉已有 lineage：

```bash
export PYTHONPATH=src
python3 -m agent_orchestra.cli.main session fork \
  --store-backend postgres \
  --work-session-id <work_session_id> \
  --title "fork for follow-up investigation" \
  --output pretty
```

完全新开：

```bash
export PYTHONPATH=src
python3 -m agent_orchestra.cli.main session new \
  --store-backend postgres \
  --group-id demo-group \
  --objective-id demo-objective \
  --title "fresh session" \
  --output pretty
```

选择原则：

- 想继承旧 session lineage，但不要继续污染原 session 时，用 `fork`
- 想开一条完全新的 root session 时，用 `new`

### 5.9 Self-host 辅助命令

看当前知识包 gap inventory：

```bash
export PYTHONPATH=src
python3 -m agent_orchestra.cli.main self-host inventory --knowledge-path resource/knowledge/agent-orchestra-runtime
```

导出 self-hosting objective 模板：

```bash
export PYTHONPATH=src
python3 -m agent_orchestra.cli.main self-host seed-template \
  --output /tmp/ao-self-host-template.json \
  --objective-id ao-self-host-next \
  --group-id ao-self-host \
  --knowledge-path resource/knowledge/agent-orchestra-runtime
```

这两条命令是 bootstrap 辅助面，不代替 session attach/wake/new/fork。

## 6. 结果语义速查

### 6.1 `session attach`

- `attached`
  - 当前已经存在可 attach 的 live resident shell / binding。
- `recovered`
  - attach 流程里触发了 reclaim / exact-wake recover。
- `warm_resumed`
  - 没有 live attach，系统改走 durable continuity fallback，并生成新的 runtime generation。
- `rejected`
  - 当前 attach 不被允许，常见原因是 approval queue 处于 `pending` / `denied`，或当前没有 honest attach path。

### 6.2 `session wake`

- `attached`
  - 当前其实已经 live，不需要额外 wake。
- `recovered`
  - 通过 exact-wake reclaim 成功恢复。
- `rejected`
  - 当前没有 detached wake daemon 或 attach approval 不允许，系统不会伪造成功。

## 7. 给其他 Agent 的最小交接模板

如果你要把当前 AO session 交给另一个 agent，最少给它这组信息：

- 仓库根目录：`/Volumes/disk1/Document/code/Agent-Orchestra`
- 环境：`PYTHONPATH=src`
- socket 路径：默认值或显式 `--socket-path`
- 持久化后端：`postgres`
- `AGENT_ORCHESTRA_DSN` 或显式 `--dsn`
- `group_id`
- `objective_id`
- `work_session_id`
- 当前建议动作：`inspect -> attach` 或 `inspect -> wake`

可以直接把下面这段交给其他 agent：

```text
Use Agent Orchestra from the repo root with `PYTHONPATH=src`.
Treat the current AO CLI as a resident-daemon control plane, not a one-shot foreground runner and not a full interactive shell reattach surface.
Start with:
1. `python3 -m agent_orchestra.cli.main server status --output pretty`
2. `python3 -m agent_orchestra.cli.main session inspect --store-backend postgres --work-session-id <work_session_id> --output pretty`
3. If you need to append operator input, run `session send`.
4. If the shell is live or attachable, run `session attach`.
5. Only if attach is not the right action, run `session wake`.
6. If a clean branch is needed, use `session fork`; if a brand-new root is needed, use `session new`.
Persist and report `work_session_id`, attach result action, any resident shell recommendation fields, and whether the daemon was already running.
```

## 8. 常见误区

- 把 `in-memory` 当成可持续会话后端。
  - 不行；它只适合同一进程里的快速试验。
- 每次交互前都重新 `server start`。
  - 不推荐；后台常驻模式下，正常做法是先 `server status`，确认你正在连现有 daemon。
- 把 `session wake` 当成后台 daemon 唤醒按钮。
  - 现在还不是；它只做 honest attach/recover/reject。
- 把 `session attach` 当成 Claude 那种完整聊天界面回连。
  - 现在还不是；它返回结构化结果，让上层操作者决定下一步。
- 把 `session new` 当成“已经开始跑任务”。
  - 不准确；它首先只是创建 continuity root，后续仍要靠 inspect/send/attach/wake 等控制面动作继续推进。
- 忽略 `inspect` 直接乱用 `wake`。
  - 不推荐；先看 `resident_shell_views`、`attach_recommendation`、`wake_capability` 更稳。
- 只保存 transcript，不保存 `work_session_id`。
  - 不够；其他 agent 真正需要的是 continuity ID，而不只是自然语言上下文。

## 9. 相关文档

- `resident-team-shell-and-attach-first.md`
- `session-continuity-and-runtime-branching.md`
- `implementation-status.md`
- `current-priority-gap-list.md`
