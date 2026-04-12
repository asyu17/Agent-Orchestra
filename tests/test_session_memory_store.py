from __future__ import annotations

import sys
from pathlib import Path
from unittest import IsolatedAsyncioTestCase

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agent_orchestra.contracts.session_continuity import ConversationHeadKind
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
from agent_orchestra.storage.in_memory import InMemoryOrchestrationStore


class SessionMemoryStoreTest(IsolatedAsyncioTestCase):
    async def test_in_memory_store_round_trips_session_memory_entities(self) -> None:
        store = InMemoryOrchestrationStore()
        turn = AgentTurnRecord(
            turn_record_id="turnrec_1",
            work_session_id="worksession_1",
            runtime_generation_id="runtimegen_1",
            head_kind=ConversationHeadKind.WORKER,
            scope_id="worker-alpha",
            actor_role=AgentTurnActorRole.WORKER,
            assignment_id="assignment-1",
            turn_kind=AgentTurnKind.WORKER_RESULT,
            input_summary="Run worker",
            output_summary="Worker completed",
            response_id="resp_1",
            status=AgentTurnStatus.COMPLETED,
            created_at="2026-04-11T12:00:00+00:00",
        )
        tool = ToolInvocationRecord(
            tool_invocation_id="toolinv_1",
            turn_record_id="turnrec_1",
            work_session_id="worksession_1",
            runtime_generation_id="runtimegen_1",
            tool_name="pytest",
            tool_kind=ToolInvocationKind.LOCAL_COMMAND,
            input_summary="pytest -q",
            output_summary="passed",
            status="completed",
            started_at="2026-04-11T12:00:01+00:00",
            completed_at="2026-04-11T12:00:02+00:00",
        )
        artifact = ArtifactRef(
            artifact_ref_id="artifactref_1",
            turn_record_id="turnrec_1",
            tool_invocation_id="toolinv_1",
            work_session_id="worksession_1",
            runtime_generation_id="runtimegen_1",
            artifact_kind=ArtifactRefKind.FINAL_REPORT,
            storage_kind=ArtifactStorageKind.INLINE_JSON,
            uri_or_path="worker-record:worker-alpha:final-report",
            content_hash="hash-1",
            size_bytes=64,
        )
        memory = SessionMemoryItem(
            memory_item_id="memitem_1",
            work_session_id="worksession_1",
            runtime_generation_id="runtimegen_1",
            head_kind=ConversationHeadKind.WORKER,
            scope_id="worker-alpha",
            memory_kind=SessionMemoryKind.HANDOFF,
            importance=7,
            summary="Worker can resume from final report.",
            source_turn_record_ids=("turnrec_1",),
            source_artifact_ref_ids=("artifactref_1",),
            created_at="2026-04-11T12:00:03+00:00",
        )

        await store.append_turn_record(turn)
        await store.append_tool_invocation_record(tool)
        await store.save_artifact_ref(artifact)
        await store.save_session_memory_item(memory)

        loaded_turns = await store.list_turn_records(
            "worksession_1",
            runtime_generation_id="runtimegen_1",
            head_kind=ConversationHeadKind.WORKER,
            scope_id="worker-alpha",
        )
        loaded_tools = await store.list_tool_invocation_records(
            "worksession_1",
            runtime_generation_id="runtimegen_1",
            turn_record_id="turnrec_1",
        )
        loaded_artifacts = await store.list_artifact_refs(
            "worksession_1",
            runtime_generation_id="runtimegen_1",
            turn_record_id="turnrec_1",
        )
        loaded_memory = await store.list_session_memory_items(
            "worksession_1",
            runtime_generation_id="runtimegen_1",
            head_kind=ConversationHeadKind.WORKER,
            scope_id="worker-alpha",
        )

        self.assertEqual(loaded_turns, [turn])
        self.assertEqual(loaded_tools, [tool])
        self.assertEqual(loaded_artifacts, [artifact])
        self.assertEqual(loaded_memory, [memory])

    async def test_in_memory_store_filters_archived_memory_items_by_default(self) -> None:
        store = InMemoryOrchestrationStore()
        active = SessionMemoryItem(
            memory_item_id="memitem_active",
            work_session_id="worksession_1",
            runtime_generation_id="runtimegen_1",
            head_kind=ConversationHeadKind.LEADER_LANE,
            scope_id="lane-runtime",
            memory_kind=SessionMemoryKind.OPEN_LOOP,
            importance=9,
            summary="Need final review.",
            created_at="2026-04-11T12:00:00+00:00",
        )
        archived = SessionMemoryItem(
            memory_item_id="memitem_archived",
            work_session_id="worksession_1",
            runtime_generation_id="runtimegen_1",
            head_kind=ConversationHeadKind.LEADER_LANE,
            scope_id="lane-runtime",
            memory_kind=SessionMemoryKind.OPEN_LOOP,
            importance=2,
            summary="Old loop.",
            created_at="2026-04-11T11:00:00+00:00",
            archived_at="2026-04-11T12:01:00+00:00",
        )

        await store.save_session_memory_item(archived)
        await store.save_session_memory_item(active)

        visible = await store.list_session_memory_items(
            "worksession_1",
            runtime_generation_id="runtimegen_1",
            head_kind=ConversationHeadKind.LEADER_LANE,
            scope_id="lane-runtime",
        )
        including_archived = await store.list_session_memory_items(
            "worksession_1",
            runtime_generation_id="runtimegen_1",
            head_kind=ConversationHeadKind.LEADER_LANE,
            scope_id="lane-runtime",
            include_archived=True,
        )

        self.assertEqual(visible, [active])
        self.assertEqual(including_archived, [archived, active])
