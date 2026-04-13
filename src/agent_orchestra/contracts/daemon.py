from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from agent_orchestra.contracts.ids import (
    make_agent_incarnation_id,
    make_agent_slot_id,
    make_id,
    make_session_attachment_id,
    make_slot_health_event_id,
)


def _mapping(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): item for key, item in value.items()}


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _coerce_int(value: object, *, default: int = 0) -> int:
    try:
        return int(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def _coerce_float(value: object, *, default: float = 0.0) -> float:
    try:
        return float(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def _coerce_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


class AgentSlotStatus(str, Enum):
    BOOTING = "booting"
    ACTIVE = "active"
    IDLE = "idle"
    DEGRADED = "degraded"
    WAITING_PROVIDER = "waiting_provider"
    QUIESCENT = "quiescent"
    FAILED = "failed"
    CLOSED = "closed"


class AgentIncarnationStatus(str, Enum):
    BOOTING = "booting"
    PENDING_RESTART = "pending_restart"
    ACTIVE = "active"
    QUIESCENT = "quiescent"
    TERMINAL = "terminal"
    FAILED = "failed"
    FENCED = "fenced"


class SlotFailureClass(str, Enum):
    NORMAL_TERMINAL = "normal_terminal"
    RECOVERABLE_ABNORMAL = "recoverable_abnormal"
    EXTERNAL_DEGRADED = "external_degraded"
    FATAL_CONFIGURATION = "fatal_configuration"


class SessionAttachmentStatus(str, Enum):
    ATTACHING = "attaching"
    ATTACHED = "attached"
    STREAMING = "streaming"
    DETACHED = "detached"
    CLOSED = "closed"
    REJECTED = "rejected"


class ProviderRouteStatus(str, Enum):
    HEALTHY = "healthy"
    WAITING_PROVIDER = "waiting_provider"
    DEGRADED_PROVIDER = "degraded_provider"
    QUARANTINED = "quarantined"


def _coerce_agent_slot_status(value: object) -> AgentSlotStatus:
    if isinstance(value, AgentSlotStatus):
        return value
    try:
        return AgentSlotStatus(str(value))
    except ValueError:
        return AgentSlotStatus.BOOTING


def _coerce_agent_incarnation_status(value: object) -> AgentIncarnationStatus:
    if isinstance(value, AgentIncarnationStatus):
        return value
    try:
        return AgentIncarnationStatus(str(value))
    except ValueError:
        return AgentIncarnationStatus.BOOTING


def _coerce_slot_failure_class(value: object) -> SlotFailureClass | None:
    if value is None:
        return None
    if isinstance(value, SlotFailureClass):
        return value
    try:
        return SlotFailureClass(str(value))
    except ValueError:
        return None


def _coerce_session_attachment_status(value: object) -> SessionAttachmentStatus:
    if isinstance(value, SessionAttachmentStatus):
        return value
    try:
        return SessionAttachmentStatus(str(value))
    except ValueError:
        return SessionAttachmentStatus.ATTACHING


def _coerce_provider_route_status(value: object) -> ProviderRouteStatus:
    if isinstance(value, ProviderRouteStatus):
        return value
    try:
        return ProviderRouteStatus(str(value))
    except ValueError:
        return ProviderRouteStatus.HEALTHY


def make_daemon_command_id() -> str:
    return make_id("daemoncmd")


def make_daemon_event_id() -> str:
    return make_id("daemonevt")


@dataclass(slots=True)
class AgentSlot:
    slot_id: str = field(default_factory=make_agent_slot_id)
    role: str = ""
    work_session_id: str = ""
    resident_team_shell_id: str | None = None
    status: AgentSlotStatus = AgentSlotStatus.BOOTING
    desired_state: str = "active"
    preferred_backend: str | None = None
    preferred_transport_class: str | None = None
    current_incarnation_id: str | None = None
    current_lease_id: str | None = None
    restart_count: int = 0
    last_failure_class: SlotFailureClass | None = None
    last_failure_reason: str | None = None
    created_at: str = ""
    updated_at: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "slot_id": self.slot_id,
            "role": self.role,
            "work_session_id": self.work_session_id,
            "resident_team_shell_id": self.resident_team_shell_id,
            "status": self.status.value,
            "desired_state": self.desired_state,
            "preferred_backend": self.preferred_backend,
            "preferred_transport_class": self.preferred_transport_class,
            "current_incarnation_id": self.current_incarnation_id,
            "current_lease_id": self.current_lease_id,
            "restart_count": self.restart_count,
            "last_failure_class": (
                self.last_failure_class.value if self.last_failure_class is not None else None
            ),
            "last_failure_reason": self.last_failure_reason,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "AgentSlot":
        return cls(
            slot_id=str(payload.get("slot_id") or make_agent_slot_id()),
            role=str(payload.get("role", "")).strip(),
            work_session_id=str(payload.get("work_session_id", "")).strip(),
            resident_team_shell_id=_optional_string(payload.get("resident_team_shell_id")),
            status=_coerce_agent_slot_status(payload.get("status")),
            desired_state=str(payload.get("desired_state", "active")).strip() or "active",
            preferred_backend=_optional_string(payload.get("preferred_backend")),
            preferred_transport_class=_optional_string(payload.get("preferred_transport_class")),
            current_incarnation_id=_optional_string(payload.get("current_incarnation_id")),
            current_lease_id=_optional_string(payload.get("current_lease_id")),
            restart_count=_coerce_int(payload.get("restart_count"), default=0),
            last_failure_class=_coerce_slot_failure_class(payload.get("last_failure_class")),
            last_failure_reason=_optional_string(payload.get("last_failure_reason")),
            created_at=str(payload.get("created_at", "")).strip(),
            updated_at=str(payload.get("updated_at", "")).strip(),
            metadata=_mapping(payload.get("metadata")),
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "AgentSlot":
        return cls.from_payload(payload)


@dataclass(slots=True)
class AgentIncarnation:
    incarnation_id: str = field(default_factory=make_agent_incarnation_id)
    slot_id: str = ""
    work_session_id: str = ""
    runtime_generation_id: str | None = None
    status: AgentIncarnationStatus = AgentIncarnationStatus.BOOTING
    backend: str = ""
    transport_locator: dict[str, Any] = field(default_factory=dict)
    lease_id: str = ""
    restart_generation: int = 0
    started_at: str = ""
    ended_at: str | None = None
    terminal_failure_class: SlotFailureClass | None = None
    terminal_reason: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "incarnation_id": self.incarnation_id,
            "slot_id": self.slot_id,
            "work_session_id": self.work_session_id,
            "runtime_generation_id": self.runtime_generation_id,
            "status": self.status.value,
            "backend": self.backend,
            "transport_locator": dict(self.transport_locator),
            "lease_id": self.lease_id,
            "restart_generation": self.restart_generation,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "terminal_failure_class": (
                self.terminal_failure_class.value
                if self.terminal_failure_class is not None
                else None
            ),
            "terminal_reason": self.terminal_reason,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "AgentIncarnation":
        return cls(
            incarnation_id=str(payload.get("incarnation_id") or make_agent_incarnation_id()),
            slot_id=str(payload.get("slot_id", "")).strip(),
            work_session_id=(
                str(payload.get("work_session_id", "")).strip()
                or str(payload.get("worker_session_id", "")).strip()
            ),
            runtime_generation_id=_optional_string(payload.get("runtime_generation_id")),
            status=_coerce_agent_incarnation_status(payload.get("status")),
            backend=str(payload.get("backend", "")).strip(),
            transport_locator=_mapping(payload.get("transport_locator")),
            lease_id=str(payload.get("lease_id", "")).strip(),
            restart_generation=_coerce_int(payload.get("restart_generation"), default=0),
            started_at=str(payload.get("started_at", "")).strip(),
            ended_at=_optional_string(payload.get("ended_at")),
            terminal_failure_class=_coerce_slot_failure_class(
                payload.get("terminal_failure_class", payload.get("failure_class"))
            ),
            terminal_reason=_optional_string(
                payload.get("terminal_reason", payload.get("terminal_status"))
            ),
            metadata=_mapping(payload.get("metadata")),
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "AgentIncarnation":
        return cls.from_payload(payload)


@dataclass(slots=True)
class SlotHealthEvent:
    event_id: str = field(default_factory=make_slot_health_event_id)
    slot_id: str = ""
    incarnation_id: str | None = None
    work_session_id: str = ""
    event_kind: str = ""
    failure_class: SlotFailureClass | None = None
    observed_at: str = ""
    detail: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "event_id": self.event_id,
            "slot_id": self.slot_id,
            "incarnation_id": self.incarnation_id,
            "work_session_id": self.work_session_id,
            "event_kind": self.event_kind,
            "failure_class": self.failure_class.value if self.failure_class is not None else None,
            "observed_at": self.observed_at,
            "detail": self.detail,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "SlotHealthEvent":
        return cls(
            event_id=str(payload.get("event_id") or make_slot_health_event_id()),
            slot_id=str(payload.get("slot_id", "")).strip(),
            incarnation_id=_optional_string(payload.get("incarnation_id")),
            work_session_id=str(payload.get("work_session_id", "")).strip(),
            event_kind=str(payload.get("event_kind", "")).strip(),
            failure_class=_coerce_slot_failure_class(
                payload.get("failure_class", payload.get("status"))
            ),
            observed_at=str(payload.get("observed_at", payload.get("recorded_at", ""))).strip(),
            detail=str(payload.get("detail", "")),
            metadata=_mapping(payload.get("metadata")),
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "SlotHealthEvent":
        return cls.from_payload(payload)


@dataclass(slots=True)
class SessionAttachment:
    attachment_id: str = field(default_factory=make_session_attachment_id)
    work_session_id: str = ""
    resident_team_shell_id: str | None = None
    slot_id: str | None = None
    incarnation_id: str | None = None
    client_id: str = ""
    status: SessionAttachmentStatus = SessionAttachmentStatus.ATTACHING
    attached_at: str = ""
    detached_at: str | None = None
    last_event_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "attachment_id": self.attachment_id,
            "work_session_id": self.work_session_id,
            "resident_team_shell_id": self.resident_team_shell_id,
            "slot_id": self.slot_id,
            "incarnation_id": self.incarnation_id,
            "client_id": self.client_id,
            "status": self.status.value,
            "attached_at": self.attached_at,
            "detached_at": self.detached_at,
            "last_event_id": self.last_event_id,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "SessionAttachment":
        return cls(
            attachment_id=str(payload.get("attachment_id") or make_session_attachment_id()),
            work_session_id=str(payload.get("work_session_id", "")).strip(),
            resident_team_shell_id=_optional_string(payload.get("resident_team_shell_id")),
            slot_id=_optional_string(payload.get("slot_id")),
            incarnation_id=_optional_string(payload.get("incarnation_id")),
            client_id=str(payload.get("client_id", "")).strip(),
            status=_coerce_session_attachment_status(payload.get("status")),
            attached_at=str(payload.get("attached_at", payload.get("created_at", ""))).strip(),
            detached_at=_optional_string(payload.get("detached_at", payload.get("updated_at"))),
            last_event_id=_optional_string(payload.get("last_event_id")),
            metadata=_mapping(payload.get("metadata")),
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "SessionAttachment":
        return cls.from_payload(payload)


@dataclass(slots=True)
class ProviderRouteHealth:
    route_key: str
    role: str = ""
    backend: str = ""
    route_fingerprint: str = ""
    status: ProviderRouteStatus = ProviderRouteStatus.HEALTHY
    health_score: float = 0.0
    consecutive_failures: int = 0
    last_failure_class: SlotFailureClass | None = None
    cooldown_expires_at: str | None = None
    preferred: bool = False
    updated_at: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "route_key": self.route_key,
            "role": self.role,
            "backend": self.backend,
            "route_fingerprint": self.route_fingerprint,
            "status": self.status.value,
            "health_score": self.health_score,
            "consecutive_failures": self.consecutive_failures,
            "last_failure_class": (
                self.last_failure_class.value if self.last_failure_class is not None else None
            ),
            "cooldown_expires_at": self.cooldown_expires_at,
            "preferred": self.preferred,
            "updated_at": self.updated_at,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "ProviderRouteHealth":
        route_key = str(payload.get("route_key", "")).strip()
        if not route_key:
            legacy_route_id = str(payload.get("route_id", "")).strip()
            legacy_role = str(payload.get("role", "")).strip()
            route_key = f"{legacy_route_id}:{legacy_role}" if legacy_role else legacy_route_id
        return cls(
            route_key=route_key or make_id("route"),
            role=str(payload.get("role", "")).strip(),
            backend=str(payload.get("backend", "")).strip(),
            route_fingerprint=str(payload.get("route_fingerprint", "")).strip(),
            status=_coerce_provider_route_status(payload.get("status")),
            health_score=_coerce_float(payload.get("health_score"), default=0.0),
            consecutive_failures=_coerce_int(
                payload.get("consecutive_failures", payload.get("consecutive_failure_count")),
                default=0,
            ),
            last_failure_class=_coerce_slot_failure_class(payload.get("last_failure_class")),
            cooldown_expires_at=_optional_string(
                payload.get("cooldown_expires_at", payload.get("cooldown_until"))
            ),
            preferred=_coerce_bool(payload.get("preferred")),
            updated_at=str(
                payload.get("updated_at", payload.get("last_failure_at", ""))
            ).strip(),
            metadata=_mapping(payload.get("metadata")),
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ProviderRouteHealth":
        return cls.from_payload(payload)


@dataclass(slots=True)
class DaemonCommandEnvelope:
    command_id: str = field(default_factory=make_daemon_command_id)
    command: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "command_id": self.command_id,
            "command": self.command,
            "payload": dict(self.payload),
            "created_at": self.created_at,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "DaemonCommandEnvelope":
        return cls(
            command_id=str(payload.get("command_id") or make_daemon_command_id()),
            command=str(payload.get("command", "")).strip(),
            payload=_mapping(payload.get("payload")),
            created_at=str(payload.get("created_at", "")).strip(),
            metadata=_mapping(payload.get("metadata")),
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "DaemonCommandEnvelope":
        return cls.from_payload(payload)


@dataclass(slots=True)
class DaemonEventEnvelope:
    event_id: str = field(default_factory=make_daemon_event_id)
    event_kind: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "event_id": self.event_id,
            "event_kind": self.event_kind,
            "payload": dict(self.payload),
            "created_at": self.created_at,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "DaemonEventEnvelope":
        return cls(
            event_id=str(payload.get("event_id") or make_daemon_event_id()),
            event_kind=str(payload.get("event_kind", "")).strip(),
            payload=_mapping(payload.get("payload")),
            created_at=str(payload.get("created_at", "")).strip(),
            metadata=_mapping(payload.get("metadata")),
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "DaemonEventEnvelope":
        return cls.from_payload(payload)
