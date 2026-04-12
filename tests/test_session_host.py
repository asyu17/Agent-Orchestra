from __future__ import annotations

import datetime
import dataclasses
import sys
from pathlib import Path
from unittest import IsolatedAsyncioTestCase

if not hasattr(datetime, "UTC"):
    from datetime import timezone

    datetime.UTC = timezone.utc

if sys.version_info < (3, 10):
    _original_dataclass = dataclasses.dataclass

    def _dataclass(_cls=None, **kwargs):
        kwargs.pop("slots", None)
        if _cls is None:
            return lambda cls: _original_dataclass(cls, **kwargs)
        return _original_dataclass(_cls, **kwargs)

    dataclasses.dataclass = _dataclass

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agent_orchestra.contracts.agent import (
    AgentSession,
    CoordinatorSessionState,
    SessionBinding,
    TeammateActivationProfile,
)
from agent_orchestra.contracts.execution import (
    ResidentCoordinatorPhase,
    ResidentCoordinatorSession,
    WorkerSession,
    WorkerSessionStatus,
)
from agent_orchestra.contracts.session_continuity import (
    ConversationHeadKind,
    ResidentTeamShell,
    ResidentTeamShellStatus,
    ShellAttachDecisionMode,
)
from agent_orchestra.contracts.session_memory import AgentTurnKind, ArtifactRefKind, ToolInvocationKind
from agent_orchestra.runtime.session_host import InMemoryResidentSessionHost, StoreBackedResidentSessionHost
from agent_orchestra.storage.in_memory import InMemoryOrchestrationStore
from agent_orchestra.tools.permission_protocol import PermissionRequest


class SessionHostTest(IsolatedAsyncioTestCase):
    @staticmethod
    def _assert_iso_timestamp(value: object) -> str:
        assert isinstance(value, str)
        parsed = datetime.datetime.fromisoformat(value)
        assert parsed.tzinfo is not None
        return value

    async def test_register_and_load_session(self) -> None:
        host = InMemoryResidentSessionHost()
        session = AgentSession(
            session_id="session-1",
            agent_id="leader-1",
            role="leader",
            phase=ResidentCoordinatorPhase.BOOTING,
            objective_id="objective-1",
        )

        await host.save_session(session)
        loaded = await host.get_session("session-1")

        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.agent_id, "leader-1")
        self.assertEqual(loaded.phase, ResidentCoordinatorPhase.BOOTING)

    async def test_bind_transport_updates_current_binding(self) -> None:
        host = InMemoryResidentSessionHost()
        session = AgentSession(
            session_id="session-2",
            agent_id="teammate-1",
            role="teammate",
            phase=ResidentCoordinatorPhase.IDLE,
        )
        await host.save_session(session)

        binding = SessionBinding(
            session_id="session-2",
            backend="tmux",
            binding_type="resident",
            transport_locator={"session_name": "ao-team-a", "pane_id": "%1"},
            supervisor_id="supervisor-a",
            lease_id="lease-a",
            lease_expires_at="2026-04-05T12:00:00+00:00",
        )
        updated = await host.bind_session("session-2", binding)

        self.assertIsNotNone(updated.current_binding)
        self.assertEqual(updated.current_binding.backend, "tmux")
        self.assertEqual(updated.current_binding.transport_locator["pane_id"], "%1")

    async def test_mark_phase_preserves_binding_and_cursors(self) -> None:
        host = InMemoryResidentSessionHost()
        session = AgentSession(
            session_id="session-3",
            agent_id="teammate-2",
            role="teammate",
            phase=ResidentCoordinatorPhase.RUNNING,
            mailbox_cursor={"mailbox": {"offset": "10-0"}},
            subscription_cursors={"mailbox": {"offset": "10-0", "event_id": "evt-10"}},
            current_binding=SessionBinding(
                session_id="session-3",
                backend="codex_cli",
                binding_type="oneshot",
                transport_locator={"pid": 123},
            ),
        )
        await host.save_session(session)

        updated = await host.mark_phase(
            "session-3",
            ResidentCoordinatorPhase.QUIESCENT,
            reason="all work drained",
        )

        self.assertEqual(updated.phase, ResidentCoordinatorPhase.QUIESCENT)
        self.assertEqual(updated.last_reason, "all work drained")
        self.assertEqual(updated.mailbox_cursor["mailbox"]["offset"], "10-0")
        self.assertEqual(updated.current_binding.transport_locator["pid"], 123)

    async def test_reclaim_expired_binding_reassigns_supervisor_lease(self) -> None:
        host = InMemoryResidentSessionHost()
        session = AgentSession(
            session_id="session-4",
            agent_id="leader-4",
            role="leader",
            phase=ResidentCoordinatorPhase.WAITING_FOR_SUBORDINATES,
            current_binding=SessionBinding(
                session_id="session-4",
                backend="tmux",
                binding_type="resident",
                supervisor_id="supervisor-old",
                lease_id="lease-old",
                lease_expires_at="2026-04-05T10:00:00+00:00",
            ),
        )
        await host.save_session(session)

        reclaimed = await host.reclaim_session(
            "session-4",
            new_supervisor_id="supervisor-new",
            new_lease_id="lease-new",
            new_expires_at="2026-04-05T11:00:00+00:00",
        )

        self.assertIsNotNone(reclaimed.current_binding)
        self.assertEqual(reclaimed.current_binding.supervisor_id, "supervisor-new")
        self.assertEqual(reclaimed.current_binding.lease_id, "lease-new")
        self.assertEqual(reclaimed.current_binding.lease_expires_at, "2026-04-05T11:00:00+00:00")

    async def test_save_session_returns_independent_copy(self) -> None:
        host = InMemoryResidentSessionHost()
        session = AgentSession(
            session_id="session-5",
            agent_id="teammate-5",
            role="teammate",
            phase=ResidentCoordinatorPhase.RUNNING,
            current_binding=SessionBinding(
                session_id="session-5",
                backend="codex_cli",
                binding_type="resident",
            ),
        )

        saved = await host.save_session(session)
        saved.current_binding.backend = "tmux"
        reloaded = await host.load_session("session-5")

        self.assertIsNotNone(reloaded)
        assert reloaded is not None
        self.assertEqual(reloaded.current_binding.backend, "codex_cli")

    async def test_register_update_bind_and_snapshot_helpers(self) -> None:
        host = InMemoryResidentSessionHost()
        await host.register_session(
            AgentSession(
                session_id="session-6",
                agent_id="leader-6",
                role="leader",
                phase=ResidentCoordinatorPhase.BOOTING,
            )
        )
        await host.update_session(
            "session-6",
            phase=ResidentCoordinatorPhase.WAITING_FOR_MAILBOX,
            reason="waiting for inbound mailbox events",
            mailbox_cursor={"mailbox": {"offset": "22-0"}},
            current_directive_ids=("directive-22",),
            last_progress_at="2026-04-05T12:00:00+00:00",
            metadata={"checkpoint": "cycle-22"},
        )
        await host.bind_transport(
            "session-6",
            SessionBinding(
                session_id="session-6",
                backend="scripted",
                binding_type="resident",
                transport_locator={"result_file": "/tmp/result.json"},
                supervisor_id="supervisor-6",
                lease_id="lease-6",
                lease_expires_at="2026-04-05T12:30:00+00:00",
            ),
        )

        snapshot = await host.snapshot_session("session-6")

        self.assertIsNotNone(snapshot)
        assert snapshot is not None
        self.assertEqual(snapshot["phase"], ResidentCoordinatorPhase.WAITING_FOR_MAILBOX.value)
        self.assertEqual(snapshot["last_reason"], "waiting for inbound mailbox events")
        self.assertEqual(snapshot["mailbox_cursor"]["mailbox"]["offset"], "22-0")
        self.assertEqual(snapshot["current_directive_ids"], ["directive-22"])
        self.assertEqual(snapshot["last_progress_at"], "2026-04-05T12:00:00+00:00")
        self.assertEqual(snapshot["metadata"]["checkpoint"], "cycle-22")
        self.assertEqual(snapshot["current_binding"]["backend"], "scripted")

    async def test_record_teammate_slot_state_updates_durable_metadata_fields(self) -> None:
        host = InMemoryResidentSessionHost()
        await host.register_session(
            AgentSession(
                session_id="team-a:teammate:1:resident",
                agent_id="team-a:teammate:1",
                role="teammate",
                phase=ResidentCoordinatorPhase.IDLE,
            )
        )

        updated = await host.record_teammate_slot_state(
            "team-a:teammate:1:resident",
            activation_epoch=2,
            current_task_id="task-123",
            current_claim_session_id="claim-session-123",
            last_claim_source="resident_mailbox_directed_claim",
            current_worker_session_id="worker-session-1",
            last_worker_session_id="worker-session-0",
            idle=False,
            reason="processing directed mailbox work",
        )

        self.assertEqual(updated.metadata["activation_epoch"], 2)
        self.assertEqual(updated.metadata["current_task_id"], "task-123")
        self.assertEqual(updated.metadata["current_claim_session_id"], "claim-session-123")
        self.assertEqual(updated.metadata["last_claim_source"], "resident_mailbox_directed_claim")
        self.assertEqual(updated.metadata["current_worker_session_id"], "worker-session-1")
        self.assertEqual(updated.metadata["last_worker_session_id"], "worker-session-0")
        self.assertEqual(updated.current_worker_session_id, "worker-session-1")
        self.assertEqual(updated.last_worker_session_id, "worker-session-0")
        self.assertEqual(updated.last_reason, "processing directed mailbox work")

    async def test_load_or_create_session_and_slot_session_are_idempotent(self) -> None:
        host = InMemoryResidentSessionHost()
        initial = AgentSession(
            session_id="session-7",
            agent_id="leader-7",
            role="leader",
            phase=ResidentCoordinatorPhase.BOOTING,
            objective_id="objective-7",
        )
        created = await host.load_or_create_session(initial)
        reloaded = await host.load_or_create_session(
            AgentSession(
                session_id="session-7",
                agent_id="leader-7-overwrite",
                role="teammate",
                phase=ResidentCoordinatorPhase.RUNNING,
            )
        )
        self.assertEqual(created.session_id, "session-7")
        self.assertEqual(reloaded.agent_id, "leader-7")
        self.assertEqual(reloaded.role, "leader")
        self.assertEqual(reloaded.phase, ResidentCoordinatorPhase.BOOTING)

        slot_created = await host.load_or_create_slot_session(
            session_id="team-a:teammate:2:resident",
            agent_id="team-a:teammate:2",
            objective_id="objective-7",
            lane_id="lane-a",
            team_id="team-a",
        )
        slot_reloaded = await host.load_or_create_slot_session(
            session_id="team-a:teammate:2:resident",
            agent_id="team-a:teammate:2-overwrite",
            objective_id="objective-overwrite",
            lane_id="lane-overwrite",
            team_id="team-overwrite",
        )
        self.assertEqual(slot_created.agent_id, "team-a:teammate:2")
        self.assertEqual(slot_reloaded.agent_id, "team-a:teammate:2")
        self.assertEqual(slot_reloaded.objective_id, "objective-7")
        self.assertEqual(slot_reloaded.lane_id, "lane-a")
        self.assertEqual(slot_reloaded.team_id, "team-a")
        self.assertEqual(slot_reloaded.metadata["activation_epoch"], 1)

    async def test_commit_mailbox_consume_updates_session_cursor_and_directive_state(self) -> None:
        host = InMemoryResidentSessionHost()
        await host.register_session(
            AgentSession(
                session_id="team-a:teammate:1:resident",
                agent_id="team-a:teammate:1",
                role="teammate",
                phase=ResidentCoordinatorPhase.IDLE,
            )
        )
        persisted: list[tuple[str, dict[str, str | None]]] = []
        ack_calls: list[tuple[str, tuple[str, ...]]] = []

        async def persist_cursor(recipient: str, cursor: dict[str, str | None]) -> None:
            persisted.append((recipient, dict(cursor)))

        async def acknowledge_bridge(recipient: str, envelope_ids: tuple[str, ...]) -> str:
            ack_calls.append((recipient, envelope_ids))
            return "envelope-2b"

        updated = await host.commit_mailbox_consume(
            "team-a:teammate:1:resident",
            recipient="team-a:teammate:1",
            envelope_ids=("envelope-1", "envelope-2"),
            current_directive_ids=("task-1", "task-2"),
            reason="Directed teammate mailbox consumed.",
            persist_cursor=persist_cursor,
            acknowledge_bridge=acknowledge_bridge,
        )

        self.assertEqual(len(persisted), 2)
        self.assertEqual(persisted[0][1]["last_envelope_id"], "envelope-2")
        self.assertEqual(persisted[1][1]["last_envelope_id"], "envelope-2b")
        self.assertEqual(len(ack_calls), 1)
        self.assertEqual(ack_calls[0], ("team-a:teammate:1", ("envelope-1", "envelope-2")))
        self.assertEqual(updated.mailbox_cursor["last_envelope_id"], "envelope-2b")
        self.assertEqual(updated.current_directive_ids, ("task-1", "task-2"))
        self.assertEqual(updated.last_reason, "Directed teammate mailbox consumed.")

    async def test_store_backed_host_commits_mailbox_consume_with_host_owned_cursor_truth(self) -> None:
        store = InMemoryOrchestrationStore()
        host = StoreBackedResidentSessionHost(store)
        await store.save_worker_session(
            WorkerSession(
                session_id="team-a:teammate:3:resident",
                worker_id="team-a:teammate:3",
                assignment_id="assign-mailbox-11",
                backend="scripted",
                role="teammate",
                status=WorkerSessionStatus.ACTIVE,
                lifecycle_status="running",
                mailbox_cursor={
                    "stream": "mailbox",
                    "event_id": "envelope-9",
                    "last_envelope_id": "envelope-9",
                },
            )
        )
        await host.register_session(
            AgentSession(
                session_id="team-a:teammate:3:resident",
                agent_id="team-a:teammate:3",
                role="teammate",
                phase=ResidentCoordinatorPhase.IDLE,
            )
        )
        ack_calls: list[tuple[str, tuple[str, ...]]] = []

        async def acknowledge_bridge(recipient: str, envelope_ids: tuple[str, ...]) -> str:
            ack_calls.append((recipient, envelope_ids))
            return "envelope-11b"

        updated = await host.commit_mailbox_consume(
            "team-a:teammate:3:resident",
            recipient="team-a:teammate:3",
            envelope_ids=("envelope-10", "envelope-11"),
            current_directive_ids=("task-11",),
            reason="Committed teammate mailbox consume.",
            acknowledge_bridge=acknowledge_bridge,
        )

        stored_cursor = await store.get_protocol_bus_cursor(
            stream="mailbox",
            consumer="team-a:teammate:3",
        )
        stored_session = await store.get_agent_session("team-a:teammate:3:resident")
        stored_worker_session = await store.get_worker_session("team-a:teammate:3:resident")

        self.assertEqual(ack_calls, [("team-a:teammate:3", ("envelope-10", "envelope-11"))])
        self.assertEqual(updated.mailbox_cursor["last_envelope_id"], "envelope-11")
        self.assertEqual(updated.current_directive_ids, ("task-11",))
        self.assertEqual(stored_cursor["last_envelope_id"], "envelope-11")
        self.assertIsNotNone(stored_session)
        assert stored_session is not None
        self.assertEqual(stored_session.mailbox_cursor["last_envelope_id"], "envelope-11")
        self.assertEqual(stored_session.current_directive_ids, ("task-11",))
        self.assertEqual(stored_session.last_reason, "Committed teammate mailbox consume.")
        self.assertIsNotNone(stored_worker_session)
        assert stored_worker_session is not None
        self.assertEqual(
            stored_worker_session.mailbox_cursor["last_envelope_id"],
            "envelope-11",
        )

    async def test_project_mailbox_consume_stages_cursor_without_persisting(self) -> None:
        host = InMemoryResidentSessionHost()
        await host.register_session(
            AgentSession(
                session_id="team-a:teammate:2:resident",
                agent_id="team-a:teammate:2",
                role="teammate",
                phase=ResidentCoordinatorPhase.IDLE,
            )
        )

        projected = await host.project_mailbox_consume(
            "team-a:teammate:2:resident",
            recipient="team-a:teammate:2",
            envelope_ids=("envelope-10", "envelope-11"),
            current_directive_ids=("task-10",),
            reason="Projected mailbox consume.",
        )
        persisted = await host.load_session("team-a:teammate:2:resident")

        self.assertEqual(projected.mailbox_cursor["last_envelope_id"], "envelope-11")
        self.assertEqual(projected.current_directive_ids, ("task-10",))
        self.assertEqual(projected.last_reason, "Projected mailbox consume.")
        self.assertEqual(projected.phase, ResidentCoordinatorPhase.IDLE)
        self.assertEqual(persisted.mailbox_cursor, {})
        self.assertEqual(persisted.current_directive_ids, ())
        self.assertEqual(persisted.last_reason, "")

    async def test_project_teammate_slot_state_can_chain_on_staged_session(self) -> None:
        host = InMemoryResidentSessionHost()
        await host.register_session(
            AgentSession(
                session_id="team-a:teammate:6:resident",
                agent_id="team-a:teammate:6",
                role="teammate",
                phase=ResidentCoordinatorPhase.IDLE,
                metadata={"activation_epoch": 2},
            )
        )

        projected_consume = await host.project_mailbox_consume(
            "team-a:teammate:6:resident",
            recipient="team-a:teammate:6",
            envelope_ids=("envelope-20",),
            current_directive_ids=("task-20",),
            reason="Projected directed consume.",
        )
        projected_state = await host.project_teammate_slot_state(
            "team-a:teammate:6:resident",
            session=projected_consume,
            activation_epoch=3,
            current_task_id=None,
            current_claim_session_id="claim-session-20",
            last_claim_source="resident_mailbox_directed_claim",
            current_worker_session_id=None,
            last_worker_session_id=None,
            idle=True,
            reason="Projected directed teammate claim.",
        )
        persisted = await host.load_session("team-a:teammate:6:resident")

        self.assertEqual(projected_state.metadata["activation_epoch"], 3)
        self.assertEqual(projected_state.metadata["current_claim_session_id"], "claim-session-20")
        self.assertEqual(projected_state.metadata["last_claim_source"], "resident_mailbox_directed_claim")
        self.assertEqual(projected_state.mailbox_cursor["last_envelope_id"], "envelope-20")
        self.assertEqual(projected_state.current_directive_ids, ("task-20",))
        self.assertEqual(projected_state.phase, ResidentCoordinatorPhase.IDLE)
        self.assertEqual(projected_state.last_reason, "Projected directed teammate claim.")
        self.assertEqual(persisted.mailbox_cursor, {})
        self.assertEqual(persisted.metadata["activation_epoch"], 2)

    async def test_record_activation_intent_and_wake_request(self) -> None:
        host = InMemoryResidentSessionHost()
        await host.register_session(
            AgentSession(
                session_id="team-a:teammate:3:resident",
                agent_id="team-a:teammate:3",
                role="teammate",
                phase=ResidentCoordinatorPhase.IDLE,
                metadata={"activation_epoch": 3},
            )
        )

        activated = await host.record_activation_intent(
            "team-a:teammate:3:resident",
            reason="Leader emitted activation intent.",
            requested_by="team-a:leader",
        )
        self.assertEqual(activated.phase, ResidentCoordinatorPhase.WAITING_FOR_MAILBOX)
        self.assertEqual(activated.metadata["activation_epoch"], 4)
        self.assertEqual(activated.metadata["last_activation_reason"], "Leader emitted activation intent.")
        self.assertEqual(activated.metadata["last_activation_requested_by"], "team-a:leader")
        self._assert_iso_timestamp(activated.metadata["last_activation_intent_at"])
        self._assert_iso_timestamp(activated.last_progress_at)

        awakened = await host.record_wake_request(
            "team-a:teammate:3:resident",
            reason="Directed mailbox arrived.",
            requested_by="mailbox:team-a:teammate:3",
        )
        self.assertEqual(awakened.phase, ResidentCoordinatorPhase.RUNNING)
        self.assertEqual(awakened.metadata["wake_request_count"], 1)
        self.assertEqual(awakened.metadata["last_wake_reason"], "Directed mailbox arrived.")
        self.assertEqual(awakened.metadata["last_wake_requested_by"], "mailbox:team-a:teammate:3")
        self._assert_iso_timestamp(awakened.metadata["last_wake_request_at"])
        self._assert_iso_timestamp(awakened.metadata["last_active_at"])
        self._assert_iso_timestamp(awakened.last_progress_at)

    async def test_record_authority_wait_and_grant_decision_updates_wake_resume_metadata(self) -> None:
        host = InMemoryResidentSessionHost()
        await host.register_session(
            AgentSession(
                session_id="team-a:teammate:7:resident",
                agent_id="team-a:teammate:7",
                role="teammate",
                phase=ResidentCoordinatorPhase.IDLE,
                metadata={"activation_epoch": 7},
            )
        )

        waiting = await host.record_authority_wait_state(
            "team-a:teammate:7:resident",
            request_id="auth-req-7",
            task_id="task-7",
            boundary_class="protected_runtime",
            reason="Blocked by out-of-scope file.",
            requested_by="team-a:leader",
        )

        self.assertEqual(waiting.phase, ResidentCoordinatorPhase.WAITING_FOR_MAILBOX)
        self.assertEqual(waiting.metadata["authority_request_id"], "auth-req-7")
        self.assertEqual(waiting.metadata["authority_waiting_task_id"], "task-7")
        self.assertEqual(waiting.metadata["authority_boundary_class"], "protected_runtime")
        self.assertTrue(waiting.metadata["authority_waiting"])
        self.assertEqual(waiting.metadata["authority_last_requested_by"], "team-a:leader")
        self.assertEqual(waiting.metadata["authority_last_reason"], "Blocked by out-of-scope file.")
        self._assert_iso_timestamp(waiting.metadata["authority_waiting_since"])

        resumed = await host.record_authority_decision_state(
            "team-a:teammate:7:resident",
            request_id="auth-req-7",
            task_id="task-7",
            decision="grant",
            actor_id="team-a:leader",
            resume_target="team-a:teammate:7",
            reason="Authority granted for bootstrap repair.",
        )

        self.assertEqual(resumed.phase, ResidentCoordinatorPhase.RUNNING)
        self.assertFalse(resumed.metadata["authority_waiting"])
        self.assertIsNone(resumed.metadata["authority_waiting_since"])
        self.assertEqual(resumed.metadata["authority_last_decision"], "grant")
        self.assertEqual(resumed.metadata["authority_last_decision_actor_id"], "team-a:leader")
        self.assertEqual(resumed.metadata["authority_resume_target"], "team-a:teammate:7")
        self.assertEqual(resumed.metadata["wake_request_count"], 1)
        self.assertEqual(resumed.metadata["last_wake_requested_by"], "team-a:leader")
        self.assertEqual(
            resumed.metadata["last_wake_reason"],
            "Authority granted for bootstrap repair.",
        )
        self.assertTrue(resumed.metadata["authority_wake_recorded"])
        self.assertEqual(resumed.metadata["authority_completion_status"], "grant_resumed")
        self._assert_iso_timestamp(resumed.metadata["authority_last_decision_at"])
        self._assert_iso_timestamp(resumed.metadata["last_wake_request_at"])
        self._assert_iso_timestamp(resumed.last_progress_at)

    async def test_record_authority_relay_state_tracks_pending_closure_truth(self) -> None:
        host = InMemoryResidentSessionHost()
        await host.register_session(
            AgentSession(
                session_id="team-a:teammate:10:resident",
                agent_id="team-a:teammate:10",
                role="teammate",
                phase=ResidentCoordinatorPhase.IDLE,
                metadata={"activation_epoch": 10},
            )
        )

        pending = await host.record_authority_relay_state(
            "team-a:teammate:10:resident",
            request_id="auth-req-10",
            task_id="task-10",
            relay_subject="authority.writeback",
            relay_envelope_id="env-auth-10",
            actor_id="superleader:obj-10",
            reason="Authority relay consumed while waiting for a committed decision payload.",
        )

        self.assertEqual(pending.phase, ResidentCoordinatorPhase.WAITING_FOR_MAILBOX)
        self.assertEqual(pending.metadata["authority_request_id"], "auth-req-10")
        self.assertEqual(pending.metadata["authority_waiting_task_id"], "task-10")
        self.assertEqual(pending.metadata["authority_last_relay_subject"], "authority.writeback")
        self.assertEqual(pending.metadata["authority_last_relay_envelope_id"], "env-auth-10")
        self.assertEqual(pending.metadata["authority_last_relay_actor_id"], "superleader:obj-10")
        self.assertTrue(pending.metadata["authority_relay_consumed"])
        self.assertTrue(pending.metadata["authority_waiting"])
        self.assertEqual(pending.metadata["authority_completion_status"], "relay_pending")
        self._assert_iso_timestamp(pending.metadata["authority_last_relay_consumed_at"])
        self._assert_iso_timestamp(pending.last_progress_at)

    async def test_project_authority_decision_state_rejects_unknown_decision_without_persisting(self) -> None:
        host = InMemoryResidentSessionHost()
        await host.register_session(
            AgentSession(
                session_id="team-a:teammate:8:resident",
                agent_id="team-a:teammate:8",
                role="teammate",
                phase=ResidentCoordinatorPhase.IDLE,
                metadata={"activation_epoch": 8},
            )
        )

        with self.assertRaises(ValueError):
            await host.project_authority_decision_state(
                "team-a:teammate:8:resident",
                request_id="auth-req-8",
                task_id="task-8",
                decision="unknown",
                actor_id="team-a:leader",
            )

        persisted = await host.load_session("team-a:teammate:8:resident")
        self.assertIsNotNone(persisted)
        assert persisted is not None
        self.assertEqual(persisted.phase, ResidentCoordinatorPhase.IDLE)
        self.assertNotIn("authority_last_decision", persisted.metadata)
        self.assertEqual(persisted.metadata["activation_epoch"], 8)

    async def test_record_authority_decision_state_tracks_reroute_and_deny_as_observable_terminal_states(self) -> None:
        host = InMemoryResidentSessionHost()
        await host.register_session(
            AgentSession(
                session_id="team-a:teammate:9:resident",
                agent_id="team-a:teammate:9",
                role="teammate",
                phase=ResidentCoordinatorPhase.IDLE,
                metadata={"activation_epoch": 9},
            )
        )

        await host.record_authority_wait_state(
            "team-a:teammate:9:resident",
            request_id="auth-req-9",
            task_id="task-9",
            boundary_class="protected_runtime",
            reason="Waiting for authority decision.",
            requested_by="team-a:leader",
        )
        rerouted = await host.record_authority_decision_state(
            "team-a:teammate:9:resident",
            request_id="auth-req-9",
            task_id="task-9",
            decision="reroute",
            actor_id="team-a:leader",
            resume_target="task-9-reroute",
            reason="Authority rerouted to a dedicated repair task.",
        )

        self.assertEqual(rerouted.phase, ResidentCoordinatorPhase.IDLE)
        self.assertFalse(rerouted.metadata["authority_waiting"])
        self.assertIsNone(rerouted.metadata["authority_waiting_since"])
        self.assertEqual(rerouted.metadata["authority_last_decision"], "reroute")
        self.assertEqual(rerouted.metadata["authority_last_decision_actor_id"], "team-a:leader")
        self.assertEqual(rerouted.metadata["authority_resume_target"], "task-9-reroute")
        self.assertFalse(rerouted.metadata["authority_wake_recorded"])
        self.assertEqual(rerouted.metadata["authority_completion_status"], "reroute_closed")
        self._assert_iso_timestamp(rerouted.metadata["authority_last_decision_at"])

        await host.record_authority_wait_state(
            "team-a:teammate:9:resident",
            request_id="auth-req-9b",
            task_id="task-9b",
            boundary_class="global_contract",
            reason="Waiting for authority root denial or grant.",
            requested_by="team-a:leader",
        )
        denied = await host.record_authority_decision_state(
            "team-a:teammate:9:resident",
            request_id="auth-req-9b",
            task_id="task-9b",
            decision="deny",
            actor_id="objective:obj-1:superleader",
            reason="Authority denied by policy.",
        )

        self.assertEqual(denied.phase, ResidentCoordinatorPhase.IDLE)
        self.assertFalse(denied.metadata["authority_waiting"])
        self.assertIsNone(denied.metadata["authority_waiting_since"])
        self.assertEqual(denied.metadata["authority_last_decision"], "deny")
        self.assertEqual(
            denied.metadata["authority_last_decision_actor_id"],
            "objective:obj-1:superleader",
        )
        self.assertEqual(denied.metadata["authority_waiting_task_id"], "task-9b")
        self.assertEqual(denied.metadata["authority_last_reason"], "Authority denied by policy.")
        self.assertFalse(denied.metadata["authority_wake_recorded"])
        self.assertEqual(denied.metadata["authority_completion_status"], "deny_closed")
        self._assert_iso_timestamp(denied.metadata["authority_last_decision_at"])

    async def test_record_teammate_slot_state_tracks_iso_active_and_idle_timestamps(self) -> None:
        host = InMemoryResidentSessionHost()
        await host.register_session(
            AgentSession(
                session_id="team-a:teammate:4:resident",
                agent_id="team-a:teammate:4",
                role="teammate",
                phase=ResidentCoordinatorPhase.IDLE,
            )
        )

        active = await host.record_teammate_slot_state(
            "team-a:teammate:4:resident",
            current_task_id="task-4",
            current_claim_session_id="claim-4",
            idle=False,
            reason="slot active",
        )
        self.assertEqual(active.phase, ResidentCoordinatorPhase.RUNNING)
        self._assert_iso_timestamp(active.metadata["last_active_at"])
        self.assertIsNone(active.metadata["idle_since"])

        idle = await host.record_teammate_slot_state(
            "team-a:teammate:4:resident",
            current_task_id=None,
            current_claim_session_id="claim-4",
            idle=True,
            reason="slot idle",
        )
        self.assertEqual(idle.phase, ResidentCoordinatorPhase.IDLE)
        idle_since = self._assert_iso_timestamp(idle.metadata["idle_since"])

        idle_again = await host.record_teammate_slot_state(
            "team-a:teammate:4:resident",
            current_task_id=None,
            current_claim_session_id="claim-4",
            idle=True,
            reason="still idle",
        )
        self.assertEqual(idle_again.metadata["idle_since"], idle_since)

    async def test_worker_session_truth_helper_reads_stable_fields(self) -> None:
        host = InMemoryResidentSessionHost()
        await host.register_session(
            AgentSession(
                session_id="team-a:teammate:5:resident",
                agent_id="team-a:teammate:5",
                role="teammate",
                phase=ResidentCoordinatorPhase.RUNNING,
                current_worker_session_id="worker-session-5",
                last_worker_session_id="worker-session-4",
                metadata={
                    "current_worker_session_id": "worker-session-metadata-ignored",
                    "last_worker_session_id": "worker-session-metadata-ignored",
                },
            )
        )

        truth = await host.read_worker_session_truth("team-a:teammate:5:resident")

        self.assertEqual(truth.current_worker_session_id, "worker-session-5")
        self.assertEqual(truth.bound_worker_session_id, "worker-session-5")
        self.assertEqual(truth.last_worker_session_id, "worker-session-4")

    async def test_project_worker_session_state_preserves_host_owned_session_fields(self) -> None:
        host = InMemoryResidentSessionHost()
        session_id = "team-a:teammate:11:resident"
        await host.register_session(
            AgentSession(
                session_id=session_id,
                agent_id="team-a:teammate:11",
                role="teammate",
                phase=ResidentCoordinatorPhase.IDLE,
                objective_id="objective-11",
                lane_id="lane-11",
                team_id="team-a",
                mailbox_cursor={
                    "stream": "mailbox",
                    "event_id": "envelope-host-11",
                    "last_envelope_id": "envelope-host-11",
                },
                subscription_cursors={"mailbox": {"event_id": "digest-11"}},
                claimed_task_ids=("task-host-11",),
                current_directive_ids=("directive-host-11",),
                metadata={
                    "activation_epoch": 11,
                    "custom_marker": "host-owned",
                },
            )
        )

        projected = await host.project_worker_session_state(
            worker_session=WorkerSession(
                session_id=session_id,
                worker_id="team-a:teammate:11",
                assignment_id="assign-worker-11",
                backend="scripted",
                role="teammate",
                status=WorkerSessionStatus.ACTIVE,
                lifecycle_status="running",
                last_active_at="2026-04-09T12:00:00+00:00",
                supervisor_id="supervisor-11",
                supervisor_lease_id="lease-11",
                supervisor_lease_expires_at="2026-04-09T12:05:00+00:00",
                mailbox_cursor={
                    "stream": "mailbox",
                    "event_id": "envelope-worker-11",
                    "last_envelope_id": "envelope-worker-11",
                },
                metadata={
                    "task_id": "task-worker-11",
                    "execution_contract": {"mode": "code_edit"},
                },
            ),
            binding=SessionBinding(
                session_id=session_id,
                backend="scripted",
                binding_type="resident",
                supervisor_id="supervisor-11",
                lease_id="lease-11",
                lease_expires_at="2026-04-09T12:05:00+00:00",
            ),
            phase=ResidentCoordinatorPhase.RUNNING,
            reason="enter_native_wait",
        )

        self.assertEqual(projected.phase, ResidentCoordinatorPhase.RUNNING)
        self.assertEqual(projected.objective_id, "objective-11")
        self.assertEqual(projected.lane_id, "lane-11")
        self.assertEqual(projected.team_id, "team-a")
        self.assertEqual(projected.mailbox_cursor["last_envelope_id"], "envelope-host-11")
        self.assertEqual(projected.subscription_cursors["mailbox"]["event_id"], "digest-11")
        self.assertEqual(projected.claimed_task_ids, ("task-host-11",))
        self.assertEqual(projected.current_directive_ids, ("directive-host-11",))
        self.assertEqual(projected.metadata["activation_epoch"], 11)
        self.assertEqual(projected.metadata["custom_marker"], "host-owned")
        self.assertEqual(projected.metadata["task_id"], "task-worker-11")
        self.assertEqual(projected.current_worker_session_id, session_id)
        self.assertEqual(projected.last_worker_session_id, session_id)
        self.assertEqual(projected.current_binding.backend, "scripted")

    async def test_store_backed_host_load_session_projects_worker_truth_without_clobbering_host_fields(self) -> None:
        store = InMemoryOrchestrationStore()
        session_id = "team-a:teammate:12:resident"
        await store.save_agent_session(
            AgentSession(
                session_id=session_id,
                agent_id="team-a:teammate:12",
                role="teammate",
                phase=ResidentCoordinatorPhase.WAITING_FOR_MAILBOX,
                objective_id="objective-12",
                lane_id="lane-12",
                team_id="team-a",
                mailbox_cursor={
                    "stream": "mailbox",
                    "event_id": "envelope-host-12",
                    "last_envelope_id": "envelope-host-12",
                },
                subscription_cursors={"mailbox": {"event_id": "digest-12"}},
                claimed_task_ids=("task-host-12",),
                current_directive_ids=("directive-host-12",),
                current_binding=SessionBinding(
                    session_id=session_id,
                    backend="scripted",
                    binding_type="resident",
                    transport_locator={"pid": 912},
                    supervisor_id="supervisor-stale",
                    lease_id="lease-stale",
                    lease_expires_at="2026-04-09T11:55:00+00:00",
                ),
                current_worker_session_id="worker-stale-12",
                last_worker_session_id="worker-stale-12",
                last_reason="Host-owned wait state.",
                metadata={
                    "activation_epoch": 12,
                    "custom_marker": "host-owned",
                },
            )
        )
        await store.save_worker_session(
            WorkerSession(
                session_id=session_id,
                worker_id="team-a:teammate:12",
                assignment_id="assign-worker-12",
                backend="scripted",
                role="teammate",
                status=WorkerSessionStatus.COMPLETED,
                lifecycle_status="completed",
                last_active_at="2026-04-09T12:00:00+00:00",
                supervisor_id="supervisor-new",
                supervisor_lease_id="lease-new",
                supervisor_lease_expires_at="2026-04-09T12:05:00+00:00",
                mailbox_cursor={
                    "stream": "mailbox",
                    "event_id": "envelope-worker-12",
                    "last_envelope_id": "envelope-worker-12",
                },
                metadata={
                    "task_id": "task-worker-12",
                    "execution_contract": {"mode": "code_edit"},
                },
            )
        )
        host = StoreBackedResidentSessionHost(store)

        loaded = await host.load_session(session_id)

        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(loaded.objective_id, "objective-12")
        self.assertEqual(loaded.lane_id, "lane-12")
        self.assertEqual(loaded.team_id, "team-a")
        self.assertEqual(loaded.mailbox_cursor["last_envelope_id"], "envelope-host-12")
        self.assertEqual(loaded.subscription_cursors["mailbox"]["event_id"], "digest-12")
        self.assertEqual(loaded.claimed_task_ids, ("task-host-12",))
        self.assertEqual(loaded.current_directive_ids, ("directive-host-12",))
        self.assertEqual(loaded.metadata["activation_epoch"], 12)
        self.assertEqual(loaded.metadata["custom_marker"], "host-owned")
        self.assertEqual(loaded.metadata["task_id"], "task-worker-12")
        self.assertIsNone(loaded.current_worker_session_id)
        self.assertEqual(loaded.last_worker_session_id, session_id)
        self.assertIsNotNone(loaded.current_binding)
        assert loaded.current_binding is not None
        self.assertEqual(loaded.current_binding.transport_locator["pid"], 912)
        self.assertEqual(loaded.current_binding.supervisor_id, "supervisor-new")
        self.assertEqual(loaded.current_binding.lease_id, "lease-new")
        self.assertEqual(
            loaded.current_binding.lease_expires_at,
            "2026-04-09T12:05:00+00:00",
        )

    async def test_load_or_create_coordinator_session_registers_host_managed_leader_lane(self) -> None:
        host = InMemoryResidentSessionHost()

        created = await host.load_or_create_coordinator_session(
            session_id="objective-1:lane-runtime:leader:resident",
            coordinator_id="leader:runtime",
            objective_id="objective-1",
            lane_id="lane-runtime",
            team_id="team-runtime",
            role="leader",
            host_owner_coordinator_id="superleader:objective-1",
            runtime_task_id="runtime-task-1",
            metadata={
                "runtime_view": "leader_lane_session_graph",
                "launch_mode": "leader_session_host",
            },
        )
        reloaded = await host.load_or_create_coordinator_session(
            session_id="objective-1:lane-runtime:leader:resident",
            coordinator_id="leader:runtime-overwrite",
            objective_id="objective-overwrite",
            lane_id="lane-overwrite",
            team_id="team-overwrite",
            role="superleader",
            host_owner_coordinator_id="superleader:overwrite",
            runtime_task_id="runtime-task-overwrite",
            metadata={"launch_mode": "overwrite"},
        )

        self.assertEqual(created.session_id, "objective-1:lane-runtime:leader:resident")
        self.assertEqual(reloaded.agent_id, "leader:runtime")
        self.assertEqual(reloaded.objective_id, "objective-1")
        self.assertEqual(reloaded.lane_id, "lane-runtime")
        self.assertEqual(reloaded.team_id, "team-runtime")

        state = await host.load_coordinator_session("objective-1:lane-runtime:leader:resident")

        self.assertIsNotNone(state)
        assert state is not None
        self.assertEqual(state.session_id, "objective-1:lane-runtime:leader:resident")
        self.assertEqual(state.coordinator_id, "leader:runtime")
        self.assertEqual(state.host_owner_coordinator_id, "superleader:objective-1")
        self.assertEqual(state.runtime_task_id, "runtime-task-1")
        self.assertEqual(state.role, "leader")
        self.assertEqual(state.objective_id, "objective-1")
        self.assertEqual(state.lane_id, "lane-runtime")
        self.assertEqual(state.team_id, "team-runtime")
        self.assertEqual(state.metadata["runtime_view"], "leader_lane_session_graph")
        self.assertEqual(state.metadata["launch_mode"], "leader_session_host")

    async def test_record_coordinator_session_state_projects_resident_counts_into_host_session(self) -> None:
        host = InMemoryResidentSessionHost()
        await host.load_or_create_coordinator_session(
            session_id="objective-1:lane-runtime:leader:resident",
            coordinator_id="leader:runtime",
            objective_id="objective-1",
            lane_id="lane-runtime",
            team_id="team-runtime",
            role="leader",
            host_owner_coordinator_id="superleader:objective-1",
            runtime_task_id="runtime-task-1",
            metadata={"runtime_view": "leader_lane_session_graph"},
        )

        updated = await host.record_coordinator_session_state(
            "objective-1:lane-runtime:leader:resident",
            coordinator_session=ResidentCoordinatorSession(
                coordinator_id="leader:runtime",
                role="leader",
                phase=ResidentCoordinatorPhase.WAITING_FOR_SUBORDINATES,
                objective_id="objective-1",
                lane_id="lane-runtime",
                team_id="team-runtime",
                cycle_count=3,
                prompt_turn_count=2,
                claimed_task_count=1,
                subordinate_dispatch_count=4,
                mailbox_poll_count=5,
                active_subordinate_ids=("team-runtime:teammate:1",),
                mailbox_cursor="envelope-9",
                last_reason="Waiting for active teammate slots.",
                metadata={"launch_mode": "leader_loop.run"},
            ),
            metadata={"runtime_view": "leader_lane_session_graph"},
        )

        self.assertEqual(updated.phase, ResidentCoordinatorPhase.WAITING_FOR_SUBORDINATES)
        self.assertEqual(updated.last_reason, "Waiting for active teammate slots.")
        self._assert_iso_timestamp(updated.last_progress_at)
        self.assertEqual(updated.mailbox_cursor["last_envelope_id"], "envelope-9")

        state = await host.load_coordinator_session("objective-1:lane-runtime:leader:resident")

        self.assertIsNotNone(state)
        assert state is not None
        self.assertEqual(state.host_owner_coordinator_id, "superleader:objective-1")
        self.assertEqual(state.runtime_task_id, "runtime-task-1")
        self.assertEqual(state.phase, ResidentCoordinatorPhase.WAITING_FOR_SUBORDINATES)
        self.assertEqual(state.cycle_count, 3)
        self.assertEqual(state.prompt_turn_count, 2)
        self.assertEqual(state.claimed_task_count, 1)
        self.assertEqual(state.subordinate_dispatch_count, 4)
        self.assertEqual(state.mailbox_poll_count, 5)
        self.assertEqual(state.active_subordinate_ids, ("team-runtime:teammate:1",))
        self.assertEqual(state.mailbox_cursor, "envelope-9")
        self.assertEqual(state.last_reason, "Waiting for active teammate slots.")
        self.assertEqual(state.metadata["runtime_view"], "leader_lane_session_graph")
        self.assertEqual(state.metadata["launch_mode"], "leader_loop.run")

    async def test_inspect_resident_team_shell_projects_host_owned_leader_and_slot_truth_over_stale_shell_payload(
        self,
    ) -> None:
        store = InMemoryOrchestrationStore()
        host = StoreBackedResidentSessionHost(store)
        await store.save_resident_team_shell(
            ResidentTeamShell(
                resident_team_shell_id="resident-shell-runtime",
                work_session_id="worksession-1",
                group_id="group-a",
                objective_id="objective-1",
                team_id="team-runtime",
                lane_id="lane-runtime",
                runtime_generation_id="runtimegeneration-1",
                status=ResidentTeamShellStatus.IDLE,
                leader_slot_session_id="stale-leader-session",
                teammate_slot_session_ids=["stale-slot-session"],
                attach_state={"preferred_session_id": "stale-leader-session"},
                created_at="2026-04-11T00:00:00+00:00",
                updated_at="2026-04-11T00:10:00+00:00",
                last_progress_at="2026-04-11T00:10:00+00:00",
                metadata={"shell_generation": "stale"},
            )
        )
        leader_session_id = "objective-1:lane-runtime:leader:resident"
        teammate_slot_2 = "team-runtime:teammate:2:resident"
        teammate_slot_1 = "team-runtime:teammate:1:resident"
        shell_metadata = {
            "group_id": "group-a",
            "work_session_id": "worksession-1",
            "runtime_generation_id": "runtimegeneration-1",
        }

        await host.load_or_create_slot_session(
            session_id=teammate_slot_2,
            agent_id="team-runtime:teammate:2",
            objective_id="objective-1",
            lane_id="lane-runtime",
            team_id="team-runtime",
            metadata=shell_metadata,
        )
        await host.load_or_create_slot_session(
            session_id=teammate_slot_1,
            agent_id="team-runtime:teammate:1",
            objective_id="objective-1",
            lane_id="lane-runtime",
            team_id="team-runtime",
            metadata=shell_metadata,
        )
        await host.record_teammate_slot_state(
            teammate_slot_2,
            activation_epoch=2,
            current_task_id="task-runtime-2",
            current_claim_session_id="claim-runtime-2",
            last_claim_source="resident_mailbox_directed_claim",
            current_worker_session_id="worker-runtime-2",
            last_worker_session_id="worker-runtime-1",
            idle=False,
            reason="Slot 2 is actively handling resident work.",
        )
        await host.record_teammate_slot_state(
            teammate_slot_1,
            activation_epoch=1,
            current_task_id=None,
            current_claim_session_id=None,
            last_claim_source="resident_idle",
            current_worker_session_id=None,
            last_worker_session_id="worker-runtime-0",
            idle=True,
            reason="Slot 1 is idle and attached.",
        )
        await host.load_or_create_coordinator_session(
            session_id=leader_session_id,
            coordinator_id="leader:runtime",
            objective_id="objective-1",
            lane_id="lane-runtime",
            team_id="team-runtime",
            role="leader",
            host_owner_coordinator_id="superleader:objective-1",
            runtime_task_id="runtime-task-1",
            metadata={
                **shell_metadata,
                "runtime_view": "leader_lane_session_graph",
            },
        )
        await host.record_coordinator_session_state(
            leader_session_id,
            coordinator_session=ResidentCoordinatorSession(
                coordinator_id="leader:runtime",
                role="leader",
                phase=ResidentCoordinatorPhase.WAITING_FOR_SUBORDINATES,
                objective_id="objective-1",
                lane_id="lane-runtime",
                team_id="team-runtime",
                cycle_count=4,
                prompt_turn_count=2,
                claimed_task_count=1,
                subordinate_dispatch_count=2,
                mailbox_poll_count=5,
                active_subordinate_ids=("team-runtime:teammate:2",),
                mailbox_cursor="leader-envelope-9",
                last_reason="Waiting for resident teammate progress.",
                metadata={"runtime_view": "leader_lane_session_graph"},
            ),
            metadata={"runtime_view": "leader_lane_session_graph"},
            last_progress_at="2026-04-11T12:00:00+00:00",
        )

        shell = await host.inspect_resident_team_shell(
            resident_team_shell_id="resident-shell-runtime"
        )

        self.assertIsNotNone(shell)
        assert shell is not None
        self.assertEqual(shell.resident_team_shell_id, "resident-shell-runtime")
        self.assertEqual(shell.created_at, "2026-04-11T00:00:00+00:00")
        self.assertEqual(shell.status, ResidentTeamShellStatus.WAITING_FOR_SUBORDINATES)
        self.assertEqual(shell.leader_slot_session_id, leader_session_id)
        self.assertEqual(
            shell.teammate_slot_session_ids,
            [teammate_slot_1, teammate_slot_2],
        )
        self.assertEqual(shell.last_progress_at, "2026-04-11T12:00:00+00:00")
        self.assertEqual(
            shell.attach_state["preferred_session_id"],
            leader_session_id,
        )
        self.assertEqual(
            shell.attach_state["active_teammate_slot_session_ids"],
            [teammate_slot_2],
        )

    async def test_store_backed_host_skips_persisting_resident_shell_when_work_session_is_unknown(
        self,
    ) -> None:
        store = InMemoryOrchestrationStore()
        host = StoreBackedResidentSessionHost(store)

        await host.save_session(
            AgentSession(
                session_id="objective-1:lane-runtime:leader:resident",
                agent_id="leader:runtime",
                role="leader",
                phase=ResidentCoordinatorPhase.WAITING_FOR_MAILBOX,
                objective_id="objective-1",
                lane_id="lane-runtime",
                team_id="team-runtime",
                metadata={
                    "group_id": "group-a",
                    "work_session_id": "worksession-missing",
                    "runtime_generation_id": "runtimegeneration-missing",
                },
            )
        )

        self.assertEqual(store.resident_team_shells, {})

    async def test_build_shell_attach_view_and_find_preferred_attach_target_prefer_live_host_binding(
        self,
    ) -> None:
        store = InMemoryOrchestrationStore()
        host = StoreBackedResidentSessionHost(store)
        leader_session_id = "objective-1:lane-runtime:leader:resident"
        shell_metadata = {
            "group_id": "group-a",
            "work_session_id": "worksession-1",
            "runtime_generation_id": "runtimegeneration-1",
        }
        await store.save_resident_team_shell(
            ResidentTeamShell(
                resident_team_shell_id="resident-shell-attach",
                work_session_id="worksession-1",
                group_id="group-a",
                objective_id="objective-1",
                team_id="team-runtime",
                lane_id="lane-runtime",
                runtime_generation_id="runtimegeneration-1",
                status=ResidentTeamShellStatus.QUIESCENT,
                leader_slot_session_id="stale-leader-session",
                teammate_slot_session_ids=[],
                attach_state={"preferred_session_id": "stale-leader-session"},
                created_at="2026-04-11T00:00:00+00:00",
                updated_at="2026-04-11T00:10:00+00:00",
                last_progress_at="2026-04-11T00:10:00+00:00",
            )
        )
        await host.load_or_create_coordinator_session(
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
        await host.bind_session(
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
        await host.record_coordinator_session_state(
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

        attach_view = await host.build_shell_attach_view(
            objective_id="objective-1",
            lane_id="lane-runtime",
            team_id="team-runtime",
        )
        decision = await host.find_preferred_attach_target(
            objective_id="objective-1",
            lane_id="lane-runtime",
            team_id="team-runtime",
        )

        self.assertEqual(attach_view["status"], ResidentTeamShellStatus.WAITING_FOR_MAILBOX.value)
        self.assertEqual(attach_view["leader_slot"]["session_id"], leader_session_id)
        self.assertEqual(
            attach_view["attach_recommendation"]["target_shell_id"],
            "resident-shell-attach",
        )
        self.assertEqual(decision.mode, ShellAttachDecisionMode.ATTACHED)
        self.assertEqual(decision.target_shell_id, "resident-shell-attach")
        self.assertEqual(decision.target_work_session_id, "worksession-1")
        self.assertEqual(decision.target_runtime_generation_id, "runtimegeneration-1")
        self.assertEqual(decision.metadata["preferred_session_id"], leader_session_id)
        self.assertEqual(decision.metadata["backend"], "tmux")
        self.assertEqual(decision.metadata["lease_id"], "lease-live")

    async def test_build_shell_attach_view_includes_scope_ids_and_slot_summary(self) -> None:
        store = InMemoryOrchestrationStore()
        host = StoreBackedResidentSessionHost(store)
        leader_session_id = "objective-1:lane-runtime:leader:resident"
        teammate_slot_id = "team-runtime:teammate:1:resident"
        shell_metadata = {
            "group_id": "group-a",
            "work_session_id": "worksession-1",
            "runtime_generation_id": "runtimegeneration-1",
        }
        await host.load_or_create_slot_session(
            session_id=teammate_slot_id,
            agent_id="team-runtime:teammate:1",
            objective_id="objective-1",
            lane_id="lane-runtime",
            team_id="team-runtime",
            metadata=shell_metadata,
        )
        await host.load_or_create_coordinator_session(
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
        await host.bind_session(
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
        await host.record_coordinator_session_state(
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

        attach_view = await host.build_shell_attach_view(
            objective_id="objective-1",
            lane_id="lane-runtime",
            team_id="team-runtime",
        )
        records = await store.list_turn_records(
            "worksession-1",
            runtime_generation_id="runtimegeneration-1",
            head_kind=ConversationHeadKind.LEADER_LANE,
            scope_id="lane-runtime",
        )
        self.assertTrue(
            any(record.turn_kind == AgentTurnKind.LEADER_DECISION for record in records)
        )
        artifacts = await store.list_artifact_refs(
            "worksession-1",
            runtime_generation_id="runtimegeneration-1",
        )
        self.assertTrue(
            any(ref.artifact_kind == ArtifactRefKind.HYDRATION_INPUT for ref in artifacts)
        )

        self.assertEqual(attach_view["objective_id"], "objective-1")
        self.assertEqual(attach_view["lane_id"], "lane-runtime")
        self.assertEqual(attach_view["team_id"], "team-runtime")
        self.assertEqual(attach_view["work_session_id"], "worksession-1")
        self.assertEqual(attach_view["runtime_generation_id"], "runtimegeneration-1")
        self.assertEqual(attach_view["leader_slot"]["backend"], "tmux")
        self.assertEqual(attach_view["leader_slot"]["lease_id"], "lease-live")
        self.assertTrue(attach_view["leader_slot"]["has_binding"])
        self.assertEqual(
            attach_view["slot_summary"],
            {"total": 2, "active": 0, "idle": 1, "waiting": 1, "runnable": 0},
        )

    async def test_record_resident_shell_approval_surfaces_attach_queue_in_attach_view(self) -> None:
        store = InMemoryOrchestrationStore()
        host = StoreBackedResidentSessionHost(store)
        leader_session_id = "objective-1:lane-runtime:leader:resident"
        shell_metadata = {
            "group_id": "group-a",
            "work_session_id": "worksession-1",
            "runtime_generation_id": "runtimegeneration-1",
        }
        await host.load_or_create_coordinator_session(
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
        await host.bind_session(
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
        await host.record_coordinator_session_state(
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
        await host.record_resident_shell_approval(
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

        attach_view = await host.build_shell_attach_view(
            objective_id="objective-1",
            lane_id="lane-runtime",
            team_id="team-runtime",
        )
        decision = await host.find_preferred_attach_target(
            objective_id="objective-1",
            lane_id="lane-runtime",
            team_id="team-runtime",
        )
        records = await store.list_turn_records(
            "worksession-1",
            runtime_generation_id="runtimegeneration-1",
            head_kind=ConversationHeadKind.LEADER_LANE,
            scope_id="lane-runtime",
        )
        self.assertTrue(
            any(record.turn_kind == AgentTurnKind.LEADER_DECISION for record in records)
        )
        tool_records = await store.list_tool_invocation_records(
            "worksession-1",
            runtime_generation_id="runtimegeneration-1",
        )
        self.assertTrue(
            any(record.tool_kind == ToolInvocationKind.PROTOCOL_TOOL for record in tool_records)
        )

        self.assertEqual(attach_view["approval_queue"]["attach"]["status"], "pending")
        self.assertEqual(attach_view["approval_queue"]["attach"]["target_mode"], "attached")
        self.assertEqual(attach_view["attach_state"]["attach_approval_status"], "pending")
        self.assertEqual(decision.mode, ShellAttachDecisionMode.REJECTED)
        self.assertEqual(decision.metadata["approval_status"], "pending")

    async def test_record_resident_shell_approval_keeps_targeted_idle_wait_entries(self) -> None:
        store = InMemoryOrchestrationStore()
        host = StoreBackedResidentSessionHost(store)
        shell_metadata = {
            "group_id": "group-a",
            "work_session_id": "worksession-1",
            "runtime_generation_id": "runtimegeneration-1",
        }
        leader_session_id = "objective-1:lane-runtime:leader:resident"
        teammate_session_id = "team-runtime:teammate:1:resident"
        await host.load_or_create_coordinator_session(
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
        await host.record_coordinator_session_state(
            leader_session_id,
            coordinator_session=ResidentCoordinatorSession(
                coordinator_id="leader:runtime",
                role="leader",
                phase=ResidentCoordinatorPhase.IDLE,
                objective_id="objective-1",
                lane_id="lane-runtime",
                team_id="team-runtime",
                cycle_count=2,
                prompt_turn_count=1,
                claimed_task_count=0,
                subordinate_dispatch_count=0,
                mailbox_poll_count=3,
                mailbox_cursor="leader-envelope-idle",
                last_reason="Leader shell is idle-attached.",
            ),
            last_progress_at="2026-04-11T12:00:00+00:00",
        )
        await host.load_or_create_slot_session(
            session_id=teammate_session_id,
            agent_id="team-runtime:teammate:1",
            objective_id="objective-1",
            lane_id="lane-runtime",
            team_id="team-runtime",
            metadata=shell_metadata,
        )
        await host.mark_phase(
            teammate_session_id,
            ResidentCoordinatorPhase.IDLE,
            reason="Teammate slot is idle-attached.",
        )

        await host.record_resident_shell_approval(
            approval_kind="idle_wait",
            status="approved",
            request=PermissionRequest(
                requester="leader:runtime",
                action="resident.idle_wait",
                rationale="Keep the leader resident shell idle-attached.",
                group_id="group-a",
                objective_id="objective-1",
                team_id="team-runtime",
                lane_id="lane-runtime",
            ),
            requested_by="leader:runtime",
            objective_id="objective-1",
            lane_id="lane-runtime",
            team_id="team-runtime",
            target_session_id=leader_session_id,
            target_mode=ResidentCoordinatorPhase.IDLE.value,
        )
        await host.record_resident_shell_approval(
            approval_kind="idle_wait",
            status="pending",
            request=PermissionRequest(
                requester="team-runtime:teammate:1",
                action="resident.idle_wait",
                rationale="Keep the teammate slot idle-attached.",
                group_id="group-a",
                objective_id="objective-1",
                team_id="team-runtime",
                lane_id="lane-runtime",
            ),
            requested_by="team-runtime:teammate:1",
            objective_id="objective-1",
            lane_id="lane-runtime",
            team_id="team-runtime",
            target_session_id=teammate_session_id,
            target_mode=ResidentCoordinatorPhase.IDLE.value,
        )

        attach_view = await host.build_shell_attach_view(
            objective_id="objective-1",
            lane_id="lane-runtime",
            team_id="team-runtime",
        )

        idle_wait_queue = attach_view["approval_queue"]["idle_wait"]
        self.assertEqual(
            idle_wait_queue["targets"][leader_session_id]["status"],
            "approved",
        )
        self.assertEqual(
            idle_wait_queue["targets"][teammate_session_id]["status"],
            "pending",
        )
        self.assertEqual(
            attach_view["attach_state"]["idle_wait_approval_status"],
            "approved",
        )

    async def test_list_coordinator_sessions_filters_by_host_owner_and_role(self) -> None:
        host = InMemoryResidentSessionHost()
        await host.load_or_create_coordinator_session(
            session_id="objective-1:lane-runtime:leader:resident",
            coordinator_id="leader:runtime",
            objective_id="objective-1",
            lane_id="lane-runtime",
            team_id="team-runtime",
            role="leader",
            host_owner_coordinator_id="superleader:objective-1",
            runtime_task_id="runtime-task-1",
        )
        await host.load_or_create_coordinator_session(
            session_id="objective-1:lane-qa:leader:resident",
            coordinator_id="leader:qa",
            objective_id="objective-1",
            lane_id="lane-qa",
            team_id="team-qa",
            role="leader",
            host_owner_coordinator_id="superleader:objective-1",
            runtime_task_id="runtime-task-2",
        )
        await host.load_or_create_coordinator_session(
            session_id="objective-2:lane-docs:leader:resident",
            coordinator_id="leader:docs",
            objective_id="objective-2",
            lane_id="lane-docs",
            team_id="team-docs",
            role="leader",
            host_owner_coordinator_id="superleader:objective-2",
            runtime_task_id="runtime-task-3",
        )
        await host.register_session(
            AgentSession(
                session_id="objective-1:team-runtime:teammate:1:resident",
                agent_id="objective-1:team-runtime:teammate:1",
                role="teammate",
                phase=ResidentCoordinatorPhase.IDLE,
            )
        )

        coordinator_sessions = await host.list_coordinator_sessions(
            role="leader",
            objective_id="objective-1",
            host_owner_coordinator_id="superleader:objective-1",
        )

        self.assertEqual(
            tuple(item.coordinator_id for item in coordinator_sessions),
            ("leader:qa", "leader:runtime"),
        )
        self.assertTrue(all(isinstance(item, CoordinatorSessionState) for item in coordinator_sessions))

    async def test_list_teammate_slot_sessions_filters_to_profiled_host_owned_slots(self) -> None:
        host = InMemoryResidentSessionHost()
        await host.load_or_create_slot_session(
            session_id="team-runtime:teammate:1:resident",
            agent_id="team-runtime:teammate:1",
            objective_id="objective-1",
            lane_id="lane-runtime",
            team_id="team-runtime",
        )
        await host.load_or_create_slot_session(
            session_id="team-runtime:teammate:2:resident",
            agent_id="team-runtime:teammate:2",
            objective_id="objective-1",
            lane_id="lane-runtime",
            team_id="team-runtime",
        )
        await host.record_teammate_activation_profile(
            "team-runtime:teammate:2:resident",
            activation_profile=TeammateActivationProfile(
                backend="in_process",
                working_dir="/tmp/team-runtime",
            ),
        )
        await host.load_or_create_slot_session(
            session_id="team-other:teammate:3:resident",
            agent_id="team-other:teammate:3",
            objective_id="objective-1",
            lane_id="lane-other",
            team_id="team-other",
        )

        profiled_sessions = await host.list_teammate_slot_sessions(
            objective_id="objective-1",
            lane_id="lane-runtime",
            team_id="team-runtime",
            require_activation_profile=True,
        )
        runnable_sessions = await host.list_runnable_teammate_slot_sessions(
            objective_id="objective-1",
            lane_id="lane-runtime",
            team_id="team-runtime",
        )
        all_runtime_sessions = await host.list_teammate_slot_sessions(
            objective_id="objective-1",
            lane_id="lane-runtime",
            team_id="team-runtime",
        )

        self.assertEqual(
            tuple(session.agent_id for session in profiled_sessions),
            ("team-runtime:teammate:2",),
        )
        self.assertEqual(
            tuple(session.agent_id for session in runnable_sessions),
            ("team-runtime:teammate:2",),
        )
        self.assertEqual(
            tuple(session.agent_id for session in all_runtime_sessions),
            ("team-runtime:teammate:1", "team-runtime:teammate:2"),
        )
