# Agent Orchestra 全量上下文自动保存与 Hydration 方案

## 1. 一句话结论

Agent Orchestra 不应把 Claude team agent 的 transcript-first 恢复原样照搬成自己的唯一真相层；更合理的路线是继续保持 `runtime-first` 主线，同时在其上补一层结构化的 `turn ledger + artifact ledger + semantic memory + hydration bundle`，让系统既保住 host-owned runtime truth，又获得更接近 Claude 的长会话恢复体验。

## 2. 范围与资料来源

- 现有 AO 连续性与常驻 shell 设计：
  - `docs/superpowers/specs/2026-04-10-agent-orchestra-session-continuity-and-forking-design.md`
  - `docs/superpowers/specs/2026-04-11-agent-orchestra-resident-team-shell-and-attach-first-design.md`
  - `resource/knowledge/agent-orchestra-runtime/session-continuity-and-runtime-branching.md`
  - `resource/knowledge/agent-orchestra-runtime/resident-team-shell-and-attach-first.md`
- 现有 AO 代码入口：
  - `src/agent_orchestra/contracts/session_continuity.py`
  - `src/agent_orchestra/contracts/agent.py`
  - `src/agent_orchestra/contracts/execution.py`
  - `src/agent_orchestra/runtime/session_continuity.py`
  - `src/agent_orchestra/runtime/group_runtime.py`
  - `src/agent_orchestra/runtime/session_host.py`
  - `src/agent_orchestra/runtime/worker_supervisor.py`
  - `src/agent_orchestra/runtime/leader_loop.py`
  - `src/agent_orchestra/runtime/teammate_work_surface.py`
  - `src/agent_orchestra/runtime/superleader.py`
  - `src/agent_orchestra/storage/base.py`
  - `src/agent_orchestra/storage/postgres/models.py`
  - `src/agent_orchestra/storage/postgres/store.py`
- Claude 对照材料：
  - `resource/knowledge/claude-code-main/agent-team.md`
  - `resource/knowledge/claude-code-main/runtime-and-ui.md`
  - `resource/others/claude-code-main/src/utils/conversationRecovery.ts`
  - `resource/others/claude-code-main/src/utils/sessionStorage.ts`
  - `resource/others/claude-code-main/src/utils/swarm/reconnection.ts`
  - `resource/others/claude-code-main/src/utils/swarm/teamHelpers.ts`

下面涉及 Claude 与 AO 的差异判断时，如果不是源码直接陈述的事实，会显式写成“判断”。

## 3. 当前事实与缺口

2026-04-11 夜间，这条线已经不再只是设计稿，first-cut 已有明确代码落点：

- `src/agent_orchestra/contracts/session_memory.py`
- `src/agent_orchestra/runtime/session_memory.py`
- `src/agent_orchestra/storage/base.py`
- `src/agent_orchestra/storage/in_memory.py`
- `src/agent_orchestra/storage/postgres/models.py`
- `src/agent_orchestra/storage/postgres/store.py`
- `src/agent_orchestra/runtime/session_continuity.py`
- `src/agent_orchestra/runtime/group_runtime.py`

当前已落地的 first-cut 包括：

- `AgentTurnRecord / ToolInvocationRecord / ArtifactRef / SessionMemoryItem / HydrationBundle` 已进入正式 contracts
- `ConversationHead` 已补入 `checkpoint_id / prompt_contract_version / toolset_hash / contract_fingerprint`
- in-memory / PostgreSQL store 都已能持久化 turn/tool/artifact/memory 四类实体
- `SessionMemoryService` 已能从 worker result 产出 turn ledger、verification command tool records、`final_report / protocol_events / protocol_state / generated_file` artifact refs，以及 handoff/open-loop semantic memory item
- `SessionInspectSnapshot` 现在会导出 `hydration_bundles`
- hydration bundle 已具备第一版 retention/compaction/redaction：默认只保留有限条 turn/tool/artifact/memory，并支持通过既有 metadata 覆盖；hydration prompt 会压缩路径与敏感标记
- inspect/CLI surfaces 会导出每个 scope 的 hydration coverage（turn/tool/artifact/memory 计数、ready/prompt-ready）
- `apply_assignment_continuity(...)` 在缺失 `last_response_id` 时，已经会把 `HydrationBundle` 注入 assignment metadata，并把可读 hydration prompt 前置到 `input_text`
- `GroupRuntime.run_worker_assignment(...)` 现在不再只在成功完成时保留 continuity summary；worker result 都会进入 session-memory capture，从而覆盖 interrupted / failed / degraded-resume 所需证据面
- `leader_loop.py` 已把 leader prompt turn 与 mailbox drain 接进 role-scoped turn/tool/artifact ledger：prompt turn 会记录 `leader_decision`，leader mailbox consume / authority-reactor consume 会记录 `mailbox_followup`、`mailbox_commit` 与 `mailbox_snapshot`
- `teammate_work_surface.py` 已把 directed/autonomous claim、authority request / control-envelope consume、以及 teammate result publish 接进 slot-scoped turn ledger；authority request 还会附带 `authority_action` tool record 与 hydration-facing artifact
- `superleader.py` 已把 planning review、cycle-level resident live-view decision、以及 finalize objective summary 接进 `superleader_decision` turn ledger，并把 objective resident live-view snapshot 落成 `delivery_snapshot`
- `session_host.py` 已把 `build_shell_attach_view(...)` 与 `record_resident_shell_approval(...)` 接进 host-owned session-memory capture：attach read model 会产出 `leader_decision` turn 与 `hydration_input` artifact，approval queue 更新会产出 `protocol_tool`
- `SessionMemoryService.record_role_turn(...) / upsert_conversation_head(...)` 现已成为非 worker role capture 的共享 helper，因此即便没有新的 provider `response_id`，leader / teammate / superleader / session-host 事件也能补齐 turn ledger 并维持可 hydration 的 conversation head

当前 AO 已经有：

- `WorkSession`
- `RuntimeGeneration`
- `ConversationHead`
- `ContinuationBundle`
- `ResidentTeamShell`
- `ResidentSessionHost`
- `AgentSession`
- `WorkerSession`

这意味着系统已经能保存：

- 用户可见的 session root
- runtime generation lineage
- per-role `last_response_id`
- checkpoint summary
- shell attach / recover / warm resume 的第一层语义

但它仍然没有完全保存：

- 每个 role scope 逐轮发生了什么
- 哪些工具调用和哪些 artifact 对下一轮有意义
- 哪些事实/决策/未完成问题应该作为长期记忆留下
- 一个足够厚的 hydration 输入包

当前 `ConversationHead` 和 `ContinuationBundle` 仍然偏薄，但已经不再是唯一的恢复面：

- `ConversationHead` 主要是 provider continuity bridge
- `ContinuationBundle` 主要是 summary-first inspect/read model

它们现在已经开始与 `HydrationBundle` 并行存在；而且 2026-04-12 这层 richer capture 已经补到 leader / teammate / superleader / session-host 主路径。当前更准确的状态不再是“只有 worker-led first-cut”，而是“role-scoped turn/artifact hydration 已进入主线，剩余缺口主要收缩到 semantic-memory extraction hardening、retention/compaction 细化，以及 broader single-owner continuation cleanup”。

## 4. 推荐的四层 full-context 自动保存模型

### 4.1 Layer 1: Turn Ledger

这是“显式轮次轨迹”层。

它应保存：

- 这一轮是谁触发的
- 输入摘要是什么
- 输出摘要是什么
- 对应的 `response_id`
- 这轮是完成、失败、中断，还是 partial

判断：

- 这层类似 Claude transcript 的一部分能力
- 但 AO 不应继续把它做成自由文本 transcript，而应做成结构化 record

### 4.2 Layer 2: Artifact Ledger

这是“大的、有引用价值的上下文对象”层。

它应保存：

- final report ref
- protocol state/events ref
- 关键 patch/file/report ref
- delivery/mailbox/blackboard/task-surface snapshot ref

规则：

- 不把大对象一股脑塞进 `WorkSessionMessage`
- 不把 `ConversationHead` 变成大 JSON 挂载点

### 4.3 Layer 3: Semantic Memory

这是“从 turn ledger 提炼出的长期记忆”层。

它应保存：

- 已确认的事实
- 已做出的决策
- 当前约束
- 仍未关闭的问题
- handoff 摘要
- artifact 的语义摘要

判断：

- 这是 AO 想更接近 Claude 的长会话体验时最关键的一层
- 没有它，warm resume 只能吃 summary 和 `last_response_id`，会越来越脆

### 4.4 Layer 4: Hydration Bundle

这是“恢复时真正喂给下一轮”的聚合层。

它应组合：

- `ConversationHead`
- 最近若干条 turn ledger
- 相关 tool / artifact refs
- semantic memory
- runtime status summary
- mailbox / blackboard / delivery / authority summary
- shell attach summary

规则：

- `HydrationBundle` 是 read model，不是 primary write model
- 它由 runtime truth + session memory 生成，不反向定义 runtime truth

## 5. 新增数据契约建议

建议新建：

- `src/agent_orchestra/contracts/session_memory.py`

至少包含：

- `AgentTurnRecord`
- `ToolInvocationRecord`
- `ArtifactRef`
- `SessionMemoryItem`
- `HydrationBundle`

同时建议对现有对象补强：

- `ConversationHead`
  - 增加 `checkpoint_id`
  - 增加 `prompt_contract_version`
  - 增加 `toolset_hash`
  - 增加 `contract_fingerprint`

判断：

- 现有 `backend / provider / model` 失效规则还不够
- prompt/tool contract 变化也应触发 continuity 失效

## 6. 运行时捕获点建议

### 6.1 Worker / Assignment 主链

最重要的捕获点在：

- `GroupRuntime.run_worker_assignment(...)`
- `DefaultWorkerSupervisor`

这里应至少捕获：

- assignment 输入摘要
- continuity 输入
- `previous_response_id`
- result 摘要
- `response_id`
- `final_report`
- verification 结果

### 6.2 Leader 主链

在 `leader_loop.py` 里应捕获：

- leader prompt 组装后的输入摘要
- leader 输出解析摘要
- slice / activation / follow-up 决策
- mailbox follow-up turn

### 6.3 Teammate 主链

在 `teammate_work_surface.py` 里应捕获：

- directed claim
- autonomous claim
- result publish
- authority request / decision
- idle / attached / quiescent 转移

### 6.4 SuperLeader 主链

在 `superleader.py` 里应捕获：

- planning review synthesis
- activation gate
- lane finalize / rebalance
- resident live-view 驱动的关键决策

### 6.5 Host-owned commit 主链

最稳的 capture 点其实是 authoritative commit family：

- mailbox consume
- teammate result commit
- authority request / decision commit
- task / blackboard / delivery coordination commit
- worker session host projection refresh

规则：

- 优先跟着 authoritative transition 产出 memory
- 不要在外围再拼一套 competing truth

## 7. Hydration / Attach / Warm Resume 主链路

### 7.1 Attach

优先级仍应是：

1. attach
2. recover / exact wake
3. warm resume

在 attach 成功时：

- runtime truth 仍是主角
- hydration 主要承担诊断和 UI/inspect 价值

### 7.2 Exact Wake

`exact_wake` 仍然是内部 recover path：

- reclaim lease
- backend `reattach(...)`
- refresh host projection
- 回到 attach

它不应被 session-memory 层替代。

### 7.3 Warm Resume

真正需要新方案的是 warm resume：

1. 新建 `RuntimeGeneration`
2. 复制兼容的 `ConversationHead`
3. 为 leader / teammate / worker scope 构造 `HydrationBundle`
4. 用 `last_response_id + checkpoint_summary + recent turns + semantic memory + artifact refs + runtime summaries` 启动下一轮

这条链路才是 AO 变得更接近 Claude 长会话感的关键。

### 7.4 Fork Session

fork 时应显式保留：

- semantic memory
- 选定的 artifact refs
- continuity checkpoint

但不应保留：

- live mailbox cursor
- live worker ownership
- live lease ownership

## 8. 与 Claude team agent 的映射关系

Claude 的真实恢复机制更接近：

- transcript JSONL
- TeamFile
- sidecar metadata
- 恢复时临时拼装的 message chain 和 team context

判断：

- Claude 是 `conversation-first, rehydrate-late`
- AO 更适合 `runtime-first core + structured hydration surface`

最准确的映射是：

- Claude transcript
  - 最接近 AO 的 `WorkSessionMessage` 输入面和 turn ledger 灵感来源
- Claude resume patch-up
  - 最接近 AO 的 `ConversationHead + ContinuationBundle / HydrationBundle`
- Claude TeamFile
  - 只对应 AO 的一部分 resident shell / roster 启发
  - 不能替代 `ResidentSessionHost + AgentSession + WorkerSession + DeliveryState`

不应直接照搬的点：

- transcript 作为唯一真相
- TeamFile 作为 authoritative runtime ledger
- tool invocation 全部内嵌在 transcript
- `AppState.teamContext` / `InProcessTeammateTaskState` 这类 UI/runtime 混合对象

## 9. 实施顺序

建议顺序：

1. 先把 PostgreSQL 固定成 durable baseline
2. 新增 `session_memory.py` 契约
3. 补 store API、in-memory、Postgres schema/store
4. 先接 worker-level capture
5. 再接 leader / teammate / superleader capture
6. 引入 semantic memory 提炼
7. 把 `ContinuationBundle` 扩成 `HydrationBundle` 主链
8. 把 `attach / warm_resume / inspect` 都接到 hydration read model
9. 最后再做 retention、compaction、redaction

其中截至 2026-04-11，前 1-4、7-8 已进入 first-cut 主线；真正还没完成的主要是 5-6 和 9。

## 10. 非目标与注意事项

- 不是做进程级完整内存快照
- 不是保存 hidden CoT
- 不是让 transcript 反过来做 runtime owner
- 不是让 `ResidentTeamShell` 变成新的上下文仓库
- 不是把 detached external wake、planner feedback、sticky provider routing 混进这次方案

另外一个重要注意事项：

- interrupted / partial turn 也必须进入 turn ledger
- 不能只记录“成功完成”的 turn

否则 warm resume 仍然会丢掉最关键的上下文。

## 11. 相关文档

- `docs/superpowers/specs/2026-04-10-agent-orchestra-session-continuity-and-forking-design.md`
- `docs/superpowers/specs/2026-04-11-agent-orchestra-resident-team-shell-and-attach-first-design.md`
- `docs/superpowers/specs/2026-04-11-agent-orchestra-full-context-session-memory-and-hydration-design.md`
- `resource/knowledge/agent-orchestra-runtime/session-continuity-and-runtime-branching.md`
- `resource/knowledge/agent-orchestra-runtime/resident-team-shell-and-attach-first.md`
