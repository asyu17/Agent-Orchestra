from __future__ import annotations

from agent_orchestra.contracts.enums import SpecEdgeKind, SpecNodeKind, SpecNodeStatus, TaskScope
from agent_orchestra.contracts.execution import LeaderTaskCard, Planner
from agent_orchestra.contracts.objective import ObjectiveSpec, SpecEdge, SpecNode
from agent_orchestra.planning.template import ObjectiveTemplate, PlanningContext, PlanningResult, WorkstreamTemplate


def _objective_node_id(objective_id: str) -> str:
    return f"{objective_id}:objective"


def _leader_node_id(objective_id: str, workstream_id: str) -> str:
    return f"{objective_id}:leader:{workstream_id}"


def _leader_task_id(objective_id: str, workstream_id: str) -> str:
    return f"{objective_id}:leader-task:{workstream_id}"


def _decompose_edge_id(objective_id: str, workstream_id: str) -> str:
    return f"{objective_id}:edge:decomposes:{workstream_id}"


def _depends_edge_id(objective_id: str, workstream_id: str, dependency_id: str) -> str:
    return f"{objective_id}:edge:depends:{workstream_id}:{dependency_id}"


def _supersedes_edge_id(objective_id: str, new_node_id: str, old_node_id: str) -> str:
    return f"{objective_id}:edge:supersedes:{new_node_id}:{old_node_id}"


class TemplatePlanner(Planner):
    async def build_initial_plan(self, objective: ObjectiveTemplate) -> PlanningResult:
        self._validate_template(objective)
        return self._compile(objective=objective, planning_context=None)

    async def replan(self, objective: ObjectiveTemplate, context: PlanningContext) -> PlanningResult:
        merged = ObjectiveTemplate(
            objective_id=objective.objective_id,
            group_id=objective.group_id,
            title=objective.title,
            description=objective.description,
            success_metrics=objective.success_metrics,
            hard_constraints=objective.hard_constraints,
            global_budget=dict(objective.global_budget),
            workstreams=objective.workstreams + context.additional_workstreams,
            metadata=dict(objective.metadata),
        )
        self._validate_template(merged)
        return self._compile(objective=merged, planning_context=context)

    def _compile(
        self,
        *,
        objective: ObjectiveTemplate,
        planning_context: PlanningContext | None,
    ) -> PlanningResult:
        objective_spec = ObjectiveSpec(
            objective_id=objective.objective_id,
            group_id=objective.group_id,
            title=objective.title,
            description=objective.description,
            success_metrics=objective.success_metrics,
            hard_constraints=objective.hard_constraints,
            budget=dict(objective.global_budget),
            metadata=dict(objective.metadata),
        )

        root_node = SpecNode(
            node_id=_objective_node_id(objective.objective_id),
            objective_id=objective.objective_id,
            kind=SpecNodeKind.OBJECTIVE,
            title=objective.title,
            summary=objective.description,
            scope=TaskScope.OBJECTIVE,
            created_by="template_planner",
            status=SpecNodeStatus.OPEN,
            metadata=dict(objective.metadata),
        )

        leader_tasks: list[LeaderTaskCard] = []
        spec_nodes: list[SpecNode] = [root_node]
        spec_edges: list[SpecEdge] = []
        workstreams_by_id = {workstream.workstream_id: workstream for workstream in objective.workstreams}

        for workstream in objective.workstreams:
            node_id = _leader_node_id(objective.objective_id, workstream.workstream_id)
            workstream_metadata = dict(workstream.metadata)
            leader_task_reason = planning_context.reason if planning_context is not None else ""
            leader_tasks.append(
                LeaderTaskCard(
                    task_id=_leader_task_id(objective.objective_id, workstream.workstream_id),
                    objective_id=objective.objective_id,
                    leader_id=f"leader:{workstream.workstream_id}",
                    title=workstream.title,
                    summary=workstream.summary,
                    budget=workstream.to_budget(),
                    metadata={
                        **workstream_metadata,
                        "team_name": workstream.team_name,
                        "acceptance_checks": list(workstream.acceptance_checks),
                        "reason": leader_task_reason,
                    },
                )
            )
            spec_nodes.append(
                SpecNode(
                    node_id=node_id,
                    objective_id=objective.objective_id,
                    kind=SpecNodeKind.LEADER_TASK,
                    title=workstream.title,
                    summary=workstream.summary,
                    scope=TaskScope.LEADER_LANE,
                    lane_id=workstream.workstream_id,
                    created_by="template_planner",
                    status=SpecNodeStatus.OPEN,
                    metadata={**workstream_metadata, "team_name": workstream.team_name},
                )
            )
            spec_edges.append(
                SpecEdge(
                    edge_id=_decompose_edge_id(objective.objective_id, workstream.workstream_id),
                    objective_id=objective.objective_id,
                    kind=SpecEdgeKind.DECOMPOSES_TO,
                    from_node_id=root_node.node_id,
                    to_node_id=node_id,
                )
            )

        for workstream in objective.workstreams:
            current_node_id = _leader_node_id(objective.objective_id, workstream.workstream_id)
            for dependency_id in workstream.depends_on:
                if dependency_id not in workstreams_by_id:
                    raise ValueError(f"Unknown workstream dependency: {dependency_id}")
                spec_edges.append(
                    SpecEdge(
                        edge_id=_depends_edge_id(objective.objective_id, workstream.workstream_id, dependency_id),
                        objective_id=objective.objective_id,
                        kind=SpecEdgeKind.DEPENDS_ON,
                        from_node_id=current_node_id,
                        to_node_id=_leader_node_id(objective.objective_id, dependency_id),
                    )
                )

        if planning_context is not None and planning_context.supersede_node_ids:
            if not planning_context.additional_workstreams:
                raise ValueError("supersede_node_ids require at least one additional workstream")
            replacement_node_id = _leader_node_id(
                objective.objective_id,
                planning_context.additional_workstreams[0].workstream_id,
            )
            for superseded in planning_context.supersede_node_ids:
                spec_edges.append(
                    SpecEdge(
                        edge_id=_supersedes_edge_id(objective.objective_id, replacement_node_id, superseded),
                        objective_id=objective.objective_id,
                        kind=SpecEdgeKind.SUPERSEDES,
                        from_node_id=replacement_node_id,
                        to_node_id=superseded,
                    )
                )

        return PlanningResult(
            objective=objective_spec,
            leader_tasks=tuple(leader_tasks),
            spec_nodes=tuple(spec_nodes),
            spec_edges=tuple(spec_edges),
        )

    def _validate_template(self, objective: ObjectiveTemplate) -> None:
        if not objective.workstreams:
            raise ValueError("Objective template must define at least one workstream")
        seen: set[str] = set()
        for workstream in objective.workstreams:
            self._validate_workstream(workstream)
            if workstream.workstream_id in seen:
                raise ValueError(f"Duplicate workstream_id: {workstream.workstream_id}")
            seen.add(workstream.workstream_id)

    def _validate_workstream(self, workstream: WorkstreamTemplate) -> None:
        if not workstream.workstream_id:
            raise ValueError("workstream_id is required")
        if not workstream.title:
            raise ValueError(f"Workstream {workstream.workstream_id} must have a title")
        if not workstream.summary:
            raise ValueError(f"Workstream {workstream.workstream_id} must have a summary")
        if not workstream.team_name:
            raise ValueError(f"Workstream {workstream.workstream_id} must have a team_name")
