from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Any, TypeVar

from agent_orchestra.contracts.execution import ResidentCoordinatorPhase, ResidentCoordinatorSession

ResultT = TypeVar("ResultT")


@dataclass(slots=True)
class ResidentCoordinatorCycleResult:
    phase: ResidentCoordinatorPhase
    stop: bool = False
    progress_made: bool = False
    prompt_turn_delta: int = 0
    claimed_task_delta: int = 0
    subordinate_dispatch_delta: int = 0
    mailbox_poll_delta: int = 0
    active_subordinate_ids: tuple[str, ...] = ()
    mailbox_cursor: str | None = None
    reason: str = ""
    metadata: dict[str, Any] | None = None


class ResidentCoordinatorKernel:
    async def run(
        self,
        *,
        session: ResidentCoordinatorSession,
        step: Callable[[ResidentCoordinatorSession], Awaitable[ResidentCoordinatorCycleResult]],
        finalize: Callable[[ResidentCoordinatorSession], Awaitable[ResultT]],
    ) -> ResultT:
        current = replace(session)
        while True:
            cycle_result = await step(current)
            current = self._apply_cycle_result(current, cycle_result)
            if cycle_result.stop:
                return await finalize(current)

    def _apply_cycle_result(
        self,
        session: ResidentCoordinatorSession,
        cycle_result: ResidentCoordinatorCycleResult,
    ) -> ResidentCoordinatorSession:
        metadata = dict(session.metadata)
        if cycle_result.metadata:
            metadata.update(cycle_result.metadata)
        idle_transition_count = session.idle_transition_count
        quiescent_transition_count = session.quiescent_transition_count
        if cycle_result.phase == ResidentCoordinatorPhase.IDLE and session.phase != ResidentCoordinatorPhase.IDLE:
            idle_transition_count += 1
        if (
            cycle_result.phase == ResidentCoordinatorPhase.QUIESCENT
            and session.phase != ResidentCoordinatorPhase.QUIESCENT
        ):
            quiescent_transition_count += 1
        return replace(
            session,
            phase=cycle_result.phase,
            cycle_count=session.cycle_count + 1,
            prompt_turn_count=session.prompt_turn_count + max(cycle_result.prompt_turn_delta, 0),
            claimed_task_count=session.claimed_task_count + max(cycle_result.claimed_task_delta, 0),
            subordinate_dispatch_count=(
                session.subordinate_dispatch_count + max(cycle_result.subordinate_dispatch_delta, 0)
            ),
            mailbox_poll_count=session.mailbox_poll_count + max(cycle_result.mailbox_poll_delta, 0),
            idle_transition_count=idle_transition_count,
            quiescent_transition_count=quiescent_transition_count,
            active_subordinate_ids=cycle_result.active_subordinate_ids,
            mailbox_cursor=cycle_result.mailbox_cursor
            if cycle_result.mailbox_cursor is not None
            else session.mailbox_cursor,
            last_reason=cycle_result.reason,
            metadata=metadata,
        )
