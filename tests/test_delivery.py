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
from agent_orchestra.contracts.blackboard import BlackboardKind
from agent_orchestra.contracts.enums import AuthorityStatus, TaskScope, TaskStatus
from agent_orchestra.runtime.delivery import DefaultDeliveryEvaluator, DeliveryPhase, DeliveryState
from agent_orchestra.runtime.group_runtime import GroupRuntime
from agent_orchestra.storage.in_memory import InMemoryOrchestrationStore


class DeliveryEvaluatorTest(IsolatedAsyncioTestCase):
    async def test_evaluate_team_returns_running_for_open_tasks(self) -> None:
        store = InMemoryOrchestrationStore()
        runtime = GroupRuntime(store=store, bus=InMemoryEventBus())
        evaluator = DefaultDeliveryEvaluator()

        await runtime.create_group("group-a")
        await runtime.create_team(group_id="group-a", team_id="team-a", name="Runtime")
        await runtime.submit_task(
            group_id="group-a",
            team_id="team-a",
            goal="Implement the reducer runtime.",
            scope=TaskScope.TEAM,
            lane_id="lane-a",
            created_by="leader:lane-a",
        )

        team_tasks = await runtime.list_visible_tasks(group_id="group-a", viewer_role="teammate", team_id="team-a")
        leader_lane_snapshot = await runtime.reduce_blackboard(
            group_id="group-a",
            kind=BlackboardKind.LEADER_LANE,
            lane_id="lane-a",
        )
        team_snapshot = await runtime.reduce_blackboard(
            group_id="group-a",
            kind=BlackboardKind.TEAM,
            team_id="team-a",
            lane_id="lane-a",
        )

        evaluation = await evaluator.evaluate_team(
            objective_id="obj-runtime",
            lane_id="lane-a",
            team_id="team-a",
            tasks=team_tasks,
            leader_lane_snapshot=leader_lane_snapshot,
            team_snapshot=team_snapshot,
            worker_records=(),
        )

        self.assertEqual(evaluation.state.phase, DeliveryPhase.RUNNING)
        self.assertTrue(evaluation.should_continue)
        self.assertEqual(evaluation.state.open_task_ids, (team_tasks[0].task_id,))

    async def test_evaluate_team_returns_blocked_when_blocker_exists(self) -> None:
        store = InMemoryOrchestrationStore()
        runtime = GroupRuntime(store=store, bus=InMemoryEventBus())
        evaluator = DefaultDeliveryEvaluator()

        await runtime.create_group("group-a")
        await runtime.create_team(group_id="group-a", team_id="team-a", name="Runtime")
        blocked_task = await runtime.submit_task(
            group_id="group-a",
            team_id="team-a",
            goal="Implement the reducer runtime.",
            scope=TaskScope.TEAM,
            lane_id="lane-a",
            created_by="leader:lane-a",
        )
        await runtime.update_task_status(
            task_id=blocked_task.task_id,
            status=TaskStatus.BLOCKED,
            actor_id="team-a:teammate:1",
            blocked_by=("need-contract",),
        )

        team_tasks = await runtime.list_visible_tasks(group_id="group-a", viewer_role="teammate", team_id="team-a")
        leader_lane_snapshot = await runtime.reduce_blackboard(
            group_id="group-a",
            kind=BlackboardKind.LEADER_LANE,
            lane_id="lane-a",
        )
        team_snapshot = await runtime.reduce_blackboard(
            group_id="group-a",
            kind=BlackboardKind.TEAM,
            team_id="team-a",
            lane_id="lane-a",
        )

        evaluation = await evaluator.evaluate_team(
            objective_id="obj-runtime",
            lane_id="lane-a",
            team_id="team-a",
            tasks=team_tasks,
            leader_lane_snapshot=leader_lane_snapshot,
            team_snapshot=team_snapshot,
            worker_records=(),
        )

        self.assertEqual(evaluation.state.phase, DeliveryPhase.BLOCKED)
        self.assertFalse(evaluation.should_continue)
        self.assertEqual(evaluation.state.blocked_task_ids, (blocked_task.task_id,))

    async def test_evaluate_objective_transitions_to_authority_pending(self) -> None:
        evaluator = DefaultDeliveryEvaluator()
        team_state = DeliveryState(
            objective_id="obj-runtime",
            phase=DeliveryPhase.TEAM_COMPLETE,
            lane_id="lane-a",
            team_id="team-a",
        )

        evaluation = await evaluator.evaluate_objective(
            objective_id="obj-runtime",
            team_states=(team_state,),
            authority_state=None,
        )

        self.assertEqual(evaluation.state.phase, DeliveryPhase.AUTHORITY_PENDING)
        self.assertFalse(evaluation.should_continue)

    async def test_evaluate_objective_transitions_to_objective_complete(self) -> None:
        evaluator = DefaultDeliveryEvaluator()
        team_state = DeliveryState(
            objective_id="obj-runtime",
            phase=DeliveryPhase.TEAM_COMPLETE,
            lane_id="lane-a",
            team_id="team-a",
        )

        evaluation = await evaluator.evaluate_objective(
            objective_id="obj-runtime",
            team_states=(team_state,),
            authority_state=AuthorityState(
                group_id="group-a",
                status=AuthorityStatus.OBJECTIVE_COMPLETE,
                summary="Objective accepted.",
            ),
        )

        self.assertEqual(evaluation.state.phase, DeliveryPhase.OBJECTIVE_COMPLETE)
        self.assertFalse(evaluation.should_continue)
