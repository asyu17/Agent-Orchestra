from __future__ import annotations

import sys
from pathlib import Path
from unittest import TestCase

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agent_orchestra.runtime.directed_mailbox_protocol import (
    DirectedTaskDirective,
    DirectedTaskReceipt,
    DirectedTaskResult,
    parse_directed_task_directive,
)


class DirectedMailboxProtocolTest(TestCase):
    def test_parse_canonical_v1_directive_keeps_compat_task_id(self) -> None:
        payload = {
            "protocol": {
                "name": "agent_orchestra.directed_mailbox",
                "version": 1,
                "message_type": "task.directive",
            },
            "directive_id": "dir-1",
            "correlation_id": "corr-1",
            "task": {
                "task_id": "task-1",
                "goal": "Implement mailbox cursor durability.",
                "reason": "Leader delegated runtime work.",
                "scope": "team",
                "derived_from": "leader-turn-2",
                "owned_paths": ["src/agent_orchestra/runtime/leader_loop.py"],
                "verification_commands": ["pytest tests/test_leader_loop.py -q"],
            },
            "target": {
                "worker_id": "group-a:team:runtime:teammate:1",
                "slot": 1,
                "group_id": "group-a",
                "lane_id": "runtime",
                "team_id": "group-a:team:runtime",
                "delivery_id": "obj-runtime:lane:runtime",
            },
            "claim": {
                "mode": "claim_existing_task",
                "claim_source": "resident_mailbox_directed_claim",
                "claim_session_id": "claim-session-1",
                "if_unclaimed": True,
                "expires_at": None,
            },
            "intent": {
                "action": "execute",
                "priority": "normal",
                "requires_ack_stage": "claim_materialized",
            },
            "context": {
                "leader_turn_index": 2,
                "leader_assignment_id": "leader-turn-2",
                "parent_task_id": "task-parent",
                "source_blackboard_ref": "blackboard:entry-42",
            },
            "compat": {
                "task_id": "task-1",
            },
        }

        directive = parse_directed_task_directive(payload=payload, subject="task.directive")

        self.assertIsInstance(directive, DirectedTaskDirective)
        self.assertEqual(directive.task.task_id, "task-1")
        self.assertEqual(directive.compat.task_id, "task-1")
        self.assertEqual(directive.protocol.message_type, "task.directive")

    def test_parse_legacy_directive_normalizes_to_v1_shape(self) -> None:
        directive = parse_directed_task_directive(
            payload={
                "task_id": "task-legacy",
                "claim_source": "resident_mailbox_directed_claim",
                "claim_session_id": "claim-legacy",
            },
            subject="task.directed",
        )

        self.assertEqual(directive.task.task_id, "task-legacy")
        self.assertEqual(directive.compat.task_id, "task-legacy")
        self.assertEqual(directive.claim.claim_source, "resident_mailbox_directed_claim")
        self.assertEqual(directive.claim.claim_session_id, "claim-legacy")

    def test_receipt_and_result_round_trip_to_payload(self) -> None:
        receipt = DirectedTaskReceipt(
            directive_id="dir-1",
            receipt_type="claim_materialized",
            task_id="task-1",
            claim_session_id="claim-1",
            consumer_cursor={
                "stream": "mailbox",
                "offset": "3-0",
                "event_id": "env-3",
                "last_envelope_id": "env-3",
            },
            delivery_id="obj-runtime:lane:runtime",
            status_summary="Claimed task-1 for teammate slot 1.",
            correlation_id="corr-1",
        )
        result = DirectedTaskResult(
            task_id="task-1",
            status="completed",
            summary="Implemented mailbox cursor durability.",
            artifact_refs=("blackboard:entry-1",),
            verification_summary="pytest tests/test_protocol_bridge.py -q",
            correlation_id="corr-1",
            in_reply_to="dir-1",
            compat_task_id="task-1",
        )

        receipt_payload = receipt.to_payload()
        result_payload = result.to_payload()

        self.assertEqual(receipt_payload["protocol"]["message_type"], "task.receipt")
        self.assertEqual(receipt_payload["consumer_cursor"]["last_envelope_id"], "env-3")
        self.assertEqual(result_payload["protocol"]["message_type"], "task.result")
        self.assertEqual(result_payload["compat"]["task_id"], "task-1")
