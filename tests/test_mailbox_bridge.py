from __future__ import annotations

import sys
from pathlib import Path
from unittest import IsolatedAsyncioTestCase

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agent_orchestra.runtime.mailbox_bridge import InMemoryMailboxBridge
from agent_orchestra.tools.mailbox import MailboxEnvelope, MailboxMessageKind


class MailboxBridgeTest(IsolatedAsyncioTestCase):
    async def test_in_memory_mailbox_bridge_tracks_messages_and_cursor(self) -> None:
        bridge = InMemoryMailboxBridge()

        env1 = await bridge.send(
            MailboxEnvelope(
                envelope_id="env-1",
                mailbox_id="group-a:leader:runtime",
                sender="teammate-1",
                recipient="leader:runtime",
                kind=MailboxMessageKind.TEAMMATE_RESULT,
                subject="Task one complete",
                payload={"task_id": "task-1"},
            )
        )
        env2 = await bridge.send(
            MailboxEnvelope(
                envelope_id="env-2",
                mailbox_id="group-a:leader:runtime",
                sender="teammate-2",
                recipient="leader:runtime",
                kind=MailboxMessageKind.SYSTEM,
                subject="System note",
                payload={"iteration": 2},
            )
        )

        unread = await bridge.list_for_recipient("leader:runtime")
        cursor = await bridge.acknowledge("leader:runtime", ("env-1", "env-2"))
        after_first = await bridge.list_for_recipient("leader:runtime", after_envelope_id="env-1")

        self.assertEqual([item.envelope_id for item in unread], ["env-1", "env-2"])
        self.assertEqual(cursor.last_envelope_id, "env-2")
        self.assertEqual(set(cursor.acknowledged_ids), {"env-1", "env-2"})
        self.assertEqual([item.envelope_id for item in after_first], ["env-2"])
        self.assertEqual(env1.subject, "Task one complete")
        self.assertEqual(env2.kind, MailboxMessageKind.SYSTEM)
