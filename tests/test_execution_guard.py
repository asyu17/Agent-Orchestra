from __future__ import annotations

import json
import shlex
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
    ExecutionGuardResult,
    ExecutionGuardStatus,
    VerificationCommandResult,
    WorkerAssignment,
    WorkerBackendCapabilities,
    WorkerHandle,
)
from agent_orchestra.contracts.enums import WorkerStatus
from agent_orchestra.runtime.group_runtime import GroupRuntime
from agent_orchestra.runtime.worker_supervisor import DefaultWorkerSupervisor
from agent_orchestra.storage.in_memory import InMemoryOrchestrationStore


class _GuardBackend:
    def __init__(
        self,
        *,
        root: Path,
        modified_files: dict[str, str],
        handle_metadata: dict[str, object] | None = None,
        raw_payload: dict[str, object] | None = None,
        status: str = "completed",
        output_text: str = "worker completed",
        error_text: str = "",
    ) -> None:
        self.root = root
        self.modified_files = dict(modified_files)
        self.handle_metadata = dict(handle_metadata or {})
        self.raw_payload = dict(raw_payload or {})
        self.status = status
        self.output_text = output_text
        self.error_text = error_text

    def describe_capabilities(self) -> WorkerBackendCapabilities:
        return WorkerBackendCapabilities(
            supports_protocol_contract=True,
            supports_protocol_state=True,
            supports_protocol_final_report=True,
            supports_resume=False,
            supports_reactivate=False,
            supports_artifact_progress=False,
            supports_verification_in_working_dir=True,
        )

    async def launch(self, assignment: WorkerAssignment) -> WorkerHandle:
        working_dir = Path(assignment.working_dir or self.root)
        for relative_path, content in self.modified_files.items():
            target = working_dir / relative_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")

        result_path = self.root / f"{assignment.assignment_id}.result.json"
        protocol_state_path = self.root / f"{assignment.assignment_id}.protocol.json"
        protocol_payload: dict[str, object] = {}
        if "protocol_events" in self.raw_payload:
            protocol_payload["protocol_events"] = self.raw_payload["protocol_events"]
        else:
            protocol_payload["protocol_events"] = []
        if "final_report" in self.raw_payload:
            protocol_payload["final_report"] = self.raw_payload["final_report"]
        protocol_state_path.write_text(json.dumps(protocol_payload), encoding="utf-8")
        result_path.write_text(
            json.dumps(
                {
                    "worker_id": assignment.worker_id,
                    "assignment_id": assignment.assignment_id,
                    "status": self.status,
                    "output_text": self.output_text if self.status == "completed" else "",
                    "error_text": self.error_text,
                    "response_id": None,
                    "usage": {},
                    "raw_payload": {"backend": "guarded", **self.raw_payload},
                }
            ),
            encoding="utf-8",
        )

        return WorkerHandle(
            worker_id=assignment.worker_id,
            role=assignment.role,
            backend="guarded",
            run_id=assignment.assignment_id,
            transport_ref=str(result_path),
            metadata={
                "resume_supported": False,
                "protocol_state_file": str(protocol_state_path),
                **self.handle_metadata,
            },
        )

    async def cancel(self, handle: WorkerHandle) -> None:
        return None

    async def resume(self, handle: WorkerHandle, assignment: WorkerAssignment | None = None) -> WorkerHandle:
        raise RuntimeError("resume unsupported in execution-guard tests")


def _assignment(
    *,
    working_dir: str,
    owned_paths: list[str] | None = None,
    verification_commands: list[str] | None = None,
    target_roots: list[str] | None = None,
) -> WorkerAssignment:
    return WorkerAssignment(
        assignment_id="assign-guard",
        worker_id="worker-guard",
        group_id="group-a",
        team_id="team-a",
        task_id="task-a",
        role="teammate",
        backend="guarded",
        instructions="Implement the guard path.",
        input_text="Run the assignment.",
        working_dir=working_dir,
        metadata={
            "owned_paths": list(owned_paths or []),
            "verification_commands": list(verification_commands or []),
            "target_roots": list(target_roots or []),
        },
    )


class ExecutionGuardContractsTest(TestCase):
    def test_guard_contracts_capture_authoritative_failure_evidence(self) -> None:
        verification = VerificationCommandResult(
            command="python3 -m unittest tests.test_execution_guard -v",
            returncode=0,
            stdout="ok",
            stderr="",
        )
        result = ExecutionGuardResult(
            status=ExecutionGuardStatus.PASSED,
            modified_paths=("src/agent_orchestra/runtime/group_runtime.py",),
            out_of_scope_paths=(),
            verification_results=(verification,),
            summary="guard passed",
            metadata={"working_dir": "/tmp/demo"},
        )

        self.assertEqual(result.status, ExecutionGuardStatus.PASSED)
        self.assertEqual(result.modified_paths[0], "src/agent_orchestra/runtime/group_runtime.py")
        self.assertEqual(result.verification_results[0].command, "python3 -m unittest tests.test_execution_guard -v")
        self.assertEqual(result.metadata["working_dir"], "/tmp/demo")


class ExecutionGuardRuntimeTest(IsolatedAsyncioTestCase):
    async def test_group_runtime_records_scope_drift_without_failing_assignment(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            backend = _GuardBackend(
                root=root,
                modified_files={
                    "owned.txt": "within scope\n",
                    "rogue.txt": "out of scope\n",
                },
            )
            store = InMemoryOrchestrationStore()
            runtime = GroupRuntime(
                store=store,
                bus=InMemoryEventBus(),
                launch_backends={"guarded": backend},
                supervisor=DefaultWorkerSupervisor(store=store, launch_backends={"guarded": backend}),
            )

            record = await runtime.run_worker_assignment(
                _assignment(working_dir=tmpdir, owned_paths=["owned.txt"])
            )
            stored = await store.get_worker_record("worker-guard")

        self.assertEqual(record.status, WorkerStatus.COMPLETED)
        self.assertEqual(record.metadata["guard_status"], "scope_drift")
        self.assertEqual(record.metadata["out_of_scope_paths"], ["rogue.txt"])
        self.assertIn("owned.txt", record.metadata["modified_paths"])
        self.assertIn("rogue.txt", record.metadata["modified_paths"])
        self.assertNotIn("rogue.txt", record.error_text)
        self.assertIsNotNone(stored)
        self.assertEqual(stored.metadata["guard_status"], "scope_drift")

    async def test_group_runtime_fails_when_modified_path_escapes_explicit_target_roots(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            backend = _GuardBackend(
                root=root,
                modified_files={
                    "target-root/owned.txt": "within target root\n",
                    "rogue.txt": "outside target root\n",
                },
            )
            store = InMemoryOrchestrationStore()
            runtime = GroupRuntime(
                store=store,
                bus=InMemoryEventBus(),
                launch_backends={"guarded": backend},
                supervisor=DefaultWorkerSupervisor(store=store, launch_backends={"guarded": backend}),
            )

            record = await runtime.run_worker_assignment(
                _assignment(
                    working_dir=tmpdir,
                    owned_paths=["target-root/owned.txt"],
                    target_roots=["target-root"],
                )
            )

        self.assertEqual(record.status, WorkerStatus.FAILED)
        self.assertEqual(record.metadata["guard_status"], "path_violation")
        self.assertEqual(record.metadata["out_of_scope_paths"], ["rogue.txt"])
        self.assertIn("escaped target_roots", record.error_text)

    async def test_group_runtime_fails_completed_attempt_when_verification_command_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            backend = _GuardBackend(root=root, modified_files={"owned.txt": "within scope\n"})
            store = InMemoryOrchestrationStore()
            runtime = GroupRuntime(
                store=store,
                bus=InMemoryEventBus(),
                launch_backends={"guarded": backend},
                supervisor=DefaultWorkerSupervisor(store=store, launch_backends={"guarded": backend}),
            )
            verification_command = (
                f"{shlex.quote(sys.executable)} -c 'import sys; sys.exit(5)'"
            )

            record = await runtime.run_worker_assignment(
                _assignment(
                    working_dir=tmpdir,
                    owned_paths=["owned.txt"],
                    verification_commands=[verification_command],
                )
            )

        self.assertEqual(record.status, WorkerStatus.FAILED)
        self.assertEqual(record.metadata["guard_status"], "verification_failed")
        self.assertEqual(record.metadata["out_of_scope_paths"], [])
        self.assertEqual(record.metadata["verification_results"][0]["command"], verification_command)
        self.assertEqual(record.metadata["verification_results"][0]["returncode"], 5)
        self.assertIn("failed verification", record.error_text.lower())

    async def test_group_runtime_accepts_equivalent_authoritative_protocol_verification_results(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            assignment_command = (
                f"{sys.executable}   -c   \"import sys; sys.exit(7)\""
            )
            backend = _GuardBackend(
                root=root,
                modified_files={"owned.txt": "within scope\n"},
                raw_payload={
                    "protocol_events": [
                        {
                            "event_id": "evt-accepted",
                            "assignment_id": "assign-guard",
                            "worker_id": "worker-guard",
                            "status": "accepted",
                            "phase": "accepted",
                        }
                    ],
                    "final_report": {
                        "assignment_id": "assign-guard",
                        "worker_id": "worker-guard",
                        "terminal_status": "completed",
                        "summary": "verification already passed",
                        "verification_results": [
                            {
                                "command": f"{shlex.quote(sys.executable)} -c 'import sys; sys.exit(7)'",
                                "returncode": 0,
                                "stdout": "authoritative ok",
                                "stderr": "",
                            }
                        ],
                    },
                },
            )
            store = InMemoryOrchestrationStore()
            runtime = GroupRuntime(
                store=store,
                bus=InMemoryEventBus(),
                launch_backends={"guarded": backend},
                supervisor=DefaultWorkerSupervisor(store=store, launch_backends={"guarded": backend}),
            )

            record = await runtime.run_worker_assignment(
                _assignment(
                    working_dir=tmpdir,
                    owned_paths=["owned.txt"],
                    verification_commands=[assignment_command],
                )
            )

        self.assertEqual(record.status, WorkerStatus.COMPLETED)
        self.assertEqual(record.metadata["guard_status"], "passed")
        self.assertEqual(record.metadata["verification_results"][0]["returncode"], 0)
        self.assertEqual(
            record.metadata["verification_results"][0]["stdout"],
            "authoritative ok",
        )

    async def test_group_runtime_accepts_authoritative_requested_command_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            requested_command = "uv run pytest tests/test_execution_guard.py -q"
            backend = _GuardBackend(
                root=root,
                modified_files={"owned.txt": "within scope\n"},
                raw_payload={
                    "protocol_events": [
                        {
                            "event_id": "evt-requested-command-map",
                            "assignment_id": "assign-guard",
                            "worker_id": "worker-guard",
                            "status": "accepted",
                            "phase": "accepted",
                        }
                    ],
                    "final_report": {
                        "assignment_id": "assign-guard",
                        "worker_id": "worker-guard",
                        "terminal_status": "completed",
                        "summary": "verification passed with teammate-selected fallback",
                        "verification_results": [
                            {
                                "requested_command": requested_command,
                                "command": ".venv/bin/pytest tests/test_execution_guard.py -q",
                                "returncode": 0,
                                "stdout": "authoritative fallback ok",
                                "stderr": "",
                            }
                        ],
                    },
                },
            )
            store = InMemoryOrchestrationStore()
            runtime = GroupRuntime(
                store=store,
                bus=InMemoryEventBus(),
                launch_backends={"guarded": backend},
                supervisor=DefaultWorkerSupervisor(store=store, launch_backends={"guarded": backend}),
            )

            record = await runtime.run_worker_assignment(
                _assignment(
                    working_dir=tmpdir,
                    owned_paths=["owned.txt"],
                    verification_commands=[requested_command],
                )
            )

        self.assertEqual(record.status, WorkerStatus.COMPLETED)
        self.assertEqual(record.metadata["guard_status"], "passed")
        self.assertEqual(record.metadata["verification_results"][0]["requested_command"], requested_command)
        self.assertEqual(
            record.metadata["verification_results"][0]["command"],
            ".venv/bin/pytest tests/test_execution_guard.py -q",
        )

    async def test_group_runtime_reruns_verification_when_authoritative_result_is_not_equivalent(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            verification_command = (
                f"{shlex.quote(sys.executable)} -c 'import sys; sys.exit(6)'"
            )
            backend = _GuardBackend(
                root=root,
                modified_files={"owned.txt": "within scope\n"},
                raw_payload={
                    "protocol_events": [
                        {
                            "event_id": "evt-accepted",
                            "assignment_id": "assign-guard",
                            "worker_id": "worker-guard",
                            "status": "accepted",
                            "phase": "accepted",
                        }
                    ],
                    "final_report": {
                        "assignment_id": "assign-guard",
                        "worker_id": "worker-guard",
                        "terminal_status": "completed",
                        "summary": "reported a different verification command",
                        "verification_results": [
                            {
                                "command": "python3 -m pytest tests/test_execution_guard.py -q",
                                "returncode": 0,
                                "stdout": "different command",
                                "stderr": "",
                            }
                        ],
                    },
                },
            )
            store = InMemoryOrchestrationStore()
            runtime = GroupRuntime(
                store=store,
                bus=InMemoryEventBus(),
                launch_backends={"guarded": backend},
                supervisor=DefaultWorkerSupervisor(store=store, launch_backends={"guarded": backend}),
            )

            record = await runtime.run_worker_assignment(
                _assignment(
                    working_dir=tmpdir,
                    owned_paths=["owned.txt"],
                    verification_commands=[verification_command],
                )
            )

        self.assertEqual(record.status, WorkerStatus.FAILED)
        self.assertEqual(record.metadata["guard_status"], "verification_failed")
        self.assertEqual(record.metadata["verification_results"][0]["command"], verification_command)
        self.assertEqual(record.metadata["verification_results"][0]["returncode"], 6)

    async def test_group_runtime_ignores_preexisting_spool_root_artifacts_when_diffing_snapshots(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            spool_root = root / "docs" / "superpowers" / "runs" / "codex-spool"
            spool_root.mkdir(parents=True, exist_ok=True)
            (spool_root / "stale.prompt.md").write_text("old prompt\n", encoding="utf-8")
            backend = _GuardBackend(
                root=root,
                modified_files={"owned.txt": "within scope\n"},
                handle_metadata={"spool_root": str(spool_root)},
            )
            store = InMemoryOrchestrationStore()
            runtime = GroupRuntime(
                store=store,
                bus=InMemoryEventBus(),
                launch_backends={"guarded": backend},
                supervisor=DefaultWorkerSupervisor(store=store, launch_backends={"guarded": backend}),
            )

            record = await runtime.run_worker_assignment(
                _assignment(working_dir=tmpdir, owned_paths=["owned.txt"])
            )

        self.assertEqual(record.status, WorkerStatus.COMPLETED)
        self.assertEqual(record.metadata["guard_status"], "passed")
        self.assertEqual(record.metadata["out_of_scope_paths"], [])
        self.assertEqual(record.metadata["modified_paths"], ["owned.txt"])

    async def test_group_runtime_normalizes_owned_paths_through_repository_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            docs_resource = root / "docs" / "resource"
            docs_resource.mkdir(parents=True, exist_ok=True)
            (root / "resource").symlink_to(docs_resource, target_is_directory=True)
            backend = _GuardBackend(
                root=root,
                modified_files={"docs/resource/knowledge/allowed.md": "within scope\n"},
            )
            store = InMemoryOrchestrationStore()
            runtime = GroupRuntime(
                store=store,
                bus=InMemoryEventBus(),
                launch_backends={"guarded": backend},
                supervisor=DefaultWorkerSupervisor(store=store, launch_backends={"guarded": backend}),
            )

            record = await runtime.run_worker_assignment(
                _assignment(
                    working_dir=tmpdir,
                    owned_paths=["resource/knowledge/allowed.md"],
                )
            )

        self.assertEqual(record.status, WorkerStatus.COMPLETED)
        self.assertEqual(record.metadata["guard_status"], "passed")
        self.assertEqual(record.metadata["out_of_scope_paths"], [])
        self.assertEqual(record.metadata["modified_paths"], ["docs/resource/knowledge/allowed.md"])

    async def test_group_runtime_ignores_python_cache_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            backend = _GuardBackend(
                root=root,
                modified_files={
                    "owned.txt": "within scope\n",
                    "src/__pycache__/module.cpython-314.pyc": "compiled\n",
                },
            )
            store = InMemoryOrchestrationStore()
            runtime = GroupRuntime(
                store=store,
                bus=InMemoryEventBus(),
                launch_backends={"guarded": backend},
                supervisor=DefaultWorkerSupervisor(store=store, launch_backends={"guarded": backend}),
            )

            record = await runtime.run_worker_assignment(
                _assignment(working_dir=tmpdir, owned_paths=["owned.txt"])
            )

        self.assertEqual(record.status, WorkerStatus.COMPLETED)
        self.assertEqual(record.metadata["guard_status"], "passed")
        self.assertEqual(record.metadata["out_of_scope_paths"], [])
        self.assertEqual(record.metadata["modified_paths"], ["owned.txt"])

    async def test_group_runtime_ignores_pytest_cache_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            backend = _GuardBackend(
                root=root,
                modified_files={
                    "owned.txt": "within scope\n",
                    ".pytest_cache/v/cache/nodeids": "[]\n",
                },
            )
            store = InMemoryOrchestrationStore()
            runtime = GroupRuntime(
                store=store,
                bus=InMemoryEventBus(),
                launch_backends={"guarded": backend},
                supervisor=DefaultWorkerSupervisor(store=store, launch_backends={"guarded": backend}),
            )

            record = await runtime.run_worker_assignment(
                _assignment(working_dir=tmpdir, owned_paths=["owned.txt"])
            )

        self.assertEqual(record.status, WorkerStatus.COMPLETED)
        self.assertEqual(record.metadata["guard_status"], "passed")
        self.assertEqual(record.metadata["out_of_scope_paths"], [])
        self.assertEqual(record.metadata["modified_paths"], ["owned.txt"])
