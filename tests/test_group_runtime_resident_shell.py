from __future__ import annotations

import sys
from tempfile import TemporaryDirectory
from pathlib import Path
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agent_orchestra.bus.in_memory import InMemoryEventBus
from agent_orchestra.contracts.agent import SessionBinding
from agent_orchestra.contracts.execution import ResidentCoordinatorPhase, ResidentCoordinatorSession
from agent_orchestra.contracts.session_continuity import ShellAttachDecisionMode
from agent_orchestra.planning.template import ObjectiveTemplate, WorkstreamTemplate
from agent_orchestra.planning.template_planner import TemplatePlanner
from agent_orchestra.runtime.bootstrap_round import materialize_planning_result
from agent_orchestra.runtime.group_runtime import GroupRuntime
from agent_orchestra.runtime.session_host import StoreBackedResidentSessionHost
from agent_orchestra.runtime.teammate_runtime import ResidentTeammateRunResult
from agent_orchestra.runtime.teammate_work_surface import TeammateWorkSurface
from agent_orchestra.runtime.worker_supervisor import DefaultWorkerSupervisor
from agent_orchestra.storage.in_memory import InMemoryOrchestrationStore
from agent_orchestra.tools.permission_protocol import PermissionDecision, PermissionRequest
from agent_orchestra.runtime.protocol_bridge import InMemoryMailboxBridge


class GroupRuntimeResidentShellTest(IsolatedAsyncioTestCase):
    async def test_attach_session_returns_attached_for_live_shell(self) -> None:
        store = InMemoryOrchestrationStore()
        session_host = StoreBackedResidentSessionHost(store)
        runtime = GroupRuntime(
            store=store,
            bus=InMemoryEventBus(),
            supervisor=DefaultWorkerSupervisor(
                store=store,
                launch_backends={},
                session_host=session_host,
            ),
        )
        continuity = await runtime.new_session(
            group_id="group-a",
            objective_id="objective-1",
            title="Resident attach",
        )
        shell_metadata = {
            "group_id": "group-a",
            "work_session_id": continuity.work_session.work_session_id,
            "runtime_generation_id": continuity.runtime_generation.runtime_generation_id,
        }
        leader_session_id = "objective-1:lane-runtime:leader:resident"
        await session_host.load_or_create_coordinator_session(
            session_id=leader_session_id,
            coordinator_id="leader:runtime",
            objective_id="objective-1",
            lane_id="lane-runtime",
            team_id="team-runtime",
            role="leader",
            host_owner_coordinator_id="superleader:objective-1",
            runtime_task_id="runtime-task-1",
            metadata=shell_metadata,
        )
        await session_host.bind_session(
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
        await session_host.record_coordinator_session_state(
            leader_session_id,
            coordinator_session=ResidentCoordinatorSession(
                coordinator_id="leader:runtime",
                role="leader",
                phase=ResidentCoordinatorPhase.WAITING_FOR_MAILBOX,
                objective_id="objective-1",
                lane_id="lane-runtime",
                team_id="team-runtime",
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

        result = await runtime.attach_session(continuity.work_session.work_session_id)

        self.assertEqual(result.action, "attached")
        self.assertEqual(result.decision.mode, ShellAttachDecisionMode.ATTACHED)
        self.assertEqual(result.metadata["preferred_session_id"], leader_session_id)

    async def test_inspect_session_includes_resident_shell_views(self) -> None:
        store = InMemoryOrchestrationStore()
        session_host = StoreBackedResidentSessionHost(store)
        runtime = GroupRuntime(
            store=store,
            bus=InMemoryEventBus(),
            supervisor=DefaultWorkerSupervisor(
                store=store,
                launch_backends={},
                session_host=session_host,
            ),
        )
        continuity = await runtime.new_session(
            group_id="group-a",
            objective_id="objective-1",
            title="Resident inspect",
        )
        shell_metadata = {
            "group_id": "group-a",
            "work_session_id": continuity.work_session.work_session_id,
            "runtime_generation_id": continuity.runtime_generation.runtime_generation_id,
        }
        leader_session_id = "objective-1:lane-runtime:leader:resident"
        await session_host.load_or_create_coordinator_session(
            session_id=leader_session_id,
            coordinator_id="leader:runtime",
            objective_id="objective-1",
            lane_id="lane-runtime",
            team_id="team-runtime",
            role="leader",
            host_owner_coordinator_id="superleader:objective-1",
            runtime_task_id="runtime-task-1",
            metadata=shell_metadata,
        )
        await session_host.bind_session(
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
        await session_host.record_coordinator_session_state(
            leader_session_id,
            coordinator_session=ResidentCoordinatorSession(
                coordinator_id="leader:runtime",
                role="leader",
                phase=ResidentCoordinatorPhase.WAITING_FOR_MAILBOX,
                objective_id="objective-1",
                lane_id="lane-runtime",
                team_id="team-runtime",
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

        snapshot = await runtime.inspect_session(continuity.work_session.work_session_id)

        self.assertEqual(len(snapshot.resident_shell_views), 1)
        shell_view = snapshot.resident_shell_views[0]
        self.assertEqual(shell_view["status"], "waiting_for_mailbox")
        self.assertEqual(
            shell_view["attach_recommendation"]["mode"],
            ShellAttachDecisionMode.ATTACHED.value,
        )
        self.assertEqual(shell_view["wake_capability"], "already_attached")

    async def test_wake_session_requests_wake_for_quiescent_shell(self) -> None:
        store = InMemoryOrchestrationStore()
        session_host = StoreBackedResidentSessionHost(store)
        runtime = GroupRuntime(
            store=store,
            bus=InMemoryEventBus(),
            supervisor=DefaultWorkerSupervisor(
                store=store,
                launch_backends={},
                session_host=session_host,
            ),
        )
        continuity = await runtime.new_session(
            group_id="group-a",
            objective_id="objective-1",
            title="Resident wake",
        )
        generation = await store.get_runtime_generation(
            continuity.runtime_generation.runtime_generation_id
        )
        assert generation is not None
        await store.save_runtime_generation(
            generation.__class__.from_payload(
                {
                    **generation.to_dict(),
                    "status": "quiescent",
                }
            )
        )
        shell_metadata = {
            "group_id": "group-a",
            "work_session_id": continuity.work_session.work_session_id,
            "runtime_generation_id": continuity.runtime_generation.runtime_generation_id,
        }
        leader_session_id = "objective-1:lane-runtime:leader:resident"
        await session_host.load_or_create_coordinator_session(
            session_id=leader_session_id,
            coordinator_id="leader:runtime",
            objective_id="objective-1",
            lane_id="lane-runtime",
            team_id="team-runtime",
            role="leader",
            host_owner_coordinator_id="superleader:objective-1",
            runtime_task_id="runtime-task-1",
            metadata=shell_metadata,
        )
        await session_host.record_coordinator_session_state(
            leader_session_id,
            coordinator_session=ResidentCoordinatorSession(
                coordinator_id="leader:runtime",
                role="leader",
                phase=ResidentCoordinatorPhase.QUIESCENT,
                objective_id="objective-1",
                lane_id="lane-runtime",
                team_id="team-runtime",
                cycle_count=1,
                prompt_turn_count=1,
                claimed_task_count=0,
                subordinate_dispatch_count=0,
                mailbox_poll_count=1,
                mailbox_cursor="leader-envelope-quiescent",
                last_reason="Resident shell is quiescent.",
            ),
            last_progress_at="2026-04-11T12:00:00+00:00",
        )

        result = await runtime.wake_session(continuity.work_session.work_session_id)

        self.assertEqual(result.action, "woken")
        self.assertEqual(result.decision.mode, ShellAttachDecisionMode.WOKEN)
        self.assertEqual(
            result.metadata["wake_requested_session_ids"],
            [leader_session_id],
        )

    async def test_attach_session_rejects_pending_attach_approval(self) -> None:
        store = InMemoryOrchestrationStore()
        session_host = StoreBackedResidentSessionHost(store)
        runtime = GroupRuntime(
            store=store,
            bus=InMemoryEventBus(),
            supervisor=DefaultWorkerSupervisor(
                store=store,
                launch_backends={},
                session_host=session_host,
            ),
        )
        continuity = await runtime.new_session(
            group_id="group-a",
            objective_id="objective-1",
            title="Resident attach approval",
        )
        shell_metadata = {
            "group_id": "group-a",
            "work_session_id": continuity.work_session.work_session_id,
            "runtime_generation_id": continuity.runtime_generation.runtime_generation_id,
        }
        leader_session_id = "objective-1:lane-runtime:leader:resident"
        await session_host.load_or_create_coordinator_session(
            session_id=leader_session_id,
            coordinator_id="leader:runtime",
            objective_id="objective-1",
            lane_id="lane-runtime",
            team_id="team-runtime",
            role="leader",
            host_owner_coordinator_id="superleader:objective-1",
            runtime_task_id="runtime-task-1",
            metadata=shell_metadata,
        )
        await session_host.bind_session(
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
        await session_host.record_coordinator_session_state(
            leader_session_id,
            coordinator_session=ResidentCoordinatorSession(
                coordinator_id="leader:runtime",
                role="leader",
                phase=ResidentCoordinatorPhase.WAITING_FOR_MAILBOX,
                objective_id="objective-1",
                lane_id="lane-runtime",
                team_id="team-runtime",
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
        await session_host.record_resident_shell_approval(
            approval_kind="attach",
            status="pending",
            request=PermissionRequest(
                requester="session.attach",
                action="resident.attach",
                rationale="Attach to live resident shell.",
                group_id="group-a",
                objective_id="objective-1",
                team_id="team-runtime",
                lane_id="lane-runtime",
            ),
            requested_by="session.attach",
            objective_id="objective-1",
            lane_id="lane-runtime",
            team_id="team-runtime",
            target_session_id=leader_session_id,
            target_mode="attached",
        )

        result = await runtime.attach_session(continuity.work_session.work_session_id)

        self.assertEqual(result.action, "rejected")
        self.assertEqual(result.decision.mode, ShellAttachDecisionMode.REJECTED)
        self.assertEqual(result.metadata["approval_status"], "pending")

    async def test_read_resident_lane_live_views_includes_host_projected_shell_and_attach_decision(
        self,
    ) -> None:
        store = InMemoryOrchestrationStore()
        session_host = StoreBackedResidentSessionHost(store)
        runtime = GroupRuntime(
            store=store,
            bus=InMemoryEventBus(),
            supervisor=DefaultWorkerSupervisor(
                store=store,
                launch_backends={},
                session_host=session_host,
            ),
        )
        await runtime.create_group("group-a")
        await runtime.create_objective(
            group_id="group-a",
            objective_id="objective-1",
            title="Resident lane shell",
            description="Expose host-projected shell state.",
        )
        continuity = await runtime.new_session(
            group_id="group-a",
            objective_id="objective-1",
            title="Resident lane live view",
        )
        shell_metadata = {
            "group_id": "group-a",
            "work_session_id": continuity.work_session.work_session_id,
            "runtime_generation_id": continuity.runtime_generation.runtime_generation_id,
        }
        leader_session_id = "objective-1:lane-runtime:leader:resident"
        teammate_slot_2 = "team-runtime:teammate:2:resident"
        teammate_slot_1 = "team-runtime:teammate:1:resident"

        await session_host.load_or_create_slot_session(
            session_id=teammate_slot_2,
            agent_id="team-runtime:teammate:2",
            objective_id="objective-1",
            lane_id="lane-runtime",
            team_id="team-runtime",
            metadata=shell_metadata,
        )
        await session_host.load_or_create_slot_session(
            session_id=teammate_slot_1,
            agent_id="team-runtime:teammate:1",
            objective_id="objective-1",
            lane_id="lane-runtime",
            team_id="team-runtime",
            metadata=shell_metadata,
        )
        await session_host.load_or_create_coordinator_session(
            session_id=leader_session_id,
            coordinator_id="leader:runtime",
            objective_id="objective-1",
            lane_id="lane-runtime",
            team_id="team-runtime",
            role="leader",
            host_owner_coordinator_id="superleader:objective-1",
            runtime_task_id="runtime-task-1",
            metadata=shell_metadata,
        )
        await session_host.bind_session(
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
        await session_host.record_coordinator_session_state(
            leader_session_id,
            coordinator_session=ResidentCoordinatorSession(
                coordinator_id="leader:runtime",
                role="leader",
                phase=ResidentCoordinatorPhase.WAITING_FOR_MAILBOX,
                objective_id="objective-1",
                lane_id="lane-runtime",
                team_id="team-runtime",
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

        live_views = await runtime.read_resident_lane_live_views(
            objective_id="objective-1",
            lane_ids=("lane-runtime",),
        )

        self.assertEqual(len(live_views), 1)
        live_view = live_views[0]
        self.assertIsNotNone(live_view.resident_team_shell)
        assert live_view.resident_team_shell is not None
        self.assertEqual(
            live_view.resident_team_shell.leader_slot_session_id,
            leader_session_id,
        )
        self.assertEqual(
            live_view.resident_team_shell.teammate_slot_session_ids,
            [teammate_slot_1, teammate_slot_2],
        )
        self.assertIsNotNone(live_view.shell_attach_decision)
        assert live_view.shell_attach_decision is not None
        self.assertEqual(live_view.shell_attach_decision.mode, ShellAttachDecisionMode.ATTACHED)
        self.assertEqual(
            live_view.shell_attach_decision.metadata["preferred_session_id"],
            leader_session_id,
        )

    async def test_run_resident_teammate_host_sweep_builds_host_owned_surface_without_leader_loop_state(
        self,
    ) -> None:
        store = InMemoryOrchestrationStore()
        session_host = StoreBackedResidentSessionHost(store)
        runtime = GroupRuntime(
            store=store,
            bus=InMemoryEventBus(),
            supervisor=DefaultWorkerSupervisor(
                store=store,
                launch_backends={},
                session_host=session_host,
            ),
        )
        planner = TemplatePlanner()
        mailbox = InMemoryMailboxBridge()

        await runtime.create_group("group-a")
        planning_result = await planner.build_initial_plan(
            ObjectiveTemplate(
                objective_id="objective-host-sweep",
                group_id="group-a",
                title="Resident teammate host sweep",
                description="Expose a standalone runtime facade for host-owned teammate continuation.",
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
        expected = ResidentTeammateRunResult(claimed_task_ids=("task-1",))
        observed: dict[str, object] = {}

        async def _allow(_request) -> PermissionDecision:
            return PermissionDecision(
                approved=True,
                reviewer="system.auto",
                reason="Automatically approved.",
            )

        async def _step_runnable_host_slots(
            self,
            *,
            request_permission,
            resident_kernel=None,
            keep_session_idle=False,
            execution_policy=None,
        ):
            observed["surface"] = {
                "runtime": self.runtime,
                "mailbox": self.mailbox,
                "objective": self.objective,
                "leader_round": self.leader_round,
                "backend": self.backend,
                "working_dir": self.working_dir,
                "turn_index": self.turn_index,
                "role_profile": self.role_profile,
                "session_host": self.session_host,
                "request_permission": request_permission,
                "resident_kernel": resident_kernel,
                "keep_session_idle": keep_session_idle,
                "execution_policy": execution_policy,
            }
            return expected

        with TemporaryDirectory() as tmpdir:
            with patch.object(
                TeammateWorkSurface,
                "step_runnable_host_slots",
                new=_step_runnable_host_slots,
            ):
                result = await runtime.run_resident_teammate_host_sweep(
                    mailbox=mailbox,
                    objective=bundle.objective,
                    leader_round=leader_round,
                    request_permission=_allow,
                    keep_session_idle=True,
                    turn_index=2,
                    working_dir=tmpdir,
                )

        self.assertIs(result, expected)
        self.assertIn("surface", observed)
        surface = observed["surface"]
        self.assertIs(surface["runtime"], runtime)
        self.assertIs(surface["mailbox"], mailbox)
        self.assertEqual(surface["objective"], bundle.objective)
        self.assertEqual(surface["leader_round"], leader_round)
        self.assertIsNone(surface["backend"])
        self.assertIsNone(surface["working_dir"])
        self.assertEqual(surface["turn_index"], 2)
        self.assertIsNone(surface["role_profile"])
        self.assertIs(surface["session_host"], session_host)
        self.assertIs(surface["request_permission"], _allow)
        self.assertIsNone(surface["resident_kernel"])
        self.assertTrue(surface["keep_session_idle"])
        self.assertIsNone(surface["execution_policy"])
