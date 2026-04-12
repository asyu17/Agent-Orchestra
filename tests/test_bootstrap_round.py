from __future__ import annotations

import sys
from pathlib import Path
from unittest import IsolatedAsyncioTestCase

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agent_orchestra.bus.in_memory import InMemoryEventBus
from agent_orchestra.contracts.blackboard import BlackboardKind
from agent_orchestra.contracts.enums import EventKind, TaskScope, WorkerStatus
from agent_orchestra.contracts.runner import AgentRunner, RunnerHealth, RunnerStreamEvent, RunnerTurnRequest, RunnerTurnResult
from agent_orchestra.planning.template import ObjectiveTemplate, WorkstreamTemplate
from agent_orchestra.planning.template_planner import TemplatePlanner
from agent_orchestra.runtime.backends.in_process import InProcessLaunchBackend
from agent_orchestra.runtime.bootstrap_round import (
    compile_leader_assignment,
    compile_leader_assignments,
    materialize_planning_result,
)
from agent_orchestra.runtime.group_runtime import GroupRuntime
from agent_orchestra.runtime.worker_supervisor import DefaultWorkerSupervisor
from agent_orchestra.storage.in_memory import InMemoryOrchestrationStore


class _FakeRunner(AgentRunner):
    async def run_turn(self, request: RunnerTurnRequest) -> RunnerTurnResult:
        return RunnerTurnResult(
            response_id="resp-leader-1",
            output_text=f"leader-handled:{request.input_text}",
            status="completed",
        )

    async def stream_turn(self, request: RunnerTurnRequest):
        if False:
            yield RunnerStreamEvent(kind=EventKind.RUNNER_COMPLETED)

    async def cancel(self, run_id: str) -> None:
        return None

    async def healthcheck(self) -> RunnerHealth:
        return RunnerHealth(healthy=True, provider="fake")


class BootstrapRoundTest(IsolatedAsyncioTestCase):
    async def test_materialize_planning_result_creates_teams_tasks_and_bootstrap_blackboards(self) -> None:
        store = InMemoryOrchestrationStore()
        bus = InMemoryEventBus()
        runtime = GroupRuntime(store=store, bus=bus)
        planner = TemplatePlanner()

        await runtime.create_group("group-a")
        planning_result = await planner.build_initial_plan(
            ObjectiveTemplate(
                objective_id="obj-runtime",
                group_id="group-a",
                title="Build runtime",
                description="Compile the runtime lane.",
                workstreams=(
                    WorkstreamTemplate(
                        workstream_id="runtime-core",
                        title="Runtime Core",
                        summary="Own the core runtime lane.",
                        team_name="Runtime",
                        acceptance_checks=("runtime tests",),
                    ),
                    WorkstreamTemplate(
                        workstream_id="verification",
                        title="Verification",
                        summary="Verify the core runtime lane.",
                        team_name="QA",
                        depends_on=("runtime-core",),
                        acceptance_checks=("integration tests",),
                    ),
                ),
            )
        )

        bundle = await materialize_planning_result(runtime, planning_result, created_by="superleader-1")

        self.assertEqual(bundle.objective.objective_id, "obj-runtime")
        self.assertEqual(
            {team.team_id for team in bundle.teams},
            {"group-a:team:runtime-core", "group-a:team:verification"},
        )
        self.assertEqual(
            {leader_round.lane_id for leader_round in bundle.leader_rounds},
            {"runtime-core", "verification"},
        )
        self.assertTrue(all(leader_round.runtime_task.scope == TaskScope.LEADER_LANE for leader_round in bundle.leader_rounds))
        self.assertEqual(len(await store.list_tasks("group-a")), 2)
        self.assertEqual(len(store.blackboard_entries), 4)
        self.assertEqual(
            {entry.kind for entry in store.blackboard_entries.values()},
            {BlackboardKind.LEADER_LANE, BlackboardKind.TEAM},
        )

    async def test_compile_leader_assignments_builds_runnable_in_process_payloads(self) -> None:
        store = InMemoryOrchestrationStore()
        bus = InMemoryEventBus()
        supervisor = DefaultWorkerSupervisor(
            store=store,
            launch_backends={"in_process": InProcessLaunchBackend()},
            runner=_FakeRunner(),
        )
        runtime = GroupRuntime(
            store=store,
            bus=bus,
            launch_backends={"in_process": InProcessLaunchBackend()},
            supervisor=supervisor,
        )
        planner = TemplatePlanner()

        await runtime.create_group("group-a")
        planning_result = await planner.build_initial_plan(
            ObjectiveTemplate(
                objective_id="obj-runtime",
                group_id="group-a",
                title="Build runtime",
                description="Compile the runtime lane.",
                success_metrics=("tests green",),
                hard_constraints=("keep library-first",),
                workstreams=(
                    WorkstreamTemplate(
                        workstream_id="runtime-core",
                        title="Runtime Core",
                        summary="Own the core runtime lane.",
                        team_name="Runtime",
                        acceptance_checks=("runtime tests",),
                        budget_max_teammates=2,
                        budget_max_iterations=3,
                    ),
                ),
            )
        )

        bundle = await materialize_planning_result(runtime, planning_result, created_by="superleader-1")
        assignments = compile_leader_assignments(bundle, backend="in_process", working_dir="/tmp/agent-orchestra")

        self.assertEqual(len(assignments), 1)
        assignment = assignments[0]
        leader_round = bundle.leader_rounds[0]
        self.assertEqual(assignment.worker_id, leader_round.leader_task.leader_id)
        self.assertEqual(assignment.role, "leader")
        self.assertEqual(assignment.backend, "in_process")
        self.assertEqual(assignment.team_id, leader_round.team_id)
        self.assertEqual(assignment.lane_id, leader_round.lane_id)
        self.assertEqual(assignment.task_id, leader_round.runtime_task.task_id)
        self.assertEqual(assignment.working_dir, "/tmp/agent-orchestra")
        self.assertEqual(assignment.metadata["team_name"], leader_round.team_name)
        self.assertEqual(assignment.metadata["leader_task_id"], leader_round.leader_task.task_id)
        self.assertIn("Build runtime", assignment.instructions)
        self.assertIn("Runtime Core", assignment.instructions)
        self.assertIn("runtime tests", assignment.instructions)

        record = await runtime.run_worker_assignment(assignment)

        self.assertEqual(record.status, WorkerStatus.COMPLETED)
        self.assertEqual(record.output_text, "leader-handled:Start your first leader coordination turn.")

    async def test_compile_leader_assignment_supports_iteration_input_and_previous_response(self) -> None:
        store = InMemoryOrchestrationStore()
        bus = InMemoryEventBus()
        runtime = GroupRuntime(store=store, bus=bus)
        planner = TemplatePlanner()

        await runtime.create_group("group-a")
        planning_result = await planner.build_initial_plan(
            ObjectiveTemplate(
                objective_id="obj-runtime",
                group_id="group-a",
                title="Build runtime",
                description="Compile the runtime lane.",
                workstreams=(
                    WorkstreamTemplate(
                        workstream_id="runtime-core",
                        title="Runtime Core",
                        summary="Own the core runtime lane.",
                        team_name="Runtime",
                    ),
                ),
            )
        )

        bundle = await materialize_planning_result(runtime, planning_result, created_by="superleader-1")
        assignment = compile_leader_assignment(
            bundle.objective,
            bundle.leader_rounds[0],
            iteration=2,
            backend="in_process",
            working_dir="/tmp/agent-orchestra",
            input_text="Continue from the previous round.",
            previous_response_id="resp-leader-1",
        )

        self.assertEqual(assignment.assignment_id, f"{bundle.leader_rounds[0].runtime_task.task_id}:leader-turn-2")
        self.assertEqual(assignment.input_text, "Continue from the previous round.")
        self.assertEqual(assignment.previous_response_id, "resp-leader-1")
