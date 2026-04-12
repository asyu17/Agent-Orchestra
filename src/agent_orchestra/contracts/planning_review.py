from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from agent_orchestra.contracts.ids import make_id


class PlanningReviewSeverity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    BLOCKER = "blocker"


class ActivationGateStatus(str, Enum):
    BLOCKED = "blocked"
    NEEDS_AUTHORITY = "needs_authority"
    NEEDS_PROJECT_ITEM_PROMOTION = "needs_project_item_promotion"
    NEEDS_REPLAN = "needs_replan"
    READY_FOR_ACTIVATION = "ready_for_activation"


def _string_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(item).strip() for item in value if str(item).strip())


def _mapping(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): item for key, item in value.items()}


def _bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _coerce_severity(value: object) -> PlanningReviewSeverity:
    if isinstance(value, PlanningReviewSeverity):
        return value
    try:
        return PlanningReviewSeverity(str(value))
    except ValueError:
        return PlanningReviewSeverity.MEDIUM


def _coerce_activation_gate_status(value: object) -> ActivationGateStatus:
    if isinstance(value, ActivationGateStatus):
        return value
    try:
        return ActivationGateStatus(str(value))
    except ValueError:
        return ActivationGateStatus.BLOCKED


def make_leader_draft_plan_id() -> str:
    return make_id("leaderdraft")


def make_leader_peer_review_id() -> str:
    return make_id("leaderpeer")


def make_superleader_global_review_id() -> str:
    return make_id("superreview")


def make_leader_revised_plan_id() -> str:
    return make_id("leaderrevised")


def make_activation_gate_decision_id() -> str:
    return make_id("activationgate")


@dataclass(slots=True)
class PlanningSlice:
    slice_id: str
    title: str
    goal: str
    reason: str
    mode: str
    depends_on: tuple[str, ...] = ()
    parallel_group: str | None = None
    owned_paths: tuple[str, ...] = ()
    verification_commands: tuple[str, ...] = ()
    acceptance_checks: tuple[str, ...] = ()
    related_project_items: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "slice_id": self.slice_id,
            "title": self.title,
            "goal": self.goal,
            "reason": self.reason,
            "mode": self.mode,
            "depends_on": list(self.depends_on),
            "parallel_group": self.parallel_group,
            "owned_paths": list(self.owned_paths),
            "verification_commands": list(self.verification_commands),
            "acceptance_checks": list(self.acceptance_checks),
            "related_project_items": list(self.related_project_items),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "PlanningSlice":
        return cls(
            slice_id=str(payload.get("slice_id", "")).strip(),
            title=str(payload.get("title", "")).strip(),
            goal=str(payload.get("goal", "")).strip(),
            reason=str(payload.get("reason", "")).strip(),
            mode=str(payload.get("mode") or payload.get("slice_mode") or "sequential").strip(),
            depends_on=_string_tuple(payload.get("depends_on")),
            parallel_group=_optional_string(payload.get("parallel_group")),
            owned_paths=_string_tuple(payload.get("owned_paths")),
            verification_commands=_string_tuple(payload.get("verification_commands")),
            acceptance_checks=_string_tuple(payload.get("acceptance_checks")),
            related_project_items=_string_tuple(payload.get("related_project_items")),
        )


@dataclass(slots=True)
class LeaderDraftPlan:
    objective_id: str
    planning_round_id: str
    leader_id: str
    lane_id: str
    team_id: str
    summary: str
    plan_id: str = field(default_factory=make_leader_draft_plan_id)
    sequential_slices: tuple[PlanningSlice, ...] = ()
    parallel_slices: tuple[PlanningSlice, ...] = ()
    project_items: tuple[str, ...] = ()
    shared_hotspots: tuple[str, ...] = ()
    verification_targets: tuple[str, ...] = ()
    authority_risks: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "objective_id": self.objective_id,
            "planning_round_id": self.planning_round_id,
            "leader_id": self.leader_id,
            "lane_id": self.lane_id,
            "team_id": self.team_id,
            "summary": self.summary,
            "plan_id": self.plan_id,
            "sequential_slices": [slice_.to_dict() for slice_ in self.sequential_slices],
            "parallel_slices": [slice_.to_dict() for slice_ in self.parallel_slices],
            "project_items": list(self.project_items),
            "shared_hotspots": list(self.shared_hotspots),
            "verification_targets": list(self.verification_targets),
            "authority_risks": list(self.authority_risks),
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "LeaderDraftPlan":
        return cls(
            objective_id=str(payload.get("objective_id", "")).strip(),
            planning_round_id=str(payload.get("planning_round_id", "")).strip(),
            leader_id=str(payload.get("leader_id", "")).strip(),
            lane_id=str(payload.get("lane_id", "")).strip(),
            team_id=str(payload.get("team_id", "")).strip(),
            summary=str(payload.get("summary", "")).strip(),
            plan_id=str(payload.get("plan_id") or make_leader_draft_plan_id()),
            sequential_slices=tuple(
                PlanningSlice.from_payload(_mapping(item))
                for item in payload.get("sequential_slices", ())
                if isinstance(item, Mapping)
            ),
            parallel_slices=tuple(
                PlanningSlice.from_payload(_mapping(item))
                for item in payload.get("parallel_slices", ())
                if isinstance(item, Mapping)
            ),
            project_items=_string_tuple(payload.get("project_items")),
            shared_hotspots=_string_tuple(payload.get("shared_hotspots")),
            verification_targets=_string_tuple(payload.get("verification_targets")),
            authority_risks=_string_tuple(payload.get("authority_risks")),
            metadata=_mapping(payload.get("metadata")),
        )


@dataclass(slots=True)
class LeaderPeerReview:
    objective_id: str
    planning_round_id: str
    reviewer_leader_id: str
    reviewer_team_id: str
    target_leader_id: str
    target_team_id: str
    summary: str
    conflict_type: str
    severity: PlanningReviewSeverity
    review_id: str = field(default_factory=make_leader_peer_review_id)
    target_plan_id: str | None = None
    affected_paths: tuple[str, ...] = ()
    affected_project_items: tuple[str, ...] = ()
    reason: str = ""
    suggested_change: str = ""
    requires_superleader_attention: bool = False
    full_text_ref: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "objective_id": self.objective_id,
            "planning_round_id": self.planning_round_id,
            "review_id": self.review_id,
            "reviewer_leader_id": self.reviewer_leader_id,
            "reviewer_team_id": self.reviewer_team_id,
            "target_leader_id": self.target_leader_id,
            "target_team_id": self.target_team_id,
            "target_plan_id": self.target_plan_id,
            "summary": self.summary,
            "conflict_type": self.conflict_type,
            "severity": self.severity.value,
            "affected_paths": list(self.affected_paths),
            "affected_project_items": list(self.affected_project_items),
            "reason": self.reason,
            "suggested_change": self.suggested_change,
            "requires_superleader_attention": self.requires_superleader_attention,
            "full_text_ref": self.full_text_ref,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "LeaderPeerReview":
        return cls(
            objective_id=str(payload.get("objective_id", "")).strip(),
            planning_round_id=str(payload.get("planning_round_id", "")).strip(),
            review_id=str(payload.get("review_id") or make_leader_peer_review_id()),
            reviewer_leader_id=str(payload.get("reviewer_leader_id", "")).strip(),
            reviewer_team_id=str(payload.get("reviewer_team_id", "")).strip(),
            target_leader_id=str(payload.get("target_leader_id", "")).strip(),
            target_team_id=str(payload.get("target_team_id", "")).strip(),
            target_plan_id=_optional_string(payload.get("target_plan_id")),
            summary=str(payload.get("summary", "")).strip(),
            conflict_type=str(payload.get("conflict_type", "")).strip(),
            severity=_coerce_severity(payload.get("severity")),
            affected_paths=_string_tuple(payload.get("affected_paths")),
            affected_project_items=_string_tuple(payload.get("affected_project_items")),
            reason=str(payload.get("reason", "")).strip(),
            suggested_change=str(payload.get("suggested_change", "")).strip(),
            requires_superleader_attention=_bool(payload.get("requires_superleader_attention")),
            full_text_ref=_optional_string(payload.get("full_text_ref")),
            metadata=_mapping(payload.get("metadata")),
        )


@dataclass(slots=True)
class SuperLeaderGlobalReview:
    objective_id: str
    planning_round_id: str
    superleader_id: str
    summary: str
    review_id: str = field(default_factory=make_superleader_global_review_id)
    global_conflicts: tuple[str, ...] = ()
    activation_blockers: tuple[str, ...] = ()
    required_reordering: tuple[str, ...] = ()
    required_serialization: tuple[str, ...] = ()
    required_project_item_promotion: tuple[str, ...] = ()
    required_authority_attention: tuple[str, ...] = ()
    recommended_adjustments: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "objective_id": self.objective_id,
            "planning_round_id": self.planning_round_id,
            "superleader_id": self.superleader_id,
            "summary": self.summary,
            "review_id": self.review_id,
            "global_conflicts": list(self.global_conflicts),
            "activation_blockers": list(self.activation_blockers),
            "required_reordering": list(self.required_reordering),
            "required_serialization": list(self.required_serialization),
            "required_project_item_promotion": list(self.required_project_item_promotion),
            "required_authority_attention": list(self.required_authority_attention),
            "recommended_adjustments": list(self.recommended_adjustments),
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "SuperLeaderGlobalReview":
        return cls(
            objective_id=str(payload.get("objective_id", "")).strip(),
            planning_round_id=str(payload.get("planning_round_id", "")).strip(),
            superleader_id=str(payload.get("superleader_id", "")).strip(),
            summary=str(payload.get("summary", "")).strip(),
            review_id=str(payload.get("review_id") or make_superleader_global_review_id()),
            global_conflicts=_string_tuple(payload.get("global_conflicts")),
            activation_blockers=_string_tuple(payload.get("activation_blockers")),
            required_reordering=_string_tuple(payload.get("required_reordering")),
            required_serialization=_string_tuple(payload.get("required_serialization")),
            required_project_item_promotion=_string_tuple(
                payload.get("required_project_item_promotion")
            ),
            required_authority_attention=_string_tuple(payload.get("required_authority_attention")),
            recommended_adjustments=_string_tuple(payload.get("recommended_adjustments")),
            metadata=_mapping(payload.get("metadata")),
        )


@dataclass(slots=True)
class LeaderRevisedPlan(LeaderDraftPlan):
    revision_bundle_ref: str | None = None

    def to_dict(self) -> dict[str, object]:
        payload = super().to_dict()
        payload["revision_bundle_ref"] = self.revision_bundle_ref
        return payload

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "LeaderRevisedPlan":
        base = LeaderDraftPlan.from_payload(payload)
        return cls(
            objective_id=base.objective_id,
            planning_round_id=base.planning_round_id,
            leader_id=base.leader_id,
            lane_id=base.lane_id,
            team_id=base.team_id,
            summary=base.summary,
            plan_id=str(payload.get("plan_id") or payload.get("revised_plan_id") or make_leader_revised_plan_id()),
            sequential_slices=base.sequential_slices,
            parallel_slices=base.parallel_slices,
            project_items=base.project_items,
            shared_hotspots=base.shared_hotspots,
            verification_targets=base.verification_targets,
            authority_risks=base.authority_risks,
            metadata=base.metadata,
            revision_bundle_ref=_optional_string(payload.get("revision_bundle_ref")),
        )


@dataclass(slots=True)
class LeaderDraftPlanDigest:
    plan_id: str
    leader_id: str
    lane_id: str
    team_id: str
    summary: str
    shared_hotspots: tuple[str, ...] = ()
    owned_paths: tuple[str, ...] = ()
    full_text_ref: str | None = None


@dataclass(slots=True)
class LeaderPeerReviewDigest:
    review_id: str
    reviewer_leader_id: str
    target_leader_id: str
    summary: str
    conflict_type: str
    severity: PlanningReviewSeverity
    affected_paths: tuple[str, ...] = ()
    requires_superleader_attention: bool = False
    full_text_ref: str | None = None


@dataclass(slots=True)
class SuperLeaderGlobalReviewDigest:
    review_id: str
    summary: str
    activation_blocker_count: int = 0
    global_conflict_count: int = 0


@dataclass(slots=True)
class LeaderRevisionContextBundle:
    objective_id: str
    planning_round_id: str
    leader_id: str
    draft_plan_refs: tuple[LeaderDraftPlanDigest | LeaderDraftPlan, ...] = ()
    peer_review_digests: tuple[LeaderPeerReviewDigest | LeaderPeerReview, ...] = ()
    superleader_review_digest: SuperLeaderGlobalReviewDigest | SuperLeaderGlobalReview | None = None
    hotspot_conflicts: tuple[str, ...] = ()
    dependency_notices: tuple[str, ...] = ()
    authority_notices: tuple[str, ...] = ()
    project_item_notices: tuple[str, ...] = ()
    full_text_refs: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ActivationGateDecision:
    objective_id: str
    planning_round_id: str
    status: ActivationGateStatus
    summary: str
    decision_id: str = field(default_factory=make_activation_gate_decision_id)
    blockers: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "objective_id": self.objective_id,
            "planning_round_id": self.planning_round_id,
            "status": self.status.value,
            "summary": self.summary,
            "decision_id": self.decision_id,
            "blockers": list(self.blockers),
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "ActivationGateDecision":
        return cls(
            objective_id=str(payload.get("objective_id", "")).strip(),
            planning_round_id=str(payload.get("planning_round_id", "")).strip(),
            status=_coerce_activation_gate_status(payload.get("status")),
            summary=str(payload.get("summary", "")).strip(),
            decision_id=str(payload.get("decision_id") or make_activation_gate_decision_id()),
            blockers=_string_tuple(payload.get("blockers")),
            metadata=_mapping(payload.get("metadata")),
        )


def build_leader_draft_plan_digest(plan: LeaderDraftPlan) -> LeaderDraftPlanDigest:
    owned_paths: list[str] = []
    for slice_ in plan.sequential_slices + plan.parallel_slices:
        owned_paths.extend(slice_.owned_paths)
    ordered_owned_paths = tuple(sorted(dict.fromkeys(owned_paths)))
    return LeaderDraftPlanDigest(
        plan_id=plan.plan_id,
        leader_id=plan.leader_id,
        lane_id=plan.lane_id,
        team_id=plan.team_id,
        summary=plan.summary,
        shared_hotspots=plan.shared_hotspots,
        owned_paths=ordered_owned_paths,
        full_text_ref=f"planning-review://leader-draft/{plan.plan_id}",
    )


def build_leader_peer_review_digest(review: LeaderPeerReview) -> LeaderPeerReviewDigest:
    return LeaderPeerReviewDigest(
        review_id=review.review_id,
        reviewer_leader_id=review.reviewer_leader_id,
        target_leader_id=review.target_leader_id,
        summary=review.summary,
        conflict_type=review.conflict_type,
        severity=review.severity,
        affected_paths=review.affected_paths,
        requires_superleader_attention=review.requires_superleader_attention,
        full_text_ref=review.full_text_ref or f"planning-review://leader-peer/{review.review_id}",
    )


def build_superleader_global_review_digest(
    review: SuperLeaderGlobalReview,
) -> SuperLeaderGlobalReviewDigest:
    return SuperLeaderGlobalReviewDigest(
        review_id=review.review_id,
        summary=review.summary,
        activation_blocker_count=len(review.activation_blockers),
        global_conflict_count=len(review.global_conflicts),
    )
