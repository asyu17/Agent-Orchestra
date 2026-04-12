# agent_orchestra ACTIVE reattach 与 protocol bus 主线

## 1. 一句话结论

这条主线在 2026 年 4 月 5 日已经落地：`ACTIVE` worker 现在会在进入 wait 前写成 durable session，协议事件推进时会续写 lease/cursor，lease 过期后新的 supervisor 可以走 `reclaim -> backend.reattach(...) -> protocol wait -> finalize` 接回仍在运行中的本机 worker；同时 Redis 已具备 `lifecycle/session/control/takeover/mailbox` 五类 stream 的 protocol-bus 路径，self-hosting 也已经用 reattach/takeover/stream evidence 来决定 gap 是否关闭。

## 2. 范围与资料来源

- 现状与缺口：
  - [implementation-status.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/implementation-status.md)
  - [current-priority-gap-list.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/current-priority-gap-list.md)
  - [remaining-work-backlog.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/remaining-work-backlog.md)
  - [worker-lifecycle-protocol-and-lease.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/worker-lifecycle-protocol-and-lease.md)
  - [data-contracts-and-finalization.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/data-contracts-and-finalization.md)
- 当前代码入口：
  - [execution.py#L261](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/contracts/execution.py#L261)
  - [worker_supervisor.py#L1236](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/worker_supervisor.py#L1236)
  - [worker_supervisor.py#L1303](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/worker_supervisor.py#L1303)
  - [worker_supervisor.py#L1699](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/worker_supervisor.py#L1699)
  - [worker_supervisor.py#L2048](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/worker_supervisor.py#L2048)
  - [protocol_bridge.py#L152](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/protocol_bridge.py#L152)
  - [protocol_bridge.py#L374](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/protocol_bridge.py#L374)
  - [protocol_bridge.py#L471](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/protocol_bridge.py#L471)
  - [redis_bus.py#L49](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/bus/redis_bus.py#L49)
  - [bootstrap.py#L729](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/self_hosting/bootstrap.py#L729)
  - [bootstrap.py#L1012](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/self_hosting/bootstrap.py#L1012)
  - [storage/postgres/store.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/storage/postgres/store.py)
- 正式设计文档：
  - [2026-04-05-agent-orchestra-active-reattach-and-protocol-bus-design.md](/Volumes/disk1/Document/code/Agent-Orchestra/docs/superpowers/specs/2026-04-05-agent-orchestra-active-reattach-and-protocol-bus-design.md)

## 3. 当前已落地内容

当前代码已经把这条链真正接上：

- `DefaultWorkerSupervisor` 会在 out-of-process assignment 进入 wait 前持久化 `ACTIVE` session，并写入 `supervisor_id / supervisor_lease_id / supervisor_lease_expires_at / protocol_cursor`：[worker_supervisor.py#L1303](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/worker_supervisor.py#L1303)
- `_wait_with_protocol(...)` 观察到新 protocol event 时，会更新 durable session 的 lifecycle/cursor，并把 bus cursor 写到 store：[worker_supervisor.py#L2048](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/worker_supervisor.py#L2048)
- `recover_active_sessions(...)` 会扫描 reclaimable session，做 lease reclaim，再调用 backend `reattach(...)` 接回活 worker：[worker_supervisor.py#L1699](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/worker_supervisor.py#L1699)
- `protocol_bridge` 现在已经提供 `ProtocolBusEvent / ProtocolBusCursor / ProtocolBus / InMemoryProtocolBus / RedisProtocolBus`，并能从 `WorkerRecord.metadata` 归一化 protocol bus event：[protocol_bridge.py#L152](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/protocol_bridge.py#L152), [protocol_bridge.py#L374](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/protocol_bridge.py#L374), [protocol_bridge.py#L471](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/protocol_bridge.py#L471)
- `RedisEventBus` 现在具备 `publish_protocol_event(...) / read_protocol_events(...)`，统一覆盖 `lifecycle/session/control/takeover/mailbox` 五类 stream：[redis_bus.py#L99](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/bus/redis_bus.py#L99), [redis_bus.py#L134](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/bus/redis_bus.py#L134)
- self-hosting bootstrap 已经把 `durable-supervisor-sessions`、`reconnector`、`protocol-bus` 变成 evidence gate，不再只凭 `delivery_state == completed` 关闭：[bootstrap.py#L729](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/self_hosting/bootstrap.py#L729), [bootstrap.py#L1012](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/self_hosting/bootstrap.py#L1012)

## 4. 为什么不能继续只靠 timeout / idle session

当前主线已经完成的，其实是这几件事：

- protocol-native lifecycle wait 已接上
- `idle` session 可以持久化并跨 supervisor hydrate/reactivate
- reconnect cursor 已经有了 store-backed preference

但真正缺失的，是 supervisor 在 worker 仍然执行中的时候崩掉后的恢复能力：

- `WorkerSession` 的 durable truth 还主要停在 idle 场景
- `ACTIVE` worker 仍然强依赖原 supervisor 的内存 handle
- Redis 现在更像 mailbox bridge，不是 session takeover 的正式 control plane

所以系统今天能处理的是：

- 已完成后的持久化
- 空闲后的复用

还不能稳定处理的是：

- 执行到一半时 supervisor 挂掉，新的 supervisor 接着把活 worker 接回来继续盯到完成

判断：

- 这不是“把 `idle_timeout_seconds` 再调大一点”能解决的问题
- 也不是“再加一个 metadata 字段”能解决的问题
- 根因是 active session ownership、transport locator 和 bus coordination 还没有成为正式契约

## 5. 这轮设计的硬边界

这次设计明确限定在：

- 单机
- 多进程
- 同一文件系统命名空间
- 同一 PostgreSQL durable store
- 同一 Redis namespace

也就是说，这轮目标不是跨机器迁移，也不是“外部任意进程都能接管”，而是：

- 原 supervisor 在本机上拉起的 worker
- supervisor 自己挂掉或被重启
- 新 supervisor 仍能通过 durable session + local process locator + protocol bus，把这个 worker 重新接回主流程

## 6. 正式架构决策

### 5.1 PostgreSQL 是 durable truth

PostgreSQL 负责保存：

- worker session snapshot
- supervisor lease owner / expiry
- transport locator
- protocol cursor
- mailbox cursor
- reconnect cursor
- assignment ownership
- terminal session summary

它回答的问题是：

- 现在权威状态是什么
- 这个 session 属于谁
- 哪个 session 可以被 reclaim
- 该从哪里继续看 protocol 和 mailbox

### 5.2 Redis 是 protocol bus

Redis 负责传播：

- lifecycle events
- mailbox events
- session checkpoint
- control command
- takeover notice

它回答的问题是：

- 现在发生了什么
- 哪个 supervisor 需要立刻响应
- takeover 是否已经开始/完成/失败

因此后续不应再把 Redis 只当“mailbox 更耐久一点”的实现细节，而应明确视为 protocol bus。

### 5.3 Supervisor 通过 lease 拿所有权

每个 `ACTIVE` session 都必须有：

- `supervisor_id`
- `supervisor_lease_id`
- `supervisor_lease_expires_at`

规则是：

- 当前 supervisor 持续续租
- 只有 lease 过期后，新的 supervisor 才允许 reclaim
- reclaim 必须是 PostgreSQL 上的原子操作

这会取代今天大量“靠内存 map 推断谁还活着”的弱语义。

## 7. 关键契约升级

### 6.1 `WorkerSession` 不再只是 idle snapshot

后续 `WorkerSession` 要覆盖完整生命周期：

- `ASSIGNED`
- `ACTIVE`
- `IDLE`
- `COMPLETED`
- `FAILED`
- `ABANDONED`

当前的 `handle_snapshot` 也不适合作为主 transport 契约，应该收敛成更正式的 `WorkerTransportLocator`。

### 6.2 `WorkerTransportLocator`

locator 应至少包含：

- `backend`
- `working_dir`
- `spool_dir`
- `protocol_state_file`
- `result_file`
- `stdout_file`
- `stderr_file`
- `last_message_file`

按 backend 再补：

- `subprocess/codex_cli`：
  - `pid`
  - `process_group_id`
  - `command_fingerprint`
- `tmux`：
  - `session_name`
  - `pane_id`

判断：

- 真正的 process reattach 能不能成立，不取决于抽象名字叫不叫 reconnect
- 取决于这个 locator 是否足够支撑同机重连验证

### 6.3 backend 需要显式 `reattach(...)`

当前已有的：

- `resume(...)`
- `reactivate(...)`

都不足以表达“新 supervisor 接管仍在运行中的 worker”。

因此需要新增语义：

- capability：`supports_reattach`
- operation：`reattach(locator, assignment) -> WorkerHandle`

语义分工要固定：

- `resume`：
  - 原监督链里的继续执行
- `reactivate`：
  - idle session 的再次启用
- `reattach`：
  - supervisor 重启后的 active process 接管

## 8. Reconnector 的真正职责

这轮里 `reconnector` 不是“记住 cursor 就算完成”，而是一个明确的 reclaim/takeover 流程：

1. 扫描 lease 已过期但 session 仍处于 `ASSIGNED/ACTIVE` 的 durable session
2. 原子 reclaim lease
3. 调 backend `reattach(locator, assignment)`
4. 恢复 protocol cursor 与 mailbox cursor
5. 在 Redis protocol bus 上发送 takeover 事件
6. 继续按 protocol-native wait 路径盯到完成
7. 用同一 authoritative `WorkerRecord` 终结 assignment

如果 reclaim 成功但 `reattach` 失败，也不能再默默超时，而应显式写出：

- `reattach_failure_reason`
- session terminal status
- takeover failure event

## 9. protocol bus 应该长什么样

需要至少统一这几类 stream：

- `mailbox`
- `lifecycle`
- `session`
- `control`
- `takeover`

典型事件包括：

- `worker.accepted`
- `worker.checkpoint`
- `worker.final_report`
- `session.assigned`
- `session.active`
- `session.lease_renewed`
- `session.idle`
- `session.takeover_started`
- `session.takeover_completed`
- `session.takeover_failed`
- `control.cancel`
- `control.verify`
- `control.shutdown`

设计判断：

- Redis 不应成为唯一 durable 历史
- 但它必须成为唯一正式 realtime coordination path

因此恢复顺序应该固定成：

1. 先从 PostgreSQL 读 durable truth
2. 再根据已落盘 cursor 接 Redis
3. 再补消费缺失的 live events

## 10. 对 backlog 语义的修正

当前 backlog 里这三个条目其实不能再拆开理解成彼此独立的小功能：

- `durable-supervisor-sessions`
- `reconnector`
- `protocol-bus`

更准确的理解应该是：

- `durable-supervisor-sessions`：负责 session durable truth + lease ownership
- `reconnector`：负责 reclaim + process reattach + control-plane continuation
- `protocol-bus`：负责 lifecycle/mailbox/control/takeover 的实时协调

它们三者要一起 cutover，系统才会真正从“能复用 idle worker”升级为“能恢复 active worker”。

## 10. 后续实现顺序

推荐顺序固定为：

1. 先补 contracts/capabilities
2. 再补 PostgreSQL durable session/lease/locator
3. 再补 backend `reattach(...)`
4. 再补 supervisor/reconnector reclaim path
5. 最后把 Redis mailbox 提升成 protocol bus 主线

原因：

- 没有 locator 和 lease，reconnector 只是空壳
- 没有 backend `reattach(...)`，protocol bus 也只能广播失败
- 没有 protocol bus，takeover 只能靠 store 轮询，仍然不是真正控制面

## 11. 相关文档

- [2026-04-05-agent-orchestra-active-reattach-and-protocol-bus-design.md](/Volumes/disk1/Document/code/Agent-Orchestra/docs/superpowers/specs/2026-04-05-agent-orchestra-active-reattach-and-protocol-bus-design.md)
- [worker-lifecycle-protocol-and-lease.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/worker-lifecycle-protocol-and-lease.md)
- [data-contracts-and-finalization.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/data-contracts-and-finalization.md)
- [implementation-status.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/implementation-status.md)
- [current-priority-gap-list.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/current-priority-gap-list.md)
