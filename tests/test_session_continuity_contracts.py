from __future__ import annotations

import sys
from pathlib import Path
from unittest import TestCase

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agent_orchestra.contracts.ids import (
    make_conversation_head_id,
    make_runtime_generation_id,
    make_resident_team_shell_id,
    make_session_event_id,
    make_work_session_id,
    make_work_session_message_id,
)
from agent_orchestra.contracts.session_continuity import (
    ContinuationBundle,
    ConversationHead,
    ConversationHeadKind,
    ResumeGateDecision,
    ResumeGateMode,
    RuntimeGeneration,
    RuntimeGenerationContinuityMode,
    RuntimeGenerationStatus,
    SessionEvent,
    WorkSession,
    WorkSessionMessage,
    ResidentTeamShell,
    ResidentTeamShellStatus,
    ShellAttachDecision,
    ShellAttachDecisionMode,
)


class SessionContinuityContractsTest(TestCase):
    def test_id_helpers_use_expected_prefixes(self) -> None:
        self.assertTrue(make_work_session_id().startswith("worksession_"))
        self.assertTrue(make_runtime_generation_id().startswith("runtimegen_"))
        self.assertTrue(make_work_session_message_id().startswith("wsmsg_"))
        self.assertTrue(make_conversation_head_id().startswith("convhead_"))
        self.assertTrue(make_session_event_id().startswith("sevt_"))
        self.assertTrue(make_resident_team_shell_id().startswith("residentteamshell_"))

    def test_runtime_generation_origin_values_exclude_exact_wake(self) -> None:
        self.assertEqual(
            {mode.value for mode in RuntimeGenerationContinuityMode},
            {"fresh", "warm_resume", "fork_seed"},
        )

    def test_resident_team_shell_status_enum_values(self) -> None:
        self.assertEqual(
            {status.value for status in ResidentTeamShellStatus},
            {
                "booting",
                "attached",
                "idle",
                "waiting_for_mailbox",
                "waiting_for_subordinates",
                "quiescent",
                "recovering",
                "failed",
                "closed",
            },
        )

    def test_work_session_round_trip_uses_to_dict_and_from_payload(self) -> None:
        session = WorkSession(
            work_session_id="worksession_123",
            group_id="group-a",
            root_objective_id="obj-1",
            title="Runtime continuity",
            status="open",
            created_at="2026-04-10T08:00:00+00:00",
            updated_at="2026-04-10T08:05:00+00:00",
            current_runtime_generation_id="runtimegen_1",
            parent_work_session_id=None,
            fork_origin_work_session_id=None,
            metadata={"source": "new_session"},
        )

        restored = WorkSession.from_payload(session.to_dict())

        self.assertEqual(restored, session)
        self.assertEqual(restored.metadata["source"], "new_session")

    def test_runtime_generation_from_payload_defaults_invalid_origin_to_fresh(self) -> None:
        restored = RuntimeGeneration.from_payload(
            {
                "runtime_generation_id": "runtimegen_1",
                "work_session_id": "worksession_1",
                "generation_index": 0,
                "status": "active",
                "continuity_mode": "exact_wake",
                "created_at": "2026-04-10T08:00:00+00:00",
                "group_id": "group-a",
                "objective_id": "obj-1",
            }
        )

        self.assertEqual(restored.continuity_mode, RuntimeGenerationContinuityMode.FRESH)
        self.assertEqual(restored.status, RuntimeGenerationStatus.ACTIVE)

    def test_conversation_head_and_message_round_trip(self) -> None:
        message = WorkSessionMessage(
            message_id="wsmsg_1",
            work_session_id="worksession_1",
            runtime_generation_id="runtimegen_2",
            role="assistant",
            scope_kind="leader",
            scope_id="runtime",
            content="Continue from prior leader checkpoint.",
            content_kind="text",
            created_at="2026-04-10T08:20:00+00:00",
            metadata={"marker": "continuity"},
        )
        head = ConversationHead(
            conversation_head_id="convhead_1",
            work_session_id="worksession_1",
            runtime_generation_id="runtimegen_2",
            head_kind=ConversationHeadKind.LEADER_LANE,
            scope_id="runtime",
            backend="codex_cli",
            model="gpt-5",
            provider="openai",
            last_response_id="resp_123",
            checkpoint_summary="Leader finished warm resume checkpoint.",
            checkpoint_metadata={"turn_index": 4},
            source_agent_session_id="session-agent-1",
            source_worker_session_id="session-worker-1",
            updated_at="2026-04-10T08:25:00+00:00",
            invalidated_at=None,
            invalidation_reason=None,
        )

        self.assertEqual(WorkSessionMessage.from_payload(message.to_dict()), message)
        restored_head = ConversationHead.from_payload(head.to_dict())
        self.assertEqual(restored_head, head)
        self.assertEqual(restored_head.last_response_id, "resp_123")

    def test_session_event_resume_gate_and_bundle_round_trip(self) -> None:
        event = SessionEvent(
            session_event_id="sevt_1",
            work_session_id="worksession_1",
            runtime_generation_id="runtimegen_2",
            event_kind="resume_gate_passed",
            payload={"decision": "exact_wake"},
            created_at="2026-04-10T08:30:00+00:00",
        )
        gate = ResumeGateDecision(
            mode=ResumeGateMode.EXACT_WAKE,
            reason="Runtime still reclaimable.",
            target_work_session_id="worksession_1",
            target_runtime_generation_id="runtimegen_2",
            requires_user_confirmation=False,
            metadata={"source": "resident_projection"},
        )
        bundle = ContinuationBundle(
            work_session_id="worksession_1",
            runtime_generation_id="runtimegen_2",
            head_kind=ConversationHeadKind.SUPERLEADER,
            scope_id="superleader",
            checkpoint_summary="Resume from lane checkpoint.",
            last_response_id="resp_123",
            runtime_status_summary={"status": "active"},
            task_surface_authority={"waiting_task_ids": ["task-1"]},
            delivery_state_summary={"active_task_ids": ["task-1"]},
            mailbox_summary={"pending": 1},
            blackboard_summary={"entries": 8},
            metadata={"mode": "exact_wake"},
        )

        self.assertEqual(SessionEvent.from_payload(event.to_dict()), event)
        self.assertEqual(ResumeGateDecision.from_payload(gate.to_dict()), gate)
        self.assertEqual(ContinuationBundle.from_payload(bundle.to_dict()), bundle)

    def test_resident_team_shell_round_trip(self) -> None:
        shell = ResidentTeamShell(
            resident_team_shell_id="residentteamshell_1",
            work_session_id="worksession_1",
            group_id="group-a",
            objective_id="obj-1",
            team_id="team-alpha",
            lane_id="lane-1",
            runtime_generation_id="runtimegen_1",
            status=ResidentTeamShellStatus.WAITING_FOR_MAILBOX,
            leader_slot_session_id="leader-slot-1",
            teammate_slot_session_ids=["slot-a", "slot-b"],
            attach_state={"status": "waiting"},
            created_at="2026-04-10T09:00:00+00:00",
            updated_at="2026-04-10T09:05:00+00:00",
            last_progress_at="2026-04-10T09:04:00+00:00",
            metadata={"shell": "main"},
        )

        restored = ResidentTeamShell.from_payload(shell.to_dict())

        self.assertEqual(restored, shell)
        self.assertEqual(restored.status, ResidentTeamShellStatus.WAITING_FOR_MAILBOX)

    def test_shell_attach_decision_round_trip(self) -> None:
        decision = ShellAttachDecision(
            mode=ShellAttachDecisionMode.WOKEN,
            reason="Shell ready for attach.",
            target_shell_id="residentteamshell_1",
            target_work_session_id="worksession_1",
            target_runtime_generation_id="runtimegen_1",
            metadata={"via": "attach"},
        )

        restored_decision = ShellAttachDecision.from_payload(decision.to_dict())

        self.assertEqual(restored_decision, decision)
        self.assertEqual(restored_decision.mode, ShellAttachDecisionMode.WOKEN)
