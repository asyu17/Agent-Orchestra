from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class TeamRegistrySnapshot:
    group_id: str
    team_ids: tuple[str, ...] = ()
    metadata: dict[str, str] = field(default_factory=dict)
