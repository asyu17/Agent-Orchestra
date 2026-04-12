# agent_orchestra worker 生命周期协议与租约模型

## 1. 一句话结论

截至 2026 年 4 月 4 日，`agent_orchestra` 的 worker lifecycle 已经完成了这轮主线切换：`WorkerExecutionContract / WorkerLeasePolicy / WorkerLease / WorkerLifecycleEvent / WorkerFinalReport / WorkerRoleProfile` 不只是抽象存在，而且已经进入真实 mainline。`DefaultWorkerSupervisor.wait(...)` 会在 protocol-capable handle 上按 `protocol_state_file` 原生等待 `accepted / renewal / hard deadline`；`GroupRuntime` 会在 launch 前做 capability gate；`codex_cli`、`worker_process`、`subprocess`、`tmux` 也都已经能稳定写出 `protocol_events / final_report`。因此 `protocol-native-lifecycle-wait` 这条 gap 现在应视为已完成主线切换，timeout 只剩 transport fallback 和 safety rail 语义。

## 2. 范围与资料来源

- 当前代码入口：
  - [execution.py#L50](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/contracts/execution.py#L50)
  - [execution.py#L216](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/contracts/execution.py#L216)
  - [worker_supervisor.py#L154](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/worker_supervisor.py#L154)
  - [worker_supervisor.py#L466](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/worker_supervisor.py#L466)
  - [worker_supervisor.py#L1583](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/worker_supervisor.py#L1583)
  - [group_runtime.py#L304](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/group_runtime.py#L304)
  - [codex_cli_backend.py#L245](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/backends/codex_cli_backend.py#L245)
  - [worker_process.py#L96](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/worker_process.py#L96)
  - [subprocess_backend.py#L69](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/backends/subprocess_backend.py#L69)
  - [tmux_backend.py#L62](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/backends/tmux_backend.py#L62)
  - [bootstrap_round.py#L128](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/bootstrap_round.py#L128)
  - [leader_output_protocol.py#L150](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_output_protocol.py#L150)
  - [leader_loop.py#L868](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py#L868)
  - [bootstrap.py#L465](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/self_hosting/bootstrap.py#L465)
- 关键验证：
  - [test_backends.py#L308](/Volumes/disk1/Document/code/Agent-Orchestra/tests/test_backends.py#L308)
  - [test_worker_process_protocol.py#L66](/Volumes/disk1/Document/code/Agent-Orchestra/tests/test_worker_process_protocol.py#L66)
  - [test_worker_reliability.py#L445](/Volumes/disk1/Document/code/Agent-Orchestra/tests/test_worker_reliability.py#L445)
  - [test_leader_loop.py#L299](/Volumes/disk1/Document/code/Agent-Orchestra/tests/test_leader_loop.py#L299)
  - [test_self_hosting_round.py#L287](/Volumes/disk1/Document/code/Agent-Orchestra/tests/test_self_hosting_round.py#L287)

## 3. 当前已落地到什么程度

### 3.1 协议对象已经成为主线数据结构

现在的主线不再只靠自由文本和 `metadata` 约定，而是显式使用：

- `WorkerExecutionContract`
- `WorkerLeasePolicy`
- `WorkerLease`
- `WorkerLifecycleEvent`
- `WorkerFinalReport`
- `WorkerRoleProfile`

`WorkerAssignment` 真正持有 `execution_contract / lease_policy / role_profile`，[execution.py#L216](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/contracts/execution.py#L216) 到 [execution.py#L218](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/contracts/execution.py#L218)；`WorkerResult` 真正持有 `protocol_events / final_report`，[execution.py#L231](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/contracts/execution.py#L231) 到 [execution.py#L232](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/contracts/execution.py#L232)；最终 authoritative `WorkerRecord` 会把 `final_report` 写入 metadata，[worker_supervisor.py#L466](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/worker_supervisor.py#L466)

### 3.2 capability gate 已经接进 launch 主线

`GroupRuntime` 现在会在真正 launch 前解析 assignment/policy 上的 contract 与 lease，并根据 backend capability 做 fail-fast 校验：

- protocol contract required，但 backend 不支持 protocol contract：直接拒绝
- `require_final_report=True`，但 backend 不支持 final report：直接拒绝
- 要求 idle session reuse，但 backend 不支持 reactivation：直接拒绝

源码入口是 [group_runtime.py#L304](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/group_runtime.py#L304)；相应回归见 [test_worker_reliability.py#L445](/Volumes/disk1/Document/code/Agent-Orchestra/tests/test_worker_reliability.py#L445)

### 3.3 native protocol wait 已经替代“只靠 timeout 猜测”

`DefaultWorkerSupervisor.wait(...)` 现在已经收口成 protocol-native 主语义：

- out-of-process path：
  - 只要 handle 暴露 `protocol_state_file`，就直接进入 protocol-native wait
  - supervisor 按 `accepted / renewal / hard deadline` 消费协议状态
  - live `protocol_events / final_report` 会被合并回最终 payload
- legacy progress/idle wait：
  - 已从 supervisor 主线移除
  - 对于还想依赖 `idle_timeout_seconds` 但 backend 又没有 protocol state 的组合，runtime 会显式拒绝，而不是再退回 artifact-progress polling
- raw result compatibility tail：
  - 也已从 supervisor 主线移除
  - 现在不会再出现“没有 `protocol_state_file` 也先靠 `process.wait + result_file` 把结果捞回来”的旧行为；如果 backend 自称支持 protocol 但 launch handle 没带 `protocol_state_file`，worker 会直接失败

关键入口是 [worker_supervisor.py#L1583](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/worker_supervisor.py#L1583)

### 3.4 真实 transport 已经协议化

方案 2 的关键不是 supervisor 自己会读协议，而是 transport 真正开始产出协议事实：

- `codex_cli`：
  - launch 后写 `accepted`
  - 观察到 artifact 变化时写 `checkpoint`
  - 退出时写 `final_report`
  - 再把这组事实并入最终 result
  - 入口：[codex_cli_backend.py#L245](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/backends/codex_cli_backend.py#L245), [codex_cli_backend.py#L275](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/backends/codex_cli_backend.py#L275), [codex_cli_backend.py#L432](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/backends/codex_cli_backend.py#L432)
- `worker_process`：
  - 支持 `--protocol-state-file`
  - 在 protocol mode 下写 `accepted / checkpoint / final_report`
  - 入口：[worker_process.py#L96](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/worker_process.py#L96), [worker_process.py#L140](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/worker_process.py#L140)
- `subprocess` / `tmux`：
  - 在 contract/lease 存在时传递 `--protocol-state-file`
  - 暴露 `protocol_state_file` 给 supervisor
  - 入口：[subprocess_backend.py#L80](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/backends/subprocess_backend.py#L80), [tmux_backend.py#L73](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/backends/tmux_backend.py#L73)

### 3.5 role profile 已经真正透传

这一层也不再停留在配置外壳：

- leader assignment 会把 `metadata["role_profile_id"]`、`execution_contract`、`lease_policy`、`role_profile` 一起写进去：[bootstrap_round.py#L128](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/bootstrap_round.py#L128)
- teammate assignment 也同样透传：[leader_output_protocol.py#L150](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_output_protocol.py#L150)
- `LeaderLoopSupervisor.run(...)` 会先解析 `WorkerRoleProfile`，再推导 backend 与 execution policy：[leader_loop.py#L868](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py#L868)
- self-hosting 也是 profile-first：[bootstrap.py#L465](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/self_hosting/bootstrap.py#L465)

对应回归见 [test_leader_loop.py#L299](/Volumes/disk1/Document/code/Agent-Orchestra/tests/test_leader_loop.py#L299) 与 [test_self_hosting_round.py#L287](/Volumes/disk1/Document/code/Agent-Orchestra/tests/test_self_hosting_round.py#L287)

### 3.6 verification contract 已经收敛到 protocol-first 语义

从 2026-04-05 起，`require_verification_results=True` 不再只是 role profile 上的软声明，而是真正进入 protocol validation；到 2026-04-06，这条链又往前收了一步，开始明确区分“worker 自己拥有的验证闭环”和“runtime 最后的 safety-net”：

- `DefaultWorkerSupervisor` 在 protocol-first finalize 阶段会把 `final_report.verification_results` 视为 authoritative verification evidence；如果 contract 要求 verification results，但 final report 缺失或解析后为空，就会把 record 直接标成 `protocol_failure_reason = missing_verification_results`
- `WorkerExecutionContract` 现在还可以携带 `required_verification_commands` 与 `completion_requires_verification_success`；对于 teammate code-edit assignment，这意味着 worker 不再只需要“带回任意 verification_results”，而是必须覆盖 required command set，且只有全部通过才能以 `terminal_status = completed` 收束
- `VerificationCommandResult` 现在允许通过 `requested_command` 把“原始要求的命令”和“真实执行的 fallback 命令”分离表达；这让 teammate 可以在环境不兼容时用 `.venv/bin/pytest` 之类的 fallback 路径自测，同时仍满足 contract 所要求的原始 verification intent
- `codex_cli` teammate prompt 也已经切成 worker-owned verification loop：prompt 会显式要求 `implement -> test -> fix -> retest`，并要求在 final response 中返回 JSON-only `final_report` payload，而不是只给一段自由文本总结
- `GroupRuntime.apply_execution_guard(...)` 不再无条件重跑原始 `verification_commands`；它会先消费 authoritative `final_report.verification_results`
- authoritative verification 与 assignment command 的匹配不再只看原始字符串，而是允许 command-equivalence 归一；只有 authoritative evidence 缺失或不等价时，guard 才回退到 working-dir rerun

这意味着 verification 语义现在也遵守和 lifecycle/lease 相同的优先级：

1. 先看 protocol/final-report carried authoritative evidence
2. 不足时才退回 transport-level rerun / timeout fallback

## 4. timeout 现在应该怎么解释

在当前模型里：

- `idle_timeout_seconds`
  - 首要作用已经不是“业务语义上的沉默即失败”
  - 在当前主线里，它只应该通过 protocol-native lease / silence 语义被消费
  - 对于没有 protocol state 的 out-of-process backend，系统不再允许用它触发 legacy progress/idle wait
- `hard_timeout_seconds`
  - 仍然是绝对上限
  - 但当 protocol-native wait 可用时，它不再替代 lifecycle/lease/final-report 语义

换句话说，正确优先级已经变成：

1. 先看 contract / lease / lifecycle event / final report
2. 再看 timeout

进一步说，当前主线的正式约束已经是：

1. `in_process` 可以不依赖 `protocol_state_file`
2. 任何 out-of-process worker 都必须进入 protocol-native wait
3. 缺少 protocol state 的 out-of-process backend 不再被兼容

补充一条 2026-04-06 收口后的操作性规则：

- 对于 role-profile 驱动的 `codex_cli` worker，fallback timeout 与 protocol-native lease timeout 不能再来自两套独立来源。
- 也就是说，像 `leader_idle_timeout_seconds / leader_hard_timeout_seconds` 这类显式 override，一旦进入 role profile，就必须同时驱动：
  - `fallback_idle_timeout_seconds / fallback_hard_timeout_seconds`
  - `lease_policy.renewal_timeout_seconds / hard_deadline_seconds`
- 否则就会出现一种非常隐蔽的分叉：表面上 fallback timeout 已经放宽，但真正生效的 protocol-native renewal timeout 仍停留在旧值，于是 worker 仍会被 supervisor 以 `lease_renewal_timeout` 提前取消。

## 5. 这轮修掉了什么真实故障

这轮之前的真实问题有两类：

- `codex_cli` raw result 已经显示 `completed`，但 authoritative `WorkerRecord` 会因为缺少 `accepted / final_report` 被协议层改写为失败
- raw result 与 final report 都显示完成，但 execution guard 仍会因为只认 rerun `verification_commands` 而把 nominal success 再次改写成失败

现在这两条链都已经被主线修掉：

- `codex_cli` 自己开始产出协议事实：[codex_cli_backend.py#L275](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/backends/codex_cli_backend.py#L275)
- `subprocess/tmux` 通过 `worker_process` 同步补齐协议主线：[worker_process.py#L140](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/worker_process.py#L140)
- incompatible transport/profile 组合不再放行到运行中间态，而是在 launch 前 fail-fast：[group_runtime.py#L314](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/group_runtime.py#L314)
- protocol-first finalize 现在会真正执行 `require_verification_results`，guard 则优先消费 authoritative verification results，再回退到 rerun

相应回归：

- [test_backends.py#L308](/Volumes/disk1/Document/code/Agent-Orchestra/tests/test_backends.py#L308)
- [test_worker_process_protocol.py#L66](/Volumes/disk1/Document/code/Agent-Orchestra/tests/test_worker_process_protocol.py#L66)
- [test_self_hosting_round.py#L287](/Volumes/disk1/Document/code/Agent-Orchestra/tests/test_self_hosting_round.py#L287)

## 6. 新增的失败归因层

2026 年 4 月 6 日这轮又补上了一层之前缺失的主线语义：worker 如果最终失败，runtime 现在不再只给出一个宽泛的 `failed`，而会把“这是 supervisor timeout、protocol contract failure，还是进程被外部终止”拆成更可复用的 attribution metadata。

直接入口：

- [worker_supervisor.py#L712](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/worker_supervisor.py#L712)
- [worker_supervisor.py#L741](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/worker_supervisor.py#L741)
- [worker_supervisor.py#L2451](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/worker_supervisor.py#L2451)
- [codex_cli_backend.py#L362](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/backends/codex_cli_backend.py#L362)
- [test_backends.py#L520](/Volumes/disk1/Document/code/Agent-Orchestra/tests/test_backends.py#L520)
- [test_worker_supervisor_protocol.py#L928](/Volumes/disk1/Document/code/Agent-Orchestra/tests/test_worker_supervisor_protocol.py#L928)

当前 authoritative `WorkerRecord.metadata` 里新增并统一收口了这些字段：

- `backend_cancel_invoked`
  - 标识 supervisor 是否真的走过 backend cancel 路径
- `supervisor_timeout_path`
  - 标识这次失败是否命中了 supervisor 自己的 timeout 分支
- `last_protocol_progress_at`
  - 记录协议事件里最后一次可观察 progress 的时间
- `last_message_exists_at_failure`
  - 记录失败时是否已经产出 `last_message`
- `termination_signal`
  - 负 exit code 归一化后的 signal number
- `termination_signal_name`
  - 对应的人类可读 signal 名称，例如 `SIGTERM`
- `failure_is_timeout`
- `failure_is_protocol_contract`
- `failure_is_process_termination`
- `failure_tags`
  - 当前主线会产出例如 `timeout_failure`、`protocol_contract_failure`、`process_termination`、`signal_sigterm`

这里的关键设计判断是：

1. transport/backend 先尽量写出低层事实
   - 例如 `codex_cli_backend` 会在 raw payload 上直接带出 `termination_signal`、`termination_signal_name`、`last_message_exists_at_failure`
2. `DefaultWorkerSupervisor` 再统一做 authoritative normalization
   - `_apply_failure_attribution(...)` 会把 timeout、protocol failure、process termination 收口成同一套 metadata truth
3. 后续排障时必须先读这组 attribution，再判断是否需要继续下钻 execution guard、provider、network、transport

这层语义修掉的不是“worker 为什么失败”的全部根因，而是“失败类型经常被混淆”的问题。最典型的历史混淆是：

- worker 结果显示 `exit_code = -15`
- 没有 `last_message`
- stderr 只有插件同步或鉴权噪音
- 但先前 artifact 里无法快速分辨这是 supervisor timeout、protocol contract 不满足，还是外部 `SIGTERM`

现在这类场景的排查顺序应该固定成：

1. 先看 `failure_tags`

## 7. 2026-04-09 新增的 codex_cli 暂时性网络失败重试

2026-04-09 这轮又补上了一条直接面向 self-hosting live run 的收口：`codex_cli` backend 现在会把一组常见的暂时性 provider / network / plugin-sync 失败显式归类成 `failure_kind=provider_unavailable`，而 supervisor 会对这类失败在同一路由上做有限次自动重试，不再要求 `allow_relaunch=True` 才能重发。

当前已经纳入这类 transient failure 识别的典型信号包括：

- `chatgpt authentication required to sync remote plugins`
- `remote plugin sync request`
- `plugins/featured`
- `403 Forbidden`
- `stream disconnected - retrying sampling request`
- `error sending request for url`

这条新语义的落点有两层：

1. `codex_cli_backend`
   - 在 result materialization 时分析 stderr
   - 如果命中上述瞬时 provider/network 信号，就把 raw payload 写成：
     - `failure_kind = provider_unavailable`
     - `error_type = ProviderUnavailableError`
2. `DefaultWorkerSupervisor`
   - `_decide_next_action(...)` 现在会把 `provider_unavailable` 视为可在当前 route 上重试的失败类型
   - 只要 `max_attempts` 还有余额，且 `fallback_on_provider_unavailable=True`，即使 `allow_relaunch=False` 也会先做同路 `RETRY`
   - 当前 route 的 attempts 用尽后，才继续走 fallback route 或 escalation

对 `codex_cli` role profile，当前默认值也已经同步放宽到更适合临时网络波动的范围：

- `fallback_max_attempts = 3`
- `fallback_backoff_seconds = 2.0`
- `fallback_provider_unavailable_backoff_initial_seconds = 15.0`
- `fallback_provider_unavailable_backoff_multiplier = 2.0`
- `fallback_provider_unavailable_backoff_max_seconds = 120.0`
- `fallback_allow_relaunch = False`

这里刻意保持 `fallback_allow_relaunch = False` 不变，是为了把“普通业务失败”与“暂时性 provider/network 失败”分开处理：

- 普通实现失败、验证失败、协议失败，不会因为这次改动被无差别重复执行
- 只有被识别为 `provider_unavailable` 的临时性失败，才会进入有限次自动重发

2026-04-10 这条线又进一步收口了一层：`provider_unavailable` 不再只复用通用 `fallback_backoff_seconds`。当前 runtime 已引入 provider-unavailable 专属指数退避：

- `WorkerExecutionPolicy` 现在显式持有：
  - `provider_unavailable_backoff_initial_seconds`
  - `provider_unavailable_backoff_multiplier`
  - `provider_unavailable_backoff_max_seconds`
- `WorkerRoleProfile` 通过对应的 `fallback_provider_unavailable_*` 字段把这组策略透传到 execution policy
- `DefaultWorkerSupervisor._sleep_with_backoff(...)` 会在 `failure_kind == provider_unavailable` 时使用指数退避，而普通失败仍保留原有 `route.backoff_seconds / policy.backoff_seconds` 语义
- 当前 `codex_cli` 默认 profile 已落成：
  - 第 1 次 `provider_unavailable` 重试前等待 `15s`
  - 后续按 `x2` 增长
  - 最大封顶 `120s`

对应回归：

- `tests/test_backends.py::test_codex_cli_backend_marks_transient_plugin_sync_failure_as_provider_unavailable`
- `tests/test_worker_reliability.py::test_group_runtime_retries_provider_unavailable_even_when_generic_relaunch_is_disabled`
- `tests/test_self_hosting.py::test_build_self_hosting_superleader_config_adds_codex_role_profiles`
- `tests/test_self_hosting_round.py::test_bootstrap_superleader_config_prefers_role_profiles_for_codex`
- `tests/test_worker_reliability.py::test_group_runtime_uses_provider_unavailable_specific_backoff_sequence`
- `tests/test_worker_reliability.py::test_group_runtime_keeps_generic_backoff_for_non_provider_failures`
- `tests/test_worker_protocol_contracts.py::test_worker_role_profile_materializes_execution_policy`
2. 再看 `supervisor_timeout_path`
3. 再看 `backend_cancel_invoked`
4. 再看 `termination_signal_name`
5. 最后结合 `last_protocol_progress_at` 与 `last_message_exists_at_failure`

判断：

- 如果 `termination_signal_name=SIGTERM`、`failure_is_process_termination=True`，同时 `supervisor_timeout_path=False`，那它更像 transport/provider/外层 orchestration 终止，而不是 AO supervisor 自己的 timeout 裁决
- 如果 `supervisor_timeout_path=True` 且 `backend_cancel_invoked=True`，才应优先怀疑 AO 自己的 lifecycle wait / timeout 分支
- 如果 `failure_is_protocol_contract=True`，说明即使进程退出了，也还要继续区分“进程被杀”与“协议没有满足 contract”这两层事实，不能混成一个笼统 failed

## 7. 剩余真正开放的生命周期缺口

当前还没有完成的是更高一层的 lifecycle/control-plane：

- `durable-supervisor-sessions`
  - 跨 transport、跨进程、跨宿主的 resident session
- `reconnector`
  - crash 后重新接管 worker / session / control-plane 实体
- 更强的 provider memory / sticky routing
  - 这属于 lifecycle 与 provider control plane 的交叉层

因此现在不要再把“protocol-native-lifecycle-wait”当作最高优先级 backlog；真正剩下的是“durable lifecycle”而不是“missing protocol artifacts”。

## 8. 后续阅读

- [implementation-status.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/implementation-status.md)
- [self-hosting-bootstrap.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/self-hosting-bootstrap.md)
- [data-contracts-and-finalization.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/data-contracts-and-finalization.md)
- [2026-04-04-agent-orchestra-mainline-capability-cutover-design.md](/Volumes/disk1/Document/code/Agent-Orchestra/docs/superpowers/specs/2026-04-04-agent-orchestra-mainline-capability-cutover-design.md)
