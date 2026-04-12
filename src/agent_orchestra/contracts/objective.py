from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agent_orchestra.contracts.enums import SpecEdgeKind, SpecNodeKind, SpecNodeStatus, TaskScope


@dataclass(slots=True)
class ObjectiveSpec:
    objective_id: str
    group_id: str
    title: str
    description: str
    success_metrics: tuple[str, ...] = ()
    hard_constraints: tuple[str, ...] = ()
    budget: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class SpecNode:
    node_id: str
    objective_id: str
    kind: SpecNodeKind
    title: str
    summary: str
    scope: TaskScope
    lane_id: str | None = None
    team_id: str | None = None
    created_by: str | None = None
    status: SpecNodeStatus = SpecNodeStatus.OPEN
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class SpecEdge:
    edge_id: str
    objective_id: str
    kind: SpecEdgeKind
    from_node_id: str
    to_node_id: str
    metadata: dict[str, Any] = field(default_factory=dict)
