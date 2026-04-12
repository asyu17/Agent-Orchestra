from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path
from unittest import IsolatedAsyncioTestCase

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agent_orchestra.runtime.session_continuity import ConversationHead, SessionContinuityService
from agent_orchestra.contracts.execution import WorkerAssignment, WorkerHandle, WorkerRecord
from agent_orchestra.contracts.enums import WorkerStatus
from agent_orchestra.storage.base import SessionTransactionStoreCommit
from agent_orchestra.storage.in_memory import InMemoryOrchestrationStore


class _ContinuityStore(InMemoryOrchestrationStore):
    def __init__(self) -> None:
        super().__init__()
        self.work_sessions: dict[str, object] = {}
        self.runtime_generations: dict[str, object] = {}
        self.work_session_messages: dict[str, list[object]] = {}
        self.conversation_heads: dict[str, object] = {}
        self.session_events: dict[str, list[object]] = {}
        self.session_transaction_commits: list[SessionTransactionStoreCommit] = []

    async def save_work_session(self, session: object) -> None:
        self.work_sessions[getattr(session, "work_session_id")] = session

    async def get_work_session(self, work_session_id: str) -> object | None:
        return self.work_sessions.get(work_session_id)

    async def list_work_sessions(
        self,
        group_id: str,
        *,
        root_objective_id: str | None = None,
    ) -> list[object]:
        sessions = list(self.work_sessions.values())
        sessions = [session for session in sessions if getattr(session, "group_id", None) == group_id]
        if root_objective_id is not None:
            sessions = [
                session
                for session in sessions
                if getattr(session, "root_objective_id", None) == root_objective_id
            ]
        return sessions

    async def save_runtime_generation(self, generation: object) -> None:
        self.runtime_generations[getattr(generation, "runtime_generation_id")] = generation

    async def get_runtime_generation(self, runtime_generation_id: str) -> object | None:
        return self.runtime_generations.get(runtime_generation_id)

    async def list_runtime_generations(self, work_session_id: str) -> list[object]:
        generations = [
            generation
            for generation in self.runtime_generations.values()
            if getattr(generation, "work_session_id", None) == work_session_id
        ]
        return sorted(generations, key=lambda item: getattr(item, "generation_index", 0))

    async def append_work_session_message(self, message: object) -> None:
        self.work_session_messages.setdefault(getattr(message, "work_session_id"), []).append(message)

    async def list_work_session_messages(
        self,
        work_session_id: str,
        *,
        runtime_generation_id: str | None = None,
    ) -> list[object]:
        messages = list(self.work_session_messages.get(work_session_id, ()))
        if runtime_generation_id is not None:
            messages = [
                message
                for message in messages
                if getattr(message, "runtime_generation_id", None) == runtime_generation_id
            ]
        return messages

    async def save_conversation_head(self, head: object) -> None:
        self.conversation_heads[getattr(head, "conversation_head_id")] = head

    async def get_conversation_head(self, conversation_head_id: str) -> object | None:
        return self.conversation_heads.get(conversation_head_id)

    async def list_conversation_heads(
        self,
        work_session_id: str,
        *,
        runtime_generation_id: str | None = None,
    ) -> list[object]:
        heads = [
            head
            for head in self.conversation_heads.values()
            if getattr(head, "work_session_id", None) == work_session_id
        ]
        if runtime_generation_id is not None:
            heads = [
                head
                for head in heads
                if getattr(head, "runtime_generation_id", None) == runtime_generation_id
            ]
        return heads

    async def append_session_event(self, event: object) -> None:
        self.session_events.setdefault(getattr(event, "work_session_id"), []).append(event)

    async def list_session_events(
        self,
        work_session_id: str,
        *,
        runtime_generation_id: str | None = None,
    ) -> list[object]:
        events = list(self.session_events.get(work_session_id, ()))
        if runtime_generation_id is not None:
            events = [
                event
                for event in events
                if getattr(event, "runtime_generation_id", None) == runtime_generation_id
            ]
        return events

    async def find_latest_resumable_runtime_generation(self, work_session_id: str) -> object | None:
        generations = await self.list_runtime_generations(work_session_id)
        resumable = [
            generation
            for generation in generations
            if getattr(generation, "status", None) not in {"closed", "failed"}
        ]
        if resumable:
            return resumable[-1]
        return generations[-1] if generations else None

    async def commit_session_transaction(
        self,
        commit: SessionTransactionStoreCommit,
    ) -> None:
        self.session_transaction_commits.append(commit)
        for work_session in commit.work_sessions:
            await self.save_work_session(work_session)
        for runtime_generation in commit.runtime_generations:
            await self.save_runtime_generation(runtime_generation)
        for message in commit.work_session_messages:
            await self.append_work_session_message(message)
        for conversation_head in commit.conversation_heads:
            await self.save_conversation_head(conversation_head)
        for session_event in commit.session_events:
            await self.append_session_event(session_event)
        for turn_record in commit.turn_records:
            await self.append_turn_record(turn_record)
        for tool_invocation_record in commit.tool_invocation_records:
            await self.append_tool_invocation_record(tool_invocation_record)
        for artifact_ref in commit.artifact_refs:
            await self.save_artifact_ref(artifact_ref)
        for session_memory_item in commit.session_memory_items:
            await self.save_session_memory_item(session_memory_item)
        for resident_shell in commit.resident_team_shells:
            await self.save_resident_team_shell(resident_shell)


class SessionContinuityServiceTest(IsolatedAsyncioTestCase):
    async def test_new_session_records_generation_message_and_events(self) -> None:
        store = _ContinuityStore()
        service = SessionContinuityService(store=store)

        created = await service.new_session(
            group_id="group-a",
            objective_id="obj-runtime",
            title="Runtime continuity",
        )

        self.assertEqual(created.work_session.group_id, "group-a")
        self.assertEqual(created.work_session.root_objective_id, "obj-runtime")
        self.assertEqual(
            created.work_session.current_runtime_generation_id,
            created.runtime_generation.runtime_generation_id,
        )
        self.assertEqual(created.runtime_generation.continuity_mode, "fresh")
        self.assertEqual(created.runtime_generation.generation_index, 0)

        messages = await store.list_work_session_messages(created.work_session.work_session_id)
        events = await store.list_session_events(created.work_session.work_session_id)

        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].role, "system")
        self.assertIn("new session", messages[0].content.lower())
        self.assertEqual(len(store.session_transaction_commits), 1)
        self.assertEqual(len(store.session_transaction_commits[0].work_sessions), 1)
        self.assertEqual(len(store.session_transaction_commits[0].runtime_generations), 1)
        self.assertEqual(
            [event.event_kind for event in events],
            ["runtime_generation_started", "new_session_created"],
        )

    async def test_warm_resume_copies_compatible_heads_and_invalidates_incompatible_provider_state(self) -> None:
        store = _ContinuityStore()
        service = SessionContinuityService(store=store)
        created = await service.new_session(
            group_id="group-a",
            objective_id="obj-runtime",
            title="Runtime continuity",
        )
        work_session_id = created.work_session.work_session_id
        runtime_generation_id = created.runtime_generation.runtime_generation_id

        await store.save_conversation_head(
            ConversationHead(
                conversation_head_id="head-leader-runtime",
                work_session_id=work_session_id,
                runtime_generation_id=runtime_generation_id,
                head_kind="leader_lane",
                scope_id="runtime",
                backend="in_process",
                model="gpt-4.1",
                provider="openai",
                last_response_id="resp-leader-1",
                checkpoint_summary="Leader checkpoint",
                updated_at="2026-04-10T00:00:00+00:00",
            )
        )
        await store.save_conversation_head(
            ConversationHead(
                conversation_head_id="head-teammate-runtime-1",
                work_session_id=work_session_id,
                runtime_generation_id=runtime_generation_id,
                head_kind="teammate_slot",
                scope_id="team-runtime:teammate:1",
                backend="in_process",
                model="gpt-4.1",
                provider="openai",
                last_response_id="resp-teammate-1",
                checkpoint_summary="Teammate checkpoint",
                updated_at="2026-04-10T00:00:00+00:00",
            )
        )

        resumed = await service.warm_resume(
            work_session_id=work_session_id,
            head_contracts={
                ("leader_lane", "runtime"): {
                    "backend": "in_process",
                    "provider": "openai",
                    "model": "gpt-4.1",
                },
                ("teammate_slot", "team-runtime:teammate:1"): {
                    "backend": "subprocess",
                },
            },
        )

        self.assertEqual(resumed.runtime_generation.continuity_mode, "warm_resume")
        self.assertEqual(resumed.runtime_generation.generation_index, 1)
        self.assertEqual(
            resumed.work_session.current_runtime_generation_id,
            resumed.runtime_generation.runtime_generation_id,
        )

        resumed_heads = {
            (head.head_kind, head.scope_id): head
            for head in resumed.conversation_heads
        }
        compatible = resumed_heads[("leader_lane", "runtime")]
        incompatible = resumed_heads[("teammate_slot", "team-runtime:teammate:1")]

        self.assertEqual(compatible.last_response_id, "resp-leader-1")
        self.assertIsNone(incompatible.last_response_id)
        self.assertEqual(incompatible.checkpoint_summary, "Teammate checkpoint")
        self.assertIn("backend", incompatible.invalidation_reason or "")
        self.assertGreaterEqual(len(store.session_transaction_commits), 2)

        events = await store.list_session_events(work_session_id)
        self.assertIn("warm_resume_started", [event.event_kind for event in events])
        self.assertIn("conversation_head_invalidated", [event.event_kind for event in events])

    async def test_fork_session_creates_new_root_without_live_response_chain(self) -> None:
        store = _ContinuityStore()
        service = SessionContinuityService(store=store)
        created = await service.new_session(
            group_id="group-a",
            objective_id="obj-runtime",
            title="Runtime continuity",
        )
        await store.save_conversation_head(
            ConversationHead(
                conversation_head_id="head-leader-runtime",
                work_session_id=created.work_session.work_session_id,
                runtime_generation_id=created.runtime_generation.runtime_generation_id,
                head_kind="leader_lane",
                scope_id="runtime",
                backend="in_process",
                model="gpt-4.1",
                provider="openai",
                last_response_id="resp-leader-1",
                checkpoint_summary="Leader checkpoint",
                updated_at="2026-04-10T00:00:00+00:00",
            )
        )

        forked = await service.fork_session(
            work_session_id=created.work_session.work_session_id,
            title="Forked runtime continuity",
        )

        self.assertNotEqual(
            forked.work_session.work_session_id,
            created.work_session.work_session_id,
        )
        self.assertEqual(
            forked.work_session.fork_origin_work_session_id,
            created.work_session.work_session_id,
        )
        self.assertEqual(forked.runtime_generation.continuity_mode, "fork_seed")
        self.assertEqual(forked.runtime_generation.generation_index, 0)
        self.assertEqual(len(forked.conversation_heads), 1)
        self.assertIsNone(forked.conversation_heads[0].last_response_id)
        self.assertEqual(forked.conversation_heads[0].checkpoint_summary, "Leader checkpoint")
        self.assertGreaterEqual(len(store.session_transaction_commits), 2)

        fork_events = await store.list_session_events(forked.work_session.work_session_id)
        self.assertIn("fork_created", [event.event_kind for event in fork_events])

    async def test_resume_gate_degrades_to_inspect_only_when_runtime_ownership_is_ambiguous(self) -> None:
        store = _ContinuityStore()
        service = SessionContinuityService(store=store)
        created = await service.new_session(
            group_id="group-a",
            objective_id="obj-runtime",
            title="Runtime continuity",
        )
        ambiguous_generation = replace(
            created.runtime_generation,
            status="active",
            metadata={
                **created.runtime_generation.metadata,
                "ownership_ambiguous": True,
            },
        )
        await store.save_runtime_generation(ambiguous_generation)

        decision = await service.resume_gate(created.work_session.work_session_id)

        self.assertEqual(decision.mode, "inspect_only")
        self.assertEqual(decision.target_work_session_id, created.work_session.work_session_id)
        self.assertEqual(
            decision.target_runtime_generation_id,
            ambiguous_generation.runtime_generation_id,
        )
        self.assertTrue(decision.requires_user_confirmation)

    async def test_list_sessions_filters_by_objective(self) -> None:
        store = _ContinuityStore()
        service = SessionContinuityService(store=store)

        first = await service.new_session(
            group_id="group-a",
            objective_id="obj-runtime-a",
            title="Runtime A",
        )
        second = await service.new_session(
            group_id="group-a",
            objective_id="obj-runtime-b",
            title="Runtime B",
        )

        sessions = await service.list_sessions(group_id="group-a")
        filtered = await service.list_sessions(
            group_id="group-a",
            root_objective_id="obj-runtime-a",
        )

        self.assertEqual(
            {session.work_session_id for session in sessions},
            {
                first.work_session.work_session_id,
                second.work_session.work_session_id,
            },
        )
        self.assertEqual(
            [session.work_session_id for session in filtered],
            [first.work_session.work_session_id],
        )

    async def test_inspect_session_returns_current_generation_and_continuation_bundle(self) -> None:
        store = _ContinuityStore()
        service = SessionContinuityService(store=store)
        created = await service.new_session(
            group_id="group-a",
            objective_id="obj-runtime",
            title="Runtime continuity",
        )
        await store.save_conversation_head(
            ConversationHead(
                conversation_head_id="head-leader-runtime",
                work_session_id=created.work_session.work_session_id,
                runtime_generation_id=created.runtime_generation.runtime_generation_id,
                head_kind="leader_lane",
                scope_id="runtime",
                backend="in_process",
                model="gpt-4.1",
                provider="openai",
                last_response_id="resp-leader-1",
                checkpoint_summary="Leader checkpoint",
                updated_at="2026-04-10T00:00:00+00:00",
            )
        )

        snapshot = await service.inspect_session(created.work_session.work_session_id)

        self.assertEqual(
            snapshot.work_session.work_session_id,
            created.work_session.work_session_id,
        )
        self.assertEqual(len(snapshot.runtime_generations), 1)
        self.assertIsNotNone(snapshot.current_runtime_generation)
        assert snapshot.current_runtime_generation is not None
        self.assertEqual(
            snapshot.current_runtime_generation.runtime_generation_id,
            created.runtime_generation.runtime_generation_id,
        )
        self.assertEqual(snapshot.resume_gate.mode, "exact_wake")
        self.assertEqual(len(snapshot.continuation_bundles), 1)
        bundle = snapshot.continuation_bundles[0]
        self.assertEqual(bundle.scope_id, "runtime")
        self.assertEqual(bundle.last_response_id, "resp-leader-1")
        self.assertEqual(bundle.runtime_status_summary["status"], "booting")
        self.assertEqual(snapshot.resident_shell_views, ())

    async def test_inspect_session_defaults_resident_shell_views_to_empty(self) -> None:
        store = _ContinuityStore()
        service = SessionContinuityService(store=store)
        created = await service.new_session(
            group_id="group-a",
            objective_id="obj-runtime",
            title="Runtime continuity",
        )

        snapshot = await service.inspect_session(created.work_session.work_session_id)

        self.assertEqual(snapshot.resident_shell_views, ())

    async def test_inspect_session_includes_hydration_bundle(self) -> None:
        store = _ContinuityStore()
        service = SessionContinuityService(store=store)
        created = await service.new_session(
            group_id="group-a",
            objective_id="obj-runtime",
            title="Runtime continuity",
        )
        head = ConversationHead(
            conversation_head_id="head-worker-runtime",
            work_session_id=created.work_session.work_session_id,
            runtime_generation_id=created.runtime_generation.runtime_generation_id,
            head_kind="worker",
            scope_id="worker-alpha",
            backend="subprocess",
            model="gpt-5.2",
            provider="openai",
            last_response_id=None,
            checkpoint_summary="Worker checkpoint",
            updated_at="2026-04-11T13:00:00+00:00",
            invalidation_reason="backend changed",
        )
        await store.save_conversation_head(head)
        await store.append_turn_record(
            service._session_memory_service.make_turn_record(
                work_session_id=created.work_session.work_session_id,
                runtime_generation_id=created.runtime_generation.runtime_generation_id,
                head_kind="worker",
                scope_id="worker-alpha",
                actor_role="worker",
                assignment_id="assignment-1",
                turn_kind="worker_result",
                input_summary="Prior input",
                output_summary="Prior output",
                status="completed",
                created_at="2026-04-11T13:00:01+00:00",
            )
        )

        snapshot = await service.inspect_session(created.work_session.work_session_id)

        self.assertEqual(len(snapshot.hydration_bundles), 1)
        bundle = snapshot.hydration_bundles[0]
        self.assertEqual(bundle.scope_id, "worker-alpha")
        self.assertEqual(bundle.invalidated_continuity_reasons, ("backend changed",))
        self.assertEqual(len(bundle.recent_turns), 1)
        self.assertEqual(len(snapshot.hydration_summary), 1)
        summary = snapshot.hydration_summary[0]
        self.assertEqual(summary["scope_id"], "worker-alpha")
        self.assertEqual(summary["coverage"]["turn_count"], 1)
        self.assertEqual(summary["coverage"]["artifact_ref_count"], 0)

    async def test_apply_assignment_continuity_injects_hydration_for_missing_response_id(self) -> None:
        store = _ContinuityStore()
        service = SessionContinuityService(store=store)
        created = await service.new_session(
            group_id="group-a",
            objective_id="obj-runtime",
            title="Runtime continuity",
        )
        await store.save_conversation_head(
            ConversationHead(
                conversation_head_id="head-worker-runtime",
                work_session_id=created.work_session.work_session_id,
                runtime_generation_id=created.runtime_generation.runtime_generation_id,
                head_kind="worker",
                scope_id="worker-alpha",
                backend="subprocess",
                model="gpt-5.2",
                provider="openai",
                last_response_id=None,
                checkpoint_summary="Worker checkpoint",
                updated_at="2026-04-11T13:00:00+00:00",
                invalidation_reason="backend changed",
            )
        )
        await store.save_session_memory_item(
            service._session_memory_service.make_memory_item(
                work_session_id=created.work_session.work_session_id,
                runtime_generation_id=created.runtime_generation.runtime_generation_id,
                head_kind="worker",
                scope_id="worker-alpha",
                memory_kind="open_loop",
                importance=8,
                summary="Need to continue verification after resume.",
                created_at="2026-04-11T13:00:01+00:00",
            )
        )

        assignment = WorkerAssignment(
            assignment_id="assignment-1",
            worker_id="worker-alpha",
            group_id="group-a",
            task_id="task-1",
            role="worker",
            backend="subprocess",
            instructions="Continue the work.",
            input_text="Resume execution.",
        )

        continued = await service.apply_assignment_continuity(
            work_session_id=created.work_session.work_session_id,
            runtime_generation_id=created.runtime_generation.runtime_generation_id,
            assignment=assignment,
        )

        self.assertIsNone(continued.previous_response_id)
        self.assertIn("Session Hydration", continued.input_text)
        self.assertIn("Need to continue verification after resume.", continued.input_text)
        self.assertIn("hydration_bundle", continued.metadata)

    async def test_record_worker_turn_commits_head_event_and_memory_ledger_atomically(self) -> None:
        store = _ContinuityStore()
        service = SessionContinuityService(store=store)
        created = await service.new_session(
            group_id="group-a",
            objective_id="obj-runtime",
            title="Runtime continuity",
        )
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
            work_session_id=created.work_session.work_session_id,
            runtime_generation_id=created.runtime_generation.runtime_generation_id,
            assignment=assignment,
            record=record,
        )

        latest_commit = store.session_transaction_commits[-1]
        self.assertEqual(len(latest_commit.conversation_heads), 1)
        self.assertEqual(len(latest_commit.session_events), 1)
        self.assertEqual(len(latest_commit.turn_records), 1)
        self.assertEqual(len(latest_commit.tool_invocation_records), 1)
        self.assertEqual(len(latest_commit.artifact_refs), 3)
        self.assertEqual(len(latest_commit.session_memory_items), 1)
