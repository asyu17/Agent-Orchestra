from __future__ import annotations

import asyncio
import json
import shutil
import sys
import tempfile
from pathlib import Path
from unittest import IsolatedAsyncioTestCase

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agent_orchestra.contracts.delivery import DeliveryState, DeliveryStateKind, DeliveryStatus
from agent_orchestra.contracts.authority import AuthorityDecision, ScopeExtensionRequest
from agent_orchestra.contracts.enums import EventKind, TaskScope, WorkerStatus
from agent_orchestra.contracts.execution import WorkerRecord
from agent_orchestra.contracts.runner import AgentRunner, RunnerHealth, RunnerStreamEvent, RunnerTurnRequest, RunnerTurnResult
from agent_orchestra.contracts.worker_protocol import WorkerRoleProfile
from agent_orchestra.runtime import build_in_memory_orchestra
from agent_orchestra.runtime.backends import CodexCliLaunchBackend, InProcessLaunchBackend
from agent_orchestra.runtime.superleader import (
    SuperLeaderConfig,
    SuperLeaderCoordinationState,
    SuperLeaderRunResult,
    SuperLeaderRuntime,
)
from agent_orchestra.runtime.bootstrap_round import materialize_planning_result
from agent_orchestra.runtime.leader_loop import LeaderLoopResult
from agent_orchestra.planning.template_planner import TemplatePlanner
from agent_orchestra.runtime.group_runtime import GroupRuntime
from agent_orchestra.planning.template import ObjectiveTemplate, WorkstreamTemplate
from agent_orchestra.self_hosting.bootstrap import (
    SelfHostingBootstrapConfig,
    SelfHostingBootstrapCoordinator,
    build_self_hosting_superleader_config,
    load_runtime_gap_inventory,
    render_self_hosting_instruction_packet,
)


class _ScriptedSelfHostingRunner(AgentRunner):
    def __init__(self) -> None:
        self.requests: list[RunnerTurnRequest] = []

    async def run_turn(self, request: RunnerTurnRequest) -> RunnerTurnResult:
        self.requests.append(request)
        if request.agent_id.startswith("leader:"):
            turn_index = int(request.metadata.get("turn_index", 1))
            lane_id = request.agent_id.split("leader:", 1)[1]
            if turn_index == 1:
                output = {
                    "summary": f"{lane_id} created one implementation task.",
                    "sequential_slices": [
                        {
                            "slice_id": f"{lane_id}-implementation",
                            "title": f"{lane_id} task",
                            "goal": f"Implement {lane_id}.",
                            "reason": f"{lane_id} is required for the next self-hosting increment.",
                            "owned_paths": [f"src/{lane_id}.py"],
                            "verification_commands": ["python3 -c \"print('verification ok')\""],
                        }
                    ],
                    "parallel_slices": [],
                }
            else:
                output = {"summary": f"{lane_id} converged.", "sequential_slices": [], "parallel_slices": []}
            return RunnerTurnResult(
                response_id=f"resp-{request.agent_id}-{turn_index}",
                output_text=json.dumps(output),
                status="completed",
            )
        return RunnerTurnResult(
            response_id=f"resp-{request.agent_id}",
            output_text=f"{request.agent_id} completed {request.metadata.get('task_id')}",
            status="completed",
        )

    async def stream_turn(self, request: RunnerTurnRequest):
        if False:
            yield RunnerStreamEvent(kind=EventKind.RUNNER_COMPLETED)

    async def cancel(self, run_id: str) -> None:
        return None

    async def healthcheck(self) -> RunnerHealth:
        return RunnerHealth(healthy=True, provider="fake")


class _DelegationValidationRunner(AgentRunner):
    def __init__(self, *, honor_validation_contract: bool) -> None:
        self.requests: list[RunnerTurnRequest] = []
        self.honor_validation_contract = honor_validation_contract

    async def run_turn(self, request: RunnerTurnRequest) -> RunnerTurnResult:
        self.requests.append(request)
        if request.agent_id.startswith("leader:"):
            turn_index = int(request.metadata.get("turn_index", 1))
            lane_id = request.agent_id.split("leader:", 1)[1]
            host_owned_convergence_check = (
                "host-owned leader session converges without extra leader prompt turns"
                in request.instructions
            )
            validation_required = (
                "non-empty slices" in request.instructions
                and (
                    "leader consumes teammate mailbox result before completion"
                    in request.instructions
                    or host_owned_convergence_check
                )
            )
            if turn_index == 1 and self.honor_validation_contract and validation_required:
                output = {
                    "summary": f"{lane_id} forced one teammate validation task.",
                    "sequential_slices": [
                        {
                            "slice_id": f"{lane_id}-delegation-validation",
                            "title": f"{lane_id} validation task",
                            "goal": f"Validate {lane_id} delegation.",
                            "reason": "Need a concrete teammate task to prove the delegation chain.",
                            "owned_paths": [f"tests/{lane_id}.py"],
                            "verification_commands": ["python3 -c \"print('delegation ok')\""],
                        }
                    ],
                    "parallel_slices": [],
                }
            else:
                output = {"summary": f"{lane_id} converged.", "sequential_slices": [], "parallel_slices": []}
            return RunnerTurnResult(
                response_id=f"resp-{request.agent_id}-{turn_index}",
                output_text=json.dumps(output),
                status="completed",
            )
        return RunnerTurnResult(
            response_id=f"resp-{request.agent_id}",
            output_text=f"{request.agent_id} completed {request.metadata.get('task_id')}",
            status="completed",
        )

    async def stream_turn(self, request: RunnerTurnRequest):
        if False:
            yield RunnerStreamEvent(kind=EventKind.RUNNER_COMPLETED)

    async def cancel(self, run_id: str) -> None:
        return None

    async def healthcheck(self) -> RunnerHealth:
        return RunnerHealth(healthy=True, provider="fake")


class _ResidentClaimSelfHostingRunner(AgentRunner):
    def __init__(self) -> None:
        self.requests: list[RunnerTurnRequest] = []

    async def run_turn(self, request: RunnerTurnRequest) -> RunnerTurnResult:
        self.requests.append(request)
        if request.agent_id.startswith("leader:"):
            turn_index = int(request.metadata.get("turn_index", 1))
            lane_id = request.agent_id.split("leader:", 1)[1]
            if turn_index == 1:
                output = {
                    "summary": f"{lane_id} created overflow teammate work for resident autonomous claim.",
                    "parallel_slices": [
                        {
                            "parallel_group": f"{lane_id}-overflow",
                            "slices": [
                                {
                                    "slice_id": f"{lane_id}-overflow-{index}",
                                    "title": f"{lane_id} task {index}",
                                    "goal": f"Implement {lane_id} work item {index}.",
                                    "reason": "Need the resident teammate slot to keep claiming overflow team tasks.",
                                }
                                for index in range(1, 26)
                            ],
                        }
                    ],
                    "sequential_slices": [],
                }
            else:
                output = {
                    "summary": f"{lane_id} converged after resident teammate draining.",
                    "sequential_slices": [],
                    "parallel_slices": [],
                }
            return RunnerTurnResult(
                response_id=f"resp-{request.agent_id}-{turn_index}",
                output_text=json.dumps(output),
                status="completed",
            )
        return RunnerTurnResult(
            response_id=f"resp-{request.agent_id}",
            output_text=f"{request.agent_id} completed {request.metadata.get('task_id')}",
            status="completed",
        )

    async def stream_turn(self, request: RunnerTurnRequest):
        if False:
            yield RunnerStreamEvent(kind=EventKind.RUNNER_COMPLETED)

    async def cancel(self, run_id: str) -> None:
        return None

    async def healthcheck(self) -> RunnerHealth:
        return RunnerHealth(healthy=True, provider="fake")


class _ConfigCapturingSuperLeader:
    def __init__(self, runtime) -> None:
        self._delegate = SuperLeaderRuntime(runtime=runtime)
        self.captured_config = None

    async def run_template(self, *, planner, template, config=None):
        self.captured_config = config
        safe_config = SuperLeaderConfig(
            leader_backend="in_process",
            teammate_backend="in_process",
            max_leader_turns=config.max_leader_turns if config is not None else None,
            auto_run_teammates=config.auto_run_teammates if config is not None else True,
            working_dir=config.working_dir if config is not None else None,
        )
        return await self._delegate.run_template(
            planner=planner,
            template=template,
            config=safe_config,
        )


class _ProtocolEvidenceInjectingSuperLeader:
    def __init__(self, runtime, *, evidence_by_lane: dict[str, tuple[dict[str, object], ...]]) -> None:
        self._delegate = SuperLeaderRuntime(runtime=runtime)
        self._evidence_by_lane = {
            lane_id: tuple(dict(item) for item in events)
            for lane_id, events in evidence_by_lane.items()
        }

    async def run_template(self, *, planner, template, config=None):
        result = await self._delegate.run_template(planner=planner, template=template, config=config)
        for lane_result in result.lane_results:
            lane_events = self._evidence_by_lane.get(lane_result.leader_round.lane_id)
            if not lane_events:
                continue
            for record in lane_result.leader_records:
                existing = record.metadata.get("protocol_bus_events")
                if not isinstance(existing, list):
                    existing = []
                    record.metadata["protocol_bus_events"] = existing
                existing.extend(dict(item) for item in lane_events)
        return result


class _StaticSuperLeader:
    def __init__(self, result) -> None:
        self._result = result

    async def run_template(self, *, planner, template, config=None):
        return self._result


class SelfHostingRoundTest(IsolatedAsyncioTestCase):
    async def _cleanup_tempdir(self, path: Path) -> None:
        await asyncio.sleep(0.2)
        for _ in range(5):
            try:
                shutil.rmtree(path)
                return
            except FileNotFoundError:
                return
            except OSError:
                await asyncio.sleep(0.1)
        shutil.rmtree(path, ignore_errors=True)

    def _write_fake_structured_codex_script(self, path: Path) -> None:
        path.write_text(
            "\n".join(
                [
                    "import json",
                    "import sys",
                    "from pathlib import Path",
                    "",
                    "def _value(flag: str):",
                    "    if flag not in sys.argv:",
                    "        return None",
                    "    index = sys.argv.index(flag)",
                    "    if index + 1 >= len(sys.argv):",
                    "        return None",
                    "    return sys.argv[index + 1]",
                    "",
                    "last_message = _value('--output-last-message')",
                    "payload = {",
                    "    'summary': 'codex self-hosting lane converged.',",
                    "    'sequential_slices': [],",
                    "    'parallel_slices': [],",
                    "}",
                    "Path(last_message).write_text(json.dumps(payload), encoding='utf-8')",
                    "sys.stdout.write(json.dumps({'type': 'thread.started', 'thread_id': 'thread-codex-self-host'}) + '\\n')",
                    "sys.stdout.write(json.dumps({'type': 'turn.completed'}) + '\\n')",
                    "sys.stdout.flush()",
                    "raise SystemExit(0)",
                ]
            ),
            encoding="utf-8",
        )

    async def test_bootstrap_superleader_config_prefers_role_profiles_for_codex(self) -> None:
        config = SelfHostingBootstrapConfig(
            objective_id="obj-self-host-profile",
            group_id="group-self-host",
            leader_backend="codex_cli",
            teammate_backend="codex_cli",
            leader_idle_timeout_seconds=120.0,
            leader_hard_timeout_seconds=2400.0,
        )

        superleader_config = build_self_hosting_superleader_config(config)

        self.assertEqual(superleader_config.leader_profile_id, "leader_codex_cli_long_turn")
        self.assertEqual(superleader_config.teammate_profile_id, "teammate_codex_cli_code_edit")
        self.assertIsNotNone(superleader_config.role_profiles)
        leader_profile = superleader_config.role_profiles["leader_codex_cli_long_turn"]
        teammate_profile = superleader_config.role_profiles["teammate_codex_cli_code_edit"]
        self.assertIsInstance(leader_profile, WorkerRoleProfile)
        self.assertIsInstance(teammate_profile, WorkerRoleProfile)
        self.assertEqual(leader_profile.backend, "codex_cli")
        self.assertEqual(teammate_profile.backend, "codex_cli")
        leader_policy = leader_profile.to_execution_policy()
        teammate_policy = teammate_profile.to_execution_policy()
        self.assertEqual(leader_policy.max_attempts, 3)
        self.assertEqual(leader_policy.idle_timeout_seconds, 120.0)
        self.assertEqual(leader_policy.hard_timeout_seconds, 2400.0)
        self.assertEqual(leader_profile.lease_policy.renewal_timeout_seconds, 120.0)
        self.assertEqual(leader_profile.lease_policy.hard_deadline_seconds, 2400.0)
        self.assertEqual(leader_policy.backoff_seconds, 2.0)
        self.assertEqual(leader_policy.provider_unavailable_backoff_initial_seconds, 15.0)
        self.assertEqual(leader_policy.provider_unavailable_backoff_multiplier, 2.0)
        self.assertEqual(leader_policy.provider_unavailable_backoff_max_seconds, 120.0)
        self.assertEqual(leader_profile.fallback_provider_unavailable_backoff_initial_seconds, 15.0)
        self.assertEqual(leader_profile.fallback_provider_unavailable_backoff_multiplier, 2.0)
        self.assertEqual(leader_profile.fallback_provider_unavailable_backoff_max_seconds, 120.0)
        self.assertEqual(teammate_policy.max_attempts, 3)
        self.assertEqual(teammate_policy.idle_timeout_seconds, 120.0)
        self.assertEqual(teammate_policy.hard_timeout_seconds, 2400.0)
        self.assertEqual(teammate_profile.lease_policy.renewal_timeout_seconds, 120.0)
        self.assertEqual(teammate_profile.lease_policy.hard_deadline_seconds, 2400.0)
        self.assertEqual(teammate_policy.backoff_seconds, 2.0)
        self.assertEqual(teammate_policy.provider_unavailable_backoff_initial_seconds, 15.0)
        self.assertEqual(teammate_policy.provider_unavailable_backoff_multiplier, 2.0)
        self.assertEqual(teammate_policy.provider_unavailable_backoff_max_seconds, 120.0)
        self.assertEqual(teammate_profile.fallback_provider_unavailable_backoff_initial_seconds, 15.0)
        self.assertEqual(teammate_profile.fallback_provider_unavailable_backoff_multiplier, 2.0)
        self.assertEqual(teammate_profile.fallback_provider_unavailable_backoff_max_seconds, 120.0)
        self.assertIsNone(superleader_config.leader_execution_policy)
        self.assertTrue(superleader_config.allow_promptless_convergence)
        self.assertFalse(superleader_config.keep_leader_session_idle)

    async def test_bootstrap_instruction_packet_serializes_contract_owned_role_profiles(self) -> None:
        markdown = """
## 6. 建议优先级

1. authority root / reducer 集成，把 lane complete 继续推进成 authority / objective complete
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            knowledge_path = Path(tmpdir) / "implementation-status.md"
            knowledge_path.write_text(markdown, encoding="utf-8")

            orchestra = build_in_memory_orchestra(runner=_ScriptedSelfHostingRunner())
            runtime = orchestra.group_runtime()
            superleader = _ConfigCapturingSuperLeader(runtime)
            coordinator = SelfHostingBootstrapCoordinator(
                runtime=runtime,
                superleader=superleader,
            )

            report = await coordinator.run_bootstrap_round(
                SelfHostingBootstrapConfig(
                    objective_id="obj-self-host-serialized-profiles",
                    group_id="group-self-host",
                    max_workstreams=1,
                    knowledge_path=knowledge_path,
                    working_dir=tmpdir,
                    leader_backend="codex_cli",
                    teammate_backend="codex_cli",
                    leader_idle_timeout_seconds=90.0,
                    leader_hard_timeout_seconds=1800.0,
                )
            )

        self.assertIsNotNone(superleader.captured_config)
        self.assertTrue(superleader.captured_config.enable_planning_review)
        self.assertIn("planning_review_status", report.instruction_packet.metadata)
        role_profiles = report.instruction_packet.metadata["role_profiles"]
        leader_profile = role_profiles["leader_codex_cli_long_turn"]
        teammate_profile = role_profiles["teammate_codex_cli_code_edit"]
        self.assertEqual(leader_profile["backend"], "codex_cli")
        self.assertEqual(
            leader_profile["execution_contract"]["mode"],
            "leader_coordination",
        )
        self.assertEqual(
            teammate_profile["execution_contract"]["mode"],
            "teammate_code_edit",
        )
        self.assertEqual(leader_profile["lease_policy"]["accept_deadline_seconds"], 30.0)
        self.assertEqual(leader_profile["lease_policy"]["renewal_timeout_seconds"], 90.0)
        self.assertEqual(leader_profile["lease_policy"]["hard_deadline_seconds"], 1800.0)
        self.assertEqual(leader_profile["fallback_idle_timeout_seconds"], 90.0)
        self.assertEqual(leader_profile["fallback_hard_timeout_seconds"], 1800.0)
        self.assertEqual(leader_profile["fallback_provider_unavailable_backoff_initial_seconds"], 15.0)
        self.assertEqual(leader_profile["fallback_provider_unavailable_backoff_multiplier"], 2.0)
        self.assertEqual(leader_profile["fallback_provider_unavailable_backoff_max_seconds"], 120.0)
        self.assertEqual(teammate_profile["lease_policy"]["renewal_timeout_seconds"], 90.0)
        self.assertEqual(teammate_profile["lease_policy"]["hard_deadline_seconds"], 1800.0)
        self.assertEqual(teammate_profile["fallback_idle_timeout_seconds"], 90.0)
        self.assertEqual(teammate_profile["fallback_hard_timeout_seconds"], 1800.0)
        self.assertEqual(teammate_profile["fallback_provider_unavailable_backoff_initial_seconds"], 15.0)
        self.assertEqual(teammate_profile["fallback_provider_unavailable_backoff_multiplier"], 2.0)
        self.assertEqual(teammate_profile["fallback_provider_unavailable_backoff_max_seconds"], 120.0)

    async def test_bootstrap_coordinator_runs_round_and_recommends_next_template(self) -> None:
        markdown = """
## 6. 建议优先级

1. authority root / reducer 集成，把 lane complete 继续推进成 authority / objective complete
2. PostgreSQL 的正式 CRUD persistence
3. typed ProtocolBus / Redis mailbox 主线路由
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            knowledge_path = Path(tmpdir) / "implementation-status.md"
            knowledge_path.write_text(markdown, encoding="utf-8")

            orchestra = build_in_memory_orchestra(runner=_ScriptedSelfHostingRunner())
            runtime = orchestra.group_runtime()
            coordinator = SelfHostingBootstrapCoordinator(runtime=runtime)

            report = await coordinator.run_bootstrap_round(
                SelfHostingBootstrapConfig(
                    objective_id="obj-self-host",
                    group_id="group-self-host",
                    max_workstreams=2,
                    knowledge_path=knowledge_path,
                    working_dir=tmpdir,
                )
            )

        self.assertEqual(report.run_result.objective_state.status.value, "completed")
        self.assertEqual(report.instruction_packet.selected_gap_ids, ("authority-integration", "postgres-persistence"))
        self.assertEqual(report.instruction_packet.completed_gap_ids, ("postgres-persistence",))
        self.assertEqual(
            report.instruction_packet.next_round_gap_ids,
            ("authority-integration", "protocol-bus"),
        )
        self.assertEqual(
            report.instruction_packet.metadata.get("validation_failed_gap_ids"),
            ["authority-integration"],
        )
        self.assertIsNotNone(report.next_template)
        self.assertEqual(
            [item.workstream_id for item in report.next_template.workstreams],
            ["authority-integration", "protocol-bus"],
        )
        self.assertEqual(len(report.instruction_packet.lane_instructions), 2)

    async def test_load_runtime_gap_inventory_catalogs_first_batch_gaps_with_knowledge_scope(self) -> None:
        markdown = """
## 6. 建议优先级

1. team-primary-semantics-switch
2. coordination-transaction-and-session-truth-convergence
3. task-surface-authority-contract
4. superleader-isomorphic-runtime
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            knowledge_path = Path(tmpdir) / "implementation-status.md"
            knowledge_path.write_text(markdown, encoding="utf-8")

            inventory = load_runtime_gap_inventory(knowledge_path)

        gap_by_id = {item.gap_id: item for item in inventory}
        self.assertEqual(
            tuple(gap_by_id.keys()),
            (
                "team-primary-semantics-switch",
                "coordination-transaction-and-session-truth-convergence",
                "task-surface-authority-contract",
                "superleader-isomorphic-runtime",
            ),
        )
        coordination_gap = gap_by_id["coordination-transaction-and-session-truth-convergence"]
        self.assertIn(
            "resource/knowledge/agent-orchestra-runtime/implementation-status.md",
            coordination_gap.owned_paths,
        )
        self.assertIn(
            "resource/knowledge/agent-orchestra-runtime/first-batch-online-collaboration-execution-pack.md",
            coordination_gap.owned_paths,
        )
        self.assertIn(
            "src/agent_orchestra/runtime/group_runtime.py",
            coordination_gap.owned_paths,
        )
        self.assertIn(
            "python3 -m unittest tests.test_runtime -v",
            coordination_gap.verification_commands,
        )

    async def test_bootstrap_coordinator_runs_real_codex_backend_with_protocol_contract(self) -> None:
        markdown = """
## 6. 建议优先级

1. authority root / reducer 集成，把 lane complete 继续推进成 authority / objective complete
"""
        tmpdir = Path(tempfile.mkdtemp())
        try:
            knowledge_path = tmpdir / "implementation-status.md"
            knowledge_path.write_text(markdown, encoding="utf-8")
            fake_codex = tmpdir / "fake_codex_structured.py"
            self._write_fake_structured_codex_script(fake_codex)
            backends = {
                "in_process": InProcessLaunchBackend(),
                "codex_cli": CodexCliLaunchBackend(
                    codex_command=(sys.executable, str(fake_codex)),
                    spool_root=str(tmpdir),
                    bypass_approvals=False,
                    sandbox_mode="workspace-write",
                ),
            }
            orchestra = build_in_memory_orchestra(launch_backends=backends)
            runtime = orchestra.group_runtime()
            coordinator = SelfHostingBootstrapCoordinator(runtime=runtime)

            report = await coordinator.run_bootstrap_round(
                SelfHostingBootstrapConfig(
                    objective_id="obj-self-host-codex-real",
                    group_id="group-self-host-codex-real",
                    max_workstreams=1,
                    knowledge_path=knowledge_path,
                    working_dir=str(tmpdir),
                    leader_backend="codex_cli",
                    teammate_backend="in_process",
                    auto_run_teammates=False,
                )
            )

            self.assertEqual(report.run_result.objective_state.status.value, "completed")
            lane_result = report.run_result.lane_results[0]
            self.assertEqual(lane_result.delivery_state.status.value, "completed")
            worker_records = await runtime.store.list_worker_records()
            leader_records = [
                record
                for record in worker_records
                if record.role == "leader" and record.backend == "codex_cli"
            ]
            self.assertTrue(leader_records)
            self.assertTrue(
                any(record.metadata.get("protocol_wait_mode") == "native" for record in leader_records)
            )
            self.assertTrue(
                any(
                    record.metadata.get("final_report", {}).get("terminal_status") == "completed"
                    for record in leader_records
                )
            )
        finally:
            await self._cleanup_tempdir(tmpdir)

    async def test_bootstrap_coordinator_keeps_protocol_recovery_gaps_open_without_evidence(self) -> None:
        markdown = """
## 6. 建议优先级

1. durable supervisor sessions
2. identityscope / reconnector 的进程级恢复
3. typed ProtocolBus / Redis mailbox 主线路由
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            knowledge_path = Path(tmpdir) / "implementation-status.md"
            knowledge_path.write_text(markdown, encoding="utf-8")

            orchestra = build_in_memory_orchestra(runner=_ScriptedSelfHostingRunner())
            runtime = orchestra.group_runtime()
            coordinator = SelfHostingBootstrapCoordinator(runtime=runtime)

            report = await coordinator.run_bootstrap_round(
                SelfHostingBootstrapConfig(
                    objective_id="obj-self-host-protocol-recovery-missing",
                    group_id="group-self-host-protocol-recovery",
                    max_workstreams=3,
                    knowledge_path=knowledge_path,
                    working_dir=tmpdir,
                )
            )

        lane_by_gap = {lane.gap_id: lane for lane in report.instruction_packet.lane_instructions}

        self.assertEqual(
            report.instruction_packet.selected_gap_ids,
            ("durable-supervisor-sessions", "reconnector", "protocol-bus"),
        )
        self.assertEqual(report.instruction_packet.completed_gap_ids, ())
        self.assertEqual(
            report.instruction_packet.remaining_gap_ids,
            ("durable-supervisor-sessions", "reconnector", "protocol-bus"),
        )
        self.assertEqual(
            report.instruction_packet.metadata["validation_failed_gap_ids"],
            ["durable-supervisor-sessions", "reconnector", "protocol-bus"],
        )
        self.assertFalse(lane_by_gap["durable-supervisor-sessions"].metadata["durable_supervisor_validation"]["validated"])
        self.assertFalse(lane_by_gap["reconnector"].metadata["reconnector_validation"]["validated"])
        self.assertFalse(lane_by_gap["protocol-bus"].metadata["protocol_bus_validation"]["validated"])

    async def test_bootstrap_coordinator_closes_protocol_recovery_gaps_with_evidence(self) -> None:
        markdown = """
## 6. 建议优先级

1. durable supervisor sessions
2. identityscope / reconnector 的进程级恢复
3. typed ProtocolBus / Redis mailbox 主线路由
"""
        protocol_bus_evidence = (
            {
                "event_id": "evt-lifecycle",
                "stream": "lifecycle",
                "event_type": "worker.accepted",
                "cursor": {"stream": "lifecycle", "offset": "1-0"},
            },
            {
                "event_id": "evt-session",
                "stream": "session",
                "event_type": "session.active",
                "cursor": {"stream": "session", "offset": "2-0"},
            },
            {
                "event_id": "evt-control",
                "stream": "control",
                "event_type": "control.verify",
                "cursor": {"stream": "control", "offset": "3-0"},
            },
            {
                "event_id": "evt-takeover",
                "stream": "takeover",
                "event_type": "session.takeover_completed",
                "payload": {"reattach": True},
                "cursor": {"stream": "takeover", "offset": "4-0"},
            },
            {
                "event_id": "evt-mailbox",
                "stream": "mailbox",
                "event_type": "mailbox.enqueued",
                "cursor": {"stream": "mailbox", "offset": "5-0"},
            },
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            knowledge_path = Path(tmpdir) / "implementation-status.md"
            knowledge_path.write_text(markdown, encoding="utf-8")

            orchestra = build_in_memory_orchestra(runner=_ScriptedSelfHostingRunner())
            runtime = orchestra.group_runtime()
            superleader = _ProtocolEvidenceInjectingSuperLeader(
                runtime,
                evidence_by_lane={
                    "durable-supervisor-sessions": protocol_bus_evidence,
                    "reconnector": protocol_bus_evidence,
                    "protocol-bus": protocol_bus_evidence,
                },
            )
            coordinator = SelfHostingBootstrapCoordinator(runtime=runtime, superleader=superleader)

            report = await coordinator.run_bootstrap_round(
                SelfHostingBootstrapConfig(
                    objective_id="obj-self-host-protocol-recovery-evidence",
                    group_id="group-self-host-protocol-recovery",
                    max_workstreams=3,
                    knowledge_path=knowledge_path,
                    working_dir=tmpdir,
                )
            )

        lane_by_gap = {lane.gap_id: lane for lane in report.instruction_packet.lane_instructions}

        self.assertEqual(
            report.instruction_packet.completed_gap_ids,
            ("durable-supervisor-sessions", "reconnector", "protocol-bus"),
        )
        self.assertEqual(report.instruction_packet.remaining_gap_ids, ())
        self.assertTrue(
            lane_by_gap["durable-supervisor-sessions"].metadata["durable_supervisor_validation"]["validated"]
        )
        self.assertTrue(lane_by_gap["reconnector"].metadata["reconnector_validation"]["validated"])
        self.assertTrue(lane_by_gap["protocol-bus"].metadata["protocol_bus_validation"]["validated"])

    async def test_bootstrap_coordinator_can_target_explicit_gap_ids(self) -> None:
        markdown = """
## 6. 建议优先级

1. authority root / reducer 集成，把 lane complete 继续推进成 authority / objective complete
2. PostgreSQL 的正式 CRUD persistence
3. tool-capable code-edit worker，允许 worker 在受控 owned_paths 内完成代码编辑与验证
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            knowledge_path = Path(tmpdir) / "implementation-status.md"
            knowledge_path.write_text(markdown, encoding="utf-8")

            orchestra = build_in_memory_orchestra(runner=_ScriptedSelfHostingRunner())
            runtime = orchestra.group_runtime()
            coordinator = SelfHostingBootstrapCoordinator(runtime=runtime)

            report = await coordinator.run_bootstrap_round(
                SelfHostingBootstrapConfig(
                    objective_id="obj-self-host-targeted",
                    group_id="group-self-host",
                    max_workstreams=2,
                    preferred_gap_ids=("tool-capable-code-edit-worker", "authority-integration"),
                    knowledge_path=knowledge_path,
                    working_dir=tmpdir,
                )
            )

        self.assertEqual(
            report.instruction_packet.selected_gap_ids,
            ("tool-capable-code-edit-worker", "authority-integration"),
        )

    async def test_bootstrap_coordinator_can_run_dynamic_planning_mode(self) -> None:
        markdown = """
## 6. 建议优先级

1. authority root / reducer 集成，把 lane complete 继续推进成 authority / objective complete
2. PostgreSQL 的正式 CRUD persistence
3. typed ProtocolBus / Redis mailbox 主线路由
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            knowledge_path = Path(tmpdir) / "implementation-status.md"
            knowledge_path.write_text(markdown, encoding="utf-8")

            orchestra = build_in_memory_orchestra(runner=_ScriptedSelfHostingRunner())
            runtime = orchestra.group_runtime()
            coordinator = SelfHostingBootstrapCoordinator(runtime=runtime)

            report = await coordinator.run_bootstrap_round(
                SelfHostingBootstrapConfig(
                    objective_id="obj-self-host-dynamic",
                    group_id="group-self-host",
                    max_workstreams=2,
                    knowledge_path=knowledge_path,
                    working_dir=tmpdir,
                    use_dynamic_planning=True,
                )
            )

        self.assertEqual(report.run_result.objective_state.status.value, "completed")
        self.assertEqual(report.template.workstreams, ())
        self.assertEqual(report.template.metadata["planning_mode"], "dynamic_superleader")
        self.assertEqual(
            report.instruction_packet.selected_gap_ids,
            ("authority-integration", "postgres-persistence"),
        )
        self.assertEqual(len(report.run_result.round_bundle.leader_rounds), 2)

    async def test_bootstrap_coordinator_drains_team_parallel_gap_with_single_resident_slot(self) -> None:
        markdown = """
## 6. 建议优先级

1. team parallel execution toward resident/subscription/autonomous claim
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            knowledge_path = Path(tmpdir) / "implementation-status.md"
            knowledge_path.write_text(markdown, encoding="utf-8")

            runner = _ResidentClaimSelfHostingRunner()
            orchestra = build_in_memory_orchestra(runner=runner)
            runtime = orchestra.group_runtime()
            coordinator = SelfHostingBootstrapCoordinator(runtime=runtime)

            report = await coordinator.run_bootstrap_round(
                SelfHostingBootstrapConfig(
                    objective_id="obj-self-host-team-parallel",
                    group_id="group-self-host-team-parallel",
                    max_workstreams=1,
                    knowledge_path=knowledge_path,
                    working_dir=tmpdir,
                    max_leader_turns=1,
                    keep_teammate_session_idle=True,
                )
            )

        lane_result = report.run_result.lane_results[0]
        leader_requests = [request for request in runner.requests if request.agent_id.startswith("leader:")]
        planning_phase_requests = [
            request for request in leader_requests if request.metadata.get("planning_review_phase")
        ]
        activation_prompt_requests = [
            request for request in leader_requests if request.metadata.get("planning_review_phase") is None
        ]
        teammate_requests = [request for request in runner.requests if ":teammate:" in request.agent_id]
        lane = report.instruction_packet.lane_instructions[0]
        validation = lane.metadata["team_parallel_validation"]

        self.assertEqual(report.instruction_packet.selected_gap_ids, ("team-parallel-execution",))
        self.assertEqual(report.run_result.objective_state.status.value, "completed")
        self.assertEqual(report.instruction_packet.completed_gap_ids, ("team-parallel-execution",))
        self.assertEqual(lane_result.delivery_state.status.value, "completed")
        self.assertEqual(len(planning_phase_requests), 3)
        self.assertEqual(len(activation_prompt_requests), 0)
        self.assertGreater(len(teammate_requests), 20)
        self.assertEqual(len(lane_result.teammate_records), len(teammate_requests))
        self.assertEqual(len(lane_result.turns), 1)
        self.assertEqual(len(lane_result.turns[0].created_task_ids), len(teammate_requests))
        self.assertEqual(len(lane_result.turns[0].produced_mailbox_ids), len(teammate_requests))
        self.assertTrue(validation["validated"])
        self.assertTrue(validation["host_owned_leader_session"])
        self.assertTrue(validation["convergence_without_extra_leader_turns"])
        self.assertGreater(validation["autonomous_claim_task_count"], 0)
        self.assertGreater(validation["resident_session_task_count"], 0)
        self.assertTrue(validation["teammate_execution_evidence"])
        self.assertTrue(validation["runtime_native_autonomous_claim_task_ids"])

    async def test_bootstrap_coordinator_keeps_team_parallel_gap_open_without_autonomous_claim_evidence(self) -> None:
        markdown = """
## 6. 建议优先级

1. team parallel execution toward resident/subscription/autonomous claim
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            knowledge_path = Path(tmpdir) / "implementation-status.md"
            knowledge_path.write_text(markdown, encoding="utf-8")

            runner = _ScriptedSelfHostingRunner()
            orchestra = build_in_memory_orchestra(runner=runner)
            runtime = orchestra.group_runtime()
            coordinator = SelfHostingBootstrapCoordinator(runtime=runtime)

            report = await coordinator.run_bootstrap_round(
                SelfHostingBootstrapConfig(
                    objective_id="obj-self-host-team-parallel-baseline-only",
                    group_id="group-self-host-team-parallel",
                    max_workstreams=1,
                    knowledge_path=knowledge_path,
                    working_dir=tmpdir,
                    max_leader_turns=2,
                    keep_teammate_session_idle=True,
                )
            )

        lane = report.instruction_packet.lane_instructions[0]
        validation = lane.metadata["team_parallel_validation"]

        self.assertEqual(report.instruction_packet.selected_gap_ids, ("team-parallel-execution",))
        self.assertEqual(report.run_result.objective_state.status.value, "completed")
        self.assertEqual(report.instruction_packet.completed_gap_ids, ())
        self.assertEqual(report.instruction_packet.remaining_gap_ids, ("team-parallel-execution",))
        self.assertEqual(report.instruction_packet.next_round_gap_ids, ("team-parallel-execution",))
        self.assertFalse(validation["validated"])
        self.assertEqual(validation["resident_session_task_count"], 0)
        self.assertFalse(validation["teammate_execution_evidence"])
        self.assertEqual(validation["runtime_native_autonomous_claim_task_ids"], ())

    async def test_bootstrap_coordinator_keeps_delegation_validation_gap_open_without_real_teammate_work(self) -> None:
        markdown = """
## 6. 建议优先级

1. 强验证 leader -> teammate delegation
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            knowledge_path = Path(tmpdir) / "implementation-status.md"
            knowledge_path.write_text(markdown, encoding="utf-8")

            orchestra = build_in_memory_orchestra(
                runner=_DelegationValidationRunner(honor_validation_contract=False)
            )
            runtime = orchestra.group_runtime()
            coordinator = SelfHostingBootstrapCoordinator(runtime=runtime)

            report = await coordinator.run_bootstrap_round(
                SelfHostingBootstrapConfig(
                    objective_id="obj-self-host-validation-miss",
                    group_id="group-self-host",
                    max_workstreams=1,
                    knowledge_path=knowledge_path,
                    working_dir=tmpdir,
                )
            )

        lane = report.instruction_packet.lane_instructions[0]
        validation = lane.metadata["delegation_validation"]

        self.assertEqual(report.instruction_packet.selected_gap_ids, ("leader-teammate-delegation-validation",))
        self.assertEqual(report.instruction_packet.completed_gap_ids, ())
        self.assertEqual(
            report.instruction_packet.remaining_gap_ids,
            ("leader-teammate-delegation-validation",),
        )
        self.assertEqual(
            report.instruction_packet.next_round_gap_ids,
            ("leader-teammate-delegation-validation",),
        )
        self.assertEqual(lane.tasks, ())
        self.assertFalse(validation["validated"])
        self.assertEqual(validation["created_task_count"], 0)
        self.assertEqual(validation["first_turn_created_task_ids"], ())
        self.assertEqual(validation["first_turn_produced_mailbox_ids"], ())
        self.assertEqual(validation["consumed_first_turn_mailbox_ids"], ())
        self.assertFalse(validation["teammate_execution_evidence"])

    async def test_bootstrap_coordinator_proves_delegation_chain_in_autonomous_round(self) -> None:
        markdown = """
## 6. 建议优先级

1. 强验证 leader -> teammate delegation
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            knowledge_path = Path(tmpdir) / "implementation-status.md"
            knowledge_path.write_text(markdown, encoding="utf-8")

            runner = _DelegationValidationRunner(honor_validation_contract=True)
            orchestra = build_in_memory_orchestra(runner=runner)
            runtime = orchestra.group_runtime()
            coordinator = SelfHostingBootstrapCoordinator(runtime=runtime)

            report = await coordinator.run_bootstrap_round(
                SelfHostingBootstrapConfig(
                    objective_id="obj-self-host-validation-pass",
                    group_id="group-self-host",
                    max_workstreams=1,
                    knowledge_path=knowledge_path,
                    working_dir=tmpdir,
                )
            )

        leader_requests = [request for request in runner.requests if request.agent_id.startswith("leader:")]
        planning_phase_requests = [
            request for request in leader_requests if request.metadata.get("planning_review_phase")
        ]
        activation_prompt_requests = [
            request for request in leader_requests if request.metadata.get("planning_review_phase") is None
        ]
        teammate_requests = [request for request in runner.requests if ":teammate:" in request.agent_id]
        lane = report.instruction_packet.lane_instructions[0]
        validation = lane.metadata["delegation_validation"]

        self.assertEqual(len(planning_phase_requests), 3)
        self.assertEqual(len(activation_prompt_requests), 0)
        self.assertEqual(len(teammate_requests), 1)
        self.assertIn("non-empty slices", planning_phase_requests[0].instructions)
        self.assertIn(
            "host-owned leader session converges without extra leader prompt turns",
            planning_phase_requests[0].instructions,
        )
        self.assertEqual(
            report.instruction_packet.completed_gap_ids,
            ("leader-teammate-delegation-validation",),
        )
        self.assertEqual(report.instruction_packet.remaining_gap_ids, ())
        self.assertEqual(len(lane.tasks), 1)
        self.assertTrue(validation["required"])
        self.assertTrue(validation["validated"])
        self.assertTrue(validation["host_owned_leader_session"])
        self.assertTrue(validation["convergence_without_extra_leader_turns"])
        self.assertEqual(validation["created_task_count"], 1)
        self.assertTrue(validation["runtime_native_teammate_execution_evidence"])
        self.assertEqual(validation["leader_turn_count"], 1)
        self.assertEqual(validation["mailbox_followup_turns_used"], 0)
        self.assertEqual(
            validation["first_turn_created_task_ids"],
            tuple(task.task_id for task in lane.tasks),
        )
        self.assertTrue(validation["first_turn_produced_mailbox_ids"])
        self.assertEqual(validation["completed_teammate_record_count"], 1)
        self.assertTrue(validation["teammate_execution_evidence"])

    async def test_bootstrap_coordinator_validates_delegation_without_mailbox_followup_turn(self) -> None:
        markdown = """
## 6. 建议优先级

1. 强验证 leader -> teammate delegation
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            knowledge_path = Path(tmpdir) / "implementation-status.md"
            knowledge_path.write_text(markdown, encoding="utf-8")

            runner = _DelegationValidationRunner(honor_validation_contract=True)
            orchestra = build_in_memory_orchestra(runner=runner)
            runtime = orchestra.group_runtime()
            coordinator = SelfHostingBootstrapCoordinator(runtime=runtime)

            report = await coordinator.run_bootstrap_round(
                SelfHostingBootstrapConfig(
                    objective_id="obj-self-host-validation-followup",
                    group_id="group-self-host",
                    max_workstreams=1,
                    knowledge_path=knowledge_path,
                    working_dir=tmpdir,
                    max_leader_turns=1,
                )
            )

        leader_requests = [request for request in runner.requests if request.agent_id.startswith("leader:")]
        planning_phase_requests = [
            request for request in leader_requests if request.metadata.get("planning_review_phase")
        ]
        activation_prompt_requests = [
            request for request in leader_requests if request.metadata.get("planning_review_phase") is None
        ]
        teammate_requests = [request for request in runner.requests if ":teammate:" in request.agent_id]
        lane = report.instruction_packet.lane_instructions[0]
        validation = lane.metadata["delegation_validation"]

        self.assertEqual(len(planning_phase_requests), 3)
        self.assertEqual(len(activation_prompt_requests), 0)
        self.assertEqual(len(teammate_requests), 1)
        self.assertEqual(
            report.instruction_packet.completed_gap_ids,
            ("leader-teammate-delegation-validation",),
        )
        self.assertTrue(validation["validated"])
        self.assertTrue(validation["host_owned_leader_session"])
        self.assertTrue(validation["convergence_without_extra_leader_turns"])
        self.assertEqual(validation["leader_turn_count"], 1)
        self.assertEqual(validation["mailbox_followup_turns_used"], 0)
        self.assertTrue(validation["first_turn_produced_mailbox_ids"])
        self.assertTrue(validation["teammate_execution_evidence"])

    async def test_self_hosting_lane_instruction_includes_authority_wait_metadata(self) -> None:
        orchestra = build_in_memory_orchestra(runner=_ScriptedSelfHostingRunner())
        runtime = orchestra.group_runtime()
        planner = TemplatePlanner()

        await runtime.create_group("group-a")
        planning_result = await planner.build_initial_plan(
            ObjectiveTemplate(
                objective_id="obj-self-host-auth",
                group_id="group-a",
                title="Authority waiting instruction",
                description="Expose authority waiting metadata in lane instructions.",
                workstreams=(
                    WorkstreamTemplate(
                        workstream_id="runtime",
                        title="Runtime",
                        summary="Detect waiting for authority.",
                        team_name="Runtime",
                    ),
                ),
            )
        )
        bundle = await materialize_planning_result(runtime, planning_result, created_by="self-host")
        leader_round = bundle.leader_rounds[0]
        lane_result = LeaderLoopResult(
            leader_round=leader_round,
            delivery_state=DeliveryState(
                delivery_id=f"{bundle.objective.objective_id}:lane:{leader_round.lane_id}",
                objective_id=bundle.objective.objective_id,
                kind=DeliveryStateKind.LANE,
                status=DeliveryStatus.WAITING_FOR_AUTHORITY,
                lane_id=leader_round.lane_id,
                team_id=leader_round.team_id,
                iteration=1,
                summary="Waiting for authority approval.",
                metadata={"waiting_for_authority_task_ids": ["task-wait"]},
            ),
            leader_records=(),
            teammate_records=(),
        )
        coordinator = SelfHostingBootstrapCoordinator(runtime=runtime)
        instruction = await coordinator._build_lane_instruction(
            leader_round.lane_id,
            lane_result,
            metadata={
                "authority_waiting": True,
                "authority_waiting_task_ids": ["task-wait"],
            },
        )

        self.assertTrue(instruction.metadata.get("authority_waiting"))
        self.assertEqual(instruction.metadata.get("authority_waiting_task_ids"), ["task-wait"])

    async def test_bootstrap_round_keeps_authority_gap_open_when_grant_relay_is_incomplete(self) -> None:
        orchestra = build_in_memory_orchestra(runner=_ScriptedSelfHostingRunner())
        runtime = orchestra.group_runtime()
        planner = TemplatePlanner()

        await runtime.create_group("group-a")
        planning_result = await planner.build_initial_plan(
            ObjectiveTemplate(
                objective_id="obj-self-host-auth-incomplete",
                group_id="group-a",
                title="Authority completion gate",
                description="Do not close authority gap from summary-only completion.",
                workstreams=(
                    WorkstreamTemplate(
                        workstream_id="authority-integration",
                        title="Authority",
                        summary="Keep authority gap open when grant relay is incomplete.",
                        team_name="Authority",
                    ),
                ),
            )
        )
        bundle = await materialize_planning_result(runtime, planning_result, created_by="self-host")
        leader_round = bundle.leader_rounds[0]
        blocked_task = await runtime.submit_task(
            group_id=bundle.objective.group_id,
            lane_id=leader_round.lane_id,
            team_id=leader_round.team_id,
            goal="Repair authority blocker",
            scope=TaskScope.TEAM,
            created_by=leader_round.leader_task.leader_id,
        )
        await runtime.store.save_delivery_state(
            DeliveryState(
                delivery_id=f"{bundle.objective.objective_id}:lane:{leader_round.lane_id}",
                objective_id=bundle.objective.objective_id,
                kind=DeliveryStateKind.LANE,
                status=DeliveryStatus.RUNNING,
                lane_id=leader_round.lane_id,
                team_id=leader_round.team_id,
                pending_task_ids=(blocked_task.task_id,),
                summary="Lane is running.",
            )
        )
        request = ScopeExtensionRequest(
            request_id=f"{blocked_task.task_id}:auth-request",
            assignment_id=f"{blocked_task.task_id}:assignment",
            worker_id=f"{leader_round.team_id}:teammate:1",
            task_id=blocked_task.task_id,
            requested_paths=("src/agent_orchestra/self_hosting/bootstrap.py",),
            reason="Need grant for protected path.",
            evidence="task cannot continue in current lane owner profile",
            retry_hint="grant and resume",
        )
        await runtime.commit_authority_request(
            objective_id=bundle.objective.objective_id,
            lane_id=leader_round.lane_id,
            team_id=leader_round.team_id,
            task_id=blocked_task.task_id,
            worker_id=request.worker_id,
            authority_request=request,
            record=WorkerRecord(
                worker_id=request.worker_id,
                assignment_id=request.assignment_id,
                backend="in_process",
                role="teammate",
                status=WorkerStatus.FAILED,
                metadata={"final_report": {"terminal_status": "blocked"}},
            ),
        )
        await runtime.commit_authority_decision(
            objective_id=bundle.objective.objective_id,
            lane_id=leader_round.lane_id,
            team_id=leader_round.team_id,
            task_id=blocked_task.task_id,
            actor_id=leader_round.leader_task.leader_id,
            authority_decision=AuthorityDecision(
                request_id=request.request_id,
                decision="grant",
                actor_id=leader_round.leader_task.leader_id,
                scope_class="soft_scope",
                reason="Grant for continuation.",
                summary="Granted authority task.",
            ),
        )
        lane_result = LeaderLoopResult(
            leader_round=leader_round,
            delivery_state=DeliveryState(
                delivery_id=f"{bundle.objective.objective_id}:lane:{leader_round.lane_id}",
                objective_id=bundle.objective.objective_id,
                kind=DeliveryStateKind.LANE,
                status=DeliveryStatus.COMPLETED,
                lane_id=leader_round.lane_id,
                team_id=leader_round.team_id,
                summary="Lane summary says completed.",
            ),
            leader_records=(),
            teammate_records=(),
        )
        run_result = SuperLeaderRunResult(
            round_bundle=bundle,
            lane_results=(lane_result,),
            coordination_state=SuperLeaderCoordinationState(
                coordinator_id=f"superleader:{bundle.objective.objective_id}",
                objective_id=bundle.objective.objective_id,
                max_active_lanes=1,
                batch_count=1,
                lane_states=(),
                pending_lane_ids=(),
                ready_lane_ids=(),
                active_lane_ids=(),
                completed_lane_ids=(leader_round.lane_id,),
                blocked_lane_ids=(),
                failed_lane_ids=(),
            ),
            objective_state=DeliveryState(
                delivery_id=bundle.objective.objective_id,
                objective_id=bundle.objective.objective_id,
                kind=DeliveryStateKind.OBJECTIVE,
                status=DeliveryStatus.COMPLETED,
                summary="Authority lane completed by summary.",
            ),
        )

        markdown = """
## 6. 建议优先级

1. authority root / reducer 集成，把 lane complete 继续推进成 authority / objective complete
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "implementation-status.md"
            path.write_text(markdown, encoding="utf-8")
            coordinator = SelfHostingBootstrapCoordinator(
                runtime=runtime,
                superleader=_StaticSuperLeader(run_result),
            )
            report = await coordinator.run_bootstrap_round(
                SelfHostingBootstrapConfig(
                    objective_id="obj-self-host-authority-incomplete",
                    group_id="group-a",
                    max_workstreams=1,
                    knowledge_path=path,
                )
            )

        lane = report.instruction_packet.lane_instructions[0]
        authority_completion = lane.metadata.get("authority_completion")
        self.assertIsInstance(authority_completion, dict)
        assert isinstance(authority_completion, dict)
        self.assertFalse(authority_completion["validated"])
        self.assertEqual(authority_completion["completion_status"], "incomplete")
        self.assertEqual(authority_completion["request_count"], 1)
        self.assertIn(request.request_id, authority_completion["incomplete_request_ids"])

        self.assertEqual(report.instruction_packet.completed_gap_ids, ())
        self.assertEqual(
            report.instruction_packet.metadata.get("validation_failed_gap_ids"),
            ["authority-integration"],
        )
        status = report.instruction_packet.metadata.get("authority_completion_status")
        self.assertIsInstance(status, dict)
        assert isinstance(status, dict)
        self.assertFalse(status["validated"])
        self.assertEqual(status["completion_status"], "incomplete")

    async def test_bootstrap_round_emits_authority_completion_evidence_from_runtime_state(self) -> None:
        orchestra = build_in_memory_orchestra(runner=_ScriptedSelfHostingRunner())
        runtime = orchestra.group_runtime()
        planner = TemplatePlanner()

        await runtime.create_group("group-a")
        planning_result = await planner.build_initial_plan(
            ObjectiveTemplate(
                objective_id="obj-self-host-auth-evidence",
                group_id="group-a",
                title="Authority mainline evidence",
                description="Derive authority decision/resume/reroute evidence from runtime state.",
                workstreams=(
                    WorkstreamTemplate(
                        workstream_id="authority-integration",
                        title="Authority",
                        summary="Track authority mainline evidence.",
                        team_name="Authority",
                    ),
                ),
            )
        )
        bundle = await materialize_planning_result(runtime, planning_result, created_by="self-host")
        leader_round = bundle.leader_rounds[0]
        blocked_task = await runtime.submit_task(
            group_id=bundle.objective.group_id,
            lane_id=leader_round.lane_id,
            team_id=leader_round.team_id,
            goal="Repair authority blocker",
            scope=TaskScope.TEAM,
            created_by=leader_round.leader_task.leader_id,
        )
        await runtime.store.save_delivery_state(
            DeliveryState(
                delivery_id=f"{bundle.objective.objective_id}:lane:{leader_round.lane_id}",
                objective_id=bundle.objective.objective_id,
                kind=DeliveryStateKind.LANE,
                status=DeliveryStatus.RUNNING,
                lane_id=leader_round.lane_id,
                team_id=leader_round.team_id,
                pending_task_ids=(blocked_task.task_id,),
                summary="Lane is running.",
            )
        )
        request = ScopeExtensionRequest(
            request_id=f"{blocked_task.task_id}:auth-request",
            assignment_id=f"{blocked_task.task_id}:assignment",
            worker_id=f"{leader_round.team_id}:teammate:1",
            task_id=blocked_task.task_id,
            requested_paths=("src/agent_orchestra/self_hosting/bootstrap.py",),
            reason="Need reroute target for protected path.",
            evidence="task cannot continue in current lane owner profile",
            retry_hint="reroute to dedicated repair slice",
        )
        await runtime.commit_authority_request(
            objective_id=bundle.objective.objective_id,
            lane_id=leader_round.lane_id,
            team_id=leader_round.team_id,
            task_id=blocked_task.task_id,
            worker_id=request.worker_id,
            authority_request=request,
            record=WorkerRecord(
                worker_id=request.worker_id,
                assignment_id=request.assignment_id,
                backend="in_process",
                role="teammate",
                status=WorkerStatus.FAILED,
                metadata={"final_report": {"terminal_status": "blocked"}},
            ),
        )
        replacement_task = await runtime.submit_task(
            group_id=bundle.objective.group_id,
            lane_id=leader_round.lane_id,
            team_id=leader_round.team_id,
            goal="Replacement authority repair task",
            scope=TaskScope.TEAM,
            created_by=leader_round.leader_task.leader_id,
        )
        decision_commit = await runtime.commit_authority_decision(
            objective_id=bundle.objective.objective_id,
            lane_id=leader_round.lane_id,
            team_id=leader_round.team_id,
            task_id=blocked_task.task_id,
            actor_id=leader_round.leader_task.leader_id,
            authority_decision=AuthorityDecision(
                request_id=request.request_id,
                decision="reroute",
                actor_id=leader_round.leader_task.leader_id,
                scope_class="soft_scope",
                reason="Reroute to replacement repair task.",
                summary="Rerouted authority task.",
            ),
            replacement_task=replacement_task,
        )
        lane_result = LeaderLoopResult(
            leader_round=leader_round,
            delivery_state=decision_commit.delivery_state
            if decision_commit.delivery_state is not None
            else DeliveryState(
                delivery_id=f"{bundle.objective.objective_id}:lane:{leader_round.lane_id}",
                objective_id=bundle.objective.objective_id,
                kind=DeliveryStateKind.LANE,
                status=DeliveryStatus.RUNNING,
                lane_id=leader_round.lane_id,
                team_id=leader_round.team_id,
                summary="Authority reroute is active.",
            ),
            leader_records=(),
            teammate_records=(),
        )
        run_result = SuperLeaderRunResult(
            round_bundle=bundle,
            lane_results=(lane_result,),
            coordination_state=SuperLeaderCoordinationState(
                coordinator_id=f"superleader:{bundle.objective.objective_id}",
                objective_id=bundle.objective.objective_id,
                max_active_lanes=1,
                batch_count=1,
                lane_states=(),
                pending_lane_ids=(),
                ready_lane_ids=(),
                active_lane_ids=(),
                completed_lane_ids=(),
                blocked_lane_ids=(),
                failed_lane_ids=(),
            ),
            objective_state=DeliveryState(
                delivery_id=bundle.objective.objective_id,
                objective_id=bundle.objective.objective_id,
                kind=DeliveryStateKind.OBJECTIVE,
                status=DeliveryStatus.RUNNING,
                summary="Authority evidence round running.",
            ),
        )

        markdown = """
## 6. 建议优先级

1. authority root / reducer 集成，把 lane complete 继续推进成 authority / objective complete
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "implementation-status.md"
            path.write_text(markdown, encoding="utf-8")
            coordinator = SelfHostingBootstrapCoordinator(
                runtime=runtime,
                superleader=_StaticSuperLeader(run_result),
            )
            report = await coordinator.run_bootstrap_round(
                SelfHostingBootstrapConfig(
                    objective_id="obj-self-host-authority",
                    group_id="group-a",
                    max_workstreams=1,
                    knowledge_path=path,
                )
            )

        lane = report.instruction_packet.lane_instructions[0]
        authority_completion = lane.metadata.get("authority_completion")
        self.assertIsInstance(authority_completion, dict)
        assert isinstance(authority_completion, dict)
        self.assertTrue(authority_completion["validated"])
        self.assertEqual(authority_completion["completion_status"], "validated")
        self.assertEqual(authority_completion["decision_counts"]["reroute"], 1)
        self.assertIn(request.request_id, authority_completion["closed_request_ids"])
        self.assertEqual(
            authority_completion["reroute_links"],
            [{"superseded_task_id": blocked_task.task_id, "replacement_task_id": replacement_task.task_id}],
        )
        self.assertIn("authority_completion_status", report.instruction_packet.metadata)
        status = report.instruction_packet.metadata["authority_completion_status"]
        self.assertTrue(status["validated"])
        self.assertEqual(status["completion_status"], "validated")

    async def test_bootstrap_round_enables_planning_review_gap_and_exports_activation_gate_status(self) -> None:
        orchestra = build_in_memory_orchestra(runner=_ScriptedSelfHostingRunner())
        runtime = orchestra.group_runtime()
        planner = TemplatePlanner()

        await runtime.create_group("group-a")
        planning_result = await planner.build_initial_plan(
            ObjectiveTemplate(
                objective_id="obj-self-host-planning-review",
                group_id="group-a",
                title="Planning review self-hosting",
                description="Surface planning review status into bootstrap packet.",
                workstreams=(
                    WorkstreamTemplate(
                        workstream_id="multi-leader-planning-review",
                        title="Planning review",
                        summary="Run draft/peer/revision plus activation gate.",
                        team_name="Planning",
                    ),
                ),
            )
        )
        bundle = await materialize_planning_result(runtime, planning_result, created_by="self-host")
        leader_round = bundle.leader_rounds[0]
        lane_result = LeaderLoopResult(
            leader_round=leader_round,
            delivery_state=DeliveryState(
                delivery_id=f"{bundle.objective.objective_id}:lane:{leader_round.lane_id}",
                objective_id=bundle.objective.objective_id,
                kind=DeliveryStateKind.LANE,
                status=DeliveryStatus.COMPLETED,
                lane_id=leader_round.lane_id,
                team_id=leader_round.team_id,
                summary="Planning review lane completed.",
            ),
            leader_records=(),
            teammate_records=(),
        )
        run_result = SuperLeaderRunResult(
            round_bundle=bundle,
            lane_results=(lane_result,),
            coordination_state=SuperLeaderCoordinationState(
                coordinator_id=f"superleader:{bundle.objective.objective_id}",
                objective_id=bundle.objective.objective_id,
                max_active_lanes=1,
                batch_count=1,
                lane_states=(),
                pending_lane_ids=(),
                ready_lane_ids=(),
                active_lane_ids=(),
                completed_lane_ids=(leader_round.lane_id,),
                blocked_lane_ids=(),
                failed_lane_ids=(),
            ),
            objective_state=DeliveryState(
                delivery_id=bundle.objective.objective_id,
                objective_id=bundle.objective.objective_id,
                kind=DeliveryStateKind.OBJECTIVE,
                status=DeliveryStatus.COMPLETED,
                summary="Planning review round completed.",
                metadata={
                    "planning_review": {
                        "enabled": True,
                        "planning_round_id": f"{bundle.objective.objective_id}:planning-round:1",
                        "draft_plan_count": 1,
                        "peer_review_count": 0,
                        "revised_plan_count": 1,
                        "activation_gate": {
                            "status": "ready_for_activation",
                            "summary": "All revised plans are ready for activation.",
                            "blockers": [],
                        },
                    },
                    "activation_gate": {
                        "status": "ready_for_activation",
                        "summary": "All revised plans are ready for activation.",
                        "blockers": [],
                    },
                },
            ),
        )

        class _CapturingStaticSuperLeader(_StaticSuperLeader):
            def __init__(self, result) -> None:
                super().__init__(result)
                self.last_config = None

            async def run_template(self, *, planner, template, config=None):
                self.last_config = config
                return await super().run_template(planner=planner, template=template, config=config)

        superleader = _CapturingStaticSuperLeader(run_result)

        markdown = """
## 6. 建议优先级

1. multi leader draft / peer review / revision / activation gate
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "implementation-status.md"
            path.write_text(markdown, encoding="utf-8")
            coordinator = SelfHostingBootstrapCoordinator(
                runtime=runtime,
                superleader=superleader,
            )
            report = await coordinator.run_bootstrap_round(
                SelfHostingBootstrapConfig(
                    objective_id="obj-self-host-planning-review",
                    group_id="group-a",
                    max_workstreams=1,
                    knowledge_path=path,
                )
            )

        self.assertIsNotNone(superleader.last_config)
        self.assertTrue(superleader.last_config.enable_planning_review)
        self.assertEqual(
            report.instruction_packet.selected_gap_ids,
            ("multi-leader-planning-review",),
        )
        self.assertEqual(
            report.instruction_packet.completed_gap_ids,
            ("multi-leader-planning-review",),
        )
        lane = report.instruction_packet.lane_instructions[0]
        planning_review = lane.metadata.get("planning_review")
        self.assertIsInstance(planning_review, dict)
        assert isinstance(planning_review, dict)
        self.assertEqual(planning_review["activation_gate"]["status"], "ready_for_activation")
        self.assertEqual(
            report.instruction_packet.metadata["planning_review_status"]["activation_gate"]["status"],
            "ready_for_activation",
        )

    async def test_bootstrap_round_exports_superleader_resident_runtime_status(self) -> None:
        orchestra = build_in_memory_orchestra(runner=_ScriptedSelfHostingRunner())
        runtime = orchestra.group_runtime()
        planner = TemplatePlanner()

        await runtime.create_group("group-a")
        planning_result = await planner.build_initial_plan(
            ObjectiveTemplate(
                objective_id="obj-self-host-superleader-runtime-status",
                group_id="group-a",
                title="Superleader runtime status export",
                description="Surface resident superleader live-view truth into the bootstrap packet.",
                workstreams=(
                    WorkstreamTemplate(
                        workstream_id="superleader-isomorphic-runtime",
                        title="Superleader runtime",
                        summary="Export resident live-view status.",
                        team_name="Runtime",
                    ),
                ),
            )
        )
        bundle = await materialize_planning_result(runtime, planning_result, created_by="self-host")
        leader_round = bundle.leader_rounds[0]
        lane_result = LeaderLoopResult(
            leader_round=leader_round,
            delivery_state=DeliveryState(
                delivery_id=f"{bundle.objective.objective_id}:lane:{leader_round.lane_id}",
                objective_id=bundle.objective.objective_id,
                kind=DeliveryStateKind.LANE,
                status=DeliveryStatus.RUNNING,
                lane_id=leader_round.lane_id,
                team_id=leader_round.team_id,
                summary="Resident runtime lane is still waiting on mailbox convergence.",
            ),
            leader_records=(),
            teammate_records=(),
        )
        run_result = SuperLeaderRunResult(
            round_bundle=bundle,
            lane_results=(lane_result,),
            coordination_state=SuperLeaderCoordinationState(
                coordinator_id=f"superleader:{bundle.objective.objective_id}",
                objective_id=bundle.objective.objective_id,
                max_active_lanes=1,
                batch_count=1,
                lane_states=(),
                pending_lane_ids=(),
                ready_lane_ids=(),
                active_lane_ids=(leader_round.lane_id,),
                completed_lane_ids=(),
                blocked_lane_ids=(),
                failed_lane_ids=(),
            ),
            objective_state=DeliveryState(
                delivery_id=bundle.objective.objective_id,
                objective_id=bundle.objective.objective_id,
                kind=DeliveryStateKind.OBJECTIVE,
                status=DeliveryStatus.RUNNING,
                summary="Resident live-view status exported.",
                metadata={
                    "coordination": {
                        "active_lane_ids": [leader_round.lane_id],
                        "pending_lane_ids": [],
                        "completed_lane_ids": [],
                    },
                    "message_runtime": {
                        "objective_shared_digest_count": 1,
                        "objective_shared_digest_envelope_ids": ["objective-digest-1"],
                    },
                    "resident_live_view": {
                        "lane_count": 1,
                        "host_owned_lane_session_count": 1,
                        "lane_digest_counts": {leader_round.lane_id: 2},
                        "lane_mailbox_followup_turns": {leader_round.lane_id: 1},
                        "lane_live_inputs": {
                            leader_round.lane_id: {
                                "delivery_status": DeliveryStatus.RUNNING.value,
                                "pending_shared_digest_count": 2,
                                "shared_digest_envelope_ids": ["lane-digest-1", "lane-digest-2"],
                                "mailbox_followup_turns_used": 1,
                                "mailbox_followup_turn_limit": 4,
                                "host_phase": "waiting_for_mailbox",
                            }
                        },
                    },
                },
            ),
        )

        markdown = """
## 6. 建议优先级

1. superleader isomorphic runtime
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "implementation-status.md"
            path.write_text(markdown, encoding="utf-8")
            coordinator = SelfHostingBootstrapCoordinator(
                runtime=runtime,
                superleader=_StaticSuperLeader(run_result),
            )
            report = await coordinator.run_bootstrap_round(
                SelfHostingBootstrapConfig(
                    objective_id="obj-self-host-superleader-runtime-status",
                    group_id="group-a",
                    max_workstreams=1,
                    knowledge_path=path,
                )
            )

        runtime_status = report.instruction_packet.metadata.get("superleader_runtime_status")
        self.assertIsInstance(runtime_status, dict)
        assert isinstance(runtime_status, dict)
        self.assertEqual(
            runtime_status["resident_live_view"]["lane_digest_counts"],
            {leader_round.lane_id: 2},
        )
        self.assertEqual(
            runtime_status["resident_live_view"]["lane_live_inputs"][leader_round.lane_id][
                "shared_digest_envelope_ids"
            ],
            ["lane-digest-1", "lane-digest-2"],
        )
        self.assertEqual(
            runtime_status["message_runtime"]["objective_shared_digest_count"],
            1,
        )
        self.assertEqual(
            runtime_status["coordination"]["active_lane_ids"],
            [leader_round.lane_id],
        )

    async def test_bootstrap_coordinator_prefers_resident_live_view_for_superleader_runtime_export(self) -> None:
        orchestra = build_in_memory_orchestra(runner=_ScriptedSelfHostingRunner())
        runtime = orchestra.group_runtime()
        planner = TemplatePlanner()
        await runtime.create_group("group-a")
        planning_result = await planner.build_initial_plan(
            ObjectiveTemplate(
                objective_id="obj-self-host-superleader-runtime-resident-preferred",
                group_id="group-a",
                title="Superleader runtime status export",
                description="Prefer resident live-view truth over stale scheduler exports.",
                workstreams=(
                    WorkstreamTemplate(
                        workstream_id="superleader-isomorphic-runtime",
                        title="Superleader runtime",
                        summary="Export resident live-view status.",
                        team_name="Runtime",
                    ),
                ),
            )
        )
        bundle = await materialize_planning_result(runtime, planning_result, created_by="self-host")
        leader_round = bundle.leader_rounds[0]
        lane_result = LeaderLoopResult(
            leader_round=leader_round,
            delivery_state=DeliveryState(
                delivery_id=f"{bundle.objective.objective_id}:lane:{leader_round.lane_id}",
                objective_id=bundle.objective.objective_id,
                kind=DeliveryStateKind.LANE,
                status=DeliveryStatus.PENDING,
                lane_id=leader_round.lane_id,
                team_id=leader_round.team_id,
                summary="Fallback scheduler snapshot still says the lane is pending.",
            ),
            leader_records=(),
            teammate_records=(),
        )
        run_result = SuperLeaderRunResult(
            round_bundle=bundle,
            lane_results=(lane_result,),
            coordination_state=SuperLeaderCoordinationState(
                coordinator_id=f"superleader:{bundle.objective.objective_id}",
                objective_id=bundle.objective.objective_id,
                max_active_lanes=1,
                batch_count=1,
                lane_states=(),
                pending_lane_ids=(leader_round.lane_id,),
                ready_lane_ids=(),
                active_lane_ids=(),
                completed_lane_ids=(),
                blocked_lane_ids=(),
                failed_lane_ids=(),
            ),
            objective_state=DeliveryState(
                delivery_id=bundle.objective.objective_id,
                objective_id=bundle.objective.objective_id,
                kind=DeliveryStateKind.OBJECTIVE,
                status=DeliveryStatus.RUNNING,
                summary="Resident live-view contract exported stronger runtime truth.",
                metadata={
                    "coordination": {
                        "active_lane_ids": [],
                        "pending_lane_ids": [leader_round.lane_id],
                        "completed_lane_ids": [],
                        "active_lane_session_ids": [],
                    },
                    "message_runtime": {
                        "objective_shared_digest_count": 0,
                        "objective_shared_digest_envelope_ids": [],
                    },
                    "resident_live_view": {
                        "lane_count": 1,
                        "host_owned_lane_session_count": 1,
                        "active_lane_ids": [leader_round.lane_id],
                        "pending_lane_ids": [],
                        "completed_lane_ids": [],
                        "failed_lane_ids": [],
                        "blocked_lane_ids": [],
                        "lane_statuses": {
                            leader_round.lane_id: DeliveryStatus.WAITING.value,
                        },
                        "active_lane_session_ids": ["leader:superleader-isomorphic-runtime"],
                        "objective_shared_digest_count": 2,
                        "objective_shared_digest_envelope_ids": [
                            "objective-digest-1",
                            "objective-digest-2",
                        ],
                        "lane_truth_sources": {
                            leader_round.lane_id: "coordinator_session",
                        },
                        "runtime_native_lane_ids": [leader_round.lane_id],
                        "fallback_lane_ids": [],
                        "primary_active_lane_ids": [leader_round.lane_id],
                        "primary_pending_lane_ids": [],
                        "primary_completed_lane_ids": [],
                        "primary_failed_lane_ids": [],
                        "primary_blocked_lane_ids": [],
                        "primary_active_lane_session_ids": [
                            "leader:superleader-isomorphic-runtime"
                        ],
                        "primary_lane_statuses": {
                            leader_round.lane_id: DeliveryStatus.WAITING.value,
                        },
                        "lane_digest_counts": {leader_round.lane_id: 2},
                        "lane_mailbox_followup_turns": {leader_round.lane_id: 1},
                        "lane_live_inputs": {
                            leader_round.lane_id: {
                                "delivery_status": DeliveryStatus.PENDING.value,
                                "effective_status": DeliveryStatus.WAITING.value,
                                "truth_source": "coordinator_session",
                                "pending_shared_digest_count": 2,
                                "shared_digest_envelope_ids": ["lane-digest-1", "lane-digest-2"],
                                "mailbox_followup_turns_used": 1,
                                "mailbox_followup_turn_limit": 4,
                                "host_phase": "waiting_for_mailbox",
                            }
                        },
                        "objective_message_runtime": {
                            "objective_shared_digest_count": 2,
                            "objective_shared_digest_envelope_ids": [
                                "objective-digest-1",
                                "objective-digest-2",
                            ],
                        },
                        "objective_coordination": {
                            "active_lane_ids": [leader_round.lane_id],
                            "pending_lane_ids": [],
                            "active_lane_session_ids": [
                                "leader:superleader-isomorphic-runtime"
                            ],
                        },
                    },
                },
            ),
        )

        markdown = """
## 6. 建议优先级

1. superleader isomorphic runtime
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "implementation-status.md"
            path.write_text(markdown, encoding="utf-8")
            coordinator = SelfHostingBootstrapCoordinator(
                runtime=runtime,
                superleader=_StaticSuperLeader(run_result),
            )
            report = await coordinator.run_bootstrap_round(
                SelfHostingBootstrapConfig(
                    objective_id="obj-self-host-superleader-runtime-resident-preferred",
                    group_id="group-a",
                    max_workstreams=1,
                    knowledge_path=path,
                )
            )

        runtime_status = report.instruction_packet.metadata.get("superleader_runtime_status")
        self.assertIsInstance(runtime_status, dict)
        assert isinstance(runtime_status, dict)
        self.assertEqual(runtime_status["resident_truth_source"], "resident_live_view")
        self.assertEqual(
            runtime_status["coordination"]["active_lane_ids"],
            [leader_round.lane_id],
        )
        self.assertEqual(runtime_status["coordination"]["pending_lane_ids"], [])
        self.assertEqual(
            runtime_status["coordination"]["active_lane_session_ids"],
            ["leader:superleader-isomorphic-runtime"],
        )
        self.assertEqual(
            runtime_status["message_runtime"]["objective_shared_digest_count"],
            2,
        )
        lane_runtime_status = report.instruction_packet.lane_instructions[0].metadata[
            "superleader_runtime_status"
        ]
        self.assertEqual(
            lane_runtime_status["coordination"]["active_lane_ids"],
            [leader_round.lane_id],
        )
        self.assertEqual(
            lane_runtime_status["message_runtime"]["objective_shared_digest_envelope_ids"],
            ["objective-digest-1", "objective-digest-2"],
        )
        rendered = render_self_hosting_instruction_packet(report.instruction_packet)
        self.assertIn("## SuperLeader Runtime", rendered)
        self.assertIn("resident_truth_source=resident_live_view", rendered)
        self.assertIn(leader_round.lane_id, rendered)
