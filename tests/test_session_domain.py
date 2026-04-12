from __future__ import annotations

import sys
from unittest import IsolatedAsyncioTestCase

ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agent_orchestra.contracts.agent import SessionBinding
from agent_orchestra.contracts.execution import ResidentCoordinatorPhase, ResidentCoordinatorSession
from agent_orchestra.contracts.session_continuity import ShellAttachDecisionMode
from agent_orchestra.runtime.session_domain import SessionDomainService
from agent_orchestra.runtime.session_host import StoreBackedResidentSessionHost
from agent_orchestra.runtime.worker_supervisor import DefaultWorkerSupervisor
from agent_orchestra.storage.in_memory import InMemoryOrchestrationStore
from agent_orchestra.tools.permission_protocol import PermissionRequest


class SessionDomainServiceTest(IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.store = InMemoryOrchestrationStore()
        self.session_host = StoreBackedResidentSessionHost(self.store)
        self.supervisor = DefaultWorkerSupervisor(
            store=self.store,
            launch_backends={},
            session_host=self.session_host,
        )
        self.service = SessionDomainService(
            store=self.store,
            supervisor=self.supervisor,
        )

    async def test_attach_session_returns_attached_for_live_shell(self) -> None:
        continuity = await self.service.new_session(
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
        await self.session_host.load_or_create_coordinator_session(
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
        await self.session_host.bind_session(
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
        await self.session_host.record_coordinator_session_state(
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

        result = await self.service.attach_session(continuity.work_session.work_session_id)

        self.assertEqual(result.action, "attached")
        self.assertEqual(result.decision.mode, ShellAttachDecisionMode.ATTACHED)
        self.assertEqual(result.metadata["preferred_session_id"], leader_session_id)

    async def test_inspect_session_includes_resident_shell_views(self) -> None:
        continuity = await self.service.new_session(
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
        await self.session_host.load_or_create_coordinator_session(
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
        await self.session_host.bind_session(
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
        await self.session_host.record_coordinator_session_state(
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

        snapshot = await self.service.inspect_session(continuity.work_session.work_session_id)

        self.assertEqual(len(snapshot.resident_shell_views), 1)
        shell_view = snapshot.resident_shell_views[0]
        self.assertEqual(shell_view["status"], "waiting_for_mailbox")
        self.assertEqual(
            shell_view["attach_recommendation"]["mode"],
            ShellAttachDecisionMode.ATTACHED.value,
        )
        self.assertEqual(shell_view["wake_capability"], "already_attached")

    async def test_attach_session_rejects_pending_attach_approval(self) -> None:
        continuity = await self.service.new_session(
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
        await self.session_host.load_or_create_coordinator_session(
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
        await self.session_host.bind_session(
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
        await self.session_host.record_coordinator_session_state(
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
        await self.session_host.record_resident_shell_approval(
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

        result = await self.service.attach_session(continuity.work_session.work_session_id)

        self.assertEqual(result.action, "rejected")
        self.assertEqual(result.decision.mode, ShellAttachDecisionMode.REJECTED)
        self.assertEqual(result.metadata["approval_status"], "pending")

    async def test_wake_session_requests_wake_for_quiescent_shell(self) -> None:
        continuity = await self.service.new_session(
            group_id="group-a",
            objective_id="objective-1",
            title="Resident wake",
        )
        generation = await self.store.get_runtime_generation(
            continuity.runtime_generation.runtime_generation_id
        )
        assert generation is not None
        await self.store.save_runtime_generation(
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
        await self.session_host.load_or_create_coordinator_session(
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
        await self.session_host.record_coordinator_session_state(
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

        result = await self.service.wake_session(continuity.work_session.work_session_id)

        self.assertEqual(result.action, "woken")
        self.assertEqual(result.decision.mode, ShellAttachDecisionMode.WOKEN)
        self.assertEqual(
            result.metadata["wake_requested_session_ids"],
            [leader_session_id],
        )
        latest_inspection = await self.service.inspect_session(
            continuity.work_session.work_session_id
        )
        self.assertEqual(
            latest_inspection.resident_shell_views[0]["attach_recommendation"]["mode"],
            ShellAttachDecisionMode.WOKEN.value,
        )
        self.assertEqual(
            latest_inspection.session_events[-1].event_kind,
            "resident_shell_wake_requested",
        )
