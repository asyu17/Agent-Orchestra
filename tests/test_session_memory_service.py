from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from unittest import IsolatedAsyncioTestCase

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agent_orchestra.contracts.execution import WorkerAssignment, WorkerHandle, WorkerRecord
from agent_orchestra.contracts.enums import WorkerStatus
from agent_orchestra.contracts.session_continuity import ConversationHead, ConversationHeadKind
from agent_orchestra.contracts.session_memory import (
    ArtifactRef,
    ArtifactRefKind,
    ArtifactStorageKind,
    SessionMemoryKind,
    ToolInvocationRecord,
)
from agent_orchestra.runtime.session_memory import SessionMemoryService
from agent_orchestra.storage.in_memory import InMemoryOrchestrationStore


class SessionMemoryServiceTest(IsolatedAsyncioTestCase):
    async def test_record_worker_turn_persists_turn_tool_artifact_and_memory_records(self) -> None:
        store = InMemoryOrchestrationStore()
        service = SessionMemoryService(store=store)
        assignment = WorkerAssignment(
            assignment_id="assignment-1",
            worker_id="worker-alpha",
            group_id="group-a",
            task_id="task-1",
            role="worker",
            backend="subprocess",
            instructions="Complete the task.",
            input_text="Apply the planned changes.",
            metadata={"provider": "openai", "model": "gpt-5.2"},
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            protocol_state = Path(temp_dir) / "protocol-state.json"
            protocol_state.write_text(json.dumps({"phase": "completed"}), encoding="utf-8")
            record = WorkerRecord(
                worker_id="worker-alpha",
                assignment_id="assignment-1",
                backend="subprocess",
                role="worker",
                status=WorkerStatus.COMPLETED,
                handle=WorkerHandle(
                    worker_id="worker-alpha",
                    role="worker",
                    backend="subprocess",
                    metadata={"protocol_state_file": str(protocol_state)},
                ),
                output_text="Patched files and verified.",
                response_id="resp-worker-1",
                metadata={
                    "verification_results": [
                        {
                            "command": "pytest -q",
                            "returncode": 0,
                            "stdout": "1 passed",
                            "stderr": "",
                        }
                    ],
                    "protocol_events": [
                        {
                            "event_id": "evt-1",
                            "assignment_id": "assignment-1",
                            "worker_id": "worker-alpha",
                            "status": "completed",
                            "phase": "verify",
                            "kind": "checkpoint",
                        }
                    ],
                    "final_report": {
                        "assignment_id": "assignment-1",
                        "worker_id": "worker-alpha",
                        "terminal_status": "completed",
                        "summary": "Patched files and verified.",
                        "artifact_refs": ["reports/final.json"],
                        "verification_results": [
                            {
                                "command": "pytest -q",
                                "returncode": 0,
                            }
                        ],
                    },
                },
            )

            await service.record_worker_turn(
                work_session_id="worksession_1",
                runtime_generation_id="runtimegen_1",
                assignment=assignment,
                record=record,
            )

        turn_records = await store.list_turn_records("worksession_1")
        tool_records = await store.list_tool_invocation_records("worksession_1")
        artifact_refs = await store.list_artifact_refs("worksession_1")
        memory_items = await store.list_session_memory_items("worksession_1")

        self.assertEqual(len(turn_records), 1)
        self.assertEqual(turn_records[0].response_id, "resp-worker-1")
        self.assertEqual(len(tool_records), 1)
        self.assertEqual(tool_records[0].tool_name, "pytest")
        self.assertEqual(
            {artifact.artifact_kind.value for artifact in artifact_refs},
            {"final_report", "protocol_events", "protocol_state", "generated_file"},
        )
        self.assertEqual(len(memory_items), 1)
        self.assertEqual(memory_items[0].memory_kind, SessionMemoryKind.HANDOFF)

    async def test_build_hydration_bundle_collects_recent_scope_state(self) -> None:
        store = InMemoryOrchestrationStore()
        service = SessionMemoryService(store=store)
        head = ConversationHead(
            conversation_head_id="convhead_1",
            work_session_id="worksession_1",
            runtime_generation_id="runtimegen_2",
            head_kind=ConversationHeadKind.WORKER,
            scope_id="worker-alpha",
            backend="subprocess",
            model="gpt-5.2",
            provider="openai",
            last_response_id="resp-worker-1",
            checkpoint_summary="Worker checkpoint",
            updated_at="2026-04-11T13:00:00+00:00",
            invalidation_reason="backend changed",
        )
        await store.save_conversation_head(head)
        await store.append_turn_record(
            service.make_turn_record(
                work_session_id="worksession_1",
                runtime_generation_id="runtimegen_2",
                head_kind=ConversationHeadKind.WORKER,
                scope_id="worker-alpha",
                actor_role="worker",
                assignment_id="assignment-1",
                turn_kind="worker_result",
                input_summary="Previous task input",
                output_summary="Previous task output",
                response_id="resp-worker-1",
                status="completed",
                created_at="2026-04-11T13:00:01+00:00",
            )
        )
        await store.save_session_memory_item(
            service.make_memory_item(
                work_session_id="worksession_1",
                runtime_generation_id="runtimegen_2",
                head_kind=ConversationHeadKind.WORKER,
                scope_id="worker-alpha",
                memory_kind="open_loop",
                importance=9,
                summary="Need to finish post-merge verification.",
                created_at="2026-04-11T13:00:02+00:00",
            )
        )

        bundle = await service.build_hydration_bundle(
            work_session_id="worksession_1",
            runtime_generation_id="runtimegen_2",
            conversation_head=head,
            continuation_mode="warm_resume",
            runtime_status_summary={"status": "detached"},
            shell_attach_summary={"mode": "warm_resumed"},
        )

        self.assertEqual(bundle.scope_id, "worker-alpha")
        self.assertEqual(bundle.last_response_id, "resp-worker-1")
        self.assertEqual(len(bundle.recent_turns), 1)
        self.assertEqual(len(bundle.memory_items), 1)
        self.assertEqual(bundle.invalidated_continuity_reasons, ("backend changed",))
        self.assertEqual(bundle.shell_attach_summary["mode"], "warm_resumed")

    async def test_build_hydration_bundle_applies_limits_from_checkpoint_metadata(self) -> None:
        store = InMemoryOrchestrationStore()
        service = SessionMemoryService(store=store)
        head = ConversationHead(
            conversation_head_id="convhead_limits",
            work_session_id="worksession_limits",
            runtime_generation_id="runtimegen_limits",
            head_kind=ConversationHeadKind.WORKER,
            scope_id="worker-limits",
            backend="subprocess",
            model="gpt-5.2",
            provider="openai",
            last_response_id=None,
            checkpoint_summary="Worker checkpoint",
            checkpoint_metadata={
                "hydration": {
                    "recent_turn_limit": 1,
                    "memory_item_limit": 1,
                    "tool_invocation_limit": 1,
                    "artifact_ref_limit": 1,
                }
            },
            updated_at="2026-04-11T13:00:00+00:00",
        )
        await store.save_conversation_head(head)
        turn_one = service.make_turn_record(
            work_session_id="worksession_limits",
            runtime_generation_id="runtimegen_limits",
            head_kind=ConversationHeadKind.WORKER,
            scope_id="worker-limits",
            actor_role="worker",
            assignment_id="assignment-1",
            turn_kind="worker_result",
            input_summary="Input one",
            output_summary="Output one",
            response_id="resp-1",
            status="completed",
            created_at="2026-04-11T13:00:01+00:00",
        )
        turn_two = service.make_turn_record(
            work_session_id="worksession_limits",
            runtime_generation_id="runtimegen_limits",
            head_kind=ConversationHeadKind.WORKER,
            scope_id="worker-limits",
            actor_role="worker",
            assignment_id="assignment-2",
            turn_kind="worker_result",
            input_summary="Input two",
            output_summary="Output two",
            response_id="resp-2",
            status="completed",
            created_at="2026-04-11T13:00:02+00:00",
        )
        await store.append_turn_record(turn_one)
        await store.append_turn_record(turn_two)
        await store.append_tool_invocation_record(
            ToolInvocationRecord.from_payload(
                {
                    "work_session_id": "worksession_limits",
                    "runtime_generation_id": "runtimegen_limits",
                    "turn_record_id": turn_one.turn_record_id,
                    "tool_name": "pytest",
                    "tool_kind": "local_command",
                    "input_summary": "pytest -q",
                    "output_summary": "ok",
                    "status": "completed",
                    "started_at": "2026-04-11T13:00:03+00:00",
                }
            )
        )
        await store.append_tool_invocation_record(
            ToolInvocationRecord.from_payload(
                {
                    "work_session_id": "worksession_limits",
                    "runtime_generation_id": "runtimegen_limits",
                    "turn_record_id": turn_two.turn_record_id,
                    "tool_name": "ruff",
                    "tool_kind": "local_command",
                    "input_summary": "ruff .",
                    "output_summary": "ok",
                    "status": "completed",
                    "started_at": "2026-04-11T13:00:04+00:00",
                }
            )
        )
        await store.save_artifact_ref(
            ArtifactRef(
                turn_record_id=turn_one.turn_record_id,
                work_session_id="worksession_limits",
                runtime_generation_id="runtimegen_limits",
                artifact_kind=ArtifactRefKind.FINAL_REPORT,
                storage_kind=ArtifactStorageKind.INLINE_JSON,
                uri_or_path="report-1",
            )
        )
        await store.save_artifact_ref(
            ArtifactRef(
                turn_record_id=turn_two.turn_record_id,
                work_session_id="worksession_limits",
                runtime_generation_id="runtimegen_limits",
                artifact_kind=ArtifactRefKind.PROTOCOL_EVENTS,
                storage_kind=ArtifactStorageKind.INLINE_JSON,
                uri_or_path="events-2",
            )
        )
        await store.save_session_memory_item(
            service.make_memory_item(
                work_session_id="worksession_limits",
                runtime_generation_id="runtimegen_limits",
                head_kind=ConversationHeadKind.WORKER,
                scope_id="worker-limits",
                memory_kind="open_loop",
                importance=8,
                summary="First memory item",
                created_at="2026-04-11T13:00:05+00:00",
            )
        )
        await store.save_session_memory_item(
            service.make_memory_item(
                work_session_id="worksession_limits",
                runtime_generation_id="runtimegen_limits",
                head_kind=ConversationHeadKind.WORKER,
                scope_id="worker-limits",
                memory_kind="handoff",
                importance=6,
                summary="Second memory item",
                created_at="2026-04-11T13:00:06+00:00",
            )
        )

        bundle = await service.build_hydration_bundle(
            work_session_id="worksession_limits",
            runtime_generation_id="runtimegen_limits",
            conversation_head=head,
            continuation_mode="warm_resume",
        )

        self.assertEqual(len(bundle.recent_turns), 1)
        self.assertEqual(len(bundle.memory_items), 1)
        self.assertEqual(len(bundle.recent_tool_invocations), 1)
        self.assertEqual(len(bundle.artifact_refs), 1)

    async def test_render_hydration_prompt_redacts_sensitive_values(self) -> None:
        store = InMemoryOrchestrationStore()
        service = SessionMemoryService(store=store)
        head = ConversationHead(
            conversation_head_id="convhead_redact",
            work_session_id="worksession_redact",
            runtime_generation_id="runtimegen_redact",
            head_kind=ConversationHeadKind.WORKER,
            scope_id="worker-redact",
            backend="subprocess",
            model="gpt-5.2",
            provider="openai",
            last_response_id=None,
            checkpoint_summary="Worker checkpoint",
            updated_at="2026-04-11T13:00:00+00:00",
        )
        await store.save_conversation_head(head)
        await store.append_turn_record(
            service.make_turn_record(
                work_session_id="worksession_redact",
                runtime_generation_id="runtimegen_redact",
                head_kind=ConversationHeadKind.WORKER,
                scope_id="worker-redact",
                actor_role="worker",
                assignment_id="assignment-1",
                turn_kind="worker_result",
                input_summary="Input with sk-SECRET1234567890 token",
                output_summary="Output",
                response_id=None,
                status="completed",
                created_at="2026-04-11T13:00:01+00:00",
            )
        )
        await store.save_session_memory_item(
            service.make_memory_item(
                work_session_id="worksession_redact",
                runtime_generation_id="runtimegen_redact",
                head_kind=ConversationHeadKind.WORKER,
                scope_id="worker-redact",
                memory_kind="open_loop",
                importance=8,
                summary="Bearer SECRET_TOKEN_SHOULD_REDACT",
                created_at="2026-04-11T13:00:02+00:00",
            )
        )

        bundle = await service.build_hydration_bundle(
            work_session_id="worksession_redact",
            runtime_generation_id="runtimegen_redact",
            conversation_head=head,
            continuation_mode="warm_resume",
        )
        prompt = service.render_hydration_prompt(bundle)

        self.assertNotIn("sk-SECRET1234567890", prompt)
        self.assertNotIn("Bearer SECRET_TOKEN_SHOULD_REDACT", prompt)

    async def test_render_hydration_prompt_compacts_artifact_paths(self) -> None:
        store = InMemoryOrchestrationStore()
        service = SessionMemoryService(store=store)
        head = ConversationHead(
            conversation_head_id="convhead_artifact",
            work_session_id="worksession_artifact",
            runtime_generation_id="runtimegen_artifact",
            head_kind=ConversationHeadKind.WORKER,
            scope_id="worker-artifact",
            backend="subprocess",
            model="gpt-5.2",
            provider="openai",
            last_response_id=None,
            checkpoint_summary="Worker checkpoint",
            updated_at="2026-04-11T13:00:00+00:00",
        )
        await store.save_conversation_head(head)
        turn = service.make_turn_record(
            work_session_id="worksession_artifact",
            runtime_generation_id="runtimegen_artifact",
            head_kind=ConversationHeadKind.WORKER,
            scope_id="worker-artifact",
            actor_role="worker",
            assignment_id="assignment-1",
            turn_kind="worker_result",
            input_summary="Input",
            output_summary="Output",
            response_id=None,
            status="completed",
            created_at="2026-04-11T13:00:01+00:00",
        )
        await store.append_turn_record(turn)
        await store.save_artifact_ref(
            ArtifactRef(
                turn_record_id=turn.turn_record_id,
                work_session_id="worksession_artifact",
                runtime_generation_id="runtimegen_artifact",
                artifact_kind=ArtifactRefKind.GENERATED_FILE,
                storage_kind=ArtifactStorageKind.EXTERNAL_REF,
                uri_or_path="/secret/path/to/output.txt",
            )
        )

        bundle = await service.build_hydration_bundle(
            work_session_id="worksession_artifact",
            runtime_generation_id="runtimegen_artifact",
            conversation_head=head,
            continuation_mode="warm_resume",
        )
        prompt = service.render_hydration_prompt(bundle)

        self.assertIn(".../output.txt", prompt)
        self.assertNotIn("/secret/path/to/output.txt", prompt)
