from __future__ import annotations

from abc import ABC, abstractmethod

from agent_orchestra.contracts.authority import AuthorityState
from agent_orchestra.contracts.blackboard import BlackboardEntry, BlackboardEntryKind, BlackboardSnapshot
from agent_orchestra.contracts.blackboard import BlackboardKind
from agent_orchestra.contracts.enums import AuthorityStatus
from agent_orchestra.contracts.handoff import HandoffRecord


class Reducer:
    async def apply(self, group_id: str, handoffs: list[HandoffRecord]) -> AuthorityState:
        if not handoffs:
            return AuthorityState(group_id=group_id, status=AuthorityStatus.PENDING)

        accepted_handoffs = tuple(handoff.handoff_id for handoff in handoffs)
        updated_task_ids = tuple(dict.fromkeys(handoff.task_id for handoff in handoffs))
        summary = f"Accepted {len(accepted_handoffs)} handoff(s) into authority state."
        return AuthorityState(
            group_id=group_id,
            status=AuthorityStatus.AUTHORITY_COMPLETE,
            accepted_handoffs=accepted_handoffs,
            updated_task_ids=updated_task_ids,
            summary=summary,
        )


class BlackboardReducer(ABC):
    @abstractmethod
    async def reduce(
        self,
        *,
        blackboard_id: str,
        group_id: str,
        kind: BlackboardKind,
        entries: list[BlackboardEntry],
        lane_id: str | None = None,
        team_id: str | None = None,
    ) -> BlackboardSnapshot:
        raise NotImplementedError


class _BaseBlackboardReducer(BlackboardReducer):
    async def reduce(
        self,
        *,
        blackboard_id: str,
        group_id: str,
        kind: BlackboardKind,
        entries: list[BlackboardEntry],
        lane_id: str | None = None,
        team_id: str | None = None,
    ) -> BlackboardSnapshot:
        latest_entry_ids = tuple(entry.entry_id for entry in entries)
        open_blockers = tuple(
            entry.entry_id for entry in entries if entry.entry_kind == BlackboardEntryKind.BLOCKER
        )
        open_proposals = tuple(
            entry.entry_id for entry in entries if entry.entry_kind == BlackboardEntryKind.PROPOSAL
        )
        summary = (
            f"Reduced {len(entries)} entry(s) for {kind.value} blackboard: "
            f"{len(open_blockers)} blocker(s), {len(open_proposals)} proposal(s)."
        )
        return BlackboardSnapshot(
            blackboard_id=blackboard_id,
            group_id=group_id,
            kind=kind,
            lane_id=lane_id,
            team_id=team_id,
            version=len(entries),
            summary=summary,
            latest_entry_ids=latest_entry_ids,
            open_blockers=open_blockers,
            open_proposals=open_proposals,
        )


class LeaderLaneBlackboardReducer(_BaseBlackboardReducer):
    pass


class TeamBlackboardReducer(_BaseBlackboardReducer):
    pass
