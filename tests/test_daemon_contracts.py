from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest import IsolatedAsyncioTestCase

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agent_orchestra.contracts.daemon import (
    AgentIncarnation,
    AgentIncarnationStatus,
    AgentSlot,
    AgentSlotStatus,
    DaemonCommandEnvelope,
    DaemonEventEnvelope,
    ProviderRouteHealth,
    ProviderRouteStatus,
    SessionAttachment,
    SessionAttachmentStatus,
    SlotFailureClass,
    SlotHealthEvent,
)
from agent_orchestra.storage.in_memory import InMemoryOrchestrationStore


class DaemonContractsTest(IsolatedAsyncioTestCase):
    async def test_agent_slot_round_trip_keeps_json_safe_payload(self) -> None:
        slot = AgentSlot(
            slot_id="leader:lane:runtime",
            role="leader",
            work_session_id="worksession-1",
            resident_team_shell_id="shell-1",
            status=AgentSlotStatus.ACTIVE,
            desired_state="active",
            preferred_backend="tmux",
            preferred_transport_class="full_resident_transport",
            current_incarnation_id="inc-1",
            current_lease_id="lease-1",
            restart_count=2,
            last_failure_class=SlotFailureClass.RECOVERABLE_ABNORMAL,
            last_failure_reason="transport lost",
            created_at="2026-04-12T10:00:00+00:00",
            updated_at="2026-04-12T10:01:00+00:00",
            metadata={"owner": "daemon"},
        )

        payload = slot.to_dict()
        restored = AgentSlot.from_dict(payload)

        self.assertEqual(restored.slot_id, "leader:lane:runtime")
        self.assertEqual(restored.status, AgentSlotStatus.ACTIVE)
        self.assertEqual(restored.last_failure_class, SlotFailureClass.RECOVERABLE_ABNORMAL)
        self.assertEqual(restored.restart_count, 2)
        json.dumps(payload)

    async def test_incarnation_health_attachment_route_and_envelopes_round_trip(self) -> None:
        incarnation = AgentIncarnation(
            incarnation_id="inc-1",
            slot_id="leader:lane:runtime",
            work_session_id="worksession-1",
            runtime_generation_id="runtimegen-1",
            status=AgentIncarnationStatus.ACTIVE,
            backend="tmux",
            transport_locator={"session_name": "ao-runtime", "pane_id": "%42"},
            lease_id="lease-1",
            restart_generation=1,
            started_at="2026-04-12T10:00:00+00:00",
            ended_at=None,
            terminal_failure_class=None,
            terminal_reason=None,
            metadata={"launch_reason": "daemon_boot"},
        )
        event = SlotHealthEvent(
            event_id="slotevt-1",
            slot_id="leader:lane:runtime",
            incarnation_id="inc-1",
            work_session_id="worksession-1",
            event_kind="lease_heartbeat",
            failure_class=None,
            observed_at="2026-04-12T10:01:00+00:00",
            detail="heartbeat ok",
            metadata={"lag_seconds": 0},
        )
        attachment = SessionAttachment(
            attachment_id="attach-1",
            work_session_id="worksession-1",
            resident_team_shell_id="shell-1",
            slot_id="leader:lane:runtime",
            incarnation_id="inc-1",
            client_id="cli-1",
            status=SessionAttachmentStatus.ATTACHED,
            attached_at="2026-04-12T10:02:00+00:00",
            detached_at=None,
            last_event_id="evt-10",
            metadata={"tty": "/dev/ttys001"},
        )
        route_health = ProviderRouteHealth(
            route_key="leader/openai/gpt-5",
            role="leader",
            backend="openai",
            route_fingerprint="model:gpt-5",
            status=ProviderRouteStatus.HEALTHY,
            health_score=1.0,
            consecutive_failures=0,
            last_failure_class=None,
            cooldown_expires_at=None,
            preferred=True,
            updated_at="2026-04-12T10:03:00+00:00",
            metadata={"source": "slot-supervisor"},
        )
        command = DaemonCommandEnvelope(
            command_id="cmd-1",
            command="session.attach",
            payload={"work_session_id": "worksession-1"},
            created_at="2026-04-12T10:04:00+00:00",
            metadata={"client_id": "cli-1"},
        )
        daemon_event = DaemonEventEnvelope(
            event_id="evt-1",
            event_kind="slot.updated",
            payload={"slot_id": "leader:lane:runtime"},
            created_at="2026-04-12T10:04:10+00:00",
            metadata={"source": "daemon"},
        )

        restored_incarnation = AgentIncarnation.from_dict(incarnation.to_dict())
        restored_event = SlotHealthEvent.from_dict(event.to_dict())
        restored_attachment = SessionAttachment.from_dict(attachment.to_dict())
        restored_route = ProviderRouteHealth.from_dict(route_health.to_dict())
        restored_command = DaemonCommandEnvelope.from_dict(command.to_dict())
        restored_daemon_event = DaemonEventEnvelope.from_dict(daemon_event.to_dict())

        self.assertEqual(restored_incarnation.lease_id, "lease-1")
        self.assertEqual(restored_event.event_kind, "lease_heartbeat")
        self.assertEqual(restored_attachment.status, SessionAttachmentStatus.ATTACHED)
        self.assertEqual(restored_route.status, ProviderRouteStatus.HEALTHY)
        self.assertEqual(restored_command.command, "session.attach")
        self.assertEqual(restored_daemon_event.event_kind, "slot.updated")
        json.dumps(incarnation.to_dict())
        json.dumps(event.to_dict())
        json.dumps(attachment.to_dict())
        json.dumps(route_health.to_dict())
        json.dumps(command.to_dict())
        json.dumps(daemon_event.to_dict())

    async def test_in_memory_store_persists_daemon_entities(self) -> None:
        store = InMemoryOrchestrationStore()

        slot = AgentSlot(
            slot_id="teammate:team-a:slot:1",
            role="teammate",
            work_session_id="worksession-2",
            resident_team_shell_id="shell-2",
            status=AgentSlotStatus.BOOTING,
            desired_state="active",
            created_at="2026-04-12T11:00:00+00:00",
            updated_at="2026-04-12T11:00:00+00:00",
        )
        incarnation = AgentIncarnation(
            incarnation_id="inc-2",
            slot_id=slot.slot_id,
            work_session_id=slot.work_session_id,
            runtime_generation_id="runtimegen-2",
            status=AgentIncarnationStatus.BOOTING,
            backend="codex_cli",
            lease_id="lease-2",
            restart_generation=0,
            started_at="2026-04-12T11:00:01+00:00",
        )
        health_event = SlotHealthEvent(
            event_id="slotevt-2",
            slot_id=slot.slot_id,
            incarnation_id=incarnation.incarnation_id,
            work_session_id=slot.work_session_id,
            event_kind="started",
            observed_at="2026-04-12T11:00:02+00:00",
        )
        attachment = SessionAttachment(
            attachment_id="attach-2",
            work_session_id=slot.work_session_id,
            resident_team_shell_id=slot.resident_team_shell_id,
            slot_id=slot.slot_id,
            incarnation_id=incarnation.incarnation_id,
            client_id="cli-2",
            status=SessionAttachmentStatus.ATTACHED,
            attached_at="2026-04-12T11:00:03+00:00",
        )
        route_health = ProviderRouteHealth(
            route_key="teammate/openai/gpt-5-mini",
            role="teammate",
            backend="openai",
            route_fingerprint="model:gpt-5-mini",
            status=ProviderRouteStatus.DEGRADED_PROVIDER,
            health_score=0.2,
            consecutive_failures=3,
            last_failure_class=SlotFailureClass.EXTERNAL_DEGRADED,
            cooldown_expires_at="2026-04-12T11:15:00+00:00",
            preferred=False,
            updated_at="2026-04-12T11:00:04+00:00",
        )

        await store.save_agent_slot(slot)
        await store.save_agent_incarnation(incarnation)
        await store.append_slot_health_event(health_event)
        await store.save_session_attachment(attachment)
        await store.save_provider_route_health(route_health)

        loaded_slot = await store.get_agent_slot(slot.slot_id)
        loaded_incarnation = await store.get_agent_incarnation(incarnation.incarnation_id)
        loaded_attachment = await store.get_session_attachment(attachment.attachment_id)
        loaded_route = await store.get_provider_route_health(route_health.route_key)

        self.assertEqual(loaded_slot, slot)
        self.assertEqual(loaded_incarnation, incarnation)
        self.assertEqual(loaded_attachment, attachment)
        self.assertEqual(loaded_route, route_health)
        self.assertEqual(
            [item.slot_id for item in await store.list_agent_slots(work_session_id=slot.work_session_id)],
            [slot.slot_id],
        )
        self.assertEqual(
            [item.incarnation_id for item in await store.list_agent_incarnations(slot_id=slot.slot_id)],
            [incarnation.incarnation_id],
        )
        self.assertEqual(
            [item.event_id for item in await store.list_slot_health_events(slot_id=slot.slot_id)],
            [health_event.event_id],
        )
        self.assertEqual(
            [item.attachment_id for item in await store.list_session_attachments(slot.work_session_id)],
            [attachment.attachment_id],
        )
        self.assertEqual(
            [item.route_key for item in await store.list_provider_route_health(role="teammate")],
            [route_health.route_key],
        )
