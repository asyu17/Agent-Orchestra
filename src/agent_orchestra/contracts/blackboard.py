from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agent_orchestra.contracts.enums import BlackboardEntryKind, BlackboardKind


@dataclass(slots=True)
class BlackboardEntry:
    entry_id: str
    blackboard_id: str
    group_id: str
    kind: BlackboardKind
    entry_kind: BlackboardEntryKind
    author_id: str
    lane_id: str | None = None
    team_id: str | None = None
    task_id: str | None = None
    summary: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: str | None = None


@dataclass(slots=True)
class BlackboardSnapshot:
    blackboard_id: str
    group_id: str
    kind: BlackboardKind
    lane_id: str | None = None
    team_id: str | None = None
    version: int = 0
    summary: str = ""
    latest_entry_ids: tuple[str, ...] = ()
    open_blockers: tuple[str, ...] = ()
    open_proposals: tuple[str, ...] = ()
