from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from unittest import IsolatedAsyncioTestCase, TestCase

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agent_orchestra.contracts.enums import SpecEdgeKind, SpecNodeKind
from agent_orchestra.planning.io import load_objective_template, render_objective_template, write_objective_template
from agent_orchestra.planning.template import ObjectiveTemplate, PlanningContext, WorkstreamTemplate
from agent_orchestra.planning.template_planner import TemplatePlanner


class TemplateIOTest(TestCase):
    def test_rendered_template_is_json_compatible_yaml_subset(self) -> None:
        rendered = render_objective_template(objective_id="obj-demo", group_id="group-demo")
        data = json.loads(rendered)

        self.assertEqual(data["objective_id"], "obj-demo")
        self.assertEqual(data["group_id"], "group-demo")
        self.assertIsInstance(data["workstreams"], list)

    def test_write_and_load_template_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "objective.yaml"
            write_objective_template(path, objective_id="obj-demo", group_id="group-demo")

            loaded = load_objective_template(path)

        self.assertEqual(loaded.objective_id, "obj-demo")
        self.assertEqual(loaded.group_id, "group-demo")
        self.assertGreaterEqual(len(loaded.workstreams), 1)


class TemplatePlannerTest(IsolatedAsyncioTestCase):
    async def test_build_initial_plan_compiles_objective_and_leader_artifacts(self) -> None:
        planner = TemplatePlanner()
        template = ObjectiveTemplate(
            objective_id="obj-runtime",
            group_id="group-a",
            title="Build runtime",
            description="Compile a deterministic runtime plan.",
            success_metrics=("tests green",),
            hard_constraints=("keep library-first",),
            global_budget={"max_teams": 2},
            workstreams=(
                WorkstreamTemplate(
                    workstream_id="runtime-core",
                    title="Runtime Core",
                    summary="Implement the runtime lane.",
                    team_name="Runtime",
                    acceptance_checks=("runtime tests",),
                    budget_max_teammates=2,
                    budget_max_iterations=3,
                ),
                WorkstreamTemplate(
                    workstream_id="verification",
                    title="Verification",
                    summary="Verify the runtime lane.",
                    team_name="QA",
                    depends_on=("runtime-core",),
                    acceptance_checks=("planning tests",),
                    budget_max_teammates=1,
                    budget_max_iterations=2,
                ),
            ),
        )

        result = await planner.build_initial_plan(template)

        self.assertEqual(result.objective.objective_id, "obj-runtime")
        self.assertEqual(len(result.leader_tasks), 2)
        self.assertEqual(len(result.spec_nodes), 3)
        self.assertTrue(any(node.kind == SpecNodeKind.OBJECTIVE for node in result.spec_nodes))
        self.assertTrue(any(edge.kind == SpecEdgeKind.DECOMPOSES_TO for edge in result.spec_edges))
        self.assertTrue(any(edge.kind == SpecEdgeKind.DEPENDS_ON for edge in result.spec_edges))

    async def test_replan_adds_workstreams_and_supersedes_edges(self) -> None:
        planner = TemplatePlanner()
        template = ObjectiveTemplate(
            objective_id="obj-runtime",
            group_id="group-a",
            title="Build runtime",
            description="Compile a deterministic runtime plan.",
            workstreams=(
                WorkstreamTemplate(
                    workstream_id="runtime-core",
                    title="Runtime Core",
                    summary="Implement the runtime lane.",
                    team_name="Runtime",
                ),
            ),
        )

        replanned = await planner.replan(
            template,
            PlanningContext(
                reason="Need a documentation lane.",
                additional_workstreams=(
                    WorkstreamTemplate(
                        workstream_id="docs",
                        title="Documentation",
                        summary="Document the runtime lane.",
                        team_name="Docs",
                    ),
                ),
                supersede_node_ids=("obj-runtime:leader:runtime-core",),
            ),
        )

        self.assertEqual(len(replanned.leader_tasks), 2)
        self.assertTrue(any(task.task_id == "obj-runtime:leader-task:docs" for task in replanned.leader_tasks))
        self.assertTrue(any(edge.kind == SpecEdgeKind.SUPERSEDES for edge in replanned.spec_edges))
