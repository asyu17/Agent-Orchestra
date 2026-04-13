from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from typing import Any
from uuid import uuid4

from agent_orchestra.daemon.protocol import encode_message, make_request, read_message
from agent_orchestra.daemon.server import default_daemon_socket_path


class DaemonClientError(RuntimeError):
    pass


class DaemonClient:
    def __init__(
        self,
        *,
        socket_path: str | None = None,
        timeout_seconds: float = 5.0,
    ) -> None:
        self.socket_path = str(socket_path or default_daemon_socket_path())
        self.timeout_seconds = timeout_seconds

    async def request(
        self,
        command: str,
        params: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        reader, writer = await asyncio.open_unix_connection(self.socket_path)
        request_id = f"req-{uuid4().hex}"
        writer.write(
            encode_message(
                make_request(
                    command=command,
                    params=params,
                    request_id=request_id,
                )
            )
        )
        await writer.drain()
        try:
            payload = await asyncio.wait_for(
                read_message(reader),
                timeout=self.timeout_seconds,
            )
            if payload is None:
                raise DaemonClientError("Daemon closed the connection without response.")
            if str(payload.get("type", "")) != "response":
                raise DaemonClientError("Daemon returned a non-response payload.")
            if str(payload.get("id", "")) != request_id:
                raise DaemonClientError("Daemon response request id mismatch.")
            if not bool(payload.get("ok", False)):
                raise DaemonClientError(str(payload.get("error", "Unknown daemon error.")))
            result = payload.get("result")
            if not isinstance(result, Mapping):
                raise DaemonClientError("Daemon response payload is invalid.")
            return {str(key): value for key, value in result.items()}
        finally:
            writer.close()
            await writer.wait_closed()

    async def stream_session_events(
        self,
        *,
        work_session_id: str | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        reader, writer = await asyncio.open_unix_connection(self.socket_path)
        request_id = f"req-{uuid4().hex}"
        writer.write(
            encode_message(
                make_request(
                    command="session.events",
                    params={"work_session_id": work_session_id} if work_session_id else {},
                    request_id=request_id,
                )
            )
        )
        await writer.drain()
        try:
            ack = await asyncio.wait_for(
                read_message(reader),
                timeout=self.timeout_seconds,
            )
            if ack is None:
                raise DaemonClientError("Daemon closed before stream subscription was acknowledged.")
            if str(ack.get("type", "")) != "response":
                raise DaemonClientError("Daemon returned a non-response stream acknowledgement.")
            if str(ack.get("id", "")) != request_id:
                raise DaemonClientError("Daemon stream acknowledgement request id mismatch.")
            if not bool(ack.get("ok", False)):
                raise DaemonClientError(str(ack.get("error", "Stream subscription rejected.")))

            while True:
                payload = await read_message(reader)
                if payload is None:
                    return
                if str(payload.get("type", "")) != "event":
                    continue
                event_payload = payload.get("event")
                if not isinstance(event_payload, Mapping):
                    continue
                yield {str(key): value for key, value in event_payload.items()}
        finally:
            writer.close()
            await writer.wait_closed()
