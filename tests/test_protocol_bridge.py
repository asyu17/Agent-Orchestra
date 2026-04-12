from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agent_orchestra.contracts.agent import AgentSession, SessionBinding
from agent_orchestra.contracts.execution import ResidentCoordinatorPhase
from agent_orchestra.runtime.protocol_bridge import (
    AutoApprovePermissionBroker,
    InMemoryMailboxBridge,
    InMemoryProtocolBus,
    InMemoryReconnectRegistry,
    ProtocolBusEvent,
    ProtocolBusCursor,
    RedisProtocolBus,
    ReconnectCursor,
    RedisMailboxBridge,
    StaticPermissionBroker,
    session_protocol_event,
    protocol_bus_events_from_worker_record,
)
from agent_orchestra.tools.mailbox import (
    MailboxDeliveryMode,
    MailboxEnvelope,
    MailboxSubscription,
    MailboxVisibilityScope,
)
from agent_orchestra.tools.permission_protocol import PermissionRequest


class _FakeRedisClient:
    def __init__(self) -> None:
        self.values: dict[str, list[str]] = {}
        self.scalar_values: dict[str, str] = {}
        self.stream_values: dict[str, list[tuple[str, dict[str, str]]]] = {}
        self.stream_counters: dict[str, int] = {}

    async def rpush(self, key: str, value: str) -> int:
        self.values.setdefault(key, []).append(value)
        return len(self.values[key])

    async def lrange(self, key: str, start: int, end: int) -> list[str]:
        items = self.values.get(key, [])
        if end == -1:
            return items[start:]
        return items[start : end + 1]

    async def lrem(self, key: str, count: int, value: str) -> int:
        items = self.values.get(key, [])
        removed = 0
        kept: list[str] = []
        for item in items:
            if item == value and (count <= 0 or removed < count):
                removed += 1
                continue
            kept.append(item)
        self.values[key] = kept
        return removed

    async def set(self, key: str, value: str) -> bool:
        self.scalar_values[key] = value
        return True

    async def get(self, key: str) -> str | None:
        return self.scalar_values.get(key)

    async def xadd(self, key: str, fields: dict[str, str]) -> str:
        index = self.stream_counters.get(key, 0) + 1
        self.stream_counters[key] = index
        stream_id = f"{index}-0"
        self.stream_values.setdefault(key, []).append((stream_id, dict(fields)))
        return stream_id

    async def xrange(
        self,
        key: str,
        min: str = "-",
        max: str = "+",
        count: int | None = None,
    ) -> list[tuple[str, dict[str, str]]]:
        items = list(self.stream_values.get(key, []))
        if min.startswith("("):
            offset = min[1:]
            items = [item for item in items if item[0] > offset]
        if max not in ("+",):
            items = [item for item in items if item[0] <= max]
        if count is not None:
            items = items[:count]
        return items


class _FakeReconnectStore:
    def __init__(self) -> None:
        self.saved: dict[str, ReconnectCursor] = {}
        self.save_calls = 0
        self.get_calls = 0

    async def save_reconnect_cursor(self, cursor: ReconnectCursor) -> None:
        self.save_calls += 1
        self.saved[cursor.worker_id] = cursor

    async def get_reconnect_cursor(self, worker_id: str) -> ReconnectCursor | None:
        self.get_calls += 1
        return self.saved.get(worker_id)


class ProtocolBridgeTest(IsolatedAsyncioTestCase):
    async def test_reconnect_cursor_to_dict_is_json_safe(self) -> None:
        cursor = ReconnectCursor(
            worker_id="worker-1",
            assignment_id="assignment-1",
            role="teammate",
            backend="subprocess",
            turn_index=2,
            metadata={"path": Path("/tmp/ao/protocol.json")},
        )

        payload = cursor.to_dict()

        self.assertEqual(payload["metadata"]["path"], "/tmp/ao/protocol.json")
        json.dumps(payload)

    async def test_protocol_bus_event_round_trips_takeover_events(self) -> None:
        started = ProtocolBusEvent(
            event_id="evt-takeover-start",
            stream="session",
            event_type="session.takeover_started",
            worker_id="worker-1",
            session_id="session-1",
            assignment_id="assignment-1",
            supervisor_id="supervisor-b",
            lease_id="lease-new",
            cursor={"stream": "session", "offset": "10-0"},
            payload={"reason": "lease_expired"},
            metadata={"source": "reconnector"},
            created_at="2026-04-05T00:01:00+00:00",
        )
        completed = ProtocolBusEvent(
            event_id="evt-takeover-done",
            stream="takeover",
            event_type="session.takeover_completed",
            worker_id="worker-1",
            session_id="session-1",
            assignment_id="assignment-1",
            supervisor_id="supervisor-b",
            lease_id="lease-new",
            cursor={"stream": "takeover", "offset": "11-0"},
            payload={"reattach": True},
            metadata={"source": "reconnector"},
            created_at="2026-04-05T00:01:01+00:00",
        )

        started_roundtrip = ProtocolBusEvent.from_dict(started.to_dict())
        completed_roundtrip = ProtocolBusEvent.from_dict(completed.to_dict())

        self.assertEqual(started_roundtrip.event_type, "session.takeover_started")
        self.assertEqual(started_roundtrip.cursor["offset"], "10-0")
        self.assertEqual(completed_roundtrip.event_type, "session.takeover_completed")
        self.assertEqual(completed_roundtrip.payload["reattach"], True)

    async def test_protocol_bus_event_round_trips_control_event(self) -> None:
        event = ProtocolBusEvent(
            event_id="evt-control-cancel",
            stream="control",
            event_type="control.cancel",
            worker_id="worker-2",
            session_id="session-2",
            assignment_id="assignment-2",
            payload={"reason": "operator_cancelled"},
            metadata={"requested_by": "leader:lane-a"},
            created_at="2026-04-05T00:02:00+00:00",
        )

        restored = ProtocolBusEvent.from_dict(event.to_dict())

        self.assertEqual(restored.stream, "control")
        self.assertEqual(restored.event_type, "control.cancel")
        self.assertEqual(restored.payload["reason"], "operator_cancelled")

    async def test_in_memory_protocol_bus_supports_stream_family_and_cursor_catch_up(self) -> None:
        bus = InMemoryProtocolBus()
        stream_events = (
            ProtocolBusEvent(event_id="evt-lifecycle", stream="lifecycle", event_type="worker.accepted"),
            ProtocolBusEvent(event_id="evt-session", stream="session", event_type="session.active"),
            ProtocolBusEvent(event_id="evt-control", stream="control", event_type="control.verify"),
            ProtocolBusEvent(event_id="evt-takeover", stream="takeover", event_type="session.takeover_started"),
            ProtocolBusEvent(event_id="evt-mailbox", stream="mailbox", event_type="mailbox.enqueued"),
        )
        for item in stream_events:
            await bus.publish(item)

        lifecycle_first = await bus.read("lifecycle", limit=1)
        self.assertEqual(len(lifecycle_first.events), 1)
        self.assertEqual(lifecycle_first.events[0].stream, "lifecycle")
        self.assertEqual(lifecycle_first.events[0].cursor.get("offset"), "1-0")

        await bus.publish(
            ProtocolBusEvent(
                event_id="evt-lifecycle-2",
                stream="lifecycle",
                event_type="worker.checkpoint",
            )
        )
        lifecycle_catch_up = await bus.catch_up(
            "lifecycle",
            cursor=ProtocolBusCursor.from_dict(lifecycle_first.next_cursor),
        )
        self.assertEqual([event.event_id for event in lifecycle_catch_up.events], ["evt-lifecycle-2"])
        self.assertEqual(lifecycle_catch_up.next_cursor.get("offset"), "2-0")

        mailbox_view = await bus.read("mailbox")
        self.assertEqual([event.event_id for event in mailbox_view.events], ["evt-mailbox"])

    async def test_redis_protocol_bus_supports_cursor_based_catch_up(self) -> None:
        client = _FakeRedisClient()
        bus = RedisProtocolBus(client=client, channel_prefix="ao-protocol-test")

        await bus.publish(
            ProtocolBusEvent(
                event_id="evt-session-1",
                stream="session",
                event_type="session.assigned",
                payload={"assignment_id": "assign-1"},
            )
        )
        await bus.publish(
            ProtocolBusEvent(
                event_id="evt-session-2",
                stream="session",
                event_type="session.active",
                payload={"assignment_id": "assign-1"},
            )
        )

        first = await bus.read("session", limit=1)
        self.assertEqual([event.event_id for event in first.events], ["evt-session-1"])
        self.assertEqual(first.next_cursor.get("offset"), "1-0")

        caught_up = await bus.catch_up("session", cursor=first.next_cursor)
        self.assertEqual([event.event_id for event in caught_up.events], ["evt-session-2"])
        self.assertEqual(caught_up.next_cursor.get("offset"), "2-0")

    async def test_protocol_bus_events_from_worker_record_normalizes_supervisor_payload(self) -> None:
        record = SimpleNamespace(
            worker_id="worker-1",
            assignment_id="assign-1",
            metadata={
                "session_id": "session-1",
                "supervisor_id": "supervisor-a",
                "protocol_events": [
                    {
                        "event_id": "evt-accepted",
                        "status": "accepted",
                        "phase": "accepted",
                        "summary": "accepted",
                    },
                    {
                        "event_id": "evt-checkpoint",
                        "status": "running",
                        "phase": "checkpoint",
                        "summary": "checkpoint",
                    },
                ],
                "final_report": {
                    "assignment_id": "assign-1",
                    "worker_id": "worker-1",
                    "terminal_status": "completed",
                    "summary": "all good",
                },
                "protocol_bus_events": [
                    {
                        "event_id": "evt-takeover-done",
                        "stream": "takeover",
                        "event_type": "session.takeover_completed",
                        "payload": {"reattach": True},
                    }
                ],
            },
        )

        events = protocol_bus_events_from_worker_record(record)
        event_types = {event.event_type for event in events}
        streams = {event.stream for event in events}

        self.assertIn("worker.accepted", event_types)
        self.assertIn("worker.checkpoint", event_types)
        self.assertIn("worker.final_report", event_types)
        self.assertIn("session.takeover_completed", event_types)
        self.assertIn("lifecycle", streams)
        self.assertIn("takeover", streams)

    async def test_session_protocol_event_includes_binding_payload(self) -> None:
        session = AgentSession(
            session_id="session-5",
            agent_id="teammate:5",
            role="teammate",
            phase=ResidentCoordinatorPhase.RUNNING,
        )
        binding = SessionBinding(
            session_id="session-5",
            backend="tmux",
            binding_type="resident",
            transport_locator={"pane_id": "%2"},
            supervisor_id="supervisor-b",
            lease_id="lease-b",
            lease_expires_at="2026-04-05T13:00:00+00:00",
        )
        session.current_binding = binding

        event = session_protocol_event(
            session=session,
            binding=binding,
            event_type="session.marked_active",
            payload={"reason": "hosted"},
        )

        self.assertEqual(event.stream, "session")
        self.assertEqual(event.event_type, "session.marked_active")
        self.assertEqual(event.payload["binding"]["backend"], "tmux")
        self.assertEqual(event.cursor["session_phase"], "running")
        self.assertEqual(event.supervisor_id, "supervisor-b")
        self.assertEqual(event.lease_id, "lease-b")

    async def test_in_memory_mailbox_bridge_sends_polls_and_acks(self) -> None:
        bridge = InMemoryMailboxBridge()

        sent = await bridge.send(
            MailboxEnvelope(
                sender="team-a:teammate:1",
                recipient="leader:lane-a",
                subject="task.completed",
                payload={"task_id": "task-1", "summary": "Implemented the reducer helper."},
            )
        )

        inbox = await bridge.poll("leader:lane-a")

        self.assertEqual(len(inbox), 1)
        self.assertEqual(inbox[0].subject, "task.completed")
        self.assertEqual(inbox[0].payload["task_id"], "task-1")
        self.assertIsNotNone(sent.envelope_id)

        await bridge.ack("leader:lane-a", sent.envelope_id)

        self.assertEqual(await bridge.poll("leader:lane-a"), ())

    async def test_in_memory_mailbox_bridge_directed_task_envelope_is_not_redelivered_after_ack(self) -> None:
        bridge = InMemoryMailboxBridge()

        first = await bridge.send(
            MailboxEnvelope(
                sender="leader:lane-a",
                recipient="team-a:teammate:1",
                subject="task.directed",
                payload={"task_id": "task-1"},
            )
        )
        second = await bridge.send(
            MailboxEnvelope(
                sender="leader:lane-a",
                recipient="team-a:teammate:1",
                subject="task.directed",
                payload={"task_id": "task-2"},
            )
        )

        initial = await bridge.poll("team-a:teammate:1")
        cursor = await bridge.acknowledge("team-a:teammate:1", (first.envelope_id,))
        remaining = await bridge.poll("team-a:teammate:1")

        self.assertEqual([item.payload["task_id"] for item in initial], ["task-1", "task-2"])
        self.assertEqual(cursor.last_envelope_id, first.envelope_id)
        self.assertEqual(cursor.acknowledged_ids, (first.envelope_id,))
        self.assertEqual([item.envelope_id for item in remaining], [second.envelope_id])

        await bridge.ack("team-a:teammate:1", second.envelope_id)

        self.assertEqual(await bridge.poll("team-a:teammate:1"), ())

    async def test_redis_mailbox_bridge_uses_transport_client(self) -> None:
        client = _FakeRedisClient()
        bridge = RedisMailboxBridge(client=client, channel_prefix="ao-test")

        sent = await bridge.send(
            MailboxEnvelope(
                sender="leader:lane-a",
                recipient="team-a:teammate:1",
                subject="directive",
                payload={"goal": "Implement the runtime reducer."},
            )
        )

        polled = await bridge.poll("team-a:teammate:1")

        self.assertEqual(len(polled), 1)
        self.assertEqual(polled[0].payload["goal"], "Implement the runtime reducer.")

        await bridge.ack("team-a:teammate:1", sent.envelope_id)

        self.assertEqual(await bridge.poll("team-a:teammate:1"), ())

    async def test_redis_mailbox_bridge_tracks_cursor_and_lists_messages_after_ack(self) -> None:
        client = _FakeRedisClient()
        bridge = RedisMailboxBridge(client=client, channel_prefix="ao-test")

        sent_1 = await bridge.send(
            MailboxEnvelope(
                sender="leader:lane-a",
                recipient="team-a:teammate:1",
                subject="directive-1",
                payload={"goal": "Implement the reducer."},
            )
        )
        sent_2 = await bridge.send(
            MailboxEnvelope(
                sender="leader:lane-a",
                recipient="team-a:teammate:1",
                subject="directive-2",
                payload={"goal": "Add delivery-state persistence."},
            )
        )

        cursor = await bridge.acknowledge("team-a:teammate:1", (sent_1.envelope_id,))
        after_first = await bridge.list_for_recipient(
            "team-a:teammate:1",
            after_envelope_id=sent_1.envelope_id,
        )
        polled = await bridge.poll("team-a:teammate:1")
        loaded_cursor = await bridge.get_cursor("team-a:teammate:1")

        self.assertEqual(cursor.last_envelope_id, sent_1.envelope_id)
        self.assertEqual(cursor.acknowledged_ids, (sent_1.envelope_id,))
        self.assertEqual([item.envelope_id for item in after_first], [sent_2.envelope_id])
        self.assertEqual([item.envelope_id for item in polled], [sent_2.envelope_id])
        self.assertEqual(loaded_cursor.last_envelope_id, sent_1.envelope_id)

    async def test_in_memory_mailbox_bridge_exposes_append_only_pool_and_digest_views(self) -> None:
        bridge = InMemoryMailboxBridge()
        leader_subscription = await bridge.ensure_subscription(
            MailboxSubscription(
                subscriber="leader:lane-a",
                recipient="leader:lane-a",
                delivery_mode=MailboxDeliveryMode.FULL_TEXT,
            )
        )
        superleader_subscription = await bridge.ensure_subscription(
            MailboxSubscription(
                subscriber="superleader:obj-runtime",
                group_id="group-a",
                lane_id="lane-a",
                visibility_scopes=("shared",),
                delivery_mode=MailboxDeliveryMode.SUMMARY_PLUS_REF,
            )
        )

        sent = await bridge.send(
            MailboxEnvelope(
                sender="team-a:teammate:1",
                recipient="leader:lane-a",
                subject="task.completed",
                group_id="group-a",
                lane_id="lane-a",
                team_id="team-a",
                summary="Reducer helper implemented and verified.",
                full_text_ref="blackboard:entry-1",
                visibility_scope="shared",
                tags=("execution_report",),
                payload={"details": "full teammate output"},
            )
        )

        pool = await bridge.list_message_pool()
        leader_view = await bridge.list_for_subscription(
            "leader:lane-a",
            subscription_id=leader_subscription.subscription_id,
        )
        superleader_view = await bridge.list_for_subscription(
            "superleader:obj-runtime",
            subscription_id=superleader_subscription.subscription_id,
        )

        self.assertEqual([item.envelope_id for item in pool], [sent.envelope_id])
        self.assertEqual([item.envelope_id for item in leader_view], [sent.envelope_id])
        self.assertEqual([item.envelope_id for item in superleader_view], [sent.envelope_id])
        self.assertEqual(superleader_view[0].summary, "Reducer helper implemented and verified.")
        self.assertEqual(superleader_view[0].full_text_ref, "blackboard:entry-1")
        self.assertEqual(superleader_view[0].delivery_mode, MailboxDeliveryMode.SUMMARY_PLUS_REF)

    async def test_redis_mailbox_bridge_tracks_subscription_cursor_independently(self) -> None:
        client = _FakeRedisClient()
        bridge = RedisMailboxBridge(client=client, channel_prefix="ao-test")
        subscription = await bridge.ensure_subscription(
            MailboxSubscription(
                subscriber="superleader:obj-runtime",
                group_id="group-a",
                lane_id="lane-a",
                visibility_scopes=("shared",),
                delivery_mode=MailboxDeliveryMode.SUMMARY_ONLY,
            )
        )

        sent_1 = await bridge.send(
            MailboxEnvelope(
                sender="leader:lane-a",
                recipient="leader:lane-a",
                subject="task.completed",
                group_id="group-a",
                lane_id="lane-a",
                summary="Turn one finished.",
                full_text_ref="blackboard:entry-1",
                visibility_scope="shared",
            )
        )
        sent_2 = await bridge.send(
            MailboxEnvelope(
                sender="leader:lane-a",
                recipient="leader:lane-a",
                subject="task.blocked",
                group_id="group-a",
                lane_id="lane-a",
                summary="Turn two needs escalation.",
                full_text_ref="blackboard:entry-2",
                visibility_scope="shared",
            )
        )

        initial_view = await bridge.poll_subscription(
            "superleader:obj-runtime",
            subscription_id=subscription.subscription_id,
        )
        cursor = await bridge.acknowledge_subscription(
            "superleader:obj-runtime",
            (sent_1.envelope_id,),
            subscription_id=subscription.subscription_id,
        )
        remaining_view = await bridge.poll_subscription(
            "superleader:obj-runtime",
            subscription_id=subscription.subscription_id,
        )
        loaded_cursor = await bridge.get_subscription_cursor(
            "superleader:obj-runtime",
            subscription_id=subscription.subscription_id,
        )
        direct_cursor = await bridge.get_cursor("superleader:obj-runtime")

        self.assertEqual([item.envelope_id for item in initial_view], [sent_1.envelope_id, sent_2.envelope_id])
        self.assertEqual(cursor.subscription_id, subscription.subscription_id)
        self.assertEqual(cursor.last_envelope_id, sent_1.envelope_id)
        self.assertEqual(cursor.acknowledged_ids, (sent_1.envelope_id,))
        self.assertEqual([item.envelope_id for item in remaining_view], [sent_2.envelope_id])
        self.assertEqual(loaded_cursor.last_envelope_id, sent_1.envelope_id)
        self.assertIsNone(direct_cursor.last_envelope_id)
        self.assertEqual(remaining_view[0].delivery_mode, MailboxDeliveryMode.SUMMARY_ONLY)
        self.assertIsNone(remaining_view[0].full_text_ref)

    async def test_in_memory_mailbox_bridge_applies_shared_vs_control_private_policy(self) -> None:
        bridge = InMemoryMailboxBridge()
        shared_subscription = await bridge.ensure_subscription(
            MailboxSubscription(
                subscriber="superleader:obj-runtime",
                group_id="group-a",
                lane_id="lane-a",
                visibility_scopes=(MailboxVisibilityScope.SHARED,),
                delivery_mode=MailboxDeliveryMode.SUMMARY_ONLY,
            )
        )
        private_subscription = await bridge.ensure_subscription(
            MailboxSubscription(
                subscriber="leader:lane-a",
                recipient="leader:lane-a",
                visibility_scopes=(MailboxVisibilityScope.CONTROL_PRIVATE,),
                delivery_mode=MailboxDeliveryMode.FULL_TEXT,
            )
        )

        direct = await bridge.send(
            MailboxEnvelope(
                sender="system.runtime",
                recipient="leader:lane-a",
                subject="control.directive",
                summary="Only leader should receive this full control directive.",
                payload={"detail": "full directive payload"},
            )
        )
        shared = await bridge.send(
            MailboxEnvelope(
                sender="team-a:teammate:1",
                recipient="leader:lane-a",
                subject="task.completed",
                group_id="group-a",
                lane_id="lane-a",
                visibility_scope=MailboxVisibilityScope.SHARED,
                summary="Shared execution summary.",
                full_text_ref="blackboard:entry-9",
                payload={"detail": "teammate output"},
            )
        )

        shared_view = await bridge.list_for_subscription(
            "superleader:obj-runtime",
            subscription_id=shared_subscription.subscription_id,
        )
        private_view = await bridge.list_for_subscription(
            "leader:lane-a",
            subscription_id=private_subscription.subscription_id,
        )

        self.assertEqual([item.envelope_id for item in shared_view], [shared.envelope_id])
        self.assertEqual([item.envelope_id for item in private_view], [direct.envelope_id])
        self.assertEqual(private_view[0].visibility_scope, MailboxVisibilityScope.CONTROL_PRIVATE)
        self.assertEqual(private_view[0].delivery_mode, MailboxDeliveryMode.FULL_TEXT)
        self.assertEqual(private_view[0].payload["detail"], "full directive payload")
        self.assertEqual(shared_view[0].visibility_scope, MailboxVisibilityScope.SHARED)
        self.assertEqual(shared_view[0].delivery_mode, MailboxDeliveryMode.SUMMARY_ONLY)
        self.assertEqual(shared_view[0].summary, "Shared execution summary.")
        self.assertEqual(shared_view[0].full_text_ref, None)

    async def test_permission_brokers_can_auto_approve_and_deny(self) -> None:
        auto = AutoApprovePermissionBroker()
        auto_decision = await auto.request(
            PermissionRequest(
                requester="leader:lane-a",
                action="leader.turn",
                rationale="Need to execute the next coordination turn.",
            )
        )

        broker = StaticPermissionBroker(default_approved=False, approved_actions={"leader.turn"})
        allowed = await broker.request(
            PermissionRequest(
                requester="leader:lane-a",
                action="leader.turn",
                rationale="Need to execute the next coordination turn.",
            )
        )
        denied = await broker.request(
            PermissionRequest(
                requester="team-a:teammate:1",
                action="teammate.turn",
                rationale="Need to execute a teammate task.",
            )
        )

        self.assertTrue(auto_decision.approved)
        self.assertTrue(allowed.approved)
        self.assertFalse(denied.approved)

    async def test_permission_brokers_can_mark_pending_actions(self) -> None:
        broker = StaticPermissionBroker(
            default_approved=False,
            pending_actions={"resident.idle_wait"},
        )
        pending = await broker.request(
            PermissionRequest(
                requester="leader:lane-a",
                action="resident.idle_wait",
                rationale="Keep the resident shell idle-attached.",
            )
        )

        self.assertFalse(pending.approved)
        self.assertTrue(pending.pending)

    async def test_reconnect_registry_remembers_latest_cursor(self) -> None:
        registry = InMemoryReconnectRegistry()
        cursor = ReconnectCursor(
            worker_id="leader:lane-a",
            assignment_id="task-1:leader-turn-2",
            role="leader",
            backend="in_process",
            turn_index=2,
            task_id="task-1",
            metadata={"team_id": "group-a:team:lane-a"},
        )

        await registry.remember(cursor)
        loaded = await registry.resolve("leader:lane-a")

        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.assignment_id, "task-1:leader-turn-2")
        self.assertEqual(loaded.turn_index, 2)

    async def test_reconnect_registry_prefers_store_backed_interface_when_available(self) -> None:
        store = _FakeReconnectStore()
        registry = InMemoryReconnectRegistry(store=store)
        cursor = ReconnectCursor(
            worker_id="leader:lane-a",
            assignment_id="task-9:leader-turn-3",
            role="leader",
            backend="in_process",
            turn_index=3,
            metadata={"lane_id": "lane-a"},
        )

        await registry.remember(cursor)
        loaded = await registry.resolve("leader:lane-a")

        self.assertEqual(store.save_calls, 1)
        self.assertGreaterEqual(store.get_calls, 1)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.assignment_id, "task-9:leader-turn-3")
        self.assertEqual(loaded.turn_index, 3)
