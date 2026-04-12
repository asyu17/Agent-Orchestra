from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agent_orchestra.bus.in_memory import InMemoryEventBus
from agent_orchestra.contracts.agent import TeammateActivationProfile
from agent_orchestra.contracts.authority import ScopeExtensionRequest
from agent_orchestra.contracts.blackboard import BlackboardKind, BlackboardSnapshot
from agent_orchestra.contracts.delivery import DeliveryState, DeliveryStateKind, DeliveryStatus
from agent_orchestra.contracts.enums import EventKind, TaskScope, TaskStatus, WorkerStatus
from agent_orchestra.contracts.execution import (
    ResidentCoordinatorPhase,
    ResidentCoordinatorSession,
    WorkerRecord,
)
from agent_orchestra.contracts.hierarchical_review import ReviewItemKind
from agent_orchestra.contracts.runner import AgentRunner, RunnerHealth, RunnerStreamEvent, RunnerTurnRequest, RunnerTurnResult
from agent_orchestra.contracts.session_continuity import ConversationHeadKind
from agent_orchestra.contracts.session_memory import AgentTurnKind, ArtifactRefKind, ToolInvocationKind
from agent_orchestra.contracts.task import TaskSurfaceMutationKind
from agent_orchestra.contracts.task_review import TaskReviewExperienceContext, TaskReviewStance
from agent_orchestra.planning.template import ObjectiveTemplate, WorkstreamTemplate
from agent_orchestra.planning.template_planner import TemplatePlanner
from agent_orchestra.runtime.backends.in_process import InProcessLaunchBackend
from agent_orchestra.runtime.bootstrap_round import materialize_planning_result
from agent_orchestra.runtime.evaluator import DefaultDeliveryEvaluator
from agent_orchestra.runtime.group_runtime import GroupRuntime
from agent_orchestra.runtime.leader_loop import (
    LeaderLoopConfig,
    LeaderLoopSupervisor,
    build_runtime_role_profiles,
    compile_leader_turn_assignment,
)
from agent_orchestra.runtime.mailbox_bridge import InMemoryMailboxBridge
from agent_orchestra.runtime.teammate_runtime import ResidentTeammateRunResult
from agent_orchestra.runtime.teammate_work_surface import TeammateWorkSurface
from agent_orchestra.runtime.worker_supervisor import DefaultWorkerSupervisor
from agent_orchestra.storage.in_memory import InMemoryOrchestrationStore
from agent_orchestra.tools.mailbox import MailboxDeliveryMode, MailboxEnvelope, MailboxMessageKind, MailboxVisibilityScope
from agent_orchestra.tools.permission_protocol import PermissionDecision


class _ScriptedLeaderRunner(AgentRunner):
    def __init__(
        self,
        *,
        emit_initial_slice: bool = True,
    ) -> None:
        self.emit_initial_slice = emit_initial_slice
        self.requests: list[RunnerTurnRequest] = []

    async def run_turn(self, request: RunnerTurnRequest) -> RunnerTurnResult:
        self.requests.append(request)
        if request.agent_id.startswith("leader:"):
            turn_index = int(request.metadata.get("turn_index", 1))
            lane_id = request.agent_id.split("leader:", 1)[1]
            if turn_index == 1 and self.emit_initial_slice:
                output = {
                    "summary": f"{lane_id} turn 1 split the work.",
                    "sequential_slices": [
                        {
                            "slice_id": f"{lane_id}-implementation",
                            "title": f"{lane_id} implementation",
                            "goal": f"Implement {lane_id} lane work.",
                            "reason": "Need one concrete teammate task before closing the lane.",
                            "owned_paths": [f"src/{lane_id}.py"],
                            "verification_commands": ['python3 -c "print(\'runtime verification ok\')"'],
                        }
                    ],
                    "parallel_slices": [],
                }
            else:
                output = {
                    "summary": f"{lane_id} converged without new teammate work.",
                    "sequential_slices": [],
                    "parallel_slices": [],
                }
            return RunnerTurnResult(
                response_id=f"resp-{request.agent_id}-{turn_index}",
                output_text=json.dumps(output),
                status="completed",
                raw_payload={"turn_index": turn_index},
            )
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


class _ContinuityInMemoryStore(InMemoryOrchestrationStore):
    def __init__(self) -> None:
        super().__init__()
        self.work_sessions: dict[str, object] = {}
        self.runtime_generations: dict[str, object] = {}
        self.work_session_messages: dict[str, list[object]] = {}
        self.conversation_heads: dict[str, object] = {}
        self.session_events: dict[str, list[object]] = {}

    async def save_work_session(self, session: object) -> None:
        self.work_sessions[getattr(session, "work_session_id")] = session

    async def get_work_session(self, work_session_id: str) -> object | None:
        return self.work_sessions.get(work_session_id)

    async def list_work_sessions(
        self,
        group_id: str,
        *,
        root_objective_id: str | None = None,
    ) -> list[object]:
        sessions = list(self.work_sessions.values())
        sessions = [session for session in sessions if getattr(session, "group_id", None) == group_id]
        if root_objective_id is not None:
            sessions = [
                session
                for session in sessions
                if getattr(session, "root_objective_id", None) == root_objective_id
            ]
        return sessions

    async def save_runtime_generation(self, generation: object) -> None:
        self.runtime_generations[getattr(generation, "runtime_generation_id")] = generation

    async def get_runtime_generation(self, runtime_generation_id: str) -> object | None:
        return self.runtime_generations.get(runtime_generation_id)

    async def list_runtime_generations(self, work_session_id: str) -> list[object]:
        generations = [
            generation
            for generation in self.runtime_generations.values()
            if getattr(generation, "work_session_id", None) == work_session_id
        ]
        return sorted(generations, key=lambda item: getattr(item, "generation_index", 0))

    async def append_work_session_message(self, message: object) -> None:
        self.work_session_messages.setdefault(getattr(message, "work_session_id"), []).append(message)

    async def list_work_session_messages(
        self,
        work_session_id: str,
        *,
        runtime_generation_id: str | None = None,
    ) -> list[object]:
        messages = list(self.work_session_messages.get(work_session_id, ()))
        if runtime_generation_id is not None:
            messages = [
                message
                for message in messages
                if getattr(message, "runtime_generation_id", None) == runtime_generation_id
            ]
        return messages

    async def save_conversation_head(self, head: object) -> None:
        self.conversation_heads[getattr(head, "conversation_head_id")] = head

    async def get_conversation_head(self, conversation_head_id: str) -> object | None:
        return self.conversation_heads.get(conversation_head_id)

    async def list_conversation_heads(
        self,
        work_session_id: str,
        *,
        runtime_generation_id: str | None = None,
    ) -> list[object]:
        heads = [
            head
            for head in self.conversation_heads.values()
            if getattr(head, "work_session_id", None) == work_session_id
        ]
        if runtime_generation_id is not None:
            heads = [
                head
                for head in heads
                if getattr(head, "runtime_generation_id", None) == runtime_generation_id
            ]
        return heads

    async def append_session_event(self, event: object) -> None:
        self.session_events.setdefault(getattr(event, "work_session_id"), []).append(event)

    async def list_session_events(
        self,
        work_session_id: str,
        *,
        runtime_generation_id: str | None = None,
    ) -> list[object]:
        events = list(self.session_events.get(work_session_id, ()))
        if runtime_generation_id is not None:
            events = [
                event
                for event in events
                if getattr(event, "runtime_generation_id", None) == runtime_generation_id
            ]
        return events

    async def find_latest_resumable_runtime_generation(self, work_session_id: str) -> object | None:
        generations = await self.list_runtime_generations(work_session_id)
        return generations[-1] if generations else None


async def _prime_host_owned_teammate_slot(
    *,
    supervisor: DefaultWorkerSupervisor,
    objective_id: str,
    leader_round,
    working_dir: str,
) -> None:
    teammate_profile = build_runtime_role_profiles()["teammate_in_process_fast"]
    session_id = f"{leader_round.team_id}:teammate:1:resident"
    await supervisor.session_host.load_or_create_slot_session(
        session_id=session_id,
        agent_id=f"{leader_round.team_id}:teammate:1",
        objective_id=objective_id,
        lane_id=leader_round.lane_id,
        team_id=leader_round.team_id,
    )
    await supervisor.session_host.record_teammate_activation_profile(
        session_id,
        activation_profile=TeammateActivationProfile(
            backend="in_process",
            working_dir=working_dir,
            role_profile=teammate_profile,
        ),
    )


class LeaderLoopTest(IsolatedAsyncioTestCase):
    async def test_group_runtime_reuses_saved_leader_response_chain_on_next_assignment(self) -> None:
        store = _ContinuityInMemoryStore()
        bus = InMemoryEventBus()
        runner = _ScriptedLeaderRunner(emit_initial_slice=False)
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
        planner = TemplatePlanner()

        await runtime.create_group("group-a")
        planning_result = await planner.build_initial_plan(
            ObjectiveTemplate(
                objective_id="obj-session-continuity",
                group_id="group-a",
                title="Preserve leader continuity",
                description="The next leader turn should reuse the saved previous_response_id.",
                workstreams=(
                    WorkstreamTemplate(
                        workstream_id="runtime",
                        title="Runtime",
                        summary="Own the runtime lane.",
                        team_name="Runtime",
                        budget_max_teammates=1,
                        budget_max_iterations=2,
                    ),
                ),
            )
        )
        bundle = await materialize_planning_result(runtime, planning_result, created_by="superleader-1")
        leader_round = bundle.leader_rounds[0]
        continuity = await runtime.new_session(
            group_id=bundle.objective.group_id,
            objective_id=bundle.objective.objective_id,
            title="Leader continuity",
        )

        leader_lane_snapshot = BlackboardSnapshot(
            blackboard_id=f"{bundle.objective.group_id}:leader_lane:{leader_round.lane_id}",
            group_id=bundle.objective.group_id,
            kind=BlackboardKind.LEADER_LANE,
            lane_id=leader_round.lane_id,
            summary="Leader lane summary",
        )
        team_snapshot = BlackboardSnapshot(
            blackboard_id=f"{bundle.objective.group_id}:team:{leader_round.team_id}",
            group_id=bundle.objective.group_id,
            kind=BlackboardKind.TEAM,
            team_id=leader_round.team_id,
            summary="Team summary",
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            first_assignment = compile_leader_turn_assignment(
                objective=bundle.objective,
                leader_round=leader_round,
                turn_index=1,
                visible_tasks=(),
                leader_lane_snapshot=leader_lane_snapshot,
                team_snapshot=team_snapshot,
                mailbox_messages=(),
                backend="in_process",
                working_dir=tmpdir,
            )
            first_record = await runtime.run_worker_assignment(first_assignment)

            second_assignment = compile_leader_turn_assignment(
                objective=bundle.objective,
                leader_round=leader_round,
                turn_index=2,
                visible_tasks=(),
                leader_lane_snapshot=leader_lane_snapshot,
                team_snapshot=team_snapshot,
                mailbox_messages=(),
                backend="in_process",
                working_dir=tmpdir,
            )
            second_record = await runtime.run_worker_assignment(second_assignment)

        leader_requests = [request for request in runner.requests if request.agent_id.startswith("leader:")]
        self.assertEqual(len(leader_requests), 2)
        self.assertIsNone(leader_requests[0].previous_response_id)
        self.assertEqual(leader_requests[1].previous_response_id, first_record.response_id)

        heads = await store.list_conversation_heads(continuity.work_session.work_session_id)
        leader_heads = [
            head
            for head in heads
            if getattr(head, "head_kind", None) == "leader_lane"
            and getattr(head, "scope_id", None) == leader_round.lane_id
        ]
        self.assertEqual(len(leader_heads), 1)
        self.assertEqual(leader_heads[0].last_response_id, second_record.response_id)

    async def test_leader_loop_publishes_team_position_review_after_teammate_reviews_exist(self) -> None:
        store = InMemoryOrchestrationStore()
        bus = InMemoryEventBus()
        runner = _ScriptedLeaderRunner(emit_initial_slice=False)
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
        planner = TemplatePlanner()
        mailbox = InMemoryMailboxBridge()
        leader_loop = LeaderLoopSupervisor(
            runtime=runtime,
            evaluator=DefaultDeliveryEvaluator(),
            mailbox=mailbox,
        )

        await runtime.create_group("group-a")
        planning_result = await planner.build_initial_plan(
            ObjectiveTemplate(
                objective_id="obj-review",
                group_id="group-a",
                title="Publish team synthesis",
                description="Leader should synthesize teammate reviews into a team position.",
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
        teammate_task = await runtime.submit_task(
            group_id=bundle.objective.group_id,
            team_id=leader_round.team_id,
            lane_id=leader_round.lane_id,
            goal="Implement hierarchical review contracts",
            scope=TaskScope.TEAM,
            created_by=leader_round.leader_task.leader_id,
        )
        await runtime.claim_task(
            task_id=teammate_task.task_id,
            owner_id=f"{leader_round.team_id}:teammate:1",
            claim_source="test.leader_loop",
        )
        await runtime.update_task_status(
            task_id=teammate_task.task_id,
            status=TaskStatus.COMPLETED,
            actor_id=f"{leader_round.team_id}:teammate:1",
        )
        first_review = await runtime.upsert_task_review(
            task_id=teammate_task.task_id,
            reviewer_agent_id=f"{leader_round.team_id}:teammate:1",
            reviewer_role="teammate",
            based_on_task_version=1,
            based_on_knowledge_epoch=1,
            stance=TaskReviewStance.GOOD_FIT,
            summary="I implemented the contract layer.",
            relation_to_my_work="I changed the contract module.",
            confidence=0.82,
            experience_context=TaskReviewExperienceContext(
                touched_paths=("src/agent_orchestra/contracts/hierarchical_review.py",),
            ),
            reviewed_at="2026-04-07T13:00:00+00:00",
        )
        second_review = await runtime.upsert_task_review(
            task_id=teammate_task.task_id,
            reviewer_agent_id=f"{leader_round.team_id}:teammate:2",
            reviewer_role="teammate",
            based_on_task_version=1,
            based_on_knowledge_epoch=1,
            stance=TaskReviewStance.HIGH_RISK,
            summary="Need to watch store coupling.",
            relation_to_my_work="I worked on the persistence layer.",
            confidence=0.71,
            experience_context=TaskReviewExperienceContext(
                touched_paths=("src/agent_orchestra/storage/postgres/store.py",),
            ),
            reviewed_at="2026-04-07T13:01:00+00:00",
        )
        item = await runtime.create_review_item(
            objective_id=bundle.objective.objective_id,
            item_id="task-item-runtime-review",
            item_kind=ReviewItemKind.TASK_ITEM,
            team_id=leader_round.team_id,
            lane_id=leader_round.lane_id,
            source_task_id=teammate_task.task_id,
            title="Runtime task review item",
            summary="Team-scoped synthesis target.",
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            result = await leader_loop.run(
                objective=bundle.objective,
                leader_round=leader_round,
                config=LeaderLoopConfig(
                    leader_backend="in_process",
                    teammate_backend="in_process",
                    max_leader_turns=1,
                    auto_run_teammates=False,
                    working_dir=tmpdir,
                ),
            )

        reviews = await runtime.list_team_position_reviews(item.item_id, team_id=leader_round.team_id)

        self.assertEqual(result.delivery_state.status, DeliveryStatus.COMPLETED)
        self.assertEqual(len(reviews), 1)
        self.assertEqual(
            reviews[0].based_on_task_review_revision_ids,
            (first_review.latest_revision_id, second_review.latest_revision_id),
        )
        self.assertEqual(
            result.delivery_state.metadata["hierarchical_review"]["team_position_review_ids"],
            [reviews[0].position_review_id],
        )

    async def test_leader_can_publish_cross_team_review_for_project_item(self) -> None:
        store = InMemoryOrchestrationStore()
        bus = InMemoryEventBus()
        runner = _ScriptedLeaderRunner(emit_initial_slice=False)
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
        planner = TemplatePlanner()
        mailbox = InMemoryMailboxBridge()
        leader_loop = LeaderLoopSupervisor(
            runtime=runtime,
            evaluator=DefaultDeliveryEvaluator(),
            mailbox=mailbox,
        )

        await runtime.create_group("group-a")
        await runtime.create_team(group_id="group-a", team_id="team-b", name="Infra")
        planning_result = await planner.build_initial_plan(
            ObjectiveTemplate(
                objective_id="obj-cross-review",
                group_id="group-a",
                title="Cross-team review",
                description="Leader should publish cross-team review from leader-level summaries only.",
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
        item = await runtime.create_review_item(
            objective_id=bundle.objective.objective_id,
            item_id="project-item-cross-review",
            item_kind=ReviewItemKind.PROJECT_ITEM,
            title="Shared contract decision",
            summary="Project-scoped cross-team review target.",
        )
        await runtime.publish_team_position_review(
            item_id=item.item_id,
            team_id="team-b",
            leader_id="leader:team-b",
            reviewed_at="2026-04-07T13:00:00+00:00",
            team_stance="infra_concerns",
            summary="Leader summary only: infra team requests a rollout gate.",
        )
        await runtime.publish_team_position_review(
            item_id=item.item_id,
            team_id=leader_round.team_id,
            leader_id=leader_round.leader_task.leader_id,
            reviewed_at="2026-04-07T13:01:00+00:00",
            team_stance="runtime_owns_this",
            summary="Leader summary only: runtime team can implement after gate.",
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            result = await leader_loop.run(
                objective=bundle.objective,
                leader_round=leader_round,
                config=LeaderLoopConfig(
                    leader_backend="in_process",
                    teammate_backend="in_process",
                    max_leader_turns=1,
                    auto_run_teammates=False,
                    working_dir=tmpdir,
                ),
            )

        reviews = await runtime.list_cross_team_leader_reviews(
            item.item_id,
            reviewer_team_id=leader_round.team_id,
            target_team_id="team-b",
        )

        self.assertEqual(result.delivery_state.status, DeliveryStatus.COMPLETED)
        self.assertEqual(len(reviews), 1)
        self.assertIn("Leader summary only", reviews[0].what_changed_in_my_understanding)
        self.assertNotIn("raw teammate", reviews[0].what_changed_in_my_understanding.lower())
        self.assertEqual(
            result.delivery_state.metadata["hierarchical_review"]["cross_team_leader_review_ids"],
            [reviews[0].cross_review_id],
        )

    async def test_leader_loop_runs_multiple_turns_until_lane_is_completed(self) -> None:
        store = InMemoryOrchestrationStore()
        bus = InMemoryEventBus()
        runner = _ScriptedLeaderRunner()
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
        planner = TemplatePlanner()
        mailbox = InMemoryMailboxBridge()
        leader_loop = LeaderLoopSupervisor(
            runtime=runtime,
            evaluator=DefaultDeliveryEvaluator(),
            mailbox=mailbox,
        )

        await runtime.create_group("group-a")
        planning_result = await planner.build_initial_plan(
            ObjectiveTemplate(
                objective_id="obj-runtime",
                group_id="group-a",
                title="Build autonomous runtime",
                description="Complete the runtime lane autonomously.",
                workstreams=(
                    WorkstreamTemplate(
                        workstream_id="runtime",
                        title="Runtime",
                        summary="Own the runtime lane.",
                        team_name="Runtime",
                        budget_max_teammates=1,
                        budget_max_iterations=2,
                    ),
                ),
            )
        )
        bundle = await materialize_planning_result(runtime, planning_result, created_by="superleader-1")

        with tempfile.TemporaryDirectory() as tmpdir:
            result = await leader_loop.run(
                objective=bundle.objective,
                leader_round=bundle.leader_rounds[0],
                config=LeaderLoopConfig(
                    leader_backend="in_process",
                    teammate_backend="in_process",
                    max_leader_turns=2,
                    auto_run_teammates=True,
                    working_dir=tmpdir,
                ),
            )

        leader_requests = [request for request in runner.requests if request.agent_id.startswith("leader:")]
        teammate_requests = [request for request in runner.requests if ":teammate:" in request.agent_id]
        team_tasks = await store.list_tasks(
            "group-a",
            team_id=bundle.leader_rounds[0].team_id,
            lane_id=bundle.leader_rounds[0].lane_id,
        )
        saved_state = await store.get_delivery_state("obj-runtime:lane:runtime")
        mailbox_messages = await mailbox.list_for_recipient("leader:runtime")

        self.assertEqual(result.delivery_state.status, DeliveryStatus.COMPLETED)
        self.assertEqual(result.delivery_state.iteration, 2)
        self.assertEqual(len(leader_requests), 1)
        self.assertEqual(len(teammate_requests), 1)
        self.assertEqual(len(result.teammate_records), 1)
        self.assertEqual(result.teammate_records[0].status, WorkerStatus.COMPLETED)
        self.assertEqual(leader_requests[0].metadata["turn_index"], 1)
        self.assertTrue(any(task.status == TaskStatus.COMPLETED for task in team_tasks if task.scope == TaskScope.TEAM))
        self.assertIsNotNone(saved_state)
        assert saved_state is not None
        self.assertEqual(saved_state.status, DeliveryStatus.COMPLETED)
        self.assertEqual(len(mailbox_messages), 1)
        self.assertEqual(mailbox_messages[0].kind, MailboxMessageKind.TEAMMATE_RESULT)

    async def test_leader_loop_records_turn_and_mailbox_commit(self) -> None:
        store = _ContinuityInMemoryStore()
        bus = InMemoryEventBus()
        runner = _ScriptedLeaderRunner()
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
        planner = TemplatePlanner()
        mailbox = InMemoryMailboxBridge()

        await runtime.create_group("group-a")
        planning_result = await planner.build_initial_plan(
            ObjectiveTemplate(
                objective_id="obj-turn-ledger",
                group_id="group-a",
                title="Turn ledger",
                description="Capture leader turn decisions and mailbox commits.",
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
            title="Leader turn ledger",
        )
        await mailbox.send(
            MailboxEnvelope(
                envelope_id="env-leader-1",
                sender="teammate:1",
                recipient=leader_round.leader_task.leader_id,
                subject="note",
                mailbox_id=f"{bundle.objective.group_id}:leader:{leader_round.lane_id}",
                kind=MailboxMessageKind.SYSTEM,
                group_id=bundle.objective.group_id,
                lane_id=leader_round.lane_id,
                team_id=leader_round.team_id,
                summary="Mailbox ping",
                full_text_ref=None,
                source_entry_id=None,
                source_scope=None,
                visibility_scope=MailboxVisibilityScope.CONTROL_PRIVATE,
                delivery_mode=MailboxDeliveryMode.SUMMARY_PLUS_REF,
                severity="info",
                tags=(),
                payload={"note": "ping"},
                metadata={},
            )
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            leader_loop = LeaderLoopSupervisor(
                runtime=runtime,
                evaluator=DefaultDeliveryEvaluator(),
                mailbox=mailbox,
            )
            await leader_loop.run(
                objective=bundle.objective,
                leader_round=leader_round,
                config=LeaderLoopConfig(
                    max_leader_turns=1,
                    auto_run_teammates=False,
                    working_dir=tmpdir,
                ),
            )

        turn_records = await store.list_turn_records(
            continuity.work_session.work_session_id,
            runtime_generation_id=continuity.runtime_generation.runtime_generation_id,
            head_kind=ConversationHeadKind.LEADER_LANE,
            scope_id=leader_round.lane_id,
        )
        self.assertTrue(
            any(record.turn_kind == AgentTurnKind.LEADER_DECISION for record in turn_records)
        )
        tool_records = await store.list_tool_invocation_records(
            continuity.work_session.work_session_id,
            runtime_generation_id=continuity.runtime_generation.runtime_generation_id,
        )
        self.assertTrue(
            any(record.tool_kind == ToolInvocationKind.MAILBOX_COMMIT for record in tool_records)
        )
        artifact_refs = await store.list_artifact_refs(
            continuity.work_session.work_session_id,
            runtime_generation_id=continuity.runtime_generation.runtime_generation_id,
        )
        self.assertTrue(
            any(ref.artifact_kind == ArtifactRefKind.MAILBOX_SNAPSHOT for ref in artifact_refs)
        )

    async def test_leader_loop_surfaces_waiting_for_authority_without_failing_lane(self) -> None:
        store = InMemoryOrchestrationStore()
        bus = InMemoryEventBus()
        runner = _ScriptedLeaderRunner(emit_initial_slice=False)
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
        planner = TemplatePlanner()
        mailbox = InMemoryMailboxBridge()
        leader_loop = LeaderLoopSupervisor(
            runtime=runtime,
            evaluator=DefaultDeliveryEvaluator(),
            mailbox=mailbox,
        )

        await runtime.create_group("group-a")
        planning_result = await planner.build_initial_plan(
            ObjectiveTemplate(
                objective_id="obj-authority",
                group_id="group-a",
                title="Handle authority wait",
                description="Stop cleanly when a team task is waiting for authority.",
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
        waiting_task = await runtime.submit_task(
            group_id=bundle.objective.group_id,
            lane_id=leader_round.lane_id,
            team_id=leader_round.team_id,
            goal="Need authority before continuing",
            scope=TaskScope.TEAM,
            created_by=leader_round.leader_task.leader_id,
        )
        await runtime.claim_task(
            task_id=waiting_task.task_id,
            owner_id=f"{leader_round.team_id}:teammate:1",
            claim_source="test.leader_loop",
        )
        await runtime.update_task_status(
            task_id=waiting_task.task_id,
            status=TaskStatus.WAITING_FOR_AUTHORITY,
            actor_id=f"{leader_round.team_id}:teammate:1",
        )
        await store.save_delivery_state(
            DeliveryState(
                delivery_id=f"{bundle.objective.objective_id}:lane:{leader_round.lane_id}",
                objective_id=bundle.objective.objective_id,
                kind=DeliveryStateKind.LANE,
                status=DeliveryStatus.RUNNING,
                lane_id=leader_round.lane_id,
                team_id=leader_round.team_id,
                iteration=0,
                summary="Lane is running.",
            )
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            result = await leader_loop.run(
                objective=bundle.objective,
                leader_round=leader_round,
                config=LeaderLoopConfig(
                    leader_backend="in_process",
                    teammate_backend="in_process",
                    max_leader_turns=1,
                    auto_run_teammates=False,
                    working_dir=tmpdir,
                ),
            )

        saved_runtime_task = await store.get_task(leader_round.runtime_task.task_id)
        leader_requests = [request for request in runner.requests if request.agent_id.startswith("leader:")]

        self.assertEqual(result.delivery_state.status, DeliveryStatus.WAITING_FOR_AUTHORITY)
        self.assertEqual(
            result.delivery_state.metadata["waiting_for_authority_task_ids"],
            [waiting_task.task_id],
        )
        self.assertIsNotNone(saved_runtime_task)
        assert saved_runtime_task is not None
        self.assertEqual(saved_runtime_task.status, TaskStatus.WAITING_FOR_AUTHORITY)
        self.assertEqual(result.coordinator_session.phase, ResidentCoordinatorPhase.WAITING_FOR_DEPENDENCIES)
        self.assertEqual(len(leader_requests), 0)
        self.assertEqual(len(result.turns), 0)

    async def test_leader_loop_grants_soft_scope_authority_without_prompt_turn(self) -> None:
        store = InMemoryOrchestrationStore()
        bus = InMemoryEventBus()
        runner = _ScriptedLeaderRunner(emit_initial_slice=False)
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
        planner = TemplatePlanner()
        mailbox = InMemoryMailboxBridge()
        leader_loop = LeaderLoopSupervisor(
            runtime=runtime,
            evaluator=DefaultDeliveryEvaluator(),
            mailbox=mailbox,
        )

        await runtime.create_group("group-a")
        planning_result = await planner.build_initial_plan(
            ObjectiveTemplate(
                objective_id="obj-authority-grant",
                group_id="group-a",
                title="Grant soft authority",
                description="Leader should grant soft authority and resume work without a new prompt turn.",
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
        waiting_task = await runtime.submit_task(
            group_id=bundle.objective.group_id,
            lane_id=leader_round.lane_id,
            team_id=leader_round.team_id,
            goal="Need authority before continuing",
            scope=TaskScope.TEAM,
            owned_paths=("src/runtime.py",),
            created_by=leader_round.leader_task.leader_id,
        )
        request = ScopeExtensionRequest(
            request_id="auth-req-grant",
            assignment_id=f"{waiting_task.task_id}:assignment",
            worker_id=f"{leader_round.team_id}:teammate:1",
            task_id=waiting_task.task_id,
            requested_paths=("docs/runtime.md",),
            reason="Need docs path authority.",
            evidence="Update spans docs and source.",
            retry_hint="Grant docs/runtime.md.",
        )
        commit = await runtime.commit_authority_request(
            objective_id=bundle.objective.objective_id,
            lane_id=leader_round.lane_id,
            team_id=leader_round.team_id,
            task_id=waiting_task.task_id,
            worker_id=request.worker_id,
            authority_request=request,
            record=WorkerRecord(
                worker_id=request.worker_id,
                assignment_id=request.assignment_id,
                backend="in_process",
                role="teammate",
                status=WorkerStatus.FAILED,
                metadata={
                    "final_report": {
                        "assignment_id": request.assignment_id,
                        "worker_id": request.worker_id,
                        "terminal_status": "blocked",
                        "authority_request": request.to_dict(),
                    }
                },
            ),
        )
        await mailbox.send(
            MailboxEnvelope(
                sender=request.worker_id,
                recipient=leader_round.leader_task.leader_id,
                subject="authority.request",
                mailbox_id=f"{bundle.objective.group_id}:leader:{leader_round.lane_id}",
                kind=MailboxMessageKind.SYSTEM,
                group_id=bundle.objective.group_id,
                lane_id=leader_round.lane_id,
                team_id=leader_round.team_id,
                summary=commit.blackboard_entry.summary,
                full_text_ref=f"blackboard:{commit.blackboard_entry.entry_id}",
                source_entry_id=commit.blackboard_entry.entry_id,
                source_scope=commit.blackboard_entry.kind.value,
                visibility_scope=MailboxVisibilityScope.CONTROL_PRIVATE,
                delivery_mode=MailboxDeliveryMode.SUMMARY_PLUS_REF,
                payload={"task_id": waiting_task.task_id, "authority_request": request.to_dict()},
            )
        )
        await store.save_delivery_state(
            DeliveryState(
                delivery_id=f"{bundle.objective.objective_id}:lane:{leader_round.lane_id}",
                objective_id=bundle.objective.objective_id,
                kind=DeliveryStateKind.LANE,
                status=DeliveryStatus.RUNNING,
                lane_id=leader_round.lane_id,
                team_id=leader_round.team_id,
                iteration=0,
                summary="Lane is running.",
            )
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            await _prime_host_owned_teammate_slot(
                supervisor=supervisor,
                objective_id=bundle.objective.objective_id,
                leader_round=leader_round,
                working_dir=tmpdir,
            )
            result = await leader_loop.run(
                objective=bundle.objective,
                leader_round=leader_round,
                config=LeaderLoopConfig(
                    leader_backend="in_process",
                    teammate_backend="in_process",
                    max_leader_turns=1,
                    auto_run_teammates=True,
                    working_dir=tmpdir,
                ),
            )

        saved_task = await store.get_task(waiting_task.task_id)
        leader_requests = [request for request in runner.requests if request.agent_id.startswith("leader:")]
        teammate_requests = [request for request in runner.requests if ":teammate:" in request.agent_id]

        self.assertEqual(result.delivery_state.status, DeliveryStatus.COMPLETED)
        self.assertEqual(len(leader_requests), 0)
        self.assertEqual(len(teammate_requests), 1)
        self.assertIsNotNone(saved_task)
        assert saved_task is not None
        self.assertEqual(saved_task.status, TaskStatus.COMPLETED)
        self.assertIn("docs/runtime.md", saved_task.owned_paths)
        self.assertIsNotNone(result.coordinator_session)
        assert result.coordinator_session is not None
        grant_metadata = result.coordinator_session.metadata
        self.assertEqual(grant_metadata.get("authority_reactor_role"), "team")
        self.assertIsInstance(grant_metadata.get("authority_reactor_last_cycle_at"), str)
        datetime.fromisoformat(str(grant_metadata["authority_reactor_last_cycle_at"]))
        self.assertIn(
            request.request_id,
            grant_metadata.get("authority_reactor_pending_request_ids", []),
        )
        self.assertIn(
            request.request_id,
            grant_metadata.get("authority_reactor_decision_request_ids", []),
        )
        self.assertEqual(
            grant_metadata.get("authority_reactor_escalated_request_ids"),
            [],
        )
        self.assertIn(
            request.request_id,
            grant_metadata.get("authority_reactor_forwarded_request_ids", []),
        )
        self.assertEqual(
            grant_metadata.get("authority_reactor_incomplete_request_ids"),
            [],
        )

    async def test_leader_loop_resumes_open_team_work_without_fresh_leader_prompt(self) -> None:
        store = InMemoryOrchestrationStore()
        bus = InMemoryEventBus()
        runner = _ScriptedLeaderRunner(emit_initial_slice=False)
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
        planner = TemplatePlanner()
        mailbox = InMemoryMailboxBridge()
        leader_loop = LeaderLoopSupervisor(
            runtime=runtime,
            evaluator=DefaultDeliveryEvaluator(),
            mailbox=mailbox,
        )

        await runtime.create_group("group-a")
        planning_result = await planner.build_initial_plan(
            ObjectiveTemplate(
                objective_id="obj-resident-open-team-work",
                group_id="group-a",
                title="Resume resident team work",
                description="Resident lane should continue open team work without a fresh leader prompt.",
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
        open_task = await runtime.submit_task(
            group_id=bundle.objective.group_id,
            lane_id=leader_round.lane_id,
            team_id=leader_round.team_id,
            goal="Continue resident runtime implementation",
            scope=TaskScope.TEAM,
            owned_paths=("src/runtime.py",),
            created_by=leader_round.leader_task.leader_id,
        )
        await store.save_delivery_state(
            DeliveryState(
                delivery_id=f"{bundle.objective.objective_id}:lane:{leader_round.lane_id}",
                objective_id=bundle.objective.objective_id,
                kind=DeliveryStateKind.LANE,
                status=DeliveryStatus.RUNNING,
                lane_id=leader_round.lane_id,
                team_id=leader_round.team_id,
                iteration=0,
                summary="Lane is waiting for resident teammate progress.",
                pending_task_ids=(open_task.task_id,),
            )
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            await _prime_host_owned_teammate_slot(
                supervisor=supervisor,
                objective_id=bundle.objective.objective_id,
                leader_round=leader_round,
                working_dir=tmpdir,
            )
            result = await leader_loop.run(
                objective=bundle.objective,
                leader_round=leader_round,
                config=LeaderLoopConfig(
                    leader_backend="in_process",
                    teammate_backend="in_process",
                    max_leader_turns=1,
                    auto_run_teammates=True,
                    working_dir=tmpdir,
                ),
            )

        leader_requests = [request for request in runner.requests if request.agent_id.startswith("leader:")]
        teammate_requests = [request for request in runner.requests if ":teammate:" in request.agent_id]
        mailbox_messages = await mailbox.list_for_recipient(leader_round.leader_task.leader_id)
        saved_task = await store.get_task(open_task.task_id)

        self.assertEqual(len(leader_requests), 0)
        self.assertEqual(len(teammate_requests), 1)
        self.assertIn(
            result.delivery_state.status,
            {DeliveryStatus.RUNNING, DeliveryStatus.COMPLETED},
        )
        self.assertIn(
            result.coordinator_session.phase,
            {
                ResidentCoordinatorPhase.WAITING_FOR_MAILBOX,
                ResidentCoordinatorPhase.QUIESCENT,
            },
        )
        self.assertIsNotNone(saved_task)
        assert saved_task is not None
        self.assertEqual(saved_task.status, TaskStatus.COMPLETED)
        self.assertTrue(
            all(message.kind == MailboxMessageKind.TEAMMATE_RESULT for message in mailbox_messages)
        )

    async def test_leader_loop_consumes_routine_teammate_mailbox_without_fresh_leader_prompt(self) -> None:
        store = InMemoryOrchestrationStore()
        bus = InMemoryEventBus()
        runner = _ScriptedLeaderRunner(emit_initial_slice=False)
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
        planner = TemplatePlanner()
        mailbox = InMemoryMailboxBridge()
        leader_loop = LeaderLoopSupervisor(
            runtime=runtime,
            evaluator=DefaultDeliveryEvaluator(),
            mailbox=mailbox,
        )

        await runtime.create_group("group-a")
        planning_result = await planner.build_initial_plan(
            ObjectiveTemplate(
                objective_id="obj-resident-routine-mailbox",
                group_id="group-a",
                title="Consume routine teammate mailbox",
                description="Resident lane should consume routine teammate mailbox without a fresh leader prompt.",
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
        await store.save_delivery_state(
            DeliveryState(
                delivery_id=f"{bundle.objective.objective_id}:lane:{leader_round.lane_id}",
                objective_id=bundle.objective.objective_id,
                kind=DeliveryStateKind.LANE,
                status=DeliveryStatus.RUNNING,
                lane_id=leader_round.lane_id,
                team_id=leader_round.team_id,
                iteration=0,
                summary="Lane is waiting for routine teammate mailbox.",
            )
        )
        await mailbox.send(
            MailboxEnvelope(
                sender=f"{leader_round.team_id}:teammate:1",
                recipient=leader_round.leader_task.leader_id,
                subject="task.receipt",
                mailbox_id=f"{bundle.objective.group_id}:leader:{leader_round.lane_id}",
                kind=MailboxMessageKind.SYSTEM,
                group_id=bundle.objective.group_id,
                lane_id=leader_round.lane_id,
                team_id=leader_round.team_id,
                summary="Teammate acknowledged resident task progress.",
                visibility_scope=MailboxVisibilityScope.SHARED,
                delivery_mode=MailboxDeliveryMode.SUMMARY_ONLY,
                payload={"task_id": "resident-task-1"},
            )
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            result = await leader_loop.run(
                objective=bundle.objective,
                leader_round=leader_round,
                config=LeaderLoopConfig(
                    leader_backend="in_process",
                    teammate_backend="in_process",
                    max_leader_turns=1,
                    auto_run_teammates=False,
                    working_dir=tmpdir,
                ),
            )

        leader_requests = [request for request in runner.requests if request.agent_id.startswith("leader:")]
        persisted_state = await store.get_delivery_state(
            f"{bundle.objective.objective_id}:lane:{leader_round.lane_id}"
        )

        self.assertEqual(len(leader_requests), 0)
        self.assertEqual(result.delivery_state.status, DeliveryStatus.COMPLETED)
        self.assertIsNotNone(result.mailbox_cursor)
        self.assertIsNotNone(persisted_state)
        assert persisted_state is not None
        self.assertEqual(persisted_state.mailbox_cursor, result.mailbox_cursor)

    async def test_leader_loop_denies_soft_scope_authority_on_reject_policy_without_prompt_turn(self) -> None:
        store = InMemoryOrchestrationStore()
        bus = InMemoryEventBus()
        runner = _ScriptedLeaderRunner(emit_initial_slice=False)
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
        planner = TemplatePlanner()
        mailbox = InMemoryMailboxBridge()
        leader_loop = LeaderLoopSupervisor(
            runtime=runtime,
            evaluator=DefaultDeliveryEvaluator(),
            mailbox=mailbox,
        )

        await runtime.create_group("group-a")
        planning_result = await planner.build_initial_plan(
            ObjectiveTemplate(
                objective_id="obj-authority-deny-leader",
                group_id="group-a",
                title="Deny soft authority by policy",
                description="Leader should deny soft authority when routing policy rejects it.",
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
        waiting_task = await runtime.submit_task(
            group_id=bundle.objective.group_id,
            lane_id=leader_round.lane_id,
            team_id=leader_round.team_id,
            goal="Need authority before continuing",
            scope=TaskScope.TEAM,
            owned_paths=("src/runtime.py",),
            created_by=leader_round.leader_task.leader_id,
        )
        request = ScopeExtensionRequest(
            request_id="auth-req-deny",
            assignment_id=f"{waiting_task.task_id}:assignment",
            worker_id=f"{leader_round.team_id}:teammate:1",
            task_id=waiting_task.task_id,
            requested_paths=("docs/runtime.md",),
            reason="Need docs authority.",
            evidence="Documentation update is out of scope.",
            retry_hint="Need policy decision from authority contract.",
            soft_scope_policy_action="deny",
        )
        commit = await runtime.commit_authority_request(
            objective_id=bundle.objective.objective_id,
            lane_id=leader_round.lane_id,
            team_id=leader_round.team_id,
            task_id=waiting_task.task_id,
            worker_id=request.worker_id,
            authority_request=request,
            record=WorkerRecord(
                worker_id=request.worker_id,
                assignment_id=request.assignment_id,
                backend="in_process",
                role="teammate",
                status=WorkerStatus.FAILED,
                metadata={
                    "final_report": {
                        "assignment_id": request.assignment_id,
                        "worker_id": request.worker_id,
                        "terminal_status": "blocked",
                        "authority_request": request.to_dict(),
                    }
                },
            ),
        )
        await mailbox.send(
            MailboxEnvelope(
                sender=request.worker_id,
                recipient=leader_round.leader_task.leader_id,
                subject="authority.request",
                mailbox_id=f"{bundle.objective.group_id}:leader:{leader_round.lane_id}",
                kind=MailboxMessageKind.SYSTEM,
                group_id=bundle.objective.group_id,
                lane_id=leader_round.lane_id,
                team_id=leader_round.team_id,
                summary=commit.blackboard_entry.summary,
                full_text_ref=f"blackboard:{commit.blackboard_entry.entry_id}",
                source_entry_id=commit.blackboard_entry.entry_id,
                source_scope=commit.blackboard_entry.kind.value,
                visibility_scope=MailboxVisibilityScope.CONTROL_PRIVATE,
                delivery_mode=MailboxDeliveryMode.SUMMARY_PLUS_REF,
                payload={"task_id": waiting_task.task_id, "authority_request": request.to_dict()},
            )
        )
        await store.save_delivery_state(
            DeliveryState(
                delivery_id=f"{bundle.objective.objective_id}:lane:{leader_round.lane_id}",
                objective_id=bundle.objective.objective_id,
                kind=DeliveryStateKind.LANE,
                status=DeliveryStatus.RUNNING,
                lane_id=leader_round.lane_id,
                team_id=leader_round.team_id,
                iteration=0,
                summary="Lane is running.",
            )
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            result = await leader_loop.run(
                objective=bundle.objective,
                leader_round=leader_round,
                config=LeaderLoopConfig(
                    leader_backend="in_process",
                    teammate_backend="in_process",
                    max_leader_turns=1,
                    auto_run_teammates=True,
                    working_dir=tmpdir,
                ),
            )

        saved_task = await store.get_task(waiting_task.task_id)
        leader_requests = [request for request in runner.requests if request.agent_id.startswith("leader:")]
        teammate_requests = [request for request in runner.requests if ":teammate:" in request.agent_id]

        self.assertEqual(result.delivery_state.status, DeliveryStatus.BLOCKED)
        self.assertEqual(len(leader_requests), 0)
        self.assertEqual(len(teammate_requests), 0)
        self.assertIsNotNone(saved_task)
        assert saved_task is not None
        self.assertEqual(saved_task.status, TaskStatus.BLOCKED)
        self.assertEqual(saved_task.authority_decision_payload.get("decision"), "deny")
        self.assertEqual(saved_task.blocked_by, ("authority.denied", request.request_id))
        self.assertIsNotNone(result.coordinator_session)
        assert result.coordinator_session is not None
        deny_metadata = result.coordinator_session.metadata
        self.assertEqual(deny_metadata.get("authority_reactor_role"), "team")
        self.assertIsInstance(deny_metadata.get("authority_reactor_last_cycle_at"), str)
        datetime.fromisoformat(str(deny_metadata["authority_reactor_last_cycle_at"]))
        self.assertIn(
            request.request_id,
            deny_metadata.get("authority_reactor_pending_request_ids", []),
        )
        self.assertIn(
            request.request_id,
            deny_metadata.get("authority_reactor_decision_request_ids", []),
        )
        self.assertEqual(
            deny_metadata.get("authority_reactor_escalated_request_ids"),
            [],
        )
        self.assertIn(
            request.request_id,
            deny_metadata.get("authority_reactor_forwarded_request_ids", []),
        )
        self.assertEqual(
            deny_metadata.get("authority_reactor_incomplete_request_ids"),
            [],
        )

    async def test_leader_loop_reroutes_soft_scope_authority_with_replacement_task_without_prompt_turn(self) -> None:
        store = InMemoryOrchestrationStore()
        bus = InMemoryEventBus()
        runner = _ScriptedLeaderRunner(emit_initial_slice=False)
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
        planner = TemplatePlanner()
        mailbox = InMemoryMailboxBridge()
        leader_loop = LeaderLoopSupervisor(
            runtime=runtime,
            evaluator=DefaultDeliveryEvaluator(),
            mailbox=mailbox,
        )

        await runtime.create_group("group-a")
        planning_result = await planner.build_initial_plan(
            ObjectiveTemplate(
                objective_id="obj-authority-reroute-leader",
                group_id="group-a",
                title="Reroute soft authority by policy",
                description="Leader should reroute soft authority using a replacement task without a new prompt turn.",
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
        waiting_task = await runtime.submit_task(
            group_id=bundle.objective.group_id,
            lane_id=leader_round.lane_id,
            team_id=leader_round.team_id,
            goal="Need authority before continuing",
            scope=TaskScope.TEAM,
            owned_paths=("src/runtime.py",),
            created_by=leader_round.leader_task.leader_id,
        )
        request = ScopeExtensionRequest(
            request_id="auth-req-reroute",
            assignment_id=f"{waiting_task.task_id}:assignment",
            worker_id=f"{leader_round.team_id}:teammate:1",
            task_id=waiting_task.task_id,
            requested_paths=("docs/runtime.md",),
            reason="Need docs authority.",
            evidence="Repair should be delegated to a dedicated teammate task.",
            retry_hint="Need policy decision from authority contract.",
            soft_scope_policy_action="reroute",
        )
        commit = await runtime.commit_authority_request(
            objective_id=bundle.objective.objective_id,
            lane_id=leader_round.lane_id,
            team_id=leader_round.team_id,
            task_id=waiting_task.task_id,
            worker_id=request.worker_id,
            authority_request=request,
            record=WorkerRecord(
                worker_id=request.worker_id,
                assignment_id=request.assignment_id,
                backend="in_process",
                role="teammate",
                status=WorkerStatus.FAILED,
                metadata={
                    "final_report": {
                        "assignment_id": request.assignment_id,
                        "worker_id": request.worker_id,
                        "terminal_status": "blocked",
                        "authority_request": request.to_dict(),
                    }
                },
            ),
        )
        await mailbox.send(
            MailboxEnvelope(
                sender=request.worker_id,
                recipient=leader_round.leader_task.leader_id,
                subject="authority.request",
                mailbox_id=f"{bundle.objective.group_id}:leader:{leader_round.lane_id}",
                kind=MailboxMessageKind.SYSTEM,
                group_id=bundle.objective.group_id,
                lane_id=leader_round.lane_id,
                team_id=leader_round.team_id,
                summary=commit.blackboard_entry.summary,
                full_text_ref=f"blackboard:{commit.blackboard_entry.entry_id}",
                source_entry_id=commit.blackboard_entry.entry_id,
                source_scope=commit.blackboard_entry.kind.value,
                visibility_scope=MailboxVisibilityScope.CONTROL_PRIVATE,
                delivery_mode=MailboxDeliveryMode.SUMMARY_PLUS_REF,
                payload={"task_id": waiting_task.task_id, "authority_request": request.to_dict()},
            )
        )
        await store.save_delivery_state(
            DeliveryState(
                delivery_id=f"{bundle.objective.objective_id}:lane:{leader_round.lane_id}",
                objective_id=bundle.objective.objective_id,
                kind=DeliveryStateKind.LANE,
                status=DeliveryStatus.RUNNING,
                lane_id=leader_round.lane_id,
                team_id=leader_round.team_id,
                iteration=0,
                summary="Lane is running.",
            )
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            await _prime_host_owned_teammate_slot(
                supervisor=supervisor,
                objective_id=bundle.objective.objective_id,
                leader_round=leader_round,
                working_dir=tmpdir,
            )
            result = await leader_loop.run(
                objective=bundle.objective,
                leader_round=leader_round,
                config=LeaderLoopConfig(
                    leader_backend="in_process",
                    teammate_backend="in_process",
                    max_leader_turns=1,
                    auto_run_teammates=True,
                    working_dir=tmpdir,
                ),
            )

        saved_task = await store.get_task(waiting_task.task_id)
        team_tasks = await store.list_tasks(
            bundle.objective.group_id,
            team_id=leader_round.team_id,
            lane_id=leader_round.lane_id,
        )
        reroute_replacement_task_ids = [
            task.task_id
            for task in team_tasks
            if task.derived_from == waiting_task.task_id
        ]
        leader_requests = [request for request in runner.requests if request.agent_id.startswith("leader:")]
        teammate_requests = [request for request in runner.requests if ":teammate:" in request.agent_id]

        self.assertEqual(result.delivery_state.status, DeliveryStatus.COMPLETED)
        self.assertEqual(len(leader_requests), 0)
        self.assertGreaterEqual(len(teammate_requests), 1)
        self.assertIsNotNone(saved_task)
        assert saved_task is not None
        self.assertEqual(saved_task.status, TaskStatus.CANCELLED)
        self.assertEqual(saved_task.authority_decision_payload.get("decision"), "reroute")
        self.assertEqual(len(reroute_replacement_task_ids), 1)
        self.assertEqual(saved_task.superseded_by_task_id, reroute_replacement_task_ids[0])
        self.assertIn(
            reroute_replacement_task_ids[0],
            [request.metadata.get("task_id") for request in teammate_requests],
        )
        self.assertIsNotNone(result.coordinator_session)
        assert result.coordinator_session is not None
        reroute_metadata = result.coordinator_session.metadata
        self.assertEqual(reroute_metadata.get("authority_reactor_role"), "team")
        self.assertIsInstance(reroute_metadata.get("authority_reactor_last_cycle_at"), str)
        datetime.fromisoformat(str(reroute_metadata["authority_reactor_last_cycle_at"]))
        self.assertIn(
            request.request_id,
            reroute_metadata.get("authority_reactor_pending_request_ids", []),
        )
        self.assertIn(
            request.request_id,
            reroute_metadata.get("authority_reactor_decision_request_ids", []),
        )
        self.assertEqual(
            reroute_metadata.get("authority_reactor_escalated_request_ids"),
            [],
        )
        self.assertIn(
            request.request_id,
            reroute_metadata.get("authority_reactor_forwarded_request_ids", []),
        )
        self.assertEqual(
            reroute_metadata.get("authority_reactor_incomplete_request_ids"),
            [],
        )

    async def test_leader_loop_escalates_protected_authority_and_surfaces_reactor_metadata(self) -> None:
        store = InMemoryOrchestrationStore()
        bus = InMemoryEventBus()
        runner = _ScriptedLeaderRunner(emit_initial_slice=False)
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
        planner = TemplatePlanner()
        mailbox = InMemoryMailboxBridge()
        leader_loop = LeaderLoopSupervisor(
            runtime=runtime,
            evaluator=DefaultDeliveryEvaluator(),
            mailbox=mailbox,
        )

        await runtime.create_group("group-a")
        planning_result = await planner.build_initial_plan(
            ObjectiveTemplate(
                objective_id="obj-authority-escalate-leader",
                group_id="group-a",
                title="Escalate protected authority by policy",
                description="Leader should escalate protected authority to superleader without a new prompt turn.",
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
        waiting_task = await runtime.submit_task(
            group_id=bundle.objective.group_id,
            lane_id=leader_round.lane_id,
            team_id=leader_round.team_id,
            goal="Need authority before continuing",
            scope=TaskScope.TEAM,
            owned_paths=("src/runtime.py",),
            created_by=leader_round.leader_task.leader_id,
        )
        request = ScopeExtensionRequest(
            request_id="auth-req-escalate",
            assignment_id=f"{waiting_task.task_id}:assignment",
            worker_id=f"{leader_round.team_id}:teammate:1",
            task_id=waiting_task.task_id,
            requested_paths=("src/agent_orchestra/contracts/task.py",),
            reason="Need protected contract authority.",
            evidence="Contract edit exceeds leader scope.",
            retry_hint="Escalate to superleader.",
        )
        commit = await runtime.commit_authority_request(
            objective_id=bundle.objective.objective_id,
            lane_id=leader_round.lane_id,
            team_id=leader_round.team_id,
            task_id=waiting_task.task_id,
            worker_id=request.worker_id,
            authority_request=request,
            record=WorkerRecord(
                worker_id=request.worker_id,
                assignment_id=request.assignment_id,
                backend="in_process",
                role="teammate",
                status=WorkerStatus.FAILED,
                metadata={
                    "final_report": {
                        "assignment_id": request.assignment_id,
                        "worker_id": request.worker_id,
                        "terminal_status": "blocked",
                        "authority_request": request.to_dict(),
                    }
                },
            ),
        )
        await mailbox.send(
            MailboxEnvelope(
                sender=request.worker_id,
                recipient=leader_round.leader_task.leader_id,
                subject="authority.request",
                mailbox_id=f"{bundle.objective.group_id}:leader:{leader_round.lane_id}",
                kind=MailboxMessageKind.SYSTEM,
                group_id=bundle.objective.group_id,
                lane_id=leader_round.lane_id,
                team_id=leader_round.team_id,
                summary=commit.blackboard_entry.summary,
                full_text_ref=f"blackboard:{commit.blackboard_entry.entry_id}",
                source_entry_id=commit.blackboard_entry.entry_id,
                source_scope=commit.blackboard_entry.kind.value,
                visibility_scope=MailboxVisibilityScope.CONTROL_PRIVATE,
                delivery_mode=MailboxDeliveryMode.SUMMARY_PLUS_REF,
                payload={"task_id": waiting_task.task_id, "authority_request": request.to_dict()},
            )
        )
        await store.save_delivery_state(
            DeliveryState(
                delivery_id=f"{bundle.objective.objective_id}:lane:{leader_round.lane_id}",
                objective_id=bundle.objective.objective_id,
                kind=DeliveryStateKind.LANE,
                status=DeliveryStatus.RUNNING,
                lane_id=leader_round.lane_id,
                team_id=leader_round.team_id,
                iteration=0,
                summary="Lane is running.",
            )
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            result = await leader_loop.run(
                objective=bundle.objective,
                leader_round=leader_round,
                config=LeaderLoopConfig(
                    leader_backend="in_process",
                    teammate_backend="in_process",
                    max_leader_turns=1,
                    auto_run_teammates=False,
                    working_dir=tmpdir,
                ),
            )

        saved_task = await store.get_task(waiting_task.task_id)
        escalated_messages = await mailbox.list_for_recipient(
            f"superleader:{bundle.objective.objective_id}",
        )
        escalated_subjects = [message.subject for message in escalated_messages]
        leader_requests = [request for request in runner.requests if request.agent_id.startswith("leader:")]
        teammate_requests = [request for request in runner.requests if ":teammate:" in request.agent_id]

        self.assertEqual(result.delivery_state.status, DeliveryStatus.WAITING_FOR_AUTHORITY)
        self.assertEqual(len(leader_requests), 0)
        self.assertEqual(len(teammate_requests), 0)
        self.assertIn("authority.escalated", escalated_subjects)
        self.assertIsNotNone(saved_task)
        assert saved_task is not None
        self.assertEqual(saved_task.status, TaskStatus.WAITING_FOR_AUTHORITY)
        self.assertEqual(saved_task.authority_decision_payload.get("decision"), "escalate")
        self.assertIsNotNone(result.coordinator_session)
        assert result.coordinator_session is not None
        escalate_metadata = result.coordinator_session.metadata
        self.assertEqual(escalate_metadata.get("authority_reactor_role"), "team")
        self.assertIsInstance(escalate_metadata.get("authority_reactor_last_cycle_at"), str)
        datetime.fromisoformat(str(escalate_metadata["authority_reactor_last_cycle_at"]))
        self.assertIn(
            request.request_id,
            escalate_metadata.get("authority_reactor_pending_request_ids", []),
        )
        self.assertIn(
            request.request_id,
            escalate_metadata.get("authority_reactor_decision_request_ids", []),
        )
        self.assertIn(
            request.request_id,
            escalate_metadata.get("authority_reactor_escalated_request_ids", []),
        )
        self.assertEqual(
            escalate_metadata.get("authority_reactor_forwarded_request_ids"),
            [],
        )
        self.assertEqual(
            escalate_metadata.get("authority_reactor_incomplete_request_ids"),
            [],
        )

    async def test_leader_loop_grants_task_surface_mutation_authority_and_applies_mutation(self) -> None:
        store = InMemoryOrchestrationStore()
        bus = InMemoryEventBus()
        runner = _ScriptedLeaderRunner(emit_initial_slice=False)
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
        planner = TemplatePlanner()
        mailbox = InMemoryMailboxBridge()
        leader_loop = LeaderLoopSupervisor(
            runtime=runtime,
            evaluator=DefaultDeliveryEvaluator(),
            mailbox=mailbox,
        )

        await runtime.create_group("group-a")
        planning_result = await planner.build_initial_plan(
            ObjectiveTemplate(
                objective_id="obj-task-surface-authority-grant-leader",
                group_id="group-a",
                title="Grant task-surface mutation authority",
                description="Leader should grant soft-scope task-surface mutation authority without a prompt turn.",
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
        teammate_id = f"{leader_round.team_id}:teammate:1"
        task = await runtime.submit_task(
            group_id=bundle.objective.group_id,
            lane_id=leader_round.lane_id,
            team_id=leader_round.team_id,
            goal="Need leader approval for a cross-subtree mutation",
            scope=TaskScope.TEAM,
            owned_paths=("src/runtime.py",),
            created_by=leader_round.leader_task.leader_id,
        )
        await runtime.claim_task(
            task_id=task.task_id,
            owner_id=teammate_id,
            claim_source="test.leader_loop",
        )
        await store.save_delivery_state(
            DeliveryState(
                delivery_id=f"{bundle.objective.objective_id}:lane:{leader_round.lane_id}",
                objective_id=bundle.objective.objective_id,
                kind=DeliveryStateKind.LANE,
                status=DeliveryStatus.RUNNING,
                lane_id=leader_round.lane_id,
                team_id=leader_round.team_id,
                iteration=0,
                summary="Lane is running.",
                pending_task_ids=(task.task_id,),
                active_task_ids=(task.task_id,),
            )
        )
        commit = await runtime.commit_task_surface_mutation(
            objective_id=bundle.objective.objective_id,
            lane_id=leader_round.lane_id,
            team_id=leader_round.team_id,
            task_id=task.task_id,
            actor_id=teammate_id,
            mutation_kind=TaskSurfaceMutationKind.NOT_NEEDED,
            reason="This leader-owned task should be closed through authority instead of a direct teammate mutation.",
        )
        assert commit.authority_request is not None
        await mailbox.send(
            MailboxEnvelope(
                sender=teammate_id,
                recipient=leader_round.leader_task.leader_id,
                subject="authority.request",
                mailbox_id=f"{bundle.objective.group_id}:leader:{leader_round.lane_id}",
                kind=MailboxMessageKind.SYSTEM,
                group_id=bundle.objective.group_id,
                lane_id=leader_round.lane_id,
                team_id=leader_round.team_id,
                summary=commit.blackboard_entry.summary,
                full_text_ref=f"blackboard:{commit.blackboard_entry.entry_id}",
                source_entry_id=commit.blackboard_entry.entry_id,
                source_scope=commit.blackboard_entry.kind.value,
                visibility_scope=MailboxVisibilityScope.CONTROL_PRIVATE,
                delivery_mode=MailboxDeliveryMode.SUMMARY_PLUS_REF,
                payload={
                    "task_id": task.task_id,
                    "authority_request": commit.authority_request.to_dict(),
                },
            )
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            result = await leader_loop.run(
                objective=bundle.objective,
                leader_round=leader_round,
                config=LeaderLoopConfig(
                    leader_backend="in_process",
                    teammate_backend="in_process",
                    max_leader_turns=1,
                    auto_run_teammates=False,
                    working_dir=tmpdir,
                ),
            )

        saved_task = await store.get_task(task.task_id)
        teammate_requests = [request for request in runner.requests if ":teammate:" in request.agent_id]

        self.assertEqual(len(teammate_requests), 0)
        self.assertNotEqual(result.delivery_state.status, DeliveryStatus.WAITING_FOR_AUTHORITY)
        self.assertIsNotNone(saved_task)
        assert saved_task is not None
        self.assertEqual(saved_task.status, TaskStatus.CANCELLED)
        self.assertEqual(saved_task.authority_decision_payload.get("decision"), "grant")
        self.assertEqual(saved_task.surface_mutation.kind, TaskSurfaceMutationKind.NOT_NEEDED)
        self.assertEqual(saved_task.surface_mutation.actor_id, teammate_id)

    async def test_leader_loop_can_seed_first_turn_output_without_leader_prompt_execution(self) -> None:
        store = InMemoryOrchestrationStore()
        bus = InMemoryEventBus()
        runner = _ScriptedLeaderRunner()
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
        planner = TemplatePlanner()
        mailbox = InMemoryMailboxBridge()
        leader_loop = LeaderLoopSupervisor(
            runtime=runtime,
            evaluator=DefaultDeliveryEvaluator(),
            mailbox=mailbox,
        )

        await runtime.create_group("group-a")
        planning_result = await planner.build_initial_plan(
            ObjectiveTemplate(
                objective_id="obj-seeded-turn",
                group_id="group-a",
                title="Seeded first turn",
                description="Use a precomputed revised plan to seed lane activation.",
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
        seed_payload = {
            "summary": "Use revised seed plan for activation.",
            "sequential_slices": [
                {
                    "slice_id": "runtime-seeded-slice",
                    "title": "Seeded runtime implementation",
                    "goal": "Implement seeded runtime slice.",
                    "reason": "Seeded from revised planning round.",
                }
            ],
            "parallel_slices": [],
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            result = await leader_loop.run(
                objective=bundle.objective,
                leader_round=leader_round,
                config=LeaderLoopConfig(
                    leader_backend="in_process",
                    teammate_backend="in_process",
                    max_leader_turns=1,
                    auto_run_teammates=True,
                    seed_leader_turn_output=seed_payload,
                    skip_initial_prompt_turn_when_seeded=True,
                    working_dir=tmpdir,
                ),
            )

        leader_requests = [request for request in runner.requests if request.agent_id.startswith("leader:")]
        teammate_requests = [request for request in runner.requests if ":teammate:" in request.agent_id]

        self.assertEqual(result.delivery_state.status, DeliveryStatus.COMPLETED)
        self.assertEqual(len(leader_requests), 0)
        self.assertEqual(len(teammate_requests), 1)
        self.assertEqual(len(result.leader_records), 1)
        self.assertEqual(result.leader_records[0].backend, "planning_seed")
        self.assertTrue(result.leader_records[0].metadata.get("seeded_from_revised_plan"))

    async def test_ensure_or_step_teammates_uses_thin_host_surface_step_without_leader_owned_preprime(
        self,
    ) -> None:
        store = InMemoryOrchestrationStore()
        bus = InMemoryEventBus()
        runner = _ScriptedLeaderRunner(emit_initial_slice=False)
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
        planner = TemplatePlanner()
        mailbox = InMemoryMailboxBridge()
        leader_loop = LeaderLoopSupervisor(
            runtime=runtime,
            evaluator=DefaultDeliveryEvaluator(),
            mailbox=mailbox,
        )

        await runtime.create_group("group-a")
        planning_result = await planner.build_initial_plan(
            ObjectiveTemplate(
                objective_id="obj-thin-host-step",
                group_id="group-a",
                title="Collapse teammate activation",
                description="Leader loop should delegate teammate stepping without a leader-owned pre-prime shell.",
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
        observed: dict[str, object] = {}

        async def _forbid_preprime(
            self,
            existing_assignments,
            limit,
        ):
            raise AssertionError(
                "leader_loop should not acquire teammate assignments outside the host-owned surface step"
            )

        async def _record_host_step(
            self,
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
                "backend": self.backend,
                "working_dir": self.working_dir,
                "turn_index": self.turn_index,
                "role_profile_id": (
                    self.role_profile.profile_id
                    if self.role_profile is not None
                    else None
                ),
            }
            return ResidentTeammateRunResult()

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(
                TeammateWorkSurface,
                "acquire_assignments",
                new=_forbid_preprime,
            ), patch.object(
                TeammateWorkSurface,
                "ensure_or_step_sessions",
                new=_record_host_step,
            ):
                result = await leader_loop._ensure_or_step_teammates(
                    objective=bundle.objective,
                    leader_round=leader_round,
                    assignments=(),
                    keep_session_idle=False,
                    execution_policy=None,
                    backend="in_process",
                    working_dir=tmpdir,
                    turn_index=1,
                    role_profile=teammate_profile,
                )

        self.assertEqual(result, ResidentTeammateRunResult())
        self.assertIn("call", observed)
        self.assertEqual(observed["call"]["assignments"], ())
        self.assertIs(observed["call"]["resident_kernel"], leader_loop.resident_kernel)
        self.assertFalse(observed["call"]["keep_session_idle"])
        self.assertIsNone(observed["call"]["execution_policy"])
        self.assertIsNone(observed["call"]["backend"])
        self.assertIsNone(observed["call"]["working_dir"])
        self.assertEqual(observed["call"]["turn_index"], 1)
        self.assertIsNone(observed["call"]["role_profile_id"])

    async def test_ensure_or_step_session_resumes_host_projection_and_limits_each_host_step(
        self,
    ) -> None:
        store = InMemoryOrchestrationStore()
        bus = InMemoryEventBus()
        runner = _ScriptedLeaderRunner()
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
        planner = TemplatePlanner()
        mailbox = InMemoryMailboxBridge()
        leader_loop = LeaderLoopSupervisor(
            runtime=runtime,
            evaluator=DefaultDeliveryEvaluator(),
            mailbox=mailbox,
        )

        await runtime.create_group("group-a")
        planning_result = await planner.build_initial_plan(
            ObjectiveTemplate(
                objective_id="obj-host-stepped-leader-runtime",
                group_id="group-a",
                title="Host-stepped leader runtime",
                description="Resume an existing leader host session one cycle at a time.",
                workstreams=(
                    WorkstreamTemplate(
                        workstream_id="runtime",
                        title="Runtime",
                        summary="Own the runtime lane.",
                        team_name="Runtime",
                        budget_max_teammates=1,
                        budget_max_iterations=4,
                    ),
                ),
            )
        )
        bundle = await materialize_planning_result(
            runtime,
            planning_result,
            created_by="leader-host-step-test",
        )
        leader_round = bundle.leader_rounds[0]
        session_id = (
            f"{bundle.objective.objective_id}:lane:{leader_round.lane_id}:leader:resident"
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            await _prime_host_owned_teammate_slot(
                supervisor=supervisor,
                objective_id=bundle.objective.objective_id,
                leader_round=leader_round,
                working_dir=tmpdir,
            )
            await supervisor.session_host.load_or_create_coordinator_session(
                session_id=session_id,
                coordinator_id=leader_round.leader_task.leader_id,
                objective_id=bundle.objective.objective_id,
                lane_id=leader_round.lane_id,
                team_id=leader_round.team_id,
                role="leader",
                phase=ResidentCoordinatorPhase.WAITING_FOR_MAILBOX,
                host_owner_coordinator_id=f"superleader:{bundle.objective.objective_id}",
                runtime_task_id=leader_round.runtime_task.task_id,
                metadata={
                    "runtime_view": "leader_lane_session_graph",
                    "launch_mode": "leader_session_host",
                },
            )
            await supervisor.session_host.record_coordinator_session_state(
                session_id,
                coordinator_session=ResidentCoordinatorSession(
                    coordinator_id=leader_round.leader_task.leader_id,
                    role="leader",
                    phase=ResidentCoordinatorPhase.WAITING_FOR_MAILBOX,
                    objective_id=bundle.objective.objective_id,
                    lane_id=leader_round.lane_id,
                    team_id=leader_round.team_id,
                    cycle_count=3,
                    prompt_turn_count=2,
                    claimed_task_count=1,
                    subordinate_dispatch_count=1,
                    mailbox_poll_count=4,
                    mailbox_cursor="leader-envelope-1",
                    last_reason="Resume the existing host-owned runtime lane.",
                    metadata={
                        "runtime_view": "leader_lane_session_graph",
                        "launch_mode": "leader_session_host",
                    },
                ),
                host_owner_coordinator_id=f"superleader:{bundle.objective.objective_id}",
                runtime_task_id=leader_round.runtime_task.task_id,
                metadata={
                    "runtime_view": "leader_lane_session_graph",
                    "launch_mode": "leader_session_host",
                },
            )

            result = await leader_loop.ensure_or_step_session(
                objective=bundle.objective,
                leader_round=leader_round,
                config=LeaderLoopConfig(
                    leader_backend="in_process",
                    teammate_backend="in_process",
                    max_leader_turns=4,
                    auto_run_teammates=True,
                    working_dir=tmpdir,
                ),
                host_owner_coordinator_id=f"superleader:{bundle.objective.objective_id}",
                runtime_task_id=leader_round.runtime_task.task_id,
            )

        self.assertEqual(result.delivery_state.status, DeliveryStatus.RUNNING)
        self.assertIsNotNone(result.coordinator_session)
        assert result.coordinator_session is not None
        self.assertEqual(
            result.coordinator_session.phase,
            ResidentCoordinatorPhase.WAITING_FOR_MAILBOX,
        )
        self.assertEqual(result.coordinator_session.cycle_count, 4)
        self.assertEqual(result.coordinator_session.prompt_turn_count, 3)
        self.assertEqual(
            result.coordinator_session.metadata["launch_mode"],
            "leader_session_host",
        )
        self.assertEqual(
            result.coordinator_session.metadata["runtime_view"],
            "leader_lane_session_graph",
        )

    async def test_ensure_or_step_session_quiesces_when_idle_wait_approval_denied(self) -> None:
        store = InMemoryOrchestrationStore()
        bus = InMemoryEventBus()
        runner = _ScriptedLeaderRunner()
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
        planner = TemplatePlanner()
        mailbox = InMemoryMailboxBridge()

        class _DenyIdleWaitBroker:
            async def request_decision(self, request):
                if request.action == "resident.idle_wait":
                    return PermissionDecision(
                        approved=False,
                        reviewer="policy.test",
                        reason="Resident idle wait denied.",
                    )
                return PermissionDecision(
                    approved=True,
                    reviewer="system.auto",
                    reason="Automatically approved.",
                )

        leader_loop = LeaderLoopSupervisor(
            runtime=runtime,
            evaluator=DefaultDeliveryEvaluator(),
            mailbox=mailbox,
            permission_broker=_DenyIdleWaitBroker(),
        )

        await runtime.create_group("group-a")
        planning_result = await planner.build_initial_plan(
            ObjectiveTemplate(
                objective_id="obj-host-stepped-leader-runtime",
                group_id="group-a",
                title="Leader idle wait denied",
                description="Denying leader idle-wait approval should drop the session to quiescent.",
                workstreams=(
                    WorkstreamTemplate(
                        workstream_id="runtime",
                        title="Runtime",
                        summary="Own the runtime lane.",
                        team_name="Runtime",
                        budget_max_teammates=1,
                        budget_max_iterations=4,
                    ),
                ),
            )
        )
        bundle = await materialize_planning_result(
            runtime,
            planning_result,
            created_by="leader-host-step-deny-test",
        )
        leader_round = bundle.leader_rounds[0]
        session_id = (
            f"{bundle.objective.objective_id}:lane:{leader_round.lane_id}:leader:resident"
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            await _prime_host_owned_teammate_slot(
                supervisor=supervisor,
                objective_id=bundle.objective.objective_id,
                leader_round=leader_round,
                working_dir=tmpdir,
            )
            await supervisor.session_host.load_or_create_coordinator_session(
                session_id=session_id,
                coordinator_id=leader_round.leader_task.leader_id,
                objective_id=bundle.objective.objective_id,
                lane_id=leader_round.lane_id,
                team_id=leader_round.team_id,
                role="leader",
                phase=ResidentCoordinatorPhase.WAITING_FOR_MAILBOX,
                host_owner_coordinator_id=f"superleader:{bundle.objective.objective_id}",
                runtime_task_id=leader_round.runtime_task.task_id,
                metadata={
                    "runtime_view": "leader_lane_session_graph",
                    "launch_mode": "leader_session_host",
                },
            )
            await supervisor.session_host.record_coordinator_session_state(
                session_id,
                coordinator_session=ResidentCoordinatorSession(
                    coordinator_id=leader_round.leader_task.leader_id,
                    role="leader",
                    phase=ResidentCoordinatorPhase.WAITING_FOR_MAILBOX,
                    objective_id=bundle.objective.objective_id,
                    lane_id=leader_round.lane_id,
                    team_id=leader_round.team_id,
                    cycle_count=3,
                    prompt_turn_count=2,
                    claimed_task_count=1,
                    subordinate_dispatch_count=1,
                    mailbox_poll_count=4,
                    mailbox_cursor="leader-envelope-1",
                    last_reason="Resume the existing host-owned runtime lane.",
                    metadata={
                        "runtime_view": "leader_lane_session_graph",
                        "launch_mode": "leader_session_host",
                    },
                ),
                host_owner_coordinator_id=f"superleader:{bundle.objective.objective_id}",
                runtime_task_id=leader_round.runtime_task.task_id,
                metadata={
                    "runtime_view": "leader_lane_session_graph",
                    "launch_mode": "leader_session_host",
                },
            )

            result = await leader_loop.ensure_or_step_session(
                objective=bundle.objective,
                leader_round=leader_round,
                config=LeaderLoopConfig(
                    leader_backend="in_process",
                    teammate_backend="in_process",
                    max_leader_turns=4,
                    auto_run_teammates=True,
                    keep_leader_session_idle=True,
                    working_dir=tmpdir,
                ),
                host_owner_coordinator_id=f"superleader:{bundle.objective.objective_id}",
                runtime_task_id=leader_round.runtime_task.task_id,
            )

        self.assertEqual(result.coordinator_session.phase, ResidentCoordinatorPhase.QUIESCENT)
        attach_view = await supervisor.session_host.build_shell_attach_view(
            objective_id=bundle.objective.objective_id,
            lane_id=leader_round.lane_id,
            team_id=leader_round.team_id,
        )
        self.assertEqual(attach_view["approval_queue"]["idle_wait"]["status"], "denied")
        self.assertEqual(attach_view["attach_state"]["idle_wait_approval_status"], "denied")
        persisted = await supervisor.session_host.load_coordinator_session(session_id)
        self.assertIsNotNone(persisted)
        assert persisted is not None
        self.assertEqual(persisted.cycle_count, 4)
        self.assertEqual(persisted.prompt_turn_count, 3)
        self.assertEqual(persisted.mailbox_poll_count, 5)
