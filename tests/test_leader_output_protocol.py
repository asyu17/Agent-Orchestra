from __future__ import annotations

import sys
from pathlib import Path
from unittest import IsolatedAsyncioTestCase

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agent_orchestra.bus.in_memory import InMemoryEventBus
from agent_orchestra.contracts.blackboard import BlackboardEntryKind
from agent_orchestra.contracts.enums import TaskScope
from agent_orchestra.planning.template import ObjectiveTemplate, WorkstreamTemplate
from agent_orchestra.planning.template_planner import TemplatePlanner
from agent_orchestra.runtime.bootstrap_round import compile_leader_assignments, materialize_planning_result
from agent_orchestra.runtime.group_runtime import GroupRuntime
from agent_orchestra.runtime.leader_output_protocol import ingest_leader_turn_output, parse_leader_turn_output
from agent_orchestra.storage.in_memory import InMemoryOrchestrationStore


class LeaderOutputProtocolTest(IsolatedAsyncioTestCase):
    def test_parse_supports_explicit_sequential_and_parallel_slices(self) -> None:
        parsed = parse_leader_turn_output(
            {
                "summary": "Split into dependent setup/implementation and parallel verification.",
                "sequential_slices": [
                    {
                        "slice_id": "slice-setup",
                        "title": "Setup protocol skeleton",
                        "goal": "Add core dataclasses and parser entrypoint.",
                        "reason": "Need a baseline slice before downstream work.",
                    },
                    {
                        "slice_id": "slice-impl",
                        "title": "Implement ingest metadata mapping",
                        "goal": "Normalize slice graph into task and assignment metadata.",
                        "reason": "Need dependency-aware ingest bridge.",
                        "depends_on": ["slice-setup"],
                    },
                ],
                "parallel_slices": [
                    {
                        "parallel_group": "verify-pack",
                        "slices": [
                            {
                                "slice_id": "slice-test",
                                "title": "Expand protocol tests",
                                "goal": "Cover parse/ingest behavior for slice graph protocol.",
                                "reason": "Need regression proof for slice metadata.",
                                "depends_on": ["slice-impl"],
                            },
                            {
                                "slice_id": "slice-doc",
                                "title": "Update runtime docs",
                                "goal": "Record new slice graph leader contract.",
                                "reason": "Need visible protocol guidance.",
                                "depends_on": ["slice-impl"],
                            },
                        ],
                    }
                ],
            }
        )

        self.assertEqual(len(parsed.teammate_tasks), 4)
        by_slice_id = {task.slice_id: task for task in parsed.teammate_tasks}
        self.assertEqual(by_slice_id["slice-setup"].depends_on, ())
        self.assertEqual(by_slice_id["slice-impl"].depends_on, ("slice-setup",))
        self.assertEqual(by_slice_id["slice-test"].parallel_group, "verify-pack")
        self.assertEqual(by_slice_id["slice-test"].depends_on, ("slice-impl",))
        self.assertEqual(by_slice_id["slice-doc"].parallel_group, "verify-pack")

    async def test_ingest_records_slice_metadata_for_assignments_and_execution_report(self) -> None:
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
                description="Advance the runtime.",
                workstreams=(
                    WorkstreamTemplate(
                        workstream_id="runtime-autonomy",
                        title="Runtime Autonomy",
                        summary="Make leader output actionable.",
                        team_name="Runtime",
                        budget_max_teammates=2,
                    ),
                ),
            )
        )
        round_bundle = await materialize_planning_result(runtime, planning_result, created_by="superleader-1")
        leader_round = round_bundle.leader_rounds[0]

        result = await ingest_leader_turn_output(
            runtime,
            round_bundle.objective,
            leader_round,
            {
                "summary": "Create sequential core work then parallelize verification.",
                "sequential_slices": [
                    {
                        "slice_id": "slice-core",
                        "title": "Implement core parser",
                        "goal": "Parse leader output into normalized slices.",
                        "reason": "Need canonical ingest input model.",
                    },
                    {
                        "slice_id": "slice-ingest",
                        "title": "Map slices to runtime metadata",
                        "goal": "Populate assignment metadata and task graph metadata.",
                        "reason": "Need runtime tracking for slice relationships.",
                        "depends_on": ["slice-core"],
                    },
                ],
                "parallel_slices": [
                    {
                        "parallel_group": "verify-track",
                        "slices": [
                            {
                                "slice_id": "slice-tests",
                                "title": "Add protocol tests",
                                "goal": "Validate parse and ingest behaviors.",
                                "reason": "Need confidence on new protocol contract.",
                                "depends_on": ["slice-ingest"],
                            }
                        ],
                    }
                ],
            },
        )

        self.assertEqual(len(result.created_tasks), 3)
        self.assertEqual(len(result.teammate_assignments), 1)

        first_assignment = result.teammate_assignments[0]
        self.assertEqual(first_assignment.metadata.get("slice_id"), "slice-core")
        self.assertEqual(first_assignment.metadata.get("depends_on"), [])
        self.assertEqual(first_assignment.metadata.get("parallel_group"), None)

        core_task = next(task for task in result.created_tasks if task.reason == "Need canonical ingest input model.")
        ingest_task = next(
            task for task in result.created_tasks if task.reason == "Need runtime tracking for slice relationships."
        )
        tests_task = next(
            task for task in result.created_tasks if task.reason == "Need confidence on new protocol contract."
        )

        report_entry = store.blackboard_entries[result.execution_report_entry_id]
        task_slice_metadata = report_entry.payload.get("task_slice_metadata")
        self.assertIsInstance(task_slice_metadata, dict)
        assert isinstance(task_slice_metadata, dict)
        self.assertEqual(task_slice_metadata[core_task.task_id]["slice_id"], "slice-core")
        self.assertEqual(task_slice_metadata[core_task.task_id]["depends_on_slice_ids"], [])
        self.assertEqual(task_slice_metadata[core_task.task_id]["depends_on_task_ids"], [])
        self.assertEqual(task_slice_metadata[ingest_task.task_id]["slice_id"], "slice-ingest")
        self.assertEqual(task_slice_metadata[ingest_task.task_id]["depends_on_slice_ids"], ["slice-core"])
        self.assertEqual(task_slice_metadata[ingest_task.task_id]["depends_on_task_ids"], [core_task.task_id])
        self.assertEqual(task_slice_metadata[tests_task.task_id]["slice_id"], "slice-tests")
        self.assertEqual(task_slice_metadata[tests_task.task_id]["parallel_group"], "verify-track")
        self.assertEqual(task_slice_metadata[tests_task.task_id]["depends_on_slice_ids"], ["slice-ingest"])
        self.assertEqual(task_slice_metadata[tests_task.task_id]["depends_on_task_ids"], [ingest_task.task_id])
        self.assertEqual(report_entry.payload.get("activation_ready_task_ids"), [core_task.task_id])
        self.assertEqual(
            report_entry.payload.get("deferred_task_ids"),
            [ingest_task.task_id, tests_task.task_id],
        )

    async def test_ingest_promotes_task_activation_contract_and_dispatches_only_ready_roots(self) -> None:
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
                description="Advance the runtime.",
                workstreams=(
                    WorkstreamTemplate(
                        workstream_id="runtime-autonomy",
                        title="Runtime Autonomy",
                        summary="Make leader output actionable.",
                        team_name="Runtime",
                        budget_max_teammates=2,
                    ),
                ),
            )
        )
        round_bundle = await materialize_planning_result(runtime, planning_result, created_by="superleader-1")
        leader_round = round_bundle.leader_rounds[0]

        result = await ingest_leader_turn_output(
            runtime,
            round_bundle.objective,
            leader_round,
            {
                "summary": "Create one root slice and defer dependent follow-up slices to the task surface.",
                "sequential_slices": [
                    {
                        "slice_id": "slice-root",
                        "title": "Implement root runtime contract",
                        "goal": "Create the first runtime activation contract.",
                        "reason": "Need an unblocked slice before dependency release can happen.",
                    },
                    {
                        "slice_id": "slice-followup",
                        "title": "Extend refill behavior",
                        "goal": "Use the shared task surface for dependency release.",
                        "reason": "Need the root slice completed before refill can continue.",
                        "depends_on": ["slice-root"],
                    },
                ],
                "parallel_slices": [
                    {
                        "parallel_group": "verify-track",
                        "slices": [
                            {
                                "slice_id": "slice-tests",
                                "title": "Cover activation release",
                                "goal": "Add regression coverage for deferred activation.",
                                "reason": "Need follow-up verification after the refill slice lands.",
                                "depends_on": ["slice-followup"],
                            }
                        ],
                    }
                ],
            },
        )

        self.assertEqual(len(result.created_tasks), 3)
        self.assertEqual(len(result.teammate_assignments), 1)
        self.assertFalse(result.proposal_entry_ids)
        self.assertFalse(result.blocker_entry_ids)

        root_task = next(task for task in result.created_tasks if task.slice_id == "slice-root")
        followup_task = next(task for task in result.created_tasks if task.slice_id == "slice-followup")
        tests_task = next(task for task in result.created_tasks if task.slice_id == "slice-tests")

        self.assertEqual(root_task.slice_mode, "sequential")
        self.assertEqual(root_task.depends_on_task_ids, ())
        self.assertEqual(root_task.depends_on_slice_ids, ())
        self.assertIsNone(root_task.parallel_group)
        self.assertEqual(followup_task.depends_on_slice_ids, ("slice-root",))
        self.assertEqual(followup_task.depends_on_task_ids, (root_task.task_id,))
        self.assertEqual(tests_task.parallel_group, "verify-track")
        self.assertEqual(tests_task.depends_on_task_ids, (followup_task.task_id,))

        self.assertEqual(result.teammate_assignments[0].task_id, root_task.task_id)

        report_entry = store.blackboard_entries[result.execution_report_entry_id]
        self.assertEqual(report_entry.payload.get("activation_ready_task_ids"), [root_task.task_id])
        self.assertEqual(
            report_entry.payload.get("deferred_task_ids"),
            [followup_task.task_id, tests_task.task_id],
        )
        self.assertEqual(report_entry.payload.get("activation_backlog_task_ids"), [])

    async def test_ingest_materializes_team_tasks_and_compiles_teammate_assignments(self) -> None:
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
                description="Advance the runtime.",
                workstreams=(
                    WorkstreamTemplate(
                        workstream_id="runtime-autonomy",
                        title="Runtime Autonomy",
                        summary="Make leader output actionable.",
                        team_name="Runtime",
                        acceptance_checks=("targeted tests",),
                        budget_max_teammates=2,
                    ),
                ),
            )
        )
        round_bundle = await materialize_planning_result(runtime, planning_result, created_by="superleader-1")
        leader_round = round_bundle.leader_rounds[0]

        result = await ingest_leader_turn_output(
            runtime,
            round_bundle.objective,
            leader_round,
            {
                "summary": "Split the next slice into protocol implementation and regression coverage.",
                "sequential_slices": [
                    {
                        "slice_id": "slice-implement-protocol-module",
                        "title": "Implement protocol module",
                        "goal": "Add leader output parsing and ingestion.",
                        "reason": "Need a runtime bridge from leader output to teammate work.",
                        "owned_paths": ["src/agent_orchestra/runtime/leader_output_protocol.py"],
                        "verification_commands": ["python3 -m unittest tests.test_leader_output_protocol -v"],
                    },
                    {
                        "slice_id": "slice-add-protocol-tests",
                        "title": "Add protocol tests",
                        "goal": "Cover happy-path and failure-path ingestion.",
                        "reason": "Need regression coverage for the bridge contract.",
                        "owned_paths": ["tests/test_leader_output_protocol.py"],
                        "verification_commands": ["python3 -m unittest tests.test_leader_output_protocol -v"],
                    },
                ],
                "parallel_slices": [],
            },
            working_dir="/tmp/agent-orchestra",
        )

        self.assertEqual(len(result.created_tasks), 2)
        self.assertEqual(len(result.teammate_assignments), 1)
        self.assertTrue(all(task.scope == TaskScope.TEAM for task in result.created_tasks))
        self.assertTrue(all(task.team_id == leader_round.team_id for task in result.created_tasks))
        self.assertTrue(all(task.derived_from == leader_round.runtime_task.task_id for task in result.created_tasks))
        self.assertEqual(
            {task.reason for task in result.created_tasks},
            {
                "Need a runtime bridge from leader output to teammate work.",
                "Need regression coverage for the bridge contract.",
            },
        )
        self.assertTrue(all(item.role == "teammate" for item in result.teammate_assignments))
        self.assertTrue(all(item.team_id == leader_round.team_id for item in result.teammate_assignments))
        self.assertEqual({item.task_id for item in result.teammate_assignments}, {result.created_tasks[0].task_id})
        report_entry = store.blackboard_entries[result.execution_report_entry_id]
        self.assertEqual(report_entry.entry_kind, BlackboardEntryKind.EXECUTION_REPORT)
        self.assertEqual(report_entry.payload.get("activation_ready_task_ids"), [result.created_tasks[0].task_id])
        self.assertEqual(report_entry.payload.get("deferred_task_ids"), [result.created_tasks[1].task_id])

    async def test_ingest_leaves_deferred_slices_on_task_surface_without_overflow_follow_up(self) -> None:
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
                description="Advance the runtime.",
                workstreams=(
                    WorkstreamTemplate(
                        workstream_id="runtime-autonomy",
                        title="Runtime Autonomy",
                        summary="Make leader output actionable.",
                        team_name="Runtime",
                        budget_max_teammates=1,
                    ),
                ),
            )
        )
        round_bundle = await materialize_planning_result(runtime, planning_result, created_by="superleader-1")
        leader_round = round_bundle.leader_rounds[0]

        result = await ingest_leader_turn_output(
            runtime,
            round_bundle.objective,
            leader_round,
            {
                "summary": "Two teammate tasks were discovered.",
                "sequential_slices": [
                    {
                        "slice_id": "slice-task-one",
                        "title": "Task one",
                        "goal": "Implement the first half.",
                        "reason": "First half of the bridge.",
                    },
                    {
                        "slice_id": "slice-task-two",
                        "title": "Task two",
                        "goal": "Implement the second half.",
                        "reason": "Second half of the bridge.",
                    },
                ],
                "parallel_slices": [],
            },
        )

        self.assertEqual(len(result.created_tasks), 2)
        self.assertEqual(len(result.teammate_assignments), 1)
        self.assertFalse(result.proposal_entry_ids)
        self.assertFalse(result.blocker_entry_ids)
        report_entry = store.blackboard_entries[result.execution_report_entry_id]
        self.assertEqual(report_entry.payload.get("activation_ready_task_ids"), [result.created_tasks[0].task_id])
        self.assertEqual(report_entry.payload.get("deferred_task_ids"), [result.created_tasks[1].task_id])

    async def test_ingest_rejects_illegal_non_team_scope(self) -> None:
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
                description="Advance the runtime.",
                workstreams=(
                    WorkstreamTemplate(
                        workstream_id="runtime-autonomy",
                        title="Runtime Autonomy",
                        summary="Make leader output actionable.",
                        team_name="Runtime",
                    ),
                ),
            )
        )
        round_bundle = await materialize_planning_result(runtime, planning_result, created_by="superleader-1")
        leader_round = round_bundle.leader_rounds[0]

        with self.assertRaises(ValueError):
            await ingest_leader_turn_output(
                runtime,
                round_bundle.objective,
                leader_round,
                {
                    "summary": "Illegal scope payload.",
                    "sequential_slices": [
                        {
                            "slice_id": "slice-illegal",
                            "title": "Illegal task",
                            "goal": "Attempt cross-scope mutation.",
                            "reason": "Should not be allowed.",
                            "scope": "leader_lane",
                        }
                    ],
                    "parallel_slices": [],
                },
            )

    def test_parse_rejects_malformed_json(self) -> None:
        with self.assertRaises(ValueError):
            parse_leader_turn_output("{not valid json")

    def test_parse_rejects_legacy_teammate_tasks_protocol(self) -> None:
        with self.assertRaises(ValueError):
            parse_leader_turn_output(
                {
                    "summary": "Legacy protocol should be rejected.",
                    "teammate_tasks": [
                        {
                            "title": "Old task",
                            "goal": "Old goal",
                            "reason": "Old reason",
                        }
                    ],
                }
            )

    async def test_leader_assignment_instructions_require_json_protocol_output(self) -> None:
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
                description="Advance the runtime.",
                workstreams=(
                    WorkstreamTemplate(
                        workstream_id="runtime-autonomy",
                        title="Runtime Autonomy",
                        summary="Make leader output actionable.",
                        team_name="Runtime",
                    ),
                ),
            )
        )
        round_bundle = await materialize_planning_result(runtime, planning_result, created_by="superleader-1")

        assignment = compile_leader_assignments(round_bundle, backend="in_process")[0]

        self.assertIn("Return a JSON object", assignment.instructions)
        self.assertIn("sequential_slices", assignment.instructions)
        self.assertIn("parallel_slices", assignment.instructions)
        self.assertNotIn("max_concurrency", assignment.instructions)
        self.assertNotIn("max_concurrency", assignment.metadata["budget"])
        self.assertNotIn("Legacy teammate_tasks", assignment.instructions)

    async def test_leader_assignment_instructions_include_lane_default_scope(self) -> None:
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
                description="Advance the runtime.",
                workstreams=(
                    WorkstreamTemplate(
                        workstream_id="coordination-transaction-and-session-truth-convergence",
                        title="Coordination Transaction And Session Truth Convergence",
                        summary="Unify session truth and transaction boundaries.",
                        team_name="Runtime",
                        budget_max_teammates=1,
                        metadata={
                            "gap_id": "coordination-transaction-and-session-truth-convergence",
                            "rationale": "Need durable coordination truth across mailbox, task, and session state.",
                            "source_path": "resource/knowledge/agent-orchestra-runtime/first-batch-online-collaboration-execution-pack.md",
                            "owned_paths": [
                                "src/agent_orchestra/runtime/group_runtime.py",
                                "resource/knowledge/agent-orchestra-runtime/implementation-status.md",
                            ],
                            "verification_commands": [
                                "python3 -m unittest tests.test_runtime -v",
                            ],
                        },
                    ),
                ),
            )
        )
        round_bundle = await materialize_planning_result(runtime, planning_result, created_by="superleader-1")

        assignment = compile_leader_assignments(round_bundle, backend="in_process")[0]

        self.assertIn("Lane defaults:", assignment.instructions)
        self.assertIn("coordination-transaction-and-session-truth-convergence", assignment.instructions)
        self.assertIn("resource/knowledge/agent-orchestra-runtime/implementation-status.md", assignment.instructions)
        self.assertIn("python3 -m unittest tests.test_runtime -v", assignment.instructions)

    async def test_ingest_merges_lane_default_scope_into_created_tasks_and_assignments(self) -> None:
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
                description="Advance the runtime.",
                workstreams=(
                    WorkstreamTemplate(
                        workstream_id="coordination-transaction-and-session-truth-convergence",
                        title="Coordination Transaction And Session Truth Convergence",
                        summary="Unify session truth and transaction boundaries.",
                        team_name="Runtime",
                        budget_max_teammates=1,
                        metadata={
                            "gap_id": "coordination-transaction-and-session-truth-convergence",
                            "owned_paths": [
                                "src/agent_orchestra/runtime/group_runtime.py",
                                "resource/knowledge/agent-orchestra-runtime/implementation-status.md",
                            ],
                            "verification_commands": [
                                "python3 -m unittest tests.test_runtime -v",
                            ],
                        },
                    ),
                ),
            )
        )
        round_bundle = await materialize_planning_result(runtime, planning_result, created_by="superleader-1")
        leader_round = round_bundle.leader_rounds[0]

        result = await ingest_leader_turn_output(
            runtime,
            round_bundle.objective,
            leader_round,
            {
                "summary": "Create one implementation slice without restating lane defaults.",
                "sequential_slices": [
                    {
                        "slice_id": "slice-core",
                        "title": "Implement core transaction bridge",
                        "goal": "Unify receipt, result, and session commit semantics.",
                        "reason": "Need a concrete first cut for the coordination path.",
                    }
                ],
                "parallel_slices": [],
            },
        )

        self.assertEqual(len(result.created_tasks), 1)
        self.assertEqual(len(result.teammate_assignments), 1)

        created_task = result.created_tasks[0]
        assignment = result.teammate_assignments[0]
        self.assertEqual(
            created_task.owned_paths,
            (
                "src/agent_orchestra/runtime/group_runtime.py",
                "resource/knowledge/agent-orchestra-runtime/implementation-status.md",
            ),
        )
        self.assertEqual(
            created_task.verification_commands,
            ("python3 -m unittest tests.test_runtime -v",),
        )
        self.assertEqual(
            assignment.metadata["owned_paths"],
            [
                "src/agent_orchestra/runtime/group_runtime.py",
                "resource/knowledge/agent-orchestra-runtime/implementation-status.md",
            ],
        )
        self.assertEqual(
            assignment.metadata["verification_commands"],
            ["python3 -m unittest tests.test_runtime -v"],
        )
