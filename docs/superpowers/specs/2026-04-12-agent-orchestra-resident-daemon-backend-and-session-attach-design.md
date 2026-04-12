# Agent Orchestra Resident Daemon Backend And Session-Attach Redesign

## 1. Summary

Agent Orchestra has already crossed an important threshold:

- it has durable `WorkSession` / `RuntimeGeneration` / `ResidentTeamShell`
- it has host-owned runtime truth via `ResidentSessionHost`
- it has `ACTIVE` worker session durability, reclaim, and `reattach(...)`
- it has attach-first operator semantics as a first-cut control plane
- it has protocol-native worker lifecycle and failure attribution

What it does not yet have is a stable product/runtime center.

Today the system still behaves primarily like:

- a bounded orchestration run
- launched by a foreground CLI process
- which may leave continuity records behind after the run exits

That leaves a structural gap between:

- durable continuity
- live resident truth
- automatic recovery
- user interaction

This redesign makes one explicit product/runtime decision:

**Agent Orchestra should become a long-lived local backend daemon with a thin CLI client, session-oriented attach semantics, and host-owned slot supervision.**

Under this model:

1. the daemon, not the CLI process, becomes the primary runtime owner
2. sessions become the primary user-facing operating surface
3. stable agent slots outlive individual worker/incarnation failures
4. abnormal failures are handled by daemon-owned supervision and replacement
5. attach/recover/wake become normal client actions against a live backend
6. bounded execution workers remain useful, but they stop being the product center

This is not a patch-level reliability improvement. It is a runtime re-platforming.

It deliberately extends, rather than replaces, the existing work on:

- session domain ownership
- resident team shell / attach-first semantics
- active reattach / protocol bus
- host-owned teammate work surface

The resulting target system is closer to:

- `codex` / `claude` style local backend + client interaction

and farther from:

- a foreground batch runner that happens to persist continuity artifacts

## 2. Scope And Sources

- Current design and knowledge:
  - `docs/superpowers/specs/2026-04-10-agent-orchestra-session-continuity-and-forking-design.md`
  - `docs/superpowers/specs/2026-04-11-agent-orchestra-resident-team-shell-and-attach-first-design.md`
  - `docs/superpowers/specs/2026-04-12-agent-orchestra-session-domain-and-durable-persistence-design.md`
  - `resource/knowledge/agent-orchestra-runtime/implementation-status.md`
  - `resource/knowledge/agent-orchestra-runtime/online-collaboration-runtime-migration.md`
  - `resource/knowledge/agent-orchestra-runtime/session-domain-and-durable-persistence.md`
  - `resource/knowledge/agent-orchestra-runtime/resident-team-shell-and-attach-first.md`
  - `resource/knowledge/agent-orchestra-runtime/active-reattach-and-protocol-bus.md`
  - `resource/knowledge/agent-orchestra-runtime/runtime-fragility-decomposition-and-hardening-roadmap.md`
- Current runtime entry points:
  - `src/agent_orchestra/runtime/group_runtime.py`
  - `src/agent_orchestra/runtime/session_domain.py`
  - `src/agent_orchestra/runtime/session_host.py`
  - `src/agent_orchestra/runtime/leader_loop.py`
  - `src/agent_orchestra/runtime/superleader.py`
  - `src/agent_orchestra/runtime/teammate_work_surface.py`
  - `src/agent_orchestra/runtime/teammate_online_loop.py`
  - `src/agent_orchestra/runtime/worker_supervisor.py`
  - `src/agent_orchestra/runtime/backends/codex_cli_backend.py`
  - `src/agent_orchestra/runtime/backends/tmux_backend.py`
  - `src/agent_orchestra/cli/app.py`
  - `src/agent_orchestra/cli/main.py`
- Current storage entry points:
  - `src/agent_orchestra/storage/base.py`
  - `src/agent_orchestra/storage/postgres/models.py`
  - `src/agent_orchestra/storage/postgres/store.py`
- Current execution contracts:
  - `src/agent_orchestra/contracts/execution.py`
  - `src/agent_orchestra/contracts/session_continuity.py`
  - `src/agent_orchestra/contracts/agent.py`
- Relevant runtime evidence:
  - `docs/superpowers/runs/2026-04-09-online-collaboration-first-batch-round-2.summary.json`
  - `docs/superpowers/runs/2026-04-09-online-collaboration-first-batch-round-2-custom-gap-replay-after-guard-fix.summary.json`
  - `docs/superpowers/runs/2026-04-10-superleader-isomorphic-runtime-replay-2026-04-10T03-47-35Z.summary.json`
  - `docs/superpowers/runs/codex-spool/task_4da77b442d06:leader-turn-1.result.json`
  - `docs/superpowers/runs/codex-spool/task_ac96c894e0c1:teammate-turn-1.result.json`

This document defines a target redesign. It does not claim that the repository already implements the daemon/client architecture described below.

## 3. Problem Statement

### 3.1 The current product center is still wrong

The current repository already supports durable state, attach-first inspection, and host-owned runtime projections.

But the primary operator story is still too close to:

- invoke a runtime through the CLI
- let that foreground process drive orchestration
- inspect the session later if it survives

That makes the CLI process too important.

The long-term correct center should instead be:

- a daemon owns live orchestration truth
- a client attaches to that daemon
- sessions remain meaningful even when a particular client exits

### 3.2 Continuity exists, but live interaction is still second-class

Current session continuity already provides:

- `new`
- `list`
- `inspect`
- `attach`
- `wake`
- `fork`

However, those flows still act more like a control plane on top of bounded runs than a primary interaction model on top of a resident backend.

The missing capability is not another resume enum.

The missing capability is:

- a long-lived runtime owner that is still present when the user returns

### 3.3 Automatic recovery is still not the primary runtime law

Current code already supports:

- active-session durability
- reclaim
- `reattach(...)`
- protocol-native wait
- failure attribution

But that is still shaped as:

- recovery tools for launched workers

rather than:

- a daemon continuously supervising stable agent slots and replacing failed incarnations

That difference is critical.

The correct target is not “restart a process”.
The correct target is “preserve a slot and replace its failed incarnation”.

### 3.4 Foreground CLI ownership keeps user experience fragile

As long as the CLI remains close to the runtime owner, the system will continue to suffer from:

- terminal lifetime affecting orchestration lifetime
- ambiguous attach semantics
- session `open` being misread as “still running”
- bounded execution backends being overused as product-facing runtime surfaces

### 3.5 `codex_cli` is useful but structurally the wrong product center

`codex_cli` is explicitly modeled as `EPHEMERAL_WORKER_TRANSPORT`.

That is a reasonable execution backend.

It is not the correct default runtime surface for:

- long-lived `leader`
- long-lived `superleader`
- daemon-owned interactive session continuity

The daemon redesign therefore does not try to make `codex_cli` pretend to be the primary resident host.

Instead:

- the daemon becomes the resident owner
- transports become replaceable execution carriers beneath it

## 4. Design Alternatives

### 4.1 Alternative A: Keep foreground CLI and only add watchdog/restart

This would add:

- health polling
- abnormal failure classification
- automatic relaunch of workers

Why it is not enough:

- the CLI still remains too close to runtime ownership
- session attach remains secondary
- live truth still disappears with the foreground runtime owner
- “open session but no live backend” ambiguity remains

This is a hardening layer, not the right product architecture.

### 4.2 Alternative B: Resident daemon backend with thin CLI client

This adds:

- a long-lived daemon as runtime owner
- thin CLI client commands over local IPC
- session attach as the primary interaction model
- stable agent slots with daemon-owned supervision
- automatic replacement of abnormal incarnations

Why this is the recommended path:

- it solves both fragility and operator experience together
- it reuses current session-domain / resident-shell / protocol-bus work
- it turns recovery from a best-effort helper into a system law

### 4.3 Alternative C: Full actor fabric immediately

This would make every agent:

- a fully independent long-lived actor
- potentially with richer scheduling and fabric semantics from the start

Why it is not the recommended first cut:

- it is too large a jump from the current codebase
- it would over-couple supervision, daemonization, and actor abstraction in one move
- it would raise implementation risk before daemon/client/session separation is stable

### 4.4 Recommendation

Choose **Alternative B**.

It is the smallest architecture that is actually correct.

It is materially stronger than watchdog patching and materially safer than a full actor-fabric rewrite.

## 5. Goals

- Make a long-lived local daemon the primary runtime owner.
- Make session attach the primary operator experience.
- Preserve stable slot identity across individual agent/incarnation failures.
- Make abnormal failure detection and replacement daemon-owned responsibilities.
- Keep `WorkSession`, `RuntimeGeneration`, and `ResidentTeamShell` as first-class durable objects.
- Preserve host-owned runtime truth rather than shifting to transcript-first truth.
- Support thin local CLI clients that can reconnect at any time.
- Make live interaction, inspection, and recovery work through session-oriented APIs.
- Keep execution backends replaceable beneath the daemon.
- Preserve the current direction of resident team shells and session-domain separation.

## 6. Non-Goals

- This redesign does not introduce remote multi-host orchestration.
- This redesign does not require distributed consensus.
- This redesign does not make Redis the durable source of truth.
- This redesign does not replace PostgreSQL as the primary durable session/runtime store.
- This redesign does not make every backend a full-resident transport.
- This redesign does not require an immediate full actor-fabric rewrite.
- This redesign does not solve planner-feedback, permission broker, or provider scoring in full detail, though it provides a place for them.

## 7. Decision

### 7.1 Introduce a first-class resident daemon

Agent Orchestra should introduce a long-lived local backend daemon.

Recommended conceptual name:

- `ao daemon`

Recommended module family:

- `src/agent_orchestra/daemon/`

This daemon becomes the primary owner for:

- live session registry
- resident shell registry
- slot registry
- slot supervision and recovery
- daemon-owned event streams
- attach interaction routing
- provider route health memory

### 7.2 Turn the CLI into a client

The CLI should become a thin local client over daemon IPC.

The CLI no longer directly owns the long-lived runtime lifecycle for operator-facing flows.

Instead, it should:

- connect to the daemon
- submit commands
- request attach streams
- send input
- render status and events

### 7.3 Preserve stable slots, replace incarnations

The daemon must treat runtime identity like this:

- `session`
  - stable user-visible root
- `resident shell`
  - stable live operating surface
- `agent slot`
  - stable role identity under a shell
- `agent incarnation`
  - concrete running process/binding for that slot

The daemon should restart or replace:

- an incarnation

It should not restart:

- the slot identity
- the session identity

### 7.4 Make daemon-owned supervision a first-class control loop

The daemon should continuously supervise:

- heartbeat
- protocol progress
- lease renewal
- mailbox lag
- idle expectations
- provider-unavailable conditions
- transport death

It should classify failures and only auto-replace abnormal ones.

## 8. Target Architecture

### 8.1 Major components

The target system should contain these primary components:

1. `DaemonServer`
   - process lifecycle, PID/lock, local namespace ownership
2. `ControlPlaneAPI`
   - local IPC command surface for session and runtime operations
3. `SessionDomainService`
   - durable session graph owner
4. `ResidentRuntimeManager`
   - session -> shell -> slot orchestration
5. `SlotSupervisor`
   - health checks, abnormal-failure classification, restart decisions
6. `ExecutionGateway`
   - execution backend orchestration for concrete assignments/incarnations
7. `EventStreamHub`
   - attach/status/event streaming to clients
8. `ProviderRoutingService`
   - sticky health memory and route quarantine
9. `ApprovalBridge`
   - attach / idle / authority approval routing for operator interaction

### 8.2 Layering

Recommended runtime layering:

1. client layer
   - CLI and future UI clients
2. daemon control plane
   - IPC commands and event streams
3. session and resident runtime layer
   - session graph, shell graph, slot graph
4. supervision layer
   - health classification and replacement
5. execution layer
   - backend launches, reattach, bounded work
6. persistence and event layer
   - Postgres durable truth, Redis/in-memory bus where appropriate

### 8.3 Why this layering matters

This prevents the system from continuing to confuse:

- foreground CLI lifetime
- backend process lifetime
- session continuity lifetime
- slot supervision lifetime

Each becomes its own explicit owner domain.

## 9. Process Model

### 9.1 Daemon process

There should be one long-lived daemon process per local orchestration namespace.

For the first cut, make the design concrete:

- single host
- single user namespace
- Unix domain socket for local IPC
- PostgreSQL as durable truth

The daemon is expected to survive:

- client exit
- terminal exit
- transient worker death

### 9.2 Client process

The CLI client should be short-lived by default.

It may:

- run one command and exit
- attach to a stream and remain interactive

But the CLI is no longer the primary owner of orchestration runtime state.

### 9.3 Worker processes

Concrete worker/incarnation execution still uses existing transports:

- `in_process`
- `tmux`
- `subprocess`
- `codex_cli`

But those transports operate beneath daemon-owned slot identity.

## 10. Slot And Incarnation Model

### 10.1 `AgentSlot`

Introduce a first-class `AgentSlot`.

Stable examples:

- `superleader:<objective_id>`
- `leader:<lane_id>`
- `teammate:<team_id>:slot:<n>`

Properties:

- stable slot id
- role
- owning session / shell
- lifecycle policy
- preferred transport class
- current incarnation id
- desired state

### 10.2 `AgentIncarnation`

Introduce `AgentIncarnation` as the concrete live or historical execution instance for a slot.

Properties:

- incarnation id
- slot id
- launch backend
- transport locator
- lease id
- started at / ended at
- terminal classification
- restart generation index
- restart reason

### 10.3 Fencing

Every incarnation must carry fencing identity.

Minimum requirement:

- `incarnation_id`
- `lease_id`

All writes originating from an incarnation must be rejectable if:

- the slot has already moved to a newer incarnation

This is mandatory to prevent stale zombie workers from corrupting current truth.

## 11. Session And Shell Model

### 11.1 Session remains the user-visible root

`WorkSession` remains the user-visible root.

It answers:

- which user-visible session exists
- what lineage it belongs to
- what resident shell(s) belong to it

### 11.2 `ResidentTeamShell` becomes the attach target

`ResidentTeamShell` becomes the primary attach surface for team interaction.

It should expose:

- shell status
- attach recommendation
- active slot summary
- degraded slot summary
- approval status
- current runtime generation
- last terminal run

### 11.3 Session truth must be explicit

The daemon must keep separate user-visible fields for:

- continuity status
- live shell status
- runtime generation status
- last terminal run status

This removes the current ambiguity where `open` continuity may be misread as live execution.

## 12. Health And Failure Model

### 12.1 Health signals

Slot supervision should evaluate at least:

- process presence
- lease freshness
- protocol progress freshness
- heartbeat recency
- mailbox lag
- task/output progress
- attach/shell phase consistency
- provider route health

### 12.2 Failure classes

The daemon should classify slot failure into at least:

1. `normal_terminal`
   - completed, blocked, denied, explicitly quiescent
2. `recoverable_abnormal`
   - process death, SIGTERM, no progress, lost transport, provider transient failure
3. `external_degraded`
   - provider unavailable exhaustion, approval wait, missing dependency
4. `fatal_configuration`
   - invalid contract/backend mismatch, missing required durable state, incompatible schema

### 12.3 Restart rule

Only `recoverable_abnormal` should trigger automatic incarnation replacement.

`normal_terminal` should not be auto-restarted.

`external_degraded` should usually not relaunch immediately; it should enter degraded waiting semantics.

`fatal_configuration` should stop and escalate rather than loop.

## 13. Supervision Policy

### 13.1 `RestartPolicy`

Introduce a role-scoped `RestartPolicy`.

Minimum fields:

- max restarts per window
- restart backoff
- provider-degraded cooldown
- escalation threshold
- allowed transport fallback classes
- allowed hydration downgrade modes

### 13.2 Replacement behavior

When the daemon replaces an incarnation:

1. mark the old incarnation terminal
2. fence the old lease
3. preserve slot identity
4. hydrate the replacement from host-owned truth
5. rebind the slot to the new incarnation
6. publish a slot-restarted event

### 13.3 Storm prevention

The daemon must prevent restart storms.

Required controls:

- exponential backoff
- restart budget window
- route quarantine for provider failures
- shell-level degraded state after repeated slot churn

## 14. Provider Routing

### 14.1 Move provider memory into the daemon

The daemon should own provider health memory.

That includes:

- route health score
- last failure class
- consecutive provider-unavailable count
- cooldown expiration
- preferred healthy route by role

### 14.2 Keep provider failure distinct from logic failure

Provider failure should not collapse into generic `failed` semantics.

Instead, the daemon should surface:

- `waiting_provider`
- `degraded_provider`
- `provider_route_quarantined`

at slot / shell / session read-model layers.

## 15. IPC And Client API

### 15.1 IPC transport

For the first cut, use:

- Unix domain socket

Reasons:

- local-only daemon
- no accidental remote exposure
- easy lifecycle coordination

### 15.2 Command families

The daemon should expose command families like:

- `server.status`
- `session.new`
- `session.list`
- `session.inspect`
- `session.attach`
- `session.send`
- `session.wake`
- `session.fork`
- `session.events`
- `slot.inspect`
- `slot.restart`
- `approval.respond`

### 15.3 Attach stream

`session attach` should return a live stream, not just a static snapshot.

The stream should support:

- status updates
- shell events
- mailbox digests
- output summaries
- interactive input injection where allowed

## 16. CLI Redesign

### 16.1 New mental model

The operator experience should become:

- start daemon once
- create or find a session
- attach to it repeatedly over time

Recommended verbs:

- `ao server start`
- `ao server status`
- `ao session new`
- `ao session list`
- `ao session inspect`
- `ao session attach`
- `ao session send`
- `ao session wake`

### 16.2 CLI responsibilities

The CLI should:

- connect to the daemon
- validate arguments
- render outputs and streams

The CLI should not:

- own long-lived runtime state
- directly supervise slots
- directly decide restart semantics

## 17. Persistence Model

### 17.1 Postgres remains durable truth

Postgres remains the primary durable truth for:

- session graph
- shell graph
- slot graph
- incarnation graph
- runtime events
- attach projections
- provider route health memory

### 17.2 New durable objects

Recommended new objects:

- `AgentSlot`
- `AgentIncarnation`
- `SlotHealthEvent`
- `SessionAttachment`
- `ProviderRouteHealth`

### 17.3 Transaction rules

Critical write families must be atomic:

- slot replacement
- incarnation fence + replacement bind
- attach state transitions
- provider route quarantine updates
- daemon-owned terminalization and shell status updates

## 18. Runtime Ownership Rules

### 18.1 Daemon-owned

The daemon owns:

- slot lifecycle
- incarnation lifecycle
- supervision and replacement
- attach stream registry
- provider route health memory

### 18.2 Session domain-owned

The session domain owns:

- `WorkSession`
- `RuntimeGeneration`
- `ResidentTeamShell`
- session-facing read models

### 18.3 Runtime-owned

The runtime layer still owns:

- `WorkerSession`
- `AgentSession`
- `ResidentCoordinatorSession`
- mailbox cursor truth
- authority and delivery truth

The daemon consumes and projects runtime truth. It does not flatten it into transcript-like session truth.

## 19. Migration Plan

### 19.1 Phase 0: Preserve existing attach-first semantics

Keep existing CLI and runtime entry points operational while introducing the daemon behind feature flags.

### 19.2 Phase 1: Daemon skeleton and IPC

Add:

- daemon bootstrap
- single-instance lock
- Unix socket IPC
- health endpoint

### 19.3 Phase 2: Session client cutover

Move CLI `session new/list/inspect/attach/wake/fork` to daemon-mediated flows.

### 19.4 Phase 3: Leader and superleader slot supervision

Represent:

- superleader
- leader

as first-class slots with incarnations and restart policies.

### 19.5 Phase 4: Teammate slot supervision

Promote teammate slot identity into daemon-owned slot supervision with replacement on abnormal failure.

### 19.6 Phase 5: Provider route health memory

Introduce daemon-owned sticky route memory and route quarantine.

### 19.7 Phase 6: Default-product cutover

Make:

- daemon-backed session interaction
- attach-first CLI
- slot supervision

the default operator path.

## 20. Verification Strategy

### 20.1 Unit coverage

Add unit coverage for:

- slot lifecycle state machine
- incarnation fencing
- failure classification
- restart policy
- provider route quarantine

### 20.2 Integration coverage

Add integration coverage for:

- daemon start / reconnect
- session attach after CLI exit
- abnormal slot death and replacement
- stale incarnation write rejection
- attach stream continuity across replacement

### 20.3 Failure injection coverage

Add dedicated failure tests for:

- worker `SIGTERM`
- no-progress timeout
- `provider_unavailable`
- lost tmux session
- daemon restart with live durable state

### 20.4 Operator verification

Manual verification should prove:

- start daemon
- create session
- attach
- kill a supervised abnormal incarnation
- observe automatic replacement
- reattach without losing session identity

## 21. Risks

### 21.1 Risk: Two owners during migration

If CLI and daemon both partially own runtime state, the migration will create split-brain behavior.

Mitigation:

- make daemon ownership explicit per command family
- avoid partial ownership overlap

### 21.2 Risk: Restart storms

Aggressive abnormal restart without route quarantine or backoff can degrade the machine.

Mitigation:

- strict restart budget
- shell-level degraded state
- provider route quarantine

### 21.3 Risk: Zombie incarnation writes

Late writes from stale incarnations can corrupt current truth.

Mitigation:

- mandatory fencing tokens
- stale-writer rejection at persistence boundaries

### 21.4 Risk: Attach semantics become too magical

If attach hides too much daemon state, operators will not know whether they are on a live shell, recovered shell, or warm-resumed shell.

Mitigation:

- explicit attach-mode and shell-state rendering

## 22. Success Criteria

This redesign is successful when all of the following are true:

1. the daemon remains alive when the CLI exits
2. the CLI can reattach to a live session later
3. stable slots survive abnormal incarnation death
4. abnormal failure triggers replacement without changing slot identity
5. normal terminal states do not trigger restart loops
6. provider degradation is surfaced separately from logic failure
7. session inspect cleanly distinguishes continuity state from live shell state
8. coordinator runtime no longer depends on a foreground CLI process as owner

## 23. Follow-On Work

- richer approval broker integration
- planner-feedback integration into daemon-owned runtime
- cross-team collaboration overlay
- optional remote-control transport after local daemon model is stable
- deeper full-resident coordinator transport strategy

## 24. Related Documents

- `docs/superpowers/specs/2026-04-10-agent-orchestra-session-continuity-and-forking-design.md`
- `docs/superpowers/specs/2026-04-11-agent-orchestra-resident-team-shell-and-attach-first-design.md`
- `docs/superpowers/specs/2026-04-12-agent-orchestra-session-domain-and-durable-persistence-design.md`
- `resource/knowledge/agent-orchestra-runtime/session-domain-and-durable-persistence.md`
- `resource/knowledge/agent-orchestra-runtime/resident-team-shell-and-attach-first.md`
- `resource/knowledge/agent-orchestra-runtime/active-reattach-and-protocol-bus.md`
- `resource/knowledge/agent-orchestra-runtime/runtime-fragility-decomposition-and-hardening-roadmap.md`
