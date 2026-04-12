from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Any

from agent_orchestra.contracts.execution import (
    ResidentCoordinatorPhase,
    ResidentCoordinatorSession,
    WorkerAssignment,
    WorkerRecord,
)
from agent_orchestra.contracts.objective import ObjectiveSpec
from agent_orchestra.runtime.bootstrap_round import LeaderRound
from agent_orchestra.runtime.resident_kernel import ResidentCoordinatorCycleResult, ResidentCoordinatorKernel
from agent_orchestra.tools.mailbox import MailboxEnvelope


def _ordered_unique_strings(values: list[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        if not value or value in seen:
            continue
        seen.add(value)
        ordered.append(value)
    return tuple(ordered)


ExecuteAssignment = Callable[
    [WorkerAssignment],
    Awaitable[tuple[WorkerRecord | None, MailboxEnvelope | None]],
]


@dataclass(slots=True)
class ResidentTeammateAcquireResult:
    assignments: tuple[WorkerAssignment, ...] = ()
    mailbox_envelopes: tuple[MailboxEnvelope, ...] = ()
    processed_mailbox_envelope_ids: tuple[str, ...] = ()
    directed_claimed_task_ids: tuple[str, ...] = ()
    autonomous_claimed_task_ids: tuple[str, ...] = ()
    claim_session_ids: tuple[str, ...] = ()
    claim_sources: tuple[str, ...] = ()
    teammate_execution_evidence: bool = False


AcquireAssignments = Callable[
    [tuple[WorkerAssignment, ...], int],
    Awaitable[ResidentTeammateAcquireResult],
]


@dataclass(slots=True)
class ResidentTeammateRunResult:
    teammate_records: tuple[WorkerRecord, ...] = ()
    mailbox_envelopes: tuple[MailboxEnvelope, ...] = ()
    claimed_task_ids: tuple[str, ...] = ()
    claim_session_ids: tuple[str, ...] = ()
    claim_sources: tuple[str, ...] = ()
    directed_claimed_task_ids: tuple[str, ...] = ()
    autonomous_claimed_task_ids: tuple[str, ...] = ()
    processed_mailbox_envelope_ids: tuple[str, ...] = ()
    teammate_execution_evidence: bool = False
    dispatched_assignment_count: int = 0
    coordinator_session: ResidentCoordinatorSession | None = None


class ResidentTeammateRuntime:
    def __init__(
        self,
        *,
        resident_kernel: ResidentCoordinatorKernel | None = None,
    ) -> None:
        self.resident_kernel = resident_kernel or ResidentCoordinatorKernel()

    async def run(
        self,
        *,
        objective: ObjectiveSpec,
        leader_round: LeaderRound,
        assignments: tuple[WorkerAssignment, ...],
        concurrency: int,
        execute_assignment: ExecuteAssignment,
        acquire_assignments: AcquireAssignments,
        coordinator_id: str,
        keep_alive_when_idle: bool = False,
        max_idle_cycles: int = 1,
    ) -> ResidentTeammateRunResult:
        teammate_records: list[WorkerRecord] = []
        mailbox_envelopes: list[MailboxEnvelope] = []
        claim_sources: list[str] = []
        claimed_task_ids: list[str] = []
        claim_session_ids: list[str] = []
        directed_claimed_task_ids: list[str] = []
        autonomous_claimed_task_ids: list[str] = []
        processed_mailbox_envelope_ids: list[str] = []
        teammate_execution_evidence = False
        dispatched_assignment_count = 0
        idle_cycles = 0

        pending_assignments = list(assignments)
        active_assignments: dict[str, WorkerAssignment] = {}
        running_assignments: dict[
            asyncio.Task[tuple[WorkerAssignment, WorkerRecord | None, MailboxEnvelope | None]],
            WorkerAssignment,
        ] = {}

        runtime_concurrency = max(concurrency, 1)

        session = ResidentCoordinatorSession(
            coordinator_id=coordinator_id,
            role="teammate_runtime",
            phase=ResidentCoordinatorPhase.BOOTING,
            objective_id=objective.objective_id,
            lane_id=leader_round.lane_id,
            team_id=leader_round.team_id,
        )

        def _record_claim_evidence(assignments_to_record: tuple[WorkerAssignment, ...]) -> None:
            for assignment in assignments_to_record:
                source_raw = assignment.metadata.get("claim_source")
                session_raw = assignment.metadata.get("claim_session_id")
                if isinstance(source_raw, str) and source_raw:
                    claim_sources.append(source_raw)
                    if source_raw in {"resident_task_list_claim", "autonomous_claim"}:
                        claimed_task_ids.append(assignment.task_id)
                        if isinstance(session_raw, str) and session_raw:
                            claim_session_ids.append(session_raw)

        async def _execute(
            assignment: WorkerAssignment,
        ) -> tuple[WorkerAssignment, WorkerRecord | None, MailboxEnvelope | None]:
            record, envelope = await execute_assignment(assignment)
            return assignment, record, envelope

        def _record_acquire_result(acquire_result: ResidentTeammateAcquireResult) -> None:
            nonlocal teammate_execution_evidence

            if not acquire_result.assignments:
                teammate_execution_evidence = (
                    teammate_execution_evidence or acquire_result.teammate_execution_evidence
                )
                mailbox_envelopes.extend(acquire_result.mailbox_envelopes)
                processed_mailbox_envelope_ids.extend(acquire_result.processed_mailbox_envelope_ids)
                directed_claimed_task_ids.extend(acquire_result.directed_claimed_task_ids)
                autonomous_claimed_task_ids.extend(acquire_result.autonomous_claimed_task_ids)
                claim_session_ids.extend(acquire_result.claim_session_ids)
                claim_sources.extend(acquire_result.claim_sources)
                return

            _record_claim_evidence(acquire_result.assignments)
            mailbox_envelopes.extend(acquire_result.mailbox_envelopes)
            processed_mailbox_envelope_ids.extend(acquire_result.processed_mailbox_envelope_ids)
            directed_claimed_task_ids.extend(acquire_result.directed_claimed_task_ids)
            autonomous_claimed_task_ids.extend(acquire_result.autonomous_claimed_task_ids)
            claim_session_ids.extend(acquire_result.claim_session_ids)
            claim_sources.extend(acquire_result.claim_sources)
            teammate_execution_evidence = (
                teammate_execution_evidence
                or acquire_result.teammate_execution_evidence
                or bool(acquire_result.processed_mailbox_envelope_ids)
                or bool(acquire_result.directed_claimed_task_ids)
                or bool(acquire_result.autonomous_claimed_task_ids)
            )

        async def _fill_slots() -> tuple[int, int]:
            nonlocal dispatched_assignment_count

            claimed_delta = 0
            dispatch_delta = 0
            while len(running_assignments) < runtime_concurrency:
                if not pending_assignments:
                    available_capacity = runtime_concurrency - len(running_assignments)
                    if available_capacity <= 0:
                        break
                    acquire_result = await acquire_assignments(
                        tuple(active_assignments.values()),
                        available_capacity,
                    )
                    if acquire_result.assignments:
                        pending_assignments.extend(acquire_result.assignments)
                        claimed_delta += len(acquire_result.assignments)
                    _record_acquire_result(acquire_result)
                if not pending_assignments:
                    break
                assignment = pending_assignments.pop(0)
                active_assignments[assignment.worker_id] = assignment
                task = asyncio.create_task(_execute(assignment))
                running_assignments[task] = assignment
                dispatch_delta += 1
            dispatched_assignment_count += dispatch_delta
            return claimed_delta, dispatch_delta

        async def _step(
            current_session: ResidentCoordinatorSession,
        ) -> ResidentCoordinatorCycleResult:
            nonlocal idle_cycles
            nonlocal teammate_execution_evidence
            claimed_delta, dispatch_delta = await _fill_slots()
            if not running_assignments:
                if keep_alive_when_idle and idle_cycles < max(max_idle_cycles, 0):
                    idle_cycles += 1
                    await asyncio.sleep(0)
                    return ResidentCoordinatorCycleResult(
                        phase=ResidentCoordinatorPhase.IDLE,
                        stop=False,
                        progress_made=claimed_delta > 0 or dispatch_delta > 0,
                        claimed_task_delta=claimed_delta,
                        subordinate_dispatch_delta=dispatch_delta,
                        reason="Resident teammate runtime remained online while idle.",
                    )
                return ResidentCoordinatorCycleResult(
                    phase=(
                        ResidentCoordinatorPhase.IDLE
                        if keep_alive_when_idle
                        else ResidentCoordinatorPhase.QUIESCENT
                    ),
                    stop=True,
                    progress_made=claimed_delta > 0 or dispatch_delta > 0,
                    claimed_task_delta=claimed_delta,
                    subordinate_dispatch_delta=dispatch_delta,
                    reason=(
                        "Resident teammate runtime exhausted idle keepalive without new work."
                        if keep_alive_when_idle
                        else "Resident teammate runtime drained all runnable work."
                    ),
                )
            idle_cycles = 0

            done, _ = await asyncio.wait(
                tuple(running_assignments),
                return_when=asyncio.FIRST_COMPLETED,
            )
            for completed_task in done:
                completed_assignment = running_assignments.pop(completed_task)
                active_assignments.pop(completed_assignment.worker_id, None)
                try:
                    _, record, envelope = await completed_task
                except Exception:
                    outstanding_tasks = tuple(running_assignments)
                    for outstanding_task in outstanding_tasks:
                        outstanding_task.cancel()
                    if outstanding_tasks:
                        await asyncio.gather(*outstanding_tasks, return_exceptions=True)
                    raise
                if record is not None:
                    teammate_records.append(record)
                    teammate_execution_evidence = True
                if envelope is not None:
                    mailbox_envelopes.append(envelope)

            refill_claimed_delta, refill_dispatch_delta = await _fill_slots()
            claimed_delta += refill_claimed_delta
            dispatch_delta += refill_dispatch_delta

            should_stop = not running_assignments and not pending_assignments
            if should_stop and keep_alive_when_idle and idle_cycles < max(max_idle_cycles, 0):
                idle_cycles += 1
                await asyncio.sleep(0)
                return ResidentCoordinatorCycleResult(
                    phase=ResidentCoordinatorPhase.IDLE,
                    stop=False,
                    progress_made=True,
                    claimed_task_delta=claimed_delta,
                    subordinate_dispatch_delta=dispatch_delta,
                    reason="Resident teammate runtime completed current work and stayed online.",
                )
            phase = (
                ResidentCoordinatorPhase.IDLE
                if should_stop and keep_alive_when_idle
                else ResidentCoordinatorPhase.QUIESCENT
                if should_stop
                else ResidentCoordinatorPhase.WAITING_FOR_SUBORDINATES
            )
            return ResidentCoordinatorCycleResult(
                phase=phase,
                stop=should_stop,
                progress_made=True,
                claimed_task_delta=claimed_delta,
                subordinate_dispatch_delta=dispatch_delta,
                active_subordinate_ids=tuple(active_assignments),
                reason=(
                    "Resident teammate runtime reached quiescence."
                    if should_stop
                    else "Resident teammate runtime waiting for active assignments."
                ),
            )

        async def _finalize(
            final_session: ResidentCoordinatorSession,
        ) -> ResidentTeammateRunResult:
            ordered_claim_sources = _ordered_unique_strings(claim_sources)
            ordered_claimed_task_ids = _ordered_unique_strings(claimed_task_ids)
            ordered_claim_session_ids = _ordered_unique_strings(claim_session_ids)
            ordered_directed_claimed_task_ids = _ordered_unique_strings(directed_claimed_task_ids)
            ordered_autonomous_claimed_task_ids = _ordered_unique_strings(autonomous_claimed_task_ids)
            ordered_processed_mailbox_envelope_ids = _ordered_unique_strings(processed_mailbox_envelope_ids)
            metadata: dict[str, Any] = dict(final_session.metadata)
            metadata.update(
                {
                    "claim_sources": ordered_claim_sources,
                    "claimed_task_ids": ordered_claimed_task_ids,
                    "claim_session_ids": ordered_claim_session_ids,
                    "directed_claimed_task_ids": ordered_directed_claimed_task_ids,
                    "autonomous_claimed_task_ids": ordered_autonomous_claimed_task_ids,
                    "processed_mailbox_envelope_ids": ordered_processed_mailbox_envelope_ids,
                    "teammate_execution_evidence": teammate_execution_evidence,
                    "dispatched_assignment_count": dispatched_assignment_count,
                }
            )
            final_session = replace(final_session, metadata=metadata)
            return ResidentTeammateRunResult(
                teammate_records=tuple(teammate_records),
                mailbox_envelopes=tuple(mailbox_envelopes),
                claimed_task_ids=ordered_claimed_task_ids,
                claim_session_ids=ordered_claim_session_ids,
                claim_sources=ordered_claim_sources,
                directed_claimed_task_ids=ordered_directed_claimed_task_ids,
                autonomous_claimed_task_ids=ordered_autonomous_claimed_task_ids,
                processed_mailbox_envelope_ids=ordered_processed_mailbox_envelope_ids,
                teammate_execution_evidence=teammate_execution_evidence,
                dispatched_assignment_count=dispatched_assignment_count,
                coordinator_session=final_session,
            )

        return await self.resident_kernel.run(
            session=session,
            step=_step,
            finalize=_finalize,
        )
