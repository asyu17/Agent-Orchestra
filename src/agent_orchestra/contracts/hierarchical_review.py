from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any

from agent_orchestra.contracts.ids import make_id


class ReviewItemKind(str, Enum):
    TASK_ITEM = "task_item"
    PROJECT_ITEM = "project_item"


class HierarchicalReviewPhase(str, Enum):
    TEAM_INDEPENDENT_REVIEW = "team_independent_review"
    TEAM_SYNTHESIS = "team_synthesis"
    CROSS_TEAM_LEADER_REVIEW = "cross_team_leader_review"
    SUPERLEADER_SYNTHESIS = "superleader_synthesis"


class HierarchicalReviewActorRole(str, Enum):
    TEAMMATE = "teammate"
    LEADER = "leader"
    SUPERLEADER = "superleader"
    SYSTEM = "system"


class HierarchicalReviewReadMode(str, Enum):
    NONE = "none"
    SUMMARY_ONLY = "summary_only"
    SUMMARY_PLUS_REF = "summary_plus_ref"
    FULL_TEXT = "full_text"


class ReviewFreshnessStatus(str, Enum):
    UNKNOWN = "unknown"
    FRESH = "fresh"
    STALE = "stale"


def _string_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(item) for item in value if item is not None and str(item))


def _metadata_mapping(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): item for key, item in value.items()}


def _coerce_confidence(value: object) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _coerce_int(value: object, *, default: int = 0) -> int:
    try:
        return int(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def _coerce_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _coerce_review_phase(value: object) -> HierarchicalReviewPhase:
    if isinstance(value, HierarchicalReviewPhase):
        return value
    try:
        return HierarchicalReviewPhase(str(value))
    except ValueError:
        return HierarchicalReviewPhase.TEAM_INDEPENDENT_REVIEW


def _optional_review_phase(value: object) -> HierarchicalReviewPhase | None:
    if value is None:
        return None
    if isinstance(value, HierarchicalReviewPhase):
        return value
    try:
        return HierarchicalReviewPhase(str(value))
    except ValueError:
        return None


def _coerce_review_freshness_status(value: object) -> ReviewFreshnessStatus:
    if isinstance(value, ReviewFreshnessStatus):
        return value
    try:
        return ReviewFreshnessStatus(str(value))
    except ValueError:
        return ReviewFreshnessStatus.UNKNOWN


def _coerce_review_read_mode(value: object) -> HierarchicalReviewReadMode:
    if isinstance(value, HierarchicalReviewReadMode):
        return value
    try:
        return HierarchicalReviewReadMode(str(value))
    except ValueError:
        return HierarchicalReviewReadMode.NONE


def make_review_item_id() -> str:
    return make_id("reviewitem")


def make_team_position_review_id() -> str:
    return make_id("teampos")


def make_cross_team_leader_review_id() -> str:
    return make_id("crossreview")


def make_superleader_synthesis_id() -> str:
    return make_id("synthesis")


@dataclass(slots=True)
class HierarchicalReviewActor:
    actor_id: str
    role: HierarchicalReviewActorRole
    team_id: str | None = None

    @classmethod
    def system(cls) -> "HierarchicalReviewActor":
        return cls(
            actor_id="hierarchical-review-system",
            role=HierarchicalReviewActorRole.SYSTEM,
        )


@dataclass(slots=True)
class HierarchicalReviewAccessDecision:
    allowed: bool
    read_mode: HierarchicalReviewReadMode = HierarchicalReviewReadMode.NONE
    reason: str = ""


@dataclass(slots=True)
class HierarchicalReviewPolicy:
    same_team_read_mode: HierarchicalReviewReadMode = HierarchicalReviewReadMode.FULL_TEXT
    cross_team_leader_read_mode: HierarchicalReviewReadMode = (
        HierarchicalReviewReadMode.SUMMARY_PLUS_REF
    )
    cross_team_cross_review_read_mode: HierarchicalReviewReadMode = (
        HierarchicalReviewReadMode.SUMMARY_PLUS_REF
    )
    leader_superleader_synthesis_read_mode: HierarchicalReviewReadMode = (
        HierarchicalReviewReadMode.SUMMARY_ONLY
    )
    superleader_read_mode: HierarchicalReviewReadMode = HierarchicalReviewReadMode.FULL_TEXT

    @classmethod
    def default(cls) -> "HierarchicalReviewPolicy":
        return cls()

    @staticmethod
    def _same_team(actor: HierarchicalReviewActor, team_id: str | None) -> bool:
        return actor.team_id is not None and team_id is not None and actor.team_id == team_id

    def review_item_read_access(
        self,
        *,
        actor: HierarchicalReviewActor,
        item: "ReviewItemRef",
    ) -> HierarchicalReviewAccessDecision:
        if actor.role in {
            HierarchicalReviewActorRole.SYSTEM,
            HierarchicalReviewActorRole.SUPERLEADER,
        }:
            return HierarchicalReviewAccessDecision(
                allowed=True,
                read_mode=self.superleader_read_mode,
            )
        if actor.role == HierarchicalReviewActorRole.LEADER:
            if item.team_id is None or self._same_team(actor, item.team_id):
                return HierarchicalReviewAccessDecision(
                    allowed=True,
                    read_mode=self.same_team_read_mode,
                )
            return HierarchicalReviewAccessDecision(
                allowed=True,
                read_mode=HierarchicalReviewReadMode.SUMMARY_ONLY,
            )
        if item.team_id is None:
            return HierarchicalReviewAccessDecision(
                allowed=True,
                read_mode=HierarchicalReviewReadMode.SUMMARY_ONLY,
            )
        if self._same_team(actor, item.team_id):
            return HierarchicalReviewAccessDecision(
                allowed=True,
                read_mode=self.same_team_read_mode,
            )
        return HierarchicalReviewAccessDecision(
            allowed=False,
            reason="teammates cannot list foreign-team review items by default",
        )

    def team_position_write_access(
        self,
        *,
        actor: HierarchicalReviewActor,
        team_id: str,
    ) -> HierarchicalReviewAccessDecision:
        if actor.role == HierarchicalReviewActorRole.SYSTEM:
            return HierarchicalReviewAccessDecision(allowed=True)
        if actor.role == HierarchicalReviewActorRole.LEADER and self._same_team(actor, team_id):
            return HierarchicalReviewAccessDecision(allowed=True)
        return HierarchicalReviewAccessDecision(
            allowed=False,
            reason="team position reviews are leader-owned within the matching team",
        )

    def team_position_read_access(
        self,
        *,
        actor: HierarchicalReviewActor,
        review: "TeamPositionReview",
    ) -> HierarchicalReviewAccessDecision:
        if actor.role in {
            HierarchicalReviewActorRole.SYSTEM,
            HierarchicalReviewActorRole.SUPERLEADER,
        }:
            return HierarchicalReviewAccessDecision(
                allowed=True,
                read_mode=self.superleader_read_mode,
            )
        if self._same_team(actor, review.team_id):
            return HierarchicalReviewAccessDecision(
                allowed=True,
                read_mode=self.same_team_read_mode,
            )
        if actor.role == HierarchicalReviewActorRole.LEADER:
            if review.item_kind != ReviewItemKind.PROJECT_ITEM:
                return HierarchicalReviewAccessDecision(
                    allowed=False,
                    reason="leaders cannot list foreign team task-item reviews by default",
                )
            return HierarchicalReviewAccessDecision(
                allowed=True,
                read_mode=self.cross_team_leader_read_mode,
            )
        return HierarchicalReviewAccessDecision(
            allowed=False,
            reason="teammates cannot read foreign team leader reviews by default",
        )

    def cross_team_leader_write_access(
        self,
        *,
        actor: HierarchicalReviewActor,
        reviewer_team_id: str,
        target_team_id: str,
    ) -> HierarchicalReviewAccessDecision:
        if reviewer_team_id == target_team_id:
            return HierarchicalReviewAccessDecision(
                allowed=False,
                reason="cross-team reviews must target a different team",
            )
        if actor.role == HierarchicalReviewActorRole.SYSTEM:
            return HierarchicalReviewAccessDecision(allowed=True)
        if actor.role == HierarchicalReviewActorRole.LEADER and self._same_team(
            actor, reviewer_team_id
        ):
            return HierarchicalReviewAccessDecision(allowed=True)
        return HierarchicalReviewAccessDecision(
            allowed=False,
            reason="cross-team leader reviews are leader-owned by the reviewer team",
        )

    def cross_team_leader_read_access(
        self,
        *,
        actor: HierarchicalReviewActor,
        review: "CrossTeamLeaderReview",
    ) -> HierarchicalReviewAccessDecision:
        if actor.role in {
            HierarchicalReviewActorRole.SYSTEM,
            HierarchicalReviewActorRole.SUPERLEADER,
        }:
            return HierarchicalReviewAccessDecision(
                allowed=True,
                read_mode=self.superleader_read_mode,
            )
        if actor.role == HierarchicalReviewActorRole.LEADER:
            if self._same_team(actor, review.reviewer_team_id):
                return HierarchicalReviewAccessDecision(
                    allowed=True,
                    read_mode=self.same_team_read_mode,
                )
            return HierarchicalReviewAccessDecision(
                allowed=True,
                read_mode=self.cross_team_cross_review_read_mode,
            )
        return HierarchicalReviewAccessDecision(
            allowed=False,
            reason="teammates cannot list cross-team leader reviews by default",
        )

    def superleader_synthesis_write_access(
        self,
        *,
        actor: HierarchicalReviewActor,
    ) -> HierarchicalReviewAccessDecision:
        if actor.role in {
            HierarchicalReviewActorRole.SYSTEM,
            HierarchicalReviewActorRole.SUPERLEADER,
        }:
            return HierarchicalReviewAccessDecision(
                allowed=True,
            )
        return HierarchicalReviewAccessDecision(
            allowed=False,
            reason="superleader synthesis is reserved for superleader/runtime actors",
        )

    def create_review_item_access(
        self,
        *,
        actor: HierarchicalReviewActor,
        item_kind: ReviewItemKind,
        item_team_id: str | None,
    ) -> HierarchicalReviewAccessDecision:
        if actor.role in {
            HierarchicalReviewActorRole.SYSTEM,
            HierarchicalReviewActorRole.SUPERLEADER,
        }:
            return HierarchicalReviewAccessDecision(allowed=True)
        if actor.role == HierarchicalReviewActorRole.LEADER and (
            item_team_id is None or actor.team_id == item_team_id
        ):
            return HierarchicalReviewAccessDecision(allowed=True)
        return HierarchicalReviewAccessDecision(
            allowed=False,
            reason=f"{item_kind.value} creation is leader-owned by default",
        )

    def superleader_synthesis_read_access(
        self,
        *,
        actor: HierarchicalReviewActor,
        synthesis: "SuperLeaderSynthesis",
    ) -> HierarchicalReviewAccessDecision:
        if actor.role in {
            HierarchicalReviewActorRole.SYSTEM,
            HierarchicalReviewActorRole.SUPERLEADER,
        }:
            return HierarchicalReviewAccessDecision(
                allowed=True,
                read_mode=self.superleader_read_mode,
            )
        if actor.role == HierarchicalReviewActorRole.LEADER:
            return HierarchicalReviewAccessDecision(
                allowed=True,
                read_mode=self.leader_superleader_synthesis_read_mode,
            )
        return HierarchicalReviewAccessDecision(
            allowed=False,
            reason="teammates cannot list superleader synthesis by default",
        )


@dataclass(slots=True)
class HierarchicalReviewDigestVisibility:
    visibility_scope: str
    read_mode: HierarchicalReviewReadMode = HierarchicalReviewReadMode.SUMMARY_ONLY
    ref_visible: bool = False


@dataclass(slots=True)
class HierarchicalReviewDigestSnapshot:
    item_id: str
    item_kind: ReviewItemKind
    current_phase: HierarchicalReviewPhase
    team_position_review_count: int = 0
    cross_team_leader_review_count: int = 0
    has_superleader_synthesis: bool = False
    latest_activity_at: str | None = None
    freshness: "ReviewFreshnessState" = field(default_factory=lambda: ReviewFreshnessState())


@dataclass(slots=True)
class TeamPositionReviewDigest:
    item_id: str
    review_ref: str | None
    summary: str
    visibility: HierarchicalReviewDigestVisibility
    based_on_task_review_revision_count: int = 0
    is_latest_for_scope: bool = False


@dataclass(slots=True)
class CrossTeamLeaderReviewDigest:
    item_id: str
    review_ref: str | None
    summary: str
    visibility: HierarchicalReviewDigestVisibility
    is_latest_for_scope: bool = False


@dataclass(slots=True)
class SuperLeaderSynthesisDigest:
    item_id: str
    review_ref: str | None
    summary: str
    visibility: HierarchicalReviewDigestVisibility
    based_on_team_position_review_count: int = 0
    based_on_cross_team_review_count: int = 0


@dataclass(slots=True)
class HierarchicalReviewDigestView:
    item: "ReviewItemRef"
    snapshot: HierarchicalReviewDigestSnapshot
    visibility: HierarchicalReviewDigestVisibility
    team_position_digests: tuple[TeamPositionReviewDigest, ...] = ()
    cross_team_leader_digests: tuple[CrossTeamLeaderReviewDigest, ...] = ()
    superleader_synthesis_digest: SuperLeaderSynthesisDigest | None = None


def _latest_reviewed_at(values: tuple[str | None, ...]) -> str | None:
    present = [value for value in values if value]
    if not present:
        return None
    return max(present)


def build_hierarchical_review_digest_snapshot(
    item: "ReviewItemRef",
    *,
    team_position_reviews: tuple["TeamPositionReview", ...] = (),
    cross_team_leader_reviews: tuple["CrossTeamLeaderReview", ...] = (),
    superleader_synthesis: "SuperLeaderSynthesis | None" = None,
) -> HierarchicalReviewDigestSnapshot:
    current_phase = item.phase
    if team_position_reviews:
        current_phase = HierarchicalReviewPhase.TEAM_SYNTHESIS
    if cross_team_leader_reviews:
        current_phase = HierarchicalReviewPhase.CROSS_TEAM_LEADER_REVIEW
    if superleader_synthesis is not None:
        current_phase = HierarchicalReviewPhase.SUPERLEADER_SYNTHESIS
    latest_activity_at = _latest_reviewed_at(
        tuple(review.reviewed_at for review in team_position_reviews)
        + tuple(review.reviewed_at for review in cross_team_leader_reviews)
        + ((superleader_synthesis.synthesized_at,) if superleader_synthesis is not None else ())
        + ((item.phase_entered_at,) if item.phase_entered_at is not None else ())
    )
    freshness = item.freshness
    if freshness.status == ReviewFreshnessStatus.UNKNOWN and current_phase != item.phase:
        freshness = ReviewFreshnessState(
            status=ReviewFreshnessStatus.STALE,
            last_evaluated_at=latest_activity_at,
            last_reviewed_at=latest_activity_at,
            needs_refresh=True,
            reason="digest snapshot observed newer review-layer activity than item phase truth",
        )
    return HierarchicalReviewDigestSnapshot(
        item_id=item.item_id,
        item_kind=item.item_kind,
        current_phase=current_phase,
        team_position_review_count=len(team_position_reviews),
        cross_team_leader_review_count=len(cross_team_leader_reviews),
        has_superleader_synthesis=superleader_synthesis is not None,
        latest_activity_at=latest_activity_at,
        freshness=freshness,
    )


def build_team_position_review_digest(
    *,
    item: "ReviewItemRef",
    review: "TeamPositionReview",
    snapshot: HierarchicalReviewDigestSnapshot,
    team_position_reviews: tuple["TeamPositionReview", ...] = (),
    visibility: HierarchicalReviewDigestVisibility,
) -> TeamPositionReviewDigest:
    latest_review = max(
        team_position_reviews or (review,),
        key=lambda current: (current.reviewed_at, current.position_review_id),
    )
    review_ref = review.position_review_id if visibility.ref_visible else None
    return TeamPositionReviewDigest(
        item_id=item.item_id,
        review_ref=review_ref,
        summary=review.summary or review.team_stance,
        visibility=visibility,
        based_on_task_review_revision_count=len(review.based_on_task_review_revision_ids),
        is_latest_for_scope=latest_review.position_review_id == review.position_review_id
        and snapshot.current_phase
        in {
            HierarchicalReviewPhase.TEAM_SYNTHESIS,
            HierarchicalReviewPhase.CROSS_TEAM_LEADER_REVIEW,
            HierarchicalReviewPhase.SUPERLEADER_SYNTHESIS,
        },
    )


def build_cross_team_leader_review_digest(
    *,
    item: "ReviewItemRef",
    review: "CrossTeamLeaderReview",
    snapshot: HierarchicalReviewDigestSnapshot,
    cross_team_leader_reviews: tuple["CrossTeamLeaderReview", ...] = (),
    visibility: HierarchicalReviewDigestVisibility,
) -> CrossTeamLeaderReviewDigest:
    latest_review = max(
        cross_team_leader_reviews or (review,),
        key=lambda current: (current.reviewed_at, current.cross_review_id),
    )
    review_ref = review.cross_review_id if visibility.ref_visible else None
    return CrossTeamLeaderReviewDigest(
        item_id=item.item_id,
        review_ref=review_ref,
        summary=(
            review.what_changed_in_my_understanding
            or review.suggested_adjustment
            or review.stance
        ),
        visibility=visibility,
        is_latest_for_scope=latest_review.cross_review_id == review.cross_review_id
        and snapshot.current_phase
        in {
            HierarchicalReviewPhase.CROSS_TEAM_LEADER_REVIEW,
            HierarchicalReviewPhase.SUPERLEADER_SYNTHESIS,
        },
    )


def build_superleader_synthesis_digest(
    *,
    item: "ReviewItemRef",
    synthesis: "SuperLeaderSynthesis",
    snapshot: HierarchicalReviewDigestSnapshot,
    visibility: HierarchicalReviewDigestVisibility,
) -> SuperLeaderSynthesisDigest:
    review_ref = synthesis.synthesis_id if visibility.ref_visible else None
    return SuperLeaderSynthesisDigest(
        item_id=item.item_id,
        review_ref=review_ref,
        summary=synthesis.final_position,
        visibility=visibility,
        based_on_team_position_review_count=len(synthesis.based_on_team_position_review_ids),
        based_on_cross_team_review_count=len(synthesis.based_on_cross_team_review_ids),
    )


@dataclass(slots=True)
class ReviewPhaseTransition:
    to_phase: HierarchicalReviewPhase
    from_phase: HierarchicalReviewPhase | None = None
    transitioned_at: str | None = None
    actor_id: str | None = None
    trigger: str = ""
    source_artifact_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "from_phase": self.from_phase.value if self.from_phase is not None else None,
            "to_phase": self.to_phase.value,
            "transitioned_at": self.transitioned_at,
            "actor_id": self.actor_id,
            "trigger": self.trigger,
            "source_artifact_id": self.source_artifact_id,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_payload(cls, payload: object) -> "ReviewPhaseTransition | None":
        if isinstance(payload, cls):
            return payload
        if not isinstance(payload, Mapping):
            return None
        to_phase = _optional_review_phase(payload.get("to_phase"))
        if to_phase is None:
            return None
        return cls(
            to_phase=to_phase,
            from_phase=_optional_review_phase(payload.get("from_phase")),
            transitioned_at=_optional_string(payload.get("transitioned_at")),
            actor_id=_optional_string(payload.get("actor_id")),
            trigger=str(payload.get("trigger", "")),
            source_artifact_id=_optional_string(payload.get("source_artifact_id")),
            metadata=_metadata_mapping(payload.get("metadata", {})),
        )


@dataclass(slots=True)
class ReviewFreshnessState:
    status: ReviewFreshnessStatus = ReviewFreshnessStatus.UNKNOWN
    last_evaluated_at: str | None = None
    last_reviewed_at: str | None = None
    stale_after_at: str | None = None
    needs_refresh: bool = False
    freshness_token: str | None = None
    stale_reviewer_ids: tuple[str, ...] = ()
    reason: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "last_evaluated_at": self.last_evaluated_at,
            "last_reviewed_at": self.last_reviewed_at,
            "stale_after_at": self.stale_after_at,
            "needs_refresh": self.needs_refresh,
            "freshness_token": self.freshness_token,
            "stale_reviewer_ids": list(self.stale_reviewer_ids),
            "reason": self.reason,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_payload(cls, payload: object) -> "ReviewFreshnessState":
        if isinstance(payload, cls):
            return payload
        if not isinstance(payload, Mapping):
            return cls()
        return cls(
            status=_coerce_review_freshness_status(payload.get("status")),
            last_evaluated_at=_optional_string(payload.get("last_evaluated_at")),
            last_reviewed_at=_optional_string(payload.get("last_reviewed_at")),
            stale_after_at=_optional_string(payload.get("stale_after_at")),
            needs_refresh=_coerce_bool(payload.get("needs_refresh", False)),
            freshness_token=_optional_string(payload.get("freshness_token")),
            stale_reviewer_ids=_string_tuple(payload.get("stale_reviewer_ids", ())),
            reason=str(payload.get("reason", "")),
            metadata=_metadata_mapping(payload.get("metadata", {})),
        )


@dataclass(slots=True)
class ReviewItemRef:
    item_id: str
    item_kind: ReviewItemKind
    objective_id: str
    lane_id: str | None = None
    team_id: str | None = None
    source_task_id: str | None = None
    title: str = ""
    summary: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    phase: HierarchicalReviewPhase = HierarchicalReviewPhase.TEAM_INDEPENDENT_REVIEW
    phase_entered_at: str | None = None
    phase_transition_count: int = 0
    last_transition: ReviewPhaseTransition | None = None
    freshness: ReviewFreshnessState = field(default_factory=ReviewFreshnessState)

    def to_dict(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "item_kind": self.item_kind.value,
            "objective_id": self.objective_id,
            "lane_id": self.lane_id,
            "team_id": self.team_id,
            "source_task_id": self.source_task_id,
            "title": self.title,
            "summary": self.summary,
            "metadata": dict(self.metadata),
            "phase": self.phase.value,
            "phase_entered_at": self.phase_entered_at,
            "phase_transition_count": self.phase_transition_count,
            "last_transition": (
                self.last_transition.to_dict() if self.last_transition is not None else None
            ),
            "freshness": self.freshness.to_dict(),
        }

    @classmethod
    def from_payload(cls, payload: object) -> "ReviewItemRef | None":
        if isinstance(payload, cls):
            return payload
        if not isinstance(payload, Mapping):
            return None
        item_id = payload.get("item_id")
        objective_id = payload.get("objective_id")
        item_kind_raw = payload.get("item_kind")
        if not all(isinstance(value, str) and value.strip() for value in (item_id, objective_id)):
            return None
        try:
            item_kind = (
                item_kind_raw
                if isinstance(item_kind_raw, ReviewItemKind)
                else ReviewItemKind(str(item_kind_raw))
            )
        except ValueError:
            return None
        last_transition = ReviewPhaseTransition.from_payload(payload.get("last_transition"))
        return cls(
            item_id=item_id.strip(),
            item_kind=item_kind,
            objective_id=objective_id.strip(),
            lane_id=str(payload["lane_id"]) if payload.get("lane_id") is not None else None,
            team_id=str(payload["team_id"]) if payload.get("team_id") is not None else None,
            source_task_id=(
                str(payload["source_task_id"])
                if payload.get("source_task_id") is not None
                else None
            ),
            title=str(payload.get("title", "")),
            summary=str(payload.get("summary", "")),
            metadata=_metadata_mapping(payload.get("metadata", {})),
            phase=_coerce_review_phase(
                payload.get("phase", HierarchicalReviewPhase.TEAM_INDEPENDENT_REVIEW.value)
            ),
            phase_entered_at=(
                _optional_string(payload.get("phase_entered_at"))
                or (last_transition.transitioned_at if last_transition is not None else None)
            ),
            phase_transition_count=_coerce_int(payload.get("phase_transition_count"), default=0),
            last_transition=last_transition,
            freshness=ReviewFreshnessState.from_payload(payload.get("freshness", {})),
        )


@dataclass(slots=True)
class TeamPositionReview:
    position_review_id: str
    item_id: str
    item_kind: ReviewItemKind
    team_id: str
    leader_id: str
    reviewed_at: str
    based_on_task_review_revision_ids: tuple[str, ...] = ()
    team_stance: str = ""
    summary: str = ""
    key_risks: tuple[str, ...] = ()
    key_dependencies: tuple[str, ...] = ()
    recommended_next_action: str = ""
    confidence: float | None = None
    evidence_refs: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "position_review_id": self.position_review_id,
            "item_id": self.item_id,
            "item_kind": self.item_kind.value,
            "team_id": self.team_id,
            "leader_id": self.leader_id,
            "reviewed_at": self.reviewed_at,
            "based_on_task_review_revision_ids": list(self.based_on_task_review_revision_ids),
            "team_stance": self.team_stance,
            "summary": self.summary,
            "key_risks": list(self.key_risks),
            "key_dependencies": list(self.key_dependencies),
            "recommended_next_action": self.recommended_next_action,
            "confidence": self.confidence,
            "evidence_refs": list(self.evidence_refs),
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_payload(cls, payload: object) -> "TeamPositionReview | None":
        if isinstance(payload, cls):
            return payload
        if not isinstance(payload, Mapping):
            return None
        position_review_id = payload.get("position_review_id")
        item_id = payload.get("item_id")
        team_id = payload.get("team_id")
        leader_id = payload.get("leader_id")
        reviewed_at = payload.get("reviewed_at")
        item_kind_raw = payload.get("item_kind")
        if not all(
            isinstance(value, str) and value.strip()
            for value in (position_review_id, item_id, team_id, leader_id, reviewed_at)
        ):
            return None
        try:
            item_kind = (
                item_kind_raw
                if isinstance(item_kind_raw, ReviewItemKind)
                else ReviewItemKind(str(item_kind_raw))
            )
        except ValueError:
            return None
        return cls(
            position_review_id=position_review_id.strip(),
            item_id=item_id.strip(),
            item_kind=item_kind,
            team_id=team_id.strip(),
            leader_id=leader_id.strip(),
            reviewed_at=reviewed_at.strip(),
            based_on_task_review_revision_ids=_string_tuple(
                payload.get("based_on_task_review_revision_ids", ())
            ),
            team_stance=str(payload.get("team_stance", "")),
            summary=str(payload.get("summary", "")),
            key_risks=_string_tuple(payload.get("key_risks", ())),
            key_dependencies=_string_tuple(payload.get("key_dependencies", ())),
            recommended_next_action=str(payload.get("recommended_next_action", "")),
            confidence=_coerce_confidence(payload.get("confidence")),
            evidence_refs=_string_tuple(payload.get("evidence_refs", ())),
            metadata=_metadata_mapping(payload.get("metadata", {})),
        )


@dataclass(slots=True)
class CrossTeamLeaderReview:
    cross_review_id: str
    item_id: str
    item_kind: ReviewItemKind
    reviewer_team_id: str
    reviewer_leader_id: str
    target_team_id: str
    target_position_review_id: str
    reviewed_at: str
    stance: str = ""
    agreement_level: str = ""
    what_changed_in_my_understanding: str = ""
    challenge_or_support: str = ""
    suggested_adjustment: str = ""
    confidence: float | None = None
    evidence_refs: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "cross_review_id": self.cross_review_id,
            "item_id": self.item_id,
            "item_kind": self.item_kind.value,
            "reviewer_team_id": self.reviewer_team_id,
            "reviewer_leader_id": self.reviewer_leader_id,
            "target_team_id": self.target_team_id,
            "target_position_review_id": self.target_position_review_id,
            "reviewed_at": self.reviewed_at,
            "stance": self.stance,
            "agreement_level": self.agreement_level,
            "what_changed_in_my_understanding": self.what_changed_in_my_understanding,
            "challenge_or_support": self.challenge_or_support,
            "suggested_adjustment": self.suggested_adjustment,
            "confidence": self.confidence,
            "evidence_refs": list(self.evidence_refs),
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_payload(cls, payload: object) -> "CrossTeamLeaderReview | None":
        if isinstance(payload, cls):
            return payload
        if not isinstance(payload, Mapping):
            return None
        required_values = (
            payload.get("cross_review_id"),
            payload.get("item_id"),
            payload.get("reviewer_team_id"),
            payload.get("reviewer_leader_id"),
            payload.get("target_team_id"),
            payload.get("target_position_review_id"),
            payload.get("reviewed_at"),
        )
        if not all(isinstance(value, str) and value.strip() for value in required_values):
            return None
        item_kind_raw = payload.get("item_kind")
        try:
            item_kind = (
                item_kind_raw
                if isinstance(item_kind_raw, ReviewItemKind)
                else ReviewItemKind(str(item_kind_raw))
            )
        except ValueError:
            return None
        return cls(
            cross_review_id=str(payload["cross_review_id"]).strip(),
            item_id=str(payload["item_id"]).strip(),
            item_kind=item_kind,
            reviewer_team_id=str(payload["reviewer_team_id"]).strip(),
            reviewer_leader_id=str(payload["reviewer_leader_id"]).strip(),
            target_team_id=str(payload["target_team_id"]).strip(),
            target_position_review_id=str(payload["target_position_review_id"]).strip(),
            reviewed_at=str(payload["reviewed_at"]).strip(),
            stance=str(payload.get("stance", "")),
            agreement_level=str(payload.get("agreement_level", "")),
            what_changed_in_my_understanding=str(
                payload.get("what_changed_in_my_understanding", "")
            ),
            challenge_or_support=str(payload.get("challenge_or_support", "")),
            suggested_adjustment=str(payload.get("suggested_adjustment", "")),
            confidence=_coerce_confidence(payload.get("confidence")),
            evidence_refs=_string_tuple(payload.get("evidence_refs", ())),
            metadata=_metadata_mapping(payload.get("metadata", {})),
        )


@dataclass(slots=True)
class SuperLeaderSynthesis:
    synthesis_id: str
    item_id: str
    item_kind: ReviewItemKind
    superleader_id: str
    synthesized_at: str
    based_on_team_position_review_ids: tuple[str, ...] = ()
    based_on_cross_team_review_ids: tuple[str, ...] = ()
    final_position: str = ""
    accepted_risks: tuple[str, ...] = ()
    rejected_paths: tuple[str, ...] = ()
    open_questions: tuple[str, ...] = ()
    next_actions: tuple[str, ...] = ()
    confidence: float | None = None
    evidence_refs: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "synthesis_id": self.synthesis_id,
            "item_id": self.item_id,
            "item_kind": self.item_kind.value,
            "superleader_id": self.superleader_id,
            "synthesized_at": self.synthesized_at,
            "based_on_team_position_review_ids": list(self.based_on_team_position_review_ids),
            "based_on_cross_team_review_ids": list(self.based_on_cross_team_review_ids),
            "final_position": self.final_position,
            "accepted_risks": list(self.accepted_risks),
            "rejected_paths": list(self.rejected_paths),
            "open_questions": list(self.open_questions),
            "next_actions": list(self.next_actions),
            "confidence": self.confidence,
            "evidence_refs": list(self.evidence_refs),
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_payload(cls, payload: object) -> "SuperLeaderSynthesis | None":
        if isinstance(payload, cls):
            return payload
        if not isinstance(payload, Mapping):
            return None
        required_values = (
            payload.get("synthesis_id"),
            payload.get("item_id"),
            payload.get("superleader_id"),
            payload.get("synthesized_at"),
        )
        if not all(isinstance(value, str) and value.strip() for value in required_values):
            return None
        item_kind_raw = payload.get("item_kind")
        try:
            item_kind = (
                item_kind_raw
                if isinstance(item_kind_raw, ReviewItemKind)
                else ReviewItemKind(str(item_kind_raw))
            )
        except ValueError:
            return None
        return cls(
            synthesis_id=str(payload["synthesis_id"]).strip(),
            item_id=str(payload["item_id"]).strip(),
            item_kind=item_kind,
            superleader_id=str(payload["superleader_id"]).strip(),
            synthesized_at=str(payload["synthesized_at"]).strip(),
            based_on_team_position_review_ids=_string_tuple(
                payload.get("based_on_team_position_review_ids", ())
            ),
            based_on_cross_team_review_ids=_string_tuple(
                payload.get("based_on_cross_team_review_ids", ())
            ),
            final_position=str(payload.get("final_position", "")),
            accepted_risks=_string_tuple(payload.get("accepted_risks", ())),
            rejected_paths=_string_tuple(payload.get("rejected_paths", ())),
            open_questions=_string_tuple(payload.get("open_questions", ())),
            next_actions=_string_tuple(payload.get("next_actions", ())),
            confidence=_coerce_confidence(payload.get("confidence")),
            evidence_refs=_string_tuple(payload.get("evidence_refs", ())),
            metadata=_metadata_mapping(payload.get("metadata", {})),
        )


@dataclass(slots=True)
class HierarchicalReviewDigestVisibility:
    visibility_scope: str = "control-private"
    read_mode: HierarchicalReviewReadMode = HierarchicalReviewReadMode.SUMMARY_ONLY
    ref_visible: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "visibility_scope": self.visibility_scope,
            "read_mode": self.read_mode.value,
            "ref_visible": self.ref_visible,
        }

    @classmethod
    def from_payload(cls, payload: object) -> "HierarchicalReviewDigestVisibility":
        if isinstance(payload, cls):
            return payload
        if not isinstance(payload, Mapping):
            return cls()
        return cls(
            visibility_scope=str(payload.get("visibility_scope", "control-private")),
            read_mode=_coerce_review_read_mode(
                payload.get("read_mode", HierarchicalReviewReadMode.SUMMARY_ONLY.value)
            ),
            ref_visible=_coerce_bool(payload.get("ref_visible", False)),
        )


@dataclass(slots=True)
class HierarchicalReviewDigestSnapshot:
    current_phase: HierarchicalReviewPhase = HierarchicalReviewPhase.TEAM_INDEPENDENT_REVIEW
    freshness: ReviewFreshnessState = field(default_factory=ReviewFreshnessState)
    team_position_review_count: int = 0
    cross_team_leader_review_count: int = 0
    has_superleader_synthesis: bool = False
    last_team_position_reviewed_at: str | None = None
    last_cross_team_reviewed_at: str | None = None
    last_superleader_synthesized_at: str | None = None
    latest_activity_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "current_phase": self.current_phase.value,
            "freshness": self.freshness.to_dict(),
            "team_position_review_count": self.team_position_review_count,
            "cross_team_leader_review_count": self.cross_team_leader_review_count,
            "has_superleader_synthesis": self.has_superleader_synthesis,
            "last_team_position_reviewed_at": self.last_team_position_reviewed_at,
            "last_cross_team_reviewed_at": self.last_cross_team_reviewed_at,
            "last_superleader_synthesized_at": self.last_superleader_synthesized_at,
            "latest_activity_at": self.latest_activity_at,
        }

    @classmethod
    def from_payload(cls, payload: object) -> "HierarchicalReviewDigestSnapshot":
        if isinstance(payload, cls):
            return payload
        if not isinstance(payload, Mapping):
            return cls()
        return cls(
            current_phase=_coerce_review_phase(
                payload.get(
                    "current_phase",
                    HierarchicalReviewPhase.TEAM_INDEPENDENT_REVIEW.value,
                )
            ),
            freshness=ReviewFreshnessState.from_payload(payload.get("freshness", {})),
            team_position_review_count=_coerce_int(
                payload.get("team_position_review_count"),
                default=0,
            ),
            cross_team_leader_review_count=_coerce_int(
                payload.get("cross_team_leader_review_count"),
                default=0,
            ),
            has_superleader_synthesis=_coerce_bool(
                payload.get("has_superleader_synthesis", False)
            ),
            last_team_position_reviewed_at=_optional_string(
                payload.get("last_team_position_reviewed_at")
            ),
            last_cross_team_reviewed_at=_optional_string(
                payload.get("last_cross_team_reviewed_at")
            ),
            last_superleader_synthesized_at=_optional_string(
                payload.get("last_superleader_synthesized_at")
            ),
            latest_activity_at=_optional_string(payload.get("latest_activity_at")),
        )


@dataclass(slots=True)
class TeamPositionReviewDigest:
    digest_id: str
    item_id: str
    item_kind: ReviewItemKind
    objective_id: str
    item_title: str = ""
    team_id: str = ""
    leader_id: str = ""
    reviewed_at: str = ""
    team_stance: str = ""
    summary: str = ""
    recommended_next_action: str = ""
    confidence: float | None = None
    based_on_task_review_revision_count: int = 0
    review_ref: str | None = None
    is_latest_for_scope: bool = False
    snapshot: HierarchicalReviewDigestSnapshot = field(
        default_factory=HierarchicalReviewDigestSnapshot
    )
    visibility: HierarchicalReviewDigestVisibility = field(
        default_factory=HierarchicalReviewDigestVisibility
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "digest_id": self.digest_id,
            "item_id": self.item_id,
            "item_kind": self.item_kind.value,
            "objective_id": self.objective_id,
            "item_title": self.item_title,
            "team_id": self.team_id,
            "leader_id": self.leader_id,
            "reviewed_at": self.reviewed_at,
            "team_stance": self.team_stance,
            "summary": self.summary,
            "recommended_next_action": self.recommended_next_action,
            "confidence": self.confidence,
            "based_on_task_review_revision_count": self.based_on_task_review_revision_count,
            "review_ref": self.review_ref,
            "is_latest_for_scope": self.is_latest_for_scope,
            "snapshot": self.snapshot.to_dict(),
            "visibility": self.visibility.to_dict(),
        }

    @classmethod
    def from_payload(cls, payload: object) -> "TeamPositionReviewDigest | None":
        if isinstance(payload, cls):
            return payload
        if not isinstance(payload, Mapping):
            return None
        digest_id = _optional_string(payload.get("digest_id"))
        item_id = _optional_string(payload.get("item_id"))
        objective_id = _optional_string(payload.get("objective_id"))
        if digest_id is None or item_id is None or objective_id is None:
            return None
        item_kind_raw = payload.get("item_kind")
        try:
            item_kind = (
                item_kind_raw
                if isinstance(item_kind_raw, ReviewItemKind)
                else ReviewItemKind(str(item_kind_raw))
            )
        except ValueError:
            return None
        return cls(
            digest_id=digest_id,
            item_id=item_id,
            item_kind=item_kind,
            objective_id=objective_id,
            item_title=str(payload.get("item_title", "")),
            team_id=str(payload.get("team_id", "")),
            leader_id=str(payload.get("leader_id", "")),
            reviewed_at=str(payload.get("reviewed_at", "")),
            team_stance=str(payload.get("team_stance", "")),
            summary=str(payload.get("summary", "")),
            recommended_next_action=str(payload.get("recommended_next_action", "")),
            confidence=_coerce_confidence(payload.get("confidence")),
            based_on_task_review_revision_count=_coerce_int(
                payload.get("based_on_task_review_revision_count"),
                default=0,
            ),
            review_ref=_optional_string(payload.get("review_ref")),
            is_latest_for_scope=_coerce_bool(payload.get("is_latest_for_scope", False)),
            snapshot=HierarchicalReviewDigestSnapshot.from_payload(payload.get("snapshot", {})),
            visibility=HierarchicalReviewDigestVisibility.from_payload(
                payload.get("visibility", {})
            ),
        )


@dataclass(slots=True)
class CrossTeamLeaderReviewDigest:
    digest_id: str
    item_id: str
    item_kind: ReviewItemKind
    objective_id: str
    item_title: str = ""
    reviewer_team_id: str = ""
    reviewer_leader_id: str = ""
    target_team_id: str = ""
    reviewed_at: str = ""
    stance: str = ""
    agreement_level: str = ""
    summary: str = ""
    suggested_adjustment: str = ""
    confidence: float | None = None
    target_position_review_ref: str | None = None
    review_ref: str | None = None
    is_latest_for_scope: bool = False
    snapshot: HierarchicalReviewDigestSnapshot = field(
        default_factory=HierarchicalReviewDigestSnapshot
    )
    visibility: HierarchicalReviewDigestVisibility = field(
        default_factory=HierarchicalReviewDigestVisibility
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "digest_id": self.digest_id,
            "item_id": self.item_id,
            "item_kind": self.item_kind.value,
            "objective_id": self.objective_id,
            "item_title": self.item_title,
            "reviewer_team_id": self.reviewer_team_id,
            "reviewer_leader_id": self.reviewer_leader_id,
            "target_team_id": self.target_team_id,
            "reviewed_at": self.reviewed_at,
            "stance": self.stance,
            "agreement_level": self.agreement_level,
            "summary": self.summary,
            "suggested_adjustment": self.suggested_adjustment,
            "confidence": self.confidence,
            "target_position_review_ref": self.target_position_review_ref,
            "review_ref": self.review_ref,
            "is_latest_for_scope": self.is_latest_for_scope,
            "snapshot": self.snapshot.to_dict(),
            "visibility": self.visibility.to_dict(),
        }

    @classmethod
    def from_payload(cls, payload: object) -> "CrossTeamLeaderReviewDigest | None":
        if isinstance(payload, cls):
            return payload
        if not isinstance(payload, Mapping):
            return None
        digest_id = _optional_string(payload.get("digest_id"))
        item_id = _optional_string(payload.get("item_id"))
        objective_id = _optional_string(payload.get("objective_id"))
        if digest_id is None or item_id is None or objective_id is None:
            return None
        item_kind_raw = payload.get("item_kind")
        try:
            item_kind = (
                item_kind_raw
                if isinstance(item_kind_raw, ReviewItemKind)
                else ReviewItemKind(str(item_kind_raw))
            )
        except ValueError:
            return None
        return cls(
            digest_id=digest_id,
            item_id=item_id,
            item_kind=item_kind,
            objective_id=objective_id,
            item_title=str(payload.get("item_title", "")),
            reviewer_team_id=str(payload.get("reviewer_team_id", "")),
            reviewer_leader_id=str(payload.get("reviewer_leader_id", "")),
            target_team_id=str(payload.get("target_team_id", "")),
            reviewed_at=str(payload.get("reviewed_at", "")),
            stance=str(payload.get("stance", "")),
            agreement_level=str(payload.get("agreement_level", "")),
            summary=str(payload.get("summary", "")),
            suggested_adjustment=str(payload.get("suggested_adjustment", "")),
            confidence=_coerce_confidence(payload.get("confidence")),
            target_position_review_ref=_optional_string(
                payload.get("target_position_review_ref")
            ),
            review_ref=_optional_string(payload.get("review_ref")),
            is_latest_for_scope=_coerce_bool(payload.get("is_latest_for_scope", False)),
            snapshot=HierarchicalReviewDigestSnapshot.from_payload(payload.get("snapshot", {})),
            visibility=HierarchicalReviewDigestVisibility.from_payload(
                payload.get("visibility", {})
            ),
        )


@dataclass(slots=True)
class SuperLeaderSynthesisDigest:
    digest_id: str
    item_id: str
    item_kind: ReviewItemKind
    objective_id: str
    item_title: str = ""
    superleader_id: str = ""
    synthesized_at: str = ""
    summary: str = ""
    next_actions: tuple[str, ...] = ()
    confidence: float | None = None
    based_on_team_position_review_count: int = 0
    based_on_cross_team_review_count: int = 0
    review_ref: str | None = None
    is_latest_for_scope: bool = False
    snapshot: HierarchicalReviewDigestSnapshot = field(
        default_factory=HierarchicalReviewDigestSnapshot
    )
    visibility: HierarchicalReviewDigestVisibility = field(
        default_factory=HierarchicalReviewDigestVisibility
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "digest_id": self.digest_id,
            "item_id": self.item_id,
            "item_kind": self.item_kind.value,
            "objective_id": self.objective_id,
            "item_title": self.item_title,
            "superleader_id": self.superleader_id,
            "synthesized_at": self.synthesized_at,
            "summary": self.summary,
            "next_actions": list(self.next_actions),
            "confidence": self.confidence,
            "based_on_team_position_review_count": self.based_on_team_position_review_count,
            "based_on_cross_team_review_count": self.based_on_cross_team_review_count,
            "review_ref": self.review_ref,
            "is_latest_for_scope": self.is_latest_for_scope,
            "snapshot": self.snapshot.to_dict(),
            "visibility": self.visibility.to_dict(),
        }

    @classmethod
    def from_payload(cls, payload: object) -> "SuperLeaderSynthesisDigest | None":
        if isinstance(payload, cls):
            return payload
        if not isinstance(payload, Mapping):
            return None
        digest_id = _optional_string(payload.get("digest_id"))
        item_id = _optional_string(payload.get("item_id"))
        objective_id = _optional_string(payload.get("objective_id"))
        if digest_id is None or item_id is None or objective_id is None:
            return None
        item_kind_raw = payload.get("item_kind")
        try:
            item_kind = (
                item_kind_raw
                if isinstance(item_kind_raw, ReviewItemKind)
                else ReviewItemKind(str(item_kind_raw))
            )
        except ValueError:
            return None
        return cls(
            digest_id=digest_id,
            item_id=item_id,
            item_kind=item_kind,
            objective_id=objective_id,
            item_title=str(payload.get("item_title", "")),
            superleader_id=str(payload.get("superleader_id", "")),
            synthesized_at=str(payload.get("synthesized_at", "")),
            summary=str(payload.get("summary", "")),
            next_actions=_string_tuple(payload.get("next_actions", ())),
            confidence=_coerce_confidence(payload.get("confidence")),
            based_on_team_position_review_count=_coerce_int(
                payload.get("based_on_team_position_review_count"),
                default=0,
            ),
            based_on_cross_team_review_count=_coerce_int(
                payload.get("based_on_cross_team_review_count"),
                default=0,
            ),
            review_ref=_optional_string(payload.get("review_ref")),
            is_latest_for_scope=_coerce_bool(payload.get("is_latest_for_scope", False)),
            snapshot=HierarchicalReviewDigestSnapshot.from_payload(payload.get("snapshot", {})),
            visibility=HierarchicalReviewDigestVisibility.from_payload(
                payload.get("visibility", {})
            ),
        )


@dataclass(slots=True)
class HierarchicalReviewDigestView:
    item: ReviewItemRef
    snapshot: HierarchicalReviewDigestSnapshot
    visibility: HierarchicalReviewDigestVisibility = field(
        default_factory=HierarchicalReviewDigestVisibility
    )
    team_position_digests: tuple[TeamPositionReviewDigest, ...] = ()
    cross_team_leader_digests: tuple[CrossTeamLeaderReviewDigest, ...] = ()
    superleader_synthesis_digest: SuperLeaderSynthesisDigest | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "item": self.item.to_dict(),
            "snapshot": self.snapshot.to_dict(),
            "visibility": self.visibility.to_dict(),
            "team_position_digests": [
                digest.to_dict() for digest in self.team_position_digests
            ],
            "cross_team_leader_digests": [
                digest.to_dict() for digest in self.cross_team_leader_digests
            ],
            "superleader_synthesis_digest": (
                self.superleader_synthesis_digest.to_dict()
                if self.superleader_synthesis_digest is not None
                else None
            ),
        }

    @classmethod
    def from_payload(cls, payload: object) -> "HierarchicalReviewDigestView | None":
        if isinstance(payload, cls):
            return payload
        if not isinstance(payload, Mapping):
            return None
        item = ReviewItemRef.from_payload(payload.get("item"))
        if item is None:
            return None
        team_position_payloads = payload.get("team_position_digests", ())
        if not isinstance(team_position_payloads, (list, tuple)):
            team_position_payloads = ()
        cross_team_payloads = payload.get("cross_team_leader_digests", ())
        if not isinstance(cross_team_payloads, (list, tuple)):
            cross_team_payloads = ()
        return cls(
            item=item,
            snapshot=HierarchicalReviewDigestSnapshot.from_payload(
                payload.get("snapshot", {})
            ),
            visibility=HierarchicalReviewDigestVisibility.from_payload(
                payload.get("visibility", {})
            ),
            team_position_digests=tuple(
                digest
                for raw_digest in team_position_payloads
                for digest in [TeamPositionReviewDigest.from_payload(raw_digest)]
                if digest is not None
            ),
            cross_team_leader_digests=tuple(
                digest
                for raw_digest in cross_team_payloads
                for digest in [CrossTeamLeaderReviewDigest.from_payload(raw_digest)]
                if digest is not None
            ),
            superleader_synthesis_digest=SuperLeaderSynthesisDigest.from_payload(
                payload.get("superleader_synthesis_digest")
            ),
        )


def _latest_timestamp(values: tuple[str | None, ...]) -> str | None:
    candidates = [value for value in values if isinstance(value, str) and value.strip()]
    if not candidates:
        return None
    return max(candidates)


def _phase_rank(phase: HierarchicalReviewPhase) -> int:
    order = {
        HierarchicalReviewPhase.TEAM_INDEPENDENT_REVIEW: 0,
        HierarchicalReviewPhase.TEAM_SYNTHESIS: 1,
        HierarchicalReviewPhase.CROSS_TEAM_LEADER_REVIEW: 2,
        HierarchicalReviewPhase.SUPERLEADER_SYNTHESIS: 3,
    }
    return order.get(phase, 0)


def _infer_phase_from_artifacts(
    *,
    item_phase: HierarchicalReviewPhase,
    team_position_review_count: int,
    cross_team_leader_review_count: int,
    has_superleader_synthesis: bool,
) -> HierarchicalReviewPhase:
    artifact_phase = HierarchicalReviewPhase.TEAM_INDEPENDENT_REVIEW
    if has_superleader_synthesis:
        artifact_phase = HierarchicalReviewPhase.SUPERLEADER_SYNTHESIS
    elif cross_team_leader_review_count > 0:
        artifact_phase = HierarchicalReviewPhase.CROSS_TEAM_LEADER_REVIEW
    elif team_position_review_count > 0:
        artifact_phase = HierarchicalReviewPhase.TEAM_SYNTHESIS
    return (
        artifact_phase
        if _phase_rank(artifact_phase) >= _phase_rank(item_phase)
        else item_phase
    )


def _first_non_empty(*values: str) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _is_latest_team_position_review(
    review: TeamPositionReview,
    team_position_reviews: tuple[TeamPositionReview, ...] | list[TeamPositionReview],
) -> bool:
    relevant = [
        candidate
        for candidate in team_position_reviews
        if candidate.item_id == review.item_id and candidate.team_id == review.team_id
    ]
    if not relevant:
        return True
    latest = max(relevant, key=lambda candidate: (candidate.reviewed_at, candidate.position_review_id))
    return (
        latest.position_review_id == review.position_review_id
        and latest.reviewed_at == review.reviewed_at
    )


def _is_latest_cross_team_leader_review(
    review: CrossTeamLeaderReview,
    cross_team_leader_reviews: tuple[CrossTeamLeaderReview, ...] | list[CrossTeamLeaderReview],
) -> bool:
    relevant = [
        candidate
        for candidate in cross_team_leader_reviews
        if candidate.item_id == review.item_id
        and candidate.reviewer_team_id == review.reviewer_team_id
        and candidate.target_team_id == review.target_team_id
    ]
    if not relevant:
        return True
    latest = max(relevant, key=lambda candidate: (candidate.reviewed_at, candidate.cross_review_id))
    return latest.cross_review_id == review.cross_review_id and latest.reviewed_at == review.reviewed_at


def build_hierarchical_review_digest_snapshot(
    item: ReviewItemRef,
    *,
    team_position_reviews: tuple[TeamPositionReview, ...] | list[TeamPositionReview] = (),
    cross_team_leader_reviews: tuple[CrossTeamLeaderReview, ...] | list[CrossTeamLeaderReview] = (),
    superleader_synthesis: SuperLeaderSynthesis | None = None,
) -> HierarchicalReviewDigestSnapshot:
    team_reviews = tuple(review for review in team_position_reviews if review.item_id == item.item_id)
    cross_reviews = tuple(
        review for review in cross_team_leader_reviews if review.item_id == item.item_id
    )
    last_team_position_reviewed_at = _latest_timestamp(
        tuple(review.reviewed_at for review in team_reviews)
    )
    last_cross_team_reviewed_at = _latest_timestamp(
        tuple(review.reviewed_at for review in cross_reviews)
    )
    last_superleader_synthesized_at = (
        superleader_synthesis.synthesized_at
        if superleader_synthesis is not None and superleader_synthesis.item_id == item.item_id
        else None
    )
    latest_activity_at = _latest_timestamp(
        (
            item.phase_entered_at,
            item.freshness.last_reviewed_at,
            last_team_position_reviewed_at,
            last_cross_team_reviewed_at,
            last_superleader_synthesized_at,
        )
    )
    freshness = item.freshness
    if freshness.last_reviewed_at is None and latest_activity_at is not None:
        freshness = replace(freshness, last_reviewed_at=latest_activity_at)
    return HierarchicalReviewDigestSnapshot(
        current_phase=_infer_phase_from_artifacts(
            item_phase=item.phase,
            team_position_review_count=len(team_reviews),
            cross_team_leader_review_count=len(cross_reviews),
            has_superleader_synthesis=last_superleader_synthesized_at is not None,
        ),
        freshness=freshness,
        team_position_review_count=len(team_reviews),
        cross_team_leader_review_count=len(cross_reviews),
        has_superleader_synthesis=last_superleader_synthesized_at is not None,
        last_team_position_reviewed_at=last_team_position_reviewed_at,
        last_cross_team_reviewed_at=last_cross_team_reviewed_at,
        last_superleader_synthesized_at=last_superleader_synthesized_at,
        latest_activity_at=latest_activity_at,
    )


def build_team_position_review_digest(
    *,
    item: ReviewItemRef,
    review: TeamPositionReview,
    snapshot: HierarchicalReviewDigestSnapshot,
    visibility: HierarchicalReviewDigestVisibility,
    team_position_reviews: tuple[TeamPositionReview, ...] | list[TeamPositionReview] = (),
) -> TeamPositionReviewDigest:
    return TeamPositionReviewDigest(
        digest_id=f"team-position-digest:{review.position_review_id}",
        item_id=item.item_id,
        item_kind=item.item_kind,
        objective_id=item.objective_id,
        item_title=item.title,
        team_id=review.team_id,
        leader_id=review.leader_id,
        reviewed_at=review.reviewed_at,
        team_stance=review.team_stance,
        summary=review.summary or review.team_stance,
        recommended_next_action=review.recommended_next_action,
        confidence=review.confidence,
        based_on_task_review_revision_count=len(review.based_on_task_review_revision_ids),
        review_ref=review.position_review_id if visibility.ref_visible else None,
        is_latest_for_scope=_is_latest_team_position_review(review, team_position_reviews),
        snapshot=snapshot,
        visibility=visibility,
    )


def build_cross_team_leader_review_digest(
    *,
    item: ReviewItemRef,
    review: CrossTeamLeaderReview,
    snapshot: HierarchicalReviewDigestSnapshot,
    visibility: HierarchicalReviewDigestVisibility,
    cross_team_leader_reviews: tuple[CrossTeamLeaderReview, ...] | list[CrossTeamLeaderReview] = (),
) -> CrossTeamLeaderReviewDigest:
    return CrossTeamLeaderReviewDigest(
        digest_id=f"cross-team-review-digest:{review.cross_review_id}",
        item_id=item.item_id,
        item_kind=item.item_kind,
        objective_id=item.objective_id,
        item_title=item.title,
        reviewer_team_id=review.reviewer_team_id,
        reviewer_leader_id=review.reviewer_leader_id,
        target_team_id=review.target_team_id,
        reviewed_at=review.reviewed_at,
        stance=review.stance,
        agreement_level=review.agreement_level,
        summary=_first_non_empty(
            review.what_changed_in_my_understanding,
            review.suggested_adjustment,
            review.stance,
        ),
        suggested_adjustment=review.suggested_adjustment,
        confidence=review.confidence,
        target_position_review_ref=(
            review.target_position_review_id if visibility.ref_visible else None
        ),
        review_ref=review.cross_review_id if visibility.ref_visible else None,
        is_latest_for_scope=_is_latest_cross_team_leader_review(
            review,
            cross_team_leader_reviews,
        ),
        snapshot=snapshot,
        visibility=visibility,
    )


def build_superleader_synthesis_digest(
    *,
    item: ReviewItemRef,
    synthesis: SuperLeaderSynthesis,
    snapshot: HierarchicalReviewDigestSnapshot,
    visibility: HierarchicalReviewDigestVisibility,
) -> SuperLeaderSynthesisDigest:
    return SuperLeaderSynthesisDigest(
        digest_id=f"superleader-synthesis-digest:{synthesis.synthesis_id}",
        item_id=item.item_id,
        item_kind=item.item_kind,
        objective_id=item.objective_id,
        item_title=item.title,
        superleader_id=synthesis.superleader_id,
        synthesized_at=synthesis.synthesized_at,
        summary=synthesis.final_position,
        next_actions=synthesis.next_actions,
        confidence=synthesis.confidence,
        based_on_team_position_review_count=len(synthesis.based_on_team_position_review_ids),
        based_on_cross_team_review_count=len(synthesis.based_on_cross_team_review_ids),
        review_ref=synthesis.synthesis_id if visibility.ref_visible else None,
        is_latest_for_scope=(
            snapshot.last_superleader_synthesized_at == synthesis.synthesized_at
            if snapshot.last_superleader_synthesized_at is not None
            else True
        ),
        snapshot=snapshot,
        visibility=visibility,
    )
