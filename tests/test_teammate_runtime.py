from __future__ import annotations

from unittest import IsolatedAsyncioTestCase

from agent_orchestra.contracts.enums import TaskScope, WorkerStatus
from agent_orchestra.contracts.execution import (
    LeaderTaskCard,
    ResidentCoordinatorPhase,
    WorkerAssignment,
    WorkerBudget,
    WorkerRecord,
)
from agent_orchestra.contracts.objective import ObjectiveSpec
from agent_orchestra.contracts.task import TaskCard
from agent_orchestra.runtime.bootstrap_round import LeaderRound
from agent_orchestra.runtime.teammate_runtime import (
    ResidentTeammateAcquireResult,
    ResidentTeammateRuntime,
)


class ResidentTeammateRuntimeTest(IsolatedAsyncioTestCase):
    async def test_runtime_can_stay_online_while_idle_and_claim_later_work(self) -> None:
        runtime = ResidentTeammateRuntime()
        objective = ObjectiveSpec(
            objective_id="obj-1",
            group_id="group-a",
            title="Runtime",
            description="Stay online while idle before claiming later work.",
        )
        leader_round = LeaderRound(
            lane_id="lane-a",
            team_id="group-a:team:lane-a",
            team_name="Runtime",
            leader_task=LeaderTaskCard(
                task_id="leader-task-1",
                objective_id="obj-1",
                leader_id="leader:lane-a",
                title="Lead runtime lane",
                summary="Keep teammates online.",
                budget=WorkerBudget(max_teammates=1, max_iterations=1),
            ),
            runtime_task=TaskCard(
                task_id="lane-task-1",
                goal="Keep teammate online",
                lane="lane-a",
                group_id="group-a",
                team_id="group-a:team:lane-a",
                scope=TaskScope.LEADER_LANE,
                created_by="superleader-1",
            ),
            leader_lane_directive_entry_id="entry-1",
            team_directive_entry_id="entry-2",
        )
        claim_attempts = 0

        async def execute_assignment(
            assignment: WorkerAssignment,
        ) -> tuple[WorkerRecord | None, None]:
            return (
                WorkerRecord(
                    worker_id=assignment.worker_id,
                    assignment_id=assignment.assignment_id,
                    backend=assignment.backend,
                    role=assignment.role,
                    status=WorkerStatus.COMPLETED,
                    output_text="claimed later work",
                ),
                None,
            )

        async def acquire_assignments(
            _existing: tuple[WorkerAssignment, ...],
            _limit: int,
        ) -> ResidentTeammateAcquireResult:
            nonlocal claim_attempts
            claim_attempts += 1
            if claim_attempts != 2:
                return ResidentTeammateAcquireResult()
            return ResidentTeammateAcquireResult(
                assignments=(
                    WorkerAssignment(
                        assignment_id="assignment-1",
                        worker_id="group-a:team:lane-a:teammate:1",
                        group_id="group-a",
                        team_id="group-a:team:lane-a",
                        lane_id="lane-a",
                        task_id="task-1",
                        role="teammate",
                        backend="in_process",
                        instructions="Claim later work",
                        input_text="run",
                        metadata={
                            "claim_source": "autonomous_claim",
                            "claim_session_id": "claim-session-1",
                        },
                    ),
                ),
                autonomous_claimed_task_ids=("task-1",),
                claim_session_ids=("claim-session-1",),
                claim_sources=("autonomous_claim",),
                teammate_execution_evidence=True,
            )

        result = await runtime.run(
            objective=objective,
            leader_round=leader_round,
            assignments=(),
            concurrency=1,
            execute_assignment=execute_assignment,
            acquire_assignments=acquire_assignments,
            coordinator_id="group-a:team:lane-a:teammate-runtime",
            keep_alive_when_idle=True,
            max_idle_cycles=2,
        )

        self.assertEqual(claim_attempts, 5)
        self.assertEqual(result.dispatched_assignment_count, 1)
        self.assertEqual(result.claimed_task_ids, ("task-1",))
        self.assertEqual(result.claim_session_ids, ("claim-session-1",))
        self.assertEqual(result.claim_sources, ("autonomous_claim",))
        self.assertEqual(result.autonomous_claimed_task_ids, ("task-1",))
        self.assertEqual(result.directed_claimed_task_ids, ())
        self.assertTrue(result.teammate_execution_evidence)
        self.assertIsNotNone(result.coordinator_session)
        assert result.coordinator_session is not None
        self.assertEqual(result.coordinator_session.phase, ResidentCoordinatorPhase.IDLE)
