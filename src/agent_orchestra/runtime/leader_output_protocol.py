from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from agent_orchestra.contracts.blackboard import BlackboardEntryKind, BlackboardKind
from agent_orchestra.contracts.enums import TaskScope
from agent_orchestra.contracts.execution import WorkerAssignment
from agent_orchestra.contracts.objective import ObjectiveSpec
from agent_orchestra.contracts.task import TaskCard
from agent_orchestra.contracts.worker_protocol import WorkerRoleProfile
from agent_orchestra.runtime.bootstrap_round import LeaderRound
from agent_orchestra.runtime.group_runtime import GroupRuntime

_SLICE_MODE_SEQUENTIAL = "sequential"
_SLICE_MODE_PARALLEL = "parallel"
_GAP_IDS_WITH_DEFAULT_VERIFICATION_MERGE = frozenset(
    {
        "team-primary-semantics-switch",
        "coordination-transaction-and-session-truth-convergence",
        "task-surface-authority-contract",
        "superleader-isomorphic-runtime",
    }
)


@dataclass(slots=True)
class ProposedTeammateTask:
    slice_id: str
    title: str
    goal: str
    reason: str
    scope: TaskScope = TaskScope.TEAM
    depends_on: tuple[str, ...] = field(default_factory=tuple)
    parallel_group: str | None = None
    slice_mode: str = _SLICE_MODE_SEQUENTIAL
    owned_paths: tuple[str, ...] = field(default_factory=tuple)
    verification_commands: tuple[str, ...] = field(default_factory=tuple)


@dataclass(slots=True)
class LeaderTurnOutput:
    summary: str
    teammate_tasks: tuple[ProposedTeammateTask, ...]


@dataclass(slots=True)
class IngestedLeaderTurn:
    parsed_output: LeaderTurnOutput
    created_tasks: tuple[TaskCard, ...]
    teammate_assignments: tuple[WorkerAssignment, ...]
    execution_report_entry_id: str
    proposal_entry_ids: tuple[str, ...] = ()
    blocker_entry_ids: tuple[str, ...] = ()


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


def _merge_string_tuples(*values: object) -> tuple[str, ...]:
    merged: list[str] = []
    seen: set[str] = set()
    for value in values:
        for item in _ordered_unique_strings(value):
            if item in seen:
                continue
            seen.add(item)
            merged.append(item)
    return tuple(merged)


def _leader_lane_defaults(leader_round: LeaderRound) -> dict[str, object]:
    metadata = leader_round.leader_task.metadata if isinstance(leader_round.leader_task.metadata, dict) else {}
    defaults: dict[str, object] = {
        "owned_paths": _ordered_unique_strings(metadata.get("owned_paths")),
        "verification_commands": _ordered_unique_strings(metadata.get("verification_commands")),
    }
    gap_id = metadata.get("gap_id")
    if isinstance(gap_id, str) and gap_id.strip():
        defaults["gap_id"] = gap_id.strip()
    return defaults


def _merge_lane_defaults(
    proposed_task: ProposedTeammateTask,
    *,
    leader_round: LeaderRound,
) -> ProposedTeammateTask:
    lane_defaults = _leader_lane_defaults(leader_round)
    gap_id = lane_defaults.get("gap_id")
    merge_verification_defaults = (
        isinstance(gap_id, str)
        and gap_id in _GAP_IDS_WITH_DEFAULT_VERIFICATION_MERGE
    )
    return replace(
        proposed_task,
        owned_paths=_merge_string_tuples(
            lane_defaults.get("owned_paths", ()),
            proposed_task.owned_paths,
        ),
        verification_commands=(
            _merge_string_tuples(
                lane_defaults.get("verification_commands", ()),
                proposed_task.verification_commands,
            )
            if merge_verification_defaults
            else proposed_task.verification_commands
        ),
    )


def _required_text_field(mapping: dict[str, Any], field: str, *, location: str) -> str:
    value = mapping.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{location}.{field} must be a non-empty string")
    return value.strip()


def _optional_str_tuple(mapping: dict[str, Any], field: str, *, location: str) -> tuple[str, ...]:
    value = mapping.get(field)
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError(f"{location}.{field} must be a list of strings")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"{location}.{field} must contain non-empty strings")
        result.append(item.strip())
    return tuple(result)


def _optional_slice_dependencies(mapping: dict[str, Any], *, location: str) -> tuple[str, ...]:
    value = mapping.get("depends_on")
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError(f"{location}.depends_on must be a list of slice ids")
    dependencies: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"{location}.depends_on must contain non-empty slice ids")
        dependencies.append(item.strip())
    return tuple(dependencies)


def _optional_parallel_group(mapping: dict[str, Any], *, location: str) -> str | None:
    value = mapping.get("parallel_group")
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{location}.parallel_group must be a non-empty string when provided")
    return value.strip()


def _optional_slice_mode(mapping: dict[str, Any], *, location: str) -> str | None:
    value = mapping.get("slice_mode")
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{location}.slice_mode must be a non-empty string when provided")
    normalized = value.strip()
    if normalized not in {_SLICE_MODE_SEQUENTIAL, _SLICE_MODE_PARALLEL}:
        raise ValueError(f"{location}.slice_mode must be '{_SLICE_MODE_SEQUENTIAL}' or '{_SLICE_MODE_PARALLEL}'")
    return normalized


def _parse_teammate_task(
    raw_task: Any,
    *,
    location: str,
    default_slice_id: str,
    default_depends_on: tuple[str, ...] = (),
    default_parallel_group: str | None = None,
    default_slice_mode: str = _SLICE_MODE_SEQUENTIAL,
    require_slice_id: bool = False,
) -> ProposedTeammateTask:
    if not isinstance(raw_task, dict):
        raise ValueError(f"{location} must be an object")
    title = _required_text_field(raw_task, "title", location=location)
    goal = _required_text_field(raw_task, "goal", location=location)
    reason = _required_text_field(raw_task, "reason", location=location)

    raw_scope = raw_task.get("scope")
    if raw_scope is None:
        scope = TaskScope.TEAM
    elif isinstance(raw_scope, str):
        scope_value = raw_scope.strip()
        if scope_value != TaskScope.TEAM.value:
            raise ValueError(f"{location}.scope must be absent or '{TaskScope.TEAM.value}'")
        scope = TaskScope.TEAM
    else:
        raise ValueError(f"{location}.scope must be absent or '{TaskScope.TEAM.value}'")

    raw_slice_id = raw_task.get("slice_id")
    if raw_slice_id is None:
        if require_slice_id:
            raise ValueError(f"{location}.slice_id must be a non-empty string")
        slice_id = default_slice_id
    elif isinstance(raw_slice_id, str) and raw_slice_id.strip():
        slice_id = raw_slice_id.strip()
    else:
        raise ValueError(f"{location}.slice_id must be a non-empty string")

    depends_on = _optional_slice_dependencies(raw_task, location=location) or default_depends_on
    parallel_group = _optional_parallel_group(raw_task, location=location) or default_parallel_group
    slice_mode = _optional_slice_mode(raw_task, location=location) or default_slice_mode

    return ProposedTeammateTask(
        slice_id=slice_id,
        title=title,
        goal=goal,
        reason=reason,
        scope=scope,
        depends_on=depends_on,
        parallel_group=parallel_group,
        slice_mode=slice_mode,
        owned_paths=_optional_str_tuple(raw_task, "owned_paths", location=location),
        verification_commands=_optional_str_tuple(raw_task, "verification_commands", location=location),
    )


def parse_leader_turn_output(payload: str | dict[str, Any]) -> LeaderTurnOutput:
    if isinstance(payload, str):
        try:
            raw_payload = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ValueError("Leader output is not valid JSON") from exc
    elif isinstance(payload, dict):
        raw_payload = payload
    else:
        raise ValueError("Leader output payload must be a JSON string or object")

    if not isinstance(raw_payload, dict):
        raise ValueError("Leader output payload must be an object")

    summary = _required_text_field(raw_payload, "summary", location="leader output")

    teammate_tasks: list[ProposedTeammateTask] = []

    sequential_slices_raw = raw_payload.get("sequential_slices")
    if sequential_slices_raw is not None:
        if not isinstance(sequential_slices_raw, list):
            raise ValueError("Leader output.sequential_slices must be a list when provided")
        previous_slice_id: str | None = None
        for index, raw_slice in enumerate(sequential_slices_raw):
            default_depends_on = (previous_slice_id,) if previous_slice_id else ()
            task = _parse_teammate_task(
                raw_slice,
                location=f"sequential_slices[{index}]",
                default_slice_id=f"sequential-slice-{index + 1}",
                default_depends_on=default_depends_on,
                default_slice_mode=_SLICE_MODE_SEQUENTIAL,
                require_slice_id=True,
            )
            if task.parallel_group is not None:
                raise ValueError(f"sequential_slices[{index}] must not define parallel_group")
            if task.slice_mode != _SLICE_MODE_SEQUENTIAL:
                raise ValueError(f"sequential_slices[{index}].slice_mode must be '{_SLICE_MODE_SEQUENTIAL}'")
            teammate_tasks.append(task)
            previous_slice_id = task.slice_id

    parallel_slices_raw = raw_payload.get("parallel_slices")
    if parallel_slices_raw is not None:
        if not isinstance(parallel_slices_raw, list):
            raise ValueError("Leader output.parallel_slices must be a list when provided")
        for group_index, raw_parallel_group in enumerate(parallel_slices_raw):
            if not isinstance(raw_parallel_group, dict):
                raise ValueError(f"parallel_slices[{group_index}] must be an object")
            parallel_group = _required_text_field(
                raw_parallel_group,
                "parallel_group",
                location=f"parallel_slices[{group_index}]",
            )
            raw_group_slices = raw_parallel_group.get("slices")
            if not isinstance(raw_group_slices, list):
                raise ValueError(f"parallel_slices[{group_index}].slices must be a list")
            for slice_index, raw_slice in enumerate(raw_group_slices):
                task = _parse_teammate_task(
                    raw_slice,
                    location=f"parallel_slices[{group_index}].slices[{slice_index}]",
                    default_slice_id=f"{parallel_group}-slice-{slice_index + 1}",
                    default_parallel_group=parallel_group,
                    default_slice_mode=_SLICE_MODE_PARALLEL,
                    require_slice_id=True,
                )
                if task.parallel_group != parallel_group:
                    raise ValueError(
                        f"parallel_slices[{group_index}].slices[{slice_index}].parallel_group must be '{parallel_group}'"
                    )
                if task.slice_mode != _SLICE_MODE_PARALLEL:
                    raise ValueError(
                        f"parallel_slices[{group_index}].slices[{slice_index}].slice_mode must be '{_SLICE_MODE_PARALLEL}'"
                    )
                teammate_tasks.append(task)

    if "teammate_tasks" in raw_payload:
        raise ValueError(
            "Leader output.teammate_tasks is no longer supported; use sequential_slices and parallel_slices."
        )

    if (
        not teammate_tasks
        and sequential_slices_raw is None
        and parallel_slices_raw is None
    ):
        raise ValueError(
            "Leader output must include sequential_slices or parallel_slices."
        )

    slice_id_to_task: dict[str, ProposedTeammateTask] = {}
    for task in teammate_tasks:
        if task.slice_id in slice_id_to_task:
            raise ValueError(f"Duplicate slice_id in leader output: {task.slice_id}")
        slice_id_to_task[task.slice_id] = task

    for task in teammate_tasks:
        for dependency in task.depends_on:
            if dependency == task.slice_id:
                raise ValueError(f"Slice {task.slice_id} cannot depend on itself")
            if dependency not in slice_id_to_task:
                raise ValueError(f"Slice {task.slice_id} depends on unknown slice_id: {dependency}")

    return LeaderTurnOutput(summary=summary, teammate_tasks=tuple(teammate_tasks))


def _build_teammate_assignment(
    *,
    objective: ObjectiveSpec,
    leader_round: LeaderRound,
    created_task: TaskCard,
    proposed_task: ProposedTeammateTask,
    teammate_index: int,
    backend: str,
    working_dir: str,
    depends_on_task_ids: tuple[str, ...] = (),
    role_profile: WorkerRoleProfile | None = None,
) -> WorkerAssignment:
    return WorkerAssignment(
        assignment_id=f"{created_task.task_id}:teammate-turn-1",
        worker_id=f"{leader_round.team_id}:teammate:{teammate_index}",
        group_id=objective.group_id,
        objective_id=objective.objective_id,
        team_id=leader_round.team_id,
        lane_id=leader_round.lane_id,
        task_id=created_task.task_id,
        role="teammate",
        backend=backend,
        instructions="\n".join(
            [
                f"You are teammate {teammate_index} in team `{leader_round.team_name}` ({leader_round.team_id}).",
                f"Task title: {proposed_task.title}",
                f"Task goal: {proposed_task.goal}",
                f"Task reason: {proposed_task.reason}",
            ]
        ),
        input_text=f"Execute task: {proposed_task.goal}",
        working_dir=working_dir,
        metadata={
            "title": proposed_task.title,
            "reason": proposed_task.reason,
            "derived_from": leader_round.runtime_task.task_id,
            "slice_id": proposed_task.slice_id,
            "slice_mode": proposed_task.slice_mode,
            "depends_on": list(proposed_task.depends_on),
            "depends_on_task_ids": list(depends_on_task_ids),
            "parallel_group": proposed_task.parallel_group,
            "owned_paths": list(proposed_task.owned_paths),
            "verification_commands": list(proposed_task.verification_commands),
            **({"role_profile_id": role_profile.profile_id} if role_profile is not None else {}),
        },
        execution_contract=role_profile.execution_contract if role_profile is not None else None,
        lease_policy=role_profile.lease_policy if role_profile is not None else None,
        role_profile=role_profile,
    )


async def ingest_leader_turn_output(
    runtime: GroupRuntime,
    objective: ObjectiveSpec,
    leader_round: LeaderRound,
    output: str | dict[str, Any] | LeaderTurnOutput,
    *,
    backend: str = "in_process",
    working_dir: str | None = None,
    role_profile: WorkerRoleProfile | None = None,
) -> IngestedLeaderTurn:
    parsed_output = output if isinstance(output, LeaderTurnOutput) else parse_leader_turn_output(output)
    cwd = str(Path(working_dir) if working_dir else Path.cwd())
    effective_tasks = tuple(
        _merge_lane_defaults(task, leader_round=leader_round)
        for task in parsed_output.teammate_tasks
    )

    created_tasks: list[TaskCard] = []
    proposed_with_created_task: list[tuple[ProposedTeammateTask, TaskCard]] = []
    for proposed_task in effective_tasks:
        if proposed_task.scope != TaskScope.TEAM:
            raise ValueError("Leader turn output can only create team-scope teammate tasks")
        task = await runtime.submit_task(
            group_id=objective.group_id,
            team_id=leader_round.team_id,
            goal=proposed_task.goal,
            scope=TaskScope.TEAM,
            lane_id=leader_round.lane_id,
            owned_paths=proposed_task.owned_paths,
            created_by=leader_round.leader_task.leader_id,
            derived_from=leader_round.runtime_task.task_id,
            reason=proposed_task.reason,
        )
        if proposed_task.verification_commands:
            task.verification_commands = proposed_task.verification_commands
            await runtime.store.save_task(task)
        created_tasks.append(task)
        proposed_with_created_task.append((proposed_task, task))

    task_id_by_slice_id: dict[str, str] = {task.slice_id: created.task_id for task, created in proposed_with_created_task}
    task_slice_metadata: dict[str, dict[str, Any]] = {}
    for proposed_task, created_task in proposed_with_created_task:
        depends_on_task_ids = tuple(
            task_id_by_slice_id[slice_id] for slice_id in proposed_task.depends_on if slice_id in task_id_by_slice_id
        )
        created_task.slice_id = proposed_task.slice_id
        created_task.slice_mode = proposed_task.slice_mode
        created_task.depends_on_slice_ids = proposed_task.depends_on
        created_task.depends_on_task_ids = depends_on_task_ids
        created_task.parallel_group = proposed_task.parallel_group
        await runtime.store.save_task(created_task)
        task_slice_metadata[created_task.task_id] = {
            "slice_id": proposed_task.slice_id,
            "slice_mode": proposed_task.slice_mode,
            "depends_on_slice_ids": list(proposed_task.depends_on),
            "depends_on_task_ids": list(depends_on_task_ids),
            "parallel_group": proposed_task.parallel_group,
        }

    max_teammates = max(leader_round.leader_task.budget.max_teammates, 0)
    activation_ready_pairs = [
        (proposed_task, task)
        for proposed_task, task in proposed_with_created_task
        if not task.depends_on_task_ids
    ]
    dependency_deferred_pairs = [
        (proposed_task, task)
        for proposed_task, task in proposed_with_created_task
        if task.depends_on_task_ids
    ]
    assignable_tasks = activation_ready_pairs[:max_teammates]
    activation_backlog_pairs = activation_ready_pairs[max_teammates:]
    teammate_assignments = tuple(
        _build_teammate_assignment(
            objective=objective,
            leader_round=leader_round,
            created_task=task,
            proposed_task=proposed_task,
            teammate_index=index + 1,
            backend=backend,
            working_dir=cwd,
            depends_on_task_ids=tuple(
                task_id_by_slice_id[slice_id] for slice_id in proposed_task.depends_on if slice_id in task_id_by_slice_id
            ),
            role_profile=role_profile,
        )
        for index, (proposed_task, task) in enumerate(assignable_tasks)
    )

    overflow_count = len(activation_backlog_pairs)
    sequential_slice_ids = [task.slice_id for task, _ in proposed_with_created_task if task.slice_mode == _SLICE_MODE_SEQUENTIAL]
    parallel_groups = sorted(
        {
            task.parallel_group
            for task, _ in proposed_with_created_task
            if task.slice_mode == _SLICE_MODE_PARALLEL and task.parallel_group
        }
    )
    execution_report = await runtime.append_blackboard_entry(
        group_id=objective.group_id,
        kind=BlackboardKind.LEADER_LANE,
        entry_kind=BlackboardEntryKind.EXECUTION_REPORT,
        author_id=leader_round.leader_task.leader_id,
        lane_id=leader_round.lane_id,
        summary=f"Ingested leader output for lane {leader_round.lane_id}.",
        task_id=leader_round.runtime_task.task_id,
        payload={
            "leader_task_id": leader_round.leader_task.task_id,
            "leader_runtime_task_id": leader_round.runtime_task.task_id,
            "summary": parsed_output.summary,
            "created_task_ids": [task.task_id for task in created_tasks],
            "assignment_task_ids": [assignment.task_id for assignment in teammate_assignments],
            "budget_max_teammates": leader_round.leader_task.budget.max_teammates,
            "slice_task_ids": dict(task_id_by_slice_id),
            "sequential_slice_ids": sequential_slice_ids,
            "parallel_groups": parallel_groups,
            "task_slice_metadata": task_slice_metadata,
            "activation_ready_task_ids": [task.task_id for _, task in activation_ready_pairs],
            "deferred_task_ids": [task.task_id for _, task in dependency_deferred_pairs],
            "activation_backlog_task_ids": [task.task_id for _, task in activation_backlog_pairs],
            "overflow_count": overflow_count,
        },
    )

    proposal_entry_ids: list[str] = []
    blocker_entry_ids: list[str] = []

    return IngestedLeaderTurn(
        parsed_output=replace(parsed_output, teammate_tasks=effective_tasks),
        created_tasks=tuple(created_tasks),
        teammate_assignments=teammate_assignments,
        execution_report_entry_id=execution_report.entry_id,
        proposal_entry_ids=tuple(proposal_entry_ids),
        blocker_entry_ids=tuple(blocker_entry_ids),
    )
