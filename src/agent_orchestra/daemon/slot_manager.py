from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone

from agent_orchestra.contracts.daemon import (
    AgentIncarnation,
    AgentIncarnationStatus,
    AgentSlot,
    AgentSlotStatus,
)
from agent_orchestra.contracts.execution import (
    WorkerAssignment,
    WorkerHandle,
    WorkerSession,
    WorkerSessionStatus,
)
from agent_orchestra.storage.base import DaemonTransactionStoreCommit, OrchestrationStore


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _mapping(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): item for key, item in value.items()}


def _slot_status_from_worker_session(status: WorkerSessionStatus) -> AgentSlotStatus:
    if status == WorkerSessionStatus.ACTIVE:
        return AgentSlotStatus.ACTIVE
    if status == WorkerSessionStatus.IDLE:
        return AgentSlotStatus.IDLE
    if status == WorkerSessionStatus.COMPLETED:
        return AgentSlotStatus.QUIESCENT
    if status in {WorkerSessionStatus.FAILED, WorkerSessionStatus.ABANDONED}:
        return AgentSlotStatus.FAILED
    return AgentSlotStatus.BOOTING


def _incarnation_status_from_worker_session(
    status: WorkerSessionStatus,
) -> AgentIncarnationStatus:
    if status == WorkerSessionStatus.ACTIVE:
        return AgentIncarnationStatus.ACTIVE
    if status == WorkerSessionStatus.IDLE:
        return AgentIncarnationStatus.QUIESCENT
    if status == WorkerSessionStatus.COMPLETED:
        return AgentIncarnationStatus.TERMINAL
    if status in {WorkerSessionStatus.FAILED, WorkerSessionStatus.ABANDONED}:
        return AgentIncarnationStatus.FAILED
    return AgentIncarnationStatus.BOOTING


class SlotManager:
    def __init__(self, *, store: OrchestrationStore) -> None:
        self.store = store

    def slot_id_for_worker(
        self,
        *,
        worker_id: str,
        metadata: Mapping[str, object] | None = None,
    ) -> str:
        if metadata is not None:
            explicit_slot_id = _optional_string(metadata.get("slot_id"))
            if explicit_slot_id is not None:
                return explicit_slot_id
        return f"slot:{worker_id}"

    def slot_id_for_assignment(
        self,
        *,
        assignment: WorkerAssignment,
        handle: WorkerHandle | None = None,
    ) -> str:
        if handle is not None:
            explicit_slot_id = _optional_string(handle.metadata.get("slot_id"))
            if explicit_slot_id is not None:
                return explicit_slot_id
        return self.slot_id_for_worker(
            worker_id=assignment.worker_id,
            metadata=assignment.metadata,
        )

    async def materialize_slot_from_worker_session(
        self,
        worker_session: WorkerSession,
    ) -> tuple[AgentSlot, AgentIncarnation]:
        metadata = _mapping(worker_session.metadata)
        slot_id = self.slot_id_for_worker(
            worker_id=worker_session.worker_id,
            metadata=metadata,
        )
        now_iso = _now_iso()
        existing_slot = await self.store.get_agent_slot(slot_id)

        current_incarnation: AgentIncarnation | None = None
        if (
            existing_slot is not None
            and existing_slot.current_incarnation_id is not None
        ):
            current_incarnation = await self.store.get_agent_incarnation(
                existing_slot.current_incarnation_id
            )

        desired_lease_id = (
            _optional_string(worker_session.supervisor_lease_id)
            or (
                current_incarnation.lease_id
                if current_incarnation is not None and current_incarnation.lease_id
                else None
            )
            or f"{slot_id}:lease:0"
        )
        incarnation_metadata = (
            dict(current_incarnation.metadata)
            if current_incarnation is not None
            else {}
        )
        incarnation_metadata.update(
            {
                "worker_id": worker_session.worker_id,
                "worker_session_id": worker_session.session_id,
            }
        )
        incarnation_metadata.update(metadata)
        incarnation_status = _incarnation_status_from_worker_session(worker_session.status)
        if (
            current_incarnation is not None
            and current_incarnation.lease_id == desired_lease_id
            and _optional_string(current_incarnation.metadata.get("worker_session_id"))
            == worker_session.session_id
        ):
            incarnation = AgentIncarnation(
                incarnation_id=current_incarnation.incarnation_id,
                slot_id=current_incarnation.slot_id,
                work_session_id=(
                    _optional_string(metadata.get("work_session_id"))
                    or current_incarnation.work_session_id
                ),
                runtime_generation_id=(
                    _optional_string(metadata.get("runtime_generation_id"))
                    or current_incarnation.runtime_generation_id
                ),
                status=incarnation_status,
                backend=worker_session.backend,
                transport_locator=(
                    worker_session.transport_locator.to_dict()
                    if worker_session.transport_locator is not None
                    else dict(current_incarnation.transport_locator)
                ),
                lease_id=desired_lease_id,
                restart_generation=current_incarnation.restart_generation,
                started_at=current_incarnation.started_at or worker_session.started_at or now_iso,
                ended_at=current_incarnation.ended_at,
                terminal_failure_class=current_incarnation.terminal_failure_class,
                terminal_reason=current_incarnation.terminal_reason,
                metadata=incarnation_metadata,
            )
        else:
            incarnation = AgentIncarnation(
                slot_id=slot_id,
                work_session_id=_optional_string(metadata.get("work_session_id")) or "",
                runtime_generation_id=_optional_string(metadata.get("runtime_generation_id")),
                status=incarnation_status,
                backend=worker_session.backend,
                transport_locator=(
                    worker_session.transport_locator.to_dict()
                    if worker_session.transport_locator is not None
                    else {}
                ),
                lease_id=desired_lease_id,
                restart_generation=existing_slot.restart_count if existing_slot is not None else 0,
                started_at=worker_session.started_at or now_iso,
                metadata=incarnation_metadata,
            )

        slot_metadata = dict(existing_slot.metadata) if existing_slot is not None else {}
        slot_metadata.update(
            {
                "worker_id": worker_session.worker_id,
                "worker_session_id": worker_session.session_id,
            }
        )
        slot_metadata.update(metadata)
        slot = AgentSlot(
            slot_id=slot_id,
            role=worker_session.role,
            work_session_id=_optional_string(metadata.get("work_session_id")) or "",
            resident_team_shell_id=_optional_string(metadata.get("resident_team_shell_id")),
            status=_slot_status_from_worker_session(worker_session.status),
            desired_state=(
                _optional_string(metadata.get("desired_state"))
                or (existing_slot.desired_state if existing_slot is not None else "active")
            ),
            preferred_backend=(
                _optional_string(metadata.get("preferred_backend"))
                or worker_session.backend
                or (existing_slot.preferred_backend if existing_slot is not None else None)
            ),
            preferred_transport_class=(
                _optional_string(metadata.get("preferred_transport_class"))
                or (
                    existing_slot.preferred_transport_class
                    if existing_slot is not None
                    else None
                )
            ),
            current_incarnation_id=incarnation.incarnation_id,
            current_lease_id=desired_lease_id,
            restart_count=existing_slot.restart_count if existing_slot is not None else 0,
            last_failure_class=(
                existing_slot.last_failure_class if existing_slot is not None else None
            ),
            last_failure_reason=(
                existing_slot.last_failure_reason if existing_slot is not None else None
            ),
            created_at=(
                existing_slot.created_at if existing_slot is not None and existing_slot.created_at else now_iso
            ),
            updated_at=now_iso,
            metadata=slot_metadata,
        )

        await self.store.commit_daemon_transaction(
            DaemonTransactionStoreCommit(
                agent_slots=(slot,),
                agent_incarnations=(incarnation,),
            )
        )
        return slot, incarnation
