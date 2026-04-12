from __future__ import annotations

import sys
from pathlib import Path
from unittest import IsolatedAsyncioTestCase

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agent_orchestra.contracts.enums import SpecEdgeKind, SpecNodeKind
from agent_orchestra.planning.dynamic_superleader import DynamicPlanningConfig, DynamicSuperLeaderPlanner
from agent_orchestra.planning.template import ObjectiveTemplate, PlanningContext


class DynamicSuperLeaderPlannerTest(IsolatedAsyncioTestCase):
    async def test_build_initial_plan_synthesizes_bounded_workstreams_from_dynamic_seeds(self) -> None:
        planner = DynamicSuperLeaderPlanner(
            DynamicPlanningConfig(
                max_workstreams=2,
                default_budget_max_teammates=2,
                default_budget_max_iterations=3,
            )
        )
        template = ObjectiveTemplate(
            objective_id="obj-dynamic",
            group_id="group-a",
            title="Advance Agent Orchestra Reliability And Planning",
            description="Use dynamic seeds to synthesize bounded workstreams.",
            metadata={
                "planning_mode": "dynamic_superleader",
                "dynamic_workstream_seeds": [
                    {
                        "workstream_id": "worker-reliability",
                        "title": "Worker Reliability",
                        "summary": "Own retry and resume hardening.",
                        "team_name": "Runtime",
                        "acceptance_checks": ["worker reliability tests"],
                        "budget": {"max_teammates": 1, "max_iterations": 2},
                    },
                    {
                        "workstream_id": "execution-guard",
                        "title": "Execution Guard",
                        "summary": "Tighten owned_paths and verification enforcement.",
                        "team_name": "QA",
                    },
                    {
                        "workstream_id": "dynamic-team-planning",
                        "title": "Dynamic Team Planning",
                        "summary": "Add bounded superleader dynamic planning.",
                        "team_name": "Planning",
                    },
                ],
            },
        )

        result = await planner.build_initial_plan(template)

        self.assertEqual(result.objective.metadata["planner_kind"], "dynamic_superleader")
        self.assertEqual(result.objective.metadata["dynamic_replan_count"], 0)
        self.assertEqual(
            result.objective.metadata["selected_workstream_ids"],
            ["worker-reliability", "execution-guard"],
        )
        self.assertEqual([task.task_id for task in result.leader_tasks], [
            "obj-dynamic:leader-task:worker-reliability",
            "obj-dynamic:leader-task:execution-guard",
        ])
        self.assertEqual(result.leader_tasks[0].budget.max_teammates, 1)
        self.assertEqual(result.leader_tasks[0].budget.max_iterations, 2)
        self.assertEqual(result.leader_tasks[1].budget.max_teammates, 2)
        self.assertEqual(result.leader_tasks[1].budget.max_iterations, 3)
        self.assertTrue(any(node.kind == SpecNodeKind.OBJECTIVE for node in result.spec_nodes))
        self.assertTrue(any(edge.kind == SpecEdgeKind.DECOMPOSES_TO for edge in result.spec_edges))

    async def test_replan_adds_follow_up_workstream_and_enforces_max_replans(self) -> None:
        planner = DynamicSuperLeaderPlanner(
            DynamicPlanningConfig(
                max_workstreams=3,
                max_replans=1,
                default_budget_max_teammates=1,
                default_budget_max_iterations=2,
            )
        )
        template = ObjectiveTemplate(
            objective_id="obj-dynamic",
            group_id="group-a",
            title="Advance Agent Orchestra Reliability",
            description="Start from one bounded workstream.",
            metadata={
                "planning_mode": "dynamic_superleader",
                "dynamic_workstream_seeds": [
                    {
                        "workstream_id": "worker-reliability",
                        "title": "Worker Reliability",
                        "summary": "Own retry and resume hardening.",
                        "team_name": "Runtime",
                    },
                ],
            },
        )

        replanned = await planner.replan(
            template,
            PlanningContext(
                reason="Need a verification follow-up lane.",
                supersede_node_ids=("obj-dynamic:leader:worker-reliability",),
                metadata={
                    "dynamic_workstream_seeds": [
                        {
                            "workstream_id": "verification-guard",
                            "title": "Verification Guard",
                            "summary": "Own owned_paths and verification follow-up.",
                            "team_name": "QA",
                        }
                    ]
                },
            ),
        )

        self.assertEqual(replanned.objective.metadata["dynamic_replan_count"], 1)
        self.assertEqual(len(replanned.leader_tasks), 2)
        self.assertTrue(any(task.task_id == "obj-dynamic:leader-task:verification-guard" for task in replanned.leader_tasks))
        self.assertTrue(any(edge.kind == SpecEdgeKind.SUPERSEDES for edge in replanned.spec_edges))

        with self.assertRaises(ValueError):
            await planner.replan(
                ObjectiveTemplate(
                    objective_id="obj-dynamic",
                    group_id="group-a",
                    title="Advance Agent Orchestra Reliability",
                    description="Start from one bounded workstream.",
                    metadata={
                        "planning_mode": "dynamic_superleader",
                        "dynamic_replan_count": 1,
                        "dynamic_workstream_seeds": [
                            {
                                "workstream_id": "worker-reliability",
                                "title": "Worker Reliability",
                                "summary": "Own retry and resume hardening.",
                                "team_name": "Runtime",
                            },
                        ],
                    },
                ),
                PlanningContext(
                    reason="Try a second replan.",
                    metadata={
                        "dynamic_workstream_seeds": [
                            {
                                "workstream_id": "planning-follow-up",
                                "title": "Planning Follow-Up",
                                "summary": "Attempt an extra replan beyond the bound.",
                                "team_name": "Planning",
                            }
                        ]
                    },
                ),
            )
