from __future__ import annotations

import asyncio
import hashlib
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING
from uuid import uuid4

from agent_orchestra.bus.base import EventBus
from agent_orchestra.contracts.agent import AgentSession, CoordinatorSessionState
from agent_orchestra.contracts.authority import (
    AuthorityDecision,
    AuthorityPolicy,
    AuthorityState,
    ScopeExtensionRequest,
)
from agent_orchestra.contracts.blackboard import BlackboardEntry, BlackboardEntryKind, BlackboardKind
from agent_orchestra.contracts.blackboard import BlackboardSnapshot
from agent_orchestra.contracts.delivery import DeliveryState, DeliveryStateKind, DeliveryStatus
from agent_orchestra.contracts.enums import SpecEdgeKind, SpecNodeKind, SpecNodeStatus, TaskScope, TaskStatus, WorkerStatus
from agent_orchestra.contracts.events import OrchestraEvent
from agent_orchestra.contracts.handoff import HandoffRecord
from agent_orchestra.contracts.hierarchical_review import (
    CrossTeamLeaderReview,
    CrossTeamLeaderReviewDigest,
    HierarchicalReviewActor,
    HierarchicalReviewActorRole,
    HierarchicalReviewDigestSnapshot,
    HierarchicalReviewDigestView,
    HierarchicalReviewDigestVisibility,
    HierarchicalReviewPolicy,
    HierarchicalReviewReadMode,
    ReviewItemKind,
    ReviewItemRef,
    SuperLeaderSynthesis,
    SuperLeaderSynthesisDigest,
    TeamPositionReview,
    TeamPositionReviewDigest,
    build_cross_team_leader_review_digest,
    build_hierarchical_review_digest_snapshot,
    build_superleader_synthesis_digest,
    build_team_position_review_digest,
    make_cross_team_leader_review_id,
    make_review_item_id,
    make_superleader_synthesis_id,
    make_team_position_review_id,
)
from agent_orchestra.contracts.ids import (
    make_blackboard_entry_id,
    make_edge_id,
    make_objective_id,
    make_spec_node_id,
    make_task_id,
)
from agent_orchestra.contracts.objective import ObjectiveSpec, SpecEdge, SpecNode
from agent_orchestra.contracts.session_continuity import (
    ResidentTeamShell,
    ShellAttachDecision,
)
from agent_orchestra.contracts.task import (
    TaskCard,
    TaskProvenance,
    TaskProvenanceKind,
    TaskSurfaceAuthorityDecision,
    TaskSurfaceAuthorityIntent,
    TaskSurfaceAuthorityIntentKind,
    TaskSurfaceAuthorityVerdict,
    TaskSurfaceAuthorityView,
    TaskSurfaceMutation,
    TaskSurfaceMutationKind,
)
from agent_orchestra.contracts.task_review import (
    TaskClaimContext,
    TaskReviewDigest,
    TaskReviewExperienceContext,
    TaskReviewRevision,
    TaskReviewSlot,
    TaskReviewStance,
    build_task_review_digest,
    make_task_review_revision_id,
    make_task_review_slot_id,
)
from agent_orchestra.contracts.team import Group, Team
from agent_orchestra.runtime.reducer import (
    LeaderLaneBlackboardReducer,
    Reducer,
    TeamBlackboardReducer,
)
from agent_orchestra.runtime.team_runtime import TeamRuntime
from agent_orchestra.storage.base import (
    AuthorityDecisionStoreCommit,
    AuthorityRequestStoreCommit,
    CoordinationTransactionStoreCommit,
    CoordinationOutboxRecord,
    OrchestrationStore,
)
from agent_orchestra.contracts.execution import (
    ExecutionGuardResult,
    ExecutionGuardStatus,
    LaunchBackend,
    Planner,
    WorkerSession,
    VerificationCommandResult,
    select_best_equivalent_verification_result,
    WorkerAssignment,
    WorkerBackendCapabilities,
    WorkerExecutionPolicy,
    WorkerHandle,
    WorkerRecord,
)
from agent_orchestra.contracts.execution import WorkerSupervisor
from agent_orchestra.contracts.worker_protocol import WorkerExecutionContract, WorkerFinalStatus, WorkerLeasePolicy
from agent_orchestra.planning.template import ObjectiveTemplate, PlanningResult
from agent_orchestra.runtime.directed_mailbox_protocol import DirectedTaskReceipt, DirectedTaskResult
from agent_orchestra.runtime.session_continuity import SessionContinuityState, SessionInspectSnapshot
from agent_orchestra.runtime.session_domain import SessionDomainService, SessionResumeResult
from agent_orchestra.tools.mailbox import (
    MailboxBridge,
    MailboxDigest,
    MailboxSubscription,
    MailboxVisibilityScope,
)

if TYPE_CHECKING:
    from agent_orchestra.contracts.worker_protocol import WorkerRoleProfile
    from agent_orchestra.contracts.planning_review import (
        LeaderDraftPlan,
        LeaderPeerReview,
        LeaderRevisedPlan,
        LeaderRevisionContextBundle,
        SuperLeaderGlobalReview,
    )
    from agent_orchestra.runtime.bootstrap_round import LeaderRound
    from agent_orchestra.runtime.resident_kernel import ResidentCoordinatorKernel
    from agent_orchestra.runtime.teammate_runtime import ResidentTeammateRunResult
    from agent_orchestra.runtime.teammate_work_surface import RequestPermission


def _build_blackboard_id(
    *,
    group_id: str,
    kind: BlackboardKind,
    lane_id: str | None = None,
    team_id: str | None = None,
) -> str:
    if kind == BlackboardKind.LEADER_LANE:
        if lane_id is None:
            raise ValueError("lane_id is required for leader-lane blackboards")
        return f"{group_id}:leader_lane:{lane_id}"
    if team_id is None:
        raise ValueError("team_id is required for team blackboards")
    return f"{group_id}:team:{team_id}"


def _string_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    result: list[str] = []
    for item in value:
        if isinstance(item, str) and item.strip():
            result.append(item.strip())
    return tuple(result)


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _merge_unique_strings(*values: object) -> tuple[str, ...]:
    merged: list[str] = []
    seen: set[str] = set()
    for value in values:
        if isinstance(value, str):
            candidates = (value,)
        elif isinstance(value, (list, tuple, set)):
            candidates = value
        else:
            continue
        for item in candidates:
            normalized = str(item).strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            merged.append(normalized)
    return tuple(merged)


def _append_unique_notice(target: list[str], value: object) -> None:
    if value is None:
        return
    normalized = str(value).strip()
    if not normalized or normalized in target:
        return
    target.append(normalized)


def _normalize_relative_path(path: str) -> str:
    normalized = PurePosixPath(path).as_posix()
    if normalized == ".":
        return normalized
    return normalized.lstrip("./")


def _normalized_scope_paths(
    values: tuple[str, ...],
    *,
    working_dir: Path,
) -> tuple[PurePosixPath, ...]:
    normalized: list[PurePosixPath] = []
    for raw_path in values:
        candidate = Path(raw_path)
        if candidate.is_absolute():
            try:
                relative = candidate.resolve().relative_to(working_dir.resolve())
            except ValueError:
                normalized.append(PurePosixPath(candidate.as_posix()))
                continue
            normalized.append(PurePosixPath(_normalize_relative_path(relative.as_posix())))
            continue
        canonical = _canonical_relative_path(working_dir, working_dir / candidate)
        if canonical is not None:
            normalized.append(PurePosixPath(canonical))
            continue
        normalized.append(PurePosixPath(_normalize_relative_path(candidate.as_posix())))
    return tuple(normalized)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalized_actor_id(actor_id: str | None) -> str | None:
    if actor_id is None:
        return None
    normalized = actor_id.strip()
    return normalized if normalized else None


def _is_teammate_actor(actor_id: str | None) -> bool:
    normalized = _normalized_actor_id(actor_id)
    if normalized is None:
        return False
    return "teammate" in {
        segment.strip().lower()
        for segment in normalized.split(":")
        if segment.strip()
    }


def _lane_delivery_id(objective_id: str, lane_id: str) -> str:
    return f"{objective_id}:lane:{lane_id}"


def _merge_task_ids(
    existing: tuple[str, ...],
    *,
    add: tuple[str, ...] = (),
    remove: tuple[str, ...] = (),
) -> tuple[str, ...]:
    removed = {item for item in remove if item}
    merged: list[str] = []
    for item in existing:
        if item and item not in removed and item not in merged:
            merged.append(item)
    for item in add:
        if item and item not in removed and item not in merged:
            merged.append(item)
    return tuple(merged)


def _task_has_activation_contract(task: TaskCard) -> bool:
    return any(
        (
            task.slice_id is not None,
            task.slice_mode is not None,
            bool(task.depends_on_slice_ids),
            bool(task.depends_on_task_ids),
            task.parallel_group is not None,
        )
    )


@dataclass(slots=True)
class DirectedTaskReceiptCommit:
    task: TaskCard
    receipt: DirectedTaskReceipt
    blackboard_entry: BlackboardEntry
    delivery_state: DeliveryState | None = None
    session_snapshot: AgentSession | None = None
    protocol_bus_cursor: dict[str, str | None] | None = None
    post_commit_outbox: tuple[CoordinationOutboxRecord, ...] = ()

    @property
    def outbox(self) -> tuple[CoordinationOutboxRecord, ...]:
        return self.post_commit_outbox


@dataclass(slots=True)
class TeammateResultCommit:
    task: TaskCard
    blackboard_entry: BlackboardEntry
    delivery_state: DeliveryState | None = None
    session_snapshot: AgentSession | None = None
    post_commit_outbox: tuple[CoordinationOutboxRecord, ...] = ()

    @property
    def outbox(self) -> tuple[CoordinationOutboxRecord, ...]:
        return self.post_commit_outbox


@dataclass(slots=True)
class AuthorityRequestCommit:
    task: TaskCard
    authority_request: ScopeExtensionRequest
    blackboard_entry: BlackboardEntry
    delivery_state: DeliveryState | None = None
    session_snapshot: AgentSession | None = None
    post_commit_outbox: tuple[CoordinationOutboxRecord, ...] = ()

    @property
    def outbox(self) -> tuple[CoordinationOutboxRecord, ...]:
        return self.post_commit_outbox


@dataclass(slots=True)
class AuthorityDecisionCommit:
    task: TaskCard
    authority_decision: AuthorityDecision
    blackboard_entry: BlackboardEntry
    delivery_state: DeliveryState | None = None
    session_snapshot: AgentSession | None = None
    replacement_task: TaskCard | None = None
    post_commit_outbox: tuple[CoordinationOutboxRecord, ...] = ()

    @property
    def outbox(self) -> tuple[CoordinationOutboxRecord, ...]:
        return self.post_commit_outbox


@dataclass(slots=True)
class TaskSurfaceMutationCommit:
    task: TaskCard
    blackboard_entry: BlackboardEntry
    delivery_state: DeliveryState | None = None
    session_snapshot: AgentSession | None = None
    authority_request: ScopeExtensionRequest | None = None
    post_commit_outbox: tuple[CoordinationOutboxRecord, ...] = ()

    @property
    def outbox(self) -> tuple[CoordinationOutboxRecord, ...]:
        return self.post_commit_outbox


@dataclass(slots=True)
class PendingAuthorityRequest:
    task: TaskCard
    authority_request: ScopeExtensionRequest
    boundary_class: str
    waiting_since: str | None = None


@dataclass(slots=True)
class ResidentLaneLiveView:
    objective_id: str
    lane_id: str
    team_id: str | None = None
    delivery_state: DeliveryState | None = None
    coordinator_session: CoordinatorSessionState | None = None
    resident_team_shell: ResidentTeamShell | None = None
    shell_attach_decision: ShellAttachDecision | None = None
    message_runtime: dict[str, object] = field(default_factory=dict)
    coordination_metadata: dict[str, object] = field(default_factory=dict)
    task_surface_authority: dict[str, object] = field(default_factory=dict)
    pending_shared_digest_count: int = 0
    shared_digest_envelope_ids: tuple[str, ...] = ()


@dataclass(slots=True, frozen=True)
class RuntimeTaskSurfaceAuthorityView:
    task: TaskSurfaceAuthorityView
    actor_id: str | None = None
    actor_is_teammate: bool = False
    ancestor_views: tuple[TaskSurfaceAuthorityView, ...] = ()
    ancestor_task_ids: tuple[str, ...] = ()
    lineage_task_ids: tuple[str, ...] = ()
    subtree_root_task_id: str | None = None
    subtree_lineage_task_ids: tuple[str, ...] = ()

    @property
    def parent_task_id(self) -> str | None:
        return self.task.parent_task_id

    @property
    def task_id(self) -> str:
        return self.task.task_id

    @property
    def scope(self) -> TaskScope:
        return self.task.scope

    @property
    def local_status_actor_ids(self) -> tuple[str, ...]:
        return self.task.local_status_actor_ids

    @property
    def local_structure_actor_ids(self) -> tuple[str, ...]:
        return self.task.local_structure_actor_ids

    @property
    def protected_read_only_fields(self) -> tuple[str, ...]:
        return self.task.protected_read_only_fields

    def has_subtree_structure_authority(self) -> bool:
        return self.subtree_root_task_id is not None


@dataclass(slots=True)
class _StoreProtocolBusCursorCommit:
    stream: str
    consumer: str
    cursor: dict[str, str | None]


@dataclass(slots=True)
class _StoreDirectedTaskReceiptCommit:
    task: TaskCard
    blackboard_entry: BlackboardEntry
    receipt: DirectedTaskReceipt | None = None
    delivery_state: DeliveryState | None = None
    protocol_bus_cursor: _StoreProtocolBusCursorCommit | None = None
    session_snapshot: AgentSession | None = None
    worker_session_snapshot: WorkerSession | None = None
    post_commit_outbox: tuple[CoordinationOutboxRecord, ...] = ()

    @property
    def agent_session(self) -> AgentSession | None:
        return self.session_snapshot

    @property
    def slot_session(self) -> AgentSession | None:
        return self.session_snapshot

    @property
    def worker_session(self) -> WorkerSession | None:
        return self.worker_session_snapshot

    @property
    def outbox(self) -> tuple[CoordinationOutboxRecord, ...]:
        return self.post_commit_outbox


@dataclass(slots=True)
class _StoreTeammateResultCommit:
    task: TaskCard
    blackboard_entry: BlackboardEntry
    delivery_state: DeliveryState | None = None
    session_snapshot: AgentSession | None = None
    worker_session_snapshot: WorkerSession | None = None
    post_commit_outbox: tuple[CoordinationOutboxRecord, ...] = ()

    @property
    def agent_session(self) -> AgentSession | None:
        return self.session_snapshot

    @property
    def slot_session(self) -> AgentSession | None:
        return self.session_snapshot

    @property
    def worker_session(self) -> WorkerSession | None:
        return self.worker_session_snapshot

    @property
    def outbox(self) -> tuple[CoordinationOutboxRecord, ...]:
        return self.post_commit_outbox


def _merge_strings(
    existing: tuple[str, ...],
    *,
    add: tuple[str, ...] = (),
    remove: tuple[str, ...] = (),
) -> tuple[str, ...]:
    removed = {item for item in remove if item}
    merged: list[str] = []
    for item in existing:
        if item and item not in removed and item not in merged:
            merged.append(item)
    for item in add:
        if item and item not in removed and item not in merged:
            merged.append(item)
    return tuple(merged)


def _planning_review_contracts():
    from agent_orchestra.contracts import planning_review as contracts

    return contracts


def _planning_payload(value: object) -> dict[str, object]:
    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        payload = to_dict()
        if isinstance(payload, Mapping):
            return {str(key): item for key, item in payload.items()}
    raise ValueError("Planning review payload must be a mapping or expose to_dict().")


def _planning_payloads(values: tuple[object, ...]) -> tuple[dict[str, object], ...]:
    payloads: list[dict[str, object]] = []
    for value in values:
        payloads.append(_planning_payload(value))
    return tuple(payloads)


def _coerce_planning_contract_instance(contract_type: object, payload: Mapping[str, object]) -> object:
    from_payload = getattr(contract_type, "from_payload", None)
    if callable(from_payload):
        return from_payload(payload)
    if callable(contract_type):
        return contract_type(**payload)  # type: ignore[misc]
    raise TypeError(f"Unsupported planning review contract type: {contract_type!r}")


def _planning_generated_id(contracts: object, maker_name: str, *, prefix: str) -> str:
    maker = getattr(contracts, maker_name, None)
    if callable(maker):
        generated = maker()
        if isinstance(generated, str) and generated:
            return generated
    return f"{prefix}-{_utc_now_iso()}"


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_relative_path(root: Path, candidate: Path) -> str | None:
    root_resolved = root.resolve(strict=False)
    candidate_resolved = candidate.resolve(strict=False)
    try:
        relative = candidate_resolved.relative_to(root_resolved)
    except ValueError:
        return None
    return _normalize_relative_path(relative.as_posix())


class GroupRuntime:
    def __init__(
        self,
        *,
        store: OrchestrationStore,
        bus: EventBus,
        reducer: Reducer | None = None,
        launch_backends: dict[str, LaunchBackend] | None = None,
        supervisor: WorkerSupervisor | None = None,
        authority_policy: AuthorityPolicy | None = None,
        hierarchical_review_policy: HierarchicalReviewPolicy | None = None,
        session_domain_service: SessionDomainService | None = None,
    ) -> None:
        self.store = store
        self.bus = bus
        self.reducer = reducer or Reducer()
        self.leader_lane_blackboard_reducer = LeaderLaneBlackboardReducer()
        self.team_blackboard_reducer = TeamBlackboardReducer()
        self.launch_backends = dict(launch_backends or {})
        self.supervisor = supervisor
        self.authority_policy = authority_policy or AuthorityPolicy.default()
        self.hierarchical_review_policy = (
            hierarchical_review_policy or HierarchicalReviewPolicy.default()
        )
        self._session_domain_service_instance: SessionDomainService | None = session_domain_service
        self._objective_work_session_ids: dict[str, str] = {}

    def _session_host(self):
        if self.supervisor is None:
            return None
        return getattr(self.supervisor, "session_host", None)

    def _session_domain_service(self) -> SessionDomainService:
        if self._session_domain_service_instance is None:
            self._session_domain_service_instance = SessionDomainService(
                store=self.store,
                supervisor=self.supervisor,
            )
        return self._session_domain_service_instance

    async def _resolve_work_session_for_objective(
        self,
        *,
        group_id: str,
        objective_id: str | None,
    ) -> WorkSession | None:
        if objective_id is None:
            return None
        known_work_session_id = self._objective_work_session_ids.get(objective_id)
        if known_work_session_id is not None:
            session = await self.store.get_work_session(known_work_session_id)
            if session is not None:
                return session
        sessions = await self.store.list_work_sessions(group_id)
        matching = [
            session
            for session in sessions
            if getattr(session, "root_objective_id", None) == objective_id
        ]
        if not matching:
            return None
        matching.sort(
            key=lambda session: (
                str(getattr(session, "updated_at", "")),
                str(getattr(session, "work_session_id", "")),
            )
        )
        session = matching[-1]
        self._objective_work_session_ids[objective_id] = session.work_session_id
        return session

    async def new_session(
        self,
        *,
        group_id: str,
        objective_id: str,
        title: str | None = None,
        metadata: Mapping[str, object] | None = None,
    ):
        continuity = await self._session_domain_service().new_session(
            group_id=group_id,
            objective_id=objective_id,
            title=title,
            metadata=metadata,
        )
        self._objective_work_session_ids[objective_id] = continuity.work_session.work_session_id
        return continuity

    async def list_work_sessions(
        self,
        *,
        group_id: str,
        objective_id: str | None = None,
    ):
        return await self._session_domain_service().list_sessions(
            group_id=group_id,
            root_objective_id=objective_id,
        )

    async def inspect_session(self, work_session_id: str) -> SessionInspectSnapshot:
        return await self._session_domain_service().inspect_session(work_session_id)

    async def warm_resume(
        self,
        *,
        work_session_id: str,
        head_contracts: Mapping[tuple[str, str], Mapping[str, object]] | None = None,
    ):
        continuity = await self._session_domain_service().warm_resume(
            work_session_id=work_session_id,
            head_contracts=head_contracts,
        )
        self._objective_work_session_ids[continuity.work_session.root_objective_id] = (
            continuity.work_session.work_session_id
        )
        return continuity

    async def fork_session(
        self,
        *,
        work_session_id: str,
        title: str | None = None,
    ):
        return await self._session_domain_service().fork_session(
            work_session_id=work_session_id,
            title=title,
        )

    async def resume_gate(self, work_session_id: str):
        return await self._session_domain_service().resume_gate(work_session_id)

    async def exact_wake(self, work_session_id: str) -> SessionResumeResult:
        return await self._session_domain_service().exact_wake(work_session_id)

    async def attach_session(
        self,
        work_session_id: str,
        force_warm_resume: bool = False,
    ) -> SessionResumeResult:
        return await self._session_domain_service().attach_session(
            work_session_id,
            force_warm_resume=force_warm_resume,
        )

    async def wake_session(
        self,
        work_session_id: str,
    ) -> SessionResumeResult:
        return await self._session_domain_service().wake_session(work_session_id)

    async def _lane_task_surface_authority_snapshot(
        self,
        *,
        group_id: str | None,
        lane_id: str,
        team_id: str | None,
        delivery_state: DeliveryState | None,
    ) -> dict[str, object]:
        metadata = (
            delivery_state.metadata
            if delivery_state is not None and isinstance(delivery_state.metadata, Mapping)
            else {}
        )
        coordination = (
            {
                str(key): value
                for key, value in metadata.get("teammate_coordination", {}).items()
            }
            if isinstance(metadata.get("teammate_coordination"), Mapping)
            else {}
        )
        waiting_task_ids: list[str] = list(
            _merge_unique_strings(metadata.get("waiting_for_authority_task_ids", ()))
        )
        task_by_id: dict[str, TaskCard] = {}
        if group_id is not None and team_id is not None:
            for existing_task in await self.store.list_tasks(
                group_id,
                team_id=team_id,
                lane_id=lane_id,
            ):
                hydrated_task = await self._hydrate_task_activation_contract(existing_task)
                task_by_id[hydrated_task.task_id] = hydrated_task
                if (
                    hydrated_task.status == TaskStatus.WAITING_FOR_AUTHORITY
                    and hydrated_task.task_id not in waiting_task_ids
                ):
                    waiting_task_ids.append(hydrated_task.task_id)

        task_surface_waiting_task_ids: list[str] = []
        mutation_waiting_task_ids: list[str] = []
        protected_field_waiting_task_ids: list[str] = []
        waiting_request_ids: list[str] = []
        boundary_classes_by_task_id: dict[str, str] = {}

        for task_id in waiting_task_ids:
            task = task_by_id.get(task_id)
            if task is None:
                existing_task = await self.store.get_task(task_id)
                if existing_task is None:
                    continue
                task = await self._hydrate_task_activation_contract(existing_task)
                task_by_id[task.task_id] = task
            request = self._task_authority_request(task)
            if request is not None and request.request_id not in waiting_request_ids:
                waiting_request_ids.append(request.request_id)
            intent = self._task_surface_authority_intent(task)
            if intent is None:
                continue
            task_surface_waiting_task_ids.append(task.task_id)
            if task.authority_boundary_class:
                boundary_classes_by_task_id[task.task_id] = task.authority_boundary_class
            if intent.kind == TaskSurfaceAuthorityIntentKind.MUTATION:
                mutation_waiting_task_ids.append(task.task_id)
            else:
                protected_field_waiting_task_ids.append(task.task_id)

        snapshot: dict[str, object] = {
            "authority_waiting": bool(waiting_task_ids),
            "waiting_task_ids": list(waiting_task_ids),
            "waiting_request_ids": list(waiting_request_ids),
            "task_surface_waiting_task_ids": list(task_surface_waiting_task_ids),
            "mutation_waiting_task_ids": list(mutation_waiting_task_ids),
            "protected_field_waiting_task_ids": list(protected_field_waiting_task_ids),
        }
        authority_request_count = int(coordination.get("authority_request_count", 0) or 0)
        if authority_request_count > 0:
            snapshot["authority_request_count"] = authority_request_count
        if boundary_classes_by_task_id:
            snapshot["boundary_classes_by_task_id"] = dict(boundary_classes_by_task_id)

        last_mutation_task_id = coordination.get("last_task_surface_mutation_task_id")
        if isinstance(last_mutation_task_id, str) and last_mutation_task_id.strip():
            snapshot["last_mutation"] = {
                "task_id": last_mutation_task_id.strip(),
                "actor_id": str(coordination.get("last_task_surface_mutation_actor_id", "")).strip(),
                "kind": str(coordination.get("last_task_surface_mutation_kind", "")).strip(),
                "target_task_id": (
                    str(coordination["last_task_surface_mutation_target_task_id"]).strip()
                    if coordination.get("last_task_surface_mutation_target_task_id") is not None
                    else None
                ),
                "blackboard_entry_id": str(
                    coordination.get("last_task_surface_mutation_blackboard_entry_id", "")
                ).strip(),
            }

        last_write_task_id = coordination.get("last_task_surface_write_task_id")
        if isinstance(last_write_task_id, str) and last_write_task_id.strip():
            snapshot["last_write"] = {
                "task_id": last_write_task_id.strip(),
                "actor_id": str(coordination.get("last_task_surface_write_actor_id", "")).strip(),
                "field_names": list(
                    _merge_unique_strings(
                        coordination.get("last_task_surface_write_field_names", ())
                    )
                ),
                "blackboard_entry_id": str(
                    coordination.get("last_task_surface_write_blackboard_entry_id", "")
                ).strip(),
            }

        meaningful_keys = {
            "authority_request_count",
            "last_mutation",
            "last_write",
        }
        if not waiting_task_ids and not any(key in snapshot for key in meaningful_keys):
            return {}
        return snapshot

    async def read_resident_lane_live_views(
        self,
        *,
        objective_id: str,
        lane_ids: tuple[str, ...],
        host_owner_coordinator_id: str | None = None,
    ) -> tuple[ResidentLaneLiveView, ...]:
        if not lane_ids:
            return ()
        lane_id_set = set(lane_ids)
        objective = await self.store.get_objective(objective_id)
        group_id = objective.group_id if objective is not None else None
        delivery_states = await self.store.list_delivery_states(objective_id)
        delivery_by_lane = {
            state.lane_id: state
            for state in delivery_states
            if state.kind == DeliveryStateKind.LANE
            and state.lane_id in lane_id_set
        }
        sessions_by_lane: dict[str, CoordinatorSessionState] = {}
        session_host = self._session_host()
        if session_host is not None:
            sessions_by_lane = await session_host.list_coordinator_sessions_by_lane(
                role="leader",
                objective_id=objective_id,
                host_owner_coordinator_id=host_owner_coordinator_id,
            )
        live_views: list[ResidentLaneLiveView] = []
        for lane_id in lane_ids:
            delivery_state = delivery_by_lane.get(lane_id)
            coordinator_session = sessions_by_lane.get(lane_id)
            resident_team_shell = None
            shell_attach_decision = None
            team_id = None
            message_runtime: dict[str, object] = {}
            coordination_metadata: dict[str, object] = {}
            pending_shared_digest_count = 0
            shared_digest_envelope_ids: tuple[str, ...] = ()
            if delivery_state is not None:
                team_id = delivery_state.team_id
                delivery_metadata = (
                    delivery_state.metadata if isinstance(delivery_state.metadata, Mapping) else {}
                )
                raw_message_runtime = delivery_metadata.get("message_runtime")
                if isinstance(raw_message_runtime, Mapping):
                    message_runtime = {
                        str(key): value for key, value in raw_message_runtime.items()
                    }
                    raw_pending_digest_count = message_runtime.get("pending_shared_digest_count", 0)
                    try:
                        pending_shared_digest_count = int(raw_pending_digest_count or 0)
                    except (TypeError, ValueError):
                        pending_shared_digest_count = 0
                    pending_shared_digest_count = max(pending_shared_digest_count, 0)
                    shared_digest_envelope_ids = _string_tuple(
                        message_runtime.get("shared_digest_envelope_ids")
                    )
                coordination_metadata = {
                    str(key): value
                    for key, value in delivery_metadata.items()
                    if str(key) not in {"message_runtime", "lane_session"}
                }
            if team_id is None and coordinator_session is not None:
                team_id = coordinator_session.team_id
            if session_host is not None:
                resident_team_shell = await session_host.inspect_resident_team_shell(
                    objective_id=objective_id,
                    lane_id=lane_id,
                    team_id=team_id,
                )
                if resident_team_shell is not None:
                    shell_attach_decision = await session_host.find_preferred_attach_target(
                        resident_team_shell_id=resident_team_shell.resident_team_shell_id,
                    )
            task_surface_authority = await self._lane_task_surface_authority_snapshot(
                group_id=group_id,
                lane_id=lane_id,
                team_id=team_id,
                delivery_state=delivery_state,
            )
            live_views.append(
                ResidentLaneLiveView(
                    objective_id=objective_id,
                    lane_id=lane_id,
                    team_id=team_id,
                    delivery_state=delivery_state,
                    coordinator_session=coordinator_session,
                    resident_team_shell=resident_team_shell,
                    shell_attach_decision=shell_attach_decision,
                    message_runtime=message_runtime,
                    coordination_metadata=coordination_metadata,
                    task_surface_authority=task_surface_authority,
                    pending_shared_digest_count=pending_shared_digest_count,
                    shared_digest_envelope_ids=shared_digest_envelope_ids,
                )
            )
        return tuple(live_views)

    async def run_resident_teammate_host_sweep(
        self,
        *,
        mailbox: MailboxBridge,
        objective: ObjectiveSpec,
        leader_round: "LeaderRound",
        request_permission: "RequestPermission",
        resident_kernel: "ResidentCoordinatorKernel | None" = None,
        keep_session_idle: bool = False,
        execution_policy: WorkerExecutionPolicy | None = None,
        backend: str | None = None,
        working_dir: str | None = None,
        turn_index: int | None = None,
        role_profile: "WorkerRoleProfile | None" = None,
    ) -> "ResidentTeammateRunResult":
        from agent_orchestra.runtime.teammate_work_surface import TeammateWorkSurface

        # Standalone host sweeps resume only from host-owned slot truth.
        _ = backend, working_dir, role_profile
        surface = TeammateWorkSurface(
            runtime=self,
            mailbox=mailbox,
            objective=objective,
            leader_round=leader_round,
            backend=None,
            working_dir=None,
            turn_index=turn_index,
            role_profile=None,
            session_host=self._session_host(),
        )
        return await surface.step_runnable_host_slots(
            request_permission=request_permission,
            resident_kernel=resident_kernel,
            keep_session_idle=keep_session_idle,
            execution_policy=execution_policy,
        )

    async def create_group(self, group_id: str, display_name: str | None = None) -> Group:
        group = Group(group_id=group_id, display_name=display_name)
        await self.store.save_group(group)
        await self.bus.publish(OrchestraEvent.group_created(group_id))
        return group

    async def create_team(
        self,
        *,
        group_id: str,
        team_id: str,
        name: str,
        member_ids: tuple[str, ...] = (),
    ) -> Team:
        group = await self.store.get_group(group_id)
        if group is None:
            raise ValueError(f"Unknown group_id: {group_id}")
        team = Team(team_id=team_id, group_id=group_id, name=name, member_ids=member_ids)
        await self.store.save_team(team)
        await self.bus.publish(OrchestraEvent.team_created(group_id, team_id))
        return team

    async def create_objective(
        self,
        *,
        group_id: str,
        objective_id: str | None = None,
        title: str,
        description: str,
        success_metrics: tuple[str, ...] = (),
        hard_constraints: tuple[str, ...] = (),
        budget: dict[str, object] | None = None,
        metadata: dict[str, object] | None = None,
    ) -> ObjectiveSpec:
        group = await self.store.get_group(group_id)
        if group is None:
            raise ValueError(f"Unknown group_id: {group_id}")
        objective = ObjectiveSpec(
            objective_id=objective_id or make_objective_id(),
            group_id=group_id,
            title=title,
            description=description,
            success_metrics=success_metrics,
            hard_constraints=hard_constraints,
            budget=budget or {},
            metadata=metadata or {},
        )
        await self.store.save_objective(objective)
        return objective

    async def apply_planning_result(self, result: PlanningResult) -> PlanningResult:
        group = await self.store.get_group(result.objective.group_id)
        if group is None:
            raise ValueError(f"Unknown group_id: {result.objective.group_id}")
        await self.store.save_objective(result.objective)
        for node in result.spec_nodes:
            await self.store.save_spec_node(node)
        for edge in result.spec_edges:
            await self.store.save_spec_edge(edge)
        return result

    async def plan_from_template(self, planner: Planner, template: ObjectiveTemplate) -> PlanningResult:
        result = await planner.build_initial_plan(template)
        return await self.apply_planning_result(result)

    def _backend(self, name: str) -> LaunchBackend:
        try:
            return self.launch_backends[name]
        except KeyError as exc:
            raise ValueError(f"Unknown backend: {name}") from exc

    def _backend_capabilities(self, name: str) -> WorkerBackendCapabilities:
        backend = self._backend(name)
        describe = getattr(backend, "describe_capabilities", None)
        if callable(describe):
            capabilities = describe()
            if isinstance(capabilities, WorkerBackendCapabilities):
                return capabilities
        return WorkerBackendCapabilities()

    def _coerce_execution_contract(self, payload: object) -> WorkerExecutionContract | None:
        if payload is None:
            return None
        if isinstance(payload, WorkerExecutionContract):
            return payload
        if not isinstance(payload, Mapping):
            return None
        contract_id = payload.get("contract_id")
        mode = payload.get("mode")
        if not isinstance(contract_id, str) or not contract_id.strip():
            return None
        if not isinstance(mode, str) or not mode.strip():
            return None
        raw_required_artifact_kinds = payload.get("required_artifact_kinds", ())
        raw_required_verification_commands = payload.get("required_verification_commands", ())
        if not isinstance(raw_required_artifact_kinds, (list, tuple)):
            raw_required_artifact_kinds = ()
        if not isinstance(raw_required_verification_commands, (list, tuple)):
            raw_required_verification_commands = ()
        raw_progress_policy = payload.get("progress_policy", {})
        raw_metadata = payload.get("metadata", {})
        return WorkerExecutionContract(
            contract_id=contract_id.strip(),
            mode=mode.strip(),
            allow_subdelegation=bool(payload.get("allow_subdelegation", False)),
            require_final_report=bool(payload.get("require_final_report", True)),
            require_verification_results=bool(payload.get("require_verification_results", False)),
            required_verification_commands=tuple(
                item.strip()
                for item in raw_required_verification_commands
                if isinstance(item, str) and item.strip()
            ),
            completion_requires_verification_success=bool(
                payload.get("completion_requires_verification_success", False)
            ),
            required_artifact_kinds=tuple(
                item.strip()
                for item in raw_required_artifact_kinds
                if isinstance(item, str) and item.strip()
            ),
            progress_policy=dict(raw_progress_policy) if isinstance(raw_progress_policy, Mapping) else {},
            metadata=dict(raw_metadata) if isinstance(raw_metadata, Mapping) else {},
        )

    def _coerce_lease_policy(self, payload: object) -> WorkerLeasePolicy | None:
        if payload is None:
            return None
        if isinstance(payload, WorkerLeasePolicy):
            return payload
        if not isinstance(payload, Mapping):
            return None
        try:
            accept_deadline_seconds = float(payload["accept_deadline_seconds"])
            renewal_timeout_seconds = float(payload["renewal_timeout_seconds"])
            hard_deadline_seconds = float(payload["hard_deadline_seconds"])
        except (KeyError, TypeError, ValueError):
            return None
        raw_renew_on_event_kinds = payload.get(
            "renew_on_event_kinds",
            ("accepted", "checkpoint", "phase_changed", "verifying"),
        )
        if not isinstance(raw_renew_on_event_kinds, (list, tuple)):
            raw_renew_on_event_kinds = ("accepted", "checkpoint", "phase_changed", "verifying")
        raw_max_silence_seconds = payload.get("max_silence_seconds")
        max_silence_seconds = None
        if raw_max_silence_seconds is not None:
            try:
                max_silence_seconds = float(raw_max_silence_seconds)
            except (TypeError, ValueError):
                return None
        return WorkerLeasePolicy(
            accept_deadline_seconds=accept_deadline_seconds,
            renewal_timeout_seconds=renewal_timeout_seconds,
            hard_deadline_seconds=hard_deadline_seconds,
            renew_on_event_kinds=tuple(
                item.strip()
                for item in raw_renew_on_event_kinds
                if isinstance(item, str) and item.strip()
            ),
            max_silence_seconds=max_silence_seconds,
        )

    def _resolved_execution_contract(
        self,
        assignment: WorkerAssignment,
        policy: WorkerExecutionPolicy | None,
    ) -> WorkerExecutionContract | None:
        for source in (
            assignment.execution_contract,
            assignment.metadata.get("execution_contract"),
            assignment.role_profile.execution_contract if assignment.role_profile is not None else None,
            policy.execution_contract if policy is not None else None,
        ):
            contract = self._coerce_execution_contract(source)
            if contract is not None:
                return contract
        return None

    def _resolved_lease_policy(
        self,
        assignment: WorkerAssignment,
        policy: WorkerExecutionPolicy | None,
    ) -> WorkerLeasePolicy | None:
        for source in (
            assignment.lease_policy,
            assignment.metadata.get("lease_policy"),
            assignment.role_profile.lease_policy if assignment.role_profile is not None else None,
            policy.lease_policy if policy is not None else None,
        ):
            lease_policy = self._coerce_lease_policy(source)
            if lease_policy is not None:
                return lease_policy
        return None

    def _validate_backend_compatibility(
        self,
        assignment: WorkerAssignment,
        *,
        policy: WorkerExecutionPolicy | None,
    ) -> WorkerBackendCapabilities:
        capabilities = self._backend_capabilities(assignment.backend)
        contract = self._resolved_execution_contract(assignment, policy)
        lease_policy = self._resolved_lease_policy(assignment, policy)
        protocol_required = contract is not None or lease_policy is not None
        if protocol_required and not capabilities.supports_protocol_contract:
            raise RuntimeError(
                f"Backend `{assignment.backend}` does not support protocol-native execution for the requested contract."
            )
        if (
            contract is not None
            and contract.require_final_report
            and not capabilities.supports_protocol_final_report
        ):
            raise RuntimeError(
                f"Backend `{assignment.backend}` does not support required final_report emission."
            )
        if policy is not None and policy.keep_session_idle and not capabilities.supports_reactivate:
            raise RuntimeError(
                f"Backend `{assignment.backend}` does not support idle session reactivation."
            )
        if assignment.backend != "in_process" and not capabilities.supports_protocol_state:
            raise RuntimeError(
                f"Backend `{assignment.backend}` does not support protocol-native wait for out-of-process execution."
            )
        return capabilities

    async def launch_worker(self, assignment: WorkerAssignment) -> WorkerHandle:
        capabilities = self._backend_capabilities(assignment.backend)
        handle = await self._backend(assignment.backend).launch(assignment)
        handle.metadata.setdefault("backend_capabilities", capabilities.to_dict())
        handle.metadata.setdefault("resume_supported", capabilities.supports_resume)
        handle.metadata.setdefault("reattach_supported", capabilities.supports_reattach)
        if capabilities.supports_reactivate:
            handle.metadata.setdefault("reactivate_supported", True)
        return handle

    async def resume_worker(
        self,
        *,
        handle: WorkerHandle,
        assignment: WorkerAssignment | None = None,
    ) -> WorkerHandle:
        return await self._backend(handle.backend).resume(handle, assignment)

    async def cancel_worker(self, *, handle: WorkerHandle) -> None:
        await self._backend(handle.backend).cancel(handle)

    async def run_worker_assignment(
        self,
        assignment: WorkerAssignment,
        *,
        policy: WorkerExecutionPolicy | None = None,
    ) -> WorkerRecord:
        if self.supervisor is None:
            raise RuntimeError("WorkerSupervisor is required to run worker assignments.")
        session_domain_service = self._session_domain_service()
        continuity_work_session = await self._resolve_work_session_for_objective(
            group_id=assignment.group_id,
            objective_id=assignment.objective_id,
        )
        active_assignment = assignment
        current_runtime_generation_id: str | None = None
        if continuity_work_session is not None:
            current_runtime_generation_id = continuity_work_session.current_runtime_generation_id
            active_assignment = await session_domain_service.apply_assignment_continuity(
                work_session_id=continuity_work_session.work_session_id,
                runtime_generation_id=current_runtime_generation_id,
                assignment=assignment,
            )
            continuity_metadata = dict(active_assignment.metadata)
            continuity_metadata["work_session_id"] = continuity_work_session.work_session_id
            if current_runtime_generation_id is not None:
                continuity_metadata["runtime_generation_id"] = current_runtime_generation_id
            active_assignment = replace(active_assignment, metadata=continuity_metadata)
        self._validate_backend_compatibility(active_assignment, policy=policy)
        working_dir = self._working_dir(active_assignment)
        baseline_snapshot = self._snapshot_working_tree(working_dir)
        record = await self.supervisor.run_assignment_with_policy(
            active_assignment,
            launch=lambda active_assignment: self.launch_worker(active_assignment),
            resume=lambda handle, active_assignment=None: self.resume_worker(
                handle=handle,
                assignment=active_assignment,
            ),
            cancel=lambda handle: self.cancel_worker(handle=handle),
            policy=policy,
        )
        guarded_record = await self._apply_guard_to_record(
            active_assignment,
            record,
            baseline_snapshot=baseline_snapshot,
        )
        if continuity_work_session is not None:
            await session_domain_service.record_worker_turn(
                work_session_id=continuity_work_session.work_session_id,
                runtime_generation_id=current_runtime_generation_id,
                assignment=active_assignment,
                record=guarded_record,
            )
        return guarded_record

    async def apply_execution_guard(
        self,
        assignment: WorkerAssignment,
        *,
        handle: WorkerHandle | None = None,
        record: WorkerRecord | None = None,
        baseline_snapshot: dict[str, str] | None = None,
    ) -> ExecutionGuardResult:
        working_dir = self._working_dir(assignment)
        metadata: dict[str, object] = {"working_dir": str(working_dir)}
        ignored_paths = self._guard_ignored_paths(handle=handle, working_dir=working_dir)
        filtered_baseline = self._filter_snapshot(
            baseline_snapshot or {},
            root=working_dir,
            ignored_paths=ignored_paths,
        )
        current_snapshot = self._snapshot_working_tree(working_dir, ignored_paths=ignored_paths)
        modified_paths = tuple(
            sorted(
                path
                for path in set(filtered_baseline) | set(current_snapshot)
                if filtered_baseline.get(path) != current_snapshot.get(path)
            )
        )

        target_roots = self._target_roots(assignment, working_dir=working_dir)
        target_root_violations = tuple(
            path for path in modified_paths if not self._path_within_target_roots(path, target_roots)
        )

        if target_root_violations:
            return ExecutionGuardResult(
                status=ExecutionGuardStatus.PATH_VIOLATION,
                modified_paths=modified_paths,
                out_of_scope_paths=target_root_violations,
                summary="Modified paths escaped target_roots: " + ", ".join(target_root_violations),
                metadata={
                    **metadata,
                    "target_roots": [path.as_posix() for path in target_roots],
                },
            )

        owned_paths = self._owned_paths(assignment, working_dir=working_dir)
        if owned_paths:
            scope_drift_paths = tuple(
                path for path in modified_paths if not self._path_within_owned_scope(path, owned_paths)
            )
        else:
            scope_drift_paths = ()
        scope_drift_detected = bool(scope_drift_paths)
        if scope_drift_detected:
            metadata["scope_drift_paths"] = list(scope_drift_paths)
            metadata["scope_drift_count"] = len(scope_drift_paths)
            metadata["scope_drift_detected"] = True

        contract = self._resolved_execution_contract(assignment, None)
        verification_commands = (
            tuple(contract.required_verification_commands)
            if contract is not None and contract.required_verification_commands
            else _string_tuple(assignment.metadata.get("verification_commands"))
        )
        verification_results: list[VerificationCommandResult] = []
        if record is not None and record.status == WorkerStatus.COMPLETED:
            authoritative_results = list(self._authoritative_verification_results(record))
            authoritative_match_count = 0
            rerun_commands: list[str] = []
            for command in verification_commands:
                matched_result = select_best_equivalent_verification_result(
                    authoritative_results,
                    command,
                )
                if matched_result is None:
                    rerun_commands.append(command)
                    continue
                authoritative_results.remove(matched_result)
                authoritative_match_count += 1
                verification_results.append(
                    VerificationCommandResult(
                        command=matched_result.command,
                        returncode=matched_result.returncode,
                        requested_command=matched_result.requested_command or command,
                        stdout=matched_result.stdout,
                        stderr=matched_result.stderr,
                    )
                )
            if authoritative_match_count:
                metadata["authoritative_verification_results_used"] = authoritative_match_count
            if rerun_commands:
                metadata["verification_rerun_commands"] = list(rerun_commands)
            try:
                for command in rerun_commands:
                    completed = await asyncio.to_thread(
                        subprocess.run,
                        command,
                        cwd=str(working_dir),
                        shell=True,
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    verification_results.append(
                        VerificationCommandResult(
                            command=command,
                            returncode=completed.returncode,
                            stdout=completed.stdout,
                            stderr=completed.stderr,
                        )
                    )
            except BaseException as exc:
                metadata["error_type"] = exc.__class__.__name__
                return ExecutionGuardResult(
                    status=ExecutionGuardStatus.GUARD_ERROR,
                    modified_paths=modified_paths,
                    out_of_scope_paths=(),
                    verification_results=tuple(verification_results),
                    summary=f"Execution guard failed while running verification: {exc}",
                    metadata=metadata,
                )

            failed_commands = [result.command for result in verification_results if result.returncode != 0]
            if failed_commands:
                return ExecutionGuardResult(
                    status=ExecutionGuardStatus.VERIFICATION_FAILED,
                    modified_paths=modified_paths,
                    out_of_scope_paths=(),
                    verification_results=tuple(verification_results),
                    summary="Worker attempt failed verification commands: " + ", ".join(failed_commands),
                    metadata=metadata,
                )

        summary = "Execution guard passed."
        status = ExecutionGuardStatus.PASSED
        out_of_scope_paths: tuple[str, ...] = ()
        if scope_drift_detected:
            status = ExecutionGuardStatus.SCOPE_DRIFT
            out_of_scope_paths = scope_drift_paths
            summary = (
                "Execution guard recorded repo-local scope drift beyond owned_paths: "
                + ", ".join(scope_drift_paths)
            )
        if record is not None and record.status != WorkerStatus.COMPLETED:
            summary = "Execution guard recorded modified paths; verification skipped because the worker attempt failed."
        return ExecutionGuardResult(
            status=status,
            modified_paths=modified_paths,
            out_of_scope_paths=out_of_scope_paths,
            verification_results=tuple(verification_results),
            summary=summary,
            metadata=metadata,
        )

    def _authoritative_verification_results(
        self,
        record: WorkerRecord | None,
    ) -> tuple[VerificationCommandResult, ...]:
        if record is None or record.status != WorkerStatus.COMPLETED:
            return ()
        if record.metadata.get("supervision_mode") != "protocol_first":
            return ()
        if record.metadata.get("protocol_failure_reason"):
            return ()
        final_report = record.metadata.get("final_report")
        if not isinstance(final_report, Mapping):
            return ()
        raw_results = final_report.get("verification_results", ())
        if not isinstance(raw_results, (list, tuple)):
            return ()
        results: list[VerificationCommandResult] = []
        for item in raw_results:
            parsed = VerificationCommandResult.from_payload(item)
            if parsed is not None:
                results.append(parsed)
        return tuple(results)

    def _working_dir(self, assignment: WorkerAssignment) -> Path:
        return Path(assignment.working_dir) if assignment.working_dir else Path.cwd()

    def _guard_ignored_paths(self, *, handle: WorkerHandle | None, working_dir: Path) -> tuple[Path, ...]:
        if handle is None:
            return ()
        candidates: list[Path] = []
        if handle.transport_ref is not None:
            candidates.append(Path(handle.transport_ref))
        for key, value in handle.metadata.items():
            if not isinstance(value, str):
                continue
            if key == "spool_root" or key.endswith("_file"):
                candidates.append(Path(value))
        ignored: list[Path] = []
        for candidate in candidates:
            resolved = candidate if candidate.is_absolute() else working_dir / candidate
            ignored.append(resolved)
        return tuple(ignored)

    def _snapshot_working_tree(
        self,
        root: Path,
        *,
        ignored_paths: tuple[Path, ...] = (),
    ) -> dict[str, str]:
        if not root.exists() or not root.is_dir():
            return {}
        snapshot: dict[str, str] = {}
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            if ".git" in path.parts:
                continue
            if self._is_guard_cache_artifact(path):
                continue
            if self._path_is_guard_ignored(path, ignored_paths):
                continue
            relative_path = self._normalized_snapshot_path(root=root, candidate=path)
            snapshot[relative_path] = _hash_file(path)
        return snapshot

    def _filter_snapshot(
        self,
        snapshot: dict[str, str],
        *,
        root: Path,
        ignored_paths: tuple[Path, ...] = (),
    ) -> dict[str, str]:
        filtered: dict[str, str] = {}
        for relative_path, digest in snapshot.items():
            candidate = root / relative_path
            if self._path_is_guard_ignored(candidate, ignored_paths):
                continue
            if self._is_guard_cache_artifact(candidate):
                continue
            normalized = self._normalized_snapshot_path(root=root, candidate=candidate)
            filtered[normalized] = digest
        return filtered

    def _normalized_snapshot_path(self, *, root: Path, candidate: Path) -> str:
        canonical = _canonical_relative_path(root, candidate)
        if canonical is not None:
            return canonical
        try:
            relative = candidate.relative_to(root)
        except ValueError:
            return _normalize_relative_path(candidate.as_posix())
        return _normalize_relative_path(relative.as_posix())

    def _path_is_guard_ignored(self, candidate: Path, ignored_paths: tuple[Path, ...]) -> bool:
        for ignored_path in ignored_paths:
            if candidate == ignored_path:
                return True
            if ignored_path.is_dir() and ignored_path in candidate.parents:
                return True
        return False

    def _is_guard_cache_artifact(self, candidate: Path) -> bool:
        return (
            "__pycache__" in candidate.parts
            or ".pytest_cache" in candidate.parts
            or candidate.suffix in {".pyc", ".pyo"}
        )

    def _owned_paths(self, assignment: WorkerAssignment, *, working_dir: Path) -> tuple[PurePosixPath, ...]:
        return _normalized_scope_paths(
            _string_tuple(assignment.metadata.get("owned_paths")),
            working_dir=working_dir,
        )

    def _target_roots(self, assignment: WorkerAssignment, *, working_dir: Path) -> tuple[PurePosixPath, ...]:
        configured = _string_tuple(assignment.metadata.get("target_roots"))
        if not configured:
            configured = (".",)
        return _normalized_scope_paths(configured, working_dir=working_dir)

    def _path_within_owned_scope(self, path: str, owned_paths: tuple[PurePosixPath, ...]) -> bool:
        candidate = PurePosixPath(path)
        for owned_path in owned_paths:
            if owned_path == PurePosixPath(".") or candidate == owned_path or owned_path in candidate.parents:
                return True
        return False

    def _path_within_target_roots(self, path: str, target_roots: tuple[PurePosixPath, ...]) -> bool:
        candidate = PurePosixPath(path)
        for target_root in target_roots:
            if target_root == PurePosixPath(".") or candidate == target_root or target_root in candidate.parents:
                return True
        return False

    async def _apply_guard_to_record(
        self,
        assignment: WorkerAssignment,
        record: WorkerRecord,
        *,
        baseline_snapshot: dict[str, str],
    ) -> WorkerRecord:
        guard = await self.apply_execution_guard(
            assignment,
            handle=record.handle,
            record=record,
            baseline_snapshot=baseline_snapshot,
        )
        metadata = dict(record.metadata)
        metadata["guard_status"] = guard.status.value
        metadata["modified_paths"] = list(guard.modified_paths)
        metadata["out_of_scope_paths"] = list(guard.out_of_scope_paths)
        metadata["verification_results"] = [
            {
                "command": result.command,
                "returncode": result.returncode,
                "requested_command": result.requested_command,
                "stdout": result.stdout,
                "stderr": result.stderr,
            }
            for result in guard.verification_results
        ]
        metadata["guard_summary"] = guard.summary
        if guard.metadata:
            metadata["guard_metadata"] = dict(guard.metadata)
        record.metadata = metadata

        hard_failure_statuses = {
            ExecutionGuardStatus.PATH_VIOLATION,
            ExecutionGuardStatus.VERIFICATION_FAILED,
            ExecutionGuardStatus.GUARD_ERROR,
        }
        should_fail_record = guard.status in hard_failure_statuses

        if should_fail_record:
            record.status = WorkerStatus.FAILED
            if record.error_text:
                if guard.summary and guard.summary not in record.error_text:
                    record.error_text = f"{record.error_text}\n{guard.summary}"
            else:
                record.error_text = guard.summary

        await self.store.save_worker_record(record)
        return record

    async def add_spec_node(
        self,
        *,
        objective_id: str,
        node_id: str | None = None,
        kind: SpecNodeKind,
        title: str,
        summary: str,
        scope: TaskScope,
        created_by: str,
        lane_id: str | None = None,
        team_id: str | None = None,
        status: SpecNodeStatus = SpecNodeStatus.OPEN,
        metadata: dict[str, object] | None = None,
    ) -> SpecNode:
        objective = await self.store.get_objective(objective_id)
        if objective is None:
            raise ValueError(f"Unknown objective_id: {objective_id}")
        node = SpecNode(
            node_id=node_id or make_spec_node_id(),
            objective_id=objective_id,
            kind=kind,
            title=title,
            summary=summary,
            scope=scope,
            lane_id=lane_id,
            team_id=team_id,
            created_by=created_by,
            status=status,
            metadata=metadata or {},
        )
        await self.store.save_spec_node(node)
        return node

    async def add_spec_edge(
        self,
        *,
        objective_id: str,
        edge_id: str | None = None,
        kind: SpecEdgeKind,
        from_node_id: str,
        to_node_id: str,
        metadata: dict[str, object] | None = None,
    ) -> SpecEdge:
        objective = await self.store.get_objective(objective_id)
        if objective is None:
            raise ValueError(f"Unknown objective_id: {objective_id}")
        edge = SpecEdge(
            edge_id=edge_id or make_edge_id(),
            objective_id=objective_id,
            kind=kind,
            from_node_id=from_node_id,
            to_node_id=to_node_id,
            metadata=metadata or {},
        )
        await self.store.save_spec_edge(edge)
        return edge

    async def _load_task_surface_parent(
        self,
        task: TaskCard,
        *,
        require_existing: bool = True,
    ) -> TaskCard | None:
        parent_task_id = task.surface_parent_task_id()
        if parent_task_id is None:
            return None
        parent = await self.store.get_task(parent_task_id)
        if parent is None:
            if not require_existing:
                return None
            raise ValueError(
                f"Task {task.task_id} references unknown task surface parent {parent_task_id}."
            )
        return await self._hydrate_task_activation_contract(parent)

    async def _task_surface_ancestry(self, task: TaskCard) -> tuple[TaskCard, ...]:
        ancestors: list[TaskCard] = []
        seen = {task.task_id}
        current = task
        while True:
            parent_task_id = current.surface_parent_task_id()
            if parent_task_id is None or parent_task_id in seen:
                break
            seen.add(parent_task_id)
            parent = await self.store.get_task(parent_task_id)
            if parent is None:
                raise ValueError(
                    f"Task {task.task_id} references unknown task surface parent {parent_task_id}."
                )
            current = await self._hydrate_task_activation_contract(parent)
            ancestors.append(current)
        return tuple(ancestors)

    @staticmethod
    def _task_surface_authority_decision(
        verdict: TaskSurfaceAuthorityVerdict,
        reason: str,
        *,
        protected_field_names: tuple[str, ...] = (),
    ) -> TaskSurfaceAuthorityDecision:
        return TaskSurfaceAuthorityDecision(
            verdict=verdict,
            reason=reason,
            protected_field_names=protected_field_names,
        )

    @staticmethod
    def _raise_task_surface_authority_decision(
        decision: TaskSurfaceAuthorityDecision,
    ) -> None:
        if decision.verdict == TaskSurfaceAuthorityVerdict.LOCAL_ALLOW:
            return
        if decision.verdict == TaskSurfaceAuthorityVerdict.ESCALATION_NEEDED:
            raise PermissionError(decision.reason)
        raise ValueError(decision.reason)

    async def read_task_surface_authority_view(
        self,
        *,
        task_id: str,
        actor_id: str | None = None,
    ) -> RuntimeTaskSurfaceAuthorityView:
        task = await self.store.get_task(task_id)
        if task is None:
            raise ValueError(f"Unknown task_id: {task_id}")
        task = await self._hydrate_task_activation_contract(task)
        ancestors = await self._task_surface_ancestry(task)
        ancestor_views = tuple(
            ancestor.surface_authority_view()
            for ancestor in reversed(ancestors)
        )
        lineage = (*ancestor_views, task.surface_authority_view())
        normalized_actor_id = _normalized_actor_id(actor_id)
        actor_is_teammate = _is_teammate_actor(normalized_actor_id)
        subtree_root_index: int | None = None
        if actor_is_teammate:
            for index, lineage_view in enumerate(lineage):
                if lineage_view.has_local_structure_authority(normalized_actor_id):
                    subtree_root_index = index
                    break
        subtree_lineage = (
            tuple(view.task_id for view in lineage[subtree_root_index:])
            if subtree_root_index is not None
            else ()
        )
        return RuntimeTaskSurfaceAuthorityView(
            task=task.surface_authority_view(),
            actor_id=normalized_actor_id,
            actor_is_teammate=actor_is_teammate,
            ancestor_views=ancestor_views,
            ancestor_task_ids=tuple(view.task_id for view in ancestor_views),
            lineage_task_ids=tuple(view.task_id for view in lineage),
            subtree_root_task_id=(
                lineage[subtree_root_index].task_id
                if subtree_root_index is not None
                else None
            ),
            subtree_lineage_task_ids=subtree_lineage,
        )

    async def _teammate_has_structure_authority(
        self,
        *,
        task: TaskCard,
        actor_id: str,
    ) -> bool:
        normalized_actor_id = _normalized_actor_id(actor_id)
        if normalized_actor_id is None or task.scope != TaskScope.TEAM:
            return False
        if task.has_local_structure_authority(normalized_actor_id):
            return True
        for ancestor in await self._task_surface_ancestry(task):
            if ancestor.scope != TaskScope.TEAM:
                return False
            if ancestor.has_local_structure_authority(normalized_actor_id):
                return True
        return False

    async def classify_task_submission_authority(
        self,
        task: TaskCard,
    ) -> TaskSurfaceAuthorityDecision:
        created_by = _normalized_actor_id(task.created_by)
        require_existing_parent = _is_teammate_actor(created_by)
        parent = await self._load_task_surface_parent(
            task,
            require_existing=require_existing_parent,
        )
        reason = task.reason.strip()
        if task.surface_parent_task_id() is not None and not reason:
            return self._task_surface_authority_decision(
                TaskSurfaceAuthorityVerdict.FORBIDDEN,
                "Task surface child creation requires a non-empty reason.",
            )
        if not _is_teammate_actor(created_by):
            return self._task_surface_authority_decision(
                TaskSurfaceAuthorityVerdict.LOCAL_ALLOW,
                "Higher-authority task submission is allowed locally.",
            )
        if parent is None:
            return self._task_surface_authority_decision(
                TaskSurfaceAuthorityVerdict.ESCALATION_NEEDED,
                "Teammate task-surface writes must derive from an existing parent task.",
            )
        if task.scope != TaskScope.TEAM:
            return self._task_surface_authority_decision(
                TaskSurfaceAuthorityVerdict.ESCALATION_NEEDED,
                "Teammate task-surface writes cannot create upper-scope tasks locally.",
            )
        if parent.scope != TaskScope.TEAM:
            return self._task_surface_authority_decision(
                TaskSurfaceAuthorityVerdict.ESCALATION_NEEDED,
                "Teammate task-surface writes cannot attach children to upper-scope tasks locally.",
            )
        if task.group_id != parent.group_id or task.team_id != parent.team_id or task.lane != parent.lane:
            return self._task_surface_authority_decision(
                TaskSurfaceAuthorityVerdict.ESCALATION_NEEDED,
                "Teammate task-surface writes cannot cross group/team/lane boundaries without escalation.",
            )
        if not reason:
            return self._task_surface_authority_decision(
                TaskSurfaceAuthorityVerdict.FORBIDDEN,
                "Teammate task-surface writes require a non-empty reason.",
            )
        if (
            parent.has_local_status_authority(created_by)
            or await self._teammate_has_structure_authority(
                task=parent,
                actor_id=created_by,
            )
        ):
            return self._task_surface_authority_decision(
                TaskSurfaceAuthorityVerdict.LOCAL_ALLOW,
                "Teammate write stays within the assigned task or owned child subtree.",
            )
        return self._task_surface_authority_decision(
            TaskSurfaceAuthorityVerdict.ESCALATION_NEEDED,
            "Teammate task-surface writes are limited to the assigned task or the teammate-owned child subtree.",
        )

    async def _validate_task_submission_authority(self, task: TaskCard) -> None:
        decision = await self.classify_task_submission_authority(task)
        self._raise_task_surface_authority_decision(decision)

    async def classify_task_status_update_authority(
        self,
        *,
        task_id: str,
        status: TaskStatus,
        actor_id: str | None,
    ) -> TaskSurfaceAuthorityDecision:
        task = await self.store.get_task(task_id)
        if task is None:
            raise ValueError(f"Unknown task_id: {task_id}")
        task = await self._hydrate_task_activation_contract(task)
        if status == TaskStatus.CANCELLED:
            return self._task_surface_authority_decision(
                TaskSurfaceAuthorityVerdict.FORBIDDEN,
                "Direct task cancellation is not allowed; use commit_task_surface_mutation(...).",
            )
        normalized_actor_id = _normalized_actor_id(actor_id)
        if not _is_teammate_actor(normalized_actor_id):
            return self._task_surface_authority_decision(
                TaskSurfaceAuthorityVerdict.LOCAL_ALLOW,
                "Higher-authority status update is allowed locally.",
            )
        if task.scope != TaskScope.TEAM:
            return self._task_surface_authority_decision(
                TaskSurfaceAuthorityVerdict.ESCALATION_NEEDED,
                "Teammate cannot update status on upper-scope task-surface items without escalation.",
            )
        if task.has_local_status_authority(normalized_actor_id):
            return self._task_surface_authority_decision(
                TaskSurfaceAuthorityVerdict.LOCAL_ALLOW,
                "Teammate status update stays within the assigned or self-created task surface.",
            )
        return self._task_surface_authority_decision(
            TaskSurfaceAuthorityVerdict.ESCALATION_NEEDED,
            "Teammate can only update status on assigned or self-created task-surface items.",
        )

    async def _validate_task_status_update_authority(
        self,
        *,
        task: TaskCard,
        status: TaskStatus,
        actor_id: str | None,
    ) -> None:
        decision = await self.classify_task_status_update_authority(
            task_id=task.task_id,
            status=status,
            actor_id=actor_id,
        )
        self._raise_task_surface_authority_decision(decision)

    async def classify_task_surface_mutation_authority(
        self,
        *,
        task_id: str,
        actor_id: str,
        reason: str,
        target_task_id: str | None = None,
    ) -> TaskSurfaceAuthorityDecision:
        task = await self.store.get_task(task_id)
        if task is None:
            raise ValueError(f"Unknown task_id: {task_id}")
        task = await self._hydrate_task_activation_contract(task)
        normalized_actor_id = _normalized_actor_id(actor_id)
        if normalized_actor_id is None:
            return self._task_surface_authority_decision(
                TaskSurfaceAuthorityVerdict.FORBIDDEN,
                "Task surface mutation requires actor_id.",
            )
        if not reason.strip():
            return self._task_surface_authority_decision(
                TaskSurfaceAuthorityVerdict.FORBIDDEN,
                "Task surface mutation requires a non-empty reason.",
            )
        target_task: TaskCard | None = None
        if target_task_id is not None:
            target_task = await self.store.get_task(target_task_id)
            if target_task is None:
                return self._task_surface_authority_decision(
                    TaskSurfaceAuthorityVerdict.FORBIDDEN,
                    f"Unknown target_task_id: {target_task_id}",
                )
            target_task = await self._hydrate_task_activation_contract(target_task)
            if (
                task.group_id != target_task.group_id
                or task.team_id != target_task.team_id
                or task.lane != target_task.lane
            ):
                return self._task_surface_authority_decision(
                    TaskSurfaceAuthorityVerdict.ESCALATION_NEEDED,
                    "Task surface mutation targets must stay within the same group/team/lane boundary unless escalated.",
                )
        if not _is_teammate_actor(normalized_actor_id):
            return self._task_surface_authority_decision(
                TaskSurfaceAuthorityVerdict.LOCAL_ALLOW,
                "Higher-authority task surface mutation is allowed locally.",
            )
        if task.scope != TaskScope.TEAM:
            return self._task_surface_authority_decision(
                TaskSurfaceAuthorityVerdict.ESCALATION_NEEDED,
                "Teammate cannot mutate upper-scope task-surface items without escalation.",
            )
        if not await self._teammate_has_structure_authority(
            task=task,
            actor_id=normalized_actor_id,
        ):
            return self._task_surface_authority_decision(
                TaskSurfaceAuthorityVerdict.ESCALATION_NEEDED,
                "Teammate task-surface mutations are limited to the teammate-owned child subtree.",
            )
        if (
            target_task is not None
            and not (
                target_task.has_local_status_authority(normalized_actor_id)
                or await self._teammate_has_structure_authority(
                    task=target_task,
                    actor_id=normalized_actor_id,
                )
            )
        ):
            return self._task_surface_authority_decision(
                TaskSurfaceAuthorityVerdict.ESCALATION_NEEDED,
                "Teammate task-surface mutations cannot target tasks outside the owned subtree.",
            )
        return self._task_surface_authority_decision(
            TaskSurfaceAuthorityVerdict.LOCAL_ALLOW,
            "Task surface mutation stays within the teammate-owned child subtree.",
        )

    async def _validate_task_surface_mutation_authority(
        self,
        *,
        task: TaskCard,
        actor_id: str,
        reason: str,
        target_task_id: str | None = None,
    ) -> None:
        decision = await self.classify_task_surface_mutation_authority(
            task_id=task.task_id,
            actor_id=actor_id,
            reason=reason,
            target_task_id=target_task_id,
        )
        self._raise_task_surface_authority_decision(decision)

    async def classify_task_protected_field_write(
        self,
        *,
        task_id: str,
        actor_id: str | None,
        field_names: tuple[str, ...],
    ) -> TaskSurfaceAuthorityDecision:
        authority_view = await self.read_task_surface_authority_view(
            task_id=task_id,
            actor_id=actor_id,
        )
        protected_field_names = authority_view.task.protected_field_names(field_names)
        if not protected_field_names:
            return self._task_surface_authority_decision(
                TaskSurfaceAuthorityVerdict.LOCAL_ALLOW,
                "Requested field write does not touch the protected task surface.",
            )
        if not authority_view.actor_is_teammate:
            return self._task_surface_authority_decision(
                TaskSurfaceAuthorityVerdict.LOCAL_ALLOW,
                "Higher-authority actor may write protected task-surface fields locally.",
                protected_field_names=protected_field_names,
            )
        return self._task_surface_authority_decision(
            TaskSurfaceAuthorityVerdict.ESCALATION_NEEDED,
            "Protected task-surface fields require higher authority to mutate.",
            protected_field_names=protected_field_names,
        )

    async def submit_task(
        self,
        *,
        group_id: str,
        team_id: str,
        goal: str,
        scope: TaskScope = TaskScope.TEAM,
        lane_id: str | None = None,
        owned_paths: tuple[str, ...] = (),
        handoff_to: tuple[str, ...] = (),
        created_by: str | None = None,
        derived_from: str | None = None,
        reason: str = "",
        verification_commands: tuple[str, ...] = (),
        slice_id: str | None = None,
        slice_mode: str | None = None,
        depends_on_slice_ids: tuple[str, ...] = (),
        depends_on_task_ids: tuple[str, ...] = (),
        parallel_group: str | None = None,
    ) -> TaskCard:
        task = TaskCard(
            task_id=make_task_id(),
            goal=goal,
            lane=lane_id or team_id,
            group_id=group_id,
            team_id=team_id,
            scope=scope,
            owned_paths=owned_paths,
            handoff_to=handoff_to,
            owner_id=None,
            created_by=created_by,
            derived_from=derived_from,
            reason=reason,
            verification_commands=verification_commands,
            slice_id=slice_id,
            slice_mode=slice_mode,
            depends_on_slice_ids=depends_on_slice_ids,
            depends_on_task_ids=depends_on_task_ids,
            parallel_group=parallel_group,
            status=TaskStatus.PENDING,
        )
        await self._validate_task_submission_authority(task)
        await self.store.save_task(task)
        await self.bus.publish(OrchestraEvent.task_submitted(group_id, team_id, task.task_id))
        return task

    async def _lane_task_slice_metadata(
        self,
        *,
        group_id: str,
        lane_id: str | None,
    ) -> dict[str, dict[str, object]]:
        if not lane_id:
            return {}
        blackboard_id = _build_blackboard_id(
            group_id=group_id,
            kind=BlackboardKind.LEADER_LANE,
            lane_id=lane_id,
        )
        entries = await self.store.list_blackboard_entries(blackboard_id)
        task_slice_metadata: dict[str, dict[str, object]] = {}
        for entry in entries:
            payload = entry.payload if isinstance(entry.payload, Mapping) else {}
            raw_metadata = payload.get("task_slice_metadata")
            if not isinstance(raw_metadata, Mapping):
                continue
            for task_id, raw_contract in raw_metadata.items():
                if not isinstance(task_id, str) or not task_id.strip():
                    continue
                if not isinstance(raw_contract, Mapping):
                    continue
                task_slice_metadata[task_id.strip()] = {
                    "slice_id": (
                        str(raw_contract["slice_id"])
                        if raw_contract.get("slice_id") is not None
                        else None
                    ),
                    "slice_mode": (
                        str(raw_contract["slice_mode"])
                        if raw_contract.get("slice_mode") is not None
                        else None
                    ),
                    "depends_on_slice_ids": _string_tuple(
                        raw_contract.get("depends_on_slice_ids", ())
                    ),
                    "depends_on_task_ids": _string_tuple(
                        raw_contract.get("depends_on_task_ids", ())
                    ),
                    "parallel_group": (
                        str(raw_contract["parallel_group"])
                        if raw_contract.get("parallel_group") is not None
                        else None
                    ),
                }
        return task_slice_metadata

    async def _hydrate_task_activation_contract(
        self,
        task: TaskCard,
        *,
        lane_task_slice_metadata: Mapping[str, Mapping[str, object]] | None = None,
    ) -> TaskCard:
        if task.group_id is None:
            return task
        if lane_task_slice_metadata is None:
            lane_task_slice_metadata = await self._lane_task_slice_metadata(
                group_id=task.group_id,
                lane_id=task.lane,
            )
        raw_contract = lane_task_slice_metadata.get(task.task_id)
        if not isinstance(raw_contract, Mapping):
            return task
        hydrated = replace(
            task,
            slice_id=task.slice_id or (
                str(raw_contract["slice_id"])
                if raw_contract.get("slice_id") is not None
                else None
            ),
            slice_mode=task.slice_mode or (
                str(raw_contract["slice_mode"])
                if raw_contract.get("slice_mode") is not None
                else None
            ),
            depends_on_slice_ids=(
                task.depends_on_slice_ids
                or _string_tuple(raw_contract.get("depends_on_slice_ids", ()))
            ),
            depends_on_task_ids=(
                task.depends_on_task_ids
                or _string_tuple(raw_contract.get("depends_on_task_ids", ()))
            ),
            parallel_group=task.parallel_group or (
                str(raw_contract["parallel_group"])
                if raw_contract.get("parallel_group") is not None
                else None
            ),
        )
        if _task_has_activation_contract(task):
            return task if task == hydrated else hydrated
        return hydrated

    async def _hydrate_task_activation_contracts(
        self,
        tasks: list[TaskCard],
    ) -> list[TaskCard]:
        metadata_cache: dict[tuple[str, str], dict[str, dict[str, object]]] = {}
        hydrated_tasks: list[TaskCard] = []
        for task in tasks:
            if task.group_id is None or not task.lane:
                hydrated_tasks.append(task)
                continue
            cache_key = (task.group_id, task.lane)
            if cache_key not in metadata_cache:
                metadata_cache[cache_key] = await self._lane_task_slice_metadata(
                    group_id=task.group_id,
                    lane_id=task.lane,
                )
            hydrated_tasks.append(
                await self._hydrate_task_activation_contract(
                    task,
                    lane_task_slice_metadata=metadata_cache[cache_key],
                )
            )
        return hydrated_tasks

    async def _activation_dependency_blockers(
        self,
        task: TaskCard,
        *,
        tasks_by_id: Mapping[str, TaskCard] | None = None,
        lane_task_slice_metadata: Mapping[str, Mapping[str, object]] | None = None,
    ) -> tuple[str, ...]:
        hydrated_task = await self._hydrate_task_activation_contract(
            task,
            lane_task_slice_metadata=lane_task_slice_metadata,
        )
        if not hydrated_task.depends_on_task_ids:
            return ()
        blockers: list[str] = []
        for dependency_task_id in hydrated_task.depends_on_task_ids:
            dependency_task = (
                tasks_by_id.get(dependency_task_id)
                if tasks_by_id is not None
                else None
            )
            if dependency_task is None:
                dependency_task = await self.store.get_task(dependency_task_id)
            if dependency_task is None or dependency_task.status != TaskStatus.COMPLETED:
                blockers.append(dependency_task_id)
        return tuple(blockers)

    async def update_task_status(
        self,
        *,
        task_id: str,
        status: TaskStatus,
        actor_id: str | None = None,
        blocked_by: tuple[str, ...] = (),
    ) -> TaskCard:
        task = await self.store.get_task(task_id)
        if task is None:
            raise ValueError(f"Unknown task_id: {task_id}")
        task = await self._hydrate_task_activation_contract(task)
        await self._validate_task_status_update_authority(
            task=task,
            status=status,
            actor_id=actor_id,
        )
        if (
            status == TaskStatus.IN_PROGRESS
            and actor_id is not None
            and task.owner_id is None
            and task.status == TaskStatus.PENDING
            and not task.blocked_by
        ):
            claimed = await self.claim_task(
                task_id=task_id,
                owner_id=actor_id,
                claim_source="runtime.update_task_status",
            )
            if claimed is not None:
                return claimed
            task = await self.store.get_task(task_id)
            if task is None:
                raise ValueError(f"Unknown task_id: {task_id}")
            task = await self._hydrate_task_activation_contract(task)
            if task.status == TaskStatus.PENDING and task.owner_id is None:
                blockers = await self._activation_dependency_blockers(task)
                if blockers:
                    raise ValueError(
                        f"Task {task_id} has unmet activation dependencies: {', '.join(blockers)}"
                    )
        task.status = status
        if status == TaskStatus.IN_PROGRESS and task.owner_id is None and actor_id is not None:
            task.owner_id = actor_id
            task.claim_source = "runtime.update_task_status"
            task.claimed_at = _utc_now_iso()
            task.claim_session_id = self._default_claim_session_id(
                owner_id=actor_id,
                task_id=task.task_id,
                claim_source="runtime.update_task_status",
                claimed_at=task.claimed_at,
            )
        if status == TaskStatus.BLOCKED:
            task.blocked_by = blocked_by
        else:
            task.blocked_by = ()
        if status != TaskStatus.WAITING_FOR_AUTHORITY:
            task.authority_request_id = None
        await self.store.save_task(task)
        return task

    async def claim_task(
        self,
        *,
        task_id: str,
        owner_id: str,
        claim_source: str,
        claim_session_id: str | None = None,
        claimed_at: str | None = None,
    ) -> TaskCard | None:
        existing = await self.store.get_task(task_id)
        if existing is None:
            raise ValueError(f"Unknown task_id: {task_id}")
        existing = await self._hydrate_task_activation_contract(existing)
        if (
            existing.status == TaskStatus.PENDING
            and existing.owner_id is None
            and (
                existing.blocked_by
                or await self._activation_dependency_blockers(existing)
            )
        ):
            return None
        effective_claimed_at = claimed_at or _utc_now_iso()
        effective_claim_session_id = claim_session_id or self._default_claim_session_id(
            owner_id=owner_id,
            task_id=task_id,
            claim_source=claim_source,
            claimed_at=effective_claimed_at,
        )
        claimed = await self.store.claim_task(
            task_id=task_id,
            owner_id=owner_id,
            claim_session_id=effective_claim_session_id,
            claimed_at=effective_claimed_at,
            claim_source=claim_source,
        )
        if claimed is None:
            return None
        return await self._hydrate_task_activation_contract(claimed)

    async def claim_next_task(
        self,
        *,
        group_id: str,
        owner_id: str,
        claim_source: str,
        team_id: str | None = None,
        lane_id: str | None = None,
        scope: TaskScope | None = None,
        claim_session_id: str | None = None,
        claimed_at: str | None = None,
    ) -> TaskCard | None:
        effective_claimed_at = claimed_at or _utc_now_iso()
        effective_claim_session_id = claim_session_id or self._default_claim_session_id(
            owner_id=owner_id,
            task_id=None,
            claim_source=claim_source,
            claimed_at=effective_claimed_at,
            group_id=group_id,
            team_id=team_id,
            lane_id=lane_id,
        )
        candidates = await self.store.list_tasks(
            group_id,
            team_id=team_id,
            lane_id=lane_id,
            scope=scope.value if scope is not None else None,
        )
        hydrated_candidates = await self._hydrate_task_activation_contracts(candidates)
        tasks_by_id = {
            task.task_id: task
            for task in hydrated_candidates
        }
        for candidate in sorted(hydrated_candidates, key=lambda item: item.task_id):
            if (
                candidate.status != TaskStatus.PENDING
                or candidate.owner_id is not None
                or candidate.blocked_by
            ):
                continue
            blockers = await self._activation_dependency_blockers(
                candidate,
                tasks_by_id=tasks_by_id,
            )
            if blockers:
                continue
            claimed = await self.store.claim_task(
                task_id=candidate.task_id,
                owner_id=owner_id,
                claim_session_id=effective_claim_session_id,
                claimed_at=effective_claimed_at,
                claim_source=claim_source,
            )
            if claimed is None:
                continue
            return await self._hydrate_task_activation_contract(claimed)
        return None

    async def upsert_task_review(
        self,
        *,
        task_id: str,
        reviewer_agent_id: str,
        actor_id: str | None = None,
        reviewer_role: str,
        based_on_task_version: int,
        based_on_knowledge_epoch: int,
        stance: TaskReviewStance,
        summary: str,
        relation_to_my_work: str,
        confidence: float | None,
        experience_context: TaskReviewExperienceContext | None = None,
        reviewed_at: str | None = None,
        metadata: dict[str, object] | None = None,
    ) -> TaskReviewSlot:
        task = await self.store.get_task(task_id)
        if task is None:
            raise ValueError(f"Unknown task_id: {task_id}")
        effective_actor_id = actor_id or reviewer_agent_id
        if effective_actor_id != reviewer_agent_id:
            raise PermissionError(
                f"Actor {effective_actor_id} cannot update review slot owned by {reviewer_agent_id}."
            )
        existing_slots = await self.store.list_task_review_slots(task_id)
        existing_slot = next(
            (slot for slot in existing_slots if slot.reviewer_agent_id == reviewer_agent_id),
            None,
        )
        effective_reviewed_at = reviewed_at or _utc_now_iso()
        slot_id = make_task_review_slot_id(
            task_id=task_id,
            reviewer_agent_id=reviewer_agent_id,
        )
        revision = TaskReviewRevision(
            revision_id=make_task_review_revision_id(),
            slot_id=slot_id,
            task_id=task_id,
            reviewer_agent_id=reviewer_agent_id,
            reviewer_role=reviewer_role,
            created_at=effective_reviewed_at,
            replaces_revision_id=(
                existing_slot.latest_revision_id if existing_slot is not None else None
            ),
            based_on_task_version=based_on_task_version,
            based_on_knowledge_epoch=based_on_knowledge_epoch,
            stance=stance,
            summary=summary,
            relation_to_my_work=relation_to_my_work,
            confidence=confidence,
            experience_context=experience_context or TaskReviewExperienceContext(),
            metadata=dict(metadata or {}),
        )
        slot = TaskReviewSlot.from_revision(revision)
        await self.store.upsert_task_review_slot(slot, revision)
        return slot

    async def list_task_reviews(self, task_id: str) -> tuple[TaskReviewSlot, ...]:
        task = await self.store.get_task(task_id)
        if task is None:
            raise ValueError(f"Unknown task_id: {task_id}")
        slots = await self.store.list_task_review_slots(task_id)
        return tuple(sorted(slots, key=lambda item: item.reviewer_agent_id))

    async def get_task_claim_context(self, task_id: str) -> TaskClaimContext:
        task = await self.store.get_task(task_id)
        if task is None:
            raise ValueError(f"Unknown task_id: {task_id}")
        hydrated_task = await self._hydrate_task_activation_contract(task)
        review_slots = await self.list_task_reviews(task_id)
        review_digest = build_task_review_digest(task_id, review_slots)
        return TaskClaimContext(
            task=hydrated_task,
            review_slots=review_slots,
            review_digest=review_digest,
        )

    @staticmethod
    def _effective_hierarchical_review_actor(
        actor: HierarchicalReviewActor | None,
    ) -> HierarchicalReviewActor:
        return actor or HierarchicalReviewActor.system()

    def _require_hierarchical_review_access(
        self,
        *,
        allowed: bool,
        actor: HierarchicalReviewActor,
        action: str,
        layer: str,
        reason: str = "",
    ) -> None:
        if allowed:
            return
        actor_label = actor.actor_id or actor.role.value
        detail = reason or f"{actor_label} cannot {action} {layer}."
        raise PermissionError(detail)

    async def _get_review_item_or_raise(self, item_id: str) -> ReviewItemRef:
        item = await self.store.get_review_item(item_id)
        if item is None:
            raise ValueError(f"Unknown review item: {item_id}")
        return item

    def _redact_review_item(
        self,
        item: ReviewItemRef,
        *,
        read_mode: HierarchicalReviewReadMode,
    ) -> ReviewItemRef:
        if read_mode == HierarchicalReviewReadMode.FULL_TEXT:
            return item
        if read_mode == HierarchicalReviewReadMode.SUMMARY_PLUS_REF:
            return replace(item, metadata={})
        return replace(item, source_task_id=None, metadata={})

    def _redact_team_position_review(
        self,
        review: TeamPositionReview,
        *,
        read_mode: HierarchicalReviewReadMode,
    ) -> TeamPositionReview:
        if read_mode == HierarchicalReviewReadMode.FULL_TEXT:
            return review
        if read_mode == HierarchicalReviewReadMode.SUMMARY_PLUS_REF:
            return replace(
                review,
                key_risks=(),
                key_dependencies=(),
                metadata={},
            )
        return replace(
            review,
            based_on_task_review_revision_ids=(),
            key_risks=(),
            key_dependencies=(),
            evidence_refs=(),
            metadata={},
        )

    def _redact_cross_team_leader_review(
        self,
        review: CrossTeamLeaderReview,
        *,
        read_mode: HierarchicalReviewReadMode,
    ) -> CrossTeamLeaderReview:
        if read_mode == HierarchicalReviewReadMode.FULL_TEXT:
            return review
        if read_mode == HierarchicalReviewReadMode.SUMMARY_PLUS_REF:
            return replace(
                review,
                what_changed_in_my_understanding="",
                metadata={},
            )
        return replace(
            review,
            target_position_review_id="",
            what_changed_in_my_understanding="",
            evidence_refs=(),
            metadata={},
        )

    def _redact_superleader_synthesis(
        self,
        synthesis: SuperLeaderSynthesis,
        *,
        read_mode: HierarchicalReviewReadMode,
    ) -> SuperLeaderSynthesis:
        if read_mode == HierarchicalReviewReadMode.FULL_TEXT:
            return synthesis
        if read_mode == HierarchicalReviewReadMode.SUMMARY_PLUS_REF:
            return replace(
                synthesis,
                accepted_risks=(),
                rejected_paths=(),
                open_questions=(),
                metadata={},
            )
        return replace(
            synthesis,
            based_on_team_position_review_ids=(),
            based_on_cross_team_review_ids=(),
            accepted_risks=(),
            rejected_paths=(),
            open_questions=(),
            evidence_refs=(),
            metadata={},
        )

    @staticmethod
    def _digest_visibility_scope_for_item(item: ReviewItemRef) -> str:
        if item.item_kind == ReviewItemKind.PROJECT_ITEM:
            return MailboxVisibilityScope.SHARED.value
        return MailboxVisibilityScope.CONTROL_PRIVATE.value

    @staticmethod
    def _cap_digest_read_mode(read_mode: HierarchicalReviewReadMode) -> HierarchicalReviewReadMode:
        if read_mode == HierarchicalReviewReadMode.NONE:
            return HierarchicalReviewReadMode.NONE
        if read_mode == HierarchicalReviewReadMode.SUMMARY_ONLY:
            return HierarchicalReviewReadMode.SUMMARY_ONLY
        return HierarchicalReviewReadMode.SUMMARY_PLUS_REF

    @staticmethod
    def _review_read_mode_rank(read_mode: HierarchicalReviewReadMode) -> int:
        return {
            HierarchicalReviewReadMode.NONE: 0,
            HierarchicalReviewReadMode.SUMMARY_ONLY: 1,
            HierarchicalReviewReadMode.SUMMARY_PLUS_REF: 2,
            HierarchicalReviewReadMode.FULL_TEXT: 3,
        }[read_mode]

    def _min_review_read_mode(
        self,
        left: HierarchicalReviewReadMode,
        right: HierarchicalReviewReadMode,
    ) -> HierarchicalReviewReadMode:
        if self._review_read_mode_rank(left) <= self._review_read_mode_rank(right):
            return left
        return right

    def _build_review_digest_visibility(
        self,
        *,
        item: ReviewItemRef,
        read_mode: HierarchicalReviewReadMode,
    ) -> HierarchicalReviewDigestVisibility:
        digest_read_mode = self._cap_digest_read_mode(read_mode)
        return HierarchicalReviewDigestVisibility(
            visibility_scope=self._digest_visibility_scope_for_item(item),
            read_mode=digest_read_mode,
            ref_visible=digest_read_mode == HierarchicalReviewReadMode.SUMMARY_PLUS_REF,
        )

    async def create_review_item(
        self,
        *,
        objective_id: str,
        item_id: str | None = None,
        item_kind: ReviewItemKind,
        title: str,
        summary: str,
        lane_id: str | None = None,
        team_id: str | None = None,
        source_task_id: str | None = None,
        metadata: dict[str, object] | None = None,
        actor: HierarchicalReviewActor | None = None,
    ) -> ReviewItemRef:
        effective_actor = self._effective_hierarchical_review_actor(actor)
        objective = await self.store.get_objective(objective_id)
        if objective is None:
            raise ValueError(f"Unknown objective_id: {objective_id}")
        if source_task_id is not None:
            source_task = await self.store.get_task(source_task_id)
            if source_task is None:
                raise ValueError(f"Unknown source_task_id: {source_task_id}")
        decision = self.hierarchical_review_policy.create_review_item_access(
            actor=effective_actor,
            item_kind=item_kind,
            item_team_id=team_id,
        )
        self._require_hierarchical_review_access(
            allowed=decision.allowed,
            actor=effective_actor,
            action="create",
            layer="review items",
            reason=decision.reason,
        )
        item = ReviewItemRef(
            item_id=item_id or make_review_item_id(),
            item_kind=item_kind,
            objective_id=objective_id,
            lane_id=lane_id,
            team_id=team_id,
            source_task_id=source_task_id,
            title=title,
            summary=summary,
            metadata=dict(metadata or {}),
        )
        await self.store.save_review_item(item)
        return item

    async def get_review_item(
        self,
        item_id: str,
        *,
        actor: HierarchicalReviewActor | None = None,
    ) -> ReviewItemRef:
        item = await self._get_review_item_or_raise(item_id)
        effective_actor = self._effective_hierarchical_review_actor(actor)
        decision = self.hierarchical_review_policy.review_item_read_access(
            actor=effective_actor,
            item=item,
        )
        self._require_hierarchical_review_access(
            allowed=decision.allowed,
            actor=effective_actor,
            action="read",
            layer="review items",
            reason=decision.reason,
        )
        return self._redact_review_item(item, read_mode=decision.read_mode)

    async def list_review_items(
        self,
        objective_id: str,
        *,
        item_kind: ReviewItemKind | None = None,
        actor: HierarchicalReviewActor | None = None,
    ) -> tuple[ReviewItemRef, ...]:
        effective_actor = self._effective_hierarchical_review_actor(actor)
        objective = await self.store.get_objective(objective_id)
        if objective is None:
            raise ValueError(f"Unknown objective_id: {objective_id}")
        items = await self.store.list_review_items(objective_id, item_kind=item_kind)
        visible: list[ReviewItemRef] = []
        for item in items:
            decision = self.hierarchical_review_policy.review_item_read_access(
                actor=effective_actor,
                item=item,
            )
            if not decision.allowed:
                continue
            visible.append(self._redact_review_item(item, read_mode=decision.read_mode))
        return tuple(visible)

    async def authorize_review_digest_view(
        self,
        item_id: str,
        *,
        actor: HierarchicalReviewActor | None = None,
    ) -> HierarchicalReviewDigestVisibility:
        item = await self._get_review_item_or_raise(item_id)
        effective_actor = self._effective_hierarchical_review_actor(actor)
        decision = self.hierarchical_review_policy.review_item_read_access(
            actor=effective_actor,
            item=item,
        )
        self._require_hierarchical_review_access(
            allowed=decision.allowed,
            actor=effective_actor,
            action="read",
            layer="review digests",
            reason=decision.reason,
        )
        return self._build_review_digest_visibility(item=item, read_mode=decision.read_mode)

    async def materialize_review_digest_view(
        self,
        item_id: str,
        *,
        actor: HierarchicalReviewActor | None = None,
    ) -> HierarchicalReviewDigestView:
        item = await self._get_review_item_or_raise(item_id)
        effective_actor = self._effective_hierarchical_review_actor(actor)
        visibility = await self.authorize_review_digest_view(item_id, actor=effective_actor)
        team_position_reviews = tuple(await self.store.list_team_position_reviews(item_id))
        cross_team_leader_reviews = tuple(await self.store.list_cross_team_leader_reviews(item_id))
        superleader_synthesis = await self.store.get_superleader_synthesis(item_id)
        snapshot = build_hierarchical_review_digest_snapshot(
            item,
            team_position_reviews=team_position_reviews,
            cross_team_leader_reviews=cross_team_leader_reviews,
            superleader_synthesis=superleader_synthesis,
        )

        team_position_digests: list[TeamPositionReviewDigest] = []
        for review in team_position_reviews:
            decision = self.hierarchical_review_policy.team_position_read_access(
                actor=effective_actor,
                review=review,
            )
            if not decision.allowed:
                continue
            digest_visibility = self._build_review_digest_visibility(
                item=item,
                read_mode=self._min_review_read_mode(decision.read_mode, visibility.read_mode),
            )
            team_position_digests.append(
                build_team_position_review_digest(
                    item=item,
                    review=review,
                    snapshot=snapshot,
                    team_position_reviews=team_position_reviews,
                    visibility=digest_visibility,
                )
            )

        cross_team_digests: list[CrossTeamLeaderReviewDigest] = []
        for review in cross_team_leader_reviews:
            decision = self.hierarchical_review_policy.cross_team_leader_read_access(
                actor=effective_actor,
                review=review,
            )
            if not decision.allowed:
                continue
            digest_visibility = self._build_review_digest_visibility(
                item=item,
                read_mode=self._min_review_read_mode(decision.read_mode, visibility.read_mode),
            )
            cross_team_digests.append(
                build_cross_team_leader_review_digest(
                    item=item,
                    review=review,
                    snapshot=snapshot,
                    cross_team_leader_reviews=cross_team_leader_reviews,
                    visibility=digest_visibility,
                )
            )

        synthesis_digest: SuperLeaderSynthesisDigest | None = None
        if superleader_synthesis is not None:
            decision = self.hierarchical_review_policy.superleader_synthesis_read_access(
                actor=effective_actor,
                synthesis=superleader_synthesis,
            )
            if decision.allowed:
                digest_visibility = self._build_review_digest_visibility(
                    item=item,
                    read_mode=self._min_review_read_mode(decision.read_mode, visibility.read_mode),
                )
                synthesis_digest = build_superleader_synthesis_digest(
                    item=item,
                    synthesis=superleader_synthesis,
                    snapshot=snapshot,
                    visibility=digest_visibility,
                )

        return HierarchicalReviewDigestView(
            item=self._redact_review_item(item, read_mode=visibility.read_mode),
            snapshot=snapshot,
            visibility=visibility,
            team_position_digests=tuple(team_position_digests),
            cross_team_leader_digests=tuple(cross_team_digests),
            superleader_synthesis_digest=synthesis_digest,
        )

    async def publish_team_position_review(
        self,
        *,
        item_id: str,
        team_id: str,
        leader_id: str,
        reviewed_at: str | None = None,
        position_review_id: str | None = None,
        based_on_task_review_revision_ids: tuple[str, ...] = (),
        team_stance: str = "",
        summary: str = "",
        key_risks: tuple[str, ...] = (),
        key_dependencies: tuple[str, ...] = (),
        recommended_next_action: str = "",
        confidence: float | None = None,
        evidence_refs: tuple[str, ...] = (),
        metadata: dict[str, object] | None = None,
        actor: HierarchicalReviewActor | None = None,
    ) -> TeamPositionReview:
        effective_actor = self._effective_hierarchical_review_actor(actor)
        decision = self.hierarchical_review_policy.team_position_write_access(
            actor=effective_actor,
            team_id=team_id,
        )
        self._require_hierarchical_review_access(
            allowed=decision.allowed,
            actor=effective_actor,
            action="publish",
            layer="team position reviews",
            reason=decision.reason,
        )
        if (
            effective_actor.role != HierarchicalReviewActorRole.SYSTEM
            and effective_actor.actor_id != leader_id
        ):
            raise PermissionError(
                f"Actor {effective_actor.actor_id} cannot publish a team position review as {leader_id}."
            )
        item = await self._get_review_item_or_raise(item_id)
        if item.team_id is not None and item.team_id != team_id:
            raise ValueError(
                f"Review item {item.item_id} is scoped to team {item.team_id}, not {team_id}."
            )
        if item.source_task_id is not None:
            source_task = await self.store.get_task(item.source_task_id)
            if source_task is not None and source_task.team_id not in (None, team_id):
                raise ValueError(
                    f"Review item {item.item_id} source task belongs to team {source_task.team_id}, not {team_id}."
                )
        review = TeamPositionReview(
            position_review_id=position_review_id or make_team_position_review_id(),
            item_id=item.item_id,
            item_kind=item.item_kind,
            team_id=team_id,
            leader_id=leader_id,
            reviewed_at=reviewed_at or _utc_now_iso(),
            based_on_task_review_revision_ids=based_on_task_review_revision_ids,
            team_stance=team_stance,
            summary=summary,
            key_risks=key_risks,
            key_dependencies=key_dependencies,
            recommended_next_action=recommended_next_action,
            confidence=confidence,
            evidence_refs=evidence_refs,
            metadata=dict(metadata or {}),
        )
        await self.store.save_team_position_review(review)
        return review

    async def list_team_position_reviews(
        self,
        item_id: str,
        *,
        team_id: str | None = None,
        actor: HierarchicalReviewActor | None = None,
    ) -> tuple[TeamPositionReview, ...]:
        item = await self._get_review_item_or_raise(item_id)
        effective_actor = self._effective_hierarchical_review_actor(actor)
        item_decision = self.hierarchical_review_policy.review_item_read_access(
            actor=effective_actor,
            item=item,
        )
        self._require_hierarchical_review_access(
            allowed=item_decision.allowed,
            actor=effective_actor,
            action="list",
            layer="team position reviews",
            reason=item_decision.reason,
        )
        reviews = await self.store.list_team_position_reviews(item_id, team_id=team_id)
        visible: list[TeamPositionReview] = []
        for review in reviews:
            decision = self.hierarchical_review_policy.team_position_read_access(
                actor=effective_actor,
                review=review,
            )
            if not decision.allowed:
                continue
            visible.append(self._redact_team_position_review(review, read_mode=decision.read_mode))
        return tuple(visible)

    async def publish_cross_team_leader_review(
        self,
        *,
        item_id: str,
        reviewer_team_id: str,
        reviewer_leader_id: str,
        target_team_id: str,
        target_position_review_id: str,
        reviewed_at: str | None = None,
        cross_review_id: str | None = None,
        stance: str = "",
        agreement_level: str = "",
        what_changed_in_my_understanding: str = "",
        challenge_or_support: str = "",
        suggested_adjustment: str = "",
        confidence: float | None = None,
        evidence_refs: tuple[str, ...] = (),
        metadata: dict[str, object] | None = None,
        actor: HierarchicalReviewActor | None = None,
    ) -> CrossTeamLeaderReview:
        effective_actor = self._effective_hierarchical_review_actor(actor)
        decision = self.hierarchical_review_policy.cross_team_leader_write_access(
            actor=effective_actor,
            reviewer_team_id=reviewer_team_id,
            target_team_id=target_team_id,
        )
        self._require_hierarchical_review_access(
            allowed=decision.allowed,
            actor=effective_actor,
            action="publish",
            layer="cross-team leader reviews",
            reason=decision.reason,
        )
        if (
            effective_actor.role != HierarchicalReviewActorRole.SYSTEM
            and effective_actor.actor_id != reviewer_leader_id
        ):
            raise PermissionError(
                f"Actor {effective_actor.actor_id} cannot publish a cross-team leader review as {reviewer_leader_id}."
            )
        item = await self._get_review_item_or_raise(item_id)
        if item.item_kind != ReviewItemKind.PROJECT_ITEM:
            raise ValueError(
                f"Cross-team leader reviews are only allowed for project items, got {item.item_kind.value}."
            )
        target_review = next(
            (
                review
                for review in await self.store.list_team_position_reviews(item.item_id)
                if review.position_review_id == target_position_review_id
            ),
            None,
        )
        if target_review is None:
            raise ValueError(f"Unknown target_position_review_id: {target_position_review_id}")
        if target_review.team_id != target_team_id:
            raise ValueError(
                f"Target position review {target_position_review_id} belongs to team {target_review.team_id}, not {target_team_id}."
            )
        review = CrossTeamLeaderReview(
            cross_review_id=cross_review_id or make_cross_team_leader_review_id(),
            item_id=item.item_id,
            item_kind=item.item_kind,
            reviewer_team_id=reviewer_team_id,
            reviewer_leader_id=reviewer_leader_id,
            target_team_id=target_team_id,
            target_position_review_id=target_position_review_id,
            reviewed_at=reviewed_at or _utc_now_iso(),
            stance=stance,
            agreement_level=agreement_level,
            what_changed_in_my_understanding=what_changed_in_my_understanding,
            challenge_or_support=challenge_or_support,
            suggested_adjustment=suggested_adjustment,
            confidence=confidence,
            evidence_refs=evidence_refs,
            metadata=dict(metadata or {}),
        )
        await self.store.save_cross_team_leader_review(review)
        return review

    async def list_cross_team_leader_reviews(
        self,
        item_id: str,
        *,
        reviewer_team_id: str | None = None,
        target_team_id: str | None = None,
        actor: HierarchicalReviewActor | None = None,
    ) -> tuple[CrossTeamLeaderReview, ...]:
        item = await self._get_review_item_or_raise(item_id)
        effective_actor = self._effective_hierarchical_review_actor(actor)
        item_decision = self.hierarchical_review_policy.review_item_read_access(
            actor=effective_actor,
            item=item,
        )
        self._require_hierarchical_review_access(
            allowed=item_decision.allowed,
            actor=effective_actor,
            action="list",
            layer="cross-team leader reviews",
            reason=item_decision.reason,
        )
        reviews = await self.store.list_cross_team_leader_reviews(
            item_id,
            reviewer_team_id=reviewer_team_id,
            target_team_id=target_team_id,
        )
        visible: list[CrossTeamLeaderReview] = []
        for review in reviews:
            decision = self.hierarchical_review_policy.cross_team_leader_read_access(
                actor=effective_actor,
                review=review,
            )
            if not decision.allowed:
                continue
            visible.append(
                self._redact_cross_team_leader_review(review, read_mode=decision.read_mode)
            )
        return tuple(visible)

    async def publish_superleader_synthesis(
        self,
        *,
        item_id: str,
        superleader_id: str,
        synthesized_at: str | None = None,
        synthesis_id: str | None = None,
        based_on_team_position_review_ids: tuple[str, ...] = (),
        based_on_cross_team_review_ids: tuple[str, ...] = (),
        final_position: str = "",
        accepted_risks: tuple[str, ...] = (),
        rejected_paths: tuple[str, ...] = (),
        open_questions: tuple[str, ...] = (),
        next_actions: tuple[str, ...] = (),
        confidence: float | None = None,
        evidence_refs: tuple[str, ...] = (),
        metadata: dict[str, object] | None = None,
        actor: HierarchicalReviewActor | None = None,
    ) -> SuperLeaderSynthesis:
        effective_actor = self._effective_hierarchical_review_actor(actor)
        decision = self.hierarchical_review_policy.superleader_synthesis_write_access(
            actor=effective_actor,
        )
        self._require_hierarchical_review_access(
            allowed=decision.allowed,
            actor=effective_actor,
            action="publish",
            layer="superleader synthesis",
            reason=decision.reason,
        )
        if (
            effective_actor.role != HierarchicalReviewActorRole.SYSTEM
            and effective_actor.actor_id != superleader_id
        ):
            raise PermissionError(
                f"Actor {effective_actor.actor_id} cannot publish superleader synthesis as {superleader_id}."
            )
        item = await self._get_review_item_or_raise(item_id)
        synthesis = SuperLeaderSynthesis(
            synthesis_id=synthesis_id or make_superleader_synthesis_id(),
            item_id=item.item_id,
            item_kind=item.item_kind,
            superleader_id=superleader_id,
            synthesized_at=synthesized_at or _utc_now_iso(),
            based_on_team_position_review_ids=based_on_team_position_review_ids,
            based_on_cross_team_review_ids=based_on_cross_team_review_ids,
            final_position=final_position,
            accepted_risks=accepted_risks,
            rejected_paths=rejected_paths,
            open_questions=open_questions,
            next_actions=next_actions,
            confidence=confidence,
            evidence_refs=evidence_refs,
            metadata=dict(metadata or {}),
        )
        await self.store.save_superleader_synthesis(synthesis)
        return synthesis

    async def get_superleader_synthesis(
        self,
        item_id: str,
        *,
        actor: HierarchicalReviewActor | None = None,
    ) -> SuperLeaderSynthesis | None:
        item = await self._get_review_item_or_raise(item_id)
        effective_actor = self._effective_hierarchical_review_actor(actor)
        item_decision = self.hierarchical_review_policy.review_item_read_access(
            actor=effective_actor,
            item=item,
        )
        self._require_hierarchical_review_access(
            allowed=item_decision.allowed,
            actor=effective_actor,
            action="read",
            layer="superleader synthesis",
            reason=item_decision.reason,
        )
        synthesis = await self.store.get_superleader_synthesis(item_id)
        if synthesis is None:
            return None
        decision = self.hierarchical_review_policy.superleader_synthesis_read_access(
            actor=effective_actor,
            synthesis=synthesis,
        )
        if not decision.allowed:
            return None
        return self._redact_superleader_synthesis(synthesis, read_mode=decision.read_mode)

    async def get_project_item_review_context(
        self,
        item_id: str,
        *,
        actor: HierarchicalReviewActor | None = None,
    ) -> dict[str, object]:
        item = await self._get_review_item_or_raise(item_id)
        if item.item_kind != ReviewItemKind.PROJECT_ITEM:
            raise ValueError(f"item_id {item_id} is not a project item")
        effective_actor = self._effective_hierarchical_review_actor(actor)
        item_decision = self.hierarchical_review_policy.review_item_read_access(
            actor=effective_actor,
            item=item,
        )
        self._require_hierarchical_review_access(
            allowed=item_decision.allowed,
            actor=effective_actor,
            action="read",
            layer="project review context",
            reason=item_decision.reason,
        )
        team_position_reviews = await self.list_team_position_reviews(item_id, actor=effective_actor)
        cross_team_leader_reviews = await self.list_cross_team_leader_reviews(
            item_id,
            actor=effective_actor,
        )
        superleader_synthesis = await self.get_superleader_synthesis(
            item_id,
            actor=effective_actor,
        )
        return {
            "item": self._redact_review_item(item, read_mode=item_decision.read_mode),
            "team_position_reviews": team_position_reviews,
            "cross_team_leader_reviews": cross_team_leader_reviews,
            "superleader_synthesis": superleader_synthesis,
        }

    async def _list_planning_review_records(
        self,
        *,
        list_method_name: str,
        objective_id: str,
        planning_round_id: str | None = None,
    ) -> tuple[object, ...]:
        list_method = getattr(self.store, list_method_name, None)
        if not callable(list_method):
            raise NotImplementedError(
                f"Store does not implement `{list_method_name}` required by planning review runtime APIs."
            )
        if planning_round_id is None:
            return tuple(await list_method(objective_id))
        try:
            return tuple(
                await list_method(
                    objective_id,
                    planning_round_id=planning_round_id,
                )
            )
        except TypeError:
            return tuple(await list_method(objective_id))

    async def publish_leader_draft_plan(
        self,
        *,
        objective_id: str,
        planning_round_id: str,
        leader_id: str,
        lane_id: str,
        team_id: str,
        summary: str,
        sequential_slices: tuple[object, ...] = (),
        parallel_slices: tuple[object, ...] = (),
        project_items: tuple[str, ...] = (),
        shared_hotspots: tuple[str, ...] = (),
        verification_targets: tuple[str, ...] = (),
        authority_risks: tuple[str, ...] = (),
        metadata: dict[str, object] | None = None,
    ) -> "LeaderDraftPlan":
        objective = await self.store.get_objective(objective_id)
        if objective is None:
            raise ValueError(f"Unknown objective_id: {objective_id}")
        contracts = _planning_review_contracts()
        payload = {
            "objective_id": objective_id,
            "planning_round_id": planning_round_id,
            "leader_id": leader_id,
            "lane_id": lane_id,
            "team_id": team_id,
            "summary": summary,
            "sequential_slices": list(_planning_payloads(sequential_slices)),
            "parallel_slices": list(_planning_payloads(parallel_slices)),
            "project_items": list(project_items),
            "shared_hotspots": list(shared_hotspots),
            "verification_targets": list(verification_targets),
            "authority_risks": list(authority_risks),
            "metadata": dict(metadata or {}),
        }
        plan = _coerce_planning_contract_instance(contracts.LeaderDraftPlan, payload)
        save_method = getattr(self.store, "save_leader_draft_plan", None)
        if not callable(save_method):
            raise NotImplementedError(
                "Store does not implement `save_leader_draft_plan` required by planning review runtime APIs."
            )
        await save_method(plan)
        return plan  # type: ignore[return-value]

    async def list_leader_draft_plans(
        self,
        objective_id: str,
        *,
        planning_round_id: str | None = None,
        leader_id: str | None = None,
        lane_id: str | None = None,
        team_id: str | None = None,
    ) -> tuple["LeaderDraftPlan", ...]:
        records = await self._list_planning_review_records(
            list_method_name="list_leader_draft_plans",
            objective_id=objective_id,
            planning_round_id=planning_round_id,
        )
        filtered: list[object] = []
        for record in records:
            if planning_round_id is not None and getattr(record, "planning_round_id", None) != planning_round_id:
                continue
            if leader_id is not None and getattr(record, "leader_id", None) != leader_id:
                continue
            if lane_id is not None and getattr(record, "lane_id", None) != lane_id:
                continue
            if team_id is not None and getattr(record, "team_id", None) != team_id:
                continue
            filtered.append(record)
        return tuple(filtered)  # type: ignore[return-value]

    async def publish_leader_peer_review(
        self,
        *,
        objective_id: str,
        planning_round_id: str,
        reviewer_leader_id: str,
        reviewer_team_id: str,
        target_leader_id: str,
        target_team_id: str,
        summary: str,
        conflict_type: str,
        severity: str,
        affected_paths: tuple[str, ...] = (),
        affected_project_items: tuple[str, ...] = (),
        reason: str = "",
        suggested_change: str = "",
        requires_superleader_attention: bool = False,
        metadata: dict[str, object] | None = None,
        review_id: str | None = None,
    ) -> "LeaderPeerReview":
        objective = await self.store.get_objective(objective_id)
        if objective is None:
            raise ValueError(f"Unknown objective_id: {objective_id}")
        if reviewer_leader_id == target_leader_id:
            raise ValueError("leader peer review cannot target the same leader")
        contracts = _planning_review_contracts()
        payload = {
            "objective_id": objective_id,
            "planning_round_id": planning_round_id,
            "review_id": review_id
            or _planning_generated_id(
                contracts,
                "make_leader_peer_review_id",
                prefix="leader-peer-review",
            ),
            "reviewer_leader_id": reviewer_leader_id,
            "reviewer_team_id": reviewer_team_id,
            "target_leader_id": target_leader_id,
            "target_team_id": target_team_id,
            "summary": summary,
            "conflict_type": conflict_type,
            "severity": severity,
            "affected_paths": list(affected_paths),
            "affected_project_items": list(affected_project_items),
            "reason": reason,
            "suggested_change": suggested_change,
            "requires_superleader_attention": bool(requires_superleader_attention),
            "metadata": dict(metadata or {}),
        }
        review = _coerce_planning_contract_instance(contracts.LeaderPeerReview, payload)
        save_method = getattr(self.store, "save_leader_peer_review", None)
        if not callable(save_method):
            raise NotImplementedError(
                "Store does not implement `save_leader_peer_review` required by planning review runtime APIs."
            )
        await save_method(review)
        return review  # type: ignore[return-value]

    async def list_leader_peer_reviews(
        self,
        objective_id: str,
        *,
        planning_round_id: str | None = None,
        reviewer_leader_id: str | None = None,
        reviewer_team_id: str | None = None,
        target_leader_id: str | None = None,
        target_team_id: str | None = None,
    ) -> tuple["LeaderPeerReview", ...]:
        records = await self._list_planning_review_records(
            list_method_name="list_leader_peer_reviews",
            objective_id=objective_id,
            planning_round_id=planning_round_id,
        )
        filtered: list[object] = []
        for record in records:
            if planning_round_id is not None and getattr(record, "planning_round_id", None) != planning_round_id:
                continue
            if reviewer_leader_id is not None and getattr(record, "reviewer_leader_id", None) != reviewer_leader_id:
                continue
            if reviewer_team_id is not None and getattr(record, "reviewer_team_id", None) != reviewer_team_id:
                continue
            if target_leader_id is not None and getattr(record, "target_leader_id", None) != target_leader_id:
                continue
            if target_team_id is not None and getattr(record, "target_team_id", None) != target_team_id:
                continue
            filtered.append(record)
        return tuple(filtered)  # type: ignore[return-value]

    async def publish_superleader_global_review(
        self,
        *,
        objective_id: str,
        planning_round_id: str,
        superleader_id: str,
        summary: str,
        global_conflicts: tuple[str, ...] = (),
        activation_blockers: tuple[str, ...] = (),
        required_reordering: tuple[str, ...] = (),
        required_serialization: tuple[str, ...] = (),
        required_project_item_promotion: tuple[str, ...] = (),
        required_authority_attention: tuple[str, ...] = (),
        recommended_adjustments: tuple[str, ...] = (),
        metadata: dict[str, object] | None = None,
        review_id: str | None = None,
    ) -> "SuperLeaderGlobalReview":
        objective = await self.store.get_objective(objective_id)
        if objective is None:
            raise ValueError(f"Unknown objective_id: {objective_id}")
        contracts = _planning_review_contracts()
        payload = {
            "objective_id": objective_id,
            "planning_round_id": planning_round_id,
            "review_id": review_id
            or _planning_generated_id(
                contracts,
                "make_superleader_global_review_id",
                prefix="superleader-global-review",
            ),
            "superleader_id": superleader_id,
            "summary": summary,
            "global_conflicts": list(global_conflicts),
            "activation_blockers": list(activation_blockers),
            "required_reordering": list(required_reordering),
            "required_serialization": list(required_serialization),
            "required_project_item_promotion": list(required_project_item_promotion),
            "required_authority_attention": list(required_authority_attention),
            "recommended_adjustments": list(recommended_adjustments),
            "metadata": dict(metadata or {}),
        }
        review = _coerce_planning_contract_instance(contracts.SuperLeaderGlobalReview, payload)
        save_method = getattr(self.store, "save_superleader_global_review", None)
        if not callable(save_method):
            raise NotImplementedError(
                "Store does not implement `save_superleader_global_review` required by planning review runtime APIs."
            )
        await save_method(review)
        return review  # type: ignore[return-value]

    async def get_superleader_global_review(
        self,
        objective_id: str,
        *,
        planning_round_id: str | None = None,
    ) -> "SuperLeaderGlobalReview | None":
        get_method = getattr(self.store, "get_superleader_global_review", None)
        if not callable(get_method):
            raise NotImplementedError(
                "Store does not implement `get_superleader_global_review` required by planning review runtime APIs."
            )
        if planning_round_id is None:
            return await get_method(objective_id)  # type: ignore[return-value]
        try:
            return await get_method(
                objective_id,
                planning_round_id=planning_round_id,
            )  # type: ignore[return-value]
        except TypeError:
            review = await get_method(objective_id)
            if review is None:
                return None
            if getattr(review, "planning_round_id", None) != planning_round_id:
                return None
            return review  # type: ignore[return-value]

    async def publish_leader_revised_plan(
        self,
        *,
        objective_id: str,
        planning_round_id: str,
        leader_id: str,
        lane_id: str,
        team_id: str,
        summary: str,
        sequential_slices: tuple[object, ...] = (),
        parallel_slices: tuple[object, ...] = (),
        project_items: tuple[str, ...] = (),
        shared_hotspots: tuple[str, ...] = (),
        verification_targets: tuple[str, ...] = (),
        authority_risks: tuple[str, ...] = (),
        metadata: dict[str, object] | None = None,
        revision_bundle_ref: str | None = None,
    ) -> "LeaderRevisedPlan":
        objective = await self.store.get_objective(objective_id)
        if objective is None:
            raise ValueError(f"Unknown objective_id: {objective_id}")
        contracts = _planning_review_contracts()
        payload = {
            "objective_id": objective_id,
            "planning_round_id": planning_round_id,
            "leader_id": leader_id,
            "lane_id": lane_id,
            "team_id": team_id,
            "summary": summary,
            "sequential_slices": list(_planning_payloads(sequential_slices)),
            "parallel_slices": list(_planning_payloads(parallel_slices)),
            "project_items": list(project_items),
            "shared_hotspots": list(shared_hotspots),
            "verification_targets": list(verification_targets),
            "authority_risks": list(authority_risks),
            "metadata": dict(metadata or {}),
            "revision_bundle_ref": revision_bundle_ref,
        }
        plan = _coerce_planning_contract_instance(contracts.LeaderRevisedPlan, payload)
        save_method = getattr(self.store, "save_leader_revised_plan", None)
        if not callable(save_method):
            raise NotImplementedError(
                "Store does not implement `save_leader_revised_plan` required by planning review runtime APIs."
            )
        await save_method(plan)
        return plan  # type: ignore[return-value]

    async def list_leader_revised_plans(
        self,
        objective_id: str,
        *,
        planning_round_id: str | None = None,
        leader_id: str | None = None,
        lane_id: str | None = None,
        team_id: str | None = None,
    ) -> tuple["LeaderRevisedPlan", ...]:
        records = await self._list_planning_review_records(
            list_method_name="list_leader_revised_plans",
            objective_id=objective_id,
            planning_round_id=planning_round_id,
        )
        filtered: list[object] = []
        for record in records:
            if planning_round_id is not None and getattr(record, "planning_round_id", None) != planning_round_id:
                continue
            if leader_id is not None and getattr(record, "leader_id", None) != leader_id:
                continue
            if lane_id is not None and getattr(record, "lane_id", None) != lane_id:
                continue
            if team_id is not None and getattr(record, "team_id", None) != team_id:
                continue
            filtered.append(record)
        return tuple(filtered)  # type: ignore[return-value]

    async def build_leader_revision_context_bundle(
        self,
        *,
        objective_id: str,
        planning_round_id: str,
        leader_id: str,
    ) -> "LeaderRevisionContextBundle":
        objective = await self.store.get_objective(objective_id)
        if objective is None:
            raise ValueError(f"Unknown objective_id: {objective_id}")
        contracts = _planning_review_contracts()
        build_draft_digest = getattr(contracts, "build_leader_draft_plan_digest", None)
        build_peer_digest = getattr(contracts, "build_leader_peer_review_digest", None)
        build_global_digest = getattr(contracts, "build_superleader_global_review_digest", None)

        draft_plans = await self.list_leader_draft_plans(
            objective_id,
            planning_round_id=planning_round_id,
        )
        peer_reviews = await self.list_leader_peer_reviews(
            objective_id,
            planning_round_id=planning_round_id,
        )
        global_review = await self.get_superleader_global_review(
            objective_id,
            planning_round_id=planning_round_id,
        )
        target_draft_plan = next(
            (
                plan
                for plan in draft_plans
                if getattr(plan, "leader_id", None) == leader_id
            ),
            None,
        )
        target_lane_id = (
            str(getattr(target_draft_plan, "lane_id", "")).strip()
            if target_draft_plan is not None
            else ""
        ) or None
        target_team_id = (
            str(getattr(target_draft_plan, "team_id", "")).strip()
            if target_draft_plan is not None
            else ""
        ) or None
        targeted_peer_reviews = tuple(
            review
            for review in peer_reviews
            if getattr(review, "target_leader_id", None) == leader_id
        )
        draft_plan_refs = tuple(
            build_draft_digest(plan) if callable(build_draft_digest) else plan
            for plan in draft_plans
            if getattr(plan, "leader_id", None) != leader_id
        )
        peer_review_digests = tuple(
            build_peer_digest(review) if callable(build_peer_digest) else review
            for review in peer_reviews
        )
        superleader_review_digest = None
        if global_review is not None:
            superleader_review_digest = (
                build_global_digest(global_review)
                if callable(build_global_digest)
                else global_review
            )
        full_text_refs = list(
            _merge_unique_strings(
                tuple(
                    getattr(review, "full_text_ref", None)
                    for review in targeted_peer_reviews
                )
            )
        )

        draft_authority_risks = _merge_unique_strings(
            getattr(target_draft_plan, "authority_risks", ())
            if target_draft_plan is not None
            else ()
        )
        draft_project_item_ids = _merge_unique_strings(
            getattr(target_draft_plan, "project_items", ())
            if target_draft_plan is not None
            else ()
        )
        peer_review_project_item_ids = _merge_unique_strings(
            tuple(
                item
                for review in targeted_peer_reviews
                for item in getattr(review, "affected_project_items", ())
            )
        )
        global_required_authority_attention = _merge_unique_strings(
            getattr(global_review, "required_authority_attention", ())
            if global_review is not None
            else ()
        )
        relevant_review_items = await self.list_review_items(
            objective_id,
            item_kind=ReviewItemKind.PROJECT_ITEM,
        )
        filtered_review_items = tuple(
            item
            for item in relevant_review_items
            if (
                item.item_id
                in set(
                    _merge_unique_strings(
                        draft_project_item_ids,
                        peer_review_project_item_ids,
                        global_required_authority_attention,
                    )
                )
                or (
                    target_team_id is not None
                    and item.team_id in {None, target_team_id}
                    and item.lane_id in {None, target_lane_id}
                )
            )
        )
        project_item_ids = _merge_unique_strings(
            draft_project_item_ids,
            peer_review_project_item_ids,
            global_required_authority_attention,
            tuple(item.item_id for item in filtered_review_items),
        )

        lane_delivery_state = (
            await self.store.get_delivery_state(_lane_delivery_id(objective_id, target_lane_id))
            if target_lane_id is not None
            else None
        )
        task_surface_authority = (
            await self._lane_task_surface_authority_snapshot(
                group_id=objective.group_id,
                lane_id=target_lane_id,
                team_id=target_team_id,
                delivery_state=lane_delivery_state,
            )
            if target_lane_id is not None
            else {}
        )
        task_surface_authority_metadata = dict(task_surface_authority)
        if target_lane_id is not None:
            task_surface_authority_metadata["lane_id"] = target_lane_id
        if target_team_id is not None:
            task_surface_authority_metadata["team_id"] = target_team_id
        task_surface_authority_metadata["draft_authority_risks"] = list(
            draft_authority_risks
        )
        task_surface_authority_metadata["peer_review_attention_review_ids"] = list(
            _merge_unique_strings(
                tuple(
                    getattr(review, "review_id", None)
                    for review in targeted_peer_reviews
                    if bool(getattr(review, "requires_superleader_attention", False))
                    or "authority"
                    in str(getattr(review, "conflict_type", "")).strip().lower()
                )
            )
        )
        task_surface_authority_metadata["global_required_authority_attention"] = list(
            global_required_authority_attention
        )

        hotspot_conflicts: list[str] = []
        dependency_notices: list[str] = []
        authority_notices: list[str] = []
        project_item_notices: list[str] = []

        for review in targeted_peer_reviews:
            review_summary = str(getattr(review, "summary", "")).strip()
            review_id = str(getattr(review, "review_id", "")).strip()
            conflict_type = str(getattr(review, "conflict_type", "")).strip().lower()
            if "hotspot" in conflict_type:
                _append_unique_notice(
                    hotspot_conflicts,
                    review_summary or f"Peer review {review_id} raised a hotspot conflict.",
                )
            if "dependency" in conflict_type:
                _append_unique_notice(
                    dependency_notices,
                    review_summary or f"Peer review {review_id} raised a dependency notice.",
                )
            if bool(getattr(review, "requires_superleader_attention", False)) or "authority" in conflict_type:
                _append_unique_notice(
                    authority_notices,
                    (
                        f"Peer review {review_id} requires authority attention: {review_summary}"
                        if review_summary
                        else f"Peer review {review_id} requires authority attention."
                    ),
                )
            for item_id in _merge_unique_strings(getattr(review, "affected_project_items", ())):
                _append_unique_notice(
                    project_item_notices,
                    f"Peer review {review_id or 'unknown'} flagged project item {item_id}.",
                )

        for blocker in _merge_unique_strings(
            getattr(global_review, "activation_blockers", ()) if global_review is not None else ()
        ):
            _append_unique_notice(
                dependency_notices,
                f"Global activation blocker: {blocker}",
            )

        for task_id in _merge_unique_strings(
            task_surface_authority.get("mutation_waiting_task_ids", ())
        ):
            _append_unique_notice(
                authority_notices,
                f"Task {task_id} is waiting for task-surface mutation authority.",
            )
        for task_id in _merge_unique_strings(
            task_surface_authority.get("protected_field_waiting_task_ids", ())
        ):
            _append_unique_notice(
                authority_notices,
                f"Task {task_id} is waiting for protected task-surface field authority.",
            )
        for risk in draft_authority_risks:
            _append_unique_notice(
                authority_notices,
                f"Draft authority risk: {risk}",
            )
        for item_id in global_required_authority_attention:
            _append_unique_notice(
                authority_notices,
                f"Global review requires authority attention for {item_id}.",
            )

        for item in filtered_review_items:
            title = item.title.strip() if item.title else ""
            summary = item.summary.strip() if item.summary else ""
            descriptor = title or summary or item.item_id
            _append_unique_notice(
                project_item_notices,
                f"Project item {item.item_id}: {descriptor}",
            )

        project_item_surface = {
            "lane_id": target_lane_id,
            "team_id": target_team_id,
            "project_item_ids": list(project_item_ids),
            "draft_project_item_ids": list(draft_project_item_ids),
            "peer_review_project_item_ids": list(peer_review_project_item_ids),
            "review_item_ids": [item.item_id for item in filtered_review_items],
        }
        payload = {
            "objective_id": objective_id,
            "planning_round_id": planning_round_id,
            "leader_id": leader_id,
            "draft_plan_refs": list(draft_plan_refs),
            "peer_review_digests": list(peer_review_digests),
            "superleader_review_digest": superleader_review_digest,
            "hotspot_conflicts": hotspot_conflicts,
            "dependency_notices": dependency_notices,
            "authority_notices": authority_notices,
            "project_item_notices": project_item_notices,
            "full_text_refs": full_text_refs,
            "metadata": {
                "task_surface_authority": task_surface_authority_metadata,
                "project_item_surface": project_item_surface,
            },
        }
        bundle = _coerce_planning_contract_instance(
            contracts.LeaderRevisionContextBundle,
            payload,
        )
        return bundle  # type: ignore[return-value]

    async def publish_activation_gate_decision(
        self,
        *,
        objective_id: str,
        planning_round_id: str,
        status: str,
        summary: str,
        blockers: tuple[str, ...] = (),
        metadata: dict[str, object] | None = None,
        decision_id: str | None = None,
    ) -> "ActivationGateDecision":
        objective = await self.store.get_objective(objective_id)
        if objective is None:
            raise ValueError(f"Unknown objective_id: {objective_id}")
        contracts = _planning_review_contracts()
        payload = {
            "objective_id": objective_id,
            "planning_round_id": planning_round_id,
            "status": status,
            "summary": summary,
            "decision_id": decision_id
            or _planning_generated_id(
                contracts,
                "make_activation_gate_decision_id",
                prefix="activation-gate",
            ),
            "blockers": list(blockers),
            "metadata": dict(metadata or {}),
        }
        decision = _coerce_planning_contract_instance(contracts.ActivationGateDecision, payload)
        save_method = getattr(self.store, "save_activation_gate_decision", None)
        if not callable(save_method):
            raise NotImplementedError(
                "Store does not implement `save_activation_gate_decision` required by planning review runtime APIs."
            )
        await save_method(decision)
        return decision  # type: ignore[return-value]

    async def get_activation_gate_decision(
        self,
        objective_id: str,
        *,
        planning_round_id: str | None = None,
    ) -> "ActivationGateDecision | None":
        get_method = getattr(self.store, "get_activation_gate_decision", None)
        if not callable(get_method):
            raise NotImplementedError(
                "Store does not implement `get_activation_gate_decision` required by planning review runtime APIs."
            )
        if planning_round_id is None:
            return await get_method(objective_id)  # type: ignore[return-value]
        try:
            return await get_method(
                objective_id,
                planning_round_id=planning_round_id,
            )  # type: ignore[return-value]
        except TypeError:
            decision = await get_method(objective_id)
            if decision is None:
                return None
            if getattr(decision, "planning_round_id", None) != planning_round_id:
                return None
            return decision  # type: ignore[return-value]

    async def _project_claimed_task(
        self,
        *,
        task_id: str,
        owner_id: str,
        claim_source: str,
        claim_session_id: str | None,
        claimed_at: str | None = None,
    ) -> TaskCard | None:
        existing = await self.store.get_task(task_id)
        if existing is None:
            raise ValueError(f"Unknown task_id: {task_id}")
        existing = await self._hydrate_task_activation_contract(existing)
        effective_claimed_at = claimed_at or _utc_now_iso()
        effective_claim_session_id = claim_session_id or self._default_claim_session_id(
            owner_id=owner_id,
            task_id=task_id,
            claim_source=claim_source,
            claimed_at=effective_claimed_at,
        )
        if (
            existing.status == TaskStatus.PENDING
            and existing.owner_id is None
            and not existing.blocked_by
            and not await self._activation_dependency_blockers(existing)
        ):
            return replace(
                existing,
                status=TaskStatus.IN_PROGRESS,
                owner_id=owner_id,
                claim_source=claim_source,
                claim_session_id=effective_claim_session_id,
                claimed_at=effective_claimed_at,
            )
        if (
            existing.status == TaskStatus.IN_PROGRESS
            and existing.owner_id == owner_id
            and existing.claim_session_id == effective_claim_session_id
        ):
            return replace(existing)
        return None

    async def _project_task_status_update(
        self,
        *,
        task_id: str,
        status: TaskStatus,
        actor_id: str | None = None,
        blocked_by: tuple[str, ...] = (),
    ) -> TaskCard:
        task = await self.store.get_task(task_id)
        if task is None:
            raise ValueError(f"Unknown task_id: {task_id}")
        task = await self._hydrate_task_activation_contract(task)
        updated = replace(task, status=status)
        if (
            status == TaskStatus.IN_PROGRESS
            and actor_id is not None
            and updated.owner_id is None
        ):
            claimed_at = _utc_now_iso()
            updated = replace(
                updated,
                owner_id=actor_id,
                claim_source="runtime.update_task_status",
                claimed_at=claimed_at,
                claim_session_id=self._default_claim_session_id(
                    owner_id=actor_id,
                    task_id=updated.task_id,
                    claim_source="runtime.update_task_status",
                    claimed_at=claimed_at,
                ),
            )
        if status == TaskStatus.BLOCKED:
            updated = replace(updated, blocked_by=blocked_by)
        else:
            updated = replace(updated, blocked_by=())
        if status != TaskStatus.WAITING_FOR_AUTHORITY:
            updated = replace(updated, authority_request_id=None)
        return updated

    async def _commit_directed_task_receipt_via_store(
        self,
        commit: _StoreDirectedTaskReceiptCommit,
    ) -> bool:
        store_commit = getattr(self.store, "commit_directed_task_receipt", None)
        if not callable(store_commit):
            return False
        await store_commit(commit)
        return True

    async def _commit_teammate_result_via_store(
        self,
        commit: _StoreTeammateResultCommit,
    ) -> bool:
        store_commit = getattr(self.store, "commit_teammate_result", None)
        if not callable(store_commit):
            return False
        await store_commit(commit)
        return True

    async def _authoritative_blackboard_entry(
        self,
        blackboard_entry: BlackboardEntry,
    ) -> BlackboardEntry:
        entries = await self.store.list_blackboard_entries(blackboard_entry.blackboard_id)
        for persisted_entry in entries:
            if persisted_entry.entry_id == blackboard_entry.entry_id:
                return persisted_entry
        return blackboard_entry

    async def _authoritative_task(
        self,
        task: TaskCard,
    ) -> TaskCard:
        persisted_task = await self.store.get_task(task.task_id)
        return persisted_task or task

    async def _authoritative_delivery_state(
        self,
        delivery_state: DeliveryState | None,
    ) -> DeliveryState | None:
        if delivery_state is None:
            return None
        persisted_delivery_state = await self.store.get_delivery_state(delivery_state.delivery_id)
        return persisted_delivery_state or delivery_state

    async def _authoritative_session_snapshot(
        self,
        session_snapshot: AgentSession | None,
    ) -> AgentSession | None:
        if session_snapshot is None:
            return None
        session_host = self._session_host()
        if session_host is not None:
            persisted_session = await session_host.load_session(session_snapshot.session_id)
            if persisted_session is not None:
                return persisted_session
        persisted_session = await self.store.get_agent_session(session_snapshot.session_id)
        return persisted_session or session_snapshot

    def _supports_worker_session_coordination_transactions(self) -> bool:
        return bool(
            getattr(self.store, "supports_worker_session_coordination_transactions", False)
        )

    async def _project_worker_session_snapshot(
        self,
        *,
        session_snapshot: AgentSession | None = None,
        worker_session: WorkerSession | None = None,
        mailbox_cursor: Mapping[str, object] | None = None,
    ) -> WorkerSession | None:
        projected = (
            WorkerSession.from_dict(worker_session.to_dict())
            if worker_session is not None
            else None
        )
        if projected is None and session_snapshot is not None:
            persisted_worker_session = await self.store.get_worker_session(session_snapshot.session_id)
            if persisted_worker_session is not None:
                projected = WorkerSession.from_dict(persisted_worker_session.to_dict())
        if projected is None:
            return None

        effective_mailbox_cursor: dict[str, object]
        if mailbox_cursor is not None:
            effective_mailbox_cursor = {
                str(key): value for key, value in mailbox_cursor.items()
            }
        elif session_snapshot is not None and session_snapshot.mailbox_cursor:
            effective_mailbox_cursor = dict(session_snapshot.mailbox_cursor)
        else:
            effective_mailbox_cursor = dict(projected.mailbox_cursor)

        metadata = dict(projected.metadata)
        if session_snapshot is not None:
            if session_snapshot.objective_id is not None:
                metadata["objective_id"] = session_snapshot.objective_id
            if session_snapshot.lane_id is not None:
                metadata["lane_id"] = session_snapshot.lane_id
            if session_snapshot.team_id is not None:
                metadata["team_id"] = session_snapshot.team_id
            if session_snapshot.current_worker_session_id is not None:
                metadata["current_worker_session_id"] = session_snapshot.current_worker_session_id
                metadata["bound_worker_session_id"] = session_snapshot.current_worker_session_id
            else:
                metadata.pop("current_worker_session_id", None)
                metadata.pop("bound_worker_session_id", None)
            if session_snapshot.last_worker_session_id is not None:
                metadata["last_worker_session_id"] = session_snapshot.last_worker_session_id

        return replace(
            projected,
            mailbox_cursor=effective_mailbox_cursor,
            metadata=metadata,
        )

    async def _persist_worker_session_coordination_fallback(
        self,
        worker_session_snapshot: WorkerSession | None,
    ) -> None:
        if (
            worker_session_snapshot is None
            or self._supports_worker_session_coordination_transactions()
        ):
            return
        await self.store.save_worker_session(worker_session_snapshot)

    async def _authoritative_protocol_bus_cursor(
        self,
        protocol_bus_cursor: _StoreProtocolBusCursorCommit | None,
    ) -> dict[str, str | None] | None:
        if protocol_bus_cursor is None:
            return None
        persisted_cursor = await self.store.get_protocol_bus_cursor(
            stream=protocol_bus_cursor.stream,
            consumer=protocol_bus_cursor.consumer,
        )
        if not isinstance(persisted_cursor, Mapping):
            return dict(protocol_bus_cursor.cursor)
        return {
            str(key): (str(value) if value is not None else None)
            for key, value in persisted_cursor.items()
        }

    def _project_lane_delivery_state_for_teammate_event(
        self,
        *,
        state: DeliveryState,
        team_id: str | None,
        task_id: str,
        worker_id: str,
        event_kind: str,
        blackboard_entry_id: str,
        claim_session_id: str | None = None,
        claim_source: str | None = None,
        receipt_type: str | None = None,
        worker_status: WorkerStatus | None = None,
        session_snapshot_id: str | None = None,
        worker_session_id: str | None = None,
        consumer_cursor: Mapping[str, object] | None = None,
    ) -> DeliveryState:
        metadata = dict(state.metadata)
        coordination = dict(metadata.get("teammate_coordination", {}))

        if event_kind == "receipt":
            coordination["receipt_count"] = int(coordination.get("receipt_count", 0)) + 1
            coordination["last_receipt_task_id"] = task_id
            coordination["last_receipt_worker_id"] = worker_id
            coordination["last_receipt_claim_session_id"] = claim_session_id
            coordination["last_receipt_claim_source"] = claim_source
            coordination["last_receipt_receipt_type"] = receipt_type
            coordination["last_receipt_blackboard_entry_id"] = blackboard_entry_id
            coordination["last_receipt_session_id"] = session_snapshot_id
            if consumer_cursor is not None:
                coordination["last_receipt_consumer_cursor"] = dict(consumer_cursor)
            pending_task_ids = _merge_task_ids(
                state.pending_task_ids,
                remove=(task_id,),
            )
            active_task_ids = _merge_task_ids(
                state.active_task_ids,
                add=(task_id,),
            )
            completed_task_ids = state.completed_task_ids
            blocked_task_ids = state.blocked_task_ids
        else:
            coordination["result_count"] = int(coordination.get("result_count", 0)) + 1
            coordination["last_result_task_id"] = task_id
            coordination["last_result_worker_id"] = worker_id
            coordination["last_result_blackboard_entry_id"] = blackboard_entry_id
            coordination["last_result_status"] = worker_status.value if worker_status is not None else None
            coordination["last_result_claim_session_id"] = claim_session_id
            coordination["last_result_claim_source"] = claim_source
            coordination["last_result_session_id"] = session_snapshot_id
            coordination["last_result_worker_session_id"] = worker_session_id
            pending_task_ids = _merge_task_ids(
                state.pending_task_ids,
                remove=(task_id,),
            )
            active_task_ids = _merge_task_ids(
                state.active_task_ids,
                remove=(task_id,),
            )
            if worker_status == WorkerStatus.COMPLETED:
                completed_task_ids = _merge_task_ids(
                    state.completed_task_ids,
                    add=(task_id,),
                )
                blocked_task_ids = _merge_task_ids(
                    state.blocked_task_ids,
                    remove=(task_id,),
                )
            elif worker_status == WorkerStatus.FAILED:
                completed_task_ids = _merge_task_ids(
                    state.completed_task_ids,
                    remove=(task_id,),
                )
                blocked_task_ids = _merge_task_ids(
                    state.blocked_task_ids,
                    remove=(task_id,),
                )
            else:
                completed_task_ids = state.completed_task_ids
                blocked_task_ids = _merge_task_ids(
                    state.blocked_task_ids,
                    add=(task_id,),
                )

        metadata["teammate_coordination"] = coordination
        latest_worker_ids = _merge_task_ids(
            state.latest_worker_ids,
            add=(worker_id,),
        )
        return replace(
            state,
            team_id=team_id or state.team_id,
            pending_task_ids=pending_task_ids,
            active_task_ids=active_task_ids,
            completed_task_ids=completed_task_ids,
            blocked_task_ids=blocked_task_ids,
            latest_worker_ids=latest_worker_ids,
            metadata=metadata,
        )

    async def _update_lane_delivery_state_for_teammate_event(
        self,
        *,
        objective_id: str,
        lane_id: str,
        team_id: str | None,
        task_id: str,
        worker_id: str,
        event_kind: str,
        blackboard_entry_id: str,
        claim_session_id: str | None = None,
        claim_source: str | None = None,
        receipt_type: str | None = None,
        worker_status: WorkerStatus | None = None,
        session_snapshot_id: str | None = None,
        worker_session_id: str | None = None,
        consumer_cursor: Mapping[str, object] | None = None,
    ) -> DeliveryState | None:
        delivery_id = _lane_delivery_id(objective_id, lane_id)
        state = await self.store.get_delivery_state(delivery_id)
        if state is None:
            return None

        updated_state = self._project_lane_delivery_state_for_teammate_event(
            state=state,
            team_id=team_id,
            task_id=task_id,
            worker_id=worker_id,
            event_kind=event_kind,
            blackboard_entry_id=blackboard_entry_id,
            claim_session_id=claim_session_id,
            claim_source=claim_source,
            receipt_type=receipt_type,
            worker_status=worker_status,
            session_snapshot_id=session_snapshot_id,
            worker_session_id=worker_session_id,
            consumer_cursor=consumer_cursor,
        )
        await self.store.save_delivery_state(updated_state)
        return updated_state

    def _authority_request_from_worker_record(
        self,
        *,
        record: WorkerRecord,
        expected_assignment_id: str | None = None,
        expected_worker_id: str | None = None,
        expected_task_id: str | None = None,
    ) -> ScopeExtensionRequest | None:
        if record.metadata.get("protocol_failure_reason"):
            return None
        final_report = record.metadata.get("final_report")
        if not isinstance(final_report, Mapping):
            return None
        if str(final_report.get("terminal_status", "")).strip() != WorkerFinalStatus.BLOCKED.value:
            return None
        final_report_assignment_id = str(final_report.get("assignment_id", "")).strip()
        final_report_worker_id = str(final_report.get("worker_id", "")).strip()
        authoritative_assignment_id = expected_assignment_id or record.assignment_id
        authoritative_worker_id = expected_worker_id or record.worker_id
        if (
            not final_report_assignment_id
            or final_report_assignment_id != authoritative_assignment_id
            or final_report_worker_id != authoritative_worker_id
        ):
            return None
        request = ScopeExtensionRequest.from_payload(final_report.get("authority_request"))
        if request is None:
            metadata = final_report.get("metadata")
            if not isinstance(metadata, Mapping):
                return None
            request = ScopeExtensionRequest.from_payload(metadata.get("authority_request"))
        if request is None:
            return None
        if request.assignment_id != authoritative_assignment_id or request.worker_id != authoritative_worker_id:
            return None
        if expected_task_id is not None and request.task_id != expected_task_id:
            return None
        return request

    def _project_lane_delivery_state_for_authority_request(
        self,
        *,
        state: DeliveryState,
        team_id: str | None,
        task_id: str,
        worker_id: str,
        blackboard_entry_id: str,
        authority_request: ScopeExtensionRequest,
        claim_session_id: str | None = None,
        claim_source: str | None = None,
        session_snapshot_id: str | None = None,
        worker_session_id: str | None = None,
    ) -> DeliveryState:
        metadata = dict(state.metadata)
        coordination = dict(metadata.get("teammate_coordination", {}))
        coordination["authority_request_count"] = int(coordination.get("authority_request_count", 0)) + 1
        coordination["last_authority_request_task_id"] = task_id
        coordination["last_authority_request_worker_id"] = worker_id
        coordination["last_authority_request_id"] = authority_request.request_id
        coordination["last_authority_request_blackboard_entry_id"] = blackboard_entry_id
        coordination["last_authority_request_claim_session_id"] = claim_session_id
        coordination["last_authority_request_claim_source"] = claim_source
        coordination["last_authority_request_session_id"] = session_snapshot_id
        coordination["last_authority_request_worker_session_id"] = worker_session_id
        waiting_task_ids = _merge_task_ids(
            tuple(
                str(item)
                for item in metadata.get("waiting_for_authority_task_ids", ())
                if isinstance(item, str) and item
            ),
            add=(task_id,),
        )
        metadata["teammate_coordination"] = coordination
        metadata["authority_waiting"] = True
        metadata["waiting_for_authority_task_ids"] = list(waiting_task_ids)
        latest_worker_ids = _merge_task_ids(
            state.latest_worker_ids,
            add=(worker_id,),
        )
        return replace(
            state,
            team_id=team_id or state.team_id,
            pending_task_ids=_merge_task_ids(state.pending_task_ids, remove=(task_id,)),
            active_task_ids=_merge_task_ids(state.active_task_ids, remove=(task_id,)),
            completed_task_ids=_merge_task_ids(state.completed_task_ids, remove=(task_id,)),
            blocked_task_ids=_merge_task_ids(state.blocked_task_ids, remove=(task_id,)),
            latest_worker_ids=latest_worker_ids,
            metadata=metadata,
        )

    def _project_lane_delivery_state_for_authority_decision(
        self,
        *,
        state: DeliveryState,
        team_id: str | None,
        task_id: str,
        worker_id: str,
        blackboard_entry_id: str,
        decision: AuthorityDecision,
        task: TaskCard | None = None,
        task_surface_intent: TaskSurfaceAuthorityIntent | None = None,
        replacement_task_id: str | None = None,
    ) -> DeliveryState:
        metadata = dict(state.metadata)
        coordination = dict(metadata.get("teammate_coordination", {}))
        coordination["last_authority_decision_task_id"] = task_id
        coordination["last_authority_decision_worker_id"] = worker_id
        coordination["last_authority_decision_request_id"] = decision.request_id
        coordination["last_authority_decision_blackboard_entry_id"] = blackboard_entry_id
        coordination["last_authority_decision"] = decision.decision
        if decision.decision == "reroute" and replacement_task_id is not None:
            coordination["last_authority_reroute_task_id"] = replacement_task_id
        waiting_task_ids = _merge_task_ids(
            tuple(
                str(item)
                for item in metadata.get("waiting_for_authority_task_ids", ())
                if isinstance(item, str) and item
            ),
            remove=(task_id,),
        )
        metadata["teammate_coordination"] = coordination
        metadata["authority_waiting"] = bool(waiting_task_ids)
        metadata["waiting_for_authority_task_ids"] = list(waiting_task_ids)
        if (
            decision.decision == "grant"
            and task is not None
            and task_surface_intent is not None
        ):
            pending_task_ids = _merge_task_ids(state.pending_task_ids, remove=(task_id,))
            active_task_ids = _merge_task_ids(state.active_task_ids, remove=(task_id,))
            completed_task_ids = _merge_task_ids(state.completed_task_ids, remove=(task_id,))
            blocked_task_ids = _merge_task_ids(state.blocked_task_ids, remove=(task_id,))
            if task.status == TaskStatus.PENDING:
                pending_task_ids = _merge_task_ids(pending_task_ids, add=(task_id,))
            elif task.status in {TaskStatus.IN_PROGRESS, TaskStatus.RUNNING}:
                active_task_ids = _merge_task_ids(active_task_ids, add=(task_id,))
            elif task.status == TaskStatus.COMPLETED:
                completed_task_ids = _merge_task_ids(completed_task_ids, add=(task_id,))
            elif task.status == TaskStatus.BLOCKED:
                blocked_task_ids = _merge_task_ids(blocked_task_ids, add=(task_id,))
            if task_surface_intent.kind == TaskSurfaceAuthorityIntentKind.MUTATION:
                coordination["last_task_surface_mutation_task_id"] = task.task_id
                coordination["last_task_surface_mutation_actor_id"] = task_surface_intent.actor_id
                coordination["last_task_surface_mutation_blackboard_entry_id"] = blackboard_entry_id
                coordination["last_task_surface_mutation_kind"] = task.surface_mutation.kind.value
                if task.surface_mutation.target_task_id is not None:
                    coordination["last_task_surface_mutation_target_task_id"] = (
                        task.surface_mutation.target_task_id
                    )
            else:
                coordination["last_task_surface_write_task_id"] = task.task_id
                coordination["last_task_surface_write_actor_id"] = task_surface_intent.actor_id
                coordination["last_task_surface_write_blackboard_entry_id"] = blackboard_entry_id
                coordination["last_task_surface_write_field_names"] = list(
                    task_surface_intent.protected_field_names
                )
            metadata["teammate_coordination"] = coordination
            status = DeliveryStatus.RUNNING
            summary = f"Authority granted for task-surface intent on task {task_id}."
        elif decision.decision == "grant":
            pending_task_ids = _merge_task_ids(state.pending_task_ids, add=(task_id,), remove=())
            active_task_ids = _merge_task_ids(state.active_task_ids, remove=(task_id,))
            completed_task_ids = _merge_task_ids(state.completed_task_ids, remove=(task_id,))
            blocked_task_ids = _merge_task_ids(state.blocked_task_ids, remove=(task_id,))
            status = DeliveryStatus.RUNNING
            summary = f"Authority granted for task {task_id}."
        elif decision.decision == "deny":
            pending_task_ids = _merge_task_ids(state.pending_task_ids, remove=(task_id,))
            active_task_ids = _merge_task_ids(state.active_task_ids, remove=(task_id,))
            completed_task_ids = _merge_task_ids(state.completed_task_ids, remove=(task_id,))
            blocked_task_ids = _merge_task_ids(state.blocked_task_ids, add=(task_id,))
            status = DeliveryStatus.BLOCKED
            summary = f"Authority denied for task {task_id}."
        elif decision.decision == "reroute":
            pending_task_ids = _merge_task_ids(
                state.pending_task_ids,
                add=((replacement_task_id,) if replacement_task_id else ()),
                remove=(task_id,),
            )
            active_task_ids = _merge_task_ids(state.active_task_ids, remove=(task_id,))
            completed_task_ids = _merge_task_ids(state.completed_task_ids, remove=(task_id,))
            blocked_task_ids = _merge_task_ids(state.blocked_task_ids, remove=(task_id,))
            status = DeliveryStatus.RUNNING
            if replacement_task_id is not None:
                summary = f"Authority rerouted task {task_id} to replacement task {replacement_task_id}."
            else:
                summary = f"Authority rerouted task {task_id}."
        else:
            pending_task_ids = state.pending_task_ids
            active_task_ids = state.active_task_ids
            completed_task_ids = state.completed_task_ids
            blocked_task_ids = state.blocked_task_ids
            status = DeliveryStatus.WAITING_FOR_AUTHORITY
            summary = f"Authority request for task {task_id} escalated."
        return replace(
            state,
            team_id=team_id or state.team_id,
            pending_task_ids=pending_task_ids,
            active_task_ids=active_task_ids,
            completed_task_ids=completed_task_ids,
            blocked_task_ids=blocked_task_ids,
            latest_worker_ids=_merge_task_ids(state.latest_worker_ids, add=(worker_id,)),
            status=status,
            summary=summary,
            metadata=metadata,
        )

    @staticmethod
    def _task_surface_mutation_kind(
        mutation_kind: TaskSurfaceMutationKind | str,
    ) -> TaskSurfaceMutationKind:
        if isinstance(mutation_kind, TaskSurfaceMutationKind):
            return mutation_kind
        normalized = str(mutation_kind).strip()
        try:
            return TaskSurfaceMutationKind(normalized)
        except ValueError as exc:
            raise ValueError(f"Unsupported task surface mutation kind: {mutation_kind}") from exc

    def _apply_task_surface_mutation(
        self,
        *,
        task: TaskCard,
        actor_id: str,
        mutation_kind: TaskSurfaceMutationKind,
        reason: str,
        target_task_id: str | None = None,
    ) -> TaskCard:
        mutation = TaskSurfaceMutation(
            kind=mutation_kind,
            actor_id=actor_id,
            reason=reason,
            target_task_id=target_task_id,
        )
        return replace(
            task,
            status=TaskStatus.CANCELLED,
            owner_id=None,
            claim_session_id=None,
            claimed_at=None,
            claim_source=None,
            blocked_by=(),
            authority_request_id=None,
            authority_request_payload={},
            authority_waiting_since=None,
            authority_resume_target=None,
            superseded_by_task_id=(
                target_task_id if mutation_kind == TaskSurfaceMutationKind.SUPERSEDED else None
            ),
            merged_into_task_id=(
                target_task_id if mutation_kind == TaskSurfaceMutationKind.MERGED_INTO else None
            ),
            surface_mutation=mutation,
        )

    @staticmethod
    def _normalized_task_surface_field_updates(
        field_updates: Mapping[str, object],
    ) -> dict[str, object]:
        normalized: dict[str, object] = {}
        sequence_fields = {
            "owned_paths",
            "allowed_inputs",
            "output_artifacts",
            "verification_commands",
            "handoff_to",
        }
        for raw_field_name, raw_value in field_updates.items():
            if not isinstance(raw_field_name, str):
                continue
            field_name = raw_field_name.strip()
            if not field_name:
                continue
            if field_name in sequence_fields:
                normalized[field_name] = _string_tuple(raw_value)
                continue
            if field_name == "goal":
                goal = str(raw_value).strip()
                if not goal:
                    raise ValueError("Task surface protected field write requires a non-empty goal.")
                normalized[field_name] = goal
                continue
            if field_name == "merge_target":
                merge_target = str(raw_value).strip() if raw_value is not None else ""
                normalized[field_name] = merge_target or None
                continue
            raise ValueError(
                f"Unsupported protected task-surface field write: {field_name}"
            )
        if not normalized:
            raise ValueError("Task surface protected field write requires at least one supported field update.")
        return normalized

    def _apply_task_surface_field_write(
        self,
        *,
        task: TaskCard,
        authority_decision: AuthorityDecision,
        intent: TaskSurfaceAuthorityIntent,
        boundary_class: str,
    ) -> TaskCard:
        normalized_updates = self._normalized_task_surface_field_updates(intent.field_updates)
        return replace(
            task,
            **normalized_updates,
            status=intent.resume_status,
            blocked_by=(),
            authority_request_id=None,
            authority_request_payload={},
            authority_decision_payload=authority_decision.to_dict(),
            authority_boundary_class=boundary_class,
            authority_waiting_since=None,
            authority_resume_target=intent.actor_id,
        )

    def _project_lane_delivery_state_for_task_surface_mutation(
        self,
        *,
        state: DeliveryState,
        team_id: str | None,
        task_id: str,
        actor_id: str,
        blackboard_entry_id: str,
        mutation: TaskSurfaceMutation,
    ) -> DeliveryState:
        metadata = dict(state.metadata)
        coordination = dict(metadata.get("teammate_coordination", {}))
        coordination["last_task_surface_mutation_task_id"] = task_id
        coordination["last_task_surface_mutation_actor_id"] = actor_id
        coordination["last_task_surface_mutation_blackboard_entry_id"] = blackboard_entry_id
        coordination["last_task_surface_mutation_kind"] = mutation.kind.value
        if mutation.target_task_id is not None:
            coordination["last_task_surface_mutation_target_task_id"] = mutation.target_task_id
        metadata["teammate_coordination"] = coordination
        pending_task_ids = _merge_task_ids(state.pending_task_ids, remove=(task_id,))
        active_task_ids = _merge_task_ids(state.active_task_ids, remove=(task_id,))
        completed_task_ids = _merge_task_ids(state.completed_task_ids, remove=(task_id,))
        blocked_task_ids = _merge_task_ids(state.blocked_task_ids, remove=(task_id,))
        if (
            mutation.kind == TaskSurfaceMutationKind.SUPERSEDED
            and mutation.target_task_id is not None
        ):
            pending_task_ids = _merge_task_ids(
                pending_task_ids,
                add=(mutation.target_task_id,),
            )
        summary = f"Task {task_id} mutation `{mutation.kind.value}` committed."
        status = state.status
        if status == DeliveryStatus.WAITING_FOR_AUTHORITY:
            status = DeliveryStatus.RUNNING
        return replace(
            state,
            team_id=team_id or state.team_id,
            pending_task_ids=pending_task_ids,
            active_task_ids=active_task_ids,
            completed_task_ids=completed_task_ids,
            blocked_task_ids=blocked_task_ids,
            latest_worker_ids=_merge_task_ids(state.latest_worker_ids, add=(actor_id,)),
            status=status,
            summary=summary,
            metadata=metadata,
        )

    def _project_lane_delivery_state_for_task_surface_field_write(
        self,
        *,
        state: DeliveryState,
        team_id: str | None,
        task: TaskCard,
        actor_id: str,
        blackboard_entry_id: str,
        field_names: tuple[str, ...],
    ) -> DeliveryState:
        metadata = dict(state.metadata)
        coordination = dict(metadata.get("teammate_coordination", {}))
        coordination["last_task_surface_write_task_id"] = task.task_id
        coordination["last_task_surface_write_actor_id"] = actor_id
        coordination["last_task_surface_write_blackboard_entry_id"] = blackboard_entry_id
        coordination["last_task_surface_write_field_names"] = list(field_names)
        metadata["teammate_coordination"] = coordination
        return replace(
            state,
            team_id=team_id or state.team_id,
            latest_worker_ids=_merge_task_ids(state.latest_worker_ids, add=(actor_id,)),
            metadata=metadata,
        )

    def _classify_authority_boundary(
        self,
        requested_paths: tuple[str, ...],
    ) -> str:
        normalized_paths = tuple(
            str(path).strip()
            for path in requested_paths
            if str(path).strip()
        )
        if any(path.startswith("task_surface/cross-team/") for path in normalized_paths):
            return "cross_team_shared"
        if any(path.startswith("task_surface/protected/") for path in normalized_paths):
            return "protected_runtime"
        if any(path.startswith("task_surface/cross-subtree/") for path in normalized_paths):
            return "soft_scope"
        return self.authority_policy.classify_boundary(requested_paths).value

    def _task_authority_request(
        self,
        task: TaskCard,
    ) -> ScopeExtensionRequest | None:
        if not task.authority_request_payload:
            return None
        return ScopeExtensionRequest.from_payload(task.authority_request_payload)

    @staticmethod
    def _task_surface_authority_intent(
        task: TaskCard,
    ) -> TaskSurfaceAuthorityIntent | None:
        if not isinstance(task.authority_request_payload, Mapping):
            return None
        return TaskSurfaceAuthorityIntent.from_payload(
            task.authority_request_payload.get("task_surface_authority")
        )

    @staticmethod
    def _task_surface_field_updates_payload(
        field_updates: Mapping[str, object],
    ) -> dict[str, object]:
        return {
            str(key): value
            for key, value in field_updates.items()
            if isinstance(key, str) and key.strip()
        }

    def _task_surface_authority_requested_paths(
        self,
        *,
        task: TaskCard,
        intent: TaskSurfaceAuthorityIntent,
        target_task: TaskCard | None = None,
    ) -> tuple[str, ...]:
        if intent.kind == TaskSurfaceAuthorityIntentKind.PROTECTED_FIELD_WRITE:
            if intent.protected_field_names:
                return tuple(
                    f"task_surface/protected/{field_name}"
                    for field_name in intent.protected_field_names
                )
            return (f"task_surface/protected/{task.task_id}",)
        if target_task is not None and (
            task.group_id != target_task.group_id
            or task.team_id != target_task.team_id
            or task.lane != target_task.lane
        ):
            target_id = target_task.task_id or task.task_id
            return (f"task_surface/cross-team/{target_id}",)
        if task.scope != TaskScope.TEAM or (
            target_task is not None and target_task.scope != TaskScope.TEAM
        ):
            return (f"task_surface/protected/{task.task_id}",)
        return (f"task_surface/cross-subtree/{task.task_id}",)

    @staticmethod
    def _task_surface_authority_evidence(
        *,
        task: TaskCard,
        intent: TaskSurfaceAuthorityIntent,
        target_task: TaskCard | None = None,
    ) -> str:
        if intent.kind == TaskSurfaceAuthorityIntentKind.PROTECTED_FIELD_WRITE:
            if intent.protected_field_names:
                field_names = ", ".join(intent.protected_field_names)
                return f"Protected task-surface fields require higher authority: {field_names}."
            return "Protected task-surface fields require higher authority."
        if target_task is not None and (
            task.group_id != target_task.group_id
            or task.team_id != target_task.team_id
            or task.lane != target_task.lane
        ):
            return (
                "Task-surface mutation crosses group/team/lane boundaries and must be "
                "resolved through authority routing."
            )
        return (
            "Task-surface mutation targets a task outside the teammate-owned subtree and "
            "must be resolved through authority routing."
        )

    @staticmethod
    def _task_surface_authority_retry_hint(
        intent: TaskSurfaceAuthorityIntent,
    ) -> str:
        if intent.kind == TaskSurfaceAuthorityIntentKind.PROTECTED_FIELD_WRITE:
            return "Need higher authority to mutate protected task-surface fields."
        return "Need authority decision before mutating task surface outside the local subtree."

    async def _commit_task_surface_authority_request(
        self,
        *,
        objective_id: str,
        lane_id: str,
        team_id: str,
        task: TaskCard,
        actor_id: str,
        reason: str,
        intent: TaskSurfaceAuthorityIntent,
        target_task: TaskCard | None = None,
        session_snapshot: AgentSession | None = None,
    ) -> TaskSurfaceMutationCommit:
        request_nonce = uuid4().hex
        authority_request = ScopeExtensionRequest(
            request_id=f"{task.task_id}:task-surface-authority:{request_nonce}",
            assignment_id=f"{task.task_id}:task-surface-authority:{request_nonce}",
            worker_id=actor_id,
            task_id=task.task_id,
            requested_paths=self._task_surface_authority_requested_paths(
                task=task,
                intent=intent,
                target_task=target_task,
            ),
            reason=reason.strip(),
            evidence=self._task_surface_authority_evidence(
                task=task,
                intent=intent,
                target_task=target_task,
            ),
            retry_hint=self._task_surface_authority_retry_hint(intent),
        )
        authority_commit = await self.commit_authority_request(
            objective_id=objective_id,
            lane_id=lane_id,
            team_id=team_id,
            task_id=task.task_id,
            worker_id=actor_id,
            authority_request=authority_request,
            record=WorkerRecord(
                worker_id=actor_id,
                assignment_id=authority_request.assignment_id,
                backend="runtime.task_surface",
                role="teammate",
                status=WorkerStatus.FAILED,
                error_text=authority_request.evidence,
                metadata={},
            ),
            session_snapshot=session_snapshot,
            request_metadata={
                "task_surface_authority": intent.to_dict(),
            },
        )
        return TaskSurfaceMutationCommit(
            task=authority_commit.task,
            blackboard_entry=authority_commit.blackboard_entry,
            delivery_state=authority_commit.delivery_state,
            session_snapshot=authority_commit.session_snapshot,
            authority_request=authority_commit.authority_request,
            post_commit_outbox=authority_commit.post_commit_outbox,
        )

    async def commit_task_surface_mutation(
        self,
        *,
        objective_id: str,
        lane_id: str,
        team_id: str,
        task_id: str,
        actor_id: str,
        mutation_kind: TaskSurfaceMutationKind | str,
        reason: str,
        target_task_id: str | None = None,
        session_snapshot: AgentSession | None = None,
    ) -> TaskSurfaceMutationCommit:
        task = await self.store.get_task(task_id)
        if task is None:
            raise ValueError(f"Unknown task_id: {task_id}")
        task = await self._hydrate_task_activation_contract(task)
        resolved_kind = self._task_surface_mutation_kind(mutation_kind)
        if resolved_kind in {
            TaskSurfaceMutationKind.SUPERSEDED,
            TaskSurfaceMutationKind.MERGED_INTO,
        } and not target_task_id:
            raise ValueError(f"Task surface mutation `{resolved_kind.value}` requires target_task_id.")
        target_task: TaskCard | None = None
        if target_task_id is not None:
            target_task = await self.store.get_task(target_task_id)
            if target_task is not None:
                target_task = await self._hydrate_task_activation_contract(target_task)
        decision = await self.classify_task_surface_mutation_authority(
            task_id=task.task_id,
            actor_id=actor_id,
            reason=reason,
            target_task_id=target_task_id,
        )
        if decision.verdict == TaskSurfaceAuthorityVerdict.ESCALATION_NEEDED:
            return await self._commit_task_surface_authority_request(
                objective_id=objective_id,
                lane_id=lane_id,
                team_id=team_id,
                task=task,
                actor_id=actor_id,
                reason=reason,
                intent=TaskSurfaceAuthorityIntent(
                    kind=TaskSurfaceAuthorityIntentKind.MUTATION,
                    actor_id=actor_id,
                    reason=reason,
                    resume_status=task.status,
                    mutation_kind=resolved_kind,
                    target_task_id=target_task_id,
                ),
                target_task=target_task,
                session_snapshot=session_snapshot,
            )
        self._raise_task_surface_authority_decision(decision)
        task = self._apply_task_surface_mutation(
            task=task,
            actor_id=actor_id,
            mutation_kind=resolved_kind,
            reason=reason,
            target_task_id=target_task_id,
        )
        if task.group_id is None:
            raise ValueError(f"Task {task.task_id} is missing group scope for task surface mutation commit.")
        blackboard_entry = self._build_blackboard_entry(
            group_id=task.group_id,
            kind=BlackboardKind.LEADER_LANE,
            entry_kind=BlackboardEntryKind.DECISION,
            author_id=actor_id,
            lane_id=lane_id,
            team_id=team_id,
            task_id=task.task_id,
            summary=f"Committed task surface mutation `{resolved_kind.value}` for task {task.task_id}.",
            payload={
                "event": "task.mutation",
                "mutation": task.surface_mutation.to_dict(),
            },
        )
        state = await self.store.get_delivery_state(_lane_delivery_id(objective_id, lane_id))
        delivery_state = (
            self._project_lane_delivery_state_for_task_surface_mutation(
                state=state,
                team_id=team_id,
                task_id=task.task_id,
                actor_id=actor_id,
                blackboard_entry_id=blackboard_entry.entry_id,
                mutation=task.surface_mutation,
            )
            if state is not None
            else None
        )
        await self.store.commit_coordination_transaction(
            CoordinationTransactionStoreCommit(
                task_mutations=(task,),
                blackboard_entries=(blackboard_entry,),
                delivery_snapshots=(delivery_state,) if delivery_state is not None else (),
                session_snapshots=(session_snapshot,) if session_snapshot is not None else (),
                outbox_scope_id=blackboard_entry.entry_id,
            )
        )
        return TaskSurfaceMutationCommit(
            task=task,
            blackboard_entry=blackboard_entry,
            delivery_state=delivery_state,
            session_snapshot=session_snapshot,
        )

    async def commit_task_protected_field_write(
        self,
        *,
        objective_id: str,
        lane_id: str,
        team_id: str,
        task_id: str,
        actor_id: str,
        field_updates: Mapping[str, object],
        reason: str,
        session_snapshot: AgentSession | None = None,
    ) -> TaskSurfaceMutationCommit:
        task = await self.store.get_task(task_id)
        if task is None:
            raise ValueError(f"Unknown task_id: {task_id}")
        task = await self._hydrate_task_activation_contract(task)
        normalized_field_updates = self._task_surface_field_updates_payload(field_updates)
        protected_field_names = tuple(normalized_field_updates.keys())
        decision = await self.classify_task_protected_field_write(
            task_id=task.task_id,
            actor_id=actor_id,
            field_names=protected_field_names,
        )
        if decision.verdict == TaskSurfaceAuthorityVerdict.ESCALATION_NEEDED:
            return await self._commit_task_surface_authority_request(
                objective_id=objective_id,
                lane_id=lane_id,
                team_id=team_id,
                task=task,
                actor_id=actor_id,
                reason=reason,
                intent=TaskSurfaceAuthorityIntent(
                    kind=TaskSurfaceAuthorityIntentKind.PROTECTED_FIELD_WRITE,
                    actor_id=actor_id,
                    reason=reason,
                    resume_status=task.status,
                    protected_field_names=decision.protected_field_names,
                    field_updates=normalized_field_updates,
                ),
                session_snapshot=session_snapshot,
            )
        self._raise_task_surface_authority_decision(decision)
        task = self._apply_task_surface_field_write(
            task=task,
            authority_decision=AuthorityDecision(
                request_id="",
                decision="local_write",
                actor_id=actor_id,
                scope_class="",
                reason=reason,
                summary=f"Committed protected task-surface field write for task {task.task_id}.",
            ),
            intent=TaskSurfaceAuthorityIntent(
                kind=TaskSurfaceAuthorityIntentKind.PROTECTED_FIELD_WRITE,
                actor_id=actor_id,
                reason=reason,
                resume_status=task.status,
                protected_field_names=decision.protected_field_names,
                field_updates=normalized_field_updates,
            ),
            boundary_class=task.authority_boundary_class or "",
        )
        if task.group_id is None:
            raise ValueError(
                f"Task {task.task_id} is missing group scope for task surface field write commit."
            )
        blackboard_entry = self._build_blackboard_entry(
            group_id=task.group_id,
            kind=BlackboardKind.LEADER_LANE,
            entry_kind=BlackboardEntryKind.DECISION,
            author_id=actor_id,
            lane_id=lane_id,
            team_id=team_id,
            task_id=task.task_id,
            summary=f"Committed protected task-surface field write for task {task.task_id}.",
            payload={
                "event": "task.field_write",
                "field_names": list(protected_field_names),
                "field_updates": normalized_field_updates,
            },
        )
        state = await self.store.get_delivery_state(_lane_delivery_id(objective_id, lane_id))
        delivery_state = (
            self._project_lane_delivery_state_for_task_surface_field_write(
                state=state,
                team_id=team_id,
                task=task,
                actor_id=actor_id,
                blackboard_entry_id=blackboard_entry.entry_id,
                field_names=protected_field_names,
            )
            if state is not None
            else None
        )
        await self.store.commit_coordination_transaction(
            CoordinationTransactionStoreCommit(
                task_mutations=(task,),
                blackboard_entries=(blackboard_entry,),
                delivery_snapshots=(delivery_state,) if delivery_state is not None else (),
                session_snapshots=(session_snapshot,) if session_snapshot is not None else (),
                outbox_scope_id=blackboard_entry.entry_id,
            )
        )
        return TaskSurfaceMutationCommit(
            task=task,
            blackboard_entry=blackboard_entry,
            delivery_state=delivery_state,
            session_snapshot=session_snapshot,
        )

    async def list_pending_authority_requests(
        self,
        *,
        group_id: str,
        team_id: str | None = None,
        lane_id: str | None = None,
    ) -> tuple[PendingAuthorityRequest, ...]:
        tasks = await self.store.list_tasks(
            group_id,
            team_id=team_id,
            lane_id=lane_id,
            scope=TaskScope.TEAM.value,
        )
        hydrated = await self._hydrate_task_activation_contracts(tasks)
        pending: list[PendingAuthorityRequest] = []
        for task in hydrated:
            if task.status != TaskStatus.WAITING_FOR_AUTHORITY:
                continue
            request = self._task_authority_request(task)
            if request is None:
                continue
            pending.append(
                PendingAuthorityRequest(
                    task=task,
                    authority_request=request,
                    boundary_class=task.authority_boundary_class or self._classify_authority_boundary(
                        request.requested_paths
                    ),
                    waiting_since=task.authority_waiting_since,
                )
            )
        pending.sort(key=lambda item: (item.task.task_id, item.authority_request.request_id))
        return tuple(pending)

    async def commit_authority_request(
        self,
        *,
        objective_id: str,
        lane_id: str,
        team_id: str,
        task_id: str,
        worker_id: str,
        authority_request: ScopeExtensionRequest,
        record: WorkerRecord,
        session_snapshot: AgentSession | None = None,
        request_metadata: Mapping[str, object] | None = None,
    ) -> AuthorityRequestCommit:
        task = await self.store.get_task(task_id)
        if task is None:
            raise ValueError(f"Unknown task_id: {task_id}")
        task = await self._hydrate_task_activation_contract(task)
        boundary_class = self._classify_authority_boundary(authority_request.requested_paths)
        authority_request_payload = authority_request.to_dict()
        if request_metadata:
            authority_request_payload.update(
                {
                    str(key): value
                    for key, value in request_metadata.items()
                    if isinstance(key, str) and key.strip()
                }
            )
        task = replace(
            task,
            status=TaskStatus.WAITING_FOR_AUTHORITY,
            blocked_by=(),
            authority_request_id=authority_request.request_id,
            authority_request_payload=authority_request_payload,
            authority_boundary_class=boundary_class,
            authority_waiting_since=_utc_now_iso(),
            authority_resume_target=worker_id,
        )
        if task.group_id is None:
            raise ValueError(f"Task {task.task_id} is missing group scope for authority request commit.")
        session_snapshot_id = session_snapshot.session_id if session_snapshot is not None else None
        worker_session_id = record.session.session_id if record.session is not None else None
        blackboard_entry = self._build_blackboard_entry(
            group_id=task.group_id,
            kind=BlackboardKind.TEAM,
            entry_kind=BlackboardEntryKind.PROPOSAL,
            author_id=worker_id,
            lane_id=lane_id,
            team_id=team_id,
            task_id=task.task_id,
            summary=f"Teammate {worker_id} requested authority for task {task.task_id}.",
            payload={
                "event": "authority.request",
                "worker_status": record.status.value,
                "claim_session_id": task.claim_session_id,
                "claim_source": task.claim_source,
                "error_text": record.error_text,
                "authority_request": authority_request.to_dict(),
                **(
                    {
                        str(key): value
                        for key, value in request_metadata.items()
                        if isinstance(key, str) and key.strip()
                    }
                    if request_metadata
                    else {}
                ),
                **({"session_snapshot_id": session_snapshot_id} if session_snapshot_id is not None else {}),
                **({"worker_session_id": worker_session_id} if worker_session_id is not None else {}),
            },
        )
        post_commit_outbox = (
            CoordinationOutboxRecord(
                subject="authority.request",
                recipient=f"leader:{lane_id}",
                sender=worker_id,
                payload={
                    "task_id": task.task_id,
                    "authority_request": authority_request.to_dict(),
                    **(
                        {
                            str(key): value
                            for key, value in request_metadata.items()
                            if isinstance(key, str) and key.strip()
                        }
                        if request_metadata
                        else {}
                    ),
                },
                metadata={
                    "group_id": task.group_id,
                    "lane_id": lane_id,
                    "team_id": team_id,
                },
            ),
        )
        blackboard_entry.payload["coordination_outbox"] = [
            record.to_dict() for record in post_commit_outbox
        ]
        state = await self.store.get_delivery_state(_lane_delivery_id(objective_id, lane_id))
        delivery_state = (
            self._project_lane_delivery_state_for_authority_request(
                state=state,
                team_id=team_id,
                task_id=task.task_id,
                worker_id=worker_id,
                blackboard_entry_id=blackboard_entry.entry_id,
                authority_request=authority_request,
                claim_session_id=task.claim_session_id,
                claim_source=task.claim_source,
                session_snapshot_id=session_snapshot_id,
                worker_session_id=worker_session_id,
            )
            if state is not None
            else None
        )
        worker_session_snapshot = await self._project_worker_session_snapshot(
            session_snapshot=session_snapshot,
            worker_session=record.session,
        )
        store_commit = AuthorityRequestStoreCommit(
            task=task,
            authority_request=authority_request,
            blackboard_entry=blackboard_entry,
            delivery_state=delivery_state,
            agent_session=session_snapshot,
            worker_session=worker_session_snapshot,
            post_commit_outbox=post_commit_outbox,
        )
        await self.store.commit_authority_request(store_commit)
        await self._persist_worker_session_coordination_fallback(worker_session_snapshot)
        task = await self._authoritative_task(task)
        blackboard_entry = await self._authoritative_blackboard_entry(blackboard_entry)
        delivery_state = await self._authoritative_delivery_state(delivery_state)
        session_snapshot = await self._authoritative_session_snapshot(session_snapshot)
        return AuthorityRequestCommit(
            task=task,
            authority_request=authority_request,
            blackboard_entry=blackboard_entry,
            delivery_state=delivery_state,
            session_snapshot=session_snapshot,
            post_commit_outbox=post_commit_outbox,
        )

    async def commit_authority_decision(
        self,
        *,
        objective_id: str,
        lane_id: str,
        team_id: str,
        task_id: str,
        actor_id: str,
        authority_decision: AuthorityDecision,
        replacement_task: TaskCard | None = None,
        session_snapshot: AgentSession | None = None,
    ) -> AuthorityDecisionCommit:
        task = await self.store.get_task(task_id)
        if task is None:
            raise ValueError(f"Unknown task_id: {task_id}")
        task = await self._hydrate_task_activation_contract(task)
        request = self._task_authority_request(task)
        task_surface_intent = self._task_surface_authority_intent(task)
        if request is None:
            raise ValueError(f"Task {task_id} is missing authority_request payload.")
        boundary_class = task.authority_boundary_class or self._classify_authority_boundary(
            request.requested_paths
        )
        granted_paths = authority_decision.granted_paths or request.requested_paths
        if authority_decision.decision == "grant":
            if task_surface_intent is not None:
                if task_surface_intent.kind == TaskSurfaceAuthorityIntentKind.MUTATION:
                    task = replace(
                        self._apply_task_surface_mutation(
                            task=task,
                            actor_id=task_surface_intent.actor_id,
                            mutation_kind=task_surface_intent.mutation_kind,
                            reason=task_surface_intent.reason or authority_decision.reason,
                            target_task_id=task_surface_intent.target_task_id,
                        ),
                        authority_decision_payload=authority_decision.to_dict(),
                        authority_boundary_class=boundary_class,
                        authority_resume_target=task_surface_intent.actor_id,
                    )
                else:
                    task = self._apply_task_surface_field_write(
                        task=task,
                        authority_decision=authority_decision,
                        intent=task_surface_intent,
                        boundary_class=boundary_class,
                    )
            else:
                task = replace(
                    task,
                    status=TaskStatus.PENDING,
                    blocked_by=(),
                    owned_paths=_merge_strings(task.owned_paths, add=granted_paths),
                    authority_request_id=None,
                    authority_request_payload={},
                    authority_decision_payload=authority_decision.to_dict(),
                    authority_boundary_class=boundary_class,
                    authority_resume_target=request.worker_id,
                    authority_waiting_since=None,
                )
        elif authority_decision.decision == "deny":
            task = replace(
                task,
                status=TaskStatus.BLOCKED,
                owner_id=None,
                claim_session_id=None,
                claimed_at=None,
                claim_source=None,
                blocked_by=("authority.denied", authority_decision.request_id),
                authority_request_id=None,
                authority_request_payload={},
                authority_decision_payload=authority_decision.to_dict(),
                authority_boundary_class=boundary_class,
                authority_waiting_since=None,
                authority_resume_target=None,
            )
        elif authority_decision.decision == "reroute":
            if replacement_task is None:
                raise ValueError("Authority reroute requires replacement_task.")
            reroute_reason = (
                authority_decision.reason.strip()
                or authority_decision.summary.strip()
                or f"Authority rerouted task {task.task_id}."
            )
            authority_decision = replace(
                authority_decision,
                reason=reroute_reason,
                reroute_task_id=replacement_task.task_id,
            )
            task = replace(
                task,
                status=TaskStatus.CANCELLED,
                owner_id=None,
                claim_session_id=None,
                claimed_at=None,
                claim_source=None,
                blocked_by=(),
                authority_request_id=None,
                authority_request_payload={},
                authority_decision_payload=authority_decision.to_dict(),
                authority_boundary_class=boundary_class,
                authority_waiting_since=None,
                authority_resume_target=None,
                superseded_by_task_id=replacement_task.task_id,
                merged_into_task_id=None,
                surface_mutation=TaskSurfaceMutation(
                    kind=TaskSurfaceMutationKind.SUPERSEDED,
                    actor_id=actor_id,
                    reason=reroute_reason,
                    target_task_id=replacement_task.task_id,
                ),
            )
            replacement_task = replace(
                await self._hydrate_task_activation_contract(replacement_task),
                status=TaskStatus.PENDING,
                derived_from=replacement_task.derived_from or task.task_id,
                reason=reroute_reason,
                provenance=TaskProvenance(
                    kind=TaskProvenanceKind.REPLACEMENT,
                    source_task_id=task.task_id,
                    reason=reroute_reason,
                ),
                owned_paths=_merge_strings(replacement_task.owned_paths, add=request.requested_paths),
                authority_request_id=None,
                authority_request_payload={},
                authority_decision_payload=authority_decision.to_dict(),
                authority_boundary_class=boundary_class,
                authority_waiting_since=None,
                authority_resume_target=None,
                superseded_by_task_id=None,
                merged_into_task_id=None,
                surface_mutation=TaskSurfaceMutation(),
                blocked_by=(),
            )
            await self._validate_task_surface_mutation_authority(
                task=task,
                actor_id=actor_id,
                reason=authority_decision.reason,
                target_task_id=replacement_task.task_id,
            )
            await self._validate_task_submission_authority(replacement_task)
        elif authority_decision.decision == "escalate":
            task = replace(
                task,
                status=TaskStatus.WAITING_FOR_AUTHORITY,
                blocked_by=(),
                authority_request_id=request.request_id,
                authority_decision_payload=authority_decision.to_dict(),
                authority_boundary_class=boundary_class,
            )
        else:
            raise ValueError(f"Unsupported authority decision: {authority_decision.decision}")

        if task.group_id is None:
            raise ValueError(f"Task {task.task_id} is missing group scope for authority decision commit.")
        blackboard_entry = self._build_blackboard_entry(
            group_id=task.group_id,
            kind=BlackboardKind.LEADER_LANE,
            entry_kind=BlackboardEntryKind.DECISION,
            author_id=actor_id,
            lane_id=lane_id,
            team_id=team_id,
            task_id=task.task_id,
            summary=authority_decision.summary or f"Authority decision `{authority_decision.decision}` for task {task.task_id}.",
            payload={
                "event": "authority.decision",
                "authority_request": request.to_dict(),
                "authority_decision": authority_decision.to_dict(),
                **(
                    {"replacement_task_id": replacement_task.task_id}
                    if replacement_task is not None
                    else {}
                ),
            },
        )
        decision_recipient = (
            authority_decision.escalated_to
            if authority_decision.decision == "escalate"
            else request.worker_id
        )
        post_commit_outbox = (
            CoordinationOutboxRecord(
                subject="authority.escalated"
                if authority_decision.decision == "escalate"
                else "authority.decision",
                recipient=decision_recipient,
                sender=actor_id,
                payload={
                    "task_id": task.task_id,
                    "authority_request": request.to_dict(),
                    "authority_decision": authority_decision.to_dict(),
                    **(
                        {"replacement_task_id": replacement_task.task_id}
                        if replacement_task is not None
                        else {}
                    ),
                },
                metadata={
                    "group_id": task.group_id,
                    "lane_id": lane_id,
                    "team_id": team_id,
                },
            ),
        ) if decision_recipient else ()
        blackboard_entry.payload["coordination_outbox"] = [
            record.to_dict() for record in post_commit_outbox
        ]
        state = await self.store.get_delivery_state(_lane_delivery_id(objective_id, lane_id))
        delivery_state = (
            self._project_lane_delivery_state_for_authority_decision(
                state=state,
                team_id=team_id,
                task_id=task.task_id,
                worker_id=actor_id,
                blackboard_entry_id=blackboard_entry.entry_id,
                decision=authority_decision,
                task=task,
                task_surface_intent=task_surface_intent,
                replacement_task_id=replacement_task.task_id if replacement_task is not None else None,
            )
            if state is not None
            else None
        )
        worker_session_snapshot = await self._project_worker_session_snapshot(
            session_snapshot=session_snapshot,
        )
        await self.store.commit_authority_decision(
            AuthorityDecisionStoreCommit(
                task=task,
                authority_decision=authority_decision,
                blackboard_entry=blackboard_entry,
                delivery_state=delivery_state,
                agent_session=session_snapshot,
                worker_session=worker_session_snapshot,
                replacement_task=replacement_task,
                post_commit_outbox=post_commit_outbox,
            )
        )
        await self._persist_worker_session_coordination_fallback(worker_session_snapshot)
        task = await self._authoritative_task(task)
        blackboard_entry = await self._authoritative_blackboard_entry(blackboard_entry)
        delivery_state = await self._authoritative_delivery_state(delivery_state)
        session_snapshot = await self._authoritative_session_snapshot(session_snapshot)
        if replacement_task is not None:
            replacement_task = await self._authoritative_task(replacement_task)
        return AuthorityDecisionCommit(
            task=task,
            authority_decision=authority_decision,
            blackboard_entry=blackboard_entry,
            delivery_state=delivery_state,
            session_snapshot=session_snapshot,
            replacement_task=replacement_task,
            post_commit_outbox=post_commit_outbox,
        )

    async def commit_directed_task_receipt(
        self,
        *,
        objective_id: str,
        lane_id: str,
        team_id: str,
        task_id: str,
        worker_id: str,
        claim_source: str,
        directive_id: str,
        correlation_id: str | None,
        claim_session_id: str | None,
        claimed_at: str | None = None,
        mailbox_consumer: str | None = None,
        consumer_cursor: Mapping[str, object] | None = None,
        status_summary: str = "",
        receipt_type: str = "claim_materialized",
        session_snapshot: AgentSession | None = None,
    ) -> DirectedTaskReceiptCommit:
        task = await self._project_claimed_task(
            task_id=task_id,
            owner_id=worker_id,
            claim_source=claim_source,
            claim_session_id=claim_session_id,
            claimed_at=claimed_at,
        )
        if task is None:
            raise ValueError(f"Task {task_id} is no longer claimable for directed receipt commit.")
        if task.group_id is None:
            raise ValueError(f"Task {task.task_id} is missing group scope for directed receipt commit.")
        delivery_id = _lane_delivery_id(objective_id, lane_id)
        session_snapshot_id = session_snapshot.session_id if session_snapshot is not None else None
        blackboard_entry = self._build_blackboard_entry(
            group_id=task.group_id,
            kind=BlackboardKind.TEAM,
            entry_kind=BlackboardEntryKind.EXECUTION_REPORT,
            author_id=worker_id,
            lane_id=lane_id,
            team_id=team_id,
            task_id=task.task_id,
            summary=status_summary,
            payload={
                "event": "task.receipt",
                "receipt_type": receipt_type,
                "directive_id": directive_id,
                "claim_source": claim_source,
                "claim_session_id": claim_session_id,
                "consumer_cursor": dict(consumer_cursor or {}),
                "delivery_id": delivery_id,
                "correlation_id": correlation_id,
                **({"session_snapshot_id": session_snapshot_id} if session_snapshot_id is not None else {}),
                **({"status_summary": status_summary} if status_summary else {}),
            },
        )
        state = await self.store.get_delivery_state(_lane_delivery_id(objective_id, lane_id))
        delivery_state = (
            self._project_lane_delivery_state_for_teammate_event(
                state=state,
                team_id=team_id,
                task_id=task.task_id,
                worker_id=worker_id,
                event_kind="receipt",
                blackboard_entry_id=blackboard_entry.entry_id,
                claim_session_id=task.claim_session_id or claim_session_id,
                claim_source=task.claim_source or claim_source,
                receipt_type=receipt_type,
                session_snapshot_id=session_snapshot_id,
                consumer_cursor=consumer_cursor,
            )
            if state is not None
            else None
        )
        protocol_bus_cursor = None
        if mailbox_consumer is not None and consumer_cursor is not None:
            protocol_bus_cursor = _StoreProtocolBusCursorCommit(
                stream="mailbox",
                consumer=mailbox_consumer,
                cursor={
                    str(key): (str(value) if value is not None else None)
                    for key, value in dict(consumer_cursor).items()
                },
            )
        receipt = DirectedTaskReceipt(
            directive_id=directive_id,
            receipt_type=receipt_type,
            task_id=task.task_id,
            claim_session_id=task.claim_session_id or claim_session_id,
            consumer_cursor=dict(consumer_cursor or {}),
            delivery_id=delivery_id,
            status_summary=status_summary,
            correlation_id=correlation_id,
            metadata={
                "blackboard_entry_id": blackboard_entry.entry_id,
                **({"session_snapshot_id": session_snapshot_id} if session_snapshot_id is not None else {}),
            },
        )
        post_commit_outbox = (
            CoordinationOutboxRecord(
                subject="task.receipt",
                recipient=f"leader:{lane_id}",
                sender=worker_id,
                payload=receipt.to_payload(),
                metadata={
                    "group_id": task.group_id,
                    "lane_id": lane_id,
                    "team_id": team_id,
                    "blackboard_entry_id": blackboard_entry.entry_id,
                },
            ),
        )
        blackboard_entry.payload["coordination_outbox"] = [
            record.to_dict() for record in post_commit_outbox
        ]
        worker_session_snapshot = await self._project_worker_session_snapshot(
            session_snapshot=session_snapshot,
            mailbox_cursor=consumer_cursor,
        )
        store_commit = _StoreDirectedTaskReceiptCommit(
            task=task,
            receipt=receipt,
            blackboard_entry=blackboard_entry,
            delivery_state=delivery_state,
            protocol_bus_cursor=protocol_bus_cursor,
            session_snapshot=session_snapshot,
            worker_session_snapshot=worker_session_snapshot,
            post_commit_outbox=post_commit_outbox,
        )
        if await self._commit_directed_task_receipt_via_store(store_commit):
            await self._persist_worker_session_coordination_fallback(worker_session_snapshot)
            task = await self._authoritative_task(task)
            blackboard_entry = await self._authoritative_blackboard_entry(blackboard_entry)
            delivery_state = await self._authoritative_delivery_state(delivery_state)
            protocol_cursor = await self._authoritative_protocol_bus_cursor(protocol_bus_cursor)
            session_snapshot = await self._authoritative_session_snapshot(session_snapshot)
        else:
            persisted_task = await self.claim_task(
                task_id=task.task_id,
                owner_id=worker_id,
                claim_source=claim_source,
                claim_session_id=task.claim_session_id,
                claimed_at=task.claimed_at,
            )
            if persisted_task is None:
                raise ValueError(f"Task {task.task_id} could not be claimed during directed receipt fallback commit.")
            task = persisted_task
            await self.store.save_blackboard_entry(blackboard_entry)
            if delivery_state is not None:
                await self.store.save_delivery_state(delivery_state)
            if protocol_bus_cursor is not None:
                await self.store.save_protocol_bus_cursor(
                    stream=protocol_bus_cursor.stream,
                    consumer=protocol_bus_cursor.consumer,
                    cursor=dict(protocol_bus_cursor.cursor),
                )
            if worker_session_snapshot is not None:
                await self.store.save_worker_session(worker_session_snapshot)
            if session_snapshot is not None:
                session_host = self._session_host()
                if session_host is not None:
                    session_snapshot = await session_host.save_session(session_snapshot)
                else:
                    await self.store.save_agent_session(session_snapshot)
            protocol_cursor = (
                dict(protocol_bus_cursor.cursor)
                if protocol_bus_cursor is not None
                else None
            )
        return DirectedTaskReceiptCommit(
            task=task,
            receipt=receipt,
            blackboard_entry=blackboard_entry,
            delivery_state=delivery_state,
            session_snapshot=session_snapshot,
            protocol_bus_cursor=protocol_cursor,
            post_commit_outbox=post_commit_outbox,
        )

    async def commit_teammate_result(
        self,
        *,
        objective_id: str,
        lane_id: str,
        team_id: str,
        task_id: str,
        worker_id: str,
        record: WorkerRecord,
        session_snapshot: AgentSession | None = None,
    ) -> TeammateResultCommit:
        task_status = (
            TaskStatus.COMPLETED
            if record.status == WorkerStatus.COMPLETED
            else TaskStatus.FAILED
        )
        task = await self._project_task_status_update(
            task_id=task_id,
            status=task_status,
            actor_id=worker_id,
        )
        if task.group_id is None:
            raise ValueError(f"Task {task.task_id} is missing group scope for teammate result commit.")
        session_snapshot_id = session_snapshot.session_id if session_snapshot is not None else None
        worker_session_id = record.session.session_id if record.session is not None else None
        if record.status == WorkerStatus.COMPLETED:
            blackboard_entry = self._build_blackboard_entry(
                group_id=task.group_id,
                kind=BlackboardKind.TEAM,
                entry_kind=BlackboardEntryKind.EXECUTION_REPORT,
                author_id=worker_id,
                lane_id=lane_id,
                team_id=team_id,
                task_id=task.task_id,
                summary=f"Teammate {worker_id} completed task {task.task_id}.",
                payload={
                    "event": "task.result",
                    "worker_status": record.status.value,
                    "claim_session_id": task.claim_session_id,
                    "claim_source": task.claim_source,
                    "output_text": record.output_text,
                    "session_snapshot_id": session_snapshot_id,
                    "worker_session_id": worker_session_id,
                },
            )
        else:
            blackboard_entry = self._build_blackboard_entry(
                group_id=task.group_id,
                kind=BlackboardKind.TEAM,
                entry_kind=BlackboardEntryKind.BLOCKER,
                author_id=worker_id,
                lane_id=lane_id,
                team_id=team_id,
                task_id=task.task_id,
                summary=f"Teammate {worker_id} failed task {task.task_id}.",
                payload={
                    "event": "task.result",
                    "worker_status": record.status.value,
                    "claim_session_id": task.claim_session_id,
                    "claim_source": task.claim_source,
                    "error_text": record.error_text,
                    "session_snapshot_id": session_snapshot_id,
                    "worker_session_id": worker_session_id,
                },
            )
        state = await self.store.get_delivery_state(_lane_delivery_id(objective_id, lane_id))
        delivery_state = (
            self._project_lane_delivery_state_for_teammate_event(
                state=state,
                team_id=team_id,
                task_id=task.task_id,
                worker_id=worker_id,
                event_kind="result",
                blackboard_entry_id=blackboard_entry.entry_id,
                claim_session_id=task.claim_session_id,
                claim_source=task.claim_source,
                worker_status=record.status,
                session_snapshot_id=session_snapshot_id,
                worker_session_id=worker_session_id,
            )
            if state is not None
            else None
        )
        result_payload = DirectedTaskResult(
            task_id=task.task_id,
            status=record.status.value,
            summary=record.output_text or record.error_text,
            artifact_refs=(f"blackboard:{blackboard_entry.entry_id}",),
            compat_task_id=task.task_id,
            metadata={
                "blackboard_entry_id": blackboard_entry.entry_id,
                **({"session_snapshot_id": session_snapshot_id} if session_snapshot_id is not None else {}),
                **({"worker_session_id": worker_session_id} if worker_session_id is not None else {}),
            },
        ).to_payload()
        post_commit_outbox = (
            CoordinationOutboxRecord(
                subject="task.result",
                recipient=f"leader:{lane_id}",
                sender=worker_id,
                payload=result_payload,
                metadata={
                    "group_id": task.group_id,
                    "lane_id": lane_id,
                    "team_id": team_id,
                    "blackboard_entry_id": blackboard_entry.entry_id,
                },
            ),
        )
        blackboard_entry.payload["coordination_outbox"] = [
            record.to_dict() for record in post_commit_outbox
        ]
        worker_session_snapshot = await self._project_worker_session_snapshot(
            session_snapshot=session_snapshot,
            worker_session=record.session,
        )
        store_commit = _StoreTeammateResultCommit(
            task=task,
            blackboard_entry=blackboard_entry,
            delivery_state=delivery_state,
            session_snapshot=session_snapshot,
            worker_session_snapshot=worker_session_snapshot,
            post_commit_outbox=post_commit_outbox,
        )
        if await self._commit_teammate_result_via_store(store_commit):
            await self._persist_worker_session_coordination_fallback(worker_session_snapshot)
            task = await self._authoritative_task(task)
            blackboard_entry = await self._authoritative_blackboard_entry(blackboard_entry)
            delivery_state = await self._authoritative_delivery_state(delivery_state)
            session_snapshot = await self._authoritative_session_snapshot(session_snapshot)
        else:
            await self.store.save_task(task)
            await self.store.save_blackboard_entry(blackboard_entry)
            if delivery_state is not None:
                await self.store.save_delivery_state(delivery_state)
            if worker_session_snapshot is not None:
                await self.store.save_worker_session(worker_session_snapshot)
            if session_snapshot is not None:
                session_host = self._session_host()
                if session_host is not None:
                    session_snapshot = await session_host.save_session(session_snapshot)
                else:
                    await self.store.save_agent_session(session_snapshot)
        return TeammateResultCommit(
            task=task,
            blackboard_entry=blackboard_entry,
            delivery_state=delivery_state,
            session_snapshot=session_snapshot,
            post_commit_outbox=post_commit_outbox,
        )

    def _default_claim_session_id(
        self,
        *,
        owner_id: str,
        claim_source: str,
        claimed_at: str,
        task_id: str | None = None,
        group_id: str | None = None,
        team_id: str | None = None,
        lane_id: str | None = None,
    ) -> str:
        if task_id is not None:
            return f"{claim_source}:{owner_id}:{task_id}:{claimed_at}"
        if group_id is not None:
            return (
                f"{claim_source}:{owner_id}:{group_id}:{team_id or '*'}:{lane_id or '*'}:{claimed_at}"
            )
        return f"{claim_source}:{owner_id}:{claimed_at}"

    async def list_visible_tasks(
        self,
        *,
        group_id: str,
        viewer_role: str,
        lane_id: str | None = None,
        team_id: str | None = None,
    ) -> list[TaskCard]:
        tasks = await self.store.list_tasks(group_id)
        tasks = await self._hydrate_task_activation_contracts(tasks)
        if viewer_role == "superleader":
            return tasks
        if viewer_role == "leader":
            return [
                task
                for task in tasks
                if (task.scope == TaskScope.LEADER_LANE and task.lane == lane_id)
                or (task.scope == TaskScope.TEAM and task.team_id == team_id)
            ]
        if viewer_role == "teammate":
            return [task for task in tasks if task.scope == TaskScope.TEAM and task.team_id == team_id]
        raise ValueError(f"Unknown viewer_role: {viewer_role}")

    def authorize_message_subscription(
        self,
        *,
        group_id: str,
        viewer_role: str,
        subscription: MailboxSubscription,
        viewer_lane_id: str | None = None,
        viewer_team_id: str | None = None,
    ) -> MailboxSubscription:
        if subscription.group_id is not None and subscription.group_id != group_id:
            raise PermissionError("Subscription group_id is outside the viewer's group scope.")
        if viewer_role == "superleader":
            return subscription
        if viewer_role == "leader":
            if subscription.team_id is not None and subscription.team_id != viewer_team_id:
                raise PermissionError("Leader cannot subscribe to another team's message surface.")
            if subscription.lane_id is not None and viewer_lane_id is not None and subscription.lane_id != viewer_lane_id:
                raise PermissionError("Leader cannot subscribe outside the current lane.")
            return subscription
        if viewer_role == "teammate":
            if subscription.team_id is not None and subscription.team_id != viewer_team_id:
                raise PermissionError("Teammate cannot subscribe to another team's message surface.")
            return subscription
        raise ValueError(f"Unknown viewer_role: {viewer_role}")

    def can_view_message_full_text(
        self,
        *,
        viewer_role: str,
        digest: MailboxDigest,
        viewer_lane_id: str | None = None,
        viewer_team_id: str | None = None,
    ) -> bool:
        if viewer_role == "superleader":
            return True
        if viewer_role == "leader":
            if viewer_team_id is not None and digest.team_id == viewer_team_id:
                return True
            if viewer_lane_id is not None and digest.lane_id == viewer_lane_id:
                return True
            return False
        if viewer_role == "teammate":
            return viewer_team_id is not None and digest.team_id == viewer_team_id
        raise ValueError(f"Unknown viewer_role: {viewer_role}")

    def can_route_cross_scope_directive(
        self,
        *,
        sender_role: str,
        recipient_role: str,
        sender_team_id: str | None = None,
        recipient_team_id: str | None = None,
    ) -> bool:
        if sender_role == "superleader":
            return recipient_role == "leader"
        if sender_role == "leader":
            if recipient_role != "teammate":
                return False
            return sender_team_id is not None and sender_team_id == recipient_team_id
        if sender_role == "teammate":
            return False
        raise ValueError(f"Unknown sender_role: {sender_role}")

    async def record_handoff(
        self,
        *,
        group_id: str,
        from_team_id: str,
        to_team_id: str,
        task_id: str,
        artifact_refs: tuple[str, ...] = (),
        summary: str = "",
        contract_assertions: tuple[str, ...] = (),
        verification_summary: dict[str, object] | None = None,
    ) -> HandoffRecord:
        return await self.team(group_id=group_id, team_id=from_team_id).record_handoff(
            to_team_id=to_team_id,
            task_id=task_id,
            artifact_refs=artifact_refs,
            summary=summary,
            contract_assertions=contract_assertions,
            verification_summary=verification_summary,
        )

    async def reduce_group(self, group_id: str) -> AuthorityState:
        handoffs = await self.store.list_handoffs(group_id)
        state = await self.reducer.apply(group_id, handoffs)
        await self.store.save_authority_state(state)
        await self.bus.publish(OrchestraEvent.authority_updated(group_id, state.accepted_handoffs))
        return state

    async def append_blackboard_entry(
        self,
        *,
        group_id: str,
        kind: BlackboardKind,
        entry_kind: BlackboardEntryKind,
        author_id: str,
        summary: str,
        lane_id: str | None = None,
        team_id: str | None = None,
        task_id: str | None = None,
        payload: dict[str, object] | None = None,
        created_at: str | None = None,
    ) -> BlackboardEntry:
        entry = self._build_blackboard_entry(
            group_id=group_id,
            kind=kind,
            entry_kind=entry_kind,
            author_id=author_id,
            summary=summary,
            lane_id=lane_id,
            team_id=team_id,
            task_id=task_id,
            payload=payload,
            created_at=created_at,
        )
        await self.store.save_blackboard_entry(entry)
        return entry

    def _build_blackboard_entry(
        self,
        *,
        group_id: str,
        kind: BlackboardKind,
        entry_kind: BlackboardEntryKind,
        author_id: str,
        summary: str,
        lane_id: str | None = None,
        team_id: str | None = None,
        task_id: str | None = None,
        payload: dict[str, object] | None = None,
        created_at: str | None = None,
    ) -> BlackboardEntry:
        blackboard_id = _build_blackboard_id(
            group_id=group_id,
            kind=kind,
            lane_id=lane_id,
            team_id=team_id,
        )
        entry = BlackboardEntry(
            entry_id=make_blackboard_entry_id(),
            blackboard_id=blackboard_id,
            group_id=group_id,
            kind=kind,
            entry_kind=entry_kind,
            author_id=author_id,
            lane_id=lane_id,
            team_id=team_id,
            task_id=task_id,
            summary=summary,
            payload=payload or {},
            created_at=created_at,
        )
        return entry

    async def reduce_blackboard(
        self,
        *,
        group_id: str,
        kind: BlackboardKind,
        lane_id: str | None = None,
        team_id: str | None = None,
    ) -> BlackboardSnapshot:
        blackboard_id = _build_blackboard_id(
            group_id=group_id,
            kind=kind,
            lane_id=lane_id,
            team_id=team_id,
        )
        entries = await self.store.list_blackboard_entries(blackboard_id)
        reducer = (
            self.leader_lane_blackboard_reducer
            if kind == BlackboardKind.LEADER_LANE
            else self.team_blackboard_reducer
        )
        snapshot = await reducer.reduce(
            blackboard_id=blackboard_id,
            group_id=group_id,
            kind=kind,
            lane_id=lane_id,
            team_id=team_id,
            entries=entries,
        )
        await self.store.save_blackboard_snapshot(snapshot)
        return snapshot

    async def inspect_group(self, group_id: str) -> dict[str, object]:
        group = await self.store.get_group(group_id)
        teams = await self.store.list_teams(group_id)
        tasks = await self.store.list_tasks(group_id)
        objectives = [
            objective for objective in self.store.objectives.values() if objective.group_id == group_id
        ] if hasattr(self.store, "objectives") else []
        handoffs = await self.store.list_handoffs(group_id)
        authority_state = await self.store.get_authority_state(group_id)
        return {
            "group": group,
            "teams": teams,
            "objectives": objectives,
            "tasks": tasks,
            "handoffs": handoffs,
            "authority_state": authority_state,
        }

    def team(self, *, group_id: str, team_id: str) -> TeamRuntime:
        return TeamRuntime(group_id=group_id, team_id=team_id, store=self.store, bus=self.bus)
