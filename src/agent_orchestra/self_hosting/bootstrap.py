from __future__ import annotations

import json
import re
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from pathlib import Path

from agent_orchestra.contracts.delivery import DeliveryStatus
from agent_orchestra.contracts.enums import WorkerStatus
from agent_orchestra.contracts.execution import Planner, WorkerExecutionPolicy
from agent_orchestra.contracts.worker_protocol import WorkerRoleProfile
from agent_orchestra.planning import DynamicSuperLeaderPlanner, ObjectiveTemplate, WorkstreamTemplate
from agent_orchestra.runtime.group_runtime import GroupRuntime
from agent_orchestra.runtime.leader_loop import (
    align_role_profile_timeouts,
    build_runtime_role_profiles,
)
from agent_orchestra.runtime.protocol_bridge import protocol_bus_events_from_worker_record
from agent_orchestra.runtime.superleader import SuperLeaderConfig, SuperLeaderRunResult, SuperLeaderRuntime


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _default_knowledge_path() -> Path:
    return _repo_root() / "resource" / "knowledge" / "agent-orchestra-runtime" / "implementation-status.md"


def _slugify(text: str) -> str:
    value = text.lower()
    value = re.sub(r"[`*_]+", "", value)
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return value.strip("-") or "gap"


def _normalize(text: str) -> str:
    value = text.strip().lower()
    value = re.sub(r"[`*_]+", "", value)
    return re.sub(r"\s+", " ", value)


_DELEGATION_VALIDATION_GAP_ID = "leader-teammate-delegation-validation"


@dataclass(frozen=True, slots=True)
class _GapDefinition:
    gap_id: str
    title: str
    summary: str
    rationale: str
    team_name: str
    acceptance_checks: tuple[str, ...]
    owned_paths: tuple[str, ...]
    verification_commands: tuple[str, ...]
    depends_on: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()


_COMMON_RUNTIME_KNOWLEDGE_PATHS: tuple[str, ...] = (
    "resource/knowledge/README.md",
    "resource/knowledge/agent-orchestra-runtime/README.md",
    "resource/knowledge/agent-orchestra-runtime/implementation-status.md",
    "resource/knowledge/agent-orchestra-runtime/current-priority-gap-list.md",
    "resource/knowledge/agent-orchestra-runtime/current-goal-gap-checklist.md",
    "resource/knowledge/agent-orchestra-runtime/remaining-work-backlog.md",
)

_FIRST_BATCH_EXECUTION_KNOWLEDGE_PATHS: tuple[str, ...] = _COMMON_RUNTIME_KNOWLEDGE_PATHS + (
    "resource/knowledge/agent-orchestra-runtime/first-batch-online-collaboration-execution-pack.md",
)


_GAP_DEFINITIONS: tuple[_GapDefinition, ...] = (
    _GapDefinition(
        gap_id="team-primary-semantics-switch",
        title="Team Primary Semantics Switch",
        summary=(
            "Push team execution fully toward mailbox + task list + TeamBlackboard resident collaboration, "
            "with leader as activation/convergence center and teammate as long-lived worker surface."
        ),
        rationale=(
            "The resident teammate path is already partially landed, but the remaining first-batch gap is to "
            "remove leader-owned teammate execution truth and make the team runtime operate from teammate-owned "
            "work surfaces."
        ),
        team_name="Runtime",
        acceptance_checks=(
            "leader loop no longer owns a second teammate execution truth",
            "teammate mailbox/task polling is teammate-owned or host-owned",
            "team runtime continues to converge through resident collaboration",
        ),
        owned_paths=(
            "src/agent_orchestra/runtime/leader_loop.py",
            "src/agent_orchestra/runtime/teammate_work_surface.py",
            "src/agent_orchestra/runtime/teammate_online_loop.py",
            "src/agent_orchestra/runtime/session_host.py",
            "tests/test_leader_loop.py",
            "tests/test_teammate_work_surface.py",
            "tests/test_teammate_online_loop.py",
            "tests/test_session_host.py",
            "resource/knowledge/agent-orchestra-runtime/online-collaboration-runtime-migration.md",
            "resource/knowledge/agent-orchestra-runtime/resident-team-collaboration-and-slice-planning.md",
            "resource/knowledge/agent-orchestra-runtime/host-owned-teammate-loop-cutover-audit.md",
        )
        + _FIRST_BATCH_EXECUTION_KNOWLEDGE_PATHS,
        verification_commands=(
            "python3 -m unittest tests.test_teammate_work_surface tests.test_teammate_online_loop -v",
            "python3 -m unittest tests.test_leader_loop tests.test_session_host -v",
        ),
        keywords=(
            "team-primary-semantics-switch",
            "team primary semantics switch",
            "resident collaboration",
            "leader assignment shell -> resident collaboration",
        ),
    ),
    _GapDefinition(
        gap_id="coordination-transaction-and-session-truth-convergence",
        title="Coordination Transaction And Session Truth Convergence",
        summary=(
            "Converge task, mailbox, blackboard, delivery, cursor, and session writes toward fewer owners "
            "and a smaller crash window."
        ),
        rationale=(
            "The coordination live path still spans multiple helpers and storage surfaces; this first-batch gap "
            "is about tightening the transaction/session truth boundary and keeping self-hosting aligned with "
            "runtime truth."
        ),
        team_name="Runtime",
        acceptance_checks=(
            "receipt/result/authority/outbox/session live writes stay aligned",
            "session continuation truth is clearer after reconnect/crash",
            "runtime and self-hosting session truth do not drift",
        ),
        owned_paths=(
            "src/agent_orchestra/runtime/group_runtime.py",
            "src/agent_orchestra/runtime/session_host.py",
            "src/agent_orchestra/storage/base.py",
            "src/agent_orchestra/storage/postgres/store.py",
            "src/agent_orchestra/runtime/worker_supervisor.py",
            "tests/test_runtime.py",
            "tests/test_postgres_store.py",
            "tests/test_session_host.py",
            "tests/test_worker_supervisor_protocol.py",
            "resource/knowledge/agent-orchestra-runtime/agent-orchestra-gap-handoff.md",
            "resource/knowledge/agent-orchestra-runtime/target-gap-roadmap-and-task-subtree-authority.md",
        )
        + _FIRST_BATCH_EXECUTION_KNOWLEDGE_PATHS,
        verification_commands=(
            "python3 -m unittest tests.test_runtime -v",
            "python3 -m unittest tests.test_postgres_store -v",
            "python3 -m unittest tests.test_session_host tests.test_worker_supervisor_protocol -v",
        ),
        keywords=(
            "coordination-transaction-and-session-truth-convergence",
            "coordination transaction and session truth convergence",
            "session truth convergence",
            "coordination transaction convergence",
        ),
    ),
    _GapDefinition(
        gap_id="task-surface-authority-contract",
        title="Task Surface Authority Contract",
        summary=(
            "Formalize task-surface structure/status authority so team task lists can act as durable collaboration "
            "surfaces without letting teammates mutate upper scopes unsafely."
        ),
        rationale=(
            "Task surface authority is the governance layer that lets long-lived teammates create or adjust "
            "team-scope work without collapsing into unbounded structure edits."
        ),
        team_name="Authority",
        acceptance_checks=(
            "task item structure/status authority is runtime-consumable",
            "subtree ownership and escalation rules are enforced",
            "task surface mutations stop living only in docs",
        ),
        owned_paths=(
            "src/agent_orchestra/contracts/task.py",
            "src/agent_orchestra/runtime/group_runtime.py",
            "src/agent_orchestra/runtime/authority_reactor.py",
            "src/agent_orchestra/runtime/leader_loop.py",
            "src/agent_orchestra/runtime/superleader.py",
            "tests/test_runtime.py",
            "tests/test_leader_loop.py",
            "tests/test_superleader_runtime.py",
            "resource/knowledge/agent-orchestra-runtime/authority-escalation-and-scope-extension.md",
            "resource/knowledge/agent-orchestra-runtime/target-gap-roadmap-and-task-subtree-authority.md",
        )
        + _FIRST_BATCH_EXECUTION_KNOWLEDGE_PATHS,
        verification_commands=(
            "python3 -m unittest tests.test_runtime -v",
            "python3 -m unittest tests.test_leader_loop tests.test_superleader_runtime -v",
        ),
        keywords=(
            "task-surface-authority-contract",
            "task surface authority contract",
            "task subtree authority",
            "task surface authority",
        ),
    ),
    _GapDefinition(
        gap_id="superleader-isomorphic-runtime",
        title="SuperLeader Isomorphic Runtime",
        summary=(
            "Move the superleader toward a resident leader-of-leaders runtime that consumes lane digest, mailbox, "
            "and objective live state rather than acting like a one-shot scheduler."
        ),
        rationale=(
            "The lower runtime has already started becoming resident and host-owned; the first-batch superleader "
            "gap is to push the upper layer into the same shape without expanding to the full cross-team overlay yet."
        ),
        team_name="Runtime",
        acceptance_checks=(
            "superleader consumes lane digest/objective live metadata more directly",
            "ready lanes flow through host-owned leader session surfaces",
            "self-hosting keeps exporting the stronger resident truth",
        ),
        owned_paths=(
            "src/agent_orchestra/runtime/superleader.py",
            "src/agent_orchestra/runtime/session_host.py",
            "src/agent_orchestra/runtime/group_runtime.py",
            "src/agent_orchestra/self_hosting/bootstrap.py",
            "tests/test_superleader_runtime.py",
            "tests/test_self_hosting_round.py",
            "resource/knowledge/agent-orchestra-runtime/resident-hierarchical-runtime.md",
            "resource/knowledge/agent-orchestra-runtime/online-collaboration-runtime-migration.md",
        )
        + _FIRST_BATCH_EXECUTION_KNOWLEDGE_PATHS,
        verification_commands=(
            "python3 -m unittest tests.test_superleader_runtime -v",
            "python3 -m unittest tests.test_self_hosting_round -v",
        ),
        keywords=(
            "superleader-isomorphic-runtime",
            "superleader isomorphic runtime",
            "leader-of-leaders resident runtime",
            "resident superleader runtime",
        ),
    ),
    _GapDefinition(
        gap_id="multi-leader-planning-review",
        title="Multi-Leader Planning Review",
        summary=(
            "Run multi-leader draft, peer review, revision, and activation-gate synthesis "
            "before self-hosting lane activation."
        ),
        rationale=(
            "Self-hosting still needs the planning-review round to become a first-class gap so "
            "bootstrap can explicitly target draft/peer/revision/activation-gate reconciliation "
            "instead of treating it as an undocumented side path."
        ),
        team_name="Planning",
        acceptance_checks=(
            "planning review tests",
            "activation gate exported into runtime and self-hosting packet",
        ),
        owned_paths=(
            "src/agent_orchestra/runtime/superleader.py",
            "src/agent_orchestra/runtime/group_runtime.py",
            "src/agent_orchestra/self_hosting/bootstrap.py",
            "tests/test_superleader_runtime.py",
            "tests/test_self_hosting.py",
            "tests/test_self_hosting_round.py",
        ),
        verification_commands=(
            "python3 -m unittest tests.test_superleader_runtime -v",
            "python3 -m unittest tests.test_self_hosting tests.test_self_hosting_round -v",
        ),
        keywords=(
            "multi leader planning review",
            "multi leader draft / peer review / revision / activation gate",
            "draft / peer review / revision / activation gate",
            "activation gate",
            "leader draft plan",
            "peer review",
            "planning review",
        ),
    ),
    _GapDefinition(
        gap_id="team-parallel-execution",
        title="Resident Async Team Coordination",
        summary="Keep the landed budgeted parallel baseline and push the leader loop toward a resident async coordinator with subscription/autonomous claim semantics.",
        rationale="Budgeted teammate concurrency, first-completion slot refill, mailbox follow-up, task-list refill, and teammate slot reuse are already landed; the remaining gap is evolving that base into a resident async team coordinator.",
        team_name="Runtime",
        acceptance_checks=("parallel teammate execution tests", "leader to teammate path stays green"),
        owned_paths=(
            "src/agent_orchestra/runtime/leader_loop.py",
            "src/agent_orchestra/runtime/group_runtime.py",
            "tests/test_leader_loop.py",
        ),
        verification_commands=(
            "python3 -m unittest tests.test_leader_loop -v",
            "python3 -m unittest discover -s tests -v",
        ),
        keywords=(
            "claude 风格的 team 内并行执行",
            "team parallel execution",
            "team parallel execution toward resident/subscription/autonomous claim",
            "resident async team coordination",
        ),
    ),
    _GapDefinition(
        gap_id=_DELEGATION_VALIDATION_GAP_ID,
        title="Leader To Teammate Delegation Validation",
        summary=(
            "Run a self-hosting validation lane that forces non-empty slices on the first leader turn "
            "and proves host-owned leader-to-teammate convergence inside one autonomous round."
        ),
        rationale="Recent self-hosting rounds completed with empty slices, so the delegation path still lacks a strong runtime validation loop.",
        team_name="Runtime",
        acceptance_checks=(
            "turn 1 emits non-empty slices",
            "host-owned leader session converges without extra leader prompt turns",
            "self-hosting delegation round evidence",
        ),
        owned_paths=(
            "src/agent_orchestra/runtime/leader_loop.py",
            "src/agent_orchestra/runtime/leader_output_protocol.py",
            "tests/test_leader_loop.py",
            "tests/test_self_hosting_round.py",
        ),
        verification_commands=(
            "python3 -m unittest tests.test_leader_loop -v",
            "python3 -m unittest tests.test_self_hosting_round -v",
        ),
        keywords=(
            "强验证 leader -> teammate delegation",
            "leader -> teammate delegation",
            "leader teammate delegation validation",
            "leader to teammate delegation validation",
        ),
    ),
    _GapDefinition(
        gap_id="superleader-parallel-scheduler",
        title="Parallel SuperLeader Lane Scheduler",
        summary="Upgrade the superleader from sequential lane execution to a budgeted parallel lane scheduler with explicit coordination state.",
        rationale="The current superleader still loops over leader rounds sequentially, so it cannot yet act like a real leader-of-leaders runtime.",
        team_name="Runtime",
        acceptance_checks=("parallel superleader tests", "objective convergence remains correct under multiple active lanes"),
        owned_paths=(
            "src/agent_orchestra/runtime/superleader.py",
            "src/agent_orchestra/runtime/leader_loop.py",
            "tests/test_superleader_runtime.py",
        ),
        verification_commands=(
            "python3 -m unittest tests.test_superleader_runtime -v",
            "python3 -m unittest discover -s tests -v",
        ),
        keywords=(
            "并行的 superleader lane scheduler",
            "superleader lane scheduler",
            "parallel superleader lane scheduler",
            "并行 superleader",
        ),
    ),
    _GapDefinition(
        gap_id="message-pool-subscriptions",
        title="Message Pool And Subscriptions",
        summary="Promote mailbox transport into an append-only message pool with subscription specs, cursors, digest views, and summary/ref-first delivery.",
        rationale="A multi-team runtime needs a single durable message truth source plus subscription-driven visibility instead of ad-hoc envelope duplication.",
        team_name="Control",
        acceptance_checks=("message pool tests", "subscription cursor tests"),
        owned_paths=(
            "src/agent_orchestra/tools/mailbox.py",
            "src/agent_orchestra/runtime/protocol_bridge.py",
            "tests/test_protocol_bridge.py",
        ),
        verification_commands=(
            "python3 -m unittest tests.test_protocol_bridge -v",
            "python3 -m unittest discover -s tests -v",
        ),
        keywords=(
            "订阅式消息池",
            "message pool",
            "message pool and subscriptions",
            "subscriptions",
            "消息池",
        ),
    ),
    _GapDefinition(
        gap_id="message-visibility-policy",
        title="Message Visibility Policy",
        summary="Formalize shared versus control/private message classes and the delivery modes summary_only, summary_plus_ref, and full_text across team and cross-team scopes.",
        rationale="Without an explicit visibility policy, high-density team subscriptions will collapse into noisy or leaky control traffic.",
        team_name="Control",
        acceptance_checks=("message visibility policy tests", "delivery mode policy documented"),
        owned_paths=(
            "src/agent_orchestra/tools/mailbox.py",
            "src/agent_orchestra/runtime/protocol_bridge.py",
            "tests/test_protocol_bridge.py",
            "resource/knowledge/agent-orchestra-runtime/agent-orchestra-framework.md",
            "resource/knowledge/agent-orchestra-runtime/parallel-team-coordination.md",
        ),
        verification_commands=(
            "python3 -m unittest tests.test_protocol_bridge -v",
            "python3 -m unittest discover -s tests -v",
        ),
        keywords=(
            "shared / control-private 消息分类与 delivery mode 规则",
            "shared / control-private",
            "control-private",
            "message visibility policy",
            "delivery mode 规则",
        ),
    ),
    _GapDefinition(
        gap_id="tool-capable-code-edit-worker",
        title="Tool-Capable Code-Edit Worker",
        summary="Add a worker capability that can edit code within owned paths and run verification commands under orchestration control.",
        rationale="Without a code-edit worker, self-hosting can plan the next slice but still cannot complete code changes autonomously.",
        team_name="Runtime",
        acceptance_checks=("code-edit worker tests", "owned path enforcement covered"),
        owned_paths=(
            "src/agent_orchestra/runtime/leader_loop.py",
            "src/agent_orchestra/contracts/execution.py",
            "src/agent_orchestra/tools",
            "tests",
        ),
        verification_commands=("python3 -m unittest discover -s tests -v",),
        keywords=("tool-capable code-edit worker", "code-edit worker", "owned_paths", "代码编辑"),
    ),
    _GapDefinition(
        gap_id="authority-integration",
        title="Authority Integration",
        summary=(
            "Authority mainline already supports request/wait and decision commit "
            "(grant/reroute/escalate/deny); continue connecting runtime decision, resume, "
            "and reroute evidence into objective/self-hosting semantics."
        ),
        rationale=(
            "Authority integration is now a residual hardening track: runtime decision/resume "
            "is active, but self-hosting evidence, policy cleanup, and final convergence "
            "semantics still need to catch up."
        ),
        team_name="Authority",
        acceptance_checks=("authority integration tests", "objective completion flow covered"),
        owned_paths=(
            "src/agent_orchestra/runtime/reducer.py",
            "src/agent_orchestra/runtime/group_runtime.py",
            "tests/test_runtime.py",
            "tests/test_delivery.py",
        ),
        verification_commands=(
            "python3 -m unittest tests.test_runtime -v",
            "python3 -m unittest tests.test_delivery -v",
        ),
        keywords=("authority root", "objective complete", "reducer"),
    ),
    _GapDefinition(
        gap_id="worker-lifecycle",
        title="Worker Lifecycle",
        summary="Add longer-lived leader/teammate supervision semantics such as idle and reactivate.",
        rationale="The runtime can loop at the coordination layer, but workers themselves are still one-turn only.",
        team_name="Runtime",
        acceptance_checks=("leader lifecycle tests", "worker resume semantics covered"),
        owned_paths=(
            "src/agent_orchestra/runtime/leader_loop.py",
            "src/agent_orchestra/runtime/worker_supervisor.py",
            "tests/test_leader_loop.py",
        ),
        verification_commands=("python3 -m unittest tests.test_leader_loop -v",),
        keywords=("leadersupervisor", "teammatesupervisor", "idle/reactivate"),
    ),
    _GapDefinition(
        gap_id="permission-broker",
        title="Formal Permission Broker",
        summary="Upgrade static permission decisions into a richer runtime-level broker contract.",
        rationale="Static allow/deny is enough for tests, but not for controlled multi-team execution.",
        team_name="Control",
        acceptance_checks=("permission broker tests",),
        owned_paths=(
            "src/agent_orchestra/tools/permission_protocol.py",
            "src/agent_orchestra/runtime/protocol_bridge.py",
            "tests/test_protocol_bridge.py",
        ),
        verification_commands=("python3 -m unittest tests.test_protocol_bridge -v",),
        keywords=("permissionbroker", "permission broker"),
    ),
    _GapDefinition(
        gap_id="reconnector",
        title="Identity And Reconnector",
        summary="Move from mailbox-cursor resume to stronger reconnect and identity restoration semantics.",
        rationale="The current system can resume runtime state, but not recover process-level execution cleanly.",
        team_name="Runtime",
        acceptance_checks=("reconnect tests",),
        owned_paths=(
            "src/agent_orchestra/runtime/protocol_bridge.py",
            "src/agent_orchestra/runtime/leader_loop.py",
            "tests/test_protocol_bridge.py",
        ),
        verification_commands=("python3 -m unittest tests.test_protocol_bridge -v",),
        keywords=("identityscope", "reconnector", "reconnect"),
    ),
    _GapDefinition(
        gap_id="protocol-bus",
        title="Protocol Bus",
        summary="Promote Redis mailbox and protocol routing into the primary multi-team coordination path.",
        rationale="The current mailbox loop is in-memory; a Redis-backed protocol bus is needed for a more durable runtime.",
        team_name="Control",
        acceptance_checks=("protocol bridge tests", "redis routing covered"),
        owned_paths=(
            "src/agent_orchestra/runtime/protocol_bridge.py",
            "src/agent_orchestra/bus/redis_bus.py",
            "tests/test_protocol_bridge.py",
        ),
        verification_commands=("python3 -m unittest tests.test_protocol_bridge -v",),
        keywords=("protocolbus", "redis mailbox"),
    ),
    _GapDefinition(
        gap_id="postgres-persistence",
        title="PostgreSQL Persistence",
        summary="Replace the schema-first skeleton with formal PostgreSQL CRUD persistence for orchestration state.",
        rationale="A durable production runtime needs real PostgreSQL storage instead of the current placeholder adapter.",
        team_name="Persistence",
        acceptance_checks=("postgres persistence tests",),
        owned_paths=(
            "src/agent_orchestra/storage/postgres/models.py",
            "src/agent_orchestra/storage/postgres/store.py",
        ),
        verification_commands=("python3 -m unittest discover -s tests -v",),
        keywords=("postgresql", "crud persistence", "postgres"),
    ),
    _GapDefinition(
        gap_id="planner-feedback",
        title="Planner Feedback Loop",
        summary="Add planner/replan memory and evaluator feedback so self-hosting can choose the next slice more adaptively.",
        rationale="The current self-hosting loop can execute a template, but it still relies on deterministic bootstrap planning.",
        team_name="Planning",
        acceptance_checks=("planner tests",),
        owned_paths=(
            "src/agent_orchestra/planning/template_planner.py",
            "src/agent_orchestra/self_hosting/bootstrap.py",
            "tests/test_planning.py",
        ),
        verification_commands=("python3 -m unittest tests.test_planning -v",),
        keywords=("planner", "replan", "feedback loop"),
    ),
    _GapDefinition(
        gap_id="sticky-provider-routing",
        title="Sticky Provider Health Routing",
        summary="Add provider health scoring, sticky route selection, and default provider memory across self-hosting rounds.",
        rationale="Per-assignment fallback exists now, but cross-round provider memory and health-aware default selection still do not.",
        team_name="Runtime",
        acceptance_checks=("provider routing tests", "sticky provider memory covered"),
        owned_paths=(
            "src/agent_orchestra/contracts/execution.py",
            "src/agent_orchestra/runtime/worker_supervisor.py",
            "tests/test_worker_reliability.py",
        ),
        verification_commands=("python3 -m unittest tests.test_worker_reliability -v",),
        keywords=(
            "sticky provider health routing",
            "default provider selection memory",
            "sticky provider routing",
            "provider health",
        ),
    ),
    _GapDefinition(
        gap_id="durable-supervisor-sessions",
        title="Durable Supervisor Sessions",
        summary="Extend leader and teammate supervision from first-stage idle/reactivate into durable cross-transport, cross-process session semantics.",
        rationale="Current worker lifecycle has first-stage session reuse, but not true durable long-lived supervision across transports and reconnects.",
        team_name="Runtime",
        acceptance_checks=("durable supervisor session tests", "cross-transport session semantics covered"),
        owned_paths=(
            "src/agent_orchestra/runtime/leader_loop.py",
            "src/agent_orchestra/runtime/worker_supervisor.py",
            "tests/test_leader_loop.py",
            "tests/test_worker_reliability.py",
        ),
        verification_commands=(
            "python3 -m unittest tests.test_leader_loop -v",
            "python3 -m unittest tests.test_worker_reliability -v",
        ),
        keywords=(
            "leadersupervisor",
            "teammatesupervisor",
            "durable reconnect",
            "cross-transport session",
            "durable leadersupervisor teammatesupervisor",
            "cross-transport session 语义",
            "durable supervisor sessions",
            "durable reconnect",
        ),
    ),
)


@dataclass(slots=True)
class SelfHostingGap:
    gap_id: str
    priority: int
    title: str
    summary: str
    rationale: str
    source_path: str
    source_line: int
    team_name: str
    acceptance_checks: tuple[str, ...] = ()
    owned_paths: tuple[str, ...] = ()
    verification_commands: tuple[str, ...] = ()
    depends_on: tuple[str, ...] = ()
    metadata: dict[str, object] = field(default_factory=dict)

    def to_workstream_template(self) -> WorkstreamTemplate:
        return WorkstreamTemplate(
            workstream_id=self.gap_id,
            title=self.title,
            summary=self.summary,
            team_name=self.team_name,
            depends_on=self.depends_on,
            acceptance_checks=self.acceptance_checks,
            budget_max_teammates=20,
            budget_max_iterations=2,
            metadata={
                "gap_id": self.gap_id,
                "rationale": self.rationale,
                "source_path": self.source_path,
                "source_line": self.source_line,
                "owned_paths": list(self.owned_paths),
                "verification_commands": list(self.verification_commands),
                **self.metadata,
            },
        )

    def to_dynamic_seed(self) -> dict[str, object]:
        return {
            "workstream_id": self.gap_id,
            "gap_id": self.gap_id,
            "title": self.title,
            "summary": self.summary,
            "team_name": self.team_name,
            "depends_on": list(self.depends_on),
            "acceptance_checks": list(self.acceptance_checks),
            "budget": {
                "max_teammates": 20,
                "max_iterations": 2,
                "max_tokens": None,
                "max_seconds": None,
            },
            "owned_paths": list(self.owned_paths),
            "verification_commands": list(self.verification_commands),
            "metadata": {
                "rationale": self.rationale,
                "source_path": self.source_path,
                "source_line": self.source_line,
                **self.metadata,
            },
        }

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(slots=True)
class SelfHostingBootstrapConfig:
    objective_id: str
    group_id: str
    max_workstreams: int = 2
    completed_gap_ids: tuple[str, ...] = ()
    preferred_gap_ids: tuple[str, ...] = ()
    leader_backend: str = "in_process"
    teammate_backend: str = "in_process"
    leader_profile_id: str | None = None
    teammate_profile_id: str | None = None
    title: str = "Advance Agent Orchestra Toward Self-Hosting"
    description: str = "Use the current runtime knowledge and orchestration stack to choose the next highest-leverage remaining development slice."
    knowledge_path: str | Path | None = None
    working_dir: str | None = None
    max_leader_turns: int = 2
    auto_run_teammates: bool = True
    keep_leader_session_idle: bool | None = None
    keep_teammate_session_idle: bool | None = None
    use_dynamic_planning: bool = False
    leader_idle_timeout_seconds: float | None = None
    leader_hard_timeout_seconds: float | None = None


def _policy_dict(policy: WorkerExecutionPolicy) -> dict[str, object]:
    return {
        "max_attempts": policy.max_attempts,
        "attempt_timeout_seconds": policy.attempt_timeout_seconds,
        "idle_timeout_seconds": policy.idle_timeout_seconds,
        "hard_timeout_seconds": policy.hard_timeout_seconds,
        "resume_on_timeout": policy.resume_on_timeout,
        "resume_on_failure": policy.resume_on_failure,
        "allow_relaunch": policy.allow_relaunch,
        "escalate_after_attempts": policy.escalate_after_attempts,
        "backoff_seconds": policy.backoff_seconds,
        "provider_unavailable_backoff_initial_seconds": (
            policy.provider_unavailable_backoff_initial_seconds
        ),
        "provider_unavailable_backoff_multiplier": policy.provider_unavailable_backoff_multiplier,
        "provider_unavailable_backoff_max_seconds": policy.provider_unavailable_backoff_max_seconds,
        "keep_session_idle": policy.keep_session_idle,
        "reactivate_idle_session": policy.reactivate_idle_session,
        "fallback_on_provider_unavailable": policy.fallback_on_provider_unavailable,
    }


def build_self_hosting_superleader_config(config: SelfHostingBootstrapConfig) -> SuperLeaderConfig:
    role_profiles: dict[str, WorkerRoleProfile] | None = None
    if config.leader_backend == "codex_cli" or config.teammate_backend == "codex_cli":
        default_profiles = build_runtime_role_profiles()
        leader_idle_timeout_seconds = (
            config.leader_idle_timeout_seconds
            if config.leader_idle_timeout_seconds is not None
            else 60.0
        )
        leader_hard_timeout_seconds = (
            config.leader_hard_timeout_seconds
            if config.leader_hard_timeout_seconds is not None
            else 1800.0
        )
        teammate_default_profile = default_profiles["teammate_codex_cli_code_edit"]
        teammate_idle_timeout_seconds = (
            leader_idle_timeout_seconds
            if config.leader_idle_timeout_seconds is not None
            else teammate_default_profile.fallback_idle_timeout_seconds
        )
        teammate_hard_timeout_seconds = (
            leader_hard_timeout_seconds
            if config.leader_hard_timeout_seconds is not None
            else teammate_default_profile.fallback_hard_timeout_seconds
        )
        role_profiles = {
            "leader_codex_cli_long_turn": align_role_profile_timeouts(
                default_profiles["leader_codex_cli_long_turn"],
                idle_timeout_seconds=leader_idle_timeout_seconds,
                hard_timeout_seconds=leader_hard_timeout_seconds,
            ),
            "teammate_codex_cli_code_edit": align_role_profile_timeouts(
                teammate_default_profile,
                idle_timeout_seconds=teammate_idle_timeout_seconds,
                hard_timeout_seconds=teammate_hard_timeout_seconds,
            ),
        }

    leader_profile_id = config.leader_profile_id
    if leader_profile_id is None:
        leader_profile_id = "leader_codex_cli_long_turn" if config.leader_backend == "codex_cli" else "leader_in_process_fast"
    teammate_profile_id = config.teammate_profile_id
    if teammate_profile_id is None:
        teammate_profile_id = (
            "teammate_codex_cli_code_edit"
            if config.teammate_backend == "codex_cli"
            else "teammate_in_process_fast"
        )
    keep_leader_session_idle = config.keep_leader_session_idle
    if keep_leader_session_idle is None:
        keep_leader_session_idle = config.leader_backend != "codex_cli"
    keep_teammate_session_idle = config.keep_teammate_session_idle
    if keep_teammate_session_idle is None:
        keep_teammate_session_idle = False
    return SuperLeaderConfig(
        leader_backend=None,
        teammate_backend=None,
        leader_execution_policy=None,
        teammate_execution_policy=None,
        leader_profile_id=leader_profile_id,
        teammate_profile_id=teammate_profile_id,
        role_profiles=role_profiles,
        max_leader_turns=config.max_leader_turns,
        auto_run_teammates=config.auto_run_teammates,
        allow_promptless_convergence=True,
        keep_leader_session_idle=keep_leader_session_idle,
        keep_teammate_session_idle=keep_teammate_session_idle,
        enable_planning_review=True,
        working_dir=config.working_dir,
    )


@dataclass(slots=True)
class SelfHostingTaskInstruction:
    task_id: str
    goal: str
    reason: str
    status: str
    owned_paths: tuple[str, ...] = ()
    verification_commands: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(slots=True)
class SelfHostingLaneInstruction:
    gap_id: str
    lane_id: str
    team_id: str
    leader_id: str
    delivery_status: str
    summary: str
    tasks: tuple[SelfHostingTaskInstruction, ...] = ()
    metadata: dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "gap_id": self.gap_id,
            "lane_id": self.lane_id,
            "team_id": self.team_id,
            "leader_id": self.leader_id,
            "delivery_status": self.delivery_status,
            "summary": self.summary,
            "tasks": [task.to_dict() for task in self.tasks],
            "metadata": dict(self.metadata),
        }


@dataclass(slots=True)
class SelfHostingInstructionPacket:
    objective_id: str
    objective_status: str
    selected_gap_ids: tuple[str, ...]
    completed_gap_ids: tuple[str, ...]
    remaining_gap_ids: tuple[str, ...]
    next_round_gap_ids: tuple[str, ...]
    next_round_prompt: str
    lane_instructions: tuple[SelfHostingLaneInstruction, ...] = ()
    metadata: dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "objective_id": self.objective_id,
            "objective_status": self.objective_status,
            "selected_gap_ids": list(self.selected_gap_ids),
            "completed_gap_ids": list(self.completed_gap_ids),
            "remaining_gap_ids": list(self.remaining_gap_ids),
            "next_round_gap_ids": list(self.next_round_gap_ids),
            "next_round_prompt": self.next_round_prompt,
            "lane_instructions": [item.to_dict() for item in self.lane_instructions],
            "metadata": dict(self.metadata),
        }


@dataclass(slots=True)
class SelfHostingRoundReport:
    inventory: tuple[SelfHostingGap, ...]
    template: ObjectiveTemplate
    run_result: SuperLeaderRunResult
    instruction_packet: SelfHostingInstructionPacket
    next_template: ObjectiveTemplate | None = None


def _definition_for_line(line: str) -> _GapDefinition:
    normalized = _normalize(line)
    best: _GapDefinition | None = None
    best_score = 0
    for definition in _GAP_DEFINITIONS:
        score = sum(1 for keyword in definition.keywords if keyword in normalized)
        if score > best_score:
            best = definition
            best_score = score
    if best is not None and best_score > 0:
        return best
    slug = _slugify(line)
    return _GapDefinition(
        gap_id=slug,
        title=line.strip(),
        summary=line.strip(),
        rationale="Derived from the current runtime priority list.",
        team_name="Runtime",
        acceptance_checks=("targeted tests pass",),
        owned_paths=(),
        verification_commands=("python3 -m unittest discover -s tests -v",),
    )


def _requires_delegation_validation(gap_id: str) -> bool:
    return gap_id == _DELEGATION_VALIDATION_GAP_ID


def _ordered_unique_strings(values) -> tuple[str, ...]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        if not isinstance(value, str) or not value:
            continue
        if value in seen:
            continue
        seen.add(value)
        ordered.append(value)
    return tuple(ordered)


def _metadata_int(metadata: dict[str, object] | None, key: str) -> int:
    if not isinstance(metadata, dict):
        return 0
    value = metadata.get(key)
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _host_owned_leader_session(lane_result) -> bool:
    coordinator_session = getattr(lane_result, "coordinator_session", None)
    if coordinator_session is None:
        return False
    metadata = getattr(coordinator_session, "metadata", {})
    if not isinstance(metadata, dict):
        metadata = {}
    return (
        getattr(coordinator_session, "role", None) == "leader"
        and metadata.get("runtime_view") == "leader_lane_session_graph"
        and metadata.get("launch_mode") == "leader_session_host"
    )


def _delegation_validation_metadata(lane_result) -> dict[str, object]:
    first_turn = lane_result.turns[0] if lane_result.turns else None
    runtime_native_teammate_execution_evidence = any(
        bool(getattr(turn, "teammate_execution_evidence", False))
        for turn in lane_result.turns
    )
    created_task_count = len(lane_result.created_task_ids)
    teammate_record_count = len(lane_result.teammate_records)
    completed_teammate_record_count = sum(
        1 for record in lane_result.teammate_records if record.status == WorkerStatus.COMPLETED
    )
    first_turn_created_task_ids = tuple(first_turn.created_task_ids) if first_turn is not None else ()
    first_turn_created_task_count = len(first_turn_created_task_ids)
    leader_turn_count = len(lane_result.turns)
    host_owned_leader_session = _host_owned_leader_session(lane_result)
    mailbox_followup_turns_used = _metadata_int(
        lane_result.delivery_state.metadata if lane_result.delivery_state is not None else None,
        "mailbox_followup_turns_used",
    )
    produced_mailbox_count = sum(len(turn.produced_mailbox_ids) for turn in lane_result.turns)
    consumed_mailbox_count = sum(len(turn.consumed_mailbox_ids) for turn in lane_result.turns)
    first_turn_produced_mailbox_ids = tuple(first_turn.produced_mailbox_ids) if first_turn is not None else ()
    followup_consumed_mailbox_ids = _ordered_unique_strings(
        envelope_id
        for turn in lane_result.turns[1:]
        for envelope_id in turn.consumed_mailbox_ids
    )
    first_turn_produced_mailbox_id_set = set(first_turn_produced_mailbox_ids)
    consumed_first_turn_mailbox_ids = tuple(
        envelope_id
        for envelope_id in followup_consumed_mailbox_ids
        if envelope_id in first_turn_produced_mailbox_id_set
    )
    leader_consumed_mailbox = bool(first_turn_produced_mailbox_ids) and (
        len(consumed_first_turn_mailbox_ids) == len(first_turn_produced_mailbox_ids)
    )
    teammate_execution_evidence = runtime_native_teammate_execution_evidence or (
        first_turn_created_task_count > 0
        and created_task_count > 0
        and teammate_record_count > 0
        and completed_teammate_record_count > 0
        and produced_mailbox_count > 0
    )
    convergence_without_extra_leader_turns = (
        host_owned_leader_session
        and first_turn_created_task_count > 0
        and teammate_execution_evidence
        and leader_turn_count == 1
        and mailbox_followup_turns_used == 0
    )
    return {
        "required": True,
        "validated": convergence_without_extra_leader_turns,
        "delivery_status": lane_result.delivery_state.status.value,
        "leader_turn_count": leader_turn_count,
        "mailbox_followup_turns_used": mailbox_followup_turns_used,
        "created_task_count": created_task_count,
        "first_turn_created_task_count": first_turn_created_task_count,
        "first_turn_created_task_ids": first_turn_created_task_ids,
        "teammate_record_count": teammate_record_count,
        "completed_teammate_record_count": completed_teammate_record_count,
        "produced_mailbox_count": produced_mailbox_count,
        "consumed_mailbox_count": consumed_mailbox_count,
        "first_turn_produced_mailbox_ids": first_turn_produced_mailbox_ids,
        "followup_consumed_mailbox_ids": followup_consumed_mailbox_ids,
        "consumed_first_turn_mailbox_ids": consumed_first_turn_mailbox_ids,
        "runtime_native_teammate_execution_evidence": runtime_native_teammate_execution_evidence,
        "teammate_execution_evidence": teammate_execution_evidence,
        "host_owned_leader_session": host_owned_leader_session,
        "convergence_without_extra_leader_turns": convergence_without_extra_leader_turns,
        "leader_consumed_mailbox": leader_consumed_mailbox,
    }


def _authority_mainline_from_completion(
    authority_completion: dict[str, object],
) -> dict[str, object]:
    validated = bool(authority_completion.get("validated"))
    request_count = int(authority_completion.get("request_count", 0))
    decision_counts = authority_completion.get("decision_counts")
    if not isinstance(decision_counts, dict):
        decision_counts = {}
    reroute_links = authority_completion.get("reroute_links")
    if not isinstance(reroute_links, list):
        reroute_links = []
    waiting_request_ids = authority_completion.get("waiting_request_ids")
    if not isinstance(waiting_request_ids, list):
        waiting_request_ids = []
    incomplete_request_ids = authority_completion.get("incomplete_request_ids")
    if not isinstance(incomplete_request_ids, list):
        incomplete_request_ids = []
    relay_pending_request_ids = authority_completion.get("relay_pending_request_ids")
    if not isinstance(relay_pending_request_ids, list):
        relay_pending_request_ids = []
    implemented_facts = [
        "Authority completion gate now uses structured runtime closure evidence.",
    ]
    if request_count > 0:
        implemented_facts.append("Authority request/decision lifecycle is observed from durable runtime truth.")
    if decision_counts:
        implemented_facts.append("Authority decision counts are aggregated from request-level closure snapshots.")
    if reroute_links:
        implemented_facts.append("Reroute closure tracks superseded/replacement task links.")
    residual_gaps: list[str] = []
    if waiting_request_ids:
        residual_gaps.append("Authority requests still waiting for final decision.")
    if relay_pending_request_ids:
        residual_gaps.append("Authority decisions exist but relay consumption/wake evidence is pending.")
    if incomplete_request_ids and not relay_pending_request_ids:
        residual_gaps.append("Authority closure evidence is incomplete for one or more requests.")
    if request_count == 0:
        residual_gaps.append("No authority request lifecycle evidence found in this lane yet.")
    if validated:
        residual_gaps = []
    return {
        "implemented_facts": implemented_facts,
        "residual_gaps": residual_gaps,
        "evidence": {
            "decision_counts": dict(decision_counts),
            "reroute_links": list(reroute_links),
            "waiting_request_ids": list(waiting_request_ids),
            "incomplete_request_ids": list(incomplete_request_ids),
            "relay_pending_request_ids": list(relay_pending_request_ids),
        },
    }


async def _authority_completion_metadata(
    runtime: GroupRuntime,
    lane_result,
    *,
    required: bool = False,
) -> dict[str, object] | None:
    group_id = getattr(lane_result.leader_round.runtime_task, "group_id", None)
    if not isinstance(group_id, str) or not group_id:
        return None
    lane_id = lane_result.leader_round.lane_id
    team_id = lane_result.leader_round.team_id
    tasks = await runtime.store.list_tasks(
        group_id,
        team_id=team_id,
        lane_id=lane_id,
        scope="team",
    )
    task_ids = {task.task_id for task in tasks}
    waiting_task_ids = _ordered_unique_strings(
        str(item)
        for item in lane_result.delivery_state.metadata.get("waiting_for_authority_task_ids", ())
        if isinstance(item, str) and item
    )
    outbox_records = await runtime.store.list_coordination_outbox_records()
    lane_outbox = [
        record
        for record in outbox_records
        if isinstance(record.metadata, dict)
        and record.metadata.get("group_id") == group_id
        and record.metadata.get("lane_id") == lane_id
        and record.metadata.get("team_id") == team_id
    ]
    sessions = [
        session
        for session in await runtime.store.list_agent_sessions()
        if (session.lane_id is None or session.lane_id == lane_id)
        and (session.team_id is None or session.team_id == team_id)
    ]

    authority_seen = bool(waiting_task_ids or lane_outbox)
    requests: dict[str, dict[str, object]] = {}

    def ensure_request(request_id: str) -> dict[str, object]:
        snapshot = requests.get(request_id)
        if snapshot is not None:
            return snapshot
        snapshot = {
            "request_id": request_id,
            "task_id": "",
            "worker_id": "",
            "boundary_class": "",
            "decision": "",
            "completion_status": "incomplete",
            "relay_subject": "",
            "relay_published": False,
            "relay_consumed": False,
            "wake_recorded": False,
            "replacement_task_id": "",
            "terminal_task_status": "",
            "_waiting": False,
            "_relay_subjects": set(),
            "_denied_blocked": False,
        }
        requests[request_id] = snapshot
        return snapshot

    for task in tasks:
        if task.authority_request_id or task.authority_request_payload or task.authority_decision_payload:
            authority_seen = True
        request_payload = (
            task.authority_request_payload
            if isinstance(task.authority_request_payload, dict)
            else {}
        )
        decision_payload = (
            task.authority_decision_payload
            if isinstance(task.authority_decision_payload, dict)
            else {}
        )
        request_id = (
            str(request_payload.get("request_id", "")).strip()
            or str(decision_payload.get("request_id", "")).strip()
            or str(task.authority_request_id or "").strip()
        )
        if not request_id:
            continue
        snapshot = ensure_request(request_id)
        if not snapshot["task_id"]:
            snapshot["task_id"] = task.task_id
        if not snapshot["worker_id"]:
            worker_id = request_payload.get("worker_id")
            if isinstance(worker_id, str) and worker_id:
                snapshot["worker_id"] = worker_id
            elif isinstance(task.authority_resume_target, str) and task.authority_resume_target:
                snapshot["worker_id"] = task.authority_resume_target
        if not snapshot["boundary_class"]:
            if isinstance(task.authority_boundary_class, str) and task.authority_boundary_class:
                snapshot["boundary_class"] = task.authority_boundary_class
            elif isinstance(decision_payload.get("scope_class"), str):
                snapshot["boundary_class"] = str(decision_payload.get("scope_class"))
        decision = decision_payload.get("decision")
        if isinstance(decision, str) and decision:
            snapshot["decision"] = decision.strip().lower()
        if isinstance(task.superseded_by_task_id, str) and task.superseded_by_task_id:
            snapshot["replacement_task_id"] = task.superseded_by_task_id
        if task.status.value == "waiting_for_authority" or task.task_id in waiting_task_ids:
            snapshot["_waiting"] = True
        snapshot["terminal_task_status"] = task.status.value
        if task.status.value == "blocked" and "authority.denied" in task.blocked_by:
            snapshot["_denied_blocked"] = True

    for record in lane_outbox:
        payload = record.payload if isinstance(record.payload, dict) else {}
        authority_request = payload.get("authority_request")
        if not isinstance(authority_request, dict):
            authority_request = {}
        authority_decision = payload.get("authority_decision")
        if not isinstance(authority_decision, dict):
            authority_decision = {}
        request_id = (
            str(authority_request.get("request_id", "")).strip()
            or str(authority_decision.get("request_id", "")).strip()
        )
        if not request_id:
            continue
        snapshot = ensure_request(request_id)
        if not snapshot["task_id"]:
            task_id = payload.get("task_id")
            if isinstance(task_id, str) and task_id:
                snapshot["task_id"] = task_id
            else:
                requested_task_id = authority_request.get("task_id")
                if isinstance(requested_task_id, str) and requested_task_id:
                    snapshot["task_id"] = requested_task_id
        if not snapshot["worker_id"]:
            worker_id = authority_request.get("worker_id")
            if isinstance(worker_id, str) and worker_id:
                snapshot["worker_id"] = worker_id
        if not snapshot["boundary_class"]:
            boundary_class = authority_decision.get("scope_class")
            if isinstance(boundary_class, str) and boundary_class:
                snapshot["boundary_class"] = boundary_class
        decision = authority_decision.get("decision")
        if isinstance(decision, str) and decision:
            snapshot["decision"] = decision.strip().lower()
        reroute_task_id = authority_decision.get("reroute_task_id")
        if isinstance(reroute_task_id, str) and reroute_task_id:
            snapshot["replacement_task_id"] = reroute_task_id
        replacement_task_id = payload.get("replacement_task_id")
        if isinstance(replacement_task_id, str) and replacement_task_id:
            snapshot["replacement_task_id"] = replacement_task_id
        subject = record.subject.strip().lower() if isinstance(record.subject, str) else ""
        if subject:
            relay_subjects = snapshot["_relay_subjects"]
            assert isinstance(relay_subjects, set)
            relay_subjects.add(subject)
            if not snapshot["relay_subject"]:
                snapshot["relay_subject"] = subject
            if subject in {"authority.decision", "authority.escalated"}:
                snapshot["relay_published"] = True

    for session in sessions:
        metadata = session.metadata if isinstance(session.metadata, dict) else {}
        request_id = metadata.get("authority_request_id")
        if not isinstance(request_id, str) or not request_id:
            continue
        snapshot = requests.get(request_id)
        if snapshot is None:
            continue
        waiting = metadata.get("authority_waiting")
        if waiting is True:
            snapshot["_waiting"] = True
        waiting_task_id = metadata.get("authority_waiting_task_id")
        if not snapshot["task_id"] and isinstance(waiting_task_id, str) and waiting_task_id:
            snapshot["task_id"] = waiting_task_id
        session_decision = metadata.get("authority_last_decision")
        if isinstance(session_decision, str) and session_decision:
            snapshot["relay_consumed"] = True
            if not snapshot["decision"]:
                snapshot["decision"] = session_decision.strip().lower()
        wake_at = metadata.get("last_wake_request_at")
        if isinstance(wake_at, str) and wake_at:
            snapshot["wake_recorded"] = True

    if not authority_seen and not required:
        return None

    requests_list: list[dict[str, object]] = []
    decision_counts: dict[str, int] = {}
    closed_request_ids: list[str] = []
    waiting_request_ids: list[str] = []
    incomplete_request_ids: list[str] = []
    relay_pending_request_ids: list[str] = []
    reroute_links: list[dict[str, str]] = []
    for request_id, raw_snapshot in requests.items():
        snapshot = dict(raw_snapshot)
        relay_subjects = snapshot.pop("_relay_subjects", set())
        waiting = bool(snapshot.pop("_waiting", False))
        denied_blocked = bool(snapshot.pop("_denied_blocked", False))
        decision = snapshot["decision"] if isinstance(snapshot.get("decision"), str) else ""
        relay_published = bool(snapshot.get("relay_published"))
        relay_consumed = bool(snapshot.get("relay_consumed"))
        wake_recorded = bool(snapshot.get("wake_recorded"))
        terminal_task_status = (
            str(snapshot.get("terminal_task_status"))
            if isinstance(snapshot.get("terminal_task_status"), str)
            else ""
        )
        if not snapshot.get("relay_subject") and isinstance(relay_subjects, set) and relay_subjects:
            snapshot["relay_subject"] = sorted(relay_subjects)[0]
        if not decision:
            completion_status = "waiting" if waiting else "incomplete"
        elif decision == "grant":
            resumable = terminal_task_status in {"pending", "in_progress", "completed"}
            if relay_published and relay_consumed and wake_recorded and resumable:
                completion_status = "grant_resumed"
            elif not relay_published or not relay_consumed:
                completion_status = "relay_pending"
            else:
                completion_status = "incomplete"
        elif decision == "reroute":
            replacement_task_id = (
                str(snapshot.get("replacement_task_id"))
                if isinstance(snapshot.get("replacement_task_id"), str)
                else ""
            )
            replacement_visible = bool(replacement_task_id and replacement_task_id in task_ids)
            if relay_published and replacement_visible:
                completion_status = "reroute_closed"
            elif not relay_published:
                completion_status = "relay_pending"
            else:
                completion_status = "incomplete"
        elif decision == "deny":
            if relay_published and (denied_blocked or terminal_task_status == "blocked"):
                completion_status = "deny_closed"
            elif not relay_published:
                completion_status = "relay_pending"
            else:
                completion_status = "incomplete"
        elif decision == "escalate":
            completion_status = "waiting"
        else:
            completion_status = "incomplete"
        snapshot["completion_status"] = completion_status
        requests_list.append(
            {
                "request_id": snapshot.get("request_id", ""),
                "task_id": snapshot.get("task_id", ""),
                "worker_id": snapshot.get("worker_id", ""),
                "boundary_class": snapshot.get("boundary_class", ""),
                "decision": decision,
                "completion_status": completion_status,
                "relay_subject": snapshot.get("relay_subject", ""),
                "relay_published": relay_published,
                "relay_consumed": relay_consumed,
                "wake_recorded": wake_recorded,
                "replacement_task_id": snapshot.get("replacement_task_id", ""),
                "terminal_task_status": terminal_task_status,
            }
        )
        if decision:
            decision_counts[decision] = int(decision_counts.get(decision, 0)) + 1
        if completion_status in {"grant_resumed", "reroute_closed", "deny_closed"}:
            closed_request_ids.append(request_id)
        elif completion_status == "waiting":
            waiting_request_ids.append(request_id)
        else:
            incomplete_request_ids.append(request_id)
            if completion_status == "relay_pending":
                relay_pending_request_ids.append(request_id)
        replacement_task_id = snapshot.get("replacement_task_id")
        task_id = snapshot.get("task_id")
        if (
            completion_status == "reroute_closed"
            and isinstance(task_id, str)
            and task_id
            and isinstance(replacement_task_id, str)
            and replacement_task_id
        ):
            reroute_links.append(
                {
                    "superseded_task_id": task_id,
                    "replacement_task_id": replacement_task_id,
                }
            )

    requests_list.sort(key=lambda item: str(item.get("request_id", "")))
    request_count = len(requests_list)
    validated = request_count > 0 and not waiting_request_ids and not incomplete_request_ids
    completion_status = (
        "validated"
        if validated
        else ("waiting" if waiting_request_ids and not incomplete_request_ids else "incomplete")
    )
    completion = {
        "validated": validated,
        "completion_status": completion_status,
        "request_count": request_count,
        "decision_counts": decision_counts,
        "closed_request_ids": list(_ordered_unique_strings(closed_request_ids)),
        "waiting_request_ids": list(_ordered_unique_strings(waiting_request_ids)),
        "incomplete_request_ids": list(_ordered_unique_strings(incomplete_request_ids)),
        "relay_pending_request_ids": list(_ordered_unique_strings(relay_pending_request_ids)),
        "reroute_links": reroute_links,
        "requests": requests_list,
    }
    if request_count == 0 and not required:
        return None
    return completion


async def _team_parallel_validation_metadata(runtime: GroupRuntime, lane_result) -> dict[str, object]:
    delegation = _delegation_validation_metadata(lane_result)
    runtime_native_autonomous_claim_task_ids = _ordered_unique_strings(
        task_id
        for turn in lane_result.turns
        for task_id in getattr(turn, "autonomous_claimed_task_ids", ())
    )
    runtime_native_directed_claim_task_ids = _ordered_unique_strings(
        task_id
        for turn in lane_result.turns
        for task_id in getattr(turn, "directed_claimed_task_ids", ())
    )
    tasks = []
    for task_id in lane_result.created_task_ids:
        task = await runtime.store.get_task(task_id)
        if task is not None:
            tasks.append(task)
    autonomous_claim_tasks = [
        task
        for task in tasks
        if getattr(task, 'claim_source', None) in {'resident_task_list_claim', 'autonomous_claim'}
    ]
    session_counts: dict[str, int] = {}
    for task in tasks:
        session_id = getattr(task, 'claim_session_id', None)
        if not isinstance(session_id, str) or not session_id:
            continue
        session_counts[session_id] = session_counts.get(session_id, 0) + 1
    resident_session_task_count = sum(count for count in session_counts.values() if count > 1)
    resident_session_reuse_count = sum(1 for count in session_counts.values() if count > 1)
    reused_session_ids = tuple(
        session_id
        for session_id, count in session_counts.items()
        if count > 1
    )
    metadata = lane_result.delivery_state.metadata or {}
    mailbox_followup_turns_used = _metadata_int(metadata, 'mailbox_followup_turns_used')
    mailbox_followup_turn_limit = metadata.get('mailbox_followup_turn_limit')
    mailbox_progress_evidence = mailbox_followup_turns_used > 0
    teammate_execution_evidence = (
        bool(runtime_native_autonomous_claim_task_ids)
        or (
            bool(autonomous_claim_tasks)
            and resident_session_task_count > 0
            and bool(delegation['teammate_execution_evidence'])
        )
    )
    validated = (
        teammate_execution_evidence
        and resident_session_task_count > 0
    )
    return {
        'required': True,
        'validated': validated,
        'delivery_status': lane_result.delivery_state.status.value,
        'created_task_count': len(tasks),
        'autonomous_claim_task_count': len(autonomous_claim_tasks),
        'resident_session_count': len(session_counts),
        'resident_session_task_count': resident_session_task_count,
        'resident_session_reuse_count': resident_session_reuse_count,
        'resident_sessions_reused': reused_session_ids,
        'runtime_native_autonomous_claim_task_ids': runtime_native_autonomous_claim_task_ids,
        'runtime_native_directed_claim_task_ids': runtime_native_directed_claim_task_ids,
        'teammate_execution_evidence': teammate_execution_evidence,
        'host_owned_leader_session': bool(delegation['host_owned_leader_session']),
        'convergence_without_extra_leader_turns': bool(
            delegation['convergence_without_extra_leader_turns']
        ),
        'leader_consumed_mailbox': bool(delegation['leader_consumed_mailbox']),
        'produced_mailbox_count': delegation['produced_mailbox_count'],
        'consumed_mailbox_count': delegation['consumed_mailbox_count'],
        'mailbox_followup_turns_used': mailbox_followup_turns_used,
        'mailbox_followup_turn_limit': mailbox_followup_turn_limit,
        'mailbox_progress_evidence': mailbox_progress_evidence,
        'mailbox_cursor': lane_result.mailbox_cursor,
    }


def _protocol_recovery_evidence(lane_result) -> dict[str, object]:
    required_streams = ("lifecycle", "session", "control", "takeover", "mailbox")
    events = []
    for record in tuple(lane_result.leader_records) + tuple(lane_result.teammate_records):
        events.extend(protocol_bus_events_from_worker_record(record))
    stream_set = {
        str(getattr(event, "stream", ""))
        for event in events
        if isinstance(getattr(event, "stream", None), str) and getattr(event, "stream")
    }
    observed_streams = tuple(sorted(stream_set))
    missing_streams = tuple(stream for stream in required_streams if stream not in stream_set)
    takeover_event_count = 0
    reattach_event_count = 0
    cursor_ready_count = 0
    for event in events:
        event_type = str(getattr(event, "event_type", ""))
        stream = str(getattr(event, "stream", ""))
        payload = getattr(event, "payload", {})
        metadata = getattr(event, "metadata", {})
        if not isinstance(payload, dict):
            payload = {}
        if not isinstance(metadata, dict):
            metadata = {}
        if stream == "takeover" or event_type.startswith("session.takeover_"):
            takeover_event_count += 1
        has_reattach_flag = bool(payload.get("reattach")) or bool(metadata.get("reattach"))
        if event_type in {"session.takeover_completed", "session.reattach_completed"} or has_reattach_flag:
            reattach_event_count += 1
        cursor = getattr(event, "cursor", {})
        if isinstance(cursor, dict):
            offset = cursor.get("offset")
            if isinstance(offset, str) and offset:
                cursor_ready_count += 1
    return {
        "delivery_status": lane_result.delivery_state.status.value,
        "protocol_bus_event_count": len(events),
        "observed_streams": observed_streams,
        "missing_streams": missing_streams,
        "takeover_event_count": takeover_event_count,
        "reattach_event_count": reattach_event_count,
        "cursor_ready_count": cursor_ready_count,
    }


def _durable_supervisor_validation_metadata(lane_result) -> dict[str, object]:
    evidence = _protocol_recovery_evidence(lane_result)
    validated = evidence["reattach_event_count"] > 0
    return {
        "required": True,
        "validated": validated,
        **evidence,
    }


def _reconnector_validation_metadata(lane_result) -> dict[str, object]:
    evidence = _protocol_recovery_evidence(lane_result)
    validated = evidence["takeover_event_count"] > 0 and evidence["reattach_event_count"] > 0
    return {
        "required": True,
        "validated": validated,
        **evidence,
    }


def _protocol_bus_validation_metadata(lane_result) -> dict[str, object]:
    evidence = _protocol_recovery_evidence(lane_result)
    validated = (
        evidence["protocol_bus_event_count"] > 0
        and not evidence["missing_streams"]
        and evidence["cursor_ready_count"] > 0
    )
    return {
        "required": True,
        "validated": validated,
        **evidence,
    }


def _planning_review_status_metadata(
    objective_metadata: dict[str, object] | None,
    *,
    required: bool = False,
) -> dict[str, object] | None:
    if not isinstance(objective_metadata, dict):
        objective_metadata = {}
    raw_planning_review = objective_metadata.get("planning_review")
    planning_review = dict(raw_planning_review) if isinstance(raw_planning_review, dict) else {}
    raw_activation_gate = objective_metadata.get("activation_gate")
    activation_gate = dict(raw_activation_gate) if isinstance(raw_activation_gate, dict) else {}
    nested_activation_gate = planning_review.get("activation_gate")
    if not activation_gate and isinstance(nested_activation_gate, dict):
        activation_gate = dict(nested_activation_gate)
    if not planning_review and not activation_gate and not required:
        return None
    if activation_gate:
        planning_review["activation_gate"] = dict(activation_gate)
    gate_status = str(planning_review.get("activation_gate", {}).get("status", "")).strip().lower()
    blockers = planning_review.get("activation_gate", {}).get("blockers")
    if not isinstance(blockers, list):
        blockers = list(blockers) if isinstance(blockers, tuple) else []
    planning_review["activation_gate_status"] = gate_status
    planning_review["activation_gate_blocker_count"] = len(blockers)
    planning_review["validated"] = gate_status == "ready_for_activation"
    return planning_review


def _superleader_runtime_status_metadata(
    objective_metadata: dict[str, object] | None,
) -> dict[str, object] | None:
    if not isinstance(objective_metadata, dict):
        objective_metadata = {}
    raw_coordination = (
        deepcopy(objective_metadata["coordination"])
        if isinstance(objective_metadata.get("coordination"), dict)
        else {}
    )
    raw_message_runtime = (
        deepcopy(objective_metadata["message_runtime"])
        if isinstance(objective_metadata.get("message_runtime"), dict)
        else {}
    )
    resident_live_view = (
        deepcopy(objective_metadata["resident_live_view"])
        if isinstance(objective_metadata.get("resident_live_view"), dict)
        else {}
    )
    if not raw_coordination and not raw_message_runtime and not resident_live_view:
        return None
    runtime_status: dict[str, object] = {}
    if resident_live_view:
        runtime_status["resident_truth_source"] = "resident_live_view"
        runtime_status["resident_live_view"] = resident_live_view
        preferred_coordination = dict(raw_coordination)
        resident_coordination = resident_live_view.get("objective_coordination")
        if not preferred_coordination and isinstance(resident_coordination, dict):
            preferred_coordination = deepcopy(resident_coordination)
        coordination_overrides = {
            "active_lane_ids": resident_live_view.get("primary_active_lane_ids", resident_live_view.get("active_lane_ids")),
            "pending_lane_ids": resident_live_view.get("primary_pending_lane_ids", resident_live_view.get("pending_lane_ids")),
            "completed_lane_ids": resident_live_view.get(
                "primary_completed_lane_ids", resident_live_view.get("completed_lane_ids")
            ),
            "failed_lane_ids": resident_live_view.get("primary_failed_lane_ids", resident_live_view.get("failed_lane_ids")),
            "blocked_lane_ids": resident_live_view.get(
                "primary_blocked_lane_ids", resident_live_view.get("blocked_lane_ids")
            ),
            "active_lane_session_ids": resident_live_view.get(
                "primary_active_lane_session_ids", resident_live_view.get("active_lane_session_ids")
            ),
            "lane_statuses": resident_live_view.get(
                "primary_lane_statuses", resident_live_view.get("lane_statuses")
            ),
            "lane_truth_sources": resident_live_view.get("lane_truth_sources"),
            "runtime_native_lane_ids": resident_live_view.get("runtime_native_lane_ids"),
            "fallback_lane_ids": resident_live_view.get("fallback_lane_ids"),
            "host_owned_lane_session_count": resident_live_view.get("host_owned_lane_session_count"),
            "lane_count": resident_live_view.get("lane_count"),
            "host_stepped_lane_ids": resident_live_view.get("host_stepped_lane_ids"),
            "lane_host_step_counts": resident_live_view.get("lane_host_step_counts"),
        }
        for key, value in coordination_overrides.items():
            if value is None:
                continue
            preferred_coordination[key] = deepcopy(value)
        if preferred_coordination:
            runtime_status["coordination"] = preferred_coordination

        preferred_message_runtime = dict(raw_message_runtime)
        resident_message_runtime = resident_live_view.get("objective_message_runtime")
        if not preferred_message_runtime and isinstance(resident_message_runtime, dict):
            preferred_message_runtime = deepcopy(resident_message_runtime)
        elif isinstance(resident_message_runtime, dict):
            preferred_message_runtime.update(deepcopy(resident_message_runtime))
        if "objective_shared_digest_count" in resident_live_view:
            preferred_message_runtime["objective_shared_digest_count"] = resident_live_view[
                "objective_shared_digest_count"
            ]
        if "objective_shared_digest_envelope_ids" in resident_live_view:
            preferred_message_runtime["objective_shared_digest_envelope_ids"] = deepcopy(
                resident_live_view["objective_shared_digest_envelope_ids"]
            )
        if preferred_message_runtime:
            runtime_status["message_runtime"] = preferred_message_runtime
    else:
        runtime_status["resident_truth_source"] = "objective_metadata"
        if raw_coordination:
            runtime_status["coordination"] = raw_coordination
        if raw_message_runtime:
            runtime_status["message_runtime"] = raw_message_runtime
    return runtime_status or None


def load_runtime_gap_inventory(path: str | Path | None = None) -> tuple[SelfHostingGap, ...]:
    source = Path(path) if path is not None else _default_knowledge_path()
    text = source.read_text(encoding="utf-8")
    lines = text.splitlines()
    in_priority_section = False
    inventory: list[SelfHostingGap] = []
    pattern = re.compile(r"^\s*(\d+)\.\s+(.*\S)\s*$")

    for line_number, line in enumerate(lines, start=1):
        if line.startswith("## ") and "建议优先级" in line:
            in_priority_section = True
            continue
        if in_priority_section and line.startswith("## "):
            break
        if not in_priority_section:
            continue
        match = pattern.match(line)
        if match is None:
            continue
        priority = int(match.group(1))
        raw_summary = match.group(2)
        definition = _definition_for_line(raw_summary)
        inventory.append(
            SelfHostingGap(
                gap_id=definition.gap_id,
                priority=priority,
                title=definition.title,
                summary=definition.summary,
                rationale=definition.rationale,
                source_path=str(source),
                source_line=line_number,
                team_name=definition.team_name,
                acceptance_checks=definition.acceptance_checks,
                owned_paths=definition.owned_paths,
                verification_commands=definition.verification_commands,
                depends_on=definition.depends_on,
                metadata={"source_summary": raw_summary},
            )
        )
    return tuple(sorted(inventory, key=lambda item: item.priority))


def build_self_hosting_template(
    *,
    inventory: tuple[SelfHostingGap, ...],
    config: SelfHostingBootstrapConfig,
) -> ObjectiveTemplate:
    remaining = [item for item in inventory if item.gap_id not in set(config.completed_gap_ids)]
    if config.preferred_gap_ids:
        gap_by_id = {item.gap_id: item for item in remaining}
        preferred = [gap_by_id[gap_id] for gap_id in config.preferred_gap_ids if gap_id in gap_by_id]
        selected = preferred[: max(config.max_workstreams, 0)]
    else:
        selected = remaining[: max(config.max_workstreams, 0)]
    description = config.description
    if selected:
        description = f"{config.description} This round focuses on: {', '.join(item.title for item in selected)}."
    metadata: dict[str, object] = {
        "selected_gap_ids": [item.gap_id for item in selected],
        "completed_gap_ids": list(config.completed_gap_ids),
    }
    workstreams = tuple(item.to_workstream_template() for item in selected)
    if config.use_dynamic_planning:
        metadata.update(
            {
                "planning_mode": "dynamic_superleader",
                "dynamic_workstream_seeds": [item.to_dynamic_seed() for item in selected],
            }
        )
        workstreams = ()
    return ObjectiveTemplate(
        objective_id=config.objective_id,
        group_id=config.group_id,
        title=config.title,
        description=description,
        success_metrics=("targeted tests pass", "full suite stays green"),
        hard_constraints=("keep library-first", "preserve knowledge package accuracy"),
        global_budget={"max_teams": config.max_workstreams, "max_iterations": config.max_leader_turns},
        workstreams=workstreams,
        metadata=metadata,
    )


def render_self_hosting_instruction_packet(packet: SelfHostingInstructionPacket) -> str:
    lines = [
        "# Self-Hosting Instruction Packet",
        "",
        f"- Objective: `{packet.objective_id}`",
        f"- Objective Status: `{packet.objective_status}`",
        f"- Selected Gaps: {', '.join(packet.selected_gap_ids) or '(none)'}",
        f"- Completed Gaps: {', '.join(packet.completed_gap_ids) or '(none)'}",
        f"- Remaining Gaps: {', '.join(packet.remaining_gap_ids) or '(none)'}",
        f"- Next Round Gaps: {', '.join(packet.next_round_gap_ids) or '(none)'}",
        "",
        "## Next Round",
        "",
        packet.next_round_prompt,
    ]
    authority_completion_status = packet.metadata.get("authority_completion_status")
    if isinstance(authority_completion_status, dict):
        lines.extend(["", "## Authority Completion"])
        lines.append(
            "- Summary: "
            f"validated={bool(authority_completion_status.get('validated'))}, "
            f"completion_status={authority_completion_status.get('completion_status', 'incomplete')}, "
            f"request_count={authority_completion_status.get('request_count', 0)}"
        )
        decision_counts = authority_completion_status.get("decision_counts")
        if isinstance(decision_counts, dict) and decision_counts:
            rendered_counts = ", ".join(
                f"{key}={value}" for key, value in sorted(decision_counts.items())
            )
            lines.append(f"- Decision Counts: {rendered_counts}")
        for label, key in (
            ("Closed Request IDs", "closed_request_ids"),
            ("Waiting Request IDs", "waiting_request_ids"),
            ("Incomplete Request IDs", "incomplete_request_ids"),
            ("Relay Pending Request IDs", "relay_pending_request_ids"),
        ):
            values = authority_completion_status.get(key)
            if isinstance(values, list):
                rendered = ", ".join(str(item) for item in values) or "(none)"
                lines.append(f"- {label}: {rendered}")
    elif isinstance(packet.metadata.get("authority_mainline_status"), dict):
        authority_mainline_status = packet.metadata.get("authority_mainline_status")
        lines.extend(["", "## Authority Mainline"])
        assert isinstance(authority_mainline_status, dict)
        implemented_facts = authority_mainline_status.get("implemented_facts")
        if isinstance(implemented_facts, list) and implemented_facts:
            lines.extend(["", "- Implemented Facts:"])
            for item in implemented_facts:
                lines.append(f"  - {item}")
        residual_gaps = authority_mainline_status.get("residual_gaps")
        if isinstance(residual_gaps, list) and residual_gaps:
            lines.extend(["", "- Residual Gaps:"])
            for item in residual_gaps:
                lines.append(f"  - {item}")
    planning_review_status = packet.metadata.get("planning_review_status")
    if isinstance(planning_review_status, dict):
        lines.extend(["", "## Planning Review"])
        activation_gate = planning_review_status.get("activation_gate")
        if not isinstance(activation_gate, dict):
            activation_gate = {}
        lines.append(
            "- Summary: "
            f"enabled={bool(planning_review_status.get('enabled'))}, "
            f"validated={bool(planning_review_status.get('validated'))}, "
            f"planning_round_id={planning_review_status.get('planning_round_id', '(none)')}"
        )
        if activation_gate:
            lines.append(
                "- Activation Gate: "
                f"status={activation_gate.get('status', 'unknown')}, "
                f"summary={activation_gate.get('summary', '')}"
            )
            blockers = activation_gate.get("blockers")
            if isinstance(blockers, list):
                rendered = ", ".join(str(item) for item in blockers) or "(none)"
                lines.append(f"- Activation Gate Blockers: {rendered}")
    superleader_runtime_status = packet.metadata.get("superleader_runtime_status")
    if isinstance(superleader_runtime_status, dict):
        lines.extend(["", "## SuperLeader Runtime"])
        coordination = superleader_runtime_status.get("coordination")
        if not isinstance(coordination, dict):
            coordination = {}
        message_runtime = superleader_runtime_status.get("message_runtime")
        if not isinstance(message_runtime, dict):
            message_runtime = {}
        lines.append(
            "- Summary: "
            f"resident_truth_source={superleader_runtime_status.get('resident_truth_source', 'objective_metadata')}, "
            f"active_lane_count={len(coordination.get('active_lane_ids', [])) if isinstance(coordination.get('active_lane_ids'), list) else 0}, "
            f"active_lane_session_count={len(coordination.get('active_lane_session_ids', [])) if isinstance(coordination.get('active_lane_session_ids'), list) else 0}, "
            f"objective_shared_digest_count={message_runtime.get('objective_shared_digest_count', 0)}"
        )
        for label, key in (
            ("Active Lanes", "active_lane_ids"),
            ("Pending Lanes", "pending_lane_ids"),
            ("Completed Lanes", "completed_lane_ids"),
            ("Runtime-Native Lanes", "runtime_native_lane_ids"),
            ("Fallback Lanes", "fallback_lane_ids"),
            ("Active Lane Sessions", "active_lane_session_ids"),
        ):
            values = coordination.get(key)
            if isinstance(values, list):
                rendered = ", ".join(str(item) for item in values) or "(none)"
                lines.append(f"- {label}: {rendered}")
    lines.extend(["", "## Lane Instructions"])
    if not packet.lane_instructions:
        lines.extend(["", "- No lane instructions available."])
        return "\n".join(lines)

    for lane in packet.lane_instructions:
        lines.extend(
            [
                "",
                f"### {lane.gap_id}",
                "",
                f"- Lane: `{lane.lane_id}`",
                f"- Team: `{lane.team_id}`",
                f"- Leader: `{lane.leader_id}`",
                f"- Delivery Status: `{lane.delivery_status}`",
                f"- Summary: {lane.summary}",
            ]
        )
        if not lane.tasks:
            lines.append("- Tasks: none")
        else:
            lines.append("- Tasks:")
            for task in lane.tasks:
                lines.append(f"  - `{task.task_id}`: {task.goal}")
                lines.append(f"    - Reason: {task.reason}")
                lines.append(f"    - Status: `{task.status}`")
                if task.owned_paths:
                    lines.append(f"    - Owned Paths: {', '.join(task.owned_paths)}")
                if task.verification_commands:
                    lines.append(f"    - Verification: {', '.join(task.verification_commands)}")
        validation = lane.metadata.get("delegation_validation")
        if isinstance(validation, dict):
            status = "passed" if validation.get("validated") else "failed"
            lines.append(f"- Delegation Validation: {status}")
            lines.append(
                "- Delegation Evidence: "
                f"created_tasks={validation.get('created_task_count', 0)}, "
                f"completed_teammates={validation.get('completed_teammate_record_count', 0)}, "
                f"teammate_execution_evidence={bool(validation.get('teammate_execution_evidence'))}, "
                f"host_owned_leader_session={bool(validation.get('host_owned_leader_session'))}, "
                f"convergence_without_extra_leader_turns="
                f"{bool(validation.get('convergence_without_extra_leader_turns'))}"
            )
        planning_review = lane.metadata.get("planning_review")
        if isinstance(planning_review, dict):
            lines.append("- Planning Review:")
            lines.append(
                "  - Summary: "
                f"enabled={bool(planning_review.get('enabled'))}, "
                f"validated={bool(planning_review.get('validated'))}, "
                f"planning_round_id={planning_review.get('planning_round_id', '(none)')}"
            )
            activation_gate = planning_review.get("activation_gate")
            if isinstance(activation_gate, dict):
                lines.append(
                    "  - Activation Gate: "
                    f"status={activation_gate.get('status', 'unknown')}, "
                    f"summary={activation_gate.get('summary', '')}"
                )
                blockers = activation_gate.get("blockers")
                if isinstance(blockers, list):
                    rendered = ", ".join(str(item) for item in blockers) or "(none)"
                    lines.append(f"  - Activation Gate Blockers: {rendered}")
        superleader_runtime = lane.metadata.get("superleader_runtime_status")
        if isinstance(superleader_runtime, dict):
            coordination = superleader_runtime.get("coordination")
            if not isinstance(coordination, dict):
                coordination = {}
            message_runtime = superleader_runtime.get("message_runtime")
            if not isinstance(message_runtime, dict):
                message_runtime = {}
            lines.append("- SuperLeader Runtime:")
            lines.append(
                "  - Summary: "
                f"resident_truth_source={superleader_runtime.get('resident_truth_source', 'objective_metadata')}, "
                f"active_lane_ids={coordination.get('active_lane_ids', [])}, "
                f"active_lane_session_ids={coordination.get('active_lane_session_ids', [])}, "
                f"objective_shared_digest_count={message_runtime.get('objective_shared_digest_count', 0)}"
            )
        authority_completion = lane.metadata.get("authority_completion")
        if isinstance(authority_completion, dict):
            lines.append("- Authority Completion:")
            lines.append(
                "  - Summary: "
                f"validated={bool(authority_completion.get('validated'))}, "
                f"completion_status={authority_completion.get('completion_status', 'incomplete')}, "
                f"request_count={authority_completion.get('request_count', 0)}"
            )
            decision_counts = authority_completion.get("decision_counts")
            if isinstance(decision_counts, dict) and decision_counts:
                rendered_counts = ", ".join(
                    f"{key}={value}" for key, value in sorted(decision_counts.items())
                )
                lines.append(f"  - decision_counts={rendered_counts}")
            reroute_links = authority_completion.get("reroute_links")
            if isinstance(reroute_links, list) and reroute_links:
                lines.append("  - Reroute Links:")
                for item in reroute_links:
                    if not isinstance(item, dict):
                        continue
                    superseded_task_id = item.get("superseded_task_id")
                    replacement_task_id = item.get("replacement_task_id")
                    if superseded_task_id and replacement_task_id:
                        lines.append(f"    - {superseded_task_id} -> {replacement_task_id}")
            requests = authority_completion.get("requests")
            if isinstance(requests, list) and requests:
                lines.append("  - Request Statuses:")
                for item in requests:
                    if not isinstance(item, dict):
                        continue
                    request_id = item.get("request_id", "")
                    request_status = item.get("completion_status", "incomplete")
                    if request_id:
                        lines.append(f"    - {request_id}: {request_status}")
            for label, key in (
                ("Closed Request IDs", "closed_request_ids"),
                ("Waiting Request IDs", "waiting_request_ids"),
                ("Incomplete Request IDs", "incomplete_request_ids"),
                ("Relay Pending Request IDs", "relay_pending_request_ids"),
            ):
                values = authority_completion.get(key)
                if isinstance(values, list):
                    rendered = ", ".join(str(item) for item in values) or "(none)"
                    lines.append(f"  - {label}: {rendered}")
        authority_mainline = lane.metadata.get("authority_mainline")
        if not isinstance(authority_completion, dict) and isinstance(authority_mainline, dict):
            lines.append("- Authority Mainline:")
            implemented_facts = authority_mainline.get("implemented_facts")
            if isinstance(implemented_facts, list) and implemented_facts:
                lines.append("  - Implemented Facts:")
                for item in implemented_facts:
                    lines.append(f"    - {item}")
    return "\n".join(lines)


def write_self_hosting_instruction_packet(path: str | Path, packet: SelfHostingInstructionPacket) -> Path:
    target = Path(path)
    if target.suffix.lower() == ".json":
        target.write_text(json.dumps(packet.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        target.write_text(render_self_hosting_instruction_packet(packet), encoding="utf-8")
    return target


class SelfHostingBootstrapCoordinator:
    def __init__(
        self,
        *,
        runtime: GroupRuntime,
        planner: Planner | None = None,
        superleader: SuperLeaderRuntime | None = None,
    ) -> None:
        self.runtime = runtime
        self.planner = planner or DynamicSuperLeaderPlanner()
        self.superleader = superleader or SuperLeaderRuntime(runtime=runtime)

    async def seed_template(self, config: SelfHostingBootstrapConfig) -> ObjectiveTemplate:
        inventory = load_runtime_gap_inventory(config.knowledge_path)
        return build_self_hosting_template(inventory=inventory, config=config)

    async def _ensure_group(self, group_id: str) -> None:
        if await self.runtime.store.get_group(group_id) is None:
            await self.runtime.create_group(group_id)

    async def _build_lane_instruction(
        self,
        gap_id: str,
        lane_result,
        *,
        metadata: dict[str, object] | None = None,
    ) -> SelfHostingLaneInstruction:
        tasks: list[SelfHostingTaskInstruction] = []
        for task_id in lane_result.created_task_ids:
            task = await self.runtime.store.get_task(task_id)
            if task is None:
                continue
            tasks.append(
                SelfHostingTaskInstruction(
                    task_id=task.task_id,
                    goal=task.goal,
                    reason=task.reason,
                    status=task.status.value,
                    owned_paths=tuple(task.owned_paths),
                    verification_commands=tuple(task.verification_commands),
                )
            )
        return SelfHostingLaneInstruction(
            gap_id=gap_id,
            lane_id=lane_result.leader_round.lane_id,
            team_id=lane_result.leader_round.team_id,
            leader_id=lane_result.leader_round.leader_task.leader_id,
            delivery_status=lane_result.delivery_state.status.value,
            summary=lane_result.delivery_state.summary,
            tasks=tuple(tasks),
            metadata=dict(metadata or {}),
        )

    async def run_bootstrap_round(self, config: SelfHostingBootstrapConfig) -> SelfHostingRoundReport:
        inventory = load_runtime_gap_inventory(config.knowledge_path)
        template = build_self_hosting_template(inventory=inventory, config=config)
        await self._ensure_group(config.group_id)
        selected_gap_ids = tuple(
            str(item)
            for item in template.metadata.get("selected_gap_ids", [])
            if str(item)
        ) or tuple(item.workstream_id for item in template.workstreams)
        superleader_config = build_self_hosting_superleader_config(config)
        run_result = await self.superleader.run_template(
            planner=self.planner,
            template=template,
            config=superleader_config,
        )

        completed_gap_ids = list(config.completed_gap_ids)
        delegation_validation: dict[str, dict[str, object]] = {}
        validation_failed_gap_ids: list[str] = []
        lane_instructions_list: list[SelfHostingLaneInstruction] = []
        authority_mainline_facts: list[str] = []
        authority_mainline_residuals: list[str] = []
        planning_review_status = _planning_review_status_metadata(
            run_result.objective_state.metadata,
            required=True,
        )
        superleader_runtime_status = _superleader_runtime_status_metadata(
            run_result.objective_state.metadata,
        )
        authority_completion_request_count = 0
        authority_completion_decision_counts: dict[str, int] = {}
        authority_completion_closed_request_ids: list[str] = []
        authority_completion_waiting_request_ids: list[str] = []
        authority_completion_incomplete_request_ids: list[str] = []
        authority_completion_relay_pending_request_ids: list[str] = []
        authority_completion_required_lane_count = 0
        authority_completion_required_validated_count = 0
        authority_completion_lane_statuses: dict[str, str] = {}
        for lane_result in run_result.lane_results:
            gap_id = lane_result.leader_round.lane_id
            lane_metadata: dict[str, object] = {}
            validation_passed = True
            if gap_id == "team-parallel-execution":
                validation_metadata = await _team_parallel_validation_metadata(self.runtime, lane_result)
                lane_metadata["team_parallel_validation"] = dict(validation_metadata)
                validation_passed = bool(validation_metadata["validated"])
                if not validation_passed:
                    validation_failed_gap_ids.append(gap_id)
            elif _requires_delegation_validation(gap_id):
                validation_metadata = _delegation_validation_metadata(lane_result)
                delegation_validation[gap_id] = dict(validation_metadata)
                lane_metadata["delegation_validation"] = dict(validation_metadata)
                validation_passed = bool(validation_metadata["validated"])
                if not validation_passed:
                    validation_failed_gap_ids.append(gap_id)
            elif gap_id == "durable-supervisor-sessions":
                validation_metadata = _durable_supervisor_validation_metadata(lane_result)
                lane_metadata["durable_supervisor_validation"] = dict(validation_metadata)
                validation_passed = bool(validation_metadata["validated"])
                if not validation_passed:
                    validation_failed_gap_ids.append(gap_id)
            elif gap_id == "reconnector":
                validation_metadata = _reconnector_validation_metadata(lane_result)
                lane_metadata["reconnector_validation"] = dict(validation_metadata)
                validation_passed = bool(validation_metadata["validated"])
                if not validation_passed:
                    validation_failed_gap_ids.append(gap_id)
            elif gap_id == "protocol-bus":
                validation_metadata = _protocol_bus_validation_metadata(lane_result)
                lane_metadata["protocol_bus_validation"] = dict(validation_metadata)
                validation_passed = bool(validation_metadata["validated"])
                if not validation_passed:
                    validation_failed_gap_ids.append(gap_id)
            elif gap_id == "multi-leader-planning-review":
                validation_metadata = _planning_review_status_metadata(
                    run_result.objective_state.metadata,
                    required=True,
                )
                if validation_metadata is not None:
                    lane_metadata["planning_review"] = dict(validation_metadata)
                    validation_passed = bool(validation_metadata.get("validated"))
                else:
                    validation_passed = False
                if not validation_passed:
                    validation_failed_gap_ids.append(gap_id)
            elif gap_id == "superleader-isomorphic-runtime" and superleader_runtime_status is not None:
                lane_metadata["superleader_runtime_status"] = deepcopy(superleader_runtime_status)
            if lane_result.delivery_state.status == DeliveryStatus.WAITING_FOR_AUTHORITY:
                lane_metadata["authority_waiting"] = True
                waiting_tasks = lane_result.delivery_state.metadata.get("waiting_for_authority_task_ids")
                if isinstance(waiting_tasks, (list, tuple)) and waiting_tasks:
                    lane_metadata["authority_waiting_task_ids"] = list(waiting_tasks)
            authority_completion_required = gap_id == "authority-integration"
            authority_completion = await _authority_completion_metadata(
                self.runtime,
                lane_result,
                required=authority_completion_required,
            )
            if authority_completion is not None:
                lane_metadata["authority_completion"] = authority_completion
                authority_mainline = _authority_mainline_from_completion(authority_completion)
                lane_metadata["authority_mainline"] = authority_mainline
                authority_completion_request_count += int(authority_completion.get("request_count", 0))
                decision_counts = authority_completion.get("decision_counts")
                if isinstance(decision_counts, dict):
                    for key, value in decision_counts.items():
                        if not isinstance(key, str):
                            continue
                        try:
                            parsed = int(value)
                        except (TypeError, ValueError):
                            continue
                        authority_completion_decision_counts[key] = (
                            authority_completion_decision_counts.get(key, 0) + parsed
                        )
                closed_request_ids = authority_completion.get("closed_request_ids")
                if isinstance(closed_request_ids, list):
                    authority_completion_closed_request_ids.extend(
                        item for item in closed_request_ids if isinstance(item, str)
                    )
                waiting_request_ids = authority_completion.get("waiting_request_ids")
                if isinstance(waiting_request_ids, list):
                    authority_completion_waiting_request_ids.extend(
                        item for item in waiting_request_ids if isinstance(item, str)
                    )
                incomplete_request_ids = authority_completion.get("incomplete_request_ids")
                if isinstance(incomplete_request_ids, list):
                    authority_completion_incomplete_request_ids.extend(
                        item for item in incomplete_request_ids if isinstance(item, str)
                    )
                relay_pending_request_ids = authority_completion.get("relay_pending_request_ids")
                if isinstance(relay_pending_request_ids, list):
                    authority_completion_relay_pending_request_ids.extend(
                        item for item in relay_pending_request_ids if isinstance(item, str)
                    )
                completion_status = authority_completion.get("completion_status")
                if isinstance(completion_status, str) and completion_status:
                    authority_completion_lane_statuses[gap_id] = completion_status
                for item in authority_mainline.get("implemented_facts", ()):
                    if isinstance(item, str) and item not in authority_mainline_facts:
                        authority_mainline_facts.append(item)
                for item in authority_mainline.get("residual_gaps", ()):
                    if isinstance(item, str) and item not in authority_mainline_residuals:
                        authority_mainline_residuals.append(item)
                if authority_completion_required:
                    authority_completion_required_lane_count += 1
                    if bool(authority_completion.get("validated")):
                        authority_completion_required_validated_count += 1
                    else:
                        validation_passed = False
                        if gap_id not in validation_failed_gap_ids:
                            validation_failed_gap_ids.append(gap_id)
            lane_instructions_list.append(
                await self._build_lane_instruction(
                    gap_id,
                    lane_result,
                    metadata=lane_metadata,
                )
            )
            if (
                lane_result.delivery_state.status.value == "completed"
                and validation_passed
                and gap_id not in completed_gap_ids
            ):
                completed_gap_ids.append(gap_id)
        remaining_gap_ids = tuple(
            item.gap_id for item in inventory if item.gap_id not in set(completed_gap_ids)
        )
        next_round_gap_ids = remaining_gap_ids[: max(config.max_workstreams, 0)]
        next_round_prompt = (
            "Continue self-hosting by addressing the next ordered gaps: "
            + (", ".join(next_round_gap_ids) if next_round_gap_ids else "no remaining gaps.")
        )
        lane_instructions = tuple(lane_instructions_list)
        packet_metadata: dict[str, object] = {
            "knowledge_path": str(Path(config.knowledge_path) if config.knowledge_path is not None else _default_knowledge_path()),
            "leader_backend": config.leader_backend,
            "teammate_backend": config.teammate_backend,
            "leader_profile_id": superleader_config.leader_profile_id,
            "teammate_profile_id": superleader_config.teammate_profile_id,
        }
        if superleader_runtime_status is not None:
            packet_metadata["superleader_runtime_status"] = deepcopy(superleader_runtime_status)
        if superleader_config.role_profiles:
            packet_metadata["role_profiles"] = {
                profile_id: profile.to_dict()
                for profile_id, profile in superleader_config.role_profiles.items()
            }
        if superleader_config.leader_execution_policy is not None:
            packet_metadata["leader_execution_policy_legacy"] = _policy_dict(
                superleader_config.leader_execution_policy
            )
        if delegation_validation:
            packet_metadata["delegation_validation"] = delegation_validation
        if validation_failed_gap_ids:
            packet_metadata["validation_failed_gap_ids"] = list(validation_failed_gap_ids)
        if planning_review_status is not None:
            packet_metadata["planning_review_status"] = dict(planning_review_status)
        authority_completion_seen = (
            authority_completion_request_count > 0
            or authority_completion_required_lane_count > 0
        )
        if authority_completion_seen:
            closed_request_ids = list(_ordered_unique_strings(authority_completion_closed_request_ids))
            waiting_request_ids = list(_ordered_unique_strings(authority_completion_waiting_request_ids))
            incomplete_request_ids = list(_ordered_unique_strings(authority_completion_incomplete_request_ids))
            relay_pending_request_ids = list(
                _ordered_unique_strings(authority_completion_relay_pending_request_ids)
            )
            authority_completion_validated = (
                authority_completion_required_lane_count > 0
                and authority_completion_required_validated_count == authority_completion_required_lane_count
            )
            authority_completion_status = (
                "validated"
                if authority_completion_validated
                else (
                    "waiting"
                    if waiting_request_ids and not incomplete_request_ids
                    else "incomplete"
                )
            )
            packet_metadata["authority_completion_status"] = {
                "validated": authority_completion_validated,
                "completion_status": authority_completion_status,
                "request_count": authority_completion_request_count,
                "decision_counts": dict(authority_completion_decision_counts),
                "closed_request_ids": closed_request_ids,
                "waiting_request_ids": waiting_request_ids,
                "incomplete_request_ids": incomplete_request_ids,
                "relay_pending_request_ids": relay_pending_request_ids,
                "lane_statuses": dict(authority_completion_lane_statuses),
                "required_lane_count": authority_completion_required_lane_count,
                "validated_required_lane_count": authority_completion_required_validated_count,
            }
        if authority_mainline_facts or authority_mainline_residuals:
            packet_metadata["authority_mainline_status"] = {
                "implemented_facts": authority_mainline_facts,
                "residual_gaps": authority_mainline_residuals,
            }
        packet = SelfHostingInstructionPacket(
            objective_id=run_result.objective_state.objective_id,
            objective_status=run_result.objective_state.status.value,
            selected_gap_ids=selected_gap_ids,
            completed_gap_ids=tuple(completed_gap_ids),
            remaining_gap_ids=remaining_gap_ids,
            next_round_gap_ids=next_round_gap_ids,
            next_round_prompt=next_round_prompt,
            lane_instructions=lane_instructions,
            metadata=packet_metadata,
        )

        next_template = None
        if next_round_gap_ids:
            next_template = build_self_hosting_template(
                inventory=inventory,
                config=SelfHostingBootstrapConfig(
                    objective_id=f"{config.objective_id}-next",
                    group_id=config.group_id,
                    max_workstreams=config.max_workstreams,
                    completed_gap_ids=tuple(completed_gap_ids),
                    leader_backend=config.leader_backend,
                    teammate_backend=config.teammate_backend,
                    title=config.title,
                    description=config.description,
                    knowledge_path=config.knowledge_path,
                    working_dir=config.working_dir,
                    max_leader_turns=config.max_leader_turns,
                    auto_run_teammates=config.auto_run_teammates,
                    use_dynamic_planning=config.use_dynamic_planning,
                    leader_idle_timeout_seconds=config.leader_idle_timeout_seconds,
                    leader_hard_timeout_seconds=config.leader_hard_timeout_seconds,
                ),
            )

        return SelfHostingRoundReport(
            inventory=inventory,
            template=template,
            run_result=run_result,
            instruction_packet=packet,
            next_template=next_template,
        )
