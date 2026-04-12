from __future__ import annotations

from enum import Enum
from pathlib import PurePosixPath
from dataclasses import dataclass, field
from typing import Any, Mapping

from agent_orchestra.contracts.enums import AuthorityStatus


class AuthorityBoundaryClass(str, Enum):
    SOFT_SCOPE = "soft_scope"
    PROTECTED_RUNTIME = "protected_runtime"
    CROSS_TEAM_SHARED = "cross_team_shared"
    GLOBAL_CONTRACT = "global_contract"


class AuthorityPolicyAction(str, Enum):
    GRANT = "grant"
    DENY = "deny"
    REROUTE = "reroute"
    ESCALATE = "escalate"


class AuthorityCompletionStatus(str, Enum):
    WAITING = "waiting"
    GRANT_RESUMED = "grant_resumed"
    REROUTE_CLOSED = "reroute_closed"
    DENY_CLOSED = "deny_closed"
    RELAY_PENDING = "relay_pending"
    INCOMPLETE = "incomplete"


def _normalize_relative_path(path: str) -> str:
    normalized = PurePosixPath(path).as_posix()
    if normalized == ".":
        return normalized
    return normalized.lstrip("./")


def _parse_boundary_class(value: object) -> AuthorityBoundaryClass | None:
    if isinstance(value, AuthorityBoundaryClass):
        return value
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    for boundary_class in AuthorityBoundaryClass:
        if boundary_class.value == normalized:
            return boundary_class
    return None


def _parse_policy_action(value: object) -> AuthorityPolicyAction | None:
    if isinstance(value, AuthorityPolicyAction):
        return value
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    for policy_action in AuthorityPolicyAction:
        if policy_action.value == normalized:
            return policy_action
    return None


@dataclass(slots=True)
class AuthorityPolicy:
    global_contract_prefixes: tuple[str, ...] = ("src/agent_orchestra/contracts/",)
    protected_runtime_prefixes: tuple[str, ...] = (
        "src/agent_orchestra/runtime/superleader.py",
        "src/agent_orchestra/runtime/session_host.py",
        "src/agent_orchestra/self_hosting/bootstrap.py",
    )
    cross_team_shared_prefixes: tuple[str, ...] = ("resource/knowledge/README.md",)
    soft_scope_default_action: AuthorityPolicyAction = AuthorityPolicyAction.GRANT
    escalated_boundary_actions: Mapping[AuthorityBoundaryClass, AuthorityPolicyAction] = field(
        default_factory=lambda: {
            AuthorityBoundaryClass.PROTECTED_RUNTIME: AuthorityPolicyAction.GRANT,
            AuthorityBoundaryClass.GLOBAL_CONTRACT: AuthorityPolicyAction.DENY,
            AuthorityBoundaryClass.CROSS_TEAM_SHARED: AuthorityPolicyAction.REROUTE,
        }
    )

    @classmethod
    def default(cls) -> "AuthorityPolicy":
        return cls()

    @staticmethod
    def _path_matches_prefix(path: str, prefix: str) -> bool:
        normalized_path = _normalize_relative_path(path)
        normalized_prefix = _normalize_relative_path(prefix)
        return normalized_path == normalized_prefix or normalized_path.startswith(normalized_prefix)

    @classmethod
    def _matches_prefix_set(
        cls,
        *,
        requested_paths: tuple[str, ...],
        prefixes: tuple[str, ...],
    ) -> bool:
        for path in requested_paths:
            if not path:
                continue
            if any(cls._path_matches_prefix(path, prefix) for prefix in prefixes if prefix):
                return True
        return False

    def classify_boundary(
        self,
        requested_paths: tuple[str, ...],
    ) -> AuthorityBoundaryClass:
        if not requested_paths:
            return AuthorityBoundaryClass.SOFT_SCOPE
        if self._matches_prefix_set(
            requested_paths=requested_paths,
            prefixes=self.global_contract_prefixes,
        ):
            return AuthorityBoundaryClass.GLOBAL_CONTRACT
        if self._matches_prefix_set(
            requested_paths=requested_paths,
            prefixes=self.cross_team_shared_prefixes,
        ):
            return AuthorityBoundaryClass.CROSS_TEAM_SHARED
        if self._matches_prefix_set(
            requested_paths=requested_paths,
            prefixes=self.protected_runtime_prefixes,
        ):
            return AuthorityBoundaryClass.PROTECTED_RUNTIME
        return AuthorityBoundaryClass.SOFT_SCOPE

    def soft_scope_action(
        self,
        authority_request: "ScopeExtensionRequest",
    ) -> AuthorityPolicyAction:
        explicit_action = _parse_policy_action(authority_request.soft_scope_policy_action)
        if explicit_action in {
            AuthorityPolicyAction.GRANT,
            AuthorityPolicyAction.DENY,
            AuthorityPolicyAction.REROUTE,
        }:
            return explicit_action
        if self.soft_scope_default_action in {
            AuthorityPolicyAction.GRANT,
            AuthorityPolicyAction.DENY,
            AuthorityPolicyAction.REROUTE,
        }:
            return self.soft_scope_default_action
        return AuthorityPolicyAction.GRANT

    def escalated_boundary_action(
        self,
        boundary_class: AuthorityBoundaryClass | str,
    ) -> AuthorityPolicyAction | None:
        normalized_boundary_class = _parse_boundary_class(boundary_class)
        if (
            normalized_boundary_class is None
            or normalized_boundary_class == AuthorityBoundaryClass.SOFT_SCOPE
        ):
            return None
        for raw_boundary_class, raw_policy_action in self.escalated_boundary_actions.items():
            candidate_boundary_class = _parse_boundary_class(raw_boundary_class)
            if candidate_boundary_class != normalized_boundary_class:
                continue
            candidate_policy_action = _parse_policy_action(raw_policy_action)
            if candidate_policy_action is not None:
                return candidate_policy_action
        return None


@dataclass(slots=True)
class ScopeExtensionRequest:
    request_id: str
    assignment_id: str
    worker_id: str
    task_id: str
    requested_paths: tuple[str, ...] = field(default_factory=tuple)
    reason: str = ""
    evidence: str = ""
    blocking_verification_command: str = ""
    retry_hint: str = ""
    soft_scope_policy_action: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "assignment_id": self.assignment_id,
            "worker_id": self.worker_id,
            "task_id": self.task_id,
            "requested_paths": list(self.requested_paths),
            "reason": self.reason,
            "evidence": self.evidence,
            "blocking_verification_command": self.blocking_verification_command,
            "retry_hint": self.retry_hint,
            "soft_scope_policy_action": self.soft_scope_policy_action,
        }

    @classmethod
    def from_payload(cls, payload: object) -> "ScopeExtensionRequest | None":
        if isinstance(payload, cls):
            return payload
        if not isinstance(payload, Mapping):
            return None
        request_id = payload.get("request_id")
        assignment_id = payload.get("assignment_id")
        worker_id = payload.get("worker_id")
        task_id = payload.get("task_id")
        if not all(
            isinstance(value, str) and value.strip()
            for value in (request_id, assignment_id, worker_id, task_id)
        ):
            return None
        requested_paths_raw = payload.get("requested_paths", ())
        if not isinstance(requested_paths_raw, (list, tuple)):
            requested_paths_raw = ()
        return cls(
            request_id=request_id.strip(),
            assignment_id=assignment_id.strip(),
            worker_id=worker_id.strip(),
            task_id=task_id.strip(),
            requested_paths=tuple(
                item.strip()
                for item in requested_paths_raw
                if isinstance(item, str) and item.strip()
            ),
            reason=str(payload.get("reason", "")),
            evidence=str(payload.get("evidence", "")),
            blocking_verification_command=str(payload.get("blocking_verification_command", "")),
            retry_hint=str(payload.get("retry_hint", "")),
            soft_scope_policy_action=str(
                payload.get("soft_scope_policy_action", payload.get("policy_action", ""))
            ).strip(),
        )


@dataclass(slots=True)
class AuthorityDecision:
    request_id: str
    decision: str
    actor_id: str = ""
    scope_class: str = ""
    granted_paths: tuple[str, ...] = field(default_factory=tuple)
    reroute_task_id: str | None = None
    escalated_to: str | None = None
    reason: str = ""
    resume_mode: str = ""
    summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "decision": self.decision,
            "actor_id": self.actor_id,
            "scope_class": self.scope_class,
            "granted_paths": list(self.granted_paths),
            "reroute_task_id": self.reroute_task_id,
            "escalated_to": self.escalated_to,
            "reason": self.reason,
            "resume_mode": self.resume_mode,
            "summary": self.summary,
        }

    @classmethod
    def from_payload(cls, payload: object) -> "AuthorityDecision | None":
        if isinstance(payload, cls):
            return payload
        if not isinstance(payload, Mapping):
            return None
        request_id = payload.get("request_id")
        decision = payload.get("decision")
        if not all(isinstance(value, str) and value.strip() for value in (request_id, decision)):
            return None
        granted_paths_raw = payload.get("granted_paths", ())
        if not isinstance(granted_paths_raw, (list, tuple)):
            granted_paths_raw = ()
        return cls(
            request_id=request_id.strip(),
            decision=decision.strip(),
            actor_id=str(payload.get("actor_id", "")),
            scope_class=str(payload.get("scope_class", "")),
            granted_paths=tuple(
                item.strip()
                for item in granted_paths_raw
                if isinstance(item, str) and item.strip()
            ),
            reroute_task_id=(
                str(payload["reroute_task_id"])
                if payload.get("reroute_task_id") is not None
                else None
            ),
            escalated_to=(
                str(payload["escalated_to"])
                if payload.get("escalated_to") is not None
                else None
            ),
            reason=str(payload.get("reason", "")),
            resume_mode=str(payload.get("resume_mode", "")),
            summary=str(payload.get("summary", "")),
        )


@dataclass(slots=True)
class AuthorityCompletionRequestSnapshot:
    request_id: str
    task_id: str
    worker_id: str
    boundary_class: str = ""
    decision: str = ""
    completion_status: AuthorityCompletionStatus = AuthorityCompletionStatus.INCOMPLETE
    relay_subject: str | None = None
    relay_published: bool = False
    relay_consumed: bool = False
    wake_recorded: bool = False
    replacement_task_id: str | None = None
    terminal_task_status: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "task_id": self.task_id,
            "worker_id": self.worker_id,
            "boundary_class": self.boundary_class,
            "decision": self.decision,
            "completion_status": self.completion_status.value,
            "relay_subject": self.relay_subject,
            "relay_published": self.relay_published,
            "relay_consumed": self.relay_consumed,
            "wake_recorded": self.wake_recorded,
            "replacement_task_id": self.replacement_task_id,
            "terminal_task_status": self.terminal_task_status,
        }


@dataclass(slots=True)
class AuthorityCompletionLaneSnapshot:
    objective_id: str
    lane_id: str
    team_id: str | None
    request_count: int = 0
    decision_counts: dict[str, int] = field(default_factory=dict)
    closed_request_ids: tuple[str, ...] = ()
    waiting_request_ids: tuple[str, ...] = ()
    incomplete_request_ids: tuple[str, ...] = ()
    relay_pending_request_ids: tuple[str, ...] = ()
    reroute_links: dict[str, str] = field(default_factory=dict)
    validated: bool = False
    requests: tuple[AuthorityCompletionRequestSnapshot, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "objective_id": self.objective_id,
            "lane_id": self.lane_id,
            "team_id": self.team_id,
            "request_count": self.request_count,
            "decision_counts": dict(self.decision_counts),
            "closed_request_ids": list(self.closed_request_ids),
            "waiting_request_ids": list(self.waiting_request_ids),
            "incomplete_request_ids": list(self.incomplete_request_ids),
            "relay_pending_request_ids": list(self.relay_pending_request_ids),
            "reroute_links": dict(self.reroute_links),
            "validated": self.validated,
            "requests": [item.to_dict() for item in self.requests],
        }


@dataclass(slots=True)
class AuthorityReactorCycleOutput:
    reactor_role: str
    pending_request_count: int = 0
    pending_request_ids: tuple[str, ...] = ()
    decision_request_ids: tuple[str, ...] = ()
    decision_lane_ids: tuple[str, ...] = ()
    escalated_request_ids: tuple[str, ...] = ()
    forwarded_request_ids: tuple[str, ...] = ()
    incomplete_request_ids: tuple[str, ...] = ()

    def to_metadata_patch(
        self,
        *,
        last_cycle_at: str,
    ) -> dict[str, Any]:
        return {
            "authority_reactor_role": self.reactor_role,
            "authority_reactor_last_cycle_at": last_cycle_at,
            "authority_reactor_pending_request_count": self.pending_request_count,
            "authority_reactor_pending_request_ids": list(self.pending_request_ids),
            "authority_reactor_decision_request_ids": list(self.decision_request_ids),
            "authority_reactor_decision_lane_ids": list(self.decision_lane_ids),
            "authority_reactor_escalated_request_ids": list(self.escalated_request_ids),
            "authority_reactor_forwarded_request_ids": list(self.forwarded_request_ids),
            "authority_reactor_incomplete_request_ids": list(self.incomplete_request_ids),
        }


@dataclass(slots=True)
class AuthorityState:
    group_id: str
    status: AuthorityStatus = AuthorityStatus.PENDING
    accepted_handoffs: tuple[str, ...] = field(default_factory=tuple)
    updated_task_ids: tuple[str, ...] = field(default_factory=tuple)
    summary: str = ""
