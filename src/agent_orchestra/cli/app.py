from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, is_dataclass
from enum import Enum
from typing import Any
from agent_orchestra.runtime.session_domain import SessionResumeResult


def _serialize(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Enum):
        return value.value
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return _serialize(to_dict())
    if is_dataclass(value):
        return _serialize(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _serialize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_serialize(item) for item in value]
    return str(value)


class CliApplication:
    def __init__(self, *, orchestra: Any) -> None:
        self.orchestra = orchestra
        self.runtime = orchestra.group_runtime()

    async def session_list(
        self,
        *,
        group_id: str,
        objective_id: str | None = None,
    ) -> dict[str, object]:
        sessions = await self.runtime.list_work_sessions(
            group_id=group_id,
            objective_id=objective_id,
        )
        return {
            "command": "session.list",
            "group_id": group_id,
            "objective_id": objective_id,
            "sessions": [_serialize(session) for session in sessions],
        }

    async def session_inspect(self, *, work_session_id: str) -> dict[str, object]:
        snapshot = await self.runtime.inspect_session(work_session_id)
        return {
            "command": "session.inspect",
            "work_session_id": work_session_id,
            "snapshot": _serialize(snapshot),
        }

    async def session_new(
        self,
        *,
        group_id: str,
        objective_id: str,
        title: str | None = None,
    ) -> dict[str, object]:
        continuity = await self.runtime.new_session(
            group_id=group_id,
            objective_id=objective_id,
            title=title,
        )
        return {
            "command": "session.new",
            "group_id": group_id,
            "objective_id": objective_id,
            "continuity": {
                "work_session": _serialize(continuity.work_session),
                "runtime_generation": _serialize(continuity.runtime_generation),
                "conversation_heads": _serialize(list(continuity.conversation_heads)),
            },
        }

    async def session_fork(
        self,
        *,
        work_session_id: str,
        title: str | None = None,
    ) -> dict[str, object]:
        continuity = await self.runtime.fork_session(
            work_session_id=work_session_id,
            title=title,
        )
        return {
            "command": "session.fork",
            "work_session_id": work_session_id,
            "continuity": {
                "work_session": _serialize(continuity.work_session),
                "runtime_generation": _serialize(continuity.runtime_generation),
                "conversation_heads": _serialize(list(continuity.conversation_heads)),
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
        message = await self.runtime.send_session_message(
            work_session_id=work_session_id,
            content=content,
            role=role,
            scope_kind=scope_kind,
            scope_id=scope_id,
        )
        return {
            "command": "session.send",
            "work_session_id": work_session_id,
            "message": _serialize(message),
        }

    async def session_attach(
        self,
        *,
        work_session_id: str,
        force_warm_resume: bool = False,
    ) -> dict[str, object]:
        result: SessionResumeResult = await self.runtime.attach_session(
            work_session_id=work_session_id,
            force_warm_resume=force_warm_resume,
        )
        return {
            "command": "session.attach",
            "work_session_id": work_session_id,
            "result": result.to_dict(),
        }

    async def session_wake(
        self,
        *,
        work_session_id: str,
    ) -> dict[str, object]:
        result: SessionResumeResult = await self.runtime.wake_session(
            work_session_id=work_session_id,
        )
        return {
            "command": "session.wake",
            "work_session_id": work_session_id,
            "result": result.to_dict(),
        }


def build_cli_application(
    *,
    store_backend: str,
    dsn: str | None = None,
    schema: str = "agent_orchestra",
) -> CliApplication:
    normalized_backend = store_backend.strip().lower()
    if normalized_backend == "postgres" and (dsn is None or not dsn.strip()):
        raise ValueError("`--dsn` is required when --store-backend=postgres.")

    from agent_orchestra.runtime.orchestrator import build_orchestra_for_store_backend

    orchestra = build_orchestra_for_store_backend(
        store_backend=normalized_backend,
        dsn=dsn,
        schema=schema,
    )
    return CliApplication(orchestra=orchestra)


__all__ = ["CliApplication", "build_cli_application"]
