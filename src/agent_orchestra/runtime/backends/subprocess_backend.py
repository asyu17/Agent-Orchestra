from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from agent_orchestra.contracts.execution import (
    LaunchBackend,
    WorkerAssignment,
    WorkerBackendCapabilities,
    WorkerHandle,
    WorkerTransportClass,
    WorkerTransportLocator,
)
from agent_orchestra.runtime.backends.base import (
    backend_capability_hints,
    command_fingerprint,
    ensure_directory,
    is_process_alive,
    terminate_process_by_pid,
)


ProcessLauncher = Callable[..., subprocess.Popen[str]]


def _assignment_to_payload(assignment: WorkerAssignment) -> dict[str, Any]:
    return {
        "assignment_id": assignment.assignment_id,
        "worker_id": assignment.worker_id,
        "group_id": assignment.group_id,
        "team_id": assignment.team_id,
        "lane_id": assignment.lane_id,
        "task_id": assignment.task_id,
        "role": assignment.role,
        "backend": assignment.backend,
        "instructions": assignment.instructions,
        "input_text": assignment.input_text,
        "objective_id": assignment.objective_id,
        "working_dir": assignment.working_dir,
        "environment": dict(assignment.environment),
        "metadata": dict(assignment.metadata),
        "execution_contract": assignment.execution_contract.to_dict()
        if assignment.execution_contract is not None
        else None,
        "lease_policy": assignment.lease_policy.to_dict()
        if assignment.lease_policy is not None
        else None,
    }


def default_process_launcher(**kwargs: Any) -> subprocess.Popen[str]:
    return subprocess.Popen(**kwargs)


def _source_root() -> Path:
    return Path(__file__).resolve().parents[3]


class SubprocessLaunchBackend(LaunchBackend):
    def __init__(
        self,
        *,
        python_executable: str | None = None,
        spool_root: str | Path | None = None,
        process_launcher: ProcessLauncher | None = None,
    ) -> None:
        self.python_executable = python_executable or sys.executable
        self.spool_root = Path(spool_root) if spool_root is not None else None
        self.process_launcher = process_launcher or default_process_launcher

    def describe_capabilities(self) -> WorkerBackendCapabilities:
        return WorkerBackendCapabilities(
            transport_class=WorkerTransportClass.EPHEMERAL_WORKER_TRANSPORT,
            supports_protocol_contract=True,
            supports_protocol_state=True,
            supports_protocol_final_report=True,
            supports_resume=True,
            supports_reactivate=False,
            supports_reattach=True,
            supports_artifact_progress=True,
            supports_verification_in_working_dir=True,
        )

    def _requires_protocol_artifacts(self, assignment: WorkerAssignment) -> bool:
        if assignment.execution_contract is not None or assignment.lease_policy is not None:
            return True
        metadata = assignment.metadata
        return isinstance(metadata.get("execution_contract"), dict) or isinstance(
            metadata.get("lease_policy"), dict
        )

    async def launch(self, assignment: WorkerAssignment) -> WorkerHandle:
        capabilities = self.describe_capabilities()
        root = ensure_directory(
            self.spool_root or Path(tempfile.mkdtemp(prefix="agent-orchestra-subprocess-"))
        )
        assignment_file = root / f"{assignment.assignment_id}.assignment.json"
        result_file = root / f"{assignment.assignment_id}.result.json"
        protocol_state_file = root / f"{assignment.assignment_id}.protocol.json"
        protocol_enabled = self._requires_protocol_artifacts(assignment)
        assignment_file.write_text(
            json.dumps(_assignment_to_payload(assignment), ensure_ascii=False),
            encoding="utf-8",
        )

        command = [
            self.python_executable,
            "-m",
            "agent_orchestra.runtime.worker_process",
            "--assignment-file",
            str(assignment_file),
            "--result-file",
            str(result_file),
        ]
        if protocol_enabled:
            command.extend(["--protocol-state-file", str(protocol_state_file)])
        env = dict(os.environ)
        env.update(assignment.environment)
        src_root = str(_source_root())
        current_pythonpath = env.get("PYTHONPATH")
        env["PYTHONPATH"] = src_root if not current_pythonpath else f"{src_root}{os.pathsep}{current_pythonpath}"
        process = self.process_launcher(
            args=command,
            cwd=assignment.working_dir,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        process_group_id: int | None = None
        try:
            process_group_id = os.getpgid(process.pid)
        except (ProcessLookupError, OSError):
            process_group_id = None
        return WorkerHandle(
            worker_id=assignment.worker_id,
            role=assignment.role,
            backend="subprocess",
            run_id=assignment.assignment_id,
            process_id=process.pid,
            transport_ref=str(result_file),
            metadata={
                "assignment_file": str(assignment_file),
                "spool_root": str(root),
                "command": command,
                "command_fingerprint": command_fingerprint(command),
                "process_group_id": process_group_id,
                **backend_capability_hints(capabilities),
                **(
                    {"protocol_state_file": str(protocol_state_file)}
                    if protocol_enabled
                    else {}
                ),
                "_process": process,
            },
        )

    async def cancel(self, handle: WorkerHandle) -> None:
        process = handle.metadata.get("_process")
        if process is not None:
            process.terminate()
            return
        terminate_process_by_pid(handle.process_id)

    async def resume(self, handle: WorkerHandle, assignment: WorkerAssignment | None = None) -> WorkerHandle:
        process = handle.metadata.get("_process")
        if process is None or (hasattr(process, "poll") and process.poll() is not None):
            raise RuntimeError("subprocess process unavailable")
        handle.metadata.update(backend_capability_hints(self.describe_capabilities()))
        if assignment is not None:
            handle.run_id = assignment.assignment_id
        return handle

    def _locator_path(
        self,
        locator: WorkerTransportLocator,
        *,
        primary_attr: str,
        metadata_key: str,
        required: bool,
        label: str,
    ) -> str | None:
        value = getattr(locator, primary_attr)
        if value is None:
            candidate = locator.metadata.get(metadata_key)
            value = str(candidate) if isinstance(candidate, str) and candidate else None
        if value is None and required:
            raise RuntimeError(f"subprocess reattach requires {label}")
        if value is None:
            return None
        path = Path(value)
        if not path.exists() and not path.parent.exists():
            raise RuntimeError(f"subprocess reattach path unavailable: {path}")
        return str(path)

    async def reattach(
        self,
        locator: WorkerTransportLocator,
        assignment: WorkerAssignment,
    ) -> WorkerHandle:
        if assignment.backend != "subprocess":
            raise RuntimeError(f"subprocess backend cannot reattach assignment backend `{assignment.backend}`")
        if locator.backend and locator.backend != "subprocess":
            raise RuntimeError(f"subprocess backend cannot reattach locator backend `{locator.backend}`")
        if locator.pid is None:
            raise RuntimeError("subprocess reattach requires pid")
        if not is_process_alive(locator.pid):
            raise RuntimeError(f"subprocess process not alive: {locator.pid}")
        result_file = self._locator_path(
            locator,
            primary_attr="result_file",
            metadata_key="transport_ref",
            required=True,
            label="result_file",
        )
        protocol_state_file = self._locator_path(
            locator,
            primary_attr="protocol_state_file",
            metadata_key="protocol_state_file",
            required=False,
            label="protocol_state_file",
        )
        stdout_file = self._locator_path(
            locator,
            primary_attr="stdout_file",
            metadata_key="stdout_file",
            required=False,
            label="stdout_file",
        )
        stderr_file = self._locator_path(
            locator,
            primary_attr="stderr_file",
            metadata_key="stderr_file",
            required=False,
            label="stderr_file",
        )
        last_message_file = self._locator_path(
            locator,
            primary_attr="last_message_file",
            metadata_key="last_message_file",
            required=False,
            label="last_message_file",
        )
        metadata = {str(key): value for key, value in locator.metadata.items()}
        metadata.update(backend_capability_hints(self.describe_capabilities()))
        metadata["reattach"] = True
        if protocol_state_file is not None:
            metadata["protocol_state_file"] = protocol_state_file
        if stdout_file is not None:
            metadata["stdout_file"] = stdout_file
        if stderr_file is not None:
            metadata["stderr_file"] = stderr_file
        if last_message_file is not None:
            metadata["last_message_file"] = last_message_file
        if locator.spool_dir is not None:
            metadata["spool_root"] = locator.spool_dir
        if locator.command_fingerprint is not None:
            metadata["command_fingerprint"] = locator.command_fingerprint
        if locator.process_group_id is not None:
            metadata["process_group_id"] = locator.process_group_id
        return WorkerHandle(
            worker_id=assignment.worker_id,
            role=assignment.role,
            backend="subprocess",
            run_id=assignment.assignment_id,
            process_id=locator.pid,
            transport_ref=result_file,
            metadata=metadata,
        )
