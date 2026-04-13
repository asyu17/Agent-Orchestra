from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from agent_orchestra.daemon.client import DaemonClient


class DaemonCliApplication:
    def __init__(self, *, client: DaemonClient) -> None:
        self.client = client

    async def server_status(self) -> dict[str, Any]:
        return await self.client.request("server.status")

    async def server_stop(self) -> dict[str, Any]:
        return await self.client.request("server.stop")

    async def session_new(
        self,
        *,
        group_id: str,
        objective_id: str,
        title: str | None = None,
    ) -> dict[str, Any]:
        return await self.client.request(
            "session.new",
            {
                "group_id": group_id,
                "objective_id": objective_id,
                "title": title,
            },
        )

    async def session_list(
        self,
        *,
        group_id: str,
        objective_id: str | None = None,
    ) -> dict[str, Any]:
        return await self.client.request(
            "session.list",
            {
                "group_id": group_id,
                "objective_id": objective_id,
            },
        )

    async def session_inspect(self, *, work_session_id: str) -> dict[str, Any]:
        return await self.client.request(
            "session.inspect",
            {"work_session_id": work_session_id},
        )

    async def session_attach(
        self,
        *,
        work_session_id: str,
        force_warm_resume: bool = False,
    ) -> dict[str, Any]:
        return await self.client.request(
            "session.attach",
            {
                "work_session_id": work_session_id,
                "force_warm_resume": force_warm_resume,
            },
        )

    async def session_wake(self, *, work_session_id: str) -> dict[str, Any]:
        return await self.client.request(
            "session.wake",
            {"work_session_id": work_session_id},
        )

    async def session_fork(
        self,
        *,
        work_session_id: str,
        title: str | None = None,
    ) -> dict[str, Any]:
        return await self.client.request(
            "session.fork",
            {
                "work_session_id": work_session_id,
                "title": title,
            },
        )

    async def session_send(
        self,
        *,
        work_session_id: str,
        content: str,
        role: str = "user",
        scope_kind: str = "session",
        scope_id: str | None = None,
    ) -> dict[str, Any]:
        return await self.client.request(
            "session.send",
            {
                "work_session_id": work_session_id,
                "content": content,
                "role": role,
                "scope_kind": scope_kind,
                "scope_id": scope_id,
            },
        )

    async def session_events(
        self,
        *,
        work_session_id: str | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        async for event in self.client.stream_session_events(work_session_id=work_session_id):
            yield event


def build_daemon_cli_application(*, socket_path: str | None = None) -> DaemonCliApplication:
    return DaemonCliApplication(client=DaemonClient(socket_path=socket_path))


__all__ = ["DaemonCliApplication", "build_daemon_cli_application"]
