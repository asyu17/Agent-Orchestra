from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from unittest import TestCase

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agent_orchestra.self_hosting.bootstrap import (
    SelfHostingInstructionPacket,
    SelfHostingLaneInstruction,
    SelfHostingTaskInstruction,
    render_self_hosting_instruction_packet,
    write_self_hosting_instruction_packet,
)


class SelfHostingExportTest(TestCase):
    def test_render_instruction_packet_mentions_next_round_and_lane_tasks(self) -> None:
        packet = SelfHostingInstructionPacket(
            objective_id="obj-self-host",
            objective_status="completed",
            selected_gap_ids=("authority-integration", "postgres-persistence"),
            completed_gap_ids=("authority-integration",),
            remaining_gap_ids=("postgres-persistence", "protocol-bus"),
            next_round_gap_ids=("postgres-persistence", "protocol-bus"),
            next_round_prompt="Continue with persistence and protocol bus.",
            lane_instructions=(
                SelfHostingLaneInstruction(
                    gap_id="authority-integration",
                    lane_id="authority-integration",
                    team_id="group-a:team:authority-integration",
                    leader_id="leader:authority-integration",
                    delivery_status="completed",
                    summary="Authority integration converged.",
                    tasks=(
                        SelfHostingTaskInstruction(
                            task_id="task-1",
                            goal="Integrate reducer completion into objective delivery.",
                            reason="Need authority-aware completion.",
                            status="completed",
                            owned_paths=("src/agent_orchestra/runtime/reducer.py",),
                            verification_commands=("python3 -m unittest tests.test_runtime -v",),
                        ),
                    ),
                ),
            ),
        )

        rendered = render_self_hosting_instruction_packet(packet)

        self.assertIn("obj-self-host", rendered)
        self.assertIn("authority-integration", rendered)
        self.assertIn("Continue with persistence and protocol bus.", rendered)
        self.assertIn("python3 -m unittest tests.test_runtime -v", rendered)

    def test_write_instruction_packet_supports_json_and_markdown(self) -> None:
        packet = SelfHostingInstructionPacket(
            objective_id="obj-self-host",
            objective_status="completed",
            selected_gap_ids=("authority-integration",),
            completed_gap_ids=("authority-integration",),
            remaining_gap_ids=("protocol-bus",),
            next_round_gap_ids=("protocol-bus",),
            next_round_prompt="Continue with protocol bus.",
            lane_instructions=(),
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            json_path = Path(tmpdir) / "packet.json"
            md_path = Path(tmpdir) / "packet.md"

            write_self_hosting_instruction_packet(json_path, packet)
            write_self_hosting_instruction_packet(md_path, packet)

            json_payload = json.loads(json_path.read_text(encoding="utf-8"))
            md_payload = md_path.read_text(encoding="utf-8")

        self.assertEqual(json_payload["objective_id"], "obj-self-host")
        self.assertIn("Continue with protocol bus.", md_payload)

    def test_write_instruction_packet_json_preserves_utf8_text(self) -> None:
        packet = SelfHostingInstructionPacket(
            objective_id="目标-自举",
            objective_status="completed",
            selected_gap_ids=("协议总线",),
            completed_gap_ids=("协议总线",),
            remaining_gap_ids=("角色配置",),
            next_round_gap_ids=("角色配置",),
            next_round_prompt="继续推进角色配置。",
            lane_instructions=(
                SelfHostingLaneInstruction(
                    gap_id="协议总线",
                    lane_id="协议总线",
                    team_id="group-a:team:协议总线",
                    leader_id="leader:协议总线",
                    delivery_status="completed",
                    summary="中文摘要应保留直写。",
                    tasks=(),
                ),
            ),
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            json_path = Path(tmpdir) / "packet.json"
            write_self_hosting_instruction_packet(json_path, packet)
            raw_payload = json_path.read_text(encoding="utf-8")

        self.assertIn("目标-自举", raw_payload)
        self.assertIn("继续推进角色配置。", raw_payload)
        self.assertNotIn("\\u76ee\\u6807", raw_payload)

    def test_render_instruction_packet_includes_authority_completion_evidence(self) -> None:
        packet = SelfHostingInstructionPacket(
            objective_id="obj-self-host-authority",
            objective_status="running",
            selected_gap_ids=("authority-integration",),
            completed_gap_ids=(),
            remaining_gap_ids=("authority-integration",),
            next_round_gap_ids=("authority-integration",),
            next_round_prompt="Continue authority hardening.",
            lane_instructions=(
                SelfHostingLaneInstruction(
                    gap_id="authority-integration",
                    lane_id="lane-authority",
                    team_id="group-a:team:authority",
                    leader_id="leader:authority",
                    delivery_status="running",
                    summary="Authority reroute is active.",
                    metadata={
                        "authority_completion": {
                            "validated": True,
                            "completion_status": "validated",
                            "request_count": 1,
                            "decision_counts": {"reroute": 1},
                            "closed_request_ids": ["req-reroute-1"],
                            "waiting_request_ids": [],
                            "incomplete_request_ids": [],
                            "relay_pending_request_ids": [],
                            "reroute_links": [
                                {
                                    "superseded_task_id": "task-old",
                                    "replacement_task_id": "task-new",
                                }
                            ],
                            "requests": [
                                {
                                    "request_id": "req-reroute-1",
                                    "task_id": "task-old",
                                    "worker_id": "worker-1",
                                    "boundary_class": "soft_scope",
                                    "decision": "reroute",
                                    "completion_status": "reroute_closed",
                                    "relay_subject": "authority.decision",
                                    "relay_published": True,
                                    "relay_consumed": True,
                                    "wake_recorded": False,
                                    "replacement_task_id": "task-new",
                                    "terminal_task_status": "cancelled",
                                }
                            ],
                        }
                    },
                ),
            ),
            metadata={
                "authority_completion_status": {
                    "validated": True,
                    "completion_status": "validated",
                    "request_count": 1,
                    "decision_counts": {"reroute": 1},
                    "closed_request_ids": ["req-reroute-1"],
                    "waiting_request_ids": [],
                    "incomplete_request_ids": [],
                    "relay_pending_request_ids": [],
                }
            },
        )

        rendered = render_self_hosting_instruction_packet(packet)

        self.assertIn("Authority Completion", rendered)
        self.assertIn("completion_status=validated", rendered)
        self.assertIn("req-reroute-1: reroute_closed", rendered)
        self.assertIn("task-old -> task-new", rendered)
        self.assertIn("decision_counts=reroute=1", rendered)

    def test_render_instruction_packet_includes_planning_review_status(self) -> None:
        packet = SelfHostingInstructionPacket(
            objective_id="obj-self-host-planning-review",
            objective_status="completed",
            selected_gap_ids=("multi-leader-planning-review",),
            completed_gap_ids=("multi-leader-planning-review",),
            remaining_gap_ids=(),
            next_round_gap_ids=(),
            next_round_prompt="No remaining gaps.",
            lane_instructions=(
                SelfHostingLaneInstruction(
                    gap_id="multi-leader-planning-review",
                    lane_id="lane-planning-review",
                    team_id="group-a:team:planning",
                    leader_id="leader:planning",
                    delivery_status="completed",
                    summary="Planning review converged.",
                    metadata={
                        "planning_review": {
                            "enabled": True,
                            "planning_round_id": "obj-self-host-planning-review:planning-round:1",
                            "validated": True,
                            "activation_gate": {
                                "status": "ready_for_activation",
                                "summary": "All revised plans are ready for activation.",
                                "blockers": [],
                            },
                        }
                    },
                ),
            ),
            metadata={
                "planning_review_status": {
                    "enabled": True,
                    "planning_round_id": "obj-self-host-planning-review:planning-round:1",
                    "validated": True,
                    "activation_gate": {
                        "status": "ready_for_activation",
                        "summary": "All revised plans are ready for activation.",
                        "blockers": [],
                    },
                }
            },
        )

        rendered = render_self_hosting_instruction_packet(packet)

        self.assertIn("Planning Review", rendered)
        self.assertIn("ready_for_activation", rendered)
        self.assertIn("obj-self-host-planning-review:planning-round:1", rendered)
