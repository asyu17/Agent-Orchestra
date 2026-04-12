# Agent Orchestra 会话连续性与运行时代际分层

## 1. 一句话结论

Agent Orchestra 不应把 Claude Code 式 transcript resume 直接照搬成自己的单一真相层；更合理的长期主语义是把“用户可见会话 root”和“runtime durable truth”拆开：`WorkSession` 负责用户连续性，`RuntimeGeneration` 负责一次具体执行代际，`ConversationHead` 负责每个 `superleader / leader / teammate` 角色的 provider-native continuity，而真正的执行真相仍然继续留在 `ResidentSessionHost + AgentSession + WorkerSession + DeliveryState` 这组 host-owned runtime read/write model 里。

## 2. 范围与资料来源

- Agent Orchestra 当前代码与知识：
  - `src/agent_orchestra/contracts/agent.py`
  - `src/agent_orchestra/contracts/execution.py`
  - `src/agent_orchestra/contracts/runner.py`
  - `src/agent_orchestra/runtime/session_host.py`
  - `src/agent_orchestra/runtime/worker_supervisor.py`
  - `src/agent_orchestra/runtime/leader_loop.py`
  - `src/agent_orchestra/runtime/group_runtime.py`
  - `resource/knowledge/agent-orchestra-runtime/implementation-status.md`
  - `resource/knowledge/agent-orchestra-runtime/active-reattach-and-protocol-bus.md`
  - `resource/knowledge/agent-orchestra-runtime/online-collaboration-runtime-migration.md`
- Claude Code 参考：
  - `resource/knowledge/claude-code-main/agent-team.md`
  - `resource/knowledge/claude-code-main/runtime-and-ui.md`
  - `resource/others/claude-code-main/src/utils/conversationRecovery.ts`
  - `resource/others/claude-code-main/src/utils/sessionStorage.ts`
  - `resource/others/claude-code-main/src/utils/swarm/reconnection.ts`
  - `resource/others/claude-code-main/src/utils/swarm/teamHelpers.ts`
- 对应正式设计文档：
  - `docs/superpowers/specs/2026-04-10-agent-orchestra-session-continuity-and-forking-design.md`

下面出现的“判断”是基于上述代码与文档的架构推断，不是说当前仓库已经全部实现了这些对象。

## 3. 为什么当前系统还缺一层

当前 runtime 已经能稳定表达很多 continuation truth：

- `ACTIVE` worker 的 durable truth
- reclaim + `reattach(...)`
- host-owned `ResidentSessionHost` projection
- leader / teammate resident continuation
- assignment-level `previous_response_id`

但这些 truth 还主要停留在执行层，而不是用户可感知的会话层。

系统现在缺的是下面这几个产品级语义：

- “继续刚才那个会话”到底指什么
- “继续同一轮执行”与“重新起一代 runtime 继续同一目标”有什么区别
- “从当前状态 fork 一个新分支”与“彻底新开会话”有什么区别
- leader / teammate 的 provider-native continuity 应该如何持久化，何时必须失效

如果没有单独的会话层，这些动作就容易被误折叠成：

- resume
- retry
- relaunch
- restart

看起来像一回事，但真实语义并不一样。

## 4. Claude Code 给出的真正启发

Claude Code 的参考价值不在于“直接复刻它的 team file”。

真正值得复用的是它把两件事分开了：

1. 主会话 resume
   - 以 transcript / resume flow 为用户入口
2. team/swarm runtime persistence
   - 以 `TeamFile + mailbox + reconnection` 维持 roster 与 teammate 恢复

判断：

- Claude Code 的主强项是 conversation-first UX
- Agent Orchestra 的主强项是 runtime-first durable coordination

因此 Agent Orchestra 最合理的路线不是向任一侧完全倒过去，而是做显式分层：

- UX 会话层学 Claude
- runtime 真相层保持自己现有主线

## 5. 推荐分层模型

### 5.1 `WorkSession`

`WorkSession` 是用户可见的顶层会话 root。

它回答的问题是：

- 我现在正在延续哪一条会话
- 这是原会话、fork 分支，还是全新会话
- 当前默认关联的 runtime generation 是哪一个

### 5.2 `RuntimeGeneration`

`RuntimeGeneration` 是某个 `WorkSession` 下的一次具体执行代际。

它回答的问题是：

- 现在活着的是哪一代 runtime
- 这是 fresh start、exact wake，还是 warm resume
- 这一代 runtime 从哪一代派生而来

这层的关键意义是把下面两件事正式区分开：

- 同一代 runtime 被唤醒
- 同一会话下新起一代 runtime 继续工作

### 5.3 `ConversationHead`

`ConversationHead` 是 continuity bridge。

它不是用户 transcript，也不是 runtime durable truth，而是“某个角色 scope 能否继续沿用 provider-native response chain”的专门对象。

最典型的 head 包括：

- `superleader`
- `leader_lane`
- `teammate_slot`

每个 head 至少应记住：

- `last_response_id`
- `checkpoint_summary`
- 当前 `provider / model / backend`
- 关联的 `AgentSession / WorkerSession`

### 5.4 Runtime Truth 继续留在现有对象

执行真相仍然应由这些对象承担：

- `AgentSession`
- `WorkerSession`
- `ResidentCoordinatorSession`
- `ResidentSessionHost`
- `DeliveryState`
- mailbox / protocol bus / blackboard / task surface authority

这条边界不能因为引入会话层而倒退。

## 6. 四种正式连续性语义

### 6.1 `exact_wake`

同一代 runtime 还活着，或者还可以通过 reclaim + `reattach(...)` 无损接回。

它是对已有 `RuntimeGeneration` 的唤醒动作，不应再被建模成一个新的 generation origin mode。

保留：

- 同一 `RuntimeGeneration`
- 现有 runtime ownership
- 兼容的 `ConversationHead.last_response_id`

### 6.2 `warm_resume`

旧 runtime 进程已经不适合直接接回，但 durable state 足够重建上下文和执行面。

保留：

- 同一 `WorkSession`
- checkpoint summaries
- 兼容的 per-role `last_response_id`

不保留：

- 旧 lease ownership
- 旧 transport liveness 假设

### 6.3 `fork_session`

创建新的 `WorkSession` 分支，但从旧会话导入选定上下文。

这个新分支的 generation 0 应显式标记为 `fork_seed`，从而和 `new_session -> fresh`、`warm_resume -> warm_resume` 区分开。

保留：

- 语义摘要
- objective lineage
- 选定 head checkpoint

不保留：

- live mailbox cursor
- live worker ownership
- lease ownership

### 6.4 `new_session`

真正的新会话。

默认不继承：

- `last_response_id`
- runtime generation
- mailbox cursor
- worker ownership

只有用户显式要求时，才允许导入摘要上下文。

## 7. 为什么 `ConversationHead` 很关键

当前代码里 `previous_response_id` 已经存在，但它更像 assignment-level continuation hook，而不是 formal session asset。

如果没有 `ConversationHead`，系统很容易把这些概念混在一起：

- leader loop 的当前 turn continuity
- teammate slot 的 provider continuity
- 整个会话的用户感知 continuity

一旦引入 `ConversationHead`，这些边界就会清晰很多：

- `WorkSession`
  - 管用户会话
- `RuntimeGeneration`
  - 管这次执行代际
- `ConversationHead`
  - 管某个 role scope 的 provider continuity
- runtime truth
  - 管实际执行和 authority

判断：

- 这是当前最值得补上的“产品层 + 运行时层之间的桥”
- 它比直接把 transcript 升成唯一真相更符合 Agent Orchestra 的现有主线

## 8. 失效与降级规则应该显式化

`last_response_id` 不应被视为永远可继承。

应显式失效的典型场景包括：

- provider 变化
- model 变化
- backend 变化
- prompt contract version 变化
- fork 边界
- 用户明确要求 clean branch

失效后不应让会话 continuity 直接消失，而应降级到：

- `checkpoint_summary`
- runtime live summary
- blackboard / delivery / mailbox 摘要

也就是说：

- provider-native continuity 是加分项
- semantic checkpoint 才是更稳的保底项

## 9. 当前已落地的 first-cut

截至 2026-04-10，这条线已经不再只是设计稿，first-cut 主体已经落到代码里：

- continuity contracts 已新增到 `src/agent_orchestra/contracts/session_continuity.py`
  - `WorkSession`
  - `RuntimeGeneration`
  - `WorkSessionMessage`
  - `ConversationHead`
  - `SessionEvent`
  - `ResumeGateDecision`
  - `ContinuationBundle`
- continuity ID helper 已新增到 `src/agent_orchestra/contracts/ids.py`
  - `make_work_session_id(...)`
  - `make_runtime_generation_id(...)`
  - `make_work_session_message_id(...)`
  - `make_conversation_head_id(...)`
  - `make_session_event_id(...)`
- store contract 与 persistence surface 已接上
  - `src/agent_orchestra/storage/base.py`
  - `src/agent_orchestra/storage/in_memory.py`
  - `src/agent_orchestra/storage/postgres/models.py`
  - `src/agent_orchestra/storage/postgres/store.py`
- runtime continuity service 已新增到 `src/agent_orchestra/runtime/session_continuity.py`
  - `new_session(...)`
  - `warm_resume(...)`
  - `fork_session(...)`
  - `resume_gate(...)`
  - `list_sessions(...)`
  - `inspect_session(...)`
  - `build_continuation_bundles(...)`
  - worker-turn `ConversationHead` 读写
- `GroupRuntime` 已接上第一版 continuity bridge
  - `new_session(...)`
  - `warm_resume(...)`
  - `fork_session(...)`
  - `resume_gate(...)`
  - `list_work_sessions(...)`
  - `inspect_session(...)`
  - `exact_wake(...)`
  - `run_worker_assignment(...)` 会在存在 active `WorkSession` 时尝试复用兼容的 `previous_response_id`，并在 turn 完成后写回 `ConversationHead`
- `DefaultWorkerSupervisor.recover_active_sessions(...)` 已新增 scoped recovery filter，可供 session exact-wake 只回收目标 continuity scope
- CLI 已不再只是 envelope：`src/agent_orchestra/cli/app.py` + `src/agent_orchestra/cli/main.py` 已接成真实 store-backed client
  - `agent-orchestra session list --group-id ... [--objective-id ...]`
  - `agent-orchestra session inspect --store-backend ... --work-session-id ...`
  - `agent-orchestra session new --store-backend ... --group-id ... --objective-id ...`
  - `agent-orchestra session attach --store-backend ... --work-session-id ...`
  - `agent-orchestra session wake --store-backend ... --work-session-id ...`
  - `agent-orchestra session fork --store-backend ... --work-session-id ... [--title ...]`

判断：

- 这意味着用户可见 session root、generation lineage、以及 per-role continuity asset 已经有了真实持久化表面
- 同时 runtime truth 仍继续留在现有 `ResidentSessionHost + WorkerSession + DeliveryState` 主线，没有被 transcript/session layer 反客为主

## 10. 当前实现边界与残留缺口

这次落地的是 first-cut，而不是终态。

已经成立的边界：

- `exact_wake` 仍然是 resume gate 的动作语义，而不是 `RuntimeGeneration.continuity_mode`
- generation origin 现在只接受：
  - `fresh`
  - `warm_resume`
  - `fork_seed`
- leader continuity 已经能在 active `WorkSession` 下把上一轮 `response_id` 写成 `ConversationHead.last_response_id`，并在下一次 assignment 上回灌为 `previous_response_id`
- fork/new 的 session-layer 边界已经存在：
  - fork 会保留 checkpoint，但清空 live response chain
  - new session 默认不继承旧 continuity

还没有完全做完的部分：

- `exact_wake` 已经不再只是 gate 决策：CLI `attach`/`wake` 会真实走到 `GroupRuntime.exact_wake(...)` 和 scoped recovery filter，当 reclaim 触发时它仍然属于 best-effort takeover，而不是跨所有 live-owner 情况都能无损 takeover 的终态
- first-cut 的 worker-turn continuity 目前重点覆盖 leader/worker 路径；更深的 superleader/session picker UX 仍是 follow-on
- 更强的一致性还没做：
  - `ConversationHead` write 与 broader coordination transaction family 的进一步收口
  - richer inspection/history UI
  - 更严格的 provider/model/backend invalidation policy 扩展

当前 follow-on 更适合继续推进这些剩余项：

- 把 `exact_wake` 从 objective-scoped best-effort reclaim 继续推进成更强的 generation-scoped / live-owner-aware takeover
- 把 `SessionInspectSnapshot` 继续补强到 host-owned live status、lane live view、以及更丰富的 mailbox / task-surface summary
- 把 CLI 从当前 session continuity client 继续扩成更完整的 session picker / continue UX

判断：

- 当前这条线已经不只是 first-cut persistence surface，而是带有真实 session continuity client 的 first usable slice
- 但 `exact_wake` 的恢复质量和 richer inspect UX 仍然属于后续 hardening，而不是已经彻底收口

## 11. 相关文档

- `active-reattach-and-protocol-bus.md`
- `online-collaboration-runtime-migration.md`
- `host-owned-teammate-loop-cutover-audit.md`
- `worker-lifecycle-protocol-and-lease.md`
- `docs/superpowers/specs/2026-04-10-agent-orchestra-session-continuity-and-forking-design.md`
