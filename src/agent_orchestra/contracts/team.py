from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agent_orchestra.contracts.agent import (
    AuthorityPolicy,
    ClaimPolicy,
    PromptTriggerPolicy,
    RolePolicy,
)


@dataclass(slots=True)
class Group:
    group_id: str
    display_name: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class AgentProfile:
    agent_id: str
    name: str
    role: str = "worker"
    model: str | None = None
    role_policy: RolePolicy | None = None
    claim_policy: ClaimPolicy | None = None
    authority_policy: AuthorityPolicy | None = None
    prompt_trigger_policy: PromptTriggerPolicy | None = None
    skill_ids: tuple[str, ...] = ()
    prompt_profile_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Team:
    team_id: str
    group_id: str
    name: str
    member_ids: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)
