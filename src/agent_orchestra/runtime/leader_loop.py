from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent_orchestra.contracts.authority import (
    AuthorityDecision,
    ScopeExtensionRequest,
)
from agent_orchestra.contracts.agent import CoordinatorSessionState, TeammateActivationProfile
from agent_orchestra.contracts.blackboard import (
    BlackboardKind,
    BlackboardSnapshot,
)
from agent_orchestra.contracts.delivery import DeliveryState, DeliveryStateKind, DeliveryStatus
from agent_orchestra.contracts.enums import TaskScope, TaskStatus, WorkerStatus
from agent_orchestra.contracts.execution import (
    ResidentCoordinatorPhase,
    ResidentCoordinatorSession,
    WorkerAssignment,
    WorkerExecutionPolicy,
    WorkerRecord,
    WorkerSessionStatus,
)
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
    ToolInvocationKind,
    ToolInvocationRecord,
)
from agent_orchestra.contracts.task import TaskCard
from agent_orchestra.contracts.task_review import build_task_review_digest
from agent_orchestra.contracts.hierarchical_review import ReviewItemKind
from agent_orchestra.contracts.worker_protocol import (
    WorkerExecutionContract,
    WorkerLeasePolicy,
    WorkerRoleProfile,
)
from agent_orchestra.runtime.authority_reactor import TeamAuthorityReactor, TeamAuthorityReactorResult
from agent_orchestra.runtime.bootstrap_round import LeaderRound, compile_leader_assignment
from agent_orchestra.runtime.evaluator import DefaultDeliveryEvaluator, DeliveryEvaluator
from agent_orchestra.runtime.group_runtime import GroupRuntime
from agent_orchestra.runtime.leader_coordinator import LeaderCoordinator
from agent_orchestra.runtime.leader_output_protocol import ingest_leader_turn_output
from agent_orchestra.runtime.leader_output_protocol import parse_leader_turn_output
from agent_orchestra.runtime.protocol_bridge import InMemoryMailboxBridge
from agent_orchestra.runtime.resident_kernel import ResidentCoordinatorCycleResult, ResidentCoordinatorKernel
from agent_orchestra.runtime.session_memory import SessionMemoryService
from agent_orchestra.runtime.teammate_runtime import (
    ResidentTeammateRunResult,
)
from agent_orchestra.runtime.teammate_work_surface import (
    TeammateWorkSurface,
    build_pending_teammate_assignment,
    mailbox_cursor_last_envelope_id as _mailbox_cursor_last_envelope_id_impl,
)
from agent_orchestra.tools.mailbox import (
    MailboxBridge,
    MailboxDeliveryMode,
    MailboxDigest,
    MailboxEnvelope,
    MailboxMessageKind,
    MailboxSubscription,
    MailboxVisibilityScope,
)
from agent_orchestra.tools.permission_protocol import PermissionBroker, PermissionDecision, PermissionRequest, StaticPermissionBroker


def _lane_delivery_id(objective_id: str, lane_id: str) -> str:
    return f"{objective_id}:lane:{lane_id}"


def _superleader_subscriber(objective_id: str) -> str:
    return f"superleader:{objective_id}"


def _leader_lane_shared_subscription_id(objective_id: str, lane_id: str) -> str:
    return f"{objective_id}:lane:{lane_id}:leader-shared-summary"


def _superleader_lane_shared_subscription_id(objective_id: str, lane_id: str) -> str:
    return f"{objective_id}:lane:{lane_id}:superleader-shared-summary"


def _leader_resident_session_id(objective_id: str, lane_id: str) -> str:
    return f"{objective_id}:lane:{lane_id}:leader:resident"


def _leader_session_metadata(
    metadata: dict[str, object] | None = None,
) -> dict[str, object]:
    merged: dict[str, object] = {
        "runtime_view": "leader_lane_session_graph",
        "launch_mode": "leader_session_host",
    }
    if metadata:
        merged.update(metadata)
    return merged


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


def _mailbox_snapshot(
    messages: tuple[MailboxEnvelope, ...],
) -> list[dict[str, object]]:
    snapshot: list[dict[str, object]] = []
    for message in messages:
        snapshot.append(
            {
                "envelope_id": message.envelope_id,
                "sender": message.sender,
                "recipient": message.recipient,
                "subject": message.subject,
                "kind": message.kind.value if hasattr(message.kind, "value") else message.kind,
                "summary": message.summary,
                "tags": list(message.tags),
                "severity": message.severity,
            }
        )
    return snapshot


def _approval_status_from_permission_decision(decision: PermissionDecision) -> str:
    if decision.approved:
        return "approved"
    if decision.pending:
        return "pending"
    return "denied"


def _teammate_slot_session_id(
    team_id: str,
    teammate_slot: int,
) -> str:
    return f"{team_id}:teammate:{teammate_slot}:resident"


def _resident_session_from_coordinator_state(
    state: CoordinatorSessionState,
) -> ResidentCoordinatorSession:
    return ResidentCoordinatorSession(
        coordinator_id=state.coordinator_id,
        role=state.role,
        phase=state.phase,
        objective_id=state.objective_id or "",
        lane_id=state.lane_id,
        team_id=state.team_id,
        cycle_count=state.cycle_count,
        prompt_turn_count=state.prompt_turn_count,
        claimed_task_count=state.claimed_task_count,
        subordinate_dispatch_count=state.subordinate_dispatch_count,
        mailbox_poll_count=state.mailbox_poll_count,
        active_subordinate_ids=state.active_subordinate_ids,
        mailbox_cursor=state.mailbox_cursor,
        last_reason=state.last_reason,
        metadata=dict(state.metadata),
    )


def _default_permission_broker() -> PermissionBroker:
    return StaticPermissionBroker(
        decision=PermissionDecision(
            approved=True,
            reviewer="system.auto",
            reason="Automatically approved.",
        )
    )


def _format_task(task: TaskCard) -> str:
    return f"- [{task.status.value}] {task.task_id}: {task.goal}"


def _format_mailbox(messages: tuple[MailboxEnvelope, ...]) -> list[str]:
    if not messages:
        return ["Mailbox:", "- (empty)"]
    lines = ["Mailbox:"]
    for message in messages:
        lines.append(
            f"- {message.envelope_id or 'unknown'} {message.subject} from {message.sender}"
        )
    return lines


def _build_turn_input(
    *,
    turn_index: int,
    visible_tasks: tuple[TaskCard, ...],
    leader_lane_snapshot: BlackboardSnapshot,
    team_snapshot: BlackboardSnapshot,
    mailbox_messages: tuple[MailboxEnvelope, ...],
) -> str:
    lines = [
        f"Leader coordination turn {turn_index}.",
        "Visible tasks:",
    ]
    if visible_tasks:
        lines.extend(_format_task(task) for task in visible_tasks)
    else:
        lines.append("- (no visible tasks)")
    lines.extend(
        [
            f"Leader-lane snapshot: {leader_lane_snapshot.summary}",
            f"Team snapshot: {team_snapshot.summary}",
        ]
    )
    lines.extend(_format_mailbox(mailbox_messages))
    lines.append("Return valid JSON only.")
    return "\n".join(lines)


@dataclass(slots=True)
class LeaderLoopConfig:
    leader_backend: str | None = None
    teammate_backend: str | None = None
    leader_execution_policy: WorkerExecutionPolicy | None = None
    teammate_execution_policy: WorkerExecutionPolicy | None = None
    leader_profile_id: str = "leader_in_process_fast"
    teammate_profile_id: str = "teammate_in_process_fast"
    role_profiles: dict[str, WorkerRoleProfile] | None = None
    max_leader_turns: int | None = None
    max_mailbox_followup_turns: int | None = 8
    auto_run_teammates: bool = True
    allow_promptless_convergence: bool = True
    working_dir: str | None = None
    keep_leader_session_idle: bool = True
    keep_teammate_session_idle: bool = False
    seed_leader_turn_output: str | dict[str, object] | None = None
    skip_initial_prompt_turn_when_seeded: bool = False
    host_step_max_cycles: int | None = 1


class _BoundedResidentKernel(ResidentCoordinatorKernel):
    def __init__(self, *, max_cycles: int) -> None:
        super().__init__()
        self.max_cycles = max(max_cycles, 1)

    async def run(self, *, session, step, finalize):
        current = replace(session)
        cycle_count = 0
        while True:
            cycle_result = await step(current)
            cycle_count += 1
            applied_result = cycle_result
            if cycle_count >= self.max_cycles and not cycle_result.stop:
                applied_result = replace(cycle_result, stop=True)
            current = self._apply_cycle_result(current, applied_result)
            if applied_result.stop:
                return await finalize(current)


def _build_role_profile(
    *,
    profile_id: str,
    backend: str,
    mode: str,
) -> WorkerRoleProfile:
    teammate_code_edit = mode == "teammate_code_edit"
    contract = WorkerExecutionContract(
        contract_id=f"{profile_id}.contract",
        mode=mode,
        allow_subdelegation=mode.startswith("leader"),
        require_final_report=True,
        require_verification_results=teammate_code_edit,
        completion_requires_verification_success=teammate_code_edit,
        required_artifact_kinds=("summary", "verification") if teammate_code_edit else ("summary",),
    )
    lease_policy = WorkerLeasePolicy(
        accept_deadline_seconds=30.0,
        renewal_timeout_seconds=120.0,
        hard_deadline_seconds=1800.0,
    )
    keep_session_idle = profile_id == "leader_in_process_fast"
    fallback_kwargs: dict[str, Any] = {}
    if backend == "codex_cli":
        fallback_kwargs = {
            "fallback_idle_timeout_seconds": 120.0,
            "fallback_hard_timeout_seconds": 2400.0,
            "fallback_max_attempts": 3,
            "fallback_resume_on_timeout": False,
            "fallback_allow_relaunch": False,
            "fallback_backoff_seconds": 2.0,
            "fallback_provider_unavailable_backoff_initial_seconds": 15.0,
            "fallback_provider_unavailable_backoff_multiplier": 2.0,
            "fallback_provider_unavailable_backoff_max_seconds": 120.0,
        }
    profile = WorkerRoleProfile(
        profile_id=profile_id,
        backend=backend,
        execution_contract=contract,
        lease_policy=lease_policy,
        keep_session_idle=keep_session_idle,
        reactivate_idle_session=True,
        **fallback_kwargs,
    )
    if backend == "codex_cli":
        return align_role_profile_timeouts(
            profile,
            idle_timeout_seconds=120.0,
            hard_timeout_seconds=2400.0,
        )
    return profile


def align_role_profile_timeouts(
    profile: WorkerRoleProfile,
    *,
    idle_timeout_seconds: float | None = None,
    hard_timeout_seconds: float | None = None,
) -> WorkerRoleProfile:
    effective_idle_timeout_seconds = idle_timeout_seconds
    if effective_idle_timeout_seconds is None:
        effective_idle_timeout_seconds = (
            profile.fallback_idle_timeout_seconds
            if profile.fallback_idle_timeout_seconds is not None
            else profile.lease_policy.renewal_timeout_seconds
        )
    effective_hard_timeout_seconds = hard_timeout_seconds
    if effective_hard_timeout_seconds is None:
        effective_hard_timeout_seconds = (
            profile.fallback_hard_timeout_seconds
            if profile.fallback_hard_timeout_seconds is not None
            else profile.lease_policy.hard_deadline_seconds
        )
    effective_hard_timeout_seconds = max(
        float(effective_hard_timeout_seconds),
        float(effective_idle_timeout_seconds),
    )
    return replace(
        profile,
        lease_policy=replace(
            profile.lease_policy,
            renewal_timeout_seconds=float(effective_idle_timeout_seconds),
            hard_deadline_seconds=effective_hard_timeout_seconds,
        ),
        fallback_idle_timeout_seconds=float(effective_idle_timeout_seconds),
        fallback_hard_timeout_seconds=effective_hard_timeout_seconds,
    )


def build_runtime_role_profiles() -> dict[str, WorkerRoleProfile]:
    profiles = (
        _build_role_profile(
            profile_id="leader_in_process_fast",
            backend="in_process",
            mode="leader_coordination",
        ),
        _build_role_profile(
            profile_id="teammate_in_process_fast",
            backend="in_process",
            mode="teammate_code_edit",
        ),
        _build_role_profile(
            profile_id="leader_codex_cli_long_turn",
            backend="codex_cli",
            mode="leader_coordination",
        ),
        _build_role_profile(
            profile_id="teammate_codex_cli_code_edit",
            backend="codex_cli",
            mode="teammate_code_edit",
        ),
    )
    return {
        profile.profile_id: profile
        for profile in profiles
    }


def _resolve_role_profiles(
    role_profiles: dict[str, WorkerRoleProfile] | None,
) -> dict[str, WorkerRoleProfile]:
    merged = build_runtime_role_profiles()
    if role_profiles:
        merged.update(role_profiles)
    return merged


def _resolve_role_profile(
    *,
    profile_id: str,
    role_profiles: dict[str, WorkerRoleProfile],
) -> WorkerRoleProfile:
    try:
        return role_profiles[profile_id]
    except KeyError as exc:  # pragma: no cover - defensive path
        available = ", ".join(sorted(role_profiles))
        raise ValueError(
            f"Unknown role profile: {profile_id}. Available profiles: {available}"
        ) from exc


def _lifecycle_policy(
    *,
    keep_idle: bool,
    base_policy: WorkerExecutionPolicy | None = None,
) -> WorkerExecutionPolicy | None:
    if base_policy is None:
        if not keep_idle:
            return None
        return WorkerExecutionPolicy(
            keep_session_idle=True,
            reactivate_idle_session=True,
        )
    return replace(
        base_policy,
        keep_session_idle=keep_idle,
        reactivate_idle_session=True if keep_idle else base_policy.reactivate_idle_session,
    )


def _metadata_int(
    metadata: dict[str, object] | None,
    key: str,
    *,
    default: int = 0,
) -> int:
    if not metadata:
        return default
    value = metadata.get(key, default)
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(parsed, 0)


def _mailbox_cursor_last_envelope_id(
    cursor: object,
) -> str | None:
    return _mailbox_cursor_last_envelope_id_impl(cursor)


def _pending_mailbox_count(state: DeliveryState | None) -> int:
    if state is None:
        return 0
    message_runtime = state.metadata.get("message_runtime")
    if isinstance(message_runtime, dict):
        pending_digest_count = message_runtime.get("pending_shared_digest_count")
        try:
            parsed = int(pending_digest_count)
        except (TypeError, ValueError):
            parsed = 0
        if parsed > 0:
            return parsed
    return _metadata_int(state.metadata, "pending_mailbox_count")


def _ordered_unique_strings(values: list[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        if not value or value in seen:
            continue
        seen.add(value)
        ordered.append(value)
    return tuple(ordered)


def _routine_teammate_mailbox_evidence(
    message: MailboxEnvelope,
) -> bool:
    if message.kind == MailboxMessageKind.TEAMMATE_RESULT:
        return True
    return (
        message.kind == MailboxMessageKind.SYSTEM
        and message.subject == "task.receipt"
    )


def _leader_mailbox_requires_prompt_turn(
    mailbox_messages: tuple[MailboxEnvelope, ...],
) -> bool:
    return any(
        not _routine_teammate_mailbox_evidence(message)
        for message in mailbox_messages
    )


def _should_resume_promptless_convergence(
    state: DeliveryState | None,
) -> bool:
    if state is not None and isinstance(state.metadata, dict):
        explicit = state.metadata.get("pending_continuous_convergence")
        if isinstance(explicit, bool):
            return explicit
    return (
        state is not None
        and state.status in {DeliveryStatus.RUNNING, DeliveryStatus.WAITING}
    )


def _teammate_capacity(leader_round: LeaderRound) -> int:
    return max(leader_round.leader_task.budget.max_teammates, 0)


def _coordination_metadata(
    *,
    metadata: dict[str, object] | None,
    max_turns: int,
    mailbox_followup_turns_used: int,
    mailbox_followup_turn_limit: int | None,
    pending_continuous_convergence: bool | None = None,
) -> dict[str, object]:
    merged: dict[str, object] = {
        "base_turn_budget": max_turns,
        "mailbox_followup_turns_used": mailbox_followup_turns_used,
    }
    if mailbox_followup_turn_limit is not None:
        merged["mailbox_followup_turn_limit"] = mailbox_followup_turn_limit
    if pending_continuous_convergence is not None:
        merged["pending_continuous_convergence"] = pending_continuous_convergence
    if metadata:
        merged.update(metadata)
    return merged


@dataclass(slots=True)
class LeaderTurnRecord:
    turn_index: int
    leader_assignment: WorkerAssignment
    leader_record: WorkerRecord
    teammate_records: tuple[WorkerRecord, ...] = ()
    created_task_ids: tuple[str, ...] = ()
    claimed_task_ids: tuple[str, ...] = ()
    claim_session_ids: tuple[str, ...] = ()
    consumed_mailbox_ids: tuple[str, ...] = ()
    produced_mailbox_ids: tuple[str, ...] = ()
    directed_claimed_task_ids: tuple[str, ...] = ()
    autonomous_claimed_task_ids: tuple[str, ...] = ()
    processed_teammate_mailbox_ids: tuple[str, ...] = ()
    teammate_execution_evidence: bool = False


@dataclass(slots=True)
class LeaderLoopResult:
    leader_round: LeaderRound
    delivery_state: DeliveryState
    leader_records: tuple[WorkerRecord, ...]
    teammate_records: tuple[WorkerRecord, ...]
    mailbox_cursor: str | None = None
    mailbox_envelopes: tuple[MailboxEnvelope, ...] = ()
    message_subscriptions: tuple[MailboxSubscription, ...] = ()
    message_digests: tuple[MailboxDigest, ...] = ()
    created_task_ids: tuple[str, ...] = ()
    turns: tuple[LeaderTurnRecord, ...] = ()
    coordinator_session: ResidentCoordinatorSession | None = None


def compile_leader_turn_assignment(
    *,
    objective: ObjectiveSpec,
    leader_round: LeaderRound,
    turn_index: int,
    visible_tasks: tuple[TaskCard, ...],
    leader_lane_snapshot: BlackboardSnapshot,
    team_snapshot: BlackboardSnapshot,
    mailbox_messages: tuple[MailboxEnvelope, ...],
    backend: str = "in_process",
    working_dir: str | None = None,
    previous_response_id: str | None = None,
    role_profile: WorkerRoleProfile | None = None,
) -> WorkerAssignment:
    return compile_leader_assignment(
        objective,
        leader_round,
        turn_index=turn_index,
        backend=backend,
        input_text=_build_turn_input(
            turn_index=turn_index,
            visible_tasks=visible_tasks,
            leader_lane_snapshot=leader_lane_snapshot,
            team_snapshot=team_snapshot,
            mailbox_messages=mailbox_messages,
        ),
        working_dir=working_dir,
        previous_response_id=previous_response_id,
        role_profile=role_profile,
        extra_metadata={
            "turn_index": turn_index,
            "visible_task_ids": [task.task_id for task in visible_tasks],
            "mailbox_count": len(mailbox_messages),
        },
    )


def _seed_output_text(payload: str | dict[str, object] | None) -> str | None:
    if payload is None:
        return None
    parsed = parse_leader_turn_output(payload)
    sequential_slices: list[dict[str, object]] = []
    parallel_group_map: dict[str, list[dict[str, object]]] = {}
    for task in parsed.teammate_tasks:
        raw_slice = {
            "slice_id": task.slice_id,
            "title": task.title,
            "goal": task.goal,
            "reason": task.reason,
            "scope": task.scope.value,
            "depends_on": list(task.depends_on),
            "owned_paths": list(task.owned_paths),
            "verification_commands": list(task.verification_commands),
        }
        if task.slice_mode == "parallel" and task.parallel_group:
            raw_slice["parallel_group"] = task.parallel_group
            parallel_group_map.setdefault(task.parallel_group, []).append(raw_slice)
        else:
            sequential_slices.append(raw_slice)
    parallel_slices = [
        {"parallel_group": group, "slices": slices}
        for group, slices in parallel_group_map.items()
    ]
    return json.dumps(
        {
            "summary": parsed.summary,
            "sequential_slices": sequential_slices,
            "parallel_slices": parallel_slices,
        }
    )


class LeaderLoopSupervisor:
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
        self.permission_broker = permission_broker or _default_permission_broker()
        self.resident_kernel = resident_kernel or ResidentCoordinatorKernel()
        self._session_memory_service = SessionMemoryService(store=self.runtime.store)

    def _session_host(self):
        supervisor = self.runtime.supervisor
        if supervisor is None:
            return None
        return getattr(supervisor, "session_host", None)

    async def _resolve_continuity_context(
        self,
        *,
        objective: ObjectiveSpec,
    ) -> tuple[str | None, str | None]:
        work_session = await self.runtime._resolve_work_session_for_objective(
            group_id=objective.group_id,
            objective_id=objective.objective_id,
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
        objective: ObjectiveSpec,
        leader_round: LeaderRound,
        actor_role: AgentTurnActorRole,
        turn_kind: AgentTurnKind,
        input_summary: str,
        output_summary: str,
        assignment_id: str | None = None,
        response_id: str | None = None,
        status: AgentTurnStatus = AgentTurnStatus.COMPLETED,
        metadata: dict[str, object] | None = None,
    ) -> AgentTurnRecord | None:
        work_session_id, runtime_generation_id = await self._resolve_continuity_context(
            objective=objective,
        )
        if work_session_id is None or runtime_generation_id is None:
            return None
        turn_record = await self._session_memory_service.record_role_turn(
            work_session_id=work_session_id,
            runtime_generation_id=runtime_generation_id,
            head_kind=ConversationHeadKind.LEADER_LANE,
            scope_id=leader_round.lane_id,
            actor_role=actor_role,
            assignment_id=assignment_id,
            turn_kind=turn_kind,
            input_summary=_summary_text(input_summary),
            output_summary=_summary_text(output_summary),
            response_id=response_id,
            status=status,
            created_at=_now_iso(),
            metadata=metadata,
            ensure_conversation_head=True,
            head_checkpoint_summary=_summary_text(output_summary),
            head_backend="leader_loop",
            head_model="leader_lane",
            head_provider="agent_orchestra",
        )
        return turn_record

    async def _record_mailbox_followup(
        self,
        *,
        objective: ObjectiveSpec,
        leader_round: LeaderRound,
        messages: tuple[MailboxEnvelope, ...],
        mailbox_cursor: str | None,
        reason: str,
    ) -> None:
        if not messages:
            return
        turn_record = await self._record_turn(
            objective=objective,
            leader_round=leader_round,
            actor_role=AgentTurnActorRole.LEADER,
            turn_kind=AgentTurnKind.MAILBOX_FOLLOWUP,
            input_summary=f"Consumed {len(messages)} mailbox messages.",
            output_summary=reason,
            metadata={
                "envelope_ids": [
                    message.envelope_id
                    for message in messages
                    if message.envelope_id is not None
                ],
                "mailbox_cursor": mailbox_cursor,
            },
        )
        if turn_record is None:
            return
        work_session_id = turn_record.work_session_id
        runtime_generation_id = turn_record.runtime_generation_id
        tool_record = ToolInvocationRecord(
            turn_record_id=turn_record.turn_record_id,
            work_session_id=work_session_id,
            runtime_generation_id=runtime_generation_id,
            tool_name="mailbox.commit",
            tool_kind=ToolInvocationKind.MAILBOX_COMMIT,
            input_summary="leader_mailbox_consume",
            output_summary=f"consumed {len(messages)} messages",
            status="completed",
            started_at=turn_record.created_at,
            completed_at=turn_record.created_at,
            metadata={
                "envelope_ids": [
                    message.envelope_id
                    for message in messages
                    if message.envelope_id is not None
                ],
            },
        )
        await self.runtime.store.append_tool_invocation_record(tool_record)
        snapshot = _mailbox_snapshot(messages)
        artifact = ArtifactRef(
            turn_record_id=turn_record.turn_record_id,
            work_session_id=work_session_id,
            runtime_generation_id=runtime_generation_id,
            artifact_kind=ArtifactRefKind.MAILBOX_SNAPSHOT,
            storage_kind=ArtifactStorageKind.INLINE_JSON,
            uri_or_path=f"mailbox:leader:{leader_round.leader_task.leader_id}:snapshot",
            content_hash=_artifact_hash(snapshot),
            size_bytes=len(json.dumps(snapshot, ensure_ascii=True)),
            metadata={
                "lane_id": leader_round.lane_id,
                "team_id": leader_round.team_id,
            },
        )
        await self.runtime.store.save_artifact_ref(artifact)

    async def _host_owns_teammate_activation_context(
        self,
        *,
        objective: ObjectiveSpec,
        leader_round: LeaderRound,
    ) -> bool:
        session_host = self._session_host()
        if session_host is None:
            return False
        teammate_capacity = _teammate_capacity(leader_round)
        if teammate_capacity <= 0:
            return False
        sessions = await session_host.list_sessions(
            role="teammate",
            objective_id=objective.objective_id,
            lane_id=leader_round.lane_id,
            team_id=leader_round.team_id,
        )
        sessions_by_id = {
            session.session_id: session
            for session in sessions
        }
        for teammate_slot in range(1, teammate_capacity + 1):
            session = sessions_by_id.get(
                _teammate_slot_session_id(leader_round.team_id, teammate_slot)
            )
            if session is None:
                return False
            activation_profile = TeammateActivationProfile.from_metadata(session.metadata)
            if activation_profile.is_empty():
                return False
        return True

    async def ensure_or_step_session(
        self,
        *,
        objective: ObjectiveSpec,
        leader_round: LeaderRound,
        config: LeaderLoopConfig | None = None,
        leader_backend: str | None = None,
        teammate_backend: str | None = None,
        working_dir: str | None = None,
        host_owner_coordinator_id: str | None = None,
        runtime_task_id: str | None = None,
        session_metadata: dict[str, object] | None = None,
    ) -> LeaderLoopResult:
        session_host = self._session_host()
        if session_host is None:
            return await self.run(
                objective=objective,
                leader_round=leader_round,
                config=config,
                leader_backend=leader_backend,
                teammate_backend=teammate_backend,
                working_dir=working_dir,
            )

        active_config = config or LeaderLoopConfig()
        resolved_runtime_task_id = runtime_task_id or leader_round.runtime_task.task_id
        session_id = _leader_resident_session_id(
            objective.objective_id,
            leader_round.lane_id,
        )
        persisted_state = await session_host.load_coordinator_session(session_id)
        base_metadata = dict(persisted_state.metadata) if persisted_state is not None else {}
        step_count = _metadata_int(base_metadata, "host_step_count")
        resolved_session_metadata = _leader_session_metadata(
            {
                **base_metadata,
                **(session_metadata or {}),
                "runtime_step_mode": "host_stepped",
                "host_step_count": step_count + 1,
                "host_step_reason": "Leader lane session stepped by superleader runtime.",
            }
        )
        if persisted_state is None:
            await session_host.load_or_create_coordinator_session(
                session_id=session_id,
                coordinator_id=leader_round.leader_task.leader_id,
                objective_id=objective.objective_id,
                lane_id=leader_round.lane_id,
                team_id=leader_round.team_id,
                role="leader",
                phase=ResidentCoordinatorPhase.BOOTING,
                host_owner_coordinator_id=host_owner_coordinator_id,
                runtime_task_id=resolved_runtime_task_id,
                metadata=resolved_session_metadata,
            )
            persisted_state = await session_host.load_coordinator_session(session_id)
        if persisted_state is None:
            raise ValueError(f"Unable to initialize coordinator session: {session_id}")

        step_session = replace(
            _resident_session_from_coordinator_state(persisted_state),
            phase=ResidentCoordinatorPhase.RUNNING,
            last_reason="Leader lane session stepped by superleader runtime.",
            metadata=resolved_session_metadata,
        )
        await session_host.record_coordinator_session_state(
            session_id,
            coordinator_session=step_session,
            host_owner_coordinator_id=host_owner_coordinator_id,
            runtime_task_id=resolved_runtime_task_id,
            metadata=resolved_session_metadata,
        )

        bounded_kernel = self.resident_kernel
        host_step_max_cycles = active_config.host_step_max_cycles
        if host_step_max_cycles is not None and host_step_max_cycles > 0:
            bounded_kernel = _BoundedResidentKernel(max_cycles=host_step_max_cycles)
        stepped_loop = LeaderLoopSupervisor(
            runtime=self.runtime,
            evaluator=self.evaluator,
            mailbox=self.mailbox,
            permission_broker=self.permission_broker,
            resident_kernel=bounded_kernel,
        )
        result = await stepped_loop.run(
            objective=objective,
            leader_round=leader_round,
            config=active_config,
            leader_backend=leader_backend,
            teammate_backend=teammate_backend,
            working_dir=working_dir,
            coordinator_session=step_session,
        )
        coordinator_session = result.coordinator_session
        if coordinator_session is None:
            return result

        idle_wait_request = None
        natural_idle_phase = coordinator_session.phase
        if active_config.keep_leader_session_idle and coordinator_session.phase in {
            ResidentCoordinatorPhase.IDLE,
            ResidentCoordinatorPhase.WAITING_FOR_MAILBOX,
            ResidentCoordinatorPhase.WAITING_FOR_SUBORDINATES,
        }:
            idle_wait_request = PermissionRequest(
                requester=leader_round.leader_task.leader_id,
                action="resident.idle_wait",
                rationale="Keep the leader resident shell idle-attached between host-owned steps.",
                group_id=objective.group_id,
                objective_id=objective.objective_id,
                team_id=leader_round.team_id,
                lane_id=leader_round.lane_id,
                task_id=resolved_runtime_task_id,
                metadata={
                    "role": "leader",
                    "session_id": session_id,
                    "target_phase": natural_idle_phase.value,
                },
            )
            idle_wait_decision = await self._request_permission(idle_wait_request)
            if not idle_wait_decision.approved and not idle_wait_decision.pending:
                coordinator_session = replace(
                    coordinator_session,
                    phase=ResidentCoordinatorPhase.QUIESCENT,
                    last_reason=(
                        idle_wait_decision.reason
                        or coordinator_session.last_reason
                    ),
                )

        finalized_metadata = dict(coordinator_session.metadata)
        finalized_metadata.update(resolved_session_metadata)
        coordinator_session = replace(
            coordinator_session,
            metadata=finalized_metadata,
        )
        await session_host.record_coordinator_session_state(
            session_id,
            coordinator_session=coordinator_session,
            host_owner_coordinator_id=host_owner_coordinator_id,
            runtime_task_id=resolved_runtime_task_id,
            metadata=finalized_metadata,
        )
        if idle_wait_request is not None:
            await session_host.record_resident_shell_approval(
                approval_kind="idle_wait",
                status=_approval_status_from_permission_decision(idle_wait_decision),
                request=idle_wait_request,
                decision=idle_wait_decision,
                requested_by=leader_round.leader_task.leader_id,
                objective_id=objective.objective_id,
                lane_id=leader_round.lane_id,
                team_id=leader_round.team_id,
                target_session_id=session_id,
                target_mode=natural_idle_phase.value,
            )
        persisted_state = await session_host.load_coordinator_session(session_id)
        if persisted_state is None:
            return result
        return replace(
            result,
            coordinator_session=_resident_session_from_coordinator_state(persisted_state),
        )

    async def _snapshots(
        self,
        *,
        group_id: str,
        lane_id: str,
        team_id: str,
    ) -> tuple[BlackboardSnapshot, BlackboardSnapshot]:
        leader_lane_snapshot = await self.runtime.reduce_blackboard(
            group_id=group_id,
            kind=BlackboardKind.LEADER_LANE,
            lane_id=lane_id,
        )
        team_snapshot = await self.runtime.reduce_blackboard(
            group_id=group_id,
            kind=BlackboardKind.TEAM,
            lane_id=lane_id,
            team_id=team_id,
        )
        return leader_lane_snapshot, team_snapshot

    async def _request_permission(self, request: PermissionRequest) -> PermissionDecision:
        if hasattr(self.permission_broker, "request_decision"):
            return await self.permission_broker.request_decision(request)
        return await self.permission_broker.request(request)  # type: ignore[attr-defined]

    async def _list_mailbox_messages(
        self,
        recipient: str,
        *,
        after_envelope_id: str | None = None,
    ) -> tuple[MailboxEnvelope, ...]:
        if hasattr(self.mailbox, "list_for_recipient"):
            messages = await self.mailbox.list_for_recipient(recipient, after_envelope_id=after_envelope_id)
            return tuple(messages)
        messages = await self.mailbox.poll(recipient)  # type: ignore[attr-defined]
        if after_envelope_id is None:
            return tuple(messages)
        seen = False
        filtered: list[MailboxEnvelope] = []
        for message in messages:
            if seen:
                filtered.append(message)
            elif message.envelope_id == after_envelope_id:
                seen = True
        return tuple(filtered)

    async def _acknowledge_leader_messages(
        self,
        leader_id: str,
        messages: tuple[MailboxEnvelope, ...],
    ) -> str | None:
        if not messages:
            return None
        envelope_ids = tuple(
            message.envelope_id
            for message in messages
            if message.envelope_id is not None
        )
        if not envelope_ids:
            return None
        cursor_payload: dict[str, str | None] = {
            "stream": "mailbox",
            "event_id": envelope_ids[-1],
            "last_envelope_id": envelope_ids[-1],
        }
        await self.runtime.store.save_protocol_bus_cursor(
            stream="mailbox",
            consumer=leader_id,
            cursor=cursor_payload,
        )
        if hasattr(self.mailbox, "acknowledge"):
            cursor = await self.mailbox.acknowledge(leader_id, envelope_ids)
            bridge_last_envelope_id = _mailbox_cursor_last_envelope_id(
                {"last_envelope_id": cursor.last_envelope_id}
            )
            if bridge_last_envelope_id is not None and bridge_last_envelope_id != envelope_ids[-1]:
                await self.runtime.store.save_protocol_bus_cursor(
                    stream="mailbox",
                    consumer=leader_id,
                    cursor={
                        **cursor_payload,
                        "last_envelope_id": bridge_last_envelope_id,
                        "event_id": bridge_last_envelope_id,
                    },
                )
            last_envelope_id = cursor.last_envelope_id
        else:
            for envelope_id in envelope_ids:
                await self.mailbox.ack(leader_id, envelope_id)  # type: ignore[attr-defined]
            last_envelope_id = envelope_ids[-1]
        return last_envelope_id

    async def _load_authoritative_leader_mailbox_cursor(
        self,
        *,
        leader_id: str,
        fallback_cursor: object = None,
    ) -> str | None:
        stored_cursor = await self.runtime.store.get_protocol_bus_cursor(
            stream="mailbox",
            consumer=leader_id,
        )
        stored_last_envelope_id = _mailbox_cursor_last_envelope_id(stored_cursor)
        if stored_last_envelope_id is not None:
            return stored_last_envelope_id
        fallback_last_envelope_id = _mailbox_cursor_last_envelope_id(fallback_cursor)
        if fallback_last_envelope_id is not None:
            return fallback_last_envelope_id
        if hasattr(self.mailbox, "get_cursor"):
            cursor = await self.mailbox.get_cursor(leader_id)
            return cursor.last_envelope_id
        return None

    async def _send_mailbox_envelope(self, envelope: MailboxEnvelope) -> MailboxEnvelope:
        return await self.mailbox.send(envelope)

    async def _try_ensure_subscription(
        self,
        subscription: MailboxSubscription,
    ) -> MailboxSubscription | None:
        try:
            return await self.mailbox.ensure_subscription(subscription)
        except NotImplementedError:
            return None

    async def _collect_shared_digests(
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
                    limit=200,
                )
            except (NotImplementedError, ValueError):
                continue
            digests.extend(items)
        return tuple(digests)

    async def _collect_subscription_cursors(
        self,
        subscriptions: tuple[MailboxSubscription, ...],
    ) -> dict[str, str | None]:
        cursors: dict[str, str | None] = {}
        for subscription in subscriptions:
            if not subscription.subscription_id:
                continue
            try:
                cursor = await self.mailbox.get_subscription_cursor(
                    subscription.subscriber,
                    subscription_id=subscription.subscription_id,
                )
            except (NotImplementedError, ValueError):
                continue
            cursors[subscription.subscription_id] = cursor.last_envelope_id
        return cursors

    async def _ensure_default_shared_subscriptions(
        self,
        *,
        objective: ObjectiveSpec,
        leader_round: LeaderRound,
    ) -> tuple[MailboxSubscription, ...]:
        leader_subscription = await self._try_ensure_subscription(
            MailboxSubscription(
                subscriber=leader_round.leader_task.leader_id,
                subscription_id=_leader_lane_shared_subscription_id(
                    objective.objective_id,
                    leader_round.lane_id,
                ),
                group_id=objective.group_id,
                lane_id=leader_round.lane_id,
                team_id=leader_round.team_id,
                visibility_scopes=(MailboxVisibilityScope.SHARED,),
                delivery_mode=MailboxDeliveryMode.SUMMARY_PLUS_REF,
                metadata={"runtime_view": "leader_lane_shared_summary"},
            )
        )
        superleader_subscription = await self._try_ensure_subscription(
            MailboxSubscription(
                subscriber=_superleader_subscriber(objective.objective_id),
                subscription_id=_superleader_lane_shared_subscription_id(
                    objective.objective_id,
                    leader_round.lane_id,
                ),
                group_id=objective.group_id,
                lane_id=leader_round.lane_id,
                team_id=leader_round.team_id,
                visibility_scopes=(MailboxVisibilityScope.SHARED,),
                delivery_mode=MailboxDeliveryMode.SUMMARY_PLUS_REF,
                metadata={"runtime_view": "superleader_lane_shared_summary"},
            )
        )
        subscriptions = tuple(
            subscription
            for subscription in (leader_subscription, superleader_subscription)
            if subscription is not None
        )
        return subscriptions

    async def _publish_hierarchical_reviews_for_lane(
        self,
        *,
        objective: ObjectiveSpec,
        leader_round: LeaderRound,
    ) -> dict[str, list[str]]:
        team_position_review_ids: list[str] = []
        cross_team_leader_review_ids: list[str] = []
        review_items = await self.runtime.list_review_items(objective.objective_id)

        for item in review_items:
            if item.lane_id not in (None, leader_round.lane_id):
                continue
            if item.team_id not in (None, leader_round.team_id):
                continue

            if item.source_task_id:
                task_reviews = await self.runtime.list_task_reviews(item.source_task_id)
                if task_reviews:
                    latest_revision_ids = tuple(
                        slot.latest_revision_id
                        for slot in task_reviews
                        if slot.latest_revision_id
                    )
                    existing_team_reviews = await self.runtime.list_team_position_reviews(
                        item.item_id,
                        team_id=leader_round.team_id,
                    )
                    latest_existing = existing_team_reviews[-1] if existing_team_reviews else None
                    if (
                        latest_revision_ids
                        and (
                            latest_existing is None
                            or latest_existing.based_on_task_review_revision_ids != latest_revision_ids
                        )
                    ):
                        review_digest = build_task_review_digest(item.source_task_id, task_reviews)
                        dominant_stance = "no_reviews"
                        if review_digest.stance_counts:
                            dominant_stance = sorted(
                                review_digest.stance_counts.items(),
                                key=lambda current: (-current[1], current[0]),
                            )[0][0]
                        key_risks: list[str] = []
                        if review_digest.high_risk_agent_ids:
                            key_risks.append("high_risk_flagged")
                        if review_digest.needs_authority_agent_ids:
                            key_risks.append("needs_authority")
                        if review_digest.needs_split_agent_ids:
                            key_risks.append("needs_split")
                        if review_digest.blocked_agent_ids:
                            key_risks.append("blocked_by_dependency")
                        recommended_next_action = {
                            "needs_authority": "request_authority",
                            "needs_split": "split_task",
                            "blocked_by_dependency": "resolve_dependency",
                            "high_risk": "review_risks",
                            "good_fit": "continue_execution",
                        }.get(dominant_stance, "review_team_position")
                        published_review = await self.runtime.publish_team_position_review(
                            item_id=item.item_id,
                            team_id=leader_round.team_id,
                            leader_id=leader_round.leader_task.leader_id,
                            based_on_task_review_revision_ids=latest_revision_ids,
                            team_stance=dominant_stance,
                            summary=(
                                f"Leader synthesis from {review_digest.slot_count} teammate reviews; "
                                f"dominant stance={dominant_stance}."
                            ),
                            key_risks=tuple(key_risks),
                            key_dependencies=(),
                            recommended_next_action=recommended_next_action,
                            confidence=1.0 if review_digest.slot_count == 1 else 0.75,
                            metadata={"source_task_id": item.source_task_id},
                        )
                        team_position_review_ids.append(published_review.position_review_id)

            if item.item_kind != ReviewItemKind.PROJECT_ITEM:
                continue
            team_reviews = await self.runtime.list_team_position_reviews(item.item_id)
            existing_cross_reviews = await self.runtime.list_cross_team_leader_reviews(
                item.item_id,
                reviewer_team_id=leader_round.team_id,
            )
            existing_pairs = {
                (review.target_team_id, review.target_position_review_id)
                for review in existing_cross_reviews
            }
            for target_review in team_reviews:
                if target_review.team_id == leader_round.team_id:
                    continue
                key = (target_review.team_id, target_review.position_review_id)
                if key in existing_pairs:
                    continue
                cross_review = await self.runtime.publish_cross_team_leader_review(
                    item_id=item.item_id,
                    reviewer_team_id=leader_round.team_id,
                    reviewer_leader_id=leader_round.leader_task.leader_id,
                    target_team_id=target_review.team_id,
                    target_position_review_id=target_review.position_review_id,
                    stance="reviewed",
                    agreement_level="summary_first",
                    what_changed_in_my_understanding=(
                        f"Leader summary only: {target_review.summary or target_review.team_stance}"
                    ),
                    challenge_or_support="support",
                    suggested_adjustment=target_review.recommended_next_action,
                    confidence=target_review.confidence,
                    metadata={"source": "leader_lane_hierarchical_review"},
                )
                cross_team_leader_review_ids.append(cross_review.cross_review_id)

        patch: dict[str, list[str]] = {}
        if team_position_review_ids:
            patch["team_position_review_ids"] = team_position_review_ids
        if cross_team_leader_review_ids:
            patch["cross_team_leader_review_ids"] = cross_team_leader_review_ids
        return patch

    async def _ensure_or_step_teammates(
        self,
        *,
        objective: ObjectiveSpec,
        leader_round: LeaderRound,
        assignments: tuple[WorkerAssignment, ...],
        keep_session_idle: bool = False,
        execution_policy: WorkerExecutionPolicy | None = None,
        backend: str | None = None,
        working_dir: str | None = None,
        turn_index: int | None = None,
        role_profile: WorkerRoleProfile | None = None,
    ) -> ResidentTeammateRunResult:
        if assignments:
            surface = TeammateWorkSurface(
                runtime=self.runtime,
                mailbox=self.mailbox,
                objective=objective,
                leader_round=leader_round,
                backend=None,
                working_dir=None,
                turn_index=turn_index,
                role_profile=None,
                session_host=self._session_host(),
            )
            return await surface.ensure_or_step_sessions(
                assignments=assignments,
                request_permission=self._request_permission,
                resident_kernel=self.resident_kernel,
                keep_session_idle=keep_session_idle,
                execution_policy=execution_policy,
            )
        return await self.runtime.run_resident_teammate_host_sweep(
            mailbox=self.mailbox,
            objective=objective,
            leader_round=leader_round,
            request_permission=self._request_permission,
            resident_kernel=self.resident_kernel,
            keep_session_idle=keep_session_idle,
            execution_policy=execution_policy,
            backend=backend,
            working_dir=working_dir,
            turn_index=turn_index,
            role_profile=role_profile,
        )

    async def _run_teammates(
        self,
        *,
        objective: ObjectiveSpec,
        leader_round: LeaderRound,
        assignments: tuple[WorkerAssignment, ...],
        keep_session_idle: bool = False,
        execution_policy: WorkerExecutionPolicy | None = None,
        backend: str | None = None,
        working_dir: str | None = None,
        turn_index: int | None = None,
        role_profile: WorkerRoleProfile | None = None,
    ) -> ResidentTeammateRunResult:
        return await self._ensure_or_step_teammates(
            objective=objective,
            leader_round=leader_round,
            assignments=assignments,
            keep_session_idle=keep_session_idle,
            execution_policy=execution_policy,
            backend=backend,
            working_dir=working_dir,
            turn_index=turn_index,
            role_profile=role_profile,
        )

    async def _save_delivery_state(self, state: DeliveryState) -> DeliveryState:
        await self.runtime.store.save_delivery_state(state)
        return state

    async def run(
        self,
        *,
        objective: ObjectiveSpec,
        leader_round: LeaderRound,
        config: LeaderLoopConfig | None = None,
        leader_backend: str | None = None,
        teammate_backend: str | None = None,
        working_dir: str | None = None,
        coordinator_session: ResidentCoordinatorSession | None = None,
    ) -> LeaderLoopResult:
        active_config = config or LeaderLoopConfig(
            working_dir=working_dir,
        )
        if leader_backend is not None:
            active_config.leader_backend = leader_backend
        if teammate_backend is not None:
            active_config.teammate_backend = teammate_backend
        if working_dir is not None:
            active_config.working_dir = working_dir
        role_profiles = _resolve_role_profiles(active_config.role_profiles)
        leader_role_profile = _resolve_role_profile(
            profile_id=active_config.leader_profile_id,
            role_profiles=role_profiles,
        )
        teammate_role_profile = _resolve_role_profile(
            profile_id=active_config.teammate_profile_id,
            role_profiles=role_profiles,
        )
        resolved_leader_backend = active_config.leader_backend or leader_role_profile.backend
        resolved_teammate_backend = active_config.teammate_backend or teammate_role_profile.backend
        resolved_leader_execution_policy = (
            active_config.leader_execution_policy
            if active_config.leader_execution_policy is not None
            else leader_role_profile.to_execution_policy()
        )
        resolved_teammate_execution_policy = (
            active_config.teammate_execution_policy
            if active_config.teammate_execution_policy is not None
            else teammate_role_profile.to_execution_policy()
        )

        max_turns = active_config.max_leader_turns
        if max_turns is None:
            max_turns = max(leader_round.leader_task.budget.max_iterations, 1)
        max_turns = max(max_turns, 1)
        mailbox_followup_turn_limit = active_config.max_mailbox_followup_turns
        if mailbox_followup_turn_limit is not None:
            mailbox_followup_turn_limit = max(mailbox_followup_turn_limit, 0)
        cwd = str(Path(active_config.working_dir) if active_config.working_dir else Path.cwd())

        delivery_id = _lane_delivery_id(objective.objective_id, leader_round.lane_id)
        existing_state = await self.runtime.store.get_delivery_state(delivery_id)
        turn_index = existing_state.iteration + 1 if existing_state is not None else 1
        mailbox_cursor = await self._load_authoritative_leader_mailbox_cursor(
            leader_id=leader_round.leader_task.leader_id,
            fallback_cursor=existing_state.mailbox_cursor if existing_state is not None else None,
        )
        mailbox_followup_turns_used = _metadata_int(
            existing_state.metadata if existing_state is not None else None,
            "mailbox_followup_turns_used",
        )
        message_subscriptions = await self._ensure_default_shared_subscriptions(
            objective=objective,
            leader_round=leader_round,
        )
        previous_response_id = None

        leader_records: list[WorkerRecord] = []
        teammate_records: list[WorkerRecord] = []
        mailbox_envelopes: list[MailboxEnvelope] = []
        created_task_ids: list[str] = []
        turns: list[LeaderTurnRecord] = []
        current_state = existing_state
        budget_exhausted_summary: str | None = None
        seeded_turn_output_text = _seed_output_text(active_config.seed_leader_turn_output)
        seeded_initial_turn_available = (
            seeded_turn_output_text is not None
            and current_state is None
            and active_config.skip_initial_prompt_turn_when_seeded
        )

        coordinator_session = (
            replace(
                coordinator_session,
                mailbox_cursor=mailbox_cursor,
            )
            if coordinator_session is not None
            else ResidentCoordinatorSession(
                coordinator_id=leader_round.leader_task.leader_id,
                role="leader",
                phase=ResidentCoordinatorPhase.BOOTING,
                objective_id=objective.objective_id,
                lane_id=leader_round.lane_id,
                team_id=leader_round.team_id,
                mailbox_cursor=mailbox_cursor,
            )
        )

        leader_coordinator = LeaderCoordinator(
            allow_promptless_convergence=active_config.allow_promptless_convergence,
            _pending_continuous_convergence=_should_resume_promptless_convergence(existing_state),
        )

        async def _handle_promptless_convergence(
            mailbox_messages: tuple[MailboxEnvelope, ...],
            *,
            has_open_team_tasks: bool,
            seed_assignments: tuple[WorkerAssignment, ...] = (),
        ) -> ResidentCoordinatorCycleResult:
            nonlocal current_state
            nonlocal mailbox_cursor

            promptless_teammate_records: tuple[WorkerRecord, ...] = ()
            promptless_mailbox_envelopes: tuple[MailboxEnvelope, ...] = ()
            promptless_claimed_task_ids: tuple[str, ...] = ()
            promptless_dispatch_count = 0

            if mailbox_messages:
                consumed_cursor = await self._acknowledge_leader_messages(
                    leader_round.leader_task.leader_id,
                    mailbox_messages,
                )
                if consumed_cursor is not None:
                    mailbox_cursor = consumed_cursor
                await self._record_mailbox_followup(
                    objective=objective,
                    leader_round=leader_round,
                    messages=mailbox_messages,
                    mailbox_cursor=mailbox_cursor,
                    reason="Promptless convergence consumed leader mailbox.",
                )

            if has_open_team_tasks:
                teammate_context_is_host_owned = await self._host_owns_teammate_activation_context(
                    objective=objective,
                    leader_round=leader_round,
                )
                teammate_run = await self._ensure_or_step_teammates(
                    objective=objective,
                    leader_round=leader_round,
                    assignments=seed_assignments,
                    keep_session_idle=active_config.keep_teammate_session_idle,
                    execution_policy=resolved_teammate_execution_policy,
                    backend=None if teammate_context_is_host_owned else resolved_teammate_backend,
                    working_dir=None if teammate_context_is_host_owned else cwd,
                    turn_index=None if teammate_context_is_host_owned else turn_index,
                    role_profile=None if teammate_context_is_host_owned else teammate_role_profile,
                )
                promptless_teammate_records = teammate_run.teammate_records
                promptless_mailbox_envelopes = teammate_run.mailbox_envelopes
                promptless_claimed_task_ids = teammate_run.claimed_task_ids
                promptless_dispatch_count = teammate_run.dispatched_assignment_count
                teammate_records.extend(promptless_teammate_records)
                mailbox_envelopes.extend(promptless_mailbox_envelopes)

            if current_state is None:
                return ResidentCoordinatorCycleResult(
                    phase=ResidentCoordinatorPhase.FAILED,
                    stop=True,
                    reason="Promptless convergence requires a baseline delivery state.",
                )

            latest_worker_ids = current_state.latest_worker_ids
            if promptless_teammate_records:
                latest_worker_ids = tuple(
                    record.worker_id for record in promptless_teammate_records
                )
            metadata = dict(current_state.metadata)
            message_runtime = dict(metadata.get("message_runtime", {}))
            message_runtime["pending_shared_digest_count"] = len(promptless_mailbox_envelopes)
            metadata["message_runtime"] = message_runtime
            metadata["pending_mailbox_count"] = len(promptless_mailbox_envelopes)
            state_for_evaluation = replace(
                current_state,
                metadata=metadata,
                mailbox_cursor=mailbox_cursor,
                latest_worker_ids=latest_worker_ids,
            )
            evaluation = await self.evaluator.evaluate_lane(
                runtime=self.runtime,
                objective=objective,
                state=state_for_evaluation,
            )
            if evaluation.status == DeliveryStatus.COMPLETED and promptless_mailbox_envelopes:
                evaluation.status = DeliveryStatus.RUNNING
                evaluation.summary = (
                    "Waiting for promptless convergence to consume fresh teammate mailbox results."
                )
            evaluation_metadata = dict(evaluation.metadata)
            hierarchical_review_metadata = await self._publish_hierarchical_reviews_for_lane(
                objective=objective,
                leader_round=leader_round,
            )
            if hierarchical_review_metadata:
                evaluation_metadata["hierarchical_review"] = hierarchical_review_metadata
            evaluation_metadata["pending_mailbox_count"] = len(promptless_mailbox_envelopes)
            evaluation_metadata["message_runtime"] = message_runtime
            progress_made = bool(
                mailbox_messages
                or promptless_mailbox_envelopes
                or promptless_claimed_task_ids
                or promptless_dispatch_count > 0
            )
            if (
                progress_made
                and not evaluation.pending_task_ids
                and not evaluation.active_task_ids
                and not promptless_mailbox_envelopes
                and int(evaluation.metadata.get("pending_mailbox_count", 0)) <= 0
                and evaluation.status in {DeliveryStatus.RUNNING, DeliveryStatus.WAITING}
            ):
                evaluation.status = DeliveryStatus.COMPLETED
                evaluation.summary = f"Lane {state_for_evaluation.lane_id} has converged."
            pending_continuous_convergence = bool(
                progress_made
                and evaluation.status
                not in {
                    DeliveryStatus.COMPLETED,
                    DeliveryStatus.BLOCKED,
                    DeliveryStatus.WAITING_FOR_AUTHORITY,
                    DeliveryStatus.FAILED,
                }
                and (
                    promptless_mailbox_envelopes
                    or evaluation.pending_task_ids
                    or evaluation.active_task_ids
                )
            )
            updated_metadata = _coordination_metadata(
                metadata=evaluation_metadata,
                max_turns=max_turns,
                mailbox_followup_turns_used=mailbox_followup_turns_used,
                mailbox_followup_turn_limit=mailbox_followup_turn_limit,
                pending_continuous_convergence=pending_continuous_convergence,
            )
            updated_state = await self._save_delivery_state(
                DeliveryState(
                    delivery_id=current_state.delivery_id,
                    objective_id=current_state.objective_id,
                    kind=current_state.kind,
                    status=evaluation.status,
                    lane_id=leader_round.lane_id,
                    team_id=leader_round.team_id,
                    iteration=turn_index,
                    summary=evaluation.summary,
                    pending_task_ids=evaluation.pending_task_ids,
                    active_task_ids=evaluation.active_task_ids,
                    completed_task_ids=evaluation.completed_task_ids,
                    blocked_task_ids=evaluation.blocked_task_ids,
                    latest_worker_ids=(
                        evaluation.latest_worker_ids or current_state.latest_worker_ids
                    ),
                    mailbox_cursor=mailbox_cursor,
                    metadata=updated_metadata,
                )
            )
            current_state = updated_state
            if evaluation.status == DeliveryStatus.COMPLETED:
                leader_coordinator.clear_convergence()
                await self.runtime.update_task_status(
                    task_id=leader_round.runtime_task.task_id,
                    status=TaskStatus.COMPLETED,
                    actor_id=leader_round.leader_task.leader_id,
                )
                return ResidentCoordinatorCycleResult(
                    phase=ResidentCoordinatorPhase.QUIESCENT,
                    stop=True,
                    progress_made=True,
                    claimed_task_delta=len(promptless_claimed_task_ids),
                    subordinate_dispatch_delta=promptless_dispatch_count,
                    mailbox_poll_delta=1,
                    mailbox_cursor=mailbox_cursor,
                    reason=evaluation.summary,
                )
            if evaluation.status in {DeliveryStatus.BLOCKED, DeliveryStatus.WAITING_FOR_AUTHORITY}:
                leader_coordinator.clear_convergence()
                metadata_blockers = ()
                if evaluation.metadata:
                    waiting_metadata = evaluation.metadata.get("waiting_for_authority_task_ids")
                    if waiting_metadata:
                        metadata_blockers = tuple(waiting_metadata)
                blocked_by = metadata_blockers or evaluation.blocked_task_ids
                await self.runtime.update_task_status(
                    task_id=leader_round.runtime_task.task_id,
                    status=TaskStatus.WAITING_FOR_AUTHORITY
                    if evaluation.status == DeliveryStatus.WAITING_FOR_AUTHORITY
                    else TaskStatus.BLOCKED,
                    actor_id=leader_round.leader_task.leader_id,
                    blocked_by=blocked_by,
                )
                return ResidentCoordinatorCycleResult(
                    phase=ResidentCoordinatorPhase.WAITING_FOR_DEPENDENCIES,
                    stop=True,
                    progress_made=True,
                    claimed_task_delta=len(promptless_claimed_task_ids),
                    subordinate_dispatch_delta=promptless_dispatch_count,
                    mailbox_poll_delta=1,
                    mailbox_cursor=mailbox_cursor,
                    reason=evaluation.summary,
                )
            if evaluation.status == DeliveryStatus.WAITING_FOR_AUTHORITY:
                leader_coordinator.clear_convergence()
                return ResidentCoordinatorCycleResult(
                    phase=ResidentCoordinatorPhase.WAITING_FOR_DEPENDENCIES,
                    stop=True,
                    progress_made=True,
                    claimed_task_delta=len(promptless_claimed_task_ids),
                    subordinate_dispatch_delta=promptless_dispatch_count,
                    mailbox_poll_delta=1,
                    mailbox_cursor=mailbox_cursor,
                    reason=evaluation.summary,
                )
            if evaluation.status == DeliveryStatus.FAILED:
                leader_coordinator.clear_convergence()
                await self.runtime.update_task_status(
                    task_id=leader_round.runtime_task.task_id,
                    status=TaskStatus.FAILED,
                    actor_id=leader_round.leader_task.leader_id,
                )
                return ResidentCoordinatorCycleResult(
                    phase=ResidentCoordinatorPhase.FAILED,
                    stop=True,
                    progress_made=True,
                    claimed_task_delta=len(promptless_claimed_task_ids),
                    subordinate_dispatch_delta=promptless_dispatch_count,
                    mailbox_poll_delta=1,
                    mailbox_cursor=mailbox_cursor,
                    reason=evaluation.summary,
                )

            if not progress_made:
                leader_coordinator.clear_convergence()
            else:
                leader_coordinator.register_prompt_turn(
                    produced_mailbox=promptless_mailbox_envelopes,
                    has_open_team_tasks=bool(
                        evaluation.pending_task_ids or evaluation.active_task_ids
                    ),
                )
            active_subordinate_ids = tuple(
                record.worker_id
                for record in promptless_teammate_records
                if record.session is not None
                and record.session.status == WorkerSessionStatus.ACTIVE
            )
            next_phase = ResidentCoordinatorPhase.IDLE
            if _pending_mailbox_count(current_state) > 0:
                next_phase = ResidentCoordinatorPhase.WAITING_FOR_MAILBOX
            elif evaluation.pending_task_ids or evaluation.active_task_ids:
                next_phase = ResidentCoordinatorPhase.WAITING_FOR_SUBORDINATES
            return ResidentCoordinatorCycleResult(
                phase=next_phase,
                stop=False,
                progress_made=progress_made,
                claimed_task_delta=len(promptless_claimed_task_ids),
                subordinate_dispatch_delta=promptless_dispatch_count,
                mailbox_poll_delta=1,
                active_subordinate_ids=active_subordinate_ids,
                mailbox_cursor=mailbox_cursor,
                reason=evaluation.summary,
            )

        async def _publish_authority_control_envelope(
            *,
            recipient: str,
            subject: str,
            visibility_scope: MailboxVisibilityScope,
            task: TaskCard,
            authority_request: ScopeExtensionRequest,
            authority_decision: AuthorityDecision,
            source_entry_id: str,
            source_scope: str,
            summary: str,
            replacement_task_id: str | None = None,
        ) -> MailboxEnvelope:
            payload: dict[str, object] = {
                "task_id": task.task_id,
                "authority_request": authority_request.to_dict(),
                "authority_decision": authority_decision.to_dict(),
            }
            if replacement_task_id is not None:
                payload["replacement_task_id"] = replacement_task_id
            return await self._send_mailbox_envelope(
                MailboxEnvelope(
                    sender=leader_round.leader_task.leader_id,
                    recipient=recipient,
                    subject=subject,
                    mailbox_id=f"{objective.group_id}:leader:{leader_round.lane_id}",
                    kind=MailboxMessageKind.SYSTEM,
                    group_id=objective.group_id,
                    lane_id=leader_round.lane_id,
                    team_id=leader_round.team_id,
                    summary=summary,
                    full_text_ref=f"blackboard:{source_entry_id}",
                    source_entry_id=source_entry_id,
                    source_scope=source_scope,
                    visibility_scope=visibility_scope,
                    delivery_mode=MailboxDeliveryMode.SUMMARY_PLUS_REF,
                    severity="info",
                    tags=(subject.replace(".", "_"), f"authority_decision:{authority_decision.decision}"),
                    payload=payload,
                    metadata={},
                )
            )

        authority_reactor = TeamAuthorityReactor(
            runtime=self.runtime,
            objective_id=objective.objective_id,
            group_id=objective.group_id,
            lane_id=leader_round.lane_id,
            team_id=leader_round.team_id,
            leader_id=leader_round.leader_task.leader_id,
            superleader_recipient=_superleader_subscriber(objective.objective_id),
        )

        async def _apply_authority_reactor_result(
            reactor_result: TeamAuthorityReactorResult,
            *,
            mailbox_messages: tuple[MailboxEnvelope, ...],
            has_open_team_tasks: bool,
        ) -> ResidentCoordinatorCycleResult | None:
            nonlocal mailbox_cursor
            nonlocal current_state
            for directive in reactor_result.control_envelope_directives:
                mailbox_envelopes.append(
                    await _publish_authority_control_envelope(
                        recipient=directive.recipient,
                        subject=directive.subject,
                        visibility_scope=directive.visibility_scope,
                        task=directive.task,
                        authority_request=directive.authority_request,
                        authority_decision=directive.authority_decision,
                        source_entry_id=directive.source_entry_id,
                        source_scope=directive.source_scope,
                        summary=directive.summary,
                        replacement_task_id=directive.replacement_task_id,
                    )
                )
            consumed_messages = reactor_result.consumed_messages
            if not consumed_messages:
                return None
            consumed_cursor = await self._acknowledge_leader_messages(
                leader_round.leader_task.leader_id,
                consumed_messages,
            )
            if consumed_cursor is not None:
                mailbox_cursor = consumed_cursor
            await self._record_mailbox_followup(
                objective=objective,
                leader_round=leader_round,
                messages=consumed_messages,
                mailbox_cursor=mailbox_cursor,
                reason="Authority reactor consumed leader mailbox.",
            )
            remaining_mailbox_messages = tuple(
                message for message in mailbox_messages if message not in consumed_messages
            )
            if current_state is not None:
                refreshed_state = await self.runtime.store.get_delivery_state(
                    current_state.delivery_id
                )
                if refreshed_state is not None:
                    current_state = refreshed_state
            effective_has_open_team_tasks = has_open_team_tasks
            if leader_coordinator.allow_promptless_convergence:
                team_tasks = await self.runtime.store.list_tasks(
                    objective.group_id,
                    team_id=leader_round.team_id,
                    lane_id=leader_round.lane_id,
                    scope=TaskScope.TEAM.value,
                )
                effective_has_open_team_tasks = any(
                    task.status not in {
                        TaskStatus.COMPLETED,
                        TaskStatus.FAILED,
                        TaskStatus.CANCELLED,
                    }
                    for task in team_tasks
                )
            cycle_result = await _handle_promptless_convergence(
                remaining_mailbox_messages,
                has_open_team_tasks=effective_has_open_team_tasks,
                seed_assignments=(),
            )
            cycle_metadata = dict(cycle_result.metadata or {})
            cycle_metadata.update(reactor_result.metadata_patch())
            return replace(cycle_result, metadata=cycle_metadata)

        async def _process_authority_writebacks(
            mailbox_messages: tuple[MailboxEnvelope, ...],
            *,
            has_open_team_tasks: bool,
        ) -> ResidentCoordinatorCycleResult | None:
            reactor_result = await authority_reactor.process_authority_writebacks(mailbox_messages)
            if reactor_result is None:
                return None
            return await _apply_authority_reactor_result(
                reactor_result,
                mailbox_messages=mailbox_messages,
                has_open_team_tasks=has_open_team_tasks,
            )

        async def _process_authority_requests(
            mailbox_messages: tuple[MailboxEnvelope, ...],
            *,
            has_open_team_tasks: bool,
        ) -> ResidentCoordinatorCycleResult | None:
            reactor_result = await authority_reactor.process_authority_requests(mailbox_messages)
            if reactor_result is None:
                return None
            return await _apply_authority_reactor_result(
                reactor_result,
                mailbox_messages=mailbox_messages,
                has_open_team_tasks=has_open_team_tasks,
            )

        async def _run_cycle(
            session: ResidentCoordinatorSession,
        ) -> ResidentCoordinatorCycleResult:
            nonlocal turn_index
            nonlocal mailbox_cursor
            nonlocal mailbox_followup_turns_used
            nonlocal previous_response_id
            nonlocal current_state
            nonlocal budget_exhausted_summary
            nonlocal seeded_initial_turn_available

            mailbox_messages = await self._list_mailbox_messages(
                leader_round.leader_task.leader_id,
                after_envelope_id=mailbox_cursor,
            )
            has_open_team_tasks = True
            if leader_coordinator.allow_promptless_convergence:
                team_tasks = await self.runtime.store.list_tasks(
                    objective.group_id,
                    team_id=leader_round.team_id,
                    lane_id=leader_round.lane_id,
                    scope=TaskScope.TEAM.value,
                )
                has_open_team_tasks = any(
                    task.status not in {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}
                    for task in team_tasks
                )
            authority_cycle = await _process_authority_requests(
                mailbox_messages,
                has_open_team_tasks=has_open_team_tasks,
            )
            if authority_cycle is not None:
                return authority_cycle
            authority_writeback_cycle = await _process_authority_writebacks(
                mailbox_messages,
                has_open_team_tasks=has_open_team_tasks,
            )
            if authority_writeback_cycle is not None:
                return authority_writeback_cycle
            mailbox_requires_prompt_turn = _leader_mailbox_requires_prompt_turn(
                mailbox_messages,
            )
            host_owned_open_team_work = False
            if (
                not mailbox_requires_prompt_turn
                and has_open_team_tasks
                and current_state is not None
            ):
                host_owned_open_team_work = (
                    await self._host_owns_teammate_activation_context(
                        objective=objective,
                        leader_round=leader_round,
                    )
                )
            if not mailbox_requires_prompt_turn and leader_coordinator.should_enter_promptless_convergence(
                mailbox_messages=mailbox_messages,
                has_open_team_tasks=has_open_team_tasks,
                resident_state_available=current_state is not None,
                routine_mailbox_only=bool(mailbox_messages),
                host_owned_open_team_work=host_owned_open_team_work,
            ):
                return await _handle_promptless_convergence(
                    mailbox_messages,
                    has_open_team_tasks=has_open_team_tasks,
                )
            if turn_index > max_turns:
                has_pending_mailbox = bool(mailbox_messages) or _pending_mailbox_count(current_state) > 0
                if not has_pending_mailbox:
                    budget_exhausted_summary = f"Leader loop exhausted its iteration budget ({max_turns})."
                    return ResidentCoordinatorCycleResult(
                        phase=ResidentCoordinatorPhase.FAILED,
                        stop=True,
                        mailbox_poll_delta=1,
                        mailbox_cursor=mailbox_cursor,
                        reason=budget_exhausted_summary,
                    )
                if (
                    mailbox_followup_turn_limit is not None
                    and mailbox_followup_turns_used >= mailbox_followup_turn_limit
                ):
                    budget_exhausted_summary = (
                        "Leader loop exhausted its mailbox follow-up budget "
                        f"({mailbox_followup_turn_limit}) after base turn budget ({max_turns})."
                    )
                    return ResidentCoordinatorCycleResult(
                        phase=ResidentCoordinatorPhase.FAILED,
                        stop=True,
                        mailbox_poll_delta=1,
                        mailbox_cursor=mailbox_cursor,
                        reason=budget_exhausted_summary,
                    )
                mailbox_followup_turns_used += 1

            visible_tasks = tuple(
                await self.runtime.list_visible_tasks(
                    group_id=objective.group_id,
                    viewer_role="leader",
                    lane_id=leader_round.lane_id,
                    team_id=leader_round.team_id,
                )
            )
            leader_lane_snapshot, team_snapshot = await self._snapshots(
                group_id=objective.group_id,
                lane_id=leader_round.lane_id,
                team_id=leader_round.team_id,
            )

            leader_assignment = compile_leader_turn_assignment(
                objective=objective,
                leader_round=leader_round,
                turn_index=turn_index,
                visible_tasks=visible_tasks,
                leader_lane_snapshot=leader_lane_snapshot,
                team_snapshot=team_snapshot,
                mailbox_messages=mailbox_messages,
                backend=resolved_leader_backend,
                working_dir=cwd,
                previous_response_id=previous_response_id,
                role_profile=leader_role_profile,
            )
            use_seeded_turn_output = (
                seeded_initial_turn_available
                and turn_index == 1
                and current_state is None
                and seeded_turn_output_text is not None
            )
            if use_seeded_turn_output:
                seeded_initial_turn_available = False
                await self.runtime.update_task_status(
                    task_id=leader_round.runtime_task.task_id,
                    status=TaskStatus.IN_PROGRESS,
                    actor_id=leader_round.leader_task.leader_id,
                )
                now = datetime.now(timezone.utc).isoformat()
                leader_record = WorkerRecord(
                    worker_id=leader_assignment.worker_id,
                    assignment_id=leader_assignment.assignment_id,
                    backend="planning_seed",
                    role=leader_assignment.role,
                    status=WorkerStatus.COMPLETED,
                    started_at=now,
                    ended_at=now,
                    output_text=seeded_turn_output_text,
                    response_id=f"seed:{leader_assignment.assignment_id}",
                    metadata={
                        "seeded_from_revised_plan": True,
                        "planning_round_id": leader_assignment.metadata.get("planning_round_id"),
                    },
                )
                leader_records.append(leader_record)
                previous_response_id = leader_record.response_id
            else:
                decision = await self._request_permission(
                    PermissionRequest(
                        requester=leader_assignment.worker_id,
                        action="execute_worker_assignment",
                        rationale=f"Execute leader turn {turn_index} for lane {leader_round.lane_id}.",
                        group_id=leader_assignment.group_id,
                        objective_id=leader_assignment.objective_id,
                        team_id=leader_assignment.team_id,
                        lane_id=leader_assignment.lane_id,
                        task_id=leader_assignment.task_id,
                        metadata={"role": leader_assignment.role},
                    )
                )
                if not decision.approved:
                    current_state = await self._save_delivery_state(
                        DeliveryState(
                            delivery_id=delivery_id,
                            objective_id=objective.objective_id,
                            kind=DeliveryStateKind.LANE,
                            status=DeliveryStatus.BLOCKED,
                            lane_id=leader_round.lane_id,
                            team_id=leader_round.team_id,
                            iteration=turn_index,
                            summary=f"Permission denied for leader turn {turn_index}.",
                            latest_worker_ids=(leader_round.leader_task.leader_id,),
                            mailbox_cursor=mailbox_cursor,
                            metadata=_coordination_metadata(
                                metadata={"reviewer": decision.reviewer, "reason": decision.reason},
                                max_turns=max_turns,
                                mailbox_followup_turns_used=mailbox_followup_turns_used,
                                mailbox_followup_turn_limit=mailbox_followup_turn_limit,
                            ),
                        )
                    )
                    await self.runtime.update_task_status(
                        task_id=leader_round.runtime_task.task_id,
                        status=TaskStatus.BLOCKED,
                        actor_id=leader_round.leader_task.leader_id,
                        blocked_by=("permission.denied",),
                    )
                    return ResidentCoordinatorCycleResult(
                        phase=ResidentCoordinatorPhase.WAITING_FOR_DEPENDENCIES,
                        stop=True,
                        mailbox_poll_delta=1,
                        mailbox_cursor=mailbox_cursor,
                        reason=current_state.summary,
                    )

                await self.runtime.update_task_status(
                    task_id=leader_round.runtime_task.task_id,
                    status=TaskStatus.IN_PROGRESS,
                    actor_id=leader_round.leader_task.leader_id,
                )
                leader_record = await self.runtime.run_worker_assignment(
                    leader_assignment,
                    policy=_lifecycle_policy(
                        keep_idle=active_config.keep_leader_session_idle,
                        base_policy=resolved_leader_execution_policy,
                    ),
                )
                leader_records.append(leader_record)
                previous_response_id = leader_record.response_id
            consumed_mailbox_ids = tuple(
                message.envelope_id
                for message in mailbox_messages
                if message.envelope_id is not None
            )
            consumed_cursor = await self._acknowledge_leader_messages(
                leader_round.leader_task.leader_id,
                mailbox_messages,
            )
            if consumed_cursor is not None:
                mailbox_cursor = consumed_cursor
            await self._record_mailbox_followup(
                objective=objective,
                leader_round=leader_round,
                messages=mailbox_messages,
                mailbox_cursor=mailbox_cursor,
                reason="Leader turn consumed mailbox messages.",
            )

            if leader_record.status != WorkerStatus.COMPLETED:
                await self._record_turn(
                    objective=objective,
                    leader_round=leader_round,
                    actor_role=AgentTurnActorRole.LEADER,
                    turn_kind=AgentTurnKind.LEADER_DECISION,
                    input_summary=leader_assignment.input_text,
                    output_summary=leader_record.error_text or leader_record.output_text,
                    assignment_id=leader_assignment.assignment_id,
                    response_id=leader_record.response_id,
                    status=AgentTurnStatus.FAILED,
                    metadata={
                        "turn_index": turn_index,
                        "mailbox_count": len(mailbox_messages),
                    },
                )
                current_state = await self._save_delivery_state(
                    DeliveryState(
                        delivery_id=delivery_id,
                        objective_id=objective.objective_id,
                        kind=DeliveryStateKind.LANE,
                        status=DeliveryStatus.FAILED,
                        lane_id=leader_round.lane_id,
                        team_id=leader_round.team_id,
                        iteration=turn_index,
                        summary=f"Leader turn {turn_index} failed.",
                        latest_worker_ids=(leader_record.worker_id,),
                        mailbox_cursor=mailbox_cursor,
                        metadata=_coordination_metadata(
                            metadata=None,
                            max_turns=max_turns,
                            mailbox_followup_turns_used=mailbox_followup_turns_used,
                            mailbox_followup_turn_limit=mailbox_followup_turn_limit,
                        ),
                    )
                )
                await self.runtime.update_task_status(
                    task_id=leader_round.runtime_task.task_id,
                    status=TaskStatus.FAILED,
                    actor_id=leader_round.leader_task.leader_id,
                )
                turns.append(
                    LeaderTurnRecord(
                        turn_index=turn_index,
                        leader_assignment=leader_assignment,
                        leader_record=leader_record,
                        consumed_mailbox_ids=consumed_mailbox_ids,
                    )
                )
                return ResidentCoordinatorCycleResult(
                    phase=ResidentCoordinatorPhase.FAILED,
                    stop=True,
                    prompt_turn_delta=1,
                    mailbox_poll_delta=1,
                    mailbox_cursor=mailbox_cursor,
                    reason=current_state.summary,
                )

            ingested_turn = await ingest_leader_turn_output(
                self.runtime,
                objective,
                leader_round,
                leader_record.output_text,
                backend=resolved_teammate_backend,
                working_dir=cwd,
                role_profile=teammate_role_profile,
            )
            created_task_ids.extend(task.task_id for task in ingested_turn.created_tasks)

            produced_envelopes: tuple[MailboxEnvelope, ...] = ()
            turn_teammate_records: tuple[WorkerRecord, ...] = ()
            dispatch_assignments: tuple[WorkerAssignment, ...] = ()
            turn_claimed_task_ids: tuple[str, ...] = ()
            turn_claim_session_ids: tuple[str, ...] = ()
            turn_directed_claimed_task_ids: tuple[str, ...] = ()
            turn_autonomous_claimed_task_ids: tuple[str, ...] = ()
            turn_processed_teammate_mailbox_ids: tuple[str, ...] = ()
            turn_teammate_execution_evidence = False
            turn_dispatch_count = 0
            if active_config.auto_run_teammates:
                dispatch_assignments = ingested_turn.teammate_assignments
                teammate_run = await self._ensure_or_step_teammates(
                    objective=objective,
                    leader_round=leader_round,
                    assignments=dispatch_assignments,
                    keep_session_idle=active_config.keep_teammate_session_idle,
                    execution_policy=resolved_teammate_execution_policy,
                    backend=resolved_teammate_backend,
                    working_dir=cwd,
                    turn_index=turn_index,
                    role_profile=teammate_role_profile,
                )
                turn_teammate_records = teammate_run.teammate_records
                produced_envelopes = teammate_run.mailbox_envelopes
                turn_claimed_task_ids = teammate_run.claimed_task_ids
                turn_claim_session_ids = teammate_run.claim_session_ids
                turn_directed_claimed_task_ids = teammate_run.directed_claimed_task_ids
                turn_autonomous_claimed_task_ids = teammate_run.autonomous_claimed_task_ids
                turn_processed_teammate_mailbox_ids = teammate_run.processed_mailbox_envelope_ids
                turn_teammate_execution_evidence = teammate_run.teammate_execution_evidence
                turn_dispatch_count = teammate_run.dispatched_assignment_count
                teammate_records.extend(turn_teammate_records)
                mailbox_envelopes.extend(produced_envelopes)

            team_scope_tasks = await self.runtime.store.list_tasks(
                objective.group_id,
                team_id=leader_round.team_id,
                lane_id=leader_round.lane_id,
                scope=TaskScope.TEAM.value,
            )
            if not ingested_turn.created_tasks and (
                not team_scope_tasks
                or all(task.status == TaskStatus.COMPLETED for task in team_scope_tasks)
            ):
                await self.runtime.update_task_status(
                    task_id=leader_round.runtime_task.task_id,
                    status=TaskStatus.COMPLETED,
                    actor_id=leader_round.leader_task.leader_id,
                )

            evaluation = await self.evaluator.evaluate_lane(
                runtime=self.runtime,
                objective=objective,
                state=DeliveryState(
                    delivery_id=delivery_id,
                    objective_id=objective.objective_id,
                    kind=DeliveryStateKind.LANE,
                    status=DeliveryStatus.RUNNING,
                    lane_id=leader_round.lane_id,
                    team_id=leader_round.team_id,
                    iteration=turn_index,
                    summary=ingested_turn.parsed_output.summary,
                    latest_worker_ids=tuple(
                        [leader_record.worker_id] + [record.worker_id for record in turn_teammate_records]
                    ),
                    mailbox_cursor=mailbox_cursor,
                    metadata={
                        "pending_mailbox_count": len(produced_envelopes),
                        "message_runtime": {
                            "shared_subscription_ids": [
                                subscription.subscription_id
                                for subscription in message_subscriptions
                                if subscription.subscription_id
                            ],
                            "pending_shared_digest_count": len(produced_envelopes),
                        },
                        "teammate_runtime": {
                            "teammate_execution_evidence": turn_teammate_execution_evidence,
                            "directed_claimed_task_count": len(turn_directed_claimed_task_ids),
                            "autonomous_claimed_task_count": len(turn_autonomous_claimed_task_ids),
                            "processed_mailbox_envelope_count": len(turn_processed_teammate_mailbox_ids),
                        },
                    },
                ),
            )
            if (
                evaluation.status == DeliveryStatus.COMPLETED
                and produced_envelopes
                and turn_index < max_turns
            ):
                evaluation.status = DeliveryStatus.RUNNING
                evaluation.summary = "Waiting for the leader to consume fresh teammate mailbox results."
            hierarchical_review_metadata = await self._publish_hierarchical_reviews_for_lane(
                objective=objective,
                leader_round=leader_round,
            )
            if hierarchical_review_metadata:
                evaluation.metadata = dict(evaluation.metadata)
                evaluation.metadata["hierarchical_review"] = hierarchical_review_metadata
            leader_coordinator.register_prompt_turn(
                produced_mailbox=produced_envelopes,
                has_open_team_tasks=bool(evaluation.pending_task_ids or evaluation.active_task_ids),
            )
            current_state = await self._save_delivery_state(
                DeliveryState(
                    delivery_id=delivery_id,
                    objective_id=objective.objective_id,
                    kind=DeliveryStateKind.LANE,
                    status=evaluation.status,
                    lane_id=leader_round.lane_id,
                    team_id=leader_round.team_id,
                    iteration=turn_index,
                    summary=evaluation.summary,
                    pending_task_ids=evaluation.pending_task_ids,
                    active_task_ids=evaluation.active_task_ids,
                    completed_task_ids=evaluation.completed_task_ids,
                    blocked_task_ids=evaluation.blocked_task_ids,
                    latest_worker_ids=evaluation.latest_worker_ids
                    or tuple([leader_record.worker_id] + [record.worker_id for record in turn_teammate_records]),
                    mailbox_cursor=mailbox_cursor,
                    metadata=_coordination_metadata(
                        metadata=dict(evaluation.metadata),
                        max_turns=max_turns,
                        mailbox_followup_turns_used=mailbox_followup_turns_used,
                        mailbox_followup_turn_limit=mailbox_followup_turn_limit,
                        pending_continuous_convergence=(
                            leader_coordinator._pending_continuous_convergence
                        ),
                    ),
                )
            )
            await self._record_turn(
                objective=objective,
                leader_round=leader_round,
                actor_role=AgentTurnActorRole.LEADER,
                turn_kind=AgentTurnKind.LEADER_DECISION,
                input_summary=leader_assignment.input_text,
                output_summary=ingested_turn.parsed_output.summary,
                assignment_id=leader_assignment.assignment_id,
                response_id=leader_record.response_id,
                metadata={
                    "turn_index": turn_index,
                    "created_task_ids": [task.task_id for task in ingested_turn.created_tasks],
                    "claimed_task_ids": list(turn_claimed_task_ids),
                    "claim_session_ids": list(turn_claim_session_ids),
                    "directed_claimed_task_ids": list(turn_directed_claimed_task_ids),
                    "autonomous_claimed_task_ids": list(turn_autonomous_claimed_task_ids),
                    "processed_teammate_mailbox_ids": list(turn_processed_teammate_mailbox_ids),
                    "teammate_execution_evidence": turn_teammate_execution_evidence,
                    "pending_mailbox_count": len(produced_envelopes),
                    "mailbox_followup_turns_used": mailbox_followup_turns_used,
                },
            )
            turns.append(
                LeaderTurnRecord(
                    turn_index=turn_index,
                    leader_assignment=leader_assignment,
                    leader_record=leader_record,
                    teammate_records=turn_teammate_records,
                    created_task_ids=tuple(task.task_id for task in ingested_turn.created_tasks),
                    claimed_task_ids=turn_claimed_task_ids,
                    claim_session_ids=turn_claim_session_ids,
                    consumed_mailbox_ids=consumed_mailbox_ids,
                    produced_mailbox_ids=tuple(
                        envelope.envelope_id for envelope in produced_envelopes if envelope.envelope_id is not None
                    ),
                    directed_claimed_task_ids=turn_directed_claimed_task_ids,
                    autonomous_claimed_task_ids=turn_autonomous_claimed_task_ids,
                    processed_teammate_mailbox_ids=turn_processed_teammate_mailbox_ids,
                    teammate_execution_evidence=turn_teammate_execution_evidence,
                )
            )

            if evaluation.status == DeliveryStatus.COMPLETED:
                await self.runtime.update_task_status(
                    task_id=leader_round.runtime_task.task_id,
                    status=TaskStatus.COMPLETED,
                    actor_id=leader_round.leader_task.leader_id,
                )
                return ResidentCoordinatorCycleResult(
                    phase=ResidentCoordinatorPhase.QUIESCENT,
                    stop=True,
                    progress_made=True,
                    prompt_turn_delta=1,
                    claimed_task_delta=len(turn_claimed_task_ids),
                    subordinate_dispatch_delta=turn_dispatch_count,
                    mailbox_poll_delta=1,
                    mailbox_cursor=mailbox_cursor,
                    reason=evaluation.summary,
                )
            if evaluation.status == DeliveryStatus.BLOCKED:
                await self.runtime.update_task_status(
                    task_id=leader_round.runtime_task.task_id,
                    status=TaskStatus.BLOCKED,
                    actor_id=leader_round.leader_task.leader_id,
                    blocked_by=evaluation.blocked_task_ids,
                )
                return ResidentCoordinatorCycleResult(
                    phase=ResidentCoordinatorPhase.WAITING_FOR_DEPENDENCIES,
                    stop=True,
                    progress_made=True,
                    prompt_turn_delta=1,
                    claimed_task_delta=len(turn_claimed_task_ids),
                    subordinate_dispatch_delta=turn_dispatch_count,
                    mailbox_poll_delta=1,
                    mailbox_cursor=mailbox_cursor,
                    reason=evaluation.summary,
                )
            if evaluation.status == DeliveryStatus.WAITING_FOR_AUTHORITY:
                await self.runtime.update_task_status(
                    task_id=leader_round.runtime_task.task_id,
                    status=TaskStatus.WAITING_FOR_AUTHORITY,
                    actor_id=leader_round.leader_task.leader_id,
                )
                return ResidentCoordinatorCycleResult(
                    phase=ResidentCoordinatorPhase.WAITING_FOR_DEPENDENCIES,
                    stop=True,
                    progress_made=True,
                    prompt_turn_delta=1,
                    claimed_task_delta=len(turn_claimed_task_ids),
                    subordinate_dispatch_delta=turn_dispatch_count,
                    mailbox_poll_delta=1,
                    mailbox_cursor=mailbox_cursor,
                    reason=evaluation.summary,
                )
            if evaluation.status == DeliveryStatus.FAILED:
                await self.runtime.update_task_status(
                    task_id=leader_round.runtime_task.task_id,
                    status=TaskStatus.FAILED,
                    actor_id=leader_round.leader_task.leader_id,
                )
                return ResidentCoordinatorCycleResult(
                    phase=ResidentCoordinatorPhase.FAILED,
                    stop=True,
                    progress_made=True,
                    prompt_turn_delta=1,
                    claimed_task_delta=len(turn_claimed_task_ids),
                    subordinate_dispatch_delta=turn_dispatch_count,
                    mailbox_poll_delta=1,
                    mailbox_cursor=mailbox_cursor,
                    reason=evaluation.summary,
                )
            active_subordinate_ids = tuple(
                record.worker_id
                for record in turn_teammate_records
                if record.session is not None and record.session.status == WorkerSessionStatus.ACTIVE
            )
            next_phase = ResidentCoordinatorPhase.IDLE
            if produced_envelopes:
                next_phase = ResidentCoordinatorPhase.WAITING_FOR_MAILBOX
            elif evaluation.pending_task_ids or evaluation.active_task_ids:
                next_phase = ResidentCoordinatorPhase.WAITING_FOR_SUBORDINATES
            turn_index += 1
            return ResidentCoordinatorCycleResult(
                phase=next_phase,
                progress_made=True,
                prompt_turn_delta=1,
                claimed_task_delta=len(turn_claimed_task_ids),
                subordinate_dispatch_delta=turn_dispatch_count,
                mailbox_poll_delta=1,
                active_subordinate_ids=active_subordinate_ids,
                mailbox_cursor=mailbox_cursor,
                reason=evaluation.summary,
            )

        async def _finalize(
            final_session: ResidentCoordinatorSession,
        ) -> LeaderLoopResult:
            nonlocal current_state
            nonlocal mailbox_cursor
            nonlocal mailbox_followup_turns_used

            if current_state is None:
                current_state = await self._save_delivery_state(
                    DeliveryState(
                        delivery_id=delivery_id,
                        objective_id=objective.objective_id,
                        kind=DeliveryStateKind.LANE,
                        status=DeliveryStatus.FAILED,
                        lane_id=leader_round.lane_id,
                        team_id=leader_round.team_id,
                        iteration=max_turns,
                        summary="Leader loop exited without producing a delivery state.",
                        mailbox_cursor=mailbox_cursor,
                        metadata=_coordination_metadata(
                            metadata=None,
                            max_turns=max_turns,
                            mailbox_followup_turns_used=mailbox_followup_turns_used,
                            mailbox_followup_turn_limit=mailbox_followup_turn_limit,
                        ),
                    )
                )

            if (
                current_state.status
                not in {
                    DeliveryStatus.COMPLETED,
                    DeliveryStatus.BLOCKED,
                    DeliveryStatus.FAILED,
                    DeliveryStatus.WAITING_FOR_AUTHORITY,
                }
                and budget_exhausted_summary is not None
            ):
                current_state = await self._save_delivery_state(
                    DeliveryState(
                        delivery_id=delivery_id,
                        objective_id=objective.objective_id,
                        kind=DeliveryStateKind.LANE,
                        status=DeliveryStatus.FAILED,
                        lane_id=leader_round.lane_id,
                        team_id=leader_round.team_id,
                        iteration=max(turn_index - 1, max_turns),
                        summary=budget_exhausted_summary,
                        pending_task_ids=current_state.pending_task_ids,
                        active_task_ids=current_state.active_task_ids,
                        completed_task_ids=current_state.completed_task_ids,
                        blocked_task_ids=current_state.blocked_task_ids,
                        latest_worker_ids=current_state.latest_worker_ids,
                        mailbox_cursor=current_state.mailbox_cursor,
                        metadata=_coordination_metadata(
                            metadata=dict(current_state.metadata),
                            max_turns=max_turns,
                            mailbox_followup_turns_used=mailbox_followup_turns_used,
                            mailbox_followup_turn_limit=mailbox_followup_turn_limit,
                        ),
                    )
                )
                await self.runtime.update_task_status(
                    task_id=leader_round.runtime_task.task_id,
                    status=TaskStatus.FAILED,
                    actor_id=leader_round.leader_task.leader_id,
                )

            message_digests = await self._collect_shared_digests(message_subscriptions)
            message_runtime_metadata: dict[str, object] = {
                "shared_subscription_ids": [
                    subscription.subscription_id
                    for subscription in message_subscriptions
                    if subscription.subscription_id
                ],
                "pending_shared_digest_count": len(message_digests),
            }
            subscription_cursors = await self._collect_subscription_cursors(message_subscriptions)
            if subscription_cursors:
                message_runtime_metadata["shared_subscription_cursors"] = subscription_cursors
            if message_digests:
                message_runtime_metadata["shared_digest_envelope_ids"] = [
                    digest.envelope_id for digest in message_digests
                ]
            current_metadata = dict(current_state.metadata)
            current_metadata["message_runtime"] = message_runtime_metadata
            current_state = await self._save_delivery_state(
                replace(
                    current_state,
                    metadata=current_metadata,
                )
            )
            final_metadata = dict(final_session.metadata)
            final_metadata["delivery_status"] = current_state.status.value
            final_session = replace(
                final_session,
                phase=(
                    ResidentCoordinatorPhase.FAILED
                    if current_state.status == DeliveryStatus.FAILED
                    else final_session.phase
                ),
                mailbox_cursor=current_state.mailbox_cursor,
                metadata=final_metadata,
            )
            return LeaderLoopResult(
                leader_round=leader_round,
                delivery_state=current_state,
                leader_records=tuple(leader_records),
                teammate_records=tuple(teammate_records),
                mailbox_cursor=current_state.mailbox_cursor,
                mailbox_envelopes=tuple(mailbox_envelopes),
                message_subscriptions=message_subscriptions,
                message_digests=message_digests,
                created_task_ids=tuple(created_task_ids),
                turns=tuple(turns),
                coordinator_session=final_session,
            )

        return await self.resident_kernel.run(
            session=coordinator_session,
            step=_run_cycle,
            finalize=_finalize,
        )


LeaderLoopRunner = LeaderLoopSupervisor
LeaderTurnExecution = LeaderTurnRecord
