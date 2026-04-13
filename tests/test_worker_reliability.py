from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, call, patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agent_orchestra.bus.in_memory import InMemoryEventBus
from agent_orchestra.contracts.daemon import ProviderRouteStatus
from agent_orchestra.contracts.enums import EventKind
from agent_orchestra.contracts.execution import (
    ResidentCoordinatorPhase,
    ResidentCoordinatorSession,
    WorkerAssignment,
    WorkerBackendCapabilities,
    WorkerEscalation,
    WorkerExecutionPolicy,
    WorkerProviderRoute,
    WorkerSession,
    WorkerSessionStatus,
    WorkerTransportLocator,
)
from agent_orchestra.contracts.execution import WorkerHandle
from agent_orchestra.contracts.enums import WorkerStatus
from agent_orchestra.contracts.runner import AgentRunner, RunnerHealth, RunnerStreamEvent, RunnerTurnRequest, RunnerTurnResult
from agent_orchestra.contracts.worker_protocol import WorkerExecutionContract, WorkerLeasePolicy
from agent_orchestra.runtime.backends.codex_cli_backend import CodexCliLaunchBackend
from agent_orchestra.runtime.backends.in_process import InProcessLaunchBackend
from agent_orchestra.runtime.backends.subprocess_backend import SubprocessLaunchBackend
from agent_orchestra.runtime.backends.tmux_backend import TmuxLaunchBackend
from agent_orchestra.runtime.group_runtime import GroupRuntime
from agent_orchestra.runtime.resident_kernel import ResidentCoordinatorCycleResult, ResidentCoordinatorKernel
from agent_orchestra.runtime.worker_supervisor import DefaultWorkerSupervisor
from agent_orchestra.storage.in_memory import InMemoryOrchestrationStore


class _FakeProcess:
    def __init__(self, *, pid: int = 1234, running: bool = True, timeout_message: str = "timed out") -> None:
        self.pid = pid
        self.running = running
        self.timeout_message = timeout_message
        self.wait_calls: list[float | None] = []

    def poll(self) -> int | None:
        return None if self.running else 0

    def wait(self, timeout: float | None = None) -> int:
        self.wait_calls.append(timeout)
        if self.running:
            raise subprocess.TimeoutExpired(cmd=["fake"], timeout=timeout or 0.0)
        return 0

    def terminate(self) -> None:
        self.running = False


class _FakeTmuxRunner:
    def __init__(self, *, has_session_returncode: int = 0) -> None:
        self.has_session_returncode = has_session_returncode
        self.commands: list[list[str]] = []

    def __call__(self, command: list[str], cwd: str | None):
        from agent_orchestra.runtime.backends.base import CommandResult

        self.commands.append(command)
        if command[:2] == ["tmux", "has-session"]:
            return CommandResult(returncode=self.has_session_returncode, stderr="missing session")
        return CommandResult(returncode=0, stderr="")


class _ScriptedBackend:
    def __init__(
        self,
        *,
        backend_name: str,
        root: Path,
        launch_steps: list[dict[str, object]],
        resume_steps: list[dict[str, object]] | None = None,
    ) -> None:
        self.backend_name = backend_name
        self.root = root
        self.launch_steps = list(launch_steps)
        self.resume_steps = list(resume_steps or [])
        self.launch_count = 0
        self.resume_count = 0
        self.cancel_count = 0

    def describe_capabilities(self) -> WorkerBackendCapabilities:
        return WorkerBackendCapabilities(
            supports_protocol_contract=True,
            supports_protocol_state=True,
            supports_protocol_final_report=True,
            supports_resume=True,
            supports_reactivate=False,
            supports_artifact_progress=False,
            supports_verification_in_working_dir=True,
        )

    async def launch(self, assignment: WorkerAssignment) -> WorkerHandle:
        self.launch_count += 1
        step = self.launch_steps[self.launch_count - 1]
        handle = WorkerHandle(
            worker_id=assignment.worker_id,
            role=assignment.role,
            backend=self.backend_name,
            run_id=assignment.assignment_id,
            transport_ref="",
            metadata={},
        )
        return self._apply_step(handle, assignment, step, mode="launch", index=self.launch_count)

    async def cancel(self, handle: WorkerHandle) -> None:
        self.cancel_count += 1
        process = handle.metadata.get("_process")
        if process is not None and hasattr(process, "terminate"):
            process.terminate()

    async def resume(self, handle: WorkerHandle, assignment: WorkerAssignment | None = None) -> WorkerHandle:
        self.resume_count += 1
        step = self.resume_steps[self.resume_count - 1]
        active_assignment = assignment
        if active_assignment is None:
            raise AssertionError("resume requires assignment in tests")
        return self._apply_step(handle, active_assignment, step, mode="resume", index=self.resume_count)

    def _apply_step(
        self,
        handle: WorkerHandle,
        assignment: WorkerAssignment,
        step: dict[str, object],
        *,
        mode: str,
        index: int,
    ) -> WorkerHandle:
        result_path = self.root / f"{assignment.assignment_id}.{mode}-{index}.result.json"
        protocol_state_path = self.root / f"{assignment.assignment_id}.{mode}-{index}.protocol.json"
        protocol_payload: dict[str, object] = {}
        raw_payload = {
            "backend": self.backend_name,
            "step": f"{mode}-{index}",
        }
        raw_payload.update(dict(step.get("raw_payload", {})))
        if "protocol_events" in raw_payload:
            protocol_payload["protocol_events"] = raw_payload["protocol_events"]
        else:
            protocol_payload["protocol_events"] = []
        if "final_report" in raw_payload:
            protocol_payload["final_report"] = raw_payload["final_report"]
        protocol_state_path.write_text(json.dumps(protocol_payload), encoding="utf-8")
        if step.get("timeout"):
            if result_path.exists():
                result_path.unlink()
        else:
            payload = {
                "worker_id": assignment.worker_id,
                "assignment_id": assignment.assignment_id,
                "status": step.get("status", "completed"),
                "output_text": step.get("output_text", ""),
                "error_text": step.get("error_text", ""),
                "response_id": step.get("response_id"),
                "usage": {},
                "raw_payload": raw_payload,
            }
            result_path.write_text(json.dumps(payload), encoding="utf-8")
        handle.transport_ref = str(result_path)
        handle.metadata["resume_supported"] = bool(step.get("resume_supported", False))
        handle.metadata["protocol_state_file"] = str(protocol_state_path)
        handle.metadata["step"] = f"{mode}-{index}"
        process = step.get("process")
        if process is not None:
            handle.process_id = getattr(process, "pid", handle.process_id)
            handle.metadata["_process"] = process
        else:
            handle.metadata.pop("_process", None)
        return handle


class _LegacyOnlyBackend:
    async def launch(self, assignment: WorkerAssignment) -> WorkerHandle:
        return WorkerHandle(
            worker_id=assignment.worker_id,
            role=assignment.role,
            backend=assignment.backend,
            run_id=assignment.assignment_id,
            transport_ref=None,
            metadata={},
        )

    async def cancel(self, handle: WorkerHandle) -> None:
        return None

    async def resume(self, handle: WorkerHandle, assignment: WorkerAssignment | None = None) -> WorkerHandle:
        return handle


class _ProgressArtifactBackend:
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
        self.resume_count = 0
        self._tasks: list[asyncio.Task[None]] = []

    async def launch(self, assignment: WorkerAssignment) -> WorkerHandle:
        process = _FakeProcess(running=True)
        stdout_file = self.root / f"{assignment.assignment_id}.stdout.jsonl"
        stderr_file = self.root / f"{assignment.assignment_id}.stderr.log"
        last_message_file = self.root / f"{assignment.assignment_id}.last_message.txt"
        result_file = self.root / f"{assignment.assignment_id}.result.json"
        stdout_file.write_text("", encoding="utf-8")
        stderr_file.write_text("", encoding="utf-8")
        handle = WorkerHandle(
            worker_id=assignment.worker_id,
            role=assignment.role,
            backend=self.backend_name,
            run_id=assignment.assignment_id,
            process_id=process.pid,
            transport_ref=str(result_file),
            metadata={
                "_process": process,
                "resume_supported": False,
                "stdout_file": str(stdout_file),
                "stderr_file": str(stderr_file),
                "last_message_file": str(last_message_file),
            },
        )
        self._tasks.append(
            asyncio.create_task(
                self._play_events(
                    assignment=assignment,
                    process=process,
                    stdout_file=stdout_file,
                    stderr_file=stderr_file,
                    last_message_file=last_message_file,
                    result_file=result_file,
                )
            )
        )
        return handle

    async def cancel(self, handle: WorkerHandle) -> None:
        self.cancel_count += 1
        process = handle.metadata.get("_process")
        if process is not None and hasattr(process, "terminate"):
            process.terminate()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def resume(self, handle: WorkerHandle, assignment: WorkerAssignment | None = None) -> WorkerHandle:
        self.resume_count += 1
        return handle

    async def _play_events(
        self,
        *,
        assignment: WorkerAssignment,
        process: _FakeProcess,
        stdout_file: Path,
        stderr_file: Path,
        last_message_file: Path,
        result_file: Path,
    ) -> None:
        try:
            for event in self.events:
                await asyncio.sleep(float(event.get("after", 0.0)))
                kind = str(event["kind"])
                if kind == "touch_stdout":
                    existing = stdout_file.read_text(encoding="utf-8") if stdout_file.exists() else ""
                    stdout_file.write_text(existing + str(event.get("text", "progress\n")), encoding="utf-8")
                    continue
                if kind == "touch_stderr":
                    existing = stderr_file.read_text(encoding="utf-8") if stderr_file.exists() else ""
                    stderr_file.write_text(existing + str(event.get("text", "stderr\n")), encoding="utf-8")
                    continue
                if kind == "write_last_message":
                    last_message_file.write_text(str(event.get("text", "")), encoding="utf-8")
                    continue
                if kind == "finish":
                    process.running = False
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
                        "raw_payload": {
                            "backend": self.backend_name,
                            **dict(event.get("raw_payload", {})),
                        },
                    }
                    result_file.write_text(json.dumps(payload), encoding="utf-8")
                    continue
                raise AssertionError(f"Unknown progress event kind: {kind}")
        except asyncio.CancelledError:
            return


class _SequencedRunner(AgentRunner):
    def __init__(self) -> None:
        self.requests: list[RunnerTurnRequest] = []

    async def run_turn(self, request: RunnerTurnRequest) -> RunnerTurnResult:
        self.requests.append(request)
        index = len(self.requests)
        return RunnerTurnResult(
            response_id=f"resp-{index}",
            output_text=f"turn-{index}",
            status="completed",
        )

    async def stream_turn(self, request: RunnerTurnRequest):
        if False:
            yield RunnerStreamEvent(kind=EventKind.RUNNER_COMPLETED)

    async def cancel(self, run_id: str) -> None:
        return None

    async def healthcheck(self) -> RunnerHealth:
        return RunnerHealth(healthy=True, provider="fake")


class _SessionAwareStore(InMemoryOrchestrationStore):
    def __init__(self) -> None:
        super().__init__()
        self.worker_sessions: dict[str, WorkerSession] = {}

    async def save_worker_session(self, session: WorkerSession) -> None:
        self.worker_sessions[session.session_id] = session

    async def get_worker_session(self, session_id: str) -> WorkerSession | None:
        return self.worker_sessions.get(session_id)

    async def list_worker_sessions(self) -> list[WorkerSession]:
        return list(self.worker_sessions.values())


class _ProtocolSequencedRunner(AgentRunner):
    def __init__(self) -> None:
        self.requests: list[RunnerTurnRequest] = []

    async def run_turn(self, request: RunnerTurnRequest) -> RunnerTurnResult:
        self.requests.append(request)
        return RunnerTurnResult(
            response_id="resp-protocol-1",
            output_text="protocol turn done",
            status="completed",
            protocol_events=(
                {
                    "event_id": "evt-accept",
                    "assignment_id": request.metadata.get("assignment_id", "assign-protocol"),
                    "worker_id": request.agent_id,
                    "status": "accepted",
                    "phase": "accepted",
                },
                {
                    "event_id": "evt-final",
                    "assignment_id": request.metadata.get("assignment_id", "assign-protocol"),
                    "worker_id": request.agent_id,
                    "status": "completed",
                    "phase": "terminal_report_announced",
                },
            ),
            final_report={
                "assignment_id": request.metadata.get("assignment_id", "assign-protocol"),
                "worker_id": request.agent_id,
                "terminal_status": "completed",
                "summary": "protocol finished",
            },
        )

    async def stream_turn(self, request: RunnerTurnRequest):
        if False:
            yield RunnerStreamEvent(kind=EventKind.RUNNER_COMPLETED)

    async def cancel(self, run_id: str) -> None:
        return None

    async def healthcheck(self) -> RunnerHealth:
        return RunnerHealth(healthy=True, provider="fake")


def _assignment(
    *,
    backend: str = "scripted",
    metadata: dict[str, object] | None = None,
) -> WorkerAssignment:
    return WorkerAssignment(
        assignment_id=f"assign-{backend}",
        worker_id=f"worker-{backend}",
        group_id="group-a",
        team_id="team-a",
        task_id="task-a",
        role="teammate",
        backend=backend,
        instructions="Improve runtime reliability.",
        input_text="Execute the assignment.",
        metadata=dict(metadata or {}),
    )


class WorkerReliabilityContractsTest(TestCase):
    def test_execution_policy_defaults_to_bounded_retry_with_resume_on_timeout(self) -> None:
        policy = WorkerExecutionPolicy()

        self.assertEqual(policy.max_attempts, 2)
        self.assertIsNone(policy.attempt_timeout_seconds)
        self.assertTrue(policy.resume_on_timeout)
        self.assertFalse(policy.resume_on_failure)
        self.assertTrue(policy.allow_relaunch)
        self.assertTrue(policy.escalate_after_attempts)
        self.assertEqual(policy.backoff_seconds, 0.0)
        self.assertFalse(policy.keep_session_idle)
        self.assertTrue(policy.reactivate_idle_session)
        self.assertTrue(policy.fallback_on_provider_unavailable)
        self.assertEqual(policy.provider_fallbacks, ())

    def test_execution_policy_supports_provider_unavailable_specific_backoff_fields(self) -> None:
        policy = WorkerExecutionPolicy(
            provider_unavailable_backoff_initial_seconds=15.0,
            provider_unavailable_backoff_multiplier=2.0,
            provider_unavailable_backoff_max_seconds=120.0,
        )

        self.assertEqual(policy.provider_unavailable_backoff_initial_seconds, 15.0)
        self.assertEqual(policy.provider_unavailable_backoff_multiplier, 2.0)
        self.assertEqual(policy.provider_unavailable_backoff_max_seconds, 120.0)

    def test_worker_escalation_retains_assignment_failure_context(self) -> None:
        escalation = WorkerEscalation(
            assignment_id="assign-1",
            worker_id="worker-1",
            attempt_count=3,
            reason="attempts_exhausted",
            backend="codex_cli",
            last_error="provider overloaded",
            metadata={"lane_id": "lane-runtime"},
        )

        self.assertEqual(escalation.assignment_id, "assign-1")
        self.assertEqual(escalation.attempt_count, 3)
        self.assertEqual(escalation.reason, "attempts_exhausted")
        self.assertEqual(escalation.metadata["lane_id"], "lane-runtime")

    def test_resident_coordinator_session_captures_runtime_progress(self) -> None:
        session = ResidentCoordinatorSession(
            coordinator_id="leader:runtime",
            role="leader",
            phase=ResidentCoordinatorPhase.BOOTING,
            objective_id="obj-runtime",
            lane_id="runtime",
            team_id="team-runtime",
        )

        self.assertEqual(session.coordinator_id, "leader:runtime")
        self.assertEqual(session.phase, ResidentCoordinatorPhase.BOOTING)
        self.assertEqual(session.cycle_count, 0)
        self.assertEqual(session.prompt_turn_count, 0)
        self.assertEqual(session.claimed_task_count, 0)
        self.assertEqual(session.subordinate_dispatch_count, 0)
        self.assertEqual(session.idle_transition_count, 0)
        self.assertEqual(session.quiescent_transition_count, 0)

    def test_worker_session_status_includes_durable_lifecycle_states(self) -> None:
        self.assertEqual(WorkerSessionStatus.ASSIGNED.value, "assigned")
        self.assertEqual(WorkerSessionStatus.ACTIVE.value, "active")
        self.assertEqual(WorkerSessionStatus.IDLE.value, "idle")
        self.assertEqual(WorkerSessionStatus.COMPLETED.value, "completed")
        self.assertEqual(WorkerSessionStatus.FAILED.value, "failed")
        self.assertEqual(WorkerSessionStatus.ABANDONED.value, "abandoned")

    def test_worker_transport_locator_roundtrip(self) -> None:
        locator = WorkerTransportLocator(
            backend="subprocess",
            working_dir="/tmp/ao",
            spool_dir="/tmp/ao/spool",
            protocol_state_file="/tmp/ao/protocol.json",
            result_file="/tmp/ao/result.json",
            stdout_file="/tmp/ao/stdout.log",
            stderr_file="/tmp/ao/stderr.log",
            last_message_file="/tmp/ao/last_message.txt",
            pid=1234,
            command_fingerprint="fp-1",
        )

        payload = locator.to_dict()
        round_tripped = WorkerTransportLocator.from_dict(payload)

        self.assertEqual(round_tripped.pid, 1234)
        self.assertEqual(round_tripped.command_fingerprint, "fp-1")

    def test_worker_transport_locator_to_dict_is_json_safe(self) -> None:
        locator = WorkerTransportLocator(
            backend="subprocess",
            metadata={"protocol_path": Path("/tmp/ao/protocol.json")},
        )

        payload = locator.to_dict()

        self.assertEqual(payload["metadata"]["protocol_path"], "/tmp/ao/protocol.json")
        json.dumps(payload)

    def test_worker_session_roundtrip_preserves_active_durable_fields(self) -> None:
        locator = WorkerTransportLocator(
            backend="subprocess",
            working_dir="/tmp/ao",
            spool_dir="/tmp/ao/spool",
            protocol_state_file="/tmp/ao/protocol.json",
            result_file="/tmp/ao/result.json",
            stdout_file="/tmp/ao/stdout.log",
            stderr_file="/tmp/ao/stderr.log",
            last_message_file="/tmp/ao/last_message.txt",
            pid=1234,
            command_fingerprint="fp-1",
        )
        session = WorkerSession(
            session_id="session-1",
            worker_id="worker-1",
            assignment_id="assignment-1",
            backend="subprocess",
            role="teammate",
            status=WorkerSessionStatus.ACTIVE,
            lifecycle_status="running",
            transport_locator=locator,
            protocol_cursor={"stream": "lifecycle", "offset": "10-0"},
            mailbox_cursor={"last_envelope_id": "env-1"},
            supervisor_id="supervisor-a",
            supervisor_lease_id="lease-a",
            supervisor_lease_expires_at="2026-04-05T00:00:30+00:00",
        )

        round_tripped = WorkerSession.from_dict(session.to_dict())

        self.assertEqual(round_tripped.status, WorkerSessionStatus.ACTIVE)
        self.assertIsNotNone(round_tripped.transport_locator)
        assert round_tripped.transport_locator is not None
        self.assertEqual(round_tripped.transport_locator.pid, 1234)
        self.assertEqual(round_tripped.protocol_cursor.get("offset"), "10-0")
        self.assertEqual(round_tripped.mailbox_cursor.get("last_envelope_id"), "env-1")
        self.assertEqual(round_tripped.supervisor_lease_id, "lease-a")

    def test_worker_session_to_dict_is_json_safe_and_normalizes_legacy_closed(self) -> None:
        session = WorkerSession(
            session_id="session-legacy",
            worker_id="worker-legacy",
            assignment_id="assignment-legacy",
            backend="subprocess",
            role="teammate",
            status=WorkerSessionStatus.CLOSED,
            protocol_cursor={"path": Path("/tmp/ao/protocol.json")},
            metadata={"spool_root": Path("/tmp/ao/spool")},
        )

        payload = session.to_dict()

        self.assertEqual(payload["status"], WorkerSessionStatus.ABANDONED.value)
        self.assertEqual(payload["protocol_cursor"]["path"], "/tmp/ao/protocol.json")
        self.assertEqual(payload["metadata"]["spool_root"], "/tmp/ao/spool")
        json.dumps(payload)


class ResidentCoordinatorKernelTest(IsolatedAsyncioTestCase):
    async def test_kernel_updates_phase_and_progress_counters(self) -> None:
        kernel = ResidentCoordinatorKernel()
        session = ResidentCoordinatorSession(
            coordinator_id="leader:runtime",
            role="leader",
            phase=ResidentCoordinatorPhase.BOOTING,
            objective_id="obj-runtime",
            lane_id="runtime",
            team_id="team-runtime",
        )
        cycle_results = iter(
            (
                ResidentCoordinatorCycleResult(
                    phase=ResidentCoordinatorPhase.RUNNING,
                    prompt_turn_delta=1,
                    claimed_task_delta=2,
                    subordinate_dispatch_delta=2,
                    mailbox_poll_delta=1,
                    active_subordinate_ids=("team-runtime:teammate:1", "team-runtime:teammate:2"),
                    reason="executed leader turn",
                ),
                ResidentCoordinatorCycleResult(
                    phase=ResidentCoordinatorPhase.IDLE,
                    mailbox_poll_delta=1,
                    active_subordinate_ids=(),
                    reason="waiting for subordinate mail",
                ),
                ResidentCoordinatorCycleResult(
                    phase=ResidentCoordinatorPhase.QUIESCENT,
                    stop=True,
                    mailbox_cursor="env-3",
                    reason="no claimable work remains",
                ),
            )
        )

        async def step(current: ResidentCoordinatorSession) -> ResidentCoordinatorCycleResult:
            return next(cycle_results)

        async def finalize(current: ResidentCoordinatorSession) -> ResidentCoordinatorSession:
            return current

        final_session = await kernel.run(session=session, step=step, finalize=finalize)

        self.assertEqual(final_session.phase, ResidentCoordinatorPhase.QUIESCENT)
        self.assertEqual(final_session.cycle_count, 3)
        self.assertEqual(final_session.prompt_turn_count, 1)
        self.assertEqual(final_session.claimed_task_count, 2)
        self.assertEqual(final_session.subordinate_dispatch_count, 2)
        self.assertEqual(final_session.mailbox_poll_count, 2)
        self.assertEqual(final_session.idle_transition_count, 1)
        self.assertEqual(final_session.quiescent_transition_count, 1)
        self.assertEqual(final_session.active_subordinate_ids, ())
        self.assertEqual(final_session.mailbox_cursor, "env-3")
        self.assertEqual(final_session.last_reason, "no claimable work remains")


class WorkerSessionStoreDurabilityTest(IsolatedAsyncioTestCase):
    async def test_in_memory_store_persists_active_session_reclaims_lease_and_tracks_bus_cursor(self) -> None:
        store = InMemoryOrchestrationStore()
        locator = WorkerTransportLocator(
            backend="subprocess",
            working_dir="/tmp/ao",
            spool_dir="/tmp/ao/spool",
            protocol_state_file="/tmp/ao/protocol.json",
            result_file="/tmp/ao/result.json",
            stdout_file="/tmp/ao/stdout.log",
            stderr_file="/tmp/ao/stderr.log",
            last_message_file="/tmp/ao/last_message.txt",
            pid=1234,
            command_fingerprint="fp-1",
            metadata={
                "reconnect": {
                    "attempts": [{"at": "2026-04-05T00:00:10+00:00"}],
                }
            },
        )
        session = WorkerSession(
            session_id="session-1",
            worker_id="worker-1",
            assignment_id="assignment-1",
            backend="subprocess",
            role="teammate",
            status=WorkerSessionStatus.ACTIVE,
            lifecycle_status="running",
            transport_locator=locator,
            protocol_cursor={"stream": "lifecycle", "offset": "10-0", "meta": {"lag": 2}},
            mailbox_cursor={"last_envelope_id": "env-1", "pending": ["env-2"]},
            supervisor_id="supervisor-a",
            supervisor_lease_id="lease-old",
            supervisor_lease_expires_at="2026-04-05T00:00:30+00:00",
        )

        await store.save_worker_session(session)

        session.protocol_cursor["meta"]["lag"] = 99
        session.mailbox_cursor["pending"].append("env-3")
        assert session.transport_locator is not None
        session.transport_locator.metadata["reconnect"]["attempts"][0]["at"] = "mutated"

        saved = await store.get_worker_session("session-1")
        self.assertIsNotNone(saved)
        assert saved is not None
        self.assertEqual(saved.status, WorkerSessionStatus.ACTIVE)
        self.assertEqual(saved.supervisor_id, "supervisor-a")
        self.assertIsNotNone(saved.transport_locator)
        assert saved.transport_locator is not None
        self.assertEqual(saved.transport_locator.pid, 1234)
        self.assertEqual(saved.protocol_cursor["meta"]["lag"], 2)
        self.assertEqual(saved.mailbox_cursor["pending"], ["env-2"])
        self.assertEqual(
            saved.transport_locator.metadata["reconnect"]["attempts"][0]["at"],
            "2026-04-05T00:00:10+00:00",
        )

        reclaimable = await store.list_reclaimable_worker_sessions(
            now="2026-04-05T00:01:00+00:00",
            statuses=(WorkerSessionStatus.ASSIGNED.value, WorkerSessionStatus.ACTIVE.value),
        )
        self.assertEqual([item.session_id for item in reclaimable], ["session-1"])

        reclaimed = await store.reclaim_worker_session_lease(
            session_id="session-1",
            previous_lease_id="lease-old",
            new_supervisor_id="supervisor-b",
            new_lease_id="lease-new",
            now="2026-04-05T00:01:00+00:00",
            new_expires_at="2026-04-05T00:01:30+00:00",
        )
        self.assertIsNotNone(reclaimed)
        assert reclaimed is not None
        self.assertEqual(reclaimed.supervisor_id, "supervisor-b")
        self.assertEqual(reclaimed.supervisor_lease_id, "lease-new")
        self.assertEqual(reclaimed.supervisor_lease_expires_at, "2026-04-05T00:01:30+00:00")

        await store.save_protocol_bus_cursor(
            stream="lifecycle",
            consumer="supervisor-b",
            cursor={
                "offset": "10-0",
                "checkpoint": {"assignment_id": "assignment-1", "retry": None},
            },
        )
        cursor = await store.get_protocol_bus_cursor(stream="lifecycle", consumer="supervisor-b")
        self.assertEqual(
            cursor,
            {
                "offset": "10-0",
                "checkpoint": {"assignment_id": "assignment-1", "retry": None},
            },
        )

    async def test_in_memory_store_normalizes_legacy_closed_status_to_abandoned(self) -> None:
        store = InMemoryOrchestrationStore()
        await store.save_worker_session(
            WorkerSession(
                session_id="session-legacy",
                worker_id="worker-legacy",
                assignment_id="assignment-legacy",
                backend="subprocess",
                role="teammate",
                status=WorkerSessionStatus.CLOSED,
            )
        )

        saved = await store.get_worker_session("session-legacy")
        self.assertIsNotNone(saved)
        assert saved is not None
        self.assertEqual(saved.status, WorkerSessionStatus.ABANDONED)


class WorkerReliabilityRuntimeTest(IsolatedAsyncioTestCase):
    async def test_group_runtime_rejects_protocol_required_assignment_on_legacy_only_backend(self) -> None:
        store = InMemoryOrchestrationStore()
        backend = _LegacyOnlyBackend()
        runtime = GroupRuntime(
            store=store,
            bus=InMemoryEventBus(),
            launch_backends={"legacy_only": backend},
            supervisor=DefaultWorkerSupervisor(store=store, launch_backends={"legacy_only": backend}),
        )

        assignment = WorkerAssignment(
            assignment_id="assign-legacy-only",
            worker_id="worker-legacy-only",
            group_id="group-a",
            task_id="task-legacy-only",
            role="leader",
            backend="legacy_only",
            instructions="Protocol-required work.",
            input_text="run",
            execution_contract=WorkerExecutionContract(
                contract_id="contract-legacy-only",
                mode="leader_coordination",
                require_final_report=True,
            ),
            lease_policy=WorkerLeasePolicy(
                accept_deadline_seconds=1.0,
                renewal_timeout_seconds=30.0,
                hard_deadline_seconds=300.0,
            ),
        )

        with self.assertRaisesRegex(RuntimeError, "does not support protocol-native execution"):
            await runtime.run_worker_assignment(
                assignment,
                policy=WorkerExecutionPolicy(allow_relaunch=False, escalate_after_attempts=False),
            )

    async def test_group_runtime_enriches_in_process_record_with_protocol_metadata(self) -> None:
        store = InMemoryOrchestrationStore()
        runner = _ProtocolSequencedRunner()
        supervisor = DefaultWorkerSupervisor(
            store=store,
            launch_backends={"in_process": InProcessLaunchBackend()},
            runner=runner,
        )
        runtime = GroupRuntime(
            store=store,
            bus=InMemoryEventBus(),
            launch_backends={"in_process": InProcessLaunchBackend()},
            supervisor=supervisor,
        )

        record = await runtime.run_worker_assignment(
            WorkerAssignment(
                assignment_id="assign-protocol",
                worker_id="leader:protocol",
                group_id="group-a",
                task_id="task-protocol",
                role="leader",
                backend="in_process",
                instructions="Protocol turn.",
                input_text="protocol",
                metadata={
                    "execution_contract": {
                        "contract_id": "contract-protocol",
                        "mode": "leader_coordination",
                        "require_final_report": True,
                    },
                    "lease_policy": {
                        "accept_deadline_seconds": 1.0,
                        "renewal_timeout_seconds": 30.0,
                        "hard_deadline_seconds": 600.0,
                    },
                },
            ),
            policy=WorkerExecutionPolicy(allow_relaunch=False, escalate_after_attempts=False),
        )

        self.assertEqual(record.status, WorkerStatus.COMPLETED)
        self.assertEqual(record.metadata.get("supervision_mode"), "protocol_first")
        self.assertEqual(record.metadata.get("lifecycle_status"), "completed")
        self.assertEqual(record.metadata.get("lease", {}).get("status"), "closed")
        self.assertEqual(record.metadata.get("final_report", {}).get("terminal_status"), "completed")
        self.assertEqual(
            runner.requests[0].metadata.get("execution_contract", {}).get("contract_id"),
            "contract-protocol",
        )
        self.assertEqual(
            runner.requests[0].metadata.get("lease_policy", {}).get("accept_deadline_seconds"),
            1.0,
        )

    async def test_group_runtime_keeps_completed_in_process_session_idle_and_reactivates_next_assignment(self) -> None:
        store = InMemoryOrchestrationStore()
        runner = _SequencedRunner()
        supervisor = DefaultWorkerSupervisor(
            store=store,
            launch_backends={"in_process": InProcessLaunchBackend()},
            runner=runner,
        )
        runtime = GroupRuntime(
            store=store,
            bus=InMemoryEventBus(),
            launch_backends={"in_process": InProcessLaunchBackend()},
            supervisor=supervisor,
        )
        policy = WorkerExecutionPolicy(keep_session_idle=True, reactivate_idle_session=True)

        first = await runtime.run_worker_assignment(
            WorkerAssignment(
                assignment_id="assign-1",
                worker_id="leader:runtime",
                group_id="group-a",
                task_id="task-1",
                role="leader",
                backend="in_process",
                instructions="Handle turn 1.",
                input_text="turn-1",
            ),
            policy=policy,
        )
        second = await runtime.run_worker_assignment(
            WorkerAssignment(
                assignment_id="assign-2",
                worker_id="leader:runtime",
                group_id="group-a",
                task_id="task-2",
                role="leader",
                backend="in_process",
                instructions="Handle turn 2.",
                input_text="turn-2",
                previous_response_id=first.response_id,
            ),
            policy=policy,
        )

        self.assertEqual(first.status, WorkerStatus.COMPLETED)
        self.assertIsNotNone(first.session)
        self.assertEqual(first.session.status, WorkerSessionStatus.IDLE)
        self.assertEqual(first.session.last_assignment_id, "assign-1")
        self.assertEqual(first.session.last_response_id, "resp-1")
        self.assertEqual(second.status, WorkerStatus.COMPLETED)
        self.assertIsNotNone(second.session)
        self.assertEqual(second.session.session_id, first.session.session_id)
        self.assertEqual(second.session.status, WorkerSessionStatus.IDLE)
        self.assertEqual(second.session.reactivation_count, 1)
        self.assertEqual(second.metadata["attempts"][0]["operation"], "reactivate")
        self.assertEqual([request.previous_response_id for request in runner.requests], [None, "resp-1"])

    async def test_supervisor_hydrates_idle_session_from_store_truth_across_instances(self) -> None:
        store = _SessionAwareStore()
        runner = _SequencedRunner()
        policy = WorkerExecutionPolicy(keep_session_idle=True, reactivate_idle_session=True)

        runtime_one = GroupRuntime(
            store=store,
            bus=InMemoryEventBus(),
            launch_backends={"in_process": InProcessLaunchBackend()},
            supervisor=DefaultWorkerSupervisor(
                store=store,
                launch_backends={"in_process": InProcessLaunchBackend()},
                runner=runner,
            ),
        )
        first = await runtime_one.run_worker_assignment(
            WorkerAssignment(
                assignment_id="assign-hydrate-1",
                worker_id="leader:hydrate",
                group_id="group-a",
                task_id="task-hydrate-1",
                role="leader",
                backend="in_process",
                instructions="Handle turn 1.",
                input_text="turn-1",
            ),
            policy=policy,
        )
        self.assertIsNotNone(first.session)
        assert first.session is not None
        first_session_id = first.session.session_id
        persisted = await store.get_worker_session(first.session.session_id)
        self.assertIsNotNone(persisted)
        self.assertEqual(persisted.status, WorkerSessionStatus.IDLE)

        latest_record = await store.get_worker_record("leader:hydrate")
        self.assertIsNotNone(latest_record)
        assert latest_record is not None
        latest_record.session = None
        await store.save_worker_record(latest_record)

        runtime_two = GroupRuntime(
            store=store,
            bus=InMemoryEventBus(),
            launch_backends={"in_process": InProcessLaunchBackend()},
            supervisor=DefaultWorkerSupervisor(
                store=store,
                launch_backends={"in_process": InProcessLaunchBackend()},
                runner=runner,
            ),
        )
        second = await runtime_two.run_worker_assignment(
            WorkerAssignment(
                assignment_id="assign-hydrate-2",
                worker_id="leader:hydrate",
                group_id="group-a",
                task_id="task-hydrate-2",
                role="leader",
                backend="in_process",
                instructions="Handle turn 2.",
                input_text="turn-2",
                previous_response_id=first.response_id,
            ),
            policy=policy,
        )

        self.assertEqual(second.status, WorkerStatus.COMPLETED)
        self.assertEqual(second.metadata["attempts"][0]["operation"], "reactivate")
        self.assertIsNotNone(second.session)
        assert second.session is not None
        self.assertEqual(second.session.session_id, first_session_id)
        self.assertEqual([request.previous_response_id for request in runner.requests], [None, "resp-1"])

    async def test_supervisor_does_not_force_reactivate_for_backend_without_reactivate_capability(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            backend = _ScriptedBackend(
                backend_name="scripted",
                root=root,
                launch_steps=[{"status": "completed", "output_text": "fresh launch"}],
            )
            store = _SessionAwareStore()
            seeded = WorkerSession(
                session_id="scripted:teammate:worker-scripted",
                worker_id="worker-scripted",
                backend="scripted",
                role="teammate",
                status=WorkerSessionStatus.IDLE,
                last_assignment_id="seed-assignment",
                handle_snapshot={
                    "backend": "scripted",
                    "transport_ref": "/tmp/nonexistent.result.json",
                    "metadata": {"resume_supported": True},
                },
            )
            await store.save_worker_session(seeded)
            supervisor = DefaultWorkerSupervisor(store=store, launch_backends={"scripted": backend})
            runtime = GroupRuntime(
                store=store,
                bus=InMemoryEventBus(),
                launch_backends={"scripted": backend},
                supervisor=supervisor,
            )

            record = await runtime.run_worker_assignment(
                _assignment(
                    metadata={"worker_session_id": "scripted:teammate:worker-scripted"},
                ),
                policy=WorkerExecutionPolicy(
                    max_attempts=1,
                    reactivate_idle_session=True,
                    keep_session_idle=False,
                    allow_relaunch=False,
                    escalate_after_attempts=False,
                ),
            )

        self.assertEqual(record.status, WorkerStatus.COMPLETED)
        self.assertEqual(backend.launch_count, 1)
        self.assertEqual(backend.resume_count, 0)
        self.assertEqual(record.metadata["attempts"][0]["operation"], "launch")

    async def test_supervisor_persists_durable_session_for_non_reactivate_backend_without_idle_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            backend = _ScriptedBackend(
                backend_name="scripted",
                root=root,
                launch_steps=[{"status": "completed", "output_text": "fresh launch"}],
            )
            store = _SessionAwareStore()
            supervisor = DefaultWorkerSupervisor(store=store, launch_backends={"scripted": backend})
            runtime = GroupRuntime(
                store=store,
                bus=InMemoryEventBus(),
                launch_backends={"scripted": backend},
                supervisor=supervisor,
            )

            record = await runtime.run_worker_assignment(
                _assignment(metadata={"supervisor_id": "supervisor-runtime"}),
                policy=WorkerExecutionPolicy(
                    max_attempts=1,
                    reactivate_idle_session=True,
                    keep_session_idle=False,
                    allow_relaunch=False,
                    escalate_after_attempts=False,
                ),
            )

        self.assertEqual(record.status, WorkerStatus.COMPLETED)
        self.assertIsNone(record.session)
        persisted = await store.get_worker_session("scripted:teammate:worker-scripted")
        self.assertIsNotNone(persisted)
        assert persisted is not None
        self.assertEqual(persisted.status, WorkerSessionStatus.COMPLETED)
        self.assertEqual(persisted.supervisor_id, "supervisor-runtime")
        self.assertIsNotNone(persisted.supervisor_lease_id)
        self.assertIsNotNone(persisted.supervisor_lease_expires_at)

    async def test_group_runtime_retries_failed_attempt_and_persists_attempt_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            backend = _ScriptedBackend(
                backend_name="scripted",
                root=root,
                launch_steps=[
                    {"status": "failed", "error_text": "provider overloaded"},
                    {"status": "completed", "output_text": "retry succeeded"},
                ],
            )
            store = InMemoryOrchestrationStore()
            runtime = GroupRuntime(
                store=store,
                bus=InMemoryEventBus(),
                launch_backends={"scripted": backend},
                supervisor=DefaultWorkerSupervisor(store=store, launch_backends={"scripted": backend}),
            )

            record = await runtime.run_worker_assignment(
                _assignment(),
                policy=WorkerExecutionPolicy(max_attempts=2, allow_relaunch=True, backoff_seconds=0.0),
            )
            stored = await store.get_worker_record("worker-scripted")

        self.assertEqual(record.status, WorkerStatus.COMPLETED)
        self.assertEqual(record.output_text, "retry succeeded")
        self.assertEqual(backend.launch_count, 2)
        self.assertEqual(backend.resume_count, 0)
        self.assertIsNotNone(stored)
        self.assertEqual(stored.metadata["attempt_count"], 2)
        self.assertEqual(stored.metadata["attempts"][0]["decision"], "retry")
        self.assertEqual(stored.metadata["attempts"][1]["decision"], "complete")
        self.assertEqual(stored.metadata["execution_policy"]["max_attempts"], 2)

    async def test_group_runtime_retries_provider_unavailable_even_when_generic_relaunch_is_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            backend = _ScriptedBackend(
                backend_name="scripted",
                root=root,
                launch_steps=[
                    {
                        "status": "failed",
                        "error_text": "chatgpt authentication required to sync remote plugins",
                        "raw_payload": {
                            "failure_kind": "provider_unavailable",
                            "error_type": "ProviderUnavailableError",
                        },
                    },
                    {
                        "status": "completed",
                        "output_text": "retry after transient network failure succeeded",
                    },
                ],
            )
            store = InMemoryOrchestrationStore()
            runtime = GroupRuntime(
                store=store,
                bus=InMemoryEventBus(),
                launch_backends={"scripted": backend},
                supervisor=DefaultWorkerSupervisor(store=store, launch_backends={"scripted": backend}),
            )

            record = await runtime.run_worker_assignment(
                _assignment(),
                policy=WorkerExecutionPolicy(
                    max_attempts=2,
                    allow_relaunch=False,
                    fallback_on_provider_unavailable=True,
                    escalate_after_attempts=False,
                    backoff_seconds=0.0,
                    provider_unavailable_backoff_initial_seconds=15.0,
                    provider_unavailable_backoff_multiplier=2.0,
                    provider_unavailable_backoff_max_seconds=120.0,
                ),
            )
            stored = await store.get_worker_record("worker-scripted")

        self.assertEqual(record.status, WorkerStatus.COMPLETED)
        self.assertEqual(record.output_text, "retry after transient network failure succeeded")
        self.assertEqual(backend.launch_count, 2)
        self.assertIsNotNone(stored)
        assert stored is not None
        self.assertEqual(stored.metadata["attempt_count"], 2)
        self.assertEqual(stored.metadata["attempts"][0]["decision"], "retry")
        self.assertEqual(stored.metadata["attempts"][1]["decision"], "complete")
        self.assertEqual(
            stored.metadata["execution_policy"]["provider_unavailable_backoff_initial_seconds"],
            15.0,
        )
        self.assertEqual(
            stored.metadata["execution_policy"]["provider_unavailable_backoff_multiplier"],
            2.0,
        )
        self.assertEqual(
            stored.metadata["execution_policy"]["provider_unavailable_backoff_max_seconds"],
            120.0,
        )

    async def test_group_runtime_prefers_resume_after_timeout_before_relaunch(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            backend = _ScriptedBackend(
                backend_name="scripted",
                root=root,
                launch_steps=[
                    {
                        "timeout": True,
                        "resume_supported": True,
                        "process": _FakeProcess(running=True),
                    }
                ],
                resume_steps=[
                    {
                        "status": "completed",
                        "output_text": "resume succeeded",
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
                    default_timeout_seconds=0.01,
                ),
            )

            record = await runtime.run_worker_assignment(
                _assignment(),
                policy=WorkerExecutionPolicy(
                    max_attempts=2,
                    attempt_timeout_seconds=0.01,
                    resume_on_timeout=True,
                    allow_relaunch=True,
                ),
            )

        self.assertEqual(record.status, WorkerStatus.COMPLETED)
        self.assertEqual(record.output_text, "resume succeeded")
        self.assertEqual(backend.launch_count, 1)
        self.assertEqual(backend.resume_count, 1)
        self.assertEqual(record.metadata["attempts"][0]["decision"], "resume")
        self.assertEqual(record.metadata["attempts"][1]["operation"], "resume")

    async def test_group_runtime_rejects_non_protocol_backend_even_if_artifacts_show_progress(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            backend = _ProgressArtifactBackend(
                backend_name="codex_cli",
                root=root,
                events=[
                    {"after": 0.005, "kind": "touch_stdout", "text": '{"type":"progress-1"}\n'},
                    {"after": 0.005, "kind": "touch_stdout", "text": '{"type":"progress-2"}\n'},
                    {
                        "after": 0.005,
                        "kind": "write_last_message",
                        "text": '{"summary":"done","sequential_slices":[],"parallel_slices":[]}',
                    },
                    {"after": 0.005, "kind": "finish"},
                    {
                        "after": 0.001,
                        "kind": "write_result",
                        "status": "completed",
                        "output_text": '{"summary":"done","sequential_slices":[],"parallel_slices":[]}',
                        "raw_payload": {"exit_code": 0},
                    },
                ],
            )
            store = InMemoryOrchestrationStore()
            runtime = GroupRuntime(
                store=store,
                bus=InMemoryEventBus(),
                launch_backends={"codex_cli": backend},
                supervisor=DefaultWorkerSupervisor(
                    store=store,
                    launch_backends={"codex_cli": backend},
                    poll_interval_seconds=0.001,
                    default_timeout_seconds=0.01,
                ),
            )

            with self.assertRaisesRegex(RuntimeError, "protocol-native wait"):
                await runtime.run_worker_assignment(
                    _assignment(backend="codex_cli"),
                    policy=WorkerExecutionPolicy(
                        allow_relaunch=False,
                        escalate_after_attempts=True,
                    ),
                )

    async def test_group_runtime_no_longer_labels_idle_timeout_for_non_protocol_backend(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            backend = _ProgressArtifactBackend(
                backend_name="codex_cli",
                root=root,
                events=[
                    {"after": 0.005, "kind": "touch_stdout", "text": '{"type":"progress-1"}\n'},
                ],
            )
            store = InMemoryOrchestrationStore()
            runtime = GroupRuntime(
                store=store,
                bus=InMemoryEventBus(),
                launch_backends={"codex_cli": backend},
                supervisor=DefaultWorkerSupervisor(
                    store=store,
                    launch_backends={"codex_cli": backend},
                    poll_interval_seconds=0.001,
                    default_timeout_seconds=0.01,
                ),
            )

            with self.assertRaisesRegex(RuntimeError, "protocol-native wait"):
                await runtime.run_worker_assignment(
                    _assignment(backend="codex_cli"),
                    policy=WorkerExecutionPolicy(
                        idle_timeout_seconds=0.02,
                        hard_timeout_seconds=0.2,
                        allow_relaunch=False,
                        escalate_after_attempts=True,
                    ),
                )

        self.assertEqual(backend.cancel_count, 0)

    async def test_group_runtime_escalates_after_attempts_are_exhausted(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            backend = _ScriptedBackend(
                backend_name="scripted",
                root=root,
                launch_steps=[
                    {"status": "failed", "error_text": "provider overloaded"},
                    {"status": "failed", "error_text": "provider still overloaded"},
                ],
            )
            store = InMemoryOrchestrationStore()
            runtime = GroupRuntime(
                store=store,
                bus=InMemoryEventBus(),
                launch_backends={"scripted": backend},
                supervisor=DefaultWorkerSupervisor(store=store, launch_backends={"scripted": backend}),
            )

            record = await runtime.run_worker_assignment(
                _assignment(),
                policy=WorkerExecutionPolicy(
                    max_attempts=2,
                    allow_relaunch=True,
                    escalate_after_attempts=True,
                ),
            )
            stored = await store.get_worker_record("worker-scripted")

        self.assertEqual(record.status, WorkerStatus.FAILED)
        self.assertEqual(record.metadata["attempt_count"], 2)
        self.assertEqual(record.metadata["attempts"][-1]["decision"], "escalate")
        self.assertEqual(record.metadata["escalation"]["reason"], "attempts_exhausted")
        self.assertEqual(record.metadata["escalation"]["attempt_count"], 2)
        self.assertEqual(stored.metadata["escalation"]["backend"], "scripted")
        self.assertIn("provider still overloaded", stored.error_text)

    async def test_group_runtime_routes_to_fallback_backend_after_primary_provider_exhaustion(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            primary = _ScriptedBackend(
                backend_name="primary",
                root=root,
                launch_steps=[
                    {
                        "status": "failed",
                        "error_text": "provider overloaded",
                        "raw_payload": {
                            "failure_kind": "provider_unavailable",
                            "provider_name": "primary",
                        },
                    },
                    {
                        "status": "failed",
                        "error_text": "provider still overloaded",
                        "raw_payload": {
                            "failure_kind": "provider_unavailable",
                            "provider_name": "primary",
                        },
                    },
                ],
            )
            fallback = _ScriptedBackend(
                backend_name="fallback",
                root=root,
                launch_steps=[
                    {
                        "status": "completed",
                        "output_text": "fallback succeeded",
                        "raw_payload": {
                            "provider_name": "backup",
                        },
                    }
                ],
            )
            store = InMemoryOrchestrationStore()
            runtime = GroupRuntime(
                store=store,
                bus=InMemoryEventBus(),
                launch_backends={"primary": primary, "fallback": fallback},
                supervisor=DefaultWorkerSupervisor(
                    store=store,
                    launch_backends={"primary": primary, "fallback": fallback},
                ),
            )

            record = await runtime.run_worker_assignment(
                _assignment(backend="primary"),
                policy=WorkerExecutionPolicy(
                    max_attempts=2,
                    allow_relaunch=True,
                    escalate_after_attempts=True,
                    provider_fallbacks=(
                        WorkerProviderRoute(
                            route_id="backup",
                            backend="fallback",
                            metadata={"provider_name": "backup"},
                        ),
                    ),
                ),
            )

        self.assertEqual(record.status, WorkerStatus.COMPLETED)
        self.assertEqual(record.output_text, "fallback succeeded")
        self.assertEqual(primary.launch_count, 2)
        self.assertEqual(fallback.launch_count, 1)
        self.assertEqual(record.metadata["attempts"][1]["decision"], "fallback")
        self.assertEqual(record.metadata["provider_routing"]["route_history"][0]["route_id"], "primary")
        self.assertEqual(record.metadata["provider_routing"]["route_history"][1]["route_id"], "backup")

    async def test_group_runtime_applies_bounded_backoff_before_retry_and_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir, patch(
            "agent_orchestra.runtime.worker_supervisor.asyncio.sleep",
            new_callable=AsyncMock,
        ) as sleep_mock:
            root = Path(tmpdir)
            primary = _ScriptedBackend(
                backend_name="primary",
                root=root,
                launch_steps=[
                    {
                        "status": "failed",
                        "error_text": "provider overloaded",
                        "raw_payload": {
                            "failure_kind": "provider_unavailable",
                            "provider_name": "primary",
                        },
                    },
                    {
                        "status": "failed",
                        "error_text": "provider still overloaded",
                        "raw_payload": {
                            "failure_kind": "provider_unavailable",
                            "provider_name": "primary",
                        },
                    },
                ],
            )
            fallback = _ScriptedBackend(
                backend_name="fallback",
                root=root,
                launch_steps=[
                    {
                        "status": "completed",
                        "output_text": "fallback succeeded",
                        "raw_payload": {
                            "provider_name": "backup",
                        },
                    }
                ],
            )
            store = InMemoryOrchestrationStore()
            runtime = GroupRuntime(
                store=store,
                bus=InMemoryEventBus(),
                launch_backends={"primary": primary, "fallback": fallback},
                supervisor=DefaultWorkerSupervisor(
                    store=store,
                    launch_backends={"primary": primary, "fallback": fallback},
                ),
            )

            record = await runtime.run_worker_assignment(
                _assignment(backend="primary"),
                policy=WorkerExecutionPolicy(
                    max_attempts=2,
                    allow_relaunch=True,
                    backoff_seconds=0.25,
                    provider_unavailable_backoff_initial_seconds=15.0,
                    provider_unavailable_backoff_multiplier=2.0,
                    provider_unavailable_backoff_max_seconds=120.0,
                    escalate_after_attempts=True,
                    provider_fallbacks=(
                        WorkerProviderRoute(
                            route_id="backup",
                            backend="fallback",
                            metadata={"provider_name": "backup"},
                        ),
                    ),
                ),
            )

        self.assertEqual(record.status, WorkerStatus.COMPLETED)
        self.assertEqual(sleep_mock.await_args_list, [call(15.0), call(30.0)])

    async def test_group_runtime_uses_generic_backoff_for_non_provider_unavailable_failures(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir, patch(
            "agent_orchestra.runtime.worker_supervisor.asyncio.sleep",
            new_callable=AsyncMock,
        ) as sleep_mock:
            root = Path(tmpdir)
            backend = _ScriptedBackend(
                backend_name="scripted",
                root=root,
                launch_steps=[
                    {
                        "status": "failed",
                        "error_text": "implementation bug",
                        "raw_payload": {
                            "failure_kind": "ordinary",
                        },
                    },
                    {
                        "status": "completed",
                        "output_text": "retry after ordinary failure succeeded",
                    },
                ],
            )
            store = InMemoryOrchestrationStore()
            runtime = GroupRuntime(
                store=store,
                bus=InMemoryEventBus(),
                launch_backends={"scripted": backend},
                supervisor=DefaultWorkerSupervisor(store=store, launch_backends={"scripted": backend}),
            )

            record = await runtime.run_worker_assignment(
                _assignment(),
                policy=WorkerExecutionPolicy(
                    max_attempts=2,
                    allow_relaunch=True,
                    backoff_seconds=0.25,
                    provider_unavailable_backoff_initial_seconds=15.0,
                    provider_unavailable_backoff_multiplier=2.0,
                    provider_unavailable_backoff_max_seconds=120.0,
                ),
            )

        self.assertEqual(record.status, WorkerStatus.COMPLETED)
        self.assertEqual(sleep_mock.await_args_list, [call(0.25)])

    async def test_group_runtime_marks_provider_exhaustion_after_all_routes_are_exhausted(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            primary = _ScriptedBackend(
                backend_name="primary",
                root=root,
                launch_steps=[
                    {
                        "status": "failed",
                        "error_text": "provider overloaded",
                        "raw_payload": {
                            "failure_kind": "provider_unavailable",
                            "provider_name": "primary",
                        },
                    },
                    {
                        "status": "failed",
                        "error_text": "provider still overloaded",
                        "raw_payload": {
                            "failure_kind": "provider_unavailable",
                            "provider_name": "primary",
                        },
                    },
                ],
            )
            fallback = _ScriptedBackend(
                backend_name="fallback",
                root=root,
                launch_steps=[
                    {
                        "status": "failed",
                        "error_text": "backup provider overloaded",
                        "raw_payload": {
                            "failure_kind": "provider_unavailable",
                            "provider_name": "backup",
                        },
                    },
                    {
                        "status": "failed",
                        "error_text": "backup provider still overloaded",
                        "raw_payload": {
                            "failure_kind": "provider_unavailable",
                            "provider_name": "backup",
                        },
                    },
                ],
            )
            store = InMemoryOrchestrationStore()
            runtime = GroupRuntime(
                store=store,
                bus=InMemoryEventBus(),
                launch_backends={"primary": primary, "fallback": fallback},
                supervisor=DefaultWorkerSupervisor(
                    store=store,
                    launch_backends={"primary": primary, "fallback": fallback},
                ),
            )

            record = await runtime.run_worker_assignment(
                _assignment(backend="primary"),
                policy=WorkerExecutionPolicy(
                    max_attempts=2,
                    allow_relaunch=True,
                    escalate_after_attempts=True,
                    provider_fallbacks=(
                        WorkerProviderRoute(
                            route_id="backup",
                            backend="fallback",
                            metadata={"provider_name": "backup"},
                        ),
                    ),
                ),
            )

        self.assertEqual(record.status, WorkerStatus.FAILED)
        self.assertEqual(primary.launch_count, 2)
        self.assertEqual(fallback.launch_count, 2)
        self.assertEqual(record.metadata["escalation"]["reason"], "provider_exhausted")
        self.assertTrue(record.metadata["provider_routing"]["exhausted"])
        self.assertEqual(record.metadata["provider_routing"]["route_history"][-1]["route_id"], "backup")

    async def test_group_runtime_persists_provider_route_health_and_quarantines_exhausted_routes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            primary = _ScriptedBackend(
                backend_name="primary",
                root=root,
                launch_steps=[
                    {
                        "status": "failed",
                        "error_text": "provider overloaded",
                        "raw_payload": {
                            "failure_kind": "provider_unavailable",
                            "provider_name": "primary",
                        },
                    },
                    {
                        "status": "failed",
                        "error_text": "provider still overloaded",
                        "raw_payload": {
                            "failure_kind": "provider_unavailable",
                            "provider_name": "primary",
                        },
                    },
                ],
            )
            fallback = _ScriptedBackend(
                backend_name="fallback",
                root=root,
                launch_steps=[
                    {
                        "status": "completed",
                        "output_text": "fallback succeeded",
                        "raw_payload": {
                            "provider_name": "backup",
                        },
                    }
                ],
            )
            store = InMemoryOrchestrationStore()
            runtime = GroupRuntime(
                store=store,
                bus=InMemoryEventBus(),
                launch_backends={"primary": primary, "fallback": fallback},
                supervisor=DefaultWorkerSupervisor(
                    store=store,
                    launch_backends={"primary": primary, "fallback": fallback},
                ),
            )

            record = await runtime.run_worker_assignment(
                _assignment(
                    backend="primary",
                    metadata={"work_session_id": "worksession-provider-health"},
                ),
                policy=WorkerExecutionPolicy(
                    max_attempts=2,
                    allow_relaunch=True,
                    escalate_after_attempts=True,
                    provider_fallbacks=(
                        WorkerProviderRoute(
                            route_id="backup",
                            backend="fallback",
                            metadata={"provider_name": "backup"},
                        ),
                    ),
                ),
            )
            routes = await store.list_provider_route_health(role="teammate")

        self.assertEqual(record.status, WorkerStatus.COMPLETED)
        self.assertEqual(len(routes), 2)
        primary_route = next(route for route in routes if route.route_key == "teammate:primary")
        fallback_route = next(route for route in routes if route.route_key == "teammate:backup")
        self.assertEqual(primary_route.status, ProviderRouteStatus.QUARANTINED)
        self.assertEqual(primary_route.consecutive_failures, 2)
        self.assertIsNotNone(primary_route.cooldown_expires_at)
        self.assertEqual(primary_route.metadata["work_session_id"], "worksession-provider-health")
        self.assertEqual(fallback_route.status, ProviderRouteStatus.HEALTHY)
        self.assertEqual(fallback_route.consecutive_failures, 0)
        self.assertTrue(fallback_route.preferred)


class BackendResumeSemanticsTest(IsolatedAsyncioTestCase):
    async def test_subprocess_backend_resume_rejects_exited_process(self) -> None:
        backend = SubprocessLaunchBackend()
        handle = WorkerHandle(
            worker_id="worker-subprocess",
            role="teammate",
            backend="subprocess",
            run_id="assign-subprocess",
            metadata={"resume_supported": True, "_process": _FakeProcess(running=False)},
        )

        with self.assertRaisesRegex(RuntimeError, "subprocess process unavailable"):
            await backend.resume(handle, _assignment(backend="subprocess"))

    async def test_codex_cli_backend_resume_rejects_exited_process(self) -> None:
        backend = CodexCliLaunchBackend(codex_command=("codex",))
        handle = WorkerHandle(
            worker_id="worker-codex",
            role="teammate",
            backend="codex_cli",
            run_id="assign-codex",
            metadata={"resume_supported": True, "_process": _FakeProcess(running=False)},
        )

        with self.assertRaisesRegex(RuntimeError, "codex process unavailable"):
            await backend.resume(handle, _assignment(backend="codex_cli"))

    async def test_tmux_backend_resume_rejects_missing_session(self) -> None:
        runner = _FakeTmuxRunner(has_session_returncode=1)
        backend = TmuxLaunchBackend(command_runner=runner)
        handle = WorkerHandle(
            worker_id="worker-tmux",
            role="teammate",
            backend="tmux",
            run_id="assign-tmux",
            session_name="missing-session",
            metadata={"resume_supported": True},
        )

        with self.assertRaisesRegex(RuntimeError, "missing session"):
            await backend.resume(handle, _assignment(backend="tmux"))
