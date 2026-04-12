# agent_orchestra execution guard failure patterns

## 1. 一句话结论

在 `agent_orchestra` runtime 里，`codex_cli` worker 的原始 `.result.json` 即使写成 `completed`，也仍然可能被 `GroupRuntime` 的 execution guard 在 authoritative `WorkerRecord` 层重写为 `failed`；2026-04-04 这轮 self-hosting group coordination 暴露出的三类误判根因，分别是基线快照与当前快照的 ignore 集不对称、`resource -> docs/resource` 符号链接导致的 `owned_paths` 字面不匹配，以及未忽略的 `__pycache__` 产物。到 2026-04-05，这个诊断模板还需要再加一条：如果 final report 已经携带 authoritative `verification_results`，guard 不应再把“只认 rerun 原始命令”当成另一套独立成功语义；当前主线已经改成 protocol-first verification，只有 authoritative evidence 缺失或不等价时才回退 rerun。到 2026-04-06，这里又补了一条关键修正：authoritative evidence 不只允许 token-equivalent 的同一命令，也允许 teammate 通过 `requested_command -> command` 映射上报“原始要求命令”和“真实 fallback 执行命令”，避免环境兼容性 fallback 再次被 guard 误判成失败。到 2026-04-09，这条主语义又正式切了一刀：execution guard 的硬边界现在是 `target_roots`，repo 内 `owned_paths` 偏移不再直接把 worker 打成失败，而是落成 `scope_drift`；同时 fallback verification adjudication 也改成“同一 required command 优先最后一条成功结果，否则取最后一条等价结果”，从而避免“一开始失败、最后成功”的 fallback loop 被误判成最终失败。

## 2. 范围与资料来源

- 源码：
  - [group_runtime.py#L194](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/group_runtime.py#L194)
  - [group_runtime.py#L220](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/group_runtime.py#L220)
  - [group_runtime.py#L316](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/group_runtime.py#L316)
  - [group_runtime.py#L361](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/group_runtime.py#L361)
  - [group_runtime.py#L383](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/group_runtime.py#L383)
  - [leader_loop.py#L325](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py#L325)
  - [evaluator.py#L65](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/evaluator.py#L65)
- 运行产物：
  - [2026-04-04-codex-self-hosting-group-coordination-round-1.json#L275](/Volumes/disk1/Document/code/Agent-Orchestra/docs/superpowers/runs/2026-04-04-codex-self-hosting-group-coordination-round-1.json#L275)
  - [2026-04-04-codex-self-hosting-group-coordination-round-1.instruction.md#L24](/Volumes/disk1/Document/code/Agent-Orchestra/docs/superpowers/runs/2026-04-04-codex-self-hosting-group-coordination-round-1.instruction.md#L24)
  - [task_a522a7ed9a52:teammate-turn-1.result.json#L1](/Volumes/disk1/Document/code/Agent-Orchestra/docs/superpowers/runs/codex-spool/task_a522a7ed9a52:teammate-turn-1.result.json#L1)
  - [task_6b26d6980392:teammate-turn-1.result.json#L1](/Volumes/disk1/Document/code/Agent-Orchestra/docs/superpowers/runs/codex-spool/task_6b26d6980392:teammate-turn-1.result.json#L1)
- 仓库约束：
  - [AGENTS.md#L22](/Volumes/disk1/Document/code/Agent-Orchestra/AGENTS.md#L22)

## 3. 现象

这轮 self-hosting round 里，同时出现了下面三层看起来互相矛盾的状态：

- raw backend result 显示 teammate worker 成功完成：
  - [task_a522a7ed9a52:teammate-turn-1.result.json#L1](/Volumes/disk1/Document/code/Agent-Orchestra/docs/superpowers/runs/codex-spool/task_a522a7ed9a52:teammate-turn-1.result.json#L1)
  - [task_6b26d6980392:teammate-turn-1.result.json#L1](/Volumes/disk1/Document/code/Agent-Orchestra/docs/superpowers/runs/codex-spool/task_6b26d6980392:teammate-turn-1.result.json#L1)
- authoritative round artifact 里的 `teammate_records` 却已经是 `failed`，并且带 `guard_status = path_violation`：
  - [2026-04-04-codex-self-hosting-group-coordination-round-1.json#L277](/Volumes/disk1/Document/code/Agent-Orchestra/docs/superpowers/runs/2026-04-04-codex-self-hosting-group-coordination-round-1.json#L277)
  - [2026-04-04-codex-self-hosting-group-coordination-round-1.json#L427](/Volumes/disk1/Document/code/Agent-Orchestra/docs/superpowers/runs/2026-04-04-codex-self-hosting-group-coordination-round-1.json#L427)
  - [2026-04-04-codex-self-hosting-group-coordination-round-1.json#L568](/Volumes/disk1/Document/code/Agent-Orchestra/docs/superpowers/runs/2026-04-04-codex-self-hosting-group-coordination-round-1.json#L568)
  - [2026-04-04-codex-self-hosting-group-coordination-round-1.json#L715](/Volumes/disk1/Document/code/Agent-Orchestra/docs/superpowers/runs/2026-04-04-codex-self-hosting-group-coordination-round-1.json#L715)
- lane 最终被 evaluator 判成 `blocked`：
  - [2026-04-04-codex-self-hosting-group-coordination-round-1.json#L21](/Volumes/disk1/Document/code/Agent-Orchestra/docs/superpowers/runs/2026-04-04-codex-self-hosting-group-coordination-round-1.json#L21)
  - [2026-04-04-codex-self-hosting-group-coordination-round-1.instruction.md#L21](/Volumes/disk1/Document/code/Agent-Orchestra/docs/superpowers/runs/2026-04-04-codex-self-hosting-group-coordination-round-1.instruction.md#L21)

结论是：这里不是 lane 后处理又额外改写了一遍，而是 authoritative `WorkerRecord` 早在 guard 阶段就已经从 nominal `completed` 被改成了 `failed`。

## 4. 精确状态流转链路

状态是按下面这条链路变化的：

1. `DefaultWorkerSupervisor` 跑完 worker，得到 nominal `WorkerRecord`。
2. `GroupRuntime.run_worker_assignment(...)` 在 worker 启动前先抓一份 baseline working tree snapshot，然后在 supervisor 返回后调用 `_apply_guard_to_record(...)`：
   - [group_runtime.py#L194](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/group_runtime.py#L194)
3. `apply_execution_guard(...)` 计算 `modified_paths`、`out_of_scope_paths`，只要有越界路径就直接返回 `PATH_VIOLATION`：
   - [group_runtime.py#L220](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/group_runtime.py#L220)
4. `_apply_guard_to_record(...)` 看到 guard 不是 `PASSED`，无条件把 `record.status` 改成 `FAILED`，并把 summary 写进 `record.error_text`：
   - [group_runtime.py#L383](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/group_runtime.py#L383)
5. `LeaderLoopSupervisor._run_teammate_assignment(...)` 并不看 raw backend result，只看这份已经被 guard 裁决过的 `record.status`；因此它把 task 更新为 `FAILED`：
   - [leader_loop.py#L371](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py#L371)
6. `DefaultDeliveryEvaluator.evaluate_lane(...)` 会把 `TaskStatus.FAILED` 和 `TaskStatus.BLOCKED` 都算进 `blocked_task_ids`，于是 lane summary 变成 `Lane ... is blocked by task state.`：
   - [evaluator.py#L65](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/evaluator.py#L65)

## 5. 这轮触发 `path_violation` 的三个具体根因

### 5.1 基线快照与当前快照的 ignore 集不对称，导致整个 codex spool 被误判为“修改”

这是这轮噪音最大的来源。

- `run_worker_assignment(...)` 在拿到 worker handle 之前就抓 baseline snapshot：
  - [group_runtime.py#L202](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/group_runtime.py#L202)
- 这时还没有 `handle.metadata["spool_root"]`，所以 baseline snapshot 没有忽略 `docs/superpowers/runs/codex-spool/`
- 但 `apply_execution_guard(...)` 的 current snapshot 会通过 `_guard_ignored_paths(...)` 忽略当前 handle 的 `spool_root` 和若干 `*_file`：
  - [group_runtime.py#L230](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/group_runtime.py#L230)
  - [group_runtime.py#L316](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/group_runtime.py#L316)

于是 `modified_paths` 的计算变成了：

- baseline 里有整棵 `codex-spool`
- current 里这棵树被整体忽略
- 两边做并集比较后，这些 spool 文件会被当成“发生变化”

这就是为什么 round artifact 里出现了 59~64 个与当前 teammate 实际改动无关的 spool 路径，例如：

- [2026-04-04-codex-self-hosting-group-coordination-round-1.json#L431](/Volumes/disk1/Document/code/Agent-Orchestra/docs/superpowers/runs/2026-04-04-codex-self-hosting-group-coordination-round-1.json#L431)
- [2026-04-04-codex-self-hosting-group-coordination-round-1.json#L719](/Volumes/disk1/Document/code/Agent-Orchestra/docs/superpowers/runs/2026-04-04-codex-self-hosting-group-coordination-round-1.json#L719)

判断：这些 spool 路径绝大多数不是 worker 越权编辑了运行目录，而是 snapshot diff 的 ignore 不对称导致的假阳性。

### 5.2 `resource -> docs/resource` 符号链接导致 `owned_paths` 与 snapshot path 字面不匹配

这轮里 teammate 被分配到的知识文档 owned paths 都写成了 `resource/knowledge/...`：

- [2026-04-04-codex-self-hosting-group-coordination-round-1.instruction.md#L27](/Volumes/disk1/Document/code/Agent-Orchestra/docs/superpowers/runs/2026-04-04-codex-self-hosting-group-coordination-round-1.instruction.md#L27)
- [2026-04-04-codex-self-hosting-group-coordination-round-1.instruction.md#L32](/Volumes/disk1/Document/code/Agent-Orchestra/docs/superpowers/runs/2026-04-04-codex-self-hosting-group-coordination-round-1.instruction.md#L32)
- [task_a522a7ed9a52:teammate-turn-1.prompt.md#L30](/Volumes/disk1/Document/code/Agent-Orchestra/docs/superpowers/runs/codex-spool/task_a522a7ed9a52:teammate-turn-1.prompt.md#L30)
- [task_6b26d6980392:teammate-turn-1.prompt.md#L30](/Volumes/disk1/Document/code/Agent-Orchestra/docs/superpowers/runs/codex-spool/task_6b26d6980392:teammate-turn-1.prompt.md#L30)

但仓库本身把 `resource` 作为 `docs/resource` 的符号链接使用：

- [AGENTS.md#L22](/Volumes/disk1/Document/code/Agent-Orchestra/AGENTS.md#L22)

而 `_owned_paths(...)` 对相对路径只做字符串归一化，不做 realpath / symlink canonicalization：

- [group_runtime.py#L361](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/group_runtime.py#L361)

`_snapshot_working_tree(...)` 记录下来的相对路径却是实际扫描到的 `docs/resource/...` 路径：

- [group_runtime.py#L333](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/group_runtime.py#L333)

所以同一个物理文件：

- assignment 允许的是 `resource/knowledge/...`
- guard 看到的却是 `docs/resource/knowledge/...`

这就是为什么 artifact 里会把知识文档本身列为 out-of-scope，例如：

- [2026-04-04-codex-self-hosting-group-coordination-round-1.json#L429](/Volumes/disk1/Document/code/Agent-Orchestra/docs/superpowers/runs/2026-04-04-codex-self-hosting-group-coordination-round-1.json#L429)
- [2026-04-04-codex-self-hosting-group-coordination-round-1.json#L717](/Volumes/disk1/Document/code/Agent-Orchestra/docs/superpowers/runs/2026-04-04-codex-self-hosting-group-coordination-round-1.json#L717)

判断：这是当前 execution guard 的 path normalization contract 缺口，而不是 teammate 真正越权修改了知识目录。

### 5.3 `__pycache__` 没有被 guard 忽略

这轮 out-of-scope 列表里还包含了 Python 自动生成的缓存文件：

- [2026-04-04-codex-self-hosting-group-coordination-round-1.json#L280](/Volumes/disk1/Document/code/Agent-Orchestra/docs/superpowers/runs/2026-04-04-codex-self-hosting-group-coordination-round-1.json#L280)
- [2026-04-04-codex-self-hosting-group-coordination-round-1.json#L568](/Volumes/disk1/Document/code/Agent-Orchestra/docs/superpowers/runs/2026-04-04-codex-self-hosting-group-coordination-round-1.json#L568)

`_snapshot_working_tree(...)` 目前只跳过 `.git`，不会跳过 `__pycache__`：

- [group_runtime.py#L342](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/group_runtime.py#L342)

因此只要验证命令或测试触发 import，这些缓存文件也会进入 `modified_paths`，进一步放大 path violation 噪音。

## 6. 对后续修复最有价值的判断

后续如果再看到“raw result completed，但 lane/task 却 blocked/failed”的现象，优先按下面顺序排查：

1. 先区分 raw backend result 和 authoritative `WorkerRecord`，不要把 spool `.result.json` 当成最终状态。
2. 先看 authoritative record metadata 里的 failure attribution：
   - `failure_tags`
   - `supervisor_timeout_path`
   - `backend_cancel_invoked`
   - `termination_signal_name`
   - `last_protocol_progress_at`
   - `last_message_exists_at_failure`
3. 只有在 authoritative record 明确显示是 guard 改写时，再看 round artifact 里的 `guard_status / modified_paths / out_of_scope_paths`。
4. 如果 out-of-scope 里主要是 `docs/superpowers/runs/codex-spool/...`，优先怀疑 baseline/current ignore 不对称。
5. 如果 out-of-scope 里出现 `docs/resource/...` 而 assignment 写的是 `resource/...`，优先怀疑 symlink canonicalization 缺口。
6. 如果列表里混有 `__pycache__`，说明当前 snapshot ignore 规则还把解释器缓存当成真实业务修改。

补充判断：

- 如果 `termination_signal_name=SIGTERM` 且 `supervisor_timeout_path=False`，不要误判成 execution guard 或 supervisor timeout；它更像 transport/provider/外层 orchestration 终止。
- 如果 `supervisor_timeout_path=True` 且 `backend_cancel_invoked=True`，才优先怀疑 AO 自己的 timeout wait path。
- 如果 `failure_tags` 同时包含 `protocol_contract_failure` 与 `process_termination`，说明要分开理解“两件事同时发生”：进程被终止是 transport 事实，协议不满足是 contract 事实，不能只保留其中一层。

## 7. 当前修复状态

这条 historical failure pattern 已经在主线修复，修复内容包括：

- `apply_execution_guard(...)` 现在会对 baseline snapshot 和 current snapshot 使用对称的 ignored path 过滤，不再把 preexisting `spool_root` 文件误算成“本轮修改”。
- `_owned_paths(...)` 现在会把相对 `owned_paths` 解析到 `working_dir` 的 canonical real path 语义，再回写成 working-tree relative path，因此 `resource/...` 与 `docs/resource/...` 这类仓库内符号链接别名不会再被误判为越界。
- `_snapshot_working_tree(...)` 与 baseline snapshot 过滤现在都跳过 `__pycache__`、`.pyc`、`.pyo`，避免 Python 解释器缓存污染 `modified_paths`。
- guard ignore 现在还会跳过 `.pytest_cache`，避免测试运行副产物被误判成 repo 越权改动。
- execution guard 的硬失败主语义已经从“repo 内超出 `owned_paths` 就失败”改成“只对真正的 `target_roots` 越界 hard fail”；repo 内 `owned_paths` 偏移现在会记录成 `scope_drift`，保留证据但不再直接把 authoritative `WorkerRecord.status` 改成 `FAILED`。
- 当 `WorkerRecord` 已经带有 protocol-first 的 authoritative `final_report.verification_results` 时，guard 现在先消费这份 evidence，并按 command equivalence 做匹配；只有 authoritative evidence 缺失或不等价时，才会在 working dir 下 rerun `verification_commands`。
- 对于 teammate 自己因为环境约束而改用 fallback 命令执行验证的情况，authoritative result 现在可以通过 `requested_command` 显式声明它满足的是哪条 required verification intent；guard 会优先按这层映射匹配，而不是强迫 worker 只能回报字面完全一致的 `command`。
- 如果同一 required command 出现多条等价 `verification_results`，主线现在会优先选择最后一条成功结果；如果没有成功结果，再选择最后一条等价结果。这样像 `uv run -> patched uv run -> .venv/bin/python -m pytest` 这种 fallback 链，不会再因为第一条失败尝试而把整次 run 判成失败。
- 这些修复已经由新的 guard 回归测试覆盖：
  - `test_group_runtime_ignores_preexisting_spool_root_artifacts_when_diffing_snapshots`
  - `test_group_runtime_normalizes_owned_paths_through_repository_symlinks`
  - `test_group_runtime_ignores_python_cache_artifacts`
  - `test_group_runtime_ignores_pytest_cache_artifacts`
  - `test_group_runtime_records_scope_drift_without_failing_assignment`
  - `test_group_runtime_fails_when_modified_path_escapes_explicit_target_roots`
  - `test_group_runtime_accepts_equivalent_authoritative_protocol_verification_results`
  - `test_group_runtime_accepts_authoritative_requested_command_mapping`
  - `test_group_runtime_reruns_verification_when_authoritative_result_is_not_equivalent`
  - `test_supervisor_prefers_last_successful_equivalent_required_verification_result`
  - `test_supervisor_uses_last_equivalent_required_verification_result_when_all_fail`

补充：2026-04-09 又用一轮 custom replay 直接回放了 historical round-2 的 4 条 lane，结果说明这条修复已经改变了 live failure shape，而不是只停留在单测层面：

- historical 原始 round-2 中：
  - `team-primary-semantics-switch`
  - `coordination-transaction-and-session-truth-convergence`
  - `task-surface-authority-contract`
  都是 `leader completed + teammate failed + lane blocked`
- custom replay after guard fix 中，这 3 条 lane 已全部转成 `delivery_status=completed`
- 剩余唯一阻断 lane 变成了 `superleader-isomorphic-runtime`
- 该 lane 的阻断原因也不再是 execution guard / verification adjudication 误判，而是 `codex_cli` worker 的真实 transport/provider failure：
  - `chatgpt authentication required to sync remote plugins`
  - `403 Forbidden`
  - `stream disconnected - retrying sampling request`
  - 最终没有 `last_message.txt`，terminal report 为 `failed`

这轮 live replay 还有一个值得保留的裁决信号：`superleader-isomorphic-runtime` 的 leader assignment 曾同时出现过成功与失败的等价 attempt result，但 authoritative round summary 最终保留了 `leader_record_statuses=["completed"]`，同时把真实失败留在 teammate record 上。这正是“优先最后一条成功等价结果”的 adjudication 在 live path 生效的直接证据，说明当前 residual blocker 已经切换成 backend/provider 稳定性，而不是旧的 guard 假失败。

## 8. 相关文档

- [implementation-status.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/implementation-status.md)
- [remaining-work-backlog.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/remaining-work-backlog.md)
- [parallel-team-coordination.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/parallel-team-coordination.md)
