from __future__ import annotations

import sys
from pathlib import Path
from unittest import IsolatedAsyncioTestCase

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agent_orchestra.bus.in_memory import InMemoryEventBus
from agent_orchestra.contracts.authority import AuthorityState
from agent_orchestra.contracts.blackboard import BlackboardEntryKind, BlackboardKind
from agent_orchestra.contracts.delivery import DeliveryDecision, DeliveryState, DeliveryStateKind, DeliveryStatus
from agent_orchestra.contracts.enums import AuthorityStatus, TaskStatus
from agent_orchestra.runtime.evaluator import DefaultDeliveryEvaluator
from agent_orchestra.runtime.group_runtime import GroupRuntime
from agent_orchestra.storage.in_memory import InMemoryOrchestrationStore


class EvaluatorTest(IsolatedAsyncioTestCase):
    async def test_default_evaluator_marks_lane_complete_when_all_team_tasks_are_completed(self) -> None:
        store = InMemoryOrchestrationStore()
        runtime = GroupRuntime(store=store, bus=InMemoryEventBus())
        evaluator = DefaultDeliveryEvaluator()

        await runtime.create_group("group-a")
        await runtime.create_team(group_id="group-a", team_id="team-runtime", name="Runtime")
        objective = await runtime.create_objective(
            group_id="group-a",
            objective_id="obj-runtime",
            title="Deliver runtime",
            description="Finish the runtime lane.",
        )
        task = await runtime.submit_task(
            group_id="group-a",
            team_id="team-runtime",
            goal="Implement autonomous runtime",
            lane_id="runtime",
        )
        await runtime.update_task_status(task_id=task.task_id, status=task.status.COMPLETED, actor_id="teammate-1")

        state = DeliveryState(
            delivery_id="obj-runtime:lane:runtime",
            objective_id="obj-runtime",
            kind=DeliveryStateKind.LANE,
            status=DeliveryStatus.RUNNING,
            lane_id="runtime",
            team_id="team-runtime",
            iteration=2,
            latest_worker_ids=("leader:runtime",),
        )

        result = await evaluator.evaluate_lane(runtime=runtime, objective=objective, state=state)

        self.assertEqual(result.decision, DeliveryDecision.COMPLETE)
        self.assertEqual(result.status, DeliveryStatus.COMPLETED)
        self.assertEqual(result.completed_task_ids, (task.task_id,))

    async def test_default_evaluator_marks_lane_blocked_when_team_blackboard_has_blockers(self) -> None:
        store = InMemoryOrchestrationStore()
        runtime = GroupRuntime(store=store, bus=InMemoryEventBus())
        evaluator = DefaultDeliveryEvaluator()

        await runtime.create_group("group-a")
        await runtime.create_team(group_id="group-a", team_id="team-runtime", name="Runtime")
        objective = await runtime.create_objective(
            group_id="group-a",
            objective_id="obj-runtime",
            title="Deliver runtime",
            description="Finish the runtime lane.",
        )
        await runtime.append_blackboard_entry(
            group_id="group-a",
            kind=BlackboardKind.TEAM,
            entry_kind=BlackboardEntryKind.BLOCKER,
            author_id="teammate-1",
            lane_id="runtime",
            team_id="team-runtime",
            summary="Need approval before continuing.",
        )

        state = DeliveryState(
            delivery_id="obj-runtime:lane:runtime",
            objective_id="obj-runtime",
            kind=DeliveryStateKind.LANE,
            status=DeliveryStatus.RUNNING,
            lane_id="runtime",
            team_id="team-runtime",
            iteration=1,
        )

        result = await evaluator.evaluate_lane(runtime=runtime, objective=objective, state=state)

        self.assertEqual(result.decision, DeliveryDecision.BLOCK)
        self.assertEqual(result.status, DeliveryStatus.BLOCKED)

    async def test_default_evaluator_waits_for_authority_after_all_lanes_complete(self) -> None:
        evaluator = DefaultDeliveryEvaluator()
        lane_a = DeliveryState(
            delivery_id="obj-runtime:lane:runtime",
            objective_id="obj-runtime",
            kind=DeliveryStateKind.LANE,
            status=DeliveryStatus.COMPLETED,
            lane_id="runtime",
            team_id="team-runtime",
            iteration=2,
        )
        lane_b = DeliveryState(
            delivery_id="obj-runtime:lane:qa",
            objective_id="obj-runtime",
            kind=DeliveryStateKind.LANE,
            status=DeliveryStatus.COMPLETED,
            lane_id="qa",
            team_id="team-qa",
            iteration=1,
        )

        result = await evaluator.evaluate_objective(
            objective_id="obj-runtime",
            lane_states=(lane_a, lane_b),
        )

        self.assertEqual(result.decision, DeliveryDecision.CONTINUE)
        self.assertEqual(result.status, DeliveryStatus.WAITING)
        self.assertEqual(result.metadata["authority_status"], AuthorityStatus.PENDING.value)
        self.assertEqual(result.metadata["completed_lane_ids"], ["runtime", "qa"])

    async def test_default_evaluator_marks_lane_waiting_for_authority(self) -> None:
        store = InMemoryOrchestrationStore()
        runtime = GroupRuntime(store=store, bus=InMemoryEventBus())
        evaluator = DefaultDeliveryEvaluator()

        await runtime.create_group("group-a")
        await runtime.create_team(group_id="group-a", team_id="team-runtime", name="Runtime")
        objective = await runtime.create_objective(
            group_id="group-a",
            objective_id="obj-auth",
            title="Authority lane",
            description="Lane stuck waiting for authority.",
        )
        task = await runtime.submit_task(
            group_id="group-a",
            team_id="team-runtime",
            goal="Wait for authority",
            lane_id="runtime",
        )
        await runtime.update_task_status(
            task_id=task.task_id,
            status=TaskStatus.WAITING_FOR_AUTHORITY,
            actor_id="teammate-1",
        )

        state = DeliveryState(
            delivery_id="obj-auth:lane:runtime",
            objective_id="obj-auth",
            kind=DeliveryStateKind.LANE,
            status=DeliveryStatus.RUNNING,
            lane_id="runtime",
            team_id="team-runtime",
            iteration=1,
        )

        result = await evaluator.evaluate_lane(runtime=runtime, objective=objective, state=state)

        self.assertEqual(result.status, DeliveryStatus.WAITING_FOR_AUTHORITY)
        self.assertTrue(result.metadata.get("authority_waiting"))
        self.assertEqual(result.metadata["waiting_for_authority_task_ids"], [task.task_id])

    async def test_default_evaluator_marks_objective_waiting_for_authority(self) -> None:
        evaluator = DefaultDeliveryEvaluator()
        waiting_lane = DeliveryState(
            delivery_id="obj-auth:lane:runtime",
            objective_id="obj-auth",
            kind=DeliveryStateKind.LANE,
            status=DeliveryStatus.WAITING_FOR_AUTHORITY,
            lane_id="runtime",
            team_id="team-runtime",
            iteration=1,
        )
        pending_lane = DeliveryState(
            delivery_id="obj-auth:lane:qa",
            objective_id="obj-auth",
            kind=DeliveryStateKind.LANE,
            status=DeliveryStatus.PENDING,
            lane_id="qa",
            team_id="team-qa",
        )

        result = await evaluator.evaluate_objective(
            objective_id="obj-auth",
            lane_states=(waiting_lane, pending_lane),
        )

        self.assertEqual(result.status, DeliveryStatus.WAITING_FOR_AUTHORITY)
        self.assertEqual(result.metadata["waiting_lane_ids"], ["runtime"])

    async def test_default_evaluator_marks_objective_complete_once_authority_is_complete(self) -> None:
        evaluator = DefaultDeliveryEvaluator()
        lane_a = DeliveryState(
            delivery_id="obj-runtime:lane:runtime",
            objective_id="obj-runtime",
            kind=DeliveryStateKind.LANE,
            status=DeliveryStatus.COMPLETED,
            lane_id="runtime",
            team_id="team-runtime",
            iteration=2,
        )
        lane_b = DeliveryState(
            delivery_id="obj-runtime:lane:qa",
            objective_id="obj-runtime",
            kind=DeliveryStateKind.LANE,
            status=DeliveryStatus.COMPLETED,
            lane_id="qa",
            team_id="team-qa",
            iteration=1,
        )

        result = await evaluator.evaluate_objective(
            objective_id="obj-runtime",
            lane_states=(lane_a, lane_b),
            authority_state=AuthorityState(
                group_id="group-a",
                status=AuthorityStatus.AUTHORITY_COMPLETE,
                accepted_handoffs=("handoff-runtime", "handoff-qa"),
                updated_task_ids=("task-runtime", "task-qa"),
                summary="Authority root accepted both lanes.",
            ),
        )

        self.assertEqual(result.decision, DeliveryDecision.COMPLETE)
        self.assertEqual(result.status, DeliveryStatus.COMPLETED)
        self.assertEqual(result.metadata["completed_lane_ids"], ["runtime", "qa"])
        self.assertEqual(result.metadata["authority_status"], AuthorityStatus.AUTHORITY_COMPLETE.value)
