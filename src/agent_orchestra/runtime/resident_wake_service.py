from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from agent_orchestra.contracts.execution import ResidentCoordinatorPhase
from agent_orchestra.contracts.session_continuity import ResidentTeamShell
from agent_orchestra.runtime.session_host import ResidentSessionHost


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


@dataclass(slots=True, frozen=True)
class ResidentWakeRequestResult:
    requested_session_ids: tuple[str, ...] = ()
    wake_requested: bool = False


class ResidentWakeService:
    def __init__(self, *, session_host: ResidentSessionHost) -> None:
        self.session_host = session_host

    async def request_wake(
        self,
        *,
        shell: ResidentTeamShell,
        requested_by: str = "session.wake",
        reason: str = "Resident shell wake requested.",
    ) -> ResidentWakeRequestResult:
        requested_session_ids: list[str] = []
        leader_session_id = _optional_string(shell.leader_slot_session_id)
        if leader_session_id is not None:
            leader_session = await self.session_host.load_session(leader_session_id)
            if leader_session is not None:
                metadata = dict(leader_session.metadata) if isinstance(leader_session.metadata, Mapping) else {}
                wake_request_count = int(metadata.get("wake_request_count", 0) or 0) + 1
                metadata["wake_request_count"] = wake_request_count
                metadata["last_wake_request_at"] = shell.updated_at
                metadata["last_wake_reason"] = reason
                metadata["last_wake_requested_by"] = requested_by
                await self.session_host.update_session(
                    leader_session_id,
                    phase=ResidentCoordinatorPhase.WAITING_FOR_MAILBOX,
                    reason=reason,
                    metadata=metadata,
                    last_progress_at=shell.updated_at,
                )
                requested_session_ids.append(leader_session_id)

        for teammate_session_id in shell.teammate_slot_session_ids:
            session_id = _optional_string(teammate_session_id)
            if session_id is None:
                continue
            teammate_session = await self.session_host.load_session(session_id)
            if teammate_session is None:
                continue
            await self.session_host.record_wake_request(
                session_id,
                reason=reason,
                requested_by=requested_by,
            )
            requested_session_ids.append(session_id)
        return ResidentWakeRequestResult(
            requested_session_ids=tuple(requested_session_ids),
            wake_requested=bool(requested_session_ids),
        )


__all__ = ["ResidentWakeRequestResult", "ResidentWakeService"]
