from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

PROTOCOL_VERSION = 1


def _mapping(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): item for key, item in value.items()}


def encode_message(payload: Mapping[str, Any]) -> bytes:
    return (json.dumps(dict(payload), ensure_ascii=False) + "\n").encode("utf-8")


async def read_message(reader) -> dict[str, Any] | None:
    while True:
        line = await reader.readline()
        if not line:
            return None
        text = line.decode("utf-8").strip()
        if not text:
            continue
        parsed = json.loads(text)
        if not isinstance(parsed, Mapping):
            raise ValueError("Daemon message must be a JSON object.")
        return {str(key): value for key, value in parsed.items()}


def make_request(
    *,
    command: str,
    params: Mapping[str, Any] | None = None,
    request_id: str,
) -> dict[str, Any]:
    normalized_command = str(command).strip()
    if not normalized_command:
        raise ValueError("Daemon request command is required.")
    return {
        "type": "request",
        "version": PROTOCOL_VERSION,
        "id": request_id,
        "command": normalized_command,
        "params": dict(params or {}),
    }


def validate_request(payload: Mapping[str, Any]) -> tuple[str, dict[str, Any], str]:
    message_type = str(payload.get("type", "")).strip()
    if message_type != "request":
        raise ValueError(f"Unsupported daemon message type: {message_type or '<missing>'}")
    command = str(payload.get("command", "")).strip()
    if not command:
        raise ValueError("Daemon request command is required.")
    request_id = str(payload.get("id", "")).strip()
    if not request_id:
        raise ValueError("Daemon request id is required.")
    params = _mapping(payload.get("params"))
    return command, params, request_id


def make_response(
    *,
    request_id: str,
    result: Mapping[str, Any] | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": "response",
        "version": PROTOCOL_VERSION,
        "id": request_id,
        "ok": error is None,
    }
    if error is None:
        payload["result"] = dict(result or {})
    else:
        payload["error"] = str(error)
    return payload


def make_event(
    *,
    stream: str,
    event: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "type": "event",
        "version": PROTOCOL_VERSION,
        "stream": str(stream).strip() or "session.events",
        "event": dict(event),
    }

