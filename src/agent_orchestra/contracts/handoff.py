from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class HandoffRecord:
    handoff_id: str
    group_id: str
    from_team_id: str
    to_team_id: str
    task_id: str
    artifact_refs: tuple[str, ...] = field(default_factory=tuple)
    summary: str = ""
    contract_assertions: tuple[str, ...] = field(default_factory=tuple)
    verification_summary: dict[str, Any] = field(default_factory=dict)
