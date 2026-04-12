from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agent_orchestra.contracts.agent import AgentSession
from agent_orchestra.contracts.authority import AuthorityDecision, ScopeExtensionRequest
from agent_orchestra.contracts.blackboard import BlackboardEntry
from agent_orchestra.contracts.delivery import DeliveryDecision, DeliveryState, DeliveryStateKind, DeliveryStatus
from agent_orchestra.contracts.enums import BlackboardEntryKind, BlackboardKind, TaskStatus
from agent_orchestra.contracts.task import TaskCard
from agent_orchestra.storage.base import AuthorityDecisionStoreCommit, AuthorityRequestStoreCommit
from agent_orchestra.tools.mailbox import MailboxBridge, MailboxCursor, MailboxEnvelope, MailboxMessageKind
from agent_orchestra.tools.permission_protocol import PermissionBroker, PermissionDecision, PermissionRequest, StaticPermissionBroker


class DeliveryContractsTest(unittest.TestCase):
    def test_delivery_state_and_control_protocol_types_capture_constructor_values(self) -> None:
        state = DeliveryState(
            delivery_id="obj-1:lane:runtime",
            objective_id="obj-1",
            kind=DeliveryStateKind.LANE,
            status=DeliveryStatus.RUNNING,
            lane_id="runtime",
            team_id="group-a:team:runtime",
            iteration=2,
            summary="Waiting for the verification turn.",
            pending_task_ids=("task-pending",),
            active_task_ids=("task-active",),
            completed_task_ids=("task-done",),
            blocked_task_ids=(),
            latest_worker_ids=("leader:runtime",),
            mailbox_cursor="env-2",
            metadata={"reason": "verification"},
        )
        envelope = MailboxEnvelope(
            envelope_id="env-2",
            mailbox_id="group-a:leader:runtime",
            sender="group-a:team:runtime:teammate:1",
            recipient="leader:runtime",
            kind=MailboxMessageKind.TEAMMATE_RESULT,
            subject="Task completed",
            payload={"task_id": "task-done"},
        )
        cursor = MailboxCursor(recipient="leader:runtime", last_envelope_id="env-2")
        request = PermissionRequest(
            request_id="perm-1",
            requester="leader:runtime",
            action="execute_worker_assignment",
            rationale="Need to execute the next runtime task.",
            group_id="group-a",
            objective_id="obj-1",
            team_id="group-a:team:runtime",
            lane_id="runtime",
            task_id="task-done",
            metadata={"role": "leader"},
        )
        decision = PermissionDecision(
            approved=True,
            reviewer="policy-engine",
            reason="allowed",
            request_id="perm-1",
        )
        broker = StaticPermissionBroker(decision=decision)

        self.assertEqual(state.kind, DeliveryStateKind.LANE)
        self.assertEqual(state.status, DeliveryStatus.RUNNING)
        self.assertEqual(state.iteration, 2)
        self.assertEqual(envelope.kind, MailboxMessageKind.TEAMMATE_RESULT)
        self.assertEqual(cursor.last_envelope_id, "env-2")
        self.assertEqual(request.group_id, "group-a")
        self.assertEqual(decision.request_id, "perm-1")
        self.assertIs(broker.decision, decision)

    def test_delivery_and_control_abstract_interfaces_cannot_be_instantiated(self) -> None:
        with self.assertRaises(TypeError):
            MailboxBridge()

        with self.assertRaises(TypeError):
            PermissionBroker()

    def test_delivery_enums_expose_expected_terminal_values(self) -> None:
        self.assertEqual(DeliveryDecision.COMPLETE.value, "complete")
        self.assertEqual(DeliveryDecision.FAIL.value, "fail")
        self.assertEqual(DeliveryStatus.BLOCKED.value, "blocked")
        self.assertEqual(DeliveryStatus.WAITING_FOR_AUTHORITY.value, "waiting_for_authority")
        self.assertEqual(TaskStatus.WAITING_FOR_AUTHORITY.value, "waiting_for_authority")
        self.assertEqual(DeliveryStateKind.OBJECTIVE.value, "objective")

    def test_authority_extension_contract_types_capture_constructor_values(self) -> None:
        request = ScopeExtensionRequest(
            request_id="auth-req-1",
            assignment_id="task-1:assignment",
            worker_id="team-a:teammate:1",
            task_id="task-1",
            requested_paths=("src/agent_orchestra/self_hosting/bootstrap.py",),
            reason="Need to fix an out-of-scope import blocker.",
            evidence="unittest import failed",
            blocking_verification_command="python3 -m unittest tests.test_leader_loop -v",
            retry_hint="Grant bootstrap.py or reroute to an implementation slice.",
        )
        decision = AuthorityDecision(
            request_id="auth-req-1",
            decision="grant",
            actor_id="team-a:leader",
            scope_class="protected_runtime",
            granted_paths=("src/agent_orchestra/self_hosting/bootstrap.py",),
            reason="Approved for unblock.",
            resume_mode="direct_reactivation",
            summary="Leader granted bootstrap.py for blocker repair.",
        )

        self.assertEqual(request.request_id, "auth-req-1")
        self.assertEqual(
            request.requested_paths,
            ("src/agent_orchestra/self_hosting/bootstrap.py",),
        )
        self.assertEqual(decision.decision, "grant")
        self.assertEqual(decision.actor_id, "team-a:leader")
        self.assertEqual(decision.resume_mode, "direct_reactivation")
        self.assertEqual(
            decision.granted_paths,
            ("src/agent_orchestra/self_hosting/bootstrap.py",),
        )

    def test_authority_store_commit_contracts_keep_payload_and_session_aliases(self) -> None:
        task = TaskCard(
            task_id="task-auth-1",
            goal="Authority flow contract coverage.",
            lane="runtime",
            group_id="group-a",
            team_id="group-a:team:runtime",
            status=TaskStatus.WAITING_FOR_AUTHORITY,
            authority_request_id="auth-req-1",
            authority_request_payload={"request_id": "auth-req-1"},
        )
        request = ScopeExtensionRequest(
            request_id="auth-req-1",
            assignment_id="task-auth-1:assignment",
            worker_id="group-a:team:runtime:teammate:1",
            task_id="task-auth-1",
            requested_paths=("src/agent_orchestra/runtime/session_host.py",),
        )
        decision = AuthorityDecision(
            request_id="auth-req-1",
            decision="reroute",
            actor_id="group-a:leader:runtime",
            reroute_task_id="task-auth-1-reroute",
            summary="Reroute to protected-contract owner.",
        )
        replacement_task = TaskCard(
            task_id="task-auth-1-reroute",
            goal="Replacement authority owner task.",
            lane="runtime",
            group_id="group-a",
            team_id="group-a:team:runtime",
            status=TaskStatus.PENDING,
        )
        blackboard_entry = BlackboardEntry(
            entry_id="entry-auth-1",
            blackboard_id="group-a:blackboard:runtime",
            group_id="group-a",
            kind=BlackboardKind.TEAM,
            entry_kind=BlackboardEntryKind.PROPOSAL,
            author_id="group-a:team:runtime:teammate:1",
            lane_id="runtime",
            team_id="group-a:team:runtime",
            task_id="task-auth-1",
            summary="Authority request/decision contract test.",
            payload={"event": "authority.request"},
            created_at="2026-04-06T03:00:00+00:00",
        )
        session = AgentSession(
            session_id="group-a:team:runtime:teammate:1:resident",
            agent_id="group-a:team:runtime:teammate:1",
            role="teammate",
            metadata={"authority_request_id": "auth-req-1"},
        )

        request_commit = AuthorityRequestStoreCommit(
            task=task,
            authority_request=request,
            blackboard_entry=blackboard_entry,
            agent_session=session,
        )
        decision_commit = AuthorityDecisionStoreCommit(
            task=task,
            authority_decision=decision,
            blackboard_entry=blackboard_entry,
            agent_session=session,
            replacement_task=replacement_task,
        )

        self.assertEqual(request_commit.authority_request.request_id, "auth-req-1")
        self.assertEqual(request_commit.authority_request.task_id, "task-auth-1")
        self.assertIs(request_commit.session_snapshot, session)
        self.assertIs(request_commit.slot_session, session)

        self.assertEqual(decision_commit.authority_decision.decision, "reroute")
        self.assertEqual(decision_commit.authority_decision.reroute_task_id, "task-auth-1-reroute")
        self.assertIs(decision_commit.replacement_task, replacement_task)
        self.assertIs(decision_commit.session_snapshot, session)
        self.assertIs(decision_commit.slot_session, session)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
