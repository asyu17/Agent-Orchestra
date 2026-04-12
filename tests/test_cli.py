from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest import IsolatedAsyncioTestCase, TestCase

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agent_orchestra.cli.app import CliApplication
from agent_orchestra.cli.main import build_parser, main
from agent_orchestra.contracts.agent import SessionBinding
from agent_orchestra.contracts.session_continuity import (
    ConversationHead,
    ConversationHeadKind,
    RuntimeGenerationStatus,
    ShellAttachDecisionMode,
)
from agent_orchestra.contracts.execution import (
    ResidentCoordinatorPhase,
    ResidentCoordinatorSession,
)
from agent_orchestra.runtime.orchestrator import build_in_memory_orchestra


class CliParserTest(TestCase):
    def test_parser_defaults_session_store_backend_to_postgres(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            [
                "session",
                "list",
                "--group-id",
                "group-a",
            ]
        )
        self.assertEqual(args.store_backend, "postgres")

    def test_parser_accepts_session_list_with_store_flags(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            [
                "session",
                "list",
                "--store-backend",
                "postgres",
                "--dsn",
                "postgresql://demo/runtime",
                "--schema",
                "demo_runtime",
                "--group-id",
                "group-a",
                "--objective-id",
                "obj-a",
            ]
        )
        self.assertEqual(args.command, "session")
        self.assertEqual(args.session_command, "list")
        self.assertEqual(args.store_backend, "postgres")
        self.assertEqual(args.dsn, "postgresql://demo/runtime")
        self.assertEqual(args.schema, "demo_runtime")

    def test_parser_accepts_session_fork_title(self) -> None:
        parser = build_parser()
        fork_args = parser.parse_args(
            [
                "session",
                "fork",
                "--work-session-id",
                "ws-1",
                "--title",
                "Forked session",
            ]
        )
        self.assertEqual(fork_args.title, "Forked session")

    def test_parser_rejects_legacy_session_resume(self) -> None:
        parser = build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(
                [
                    "session",
                    "resume",
                    "--work-session-id",
                    "ws-1",
                ]
            )

    def test_parser_accepts_session_attach(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            [
                "session",
                "attach",
                "--work-session-id",
                "ws-1",
            ]
        )
        self.assertEqual(args.command, "session")
        self.assertEqual(args.session_command, "attach")
        self.assertEqual(args.work_session_id, "ws-1")

    def test_parser_accepts_session_wake(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            [
                "session",
                "wake",
                "--work-session-id",
                "ws-1",
            ]
        )
        self.assertEqual(args.command, "session")
        self.assertEqual(args.session_command, "wake")
        self.assertEqual(args.work_session_id, "ws-1")


class CliApplicationTest(IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.orchestra = build_in_memory_orchestra()
        self.app = CliApplication(orchestra=self.orchestra)
        self.store = self.orchestra.store

    async def test_session_new_and_inspect_return_real_snapshot(self) -> None:
        created = await self.app.session_new(
            group_id="group-a",
            objective_id="obj-runtime",
            title="Runtime continuity",
        )
        work_session_id = created["continuity"]["work_session"]["work_session_id"]

        payload = await self.app.session_inspect(work_session_id=work_session_id)

        self.assertEqual(payload["command"], "session.inspect")
        snapshot = payload["snapshot"]
        assert isinstance(snapshot, dict)
        self.assertEqual(snapshot["work_session"]["work_session_id"], work_session_id)
        self.assertEqual(len(snapshot["runtime_generations"]), 1)
        self.assertEqual(snapshot["resume_gate"]["mode"], "exact_wake")
        self.assertEqual(snapshot["continuation_bundles"], [])
        self.assertEqual(snapshot["resident_shell_views"], [])
        self.assertEqual(snapshot["hydration_summary"], [])

    async def test_session_list_filters_by_objective(self) -> None:
        first = await self.app.session_new(
            group_id="group-a",
            objective_id="obj-a",
            title="Runtime A",
        )
        await self.app.session_new(
            group_id="group-a",
            objective_id="obj-b",
            title="Runtime B",
        )

        payload = await self.app.session_list(group_id="group-a", objective_id="obj-a")

        self.assertEqual(payload["command"], "session.list")
        self.assertEqual(
            [session["work_session_id"] for session in payload["sessions"]],
            [first["continuity"]["work_session"]["work_session_id"]],
        )

    async def test_session_fork_clears_response_chain(self) -> None:
        created = await self.app.session_new(
            group_id="group-a",
            objective_id="obj-runtime",
            title="Runtime continuity",
        )
        work_session = created["continuity"]["work_session"]
        runtime_generation = created["continuity"]["runtime_generation"]
        await self.store.save_conversation_head(
            ConversationHead(
                conversation_head_id="head-leader-runtime",
                work_session_id=work_session["work_session_id"],
                runtime_generation_id=runtime_generation["runtime_generation_id"],
                head_kind=ConversationHeadKind.LEADER_LANE,
                scope_id="runtime",
                backend="in_process",
                model="gpt-4.1",
                provider="openai",
                last_response_id="resp-leader-1",
                checkpoint_summary="Leader checkpoint",
                updated_at="2026-04-10T00:00:00+00:00",
            )
        )

        payload = await self.app.session_fork(
            work_session_id=work_session["work_session_id"],
            title="Forked continuity",
        )

        self.assertEqual(payload["command"], "session.fork")
        heads = payload["continuity"]["conversation_heads"]
        self.assertEqual(len(heads), 1)
        self.assertIsNone(heads[0]["last_response_id"])
        self.assertEqual(heads[0]["checkpoint_summary"], "Leader checkpoint")

    async def test_cli_application_no_longer_exposes_session_resume_alias(self) -> None:
        self.assertFalse(hasattr(self.app, "session_resume"))

    async def test_session_attach_warm_resumes_detached_generation(self) -> None:
        created = await self.app.session_new(
            group_id="group-a",
            objective_id="obj-runtime",
            title="Runtime continuity",
        )
        work_session_id = created["continuity"]["work_session"]["work_session_id"]
        generation_id = created["continuity"]["runtime_generation"]["runtime_generation_id"]
        generation = await self.store.get_runtime_generation(generation_id)
        assert generation is not None
        await self.store.save_runtime_generation(
            replace(generation, status=RuntimeGenerationStatus.DETACHED)
        )

        payload = await self.app.session_attach(work_session_id=work_session_id)

        self.assertEqual(payload["result"]["action"], "warm_resumed")
        continuity_state = payload["result"]["continuity_state"]
        assert isinstance(continuity_state, dict)
        self.assertEqual(continuity_state["runtime_generation"]["generation_index"], 1)

    async def test_session_attach_exact_wake_returns_structured_result(self) -> None:
        created = await self.app.session_new(
            group_id="group-a",
            objective_id="obj-runtime",
            title="Runtime continuity",
        )
        runtime = self.app.runtime
        session_host = runtime.supervisor.session_host
        work_session = created["continuity"]["work_session"]
        runtime_generation = created["continuity"]["runtime_generation"]
        leader_session_id = "objective-runtime:lane-runtime:leader:resident"
        metadata = {
            "group_id": "group-a",
            "work_session_id": work_session["work_session_id"],
            "runtime_generation_id": runtime_generation["runtime_generation_id"],
        }
        await session_host.load_or_create_coordinator_session(
            session_id=leader_session_id,
            coordinator_id="leader:runtime",
            objective_id="obj-runtime",
            lane_id="lane-runtime",
            team_id="team-runtime",
            role="leader",
            host_owner_coordinator_id="superleader:obj-runtime",
            runtime_task_id="runtime-task-1",
            metadata=metadata,
        )
        await session_host.bind_session(
            leader_session_id,
            SessionBinding(
                session_id=leader_session_id,
                backend="tmux",
                binding_type="resident",
                transport_locator={"session_name": "ao-runtime", "pane_id": "%7"},
                supervisor_id="supervisor-live",
                lease_id="lease-live",
                lease_expires_at="2026-04-11T12:30:00+00:00",
            ),
        )
        await session_host.record_coordinator_session_state(
            leader_session_id,
            coordinator_session=ResidentCoordinatorSession(
                coordinator_id="leader:runtime",
                role="leader",
                phase=ResidentCoordinatorPhase.WAITING_FOR_MAILBOX,
                objective_id="obj-runtime",
                lane_id="lane-runtime",
                team_id="team-runtime",
                cycle_count=2,
                prompt_turn_count=1,
                claimed_task_count=0,
                subordinate_dispatch_count=0,
                mailbox_poll_count=3,
                mailbox_cursor="leader-envelope-attach",
                last_reason="Standing by for mailbox events.",
            ),
            last_progress_at="2026-04-11T12:00:00+00:00",
        )

        payload = await self.app.session_attach(
            work_session_id=work_session["work_session_id"]
        )

        self.assertEqual(payload["result"]["action"], "attached")
        self.assertEqual(
            payload["result"]["decision"]["mode"],
            ShellAttachDecisionMode.ATTACHED.value,
        )
        self.assertEqual(
            payload["result"]["metadata"]["preferred_session_id"],
            leader_session_id,
        )

    async def test_session_attach_returns_attached_for_live_shell(self) -> None:
        created = await self.app.session_new(
            group_id="group-a",
            objective_id="obj-runtime",
            title="Runtime continuity",
        )
        runtime = self.app.runtime
        session_host = runtime.supervisor.session_host
        work_session = created["continuity"]["work_session"]
        runtime_generation = created["continuity"]["runtime_generation"]
        leader_session_id = "objective-runtime:lane-runtime:leader:resident"
        metadata = {
            "group_id": "group-a",
            "work_session_id": work_session["work_session_id"],
            "runtime_generation_id": runtime_generation["runtime_generation_id"],
        }
        await session_host.load_or_create_coordinator_session(
            session_id=leader_session_id,
            coordinator_id="leader:runtime",
            objective_id="obj-runtime",
            lane_id="lane-runtime",
            team_id="team-runtime",
            role="leader",
            host_owner_coordinator_id="superleader:obj-runtime",
            runtime_task_id="runtime-task-1",
            metadata=metadata,
        )
        await session_host.bind_session(
            leader_session_id,
            SessionBinding(
                session_id=leader_session_id,
                backend="tmux",
                binding_type="resident",
                transport_locator={"session_name": "ao-runtime", "pane_id": "%7"},
                supervisor_id="supervisor-live",
                lease_id="lease-live",
                lease_expires_at="2026-04-11T12:30:00+00:00",
            ),
        )
        await session_host.record_coordinator_session_state(
            leader_session_id,
            coordinator_session=ResidentCoordinatorSession(
                coordinator_id="leader:runtime",
                role="leader",
                phase=ResidentCoordinatorPhase.WAITING_FOR_MAILBOX,
                objective_id="obj-runtime",
                lane_id="lane-runtime",
                team_id="team-runtime",
                cycle_count=2,
                prompt_turn_count=1,
                claimed_task_count=0,
                subordinate_dispatch_count=0,
                mailbox_poll_count=3,
                mailbox_cursor="leader-envelope-attach",
                last_reason="Standing by for mailbox events.",
            ),
            last_progress_at="2026-04-11T12:00:00+00:00",
        )

        payload = await self.app.session_attach(work_session_id=work_session["work_session_id"])

        self.assertEqual(payload["command"], "session.attach")
        self.assertEqual(payload["result"]["action"], "attached")
        self.assertEqual(
            payload["result"]["decision"]["mode"],
            ShellAttachDecisionMode.ATTACHED.value,
        )

    async def test_session_inspect_includes_resident_shell_views(self) -> None:
        created = await self.app.session_new(
            group_id="group-a",
            objective_id="obj-runtime",
            title="Runtime continuity",
        )
        runtime = self.app.runtime
        session_host = runtime.supervisor.session_host
        work_session = created["continuity"]["work_session"]
        runtime_generation = created["continuity"]["runtime_generation"]
        leader_session_id = "objective-runtime:lane-runtime:leader:resident"
        metadata = {
            "group_id": "group-a",
            "work_session_id": work_session["work_session_id"],
            "runtime_generation_id": runtime_generation["runtime_generation_id"],
        }
        await session_host.load_or_create_coordinator_session(
            session_id=leader_session_id,
            coordinator_id="leader:runtime",
            objective_id="obj-runtime",
            lane_id="lane-runtime",
            team_id="team-runtime",
            role="leader",
            host_owner_coordinator_id="superleader:obj-runtime",
            runtime_task_id="runtime-task-1",
            metadata=metadata,
        )
        await session_host.bind_session(
            leader_session_id,
            SessionBinding(
                session_id=leader_session_id,
                backend="tmux",
                binding_type="resident",
                transport_locator={"session_name": "ao-runtime", "pane_id": "%7"},
                supervisor_id="supervisor-live",
                lease_id="lease-live",
                lease_expires_at="2026-04-11T12:30:00+00:00",
            ),
        )
        await session_host.record_coordinator_session_state(
            leader_session_id,
            coordinator_session=ResidentCoordinatorSession(
                coordinator_id="leader:runtime",
                role="leader",
                phase=ResidentCoordinatorPhase.WAITING_FOR_MAILBOX,
                objective_id="obj-runtime",
                lane_id="lane-runtime",
                team_id="team-runtime",
                cycle_count=2,
                prompt_turn_count=1,
                claimed_task_count=0,
                subordinate_dispatch_count=0,
                mailbox_poll_count=3,
                mailbox_cursor="leader-envelope-attach",
                last_reason="Standing by for mailbox events.",
            ),
            last_progress_at="2026-04-11T12:00:00+00:00",
        )

        payload = await self.app.session_inspect(work_session_id=work_session["work_session_id"])

        snapshot = payload["snapshot"]
        assert isinstance(snapshot, dict)
        self.assertEqual(len(snapshot["resident_shell_views"]), 1)
        shell_view = snapshot["resident_shell_views"][0]
        self.assertEqual(shell_view["status"], "waiting_for_mailbox")
        self.assertEqual(
            shell_view["attach_recommendation"]["mode"],
            ShellAttachDecisionMode.ATTACHED.value,
        )
        self.assertEqual(shell_view["wake_capability"], "already_attached")
        self.assertEqual(shell_view["slot_summary"]["total"], 1)
        self.assertEqual(shell_view["leader_slot"]["session_id"], leader_session_id)

    async def test_session_wake_returns_attached_for_live_shell(self) -> None:
        created = await self.app.session_new(
            group_id="group-a",
            objective_id="obj-runtime",
            title="Runtime continuity",
        )
        runtime = self.app.runtime
        session_host = runtime.supervisor.session_host
        work_session = created["continuity"]["work_session"]
        runtime_generation = created["continuity"]["runtime_generation"]
        leader_session_id = "objective-runtime:lane-runtime:leader:resident"
        metadata = {
            "group_id": "group-a",
            "work_session_id": work_session["work_session_id"],
            "runtime_generation_id": runtime_generation["runtime_generation_id"],
        }
        await session_host.load_or_create_coordinator_session(
            session_id=leader_session_id,
            coordinator_id="leader:runtime",
            objective_id="obj-runtime",
            lane_id="lane-runtime",
            team_id="team-runtime",
            role="leader",
            host_owner_coordinator_id="superleader:obj-runtime",
            runtime_task_id="runtime-task-1",
            metadata=metadata,
        )
        await session_host.bind_session(
            leader_session_id,
            SessionBinding(
                session_id=leader_session_id,
                backend="tmux",
                binding_type="resident",
                transport_locator={"session_name": "ao-runtime", "pane_id": "%7"},
                supervisor_id="supervisor-live",
                lease_id="lease-live",
                lease_expires_at="2026-04-11T12:30:00+00:00",
            ),
        )
        await session_host.record_coordinator_session_state(
            leader_session_id,
            coordinator_session=ResidentCoordinatorSession(
                coordinator_id="leader:runtime",
                role="leader",
                phase=ResidentCoordinatorPhase.WAITING_FOR_MAILBOX,
                objective_id="obj-runtime",
                lane_id="lane-runtime",
                team_id="team-runtime",
                cycle_count=2,
                prompt_turn_count=1,
                claimed_task_count=0,
                subordinate_dispatch_count=0,
                mailbox_poll_count=3,
                mailbox_cursor="leader-envelope-attach",
                last_reason="Standing by for mailbox events.",
            ),
            last_progress_at="2026-04-11T12:00:00+00:00",
        )

        payload = await self.app.session_wake(work_session_id=work_session["work_session_id"])

        self.assertEqual(payload["command"], "session.wake")
        self.assertEqual(payload["result"]["action"], "attached")
        self.assertEqual(
            payload["result"]["decision"]["mode"],
            ShellAttachDecisionMode.ATTACHED.value,
        )

    async def test_session_wake_recovers_exact_wake_session_without_live_attach(self) -> None:
        created = await self.app.session_new(
            group_id="group-a",
            objective_id="obj-runtime",
            title="Runtime continuity",
        )

        payload = await self.app.session_wake(
            work_session_id=created["continuity"]["work_session"]["work_session_id"]
        )

        self.assertEqual(payload["command"], "session.wake")
        self.assertEqual(payload["result"]["action"], "recovered")
        self.assertTrue(payload["result"]["metadata"]["exact_wake_executed"])

    async def test_session_wake_requests_wake_for_quiescent_shell(self) -> None:
        created = await self.app.session_new(
            group_id="group-a",
            objective_id="obj-runtime",
            title="Runtime continuity",
        )
        runtime = self.app.runtime
        session_host = runtime.supervisor.session_host
        work_session = created["continuity"]["work_session"]
        runtime_generation = created["continuity"]["runtime_generation"]
        generation = await self.store.get_runtime_generation(runtime_generation["runtime_generation_id"])
        assert generation is not None
        await self.store.save_runtime_generation(
            replace(generation, status=RuntimeGenerationStatus.QUIESCENT)
        )
        leader_session_id = "objective-runtime:lane-runtime:leader:resident"
        metadata = {
            "group_id": "group-a",
            "work_session_id": work_session["work_session_id"],
            "runtime_generation_id": runtime_generation["runtime_generation_id"],
        }
        await session_host.load_or_create_coordinator_session(
            session_id=leader_session_id,
            coordinator_id="leader:runtime",
            objective_id="obj-runtime",
            lane_id="lane-runtime",
            team_id="team-runtime",
            role="leader",
            host_owner_coordinator_id="superleader:obj-runtime",
            runtime_task_id="runtime-task-1",
            metadata=metadata,
        )
        await session_host.record_coordinator_session_state(
            leader_session_id,
            coordinator_session=ResidentCoordinatorSession(
                coordinator_id="leader:runtime",
                role="leader",
                phase=ResidentCoordinatorPhase.QUIESCENT,
                objective_id="obj-runtime",
                lane_id="lane-runtime",
                team_id="team-runtime",
                cycle_count=2,
                prompt_turn_count=1,
                claimed_task_count=0,
                subordinate_dispatch_count=0,
                mailbox_poll_count=3,
                mailbox_cursor="leader-envelope-quiescent",
                last_reason="Resident shell is quiescent.",
            ),
            last_progress_at="2026-04-11T12:00:00+00:00",
        )

        payload = await self.app.session_wake(work_session_id=work_session["work_session_id"])

        self.assertEqual(payload["command"], "session.wake")
        self.assertEqual(payload["result"]["action"], "woken")
        self.assertEqual(
            payload["result"]["decision"]["mode"],
            ShellAttachDecisionMode.WOKEN.value,
        )
        self.assertEqual(
            payload["result"]["metadata"]["wake_requested_session_ids"],
            [leader_session_id],
        )


class CliMainTest(TestCase):
    def test_main_session_list_requires_durable_storage_config(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = main(["session", "list", "--group-id", "group-a"])
        self.assertEqual(exit_code, 1)
        payload = json.loads(output.getvalue())
        self.assertIn("dsn", payload["error"].lower())

    def test_main_session_list_prints_real_payload_when_in_memory_is_explicit(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = main(
                [
                    "session",
                    "list",
                    "--store-backend",
                    "in-memory",
                    "--group-id",
                    "group-a",
                ]
            )
        self.assertEqual(exit_code, 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["command"], "session.list")
        self.assertEqual(payload["sessions"], [])

    def test_module_execution_prints_schema(self) -> None:
        env = dict(os.environ)
        env["PYTHONPATH"] = str(SRC)
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "agent_orchestra.cli.main",
                "schema",
                "--schema",
                "demo_runtime",
            ],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("CREATE SCHEMA IF NOT EXISTS demo_runtime;", result.stdout)
