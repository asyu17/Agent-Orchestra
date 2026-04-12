# agent_orchestra Self-Hosting Bootstrap

## 1. 一句话结论

截至 2026 年 4 月 8 日，`self_hosting.bootstrap` 已经从“给现有 runtime 套一层脚本”升级成真正的自举入口：它会从知识库读取当前优先级、生成 `ObjectiveTemplate` 或 dynamic seeds、构造 profile-first 的 `SuperLeaderConfig`、执行一轮 `SuperLeaderRuntime`，并导出下一轮 instruction packet。方案 2 完成后，这条链已经可以在真实 `codex_cli` 主线上消费 protocol-capable role profiles，而不是继续依赖旧的 backend-name + timeout tuning 组合；本轮又把 `multi-leader-planning-review` 正式接进了 gap inventory 和 packet 主线，但这条 planning-review 路径已经不再由 selected gap 决定是否开启，而是 superleader/self-hosting 默认主路径，packet 也会回写 `planning_review_status / activation_gate`。

## 2. 范围与资料来源

- 当前代码：
  - [bootstrap.py#L690](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/self_hosting/bootstrap.py#L690)
  - [bootstrap.py#L465](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/self_hosting/bootstrap.py#L465)
  - [bootstrap.py#L842](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/self_hosting/bootstrap.py#L842)
  - [leader_loop.py#L166](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py#L166)
  - [superleader.py#L297](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/superleader.py#L297)
- 相关测试：
  - [test_self_hosting_round.py#L178](/Volumes/disk1/Document/code/Agent-Orchestra/tests/test_self_hosting_round.py#L178)
  - [test_self_hosting_round.py#L202](/Volumes/disk1/Document/code/Agent-Orchestra/tests/test_self_hosting_round.py#L202)
  - [test_self_hosting_round.py#L287](/Volumes/disk1/Document/code/Agent-Orchestra/tests/test_self_hosting_round.py#L287)
  - [test_self_hosting.py#L1](/Volumes/disk1/Document/code/Agent-Orchestra/tests/test_self_hosting.py#L1)
- 依赖知识：
  - [implementation-status.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/implementation-status.md)
  - [current-priority-gap-list.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/current-priority-gap-list.md)

## 3. 这层解决了什么问题

在这层之前，系统虽然已经能跑 `SuperLeader -> Leader -> Teammate`，但还需要人手工指定：

- 下一轮 objective 是什么
- 哪些 gap 值得优先做
- 跑完一轮后下一轮应该继续什么

`self_hosting.bootstrap` 把这三件事串成了固定入口，因此它已经不只是“再跑一次 runtime”，而是“从知识到下一轮任务”的生成器。

## 4. 核心能力

### 4.1 从知识库读取权威 gap inventory

`load_runtime_gap_inventory(...)` 默认读取 [implementation-status.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/implementation-status.md) 的“建议优先级”段落，并把编号条目映射成 `SelfHostingGap`：[bootstrap.py#L690](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/self_hosting/bootstrap.py#L690)

现在 bootstrap catalog 不只覆盖 `team-parallel-execution / authority-integration / protocol-bus` 这些老 gap，也已经能识别 `multi-leader-planning-review`。这意味着：

- bootstrap 可以继续把它作为 targeted validation / inventory 项来排序和回写
- 但 planning-review 自身已经不再依赖某个 selected gap 才开启
- 也就是即使这轮 objective 主要目标不是 `multi-leader-planning-review`，superleader 仍会先跑 `draft -> peer review -> global review -> revision -> activation gate`

这意味着后续如果要改变 self-hosting 的下一轮目标，优先更新知识文档，而不是散落地改脚本常量。

2026-04-09 又补了一条必须固定下来的规则：

- `team-primary-semantics-switch`
- `coordination-transaction-and-session-truth-convergence`
- `task-surface-authority-contract`
- `superleader-isomorphic-runtime`

这 4 个 first-batch 在线协作 gap 现在已经是 bootstrap catalog 的正式条目，不再允许只靠 slug fallback 命中。每个条目都必须同时携带：

- 代码热点 `owned_paths`
- 对应的知识文档 scope
- 最低验证命令

同时，lane-level default scope 现在会沿着 `SelfHostingGap -> WorkstreamTemplate.metadata -> LeaderTaskCard.metadata -> leader prompt / leader ingest` 这条链透传：

- leader prompt 会显式看到 gap id、rationale、source_path、default owned paths、default verification commands
- descendant team task 在 slice 未显式声明时，会自动继承 lane-owned scope，避免 teammate 因“需要知识固化但知识文档不在授权范围内”而直接 blocked

但这里有一个新边界也必须固定：

- default `verification_commands` 继承不是全局打开
- 当前只对这 4 个 first-batch gap 启用
- 旧的 validation/self-hosting gap 仍保持原行为，避免把 team-parallel/delegation 这类 fake validation lane 意外提升成“teammate 必须跑真实全量验证”的错误主语义

### 4.2 把 gap inventory 变成 template 或 dynamic seeds

bootstrap 现在可以：

- 生成显式 `ObjectiveTemplate`
- 或在 `use_dynamic_planning=True` 时只生成 `dynamic_workstream_seeds`

对应入口在 [bootstrap.py#L740](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/self_hosting/bootstrap.py#L740)。因此 runtime 既支持 deterministic template mode，也支持 bounded dynamic planning mode。

### 4.3 profile-first 的 superleader 配置

`build_self_hosting_superleader_config(...)` 是这轮最重要的收敛点：

- 当 `leader_backend` 或 `teammate_backend` 是 `codex_cli` 时，它会直接生成 contract-owned `WorkerRoleProfile`：[bootstrap.py#L465](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/self_hosting/bootstrap.py#L465)
- `leader_idle_timeout_seconds / leader_hard_timeout_seconds` 现在不只覆盖 fallback safety rail，还会通过同一 role-profile timeout source 同步覆盖 protocol-native lease timeout：也就是 `fallback_idle_timeout_seconds / fallback_hard_timeout_seconds` 与 `lease_policy.renewal_timeout_seconds / hard_deadline_seconds` 会一起对齐，而不是再出现“fallback 已放宽，但 protocol lease 仍在旧值上先杀进程”的分叉：[bootstrap.py#L469](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/self_hosting/bootstrap.py#L469), [leader_loop.py#L223](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py#L223)
- 从 2026-04-05 起，self-hosting 主线会把显式传入的 leader codex timeout override 同步覆盖到 `teammate_codex_cli_code_edit`；到 2026-04-06，这条覆盖又向下收口到了 role profile 的 lease/fallback 双层，因此真实 codex teammate 不会再悄悄退回默认 `120 / 2400` 秒 safety rail，也不会再保留旧 `renewal_timeout_seconds = 120` 的 protocol-native kill path；未显式传入 override 时，leader/teammate 仍保持 runtime role profile 的默认统一超时源。
- `codex_cli` leader 默认关闭 `keep_leader_session_idle`，因为 backend 当前并不支持 reactivation：[bootstrap.py#L500](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/self_hosting/bootstrap.py#L500)
- 现在 `build_self_hosting_superleader_config(...)` 会固定产出 `enable_planning_review=True` 的 `SuperLeaderConfig`，也就是 self-hosting 不再保留旧的“默认关闭 planning-review”路径。

这条设计的直接验证是 [test_self_hosting_round.py#L178](/Volumes/disk1/Document/code/Agent-Orchestra/tests/test_self_hosting_round.py#L178)

### 4.4 instruction packet 现在直接导出 contract-owned role profiles

instruction packet 不再导出 runtime 自己包的一层 wrapper，而是直接导出 contract-layer `WorkerRoleProfile` 形状：[bootstrap.py#L949](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/self_hosting/bootstrap.py#L949)

对应验证见 [test_self_hosting_round.py#L202](/Volumes/disk1/Document/code/Agent-Orchestra/tests/test_self_hosting_round.py#L202)

### 4.5 instruction packet 现在还会导出 planning review / activation gate 状态

现在 instruction packet 不再只会告诉下一轮“哪些 gap 做完了”，还会把当前 objective 的 planning-review live truth 摘出来：

- `planning_review_status`
- `planning_review.activation_gate`
- 顶层 `activation_gate`

对应主语义是：

- `ActivationGateDecision` 已经不只是 store 里的 first-cut 记录
- runtime 会真实发布并持久化 gate decision
- bootstrap 会把它作为 self-hosting packet 的一等状态导出

### 4.6 自举 leader 已经能走 protocol-native codex 主线

这轮切换后，自举 `codex_cli` leader 的 record 已经能直接反映：

- `protocol_wait_mode == "native"`
- `metadata.final_report.terminal_status == "completed"`

对应回归见 [test_self_hosting_round.py#L287](/Volumes/disk1/Document/code/Agent-Orchestra/tests/test_self_hosting_round.py#L287)

## 5. 现在离“接近自举”还有多远

当前这层已经能做到：

- 从知识库推导下一轮优先 gap
- 自动生成 objective template
- 用 profile-first 配置跑一轮 `SuperLeaderRuntime`
- 导出下一轮 instruction packet
- 在真实 `codex_cli` 主线上消费 protocol-capable leader/teammate profile

但它还没有完全进入“自主持续开发”状态，主要剩余约束是：

- `durable-supervisor-sessions`
- `reconnector`
- `protocol-bus`
- `permission-broker`
- `planner-feedback`
- `sticky-provider-routing`
- `postgres-persistence`

也就是说，现在缺的已经不是“bootstrap 会不会生成错配置”，而是“底层 runtime/control-plane 还不够 durable/adaptive”。

## 6. 操作性规则

关于 bootstrap 这层，有两条规则需要固定下来：

1. `implementation-status.md` 的“建议优先级”是默认权威输入源。
2. `leader_idle_timeout_seconds / hard_timeout_seconds` 在 profile-first 模型里不应再只被理解成旧 fallback watchdog 旋钮；当前主线要求它们成为 codex role profile 的统一 timeout source，同时驱动 fallback timeout 与 protocol-native lease timeout。也就是说，如果 self-hosting 显式传入这组 codex timeout override，leader 与 teammate codex profile 必须同时对齐 `fallback_*` 和 `lease_policy.*`，避免主线运行时再出现“idle/hard 已放宽，但 renewal_timeout 仍是旧值”的隐性 kill path。
3. `leader-teammate-delegation-validation` 与 `team-parallel-execution` 这两类 self-hosting gap 的关闭条件，当前主语义是 `teammate_execution_evidence`，也就是 teammate 必须在无新增上游 prompt turn 的条件下完成 claim、执行、结果回写，以及 cursor/session 提交；`leader_consumed_mailbox` 只保留为诊断性 metadata。
4. `multi-leader-planning-review` 的关闭条件，当前主语义是 objective live truth 中的 `activation_gate.status == "ready_for_activation"`；如果 gate 仍是 `needs_replan / needs_authority / needs_project_item_promotion / blocked`，bootstrap 不应把这个 gap 记成已完成。
5. first-batch 4 个在线协作 gap 的 descendant task 默认 scope 现在必须自动带上 lane-owned knowledge docs；否则 teammate 一旦按 `AGENTS.md` 进行知识固化，就会因为 scope 缺失被错误阻断。
6. first-batch 4 个在线协作 gap 的 default `verification_commands` 可以作为 descendant task 的缺省验证集；但这条继承当前只属于这些 gap，不应无差别扩散到 `team-parallel-execution`、`leader-teammate-delegation-validation` 这类 validation lane。

### 6.1 2026-04-06 round-2 暴露出的收尾阻断模式与 2026-04-08 修复

这轮 `obj-agent-orchestra-top-5-gaps-2026-04-06-round-2` 又补出了一条值得固定的规则：

- 如果某个 teammate assignment 自己返回了 `final_report.terminal_status = "blocked"`，`codex_cli_backend` 当前会把 raw worker result 直接归一成 `status = "failed"`，即使进程 `exit_code == 0` 且已经成功产出 terminal report：
  - [codex_cli_backend.py#L381](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/backends/codex_cli_backend.py#L381)
- `GroupRuntime.commit_teammate_result(...)` 又会把所有 `record.status != COMPLETED` 的 teammate task 统一写成 `TaskStatus.FAILED`：
  - [group_runtime.py#L1609](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/group_runtime.py#L1609)
- `DefaultDeliveryEvaluator.evaluate_lane(...)` 会把 `TaskStatus.FAILED` 与 `TaskStatus.BLOCKED` 一起视为 lane blocker，进而让 leader lane 进入 `DeliveryStatus.BLOCKED`：
  - [evaluator.py#L74](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/evaluator.py#L74)
  - [leader_loop.py#L1708](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py#L1708)

到这里为止，lane 被判成 `BLOCKED` 是符合当前代码语义的；真正会让 self-hosting round 看起来“没有收尾”的，原来是后半段 superleader resident loop：

- 下游 lane 的依赖判断现在只接受“依赖 lane 已 `COMPLETED`”，不会把 `BLOCKED` 当成可终态：
  - [superleader.py#L232](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/superleader.py#L232)
- 当上游 lane 被阻断、下游 lane 还处于 `pending_lane_ids` 时，旧实现会返回 `WAITING_FOR_DEPENDENCIES` 但不设置 `stop=True`；2026-04-08 之后，这类 dead-end pending graph 已改为直接 stop+finalize，并会在 coordinator metadata 中额外写出 `deadlocked_pending_lane_ids / deadlocked_waiting_on_lane_ids` 作为诊断证据：
  - [superleader.py#L656](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/superleader.py#L656)
- `ResidentCoordinatorKernel.run(...)` 只有在 `cycle_result.stop` 为真时才会进入 `finalize(...)`，因此 round report / instruction packet 不会写盘：
  - [resident_kernel.py#L27](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/resident_kernel.py#L27)
- 同一轮修复还把 finalize 的 objective evaluate 输入从“只看 `lane_results`”改成“消费全量 lane state”，也就是仍未启动或仍处于 pending 的 lane 不会在最终 objective 判定里被静默丢掉：
  - [superleader.py#L1381](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/superleader.py#L1381)

这条 failure pattern 现在的诊断结论应该固定为：

- 如果 lane 已经出现明确 `BLOCKED` / `FAILED`，但 self-hosting round 仍没有最终 report，先不要怀疑 codex transport 卡死。
- 先看这轮是否还保留着依赖这个 lane 的 `pending_lane_ids`，以及 coordinator metadata 里有没有 `deadlocked_pending_lane_ids`。
- 如果是，那么根因通常不是“worker 没结束”，而是“superleader finalize/live state 之间又出现了新的协调断层”。
- 如果 objective 最终状态看起来比 lane graph 更乐观，例如只剩一条已完成 lane 却把 objective 判成 `WAITING/COMPLETED`，优先检查 finalize 是否真的消费了全量 lane state，而不是只消费了 `lane_results`。

这条 failure pattern 后来已经被 authority mainline 吸收，不再只是“把 finalize 条件补严”：

- 对这类“worker 自己返回 `blocked`，原因是修复它需要额外 scope/authority”的场景，系统后来已经补入 `authority-escalation-and-scope-extension` 的完整 decision + closure mainline。
- 也就是 teammate 现在可以提交 `AuthorityRequest / ScopeExtensionRequest` 并进入 `WAITING_FOR_AUTHORITY`；leader 已能做 `grant / deny / reroute / escalate`，superleader 已能对 escalated request 做 `grant / deny / reroute` 并显式回写 subordinate leader，runtime 也已支持 `reroute` commit，而不是继续让 `blocked teammate -> raw failed -> task failed -> lane blocked` 成为唯一链路；self-hosting 也已改为消费结构化 `authority_completion`，并能区分 `waiting / grant_resumed / reroute_closed / deny_closed / relay_pending / incomplete`。
- 这样 finalize 也才能正式区分：
  - authority pending
  - authority denied / hard blocked
  - 真实 execution failure

这轮运行产物里最直接的样本是：

- blocked teammate assignment：
  - [task_0714d29d057a:teammate-turn-1.result.json](/Volumes/disk1/Document/code/Agent-Orchestra/docs/superpowers/runs/codex-spool/task_0714d29d057a:teammate-turn-1.result.json)
  - [task_0714d29d057a:teammate-turn-1.last_message.txt](/Volumes/disk1/Document/code/Agent-Orchestra/docs/superpowers/runs/codex-spool/task_0714d29d057a:teammate-turn-1.last_message.txt)
- 当前 round 的依赖顺序说明：
  - [2026-04-06-agent-orchestra-top-5-gap-launch-brief.md](/Volumes/disk1/Document/code/Agent-Orchestra/docs/superpowers/runs/2026-04-06-agent-orchestra-top-5-gap-launch-brief.md)

## 7. 最有价值的后续入口

如果下一步继续推进自举，优先进入：

1. [bootstrap.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/self_hosting/bootstrap.py)
2. [worker_supervisor.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/worker_supervisor.py)
3. [leader_loop.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/leader_loop.py)
4. [superleader.py](/Volumes/disk1/Document/code/Agent-Orchestra/src/agent_orchestra/runtime/superleader.py)
5. [implementation-status.md](/Volumes/disk1/Document/code/Agent-Orchestra/resource/knowledge/agent-orchestra-runtime/implementation-status.md)
