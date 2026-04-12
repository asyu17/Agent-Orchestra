from __future__ import annotations

from agent_orchestra.bus.base import EventBus
from agent_orchestra.contracts.enums import TaskScope, TaskStatus
from agent_orchestra.contracts.events import OrchestraEvent
from agent_orchestra.contracts.handoff import HandoffRecord
from agent_orchestra.contracts.ids import make_handoff_id, make_task_id
from agent_orchestra.contracts.task import TaskCard
from agent_orchestra.storage.base import OrchestrationStore


class TeamRuntime:
    def __init__(self, group_id: str, team_id: str, *, store: OrchestrationStore, bus: EventBus) -> None:
        self.group_id = group_id
        self.team_id = team_id
        self.store = store
        self.bus = bus

    async def submit_task(
        self,
        goal: str,
        *,
        scope: TaskScope = TaskScope.TEAM,
        lane_id: str | None = None,
        owned_paths: tuple[str, ...] = (),
        handoff_to: tuple[str, ...] = (),
        created_by: str | None = None,
        derived_from: str | None = None,
        reason: str = "",
    ) -> TaskCard:
        task = TaskCard(
            task_id=make_task_id(),
            goal=goal,
            lane=lane_id or self.team_id,
            group_id=self.group_id,
            team_id=self.team_id,
            scope=scope,
            owned_paths=owned_paths,
            handoff_to=handoff_to,
            owner_id=None,
            created_by=created_by,
            derived_from=derived_from,
            reason=reason,
            status=TaskStatus.PENDING,
        )
        await self.store.save_task(task)
        await self.bus.publish(OrchestraEvent.task_submitted(self.group_id, self.team_id, task.task_id))
        return task

    async def record_handoff(
        self,
        *,
        to_team_id: str,
        task_id: str,
        artifact_refs: tuple[str, ...] = (),
        summary: str = "",
        contract_assertions: tuple[str, ...] = (),
        verification_summary: dict[str, object] | None = None,
    ) -> HandoffRecord:
        handoff = HandoffRecord(
            handoff_id=make_handoff_id(),
            group_id=self.group_id,
            from_team_id=self.team_id,
            to_team_id=to_team_id,
            task_id=task_id,
            artifact_refs=artifact_refs,
            summary=summary,
            contract_assertions=contract_assertions,
            verification_summary=dict(verification_summary or {}),
        )
        await self.store.save_handoff(handoff)
        await self.bus.publish(
            OrchestraEvent.handoff_recorded(
                self.group_id,
                from_team_id=self.team_id,
                to_team_id=to_team_id,
                task_id=task_id,
            )
        )
        return handoff
