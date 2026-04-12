from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agent_orchestra.contracts.enums import EventKind
from agent_orchestra.contracts.ids import make_event_id


@dataclass(slots=True)
class OrchestraEvent:
    event_id: str
    kind: EventKind
    group_id: str | None = None
    team_id: str | None = None
    task_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def group_created(cls, group_id: str) -> "OrchestraEvent":
        return cls(event_id=make_event_id(), kind=EventKind.GROUP_CREATED, group_id=group_id)

    @classmethod
    def team_created(cls, group_id: str, team_id: str) -> "OrchestraEvent":
        return cls(
            event_id=make_event_id(),
            kind=EventKind.TEAM_CREATED,
            group_id=group_id,
            team_id=team_id,
        )

    @classmethod
    def task_submitted(cls, group_id: str, team_id: str, task_id: str) -> "OrchestraEvent":
        return cls(
            event_id=make_event_id(),
            kind=EventKind.TASK_SUBMITTED,
            group_id=group_id,
            team_id=team_id,
            task_id=task_id,
        )

    @classmethod
    def handoff_recorded(
        cls,
        group_id: str,
        from_team_id: str,
        to_team_id: str,
        task_id: str,
    ) -> "OrchestraEvent":
        return cls(
            event_id=make_event_id(),
            kind=EventKind.HANDOFF_RECORDED,
            group_id=group_id,
            team_id=from_team_id,
            task_id=task_id,
            payload={"to_team_id": to_team_id},
        )

    @classmethod
    def authority_updated(cls, group_id: str, accepted_handoffs: tuple[str, ...]) -> "OrchestraEvent":
        return cls(
            event_id=make_event_id(),
            kind=EventKind.AUTHORITY_UPDATED,
            group_id=group_id,
            payload={"accepted_handoffs": list(accepted_handoffs)},
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "kind": self.kind.value,
            "group_id": self.group_id,
            "team_id": self.team_id,
            "task_id": self.task_id,
            "payload": dict(self.payload),
        }
