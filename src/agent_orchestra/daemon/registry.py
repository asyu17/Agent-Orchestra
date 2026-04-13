from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4


@dataclass(slots=True)
class ConnectedClient:
    client_id: str
    connected_at: str
    writer: Any


class ClientRegistry:
    def __init__(self) -> None:
        self._clients: dict[str, ConnectedClient] = {}

    def register(self, writer: Any) -> str:
        client_id = f"client-{uuid4().hex}"
        self._clients[client_id] = ConnectedClient(
            client_id=client_id,
            connected_at=datetime.now(UTC).isoformat(),
            writer=writer,
        )
        return client_id

    def unregister(self, client_id: str) -> None:
        self._clients.pop(client_id, None)

    def count(self) -> int:
        return len(self._clients)

    async def close_all(self) -> None:
        clients = tuple(self._clients.values())
        self._clients.clear()
        for client in clients:
            try:
                client.writer.close()
                await client.writer.wait_closed()
            except Exception:
                continue
