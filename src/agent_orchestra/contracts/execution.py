from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
import shlex
from typing import Any

from agent_orchestra.contracts.enums import WorkerStatus
from agent_orchestra.contracts.worker_protocol import (
    WorkerExecutionContract,
    WorkerFinalReport,
    WorkerLeasePolicy,
    WorkerRoleProfile,
)


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return str(value)


def verification_command_equivalence_key(command: str) -> str:
    normalized = command.strip()
    if not normalized:
        return ""
    try:
        tokens = shlex.split(normalized, posix=True)
    except ValueError:
        tokens = normalized.split()
    if not tokens:
        return ""
    return "\0".join(tokens)


def select_best_equivalent_verification_result(
    results: Sequence["VerificationCommandResult"],
    command: str,
) -> "VerificationCommandResult | None":
    return select_preferred_equivalent_verification_result(
        command=command,
        results=results,
    )


@dataclass(slots=True)
class WorkerBudget:
    max_teammates: int = 0
    max_iterations: int = 0
    max_tokens: int | None = None
    max_seconds: int | None = None


@dataclass(slots=True)
class LeaderTaskCard:
    task_id: str
    objective_id: str
    leader_id: str
    title: str
    summary: str
    budget: WorkerBudget
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class WorkerHandle:
    worker_id: str
    role: str
    backend: str
    run_id: str | None = None
    process_id: int | None = None
    session_name: str | None = None
    transport_ref: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class WorkerTransportClass(str, Enum):
    FULL_RESIDENT_TRANSPORT = "full_resident_transport"
    EPHEMERAL_WORKER_TRANSPORT = "ephemeral_worker_transport"


@dataclass(frozen=True, slots=True)
class WorkerBackendCapabilities:
    transport_class: WorkerTransportClass = WorkerTransportClass.EPHEMERAL_WORKER_TRANSPORT
    supports_protocol_contract: bool = False
    supports_protocol_state: bool = False
    supports_protocol_final_report: bool = False
    supports_resume: bool = False
    supports_reactivate: bool = False
    supports_reattach: bool = False
    supports_artifact_progress: bool = False
    supports_verification_in_working_dir: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "transport_class": self.transport_class.value,
            "supports_protocol_contract": self.supports_protocol_contract,
            "supports_protocol_state": self.supports_protocol_state,
            "supports_protocol_final_report": self.supports_protocol_final_report,
            "supports_resume": self.supports_resume,
            "supports_reactivate": self.supports_reactivate,
            "supports_reattach": self.supports_reattach,
            "supports_artifact_progress": self.supports_artifact_progress,
            "supports_verification_in_working_dir": self.supports_verification_in_working_dir,
        }


class WorkerAttemptDecision(str, Enum):
    COMPLETE = "complete"
    RESUME = "resume"
    RETRY = "retry"
    FALLBACK = "fallback"
    ESCALATE = "escalate"


class WorkerSessionStatus(str, Enum):
    ASSIGNED = "assigned"
    ACTIVE = "active"
    IDLE = "idle"
    COMPLETED = "completed"
    FAILED = "failed"
    ABANDONED = "abandoned"
    # Backward-compatibility alias for legacy callers that still reference `CLOSED`
    # in code; durable payloads normalize legacy `closed` snapshots to `abandoned`.
    CLOSED = "abandoned"


class ResidentCoordinatorPhase(str, Enum):
    BOOTING = "booting"
    RUNNING = "running"
    IDLE = "idle"
    WAITING_FOR_MAILBOX = "waiting_for_mailbox"
    WAITING_FOR_DEPENDENCIES = "waiting_for_dependencies"
    WAITING_FOR_SUBORDINATES = "waiting_for_subordinates"
    QUIESCENT = "quiescent"
    SHUTDOWN_REQUESTED = "shutdown_requested"
    FAILED = "failed"


class ExecutionGuardStatus(str, Enum):
    PASSED = "passed"
    SCOPE_DRIFT = "scope_drift"
    PATH_VIOLATION = "path_violation"
    VERIFICATION_FAILED = "verification_failed"
    GUARD_ERROR = "guard_error"


class WorkerFailureKind(str, Enum):
    ORDINARY = "ordinary"
    TIMEOUT = "timeout"
    PROVIDER_UNAVAILABLE = "provider_unavailable"


@dataclass(slots=True)
class WorkerProviderRoute:
    route_id: str
    backend: str
    metadata: dict[str, Any] = field(default_factory=dict)
    environment: dict[str, str] = field(default_factory=dict)
    backoff_seconds: float | None = None


@dataclass(slots=True)
class WorkerExecutionPolicy:
    max_attempts: int = 2
    attempt_timeout_seconds: float | None = None
    idle_timeout_seconds: float | None = None
    hard_timeout_seconds: float | None = None
    resume_on_timeout: bool = True
    resume_on_failure: bool = False
    allow_relaunch: bool = True
    escalate_after_attempts: bool = True
    backoff_seconds: float = 0.0
    provider_unavailable_backoff_initial_seconds: float = 0.0
    provider_unavailable_backoff_multiplier: float = 2.0
    provider_unavailable_backoff_max_seconds: float = 0.0
    keep_session_idle: bool = False
    reactivate_idle_session: bool = True
    fallback_on_provider_unavailable: bool = True
    provider_fallbacks: tuple[WorkerProviderRoute, ...] = ()
    provider_unavailable_error_types: tuple[str, ...] = ("ProviderUnavailableError",)
    provider_unavailable_substrings: tuple[str, ...] = (
        "provider unavailable",
        "provider overloaded",
        "rate limited",
        "rate limit",
        "capacity",
        "over capacity",
    )
    execution_contract: WorkerExecutionContract | None = None
    lease_policy: WorkerLeasePolicy | None = None
    role_profile_id: str | None = None

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if self.attempt_timeout_seconds is not None and self.attempt_timeout_seconds <= 0:
            raise ValueError("attempt_timeout_seconds must be positive when provided")
        if self.idle_timeout_seconds is not None and self.idle_timeout_seconds <= 0:
            raise ValueError("idle_timeout_seconds must be positive when provided")
        if self.hard_timeout_seconds is not None and self.hard_timeout_seconds <= 0:
            raise ValueError("hard_timeout_seconds must be positive when provided")
        if (
            self.idle_timeout_seconds is not None
            and self.hard_timeout_seconds is not None
            and self.hard_timeout_seconds < self.idle_timeout_seconds
        ):
            raise ValueError("hard_timeout_seconds must be greater than or equal to idle_timeout_seconds")
        if self.backoff_seconds < 0:
            raise ValueError("backoff_seconds must be non-negative")
        if self.provider_unavailable_backoff_initial_seconds < 0:
            raise ValueError("provider_unavailable_backoff_initial_seconds must be non-negative")
        if self.provider_unavailable_backoff_multiplier < 1.0:
            raise ValueError("provider_unavailable_backoff_multiplier must be >= 1.0")
        if self.provider_unavailable_backoff_max_seconds < 0:
            raise ValueError("provider_unavailable_backoff_max_seconds must be non-negative")
        normalized_routes: list[WorkerProviderRoute] = []
        seen_route_ids: set[str] = set()
        for index, route in enumerate(self.provider_fallbacks):
            if isinstance(route, WorkerProviderRoute):
                normalized = route
            elif isinstance(route, Mapping):
                route_id = route.get("route_id")
                backend = route.get("backend")
                if not isinstance(route_id, str) or not route_id.strip():
                    raise ValueError("provider_fallbacks route_id must be a non-empty string")
                if not isinstance(backend, str) or not backend.strip():
                    raise ValueError("provider_fallbacks backend must be a non-empty string")
                metadata = route.get("metadata", {})
                environment = route.get("environment", {})
                backoff_seconds = route.get("backoff_seconds")
                if metadata is None:
                    metadata = {}
                if environment is None:
                    environment = {}
                if not isinstance(metadata, Mapping):
                    raise ValueError("provider_fallbacks metadata must be a mapping when provided")
                if not isinstance(environment, Mapping):
                    raise ValueError("provider_fallbacks environment must be a mapping when provided")
                if backoff_seconds is not None and (
                    not isinstance(backoff_seconds, (int, float)) or backoff_seconds < 0
                ):
                    raise ValueError("provider_fallbacks backoff_seconds must be non-negative when provided")
                normalized = WorkerProviderRoute(
                    route_id=route_id.strip(),
                    backend=backend.strip(),
                    metadata={str(key): value for key, value in metadata.items()},
                    environment={str(key): str(value) for key, value in environment.items()},
                    backoff_seconds=float(backoff_seconds) if backoff_seconds is not None else None,
                )
            else:
                raise ValueError("provider_fallbacks entries must be WorkerProviderRoute or mapping values")
            if normalized.backoff_seconds is not None and normalized.backoff_seconds < 0:
                raise ValueError("provider_fallbacks backoff_seconds must be non-negative")
            if normalized.route_id in seen_route_ids:
                raise ValueError(f"provider_fallbacks route_id must be unique: {normalized.route_id}")
            seen_route_ids.add(normalized.route_id)
            normalized_routes.append(normalized)
        object.__setattr__(self, "provider_fallbacks", tuple(normalized_routes))


@dataclass(slots=True)
class WorkerAssignment:
    assignment_id: str
    worker_id: str
    group_id: str
    task_id: str
    role: str
    backend: str
    instructions: str
    input_text: str
    conversation: tuple[Mapping[str, Any], ...] = ()
    previous_response_id: str | None = None
    objective_id: str | None = None
    team_id: str | None = None
    lane_id: str | None = None
    working_dir: str | None = None
    environment: dict[str, str] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    execution_contract: WorkerExecutionContract | None = None
    lease_policy: WorkerLeasePolicy | None = None
    role_profile: WorkerRoleProfile | None = None


@dataclass(slots=True)
class WorkerResult:
    worker_id: str
    assignment_id: str
    status: WorkerStatus
    output_text: str = ""
    error_text: str = ""
    response_id: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    raw_payload: dict[str, Any] = field(default_factory=dict)
    protocol_events: tuple[dict[str, Any], ...] = ()
    final_report: WorkerFinalReport | None = None


@dataclass(slots=True)
class WorkerTransportLocator:
    backend: str
    working_dir: str | None = None
    spool_dir: str | None = None
    protocol_state_file: str | None = None
    result_file: str | None = None
    stdout_file: str | None = None
    stderr_file: str | None = None
    last_message_file: str | None = None
    pid: int | None = None
    process_group_id: int | None = None
    session_name: str | None = None
    pane_id: str | None = None
    command_fingerprint: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "working_dir": self.working_dir,
            "spool_dir": self.spool_dir,
            "protocol_state_file": self.protocol_state_file,
            "result_file": self.result_file,
            "stdout_file": self.stdout_file,
            "stderr_file": self.stderr_file,
            "last_message_file": self.last_message_file,
            "pid": self.pid,
            "process_group_id": self.process_group_id,
            "session_name": self.session_name,
            "pane_id": self.pane_id,
            "command_fingerprint": self.command_fingerprint,
            "metadata": _json_safe(dict(self.metadata)),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> WorkerTransportLocator:
        known_keys = {
            "backend",
            "working_dir",
            "spool_dir",
            "protocol_state_file",
            "result_file",
            "stdout_file",
            "stderr_file",
            "last_message_file",
            "pid",
            "process_group_id",
            "session_name",
            "pane_id",
            "command_fingerprint",
            "metadata",
        }
        metadata = dict(payload.get("metadata", {}))
        for key, value in payload.items():
            if key not in known_keys:
                metadata[str(key)] = value
        pid_payload = payload.get("pid")
        if pid_payload is None:
            pid_payload = payload.get("process_id")
        if pid_payload is None and metadata.get("process_id") is not None:
            pid_payload = metadata.get("process_id")
        process_group_payload = payload.get("process_group_id")
        if process_group_payload is None and metadata.get("process_group_id") is not None:
            process_group_payload = metadata.get("process_group_id")
        result_file_payload = payload.get("result_file")
        if result_file_payload is None:
            result_file_payload = payload.get("transport_ref")
        if result_file_payload is None and metadata.get("transport_ref") is not None:
            result_file_payload = metadata.get("transport_ref")
        protocol_state_payload = payload.get("protocol_state_file")
        if protocol_state_payload is None and metadata.get("protocol_state_file") is not None:
            protocol_state_payload = metadata.get("protocol_state_file")
        working_dir_payload = payload.get("working_dir")
        if working_dir_payload is None and metadata.get("working_dir") is not None:
            working_dir_payload = metadata.get("working_dir")
        spool_dir_payload = payload.get("spool_dir")
        if spool_dir_payload is None and metadata.get("spool_root") is not None:
            spool_dir_payload = metadata.get("spool_root")
        stdout_file_payload = payload.get("stdout_file")
        if stdout_file_payload is None and metadata.get("stdout_file") is not None:
            stdout_file_payload = metadata.get("stdout_file")
        stderr_file_payload = payload.get("stderr_file")
        if stderr_file_payload is None and metadata.get("stderr_file") is not None:
            stderr_file_payload = metadata.get("stderr_file")
        last_message_payload = payload.get("last_message_file")
        if last_message_payload is None and metadata.get("last_message_file") is not None:
            last_message_payload = metadata.get("last_message_file")
        command_fingerprint_payload = payload.get("command_fingerprint")
        if command_fingerprint_payload is None and metadata.get("command_fingerprint") is not None:
            command_fingerprint_payload = metadata.get("command_fingerprint")
        return cls(
            backend=str(payload.get("backend", "")),
            working_dir=str(working_dir_payload) if working_dir_payload is not None else None,
            spool_dir=str(spool_dir_payload) if spool_dir_payload is not None else None,
            protocol_state_file=str(protocol_state_payload) if protocol_state_payload is not None else None,
            result_file=str(result_file_payload) if result_file_payload is not None else None,
            stdout_file=str(stdout_file_payload) if stdout_file_payload is not None else None,
            stderr_file=str(stderr_file_payload) if stderr_file_payload is not None else None,
            last_message_file=str(last_message_payload) if last_message_payload is not None else None,
            pid=int(pid_payload) if pid_payload is not None else None,
            process_group_id=int(process_group_payload) if process_group_payload is not None else None,
            session_name=str(payload["session_name"]) if payload.get("session_name") is not None else None,
            pane_id=str(payload["pane_id"]) if payload.get("pane_id") is not None else None,
            command_fingerprint=(
                str(command_fingerprint_payload) if command_fingerprint_payload is not None else None
            ),
            metadata=metadata,
        )


@dataclass(slots=True)
class WorkerSession:
    session_id: str
    worker_id: str
    backend: str
    role: str
    status: WorkerSessionStatus
    assignment_id: str | None = None
    lifecycle_status: str | None = None
    started_at: str | None = None
    last_active_at: str | None = None
    idle_since: str | None = None
    protocol_cursor: dict[str, Any] = field(default_factory=dict)
    mailbox_cursor: dict[str, Any] = field(default_factory=dict)
    supervisor_id: str | None = None
    supervisor_lease_id: str | None = None
    supervisor_lease_expires_at: str | None = None
    transport_locator: WorkerTransportLocator | None = None
    reattach_count: int = 0
    last_assignment_id: str | None = None
    last_response_id: str | None = None
    reactivation_count: int = 0
    handle_snapshot: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.assignment_id is None and self.last_assignment_id is not None:
            self.assignment_id = self.last_assignment_id
        if self.last_assignment_id is None and self.assignment_id is not None:
            self.last_assignment_id = self.assignment_id
        if self.transport_locator is None and self.handle_snapshot:
            payload = {"backend": self.backend}
            payload.update(self.handle_snapshot)
            self.transport_locator = WorkerTransportLocator.from_dict(payload)
        if self.transport_locator is not None and not self.handle_snapshot:
            self.handle_snapshot = self.transport_locator.to_dict()

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "worker_id": self.worker_id,
            "assignment_id": self.assignment_id,
            "backend": self.backend,
            "role": self.role,
            "status": self.status.value,
            "lifecycle_status": self.lifecycle_status,
            "started_at": self.started_at,
            "last_active_at": self.last_active_at,
            "idle_since": self.idle_since,
            "protocol_cursor": _json_safe(dict(self.protocol_cursor)),
            "mailbox_cursor": _json_safe(dict(self.mailbox_cursor)),
            "supervisor_id": self.supervisor_id,
            "supervisor_lease_id": self.supervisor_lease_id,
            "supervisor_lease_expires_at": self.supervisor_lease_expires_at,
            "transport_locator": (
                self.transport_locator.to_dict() if self.transport_locator is not None else None
            ),
            "reattach_count": self.reattach_count,
            "last_assignment_id": self.last_assignment_id,
            "last_response_id": self.last_response_id,
            "reactivation_count": self.reactivation_count,
            "handle_snapshot": _json_safe(dict(self.handle_snapshot)),
            "metadata": _json_safe(dict(self.metadata)),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> WorkerSession:
        status_raw = str(payload["status"])
        if status_raw == "closed":
            status_raw = WorkerSessionStatus.ABANDONED.value
        assignment_id = str(payload["assignment_id"]) if payload.get("assignment_id") is not None else None
        last_assignment_id = (
            str(payload["last_assignment_id"])
            if payload.get("last_assignment_id") is not None
            else assignment_id
        )
        handle_snapshot = dict(payload.get("handle_snapshot", {}))
        transport_locator_payload = payload.get("transport_locator")
        transport_locator: WorkerTransportLocator | None = None
        if isinstance(transport_locator_payload, Mapping):
            transport_locator = WorkerTransportLocator.from_dict(transport_locator_payload)
        elif handle_snapshot:
            locator_payload = {"backend": payload.get("backend")}
            locator_payload.update(handle_snapshot)
            transport_locator = WorkerTransportLocator.from_dict(locator_payload)
        return cls(
            session_id=str(payload["session_id"]),
            worker_id=str(payload["worker_id"]),
            assignment_id=assignment_id,
            backend=str(payload["backend"]),
            role=str(payload["role"]),
            status=WorkerSessionStatus(status_raw),
            lifecycle_status=(
                str(payload["lifecycle_status"]) if payload.get("lifecycle_status") is not None else None
            ),
            started_at=str(payload["started_at"]) if payload.get("started_at") is not None else None,
            last_active_at=(
                str(payload["last_active_at"]) if payload.get("last_active_at") is not None else None
            ),
            idle_since=str(payload["idle_since"]) if payload.get("idle_since") is not None else None,
            protocol_cursor=dict(payload.get("protocol_cursor", {})),
            mailbox_cursor=dict(payload.get("mailbox_cursor", {})),
            supervisor_id=str(payload["supervisor_id"]) if payload.get("supervisor_id") is not None else None,
            supervisor_lease_id=(
                str(payload["supervisor_lease_id"])
                if payload.get("supervisor_lease_id") is not None
                else None
            ),
            supervisor_lease_expires_at=(
                str(payload["supervisor_lease_expires_at"])
                if payload.get("supervisor_lease_expires_at") is not None
                else None
            ),
            transport_locator=transport_locator,
            reattach_count=int(payload.get("reattach_count", 0)),
            last_assignment_id=last_assignment_id,
            last_response_id=(
                str(payload["last_response_id"]) if payload.get("last_response_id") is not None else None
            ),
            reactivation_count=int(payload.get("reactivation_count", 0)),
            handle_snapshot=handle_snapshot,
            metadata=dict(payload.get("metadata", {})),
        )


class WorkerSessionStore(ABC):
    @abstractmethod
    async def save_worker_session(self, session: WorkerSession) -> None:
        raise NotImplementedError

    @abstractmethod
    async def get_worker_session(self, session_id: str) -> WorkerSession | None:
        raise NotImplementedError


@dataclass(slots=True)
class ResidentCoordinatorSession:
    coordinator_id: str
    role: str
    phase: ResidentCoordinatorPhase
    objective_id: str
    lane_id: str | None = None
    team_id: str | None = None
    cycle_count: int = 0
    prompt_turn_count: int = 0
    claimed_task_count: int = 0
    subordinate_dispatch_count: int = 0
    mailbox_poll_count: int = 0
    idle_transition_count: int = 0
    quiescent_transition_count: int = 0
    active_subordinate_ids: tuple[str, ...] = ()
    mailbox_cursor: str | None = None
    last_reason: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class WorkerRecord:
    worker_id: str
    assignment_id: str
    backend: str
    role: str
    status: WorkerStatus
    handle: WorkerHandle | None = None
    started_at: str | None = None
    ended_at: str | None = None
    last_heartbeat_at: str | None = None
    output_text: str = ""
    error_text: str = ""
    response_id: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    session: WorkerSession | None = None


@dataclass(slots=True)
class WorkerEscalation:
    assignment_id: str
    worker_id: str
    attempt_count: int
    reason: str
    backend: str
    last_error: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class VerificationCommandResult:
    command: str
    returncode: int
    requested_command: str | None = None
    stdout: str = ""
    stderr: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "command": self.command,
            "returncode": self.returncode,
            "requested_command": self.requested_command,
            "stdout": self.stdout,
            "stderr": self.stderr,
        }

    @classmethod
    def from_payload(cls, payload: object) -> "VerificationCommandResult | None":
        if isinstance(payload, cls):
            return payload
        if not isinstance(payload, Mapping):
            return None
        command = payload.get("command")
        returncode = payload.get("returncode")
        if not isinstance(command, str) or not command.strip():
            return None
        try:
            parsed_returncode = int(returncode)
        except (TypeError, ValueError):
            return None
        stdout = payload.get("stdout", "")
        stderr = payload.get("stderr", "")
        requested_command = payload.get("requested_command")
        return cls(
            command=command,
            returncode=parsed_returncode,
            requested_command=(
                requested_command.strip()
                if isinstance(requested_command, str) and requested_command.strip()
                else None
            ),
            stdout=stdout if isinstance(stdout, str) else str(stdout),
            stderr=stderr if isinstance(stderr, str) else str(stderr),
        )

    def is_equivalent_to(self, command: str) -> bool:
        if self.command.strip() == command.strip():
            return True
        if self.requested_command is not None and self.requested_command.strip() == command.strip():
            return True
        reported_key = verification_command_equivalence_key(
            self.requested_command if self.requested_command is not None else self.command
        )
        expected_key = verification_command_equivalence_key(command)
        return bool(reported_key) and reported_key == expected_key


def select_preferred_equivalent_verification_result(
    *,
    command: str,
    results: Sequence[VerificationCommandResult],
) -> VerificationCommandResult | None:
    """Pick authoritative verification result for one required command.

    Selection rule:
    1) among equivalent results, prefer the last successful result (returncode == 0)
    2) if none succeeded, use the last equivalent result
    """
    last_equivalent: VerificationCommandResult | None = None
    last_successful: VerificationCommandResult | None = None
    for result in results:
        if not result.is_equivalent_to(command):
            continue
        last_equivalent = result
        if result.returncode == 0:
            last_successful = result
    if last_successful is not None:
        return last_successful
    return last_equivalent


def pop_preferred_equivalent_verification_result(
    *,
    command: str,
    results: list[VerificationCommandResult],
) -> VerificationCommandResult | None:
    selected_index: int | None = None
    selected_successful_index: int | None = None
    for index, result in enumerate(results):
        if not result.is_equivalent_to(command):
            continue
        selected_index = index
        if result.returncode == 0:
            selected_successful_index = index
    chosen_index = (
        selected_successful_index if selected_successful_index is not None else selected_index
    )
    if chosen_index is None:
        return None
    return results.pop(chosen_index)


@dataclass(slots=True)
class ExecutionGuardResult:
    status: ExecutionGuardStatus
    modified_paths: tuple[str, ...] = ()
    out_of_scope_paths: tuple[str, ...] = ()
    verification_results: tuple[VerificationCommandResult, ...] = ()
    summary: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


class Planner(ABC):
    @abstractmethod
    async def build_initial_plan(self, objective: Any) -> Any:
        raise NotImplementedError

    @abstractmethod
    async def replan(self, objective: Any, context: Any) -> Any:
        raise NotImplementedError


class LaunchBackend(ABC):
    def describe_capabilities(self) -> WorkerBackendCapabilities:
        return WorkerBackendCapabilities()

    @abstractmethod
    async def launch(self, assignment: WorkerAssignment) -> WorkerHandle:
        raise NotImplementedError

    @abstractmethod
    async def cancel(self, handle: WorkerHandle) -> None:
        raise NotImplementedError

    @abstractmethod
    async def resume(self, handle: WorkerHandle, assignment: WorkerAssignment | None = None) -> WorkerHandle:
        raise NotImplementedError

    async def reattach(
        self,
        locator: WorkerTransportLocator,
        assignment: WorkerAssignment,
    ) -> WorkerHandle:
        raise RuntimeError(
            f"Backend `{assignment.backend}` does not support reattach for active worker recovery."
        )


class WorkerSupervisor(ABC):
    @abstractmethod
    async def run_assignment_with_policy(
        self,
        assignment: WorkerAssignment,
        *,
        launch: Callable[[WorkerAssignment], Awaitable[WorkerHandle]],
        resume: Callable[[WorkerHandle, WorkerAssignment | None], Awaitable[WorkerHandle]],
        cancel: Callable[[WorkerHandle], Awaitable[None]] | None = None,
        policy: WorkerExecutionPolicy | None = None,
    ) -> WorkerRecord:
        raise NotImplementedError

    @abstractmethod
    async def start(self, handle: WorkerHandle, assignment: WorkerAssignment) -> None:
        raise NotImplementedError

    @abstractmethod
    async def wait(
        self,
        handle: WorkerHandle,
        timeout_seconds: float | None = None,
        policy: WorkerExecutionPolicy | None = None,
    ) -> WorkerRecord:
        raise NotImplementedError

    @abstractmethod
    async def complete(self, handle: WorkerHandle, result: WorkerResult) -> WorkerRecord:
        raise NotImplementedError

    @abstractmethod
    async def fail(self, handle: WorkerHandle, error: BaseException | str | Any) -> WorkerRecord:
        raise NotImplementedError
