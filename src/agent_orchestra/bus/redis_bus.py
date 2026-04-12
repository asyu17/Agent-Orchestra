from __future__ import annotations

import inspect
import json
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from agent_orchestra.bus.base import EventBus
from agent_orchestra.contracts.events import OrchestraEvent


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _decode_raw(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return str(value)


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


class RedisEventBus(EventBus):
    _PROTOCOL_STREAMS: tuple[str, ...] = ("lifecycle", "session", "control", "takeover", "mailbox")

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

    async def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            from redis.asyncio import from_url  # type: ignore
        except ImportError as exc:
            raise RuntimeError("redis is required for RedisEventBus. Install the 'redis' extra.") from exc
        self._client = from_url(self.url, decode_responses=True)
        return self._client

    async def publish(self, event: OrchestraEvent) -> None:
        client = await self._get_client()
        channel = f"{self.channel_prefix}:{event.kind.value}"
        payload = json.dumps(event.to_dict(), ensure_ascii=True)
        await _maybe_await(client.publish(channel, payload))

    def _protocol_stream_key(self, stream: str) -> str:
        return f"{self.channel_prefix}:protocol:{stream}"

    def _normalize_stream(self, stream: str | None) -> str:
        raw = str(stream or "").strip().lower()
        if raw in self._PROTOCOL_STREAMS:
            return raw
        if raw:
            return raw
        return "lifecycle"

    def _cursor_offset(self, cursor: dict[str, Any] | str | None) -> str | None:
        if isinstance(cursor, str) and cursor:
            return cursor
        if isinstance(cursor, dict):
            offset = cursor.get("offset")
            if isinstance(offset, str) and offset:
                return offset
        return None

    async def publish_protocol_event(
        self,
        *,
        stream: str,
        event: dict[str, Any],
    ) -> dict[str, Any]:
        normalized_stream = self._normalize_stream(stream)
        payload = dict(event)
        payload["stream"] = normalized_stream
        payload.setdefault("event_id", f"protocol-{uuid4().hex}")
        payload.setdefault("created_at", _now())
        client = await self._get_client()
        stream_key = self._protocol_stream_key(normalized_stream)
        offset: str
        xadd = getattr(client, "xadd", None)
        if callable(xadd):
            offset = str(
                await _maybe_await(
                    xadd(
                        stream_key,
                        {"payload": json.dumps(payload, ensure_ascii=True)},
                    )
                )
            )
        else:
            length = await _maybe_await(client.rpush(stream_key, json.dumps(payload, ensure_ascii=True)))
            offset = f"{int(length)}-0"
        cursor_payload = payload.get("cursor")
        if not isinstance(cursor_payload, dict):
            cursor_payload = {}
        cursor_payload = dict(cursor_payload)
        cursor_payload.update({"stream": normalized_stream, "offset": offset})
        payload["cursor"] = cursor_payload
        return payload

    async def read_protocol_events(
        self,
        *,
        stream: str,
        cursor: dict[str, Any] | str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        normalized_stream = self._normalize_stream(stream)
        max_items = max(int(limit), 0)
        if max_items == 0:
            return []
        cursor_offset = self._cursor_offset(cursor)
        client = await self._get_client()
        stream_key = self._protocol_stream_key(normalized_stream)

        events: list[dict[str, Any]] = []
        xrange = getattr(client, "xrange", None)
        if callable(xrange):
            min_token = "-" if cursor_offset is None else f"({cursor_offset}"
            try:
                raw_rows = await _maybe_await(
                    xrange(
                        stream_key,
                        min=min_token,
                        max="+",
                        count=max_items,
                    )
                )
            except TypeError:
                raw_rows = await _maybe_await(xrange(stream_key, min_token, "+"))
            for row_id, fields in raw_rows:
                payload: dict[str, Any] = {}
                if isinstance(fields, dict):
                    raw_payload = fields.get("payload")
                    if raw_payload is not None:
                        payload = json.loads(_decode_raw(raw_payload))
                payload["stream"] = normalized_stream
                cursor_payload = payload.get("cursor")
                if not isinstance(cursor_payload, dict):
                    cursor_payload = {}
                cursor_payload = dict(cursor_payload)
                cursor_payload.update({"stream": normalized_stream, "offset": _decode_raw(row_id)})
                payload["cursor"] = cursor_payload
                events.append(payload)
                if len(events) >= max_items:
                    break
            return events

        raw_items = await _maybe_await(client.lrange(stream_key, 0, -1))
        for index, raw_item in enumerate(raw_items, start=1):
            payload = json.loads(_decode_raw(raw_item))
            event_offset = f"{index}-0"
            if not _offset_gt(event_offset, cursor_offset):
                continue
            payload["stream"] = normalized_stream
            cursor_payload = payload.get("cursor")
            if not isinstance(cursor_payload, dict):
                cursor_payload = {}
            cursor_payload = dict(cursor_payload)
            cursor_payload.update({"stream": normalized_stream, "offset": event_offset})
            payload["cursor"] = cursor_payload
            events.append(payload)
            if len(events) >= max_items:
                break
        return events

    async def healthcheck(self) -> bool:
        client = await self._get_client()
        pong = await _maybe_await(client.ping())
        return bool(pong)
