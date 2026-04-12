from __future__ import annotations

import asyncio
import tempfile
from datetime import datetime
from typing import Any
from unittest import IsolatedAsyncioTestCase

from agent_orchestra.bus.in_memory import InMemoryEventBus
from agent_orchestra.contracts.blackboard import BlackboardEntryKind
from agent_orchestra.contracts.delivery import DeliveryState, DeliveryStateKind, DeliveryStatus
from agent_orchestra.contracts.enums import EventKind, TaskScope, TaskStatus, WorkerStatus
from agent_orchestra.contracts.execution import (
    ResidentCoordinatorPhase,
    WorkerAssignment,
    WorkerBudget,
    WorkerRecord,
    WorkerSession,
)
from agent_orchestra.contracts.session_continuity import ConversationHeadKind
from agent_orchestra.contracts.session_memory import AgentTurnKind
from agent_orchestra.contracts.task_review import TaskReviewExperienceContext, TaskReviewStance
from agent_orchestra.contracts.runner import AgentRunner, RunnerHealth, RunnerStreamEvent, RunnerTurnRequest, RunnerTurnResult
from agent_orchestra.planning.template import ObjectiveTemplate, WorkstreamTemplate
from agent_orchestra.planning.template_planner import TemplatePlanner
from agent_orchestra.runtime.bootstrap_round import materialize_planning_result
from agent_orchestra.runtime.group_runtime import GroupRuntime
from agent_orchestra.runtime.leader_loop import build_runtime_role_profiles
from agent_orchestra.runtime.teammate_runtime import ResidentTeammateRunResult
from agent_orchestra.runtime.teammate_work_surface import (
    TeammateWorkSurface,
    build_pending_teammate_assignment,
    resident_teammate_session_id,
)
from agent_orchestra.runtime.backends.in_process import InProcessLaunchBackend
from agent_orchestra.runtime.worker_supervisor import DefaultWorkerSupervisor
from agent_orchestra.storage.in_memory import InMemoryOrchestrationStore
from agent_orchestra.tools.mailbox import MailboxEnvelope
from agent_orchestra.tools.permission_protocol import PermissionDecision
from agent_orchestra.runtime.protocol_bridge import InMemoryMailboxBridge


class _NoopRunner:
    async def run(self, request):  # pragma: no cover - not used by these tests
        raise AssertionError("runner should not be called in teammate work surface unit tests")


class _CompletedRunner(AgentRunner):
    async def run_turn(self, request: RunnerTurnRequest) -> RunnerTurnResult:
        return RunnerTurnResult(
            response_id=f"resp-{request.agent_id}",
            output_text=f"{request.agent_id} completed {request.metadata.get('task_id')}",
            status="completed",
        )

    async def stream_turn(self, request: RunnerTurnRequest):
        if False:
            yield RunnerStreamEvent(kind=EventKind.RUNNER_COMPLETED)

    async def cancel(self, run_id: str) -> None:
        return None

    async def healthcheck(self) -> RunnerHealth:
        return RunnerHealth(healthy=True, provider="fake")


class _FailedRunner(AgentRunner):
    async def run_turn(self, request: RunnerTurnRequest) -> RunnerTurnResult:
        return RunnerTurnResult(
            response_id=f"resp-{request.agent_id}",
            output_text="",
            status="failed",
            error_text=f"{request.agent_id} failed {request.metadata.get('task_id')}",
        )

    async def stream_turn(self, request: RunnerTurnRequest):
        if False:
            yield RunnerStreamEvent(kind=EventKind.RUNNER_COMPLETED)

    async def cancel(self, run_id: str) -> None:
        return None

    async def healthcheck(self) -> RunnerHealth:
        return RunnerHealth(healthy=True, provider="fake")


def _coordination_session_snapshot(commit: object) -> Any | None:
    for attribute in ("session_snapshot", "agent_session", "slot_session"):
        value = getattr(commit, attribute, None)
        if value is not None:
            return value
    return None


class _TrackingCoordinationStore(InMemoryOrchestrationStore):
    def __init__(self) -> None:
        super().__init__()
        self.coordination_events: list[str] = []
        self.directed_receipt_commits: list[object] = []
        self.teammate_result_commits: list[object] = []
        self.authority_request_commits: list[object] = []

    async def save_blackboard_entry(self, entry) -> None:
        self.coordination_events.append("store.save_blackboard_entry")
        await super().save_blackboard_entry(entry)

    async def save_agent_session(self, session) -> None:
        self.coordination_events.append("store.save_agent_session")
        await super().save_agent_session(session)

    async def commit_directed_task_receipt(self, commit: object) -> None:
        self.coordination_events.append("store.commit_directed_task_receipt")
        self.directed_receipt_commits.append(commit)
        task = getattr(commit, "task", None)
        if task is not None:
            await self.save_task(task)
        blackboard_entry = getattr(commit, "blackboard_entry", None)
        if blackboard_entry is not None:
            await self.save_blackboard_entry(blackboard_entry)
        delivery_state = getattr(commit, "delivery_state", None)
        if delivery_state is not None:
            await self.save_delivery_state(delivery_state)
        protocol_bus_cursor = getattr(commit, "protocol_bus_cursor", None)
        if protocol_bus_cursor is not None:
            await self.save_protocol_bus_cursor(
                stream=protocol_bus_cursor.stream,
                consumer=protocol_bus_cursor.consumer,
                cursor=dict(protocol_bus_cursor.cursor),
            )
        agent_session = _coordination_session_snapshot(commit)
        if agent_session is not None:
            await self.save_agent_session(agent_session)

    async def commit_teammate_result(self, commit: object) -> None:
        self.coordination_events.append("store.commit_teammate_result")
        self.teammate_result_commits.append(commit)
        task = getattr(commit, "task", None)
        if task is not None:
            await self.save_task(task)
        blackboard_entry = getattr(commit, "blackboard_entry", None)
        if blackboard_entry is not None:
            await self.save_blackboard_entry(blackboard_entry)
        delivery_state = getattr(commit, "delivery_state", None)
        if delivery_state is not None:
            await self.save_delivery_state(delivery_state)
        agent_session = _coordination_session_snapshot(commit)
        if agent_session is not None:
            await self.save_agent_session(agent_session)

    async def commit_authority_request(self, commit: object) -> None:
        self.coordination_events.append("store.commit_authority_request")
        self.authority_request_commits.append(commit)
        task = getattr(commit, "task", None)
        if task is not None:
            await self.save_task(task)
        blackboard_entry = getattr(commit, "blackboard_entry", None)
        if blackboard_entry is not None:
            await self.save_blackboard_entry(blackboard_entry)
        delivery_state = getattr(commit, "delivery_state", None)
        if delivery_state is not None:
            await self.save_delivery_state(delivery_state)
        agent_session = _coordination_session_snapshot(commit)
        if agent_session is not None:
            await self.save_agent_session(agent_session)


class _TrackingMailboxBridge(InMemoryMailboxBridge):
    def __init__(self, *, coordination_events: list[str]) -> None:
        super().__init__()
        self._coordination_events = coordination_events

    async def send(self, envelope: MailboxEnvelope) -> MailboxEnvelope:
        if envelope.subject in {"task.receipt", "task.result"}:
            self._coordination_events.append(f"mailbox.send:{envelope.subject}")
        return await super().send(envelope)


class TeammateWorkSurfaceTest(IsolatedAsyncioTestCase):
    async def test_autonomous_claim_assignment_includes_task_review_context(self) -> None:
        store = InMemoryOrchestrationStore()
        bus = InMemoryEventBus()
        supervisor = DefaultWorkerSupervisor(
            store=store,
            launch_backends={"in_process": InProcessLaunchBackend()},
            runner=_CompletedRunner(),
        )
        runtime = GroupRuntime(
            store=store,
            bus=bus,
            launch_backends={"in_process": InProcessLaunchBackend()},
            supervisor=supervisor,
        )
        planner = TemplatePlanner()
        mailbox = InMemoryMailboxBridge()

        await runtime.create_group("group-a")
        planning_result = await planner.build_initial_plan(
            ObjectiveTemplate(
                objective_id="obj-review-context",
                group_id="group-a",
                title="Review context",
                description="Inject task review context into claim assignments.",
                workstreams=(
                    WorkstreamTemplate(
                        workstream_id="runtime",
                        title="Runtime",
                        summary="Own the runtime lane.",
                        team_name="Runtime",
                        budget_max_teammates=1,
                        budget_max_iterations=1,
                    ),
                ),
            )
        )
        bundle = await materialize_planning_result(runtime, planning_result, created_by="superleader-1")
        leader_round = bundle.leader_rounds[0]
        task = await runtime.submit_task(
            group_id=bundle.objective.group_id,
            team_id=leader_round.team_id,
            lane_id=leader_round.lane_id,
            goal="Implement review-aware claim context",
            scope=TaskScope.TEAM,
            created_by=leader_round.leader_task.leader_id,
        )
        await runtime.upsert_task_review(
            task_id=task.task_id,
            reviewer_agent_id=f"{leader_round.team_id}:teammate:2",
            reviewer_role="teammate",
            based_on_task_version=1,
            based_on_knowledge_epoch=1,
            stance=TaskReviewStance.GOOD_FIT,
            summary="I already touched the runtime path.",
            relation_to_my_work="Runtime files overlap with my previous changes.",
            confidence=0.9,
            experience_context=TaskReviewExperienceContext(
                touched_paths=("src/agent_orchestra/runtime/group_runtime.py",),
            ),
            reviewed_at="2026-04-07T12:30:00+00:00",
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            surface = TeammateWorkSurface(
                runtime=runtime,
                mailbox=mailbox,
                objective=bundle.objective,
                leader_round=leader_round,
                backend="in_process",
                working_dir=tmpdir,
                turn_index=1,
                session_host=supervisor.session_host,
            )
            assignment, _claim_session_id, _claim_source = await surface._claim_autonomous_assignment_for_slot(1)

        self.assertIsNotNone(assignment)
        assert assignment is not None
        self.assertIn("task_review_digest", assignment.metadata)
        self.assertIn("task_review_slots", assignment.metadata)
        digest = assignment.metadata["task_review_digest"]
        self.assertEqual(digest["slot_count"], 1)
        self.assertIn("Task review digest:", assignment.instructions)
        self.assertIn("I already touched the runtime path.", assignment.instructions)

    async def test_acquire_assignments_reuses_host_owned_activation_profile_when_surface_context_is_missing(
        self,
    ) -> None:
        store = InMemoryOrchestrationStore()
        bus = InMemoryEventBus()
        supervisor = DefaultWorkerSupervisor(
            store=store,
            launch_backends={"in_process": InProcessLaunchBackend()},
            runner=_NoopRunner(),
        )
        runtime = GroupRuntime(
            store=store,
            bus=bus,
            launch_backends={"in_process": InProcessLaunchBackend()},
            supervisor=supervisor,
        )
        planner = TemplatePlanner()
        mailbox = InMemoryMailboxBridge()

        await runtime.create_group("group-a")
        planning_result = await planner.build_initial_plan(
            ObjectiveTemplate(
                objective_id="obj-runtime",
                group_id="group-a",
                title="Build runtime",
                description="Reuse the slot activation profile from the session host.",
                workstreams=(
                    WorkstreamTemplate(
                        workstream_id="runtime",
                        title="Runtime",
                        summary="Own the runtime lane.",
                        team_name="Runtime",
                        budget_max_teammates=1,
                        budget_max_iterations=1,
                    ),
                ),
            )
        )
        bundle = await materialize_planning_result(runtime, planning_result, created_by="superleader-1")
        leader_round = bundle.leader_rounds[0]
        teammate_worker_id = f"{leader_round.team_id}:teammate:1"
        continuity = await runtime.new_session(
            group_id=bundle.objective.group_id,
            objective_id=bundle.objective.objective_id,
            title="Teammate authority turn ledger",
        )
        session_id = resident_teammate_session_id(leader_round=leader_round, teammate_slot=1)
        teammate_profile = build_runtime_role_profiles()["teammate_in_process_fast"]

        task = await runtime.submit_task(
            group_id=bundle.objective.group_id,
            lane_id=leader_round.lane_id,
            team_id=leader_round.team_id,
            goal="Continue teammate work from the host-owned slot profile",
            scope=TaskScope.TEAM,
            created_by=leader_round.leader_task.leader_id,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            priming_surface = TeammateWorkSurface(
                runtime=runtime,
                mailbox=mailbox,
                objective=bundle.objective,
                leader_round=leader_round,
                backend="in_process",
                working_dir=tmpdir,
                turn_index=1,
                role_profile=teammate_profile,
                session_host=supervisor.session_host,
            )
            await priming_surface.ensure_slot_session(worker_id=teammate_worker_id)

            continuation_surface = TeammateWorkSurface(
                runtime=runtime,
                mailbox=mailbox,
                objective=bundle.objective,
                leader_round=leader_round,
                backend=None,
                working_dir=None,
                turn_index=2,
                role_profile=None,
                session_host=supervisor.session_host,
            )
            acquire_result = await continuation_surface.acquire_assignments(
                existing_assignments=(),
                limit=1,
            )

        self.assertEqual(len(acquire_result.assignments), 1)
        assignment = acquire_result.assignments[0]
        self.assertEqual(assignment.task_id, task.task_id)
        self.assertEqual(assignment.backend, "in_process")
        self.assertEqual(assignment.working_dir, tmpdir)
        self.assertIsNotNone(assignment.role_profile)
        assert assignment.role_profile is not None
        self.assertEqual(assignment.role_profile.profile_id, teammate_profile.profile_id)
        self.assertEqual(assignment.metadata["role_profile_id"], teammate_profile.profile_id)

        session_snapshot = await supervisor.session_host.snapshot_session(session_id)
        self.assertIsNotNone(session_snapshot)
        assert session_snapshot is not None
        activation_profile = session_snapshot["metadata"]["activation_profile"]
        self.assertEqual(activation_profile["backend"], "in_process")
        self.assertEqual(activation_profile["working_dir"], tmpdir)
        self.assertEqual(
            activation_profile["role_profile"]["profile_id"],
            teammate_profile.profile_id,
        )

    async def test_acquire_assignments_prefers_directed_mailbox_and_persists_cursor(self) -> None:
        store = InMemoryOrchestrationStore()
        bus = InMemoryEventBus()
        supervisor = DefaultWorkerSupervisor(
            store=store,
            launch_backends={"in_process": InProcessLaunchBackend()},
            runner=_NoopRunner(),
        )
        runtime = GroupRuntime(
            store=store,
            bus=bus,
            launch_backends={"in_process": InProcessLaunchBackend()},
            supervisor=supervisor,
        )
        planner = TemplatePlanner()
        mailbox = InMemoryMailboxBridge()

        await runtime.create_group("group-a")
        planning_result = await planner.build_initial_plan(
            ObjectiveTemplate(
                objective_id="obj-runtime",
                group_id="group-a",
                title="Build runtime",
                description="Use teammate-owned work surface.",
                workstreams=(
                    WorkstreamTemplate(
                        workstream_id="runtime",
                        title="Runtime",
                        summary="Own the runtime lane.",
                        team_name="Runtime",
                        budget_max_teammates=1,
                        budget_max_iterations=1,
                    ),
                ),
            )
        )
        bundle = await materialize_planning_result(runtime, planning_result, created_by="superleader-1")
        leader_round = bundle.leader_rounds[0]
        teammate_slot = 1
        teammate_worker_id = f"{leader_round.team_id}:teammate:{teammate_slot}"
        session_id = resident_teammate_session_id(leader_round=leader_round, teammate_slot=teammate_slot)

        task = await runtime.submit_task(
            group_id=bundle.objective.group_id,
            lane_id=leader_round.lane_id,
            team_id=leader_round.team_id,
            goal="Handle directed runtime task",
            scope=TaskScope.TEAM,
            created_by=leader_round.leader_task.leader_id,
        )
        envelope = await mailbox.send(
            MailboxEnvelope(
                sender=leader_round.leader_task.leader_id,
                recipient=teammate_worker_id,
                subject="task.directed",
                group_id=bundle.objective.group_id,
                lane_id=leader_round.lane_id,
                team_id=leader_round.team_id,
                payload={"task_id": task.task_id},
            )
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            surface = TeammateWorkSurface(
                runtime=runtime,
                mailbox=mailbox,
                objective=bundle.objective,
                leader_round=leader_round,
                backend="in_process",
                working_dir=tmpdir,
                turn_index=1,
                session_host=supervisor.session_host,
            )
            acquire_result = await surface.acquire_assignments(existing_assignments=(), limit=1)

        self.assertEqual(len(acquire_result.assignments), 1)
        self.assertEqual(acquire_result.directed_claimed_task_ids, (task.task_id,))
        self.assertEqual(acquire_result.autonomous_claimed_task_ids, ())
        self.assertEqual(acquire_result.processed_mailbox_envelope_ids, (envelope.envelope_id,))
        self.assertTrue(acquire_result.teammate_execution_evidence)

        cursor = await mailbox.get_cursor(teammate_worker_id)
        self.assertEqual(cursor.last_envelope_id, envelope.envelope_id)
        session_snapshot = await supervisor.session_host.snapshot_session(session_id)
        self.assertIsNotNone(session_snapshot)
        assert session_snapshot is not None
        self.assertEqual(
            session_snapshot["mailbox_cursor"]["last_envelope_id"],
            envelope.envelope_id,
        )
        self.assertEqual(session_snapshot["current_directive_ids"], [task.task_id])

    async def test_acquire_assignments_emits_task_receipt_and_updates_delivery_snapshot(self) -> None:
        store = InMemoryOrchestrationStore()
        bus = InMemoryEventBus()
        supervisor = DefaultWorkerSupervisor(
            store=store,
            launch_backends={"in_process": InProcessLaunchBackend()},
            runner=_NoopRunner(),
        )
        runtime = GroupRuntime(
            store=store,
            bus=bus,
            launch_backends={"in_process": InProcessLaunchBackend()},
            supervisor=supervisor,
        )
        planner = TemplatePlanner()
        mailbox = InMemoryMailboxBridge()

        await runtime.create_group("group-a")
        planning_result = await planner.build_initial_plan(
            ObjectiveTemplate(
                objective_id="obj-runtime",
                group_id="group-a",
                title="Build runtime",
                description="Emit task.receipt during directed teammate claim materialization.",
                workstreams=(
                    WorkstreamTemplate(
                        workstream_id="runtime",
                        title="Runtime",
                        summary="Own the runtime lane.",
                        team_name="Runtime",
                        budget_max_teammates=1,
                        budget_max_iterations=1,
                    ),
                ),
            )
        )
        bundle = await materialize_planning_result(runtime, planning_result, created_by="superleader-1")
        leader_round = bundle.leader_rounds[0]
        teammate_worker_id = f"{leader_round.team_id}:teammate:1"
        delivery_id = f"{bundle.objective.objective_id}:lane:{leader_round.lane_id}"

        task = await runtime.submit_task(
            group_id=bundle.objective.group_id,
            lane_id=leader_round.lane_id,
            team_id=leader_round.team_id,
            goal="Materialize directed teammate claim",
            scope=TaskScope.TEAM,
            created_by=leader_round.leader_task.leader_id,
        )
        await runtime.upsert_task_review(
            task_id=task.task_id,
            reviewer_agent_id=f"{leader_round.team_id}:teammate:2",
            reviewer_role="teammate",
            based_on_task_version=1,
            based_on_knowledge_epoch=1,
            stance=TaskReviewStance.GOOD_FIT,
            summary="Directed claim has relevant prior context.",
            relation_to_my_work="I already worked on the same lane.",
            confidence=0.8,
            experience_context=TaskReviewExperienceContext(
                touched_paths=("src/agent_orchestra/runtime/teammate_work_surface.py",),
            ),
            reviewed_at="2026-04-07T12:15:00+00:00",
        )
        await store.save_delivery_state(
            DeliveryState(
                delivery_id=delivery_id,
                objective_id=bundle.objective.objective_id,
                kind=DeliveryStateKind.LANE,
                status=DeliveryStatus.RUNNING,
                lane_id=leader_round.lane_id,
                team_id=leader_round.team_id,
                pending_task_ids=(task.task_id,),
                summary="Lane is running.",
            )
        )
        envelope = await mailbox.send(
            MailboxEnvelope(
                sender=leader_round.leader_task.leader_id,
                recipient=teammate_worker_id,
                subject="task.directed",
                group_id=bundle.objective.group_id,
                lane_id=leader_round.lane_id,
                team_id=leader_round.team_id,
                payload={
                    "directive_id": "dir-1",
                    "correlation_id": "corr-1",
                    "task_id": task.task_id,
                },
            )
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            surface = TeammateWorkSurface(
                runtime=runtime,
                mailbox=mailbox,
                objective=bundle.objective,
                leader_round=leader_round,
                backend="in_process",
                working_dir=tmpdir,
                turn_index=1,
                session_host=supervisor.session_host,
            )
            acquire_result = await surface.acquire_assignments(existing_assignments=(), limit=1)

        self.assertEqual(len(acquire_result.assignments), 1)
        self.assertEqual(len(acquire_result.mailbox_envelopes), 1)
        receipt_envelope = acquire_result.mailbox_envelopes[0]
        self.assertEqual(receipt_envelope.subject, "task.receipt")
        self.assertEqual(receipt_envelope.payload["protocol"]["message_type"], "task.receipt")
        self.assertEqual(receipt_envelope.payload["task_id"], task.task_id)
        self.assertEqual(
            receipt_envelope.payload["consumer_cursor"]["last_envelope_id"],
            envelope.envelope_id,
        )
        team_entries = await store.list_blackboard_entries(f"group-a:team:{leader_round.team_id}")
        self.assertTrue(any(entry.task_id == task.task_id for entry in team_entries))
        saved_state = await store.get_delivery_state(delivery_id)
        self.assertIsNotNone(saved_state)
        assert saved_state is not None
        self.assertIn(task.task_id, saved_state.active_task_ids)
        self.assertNotIn(task.task_id, saved_state.pending_task_ids)
        coordination = saved_state.metadata["teammate_coordination"]
        self.assertEqual(coordination["receipt_count"], 1)
        self.assertEqual(coordination["last_receipt_task_id"], task.task_id)
        self.assertEqual(coordination["last_receipt_worker_id"], teammate_worker_id)

    async def test_acquire_assignments_consumes_authority_decision_without_materializing_directive_work(self) -> None:
        store = InMemoryOrchestrationStore()
        bus = InMemoryEventBus()
        supervisor = DefaultWorkerSupervisor(
            store=store,
            launch_backends={"in_process": InProcessLaunchBackend()},
            runner=_NoopRunner(),
        )
        runtime = GroupRuntime(
            store=store,
            bus=bus,
            launch_backends={"in_process": InProcessLaunchBackend()},
            supervisor=supervisor,
        )
        planner = TemplatePlanner()
        mailbox = InMemoryMailboxBridge()

        await runtime.create_group("group-a")
        planning_result = await planner.build_initial_plan(
            ObjectiveTemplate(
                objective_id="obj-runtime",
                group_id="group-a",
                title="Ignore authority control envelope",
                description="Control-plane authority messages must not materialize directed task claims.",
                workstreams=(
                    WorkstreamTemplate(
                        workstream_id="runtime",
                        title="Runtime",
                        summary="Own the runtime lane.",
                        team_name="Runtime",
                        budget_max_teammates=1,
                        budget_max_iterations=1,
                    ),
                ),
            )
        )
        bundle = await materialize_planning_result(runtime, planning_result, created_by="superleader-1")
        leader_round = bundle.leader_rounds[0]
        teammate_worker_id = f"{leader_round.team_id}:teammate:1"
        session_id = resident_teammate_session_id(leader_round=leader_round, teammate_slot=1)

        envelope = await mailbox.send(
            MailboxEnvelope(
                sender=leader_round.leader_task.leader_id,
                recipient=teammate_worker_id,
                subject="authority.decision",
                group_id=bundle.objective.group_id,
                lane_id=leader_round.lane_id,
                team_id=leader_round.team_id,
                payload={
                    "task_id": "task-authority-1",
                    "authority_request": {
                        "request_id": "auth-req-1",
                        "assignment_id": "task-authority-1:assignment",
                        "worker_id": teammate_worker_id,
                        "task_id": "task-authority-1",
                        "requested_paths": ["docs/runtime.md"],
                    },
                    "authority_decision": {
                        "request_id": "auth-req-1",
                        "decision": "grant",
                        "actor_id": leader_round.leader_task.leader_id,
                        "scope_class": "soft_scope",
                        "granted_paths": ["docs/runtime.md"],
                    },
                },
            )
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            surface = TeammateWorkSurface(
                runtime=runtime,
                mailbox=mailbox,
                objective=bundle.objective,
                leader_round=leader_round,
                backend="in_process",
                working_dir=tmpdir,
                turn_index=1,
                session_host=supervisor.session_host,
            )
            acquire_result = await surface.acquire_assignments(existing_assignments=(), limit=1)

        self.assertEqual(len(acquire_result.assignments), 0)
        self.assertEqual(acquire_result.directed_claimed_task_ids, ())
        self.assertEqual(acquire_result.autonomous_claimed_task_ids, ())
        self.assertEqual(
            acquire_result.processed_mailbox_envelope_ids,
            (envelope.envelope_id,),
        )

        cursor = await mailbox.get_cursor(teammate_worker_id)
        self.assertEqual(cursor.last_envelope_id, envelope.envelope_id)
        session_snapshot = await supervisor.session_host.snapshot_session(session_id)
        self.assertIsNotNone(session_snapshot)
        assert session_snapshot is not None
        self.assertEqual(session_snapshot["current_directive_ids"], [])
        metadata = session_snapshot["metadata"]
        self.assertEqual(metadata["authority_request_id"], "auth-req-1")
        self.assertEqual(metadata["authority_waiting_task_id"], "task-authority-1")
        self.assertEqual(metadata["authority_last_decision"], "grant")
        self.assertEqual(metadata["authority_last_decision_actor_id"], leader_round.leader_task.leader_id)
        self.assertFalse(metadata["authority_waiting"])
        self.assertEqual(metadata["wake_request_count"], 1)
        self.assertEqual(metadata["last_wake_requested_by"], leader_round.leader_task.leader_id)
        self.assertEqual(metadata["authority_last_relay_subject"], "authority.decision")
        self.assertEqual(metadata["authority_last_relay_envelope_id"], envelope.envelope_id)
        self.assertTrue(metadata["authority_relay_consumed"])
        self.assertTrue(metadata["authority_wake_recorded"])
        self.assertEqual(metadata["authority_completion_status"], "grant_resumed")
        self.assertIsInstance(metadata["authority_last_relay_consumed_at"], str)
        datetime.fromisoformat(metadata["authority_last_relay_consumed_at"])

    async def test_acquire_assignments_records_relay_pending_state_for_authority_control_without_decision(self) -> None:
        store = InMemoryOrchestrationStore()
        bus = InMemoryEventBus()
        supervisor = DefaultWorkerSupervisor(
            store=store,
            launch_backends={"in_process": InProcessLaunchBackend()},
            runner=_NoopRunner(),
        )
        runtime = GroupRuntime(
            store=store,
            bus=bus,
            launch_backends={"in_process": InProcessLaunchBackend()},
            supervisor=supervisor,
        )
        planner = TemplatePlanner()
        mailbox = InMemoryMailboxBridge()

        await runtime.create_group("group-a")
        planning_result = await planner.build_initial_plan(
            ObjectiveTemplate(
                objective_id="obj-runtime",
                group_id="group-a",
                title="Track authority relay pending state",
                description="Authority relay envelopes without a decision should still update closure metadata.",
                workstreams=(
                    WorkstreamTemplate(
                        workstream_id="runtime",
                        title="Runtime",
                        summary="Own the runtime lane.",
                        team_name="Runtime",
                        budget_max_teammates=1,
                        budget_max_iterations=1,
                    ),
                ),
            )
        )
        bundle = await materialize_planning_result(runtime, planning_result, created_by="superleader-1")
        leader_round = bundle.leader_rounds[0]
        teammate_worker_id = f"{leader_round.team_id}:teammate:1"
        session_id = resident_teammate_session_id(leader_round=leader_round, teammate_slot=1)

        envelope = await mailbox.send(
            MailboxEnvelope(
                sender=leader_round.leader_task.leader_id,
                recipient=teammate_worker_id,
                subject="authority.writeback",
                group_id=bundle.objective.group_id,
                lane_id=leader_round.lane_id,
                team_id=leader_round.team_id,
                payload={
                    "task_id": "task-authority-pending-1",
                    "authority_request": {
                        "request_id": "auth-req-pending-1",
                        "assignment_id": "task-authority-pending-1:assignment",
                        "worker_id": teammate_worker_id,
                        "task_id": "task-authority-pending-1",
                        "requested_paths": ["docs/runtime.md"],
                    },
                },
            )
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            surface = TeammateWorkSurface(
                runtime=runtime,
                mailbox=mailbox,
                objective=bundle.objective,
                leader_round=leader_round,
                backend="in_process",
                working_dir=tmpdir,
                turn_index=1,
                session_host=supervisor.session_host,
            )
            acquire_result = await surface.acquire_assignments(existing_assignments=(), limit=1)

        self.assertEqual(len(acquire_result.assignments), 0)
        self.assertEqual(
            acquire_result.processed_mailbox_envelope_ids,
            (envelope.envelope_id,),
        )
        session_snapshot = await supervisor.session_host.snapshot_session(session_id)
        self.assertIsNotNone(session_snapshot)
        assert session_snapshot is not None
        metadata = session_snapshot["metadata"]
        self.assertEqual(metadata["authority_request_id"], "auth-req-pending-1")
        self.assertEqual(metadata["authority_waiting_task_id"], "task-authority-pending-1")
        self.assertEqual(metadata["authority_last_relay_subject"], "authority.writeback")
        self.assertEqual(metadata["authority_last_relay_envelope_id"], envelope.envelope_id)
        self.assertTrue(metadata["authority_relay_consumed"])
        self.assertTrue(metadata["authority_waiting"])
        self.assertEqual(metadata["authority_completion_status"], "relay_pending")
        self.assertIsInstance(metadata["authority_last_relay_consumed_at"], str)
        datetime.fromisoformat(metadata["authority_last_relay_consumed_at"])

    async def test_acquire_assignments_routes_directed_claim_through_store_coordination_commit(self) -> None:
        store = _TrackingCoordinationStore()
        bus = InMemoryEventBus()
        supervisor = DefaultWorkerSupervisor(
            store=store,
            launch_backends={"in_process": InProcessLaunchBackend()},
            runner=_NoopRunner(),
        )
        runtime = GroupRuntime(
            store=store,
            bus=bus,
            launch_backends={"in_process": InProcessLaunchBackend()},
            supervisor=supervisor,
        )
        planner = TemplatePlanner()
        mailbox = _TrackingMailboxBridge(coordination_events=store.coordination_events)

        await runtime.create_group("group-a")
        planning_result = await planner.build_initial_plan(
            ObjectiveTemplate(
                objective_id="obj-runtime",
                group_id="group-a",
                title="Build runtime",
                description="Route directed claim materialization through the store coordination commit.",
                workstreams=(
                    WorkstreamTemplate(
                        workstream_id="runtime",
                        title="Runtime",
                        summary="Own the runtime lane.",
                        team_name="Runtime",
                        budget_max_teammates=1,
                        budget_max_iterations=1,
                    ),
                ),
            )
        )
        bundle = await materialize_planning_result(runtime, planning_result, created_by="superleader-1")
        leader_round = bundle.leader_rounds[0]
        teammate_worker_id = f"{leader_round.team_id}:teammate:1"
        session_id = resident_teammate_session_id(leader_round=leader_round, teammate_slot=1)
        delivery_id = f"{bundle.objective.objective_id}:lane:{leader_round.lane_id}"

        task = await runtime.submit_task(
            group_id=bundle.objective.group_id,
            lane_id=leader_round.lane_id,
            team_id=leader_round.team_id,
            goal="Commit directed teammate claim through the store transaction",
            scope=TaskScope.TEAM,
            created_by=leader_round.leader_task.leader_id,
        )
        await runtime.upsert_task_review(
            task_id=task.task_id,
            reviewer_agent_id=f"{leader_round.team_id}:teammate:2",
            reviewer_role="teammate",
            based_on_task_version=1,
            based_on_knowledge_epoch=1,
            stance=TaskReviewStance.GOOD_FIT,
            summary="Directed claim has relevant prior context.",
            relation_to_my_work="I already worked on the same lane.",
            confidence=0.8,
            experience_context=TaskReviewExperienceContext(
                touched_paths=("src/agent_orchestra/runtime/teammate_work_surface.py",),
            ),
            reviewed_at="2026-04-07T12:15:00+00:00",
        )
        await store.save_delivery_state(
            DeliveryState(
                delivery_id=delivery_id,
                objective_id=bundle.objective.objective_id,
                kind=DeliveryStateKind.LANE,
                status=DeliveryStatus.RUNNING,
                lane_id=leader_round.lane_id,
                team_id=leader_round.team_id,
                pending_task_ids=(task.task_id,),
                summary="Lane is running.",
            )
        )
        envelope = await mailbox.send(
            MailboxEnvelope(
                sender=leader_round.leader_task.leader_id,
                recipient=teammate_worker_id,
                subject="task.directed",
                group_id=bundle.objective.group_id,
                lane_id=leader_round.lane_id,
                team_id=leader_round.team_id,
                payload={"directive_id": "dir-store-1", "task_id": task.task_id},
            )
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            surface = TeammateWorkSurface(
                runtime=runtime,
                mailbox=mailbox,
                objective=bundle.objective,
                leader_round=leader_round,
                backend="in_process",
                working_dir=tmpdir,
                turn_index=1,
                session_host=supervisor.session_host,
            )
            result = await surface.acquire_assignments(existing_assignments=(), limit=1)

        self.assertEqual(len(result.assignments), 1)
        assignment = result.assignments[0]
        self.assertIn("task_review_digest", assignment.metadata)
        self.assertIn("task_review_slots", assignment.metadata)
        self.assertIn("Task review digest:", assignment.instructions)
        self.assertIn("Directed claim has relevant prior context.", assignment.instructions)
        self.assertEqual(len(store.directed_receipt_commits), 1)
        commit = store.directed_receipt_commits[0]
        session_snapshot = _coordination_session_snapshot(commit)
        self.assertIsNotNone(session_snapshot)
        assert session_snapshot is not None
        self.assertEqual(session_snapshot.session_id, session_id)
        self.assertEqual(session_snapshot.mailbox_cursor["last_envelope_id"], envelope.envelope_id)
        self.assertEqual(session_snapshot.current_directive_ids, (task.task_id,))
        self.assertEqual(
            session_snapshot.metadata["current_claim_session_id"],
            commit.task.claim_session_id,
        )
        self.assertEqual(
            session_snapshot.metadata["last_claim_source"],
            commit.task.claim_source,
        )
        self.assertLess(
            store.coordination_events.index("store.commit_directed_task_receipt"),
            store.coordination_events.index("mailbox.send:task.receipt"),
        )

        session_snapshot_after_commit = await supervisor.session_host.snapshot_session(session_id)
        self.assertIsNotNone(session_snapshot_after_commit)
        assert session_snapshot_after_commit is not None
        self.assertEqual(
            session_snapshot_after_commit["mailbox_cursor"]["last_envelope_id"],
            envelope.envelope_id,
        )
        self.assertEqual(session_snapshot_after_commit["current_directive_ids"], [task.task_id])
        stored_session = await store.get_agent_session(session_id)
        self.assertEqual(stored_session, session_snapshot)

    async def test_reserve_initial_assignments_routes_leader_activation_through_store_coordination_commit(self) -> None:
        store = _TrackingCoordinationStore()
        bus = InMemoryEventBus()
        supervisor = DefaultWorkerSupervisor(
            store=store,
            launch_backends={"in_process": InProcessLaunchBackend()},
            runner=_NoopRunner(),
        )
        runtime = GroupRuntime(
            store=store,
            bus=bus,
            launch_backends={"in_process": InProcessLaunchBackend()},
            supervisor=supervisor,
        )
        planner = TemplatePlanner()
        mailbox = _TrackingMailboxBridge(coordination_events=store.coordination_events)

        await runtime.create_group("group-a")
        planning_result = await planner.build_initial_plan(
            ObjectiveTemplate(
                objective_id="obj-runtime",
                group_id="group-a",
                title="Build runtime",
                description="Route leader activation through the runtime-owned coordination surface.",
                workstreams=(
                    WorkstreamTemplate(
                        workstream_id="runtime",
                        title="Runtime",
                        summary="Own the runtime lane.",
                        team_name="Runtime",
                        budget_max_teammates=1,
                        budget_max_iterations=1,
                    ),
                ),
            )
        )
        bundle = await materialize_planning_result(runtime, planning_result, created_by="superleader-1")
        leader_round = bundle.leader_rounds[0]
        teammate_worker_id = f"{leader_round.team_id}:teammate:1"
        session_id = resident_teammate_session_id(leader_round=leader_round, teammate_slot=1)
        delivery_id = f"{bundle.objective.objective_id}:lane:{leader_round.lane_id}"

        task = await runtime.submit_task(
            group_id=bundle.objective.group_id,
            lane_id=leader_round.lane_id,
            team_id=leader_round.team_id,
            goal="Activate leader-dispatched teammate work",
            scope=TaskScope.TEAM,
            created_by=leader_round.leader_task.leader_id,
        )
        await store.save_delivery_state(
            DeliveryState(
                delivery_id=delivery_id,
                objective_id=bundle.objective.objective_id,
                kind=DeliveryStateKind.LANE,
                status=DeliveryStatus.RUNNING,
                lane_id=leader_round.lane_id,
                team_id=leader_round.team_id,
                pending_task_ids=(task.task_id,),
                summary="Lane is running.",
            )
        )
        assignment = WorkerAssignment(
            assignment_id=f"{task.task_id}:leader-dispatch",
            worker_id=teammate_worker_id,
            group_id=bundle.objective.group_id,
            objective_id=bundle.objective.objective_id,
            team_id=leader_round.team_id,
            lane_id=leader_round.lane_id,
            task_id=task.task_id,
            role="teammate",
            backend="in_process",
            instructions="Execute leader-dispatched teammate work",
            input_text="run",
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            surface = TeammateWorkSurface(
                runtime=runtime,
                mailbox=mailbox,
                objective=bundle.objective,
                leader_round=leader_round,
                backend="in_process",
                working_dir=tmpdir,
                turn_index=1,
                session_host=supervisor.session_host,
            )
            reserved = await surface.reserve_initial_assignments((assignment,))

        self.assertEqual(len(reserved), 1)
        self.assertEqual(len(store.directed_receipt_commits), 1)
        commit = store.directed_receipt_commits[0]
        self.assertEqual(commit.task.claim_source, "leader_assignment_dispatch")
        self.assertEqual(commit.receipt.receipt_type, "activation_reserved")
        self.assertEqual(commit.blackboard_entry.payload["event"], "task.receipt")
        self.assertEqual(commit.blackboard_entry.payload["claim_source"], "leader_assignment_dispatch")
        self.assertEqual(commit.blackboard_entry.payload["receipt_type"], "activation_reserved")
        self.assertEqual(
            commit.blackboard_entry.payload["session_snapshot_id"],
            session_id,
        )
        session_snapshot = _coordination_session_snapshot(commit)
        self.assertIsNotNone(session_snapshot)
        assert session_snapshot is not None
        self.assertEqual(session_snapshot.session_id, session_id)
        self.assertEqual(session_snapshot.current_directive_ids, ())
        self.assertEqual(
            session_snapshot.metadata["current_claim_session_id"],
            commit.task.claim_session_id,
        )
        self.assertEqual(
            session_snapshot.metadata["last_claim_source"],
            "leader_assignment_dispatch",
        )
        self.assertNotIn("mailbox.send:task.receipt", store.coordination_events)

        saved_state = await store.get_delivery_state(delivery_id)
        self.assertIsNotNone(saved_state)
        assert saved_state is not None
        self.assertIn(task.task_id, saved_state.active_task_ids)
        self.assertNotIn(task.task_id, saved_state.pending_task_ids)
        coordination = saved_state.metadata["teammate_coordination"]
        self.assertEqual(coordination["receipt_count"], 1)
        self.assertEqual(coordination["last_receipt_task_id"], task.task_id)
        self.assertEqual(coordination["last_receipt_worker_id"], teammate_worker_id)
        self.assertEqual(
            coordination["last_receipt_claim_session_id"],
            commit.task.claim_session_id,
        )
        self.assertEqual(coordination["last_receipt_claim_source"], "leader_assignment_dispatch")
        self.assertEqual(coordination["last_receipt_receipt_type"], "activation_reserved")
        post_commit_events = store.coordination_events[
            store.coordination_events.index("store.commit_directed_task_receipt") + 1 :
        ]
        self.assertEqual(post_commit_events.count("store.save_agent_session"), 1)
        self.assertEqual(coordination["last_receipt_session_id"], session_id)

        session_snapshot_after_commit = await supervisor.session_host.snapshot_session(session_id)
        self.assertIsNotNone(session_snapshot_after_commit)
        assert session_snapshot_after_commit is not None
        self.assertEqual(session_snapshot_after_commit["current_directive_ids"], [])
        self.assertEqual(
            session_snapshot_after_commit["metadata"]["current_claim_session_id"],
            commit.task.claim_session_id,
        )

    async def test_teammate_work_surface_records_claim_and_result_turns(self) -> None:
        store = InMemoryOrchestrationStore()
        bus = InMemoryEventBus()
        supervisor = DefaultWorkerSupervisor(
            store=store,
            launch_backends={"in_process": InProcessLaunchBackend()},
            runner=_CompletedRunner(),
        )
        runtime = GroupRuntime(
            store=store,
            bus=bus,
            launch_backends={"in_process": InProcessLaunchBackend()},
            supervisor=supervisor,
        )
        planner = TemplatePlanner()
        mailbox = InMemoryMailboxBridge()

        await runtime.create_group("group-a")
        planning_result = await planner.build_initial_plan(
            ObjectiveTemplate(
                objective_id="obj-teammate-ledger",
                group_id="group-a",
                title="Teammate ledger",
                description="Capture teammate claim and result transitions.",
                workstreams=(
                    WorkstreamTemplate(
                        workstream_id="runtime",
                        title="Runtime",
                        summary="Own the runtime lane.",
                        team_name="Runtime",
                        budget_max_teammates=1,
                        budget_max_iterations=1,
                    ),
                ),
            )
        )
        bundle = await materialize_planning_result(runtime, planning_result, created_by="superleader-1")
        leader_round = bundle.leader_rounds[0]
        continuity = await runtime.new_session(
            group_id=bundle.objective.group_id,
            objective_id=bundle.objective.objective_id,
            title="Teammate turn ledger",
        )
        task = await runtime.submit_task(
            group_id=bundle.objective.group_id,
            team_id=leader_round.team_id,
            lane_id=leader_round.lane_id,
            goal="Capture teammate turn records",
            scope=TaskScope.TEAM,
            created_by=leader_round.leader_task.leader_id,
        )
        role_profile = build_runtime_role_profiles()["teammate_in_process_fast"]

        async def _allow(_request) -> PermissionDecision:
            return PermissionDecision(
                approved=True,
                reviewer="system.auto",
                reason="Automatically approved.",
            )

        with tempfile.TemporaryDirectory() as tmpdir:
            assignment = build_pending_teammate_assignment(
                objective=bundle.objective,
                leader_round=leader_round,
                task=task,
                claim_context=None,
                teammate_slot=1,
                turn_index=1,
                backend="in_process",
                working_dir=tmpdir,
                role_profile=role_profile,
            )
            surface = TeammateWorkSurface(
                runtime=runtime,
                mailbox=mailbox,
                objective=bundle.objective,
                leader_round=leader_round,
                backend="in_process",
                working_dir=tmpdir,
                turn_index=1,
                role_profile=role_profile,
                session_host=supervisor.session_host,
            )
            await surface.ensure_or_step_sessions(
                assignments=(assignment,),
                request_permission=_allow,
            )

        records = await store.list_turn_records(
            continuity.work_session.work_session_id,
            runtime_generation_id=continuity.runtime_generation.runtime_generation_id,
            head_kind=ConversationHeadKind.TEAMMATE_SLOT,
            scope_id=assignment.worker_id,
        )
        self.assertTrue(any(record.turn_kind == AgentTurnKind.WORKER_RESULT for record in records))
        self.assertTrue(
            any(
                record.metadata.get("claim_source") == "leader_assignment_dispatch"
                for record in records
            )
        )

    async def test_prepare_and_finalize_assignment_persist_teammate_slot_session_truth(self) -> None:
        store = InMemoryOrchestrationStore()
        bus = InMemoryEventBus()
        supervisor = DefaultWorkerSupervisor(
            store=store,
            launch_backends={"in_process": InProcessLaunchBackend()},
            runner=_NoopRunner(),
        )
        runtime = GroupRuntime(
            store=store,
            bus=bus,
            launch_backends={"in_process": InProcessLaunchBackend()},
            supervisor=supervisor,
        )
        planner = TemplatePlanner()
        mailbox = InMemoryMailboxBridge()

        await runtime.create_group("group-a")
        planning_result = await planner.build_initial_plan(
            ObjectiveTemplate(
                objective_id="obj-runtime",
                group_id="group-a",
                title="Build runtime",
                description="Persist teammate slot state.",
                workstreams=(
                    WorkstreamTemplate(
                        workstream_id="runtime",
                        title="Runtime",
                        summary="Own the runtime lane.",
                        team_name="Runtime",
                        budget_max_teammates=1,
                        budget_max_iterations=1,
                    ),
                ),
            )
        )
        bundle = await materialize_planning_result(runtime, planning_result, created_by="superleader-1")
        leader_round = bundle.leader_rounds[0]
        teammate_slot = 1
        teammate_worker_id = f"{leader_round.team_id}:teammate:{teammate_slot}"
        session_id = resident_teammate_session_id(leader_round=leader_round, teammate_slot=teammate_slot)

        task = await runtime.submit_task(
            group_id=bundle.objective.group_id,
            lane_id=leader_round.lane_id,
            team_id=leader_round.team_id,
            goal="Persist teammate state",
            scope=TaskScope.TEAM,
            created_by=leader_round.leader_task.leader_id,
        )
        assignment = WorkerAssignment(
            assignment_id=f"{task.task_id}:assignment",
            worker_id=teammate_worker_id,
            group_id=bundle.objective.group_id,
            objective_id=bundle.objective.objective_id,
            team_id=leader_round.team_id,
            lane_id=leader_round.lane_id,
            task_id=task.task_id,
            role="teammate",
            backend="in_process",
            instructions="Run teammate work",
            input_text="run",
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            surface = TeammateWorkSurface(
                runtime=runtime,
                mailbox=mailbox,
                objective=bundle.objective,
                leader_round=leader_round,
                backend="in_process",
                working_dir=tmpdir,
                turn_index=1,
                session_host=supervisor.session_host,
            )
            context = await surface.prepare_assignment(assignment)

            snapshot = await supervisor.session_host.snapshot_session(session_id)
            self.assertIsNotNone(snapshot)
            assert snapshot is not None
            self.assertEqual(snapshot["metadata"]["current_task_id"], task.task_id)
            self.assertEqual(snapshot["metadata"]["current_claim_session_id"], context.claim_session_id)
            self.assertEqual(snapshot["metadata"]["last_claim_source"], context.claim_source)

            await surface.finalize_assignment(
                assignment,
                context=context,
                record=WorkerRecord(
                    worker_id=teammate_worker_id,
                    assignment_id=assignment.assignment_id,
                    backend="in_process",
                    role="teammate",
                    status=WorkerStatus.COMPLETED,
                    output_text="done",
                    session=WorkerSession(
                        session_id="worker-session-1",
                        worker_id=teammate_worker_id,
                        role="teammate",
                        backend="in_process",
                        status="idle",
                    ),
                ),
            )

        final_snapshot = await supervisor.session_host.snapshot_session(session_id)
        self.assertIsNotNone(final_snapshot)
        assert final_snapshot is not None
        self.assertIsNone(final_snapshot["metadata"]["current_task_id"])
        self.assertEqual(final_snapshot["metadata"]["current_claim_session_id"], context.claim_session_id)
        self.assertEqual(final_snapshot["metadata"]["last_worker_session_id"], "worker-session-1")

    async def test_run_delegates_to_ensure_or_step_sessions(self) -> None:
        store = InMemoryOrchestrationStore()
        bus = InMemoryEventBus()
        supervisor = DefaultWorkerSupervisor(
            store=store,
            launch_backends={"in_process": InProcessLaunchBackend()},
            runner=_CompletedRunner(),
        )
        runtime = GroupRuntime(
            store=store,
            bus=bus,
            launch_backends={"in_process": InProcessLaunchBackend()},
            supervisor=supervisor,
        )
        planner = TemplatePlanner()
        mailbox = InMemoryMailboxBridge()

        await runtime.create_group("group-a")
        planning_result = await planner.build_initial_plan(
            ObjectiveTemplate(
                objective_id="obj-runtime",
                group_id="group-a",
                title="Build runtime",
                description="Delegate teammate activation to ensure-or-step sessions.",
                workstreams=(
                    WorkstreamTemplate(
                        workstream_id="runtime",
                        title="Runtime",
                        summary="Own the runtime lane.",
                        team_name="Runtime",
                        budget_max_teammates=1,
                        budget_max_iterations=1,
                    ),
                ),
            )
        )
        bundle = await materialize_planning_result(runtime, planning_result, created_by="superleader-1")
        expected_result = ResidentTeammateRunResult(claimed_task_ids=("task-1",))
        observed: dict[str, object] = {}

        async def _allow(_request) -> PermissionDecision:
            return PermissionDecision(
                approved=True,
                reviewer="system.auto",
                reason="Automatically approved.",
            )

        with tempfile.TemporaryDirectory() as tmpdir:
            surface = TeammateWorkSurface(
                runtime=runtime,
                mailbox=mailbox,
                objective=bundle.objective,
                leader_round=bundle.leader_rounds[0],
                backend="in_process",
                working_dir=tmpdir,
                turn_index=1,
                session_host=supervisor.session_host,
            )

            async def _ensure_or_step_sessions(
                *,
                assignments,
                request_permission,
                resident_kernel=None,
                keep_session_idle=False,
                execution_policy=None,
            ):
                observed["call"] = {
                    "assignments": assignments,
                    "request_permission": request_permission,
                    "resident_kernel": resident_kernel,
                    "keep_session_idle": keep_session_idle,
                    "execution_policy": execution_policy,
                }
                return expected_result

            surface.ensure_or_step_sessions = _ensure_or_step_sessions  # type: ignore[attr-defined]
            result = await surface.run(
                assignments=(),
                request_permission=_allow,
            )

        self.assertIs(result, expected_result)
        self.assertIn("call", observed)
        self.assertEqual(observed["call"]["assignments"], ())
        self.assertEqual(observed["call"]["request_permission"], _allow)
        self.assertIsNone(observed["call"]["resident_kernel"])
        self.assertFalse(observed["call"]["keep_session_idle"])
        self.assertIsNone(observed["call"]["execution_policy"])

    async def test_run_executes_initial_assignments_and_owns_leader_dispatch_claim_truth(self) -> None:
        store = InMemoryOrchestrationStore()
        bus = InMemoryEventBus()
        supervisor = DefaultWorkerSupervisor(
            store=store,
            launch_backends={"in_process": InProcessLaunchBackend()},
            runner=_CompletedRunner(),
        )
        runtime = GroupRuntime(
            store=store,
            bus=bus,
            launch_backends={"in_process": InProcessLaunchBackend()},
            supervisor=supervisor,
        )
        planner = TemplatePlanner()
        mailbox = InMemoryMailboxBridge()

        await runtime.create_group("group-a")
        planning_result = await planner.build_initial_plan(
            ObjectiveTemplate(
                objective_id="obj-runtime",
                group_id="group-a",
                title="Build runtime",
                description="Run initial teammate assignment through the work surface.",
                workstreams=(
                    WorkstreamTemplate(
                        workstream_id="runtime",
                        title="Runtime",
                        summary="Own the runtime lane.",
                        team_name="Runtime",
                        budget_max_teammates=1,
                        budget_max_iterations=1,
                    ),
                ),
            )
        )
        bundle = await materialize_planning_result(runtime, planning_result, created_by="superleader-1")
        leader_round = bundle.leader_rounds[0]
        teammate_slot = 1
        teammate_worker_id = f"{leader_round.team_id}:teammate:{teammate_slot}"
        session_id = resident_teammate_session_id(leader_round=leader_round, teammate_slot=teammate_slot)

        task = await runtime.submit_task(
            group_id=bundle.objective.group_id,
            lane_id=leader_round.lane_id,
            team_id=leader_round.team_id,
            goal="Execute leader-dispatched teammate work",
            scope=TaskScope.TEAM,
            created_by=leader_round.leader_task.leader_id,
        )
        assignment = WorkerAssignment(
            assignment_id=f"{task.task_id}:leader-dispatch",
            worker_id=teammate_worker_id,
            group_id=bundle.objective.group_id,
            objective_id=bundle.objective.objective_id,
            team_id=leader_round.team_id,
            lane_id=leader_round.lane_id,
            task_id=task.task_id,
            role="teammate",
            backend="in_process",
            instructions="Execute leader-dispatched teammate work",
            input_text="run",
        )

        async def _allow(_request) -> PermissionDecision:
            return PermissionDecision(
                approved=True,
                reviewer="system.auto",
                reason="Automatically approved.",
            )

        with tempfile.TemporaryDirectory() as tmpdir:
            surface = TeammateWorkSurface(
                runtime=runtime,
                mailbox=mailbox,
                objective=bundle.objective,
                leader_round=leader_round,
                backend="in_process",
                working_dir=tmpdir,
                turn_index=1,
                session_host=supervisor.session_host,
            )
            result = await surface.run(
                assignments=(assignment,),
                request_permission=_allow,
            )

        self.assertEqual(len(result.teammate_records), 1)
        self.assertEqual(result.teammate_records[0].status, WorkerStatus.COMPLETED)
        self.assertEqual(result.dispatched_assignment_count, 1)
        self.assertTrue(result.teammate_execution_evidence)
        self.assertEqual(len(result.mailbox_envelopes), 1)
        saved_task = await store.get_task(task.task_id)
        self.assertIsNotNone(saved_task)
        assert saved_task is not None
        self.assertEqual(saved_task.status, TaskStatus.COMPLETED)
        self.assertEqual(saved_task.claim_source, "leader_assignment_dispatch")
        self.assertTrue(saved_task.claim_session_id)
        session_snapshot = await supervisor.session_host.snapshot_session(session_id)
        self.assertIsNotNone(session_snapshot)
        assert session_snapshot is not None
        self.assertIsNone(session_snapshot["metadata"]["current_task_id"])
        self.assertEqual(session_snapshot["current_directive_ids"], [])

    async def test_run_refills_from_task_surface_and_reports_resident_claim_evidence(self) -> None:
        store = InMemoryOrchestrationStore()
        bus = InMemoryEventBus()
        supervisor = DefaultWorkerSupervisor(
            store=store,
            launch_backends={"in_process": InProcessLaunchBackend()},
            runner=_CompletedRunner(),
        )
        runtime = GroupRuntime(
            store=store,
            bus=bus,
            launch_backends={"in_process": InProcessLaunchBackend()},
            supervisor=supervisor,
        )
        planner = TemplatePlanner()
        mailbox = InMemoryMailboxBridge()

        await runtime.create_group("group-a")
        planning_result = await planner.build_initial_plan(
            ObjectiveTemplate(
                objective_id="obj-runtime",
                group_id="group-a",
                title="Build runtime",
                description="Refill teammate work from the shared task surface.",
                workstreams=(
                    WorkstreamTemplate(
                        workstream_id="runtime",
                        title="Runtime",
                        summary="Own the runtime lane.",
                        team_name="Runtime",
                        budget_max_teammates=1,
                        budget_max_iterations=1,
                    ),
                ),
            )
        )
        bundle = await materialize_planning_result(runtime, planning_result, created_by="superleader-1")
        leader_round = bundle.leader_rounds[0]
        teammate_worker_id = f"{leader_round.team_id}:teammate:1"

        initial_task = await runtime.submit_task(
            group_id=bundle.objective.group_id,
            lane_id=leader_round.lane_id,
            team_id=leader_round.team_id,
            goal="Execute initial teammate work",
            scope=TaskScope.TEAM,
            created_by=leader_round.leader_task.leader_id,
        )
        overflow_task = await runtime.submit_task(
            group_id=bundle.objective.group_id,
            lane_id=leader_round.lane_id,
            team_id=leader_round.team_id,
            goal="Execute overflow teammate work",
            scope=TaskScope.TEAM,
            created_by=leader_round.leader_task.leader_id,
        )
        initial_assignment = WorkerAssignment(
            assignment_id=f"{initial_task.task_id}:leader-dispatch",
            worker_id=teammate_worker_id,
            group_id=bundle.objective.group_id,
            objective_id=bundle.objective.objective_id,
            team_id=leader_round.team_id,
            lane_id=leader_round.lane_id,
            task_id=initial_task.task_id,
            role="teammate",
            backend="in_process",
            instructions="Execute initial teammate work",
            input_text="run",
        )

        async def _allow(_request) -> PermissionDecision:
            return PermissionDecision(
                approved=True,
                reviewer="system.auto",
                reason="Automatically approved.",
            )

        with tempfile.TemporaryDirectory() as tmpdir:
            surface = TeammateWorkSurface(
                runtime=runtime,
                mailbox=mailbox,
                objective=bundle.objective,
                leader_round=leader_round,
                backend="in_process",
                working_dir=tmpdir,
                turn_index=1,
                session_host=supervisor.session_host,
            )
            result = await surface.run(
                assignments=(initial_assignment,),
                request_permission=_allow,
            )

        self.assertEqual(len(result.teammate_records), 2)
        self.assertIn(overflow_task.task_id, result.autonomous_claimed_task_ids)
        self.assertTrue(result.claim_session_ids)
        self.assertIn("resident_task_list_claim", result.claim_sources)
        self.assertTrue(result.teammate_execution_evidence)
        saved_overflow_task = await store.get_task(overflow_task.task_id)
        self.assertIsNotNone(saved_overflow_task)
        assert saved_overflow_task is not None
        self.assertEqual(saved_overflow_task.status, TaskStatus.COMPLETED)

    async def test_run_requires_seeded_assignment_or_host_owned_slot_for_autonomous_claims(self) -> None:
        store = InMemoryOrchestrationStore()
        bus = InMemoryEventBus()
        supervisor = DefaultWorkerSupervisor(
            store=store,
            launch_backends={"in_process": InProcessLaunchBackend()},
            runner=_CompletedRunner(),
        )
        runtime = GroupRuntime(
            store=store,
            bus=bus,
            launch_backends={"in_process": InProcessLaunchBackend()},
            supervisor=supervisor,
        )
        planner = TemplatePlanner()
        mailbox = InMemoryMailboxBridge()

        await runtime.create_group("group-a")
        planning_result = await planner.build_initial_plan(
            ObjectiveTemplate(
                objective_id="obj-runtime",
                group_id="group-a",
                title="Build runtime",
                description="Let the slot online loop own autonomous claim acquisition.",
                workstreams=(
                    WorkstreamTemplate(
                        workstream_id="runtime",
                        title="Runtime",
                        summary="Own the runtime lane.",
                        team_name="Runtime",
                        budget_max_teammates=1,
                        budget_max_iterations=1,
                    ),
                ),
            )
        )
        bundle = await materialize_planning_result(runtime, planning_result, created_by="superleader-1")
        leader_round = bundle.leader_rounds[0]

        task = await runtime.submit_task(
            group_id=bundle.objective.group_id,
            lane_id=leader_round.lane_id,
            team_id=leader_round.team_id,
            goal="Let the slot loop claim this work directly",
            scope=TaskScope.TEAM,
            created_by=leader_round.leader_task.leader_id,
        )

        async def _allow(_request) -> PermissionDecision:
            return PermissionDecision(
                approved=True,
                reviewer="system.auto",
                reason="Automatically approved.",
            )

        with tempfile.TemporaryDirectory() as tmpdir:
            surface = TeammateWorkSurface(
                runtime=runtime,
                mailbox=mailbox,
                objective=bundle.objective,
                leader_round=leader_round,
                backend="in_process",
                working_dir=tmpdir,
                turn_index=1,
                session_host=supervisor.session_host,
            )

            async def _forbid_preprime(*_args, **_kwargs):
                raise AssertionError("run() should not pre-prime teammate acquisition outside the slot loop")

            surface.acquire_assignments = _forbid_preprime  # type: ignore[assignment]
            result = await surface.run(
                assignments=(),
                request_permission=_allow,
            )

        self.assertEqual(result.teammate_records, ())
        self.assertEqual(result.claimed_task_ids, ())
        self.assertEqual(result.autonomous_claimed_task_ids, ())
        self.assertFalse(result.teammate_execution_evidence)
        self.assertIsNotNone(result.coordinator_session)
        assert result.coordinator_session is not None
        self.assertEqual(result.coordinator_session.metadata["slot_count"], 0)
        saved_task = await store.get_task(task.task_id)
        self.assertIsNotNone(saved_task)
        assert saved_task is not None
        self.assertEqual(saved_task.status, TaskStatus.PENDING)
        self.assertIsNone(
            await supervisor.session_host.load_session(
                resident_teammate_session_id(leader_round=leader_round, teammate_slot=1)
            )
        )

    async def test_ensure_or_step_sessions_steps_only_host_profiled_slots_for_autonomous_claims_without_surface_context(
        self,
    ) -> None:
        store = InMemoryOrchestrationStore()
        bus = InMemoryEventBus()
        supervisor = DefaultWorkerSupervisor(
            store=store,
            launch_backends={"in_process": InProcessLaunchBackend()},
            runner=_CompletedRunner(),
        )
        runtime = GroupRuntime(
            store=store,
            bus=bus,
            launch_backends={"in_process": InProcessLaunchBackend()},
            supervisor=supervisor,
        )
        planner = TemplatePlanner()
        mailbox = InMemoryMailboxBridge()

        await runtime.create_group("group-a")
        planning_result = await planner.build_initial_plan(
            ObjectiveTemplate(
                objective_id="obj-runtime",
                group_id="group-a",
                title="Build runtime",
                description="Only host-profiled teammate slots should be stepped when surface context is absent.",
                workstreams=(
                    WorkstreamTemplate(
                        workstream_id="runtime",
                        title="Runtime",
                        summary="Own the runtime lane.",
                        team_name="Runtime",
                        budget_max_teammates=2,
                        budget_max_iterations=1,
                    ),
                ),
            )
        )
        bundle = await materialize_planning_result(runtime, planning_result, created_by="superleader-1")
        leader_round = bundle.leader_rounds[0]
        teammate_profile = build_runtime_role_profiles()["teammate_in_process_fast"]
        teammate_slot_two_worker_id = f"{leader_round.team_id}:teammate:2"
        slot_one_session_id = resident_teammate_session_id(leader_round=leader_round, teammate_slot=1)
        slot_two_session_id = resident_teammate_session_id(leader_round=leader_round, teammate_slot=2)

        task = await runtime.submit_task(
            group_id=bundle.objective.group_id,
            lane_id=leader_round.lane_id,
            team_id=leader_round.team_id,
            goal="Let the already-hosted slot keep autonomously claiming work",
            scope=TaskScope.TEAM,
            created_by=leader_round.leader_task.leader_id,
        )

        async def _allow(_request) -> PermissionDecision:
            return PermissionDecision(
                approved=True,
                reviewer="system.auto",
                reason="Automatically approved.",
            )

        with tempfile.TemporaryDirectory() as tmpdir:
            priming_surface = TeammateWorkSurface(
                runtime=runtime,
                mailbox=mailbox,
                objective=bundle.objective,
                leader_round=leader_round,
                backend="in_process",
                working_dir=tmpdir,
                turn_index=1,
                role_profile=teammate_profile,
                session_host=supervisor.session_host,
            )
            await priming_surface.ensure_slot_session(worker_id=teammate_slot_two_worker_id)

            continuation_surface = TeammateWorkSurface(
                runtime=runtime,
                mailbox=mailbox,
                objective=bundle.objective,
                leader_round=leader_round,
                backend=None,
                working_dir=None,
                turn_index=2,
                role_profile=None,
                session_host=supervisor.session_host,
            )
            result = await continuation_surface.ensure_or_step_sessions(
                assignments=(),
                request_permission=_allow,
            )

        self.assertEqual(result.autonomous_claimed_task_ids, (task.task_id,))
        self.assertIsNotNone(result.coordinator_session)
        assert result.coordinator_session is not None
        self.assertEqual(result.coordinator_session.metadata["slot_count"], 1)
        saved_task = await store.get_task(task.task_id)
        self.assertIsNotNone(saved_task)
        assert saved_task is not None
        self.assertEqual(saved_task.status, TaskStatus.COMPLETED)
        self.assertIsNone(await supervisor.session_host.load_session(slot_one_session_id))
        slot_two_snapshot = await supervisor.session_host.snapshot_session(slot_two_session_id)
        self.assertIsNotNone(slot_two_snapshot)
        assert slot_two_snapshot is not None
        self.assertEqual(
            slot_two_snapshot["metadata"]["activation_profile"]["role_profile"]["profile_id"],
            teammate_profile.profile_id,
        )

    async def test_ensure_or_step_sessions_preserves_host_activation_truth_over_surface_context_on_continuation(
        self,
    ) -> None:
        store = InMemoryOrchestrationStore()
        bus = InMemoryEventBus()
        supervisor = DefaultWorkerSupervisor(
            store=store,
            launch_backends={"in_process": InProcessLaunchBackend()},
            runner=_CompletedRunner(),
        )
        runtime = GroupRuntime(
            store=store,
            bus=bus,
            launch_backends={"in_process": InProcessLaunchBackend()},
            supervisor=supervisor,
        )
        planner = TemplatePlanner()
        mailbox = InMemoryMailboxBridge()

        await runtime.create_group("group-a")
        planning_result = await planner.build_initial_plan(
            ObjectiveTemplate(
                objective_id="obj-runtime",
                group_id="group-a",
                title="Preserve host slot truth",
                description=(
                    "Continuation should resume from the host-owned slot profile instead of rewriting "
                    "activation or wake metadata from the current surface context."
                ),
                workstreams=(
                    WorkstreamTemplate(
                        workstream_id="runtime",
                        title="Runtime",
                        summary="Own the runtime lane.",
                        team_name="Runtime",
                        budget_max_teammates=1,
                        budget_max_iterations=1,
                    ),
                ),
            )
        )
        bundle = await materialize_planning_result(runtime, planning_result, created_by="superleader-1")
        leader_round = bundle.leader_rounds[0]
        teammate_profile = build_runtime_role_profiles()["teammate_in_process_fast"]
        teammate_worker_id = f"{leader_round.team_id}:teammate:1"
        session_id = resident_teammate_session_id(leader_round=leader_round, teammate_slot=1)

        task = await runtime.submit_task(
            group_id=bundle.objective.group_id,
            lane_id=leader_round.lane_id,
            team_id=leader_round.team_id,
            goal="Resume the host-owned slot without rewriting its activation profile",
            scope=TaskScope.TEAM,
            created_by=leader_round.leader_task.leader_id,
        )

        observed_working_dirs: list[str] = []
        original_run_worker_assignment = runtime.run_worker_assignment

        async def _capture_working_dir(
            assignment: WorkerAssignment,
            *,
            policy=None,
        ) -> WorkerRecord:
            observed_working_dirs.append(assignment.working_dir)
            return await original_run_worker_assignment(assignment, policy=policy)

        runtime.run_worker_assignment = _capture_working_dir  # type: ignore[assignment]

        async def _allow(_request) -> PermissionDecision:
            return PermissionDecision(
                approved=True,
                reviewer="system.auto",
                reason="Automatically approved.",
            )

        with tempfile.TemporaryDirectory() as host_dir, tempfile.TemporaryDirectory() as override_dir:
            priming_surface = TeammateWorkSurface(
                runtime=runtime,
                mailbox=mailbox,
                objective=bundle.objective,
                leader_round=leader_round,
                backend="in_process",
                working_dir=host_dir,
                turn_index=1,
                role_profile=teammate_profile,
                session_host=supervisor.session_host,
            )
            await priming_surface.ensure_slot_session(worker_id=teammate_worker_id)

            continuation_surface = TeammateWorkSurface(
                runtime=runtime,
                mailbox=mailbox,
                objective=bundle.objective,
                leader_round=leader_round,
                backend="in_process",
                working_dir=override_dir,
                turn_index=2,
                role_profile=None,
                session_host=supervisor.session_host,
            )
            result = await continuation_surface.ensure_or_step_sessions(
                assignments=(),
                request_permission=_allow,
            )

        saved_task = await store.get_task(task.task_id)
        self.assertEqual(len(result.teammate_records), 1)
        self.assertEqual(result.autonomous_claimed_task_ids, (task.task_id,))
        self.assertEqual(observed_working_dirs, [host_dir])
        self.assertIsNotNone(saved_task)
        assert saved_task is not None
        self.assertEqual(saved_task.status, TaskStatus.COMPLETED)
        slot_snapshot = await supervisor.session_host.snapshot_session(session_id)
        self.assertIsNotNone(slot_snapshot)
        assert slot_snapshot is not None
        metadata = slot_snapshot["metadata"]
        self.assertEqual(metadata["activation_profile"]["working_dir"], host_dir)
        self.assertEqual(
            metadata["activation_profile"]["role_profile"]["profile_id"],
            teammate_profile.profile_id,
        )
        self.assertNotIn("last_activation_intent_at", metadata)
        self.assertNotIn("last_activation_requested_by", metadata)
        self.assertNotIn("wake_request_count", metadata)
        self.assertNotIn("last_wake_request_at", metadata)
        self.assertNotIn("last_wake_requested_by", metadata)

    async def test_ensure_or_step_sessions_polls_directed_mailbox_for_existing_host_owned_slots_during_leader_seeded_activation(
        self,
    ) -> None:
        store = InMemoryOrchestrationStore()
        bus = InMemoryEventBus()
        supervisor = DefaultWorkerSupervisor(
            store=store,
            launch_backends={"in_process": InProcessLaunchBackend()},
            runner=_CompletedRunner(),
        )
        runtime = GroupRuntime(
            store=store,
            bus=bus,
            launch_backends={"in_process": InProcessLaunchBackend()},
            supervisor=supervisor,
        )
        planner = TemplatePlanner()
        mailbox = InMemoryMailboxBridge()

        await runtime.create_group("group-a")
        planning_result = await planner.build_initial_plan(
            ObjectiveTemplate(
                objective_id="obj-runtime",
                group_id="group-a",
                title="Build runtime",
                description="Existing host-owned slots must keep polling directed mailbox while leader seeds another slot.",
                workstreams=(
                    WorkstreamTemplate(
                        workstream_id="runtime",
                        title="Runtime",
                        summary="Own the runtime lane.",
                        team_name="Runtime",
                        budget_max_teammates=2,
                        budget_max_iterations=1,
                    ),
                ),
            )
        )
        bundle = await materialize_planning_result(runtime, planning_result, created_by="superleader-1")
        leader_round = bundle.leader_rounds[0]
        teammate_profile = build_runtime_role_profiles()["teammate_in_process_fast"]
        teammate_slot_one_worker_id = f"{leader_round.team_id}:teammate:1"
        teammate_slot_two_worker_id = f"{leader_round.team_id}:teammate:2"
        slot_two_session_id = resident_teammate_session_id(leader_round=leader_round, teammate_slot=2)

        initial_task = await runtime.submit_task(
            group_id=bundle.objective.group_id,
            lane_id=leader_round.lane_id,
            team_id=leader_round.team_id,
            goal="Run the leader-seeded teammate task",
            scope=TaskScope.TEAM,
            created_by=leader_round.leader_task.leader_id,
        )
        directed_task = await runtime.submit_task(
            group_id=bundle.objective.group_id,
            lane_id=leader_round.lane_id,
            team_id=leader_round.team_id,
            goal="Consume directed mailbox work on the already-hosted slot",
            scope=TaskScope.TEAM,
            created_by=leader_round.leader_task.leader_id,
        )
        original_run_worker_assignment = runtime.run_worker_assignment

        async def _run_worker_assignment_with_slot_one_delay(
            assignment: WorkerAssignment,
            *,
            policy=None,
        ) -> WorkerRecord:
            if assignment.task_id == initial_task.task_id:
                await asyncio.sleep(0.05)
            return await original_run_worker_assignment(assignment, policy=policy)

        runtime.run_worker_assignment = _run_worker_assignment_with_slot_one_delay  # type: ignore[assignment]

        async def _allow(_request) -> PermissionDecision:
            return PermissionDecision(
                approved=True,
                reviewer="system.auto",
                reason="Automatically approved.",
            )

        with tempfile.TemporaryDirectory() as tmpdir:
            priming_surface = TeammateWorkSurface(
                runtime=runtime,
                mailbox=mailbox,
                objective=bundle.objective,
                leader_round=leader_round,
                backend="in_process",
                working_dir=tmpdir,
                turn_index=1,
                role_profile=teammate_profile,
                session_host=supervisor.session_host,
            )
            await priming_surface.ensure_slot_session(worker_id=teammate_slot_two_worker_id)

            directed_envelope = await mailbox.send(
                MailboxEnvelope(
                    sender=leader_round.leader_task.leader_id,
                    recipient=teammate_slot_two_worker_id,
                    subject="task.directed",
                    group_id=bundle.objective.group_id,
                    lane_id=leader_round.lane_id,
                    team_id=leader_round.team_id,
                    payload={"directive_id": "dir-host-slot-2", "task_id": directed_task.task_id},
                )
            )
            initial_assignment = WorkerAssignment(
                assignment_id=f"{initial_task.task_id}:leader-dispatch",
                worker_id=teammate_slot_one_worker_id,
                group_id=bundle.objective.group_id,
                objective_id=bundle.objective.objective_id,
                team_id=leader_round.team_id,
                lane_id=leader_round.lane_id,
                task_id=initial_task.task_id,
                role="teammate",
                backend="in_process",
                instructions="Execute leader-seeded teammate work",
                input_text="run",
                working_dir=tmpdir,
                role_profile=teammate_profile,
            )

            continuation_surface = TeammateWorkSurface(
                runtime=runtime,
                mailbox=mailbox,
                objective=bundle.objective,
                leader_round=leader_round,
                backend=None,
                working_dir=None,
                turn_index=2,
                role_profile=None,
                session_host=supervisor.session_host,
            )
            result = await continuation_surface.ensure_or_step_sessions(
                assignments=(initial_assignment,),
                request_permission=_allow,
            )

        self.assertEqual(len(result.teammate_records), 2)
        self.assertEqual(result.directed_claimed_task_ids, (directed_task.task_id,))
        self.assertIn(directed_envelope.envelope_id, result.processed_mailbox_envelope_ids)
        self.assertIsNotNone(result.coordinator_session)
        assert result.coordinator_session is not None
        self.assertEqual(result.coordinator_session.metadata["slot_count"], 2)
        saved_initial_task = await store.get_task(initial_task.task_id)
        self.assertIsNotNone(saved_initial_task)
        assert saved_initial_task is not None
        self.assertEqual(saved_initial_task.status, TaskStatus.COMPLETED)
        saved_directed_task = await store.get_task(directed_task.task_id)
        self.assertIsNotNone(saved_directed_task)
        assert saved_directed_task is not None
        self.assertEqual(saved_directed_task.status, TaskStatus.COMPLETED)
        slot_two_snapshot = await supervisor.session_host.snapshot_session(slot_two_session_id)
        self.assertIsNotNone(slot_two_snapshot)
        assert slot_two_snapshot is not None
        self.assertEqual(slot_two_snapshot["current_directive_ids"], [])

    async def test_ensure_or_step_sessions_is_noop_without_host_owned_slots_or_surface_activation_context(
        self,
    ) -> None:
        store = InMemoryOrchestrationStore()
        bus = InMemoryEventBus()
        supervisor = DefaultWorkerSupervisor(
            store=store,
            launch_backends={"in_process": InProcessLaunchBackend()},
            runner=_CompletedRunner(),
        )
        runtime = GroupRuntime(
            store=store,
            bus=bus,
            launch_backends={"in_process": InProcessLaunchBackend()},
            supervisor=supervisor,
        )
        planner = TemplatePlanner()
        mailbox = InMemoryMailboxBridge()

        await runtime.create_group("group-a")
        planning_result = await planner.build_initial_plan(
            ObjectiveTemplate(
                objective_id="obj-runtime",
                group_id="group-a",
                title="Build runtime",
                description=(
                    "Without persisted slot activation state, continuation stepping should stay idle "
                    "instead of creating placeholder teammate sessions."
                ),
                workstreams=(
                    WorkstreamTemplate(
                        workstream_id="runtime",
                        title="Runtime",
                        summary="Own the runtime lane.",
                        team_name="Runtime",
                        budget_max_teammates=2,
                        budget_max_iterations=1,
                    ),
                ),
            )
        )
        bundle = await materialize_planning_result(runtime, planning_result, created_by="superleader-1")
        leader_round = bundle.leader_rounds[0]
        untouched_task = await runtime.submit_task(
            group_id=bundle.objective.group_id,
            lane_id=leader_round.lane_id,
            team_id=leader_round.team_id,
            goal="Do not bootstrap this task without a persisted host-owned slot profile",
            scope=TaskScope.TEAM,
            created_by=leader_round.leader_task.leader_id,
        )

        async def _allow(_request) -> PermissionDecision:
            return PermissionDecision(
                approved=True,
                reviewer="system.auto",
                reason="Automatically approved.",
            )

        continuation_surface = TeammateWorkSurface(
            runtime=runtime,
            mailbox=mailbox,
            objective=bundle.objective,
            leader_round=leader_round,
            backend=None,
            working_dir=None,
            turn_index=2,
            role_profile=None,
            session_host=supervisor.session_host,
        )
        result = await continuation_surface.ensure_or_step_sessions(
            assignments=(),
            request_permission=_allow,
        )

        saved_task = await store.get_task(untouched_task.task_id)
        self.assertEqual(result.teammate_records, ())
        self.assertEqual(result.claimed_task_ids, ())
        self.assertEqual(result.autonomous_claimed_task_ids, ())
        self.assertFalse(result.teammate_execution_evidence)
        self.assertIsNotNone(result.coordinator_session)
        assert result.coordinator_session is not None
        self.assertEqual(result.coordinator_session.metadata["slot_count"], 0)
        self.assertIsNotNone(saved_task)
        assert saved_task is not None
        self.assertEqual(saved_task.status, TaskStatus.PENDING)
        self.assertIsNone(
            await supervisor.session_host.load_session(
                resident_teammate_session_id(leader_round=leader_round, teammate_slot=1)
            )
        )
        self.assertIsNone(
            await supervisor.session_host.load_session(
                resident_teammate_session_id(leader_round=leader_round, teammate_slot=2)
            )
        )

    async def test_run_does_not_poll_directed_mailbox_without_host_owned_slot_or_seeded_assignment(
        self,
    ) -> None:
        store = InMemoryOrchestrationStore()
        bus = InMemoryEventBus()
        supervisor = DefaultWorkerSupervisor(
            store=store,
            launch_backends={"in_process": InProcessLaunchBackend()},
            runner=_CompletedRunner(),
        )
        runtime = GroupRuntime(
            store=store,
            bus=bus,
            launch_backends={"in_process": InProcessLaunchBackend()},
            supervisor=supervisor,
        )
        planner = TemplatePlanner()
        mailbox = InMemoryMailboxBridge()

        await runtime.create_group("group-a")
        planning_result = await planner.build_initial_plan(
            ObjectiveTemplate(
                objective_id="obj-runtime",
                group_id="group-a",
                title="Build runtime",
                description="Continue from directed mailbox work into autonomous task-surface claims.",
                workstreams=(
                    WorkstreamTemplate(
                        workstream_id="runtime",
                        title="Runtime",
                        summary="Own the runtime lane.",
                        team_name="Runtime",
                        budget_max_teammates=1,
                        budget_max_iterations=1,
                    ),
                ),
            )
        )
        bundle = await materialize_planning_result(runtime, planning_result, created_by="superleader-1")
        leader_round = bundle.leader_rounds[0]
        teammate_worker_id = f"{leader_round.team_id}:teammate:1"
        session_id = resident_teammate_session_id(leader_round=leader_round, teammate_slot=1)

        directed_task = await runtime.submit_task(
            group_id=bundle.objective.group_id,
            lane_id=leader_round.lane_id,
            team_id=leader_round.team_id,
            goal="Materialize directed teammate work first",
            scope=TaskScope.TEAM,
            created_by=leader_round.leader_task.leader_id,
        )
        overflow_task = await runtime.submit_task(
            group_id=bundle.objective.group_id,
            lane_id=leader_round.lane_id,
            team_id=leader_round.team_id,
            goal="Then keep going without another leader dispatch",
            scope=TaskScope.TEAM,
            created_by=leader_round.leader_task.leader_id,
        )
        await mailbox.send(
            MailboxEnvelope(
                sender=leader_round.leader_task.leader_id,
                recipient=teammate_worker_id,
                subject="task.directed",
                group_id=bundle.objective.group_id,
                lane_id=leader_round.lane_id,
                team_id=leader_round.team_id,
                payload={"task_id": directed_task.task_id},
            )
        )

        async def _allow(_request) -> PermissionDecision:
            return PermissionDecision(
                approved=True,
                reviewer="system.auto",
                reason="Automatically approved.",
            )

        with tempfile.TemporaryDirectory() as tmpdir:
            surface = TeammateWorkSurface(
                runtime=runtime,
                mailbox=mailbox,
                objective=bundle.objective,
                leader_round=leader_round,
                backend="in_process",
                working_dir=tmpdir,
                turn_index=1,
                session_host=supervisor.session_host,
            )
            result = await surface.run(
                assignments=(),
                request_permission=_allow,
            )

        self.assertEqual(result.teammate_records, ())
        self.assertEqual(result.claimed_task_ids, ())
        self.assertEqual(result.directed_claimed_task_ids, ())
        self.assertEqual(result.autonomous_claimed_task_ids, ())
        self.assertFalse(result.teammate_execution_evidence)
        self.assertIsNotNone(result.coordinator_session)
        assert result.coordinator_session is not None
        self.assertEqual(result.coordinator_session.metadata["slot_count"], 0)
        saved_directed_task = await store.get_task(directed_task.task_id)
        self.assertIsNotNone(saved_directed_task)
        assert saved_directed_task is not None
        self.assertEqual(saved_directed_task.status, TaskStatus.PENDING)
        saved_overflow_task = await store.get_task(overflow_task.task_id)
        self.assertIsNotNone(saved_overflow_task)
        assert saved_overflow_task is not None
        self.assertEqual(saved_overflow_task.status, TaskStatus.PENDING)
        cursor = await mailbox.get_cursor(teammate_worker_id)
        self.assertIsNone(cursor.last_envelope_id)
        self.assertIsNone(await supervisor.session_host.load_session(session_id))

    async def test_run_continues_from_directed_mailbox_into_autonomous_claims(self) -> None:
        store = InMemoryOrchestrationStore()
        bus = InMemoryEventBus()
        supervisor = DefaultWorkerSupervisor(
            store=store,
            launch_backends={"in_process": InProcessLaunchBackend()},
            runner=_CompletedRunner(),
        )
        runtime = GroupRuntime(
            store=store,
            bus=bus,
            launch_backends={"in_process": InProcessLaunchBackend()},
            supervisor=supervisor,
        )
        planner = TemplatePlanner()
        mailbox = InMemoryMailboxBridge()

        await runtime.create_group("group-a")
        planning_result = await planner.build_initial_plan(
            ObjectiveTemplate(
                objective_id="obj-runtime",
                group_id="group-a",
                title="Build runtime",
                description="Continue from directed mailbox work into autonomous task-surface claims.",
                workstreams=(
                    WorkstreamTemplate(
                        workstream_id="runtime",
                        title="Runtime",
                        summary="Own the runtime lane.",
                        team_name="Runtime",
                        budget_max_teammates=1,
                        budget_max_iterations=1,
                    ),
                ),
            )
        )
        bundle = await materialize_planning_result(runtime, planning_result, created_by="superleader-1")
        leader_round = bundle.leader_rounds[0]
        teammate_worker_id = f"{leader_round.team_id}:teammate:1"

        directed_task = await runtime.submit_task(
            group_id=bundle.objective.group_id,
            lane_id=leader_round.lane_id,
            team_id=leader_round.team_id,
            goal="Materialize directed teammate work first",
            scope=TaskScope.TEAM,
            created_by=leader_round.leader_task.leader_id,
        )
        overflow_task = await runtime.submit_task(
            group_id=bundle.objective.group_id,
            lane_id=leader_round.lane_id,
            team_id=leader_round.team_id,
            goal="Then keep going without another leader dispatch",
            scope=TaskScope.TEAM,
            created_by=leader_round.leader_task.leader_id,
        )
        await mailbox.send(
            MailboxEnvelope(
                sender=leader_round.leader_task.leader_id,
                recipient=teammate_worker_id,
                subject="task.directed",
                group_id=bundle.objective.group_id,
                lane_id=leader_round.lane_id,
                team_id=leader_round.team_id,
                payload={"task_id": directed_task.task_id},
            )
        )

        async def _allow(_request) -> PermissionDecision:
            return PermissionDecision(
                approved=True,
                reviewer="system.auto",
                reason="Automatically approved.",
            )

        with tempfile.TemporaryDirectory() as tmpdir:
            priming_surface = TeammateWorkSurface(
                runtime=runtime,
                mailbox=mailbox,
                objective=bundle.objective,
                leader_round=leader_round,
                backend="in_process",
                working_dir=tmpdir,
                turn_index=1,
                session_host=supervisor.session_host,
            )
            await priming_surface.ensure_slot_session(worker_id=teammate_worker_id)

            continuation_surface = TeammateWorkSurface(
                runtime=runtime,
                mailbox=mailbox,
                objective=bundle.objective,
                leader_round=leader_round,
                backend=None,
                working_dir=None,
                turn_index=2,
                session_host=supervisor.session_host,
            )
            result = await continuation_surface.run(
                assignments=(),
                request_permission=_allow,
            )

        self.assertEqual(len(result.teammate_records), 2)
        self.assertEqual(result.directed_claimed_task_ids, (directed_task.task_id,))
        self.assertIn(overflow_task.task_id, result.autonomous_claimed_task_ids)
        saved_directed_task = await store.get_task(directed_task.task_id)
        self.assertIsNotNone(saved_directed_task)
        assert saved_directed_task is not None
        self.assertEqual(saved_directed_task.status, TaskStatus.COMPLETED)
        saved_overflow_task = await store.get_task(overflow_task.task_id)
        self.assertIsNotNone(saved_overflow_task)
        assert saved_overflow_task is not None
        self.assertEqual(saved_overflow_task.status, TaskStatus.COMPLETED)

    async def test_step_runnable_host_slots_drains_directed_mailbox_and_autonomous_claims_without_leader_seed_context(
        self,
    ) -> None:
        store = InMemoryOrchestrationStore()
        bus = InMemoryEventBus()
        supervisor = DefaultWorkerSupervisor(
            store=store,
            launch_backends={"in_process": InProcessLaunchBackend()},
            runner=_CompletedRunner(),
        )
        runtime = GroupRuntime(
            store=store,
            bus=bus,
            launch_backends={"in_process": InProcessLaunchBackend()},
            supervisor=supervisor,
        )
        planner = TemplatePlanner()
        mailbox = InMemoryMailboxBridge()

        await runtime.create_group("group-a")
        planning_result = await planner.build_initial_plan(
            ObjectiveTemplate(
                objective_id="obj-runtime",
                group_id="group-a",
                title="Standalone teammate host sweep",
                description=(
                    "Host-owned runnable slots should keep draining directed mailbox work and then "
                    "continue into autonomous claims without another leader-seeded activation tuple."
                ),
                workstreams=(
                    WorkstreamTemplate(
                        workstream_id="runtime",
                        title="Runtime",
                        summary="Own the runtime lane.",
                        team_name="Runtime",
                        budget_max_teammates=1,
                        budget_max_iterations=1,
                    ),
                ),
            )
        )
        bundle = await materialize_planning_result(runtime, planning_result, created_by="superleader-1")
        leader_round = bundle.leader_rounds[0]
        teammate_worker_id = f"{leader_round.team_id}:teammate:1"

        directed_task = await runtime.submit_task(
            group_id=bundle.objective.group_id,
            lane_id=leader_round.lane_id,
            team_id=leader_round.team_id,
            goal="Materialize directed teammate work first",
            scope=TaskScope.TEAM,
            created_by=leader_round.leader_task.leader_id,
        )
        overflow_task = await runtime.submit_task(
            group_id=bundle.objective.group_id,
            lane_id=leader_round.lane_id,
            team_id=leader_round.team_id,
            goal="Then keep going without another leader dispatch",
            scope=TaskScope.TEAM,
            created_by=leader_round.leader_task.leader_id,
        )
        await mailbox.send(
            MailboxEnvelope(
                sender=leader_round.leader_task.leader_id,
                recipient=teammate_worker_id,
                subject="task.directed",
                group_id=bundle.objective.group_id,
                lane_id=leader_round.lane_id,
                team_id=leader_round.team_id,
                payload={"task_id": directed_task.task_id},
            )
        )

        async def _allow(_request) -> PermissionDecision:
            return PermissionDecision(
                approved=True,
                reviewer="system.auto",
                reason="Automatically approved.",
            )

        with tempfile.TemporaryDirectory() as tmpdir:
            priming_surface = TeammateWorkSurface(
                runtime=runtime,
                mailbox=mailbox,
                objective=bundle.objective,
                leader_round=leader_round,
                backend="in_process",
                working_dir=tmpdir,
                turn_index=1,
                session_host=supervisor.session_host,
            )
            await priming_surface.ensure_slot_session(worker_id=teammate_worker_id)

            continuation_surface = TeammateWorkSurface(
                runtime=runtime,
                mailbox=mailbox,
                objective=bundle.objective,
                leader_round=leader_round,
                backend=None,
                working_dir=None,
                turn_index=2,
                session_host=supervisor.session_host,
            )
            result = await continuation_surface.step_runnable_host_slots(
                request_permission=_allow,
            )

        self.assertEqual(len(result.teammate_records), 2)
        self.assertEqual(result.directed_claimed_task_ids, (directed_task.task_id,))
        self.assertEqual(result.autonomous_claimed_task_ids, (overflow_task.task_id,))
        saved_directed_task = await store.get_task(directed_task.task_id)
        self.assertIsNotNone(saved_directed_task)
        assert saved_directed_task is not None
        self.assertEqual(saved_directed_task.status, TaskStatus.COMPLETED)
        saved_overflow_task = await store.get_task(overflow_task.task_id)
        self.assertIsNotNone(saved_overflow_task)
        assert saved_overflow_task is not None
        self.assertEqual(saved_overflow_task.status, TaskStatus.COMPLETED)

    async def test_step_runnable_host_slots_records_idle_wait_approval_pending(self) -> None:
        store = InMemoryOrchestrationStore()
        bus = InMemoryEventBus()
        supervisor = DefaultWorkerSupervisor(
            store=store,
            launch_backends={"in_process": InProcessLaunchBackend()},
            runner=_CompletedRunner(),
        )
        runtime = GroupRuntime(
            store=store,
            bus=bus,
            launch_backends={"in_process": InProcessLaunchBackend()},
            supervisor=supervisor,
        )
        planner = TemplatePlanner()
        mailbox = InMemoryMailboxBridge()

        await runtime.create_group("group-a")
        planning_result = await planner.build_initial_plan(
            ObjectiveTemplate(
                objective_id="obj-runtime",
                group_id="group-a",
                title="Idle approval pending",
                description="Idle-attached slot should surface pending idle approval in the resident shell view.",
                workstreams=(
                    WorkstreamTemplate(
                        workstream_id="runtime",
                        title="Runtime",
                        summary="Own the runtime lane.",
                        team_name="Runtime",
                        budget_max_teammates=1,
                        budget_max_iterations=1,
                    ),
                ),
            )
        )
        bundle = await materialize_planning_result(runtime, planning_result, created_by="superleader-1")
        leader_round = bundle.leader_rounds[0]
        teammate_profile = build_runtime_role_profiles()["teammate_in_process_fast"]
        teammate_worker_id = f"{leader_round.team_id}:teammate:1"

        await runtime.submit_task(
            group_id=bundle.objective.group_id,
            lane_id=leader_round.lane_id,
            team_id=leader_round.team_id,
            goal="Keep the slot idle-attached after host-owned work drains",
            scope=TaskScope.TEAM,
            created_by=leader_round.leader_task.leader_id,
        )

        async def _approval_queue(request) -> PermissionDecision:
            if request.action == "resident.idle_wait":
                return PermissionDecision(
                    approved=False,
                    reviewer="policy.test",
                    reason="Waiting for resident idle approval.",
                    pending=True,
                )
            return PermissionDecision(
                approved=True,
                reviewer="system.auto",
                reason="Automatically approved.",
            )

        with tempfile.TemporaryDirectory() as tmpdir:
            priming_surface = TeammateWorkSurface(
                runtime=runtime,
                mailbox=mailbox,
                objective=bundle.objective,
                leader_round=leader_round,
                backend="in_process",
                working_dir=tmpdir,
                turn_index=1,
                role_profile=teammate_profile,
                session_host=supervisor.session_host,
            )
            await priming_surface.ensure_slot_session(worker_id=teammate_worker_id)

            continuation_surface = TeammateWorkSurface(
                runtime=runtime,
                mailbox=mailbox,
                objective=bundle.objective,
                leader_round=leader_round,
                backend=None,
                working_dir=None,
                turn_index=2,
                role_profile=None,
                session_host=supervisor.session_host,
            )
            result = await continuation_surface.step_runnable_host_slots(
                request_permission=_approval_queue,
                keep_session_idle=True,
            )

        self.assertEqual(result.coordinator_session.phase, ResidentCoordinatorPhase.IDLE)
        attach_view = await supervisor.session_host.build_shell_attach_view(
            objective_id=bundle.objective.objective_id,
            lane_id=leader_round.lane_id,
            team_id=leader_round.team_id,
        )
        self.assertEqual(attach_view["approval_queue"]["idle_wait"]["status"], "pending")
        self.assertEqual(attach_view["attach_state"]["idle_wait_approval_status"], "pending")

    async def test_step_runnable_host_slots_leaves_slot_idle_attached_with_activation_profile_intact_after_quiescence(
        self,
    ) -> None:
        store = InMemoryOrchestrationStore()
        bus = InMemoryEventBus()
        supervisor = DefaultWorkerSupervisor(
            store=store,
            launch_backends={"in_process": InProcessLaunchBackend()},
            runner=_CompletedRunner(),
        )
        runtime = GroupRuntime(
            store=store,
            bus=bus,
            launch_backends={"in_process": InProcessLaunchBackend()},
            supervisor=supervisor,
        )
        planner = TemplatePlanner()
        mailbox = InMemoryMailboxBridge()

        await runtime.create_group("group-a")
        planning_result = await planner.build_initial_plan(
            ObjectiveTemplate(
                objective_id="obj-runtime",
                group_id="group-a",
                title="Idle-attached host sweep",
                description="Standalone host sweeps should preserve slot activation truth after quiescence.",
                workstreams=(
                    WorkstreamTemplate(
                        workstream_id="runtime",
                        title="Runtime",
                        summary="Own the runtime lane.",
                        team_name="Runtime",
                        budget_max_teammates=1,
                        budget_max_iterations=1,
                    ),
                ),
            )
        )
        bundle = await materialize_planning_result(runtime, planning_result, created_by="superleader-1")
        leader_round = bundle.leader_rounds[0]
        teammate_profile = build_runtime_role_profiles()["teammate_in_process_fast"]
        teammate_worker_id = f"{leader_round.team_id}:teammate:1"
        session_id = resident_teammate_session_id(leader_round=leader_round, teammate_slot=1)

        task = await runtime.submit_task(
            group_id=bundle.objective.group_id,
            lane_id=leader_round.lane_id,
            team_id=leader_round.team_id,
            goal="Keep the slot idle-attached after host-owned work drains",
            scope=TaskScope.TEAM,
            created_by=leader_round.leader_task.leader_id,
        )

        async def _allow(_request) -> PermissionDecision:
            return PermissionDecision(
                approved=True,
                reviewer="system.auto",
                reason="Automatically approved.",
            )

        with tempfile.TemporaryDirectory() as tmpdir:
            priming_surface = TeammateWorkSurface(
                runtime=runtime,
                mailbox=mailbox,
                objective=bundle.objective,
                leader_round=leader_round,
                backend="in_process",
                working_dir=tmpdir,
                turn_index=1,
                role_profile=teammate_profile,
                session_host=supervisor.session_host,
            )
            await priming_surface.ensure_slot_session(worker_id=teammate_worker_id)

            continuation_surface = TeammateWorkSurface(
                runtime=runtime,
                mailbox=mailbox,
                objective=bundle.objective,
                leader_round=leader_round,
                backend=None,
                working_dir=None,
                turn_index=2,
                session_host=supervisor.session_host,
            )
            result = await continuation_surface.step_runnable_host_slots(
                request_permission=_allow,
                keep_session_idle=True,
            )

        self.assertEqual(result.autonomous_claimed_task_ids, (task.task_id,))
        self.assertIsNotNone(result.coordinator_session)
        assert result.coordinator_session is not None
        self.assertEqual(result.coordinator_session.phase, ResidentCoordinatorPhase.IDLE)
        slot_snapshot = await supervisor.session_host.snapshot_session(session_id)
        self.assertIsNotNone(slot_snapshot)
        assert slot_snapshot is not None
        self.assertEqual(slot_snapshot["phase"], ResidentCoordinatorPhase.IDLE.value)
        self.assertEqual(slot_snapshot["metadata"]["activation_profile"]["working_dir"], tmpdir)
        self.assertEqual(
            slot_snapshot["metadata"]["activation_profile"]["role_profile"]["profile_id"],
            teammate_profile.profile_id,
        )
        self.assertEqual(slot_snapshot["current_directive_ids"], [])

    async def test_run_consumes_authority_escalated_envelope_and_updates_wait_metadata(self) -> None:
        store = InMemoryOrchestrationStore()
        bus = InMemoryEventBus()
        supervisor = DefaultWorkerSupervisor(
            store=store,
            launch_backends={"in_process": InProcessLaunchBackend()},
            runner=_CompletedRunner(),
        )
        runtime = GroupRuntime(
            store=store,
            bus=bus,
            launch_backends={"in_process": InProcessLaunchBackend()},
            supervisor=supervisor,
        )
        planner = TemplatePlanner()
        mailbox = InMemoryMailboxBridge()

        await runtime.create_group("group-a")
        planning_result = await planner.build_initial_plan(
            ObjectiveTemplate(
                objective_id="obj-runtime",
                group_id="group-a",
                title="Consume authority-escalated",
                description="Teammate live loop should consume authority escalation writeback and project wait metadata.",
                workstreams=(
                    WorkstreamTemplate(
                        workstream_id="runtime",
                        title="Runtime",
                        summary="Own the runtime lane.",
                        team_name="Runtime",
                        budget_max_teammates=1,
                        budget_max_iterations=1,
                    ),
                ),
            )
        )
        bundle = await materialize_planning_result(runtime, planning_result, created_by="superleader-1")
        leader_round = bundle.leader_rounds[0]
        teammate_worker_id = f"{leader_round.team_id}:teammate:1"
        session_id = resident_teammate_session_id(leader_round=leader_round, teammate_slot=1)

        envelope = await mailbox.send(
            MailboxEnvelope(
                sender=leader_round.leader_task.leader_id,
                recipient=teammate_worker_id,
                subject="authority.escalated",
                group_id=bundle.objective.group_id,
                lane_id=leader_round.lane_id,
                team_id=leader_round.team_id,
                payload={
                    "task_id": "task-authority-2",
                    "authority_request": {
                        "request_id": "auth-req-2",
                        "assignment_id": "task-authority-2:assignment",
                        "worker_id": teammate_worker_id,
                        "task_id": "task-authority-2",
                        "requested_paths": ["src/agent_orchestra/runtime/session_host.py"],
                    },
                    "authority_decision": {
                        "request_id": "auth-req-2",
                        "decision": "escalate",
                        "actor_id": leader_round.leader_task.leader_id,
                        "scope_class": "protected_runtime",
                        "escalated_to": f"objective:{bundle.objective.objective_id}:superleader",
                        "reason": "Escalated to authority root.",
                    },
                },
            )
        )

        async def _allow(_request) -> PermissionDecision:
            return PermissionDecision(
                approved=True,
                reviewer="system.auto",
                reason="Automatically approved.",
            )

        with tempfile.TemporaryDirectory() as tmpdir:
            priming_surface = TeammateWorkSurface(
                runtime=runtime,
                mailbox=mailbox,
                objective=bundle.objective,
                leader_round=leader_round,
                backend="in_process",
                working_dir=tmpdir,
                turn_index=1,
                session_host=supervisor.session_host,
            )
            await priming_surface.ensure_slot_session(worker_id=teammate_worker_id)

            continuation_surface = TeammateWorkSurface(
                runtime=runtime,
                mailbox=mailbox,
                objective=bundle.objective,
                leader_round=leader_round,
                backend=None,
                working_dir=None,
                turn_index=2,
                session_host=supervisor.session_host,
            )
            result = await continuation_surface.run(
                assignments=(),
                request_permission=_allow,
                keep_session_idle=True,
            )

        self.assertEqual(len(result.teammate_records), 0)
        self.assertEqual(result.directed_claimed_task_ids, ())
        self.assertEqual(result.autonomous_claimed_task_ids, ())
        self.assertEqual(result.processed_mailbox_envelope_ids, (envelope.envelope_id,))
        cursor = await mailbox.get_cursor(teammate_worker_id)
        self.assertEqual(cursor.last_envelope_id, envelope.envelope_id)
        session_snapshot = await supervisor.session_host.snapshot_session(session_id)
        self.assertIsNotNone(session_snapshot)
        assert session_snapshot is not None
        metadata = session_snapshot["metadata"]
        self.assertEqual(metadata["authority_request_id"], "auth-req-2")
        self.assertEqual(metadata["authority_waiting_task_id"], "task-authority-2")
        self.assertEqual(metadata["authority_boundary_class"], "protected_runtime")
        self.assertTrue(metadata["authority_waiting"])
        self.assertEqual(metadata["authority_last_requested_by"], leader_round.leader_task.leader_id)

    async def test_execute_assignment_owns_task_status_blackboard_and_result_publication(self) -> None:
        store = InMemoryOrchestrationStore()
        bus = InMemoryEventBus()
        supervisor = DefaultWorkerSupervisor(
            store=store,
            launch_backends={"in_process": InProcessLaunchBackend()},
            runner=_CompletedRunner(),
        )
        runtime = GroupRuntime(
            store=store,
            bus=bus,
            launch_backends={"in_process": InProcessLaunchBackend()},
            supervisor=supervisor,
        )
        planner = TemplatePlanner()
        mailbox = InMemoryMailboxBridge()

        await runtime.create_group("group-a")
        planning_result = await planner.build_initial_plan(
            ObjectiveTemplate(
                objective_id="obj-runtime",
                group_id="group-a",
                title="Build runtime",
                description="Own teammate execution side effects.",
                workstreams=(
                    WorkstreamTemplate(
                        workstream_id="runtime",
                        title="Runtime",
                        summary="Own the runtime lane.",
                        team_name="Runtime",
                        budget_max_teammates=1,
                        budget_max_iterations=1,
                    ),
                ),
            )
        )
        bundle = await materialize_planning_result(runtime, planning_result, created_by="superleader-1")
        leader_round = bundle.leader_rounds[0]
        teammate_slot = 1
        teammate_worker_id = f"{leader_round.team_id}:teammate:{teammate_slot}"
        session_id = resident_teammate_session_id(leader_round=leader_round, teammate_slot=teammate_slot)

        task = await runtime.submit_task(
            group_id=bundle.objective.group_id,
            lane_id=leader_round.lane_id,
            team_id=leader_round.team_id,
            goal="Execute teammate-owned work",
            scope=TaskScope.TEAM,
            created_by=leader_round.leader_task.leader_id,
        )
        delivery_id = f"{bundle.objective.objective_id}:lane:{leader_round.lane_id}"
        await store.save_delivery_state(
            DeliveryState(
                delivery_id=delivery_id,
                objective_id=bundle.objective.objective_id,
                kind=DeliveryStateKind.LANE,
                status=DeliveryStatus.RUNNING,
                lane_id=leader_round.lane_id,
                team_id=leader_round.team_id,
                active_task_ids=(task.task_id,),
                summary="Lane is running.",
            )
        )
        assignment = WorkerAssignment(
            assignment_id=f"{task.task_id}:assignment",
            worker_id=teammate_worker_id,
            group_id=bundle.objective.group_id,
            objective_id=bundle.objective.objective_id,
            team_id=leader_round.team_id,
            lane_id=leader_round.lane_id,
            task_id=task.task_id,
            role="teammate",
            backend="in_process",
            instructions="Run teammate work",
            input_text="run",
        )

        async def _allow(_request) -> PermissionDecision:
            return PermissionDecision(
                approved=True,
                reviewer="system.auto",
                reason="Automatically approved.",
            )

        with tempfile.TemporaryDirectory() as tmpdir:
            surface = TeammateWorkSurface(
                runtime=runtime,
                mailbox=mailbox,
                objective=bundle.objective,
                leader_round=leader_round,
                backend="in_process",
                working_dir=tmpdir,
                turn_index=1,
                session_host=supervisor.session_host,
            )
            execution = await surface.execute_assignment(
                assignment,
                request_permission=_allow,
            )

        self.assertIsNotNone(execution.record)
        assert execution.record is not None
        self.assertEqual(execution.record.status, WorkerStatus.COMPLETED)
        self.assertIsNotNone(execution.source_entry)
        assert execution.source_entry is not None
        self.assertEqual(execution.source_entry.entry_kind, BlackboardEntryKind.EXECUTION_REPORT)
        self.assertIsNotNone(execution.envelope)
        assert execution.envelope is not None
        self.assertEqual(execution.envelope.source_entry_id, execution.source_entry.entry_id)
        self.assertEqual(execution.envelope.subject, "task.result")
        self.assertEqual(execution.envelope.severity, "info")
        saved_task = await store.get_task(task.task_id)
        self.assertIsNotNone(saved_task)
        assert saved_task is not None
        self.assertEqual(saved_task.status, TaskStatus.COMPLETED)
        team_entries = await store.list_blackboard_entries(f"group-a:team:{leader_round.team_id}")
        self.assertIn(execution.source_entry.entry_id, {entry.entry_id for entry in team_entries})
        saved_state = await store.get_delivery_state(delivery_id)
        self.assertIsNotNone(saved_state)
        assert saved_state is not None
        self.assertNotIn(task.task_id, saved_state.active_task_ids)
        self.assertIn(task.task_id, saved_state.completed_task_ids)
        coordination = saved_state.metadata["teammate_coordination"]
        self.assertEqual(coordination["result_count"], 1)
        self.assertEqual(coordination["last_result_task_id"], task.task_id)
        self.assertEqual(coordination["last_result_status"], WorkerStatus.COMPLETED.value)
        session_snapshot = await supervisor.session_host.snapshot_session(session_id)
        self.assertIsNotNone(session_snapshot)
        assert session_snapshot is not None
        self.assertIsNone(session_snapshot["metadata"]["current_task_id"])
        self.assertEqual(session_snapshot["current_directive_ids"], [])

    async def test_execute_assignment_routes_result_convergence_through_store_coordination_commit(self) -> None:
        store = _TrackingCoordinationStore()
        bus = InMemoryEventBus()
        supervisor = DefaultWorkerSupervisor(
            store=store,
            launch_backends={"in_process": InProcessLaunchBackend()},
            runner=_CompletedRunner(),
        )
        runtime = GroupRuntime(
            store=store,
            bus=bus,
            launch_backends={"in_process": InProcessLaunchBackend()},
            supervisor=supervisor,
        )
        planner = TemplatePlanner()
        mailbox = _TrackingMailboxBridge(coordination_events=store.coordination_events)

        await runtime.create_group("group-a")
        planning_result = await planner.build_initial_plan(
            ObjectiveTemplate(
                objective_id="obj-runtime",
                group_id="group-a",
                title="Build runtime",
                description="Route teammate result convergence through the store coordination commit.",
                workstreams=(
                    WorkstreamTemplate(
                        workstream_id="runtime",
                        title="Runtime",
                        summary="Own the runtime lane.",
                        team_name="Runtime",
                        budget_max_teammates=1,
                        budget_max_iterations=1,
                    ),
                ),
            )
        )
        bundle = await materialize_planning_result(runtime, planning_result, created_by="superleader-1")
        leader_round = bundle.leader_rounds[0]
        teammate_worker_id = f"{leader_round.team_id}:teammate:1"
        session_id = resident_teammate_session_id(leader_round=leader_round, teammate_slot=1)
        delivery_id = f"{bundle.objective.objective_id}:lane:{leader_round.lane_id}"

        task = await runtime.submit_task(
            group_id=bundle.objective.group_id,
            lane_id=leader_round.lane_id,
            team_id=leader_round.team_id,
            goal="Commit teammate result through the store transaction",
            scope=TaskScope.TEAM,
            created_by=leader_round.leader_task.leader_id,
        )
        await store.save_delivery_state(
            DeliveryState(
                delivery_id=delivery_id,
                objective_id=bundle.objective.objective_id,
                kind=DeliveryStateKind.LANE,
                status=DeliveryStatus.RUNNING,
                lane_id=leader_round.lane_id,
                team_id=leader_round.team_id,
                active_task_ids=(task.task_id,),
                summary="Lane is running.",
            )
        )
        assignment = WorkerAssignment(
            assignment_id=f"{task.task_id}:assignment",
            worker_id=teammate_worker_id,
            group_id=bundle.objective.group_id,
            objective_id=bundle.objective.objective_id,
            team_id=leader_round.team_id,
            lane_id=leader_round.lane_id,
            task_id=task.task_id,
            role="teammate",
            backend="in_process",
            instructions="Run teammate work",
            input_text="run",
        )

        async def _allow(_request) -> PermissionDecision:
            return PermissionDecision(
                approved=True,
                reviewer="system.auto",
                reason="Automatically approved.",
            )

        with tempfile.TemporaryDirectory() as tmpdir:
            surface = TeammateWorkSurface(
                runtime=runtime,
                mailbox=mailbox,
                objective=bundle.objective,
                leader_round=leader_round,
                backend="in_process",
                working_dir=tmpdir,
                turn_index=1,
                session_host=supervisor.session_host,
            )
            execution = await surface.execute_assignment(
                assignment,
                request_permission=_allow,
            )

        self.assertIsNotNone(execution.record)
        self.assertEqual(len(store.teammate_result_commits), 1)
        commit = store.teammate_result_commits[0]
        expected_worker_session_id = (
            execution.record.session.session_id
            if execution.record.session is not None
            else None
        )
        session_snapshot = _coordination_session_snapshot(commit)
        self.assertIsNotNone(session_snapshot)
        assert session_snapshot is not None
        self.assertEqual(session_snapshot.session_id, session_id)
        self.assertIsNone(session_snapshot.metadata["current_task_id"])
        self.assertEqual(session_snapshot.current_directive_ids, ())
        self.assertEqual(
            session_snapshot.last_worker_session_id,
            expected_worker_session_id,
        )
        self.assertEqual(commit.blackboard_entry.payload["event"], "task.result")
        self.assertEqual(
            commit.blackboard_entry.payload["claim_session_id"],
            commit.task.claim_session_id,
        )
        self.assertEqual(
            commit.blackboard_entry.payload["claim_source"],
            commit.task.claim_source,
        )
        self.assertEqual(
            commit.blackboard_entry.payload["session_snapshot_id"],
            session_id,
        )
        self.assertEqual(
            commit.blackboard_entry.payload["worker_session_id"],
            expected_worker_session_id,
        )
        self.assertLess(
            store.coordination_events.index("store.commit_teammate_result"),
            store.coordination_events.index("mailbox.send:task.result"),
        )
        post_commit_events = store.coordination_events[
            store.coordination_events.index("store.commit_teammate_result") + 1 :
            store.coordination_events.index("mailbox.send:task.result")
        ]
        self.assertEqual(post_commit_events.count("store.save_agent_session"), 1)
        self.assertEqual(post_commit_events.count("store.save_blackboard_entry"), 1)

        session_snapshot_after_commit = await supervisor.session_host.snapshot_session(session_id)
        self.assertIsNotNone(session_snapshot_after_commit)
        assert session_snapshot_after_commit is not None
        self.assertIsNone(session_snapshot_after_commit["metadata"]["current_task_id"])
        self.assertEqual(
            session_snapshot_after_commit["last_worker_session_id"],
            expected_worker_session_id,
        )
        self.assertEqual(session_snapshot_after_commit["current_directive_ids"], [])
        stored_session = await store.get_agent_session(session_id)
        self.assertEqual(stored_session, session_snapshot)
        saved_state = await store.get_delivery_state(delivery_id)
        self.assertIsNotNone(saved_state)
        assert saved_state is not None
        coordination = saved_state.metadata["teammate_coordination"]
        self.assertEqual(
            coordination["last_result_claim_session_id"],
            commit.task.claim_session_id,
        )
        self.assertEqual(
            coordination["last_result_claim_source"],
            commit.task.claim_source,
        )
        self.assertEqual(coordination["last_result_session_id"], session_id)
        self.assertEqual(
            coordination["last_result_worker_session_id"],
            expected_worker_session_id,
        )

    async def test_execute_assignment_records_permission_denial_without_worker_execution(self) -> None:
        store = InMemoryOrchestrationStore()
        bus = InMemoryEventBus()
        supervisor = DefaultWorkerSupervisor(
            store=store,
            launch_backends={"in_process": InProcessLaunchBackend()},
            runner=_CompletedRunner(),
        )
        runtime = GroupRuntime(
            store=store,
            bus=bus,
            launch_backends={"in_process": InProcessLaunchBackend()},
            supervisor=supervisor,
        )
        planner = TemplatePlanner()
        mailbox = InMemoryMailboxBridge()

        await runtime.create_group("group-a")
        planning_result = await planner.build_initial_plan(
            ObjectiveTemplate(
                objective_id="obj-runtime",
                group_id="group-a",
                title="Build runtime",
                description="Block teammate execution through permission denial.",
                workstreams=(
                    WorkstreamTemplate(
                        workstream_id="runtime",
                        title="Runtime",
                        summary="Own the runtime lane.",
                        team_name="Runtime",
                        budget_max_teammates=1,
                        budget_max_iterations=1,
                    ),
                ),
            )
        )
        bundle = await materialize_planning_result(runtime, planning_result, created_by="superleader-1")
        leader_round = bundle.leader_rounds[0]
        teammate_worker_id = f"{leader_round.team_id}:teammate:1"
        continuity = await runtime.new_session(
            group_id=bundle.objective.group_id,
            objective_id=bundle.objective.objective_id,
            title="Teammate authority transition ledger",
        )

        task = await runtime.submit_task(
            group_id=bundle.objective.group_id,
            lane_id=leader_round.lane_id,
            team_id=leader_round.team_id,
            goal="Blocked teammate work",
            scope=TaskScope.TEAM,
            created_by=leader_round.leader_task.leader_id,
        )
        assignment = WorkerAssignment(
            assignment_id=f"{task.task_id}:assignment",
            worker_id=teammate_worker_id,
            group_id=bundle.objective.group_id,
            objective_id=bundle.objective.objective_id,
            team_id=leader_round.team_id,
            lane_id=leader_round.lane_id,
            task_id=task.task_id,
            role="teammate",
            backend="in_process",
            instructions="Run teammate work",
            input_text="run",
        )

        async def _deny(_request) -> PermissionDecision:
            return PermissionDecision(
                approved=False,
                reviewer="policy.test",
                reason="Denied for regression coverage.",
            )

        with tempfile.TemporaryDirectory() as tmpdir:
            surface = TeammateWorkSurface(
                runtime=runtime,
                mailbox=mailbox,
                objective=bundle.objective,
                leader_round=leader_round,
                backend="in_process",
                working_dir=tmpdir,
                turn_index=1,
                session_host=supervisor.session_host,
            )
            execution = await surface.execute_assignment(
                assignment,
                request_permission=_deny,
            )

        self.assertIsNone(execution.record)
        self.assertIsNone(execution.envelope)
        self.assertIsNotNone(execution.source_entry)
        assert execution.source_entry is not None
        self.assertEqual(execution.source_entry.entry_kind, BlackboardEntryKind.BLOCKER)
        self.assertEqual(execution.source_entry.author_id, "supervisor.teammate_work_surface")
        blocked_task = await store.get_task(task.task_id)
        self.assertIsNotNone(blocked_task)
        assert blocked_task is not None
        self.assertEqual(blocked_task.status, TaskStatus.BLOCKED)
        self.assertEqual(blocked_task.blocked_by, ("permission.denied",))

    async def test_execute_assignment_routes_authority_request_without_collapsing_to_failed_task(self) -> None:
        store = _TrackingCoordinationStore()
        bus = InMemoryEventBus()
        supervisor = DefaultWorkerSupervisor(
            store=store,
            launch_backends={"in_process": InProcessLaunchBackend()},
            runner=_CompletedRunner(),
        )
        runtime = GroupRuntime(
            store=store,
            bus=bus,
            launch_backends={"in_process": InProcessLaunchBackend()},
            supervisor=supervisor,
        )
        planner = TemplatePlanner()
        mailbox = InMemoryMailboxBridge()

        await runtime.create_group("group-a")
        planning_result = await planner.build_initial_plan(
            ObjectiveTemplate(
                objective_id="obj-runtime",
                group_id="group-a",
                title="Build runtime",
                description="Convert scope blockers into authority requests.",
                workstreams=(
                    WorkstreamTemplate(
                        workstream_id="runtime",
                        title="Runtime",
                        summary="Own the runtime lane.",
                        team_name="Runtime",
                        budget_max_teammates=1,
                        budget_max_iterations=1,
                    ),
                ),
            )
        )
        bundle = await materialize_planning_result(runtime, planning_result, created_by="superleader-1")
        leader_round = bundle.leader_rounds[0]
        teammate_worker_id = f"{leader_round.team_id}:teammate:1"
        continuity = await runtime.new_session(
            group_id=bundle.objective.group_id,
            objective_id=bundle.objective.objective_id,
            title="Teammate authority transition ledger",
        )

        task = await runtime.submit_task(
            group_id=bundle.objective.group_id,
            lane_id=leader_round.lane_id,
            team_id=leader_round.team_id,
            goal="Request broader authority to repair bootstrap",
            scope=TaskScope.TEAM,
            created_by=leader_round.leader_task.leader_id,
        )
        assignment = WorkerAssignment(
            assignment_id=f"{task.task_id}:assignment",
            worker_id=teammate_worker_id,
            group_id=bundle.objective.group_id,
            objective_id=bundle.objective.objective_id,
            team_id=leader_round.team_id,
            lane_id=leader_round.lane_id,
            task_id=task.task_id,
            role="teammate",
            backend="in_process",
            instructions="Run teammate work",
            input_text="run",
        )

        authority_request_payload = {
            "request_id": "auth-req-1",
            "assignment_id": assignment.assignment_id,
            "worker_id": assignment.worker_id,
            "task_id": task.task_id,
            "requested_paths": ["src/agent_orchestra/self_hosting/bootstrap.py"],
            "reason": "Need to repair an out-of-scope bootstrap import blocker.",
            "evidence": "Importing bootstrap.py raises a SyntaxError during verification.",
            "blocking_verification_command": "python3 -m unittest tests.test_leader_loop -v",
            "retry_hint": "Grant bootstrap.py or reroute to a repair slice.",
        }
        blocked_record = WorkerRecord(
            worker_id=assignment.worker_id,
            assignment_id=assignment.assignment_id,
            backend=assignment.backend,
            role=assignment.role,
            status=WorkerStatus.FAILED,
            error_text="bootstrap.py is outside the current owned_paths.",
            metadata={
                "final_report": {
                    "assignment_id": assignment.assignment_id,
                    "worker_id": assignment.worker_id,
                    "terminal_status": "blocked",
                    "summary": "Need broader authority before continuing.",
                    "blocker": "bootstrap.py is outside the owned_paths scope.",
                    "retry_hint": "Grant bootstrap.py or reroute the repair.",
                    "authority_request": authority_request_payload,
                    "metadata": {"backend": "in_process"},
                }
            },
        )

        async def _allow(_request) -> PermissionDecision:
            return PermissionDecision(
                approved=True,
                reviewer="system.auto",
                reason="Automatically approved.",
            )

        async def _run_blocked_assignment(
            _assignment: WorkerAssignment,
            *,
            policy=None,
        ) -> WorkerRecord:
            return blocked_record

        runtime.run_worker_assignment = _run_blocked_assignment  # type: ignore[assignment]

        with tempfile.TemporaryDirectory() as tmpdir:
            surface = TeammateWorkSurface(
                runtime=runtime,
                mailbox=mailbox,
                objective=bundle.objective,
                leader_round=leader_round,
                backend="in_process",
                working_dir=tmpdir,
                turn_index=1,
                session_host=supervisor.session_host,
            )
            execution = await surface.execute_assignment(
                assignment,
                request_permission=_allow,
            )

        saved_task = await store.get_task(task.task_id)

        self.assertIsNotNone(execution.record)
        assert execution.record is not None
        self.assertEqual(execution.record.status, WorkerStatus.FAILED)
        self.assertEqual(execution.task_status, TaskStatus.WAITING_FOR_AUTHORITY)
        self.assertIsNotNone(execution.source_entry)
        assert execution.source_entry is not None
        self.assertEqual(execution.source_entry.entry_kind, BlackboardEntryKind.PROPOSAL)
        self.assertEqual(execution.source_entry.payload["event"], "authority.request")
        self.assertEqual(
            execution.source_entry.payload["authority_request"]["request_id"],
            "auth-req-1",
        )
        self.assertIsNotNone(execution.envelope)
        assert execution.envelope is not None
        self.assertEqual(execution.envelope.subject, "authority.request")
        self.assertEqual(execution.envelope.visibility_scope.value, "control-private")
        self.assertEqual(
            execution.envelope.payload["authority_request"]["requested_paths"],
            ["src/agent_orchestra/self_hosting/bootstrap.py"],
        )
        self.assertIsNotNone(saved_task)
        assert saved_task is not None
        self.assertEqual(saved_task.status, TaskStatus.WAITING_FOR_AUTHORITY)
        self.assertEqual(saved_task.authority_request_id, "auth-req-1")
        commit_index = store.coordination_events.index("store.commit_authority_request")
        post_commit_events = store.coordination_events[commit_index + 1 :]
        self.assertEqual(post_commit_events.count("store.save_agent_session"), 1)
        records = await store.list_turn_records(
            continuity.work_session.work_session_id,
            runtime_generation_id=continuity.runtime_generation.runtime_generation_id,
            head_kind=ConversationHeadKind.TEAMMATE_SLOT,
            scope_id=assignment.worker_id,
        )
        self.assertTrue(
            any(record.turn_kind == AgentTurnKind.AUTHORITY_TRANSITION for record in records)
        )

    async def test_execute_assignment_rejects_authority_request_when_protocol_contract_failed(self) -> None:
        store = InMemoryOrchestrationStore()
        bus = InMemoryEventBus()
        supervisor = DefaultWorkerSupervisor(
            store=store,
            launch_backends={"in_process": InProcessLaunchBackend()},
            runner=_CompletedRunner(),
        )
        runtime = GroupRuntime(
            store=store,
            bus=bus,
            launch_backends={"in_process": InProcessLaunchBackend()},
            supervisor=supervisor,
        )
        planner = TemplatePlanner()
        mailbox = InMemoryMailboxBridge()

        await runtime.create_group("group-a")
        planning_result = await planner.build_initial_plan(
            ObjectiveTemplate(
                objective_id="obj-runtime",
                group_id="group-a",
                title="Build runtime",
                description="Reject invalid authority requests.",
                workstreams=(
                    WorkstreamTemplate(
                        workstream_id="runtime",
                        title="Runtime",
                        summary="Own the runtime lane.",
                        team_name="Runtime",
                        budget_max_teammates=1,
                        budget_max_iterations=1,
                    ),
                ),
            )
        )
        bundle = await materialize_planning_result(runtime, planning_result, created_by="superleader-1")
        leader_round = bundle.leader_rounds[0]
        teammate_worker_id = f"{leader_round.team_id}:teammate:1"

        task = await runtime.submit_task(
            group_id=bundle.objective.group_id,
            lane_id=leader_round.lane_id,
            team_id=leader_round.team_id,
            goal="Do not accept protocol-invalid authority escalation",
            scope=TaskScope.TEAM,
            created_by=leader_round.leader_task.leader_id,
        )
        assignment = WorkerAssignment(
            assignment_id=f"{task.task_id}:assignment",
            worker_id=teammate_worker_id,
            group_id=bundle.objective.group_id,
            objective_id=bundle.objective.objective_id,
            team_id=leader_round.team_id,
            lane_id=leader_round.lane_id,
            task_id=task.task_id,
            role="teammate",
            backend="in_process",
            instructions="Run teammate work",
            input_text="run",
        )

        blocked_record = WorkerRecord(
            worker_id=assignment.worker_id,
            assignment_id=assignment.assignment_id,
            backend=assignment.backend,
            role=assignment.role,
            status=WorkerStatus.FAILED,
            error_text="Protocol contract failed before authority escalation was accepted.",
            metadata={
                "protocol_failure_reason": "final_report_assignment_mismatch",
                "final_report": {
                    "assignment_id": assignment.assignment_id,
                    "worker_id": assignment.worker_id,
                    "terminal_status": "blocked",
                    "summary": "Need broader authority before continuing.",
                    "blocker": "bootstrap.py is outside the owned_paths scope.",
                    "retry_hint": "Grant bootstrap.py or reroute the repair.",
                    "authority_request": {
                        "request_id": "auth-req-invalid",
                        "assignment_id": assignment.assignment_id,
                        "worker_id": assignment.worker_id,
                        "task_id": task.task_id,
                        "requested_paths": ["src/agent_orchestra/self_hosting/bootstrap.py"],
                        "reason": "Need to repair an out-of-scope bootstrap import blocker.",
                        "evidence": "Importing bootstrap.py raises a SyntaxError during verification.",
                        "blocking_verification_command": "python3 -m unittest tests.test_leader_loop -v",
                        "retry_hint": "Grant bootstrap.py or reroute to a repair slice.",
                    },
                    "metadata": {"backend": "in_process"},
                },
            },
        )

        async def _allow(_request) -> PermissionDecision:
            return PermissionDecision(
                approved=True,
                reviewer="system.auto",
                reason="Automatically approved.",
            )

        async def _run_blocked_assignment(
            _assignment: WorkerAssignment,
            *,
            policy=None,
        ) -> WorkerRecord:
            return blocked_record

        runtime.run_worker_assignment = _run_blocked_assignment  # type: ignore[assignment]

        with tempfile.TemporaryDirectory() as tmpdir:
            surface = TeammateWorkSurface(
                runtime=runtime,
                mailbox=mailbox,
                objective=bundle.objective,
                leader_round=leader_round,
                backend="in_process",
                working_dir=tmpdir,
                turn_index=1,
                session_host=supervisor.session_host,
            )
            execution = await surface.execute_assignment(
                assignment,
                request_permission=_allow,
            )

        saved_task = await store.get_task(task.task_id)

        self.assertIsNotNone(execution.record)
        assert execution.record is not None
        self.assertEqual(execution.record.status, WorkerStatus.FAILED)
        self.assertEqual(execution.task_status, TaskStatus.FAILED)
        self.assertIsNotNone(execution.source_entry)
        assert execution.source_entry is not None
        self.assertEqual(execution.source_entry.entry_kind, BlackboardEntryKind.BLOCKER)
        self.assertEqual(execution.source_entry.payload["event"], "task.result")
        self.assertIsNotNone(execution.envelope)
        assert execution.envelope is not None
        self.assertEqual(execution.envelope.subject, "task.result")
        self.assertIsNotNone(saved_task)
        assert saved_task is not None
        self.assertEqual(saved_task.status, TaskStatus.FAILED)
        self.assertIsNone(saved_task.authority_request_id)

    async def test_execute_assignment_records_failed_result_as_blocker_and_error_envelope(self) -> None:
        store = InMemoryOrchestrationStore()
        bus = InMemoryEventBus()
        supervisor = DefaultWorkerSupervisor(
            store=store,
            launch_backends={"in_process": InProcessLaunchBackend()},
            runner=_FailedRunner(),
        )
        runtime = GroupRuntime(
            store=store,
            bus=bus,
            launch_backends={"in_process": InProcessLaunchBackend()},
            supervisor=supervisor,
        )
        planner = TemplatePlanner()
        mailbox = InMemoryMailboxBridge()

        await runtime.create_group("group-a")
        planning_result = await planner.build_initial_plan(
            ObjectiveTemplate(
                objective_id="obj-runtime",
                group_id="group-a",
                title="Build runtime",
                description="Persist teammate failure side effects.",
                workstreams=(
                    WorkstreamTemplate(
                        workstream_id="runtime",
                        title="Runtime",
                        summary="Own the runtime lane.",
                        team_name="Runtime",
                        budget_max_teammates=1,
                        budget_max_iterations=1,
                    ),
                ),
            )
        )
        bundle = await materialize_planning_result(runtime, planning_result, created_by="superleader-1")
        leader_round = bundle.leader_rounds[0]
        teammate_worker_id = f"{leader_round.team_id}:teammate:1"

        task = await runtime.submit_task(
            group_id=bundle.objective.group_id,
            lane_id=leader_round.lane_id,
            team_id=leader_round.team_id,
            goal="Fail teammate-owned work",
            scope=TaskScope.TEAM,
            created_by=leader_round.leader_task.leader_id,
        )
        assignment = WorkerAssignment(
            assignment_id=f"{task.task_id}:assignment",
            worker_id=teammate_worker_id,
            group_id=bundle.objective.group_id,
            objective_id=bundle.objective.objective_id,
            team_id=leader_round.team_id,
            lane_id=leader_round.lane_id,
            task_id=task.task_id,
            role="teammate",
            backend="in_process",
            instructions="Run failing teammate work",
            input_text="run",
        )

        async def _allow(_request) -> PermissionDecision:
            return PermissionDecision(
                approved=True,
                reviewer="system.auto",
                reason="Automatically approved.",
            )

        with tempfile.TemporaryDirectory() as tmpdir:
            surface = TeammateWorkSurface(
                runtime=runtime,
                mailbox=mailbox,
                objective=bundle.objective,
                leader_round=leader_round,
                backend="in_process",
                working_dir=tmpdir,
                turn_index=1,
                session_host=supervisor.session_host,
            )
            execution = await surface.execute_assignment(
                assignment,
                request_permission=_allow,
            )

        self.assertIsNotNone(execution.record)
        assert execution.record is not None
        self.assertEqual(execution.record.status, WorkerStatus.FAILED)
        self.assertIsNotNone(execution.source_entry)
        assert execution.source_entry is not None
        self.assertEqual(execution.source_entry.entry_kind, BlackboardEntryKind.BLOCKER)
        self.assertEqual(execution.source_entry.payload["error_text"], execution.record.error_text)
        self.assertIsNotNone(execution.envelope)
        assert execution.envelope is not None
        self.assertEqual(execution.envelope.severity, "error")
        self.assertIn("task_status:failed", execution.envelope.tags)
        self.assertEqual(execution.envelope.payload["status"], WorkerStatus.FAILED.value)
        failed_task = await store.get_task(task.task_id)
        self.assertIsNotNone(failed_task)
        assert failed_task is not None
        self.assertEqual(failed_task.status, TaskStatus.FAILED)
