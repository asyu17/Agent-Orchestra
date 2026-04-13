from __future__ import annotations

import asyncio
from dataclasses import replace
import importlib.util
import json
import sys
import tempfile
from pathlib import Path
from unittest import IsolatedAsyncioTestCase

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agent_orchestra.bus.in_memory import InMemoryEventBus
from agent_orchestra.contracts.agent import AgentSession, SessionBinding
from agent_orchestra.contracts.authority import (
    AuthorityBoundaryClass,
    AuthorityCompletionStatus,
    AuthorityDecision,
    AuthorityPolicy,
    ScopeExtensionRequest,
)
from agent_orchestra.contracts.blackboard import BlackboardEntryKind, BlackboardKind
from agent_orchestra.contracts.delivery import DeliveryState, DeliveryStateKind, DeliveryStatus
from agent_orchestra.contracts.enums import AuthorityStatus, EventKind, WorkerStatus
from agent_orchestra.contracts.enums import SpecEdgeKind, SpecNodeKind, TaskScope, TaskStatus
from agent_orchestra.contracts.execution import (
    ResidentCoordinatorPhase,
    ResidentCoordinatorSession,
    WorkerAssignment,
    WorkerBackendCapabilities,
    WorkerExecutionPolicy,
    WorkerHandle,
    WorkerRecord,
    WorkerSession,
    WorkerSessionStatus,
)
from agent_orchestra.contracts.session_continuity import (
    RuntimeGeneration,
    RuntimeGenerationContinuityMode,
    RuntimeGenerationStatus,
    WorkSession,
)
from agent_orchestra.contracts.hierarchical_review import (
    HierarchicalReviewActor,
    HierarchicalReviewActorRole,
    HierarchicalReviewReadMode,
    ReviewItemKind,
)
from agent_orchestra.contracts.runner import AgentRunner, RunnerHealth, RunnerStreamEvent, RunnerTurnRequest, RunnerTurnResult
from agent_orchestra.contracts.task import (
    TaskCard,
    TaskProvenanceKind,
    TaskSurfaceAuthorityVerdict,
    TaskSurfaceMutationKind,
)
from agent_orchestra.planning.template import ObjectiveTemplate, WorkstreamTemplate
from agent_orchestra.planning.template_planner import TemplatePlanner
from agent_orchestra.daemon.client import DaemonClient
from agent_orchestra.daemon.server import DaemonServer
from agent_orchestra.runtime import build_in_memory_orchestra
from agent_orchestra.runtime.backends.in_process import InProcessLaunchBackend
from agent_orchestra.runtime.authority_reactor import collect_lane_authority_completion_snapshot
from agent_orchestra.runtime.group_runtime import GroupRuntime
from agent_orchestra.runtime.session_domain import SessionResumeResult
from agent_orchestra.runtime.worker_supervisor import DefaultWorkerSupervisor
from agent_orchestra.runtime.session_continuity import SessionContinuityState, SessionInspectSnapshot
from agent_orchestra.storage.in_memory import InMemoryOrchestrationStore
from agent_orchestra.tools.mailbox import MailboxDeliveryMode, MailboxDigest, MailboxSubscription, MailboxVisibilityScope


class _SequencedDaemonBackend:
    def __init__(self, *, root: Path, results: list[dict[str, object]]) -> None:
        self.root = root
        self.results = list(results)
        self.launch_count = 0

    def describe_capabilities(self) -> WorkerBackendCapabilities:
        return WorkerBackendCapabilities(
            supports_protocol_contract=True,
            supports_protocol_state=True,
            supports_protocol_final_report=False,
            supports_resume=False,
            supports_reactivate=False,
            supports_artifact_progress=False,
            supports_verification_in_working_dir=True,
        )

    async def launch(self, assignment: WorkerAssignment) -> WorkerHandle:
        self.launch_count += 1
        index = self.launch_count - 1
        step = self.results[index]
        result_file = self.root / f"{assignment.assignment_id}.launch-{self.launch_count}.result.json"
        protocol_file = self.root / f"{assignment.assignment_id}.launch-{self.launch_count}.protocol.json"
        raw_payload = dict(step.get("raw_payload", {}))
        protocol_file.write_text(json.dumps({"protocol_events": []}), encoding="utf-8")
        result_file.write_text(
            json.dumps(
                {
                    "worker_id": assignment.worker_id,
                    "assignment_id": assignment.assignment_id,
                    "status": step.get("status", "completed"),
                    "output_text": step.get("output_text", ""),
                    "error_text": step.get("error_text", ""),
                    "response_id": step.get("response_id"),
                    "usage": {},
                    "raw_payload": raw_payload,
                }
            ),
            encoding="utf-8",
        )
        return WorkerHandle(
            worker_id=assignment.worker_id,
            role=assignment.role,
            backend=assignment.backend,
            run_id=assignment.assignment_id,
            transport_ref=str(result_file),
            metadata={
                "protocol_state_file": str(protocol_file),
                "resume_supported": False,
            },
        )

    async def cancel(self, handle: WorkerHandle) -> None:
        return None

    async def resume(
        self,
        handle: WorkerHandle,
        assignment: WorkerAssignment | None = None,
    ) -> WorkerHandle:
        return handle


class _FakeRunner(AgentRunner):
    def __init__(self) -> None:
        self.requests: list[RunnerTurnRequest] = []

    async def run_turn(self, request: RunnerTurnRequest) -> RunnerTurnResult:
        self.requests.append(request)
        return RunnerTurnResult(
            response_id="resp-worker-1",
            output_text=f"handled:{request.input_text}",
            status="completed",
            usage={"total_tokens": 9},
        )

    async def stream_turn(self, request: RunnerTurnRequest):
        if False:
            yield RunnerStreamEvent(kind=EventKind.RUNNER_COMPLETED)

    async def cancel(self, run_id: str) -> None:
        return None

    async def healthcheck(self) -> RunnerHealth:
        return RunnerHealth(healthy=True, provider="fake")


class _AuthoritativeAuthorityCommitStore(InMemoryOrchestrationStore):
    async def commit_authority_request(self, commit) -> None:
        await super().commit_authority_request(commit)
        task = await self.get_task(commit.task.task_id)
        if task is not None:
            await self.save_task(
                replace(
                    task,
                    reason="persisted authority request reason",
                )
            )
        await self.save_blackboard_entry(
            replace(
                commit.blackboard_entry,
                summary="Persisted authority request entry.",
                payload={
                    **commit.blackboard_entry.payload,
                    "persisted_marker": "authority.request",
                },
            )
        )
        if commit.delivery_state is not None:
            await self.save_delivery_state(
                replace(
                    commit.delivery_state,
                    summary="Persisted authority request delivery.",
                )
            )
        agent_session = getattr(commit, "agent_session", None)
        if agent_session is not None:
            await self.save_agent_session(
                replace(
                    agent_session,
                    metadata={
                        **agent_session.metadata,
                        "persisted_marker": "authority.request",
                    },
                )
            )

    async def commit_authority_decision(self, commit) -> None:
        await super().commit_authority_decision(commit)
        task = await self.get_task(commit.task.task_id)
        if task is not None:
            await self.save_task(
                replace(
                    task,
                    reason="persisted authority decision reason",
                )
            )
        await self.save_blackboard_entry(
            replace(
                commit.blackboard_entry,
                summary="Persisted authority decision entry.",
                payload={
                    **commit.blackboard_entry.payload,
                    "persisted_marker": "authority.decision",
                },
            )
        )
        if commit.delivery_state is not None:
            await self.save_delivery_state(
                replace(
                    commit.delivery_state,
                    summary="Persisted authority decision delivery.",
                )
            )
        agent_session = getattr(commit, "agent_session", None)
        if agent_session is not None:
            await self.save_agent_session(
                replace(
                    agent_session,
                    metadata={
                        **agent_session.metadata,
                        "persisted_marker": "authority.decision",
                    },
                )
            )


class RuntimeTest(IsolatedAsyncioTestCase):
    async def test_daemon_server_keeps_live_session_attachable_after_client_disconnect(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            socket_path = Path(tmpdir) / "agent-orchestra.sock"
            orchestra = build_in_memory_orchestra()
            server = DaemonServer(socket_path=str(socket_path), orchestra=orchestra)
            await server.start()
            try:
                first_client = DaemonClient(socket_path=str(socket_path))
                created = await first_client.request(
                    "session.new",
                    {
                        "group_id": "group-a",
                        "objective_id": "objective-1",
                        "title": "Daemon attach lifecycle",
                    },
                )
                work_session_id = created["continuity"]["work_session"]["work_session_id"]
                runtime_generation_id = created["continuity"]["runtime_generation"]["runtime_generation_id"]
                session_host = orchestra.supervisor.session_host
                leader_session_id = "objective-1:lane-runtime:leader:resident"
                metadata = {
                    "group_id": "group-a",
                    "work_session_id": work_session_id,
                    "runtime_generation_id": runtime_generation_id,
                }
                await session_host.load_or_create_coordinator_session(
                    session_id=leader_session_id,
                    coordinator_id="leader:runtime",
                    objective_id="objective-1",
                    lane_id="lane-runtime",
                    team_id="team-runtime",
                    role="leader",
                    host_owner_coordinator_id="superleader:objective-1",
                    runtime_task_id="runtime-task-1",
                    metadata=metadata,
                )
                await session_host.bind_session(
                    leader_session_id,
                    SessionBinding(
                        session_id=leader_session_id,
                        backend="tmux",
                        binding_type="resident",
                        transport_locator={"session_name": "ao-runtime", "pane_id": "%9"},
                        supervisor_id="daemon-supervisor",
                        lease_id="lease-live",
                        lease_expires_at="2026-04-13T12:30:00+00:00",
                    ),
                )
                await session_host.record_coordinator_session_state(
                    leader_session_id,
                    coordinator_session=ResidentCoordinatorSession(
                        coordinator_id="leader:runtime",
                        role="leader",
                        phase=ResidentCoordinatorPhase.WAITING_FOR_MAILBOX,
                        objective_id="objective-1",
                        lane_id="lane-runtime",
                        team_id="team-runtime",
                        cycle_count=1,
                        prompt_turn_count=1,
                        claimed_task_count=0,
                        subordinate_dispatch_count=0,
                        mailbox_poll_count=1,
                        mailbox_cursor="leader-envelope-live",
                        last_reason="Waiting for mailbox.",
                    ),
                    last_progress_at="2026-04-13T12:00:00+00:00",
                )
                del first_client

                second_client = DaemonClient(socket_path=str(socket_path))
                inspected = await second_client.request(
                    "session.inspect",
                    {"work_session_id": work_session_id},
                )
                attached = await second_client.request(
                    "session.attach",
                    {"work_session_id": work_session_id},
                )

                self.assertTrue(server.is_running)
                self.assertEqual(
                    inspected["snapshot"]["resident_shell_views"][0]["attach_recommendation"]["mode"],
                    "attached",
                )
                self.assertEqual(attached["result"]["action"], "attached")
                self.assertEqual(attached["result"]["metadata"]["preferred_session_id"], leader_session_id)
            finally:
                await server.close()

    async def test_daemon_server_replaces_abnormal_slot_and_emits_restart_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            socket_path = Path(tmpdir) / "agent-orchestra.sock"
            backend = _SequencedDaemonBackend(
                root=Path(tmpdir),
                results=[
                    {
                        "status": "failed",
                        "error_text": "worker transport died",
                        "raw_payload": {
                            "failure_is_process_termination": True,
                            "failure_tags": ["process_termination"],
                        },
                    },
                    {
                        "status": "completed",
                        "output_text": "replacement succeeded",
                        "raw_payload": {},
                    },
                ],
            )
            orchestra = build_in_memory_orchestra(launch_backends={"scripted": backend})
            runtime = orchestra.group_runtime()
            server = DaemonServer(socket_path=str(socket_path), orchestra=orchestra)
            await server.start()
            try:
                client = DaemonClient(socket_path=str(socket_path))
                created = await client.request(
                    "session.new",
                    {
                        "group_id": "group-a",
                        "objective_id": "objective-restart",
                        "title": "Restart lifecycle",
                    },
                )
                work_session_id = created["continuity"]["work_session"]["work_session_id"]
                runtime_generation_id = created["continuity"]["runtime_generation"]["runtime_generation_id"]
                event_stream = client.stream_session_events(work_session_id=work_session_id)
                
                async def _next_restart_event() -> dict[str, object]:
                    async for event in event_stream:
                        if event.get("command") == "slot.restart_queued":
                            return event
                    raise AssertionError("session event stream closed before slot.restart_queued")

                event_task = asyncio.create_task(_next_restart_event())
                await asyncio.sleep(0.05)

                first_record = await runtime.run_worker_assignment(
                    WorkerAssignment(
                        assignment_id="assign-restart",
                        worker_id="worker-restart",
                        group_id="group-a",
                        team_id="team-a",
                        task_id="task-restart",
                        role="teammate",
                        backend="scripted",
                        instructions="Retry after process termination.",
                        input_text="run",
                        metadata={
                            "work_session_id": work_session_id,
                            "runtime_generation_id": runtime_generation_id,
                            "slot_id": "slot:team-a:teammate:1",
                        },
                    ),
                    policy=WorkerExecutionPolicy(
                        max_attempts=1,
                        allow_relaunch=False,
                        escalate_after_attempts=False,
                    ),
                )
                self.assertEqual(first_record.status, WorkerStatus.FAILED)
                restart_event = await asyncio.wait_for(event_task, timeout=3.0)
                await event_stream.aclose()
                await asyncio.sleep(0.6)

                slot = await orchestra.store.get_agent_slot("slot:team-a:teammate:1")
                incarnations = await orchestra.store.list_agent_incarnations(
                    slot_id="slot:team-a:teammate:1"
                )

                self.assertEqual(restart_event["command"], "slot.restart_queued")
                self.assertEqual(restart_event["work_session_id"], work_session_id)
                self.assertIsNotNone(slot)
                assert slot is not None
                self.assertEqual(slot.restart_count, 1)
                self.assertEqual(backend.launch_count, 2)
                self.assertGreaterEqual(len(incarnations), 2)
            finally:
                await server.close()

    async def test_group_runtime_accepts_session_domain_service_for_user_visible_session_calls(self) -> None:
        store = InMemoryOrchestrationStore()
        bus = InMemoryEventBus()
        work_session = WorkSession(
            work_session_id="worksession-delegated",
            group_id="group-a",
            root_objective_id="obj-runtime",
            title="delegated",
            status="open",
            created_at="2026-04-12T00:00:00+00:00",
            updated_at="2026-04-12T00:00:00+00:00",
            current_runtime_generation_id="runtimegen-delegated",
        )
        runtime_generation = RuntimeGeneration(
            runtime_generation_id="runtimegen-delegated",
            work_session_id=work_session.work_session_id,
            generation_index=0,
            status=RuntimeGenerationStatus.BOOTING,
            continuity_mode=RuntimeGenerationContinuityMode.FRESH,
            created_at="2026-04-12T00:00:00+00:00",
            group_id="group-a",
            objective_id="obj-runtime",
        )
        continuity_state = SessionContinuityState(
            work_session=work_session,
            runtime_generation=runtime_generation,
        )
        snapshot = SessionInspectSnapshot(
            work_session=work_session,
        )

        class _FakeSessionDomainService:
            def __init__(self) -> None:
                self.calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

            async def new_session(self, **kwargs):
                self.calls.append(("new_session", (), dict(kwargs)))
                return continuity_state

            async def list_sessions(self, **kwargs):
                self.calls.append(("list_sessions", (), dict(kwargs)))
                return (continuity_state.work_session,)

            async def inspect_session(self, work_session_id: str):
                self.calls.append(("inspect_session", (work_session_id,), {}))
                return snapshot

            async def attach_session(self, work_session_id: str, force_warm_resume: bool = False):
                self.calls.append(
                    ("attach_session", (work_session_id,), {"force_warm_resume": force_warm_resume})
                )
                decision = await GroupRuntime(store=store, bus=bus).resume_gate(work_session_id)
                return SessionResumeResult(action="attached", decision=decision)

            async def wake_session(self, work_session_id: str):
                self.calls.append(("wake_session", (work_session_id,), {}))
                decision = await GroupRuntime(store=store, bus=bus).resume_gate(work_session_id)
                return SessionResumeResult(action="recovered", decision=decision)

        service = _FakeSessionDomainService()

        runtime = GroupRuntime(
            store=store,
            bus=bus,
            session_domain_service=service,  # type: ignore[call-arg]
        )

        created = await runtime.new_session(
            group_id="group-a",
            objective_id="obj-runtime",
            title="delegated",
        )
        sessions = await runtime.list_work_sessions(group_id="group-a")
        inspected = await runtime.inspect_session("worksession-delegated")
        attached = await runtime.attach_session("worksession-delegated", force_warm_resume=True)
        woken = await runtime.wake_session("worksession-delegated")

        self.assertIs(created, continuity_state)
        self.assertEqual(sessions, (continuity_state.work_session,))
        self.assertIs(inspected, snapshot)
        self.assertEqual(attached.action, "attached")
        self.assertEqual(woken.action, "recovered")
        self.assertEqual(
            [call[0] for call in service.calls],
            ["new_session", "list_sessions", "inspect_session", "attach_session", "wake_session"],
        )

    def _skip_unless_planning_review_available(self, store: InMemoryOrchestrationStore) -> None:
        if importlib.util.find_spec("agent_orchestra.contracts.planning_review") is None:
            self.skipTest("planning_review contracts are not available in this workspace snapshot.")
        required_methods = (
            "save_leader_draft_plan",
            "list_leader_draft_plans",
            "save_leader_peer_review",
            "list_leader_peer_reviews",
            "save_superleader_global_review",
            "get_superleader_global_review",
            "save_leader_revised_plan",
            "list_leader_revised_plans",
        )
        missing = [name for name in required_methods if not hasattr(store, name)]
        if missing:
            self.skipTest(
                "planning review store APIs are not available yet: " + ", ".join(missing)
            )

    async def test_group_runtime_creates_project_item(self) -> None:
        store = InMemoryOrchestrationStore()
        bus = InMemoryEventBus()
        runtime = GroupRuntime(store=store, bus=bus)

        await runtime.create_group("group-a")
        objective = await runtime.create_objective(
            group_id="group-a",
            objective_id="obj-review",
            title="Review objective",
            description="Exercise hierarchical review item creation.",
        )

        item = await runtime.create_review_item(
            objective_id=objective.objective_id,
            item_id="project-item-runtime",
            item_kind=ReviewItemKind.PROJECT_ITEM,
            title="Shared interface decision",
            summary="Cross-team review surface.",
            metadata={"shared_module": "src/agent_orchestra/runtime/group_runtime.py"},
        )

        self.assertEqual(item.item_id, "project-item-runtime")
        self.assertEqual(item.item_kind, ReviewItemKind.PROJECT_ITEM)
        self.assertEqual(item.metadata["shared_module"], "src/agent_orchestra/runtime/group_runtime.py")

    async def test_group_runtime_rejects_teammate_review_item_creation(self) -> None:
        store = InMemoryOrchestrationStore()
        bus = InMemoryEventBus()
        runtime = GroupRuntime(store=store, bus=bus)

        await runtime.create_group("group-a")
        await runtime.create_team(group_id="group-a", team_id="team-a", name="Runtime")
        objective = await runtime.create_objective(
            group_id="group-a",
            objective_id="obj-review",
            title="Review objective",
            description="Exercise review item authorization.",
        )

        with self.assertRaises(PermissionError):
            await runtime.create_review_item(
                objective_id=objective.objective_id,
                item_id="project-item-runtime",
                item_kind=ReviewItemKind.PROJECT_ITEM,
                title="Shared interface decision",
                summary="Cross-team review surface.",
                actor=HierarchicalReviewActor(
                    actor_id="teammate:team-a:1",
                    role=HierarchicalReviewActorRole.TEAMMATE,
                    team_id="team-a",
                ),
            )

    async def test_group_runtime_publishes_team_position_review(self) -> None:
        store = InMemoryOrchestrationStore()
        bus = InMemoryEventBus()
        runtime = GroupRuntime(store=store, bus=bus)

        await runtime.create_group("group-a")
        await runtime.create_team(group_id="group-a", team_id="team-a", name="Runtime")
        objective = await runtime.create_objective(
            group_id="group-a",
            objective_id="obj-review",
            title="Review objective",
            description="Exercise hierarchical team review publishing.",
        )
        task = await runtime.submit_task(
            group_id="group-a",
            team_id="team-a",
            lane_id="runtime",
            goal="Implement runtime review APIs",
            scope=TaskScope.TEAM,
            created_by="leader:team-a",
        )
        await runtime.create_review_item(
            objective_id=objective.objective_id,
            item_id="project-item-runtime",
            item_kind=ReviewItemKind.PROJECT_ITEM,
            source_task_id=task.task_id,
            lane_id="runtime",
            team_id="team-a",
            title="Shared review item",
            summary="Project-scoped review surface.",
        )

        original_task = await store.get_task(task.task_id)
        review = await runtime.publish_team_position_review(
            item_id="project-item-runtime",
            team_id="team-a",
            leader_id="leader:team-a",
            reviewed_at="2026-04-07T13:00:00+00:00",
            based_on_task_review_revision_ids=("rev-1", "rev-2"),
            team_stance="runtime_owns_this",
            summary="Team A thinks runtime should own the implementation.",
            key_risks=("store coupling",),
            key_dependencies=("postgres schema",),
            recommended_next_action="Implement persistence first.",
            confidence=0.84,
        )

        persisted_task = await store.get_task(task.task_id)
        assert original_task is not None
        assert persisted_task is not None
        self.assertEqual(review.team_id, "team-a")
        self.assertEqual(review.item_kind, ReviewItemKind.PROJECT_ITEM)
        self.assertEqual(persisted_task.status, original_task.status)
        self.assertEqual(persisted_task.owner_id, original_task.owner_id)

    async def test_group_runtime_rejects_foreign_leader_team_position_publish(self) -> None:
        store = InMemoryOrchestrationStore()
        bus = InMemoryEventBus()
        runtime = GroupRuntime(store=store, bus=bus)

        await runtime.create_group("group-a")
        await runtime.create_team(group_id="group-a", team_id="team-a", name="Runtime")
        await runtime.create_team(group_id="group-a", team_id="team-b", name="Storage")
        objective = await runtime.create_objective(
            group_id="group-a",
            objective_id="obj-review",
            title="Review objective",
            description="Exercise team position authorization.",
        )
        await runtime.create_review_item(
            objective_id=objective.objective_id,
            item_id="project-item-runtime",
            item_kind=ReviewItemKind.PROJECT_ITEM,
            team_id="team-a",
            title="Shared interface decision",
            summary="Cross-team review surface.",
        )

        with self.assertRaises(PermissionError):
            await runtime.publish_team_position_review(
                item_id="project-item-runtime",
                team_id="team-a",
                leader_id="leader:team-a",
                reviewed_at="2026-04-07T13:00:00+00:00",
                team_stance="runtime_owns_this",
                summary="Forged cross-team write should be rejected.",
                actor=HierarchicalReviewActor(
                    actor_id="leader:team-b",
                    role=HierarchicalReviewActorRole.LEADER,
                    team_id="team-b",
                ),
            )

    async def test_group_runtime_publishes_cross_team_leader_review(self) -> None:
        store = InMemoryOrchestrationStore()
        bus = InMemoryEventBus()
        runtime = GroupRuntime(store=store, bus=bus)

        await runtime.create_group("group-a")
        objective = await runtime.create_objective(
            group_id="group-a",
            objective_id="obj-review",
            title="Review objective",
            description="Exercise cross-team leader review publishing.",
        )
        await runtime.create_review_item(
            objective_id=objective.objective_id,
            item_id="project-item-runtime",
            item_kind=ReviewItemKind.PROJECT_ITEM,
            title="Shared interface decision",
            summary="Cross-team review surface.",
        )
        target_review = await runtime.publish_team_position_review(
            item_id="project-item-runtime",
            team_id="team-a",
            leader_id="leader:team-a",
            reviewed_at="2026-04-07T13:00:00+00:00",
            team_stance="runtime_owns_this",
            summary="Team A recommends runtime ownership.",
        )

        review = await runtime.publish_cross_team_leader_review(
            item_id="project-item-runtime",
            reviewer_team_id="team-b",
            reviewer_leader_id="leader:team-b",
            target_team_id="team-a",
            target_position_review_id=target_review.position_review_id,
            reviewed_at="2026-04-07T13:05:00+00:00",
            stance="support_with_adjustment",
            agreement_level="partial",
            what_changed_in_my_understanding="Team A highlighted rollout ordering risk.",
            challenge_or_support="support",
            suggested_adjustment="Add a project-phase gate.",
            confidence=0.71,
        )

        self.assertEqual(review.reviewer_team_id, "team-b")
        self.assertEqual(review.target_team_id, "team-a")
        self.assertIn("rollout ordering risk", review.what_changed_in_my_understanding)

    async def test_group_runtime_reads_hierarchical_review_context_for_project_item(self) -> None:
        store = InMemoryOrchestrationStore()
        bus = InMemoryEventBus()
        runtime = GroupRuntime(store=store, bus=bus)

        await runtime.create_group("group-a")
        objective = await runtime.create_objective(
            group_id="group-a",
            objective_id="obj-review",
            title="Review objective",
            description="Exercise hierarchical context reads.",
        )
        item = await runtime.create_review_item(
            objective_id=objective.objective_id,
            item_id="project-item-runtime",
            item_kind=ReviewItemKind.PROJECT_ITEM,
            title="Shared interface decision",
            summary="Cross-team review surface.",
        )
        team_review = await runtime.publish_team_position_review(
            item_id=item.item_id,
            team_id="team-a",
            leader_id="leader:team-a",
            reviewed_at="2026-04-07T13:00:00+00:00",
            team_stance="runtime_owns_this",
            summary="Team A recommends runtime ownership.",
        )
        cross_review = await runtime.publish_cross_team_leader_review(
            item_id=item.item_id,
            reviewer_team_id="team-b",
            reviewer_leader_id="leader:team-b",
            target_team_id="team-a",
            target_position_review_id=team_review.position_review_id,
            reviewed_at="2026-04-07T13:05:00+00:00",
            stance="support_with_adjustment",
            agreement_level="partial",
            what_changed_in_my_understanding="Team A highlighted rollout ordering risk.",
            challenge_or_support="support",
            suggested_adjustment="Add a project-phase gate.",
        )
        synthesis = await runtime.publish_superleader_synthesis(
            item_id=item.item_id,
            superleader_id="superleader:obj-review",
            synthesized_at="2026-04-07T13:10:00+00:00",
            based_on_team_position_review_ids=(team_review.position_review_id,),
            based_on_cross_team_review_ids=(cross_review.cross_review_id,),
            final_position="Proceed with runtime ownership after project gate.",
            next_actions=("implement store API",),
        )

        context = await runtime.get_project_item_review_context(item.item_id)

        self.assertEqual(context["item"].item_id, item.item_id)
        self.assertEqual(len(context["team_position_reviews"]), 1)
        self.assertEqual(len(context["cross_team_leader_reviews"]), 1)
        self.assertEqual(context["superleader_synthesis"].synthesis_id, synthesis.synthesis_id)

    async def test_group_runtime_redacts_foreign_review_context_for_leader(self) -> None:
        store = InMemoryOrchestrationStore()
        bus = InMemoryEventBus()
        runtime = GroupRuntime(store=store, bus=bus)

        leader_a = HierarchicalReviewActor(
            actor_id="leader:team-a",
            role=HierarchicalReviewActorRole.LEADER,
            team_id="team-a",
        )
        leader_b = HierarchicalReviewActor(
            actor_id="leader:team-b",
            role=HierarchicalReviewActorRole.LEADER,
            team_id="team-b",
        )
        superleader = HierarchicalReviewActor(
            actor_id="superleader:obj-review",
            role=HierarchicalReviewActorRole.SUPERLEADER,
        )

        await runtime.create_group("group-a")
        await runtime.create_team(group_id="group-a", team_id="team-a", name="Runtime")
        await runtime.create_team(group_id="group-a", team_id="team-b", name="Storage")
        objective = await runtime.create_objective(
            group_id="group-a",
            objective_id="obj-review",
            title="Review objective",
            description="Exercise redacted project-item context reads.",
        )
        item = await runtime.create_review_item(
            objective_id=objective.objective_id,
            item_id="project-item-runtime",
            item_kind=ReviewItemKind.PROJECT_ITEM,
            title="Shared interface decision",
            summary="Cross-team review surface.",
            actor=leader_a,
        )
        team_a_review = await runtime.publish_team_position_review(
            item_id=item.item_id,
            team_id="team-a",
            leader_id="leader:team-a",
            reviewed_at="2026-04-07T13:00:00+00:00",
            based_on_task_review_revision_ids=("rev-a-1",),
            team_stance="runtime_owns_this",
            summary="Team A recommends runtime ownership.",
            key_risks=("store coupling",),
            key_dependencies=("postgres schema",),
            recommended_next_action="Implement persistence first.",
            confidence=0.83,
            evidence_refs=("bb:entry:team-a",),
            metadata={"private_note": "team-a-full"},
            actor=leader_a,
        )
        team_b_review = await runtime.publish_team_position_review(
            item_id=item.item_id,
            team_id="team-b",
            leader_id="leader:team-b",
            reviewed_at="2026-04-07T13:02:00+00:00",
            based_on_task_review_revision_ids=("rev-b-1",),
            team_stance="support_with_adjustment",
            summary="Team B supports with rollout ordering constraints.",
            key_risks=("migration ordering",),
            key_dependencies=("release window",),
            recommended_next_action="Stage rollout.",
            confidence=0.72,
            evidence_refs=("bb:entry:team-b",),
            metadata={"private_note": "team-b-full"},
            actor=leader_b,
        )
        cross_review = await runtime.publish_cross_team_leader_review(
            item_id=item.item_id,
            reviewer_team_id="team-b",
            reviewer_leader_id="leader:team-b",
            target_team_id="team-a",
            target_position_review_id=team_a_review.position_review_id,
            reviewed_at="2026-04-07T13:05:00+00:00",
            stance="support_with_adjustment",
            agreement_level="partial",
            what_changed_in_my_understanding="Team A highlighted rollout ordering risk.",
            challenge_or_support="support",
            suggested_adjustment="Add a project-phase gate.",
            confidence=0.71,
            evidence_refs=("bb:entry:cross-1",),
            metadata={"private_note": "cross-full"},
            actor=leader_b,
        )
        await runtime.publish_superleader_synthesis(
            item_id=item.item_id,
            superleader_id="superleader:obj-review",
            synthesized_at="2026-04-07T13:10:00+00:00",
            based_on_team_position_review_ids=(
                team_a_review.position_review_id,
                team_b_review.position_review_id,
            ),
            based_on_cross_team_review_ids=(cross_review.cross_review_id,),
            final_position="Proceed with runtime ownership after project gate.",
            accepted_risks=("slower rollout",),
            open_questions=("Who owns cutover?",),
            next_actions=("implement store API",),
            confidence=0.79,
            evidence_refs=("bb:entry:synth-1",),
            metadata={"private_note": "synth-full"},
            actor=superleader,
        )

        context = await runtime.get_project_item_review_context(item.item_id, actor=leader_a)

        team_reviews = {
            review.team_id: review
            for review in context["team_position_reviews"]
        }
        own_team_review = team_reviews["team-a"]
        foreign_team_review = team_reviews["team-b"]
        cross_review = context["cross_team_leader_reviews"][0]
        synthesis = context["superleader_synthesis"]

        self.assertEqual(own_team_review.key_risks, ("store coupling",))
        self.assertEqual(own_team_review.based_on_task_review_revision_ids, ("rev-a-1",))
        self.assertEqual(own_team_review.metadata["private_note"], "team-a-full")
        self.assertEqual(foreign_team_review.key_risks, ())
        self.assertEqual(foreign_team_review.based_on_task_review_revision_ids, ("rev-b-1",))
        self.assertEqual(foreign_team_review.evidence_refs, ("bb:entry:team-b",))
        self.assertEqual(foreign_team_review.metadata, {})
        self.assertEqual(cross_review.what_changed_in_my_understanding, "")
        self.assertEqual(cross_review.evidence_refs, ("bb:entry:cross-1",))
        self.assertIsNotNone(synthesis)
        assert synthesis is not None
        self.assertEqual(synthesis.final_position, "Proceed with runtime ownership after project gate.")
        self.assertEqual(synthesis.accepted_risks, ())
        self.assertEqual(synthesis.based_on_team_position_review_ids, ())

    async def test_group_runtime_materializes_review_digest_view_with_capped_read_modes(self) -> None:
        store = InMemoryOrchestrationStore()
        bus = InMemoryEventBus()
        runtime = GroupRuntime(store=store, bus=bus)

        leader_a = HierarchicalReviewActor(
            actor_id="leader:team-a",
            role=HierarchicalReviewActorRole.LEADER,
            team_id="team-a",
        )
        leader_b = HierarchicalReviewActor(
            actor_id="leader:team-b",
            role=HierarchicalReviewActorRole.LEADER,
            team_id="team-b",
        )
        superleader = HierarchicalReviewActor(
            actor_id="superleader:obj-review",
            role=HierarchicalReviewActorRole.SUPERLEADER,
        )

        await runtime.create_group("group-a")
        await runtime.create_team(group_id="group-a", team_id="team-a", name="Runtime")
        await runtime.create_team(group_id="group-a", team_id="team-b", name="Storage")
        objective = await runtime.create_objective(
            group_id="group-a",
            objective_id="obj-review",
            title="Review objective",
            description="Exercise digest materialization for review subscriptions.",
        )
        item = await runtime.create_review_item(
            objective_id=objective.objective_id,
            item_id="project-item-runtime",
            item_kind=ReviewItemKind.PROJECT_ITEM,
            title="Shared interface decision",
            summary="Cross-team review surface.",
            actor=leader_a,
        )
        team_a_review = await runtime.publish_team_position_review(
            item_id=item.item_id,
            team_id="team-a",
            leader_id="leader:team-a",
            reviewed_at="2026-04-07T13:00:00+00:00",
            based_on_task_review_revision_ids=("rev-a-1",),
            team_stance="runtime_owns_this",
            summary="Team A recommends runtime ownership.",
            actor=leader_a,
        )
        await runtime.publish_team_position_review(
            item_id=item.item_id,
            team_id="team-b",
            leader_id="leader:team-b",
            reviewed_at="2026-04-07T13:02:00+00:00",
            based_on_task_review_revision_ids=("rev-b-1",),
            team_stance="support_with_adjustment",
            summary="Team B supports with rollout ordering constraints.",
            actor=leader_b,
        )
        await runtime.publish_cross_team_leader_review(
            item_id=item.item_id,
            reviewer_team_id="team-b",
            reviewer_leader_id="leader:team-b",
            target_team_id="team-a",
            target_position_review_id=team_a_review.position_review_id,
            reviewed_at="2026-04-07T13:05:00+00:00",
            stance="support_with_adjustment",
            agreement_level="partial",
            what_changed_in_my_understanding="Team A highlighted rollout ordering risk.",
            challenge_or_support="support",
            suggested_adjustment="Add a project-phase gate.",
            actor=leader_b,
        )
        await runtime.publish_superleader_synthesis(
            item_id=item.item_id,
            superleader_id="superleader:obj-review",
            synthesized_at="2026-04-07T13:10:00+00:00",
            based_on_team_position_review_ids=("teampos-a", "teampos-b"),
            based_on_cross_team_review_ids=("cross-1",),
            final_position="Proceed with runtime ownership after project gate.",
            next_actions=("implement store API",),
            actor=superleader,
        )

        view = await runtime.materialize_review_digest_view(item.item_id, actor=leader_a)

        self.assertEqual(view.snapshot.current_phase.value, "superleader_synthesis")
        self.assertEqual(view.snapshot.team_position_review_count, 2)
        self.assertEqual(view.snapshot.cross_team_leader_review_count, 1)
        self.assertEqual(len(view.team_position_digests), 2)
        self.assertEqual(len(view.cross_team_leader_digests), 1)
        self.assertIsNotNone(view.superleader_synthesis_digest)
        self.assertTrue(
            all(
                digest.visibility.read_mode == HierarchicalReviewReadMode.SUMMARY_PLUS_REF
                for digest in view.team_position_digests
            )
        )
        self.assertEqual(
            view.cross_team_leader_digests[0].visibility.read_mode,
            HierarchicalReviewReadMode.SUMMARY_PLUS_REF,
        )
        assert view.superleader_synthesis_digest is not None
        self.assertEqual(
            view.superleader_synthesis_digest.visibility.read_mode,
            HierarchicalReviewReadMode.SUMMARY_ONLY,
        )
        self.assertIsNone(view.superleader_synthesis_digest.review_ref)

    async def test_group_runtime_authorize_review_digest_view_reuses_item_scope_policy(self) -> None:
        store = InMemoryOrchestrationStore()
        bus = InMemoryEventBus()
        runtime = GroupRuntime(store=store, bus=bus)

        leader_a = HierarchicalReviewActor(
            actor_id="leader:team-a",
            role=HierarchicalReviewActorRole.LEADER,
            team_id="team-a",
        )
        teammate_a = HierarchicalReviewActor(
            actor_id="team-a:teammate:1",
            role=HierarchicalReviewActorRole.TEAMMATE,
            team_id="team-a",
        )
        teammate_b = HierarchicalReviewActor(
            actor_id="team-b:teammate:1",
            role=HierarchicalReviewActorRole.TEAMMATE,
            team_id="team-b",
        )

        await runtime.create_group("group-a")
        await runtime.create_team(group_id="group-a", team_id="team-a", name="Runtime")
        objective = await runtime.create_objective(
            group_id="group-a",
            objective_id="obj-review",
            title="Review objective",
            description="Exercise digest-view authorization.",
        )
        task_item = await runtime.create_review_item(
            objective_id=objective.objective_id,
            item_id="task-item-runtime",
            item_kind=ReviewItemKind.TASK_ITEM,
            team_id="team-a",
            title="Team-local task review item",
            summary="Control-private digest surface.",
            actor=leader_a,
        )

        teammate_visibility = await runtime.authorize_review_digest_view(
            task_item.item_id,
            actor=teammate_a,
        )
        self.assertEqual(teammate_visibility.visibility_scope, "control-private")
        self.assertEqual(
            teammate_visibility.read_mode,
            HierarchicalReviewReadMode.SUMMARY_PLUS_REF,
        )

        with self.assertRaises(PermissionError):
            await runtime.authorize_review_digest_view(task_item.item_id, actor=teammate_b)

    async def test_group_runtime_publishes_and_lists_planning_review_artifacts(self) -> None:
        store = InMemoryOrchestrationStore()
        self._skip_unless_planning_review_available(store)
        bus = InMemoryEventBus()
        runtime = GroupRuntime(store=store, bus=bus)

        await runtime.create_group("group-a")
        objective = await runtime.create_objective(
            group_id="group-a",
            objective_id="obj-planning-review",
            title="Planning review objective",
            description="Exercise leader draft/peer/global/revised runtime APIs.",
        )
        await runtime.create_team(
            group_id="group-a",
            team_id="group-a:team:runtime",
            name="Runtime",
        )
        await runtime.create_team(
            group_id="group-a",
            team_id="group-a:team:infra",
            name="Infra",
        )

        runtime_draft = await runtime.publish_leader_draft_plan(
            objective_id=objective.objective_id,
            planning_round_id="round-1",
            leader_id="leader:runtime",
            lane_id="runtime",
            team_id="group-a:team:runtime",
            summary="Runtime initial plan.",
            sequential_slices=(
                {
                    "slice_id": "runtime-core",
                    "title": "Runtime core",
                    "goal": "Implement runtime core path.",
                    "reason": "Required before infra integration.",
                    "mode": "sequential",
                    "owned_paths": ["src/agent_orchestra/runtime/group_runtime.py"],
                },
            ),
            parallel_slices=(),
            shared_hotspots=("src/agent_orchestra/runtime/group_runtime.py",),
        )
        infra_draft = await runtime.publish_leader_draft_plan(
            objective_id=objective.objective_id,
            planning_round_id="round-1",
            leader_id="leader:infra",
            lane_id="infra",
            team_id="group-a:team:infra",
            summary="Infra initial plan.",
            sequential_slices=(
                {
                    "slice_id": "infra-schema",
                    "title": "Infra schema",
                    "goal": "Implement store schema path.",
                    "reason": "Needed for persistent state.",
                    "mode": "sequential",
                    "owned_paths": ["src/agent_orchestra/storage/postgres/store.py"],
                },
            ),
            parallel_slices=(),
        )

        review_a = await runtime.publish_leader_peer_review(
            objective_id=objective.objective_id,
            planning_round_id="round-1",
            reviewer_leader_id="leader:runtime",
            reviewer_team_id="group-a:team:runtime",
            target_leader_id="leader:infra",
            target_team_id="group-a:team:infra",
            summary="Infra plan overlaps shared runtime hotspot.",
            conflict_type="shared_hotspot_conflict",
            severity="high",
            affected_paths=("src/agent_orchestra/runtime/group_runtime.py",),
            reason="Both plans touch the runtime hotspot in the first slice.",
            suggested_change="Serialize hotspot write in revised plan.",
            requires_superleader_attention=True,
        )
        review_b = await runtime.publish_leader_peer_review(
            objective_id=objective.objective_id,
            planning_round_id="round-1",
            reviewer_leader_id="leader:infra",
            reviewer_team_id="group-a:team:infra",
            target_leader_id="leader:runtime",
            target_team_id="group-a:team:runtime",
            summary="Runtime plan needs explicit migration dependency.",
            conflict_type="dependency_mismatch",
            severity="medium",
            affected_paths=("src/agent_orchestra/storage/postgres/store.py",),
            reason="Runtime sequence should depend on migration slice.",
            suggested_change="Add depends_on edge to revised runtime slice.",
        )

        global_review = await runtime.publish_superleader_global_review(
            objective_id=objective.objective_id,
            planning_round_id="round-1",
            superleader_id="superleader:obj-planning-review",
            summary="Serialize shared hotspot and add migration dependency.",
            activation_blockers=("shared_hotspot_conflict",),
            required_serialization=("runtime-core", "infra-schema"),
            required_reordering=("runtime-core->infra-schema",),
        )

        revised_runtime = await runtime.publish_leader_revised_plan(
            objective_id=objective.objective_id,
            planning_round_id="round-1",
            leader_id="leader:runtime",
            lane_id="runtime",
            team_id="group-a:team:runtime",
            summary="Runtime revised plan serializes hotspot writes.",
            sequential_slices=(
                {
                    "slice_id": "runtime-core-revised",
                    "title": "Runtime core revised",
                    "goal": "Apply serialized hotspot write.",
                    "reason": "Satisfy superleader global review.",
                    "mode": "sequential",
                    "depends_on": ("infra-schema",),
                    "owned_paths": ["src/agent_orchestra/runtime/group_runtime.py"],
                },
            ),
            revision_bundle_ref="bundle:round-1:leader:runtime",
        )

        all_drafts = await runtime.list_leader_draft_plans(
            objective.objective_id,
            planning_round_id="round-1",
        )
        runtime_only = await runtime.list_leader_draft_plans(
            objective.objective_id,
            planning_round_id="round-1",
            leader_id="leader:runtime",
        )
        all_peer_reviews = await runtime.list_leader_peer_reviews(
            objective.objective_id,
            planning_round_id="round-1",
        )
        runtime_review_targets = await runtime.list_leader_peer_reviews(
            objective.objective_id,
            planning_round_id="round-1",
            target_leader_id="leader:runtime",
        )
        revised_plans = await runtime.list_leader_revised_plans(
            objective.objective_id,
            planning_round_id="round-1",
        )
        loaded_global = await runtime.get_superleader_global_review(
            objective.objective_id,
            planning_round_id="round-1",
        )

        self.assertEqual(len(all_drafts), 2)
        self.assertEqual({draft.leader_id for draft in all_drafts}, {"leader:runtime", "leader:infra"})
        self.assertEqual(len(runtime_only), 1)
        self.assertEqual(runtime_only[0].leader_id, "leader:runtime")
        self.assertEqual(len(all_peer_reviews), 2)
        self.assertEqual({review.reviewer_leader_id for review in all_peer_reviews}, {"leader:runtime", "leader:infra"})
        self.assertEqual(len(runtime_review_targets), 1)
        self.assertEqual(runtime_review_targets[0].target_leader_id, "leader:runtime")
        self.assertEqual(len(revised_plans), 1)
        self.assertEqual(revised_plans[0].leader_id, "leader:runtime")
        self.assertEqual(revised_plans[0].revision_bundle_ref, "bundle:round-1:leader:runtime")
        self.assertIsNotNone(loaded_global)
        assert loaded_global is not None
        self.assertEqual(loaded_global.summary, global_review.summary)
        self.assertEqual(runtime_draft.leader_id, "leader:runtime")
        self.assertEqual(infra_draft.leader_id, "leader:infra")
        self.assertEqual(review_a.target_leader_id, "leader:infra")
        self.assertEqual(review_b.target_leader_id, "leader:runtime")
        self.assertEqual(revised_runtime.leader_id, "leader:runtime")

    async def test_group_runtime_builds_summary_first_leader_revision_context_bundle(self) -> None:
        store = InMemoryOrchestrationStore()
        self._skip_unless_planning_review_available(store)
        bus = InMemoryEventBus()
        runtime = GroupRuntime(store=store, bus=bus)

        await runtime.create_group("group-a")
        objective = await runtime.create_objective(
            group_id="group-a",
            objective_id="obj-planning-review",
            title="Planning review objective",
            description="Exercise revision context bundle builder.",
        )
        await runtime.create_team(
            group_id="group-a",
            team_id="group-a:team:runtime",
            name="Runtime",
        )
        await runtime.create_team(
            group_id="group-a",
            team_id="group-a:team:infra",
            name="Infra",
        )

        await runtime.publish_leader_draft_plan(
            objective_id=objective.objective_id,
            planning_round_id="round-1",
            leader_id="leader:runtime",
            lane_id="runtime",
            team_id="group-a:team:runtime",
            summary="Runtime initial plan.",
            sequential_slices=(
                {
                    "slice_id": "runtime-core",
                    "title": "Runtime core",
                    "goal": "Implement runtime core path.",
                    "reason": "Needed before infra integration.",
                    "mode": "sequential",
                },
            ),
        )
        await runtime.publish_leader_draft_plan(
            objective_id=objective.objective_id,
            planning_round_id="round-1",
            leader_id="leader:infra",
            lane_id="infra",
            team_id="group-a:team:infra",
            summary="Infra initial plan.",
            sequential_slices=(
                {
                    "slice_id": "infra-schema",
                    "title": "Infra schema",
                    "goal": "Implement store schema path.",
                    "reason": "Needed for persistent state.",
                    "mode": "sequential",
                },
            ),
        )
        await runtime.publish_leader_peer_review(
            objective_id=objective.objective_id,
            planning_round_id="round-1",
            reviewer_leader_id="leader:runtime",
            reviewer_team_id="group-a:team:runtime",
            target_leader_id="leader:infra",
            target_team_id="group-a:team:infra",
            summary="Runtime review for infra draft.",
            conflict_type="shared_hotspot_conflict",
            severity="high",
        )
        await runtime.publish_leader_peer_review(
            objective_id=objective.objective_id,
            planning_round_id="round-1",
            reviewer_leader_id="leader:infra",
            reviewer_team_id="group-a:team:infra",
            target_leader_id="leader:runtime",
            target_team_id="group-a:team:runtime",
            summary="Infra review for runtime draft.",
            conflict_type="dependency_mismatch",
            severity="medium",
        )
        await runtime.publish_superleader_global_review(
            objective_id=objective.objective_id,
            planning_round_id="round-1",
            superleader_id="superleader:obj-planning-review",
            summary="Global review summary.",
            activation_blockers=("shared_hotspot_conflict",),
        )

        bundle = await runtime.build_leader_revision_context_bundle(
            objective_id=objective.objective_id,
            planning_round_id="round-1",
            leader_id="leader:runtime",
        )

        self.assertEqual(bundle.objective_id, objective.objective_id)
        self.assertEqual(bundle.planning_round_id, "round-1")
        self.assertEqual(bundle.leader_id, "leader:runtime")
        self.assertEqual(len(bundle.draft_plan_refs), 1)
        self.assertEqual(len(bundle.peer_review_digests), 2)
        self.assertIsNotNone(bundle.superleader_review_digest)

    async def test_group_runtime_builds_leader_revision_context_bundle_with_task_surface_and_project_item_notices(self) -> None:
        store = InMemoryOrchestrationStore()
        self._skip_unless_planning_review_available(store)
        bus = InMemoryEventBus()
        runtime = GroupRuntime(store=store, bus=bus)

        await runtime.create_group("group-a")
        objective = await runtime.create_objective(
            group_id="group-a",
            objective_id="obj-planning-review-authority",
            title="Planning review authority context",
            description="Exercise revision context bundle task-surface authority notices.",
        )
        await runtime.create_team(
            group_id="group-a",
            team_id="group-a:team:runtime",
            name="Runtime",
        )
        await runtime.create_team(
            group_id="group-a",
            team_id="group-a:team:infra",
            name="Infra",
        )

        task = await runtime.submit_task(
            group_id="group-a",
            team_id="group-a:team:runtime",
            lane_id="runtime",
            goal="Escalate runtime task-surface mutation",
            scope=TaskScope.TEAM,
            created_by="leader:runtime",
        )
        teammate_id = "group-a:team:runtime:teammate:1"
        await runtime.claim_task(
            task_id=task.task_id,
            owner_id=teammate_id,
            claim_source="test.runtime",
        )
        await store.save_delivery_state(
            DeliveryState(
                delivery_id=f"{objective.objective_id}:lane:runtime",
                objective_id=objective.objective_id,
                kind=DeliveryStateKind.LANE,
                status=DeliveryStatus.RUNNING,
                lane_id="runtime",
                team_id="group-a:team:runtime",
                pending_task_ids=(task.task_id,),
                active_task_ids=(task.task_id,),
            )
        )
        authority_commit = await runtime.commit_task_surface_mutation(
            objective_id=objective.objective_id,
            lane_id="runtime",
            team_id="group-a:team:runtime",
            task_id=task.task_id,
            actor_id=teammate_id,
            mutation_kind=TaskSurfaceMutationKind.NOT_NEEDED,
            reason="Teammate needs higher authority before mutating the leader-owned task surface.",
        )
        self.assertIsNotNone(authority_commit.authority_request)

        await runtime.create_review_item(
            objective_id=objective.objective_id,
            item_id="project-item-runtime",
            item_kind=ReviewItemKind.PROJECT_ITEM,
            source_task_id=task.task_id,
            lane_id="runtime",
            team_id="group-a:team:runtime",
            title="Shared runtime contract",
            summary="Tracks the shared runtime task-surface contract.",
        )

        await runtime.publish_leader_draft_plan(
            objective_id=objective.objective_id,
            planning_round_id="round-1",
            leader_id="leader:runtime",
            lane_id="runtime",
            team_id="group-a:team:runtime",
            summary="Runtime initial plan.",
            project_items=("project-item-runtime",),
            authority_risks=("Need authority review for shared runtime task-surface changes.",),
            sequential_slices=(
                {
                    "slice_id": "runtime-authority-surface",
                    "title": "Runtime authority surface",
                    "goal": "Align runtime lane authority surface.",
                    "reason": "Planning should see governed task-surface state.",
                    "mode": "sequential",
                },
            ),
        )
        await runtime.publish_leader_draft_plan(
            objective_id=objective.objective_id,
            planning_round_id="round-1",
            leader_id="leader:infra",
            lane_id="infra",
            team_id="group-a:team:infra",
            summary="Infra initial plan.",
            sequential_slices=(
                {
                    "slice_id": "infra-observe-runtime",
                    "title": "Infra observe runtime",
                    "goal": "Review runtime coordination dependencies.",
                    "reason": "Exercise foreign draft references.",
                    "mode": "sequential",
                },
            ),
        )
        await runtime.publish_leader_peer_review(
            objective_id=objective.objective_id,
            planning_round_id="round-1",
            reviewer_leader_id="leader:infra",
            reviewer_team_id="group-a:team:infra",
            target_leader_id="leader:runtime",
            target_team_id="group-a:team:runtime",
            summary="Runtime draft touches a governed shared task-surface contract.",
            conflict_type="task_surface_authority",
            severity="high",
            affected_project_items=("project-item-runtime",),
            requires_superleader_attention=True,
            suggested_change="Revise the plan to respect the governed task-surface boundary.",
        )
        await runtime.publish_superleader_global_review(
            objective_id=objective.objective_id,
            planning_round_id="round-1",
            superleader_id="superleader:obj-planning-review-authority",
            summary="Global review summary.",
            required_authority_attention=("project-item-runtime",),
        )

        bundle = await runtime.build_leader_revision_context_bundle(
            objective_id=objective.objective_id,
            planning_round_id="round-1",
            leader_id="leader:runtime",
        )

        self.assertTrue(
            any(task.task_id in notice for notice in bundle.authority_notices),
            bundle.authority_notices,
        )
        self.assertTrue(
            any("project-item-runtime" in notice for notice in bundle.project_item_notices),
            bundle.project_item_notices,
        )
        self.assertIn("task_surface_authority", bundle.metadata)
        self.assertEqual(
            bundle.metadata["task_surface_authority"]["task_surface_waiting_task_ids"],
            [task.task_id],
        )
        self.assertEqual(
            bundle.metadata["task_surface_authority"]["draft_authority_risks"],
            ["Need authority review for shared runtime task-surface changes."],
        )
        self.assertIn("project_item_surface", bundle.metadata)
        self.assertEqual(
            bundle.metadata["project_item_surface"]["project_item_ids"],
            ["project-item-runtime"],
        )

    async def test_group_runtime_dispatches_task_and_records_event(self) -> None:
        store = InMemoryOrchestrationStore()
        bus = InMemoryEventBus()
        runtime = GroupRuntime(store=store, bus=bus)

        await runtime.create_group("group-a")
        await runtime.create_team(group_id="group-a", team_id="team-a", name="Research")
        task = await runtime.submit_task(
            group_id="group-a",
            team_id="team-a",
            goal="Map abstractions",
            owned_paths=("src/runtime.py",),
        )

        self.assertEqual(task.team_id, "team-a")
        self.assertEqual(len(store.tasks), 1)
        self.assertEqual(bus.published_events[-1].kind, EventKind.TASK_SUBMITTED)

    async def test_group_runtime_reduces_handoffs_into_authority_state(self) -> None:
        store = InMemoryOrchestrationStore()
        bus = InMemoryEventBus()
        runtime = GroupRuntime(store=store, bus=bus)

        await runtime.create_group("group-a")
        await runtime.create_team(group_id="group-a", team_id="team-a", name="Research")
        await runtime.create_team(group_id="group-a", team_id="team-b", name="Delivery")
        task = await runtime.submit_task(group_id="group-a", team_id="team-a", goal="Write summary")
        handoff = await runtime.record_handoff(
            group_id="group-a",
            from_team_id="team-a",
            to_team_id="team-b",
            task_id=task.task_id,
            artifact_refs=("artifacts/summary.md",),
            summary="summary ready",
        )

        state = await runtime.reduce_group("group-a")

        self.assertEqual(state.status, AuthorityStatus.AUTHORITY_COMPLETE)
        self.assertIn(handoff.handoff_id, state.accepted_handoffs)
        self.assertEqual(bus.published_events[-1].kind, EventKind.AUTHORITY_UPDATED)

    async def test_group_runtime_records_handoff_contract_assertions_and_verification(self) -> None:
        store = InMemoryOrchestrationStore()
        bus = InMemoryEventBus()
        runtime = GroupRuntime(store=store, bus=bus)

        await runtime.create_group("group-a")
        await runtime.create_team(group_id="group-a", team_id="team-a", name="Research")
        await runtime.create_team(group_id="group-a", team_id="team-b", name="Authority Root")
        task = await runtime.submit_task(group_id="group-a", team_id="team-a", goal="Write summary")

        handoff = await runtime.record_handoff(
            group_id="group-a",
            from_team_id="team-a",
            to_team_id="team-b",
            task_id=task.task_id,
            artifact_refs=("artifacts/summary.md",),
            summary="summary ready",
            contract_assertions=("runtime tests",),
            verification_summary={
                "status": "passed",
                "commands": ["python3 -m unittest tests.test_runtime -v"],
            },
        )

        self.assertEqual(handoff.contract_assertions, ("runtime tests",))
        self.assertEqual(handoff.verification_summary["status"], "passed")

    async def test_group_runtime_persists_objective_and_spec_dag(self) -> None:
        store = InMemoryOrchestrationStore()
        bus = InMemoryEventBus()
        runtime = GroupRuntime(store=store, bus=bus)

        await runtime.create_group("group-a")
        objective = await runtime.create_objective(
            group_id="group-a",
            objective_id="obj-a",
            title="Deliver runtime",
            description="Build the core coordination runtime.",
            success_metrics=("all tests green", "scope model implemented"),
            hard_constraints=("do not break existing CLI",),
            budget={"max_teams": 2, "max_iterations": 3},
        )
        leader_node = await runtime.add_spec_node(
            objective_id=objective.objective_id,
            node_id="node-leader-a",
            kind=SpecNodeKind.LEADER_TASK,
            title="Run team A",
            summary="Own the runtime core.",
            scope=TaskScope.LEADER_LANE,
            lane_id="lane-a",
            created_by="superleader-1",
        )
        teammate_node = await runtime.add_spec_node(
            objective_id=objective.objective_id,
            node_id="node-team-a-1",
            kind=SpecNodeKind.TEAMMATE_TASK,
            title="Implement reducer logic",
            summary="Add task visibility and blackboard reduction.",
            scope=TaskScope.TEAM,
            lane_id="lane-a",
            team_id="team-a",
            created_by="leader-a",
        )
        edge = await runtime.add_spec_edge(
            objective_id=objective.objective_id,
            edge_id="edge-a",
            kind=SpecEdgeKind.DECOMPOSES_TO,
            from_node_id=leader_node.node_id,
            to_node_id=teammate_node.node_id,
        )

        self.assertEqual(objective.objective_id, "obj-a")
        self.assertEqual(leader_node.scope, TaskScope.LEADER_LANE)
        self.assertEqual(teammate_node.team_id, "team-a")
        self.assertEqual(edge.kind, SpecEdgeKind.DECOMPOSES_TO)
        self.assertEqual(len(await store.list_spec_nodes("obj-a")), 2)
        self.assertEqual(len(await store.list_spec_edges("obj-a")), 1)

    async def test_scoped_task_visibility_and_auto_claim_follow_framework_rules(self) -> None:
        store = InMemoryOrchestrationStore()
        bus = InMemoryEventBus()
        runtime = GroupRuntime(store=store, bus=bus)

        await runtime.create_group("group-a")
        await runtime.create_team(group_id="group-a", team_id="team-a", name="Runtime")
        await runtime.create_team(group_id="group-a", team_id="team-b", name="QA")

        lane_task = await runtime.submit_task(
            group_id="group-a",
            team_id="team-a",
            goal="Coordinate lane work",
            scope=TaskScope.LEADER_LANE,
            lane_id="lane-a",
            created_by="superleader-1",
        )
        team_task = await runtime.submit_task(
            group_id="group-a",
            team_id="team-a",
            goal="Implement runtime core",
            scope=TaskScope.TEAM,
            lane_id="lane-a",
            created_by="leader-a",
        )
        await runtime.claim_task(
            task_id=team_task.task_id,
            owner_id="teammate-a1",
            claim_source="test.runtime",
        )
        extra_task = await runtime.submit_task(
            group_id="group-a",
            team_id="team-a",
            goal="Add regression coverage",
            scope=TaskScope.TEAM,
            lane_id="lane-a",
            created_by="teammate-a1",
            derived_from=team_task.task_id,
            reason="Found additional validation work while implementing runtime core.",
        )
        team_b_task = await runtime.submit_task(
            group_id="group-a",
            team_id="team-b",
            goal="Verify another team",
            scope=TaskScope.TEAM,
            lane_id="lane-b",
            created_by="leader-b",
        )

        claimed = await runtime.update_task_status(
            task_id=extra_task.task_id,
            status=TaskStatus.IN_PROGRESS,
            actor_id="teammate-a1",
        )

        superleader_tasks = await runtime.list_visible_tasks(group_id="group-a", viewer_role="superleader")
        leader_tasks = await runtime.list_visible_tasks(
            group_id="group-a",
            viewer_role="leader",
            lane_id="lane-a",
            team_id="team-a",
        )
        teammate_tasks = await runtime.list_visible_tasks(
            group_id="group-a",
            viewer_role="teammate",
            team_id="team-a",
        )

        self.assertIsNone(lane_task.owner_id)
        self.assertEqual(claimed.owner_id, "teammate-a1")
        self.assertEqual(claimed.status, TaskStatus.IN_PROGRESS)
        self.assertEqual(claimed.claim_source, "runtime.update_task_status")
        self.assertIsNotNone(claimed.claim_session_id)
        self.assertIsNotNone(claimed.claimed_at)
        self.assertEqual(extra_task.derived_from, team_task.task_id)
        self.assertEqual(
            {task.task_id for task in superleader_tasks},
            {lane_task.task_id, team_task.task_id, extra_task.task_id, team_b_task.task_id},
        )
        self.assertEqual({task.task_id for task in leader_tasks}, {lane_task.task_id, team_task.task_id, extra_task.task_id})
        self.assertEqual({task.task_id for task in teammate_tasks}, {team_task.task_id, extra_task.task_id})
        self.assertNotIn(lane_task.task_id, {task.task_id for task in teammate_tasks})

    async def test_submit_task_persists_task_surface_provenance_and_read_only_fields(self) -> None:
        store = InMemoryOrchestrationStore()
        bus = InMemoryEventBus()
        runtime = GroupRuntime(store=store, bus=bus)

        await runtime.create_group("group-a")
        await runtime.create_team(group_id="group-a", team_id="team-a", name="Runtime")

        parent = await runtime.submit_task(
            group_id="group-a",
            team_id="team-a",
            goal="Implement runtime mutation contract",
            scope=TaskScope.TEAM,
            lane_id="lane-a",
            owned_paths=("src/agent_orchestra/runtime/group_runtime.py",),
            created_by="leader-a",
        )
        await runtime.claim_task(
            task_id=parent.task_id,
            owner_id="teammate-a1",
            claim_source="test.runtime",
        )
        child = await runtime.submit_task(
            group_id="group-a",
            team_id="team-a",
            goal="Add runtime mutation regression coverage",
            scope=TaskScope.TEAM,
            lane_id="lane-a",
            owned_paths=("tests/test_runtime.py",),
            created_by="teammate-a1",
            derived_from=parent.task_id,
            reason="Found follow-up work while implementing the runtime mutation contract.",
        )

        saved_parent = await store.get_task(parent.task_id)
        saved_child = await store.get_task(child.task_id)

        self.assertIsNotNone(saved_parent)
        self.assertIsNotNone(saved_child)
        assert saved_parent is not None
        assert saved_child is not None
        self.assertEqual(saved_parent.provenance.kind, TaskProvenanceKind.ROOT)
        self.assertEqual(saved_child.provenance.kind, TaskProvenanceKind.CHILD)
        self.assertEqual(saved_child.provenance.source_task_id, parent.task_id)
        self.assertEqual(
            saved_child.provenance.reason,
            "Found follow-up work while implementing the runtime mutation contract.",
        )
        self.assertIn("goal", saved_child.protected_read_only_fields)
        self.assertIn("owned_paths", saved_child.protected_read_only_fields)
        self.assertIn("verification_commands", saved_child.protected_read_only_fields)

    async def test_submit_task_requires_teammate_child_lineage_and_reason(self) -> None:
        store = InMemoryOrchestrationStore()
        bus = InMemoryEventBus()
        runtime = GroupRuntime(store=store, bus=bus)

        await runtime.create_group("group-a")
        await runtime.create_team(group_id="group-a", team_id="team-a", name="Runtime")
        parent = await runtime.submit_task(
            group_id="group-a",
            team_id="team-a",
            goal="Implement runtime coordination surface",
            scope=TaskScope.TEAM,
            lane_id="lane-a",
            created_by="leader-a",
        )
        await runtime.claim_task(
            task_id=parent.task_id,
            owner_id="group-a:team:team-a:teammate:1",
            claim_source="test.runtime",
        )

        with self.assertRaises(PermissionError):
            await runtime.submit_task(
                group_id="group-a",
                team_id="team-a",
                goal="Teammate root task write",
                scope=TaskScope.TEAM,
                lane_id="lane-a",
                created_by="group-a:team:team-a:teammate:1",
            )

        with self.assertRaises(ValueError):
            await runtime.submit_task(
                group_id="group-a",
                team_id="team-a",
                goal="Teammate child task without reason",
                scope=TaskScope.TEAM,
                lane_id="lane-a",
                created_by="group-a:team:team-a:teammate:1",
                derived_from=parent.task_id,
            )

        child = await runtime.submit_task(
            group_id="group-a",
            team_id="team-a",
            goal="Add task-surface authority regression coverage",
            scope=TaskScope.TEAM,
            lane_id="lane-a",
            created_by="group-a:team:team-a:teammate:1",
            derived_from=parent.task_id,
            reason="Need a follow-up slice for teammate-owned task-surface validation.",
        )

        self.assertEqual(child.provenance.kind, TaskProvenanceKind.CHILD)
        self.assertEqual(child.provenance.source_task_id, parent.task_id)

    async def test_task_card_surface_authority_view_exposes_local_surface_truth(self) -> None:
        task = TaskCard(
            task_id="task-child",
            goal="Review runtime surface authority projection.",
            lane="lane-a",
            group_id="group-a",
            team_id="team-a",
            scope=TaskScope.TEAM,
            owner_id="group-a:team:team-a:teammate:2",
            created_by="group-a:team:team-a:teammate:1",
            derived_from="task-parent",
            reason="Need a child slice to review the runtime surface contract.",
        )

        authority_view = task.surface_authority_view()

        self.assertEqual(authority_view.task_id, "task-child")
        self.assertEqual(authority_view.parent_task_id, "task-parent")
        self.assertEqual(
            authority_view.local_status_actor_ids,
            (
                "group-a:team:team-a:teammate:2",
                "group-a:team:team-a:teammate:1",
            ),
        )
        self.assertEqual(
            authority_view.local_structure_actor_ids,
            ("group-a:team:team-a:teammate:1",),
        )
        self.assertIn("goal", authority_view.protected_read_only_fields)
        self.assertIn("owned_paths", authority_view.protected_read_only_fields)

    async def test_group_runtime_task_surface_authority_view_classifies_local_and_escalated_mutations(self) -> None:
        store = InMemoryOrchestrationStore()
        bus = InMemoryEventBus()
        runtime = GroupRuntime(store=store, bus=bus)

        await runtime.create_group("group-a")
        await runtime.create_team(group_id="group-a", team_id="team-a", name="Runtime")
        await runtime.create_team(group_id="group-a", team_id="team-b", name="QA")

        teammate_id = "group-a:team:team-a:teammate:1"
        parent = await runtime.submit_task(
            group_id="group-a",
            team_id="team-a",
            goal="Implement runtime surface contract",
            scope=TaskScope.TEAM,
            lane_id="lane-a",
            created_by="leader-a",
        )
        await runtime.claim_task(
            task_id=parent.task_id,
            owner_id=teammate_id,
            claim_source="test.runtime",
        )
        child = await runtime.submit_task(
            group_id="group-a",
            team_id="team-a",
            goal="Add subtree authority coverage",
            scope=TaskScope.TEAM,
            lane_id="lane-a",
            created_by=teammate_id,
            derived_from=parent.task_id,
            reason="Need a teammate-owned child subtree for authority classification coverage.",
        )
        grandchild = await runtime.submit_task(
            group_id="group-a",
            team_id="team-a",
            goal="Add grandchild authority coverage",
            scope=TaskScope.TEAM,
            lane_id="lane-a",
            created_by=teammate_id,
            derived_from=child.task_id,
            reason="Need one deeper descendant to verify subtree lineage projection.",
        )
        foreign_task = await runtime.submit_task(
            group_id="group-a",
            team_id="team-b",
            goal="Cross-team task should require escalation",
            scope=TaskScope.TEAM,
            lane_id="lane-b",
            created_by="leader-b",
        )

        authority_view = await runtime.read_task_surface_authority_view(
            task_id=grandchild.task_id,
            actor_id=teammate_id,
        )
        submission_decision = await runtime.classify_task_submission_authority(
            TaskCard(
                task_id="candidate-child",
                goal="Spawn another teammate-owned descendant",
                lane="lane-a",
                group_id="group-a",
                team_id="team-a",
                scope=TaskScope.TEAM,
                created_by=teammate_id,
                derived_from=child.task_id,
                reason="Need one more descendant for local subtree work.",
            )
        )
        status_decision = await runtime.classify_task_status_update_authority(
            task_id=grandchild.task_id,
            actor_id=teammate_id,
            status=TaskStatus.IN_PROGRESS,
        )
        cancel_decision = await runtime.classify_task_status_update_authority(
            task_id=grandchild.task_id,
            actor_id=teammate_id,
            status=TaskStatus.CANCELLED,
        )
        local_mutation = await runtime.classify_task_surface_mutation_authority(
            task_id=grandchild.task_id,
            actor_id=teammate_id,
            reason="This descendant can be closed locally.",
        )
        parent_mutation = await runtime.classify_task_surface_mutation_authority(
            task_id=parent.task_id,
            actor_id=teammate_id,
            reason="This parent mutation should require higher authority.",
        )
        cross_team_mutation = await runtime.classify_task_surface_mutation_authority(
            task_id=grandchild.task_id,
            actor_id=teammate_id,
            reason="This mutation targets another team.",
            target_task_id=foreign_task.task_id,
        )
        protected_field_mutation = await runtime.classify_task_protected_field_write(
            task_id=grandchild.task_id,
            actor_id=teammate_id,
            field_names=("goal", "reason"),
        )

        self.assertEqual(authority_view.parent_task_id, child.task_id)
        self.assertEqual(authority_view.ancestor_task_ids, (parent.task_id, child.task_id))
        self.assertEqual(authority_view.subtree_root_task_id, child.task_id)
        self.assertEqual(
            authority_view.subtree_lineage_task_ids,
            (child.task_id, grandchild.task_id),
        )
        self.assertEqual(submission_decision.verdict, TaskSurfaceAuthorityVerdict.LOCAL_ALLOW)
        self.assertEqual(status_decision.verdict, TaskSurfaceAuthorityVerdict.LOCAL_ALLOW)
        self.assertEqual(cancel_decision.verdict, TaskSurfaceAuthorityVerdict.FORBIDDEN)
        self.assertEqual(local_mutation.verdict, TaskSurfaceAuthorityVerdict.LOCAL_ALLOW)
        self.assertEqual(parent_mutation.verdict, TaskSurfaceAuthorityVerdict.ESCALATION_NEEDED)
        self.assertEqual(cross_team_mutation.verdict, TaskSurfaceAuthorityVerdict.ESCALATION_NEEDED)
        self.assertEqual(
            protected_field_mutation.verdict,
            TaskSurfaceAuthorityVerdict.ESCALATION_NEEDED,
        )
        self.assertEqual(protected_field_mutation.protected_field_names, ("goal",))

    async def test_group_runtime_claim_task_sets_metadata_and_prevents_double_claim(self) -> None:
        store = InMemoryOrchestrationStore()
        bus = InMemoryEventBus()
        runtime = GroupRuntime(store=store, bus=bus)

        await runtime.create_group("group-a")
        await runtime.create_team(group_id="group-a", team_id="team-a", name="Runtime")
        task = await runtime.submit_task(
            group_id="group-a",
            team_id="team-a",
            goal="Claim this task once",
            scope=TaskScope.TEAM,
            lane_id="lane-a",
            created_by="leader-a",
        )

        claimed = await runtime.claim_task(
            task_id=task.task_id,
            owner_id="teammate-a1",
            claim_session_id="session-a1",
            claim_source="leader_loop.refill",
            claimed_at="2026-04-04T08:00:00+00:00",
        )
        self.assertIsNotNone(claimed)
        assert claimed is not None
        self.assertEqual(claimed.status, TaskStatus.IN_PROGRESS)
        self.assertEqual(claimed.owner_id, "teammate-a1")
        self.assertEqual(claimed.claim_session_id, "session-a1")
        self.assertEqual(claimed.claim_source, "leader_loop.refill")
        self.assertEqual(claimed.claimed_at, "2026-04-04T08:00:00+00:00")

        second_attempt = await runtime.claim_task(
            task_id=task.task_id,
            owner_id="teammate-a2",
            claim_session_id="session-a2",
            claim_source="leader_loop.refill",
            claimed_at="2026-04-04T08:01:00+00:00",
        )
        self.assertIsNone(second_attempt)
        stored = await store.get_task(task.task_id)
        self.assertIsNotNone(stored)
        assert stored is not None
        self.assertEqual(stored.owner_id, "teammate-a1")
        self.assertEqual(stored.claim_session_id, "session-a1")

    async def test_update_task_status_rejects_teammate_upper_scope_writes_and_direct_cancellation(self) -> None:
        store = InMemoryOrchestrationStore()
        bus = InMemoryEventBus()
        runtime = GroupRuntime(store=store, bus=bus)

        await runtime.create_group("group-a")
        await runtime.create_team(group_id="group-a", team_id="team-a", name="Runtime")
        lane_task = await runtime.submit_task(
            group_id="group-a",
            team_id="team-a",
            goal="Coordinate the runtime lane",
            scope=TaskScope.LEADER_LANE,
            lane_id="lane-a",
            created_by="leader-a",
        )
        team_task = await runtime.submit_task(
            group_id="group-a",
            team_id="team-a",
            goal="Implement runtime authority gate",
            scope=TaskScope.TEAM,
            lane_id="lane-a",
            created_by="leader-a",
        )

        with self.assertRaises(PermissionError):
            await runtime.update_task_status(
                task_id=lane_task.task_id,
                status=TaskStatus.COMPLETED,
                actor_id="group-a:team:team-a:teammate:1",
            )

        with self.assertRaises(ValueError):
            await runtime.update_task_status(
                task_id=team_task.task_id,
                status=TaskStatus.CANCELLED,
                actor_id="leader-a",
            )

    async def test_group_runtime_claim_next_task_claims_pending_unowned_unblocked_only(self) -> None:
        store = InMemoryOrchestrationStore()
        bus = InMemoryEventBus()
        runtime = GroupRuntime(store=store, bus=bus)

        await runtime.create_group("group-a")
        await runtime.create_team(group_id="group-a", team_id="team-a", name="Runtime")
        task_a = await runtime.submit_task(
            group_id="group-a",
            team_id="team-a",
            goal="Task A",
            scope=TaskScope.TEAM,
            lane_id="lane-a",
            created_by="leader-a",
        )
        task_b = await runtime.submit_task(
            group_id="group-a",
            team_id="team-a",
            goal="Task B",
            scope=TaskScope.TEAM,
            lane_id="lane-a",
            created_by="leader-a",
        )
        blocked = await runtime.submit_task(
            group_id="group-a",
            team_id="team-a",
            goal="Task C blocked",
            scope=TaskScope.TEAM,
            lane_id="lane-a",
            created_by="leader-a",
        )
        await runtime.update_task_status(
            task_id=blocked.task_id,
            status=TaskStatus.BLOCKED,
            blocked_by=("waiting-for-approval",),
        )

        first = await runtime.claim_next_task(
            group_id="group-a",
            owner_id="teammate-a1",
            team_id="team-a",
            lane_id="lane-a",
            scope=TaskScope.TEAM,
            claim_session_id="session-next-1",
            claim_source="leader_loop.refill",
            claimed_at="2026-04-04T09:00:00+00:00",
        )
        second = await runtime.claim_next_task(
            group_id="group-a",
            owner_id="teammate-a2",
            team_id="team-a",
            lane_id="lane-a",
            scope=TaskScope.TEAM,
            claim_session_id="session-next-2",
            claim_source="leader_loop.refill",
            claimed_at="2026-04-04T09:01:00+00:00",
        )
        third = await runtime.claim_next_task(
            group_id="group-a",
            owner_id="teammate-a3",
            team_id="team-a",
            lane_id="lane-a",
            scope=TaskScope.TEAM,
            claim_session_id="session-next-3",
            claim_source="leader_loop.refill",
            claimed_at="2026-04-04T09:02:00+00:00",
        )

        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertIsNone(third)
        assert first is not None and second is not None
        self.assertEqual({first.task_id, second.task_id}, {task_a.task_id, task_b.task_id})
        self.assertEqual(first.status, TaskStatus.IN_PROGRESS)
        self.assertEqual(second.status, TaskStatus.IN_PROGRESS)
        self.assertEqual(first.claim_source, "leader_loop.refill")
        self.assertEqual(second.claim_source, "leader_loop.refill")

        blocked_after = await store.get_task(blocked.task_id)
        self.assertIsNotNone(blocked_after)
        assert blocked_after is not None
        self.assertEqual(blocked_after.status, TaskStatus.BLOCKED)
        self.assertIsNone(blocked_after.owner_id)

    async def test_group_runtime_claim_next_task_waits_for_activation_dependencies(self) -> None:
        store = InMemoryOrchestrationStore()
        bus = InMemoryEventBus()
        runtime = GroupRuntime(store=store, bus=bus)

        await runtime.create_group("group-a")
        await runtime.create_team(group_id="group-a", team_id="team-a", name="Runtime")
        root_task = await runtime.submit_task(
            group_id="group-a",
            team_id="team-a",
            goal="Implement root runtime contract",
            scope=TaskScope.TEAM,
            lane_id="lane-a",
            created_by="leader-a",
            slice_id="slice-root",
            slice_mode="sequential",
        )
        dependent_task = await runtime.submit_task(
            group_id="group-a",
            team_id="team-a",
            goal="Run dependency-gated follow-up",
            scope=TaskScope.TEAM,
            lane_id="lane-a",
            created_by="leader-a",
            slice_id="slice-followup",
            slice_mode="sequential",
            depends_on_slice_ids=("slice-root",),
            depends_on_task_ids=(root_task.task_id,),
        )

        first = await runtime.claim_next_task(
            group_id="group-a",
            owner_id="teammate-a1",
            team_id="team-a",
            lane_id="lane-a",
            scope=TaskScope.TEAM,
            claim_session_id="session-root",
            claim_source="resident_task_list_claim",
            claimed_at="2026-04-06T09:00:00+00:00",
        )
        second = await runtime.claim_next_task(
            group_id="group-a",
            owner_id="teammate-a2",
            team_id="team-a",
            lane_id="lane-a",
            scope=TaskScope.TEAM,
            claim_session_id="session-followup-1",
            claim_source="resident_task_list_claim",
            claimed_at="2026-04-06T09:01:00+00:00",
        )

        self.assertIsNotNone(first)
        assert first is not None
        self.assertEqual(first.task_id, root_task.task_id)
        self.assertIsNone(second)

        await runtime.update_task_status(
            task_id=root_task.task_id,
            status=TaskStatus.COMPLETED,
            actor_id="teammate-a1",
        )

        released = await runtime.claim_next_task(
            group_id="group-a",
            owner_id="teammate-a2",
            team_id="team-a",
            lane_id="lane-a",
            scope=TaskScope.TEAM,
            claim_session_id="session-followup-2",
            claim_source="resident_task_list_claim",
            claimed_at="2026-04-06T09:02:00+00:00",
        )

        self.assertIsNotNone(released)
        assert released is not None
        self.assertEqual(released.task_id, dependent_task.task_id)
        self.assertEqual(released.depends_on_task_ids, (root_task.task_id,))

    async def test_group_runtime_authorizes_message_subscription_and_full_text_access_by_scope(self) -> None:
        store = InMemoryOrchestrationStore()
        bus = InMemoryEventBus()
        runtime = GroupRuntime(store=store, bus=bus)

        await runtime.create_group("group-a")
        await runtime.create_team(group_id="group-a", team_id="team-a", name="Runtime")
        await runtime.create_team(group_id="group-a", team_id="team-b", name="Infra")

        leader_subscription = runtime.authorize_message_subscription(
            group_id="group-a",
            viewer_role="leader",
            viewer_lane_id="lane-a",
            viewer_team_id="team-a",
            subscription=MailboxSubscription(
                subscriber="leader:lane-a",
                team_id="team-a",
                lane_id="lane-a",
                visibility_scopes=(MailboxVisibilityScope.SHARED,),
                delivery_mode=MailboxDeliveryMode.SUMMARY_PLUS_REF,
            ),
        )
        superleader_subscription = runtime.authorize_message_subscription(
            group_id="group-a",
            viewer_role="superleader",
            subscription=MailboxSubscription(
                subscriber="superleader:obj-runtime",
                group_id="group-a",
                visibility_scopes=(MailboxVisibilityScope.SHARED,),
                delivery_mode=MailboxDeliveryMode.SUMMARY_PLUS_REF,
            ),
        )

        with self.assertRaises(PermissionError):
            runtime.authorize_message_subscription(
                group_id="group-a",
                viewer_role="teammate",
                viewer_team_id="team-a",
                subscription=MailboxSubscription(
                    subscriber="team-a:teammate:1",
                    team_id="team-b",
                    visibility_scopes=(MailboxVisibilityScope.SHARED,),
                    delivery_mode=MailboxDeliveryMode.SUMMARY_PLUS_REF,
                ),
            )

        foreign_digest = MailboxDigest(
            subscription_id="sub-foreign",
            subscriber="team-a:teammate:1",
            envelope_id="env-1",
            sender="group-a:team:team-b:leader",
            recipient="group-a:team:team-b:leader",
            subject="task.result",
            summary="Foreign team summary",
            delivery_mode=MailboxDeliveryMode.SUMMARY_PLUS_REF,
            group_id="group-a",
            lane_id="lane-b",
            team_id="team-b",
            visibility_scope=MailboxVisibilityScope.SHARED,
            full_text_ref="blackboard:entry-1",
        )

        self.assertEqual(leader_subscription.team_id, "team-a")
        self.assertEqual(superleader_subscription.group_id, "group-a")
        self.assertFalse(
            runtime.can_view_message_full_text(
                viewer_role="teammate",
                viewer_team_id="team-a",
                digest=foreign_digest,
            )
        )
        self.assertTrue(
            runtime.can_view_message_full_text(
                viewer_role="superleader",
                digest=foreign_digest,
            )
        )

    async def test_group_runtime_allows_only_hierarchical_cross_scope_directives(self) -> None:
        store = InMemoryOrchestrationStore()
        bus = InMemoryEventBus()
        runtime = GroupRuntime(store=store, bus=bus)

        self.assertTrue(
            runtime.can_route_cross_scope_directive(
                sender_role="superleader",
                recipient_role="leader",
            )
        )
        self.assertFalse(
            runtime.can_route_cross_scope_directive(
                sender_role="superleader",
                recipient_role="teammate",
            )
        )
        self.assertFalse(
            runtime.can_route_cross_scope_directive(
                sender_role="leader",
                recipient_role="teammate",
                sender_team_id="team-a",
                recipient_team_id="team-b",
            )
        )

    async def test_team_blackboard_entries_reduce_into_snapshot(self) -> None:
        store = InMemoryOrchestrationStore()
        bus = InMemoryEventBus()
        runtime = GroupRuntime(store=store, bus=bus)

        await runtime.create_group("group-a")
        await runtime.create_team(group_id="group-a", team_id="team-a", name="Runtime")

        directive = await runtime.append_blackboard_entry(
            group_id="group-a",
            kind=BlackboardKind.TEAM,
            entry_kind=BlackboardEntryKind.DIRECTIVE,
            author_id="leader-a",
            team_id="team-a",
            lane_id="lane-a",
            summary="Prioritize runtime tests first.",
        )
        proposal = await runtime.append_blackboard_entry(
            group_id="group-a",
            kind=BlackboardKind.TEAM,
            entry_kind=BlackboardEntryKind.PROPOSAL,
            author_id="teammate-a1",
            team_id="team-a",
            lane_id="lane-a",
            summary="Split reducer logic into its own helper.",
        )
        blocker = await runtime.append_blackboard_entry(
            group_id="group-a",
            kind=BlackboardKind.TEAM,
            entry_kind=BlackboardEntryKind.BLOCKER,
            author_id="teammate-a2",
            team_id="team-a",
            lane_id="lane-a",
            summary="Need task visibility contract before continuing.",
        )

        snapshot = await runtime.reduce_blackboard(
            group_id="group-a",
            kind=BlackboardKind.TEAM,
            team_id="team-a",
            lane_id="lane-a",
        )

        self.assertEqual(snapshot.kind, BlackboardKind.TEAM)
        self.assertEqual(snapshot.team_id, "team-a")
        self.assertIn(proposal.entry_id, snapshot.open_proposals)
        self.assertIn(blocker.entry_id, snapshot.open_blockers)
        self.assertEqual(snapshot.latest_entry_ids[-1], blocker.entry_id)
        self.assertIn(directive.entry_id, {entry.entry_id for entry in await store.list_blackboard_entries(snapshot.blackboard_id)})

    async def test_group_runtime_can_plan_from_template_and_persist_result(self) -> None:
        store = InMemoryOrchestrationStore()
        bus = InMemoryEventBus()
        runtime = GroupRuntime(store=store, bus=bus)
        planner = TemplatePlanner()

        await runtime.create_group("group-a")
        template = ObjectiveTemplate(
            objective_id="obj-a",
            group_id="group-a",
            title="Build runtime",
            description="Compile and persist a planning result.",
            workstreams=(
                WorkstreamTemplate(
                    workstream_id="runtime-core",
                    title="Runtime Core",
                    summary="Build runtime core.",
                    team_name="Runtime",
                ),
                WorkstreamTemplate(
                    workstream_id="docs",
                    title="Docs",
                    summary="Document runtime core.",
                    team_name="Docs",
                    depends_on=("runtime-core",),
                ),
            ),
        )

        result = await runtime.plan_from_template(planner, template)

        self.assertEqual(result.objective.objective_id, "obj-a")
        self.assertEqual(len(await store.list_spec_nodes("obj-a")), 3)
        self.assertEqual(len(await store.list_spec_edges("obj-a")), 3)

    async def test_group_runtime_runs_in_process_worker_assignment_and_persists_record(self) -> None:
        store = InMemoryOrchestrationStore()
        bus = InMemoryEventBus()
        runner = _FakeRunner()
        supervisor = DefaultWorkerSupervisor(
            store=store,
            launch_backends={"in_process": InProcessLaunchBackend()},
            runner=runner,
        )
        runtime = GroupRuntime(
            store=store,
            bus=bus,
            launch_backends={"in_process": InProcessLaunchBackend()},
            supervisor=supervisor,
        )

        record = await runtime.run_worker_assignment(
            WorkerAssignment(
                assignment_id="assign-1",
                worker_id="worker-1",
                group_id="group-a",
                team_id="team-a",
                task_id="task-1",
                role="leader",
                backend="in_process",
                instructions="You are the runtime worker.",
                input_text="execute",
                previous_response_id="resp-prev",
            )
        )

        stored = await store.get_worker_record("worker-1")

        self.assertEqual(record.status, WorkerStatus.COMPLETED)
        self.assertEqual(record.output_text, "handled:execute")
        self.assertEqual(record.response_id, "resp-worker-1")
        self.assertEqual(record.usage["total_tokens"], 9)
        self.assertIsNotNone(stored)
        self.assertEqual(stored.status, WorkerStatus.COMPLETED)
        self.assertEqual(stored.response_id, "resp-worker-1")
        self.assertEqual(stored.usage["total_tokens"], 9)
        self.assertEqual(runner.requests[0].previous_response_id, "resp-prev")

    async def test_authority_policy_contract_classifies_default_boundary_classes(self) -> None:
        policy = AuthorityPolicy.default()

        self.assertEqual(
            policy.classify_boundary(("docs/runtime.md",)),
            AuthorityBoundaryClass.SOFT_SCOPE,
        )
        self.assertEqual(
            policy.classify_boundary(("src/agent_orchestra/self_hosting/bootstrap.py",)),
            AuthorityBoundaryClass.PROTECTED_RUNTIME,
        )
        self.assertEqual(
            policy.classify_boundary(("resource/knowledge/README.md",)),
            AuthorityBoundaryClass.CROSS_TEAM_SHARED,
        )
        self.assertEqual(
            policy.classify_boundary(("src/agent_orchestra/contracts/task.py",)),
            AuthorityBoundaryClass.GLOBAL_CONTRACT,
        )

    async def test_commit_authority_request_uses_authority_policy_contract_boundary(self) -> None:
        store = InMemoryOrchestrationStore()
        bus = InMemoryEventBus()
        runtime = GroupRuntime(
            store=store,
            bus=bus,
            authority_policy=AuthorityPolicy(
                global_contract_prefixes=("contracts/",),
                protected_runtime_prefixes=("protected/",),
                cross_team_shared_prefixes=("shared/",),
            ),
        )

        await runtime.create_group("group-a")
        await runtime.create_team(group_id="group-a", team_id="team-a", name="Runtime")
        task = await runtime.submit_task(
            group_id="group-a",
            team_id="team-a",
            goal="Request authority across team shared policy surface",
            scope=TaskScope.TEAM,
            lane_id="lane-a",
            created_by="leader-a",
        )
        await store.save_delivery_state(
            DeliveryState(
                delivery_id="obj-a:lane:lane-a",
                objective_id="obj-a",
                kind=DeliveryStateKind.LANE,
                status=DeliveryStatus.RUNNING,
                lane_id="lane-a",
                team_id="team-a",
            )
        )

        request = ScopeExtensionRequest(
            request_id="auth-req-policy-boundary",
            assignment_id=f"{task.task_id}:assignment",
            worker_id="team-a:teammate:1",
            task_id=task.task_id,
            requested_paths=("shared/runtime-index.md",),
            reason="Need shared docs authority.",
            evidence="shared index is out of lane-local ownership",
            retry_hint="policy surface should classify boundary",
        )
        commit = await runtime.commit_authority_request(
            objective_id="obj-a",
            lane_id="lane-a",
            team_id="team-a",
            task_id=task.task_id,
            worker_id="team-a:teammate:1",
            authority_request=request,
            record=WorkerRecord(
                worker_id="team-a:teammate:1",
                assignment_id=request.assignment_id,
                backend="in_process",
                role="teammate",
                status=WorkerStatus.FAILED,
                metadata={
                    "final_report": {
                        "assignment_id": request.assignment_id,
                        "worker_id": request.worker_id,
                        "authority_request": request.to_dict(),
                        "terminal_status": "blocked",
                    }
                },
            ),
        )

        pending_requests = await runtime.list_pending_authority_requests(
            group_id="group-a",
            lane_id="lane-a",
        )

        self.assertEqual(
            commit.task.authority_boundary_class,
            AuthorityBoundaryClass.CROSS_TEAM_SHARED.value,
        )
        self.assertEqual(len(pending_requests), 1)
        self.assertEqual(
            pending_requests[0].boundary_class,
            AuthorityBoundaryClass.CROSS_TEAM_SHARED.value,
        )

    async def test_commit_authority_decision_grant_reopens_task_with_expanded_scope(self) -> None:
        store = InMemoryOrchestrationStore()
        bus = InMemoryEventBus()
        runtime = GroupRuntime(store=store, bus=bus)

        await runtime.create_group("group-a")
        await runtime.create_team(group_id="group-a", team_id="team-a", name="Runtime")
        task = await runtime.submit_task(
            group_id="group-a",
            team_id="team-a",
            goal="Repair runtime blocker",
            scope=TaskScope.TEAM,
            lane_id="lane-a",
            owned_paths=("src/agent_orchestra/runtime/leader_loop.py",),
            created_by="leader-a",
        )
        await store.save_delivery_state(
            DeliveryState(
                delivery_id="obj-a:lane:lane-a",
                objective_id="obj-a",
                kind=DeliveryStateKind.LANE,
                status=DeliveryStatus.RUNNING,
                lane_id="lane-a",
                team_id="team-a",
            )
        )

        request = ScopeExtensionRequest(
            request_id="auth-req-1",
            assignment_id=f"{task.task_id}:assignment",
            worker_id="team-a:teammate:1",
            task_id=task.task_id,
            requested_paths=("src/agent_orchestra/self_hosting/bootstrap.py",),
            reason="Need bootstrap authority.",
            evidence="verification import failed",
            retry_hint="grant bootstrap path",
        )
        await runtime.commit_authority_request(
            objective_id="obj-a",
            lane_id="lane-a",
            team_id="team-a",
            task_id=task.task_id,
            worker_id="team-a:teammate:1",
            authority_request=request,
            record=WorkerRecord(
                worker_id="team-a:teammate:1",
                assignment_id=request.assignment_id,
                backend="in_process",
                role="teammate",
                status=WorkerStatus.FAILED,
                metadata={
                    "final_report": {
                        "assignment_id": request.assignment_id,
                        "worker_id": request.worker_id,
                        "authority_request": request.to_dict(),
                        "terminal_status": "blocked",
                    }
                },
            ),
        )

        decision = AuthorityDecision(
            request_id=request.request_id,
            decision="grant",
            actor_id="leader-a",
            scope_class="soft_scope",
            granted_paths=("src/agent_orchestra/self_hosting/bootstrap.py",),
            reason="repo-local authority expansion",
            resume_mode="direct_reactivation",
            summary="Granted bootstrap.py to continue the blocked task.",
        )
        decision_commit = await runtime.commit_authority_decision(
            objective_id="obj-a",
            lane_id="lane-a",
            team_id="team-a",
            task_id=task.task_id,
            actor_id="leader-a",
            authority_decision=decision,
        )

        saved_task = await store.get_task(task.task_id)
        saved_state = await store.get_delivery_state("obj-a:lane:lane-a")

        self.assertIsNotNone(saved_task)
        assert saved_task is not None
        self.assertEqual(saved_task.status, TaskStatus.PENDING)
        self.assertIsNone(saved_task.authority_request_id)
        self.assertEqual(
            saved_task.owned_paths,
            (
                "src/agent_orchestra/runtime/leader_loop.py",
                "src/agent_orchestra/self_hosting/bootstrap.py",
            ),
        )
        self.assertEqual(saved_task.authority_resume_target, "team-a:teammate:1")
        self.assertEqual(saved_task.authority_decision_payload["decision"], "grant")
        self.assertIsNotNone(saved_state)
        assert saved_state is not None
        self.assertEqual(saved_state.status, DeliveryStatus.RUNNING)
        self.assertNotIn(task.task_id, saved_state.metadata.get("waiting_for_authority_task_ids", ()))
        self.assertEqual(decision_commit.blackboard_entry.entry_kind, BlackboardEntryKind.DECISION)

    async def test_commit_authority_request_exposes_outbox_truth(self) -> None:
        store = InMemoryOrchestrationStore()
        bus = InMemoryEventBus()
        runtime = GroupRuntime(store=store, bus=bus)

        await runtime.create_group("group-a")
        await runtime.create_team(group_id="group-a", team_id="team-a", name="Runtime")
        task = await runtime.submit_task(
            group_id="group-a",
            team_id="team-a",
            goal="Request authority with coordination outbox truth.",
            scope=TaskScope.TEAM,
            lane_id="lane-a",
            created_by="leader-a",
        )
        await store.save_delivery_state(
            DeliveryState(
                delivery_id="obj-a:lane:lane-a",
                objective_id="obj-a",
                kind=DeliveryStateKind.LANE,
                status=DeliveryStatus.RUNNING,
                lane_id="lane-a",
                team_id="team-a",
            )
        )

        request = ScopeExtensionRequest(
            request_id="auth-req-outbox-1",
            assignment_id=f"{task.task_id}:assignment",
            worker_id="team-a:teammate:1",
            task_id=task.task_id,
            requested_paths=("src/agent_orchestra/self_hosting/bootstrap.py",),
            reason="Need scope expansion.",
            evidence="blocked by owned_paths boundary",
            retry_hint="grant bootstrap path",
        )
        commit = await runtime.commit_authority_request(
            objective_id="obj-a",
            lane_id="lane-a",
            team_id="team-a",
            task_id=task.task_id,
            worker_id="team-a:teammate:1",
            authority_request=request,
            record=WorkerRecord(
                worker_id="team-a:teammate:1",
                assignment_id=request.assignment_id,
                backend="in_process",
                role="teammate",
                status=WorkerStatus.FAILED,
            ),
        )

        self.assertTrue(hasattr(commit, "outbox"))
        self.assertEqual(len(commit.outbox), 1)
        self.assertEqual(commit.outbox[0].subject, "authority.request")
        self.assertIsInstance(commit.blackboard_entry.payload.get("coordination_outbox"), list)
        self.assertEqual(
            commit.blackboard_entry.payload["coordination_outbox"][0]["subject"],
            "authority.request",
        )
        outbox_records = await store.list_coordination_outbox_records()
        self.assertEqual(len(outbox_records), 1)
        self.assertEqual(outbox_records[0].subject, "authority.request")

    async def test_commit_authority_request_reads_back_authoritative_persisted_truth(self) -> None:
        store = _AuthoritativeAuthorityCommitStore()
        bus = InMemoryEventBus()
        runtime = GroupRuntime(store=store, bus=bus)

        await runtime.create_group("group-a")
        await runtime.create_team(group_id="group-a", team_id="team-a", name="Runtime")
        task = await runtime.submit_task(
            group_id="group-a",
            team_id="team-a",
            goal="Request authority with authoritative readback.",
            scope=TaskScope.TEAM,
            lane_id="lane-a",
            created_by="leader-a",
        )
        await store.save_delivery_state(
            DeliveryState(
                delivery_id="obj-a:lane:lane-a",
                objective_id="obj-a",
                kind=DeliveryStateKind.LANE,
                status=DeliveryStatus.RUNNING,
                lane_id="lane-a",
                team_id="team-a",
            )
        )
        session_snapshot = AgentSession(
            session_id="team-a:teammate:1:resident",
            agent_id="team-a:teammate:1",
            role="teammate",
            objective_id="obj-a",
            lane_id="lane-a",
            team_id="team-a",
            metadata={"activation_epoch": 3},
        )

        request = ScopeExtensionRequest(
            request_id="auth-req-readback-1",
            assignment_id=f"{task.task_id}:assignment",
            worker_id="team-a:teammate:1",
            task_id=task.task_id,
            requested_paths=("src/agent_orchestra/self_hosting/bootstrap.py",),
            reason="Need authoritative readback.",
            evidence="blocked by owned_paths boundary",
            retry_hint="grant bootstrap path",
        )
        commit = await runtime.commit_authority_request(
            objective_id="obj-a",
            lane_id="lane-a",
            team_id="team-a",
            task_id=task.task_id,
            worker_id="team-a:teammate:1",
            authority_request=request,
            record=WorkerRecord(
                worker_id="team-a:teammate:1",
                assignment_id=request.assignment_id,
                backend="in_process",
                role="teammate",
                status=WorkerStatus.FAILED,
            ),
            session_snapshot=session_snapshot,
        )

        self.assertEqual(commit.task.reason, "persisted authority request reason")
        self.assertEqual(commit.blackboard_entry.summary, "Persisted authority request entry.")
        self.assertEqual(commit.blackboard_entry.payload["persisted_marker"], "authority.request")
        self.assertIsNotNone(commit.delivery_state)
        assert commit.delivery_state is not None
        self.assertEqual(commit.delivery_state.summary, "Persisted authority request delivery.")
        self.assertIsNotNone(commit.session_snapshot)
        assert commit.session_snapshot is not None
        self.assertEqual(commit.session_snapshot.metadata["persisted_marker"], "authority.request")
        loaded_task = await store.get_task(task.task_id)
        loaded_entries = await store.list_blackboard_entries(commit.blackboard_entry.blackboard_id)
        loaded_delivery = await store.get_delivery_state("obj-a:lane:lane-a")
        loaded_session = await store.get_agent_session("team-a:teammate:1:resident")

        self.assertEqual(loaded_task, commit.task)
        self.assertIn(commit.blackboard_entry, loaded_entries)
        self.assertEqual(loaded_delivery, commit.delivery_state)
        self.assertEqual(loaded_session, commit.session_snapshot)

    async def test_commit_authority_decision_exposes_outbox_truth(self) -> None:
        store = InMemoryOrchestrationStore()
        bus = InMemoryEventBus()
        runtime = GroupRuntime(store=store, bus=bus)

        await runtime.create_group("group-a")
        await runtime.create_team(group_id="group-a", team_id="team-a", name="Runtime")
        task = await runtime.submit_task(
            group_id="group-a",
            team_id="team-a",
            goal="Decide authority with coordination outbox truth.",
            scope=TaskScope.TEAM,
            lane_id="lane-a",
            created_by="leader-a",
        )
        await store.save_delivery_state(
            DeliveryState(
                delivery_id="obj-a:lane:lane-a",
                objective_id="obj-a",
                kind=DeliveryStateKind.LANE,
                status=DeliveryStatus.RUNNING,
                lane_id="lane-a",
                team_id="team-a",
            )
        )
        request = ScopeExtensionRequest(
            request_id="auth-req-outbox-2",
            assignment_id=f"{task.task_id}:assignment",
            worker_id="team-a:teammate:1",
            task_id=task.task_id,
            requested_paths=("src/agent_orchestra/self_hosting/bootstrap.py",),
            reason="Need scope expansion.",
            evidence="blocked by owned_paths boundary",
            retry_hint="grant bootstrap path",
        )
        await runtime.commit_authority_request(
            objective_id="obj-a",
            lane_id="lane-a",
            team_id="team-a",
            task_id=task.task_id,
            worker_id="team-a:teammate:1",
            authority_request=request,
            record=WorkerRecord(
                worker_id="team-a:teammate:1",
                assignment_id=request.assignment_id,
                backend="in_process",
                role="teammate",
                status=WorkerStatus.FAILED,
            ),
        )
        decision = AuthorityDecision(
            request_id=request.request_id,
            decision="grant",
            actor_id="leader-a",
            scope_class="soft_scope",
            granted_paths=("src/agent_orchestra/self_hosting/bootstrap.py",),
            reason="repo-local authority expansion",
            resume_mode="direct_reactivation",
            summary="Granted bootstrap.py to continue blocked task.",
        )
        commit = await runtime.commit_authority_decision(
            objective_id="obj-a",
            lane_id="lane-a",
            team_id="team-a",
            task_id=task.task_id,
            actor_id="leader-a",
            authority_decision=decision,
        )

        self.assertTrue(hasattr(commit, "outbox"))
        self.assertEqual(len(commit.outbox), 1)
        self.assertEqual(commit.outbox[0].subject, "authority.decision")
        self.assertEqual(commit.outbox[0].recipient, "team-a:teammate:1")
        self.assertIsInstance(commit.blackboard_entry.payload.get("coordination_outbox"), list)
        self.assertEqual(
            commit.blackboard_entry.payload["coordination_outbox"][0]["subject"],
            "authority.decision",
        )
        outbox_records = await store.list_coordination_outbox_records()
        self.assertEqual(len(outbox_records), 2)
        self.assertEqual(outbox_records[-1].subject, "authority.decision")

    async def test_commit_authority_decision_reads_back_authoritative_persisted_truth(self) -> None:
        store = _AuthoritativeAuthorityCommitStore()
        bus = InMemoryEventBus()
        runtime = GroupRuntime(store=store, bus=bus)

        await runtime.create_group("group-a")
        await runtime.create_team(group_id="group-a", team_id="team-a", name="Runtime")
        task = await runtime.submit_task(
            group_id="group-a",
            team_id="team-a",
            goal="Decide authority with authoritative readback.",
            scope=TaskScope.TEAM,
            lane_id="lane-a",
            created_by="leader-a",
        )
        await store.save_delivery_state(
            DeliveryState(
                delivery_id="obj-a:lane:lane-a",
                objective_id="obj-a",
                kind=DeliveryStateKind.LANE,
                status=DeliveryStatus.RUNNING,
                lane_id="lane-a",
                team_id="team-a",
            )
        )
        request = ScopeExtensionRequest(
            request_id="auth-req-readback-2",
            assignment_id=f"{task.task_id}:assignment",
            worker_id="team-a:teammate:1",
            task_id=task.task_id,
            requested_paths=("src/agent_orchestra/self_hosting/bootstrap.py",),
            reason="Need scope expansion.",
            evidence="blocked by owned_paths boundary",
            retry_hint="grant bootstrap path",
        )
        await runtime.commit_authority_request(
            objective_id="obj-a",
            lane_id="lane-a",
            team_id="team-a",
            task_id=task.task_id,
            worker_id="team-a:teammate:1",
            authority_request=request,
            record=WorkerRecord(
                worker_id="team-a:teammate:1",
                assignment_id=request.assignment_id,
                backend="in_process",
                role="teammate",
                status=WorkerStatus.FAILED,
            ),
        )
        session_snapshot = AgentSession(
            session_id="team-a:teammate:1:resident",
            agent_id="team-a:teammate:1",
            role="teammate",
            objective_id="obj-a",
            lane_id="lane-a",
            team_id="team-a",
            metadata={"activation_epoch": 4},
        )
        decision = AuthorityDecision(
            request_id=request.request_id,
            decision="grant",
            actor_id="leader-a",
            scope_class="soft_scope",
            granted_paths=("src/agent_orchestra/self_hosting/bootstrap.py",),
            reason="repo-local authority expansion",
            resume_mode="direct_reactivation",
            summary="Granted bootstrap.py to continue blocked task.",
        )
        commit = await runtime.commit_authority_decision(
            objective_id="obj-a",
            lane_id="lane-a",
            team_id="team-a",
            task_id=task.task_id,
            actor_id="leader-a",
            authority_decision=decision,
            session_snapshot=session_snapshot,
        )

        self.assertEqual(commit.task.reason, "persisted authority decision reason")
        self.assertEqual(commit.blackboard_entry.summary, "Persisted authority decision entry.")
        self.assertEqual(commit.blackboard_entry.payload["persisted_marker"], "authority.decision")
        self.assertIsNotNone(commit.delivery_state)
        assert commit.delivery_state is not None
        self.assertEqual(commit.delivery_state.summary, "Persisted authority decision delivery.")
        self.assertIsNotNone(commit.session_snapshot)
        assert commit.session_snapshot is not None
        self.assertEqual(commit.session_snapshot.metadata["persisted_marker"], "authority.decision")
        loaded_task = await store.get_task(task.task_id)
        loaded_entries = await store.list_blackboard_entries(commit.blackboard_entry.blackboard_id)
        loaded_delivery = await store.get_delivery_state("obj-a:lane:lane-a")
        loaded_session = await store.get_agent_session("team-a:teammate:1:resident")

        self.assertEqual(loaded_task, commit.task)
        self.assertIn(commit.blackboard_entry, loaded_entries)
        self.assertEqual(loaded_delivery, commit.delivery_state)
        self.assertEqual(loaded_session, commit.session_snapshot)

    async def test_commit_directed_task_receipt_exposes_durable_outbox_and_commit_truth(self) -> None:
        store = InMemoryOrchestrationStore()
        bus = InMemoryEventBus()
        runtime = GroupRuntime(store=store, bus=bus)

        await runtime.create_group("group-a")
        await runtime.create_team(group_id="group-a", team_id="team-a", name="Runtime")
        task = await runtime.submit_task(
            group_id="group-a",
            team_id="team-a",
            goal="Persist receipt coordination truth.",
            scope=TaskScope.TEAM,
            lane_id="lane-a",
            created_by="leader-a",
        )
        await store.save_delivery_state(
            DeliveryState(
                delivery_id="obj-a:lane:lane-a",
                objective_id="obj-a",
                kind=DeliveryStateKind.LANE,
                status=DeliveryStatus.RUNNING,
                lane_id="lane-a",
                team_id="team-a",
            )
        )
        session_snapshot = AgentSession(
            session_id="team-a:teammate:1:resident",
            agent_id="team-a:teammate:1",
            role="teammate",
            objective_id="obj-a",
            lane_id="lane-a",
            team_id="team-a",
            current_worker_session_id="team-a:teammate:1:resident",
            last_worker_session_id="team-a:teammate:1:resident",
            metadata={"activation_epoch": 2},
        )
        await store.save_worker_session(
            WorkerSession(
                session_id="team-a:teammate:1:resident",
                worker_id="team-a:teammate:1",
                assignment_id=f"{task.task_id}:assignment",
                backend="scripted",
                role="teammate",
                status=WorkerSessionStatus.ACTIVE,
                lifecycle_status="running",
                mailbox_cursor={
                    "stream": "mailbox",
                    "event_id": "env-receipt-stale",
                    "last_envelope_id": "env-receipt-stale",
                },
            )
        )

        commit = await runtime.commit_directed_task_receipt(
            objective_id="obj-a",
            lane_id="lane-a",
            team_id="team-a",
            task_id=task.task_id,
            worker_id="team-a:teammate:1",
            claim_source="teammate.directed",
            directive_id="directive-receipt-1",
            correlation_id="corr-receipt-1",
            claim_session_id="claim-receipt-1",
            mailbox_consumer="team-a:teammate:1",
            consumer_cursor={
                "stream": "mailbox",
                "event_id": "env-receipt-1",
                "last_envelope_id": "env-receipt-1",
            },
            status_summary="Receipt committed.",
            session_snapshot=session_snapshot,
        )

        self.assertTrue(hasattr(commit, "outbox"))
        self.assertEqual(len(commit.outbox), 1)
        self.assertEqual(commit.outbox[0].subject, "task.receipt")
        self.assertEqual(commit.outbox[0].recipient, "leader:lane-a")
        self.assertIsInstance(commit.blackboard_entry.payload.get("coordination_outbox"), list)
        self.assertEqual(
            commit.blackboard_entry.payload["coordination_outbox"][0]["subject"],
            "task.receipt",
        )
        loaded_task = await store.get_task(task.task_id)
        loaded_entries = await store.list_blackboard_entries(commit.blackboard_entry.blackboard_id)
        loaded_delivery = await store.get_delivery_state("obj-a:lane:lane-a")
        loaded_cursor = await store.get_protocol_bus_cursor(
            stream="mailbox",
            consumer="team-a:teammate:1",
        )
        loaded_session = await store.get_agent_session("team-a:teammate:1:resident")
        loaded_worker_session = await store.get_worker_session("team-a:teammate:1:resident")
        outbox_records = await store.list_coordination_outbox_records()

        self.assertEqual(loaded_task, commit.task)
        self.assertIn(commit.blackboard_entry, loaded_entries)
        self.assertEqual(loaded_delivery, commit.delivery_state)
        self.assertEqual(loaded_cursor, commit.protocol_bus_cursor)
        self.assertEqual(loaded_session, commit.session_snapshot)
        self.assertIsNotNone(loaded_worker_session)
        assert loaded_worker_session is not None
        self.assertEqual(
            loaded_worker_session.mailbox_cursor["last_envelope_id"],
            "env-receipt-1",
        )
        self.assertEqual(len(outbox_records), 1)
        self.assertEqual(outbox_records[0].subject, "task.receipt")

    async def test_commit_teammate_result_exposes_durable_outbox_and_commit_truth(self) -> None:
        store = InMemoryOrchestrationStore()
        bus = InMemoryEventBus()
        runtime = GroupRuntime(store=store, bus=bus)

        await runtime.create_group("group-a")
        await runtime.create_team(group_id="group-a", team_id="team-a", name="Runtime")
        task = await runtime.submit_task(
            group_id="group-a",
            team_id="team-a",
            goal="Persist teammate result coordination truth.",
            scope=TaskScope.TEAM,
            lane_id="lane-a",
            created_by="leader-a",
        )
        await runtime.claim_task(
            task_id=task.task_id,
            owner_id="team-a:teammate:1",
            claim_source="teammate.directed",
            claim_session_id="claim-result-1",
            claimed_at="2026-04-09T10:00:00+00:00",
        )
        await store.save_delivery_state(
            DeliveryState(
                delivery_id="obj-a:lane:lane-a",
                objective_id="obj-a",
                kind=DeliveryStateKind.LANE,
                status=DeliveryStatus.RUNNING,
                lane_id="lane-a",
                team_id="team-a",
            )
        )
        session_snapshot = AgentSession(
            session_id="team-a:teammate:1:resident",
            agent_id="team-a:teammate:1",
            role="teammate",
            objective_id="obj-a",
            lane_id="lane-a",
            team_id="team-a",
            metadata={"last_worker_session_id": "worker-session-result-1"},
        )

        commit = await runtime.commit_teammate_result(
            objective_id="obj-a",
            lane_id="lane-a",
            team_id="team-a",
            task_id=task.task_id,
            worker_id="team-a:teammate:1",
            record=WorkerRecord(
                worker_id="team-a:teammate:1",
                assignment_id=f"{task.task_id}:assignment",
                backend="in_process",
                role="teammate",
                status=WorkerStatus.COMPLETED,
                output_text="Completed runtime transaction alignment.",
            ),
            session_snapshot=session_snapshot,
        )

        self.assertTrue(hasattr(commit, "outbox"))
        self.assertEqual(len(commit.outbox), 1)
        self.assertEqual(commit.outbox[0].subject, "task.result")
        self.assertEqual(commit.outbox[0].recipient, "leader:lane-a")
        self.assertIsInstance(commit.blackboard_entry.payload.get("coordination_outbox"), list)
        self.assertEqual(
            commit.blackboard_entry.payload["coordination_outbox"][0]["subject"],
            "task.result",
        )
        loaded_task = await store.get_task(task.task_id)
        loaded_entries = await store.list_blackboard_entries(commit.blackboard_entry.blackboard_id)
        loaded_delivery = await store.get_delivery_state("obj-a:lane:lane-a")
        loaded_session = await store.get_agent_session("team-a:teammate:1:resident")
        outbox_records = await store.list_coordination_outbox_records()

        self.assertEqual(loaded_task, commit.task)
        self.assertIn(commit.blackboard_entry, loaded_entries)
        self.assertEqual(loaded_delivery, commit.delivery_state)
        self.assertEqual(loaded_session, commit.session_snapshot)
        self.assertEqual(len(outbox_records), 1)
        self.assertEqual(outbox_records[0].subject, "task.result")

    async def test_commit_teammate_result_reads_back_host_projected_session_truth(self) -> None:
        store = InMemoryOrchestrationStore()
        bus = InMemoryEventBus()
        supervisor = DefaultWorkerSupervisor(store=store, launch_backends={})
        runtime = GroupRuntime(store=store, bus=bus, supervisor=supervisor)

        await runtime.create_group("group-a")
        await runtime.create_team(group_id="group-a", team_id="team-a", name="Runtime")
        task = await runtime.submit_task(
            group_id="group-a",
            team_id="team-a",
            goal="Prefer host-projected session truth during teammate finalize.",
            scope=TaskScope.TEAM,
            lane_id="lane-a",
            created_by="leader-a",
        )
        await runtime.claim_task(
            task_id=task.task_id,
            owner_id="team-a:teammate:1",
            claim_source="teammate.directed",
            claim_session_id="claim-result-projected-1",
            claimed_at="2026-04-09T10:00:00+00:00",
        )
        await store.save_delivery_state(
            DeliveryState(
                delivery_id="obj-a:lane:lane-a",
                objective_id="obj-a",
                kind=DeliveryStateKind.LANE,
                status=DeliveryStatus.RUNNING,
                lane_id="lane-a",
                team_id="team-a",
            )
        )
        await store.save_worker_session(
            WorkerSession(
                session_id="team-a:teammate:1:resident",
                worker_id="team-a:teammate:1",
                assignment_id=f"{task.task_id}:assignment",
                backend="scripted",
                role="teammate",
                status=WorkerSessionStatus.ACTIVE,
                lifecycle_status="running",
                last_active_at="2026-04-09T10:03:00+00:00",
                metadata={
                    "group_id": "group-a",
                    "task_id": task.task_id,
                    "objective_id": "obj-a",
                    "team_id": "team-a",
                    "lane_id": "lane-a",
                },
            )
        )
        session_snapshot = AgentSession(
            session_id="team-a:teammate:1:resident",
            agent_id="team-a:teammate:1",
            role="teammate",
            phase=ResidentCoordinatorPhase.RUNNING,
            objective_id="obj-a",
            lane_id="lane-a",
            team_id="team-a",
            mailbox_cursor={
                "stream": "mailbox",
                "event_id": "env-host-projected-1",
                "last_envelope_id": "env-host-projected-1",
            },
            current_directive_ids=("directive-host-projected-1",),
            current_worker_session_id="team-a:teammate:1:resident",
            last_worker_session_id="team-a:teammate:1:resident",
            metadata={"custom_marker": "host-owned"},
        )

        commit = await runtime.commit_teammate_result(
            objective_id="obj-a",
            lane_id="lane-a",
            team_id="team-a",
            task_id=task.task_id,
            worker_id="team-a:teammate:1",
            record=WorkerRecord(
                worker_id="team-a:teammate:1",
                assignment_id=f"{task.task_id}:assignment",
                backend="scripted",
                role="teammate",
                status=WorkerStatus.COMPLETED,
                output_text="Projected finalize readback.",
                session=WorkerSession(
                    session_id="team-a:teammate:1:resident",
                    worker_id="team-a:teammate:1",
                    assignment_id=f"{task.task_id}:assignment",
                    backend="scripted",
                    role="teammate",
                    status=WorkerSessionStatus.COMPLETED,
                    lifecycle_status="completed",
                ),
            ),
            session_snapshot=session_snapshot,
        )

        self.assertIsNotNone(commit.session_snapshot)
        assert commit.session_snapshot is not None
        self.assertEqual(
            commit.session_snapshot.mailbox_cursor["last_envelope_id"],
            "env-host-projected-1",
        )
        self.assertEqual(
            commit.session_snapshot.current_directive_ids,
            ("directive-host-projected-1",),
        )
        self.assertEqual(commit.session_snapshot.metadata["custom_marker"], "host-owned")
        self.assertIsNone(commit.session_snapshot.current_worker_session_id)
        self.assertEqual(
            commit.session_snapshot.last_worker_session_id,
            "team-a:teammate:1:resident",
        )
        stored_worker_session = await store.get_worker_session("team-a:teammate:1:resident")
        self.assertIsNotNone(stored_worker_session)
        assert stored_worker_session is not None
        self.assertEqual(stored_worker_session.status, WorkerSessionStatus.COMPLETED)

    async def test_commit_authority_request_overrides_stale_worker_session_before_host_readback(self) -> None:
        store = InMemoryOrchestrationStore()
        bus = InMemoryEventBus()
        supervisor = DefaultWorkerSupervisor(store=store, launch_backends={})
        runtime = GroupRuntime(store=store, bus=bus, supervisor=supervisor)

        await runtime.create_group("group-a")
        await runtime.create_team(group_id="group-a", team_id="team-a", name="Runtime")
        task = await runtime.submit_task(
            group_id="group-a",
            team_id="team-a",
            goal="Request authority after a stale active worker snapshot.",
            scope=TaskScope.TEAM,
            lane_id="lane-a",
            created_by="leader-a",
        )
        await store.save_delivery_state(
            DeliveryState(
                delivery_id="obj-a:lane:lane-a",
                objective_id="obj-a",
                kind=DeliveryStateKind.LANE,
                status=DeliveryStatus.RUNNING,
                lane_id="lane-a",
                team_id="team-a",
            )
        )
        await store.save_worker_session(
            WorkerSession(
                session_id="team-a:teammate:1:resident",
                worker_id="team-a:teammate:1",
                assignment_id=f"{task.task_id}:assignment",
                backend="scripted",
                role="teammate",
                status=WorkerSessionStatus.ACTIVE,
                lifecycle_status="running",
                last_active_at="2026-04-09T10:03:00+00:00",
            )
        )
        session_snapshot = AgentSession(
            session_id="team-a:teammate:1:resident",
            agent_id="team-a:teammate:1",
            role="teammate",
            phase=ResidentCoordinatorPhase.RUNNING,
            objective_id="obj-a",
            lane_id="lane-a",
            team_id="team-a",
            mailbox_cursor={
                "stream": "mailbox",
                "event_id": "env-authority-host-1",
                "last_envelope_id": "env-authority-host-1",
            },
            metadata={"custom_marker": "host-owned"},
        )
        request = ScopeExtensionRequest(
            request_id="auth-req-worker-session-readback-1",
            assignment_id=f"{task.task_id}:assignment",
            worker_id="team-a:teammate:1",
            task_id=task.task_id,
            requested_paths=("src/agent_orchestra/self_hosting/bootstrap.py",),
            reason="Need scope expansion after blocked worker execution.",
            evidence="blocked by owned_paths boundary",
            retry_hint="grant bootstrap path",
        )

        commit = await runtime.commit_authority_request(
            objective_id="obj-a",
            lane_id="lane-a",
            team_id="team-a",
            task_id=task.task_id,
            worker_id="team-a:teammate:1",
            authority_request=request,
            record=WorkerRecord(
                worker_id="team-a:teammate:1",
                assignment_id=request.assignment_id,
                backend="scripted",
                role="teammate",
                status=WorkerStatus.FAILED,
                session=WorkerSession(
                    session_id="team-a:teammate:1:resident",
                    worker_id="team-a:teammate:1",
                    assignment_id=request.assignment_id,
                    backend="scripted",
                    role="teammate",
                    status=WorkerSessionStatus.FAILED,
                    lifecycle_status="blocked",
                    last_active_at="2026-04-09T10:05:00+00:00",
                    mailbox_cursor={
                        "stream": "mailbox",
                        "event_id": "env-authority-host-1",
                        "last_envelope_id": "env-authority-host-1",
                    },
                ),
            ),
            session_snapshot=session_snapshot,
        )

        self.assertIsNotNone(commit.session_snapshot)
        assert commit.session_snapshot is not None
        self.assertEqual(
            commit.session_snapshot.mailbox_cursor["last_envelope_id"],
            "env-authority-host-1",
        )
        self.assertEqual(commit.session_snapshot.metadata["custom_marker"], "host-owned")
        self.assertIsNone(commit.session_snapshot.current_worker_session_id)
        self.assertEqual(
            commit.session_snapshot.last_worker_session_id,
            "team-a:teammate:1:resident",
        )
        stored_worker_session = await store.get_worker_session("team-a:teammate:1:resident")
        self.assertIsNotNone(stored_worker_session)
        assert stored_worker_session is not None
        self.assertEqual(stored_worker_session.status, WorkerSessionStatus.FAILED)

    async def test_commit_authority_decision_deny_blocks_task(self) -> None:
        store = InMemoryOrchestrationStore()
        bus = InMemoryEventBus()
        runtime = GroupRuntime(store=store, bus=bus)

        await runtime.create_group("group-a")
        await runtime.create_team(group_id="group-a", team_id="team-a", name="Runtime")
        task = await runtime.submit_task(
            group_id="group-a",
            team_id="team-a",
            goal="Repair protected blocker",
            scope=TaskScope.TEAM,
            lane_id="lane-a",
            created_by="leader-a",
        )
        await store.save_delivery_state(
            DeliveryState(
                delivery_id="obj-a:lane:lane-a",
                objective_id="obj-a",
                kind=DeliveryStateKind.LANE,
                status=DeliveryStatus.RUNNING,
                lane_id="lane-a",
                team_id="team-a",
            )
        )
        request = ScopeExtensionRequest(
            request_id="auth-req-2",
            assignment_id=f"{task.task_id}:assignment",
            worker_id="team-a:teammate:1",
            task_id=task.task_id,
            requested_paths=("src/agent_orchestra/contracts/task.py",),
            reason="Need protected contract authority.",
            evidence="contract file out of scope",
            retry_hint="escalate or deny",
        )
        await runtime.commit_authority_request(
            objective_id="obj-a",
            lane_id="lane-a",
            team_id="team-a",
            task_id=task.task_id,
            worker_id="team-a:teammate:1",
            authority_request=request,
            record=WorkerRecord(
                worker_id="team-a:teammate:1",
                assignment_id=request.assignment_id,
                backend="in_process",
                role="teammate",
                status=WorkerStatus.FAILED,
                metadata={
                    "final_report": {
                        "assignment_id": request.assignment_id,
                        "worker_id": request.worker_id,
                        "authority_request": request.to_dict(),
                        "terminal_status": "blocked",
                    }
                },
            ),
        )

        decision = AuthorityDecision(
            request_id=request.request_id,
            decision="deny",
            actor_id="leader-a",
            scope_class="global_contract",
            reason="protected contract file is not leader-grantable",
            summary="Denied protected authority request.",
        )
        await runtime.commit_authority_decision(
            objective_id="obj-a",
            lane_id="lane-a",
            team_id="team-a",
            task_id=task.task_id,
            actor_id="leader-a",
            authority_decision=decision,
        )

        saved_task = await store.get_task(task.task_id)
        saved_state = await store.get_delivery_state("obj-a:lane:lane-a")

        self.assertIsNotNone(saved_task)
        assert saved_task is not None
        self.assertEqual(saved_task.status, TaskStatus.BLOCKED)
        self.assertEqual(saved_task.blocked_by, ("authority.denied", "auth-req-2"))
        self.assertIsNone(saved_task.authority_request_id)
        self.assertEqual(saved_task.authority_decision_payload["decision"], "deny")
        self.assertIsNotNone(saved_state)
        assert saved_state is not None
        self.assertEqual(saved_state.status, DeliveryStatus.BLOCKED)

    async def test_commit_authority_decision_reroute_requires_replacement_task(self) -> None:
        store = InMemoryOrchestrationStore()
        bus = InMemoryEventBus()
        runtime = GroupRuntime(store=store, bus=bus)

        await runtime.create_group("group-a")
        await runtime.create_team(group_id="group-a", team_id="team-a", name="Runtime")
        task = await runtime.submit_task(
            group_id="group-a",
            team_id="team-a",
            goal="Repair reroute blocker",
            scope=TaskScope.TEAM,
            lane_id="lane-a",
            created_by="leader-a",
        )
        await store.save_delivery_state(
            DeliveryState(
                delivery_id="obj-a:lane:lane-a",
                objective_id="obj-a",
                kind=DeliveryStateKind.LANE,
                status=DeliveryStatus.RUNNING,
                lane_id="lane-a",
                team_id="team-a",
            )
        )

        request = ScopeExtensionRequest(
            request_id="auth-req-reroute-missing",
            assignment_id=f"{task.task_id}:assignment",
            worker_id="team-a:teammate:1",
            task_id=task.task_id,
            requested_paths=("src/agent_orchestra/self_hosting/bootstrap.py",),
            reason="Need reroute target for protected path.",
            evidence="task cannot continue in current lane owner profile",
            retry_hint="reroute to dedicated repair slice",
        )
        await runtime.commit_authority_request(
            objective_id="obj-a",
            lane_id="lane-a",
            team_id="team-a",
            task_id=task.task_id,
            worker_id="team-a:teammate:1",
            authority_request=request,
            record=WorkerRecord(
                worker_id="team-a:teammate:1",
                assignment_id=request.assignment_id,
                backend="in_process",
                role="teammate",
                status=WorkerStatus.FAILED,
                metadata={
                    "final_report": {
                        "assignment_id": request.assignment_id,
                        "worker_id": request.worker_id,
                        "authority_request": request.to_dict(),
                        "terminal_status": "blocked",
                    }
                },
            ),
        )

        with self.assertRaisesRegex(ValueError, "replacement_task"):
            await runtime.commit_authority_decision(
                objective_id="obj-a",
                lane_id="lane-a",
                team_id="team-a",
                task_id=task.task_id,
                actor_id="leader-a",
                authority_decision=AuthorityDecision(
                    request_id=request.request_id,
                    decision="reroute",
                    actor_id="leader-a",
                    scope_class="soft_scope",
                    reason="reroute requested by leader",
                    summary="Reroute blocked task.",
                ),
            )

    async def test_commit_authority_decision_reroute_links_superseded_and_replacement_tasks(self) -> None:
        store = InMemoryOrchestrationStore()
        bus = InMemoryEventBus()
        runtime = GroupRuntime(store=store, bus=bus)

        await runtime.create_group("group-a")
        await runtime.create_team(group_id="group-a", team_id="team-a", name="Runtime")
        blocked_task = await runtime.submit_task(
            group_id="group-a",
            team_id="team-a",
            goal="Repair authority blocker",
            scope=TaskScope.TEAM,
            lane_id="lane-a",
            owned_paths=("src/agent_orchestra/runtime/leader_loop.py",),
            created_by="leader-a",
        )
        await store.save_delivery_state(
            DeliveryState(
                delivery_id="obj-a:lane:lane-a",
                objective_id="obj-a",
                kind=DeliveryStateKind.LANE,
                status=DeliveryStatus.RUNNING,
                lane_id="lane-a",
                team_id="team-a",
                pending_task_ids=(blocked_task.task_id,),
            )
        )

        request = ScopeExtensionRequest(
            request_id="auth-req-reroute-1",
            assignment_id=f"{blocked_task.task_id}:assignment",
            worker_id="team-a:teammate:1",
            task_id=blocked_task.task_id,
            requested_paths=("src/agent_orchestra/self_hosting/bootstrap.py",),
            reason="Need self-hosting bootstrap path with different lane task.",
            evidence="current teammate role should not own bootstrap changes directly",
            retry_hint="reroute to dedicated self-hosting repair task",
        )
        await runtime.commit_authority_request(
            objective_id="obj-a",
            lane_id="lane-a",
            team_id="team-a",
            task_id=blocked_task.task_id,
            worker_id="team-a:teammate:1",
            authority_request=request,
            record=WorkerRecord(
                worker_id="team-a:teammate:1",
                assignment_id=request.assignment_id,
                backend="in_process",
                role="teammate",
                status=WorkerStatus.FAILED,
                metadata={
                    "final_report": {
                        "assignment_id": request.assignment_id,
                        "worker_id": request.worker_id,
                        "authority_request": request.to_dict(),
                        "terminal_status": "blocked",
                    }
                },
            ),
        )
        replacement = await runtime.submit_task(
            group_id="group-a",
            team_id="team-a",
            goal="Dedicated reroute repair task",
            scope=TaskScope.TEAM,
            lane_id="lane-a",
            created_by="leader-a",
        )

        reroute_commit = await runtime.commit_authority_decision(
            objective_id="obj-a",
            lane_id="lane-a",
            team_id="team-a",
            task_id=blocked_task.task_id,
            actor_id="leader-a",
            authority_decision=AuthorityDecision(
                request_id=request.request_id,
                decision="reroute",
                actor_id="leader-a",
                scope_class="soft_scope",
                reason="Reroute to dedicated repair task.",
                summary="Rerouted blocked task to replacement task.",
            ),
            replacement_task=replacement,
        )

        saved_blocked = await store.get_task(blocked_task.task_id)
        saved_replacement = await store.get_task(replacement.task_id)
        saved_state = await store.get_delivery_state("obj-a:lane:lane-a")

        self.assertIsNotNone(saved_blocked)
        assert saved_blocked is not None
        self.assertEqual(saved_blocked.status, TaskStatus.CANCELLED)
        self.assertEqual(saved_blocked.superseded_by_task_id, replacement.task_id)
        self.assertEqual(saved_blocked.authority_request_payload, {})
        self.assertIsNone(saved_blocked.authority_request_id)
        self.assertIsNone(saved_blocked.authority_resume_target)
        self.assertEqual(saved_blocked.authority_decision_payload["decision"], "reroute")
        self.assertEqual(saved_blocked.authority_decision_payload["reroute_task_id"], replacement.task_id)

        self.assertIsNotNone(saved_replacement)
        assert saved_replacement is not None
        self.assertEqual(saved_replacement.status, TaskStatus.PENDING)
        self.assertEqual(saved_replacement.derived_from, blocked_task.task_id)
        self.assertEqual(saved_blocked.surface_mutation.kind, TaskSurfaceMutationKind.SUPERSEDED)
        self.assertEqual(saved_blocked.surface_mutation.target_task_id, replacement.task_id)
        self.assertEqual(saved_replacement.provenance.kind, TaskProvenanceKind.REPLACEMENT)
        self.assertEqual(saved_replacement.provenance.source_task_id, blocked_task.task_id)
        self.assertEqual(saved_replacement.authority_decision_payload["decision"], "reroute")
        self.assertEqual(saved_replacement.authority_decision_payload["reroute_task_id"], replacement.task_id)
        self.assertIn("src/agent_orchestra/self_hosting/bootstrap.py", saved_replacement.owned_paths)

        self.assertIsNotNone(saved_state)
        assert saved_state is not None
        self.assertEqual(saved_state.status, DeliveryStatus.RUNNING)
        self.assertIn(replacement.task_id, saved_state.pending_task_ids)
        self.assertNotIn(blocked_task.task_id, saved_state.pending_task_ids)
        self.assertNotIn(blocked_task.task_id, saved_state.metadata.get("waiting_for_authority_task_ids", ()))
        self.assertEqual(
            saved_state.metadata["teammate_coordination"]["last_authority_reroute_task_id"],
            replacement.task_id,
        )

    async def test_commit_task_surface_mutation_marks_task_not_needed(self) -> None:
        store = InMemoryOrchestrationStore()
        bus = InMemoryEventBus()
        runtime = GroupRuntime(store=store, bus=bus)

        await runtime.create_group("group-a")
        await runtime.create_team(group_id="group-a", team_id="team-a", name="Runtime")
        task = await runtime.submit_task(
            group_id="group-a",
            team_id="team-a",
            goal="Remove redundant runtime slice",
            scope=TaskScope.TEAM,
            lane_id="lane-a",
            created_by="leader-a",
        )
        await store.save_delivery_state(
            DeliveryState(
                delivery_id="obj-a:lane:lane-a",
                objective_id="obj-a",
                kind=DeliveryStateKind.LANE,
                status=DeliveryStatus.RUNNING,
                lane_id="lane-a",
                team_id="team-a",
                pending_task_ids=(task.task_id,),
            )
        )

        commit = await runtime.commit_task_surface_mutation(
            objective_id="obj-a",
            lane_id="lane-a",
            team_id="team-a",
            task_id=task.task_id,
            actor_id="leader-a",
            mutation_kind=TaskSurfaceMutationKind.NOT_NEEDED,
            reason="Follow-up review showed the slice is already covered by another task.",
        )

        saved_task = await store.get_task(task.task_id)
        saved_state = await store.get_delivery_state("obj-a:lane:lane-a")

        self.assertIsNotNone(saved_task)
        self.assertIsNotNone(saved_state)
        assert saved_task is not None
        assert saved_state is not None
        self.assertEqual(saved_task.status, TaskStatus.CANCELLED)
        self.assertEqual(saved_task.surface_mutation.kind, TaskSurfaceMutationKind.NOT_NEEDED)
        self.assertEqual(saved_task.surface_mutation.actor_id, "leader-a")
        self.assertEqual(
            saved_task.surface_mutation.reason,
            "Follow-up review showed the slice is already covered by another task.",
        )
        self.assertEqual(commit.blackboard_entry.payload["event"], "task.mutation")
        self.assertEqual(
            commit.blackboard_entry.payload["mutation"]["kind"],
            TaskSurfaceMutationKind.NOT_NEEDED.value,
        )
        self.assertNotIn(task.task_id, saved_state.pending_task_ids)

    async def test_commit_task_surface_mutation_enforces_teammate_subtree_authority(self) -> None:
        store = InMemoryOrchestrationStore()
        bus = InMemoryEventBus()
        runtime = GroupRuntime(store=store, bus=bus)

        await runtime.create_group("group-a")
        await runtime.create_team(group_id="group-a", team_id="team-a", name="Runtime")
        parent = await runtime.submit_task(
            group_id="group-a",
            team_id="team-a",
            goal="Implement runtime mutation contract",
            scope=TaskScope.TEAM,
            lane_id="lane-a",
            created_by="leader-a",
        )
        await runtime.claim_task(
            task_id=parent.task_id,
            owner_id="group-a:team:team-a:teammate:1",
            claim_source="test.runtime",
        )
        child = await runtime.submit_task(
            group_id="group-a",
            team_id="team-a",
            goal="Add follow-up mutation coverage",
            scope=TaskScope.TEAM,
            lane_id="lane-a",
            created_by="group-a:team:team-a:teammate:1",
            derived_from=parent.task_id,
            reason="Need a child slice owned by the teammate subtree.",
        )
        await store.save_delivery_state(
            DeliveryState(
                delivery_id="obj-a:lane:lane-a",
                objective_id="obj-a",
                kind=DeliveryStateKind.LANE,
                status=DeliveryStatus.RUNNING,
                lane_id="lane-a",
                team_id="team-a",
                pending_task_ids=(parent.task_id, child.task_id),
            )
        )

        escalated_commit = await runtime.commit_task_surface_mutation(
            objective_id="obj-a",
            lane_id="lane-a",
            team_id="team-a",
            task_id=parent.task_id,
            actor_id="group-a:team:team-a:teammate:1",
            mutation_kind=TaskSurfaceMutationKind.NOT_NEEDED,
            reason="Teammate should not directly mutate the leader-owned parent task.",
        )

        commit = await runtime.commit_task_surface_mutation(
            objective_id="obj-a",
            lane_id="lane-a",
            team_id="team-a",
            task_id=child.task_id,
            actor_id="group-a:team:team-a:teammate:1",
            mutation_kind=TaskSurfaceMutationKind.NOT_NEEDED,
            reason="The teammate-owned child slice is no longer needed after local consolidation.",
        )

        self.assertEqual(escalated_commit.task.status, TaskStatus.WAITING_FOR_AUTHORITY)
        self.assertIsNotNone(escalated_commit.authority_request)
        assert escalated_commit.authority_request is not None
        self.assertEqual(
            escalated_commit.task.authority_request_payload["task_surface_authority"]["kind"],
            "mutation",
        )
        self.assertEqual(commit.task.surface_mutation.kind, TaskSurfaceMutationKind.NOT_NEEDED)
        self.assertEqual(commit.task.surface_mutation.actor_id, "group-a:team:team-a:teammate:1")

    async def test_commit_task_surface_mutation_routes_cross_subtree_write_into_authority_flow(self) -> None:
        store = InMemoryOrchestrationStore()
        bus = InMemoryEventBus()
        runtime = GroupRuntime(store=store, bus=bus)

        await runtime.create_group("group-a")
        await runtime.create_team(group_id="group-a", team_id="team-a", name="Runtime")
        parent = await runtime.submit_task(
            group_id="group-a",
            team_id="team-a",
            goal="Implement runtime mutation contract",
            scope=TaskScope.TEAM,
            lane_id="lane-a",
            created_by="leader-a",
        )
        teammate_id = "group-a:team:team-a:teammate:1"
        await runtime.claim_task(
            task_id=parent.task_id,
            owner_id=teammate_id,
            claim_source="test.runtime",
        )
        await store.save_delivery_state(
            DeliveryState(
                delivery_id="obj-a:lane:lane-a",
                objective_id="obj-a",
                kind=DeliveryStateKind.LANE,
                status=DeliveryStatus.RUNNING,
                lane_id="lane-a",
                team_id="team-a",
                pending_task_ids=(parent.task_id,),
                active_task_ids=(parent.task_id,),
            )
        )

        commit = await runtime.commit_task_surface_mutation(
            objective_id="obj-a",
            lane_id="lane-a",
            team_id="team-a",
            task_id=parent.task_id,
            actor_id=teammate_id,
            mutation_kind=TaskSurfaceMutationKind.NOT_NEEDED,
            reason="This leader-owned parent mutation should escalate into the authority flow.",
        )

        self.assertEqual(commit.task.status, TaskStatus.WAITING_FOR_AUTHORITY)
        self.assertIsNotNone(commit.authority_request)
        assert commit.authority_request is not None
        self.assertEqual(commit.authority_request.task_id, parent.task_id)
        self.assertEqual(commit.authority_request.worker_id, teammate_id)
        self.assertEqual(commit.blackboard_entry.payload["event"], "authority.request")
        self.assertEqual(commit.outbox[0].subject, "authority.request")
        self.assertEqual(
            commit.task.authority_request_payload["task_surface_authority"]["kind"],
            "mutation",
        )
        self.assertEqual(
            commit.task.authority_request_payload["task_surface_authority"]["mutation_kind"],
            TaskSurfaceMutationKind.NOT_NEEDED.value,
        )
        self.assertEqual(
            commit.task.authority_request_payload["task_surface_authority"]["actor_id"],
            teammate_id,
        )

    async def test_commit_task_protected_field_write_routes_teammate_patch_into_authority_flow(self) -> None:
        store = InMemoryOrchestrationStore()
        bus = InMemoryEventBus()
        runtime = GroupRuntime(store=store, bus=bus)

        await runtime.create_group("group-a")
        await runtime.create_team(group_id="group-a", team_id="team-a", name="Runtime")
        task = await runtime.submit_task(
            group_id="group-a",
            team_id="team-a",
            goal="Implement runtime authority write contract",
            scope=TaskScope.TEAM,
            lane_id="lane-a",
            owned_paths=("src/runtime.py",),
            created_by="leader-a",
        )
        teammate_id = "group-a:team:team-a:teammate:1"
        await runtime.claim_task(
            task_id=task.task_id,
            owner_id=teammate_id,
            claim_source="test.runtime",
        )
        await store.save_delivery_state(
            DeliveryState(
                delivery_id="obj-a:lane:lane-a",
                objective_id="obj-a",
                kind=DeliveryStateKind.LANE,
                status=DeliveryStatus.RUNNING,
                lane_id="lane-a",
                team_id="team-a",
                active_task_ids=(task.task_id,),
            )
        )

        commit = await runtime.commit_task_protected_field_write(
            objective_id="obj-a",
            lane_id="lane-a",
            team_id="team-a",
            task_id=task.task_id,
            actor_id=teammate_id,
            field_updates={
                "goal": "Updated runtime contract goal after authority grant.",
                "owned_paths": ("src/runtime.py", "docs/runtime.md"),
            },
            reason="Protected task contract update should escalate into the authority flow.",
        )

        self.assertEqual(commit.task.status, TaskStatus.WAITING_FOR_AUTHORITY)
        self.assertIsNotNone(commit.authority_request)
        assert commit.authority_request is not None
        self.assertEqual(commit.blackboard_entry.payload["event"], "authority.request")
        self.assertEqual(commit.outbox[0].subject, "authority.request")
        self.assertEqual(
            commit.task.authority_request_payload["task_surface_authority"]["kind"],
            "protected_field_write",
        )
        self.assertEqual(
            tuple(commit.task.authority_request_payload["task_surface_authority"]["protected_field_names"]),
            ("goal", "owned_paths"),
        )

    async def test_commit_task_surface_mutation_marks_task_merged_into_target(self) -> None:
        store = InMemoryOrchestrationStore()
        bus = InMemoryEventBus()
        runtime = GroupRuntime(store=store, bus=bus)

        await runtime.create_group("group-a")
        await runtime.create_team(group_id="group-a", team_id="team-a", name="Runtime")
        source_task = await runtime.submit_task(
            group_id="group-a",
            team_id="team-a",
            goal="Merge review notes into canonical task",
            scope=TaskScope.TEAM,
            lane_id="lane-a",
            created_by="leader-a",
        )
        target_task = await runtime.submit_task(
            group_id="group-a",
            team_id="team-a",
            goal="Canonical runtime task",
            scope=TaskScope.TEAM,
            lane_id="lane-a",
            created_by="leader-a",
        )
        await store.save_delivery_state(
            DeliveryState(
                delivery_id="obj-a:lane:lane-a",
                objective_id="obj-a",
                kind=DeliveryStateKind.LANE,
                status=DeliveryStatus.RUNNING,
                lane_id="lane-a",
                team_id="team-a",
                pending_task_ids=(source_task.task_id, target_task.task_id),
            )
        )

        commit = await runtime.commit_task_surface_mutation(
            objective_id="obj-a",
            lane_id="lane-a",
            team_id="team-a",
            task_id=source_task.task_id,
            actor_id="leader-a",
            mutation_kind=TaskSurfaceMutationKind.MERGED_INTO,
            target_task_id=target_task.task_id,
            reason="Merged the duplicate slice into the canonical runtime task.",
        )

        saved_source = await store.get_task(source_task.task_id)
        saved_state = await store.get_delivery_state("obj-a:lane:lane-a")

        self.assertIsNotNone(saved_source)
        self.assertIsNotNone(saved_state)
        assert saved_source is not None
        assert saved_state is not None
        self.assertEqual(saved_source.status, TaskStatus.CANCELLED)
        self.assertEqual(saved_source.merged_into_task_id, target_task.task_id)
        self.assertEqual(saved_source.surface_mutation.kind, TaskSurfaceMutationKind.MERGED_INTO)
        self.assertEqual(saved_source.surface_mutation.target_task_id, target_task.task_id)
        self.assertEqual(commit.blackboard_entry.payload["mutation"]["target_task_id"], target_task.task_id)
        self.assertNotIn(source_task.task_id, saved_state.pending_task_ids)

    async def test_commit_authority_decision_grant_applies_task_surface_mutation_intent(self) -> None:
        store = InMemoryOrchestrationStore()
        bus = InMemoryEventBus()
        runtime = GroupRuntime(store=store, bus=bus)

        await runtime.create_group("group-a")
        await runtime.create_team(group_id="group-a", team_id="team-a", name="Runtime")
        task = await runtime.submit_task(
            group_id="group-a",
            team_id="team-a",
            goal="Escalated mutation target",
            scope=TaskScope.TEAM,
            lane_id="lane-a",
            created_by="leader-a",
        )
        teammate_id = "group-a:team:team-a:teammate:1"
        await runtime.claim_task(
            task_id=task.task_id,
            owner_id=teammate_id,
            claim_source="test.runtime",
        )
        await store.save_delivery_state(
            DeliveryState(
                delivery_id="obj-a:lane:lane-a",
                objective_id="obj-a",
                kind=DeliveryStateKind.LANE,
                status=DeliveryStatus.RUNNING,
                lane_id="lane-a",
                team_id="team-a",
                active_task_ids=(task.task_id,),
            )
        )

        request_commit = await runtime.commit_task_surface_mutation(
            objective_id="obj-a",
            lane_id="lane-a",
            team_id="team-a",
            task_id=task.task_id,
            actor_id=teammate_id,
            mutation_kind=TaskSurfaceMutationKind.NOT_NEEDED,
            reason="This mutation should require leader authority before it can be applied.",
        )

        assert request_commit.authority_request is not None
        await runtime.commit_authority_decision(
            objective_id="obj-a",
            lane_id="lane-a",
            team_id="team-a",
            task_id=task.task_id,
            actor_id="leader-a",
            authority_decision=AuthorityDecision(
                request_id=request_commit.authority_request.request_id,
                decision="grant",
                actor_id="leader-a",
                scope_class=AuthorityBoundaryClass.SOFT_SCOPE.value,
                reason="Leader granted the cross-subtree task-surface mutation request.",
                summary="Granted task-surface mutation request.",
            ),
        )

        saved_task = await store.get_task(task.task_id)
        saved_state = await store.get_delivery_state("obj-a:lane:lane-a")

        self.assertIsNotNone(saved_task)
        self.assertIsNotNone(saved_state)
        assert saved_task is not None
        assert saved_state is not None
        self.assertEqual(saved_task.status, TaskStatus.CANCELLED)
        self.assertEqual(saved_task.authority_decision_payload["decision"], "grant")
        self.assertEqual(saved_task.surface_mutation.kind, TaskSurfaceMutationKind.NOT_NEEDED)
        self.assertEqual(saved_task.surface_mutation.actor_id, teammate_id)
        self.assertNotIn(task.task_id, saved_state.pending_task_ids)
        self.assertNotIn(task.task_id, saved_state.active_task_ids)

    async def test_commit_authority_decision_grant_applies_task_protected_field_write_intent(self) -> None:
        store = InMemoryOrchestrationStore()
        bus = InMemoryEventBus()
        runtime = GroupRuntime(store=store, bus=bus)

        await runtime.create_group("group-a")
        await runtime.create_team(group_id="group-a", team_id="team-a", name="Runtime")
        task = await runtime.submit_task(
            group_id="group-a",
            team_id="team-a",
            goal="Escalated protected write target",
            scope=TaskScope.TEAM,
            lane_id="lane-a",
            owned_paths=("src/runtime.py",),
            created_by="leader-a",
        )
        teammate_id = "group-a:team:team-a:teammate:1"
        await runtime.claim_task(
            task_id=task.task_id,
            owner_id=teammate_id,
            claim_source="test.runtime",
        )
        await store.save_delivery_state(
            DeliveryState(
                delivery_id="obj-a:lane:lane-a",
                objective_id="obj-a",
                kind=DeliveryStateKind.LANE,
                status=DeliveryStatus.RUNNING,
                lane_id="lane-a",
                team_id="team-a",
                active_task_ids=(task.task_id,),
            )
        )

        request_commit = await runtime.commit_task_protected_field_write(
            objective_id="obj-a",
            lane_id="lane-a",
            team_id="team-a",
            task_id=task.task_id,
            actor_id=teammate_id,
            field_updates={
                "goal": "Protected write grant updated the task goal.",
                "owned_paths": ("src/runtime.py", "docs/runtime.md"),
            },
            reason="This protected task-surface write should require higher authority.",
        )

        assert request_commit.authority_request is not None
        await runtime.commit_authority_decision(
            objective_id="obj-a",
            lane_id="lane-a",
            team_id="team-a",
            task_id=task.task_id,
            actor_id="superleader:obj-a",
            authority_decision=AuthorityDecision(
                request_id=request_commit.authority_request.request_id,
                decision="grant",
                actor_id="superleader:obj-a",
                scope_class=AuthorityBoundaryClass.PROTECTED_RUNTIME.value,
                reason="Root authority granted the protected task-surface write.",
                summary="Granted protected task-surface write request.",
            ),
        )

        saved_task = await store.get_task(task.task_id)
        saved_state = await store.get_delivery_state("obj-a:lane:lane-a")

        self.assertIsNotNone(saved_task)
        self.assertIsNotNone(saved_state)
        assert saved_task is not None
        assert saved_state is not None
        self.assertEqual(saved_task.status, TaskStatus.IN_PROGRESS)
        self.assertEqual(saved_task.goal, "Protected write grant updated the task goal.")
        self.assertEqual(saved_task.owned_paths, ("src/runtime.py", "docs/runtime.md"))
        self.assertEqual(saved_task.authority_decision_payload["decision"], "grant")
        self.assertIn(task.task_id, saved_state.active_task_ids)

    async def test_authority_completion_snapshot_classifies_lane_request_closure_states(self) -> None:
        store = InMemoryOrchestrationStore()
        bus = InMemoryEventBus()
        runtime = GroupRuntime(store=store, bus=bus)

        await runtime.create_group("group-a")
        await runtime.create_team(group_id="group-a", team_id="team-a", name="Runtime")
        await store.save_delivery_state(
            DeliveryState(
                delivery_id="obj-a:lane:lane-a",
                objective_id="obj-a",
                kind=DeliveryStateKind.LANE,
                status=DeliveryStatus.RUNNING,
                lane_id="lane-a",
                team_id="team-a",
            )
        )

        waiting_task = await runtime.submit_task(
            group_id="group-a",
            team_id="team-a",
            goal="Waiting authority task",
            scope=TaskScope.TEAM,
            lane_id="lane-a",
            created_by="leader-a",
        )
        waiting_request = ScopeExtensionRequest(
            request_id="auth-snapshot-waiting",
            assignment_id=f"{waiting_task.task_id}:assignment",
            worker_id="team-a:teammate:1",
            task_id=waiting_task.task_id,
            requested_paths=("docs/runtime.md",),
            reason="Need docs authority.",
        )
        await runtime.commit_authority_request(
            objective_id="obj-a",
            lane_id="lane-a",
            team_id="team-a",
            task_id=waiting_task.task_id,
            worker_id=waiting_request.worker_id,
            authority_request=waiting_request,
            record=WorkerRecord(
                worker_id=waiting_request.worker_id,
                assignment_id=waiting_request.assignment_id,
                backend="in_process",
                role="teammate",
                status=WorkerStatus.FAILED,
            ),
        )

        grant_task = await runtime.submit_task(
            group_id="group-a",
            team_id="team-a",
            goal="Grant authority task",
            scope=TaskScope.TEAM,
            lane_id="lane-a",
            created_by="leader-a",
        )
        grant_request = ScopeExtensionRequest(
            request_id="auth-snapshot-grant",
            assignment_id=f"{grant_task.task_id}:assignment",
            worker_id="team-a:teammate:2",
            task_id=grant_task.task_id,
            requested_paths=("docs/runtime-grant.md",),
            reason="Need grant authority.",
        )
        await runtime.commit_authority_request(
            objective_id="obj-a",
            lane_id="lane-a",
            team_id="team-a",
            task_id=grant_task.task_id,
            worker_id=grant_request.worker_id,
            authority_request=grant_request,
            record=WorkerRecord(
                worker_id=grant_request.worker_id,
                assignment_id=grant_request.assignment_id,
                backend="in_process",
                role="teammate",
                status=WorkerStatus.FAILED,
            ),
        )
        await runtime.commit_authority_decision(
            objective_id="obj-a",
            lane_id="lane-a",
            team_id="team-a",
            task_id=grant_task.task_id,
            actor_id="leader-a",
            authority_decision=AuthorityDecision(
                request_id=grant_request.request_id,
                decision="grant",
                actor_id="leader-a",
                scope_class="soft_scope",
                granted_paths=grant_request.requested_paths,
                summary="Granted authority.",
            ),
        )
        await store.save_agent_session(
            AgentSession(
                session_id="team-a:teammate:2:resident",
                agent_id=grant_request.worker_id,
                role="teammate",
                objective_id="obj-a",
                lane_id="lane-a",
                team_id="team-a",
                metadata={
                    "authority_request_id": grant_request.request_id,
                    "authority_waiting_task_id": grant_task.task_id,
                    "authority_last_decision": "grant",
                    "wake_request_count": 1,
                    "last_wake_request_at": "2026-04-07T00:00:00+00:00",
                },
            )
        )

        reroute_task = await runtime.submit_task(
            group_id="group-a",
            team_id="team-a",
            goal="Reroute authority task",
            scope=TaskScope.TEAM,
            lane_id="lane-a",
            created_by="leader-a",
        )
        reroute_request = ScopeExtensionRequest(
            request_id="auth-snapshot-reroute",
            assignment_id=f"{reroute_task.task_id}:assignment",
            worker_id="team-a:teammate:3",
            task_id=reroute_task.task_id,
            requested_paths=("docs/runtime-reroute.md",),
            reason="Need reroute authority.",
        )
        await runtime.commit_authority_request(
            objective_id="obj-a",
            lane_id="lane-a",
            team_id="team-a",
            task_id=reroute_task.task_id,
            worker_id=reroute_request.worker_id,
            authority_request=reroute_request,
            record=WorkerRecord(
                worker_id=reroute_request.worker_id,
                assignment_id=reroute_request.assignment_id,
                backend="in_process",
                role="teammate",
                status=WorkerStatus.FAILED,
            ),
        )
        reroute_replacement = await runtime.submit_task(
            group_id="group-a",
            team_id="team-a",
            goal="Reroute replacement",
            scope=TaskScope.TEAM,
            lane_id="lane-a",
            created_by="leader-a",
        )
        await runtime.commit_authority_decision(
            objective_id="obj-a",
            lane_id="lane-a",
            team_id="team-a",
            task_id=reroute_task.task_id,
            actor_id="leader-a",
            authority_decision=AuthorityDecision(
                request_id=reroute_request.request_id,
                decision="reroute",
                actor_id="leader-a",
                scope_class="soft_scope",
                summary="Rerouted authority.",
            ),
            replacement_task=reroute_replacement,
        )
        await store.save_agent_session(
            AgentSession(
                session_id="team-a:teammate:3:resident",
                agent_id=reroute_request.worker_id,
                role="teammate",
                objective_id="obj-a",
                lane_id="lane-a",
                team_id="team-a",
                metadata={
                    "authority_request_id": reroute_request.request_id,
                    "authority_waiting_task_id": reroute_task.task_id,
                    "authority_last_decision": "reroute",
                },
            )
        )

        deny_task = await runtime.submit_task(
            group_id="group-a",
            team_id="team-a",
            goal="Deny authority task",
            scope=TaskScope.TEAM,
            lane_id="lane-a",
            created_by="leader-a",
        )
        deny_request = ScopeExtensionRequest(
            request_id="auth-snapshot-deny",
            assignment_id=f"{deny_task.task_id}:assignment",
            worker_id="team-a:teammate:4",
            task_id=deny_task.task_id,
            requested_paths=("src/agent_orchestra/contracts/task.py",),
            reason="Need deny authority.",
        )
        await runtime.commit_authority_request(
            objective_id="obj-a",
            lane_id="lane-a",
            team_id="team-a",
            task_id=deny_task.task_id,
            worker_id=deny_request.worker_id,
            authority_request=deny_request,
            record=WorkerRecord(
                worker_id=deny_request.worker_id,
                assignment_id=deny_request.assignment_id,
                backend="in_process",
                role="teammate",
                status=WorkerStatus.FAILED,
            ),
        )
        await runtime.commit_authority_decision(
            objective_id="obj-a",
            lane_id="lane-a",
            team_id="team-a",
            task_id=deny_task.task_id,
            actor_id="leader-a",
            authority_decision=AuthorityDecision(
                request_id=deny_request.request_id,
                decision="deny",
                actor_id="leader-a",
                scope_class="global_contract",
                summary="Denied authority.",
            ),
        )
        await store.save_agent_session(
            AgentSession(
                session_id="team-a:teammate:4:resident",
                agent_id=deny_request.worker_id,
                role="teammate",
                objective_id="obj-a",
                lane_id="lane-a",
                team_id="team-a",
                metadata={
                    "authority_request_id": deny_request.request_id,
                    "authority_waiting_task_id": deny_task.task_id,
                    "authority_last_decision": "deny",
                },
            )
        )

        incomplete_task = await runtime.submit_task(
            group_id="group-a",
            team_id="team-a",
            goal="Incomplete authority task",
            scope=TaskScope.TEAM,
            lane_id="lane-a",
            created_by="leader-a",
        )
        incomplete_request = ScopeExtensionRequest(
            request_id="auth-snapshot-incomplete",
            assignment_id=f"{incomplete_task.task_id}:assignment",
            worker_id="team-a:teammate:5",
            task_id=incomplete_task.task_id,
            requested_paths=("docs/runtime-incomplete.md",),
            reason="Need incomplete authority.",
        )
        await runtime.commit_authority_request(
            objective_id="obj-a",
            lane_id="lane-a",
            team_id="team-a",
            task_id=incomplete_task.task_id,
            worker_id=incomplete_request.worker_id,
            authority_request=incomplete_request,
            record=WorkerRecord(
                worker_id=incomplete_request.worker_id,
                assignment_id=incomplete_request.assignment_id,
                backend="in_process",
                role="teammate",
                status=WorkerStatus.FAILED,
            ),
        )
        await runtime.commit_authority_decision(
            objective_id="obj-a",
            lane_id="lane-a",
            team_id="team-a",
            task_id=incomplete_task.task_id,
            actor_id="leader-a",
            authority_decision=AuthorityDecision(
                request_id=incomplete_request.request_id,
                decision="grant",
                actor_id="leader-a",
                scope_class="soft_scope",
                granted_paths=incomplete_request.requested_paths,
                summary="Granted authority but teammate did not consume relay.",
            ),
        )

        snapshot = await collect_lane_authority_completion_snapshot(
            runtime=runtime,
            objective_id="obj-a",
            lane_id="lane-a",
            team_id="team-a",
        )
        status_by_request = {
            item.request_id: item.completion_status
            for item in snapshot.requests
        }

        self.assertEqual(status_by_request[waiting_request.request_id], AuthorityCompletionStatus.WAITING)
        self.assertEqual(status_by_request[grant_request.request_id], AuthorityCompletionStatus.GRANT_RESUMED)
        self.assertEqual(status_by_request[reroute_request.request_id], AuthorityCompletionStatus.REROUTE_CLOSED)
        self.assertEqual(status_by_request[deny_request.request_id], AuthorityCompletionStatus.DENY_CLOSED)
        self.assertEqual(status_by_request[incomplete_request.request_id], AuthorityCompletionStatus.INCOMPLETE)
