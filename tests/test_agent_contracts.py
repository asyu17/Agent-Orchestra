from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest import TestCase

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agent_orchestra.contracts.agent import (
    Agent,
    AgentAction,
    AgentActionKind,
    AgentSession,
    AuthorityPolicy,
    ClaimPolicy,
    PromptTriggerPolicy,
    RolePolicy,
    SessionBinding,
)
from agent_orchestra.contracts.execution import ResidentCoordinatorPhase
from agent_orchestra.contracts.team import AgentProfile


class AgentContractsTest(TestCase):
    def test_agent_carries_scope_policy_prompt_and_session_identity(self) -> None:
        agent = Agent(
            agent_id="superleader-1",
            role_kind="superleader",
            scope_id="objective:alpha",
            profile_id="superleader-default",
            session_id="superleader-1:resident",
            mailbox_binding="mailbox:objective:alpha",
            task_surface_binding="task-surface:objective:alpha",
            blackboard_binding="blackboard:objective:alpha",
            policy_bundle={
                "role": "superleader",
                "claim": "digest_only",
            },
        )

        self.assertEqual(agent.role_kind, "superleader")
        self.assertEqual(agent.scope_id, "objective:alpha")
        self.assertEqual(agent.session_id, "superleader-1:resident")
        self.assertEqual(agent.policy_bundle["claim"], "digest_only")

    def test_agent_profile_can_carry_role_and_prompt_policies(self) -> None:
        profile = AgentProfile(
            agent_id="leader-a",
            name="Leader A",
            role="leader",
            role_policy=RolePolicy(
                role_kind="leader",
                visible_scopes=("leader_lane", "team"),
                writable_scopes=("team",),
                can_spawn_subordinates=True,
                allowed_subordinate_roles=("teammate",),
            ),
            claim_policy=ClaimPolicy(
                allow_directed_claims=True,
                allow_autonomous_claims=False,
                claim_scopes=("team",),
            ),
            authority_policy=AuthorityPolicy(
                mutation_scopes=("team",),
                readable_scopes=("leader_lane", "team"),
                allowed_directive_roles=("teammate",),
                can_create_tasks=True,
            ),
            prompt_trigger_policy=PromptTriggerPolicy(
                allow_prompt_turns=True,
                trigger_kinds=("mailbox_blocker", "team_conflict"),
            ),
            skill_ids=("planning", "coordination"),
            prompt_profile_id="leader-default",
        )

        self.assertEqual(profile.role_policy.role_kind, "leader")
        self.assertEqual(profile.claim_policy.claim_scopes, ("team",))
        self.assertEqual(profile.authority_policy.allowed_directive_roles, ("teammate",))
        self.assertEqual(profile.prompt_trigger_policy.trigger_kinds, ("mailbox_blocker", "team_conflict"))
        self.assertEqual(profile.skill_ids, ("planning", "coordination"))

    def test_agent_session_round_trips_json_safe_payload(self) -> None:
        session = AgentSession(
            session_id="session-1",
            agent_id="teammate-1",
            role="teammate",
            phase=ResidentCoordinatorPhase.IDLE,
            objective_id="objective-1",
            lane_id="lane-a",
            team_id="team-a",
            mailbox_cursor={"mailbox": {"offset": "10-0"}},
            subscription_cursors={"mailbox": {"offset": "10-0", "event_id": "evt-10"}},
            claimed_task_ids=("task-1", "task-2"),
            current_directive_ids=("directive-1",),
            current_binding=SessionBinding(
                session_id="session-1",
                backend="tmux",
                binding_type="resident",
                transport_locator={"session_name": "ao-team-a", "pane_id": "%42"},
            ),
            lease_id="lease-1",
            lease_expires_at="2026-04-05T12:00:00+00:00",
            last_progress_at="2026-04-05T11:59:00+00:00",
            metadata={"path": Path("/tmp/agent-orchestra")},
        )

        payload = session.to_dict()
        restored = AgentSession.from_dict(payload)

        self.assertEqual(payload["metadata"]["path"], "/tmp/agent-orchestra")
        self.assertEqual(restored.phase, ResidentCoordinatorPhase.IDLE)
        self.assertEqual(restored.current_binding.binding_type, "resident")
        self.assertEqual(restored.claimed_task_ids, ("task-1", "task-2"))
        self.assertEqual(restored.current_directive_ids, ("directive-1",))
        self.assertEqual(restored.last_progress_at, "2026-04-05T11:59:00+00:00")
        json.dumps(payload)

    def test_agent_action_captures_runtime_intent(self) -> None:
        action = AgentAction(
            kind=AgentActionKind.RUN_PROMPT_TURN,
            agent_id="leader-a",
            reason="team_conflict_detected",
            target_task_id="task-17",
            payload={"mailbox_events": 3},
        )

        self.assertEqual(action.kind, AgentActionKind.RUN_PROMPT_TURN)
        self.assertEqual(action.target_task_id, "task-17")
        self.assertEqual(action.payload["mailbox_events"], 3)

    def test_agent_action_kind_includes_subscription_and_escalation(self) -> None:
        self.assertEqual(AgentActionKind.SUBSCRIBE_MESSAGES.value, "subscribe_messages")
        self.assertEqual(AgentActionKind.UNSUBSCRIBE_MESSAGES.value, "unsubscribe_messages")
        self.assertEqual(AgentActionKind.ESCALATE.value, "escalate")
