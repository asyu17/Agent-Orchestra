from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from agent_orchestra.contracts.authority import AuthorityState
from agent_orchestra.contracts.blackboard import BlackboardSnapshot
from agent_orchestra.contracts.enums import AuthorityStatus
from agent_orchestra.contracts.execution import WorkerRecord
from agent_orchestra.contracts.task import TaskCard


class DeliveryPhase(str):
    PLANNED = "planned"
    RUNNING = "running"
    BLOCKED = "blocked"
    FAILED = "failed"
    TEAM_COMPLETE = "team_complete"
    AUTHORITY_PENDING = "authority_pending"
    AUTHORITY_COMPLETE = "authority_complete"
    OBJECTIVE_COMPLETE = "objective_complete"


@dataclass(slots=True)
class DeliveryState:
    objective_id: str
    phase: str
    lane_id: str | None = None
    team_id: str | None = None
    open_task_ids: tuple[str, ...] = ()
    blocked_task_ids: tuple[str, ...] = ()
    completed_task_ids: tuple[str, ...] = ()
    blocker_entry_ids: tuple[str, ...] = ()
    proposal_entry_ids: tuple[str, ...] = ()
    latest_summary: str = ""
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(slots=True)
class DeliveryEvaluation:
    state: DeliveryState
    should_continue: bool
    reason: str


class DeliveryEvaluator(ABC):
    @abstractmethod
    async def evaluate_team(
        self,
        *,
        objective_id: str,
        lane_id: str,
        team_id: str,
        tasks: tuple[TaskCard, ...] | list[TaskCard],
        leader_lane_snapshot: BlackboardSnapshot | None,
        team_snapshot: BlackboardSnapshot | None,
        worker_records: tuple[WorkerRecord, ...] | list[WorkerRecord],
    ) -> DeliveryEvaluation:
        raise NotImplementedError

    @abstractmethod
    async def evaluate_objective(
        self,
        *,
        objective_id: str,
        team_states: tuple[DeliveryState, ...] | list[DeliveryState],
        authority_state: AuthorityState | None,
    ) -> DeliveryEvaluation:
        raise NotImplementedError


class DefaultDeliveryEvaluator(DeliveryEvaluator):
    async def evaluate_team(
        self,
        *,
        objective_id: str,
        lane_id: str,
        team_id: str,
        tasks: tuple[TaskCard, ...] | list[TaskCard],
        leader_lane_snapshot: BlackboardSnapshot | None,
        team_snapshot: BlackboardSnapshot | None,
        worker_records: tuple[WorkerRecord, ...] | list[WorkerRecord],
    ) -> DeliveryEvaluation:
        del worker_records

        open_task_ids = tuple(task.task_id for task in tasks if task.status.value in {"pending", "in_progress"})
        blocked_task_ids = tuple(task.task_id for task in tasks if task.status.value == "blocked")
        failed_task_ids = tuple(task.task_id for task in tasks if task.status.value == "failed")
        completed_task_ids = tuple(task.task_id for task in tasks if task.status.value == "completed")
        blocker_entry_ids = tuple(
            dict.fromkeys(
                (leader_lane_snapshot.open_blockers if leader_lane_snapshot else ())
                + (team_snapshot.open_blockers if team_snapshot else ())
            )
        )
        proposal_entry_ids = tuple(
            dict.fromkeys(
                (leader_lane_snapshot.open_proposals if leader_lane_snapshot else ())
                + (team_snapshot.open_proposals if team_snapshot else ())
            )
        )
        latest_summary = (
            (leader_lane_snapshot.summary if leader_lane_snapshot else "")
            or (team_snapshot.summary if team_snapshot else "")
        )

        if failed_task_ids:
            state = DeliveryState(
                objective_id=objective_id,
                phase=DeliveryPhase.FAILED,
                lane_id=lane_id,
                team_id=team_id,
                open_task_ids=open_task_ids,
                blocked_task_ids=blocked_task_ids,
                completed_task_ids=completed_task_ids,
                blocker_entry_ids=blocker_entry_ids,
                proposal_entry_ids=proposal_entry_ids,
                latest_summary=latest_summary,
                metadata={"failed_task_ids": failed_task_ids},
            )
            return DeliveryEvaluation(state=state, should_continue=False, reason="At least one team task failed.")

        if blocked_task_ids or blocker_entry_ids:
            state = DeliveryState(
                objective_id=objective_id,
                phase=DeliveryPhase.BLOCKED,
                lane_id=lane_id,
                team_id=team_id,
                open_task_ids=open_task_ids,
                blocked_task_ids=blocked_task_ids,
                completed_task_ids=completed_task_ids,
                blocker_entry_ids=blocker_entry_ids,
                proposal_entry_ids=proposal_entry_ids,
                latest_summary=latest_summary,
            )
            return DeliveryEvaluation(state=state, should_continue=False, reason="Team work is blocked.")

        if open_task_ids:
            state = DeliveryState(
                objective_id=objective_id,
                phase=DeliveryPhase.RUNNING,
                lane_id=lane_id,
                team_id=team_id,
                open_task_ids=open_task_ids,
                blocked_task_ids=blocked_task_ids,
                completed_task_ids=completed_task_ids,
                blocker_entry_ids=blocker_entry_ids,
                proposal_entry_ids=proposal_entry_ids,
                latest_summary=latest_summary,
            )
            return DeliveryEvaluation(state=state, should_continue=True, reason="Open team tasks remain.")

        state = DeliveryState(
            objective_id=objective_id,
            phase=DeliveryPhase.TEAM_COMPLETE,
            lane_id=lane_id,
            team_id=team_id,
            open_task_ids=open_task_ids,
            blocked_task_ids=blocked_task_ids,
            completed_task_ids=completed_task_ids,
            blocker_entry_ids=blocker_entry_ids,
            proposal_entry_ids=proposal_entry_ids,
            latest_summary=latest_summary,
        )
        return DeliveryEvaluation(state=state, should_continue=False, reason="Team tasks are complete.")

    async def evaluate_objective(
        self,
        *,
        objective_id: str,
        team_states: tuple[DeliveryState, ...] | list[DeliveryState],
        authority_state: AuthorityState | None,
    ) -> DeliveryEvaluation:
        phases = {state.phase for state in team_states}
        metadata = {"team_phases": sorted(phases)}

        if DeliveryPhase.FAILED in phases:
            state = DeliveryState(objective_id=objective_id, phase=DeliveryPhase.FAILED, metadata=metadata)
            return DeliveryEvaluation(state=state, should_continue=False, reason="A team failed.")

        if DeliveryPhase.BLOCKED in phases:
            state = DeliveryState(objective_id=objective_id, phase=DeliveryPhase.BLOCKED, metadata=metadata)
            return DeliveryEvaluation(state=state, should_continue=False, reason="A team is blocked.")

        if DeliveryPhase.RUNNING in phases or DeliveryPhase.PLANNED in phases:
            state = DeliveryState(objective_id=objective_id, phase=DeliveryPhase.RUNNING, metadata=metadata)
            return DeliveryEvaluation(state=state, should_continue=True, reason="At least one team is still running.")

        if authority_state is not None:
            if authority_state.status == AuthorityStatus.OBJECTIVE_COMPLETE:
                state = DeliveryState(objective_id=objective_id, phase=DeliveryPhase.OBJECTIVE_COMPLETE, metadata=metadata)
                return DeliveryEvaluation(state=state, should_continue=False, reason="Objective gate is satisfied.")
            if authority_state.status in {AuthorityStatus.AUTHORITY_COMPLETE, AuthorityStatus.TEAM_COMPLETE}:
                state = DeliveryState(objective_id=objective_id, phase=DeliveryPhase.AUTHORITY_COMPLETE, metadata=metadata)
                return DeliveryEvaluation(state=state, should_continue=False, reason="Authority state is complete.")

        state = DeliveryState(objective_id=objective_id, phase=DeliveryPhase.AUTHORITY_PENDING, metadata=metadata)
        return DeliveryEvaluation(state=state, should_continue=False, reason="Teams are complete but authority is pending.")
