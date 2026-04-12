from __future__ import annotations

import inspect
import json
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, Mapping
from uuid import uuid4

from agent_orchestra.bus.redis_bus import RedisEventBus
from agent_orchestra.contracts.agent import AgentSession, SessionBinding
from agent_orchestra.tools.mailbox import (
    MailboxBridge as RuntimeMailboxBridge,
    MailboxCursor,
    MailboxDeliveryMode,
    MailboxDigest,
    MailboxEnvelope,
    MailboxMessageKind,
    MailboxSubscription,
    MailboxVisibilityScope,
)
from agent_orchestra.tools.permission_protocol import PermissionDecision, PermissionRequest


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _make_id(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex}"


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return str(value)


def _normalize_envelope(envelope: MailboxEnvelope) -> MailboxEnvelope:
    if envelope.envelope_id is None:
        envelope.envelope_id = _make_id("mailbox")
    if envelope.created_at is None:
        envelope.created_at = _now()
    envelope.visibility_scope = _coerce_visibility_scope(envelope.visibility_scope)
    return envelope


def _coerce_delivery_mode(value: MailboxDeliveryMode | str | None) -> MailboxDeliveryMode:
    if isinstance(value, MailboxDeliveryMode):
        return value
    if isinstance(value, str) and value:
        return MailboxDeliveryMode(value)
    return MailboxDeliveryMode.FULL_TEXT


def _coerce_message_kind(value: MailboxMessageKind | str | None) -> MailboxMessageKind:
    if isinstance(value, MailboxMessageKind):
        return value
    if isinstance(value, str) and value:
        return MailboxMessageKind(value)
    return MailboxMessageKind.SYSTEM


def _coerce_visibility_scope(value: MailboxVisibilityScope | str | None) -> MailboxVisibilityScope | str:
    if isinstance(value, MailboxVisibilityScope):
        return value
    if isinstance(value, str) and value:
        try:
            return MailboxVisibilityScope(value)
        except ValueError:
            return value
    return MailboxVisibilityScope.CONTROL_PRIVATE


def _normalize_subscription(subscription: MailboxSubscription) -> MailboxSubscription:
    normalized_id = subscription.subscription_id or _make_id("subscription")
    kinds = tuple(
        _coerce_message_kind(kind)
        for kind in subscription.kinds
        if isinstance(kind, (MailboxMessageKind, str)) and str(kind)
    )
    visibility_scopes = tuple(
        _coerce_visibility_scope(scope)
        for scope in subscription.visibility_scopes
        if isinstance(scope, (MailboxVisibilityScope, str)) and str(scope)
    )
    tags = tuple(tag for tag in subscription.tags if isinstance(tag, str) and tag)
    return MailboxSubscription(
        subscriber=subscription.subscriber,
        subscription_id=normalized_id,
        recipient=subscription.recipient,
        sender=subscription.sender,
        group_id=subscription.group_id,
        lane_id=subscription.lane_id,
        team_id=subscription.team_id,
        kinds=kinds,
        visibility_scopes=visibility_scopes,
        tags=tags,
        delivery_mode=_coerce_delivery_mode(subscription.delivery_mode),
        metadata=dict(subscription.metadata),
    )


@dataclass(slots=True)
class ReconnectCursor:
    worker_id: str
    assignment_id: str
    role: str
    backend: str
    turn_index: int
    task_id: str | None = None
    run_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "worker_id": self.worker_id,
            "assignment_id": self.assignment_id,
            "role": self.role,
            "backend": self.backend,
            "turn_index": self.turn_index,
            "task_id": self.task_id,
            "run_id": self.run_id,
            "metadata": _json_safe(dict(self.metadata)),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ReconnectCursor:
        return cls(
            worker_id=str(payload["worker_id"]),
            assignment_id=str(payload["assignment_id"]),
            role=str(payload["role"]),
            backend=str(payload["backend"]),
            turn_index=int(payload["turn_index"]),
            task_id=str(payload["task_id"]) if payload.get("task_id") is not None else None,
            run_id=str(payload["run_id"]) if payload.get("run_id") is not None else None,
            metadata=dict(payload.get("metadata", {})),
        )


@dataclass(slots=True)
class ProtocolBusEvent:
    event_id: str
    stream: str
    event_type: str
    worker_id: str | None = None
    session_id: str | None = None
    assignment_id: str | None = None
    supervisor_id: str | None = None
    lease_id: str | None = None
    cursor: dict[str, Any] = field(default_factory=dict)
    payload: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "stream": self.stream,
            "event_type": self.event_type,
            "worker_id": self.worker_id,
            "session_id": self.session_id,
            "assignment_id": self.assignment_id,
            "supervisor_id": self.supervisor_id,
            "lease_id": self.lease_id,
            "cursor": _json_safe(dict(self.cursor)),
            "payload": _json_safe(dict(self.payload)),
            "metadata": _json_safe(dict(self.metadata)),
            "created_at": self.created_at or _now(),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ProtocolBusEvent:
        return cls(
            event_id=str(payload["event_id"]),
            stream=str(payload["stream"]),
            event_type=str(payload["event_type"]),
            worker_id=str(payload["worker_id"]) if payload.get("worker_id") is not None else None,
            session_id=str(payload["session_id"]) if payload.get("session_id") is not None else None,
            assignment_id=(
                str(payload["assignment_id"]) if payload.get("assignment_id") is not None else None
            ),
            supervisor_id=(
                str(payload["supervisor_id"]) if payload.get("supervisor_id") is not None else None
            ),
            lease_id=str(payload["lease_id"]) if payload.get("lease_id") is not None else None,
            cursor=dict(payload.get("cursor", {})),
            payload=dict(payload.get("payload", {})),
            metadata=dict(payload.get("metadata", {})),
            created_at=str(payload["created_at"]) if payload.get("created_at") is not None else None,
        )


_PROTOCOL_BUS_STREAMS: tuple[str, ...] = ("lifecycle", "session", "control", "takeover", "mailbox")


def _offset_parts(value: str | None) -> tuple[int, int]:
    if not value:
        return (0, 0)
    head, separator, tail = value.partition("-")
    if not separator:
        return (0, 0)
    try:
        return (int(head), int(tail))
    except ValueError:
        return (0, 0)


def _offset_gt(left: str | None, right: str | None) -> bool:
    if left is None:
        return False
    if right is None:
        return True
    return _offset_parts(left) > _offset_parts(right)


def _normalize_protocol_stream(stream: str | None) -> str:
    raw = str(stream or "").strip().lower()
    if raw in _PROTOCOL_BUS_STREAMS:
        return raw
    if raw:
        return raw
    return "lifecycle"


def _cursor_parts(
    cursor: "ProtocolBusCursor | Mapping[str, Any] | str | None",
) -> tuple[str | None, str | None]:
    if isinstance(cursor, ProtocolBusCursor):
        return cursor.offset, cursor.event_id
    if isinstance(cursor, str):
        return (cursor if cursor else None, None)
    if isinstance(cursor, Mapping):
        offset_raw = cursor.get("offset")
        event_id_raw = cursor.get("event_id")
        offset = str(offset_raw) if offset_raw is not None else None
        event_id = str(event_id_raw) if event_id_raw is not None else None
        return offset, event_id
    return None, None


def _cursor_stream(
    cursor: "ProtocolBusCursor | Mapping[str, Any] | str | None",
    *,
    stream: str,
) -> str:
    if isinstance(cursor, ProtocolBusCursor):
        return _normalize_protocol_stream(cursor.stream)
    if isinstance(cursor, Mapping):
        value = cursor.get("stream")
        if isinstance(value, str) and value:
            return _normalize_protocol_stream(value)
    return _normalize_protocol_stream(stream)


@dataclass(slots=True)
class ProtocolBusCursor:
    stream: str
    offset: str | None = None
    event_id: str | None = None

    def to_dict(self) -> dict[str, str | None]:
        return {
            "stream": _normalize_protocol_stream(self.stream),
            "offset": self.offset,
            "event_id": self.event_id,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ProtocolBusCursor":
        return cls(
            stream=_normalize_protocol_stream(str(payload.get("stream", "lifecycle"))),
            offset=str(payload["offset"]) if payload.get("offset") is not None else None,
            event_id=str(payload["event_id"]) if payload.get("event_id") is not None else None,
        )


@dataclass(slots=True)
class ProtocolBusReadResult:
    stream: str
    events: tuple[ProtocolBusEvent, ...]
    next_cursor: dict[str, str | None]


def _next_cursor_for_events(
    *,
    stream: str,
    events: tuple[ProtocolBusEvent, ...],
    fallback: "ProtocolBusCursor | Mapping[str, Any] | str | None" = None,
) -> dict[str, str | None]:
    if events:
        last = events[-1]
        cursor = dict(last.cursor) if isinstance(last.cursor, dict) else {}
        return ProtocolBusCursor(
            stream=_normalize_protocol_stream(last.stream),
            offset=str(cursor["offset"]) if cursor.get("offset") is not None else None,
            event_id=last.event_id,
        ).to_dict()
    offset, event_id = _cursor_parts(fallback)
    return ProtocolBusCursor(
        stream=_cursor_stream(fallback, stream=stream),
        offset=offset,
        event_id=event_id,
    ).to_dict()


def _event_after_cursor(
    event: ProtocolBusEvent,
    *,
    offset: str | None,
    event_id: str | None,
    seen_event_id: bool,
) -> bool:
    if offset is not None:
        event_offset = None
        if isinstance(event.cursor, dict):
            raw_offset = event.cursor.get("offset")
            if raw_offset is not None:
                event_offset = str(raw_offset)
        return _offset_gt(event_offset, offset)
    if event_id is None:
        return True
    if seen_event_id:
        return True
    return False


def _coerce_protocol_bus_event(
    item: ProtocolBusEvent | Mapping[str, Any],
    *,
    index: int,
    default_stream: str,
    default_worker_id: str | None,
    default_assignment_id: str | None,
    default_session_id: str | None,
    default_supervisor_id: str | None,
) -> ProtocolBusEvent:
    if isinstance(item, ProtocolBusEvent):
        event = item
    else:
        payload = dict(item)
        payload.setdefault("event_id", _make_id(f"protocol-{index + 1}"))
        payload.setdefault("stream", default_stream)
        payload.setdefault("event_type", "worker.event")
        payload.setdefault("cursor", {})
        payload.setdefault("payload", {})
        payload.setdefault("metadata", {})
        event = ProtocolBusEvent.from_dict(payload)
    if event.stream != _normalize_protocol_stream(event.stream):
        event = replace(event, stream=_normalize_protocol_stream(event.stream))
    if event.worker_id is None and default_worker_id is not None:
        event = replace(event, worker_id=default_worker_id)
    if event.assignment_id is None and default_assignment_id is not None:
        event = replace(event, assignment_id=default_assignment_id)
    if event.session_id is None and default_session_id is not None:
        event = replace(event, session_id=default_session_id)
    if event.supervisor_id is None and default_supervisor_id is not None:
        event = replace(event, supervisor_id=default_supervisor_id)
    if event.created_at is None:
        event = replace(event, created_at=_now())
    return event


def protocol_bus_events_from_worker_record(record: Any) -> tuple[ProtocolBusEvent, ...]:
    metadata_raw = getattr(record, "metadata", {})
    metadata: dict[str, Any] = dict(metadata_raw) if isinstance(metadata_raw, Mapping) else {}
    worker_id_raw = getattr(record, "worker_id", None)
    assignment_id_raw = getattr(record, "assignment_id", None)
    worker_id = str(worker_id_raw) if worker_id_raw is not None else None
    assignment_id = str(assignment_id_raw) if assignment_id_raw is not None else None
    session_id_raw = metadata.get("session_id")
    supervisor_id_raw = metadata.get("supervisor_id")
    session_id = str(session_id_raw) if session_id_raw is not None else None
    supervisor_id = str(supervisor_id_raw) if supervisor_id_raw is not None else None

    events: list[ProtocolBusEvent] = []
    explicit_bus_events = metadata.get("protocol_bus_events")
    if isinstance(explicit_bus_events, (list, tuple)):
        for index, item in enumerate(explicit_bus_events):
            if isinstance(item, (ProtocolBusEvent, Mapping)):
                events.append(
                    _coerce_protocol_bus_event(
                        item,
                        index=index,
                        default_stream="lifecycle",
                        default_worker_id=worker_id,
                        default_assignment_id=assignment_id,
                        default_session_id=session_id,
                        default_supervisor_id=supervisor_id,
                    )
                )

    raw_protocol_events = metadata.get("protocol_events")
    if isinstance(raw_protocol_events, (list, tuple)):
        for index, item in enumerate(raw_protocol_events):
            if not isinstance(item, Mapping):
                continue
            phase = str(item.get("phase") or item.get("status") or "running")
            status = str(item.get("status") or phase)
            payload = {
                "status": status,
                "phase": phase,
                "summary": str(item.get("summary", "")),
                "metadata": dict(item.get("metadata", {})) if isinstance(item.get("metadata"), Mapping) else {},
            }
            created_at = str(item["timestamp"]) if item.get("timestamp") is not None else None
            stream = _normalize_protocol_stream(str(item.get("stream", "lifecycle")))
            cursor = dict(item.get("cursor", {})) if isinstance(item.get("cursor"), Mapping) else {}
            events.append(
                ProtocolBusEvent(
                    event_id=str(item.get("event_id", _make_id(f"worker-protocol-{index + 1}"))),
                    stream=stream,
                    event_type=f"worker.{phase}",
                    worker_id=str(item.get("worker_id", worker_id)) if (item.get("worker_id") or worker_id) else None,
                    session_id=(
                        str(item.get("session_id"))
                        if item.get("session_id") is not None
                        else session_id
                    ),
                    assignment_id=(
                        str(item.get("assignment_id"))
                        if item.get("assignment_id") is not None
                        else assignment_id
                    ),
                    supervisor_id=supervisor_id,
                    cursor=cursor,
                    payload=payload,
                    metadata={},
                    created_at=created_at,
                )
            )

    final_report = metadata.get("final_report")
    if isinstance(final_report, Mapping):
        events.append(
            ProtocolBusEvent(
                event_id=_make_id("worker-final-report"),
                stream="lifecycle",
                event_type="worker.final_report",
                worker_id=worker_id,
                session_id=session_id,
                assignment_id=assignment_id,
                supervisor_id=supervisor_id,
                payload=dict(final_report),
                metadata={},
                created_at=_now(),
            )
        )

    deduped: list[ProtocolBusEvent] = []
    seen_keys: set[tuple[str, str]] = set()
    for event in events:
        key = (event.event_id, event.stream)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        deduped.append(event)
    return tuple(deduped)


def session_protocol_event(
    *,
    session: AgentSession,
    event_type: str,
    binding: SessionBinding | None = None,
    stream: str | None = None,
    payload: Mapping[str, Any] | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> ProtocolBusEvent:
    normalized_stream = _normalize_protocol_stream(stream or "session")
    event_payload: dict[str, Any] = dict(payload or {})
    if binding is not None:
        event_payload["binding"] = binding.to_dict()
    event_metadata: dict[str, Any] = dict(metadata or {})
    cursor_payload = {
        "session_phase": session.phase.value,
        "session_id": session.session_id,
    }
    return ProtocolBusEvent(
        event_id=_make_id("session"),
        stream=normalized_stream,
        event_type=event_type,
        worker_id=session.agent_id,
        session_id=session.session_id,
        assignment_id=session.metadata.get("assignment_id"),
        supervisor_id=(
            binding.supervisor_id
            if binding and binding.supervisor_id is not None
            else session.current_binding.supervisor_id if session.current_binding else None
        ),
        lease_id=binding.lease_id if binding and binding.lease_id is not None else session.lease_id,
        cursor=cursor_payload,
        payload=event_payload,
        metadata=event_metadata,
    )


class ProtocolBus(ABC):
    @abstractmethod
    async def publish(self, event: ProtocolBusEvent) -> ProtocolBusEvent:
        raise NotImplementedError

    async def publish_many(self, events: tuple[ProtocolBusEvent, ...]) -> tuple[ProtocolBusEvent, ...]:
        published: list[ProtocolBusEvent] = []
        for event in events:
            published.append(await self.publish(event))
        return tuple(published)

    @abstractmethod
    async def read(
        self,
        stream: str,
        *,
        cursor: ProtocolBusCursor | Mapping[str, Any] | str | None = None,
        limit: int = 100,
    ) -> ProtocolBusReadResult:
        raise NotImplementedError

    async def catch_up(
        self,
        stream: str,
        *,
        cursor: ProtocolBusCursor | Mapping[str, Any] | str | None = None,
        limit: int = 100,
    ) -> ProtocolBusReadResult:
        return await self.read(stream, cursor=cursor, limit=limit)


class InMemoryProtocolBus(ProtocolBus):
    def __init__(self) -> None:
        self._streams: dict[str, list[ProtocolBusEvent]] = {
            stream: [] for stream in _PROTOCOL_BUS_STREAMS
        }
        self._offsets: dict[str, int] = {stream: 0 for stream in _PROTOCOL_BUS_STREAMS}

    async def publish(self, event: ProtocolBusEvent) -> ProtocolBusEvent:
        stream = _normalize_protocol_stream(event.stream)
        if stream not in self._streams:
            self._streams[stream] = []
            self._offsets[stream] = 0
        self._offsets[stream] = self._offsets.get(stream, 0) + 1
        offset = f"{self._offsets[stream]}-0"
        cursor_payload = dict(event.cursor) if isinstance(event.cursor, dict) else {}
        cursor_payload.update({"stream": stream, "offset": offset})
        published = replace(
            event,
            event_id=event.event_id or _make_id("protocol"),
            stream=stream,
            cursor=cursor_payload,
            created_at=event.created_at or _now(),
        )
        self._streams[stream].append(published)
        return published

    async def read(
        self,
        stream: str,
        *,
        cursor: ProtocolBusCursor | Mapping[str, Any] | str | None = None,
        limit: int = 100,
    ) -> ProtocolBusReadResult:
        normalized_stream = _normalize_protocol_stream(stream)
        items = list(self._streams.get(normalized_stream, []))
        offset, event_id = _cursor_parts(cursor)
        seen_event_id = event_id is None
        filtered: list[ProtocolBusEvent] = []
        for item in items:
            if event_id is not None and item.event_id == event_id:
                seen_event_id = True
                continue
            if _event_after_cursor(
                item,
                offset=offset,
                event_id=event_id,
                seen_event_id=seen_event_id,
            ):
                filtered.append(item)
        max_items = max(int(limit), 0)
        selected = tuple(filtered[:max_items]) if max_items else ()
        return ProtocolBusReadResult(
            stream=normalized_stream,
            events=selected,
            next_cursor=_next_cursor_for_events(
                stream=normalized_stream,
                events=selected,
                fallback=cursor,
            ),
        )


class RedisProtocolBus(ProtocolBus):
    def __init__(
        self,
        url: str = "redis://localhost:6379/0",
        *,
        client: Any | None = None,
        channel_prefix: str = "agent_orchestra",
        event_bus: RedisEventBus | None = None,
    ) -> None:
        self._event_bus = event_bus or RedisEventBus(
            url=url,
            client=client,
            channel_prefix=channel_prefix,
        )

    async def publish(self, event: ProtocolBusEvent) -> ProtocolBusEvent:
        stream = _normalize_protocol_stream(event.stream)
        payload = await self._event_bus.publish_protocol_event(
            stream=stream,
            event=event.to_dict(),
        )
        return ProtocolBusEvent.from_dict(payload)

    async def read(
        self,
        stream: str,
        *,
        cursor: ProtocolBusCursor | Mapping[str, Any] | str | None = None,
        limit: int = 100,
    ) -> ProtocolBusReadResult:
        normalized_stream = _normalize_protocol_stream(stream)
        cursor_payload: dict[str, Any] | str | None
        if isinstance(cursor, ProtocolBusCursor):
            cursor_payload = cursor.to_dict()
        elif isinstance(cursor, Mapping):
            cursor_payload = dict(cursor)
        else:
            cursor_payload = cursor
        raw_items = await self._event_bus.read_protocol_events(
            stream=normalized_stream,
            cursor=cursor_payload,
            limit=limit,
        )
        events = tuple(
            ProtocolBusEvent.from_dict(dict(item))
            for item in raw_items
            if isinstance(item, Mapping)
        )
        return ProtocolBusReadResult(
            stream=normalized_stream,
            events=events,
            next_cursor=_next_cursor_for_events(
                stream=normalized_stream,
                events=events,
                fallback=cursor,
            ),
        )


def _normalize_cursor(cursor: MailboxCursor | None, *, recipient: str) -> MailboxCursor:
    if cursor is None:
        return MailboxCursor(recipient=recipient)
    return MailboxCursor(
        recipient=cursor.recipient or recipient,
        last_envelope_id=cursor.last_envelope_id,
        acknowledged_ids=tuple(
            envelope_id
            for envelope_id in cursor.acknowledged_ids
            if isinstance(envelope_id, str) and envelope_id
        ),
    )


def _normalize_subscription_cursor(
    cursor: MailboxCursor | None,
    *,
    subscriber: str,
    subscription_id: str,
) -> MailboxCursor:
    normalized = _normalize_cursor(cursor, recipient=subscriber)
    return MailboxCursor(
        recipient=subscriber,
        last_envelope_id=normalized.last_envelope_id,
        acknowledged_ids=normalized.acknowledged_ids,
        subscription_id=subscription_id,
    )


def _filter_after_envelope_id(
    envelopes: list[MailboxEnvelope],
    *,
    after_envelope_id: str | None,
) -> list[MailboxEnvelope]:
    if after_envelope_id is None:
        return list(envelopes)
    if all(envelope.envelope_id != after_envelope_id for envelope in envelopes):
        return list(envelopes)
    seen = False
    filtered: list[MailboxEnvelope] = []
    for envelope in envelopes:
        if seen:
            filtered.append(envelope)
        if envelope.envelope_id == after_envelope_id:
            seen = True
    return filtered


def _subscription_matches_envelope(
    subscription: MailboxSubscription,
    envelope: MailboxEnvelope,
) -> bool:
    if subscription.recipient and envelope.recipient != subscription.recipient:
        return False
    if subscription.sender and envelope.sender != subscription.sender:
        return False
    if subscription.group_id and envelope.group_id != subscription.group_id:
        return False
    if subscription.lane_id and envelope.lane_id != subscription.lane_id:
        return False
    if subscription.team_id and envelope.team_id != subscription.team_id:
        return False
    if subscription.kinds:
        kinds = {kind.value if isinstance(kind, MailboxMessageKind) else str(kind) for kind in subscription.kinds}
        envelope_kind = envelope.kind.value if isinstance(envelope.kind, MailboxMessageKind) else str(envelope.kind)
        if envelope_kind not in kinds:
            return False
    if subscription.visibility_scopes:
        visibility_scope = _coerce_visibility_scope(envelope.visibility_scope)
        visibility_scopes = {
            _coerce_visibility_scope(scope)
            for scope in subscription.visibility_scopes
        }
        if visibility_scope not in visibility_scopes:
            return False
    if subscription.tags and not (set(subscription.tags) & set(envelope.tags)):
        return False
    return True


def _materialize_digest(
    envelope: MailboxEnvelope,
    *,
    subscription: MailboxSubscription,
) -> MailboxDigest:
    delivery_mode = _coerce_delivery_mode(subscription.delivery_mode)
    summary = envelope.summary or envelope.subject
    full_text_ref = envelope.full_text_ref if delivery_mode != MailboxDeliveryMode.SUMMARY_ONLY else None
    payload = dict(envelope.payload) if delivery_mode == MailboxDeliveryMode.FULL_TEXT else {}
    return MailboxDigest(
        subscription_id=subscription.subscription_id or "",
        subscriber=subscription.subscriber,
        envelope_id=envelope.envelope_id or "",
        sender=envelope.sender,
        recipient=envelope.recipient,
        subject=envelope.subject,
        summary=summary,
        delivery_mode=delivery_mode,
        kind=_coerce_message_kind(envelope.kind),
        mailbox_id=envelope.mailbox_id,
        full_text_ref=full_text_ref,
        group_id=envelope.group_id,
        lane_id=envelope.lane_id,
        team_id=envelope.team_id,
        source_entry_id=envelope.source_entry_id,
        source_scope=envelope.source_scope,
        visibility_scope=_coerce_visibility_scope(envelope.visibility_scope),
        severity=envelope.severity,
        created_at=envelope.created_at,
        tags=tuple(envelope.tags),
        payload=payload,
        metadata=dict(envelope.metadata),
    )


def _subscription_key(subscriber: str, subscription_id: str) -> tuple[str, str]:
    return subscriber, subscription_id


def _deserialize_envelope(raw_item: str) -> MailboxEnvelope:
    payload = json.loads(raw_item)
    payload["kind"] = _coerce_message_kind(payload.get("kind"))
    payload["delivery_mode"] = _coerce_delivery_mode(payload.get("delivery_mode"))
    payload["visibility_scope"] = _coerce_visibility_scope(payload.get("visibility_scope"))
    payload["tags"] = tuple(payload.get("tags", ()))
    payload["payload"] = dict(payload.get("payload", {}))
    payload["metadata"] = dict(payload.get("metadata", {}))
    return MailboxEnvelope(**payload)


def _deserialize_cursor(raw_cursor: str | None, *, recipient: str, subscription_id: str | None = None) -> MailboxCursor:
    if not raw_cursor:
        return MailboxCursor(recipient=recipient, subscription_id=subscription_id)
    payload = json.loads(raw_cursor)
    return MailboxCursor(
        recipient=payload.get("recipient", recipient),
        last_envelope_id=payload.get("last_envelope_id"),
        acknowledged_ids=tuple(payload.get("acknowledged_ids", ())),
        subscription_id=payload.get("subscription_id", subscription_id),
    )


def _deserialize_subscription(raw_subscription: str) -> MailboxSubscription:
    payload = json.loads(raw_subscription)
    payload["kinds"] = tuple(_coerce_message_kind(kind) for kind in payload.get("kinds", ()))
    payload["visibility_scopes"] = tuple(
        _coerce_visibility_scope(scope)
        for scope in payload.get("visibility_scopes", ())
    )
    payload["tags"] = tuple(payload.get("tags", ()))
    payload["delivery_mode"] = _coerce_delivery_mode(payload.get("delivery_mode"))
    payload["metadata"] = dict(payload.get("metadata", {}))
    return MailboxSubscription(**payload)


class MailboxBridge(RuntimeMailboxBridge, ABC):
    @abstractmethod
    async def send(self, envelope: MailboxEnvelope) -> MailboxEnvelope:
        raise NotImplementedError

    @abstractmethod
    async def list_for_recipient(
        self,
        recipient: str,
        *,
        after_envelope_id: str | None = None,
    ) -> list[MailboxEnvelope]:
        raise NotImplementedError

    @abstractmethod
    async def acknowledge(self, recipient: str, envelope_ids: tuple[str, ...]) -> MailboxCursor:
        raise NotImplementedError

    @abstractmethod
    async def get_cursor(self, recipient: str) -> MailboxCursor:
        raise NotImplementedError

    @abstractmethod
    async def list_message_pool(
        self,
        *,
        after_envelope_id: str | None = None,
    ) -> list[MailboxEnvelope]:
        raise NotImplementedError

    @abstractmethod
    async def ensure_subscription(self, subscription: MailboxSubscription) -> MailboxSubscription:
        raise NotImplementedError

    @abstractmethod
    async def list_for_subscription(
        self,
        subscriber: str,
        *,
        subscription_id: str,
        after_envelope_id: str | None = None,
    ) -> list[MailboxDigest]:
        raise NotImplementedError

    @abstractmethod
    async def acknowledge_subscription(
        self,
        subscriber: str,
        envelope_ids: tuple[str, ...],
        *,
        subscription_id: str,
    ) -> MailboxCursor:
        raise NotImplementedError

    @abstractmethod
    async def get_subscription_cursor(
        self,
        subscriber: str,
        *,
        subscription_id: str,
    ) -> MailboxCursor:
        raise NotImplementedError

    async def poll(self, recipient: str, *, limit: int = 100) -> tuple[MailboxEnvelope, ...]:
        cursor = await self.get_cursor(recipient)
        items = await self.list_for_recipient(
            recipient,
            after_envelope_id=cursor.last_envelope_id,
        )
        return tuple(items[:limit])

    async def ack(self, recipient: str, envelope_id: str) -> MailboxCursor:
        return await self.acknowledge(recipient, (envelope_id,))

    async def poll_subscription(
        self,
        subscriber: str,
        *,
        subscription_id: str,
        limit: int = 100,
    ) -> tuple[MailboxDigest, ...]:
        cursor = await self.get_subscription_cursor(subscriber, subscription_id=subscription_id)
        items = await self.list_for_subscription(
            subscriber,
            subscription_id=subscription_id,
            after_envelope_id=cursor.last_envelope_id,
        )
        return tuple(items[:limit])


class InMemoryMailboxBridge(MailboxBridge):
    def __init__(self) -> None:
        self._mailboxes: dict[str, list[MailboxEnvelope]] = {}
        self._message_pool: list[MailboxEnvelope] = []
        self._cursors: dict[str, MailboxCursor] = {}
        self._subscriptions: dict[tuple[str, str], MailboxSubscription] = {}
        self._subscription_cursors: dict[tuple[str, str], MailboxCursor] = {}
        self._counter = 0

    def _next_id(self) -> str:
        self._counter += 1
        return f"env-{self._counter}"

    async def send(self, envelope: MailboxEnvelope) -> MailboxEnvelope:
        normalized = _normalize_envelope(envelope)
        if normalized.mailbox_id is None:
            normalized = replace(normalized, mailbox_id=normalized.recipient)
        self._mailboxes.setdefault(normalized.recipient, []).append(normalized)
        self._message_pool.append(normalized)
        self._cursors.setdefault(normalized.recipient, MailboxCursor(recipient=normalized.recipient))
        return normalized

    async def list_for_recipient(
        self,
        recipient: str,
        *,
        after_envelope_id: str | None = None,
    ) -> list[MailboxEnvelope]:
        items = list(self._mailboxes.get(recipient, []))
        return _filter_after_envelope_id(items, after_envelope_id=after_envelope_id)

    async def acknowledge(self, recipient: str, envelope_ids: tuple[str, ...]) -> MailboxCursor:
        cursor = _normalize_cursor(self._cursors.get(recipient), recipient=recipient)
        normalized_ids = tuple(
            envelope_id
            for envelope_id in envelope_ids
            if isinstance(envelope_id, str) and envelope_id
        )
        acknowledged = tuple(dict.fromkeys(cursor.acknowledged_ids + normalized_ids))
        updated = MailboxCursor(
            recipient=recipient,
            last_envelope_id=normalized_ids[-1] if normalized_ids else cursor.last_envelope_id,
            acknowledged_ids=acknowledged,
        )
        self._cursors[recipient] = updated
        return updated

    async def get_cursor(self, recipient: str) -> MailboxCursor:
        return _normalize_cursor(self._cursors.get(recipient), recipient=recipient)

    async def list_message_pool(
        self,
        *,
        after_envelope_id: str | None = None,
    ) -> list[MailboxEnvelope]:
        items = list(self._message_pool)
        return _filter_after_envelope_id(items, after_envelope_id=after_envelope_id)

    async def ensure_subscription(self, subscription: MailboxSubscription) -> MailboxSubscription:
        normalized = _normalize_subscription(subscription)
        key = _subscription_key(normalized.subscriber, normalized.subscription_id or "")
        self._subscriptions[key] = normalized
        self._subscription_cursors.setdefault(
            key,
            MailboxCursor(
                recipient=normalized.subscriber,
                subscription_id=normalized.subscription_id,
            ),
        )
        return normalized

    def _get_subscription(self, subscriber: str, subscription_id: str) -> MailboxSubscription:
        key = _subscription_key(subscriber, subscription_id)
        try:
            subscription = self._subscriptions[key]
        except KeyError as exc:
            raise ValueError(
                f"Unknown mailbox subscription '{subscription_id}' for subscriber '{subscriber}'."
            ) from exc
        return subscription

    async def list_for_subscription(
        self,
        subscriber: str,
        *,
        subscription_id: str,
        after_envelope_id: str | None = None,
    ) -> list[MailboxDigest]:
        subscription = self._get_subscription(subscriber, subscription_id)
        pool = await self.list_message_pool(after_envelope_id=after_envelope_id)
        return [
            _materialize_digest(envelope, subscription=subscription)
            for envelope in pool
            if _subscription_matches_envelope(subscription, envelope)
        ]

    async def acknowledge_subscription(
        self,
        subscriber: str,
        envelope_ids: tuple[str, ...],
        *,
        subscription_id: str,
    ) -> MailboxCursor:
        subscription = self._get_subscription(subscriber, subscription_id)
        key = _subscription_key(subscriber, subscription.subscription_id or subscription_id)
        cursor = _normalize_subscription_cursor(
            self._subscription_cursors.get(key),
            subscriber=subscriber,
            subscription_id=subscription.subscription_id or subscription_id,
        )
        normalized_ids = tuple(
            envelope_id
            for envelope_id in envelope_ids
            if isinstance(envelope_id, str) and envelope_id
        )
        updated = MailboxCursor(
            recipient=subscriber,
            last_envelope_id=normalized_ids[-1] if normalized_ids else cursor.last_envelope_id,
            acknowledged_ids=tuple(dict.fromkeys(cursor.acknowledged_ids + normalized_ids)),
            subscription_id=subscription.subscription_id,
        )
        self._subscription_cursors[key] = updated
        return updated

    async def get_subscription_cursor(
        self,
        subscriber: str,
        *,
        subscription_id: str,
    ) -> MailboxCursor:
        subscription = self._get_subscription(subscriber, subscription_id)
        key = _subscription_key(subscriber, subscription.subscription_id or subscription_id)
        return _normalize_subscription_cursor(
            self._subscription_cursors.get(key),
            subscriber=subscriber,
            subscription_id=subscription.subscription_id or subscription_id,
        )


class RedisMailboxBridge(MailboxBridge):
    def __init__(
        self,
        url: str = "redis://localhost:6379/0",
        *,
        client: Any | None = None,
        channel_prefix: str = "agent_orchestra",
    ) -> None:
        self.url = url
        self._client = client
        self.channel_prefix = channel_prefix

    def _channel(self, recipient: str) -> str:
        return f"{self.channel_prefix}:mailbox:{recipient}"

    def _cursor_key(self, recipient: str) -> str:
        return f"{self.channel_prefix}:mailbox_cursor:{recipient}"

    def _pool_key(self) -> str:
        return f"{self.channel_prefix}:message_pool"

    def _subscription_key(self, subscriber: str, subscription_id: str) -> str:
        return f"{self.channel_prefix}:subscription:{subscriber}:{subscription_id}"

    def _subscription_cursor_key(self, subscriber: str, subscription_id: str) -> str:
        return f"{self.channel_prefix}:subscription_cursor:{subscriber}:{subscription_id}"

    async def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            from redis.asyncio import from_url  # type: ignore
        except ImportError as exc:
            raise RuntimeError("redis is required for RedisMailboxBridge. Install the 'redis' extra.") from exc
        self._client = from_url(self.url, decode_responses=True)
        return self._client

    async def send(self, envelope: MailboxEnvelope) -> MailboxEnvelope:
        normalized = _normalize_envelope(envelope)
        if normalized.mailbox_id is None:
            normalized = replace(normalized, mailbox_id=normalized.recipient)
        client = await self._get_client()
        payload = json.dumps(asdict(normalized), ensure_ascii=True)
        await _maybe_await(client.rpush(self._channel(normalized.recipient), payload))
        await _maybe_await(client.rpush(self._pool_key(), payload))
        return normalized

    async def list_for_recipient(
        self,
        recipient: str,
        *,
        after_envelope_id: str | None = None,
    ) -> list[MailboxEnvelope]:
        client = await self._get_client()
        raw_items = await _maybe_await(client.lrange(self._channel(recipient), 0, -1))
        envelopes = [_deserialize_envelope(item) for item in raw_items]
        return _filter_after_envelope_id(envelopes, after_envelope_id=after_envelope_id)

    async def acknowledge(self, recipient: str, envelope_ids: tuple[str, ...]) -> MailboxCursor:
        client = await self._get_client()
        cursor = await self.get_cursor(recipient)
        normalized_ids = tuple(
            envelope_id
            for envelope_id in envelope_ids
            if isinstance(envelope_id, str) and envelope_id
        )
        updated = MailboxCursor(
            recipient=recipient,
            last_envelope_id=normalized_ids[-1] if normalized_ids else cursor.last_envelope_id,
            acknowledged_ids=tuple(dict.fromkeys(cursor.acknowledged_ids + normalized_ids)),
        )
        await _maybe_await(
            client.set(
                self._cursor_key(recipient),
                json.dumps(asdict(updated), ensure_ascii=True),
            )
        )
        return updated

    async def get_cursor(self, recipient: str) -> MailboxCursor:
        client = await self._get_client()
        raw_cursor = await _maybe_await(client.get(self._cursor_key(recipient)))
        return _normalize_cursor(_deserialize_cursor(raw_cursor, recipient=recipient), recipient=recipient)

    async def list_message_pool(
        self,
        *,
        after_envelope_id: str | None = None,
    ) -> list[MailboxEnvelope]:
        client = await self._get_client()
        raw_items = await _maybe_await(client.lrange(self._pool_key(), 0, -1))
        envelopes = [_deserialize_envelope(item) for item in raw_items]
        return _filter_after_envelope_id(envelopes, after_envelope_id=after_envelope_id)

    async def ensure_subscription(self, subscription: MailboxSubscription) -> MailboxSubscription:
        normalized = _normalize_subscription(subscription)
        client = await self._get_client()
        await _maybe_await(
            client.set(
                self._subscription_key(normalized.subscriber, normalized.subscription_id or ""),
                json.dumps(asdict(normalized), ensure_ascii=True),
            )
        )
        return normalized

    async def _get_subscription(self, subscriber: str, subscription_id: str) -> MailboxSubscription:
        client = await self._get_client()
        raw_subscription = await _maybe_await(client.get(self._subscription_key(subscriber, subscription_id)))
        if not raw_subscription:
            raise ValueError(f"Unknown mailbox subscription '{subscription_id}' for subscriber '{subscriber}'.")
        subscription = _deserialize_subscription(raw_subscription)
        return _normalize_subscription(subscription)

    async def list_for_subscription(
        self,
        subscriber: str,
        *,
        subscription_id: str,
        after_envelope_id: str | None = None,
    ) -> list[MailboxDigest]:
        subscription = await self._get_subscription(subscriber, subscription_id)
        pool = await self.list_message_pool(after_envelope_id=after_envelope_id)
        return [
            _materialize_digest(envelope, subscription=subscription)
            for envelope in pool
            if _subscription_matches_envelope(subscription, envelope)
        ]

    async def acknowledge_subscription(
        self,
        subscriber: str,
        envelope_ids: tuple[str, ...],
        *,
        subscription_id: str,
    ) -> MailboxCursor:
        client = await self._get_client()
        subscription = await self._get_subscription(subscriber, subscription_id)
        cursor = await self.get_subscription_cursor(subscriber, subscription_id=subscription_id)
        normalized_ids = tuple(
            envelope_id
            for envelope_id in envelope_ids
            if isinstance(envelope_id, str) and envelope_id
        )
        updated = MailboxCursor(
            recipient=subscriber,
            last_envelope_id=normalized_ids[-1] if normalized_ids else cursor.last_envelope_id,
            acknowledged_ids=tuple(dict.fromkeys(cursor.acknowledged_ids + normalized_ids)),
            subscription_id=subscription.subscription_id,
        )
        await _maybe_await(
            client.set(
                self._subscription_cursor_key(subscriber, subscription.subscription_id or subscription_id),
                json.dumps(asdict(updated), ensure_ascii=True),
            )
        )
        return updated

    async def get_subscription_cursor(
        self,
        subscriber: str,
        *,
        subscription_id: str,
    ) -> MailboxCursor:
        subscription = await self._get_subscription(subscriber, subscription_id)
        client = await self._get_client()
        raw_cursor = await _maybe_await(
            client.get(self._subscription_cursor_key(subscriber, subscription.subscription_id or subscription_id))
        )
        return _normalize_subscription_cursor(
            _deserialize_cursor(
                raw_cursor,
                recipient=subscriber,
                subscription_id=subscription.subscription_id or subscription_id,
            ),
            subscriber=subscriber,
            subscription_id=subscription.subscription_id or subscription_id,
        )


class PermissionBroker(ABC):
    @abstractmethod
    async def request(self, request: PermissionRequest) -> PermissionDecision:
        raise NotImplementedError


class AutoApprovePermissionBroker(PermissionBroker):
    def __init__(self, reviewer: str = "system.auto") -> None:
        self.reviewer = reviewer

    async def request(self, request: PermissionRequest) -> PermissionDecision:
        return PermissionDecision(
            request_id=request.request_id or _make_id("permission"),
            approved=True,
            reviewer=self.reviewer,
            reason="Automatically approved.",
        )


class StaticPermissionBroker(PermissionBroker):
    def __init__(
        self,
        *,
        default_approved: bool = True,
        approved_actions: set[str] | None = None,
        denied_actions: set[str] | None = None,
        pending_actions: set[str] | None = None,
        reviewer: str = "system.static",
    ) -> None:
        self.default_approved = default_approved
        self.approved_actions = set(approved_actions or set())
        self.denied_actions = set(denied_actions or set())
        self.pending_actions = set(pending_actions or set())
        self.reviewer = reviewer

    async def request(self, request: PermissionRequest) -> PermissionDecision:
        approved = self.default_approved
        if request.action in self.approved_actions:
            approved = True
        if request.action in self.denied_actions:
            approved = False
        pending = request.action in self.pending_actions
        if pending:
            approved = False
        return PermissionDecision(
            request_id=request.request_id or _make_id("permission"),
            approved=approved,
            reviewer=self.reviewer,
            reason=(
                "Pending manual approval by static policy."
                if pending
                else "Approved by static policy."
                if approved
                else "Denied by static policy."
            ),
            pending=pending,
        )

    async def request_decision(self, request: PermissionRequest) -> PermissionDecision:
        return await self.request(request)


class ReconnectRegistry(ABC):
    @abstractmethod
    async def remember(self, cursor: ReconnectCursor) -> None:
        raise NotImplementedError

    @abstractmethod
    async def resolve(self, worker_id: str) -> ReconnectCursor | None:
        raise NotImplementedError


class InMemoryReconnectRegistry(ReconnectRegistry):
    def __init__(self, *, store: Any | None = None) -> None:
        self._cursors: dict[str, ReconnectCursor] = {}
        self._store = store

    async def remember(self, cursor: ReconnectCursor) -> None:
        if self._store is not None:
            save_method = getattr(self._store, "save_reconnect_cursor", None)
            if save_method is not None:
                await _maybe_await(save_method(cursor))
        self._cursors[cursor.worker_id] = cursor

    async def resolve(self, worker_id: str) -> ReconnectCursor | None:
        if self._store is not None:
            get_method = getattr(self._store, "get_reconnect_cursor", None)
            if get_method is not None:
                persisted = await _maybe_await(get_method(worker_id))
                if isinstance(persisted, ReconnectCursor):
                    self._cursors[worker_id] = persisted
                    return persisted
                if isinstance(persisted, dict):
                    cursor = ReconnectCursor.from_dict(persisted)
                    self._cursors[worker_id] = cursor
                    return cursor
                if persisted is not None:
                    return None
        return self._cursors.get(worker_id)
