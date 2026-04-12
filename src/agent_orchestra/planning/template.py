from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agent_orchestra.contracts.execution import LeaderTaskCard, WorkerBudget
from agent_orchestra.contracts.objective import ObjectiveSpec, SpecEdge, SpecNode


@dataclass(slots=True)
class WorkstreamTemplate:
    workstream_id: str
    title: str
    summary: str
    team_name: str
    depends_on: tuple[str, ...] = ()
    acceptance_checks: tuple[str, ...] = ()
    budget_max_teammates: int = 0
    budget_max_iterations: int = 0
    budget_max_tokens: int | None = None
    budget_max_seconds: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "workstream_id": self.workstream_id,
            "title": self.title,
            "summary": self.summary,
            "team_name": self.team_name,
            "depends_on": list(self.depends_on),
            "acceptance_checks": list(self.acceptance_checks),
            "budget": {
                "max_teammates": self.budget_max_teammates,
                "max_iterations": self.budget_max_iterations,
                "max_tokens": self.budget_max_tokens,
                "max_seconds": self.budget_max_seconds,
            },
            "metadata": dict(self.metadata),
        }

    def to_budget(self) -> WorkerBudget:
        return WorkerBudget(
            max_teammates=self.budget_max_teammates,
            max_iterations=self.budget_max_iterations,
            max_tokens=self.budget_max_tokens,
            max_seconds=self.budget_max_seconds,
        )


@dataclass(slots=True)
class ObjectiveTemplate:
    objective_id: str
    group_id: str
    title: str
    description: str
    success_metrics: tuple[str, ...] = ()
    hard_constraints: tuple[str, ...] = ()
    global_budget: dict[str, Any] = field(default_factory=dict)
    workstreams: tuple[WorkstreamTemplate, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "objective_id": self.objective_id,
            "group_id": self.group_id,
            "title": self.title,
            "description": self.description,
            "success_metrics": list(self.success_metrics),
            "hard_constraints": list(self.hard_constraints),
            "global_budget": dict(self.global_budget),
            "workstreams": [workstream.to_dict() for workstream in self.workstreams],
            "metadata": dict(self.metadata),
        }


@dataclass(slots=True)
class PlanningContext:
    reason: str = ""
    additional_workstreams: tuple[WorkstreamTemplate, ...] = ()
    supersede_node_ids: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class PlanningResult:
    objective: ObjectiveSpec
    leader_tasks: tuple[LeaderTaskCard, ...]
    spec_nodes: tuple[SpecNode, ...]
    spec_edges: tuple[SpecEdge, ...]
