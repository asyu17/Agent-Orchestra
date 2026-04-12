from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from unittest import IsolatedAsyncioTestCase

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agent_orchestra.bus.in_memory import InMemoryEventBus
from agent_orchestra.contracts.agent import AgentSession, SessionBinding
from agent_orchestra.contracts.enums import WorkerStatus
from agent_orchestra.contracts.execution import (
    ResidentCoordinatorPhase,
    WorkerAssignment,
    WorkerBackendCapabilities,
    WorkerExecutionPolicy,
    WorkerHandle,
    WorkerSession,
    WorkerSessionStatus,
    WorkerTransportLocator,
)
from agent_orchestra.contracts.session_continuity import ShellAttachDecisionMode
from agent_orchestra.contracts.worker_protocol import WorkerExecutionContract, WorkerLeasePolicy
from agent_orchestra.runtime.group_runtime import GroupRuntime
from agent_orchestra.runtime.session_host import InMemoryResidentSessionHost
from agent_orchestra.runtime.transport_adapter import DefaultTransportAdapter
from agent_orchestra.runtime.worker_supervisor import DefaultWorkerSupervisor
from agent_orchestra.storage.in_memory import InMemoryOrchestrationStore


class _TrackingSessionStore(InMemoryOrchestrationStore):
    def __init__(self) -> None:
        super().__init__()
        self.saved_worker_session_payloads: list[dict[str, object]] = []
        self.saved_protocol_bus_cursor_payloads: list[dict[str, object]] = []

    async def save_worker_session(self, session) -> None:
        await super().save_worker_session(session)
        self.saved_worker_session_payloads.append(session.to_dict())

    async def save_protocol_bus_cursor(
        self,
        *,
        stream: str,
        consumer: str,
        cursor: dict[str, str | None],
    ) -> None:
        await super().save_protocol_bus_cursor(stream=stream, consumer=consumer, cursor=cursor)
        self.saved_protocol_bus_cursor_payloads.append(
            {
                "stream": stream,
                "consumer": consumer,
                "cursor": dict(cursor),
            }
        )


class _RecordingSessionHost(InMemoryResidentSessionHost):
    def __init__(self) -> None:
        super().__init__()
        self.saved_sessions: list[AgentSession] = []
        self.marked_phases: list[tuple[str, ResidentCoordinatorPhase, str]] = []
        self.updated_phases: list[tuple[str, ResidentCoordinatorPhase, str]] = []
        self.reclaimed_sessions: list[str] = []

    async def save_session(self, session: AgentSession) -> AgentSession:
        self.saved_sessions.append(session)
        return await super().save_session(session)

    async def mark_phase(
        self,
        session_id: str,
        phase: ResidentCoordinatorPhase,
        *,
        reason: str = "",
    ) -> AgentSession:
        self.marked_phases.append((session_id, phase, reason))
        return await super().mark_phase(session_id, phase, reason=reason)

    async def reclaim_session(
        self,
        session_id: str,
        *,
        new_supervisor_id: str,
        new_lease_id: str,
        new_expires_at: str,
    ) -> AgentSession:
        self.reclaimed_sessions.append(session_id)
        return await super().reclaim_session(
            session_id,
            new_supervisor_id=new_supervisor_id,
            new_lease_id=new_lease_id,
            new_expires_at=new_expires_at,
        )

    async def update_session(
        self,
        session_id: str,
        *,
        phase: ResidentCoordinatorPhase | None = None,
        reason: str | None = None,
        mailbox_cursor: dict[str, object] | None = None,
        subscription_cursors: dict[str, dict[str, object]] | None = None,
        metadata: dict[str, object] | None = None,
        lease_id: str | None = None,
        lease_expires_at: str | None = None,
    ) -> AgentSession:
        if phase is not None:
            self.updated_phases.append((session_id, phase, reason or ""))
        return await super().update_session(
            session_id,
            phase=phase,
            reason=reason,
            mailbox_cursor=mailbox_cursor,
            subscription_cursors=subscription_cursors,
            metadata=metadata,
            lease_id=lease_id,
            lease_expires_at=lease_expires_at,
        )


class _RecordingTransportAdapter(DefaultTransportAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.snapshot_calls = 0
        self.binding_calls = 0
        self.handle_from_session_calls = 0
        self.locator_from_session_calls = 0

    def snapshot_handle(self, handle: WorkerHandle) -> dict[str, object]:
        self.snapshot_calls += 1
        return super().snapshot_handle(handle)

    def binding_from_handle(
        self,
        *,
        session_id: str,
        backend: str,
        handle: WorkerHandle | None,
        binding_type: str,
        supervisor_id: str | None,
        lease_id: str | None,
        lease_expires_at: str | None,
        metadata: dict[str, object] | None = None,
    ):
        self.binding_calls += 1
        return super().binding_from_handle(
            session_id=session_id,
            backend=backend,
            handle=handle,
            binding_type=binding_type,
            supervisor_id=supervisor_id,
            lease_id=lease_id,
            lease_expires_at=lease_expires_at,
            metadata=metadata,
        )

    def handle_from_worker_session(self, session: WorkerSession, *, backend: str):
        self.handle_from_session_calls += 1
        return super().handle_from_worker_session(session, backend=backend)

    def locator_from_worker_session(self, session: WorkerSession) -> WorkerTransportLocator | None:
        self.locator_from_session_calls += 1
        return super().locator_from_worker_session(session)


class _ProtocolResultBackend:
    def __init__(
        self,
        *,
        backend_name: str,
        root: Path,
        payload: dict[str, object],
    ) -> None:
        self.backend_name = backend_name
        self.root = root
        self.payload = payload
        self.cancel_count = 0

    def describe_capabilities(self) -> WorkerBackendCapabilities:
        return WorkerBackendCapabilities(
            supports_protocol_contract=True,
            supports_protocol_state=True,
            supports_protocol_final_report=True,
            supports_resume=True,
            supports_reactivate=False,
            supports_artifact_progress=True,
            supports_verification_in_working_dir=True,
        )

    async def launch(self, assignment: WorkerAssignment) -> WorkerHandle:
        result_file = self.root / f"{assignment.assignment_id}.result.json"
        protocol_state_file = self.root / f"{assignment.assignment_id}.protocol.json"
        protocol_payload = dict(self.payload.get("raw_payload", {}))
        if "protocol_events" not in protocol_payload:
            protocol_payload["protocol_events"] = []
        protocol_state_file.write_text(json.dumps(protocol_payload), encoding="utf-8")
        full_payload = {
            "worker_id": assignment.worker_id,
            "assignment_id": assignment.assignment_id,
            "status": self.payload.get("status", "completed"),
            "output_text": self.payload.get("output_text", ""),
            "error_text": self.payload.get("error_text", ""),
            "response_id": self.payload.get("response_id"),
            "usage": {},
            "raw_payload": dict(self.payload.get("raw_payload", {})),
        }
        result_file.write_text(json.dumps(full_payload), encoding="utf-8")
        return WorkerHandle(
            worker_id=assignment.worker_id,
            role=assignment.role,
            backend=self.backend_name,
            run_id=assignment.assignment_id,
            transport_ref=str(result_file),
            metadata={"protocol_state_file": str(protocol_state_file)},
        )

    async def cancel(self, handle: WorkerHandle) -> None:
        self.cancel_count += 1

    async def resume(self, handle: WorkerHandle, assignment: WorkerAssignment | None = None) -> WorkerHandle:
        return handle


class _LegacyProgressBackend:
    def __init__(self, *, backend_name: str, root: Path) -> None:
        self.backend_name = backend_name
        self.root = root
        self._tasks: list[asyncio.Task[None]] = []
        self.cancel_count = 0

    def describe_capabilities(self) -> WorkerBackendCapabilities:
        return WorkerBackendCapabilities(
            supports_protocol_contract=False,
            supports_protocol_state=False,
            supports_protocol_final_report=False,
            supports_resume=True,
            supports_reactivate=False,
            supports_artifact_progress=True,
            supports_verification_in_working_dir=True,
        )

    async def launch(self, assignment: WorkerAssignment) -> WorkerHandle:
        stdout_file = self.root / f"{assignment.assignment_id}.stdout.jsonl"
        stderr_file = self.root / f"{assignment.assignment_id}.stderr.log"
        result_file = self.root / f"{assignment.assignment_id}.result.json"
        stdout_file.write_text("", encoding="utf-8")
        stderr_file.write_text("", encoding="utf-8")

        handle = WorkerHandle(
            worker_id=assignment.worker_id,
            role=assignment.role,
            backend=self.backend_name,
            run_id=assignment.assignment_id,
            transport_ref=str(result_file),
            metadata={
                "stdout_file": str(stdout_file),
                "stderr_file": str(stderr_file),
            },
        )
        self._tasks.append(
            asyncio.create_task(
                self._play(
                    assignment=assignment,
                    stdout_file=stdout_file,
                    result_file=result_file,
                )
            )
        )
        return handle

    async def _play(
        self,
        *,
        assignment: WorkerAssignment,
        stdout_file: Path,
        result_file: Path,
    ) -> None:
        try:
            await asyncio.sleep(0.01)
            stdout_file.write_text('{"type":"progress"}\n', encoding="utf-8")
            await asyncio.sleep(0.01)
            result_file.write_text(
                json.dumps(
                    {
                        "worker_id": assignment.worker_id,
                        "assignment_id": assignment.assignment_id,
                        "status": "completed",
                        "output_text": "legacy backend done",
                        "error_text": "",
                        "response_id": None,
                        "usage": {},
                        "raw_payload": {"backend": self.backend_name},
                    }
                ),
                encoding="utf-8",
            )
        except asyncio.CancelledError:
            return

    async def cancel(self, handle: WorkerHandle) -> None:
        self.cancel_count += 1
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def resume(self, handle: WorkerHandle, assignment: WorkerAssignment | None = None) -> WorkerHandle:
        return handle


class _ProtocolClaimingResultOnlyBackend(_ProtocolResultBackend):
    async def launch(self, assignment: WorkerAssignment) -> WorkerHandle:
        handle = await super().launch(assignment)
        handle.metadata.pop("protocol_state_file", None)
        return handle


class _ProtocolProgressBackend:
    def __init__(
        self,
        *,
        backend_name: str,
        root: Path,
        events: list[dict[str, object]],
    ) -> None:
        self.backend_name = backend_name
        self.root = root
        self.events = list(events)
        self.cancel_count = 0
        self._tasks: list[asyncio.Task[None]] = []

    def describe_capabilities(self) -> WorkerBackendCapabilities:
        return WorkerBackendCapabilities(
            supports_protocol_contract=True,
            supports_protocol_state=True,
            supports_protocol_final_report=True,
            supports_resume=True,
            supports_reactivate=False,
            supports_artifact_progress=True,
            supports_verification_in_working_dir=True,
        )

    async def launch(self, assignment: WorkerAssignment) -> WorkerHandle:
        protocol_state_file = self.root / f"{assignment.assignment_id}.protocol.json"
        result_file = self.root / f"{assignment.assignment_id}.result.json"
        protocol_state_file.write_text(json.dumps({"protocol_events": []}), encoding="utf-8")
        handle = WorkerHandle(
            worker_id=assignment.worker_id,
            role=assignment.role,
            backend=self.backend_name,
            run_id=assignment.assignment_id,
            transport_ref=str(result_file),
            metadata={"protocol_state_file": str(protocol_state_file)},
        )
        self._tasks.append(
            asyncio.create_task(
                self._play(
                    assignment=assignment,
                    protocol_state_file=protocol_state_file,
                    result_file=result_file,
                )
            )
        )
        return handle

    async def _play(
        self,
        *,
        assignment: WorkerAssignment,
        protocol_state_file: Path,
        result_file: Path,
    ) -> None:
        state: dict[str, object] = {"protocol_events": []}
        try:
            for event in self.events:
                await asyncio.sleep(float(event.get("after", 0.0)))
                kind = str(event["kind"])
                if kind == "append_protocol_event":
                    protocol_events = list(state.get("protocol_events", []))
                    protocol_events.append(dict(event["event"]))
                    state["protocol_events"] = protocol_events
                    protocol_state_file.write_text(json.dumps(state), encoding="utf-8")
                    continue
                if kind == "write_final_report":
                    state["final_report"] = dict(event["report"])
                    protocol_state_file.write_text(json.dumps(state), encoding="utf-8")
                    continue
                if kind == "write_result":
                    payload = {
                        "worker_id": assignment.worker_id,
                        "assignment_id": assignment.assignment_id,
                        "status": event.get("status", "completed"),
                        "output_text": event.get("output_text", ""),
                        "error_text": event.get("error_text", ""),
                        "response_id": event.get("response_id"),
                        "usage": {},
                        "raw_payload": dict(event.get("raw_payload", {})),
                    }
                    result_file.write_text(json.dumps(payload), encoding="utf-8")
                    continue
                raise AssertionError(f"Unknown protocol progress event kind: {kind}")
        except asyncio.CancelledError:
            return

    async def cancel(self, handle: WorkerHandle) -> None:
        self.cancel_count += 1
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def resume(self, handle: WorkerHandle, assignment: WorkerAssignment | None = None) -> WorkerHandle:
        return handle


class _RecoverableProtocolBackend(_ProtocolProgressBackend):
    def __init__(self, *, backend_name: str, root: Path, events: list[dict[str, object]]) -> None:
        super().__init__(backend_name=backend_name, root=root, events=events)
        self.reattach_count = 0

    def describe_capabilities(self) -> WorkerBackendCapabilities:
        return WorkerBackendCapabilities(
            supports_protocol_contract=True,
            supports_protocol_state=True,
            supports_protocol_final_report=True,
            supports_resume=True,
            supports_reactivate=False,
            supports_reattach=True,
            supports_artifact_progress=True,
            supports_verification_in_working_dir=True,
        )

    async def reattach(self, locator, assignment: WorkerAssignment) -> WorkerHandle:
        self.reattach_count += 1
        result_file = locator.result_file or locator.metadata.get("transport_ref")
        protocol_state_file = locator.protocol_state_file or locator.metadata.get("protocol_state_file")
        return WorkerHandle(
            worker_id=assignment.worker_id,
            role=assignment.role,
            backend=self.backend_name,
            run_id=assignment.assignment_id,
            transport_ref=str(result_file),
            metadata={
                "protocol_state_file": str(protocol_state_file),
                "resume_supported": True,
                "reattach_supported": True,
                "reattach": True,
            },
        )


def _protocol_assignment(
    *,
    assignment_id: str,
    metadata: dict[str, object] | None = None,
) -> WorkerAssignment:
    return WorkerAssignment(
        assignment_id=assignment_id,
        worker_id=f"worker-{assignment_id}",
        group_id="group-a",
        team_id="team-a",
        task_id=f"task-{assignment_id}",
        role="teammate",
        backend="scripted",
        instructions="Handle protocol-aware work.",
        input_text="run",
        metadata=dict(metadata or {}),
    )


def _protocol_metadata(
    *,
    require_final_report: bool = True,
    require_verification_results: bool = False,
    required_verification_commands: tuple[str, ...] = (),
    completion_requires_verification_success: bool = False,
) -> dict[str, object]:
    return {
        "execution_contract": {
            "contract_id": "contract-v1",
            "mode": "code_edit",
            "allow_subdelegation": False,
            "require_final_report": require_final_report,
            "require_verification_results": require_verification_results,
            "required_verification_commands": list(required_verification_commands),
            "completion_requires_verification_success": completion_requires_verification_success,
            "required_artifact_kinds": [],
        },
        "lease_policy": {
            "accept_deadline_seconds": 1.0,
            "renewal_timeout_seconds": 30.0,
            "hard_deadline_seconds": 300.0,
            "renew_on_event_kinds": ("accepted", "checkpoint", "phase_changed", "verifying"),
        },
    }


class WorkerSupervisorProtocolTest(IsolatedAsyncioTestCase):
    async def test_supervisor_uses_transport_adapter_for_binding_and_snapshots(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            backend = _ProtocolResultBackend(
                backend_name="scripted",
                root=Path(tmpdir),
                payload={
                    "status": "completed",
                    "output_text": "done",
                    "raw_payload": {
                        "protocol_events": [
                            {
                                "event_id": "evt-adapter-accept",
                                "assignment_id": "assign-adapter",
                                "worker_id": "worker-assign-adapter",
                                "status": "accepted",
                                "phase": "accepted",
                            }
                        ],
                        "final_report": {
                            "assignment_id": "assign-adapter",
                            "worker_id": "worker-assign-adapter",
                            "terminal_status": "completed",
                            "summary": "done",
                        },
                    },
                },
            )
            store = InMemoryOrchestrationStore()
            adapter = _RecordingTransportAdapter()
            runtime = GroupRuntime(
                store=store,
                bus=InMemoryEventBus(),
                launch_backends={"scripted": backend},
                supervisor=DefaultWorkerSupervisor(
                    store=store,
                    launch_backends={"scripted": backend},
                    transport_adapter=adapter,
                ),
            )

            record = await runtime.run_worker_assignment(
                _protocol_assignment(
                    assignment_id="assign-adapter",
                    metadata=_protocol_metadata(),
                ),
                policy=WorkerExecutionPolicy(allow_relaunch=False, escalate_after_attempts=False),
            )

        self.assertEqual(record.status, WorkerStatus.COMPLETED)
        self.assertGreater(adapter.binding_calls, 0)
        self.assertGreater(adapter.snapshot_calls, 0)

    async def test_group_runtime_records_session_memory_for_protocol_worker_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            backend = _ProtocolResultBackend(
                backend_name="scripted",
                root=Path(tmpdir),
                payload={
                    "status": "completed",
                    "output_text": "worker finished protocol task",
                    "response_id": "resp-protocol-memory",
                    "raw_payload": {
                        "protocol_events": [
                            {
                                "event_id": "evt-memory-accept",
                                "assignment_id": "assign-memory",
                                "worker_id": "worker-assign-memory",
                                "status": "accepted",
                                "phase": "accepted",
                                "kind": "accepted",
                            },
                            {
                                "event_id": "evt-memory-1",
                                "assignment_id": "assign-memory",
                                "worker_id": "worker-assign-memory",
                                "status": "completed",
                                "phase": "verify",
                                "kind": "checkpoint",
                            }
                        ],
                        "final_report": {
                            "assignment_id": "assign-memory",
                            "worker_id": "worker-assign-memory",
                            "terminal_status": "completed",
                            "summary": "worker finished protocol task",
                            "artifact_refs": ["reports/final-memory.json"],
                            "verification_results": [
                                {
                                    "command": "pytest -q",
                                    "returncode": 0,
                                }
                            ],
                        },
                    },
                },
            )
            store = InMemoryOrchestrationStore()
            runtime = GroupRuntime(
                store=store,
                bus=InMemoryEventBus(),
                launch_backends={"scripted": backend},
                supervisor=DefaultWorkerSupervisor(
                    store=store,
                    launch_backends={"scripted": backend},
                ),
            )
            continuity = await runtime.new_session(
                group_id="group-a",
                objective_id="objective-memory",
                title="Protocol memory",
            )
            assignment = replace(
                _protocol_assignment(
                    assignment_id="assign-memory",
                    metadata=_protocol_metadata(),
                ),
                objective_id="objective-memory",
            )

            record = await runtime.run_worker_assignment(
                assignment,
                policy=WorkerExecutionPolicy(allow_relaunch=False, escalate_after_attempts=False),
            )

        self.assertEqual(record.status, WorkerStatus.COMPLETED)
        turn_records = await store.list_turn_records(continuity.work_session.work_session_id)
        artifact_refs = await store.list_artifact_refs(continuity.work_session.work_session_id)
        memory_items = await store.list_session_memory_items(continuity.work_session.work_session_id)
        inspection = await runtime.inspect_session(continuity.work_session.work_session_id)

        self.assertEqual(len(turn_records), 1)
        self.assertEqual(turn_records[0].response_id, "resp-protocol-memory")
        self.assertEqual(
            {artifact.artifact_kind.value for artifact in artifact_refs},
            {"final_report", "protocol_events", "protocol_state", "generated_file"},
        )
        self.assertEqual(len(memory_items), 1)
        self.assertEqual(memory_items[0].summary, "worker finished protocol task")
        self.assertEqual(len(inspection.hydration_bundles), 1)
        self.assertEqual(inspection.hydration_bundles[0].recent_turns[0].response_id, "resp-protocol-memory")

    async def test_supervisor_persists_active_session_before_entering_native_wait(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            backend = _ProtocolProgressBackend(
                backend_name="scripted",
                root=Path(tmpdir),
                events=[
                    {
                        "after": 0.04,
                        "kind": "append_protocol_event",
                        "event": {
                            "event_id": "evt-accept-active",
                            "assignment_id": "assign-active-persist",
                            "worker_id": "worker-assign-active-persist",
                            "status": "accepted",
                            "phase": "accepted",
                        },
                    },
                    {
                        "after": 0.04,
                        "kind": "write_final_report",
                        "report": {
                            "assignment_id": "assign-active-persist",
                            "worker_id": "worker-assign-active-persist",
                            "terminal_status": "completed",
                            "summary": "done",
                        },
                    },
                    {
                        "after": 0.02,
                        "kind": "write_result",
                        "status": "completed",
                        "output_text": "done",
                        "raw_payload": {"backend": "scripted"},
                    },
                ],
            )
            store = _TrackingSessionStore()
            session_host = _RecordingSessionHost()
            runtime = GroupRuntime(
                store=store,
                bus=InMemoryEventBus(),
                launch_backends={"scripted": backend},
                supervisor=DefaultWorkerSupervisor(
                    store=store,
                    launch_backends={"scripted": backend},
                    poll_interval_seconds=0.001,
                    default_timeout_seconds=0.2,
                    session_host=session_host,
                ),
            )

            assignment = _protocol_assignment(
                assignment_id="assign-active-persist",
                metadata={
                    **_protocol_metadata(),
                    "supervisor_id": "supervisor-active-persist",
                },
            )
            run_task = asyncio.create_task(
                runtime.run_worker_assignment(
                    assignment,
                    policy=WorkerExecutionPolicy(
                        idle_timeout_seconds=0.01,
                        hard_timeout_seconds=0.3,
                        keep_session_idle=False,
                        allow_relaunch=False,
                        escalate_after_attempts=False,
                    ),
                )
            )
            await asyncio.sleep(0.01)
            persisted = await store.get_worker_session("scripted:teammate:worker-assign-active-persist")
            self.assertIsNotNone(persisted)
            assert persisted is not None
            self.assertEqual(persisted.status.value, "active")
            self.assertEqual(persisted.supervisor_id, "supervisor-active-persist")
            self.assertIsNotNone(persisted.supervisor_lease_id)
            self.assertIsNotNone(persisted.supervisor_lease_expires_at)
            host_session = await session_host.load_session("scripted:teammate:worker-assign-active-persist")
            self.assertIsNotNone(host_session)
            assert host_session is not None
            self.assertEqual(
                host_session.current_worker_session_id,
                "scripted:teammate:worker-assign-active-persist",
            )
            self.assertEqual(
                host_session.last_worker_session_id,
                "scripted:teammate:worker-assign-active-persist",
            )
            truth = await session_host.read_worker_session_truth("scripted:teammate:worker-assign-active-persist")
            self.assertEqual(truth.bound_worker_session_id, "scripted:teammate:worker-assign-active-persist")
            self.assertEqual(truth.last_worker_session_id, "scripted:teammate:worker-assign-active-persist")
            record = await run_task
            self.assertTrue(session_host.saved_sessions)
            self.assertEqual(
                session_host.saved_sessions[-1].current_binding.backend,
                "scripted",
            )
            self.assertEqual(
                session_host.updated_phases[-1][1],
                ResidentCoordinatorPhase.QUIESCENT,
            )

        self.assertEqual(record.status, WorkerStatus.COMPLETED)
        self.assertEqual(record.metadata.get("protocol_wait_mode"), "native")

    async def test_supervisor_persists_active_session_into_slot_owned_resident_shell(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            backend = _ProtocolProgressBackend(
                backend_name="scripted",
                root=Path(tmpdir),
                events=[
                    {
                        "after": 0.04,
                        "kind": "append_protocol_event",
                        "event": {
                            "event_id": "evt-accept-slot-shell",
                            "assignment_id": "assign-slot-shell",
                            "worker_id": "worker-assign-slot-shell",
                            "status": "accepted",
                            "phase": "accepted",
                        },
                    },
                    {
                        "after": 0.04,
                        "kind": "write_final_report",
                        "report": {
                            "assignment_id": "assign-slot-shell",
                            "worker_id": "worker-assign-slot-shell",
                            "terminal_status": "completed",
                            "summary": "done",
                        },
                    },
                    {
                        "after": 0.02,
                        "kind": "write_result",
                        "status": "completed",
                        "output_text": "done",
                        "raw_payload": {"backend": "scripted"},
                    },
                ],
            )
            store = _TrackingSessionStore()
            session_host = _RecordingSessionHost()
            stable_slot_session_id = "objective-1:lane-runtime:teammate:1:resident"
            await session_host.register_session(
                AgentSession(
                    session_id=stable_slot_session_id,
                    agent_id="team-runtime:teammate:1",
                    role="teammate",
                    phase=ResidentCoordinatorPhase.IDLE,
                    objective_id="objective-1",
                    lane_id="lane-runtime",
                    team_id="team-runtime",
                    mailbox_cursor={
                        "stream": "mailbox",
                        "event_id": "env-slot-shell",
                        "last_envelope_id": "env-slot-shell",
                    },
                    subscription_cursors={"mailbox": {"event_id": "digest-slot-shell"}},
                    current_directive_ids=("directive-slot-shell",),
                    metadata={
                        "activation_epoch": 3,
                        "group_id": "group-a",
                        "work_session_id": "worksession-1",
                        "runtime_generation_id": "runtimegeneration-1",
                    },
                )
            )
            runtime = GroupRuntime(
                store=store,
                bus=InMemoryEventBus(),
                launch_backends={"scripted": backend},
                supervisor=DefaultWorkerSupervisor(
                    store=store,
                    launch_backends={"scripted": backend},
                    poll_interval_seconds=0.001,
                    default_timeout_seconds=0.2,
                    session_host=session_host,
                ),
            )
            assignment = replace(
                _protocol_assignment(
                    assignment_id="assign-slot-shell",
                    metadata={
                        **_protocol_metadata(),
                        "supervisor_id": "supervisor-slot-shell",
                        "worker_session_id": stable_slot_session_id,
                        "work_session_id": "worksession-1",
                        "runtime_generation_id": "runtimegeneration-1",
                    },
                ),
                objective_id="objective-1",
                lane_id="lane-runtime",
                team_id="team-runtime",
            )
            run_task = asyncio.create_task(
                runtime.run_worker_assignment(
                    assignment,
                    policy=WorkerExecutionPolicy(
                        idle_timeout_seconds=0.01,
                        hard_timeout_seconds=0.3,
                        keep_session_idle=False,
                        allow_relaunch=False,
                        escalate_after_attempts=False,
                    ),
                )
            )
            await asyncio.sleep(0.01)
            shells = await session_host.list_resident_team_shells(
                objective_id="objective-1",
                lane_id="lane-runtime",
                team_id="team-runtime",
            )
            self.assertEqual(len(shells), 1)
            shell = shells[0]
            self.assertEqual(shell.teammate_slot_session_ids, [stable_slot_session_id])
            decision = await session_host.find_preferred_attach_target(
                objective_id="objective-1",
                lane_id="lane-runtime",
                team_id="team-runtime",
            )
            self.assertEqual(decision.mode, ShellAttachDecisionMode.ATTACHED)
            self.assertIsNone(
                await session_host.load_session("scripted:teammate:worker-assign-slot-shell")
            )
            host_session = await session_host.load_session(stable_slot_session_id)
            self.assertIsNotNone(host_session)
            assert host_session is not None
            self.assertEqual(host_session.mailbox_cursor["last_envelope_id"], "env-slot-shell")
            self.assertEqual(
                host_session.subscription_cursors["mailbox"]["event_id"],
                "digest-slot-shell",
            )
            self.assertEqual(host_session.current_directive_ids, ("directive-slot-shell",))
            record = await run_task

        self.assertEqual(record.status, WorkerStatus.COMPLETED)

    async def test_supervisor_enforces_policy_carried_final_report_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            backend = _ProtocolResultBackend(
                backend_name="scripted",
                root=Path(tmpdir),
                payload={
                    "status": "completed",
                    "output_text": "done",
                    "raw_payload": {
                        "protocol_events": [
                            {
                                "event_id": "evt-1",
                                "assignment_id": "assign-policy-contract",
                                "worker_id": "worker-assign-policy-contract",
                                "status": "accepted",
                                "phase": "accepted",
                            }
                        ]
                    },
                },
            )
            store = InMemoryOrchestrationStore()
            runtime = GroupRuntime(
                store=store,
                bus=InMemoryEventBus(),
                launch_backends={"scripted": backend},
                supervisor=DefaultWorkerSupervisor(store=store, launch_backends={"scripted": backend}),
            )

            record = await runtime.run_worker_assignment(
                _protocol_assignment(assignment_id="assign-policy-contract"),
                policy=WorkerExecutionPolicy(
                    allow_relaunch=False,
                    escalate_after_attempts=False,
                    execution_contract=WorkerExecutionContract(
                        contract_id="policy-contract",
                        mode="code_edit",
                        require_final_report=True,
                    ),
                    lease_policy=WorkerLeasePolicy(
                        accept_deadline_seconds=1.0,
                        renewal_timeout_seconds=30.0,
                        hard_deadline_seconds=300.0,
                    ),
                ),
            )

        self.assertEqual(record.status, WorkerStatus.FAILED)
        self.assertEqual(record.metadata.get("supervision_mode"), "protocol_first")
        self.assertEqual(record.metadata.get("protocol_failure_reason"), "missing_final_report")

    async def test_supervisor_requires_verification_results_when_contract_demands_them(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            backend = _ProtocolResultBackend(
                backend_name="scripted",
                root=Path(tmpdir),
                payload={
                    "status": "completed",
                    "output_text": "done",
                    "raw_payload": {
                        "protocol_events": [
                            {
                                "event_id": "evt-verify-required",
                                "assignment_id": "assign-missing-verification-results",
                                "worker_id": "worker-assign-missing-verification-results",
                                "status": "accepted",
                                "phase": "accepted",
                            }
                        ],
                        "final_report": {
                            "assignment_id": "assign-missing-verification-results",
                            "worker_id": "worker-assign-missing-verification-results",
                            "terminal_status": "completed",
                            "summary": "done",
                        },
                    },
                },
            )
            store = InMemoryOrchestrationStore()
            runtime = GroupRuntime(
                store=store,
                bus=InMemoryEventBus(),
                launch_backends={"scripted": backend},
                supervisor=DefaultWorkerSupervisor(store=store, launch_backends={"scripted": backend}),
            )

            record = await runtime.run_worker_assignment(
                _protocol_assignment(
                    assignment_id="assign-missing-verification-results",
                    metadata=_protocol_metadata(require_verification_results=True),
                ),
                policy=WorkerExecutionPolicy(allow_relaunch=False, escalate_after_attempts=False),
            )

        self.assertEqual(record.status, WorkerStatus.FAILED)
        self.assertEqual(record.metadata.get("supervision_mode"), "protocol_first")
        self.assertEqual(record.metadata.get("protocol_failure_reason"), "missing_verification_results")

    async def test_supervisor_requires_completed_final_report_to_cover_required_verification_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            required_command = "uv run pytest tests/test_worker_supervisor_protocol.py -q"
            backend = _ProtocolResultBackend(
                backend_name="scripted",
                root=Path(tmpdir),
                payload={
                    "status": "completed",
                    "output_text": "done",
                    "raw_payload": {
                        "protocol_events": [
                            {
                                "event_id": "evt-required-verification",
                                "assignment_id": "assign-required-verification",
                                "worker_id": "worker-assign-required-verification",
                                "status": "accepted",
                                "phase": "accepted",
                            }
                        ],
                        "final_report": {
                            "assignment_id": "assign-required-verification",
                            "worker_id": "worker-assign-required-verification",
                            "terminal_status": "completed",
                            "summary": "done",
                            "verification_results": [
                                {
                                    "command": "python3 -m pytest tests/test_worker_supervisor_protocol.py -q",
                                    "returncode": 0,
                                    "stdout": "different command",
                                    "stderr": "",
                                }
                            ],
                        },
                    },
                },
            )
            store = InMemoryOrchestrationStore()
            runtime = GroupRuntime(
                store=store,
                bus=InMemoryEventBus(),
                launch_backends={"scripted": backend},
                supervisor=DefaultWorkerSupervisor(store=store, launch_backends={"scripted": backend}),
            )

            record = await runtime.run_worker_assignment(
                _protocol_assignment(
                    assignment_id="assign-required-verification",
                    metadata=_protocol_metadata(
                        require_verification_results=True,
                        required_verification_commands=(required_command,),
                        completion_requires_verification_success=True,
                    ),
                ),
                policy=WorkerExecutionPolicy(allow_relaunch=False, escalate_after_attempts=False),
            )

        self.assertEqual(record.status, WorkerStatus.FAILED)
        self.assertEqual(
            record.metadata.get("protocol_failure_reason"),
            "missing_required_verification_commands",
        )
        self.assertEqual(
            record.metadata.get("missing_required_verification_commands"),
            [required_command],
        )

    async def test_supervisor_accepts_requested_command_mapping_for_required_verification(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            required_command = "uv run pytest tests/test_worker_supervisor_protocol.py -q"
            backend = _ProtocolResultBackend(
                backend_name="scripted",
                root=Path(tmpdir),
                payload={
                    "status": "completed",
                    "output_text": "done",
                    "raw_payload": {
                        "protocol_events": [
                            {
                                "event_id": "evt-required-verification-map",
                                "assignment_id": "assign-required-verification-map",
                                "worker_id": "worker-assign-required-verification-map",
                                "status": "accepted",
                                "phase": "accepted",
                            }
                        ],
                        "final_report": {
                            "assignment_id": "assign-required-verification-map",
                            "worker_id": "worker-assign-required-verification-map",
                            "terminal_status": "completed",
                            "summary": "done",
                            "verification_results": [
                                {
                                    "requested_command": required_command,
                                    "command": ".venv/bin/pytest tests/test_worker_supervisor_protocol.py -q",
                                    "returncode": 0,
                                    "stdout": "mapped authoritative verification passed",
                                    "stderr": "",
                                }
                            ],
                        },
                    },
                },
            )
            store = InMemoryOrchestrationStore()
            runtime = GroupRuntime(
                store=store,
                bus=InMemoryEventBus(),
                launch_backends={"scripted": backend},
                supervisor=DefaultWorkerSupervisor(store=store, launch_backends={"scripted": backend}),
            )

            record = await runtime.run_worker_assignment(
                _protocol_assignment(
                    assignment_id="assign-required-verification-map",
                    metadata=_protocol_metadata(
                        require_verification_results=True,
                        required_verification_commands=(required_command,),
                        completion_requires_verification_success=True,
                    ),
                ),
                policy=WorkerExecutionPolicy(allow_relaunch=False, escalate_after_attempts=False),
            )

        self.assertEqual(record.status, WorkerStatus.COMPLETED)
        self.assertEqual(record.metadata.get("protocol_failure_reason"), None)
        self.assertEqual(
            record.metadata.get("final_report", {}).get("verification_results", [])[0]["requested_command"],
            required_command,
        )

    async def test_supervisor_prefers_last_successful_equivalent_required_verification_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            required_command = "uv run pytest tests/test_worker_supervisor_protocol.py -q"
            backend = _ProtocolResultBackend(
                backend_name="scripted",
                root=Path(tmpdir),
                payload={
                    "status": "completed",
                    "output_text": "done",
                    "raw_payload": {
                        "protocol_events": [
                            {
                                "event_id": "evt-required-verification-fallback",
                                "assignment_id": "assign-required-verification-fallback",
                                "worker_id": "worker-assign-required-verification-fallback",
                                "status": "accepted",
                                "phase": "accepted",
                            }
                        ],
                        "final_report": {
                            "assignment_id": "assign-required-verification-fallback",
                            "worker_id": "worker-assign-required-verification-fallback",
                            "terminal_status": "completed",
                            "summary": "done",
                            "verification_results": [
                                {
                                    "requested_command": required_command,
                                    "command": required_command,
                                    "returncode": 2,
                                    "stdout": "",
                                    "stderr": "uv missing",
                                },
                                {
                                    "requested_command": required_command,
                                    "command": (
                                        "UV_CACHE_DIR=/tmp/uv-cache uv run pytest "
                                        "tests/test_worker_supervisor_protocol.py -q"
                                    ),
                                    "returncode": 101,
                                    "stdout": "",
                                    "stderr": "resolver failed",
                                },
                                {
                                    "requested_command": required_command,
                                    "command": ".venv/bin/python -m pytest tests/test_worker_supervisor_protocol.py -q",
                                    "returncode": 0,
                                    "stdout": "fallback passed",
                                    "stderr": "",
                                },
                            ],
                        },
                    },
                },
            )
            store = InMemoryOrchestrationStore()
            supervisor = DefaultWorkerSupervisor(
                store=store,
                launch_backends={"scripted": backend},
            )
            record = await supervisor.run_assignment_with_policy(
                _protocol_assignment(
                    assignment_id="assign-required-verification-fallback",
                    metadata=_protocol_metadata(
                        require_verification_results=True,
                        required_verification_commands=(required_command,),
                        completion_requires_verification_success=True,
                    ),
                ),
                launch=backend.launch,
                resume=backend.resume,
                cancel=backend.cancel,
                policy=WorkerExecutionPolicy(allow_relaunch=False, escalate_after_attempts=False),
            )

        self.assertEqual(record.status, WorkerStatus.COMPLETED)
        self.assertEqual(record.metadata.get("protocol_failure_reason"), None)

    async def test_supervisor_uses_last_equivalent_required_verification_result_when_all_fail(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            required_command = "uv run pytest tests/test_worker_supervisor_protocol.py -q"
            backend = _ProtocolResultBackend(
                backend_name="scripted",
                root=Path(tmpdir),
                payload={
                    "status": "completed",
                    "output_text": "done",
                    "raw_payload": {
                        "protocol_events": [
                            {
                                "event_id": "evt-required-verification-all-fail",
                                "assignment_id": "assign-required-verification-all-fail",
                                "worker_id": "worker-assign-required-verification-all-fail",
                                "status": "accepted",
                                "phase": "accepted",
                            }
                        ],
                        "final_report": {
                            "assignment_id": "assign-required-verification-all-fail",
                            "worker_id": "worker-assign-required-verification-all-fail",
                            "terminal_status": "completed",
                            "summary": "done",
                            "verification_results": [
                                {
                                    "requested_command": required_command,
                                    "command": required_command,
                                    "returncode": 2,
                                    "stdout": "",
                                    "stderr": "uv missing",
                                },
                                {
                                    "requested_command": required_command,
                                    "command": ".venv/bin/python -m pytest tests/test_worker_supervisor_protocol.py -q",
                                    "returncode": 1,
                                    "stdout": "",
                                    "stderr": "tests failed",
                                },
                            ],
                        },
                    },
                },
            )
            store = InMemoryOrchestrationStore()
            supervisor = DefaultWorkerSupervisor(
                store=store,
                launch_backends={"scripted": backend},
            )
            record = await supervisor.run_assignment_with_policy(
                _protocol_assignment(
                    assignment_id="assign-required-verification-all-fail",
                    metadata=_protocol_metadata(
                        require_verification_results=True,
                        required_verification_commands=(required_command,),
                        completion_requires_verification_success=True,
                    ),
                ),
                launch=backend.launch,
                resume=backend.resume,
                cancel=backend.cancel,
                policy=WorkerExecutionPolicy(allow_relaunch=False, escalate_after_attempts=False),
            )

        self.assertEqual(record.status, WorkerStatus.FAILED)
        self.assertEqual(
            record.metadata.get("protocol_failure_reason"),
            "required_verification_commands_failed",
        )
        self.assertEqual(
            record.metadata.get("failed_required_verification_commands"),
            [required_command],
        )

    async def test_supervisor_requires_accept_before_accept_deadline(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            progress_timestamp = "2026-04-06T00:00:01+00:00"
            backend = _ProtocolResultBackend(
                backend_name="scripted",
                root=Path(tmpdir),
                payload={
                    "status": "completed",
                    "output_text": "done",
                    "raw_payload": {
                        "protocol_events": [
                            {
                                "event_id": "evt-1",
                                "assignment_id": "assign-missing-accept",
                                "worker_id": "worker-assign-missing-accept",
                                "status": "running",
                                "phase": "running",
                                "timestamp": progress_timestamp,
                            }
                        ],
                        "final_report": {
                            "assignment_id": "assign-missing-accept",
                            "worker_id": "worker-assign-missing-accept",
                            "terminal_status": "completed",
                            "summary": "done",
                        },
                    },
                },
            )
            store = InMemoryOrchestrationStore()
            runtime = GroupRuntime(
                store=store,
                bus=InMemoryEventBus(),
                launch_backends={"scripted": backend},
                supervisor=DefaultWorkerSupervisor(store=store, launch_backends={"scripted": backend}),
            )

            record = await runtime.run_worker_assignment(
                _protocol_assignment(
                    assignment_id="assign-missing-accept",
                    metadata=_protocol_metadata(),
                ),
                policy=WorkerExecutionPolicy(allow_relaunch=False, escalate_after_attempts=False),
            )

        self.assertEqual(record.status, WorkerStatus.FAILED)
        self.assertEqual(record.metadata.get("supervision_mode"), "protocol_first")
        self.assertEqual(record.metadata.get("protocol_failure_reason"), "missing_accept")
        self.assertEqual(record.metadata.get("last_protocol_progress_at"), progress_timestamp)
        self.assertEqual(record.metadata.get("supervisor_timeout_path"), False)
        self.assertIn("protocol_contract_failure", record.metadata.get("failure_tags", ()))
        self.assertNotIn("timeout_failure", record.metadata.get("failure_tags", ()))
        self.assertNotIn("process_termination", record.metadata.get("failure_tags", ()))

    async def test_supervisor_timeout_failure_tracks_cancel_and_protocol_progress(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            progress_timestamp = "2026-04-06T00:00:12+00:00"
            backend = _ProtocolProgressBackend(
                backend_name="scripted",
                root=Path(tmpdir),
                events=[
                    {
                        "after": 0.005,
                        "kind": "append_protocol_event",
                        "event": {
                            "event_id": "evt-timeout-accept",
                            "assignment_id": "assign-timeout-progress",
                            "worker_id": "worker-assign-timeout-progress",
                            "status": "accepted",
                            "phase": "accepted",
                            "kind": "accepted",
                            "timestamp": progress_timestamp,
                        },
                    }
                ],
            )
            store = InMemoryOrchestrationStore()
            runtime = GroupRuntime(
                store=store,
                bus=InMemoryEventBus(),
                launch_backends={"scripted": backend},
                supervisor=DefaultWorkerSupervisor(
                    store=store,
                    launch_backends={"scripted": backend},
                    poll_interval_seconds=0.001,
                    default_timeout_seconds=0.3,
                ),
            )

            record = await runtime.run_worker_assignment(
                _protocol_assignment(
                    assignment_id="assign-timeout-progress",
                    metadata={
                        **_protocol_metadata(),
                        "lease_policy": {
                            "accept_deadline_seconds": 0.03,
                            "renewal_timeout_seconds": 0.03,
                            "hard_deadline_seconds": 0.3,
                            "renew_on_event_kinds": ("accepted",),
                        },
                    },
                ),
                policy=WorkerExecutionPolicy(
                    max_attempts=1,
                    resume_on_timeout=False,
                    allow_relaunch=False,
                    escalate_after_attempts=True,
                    hard_timeout_seconds=0.3,
                ),
            )

        self.assertEqual(record.status, WorkerStatus.FAILED)
        self.assertEqual(record.metadata.get("timeout_kind"), "renewal")
        self.assertEqual(record.metadata.get("protocol_failure_reason"), "lease_renewal_timeout")
        self.assertEqual(record.metadata.get("supervisor_timeout_path"), True)
        self.assertEqual(record.metadata.get("backend_cancel_invoked"), True)
        self.assertEqual(record.metadata.get("last_protocol_progress_at"), progress_timestamp)
        self.assertIn("timeout_failure", record.metadata.get("failure_tags", ()))
        self.assertIn("protocol_contract_failure", record.metadata.get("failure_tags", ()))
        self.assertEqual(backend.cancel_count, 1)

    async def test_supervisor_uses_live_protocol_wait_path_for_accept_deadline(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            backend = _ProtocolProgressBackend(
                backend_name="scripted",
                root=Path(tmpdir),
                events=[],
            )
            store = InMemoryOrchestrationStore()
            runtime = GroupRuntime(
                store=store,
                bus=InMemoryEventBus(),
                launch_backends={"scripted": backend},
                supervisor=DefaultWorkerSupervisor(
                    store=store,
                    launch_backends={"scripted": backend},
                    poll_interval_seconds=0.001,
                    default_timeout_seconds=0.1,
                ),
            )

            record = await runtime.run_worker_assignment(
                _protocol_assignment(
                    assignment_id="assign-native-accept-deadline",
                    metadata={
                        **_protocol_metadata(),
                        "lease_policy": {
                            "accept_deadline_seconds": 0.02,
                            "renewal_timeout_seconds": 0.05,
                            "hard_deadline_seconds": 0.2,
                            "renew_on_event_kinds": ("accepted", "checkpoint", "phase_changed", "verifying"),
                        },
                    },
                ),
                policy=WorkerExecutionPolicy(
                    idle_timeout_seconds=0.05,
                    hard_timeout_seconds=0.2,
                    allow_relaunch=False,
                    escalate_after_attempts=False,
                ),
            )

        self.assertEqual(record.status, WorkerStatus.FAILED)
        self.assertEqual(record.metadata.get("supervision_mode"), "protocol_first")
        self.assertEqual(record.metadata.get("protocol_wait_mode"), "native")
        self.assertEqual(record.metadata.get("protocol_failure_reason"), "accept_deadline_exceeded")
        self.assertEqual(record.metadata.get("timeout_kind"), "accept")

    async def test_supervisor_uses_policy_carried_lease_for_native_protocol_wait(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            backend = _ProtocolProgressBackend(
                backend_name="scripted",
                root=Path(tmpdir),
                events=[],
            )
            store = InMemoryOrchestrationStore()
            runtime = GroupRuntime(
                store=store,
                bus=InMemoryEventBus(),
                launch_backends={"scripted": backend},
                supervisor=DefaultWorkerSupervisor(
                    store=store,
                    launch_backends={"scripted": backend},
                    poll_interval_seconds=0.001,
                    default_timeout_seconds=0.1,
                ),
            )

            record = await runtime.run_worker_assignment(
                _protocol_assignment(assignment_id="assign-policy-native-wait"),
                policy=WorkerExecutionPolicy(
                    idle_timeout_seconds=0.05,
                    hard_timeout_seconds=0.2,
                    allow_relaunch=False,
                    escalate_after_attempts=False,
                    execution_contract=WorkerExecutionContract(
                        contract_id="policy-contract",
                        mode="code_edit",
                        require_final_report=True,
                    ),
                    lease_policy=WorkerLeasePolicy(
                        accept_deadline_seconds=0.02,
                        renewal_timeout_seconds=0.05,
                        hard_deadline_seconds=0.2,
                    ),
                ),
            )

        self.assertEqual(record.status, WorkerStatus.FAILED)
        self.assertEqual(record.metadata.get("supervision_mode"), "protocol_first")
        self.assertEqual(record.metadata.get("protocol_wait_mode"), "native")
        self.assertEqual(record.metadata.get("protocol_failure_reason"), "accept_deadline_exceeded")
        self.assertEqual(record.metadata.get("timeout_kind"), "accept")

    async def test_supervisor_renews_lease_on_protocol_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            backend = _ProtocolResultBackend(
                backend_name="scripted",
                root=Path(tmpdir),
                payload={
                    "status": "completed",
                    "output_text": "done",
                    "raw_payload": {
                        "protocol_events": [
                            {
                                "event_id": "evt-1",
                                "assignment_id": "assign-lease",
                                "worker_id": "worker-assign-lease",
                                "status": "accepted",
                                "phase": "accepted",
                            },
                            {
                                "event_id": "evt-2",
                                "assignment_id": "assign-lease",
                                "worker_id": "worker-assign-lease",
                                "status": "running",
                                "phase": "checkpoint",
                            },
                            {
                                "event_id": "evt-3",
                                "assignment_id": "assign-lease",
                                "worker_id": "worker-assign-lease",
                                "status": "completed",
                                "phase": "terminal_report_announced",
                            },
                        ],
                        "final_report": {
                            "assignment_id": "assign-lease",
                            "worker_id": "worker-assign-lease",
                            "terminal_status": "completed",
                            "summary": "done",
                        },
                    },
                },
            )
            store = InMemoryOrchestrationStore()
            runtime = GroupRuntime(
                store=store,
                bus=InMemoryEventBus(),
                launch_backends={"scripted": backend},
                supervisor=DefaultWorkerSupervisor(store=store, launch_backends={"scripted": backend}),
            )

            record = await runtime.run_worker_assignment(
                _protocol_assignment(
                    assignment_id="assign-lease",
                    metadata=_protocol_metadata(),
                ),
                policy=WorkerExecutionPolicy(allow_relaunch=False, escalate_after_attempts=False),
            )

        self.assertEqual(record.status, WorkerStatus.COMPLETED)
        self.assertEqual(record.metadata.get("supervision_mode"), "protocol_first")
        self.assertEqual(record.metadata.get("lifecycle_status"), "completed")
        self.assertEqual(record.metadata.get("lease", {}).get("status"), "closed")
        self.assertEqual(record.metadata.get("final_report", {}).get("terminal_status"), "completed")

    async def test_supervisor_renews_live_lease_without_artifact_progress(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            backend = _ProtocolProgressBackend(
                backend_name="scripted",
                root=Path(tmpdir),
                events=[
                    {
                        "after": 0.005,
                        "kind": "append_protocol_event",
                        "event": {
                            "event_id": "evt-accept",
                            "assignment_id": "assign-live-lease",
                            "worker_id": "worker-assign-live-lease",
                            "status": "accepted",
                            "phase": "accepted",
                        },
                    },
                    {
                        "after": 0.015,
                        "kind": "append_protocol_event",
                        "event": {
                            "event_id": "evt-waiting",
                            "assignment_id": "assign-live-lease",
                            "worker_id": "worker-assign-live-lease",
                            "status": "waiting_on_subtasks",
                            "phase": "waiting_on_subtasks",
                        },
                    },
                    {
                        "after": 0.015,
                        "kind": "append_protocol_event",
                        "event": {
                            "event_id": "evt-verifying",
                            "assignment_id": "assign-live-lease",
                            "worker_id": "worker-assign-live-lease",
                            "status": "verifying",
                            "phase": "verifying",
                        },
                    },
                    {
                        "after": 0.01,
                        "kind": "write_final_report",
                        "report": {
                            "assignment_id": "assign-live-lease",
                            "worker_id": "worker-assign-live-lease",
                            "terminal_status": "completed",
                            "summary": "done",
                        },
                    },
                    {
                        "after": 0.005,
                        "kind": "write_result",
                        "status": "completed",
                        "output_text": "done",
                        "raw_payload": {"backend": "scripted"},
                    },
                ],
            )
            store = InMemoryOrchestrationStore()
            runtime = GroupRuntime(
                store=store,
                bus=InMemoryEventBus(),
                launch_backends={"scripted": backend},
                supervisor=DefaultWorkerSupervisor(
                    store=store,
                    launch_backends={"scripted": backend},
                    poll_interval_seconds=0.001,
                    default_timeout_seconds=0.1,
                ),
            )

            record = await runtime.run_worker_assignment(
                _protocol_assignment(
                    assignment_id="assign-live-lease",
                    metadata={
                        **_protocol_metadata(),
                        "lease_policy": {
                            "accept_deadline_seconds": 0.02,
                            "renewal_timeout_seconds": 0.03,
                            "hard_deadline_seconds": 0.2,
                            "renew_on_event_kinds": ("accepted", "waiting_on_subtasks", "verifying"),
                        },
                    },
                ),
                policy=WorkerExecutionPolicy(
                    idle_timeout_seconds=0.01,
                    hard_timeout_seconds=0.2,
                    allow_relaunch=False,
                    escalate_after_attempts=False,
                ),
            )

        self.assertEqual(record.status, WorkerStatus.COMPLETED)
        self.assertEqual(record.metadata.get("supervision_mode"), "protocol_first")
        self.assertEqual(record.metadata.get("protocol_wait_mode"), "native")
        self.assertEqual(record.metadata.get("lifecycle_status"), "completed")
        self.assertEqual(record.metadata.get("final_report", {}).get("terminal_status"), "completed")
        self.assertEqual(record.metadata.get("protocol_event_count"), 3)

    async def test_supervisor_renews_durable_lease_and_persists_protocol_cursor_on_new_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            backend = _ProtocolProgressBackend(
                backend_name="scripted",
                root=Path(tmpdir),
                events=[
                    {
                        "after": 0.005,
                        "kind": "append_protocol_event",
                        "event": {
                            "event_id": "evt-accept-cursor",
                            "assignment_id": "assign-cursor-renew",
                            "worker_id": "worker-assign-cursor-renew",
                            "status": "accepted",
                            "phase": "accepted",
                        },
                    },
                    {
                        "after": 0.01,
                        "kind": "append_protocol_event",
                        "event": {
                            "event_id": "evt-checkpoint-cursor",
                            "assignment_id": "assign-cursor-renew",
                            "worker_id": "worker-assign-cursor-renew",
                            "status": "running",
                            "phase": "checkpoint",
                        },
                    },
                    {
                        "after": 0.01,
                        "kind": "append_protocol_event",
                        "event": {
                            "event_id": "evt-verifying-cursor",
                            "assignment_id": "assign-cursor-renew",
                            "worker_id": "worker-assign-cursor-renew",
                            "status": "verifying",
                            "phase": "verifying",
                        },
                    },
                    {
                        "after": 0.01,
                        "kind": "write_final_report",
                        "report": {
                            "assignment_id": "assign-cursor-renew",
                            "worker_id": "worker-assign-cursor-renew",
                            "terminal_status": "completed",
                            "summary": "done",
                        },
                    },
                    {
                        "after": 0.01,
                        "kind": "write_result",
                        "status": "completed",
                        "output_text": "done",
                        "raw_payload": {"backend": "scripted"},
                    },
                ],
            )
            store = _TrackingSessionStore()
            runtime = GroupRuntime(
                store=store,
                bus=InMemoryEventBus(),
                launch_backends={"scripted": backend},
                supervisor=DefaultWorkerSupervisor(
                    store=store,
                    launch_backends={"scripted": backend},
                    poll_interval_seconds=0.001,
                    default_timeout_seconds=0.2,
                ),
            )

            record = await runtime.run_worker_assignment(
                _protocol_assignment(
                    assignment_id="assign-cursor-renew",
                    metadata={
                        **_protocol_metadata(),
                        "supervisor_id": "supervisor-cursor-renew",
                    },
                ),
                policy=WorkerExecutionPolicy(
                    idle_timeout_seconds=0.01,
                    hard_timeout_seconds=0.3,
                    keep_session_idle=False,
                    allow_relaunch=False,
                    escalate_after_attempts=False,
                ),
            )

        self.assertEqual(record.status, WorkerStatus.COMPLETED)
        active_snapshots = [
            payload
            for payload in store.saved_worker_session_payloads
            if payload.get("assignment_id") == "assign-cursor-renew" and payload.get("status") == "active"
        ]
        self.assertGreaterEqual(len(active_snapshots), 2)
        self.assertTrue(
            any(
                payload.get("protocol_cursor", {}).get("event_id") == "evt-verifying-cursor"
                for payload in active_snapshots
            )
        )
        self.assertTrue(
            any(
                payload.get("supervisor_lease_expires_at")
                for payload in active_snapshots
            )
        )
        lifecycle_cursor_updates = [
            payload
            for payload in store.saved_protocol_bus_cursor_payloads
            if payload.get("stream") == "lifecycle" and payload.get("consumer") == "supervisor-cursor-renew"
        ]
        self.assertGreaterEqual(len(lifecycle_cursor_updates), 3)
        self.assertEqual(
            lifecycle_cursor_updates[-1].get("cursor", {}).get("event_id"),
            "evt-verifying-cursor",
        )

    async def test_supervisor_recovers_reclaimable_active_session_via_reattach(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            assignment = _protocol_assignment(
                assignment_id="assign-recover",
                metadata={
                    **_protocol_metadata(),
                    "supervisor_id": "supervisor-old",
                },
            )
            backend = _RecoverableProtocolBackend(
                backend_name="scripted",
                root=root,
                events=[
                    {
                        "after": 0.005,
                        "kind": "append_protocol_event",
                        "event": {
                            "event_id": "evt-recover-accept",
                            "assignment_id": assignment.assignment_id,
                            "worker_id": assignment.worker_id,
                            "status": "accepted",
                            "phase": "accepted",
                        },
                    },
                    {
                        "after": 0.01,
                        "kind": "append_protocol_event",
                        "event": {
                            "event_id": "evt-recover-checkpoint",
                            "assignment_id": assignment.assignment_id,
                            "worker_id": assignment.worker_id,
                            "status": "running",
                            "phase": "checkpoint",
                        },
                    },
                    {
                        "after": 0.01,
                        "kind": "write_final_report",
                        "report": {
                            "assignment_id": assignment.assignment_id,
                            "worker_id": assignment.worker_id,
                            "terminal_status": "completed",
                            "summary": "done",
                        },
                    },
                    {
                        "after": 0.01,
                        "kind": "write_result",
                        "status": "completed",
                        "output_text": "done",
                        "raw_payload": {"backend": "scripted"},
                    },
                ],
            )
            protocol_state_file = root / f"{assignment.assignment_id}.protocol.json"
            result_file = root / f"{assignment.assignment_id}.result.json"
            protocol_state_file.write_text(json.dumps({"protocol_events": []}), encoding="utf-8")
            asyncio.create_task(
                backend._play(
                    assignment=assignment,
                    protocol_state_file=protocol_state_file,
                    result_file=result_file,
                )
            )

            store = _TrackingSessionStore()
            await store.save_worker_session(
                WorkerSession(
                    session_id="scripted:teammate:worker-assign-recover",
                    worker_id=assignment.worker_id,
                    assignment_id=assignment.assignment_id,
                    backend="scripted",
                    role="teammate",
                    status=WorkerSessionStatus.ACTIVE,
                    lifecycle_status="running",
                    supervisor_id="supervisor-old",
                    supervisor_lease_id="lease-old",
                    supervisor_lease_expires_at="2026-04-05T00:00:30+00:00",
                    handle_snapshot={
                        "backend": "scripted",
                        "transport_ref": str(result_file),
                        "metadata": {
                            "protocol_state_file": str(protocol_state_file),
                            "reattach_supported": True,
                        },
                    },
                    metadata={
                        "group_id": assignment.group_id,
                        "task_id": assignment.task_id,
                        "team_id": assignment.team_id,
                        "execution_contract": dict(assignment.metadata.get("execution_contract", {})),
                        "lease_policy": dict(assignment.metadata.get("lease_policy", {})),
                    },
                )
            )

            session_host = _RecordingSessionHost()
            await session_host.register_session(
                AgentSession(
                    session_id="scripted:teammate:worker-assign-recover",
                    agent_id=assignment.worker_id,
                    role="teammate",
                    phase=ResidentCoordinatorPhase.WAITING_FOR_MAILBOX,
                    objective_id=assignment.objective_id,
                    lane_id=assignment.lane_id,
                    team_id=assignment.team_id,
                    mailbox_cursor={
                        "stream": "mailbox",
                        "event_id": "env-host-recover",
                        "last_envelope_id": "env-host-recover",
                    },
                    subscription_cursors={"mailbox": {"event_id": "digest-host-recover"}},
                    claimed_task_ids=("task-host-recover",),
                    current_directive_ids=("directive-host-recover",),
                    current_binding=SessionBinding(
                        session_id="scripted:teammate:worker-assign-recover",
                        backend="scripted",
                        binding_type="resident",
                        transport_locator={"pid": 321},
                        supervisor_id="supervisor-old",
                        lease_id="lease-old",
                        lease_expires_at="2026-04-05T00:00:30+00:00",
                    ),
                    metadata={
                        "activation_epoch": 6,
                        "custom_marker": "host-owned",
                    },
                )
            )
            adapter = _RecordingTransportAdapter()
            supervisor = DefaultWorkerSupervisor(
                store=store,
                launch_backends={"scripted": backend},
                poll_interval_seconds=0.001,
                default_timeout_seconds=0.2,
                session_host=session_host,
                transport_adapter=adapter,
            )
            records = await supervisor.recover_active_sessions(
                policy=WorkerExecutionPolicy(
                    idle_timeout_seconds=0.01,
                    hard_timeout_seconds=0.3,
                    keep_session_idle=False,
                    allow_relaunch=False,
                    escalate_after_attempts=False,
                )
            )

        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record.status, WorkerStatus.COMPLETED)
        self.assertEqual(record.metadata.get("recovered_via"), "process_reattach")
        self.assertEqual(backend.reattach_count, 1)
        persisted = await store.get_worker_session("scripted:teammate:worker-assign-recover")
        self.assertIsNotNone(persisted)
        assert persisted is not None
        self.assertEqual(persisted.status, WorkerSessionStatus.COMPLETED)
        self.assertIn("scripted:teammate:worker-assign-recover", session_host.reclaimed_sessions)
        self.assertTrue(session_host.saved_sessions)
        host_session = await session_host.load_session("scripted:teammate:worker-assign-recover")
        self.assertIsNotNone(host_session)
        assert host_session is not None
        self.assertEqual(host_session.mailbox_cursor["last_envelope_id"], "env-host-recover")
        self.assertEqual(host_session.subscription_cursors["mailbox"]["event_id"], "digest-host-recover")
        self.assertEqual(host_session.claimed_task_ids, ("task-host-recover",))
        self.assertEqual(host_session.current_directive_ids, ("directive-host-recover",))
        self.assertEqual(host_session.metadata["custom_marker"], "host-owned")
        self.assertIsNone(host_session.current_worker_session_id)
        self.assertEqual(host_session.last_worker_session_id, "scripted:teammate:worker-assign-recover")
        truth = await session_host.read_worker_session_truth("scripted:teammate:worker-assign-recover")
        self.assertIsNone(truth.bound_worker_session_id)
        self.assertEqual(truth.last_worker_session_id, "scripted:teammate:worker-assign-recover")
        self.assertGreater(adapter.locator_from_session_calls, 0)
        self.assertGreater(adapter.handle_from_session_calls, 0)

    async def test_supervisor_recover_active_sessions_honors_session_filter(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            recover_assignment = _protocol_assignment(
                assignment_id="assign-recover-filtered",
                metadata={
                    **_protocol_metadata(),
                    "supervisor_id": "supervisor-old",
                },
            )
            skip_assignment = replace(
                _protocol_assignment(
                    assignment_id="assign-skip-filtered",
                    metadata={
                        **_protocol_metadata(),
                        "supervisor_id": "supervisor-old",
                    },
                ),
                worker_id="teammate:skip-filtered",
            )
            backend = _RecoverableProtocolBackend(
                backend_name="scripted",
                root=root,
                events=[
                    {
                        "after": 0.005,
                        "kind": "append_protocol_event",
                        "event": {
                            "event_id": "evt-filtered-accept",
                            "assignment_id": recover_assignment.assignment_id,
                            "worker_id": recover_assignment.worker_id,
                            "status": "accepted",
                            "phase": "accepted",
                        },
                    },
                    {
                        "after": 0.01,
                        "kind": "write_final_report",
                        "report": {
                            "assignment_id": recover_assignment.assignment_id,
                            "worker_id": recover_assignment.worker_id,
                            "terminal_status": "completed",
                            "summary": "done",
                        },
                    },
                    {
                        "after": 0.01,
                        "kind": "write_result",
                        "status": "completed",
                        "output_text": "done",
                        "raw_payload": {"backend": "scripted"},
                    },
                ],
            )
            recover_protocol_state_file = root / f"{recover_assignment.assignment_id}.protocol.json"
            recover_result_file = root / f"{recover_assignment.assignment_id}.result.json"
            recover_protocol_state_file.write_text(json.dumps({"protocol_events": []}), encoding="utf-8")
            asyncio.create_task(
                backend._play(
                    assignment=recover_assignment,
                    protocol_state_file=recover_protocol_state_file,
                    result_file=recover_result_file,
                )
            )

            store = _TrackingSessionStore()
            await store.save_worker_session(
                WorkerSession(
                    session_id="scripted:teammate:recover-filtered",
                    worker_id=recover_assignment.worker_id,
                    assignment_id=recover_assignment.assignment_id,
                    backend="scripted",
                    role="teammate",
                    status=WorkerSessionStatus.ACTIVE,
                    lifecycle_status="running",
                    supervisor_id="supervisor-old",
                    supervisor_lease_id="lease-old-1",
                    supervisor_lease_expires_at="2026-04-05T00:00:30+00:00",
                    handle_snapshot={
                        "backend": "scripted",
                        "transport_ref": str(recover_result_file),
                        "metadata": {
                            "protocol_state_file": str(recover_protocol_state_file),
                            "reattach_supported": True,
                        },
                    },
                    metadata={
                        "group_id": recover_assignment.group_id,
                        "task_id": recover_assignment.task_id,
                        "objective_id": "obj-target",
                        "execution_contract": dict(recover_assignment.metadata.get("execution_contract", {})),
                        "lease_policy": dict(recover_assignment.metadata.get("lease_policy", {})),
                    },
                )
            )
            await store.save_worker_session(
                WorkerSession(
                    session_id="scripted:teammate:skip-filtered",
                    worker_id=skip_assignment.worker_id,
                    assignment_id=skip_assignment.assignment_id,
                    backend="scripted",
                    role="teammate",
                    status=WorkerSessionStatus.ACTIVE,
                    lifecycle_status="running",
                    supervisor_id="supervisor-old",
                    supervisor_lease_id="lease-old-2",
                    supervisor_lease_expires_at="2026-04-05T00:00:30+00:00",
                    handle_snapshot={
                        "backend": "scripted",
                        "transport_ref": str(root / f"{skip_assignment.assignment_id}.result.json"),
                        "metadata": {
                            "protocol_state_file": str(root / f"{skip_assignment.assignment_id}.protocol.json"),
                            "reattach_supported": True,
                        },
                    },
                    metadata={
                        "group_id": skip_assignment.group_id,
                        "task_id": skip_assignment.task_id,
                        "objective_id": "obj-other",
                        "execution_contract": dict(skip_assignment.metadata.get("execution_contract", {})),
                        "lease_policy": dict(skip_assignment.metadata.get("lease_policy", {})),
                    },
                )
            )

            supervisor = DefaultWorkerSupervisor(
                store=store,
                launch_backends={"scripted": backend},
                poll_interval_seconds=0.001,
                default_timeout_seconds=0.2,
            )
            records = await supervisor.recover_active_sessions(
                policy=WorkerExecutionPolicy(
                    idle_timeout_seconds=0.01,
                    hard_timeout_seconds=0.3,
                    keep_session_idle=False,
                    allow_relaunch=False,
                    escalate_after_attempts=False,
                ),
                session_filter=lambda session: session.metadata.get("objective_id") == "obj-target",
            )

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].assignment_id, recover_assignment.assignment_id)
        self.assertEqual(backend.reattach_count, 1)
        skipped = await store.get_worker_session("scripted:teammate:skip-filtered")
        self.assertIsNotNone(skipped)
        assert skipped is not None
        self.assertEqual(skipped.supervisor_lease_id, "lease-old-2")

    async def test_supervisor_validates_final_report_before_completed_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            backend = _ProtocolResultBackend(
                backend_name="scripted",
                root=Path(tmpdir),
                payload={
                    "status": "completed",
                    "output_text": "done",
                    "raw_payload": {
                        "protocol_events": [
                            {
                                "event_id": "evt-1",
                                "assignment_id": "assign-missing-report",
                                "worker_id": "worker-assign-missing-report",
                                "status": "accepted",
                                "phase": "accepted",
                            }
                        ]
                    },
                },
            )
            store = InMemoryOrchestrationStore()
            runtime = GroupRuntime(
                store=store,
                bus=InMemoryEventBus(),
                launch_backends={"scripted": backend},
                supervisor=DefaultWorkerSupervisor(store=store, launch_backends={"scripted": backend}),
            )

            record = await runtime.run_worker_assignment(
                _protocol_assignment(
                    assignment_id="assign-missing-report",
                    metadata=_protocol_metadata(require_final_report=True),
                ),
                policy=WorkerExecutionPolicy(allow_relaunch=False, escalate_after_attempts=False),
            )

        self.assertEqual(record.status, WorkerStatus.FAILED)
        self.assertEqual(record.metadata.get("supervision_mode"), "protocol_first")
        self.assertEqual(record.metadata.get("protocol_failure_reason"), "missing_final_report")

    async def test_supervisor_rejects_legacy_backend_without_protocol_native_wait(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            backend = _LegacyProgressBackend(backend_name="legacy", root=Path(tmpdir))
            store = InMemoryOrchestrationStore()
            runtime = GroupRuntime(
                store=store,
                bus=InMemoryEventBus(),
                launch_backends={"legacy": backend},
                supervisor=DefaultWorkerSupervisor(
                    store=store,
                    launch_backends={"legacy": backend},
                    poll_interval_seconds=0.001,
                    default_timeout_seconds=0.02,
                ),
            )
            assignment = WorkerAssignment(
                assignment_id="assign-legacy",
                worker_id="worker-assign-legacy",
                group_id="group-a",
                team_id="team-a",
                task_id="task-legacy",
                role="teammate",
                backend="legacy",
                instructions="legacy flow",
                input_text="run",
            )

            with self.assertRaisesRegex(RuntimeError, "protocol-native wait"):
                await runtime.run_worker_assignment(
                    assignment,
                    policy=WorkerExecutionPolicy(
                        allow_relaunch=False,
                        escalate_after_attempts=False,
                    ),
                )

    async def test_supervisor_fails_when_protocol_capable_backend_omits_protocol_state_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            backend = _ProtocolClaimingResultOnlyBackend(
                backend_name="scripted",
                root=Path(tmpdir),
                payload={
                    "status": "completed",
                    "output_text": "done",
                    "raw_payload": {"backend": "scripted"},
                },
            )
            store = InMemoryOrchestrationStore()
            runtime = GroupRuntime(
                store=store,
                bus=InMemoryEventBus(),
                launch_backends={"scripted": backend},
                supervisor=DefaultWorkerSupervisor(
                    store=store,
                    launch_backends={"scripted": backend},
                ),
            )

            record = await runtime.run_worker_assignment(
                _protocol_assignment(assignment_id="assign-missing-protocol-state-file"),
                policy=WorkerExecutionPolicy(
                    allow_relaunch=False,
                    escalate_after_attempts=False,
                ),
            )

        self.assertEqual(record.status, WorkerStatus.FAILED)
        self.assertIn("protocol_state_file", record.error_text)
