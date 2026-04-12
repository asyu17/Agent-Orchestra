from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from unittest import TestCase

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agent_orchestra.contracts.worker_protocol import WorkerRoleProfile
from agent_orchestra.self_hosting.bootstrap import (
    SelfHostingGap,
    SelfHostingBootstrapConfig,
    build_self_hosting_superleader_config,
    build_self_hosting_template,
    load_runtime_gap_inventory,
)


class SelfHostingInventoryTest(TestCase):
    def test_load_runtime_gap_inventory_preserves_priority_order_from_knowledge(self) -> None:
        markdown = """
# status

## 6. 建议优先级

1. authority root / reducer 集成，把 lane complete 继续推进成 authority / objective complete
2. PostgreSQL 的正式 CRUD persistence
3. typed ProtocolBus / Redis mailbox 主线路由
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "implementation-status.md"
            path.write_text(markdown, encoding="utf-8")
            inventory = load_runtime_gap_inventory(path)

        self.assertEqual([item.priority for item in inventory], [1, 2, 3])
        self.assertEqual([item.gap_id for item in inventory], ["authority-integration", "postgres-persistence", "protocol-bus"])
        self.assertTrue(all(item.source_path.endswith("implementation-status.md") for item in inventory))

    def test_build_self_hosting_template_skips_completed_gaps_and_keeps_priority_order(self) -> None:
        markdown = """
## 6. 建议优先级

1. authority root / reducer 集成，把 lane complete 继续推进成 authority / objective complete
2. 多轮 LeaderSupervisor / TeammateSupervisor 与 worker idle/reactivate 语义
3. PostgreSQL 的正式 CRUD persistence
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "implementation-status.md"
            path.write_text(markdown, encoding="utf-8")
            inventory = load_runtime_gap_inventory(path)

        template = build_self_hosting_template(
            inventory=inventory,
            config=SelfHostingBootstrapConfig(
                objective_id="obj-self-host",
                group_id="group-self-host",
                max_workstreams=2,
                completed_gap_ids=("authority-integration",),
            ),
        )

        self.assertEqual(template.objective_id, "obj-self-host")
        self.assertEqual([item.workstream_id for item in template.workstreams], ["worker-lifecycle", "postgres-persistence"])
        self.assertIn("self-hosting", template.title.lower())

    def test_inventory_recognizes_tool_capable_code_edit_worker_gap(self) -> None:
        markdown = """
## 6. 建议优先级

1. tool-capable code-edit worker，允许 worker 在受控 owned_paths 内完成代码编辑与验证
2. authority root / reducer 集成，把 lane complete 继续推进成 authority / objective complete
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "implementation-status.md"
            path.write_text(markdown, encoding="utf-8")
            inventory = load_runtime_gap_inventory(path)

        self.assertEqual([item.gap_id for item in inventory], ["tool-capable-code-edit-worker", "authority-integration"])

    def test_inventory_recognizes_multi_leader_planning_review_gap(self) -> None:
        markdown = """
## 6. 建议优先级

1. multi leader draft / peer review / revision / activation gate
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "implementation-status.md"
            path.write_text(markdown, encoding="utf-8")
            inventory = load_runtime_gap_inventory(path)

        self.assertEqual(len(inventory), 1)
        planning_review_gap = inventory[0]
        self.assertEqual(planning_review_gap.gap_id, "multi-leader-planning-review")
        planning_text = " ".join(
            (planning_review_gap.title, planning_review_gap.summary, planning_review_gap.rationale)
        ).lower()
        self.assertIn("planning", planning_text)
        self.assertIn("activation", planning_text)

    def test_build_self_hosting_template_can_prefer_explicit_gap_ids(self) -> None:
        markdown = """
## 6. 建议优先级

1. authority root / reducer 集成，把 lane complete 继续推进成 authority / objective complete
2. PostgreSQL 的正式 CRUD persistence
3. tool-capable code-edit worker，允许 worker 在受控 owned_paths 内完成代码编辑与验证
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "implementation-status.md"
            path.write_text(markdown, encoding="utf-8")
            inventory = load_runtime_gap_inventory(path)

        template = build_self_hosting_template(
            inventory=inventory,
            config=SelfHostingBootstrapConfig(
                objective_id="obj-self-host",
                group_id="group-self-host",
                max_workstreams=2,
                preferred_gap_ids=("tool-capable-code-edit-worker", "authority-integration"),
            ),
        )

        self.assertEqual(
            [item.workstream_id for item in template.workstreams],
            ["tool-capable-code-edit-worker", "authority-integration"],
        )

    def test_inventory_recognizes_group_coordination_gap_definitions(self) -> None:
        markdown = """
## 6. 建议优先级

1. Claude 风格的 team 内并行执行
2. 强验证 leader -> teammate delegation
3. 并行的 SuperLeader lane scheduler
4. 订阅式消息池
5. shared / control-private 消息分类与 delivery mode 规则
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "implementation-status.md"
            path.write_text(markdown, encoding="utf-8")
            inventory = load_runtime_gap_inventory(path)

        self.assertEqual(
            [item.gap_id for item in inventory],
            [
                "team-parallel-execution",
                "leader-teammate-delegation-validation",
                "superleader-parallel-scheduler",
                "message-pool-subscriptions",
                "message-visibility-policy",
            ],
        )

    def test_build_self_hosting_template_keeps_group_coordination_priority_order(self) -> None:
        markdown = """
## 6. 建议优先级

1. Claude 风格的 team 内并行执行
2. 强验证 leader -> teammate delegation
3. 并行的 SuperLeader lane scheduler
4. 订阅式消息池
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "implementation-status.md"
            path.write_text(markdown, encoding="utf-8")
            inventory = load_runtime_gap_inventory(path)

        template = build_self_hosting_template(
            inventory=inventory,
            config=SelfHostingBootstrapConfig(
                objective_id="obj-group-coord",
                group_id="group-self-host",
                max_workstreams=3,
            ),
        )

        self.assertEqual(
            [item.workstream_id for item in template.workstreams],
            [
                "team-parallel-execution",
                "leader-teammate-delegation-validation",
                "superleader-parallel-scheduler",
            ],
        )

    def test_inventory_maps_team_parallel_execution_to_resident_async_coordination_metadata(self) -> None:
        markdown = """
## 6. 建议优先级

1. team parallel execution toward resident/subscription/autonomous claim
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "implementation-status.md"
            path.write_text(markdown, encoding="utf-8")
            inventory = load_runtime_gap_inventory(path)

        self.assertEqual(len(inventory), 1)
        self.assertEqual(inventory[0].gap_id, "team-parallel-execution")
        metadata_text = " ".join(
            (inventory[0].title, inventory[0].summary, inventory[0].rationale)
        ).lower()
        self.assertIn("resident", metadata_text)
        self.assertIn("autonomous claim", metadata_text)
        self.assertNotIn("still runs teammate assignments serially", metadata_text)
        self.assertNotIn("serial teammate dispatch", metadata_text)

    def test_inventory_recognizes_markdown_wrapped_priority_lines(self) -> None:
        markdown = """
## 6. 建议优先级

1. 并行的 `SuperLeader` lane scheduler
2. 多轮 `LeaderSupervisor / TeammateSupervisor` 的 durable reconnect / cross-transport session 语义
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "implementation-status.md"
            path.write_text(markdown, encoding="utf-8")
            inventory = load_runtime_gap_inventory(path)

        self.assertEqual(
            [item.gap_id for item in inventory],
            [
                "superleader-parallel-scheduler",
                "durable-supervisor-sessions",
            ],
        )

    def test_build_self_hosting_superleader_config_adds_codex_role_profiles(self) -> None:
        superleader_config = build_self_hosting_superleader_config(
            SelfHostingBootstrapConfig(
                objective_id="obj-self-host",
                group_id="group-self-host",
                leader_backend="codex_cli",
                teammate_backend="codex_cli",
                leader_idle_timeout_seconds=45.0,
                leader_hard_timeout_seconds=900.0,
            )
        )

        self.assertIsNone(superleader_config.leader_backend)
        self.assertIsNone(superleader_config.teammate_backend)
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
        self.assertEqual(leader_policy.idle_timeout_seconds, 45.0)
        self.assertEqual(leader_policy.hard_timeout_seconds, 900.0)
        self.assertEqual(leader_profile.lease_policy.renewal_timeout_seconds, 45.0)
        self.assertEqual(leader_profile.lease_policy.hard_deadline_seconds, 900.0)
        self.assertFalse(leader_policy.resume_on_timeout)
        self.assertFalse(leader_policy.allow_relaunch)
        self.assertEqual(leader_policy.backoff_seconds, 2.0)
        self.assertEqual(leader_policy.provider_unavailable_backoff_initial_seconds, 15.0)
        self.assertEqual(leader_policy.provider_unavailable_backoff_multiplier, 2.0)
        self.assertEqual(leader_policy.provider_unavailable_backoff_max_seconds, 120.0)
        self.assertEqual(leader_profile.fallback_provider_unavailable_backoff_initial_seconds, 15.0)
        self.assertEqual(leader_profile.fallback_provider_unavailable_backoff_multiplier, 2.0)
        self.assertEqual(leader_profile.fallback_provider_unavailable_backoff_max_seconds, 120.0)
        self.assertEqual(teammate_policy.max_attempts, 3)
        self.assertEqual(teammate_policy.backoff_seconds, 2.0)
        self.assertEqual(teammate_policy.provider_unavailable_backoff_initial_seconds, 15.0)
        self.assertEqual(teammate_policy.provider_unavailable_backoff_multiplier, 2.0)
        self.assertEqual(teammate_policy.provider_unavailable_backoff_max_seconds, 120.0)
        self.assertEqual(teammate_policy.idle_timeout_seconds, 45.0)
        self.assertEqual(teammate_policy.hard_timeout_seconds, 900.0)
        self.assertEqual(teammate_profile.lease_policy.renewal_timeout_seconds, 45.0)
        self.assertEqual(teammate_profile.lease_policy.hard_deadline_seconds, 900.0)
        self.assertEqual(teammate_profile.fallback_provider_unavailable_backoff_initial_seconds, 15.0)
        self.assertEqual(teammate_profile.fallback_provider_unavailable_backoff_multiplier, 2.0)
        self.assertEqual(teammate_profile.fallback_provider_unavailable_backoff_max_seconds, 120.0)

    def test_build_self_hosting_superleader_config_enables_planning_review_by_default(self) -> None:
        superleader_config = build_self_hosting_superleader_config(
            SelfHostingBootstrapConfig(
                objective_id="obj-self-host",
                group_id="group-self-host",
            )
        )

        self.assertTrue(superleader_config.enable_planning_review)

    def test_self_hosting_budget_exports_drop_concurrency_and_use_teammate_limit_20(self) -> None:
        gap = SelfHostingGap(
            gap_id="budget-simplification",
            priority=1,
            title="Budget simplification",
            summary="Remove max_concurrency from formal budget exports.",
            rationale="Team budget semantics now rely on teammate limit only.",
            source_path="resource/knowledge/agent-orchestra-runtime/resident-team-collaboration-and-slice-planning.md",
            source_line=1,
            team_name="Planning",
        )

        workstream_budget = gap.to_workstream_template().to_dict()["budget"]
        self.assertEqual(
            workstream_budget,
            {
                "max_teammates": 20,
                "max_iterations": 2,
                "max_tokens": None,
                "max_seconds": None,
            },
        )
        self.assertNotIn("max_concurrency", workstream_budget)

        dynamic_budget = gap.to_dynamic_seed()["budget"]
        self.assertEqual(
            dynamic_budget,
            {
                "max_teammates": 20,
                "max_iterations": 2,
                "max_tokens": None,
                "max_seconds": None,
            },
        )
        self.assertNotIn("max_concurrency", dynamic_budget)

    def test_inventory_authority_gap_summary_mentions_decision_resume_and_residual(self) -> None:
        markdown = """
## 6. 建议优先级

1. authority root / reducer 集成，把 lane complete 继续推进成 authority / objective complete
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "implementation-status.md"
            path.write_text(markdown, encoding="utf-8")
            inventory = load_runtime_gap_inventory(path)

        self.assertEqual(len(inventory), 1)
        authority_gap = inventory[0]
        self.assertEqual(authority_gap.gap_id, "authority-integration")
        authority_text = " ".join((authority_gap.summary, authority_gap.rationale)).lower()
        self.assertIn("grant", authority_text)
        self.assertIn("reroute", authority_text)
        self.assertIn("resume", authority_text)
        self.assertIn("residual", authority_text)
