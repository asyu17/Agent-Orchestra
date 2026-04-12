from __future__ import annotations

from dataclasses import fields
import json
import sys
import unittest
from pathlib import Path
from unittest import IsolatedAsyncioTestCase

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agent_orchestra.contracts.enums import WorkerStatus
from agent_orchestra.contracts.execution import (
    LeaderTaskCard,
    LaunchBackend,
    Planner,
    WorkerAssignment,
    WorkerBudget,
    WorkerHandle,
    WorkerRecord,
    WorkerResult,
    WorkerSupervisor,
)
from agent_orchestra.planning.dynamic_superleader import DynamicPlanningConfig, DynamicSuperLeaderPlanner
from agent_orchestra.planning.io import objective_template_from_dict, render_objective_template
from agent_orchestra.planning.template import WorkstreamTemplate
from agent_orchestra.storage.in_memory import InMemoryOrchestrationStore


class ExecutionContractsTest(unittest.TestCase):
    def test_dataclasses_capture_constructor_values(self) -> None:
        budget = WorkerBudget(max_teammates=3, max_iterations=5, max_tokens=1_000)
        task = LeaderTaskCard(
            task_id="task-1",
            objective_id="obj-1",
            leader_id="leader-1",
            title="Lead the charge",
            summary="Summary",
            budget=budget,
        )
        handle = WorkerHandle(
            worker_id="worker-1",
            role="teammate",
            backend="noop",
            run_id="run-1",
            process_id=123,
            session_name="sess-1",
            transport_ref="/tmp/worker.json",
        )
        assignment = WorkerAssignment(
            assignment_id="assign-1",
            worker_id="worker-1",
            group_id="group-a",
            team_id="team-a",
            task_id="task-1",
            role="leader",
            backend="in_process",
            instructions="Do the work.",
            input_text="run",
            conversation=({"role": "assistant", "content": "prior turn"},),
            previous_response_id="resp-prev",
        )
        result = WorkerResult(
            worker_id="worker-1",
            assignment_id="assign-1",
            status=WorkerStatus.COMPLETED,
            output_text="done",
            response_id="resp-1",
            usage={"output_tokens": 42},
        )
        record = WorkerRecord(
            worker_id="worker-1",
            assignment_id="assign-1",
            backend="in_process",
            role="leader",
            status=WorkerStatus.COMPLETED,
            handle=handle,
            output_text="done",
            response_id="resp-1",
            usage={"output_tokens": 42},
        )

        self.assertIs(task.budget, budget)
        self.assertEqual(task.task_id, "task-1")
        self.assertEqual(task.leader_id, "leader-1")
        self.assertEqual(handle.process_id, 123)
        self.assertEqual(handle.metadata, {})
        self.assertEqual(assignment.team_id, "team-a")
        self.assertEqual(assignment.previous_response_id, "resp-prev")
        self.assertEqual(assignment.conversation[0]["content"], "prior turn")
        self.assertEqual(result.status, WorkerStatus.COMPLETED)
        self.assertEqual(result.response_id, "resp-1")
        self.assertEqual(result.usage["output_tokens"], 42)
        self.assertIs(record.handle, handle)
        self.assertEqual(record.response_id, "resp-1")
        self.assertEqual(record.usage["output_tokens"], 42)

    def test_worker_budget_formal_fields_exclude_max_concurrency(self) -> None:
        field_names = {field.name for field in fields(WorkerBudget)}
        self.assertEqual(
            field_names,
            {"max_teammates", "max_iterations", "max_tokens", "max_seconds"},
        )

    def test_worker_budget_rejects_removed_max_concurrency_arg(self) -> None:
        with self.assertRaises(TypeError):
            WorkerBudget(max_teammates=2, max_concurrency=1)

    def test_workstream_template_budget_export_excludes_max_concurrency(self) -> None:
        template = WorkstreamTemplate(
            workstream_id="stream-a",
            title="A",
            summary="B",
            team_name="runtime",
            budget_max_teammates=4,
            budget_max_iterations=3,
            budget_max_tokens=2_000,
            budget_max_seconds=600,
        )

        budget_payload = template.to_dict()["budget"]
        self.assertEqual(
            budget_payload,
            {
                "max_teammates": 4,
                "max_iterations": 3,
                "max_tokens": 2_000,
                "max_seconds": 600,
            },
        )
        self.assertNotIn("max_concurrency", budget_payload)

    def test_workstream_template_rejects_removed_budget_max_concurrency_arg(self) -> None:
        with self.assertRaises(TypeError):
            WorkstreamTemplate(
                workstream_id="stream-a",
                title="A",
                summary="B",
                team_name="runtime",
                budget_max_concurrency=1,
            )

    def test_template_io_budget_export_excludes_max_concurrency(self) -> None:
        rendered = json.loads(render_objective_template())
        budget_payload = rendered["workstreams"][0]["budget"]

        self.assertNotIn("max_concurrency", budget_payload)
        self.assertEqual(
            set(budget_payload.keys()),
            {"max_teammates", "max_iterations", "max_tokens", "max_seconds"},
        )

        objective = objective_template_from_dict(rendered)
        parsed_budget_payload = objective.workstreams[0].to_dict()["budget"]
        self.assertNotIn("max_concurrency", parsed_budget_payload)

    def test_template_io_rejects_legacy_max_concurrency_budget_field(self) -> None:
        rendered = json.loads(render_objective_template())
        rendered["workstreams"][0]["budget"]["max_concurrency"] = 7

        with self.assertRaises(ValueError):
            objective_template_from_dict(rendered)

    def test_dynamic_planner_budget_export_excludes_max_concurrency(self) -> None:
        planner = DynamicSuperLeaderPlanner(config=DynamicPlanningConfig())

        workstream = planner._seed_to_workstream(  # noqa: SLF001 - direct unit pinning for budget export contract
            {
                "title": "runtime planner",
                "summary": "runtime planner summary",
                "budget": {
                    "max_teammates": 6,
                    "max_iterations": 4,
                    "max_tokens": 4000,
                    "max_seconds": 1200,
                },
            },
            index=1,
        )
        budget_payload = workstream.to_dict()["budget"]
        self.assertEqual(
            budget_payload,
            {
                "max_teammates": 6,
                "max_iterations": 4,
                "max_tokens": 4000,
                "max_seconds": 1200,
            },
        )
        self.assertNotIn("max_concurrency", budget_payload)

    def test_dynamic_planner_rejects_legacy_max_concurrency_budget_field(self) -> None:
        planner = DynamicSuperLeaderPlanner(config=DynamicPlanningConfig())

        with self.assertRaises(ValueError):
            planner._seed_to_workstream(  # noqa: SLF001 - direct unit pinning for budget validation
                {
                    "title": "runtime planner",
                    "summary": "runtime planner summary",
                    "budget": {
                        "max_teammates": 6,
                        "max_concurrency": 99,
                        "max_iterations": 4,
                    },
                },
                index=1,
            )

    def test_abstract_interfaces_cannot_be_instantiated(self) -> None:
        with self.assertRaises(TypeError):
            Planner()

        with self.assertRaises(TypeError):
            LaunchBackend()

        with self.assertRaises(TypeError):
            WorkerSupervisor()


class WorkerRecordStoreTest(IsolatedAsyncioTestCase):
    async def test_in_memory_store_persists_worker_records(self) -> None:
        store = InMemoryOrchestrationStore()
        record = WorkerRecord(
            worker_id="worker-1",
            assignment_id="assign-1",
            backend="in_process",
            role="leader",
            status=WorkerStatus.RUNNING,
        )

        await store.save_worker_record(record)

        loaded = await store.get_worker_record("worker-1")
        records = await store.list_worker_records()

        self.assertIs(loaded, record)
        self.assertEqual(records, [record])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
