from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any

from agent_orchestra.contracts.agent import TeammateActivationProfile
from agent_orchestra.contracts.blackboard import BlackboardEntry, BlackboardEntryKind, BlackboardKind
from agent_orchestra.contracts.enums import TaskScope, TaskStatus, WorkerStatus
from agent_orchestra.contracts.execution import (
    ResidentCoordinatorPhase,
    ResidentCoordinatorSession,
    WorkerAssignment,
    WorkerExecutionContract,
    WorkerExecutionPolicy,
    WorkerRecord,
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
from agent_orchestra.contracts.task_review import TaskClaimContext
from agent_orchestra.contracts.worker_protocol import WorkerRoleProfile
from agent_orchestra.runtime.bootstrap_round import LeaderRound
from agent_orchestra.runtime.directed_mailbox_protocol import (
    DirectedTaskResult,
    parse_directed_task_directive,
)
from agent_orchestra.runtime.group_runtime import (
    AuthorityRequestCommit,
    DirectedTaskReceiptCommit,
    GroupRuntime,
)
from agent_orchestra.runtime.resident_kernel import ResidentCoordinatorCycleResult, ResidentCoordinatorKernel
from agent_orchestra.runtime.session_memory import SessionMemoryService
from agent_orchestra.runtime.session_host import ResidentSessionHost
from agent_orchestra.runtime.teammate_runtime import (
    ResidentTeammateAcquireResult,
    ResidentTeammateRunResult,
)
from agent_orchestra.runtime.teammate_online_loop import TeammateOnlineLoop
from agent_orchestra.tools.mailbox import (
    MailboxBridge,
    MailboxDeliveryMode,
    MailboxEnvelope,
    MailboxMessageKind,
    MailboxVisibilityScope,
)
from agent_orchestra.tools.permission_protocol import PermissionDecision, PermissionRequest


RequestPermission = Callable[[PermissionRequest], Awaitable[PermissionDecision]]


def approval_status_from_permission_decision(decision: PermissionDecision) -> str:
    if decision.approved:
        return "approved"
    if decision.pending:
        return "pending"
    return "denied"


def ordered_unique_strings(values: list[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        if not value or value in seen:
            continue
        seen.add(value)
        ordered.append(value)
    return tuple(ordered)


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


def teammate_slot_from_worker_id(worker_id: str) -> int | None:
    marker = ":teammate:"
    if marker not in worker_id:
        return None
    suffix = worker_id.rsplit(marker, 1)[1].strip()
    try:
        slot = int(suffix)
    except ValueError:
        return None
    return slot if slot > 0 else None


def resident_claim_session_id(
    *,
    objective: ObjectiveSpec,
    leader_round: LeaderRound,
    teammate_slot: int,
) -> str:
    return (
        f"{objective.objective_id}:lane:{leader_round.lane_id}:"
        f"team:{leader_round.team_id}:slot:{teammate_slot}:resident"
    )


def resident_teammate_session_id(
    *,
    leader_round: LeaderRound,
    teammate_slot: int,
) -> str:
    return f"{leader_round.team_id}:teammate:{teammate_slot}:resident"


def resident_teammate_session_id_for_recipient(recipient: str) -> str | None:
    marker = ":teammate:"
    if marker not in recipient:
        return None
    return f"{recipient}:resident"


def mailbox_cursor_last_envelope_id(cursor: object) -> str | None:
    if cursor is None:
        return None
    if isinstance(cursor, str):
        return cursor or None
    if isinstance(cursor, dict):
        value = cursor.get("last_envelope_id")
        if isinstance(value, str) and value:
            return value
    return None


def _is_directive_envelope(envelope: MailboxEnvelope) -> bool:
    payload = envelope.payload if isinstance(envelope.payload, dict) else {}
    protocol_payload = payload.get("protocol")
    if isinstance(protocol_payload, dict):
        message_type = str(protocol_payload.get("message_type", "")).strip()
        if message_type:
            return message_type == "task.directive"
    return envelope.subject in {"task.directive", "task.directed"}


def _is_authority_control_envelope(envelope: MailboxEnvelope) -> bool:
    return envelope.subject in {"authority.decision", "authority.escalated", "authority.writeback"}


def _assignment_execution_contract(
    *,
    task: TaskCard,
    role_profile: WorkerRoleProfile | None,
) -> WorkerExecutionContract | None:
    if role_profile is None:
        return None
    contract = replace(role_profile.execution_contract)
    verification_commands = tuple(task.verification_commands)
    if contract.mode == "teammate_code_edit":
        contract = replace(
            contract,
            require_verification_results=bool(verification_commands),
            required_verification_commands=verification_commands,
            completion_requires_verification_success=bool(verification_commands),
        )
    return contract


def _activation_profile_is_runnable(
    profile: TeammateActivationProfile | None,
) -> bool:
    return (
        profile is not None
        and not profile.is_empty()
        and profile.backend is not None
        and profile.working_dir is not None
    )


def build_pending_teammate_assignment(
    *,
    objective: ObjectiveSpec,
    leader_round: LeaderRound,
    task: TaskCard,
    claim_context: TaskClaimContext | None,
    teammate_slot: int,
    turn_index: int | None,
    backend: str,
    working_dir: str,
    role_profile: WorkerRoleProfile | None = None,
) -> WorkerAssignment:
    reason = task.reason or "Pending team task claimed from the shared task list."
    execution_contract = _assignment_execution_contract(task=task, role_profile=role_profile)
    assignment_id = (
        f"{task.task_id}:teammate-claim-turn-{turn_index}"
        if turn_index is not None
        else f"{task.task_id}:teammate-host-claim"
    )
    instructions = [
        f"You are teammate slot {teammate_slot} in team `{leader_round.team_name}` ({leader_round.team_id}).",
        f"Claimed pending team task: {task.task_id}",
        f"Task goal: {task.goal}",
        f"Task reason: {reason}",
    ]
    review_digest_payload: dict[str, object] | None = None
    review_slots_payload: list[dict[str, object]] = []
    if claim_context is not None and claim_context.review_digest is not None:
        review_digest_payload = claim_context.review_digest.to_dict()
        if claim_context.review_digest.summary_lines:
            instructions.append("Task review digest:")
            for line in claim_context.review_digest.summary_lines:
                instructions.append(f"- {line}")
        if claim_context.review_slots:
            instructions.append("Latest task reviews:")
            for slot in claim_context.review_slots:
                touched = slot.experience_context.touched_paths[:3]
                touched_text = f" touched={', '.join(touched)}" if touched else ""
                instructions.append(
                    f"- {slot.reviewer_agent_id} [{slot.stance.value}] {slot.summary}{touched_text}"
                )
            review_slots_payload = [slot.to_dict() for slot in claim_context.review_slots]
    if task.derived_from:
        instructions.append(f"Derived from: {task.derived_from}")
    return WorkerAssignment(
        assignment_id=assignment_id,
        worker_id=f"{leader_round.team_id}:teammate:{teammate_slot}",
        group_id=objective.group_id,
        objective_id=objective.objective_id,
        team_id=leader_round.team_id,
        lane_id=leader_round.lane_id,
        task_id=task.task_id,
        role="teammate",
        backend=backend,
        instructions="\n".join(instructions),
        input_text=f"Execute claimed team task: {task.goal}",
        working_dir=working_dir,
        metadata={
            "reason": reason,
            "owned_paths": list(task.owned_paths),
            "verification_commands": list(task.verification_commands),
            "derived_from": task.derived_from,
            "assignment_source": "team_task_queue",
            "claimed_from_task_list": True,
            "claim_source": task.claim_source,
            "claim_session_id": task.claim_session_id,
            **(
                {"task_review_digest": review_digest_payload}
                if review_digest_payload is not None
                else {}
            ),
            **(
                {"task_review_slots": review_slots_payload}
                if review_slots_payload
                else {}
            ),
            **({"role_profile_id": role_profile.profile_id} if role_profile is not None else {}),
            **({"claim_turn_index": turn_index} if turn_index is not None else {}),
        },
        execution_contract=execution_contract,
        lease_policy=role_profile.lease_policy if role_profile is not None else None,
        role_profile=role_profile,
    )


@dataclass(slots=True)
class TeammateAssignmentContext:
    claim_source: str
    claim_session_id: str
    activation_epoch: int
    slot_session_id: str | None
    current_directive_ids: tuple[str, ...] = ()


@dataclass(slots=True)
class TeammateExecutionResult:
    record: WorkerRecord | None = None
    envelope: MailboxEnvelope | None = None
    source_entry: BlackboardEntry | None = None
    claim_source: str | None = None
    claim_session_id: str | None = None
    task_status: TaskStatus | None = None


@dataclass(slots=True)
class DirectedClaimMaterialization:
    assignment: WorkerAssignment | None = None
    processed_envelope_id: str | None = None
    claim_session_id: str | None = None
    claim_source: str | None = None
    receipt_envelope: MailboxEnvelope | None = None


@dataclass(slots=True)
class TeammateSlotRunResult:
    teammate_records: tuple[WorkerRecord, ...] = ()
    mailbox_envelopes: tuple[MailboxEnvelope, ...] = ()
    claimed_task_ids: tuple[str, ...] = ()
    claim_session_ids: tuple[str, ...] = ()
    claim_sources: tuple[str, ...] = ()
    directed_claimed_task_ids: tuple[str, ...] = ()
    autonomous_claimed_task_ids: tuple[str, ...] = ()
    processed_mailbox_envelope_ids: tuple[str, ...] = ()
    teammate_execution_evidence: bool = False
    dispatched_assignment_count: int = 0
    coordinator_session: ResidentCoordinatorSession | None = None


class TeammateWorkSurface:
    def __init__(
        self,
        *,
        runtime: GroupRuntime,
        mailbox: MailboxBridge,
        objective: ObjectiveSpec,
        leader_round: LeaderRound,
        backend: str | None,
        working_dir: str | None,
        turn_index: int | None,
        role_profile: WorkerRoleProfile | None = None,
        session_host: ResidentSessionHost | None = None,
    ) -> None:
        self.runtime = runtime
        self.mailbox = mailbox
        self.objective = objective
        self.leader_round = leader_round
        self.backend = backend
        self.working_dir = working_dir
        self.turn_index = turn_index
        self.role_profile = role_profile
        self.session_host = session_host
        self._session_memory_service = SessionMemoryService(store=self.runtime.store)

    async def _resolve_continuity_context(self) -> tuple[str | None, str | None]:
        work_session = await self.runtime._resolve_work_session_for_objective(
            group_id=self.objective.group_id,
            objective_id=self.objective.objective_id,
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
        worker_id: str,
        actor_role: AgentTurnActorRole,
        turn_kind: AgentTurnKind,
        input_summary: str,
        output_summary: str,
        assignment_id: str | None = None,
        response_id: str | None = None,
        status: AgentTurnStatus = AgentTurnStatus.COMPLETED,
        metadata: dict[str, object] | None = None,
    ) -> AgentTurnRecord | None:
        work_session_id, runtime_generation_id = await self._resolve_continuity_context()
        if work_session_id is None or runtime_generation_id is None:
            return None
        turn_record = await self._session_memory_service.record_role_turn(
            work_session_id=work_session_id,
            runtime_generation_id=runtime_generation_id,
            head_kind=ConversationHeadKind.TEAMMATE_SLOT,
            scope_id=worker_id,
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
            head_backend="teammate_work_surface",
            head_model="teammate_slot",
            head_provider="agent_orchestra",
        )
        return turn_record

    async def _record_tool_invocation(
        self,
        *,
        turn_record: AgentTurnRecord | None,
        tool_kind: ToolInvocationKind,
        tool_name: str,
        input_summary: str,
        output_summary: str,
        metadata: dict[str, object] | None = None,
    ) -> None:
        if turn_record is None:
            return
        tool_record = ToolInvocationRecord(
            turn_record_id=turn_record.turn_record_id,
            work_session_id=turn_record.work_session_id,
            runtime_generation_id=turn_record.runtime_generation_id,
            tool_name=tool_name,
            tool_kind=tool_kind,
            input_summary=input_summary,
            output_summary=output_summary,
            status="completed",
            started_at=turn_record.created_at,
            completed_at=turn_record.created_at,
            metadata=dict(metadata or {}),
        )
        await self.runtime.store.append_tool_invocation_record(tool_record)

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

    def _teammate_slot_limit(
        self,
        *,
        slot_count: int,
    ) -> int:
        return max(1, slot_count)

    def _lifecycle_policy(
        self,
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

    def _surface_activation_profile(self) -> TeammateActivationProfile | None:
        profile = TeammateActivationProfile(
            backend=self.backend,
            working_dir=self.working_dir,
            role_profile_id=self.role_profile.profile_id if self.role_profile is not None else None,
            role_profile=self.role_profile,
        )
        return None if profile.is_empty() else profile

    @staticmethod
    def _assignment_activation_profile(
        assignment: WorkerAssignment | None,
    ) -> TeammateActivationProfile | None:
        if assignment is None:
            return None
        role_profile_id_raw = assignment.metadata.get("role_profile_id")
        role_profile_id = (
            role_profile_id_raw
            if isinstance(role_profile_id_raw, str) and role_profile_id_raw
            else None
        )
        profile = TeammateActivationProfile(
            backend=assignment.backend or None,
            working_dir=assignment.working_dir,
            role_profile_id=role_profile_id,
            role_profile=assignment.role_profile,
        )
        return None if profile.is_empty() else profile

    def _candidate_activation_profile(
        self,
        *,
        assignment: WorkerAssignment | None = None,
        activation_profile: TeammateActivationProfile | None = None,
    ) -> TeammateActivationProfile | None:
        candidate = activation_profile
        assignment_profile = self._assignment_activation_profile(assignment)
        if assignment_profile is not None:
            candidate = assignment_profile.merged_with(candidate)
        surface_profile = self._surface_activation_profile()
        if surface_profile is not None:
            candidate = surface_profile if candidate is None else candidate.merged_with(surface_profile)
        return candidate

    async def _resolve_slot_activation_profile(
        self,
        *,
        worker_id: str,
        assignment: WorkerAssignment | None = None,
        activation_profile: TeammateActivationProfile | None = None,
    ) -> TeammateActivationProfile | None:
        candidate = self._candidate_activation_profile(
            assignment=assignment,
            activation_profile=activation_profile,
        )
        slot_session_id, _, _ = await self.ensure_slot_session(
            worker_id=worker_id,
            activation_profile=candidate,
        )
        if self.session_host is not None and slot_session_id is not None:
            stored_profile = await self.session_host.load_teammate_activation_profile(
                slot_session_id,
            )
            if _activation_profile_is_runnable(stored_profile):
                return stored_profile
        return candidate

    async def ensure_slot_session(
        self,
        *,
        worker_id: str,
        activation_profile: TeammateActivationProfile | None = None,
    ) -> tuple[str | None, int | None, int]:
        teammate_slot = teammate_slot_from_worker_id(worker_id)
        if self.session_host is None or teammate_slot is None:
            return None, teammate_slot, 1
        session_id = resident_teammate_session_id(
            leader_round=self.leader_round,
            teammate_slot=teammate_slot,
        )
        candidate_profile = self._candidate_activation_profile(
            activation_profile=activation_profile,
        )
        session_metadata: dict[str, object] | None = None
        if candidate_profile is not None and not candidate_profile.is_empty():
            session_metadata = {
                "activation_profile": candidate_profile.to_metadata_payload(),
            }
        existing = await self.session_host.load_or_create_slot_session(
            session_id=session_id,
            agent_id=worker_id,
            objective_id=self.objective.objective_id,
            lane_id=self.leader_round.lane_id,
            team_id=self.leader_round.team_id,
            metadata=session_metadata,
        )
        stored_profile = TeammateActivationProfile.from_metadata(existing.metadata)
        if candidate_profile is None:
            resolved_profile = stored_profile
        elif stored_profile.is_empty():
            resolved_profile = candidate_profile
        else:
            # Once the host owns a slot profile, continuation fills only missing fields
            # from the current surface instead of rewriting the stored host truth.
            resolved_profile = stored_profile.merged_with(candidate_profile)
        if not resolved_profile.is_empty() and resolved_profile != stored_profile:
            existing = await self.session_host.record_teammate_activation_profile(
                session_id,
                activation_profile=resolved_profile,
            )
        raw_epoch = existing.metadata.get("activation_epoch")
        try:
            activation_epoch = int(raw_epoch)
        except (TypeError, ValueError):
            activation_epoch = 1
        return session_id, teammate_slot, activation_epoch

    async def load_authoritative_mailbox_cursor(
        self,
        *,
        recipient: str,
        fallback_cursor: object = None,
    ) -> str | None:
        if self.session_host is not None:
            slot_session_id = resident_teammate_session_id_for_recipient(recipient)
            if slot_session_id is not None:
                session = await self.session_host.load_session(slot_session_id)
                if session is not None:
                    session_cursor = mailbox_cursor_last_envelope_id(session.mailbox_cursor)
                    if session_cursor is not None:
                        return session_cursor
        stored_cursor = await self.runtime.store.get_protocol_bus_cursor(
            stream="mailbox",
            consumer=recipient,
        )
        stored_last_envelope_id = mailbox_cursor_last_envelope_id(stored_cursor)
        if stored_last_envelope_id is not None:
            return stored_last_envelope_id
        fallback_last_envelope_id = mailbox_cursor_last_envelope_id(fallback_cursor)
        if fallback_last_envelope_id is not None:
            return fallback_last_envelope_id
        if hasattr(self.mailbox, "get_cursor"):
            cursor = await self.mailbox.get_cursor(recipient)
            return cursor.last_envelope_id
        return None

    async def list_mailbox_messages(
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

    async def acknowledge_messages(
        self,
        recipient: str,
        messages: tuple[MailboxEnvelope, ...],
        *,
        current_directive_ids: tuple[str, ...] = (),
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
        slot_session_id = resident_teammate_session_id_for_recipient(recipient)

        async def _persist_cursor(consumer: str, cursor_payload: dict[str, str | None]) -> None:
            await self.runtime.store.save_protocol_bus_cursor(
                stream="mailbox",
                consumer=consumer,
                cursor=cursor_payload,
            )

        async def _acknowledge_bridge(consumer: str, message_ids: tuple[str, ...]) -> str | None:
            if hasattr(self.mailbox, "acknowledge"):
                cursor = await self.mailbox.acknowledge(consumer, message_ids)
                return cursor.last_envelope_id
            for envelope_id in message_ids:
                await self.mailbox.ack(consumer, envelope_id)  # type: ignore[attr-defined]
            return message_ids[-1]

        if self.session_host is not None and slot_session_id is not None:
            try:
                session = await self.session_host.commit_mailbox_consume(
                    slot_session_id,
                    recipient=recipient,
                    envelope_ids=envelope_ids,
                    current_directive_ids=current_directive_ids,
                    reason="Committed teammate mailbox consume.",
                    persist_cursor=_persist_cursor,
                    acknowledge_bridge=_acknowledge_bridge,
                )
                return mailbox_cursor_last_envelope_id(session.mailbox_cursor)
            except ValueError:
                pass

        last_envelope_id = await _acknowledge_bridge(recipient, envelope_ids)
        if last_envelope_id is None:
            return None
        await _persist_cursor(
            recipient,
            {
                "stream": "mailbox",
                "event_id": last_envelope_id,
                "last_envelope_id": last_envelope_id,
            },
        )
        return last_envelope_id

    async def _acknowledge_bridge_only(
        self,
        recipient: str,
        messages: tuple[MailboxEnvelope, ...],
    ) -> str | None:
        envelope_ids = tuple(
            message.envelope_id
            for message in messages
            if message.envelope_id is not None
        )
        if not envelope_ids:
            return None
        if hasattr(self.mailbox, "acknowledge"):
            cursor = await self.mailbox.acknowledge(recipient, envelope_ids)
            return cursor.last_envelope_id
        for envelope_id in envelope_ids:
            await self.mailbox.ack(recipient, envelope_id)  # type: ignore[attr-defined]
        return envelope_ids[-1]

    def _worker_id_for_slot(self, teammate_slot: int) -> str:
        return f"{self.leader_round.team_id}:teammate:{teammate_slot}"

    async def _record_slot_claim_state(
        self,
        *,
        worker_id: str,
        claim_session_id: str,
        claim_source: str,
        reason: str,
    ) -> None:
        if self.session_host is None:
            return
        slot_session_id, _, activation_epoch = await self.ensure_slot_session(worker_id=worker_id)
        if slot_session_id is None:
            return
        await self.session_host.record_teammate_slot_state(
            slot_session_id,
            activation_epoch=activation_epoch,
            current_task_id=None,
            current_claim_session_id=claim_session_id,
            last_claim_source=claim_source,
            current_worker_session_id=None,
            last_worker_session_id=None,
            idle=True,
            reason=reason,
        )

    async def _project_activation_session_snapshot(
        self,
        *,
        worker_id: str,
        claim_session_id: str,
        claim_source: str,
        current_directive_ids: tuple[str, ...] = (),
        reason: str,
        session: Any | None = None,
    ) -> Any | None:
        if self.session_host is None:
            return None
        slot_session_id, _, activation_epoch = await self.ensure_slot_session(worker_id=worker_id)
        if slot_session_id is None:
            return None
        return await self.session_host.project_teammate_activation_state(
            slot_session_id,
            session=session,
            activation_epoch=activation_epoch,
            current_claim_session_id=claim_session_id,
            last_claim_source=claim_source,
            current_directive_ids=current_directive_ids,
            reason=reason,
        )

    async def _commit_activation_receipt(
        self,
        *,
        task_id: str,
        worker_id: str,
        claim_source: str,
        claim_session_id: str,
        directive_id: str,
        correlation_id: str | None,
        status_summary: str,
        receipt_type: str,
        current_directive_ids: tuple[str, ...] = (),
        mailbox_consumer: str | None = None,
        consumer_cursor: dict[str, object] | None = None,
        session: Any | None = None,
    ) -> DirectedTaskReceiptCommit | None:
        session_snapshot = await self._project_activation_session_snapshot(
            worker_id=worker_id,
            claim_session_id=claim_session_id,
            claim_source=claim_source,
            current_directive_ids=current_directive_ids,
            reason=status_summary,
            session=session,
        )
        try:
            commit = await self.runtime.commit_directed_task_receipt(
                objective_id=self.objective.objective_id,
                lane_id=self.leader_round.lane_id,
                team_id=self.leader_round.team_id,
                task_id=task_id,
                worker_id=worker_id,
                claim_source=claim_source,
                directive_id=directive_id,
                correlation_id=correlation_id,
                claim_session_id=claim_session_id,
                mailbox_consumer=mailbox_consumer,
                consumer_cursor=consumer_cursor,
                status_summary=status_summary,
                receipt_type=receipt_type,
                session_snapshot=session_snapshot,
            )
        except ValueError:
            return None
        return commit

    async def _claim_directed_assignment_from_envelope(
        self,
        *,
        teammate_slot: int,
        recipient: str,
        envelope: MailboxEnvelope,
    ) -> DirectedClaimMaterialization:
        if not _is_directive_envelope(envelope):
            return DirectedClaimMaterialization()
        worker_id = recipient
        activation_profile = await self._resolve_slot_activation_profile(
            worker_id=worker_id,
        )
        if (
            activation_profile is None
            or activation_profile.backend is None
            or activation_profile.working_dir is None
        ):
            return DirectedClaimMaterialization()
        default_claim_session_id = resident_claim_session_id(
            objective=self.objective,
            leader_round=self.leader_round,
            teammate_slot=teammate_slot,
        )
        payload = envelope.payload if isinstance(envelope.payload, dict) else {}
        try:
            directive = parse_directed_task_directive(
                payload=payload,
                subject=envelope.subject,
            )
        except ValueError:
            return DirectedClaimMaterialization()
        if directive.target.worker_id and directive.target.worker_id != worker_id:
            return DirectedClaimMaterialization()
        task = await self.runtime.store.get_task(directive.task.task_id)
        if task is None:
            return DirectedClaimMaterialization()
        if task.group_id != self.objective.group_id:
            return DirectedClaimMaterialization()
        if task.team_id != self.leader_round.team_id:
            return DirectedClaimMaterialization()
        if task.lane != self.leader_round.lane_id:
            return DirectedClaimMaterialization()
        if task.scope != TaskScope.TEAM:
            return DirectedClaimMaterialization()

        claim_source = directive.claim.claim_source or "resident_mailbox_directed_claim"
        claim_session_id = directive.claim.claim_session_id or default_claim_session_id
        consumer_cursor: dict[str, object] | None = None
        projected_consume = None
        slot_session_id, _, _ = await self.ensure_slot_session(worker_id=worker_id)
        if self.session_host is not None and slot_session_id is not None:
            projected_consume = await self.session_host.project_mailbox_consume(
                slot_session_id,
                session=None,
                recipient=recipient,
                envelope_ids=(envelope.envelope_id,) if envelope.envelope_id is not None else (),
                current_directive_ids=(task.task_id,),
                reason="Committed teammate mailbox consume.",
            )
            consumer_cursor = dict(projected_consume.mailbox_cursor)
        elif envelope.envelope_id is not None:
            consumer_cursor = {
                "stream": "mailbox",
                "event_id": envelope.envelope_id,
                "last_envelope_id": envelope.envelope_id,
            }
        receipt_commit = await self._commit_activation_receipt(
            task_id=task.task_id,
            worker_id=worker_id,
            claim_source=claim_source,
            claim_session_id=claim_session_id,
            directive_id=directive.directive_id or (envelope.envelope_id or task.task_id),
            correlation_id=directive.correlation_id,
            status_summary=f"Teammate {worker_id} claimed directed task {task.task_id}.",
            receipt_type="claim_materialized",
            current_directive_ids=(task.task_id,),
            mailbox_consumer=recipient,
            consumer_cursor=consumer_cursor,
            session=projected_consume,
        )
        if receipt_commit is None:
            return DirectedClaimMaterialization()
        await self._acknowledge_bridge_only(recipient, (envelope,))
        claimed_task = receipt_commit.task
        resolved_claim_session_id = claimed_task.claim_session_id or claim_session_id
        resolved_claim_source = claimed_task.claim_source or claim_source
        claim_context = await self.runtime.get_task_claim_context(claimed_task.task_id)
        assignment = build_pending_teammate_assignment(
            objective=self.objective,
            leader_round=self.leader_round,
            task=claimed_task,
            claim_context=claim_context,
            teammate_slot=teammate_slot,
            turn_index=self.turn_index,
            backend=activation_profile.backend,
            working_dir=activation_profile.working_dir,
            role_profile=activation_profile.role_profile,
        )
        receipt_envelope = await self._publish_receipt_envelope(
            recipient=self.leader_round.leader_task.leader_id,
            commit=receipt_commit,
            sender=worker_id,
        )
        assignment.metadata["claim_recorded"] = True
        turn_record = await self._record_turn(
            worker_id=worker_id,
            actor_role=AgentTurnActorRole.TEAMMATE,
            turn_kind=AgentTurnKind.MAILBOX_FOLLOWUP,
            input_summary=envelope.summary or envelope.subject,
            output_summary=f"Claimed directed task {claimed_task.task_id}.",
            assignment_id=assignment.assignment_id,
            metadata={
                "claim_source": resolved_claim_source,
                "claim_session_id": resolved_claim_session_id,
                "task_id": claimed_task.task_id,
                "envelope_id": envelope.envelope_id,
            },
        )
        await self._record_tool_invocation(
            turn_record=turn_record,
            tool_kind=ToolInvocationKind.MAILBOX_COMMIT,
            tool_name="mailbox.consume",
            input_summary=str(envelope.envelope_id or ""),
            output_summary="directed claim materialized",
            metadata={"recipient": recipient},
        )
        await self._record_artifact(
            turn_record=turn_record,
            artifact_kind=ArtifactRefKind.MAILBOX_SNAPSHOT,
            uri=f"mailbox:{recipient}:{envelope.envelope_id or claimed_task.task_id}",
            payload={
                "envelope_id": envelope.envelope_id,
                "sender": envelope.sender,
                "recipient": envelope.recipient,
                "subject": envelope.subject,
                "summary": envelope.summary,
                "payload": envelope.payload,
            },
            metadata={"task_id": claimed_task.task_id},
        )
        return DirectedClaimMaterialization(
            assignment=assignment,
            processed_envelope_id=envelope.envelope_id,
            claim_session_id=resolved_claim_session_id,
            claim_source=resolved_claim_source,
            receipt_envelope=receipt_envelope,
        )

    async def _consume_authority_control_envelope(
        self,
        *,
        recipient: str,
        envelope: MailboxEnvelope,
    ) -> str | None:
        if not _is_authority_control_envelope(envelope):
            return None
        payload = envelope.payload if isinstance(envelope.payload, dict) else {}
        task_id_raw = payload.get("task_id")
        task_id = task_id_raw if isinstance(task_id_raw, str) and task_id_raw else None
        authority_request_payload = (
            payload.get("authority_request")
            if isinstance(payload.get("authority_request"), dict)
            else {}
        )
        authority_decision_payload = (
            payload.get("authority_decision")
            if isinstance(payload.get("authority_decision"), dict)
            else {}
        )
        request_id_raw = authority_request_payload.get("request_id") or authority_decision_payload.get("request_id")
        request_id = request_id_raw if isinstance(request_id_raw, str) and request_id_raw else None
        decision_raw = authority_decision_payload.get("decision")
        decision = decision_raw if isinstance(decision_raw, str) and decision_raw else None
        actor_id_raw = authority_decision_payload.get("actor_id")
        actor_id = actor_id_raw if isinstance(actor_id_raw, str) and actor_id_raw else envelope.sender
        reason_raw = (
            authority_decision_payload.get("summary")
            or authority_decision_payload.get("reason")
            or payload.get("summary")
        )
        reason = reason_raw if isinstance(reason_raw, str) else ""
        slot_session_id, _, _ = await self.ensure_slot_session(worker_id=recipient)
        if self.session_host is not None and slot_session_id is not None and request_id and task_id:
            relay_reason = (
                reason
                if reason
                else f"Consumed authority control envelope `{envelope.subject}`."
            )
            await self.session_host.record_authority_relay_state(
                slot_session_id,
                request_id=request_id,
                task_id=task_id,
                relay_subject=envelope.subject,
                relay_envelope_id=envelope.envelope_id,
                actor_id=actor_id,
                reason=relay_reason,
            )
            if decision == "escalate":
                boundary_class_raw = authority_decision_payload.get("scope_class")
                boundary_class = boundary_class_raw if isinstance(boundary_class_raw, str) else None
                await self.session_host.record_authority_wait_state(
                    slot_session_id,
                    request_id=request_id,
                    task_id=task_id,
                    boundary_class=boundary_class,
                    reason=reason,
                    requested_by=actor_id,
                )
            elif decision in {"grant", "deny", "reroute"}:
                resume_target_raw = authority_decision_payload.get("resume_target")
                resume_target = resume_target_raw if isinstance(resume_target_raw, str) else None
                await self.session_host.record_authority_decision_state(
                    slot_session_id,
                    request_id=request_id,
                    task_id=task_id,
                    decision=decision,
                    actor_id=actor_id,
                    resume_target=resume_target,
                    reason=reason,
                    relay_subject=envelope.subject,
                    relay_envelope_id=envelope.envelope_id,
                )
        await self._record_turn(
            worker_id=recipient,
            actor_role=AgentTurnActorRole.TEAMMATE,
            turn_kind=AgentTurnKind.AUTHORITY_TRANSITION,
            input_summary=envelope.subject,
            output_summary=reason or "Consumed authority control envelope.",
            metadata={
                "request_id": request_id,
                "task_id": task_id,
                "decision": decision,
                "actor_id": actor_id,
                "envelope_id": envelope.envelope_id,
            },
        )
        await self.acknowledge_messages(
            recipient,
            (envelope,),
            current_directive_ids=(),
        )
        return envelope.envelope_id

    async def _claim_autonomous_assignment_for_slot(
        self,
        teammate_slot: int,
    ) -> tuple[WorkerAssignment | None, str | None, str | None]:
        worker_id = self._worker_id_for_slot(teammate_slot)
        activation_profile = await self._resolve_slot_activation_profile(
            worker_id=worker_id,
        )
        if (
            activation_profile is None
            or activation_profile.backend is None
            or activation_profile.working_dir is None
        ):
            return None, None, None
        claim_session_id = resident_claim_session_id(
            objective=self.objective,
            leader_round=self.leader_round,
            teammate_slot=teammate_slot,
        )
        claimed_task = await self.runtime.claim_next_task(
            group_id=self.objective.group_id,
            owner_id=worker_id,
            claim_source="resident_task_list_claim",
            team_id=self.leader_round.team_id,
            lane_id=self.leader_round.lane_id,
            scope=TaskScope.TEAM,
            claim_session_id=claim_session_id,
        )
        if claimed_task is None:
            return None, None, None
        resolved_claim_session_id = claimed_task.claim_session_id or claim_session_id
        resolved_claim_source = claimed_task.claim_source or "resident_task_list_claim"
        await self._record_slot_claim_state(
            worker_id=worker_id,
            claim_session_id=resolved_claim_session_id,
            claim_source=resolved_claim_source,
            reason="Autonomous teammate task claimed from task surface.",
        )
        assignment_turn = await self._record_turn(
            worker_id=worker_id,
            actor_role=AgentTurnActorRole.TEAMMATE,
            turn_kind=AgentTurnKind.LEADER_DECISION,
            input_summary=f"Claim next task for slot {teammate_slot}.",
            output_summary=f"Claimed task {claimed_task.task_id}.",
            metadata={
                "claim_source": resolved_claim_source,
                "claim_session_id": resolved_claim_session_id,
                "task_id": claimed_task.task_id,
            },
        )
        claim_context = await self.runtime.get_task_claim_context(claimed_task.task_id)
        assignment = build_pending_teammate_assignment(
            objective=self.objective,
            leader_round=self.leader_round,
            task=claimed_task,
            claim_context=claim_context,
            teammate_slot=teammate_slot,
            turn_index=self.turn_index,
            backend=activation_profile.backend,
            working_dir=activation_profile.working_dir,
            role_profile=activation_profile.role_profile,
        )
        if assignment_turn is not None:
            assignment.metadata["claim_recorded"] = True
        return assignment, resolved_claim_session_id, resolved_claim_source

    async def acquire_assignments(
        self,
        existing_assignments: tuple[WorkerAssignment, ...],
        limit: int,
    ) -> ResidentTeammateAcquireResult:
        teammate_capacity = max(self.leader_round.leader_task.budget.max_teammates, 0)
        if teammate_capacity <= 0:
            return ResidentTeammateAcquireResult()
        limit = max(limit, 0)
        if limit == 0:
            return ResidentTeammateAcquireResult()

        used_slots = {
            slot
            for slot in (
                teammate_slot_from_worker_id(assignment.worker_id)
                for assignment in existing_assignments
            )
            if slot is not None
        }
        available_slots = [
            slot
            for slot in range(1, teammate_capacity + 1)
            if slot not in used_slots
        ]
        if not available_slots:
            return ResidentTeammateAcquireResult()

        max_new_assignments = min(len(available_slots), limit)
        assignments: list[WorkerAssignment] = []
        mailbox_envelopes: list[MailboxEnvelope] = []
        processed_mailbox_envelope_ids: list[str] = []
        directed_claimed_task_ids: list[str] = []
        autonomous_claimed_task_ids: list[str] = []
        claim_session_ids: list[str] = []
        claim_sources: list[str] = []

        while available_slots and len(assignments) < max_new_assignments:
            progress_made = False

            for teammate_slot in tuple(available_slots):
                if len(assignments) >= max_new_assignments:
                    break
                recipient = self._worker_id_for_slot(teammate_slot)
                mailbox_cursor = await self.load_authoritative_mailbox_cursor(
                    recipient=recipient,
                )
                messages = await self.list_mailbox_messages(
                    recipient,
                    after_envelope_id=mailbox_cursor,
                )
                if not messages:
                    continue

                envelope = messages[0]
                consumed_control_envelope_id = await self._consume_authority_control_envelope(
                    recipient=recipient,
                    envelope=envelope,
                )
                if consumed_control_envelope_id is not None:
                    processed_mailbox_envelope_ids.append(consumed_control_envelope_id)
                    progress_made = True
                    continue
                materialized = (
                    await self._claim_directed_assignment_from_envelope(
                        teammate_slot=teammate_slot,
                        recipient=recipient,
                        envelope=envelope,
                    )
                )
                if materialized.assignment is None:
                    continue
                if materialized.processed_envelope_id is not None:
                    processed_mailbox_envelope_ids.append(materialized.processed_envelope_id)
                directed_claimed_task_ids.append(materialized.assignment.task_id)
                if materialized.claim_session_id is not None:
                    claim_session_ids.append(materialized.claim_session_id)
                if materialized.claim_source is not None:
                    claim_sources.append(materialized.claim_source)
                if materialized.receipt_envelope is not None:
                    mailbox_envelopes.append(materialized.receipt_envelope)
                available_slots.remove(teammate_slot)
                assignments.append(materialized.assignment)
                progress_made = True

            if len(assignments) >= max_new_assignments:
                break

            for teammate_slot in tuple(available_slots):
                if len(assignments) >= max_new_assignments:
                    break
                assignment, claim_session_id, claim_source = await self._claim_autonomous_assignment_for_slot(
                    teammate_slot
                )
                if assignment is None:
                    continue
                autonomous_claimed_task_ids.append(assignment.task_id)
                if claim_session_id is not None:
                    claim_session_ids.append(claim_session_id)
                if claim_source is not None:
                    claim_sources.append(claim_source)
                available_slots.remove(teammate_slot)
                assignments.append(assignment)
                progress_made = True

            if not progress_made:
                break

        return ResidentTeammateAcquireResult(
            assignments=tuple(assignments),
            mailbox_envelopes=tuple(mailbox_envelopes),
            processed_mailbox_envelope_ids=ordered_unique_strings(processed_mailbox_envelope_ids),
            directed_claimed_task_ids=ordered_unique_strings(directed_claimed_task_ids),
            autonomous_claimed_task_ids=ordered_unique_strings(autonomous_claimed_task_ids),
            claim_session_ids=ordered_unique_strings(claim_session_ids),
            claim_sources=ordered_unique_strings(claim_sources),
            teammate_execution_evidence=bool(assignments),
        )

    async def prepare_assignment(
        self,
        assignment: WorkerAssignment,
    ) -> TeammateAssignmentContext:
        claim_source_raw = assignment.metadata.get("claim_source")
        claim_source = (
            claim_source_raw
            if isinstance(claim_source_raw, str) and claim_source_raw
            else "autonomous_claim"
        )
        claim_session_raw = assignment.metadata.get("claim_session_id")
        claim_session_id = (
            claim_session_raw
            if isinstance(claim_session_raw, str) and claim_session_raw
            else None
        )
        teammate_slot = teammate_slot_from_worker_id(assignment.worker_id)
        if claim_session_id is None and teammate_slot is not None:
            claim_session_id = resident_claim_session_id(
                objective=self.objective,
                leader_round=self.leader_round,
                teammate_slot=teammate_slot,
            )
        if claim_session_id is None:
            raise ValueError(f"Unable to derive claim_session_id for {assignment.assignment_id}")
        claimed_task = await self.runtime.claim_task(
            task_id=assignment.task_id,
            owner_id=assignment.worker_id,
            claim_source=claim_source,
            claim_session_id=claim_session_id,
        )
        if claimed_task is not None:
            assignment.metadata["claim_source"] = claimed_task.claim_source
            assignment.metadata["claim_session_id"] = claimed_task.claim_session_id
            claim_source = claimed_task.claim_source or claim_source
            claim_session_id = claimed_task.claim_session_id or claim_session_id
        else:
            assignment.metadata["claim_source"] = claim_source
            assignment.metadata["claim_session_id"] = claim_session_id

        if not assignment.metadata.get("claim_recorded"):
            turn_kind = (
                AgentTurnKind.MAILBOX_FOLLOWUP
                if claim_source == "resident_mailbox_directed_claim"
                else AgentTurnKind.LEADER_DECISION
            )
            await self._record_turn(
                worker_id=assignment.worker_id,
                actor_role=AgentTurnActorRole.TEAMMATE,
                turn_kind=turn_kind,
                input_summary=assignment.input_text,
                output_summary=f"Claimed task {assignment.task_id}.",
                assignment_id=assignment.assignment_id,
                metadata={
                    "claim_source": claim_source,
                    "claim_session_id": claim_session_id,
                    "task_id": assignment.task_id,
                },
            )
            assignment.metadata["claim_recorded"] = True

        slot_session_id, _, activation_epoch = await self.ensure_slot_session(
            worker_id=assignment.worker_id,
            activation_profile=self._assignment_activation_profile(assignment),
        )
        current_directive_ids = (
            (assignment.task_id,)
            if assignment.metadata.get("claim_source") == "resident_mailbox_directed_claim"
            else ()
        )
        if self.session_host is not None and slot_session_id is not None:
            await self.session_host.record_teammate_slot_state(
                slot_session_id,
                activation_epoch=activation_epoch,
                current_task_id=assignment.task_id,
                current_claim_session_id=claim_session_id,
                last_claim_source=claim_source,
                current_worker_session_id=None,
                last_worker_session_id=None,
                idle=False,
                reason=f"Running teammate assignment {assignment.assignment_id}.",
            )
            await self.session_host.update_session(
                slot_session_id,
                current_directive_ids=current_directive_ids,
            )
        return TeammateAssignmentContext(
            claim_source=claim_source,
            claim_session_id=claim_session_id,
            activation_epoch=activation_epoch,
            slot_session_id=slot_session_id,
            current_directive_ids=current_directive_ids,
        )

    async def reserve_initial_assignments(
        self,
        assignments: tuple[WorkerAssignment, ...],
    ) -> tuple[WorkerAssignment, ...]:
        reserved: list[WorkerAssignment] = []
        for assignment in assignments:
            claim_source_raw = assignment.metadata.get("claim_source")
            claim_source = (
                claim_source_raw
                if isinstance(claim_source_raw, str) and claim_source_raw
                else "leader_assignment_dispatch"
            )
            claim_session_raw = assignment.metadata.get("claim_session_id")
            claim_session_id = (
                claim_session_raw
                if isinstance(claim_session_raw, str) and claim_session_raw
                else None
            )
            teammate_slot = teammate_slot_from_worker_id(assignment.worker_id)
            if claim_session_id is None and teammate_slot is not None:
                claim_session_id = resident_claim_session_id(
                    objective=self.objective,
                    leader_round=self.leader_round,
                    teammate_slot=teammate_slot,
                )
            if claim_session_id is None:
                reserved.append(assignment)
                continue
            status_summary = (
                f"Leader reserved teammate task {assignment.task_id} for {assignment.worker_id} activation."
            )
            commit = await self._commit_activation_receipt(
                task_id=assignment.task_id,
                worker_id=assignment.worker_id,
                claim_source=claim_source,
                claim_session_id=claim_session_id,
                directive_id=assignment.assignment_id,
                correlation_id=None,
                status_summary=status_summary,
                receipt_type="activation_reserved",
                current_directive_ids=(),
            )
            if commit is None:
                claimed_task = await self.runtime.claim_task(
                    task_id=assignment.task_id,
                    owner_id=assignment.worker_id,
                    claim_source=claim_source,
                    claim_session_id=claim_session_id,
                )
                if claimed_task is not None:
                    assignment.metadata["claim_source"] = claimed_task.claim_source
                    assignment.metadata["claim_session_id"] = claimed_task.claim_session_id
                else:
                    assignment.metadata["claim_source"] = claim_source
                    assignment.metadata["claim_session_id"] = claim_session_id
                reserved.append(assignment)
                continue
            assignment.metadata["claim_source"] = commit.task.claim_source
            assignment.metadata["claim_session_id"] = commit.task.claim_session_id
            assignment.metadata["activation_receipt_type"] = commit.receipt.receipt_type
            reserved.append(assignment)
        return tuple(reserved)

    async def _host_owned_slot_ids(
        self,
    ) -> tuple[int, ...]:
        if self.session_host is None:
            return ()
        teammate_capacity = max(self.leader_round.leader_task.budget.max_teammates, 0)
        if teammate_capacity <= 0:
            return ()
        sessions = await self.session_host.list_runnable_teammate_slot_sessions(
            objective_id=self.objective.objective_id,
            lane_id=self.leader_round.lane_id,
            team_id=self.leader_round.team_id,
        )
        slot_ids = ordered_unique_strings(
            [
                str(slot)
                for slot in (
                    teammate_slot_from_worker_id(session.agent_id)
                    for session in sessions
                )
                if slot is not None and 1 <= slot <= teammate_capacity
            ]
        )
        return tuple(int(slot) for slot in slot_ids)

    async def _slot_ids_to_activate(
        self,
        assignments: tuple[WorkerAssignment, ...],
    ) -> tuple[int, ...]:
        activated_slots = ordered_unique_strings(
            [
                str(slot)
                for slot in (
                    teammate_slot_from_worker_id(assignment.worker_id)
                    for assignment in assignments
                )
                if slot is not None
            ]
        )
        host_owned_slots = await self._host_owned_slot_ids()
        if activated_slots:
            return tuple(
                int(slot)
                for slot in ordered_unique_strings(
                    [*activated_slots, *(str(slot) for slot in host_owned_slots)]
                )
            )
        if host_owned_slots:
            return host_owned_slots
        return ()

    async def _mark_slot_waiting(
        self,
        *,
        worker_id: str,
        reason: str,
    ) -> None:
        if self.session_host is None:
            return
        slot_session_id, _, _ = await self.ensure_slot_session(worker_id=worker_id)
        if slot_session_id is None:
            return
        await self.session_host.mark_phase(
            slot_session_id,
            ResidentCoordinatorPhase.IDLE,
            reason=reason,
        )

    async def _run_slot_online_loop(
        self,
        *,
        teammate_slot: int,
        initial_assignments: tuple[WorkerAssignment, ...],
        request_permission: RequestPermission,
        execution_policy: WorkerExecutionPolicy | None,
        keep_session_idle: bool,
        execution_semaphore: asyncio.Semaphore | None = None,
    ) -> TeammateSlotRunResult:
        worker_id = self._worker_id_for_slot(teammate_slot)
        existing_slot_session = None
        existing_slot_profile: TeammateActivationProfile | None = None
        if self.session_host is not None:
            existing_slot_session = await self.session_host.load_session(
                resident_teammate_session_id(
                    leader_round=self.leader_round,
                    teammate_slot=teammate_slot,
                )
            )
            if existing_slot_session is not None:
                existing_slot_profile = TeammateActivationProfile.from_metadata(
                    existing_slot_session.metadata
                )
        initial_activation_profile = None
        if initial_assignments:
            initial_activation_profile = self._assignment_activation_profile(initial_assignments[0])
        slot_session_id, _, activation_epoch = await self.ensure_slot_session(
            worker_id=worker_id,
            activation_profile=initial_activation_profile,
        )
        if self.session_host is not None and slot_session_id is not None:
            should_record_host_activation = bool(initial_assignments) or not _activation_profile_is_runnable(
                existing_slot_profile
            )
            if should_record_host_activation:
                await self.session_host.record_activation_intent(
                    slot_session_id,
                    reason="Leader activated teammate slot for online collaboration.",
                    requested_by=self.leader_round.leader_task.leader_id,
                    activation_epoch=activation_epoch,
                )
                await self.session_host.record_wake_request(
                    slot_session_id,
                    reason="Teammate slot online loop starting.",
                    requested_by=self.leader_round.leader_task.leader_id,
                )

        pending_assignments = list(initial_assignments)
        teammate_records: list[WorkerRecord] = []
        mailbox_envelopes: list[MailboxEnvelope] = []
        claimed_task_ids: list[str] = []
        claim_session_ids: list[str] = []
        claim_sources: list[str] = []
        directed_claimed_task_ids: list[str] = []
        autonomous_claimed_task_ids: list[str] = []
        processed_mailbox_envelope_ids: list[str] = []
        dispatched_assignment_count = 0
        teammate_execution_evidence = bool(initial_assignments)
        stop_event = asyncio.Event()
        idle_limit = 1 if keep_session_idle else 0
        consecutive_idle_waits = 0
        loop = TeammateOnlineLoop()

        def _record_progress() -> None:
            nonlocal consecutive_idle_waits
            consecutive_idle_waits = 0

        async def poll_mailbox() -> tuple[MailboxEnvelope, ...]:
            nonlocal teammate_execution_evidence
            mailbox_cursor = await self.load_authoritative_mailbox_cursor(recipient=worker_id)
            unread_messages = await self.list_mailbox_messages(
                worker_id,
                after_envelope_id=mailbox_cursor,
            )
            processed: list[MailboxEnvelope] = []
            for envelope in unread_messages:
                consumed_control_envelope_id = await self._consume_authority_control_envelope(
                    recipient=worker_id,
                    envelope=envelope,
                )
                if consumed_control_envelope_id is not None:
                    _record_progress()
                    if consumed_control_envelope_id:
                        processed_mailbox_envelope_ids.append(consumed_control_envelope_id)
                    processed.append(envelope)
                    continue
                materialized = await self._claim_directed_assignment_from_envelope(
                    teammate_slot=teammate_slot,
                    recipient=worker_id,
                    envelope=envelope,
                )
                if materialized.assignment is None:
                    continue
                pending_assignments.append(materialized.assignment)
                _record_progress()
                teammate_execution_evidence = True
                if materialized.processed_envelope_id is not None:
                    processed_mailbox_envelope_ids.append(materialized.processed_envelope_id)
                directed_claimed_task_ids.append(materialized.assignment.task_id)
                claimed_task_ids.append(materialized.assignment.task_id)
                if materialized.claim_session_id is not None:
                    claim_session_ids.append(materialized.claim_session_id)
                if materialized.claim_source is not None:
                    claim_sources.append(materialized.claim_source)
                if materialized.receipt_envelope is not None:
                    mailbox_envelopes.append(materialized.receipt_envelope)
                processed.append(envelope)
            return tuple(processed)

        async def claim_task() -> WorkerAssignment | None:
            nonlocal teammate_execution_evidence
            if pending_assignments:
                _record_progress()
                assignment = pending_assignments.pop(0)
                teammate_execution_evidence = True
                return assignment
            assignment, claim_session_id, claim_source = await self._claim_autonomous_assignment_for_slot(
                teammate_slot
            )
            if assignment is None:
                return None
            _record_progress()
            teammate_execution_evidence = True
            claimed_task_ids.append(assignment.task_id)
            autonomous_claimed_task_ids.append(assignment.task_id)
            if claim_session_id is not None:
                claim_session_ids.append(claim_session_id)
            if claim_source is not None:
                claim_sources.append(claim_source)
            return assignment

        async def on_mailbox_envelope(_envelope: MailboxEnvelope) -> None:
            return None

        async def on_task_claim(assignment: WorkerAssignment) -> None:
            nonlocal dispatched_assignment_count
            nonlocal teammate_execution_evidence
            if execution_semaphore is None:
                execution = await self.execute_assignment(
                    assignment,
                    request_permission=request_permission,
                    execution_policy=execution_policy,
                )
            else:
                async with execution_semaphore:
                    execution = await self.execute_assignment(
                        assignment,
                        request_permission=request_permission,
                        execution_policy=execution_policy,
                    )
            dispatched_assignment_count += 1
            teammate_execution_evidence = True
            _record_progress()
            if execution.record is not None:
                teammate_records.append(execution.record)
            if execution.envelope is not None:
                mailbox_envelopes.append(execution.envelope)
            if execution.claim_session_id is not None:
                claim_session_ids.append(execution.claim_session_id)
            if execution.claim_source is not None:
                claim_sources.append(execution.claim_source)

        async def idle_wait() -> None:
            nonlocal consecutive_idle_waits
            reason = "Resident teammate slot waiting for mailbox or task-surface work."
            await self._mark_slot_waiting(
                worker_id=worker_id,
                reason=reason,
            )
            if consecutive_idle_waits >= idle_limit:
                stop_event.set()
                return
            consecutive_idle_waits += 1
            await asyncio.sleep(0)

        loop_result = await loop.run(
            poll_mailbox=poll_mailbox,
            claim_task=claim_task,
            on_mailbox_envelope=on_mailbox_envelope,
            on_task_claim=on_task_claim,
            idle_wait=idle_wait,
            stop_event=stop_event,
        )

        coordinator_session = ResidentCoordinatorSession(
            coordinator_id=f"{worker_id}:online-loop",
            role="teammate",
            phase=ResidentCoordinatorPhase.IDLE if keep_session_idle else ResidentCoordinatorPhase.QUIESCENT,
            objective_id=self.objective.objective_id,
            lane_id=self.leader_round.lane_id,
            team_id=self.leader_round.team_id,
            cycle_count=loop_result.metrics.iterations,
            claimed_task_count=len(ordered_unique_strings(claimed_task_ids)),
            subordinate_dispatch_count=dispatched_assignment_count,
            mailbox_poll_count=loop_result.metrics.mailbox_polls,
            idle_transition_count=loop_result.metrics.idle_waits,
            last_reason="Resident teammate slot reached bounded quiescence.",
        )
        natural_idle_phase = coordinator_session.phase
        if keep_session_idle:
            idle_wait_request = PermissionRequest(
                requester=worker_id,
                action="resident.idle_wait",
                rationale="Keep the resident teammate slot idle-attached after host-owned work drains.",
                group_id=self.objective.group_id,
                objective_id=self.objective.objective_id,
                team_id=self.leader_round.team_id,
                lane_id=self.leader_round.lane_id,
                metadata={
                    "role": "teammate",
                    "session_id": slot_session_id,
                    "target_phase": natural_idle_phase.value,
                },
            )
            idle_wait_decision = await request_permission(idle_wait_request)
            if self.session_host is not None:
                await self.session_host.record_resident_shell_approval(
                    approval_kind="idle_wait",
                    status=approval_status_from_permission_decision(idle_wait_decision),
                    request=idle_wait_request,
                    decision=idle_wait_decision,
                    requested_by=worker_id,
                    objective_id=self.objective.objective_id,
                    lane_id=self.leader_round.lane_id,
                    team_id=self.leader_round.team_id,
                    target_session_id=slot_session_id,
                    target_mode=natural_idle_phase.value,
                )
            if not idle_wait_decision.approved and not idle_wait_decision.pending:
                denial_reason = (
                    idle_wait_decision.reason
                    or "Resident teammate idle wait was denied."
                )
                coordinator_session = replace(
                    coordinator_session,
                    phase=ResidentCoordinatorPhase.QUIESCENT,
                    last_reason=denial_reason,
                )
                if self.session_host is not None and slot_session_id is not None:
                    await self.session_host.mark_phase(
                        slot_session_id,
                        ResidentCoordinatorPhase.QUIESCENT,
                        reason=denial_reason,
                    )
        return TeammateSlotRunResult(
            teammate_records=tuple(teammate_records),
            mailbox_envelopes=tuple(mailbox_envelopes),
            claimed_task_ids=ordered_unique_strings(claimed_task_ids),
            claim_session_ids=ordered_unique_strings(claim_session_ids),
            claim_sources=ordered_unique_strings(claim_sources),
            directed_claimed_task_ids=ordered_unique_strings(directed_claimed_task_ids),
            autonomous_claimed_task_ids=ordered_unique_strings(autonomous_claimed_task_ids),
            processed_mailbox_envelope_ids=ordered_unique_strings(processed_mailbox_envelope_ids),
            teammate_execution_evidence=teammate_execution_evidence,
            dispatched_assignment_count=dispatched_assignment_count,
            coordinator_session=coordinator_session,
        )

    async def ensure_or_step_sessions(
        self,
        *,
        assignments: tuple[WorkerAssignment, ...],
        request_permission: RequestPermission,
        resident_kernel: ResidentCoordinatorKernel | None = None,
        keep_session_idle: bool = False,
        execution_policy: WorkerExecutionPolicy | None = None,
    ) -> ResidentTeammateRunResult:
        reserved_assignments = await self.reserve_initial_assignments(assignments)
        resolved_policy = self._lifecycle_policy(
            keep_idle=keep_session_idle,
            base_policy=execution_policy,
        )

        assignment_queues: dict[int, list[WorkerAssignment]] = {}
        for assignment in reserved_assignments:
            teammate_slot = teammate_slot_from_worker_id(assignment.worker_id)
            if teammate_slot is None:
                continue
            assignment_queues.setdefault(teammate_slot, []).append(assignment)

        slot_ids = await self._slot_ids_to_activate(reserved_assignments)
        concurrency_limit = self._teammate_slot_limit(
            slot_count=max(len(slot_ids), 1),
        )
        execution_semaphore = asyncio.Semaphore(max(concurrency_limit, 1))
        active_kernel = resident_kernel or ResidentCoordinatorKernel()
        slot_results: list[TeammateSlotRunResult] = []

        def _aggregate_result(
            coordinator_session: ResidentCoordinatorSession,
        ) -> ResidentTeammateRunResult:
            teammate_records = tuple(
                record
                for slot_result in slot_results
                for record in slot_result.teammate_records
            )
            mailbox_envelopes = tuple(
                envelope
                for slot_result in slot_results
                for envelope in slot_result.mailbox_envelopes
            )
            claimed_task_ids = ordered_unique_strings(
                [
                    task_id
                    for slot_result in slot_results
                    for task_id in slot_result.claimed_task_ids
                ]
            )
            claim_session_ids = ordered_unique_strings(
                [
                    claim_session_id
                    for slot_result in slot_results
                    for claim_session_id in slot_result.claim_session_ids
                ]
            )
            claim_sources = ordered_unique_strings(
                [
                    claim_source
                    for slot_result in slot_results
                    for claim_source in slot_result.claim_sources
                ]
            )
            directed_claimed_task_ids = ordered_unique_strings(
                [
                    task_id
                    for slot_result in slot_results
                    for task_id in slot_result.directed_claimed_task_ids
                ]
            )
            autonomous_claimed_task_ids = ordered_unique_strings(
                [
                    task_id
                    for slot_result in slot_results
                    for task_id in slot_result.autonomous_claimed_task_ids
                ]
            )
            processed_mailbox_envelope_ids = ordered_unique_strings(
                [
                    envelope_id
                    for slot_result in slot_results
                    for envelope_id in slot_result.processed_mailbox_envelope_ids
                ]
            )
            dispatched_assignment_count = sum(
                slot_result.dispatched_assignment_count
                for slot_result in slot_results
            )
            teammate_execution_evidence = any(
                slot_result.teammate_execution_evidence
                for slot_result in slot_results
            )
            coordinator_metadata = dict(coordinator_session.metadata)
            coordinator_metadata.update(
                {
                    "slot_count": len(slot_results),
                    "claim_sources": claim_sources,
                    "claimed_task_ids": claimed_task_ids,
                    "claim_session_ids": claim_session_ids,
                    "directed_claimed_task_ids": directed_claimed_task_ids,
                    "autonomous_claimed_task_ids": autonomous_claimed_task_ids,
                    "processed_mailbox_envelope_ids": processed_mailbox_envelope_ids,
                    "teammate_execution_evidence": teammate_execution_evidence,
                    "dispatched_assignment_count": dispatched_assignment_count,
                }
            )
            final_coordinator_session = replace(
                coordinator_session,
                metadata=coordinator_metadata,
            )
            return ResidentTeammateRunResult(
                teammate_records=teammate_records,
                mailbox_envelopes=mailbox_envelopes,
                claimed_task_ids=claimed_task_ids,
                claim_session_ids=claim_session_ids,
                claim_sources=claim_sources,
                directed_claimed_task_ids=directed_claimed_task_ids,
                autonomous_claimed_task_ids=autonomous_claimed_task_ids,
                processed_mailbox_envelope_ids=processed_mailbox_envelope_ids,
                teammate_execution_evidence=teammate_execution_evidence,
                dispatched_assignment_count=dispatched_assignment_count,
                coordinator_session=final_coordinator_session,
            )

        async def _step(
            session: ResidentCoordinatorSession,
        ) -> ResidentCoordinatorCycleResult:
            nonlocal slot_results
            slot_results = list(
                await asyncio.gather(
                    *[
                        self._run_slot_online_loop(
                            teammate_slot=slot_id,
                            initial_assignments=tuple(assignment_queues.get(slot_id, ())),
                            request_permission=request_permission,
                            execution_policy=resolved_policy,
                            keep_session_idle=keep_session_idle,
                            execution_semaphore=execution_semaphore,
                        )
                        for slot_id in slot_ids
                    ]
                )
            )
            claimed_task_delta = len(
                ordered_unique_strings(
                    [
                        task_id
                        for slot_result in slot_results
                        for task_id in slot_result.claimed_task_ids
                    ]
                )
            )
            subordinate_dispatch_delta = sum(
                slot_result.dispatched_assignment_count
                for slot_result in slot_results
            )
            mailbox_poll_delta = sum(
                slot_result.coordinator_session.mailbox_poll_count
                for slot_result in slot_results
                if slot_result.coordinator_session is not None
            )
            progress_made = any(
                slot_result.teammate_execution_evidence
                for slot_result in slot_results
            )
            all_slots_quiescent = (
                bool(slot_results)
                and all(
                    slot_result.coordinator_session.phase == ResidentCoordinatorPhase.QUIESCENT
                    for slot_result in slot_results
                )
            )
            return ResidentCoordinatorCycleResult(
                phase=(
                    ResidentCoordinatorPhase.IDLE
                    if keep_session_idle and not all_slots_quiescent
                    else ResidentCoordinatorPhase.QUIESCENT
                ),
                stop=True,
                progress_made=progress_made,
                claimed_task_delta=claimed_task_delta,
                subordinate_dispatch_delta=subordinate_dispatch_delta,
                mailbox_poll_delta=mailbox_poll_delta,
                reason="Teammate work surface drained the active slot loops.",
            )

        async def _finalize(
            final_session: ResidentCoordinatorSession,
        ) -> ResidentTeammateRunResult:
            return _aggregate_result(final_session)

        return await active_kernel.run(
            session=ResidentCoordinatorSession(
                coordinator_id=f"{self.leader_round.team_id}:teammate-runtime",
                role="teammate_runtime",
                phase=ResidentCoordinatorPhase.BOOTING,
                objective_id=self.objective.objective_id,
                lane_id=self.leader_round.lane_id,
                team_id=self.leader_round.team_id,
            ),
            step=_step,
            finalize=_finalize,
        )

    async def run(
        self,
        *,
        assignments: tuple[WorkerAssignment, ...],
        request_permission: RequestPermission,
        resident_kernel: ResidentCoordinatorKernel | None = None,
        keep_session_idle: bool = False,
        execution_policy: WorkerExecutionPolicy | None = None,
    ) -> ResidentTeammateRunResult:
        return await self.ensure_or_step_sessions(
            assignments=assignments,
            request_permission=request_permission,
            resident_kernel=resident_kernel,
            keep_session_idle=keep_session_idle,
            execution_policy=execution_policy,
        )

    async def step_runnable_host_slots(
        self,
        *,
        request_permission: RequestPermission,
        resident_kernel: ResidentCoordinatorKernel | None = None,
        keep_session_idle: bool = False,
        execution_policy: WorkerExecutionPolicy | None = None,
    ) -> ResidentTeammateRunResult:
        return await self.ensure_or_step_sessions(
            assignments=(),
            request_permission=request_permission,
            resident_kernel=resident_kernel,
            keep_session_idle=keep_session_idle,
            execution_policy=execution_policy,
        )

    async def _append_result_entry(
        self,
        *,
        assignment: WorkerAssignment,
        record: WorkerRecord,
    ) -> BlackboardEntry:
        if record.status == WorkerStatus.COMPLETED:
            return await self.runtime.append_blackboard_entry(
                group_id=self.objective.group_id,
                kind=BlackboardKind.TEAM,
                entry_kind=BlackboardEntryKind.EXECUTION_REPORT,
                author_id=assignment.worker_id,
                lane_id=self.leader_round.lane_id,
                team_id=self.leader_round.team_id,
                task_id=assignment.task_id,
                summary=f"Teammate {assignment.worker_id} completed task {assignment.task_id}.",
                payload={"output_text": record.output_text},
            )
        return await self.runtime.append_blackboard_entry(
            group_id=self.objective.group_id,
            kind=BlackboardKind.TEAM,
            entry_kind=BlackboardEntryKind.BLOCKER,
            author_id=assignment.worker_id,
            lane_id=self.leader_round.lane_id,
            team_id=self.leader_round.team_id,
            task_id=assignment.task_id,
            summary=f"Teammate {assignment.worker_id} failed task {assignment.task_id}.",
            payload={"error_text": record.error_text},
        )

    async def _publish_result_envelope(
        self,
        *,
        assignment: WorkerAssignment,
        source_entry: BlackboardEntry,
        record: WorkerRecord,
    ) -> MailboxEnvelope:
        severity = "info" if record.status == WorkerStatus.COMPLETED else "error"
        payload = DirectedTaskResult(
            task_id=assignment.task_id,
            status=record.status.value,
            summary=record.output_text or record.error_text,
            artifact_refs=(f"blackboard:{source_entry.entry_id}",),
            compat_task_id=assignment.task_id,
        ).to_payload()
        return await self.mailbox.send(
            MailboxEnvelope(
                sender=assignment.worker_id,
                recipient=self.leader_round.leader_task.leader_id,
                subject="task.result",
                mailbox_id=f"{self.objective.group_id}:leader:{self.leader_round.lane_id}",
                kind=MailboxMessageKind.TEAMMATE_RESULT,
                group_id=self.objective.group_id,
                lane_id=self.leader_round.lane_id,
                team_id=self.leader_round.team_id,
                summary=source_entry.summary,
                full_text_ref=f"blackboard:{source_entry.entry_id}",
                source_entry_id=source_entry.entry_id,
                source_scope=source_entry.kind.value,
                visibility_scope=MailboxVisibilityScope.SHARED,
                delivery_mode=MailboxDeliveryMode.SUMMARY_PLUS_REF,
                severity=severity,
                tags=(
                    "teammate_result",
                    f"task_status:{record.status.value}",
                    source_entry.entry_kind.value,
                ),
                payload=payload,
                metadata={"source_blackboard_id": source_entry.blackboard_id},
            )
        )

    async def _publish_authority_request_envelope(
        self,
        *,
        assignment: WorkerAssignment,
        commit: AuthorityRequestCommit,
    ) -> MailboxEnvelope:
        return await self.mailbox.send(
            MailboxEnvelope(
                sender=assignment.worker_id,
                recipient=self.leader_round.leader_task.leader_id,
                subject="authority.request",
                mailbox_id=f"{self.objective.group_id}:leader:{self.leader_round.lane_id}",
                kind=MailboxMessageKind.SYSTEM,
                group_id=self.objective.group_id,
                lane_id=self.leader_round.lane_id,
                team_id=self.leader_round.team_id,
                summary=commit.blackboard_entry.summary,
                full_text_ref=f"blackboard:{commit.blackboard_entry.entry_id}",
                source_entry_id=commit.blackboard_entry.entry_id,
                source_scope=commit.blackboard_entry.kind.value,
                visibility_scope=MailboxVisibilityScope.CONTROL_PRIVATE,
                delivery_mode=MailboxDeliveryMode.SUMMARY_PLUS_REF,
                severity="warning",
                tags=("authority_request", f"task_status:{commit.task.status.value}"),
                payload={
                    "task_id": assignment.task_id,
                    "authority_request": commit.authority_request.to_dict(),
                    "summary": commit.blackboard_entry.summary,
                    "worker_status": record_status if (record_status := commit.blackboard_entry.payload.get("worker_status")) else None,
                    "retry_hint": commit.authority_request.retry_hint,
                },
                metadata={"source_blackboard_id": commit.blackboard_entry.blackboard_id},
            )
        )

    async def _publish_receipt_envelope(
        self,
        *,
        recipient: str,
        commit: DirectedTaskReceiptCommit,
        sender: str,
    ) -> MailboxEnvelope:
        return await self.mailbox.send(
            MailboxEnvelope(
                sender=sender,
                recipient=recipient,
                subject="task.receipt",
                mailbox_id=f"{self.objective.group_id}:leader:{self.leader_round.lane_id}",
                kind=MailboxMessageKind.SYSTEM,
                group_id=self.objective.group_id,
                lane_id=self.leader_round.lane_id,
                team_id=self.leader_round.team_id,
                summary=commit.blackboard_entry.summary,
                full_text_ref=f"blackboard:{commit.blackboard_entry.entry_id}",
                source_entry_id=commit.blackboard_entry.entry_id,
                source_scope=commit.blackboard_entry.kind.value,
                visibility_scope=MailboxVisibilityScope.CONTROL_PRIVATE,
                delivery_mode=MailboxDeliveryMode.SUMMARY_PLUS_REF,
                severity="info",
                tags=("task_receipt", "receipt_type:claim_materialized"),
                payload=commit.receipt.to_payload(),
                metadata={"source_blackboard_id": commit.blackboard_entry.blackboard_id},
            )
        )

    async def execute_assignment(
        self,
        assignment: WorkerAssignment,
        *,
        request_permission: RequestPermission,
        execution_policy: WorkerExecutionPolicy | None = None,
    ) -> TeammateExecutionResult:
        decision = await request_permission(
            PermissionRequest(
                requester=assignment.worker_id,
                action="execute_worker_assignment",
                rationale=f"Execute teammate assignment {assignment.assignment_id}.",
                group_id=assignment.group_id,
                objective_id=assignment.objective_id,
                team_id=assignment.team_id,
                lane_id=assignment.lane_id,
                task_id=assignment.task_id,
                metadata={"role": assignment.role},
            )
        )
        if not decision.approved:
            context = await self.prepare_assignment(assignment)
            await self.runtime.update_task_status(
                task_id=assignment.task_id,
                status=TaskStatus.BLOCKED,
                actor_id=assignment.worker_id,
                blocked_by=("permission.denied",),
            )
            source_entry = await self.runtime.append_blackboard_entry(
                group_id=self.objective.group_id,
                kind=BlackboardKind.TEAM,
                entry_kind=BlackboardEntryKind.BLOCKER,
                author_id="supervisor.teammate_work_surface",
                lane_id=self.leader_round.lane_id,
                team_id=self.leader_round.team_id,
                task_id=assignment.task_id,
                summary=f"Permission denied for {assignment.assignment_id}.",
                payload={"reviewer": decision.reviewer, "reason": decision.reason},
            )
            if self.session_host is not None and context.slot_session_id is not None:
                await self.session_host.record_teammate_slot_state(
                    context.slot_session_id,
                    activation_epoch=context.activation_epoch,
                    current_task_id=None,
                    current_claim_session_id=context.claim_session_id,
                    last_claim_source=context.claim_source,
                    current_worker_session_id=None,
                    last_worker_session_id=None,
                    idle=True,
                    reason=f"Permission denied for {assignment.assignment_id}.",
                )
                await self.session_host.update_session(
                    context.slot_session_id,
                    current_directive_ids=(),
                )
            return TeammateExecutionResult(
                source_entry=source_entry,
                task_status=TaskStatus.BLOCKED,
            )

        context = await self.prepare_assignment(assignment)
        await self.runtime.update_task_status(
            task_id=assignment.task_id,
            status=TaskStatus.IN_PROGRESS,
            actor_id=assignment.worker_id,
        )
        record = await self.runtime.run_worker_assignment(
            assignment,
            policy=execution_policy,
        )
        session_snapshot = await self.finalize_assignment(
            assignment,
            context=context,
            record=record,
            persist=False,
        )
        authority_request = self.runtime._authority_request_from_worker_record(
            record=record,
            expected_assignment_id=assignment.assignment_id,
            expected_worker_id=assignment.worker_id,
            expected_task_id=assignment.task_id,
        )
        if authority_request is not None:
            commit = await self.runtime.commit_authority_request(
                objective_id=self.objective.objective_id,
                lane_id=self.leader_round.lane_id,
                team_id=self.leader_round.team_id,
                task_id=assignment.task_id,
                worker_id=assignment.worker_id,
                authority_request=authority_request,
                record=record,
                session_snapshot=session_snapshot,
            )
            envelope = await self._publish_authority_request_envelope(
                assignment=assignment,
                commit=commit,
            )
            authority_turn = await self._record_turn(
                worker_id=assignment.worker_id,
                actor_role=AgentTurnActorRole.TEAMMATE,
                turn_kind=AgentTurnKind.AUTHORITY_TRANSITION,
                input_summary=assignment.input_text,
                output_summary=commit.blackboard_entry.summary,
                assignment_id=assignment.assignment_id,
                response_id=record.response_id,
                status=AgentTurnStatus.FAILED
                if record.status != WorkerStatus.COMPLETED
                else AgentTurnStatus.COMPLETED,
                metadata={
                    "task_id": assignment.task_id,
                    "request_id": authority_request.request_id,
                    "decision": "request",
                    "claim_source": context.claim_source,
                    "claim_session_id": context.claim_session_id,
                },
            )
            await self._record_tool_invocation(
                turn_record=authority_turn,
                tool_kind=ToolInvocationKind.AUTHORITY_ACTION,
                tool_name="authority.request",
                input_summary=authority_request.request_id,
                output_summary=commit.blackboard_entry.summary,
                metadata={"task_id": assignment.task_id},
            )
            await self._record_artifact(
                turn_record=authority_turn,
                artifact_kind=ArtifactRefKind.HYDRATION_INPUT,
                uri=f"authority:request:{authority_request.request_id}",
                payload=authority_request.to_dict(),
                metadata={"task_id": assignment.task_id},
            )
            await self._record_turn(
                worker_id=assignment.worker_id,
                actor_role=AgentTurnActorRole.TEAMMATE,
                turn_kind=AgentTurnKind.WORKER_RESULT,
                input_summary=assignment.input_text,
                output_summary=record.output_text or record.error_text,
                assignment_id=assignment.assignment_id,
                response_id=record.response_id,
                status=AgentTurnStatus.COMPLETED
                if record.status == WorkerStatus.COMPLETED
                else AgentTurnStatus.FAILED,
                metadata={
                    "task_id": assignment.task_id,
                    "worker_status": record.status.value,
                    "claim_source": context.claim_source,
                    "claim_session_id": context.claim_session_id,
                },
            )
            return TeammateExecutionResult(
                record=record,
                envelope=envelope,
                source_entry=commit.blackboard_entry,
                claim_source=context.claim_source,
                claim_session_id=context.claim_session_id,
                task_status=commit.task.status,
            )
        commit = await self.runtime.commit_teammate_result(
            objective_id=self.objective.objective_id,
            lane_id=self.leader_round.lane_id,
            team_id=self.leader_round.team_id,
            task_id=assignment.task_id,
            worker_id=assignment.worker_id,
            record=record,
            session_snapshot=session_snapshot,
        )
        source_entry = commit.blackboard_entry
        envelope = await self._publish_result_envelope(
            assignment=assignment,
            source_entry=source_entry,
            record=record,
        )
        task_status = commit.task.status
        await self._record_turn(
            worker_id=assignment.worker_id,
            actor_role=AgentTurnActorRole.TEAMMATE,
            turn_kind=AgentTurnKind.WORKER_RESULT,
            input_summary=assignment.input_text,
            output_summary=record.output_text or record.error_text,
            assignment_id=assignment.assignment_id,
            response_id=record.response_id,
            status=AgentTurnStatus.COMPLETED
            if record.status == WorkerStatus.COMPLETED
            else AgentTurnStatus.FAILED,
            metadata={
                "task_id": assignment.task_id,
                "worker_status": record.status.value,
                "claim_source": context.claim_source,
                "claim_session_id": context.claim_session_id,
            },
        )
        return TeammateExecutionResult(
            record=record,
            envelope=envelope,
            source_entry=source_entry,
            claim_source=context.claim_source,
            claim_session_id=context.claim_session_id,
            task_status=task_status,
        )

    async def finalize_assignment(
        self,
        assignment: WorkerAssignment,
        *,
        context: TeammateAssignmentContext,
        record: WorkerRecord,
        persist: bool = True,
    ) -> Any | None:
        if self.session_host is None or context.slot_session_id is None:
            return None
        worker_session_id = record.session.session_id if record.session is not None else None
        projected = await self.session_host.project_teammate_slot_state(
            context.slot_session_id,
            session=None,
            activation_epoch=context.activation_epoch,
            current_task_id=None,
            current_claim_session_id=context.claim_session_id,
            last_claim_source=context.claim_source,
            current_worker_session_id=None,
            last_worker_session_id=worker_session_id,
            idle=True,
            reason=f"Teammate assignment {assignment.assignment_id} finished.",
        )
        projected = await self.session_host.project_session_update(
            context.slot_session_id,
            session=projected,
            current_directive_ids=(),
        )
        if persist:
            return await self.session_host.save_session(projected)
        return projected
