from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from agent_orchestra.contracts.ids import (
    make_conversation_head_id,
    make_resident_team_shell_id,
    make_runtime_generation_id,
    make_session_event_id,
    make_work_session_id,
    make_work_session_message_id,
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


def _coerce_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


class RuntimeGenerationStatus(str, Enum):
    BOOTING = "booting"
    ACTIVE = "active"
    QUIESCENT = "quiescent"
    DETACHED = "detached"
    FAILED = "failed"
    CLOSED = "closed"


class RuntimeGenerationContinuityMode(str, Enum):
    FRESH = "fresh"
    WARM_RESUME = "warm_resume"
    FORK_SEED = "fork_seed"


class ConversationHeadKind(str, Enum):
    SUPERLEADER = "superleader"
    LEADER_LANE = "leader_lane"
    TEAMMATE_SLOT = "teammate_slot"
    WORKER = "worker"


class ResumeGateMode(str, Enum):
    EXACT_WAKE = "exact_wake"
    WARM_RESUME = "warm_resume"
    INSPECT_ONLY = "inspect_only"
    REJECT = "reject"


class ResidentTeamShellStatus(str, Enum):
    BOOTING = "booting"
    ATTACHED = "attached"
    IDLE = "idle"
    WAITING_FOR_MAILBOX = "waiting_for_mailbox"
    WAITING_FOR_SUBORDINATES = "waiting_for_subordinates"
    QUIESCENT = "quiescent"
    RECOVERING = "recovering"
    FAILED = "failed"
    CLOSED = "closed"


class ShellAttachDecisionMode(str, Enum):
    ATTACHED = "attached"
    WOKEN = "woken"
    RECOVERED = "recovered"
    WARM_RESUMED = "warm_resumed"
    REJECTED = "rejected"


def _coerce_runtime_generation_status(value: object) -> RuntimeGenerationStatus:
    if isinstance(value, RuntimeGenerationStatus):
        return value
    try:
        return RuntimeGenerationStatus(str(value))
    except ValueError:
        return RuntimeGenerationStatus.BOOTING


def _coerce_runtime_generation_mode(value: object) -> RuntimeGenerationContinuityMode:
    if isinstance(value, RuntimeGenerationContinuityMode):
        return value
    try:
        return RuntimeGenerationContinuityMode(str(value))
    except ValueError:
        return RuntimeGenerationContinuityMode.FRESH


def _coerce_conversation_head_kind(value: object) -> ConversationHeadKind:
    if isinstance(value, ConversationHeadKind):
        return value
    try:
        return ConversationHeadKind(str(value))
    except ValueError:
        return ConversationHeadKind.WORKER


def _coerce_resume_gate_mode(value: object) -> ResumeGateMode:
    if isinstance(value, ResumeGateMode):
        return value
    try:
        return ResumeGateMode(str(value))
    except ValueError:
        return ResumeGateMode.REJECT


def _coerce_resident_team_shell_status(value: object) -> ResidentTeamShellStatus:
    if isinstance(value, ResidentTeamShellStatus):
        return value
    try:
        return ResidentTeamShellStatus(str(value))
    except ValueError:
        return ResidentTeamShellStatus.BOOTING


def _coerce_shell_attach_decision_mode(value: object) -> ShellAttachDecisionMode:
    if isinstance(value, ShellAttachDecisionMode):
        return value
    try:
        return ShellAttachDecisionMode(str(value))
    except ValueError:
        return ShellAttachDecisionMode.REJECTED


@dataclass(slots=True)
class WorkSession:
    work_session_id: str = field(default_factory=make_work_session_id)
    group_id: str = ""
    root_objective_id: str = ""
    title: str = ""
    status: str = "open"
    created_at: str = ""
    updated_at: str = ""
    current_runtime_generation_id: str | None = None
    parent_work_session_id: str | None = None
    fork_origin_work_session_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "work_session_id": self.work_session_id,
            "group_id": self.group_id,
            "root_objective_id": self.root_objective_id,
            "title": self.title,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "current_runtime_generation_id": self.current_runtime_generation_id,
            "parent_work_session_id": self.parent_work_session_id,
            "fork_origin_work_session_id": self.fork_origin_work_session_id,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "WorkSession":
        return cls(
            work_session_id=str(payload.get("work_session_id") or make_work_session_id()),
            group_id=str(payload.get("group_id", "")).strip(),
            root_objective_id=str(payload.get("root_objective_id", "")).strip(),
            title=str(payload.get("title", "")).strip(),
            status=str(payload.get("status", "open")).strip() or "open",
            created_at=str(payload.get("created_at", "")).strip(),
            updated_at=str(payload.get("updated_at", "")).strip(),
            current_runtime_generation_id=_optional_string(
                payload.get("current_runtime_generation_id")
            ),
            parent_work_session_id=_optional_string(payload.get("parent_work_session_id")),
            fork_origin_work_session_id=_optional_string(payload.get("fork_origin_work_session_id")),
            metadata=_mapping(payload.get("metadata")),
        )


@dataclass(slots=True)
class RuntimeGeneration:
    runtime_generation_id: str = field(default_factory=make_runtime_generation_id)
    work_session_id: str = ""
    generation_index: int = 0
    status: RuntimeGenerationStatus = RuntimeGenerationStatus.BOOTING
    continuity_mode: RuntimeGenerationContinuityMode = RuntimeGenerationContinuityMode.FRESH
    created_at: str = ""
    closed_at: str | None = None
    source_runtime_generation_id: str | None = None
    group_id: str = ""
    objective_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "runtime_generation_id": self.runtime_generation_id,
            "work_session_id": self.work_session_id,
            "generation_index": self.generation_index,
            "status": self.status.value,
            "continuity_mode": self.continuity_mode.value,
            "created_at": self.created_at,
            "closed_at": self.closed_at,
            "source_runtime_generation_id": self.source_runtime_generation_id,
            "group_id": self.group_id,
            "objective_id": self.objective_id,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "RuntimeGeneration":
        return cls(
            runtime_generation_id=str(payload.get("runtime_generation_id") or make_runtime_generation_id()),
            work_session_id=str(payload.get("work_session_id", "")).strip(),
            generation_index=_coerce_int(payload.get("generation_index"), default=0),
            status=_coerce_runtime_generation_status(payload.get("status")),
            continuity_mode=_coerce_runtime_generation_mode(payload.get("continuity_mode")),
            created_at=str(payload.get("created_at", "")).strip(),
            closed_at=_optional_string(payload.get("closed_at")),
            source_runtime_generation_id=_optional_string(payload.get("source_runtime_generation_id")),
            group_id=str(payload.get("group_id", "")).strip(),
            objective_id=str(payload.get("objective_id", "")).strip(),
            metadata=_mapping(payload.get("metadata")),
        )


@dataclass(slots=True)
class WorkSessionMessage:
    message_id: str = field(default_factory=make_work_session_message_id)
    work_session_id: str = ""
    runtime_generation_id: str | None = None
    role: str = "user"
    scope_kind: str = "session"
    scope_id: str | None = None
    content: str = ""
    content_kind: str = "text"
    created_at: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "message_id": self.message_id,
            "work_session_id": self.work_session_id,
            "runtime_generation_id": self.runtime_generation_id,
            "role": self.role,
            "scope_kind": self.scope_kind,
            "scope_id": self.scope_id,
            "content": self.content,
            "content_kind": self.content_kind,
            "created_at": self.created_at,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "WorkSessionMessage":
        return cls(
            message_id=str(payload.get("message_id") or make_work_session_message_id()),
            work_session_id=str(payload.get("work_session_id", "")).strip(),
            runtime_generation_id=_optional_string(payload.get("runtime_generation_id")),
            role=str(payload.get("role", "user")).strip() or "user",
            scope_kind=str(payload.get("scope_kind", "session")).strip() or "session",
            scope_id=_optional_string(payload.get("scope_id")),
            content=str(payload.get("content", "")),
            content_kind=str(payload.get("content_kind", "text")).strip() or "text",
            created_at=str(payload.get("created_at", "")).strip(),
            metadata=_mapping(payload.get("metadata")),
        )


@dataclass(slots=True)
class ConversationHead:
    conversation_head_id: str = field(default_factory=make_conversation_head_id)
    work_session_id: str = ""
    runtime_generation_id: str = ""
    head_kind: ConversationHeadKind = ConversationHeadKind.WORKER
    scope_id: str | None = None
    backend: str = ""
    model: str = ""
    provider: str = ""
    last_response_id: str | None = None
    checkpoint_summary: str = ""
    checkpoint_metadata: dict[str, Any] = field(default_factory=dict)
    checkpoint_id: str | None = None
    prompt_contract_version: str | None = None
    toolset_hash: str | None = None
    contract_fingerprint: str | None = None
    source_agent_session_id: str | None = None
    source_worker_session_id: str | None = None
    updated_at: str = ""
    invalidated_at: str | None = None
    invalidation_reason: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "conversation_head_id": self.conversation_head_id,
            "work_session_id": self.work_session_id,
            "runtime_generation_id": self.runtime_generation_id,
            "head_kind": self.head_kind.value,
            "scope_id": self.scope_id,
            "backend": self.backend,
            "model": self.model,
            "provider": self.provider,
            "last_response_id": self.last_response_id,
            "checkpoint_summary": self.checkpoint_summary,
            "checkpoint_metadata": dict(self.checkpoint_metadata),
            "checkpoint_id": self.checkpoint_id,
            "prompt_contract_version": self.prompt_contract_version,
            "toolset_hash": self.toolset_hash,
            "contract_fingerprint": self.contract_fingerprint,
            "source_agent_session_id": self.source_agent_session_id,
            "source_worker_session_id": self.source_worker_session_id,
            "updated_at": self.updated_at,
            "invalidated_at": self.invalidated_at,
            "invalidation_reason": self.invalidation_reason,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "ConversationHead":
        return cls(
            conversation_head_id=str(payload.get("conversation_head_id") or make_conversation_head_id()),
            work_session_id=str(payload.get("work_session_id", "")).strip(),
            runtime_generation_id=str(payload.get("runtime_generation_id", "")).strip(),
            head_kind=_coerce_conversation_head_kind(payload.get("head_kind")),
            scope_id=_optional_string(payload.get("scope_id")),
            backend=str(payload.get("backend", "")).strip(),
            model=str(payload.get("model", "")).strip(),
            provider=str(payload.get("provider", "")).strip(),
            last_response_id=_optional_string(payload.get("last_response_id")),
            checkpoint_summary=str(payload.get("checkpoint_summary", "")),
            checkpoint_metadata=_mapping(payload.get("checkpoint_metadata")),
            checkpoint_id=_optional_string(payload.get("checkpoint_id")),
            prompt_contract_version=_optional_string(payload.get("prompt_contract_version")),
            toolset_hash=_optional_string(payload.get("toolset_hash")),
            contract_fingerprint=_optional_string(payload.get("contract_fingerprint")),
            source_agent_session_id=_optional_string(payload.get("source_agent_session_id")),
            source_worker_session_id=_optional_string(payload.get("source_worker_session_id")),
            updated_at=str(payload.get("updated_at", "")).strip(),
            invalidated_at=_optional_string(payload.get("invalidated_at")),
            invalidation_reason=_optional_string(payload.get("invalidation_reason")),
        )


@dataclass(slots=True)
class SessionEvent:
    session_event_id: str = field(default_factory=make_session_event_id)
    work_session_id: str = ""
    runtime_generation_id: str | None = None
    event_kind: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "session_event_id": self.session_event_id,
            "work_session_id": self.work_session_id,
            "runtime_generation_id": self.runtime_generation_id,
            "event_kind": self.event_kind,
            "payload": dict(self.payload),
            "created_at": self.created_at,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "SessionEvent":
        return cls(
            session_event_id=str(payload.get("session_event_id") or make_session_event_id()),
            work_session_id=str(payload.get("work_session_id", "")).strip(),
            runtime_generation_id=_optional_string(payload.get("runtime_generation_id")),
            event_kind=str(payload.get("event_kind", "")).strip(),
            payload=_mapping(payload.get("payload")),
            created_at=str(payload.get("created_at", "")).strip(),
        )


@dataclass(slots=True)
class ResumeGateDecision:
    mode: ResumeGateMode = ResumeGateMode.REJECT
    reason: str = ""
    target_work_session_id: str | None = None
    target_runtime_generation_id: str | None = None
    requires_user_confirmation: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "mode": self.mode.value,
            "reason": self.reason,
            "target_work_session_id": self.target_work_session_id,
            "target_runtime_generation_id": self.target_runtime_generation_id,
            "requires_user_confirmation": self.requires_user_confirmation,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "ResumeGateDecision":
        return cls(
            mode=_coerce_resume_gate_mode(payload.get("mode")),
            reason=str(payload.get("reason", "")),
            target_work_session_id=_optional_string(payload.get("target_work_session_id")),
            target_runtime_generation_id=_optional_string(payload.get("target_runtime_generation_id")),
            requires_user_confirmation=_coerce_bool(payload.get("requires_user_confirmation")),
            metadata=_mapping(payload.get("metadata")),
        )


@dataclass(slots=True)
class ContinuationBundle:
    work_session_id: str = ""
    runtime_generation_id: str = ""
    head_kind: ConversationHeadKind = ConversationHeadKind.WORKER
    scope_id: str | None = None
    checkpoint_summary: str = ""
    last_response_id: str | None = None
    runtime_status_summary: dict[str, Any] = field(default_factory=dict)
    task_surface_authority: dict[str, Any] = field(default_factory=dict)
    delivery_state_summary: dict[str, Any] = field(default_factory=dict)
    mailbox_summary: dict[str, Any] = field(default_factory=dict)
    blackboard_summary: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "work_session_id": self.work_session_id,
            "runtime_generation_id": self.runtime_generation_id,
            "head_kind": self.head_kind.value,
            "scope_id": self.scope_id,
            "checkpoint_summary": self.checkpoint_summary,
            "last_response_id": self.last_response_id,
            "runtime_status_summary": dict(self.runtime_status_summary),
            "task_surface_authority": dict(self.task_surface_authority),
            "delivery_state_summary": dict(self.delivery_state_summary),
            "mailbox_summary": dict(self.mailbox_summary),
            "blackboard_summary": dict(self.blackboard_summary),
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "ContinuationBundle":
        return cls(
            work_session_id=str(payload.get("work_session_id", "")).strip(),
            runtime_generation_id=str(payload.get("runtime_generation_id", "")).strip(),
            head_kind=_coerce_conversation_head_kind(payload.get("head_kind")),
            scope_id=_optional_string(payload.get("scope_id")),
            checkpoint_summary=str(payload.get("checkpoint_summary", "")),
            last_response_id=_optional_string(payload.get("last_response_id")),
            runtime_status_summary=_mapping(payload.get("runtime_status_summary")),
            task_surface_authority=_mapping(payload.get("task_surface_authority")),
            delivery_state_summary=_mapping(payload.get("delivery_state_summary")),
            mailbox_summary=_mapping(payload.get("mailbox_summary")),
            blackboard_summary=_mapping(payload.get("blackboard_summary")),
            metadata=_mapping(payload.get("metadata")),
        )


def _list_of_strings(value: object) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [str(item) for item in value if item is not None]


@dataclass(slots=True)
class ResidentTeamShell:
    resident_team_shell_id: str = field(default_factory=make_resident_team_shell_id)
    work_session_id: str = ""
    group_id: str = ""
    objective_id: str = ""
    team_id: str = ""
    lane_id: str = ""
    runtime_generation_id: str = ""
    status: ResidentTeamShellStatus = ResidentTeamShellStatus.BOOTING
    leader_slot_session_id: str | None = None
    teammate_slot_session_ids: list[str] = field(default_factory=list)
    attach_state: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""
    last_progress_at: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "resident_team_shell_id": self.resident_team_shell_id,
            "work_session_id": self.work_session_id,
            "group_id": self.group_id,
            "objective_id": self.objective_id,
            "team_id": self.team_id,
            "lane_id": self.lane_id,
            "runtime_generation_id": self.runtime_generation_id,
            "status": self.status.value,
            "leader_slot_session_id": self.leader_slot_session_id,
            "teammate_slot_session_ids": list(self.teammate_slot_session_ids),
            "attach_state": dict(self.attach_state),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_progress_at": self.last_progress_at,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "ResidentTeamShell":
        return cls(
            resident_team_shell_id=str(
                payload.get("resident_team_shell_id") or make_resident_team_shell_id()
            ),
            work_session_id=str(payload.get("work_session_id", "")).strip(),
            group_id=str(payload.get("group_id", "")).strip(),
            objective_id=str(payload.get("objective_id", "")).strip(),
            team_id=str(payload.get("team_id", "")).strip(),
            lane_id=str(payload.get("lane_id", "")).strip(),
            runtime_generation_id=str(payload.get("runtime_generation_id", "")).strip(),
            status=_coerce_resident_team_shell_status(payload.get("status")),
            leader_slot_session_id=_optional_string(
                payload.get("leader_slot_session_id")
            ),
            teammate_slot_session_ids=_list_of_strings(
                payload.get("teammate_slot_session_ids")
            ),
            attach_state=_mapping(payload.get("attach_state")),
            created_at=str(payload.get("created_at", "")).strip(),
            updated_at=str(payload.get("updated_at", "")).strip(),
            last_progress_at=str(payload.get("last_progress_at", "")).strip(),
            metadata=_mapping(payload.get("metadata")),
        )


@dataclass(slots=True)
class ShellAttachDecision:
    mode: ShellAttachDecisionMode = ShellAttachDecisionMode.REJECTED
    reason: str = ""
    target_shell_id: str | None = None
    target_work_session_id: str | None = None
    target_runtime_generation_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "mode": self.mode.value,
            "reason": self.reason,
            "target_shell_id": self.target_shell_id,
            "target_work_session_id": self.target_work_session_id,
            "target_runtime_generation_id": self.target_runtime_generation_id,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "ShellAttachDecision":
        return cls(
            mode=_coerce_shell_attach_decision_mode(payload.get("mode")),
            reason=str(payload.get("reason", "")),
            target_shell_id=_optional_string(payload.get("target_shell_id")),
            target_work_session_id=_optional_string(payload.get("target_work_session_id")),
            target_runtime_generation_id=_optional_string(
                payload.get("target_runtime_generation_id")
            ),
            metadata=_mapping(payload.get("metadata")),
        )
