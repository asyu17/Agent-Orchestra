from __future__ import annotations

import dataclasses
import datetime
import sys
from pathlib import Path
from unittest import IsolatedAsyncioTestCase

if not hasattr(datetime, "UTC"):
    from datetime import timezone

    datetime.UTC = timezone.utc

if sys.version_info < (3, 10):
    _original_dataclass = dataclasses.dataclass

    def _dataclass(_cls=None, **kwargs):
        kwargs.pop("slots", None)
        if _cls is None:
            return lambda cls: _original_dataclass(cls, **kwargs)
        return _original_dataclass(_cls, **kwargs)

    dataclasses.dataclass = _dataclass

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agent_orchestra.contracts.daemon import (
    AgentIncarnationStatus,
    AgentSlotStatus,
    SlotFailureClass,
)
from agent_orchestra.contracts.enums import WorkerStatus
from agent_orchestra.contracts.execution import (
    WorkerAssignment,
    WorkerHandle,
    WorkerRecord,
    WorkerSession,
    WorkerSessionStatus,
    WorkerTransportLocator,
)
from agent_orchestra.daemon.slot_manager import SlotManager
from agent_orchestra.daemon.supervisor import SlotSupervisor
from agent_orchestra.storage.in_memory import InMemoryOrchestrationStore


class SlotSupervisorTest(IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.store = InMemoryOrchestrationStore()
        self.slot_manager = SlotManager(store=self.store)
        self.supervisor = SlotSupervisor(store=self.store, slot_manager=self.slot_manager)

    async def test_materialize_slot_creates_stable_slot_and_first_incarnation(self) -> None:
        session = WorkerSession(
            session_id="worker-session-1",
            worker_id="leader:lane-a",
            assignment_id="assignment-1",
            backend="tmux",
            role="leader",
            status=WorkerSessionStatus.ACTIVE,
            supervisor_id="daemon-1",
            supervisor_lease_id="lease-1",
            transport_locator=WorkerTransportLocator(
                backend="tmux",
                session_name="ao-leader-a",
            ),
            metadata={
                "work_session_id": "worksession-1",
                "resident_team_shell_id": "shell-1",
            },
        )

        slot, incarnation = await self.slot_manager.materialize_slot_from_worker_session(session)

        self.assertEqual(slot.slot_id, "slot:leader:lane-a")
        self.assertEqual(slot.status, AgentSlotStatus.ACTIVE)
        self.assertEqual(slot.current_incarnation_id, incarnation.incarnation_id)
        self.assertEqual(incarnation.slot_id, slot.slot_id)
        self.assertEqual(incarnation.status, AgentIncarnationStatus.ACTIVE)

    async def test_classifies_process_termination_as_recoverable_abnormal(self) -> None:
        record = WorkerRecord(
            worker_id="leader:lane-a",
            assignment_id="assignment-1",
            backend="codex_cli",
            role="leader",
            status=WorkerStatus.FAILED,
            error_text="stream disconnected - retrying sampling request",
            metadata={
                "failure_kind": "provider_unavailable",
                "termination_signal_name": "SIGTERM",
                "failure_is_process_termination": True,
                "failure_tags": [
                    "process_termination",
                    "signal_sigterm",
                ],
            },
        )

        self.assertEqual(
            self.supervisor.classify_record_failure(record),
            SlotFailureClass.RECOVERABLE_ABNORMAL,
        )

    async def test_classifies_completed_record_as_normal_terminal(self) -> None:
        record = WorkerRecord(
            worker_id="leader:lane-a",
            assignment_id="assignment-1",
            backend="tmux",
            role="leader",
            status=WorkerStatus.COMPLETED,
        )

        self.assertEqual(
            self.supervisor.classify_record_failure(record),
            SlotFailureClass.NORMAL_TERMINAL,
        )

    async def test_replace_abnormal_incarnation_preserves_slot_identity(self) -> None:
        session = WorkerSession(
            session_id="worker-session-2",
            worker_id="teammate:slot:1",
            assignment_id="assignment-2",
            backend="subprocess",
            role="teammate",
            status=WorkerSessionStatus.ACTIVE,
            supervisor_id="daemon-1",
            supervisor_lease_id="lease-1",
            transport_locator=WorkerTransportLocator(
                backend="subprocess",
                pid=1234,
            ),
            metadata={
                "work_session_id": "worksession-2",
                "resident_team_shell_id": "shell-2",
            },
        )
        slot, incarnation = await self.slot_manager.materialize_slot_from_worker_session(session)

        record = WorkerRecord(
            worker_id=session.worker_id,
            assignment_id=session.assignment_id or "assignment-2",
            backend=session.backend,
            role=session.role,
            status=WorkerStatus.FAILED,
            error_text="transport died",
            metadata={
                "failure_is_process_termination": True,
                "failure_tags": ["process_termination"],
                "slot_id": slot.slot_id,
                "incarnation_id": incarnation.incarnation_id,
            },
        )

        replacement = await self.supervisor.replace_abnormal_incarnation(
            worker_session=session,
            record=record,
        )

        refreshed_slot = await self.store.get_agent_slot(slot.slot_id)
        assert refreshed_slot is not None
        self.assertEqual(refreshed_slot.slot_id, slot.slot_id)
        self.assertEqual(refreshed_slot.restart_count, 1)
        self.assertEqual(refreshed_slot.current_incarnation_id, replacement.incarnation_id)
        self.assertNotEqual(replacement.incarnation_id, incarnation.incarnation_id)
        self.assertEqual(replacement.status, AgentIncarnationStatus.PENDING_RESTART)

    async def test_does_not_replace_normal_terminal_record(self) -> None:
        session = WorkerSession(
            session_id="worker-session-3",
            worker_id="leader:lane-b",
            assignment_id="assignment-3",
            backend="tmux",
            role="leader",
            status=WorkerSessionStatus.COMPLETED,
            metadata={"work_session_id": "worksession-3"},
        )
        slot, incarnation = await self.slot_manager.materialize_slot_from_worker_session(session)
        record = WorkerRecord(
            worker_id=session.worker_id,
            assignment_id="assignment-3",
            backend=session.backend,
            role=session.role,
            status=WorkerStatus.COMPLETED,
            metadata={
                "slot_id": slot.slot_id,
                "incarnation_id": incarnation.incarnation_id,
            },
        )

        replacement = await self.supervisor.maybe_replace_incarnation(
            worker_session=session,
            record=record,
        )

        self.assertIsNone(replacement)


class SlotManagerIdentityTest(IsolatedAsyncioTestCase):
    async def test_assignment_metadata_slot_id_overrides_worker_id(self) -> None:
        store = InMemoryOrchestrationStore()
        manager = SlotManager(store=store)
        assignment = WorkerAssignment(
            assignment_id="assignment-4",
            worker_id="leader:lane-c",
            group_id="group-a",
            task_id="task-4",
            role="leader",
            backend="tmux",
            instructions="",
            input_text="",
            metadata={"slot_id": "slot:custom:lane-c"},
        )
        handle = WorkerHandle(
            worker_id=assignment.worker_id,
            role=assignment.role,
            backend=assignment.backend,
        )

        slot_id = manager.slot_id_for_assignment(assignment=assignment, handle=handle)

        self.assertEqual(slot_id, "slot:custom:lane-c")
