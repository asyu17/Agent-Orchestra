from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest import IsolatedAsyncioTestCase, TestCase

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agent_orchestra.bus.in_memory import InMemoryEventBus
from agent_orchestra.contracts.execution import WorkerAssignment, WorkerExecutionPolicy, WorkerTransportLocator
from agent_orchestra.contracts.enums import WorkerStatus
from agent_orchestra.contracts.worker_protocol import WorkerExecutionContract, WorkerLeasePolicy
from agent_orchestra.runtime.backends import (
    BackendRegistry,
    CodexCliLaunchBackend,
    CommandResult,
    InProcessLaunchBackend,
    SubprocessLaunchBackend,
    TmuxLaunchBackend,
)
from agent_orchestra.runtime.group_runtime import GroupRuntime
from agent_orchestra.runtime.worker_supervisor import DefaultWorkerSupervisor
from agent_orchestra.storage.in_memory import InMemoryOrchestrationStore


class _FakeTmuxRunner:
    def __init__(self) -> None:
        self.commands: list[tuple[list[str], str | None]] = []

    def __call__(self, command: list[str], cwd: str | None) -> CommandResult:
        self.commands.append((command, cwd))
        return CommandResult(returncode=0)


def _assignment(backend: str) -> WorkerAssignment:
    return WorkerAssignment(
        assignment_id=f"assign-{backend}",
        worker_id=f"worker-{backend}",
        group_id="group-a",
        team_id="team-a",
        task_id="task-a",
        role="leader",
        backend=backend,
        instructions="Do the work.",
        input_text="run",
    )


class BackendRegistryTest(TestCase):
    def test_registry_returns_four_concrete_backends(self) -> None:
        registry = BackendRegistry(
            {
                "in_process": InProcessLaunchBackend(),
                "subprocess": SubprocessLaunchBackend(),
                "tmux": TmuxLaunchBackend(command_runner=_FakeTmuxRunner()),
                "codex_cli": CodexCliLaunchBackend(codex_command=("codex",)),
            }
        )

        self.assertEqual(registry.names(), ("in_process", "subprocess", "tmux", "codex_cli"))
        self.assertIsInstance(registry.get("in_process"), InProcessLaunchBackend)
        self.assertIsInstance(registry.get("subprocess"), SubprocessLaunchBackend)
        self.assertIsInstance(registry.get("tmux"), TmuxLaunchBackend)
        self.assertIsInstance(registry.get("codex_cli"), CodexCliLaunchBackend)

    def test_codex_cli_backend_declares_protocol_capabilities(self) -> None:
        backend = CodexCliLaunchBackend(codex_command=("codex",))

        capabilities = backend.describe_capabilities()

        self.assertTrue(capabilities.supports_protocol_state)
        self.assertTrue(capabilities.supports_protocol_final_report)
        self.assertTrue(capabilities.supports_artifact_progress)

    def test_subprocess_backend_declares_protocol_capabilities(self) -> None:
        backend = SubprocessLaunchBackend()

        capabilities = backend.describe_capabilities()

        self.assertTrue(capabilities.supports_protocol_contract)
        self.assertTrue(capabilities.supports_protocol_state)
        self.assertTrue(capabilities.supports_protocol_final_report)
        self.assertTrue(capabilities.supports_artifact_progress)

    def test_process_backends_declare_reattach_capability(self) -> None:
        subprocess_backend = SubprocessLaunchBackend()
        tmux_backend = TmuxLaunchBackend(command_runner=_FakeTmuxRunner())
        codex_backend = CodexCliLaunchBackend(codex_command=("codex",))

        self.assertTrue(subprocess_backend.describe_capabilities().supports_reattach)
        self.assertTrue(tmux_backend.describe_capabilities().supports_reattach)
        self.assertTrue(codex_backend.describe_capabilities().supports_reattach)

    def test_backends_declare_resident_transport_class_split(self) -> None:
        in_process_backend = InProcessLaunchBackend()
        subprocess_backend = SubprocessLaunchBackend()
        tmux_backend = TmuxLaunchBackend(command_runner=_FakeTmuxRunner())
        codex_backend = CodexCliLaunchBackend(codex_command=("codex",))

        self.assertEqual(
            in_process_backend.describe_capabilities().to_dict().get("transport_class"),
            "full_resident_transport",
        )
        self.assertEqual(
            tmux_backend.describe_capabilities().to_dict().get("transport_class"),
            "full_resident_transport",
        )
        self.assertEqual(
            subprocess_backend.describe_capabilities().to_dict().get("transport_class"),
            "ephemeral_worker_transport",
        )
        self.assertEqual(
            codex_backend.describe_capabilities().to_dict().get("transport_class"),
            "ephemeral_worker_transport",
        )


class BackendLaunchTest(IsolatedAsyncioTestCase):
    def _write_fake_codex_script(self, path: Path) -> None:
        path.write_text(
            "\n".join(
                [
                    "import json",
                    "import os",
                    "import sys",
                    "from pathlib import Path",
                    "",
                    "def _value(flag: str) -> str | None:",
                    "    if flag not in sys.argv:",
                    "        return None",
                    "    index = sys.argv.index(flag)",
                    "    if index + 1 >= len(sys.argv):",
                    "        return None",
                    "    return sys.argv[index + 1]",
                    "",
                    "prompt = sys.stdin.read()",
                    "last_message = _value('--output-last-message')",
                    "sleep_seconds = os.environ.get('FAKE_CODEX_SLEEP_SECONDS')",
                    "if sleep_seconds is not None:",
                    "    import time",
                    "    time.sleep(float(sleep_seconds))",
                    "custom_last_message = os.environ.get('FAKE_CODEX_LAST_MESSAGE')",
                    "custom_stderr = os.environ.get('FAKE_CODEX_STDERR')",
                    "skip_last_message = os.environ.get('FAKE_CODEX_SKIP_LAST_MESSAGE') == '1'",
                    "if not skip_last_message:",
                    "    if custom_last_message is not None:",
                    "        Path(last_message).write_text(custom_last_message, encoding='utf-8')",
                    "    else:",
                    "        Path(last_message).write_text(f'codex-done\\n{prompt}', encoding='utf-8')",
                    "sys.stdout.write(json.dumps({'type': 'thread.started', 'thread_id': 'thread-123'}) + '\\n')",
                    "sys.stdout.write(json.dumps({'type': 'turn.completed'}) + '\\n')",
                    "sys.stdout.flush()",
                    "if os.environ.get('FAKE_CODEX_FAIL') == '1':",
                    "    sys.stderr.write((custom_stderr if custom_stderr is not None else 'codex failed on purpose') + '\\n')",
                    "    sys.stderr.flush()",
                    "    raise SystemExit(17)",
                    "sys.stderr.write('codex warning\\n')",
                    "sys.stderr.flush()",
                ]
            ),
            encoding="utf-8",
        )

    async def test_in_process_backend_launches_handle(self) -> None:
        backend = InProcessLaunchBackend()

        handle = await backend.launch(_assignment("in_process"))

        self.assertEqual(handle.backend, "in_process")
        self.assertEqual(handle.run_id, "assign-in_process")
        self.assertEqual(handle.metadata.get("transport_class"), "full_resident_transport")

    async def test_subprocess_backend_reattach_rebuilds_protocol_handle(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            result_file = root / "assign-subprocess-reattach.result.json"
            protocol_state_file = root / "assign-subprocess-reattach.protocol.json"
            stdout_file = root / "assign-subprocess-reattach.stdout.jsonl"
            stderr_file = root / "assign-subprocess-reattach.stderr.log"
            last_message_file = root / "assign-subprocess-reattach.last_message.txt"
            for path in (result_file, protocol_state_file, stdout_file, stderr_file, last_message_file):
                path.write_text("", encoding="utf-8")
            backend = SubprocessLaunchBackend(spool_root=tmpdir)
            locator = WorkerTransportLocator(
                backend="subprocess",
                working_dir=tmpdir,
                spool_dir=tmpdir,
                protocol_state_file=str(protocol_state_file),
                result_file=str(result_file),
                stdout_file=str(stdout_file),
                stderr_file=str(stderr_file),
                last_message_file=str(last_message_file),
                pid=os.getpid(),
                command_fingerprint="fp-subprocess",
            )

            handle = await backend.reattach(locator, _assignment("subprocess"))

        self.assertEqual(handle.process_id, os.getpid())
        self.assertEqual(handle.transport_ref, str(result_file))
        self.assertEqual(handle.metadata["protocol_state_file"], str(protocol_state_file))
        self.assertEqual(handle.metadata["reattach"], True)
        self.assertEqual(handle.metadata.get("transport_class"), "ephemeral_worker_transport")

    async def test_tmux_backend_reattach_rebuilds_protocol_handle(self) -> None:
        runner = _FakeTmuxRunner()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            result_file = root / "assign-tmux-reattach.result.json"
            protocol_state_file = root / "assign-tmux-reattach.protocol.json"
            result_file.write_text("", encoding="utf-8")
            protocol_state_file.write_text("", encoding="utf-8")
            backend = TmuxLaunchBackend(command_runner=runner, spool_root=tmpdir, session_prefix="demo")
            locator = WorkerTransportLocator(
                backend="tmux",
                working_dir=tmpdir,
                spool_dir=tmpdir,
                protocol_state_file=str(protocol_state_file),
                result_file=str(result_file),
                session_name="demo-worker-tmux-assign-tmux",
                pane_id="%1",
            )

            handle = await backend.reattach(locator, _assignment("tmux"))

        self.assertEqual(handle.session_name, "demo-worker-tmux-assign-tmux")
        self.assertEqual(handle.transport_ref, str(result_file))
        self.assertEqual(handle.metadata["protocol_state_file"], str(protocol_state_file))
        self.assertEqual(handle.metadata["reattach"], True)
        self.assertEqual(handle.metadata.get("transport_class"), "full_resident_transport")

    async def test_codex_cli_backend_reattach_rebuilds_protocol_handle(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            result_file = root / "assign-codex-reattach.result.json"
            protocol_state_file = root / "assign-codex-reattach.protocol.json"
            stdout_file = root / "assign-codex-reattach.stdout.jsonl"
            stderr_file = root / "assign-codex-reattach.stderr.log"
            last_message_file = root / "assign-codex-reattach.last_message.txt"
            for path in (result_file, protocol_state_file, stdout_file, stderr_file, last_message_file):
                path.write_text("", encoding="utf-8")
            backend = CodexCliLaunchBackend(codex_command=("codex",), spool_root=tmpdir)
            locator = WorkerTransportLocator(
                backend="codex_cli",
                working_dir=tmpdir,
                spool_dir=tmpdir,
                protocol_state_file=str(protocol_state_file),
                result_file=str(result_file),
                stdout_file=str(stdout_file),
                stderr_file=str(stderr_file),
                last_message_file=str(last_message_file),
                pid=os.getpid(),
                command_fingerprint="fp-codex",
                metadata={"command_fingerprint": "fp-codex"},
            )
            assignment = _assignment("codex_cli")
            assignment.working_dir = tmpdir
            assignment.metadata["command_fingerprint"] = "fp-codex"

            handle = await backend.reattach(locator, assignment)

        self.assertEqual(handle.process_id, os.getpid())
        self.assertEqual(handle.transport_ref, str(result_file))
        self.assertEqual(handle.metadata["protocol_state_file"], str(protocol_state_file))
        self.assertEqual(handle.metadata["reattach"], True)
        self.assertEqual(handle.metadata.get("transport_class"), "ephemeral_worker_transport")

    async def test_subprocess_backend_launches_worker_harness_and_writes_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            backend = SubprocessLaunchBackend(spool_root=tmpdir)
            handle = await backend.launch(
                WorkerAssignment(
                    assignment_id="assign-subprocess",
                    worker_id="worker-subprocess",
                    group_id="group-a",
                    team_id="team-a",
                    task_id="task-a",
                    role="leader",
                    backend="subprocess",
                    instructions="Do the work.",
                    input_text="run",
                    metadata={"simulated_output": "subprocess-done"},
                )
            )
            process = handle.metadata["_process"]
            process.communicate(timeout=5)

            raw_result = Path(handle.transport_ref).read_text(encoding="utf-8")
            result = json.loads(raw_result)

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["output_text"], "subprocess-done")
        self.assertIn("subprocess-done", raw_result)

    async def test_subprocess_backend_result_json_preserves_utf8_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            backend = SubprocessLaunchBackend(spool_root=tmpdir)
            handle = await backend.launch(
                WorkerAssignment(
                    assignment_id="assign-subprocess-utf8",
                    worker_id="worker-subprocess-utf8",
                    group_id="group-a",
                    team_id="team-a",
                    task_id="task-a",
                    role="leader",
                    backend="subprocess",
                    instructions="处理中文输出。",
                    input_text="运行",
                    metadata={"simulated_output": "中文完成"},
                )
            )
            process = handle.metadata["_process"]
            process.communicate(timeout=5)

            raw_assignment = Path(handle.metadata["assignment_file"]).read_text(encoding="utf-8")
            raw_result = Path(handle.transport_ref).read_text(encoding="utf-8")

        self.assertIn("处理中文输出。", raw_assignment)
        self.assertNotIn("\\u5904\\u7406", raw_assignment)
        self.assertIn("中文完成", raw_result)
        self.assertNotIn("\\u4e2d\\u6587", raw_result)

    async def test_tmux_backend_builds_deterministic_command(self) -> None:
        runner = _FakeTmuxRunner()
        with tempfile.TemporaryDirectory() as tmpdir:
            backend = TmuxLaunchBackend(command_runner=runner, spool_root=tmpdir, session_prefix="demo")
            handle = await backend.launch(
                WorkerAssignment(
                    assignment_id="assign-tmux",
                    worker_id="worker-tmux",
                    group_id="group-a",
                    team_id="team-a",
                    task_id="task-a",
                    role="leader",
                    backend="tmux",
                    instructions="请在 tmux 中处理中文。",
                    input_text="继续执行。",
                )
            )
            raw_assignment = Path(handle.metadata["assignment_file"]).read_text(encoding="utf-8")

        self.assertEqual(handle.backend, "tmux")
        self.assertEqual(handle.session_name, "demo-worker-tmux-assign-tmux")
        self.assertEqual(runner.commands[0][0][:4], ["tmux", "new-session", "-d", "-s"])
        self.assertIn("请在 tmux 中处理中文。", raw_assignment)
        self.assertNotIn("\\u8bf7", raw_assignment)

    async def test_codex_cli_backend_materializes_completed_result_for_supervisor(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_codex = Path(tmpdir) / "fake_codex.py"
            self._write_fake_codex_script(fake_codex)

            backend = CodexCliLaunchBackend(
                codex_command=(sys.executable, str(fake_codex)),
                spool_root=tmpdir,
                bypass_approvals=False,
                sandbox_mode="workspace-write",
            )
            store = InMemoryOrchestrationStore()
            supervisor = DefaultWorkerSupervisor(store=store, launch_backends={"codex_cli": backend})
            assignment = WorkerAssignment(
                assignment_id="assign-codex",
                worker_id="worker-codex",
                group_id="group-a",
                team_id="team-a",
                task_id="task-a",
                role="teammate",
                backend="codex_cli",
                instructions="Edit the runtime.",
                input_text="Implement the next slice.",
                working_dir=tmpdir,
                metadata={
                    "owned_paths": ["src/agent_orchestra/runtime/backends/codex_cli_backend.py"],
                    "verification_commands": ["python3 -m unittest tests.test_backends -v"],
                },
            )

            handle = await backend.launch(assignment)
            await supervisor.start(handle, assignment)
            record = await supervisor.wait(handle, timeout_seconds=5.0)

            prompt = Path(handle.metadata["prompt_file"]).read_text(encoding="utf-8")

        self.assertEqual(record.status, WorkerStatus.COMPLETED)
        self.assertIn("codex-done", record.output_text)
        self.assertIn("non-interactive", prompt.lower())
        self.assertIn("owned paths", prompt.lower())
        self.assertIn("verification commands", prompt.lower())
        self.assertIn("authoritative", prompt.lower())
        self.assertIn("out-of-scope", prompt.lower())
        self.assertIn("do not wait for user approval", prompt.lower())
        self.assertEqual(record.metadata["backend"], "codex_cli")
        self.assertEqual(record.metadata["thread_id"], "thread-123")
        self.assertEqual(record.metadata["exit_code"], 0)

    async def test_codex_cli_backend_exposes_protocol_state_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_codex = Path(tmpdir) / "fake_codex.py"
            self._write_fake_codex_script(fake_codex)
            backend = CodexCliLaunchBackend(
                codex_command=(sys.executable, str(fake_codex)),
                spool_root=tmpdir,
                bypass_approvals=False,
                sandbox_mode="workspace-write",
            )
            assignment = WorkerAssignment(
                assignment_id="assign-codex-protocol-file",
                worker_id="worker-codex-protocol-file",
                group_id="group-a",
                team_id="team-a",
                task_id="task-a",
                role="leader",
                backend="codex_cli",
                instructions="Return valid JSON only.",
                input_text="run",
                working_dir=tmpdir,
                execution_contract=WorkerExecutionContract(
                    contract_id="contract-codex-protocol-file",
                    mode="leader_coordination",
                    require_final_report=True,
                ),
                lease_policy=WorkerLeasePolicy(
                    accept_deadline_seconds=1.0,
                    renewal_timeout_seconds=30.0,
                    hard_deadline_seconds=300.0,
                ),
            )

            handle = await backend.launch(assignment)
            await asyncio.to_thread(handle.metadata["_process"].wait, 5)
            result_path = Path(handle.transport_ref)
            for _ in range(50):
                if result_path.exists():
                    break
                await asyncio.sleep(0.01)

        self.assertIn("protocol_state_file", handle.metadata)

    async def test_codex_cli_backend_satisfies_protocol_contract_via_group_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_codex = Path(tmpdir) / "fake_codex.py"
            self._write_fake_codex_script(fake_codex)
            backend = CodexCliLaunchBackend(
                codex_command=(sys.executable, str(fake_codex)),
                spool_root=tmpdir,
                bypass_approvals=False,
                sandbox_mode="workspace-write",
            )
            store = InMemoryOrchestrationStore()
            runtime = GroupRuntime(
                store=store,
                bus=InMemoryEventBus(),
                launch_backends={"codex_cli": backend},
                supervisor=DefaultWorkerSupervisor(store=store, launch_backends={"codex_cli": backend}),
            )
            assignment = WorkerAssignment(
                assignment_id="assign-codex-protocol-runtime",
                worker_id="worker-codex-protocol-runtime",
                group_id="group-a",
                team_id="team-a",
                task_id="task-a",
                role="leader",
                backend="codex_cli",
                instructions=(
                    "Return a JSON object with this shape:\n"
                    '{ "summary": "...", "sequential_slices": [], "parallel_slices": [] }'
                ),
                input_text="Return valid JSON only.",
                working_dir=tmpdir,
                execution_contract=WorkerExecutionContract(
                    contract_id="contract-codex-protocol-runtime",
                    mode="leader_coordination",
                    require_final_report=True,
                ),
                lease_policy=WorkerLeasePolicy(
                    accept_deadline_seconds=1.0,
                    renewal_timeout_seconds=30.0,
                    hard_deadline_seconds=300.0,
                ),
            )

            record = await runtime.run_worker_assignment(
                assignment,
                policy=WorkerExecutionPolicy(allow_relaunch=False, escalate_after_attempts=False),
            )

        self.assertEqual(record.status, WorkerStatus.COMPLETED)
        self.assertEqual(record.metadata.get("protocol_wait_mode"), "native")
        self.assertEqual(record.metadata.get("lifecycle_status"), "completed")
        self.assertEqual(record.metadata.get("final_report", {}).get("terminal_status"), "completed")

    async def test_codex_cli_backend_materializes_failed_result_for_supervisor(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_codex = Path(tmpdir) / "fake_codex.py"
            self._write_fake_codex_script(fake_codex)

            backend = CodexCliLaunchBackend(
                codex_command=(sys.executable, str(fake_codex)),
                spool_root=tmpdir,
                bypass_approvals=False,
                sandbox_mode="workspace-write",
            )
            store = InMemoryOrchestrationStore()
            supervisor = DefaultWorkerSupervisor(store=store, launch_backends={"codex_cli": backend})
            assignment = WorkerAssignment(
                assignment_id="assign-codex-fail",
                worker_id="worker-codex-fail",
                group_id="group-a",
                team_id="team-a",
                task_id="task-a",
                role="teammate",
                backend="codex_cli",
                instructions="Edit the runtime.",
                input_text="Implement the next slice.",
                working_dir=tmpdir,
                environment={"FAKE_CODEX_FAIL": "1"},
            )

            handle = await backend.launch(assignment)
            await supervisor.start(handle, assignment)
            record = await supervisor.wait(handle, timeout_seconds=5.0)

        self.assertEqual(record.status, WorkerStatus.FAILED)
        self.assertIn("codex failed on purpose", record.error_text)

    async def test_codex_cli_backend_marks_transient_plugin_sync_failure_as_provider_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_codex = Path(tmpdir) / "fake_codex.py"
            self._write_fake_codex_script(fake_codex)

            backend = CodexCliLaunchBackend(
                codex_command=(sys.executable, str(fake_codex)),
                spool_root=tmpdir,
                bypass_approvals=False,
                sandbox_mode="workspace-write",
            )
            store = InMemoryOrchestrationStore()
            supervisor = DefaultWorkerSupervisor(store=store, launch_backends={"codex_cli": backend})
            assignment = WorkerAssignment(
                assignment_id="assign-codex-provider-unavailable",
                worker_id="worker-codex-provider-unavailable",
                group_id="group-a",
                team_id="team-a",
                task_id="task-a",
                role="teammate",
                backend="codex_cli",
                instructions="Edit the runtime.",
                input_text="Implement the next slice.",
                working_dir=tmpdir,
                environment={
                    "FAKE_CODEX_FAIL": "1",
                    "FAKE_CODEX_STDERR": (
                        "chatgpt authentication required to sync remote plugins; "
                        "remote plugin sync request to https://chatgpt.com/backend-api/plugins/featured "
                        "failed with status 403 Forbidden; stream disconnected - retrying sampling request"
                    ),
                },
            )

            handle = await backend.launch(assignment)
            await supervisor.start(handle, assignment)
            record = await supervisor.wait(handle, timeout_seconds=5.0)

        self.assertEqual(record.status, WorkerStatus.FAILED)
        self.assertEqual(record.metadata.get("failure_kind"), "provider_unavailable")
        self.assertEqual(record.metadata.get("error_type"), "ProviderUnavailableError")

    async def test_codex_cli_backend_attributes_sigterm_failure_without_last_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_codex = Path(tmpdir) / "fake_codex.py"
            self._write_fake_codex_script(fake_codex)

            backend = CodexCliLaunchBackend(
                codex_command=(sys.executable, str(fake_codex)),
                spool_root=tmpdir,
                bypass_approvals=False,
                sandbox_mode="workspace-write",
            )
            store = InMemoryOrchestrationStore()
            supervisor = DefaultWorkerSupervisor(store=store, launch_backends={"codex_cli": backend})
            assignment = WorkerAssignment(
                assignment_id="assign-codex-sigterm-fail",
                worker_id="worker-codex-sigterm-fail",
                group_id="group-a",
                team_id="team-a",
                task_id="task-a",
                role="teammate",
                backend="codex_cli",
                instructions="Edit the runtime.",
                input_text="Implement the next slice.",
                working_dir=tmpdir,
                environment={
                    "FAKE_CODEX_SLEEP_SECONDS": "1.5",
                    "FAKE_CODEX_SKIP_LAST_MESSAGE": "1",
                },
            )

            handle = await backend.launch(assignment)
            await supervisor.start(handle, assignment)
            await backend.cancel(handle)
            record = await supervisor.wait(handle, timeout_seconds=5.0)

        self.assertEqual(record.status, WorkerStatus.FAILED)
        self.assertLess(int(record.metadata.get("exit_code", 0)), 0)
        self.assertEqual(record.metadata.get("termination_signal"), 15)
        self.assertEqual(record.metadata.get("termination_signal_name"), "SIGTERM")
        self.assertEqual(record.metadata.get("last_message_exists_at_failure"), False)
        self.assertIsNotNone(record.metadata.get("last_protocol_progress_at"))
        self.assertEqual(record.metadata.get("supervisor_timeout_path"), False)
        self.assertIn("process_termination", record.metadata.get("failure_tags", ()))
        self.assertIn("signal_sigterm", record.metadata.get("failure_tags", ()))
        self.assertNotIn("timeout_failure", record.metadata.get("failure_tags", ()))

    async def test_codex_cli_backend_requires_teammate_verification_loop_in_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_codex = Path(tmpdir) / "fake_codex.py"
            self._write_fake_codex_script(fake_codex)
            backend = CodexCliLaunchBackend(
                codex_command=(sys.executable, str(fake_codex)),
                spool_root=tmpdir,
                bypass_approvals=False,
                sandbox_mode="workspace-write",
            )
            assignment = WorkerAssignment(
                assignment_id="assign-codex-teammate-verification-prompt",
                worker_id="team-a:teammate:1",
                group_id="group-a",
                team_id="team-a",
                task_id="task-a",
                role="teammate",
                backend="codex_cli",
                instructions="Implement the runtime slice.",
                input_text="Run the requested tests.",
                working_dir=tmpdir,
                metadata={"verification_commands": ["python3 -m unittest tests.test_backends -v"]},
                execution_contract=WorkerExecutionContract(
                    contract_id="contract-codex-teammate-verification-prompt",
                    mode="teammate_code_edit",
                    require_final_report=True,
                    require_verification_results=True,
                ),
                lease_policy=WorkerLeasePolicy(
                    accept_deadline_seconds=1.0,
                    renewal_timeout_seconds=30.0,
                    hard_deadline_seconds=300.0,
                ),
            )

            handle = await backend.launch(assignment)
            prompt = Path(handle.metadata["prompt_file"]).read_text(encoding="utf-8")
            await asyncio.to_thread(handle.metadata["_process"].wait, 5)
            result_path = Path(handle.transport_ref)
            for _ in range(50):
                if result_path.exists():
                    break
                await asyncio.sleep(0.01)

        self.assertIn("implement -> test -> fix -> retest", prompt.lower())
        self.assertIn('"verification_results"', prompt)
        self.assertIn("return json only", prompt.lower())
        self.assertNotIn(
            "Perform the assigned repository work and end with a concise summary of changes and verification results.",
            prompt,
        )

    async def test_codex_cli_backend_parses_json_last_message_into_final_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_codex = Path(tmpdir) / "fake_codex.py"
            self._write_fake_codex_script(fake_codex)
            backend = CodexCliLaunchBackend(
                codex_command=(sys.executable, str(fake_codex)),
                spool_root=tmpdir,
                bypass_approvals=False,
                sandbox_mode="workspace-write",
            )
            store = InMemoryOrchestrationStore()
            supervisor = DefaultWorkerSupervisor(store=store, launch_backends={"codex_cli": backend})
            verification_command = "python3 -m unittest tests.test_backends -v"
            final_report_payload = {
                "summary": "Implemented and verified teammate runtime updates.",
                "terminal_status": "completed",
                "verification_results": [
                    {
                        "command": verification_command,
                        "returncode": 0,
                        "stdout": "authoritative verification passed",
                        "stderr": "",
                    }
                ],
                "artifact_refs": ["src/agent_orchestra/runtime/backends/codex_cli_backend.py"],
                "metadata": {"report_source": "teammate"},
            }
            assignment = WorkerAssignment(
                assignment_id="assign-codex-json-final-report",
                worker_id="team-a:teammate:1",
                group_id="group-a",
                team_id="team-a",
                task_id="task-a",
                role="teammate",
                backend="codex_cli",
                instructions="Own implementation and verification.",
                input_text="Complete the assignment and report JSON.",
                working_dir=tmpdir,
                environment={"FAKE_CODEX_LAST_MESSAGE": json.dumps(final_report_payload, ensure_ascii=False)},
                metadata={"verification_commands": [verification_command]},
                execution_contract=WorkerExecutionContract(
                    contract_id="contract-codex-json-final-report",
                    mode="teammate_code_edit",
                    require_final_report=True,
                    require_verification_results=True,
                ),
                lease_policy=WorkerLeasePolicy(
                    accept_deadline_seconds=1.0,
                    renewal_timeout_seconds=30.0,
                    hard_deadline_seconds=300.0,
                ),
            )

            handle = await backend.launch(assignment)
            await supervisor.start(handle, assignment)
            record = await supervisor.wait(handle, timeout_seconds=5.0)

        self.assertEqual(record.status, WorkerStatus.COMPLETED)
        self.assertEqual(
            record.metadata["final_report"]["summary"],
            "Implemented and verified teammate runtime updates.",
        )
        self.assertEqual(record.metadata["final_report"]["terminal_status"], "completed")
        self.assertEqual(len(record.metadata["final_report"]["verification_results"]), 1)
        self.assertEqual(
            record.metadata["final_report"]["verification_results"][0]["command"],
            verification_command,
        )
        self.assertEqual(
            record.metadata["final_report"]["verification_results"][0]["returncode"],
            0,
        )

    async def test_codex_cli_backend_result_json_preserves_utf8_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_codex = Path(tmpdir) / "fake_codex.py"
            self._write_fake_codex_script(fake_codex)

            backend = CodexCliLaunchBackend(
                codex_command=(sys.executable, str(fake_codex)),
                spool_root=tmpdir,
                bypass_approvals=False,
                sandbox_mode="workspace-write",
            )
            store = InMemoryOrchestrationStore()
            supervisor = DefaultWorkerSupervisor(store=store, launch_backends={"codex_cli": backend})
            assignment = WorkerAssignment(
                assignment_id="assign-codex-utf8",
                worker_id="worker-codex-utf8",
                group_id="group-a",
                team_id="team-a",
                task_id="task-a",
                role="teammate",
                backend="codex_cli",
                instructions="请处理中文说明。",
                input_text="继续执行中文任务。",
                working_dir=tmpdir,
            )

            handle = await backend.launch(assignment)
            await supervisor.start(handle, assignment)
            await supervisor.wait(handle, timeout_seconds=5.0)
            raw_result = Path(handle.transport_ref).read_text(encoding="utf-8")

        self.assertIn("请处理中文说明。", raw_result)
        self.assertIn("继续执行中文任务。", raw_result)
        self.assertNotIn("\\u8bf7", raw_result)

    async def test_codex_cli_backend_keeps_structured_leader_prompt_json_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_codex = Path(tmpdir) / "fake_codex.py"
            self._write_fake_codex_script(fake_codex)

            backend = CodexCliLaunchBackend(
                codex_command=(sys.executable, str(fake_codex)),
                spool_root=tmpdir,
                bypass_approvals=False,
                sandbox_mode="workspace-write",
            )
            assignment = WorkerAssignment(
                assignment_id="assign-codex-structured-leader",
                worker_id="leader:runtime",
                group_id="group-a",
                team_id="team-a",
                task_id="task-a",
                role="leader",
                backend="codex_cli",
                instructions=(
                    "Return a JSON object with this shape:\n"
                    '{ "summary": "...", "sequential_slices": [], "parallel_slices": [] }\n'
                    "Use sequential_slices for dependency-ordered work and parallel_slices for same-batch work."
                ),
                input_text="Return valid JSON only.",
                working_dir=tmpdir,
            )

            handle = await backend.launch(assignment)
            prompt = Path(handle.metadata["prompt_file"]).read_text(encoding="utf-8")
            await asyncio.to_thread(handle.metadata["_process"].wait, 5)
            result_path = Path(handle.transport_ref)
            for _ in range(50):
                if result_path.exists():
                    break
                await asyncio.sleep(0.01)

        self.assertIn("Return a JSON object with this shape", prompt)
        self.assertNotIn("Perform the assigned repository work and end with a concise summary of changes and verification results.", prompt)
