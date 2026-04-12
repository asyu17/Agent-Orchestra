from __future__ import annotations

import hashlib
import os
import shlex
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from agent_orchestra.contracts.execution import (
    LaunchBackend,
    WorkerBackendCapabilities,
    WorkerTransportClass,
)


@dataclass(slots=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


CommandRunner = Callable[[list[str], str | None], CommandResult]


def default_command_runner(command: list[str], cwd: str | None = None) -> CommandResult:
    completed = subprocess.run(
        command,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    return CommandResult(
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


def shell_join(parts: list[str]) -> str:
    return shlex.join(parts)


class BackendRegistry:
    def __init__(self, backends: dict[str, LaunchBackend] | None = None) -> None:
        self._backends: dict[str, LaunchBackend] = dict(backends or {})

    def register(self, name: str, backend: LaunchBackend) -> None:
        self._backends[name] = backend

    def get(self, name: str) -> LaunchBackend:
        try:
            return self._backends[name]
        except KeyError as exc:
            raise ValueError(f"Unknown backend: {name}") from exc

    def names(self) -> tuple[str, ...]:
        return tuple(self._backends.keys())

    def as_mapping(self) -> dict[str, LaunchBackend]:
        return dict(self._backends)


def describe_backend_capabilities(backend: LaunchBackend | None) -> WorkerBackendCapabilities:
    if backend is None:
        return WorkerBackendCapabilities()
    describe = getattr(backend, "describe_capabilities", None)
    if callable(describe):
        capabilities = describe()
        if isinstance(capabilities, WorkerBackendCapabilities):
            return capabilities
    return WorkerBackendCapabilities()


def backend_capability_hints(
    capabilities: WorkerBackendCapabilities,
) -> dict[str, bool | str]:
    hints: dict[str, bool | str] = {
        "transport_class": capabilities.transport_class.value,
    }
    if capabilities.supports_resume:
        hints["resume_supported"] = True
    if capabilities.supports_reactivate:
        hints["reactivate_supported"] = True
    if capabilities.supports_reattach:
        hints["reattach_supported"] = True
    return hints


def describe_backend_capability_hints(backend: LaunchBackend | None) -> dict[str, bool | str]:
    return backend_capability_hints(describe_backend_capabilities(backend))


def ensure_directory(path: str | Path) -> Path:
    target = Path(path)
    target.mkdir(parents=True, exist_ok=True)
    return target


def command_fingerprint(command: list[str]) -> str:
    encoded = "\x00".join(command).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def is_process_alive(pid: int | None) -> bool:
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def terminate_process_by_pid(pid: int | None) -> None:
    if pid is None:
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
