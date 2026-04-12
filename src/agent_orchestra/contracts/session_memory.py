from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from agent_orchestra.contracts.ids import (
    make_agent_turn_record_id,
    make_artifact_ref_id,
    make_memory_item_id,
    make_tool_invocation_id,
)
from agent_orchestra.contracts.session_continuity import (
    ConversationHeadKind,
    _coerce_conversation_head_kind,
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


def _string_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(item).strip() for item in value if str(item).strip())


def _coerce_enum(value: object, enum_type: type[Enum], default: Enum) -> Enum:
    if isinstance(value, enum_type):
        return value
    try:
        return enum_type(str(value))
    except ValueError:
        return default


class AgentTurnActorRole(str, Enum):
    USER = "user"
    SUPERLEADER = "superleader"
    LEADER = "leader"
    TEAMMATE = "teammate"
    WORKER = "worker"
    SYSTEM = "system"


class AgentTurnKind(str, Enum):
    PROMPT_INPUT = "prompt_input"
    WORKER_RESULT = "worker_result"
    LEADER_DECISION = "leader_decision"
    SUPERLEADER_DECISION = "superleader_decision"
    MAILBOX_FOLLOWUP = "mailbox_followup"
    AUTHORITY_TRANSITION = "authority_transition"
    RESUME_SEED = "resume_seed"


class AgentTurnStatus(str, Enum):
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    PARTIAL = "partial"


class ToolInvocationKind(str, Enum):
    PROTOCOL_TOOL = "protocol_tool"
    LOCAL_COMMAND = "local_command"
    MAILBOX_COMMIT = "mailbox_commit"
    AUTHORITY_ACTION = "authority_action"
    ARTIFACT_EMIT = "artifact_emit"


class ArtifactRefKind(str, Enum):
    FINAL_REPORT = "final_report"
    PROTOCOL_STATE = "protocol_state"
    PROTOCOL_EVENTS = "protocol_events"
    FILE_PATCH = "file_patch"
    GENERATED_FILE = "generated_file"
    DELIVERY_SNAPSHOT = "delivery_snapshot"
    MAILBOX_SNAPSHOT = "mailbox_snapshot"
    BLACKBOARD_SNAPSHOT = "blackboard_snapshot"
    TASK_SURFACE_SNAPSHOT = "task_surface_snapshot"
    HYDRATION_INPUT = "hydration_input"


class ArtifactStorageKind(str, Enum):
    INLINE_JSON = "inline_json"
    REPO_PATH = "repo_path"
    EXTERNAL_REF = "external_ref"


class SessionMemoryKind(str, Enum):
    FACT = "fact"
    DECISION = "decision"
    CONSTRAINT = "constraint"
    OPEN_LOOP = "open_loop"
    HANDOFF = "handoff"
    ARTIFACT_SUMMARY = "artifact_summary"


@dataclass(slots=True)
class AgentTurnRecord:
    turn_record_id: str = field(default_factory=make_agent_turn_record_id)
    work_session_id: str = ""
    runtime_generation_id: str = ""
    head_kind: ConversationHeadKind = ConversationHeadKind.WORKER
    scope_id: str | None = None
    actor_role: AgentTurnActorRole = AgentTurnActorRole.WORKER
    source_agent_session_id: str | None = None
    source_worker_session_id: str | None = None
    assignment_id: str | None = None
    turn_kind: AgentTurnKind = AgentTurnKind.WORKER_RESULT
    input_summary: str = ""
    output_summary: str = ""
    response_id: str | None = None
    status: AgentTurnStatus = AgentTurnStatus.COMPLETED
    created_at: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "turn_record_id": self.turn_record_id,
            "work_session_id": self.work_session_id,
            "runtime_generation_id": self.runtime_generation_id,
            "head_kind": self.head_kind.value,
            "scope_id": self.scope_id,
            "actor_role": self.actor_role.value,
            "source_agent_session_id": self.source_agent_session_id,
            "source_worker_session_id": self.source_worker_session_id,
            "assignment_id": self.assignment_id,
            "turn_kind": self.turn_kind.value,
            "input_summary": self.input_summary,
            "output_summary": self.output_summary,
            "response_id": self.response_id,
            "status": self.status.value,
            "created_at": self.created_at,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "AgentTurnRecord":
        return cls(
            turn_record_id=str(payload.get("turn_record_id") or make_agent_turn_record_id()),
            work_session_id=str(payload.get("work_session_id", "")).strip(),
            runtime_generation_id=str(payload.get("runtime_generation_id", "")).strip(),
            head_kind=_coerce_conversation_head_kind(payload.get("head_kind")),
            scope_id=_optional_string(payload.get("scope_id")),
            actor_role=_coerce_enum(
                payload.get("actor_role"),
                AgentTurnActorRole,
                AgentTurnActorRole.WORKER,
            ),
            source_agent_session_id=_optional_string(payload.get("source_agent_session_id")),
            source_worker_session_id=_optional_string(payload.get("source_worker_session_id")),
            assignment_id=_optional_string(payload.get("assignment_id")),
            turn_kind=_coerce_enum(
                payload.get("turn_kind"),
                AgentTurnKind,
                AgentTurnKind.WORKER_RESULT,
            ),
            input_summary=str(payload.get("input_summary", "")),
            output_summary=str(payload.get("output_summary", "")),
            response_id=_optional_string(payload.get("response_id")),
            status=_coerce_enum(
                payload.get("status"),
                AgentTurnStatus,
                AgentTurnStatus.COMPLETED,
            ),
            created_at=str(payload.get("created_at", "")).strip(),
            metadata=_mapping(payload.get("metadata")),
        )


@dataclass(slots=True)
class ToolInvocationRecord:
    tool_invocation_id: str = field(default_factory=make_tool_invocation_id)
    turn_record_id: str | None = None
    work_session_id: str = ""
    runtime_generation_id: str = ""
    tool_name: str = ""
    tool_kind: ToolInvocationKind = ToolInvocationKind.LOCAL_COMMAND
    input_summary: str = ""
    output_summary: str = ""
    status: str = ""
    started_at: str = ""
    completed_at: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "tool_invocation_id": self.tool_invocation_id,
            "turn_record_id": self.turn_record_id,
            "work_session_id": self.work_session_id,
            "runtime_generation_id": self.runtime_generation_id,
            "tool_name": self.tool_name,
            "tool_kind": self.tool_kind.value,
            "input_summary": self.input_summary,
            "output_summary": self.output_summary,
            "status": self.status,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "ToolInvocationRecord":
        return cls(
            tool_invocation_id=str(payload.get("tool_invocation_id") or make_tool_invocation_id()),
            turn_record_id=_optional_string(payload.get("turn_record_id")),
            work_session_id=str(payload.get("work_session_id", "")).strip(),
            runtime_generation_id=str(payload.get("runtime_generation_id", "")).strip(),
            tool_name=str(payload.get("tool_name", "")).strip(),
            tool_kind=_coerce_enum(
                payload.get("tool_kind"),
                ToolInvocationKind,
                ToolInvocationKind.LOCAL_COMMAND,
            ),
            input_summary=str(payload.get("input_summary", "")),
            output_summary=str(payload.get("output_summary", "")),
            status=str(payload.get("status", "")).strip(),
            started_at=str(payload.get("started_at", "")).strip(),
            completed_at=_optional_string(payload.get("completed_at")),
            metadata=_mapping(payload.get("metadata")),
        )


@dataclass(slots=True)
class ArtifactRef:
    artifact_ref_id: str = field(default_factory=make_artifact_ref_id)
    turn_record_id: str | None = None
    tool_invocation_id: str | None = None
    work_session_id: str = ""
    runtime_generation_id: str = ""
    artifact_kind: ArtifactRefKind = ArtifactRefKind.FINAL_REPORT
    storage_kind: ArtifactStorageKind = ArtifactStorageKind.INLINE_JSON
    uri_or_path: str = ""
    content_hash: str = ""
    size_bytes: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "artifact_ref_id": self.artifact_ref_id,
            "turn_record_id": self.turn_record_id,
            "tool_invocation_id": self.tool_invocation_id,
            "work_session_id": self.work_session_id,
            "runtime_generation_id": self.runtime_generation_id,
            "artifact_kind": self.artifact_kind.value,
            "storage_kind": self.storage_kind.value,
            "uri_or_path": self.uri_or_path,
            "content_hash": self.content_hash,
            "size_bytes": self.size_bytes,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "ArtifactRef":
        return cls(
            artifact_ref_id=str(payload.get("artifact_ref_id") or make_artifact_ref_id()),
            turn_record_id=_optional_string(payload.get("turn_record_id")),
            tool_invocation_id=_optional_string(payload.get("tool_invocation_id")),
            work_session_id=str(payload.get("work_session_id", "")).strip(),
            runtime_generation_id=str(payload.get("runtime_generation_id", "")).strip(),
            artifact_kind=_coerce_enum(
                payload.get("artifact_kind"),
                ArtifactRefKind,
                ArtifactRefKind.FINAL_REPORT,
            ),
            storage_kind=_coerce_enum(
                payload.get("storage_kind"),
                ArtifactStorageKind,
                ArtifactStorageKind.INLINE_JSON,
            ),
            uri_or_path=str(payload.get("uri_or_path", "")).strip(),
            content_hash=str(payload.get("content_hash", "")).strip(),
            size_bytes=_coerce_int(payload.get("size_bytes"), default=0),
            metadata=_mapping(payload.get("metadata")),
        )


@dataclass(slots=True)
class SessionMemoryItem:
    memory_item_id: str = field(default_factory=make_memory_item_id)
    work_session_id: str = ""
    runtime_generation_id: str = ""
    head_kind: ConversationHeadKind = ConversationHeadKind.WORKER
    scope_id: str | None = None
    memory_kind: SessionMemoryKind = SessionMemoryKind.HANDOFF
    importance: int = 0
    summary: str = ""
    source_turn_record_ids: tuple[str, ...] = ()
    source_artifact_ref_ids: tuple[str, ...] = ()
    supersedes_memory_item_id: str | None = None
    created_at: str = ""
    archived_at: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "memory_item_id": self.memory_item_id,
            "work_session_id": self.work_session_id,
            "runtime_generation_id": self.runtime_generation_id,
            "head_kind": self.head_kind.value,
            "scope_id": self.scope_id,
            "memory_kind": self.memory_kind.value,
            "importance": self.importance,
            "summary": self.summary,
            "source_turn_record_ids": list(self.source_turn_record_ids),
            "source_artifact_ref_ids": list(self.source_artifact_ref_ids),
            "supersedes_memory_item_id": self.supersedes_memory_item_id,
            "created_at": self.created_at,
            "archived_at": self.archived_at,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "SessionMemoryItem":
        return cls(
            memory_item_id=str(payload.get("memory_item_id") or make_memory_item_id()),
            work_session_id=str(payload.get("work_session_id", "")).strip(),
            runtime_generation_id=str(payload.get("runtime_generation_id", "")).strip(),
            head_kind=_coerce_conversation_head_kind(payload.get("head_kind")),
            scope_id=_optional_string(payload.get("scope_id")),
            memory_kind=_coerce_enum(
                payload.get("memory_kind"),
                SessionMemoryKind,
                SessionMemoryKind.HANDOFF,
            ),
            importance=_coerce_int(payload.get("importance"), default=0),
            summary=str(payload.get("summary", "")),
            source_turn_record_ids=_string_tuple(payload.get("source_turn_record_ids")),
            source_artifact_ref_ids=_string_tuple(payload.get("source_artifact_ref_ids")),
            supersedes_memory_item_id=_optional_string(payload.get("supersedes_memory_item_id")),
            created_at=str(payload.get("created_at", "")).strip(),
            archived_at=_optional_string(payload.get("archived_at")),
            metadata=_mapping(payload.get("metadata")),
        )


@dataclass(slots=True)
class HydrationBundle:
    work_session_id: str = ""
    runtime_generation_id: str = ""
    head_kind: ConversationHeadKind = ConversationHeadKind.WORKER
    scope_id: str | None = None
    conversation_head_id: str | None = None
    continuation_mode: str = ""
    last_response_id: str | None = None
    checkpoint_summary: str = ""
    recent_turns: tuple[AgentTurnRecord, ...] = ()
    recent_tool_invocations: tuple[ToolInvocationRecord, ...] = ()
    artifact_refs: tuple[ArtifactRef, ...] = ()
    memory_items: tuple[SessionMemoryItem, ...] = ()
    runtime_status_summary: dict[str, Any] = field(default_factory=dict)
    delivery_state_summary: dict[str, Any] = field(default_factory=dict)
    mailbox_summary: dict[str, Any] = field(default_factory=dict)
    blackboard_summary: dict[str, Any] = field(default_factory=dict)
    task_surface_authority: dict[str, Any] = field(default_factory=dict)
    shell_attach_summary: dict[str, Any] = field(default_factory=dict)
    invalidated_continuity_reasons: tuple[str, ...] = ()
    bundle_created_at: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "work_session_id": self.work_session_id,
            "runtime_generation_id": self.runtime_generation_id,
            "head_kind": self.head_kind.value,
            "scope_id": self.scope_id,
            "conversation_head_id": self.conversation_head_id,
            "continuation_mode": self.continuation_mode,
            "last_response_id": self.last_response_id,
            "checkpoint_summary": self.checkpoint_summary,
            "recent_turns": [turn.to_dict() for turn in self.recent_turns],
            "recent_tool_invocations": [item.to_dict() for item in self.recent_tool_invocations],
            "artifact_refs": [item.to_dict() for item in self.artifact_refs],
            "memory_items": [item.to_dict() for item in self.memory_items],
            "runtime_status_summary": dict(self.runtime_status_summary),
            "delivery_state_summary": dict(self.delivery_state_summary),
            "mailbox_summary": dict(self.mailbox_summary),
            "blackboard_summary": dict(self.blackboard_summary),
            "task_surface_authority": dict(self.task_surface_authority),
            "shell_attach_summary": dict(self.shell_attach_summary),
            "invalidated_continuity_reasons": list(self.invalidated_continuity_reasons),
            "bundle_created_at": self.bundle_created_at,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "HydrationBundle":
        return cls(
            work_session_id=str(payload.get("work_session_id", "")).strip(),
            runtime_generation_id=str(payload.get("runtime_generation_id", "")).strip(),
            head_kind=_coerce_conversation_head_kind(payload.get("head_kind")),
            scope_id=_optional_string(payload.get("scope_id")),
            conversation_head_id=_optional_string(payload.get("conversation_head_id")),
            continuation_mode=str(payload.get("continuation_mode", "")).strip(),
            last_response_id=_optional_string(payload.get("last_response_id")),
            checkpoint_summary=str(payload.get("checkpoint_summary", "")),
            recent_turns=tuple(
                AgentTurnRecord.from_payload(item)
                for item in payload.get("recent_turns", ())
                if isinstance(item, Mapping)
            ),
            recent_tool_invocations=tuple(
                ToolInvocationRecord.from_payload(item)
                for item in payload.get("recent_tool_invocations", ())
                if isinstance(item, Mapping)
            ),
            artifact_refs=tuple(
                ArtifactRef.from_payload(item)
                for item in payload.get("artifact_refs", ())
                if isinstance(item, Mapping)
            ),
            memory_items=tuple(
                SessionMemoryItem.from_payload(item)
                for item in payload.get("memory_items", ())
                if isinstance(item, Mapping)
            ),
            runtime_status_summary=_mapping(payload.get("runtime_status_summary")),
            delivery_state_summary=_mapping(payload.get("delivery_state_summary")),
            mailbox_summary=_mapping(payload.get("mailbox_summary")),
            blackboard_summary=_mapping(payload.get("blackboard_summary")),
            task_surface_authority=_mapping(payload.get("task_surface_authority")),
            shell_attach_summary=_mapping(payload.get("shell_attach_summary")),
            invalidated_continuity_reasons=_string_tuple(
                payload.get("invalidated_continuity_reasons")
            ),
            bundle_created_at=str(payload.get("bundle_created_at", "")).strip(),
            metadata=_mapping(payload.get("metadata")),
        )


__all__ = [
    "AgentTurnActorRole",
    "AgentTurnKind",
    "AgentTurnRecord",
    "AgentTurnStatus",
    "ArtifactRef",
    "ArtifactRefKind",
    "ArtifactStorageKind",
    "HydrationBundle",
    "SessionMemoryItem",
    "SessionMemoryKind",
    "ToolInvocationKind",
    "ToolInvocationRecord",
]
