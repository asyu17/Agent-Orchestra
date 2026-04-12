# agent_orchestra 运行脆弱性分层与硬化路线

## 1. 一句话结论

当前 AO “容易断”已经不应再理解成一个单点 bug；从现有代码、知识与运行产物看，旧的 execution guard 假失败主因已经基本退出主矛盾，真正残留的脆弱性主要来自 4 层叠加：`codex_cli` 作为 `ephemeral_worker_transport` 承担了本不适合它的常驻协作角色、provider/plugin-sync/network 暂时性失败频繁打断 worker、`WorkSession=open` 与 live process/attach truth 在产品面上仍容易被误读成“还在跑”、以及 host-owned resident runtime 还没有完全收口成更彻底的 single-owner long-lived operating surface。判断：如果目标是“彻底解决”而不是“降低一点失败率”，默认运行面必须从“bounded `codex_cli` worker orchestration”切到“full-resident coordinator transport + sticky provider routing + honest run/session finalization + deeper resident reconnect hardening”；单独继续加 timeout、加 generic retry、或继续让 `codex_cli` 承担长期 leader/superleader 主执行面，都只能缓解，不会根治。

## 2. 范围与资料来源

- 知识文档：
  - `resource/knowledge/agent-orchestra-runtime/implementation-status.md`
  - `resource/knowledge/agent-orchestra-runtime/current-priority-gap-list.md`
  - `resource/knowledge/agent-orchestra-runtime/remaining-work-backlog.md`
  - `resource/knowledge/agent-orchestra-runtime/worker-lifecycle-protocol-and-lease.md`
  - `resource/knowledge/agent-orchestra-runtime/execution-guard-failure-patterns.md`
  - `resource/knowledge/agent-orchestra-runtime/session-domain-and-durable-persistence.md`
  - `resource/knowledge/agent-orchestra-runtime/resident-team-shell-and-attach-first.md`
  - `resource/knowledge/agent-orchestra-runtime/agent-orchestra-cli-operations-and-agent-handoff.md`
- 关键代码：
  - `src/agent_orchestra/runtime/backends/codex_cli_backend.py`
  - `src/agent_orchestra/runtime/backends/tmux_backend.py`
  - `src/agent_orchestra/contracts/execution.py`
  - `src/agent_orchestra/runtime/worker_supervisor.py`
  - `src/agent_orchestra/contracts/session_continuity.py`
  - `src/agent_orchestra/cli/app.py`
- 运行产物：
  - `docs/superpowers/runs/2026-04-09-online-collaboration-first-batch-round-2.summary.json`
  - `docs/superpowers/runs/2026-04-09-online-collaboration-first-batch-round-2-custom-gap-replay-after-guard-fix.summary.json`
  - `docs/superpowers/runs/2026-04-10-superleader-isomorphic-runtime-replay-2026-04-10T03-47-35Z.summary.json`
  - `docs/superpowers/runs/codex-spool/task_4da77b442d06:leader-turn-1.result.json`
  - `docs/superpowers/runs/codex-spool/task_ac96c894e0c1:teammate-turn-1.result.json`
- 运行产物扫描：
  - 2026-04-12 对 `docs/superpowers/runs/codex-spool/*.stderr.log` 与 `*.result.json` 的本地扫描显示，包含 `authentication required to sync remote plugins` 的文件有 32 个，包含 `403 Forbidden` 或 `plugins/featured` 的文件有 32 个，包含 `stream disconnected - retrying sampling request` 的文件有 9 个，带 `provider_unavailable` 标记的文件有 29 个。

下面出现的“判断”是基于这些代码、知识与运行产物做出的架构推断，不表示仓库当前已经完全实现对应目标状态。

## 3. 当前脆弱性到底来自哪里

### 3.1 transport 选型与运行面错配

当前最直接的问题，不是 AO 没有 session/reconnect，而是默认运行面经常仍建立在 `codex_cli` 上，而 `codex_cli` 在代码里被明确声明为：

- `transport_class = EPHEMERAL_WORKER_TRANSPORT`
- `supports_reactivate = False`
- `supports_reattach = True`

这意味着它适合：

- bounded worker
- 可回收、可重连的单次执行面

但它并不等价于：

- full resident shell
- 长时间在线的 coordinator transport
- 用户离开再回来时“像常驻 team 一样”稳定可 attach 的主 operating surface

与之相对，`tmux` 与 `in_process` 才被声明为 `FULL_RESIDENT_TRANSPORT`。

判断：

- 只要长期协作主路径还主要压在 `codex_cli` 上，AO 就会持续表现出“continuity 还在，但真实执行面很脆”的体感。
- 这不是 `codex_cli` 做错了，而是 transport class 与产品期待之间存在结构性错配。

### 3.2 provider / network / plugin-sync 波动会把 bounded worker 直接打断

现有产物里反复出现的真实错误形状非常集中：

- `chatgpt authentication required to sync remote plugins`
- `remote plugin sync request ... failed with status 403 Forbidden`
- `stream disconnected - retrying sampling request`
- `termination_signal_name = SIGTERM`
- `last_message_exists_at_failure = false`

当前 runtime 已经把这类失败显式归类成 `provider_unavailable`，并增加了专属指数退避；但这些机制的本质仍是：

- 识别后重试
- 减少过快重打
- 允许同路由有限次恢复

它们并没有改变一个更基础的事实：

- 当长期运行的 lane/coordinator 本身就是 bounded `codex_cli` worker 时，provider 短波动就会直接把主协作面打断。

判断：

- 这条线已经从“系统不知道为什么失败”进化到“系统能识别并有限恢复”，但还没有进化到“系统即使遭遇 provider 波动，也不会把主协作面打断”。

### 3.3 continuity truth 与 live truth 在产品面仍容易混淆

当前 `WorkSession.status` 默认是 `open`，这代表 continuity root 仍存在，不代表：

- 还有 live AO 进程
- 还有 live resident shell
- 当前 `attach` 一定能回到完整交互执行面

CLI 知识与代码都已经说明：

- `session attach / wake` 当前是 control plane
- 不是 Claude 式完整交互 shell reentry
- `wake` 采用 honest semantics：能 attach 就 `attached`，能 recover 就 `recovered`，否则 `rejected`

判断：

- 用户感知中的“断”，有一部分不是执行面中断，而是 continuity root 仍显示 open，live process 却早已结束。
- 如果产品面继续让 `session open`、`live resident shell`、`runtime generation still active` 这三层状态混在一起，AO 即使底层更稳，体感仍会像“经常断”。

### 3.4 host-owned resident runtime 还没有完全收口到长期 operating surface

当前知识库已经把真正剩余 backlog 收缩到：

- `online-collaboration-runtime` residual hardening
- `sticky-provider-routing`
- `planner-feedback`
- `permission-broker`
- `PostgreSQL persistence`
- 更深一层的 long-lived resident worker / reconnect hardening

这说明系统已经不再缺少：

- protocol-native wait
- active reattach
- protocol bus
- session continuity first-cut
- resident shell first-cut

但仍缺更深一层：

- standalone teammate host / detached external wake loop
- broader single-owner session truth
- long-lived resident worker mainline
- host-owned evidence gate cleanup

判断：

- 当前系统并不是“没有 resident runtime”，而是“已经有 first-cut resident runtime，但还没硬化到足以把 bounded worker orchestration 退成次要路径”。

### 3.5 旧 execution guard 假失败已不再是主矛盾

历史上确实存在 raw result 已 completed、但 authoritative worker 被 guard 改写成 failed 的情况；不过这条线的主修复已经进入代码与 replay 证据：

- 2026-04-09 原始 round-2 中有 3 条 lane blocked
- guard fix 之后的 custom replay 里，这 3 条 lane 已转成 completed
- 剩余 blocker 转成 `superleader-isomorphic-runtime` 相关的真实 transport/provider failure

判断：

- 后续若继续把“AO 容易断”主要理解成 old guard false failure，会把修复重心放错。
- 当前主矛盾已经从“判错”切换成“真实 transport/provider 脆弱性 + 产品面状态语义混淆”。

## 4. 什么才算“彻底解决”

“彻底解决”不应被定义成：

- 把 timeout 再拉长一点
- 让 generic retry 次数再多一点
- 继续复用 `codex_cli`，但把 failure classifier 写得更复杂
- 让 `session list` 更像“永远在线”

更合理的目标状态应该同时满足下面 5 条：

1. 长期协作主路径使用 full-resident transport，而不是把 bounded worker transport 伪装成常驻 shell。
2. live process truth、resident shell truth、continuity truth、artifact/finalization truth 分层明确，且对外语义诚实。
3. provider 临时波动只会让 lane 进入可恢复的 degraded / waiting-provider 状态，而不是频繁把主协作面直接打断。
4. 当一轮 run 真实结束时，session/control-plane 会明确呈现“continuity still open, run ended”而不是模糊的 open 态。
5. 默认 attach/wake 进入的是 host-owned resident surface，而不是“再赌一次 bounded worker 能不能正好活着”。

## 5. 彻底治理的推荐路线

### 5.1 第一优先级：把 coordinator 主运行面迁出 `codex_cli`

如果要根治脆弱性，最先该切的不是 classifier，而是默认 transport 策略：

- `superleader`
  - 默认只使用 `FULL_RESIDENT_TRANSPORT`
- `leader`
  - 默认优先 `FULL_RESIDENT_TRANSPORT`
- `teammate`
  - 允许继续使用 bounded transport，但应明确属于 subordinate execution path

最现实的第一步是：

- 把 `tmux` 提升为默认 coordinator transport
- 让 `codex_cli` 退回成 bounded execution worker 或 provider fallback route

判断：

- 只要 leader/superleader 仍主要压在 `codex_cli` 上，AO 的“主脑”就仍暴露在 provider/plugin-sync/network 短波动下。
- 把 `tmux` 或其他 full-resident transport 提升成默认 coordinator surface，是“根治”而不是“缓解”的第一刀。

### 5.2 第二优先级：把 live truth 与 continuity truth 在控制面上彻底拆开

当前 `WorkSession=open` 容易被误读成“run 还在”。更稳的产品面应该显式区分：

- `continuity_status`
  - 会话 root 是否仍可继续
- `resident_shell_status`
  - 当前是否有 live attachable shell
- `runtime_generation_status`
  - 当前这代 runtime 是 `booting / active / quiescent / closed`
- `run_terminal_status`
  - 最近一次真实执行是 `completed / failed / aborted / provider_unavailable_exhausted`

建议的硬规则：

- `session list`
  - 不再只显示 `open/closed`
  - 必须同时显示 live shell / active runtime / last terminal run
- `session inspect`
  - 直接导出“是否仍有 live process / resident shell”
- `wake/attach`
  - 不再让用户自己推断 open 是否代表能接回

判断：

- 这条线不能减少真实 worker failure，但能把“已经结束”与“执行中断”分开，显著降低“AO 看起来总在莫名其妙断”的认知噪音。

### 5.3 第三优先级：把 `provider_unavailable` 从“有限重试”升级成“稳定路由系统”

当前已有的：

- transient failure classifier
- 同路由有限重试
- provider-unavailable 专属指数退避

真正还缺的是：

- sticky provider routing
- provider health memory
- route quarantine / cooldown
- cross-run provider score

推荐硬化方向：

- 给每条 route 记录：
  - 最近失败类型
  - 最近失败时间
  - 连续 `provider_unavailable` 次数
  - cooldown 截止时间
- 对 coordinator 角色启用：
  - failing route quarantine
  - sticky healthy route preference
- 把 `provider_unavailable` exhaustion 的终态从普通 `failed` 分离出来
  - 例如 lane/status 上显式进入 `waiting_provider` 或 `degraded`

判断：

- 当前 AO 已经会“看到 provider 波动”，下一步必须让 runtime“记住 provider 波动”，否则每轮仍会用同样脆弱的默认 route 重新踩坑。

### 5.4 第四优先级：把 resident runtime 从 first-cut 推到 detached operating surface

当前 resident shell / host-owned runtime 已经存在，但要真正摆脱“像 bounded orchestration”：

- 需要 detached external wake loop
- 需要 standalone teammate host
- 需要 leader session runtime 可重入、可恢复、可跨 cycle step

也就是说，目标不是继续让 leader 每轮“再启动一次 bounded step”，而是：

- leader / teammate 本身就是长期活着的 resident actor
- bounded worker 只用于真正的执行切片

判断：

- 如果这一层不补齐，AO 永远会处在“底座已经想做 resident runtime，但主运行面仍像 bounded batch orchestration”的中间态，这正是脆弱性的主要来源之一。

### 5.5 第五优先级：把 provider 故障从 objective failure 里语义拆出

当前 provider 抖动经常最终表现成：

- worker failed
- lane blocked
- objective blocked

这在 runtime 收口阶段可以接受，但不适合作为长期产品语义。

更合理的长期状态机应该显式区分：

- 业务/实现失败
- authority 阻断
- transport/provider 暂时不可用
- shell 已结束但 continuity 仍可继续

建议：

- lane / objective evaluator 对 `provider_unavailable` exhaustion 进入独立 degraded family
- 只有当 provider 问题经过 route switch / cooldown / retry budget 仍无法恢复时，才把它提升成真正的 blocked terminal reason

判断：

- 这一步不是“掩盖失败”，而是把“系统当前做不了工作，因为 provider 临时不可用”与“当前任务逻辑无法继续”分开。

## 6. 推荐实施顺序

### 6.1 必做项

1. 让 `superleader` 与 `leader` 默认只走 `FULL_RESIDENT_TRANSPORT`
2. 让 `session list/inspect` 显式展示 live shell / active runtime / last run terminal status
3. 实现 sticky provider health routing 与 route quarantine
4. 把 `provider_unavailable` 从普通 blocked/failure 里独立出一层 degraded 语义

### 6.2 高价值后续项

1. standalone teammate host
2. detached external wake loop
3. leader session runtime 的可重入 / 可恢复 step
4. broader single-owner `WorkerSession / AgentSession / ResidentCoordinatorSession` 收口

### 6.3 不足以根治的做法

下面这些可以缓解，但不应被当成“彻底解决”：

1. 继续只加 timeout
2. 继续只加 generic retry
3. 继续把 `codex_cli` 作为默认常驻 coordinator transport
4. 继续只用 `WorkSession.status=open` 代表“还没结束”

## 7. 给操作者的简化判断

如果当前目标不是立刻重构全部 runtime，而是先把线上体感明显拉稳，最小可执行组合是：

1. coordinator 默认 transport 切到 `tmux`
2. `codex_cli` 退到 bounded subordinate execution path
3. `session list/inspect` 加 live shell / active runtime / terminal run 分栏
4. provider route 引入 cooldown 和 sticky healthy route

判断：

- 这 4 项做完之后，AO 不一定已经达到“理想常驻 agent runtime”，但“经常断”的体感会先下降一个数量级。
- 再往后补 detached host、leader reentrant runtime 和 broader single-owner truth，才是把系统真正推到更像 Claude team agent 的长期 operating surface。

## 8. 相关文档

- `resource/knowledge/agent-orchestra-runtime/worker-lifecycle-protocol-and-lease.md`
- `resource/knowledge/agent-orchestra-runtime/session-domain-and-durable-persistence.md`
- `resource/knowledge/agent-orchestra-runtime/resident-team-shell-and-attach-first.md`
- `resource/knowledge/agent-orchestra-runtime/implementation-status.md`
- `resource/knowledge/agent-orchestra-runtime/current-priority-gap-list.md`
- `resource/knowledge/agent-orchestra-runtime/remaining-work-backlog.md`
- `resource/knowledge/agent-orchestra-runtime/execution-guard-failure-patterns.md`
