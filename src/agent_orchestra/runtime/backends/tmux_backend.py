from __future__ import annotations

import json
import os
import sys
import tempfile
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
    CommandRunner,
    backend_capability_hints,
    default_command_runner,
    ensure_directory,
    shell_join,
)


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


def _source_root() -> Path:
    return Path(__file__).resolve().parents[3]


class TmuxLaunchBackend(LaunchBackend):
    def __init__(
        self,
        *,
        command_runner: CommandRunner | None = None,
        python_executable: str | None = None,
        spool_root: str | Path | None = None,
        session_prefix: str = "agent-orchestra",
    ) -> None:
        self.command_runner = command_runner or default_command_runner
        self.python_executable = python_executable or sys.executable
        self.spool_root = Path(spool_root) if spool_root is not None else None
        self.session_prefix = session_prefix

    def describe_capabilities(self) -> WorkerBackendCapabilities:
        return WorkerBackendCapabilities(
            transport_class=WorkerTransportClass.FULL_RESIDENT_TRANSPORT,
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
        root = ensure_directory(self.spool_root or Path(tempfile.mkdtemp(prefix="agent-orchestra-tmux-")))
        assignment_file = root / f"{assignment.assignment_id}.assignment.json"
        result_file = root / f"{assignment.assignment_id}.result.json"
        protocol_state_file = root / f"{assignment.assignment_id}.protocol.json"
        protocol_enabled = self._requires_protocol_artifacts(assignment)
        assignment_file.write_text(
            json.dumps(_assignment_to_payload(assignment), ensure_ascii=False),
            encoding="utf-8",
        )

        src_root = str(_source_root())
        current_pythonpath = os.environ.get("PYTHONPATH", "")
        pythonpath = src_root if not current_pythonpath else f"{src_root}{os.pathsep}{current_pythonpath}"
        worker_command = shell_join(
            [
                "env",
                f"PYTHONPATH={pythonpath}",
                self.python_executable,
                "-m",
                "agent_orchestra.runtime.worker_process",
                "--assignment-file",
                str(assignment_file),
                "--result-file",
                str(result_file),
            ]
        )
        if protocol_enabled:
            worker_command = shell_join(
                [
                    "env",
                    f"PYTHONPATH={pythonpath}",
                    self.python_executable,
                    "-m",
                    "agent_orchestra.runtime.worker_process",
                    "--assignment-file",
                    str(assignment_file),
                    "--result-file",
                    str(result_file),
                    "--protocol-state-file",
                    str(protocol_state_file),
                ]
            )
        session_name = f"{self.session_prefix}-{assignment.worker_id}-{assignment.assignment_id}"
        tmux_command = ["tmux", "new-session", "-d", "-s", session_name, worker_command]
        result = self.command_runner(tmux_command, assignment.working_dir)
        if result.returncode != 0:
            raise RuntimeError(result.stderr or "tmux launch failed")
        return WorkerHandle(
            worker_id=assignment.worker_id,
            role=assignment.role,
            backend="tmux",
            run_id=assignment.assignment_id,
            session_name=session_name,
            transport_ref=str(result_file),
            metadata={
                "assignment_file": str(assignment_file),
                "spool_root": str(root),
                "command": tmux_command,
                **backend_capability_hints(capabilities),
                **(
                    {"protocol_state_file": str(protocol_state_file)}
                    if protocol_enabled
                    else {}
                ),
            },
        )

    async def cancel(self, handle: WorkerHandle) -> None:
        if handle.session_name is None:
            return
        result = self.command_runner(["tmux", "kill-session", "-t", handle.session_name], None)
        if result.returncode != 0:
            raise RuntimeError(result.stderr or "tmux cancel failed")

    async def resume(self, handle: WorkerHandle, assignment: WorkerAssignment | None = None) -> WorkerHandle:
        if handle.session_name is not None:
            result = self.command_runner(["tmux", "has-session", "-t", handle.session_name], None)
            if result.returncode != 0:
                raise RuntimeError(result.stderr or "tmux session unavailable")
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
            raise RuntimeError(f"tmux reattach requires {label}")
        if value is None:
            return None
        path = Path(value)
        if not path.exists() and not path.parent.exists():
            raise RuntimeError(f"tmux reattach path unavailable: {path}")
        return str(path)

    async def reattach(
        self,
        locator: WorkerTransportLocator,
        assignment: WorkerAssignment,
    ) -> WorkerHandle:
        if assignment.backend != "tmux":
            raise RuntimeError(f"tmux backend cannot reattach assignment backend `{assignment.backend}`")
        if locator.backend and locator.backend != "tmux":
            raise RuntimeError(f"tmux backend cannot reattach locator backend `{locator.backend}`")
        session_name = locator.session_name
        if not isinstance(session_name, str) or not session_name.strip():
            raise RuntimeError("tmux reattach requires session_name")
        result = self.command_runner(["tmux", "has-session", "-t", session_name], None)
        if result.returncode != 0:
            raise RuntimeError(result.stderr or "tmux session unavailable")
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
        metadata = {str(key): value for key, value in locator.metadata.items()}
        metadata.update(backend_capability_hints(self.describe_capabilities()))
        metadata["reattach"] = True
        if protocol_state_file is not None:
            metadata["protocol_state_file"] = protocol_state_file
        if locator.spool_dir is not None:
            metadata["spool_root"] = locator.spool_dir
        if locator.pane_id is not None:
            metadata["pane_id"] = locator.pane_id
        return WorkerHandle(
            worker_id=assignment.worker_id,
            role=assignment.role,
            backend="tmux",
            run_id=assignment.assignment_id,
            session_name=session_name,
            transport_ref=result_file,
            metadata=metadata,
        )
