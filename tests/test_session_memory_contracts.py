from __future__ import annotations

import sys
from pathlib import Path
from unittest import TestCase

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agent_orchestra.contracts.ids import (
    make_agent_turn_record_id,
    make_artifact_ref_id,
    make_memory_item_id,
    make_tool_invocation_id,
)
from agent_orchestra.contracts.session_continuity import (
    ConversationHead,
    ConversationHeadKind,
)
from agent_orchestra.contracts.session_memory import (
    AgentTurnActorRole,
    AgentTurnKind,
    AgentTurnRecord,
    AgentTurnStatus,
    ArtifactRef,
    ArtifactRefKind,
    ArtifactStorageKind,
    HydrationBundle,
    SessionMemoryItem,
    SessionMemoryKind,
    ToolInvocationKind,
    ToolInvocationRecord,
)


class SessionMemoryContractsTest(TestCase):
    def test_session_memory_id_helpers_use_expected_prefixes(self) -> None:
        self.assertTrue(make_agent_turn_record_id().startswith("turnrec_"))
        self.assertTrue(make_tool_invocation_id().startswith("toolinv_"))
        self.assertTrue(make_artifact_ref_id().startswith("artifactref_"))
        self.assertTrue(make_memory_item_id().startswith("memitem_"))

    def test_conversation_head_round_trip_preserves_contract_metadata(self) -> None:
        head = ConversationHead(
            conversation_head_id="convhead_1",
            work_session_id="worksession_1",
            runtime_generation_id="runtimegen_1",
            head_kind=ConversationHeadKind.LEADER_LANE,
            scope_id="lane-runtime",
            backend="codex_cli",
            model="gpt-5.2",
            provider="openai",
            last_response_id="resp_123",
            checkpoint_summary="Leader checkpoint",
            checkpoint_metadata={"turn": 4},
            source_agent_session_id="agent-session-1",
            source_worker_session_id="worker-session-1",
            updated_at="2026-04-11T12:00:00+00:00",
            checkpoint_id="checkpoint-1",
            prompt_contract_version="prompt-v2",
            toolset_hash="toolset-abc",
            contract_fingerprint="contract-xyz",
        )

        restored = ConversationHead.from_payload(head.to_dict())

        self.assertEqual(restored, head)
        self.assertEqual(restored.checkpoint_id, "checkpoint-1")
        self.assertEqual(restored.prompt_contract_version, "prompt-v2")
        self.assertEqual(restored.toolset_hash, "toolset-abc")
        self.assertEqual(restored.contract_fingerprint, "contract-xyz")

    def test_session_memory_contracts_round_trip(self) -> None:
        turn = AgentTurnRecord(
            turn_record_id="turnrec_1",
            work_session_id="worksession_1",
            runtime_generation_id="runtimegen_2",
            head_kind=ConversationHeadKind.WORKER,
            scope_id="worker-alpha",
            actor_role=AgentTurnActorRole.WORKER,
            assignment_id="assignment-1",
            turn_kind=AgentTurnKind.WORKER_RESULT,
            input_summary="Run verification on changed files.",
            output_summary="Verification passed and final report emitted.",
            response_id="resp_worker_1",
            status=AgentTurnStatus.COMPLETED,
            created_at="2026-04-11T12:10:00+00:00",
            metadata={"attempt": 1},
        )
        tool = ToolInvocationRecord(
            tool_invocation_id="toolinv_1",
            turn_record_id=turn.turn_record_id,
            work_session_id=turn.work_session_id,
            runtime_generation_id=turn.runtime_generation_id,
            tool_name="pytest",
            tool_kind=ToolInvocationKind.LOCAL_COMMAND,
            input_summary="pytest tests/test_session_memory_contracts.py -v",
            output_summary="1 passed",
            status="completed",
            started_at="2026-04-11T12:10:05+00:00",
            completed_at="2026-04-11T12:10:06+00:00",
            metadata={"returncode": 0},
        )
        artifact = ArtifactRef(
            artifact_ref_id="artifactref_1",
            turn_record_id=turn.turn_record_id,
            tool_invocation_id=tool.tool_invocation_id,
            work_session_id=turn.work_session_id,
            runtime_generation_id=turn.runtime_generation_id,
            artifact_kind=ArtifactRefKind.FINAL_REPORT,
            storage_kind=ArtifactStorageKind.INLINE_JSON,
            uri_or_path="worker-record:worker-alpha:final-report",
            content_hash="abc123",
            size_bytes=128,
            metadata={"terminal_status": "completed"},
        )
        memory = SessionMemoryItem(
            memory_item_id="memitem_1",
            work_session_id=turn.work_session_id,
            runtime_generation_id=turn.runtime_generation_id,
            head_kind=ConversationHeadKind.WORKER,
            scope_id="worker-alpha",
            memory_kind=SessionMemoryKind.HANDOFF,
            importance=8,
            summary="Verification completed cleanly; next resume can continue from report.",
            source_turn_record_ids=(turn.turn_record_id,),
            source_artifact_ref_ids=(artifact.artifact_ref_id,),
            created_at="2026-04-11T12:10:07+00:00",
            metadata={"fresh": True},
        )
        bundle = HydrationBundle(
            work_session_id=turn.work_session_id,
            runtime_generation_id=turn.runtime_generation_id,
            head_kind=ConversationHeadKind.WORKER,
            scope_id="worker-alpha",
            conversation_head_id="convhead_1",
            continuation_mode="warm_resume",
            last_response_id="resp_worker_1",
            checkpoint_summary="Worker checkpoint",
            recent_turns=(turn,),
            recent_tool_invocations=(tool,),
            artifact_refs=(artifact,),
            memory_items=(memory,),
            runtime_status_summary={"status": "detached"},
            delivery_state_summary={"status": "running"},
            mailbox_summary={"pending": 1},
            blackboard_summary={"entries": 2},
            task_surface_authority={"waiting_task_ids": ["task-1"]},
            shell_attach_summary={"mode": "warm_resumed"},
            invalidated_continuity_reasons=("backend changed",),
            bundle_created_at="2026-04-11T12:11:00+00:00",
            metadata={"coverage": "full"},
        )

        self.assertEqual(AgentTurnRecord.from_payload(turn.to_dict()), turn)
        self.assertEqual(ToolInvocationRecord.from_payload(tool.to_dict()), tool)
        self.assertEqual(ArtifactRef.from_payload(artifact.to_dict()), artifact)
        self.assertEqual(SessionMemoryItem.from_payload(memory.to_dict()), memory)
        restored_bundle = HydrationBundle.from_payload(bundle.to_dict())
        self.assertEqual(restored_bundle, bundle)
        self.assertEqual(restored_bundle.recent_turns[0].turn_kind, AgentTurnKind.WORKER_RESULT)
        self.assertEqual(restored_bundle.memory_items[0].memory_kind, SessionMemoryKind.HANDOFF)
