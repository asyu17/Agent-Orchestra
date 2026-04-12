from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from agent_orchestra.contracts.ids import make_id
from agent_orchestra.contracts.task import TaskCard


class TaskReviewStance(str, Enum):
    GOOD_FIT = "good_fit"
    NOT_FIT = "not_fit"
    BLOCKED_BY_DEPENDENCY = "blocked_by_dependency"
    NEEDS_SPLIT = "needs_split"
    NEEDS_AUTHORITY = "needs_authority"
    DUPLICATE = "duplicate"
    HIGH_RISK = "high_risk"
    UNCERTAIN = "uncertain"


def _string_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(item) for item in value if item is not None and str(item))


def make_task_review_slot_id(*, task_id: str, reviewer_agent_id: str) -> str:
    return f"{task_id}:review-slot:{reviewer_agent_id}"


def make_task_review_revision_id() -> str:
    return make_id("taskreview")


@dataclass(slots=True)
class TaskReviewExperienceContext:
    touched_paths: tuple[str, ...] = ()
    observed_paths: tuple[str, ...] = ()
    related_task_ids: tuple[str, ...] = ()
    related_lane_ids: tuple[str, ...] = ()
    related_blackboard_entry_ids: tuple[str, ...] = ()
    produced_artifact_refs: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "touched_paths": list(self.touched_paths),
            "observed_paths": list(self.observed_paths),
            "related_task_ids": list(self.related_task_ids),
            "related_lane_ids": list(self.related_lane_ids),
            "related_blackboard_entry_ids": list(self.related_blackboard_entry_ids),
            "produced_artifact_refs": list(self.produced_artifact_refs),
        }

    @classmethod
    def from_payload(cls, payload: object) -> "TaskReviewExperienceContext":
        if isinstance(payload, cls):
            return payload
        if not isinstance(payload, Mapping):
            return cls()
        return cls(
            touched_paths=_string_tuple(payload.get("touched_paths", ())),
            observed_paths=_string_tuple(payload.get("observed_paths", ())),
            related_task_ids=_string_tuple(payload.get("related_task_ids", ())),
            related_lane_ids=_string_tuple(payload.get("related_lane_ids", ())),
            related_blackboard_entry_ids=_string_tuple(
                payload.get("related_blackboard_entry_ids", ())
            ),
            produced_artifact_refs=_string_tuple(payload.get("produced_artifact_refs", ())),
        )


@dataclass(slots=True)
class TaskReviewRevision:
    revision_id: str
    slot_id: str
    task_id: str
    reviewer_agent_id: str
    reviewer_role: str = ""
    created_at: str = ""
    replaces_revision_id: str | None = None
    based_on_task_version: int = 0
    based_on_knowledge_epoch: int = 0
    stance: TaskReviewStance = TaskReviewStance.UNCERTAIN
    summary: str = ""
    relation_to_my_work: str = ""
    confidence: float | None = None
    experience_context: TaskReviewExperienceContext = field(
        default_factory=TaskReviewExperienceContext
    )
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "revision_id": self.revision_id,
            "slot_id": self.slot_id,
            "task_id": self.task_id,
            "reviewer_agent_id": self.reviewer_agent_id,
            "reviewer_role": self.reviewer_role,
            "created_at": self.created_at,
            "replaces_revision_id": self.replaces_revision_id,
            "based_on_task_version": self.based_on_task_version,
            "based_on_knowledge_epoch": self.based_on_knowledge_epoch,
            "stance": self.stance.value,
            "summary": self.summary,
            "relation_to_my_work": self.relation_to_my_work,
            "confidence": self.confidence,
            "experience_context": self.experience_context.to_dict(),
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_payload(cls, payload: object) -> "TaskReviewRevision | None":
        if isinstance(payload, cls):
            return payload
        if not isinstance(payload, Mapping):
            return None
        revision_id = payload.get("revision_id")
        slot_id = payload.get("slot_id")
        task_id = payload.get("task_id")
        reviewer_agent_id = payload.get("reviewer_agent_id")
        if not all(
            isinstance(value, str) and value.strip()
            for value in (revision_id, slot_id, task_id, reviewer_agent_id)
        ):
            return None
        stance_raw = payload.get("stance", TaskReviewStance.UNCERTAIN.value)
        try:
            stance = (
                stance_raw
                if isinstance(stance_raw, TaskReviewStance)
                else TaskReviewStance(str(stance_raw))
            )
        except ValueError:
            stance = TaskReviewStance.UNCERTAIN
        confidence_raw = payload.get("confidence")
        try:
            confidence = float(confidence_raw) if confidence_raw is not None else None
        except (TypeError, ValueError):
            confidence = None
        return cls(
            revision_id=revision_id.strip(),
            slot_id=slot_id.strip(),
            task_id=task_id.strip(),
            reviewer_agent_id=reviewer_agent_id.strip(),
            reviewer_role=str(payload.get("reviewer_role", "")),
            created_at=str(payload.get("created_at", "")),
            replaces_revision_id=(
                str(payload["replaces_revision_id"])
                if payload.get("replaces_revision_id") is not None
                else None
            ),
            based_on_task_version=int(payload.get("based_on_task_version", 0) or 0),
            based_on_knowledge_epoch=int(payload.get("based_on_knowledge_epoch", 0) or 0),
            stance=stance,
            summary=str(payload.get("summary", "")),
            relation_to_my_work=str(payload.get("relation_to_my_work", "")),
            confidence=confidence,
            experience_context=TaskReviewExperienceContext.from_payload(
                payload.get("experience_context", {})
            ),
            metadata={
                str(key): value
                for key, value in payload.get("metadata", {}).items()
            }
            if isinstance(payload.get("metadata"), Mapping)
            else {},
        )


@dataclass(slots=True)
class TaskReviewSlot:
    slot_id: str
    task_id: str
    reviewer_agent_id: str
    reviewer_role: str = ""
    latest_revision_id: str = ""
    reviewed_at: str = ""
    based_on_task_version: int = 0
    based_on_knowledge_epoch: int = 0
    stance: TaskReviewStance = TaskReviewStance.UNCERTAIN
    summary: str = ""
    relation_to_my_work: str = ""
    confidence: float | None = None
    experience_context: TaskReviewExperienceContext = field(
        default_factory=TaskReviewExperienceContext
    )
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_revision(cls, revision: TaskReviewRevision) -> "TaskReviewSlot":
        return cls(
            slot_id=revision.slot_id,
            task_id=revision.task_id,
            reviewer_agent_id=revision.reviewer_agent_id,
            reviewer_role=revision.reviewer_role,
            latest_revision_id=revision.revision_id,
            reviewed_at=revision.created_at,
            based_on_task_version=revision.based_on_task_version,
            based_on_knowledge_epoch=revision.based_on_knowledge_epoch,
            stance=revision.stance,
            summary=revision.summary,
            relation_to_my_work=revision.relation_to_my_work,
            confidence=revision.confidence,
            experience_context=revision.experience_context,
            metadata=dict(revision.metadata),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "slot_id": self.slot_id,
            "task_id": self.task_id,
            "reviewer_agent_id": self.reviewer_agent_id,
            "reviewer_role": self.reviewer_role,
            "latest_revision_id": self.latest_revision_id,
            "reviewed_at": self.reviewed_at,
            "based_on_task_version": self.based_on_task_version,
            "based_on_knowledge_epoch": self.based_on_knowledge_epoch,
            "stance": self.stance.value,
            "summary": self.summary,
            "relation_to_my_work": self.relation_to_my_work,
            "confidence": self.confidence,
            "experience_context": self.experience_context.to_dict(),
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_payload(cls, payload: object) -> "TaskReviewSlot | None":
        if isinstance(payload, cls):
            return payload
        if not isinstance(payload, Mapping):
            return None
        slot_id = payload.get("slot_id")
        task_id = payload.get("task_id")
        reviewer_agent_id = payload.get("reviewer_agent_id")
        latest_revision_id = payload.get("latest_revision_id")
        if not all(
            isinstance(value, str) and value.strip()
            for value in (slot_id, task_id, reviewer_agent_id, latest_revision_id)
        ):
            return None
        stance_raw = payload.get("stance", TaskReviewStance.UNCERTAIN.value)
        try:
            stance = (
                stance_raw
                if isinstance(stance_raw, TaskReviewStance)
                else TaskReviewStance(str(stance_raw))
            )
        except ValueError:
            stance = TaskReviewStance.UNCERTAIN
        confidence_raw = payload.get("confidence")
        try:
            confidence = float(confidence_raw) if confidence_raw is not None else None
        except (TypeError, ValueError):
            confidence = None
        return cls(
            slot_id=slot_id.strip(),
            task_id=task_id.strip(),
            reviewer_agent_id=reviewer_agent_id.strip(),
            reviewer_role=str(payload.get("reviewer_role", "")),
            latest_revision_id=latest_revision_id.strip(),
            reviewed_at=str(payload.get("reviewed_at", "")),
            based_on_task_version=int(payload.get("based_on_task_version", 0) or 0),
            based_on_knowledge_epoch=int(payload.get("based_on_knowledge_epoch", 0) or 0),
            stance=stance,
            summary=str(payload.get("summary", "")),
            relation_to_my_work=str(payload.get("relation_to_my_work", "")),
            confidence=confidence,
            experience_context=TaskReviewExperienceContext.from_payload(
                payload.get("experience_context", {})
            ),
            metadata={
                str(key): value
                for key, value in payload.get("metadata", {}).items()
            }
            if isinstance(payload.get("metadata"), Mapping)
            else {},
        )


@dataclass(slots=True)
class TaskReviewDigest:
    task_id: str
    slot_count: int = 0
    last_reviewed_at: str | None = None
    stance_counts: dict[str, int] = field(default_factory=dict)
    good_fit_agent_ids: tuple[str, ...] = ()
    blocked_agent_ids: tuple[str, ...] = ()
    needs_authority_agent_ids: tuple[str, ...] = ()
    needs_split_agent_ids: tuple[str, ...] = ()
    duplicate_agent_ids: tuple[str, ...] = ()
    high_risk_agent_ids: tuple[str, ...] = ()
    summary_lines: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "slot_count": self.slot_count,
            "last_reviewed_at": self.last_reviewed_at,
            "stance_counts": dict(self.stance_counts),
            "good_fit_agent_ids": list(self.good_fit_agent_ids),
            "blocked_agent_ids": list(self.blocked_agent_ids),
            "needs_authority_agent_ids": list(self.needs_authority_agent_ids),
            "needs_split_agent_ids": list(self.needs_split_agent_ids),
            "duplicate_agent_ids": list(self.duplicate_agent_ids),
            "high_risk_agent_ids": list(self.high_risk_agent_ids),
            "summary_lines": list(self.summary_lines),
        }

    @classmethod
    def from_payload(cls, payload: object) -> "TaskReviewDigest | None":
        if isinstance(payload, cls):
            return payload
        if not isinstance(payload, Mapping):
            return None
        task_id = payload.get("task_id")
        if not isinstance(task_id, str) or not task_id.strip():
            return None
        stance_counts_raw = payload.get("stance_counts", {})
        stance_counts = (
            {
                str(key): int(value)
                for key, value in stance_counts_raw.items()
                if str(key)
            }
            if isinstance(stance_counts_raw, Mapping)
            else {}
        )
        return cls(
            task_id=task_id.strip(),
            slot_count=int(payload.get("slot_count", 0) or 0),
            last_reviewed_at=(
                str(payload["last_reviewed_at"])
                if payload.get("last_reviewed_at") is not None
                else None
            ),
            stance_counts=stance_counts,
            good_fit_agent_ids=_string_tuple(payload.get("good_fit_agent_ids", ())),
            blocked_agent_ids=_string_tuple(payload.get("blocked_agent_ids", ())),
            needs_authority_agent_ids=_string_tuple(
                payload.get("needs_authority_agent_ids", ())
            ),
            needs_split_agent_ids=_string_tuple(payload.get("needs_split_agent_ids", ())),
            duplicate_agent_ids=_string_tuple(payload.get("duplicate_agent_ids", ())),
            high_risk_agent_ids=_string_tuple(payload.get("high_risk_agent_ids", ())),
            summary_lines=_string_tuple(payload.get("summary_lines", ())),
        )


@dataclass(slots=True)
class TaskClaimContext:
    task: TaskCard
    review_slots: tuple[TaskReviewSlot, ...] = ()
    review_digest: TaskReviewDigest | None = None


def reduce_task_review_slots(
    task_id: str,
    revisions: tuple[TaskReviewRevision, ...] | list[TaskReviewRevision],
) -> tuple[TaskReviewSlot, ...]:
    latest_by_agent: dict[str, TaskReviewRevision] = {}
    ordered = sorted(
        (
            revision
            for revision in revisions
            if revision.task_id == task_id
        ),
        key=lambda item: (item.created_at, item.revision_id),
    )
    for revision in ordered:
        latest_by_agent[revision.reviewer_agent_id] = revision
    slots = [
        TaskReviewSlot.from_revision(latest_by_agent[reviewer_agent_id])
        for reviewer_agent_id in sorted(latest_by_agent)
    ]
    return tuple(slots)


def build_task_review_digest(
    task_id: str,
    review_slots: tuple[TaskReviewSlot, ...] | list[TaskReviewSlot],
) -> TaskReviewDigest:
    slots = tuple(slot for slot in review_slots if slot.task_id == task_id)
    stance_counts: dict[str, int] = {}
    last_reviewed_at: str | None = None
    good_fit: list[str] = []
    blocked: list[str] = []
    needs_authority: list[str] = []
    needs_split: list[str] = []
    duplicate: list[str] = []
    high_risk: list[str] = []
    summary_lines: list[str] = []

    for slot in slots:
        stance_key = slot.stance.value
        stance_counts[stance_key] = stance_counts.get(stance_key, 0) + 1
        if last_reviewed_at is None or slot.reviewed_at > last_reviewed_at:
            last_reviewed_at = slot.reviewed_at
        if slot.stance == TaskReviewStance.GOOD_FIT:
            good_fit.append(slot.reviewer_agent_id)
        elif slot.stance == TaskReviewStance.BLOCKED_BY_DEPENDENCY:
            blocked.append(slot.reviewer_agent_id)
        elif slot.stance == TaskReviewStance.NEEDS_AUTHORITY:
            needs_authority.append(slot.reviewer_agent_id)
        elif slot.stance == TaskReviewStance.NEEDS_SPLIT:
            needs_split.append(slot.reviewer_agent_id)
        elif slot.stance == TaskReviewStance.DUPLICATE:
            duplicate.append(slot.reviewer_agent_id)
        elif slot.stance == TaskReviewStance.HIGH_RISK:
            high_risk.append(slot.reviewer_agent_id)
        summary_lines.append(
            f"{slot.reviewer_agent_id} [{slot.stance.value}] {slot.summary}".strip()
        )

    return TaskReviewDigest(
        task_id=task_id,
        slot_count=len(slots),
        last_reviewed_at=last_reviewed_at,
        stance_counts=stance_counts,
        good_fit_agent_ids=tuple(good_fit),
        blocked_agent_ids=tuple(blocked),
        needs_authority_agent_ids=tuple(needs_authority),
        needs_split_agent_ids=tuple(needs_split),
        duplicate_agent_ids=tuple(duplicate),
        high_risk_agent_ids=tuple(high_risk),
        summary_lines=tuple(summary_lines),
    )
