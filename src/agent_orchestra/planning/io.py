from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agent_orchestra.planning.template import ObjectiveTemplate, WorkstreamTemplate


def _workstream_from_dict(data: dict[str, Any]) -> WorkstreamTemplate:
    budget = data.get("budget", {})
    if not isinstance(budget, dict):
        raise ValueError("Workstream budget must be an object when provided.")
    if "max_concurrency" in budget:
        raise ValueError("Workstream budget.max_concurrency is no longer supported; use max_teammates only.")
    return WorkstreamTemplate(
        workstream_id=data["workstream_id"],
        title=data["title"],
        summary=data["summary"],
        team_name=data["team_name"],
        depends_on=tuple(data.get("depends_on", [])),
        acceptance_checks=tuple(data.get("acceptance_checks", [])),
        budget_max_teammates=int(budget.get("max_teammates", 0)),
        budget_max_iterations=int(budget.get("max_iterations", 0)),
        budget_max_tokens=budget.get("max_tokens"),
        budget_max_seconds=budget.get("max_seconds"),
        metadata=dict(data.get("metadata", {})),
    )


def objective_template_from_dict(data: dict[str, Any]) -> ObjectiveTemplate:
    required = ("objective_id", "group_id", "title", "description", "workstreams")
    missing = [name for name in required if name not in data]
    if missing:
        raise ValueError(f"Objective template is missing required fields: {', '.join(missing)}")
    return ObjectiveTemplate(
        objective_id=data["objective_id"],
        group_id=data["group_id"],
        title=data["title"],
        description=data["description"],
        success_metrics=tuple(data.get("success_metrics", [])),
        hard_constraints=tuple(data.get("hard_constraints", [])),
        global_budget=dict(data.get("global_budget", {})),
        workstreams=tuple(_workstream_from_dict(item) for item in data.get("workstreams", [])),
        metadata=dict(data.get("metadata", {})),
    )


def render_objective_template(
    *,
    objective_id: str = "objective-demo",
    group_id: str = "group-demo",
) -> str:
    template = ObjectiveTemplate(
        objective_id=objective_id,
        group_id=group_id,
        title="Describe the overall objective",
        description="Describe the target outcome, constraints, and delivery context.",
        success_metrics=("Define measurable success criteria",),
        hard_constraints=("List non-negotiable constraints",),
        global_budget={"max_teams": 2, "max_iterations": 3},
        workstreams=(
            WorkstreamTemplate(
                workstream_id="workstream-1",
                title="Primary workstream",
                summary="Describe the leader lane for this workstream.",
                team_name="Team One",
                acceptance_checks=("Add at least one acceptance check",),
                budget_max_teammates=2,
                budget_max_iterations=3,
            ),
        ),
    )
    return json.dumps(template.to_dict(), indent=2)


def write_objective_template(
    path: str | Path,
    *,
    objective_id: str = "objective-demo",
    group_id: str = "group-demo",
) -> Path:
    target = Path(path)
    target.write_text(
        render_objective_template(objective_id=objective_id, group_id=group_id),
        encoding="utf-8",
    )
    return target


def load_objective_template(path: str | Path) -> ObjectiveTemplate:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Objective template root must be an object.")
    return objective_template_from_dict(payload)
