# agent_orchestra 常驻 daemon backend 与 session attach 主形态

## 1. 一句话结论

如果 AO 要从“容易断的前台 orchestration run”升级成真正稳定的长期在线协作系统，正确主线不是继续给 bounded run 打补丁，而是把系统改成“常驻本地 daemon backend + thin CLI client + session attach + slot/incarnation supervision”这套形态：daemon 成为 live runtime owner，session 成为用户主操作面，稳定 `AgentSlot` 跨越单个 incarnation 失败持续存在，异常失败由 daemon 监督并替换新 incarnation，而不是让 CLI 进程或单次 `codex_cli` worker 承担主真相。

## 2. 范围与资料来源

- 设计文档：
  - `docs/superpowers/specs/2026-04-12-agent-orchestra-resident-daemon-backend-and-session-attach-design.md`
- 相关知识：
  - `resource/knowledge/agent-orchestra-runtime/session-domain-and-durable-persistence.md`
  - `resource/knowledge/agent-orchestra-runtime/resident-team-shell-and-attach-first.md`
  - `resource/knowledge/agent-orchestra-runtime/active-reattach-and-protocol-bus.md`
  - `resource/knowledge/agent-orchestra-runtime/runtime-fragility-decomposition-and-hardening-roadmap.md`
- 当前代码入口：
  - `src/agent_orchestra/runtime/group_runtime.py`
  - `src/agent_orchestra/runtime/session_domain.py`
  - `src/agent_orchestra/runtime/session_host.py`
  - `src/agent_orchestra/runtime/worker_supervisor.py`
  - `src/agent_orchestra/contracts/daemon.py`
  - `src/agent_orchestra/daemon/server.py`
  - `src/agent_orchestra/daemon/client.py`
  - `src/agent_orchestra/daemon/slot_manager.py`
  - `src/agent_orchestra/daemon/supervisor.py`
  - `src/agent_orchestra/runtime/backends/codex_cli_backend.py`
  - `src/agent_orchestra/runtime/backends/tmux_backend.py`
  - `src/agent_orchestra/cli/main.py`
  - `src/agent_orchestra/cli/app.py`
  - `src/agent_orchestra/cli/daemon_app.py`
  - `tests/test_daemon_server.py`
  - `tests/test_cli.py`
  - `tests/test_slot_supervisor.py`

下面出现的“判断”是基于上述代码与设计的架构推断；但从 2026-04-12 这轮实现开始，仓库里已经不再只是纯设计文档，daemon/backend 主线已有 first-cut 落地。

## 3. 为什么这条线比“只做 watchdog”更正确

单独做 watchdog/restart 只能回答：

- 某个 worker 死了怎么办

但它回答不了：

- CLI 退出后谁继续拥有 live runtime truth
- 用户回来 attach 的到底是什么
- session 为什么仍然 open，但实际 run 已经结束
- 一个 agent 重启后如何保持稳定身份而不是像新 run

因此长期正确形态必须更上移一层：

- runtime owner 从前台 CLI 移到常驻 daemon
- 用户心智从“启动一轮 run”改成“连接某个 session”
- 重启对象从“process”升级成“slot 的 incarnation”

## 4. 核心对象

### 4.1 daemon

常驻后端，拥有：

- live session registry
- resident shell registry
- slot supervision
- attach/event stream
- provider health memory

### 4.2 session

用户主操作面，回答：

- 这条工作会话是什么
- 现在有没有 live shell
- 当前 attach 到哪里

### 4.3 `AgentSlot`

稳定角色身份，例如：

- `superleader:<objective>`
- `leader:<lane>`
- `teammate:<team>:slot:<n>`

slot 是长期存在的；单个失败不会改变 slot 身份。

### 4.4 `AgentIncarnation`

某个 slot 当前或历史上的具体运行实例。

异常失败时，daemon 替换的是 incarnation，不是 slot。

## 5. 关键设计判断

### 5.1 CLI 必须退成 thin client

CLI 只负责：

- 发命令
- attach 流
- 发输入
- 看状态

CLI 不再拥有长生命周期 runtime truth。

### 5.2 session attach 必须成为主交互面

系统默认心智要从：

- run 一轮 AO

切到：

- attach 到 AO backend 上的某个 session

### 5.3 异常重启必须是 slot-aware

正确语义不是：

- 某个进程挂了就起个新进程

而是：

- 某个 slot 的当前 incarnation 异常终止，由 daemon 用同一 slot identity 拉起 replacement，并用 host-owned truth 恢复

### 5.4 fencing 必须成为硬约束

只要有自动替换 incarnation，就必须有：

- `incarnation_id`
- `lease_id`

否则旧进程的迟到写入会污染新状态。

## 6. 推荐落地顺序

1. 引入 daemon 进程与本地 IPC
2. 把 CLI `session` 命令改成 daemon client
3. 引入 `AgentSlot` / `AgentIncarnation`
4. 实现 slot supervision 与异常失败替换
5. 把 attach 做成 live stream，而不是静态 inspect
6. 再补 sticky provider routing、approval broker、planner feedback

## 7. 当前实现状态

截至 2026-04-12，这条线已经进入代码主线的 first-cut 状态，而不是停留在设计层：

- daemon control plane 已存在：
  - `DaemonServer` / `DaemonClient` / `protocol.py` / `registry.py` / `event_stream.py` 已实现 Unix socket IPC、request/response，以及 `session.events` 订阅流
- CLI 已切成 thin client：
  - `server start/status/stop`
  - `session new/list/inspect/attach/wake/fork/events/send`
  - `session` 默认走 `--control-plane daemon`
  - `server start --no-foreground` 已支持后台常驻启动
- durable daemon objects 已存在：
  - `AgentSlot`
  - `AgentIncarnation`
  - `SlotHealthEvent`
  - `SessionAttachment`
  - `ProviderRouteHealth`
- slot supervision 已进入主线：
  - `SlotManager` 负责稳定 slot identity 与 first incarnation materialization
  - `SlotSupervisor` 负责 terminal record classification、stale-incarnation fencing、abnormal replacement
- daemon 已拥有 background supervision first-cut：
  - 常驻 loop 会 materialize active worker sessions 成 slot/incarnation
  - terminal worker records 会被 daemon 读取并送入 `SlotSupervisor`
  - `recoverable_abnormal` 会生成 replacement incarnation，并尝试用现有 runtime/session truth 触发 restart
  - `event_stream.py` 现在还带有小型 replay buffer，`slot.restart_queued` 这类 ephemeral daemon event 不会再因为 `session.events` 订阅建立竞态直接丢失
- runtime truth 已开始显式透传 slot/incarnation 元数据：
  - `WorkerSession` / `WorkerRecord` 现在已有 `slot_id / incarnation_id / slot_lease_id / incarnation_status`
  - `DefaultWorkerSupervisor` 会在 active session persist、snapshot 和 host projection 时保留这些字段，并继续把 `work_session_id / runtime_generation_id` 跟着 worker-session durable truth 一起写下去
  - `SessionDomainService.exact_wake(...)` 也会把 recovered `slot_ids / incarnation_ids` 暴露给控制面
- session 交互面已不再只有 inspect/attach：
  - `session.send` 现在会写入 `WorkSessionMessage` 与 `SessionEvent`
  - daemon event stream 不再只是单次 RPC 回包，而会后台 relay 已知 work session 的 durable session events
  - `session.inspect` 与 `session.attach / wake / exact_wake` 现在还会显式导出 `provider_route_health`
- provider health memory 已进入 worker-supervisor 主线：
  - `DefaultWorkerSupervisor` 现在会在每轮 assignment finalize 后持久化 `ProviderRouteHealth`
  - 路由健康记录使用稳定 `role:route_id` key，并带上 `work_session_id / objective_id / provider_route_id`
  - `provider_unavailable` exhaustion 会把路由写成 `quarantined + cooldown_expires_at`
  - 最终成功路由会被写成 `healthy + preferred`

判断：

- 这仍然是 first-cut resident backend，而不是最终 fully-hardened 形态。
- 真正更强的下一层仍然包括 sticky route selection、更加正式的 restart policy、以及不靠 polling 的 runtime-native event publication；但 `ProviderRouteHealth` 的 durable memory 与 attach/read-model 暴露已不再只是设计缺口。

## 8. 对当前脆弱性的意义

这条线可以同时解决当前几类问题：

- CLI/终端退出导致 runtime owner 消失
- `WorkSession=open` 被误读成“run 还活着”
- `codex_cli` 这种 bounded worker 被过度当成主运行面
- agent 异常失败后只能靠下一次人工 attach/wake 触发恢复

判断：

- 这比单纯继续加 timeout/retry 更接近根治。
- 这也比只做 watchdog 更完整，因为它把 supervision、session、attach、runtime owner 一并统一起来了。

## 9. 相关文档

- `docs/superpowers/specs/2026-04-12-agent-orchestra-resident-daemon-backend-and-session-attach-design.md`
- `resource/knowledge/agent-orchestra-runtime/session-domain-and-durable-persistence.md`
- `resource/knowledge/agent-orchestra-runtime/resident-team-shell-and-attach-first.md`
- `resource/knowledge/agent-orchestra-runtime/active-reattach-and-protocol-bus.md`
- `resource/knowledge/agent-orchestra-runtime/runtime-fragility-decomposition-and-hardening-roadmap.md`
