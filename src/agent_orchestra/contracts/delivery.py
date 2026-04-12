from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class DeliveryStateKind(str, Enum):
    LANE = "lane"
    OBJECTIVE = "objective"


class DeliveryStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING = "waiting"
    WAITING_FOR_AUTHORITY = "waiting_for_authority"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    FAILED = "failed"


class DeliveryDecision(str, Enum):
    CONTINUE = "continue"
    COMPLETE = "complete"
    BLOCK = "block"
    FAIL = "fail"


@dataclass(slots=True)
class DeliveryState:
    delivery_id: str
    objective_id: str
    kind: DeliveryStateKind
    status: DeliveryStatus
    lane_id: str | None = None
    team_id: str | None = None
    iteration: int = 0
    summary: str = ""
    pending_task_ids: tuple[str, ...] = ()
    active_task_ids: tuple[str, ...] = ()
    completed_task_ids: tuple[str, ...] = ()
    blocked_task_ids: tuple[str, ...] = ()
    latest_worker_ids: tuple[str, ...] = ()
    mailbox_cursor: dict[str, Any] | str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
