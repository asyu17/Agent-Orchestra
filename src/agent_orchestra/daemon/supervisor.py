from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone

from agent_orchestra.contracts.daemon import (
    AgentIncarnation,
    AgentIncarnationStatus,
    AgentSlot,
    AgentSlotStatus,
    SlotFailureClass,
    SlotHealthEvent,
)
from agent_orchestra.contracts.enums import WorkerStatus
from agent_orchestra.contracts.execution import WorkerFailureKind, WorkerRecord, WorkerSession
from agent_orchestra.daemon.slot_manager import SlotManager
from agent_orchestra.storage.base import DaemonTransactionStoreCommit, OrchestrationStore


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _failure_reason(record: WorkerRecord) -> str:
    if isinstance(record.error_text, str) and record.error_text.strip():
        return record.error_text.strip()
    protocol_failure_reason = _optional_string(record.metadata.get("protocol_failure_reason"))
    if protocol_failure_reason is not None:
        return protocol_failure_reason
    return f"worker_status={record.status.value}"


def _event_detail(record: WorkerRecord, failure_class: SlotFailureClass) -> str:
    reason = _failure_reason(record)
    return f"{failure_class.value}:{reason}"


class SlotSupervisor:
    def __init__(
        self,
        *,
        store: OrchestrationStore,
        slot_manager: SlotManager,
    ) -> None:
        self.store = store
        self.slot_manager = slot_manager

    def classify_record_failure(self, record: WorkerRecord) -> SlotFailureClass:
        if record.status in {WorkerStatus.COMPLETED, WorkerStatus.CANCELLED}:
            return SlotFailureClass.NORMAL_TERMINAL

        metadata = record.metadata
        failure_kind = _optional_string(metadata.get("failure_kind"))
        failure_tags = {
            str(item).strip()
            for item in metadata.get("failure_tags", ())
            if isinstance(item, str) and str(item).strip()
        }
        is_process_termination = _bool(
            metadata.get("failure_is_process_termination")
        ) or "process_termination" in failure_tags
        is_timeout = _bool(metadata.get("failure_is_timeout")) or _bool(
            metadata.get("supervisor_timeout_path")
        )
        is_protocol_contract = _bool(metadata.get("failure_is_protocol_contract"))

        if failure_kind == WorkerFailureKind.PROVIDER_UNAVAILABLE.value:
            if is_process_termination or is_timeout:
                return SlotFailureClass.RECOVERABLE_ABNORMAL
            return SlotFailureClass.EXTERNAL_DEGRADED

        if is_process_termination or is_timeout:
            return SlotFailureClass.RECOVERABLE_ABNORMAL

        if is_protocol_contract or "path_violation" in failure_tags:
            return SlotFailureClass.FATAL_CONFIGURATION

        if record.status == WorkerStatus.FAILED:
            return SlotFailureClass.RECOVERABLE_ABNORMAL

        return SlotFailureClass.NORMAL_TERMINAL

    async def maybe_replace_incarnation(
        self,
        *,
        worker_session: WorkerSession,
        record: WorkerRecord,
    ) -> AgentIncarnation | None:
        slot, incarnation = await self._resolve_slot_context(
            worker_session=worker_session,
            record=record,
        )
        if await self._fence_stale_record_if_needed(
            slot=slot,
            incarnation=incarnation,
            worker_session=worker_session,
            record=record,
        ):
            return None

        failure_class = self.classify_record_failure(record)
        if failure_class == SlotFailureClass.RECOVERABLE_ABNORMAL:
            return await self.replace_abnormal_incarnation(
                worker_session=worker_session,
                record=record,
            )

        await self._finalize_without_replacement(
            slot=slot,
            incarnation=incarnation,
            record=record,
            failure_class=failure_class,
        )
        return None

    async def replace_abnormal_incarnation(
        self,
        *,
        worker_session: WorkerSession,
        record: WorkerRecord,
    ) -> AgentIncarnation:
        slot, incarnation = await self._resolve_slot_context(
            worker_session=worker_session,
            record=record,
        )
        failure_class = self.classify_record_failure(record)
        if failure_class != SlotFailureClass.RECOVERABLE_ABNORMAL:
            raise ValueError(
                "replace_abnormal_incarnation only accepts recoverable abnormal failures."
            )

        now_iso = _now_iso()
        replacement_lease_id = (
            _optional_string(record.metadata.get("replacement_lease_id"))
            or f"{slot.slot_id}:lease:{slot.restart_count + 1}"
        )
        replacement = AgentIncarnation(
            slot_id=slot.slot_id,
            work_session_id=slot.work_session_id,
            runtime_generation_id=_optional_string(
                incarnation.metadata.get("runtime_generation_id")
            )
            or incarnation.runtime_generation_id,
            status=AgentIncarnationStatus.PENDING_RESTART,
            backend=worker_session.backend,
            transport_locator=(
                worker_session.transport_locator.to_dict()
                if worker_session.transport_locator is not None
                else dict(incarnation.transport_locator)
            ),
            lease_id=replacement_lease_id,
            restart_generation=slot.restart_count + 1,
            started_at=now_iso,
            metadata={
                **dict(incarnation.metadata),
                "worker_id": worker_session.worker_id,
                "worker_session_id": worker_session.session_id,
                "replaces_incarnation_id": incarnation.incarnation_id,
            },
        )
        finalized_incarnation = AgentIncarnation(
            incarnation_id=incarnation.incarnation_id,
            slot_id=incarnation.slot_id,
            work_session_id=incarnation.work_session_id,
            runtime_generation_id=incarnation.runtime_generation_id,
            status=AgentIncarnationStatus.FENCED,
            backend=incarnation.backend,
            transport_locator=dict(incarnation.transport_locator),
            lease_id=incarnation.lease_id,
            restart_generation=incarnation.restart_generation,
            started_at=incarnation.started_at,
            ended_at=record.ended_at or now_iso,
            terminal_failure_class=failure_class,
            terminal_reason=_failure_reason(record),
            metadata=dict(incarnation.metadata),
        )
        updated_slot = AgentSlot(
            slot_id=slot.slot_id,
            role=slot.role,
            work_session_id=slot.work_session_id,
            resident_team_shell_id=slot.resident_team_shell_id,
            status=AgentSlotStatus.BOOTING,
            desired_state=slot.desired_state,
            preferred_backend=slot.preferred_backend,
            preferred_transport_class=slot.preferred_transport_class,
            current_incarnation_id=replacement.incarnation_id,
            current_lease_id=replacement_lease_id,
            restart_count=slot.restart_count + 1,
            last_failure_class=failure_class,
            last_failure_reason=_failure_reason(record),
            created_at=slot.created_at,
            updated_at=now_iso,
            metadata=dict(slot.metadata),
        )
        event = SlotHealthEvent(
            slot_id=slot.slot_id,
            incarnation_id=incarnation.incarnation_id,
            work_session_id=slot.work_session_id,
            event_kind="incarnation_replaced",
            failure_class=failure_class,
            observed_at=now_iso,
            detail=_event_detail(record, failure_class),
            metadata={
                "replacement_incarnation_id": replacement.incarnation_id,
                "replacement_lease_id": replacement_lease_id,
            },
        )
        await self.store.commit_daemon_transaction(
            DaemonTransactionStoreCommit(
                agent_slots=(updated_slot,),
                agent_incarnations=(finalized_incarnation, replacement),
                slot_health_events=(event,),
            )
        )
        return replacement

    async def _resolve_slot_context(
        self,
        *,
        worker_session: WorkerSession,
        record: WorkerRecord,
    ) -> tuple[AgentSlot, AgentIncarnation]:
        metadata = worker_session.metadata if isinstance(worker_session.metadata, Mapping) else {}
        slot_id = _optional_string(record.metadata.get("slot_id")) or self.slot_manager.slot_id_for_worker(
            worker_id=worker_session.worker_id,
            metadata=metadata,
        )
        slot = await self.store.get_agent_slot(slot_id)
        if slot is None:
            return await self.slot_manager.materialize_slot_from_worker_session(worker_session)

        incarnation_id = (
            _optional_string(record.metadata.get("incarnation_id"))
            or _optional_string(metadata.get("incarnation_id"))
            or slot.current_incarnation_id
        )
        incarnation = (
            await self.store.get_agent_incarnation(incarnation_id)
            if incarnation_id is not None
            else None
        )
        if incarnation is None:
            return await self.slot_manager.materialize_slot_from_worker_session(worker_session)
        return slot, incarnation

    async def _fence_stale_record_if_needed(
        self,
        *,
        slot: AgentSlot,
        incarnation: AgentIncarnation,
        worker_session: WorkerSession,
        record: WorkerRecord,
    ) -> bool:
        record_incarnation_id = _optional_string(record.metadata.get("incarnation_id"))
        if (
            record_incarnation_id is None
            or slot.current_incarnation_id is None
            or record_incarnation_id == slot.current_incarnation_id
        ):
            return False
        stale = await self.store.get_agent_incarnation(record_incarnation_id)
        if stale is None:
            return False
        now_iso = _now_iso()
        fenced = AgentIncarnation(
            incarnation_id=stale.incarnation_id,
            slot_id=stale.slot_id,
            work_session_id=stale.work_session_id,
            runtime_generation_id=stale.runtime_generation_id,
            status=AgentIncarnationStatus.FENCED,
            backend=stale.backend,
            transport_locator=dict(stale.transport_locator),
            lease_id=stale.lease_id,
            restart_generation=stale.restart_generation,
            started_at=stale.started_at,
            ended_at=record.ended_at or now_iso,
            terminal_failure_class=self.classify_record_failure(record),
            terminal_reason=_failure_reason(record),
            metadata=dict(stale.metadata),
        )
        event = SlotHealthEvent(
            slot_id=slot.slot_id,
            incarnation_id=stale.incarnation_id,
            work_session_id=slot.work_session_id,
            event_kind="stale_incarnation_fenced",
            failure_class=self.classify_record_failure(record),
            observed_at=now_iso,
            detail=_event_detail(record, self.classify_record_failure(record)),
            metadata={
                "current_incarnation_id": slot.current_incarnation_id,
                "worker_session_id": worker_session.session_id,
            },
        )
        await self.store.commit_daemon_transaction(
            DaemonTransactionStoreCommit(
                agent_incarnations=(fenced,),
                slot_health_events=(event,),
            )
        )
        return True

    async def _finalize_without_replacement(
        self,
        *,
        slot: AgentSlot,
        incarnation: AgentIncarnation,
        record: WorkerRecord,
        failure_class: SlotFailureClass,
    ) -> None:
        now_iso = _now_iso()
        if failure_class == SlotFailureClass.NORMAL_TERMINAL:
            slot_status = AgentSlotStatus.QUIESCENT
            incarnation_status = AgentIncarnationStatus.TERMINAL
        elif failure_class == SlotFailureClass.EXTERNAL_DEGRADED:
            slot_status = AgentSlotStatus.WAITING_PROVIDER
            incarnation_status = AgentIncarnationStatus.FAILED
        else:
            slot_status = AgentSlotStatus.FAILED
            incarnation_status = AgentIncarnationStatus.FAILED

        updated_incarnation = AgentIncarnation(
            incarnation_id=incarnation.incarnation_id,
            slot_id=incarnation.slot_id,
            work_session_id=incarnation.work_session_id,
            runtime_generation_id=incarnation.runtime_generation_id,
            status=incarnation_status,
            backend=incarnation.backend,
            transport_locator=dict(incarnation.transport_locator),
            lease_id=incarnation.lease_id,
            restart_generation=incarnation.restart_generation,
            started_at=incarnation.started_at,
            ended_at=record.ended_at or now_iso,
            terminal_failure_class=(
                None
                if failure_class == SlotFailureClass.NORMAL_TERMINAL
                else failure_class
            ),
            terminal_reason=(
                None
                if failure_class == SlotFailureClass.NORMAL_TERMINAL
                else _failure_reason(record)
            ),
            metadata=dict(incarnation.metadata),
        )
        updated_slot = AgentSlot(
            slot_id=slot.slot_id,
            role=slot.role,
            work_session_id=slot.work_session_id,
            resident_team_shell_id=slot.resident_team_shell_id,
            status=slot_status,
            desired_state=slot.desired_state,
            preferred_backend=slot.preferred_backend,
            preferred_transport_class=slot.preferred_transport_class,
            current_incarnation_id=slot.current_incarnation_id,
            current_lease_id=slot.current_lease_id,
            restart_count=slot.restart_count,
            last_failure_class=(
                None
                if failure_class == SlotFailureClass.NORMAL_TERMINAL
                else failure_class
            ),
            last_failure_reason=(
                None
                if failure_class == SlotFailureClass.NORMAL_TERMINAL
                else _failure_reason(record)
            ),
            created_at=slot.created_at,
            updated_at=now_iso,
            metadata=dict(slot.metadata),
        )
        event = SlotHealthEvent(
            slot_id=slot.slot_id,
            incarnation_id=incarnation.incarnation_id,
            work_session_id=slot.work_session_id,
            event_kind=(
                "incarnation_terminal"
                if failure_class == SlotFailureClass.NORMAL_TERMINAL
                else "incarnation_degraded"
                if failure_class == SlotFailureClass.EXTERNAL_DEGRADED
                else "incarnation_failed"
            ),
            failure_class=(
                None
                if failure_class == SlotFailureClass.NORMAL_TERMINAL
                else failure_class
            ),
            observed_at=now_iso,
            detail=_event_detail(record, failure_class),
            metadata={},
        )
        await self.store.commit_daemon_transaction(
            DaemonTransactionStoreCommit(
                agent_slots=(updated_slot,),
                agent_incarnations=(updated_incarnation,),
                slot_health_events=(event,),
            )
        )
