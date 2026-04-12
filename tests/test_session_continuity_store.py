from __future__ import annotations

import sys
from pathlib import Path
from unittest import IsolatedAsyncioTestCase

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agent_orchestra.contracts.session_continuity import (
    ConversationHead,
    ConversationHeadKind,
    ResidentTeamShell,
    ResidentTeamShellStatus,
    RuntimeGeneration,
    RuntimeGenerationContinuityMode,
    RuntimeGenerationStatus,
    SessionEvent,
    WorkSession,
    WorkSessionMessage,
)
from agent_orchestra.contracts.session_memory import (
    AgentTurnActorRole,
    AgentTurnKind,
    AgentTurnRecord,
    AgentTurnStatus,
    ArtifactRef,
    ArtifactRefKind,
    ArtifactStorageKind,
    SessionMemoryItem,
    SessionMemoryKind,
    ToolInvocationKind,
    ToolInvocationRecord,
)
from agent_orchestra.storage.base import SessionTransactionStoreCommit
from agent_orchestra.storage.in_memory import InMemoryOrchestrationStore


class SessionContinuityStoreTest(IsolatedAsyncioTestCase):
    async def test_in_memory_store_commits_session_transaction(self) -> None:
        store = InMemoryOrchestrationStore()
        work_session = WorkSession(
            work_session_id="worksession_txn",
            group_id="group-a",
            root_objective_id="obj-1",
            title="Transactional session",
            status="open",
            created_at="2026-04-12T09:00:00+00:00",
            updated_at="2026-04-12T09:00:00+00:00",
            current_runtime_generation_id="runtimegen_txn",
        )
        generation = RuntimeGeneration(
            runtime_generation_id="runtimegen_txn",
            work_session_id="worksession_txn",
            generation_index=0,
            status=RuntimeGenerationStatus.BOOTING,
            continuity_mode=RuntimeGenerationContinuityMode.FRESH,
            created_at="2026-04-12T09:00:00+00:00",
            group_id="group-a",
            objective_id="obj-1",
        )
        message = WorkSessionMessage(
            message_id="wsmsg_txn",
            work_session_id="worksession_txn",
            runtime_generation_id="runtimegen_txn",
            role="system",
            content="Transactional session created.",
            content_kind="summary",
            created_at="2026-04-12T09:00:01+00:00",
        )
        head = ConversationHead(
            conversation_head_id="convhead_txn",
            work_session_id="worksession_txn",
            runtime_generation_id="runtimegen_txn",
            head_kind=ConversationHeadKind.LEADER_LANE,
            scope_id="runtime",
            backend="codex_cli",
            model="gpt-5",
            provider="openai",
            last_response_id="resp_txn",
            checkpoint_summary="Checkpoint",
            updated_at="2026-04-12T09:00:02+00:00",
        )
        event = SessionEvent(
            session_event_id="sevt_txn",
            work_session_id="worksession_txn",
            runtime_generation_id="runtimegen_txn",
            event_kind="new_session_created",
            payload={"objective_id": "obj-1"},
            created_at="2026-04-12T09:00:03+00:00",
        )
        turn_record = AgentTurnRecord(
            turn_record_id="turnrec_txn",
            work_session_id="worksession_txn",
            runtime_generation_id="runtimegen_txn",
            head_kind=ConversationHeadKind.LEADER_LANE,
            scope_id="runtime",
            actor_role=AgentTurnActorRole.WORKER,
            assignment_id="assignment_txn",
            turn_kind=AgentTurnKind.WORKER_RESULT,
            input_summary="Run worker",
            output_summary="Worker completed",
            response_id="resp_txn",
            status=AgentTurnStatus.COMPLETED,
            created_at="2026-04-12T09:00:04+00:00",
        )
        tool_record = ToolInvocationRecord(
            tool_invocation_id="toolinv_txn",
            turn_record_id="turnrec_txn",
            work_session_id="worksession_txn",
            runtime_generation_id="runtimegen_txn",
            tool_name="pytest",
            tool_kind=ToolInvocationKind.LOCAL_COMMAND,
            input_summary="pytest -q",
            output_summary="passed",
            status="completed",
            started_at="2026-04-12T09:00:05+00:00",
            completed_at="2026-04-12T09:00:06+00:00",
        )
        artifact_ref = ArtifactRef(
            artifact_ref_id="artifactref_txn",
            turn_record_id="turnrec_txn",
            tool_invocation_id="toolinv_txn",
            work_session_id="worksession_txn",
            runtime_generation_id="runtimegen_txn",
            artifact_kind=ArtifactRefKind.FINAL_REPORT,
            storage_kind=ArtifactStorageKind.INLINE_JSON,
            uri_or_path="worker-record:worker-alpha:final-report",
            content_hash="hash-1",
            size_bytes=64,
        )
        memory_item = SessionMemoryItem(
            memory_item_id="memitem_txn",
            work_session_id="worksession_txn",
            runtime_generation_id="runtimegen_txn",
            head_kind=ConversationHeadKind.LEADER_LANE,
            scope_id="runtime",
            memory_kind=SessionMemoryKind.HANDOFF,
            importance=7,
            summary="Worker can resume from final report.",
            source_turn_record_ids=("turnrec_txn",),
            source_artifact_ref_ids=("artifactref_txn",),
            created_at="2026-04-12T09:00:07+00:00",
        )

        await store.commit_session_transaction(
            SessionTransactionStoreCommit(
                work_sessions=(work_session,),
                runtime_generations=(generation,),
                work_session_messages=(message,),
                conversation_heads=(head,),
                session_events=(event,),
                turn_records=(turn_record,),
                tool_invocation_records=(tool_record,),
                artifact_refs=(artifact_ref,),
                session_memory_items=(memory_item,),
            )
        )

        self.assertEqual(await store.get_work_session("worksession_txn"), work_session)
        self.assertEqual(await store.get_runtime_generation("runtimegen_txn"), generation)
        self.assertEqual(
            await store.list_work_session_messages("worksession_txn"),
            [message],
        )
        self.assertEqual(await store.get_conversation_head("convhead_txn"), head)
        self.assertEqual(
            await store.list_session_events("worksession_txn"),
            [event],
        )
        self.assertEqual(await store.list_turn_records("worksession_txn"), [turn_record])
        self.assertEqual(
            await store.list_tool_invocation_records("worksession_txn"),
            [tool_record],
        )
        self.assertEqual(await store.list_artifact_refs("worksession_txn"), [artifact_ref])
        self.assertEqual(
            await store.list_session_memory_items("worksession_txn"),
            [memory_item],
        )

    async def test_in_memory_store_round_trips_continuity_entities(self) -> None:
        store = InMemoryOrchestrationStore()
        work_session = WorkSession(
            work_session_id="worksession_1",
            group_id="group-a",
            root_objective_id="obj-1",
            title="Session continuity",
            status="open",
            created_at="2026-04-10T09:00:00+00:00",
            updated_at="2026-04-10T09:00:00+00:00",
            current_runtime_generation_id="runtimegen_2",
            metadata={"entry_mode": "continue"},
        )
        generation_0 = RuntimeGeneration(
            runtime_generation_id="runtimegen_1",
            work_session_id="worksession_1",
            generation_index=0,
            status=RuntimeGenerationStatus.CLOSED,
            continuity_mode=RuntimeGenerationContinuityMode.FRESH,
            created_at="2026-04-10T09:00:00+00:00",
            closed_at="2026-04-10T09:05:00+00:00",
            source_runtime_generation_id=None,
            group_id="group-a",
            objective_id="obj-1",
            metadata={"reason": "first run"},
        )
        generation_1 = RuntimeGeneration(
            runtime_generation_id="runtimegen_2",
            work_session_id="worksession_1",
            generation_index=1,
            status=RuntimeGenerationStatus.ACTIVE,
            continuity_mode=RuntimeGenerationContinuityMode.WARM_RESUME,
            created_at="2026-04-10T09:10:00+00:00",
            closed_at=None,
            source_runtime_generation_id="runtimegen_1",
            group_id="group-a",
            objective_id="obj-1",
            metadata={"reason": "warm_resume"},
        )
        message = WorkSessionMessage(
            message_id="wsmsg_1",
            work_session_id="worksession_1",
            runtime_generation_id="runtimegen_2",
            role="assistant",
            scope_kind="session",
            scope_id=None,
            content="Warm resume generation created.",
            content_kind="summary",
            created_at="2026-04-10T09:11:00+00:00",
            metadata={"marker": "warm_resume_started"},
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
            last_response_id="resp_9001",
            checkpoint_summary="Leader lane checkpoint",
            checkpoint_metadata={"turn": 3},
            source_agent_session_id="agent-session-1",
            source_worker_session_id="worker-session-1",
            updated_at="2026-04-10T09:12:00+00:00",
            invalidated_at=None,
            invalidation_reason=None,
        )
        event = SessionEvent(
            session_event_id="sevt_1",
            work_session_id="worksession_1",
            runtime_generation_id="runtimegen_2",
            event_kind="conversation_head_updated",
            payload={"conversation_head_id": "convhead_1"},
            created_at="2026-04-10T09:12:30+00:00",
        )

        await store.save_work_session(work_session)
        await store.save_runtime_generation(generation_0)
        await store.save_runtime_generation(generation_1)
        await store.append_work_session_message(message)
        await store.save_conversation_head(head)
        await store.append_session_event(event)

        loaded_session = await store.get_work_session("worksession_1")
        loaded_generations = await store.list_runtime_generations("worksession_1")
        loaded_message = await store.list_work_session_messages("worksession_1")
        loaded_head = await store.get_conversation_head("convhead_1")
        loaded_heads = await store.list_conversation_heads("worksession_1")
        loaded_events = await store.list_session_events("worksession_1")
        latest_resumable = await store.find_latest_resumable_runtime_generation("worksession_1")

        self.assertEqual(loaded_session, work_session)
        self.assertEqual(
            [generation.runtime_generation_id for generation in loaded_generations],
            ["runtimegen_1", "runtimegen_2"],
        )
        self.assertEqual(len(loaded_message), 1)
        self.assertEqual(loaded_message[0], message)
        self.assertEqual(loaded_head, head)
        self.assertEqual(loaded_heads, [head])
        self.assertEqual(loaded_events, [event])
        self.assertEqual(latest_resumable, generation_1)

    async def test_in_memory_store_returns_none_for_non_resumable_generations(self) -> None:
        store = InMemoryOrchestrationStore()
        await store.save_runtime_generation(
            RuntimeGeneration(
                runtime_generation_id="runtimegen_1",
                work_session_id="worksession_1",
                generation_index=0,
                status=RuntimeGenerationStatus.CLOSED,
                continuity_mode=RuntimeGenerationContinuityMode.FRESH,
                created_at="2026-04-10T10:00:00+00:00",
                group_id="group-a",
                objective_id="obj-1",
            )
        )
        await store.save_runtime_generation(
            RuntimeGeneration(
                runtime_generation_id="runtimegen_2",
                work_session_id="worksession_1",
                generation_index=1,
                status=RuntimeGenerationStatus.FAILED,
                continuity_mode=RuntimeGenerationContinuityMode.WARM_RESUME,
                created_at="2026-04-10T10:05:00+00:00",
                source_runtime_generation_id="runtimegen_1",
                group_id="group-a",
                objective_id="obj-1",
            )
        )

        latest = await store.find_latest_resumable_runtime_generation("worksession_1")

        self.assertIsNone(latest)

    async def test_in_memory_store_persists_and_queries_resident_team_shells(self) -> None:
        store = InMemoryOrchestrationStore()
        shell_1 = ResidentTeamShell(
            resident_team_shell_id="shell_001",
            work_session_id="worksession_1",
            group_id="group-a",
            objective_id="obj-1",
            team_id="team-runtime",
            lane_id="runtime",
            runtime_generation_id="runtimegen_1",
            status=ResidentTeamShellStatus.IDLE,
            leader_slot_session_id="leader-session-1",
            teammate_slot_session_ids=["tm-session-1"],
            attach_state={"mode": "attached"},
            created_at="2026-04-11T08:00:00+00:00",
            updated_at="2026-04-11T08:01:00+00:00",
            last_progress_at="2026-04-11T08:01:00+00:00",
            metadata={"phase": "boot"},
        )
        shell_2 = ResidentTeamShell(
            resident_team_shell_id="shell_002",
            work_session_id="worksession_1",
            group_id="group-a",
            objective_id="obj-1",
            team_id="team-runtime",
            lane_id="runtime",
            runtime_generation_id="runtimegen_2",
            status=ResidentTeamShellStatus.ATTACHED,
            leader_slot_session_id="leader-session-1",
            teammate_slot_session_ids=["tm-session-1", "tm-session-2"],
            attach_state={"mode": "woken"},
            created_at="2026-04-11T09:00:00+00:00",
            updated_at="2026-04-11T09:02:00+00:00",
            last_progress_at="2026-04-11T09:02:00+00:00",
            metadata={"phase": "steady"},
        )
        shell_3 = ResidentTeamShell(
            resident_team_shell_id="shell_900",
            work_session_id="worksession_2",
            group_id="group-a",
            objective_id="obj-2",
            team_id="team-runtime",
            lane_id="runtime",
            runtime_generation_id="runtimegen_9",
            status=ResidentTeamShellStatus.RECOVERING,
            created_at="2026-04-11T10:00:00+00:00",
            updated_at="2026-04-11T10:01:00+00:00",
            last_progress_at="2026-04-11T10:01:00+00:00",
        )

        await store.save_resident_team_shell(shell_2)
        await store.save_resident_team_shell(shell_1)
        await store.save_resident_team_shell(shell_3)

        loaded_shell_1 = await store.get_resident_team_shell("shell_001")
        listed_shells = await store.list_resident_team_shells("worksession_1")
        latest_shell = await store.find_latest_resident_team_shell("worksession_1")

        self.assertEqual(loaded_shell_1, shell_1)
        self.assertEqual(
            [shell.resident_team_shell_id for shell in listed_shells],
            ["shell_001", "shell_002"],
        )
        self.assertEqual(latest_shell, shell_2)

    async def test_in_memory_store_prefers_latest_resident_shell_by_progress_and_update_time(self) -> None:
        store = InMemoryOrchestrationStore()
        shell_old_created_recent_progress = ResidentTeamShell(
            resident_team_shell_id="shell_001",
            work_session_id="worksession_1",
            group_id="group-a",
            objective_id="obj-1",
            team_id="team-runtime",
            lane_id="runtime",
            runtime_generation_id="runtimegen_1",
            status=ResidentTeamShellStatus.IDLE,
            created_at="2026-04-11T08:00:00+00:00",
            updated_at="2026-04-11T12:05:00+00:00",
            last_progress_at="2026-04-11T12:10:00+00:00",
        )
        shell_newer_created_stale_progress = ResidentTeamShell(
            resident_team_shell_id="shell_002",
            work_session_id="worksession_1",
            group_id="group-a",
            objective_id="obj-1",
            team_id="team-runtime",
            lane_id="runtime",
            runtime_generation_id="runtimegen_2",
            status=ResidentTeamShellStatus.ATTACHED,
            created_at="2026-04-11T09:00:00+00:00",
            updated_at="2026-04-11T09:10:00+00:00",
            last_progress_at="2026-04-11T09:10:00+00:00",
        )

        await store.save_resident_team_shell(shell_old_created_recent_progress)
        await store.save_resident_team_shell(shell_newer_created_stale_progress)

        latest = await store.find_latest_resident_team_shell("worksession_1")

        self.assertIsNotNone(latest)
        assert latest is not None
        self.assertEqual(latest.resident_team_shell_id, "shell_001")

    async def test_in_memory_store_preserves_shell_created_at_on_update(self) -> None:
        store = InMemoryOrchestrationStore()
        original = ResidentTeamShell(
            resident_team_shell_id="shell_001",
            work_session_id="worksession_1",
            group_id="group-a",
            objective_id="obj-1",
            team_id="team-runtime",
            lane_id="runtime",
            runtime_generation_id="runtimegen_1",
            status=ResidentTeamShellStatus.IDLE,
            created_at="2026-04-11T08:00:00+00:00",
            updated_at="2026-04-11T08:05:00+00:00",
            last_progress_at="2026-04-11T08:05:00+00:00",
        )
        updated = ResidentTeamShell(
            resident_team_shell_id="shell_001",
            work_session_id="worksession_1",
            group_id="group-a",
            objective_id="obj-1",
            team_id="team-runtime",
            lane_id="runtime",
            runtime_generation_id="runtimegen_1",
            status=ResidentTeamShellStatus.ATTACHED,
            created_at="2026-04-11T12:00:00+00:00",
            updated_at="2026-04-11T12:10:00+00:00",
            last_progress_at="2026-04-11T12:10:00+00:00",
            metadata={"phase": "steady"},
        )

        await store.save_resident_team_shell(original)
        await store.save_resident_team_shell(updated)

        loaded = await store.get_resident_team_shell("shell_001")

        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(loaded.created_at, "2026-04-11T08:00:00+00:00")
        self.assertEqual(loaded.updated_at, "2026-04-11T12:10:00+00:00")
        self.assertEqual(loaded.last_progress_at, "2026-04-11T12:10:00+00:00")
