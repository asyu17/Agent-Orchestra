from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from collections.abc import Mapping
from typing import Any


@dataclass(slots=True)
class PermissionRequest:
    requester: str
    action: str
    rationale: str
    request_id: str | None = None
    scope: str | None = None
    group_id: str | None = None
    objective_id: str | None = None
    team_id: str | None = None
    lane_id: str | None = None
    task_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "requester": self.requester,
            "action": self.action,
            "rationale": self.rationale,
            "request_id": self.request_id,
            "scope": self.scope,
            "group_id": self.group_id,
            "objective_id": self.objective_id,
            "team_id": self.team_id,
            "lane_id": self.lane_id,
            "task_id": self.task_id,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "PermissionRequest":
        return cls(
            requester=str(payload.get("requester", "")).strip(),
            action=str(payload.get("action", "")).strip(),
            rationale=str(payload.get("rationale", "")).strip(),
            request_id=(
                str(payload["request_id"]).strip()
                if payload.get("request_id") is not None
                else None
            ),
            scope=str(payload["scope"]).strip() if payload.get("scope") is not None else None,
            group_id=(
                str(payload["group_id"]).strip()
                if payload.get("group_id") is not None
                else None
            ),
            objective_id=(
                str(payload["objective_id"]).strip()
                if payload.get("objective_id") is not None
                else None
            ),
            team_id=str(payload["team_id"]).strip() if payload.get("team_id") is not None else None,
            lane_id=str(payload["lane_id"]).strip() if payload.get("lane_id") is not None else None,
            task_id=str(payload["task_id"]).strip() if payload.get("task_id") is not None else None,
            metadata=dict(payload.get("metadata", {})),
        )


@dataclass(slots=True)
class PermissionDecision:
    approved: bool
    reviewer: str
    reason: str = ""
    request_id: str | None = None
    pending: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "approved": self.approved,
            "reviewer": self.reviewer,
            "reason": self.reason,
            "request_id": self.request_id,
            "pending": self.pending,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "PermissionDecision":
        return cls(
            approved=bool(payload.get("approved", False)),
            reviewer=str(payload.get("reviewer", "")).strip(),
            reason=str(payload.get("reason", "")).strip(),
            request_id=(
                str(payload["request_id"]).strip()
                if payload.get("request_id") is not None
                else None
            ),
            pending=bool(payload.get("pending", False)),
        )


class PermissionBroker(ABC):
    @abstractmethod
    async def request_decision(self, request: PermissionRequest) -> PermissionDecision:
        raise NotImplementedError


@dataclass(slots=True)
class StaticPermissionBroker(PermissionBroker):
    decision: PermissionDecision | None = None
    default_approved: bool = True
    approved_actions: set[str] = field(default_factory=set)
    denied_actions: set[str] = field(default_factory=set)
    pending_actions: set[str] = field(default_factory=set)
    reviewer: str = "system.static"

    async def request_decision(self, request: PermissionRequest) -> PermissionDecision:
        if self.decision is not None:
            request_id = self.decision.request_id or request.request_id
            return PermissionDecision(
                approved=self.decision.approved,
                reviewer=self.decision.reviewer,
                reason=self.decision.reason,
                request_id=request_id,
                pending=self.decision.pending,
            )
        approved = self.default_approved
        if request.action in self.approved_actions:
            approved = True
        if request.action in self.denied_actions:
            approved = False
        pending = request.action in self.pending_actions
        if pending:
            approved = False
        return PermissionDecision(
            approved=approved,
            reviewer=self.reviewer,
            reason=(
                "Pending manual approval by static policy."
                if pending
                else "Approved by static policy."
                if approved
                else "Denied by static policy."
            ),
            request_id=request.request_id,
            pending=pending,
        )

    async def request(self, request: PermissionRequest) -> PermissionDecision:
        return await self.request_decision(request)
