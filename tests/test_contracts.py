from __future__ import annotations

import inspect
import sys
from pathlib import Path
from unittest import TestCase

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agent_orchestra.contracts.blackboard import BlackboardEntry, BlackboardSnapshot
from agent_orchestra.contracts.enums import (
    BlackboardEntryKind,
    BlackboardKind,
    SpecEdgeKind,
    SpecNodeKind,
    SpecNodeStatus,
    TaskScope,
    TaskStatus,
)
from agent_orchestra.contracts.objective import ObjectiveSpec, SpecEdge, SpecNode
from agent_orchestra.contracts.runner import AgentRunner, RunnerTurnRequest
from agent_orchestra.contracts.task import TaskCard


class ContractsTest(TestCase):
    def test_task_card_defaults_to_claude_compatible_open_state(self) -> None:
        card = TaskCard(task_id="task-1", goal="demo", lane="research")
        self.assertEqual(card.status, TaskStatus.PENDING)
        self.assertEqual(card.scope, TaskScope.TEAM)
        self.assertIsNone(card.owner_id)
        self.assertEqual(card.blocked_by, ())

    def test_agent_runner_is_abstract(self) -> None:
        self.assertTrue(inspect.isabstract(AgentRunner))
        self.assertTrue(hasattr(AgentRunner, "run_turn"))
        self.assertTrue(hasattr(AgentRunner, "stream_turn"))

    def test_runner_turn_request_keeps_previous_response_id(self) -> None:
        request = RunnerTurnRequest(
            agent_id="agent-1",
            instructions="You are a test agent.",
            input_text="hello",
            previous_response_id="resp_123",
        )
        self.assertEqual(request.previous_response_id, "resp_123")

    def test_objective_and_spec_contracts_store_values(self) -> None:
        objective = ObjectiveSpec(
            objective_id="obj-1",
            group_id="group-a",
            title="Launch runtime",
            description="Implement the orchestration core.",
            success_metrics=("tests green",),
            hard_constraints=("keep CLI working",),
            budget={"max_teams": 2},
        )
        node = SpecNode(
            node_id="node-1",
            objective_id="obj-1",
            kind=SpecNodeKind.LEADER_TASK,
            title="Lead runtime work",
            summary="Own the main lane.",
            scope=TaskScope.LEADER_LANE,
            lane_id="lane-a",
            created_by="superleader-1",
            status=SpecNodeStatus.OPEN,
        )
        edge = SpecEdge(
            edge_id="edge-1",
            objective_id="obj-1",
            kind=SpecEdgeKind.DECOMPOSES_TO,
            from_node_id="node-1",
            to_node_id="node-2",
        )

        self.assertEqual(objective.title, "Launch runtime")
        self.assertEqual(node.kind, SpecNodeKind.LEADER_TASK)
        self.assertEqual(edge.kind, SpecEdgeKind.DECOMPOSES_TO)

    def test_blackboard_contracts_store_values(self) -> None:
        entry = BlackboardEntry(
            entry_id="entry-1",
            blackboard_id="group-a:team:team-a",
            group_id="group-a",
            kind=BlackboardKind.TEAM,
            entry_kind=BlackboardEntryKind.PROPOSAL,
            author_id="teammate-a1",
            team_id="team-a",
            summary="Split reducer logic",
        )
        snapshot = BlackboardSnapshot(
            blackboard_id=entry.blackboard_id,
            group_id="group-a",
            kind=BlackboardKind.TEAM,
            team_id="team-a",
            version=1,
            latest_entry_ids=(entry.entry_id,),
            open_proposals=(entry.entry_id,),
        )

        self.assertEqual(entry.entry_kind, BlackboardEntryKind.PROPOSAL)
        self.assertEqual(snapshot.open_proposals, ("entry-1",))
