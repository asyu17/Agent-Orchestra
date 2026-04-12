from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

DIRECTED_MAILBOX_PROTOCOL_NAME = "agent_orchestra.directed_mailbox"
DIRECTED_MAILBOX_PROTOCOL_VERSION = 1


def _as_str(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value or None
    return str(value)


def _coerce_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, str)):
        normalized = str(value).strip().lower()
        if normalized in {"true", "1", "yes"}:
            return True
        if normalized in {"false", "0", "no"}:
            return False
    return None


def _sequence_of_strings(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (list, tuple)):
        return tuple(str(item) for item in value if item is not None)
    return (str(value),)


def _coerce_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    return {}


def _first_not_none(*values: Any) -> Any:
    for value in values:
        if value is not None:
            return value
    return None


def _default_protocol_header(message_type: str) -> "DirectedProtocolHeader":
    return DirectedProtocolHeader(
        name=DIRECTED_MAILBOX_PROTOCOL_NAME,
        version=DIRECTED_MAILBOX_PROTOCOL_VERSION,
        message_type=message_type,
    )


@dataclass(slots=True)
class DirectedProtocolHeader:
    name: str
    version: int
    message_type: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "message_type": self.message_type,
        }


@dataclass(slots=True)
class DirectedTask:
    task_id: str
    goal: str | None = None
    reason: str | None = None
    scope: str | None = None
    derived_from: str | None = None
    owned_paths: tuple[str, ...] = ()
    verification_commands: tuple[str, ...] = ()


@dataclass(slots=True)
class DirectedTaskTarget:
    worker_id: str | None = None
    slot: int | None = None
    group_id: str | None = None
    lane_id: str | None = None
    team_id: str | None = None
    delivery_id: str | None = None


@dataclass(slots=True)
class DirectedTaskClaim:
    mode: str | None = None
    claim_source: str | None = None
    claim_session_id: str | None = None
    if_unclaimed: bool | None = None
    expires_at: str | None = None


@dataclass(slots=True)
class DirectedTaskIntent:
    action: str | None = None
    priority: str | None = None
    requires_ack_stage: str | None = None


@dataclass(slots=True)
class DirectedTaskContext:
    leader_turn_index: int | None = None
    leader_assignment_id: str | None = None
    parent_task_id: str | None = None
    source_blackboard_ref: str | None = None


@dataclass(slots=True)
class DirectedTaskCompat:
    task_id: str | None = None


@dataclass(slots=True)
class DirectedTaskDirective:
    protocol: DirectedProtocolHeader
    directive_id: str | None
    correlation_id: str | None
    task: DirectedTask
    target: DirectedTaskTarget
    claim: DirectedTaskClaim
    intent: DirectedTaskIntent
    context: DirectedTaskContext
    compat: DirectedTaskCompat


@dataclass(slots=True)
class DirectedTaskReceipt:
    directive_id: str
    receipt_type: str
    task_id: str
    claim_session_id: str | None = None
    consumer_cursor: Mapping[str, Any] | None = None
    delivery_id: str | None = None
    status_summary: str | None = None
    correlation_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "protocol": _default_protocol_header("task.receipt").to_dict(),
            "directive_id": self.directive_id,
            "receipt_type": self.receipt_type,
            "task_id": self.task_id,
        }
        if self.claim_session_id is not None:
            payload["claim_session_id"] = self.claim_session_id
        if self.consumer_cursor is not None:
            payload["consumer_cursor"] = dict(self.consumer_cursor)
        if self.delivery_id is not None:
            payload["delivery_id"] = self.delivery_id
        if self.status_summary is not None:
            payload["status_summary"] = self.status_summary
        if self.correlation_id is not None:
            payload["correlation_id"] = self.correlation_id
        if self.metadata:
            payload["metadata"] = dict(self.metadata)
        return payload


@dataclass(slots=True)
class DirectedTaskResult:
    task_id: str
    status: str
    summary: str | None = None
    artifact_refs: tuple[str, ...] = ()
    verification_summary: str | None = None
    correlation_id: str | None = None
    in_reply_to: str | None = None
    compat_task_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "protocol": _default_protocol_header("task.result").to_dict(),
            "task_id": self.task_id,
            "status": self.status,
        }
        if self.summary is not None:
            payload["summary"] = self.summary
        if self.artifact_refs:
            payload["artifact_refs"] = [str(ref) for ref in self.artifact_refs]
        if self.verification_summary is not None:
            payload["verification_summary"] = self.verification_summary
        if self.correlation_id is not None:
            payload["correlation_id"] = self.correlation_id
        if self.in_reply_to is not None:
            payload["in_reply_to"] = self.in_reply_to
        if self.compat_task_id is not None:
            payload["compat"] = {"task_id": self.compat_task_id}
        if self.metadata:
            payload["metadata"] = dict(self.metadata)
        return payload


def parse_directed_task_directive(
    payload: Mapping[str, Any],
    *,
    subject: str,
) -> DirectedTaskDirective:
    raw = {str(key): item for key, item in payload.items()}
    protocol_payload = _coerce_mapping(raw.get("protocol"))
    protocol = DirectedProtocolHeader(
        name=_as_str(protocol_payload.get("name")) or DIRECTED_MAILBOX_PROTOCOL_NAME,
        version=_coerce_int(protocol_payload.get("version")) or DIRECTED_MAILBOX_PROTOCOL_VERSION,
        message_type=_as_str(protocol_payload.get("message_type")) or subject,
    )
    task_payload = _coerce_mapping(raw.get("task"))
    compat_payload = _coerce_mapping(raw.get("compat"))
    task_id = (
        _as_str(task_payload.get("task_id"))
        or _as_str(raw.get("task_id"))
        or _as_str(compat_payload.get("task_id"))
    )
    if task_id is None:
        raise ValueError("task.directive must include a task_id")
    target_payload = _coerce_mapping(raw.get("target"))
    claim_payload = _coerce_mapping(raw.get("claim"))
    intent_payload = _coerce_mapping(raw.get("intent"))
    context_payload = _coerce_mapping(raw.get("context"))

    task = DirectedTask(
        task_id=task_id,
        goal=_as_str(task_payload.get("goal")),
        reason=_as_str(task_payload.get("reason")),
        scope=_as_str(task_payload.get("scope")),
        derived_from=_as_str(task_payload.get("derived_from")),
        owned_paths=_sequence_of_strings(task_payload.get("owned_paths")),
        verification_commands=_sequence_of_strings(task_payload.get("verification_commands")),
    )
    target = DirectedTaskTarget(
        worker_id=_first_not_none(_as_str(target_payload.get("worker_id")), _as_str(raw.get("worker_id"))),
        slot=_first_not_none(_coerce_int(target_payload.get("slot")), _coerce_int(raw.get("slot"))),
        group_id=_first_not_none(_as_str(target_payload.get("group_id")), _as_str(raw.get("group_id"))),
        lane_id=_first_not_none(_as_str(target_payload.get("lane_id")), _as_str(raw.get("lane_id"))),
        team_id=_first_not_none(_as_str(target_payload.get("team_id")), _as_str(raw.get("team_id"))),
        delivery_id=_first_not_none(_as_str(target_payload.get("delivery_id")), _as_str(raw.get("delivery_id"))),
    )
    claim = DirectedTaskClaim(
        mode=_first_not_none(_as_str(claim_payload.get("mode")), _as_str(raw.get("mode"))),
        claim_source=_first_not_none(
            _as_str(claim_payload.get("claim_source")),
            _as_str(raw.get("claim_source")),
        ),
        claim_session_id=_first_not_none(
            _as_str(claim_payload.get("claim_session_id")),
            _as_str(raw.get("claim_session_id")),
        ),
        if_unclaimed=_first_not_none(
            _coerce_bool(claim_payload.get("if_unclaimed")),
            _coerce_bool(raw.get("if_unclaimed")),
        ),
        expires_at=_first_not_none(
            _as_str(claim_payload.get("expires_at")),
            _as_str(raw.get("expires_at")),
        ),
    )
    intent = DirectedTaskIntent(
        action=_as_str(intent_payload.get("action")),
        priority=_as_str(intent_payload.get("priority")),
        requires_ack_stage=_as_str(intent_payload.get("requires_ack_stage")),
    )
    context = DirectedTaskContext(
        leader_turn_index=_coerce_int(context_payload.get("leader_turn_index")),
        leader_assignment_id=_as_str(context_payload.get("leader_assignment_id")),
        parent_task_id=_as_str(context_payload.get("parent_task_id")),
        source_blackboard_ref=_as_str(context_payload.get("source_blackboard_ref")),
    )
    compat = DirectedTaskCompat(task_id=_as_str(compat_payload.get("task_id")) or task_id)

    return DirectedTaskDirective(
        protocol=protocol,
        directive_id=_as_str(raw.get("directive_id")),
        correlation_id=_as_str(raw.get("correlation_id")),
        task=task,
        target=target,
        claim=claim,
        intent=intent,
        context=context,
        compat=compat,
    )


__all__ = [
    "DirectedTaskDirective",
    "DirectedTaskReceipt",
    "DirectedTaskResult",
    "parse_directed_task_directive",
]
