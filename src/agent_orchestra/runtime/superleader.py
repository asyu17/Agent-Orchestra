from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any

from agent_orchestra.contracts.execution import Planner, WorkerExecutionPolicy
from agent_orchestra.contracts.authority import (
    AuthorityReactorCycleOutput,
    AuthorityState,
)
from agent_orchestra.contracts.delivery import DeliveryState, DeliveryStateKind, DeliveryStatus
from agent_orchestra.contracts.enums import AuthorityStatus, SpecEdgeKind, SpecNodeKind, WorkerStatus
from agent_orchestra.contracts.execution import ResidentCoordinatorPhase, ResidentCoordinatorSession
from agent_orchestra.contracts.hierarchical_review import ReviewItemKind
from agent_orchestra.contracts.objective import ObjectiveSpec
from agent_orchestra.contracts.session_continuity import ConversationHeadKind
from agent_orchestra.contracts.session_memory import (
    AgentTurnActorRole,
    AgentTurnKind,
    AgentTurnRecord,
    AgentTurnStatus,
    ArtifactRef,
    ArtifactRefKind,
    ArtifactStorageKind,
)
from agent_orchestra.contracts.worker_protocol import WorkerRoleProfile
from agent_orchestra.planning.template import ObjectiveTemplate, PlanningResult
from agent_orchestra.runtime.bootstrap_round import (
    HybridTeamRound,
    compile_leader_assignment,
    materialize_planning_result,
)
from agent_orchestra.runtime.authority_reactor import ObjectiveAuthorityRootReactor
from agent_orchestra.runtime.evaluator import DefaultDeliveryEvaluator, DeliveryEvaluator
from agent_orchestra.runtime.group_runtime import GroupRuntime, ResidentLaneLiveView
from agent_orchestra.runtime.leader_output_protocol import parse_leader_turn_output
from agent_orchestra.runtime.leader_loop import (
    LeaderLoopConfig,
    LeaderLoopResult,
    LeaderLoopSupervisor,
    _resolve_role_profile,
    _resolve_role_profiles,
)
from agent_orchestra.runtime.planning_review_protocol import parse_leader_peer_review_output
from agent_orchestra.runtime.protocol_bridge import InMemoryMailboxBridge
from agent_orchestra.runtime.resident_kernel import ResidentCoordinatorCycleResult, ResidentCoordinatorKernel
from agent_orchestra.runtime.session_memory import SessionMemoryService
from agent_orchestra.tools.mailbox import (
    MailboxBridge,
    MailboxDeliveryMode,
    MailboxDigest,
    MailboxSubscription,
    MailboxVisibilityScope,
)
from agent_orchestra.tools.permission_protocol import PermissionBroker


def _objective_delivery_id(objective_id: str) -> str:
    return f"{objective_id}:objective"


def _authority_root_team_id(group_id: str) -> str:
    return f"{group_id}:authority-root"


def _superleader_subscriber(objective_id: str) -> str:
    return f"superleader:{objective_id}"


def _objective_shared_subscription_id(objective_id: str) -> str:
    return f"{objective_id}:objective-shared-summary"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _summary_text(value: object, *, limit: int = 400) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _artifact_hash(payload: object) -> str:
    raw = json.dumps(payload, ensure_ascii=True, sort_keys=True).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


@dataclass(slots=True)
class SuperLeaderConfig:
    leader_backend: str | None = None
    teammate_backend: str | None = None
    leader_execution_policy: WorkerExecutionPolicy | None = None
    teammate_execution_policy: WorkerExecutionPolicy | None = None
    leader_profile_id: str = "leader_in_process_fast"
    teammate_profile_id: str = "teammate_in_process_fast"
    role_profiles: dict[str, WorkerRoleProfile] | None = None
    max_leader_turns: int | None = None
    max_mailbox_followup_turns: int | None = 8
    max_active_lanes: int | None = None
    auto_run_teammates: bool = True
    allow_promptless_convergence: bool = True
    keep_leader_session_idle: bool = True
    keep_teammate_session_idle: bool = False
    enable_planning_review: bool = True
    working_dir: str | None = None
    created_by: str = "superleader.runtime"


@dataclass(slots=True)
class SuperLeaderLaneSessionState:
    coordinator_id: str
    host_owner_coordinator_id: str
    role: str = "leader"
    objective_id: str | None = None
    lane_id: str | None = None
    team_id: str | None = None
    runtime_task_id: str | None = None
    phase: ResidentCoordinatorPhase = ResidentCoordinatorPhase.BOOTING
    cycle_count: int = 0
    prompt_turn_count: int = 0
    claimed_task_count: int = 0
    subordinate_dispatch_count: int = 0
    mailbox_poll_count: int = 0
    active_subordinate_ids: tuple[str, ...] = ()
    mailbox_cursor: str | None = None
    last_reason: str = ""
    metadata: dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "coordinator_id": self.coordinator_id,
            "host_owner_coordinator_id": self.host_owner_coordinator_id,
            "role": self.role,
            "objective_id": self.objective_id,
            "lane_id": self.lane_id,
            "team_id": self.team_id,
            "runtime_task_id": self.runtime_task_id,
            "phase": self.phase.value,
            "cycle_count": self.cycle_count,
            "prompt_turn_count": self.prompt_turn_count,
            "claimed_task_count": self.claimed_task_count,
            "subordinate_dispatch_count": self.subordinate_dispatch_count,
            "mailbox_poll_count": self.mailbox_poll_count,
            "active_subordinate_ids": list(self.active_subordinate_ids),
            "mailbox_cursor": self.mailbox_cursor,
            "last_reason": self.last_reason,
            "metadata": dict(self.metadata),
        }


@dataclass(slots=True)
class SuperLeaderLaneCoordinationState:
    lane_id: str
    team_id: str
    dependency_lane_ids: tuple[str, ...] = ()
    waiting_on_lane_ids: tuple[str, ...] = ()
    status: DeliveryStatus = DeliveryStatus.PENDING
    started_in_batch: int | None = None
    completed_in_batch: int | None = None
    iteration: int = 0
    summary: str = ""
    latest_worker_ids: tuple[str, ...] = ()
    session: SuperLeaderLaneSessionState | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "lane_id": self.lane_id,
            "team_id": self.team_id,
            "dependency_lane_ids": list(self.dependency_lane_ids),
            "waiting_on_lane_ids": list(self.waiting_on_lane_ids),
            "status": self.status.value,
            "started_in_batch": self.started_in_batch,
            "completed_in_batch": self.completed_in_batch,
            "iteration": self.iteration,
            "summary": self.summary,
            "latest_worker_ids": list(self.latest_worker_ids),
            "session": self.session.to_dict() if self.session is not None else None,
        }


@dataclass(slots=True)
class SuperLeaderCoordinationState:
    coordinator_id: str
    objective_id: str
    max_active_lanes: int
    batch_count: int
    lane_states: tuple[SuperLeaderLaneCoordinationState, ...]
    pending_lane_ids: tuple[str, ...]
    ready_lane_ids: tuple[str, ...]
    active_lane_ids: tuple[str, ...]
    completed_lane_ids: tuple[str, ...]
    blocked_lane_ids: tuple[str, ...]
    failed_lane_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "coordinator_id": self.coordinator_id,
            "objective_id": self.objective_id,
            "max_active_lanes": self.max_active_lanes,
            "batch_count": self.batch_count,
            "lane_states": [state.to_dict() for state in self.lane_states],
            "pending_lane_ids": list(self.pending_lane_ids),
            "ready_lane_ids": list(self.ready_lane_ids),
            "active_lane_ids": list(self.active_lane_ids),
            "completed_lane_ids": list(self.completed_lane_ids),
            "blocked_lane_ids": list(self.blocked_lane_ids),
            "failed_lane_ids": list(self.failed_lane_ids),
        }


@dataclass(slots=True)
class SuperLeaderRunResult:
    round_bundle: HybridTeamRound
    lane_results: tuple[LeaderLoopResult, ...]
    coordination_state: SuperLeaderCoordinationState
    objective_state: DeliveryState
    message_subscriptions: tuple[MailboxSubscription, ...] = ()
    message_digests: tuple[MailboxDigest, ...] = ()
    coordinator_session: ResidentCoordinatorSession | None = None


@dataclass(slots=True)
class SuperLeaderResidentLiveView:
    objective_id: str
    lane_views: tuple[ResidentLaneLiveView, ...]
    objective_shared_digests: tuple[MailboxDigest, ...] = ()
    objective_message_runtime: dict[str, object] = field(default_factory=dict)
    objective_coordination: dict[str, object] = field(default_factory=dict)


def _lane_scheduler_budget(
    *,
    objective_budget: dict[str, object],
    lane_count: int,
    config: SuperLeaderConfig,
) -> int:
    if lane_count <= 0:
        return 0
    if config.max_active_lanes is not None:
        try:
            parsed = int(config.max_active_lanes)
        except (TypeError, ValueError):
            parsed = lane_count
    else:
        raw_budget = objective_budget.get("max_teams", lane_count)
        try:
            parsed = int(raw_budget)
        except (TypeError, ValueError):
            parsed = lane_count
    if parsed <= 0:
        parsed = lane_count
    return max(1, min(parsed, lane_count))


def _lane_dependencies(
    *,
    result: PlanningResult,
    round_bundle: HybridTeamRound,
) -> dict[str, tuple[str, ...]]:
    lane_ids = {leader_round.lane_id for leader_round in round_bundle.leader_rounds}
    dependencies: dict[str, list[str]] = {lane_id: [] for lane_id in lane_ids}
    node_to_lane = {
        node.node_id: node.lane_id
        for node in result.spec_nodes
        if node.kind == SpecNodeKind.LEADER_TASK and node.lane_id
    }
    for edge in result.spec_edges:
        if edge.kind != SpecEdgeKind.DEPENDS_ON:
            continue
        dependent_lane_id = node_to_lane.get(edge.from_node_id)
        prerequisite_lane_id = node_to_lane.get(edge.to_node_id)
        if dependent_lane_id not in lane_ids or prerequisite_lane_id not in lane_ids:
            continue
        dependencies[dependent_lane_id].append(prerequisite_lane_id)
    return {
        lane_id: tuple(dict.fromkeys(dependencies[lane_id]))
        for lane_id in dependencies
    }


def _pending_dependency_ids(
    *,
    lane_id: str,
    dependencies: dict[str, tuple[str, ...]],
    lane_states_by_id: dict[str, SuperLeaderLaneCoordinationState],
) -> tuple[str, ...]:
    return tuple(
        dependency_lane_id
        for dependency_lane_id in dependencies.get(lane_id, ())
        if lane_states_by_id.get(dependency_lane_id) is None
        or lane_states_by_id[dependency_lane_id].status != DeliveryStatus.COMPLETED
    )


def _lane_status_is_active(status: DeliveryStatus) -> bool:
    return status in {DeliveryStatus.RUNNING, DeliveryStatus.WAITING}


def _lane_status_from_resident_phase(
    phase: ResidentCoordinatorPhase,
) -> DeliveryStatus:
    if phase == ResidentCoordinatorPhase.QUIESCENT:
        return DeliveryStatus.COMPLETED
    if phase == ResidentCoordinatorPhase.FAILED:
        return DeliveryStatus.FAILED
    if phase in {
        ResidentCoordinatorPhase.WAITING_FOR_MAILBOX,
        ResidentCoordinatorPhase.WAITING_FOR_DEPENDENCIES,
        ResidentCoordinatorPhase.WAITING_FOR_SUBORDINATES,
        ResidentCoordinatorPhase.SHUTDOWN_REQUESTED,
    }:
        return DeliveryStatus.WAITING
    if phase in {
        ResidentCoordinatorPhase.BOOTING,
        ResidentCoordinatorPhase.RUNNING,
        ResidentCoordinatorPhase.IDLE,
    }:
        return DeliveryStatus.RUNNING
    return DeliveryStatus.PENDING


def _effective_lane_status_from_live_view(
    *,
    delivery_state: DeliveryState | None,
    coordinator_session: SuperLeaderLaneSessionState | None,
) -> DeliveryStatus:
    status, _source = _lane_effective_status_and_source(
        delivery_state=delivery_state,
        coordinator_session=coordinator_session,
    )
    return status


def _lane_effective_status_and_source(
    *,
    delivery_state: DeliveryState | None,
    coordinator_session: SuperLeaderLaneSessionState | None,
) -> tuple[DeliveryStatus, str]:
    if delivery_state is None:
        if coordinator_session is None:
            return DeliveryStatus.PENDING, "scheduler_fallback"
        return _lane_status_from_resident_phase(coordinator_session.phase), "coordinator_session"
    status = delivery_state.status
    if coordinator_session is None:
        return status, "delivery_state"
    projected_status = _lane_status_from_resident_phase(coordinator_session.phase)
    if status == DeliveryStatus.PENDING and projected_status != DeliveryStatus.PENDING:
        return projected_status, "coordinator_session"
    if (
        status in {DeliveryStatus.PENDING, DeliveryStatus.RUNNING, DeliveryStatus.WAITING}
        and projected_status in {DeliveryStatus.COMPLETED, DeliveryStatus.FAILED}
    ):
        return projected_status, "coordinator_session"
    return status, "delivery_state"


def _authority_waiting_blockers(
    *,
    pending_lane_ids: set[str],
    lane_states_by_id: dict[str, SuperLeaderLaneCoordinationState],
) -> tuple[str, ...]:
    blockers: set[str] = set()
    for lane_id in pending_lane_ids:
        lane_state = lane_states_by_id.get(lane_id)
        if lane_state is None:
            continue
        blockers.update(lane_state.waiting_on_lane_ids)
    if not blockers:
        return ()
    waiting_blockers = []
    for blocker in sorted(blockers):
        blocker_state = lane_states_by_id.get(blocker)
        if blocker_state is None or blocker_state.status != DeliveryStatus.WAITING_FOR_AUTHORITY:
            return ()
        waiting_blockers.append(blocker)
    return tuple(waiting_blockers)


def _terminal_dependency_blockers(
    *,
    pending_lane_ids: set[str],
    lane_states_by_id: dict[str, SuperLeaderLaneCoordinationState],
) -> tuple[str, ...]:
    blockers: set[str] = set()
    for lane_id in pending_lane_ids:
        lane_state = lane_states_by_id.get(lane_id)
        if lane_state is None:
            continue
        blockers.update(lane_state.waiting_on_lane_ids)
    if not blockers:
        return ()
    terminal_blockers = []
    for blocker in sorted(blockers):
        blocker_state = lane_states_by_id.get(blocker)
        if blocker_state is None or blocker_state.status not in {
            DeliveryStatus.BLOCKED,
            DeliveryStatus.FAILED,
        }:
            return ()
        terminal_blockers.append(blocker)
    return tuple(terminal_blockers)


def _lane_has_task_surface_authority_pressure(
    task_surface_authority: dict[str, object],
) -> bool:
    if not task_surface_authority:
        return False
    if bool(task_surface_authority.get("authority_waiting")):
        return True
    for key in (
        "task_surface_waiting_task_ids",
        "mutation_waiting_task_ids",
        "protected_field_waiting_task_ids",
        "waiting_request_ids",
    ):
        values = task_surface_authority.get(key)
        if isinstance(values, (list, tuple)) and values:
            return True
    return False


def _lane_host_step_live_inputs(
    *,
    lane_live_view: ResidentLaneLiveView,
    objective_shared_digests: tuple[MailboxDigest, ...],
) -> dict[str, object]:
    live_inputs: dict[str, object] = {
        "pending_shared_digest_count": lane_live_view.pending_shared_digest_count,
        "mailbox_followup_turns_used": int(
            lane_live_view.coordination_metadata.get("mailbox_followup_turns_used", 0) or 0
        ),
        "objective_shared_digest_count": len(objective_shared_digests),
    }
    if lane_live_view.shared_digest_envelope_ids:
        live_inputs["shared_digest_envelope_ids"] = list(
            lane_live_view.shared_digest_envelope_ids
        )
    if "mailbox_followup_turn_limit" in lane_live_view.coordination_metadata:
        live_inputs["mailbox_followup_turn_limit"] = lane_live_view.coordination_metadata.get(
            "mailbox_followup_turn_limit"
        )
    if lane_live_view.coordinator_session is not None:
        live_inputs["host_phase"] = lane_live_view.coordinator_session.phase.value
    if lane_live_view.resident_team_shell is not None:
        live_inputs["resident_team_shell"] = {
            "resident_team_shell_id": lane_live_view.resident_team_shell.resident_team_shell_id,
            "status": lane_live_view.resident_team_shell.status.value,
            "leader_slot_session_id": lane_live_view.resident_team_shell.leader_slot_session_id,
            "teammate_slot_session_ids": list(
                lane_live_view.resident_team_shell.teammate_slot_session_ids
            ),
        }
    if lane_live_view.shell_attach_decision is not None:
        live_inputs["shell_attach"] = lane_live_view.shell_attach_decision.to_dict()
    if lane_live_view.task_surface_authority:
        live_inputs["task_surface_authority"] = dict(
            lane_live_view.task_surface_authority
        )
    if objective_shared_digests:
        live_inputs["objective_shared_digest_envelope_ids"] = [
            digest.envelope_id for digest in objective_shared_digests
        ]
    return live_inputs


def _lane_should_schedule_host_step(
    *,
    lane_state: SuperLeaderLaneCoordinationState,
    lane_live_view: ResidentLaneLiveView,
    objective_shared_digests: tuple[MailboxDigest, ...],
    in_flight_lane_ids: set[str],
) -> bool:
    if lane_state.lane_id in in_flight_lane_ids:
        return False
    if lane_state.session is None:
        return False
    if lane_state.status not in {DeliveryStatus.RUNNING, DeliveryStatus.WAITING}:
        return False
    phase = lane_state.session.phase
    if phase in {
        ResidentCoordinatorPhase.BOOTING,
        ResidentCoordinatorPhase.RUNNING,
        ResidentCoordinatorPhase.IDLE,
        ResidentCoordinatorPhase.WAITING_FOR_SUBORDINATES,
    }:
        return True
    if phase == ResidentCoordinatorPhase.WAITING_FOR_MAILBOX:
        return True
    if phase != ResidentCoordinatorPhase.WAITING_FOR_DEPENDENCIES:
        return False
    return _lane_has_task_surface_authority_pressure(
        lane_live_view.task_surface_authority
    ) or bool(objective_shared_digests)


def _build_coordination_state(
    *,
    coordinator_id: str,
    objective_id: str,
    lane_order: tuple[str, ...],
    max_active_lanes: int,
    batch_count: int,
    lane_states_by_id: dict[str, SuperLeaderLaneCoordinationState],
) -> SuperLeaderCoordinationState:
    lane_states = tuple(lane_states_by_id[lane_id] for lane_id in lane_order)
    pending_lane_ids = tuple(
        lane_state.lane_id for lane_state in lane_states if lane_state.status == DeliveryStatus.PENDING
    )
    ready_lane_ids = tuple(
        lane_state.lane_id
        for lane_state in lane_states
        if lane_state.status == DeliveryStatus.PENDING and not lane_state.waiting_on_lane_ids
    )
    active_lane_ids = tuple(
        lane_state.lane_id for lane_state in lane_states if lane_state.status in {DeliveryStatus.RUNNING, DeliveryStatus.WAITING}
    )
    completed_lane_ids = tuple(
        lane_state.lane_id for lane_state in lane_states if lane_state.status == DeliveryStatus.COMPLETED
    )
    blocked_lane_ids = tuple(
        lane_state.lane_id for lane_state in lane_states if lane_state.status == DeliveryStatus.BLOCKED
    )
    failed_lane_ids = tuple(
        lane_state.lane_id for lane_state in lane_states if lane_state.status == DeliveryStatus.FAILED
    )
    return SuperLeaderCoordinationState(
        coordinator_id=coordinator_id,
        objective_id=objective_id,
        max_active_lanes=max_active_lanes,
        batch_count=batch_count,
        lane_states=lane_states,
        pending_lane_ids=pending_lane_ids,
        ready_lane_ids=ready_lane_ids,
        active_lane_ids=active_lane_ids,
        completed_lane_ids=completed_lane_ids,
        blocked_lane_ids=blocked_lane_ids,
        failed_lane_ids=failed_lane_ids,
    )


def _merge_mailbox_subscriptions(
    first: tuple[MailboxSubscription, ...],
    second: tuple[MailboxSubscription, ...],
) -> tuple[MailboxSubscription, ...]:
    merged: list[MailboxSubscription] = []
    seen: set[tuple[str, str]] = set()
    for subscription in first + second:
        key = (
            subscription.subscriber,
            subscription.subscription_id or "",
        )
        if key in seen:
            continue
        seen.add(key)
        merged.append(subscription)
    return tuple(merged)


def _merge_mailbox_digests(
    first: tuple[MailboxDigest, ...],
    second: tuple[MailboxDigest, ...],
) -> tuple[MailboxDigest, ...]:
    merged: list[MailboxDigest] = []
    seen: set[tuple[str, str, str]] = set()
    for digest in first + second:
        key = (
            digest.subscription_id,
            digest.subscriber,
            digest.envelope_id,
        )
        if key in seen:
            continue
        seen.add(key)
        merged.append(digest)
    return tuple(merged)


def _merge_leader_loop_results(
    *,
    existing: LeaderLoopResult | None,
    latest: LeaderLoopResult,
) -> LeaderLoopResult:
    if existing is None:
        return latest
    return LeaderLoopResult(
        leader_round=existing.leader_round,
        delivery_state=latest.delivery_state,
        leader_records=existing.leader_records + latest.leader_records,
        teammate_records=existing.teammate_records + latest.teammate_records,
        mailbox_cursor=latest.mailbox_cursor or existing.mailbox_cursor,
        mailbox_envelopes=existing.mailbox_envelopes + latest.mailbox_envelopes,
        message_subscriptions=_merge_mailbox_subscriptions(
            existing.message_subscriptions,
            latest.message_subscriptions,
        ),
        message_digests=_merge_mailbox_digests(
            existing.message_digests,
            latest.message_digests,
        ),
        created_task_ids=tuple(
            dict.fromkeys(existing.created_task_ids + latest.created_task_ids)
        ),
        turns=existing.turns + latest.turns,
        coordinator_session=latest.coordinator_session or existing.coordinator_session,
    )


def _lane_delivery_state_from_coordination_state(
    *,
    objective_id: str,
    lane_state: SuperLeaderLaneCoordinationState,
) -> DeliveryState:
    metadata: dict[str, object] = {}
    if lane_state.dependency_lane_ids:
        metadata["dependency_lane_ids"] = list(lane_state.dependency_lane_ids)
    if lane_state.waiting_on_lane_ids:
        metadata["waiting_on_lane_ids"] = list(lane_state.waiting_on_lane_ids)
    if lane_state.session is not None:
        metadata["lane_session"] = lane_state.session.to_dict()
    return DeliveryState(
        delivery_id=f"{objective_id}:lane:{lane_state.lane_id}",
        objective_id=objective_id,
        kind=DeliveryStateKind.LANE,
        status=lane_state.status,
        lane_id=lane_state.lane_id,
        team_id=lane_state.team_id,
        iteration=lane_state.iteration,
        summary=lane_state.summary,
        latest_worker_ids=lane_state.latest_worker_ids,
        metadata=metadata,
    )


def _lane_status_phase(status: DeliveryStatus) -> ResidentCoordinatorPhase:
    if status == DeliveryStatus.COMPLETED:
        return ResidentCoordinatorPhase.QUIESCENT
    if status == DeliveryStatus.BLOCKED:
        return ResidentCoordinatorPhase.WAITING_FOR_DEPENDENCIES
    if status == DeliveryStatus.FAILED:
        return ResidentCoordinatorPhase.FAILED
    if status in {DeliveryStatus.RUNNING, DeliveryStatus.WAITING}:
        return ResidentCoordinatorPhase.WAITING_FOR_SUBORDINATES
    return ResidentCoordinatorPhase.IDLE


def _project_lane_session_state(
    *,
    base_state: SuperLeaderLaneSessionState,
    lane_status: DeliveryStatus,
    coordinator_session: ResidentCoordinatorSession | None,
) -> SuperLeaderLaneSessionState:
    if coordinator_session is None:
        return replace(
            base_state,
            phase=_lane_status_phase(lane_status),
            active_subordinate_ids=(),
            last_reason="Leader lane completed without session projection.",
        )
    return replace(
        base_state,
        phase=coordinator_session.phase,
        cycle_count=coordinator_session.cycle_count,
        prompt_turn_count=coordinator_session.prompt_turn_count,
        claimed_task_count=coordinator_session.claimed_task_count,
        subordinate_dispatch_count=coordinator_session.subordinate_dispatch_count,
        mailbox_poll_count=coordinator_session.mailbox_poll_count,
        active_subordinate_ids=coordinator_session.active_subordinate_ids,
        mailbox_cursor=coordinator_session.mailbox_cursor,
        last_reason=coordinator_session.last_reason,
        metadata=dict(coordinator_session.metadata),
    )


def _lane_session_state_from_coordinator_state(
    state: Any,
) -> SuperLeaderLaneSessionState | None:
    if state is None:
        return None
    coordinator_id = getattr(state, "coordinator_id", None)
    if not isinstance(coordinator_id, str) or not coordinator_id:
        return None
    phase = getattr(state, "phase", ResidentCoordinatorPhase.BOOTING)
    if not isinstance(phase, ResidentCoordinatorPhase):
        try:
            phase = ResidentCoordinatorPhase(str(phase))
        except ValueError:
            phase = ResidentCoordinatorPhase.BOOTING
    active_subordinate_ids = getattr(state, "active_subordinate_ids", ())
    if not isinstance(active_subordinate_ids, (list, tuple)):
        active_subordinate_ids = ()
    metadata = getattr(state, "metadata", {})
    if not isinstance(metadata, dict):
        metadata = {}
    return SuperLeaderLaneSessionState(
        coordinator_id=coordinator_id,
        host_owner_coordinator_id=(
            str(getattr(state, "host_owner_coordinator_id", "") or coordinator_id)
        ),
        role=str(getattr(state, "role", "leader") or "leader"),
        objective_id=getattr(state, "objective_id", None),
        lane_id=getattr(state, "lane_id", None),
        team_id=getattr(state, "team_id", None),
        runtime_task_id=getattr(state, "runtime_task_id", None),
        phase=phase,
        cycle_count=int(getattr(state, "cycle_count", 0) or 0),
        prompt_turn_count=int(getattr(state, "prompt_turn_count", 0) or 0),
        claimed_task_count=int(getattr(state, "claimed_task_count", 0) or 0),
        subordinate_dispatch_count=int(getattr(state, "subordinate_dispatch_count", 0) or 0),
        mailbox_poll_count=int(getattr(state, "mailbox_poll_count", 0) or 0),
        active_subordinate_ids=tuple(str(item) for item in active_subordinate_ids if str(item)),
        mailbox_cursor=getattr(state, "mailbox_cursor", None),
        last_reason=str(getattr(state, "last_reason", "") or ""),
        metadata=dict(metadata),
    )


def _lane_session_state_from_payload(payload: object) -> SuperLeaderLaneSessionState | None:
    if not isinstance(payload, dict):
        return None
    coordinator_id = payload.get("coordinator_id")
    if not isinstance(coordinator_id, str) or not coordinator_id:
        return None
    phase_raw = payload.get("phase", ResidentCoordinatorPhase.BOOTING.value)
    try:
        phase = (
            phase_raw
            if isinstance(phase_raw, ResidentCoordinatorPhase)
            else ResidentCoordinatorPhase(str(phase_raw))
        )
    except ValueError:
        phase = ResidentCoordinatorPhase.BOOTING
    active_subordinate_ids = payload.get("active_subordinate_ids", ())
    if not isinstance(active_subordinate_ids, (list, tuple)):
        active_subordinate_ids = ()
    metadata = payload.get("metadata", {})
    if not isinstance(metadata, dict):
        metadata = {}
    return SuperLeaderLaneSessionState(
        coordinator_id=coordinator_id,
        host_owner_coordinator_id=str(
            payload.get("host_owner_coordinator_id") or coordinator_id
        ),
        role=str(payload.get("role", "leader") or "leader"),
        objective_id=str(payload["objective_id"]) if payload.get("objective_id") is not None else None,
        lane_id=str(payload["lane_id"]) if payload.get("lane_id") is not None else None,
        team_id=str(payload["team_id"]) if payload.get("team_id") is not None else None,
        runtime_task_id=(
            str(payload["runtime_task_id"]) if payload.get("runtime_task_id") is not None else None
        ),
        phase=phase,
        cycle_count=int(payload.get("cycle_count", 0) or 0),
        prompt_turn_count=int(payload.get("prompt_turn_count", 0) or 0),
        claimed_task_count=int(payload.get("claimed_task_count", 0) or 0),
        subordinate_dispatch_count=int(payload.get("subordinate_dispatch_count", 0) or 0),
        mailbox_poll_count=int(payload.get("mailbox_poll_count", 0) or 0),
        active_subordinate_ids=tuple(str(item) for item in active_subordinate_ids if str(item)),
        mailbox_cursor=(
            str(payload["mailbox_cursor"]) if payload.get("mailbox_cursor") is not None else None
        ),
        last_reason=str(payload.get("last_reason", "") or ""),
        metadata=dict(metadata),
    )


def _synchronize_lane_state_from_live_view(
    *,
    lane_state: SuperLeaderLaneCoordinationState,
    live_view: ResidentLaneLiveView,
) -> None:
    delivery_state = live_view.delivery_state
    if delivery_state is not None:
        lane_state.status = delivery_state.status
        lane_state.iteration = delivery_state.iteration
        lane_state.summary = delivery_state.summary
        lane_state.latest_worker_ids = delivery_state.latest_worker_ids
        metadata = delivery_state.metadata if isinstance(delivery_state.metadata, dict) else {}
        waiting_on_lane_ids = metadata.get("waiting_on_lane_ids")
        if isinstance(waiting_on_lane_ids, (list, tuple)):
            lane_state.waiting_on_lane_ids = tuple(
                str(item) for item in waiting_on_lane_ids if str(item)
            )
        dependency_lane_ids = metadata.get("dependency_lane_ids")
        if isinstance(dependency_lane_ids, (list, tuple)):
            lane_state.dependency_lane_ids = tuple(
                str(item) for item in dependency_lane_ids if str(item)
            )
    coordinator_session = _lane_session_state_from_coordinator_state(
        live_view.coordinator_session
    )
    if coordinator_session is None and delivery_state is not None:
        metadata = delivery_state.metadata if isinstance(delivery_state.metadata, dict) else {}
        coordinator_session = _lane_session_state_from_payload(metadata.get("lane_session"))
    if delivery_state is None and coordinator_session is None:
        # Missing live projections are not authoritative evidence that the lane
        # reverted to pending; keep the in-memory coordination state until a
        # fresher delivery/session snapshot arrives.
        return
    if coordinator_session is not None:
        lane_state.session = coordinator_session
    lane_state.status = _effective_lane_status_from_live_view(
        delivery_state=delivery_state,
        coordinator_session=coordinator_session,
    )


def _resident_live_view_metadata(
    *,
    coordination_state: SuperLeaderCoordinationState,
    live_view: SuperLeaderResidentLiveView,
) -> dict[str, object]:
    coordination_lane_by_id = {
        lane_state.lane_id: lane_state for lane_state in coordination_state.lane_states
    }
    lane_live_inputs: dict[str, object] = {}
    lane_digest_counts: dict[str, int] = {}
    lane_mailbox_followup_turns: dict[str, int] = {}
    lane_host_phases: dict[str, str] = {}
    lane_shell_statuses: dict[str, str] = {}
    lane_attach_modes: dict[str, str] = {}
    lane_attach_targets: dict[str, str] = {}
    lane_truth_sources: dict[str, str] = {}
    primary_lane_statuses: dict[str, str] = {}
    runtime_native_lane_ids: list[str] = []
    fallback_lane_ids: list[str] = []
    primary_active_lane_ids: list[str] = []
    primary_pending_lane_ids: list[str] = []
    primary_completed_lane_ids: list[str] = []
    primary_failed_lane_ids: list[str] = []
    primary_blocked_lane_ids: list[str] = []
    primary_waiting_for_authority_lane_ids: list[str] = []
    primary_active_lane_session_ids: list[str] = []
    task_surface_authority_lane_ids: list[str] = []
    lane_task_surface_authority_waiting_task_ids: dict[str, list[str]] = {}
    lane_task_surface_authority_waiting_request_ids: dict[str, list[str]] = {}
    host_stepped_lane_ids: list[str] = []
    lane_host_step_counts: dict[str, int] = {}
    for lane_live_view in live_view.lane_views:
        lane_state = coordination_lane_by_id.get(lane_live_view.lane_id)
        effective_status, truth_source = _lane_effective_status_and_source(
            delivery_state=lane_live_view.delivery_state,
            coordinator_session=_lane_session_state_from_coordinator_state(
                lane_live_view.coordinator_session
            ),
        )
        if truth_source == "scheduler_fallback" and lane_state is not None:
            effective_status = lane_state.status
        input_metadata: dict[str, object] = {}
        if lane_live_view.delivery_state is not None:
            input_metadata["delivery_status"] = lane_live_view.delivery_state.status.value
        input_metadata["effective_status"] = effective_status.value
        input_metadata["truth_source"] = truth_source
        input_metadata["pending_shared_digest_count"] = (
            lane_live_view.pending_shared_digest_count
        )
        if lane_live_view.shared_digest_envelope_ids:
            input_metadata["shared_digest_envelope_ids"] = list(
                lane_live_view.shared_digest_envelope_ids
            )
        mailbox_followup_turns_used = int(
            lane_live_view.coordination_metadata.get("mailbox_followup_turns_used", 0) or 0
        )
        input_metadata["mailbox_followup_turns_used"] = mailbox_followup_turns_used
        if "mailbox_followup_turn_limit" in lane_live_view.coordination_metadata:
            input_metadata["mailbox_followup_turn_limit"] = lane_live_view.coordination_metadata.get(
                "mailbox_followup_turn_limit"
            )
        if lane_live_view.task_surface_authority:
            task_surface_authority = dict(lane_live_view.task_surface_authority)
            input_metadata["task_surface_authority"] = task_surface_authority
            task_surface_authority_lane_ids.append(lane_live_view.lane_id)
            waiting_task_ids = task_surface_authority.get("task_surface_waiting_task_ids")
            if isinstance(waiting_task_ids, list) and waiting_task_ids:
                lane_task_surface_authority_waiting_task_ids[lane_live_view.lane_id] = list(
                    waiting_task_ids
                )
            waiting_request_ids = task_surface_authority.get("waiting_request_ids")
            if isinstance(waiting_request_ids, list) and waiting_request_ids:
                lane_task_surface_authority_waiting_request_ids[lane_live_view.lane_id] = list(
                    waiting_request_ids
                )
        if lane_live_view.coordinator_session is not None:
            input_metadata["host_phase"] = lane_live_view.coordinator_session.phase.value
            input_metadata["host_owner_coordinator_id"] = (
                lane_live_view.coordinator_session.host_owner_coordinator_id
            )
            input_metadata["host_mailbox_poll_count"] = (
                lane_live_view.coordinator_session.mailbox_poll_count
            )
            if lane_live_view.coordinator_session.mailbox_cursor is not None:
                input_metadata["host_mailbox_cursor"] = (
                    lane_live_view.coordinator_session.mailbox_cursor
                )
            if lane_live_view.coordinator_session.last_reason:
                input_metadata["host_last_reason"] = lane_live_view.coordinator_session.last_reason
            host_metadata = (
                lane_live_view.coordinator_session.metadata
                if isinstance(lane_live_view.coordinator_session.metadata, dict)
                else {}
            )
            host_step_count = host_metadata.get("host_step_count")
            try:
                parsed_host_step_count = int(host_step_count or 0)
            except (TypeError, ValueError):
                parsed_host_step_count = 0
            if parsed_host_step_count > 0:
                input_metadata["host_step_count"] = parsed_host_step_count
                lane_host_step_counts[lane_live_view.lane_id] = parsed_host_step_count
                host_stepped_lane_ids.append(lane_live_view.lane_id)
            runtime_step_mode = host_metadata.get("runtime_step_mode")
            if isinstance(runtime_step_mode, str) and runtime_step_mode:
                input_metadata["runtime_step_mode"] = runtime_step_mode
            host_step_reason = host_metadata.get("host_step_reason")
            if isinstance(host_step_reason, str) and host_step_reason:
                input_metadata["host_step_reason"] = host_step_reason
            lane_host_phases[lane_live_view.lane_id] = (
                lane_live_view.coordinator_session.phase.value
            )
        if lane_live_view.resident_team_shell is not None:
            shell_status = lane_live_view.resident_team_shell.status.value
            lane_shell_statuses[lane_live_view.lane_id] = shell_status
            input_metadata["resident_team_shell"] = {
                "resident_team_shell_id": lane_live_view.resident_team_shell.resident_team_shell_id,
                "status": shell_status,
                "leader_slot_session_id": lane_live_view.resident_team_shell.leader_slot_session_id,
                "teammate_slot_session_ids": list(
                    lane_live_view.resident_team_shell.teammate_slot_session_ids
                ),
            }
        if lane_live_view.shell_attach_decision is not None:
            attach_mode = lane_live_view.shell_attach_decision.mode.value
            lane_attach_modes[lane_live_view.lane_id] = attach_mode
            preferred_session_id = lane_live_view.shell_attach_decision.metadata.get(
                "preferred_session_id"
            )
            if isinstance(preferred_session_id, str) and preferred_session_id:
                lane_attach_targets[lane_live_view.lane_id] = preferred_session_id
            input_metadata["shell_attach"] = lane_live_view.shell_attach_decision.to_dict()
        lane_truth_sources[lane_live_view.lane_id] = truth_source
        primary_lane_statuses[lane_live_view.lane_id] = effective_status.value
        if truth_source == "scheduler_fallback":
            fallback_lane_ids.append(lane_live_view.lane_id)
        else:
            runtime_native_lane_ids.append(lane_live_view.lane_id)
        if effective_status == DeliveryStatus.PENDING:
            primary_pending_lane_ids.append(lane_live_view.lane_id)
        elif effective_status == DeliveryStatus.WAITING_FOR_AUTHORITY:
            primary_waiting_for_authority_lane_ids.append(lane_live_view.lane_id)
        elif effective_status == DeliveryStatus.COMPLETED:
            primary_completed_lane_ids.append(lane_live_view.lane_id)
        elif effective_status == DeliveryStatus.FAILED:
            primary_failed_lane_ids.append(lane_live_view.lane_id)
        elif effective_status == DeliveryStatus.BLOCKED:
            primary_blocked_lane_ids.append(lane_live_view.lane_id)
        elif _lane_status_is_active(effective_status):
            primary_active_lane_ids.append(lane_live_view.lane_id)
            if lane_live_view.coordinator_session is not None:
                primary_active_lane_session_ids.append(
                    lane_live_view.coordinator_session.coordinator_id
                )
            elif lane_state is not None and lane_state.session is not None:
                primary_active_lane_session_ids.append(lane_state.session.coordinator_id)
        lane_live_inputs[lane_live_view.lane_id] = input_metadata
        if lane_live_view.pending_shared_digest_count > 0:
            lane_digest_counts[lane_live_view.lane_id] = (
                lane_live_view.pending_shared_digest_count
            )
        if mailbox_followup_turns_used > 0:
            lane_mailbox_followup_turns[lane_live_view.lane_id] = (
                mailbox_followup_turns_used
            )
    return {
        "lane_count": len(live_view.lane_views),
        "host_owned_lane_session_count": sum(
            1 for item in live_view.lane_views if item.coordinator_session is not None
        ),
        "active_lane_ids": list(coordination_state.active_lane_ids),
        "pending_lane_ids": list(coordination_state.pending_lane_ids),
        "completed_lane_ids": list(coordination_state.completed_lane_ids),
        "failed_lane_ids": list(coordination_state.failed_lane_ids),
        "blocked_lane_ids": list(coordination_state.blocked_lane_ids),
        "lane_statuses": {
            lane_state.lane_id: lane_state.status.value
            for lane_state in coordination_state.lane_states
        },
        "active_lane_session_ids": [
            lane_state.session.coordinator_id
            for lane_state in coordination_state.lane_states
            if lane_state.session is not None and _lane_status_is_active(lane_state.status)
        ],
        "objective_shared_digest_count": len(live_view.objective_shared_digests),
        "objective_shared_digest_envelope_ids": [
            digest.envelope_id for digest in live_view.objective_shared_digests
        ],
        "lane_digest_counts": lane_digest_counts,
        "lane_mailbox_followup_turns": lane_mailbox_followup_turns,
        "lane_host_phases": lane_host_phases,
        "lane_shell_statuses": lane_shell_statuses,
        "lane_attach_modes": lane_attach_modes,
        "lane_attach_targets": lane_attach_targets,
        "lane_truth_sources": lane_truth_sources,
        "runtime_native_lane_ids": runtime_native_lane_ids,
        "fallback_lane_ids": fallback_lane_ids,
        "primary_active_lane_ids": primary_active_lane_ids,
        "primary_pending_lane_ids": primary_pending_lane_ids,
        "primary_waiting_for_authority_lane_ids": primary_waiting_for_authority_lane_ids,
        "primary_completed_lane_ids": primary_completed_lane_ids,
        "primary_failed_lane_ids": primary_failed_lane_ids,
        "primary_blocked_lane_ids": primary_blocked_lane_ids,
        "primary_active_lane_session_ids": primary_active_lane_session_ids,
        "primary_lane_statuses": primary_lane_statuses,
        "task_surface_authority_lane_ids": task_surface_authority_lane_ids,
        "lane_task_surface_authority_waiting_task_ids": (
            lane_task_surface_authority_waiting_task_ids
        ),
        "lane_task_surface_authority_waiting_request_ids": (
            lane_task_surface_authority_waiting_request_ids
        ),
        "host_stepped_lane_ids": host_stepped_lane_ids,
        "lane_host_step_counts": lane_host_step_counts,
        "lane_live_inputs": lane_live_inputs,
        "objective_message_runtime": dict(live_view.objective_message_runtime),
        "objective_coordination": dict(live_view.objective_coordination),
    }


def _planning_round_id(objective_id: str) -> str:
    return f"{objective_id}:planning-round:1"


def _slice_to_payload(task: Any) -> dict[str, object]:
    return {
        "slice_id": task.slice_id,
        "title": task.title,
        "goal": task.goal,
        "reason": task.reason,
        "scope": task.scope.value if hasattr(task.scope, "value") else str(task.scope),
        "depends_on": list(getattr(task, "depends_on", ()) or ()),
        "owned_paths": list(getattr(task, "owned_paths", ()) or ()),
        "verification_commands": list(getattr(task, "verification_commands", ()) or ()),
    }


def _parsed_turn_payload(output_text: str) -> dict[str, object]:
    parsed = parse_leader_turn_output(output_text)
    sequential_slices: list[dict[str, object]] = []
    parallel_group_map: dict[str, list[dict[str, object]]] = {}
    for task in parsed.teammate_tasks:
        raw_slice = _slice_to_payload(task)
        if task.slice_mode == "parallel" and task.parallel_group:
            raw_slice["parallel_group"] = task.parallel_group
            parallel_group_map.setdefault(task.parallel_group, []).append(raw_slice)
        else:
            sequential_slices.append(raw_slice)
    parallel_slices = [
        {"parallel_group": group, "slices": slices}
        for group, slices in parallel_group_map.items()
    ]
    return {
        "summary": parsed.summary,
        "sequential_slices": sequential_slices,
        "parallel_slices": parallel_slices,
    }


def _draft_prompt(
    *,
    objective: ObjectiveSpec,
    lane_id: str,
    team_id: str,
) -> str:
    return "\n".join(
        [
            "Planning review phase: draft",
            f"Objective: {objective.title}",
            f"Lane: {lane_id}",
            f"Team: {team_id}",
            "Return JSON with summary, sequential_slices, parallel_slices.",
        ]
    )


def _peer_review_prompt(
    *,
    objective: ObjectiveSpec,
    reviewer_lane_id: str,
    reviewer_team_id: str,
    peer_draft_payloads: tuple[dict[str, object], ...],
) -> str:
    peer_json = "\n".join(
        f"- {item.get('leader_id')} ({item.get('lane_id')}): {item.get('summary')}"
        for item in peer_draft_payloads
    )
    return "\n".join(
        [
            "Planning review phase: peer_review",
            f"Objective: {objective.title}",
            f"Reviewer lane: {reviewer_lane_id}",
            f"Reviewer team: {reviewer_team_id}",
            "Peer drafts (summary-first):",
            peer_json if peer_json else "- (none)",
            (
                "Return JSON: {\"summary\":\"...\",\"reviews\":[{\"target_leader_id\":\"...\","
                "\"target_team_id\":\"...\",\"summary\":\"...\",\"conflict_type\":\"...\","
                "\"severity\":\"...\",\"affected_paths\":[\"...\"],\"suggested_change\":\"...\"}]}"
            ),
        ]
    )


def _revision_prompt(
    *,
    objective: ObjectiveSpec,
    leader_id: str,
    lane_id: str,
    revision_bundle: dict[str, object],
) -> str:
    return "\n".join(
        [
            "Planning review phase: revision",
            f"Objective: {objective.title}",
            f"Leader: {leader_id}",
            f"Lane: {lane_id}",
            "Revision context (summary-first):",
            str(revision_bundle),
            "Return JSON with summary, sequential_slices, parallel_slices.",
        ]
    )


def _supports_planning_review(runtime: GroupRuntime) -> bool:
    required = (
        "publish_leader_draft_plan",
        "list_leader_draft_plans",
        "publish_leader_peer_review",
        "list_leader_peer_reviews",
        "publish_superleader_global_review",
        "build_leader_revision_context_bundle",
        "publish_leader_revised_plan",
        "publish_activation_gate_decision",
    )
    return all(callable(getattr(runtime, item, None)) for item in required)


async def _call_runtime_api(runtime: GroupRuntime, method_name: str, **kwargs: Any) -> Any:
    method = getattr(runtime, method_name)
    signature = inspect.signature(method)
    accepts_kwargs = any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    )
    if accepts_kwargs:
        return await method(**kwargs)
    filtered = {
        key: value
        for key, value in kwargs.items()
        if key in signature.parameters
    }
    return await method(**filtered)


class SuperLeaderRuntime:
    def __init__(
        self,
        *,
        runtime: GroupRuntime,
        evaluator: DeliveryEvaluator | None = None,
        mailbox: MailboxBridge | None = None,
        permission_broker: PermissionBroker | None = None,
        resident_kernel: ResidentCoordinatorKernel | None = None,
    ) -> None:
        self.runtime = runtime
        self.evaluator = evaluator or DefaultDeliveryEvaluator()
        self.mailbox = mailbox or InMemoryMailboxBridge()
        self.permission_broker = permission_broker
        self.resident_kernel = resident_kernel or ResidentCoordinatorKernel()
        self.authority_root_reactor = ObjectiveAuthorityRootReactor(
            runtime=self.runtime,
            mailbox=self.mailbox,
        )
        self._session_memory_service = SessionMemoryService(store=self.runtime.store)

    async def _resolve_continuity_context(
        self,
        *,
        group_id: str,
        objective_id: str,
    ) -> tuple[str | None, str | None]:
        work_session = await self.runtime._resolve_work_session_for_objective(
            group_id=group_id,
            objective_id=objective_id,
        )
        if work_session is None:
            return None, None
        runtime_generation_id = work_session.current_runtime_generation_id
        if runtime_generation_id is None:
            generation = await self.runtime.store.find_latest_resumable_runtime_generation(
                work_session.work_session_id
            )
            runtime_generation_id = (
                generation.runtime_generation_id if generation is not None else None
            )
        if runtime_generation_id is None:
            return None, None
        return work_session.work_session_id, runtime_generation_id

    async def _record_turn(
        self,
        *,
        group_id: str,
        objective_id: str,
        turn_kind: AgentTurnKind,
        input_summary: str,
        output_summary: str,
        status: AgentTurnStatus = AgentTurnStatus.COMPLETED,
        metadata: dict[str, object] | None = None,
    ) -> AgentTurnRecord | None:
        work_session_id, runtime_generation_id = await self._resolve_continuity_context(
            group_id=group_id,
            objective_id=objective_id,
        )
        if work_session_id is None or runtime_generation_id is None:
            return None
        turn_record = await self._session_memory_service.record_role_turn(
            work_session_id=work_session_id,
            runtime_generation_id=runtime_generation_id,
            head_kind=ConversationHeadKind.SUPERLEADER,
            scope_id=objective_id,
            actor_role=AgentTurnActorRole.SUPERLEADER,
            assignment_id=None,
            turn_kind=turn_kind,
            input_summary=_summary_text(input_summary),
            output_summary=_summary_text(output_summary),
            response_id=None,
            status=status,
            created_at=_now_iso(),
            metadata=metadata,
            ensure_conversation_head=True,
            head_checkpoint_summary=_summary_text(output_summary),
            head_backend="superleader_runtime",
            head_model="superleader",
            head_provider="agent_orchestra",
        )
        return turn_record

    async def _record_artifact(
        self,
        *,
        turn_record: AgentTurnRecord | None,
        artifact_kind: ArtifactRefKind,
        uri: str,
        payload: object,
        metadata: dict[str, object] | None = None,
    ) -> None:
        if turn_record is None:
            return
        serialized = json.dumps(payload, ensure_ascii=True)
        artifact = ArtifactRef(
            turn_record_id=turn_record.turn_record_id,
            work_session_id=turn_record.work_session_id,
            runtime_generation_id=turn_record.runtime_generation_id,
            artifact_kind=artifact_kind,
            storage_kind=ArtifactStorageKind.INLINE_JSON,
            uri_or_path=uri,
            content_hash=_artifact_hash(payload),
            size_bytes=len(serialized),
            metadata=dict(metadata or {}),
        )
        await self.runtime.store.save_artifact_ref(artifact)

    async def _resolve_authority_waiting_blockers(
        self,
        *,
        objective_id: str,
        group_id: str,
        pending_lane_ids: set[str],
        lane_states_by_id: dict[str, SuperLeaderLaneCoordinationState],
        lane_results_by_id: dict[str, LeaderLoopResult],
        authority_blockers: tuple[str, ...],
    ) -> AuthorityReactorCycleOutput:
        return await self.authority_root_reactor.resolve_waiting_blockers(
            objective_id=objective_id,
            group_id=group_id,
            pending_lane_ids=pending_lane_ids,
            lane_states_by_id=lane_states_by_id,
            lane_results_by_id=lane_results_by_id,
            authority_blockers=authority_blockers,
        )

    async def _try_ensure_subscription(
        self,
        subscription: MailboxSubscription,
    ) -> MailboxSubscription | None:
        try:
            return await self.mailbox.ensure_subscription(subscription)
        except NotImplementedError:
            return None

    async def _collect_subscription_digests(
        self,
        subscriptions: tuple[MailboxSubscription, ...],
    ) -> tuple[MailboxDigest, ...]:
        digests: list[MailboxDigest] = []
        for subscription in subscriptions:
            if not subscription.subscription_id:
                continue
            try:
                items = await self.mailbox.poll_subscription(
                    subscription.subscriber,
                    subscription_id=subscription.subscription_id,
                    limit=500,
                )
            except (NotImplementedError, ValueError):
                continue
            digests.extend(items)
        return tuple(digests)

    async def _ensure_objective_shared_subscription(
        self,
        *,
        objective_id: str,
        group_id: str,
    ) -> MailboxSubscription | None:
        return await self._try_ensure_subscription(
            MailboxSubscription(
                subscriber=_superleader_subscriber(objective_id),
                subscription_id=_objective_shared_subscription_id(objective_id),
                group_id=group_id,
                visibility_scopes=(MailboxVisibilityScope.SHARED,),
                delivery_mode=MailboxDeliveryMode.SUMMARY_PLUS_REF,
                metadata={"runtime_view": "objective_shared_summary"},
            )
        )

    async def _read_resident_live_view(
        self,
        *,
        objective_id: str,
        lane_order: tuple[str, ...],
        host_owner_coordinator_id: str,
        objective_subscriptions: tuple[MailboxSubscription, ...],
    ) -> SuperLeaderResidentLiveView:
        lane_views = await self.runtime.read_resident_lane_live_views(
            objective_id=objective_id,
            lane_ids=lane_order,
            host_owner_coordinator_id=host_owner_coordinator_id,
        )
        objective_shared_digests = await self._collect_subscription_digests(
            objective_subscriptions
        )
        objective_delivery_state = await self.runtime.store.get_delivery_state(
            _objective_delivery_id(objective_id)
        )
        objective_metadata = (
            objective_delivery_state.metadata
            if objective_delivery_state is not None and isinstance(objective_delivery_state.metadata, dict)
            else {}
        )
        objective_message_runtime = (
            dict(objective_metadata["message_runtime"])
            if isinstance(objective_metadata.get("message_runtime"), dict)
            else {}
        )
        objective_coordination = (
            dict(objective_metadata["coordination"])
            if isinstance(objective_metadata.get("coordination"), dict)
            else {}
        )
        return SuperLeaderResidentLiveView(
            objective_id=objective_id,
            lane_views=lane_views,
            objective_shared_digests=objective_shared_digests,
            objective_message_runtime=objective_message_runtime,
            objective_coordination=objective_coordination,
        )

    async def _publish_superleader_syntheses(
        self,
        *,
        objective: ObjectiveSpec,
        superleader_id: str,
    ) -> dict[str, list[str]]:
        synthesis_ids: list[str] = []
        review_items = await self.runtime.list_review_items(
            objective.objective_id,
            item_kind=ReviewItemKind.PROJECT_ITEM,
        )
        for item in review_items:
            team_reviews = await self.runtime.list_team_position_reviews(item.item_id)
            cross_reviews = await self.runtime.list_cross_team_leader_reviews(item.item_id)
            if not team_reviews and not cross_reviews:
                continue
            based_on_team_ids = tuple(review.position_review_id for review in team_reviews)
            based_on_cross_ids = tuple(review.cross_review_id for review in cross_reviews)
            existing = await self.runtime.get_superleader_synthesis(item.item_id)
            if (
                existing is not None
                and existing.based_on_team_position_review_ids == based_on_team_ids
                and existing.based_on_cross_team_review_ids == based_on_cross_ids
            ):
                synthesis_ids.append(existing.synthesis_id)
                continue
            leader_summaries = [
                review.summary or review.team_stance
                for review in team_reviews
                if review.summary or review.team_stance
            ]
            cross_team_changes = [
                review.what_changed_in_my_understanding or review.suggested_adjustment
                for review in cross_reviews
                if review.what_changed_in_my_understanding or review.suggested_adjustment
            ]
            final_position_parts: list[str] = []
            if leader_summaries:
                final_position_parts.append(
                    "Leader summary only: " + " | ".join(leader_summaries)
                )
            if cross_team_changes:
                final_position_parts.append("Cross-team review: " + " | ".join(cross_team_changes))
            synthesis = await self.runtime.publish_superleader_synthesis(
                item_id=item.item_id,
                superleader_id=superleader_id,
                based_on_team_position_review_ids=based_on_team_ids,
                based_on_cross_team_review_ids=based_on_cross_ids,
                final_position=" ".join(final_position_parts).strip(),
                next_actions=tuple(
                    review.recommended_next_action
                    for review in team_reviews
                    if review.recommended_next_action
                ),
                metadata={"source": "superleader_hierarchical_synthesis"},
            )
            synthesis_ids.append(synthesis.synthesis_id)
        if not synthesis_ids:
            return {}
        return {"superleader_synthesis_ids": synthesis_ids}

    async def _run_pre_activation_planning_review(
        self,
        *,
        round_bundle: HybridTeamRound,
        config: SuperLeaderConfig,
    ) -> tuple[dict[str, str], dict[str, object]]:
        objective = round_bundle.objective
        if not _supports_planning_review(self.runtime):
            return {}, {
                "enabled": False,
                "status": "skipped_runtime_api_unavailable",
            }
        if not round_bundle.leader_rounds:
            return {}, {"enabled": False, "status": "no_leader_rounds"}

        planning_round_id = _planning_round_id(objective.objective_id)
        role_profiles = _resolve_role_profiles(config.role_profiles)
        leader_role_profile = _resolve_role_profile(
            profile_id=config.leader_profile_id,
            role_profiles=role_profiles,
        )
        leader_backend = config.leader_backend or leader_role_profile.backend
        working_dir = config.working_dir
        leader_policy = (
            config.leader_execution_policy
            if config.leader_execution_policy is not None
            else leader_role_profile.to_execution_policy()
        )

        draft_payloads_by_leader: dict[str, dict[str, object]] = {}
        seed_payload_by_lane: dict[str, str] = {}
        leader_rounds = tuple(round_bundle.leader_rounds)

        for leader_round in leader_rounds:
            draft_assignment = compile_leader_assignment(
                objective,
                leader_round,
                turn_index=1,
                backend=leader_backend,
                input_text=_draft_prompt(
                    objective=objective,
                    lane_id=leader_round.lane_id,
                    team_id=leader_round.team_id,
                ),
                working_dir=working_dir,
                extra_metadata={
                    "planning_review_phase": "draft",
                    "planning_round_id": planning_round_id,
                    "runtime_view": "leader_planning_review_draft",
                },
            )
            draft_record = await self.runtime.run_worker_assignment(
                draft_assignment,
                policy=leader_policy,
            )
            if draft_record.status != WorkerStatus.COMPLETED:
                parsed_payload = {
                    "summary": f"{leader_round.lane_id} draft failed; fallback to empty draft.",
                    "sequential_slices": [],
                    "parallel_slices": [],
                }
            else:
                try:
                    parsed_payload = _parsed_turn_payload(draft_record.output_text)
                except ValueError:
                    parsed_payload = {
                        "summary": f"{leader_round.lane_id} draft parse failed; fallback to empty draft.",
                        "sequential_slices": [],
                        "parallel_slices": [],
                    }
            draft_payloads_by_leader[leader_round.leader_task.leader_id] = {
                "objective_id": objective.objective_id,
                "planning_round_id": planning_round_id,
                "leader_id": leader_round.leader_task.leader_id,
                "lane_id": leader_round.lane_id,
                "team_id": leader_round.team_id,
                **parsed_payload,
            }
            await _call_runtime_api(
                self.runtime,
                "publish_leader_draft_plan",
                objective_id=objective.objective_id,
                planning_round_id=planning_round_id,
                leader_id=leader_round.leader_task.leader_id,
                lane_id=leader_round.lane_id,
                team_id=leader_round.team_id,
                summary=parsed_payload["summary"],
                sequential_slices=tuple(parsed_payload["sequential_slices"]),
                parallel_slices=tuple(parsed_payload["parallel_slices"]),
            )

        peer_review_count = 0
        for reviewer_round in leader_rounds:
            reviewer_leader_id = reviewer_round.leader_task.leader_id
            peer_payloads = tuple(
                payload
                for leader_id, payload in draft_payloads_by_leader.items()
                if leader_id != reviewer_leader_id
            )
            peer_assignment = compile_leader_assignment(
                objective,
                reviewer_round,
                turn_index=1,
                backend=leader_backend,
                input_text=_peer_review_prompt(
                    objective=objective,
                    reviewer_lane_id=reviewer_round.lane_id,
                    reviewer_team_id=reviewer_round.team_id,
                    peer_draft_payloads=peer_payloads,
                ),
                working_dir=working_dir,
                extra_metadata={
                    "planning_review_phase": "peer_review",
                    "planning_round_id": planning_round_id,
                    "runtime_view": "leader_planning_review_peer",
                },
            )
            peer_record = await self.runtime.run_worker_assignment(
                peer_assignment,
                policy=leader_policy,
            )
            parsed_peer = None
            if peer_record.status == WorkerStatus.COMPLETED:
                try:
                    parsed_peer = parse_leader_peer_review_output(peer_record.output_text)
                except ValueError:
                    parsed_peer = None

            published_target_ids: set[str] = set()
            if parsed_peer is not None:
                for review in parsed_peer.reviews:
                    await _call_runtime_api(
                        self.runtime,
                        "publish_leader_peer_review",
                        objective_id=objective.objective_id,
                        planning_round_id=planning_round_id,
                        reviewer_leader_id=reviewer_leader_id,
                        reviewer_team_id=reviewer_round.team_id,
                        target_leader_id=review.target_leader_id,
                        target_team_id=review.target_team_id,
                        summary=review.summary,
                        conflict_type=review.conflict_type,
                        severity=review.severity,
                        affected_paths=review.affected_paths,
                        affected_project_items=review.affected_project_items,
                        reason=review.reason,
                        suggested_change=review.suggested_change,
                        requires_superleader_attention=review.requires_superleader_attention,
                        full_text_ref=review.full_text_ref,
                    )
                    peer_review_count += 1
                    published_target_ids.add(review.target_leader_id)

            for target_payload in peer_payloads:
                target_leader_id = str(target_payload["leader_id"])
                if target_leader_id in published_target_ids:
                    continue
                await _call_runtime_api(
                    self.runtime,
                    "publish_leader_peer_review",
                    objective_id=objective.objective_id,
                    planning_round_id=planning_round_id,
                    reviewer_leader_id=reviewer_leader_id,
                    reviewer_team_id=reviewer_round.team_id,
                    target_leader_id=target_leader_id,
                    target_team_id=str(target_payload["team_id"]),
                    summary="No explicit conflict reported in first-cut peer review.",
                    conflict_type="no_conflict",
                    severity="low",
                    affected_paths=(),
                    affected_project_items=(),
                    reason="Autofilled to preserve all-to-all peer review shape.",
                    suggested_change="",
                    requires_superleader_attention=False,
                    full_text_ref=None,
                )
                peer_review_count += 1

        peer_reviews = await _call_runtime_api(
            self.runtime,
            "list_leader_peer_reviews",
            objective_id=objective.objective_id,
            planning_round_id=planning_round_id,
        )
        activation_blockers = tuple(
            sorted(
                {
                    str(getattr(review, "target_leader_id", ""))
                    for review in peer_reviews
                    if str(getattr(review, "severity", "")).lower() in {"critical", "high"}
                    and str(getattr(review, "target_leader_id", "")).strip()
                }
            )
        )
        global_review_summary = (
            f"Planning round {planning_round_id} synthesized {len(draft_payloads_by_leader)} drafts "
            f"and {peer_review_count} peer reviews."
        )
        required_project_item_promotion: tuple[str, ...] = ()
        required_authority_attention: tuple[str, ...] = ()
        required_reordering: tuple[str, ...] = ()
        required_serialization: tuple[str, ...] = ()
        await _call_runtime_api(
            self.runtime,
            "publish_superleader_global_review",
            objective_id=objective.objective_id,
            planning_round_id=planning_round_id,
            superleader_id=_superleader_subscriber(objective.objective_id),
            summary=global_review_summary,
            activation_blockers=activation_blockers,
            required_reordering=required_reordering,
            required_serialization=required_serialization,
            required_project_item_promotion=required_project_item_promotion,
            required_authority_attention=required_authority_attention,
            recommended_adjustments=(),
        )

        revised_plan_count = 0
        for leader_round in leader_rounds:
            revision_bundle = await _call_runtime_api(
                self.runtime,
                "build_leader_revision_context_bundle",
                objective_id=objective.objective_id,
                planning_round_id=planning_round_id,
                leader_id=leader_round.leader_task.leader_id,
            )
            revision_assignment = compile_leader_assignment(
                objective,
                leader_round,
                turn_index=1,
                backend=leader_backend,
                input_text=_revision_prompt(
                    objective=objective,
                    leader_id=leader_round.leader_task.leader_id,
                    lane_id=leader_round.lane_id,
                    revision_bundle=(
                        revision_bundle.to_dict()
                        if hasattr(revision_bundle, "to_dict")
                        else {"bundle": str(revision_bundle)}
                    ),
                ),
                working_dir=working_dir,
                extra_metadata={
                    "planning_review_phase": "revision",
                    "planning_round_id": planning_round_id,
                    "runtime_view": "leader_planning_review_revision",
                },
            )
            revision_record = await self.runtime.run_worker_assignment(
                revision_assignment,
                policy=leader_policy,
            )
            if revision_record.status != WorkerStatus.COMPLETED:
                revised_payload = {
                    "summary": f"{leader_round.lane_id} revision failed; fallback to draft payload.",
                    "sequential_slices": list(
                        draft_payloads_by_leader[leader_round.leader_task.leader_id]["sequential_slices"]
                    ),
                    "parallel_slices": list(
                        draft_payloads_by_leader[leader_round.leader_task.leader_id]["parallel_slices"]
                    ),
                }
            else:
                try:
                    revised_payload = _parsed_turn_payload(revision_record.output_text)
                except ValueError:
                    revised_payload = {
                        "summary": f"{leader_round.lane_id} revision parse failed; fallback to draft payload.",
                        "sequential_slices": list(
                            draft_payloads_by_leader[leader_round.leader_task.leader_id]["sequential_slices"]
                        ),
                        "parallel_slices": list(
                            draft_payloads_by_leader[leader_round.leader_task.leader_id]["parallel_slices"]
                        ),
                    }
            await _call_runtime_api(
                self.runtime,
                "publish_leader_revised_plan",
                objective_id=objective.objective_id,
                planning_round_id=planning_round_id,
                leader_id=leader_round.leader_task.leader_id,
                lane_id=leader_round.lane_id,
                team_id=leader_round.team_id,
                summary=str(revised_payload["summary"]),
                sequential_slices=tuple(revised_payload["sequential_slices"]),
                parallel_slices=tuple(revised_payload["parallel_slices"]),
                revision_bundle_ref=getattr(revision_bundle, "bundle_id", None),
            )
            revised_plan_count += 1
            seed_payload_by_lane[leader_round.lane_id] = json.dumps(revised_payload)

        activation_gate_status = "ready_for_activation"
        activation_gate_summary = "All revised plans are ready for activation."
        activation_gate_blockers = activation_blockers
        if required_authority_attention:
            activation_gate_status = "needs_authority"
            activation_gate_summary = "Activation is waiting on authority attention raised during planning review."
        elif required_project_item_promotion:
            activation_gate_status = "needs_project_item_promotion"
            activation_gate_summary = (
                "Activation is waiting on project item promotion required by planning review."
            )
        elif activation_blockers or required_reordering or required_serialization:
            activation_gate_status = "needs_replan"
            activation_gate_summary = "Planning review found blockers that require replan before activation."

        activation_gate = await _call_runtime_api(
            self.runtime,
            "publish_activation_gate_decision",
            objective_id=objective.objective_id,
            planning_round_id=planning_round_id,
            status=activation_gate_status,
            summary=activation_gate_summary,
            blockers=activation_gate_blockers,
            metadata={
                "activation_blockers": list(activation_blockers),
                "required_reordering": list(required_reordering),
                "required_serialization": list(required_serialization),
                "required_project_item_promotion": list(required_project_item_promotion),
                "required_authority_attention": list(required_authority_attention),
            },
        )

        return seed_payload_by_lane, {
            "enabled": True,
            "planning_round_id": planning_round_id,
            "draft_plan_count": len(draft_payloads_by_leader),
            "peer_review_count": peer_review_count,
            "revised_plan_count": revised_plan_count,
            "activation_blocker_count": len(activation_blockers),
            "activation_blockers": list(activation_blockers),
            "activation_gate": (
                activation_gate.to_dict()
                if hasattr(activation_gate, "to_dict")
                else {
                    "status": activation_gate_status,
                    "summary": activation_gate_summary,
                    "blockers": list(activation_gate_blockers),
                }
            ),
        }

    async def run_planning_result(
        self,
        result: PlanningResult,
        *,
        config: SuperLeaderConfig | None = None,
        created_by: str | None = None,
        leader_backend: str | None = None,
        teammate_backend: str | None = None,
        working_dir: str | None = None,
    ) -> SuperLeaderRunResult:
        active_config = config or SuperLeaderConfig()
        if created_by is not None:
            active_config.created_by = created_by
        if leader_backend is not None:
            active_config.leader_backend = leader_backend
        if teammate_backend is not None:
            active_config.teammate_backend = teammate_backend
        if working_dir is not None:
            active_config.working_dir = working_dir

        round_bundle = await materialize_planning_result(
            self.runtime,
            result,
            created_by=active_config.created_by,
        )
        revised_seed_payload_by_lane, planning_review_metadata = await self._run_pre_activation_planning_review(
            round_bundle=round_bundle,
            config=active_config,
        )
        activation_gate_summary = ""
        activation_gate = planning_review_metadata.get("activation_gate")
        if isinstance(activation_gate, dict):
            activation_gate_summary = str(activation_gate.get("summary") or "")
        await self._record_turn(
            group_id=round_bundle.objective.group_id,
            objective_id=round_bundle.objective.objective_id,
            turn_kind=AgentTurnKind.SUPERLEADER_DECISION,
            input_summary="Planning review round",
            output_summary=activation_gate_summary or "Planning review completed.",
            metadata=dict(planning_review_metadata),
        )
        objective_shared_subscription = await self._ensure_objective_shared_subscription(
            objective_id=round_bundle.objective.objective_id,
            group_id=round_bundle.objective.group_id,
        )
        leader_loop = LeaderLoopSupervisor(
            runtime=self.runtime,
            evaluator=self.evaluator,
            mailbox=self.mailbox,
            permission_broker=self.permission_broker,
        )
        lane_order = tuple(leader_round.lane_id for leader_round in round_bundle.leader_rounds)
        leader_rounds_by_lane = {
            leader_round.lane_id: leader_round for leader_round in round_bundle.leader_rounds
        }
        superleader_coordinator_id = _superleader_subscriber(round_bundle.objective.objective_id)
        max_active_lanes = _lane_scheduler_budget(
            objective_budget=round_bundle.objective.budget,
            lane_count=len(round_bundle.leader_rounds),
            config=active_config,
        )
        lane_dependencies = _lane_dependencies(result=result, round_bundle=round_bundle)
        lane_states_by_id = {
            leader_round.lane_id: SuperLeaderLaneCoordinationState(
                lane_id=leader_round.lane_id,
                team_id=leader_round.team_id,
                dependency_lane_ids=lane_dependencies.get(leader_round.lane_id, ()),
                waiting_on_lane_ids=lane_dependencies.get(leader_round.lane_id, ()),
                session=SuperLeaderLaneSessionState(
                    coordinator_id=leader_round.leader_task.leader_id,
                    host_owner_coordinator_id=superleader_coordinator_id,
                    objective_id=round_bundle.objective.objective_id,
                    lane_id=leader_round.lane_id,
                    team_id=leader_round.team_id,
                    runtime_task_id=leader_round.runtime_task.task_id,
                    metadata={
                        "runtime_view": "leader_lane_session_graph",
                        "launch_mode": "leader_session_host",
                    },
                ),
            )
            for leader_round in round_bundle.leader_rounds
        }
        leader_loop_config = LeaderLoopConfig(
            leader_backend=active_config.leader_backend,
            teammate_backend=active_config.teammate_backend,
            leader_execution_policy=active_config.leader_execution_policy,
            teammate_execution_policy=active_config.teammate_execution_policy,
            leader_profile_id=active_config.leader_profile_id,
            teammate_profile_id=active_config.teammate_profile_id,
            role_profiles=active_config.role_profiles,
            max_leader_turns=active_config.max_leader_turns,
            max_mailbox_followup_turns=active_config.max_mailbox_followup_turns,
            auto_run_teammates=active_config.auto_run_teammates,
            allow_promptless_convergence=active_config.allow_promptless_convergence,
            keep_leader_session_idle=active_config.keep_leader_session_idle,
            keep_teammate_session_idle=active_config.keep_teammate_session_idle,
            working_dir=active_config.working_dir,
        )
        objective_subscriptions = tuple(
            subscription
            for subscription in (objective_shared_subscription,)
            if subscription is not None
        )
        lane_results_by_id: dict[str, LeaderLoopResult] = {}
        active_tasks: dict[asyncio.Task[LeaderLoopResult], str] = {}
        batch_count = 0

        initial_session = ResidentCoordinatorSession(
            coordinator_id=superleader_coordinator_id,
            role="superleader",
            phase=ResidentCoordinatorPhase.BOOTING,
            objective_id=round_bundle.objective.objective_id,
        )

        async def _run_cycle(
            session: ResidentCoordinatorSession,
        ) -> ResidentCoordinatorCycleResult:
            nonlocal batch_count
            live_view = await self._read_resident_live_view(
                objective_id=round_bundle.objective.objective_id,
                lane_order=lane_order,
                host_owner_coordinator_id=superleader_coordinator_id,
                objective_subscriptions=objective_subscriptions,
            )
            for lane_live_view in live_view.lane_views:
                lane_state = lane_states_by_id.get(lane_live_view.lane_id)
                if lane_state is None:
                    continue
                _synchronize_lane_state_from_live_view(
                    lane_state=lane_state,
                    live_view=lane_live_view,
                )

            for lane_id in lane_order:
                lane_state = lane_states_by_id[lane_id]
                if lane_state.status != DeliveryStatus.PENDING:
                    continue
                lane_state.waiting_on_lane_ids = _pending_dependency_ids(
                    lane_id=lane_id,
                    dependencies=lane_dependencies,
                    lane_states_by_id=lane_states_by_id,
                )

            pending_lane_ids = {
                lane_id
                for lane_id in lane_order
                if lane_states_by_id[lane_id].status == DeliveryStatus.PENDING
            }
            lane_live_views_by_id = {
                lane_live_view.lane_id: lane_live_view
                for lane_live_view in live_view.lane_views
            }
            ready_lane_ids = [
                lane_id
                for lane_id in lane_order
                if lane_id in pending_lane_ids
                and not lane_states_by_id[lane_id].waiting_on_lane_ids
            ]
            active_lane_ids = [
                lane_id
                for lane_id in lane_order
                if _lane_status_is_active(lane_states_by_id[lane_id].status)
            ]
            launched_lane_ids: list[str] = []
            stepped_lane_ids: list[str] = []
            if ready_lane_ids and len(active_lane_ids) < max_active_lanes:
                batch_count += 1
            active_task_lane_ids = set(active_tasks.values())
            for lane_id in lane_order:
                lane_state = lane_states_by_id[lane_id]
                lane_live_view = lane_live_views_by_id.get(lane_id)
                if lane_live_view is None:
                    continue
                if not _lane_should_schedule_host_step(
                    lane_state=lane_state,
                    lane_live_view=lane_live_view,
                    objective_shared_digests=live_view.objective_shared_digests,
                    in_flight_lane_ids=active_task_lane_ids,
                ):
                    continue
                session_metadata = dict(
                    lane_state.session.metadata if lane_state.session is not None else {}
                )
                session_metadata["resident_live_inputs"] = _lane_host_step_live_inputs(
                    lane_live_view=lane_live_view,
                    objective_shared_digests=live_view.objective_shared_digests,
                )
                active_tasks[
                    asyncio.create_task(
                        leader_loop.ensure_or_step_session(
                            objective=round_bundle.objective,
                            leader_round=leader_rounds_by_lane[lane_id],
                            host_owner_coordinator_id=superleader_coordinator_id,
                            runtime_task_id=leader_rounds_by_lane[lane_id].runtime_task.task_id,
                            session_metadata=session_metadata,
                            config=replace(
                                leader_loop_config,
                                seed_leader_turn_output=revised_seed_payload_by_lane.get(lane_id),
                                skip_initial_prompt_turn_when_seeded=(
                                    lane_id in revised_seed_payload_by_lane
                                ),
                            ),
                        )
                    )
                ] = lane_id
                active_task_lane_ids.add(lane_id)
                stepped_lane_ids.append(lane_id)
            while ready_lane_ids and (len(active_lane_ids) + len(launched_lane_ids)) < max_active_lanes:
                lane_id = ready_lane_ids.pop(0)
                leader_round = leader_rounds_by_lane[lane_id]
                lane_state = lane_states_by_id[lane_id]
                lane_state.status = DeliveryStatus.RUNNING
                lane_state.waiting_on_lane_ids = ()
                lane_state.started_in_batch = batch_count
                if lane_state.session is not None:
                    session_metadata = dict(lane_state.session.metadata)
                    session_metadata["started_in_batch"] = batch_count
                    lane_state.session = replace(
                        lane_state.session,
                        phase=ResidentCoordinatorPhase.RUNNING,
                        metadata=session_metadata,
                    )
                lane_live_view = lane_live_views_by_id.get(lane_id)
                session_metadata = (
                    dict(lane_state.session.metadata)
                    if lane_state.session is not None
                    else {}
                )
                if lane_live_view is not None:
                    session_metadata["resident_live_inputs"] = _lane_host_step_live_inputs(
                        lane_live_view=lane_live_view,
                        objective_shared_digests=live_view.objective_shared_digests,
                    )
                active_tasks[
                    asyncio.create_task(
                        # Seeded revised plans let lane activation skip an extra leader prompt turn.
                        leader_loop.ensure_or_step_session(
                            objective=round_bundle.objective,
                            leader_round=leader_round,
                            host_owner_coordinator_id=superleader_coordinator_id,
                            runtime_task_id=leader_round.runtime_task.task_id,
                            session_metadata=session_metadata or None,
                            # Per-lane seed payload comes from the pre-activation planning review round.
                            # We clone config at call-site to avoid mutating shared defaults across lanes.
                            config=replace(
                                leader_loop_config,
                                seed_leader_turn_output=revised_seed_payload_by_lane.get(lane_id),
                                skip_initial_prompt_turn_when_seeded=(
                                    lane_id in revised_seed_payload_by_lane
                                ),
                            ),
                        )
                    )
                ] = lane_id
                launched_lane_ids.append(lane_id)

            completed_lane_ids: list[str] = []
            if active_tasks:
                done, _pending = await asyncio.wait(
                    active_tasks.keys(),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for finished_task in done:
                    lane_id = active_tasks.pop(finished_task)
                    lane_result = finished_task.result()
                    aggregated_lane_result = _merge_leader_loop_results(
                        existing=lane_results_by_id.get(lane_id),
                        latest=lane_result,
                    )
                    lane_results_by_id[lane_id] = aggregated_lane_result
                    delivery_state = aggregated_lane_result.delivery_state
                    lane_state = lane_states_by_id[lane_id]
                    lane_state.status = delivery_state.status
                    lane_state.completed_in_batch = batch_count
                    lane_state.iteration = delivery_state.iteration
                    lane_state.summary = delivery_state.summary
                    lane_state.latest_worker_ids = delivery_state.latest_worker_ids
                    lane_state.waiting_on_lane_ids = ()
                    if lane_state.session is not None:
                        lane_state.session = _project_lane_session_state(
                            base_state=lane_state.session,
                            lane_status=delivery_state.status,
                            coordinator_session=aggregated_lane_result.coordinator_session,
                        )
                    completed_lane_ids.append(lane_id)

            for lane_id in lane_order:
                lane_state = lane_states_by_id[lane_id]
                if lane_state.status != DeliveryStatus.PENDING:
                    continue
                lane_state.waiting_on_lane_ids = _pending_dependency_ids(
                    lane_id=lane_id,
                    dependencies=lane_dependencies,
                    lane_states_by_id=lane_states_by_id,
                )

            pending_lane_ids = {
                lane_id
                for lane_id in lane_order
                if lane_states_by_id[lane_id].status == DeliveryStatus.PENDING
            }
            next_ready_lane_ids = [
                lane_id
                for lane_id in lane_order
                if lane_id in pending_lane_ids
                and not lane_states_by_id[lane_id].waiting_on_lane_ids
            ]
            active_lane_ids = [
                lane_id
                for lane_id in lane_order
                if _lane_status_is_active(lane_states_by_id[lane_id].status)
            ]
            active_subordinate_ids = tuple(
                leader_rounds_by_lane[lane_id].leader_task.leader_id
                for lane_id in lane_order
                if lane_id in active_lane_ids
            )
            progress_made = bool(launched_lane_ids or stepped_lane_ids or completed_lane_ids)
            cycle_metadata = {
                "batch_count": batch_count,
                "stepped_lane_ids": stepped_lane_ids,
                "ready_lane_ids": next_ready_lane_ids,
                "pending_lane_ids": [lane_id for lane_id in lane_order if lane_id in pending_lane_ids],
                "active_lane_ids": active_lane_ids,
                "completed_lane_ids": [
                    lane_id
                    for lane_id in lane_order
                    if lane_states_by_id[lane_id].status == DeliveryStatus.COMPLETED
                ],
                "active_lane_session_ids": [
                    lane_states_by_id[lane_id].session.coordinator_id
                    for lane_id in active_lane_ids
                    if lane_states_by_id[lane_id].session is not None
                ],
                "objective_shared_digest_count": len(live_view.objective_shared_digests),
                "objective_shared_digest_envelope_ids": [
                    digest.envelope_id for digest in live_view.objective_shared_digests
                ],
                "lane_digest_counts": {
                    lane_live_view.lane_id: lane_live_view.pending_shared_digest_count
                    for lane_live_view in live_view.lane_views
                    if lane_live_view.pending_shared_digest_count > 0
                },
                "lane_mailbox_followup_turns": {
                    lane_live_view.lane_id: int(
                        lane_live_view.coordination_metadata.get("mailbox_followup_turns_used", 0) or 0
                    )
                    for lane_live_view in live_view.lane_views
                    if int(
                        lane_live_view.coordination_metadata.get("mailbox_followup_turns_used", 0) or 0
                    )
                    > 0
                },
            }
            if live_view.objective_message_runtime:
                cycle_metadata["objective_message_runtime"] = dict(live_view.objective_message_runtime)
            if live_view.objective_coordination:
                cycle_metadata["objective_coordination"] = dict(live_view.objective_coordination)
            lane_mailbox_wait_lane_ids = [
                lane_id
                for lane_id in lane_order
                if any(
                    lane_live_view.lane_id == lane_id
                    and lane_live_view.pending_shared_digest_count > 0
                    for lane_live_view in live_view.lane_views
                )
                or (
                    lane_states_by_id[lane_id].session is not None
                    and lane_states_by_id[lane_id].session.phase
                    == ResidentCoordinatorPhase.WAITING_FOR_MAILBOX
                )
            ]
            cycle_metadata["lane_mailbox_wait_lane_ids"] = lane_mailbox_wait_lane_ids
            mailbox_poll_delta = 1 if objective_subscriptions else 0
            await self._record_turn(
                group_id=round_bundle.objective.group_id,
                objective_id=round_bundle.objective.objective_id,
                turn_kind=AgentTurnKind.SUPERLEADER_DECISION,
                input_summary="Superleader coordination cycle",
                output_summary="Evaluated lane readiness and live view inputs.",
                metadata=dict(cycle_metadata),
            )

            if not pending_lane_ids and not active_lane_ids:
                return ResidentCoordinatorCycleResult(
                    phase=ResidentCoordinatorPhase.QUIESCENT,
                    stop=True,
                    progress_made=progress_made,
                    claimed_task_delta=len(launched_lane_ids),
                    subordinate_dispatch_delta=len(launched_lane_ids),
                    mailbox_poll_delta=mailbox_poll_delta,
                    active_subordinate_ids=(),
                    reason="All currently reachable lanes have converged.",
                    metadata=cycle_metadata,
                )
            if active_lane_ids:
                return ResidentCoordinatorCycleResult(
                    phase=ResidentCoordinatorPhase.WAITING_FOR_SUBORDINATES,
                    progress_made=progress_made,
                    claimed_task_delta=len(launched_lane_ids),
                    subordinate_dispatch_delta=len(launched_lane_ids),
                    mailbox_poll_delta=mailbox_poll_delta,
                    active_subordinate_ids=active_subordinate_ids,
                    reason="Waiting for active leader lanes to complete.",
                    metadata=cycle_metadata,
                )
            if next_ready_lane_ids:
                return ResidentCoordinatorCycleResult(
                    phase=ResidentCoordinatorPhase.RUNNING,
                    progress_made=progress_made,
                    claimed_task_delta=len(launched_lane_ids),
                    subordinate_dispatch_delta=len(launched_lane_ids),
                    mailbox_poll_delta=mailbox_poll_delta,
                    active_subordinate_ids=(),
                    reason="Additional ready lanes are available for the next coordination cycle.",
                    metadata=cycle_metadata,
                )
            if live_view.objective_shared_digests or lane_mailbox_wait_lane_ids:
                await asyncio.sleep(0)
                return ResidentCoordinatorCycleResult(
                    phase=ResidentCoordinatorPhase.WAITING_FOR_MAILBOX,
                    progress_made=True,
                    claimed_task_delta=len(launched_lane_ids),
                    subordinate_dispatch_delta=len(launched_lane_ids),
                    mailbox_poll_delta=mailbox_poll_delta,
                    active_subordinate_ids=(),
                    reason="Objective or lane mailbox digests are available for convergence.",
                    metadata=cycle_metadata,
                )
            authority_blockers = _authority_waiting_blockers(
                pending_lane_ids=pending_lane_ids,
                lane_states_by_id=lane_states_by_id,
            )
            if authority_blockers:
                authority_reaction = await self._resolve_authority_waiting_blockers(
                    objective_id=round_bundle.objective.objective_id,
                    group_id=round_bundle.objective.group_id,
                    pending_lane_ids=pending_lane_ids,
                    lane_states_by_id=lane_states_by_id,
                    lane_results_by_id=lane_results_by_id,
                    authority_blockers=authority_blockers,
                )
                cycle_metadata.update(
                    authority_reaction.to_metadata_patch(
                        last_cycle_at=datetime.now(timezone.utc).isoformat()
                    )
                )
                cycle_metadata["authority_waiting_blockers"] = list(authority_blockers)
                if authority_reaction.decision_request_ids:
                    await asyncio.sleep(0)
                    return ResidentCoordinatorCycleResult(
                        phase=ResidentCoordinatorPhase.RUNNING,
                        progress_made=True,
                        claimed_task_delta=len(launched_lane_ids),
                        subordinate_dispatch_delta=len(launched_lane_ids),
                        mailbox_poll_delta=mailbox_poll_delta,
                        active_subordinate_ids=(),
                        reason="Committed superleader authority decisions for waiting blockers.",
                        metadata=cycle_metadata,
                    )
                await asyncio.sleep(0)
                return ResidentCoordinatorCycleResult(
                    phase=ResidentCoordinatorPhase.WAITING_FOR_DEPENDENCIES,
                    stop=True,
                    progress_made=progress_made,
                    claimed_task_delta=len(launched_lane_ids),
                    subordinate_dispatch_delta=len(launched_lane_ids),
                    mailbox_poll_delta=mailbox_poll_delta,
                    active_subordinate_ids=(),
                    reason="Remaining dependencies are waiting for authority.",
                    metadata=cycle_metadata,
                )
            terminal_blockers = _terminal_dependency_blockers(
                pending_lane_ids=pending_lane_ids,
                lane_states_by_id=lane_states_by_id,
            )
            if terminal_blockers:
                cycle_metadata["terminal_dependency_blockers"] = list(terminal_blockers)
                await asyncio.sleep(0)
                return ResidentCoordinatorCycleResult(
                    phase=ResidentCoordinatorPhase.WAITING_FOR_DEPENDENCIES,
                    stop=True,
                    progress_made=progress_made,
                    claimed_task_delta=len(launched_lane_ids),
                    subordinate_dispatch_delta=len(launched_lane_ids),
                    mailbox_poll_delta=mailbox_poll_delta,
                    active_subordinate_ids=(),
                    reason="Remaining dependencies are blocked or failed.",
                    metadata=cycle_metadata,
                )
            if pending_lane_ids:
                deadlocked_pending_lane_ids = [
                    lane_id for lane_id in lane_order if lane_id in pending_lane_ids
                ]
                cycle_metadata["deadlocked_pending_lane_ids"] = deadlocked_pending_lane_ids
                cycle_metadata["deadlocked_waiting_on_lane_ids"] = {
                    lane_id: list(lane_states_by_id[lane_id].waiting_on_lane_ids)
                    for lane_id in deadlocked_pending_lane_ids
                    if lane_states_by_id[lane_id].waiting_on_lane_ids
                }
                await asyncio.sleep(0)
                return ResidentCoordinatorCycleResult(
                    phase=ResidentCoordinatorPhase.WAITING_FOR_DEPENDENCIES,
                    stop=True,
                    progress_made=progress_made,
                    claimed_task_delta=len(launched_lane_ids),
                    subordinate_dispatch_delta=len(launched_lane_ids),
                    mailbox_poll_delta=mailbox_poll_delta,
                    active_subordinate_ids=(),
                    reason="Pending lanes cannot progress under the current dependency state.",
                    metadata=cycle_metadata,
                )
            return ResidentCoordinatorCycleResult(
                phase=ResidentCoordinatorPhase.WAITING_FOR_DEPENDENCIES,
                stop=True,
                progress_made=progress_made,
                claimed_task_delta=len(launched_lane_ids),
                subordinate_dispatch_delta=len(launched_lane_ids),
                mailbox_poll_delta=mailbox_poll_delta,
                active_subordinate_ids=(),
                reason="No ready lanes remain under the current dependency state.",
                metadata=cycle_metadata,
            )

        async def _finalize(
            final_session: ResidentCoordinatorSession,
        ) -> SuperLeaderRunResult:
            live_view = await self._read_resident_live_view(
                objective_id=round_bundle.objective.objective_id,
                lane_order=lane_order,
                host_owner_coordinator_id=superleader_coordinator_id,
                objective_subscriptions=objective_subscriptions,
            )
            for lane_live_view in live_view.lane_views:
                lane_state = lane_states_by_id.get(lane_live_view.lane_id)
                if lane_state is None:
                    continue
                _synchronize_lane_state_from_live_view(
                    lane_state=lane_state,
                    live_view=lane_live_view,
                )
            for lane_id in lane_order:
                lane_state = lane_states_by_id[lane_id]
                if lane_state.status != DeliveryStatus.PENDING:
                    continue
                lane_state.waiting_on_lane_ids = _pending_dependency_ids(
                    lane_id=lane_id,
                    dependencies=lane_dependencies,
                    lane_states_by_id=lane_states_by_id,
                )
            coordination_state = _build_coordination_state(
                coordinator_id=superleader_coordinator_id,
                objective_id=round_bundle.objective.objective_id,
                lane_order=lane_order,
                max_active_lanes=max_active_lanes,
                batch_count=batch_count,
                lane_states_by_id=lane_states_by_id,
            )
            lane_results = [
                lane_results_by_id[lane_id]
                for lane_id in lane_order
                if lane_id in lane_results_by_id
            ]
            persisted_lane_delivery_by_id = {
                lane_live_view.lane_id: lane_live_view.delivery_state
                for lane_live_view in live_view.lane_views
                if lane_live_view.delivery_state is not None
            }
            lane_delivery_states = tuple(
                persisted_lane_delivery_by_id[lane_id]
                if lane_id in persisted_lane_delivery_by_id
                else lane_results_by_id[lane_id].delivery_state
                if lane_id in lane_results_by_id
                else _lane_delivery_state_from_coordination_state(
                    objective_id=round_bundle.objective.objective_id,
                    lane_state=lane_states_by_id[lane_id],
                )
                for lane_id in lane_order
            )

            authority_state: AuthorityState | None = None
            for lane_result in lane_results:
                if lane_result.delivery_state.status != DeliveryStatus.COMPLETED:
                    continue
                acceptance_checks = tuple(
                    str(item)
                    for item in lane_result.leader_round.leader_task.metadata.get("acceptance_checks", [])
                    if str(item)
                )
                verification_summary = {
                    "status": lane_result.delivery_state.status.value,
                    "summary": lane_result.delivery_state.summary,
                }
                await self.runtime.record_handoff(
                    group_id=round_bundle.objective.group_id,
                    from_team_id=lane_result.leader_round.team_id,
                    to_team_id=_authority_root_team_id(round_bundle.objective.group_id),
                    task_id=lane_result.leader_round.runtime_task.task_id,
                    summary=lane_result.delivery_state.summary,
                    contract_assertions=acceptance_checks,
                    verification_summary=verification_summary,
                )

            if lane_delivery_states and all(
                item.status == DeliveryStatus.COMPLETED for item in lane_delivery_states
            ):
                authority_state = await self.runtime.reduce_group(round_bundle.objective.group_id)

            objective_evaluation = await self.evaluator.evaluate_objective(
                objective_id=round_bundle.objective.objective_id,
                lane_states=lane_delivery_states,
                authority_state=authority_state,
            )
            objective_digests = live_view.objective_shared_digests
            objective_metadata = dict(objective_evaluation.metadata)
            objective_metadata["coordination"] = coordination_state.to_dict()
            objective_metadata["resident_live_view"] = _resident_live_view_metadata(
                coordination_state=coordination_state,
                live_view=live_view,
            )
            objective_metadata["planning_review"] = dict(planning_review_metadata)
            if isinstance(planning_review_metadata.get("activation_gate"), dict):
                objective_metadata["activation_gate"] = dict(planning_review_metadata["activation_gate"])
            objective_metadata["message_runtime"] = {
                "objective_shared_subscription_id": (
                    objective_shared_subscription.subscription_id
                    if objective_shared_subscription is not None
                    else None
                ),
                "objective_shared_digest_count": len(objective_digests),
                "objective_shared_digest_envelope_ids": [digest.envelope_id for digest in objective_digests],
                "lane_shared_subscription_ids": sorted(
                    {
                        subscription.subscription_id
                        for lane_result in lane_results
                        for subscription in lane_result.message_subscriptions
                        if subscription.subscription_id
                    }
                ),
            }
            if authority_state is not None:
                objective_metadata.update(
                    {
                        "authority_status": authority_state.status.value,
                        "accepted_handoffs": list(authority_state.accepted_handoffs),
                        "authority_updated_task_ids": list(authority_state.updated_task_ids),
                    }
                )
            elif "authority_status" not in objective_metadata:
                objective_metadata["authority_status"] = AuthorityStatus.PENDING.value
            hierarchical_review_metadata = await self._publish_superleader_syntheses(
                objective=round_bundle.objective,
                superleader_id=superleader_coordinator_id,
            )
            if hierarchical_review_metadata:
                objective_metadata["hierarchical_review"] = hierarchical_review_metadata
            objective_state = DeliveryState(
                delivery_id=_objective_delivery_id(round_bundle.objective.objective_id),
                objective_id=round_bundle.objective.objective_id,
                kind=DeliveryStateKind.OBJECTIVE,
                status=objective_evaluation.status,
                iteration=len(lane_results),
                summary=objective_evaluation.summary,
                latest_worker_ids=tuple(
                    worker_id
                    for lane_result in lane_results
                    for worker_id in lane_result.delivery_state.latest_worker_ids
                ),
                metadata=objective_metadata,
            )
            await self.runtime.store.save_delivery_state(objective_state)
            final_turn = await self._record_turn(
                group_id=round_bundle.objective.group_id,
                objective_id=round_bundle.objective.objective_id,
                turn_kind=AgentTurnKind.SUPERLEADER_DECISION,
                input_summary="Finalize objective delivery state",
                output_summary=objective_state.summary,
                metadata={
                    "objective_status": objective_state.status.value,
                    "completed_lane_count": len(coordination_state.completed_lane_ids),
                    "blocked_lane_count": len(coordination_state.blocked_lane_ids),
                    "failed_lane_count": len(coordination_state.failed_lane_ids),
                },
            )
            resident_live_view_payload = objective_metadata.get("resident_live_view", {})
            if resident_live_view_payload:
                await self._record_artifact(
                    turn_record=final_turn,
                    artifact_kind=ArtifactRefKind.DELIVERY_SNAPSHOT,
                    uri=f"superleader:{round_bundle.objective.objective_id}:resident_live_view",
                    payload=resident_live_view_payload,
                    metadata={"objective_id": round_bundle.objective.objective_id},
                )

            final_metadata = dict(final_session.metadata)
            final_metadata.update(
                {
                    "delivery_status": objective_state.status.value,
                    "completed_lane_count": len(coordination_state.completed_lane_ids),
                    "blocked_lane_count": len(coordination_state.blocked_lane_ids),
                    "failed_lane_count": len(coordination_state.failed_lane_ids),
                }
            )
            final_phase = final_session.phase
            if objective_state.status == DeliveryStatus.FAILED:
                final_phase = ResidentCoordinatorPhase.FAILED
            elif objective_state.status == DeliveryStatus.BLOCKED:
                final_phase = ResidentCoordinatorPhase.WAITING_FOR_DEPENDENCIES
            final_session = replace(
                final_session,
                phase=final_phase,
                metadata=final_metadata,
            )

            return SuperLeaderRunResult(
                round_bundle=round_bundle,
                lane_results=tuple(lane_results),
                coordination_state=coordination_state,
                objective_state=objective_state,
                message_subscriptions=objective_subscriptions,
                message_digests=objective_digests,
                coordinator_session=final_session,
            )

        return await self.resident_kernel.run(
            session=initial_session,
            step=_run_cycle,
            finalize=_finalize,
        )

    async def run_template(
        self,
        *,
        planner: Planner,
        template: ObjectiveTemplate,
        config: SuperLeaderConfig | None = None,
    ) -> SuperLeaderRunResult:
        planning_result = await self.runtime.plan_from_template(planner, template)
        return await self.run_planning_result(planning_result, config=config)
