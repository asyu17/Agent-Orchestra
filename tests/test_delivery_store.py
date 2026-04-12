from __future__ import annotations

import sys
from pathlib import Path
from unittest import IsolatedAsyncioTestCase

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agent_orchestra.contracts.authority import AuthorityDecision, ScopeExtensionRequest
from agent_orchestra.contracts.blackboard import BlackboardEntry
from agent_orchestra.contracts.agent import AgentSession
from agent_orchestra.contracts.delivery import DeliveryState, DeliveryStateKind, DeliveryStatus
from agent_orchestra.contracts.enums import BlackboardEntryKind, BlackboardKind, TaskStatus
from agent_orchestra.contracts.task import TaskCard
from agent_orchestra.storage.base import (
    AuthorityDecisionStoreCommit,
    AuthorityRequestStoreCommit,
    DirectedTaskReceiptStoreCommit,
    MailboxConsumeStoreCommit,
    ProtocolBusCursorCommit,
    TeammateResultStoreCommit,
)
from agent_orchestra.storage.in_memory import InMemoryOrchestrationStore


class DeliveryStateStoreTest(IsolatedAsyncioTestCase):
    async def test_in_memory_store_persists_delivery_states(self) -> None:
        store = InMemoryOrchestrationStore()
        lane_state = DeliveryState(
            delivery_id="obj-1:lane:runtime",
            objective_id="obj-1",
            kind=DeliveryStateKind.LANE,
            status=DeliveryStatus.RUNNING,
            lane_id="runtime",
            team_id="group-a:team:runtime",
            iteration=1,
            summary="First leader turn is running.",
        )
        objective_state = DeliveryState(
            delivery_id="obj-1:objective",
            objective_id="obj-1",
            kind=DeliveryStateKind.OBJECTIVE,
            status=DeliveryStatus.PENDING,
            summary="Objective created.",
        )

        await store.save_delivery_state(lane_state)
        await store.save_delivery_state(objective_state)

        loaded_lane = await store.get_delivery_state("obj-1:lane:runtime")
        delivery_states = await store.list_delivery_states("obj-1")

        self.assertIs(loaded_lane, lane_state)
        self.assertEqual({state.delivery_id for state in delivery_states}, {"obj-1:lane:runtime", "obj-1:objective"})

    async def test_in_memory_store_preserves_structured_delivery_mailbox_cursor(self) -> None:
        store = InMemoryOrchestrationStore()
        lane_state = DeliveryState(
            delivery_id="obj-2:lane:runtime",
            objective_id="obj-2",
            kind=DeliveryStateKind.LANE,
            status=DeliveryStatus.RUNNING,
            lane_id="runtime",
            team_id="group-a:team:runtime",
            iteration=3,
            summary="Resume from authoritative mailbox consumer cursor.",
            mailbox_cursor={
                "stream": "mailbox",
                "offset": "2-0",
                "event_id": "env-2",
                "last_envelope_id": "env-2",
            },
        )

        await store.save_delivery_state(lane_state)
        loaded = await store.get_delivery_state("obj-2:lane:runtime")

        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(loaded.mailbox_cursor["offset"], "2-0")
        self.assertEqual(loaded.mailbox_cursor["event_id"], "env-2")
        self.assertEqual(loaded.mailbox_cursor["last_envelope_id"], "env-2")

    async def test_in_memory_store_commits_directed_task_receipt_coordination_state(self) -> None:
        store = InMemoryOrchestrationStore()
        cursor = {
            "offset": "4-0",
            "event_id": "env-4",
            "last_envelope_id": "env-4",
        }
        receipt_task = TaskCard(
            task_id="task-receipt-1",
            goal="Materialize the directed task claim.",
            lane="runtime",
            group_id="group-a",
            team_id="group-a:team:runtime",
            owner_id="group-a:team:runtime:teammate:1",
            claim_session_id="claim-1",
            claimed_at="2026-04-05T02:00:00+00:00",
            claim_source="teammate.directed",
            status=TaskStatus.IN_PROGRESS,
        )
        receipt_entry = BlackboardEntry(
            entry_id="entry-receipt-1",
            blackboard_id="group-a:blackboard:runtime",
            group_id="group-a",
            kind=BlackboardKind.TEAM,
            entry_kind=BlackboardEntryKind.EXECUTION_REPORT,
            author_id="group-a:team:runtime:teammate:1",
            lane_id="runtime",
            team_id="group-a:team:runtime",
            task_id="task-receipt-1",
            summary="Directed receipt committed.",
            payload={"event": "task.receipt"},
            created_at="2026-04-05T02:00:01+00:00",
        )
        receipt_delivery_state = DeliveryState(
            delivery_id="obj-commit:lane:runtime",
            objective_id="obj-commit",
            kind=DeliveryStateKind.LANE,
            status=DeliveryStatus.RUNNING,
            lane_id="runtime",
            team_id="group-a:team:runtime",
            iteration=2,
            summary="Receipt committed with task and cursor.",
            active_task_ids=("task-receipt-1",),
            mailbox_cursor=cursor,
        )
        receipt_session = AgentSession(
            session_id="group-a:team:runtime:teammate:1:resident",
            agent_id="group-a:team:runtime:teammate:1",
            role="teammate",
            lane_id="runtime",
            team_id="group-a:team:runtime",
            objective_id="obj-commit",
            mailbox_cursor={"stream": "mailbox", **cursor},
            current_directive_ids=("task-receipt-1",),
            metadata={"current_claim_session_id": "claim-1"},
        )

        await store.commit_directed_task_receipt(
            DirectedTaskReceiptStoreCommit(
                task=receipt_task,
                blackboard_entry=receipt_entry,
                delivery_state=receipt_delivery_state,
                protocol_bus_cursor=ProtocolBusCursorCommit(
                    stream="mailbox",
                    consumer="group-a:team:runtime:teammate:1",
                    cursor=cursor,
                ),
                agent_session=receipt_session,
            )
        )

        cursor["offset"] = "stale"

        loaded_task = await store.get_task("task-receipt-1")
        loaded_entries = await store.list_blackboard_entries("group-a:blackboard:runtime")
        loaded_delivery = await store.get_delivery_state("obj-commit:lane:runtime")
        loaded_cursor = await store.get_protocol_bus_cursor(
            stream="mailbox",
            consumer="group-a:team:runtime:teammate:1",
        )
        loaded_session = await store.get_agent_session("group-a:team:runtime:teammate:1:resident")

        self.assertIs(loaded_task, receipt_task)
        self.assertEqual(loaded_entries, [receipt_entry])
        self.assertIs(loaded_delivery, receipt_delivery_state)
        self.assertEqual(loaded_cursor["offset"], "4-0")
        self.assertEqual(loaded_cursor["event_id"], "env-4")
        self.assertEqual(loaded_session, receipt_session)
        self.assertEqual(loaded_session.mailbox_cursor["last_envelope_id"], "env-4")
        self.assertEqual(loaded_session.current_directive_ids, ("task-receipt-1",))

    async def test_in_memory_store_commits_teammate_result_coordination_state(self) -> None:
        store = InMemoryOrchestrationStore()
        result_task = TaskCard(
            task_id="task-result-1",
            goal="Persist the teammate result.",
            lane="runtime",
            group_id="group-a",
            team_id="group-a:team:runtime",
            owner_id="group-a:team:runtime:teammate:1",
            claim_session_id="claim-2",
            claimed_at="2026-04-05T02:10:00+00:00",
            claim_source="teammate.directed",
            status=TaskStatus.COMPLETED,
        )
        result_entry = BlackboardEntry(
            entry_id="entry-result-1",
            blackboard_id="group-a:blackboard:runtime",
            group_id="group-a",
            kind=BlackboardKind.TEAM,
            entry_kind=BlackboardEntryKind.EXECUTION_REPORT,
            author_id="group-a:team:runtime:teammate:1",
            lane_id="runtime",
            team_id="group-a:team:runtime",
            task_id="task-result-1",
            summary="Teammate result committed.",
            payload={"event": "task.result"},
            created_at="2026-04-05T02:10:01+00:00",
        )
        result_delivery_state = DeliveryState(
            delivery_id="obj-result:lane:runtime",
            objective_id="obj-result",
            kind=DeliveryStateKind.LANE,
            status=DeliveryStatus.RUNNING,
            lane_id="runtime",
            team_id="group-a:team:runtime",
            iteration=3,
            summary="Result committed with task and delivery snapshot.",
            completed_task_ids=("task-result-1",),
            latest_worker_ids=("group-a:team:runtime:teammate:1",),
        )
        result_session = AgentSession(
            session_id="group-a:team:runtime:teammate:1:resident",
            agent_id="group-a:team:runtime:teammate:1",
            role="teammate",
            lane_id="runtime",
            team_id="group-a:team:runtime",
            objective_id="obj-result",
            metadata={"last_worker_session_id": "worker-session-1"},
        )

        await store.commit_teammate_result(
            TeammateResultStoreCommit(
                task=result_task,
                blackboard_entry=result_entry,
                delivery_state=result_delivery_state,
                agent_session=result_session,
            )
        )

        loaded_task = await store.get_task("task-result-1")
        loaded_entries = await store.list_blackboard_entries("group-a:blackboard:runtime")
        loaded_delivery = await store.get_delivery_state("obj-result:lane:runtime")
        loaded_session = await store.get_agent_session("group-a:team:runtime:teammate:1:resident")

        self.assertIs(loaded_task, result_task)
        self.assertEqual(loaded_entries, [result_entry])
        self.assertIs(loaded_delivery, result_delivery_state)
        self.assertEqual(loaded_session, result_session)
        self.assertEqual(loaded_session.metadata["last_worker_session_id"], "worker-session-1")

    async def test_in_memory_store_commits_mailbox_consume_with_agent_session_snapshot(self) -> None:
        store = InMemoryOrchestrationStore()
        cursor = {
            "stream": "mailbox",
            "event_id": "env-11",
            "last_envelope_id": "env-11",
        }
        session = AgentSession(
            session_id="group-a:team:runtime:teammate:1:resident",
            agent_id="group-a:team:runtime:teammate:1",
            role="teammate",
            objective_id="obj-mailbox",
            lane_id="runtime",
            team_id="group-a:team:runtime",
            mailbox_cursor=cursor,
            current_directive_ids=("task-11",),
            last_reason="Committed teammate mailbox consume.",
        )

        await store.commit_mailbox_consume(
            MailboxConsumeStoreCommit(
                recipient="group-a:team:runtime:teammate:1",
                envelope_ids=("env-10", "env-11"),
                agent_session=session,
                protocol_bus_cursor=ProtocolBusCursorCommit(
                    stream="mailbox",
                    consumer="group-a:team:runtime:teammate:1",
                    cursor=cursor,
                ),
            )
        )

        cursor["last_envelope_id"] = "stale"

        loaded_cursor = await store.get_protocol_bus_cursor(
            stream="mailbox",
            consumer="group-a:team:runtime:teammate:1",
        )
        loaded_session = await store.get_agent_session("group-a:team:runtime:teammate:1:resident")

        self.assertEqual(loaded_cursor["last_envelope_id"], "env-11")
        self.assertEqual(loaded_session.mailbox_cursor["last_envelope_id"], "env-11")
        self.assertEqual(loaded_session.current_directive_ids, ("task-11",))

    async def test_in_memory_store_commits_authority_request_coordination_state(self) -> None:
        store = InMemoryOrchestrationStore()
        authority_task = TaskCard(
            task_id="task-authority-1",
            goal="Request authority for protected runtime fix.",
            lane="runtime",
            group_id="group-a",
            team_id="group-a:team:runtime",
            owner_id="group-a:team:runtime:teammate:1",
            claim_session_id="claim-auth-1",
            claimed_at="2026-04-06T02:00:00+00:00",
            claim_source="teammate.directed",
            status=TaskStatus.WAITING_FOR_AUTHORITY,
            authority_request_id="auth-req-1",
            authority_request_payload={"request_id": "auth-req-1", "task_id": "task-authority-1"},
            authority_boundary_class="protected_runtime",
            authority_waiting_since="2026-04-06T02:00:01+00:00",
            authority_resume_target="group-a:team:runtime:teammate:1",
        )
        authority_request = ScopeExtensionRequest(
            request_id="auth-req-1",
            assignment_id="task-authority-1:assignment",
            worker_id="group-a:team:runtime:teammate:1",
            task_id="task-authority-1",
            requested_paths=("src/agent_orchestra/runtime/session_host.py",),
            reason="Need protected runtime authority.",
            evidence="verification touched protected runtime surface",
            retry_hint="Await leader/superleader authority decision.",
        )
        authority_entry = BlackboardEntry(
            entry_id="entry-authority-request-1",
            blackboard_id="group-a:blackboard:runtime",
            group_id="group-a",
            kind=BlackboardKind.TEAM,
            entry_kind=BlackboardEntryKind.PROPOSAL,
            author_id="group-a:team:runtime:teammate:1",
            lane_id="runtime",
            team_id="group-a:team:runtime",
            task_id="task-authority-1",
            summary="Teammate requested authority for protected runtime file.",
            payload={
                "event": "authority.request",
                "authority_request": authority_request.to_dict(),
            },
            created_at="2026-04-06T02:00:02+00:00",
        )
        authority_delivery = DeliveryState(
            delivery_id="obj-authority:lane:runtime",
            objective_id="obj-authority",
            kind=DeliveryStateKind.LANE,
            status=DeliveryStatus.WAITING_FOR_AUTHORITY,
            lane_id="runtime",
            team_id="group-a:team:runtime",
            summary="Lane is waiting for authority request decision.",
            metadata={
                "authority_waiting": True,
                "waiting_for_authority_task_ids": ["task-authority-1"],
            },
        )
        authority_session = AgentSession(
            session_id="group-a:team:runtime:teammate:1:resident",
            agent_id="group-a:team:runtime:teammate:1",
            role="teammate",
            objective_id="obj-authority",
            lane_id="runtime",
            team_id="group-a:team:runtime",
            metadata={
                "authority_request_id": "auth-req-1",
                "authority_waiting_task_id": "task-authority-1",
                "authority_waiting": True,
            },
        )

        await store.commit_authority_request(
            AuthorityRequestStoreCommit(
                task=authority_task,
                authority_request=authority_request,
                blackboard_entry=authority_entry,
                delivery_state=authority_delivery,
                agent_session=authority_session,
            )
        )
        authority_session.metadata["authority_waiting"] = False

        loaded_task = await store.get_task("task-authority-1")
        loaded_entries = await store.list_blackboard_entries("group-a:blackboard:runtime")
        loaded_delivery = await store.get_delivery_state("obj-authority:lane:runtime")
        loaded_session = await store.get_agent_session("group-a:team:runtime:teammate:1:resident")

        self.assertIs(loaded_task, authority_task)
        self.assertEqual(loaded_task.authority_request_id, "auth-req-1")
        self.assertEqual(loaded_task.status, TaskStatus.WAITING_FOR_AUTHORITY)
        self.assertEqual(loaded_entries, [authority_entry])
        self.assertIs(loaded_delivery, authority_delivery)
        self.assertEqual(loaded_delivery.status, DeliveryStatus.WAITING_FOR_AUTHORITY)
        self.assertIsNotNone(loaded_session)
        assert loaded_session is not None
        self.assertTrue(loaded_session.metadata["authority_waiting"])
        self.assertEqual(loaded_session.metadata["authority_request_id"], "auth-req-1")

    async def test_in_memory_store_commits_authority_decision_and_replacement_task(self) -> None:
        store = InMemoryOrchestrationStore()
        decided_task = TaskCard(
            task_id="task-authority-2",
            goal="Escalation decision for protected task.",
            lane="runtime",
            group_id="group-a",
            team_id="group-a:team:runtime",
            owner_id=None,
            status=TaskStatus.CANCELLED,
            authority_request_id=None,
            authority_decision_payload={"request_id": "auth-req-2", "decision": "reroute"},
            authority_boundary_class="global_contract",
            superseded_by_task_id="task-authority-2-reroute",
        )
        reroute_task = TaskCard(
            task_id="task-authority-2-reroute",
            goal="Follow-up task after authority reroute.",
            lane="runtime",
            group_id="group-a",
            team_id="group-a:team:runtime",
            status=TaskStatus.PENDING,
            derived_from="task-authority-2",
        )
        authority_decision = AuthorityDecision(
            request_id="auth-req-2",
            decision="reroute",
            actor_id="group-a:leader:runtime",
            scope_class="global_contract",
            reroute_task_id="task-authority-2-reroute",
            summary="Reroute authority request to a contract-owner slice.",
        )
        authority_entry = BlackboardEntry(
            entry_id="entry-authority-decision-2",
            blackboard_id="group-a:blackboard:leader",
            group_id="group-a",
            kind=BlackboardKind.LEADER_LANE,
            entry_kind=BlackboardEntryKind.DECISION,
            author_id="group-a:leader:runtime",
            lane_id="runtime",
            team_id="group-a:team:runtime",
            task_id="task-authority-2",
            summary="Authority request rerouted to replacement task.",
            payload={
                "event": "authority.decision",
                "authority_decision": authority_decision.to_dict(),
                "replacement_task_id": "task-authority-2-reroute",
            },
            created_at="2026-04-06T02:10:00+00:00",
        )
        authority_delivery = DeliveryState(
            delivery_id="obj-authority:lane:runtime",
            objective_id="obj-authority",
            kind=DeliveryStateKind.LANE,
            status=DeliveryStatus.RUNNING,
            lane_id="runtime",
            team_id="group-a:team:runtime",
            summary="Authority reroute committed.",
            pending_task_ids=("task-authority-2-reroute",),
            metadata={"authority_waiting": False},
        )
        authority_session = AgentSession(
            session_id="group-a:team:runtime:teammate:2:resident",
            agent_id="group-a:team:runtime:teammate:2",
            role="teammate",
            objective_id="obj-authority",
            lane_id="runtime",
            team_id="group-a:team:runtime",
            metadata={
                "authority_last_decision": "reroute",
                "authority_request_id": "auth-req-2",
                "authority_waiting": False,
            },
        )

        await store.commit_authority_decision(
            AuthorityDecisionStoreCommit(
                task=decided_task,
                authority_decision=authority_decision,
                blackboard_entry=authority_entry,
                delivery_state=authority_delivery,
                agent_session=authority_session,
                replacement_task=reroute_task,
            )
        )
        authority_session.metadata["authority_last_decision"] = "grant"

        loaded_task = await store.get_task("task-authority-2")
        loaded_replacement = await store.get_task("task-authority-2-reroute")
        loaded_entries = await store.list_blackboard_entries("group-a:blackboard:leader")
        loaded_delivery = await store.get_delivery_state("obj-authority:lane:runtime")
        loaded_session = await store.get_agent_session("group-a:team:runtime:teammate:2:resident")

        self.assertIs(loaded_task, decided_task)
        self.assertIs(loaded_replacement, reroute_task)
        self.assertEqual(loaded_entries, [authority_entry])
        self.assertIs(loaded_delivery, authority_delivery)
        self.assertEqual(loaded_delivery.pending_task_ids, ("task-authority-2-reroute",))
        self.assertIsNotNone(loaded_session)
        assert loaded_session is not None
        self.assertEqual(loaded_session.metadata["authority_last_decision"], "reroute")
        self.assertFalse(loaded_session.metadata["authority_waiting"])


if __name__ == "__main__":  # pragma: no cover
    import unittest

    unittest.main()
