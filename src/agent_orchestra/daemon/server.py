from __future__ import annotations

import asyncio
import os
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent_orchestra.contracts.daemon import AgentIncarnation
from agent_orchestra.contracts.enums import WorkerStatus
from agent_orchestra.contracts.execution import WorkerExecutionPolicy, WorkerSession, WorkerSessionStatus
from agent_orchestra.daemon.event_stream import EventStreamHub
from agent_orchestra.daemon.slot_manager import SlotManager
from agent_orchestra.daemon.protocol import (
    encode_message,
    make_event,
    make_response,
    read_message,
    validate_request,
)
from agent_orchestra.daemon.registry import ClientRegistry
from agent_orchestra.daemon.supervisor import SlotSupervisor


def default_daemon_socket_path() -> str:
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
    return str(Path(runtime_dir) / "agent-orchestra.sock")


class DaemonServer:
    def __init__(
        self,
        *,
        socket_path: str | None = None,
        app: Any | None = None,
        orchestra: Any | None = None,
        store_backend: str = "in-memory",
        dsn: str | None = None,
        schema: str = "agent_orchestra",
    ) -> None:
        self.socket_path = str(socket_path or default_daemon_socket_path())
        if app is None:
            from agent_orchestra.cli.app import CliApplication
            from agent_orchestra.runtime.orchestrator import build_orchestra_for_store_backend

            active_orchestra = orchestra or build_orchestra_for_store_backend(
                store_backend=store_backend,
                dsn=dsn,
                schema=schema,
            )
            self._app = CliApplication(orchestra=active_orchestra)
        else:
            self._app = app
        self._registry = ClientRegistry()
        self._event_hub = EventStreamHub()
        self._server: asyncio.AbstractServer | None = None
        self._closing = False
        self._tracked_work_session_ids: set[str] = set()
        self._processed_terminal_record_keys: set[tuple[str, str, str, str]] = set()
        self._emitted_session_event_ids: set[str] = set()
        self._background_tasks: set[asyncio.Task[None]] = set()
        self._slot_restart_tasks: dict[str, asyncio.Task[None]] = {}
        self._runtime = getattr(self._app, "runtime", None)
        self._store = getattr(getattr(self._app, "orchestra", None), "store", None)
        self._worker_supervisor = getattr(self._runtime, "supervisor", None)
        if self._store is not None:
            self._slot_manager = SlotManager(store=self._store)
            self._slot_supervisor = SlotSupervisor(
                store=self._store,
                slot_manager=self._slot_manager,
            )
        else:
            self._slot_manager = None
            self._slot_supervisor = None

    @property
    def is_running(self) -> bool:
        return self._server is not None and self._server.is_serving()

    async def start(self) -> None:
        if self._server is not None:
            return
        socket_file = Path(self.socket_path)
        socket_file.parent.mkdir(parents=True, exist_ok=True)
        if socket_file.exists():
            socket_file.unlink()
        self._server = await asyncio.start_unix_server(
            self._handle_client,
            path=self.socket_path,
        )
        self._start_background_tasks()

    async def serve_forever(self) -> None:
        await self.start()
        assert self._server is not None
        async with self._server:
            await self._server.serve_forever()

    async def close(self) -> None:
        if self._server is None:
            return
        if self._closing:
            return
        self._closing = True
        try:
            self._event_hub.close()
            for task in tuple(self._background_tasks):
                task.cancel()
            for task in tuple(self._slot_restart_tasks.values()):
                task.cancel()
            await self._registry.close_all()
            self._server.close()
            await self._server.wait_closed()
            socket_file = Path(self.socket_path)
            if socket_file.exists():
                socket_file.unlink()
        finally:
            self._background_tasks.clear()
            self._slot_restart_tasks.clear()
            self._server = None
            self._closing = False

    def _start_background_tasks(self) -> None:
        if self._store is None:
            return
        self._spawn_background_task(self._relay_session_events_loop())
        if self._slot_supervisor is not None:
            self._spawn_background_task(self._slot_supervision_loop())

    def _spawn_background_task(self, coro: object) -> None:
        task = asyncio.create_task(coro)  # type: ignore[arg-type]
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _relay_session_events_loop(self) -> None:
        if self._store is None:
            return
        while not self._closing:
            for work_session_id in tuple(self._tracked_work_session_ids):
                for event in await self._store.list_session_events(work_session_id):
                    if event.session_event_id in self._emitted_session_event_ids:
                        continue
                    self._emitted_session_event_ids.add(event.session_event_id)
                    self._event_hub.publish(
                        {
                            "command": "session.event",
                            "work_session_id": event.work_session_id,
                            "runtime_generation_id": event.runtime_generation_id,
                            "event_id": event.session_event_id,
                            "event_kind": event.event_kind,
                            "payload": dict(event.payload),
                            "timestamp": event.created_at,
                        },
                        replay=False,
                    )
            await asyncio.sleep(0.2)

    async def _slot_supervision_loop(self) -> None:
        if self._store is None or self._slot_manager is None or self._slot_supervisor is None:
            return
        while not self._closing:
            sessions = await self._store.list_worker_sessions()
            for worker_session in sessions:
                if worker_session.status not in {
                    WorkerSessionStatus.ASSIGNED,
                    WorkerSessionStatus.ACTIVE,
                    WorkerSessionStatus.IDLE,
                }:
                    continue
                work_session_id = _optional_string(worker_session.metadata.get("work_session_id"))
                if work_session_id:
                    self._tracked_work_session_ids.add(work_session_id)
                await self._slot_manager.materialize_slot_from_worker_session(worker_session)

            records = await self._store.list_worker_records()
            for record in records:
                if record.status not in {
                    WorkerStatus.COMPLETED,
                    WorkerStatus.CANCELLED,
                    WorkerStatus.FAILED,
                }:
                    continue
                record_key = (
                    record.worker_id,
                    record.assignment_id,
                    record.ended_at or "",
                    record.status.value,
                )
                if record_key in self._processed_terminal_record_keys:
                    continue
                session_id = (
                    _optional_string(record.metadata.get("worker_session_id"))
                    or _optional_string(record.metadata.get("last_worker_session_id"))
                    or (record.session.session_id if record.session is not None else None)
                )
                if session_id is None:
                    self._processed_terminal_record_keys.add(record_key)
                    continue
                worker_session = await self._store.get_worker_session(session_id)
                if worker_session is None:
                    self._processed_terminal_record_keys.add(record_key)
                    continue
                replacement = await self._slot_supervisor.maybe_replace_incarnation(
                    worker_session=worker_session,
                    record=record,
                )
                self._processed_terminal_record_keys.add(record_key)
                if replacement is not None:
                    self._publish_slot_restart_event(worker_session=worker_session, replacement=replacement)
                    self._schedule_slot_restart(worker_session=worker_session, replacement=replacement)
            await asyncio.sleep(0.2)

    def _publish_slot_restart_event(
        self,
        *,
        worker_session: WorkerSession,
        replacement: AgentIncarnation,
    ) -> None:
        self._event_hub.publish(
            {
                "command": "slot.restart_queued",
                "work_session_id": _optional_string(worker_session.metadata.get("work_session_id")),
                "slot_id": replacement.slot_id,
                "incarnation_id": replacement.incarnation_id,
                "timestamp": datetime.now(UTC).isoformat(),
            }
        )

    def _schedule_slot_restart(
        self,
        *,
        worker_session: WorkerSession,
        replacement: AgentIncarnation,
    ) -> None:
        if self._runtime is None or self._worker_supervisor is None:
            return
        existing = self._slot_restart_tasks.get(replacement.slot_id)
        if existing is not None and not existing.done():
            return
        task = asyncio.create_task(
            self._restart_slot_incarnation(
                worker_session=worker_session,
                replacement=replacement,
            )
        )
        self._slot_restart_tasks[replacement.slot_id] = task
        task.add_done_callback(lambda _: self._slot_restart_tasks.pop(replacement.slot_id, None))

    async def _restart_slot_incarnation(
        self,
        *,
        worker_session: WorkerSession,
        replacement: AgentIncarnation,
    ) -> None:
        assignment_from_session = getattr(self._worker_supervisor, "_assignment_from_session", None)
        if not callable(assignment_from_session):
            return
        metadata = dict(worker_session.metadata)
        metadata["incarnation_id"] = replacement.incarnation_id
        metadata["slot_lease_id"] = replacement.lease_id
        metadata["incarnation_status"] = "active"
        relaunch_session = replace(
            WorkerSession.from_dict(worker_session.to_dict()),
            supervisor_lease_id=replacement.lease_id,
            slot_lease_id=replacement.lease_id,
            incarnation_id=replacement.incarnation_id,
            incarnation_status="active",
            metadata=metadata,
        )
        assignment = assignment_from_session(relaunch_session)
        if not assignment.instructions.strip() and not assignment.input_text.strip():
            return
        await self._runtime.run_worker_assignment(
            assignment,
            policy=WorkerExecutionPolicy(),
        )

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        client_id = self._registry.register(writer)
        try:
            while True:
                payload = await read_message(reader)
                if payload is None:
                    return
                try:
                    command, params, request_id = validate_request(payload)
                except ValueError as exc:
                    writer.write(encode_message(make_response(request_id="unknown", error=str(exc))))
                    await writer.drain()
                    continue
                if command == "session.events":
                    await self._serve_event_stream(
                        writer=writer,
                        request_id=request_id,
                        params=params,
                    )
                    return
                try:
                    result = await self._dispatch_command(command, params)
                    writer.write(
                        encode_message(
                            make_response(
                                request_id=request_id,
                                result=result,
                            )
                        )
                    )
                    await writer.drain()
                except Exception as exc:  # pragma: no cover - defensive guard
                    writer.write(
                        encode_message(
                            make_response(
                                request_id=request_id,
                                error=str(exc),
                            )
                        )
                    )
                    await writer.drain()
        finally:
            self._registry.unregister(client_id)
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    async def _serve_event_stream(
        self,
        *,
        writer: asyncio.StreamWriter,
        request_id: str,
        params: Mapping[str, Any],
    ) -> None:
        work_session_id = str(params.get("work_session_id", "")).strip() or None
        subscription = self._event_hub.subscribe(work_session_id=work_session_id)
        writer.write(
            encode_message(
                make_response(
                    request_id=request_id,
                    result={
                        "command": "session.events",
                        "status": "subscribed",
                        "work_session_id": work_session_id,
                    },
                )
            )
        )
        await writer.drain()
        try:
            while True:
                event = await subscription.queue.get()
                if event is None:
                    return
                writer.write(
                    encode_message(
                        make_event(
                            stream="session.events",
                            event=event,
                        )
                    )
                )
                await writer.drain()
        finally:
            self._event_hub.unsubscribe(subscription.subscription_id)

    async def _dispatch_command(
        self,
        command: str,
        params: Mapping[str, Any],
    ) -> dict[str, Any]:
        if command == "server.status":
            return {
                "command": "server.status",
                "status": "running" if self.is_running else "stopped",
                "socket_path": self.socket_path,
                "connected_clients": self._registry.count(),
            }
        if command == "server.stop":
            asyncio.create_task(self.close())
            return {
                "command": "server.stop",
                "status": "stopping",
            }

        if command == "session.new":
            result = await self._app.session_new(
                group_id=str(params.get("group_id", "")).strip(),
                objective_id=str(params.get("objective_id", "")).strip(),
                title=str(params["title"]) if "title" in params and params["title"] is not None else None,
            )
        elif command == "session.list":
            result = await self._app.session_list(
                group_id=str(params.get("group_id", "")).strip(),
                objective_id=(
                    str(params["objective_id"]).strip()
                    if "objective_id" in params and params["objective_id"] is not None
                    else None
                ),
            )
        elif command == "session.inspect":
            result = await self._app.session_inspect(
                work_session_id=str(params.get("work_session_id", "")).strip(),
            )
        elif command == "session.attach":
            result = await self._app.session_attach(
                work_session_id=str(params.get("work_session_id", "")).strip(),
                force_warm_resume=bool(params.get("force_warm_resume", False)),
            )
        elif command == "session.wake":
            result = await self._app.session_wake(
                work_session_id=str(params.get("work_session_id", "")).strip(),
            )
        elif command == "session.fork":
            result = await self._app.session_fork(
                work_session_id=str(params.get("work_session_id", "")).strip(),
                title=str(params["title"]) if "title" in params and params["title"] is not None else None,
            )
        elif command == "session.send":
            result = await self._app.session_send(
                work_session_id=str(params.get("work_session_id", "")).strip(),
                content=str(params.get("content", "")),
                role=str(params.get("role", "user")).strip() or "user",
                scope_kind=str(params.get("scope_kind", "session")).strip() or "session",
                scope_id=(
                    str(params["scope_id"]).strip()
                    if "scope_id" in params and params["scope_id"] is not None
                    else None
                ),
            )
        else:
            raise ValueError(f"Unsupported daemon command: {command}")

        self._event_hub.publish(
            {
                "command": command,
                "work_session_id": self._extract_work_session_id(result),
                "timestamp": datetime.now(UTC).isoformat(),
            },
            replay=False,
        )
        self._track_work_session_ids(result)
        return result

    def _track_work_session_ids(self, payload: Mapping[str, Any]) -> None:
        direct = self._extract_work_session_id(payload)
        if direct is not None:
            self._tracked_work_session_ids.add(direct)
        sessions = payload.get("sessions")
        if not isinstance(sessions, list):
            return
        for item in sessions:
            if not isinstance(item, Mapping):
                continue
            work_session_id = item.get("work_session_id")
            if isinstance(work_session_id, str) and work_session_id.strip():
                self._tracked_work_session_ids.add(work_session_id.strip())

    @staticmethod
    def _extract_work_session_id(payload: Mapping[str, Any]) -> str | None:
        direct = payload.get("work_session_id")
        if isinstance(direct, str) and direct.strip():
            return direct.strip()
        continuity = payload.get("continuity")
        if not isinstance(continuity, Mapping):
            return None
        work_session = continuity.get("work_session")
        if not isinstance(work_session, Mapping):
            return None
        work_session_id = work_session.get("work_session_id")
        if isinstance(work_session_id, str) and work_session_id.strip():
            return work_session_id.strip()
        return None


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
