from __future__ import annotations

import json
import os
import signal
import subprocess
import tempfile
import threading
from datetime import datetime, timezone
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
import time
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


_TRANSIENT_PROVIDER_ERROR_TOKENS = (
    "chatgpt authentication required to sync remote plugins",
    "remote plugin sync request",
    "failed to warm featured plugin ids cache",
    "failed to send remote plugin sync request",
    "plugins/featured",
    "403 forbidden",
    "stream disconnected - retrying sampling request",
    "error sending request for url",
)


def default_process_launcher(**kwargs: Any) -> subprocess.Popen[str]:
    return subprocess.Popen(**kwargs)


def _string_list(value: object) -> tuple[str, ...]:
    if not isinstance(value, Iterable) or isinstance(value, (str, bytes)):
        return ()
    result: list[str] = []
    for item in value:
        if isinstance(item, str) and item:
            result.append(item)
    return tuple(result)


def _mapping_dict(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): item for key, item in value.items()}


def _is_transient_provider_failure(stderr_text: str) -> bool:
    normalized = stderr_text.strip().lower()
    if not normalized:
        return False
    return any(token in normalized for token in _TRANSIENT_PROVIDER_ERROR_TOKENS)


def _assignment_execution_contract_payload(assignment: WorkerAssignment) -> Mapping[str, object]:
    if assignment.execution_contract is not None:
        return {
            "mode": assignment.execution_contract.mode,
            "require_verification_results": assignment.execution_contract.require_verification_results,
            "required_verification_commands": list(
                assignment.execution_contract.required_verification_commands
            ),
        }
    payload = assignment.metadata.get("execution_contract")
    if isinstance(payload, Mapping):
        return payload
    return {}


def _requires_teammate_verification_report(assignment: WorkerAssignment) -> bool:
    contract_payload = _assignment_execution_contract_payload(assignment)
    require_verification_results = bool(contract_payload.get("require_verification_results", False))
    if not require_verification_results:
        return False
    mode = str(contract_payload.get("mode", "")).strip().lower()
    role = assignment.role.strip().lower()
    return (
        mode == "teammate_code_edit"
        or "code_edit" in mode
        or "code-edit" in mode
        or role == "teammate"
    )


def _structured_report_payload(output_text: str) -> Mapping[str, object] | None:
    stripped = output_text.strip()
    if not stripped:
        return None
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, Mapping):
        return None
    nested_report = parsed.get("final_report")
    if isinstance(nested_report, Mapping):
        return nested_report
    return parsed


def _mapping_list(value: object) -> tuple[dict[str, object], ...]:
    if not isinstance(value, Iterable) or isinstance(value, (str, bytes)):
        return ()
    items: list[dict[str, object]] = []
    for item in value:
        if isinstance(item, Mapping):
            items.append(_mapping_dict(item))
    return tuple(items)


def _coerce_terminal_status(value: object, *, fallback: str) -> str:
    if isinstance(value, str):
        candidate = value.strip().lower()
        if candidate in {"completed", "blocked", "failed", "abandoned"}:
            return candidate
    return fallback


def _build_protocol_final_report(
    *,
    assignment: WorkerAssignment,
    output_text: str,
    terminal_status: str,
    terminal_summary: str,
) -> dict[str, object]:
    report: dict[str, object] = {
        "assignment_id": assignment.assignment_id,
        "worker_id": assignment.worker_id,
        "terminal_status": terminal_status,
        "summary": terminal_summary,
        "metadata": {"backend": "codex_cli"},
    }
    structured_payload = _structured_report_payload(output_text)
    if structured_payload is None:
        return report

    report["terminal_status"] = _coerce_terminal_status(
        structured_payload.get("terminal_status"),
        fallback=terminal_status,
    )
    report["summary"] = str(structured_payload.get("summary", terminal_summary))

    assignment_id = structured_payload.get("assignment_id")
    if isinstance(assignment_id, str) and assignment_id.strip():
        report["assignment_id"] = assignment_id
    worker_id = structured_payload.get("worker_id")
    if isinstance(worker_id, str) and worker_id.strip():
        report["worker_id"] = worker_id

    artifact_refs = _string_list(structured_payload.get("artifact_refs", ()))
    if artifact_refs:
        report["artifact_refs"] = list(artifact_refs)
    verification_results = _mapping_list(structured_payload.get("verification_results", ()))
    if isinstance(structured_payload.get("verification_results"), (list, tuple)):
        report["verification_results"] = [dict(item) for item in verification_results]
    pending_verification_commands = _string_list(
        structured_payload.get("pending_verification_commands", ())
    )
    if pending_verification_commands:
        report["pending_verification_commands"] = list(pending_verification_commands)
    missing_dependencies = _string_list(structured_payload.get("missing_dependencies", ()))
    if missing_dependencies:
        report["missing_dependencies"] = list(missing_dependencies)

    blocker = structured_payload.get("blocker")
    if isinstance(blocker, str) and blocker:
        report["blocker"] = blocker
    retry_hint = structured_payload.get("retry_hint")
    if isinstance(retry_hint, str) and retry_hint:
        report["retry_hint"] = retry_hint
    authority_request = structured_payload.get("authority_request")
    if isinstance(authority_request, Mapping):
        report["authority_request"] = dict(authority_request)

    metadata = {"backend": "codex_cli", **_mapping_dict(structured_payload.get("metadata"))}
    report["metadata"] = metadata
    return report


def _render_prompt(assignment: WorkerAssignment) -> str:
    owned_paths = _string_list(assignment.metadata.get("owned_paths"))
    verification_commands = _string_list(assignment.metadata.get("verification_commands"))

    lines = [
        "# Agent Orchestra Worker Assignment",
        "",
        "## Identity",
        f"- Worker ID: {assignment.worker_id}",
        f"- Role: {assignment.role}",
        f"- Assignment ID: {assignment.assignment_id}",
        f"- Group ID: {assignment.group_id}",
    ]
    if assignment.objective_id is not None:
        lines.append(f"- Objective ID: {assignment.objective_id}")
    if assignment.lane_id is not None:
        lines.append(f"- Lane ID: {assignment.lane_id}")
    if assignment.team_id is not None:
        lines.append(f"- Team ID: {assignment.team_id}")

    lines.extend(
        [
            "",
            "## Execution Mode",
            "- This is a non-interactive orchestration worker run.",
            "- The assignment is already authorized within its declared scope.",
            "- Owned paths and verification commands are enforced authoritatively after execution.",
            "- Out-of-scope edits or failed verification will be treated as worker failure.",
            "- Do not wait for user approval, review, or clarification.",
            "- Do not enter approval-gated brainstorming or planning workflows.",
            "- Execute autonomously within the provided instructions and constraints.",
            "",
            "## Instructions",
            assignment.instructions,
            "",
            "## Input",
            assignment.input_text,
        ]
    )

    if assignment.conversation:
        lines.extend(["", "## Prior Conversation"])
        for item in assignment.conversation:
            role = item.get("role", "unknown")
            content = item.get("content", "")
            lines.append(f"- {role}: {content}")

    if owned_paths:
        lines.extend(["", "## Owned Paths", "Stay within these owned paths when editing:"])
        lines.extend(f"- {path}" for path in owned_paths)

    if verification_commands:
        lines.extend(["", "## Verification Commands", "Run these verification commands within your own execution loop:"])
        lines.extend(f"- {command}" for command in verification_commands)

    structured_output = assignment.metadata.get("structured_output")
    requires_structured_final_response = (
        structured_output == "json_only"
        or "Return a JSON object with this shape:" in assignment.instructions
    )
    requires_teammate_verification_report = _requires_teammate_verification_report(assignment)
    if requires_teammate_verification_report:
        lines.extend(
            [
                "",
                "## Required Verification Loop",
                "- Own this full loop: implement -> test -> fix -> retest.",
                "- Run all listed verification commands before finalization.",
                "- If verification fails, fix the issue and rerun until all required checks pass or you hit a real blocker.",
                "- If you must use an environment-compatible fallback to execute a required command, report the original required command in `requested_command` and the actual fallback command in `command`.",
                "",
                "## Required Final Response",
                "Return JSON only. Do not include markdown fences or non-JSON text.",
                "Use this shape:",
                '{ "summary": "...", "terminal_status": "completed|blocked|failed|abandoned", '
                '"verification_results": [ { "requested_command": "...", "command": "...", "returncode": 0, "stdout": "...", "stderr": "..." } ], '
                '"pending_verification_commands": ["..."], "artifact_refs": ["..."], "blocker": "", '
                '"missing_dependencies": ["..."], "retry_hint": "", '
                '"authority_request": null, '
                '"metadata": {} }',
                "- Only set `authority_request` when the task is blocked specifically because it needs broader scope or higher authority to continue.",
                "- When `authority_request` is present, keep `terminal_status` as `blocked` and include the requested paths, reason, evidence, and retry hint.",
            ]
        )
    elif not requires_structured_final_response:
        lines.extend(
            [
                "",
                "## Required Final Response",
                "Perform the assigned repository work and end with a concise summary of changes and verification results.",
            ]
        )
    return "\n".join(lines) + "\n"


def _extract_thread_id(stdout_path: Path) -> str | None:
    if not stdout_path.exists():
        return None
    for raw_line in stdout_path.read_text(encoding="utf-8").splitlines():
        try:
            payload = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if payload.get("type") == "thread.started":
            thread_id = payload.get("thread_id")
            if isinstance(thread_id, str) and thread_id:
                return thread_id
    return None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _signal_name(signum: int) -> str | None:
    try:
        return signal.Signals(signum).name
    except ValueError:
        return None


def _progress_snapshot(*paths: Path) -> tuple[tuple[str, bool, int | None, int | None], ...]:
    snapshot: list[tuple[str, bool, int | None, int | None]] = []
    for path in paths:
        if path.exists():
            stat = path.stat()
            snapshot.append((str(path), True, stat.st_size, stat.st_mtime_ns))
        else:
            snapshot.append((str(path), False, None, None))
    return tuple(snapshot)


def _write_protocol_state(path: Path, payload: Mapping[str, object]) -> None:
    path.write_text(json.dumps(dict(payload), ensure_ascii=False), encoding="utf-8")


def _append_protocol_event(
    protocol_state: dict[str, object],
    *,
    protocol_state_path: Path,
    assignment: WorkerAssignment,
    status: str,
    phase: str,
    kind: str,
    summary: str,
) -> None:
    existing_events = protocol_state.setdefault("protocol_events", [])
    if not isinstance(existing_events, list):
        existing_events = []
        protocol_state["protocol_events"] = existing_events
    event_id = f"{assignment.assignment_id}:{len(existing_events) + 1}"
    existing_events.append(
        {
            "event_id": event_id,
            "assignment_id": assignment.assignment_id,
            "worker_id": assignment.worker_id,
            "status": status,
            "phase": phase,
            "kind": kind,
            "timestamp": _now_iso(),
            "summary": summary,
        }
    )
    _write_protocol_state(protocol_state_path, protocol_state)


def _write_result_file(
    *,
    assignment: WorkerAssignment,
    result_path: Path,
    last_message_path: Path,
    stdout_path: Path,
    stderr_path: Path,
    command: list[str],
    exit_code: int,
    protocol_state_path: Path | None = None,
    protocol_payload: Mapping[str, object] | None = None,
) -> None:
    output_text = ""
    last_message_exists = last_message_path.exists()
    if last_message_exists:
        output_text = last_message_path.read_text(encoding="utf-8")

    stderr_text = ""
    if stderr_path.exists():
        stderr_text = stderr_path.read_text(encoding="utf-8").strip()

    thread_id = _extract_thread_id(stdout_path)
    raw_payload: dict[str, object] = {
        "backend": "codex_cli",
        "command": list(command),
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "last_message_path": str(last_message_path),
        "last_message_exists": last_message_exists,
        "exit_code": exit_code,
    }
    if exit_code < 0:
        signal_number = -exit_code
        raw_payload["termination_signal"] = signal_number
        signal_name = _signal_name(signal_number)
        if signal_name is not None:
            raw_payload["termination_signal_name"] = signal_name
    if protocol_state_path is not None:
        raw_payload["protocol_state_file"] = str(protocol_state_path)
    if thread_id is not None:
        raw_payload["thread_id"] = thread_id

    merged_protocol_payload = dict(protocol_payload or {})
    protocol_final_report = merged_protocol_payload.get("final_report")
    protocol_terminal_status = None
    if isinstance(protocol_final_report, Mapping):
        raw_terminal_status = protocol_final_report.get("terminal_status")
        if isinstance(raw_terminal_status, str) and raw_terminal_status.strip():
            protocol_terminal_status = raw_terminal_status.strip().lower()
    if protocol_terminal_status == "completed":
        status = "completed" if exit_code == 0 and output_text else "failed"
    else:
        status = "failed"
    error_text = "" if status == "completed" else (stderr_text or f"codex exited with code {exit_code}")
    if status != "completed":
        raw_payload["last_message_exists_at_failure"] = last_message_exists
        if _is_transient_provider_failure(stderr_text):
            raw_payload["failure_kind"] = "provider_unavailable"
            raw_payload["error_type"] = "ProviderUnavailableError"
    if merged_protocol_payload:
        if "protocol_events" in merged_protocol_payload:
            raw_payload["protocol_events"] = merged_protocol_payload["protocol_events"]
        if "final_report" in merged_protocol_payload:
            raw_payload["final_report"] = merged_protocol_payload["final_report"]
    result = {
        "worker_id": assignment.worker_id,
        "assignment_id": assignment.assignment_id,
        "status": status,
        "output_text": output_text,
        "error_text": error_text,
        "response_id": None,
        "usage": {},
        "raw_payload": raw_payload,
    }
    result_path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")


def _watch_process(
    *,
    process: subprocess.Popen[str],
    assignment: WorkerAssignment,
    result_path: Path,
    last_message_path: Path,
    stdout_path: Path,
    stderr_path: Path,
    command: list[str],
    protocol_state_path: Path | None = None,
) -> None:
    try:
        protocol_state: dict[str, object] = {"protocol_events": []}
        last_snapshot = _progress_snapshot(stdout_path, stderr_path, last_message_path)
        if protocol_state_path is not None:
            _append_protocol_event(
                protocol_state,
                protocol_state_path=protocol_state_path,
                assignment=assignment,
                status="accepted",
                phase="accepted",
                kind="accepted",
                summary="Codex worker launch accepted.",
            )
        while True:
            exit_code = process.poll()
            if exit_code is not None:
                break
            current_snapshot = _progress_snapshot(stdout_path, stderr_path, last_message_path)
            if protocol_state_path is not None and current_snapshot != last_snapshot:
                last_snapshot = current_snapshot
                _append_protocol_event(
                    protocol_state,
                    protocol_state_path=protocol_state_path,
                    assignment=assignment,
                    status="running",
                    phase="checkpoint",
                    kind="checkpoint",
                    summary="Observed codex worker artifact progress.",
                )
            time.sleep(0.1)
        output_text = last_message_path.read_text(encoding="utf-8") if last_message_path.exists() else ""
        stderr_text = stderr_path.read_text(encoding="utf-8").strip() if stderr_path.exists() else ""
        terminal_status = "completed" if exit_code == 0 and output_text else "failed"
        terminal_summary = output_text.strip() or stderr_text or f"codex exited with code {exit_code}"
        if protocol_state_path is not None:
            _append_protocol_event(
                protocol_state,
                protocol_state_path=protocol_state_path,
                assignment=assignment,
                status=terminal_status,
                phase="terminal_report_announced",
                kind="phase_changed",
                summary="Codex worker produced a terminal report.",
            )
            protocol_state["final_report"] = _build_protocol_final_report(
                assignment=assignment,
                output_text=output_text,
                terminal_status=terminal_status,
                terminal_summary=terminal_summary,
            )
            _write_protocol_state(protocol_state_path, protocol_state)
        _write_result_file(
            assignment=assignment,
            result_path=result_path,
            last_message_path=last_message_path,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            command=command,
            exit_code=exit_code,
            protocol_state_path=protocol_state_path,
            protocol_payload=protocol_state,
        )
    except BaseException as exc:  # pragma: no cover - defensive path
        result = {
            "worker_id": assignment.worker_id,
            "assignment_id": assignment.assignment_id,
            "status": "failed",
            "output_text": "",
            "error_text": str(exc),
            "response_id": None,
            "usage": {},
            "raw_payload": {
                "backend": "codex_cli",
                "command": list(command),
                "stdout_path": str(stdout_path),
                "stderr_path": str(stderr_path),
                "last_message_path": str(last_message_path),
            },
        }
        result_path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")


class CodexCliLaunchBackend(LaunchBackend):
    def __init__(
        self,
        *,
        codex_command: tuple[str, ...] = ("codex",),
        spool_root: str | Path | None = None,
        process_launcher: ProcessLauncher | None = None,
        sandbox_mode: str | None = "workspace-write",
        bypass_approvals: bool = False,
        skip_git_repo_check: bool = True,
    ) -> None:
        self.codex_command = tuple(codex_command)
        self.spool_root = Path(spool_root) if spool_root is not None else None
        self.process_launcher = process_launcher or default_process_launcher
        self.sandbox_mode = sandbox_mode
        self.bypass_approvals = bypass_approvals
        self.skip_git_repo_check = skip_git_repo_check

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

    def _build_command(
        self,
        *,
        working_dir: str,
        last_message_path: Path,
    ) -> list[str]:
        command = [*self.codex_command, "exec", "-", "--cd", working_dir]
        if self.skip_git_repo_check:
            command.append("--skip-git-repo-check")
        if self.bypass_approvals:
            command.append("--dangerously-bypass-approvals-and-sandbox")
        elif self.sandbox_mode is not None:
            command.extend(["--sandbox", self.sandbox_mode])
        command.extend(["--output-last-message", str(last_message_path), "--json"])
        return command

    async def launch(self, assignment: WorkerAssignment) -> WorkerHandle:
        capabilities = self.describe_capabilities()
        root = ensure_directory(self.spool_root or Path(tempfile.mkdtemp(prefix="agent-orchestra-codex-")))
        prompt_file = root / f"{assignment.assignment_id}.prompt.md"
        stdout_file = root / f"{assignment.assignment_id}.stdout.jsonl"
        stderr_file = root / f"{assignment.assignment_id}.stderr.log"
        last_message_file = root / f"{assignment.assignment_id}.last_message.txt"
        protocol_state_file = root / f"{assignment.assignment_id}.protocol_state.json"
        result_file = root / f"{assignment.assignment_id}.result.json"

        prompt_file.write_text(_render_prompt(assignment), encoding="utf-8")
        working_dir = str(Path(assignment.working_dir) if assignment.working_dir else Path.cwd())
        command = self._build_command(working_dir=working_dir, last_message_path=last_message_file)

        env = dict(os.environ)
        env.update(assignment.environment)

        prompt_handle = prompt_file.open("r", encoding="utf-8")
        stdout_handle = stdout_file.open("w", encoding="utf-8")
        stderr_handle = stderr_file.open("w", encoding="utf-8")
        try:
            process = self.process_launcher(
                args=command,
                cwd=working_dir,
                env=env,
                stdin=prompt_handle,
                stdout=stdout_handle,
                stderr=stderr_handle,
                text=True,
            )
        finally:
            prompt_handle.close()
            stdout_handle.close()
            stderr_handle.close()

        watcher = threading.Thread(
            target=_watch_process,
            kwargs={
                "process": process,
                "assignment": assignment,
                "result_path": result_file,
                "last_message_path": last_message_file,
                "stdout_path": stdout_file,
                "stderr_path": stderr_file,
                "command": command,
                "protocol_state_path": protocol_state_file,
            },
            daemon=True,
        )
        watcher.start()

        return WorkerHandle(
            worker_id=assignment.worker_id,
            role=assignment.role,
            backend="codex_cli",
            run_id=assignment.assignment_id,
            process_id=process.pid,
            transport_ref=str(result_file),
            metadata={
                "spool_root": str(root),
                "prompt_file": str(prompt_file),
                "stdout_file": str(stdout_file),
                "stderr_file": str(stderr_file),
                "last_message_file": str(last_message_file),
                "protocol_state_file": str(protocol_state_file),
                "command": command,
                "command_fingerprint": command_fingerprint(command),
                **backend_capability_hints(capabilities),
                "_process": process,
                "_watcher": watcher,
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
            raise RuntimeError("codex process unavailable")
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
            raise RuntimeError(f"codex_cli reattach requires {label}")
        if value is None:
            return None
        path = Path(value)
        if not path.exists() and not path.parent.exists():
            raise RuntimeError(f"codex_cli reattach path unavailable: {path}")
        return str(path)

    async def reattach(
        self,
        locator: WorkerTransportLocator,
        assignment: WorkerAssignment,
    ) -> WorkerHandle:
        if assignment.backend != "codex_cli":
            raise RuntimeError(f"codex_cli backend cannot reattach assignment backend `{assignment.backend}`")
        if locator.backend and locator.backend != "codex_cli":
            raise RuntimeError(f"codex_cli backend cannot reattach locator backend `{locator.backend}`")
        if locator.pid is None:
            raise RuntimeError("codex_cli reattach requires pid")
        if not is_process_alive(locator.pid):
            raise RuntimeError(f"codex_cli process not alive: {locator.pid}")
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
        expected_fingerprint = assignment.metadata.get("command_fingerprint")
        if not isinstance(expected_fingerprint, str) or not expected_fingerprint:
            expected_fingerprint = None
        locator_fingerprint = locator.command_fingerprint
        if locator_fingerprint is None:
            candidate = locator.metadata.get("command_fingerprint")
            if isinstance(candidate, str) and candidate:
                locator_fingerprint = candidate
        if expected_fingerprint is not None and locator_fingerprint != expected_fingerprint:
            raise RuntimeError("codex_cli reattach command fingerprint mismatch")
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
        if locator_fingerprint is not None:
            metadata["command_fingerprint"] = locator_fingerprint
        return WorkerHandle(
            worker_id=assignment.worker_id,
            role=assignment.role,
            backend="codex_cli",
            run_id=assignment.assignment_id,
            process_id=locator.pid,
            transport_ref=result_file,
            metadata=metadata,
        )
