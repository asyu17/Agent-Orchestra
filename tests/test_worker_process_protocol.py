from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from unittest import IsolatedAsyncioTestCase, TestCase

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agent_orchestra.bus.in_memory import InMemoryEventBus
from agent_orchestra.contracts.execution import (
    LaunchBackend,
    WorkerAssignment,
    WorkerBackendCapabilities,
    WorkerHandle,
    WorkerSession,
    WorkerSessionStatus,
    WorkerTransportLocator,
)
from agent_orchestra.runtime.backends.base import CommandResult
from agent_orchestra.runtime.backends.subprocess_backend import SubprocessLaunchBackend
from agent_orchestra.runtime.backends.tmux_backend import TmuxLaunchBackend
from agent_orchestra.runtime.group_runtime import GroupRuntime
from agent_orchestra.runtime.worker_supervisor import DefaultWorkerSupervisor
from agent_orchestra.runtime.worker_process import run_once
from agent_orchestra.storage.in_memory import InMemoryOrchestrationStore


def _protocol_metadata() -> dict[str, object]:
    return {
        "execution_contract": {
            "contract_id": "contract-v1",
            "mode": "code_edit",
            "allow_subdelegation": False,
            "require_final_report": True,
            "require_verification_results": False,
            "required_artifact_kinds": [],
        },
        "lease_policy": {
            "accept_deadline_seconds": 30.0,
            "renewal_timeout_seconds": 60.0,
            "hard_deadline_seconds": 300.0,
            "renew_on_event_kinds": ("accepted", "checkpoint", "phase_changed", "verifying"),
        },
    }


class WorkerProcessProtocolTest(TestCase):
    def test_run_once_keeps_legacy_result_only_behavior_without_protocol_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            assignment_path = Path(tmpdir) / "assignment.json"
            result_path = Path(tmpdir) / "result.json"
            assignment_path.write_text(
                json.dumps(
                    {
                        "assignment_id": "assign-legacy",
                        "worker_id": "worker-legacy",
                        "backend": "subprocess",
                        "role": "teammate",
                        "metadata": {"simulated_output": "legacy done"},
                    }
                ),
                encoding="utf-8",
            )

            result = run_once(str(assignment_path), str(result_path))

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["output_text"], "legacy done")
        self.assertNotIn("protocol_events", result["raw_payload"])
        self.assertNotIn("final_report", result["raw_payload"])

    def test_run_once_writes_protocol_state_with_accepted_checkpoint_and_final_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            assignment_path = Path(tmpdir) / "assignment.json"
            result_path = Path(tmpdir) / "result.json"
            protocol_path = Path(tmpdir) / "protocol.json"
            assignment_path.write_text(
                json.dumps(
                    {
                        "assignment_id": "assign-protocol",
                        "worker_id": "worker-protocol",
                        "backend": "subprocess",
                        "role": "teammate",
                        "metadata": {
                            "simulated_output": "protocol done",
                            **_protocol_metadata(),
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            result = run_once(
                str(assignment_path),
                str(result_path),
                protocol_state_file=str(protocol_path),
            )
            protocol_state = json.loads(protocol_path.read_text(encoding="utf-8"))

        self.assertEqual(result["status"], "completed")
        events = protocol_state.get("protocol_events", [])
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["status"], "accepted")
        self.assertEqual(events[1]["kind"], "checkpoint")
        self.assertEqual(protocol_state["final_report"]["terminal_status"], "completed")

    def test_worker_transport_locator_supports_handle_snapshot_aliases_for_reattach(self) -> None:
        locator = WorkerTransportLocator.from_dict(
            {
                "backend": "subprocess",
                "process_id": 1234,
                "transport_ref": "/tmp/reattach.result.json",
                "metadata": {"protocol_state_file": "/tmp/reattach.protocol.json"},
            }
        )

        self.assertEqual(locator.pid, 1234)
        self.assertEqual(locator.result_file, "/tmp/reattach.result.json")
        self.assertEqual(locator.protocol_state_file, "/tmp/reattach.protocol.json")


class _FakeTmuxRunner:
    def __init__(self) -> None:
        self.commands: list[tuple[list[str], str | None]] = []

    def __call__(self, command: list[str], cwd: str | None) -> CommandResult:
        self.commands.append((command, cwd))
        return CommandResult(returncode=0, stdout="", stderr="")


class _NoReattachBackend(LaunchBackend):
    async def launch(self, assignment: WorkerAssignment) -> WorkerHandle:
        return WorkerHandle(
            worker_id=assignment.worker_id,
            role=assignment.role,
            backend=assignment.backend,
            run_id=assignment.assignment_id,
        )

    async def cancel(self, handle: WorkerHandle) -> None:
        return None

    async def resume(self, handle: WorkerHandle, assignment: WorkerAssignment | None = None) -> WorkerHandle:
        return handle


class _ReattachCapableBackend:
    def describe_capabilities(self) -> WorkerBackendCapabilities:
        return WorkerBackendCapabilities(
            supports_resume=True,
            supports_reactivate=False,
            supports_reattach=True,
        )

    async def launch(self, assignment: WorkerAssignment) -> WorkerHandle:
        return WorkerHandle(
            worker_id=assignment.worker_id,
            role=assignment.role,
            backend=assignment.backend,
            run_id=assignment.assignment_id,
            transport_ref="/tmp/fake.result.json",
        )

    async def cancel(self, handle: WorkerHandle) -> None:
        return None

    async def resume(self, handle: WorkerHandle, assignment: WorkerAssignment | None = None) -> WorkerHandle:
        return handle


class BackendProtocolHandleTest(IsolatedAsyncioTestCase):
    async def test_launch_backend_default_reattach_path_is_unsupported(self) -> None:
        backend = _NoReattachBackend()
        assignment = WorkerAssignment(
            assignment_id="assign-no-reattach",
            worker_id="worker-no-reattach",
            group_id="group-a",
            team_id="team-a",
            task_id="task-a",
            role="teammate",
            backend="subprocess",
            instructions="Do the work.",
            input_text="run",
        )
        with self.assertRaisesRegex(RuntimeError, "does not support reattach"):
            await backend.reattach(WorkerTransportLocator(backend="subprocess"), assignment)

    async def test_subprocess_launch_materializes_protocol_state_file_when_contract_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            backend = SubprocessLaunchBackend(spool_root=tmpdir)
            assignment = WorkerAssignment(
                assignment_id="assign-subprocess-protocol",
                worker_id="worker-subprocess-protocol",
                group_id="group-a",
                team_id="team-a",
                task_id="task-a",
                role="teammate",
                backend="subprocess",
                instructions="Do the work.",
                input_text="run",
                metadata={
                    "simulated_output": "subprocess protocol done",
                    **_protocol_metadata(),
                },
            )
            handle = await backend.launch(assignment)
            process = handle.metadata["_process"]
            process.communicate(timeout=5)

            protocol_state_file = handle.metadata.get("protocol_state_file")
            self.assertIsInstance(protocol_state_file, str)
            protocol_state = json.loads(Path(protocol_state_file).read_text(encoding="utf-8"))

        self.assertIn("protocol_events", protocol_state)
        self.assertIn("final_report", protocol_state)
        self.assertEqual(protocol_state["final_report"]["terminal_status"], "completed")

    async def test_subprocess_launch_keeps_legacy_handle_without_protocol_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            backend = SubprocessLaunchBackend(spool_root=tmpdir)
            assignment = WorkerAssignment(
                assignment_id="assign-subprocess-legacy",
                worker_id="worker-subprocess-legacy",
                group_id="group-a",
                team_id="team-a",
                task_id="task-a",
                role="teammate",
                backend="subprocess",
                instructions="Do the work.",
                input_text="run",
            )
            handle = await backend.launch(assignment)
            process = handle.metadata["_process"]
            process.communicate(timeout=5)

        self.assertNotIn("protocol_state_file", handle.metadata)

    async def test_tmux_launch_exposes_protocol_state_file_and_flag_when_contract_present(self) -> None:
        runner = _FakeTmuxRunner()
        with tempfile.TemporaryDirectory() as tmpdir:
            backend = TmuxLaunchBackend(
                command_runner=runner,
                spool_root=tmpdir,
                session_prefix="demo",
            )
            assignment = WorkerAssignment(
                assignment_id="assign-tmux-protocol",
                worker_id="worker-tmux-protocol",
                group_id="group-a",
                team_id="team-a",
                task_id="task-a",
                role="leader",
                backend="tmux",
                instructions="Do the work.",
                input_text="run",
                metadata=_protocol_metadata(),
            )
            handle = await backend.launch(assignment)

        self.assertIn("protocol_state_file", handle.metadata)
        command = runner.commands[0][0]
        self.assertEqual(command[:4], ["tmux", "new-session", "-d", "-s"])
        self.assertIn("--protocol-state-file", command[-1])

    async def test_tmux_launch_keeps_legacy_command_without_protocol_contract(self) -> None:
        runner = _FakeTmuxRunner()
        with tempfile.TemporaryDirectory() as tmpdir:
            backend = TmuxLaunchBackend(
                command_runner=runner,
                spool_root=tmpdir,
                session_prefix="demo",
            )
            assignment = WorkerAssignment(
                assignment_id="assign-tmux-legacy",
                worker_id="worker-tmux-legacy",
                group_id="group-a",
                team_id="team-a",
                task_id="task-a",
                role="leader",
                backend="tmux",
                instructions="Do the work.",
                input_text="run",
            )
            handle = await backend.launch(assignment)

        self.assertNotIn("protocol_state_file", handle.metadata)
        command = runner.commands[0][0]
        self.assertNotIn("--protocol-state-file", command[-1])

    async def test_group_runtime_launch_propagates_reattach_supported_metadata(self) -> None:
        backend = _ReattachCapableBackend()
        runtime = GroupRuntime(
            store=InMemoryOrchestrationStore(),
            bus=InMemoryEventBus(),
            launch_backends={"subprocess": backend},
        )
        handle = await runtime.launch_worker(
            WorkerAssignment(
                assignment_id="assign-runtime-reattach-capability",
                worker_id="worker-runtime-reattach-capability",
                group_id="group-a",
                team_id="team-a",
                task_id="task-a",
                role="teammate",
                backend="subprocess",
                instructions="Do the work.",
                input_text="run",
            )
        )

        self.assertEqual(handle.metadata.get("reattach_supported"), True)
        backend_capabilities = handle.metadata.get("backend_capabilities")
        self.assertIsInstance(backend_capabilities, dict)
        assert isinstance(backend_capabilities, dict)
        self.assertEqual(backend_capabilities.get("supports_reattach"), True)

    async def test_supervisor_restores_reattach_supported_from_persisted_snapshot(self) -> None:
        supervisor = DefaultWorkerSupervisor(
            store=InMemoryOrchestrationStore(),
            launch_backends={"subprocess": _ReattachCapableBackend()},
        )
        session = WorkerSession(
            session_id="session-reattach-hydrate",
            worker_id="worker-reattach-hydrate",
            assignment_id="assign-reattach-hydrate",
            backend="subprocess",
            role="teammate",
            status=WorkerSessionStatus.IDLE,
            handle_snapshot={
                "backend": "subprocess",
                "transport_ref": "/tmp/reattach.result.json",
                "metadata": {"resume_supported": True},
            },
        )

        handle = supervisor._handle_from_session(session)

        self.assertEqual(handle.metadata.get("reattach_supported"), True)
