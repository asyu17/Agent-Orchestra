from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agent_orchestra.bus.in_memory import InMemoryEventBus
from agent_orchestra.contracts.agent import SessionBinding
from agent_orchestra.contracts.authority import (
    AuthorityBoundaryClass,
    AuthorityDecision,
    AuthorityPolicy,
    AuthorityPolicyAction,
    ScopeExtensionRequest,
)
from agent_orchestra.contracts.delivery import DeliveryState, DeliveryStateKind, DeliveryStatus
from agent_orchestra.contracts.enums import AuthorityStatus, EventKind, TaskScope, TaskStatus, WorkerStatus
from agent_orchestra.contracts.execution import ResidentCoordinatorSession, WorkerRecord
from agent_orchestra.contracts.hierarchical_review import ReviewItemKind
from agent_orchestra.contracts.session_continuity import ConversationHeadKind, ShellAttachDecisionMode
from agent_orchestra.contracts.session_memory import AgentTurnKind, ArtifactRefKind
from agent_orchestra.contracts.runner import AgentRunner, RunnerHealth, RunnerStreamEvent, RunnerTurnRequest, RunnerTurnResult
from agent_orchestra.contracts.task import TaskSurfaceMutationKind
from agent_orchestra.contracts.worker_protocol import WorkerRoleProfile
from agent_orchestra.planning.dynamic_superleader import DynamicPlanningConfig, DynamicSuperLeaderPlanner
from agent_orchestra.planning.template import ObjectiveTemplate, WorkstreamTemplate
from agent_orchestra.planning.template_planner import TemplatePlanner
from agent_orchestra.runtime.backends.in_process import InProcessLaunchBackend
from agent_orchestra.runtime.bootstrap_round import materialize_planning_result
from agent_orchestra.runtime.evaluator import DefaultDeliveryEvaluator
from agent_orchestra.runtime.leader_loop import LeaderLoopResult, LeaderLoopSupervisor, build_runtime_role_profiles
from agent_orchestra.runtime.mailbox_bridge import InMemoryMailboxBridge
from agent_orchestra.runtime.protocol_bridge import InMemoryMailboxBridge as ProtocolInMemoryMailboxBridge
from agent_orchestra.runtime.group_runtime import GroupRuntime, ResidentLaneLiveView
from agent_orchestra.runtime.resident_kernel import ResidentCoordinatorCycleResult, ResidentCoordinatorKernel
from agent_orchestra.runtime.superleader import (
    SuperLeaderConfig,
    SuperLeaderCoordinationState,
    SuperLeaderLaneCoordinationState,
    SuperLeaderResidentLiveView,
    SuperLeaderRuntime,
    _resident_live_view_metadata,
)
from agent_orchestra.runtime.worker_supervisor import DefaultWorkerSupervisor
from agent_orchestra.storage.in_memory import InMemoryOrchestrationStore
from agent_orchestra.tools.mailbox import (
    MailboxDeliveryMode,
    MailboxDigest,
    MailboxEnvelope,
    MailboxMessageKind,
    MailboxVisibilityScope,
)
from agent_orchestra.contracts.execution import ResidentCoordinatorPhase


class _ScriptedSuperLeaderRunner(AgentRunner):
    def __init__(self) -> None:
        self.requests: list[RunnerTurnRequest] = []
        self._leader_turns: dict[str, int] = {}

    async def run_turn(self, request: RunnerTurnRequest) -> RunnerTurnResult:
        self.requests.append(request)
        if request.agent_id.startswith("leader:"):
            phase = str(request.metadata.get("planning_review_phase", ""))
            lane_id = request.agent_id.split("leader:", 1)[1]
            if phase == "draft":
                output = {
                    "summary": f"{lane_id} draft plan.",
                    "sequential_slices": [
                        {
                            "slice_id": f"{lane_id}-draft-implementation",
                            "title": f"{lane_id} draft implementation",
                            "goal": f"Implement {lane_id}.",
                            "reason": "Initial draft before peer review.",
                        }
                    ],
                    "parallel_slices": [],
                }
                return RunnerTurnResult(
                    response_id=f"resp-{request.agent_id}-draft",
                    output_text=json.dumps(output),
                    status="completed",
                )
            if phase == "peer_review":
                output = {
                    "summary": f"{lane_id} peer review complete.",
                    "reviews": [],
                }
                return RunnerTurnResult(
                    response_id=f"resp-{request.agent_id}-peer",
                    output_text=json.dumps(output),
                    status="completed",
                )
            if phase == "revision":
                output = {
                    "summary": f"{lane_id} revised plan.",
                    "sequential_slices": [
                        {
                            "slice_id": f"{lane_id}-revised-implementation",
                            "title": f"{lane_id} revised implementation",
                            "goal": f"Implement {lane_id}.",
                            "reason": "Updated after planning review.",
                        }
                    ],
                    "parallel_slices": [],
                }
                return RunnerTurnResult(
                    response_id=f"resp-{request.agent_id}-revision",
                    output_text=json.dumps(output),
                    status="completed",
                )
            turn_index = self._leader_turns.get(request.agent_id, 0) + 1
            self._leader_turns[request.agent_id] = turn_index
            if turn_index == 1:
                output = {
                    "summary": f"{lane_id} created one teammate task.",
                    "sequential_slices": [
                        {
                            "slice_id": f"{lane_id}-implementation",
                            "title": f"{lane_id} implementation",
                            "goal": f"Implement {lane_id}.",
                            "reason": "Need one execution task before closing the lane.",
                        }
                    ],
                    "parallel_slices": [],
                }
            else:
                output = {"summary": f"{lane_id} converged.", "sequential_slices": [], "parallel_slices": []}
            return RunnerTurnResult(
                response_id=f"resp-{request.agent_id}-{turn_index}",
                output_text=json.dumps(output),
                status="completed",
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


class _ParallelSuperLeaderRunner(AgentRunner):
    def __init__(self) -> None:
        self.requests: list[RunnerTurnRequest] = []
        self.active_leaders = 0
        self.max_active_leaders = 0
        self.leader_start_order: list[str] = []
        self.leader_completion_order: list[str] = []

    async def run_turn(self, request: RunnerTurnRequest) -> RunnerTurnResult:
        self.requests.append(request)
        if request.agent_id.startswith("leader:"):
            lane_id = request.agent_id.split("leader:", 1)[1]
            self.leader_start_order.append(lane_id)
            self.active_leaders += 1
            self.max_active_leaders = max(self.max_active_leaders, self.active_leaders)
            try:
                await asyncio.sleep(0.05)
                output = {
                    "summary": f"{lane_id} converged.",
                    "sequential_slices": [],
                    "parallel_slices": [],
                }
                return RunnerTurnResult(
                    response_id=f"resp-{request.agent_id}",
                    output_text=json.dumps(output),
                    status="completed",
                )
            finally:
                self.leader_completion_order.append(lane_id)
                self.active_leaders -= 1
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


class _PlanningReviewRunner(AgentRunner):
    def __init__(self) -> None:
        self.requests: list[RunnerTurnRequest] = []

    async def run_turn(self, request: RunnerTurnRequest) -> RunnerTurnResult:
        self.requests.append(request)
        phase = str(request.metadata.get("planning_review_phase", ""))
        lane_id = request.agent_id.split("leader:", 1)[1] if request.agent_id.startswith("leader:") else ""
        if phase == "draft":
            output = {
                "summary": f"{lane_id} draft plan.",
                "sequential_slices": [
                    {
                        "slice_id": f"{lane_id}-draft-slice",
                        "title": f"{lane_id} draft implementation",
                        "goal": f"Implement {lane_id} draft scope.",
                        "reason": "Initial draft for peer review.",
                    }
                ],
                "parallel_slices": [],
            }
            return RunnerTurnResult(
                response_id=f"resp-{request.agent_id}-draft",
                output_text=json.dumps(output),
                status="completed",
            )
        if phase == "peer_review":
            target_leader_id = "leader:infra" if lane_id == "runtime" else "leader:runtime"
            target_team_id = "group-a:team:infra" if lane_id == "runtime" else "group-a:team:runtime"
            output = {
                "summary": f"{lane_id} peer review complete.",
                "reviews": [
                    {
                        "target_leader_id": target_leader_id,
                        "target_team_id": target_team_id,
                        "summary": "Looks good with low conflict risk.",
                        "conflict_type": "no_conflict",
                        "severity": "low",
                        "affected_paths": [],
                        "suggested_change": "",
                    }
                ],
            }
            return RunnerTurnResult(
                response_id=f"resp-{request.agent_id}-peer",
                output_text=json.dumps(output),
                status="completed",
            )
        if phase == "revision":
            output = {
                "summary": f"{lane_id} revised plan.",
                "sequential_slices": [
                    {
                        "slice_id": f"{lane_id}-revised-slice",
                        "title": f"{lane_id} revised implementation",
                        "goal": f"Implement {lane_id} revised scope.",
                        "reason": "Updated after peer/global review.",
                    }
                ],
                "parallel_slices": [],
            }
            return RunnerTurnResult(
                response_id=f"resp-{request.agent_id}-revision",
                output_text=json.dumps(output),
                status="completed",
            )
        if request.agent_id.startswith("leader:"):
            output = {"summary": f"{lane_id} converged.", "sequential_slices": [], "parallel_slices": []}
            return RunnerTurnResult(
                response_id=f"resp-{request.agent_id}",
                output_text=json.dumps(output),
                status="completed",
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


class _SpyResidentKernel:
    def __init__(self) -> None:
        self.sessions = []
        self._delegate = ResidentCoordinatorKernel()

    async def run(self, *, session, step, finalize):
        self.sessions.append(session)
        return await self._delegate.run(session=session, step=step, finalize=finalize)


class _ObservationResidentKernel(ResidentCoordinatorKernel):
    def __init__(self, *, max_cycles: int) -> None:
        super().__init__()
        self.max_cycles = max_cycles
        self.cycles: list[ResidentCoordinatorCycleResult] = []

    async def run(self, *, session, step, finalize):
        current = replace(session)
        while True:
            cycle_result = await step(current)
            self.cycles.append(cycle_result)
            mutated_result = cycle_result
            if len(self.cycles) >= self.max_cycles and not cycle_result.stop:
                mutated_result = replace(cycle_result, stop=True)
            current = self._apply_cycle_result(current, mutated_result)
            if mutated_result.stop:
                return await finalize(current)


class SuperLeaderRuntimeTest(IsolatedAsyncioTestCase):
    async def test_superleader_runtime_runs_planning_review_round_and_seeds_lane_activation(self) -> None:
        class _RevisionBundle:
            def __init__(self, leader_id: str) -> None:
                self.leader_id = leader_id
                self.bundle_id = f"bundle:{leader_id}"

            def to_dict(self) -> dict[str, object]:
                return {"leader_id": self.leader_id, "peer_review_digests": []}

        class _PeerReview:
            def __init__(self, *, target_leader_id: str, severity: str) -> None:
                self.target_leader_id = target_leader_id
                self.severity = severity

        store = InMemoryOrchestrationStore()
        bus = InMemoryEventBus()
        runner = _PlanningReviewRunner()
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
        observation_kernel = _ObservationResidentKernel(max_cycles=4)
        superleader = SuperLeaderRuntime(
            runtime=runtime,
            evaluator=DefaultDeliveryEvaluator(),
            mailbox=InMemoryMailboxBridge(),
            resident_kernel=observation_kernel,
        )

        draft_calls: list[dict[str, object]] = []
        peer_calls: list[dict[str, object]] = []
        revised_calls: list[dict[str, object]] = []
        global_calls: list[dict[str, object]] = []

        async def _publish_leader_draft_plan(**kwargs):
            draft_calls.append(dict(kwargs))
            return type("DraftPlan", (), kwargs)()

        async def _list_leader_draft_plans(**kwargs):
            return tuple(type("DraftPlan", (), payload)() for payload in draft_calls)

        async def _publish_leader_peer_review(**kwargs):
            peer_calls.append(dict(kwargs))
            return type("PeerReview", (), kwargs)()

        async def _list_leader_peer_reviews(**kwargs):
            return tuple(
                _PeerReview(
                    target_leader_id=str(payload.get("target_leader_id", "")),
                    severity=str(payload.get("severity", "")),
                )
                for payload in peer_calls
            )

        async def _publish_superleader_global_review(**kwargs):
            global_calls.append(dict(kwargs))
            return type("GlobalReview", (), kwargs)()

        async def _build_leader_revision_context_bundle(**kwargs):
            leader_id = str(kwargs.get("leader_id", ""))
            return _RevisionBundle(leader_id)

        async def _publish_leader_revised_plan(**kwargs):
            revised_calls.append(dict(kwargs))
            return type("RevisedPlan", (), kwargs)()

        runtime.publish_leader_draft_plan = _publish_leader_draft_plan  # type: ignore[attr-defined]
        runtime.list_leader_draft_plans = _list_leader_draft_plans  # type: ignore[attr-defined]
        runtime.publish_leader_peer_review = _publish_leader_peer_review  # type: ignore[attr-defined]
        runtime.list_leader_peer_reviews = _list_leader_peer_reviews  # type: ignore[attr-defined]
        runtime.publish_superleader_global_review = _publish_superleader_global_review  # type: ignore[attr-defined]
        runtime.build_leader_revision_context_bundle = _build_leader_revision_context_bundle  # type: ignore[attr-defined]
        runtime.publish_leader_revised_plan = _publish_leader_revised_plan  # type: ignore[attr-defined]

        await runtime.create_group("group-a")
        planning_result = await planner.build_initial_plan(
            ObjectiveTemplate(
                objective_id="obj-planning-review",
                group_id="group-a",
                title="Planning review runtime",
                description="Run draft/peer/revision before lane activation.",
                workstreams=(
                    WorkstreamTemplate(
                        workstream_id="runtime",
                        title="Runtime",
                        summary="Own runtime lane.",
                        team_name="Runtime",
                        budget_max_teammates=1,
                        budget_max_iterations=1,
                    ),
                    WorkstreamTemplate(
                        workstream_id="infra",
                        title="Infra",
                        summary="Own infra lane.",
                        team_name="Infra",
                        budget_max_teammates=1,
                        budget_max_iterations=1,
                    ),
                ),
            )
        )

        result = await superleader.run_planning_result(
            planning_result,
            config=SuperLeaderConfig(
                leader_backend="in_process",
                teammate_backend="in_process",
                max_leader_turns=1,
                auto_run_teammates=True,
                working_dir="/tmp/agent-orchestra",
            ),
        )

        leader_requests = [request for request in runner.requests if request.agent_id.startswith("leader:")]
        planning_phase_requests = [
            request for request in leader_requests if request.metadata.get("planning_review_phase")
        ]
        activation_prompt_requests = [
            request for request in leader_requests if request.metadata.get("planning_review_phase") is None
        ]
        teammate_requests = [request for request in runner.requests if ":teammate:" in request.agent_id]

        self.assertEqual(result.objective_state.status, DeliveryStatus.COMPLETED)
        self.assertEqual(len(draft_calls), 2)
        self.assertEqual(len(peer_calls), 2)
        self.assertEqual(len(revised_calls), 2)
        self.assertEqual(len(global_calls), 1)
        self.assertGreaterEqual(len(planning_phase_requests), 6)
        self.assertEqual(len(activation_prompt_requests), 0)
        self.assertGreaterEqual(len(teammate_requests), 2)
        self.assertIn("planning_review", result.objective_state.metadata)
        self.assertTrue(result.objective_state.metadata["planning_review"]["enabled"])
        self.assertEqual(result.objective_state.metadata["planning_review"]["draft_plan_count"], 2)
        self.assertIn("activation_gate", result.objective_state.metadata)
        self.assertEqual(
            result.objective_state.metadata["activation_gate"]["status"],
            "ready_for_activation",
        )
        self.assertEqual(
            result.objective_state.metadata["planning_review"]["activation_gate"]["status"],
            "ready_for_activation",
        )
        activation_gate = await runtime.store.get_activation_gate_decision("obj-planning-review")
        self.assertIsNotNone(activation_gate)
        assert activation_gate is not None
        self.assertEqual(activation_gate.status.value, "ready_for_activation")

    async def test_superleader_publishes_synthesis_after_team_positions_and_cross_team_reviews_exist(self) -> None:
        store = InMemoryOrchestrationStore()
        bus = InMemoryEventBus()
        runner = _ScriptedSuperLeaderRunner()
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
        superleader = SuperLeaderRuntime(
            runtime=runtime,
            evaluator=DefaultDeliveryEvaluator(),
            mailbox=InMemoryMailboxBridge(),
        )

        await runtime.create_group("group-a")
        planning_result = await planner.build_initial_plan(
            ObjectiveTemplate(
                objective_id="obj-review-synthesis",
                group_id="group-a",
                title="Synthesize review hierarchy",
                description="Superleader should synthesize leader-level positions and cross-team reviews.",
                workstreams=(
                    WorkstreamTemplate(
                        workstream_id="runtime",
                        title="Runtime",
                        summary="Own the runtime lane.",
                        team_name="Runtime",
                        budget_max_iterations=1,
                    ),
                    WorkstreamTemplate(
                        workstream_id="infra",
                        title="Infra",
                        summary="Own the infra lane.",
                        team_name="Infra",
                        budget_max_iterations=1,
                    ),
                ),
            )
        )
        await runtime.apply_planning_result(planning_result)
        item = await runtime.create_review_item(
            objective_id="obj-review-synthesis",
            item_id="project-item-review-synthesis",
            item_kind=ReviewItemKind.PROJECT_ITEM,
            title="Shared contract decision",
            summary="Project-scoped synthesis target.",
        )
        team_a_review = await runtime.publish_team_position_review(
            item_id=item.item_id,
            team_id="group-a:team:runtime",
            leader_id="leader:runtime",
            reviewed_at="2026-04-07T13:00:00+00:00",
            team_stance="runtime_owns_this",
            summary="Leader summary only: runtime can implement after gate.",
        )
        team_b_review = await runtime.publish_team_position_review(
            item_id=item.item_id,
            team_id="group-a:team:infra",
            leader_id="leader:infra",
            reviewed_at="2026-04-07T13:01:00+00:00",
            team_stance="infra_requires_gate",
            summary="Leader summary only: infra requires rollout gate.",
        )
        cross_review = await runtime.publish_cross_team_leader_review(
            item_id=item.item_id,
            reviewer_team_id="group-a:team:runtime",
            reviewer_leader_id="leader:runtime",
            target_team_id="group-a:team:infra",
            target_position_review_id=team_b_review.position_review_id,
            reviewed_at="2026-04-07T13:02:00+00:00",
            stance="support_with_adjustment",
            agreement_level="partial",
            what_changed_in_my_understanding="Leader summary only: rollout gate is necessary before merge.",
            challenge_or_support="support",
            suggested_adjustment="Add explicit gate task.",
        )

        result = await superleader.run_planning_result(
            planning_result,
            config=SuperLeaderConfig(
                leader_backend="in_process",
                teammate_backend="in_process",
                max_leader_turns=2,
            ),
        )

        synthesis = await runtime.get_superleader_synthesis(item.item_id)

        self.assertIsNotNone(synthesis)
        assert synthesis is not None
        self.assertEqual(
            synthesis.based_on_team_position_review_ids,
            (team_a_review.position_review_id, team_b_review.position_review_id),
        )
        self.assertIn(cross_review.cross_review_id, synthesis.based_on_cross_team_review_ids)
        self.assertEqual(
            result.objective_state.metadata["hierarchical_review"]["superleader_synthesis_ids"],
            [synthesis.synthesis_id],
        )

    async def test_superleader_synthesis_reads_leader_level_reviews_not_raw_teammate_slots(self) -> None:
        store = InMemoryOrchestrationStore()
        bus = InMemoryEventBus()
        runner = _ScriptedSuperLeaderRunner()
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
        superleader = SuperLeaderRuntime(
            runtime=runtime,
            evaluator=DefaultDeliveryEvaluator(),
            mailbox=InMemoryMailboxBridge(),
        )

        await runtime.create_group("group-a")
        planning_result = await planner.build_initial_plan(
            ObjectiveTemplate(
                objective_id="obj-review-visibility",
                group_id="group-a",
                title="Synthesis visibility",
                description="Superleader synthesis should stay on leader-level summaries.",
                workstreams=(
                    WorkstreamTemplate(
                        workstream_id="runtime",
                        title="Runtime",
                        summary="Own the runtime lane.",
                        team_name="Runtime",
                        budget_max_iterations=1,
                    ),
                    WorkstreamTemplate(
                        workstream_id="infra",
                        title="Infra",
                        summary="Own the infra lane.",
                        team_name="Infra",
                        budget_max_iterations=1,
                    ),
                ),
            )
        )
        await runtime.apply_planning_result(planning_result)
        item = await runtime.create_review_item(
            objective_id="obj-review-visibility",
            item_id="project-item-visibility",
            item_kind=ReviewItemKind.PROJECT_ITEM,
            title="Shared contract decision",
            summary="Project-scoped synthesis target.",
        )
        await runtime.publish_team_position_review(
            item_id=item.item_id,
            team_id="group-a:team:runtime",
            leader_id="leader:runtime",
            reviewed_at="2026-04-07T13:00:00+00:00",
            team_stance="runtime_owns_this",
            summary="Leader summary only: runtime can implement after gate.",
        )
        infra_review = await runtime.publish_team_position_review(
            item_id=item.item_id,
            team_id="group-a:team:infra",
            leader_id="leader:infra",
            reviewed_at="2026-04-07T13:01:00+00:00",
            team_stance="infra_requires_gate",
            summary="Leader summary only: infra requires rollout gate.",
        )
        await runtime.publish_cross_team_leader_review(
            item_id=item.item_id,
            reviewer_team_id="group-a:team:runtime",
            reviewer_leader_id="leader:runtime",
            target_team_id="group-a:team:infra",
            target_position_review_id=infra_review.position_review_id,
            reviewed_at="2026-04-07T13:02:00+00:00",
            stance="support_with_adjustment",
            agreement_level="partial",
            what_changed_in_my_understanding="Leader summary only: rollout gate is necessary before merge.",
            challenge_or_support="support",
            suggested_adjustment="Add explicit gate task.",
        )

        await superleader.run_planning_result(
            planning_result,
            config=SuperLeaderConfig(
                leader_backend="in_process",
                teammate_backend="in_process",
                max_leader_turns=2,
            ),
        )

        synthesis = await runtime.get_superleader_synthesis(item.item_id)

        self.assertIsNotNone(synthesis)
        assert synthesis is not None
        self.assertIn("Leader summary only", synthesis.final_position)
        self.assertNotIn("raw teammate", synthesis.final_position.lower())

    async def test_superleader_runtime_uses_role_profiles_for_lane_execution(self) -> None:
        store = InMemoryOrchestrationStore()
        bus = InMemoryEventBus()
        runner = _ScriptedSuperLeaderRunner()
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
        superleader = SuperLeaderRuntime(
            runtime=runtime,
            evaluator=DefaultDeliveryEvaluator(),
            mailbox=InMemoryMailboxBridge(),
        )

        await runtime.create_group("group-a")
        planning_result = await planner.build_initial_plan(
            ObjectiveTemplate(
                objective_id="obj-runtime",
                group_id="group-a",
                title="Build autonomous runtime",
                description="Resolve backend from superleader role profile.",
                workstreams=(
                    WorkstreamTemplate(
                        workstream_id="runtime",
                        title="Runtime",
                        summary="Own the runtime lane.",
                        team_name="Runtime",
                        budget_max_iterations=1,
                    ),
                ),
            )
        )

        profiles = build_runtime_role_profiles()
        with self.assertRaisesRegex(ValueError, "Unknown backend: missing_backend"):
            await superleader.run_planning_result(
                planning_result,
                config=SuperLeaderConfig(
                    leader_backend=None,
                    teammate_backend=None,
                    max_leader_turns=1,
                    role_profiles={
                        "leader_missing_backend": WorkerRoleProfile(
                            profile_id="leader_missing_backend",
                            backend="missing_backend",
                            execution_contract=profiles["leader_in_process_fast"].execution_contract,
                            lease_policy=profiles["leader_in_process_fast"].lease_policy,
                        ),
                    },
                    leader_profile_id="leader_missing_backend",
                ),
            )

    async def test_superleader_runtime_runs_multiple_lanes_and_marks_objective_complete(self) -> None:
        store = InMemoryOrchestrationStore()
        bus = InMemoryEventBus()
        runner = _ScriptedSuperLeaderRunner()
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
        superleader = SuperLeaderRuntime(
            runtime=runtime,
            evaluator=DefaultDeliveryEvaluator(),
            mailbox=InMemoryMailboxBridge(),
        )

        await runtime.create_group("group-a")
        planning_result = await planner.build_initial_plan(
            ObjectiveTemplate(
                objective_id="obj-runtime",
                group_id="group-a",
                title="Build autonomous runtime",
                description="Drive both runtime and qa lanes.",
                workstreams=(
                    WorkstreamTemplate(
                        workstream_id="runtime",
                        title="Runtime",
                        summary="Own the runtime lane.",
                        team_name="Runtime",
                        budget_max_teammates=1,
                        budget_max_iterations=2,
                    ),
                    WorkstreamTemplate(
                        workstream_id="qa",
                        title="QA",
                        summary="Own the qa lane.",
                        team_name="QA",
                        depends_on=("runtime",),
                        budget_max_teammates=1,
                        budget_max_iterations=2,
                    ),
                ),
            )
        )

        result = await superleader.run_planning_result(
            planning_result,
            config=SuperLeaderConfig(
                leader_backend="in_process",
                teammate_backend="in_process",
                max_leader_turns=2,
                allow_promptless_convergence=False,
                auto_run_teammates=True,
                working_dir="/tmp/agent-orchestra",
            ),
        )

        leader_requests = [request for request in runner.requests if request.agent_id.startswith("leader:")]
        teammate_requests = [request for request in runner.requests if ":teammate:" in request.agent_id]
        authority_state = await store.get_authority_state("group-a")
        handoffs = await store.list_handoffs("group-a")

        self.assertEqual(result.objective_state.status, DeliveryStatus.COMPLETED)
        self.assertIsNotNone(authority_state)
        self.assertEqual(authority_state.status, AuthorityStatus.AUTHORITY_COMPLETE)
        self.assertEqual(len(handoffs), 2)
        self.assertEqual(
            {handoff.to_team_id for handoff in handoffs},
            {"group-a:authority-root"},
        )
        self.assertEqual(
            result.objective_state.metadata["authority_status"],
            AuthorityStatus.AUTHORITY_COMPLETE.value,
        )
        self.assertEqual(result.coordination_state.coordinator_id, "superleader:obj-runtime")
        coordination_metadata = result.objective_state.metadata["coordination"]
        self.assertEqual(coordination_metadata["coordinator_id"], "superleader:obj-runtime")
        lane_metadata_by_id = {
            lane_item["lane_id"]: lane_item for lane_item in coordination_metadata["lane_states"]
        }
        self.assertEqual(
            set(lane_metadata_by_id),
            {"runtime", "qa"},
        )
        for lane_state in result.coordination_state.lane_states:
            self.assertIsNotNone(lane_state.session)
            lane_session = lane_state.session
            assert lane_session is not None
            self.assertEqual(
                lane_session.host_owner_coordinator_id,
                result.coordination_state.coordinator_id,
            )
            self.assertTrue(lane_session.runtime_task_id)
            self.assertGreaterEqual(lane_session.prompt_turn_count, 1)
            self.assertEqual(lane_session.phase, ResidentCoordinatorPhase.QUIESCENT)
            lane_metadata = lane_metadata_by_id[lane_state.lane_id]
            self.assertEqual(
                lane_metadata["session"]["coordinator_id"],
                lane_session.coordinator_id,
            )
            self.assertEqual(
                lane_metadata["session"]["host_owner_coordinator_id"],
                result.coordination_state.coordinator_id,
            )
        planning_phase_requests = [
            request for request in leader_requests if request.metadata.get("planning_review_phase")
        ]
        activation_prompt_requests = [
            request for request in leader_requests if request.metadata.get("planning_review_phase") is None
        ]
        self.assertEqual(len(result.lane_results), 2)
        self.assertTrue(all(item.delivery_state.status == DeliveryStatus.COMPLETED for item in result.lane_results))
        self.assertEqual(len(planning_phase_requests), 6)
        self.assertEqual(len(activation_prompt_requests), 4)
        self.assertGreaterEqual(len(teammate_requests), 2)

    async def test_superleader_records_decision_and_live_view_snapshot(self) -> None:
        store = InMemoryOrchestrationStore()
        bus = InMemoryEventBus()
        runner = _ScriptedSuperLeaderRunner()
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
        superleader = SuperLeaderRuntime(
            runtime=runtime,
            evaluator=DefaultDeliveryEvaluator(),
            mailbox=InMemoryMailboxBridge(),
        )

        await runtime.create_group("group-a")
        planning_result = await planner.build_initial_plan(
            ObjectiveTemplate(
                objective_id="obj-superleader-ledger",
                group_id="group-a",
                title="Superleader ledger",
                description="Capture superleader decisions and live view snapshots.",
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
        continuity = await runtime.new_session(
            group_id="group-a",
            objective_id="obj-superleader-ledger",
            title="Superleader turn ledger",
        )

        await superleader.run_planning_result(
            planning_result,
            config=SuperLeaderConfig(
                leader_backend="in_process",
                teammate_backend="in_process",
                max_leader_turns=2,
                allow_promptless_convergence=False,
                auto_run_teammates=True,
                working_dir="/tmp/agent-orchestra",
            ),
        )

        records = await store.list_turn_records(
            continuity.work_session.work_session_id,
            runtime_generation_id=continuity.runtime_generation.runtime_generation_id,
            head_kind=ConversationHeadKind.SUPERLEADER,
            scope_id="obj-superleader-ledger",
        )
        self.assertTrue(
            any(record.turn_kind == AgentTurnKind.SUPERLEADER_DECISION for record in records)
        )
        artifacts = await store.list_artifact_refs(
            continuity.work_session.work_session_id,
            runtime_generation_id=continuity.runtime_generation.runtime_generation_id,
        )
        self.assertTrue(
            any(ref.artifact_kind == ArtifactRefKind.DELIVERY_SNAPSHOT for ref in artifacts)
        )

    async def test_superleader_runtime_propagates_promptless_convergence_to_leader_lanes(self) -> None:
        store = InMemoryOrchestrationStore()
        bus = InMemoryEventBus()
        runner = _ScriptedSuperLeaderRunner()
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
        superleader = SuperLeaderRuntime(
            runtime=runtime,
            evaluator=DefaultDeliveryEvaluator(),
            mailbox=InMemoryMailboxBridge(),
        )

        await runtime.create_group("group-a")
        planning_result = await planner.build_initial_plan(
            ObjectiveTemplate(
                objective_id="obj-runtime-promptless",
                group_id="group-a",
                title="Build autonomous runtime",
                description="Propagate leader promptless convergence through superleader lanes.",
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

        result = await superleader.run_planning_result(
            planning_result,
            config=SuperLeaderConfig(
                leader_backend="in_process",
                teammate_backend="in_process",
                max_leader_turns=1,
                max_mailbox_followup_turns=0,
                auto_run_teammates=True,
                working_dir="/tmp/agent-orchestra",
            ),
        )

        leader_requests = [request for request in runner.requests if request.agent_id.startswith("leader:")]
        planning_phase_requests = [
            request for request in leader_requests if request.metadata.get("planning_review_phase")
        ]
        activation_prompt_requests = [
            request for request in leader_requests if request.metadata.get("planning_review_phase") is None
        ]
        teammate_requests = [request for request in runner.requests if ":teammate:" in request.agent_id]

        self.assertEqual(result.objective_state.status, DeliveryStatus.COMPLETED)
        self.assertEqual(len(result.lane_results), 1)
        self.assertEqual(result.lane_results[0].delivery_state.status, DeliveryStatus.COMPLETED)
        self.assertEqual(len(planning_phase_requests), 3)
        self.assertEqual(len(activation_prompt_requests), 0)
        self.assertEqual(len(teammate_requests), 1)
        self.assertEqual(len(result.lane_results[0].turns), 1)
        self.assertEqual(result.lane_results[0].coordinator_session.prompt_turn_count, 1)

    async def test_superleader_runtime_runs_dynamically_planned_template(self) -> None:
        store = InMemoryOrchestrationStore()
        bus = InMemoryEventBus()
        runner = _ScriptedSuperLeaderRunner()
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
        planner = DynamicSuperLeaderPlanner(
            DynamicPlanningConfig(
                max_workstreams=2,
                default_budget_max_teammates=1,
                default_budget_max_iterations=2,
            )
        )
        superleader = SuperLeaderRuntime(
            runtime=runtime,
            evaluator=DefaultDeliveryEvaluator(),
            mailbox=InMemoryMailboxBridge(),
        )

        await runtime.create_group("group-a")
        result = await superleader.run_template(
            planner=planner,
            template=ObjectiveTemplate(
                objective_id="obj-dynamic-runtime",
                group_id="group-a",
                title="Advance runtime reliability and planning",
                description="Let the dynamic planner synthesize runtime and planning lanes.",
                metadata={
                    "planning_mode": "dynamic_superleader",
                    "dynamic_workstream_seeds": [
                        {
                            "workstream_id": "worker-reliability",
                            "title": "Worker Reliability",
                            "summary": "Own retry and resume hardening.",
                            "team_name": "Runtime",
                            "budget": {"max_teammates": 1, "max_iterations": 2},
                        },
                        {
                            "workstream_id": "dynamic-team-planning",
                            "title": "Dynamic Team Planning",
                            "summary": "Own bounded dynamic planner integration.",
                            "team_name": "Planning",
                            "depends_on": ["worker-reliability"],
                            "budget": {"max_teammates": 1, "max_iterations": 2},
                        },
                    ],
                },
            ),
            config=SuperLeaderConfig(
                leader_backend="in_process",
                teammate_backend="in_process",
                max_leader_turns=2,
                auto_run_teammates=True,
                working_dir="/tmp/agent-orchestra",
            ),
        )

        self.assertEqual(result.objective_state.status, DeliveryStatus.COMPLETED)
        self.assertEqual(len(result.lane_results), 2)
        self.assertEqual(
            {item.leader_round.lane_id for item in result.lane_results},
            {"worker-reliability", "dynamic-team-planning"},
        )
        self.assertTrue(all(item.delivery_state.status == DeliveryStatus.COMPLETED for item in result.lane_results))

    async def test_superleader_runtime_uses_injected_resident_kernel_and_reports_session(self) -> None:
        store = InMemoryOrchestrationStore()
        bus = InMemoryEventBus()
        runner = _ScriptedSuperLeaderRunner()
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
        spy_kernel = _SpyResidentKernel()
        superleader = SuperLeaderRuntime(
            runtime=runtime,
            evaluator=DefaultDeliveryEvaluator(),
            mailbox=InMemoryMailboxBridge(),
            resident_kernel=spy_kernel,
        )

        await runtime.create_group("group-a")
        planning_result = await planner.build_initial_plan(
            ObjectiveTemplate(
                objective_id="obj-runtime",
                group_id="group-a",
                title="Build autonomous runtime",
                description="Run the outer coordination shell through the shared resident kernel.",
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

        result = await superleader.run_planning_result(
            planning_result,
            config=SuperLeaderConfig(
                leader_backend="in_process",
                teammate_backend="in_process",
                max_leader_turns=2,
                auto_run_teammates=True,
                working_dir="/tmp/agent-orchestra",
            ),
        )

        self.assertEqual(len(spy_kernel.sessions), 1)
        self.assertEqual(spy_kernel.sessions[0].phase, ResidentCoordinatorPhase.BOOTING)
        self.assertIsNotNone(result.coordinator_session)
        self.assertEqual(result.coordinator_session.phase, ResidentCoordinatorPhase.QUIESCENT)
        self.assertGreaterEqual(result.coordinator_session.cycle_count, 1)
        self.assertEqual(result.coordinator_session.prompt_turn_count, 0)
        self.assertEqual(result.coordinator_session.claimed_task_count, 1)
        self.assertEqual(result.coordinator_session.subordinate_dispatch_count, 1)
        self.assertEqual(result.coordinator_session.active_subordinate_ids, ())
        self.assertEqual(result.coordinator_session.metadata["delivery_status"], DeliveryStatus.COMPLETED.value)

    async def test_superleader_runtime_runs_parallel_lanes_up_to_objective_budget(self) -> None:
        store = InMemoryOrchestrationStore()
        bus = InMemoryEventBus()
        runner = _ParallelSuperLeaderRunner()
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
        superleader = SuperLeaderRuntime(
            runtime=runtime,
            evaluator=DefaultDeliveryEvaluator(),
            mailbox=InMemoryMailboxBridge(),
        )

        await runtime.create_group("group-a")
        planning_result = await planner.build_initial_plan(
            ObjectiveTemplate(
                objective_id="obj-parallel-runtime",
                group_id="group-a",
                title="Run parallel runtime lanes",
                description="Exercise bounded parallel superleader execution.",
                global_budget={"max_teams": 2},
                workstreams=(
                    WorkstreamTemplate(
                        workstream_id="runtime",
                        title="Runtime",
                        summary="Own the runtime lane.",
                        team_name="Runtime",
                        budget_max_iterations=1,
                    ),
                    WorkstreamTemplate(
                        workstream_id="qa",
                        title="QA",
                        summary="Own the qa lane.",
                        team_name="QA",
                        budget_max_iterations=1,
                    ),
                    WorkstreamTemplate(
                        workstream_id="docs",
                        title="Docs",
                        summary="Own the docs lane.",
                        team_name="Docs",
                        budget_max_iterations=1,
                    ),
                ),
            )
        )

        result = await superleader.run_planning_result(
            planning_result,
            config=SuperLeaderConfig(
                leader_backend="in_process",
                teammate_backend="in_process",
                max_leader_turns=1,
                auto_run_teammates=True,
                working_dir="/tmp/agent-orchestra",
            ),
        )

        self.assertEqual(result.objective_state.status, DeliveryStatus.COMPLETED)
        self.assertEqual(len(result.lane_results), 3)
        self.assertEqual(result.coordination_state.max_active_lanes, 2)
        self.assertEqual(result.coordination_state.batch_count, 2)
        self.assertEqual(result.coordination_state.pending_lane_ids, ())
        self.assertEqual(result.coordination_state.active_lane_ids, ())
        self.assertEqual(
            set(result.coordination_state.completed_lane_ids),
            {"runtime", "qa", "docs"},
        )
        self.assertEqual(result.coordination_state.coordinator_id, "superleader:obj-parallel-runtime")
        lane_sessions_by_lane = {}
        for lane_state in result.coordination_state.lane_states:
            self.assertIsNotNone(lane_state.session)
            lane_session = lane_state.session
            assert lane_session is not None
            lane_sessions_by_lane[lane_state.lane_id] = lane_session
            self.assertEqual(lane_session.host_owner_coordinator_id, result.coordination_state.coordinator_id)
            self.assertGreaterEqual(lane_session.cycle_count, 1)
            self.assertEqual(lane_session.phase, ResidentCoordinatorPhase.QUIESCENT)
        self.assertEqual(
            {lane_id: lane_session.coordinator_id for lane_id, lane_session in lane_sessions_by_lane.items()},
            {
                "runtime": "leader:runtime",
                "qa": "leader:qa",
                "docs": "leader:docs",
            },
        )
        self.assertEqual(
            result.objective_state.metadata["coordination"]["max_active_lanes"],
            2,
        )

    async def test_superleader_runtime_projects_host_owned_leader_session_graph(self) -> None:
        store = InMemoryOrchestrationStore()
        bus = InMemoryEventBus()
        runner = _ScriptedSuperLeaderRunner()
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
        superleader = SuperLeaderRuntime(
            runtime=runtime,
            evaluator=DefaultDeliveryEvaluator(),
            mailbox=InMemoryMailboxBridge(),
        )

        await runtime.create_group("group-a")
        planning_result = await planner.build_initial_plan(
            ObjectiveTemplate(
                objective_id="obj-host-owned-leader-runtime",
                group_id="group-a",
                title="Project host-owned leader sessions",
                description="Use the session host as the leader lane runtime boundary.",
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

        result = await superleader.run_planning_result(
            planning_result,
            config=SuperLeaderConfig(
                leader_backend="in_process",
                teammate_backend="in_process",
                max_leader_turns=2,
                auto_run_teammates=True,
                working_dir="/tmp/agent-orchestra",
            ),
        )

        coordinator_sessions = await supervisor.session_host.list_coordinator_sessions(
            role="leader",
            objective_id="obj-host-owned-leader-runtime",
            host_owner_coordinator_id=result.coordination_state.coordinator_id,
        )

        self.assertEqual(result.objective_state.status, DeliveryStatus.COMPLETED)
        self.assertEqual(len(coordinator_sessions), 1)
        self.assertEqual(coordinator_sessions[0].lane_id, "runtime")
        self.assertEqual(coordinator_sessions[0].metadata["runtime_view"], "leader_lane_session_graph")
        self.assertEqual(coordinator_sessions[0].metadata["launch_mode"], "leader_session_host")
        lane_session = result.coordination_state.lane_states[0].session
        self.assertIsNotNone(lane_session)
        assert lane_session is not None
        self.assertEqual(lane_session.metadata["runtime_view"], "leader_lane_session_graph")
        self.assertEqual(lane_session.metadata["launch_mode"], "leader_session_host")
        self.assertEqual(
            lane_session.prompt_turn_count,
            coordinator_sessions[0].prompt_turn_count,
        )

    async def test_superleader_runtime_keeps_dependency_gates_under_parallel_scheduling(self) -> None:
        store = InMemoryOrchestrationStore()
        bus = InMemoryEventBus()
        runner = _ParallelSuperLeaderRunner()
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
        superleader = SuperLeaderRuntime(
            runtime=runtime,
            evaluator=DefaultDeliveryEvaluator(),
            mailbox=InMemoryMailboxBridge(),
        )

        await runtime.create_group("group-a")
        planning_result = await planner.build_initial_plan(
            ObjectiveTemplate(
                objective_id="obj-parallel-deps",
                group_id="group-a",
                title="Respect lane dependencies",
                description="Do not start dependent lanes before their prerequisites finish.",
                global_budget={"max_teams": 2},
                workstreams=(
                    WorkstreamTemplate(
                        workstream_id="runtime",
                        title="Runtime",
                        summary="Own the runtime lane.",
                        team_name="Runtime",
                        budget_max_iterations=1,
                    ),
                    WorkstreamTemplate(
                        workstream_id="docs",
                        title="Docs",
                        summary="Own the docs lane.",
                        team_name="Docs",
                        budget_max_iterations=1,
                    ),
                    WorkstreamTemplate(
                        workstream_id="qa",
                        title="QA",
                        summary="Own the qa lane after runtime converges.",
                        team_name="QA",
                        depends_on=("runtime",),
                        budget_max_iterations=1,
                    ),
                ),
            )
        )

        result = await superleader.run_planning_result(
            planning_result,
            config=SuperLeaderConfig(
                leader_backend="in_process",
                teammate_backend="in_process",
                max_leader_turns=1,
                auto_run_teammates=True,
                working_dir="/tmp/agent-orchestra",
            ),
        )

        self.assertEqual(result.objective_state.status, DeliveryStatus.COMPLETED)
        self.assertEqual(runner.leader_start_order[:2], ["runtime", "docs"])
        self.assertEqual(runner.leader_start_order[2], "qa")
        self.assertIn("runtime", runner.leader_completion_order[:2])
        self.assertEqual(len(result.lane_results), 3)
        self.assertEqual(result.coordination_state.batch_count, 2)
        qa_state = next(
            state for state in result.coordination_state.lane_states if state.lane_id == "qa"
        )
        self.assertEqual(qa_state.dependency_lane_ids, ("runtime",))
        self.assertEqual(qa_state.started_in_batch, 2)

    async def test_superleader_runtime_exposes_objective_shared_subscription_view(self) -> None:
        store = InMemoryOrchestrationStore()
        bus = InMemoryEventBus()
        runner = _ScriptedSuperLeaderRunner()
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
        superleader = SuperLeaderRuntime(
            runtime=runtime,
            evaluator=DefaultDeliveryEvaluator(),
            mailbox=ProtocolInMemoryMailboxBridge(),
        )

        await runtime.create_group("group-a")
        planning_result = await planner.build_initial_plan(
            ObjectiveTemplate(
                objective_id="obj-runtime",
                group_id="group-a",
                title="Build autonomous runtime",
                description="Expose objective-level shared subscription views.",
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

        result = await superleader.run_planning_result(
            planning_result,
            config=SuperLeaderConfig(
                leader_backend="in_process",
                teammate_backend="in_process",
                max_leader_turns=2,
                auto_run_teammates=True,
                working_dir="/tmp/agent-orchestra",
            ),
        )

        self.assertEqual(result.objective_state.status, DeliveryStatus.COMPLETED)
        self.assertTrue(result.message_subscriptions)
        objective_shared = result.message_subscriptions[0]
        self.assertEqual(objective_shared.visibility_scopes, (MailboxVisibilityScope.SHARED,))
        self.assertEqual(objective_shared.delivery_mode, MailboxDeliveryMode.SUMMARY_PLUS_REF)
        if result.message_digests:
            self.assertTrue(
                all(item.visibility_scope == MailboxVisibilityScope.SHARED for item in result.message_digests)
            )
        message_runtime = result.objective_state.metadata["message_runtime"]
        self.assertEqual(
            message_runtime["objective_shared_subscription_id"],
            objective_shared.subscription_id,
        )
        self.assertEqual(
            message_runtime["objective_shared_digest_count"],
            len(result.message_digests),
        )

    async def test_superleader_runtime_steps_host_owned_lane_from_resident_live_view(self) -> None:
        store = InMemoryOrchestrationStore()
        bus = InMemoryEventBus()
        runner = _ScriptedSuperLeaderRunner()
        mailbox = ProtocolInMemoryMailboxBridge()
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
        observation_kernel = _ObservationResidentKernel(max_cycles=2)
        superleader = SuperLeaderRuntime(
            runtime=runtime,
            evaluator=DefaultDeliveryEvaluator(),
            mailbox=mailbox,
            resident_kernel=observation_kernel,
        )

        await runtime.create_group("group-a")
        planning_result = await planner.build_initial_plan(
            ObjectiveTemplate(
                objective_id="obj-resident-live-view",
                group_id="group-a",
                title="Resident lane live view",
                description="Use host-owned lane truth before relaunching leader lanes.",
                workstreams=(
                    WorkstreamTemplate(
                        workstream_id="runtime",
                        title="Runtime",
                        summary="Runtime lane is already resident.",
                        team_name="Runtime",
                    ),
                    WorkstreamTemplate(
                        workstream_id="qa",
                        title="QA",
                        summary="QA waits for runtime lane completion.",
                        team_name="QA",
                        depends_on=("runtime",),
                    ),
                ),
            )
        )
        round_bundle = await materialize_planning_result(
            runtime,
            planning_result,
            created_by="resident-live-view-test",
        )
        runtime_round = next(
            leader_round
            for leader_round in round_bundle.leader_rounds
            if leader_round.lane_id == "runtime"
        )
        superleader_id = f"superleader:{round_bundle.objective.objective_id}"
        session_host = supervisor.session_host
        runtime_session_id = (
            f"{round_bundle.objective.objective_id}:lane:{runtime_round.lane_id}:leader:resident"
        )
        await session_host.load_or_create_coordinator_session(
            session_id=runtime_session_id,
            coordinator_id=runtime_round.leader_task.leader_id,
            objective_id=round_bundle.objective.objective_id,
            lane_id=runtime_round.lane_id,
            team_id=runtime_round.team_id,
            role="leader",
            phase=ResidentCoordinatorPhase.WAITING_FOR_MAILBOX,
            host_owner_coordinator_id=superleader_id,
            runtime_task_id=runtime_round.runtime_task.task_id,
            metadata={
                "runtime_view": "leader_lane_session_graph",
                "launch_mode": "leader_session_host",
            },
        )
        await session_host.record_coordinator_session_state(
            runtime_session_id,
            coordinator_session=ResidentCoordinatorSession(
                coordinator_id=runtime_round.leader_task.leader_id,
                role="leader",
                phase=ResidentCoordinatorPhase.WAITING_FOR_MAILBOX,
                objective_id=round_bundle.objective.objective_id,
                lane_id=runtime_round.lane_id,
                team_id=runtime_round.team_id,
                cycle_count=3,
                prompt_turn_count=1,
                mailbox_poll_count=2,
                mailbox_cursor="envelope-runtime-1",
                last_reason="Resident runtime lane is waiting on mailbox activity.",
                metadata={
                    "runtime_view": "leader_lane_session_graph",
                    "launch_mode": "leader_session_host",
                },
            ),
            host_owner_coordinator_id=superleader_id,
            runtime_task_id=runtime_round.runtime_task.task_id,
            metadata={
                "runtime_view": "leader_lane_session_graph",
                "launch_mode": "leader_session_host",
            },
        )
        await runtime.store.save_delivery_state(
            DeliveryState(
                delivery_id=f"{round_bundle.objective.objective_id}:lane:{runtime_round.lane_id}",
                objective_id=round_bundle.objective.objective_id,
                kind=DeliveryStateKind.LANE,
                status=DeliveryStatus.RUNNING,
                lane_id=runtime_round.lane_id,
                team_id=runtime_round.team_id,
                iteration=1,
                summary="Runtime lane already running under resident leader session.",
                metadata={
                    "message_runtime": {"pending_shared_digest_count": 1},
                    "lane_session": {
                        "coordinator_id": runtime_round.leader_task.leader_id,
                        "host_owner_coordinator_id": superleader_id,
                        "role": "leader",
                        "objective_id": round_bundle.objective.objective_id,
                        "lane_id": runtime_round.lane_id,
                        "team_id": runtime_round.team_id,
                        "runtime_task_id": runtime_round.runtime_task.task_id,
                        "phase": ResidentCoordinatorPhase.WAITING_FOR_MAILBOX.value,
                        "cycle_count": 3,
                        "prompt_turn_count": 1,
                        "claimed_task_count": 0,
                        "subordinate_dispatch_count": 0,
                        "mailbox_poll_count": 2,
                        "active_subordinate_ids": [],
                        "mailbox_cursor": "envelope-runtime-1",
                        "last_reason": "Resident runtime lane is waiting on mailbox activity.",
                        "metadata": {
                            "runtime_view": "leader_lane_session_graph",
                            "launch_mode": "leader_session_host",
                        },
                    }
                },
            )
        )
        await mailbox.send(
            MailboxEnvelope(
                sender="group-a:team:runtime:leader",
                recipient="group-a:team:runtime:leader",
                subject="task.result",
                group_id="group-a",
                lane_id="runtime",
                team_id=runtime_round.team_id,
                summary="Resident runtime lane published a shared digest.",
                full_text_ref="blackboard:resident-live-view-1",
                visibility_scope=MailboxVisibilityScope.SHARED,
            )
        )

        lane_calls: list[str] = []
        lane_session_metadata: dict[str, dict[str, object]] = {}

        async def _step_existing_lane(
            *,
            objective,
            leader_round,
            session_metadata=None,
            **kwargs,
        ):
            lane_calls.append(leader_round.lane_id)
            lane_session_metadata[leader_round.lane_id] = dict(session_metadata or {})
            delivery_state = DeliveryState(
                delivery_id=f"{objective.objective_id}:lane:{leader_round.lane_id}",
                objective_id=objective.objective_id,
                kind=DeliveryStateKind.LANE,
                status=DeliveryStatus.COMPLETED,
                lane_id=leader_round.lane_id,
                team_id=leader_round.team_id,
                iteration=2,
                summary=f"{leader_round.lane_id} converged from the host-stepped resident runtime.",
            )
            coordinator_session = ResidentCoordinatorSession(
                coordinator_id=leader_round.leader_task.leader_id,
                role="leader",
                phase=ResidentCoordinatorPhase.QUIESCENT,
                objective_id=objective.objective_id,
                lane_id=leader_round.lane_id,
                team_id=leader_round.team_id,
                cycle_count=4,
                prompt_turn_count=1,
                mailbox_poll_count=3,
                mailbox_cursor="envelope-runtime-1",
                last_reason="Host-stepped resident runtime converged.",
                metadata=dict(session_metadata or {}),
            )
            await runtime.store.save_delivery_state(delivery_state)
            await session_host.load_or_create_coordinator_session(
                session_id=f"{objective.objective_id}:lane:{leader_round.lane_id}:leader:resident",
                coordinator_id=leader_round.leader_task.leader_id,
                objective_id=objective.objective_id,
                lane_id=leader_round.lane_id,
                team_id=leader_round.team_id,
                role="leader",
                phase=ResidentCoordinatorPhase.BOOTING,
                host_owner_coordinator_id=superleader_id,
                runtime_task_id=leader_round.runtime_task.task_id,
                metadata=dict(session_metadata or {}),
            )
            await session_host.record_coordinator_session_state(
                f"{objective.objective_id}:lane:{leader_round.lane_id}:leader:resident",
                coordinator_session=coordinator_session,
                host_owner_coordinator_id=superleader_id,
                runtime_task_id=leader_round.runtime_task.task_id,
                metadata=dict(session_metadata or {}),
            )
            return LeaderLoopResult(
                leader_round=leader_round,
                delivery_state=delivery_state,
                leader_records=(),
                teammate_records=(),
                coordinator_session=coordinator_session,
            )

        with patch.object(
            LeaderLoopSupervisor,
            "ensure_or_step_session",
            new=AsyncMock(side_effect=_step_existing_lane),
        ):
            result = await superleader.run_planning_result(
                planning_result,
                config=SuperLeaderConfig(
                    leader_backend="in_process",
                    teammate_backend="in_process",
                    max_leader_turns=1,
                    auto_run_teammates=True,
                    working_dir="/tmp/agent-orchestra",
                ),
            )

        self.assertEqual(lane_calls, ["runtime", "qa"])
        runtime_inputs = lane_session_metadata["runtime"]["resident_live_inputs"]
        self.assertEqual(runtime_inputs["pending_shared_digest_count"], 1)
        self.assertEqual(runtime_inputs["objective_shared_digest_count"], 1)
        self.assertEqual(runtime_inputs["host_phase"], "waiting_for_mailbox")
        self.assertEqual(result.objective_state.status, DeliveryStatus.COMPLETED)
        self.assertEqual(result.coordination_state.completed_lane_ids, ("runtime", "qa"))

    async def test_superleader_runtime_prefers_host_lane_session_projection_over_stale_pending_delivery(self) -> None:
        store = InMemoryOrchestrationStore()
        bus = InMemoryEventBus()
        runner = _ScriptedSuperLeaderRunner()
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
        observation_kernel = _ObservationResidentKernel(max_cycles=2)
        superleader = SuperLeaderRuntime(
            runtime=runtime,
            evaluator=DefaultDeliveryEvaluator(),
            mailbox=ProtocolInMemoryMailboxBridge(),
            resident_kernel=observation_kernel,
        )

        await runtime.create_group("group-a")
        planning_result = await planner.build_initial_plan(
            ObjectiveTemplate(
                objective_id="obj-stale-lane-delivery",
                group_id="group-a",
                title="Stale lane delivery snapshot",
                description="Host-owned leader sessions should override stale pending lane delivery snapshots.",
                workstreams=(
                    WorkstreamTemplate(
                        workstream_id="runtime",
                        title="Runtime",
                        summary="Runtime lane already has a resident host session.",
                        team_name="Runtime",
                    ),
                    WorkstreamTemplate(
                        workstream_id="qa",
                        title="QA",
                        summary="QA depends on runtime completion.",
                        team_name="QA",
                        depends_on=("runtime",),
                    ),
                ),
            )
        )
        round_bundle = await materialize_planning_result(
            runtime,
            planning_result,
            created_by="stale-resident-live-view-test",
        )
        runtime_round = next(
            leader_round
            for leader_round in round_bundle.leader_rounds
            if leader_round.lane_id == "runtime"
        )
        superleader_id = f"superleader:{round_bundle.objective.objective_id}"
        session_host = supervisor.session_host
        runtime_session_id = (
            f"{round_bundle.objective.objective_id}:lane:{runtime_round.lane_id}:leader:resident"
        )
        await session_host.load_or_create_coordinator_session(
            session_id=runtime_session_id,
            coordinator_id=runtime_round.leader_task.leader_id,
            objective_id=round_bundle.objective.objective_id,
            lane_id=runtime_round.lane_id,
            team_id=runtime_round.team_id,
            role="leader",
            phase=ResidentCoordinatorPhase.WAITING_FOR_MAILBOX,
            host_owner_coordinator_id=superleader_id,
            runtime_task_id=runtime_round.runtime_task.task_id,
            metadata={
                "runtime_view": "leader_lane_session_graph",
                "launch_mode": "leader_session_host",
            },
        )
        await session_host.record_coordinator_session_state(
            runtime_session_id,
            coordinator_session=ResidentCoordinatorSession(
                coordinator_id=runtime_round.leader_task.leader_id,
                role="leader",
                phase=ResidentCoordinatorPhase.WAITING_FOR_MAILBOX,
                objective_id=round_bundle.objective.objective_id,
                lane_id=runtime_round.lane_id,
                team_id=runtime_round.team_id,
                cycle_count=4,
                prompt_turn_count=1,
                mailbox_poll_count=3,
                mailbox_cursor="stale-runtime-envelope",
                last_reason="Runtime lane is waiting on mailbox convergence.",
                metadata={
                    "runtime_view": "leader_lane_session_graph",
                    "launch_mode": "leader_session_host",
                },
            ),
            host_owner_coordinator_id=superleader_id,
            runtime_task_id=runtime_round.runtime_task.task_id,
            metadata={
                "runtime_view": "leader_lane_session_graph",
                "launch_mode": "leader_session_host",
            },
        )
        await runtime.store.save_delivery_state(
            DeliveryState(
                delivery_id=f"{round_bundle.objective.objective_id}:lane:{runtime_round.lane_id}",
                objective_id=round_bundle.objective.objective_id,
                kind=DeliveryStateKind.LANE,
                status=DeliveryStatus.PENDING,
                lane_id=runtime_round.lane_id,
                team_id=runtime_round.team_id,
                iteration=0,
                summary="Stale snapshot still says the runtime lane is pending.",
                metadata={
                    "message_runtime": {
                        "pending_shared_digest_count": 1,
                        "shared_digest_envelope_ids": ["stale-runtime-envelope"],
                    },
                    "mailbox_followup_turns_used": 1,
                    "mailbox_followup_turn_limit": 4,
                },
            )
        )

        lane_calls: list[str] = []
        lane_session_metadata: dict[str, dict[str, object]] = {}

        async def _step_host_projected_lane(
            *,
            objective,
            leader_round,
            session_metadata=None,
            **kwargs,
        ):
            lane_calls.append(leader_round.lane_id)
            lane_session_metadata[leader_round.lane_id] = dict(session_metadata or {})
            delivery_state = DeliveryState(
                delivery_id=f"{objective.objective_id}:lane:{leader_round.lane_id}",
                objective_id=objective.objective_id,
                kind=DeliveryStateKind.LANE,
                status=DeliveryStatus.COMPLETED,
                lane_id=leader_round.lane_id,
                team_id=leader_round.team_id,
                iteration=1,
                summary=f"{leader_round.lane_id} converged after stale pending delivery was ignored.",
            )
            coordinator_session = ResidentCoordinatorSession(
                coordinator_id=leader_round.leader_task.leader_id,
                role="leader",
                phase=ResidentCoordinatorPhase.QUIESCENT,
                objective_id=objective.objective_id,
                lane_id=leader_round.lane_id,
                team_id=leader_round.team_id,
                cycle_count=5,
                prompt_turn_count=1,
                mailbox_poll_count=4,
                mailbox_cursor="stale-runtime-envelope",
                last_reason="Host-stepped resident runtime overrode stale pending delivery state.",
                metadata=dict(session_metadata or {}),
            )
            await runtime.store.save_delivery_state(delivery_state)
            await session_host.load_or_create_coordinator_session(
                session_id=f"{objective.objective_id}:lane:{leader_round.lane_id}:leader:resident",
                coordinator_id=leader_round.leader_task.leader_id,
                objective_id=objective.objective_id,
                lane_id=leader_round.lane_id,
                team_id=leader_round.team_id,
                role="leader",
                phase=ResidentCoordinatorPhase.BOOTING,
                host_owner_coordinator_id=superleader_id,
                runtime_task_id=leader_round.runtime_task.task_id,
                metadata=dict(session_metadata or {}),
            )
            await session_host.record_coordinator_session_state(
                f"{objective.objective_id}:lane:{leader_round.lane_id}:leader:resident",
                coordinator_session=coordinator_session,
                host_owner_coordinator_id=superleader_id,
                runtime_task_id=leader_round.runtime_task.task_id,
                metadata=dict(session_metadata or {}),
            )
            return LeaderLoopResult(
                leader_round=leader_round,
                delivery_state=delivery_state,
                leader_records=(),
                teammate_records=(),
                coordinator_session=coordinator_session,
            )

        with patch.object(
            LeaderLoopSupervisor,
            "ensure_or_step_session",
            new=AsyncMock(side_effect=_step_host_projected_lane),
        ):
            result = await superleader.run_planning_result(
                planning_result,
                config=SuperLeaderConfig(
                    leader_backend="in_process",
                    teammate_backend="in_process",
                    max_leader_turns=1,
                    auto_run_teammates=True,
                    working_dir="/tmp/agent-orchestra",
                ),
            )

        self.assertEqual(lane_calls, ["runtime", "qa"])
        runtime_inputs = lane_session_metadata["runtime"]["resident_live_inputs"]
        self.assertEqual(runtime_inputs["pending_shared_digest_count"], 1)
        self.assertEqual(
            runtime_inputs["shared_digest_envelope_ids"],
            ["stale-runtime-envelope"],
        )
        self.assertEqual(runtime_inputs["mailbox_followup_turns_used"], 1)
        self.assertEqual(runtime_inputs["host_phase"], "waiting_for_mailbox")
        self.assertEqual(result.objective_state.status, DeliveryStatus.COMPLETED)
        resident_live_view = result.objective_state.metadata["resident_live_view"]
        self.assertEqual(
            resident_live_view["lane_truth_sources"],
            {"runtime": "delivery_state", "qa": "delivery_state"},
        )
        self.assertEqual(resident_live_view["runtime_native_lane_ids"], ["runtime", "qa"])
        self.assertEqual(resident_live_view["fallback_lane_ids"], [])
        self.assertEqual(resident_live_view["primary_active_lane_ids"], [])
        self.assertEqual(resident_live_view["primary_pending_lane_ids"], [])
        self.assertEqual(resident_live_view["primary_active_lane_session_ids"], [])
        self.assertEqual(
            resident_live_view["primary_lane_statuses"],
            {"runtime": DeliveryStatus.COMPLETED.value, "qa": DeliveryStatus.COMPLETED.value},
        )

    async def test_superleader_runtime_passes_task_surface_authority_live_inputs_into_host_step(
        self,
    ) -> None:
        store = InMemoryOrchestrationStore()
        bus = InMemoryEventBus()
        runner = _ScriptedSuperLeaderRunner()
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
        observation_kernel = _ObservationResidentKernel(max_cycles=1)
        superleader = SuperLeaderRuntime(
            runtime=runtime,
            evaluator=DefaultDeliveryEvaluator(),
            mailbox=ProtocolInMemoryMailboxBridge(),
            resident_kernel=observation_kernel,
        )

        await runtime.create_group("group-a")
        planning_result = await planner.build_initial_plan(
            ObjectiveTemplate(
                objective_id="obj-task-surface-host-step",
                group_id="group-a",
                title="Task-surface governed host step",
                description="Pass resident live inputs into the host-stepped leader runtime.",
                workstreams=(
                    WorkstreamTemplate(
                        workstream_id="runtime",
                        title="Runtime",
                        summary="Runtime lane already has a resident host session.",
                        team_name="Runtime",
                    ),
                ),
            )
        )
        round_bundle = await materialize_planning_result(
            runtime,
            planning_result,
            created_by="task-surface-host-step-test",
        )
        runtime_round = round_bundle.leader_rounds[0]
        session_id = (
            f"{round_bundle.objective.objective_id}:lane:{runtime_round.lane_id}:leader:resident"
        )
        await supervisor.session_host.load_or_create_coordinator_session(
            session_id=session_id,
            coordinator_id=runtime_round.leader_task.leader_id,
            objective_id=round_bundle.objective.objective_id,
            lane_id=runtime_round.lane_id,
            team_id=runtime_round.team_id,
            role="leader",
            phase=ResidentCoordinatorPhase.WAITING_FOR_MAILBOX,
            host_owner_coordinator_id=f"superleader:{round_bundle.objective.objective_id}",
            runtime_task_id=runtime_round.runtime_task.task_id,
            metadata={
                "runtime_view": "leader_lane_session_graph",
                "launch_mode": "leader_session_host",
            },
        )
        coordinator_session = await supervisor.session_host.load_coordinator_session(session_id)
        self.assertIsNotNone(coordinator_session)
        assert coordinator_session is not None

        captured_session_metadata: dict[str, object] = {}
        lane_completed = False

        async def _fake_read_resident_live_view(*args, **kwargs):
            delivery_status = (
                DeliveryStatus.COMPLETED if lane_completed else DeliveryStatus.RUNNING
            )
            phase = (
                ResidentCoordinatorPhase.QUIESCENT
                if lane_completed
                else ResidentCoordinatorPhase.WAITING_FOR_MAILBOX
            )
            task_surface_authority = (
                {}
                if lane_completed
                else {
                    "authority_waiting": True,
                    "task_surface_waiting_task_ids": ["task-runtime-authority"],
                    "mutation_waiting_task_ids": ["task-runtime-authority"],
                    "waiting_request_ids": ["authority-request-1"],
                }
            )
            return SuperLeaderResidentLiveView(
                objective_id=round_bundle.objective.objective_id,
                lane_views=(
                    ResidentLaneLiveView(
                        objective_id=round_bundle.objective.objective_id,
                        lane_id=runtime_round.lane_id,
                        team_id=runtime_round.team_id,
                        delivery_state=DeliveryState(
                            delivery_id=(
                                f"{round_bundle.objective.objective_id}:lane:{runtime_round.lane_id}"
                            ),
                            objective_id=round_bundle.objective.objective_id,
                            kind=DeliveryStateKind.LANE,
                            status=delivery_status,
                            lane_id=runtime_round.lane_id,
                            team_id=runtime_round.team_id,
                            iteration=0 if not lane_completed else 1,
                            summary=(
                                "Runtime lane is waiting on a governed host step."
                                if not lane_completed
                                else "Runtime lane converged after the governed host step."
                            ),
                        ),
                        coordinator_session=replace(
                            coordinator_session,
                            phase=phase,
                        ),
                        coordination_metadata={
                            "mailbox_followup_turns_used": 2,
                            "mailbox_followup_turn_limit": 4,
                        }
                        if not lane_completed
                        else {},
                        task_surface_authority=task_surface_authority,
                        pending_shared_digest_count=0,
                        shared_digest_envelope_ids=(),
                    ),
                ),
                objective_shared_digests=(
                    ()
                    if lane_completed
                    else (
                        MailboxDigest(
                            subscription_id="objective-shared",
                            subscriber=f"superleader:{round_bundle.objective.objective_id}",
                            envelope_id="objective-digest-1",
                            sender="group-a:team:runtime:leader",
                            recipient="group-a:team:runtime:leader",
                            subject="task.result",
                            summary="Objective digest requires another host step.",
                            delivery_mode=MailboxDeliveryMode.SUMMARY_PLUS_REF,
                            kind=MailboxMessageKind.SYSTEM,
                            group_id="group-a",
                            lane_id=runtime_round.lane_id,
                            team_id=runtime_round.team_id,
                            visibility_scope=MailboxVisibilityScope.SHARED,
                        ),
                    )
                ),
                objective_message_runtime={
                    "objective_shared_digest_count": 0 if lane_completed else 1
                },
                objective_coordination={
                    "active_lane_ids": [] if lane_completed else [runtime_round.lane_id]
                },
            )

        async def _step_host_runtime(*, objective, leader_round, session_metadata=None, **kwargs):
            nonlocal lane_completed
            captured_session_metadata.update(dict(session_metadata or {}))
            lane_completed = True
            return LeaderLoopResult(
                leader_round=leader_round,
                delivery_state=DeliveryState(
                    delivery_id=f"{objective.objective_id}:lane:{leader_round.lane_id}",
                    objective_id=objective.objective_id,
                    kind=DeliveryStateKind.LANE,
                    status=DeliveryStatus.COMPLETED,
                    lane_id=leader_round.lane_id,
                    team_id=leader_round.team_id,
                    iteration=1,
                    summary="Runtime lane converged after governed host step.",
                ),
                leader_records=(),
                teammate_records=(),
                coordinator_session=ResidentCoordinatorSession(
                    coordinator_id=leader_round.leader_task.leader_id,
                    role="leader",
                    phase=ResidentCoordinatorPhase.QUIESCENT,
                    objective_id=objective.objective_id,
                    lane_id=leader_round.lane_id,
                    team_id=leader_round.team_id,
                    cycle_count=1,
                    mailbox_poll_count=1,
                    metadata=dict(session_metadata or {}),
                ),
            )

        with patch.object(
            superleader,
            "_read_resident_live_view",
            new=AsyncMock(side_effect=_fake_read_resident_live_view),
        ), patch.object(
            LeaderLoopSupervisor,
            "ensure_or_step_session",
            new=AsyncMock(side_effect=_step_host_runtime),
        ):
            result = await superleader.run_planning_result(
                planning_result,
                config=SuperLeaderConfig(
                    leader_backend="in_process",
                    teammate_backend="in_process",
                    max_leader_turns=1,
                    auto_run_teammates=True,
                    working_dir="/tmp/agent-orchestra",
                ),
            )

        resident_inputs = captured_session_metadata["resident_live_inputs"]
        self.assertEqual(resident_inputs["objective_shared_digest_count"], 1)
        self.assertEqual(resident_inputs["mailbox_followup_turns_used"], 2)
        self.assertEqual(
            resident_inputs["task_surface_authority"]["waiting_request_ids"],
            ["authority-request-1"],
        )
        self.assertTrue(resident_inputs["task_surface_authority"]["authority_waiting"])
        self.assertEqual(result.objective_state.status, DeliveryStatus.COMPLETED)

    async def test_superleader_runtime_finalizes_dead_end_pending_dependencies_without_launching_lanes(self) -> None:
        store = InMemoryOrchestrationStore()
        bus = InMemoryEventBus()
        runner = _ScriptedSuperLeaderRunner()
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
        observation_kernel = _ObservationResidentKernel(max_cycles=1)
        superleader = SuperLeaderRuntime(
            runtime=runtime,
            evaluator=DefaultDeliveryEvaluator(),
            mailbox=InMemoryMailboxBridge(),
            resident_kernel=observation_kernel,
        )

        await runtime.create_group("group-a")
        planning_result = await planner.build_initial_plan(
            ObjectiveTemplate(
                objective_id="obj-observe",
                group_id="group-a",
                title="Observe blocked dependencies",
                description="Ensure the superleader can stay resident when dependencies never resolve.",
                workstreams=(
                    WorkstreamTemplate(
                        workstream_id="alpha",
                        title="Alpha",
                        summary="Alpha lane depends on beta.",
                        team_name="Alpha",
                        depends_on=("beta",),
                    ),
                    WorkstreamTemplate(
                        workstream_id="beta",
                        title="Beta",
                        summary="Beta lane depends on alpha.",
                        team_name="Beta",
                        depends_on=("alpha",),
                    ),
                ),
            )
        )

        result = await superleader.run_planning_result(
            planning_result,
            config=SuperLeaderConfig(
                leader_backend="in_process",
                teammate_backend="in_process",
                max_leader_turns=1,
                auto_run_teammates=True,
                working_dir="/tmp/agent-orchestra",
            ),
        )

        self.assertEqual(len(observation_kernel.cycles), 1)
        cycle = observation_kernel.cycles[0]
        self.assertTrue(cycle.stop)
        self.assertEqual(cycle.phase, ResidentCoordinatorPhase.WAITING_FOR_DEPENDENCIES)
        self.assertEqual(cycle.claimed_task_delta, 0)
        self.assertEqual(len(result.lane_results), 0)
        self.assertEqual(result.objective_state.status, DeliveryStatus.PENDING)

    async def test_superleader_runtime_finalizes_using_full_lane_state_when_pending_lanes_remain(self) -> None:
        store = InMemoryOrchestrationStore()
        bus = InMemoryEventBus()
        runner = _ScriptedSuperLeaderRunner()
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
        observation_kernel = _ObservationResidentKernel(max_cycles=2)
        superleader = SuperLeaderRuntime(
            runtime=runtime,
            evaluator=DefaultDeliveryEvaluator(),
            mailbox=InMemoryMailboxBridge(),
            resident_kernel=observation_kernel,
        )

        await runtime.create_group("group-a")
        planning_result = await planner.build_initial_plan(
            ObjectiveTemplate(
                objective_id="obj-partial-dead-end",
                group_id="group-a",
                title="Partial dead-end dependency graph",
                description="Completed lanes must not hide still-pending dead-end lanes during finalize.",
                workstreams=(
                    WorkstreamTemplate(
                        workstream_id="alpha",
                        title="Alpha",
                        summary="Alpha can complete immediately.",
                        team_name="Alpha",
                    ),
                    WorkstreamTemplate(
                        workstream_id="beta",
                        title="Beta",
                        summary="Beta waits on gamma forever.",
                        team_name="Beta",
                        depends_on=("gamma",),
                    ),
                    WorkstreamTemplate(
                        workstream_id="gamma",
                        title="Gamma",
                        summary="Gamma waits on beta forever.",
                        team_name="Gamma",
                        depends_on=("beta",),
                    ),
                ),
            )
        )

        lane_calls: list[str] = []

        async def fake_completed_lane(*, objective, leader_round, **kwargs):
            lane_calls.append(leader_round.lane_id)
            return LeaderLoopResult(
                leader_round=leader_round,
                delivery_state=DeliveryState(
                    delivery_id=f"{objective.objective_id}:lane:{leader_round.lane_id}",
                    objective_id=objective.objective_id,
                    kind=DeliveryStateKind.LANE,
                    status=DeliveryStatus.COMPLETED,
                    lane_id=leader_round.lane_id,
                    team_id=leader_round.team_id,
                    iteration=1,
                    summary="Alpha completed.",
                ),
                leader_records=(),
                teammate_records=(),
            )

        with patch.object(
            LeaderLoopSupervisor,
            "ensure_or_step_session",
            new=AsyncMock(side_effect=fake_completed_lane),
        ):
            result = await superleader.run_planning_result(
                planning_result,
                config=SuperLeaderConfig(
                    leader_backend="in_process",
                    teammate_backend="in_process",
                    max_leader_turns=1,
                    auto_run_teammates=True,
                    working_dir="/tmp/agent-orchestra",
                ),
            )

        self.assertEqual(lane_calls, ["alpha"])
        self.assertEqual(len(observation_kernel.cycles), 1)
        self.assertTrue(observation_kernel.cycles[-1].stop)
        self.assertEqual(result.coordination_state.completed_lane_ids, ("alpha",))
        self.assertEqual(result.coordination_state.pending_lane_ids, ("beta", "gamma"))
        self.assertEqual(result.objective_state.status, DeliveryStatus.PENDING)

    async def test_superleader_runtime_prefers_waiting_for_mailbox_when_shared_digests_exist(self) -> None:
        store = InMemoryOrchestrationStore()
        bus = InMemoryEventBus()
        runner = _ScriptedSuperLeaderRunner()
        mailbox = ProtocolInMemoryMailboxBridge()
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
        observation_kernel = _ObservationResidentKernel(max_cycles=1)
        superleader = SuperLeaderRuntime(
            runtime=runtime,
            evaluator=DefaultDeliveryEvaluator(),
            mailbox=mailbox,
            resident_kernel=observation_kernel,
        )

        await runtime.create_group("group-a")
        await mailbox.send(
            MailboxEnvelope(
                sender="group-a:team:alpha:leader",
                recipient="group-a:team:alpha:leader",
                subject="task.result",
                group_id="group-a",
                lane_id="alpha",
                team_id="group-a:team:alpha",
                summary="Shared lane digest exists before dependencies resolve.",
                full_text_ref="blackboard:entry-1",
                visibility_scope=MailboxVisibilityScope.SHARED,
            )
        )
        planning_result = await planner.build_initial_plan(
            ObjectiveTemplate(
                objective_id="obj-observe-mailbox",
                group_id="group-a",
                title="Observe shared mailbox digests",
                description="Prefer mailbox waiting when shared digests exist.",
                workstreams=(
                    WorkstreamTemplate(
                        workstream_id="alpha",
                        title="Alpha",
                        summary="Alpha lane depends on beta.",
                        team_name="Alpha",
                        depends_on=("beta",),
                    ),
                    WorkstreamTemplate(
                        workstream_id="beta",
                        title="Beta",
                        summary="Beta lane depends on alpha.",
                        team_name="Beta",
                        depends_on=("alpha",),
                    ),
                ),
            )
        )

        result = await superleader.run_planning_result(
            planning_result,
            config=SuperLeaderConfig(
                leader_backend="in_process",
                teammate_backend="in_process",
                max_leader_turns=1,
                auto_run_teammates=True,
                working_dir="/tmp/agent-orchestra",
            ),
        )

        self.assertEqual(len(observation_kernel.cycles), 1)
        cycle = observation_kernel.cycles[0]
        self.assertEqual(cycle.phase, ResidentCoordinatorPhase.WAITING_FOR_MAILBOX)
        self.assertEqual(cycle.mailbox_poll_delta, 1)
        self.assertEqual(cycle.metadata["objective_shared_digest_count"], 1)
        self.assertEqual(result.objective_state.status, DeliveryStatus.PENDING)

    async def test_superleader_runtime_uses_lane_digest_metadata_to_keep_waiting_for_mailbox(self) -> None:
        store = InMemoryOrchestrationStore()
        bus = InMemoryEventBus()
        runner = _ScriptedSuperLeaderRunner()
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
        observation_kernel = _ObservationResidentKernel(max_cycles=1)
        superleader = SuperLeaderRuntime(
            runtime=runtime,
            evaluator=DefaultDeliveryEvaluator(),
            mailbox=ProtocolInMemoryMailboxBridge(),
            resident_kernel=observation_kernel,
        )

        await runtime.create_group("group-a")
        planning_result = await planner.build_initial_plan(
            ObjectiveTemplate(
                objective_id="obj-lane-digest-wait",
                group_id="group-a",
                title="Lane digest mailbox wait",
                description="Lane digest metadata should keep the superleader waiting for mailbox convergence.",
                workstreams=(
                    WorkstreamTemplate(
                        workstream_id="alpha",
                        title="Alpha",
                        summary="Alpha waits on beta.",
                        team_name="Alpha",
                        depends_on=("beta",),
                    ),
                    WorkstreamTemplate(
                        workstream_id="beta",
                        title="Beta",
                        summary="Beta waits on alpha.",
                        team_name="Beta",
                        depends_on=("alpha",),
                    ),
                ),
            )
        )

        round_bundle = await materialize_planning_result(
            runtime,
            planning_result,
            created_by="lane-digest-mailbox-wait-test",
        )
        alpha_round = next(
            leader_round
            for leader_round in round_bundle.leader_rounds
            if leader_round.lane_id == "alpha"
        )
        await runtime.store.save_delivery_state(
            DeliveryState(
                delivery_id=f"{round_bundle.objective.objective_id}:lane:{alpha_round.lane_id}",
                objective_id=round_bundle.objective.objective_id,
                kind=DeliveryStateKind.LANE,
                status=DeliveryStatus.PENDING,
                lane_id=alpha_round.lane_id,
                team_id=alpha_round.team_id,
                summary="Alpha lane is waiting on shared digest convergence.",
                metadata={
                    "message_runtime": {
                        "pending_shared_digest_count": 1,
                        "shared_digest_envelope_ids": ["alpha-digest-1"],
                    },
                    "mailbox_followup_turns_used": 2,
                    "mailbox_followup_turn_limit": 4,
                },
            )
        )

        result = await superleader.run_planning_result(
            planning_result,
            config=SuperLeaderConfig(
                leader_backend="in_process",
                teammate_backend="in_process",
                max_leader_turns=1,
                auto_run_teammates=True,
                working_dir="/tmp/agent-orchestra",
            ),
        )

        self.assertEqual(len(observation_kernel.cycles), 1)
        cycle = observation_kernel.cycles[0]
        self.assertEqual(cycle.phase, ResidentCoordinatorPhase.WAITING_FOR_MAILBOX)
        self.assertEqual(cycle.mailbox_poll_delta, 1)
        self.assertEqual(cycle.metadata["objective_shared_digest_count"], 0)
        self.assertEqual(cycle.metadata["lane_digest_counts"], {"alpha": 1})
        resident_live_view = result.objective_state.metadata["resident_live_view"]
        self.assertEqual(resident_live_view["lane_digest_counts"], {"alpha": 1})
        self.assertEqual(
            resident_live_view["lane_mailbox_followup_turns"],
            {"alpha": 2},
        )
        self.assertEqual(
            resident_live_view["lane_live_inputs"]["alpha"]["shared_digest_envelope_ids"],
            ["alpha-digest-1"],
        )
        self.assertEqual(result.objective_state.status, DeliveryStatus.PENDING)

    async def test_superleader_runtime_surfaces_task_surface_authority_truth_in_resident_live_view(self) -> None:
        store = InMemoryOrchestrationStore()
        bus = InMemoryEventBus()
        runner = _ScriptedSuperLeaderRunner()
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
        superleader = SuperLeaderRuntime(
            runtime=runtime,
            evaluator=DefaultDeliveryEvaluator(),
            mailbox=InMemoryMailboxBridge(),
        )

        await runtime.create_group("group-a")
        planning_result = await planner.build_initial_plan(
            ObjectiveTemplate(
                objective_id="obj-task-surface-live-view",
                group_id="group-a",
                title="Resident lane live view",
                description="Expose governed task-surface authority truth in resident live view metadata.",
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
        round_bundle = await materialize_planning_result(
            runtime,
            planning_result,
            created_by="task-surface-live-view-test",
        )
        leader_round = round_bundle.leader_rounds[0]
        teammate_id = f"{leader_round.team_id}:teammate:1"
        task = await runtime.submit_task(
            group_id=round_bundle.objective.group_id,
            lane_id=leader_round.lane_id,
            team_id=leader_round.team_id,
            goal="Need task-surface authority before mutation",
            scope=TaskScope.TEAM,
            created_by=leader_round.leader_task.leader_id,
        )
        await runtime.claim_task(
            task_id=task.task_id,
            owner_id=teammate_id,
            claim_source="test.superleader_runtime",
        )
        await store.save_delivery_state(
            DeliveryState(
                delivery_id=f"{round_bundle.objective.objective_id}:lane:{leader_round.lane_id}",
                objective_id=round_bundle.objective.objective_id,
                kind=DeliveryStateKind.LANE,
                status=DeliveryStatus.RUNNING,
                lane_id=leader_round.lane_id,
                team_id=leader_round.team_id,
                summary="Runtime lane has a governed task-surface mutation pending authority.",
                pending_task_ids=(task.task_id,),
                active_task_ids=(task.task_id,),
            )
        )
        authority_commit = await runtime.commit_task_surface_mutation(
            objective_id=round_bundle.objective.objective_id,
            lane_id=leader_round.lane_id,
            team_id=leader_round.team_id,
            task_id=task.task_id,
            actor_id=teammate_id,
            mutation_kind=TaskSurfaceMutationKind.NOT_NEEDED,
            reason="This leader-owned task requires authority before a teammate can mutate it.",
        )
        self.assertIsNotNone(authority_commit.authority_request)

        live_view = await superleader._read_resident_live_view(
            objective_id=round_bundle.objective.objective_id,
            lane_order=(leader_round.lane_id,),
            host_owner_coordinator_id="superleader:task-surface-live-view-test",
            objective_subscriptions=(),
        )
        resident_live_view = _resident_live_view_metadata(
            coordination_state=SuperLeaderCoordinationState(
                coordinator_id="superleader:task-surface-live-view-test",
                objective_id=round_bundle.objective.objective_id,
                max_active_lanes=1,
                batch_count=0,
                lane_states=(
                    SuperLeaderLaneCoordinationState(
                        lane_id=leader_round.lane_id,
                        team_id=leader_round.team_id,
                        status=DeliveryStatus.WAITING_FOR_AUTHORITY,
                    ),
                ),
                pending_lane_ids=(),
                ready_lane_ids=(),
                active_lane_ids=(),
                completed_lane_ids=(),
                blocked_lane_ids=(),
                failed_lane_ids=(),
            ),
            live_view=live_view,
        )
        self.assertEqual(
            resident_live_view["task_surface_authority_lane_ids"],
            ["runtime"],
        )
        self.assertEqual(
            resident_live_view["lane_task_surface_authority_waiting_task_ids"],
            {"runtime": [task.task_id]},
        )
        lane_input = resident_live_view["lane_live_inputs"]["runtime"]
        self.assertEqual(
            lane_input["task_surface_authority"]["task_surface_waiting_task_ids"],
            [task.task_id],
        )
        self.assertEqual(
            lane_input["task_surface_authority"]["mutation_waiting_task_ids"],
            [task.task_id],
        )
        self.assertTrue(lane_input["task_surface_authority"]["authority_waiting"])

    async def test_superleader_runtime_surfaces_lane_shell_attach_truth_in_resident_live_view(self) -> None:
        store = InMemoryOrchestrationStore()
        bus = InMemoryEventBus()
        runner = _ScriptedSuperLeaderRunner()
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
        superleader = SuperLeaderRuntime(
            runtime=runtime,
            evaluator=DefaultDeliveryEvaluator(),
            mailbox=InMemoryMailboxBridge(),
        )

        await runtime.create_group("group-a")
        planning_result = await planner.build_initial_plan(
            ObjectiveTemplate(
                objective_id="obj-shell-live-view",
                group_id="group-a",
                title="Resident shell live view",
                description="Expose lane-level shell attach truth in the superleader resident live view.",
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
        round_bundle = await materialize_planning_result(
            runtime,
            planning_result,
            created_by="shell-live-view-test",
        )
        leader_round = round_bundle.leader_rounds[0]
        leader_session_id = f"{round_bundle.objective.objective_id}:lane:{leader_round.lane_id}:leader:resident"
        shell_metadata = {
            "group_id": round_bundle.objective.group_id,
            "work_session_id": "worksession-shell-live-view",
            "runtime_generation_id": "runtimegeneration-shell-live-view",
        }
        await supervisor.session_host.load_or_create_coordinator_session(
            session_id=leader_session_id,
            coordinator_id=leader_round.leader_task.leader_id,
            objective_id=round_bundle.objective.objective_id,
            lane_id=leader_round.lane_id,
            team_id=leader_round.team_id,
            role="leader",
            host_owner_coordinator_id="superleader:shell-live-view-test",
            runtime_task_id=leader_round.runtime_task.task_id,
            metadata=shell_metadata,
        )
        await supervisor.session_host.bind_session(
            leader_session_id,
            SessionBinding(
                session_id=leader_session_id,
                backend="tmux",
                binding_type="resident",
                transport_locator={"session_name": "ao-runtime", "pane_id": "%7"},
                supervisor_id="supervisor-live",
                lease_id="lease-live",
                lease_expires_at="2026-04-11T12:30:00+00:00",
            ),
        )
        await supervisor.session_host.record_coordinator_session_state(
            leader_session_id,
            coordinator_session=ResidentCoordinatorSession(
                coordinator_id=leader_round.leader_task.leader_id,
                role="leader",
                phase=ResidentCoordinatorPhase.WAITING_FOR_MAILBOX,
                objective_id=round_bundle.objective.objective_id,
                lane_id=leader_round.lane_id,
                team_id=leader_round.team_id,
                cycle_count=2,
                prompt_turn_count=1,
                claimed_task_count=0,
                subordinate_dispatch_count=0,
                mailbox_poll_count=3,
                mailbox_cursor="leader-envelope-attach",
                last_reason="Standing by for mailbox events.",
            ),
            last_progress_at="2026-04-11T12:00:00+00:00",
        )
        await store.save_delivery_state(
            DeliveryState(
                delivery_id=f"{round_bundle.objective.objective_id}:lane:{leader_round.lane_id}",
                objective_id=round_bundle.objective.objective_id,
                kind=DeliveryStateKind.LANE,
                status=DeliveryStatus.RUNNING,
                lane_id=leader_round.lane_id,
                team_id=leader_round.team_id,
                summary="Lane remains resident.",
            )
        )

        live_view = await superleader._read_resident_live_view(
            objective_id=round_bundle.objective.objective_id,
            lane_order=(leader_round.lane_id,),
            host_owner_coordinator_id="superleader:shell-live-view-test",
            objective_subscriptions=(),
        )
        resident_live_view = _resident_live_view_metadata(
            coordination_state=SuperLeaderCoordinationState(
                coordinator_id="superleader:shell-live-view-test",
                objective_id=round_bundle.objective.objective_id,
                max_active_lanes=1,
                batch_count=0,
                lane_states=(
                    SuperLeaderLaneCoordinationState(
                        lane_id=leader_round.lane_id,
                        team_id=leader_round.team_id,
                        status=DeliveryStatus.RUNNING,
                    ),
                ),
                pending_lane_ids=(),
                ready_lane_ids=(),
                active_lane_ids=(leader_round.lane_id,),
                completed_lane_ids=(),
                blocked_lane_ids=(),
                failed_lane_ids=(),
            ),
            live_view=live_view,
        )

        self.assertEqual(
            resident_live_view["lane_shell_statuses"],
            {leader_round.lane_id: "waiting_for_mailbox"},
        )
        self.assertEqual(
            resident_live_view["lane_attach_modes"],
            {leader_round.lane_id: ShellAttachDecisionMode.ATTACHED.value},
        )
        self.assertEqual(
            resident_live_view["lane_attach_targets"],
            {leader_round.lane_id: leader_session_id},
        )
        lane_input = resident_live_view["lane_live_inputs"][leader_round.lane_id]
        self.assertEqual(
            lane_input["resident_team_shell"]["status"],
            "waiting_for_mailbox",
        )
        self.assertEqual(
            lane_input["shell_attach"]["mode"],
            ShellAttachDecisionMode.ATTACHED.value,
        )

    async def test_superleader_runtime_finalizes_when_dependencies_waiting_for_authority(self) -> None:
        store = InMemoryOrchestrationStore()
        bus = InMemoryEventBus()
        runner = _ScriptedSuperLeaderRunner()
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
        superleader = SuperLeaderRuntime(
            runtime=runtime,
            evaluator=DefaultDeliveryEvaluator(),
            mailbox=InMemoryMailboxBridge(),
        )

        await runtime.create_group("group-a")
        planning_result = await planner.build_initial_plan(
            ObjectiveTemplate(
                objective_id="obj-authority-wait",
                group_id="group-a",
                title="Authority wait dependencies",
                description="Ensure superleader finalizes when dependencies wait for authority.",
                workstreams=(
                    WorkstreamTemplate(
                        workstream_id="runtime",
                        title="Runtime",
                        summary="Runtime lane needs authority.",
                        team_name="Runtime",
                        budget_max_teammates=1,
                    ),
                    WorkstreamTemplate(
                        workstream_id="qa",
                        title="QA",
                        summary="QA lane depends on runtime.",
                        team_name="QA",
                        depends_on=("runtime",),
                        budget_max_teammates=1,
                    ),
                ),
            )
        )

        async def fake_waiting_run(*, objective, leader_round, **kwargs):
            return LeaderLoopResult(
                leader_round=leader_round,
                delivery_state=DeliveryState(
                    delivery_id=f"{objective.objective_id}:lane:{leader_round.lane_id}",
                    objective_id=objective.objective_id,
                    kind=DeliveryStateKind.LANE,
                    status=DeliveryStatus.WAITING_FOR_AUTHORITY,
                    lane_id=leader_round.lane_id,
                    team_id=leader_round.team_id,
                    iteration=1,
                    summary="Waiting for authority extension.",
                    metadata={"waiting_for_authority_task_ids": [leader_round.runtime_task.task_id]},
                ),
                leader_records=(),
                teammate_records=(),
            )

        with patch.object(
            LeaderLoopSupervisor,
            "ensure_or_step_session",
            new=AsyncMock(side_effect=fake_waiting_run),
        ):
            result = await superleader.run_planning_result(
                planning_result,
                config=SuperLeaderConfig(
                    leader_backend="in_process",
                    teammate_backend="in_process",
                    max_leader_turns=1,
                    auto_run_teammates=True,
                    working_dir="/tmp/agent-orchestra",
                ),
            )

        self.assertEqual(result.objective_state.status, DeliveryStatus.WAITING_FOR_AUTHORITY)
        self.assertIn("qa", result.coordination_state.pending_lane_ids)
        self.assertTrue(
            any(
                lane_state.status == DeliveryStatus.WAITING_FOR_AUTHORITY
                for lane_state in result.coordination_state.lane_states
            )
        )

    async def test_superleader_runtime_uses_shared_authority_policy_surface_for_escalated_decision(self) -> None:
        store = InMemoryOrchestrationStore()
        bus = InMemoryEventBus()
        runtime = GroupRuntime(
            store=store,
            bus=bus,
            authority_policy=AuthorityPolicy(
                escalated_boundary_actions={
                    AuthorityBoundaryClass.CROSS_TEAM_SHARED: AuthorityPolicyAction.DENY,
                }
            ),
        )
        superleader = SuperLeaderRuntime(runtime=runtime)
        request = ScopeExtensionRequest(
            request_id="auth-req-shared-policy",
            assignment_id="task-a:assignment",
            worker_id="team-a:teammate:1",
            task_id="task-a",
            requested_paths=("resource/knowledge/README.md",),
        )

        self.assertIsNotNone(superleader.authority_root_reactor)
        assert superleader.authority_root_reactor is not None
        decision = superleader.authority_root_reactor._build_authority_decision(
            objective_id="obj-a",
            authority_request=request,
            boundary_class=AuthorityBoundaryClass.CROSS_TEAM_SHARED.value,
            task_authority_decision_payload={
                "decision": "escalate",
                "scope_class": AuthorityBoundaryClass.CROSS_TEAM_SHARED.value,
            },
        )

        self.assertIsNotNone(decision)
        assert decision is not None
        self.assertEqual(decision.decision, AuthorityPolicyAction.DENY.value)
        self.assertEqual(decision.scope_class, AuthorityBoundaryClass.CROSS_TEAM_SHARED.value)

    async def test_superleader_runtime_grants_escalated_protected_authority_and_requeues_lane(self) -> None:
        store = InMemoryOrchestrationStore()
        bus = InMemoryEventBus()
        runner = _ScriptedSuperLeaderRunner()
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
        superleader = SuperLeaderRuntime(
            runtime=runtime,
            evaluator=DefaultDeliveryEvaluator(),
            mailbox=InMemoryMailboxBridge(),
        )

        await runtime.create_group("group-a")
        planning_result = await planner.build_initial_plan(
            ObjectiveTemplate(
                objective_id="obj-authority-grant",
                group_id="group-a",
                title="Authority grant recovery",
                description="Superleader should grant escalated protected authority and resume lane progress.",
                workstreams=(
                    WorkstreamTemplate(
                        workstream_id="runtime",
                        title="Runtime",
                        summary="Runtime lane needs protected authority.",
                        team_name="Runtime",
                        budget_max_teammates=1,
                    ),
                    WorkstreamTemplate(
                        workstream_id="qa",
                        title="QA",
                        summary="QA waits for runtime lane recovery.",
                        team_name="QA",
                        depends_on=("runtime",),
                        budget_max_teammates=1,
                    ),
                ),
            )
        )

        runtime_lane_calls = 0
        authority_task_id: str | None = None

        async def fake_lane_run(*, objective, leader_round, **kwargs):
            nonlocal runtime_lane_calls, authority_task_id
            if leader_round.lane_id == "runtime":
                runtime_lane_calls += 1
                if runtime_lane_calls == 1:
                    teammate_task = await runtime.submit_task(
                        group_id=objective.group_id,
                        team_id=leader_round.team_id,
                        lane_id=leader_round.lane_id,
                        goal="Fix protected runtime blocker",
                        scope=TaskScope.TEAM,
                        created_by=leader_round.leader_task.leader_id,
                        owned_paths=("src/agent_orchestra/runtime/leader_loop.py",),
                    )
                    authority_task_id = teammate_task.task_id
                    request = ScopeExtensionRequest(
                        request_id=f"{teammate_task.task_id}:auth-request",
                        assignment_id=f"{teammate_task.task_id}:assignment",
                        worker_id=f"{leader_round.team_id}:teammate:1",
                        task_id=teammate_task.task_id,
                        requested_paths=("src/agent_orchestra/self_hosting/bootstrap.py",),
                        reason="Need protected runtime scope to complete verification.",
                        evidence="bootstrap import path is out of teammate scope",
                        retry_hint="escalate to superleader",
                    )
                    await runtime.commit_authority_request(
                        objective_id=objective.objective_id,
                        lane_id=leader_round.lane_id,
                        team_id=leader_round.team_id,
                        task_id=teammate_task.task_id,
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
                                    "authority_request": request.to_dict(),
                                    "terminal_status": "blocked",
                                }
                            },
                        ),
                    )
                    await runtime.commit_authority_decision(
                        objective_id=objective.objective_id,
                        lane_id=leader_round.lane_id,
                        team_id=leader_round.team_id,
                        task_id=teammate_task.task_id,
                        actor_id=leader_round.leader_task.leader_id,
                        authority_decision=AuthorityDecision(
                            request_id=request.request_id,
                            decision="escalate",
                            actor_id=leader_round.leader_task.leader_id,
                            scope_class="protected_runtime",
                            escalated_to=f"superleader:{objective.objective_id}",
                            reason="Protected runtime files require superleader authority.",
                            summary="Leader escalated protected runtime authority request.",
                        ),
                    )
                    return LeaderLoopResult(
                        leader_round=leader_round,
                        delivery_state=DeliveryState(
                            delivery_id=f"{objective.objective_id}:lane:{leader_round.lane_id}",
                            objective_id=objective.objective_id,
                            kind=DeliveryStateKind.LANE,
                            status=DeliveryStatus.WAITING_FOR_AUTHORITY,
                            lane_id=leader_round.lane_id,
                            team_id=leader_round.team_id,
                            iteration=1,
                            summary="Waiting for escalated protected authority.",
                            metadata={"waiting_for_authority_task_ids": [teammate_task.task_id]},
                        ),
                        leader_records=(),
                        teammate_records=(),
                    )
                return LeaderLoopResult(
                    leader_round=leader_round,
                    delivery_state=DeliveryState(
                        delivery_id=f"{objective.objective_id}:lane:{leader_round.lane_id}",
                        objective_id=objective.objective_id,
                        kind=DeliveryStateKind.LANE,
                        status=DeliveryStatus.COMPLETED,
                        lane_id=leader_round.lane_id,
                        team_id=leader_round.team_id,
                        iteration=2,
                        summary="Runtime lane resumed and converged after authority grant.",
                    ),
                    leader_records=(),
                    teammate_records=(),
                )
            return LeaderLoopResult(
                leader_round=leader_round,
                delivery_state=DeliveryState(
                    delivery_id=f"{objective.objective_id}:lane:{leader_round.lane_id}",
                    objective_id=objective.objective_id,
                    kind=DeliveryStateKind.LANE,
                    status=DeliveryStatus.COMPLETED,
                    lane_id=leader_round.lane_id,
                    team_id=leader_round.team_id,
                    iteration=1,
                    summary="QA lane converged once runtime dependency completed.",
                ),
                leader_records=(),
                teammate_records=(),
            )

        with patch.object(
            LeaderLoopSupervisor,
            "ensure_or_step_session",
            new=AsyncMock(side_effect=fake_lane_run),
        ):
            result = await superleader.run_planning_result(
                planning_result,
                config=SuperLeaderConfig(
                    leader_backend="in_process",
                    teammate_backend="in_process",
                    max_leader_turns=1,
                    auto_run_teammates=True,
                    working_dir="/tmp/agent-orchestra",
                ),
            )

        self.assertEqual(result.objective_state.status, DeliveryStatus.COMPLETED)
        self.assertEqual(runtime_lane_calls, 2)
        self.assertIsNotNone(authority_task_id)
        assert authority_task_id is not None
        authority_task = await store.get_task(authority_task_id)
        self.assertIsNotNone(authority_task)
        assert authority_task is not None
        self.assertEqual(authority_task.status, TaskStatus.PENDING)
        self.assertEqual(authority_task.authority_decision_payload.get("decision"), "grant")
        self.assertEqual(
            authority_task.authority_decision_payload.get("actor_id"),
            "superleader:obj-authority-grant",
        )
        pending_requests = await runtime.list_pending_authority_requests(
            group_id="group-a",
            lane_id="runtime",
        )
        self.assertEqual(pending_requests, ())

    async def test_superleader_runtime_grants_escalated_protected_task_surface_write_and_applies_update(self) -> None:
        store = InMemoryOrchestrationStore()
        bus = InMemoryEventBus()
        runner = _ScriptedSuperLeaderRunner()
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
        superleader = SuperLeaderRuntime(
            runtime=runtime,
            evaluator=DefaultDeliveryEvaluator(),
            mailbox=InMemoryMailboxBridge(),
        )

        await runtime.create_group("group-a")
        planning_result = await planner.build_initial_plan(
            ObjectiveTemplate(
                objective_id="obj-task-surface-write-grant",
                group_id="group-a",
                title="Task-surface write grant recovery",
                description="Superleader should grant escalated protected task-surface writes and apply the update.",
                workstreams=(
                    WorkstreamTemplate(
                        workstream_id="runtime",
                        title="Runtime",
                        summary="Runtime lane needs protected task-surface authority.",
                        team_name="Runtime",
                        budget_max_teammates=1,
                    ),
                    WorkstreamTemplate(
                        workstream_id="qa",
                        title="QA",
                        summary="QA waits for runtime lane recovery.",
                        team_name="QA",
                        depends_on=("runtime",),
                        budget_max_teammates=1,
                    ),
                ),
            )
        )

        runtime_lane_calls = 0
        authority_task_id: str | None = None

        async def fake_lane_run(*, objective, leader_round, **kwargs):
            nonlocal runtime_lane_calls, authority_task_id
            if leader_round.lane_id == "runtime":
                runtime_lane_calls += 1
                if runtime_lane_calls == 1:
                    teammate_task = await runtime.submit_task(
                        group_id=objective.group_id,
                        team_id=leader_round.team_id,
                        lane_id=leader_round.lane_id,
                        goal="Fix protected task-surface blocker",
                        scope=TaskScope.TEAM,
                        created_by=leader_round.leader_task.leader_id,
                        owned_paths=("src/runtime.py",),
                    )
                    authority_task_id = teammate_task.task_id
                    write_commit = await runtime.commit_task_protected_field_write(
                        objective_id=objective.objective_id,
                        lane_id=leader_round.lane_id,
                        team_id=leader_round.team_id,
                        task_id=teammate_task.task_id,
                        actor_id=f"{leader_round.team_id}:teammate:1",
                        field_updates={
                            "goal": "Protected task-surface write grant updated the runtime blocker.",
                            "owned_paths": ("src/runtime.py", "docs/runtime.md"),
                        },
                        reason="Need protected task-surface authority to update the task contract.",
                    )
                    assert write_commit.authority_request is not None
                    await runtime.commit_authority_decision(
                        objective_id=objective.objective_id,
                        lane_id=leader_round.lane_id,
                        team_id=leader_round.team_id,
                        task_id=teammate_task.task_id,
                        actor_id=leader_round.leader_task.leader_id,
                        authority_decision=AuthorityDecision(
                            request_id=write_commit.authority_request.request_id,
                            decision="escalate",
                            actor_id=leader_round.leader_task.leader_id,
                            scope_class="protected_runtime",
                            escalated_to=f"superleader:{objective.objective_id}",
                            reason="Protected task-surface fields require superleader authority.",
                            summary="Leader escalated protected task-surface write request.",
                        ),
                    )
                    return LeaderLoopResult(
                        leader_round=leader_round,
                        delivery_state=DeliveryState(
                            delivery_id=f"{objective.objective_id}:lane:{leader_round.lane_id}",
                            objective_id=objective.objective_id,
                            kind=DeliveryStateKind.LANE,
                            status=DeliveryStatus.WAITING_FOR_AUTHORITY,
                            lane_id=leader_round.lane_id,
                            team_id=leader_round.team_id,
                            iteration=1,
                            summary="Waiting for escalated protected task-surface write authority.",
                            metadata={"waiting_for_authority_task_ids": [teammate_task.task_id]},
                        ),
                        leader_records=(),
                        teammate_records=(),
                    )
                return LeaderLoopResult(
                    leader_round=leader_round,
                    delivery_state=DeliveryState(
                        delivery_id=f"{objective.objective_id}:lane:{leader_round.lane_id}",
                        objective_id=objective.objective_id,
                        kind=DeliveryStateKind.LANE,
                        status=DeliveryStatus.COMPLETED,
                        lane_id=leader_round.lane_id,
                        team_id=leader_round.team_id,
                        iteration=2,
                        summary="Runtime lane resumed and converged after task-surface write grant.",
                    ),
                    leader_records=(),
                    teammate_records=(),
                )
            return LeaderLoopResult(
                leader_round=leader_round,
                delivery_state=DeliveryState(
                    delivery_id=f"{objective.objective_id}:lane:{leader_round.lane_id}",
                    objective_id=objective.objective_id,
                    kind=DeliveryStateKind.LANE,
                    status=DeliveryStatus.COMPLETED,
                    lane_id=leader_round.lane_id,
                    team_id=leader_round.team_id,
                    iteration=1,
                    summary="QA lane converged after runtime dependency recovered.",
                ),
                leader_records=(),
                teammate_records=(),
            )

        with patch.object(
            LeaderLoopSupervisor,
            "ensure_or_step_session",
            new=AsyncMock(side_effect=fake_lane_run),
        ):
            result = await superleader.run_planning_result(
                planning_result,
                config=SuperLeaderConfig(
                    leader_backend="in_process",
                    teammate_backend="in_process",
                    max_leader_turns=1,
                    auto_run_teammates=True,
                    working_dir="/tmp/agent-orchestra",
                ),
            )

        self.assertEqual(result.objective_state.status, DeliveryStatus.COMPLETED)
        self.assertEqual(runtime_lane_calls, 2)
        self.assertIsNotNone(authority_task_id)
        assert authority_task_id is not None
        authority_task = await store.get_task(authority_task_id)
        self.assertIsNotNone(authority_task)
        assert authority_task is not None
        self.assertEqual(authority_task.status, TaskStatus.PENDING)
        self.assertEqual(
            authority_task.goal,
            "Protected task-surface write grant updated the runtime blocker.",
        )
        self.assertEqual(authority_task.owned_paths, ("src/runtime.py", "docs/runtime.md"))
        self.assertEqual(authority_task.authority_decision_payload.get("decision"), "grant")

    async def test_superleader_runtime_denies_escalated_global_authority_and_blocks_objective(self) -> None:
        store = InMemoryOrchestrationStore()
        bus = InMemoryEventBus()
        runner = _ScriptedSuperLeaderRunner()
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
        superleader = SuperLeaderRuntime(
            runtime=runtime,
            evaluator=DefaultDeliveryEvaluator(),
            mailbox=InMemoryMailboxBridge(),
        )

        await runtime.create_group("group-a")
        planning_result = await planner.build_initial_plan(
            ObjectiveTemplate(
                objective_id="obj-authority-deny",
                group_id="group-a",
                title="Authority deny convergence",
                description="Superleader should deny escalated global authority and finalize objective as blocked.",
                workstreams=(
                    WorkstreamTemplate(
                        workstream_id="runtime",
                        title="Runtime",
                        summary="Runtime lane requests protected contract authority.",
                        team_name="Runtime",
                        budget_max_teammates=1,
                    ),
                    WorkstreamTemplate(
                        workstream_id="qa",
                        title="QA",
                        summary="QA waits for runtime lane.",
                        team_name="QA",
                        depends_on=("runtime",),
                        budget_max_teammates=1,
                    ),
                ),
            )
        )

        runtime_lane_calls = 0
        qa_lane_calls = 0
        authority_task_id: str | None = None

        async def fake_lane_run(*, objective, leader_round, **kwargs):
            nonlocal runtime_lane_calls, qa_lane_calls, authority_task_id
            if leader_round.lane_id == "qa":
                qa_lane_calls += 1
            if leader_round.lane_id != "runtime":
                return LeaderLoopResult(
                    leader_round=leader_round,
                    delivery_state=DeliveryState(
                        delivery_id=f"{objective.objective_id}:lane:{leader_round.lane_id}",
                        objective_id=objective.objective_id,
                        kind=DeliveryStateKind.LANE,
                        status=DeliveryStatus.COMPLETED,
                        lane_id=leader_round.lane_id,
                        team_id=leader_round.team_id,
                        iteration=1,
                        summary="Unexpectedly converged dependency lane.",
                    ),
                    leader_records=(),
                    teammate_records=(),
                )

            runtime_lane_calls += 1
            teammate_task = await runtime.submit_task(
                group_id=objective.group_id,
                team_id=leader_round.team_id,
                lane_id=leader_round.lane_id,
                goal="Fix global contract blocker",
                scope=TaskScope.TEAM,
                created_by=leader_round.leader_task.leader_id,
            )
            authority_task_id = teammate_task.task_id
            request = ScopeExtensionRequest(
                request_id=f"{teammate_task.task_id}:auth-request",
                assignment_id=f"{teammate_task.task_id}:assignment",
                worker_id=f"{leader_round.team_id}:teammate:1",
                task_id=teammate_task.task_id,
                requested_paths=("src/agent_orchestra/contracts/task.py",),
                reason="Need global contract scope.",
                evidence="contract edit is out of leader authority boundary",
                retry_hint="escalate to superleader",
            )
            await runtime.commit_authority_request(
                objective_id=objective.objective_id,
                lane_id=leader_round.lane_id,
                team_id=leader_round.team_id,
                task_id=teammate_task.task_id,
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
                            "authority_request": request.to_dict(),
                            "terminal_status": "blocked",
                        }
                    },
                ),
            )
            await runtime.commit_authority_decision(
                objective_id=objective.objective_id,
                lane_id=leader_round.lane_id,
                team_id=leader_round.team_id,
                task_id=teammate_task.task_id,
                actor_id=leader_round.leader_task.leader_id,
                authority_decision=AuthorityDecision(
                    request_id=request.request_id,
                    decision="escalate",
                    actor_id=leader_round.leader_task.leader_id,
                    scope_class="global_contract",
                    escalated_to=f"superleader:{objective.objective_id}",
                    reason="Global contract path requires root authority.",
                    summary="Leader escalated global contract authority request.",
                ),
            )
            return LeaderLoopResult(
                leader_round=leader_round,
                delivery_state=DeliveryState(
                    delivery_id=f"{objective.objective_id}:lane:{leader_round.lane_id}",
                    objective_id=objective.objective_id,
                    kind=DeliveryStateKind.LANE,
                    status=DeliveryStatus.WAITING_FOR_AUTHORITY,
                    lane_id=leader_round.lane_id,
                    team_id=leader_round.team_id,
                    iteration=1,
                    summary="Waiting for escalated global authority.",
                    metadata={"waiting_for_authority_task_ids": [teammate_task.task_id]},
                ),
                leader_records=(),
                teammate_records=(),
            )

        with patch.object(
            LeaderLoopSupervisor,
            "ensure_or_step_session",
            new=AsyncMock(side_effect=fake_lane_run),
        ):
            result = await superleader.run_planning_result(
                planning_result,
                config=SuperLeaderConfig(
                    leader_backend="in_process",
                    teammate_backend="in_process",
                    max_leader_turns=1,
                    auto_run_teammates=True,
                    working_dir="/tmp/agent-orchestra",
                ),
            )

        self.assertEqual(result.objective_state.status, DeliveryStatus.BLOCKED)
        self.assertEqual(runtime_lane_calls, 1)
        self.assertEqual(qa_lane_calls, 0)
        runtime_lane_state = next(
            lane_state
            for lane_state in result.coordination_state.lane_states
            if lane_state.lane_id == "runtime"
        )
        self.assertEqual(runtime_lane_state.status, DeliveryStatus.BLOCKED)
        self.assertIn("qa", result.coordination_state.pending_lane_ids)
        self.assertIsNotNone(authority_task_id)
        assert authority_task_id is not None
        authority_task = await store.get_task(authority_task_id)
        self.assertIsNotNone(authority_task)
        assert authority_task is not None
        self.assertEqual(authority_task.status, TaskStatus.BLOCKED)
        self.assertEqual(authority_task.authority_decision_payload.get("decision"), "deny")
        self.assertEqual(
            authority_task.authority_decision_payload.get("actor_id"),
            "superleader:obj-authority-deny",
        )
        pending_requests = await runtime.list_pending_authority_requests(
            group_id="group-a",
            lane_id="runtime",
        )
        self.assertEqual(pending_requests, ())

    async def test_superleader_runtime_reroutes_escalated_cross_team_authority_and_requeues_lane(self) -> None:
        store = InMemoryOrchestrationStore()
        bus = InMemoryEventBus()
        runner = _ScriptedSuperLeaderRunner()
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
        superleader = SuperLeaderRuntime(
            runtime=runtime,
            evaluator=DefaultDeliveryEvaluator(),
            mailbox=mailbox,
        )

        await runtime.create_group("group-a")
        planning_result = await planner.build_initial_plan(
            ObjectiveTemplate(
                objective_id="obj-authority-reroute",
                group_id="group-a",
                title="Authority reroute convergence",
                description="Superleader should reroute escalated cross-team authority and resume lane progress.",
                workstreams=(
                    WorkstreamTemplate(
                        workstream_id="runtime",
                        title="Runtime",
                        summary="Runtime lane requests cross-team shared authority.",
                        team_name="Runtime",
                        budget_max_teammates=1,
                    ),
                    WorkstreamTemplate(
                        workstream_id="qa",
                        title="QA",
                        summary="QA waits for runtime lane recovery.",
                        team_name="QA",
                        depends_on=("runtime",),
                        budget_max_teammates=1,
                    ),
                ),
            )
        )

        runtime_lane_calls = 0
        authority_task_id: str | None = None

        async def fake_lane_run(*, objective, leader_round, **kwargs):
            nonlocal runtime_lane_calls, authority_task_id
            if leader_round.lane_id == "runtime":
                runtime_lane_calls += 1
                if runtime_lane_calls == 1:
                    teammate_task = await runtime.submit_task(
                        group_id=objective.group_id,
                        team_id=leader_round.team_id,
                        lane_id=leader_round.lane_id,
                        goal="Fix cross-team shared blocker",
                        scope=TaskScope.TEAM,
                        created_by=leader_round.leader_task.leader_id,
                        owned_paths=("docs/runtime.md",),
                    )
                    authority_task_id = teammate_task.task_id
                    request = ScopeExtensionRequest(
                        request_id=f"{teammate_task.task_id}:auth-request",
                        assignment_id=f"{teammate_task.task_id}:assignment",
                        worker_id=f"{leader_round.team_id}:teammate:1",
                        task_id=teammate_task.task_id,
                        requested_paths=("resource/knowledge/README.md",),
                        reason="Need cross-team shared authority to update shared knowledge index.",
                        evidence="shared knowledge index is outside lane-local ownership",
                        retry_hint="escalate to superleader",
                    )
                    await runtime.commit_authority_request(
                        objective_id=objective.objective_id,
                        lane_id=leader_round.lane_id,
                        team_id=leader_round.team_id,
                        task_id=teammate_task.task_id,
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
                                    "authority_request": request.to_dict(),
                                    "terminal_status": "blocked",
                                }
                            },
                        ),
                    )
                    await runtime.commit_authority_decision(
                        objective_id=objective.objective_id,
                        lane_id=leader_round.lane_id,
                        team_id=leader_round.team_id,
                        task_id=teammate_task.task_id,
                        actor_id=leader_round.leader_task.leader_id,
                        authority_decision=AuthorityDecision(
                            request_id=request.request_id,
                            decision="escalate",
                            actor_id=leader_round.leader_task.leader_id,
                            scope_class="cross_team_shared",
                            escalated_to=f"superleader:{objective.objective_id}",
                            reason="Cross-team shared files require superleader authority.",
                            summary="Leader escalated cross-team shared authority request.",
                        ),
                    )
                    return LeaderLoopResult(
                        leader_round=leader_round,
                        delivery_state=DeliveryState(
                            delivery_id=f"{objective.objective_id}:lane:{leader_round.lane_id}",
                            objective_id=objective.objective_id,
                            kind=DeliveryStateKind.LANE,
                            status=DeliveryStatus.WAITING_FOR_AUTHORITY,
                            lane_id=leader_round.lane_id,
                            team_id=leader_round.team_id,
                            iteration=1,
                            summary="Waiting for escalated cross-team shared authority.",
                            metadata={"waiting_for_authority_task_ids": [teammate_task.task_id]},
                        ),
                        leader_records=(),
                        teammate_records=(),
                    )
                return LeaderLoopResult(
                    leader_round=leader_round,
                    delivery_state=DeliveryState(
                        delivery_id=f"{objective.objective_id}:lane:{leader_round.lane_id}",
                        objective_id=objective.objective_id,
                        kind=DeliveryStateKind.LANE,
                        status=DeliveryStatus.COMPLETED,
                        lane_id=leader_round.lane_id,
                        team_id=leader_round.team_id,
                        iteration=2,
                        summary="Runtime lane resumed and converged after authority reroute.",
                    ),
                    leader_records=(),
                    teammate_records=(),
                )
            return LeaderLoopResult(
                leader_round=leader_round,
                delivery_state=DeliveryState(
                    delivery_id=f"{objective.objective_id}:lane:{leader_round.lane_id}",
                    objective_id=objective.objective_id,
                    kind=DeliveryStateKind.LANE,
                    status=DeliveryStatus.COMPLETED,
                    lane_id=leader_round.lane_id,
                    team_id=leader_round.team_id,
                    iteration=1,
                    summary="QA lane converged after runtime dependency recovered.",
                ),
                leader_records=(),
                teammate_records=(),
            )

        with patch.object(
            LeaderLoopSupervisor,
            "ensure_or_step_session",
            new=AsyncMock(side_effect=fake_lane_run),
        ):
            result = await superleader.run_planning_result(
                planning_result,
                config=SuperLeaderConfig(
                    leader_backend="in_process",
                    teammate_backend="in_process",
                    max_leader_turns=1,
                    auto_run_teammates=True,
                    working_dir="/tmp/agent-orchestra",
                ),
            )

        self.assertEqual(result.objective_state.status, DeliveryStatus.COMPLETED)
        self.assertEqual(runtime_lane_calls, 2)
        self.assertIsNotNone(authority_task_id)
        assert authority_task_id is not None
        authority_task = await store.get_task(authority_task_id)
        self.assertIsNotNone(authority_task)
        assert authority_task is not None
        self.assertEqual(authority_task.status, TaskStatus.CANCELLED)
        self.assertEqual(authority_task.authority_decision_payload.get("decision"), "reroute")
        replacement_task_id = authority_task.superseded_by_task_id
        self.assertIsNotNone(replacement_task_id)
        assert replacement_task_id is not None
        replacement_task = await store.get_task(replacement_task_id)
        self.assertIsNotNone(replacement_task)
        assert replacement_task is not None
        self.assertEqual(replacement_task.status, TaskStatus.PENDING)
        self.assertEqual(replacement_task.derived_from, authority_task_id)
        pending_requests = await runtime.list_pending_authority_requests(
            group_id="group-a",
            lane_id="runtime",
        )
        self.assertEqual(pending_requests, ())

    async def test_superleader_runtime_writes_back_authority_decision_to_subordinate_leader(self) -> None:
        store = InMemoryOrchestrationStore()
        bus = InMemoryEventBus()
        runner = _ScriptedSuperLeaderRunner()
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
        superleader = SuperLeaderRuntime(
            runtime=runtime,
            evaluator=DefaultDeliveryEvaluator(),
            mailbox=mailbox,
        )

        await runtime.create_group("group-a")
        planning_result = await planner.build_initial_plan(
            ObjectiveTemplate(
                objective_id="obj-authority-writeback",
                group_id="group-a",
                title="Authority control writeback",
                description="Superleader should write authority decisions back to subordinate leader control plane.",
                workstreams=(
                    WorkstreamTemplate(
                        workstream_id="runtime",
                        title="Runtime",
                        summary="Runtime lane needs protected authority.",
                        team_name="Runtime",
                        budget_max_teammates=1,
                    ),
                    WorkstreamTemplate(
                        workstream_id="qa",
                        title="QA",
                        summary="QA waits for runtime lane.",
                        team_name="QA",
                        depends_on=("runtime",),
                        budget_max_teammates=1,
                    ),
                ),
            )
        )

        runtime_lane_calls = 0
        runtime_leader_id: str | None = None
        authority_task_id: str | None = None

        async def fake_lane_run(*, objective, leader_round, **kwargs):
            nonlocal runtime_lane_calls, runtime_leader_id, authority_task_id
            if leader_round.lane_id == "runtime":
                runtime_lane_calls += 1
                runtime_leader_id = leader_round.leader_task.leader_id
                if runtime_lane_calls == 1:
                    teammate_task = await runtime.submit_task(
                        group_id=objective.group_id,
                        team_id=leader_round.team_id,
                        lane_id=leader_round.lane_id,
                        goal="Fix protected runtime blocker",
                        scope=TaskScope.TEAM,
                        created_by=leader_round.leader_task.leader_id,
                        owned_paths=("src/agent_orchestra/runtime/leader_loop.py",),
                    )
                    authority_task_id = teammate_task.task_id
                    request = ScopeExtensionRequest(
                        request_id=f"{teammate_task.task_id}:auth-request",
                        assignment_id=f"{teammate_task.task_id}:assignment",
                        worker_id=f"{leader_round.team_id}:teammate:1",
                        task_id=teammate_task.task_id,
                        requested_paths=("src/agent_orchestra/self_hosting/bootstrap.py",),
                        reason="Need protected runtime scope to complete verification.",
                        evidence="bootstrap import path is out of teammate scope",
                        retry_hint="escalate to superleader",
                    )
                    await runtime.commit_authority_request(
                        objective_id=objective.objective_id,
                        lane_id=leader_round.lane_id,
                        team_id=leader_round.team_id,
                        task_id=teammate_task.task_id,
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
                                    "authority_request": request.to_dict(),
                                    "terminal_status": "blocked",
                                }
                            },
                        ),
                    )
                    await runtime.commit_authority_decision(
                        objective_id=objective.objective_id,
                        lane_id=leader_round.lane_id,
                        team_id=leader_round.team_id,
                        task_id=teammate_task.task_id,
                        actor_id=leader_round.leader_task.leader_id,
                        authority_decision=AuthorityDecision(
                            request_id=request.request_id,
                            decision="escalate",
                            actor_id=leader_round.leader_task.leader_id,
                            scope_class="protected_runtime",
                            escalated_to=f"superleader:{objective.objective_id}",
                            reason="Protected runtime files require superleader authority.",
                            summary="Leader escalated protected runtime authority request.",
                        ),
                    )
                    return LeaderLoopResult(
                        leader_round=leader_round,
                        delivery_state=DeliveryState(
                            delivery_id=f"{objective.objective_id}:lane:{leader_round.lane_id}",
                            objective_id=objective.objective_id,
                            kind=DeliveryStateKind.LANE,
                            status=DeliveryStatus.WAITING_FOR_AUTHORITY,
                            lane_id=leader_round.lane_id,
                            team_id=leader_round.team_id,
                            iteration=1,
                            summary="Waiting for escalated protected authority.",
                            metadata={"waiting_for_authority_task_ids": [teammate_task.task_id]},
                        ),
                        leader_records=(),
                        teammate_records=(),
                    )
                return LeaderLoopResult(
                    leader_round=leader_round,
                    delivery_state=DeliveryState(
                        delivery_id=f"{objective.objective_id}:lane:{leader_round.lane_id}",
                        objective_id=objective.objective_id,
                        kind=DeliveryStateKind.LANE,
                        status=DeliveryStatus.COMPLETED,
                        lane_id=leader_round.lane_id,
                        team_id=leader_round.team_id,
                        iteration=2,
                        summary="Runtime lane resumed and converged after authority grant.",
                    ),
                    leader_records=(),
                    teammate_records=(),
                )
            return LeaderLoopResult(
                leader_round=leader_round,
                delivery_state=DeliveryState(
                    delivery_id=f"{objective.objective_id}:lane:{leader_round.lane_id}",
                    objective_id=objective.objective_id,
                    kind=DeliveryStateKind.LANE,
                    status=DeliveryStatus.COMPLETED,
                    lane_id=leader_round.lane_id,
                    team_id=leader_round.team_id,
                    iteration=1,
                    summary="QA lane converged once runtime dependency completed.",
                ),
                leader_records=(),
                teammate_records=(),
            )

        with patch.object(
            LeaderLoopSupervisor,
            "ensure_or_step_session",
            new=AsyncMock(side_effect=fake_lane_run),
        ):
            result = await superleader.run_planning_result(
                planning_result,
                config=SuperLeaderConfig(
                    leader_backend="in_process",
                    teammate_backend="in_process",
                    max_leader_turns=1,
                    auto_run_teammates=True,
                    working_dir="/tmp/agent-orchestra",
                ),
            )

        self.assertEqual(result.objective_state.status, DeliveryStatus.COMPLETED)
        self.assertEqual(runtime_lane_calls, 2)
        self.assertIsNotNone(authority_task_id)
        self.assertIsNotNone(runtime_leader_id)
        assert authority_task_id is not None
        assert runtime_leader_id is not None
        authority_task = await store.get_task(authority_task_id)
        self.assertIsNotNone(authority_task)
        assert authority_task is not None
        self.assertEqual(authority_task.authority_decision_payload.get("decision"), "grant")
        control_messages = await mailbox.list_for_recipient(runtime_leader_id)
        decision_messages = [
            message for message in control_messages if message.subject == "authority.decision"
        ]
        self.assertEqual(len(decision_messages), 1)
        decision_message = decision_messages[0]
        self.assertEqual(decision_message.visibility_scope, MailboxVisibilityScope.CONTROL_PRIVATE)
        self.assertEqual(decision_message.delivery_mode, MailboxDeliveryMode.SUMMARY_PLUS_REF)
        self.assertEqual(decision_message.payload.get("task_id"), authority_task_id)
        self.assertEqual(
            decision_message.payload.get("authority_decision", {}).get("decision"),
            "grant",
        )
        self.assertTrue(decision_message.payload.get("authority_request"))
        self.assertIsNotNone(result.coordinator_session)
        assert result.coordinator_session is not None
        metadata = result.coordinator_session.metadata
        request_id = f"{authority_task_id}:auth-request"
        self.assertEqual(metadata.get("authority_reactor_role"), "objective_root")
        self.assertIsInstance(metadata.get("authority_reactor_last_cycle_at"), str)
        datetime.fromisoformat(str(metadata["authority_reactor_last_cycle_at"]))
        self.assertIn(
            request_id,
            metadata.get("authority_reactor_pending_request_ids", []),
        )
        self.assertIn(
            request_id,
            metadata.get("authority_reactor_decision_request_ids", []),
        )
        self.assertIn(
            request_id,
            metadata.get("authority_reactor_escalated_request_ids", []),
        )
        self.assertIn(
            request_id,
            metadata.get("authority_reactor_forwarded_request_ids", []),
        )
        self.assertEqual(
            metadata.get("authority_reactor_incomplete_request_ids"),
            [],
        )
