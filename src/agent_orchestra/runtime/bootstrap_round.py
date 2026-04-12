from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_orchestra.contracts.blackboard import BlackboardEntryKind, BlackboardKind
from agent_orchestra.contracts.execution import LeaderTaskCard, WorkerAssignment
from agent_orchestra.contracts.objective import ObjectiveSpec
from agent_orchestra.contracts.task import TaskCard
from agent_orchestra.contracts.team import Team
from agent_orchestra.contracts.enums import TaskScope
from agent_orchestra.contracts.worker_protocol import WorkerRoleProfile
from agent_orchestra.planning.template import PlanningResult
from agent_orchestra.runtime.group_runtime import GroupRuntime


def _ordered_unique_strings(values: object) -> tuple[str, ...]:
    if not isinstance(values, (list, tuple)):
        return ()
    seen: set[str] = set()
    ordered: list[str] = []
    for item in values:
        if not isinstance(item, str):
            continue
        normalized = item.strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        ordered.append(normalized)
    return tuple(ordered)


def _leader_lane_defaults(leader_task: LeaderTaskCard) -> dict[str, object]:
    metadata = leader_task.metadata if isinstance(leader_task.metadata, dict) else {}
    lane_defaults: dict[str, object] = {}
    for key in ("gap_id", "rationale", "source_path"):
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            lane_defaults[key] = value.strip()
    owned_paths = _ordered_unique_strings(metadata.get("owned_paths"))
    if owned_paths:
        lane_defaults["owned_paths"] = owned_paths
    verification_commands = _ordered_unique_strings(metadata.get("verification_commands"))
    if verification_commands:
        lane_defaults["verification_commands"] = verification_commands
    return lane_defaults


def _lane_defaults_metadata_payload(lane_defaults: dict[str, object]) -> dict[str, object]:
    payload: dict[str, object] = {}
    gap_id = lane_defaults.get("gap_id")
    if isinstance(gap_id, str) and gap_id:
        payload["gap_id"] = gap_id
    rationale = lane_defaults.get("rationale")
    if isinstance(rationale, str) and rationale:
        payload["rationale"] = rationale
    source_path = lane_defaults.get("source_path")
    if isinstance(source_path, str) and source_path:
        payload["source_path"] = source_path
    owned_paths = lane_defaults.get("owned_paths")
    if isinstance(owned_paths, tuple) and owned_paths:
        payload["default_owned_paths"] = list(owned_paths)
    verification_commands = lane_defaults.get("verification_commands")
    if isinstance(verification_commands, tuple) and verification_commands:
        payload["default_verification_commands"] = list(verification_commands)
    return payload


def _lane_id_for_leader_task(leader_task: LeaderTaskCard) -> str:
    if leader_task.leader_id.startswith("leader:"):
        return leader_task.leader_id.split(":", 1)[1]
    lane_id = leader_task.metadata.get("lane_id")
    if isinstance(lane_id, str) and lane_id:
        return lane_id
    return leader_task.task_id.rsplit(":", 1)[-1]


def _team_id(group_id: str, lane_id: str) -> str:
    return f"{group_id}:team:{lane_id}"


def _budget_payload(leader_task: LeaderTaskCard) -> dict[str, Any]:
    return {
        "max_teammates": leader_task.budget.max_teammates,
        "max_iterations": leader_task.budget.max_iterations,
        "max_tokens": leader_task.budget.max_tokens,
        "max_seconds": leader_task.budget.max_seconds,
    }


def _directive_payload(
    objective: ObjectiveSpec,
    leader_task: LeaderTaskCard,
    *,
    lane_id: str,
    team_id: str,
    team_name: str,
) -> dict[str, object]:
    lane_defaults = _leader_lane_defaults(leader_task)
    return {
        "objective_id": objective.objective_id,
        "objective_title": objective.title,
        "objective_description": objective.description,
        "success_metrics": list(objective.success_metrics),
        "hard_constraints": list(objective.hard_constraints),
        "leader_id": leader_task.leader_id,
        "leader_task_id": leader_task.task_id,
        "leader_task_title": leader_task.title,
        "leader_task_summary": leader_task.summary,
        "lane_id": lane_id,
        "team_id": team_id,
        "team_name": team_name,
        "acceptance_checks": list(leader_task.metadata.get("acceptance_checks", [])),
        "budget": _budget_payload(leader_task),
        **_lane_defaults_metadata_payload(lane_defaults),
    }


def build_leader_instructions(objective: ObjectiveSpec, leader_round: "LeaderRound") -> str:
    acceptance_checks = leader_round.leader_task.metadata.get("acceptance_checks", [])
    checks_text = ", ".join(str(item) for item in acceptance_checks) or "None"
    lane_defaults = _leader_lane_defaults(leader_round.leader_task)
    lines = [
        f"You are {leader_round.leader_task.leader_id}, the leader for lane `{leader_round.lane_id}`.",
        f"Objective: {objective.title}",
        f"Objective description: {objective.description}",
        f"Leader task: {leader_round.leader_task.title}",
        f"Leader task summary: {leader_round.leader_task.summary}",
        f"Team: {leader_round.team_name} ({leader_round.team_id})",
        f"Acceptance checks: {checks_text}",
    ]
    if lane_defaults:
        lines.extend(["Lane defaults:"])
        gap_id = lane_defaults.get("gap_id")
        if isinstance(gap_id, str) and gap_id:
            lines.append(f"- gap_id: {gap_id}")
        rationale = lane_defaults.get("rationale")
        if isinstance(rationale, str) and rationale:
            lines.append(f"- rationale: {rationale}")
        source_path = lane_defaults.get("source_path")
        if isinstance(source_path, str) and source_path:
            lines.append(f"- source_path: {source_path}")
        owned_paths = lane_defaults.get("owned_paths", ())
        if isinstance(owned_paths, tuple) and owned_paths:
            lines.append(f"- default_owned_paths: {', '.join(owned_paths)}")
        verification_commands = lane_defaults.get("verification_commands", ())
        if isinstance(verification_commands, tuple) and verification_commands:
            lines.append(f"- default_verification_commands: {', '.join(verification_commands)}")
        lines.append(
            "- Runtime will merge lane-owned scope into descendant team tasks; selected gaps may also inherit default verification commands when omitted."
        )
    lines.extend(
        [
            "Budget:",
            f"- max_teammates: {leader_round.leader_task.budget.max_teammates}",
            f"- max_iterations: {leader_round.leader_task.budget.max_iterations}",
            f"- max_tokens: {leader_round.leader_task.budget.max_tokens}",
            f"- max_seconds: {leader_round.leader_task.budget.max_seconds}",
            "Rules:",
            "- Do not rewrite the global objective.",
            "- Work only within your lane and team scope.",
            "- If you discover new team-scope work, create team tasks with derived_from and reason.",
            "- If you discover cross-scope work, report it as a proposal or blocker instead of mutating upper scopes.",
            "Return a JSON object with this shape:",
            (
                '{ "summary": "...", '
                '"sequential_slices": ['
                '{ "slice_id": "...", "title": "...", "goal": "...", "reason": "...", '
                '"scope": "team", "depends_on": ["..."], "owned_paths": ["..."], "verification_commands": ["..."] }'
                "], "
                '"parallel_slices": ['
                '{ "parallel_group": "...", "slices": ['
                '{ "slice_id": "...", "title": "...", "goal": "...", "reason": "...", '
                '"scope": "team", "depends_on": ["..."], "owned_paths": ["..."], "verification_commands": ["..."] }'
                "] }"
                "] }"
            ),
            "Use sequential_slices for dependency-ordered work and parallel_slices for same-batch work.",
        ]
    )
    return "\n".join(lines)


def compile_leader_assignment(
    objective: ObjectiveSpec,
    leader_round: "LeaderRound",
    *,
    iteration: int | None = None,
    turn_index: int | None = None,
    backend: str = "in_process",
    input_text: str | None = None,
    working_dir: str | None = None,
    environment: dict[str, str] | None = None,
    previous_response_id: str | None = None,
    extra_metadata: dict[str, object] | None = None,
    role_profile: WorkerRoleProfile | None = None,
) -> WorkerAssignment:
    effective_turn = turn_index if turn_index is not None else iteration
    if effective_turn is None:
        effective_turn = 1
    cwd = working_dir or str(Path.cwd())
    lane_defaults = _leader_lane_defaults(leader_round.leader_task)
    metadata = {
        "team_name": leader_round.team_name,
        "leader_task_id": leader_round.leader_task.task_id,
        "leader_lane_directive_entry_id": leader_round.leader_lane_directive_entry_id,
        "team_directive_entry_id": leader_round.team_directive_entry_id,
        "structured_output": "json_only",
        "acceptance_checks": list(leader_round.leader_task.metadata.get("acceptance_checks", [])),
        "budget": _budget_payload(leader_round.leader_task),
        "iteration": effective_turn,
        "turn_index": effective_turn,
        **_lane_defaults_metadata_payload(lane_defaults),
    }
    if extra_metadata:
        metadata.update(extra_metadata)
    if role_profile is not None:
        metadata["role_profile_id"] = role_profile.profile_id
    return WorkerAssignment(
        assignment_id=f"{leader_round.runtime_task.task_id}:leader-turn-{effective_turn}",
        worker_id=leader_round.leader_task.leader_id,
        group_id=objective.group_id,
        objective_id=objective.objective_id,
        team_id=leader_round.team_id,
        lane_id=leader_round.lane_id,
        task_id=leader_round.runtime_task.task_id,
        role="leader",
        backend=backend,
        instructions=build_leader_instructions(objective, leader_round),
        input_text=input_text or "Start your first leader coordination turn.",
        previous_response_id=previous_response_id,
        working_dir=cwd,
        environment=dict(environment or {}),
        metadata=metadata,
        execution_contract=role_profile.execution_contract if role_profile is not None else None,
        lease_policy=role_profile.lease_policy if role_profile is not None else None,
        role_profile=role_profile,
    )


@dataclass(slots=True)
class LeaderRound:
    lane_id: str
    team_id: str
    team_name: str
    leader_task: LeaderTaskCard
    runtime_task: TaskCard
    leader_lane_directive_entry_id: str
    team_directive_entry_id: str

    def to_dict(self) -> dict[str, object]:
        return {
            "lane_id": self.lane_id,
            "team_id": self.team_id,
            "team_name": self.team_name,
            "leader_id": self.leader_task.leader_id,
            "leader_task_id": self.leader_task.task_id,
            "runtime_task_id": self.runtime_task.task_id,
            "leader_lane_directive_entry_id": self.leader_lane_directive_entry_id,
            "team_directive_entry_id": self.team_directive_entry_id,
        }


@dataclass(slots=True)
class HybridTeamRound:
    objective: ObjectiveSpec
    teams: tuple[Team, ...]
    leader_rounds: tuple[LeaderRound, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "objective_id": self.objective.objective_id,
            "group_id": self.objective.group_id,
            "title": self.objective.title,
            "teams": [
                {"team_id": team.team_id, "name": team.name, "member_ids": list(team.member_ids)}
                for team in self.teams
            ],
            "leader_rounds": [leader_round.to_dict() for leader_round in self.leader_rounds],
        }


async def materialize_planning_result(
    runtime: GroupRuntime,
    result: PlanningResult,
    *,
    created_by: str = "superleader.bootstrap",
) -> HybridTeamRound:
    await runtime.apply_planning_result(result)

    teams: list[Team] = []
    leader_rounds: list[LeaderRound] = []

    for leader_task in result.leader_tasks:
        lane_id = _lane_id_for_leader_task(leader_task)
        team_name = str(leader_task.metadata.get("team_name") or lane_id)
        team_id = _team_id(result.objective.group_id, lane_id)

        team = await runtime.store.get_team(team_id)
        if team is None:
            team = await runtime.create_team(
                group_id=result.objective.group_id,
                team_id=team_id,
                name=team_name,
                member_ids=(leader_task.leader_id,),
            )
        elif leader_task.leader_id not in team.member_ids:
            team.member_ids = team.member_ids + (leader_task.leader_id,)
            await runtime.store.save_team(team)

        teams.append(team)

        existing_lane_tasks = await runtime.store.list_tasks(
            result.objective.group_id,
            team_id=team_id,
            lane_id=lane_id,
            scope=TaskScope.LEADER_LANE.value,
        )
        runtime_task = next((task for task in existing_lane_tasks if task.derived_from == leader_task.task_id), None)
        if runtime_task is None:
            runtime_task = await runtime.submit_task(
                group_id=result.objective.group_id,
                team_id=team_id,
                goal=f"{leader_task.title}: {leader_task.summary}",
                scope=TaskScope.LEADER_LANE,
                lane_id=lane_id,
                owned_paths=_ordered_unique_strings(leader_task.metadata.get("owned_paths")),
                created_by=created_by,
                derived_from=leader_task.task_id,
                reason=str(leader_task.metadata.get("reason") or f"Bootstrap leader round for {leader_task.title}."),
                verification_commands=_ordered_unique_strings(leader_task.metadata.get("verification_commands")),
            )

        directive_payload = _directive_payload(
            result.objective,
            leader_task,
            lane_id=lane_id,
            team_id=team_id,
            team_name=team_name,
        )
        lane_directive = await runtime.append_blackboard_entry(
            group_id=result.objective.group_id,
            kind=BlackboardKind.LEADER_LANE,
            entry_kind=BlackboardEntryKind.DIRECTIVE,
            author_id=created_by,
            lane_id=lane_id,
            summary=f"Bootstrap leader directive for {leader_task.title}.",
            payload=directive_payload,
        )
        team_directive = await runtime.append_blackboard_entry(
            group_id=result.objective.group_id,
            kind=BlackboardKind.TEAM,
            entry_kind=BlackboardEntryKind.DIRECTIVE,
            author_id=created_by,
            lane_id=lane_id,
            team_id=team_id,
            summary=f"Bootstrap team directive for {leader_task.title}.",
            payload=directive_payload,
        )
        leader_rounds.append(
            LeaderRound(
                lane_id=lane_id,
                team_id=team_id,
                team_name=team_name,
                leader_task=leader_task,
                runtime_task=runtime_task,
                leader_lane_directive_entry_id=lane_directive.entry_id,
                team_directive_entry_id=team_directive.entry_id,
            )
        )

    unique_teams: dict[str, Team] = {team.team_id: team for team in teams}
    return HybridTeamRound(
        objective=result.objective,
        teams=tuple(unique_teams.values()),
        leader_rounds=tuple(leader_rounds),
    )


def compile_leader_assignments(
    round_bundle: HybridTeamRound,
    *,
    backend: str = "in_process",
    working_dir: str | None = None,
    environment: dict[str, str] | None = None,
) -> tuple[WorkerAssignment, ...]:
    assignments: list[WorkerAssignment] = []
    for leader_round in round_bundle.leader_rounds:
        assignments.append(
            compile_leader_assignment(
                round_bundle.objective,
                leader_round,
                iteration=1,
                backend=backend,
                input_text="Start your first leader coordination turn.",
                working_dir=working_dir,
                environment=environment,
            )
        )
    return tuple(assignments)
