from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any

from agent_orchestra.contracts.authority import ScopeExtensionRequest


class WorkerLeaseStatus(str, Enum):
    PENDING_ACCEPT = "pending_accept"
    ACTIVE = "active"
    EXPIRED = "expired"
    CLOSED = "closed"


class WorkerLifecycleStatus(str, Enum):
    ASSIGNED = "assigned"
    ACCEPTED = "accepted"
    RUNNING = "running"
    WAITING_ON_SUBTASKS = "waiting_on_subtasks"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    FAILED = "failed"
    ABANDONED = "abandoned"


class WorkerFinalStatus(str, Enum):
    COMPLETED = "completed"
    BLOCKED = "blocked"
    FAILED = "failed"
    ABANDONED = "abandoned"


@dataclass(slots=True)
class WorkerExecutionContract:
    contract_id: str
    mode: str
    allow_subdelegation: bool = False
    require_final_report: bool = True
    require_verification_results: bool = False
    required_verification_commands: tuple[str, ...] = ()
    completion_requires_verification_success: bool = False
    required_artifact_kinds: tuple[str, ...] = ()
    progress_policy: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.contract_id.strip():
            raise ValueError("contract_id must be a non-empty string")
        if not self.mode.strip():
            raise ValueError("mode must be a non-empty string")
        if self.completion_requires_verification_success and not self.require_verification_results:
            raise ValueError(
                "completion_requires_verification_success requires require_verification_results"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_id": self.contract_id,
            "mode": self.mode,
            "allow_subdelegation": self.allow_subdelegation,
            "require_final_report": self.require_final_report,
            "require_verification_results": self.require_verification_results,
            "required_verification_commands": list(self.required_verification_commands),
            "completion_requires_verification_success": self.completion_requires_verification_success,
            "required_artifact_kinds": list(self.required_artifact_kinds),
            "progress_policy": dict(self.progress_policy),
            "metadata": dict(self.metadata),
        }


@dataclass(slots=True)
class WorkerLeasePolicy:
    accept_deadline_seconds: float
    renewal_timeout_seconds: float
    hard_deadline_seconds: float
    renew_on_event_kinds: tuple[str, ...] = (
        "accepted",
        "checkpoint",
        "phase_changed",
        "verifying",
    )
    max_silence_seconds: float | None = None

    def __post_init__(self) -> None:
        if self.accept_deadline_seconds <= 0:
            raise ValueError("accept_deadline_seconds must be positive")
        if self.renewal_timeout_seconds <= 0:
            raise ValueError("renewal_timeout_seconds must be positive")
        if self.hard_deadline_seconds < self.renewal_timeout_seconds:
            raise ValueError("hard_deadline_seconds must be >= renewal_timeout_seconds")
        if self.max_silence_seconds is not None and self.max_silence_seconds <= 0:
            raise ValueError("max_silence_seconds must be positive when provided")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class WorkerLease:
    lease_id: str
    assignment_id: str
    worker_id: str
    issued_at: str
    accepted_at: str | None
    renewed_at: str | None
    expires_at: str
    hard_deadline_at: str
    status: WorkerLeaseStatus
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["status"] = self.status.value
        return payload


@dataclass(slots=True)
class WorkerLifecycleEvent:
    event_id: str
    assignment_id: str
    worker_id: str
    status: WorkerLifecycleStatus
    phase: str
    kind: str = ""
    timestamp: str | None = None
    summary: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["status"] = self.status.value
        return payload


@dataclass(slots=True)
class WorkerFinalReport:
    assignment_id: str
    worker_id: str
    terminal_status: WorkerFinalStatus
    summary: str
    artifact_refs: tuple[str, ...] = ()
    verification_results: tuple[dict[str, Any], ...] = ()
    pending_verification_commands: tuple[str, ...] = ()
    blocker: str = ""
    missing_dependencies: tuple[str, ...] = ()
    retry_hint: str = ""
    authority_request: ScopeExtensionRequest | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "assignment_id": self.assignment_id,
            "worker_id": self.worker_id,
            "terminal_status": self.terminal_status.value,
            "summary": self.summary,
            "artifact_refs": list(self.artifact_refs),
            "verification_results": [dict(item) for item in self.verification_results],
            "pending_verification_commands": list(self.pending_verification_commands),
            "blocker": self.blocker,
            "missing_dependencies": list(self.missing_dependencies),
            "retry_hint": self.retry_hint,
            "authority_request": (
                self.authority_request.to_dict()
                if self.authority_request is not None
                else None
            ),
            "metadata": dict(self.metadata),
        }


@dataclass(slots=True)
class WorkerRoleProfile:
    profile_id: str
    backend: str
    execution_contract: WorkerExecutionContract
    lease_policy: WorkerLeasePolicy
    keep_session_idle: bool = False
    reactivate_idle_session: bool = True
    fallback_idle_timeout_seconds: float | None = None
    fallback_hard_timeout_seconds: float | None = None
    fallback_max_attempts: int = 2
    fallback_attempt_timeout_seconds: float | None = None
    fallback_resume_on_timeout: bool = True
    fallback_resume_on_failure: bool = False
    fallback_allow_relaunch: bool = True
    fallback_escalate_after_attempts: bool = True
    fallback_backoff_seconds: float = 0.0
    fallback_provider_unavailable_backoff_initial_seconds: float = 0.0
    fallback_provider_unavailable_backoff_multiplier: float = 2.0
    fallback_provider_unavailable_backoff_max_seconds: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.profile_id.strip():
            raise ValueError("profile_id must be a non-empty string")
        if not self.backend.strip():
            raise ValueError("backend must be a non-empty string")
        if self.fallback_max_attempts < 1:
            raise ValueError("fallback_max_attempts must be at least 1")
        if (
            self.fallback_idle_timeout_seconds is not None
            and self.fallback_idle_timeout_seconds <= 0
        ):
            raise ValueError("fallback_idle_timeout_seconds must be positive when provided")
        if (
            self.fallback_hard_timeout_seconds is not None
            and self.fallback_hard_timeout_seconds <= 0
        ):
            raise ValueError("fallback_hard_timeout_seconds must be positive when provided")
        if (
            self.fallback_idle_timeout_seconds is not None
            and self.fallback_hard_timeout_seconds is not None
            and self.fallback_hard_timeout_seconds < self.fallback_idle_timeout_seconds
        ):
            raise ValueError(
                "fallback_hard_timeout_seconds must be >= fallback_idle_timeout_seconds"
            )
        if (
            self.fallback_attempt_timeout_seconds is not None
            and self.fallback_attempt_timeout_seconds <= 0
        ):
            raise ValueError("fallback_attempt_timeout_seconds must be positive when provided")
        if self.fallback_backoff_seconds < 0:
            raise ValueError("fallback_backoff_seconds must be non-negative")
        if self.fallback_provider_unavailable_backoff_initial_seconds < 0:
            raise ValueError(
                "fallback_provider_unavailable_backoff_initial_seconds must be non-negative"
            )
        if self.fallback_provider_unavailable_backoff_multiplier < 1.0:
            raise ValueError("fallback_provider_unavailable_backoff_multiplier must be >= 1.0")
        if self.fallback_provider_unavailable_backoff_max_seconds < 0:
            raise ValueError(
                "fallback_provider_unavailable_backoff_max_seconds must be non-negative"
            )

    def to_execution_policy(self):
        from agent_orchestra.contracts.execution import WorkerExecutionPolicy

        return WorkerExecutionPolicy(
            max_attempts=self.fallback_max_attempts,
            attempt_timeout_seconds=self.fallback_attempt_timeout_seconds,
            idle_timeout_seconds=self.fallback_idle_timeout_seconds,
            hard_timeout_seconds=self.fallback_hard_timeout_seconds,
            resume_on_timeout=self.fallback_resume_on_timeout,
            resume_on_failure=self.fallback_resume_on_failure,
            allow_relaunch=self.fallback_allow_relaunch,
            escalate_after_attempts=self.fallback_escalate_after_attempts,
            backoff_seconds=self.fallback_backoff_seconds,
            provider_unavailable_backoff_initial_seconds=(
                self.fallback_provider_unavailable_backoff_initial_seconds
            ),
            provider_unavailable_backoff_multiplier=(
                self.fallback_provider_unavailable_backoff_multiplier
            ),
            provider_unavailable_backoff_max_seconds=(
                self.fallback_provider_unavailable_backoff_max_seconds
            ),
            keep_session_idle=self.keep_session_idle,
            reactivate_idle_session=self.reactivate_idle_session,
            execution_contract=self.execution_contract,
            lease_policy=self.lease_policy,
            role_profile_id=self.profile_id,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "backend": self.backend,
            "execution_contract": self.execution_contract.to_dict(),
            "lease_policy": self.lease_policy.to_dict(),
            "keep_session_idle": self.keep_session_idle,
            "reactivate_idle_session": self.reactivate_idle_session,
            "fallback_idle_timeout_seconds": self.fallback_idle_timeout_seconds,
            "fallback_hard_timeout_seconds": self.fallback_hard_timeout_seconds,
            "fallback_max_attempts": self.fallback_max_attempts,
            "fallback_attempt_timeout_seconds": self.fallback_attempt_timeout_seconds,
            "fallback_resume_on_timeout": self.fallback_resume_on_timeout,
            "fallback_resume_on_failure": self.fallback_resume_on_failure,
            "fallback_allow_relaunch": self.fallback_allow_relaunch,
            "fallback_escalate_after_attempts": self.fallback_escalate_after_attempts,
            "fallback_backoff_seconds": self.fallback_backoff_seconds,
            "fallback_provider_unavailable_backoff_initial_seconds": (
                self.fallback_provider_unavailable_backoff_initial_seconds
            ),
            "fallback_provider_unavailable_backoff_multiplier": (
                self.fallback_provider_unavailable_backoff_multiplier
            ),
            "fallback_provider_unavailable_backoff_max_seconds": (
                self.fallback_provider_unavailable_backoff_max_seconds
            ),
            "metadata": dict(self.metadata),
        }
