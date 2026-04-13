from __future__ import annotations

import asyncio
import sys
import tempfile
import types
from pathlib import Path
from unittest import IsolatedAsyncioTestCase

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

if "agent_orchestra" not in sys.modules:
    package = types.ModuleType("agent_orchestra")
    package.__path__ = [str(SRC / "agent_orchestra")]
    sys.modules["agent_orchestra"] = package

from agent_orchestra.daemon.client import DaemonClient
from agent_orchestra.daemon.server import DaemonServer
from agent_orchestra.contracts.daemon import ProviderRouteHealth, ProviderRouteStatus


class _FakeSessionApp:
    def __init__(self) -> None:
        self._sessions: dict[str, dict[str, object]] = {}
        self._counter = 0

    async def session_new(
        self,
        *,
        group_id: str,
        objective_id: str,
        title: str | None = None,
    ) -> dict[str, object]:
        self._counter += 1
        work_session_id = f"ws-{self._counter}"
        session = {
            "work_session_id": work_session_id,
            "group_id": group_id,
            "root_objective_id": objective_id,
            "title": title or "",
        }
        self._sessions[work_session_id] = session
        return {
            "command": "session.new",
            "group_id": group_id,
            "objective_id": objective_id,
            "continuity": {
                "work_session": dict(session),
                "runtime_generation": {
                    "runtime_generation_id": f"gen-{self._counter}",
                    "work_session_id": work_session_id,
                    "generation_index": 0,
                },
                "conversation_heads": [],
            },
        }

    async def session_list(
        self,
        *,
        group_id: str,
        objective_id: str | None = None,
    ) -> dict[str, object]:
        sessions = [
            session
            for session in self._sessions.values()
            if session["group_id"] == group_id
            and (objective_id is None or session["root_objective_id"] == objective_id)
        ]
        return {
            "command": "session.list",
            "group_id": group_id,
            "objective_id": objective_id,
            "sessions": sessions,
        }

    async def session_inspect(self, *, work_session_id: str) -> dict[str, object]:
        session = self._sessions[work_session_id]
        return {
            "command": "session.inspect",
            "work_session_id": work_session_id,
            "snapshot": {
                "work_session": dict(session),
                "runtime_generations": [],
                "resume_gate": {"mode": "exact_wake"},
                "continuation_bundles": [],
                "resident_shell_views": [],
                "hydration_summary": [],
            },
        }

    async def session_attach(
        self,
        *,
        work_session_id: str,
        force_warm_resume: bool = False,
    ) -> dict[str, object]:
        return {
            "command": "session.attach",
            "work_session_id": work_session_id,
            "result": {"action": "attached", "force_warm_resume": force_warm_resume},
        }

    async def session_wake(self, *, work_session_id: str) -> dict[str, object]:
        return {
            "command": "session.wake",
            "work_session_id": work_session_id,
            "result": {"action": "attached"},
        }

    async def session_fork(
        self,
        *,
        work_session_id: str,
        title: str | None = None,
    ) -> dict[str, object]:
        return {
            "command": "session.fork",
            "work_session_id": work_session_id,
            "continuity": {
                "work_session": {
                    "work_session_id": f"{work_session_id}-fork",
                    "group_id": "group-a",
                    "root_objective_id": "obj-a",
                    "title": title or "",
                },
                "runtime_generation": {
                    "runtime_generation_id": f"{work_session_id}-fork-gen",
                    "work_session_id": f"{work_session_id}-fork",
                    "generation_index": 0,
                },
                "conversation_heads": [],
            },
        }

    async def session_send(
        self,
        *,
        work_session_id: str,
        content: str,
        role: str = "user",
        scope_kind: str = "session",
        scope_id: str | None = None,
    ) -> dict[str, object]:
        return {
            "command": "session.send",
            "work_session_id": work_session_id,
            "message": {
                "message_id": f"msg-{work_session_id}",
                "work_session_id": work_session_id,
                "role": role,
                "scope_kind": scope_kind,
                "scope_id": scope_id,
                "content": content,
            },
        }


class DaemonServerTest(IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.socket_path = Path(self.tempdir.name) / "agent-orchestra.sock"
        self.server = DaemonServer(
            socket_path=str(self.socket_path),
            app=_FakeSessionApp(),
        )
        await self.server.start()
        self.client = DaemonClient(socket_path=str(self.socket_path))

    async def asyncTearDown(self) -> None:
        await self.server.close()
        self.tempdir.cleanup()

    async def test_server_status_round_trip(self) -> None:
        payload = await self.client.request("server.status")

        self.assertEqual(payload["command"], "server.status")
        self.assertEqual(payload["status"], "running")
        self.assertEqual(payload["socket_path"], str(self.socket_path))

    async def test_session_commands_round_trip(self) -> None:
        created = await self.client.request(
            "session.new",
            {
                "group_id": "group-a",
                "objective_id": "obj-a",
                "title": "Daemon session",
            },
        )
        work_session_id = created["continuity"]["work_session"]["work_session_id"]

        listed = await self.client.request(
            "session.list",
            {
                "group_id": "group-a",
                "objective_id": "obj-a",
            },
        )
        inspected = await self.client.request(
            "session.inspect",
            {
                "work_session_id": work_session_id,
            },
        )

        self.assertEqual(listed["command"], "session.list")
        self.assertEqual(len(listed["sessions"]), 1)
        self.assertEqual(
            listed["sessions"][0]["work_session_id"],
            work_session_id,
        )
        self.assertEqual(inspected["command"], "session.inspect")
        self.assertEqual(inspected["work_session_id"], work_session_id)

    async def test_session_event_stream_receives_updates(self) -> None:
        created = await self.client.request(
            "session.new",
            {
                "group_id": "group-a",
                "objective_id": "obj-a",
                "title": "Daemon stream",
            },
        )
        work_session_id = created["continuity"]["work_session"]["work_session_id"]
        stream = self.client.stream_session_events(work_session_id=work_session_id)
        next_event_task = asyncio.create_task(anext(stream))
        await asyncio.sleep(0.05)

        await self.client.request(
            "session.inspect",
            {"work_session_id": work_session_id},
        )
        event = await asyncio.wait_for(next_event_task, timeout=2.0)
        await stream.aclose()

        self.assertEqual(event["command"], "session.inspect")
        self.assertEqual(event["work_session_id"], work_session_id)

    async def test_session_send_round_trip(self) -> None:
        created = await self.client.request(
            "session.new",
            {
                "group_id": "group-a",
                "objective_id": "obj-a",
                "title": "Daemon send",
            },
        )
        work_session_id = created["continuity"]["work_session"]["work_session_id"]

        payload = await self.client.request(
            "session.send",
            {
                "work_session_id": work_session_id,
                "content": "resume from latest state",
                "role": "user",
                "scope_kind": "session",
            },
        )

        self.assertEqual(payload["command"], "session.send")
        self.assertEqual(payload["work_session_id"], work_session_id)
        self.assertEqual(payload["message"]["content"], "resume from latest state")

    async def test_real_server_inspect_and_attach_surface_provider_route_health(self) -> None:
        await self.server.close()
        self.server = DaemonServer(socket_path=str(self.socket_path), store_backend="in-memory")
        await self.server.start()
        self.client = DaemonClient(socket_path=str(self.socket_path))

        created = await self.client.request(
            "session.new",
            {
                "group_id": "group-a",
                "objective_id": "obj-a",
                "title": "Provider health",
            },
        )
        work_session_id = created["continuity"]["work_session"]["work_session_id"]
        assert self.server._store is not None
        await self.server._store.save_provider_route_health(
            ProviderRouteHealth(
                route_key="teammate:primary",
                role="teammate",
                backend="primary",
                route_fingerprint="primary",
                status=ProviderRouteStatus.QUARANTINED,
                health_score=0.0,
                consecutive_failures=2,
                cooldown_expires_at="2026-04-13T12:00:00+00:00",
                preferred=False,
                updated_at="2026-04-13T11:30:00+00:00",
                metadata={
                    "work_session_id": work_session_id,
                    "objective_id": "obj-a",
                },
            )
        )

        inspected = await self.client.request(
            "session.inspect",
            {"work_session_id": work_session_id},
        )
        attached = await self.client.request(
            "session.attach",
            {"work_session_id": work_session_id},
        )

        self.assertEqual(
            inspected["snapshot"]["provider_route_health"][0]["status"],
            ProviderRouteStatus.QUARANTINED.value,
        )
        self.assertEqual(
            attached["result"]["metadata"]["provider_route_health"][0]["status"],
            ProviderRouteStatus.QUARANTINED.value,
        )
