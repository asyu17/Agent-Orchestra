from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from agent_orchestra.contracts.enums import TaskScope, TaskStatus

_DEFAULT_PROTECTED_TASK_FIELDS = (
    "goal",
    "owned_paths",
    "allowed_inputs",
    "output_artifacts",
    "verification_commands",
    "handoff_to",
    "merge_target",
    "scope",
    "lane",
    "team_id",
    "group_id",
)


def _string_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    normalized: list[str] = []
    for item in value:
        if not isinstance(item, str):
            continue
        stripped = item.strip()
        if stripped and stripped not in normalized:
            normalized.append(stripped)
    return tuple(normalized)


def _normalized_actor_id(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized if normalized else None


def default_task_protected_read_only_fields() -> tuple[str, ...]:
    return _DEFAULT_PROTECTED_TASK_FIELDS


class TaskProvenanceKind(str, Enum):
    ROOT = "root"
    CHILD = "child"
    REPLACEMENT = "replacement"


class TaskSurfaceMutationKind(str, Enum):
    NONE = "none"
    SUPERSEDED = "superseded"
    CANCELLED = "cancelled"
    MERGED_INTO = "merged_into"
    NOT_NEEDED = "not_needed"


class TaskSurfaceAuthorityIntentKind(str, Enum):
    MUTATION = "mutation"
    PROTECTED_FIELD_WRITE = "protected_field_write"


class TaskSurfaceAuthorityVerdict(str, Enum):
    LOCAL_ALLOW = "local_allow"
    ESCALATION_NEEDED = "escalation_needed"
    FORBIDDEN = "forbidden"


@dataclass(slots=True, frozen=True)
class TaskSurfaceAuthorityDecision:
    verdict: TaskSurfaceAuthorityVerdict
    reason: str = ""
    protected_field_names: tuple[str, ...] = field(default_factory=tuple)


@dataclass(slots=True, frozen=True)
class TaskSurfaceAuthorityView:
    task_id: str
    scope: TaskScope
    lane: str
    group_id: str | None
    team_id: str | None
    provenance_kind: TaskProvenanceKind
    parent_task_id: str | None
    local_status_actor_ids: tuple[str, ...]
    local_structure_actor_ids: tuple[str, ...]
    protected_read_only_fields: tuple[str, ...]

    def has_local_status_authority(self, actor_id: str | None) -> bool:
        normalized = _normalized_actor_id(actor_id)
        return normalized is not None and normalized in self.local_status_actor_ids

    def has_local_structure_authority(self, actor_id: str | None) -> bool:
        normalized = _normalized_actor_id(actor_id)
        return normalized is not None and normalized in self.local_structure_actor_ids

    def protected_field_names(self, field_names: tuple[str, ...]) -> tuple[str, ...]:
        requested = _string_tuple(field_names)
        protected: list[str] = []
        for field_name in requested:
            if field_name in self.protected_read_only_fields and field_name not in protected:
                protected.append(field_name)
        return tuple(protected)


def _task_surface_json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {
            str(key): _task_surface_json_safe(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_task_surface_json_safe(item) for item in value]
    return str(value)


@dataclass(slots=True, frozen=True)
class TaskSurfaceAuthorityIntent:
    kind: TaskSurfaceAuthorityIntentKind
    actor_id: str
    reason: str = ""
    resume_status: TaskStatus = TaskStatus.PENDING
    mutation_kind: TaskSurfaceMutationKind = TaskSurfaceMutationKind.NONE
    target_task_id: str | None = None
    protected_field_names: tuple[str, ...] = field(default_factory=tuple)
    field_updates: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "actor_id": self.actor_id,
            "reason": self.reason,
            "resume_status": self.resume_status.value,
            "mutation_kind": self.mutation_kind.value,
            "target_task_id": self.target_task_id,
            "protected_field_names": list(self.protected_field_names),
            "field_updates": {
                str(key): _task_surface_json_safe(value)
                for key, value in self.field_updates.items()
            },
        }

    @classmethod
    def from_payload(cls, payload: object) -> "TaskSurfaceAuthorityIntent | None":
        if isinstance(payload, cls):
            return payload
        if not isinstance(payload, Mapping):
            return None
        raw_kind = payload.get("kind")
        raw_actor_id = payload.get("actor_id")
        if not isinstance(raw_kind, str) or not raw_kind.strip():
            return None
        if not isinstance(raw_actor_id, str) or not raw_actor_id.strip():
            return None
        try:
            kind = TaskSurfaceAuthorityIntentKind(raw_kind.strip())
        except ValueError:
            return None
        raw_resume_status = payload.get("resume_status", TaskStatus.PENDING.value)
        try:
            resume_status = (
                raw_resume_status
                if isinstance(raw_resume_status, TaskStatus)
                else TaskStatus(str(raw_resume_status).strip())
            )
        except ValueError:
            resume_status = TaskStatus.PENDING
        raw_mutation_kind = payload.get("mutation_kind", TaskSurfaceMutationKind.NONE.value)
        try:
            mutation_kind = (
                raw_mutation_kind
                if isinstance(raw_mutation_kind, TaskSurfaceMutationKind)
                else TaskSurfaceMutationKind(str(raw_mutation_kind).strip())
            )
        except ValueError:
            mutation_kind = TaskSurfaceMutationKind.NONE
        raw_target_task_id = payload.get("target_task_id")
        target_task_id = (
            str(raw_target_task_id).strip()
            if raw_target_task_id is not None and str(raw_target_task_id).strip()
            else None
        )
        raw_field_updates = payload.get("field_updates", {})
        field_updates = (
            {str(key): value for key, value in raw_field_updates.items()}
            if isinstance(raw_field_updates, Mapping)
            else {}
        )
        return cls(
            kind=kind,
            actor_id=raw_actor_id.strip(),
            reason=str(payload.get("reason", "")),
            resume_status=resume_status,
            mutation_kind=mutation_kind,
            target_task_id=target_task_id,
            protected_field_names=_string_tuple(payload.get("protected_field_names", ())),
            field_updates=field_updates,
        )


@dataclass(slots=True, frozen=True)
class TaskProvenance:
    kind: TaskProvenanceKind = TaskProvenanceKind.ROOT
    source_task_id: str | None = None
    reason: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind.value,
            "source_task_id": self.source_task_id,
            "reason": self.reason,
        }

    @classmethod
    def from_payload(cls, payload: object) -> "TaskProvenance" | None:
        if isinstance(payload, cls):
            return payload
        if not isinstance(payload, Mapping):
            return None
        raw_kind = payload.get("kind")
        try:
            kind = TaskProvenanceKind(str(raw_kind).strip()) if raw_kind is not None else TaskProvenanceKind.ROOT
        except ValueError:
            kind = TaskProvenanceKind.ROOT
        source_task_id = payload.get("source_task_id")
        normalized_source = (
            str(source_task_id).strip()
            if source_task_id is not None and str(source_task_id).strip()
            else None
        )
        return cls(
            kind=kind,
            source_task_id=normalized_source,
            reason=str(payload.get("reason", "")),
        )


@dataclass(slots=True, frozen=True)
class TaskSurfaceMutation:
    kind: TaskSurfaceMutationKind = TaskSurfaceMutationKind.NONE
    actor_id: str | None = None
    reason: str = ""
    target_task_id: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind.value,
            "actor_id": self.actor_id,
            "reason": self.reason,
            "target_task_id": self.target_task_id,
        }

    @classmethod
    def from_payload(cls, payload: object) -> "TaskSurfaceMutation" | None:
        if isinstance(payload, cls):
            return payload
        if not isinstance(payload, Mapping):
            return None
        raw_kind = payload.get("kind")
        try:
            kind = TaskSurfaceMutationKind(str(raw_kind).strip()) if raw_kind is not None else TaskSurfaceMutationKind.NONE
        except ValueError:
            kind = TaskSurfaceMutationKind.NONE
        actor_id = payload.get("actor_id")
        normalized_actor = (
            str(actor_id).strip()
            if actor_id is not None and str(actor_id).strip()
            else None
        )
        target_task_id = payload.get("target_task_id")
        normalized_target = (
            str(target_task_id).strip()
            if target_task_id is not None and str(target_task_id).strip()
            else None
        )
        return cls(
            kind=kind,
            actor_id=normalized_actor,
            reason=str(payload.get("reason", "")),
            target_task_id=normalized_target,
        )


def _task_surface_provenance_kind(
    *,
    derived_from: str | None,
    authority_decision_payload: Mapping[str, object],
) -> TaskProvenanceKind:
    if derived_from is None:
        return TaskProvenanceKind.ROOT
    decision = authority_decision_payload.get("decision")
    if isinstance(decision, str) and decision.strip().lower() == "reroute":
        return TaskProvenanceKind.REPLACEMENT
    return TaskProvenanceKind.CHILD


@dataclass(slots=True)
class TaskCard:
    task_id: str
    goal: str
    lane: str
    group_id: str | None = None
    team_id: str | None = None
    scope: TaskScope = TaskScope.TEAM
    owned_paths: tuple[str, ...] = field(default_factory=tuple)
    allowed_inputs: tuple[str, ...] = field(default_factory=tuple)
    output_artifacts: tuple[str, ...] = field(default_factory=tuple)
    verification_commands: tuple[str, ...] = field(default_factory=tuple)
    handoff_to: tuple[str, ...] = field(default_factory=tuple)
    merge_target: str | None = None
    owner_id: str | None = None
    authority_request_id: str | None = None
    authority_request_payload: dict[str, object] = field(default_factory=dict)
    authority_decision_payload: dict[str, object] = field(default_factory=dict)
    authority_boundary_class: str | None = None
    authority_waiting_since: str | None = None
    authority_resume_target: str | None = None
    superseded_by_task_id: str | None = None
    merged_into_task_id: str | None = None
    claim_session_id: str | None = None
    claimed_at: str | None = None
    claim_source: str | None = None
    blocked_by: tuple[str, ...] = field(default_factory=tuple)
    created_by: str | None = None
    derived_from: str | None = None
    reason: str = ""
    provenance: TaskProvenance = field(default_factory=TaskProvenance)
    surface_mutation: TaskSurfaceMutation = field(default_factory=TaskSurfaceMutation)
    protected_read_only_fields: tuple[str, ...] = field(default_factory=default_task_protected_read_only_fields)
    slice_id: str | None = None
    slice_mode: str | None = None
    depends_on_slice_ids: tuple[str, ...] = field(default_factory=tuple)
    depends_on_task_ids: tuple[str, ...] = field(default_factory=tuple)
    parallel_group: str | None = None
    status: TaskStatus = TaskStatus.PENDING

    def __post_init__(self) -> None:
        self.protected_read_only_fields = _string_tuple(
            self.protected_read_only_fields or _DEFAULT_PROTECTED_TASK_FIELDS
        ) or _DEFAULT_PROTECTED_TASK_FIELDS
        provenance = TaskProvenance.from_payload(self.provenance) or TaskProvenance()
        derived_from = self.derived_from.strip() if isinstance(self.derived_from, str) and self.derived_from.strip() else None
        if provenance.source_task_id is None and derived_from is not None:
            provenance = TaskProvenance(
                kind=_task_surface_provenance_kind(
                    derived_from=derived_from,
                    authority_decision_payload=self.authority_decision_payload,
                ),
                source_task_id=derived_from,
                reason=provenance.reason or self.reason,
            )
        elif provenance.kind == TaskProvenanceKind.ROOT and derived_from is not None:
            provenance = TaskProvenance(
                kind=_task_surface_provenance_kind(
                    derived_from=derived_from,
                    authority_decision_payload=self.authority_decision_payload,
                ),
                source_task_id=provenance.source_task_id or derived_from,
                reason=provenance.reason or self.reason,
            )
        elif not provenance.reason and self.reason:
            provenance = TaskProvenance(
                kind=provenance.kind,
                source_task_id=provenance.source_task_id,
                reason=self.reason,
            )
        self.provenance = provenance

        mutation = TaskSurfaceMutation.from_payload(self.surface_mutation) or TaskSurfaceMutation()
        if mutation.kind == TaskSurfaceMutationKind.NONE:
            if isinstance(self.superseded_by_task_id, str) and self.superseded_by_task_id.strip():
                mutation = TaskSurfaceMutation(
                    kind=TaskSurfaceMutationKind.SUPERSEDED,
                    target_task_id=self.superseded_by_task_id.strip(),
                )
            elif isinstance(self.merged_into_task_id, str) and self.merged_into_task_id.strip():
                mutation = TaskSurfaceMutation(
                    kind=TaskSurfaceMutationKind.MERGED_INTO,
                    target_task_id=self.merged_into_task_id.strip(),
                )
        self.surface_mutation = mutation

    def surface_parent_task_id(self) -> str | None:
        if self.provenance.source_task_id is not None:
            return _normalized_actor_id(self.provenance.source_task_id)
        return _normalized_actor_id(self.derived_from)

    def surface_authority_view(self) -> TaskSurfaceAuthorityView:
        return TaskSurfaceAuthorityView(
            task_id=self.task_id,
            scope=self.scope,
            lane=self.lane,
            group_id=self.group_id,
            team_id=self.team_id,
            provenance_kind=self.provenance.kind,
            parent_task_id=self.surface_parent_task_id(),
            local_status_actor_ids=self.local_status_actor_ids(),
            local_structure_actor_ids=self.local_structure_actor_ids(),
            protected_read_only_fields=self.protected_read_only_fields,
        )

    def local_status_actor_ids(self) -> tuple[str, ...]:
        actor_ids: list[str] = []
        for raw_actor_id in (self.owner_id, self.created_by):
            normalized = _normalized_actor_id(raw_actor_id)
            if normalized is not None and normalized not in actor_ids:
                actor_ids.append(normalized)
        return tuple(actor_ids)

    def local_structure_actor_ids(self) -> tuple[str, ...]:
        actor_ids: list[str] = []
        normalized = _normalized_actor_id(self.created_by)
        if normalized is not None:
            actor_ids.append(normalized)
        return tuple(actor_ids)

    def has_local_status_authority(self, actor_id: str | None) -> bool:
        normalized = _normalized_actor_id(actor_id)
        return normalized is not None and normalized in self.local_status_actor_ids()

    def has_local_structure_authority(self, actor_id: str | None) -> bool:
        normalized = _normalized_actor_id(actor_id)
        return normalized is not None and normalized in self.local_structure_actor_ids()
