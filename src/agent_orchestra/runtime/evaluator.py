from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from agent_orchestra.contracts.authority import AuthorityState
from agent_orchestra.contracts.blackboard import BlackboardKind
from agent_orchestra.contracts.delivery import DeliveryDecision, DeliveryState, DeliveryStatus
from agent_orchestra.contracts.enums import AuthorityStatus, TaskScope, TaskStatus
from agent_orchestra.contracts.objective import ObjectiveSpec
from agent_orchestra.runtime.group_runtime import GroupRuntime


@dataclass(slots=True)
class EvaluationResult:
    decision: DeliveryDecision
    status: DeliveryStatus
    summary: str
    pending_task_ids: tuple[str, ...] = ()
    active_task_ids: tuple[str, ...] = ()
    completed_task_ids: tuple[str, ...] = ()
    blocked_task_ids: tuple[str, ...] = ()
    latest_worker_ids: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)


class DeliveryEvaluator(ABC):
    @abstractmethod
    async def evaluate_lane(
        self,
        *,
        runtime: GroupRuntime,
        objective: ObjectiveSpec,
        state: DeliveryState,
    ) -> EvaluationResult:
        raise NotImplementedError

    @abstractmethod
    async def evaluate_objective(
        self,
        *,
        objective_id: str,
        lane_states: tuple[DeliveryState, ...],
        authority_state: AuthorityState | None = None,
    ) -> EvaluationResult:
        raise NotImplementedError


class DefaultDeliveryEvaluator(DeliveryEvaluator):
    async def evaluate_lane(
        self,
        *,
        runtime: GroupRuntime,
        objective: ObjectiveSpec,
        state: DeliveryState,
    ) -> EvaluationResult:
        if state.team_id is None or state.lane_id is None:
            return EvaluationResult(
                decision=DeliveryDecision.FAIL,
                status=DeliveryStatus.FAILED,
                summary="Lane delivery state is missing team_id or lane_id.",
            )

        tasks = await runtime.store.list_tasks(
            objective.group_id,
            team_id=state.team_id,
            lane_id=state.lane_id,
            scope=TaskScope.TEAM.value,
        )
        pending_task_ids = tuple(task.task_id for task in tasks if task.status == TaskStatus.PENDING)
        active_task_ids = tuple(task.task_id for task in tasks if task.status == TaskStatus.IN_PROGRESS)
        completed_task_ids = tuple(task.task_id for task in tasks if task.status == TaskStatus.COMPLETED)
        blocked_task_ids = tuple(
            task.task_id
            for task in tasks
            if task.status in {TaskStatus.BLOCKED, TaskStatus.FAILED}
        )
        waiting_task_ids = tuple(
            task.task_id
            for task in tasks
            if task.status == TaskStatus.WAITING_FOR_AUTHORITY
        )

        team_snapshot = await runtime.reduce_blackboard(
            group_id=objective.group_id,
            kind=BlackboardKind.TEAM,
            lane_id=state.lane_id,
            team_id=state.team_id,
        )

        if blocked_task_ids:
            return EvaluationResult(
                decision=DeliveryDecision.BLOCK,
                status=DeliveryStatus.BLOCKED,
                summary=f"Lane {state.lane_id} is blocked by task state.",
                pending_task_ids=pending_task_ids,
                active_task_ids=active_task_ids,
                completed_task_ids=completed_task_ids,
                blocked_task_ids=blocked_task_ids,
                latest_worker_ids=state.latest_worker_ids,
                metadata={"open_blockers": list(team_snapshot.open_blockers)},
            )

        waiting_task_ids = tuple(
            task.task_id
            for task in tasks
            if task.status == TaskStatus.WAITING_FOR_AUTHORITY
        )

        if team_snapshot.open_blockers:
            return EvaluationResult(
                decision=DeliveryDecision.BLOCK,
                status=DeliveryStatus.BLOCKED,
                summary=f"Lane {state.lane_id} has open team blockers.",
                pending_task_ids=pending_task_ids,
                active_task_ids=active_task_ids,
                completed_task_ids=completed_task_ids,
                blocked_task_ids=tuple(team_snapshot.open_blockers),
                latest_worker_ids=state.latest_worker_ids,
            )

        if waiting_task_ids:
            return EvaluationResult(
                decision=DeliveryDecision.CONTINUE,
                status=DeliveryStatus.WAITING_FOR_AUTHORITY,
                summary=
                    f"Lane {state.lane_id} is waiting for authority for tasks: {', '.join(waiting_task_ids)}.",
                pending_task_ids=pending_task_ids,
                active_task_ids=active_task_ids,
                completed_task_ids=completed_task_ids,
                blocked_task_ids=blocked_task_ids,
                latest_worker_ids=state.latest_worker_ids,
                metadata={
                    "authority_waiting": True,
                    "waiting_for_authority_task_ids": list(waiting_task_ids),
                },
            )

        if pending_task_ids or active_task_ids:
            return EvaluationResult(
                decision=DeliveryDecision.CONTINUE,
                status=DeliveryStatus.WAITING,
                summary=f"Lane {state.lane_id} still has open runtime tasks.",
                pending_task_ids=pending_task_ids,
                active_task_ids=active_task_ids,
                completed_task_ids=completed_task_ids,
                blocked_task_ids=blocked_task_ids,
                latest_worker_ids=state.latest_worker_ids,
            )

        pending_mailbox_count = int(state.metadata.get("pending_mailbox_count", 0))
        if pending_mailbox_count > 0:
            return EvaluationResult(
                decision=DeliveryDecision.CONTINUE,
                status=DeliveryStatus.RUNNING,
                summary=f"Lane {state.lane_id} is waiting for the leader to consume teammate results.",
                pending_task_ids=pending_task_ids,
                active_task_ids=active_task_ids,
                completed_task_ids=completed_task_ids,
                blocked_task_ids=blocked_task_ids,
                latest_worker_ids=state.latest_worker_ids,
                metadata={"pending_mailbox_count": pending_mailbox_count},
            )

        if state.iteration > 0:
            return EvaluationResult(
                decision=DeliveryDecision.COMPLETE,
                status=DeliveryStatus.COMPLETED,
                summary=f"Lane {state.lane_id} has converged.",
                pending_task_ids=pending_task_ids,
                active_task_ids=active_task_ids,
                completed_task_ids=completed_task_ids,
                blocked_task_ids=blocked_task_ids,
                latest_worker_ids=state.latest_worker_ids,
                metadata={"open_proposals": list(team_snapshot.open_proposals)},
            )

        return EvaluationResult(
            decision=DeliveryDecision.CONTINUE,
            status=DeliveryStatus.RUNNING,
            summary=f"Lane {state.lane_id} is still running.",
            pending_task_ids=pending_task_ids,
            active_task_ids=active_task_ids,
            completed_task_ids=completed_task_ids,
            blocked_task_ids=blocked_task_ids,
            latest_worker_ids=state.latest_worker_ids,
        )

    async def evaluate_objective(
        self,
        *,
        objective_id: str,
        lane_states: tuple[DeliveryState, ...],
        authority_state: AuthorityState | None = None,
    ) -> EvaluationResult:
        completed_lane_ids = [state.lane_id for state in lane_states if state.status == DeliveryStatus.COMPLETED and state.lane_id]
        blocked_lane_ids = [state.lane_id for state in lane_states if state.status == DeliveryStatus.BLOCKED and state.lane_id]
        failed_lane_ids = [state.lane_id for state in lane_states if state.status == DeliveryStatus.FAILED and state.lane_id]
        active_lane_ids = [
            state.lane_id
            for state in lane_states
            if state.status in {DeliveryStatus.RUNNING, DeliveryStatus.WAITING}
            and state.lane_id
        ]

        if failed_lane_ids:
            return EvaluationResult(
                decision=DeliveryDecision.FAIL,
                status=DeliveryStatus.FAILED,
                summary=f"Objective {objective_id} has failed lanes.",
                metadata={"failed_lane_ids": failed_lane_ids},
            )
        if blocked_lane_ids:
            return EvaluationResult(
                decision=DeliveryDecision.BLOCK,
                status=DeliveryStatus.BLOCKED,
                summary=f"Objective {objective_id} is blocked by at least one lane.",
                metadata={"blocked_lane_ids": blocked_lane_ids},
            )
        waiting_lane_ids = [
            state.lane_id
            for state in lane_states
            if state.status == DeliveryStatus.WAITING_FOR_AUTHORITY and state.lane_id
        ]
        if waiting_lane_ids:
            return EvaluationResult(
                decision=DeliveryDecision.CONTINUE,
                status=DeliveryStatus.WAITING_FOR_AUTHORITY,
                summary=f"Objective {objective_id} is waiting for authority on lane(s): {', '.join(waiting_lane_ids)}.",
                metadata={
                    "waiting_lane_ids": waiting_lane_ids,
                    "authority_waiting": True,
                },
            )
        if lane_states and len(completed_lane_ids) == len(lane_states):
            authority_status = (
                authority_state.status.value
                if authority_state is not None
                else AuthorityStatus.PENDING.value
            )
            metadata = {
                "completed_lane_ids": completed_lane_ids,
                "authority_status": authority_status,
            }
            if authority_state is None or authority_state.status not in {
                AuthorityStatus.AUTHORITY_COMPLETE,
                AuthorityStatus.OBJECTIVE_COMPLETE,
            }:
                return EvaluationResult(
                    decision=DeliveryDecision.CONTINUE,
                    status=DeliveryStatus.WAITING,
                    summary=f"Objective {objective_id} is waiting for authority completion.",
                    metadata=metadata,
                )
            return EvaluationResult(
                decision=DeliveryDecision.COMPLETE,
                status=DeliveryStatus.COMPLETED,
                summary=f"Objective {objective_id} has completed all lanes and authority acceptance.",
                metadata=metadata,
            )
        if active_lane_ids:
            return EvaluationResult(
                decision=DeliveryDecision.CONTINUE,
                status=DeliveryStatus.RUNNING,
                summary=f"Objective {objective_id} still has active lanes.",
                metadata={"active_lane_ids": active_lane_ids, "completed_lane_ids": completed_lane_ids},
            )
        return EvaluationResult(
            decision=DeliveryDecision.CONTINUE,
            status=DeliveryStatus.PENDING,
            summary=f"Objective {objective_id} is pending lane work.",
            metadata={"completed_lane_ids": completed_lane_ids},
        )
