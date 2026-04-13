from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from agent_orchestra.contracts.agent import AgentSession
from agent_orchestra.contracts.authority import AuthorityDecision, AuthorityState, ScopeExtensionRequest
from agent_orchestra.contracts.blackboard import BlackboardEntry, BlackboardSnapshot
from agent_orchestra.contracts.delivery import DeliveryState
from agent_orchestra.contracts.daemon import (
    AgentIncarnation,
    AgentSlot,
    ProviderRouteHealth,
    SessionAttachment,
    SlotHealthEvent,
)
from agent_orchestra.contracts.execution import WorkerRecord, WorkerSession
from agent_orchestra.contracts.handoff import HandoffRecord
from agent_orchestra.contracts.hierarchical_review import (
    CrossTeamLeaderReview,
    ReviewItemKind,
    ReviewItemRef,
    SuperLeaderSynthesis,
    TeamPositionReview,
)
from agent_orchestra.contracts.objective import ObjectiveSpec, SpecEdge, SpecNode
from agent_orchestra.contracts.planning_review import (
    ActivationGateDecision,
    LeaderDraftPlan,
    LeaderPeerReview,
    LeaderRevisedPlan,
    SuperLeaderGlobalReview,
)
from agent_orchestra.contracts.session_continuity import (
    ConversationHead,
    ResidentTeamShell,
    RuntimeGeneration,
    SessionEvent,
    WorkSession,
    WorkSessionMessage,
)
from agent_orchestra.contracts.session_memory import (
    AgentTurnRecord,
    ArtifactRef,
    SessionMemoryItem,
    ToolInvocationRecord,
)
from agent_orchestra.contracts.task import TaskCard
from agent_orchestra.contracts.task_review import TaskReviewRevision, TaskReviewSlot
from agent_orchestra.contracts.team import Group, Team


@dataclass(slots=True, frozen=True)
class ProtocolBusCursorCommit:
    stream: str
    consumer: str
    cursor: dict[str, Any]


@dataclass(slots=True, frozen=True)
class CoordinationOutboxRecord:
    subject: str
    recipient: str
    sender: str
    payload: dict[str, Any]
    metadata: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "subject": self.subject,
            "recipient": self.recipient,
            "sender": self.sender,
            "payload": dict(self.payload),
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "CoordinationOutboxRecord":
        return cls(
            subject=str(payload.get("subject", "")),
            recipient=str(payload.get("recipient", "")),
            sender=str(payload.get("sender", "")),
            payload={
                str(key): value
                for key, value in payload.get("payload", {}).items()
            }
            if isinstance(payload.get("payload"), Mapping)
            else {},
            metadata={
                str(key): value
                for key, value in payload.get("metadata", {}).items()
            }
            if isinstance(payload.get("metadata"), Mapping)
            else {},
        )


def _coerce_protocol_bus_cursor(value: object) -> ProtocolBusCursorCommit | None:
    if isinstance(value, ProtocolBusCursorCommit):
        return value
    stream = getattr(value, "stream", None)
    consumer = getattr(value, "consumer", None)
    cursor = getattr(value, "cursor", None)
    if not isinstance(stream, str) or not isinstance(consumer, str) or not isinstance(cursor, Mapping):
        return None
    return ProtocolBusCursorCommit(
        stream=stream,
        consumer=consumer,
        cursor={str(key): item for key, item in cursor.items()},
    )


def _coerce_worker_session(value: object) -> WorkerSession | None:
    if isinstance(value, WorkerSession):
        return WorkerSession.from_dict(value.to_dict())
    if isinstance(value, Mapping):
        try:
            return WorkerSession.from_dict(value)
        except Exception:
            return None
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        payload = to_dict()
        if isinstance(payload, Mapping):
            try:
                return WorkerSession.from_dict(payload)
            except Exception:
                return None
    return None


def _synchronize_worker_session_mailbox_cursor(
    worker_session: WorkerSession | None,
    protocol_bus_cursor: ProtocolBusCursorCommit | None,
) -> WorkerSession | None:
    if worker_session is None or protocol_bus_cursor is None:
        return worker_session
    synchronized = WorkerSession.from_dict(worker_session.to_dict())
    synchronized.mailbox_cursor = {
        str(key): value for key, value in protocol_bus_cursor.cursor.items()
    }
    return synchronized


def _coerce_outbox_record(value: object) -> CoordinationOutboxRecord | None:
    if isinstance(value, CoordinationOutboxRecord):
        return value
    if isinstance(value, Mapping):
        return CoordinationOutboxRecord.from_payload(value)
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        payload = to_dict()
        if isinstance(payload, Mapping):
            return CoordinationOutboxRecord.from_payload(payload)
    return None


@dataclass(slots=True, frozen=True)
class CoordinationTransactionStoreCommit:
    task_mutations: tuple[TaskCard, ...] = ()
    replacement_tasks: tuple[TaskCard, ...] = ()
    blackboard_entries: tuple[BlackboardEntry, ...] = ()
    delivery_snapshots: tuple[DeliveryState, ...] = ()
    mailbox_cursors: tuple[ProtocolBusCursorCommit, ...] = ()
    session_snapshots: tuple[AgentSession, ...] = ()
    worker_session_snapshots: tuple[WorkerSession, ...] = ()
    durable_outbox_records: tuple[CoordinationOutboxRecord, ...] = ()
    outbox_scope_id: str | None = None

    @property
    def task(self) -> TaskCard | None:
        return self.task_mutations[0] if self.task_mutations else None

    @property
    def replacement_task(self) -> TaskCard | None:
        return self.replacement_tasks[0] if self.replacement_tasks else None

    @property
    def blackboard_entry(self) -> BlackboardEntry | None:
        return self.blackboard_entries[0] if self.blackboard_entries else None

    @property
    def delivery_state(self) -> DeliveryState | None:
        return self.delivery_snapshots[0] if self.delivery_snapshots else None

    @property
    def protocol_bus_cursor(self) -> ProtocolBusCursorCommit | None:
        return self.mailbox_cursors[0] if self.mailbox_cursors else None

    @property
    def agent_session(self) -> AgentSession | None:
        return self.session_snapshots[0] if self.session_snapshots else None

    @property
    def worker_session(self) -> WorkerSession | None:
        return self.worker_session_snapshots[0] if self.worker_session_snapshots else None

    @property
    def session_snapshot(self) -> AgentSession | None:
        return self.agent_session

    @property
    def slot_session(self) -> AgentSession | None:
        return self.agent_session

    @property
    def outbox(self) -> tuple[CoordinationOutboxRecord, ...]:
        return self.durable_outbox_records

    @property
    def effective_outbox_scope_id(self) -> str | None:
        if self.outbox_scope_id:
            return self.outbox_scope_id
        if self.blackboard_entries:
            return self.blackboard_entries[0].entry_id
        if self.task_mutations:
            return self.task_mutations[0].task_id
        return None

    @classmethod
    def from_legacy_commit(cls, commit: object) -> "CoordinationTransactionStoreCommit":
        if isinstance(commit, cls):
            return commit
        converter = getattr(commit, "as_coordination_transaction", None)
        if callable(converter):
            converted = converter()
            if isinstance(converted, cls):
                return converted

        task = getattr(commit, "task", None)
        replacement_task = getattr(commit, "replacement_task", None)
        blackboard_entry = getattr(commit, "blackboard_entry", None)
        delivery_state = getattr(commit, "delivery_state", None)
        mailbox_cursor = _coerce_protocol_bus_cursor(getattr(commit, "protocol_bus_cursor", None))

        agent_session = getattr(commit, "agent_session", None)
        if agent_session is None:
            agent_session = getattr(commit, "session_snapshot", None)
        worker_session = getattr(commit, "worker_session", None)
        if worker_session is None:
            worker_session = getattr(commit, "worker_session_snapshot", None)

        durable_outbox_records: list[CoordinationOutboxRecord] = []
        raw_outbox = getattr(commit, "outbox", None)
        if raw_outbox is None:
            raw_outbox = getattr(commit, "post_commit_outbox", ())
        if isinstance(raw_outbox, (list, tuple)):
            for item in raw_outbox:
                record = _coerce_outbox_record(item)
                if record is not None:
                    durable_outbox_records.append(record)

        outbox_scope_id = getattr(commit, "outbox_scope_id", None)
        if not isinstance(outbox_scope_id, str) or not outbox_scope_id:
            if isinstance(blackboard_entry, BlackboardEntry):
                outbox_scope_id = blackboard_entry.entry_id
            elif isinstance(task, TaskCard):
                outbox_scope_id = task.task_id
            else:
                outbox_scope_id = None

        return cls(
            task_mutations=(task,) if isinstance(task, TaskCard) else (),
            replacement_tasks=(replacement_task,) if isinstance(replacement_task, TaskCard) else (),
            blackboard_entries=(blackboard_entry,) if isinstance(blackboard_entry, BlackboardEntry) else (),
            delivery_snapshots=(delivery_state,) if isinstance(delivery_state, DeliveryState) else (),
            mailbox_cursors=(mailbox_cursor,) if mailbox_cursor is not None else (),
            session_snapshots=(agent_session,) if isinstance(agent_session, AgentSession) else (),
            worker_session_snapshots=(worker_session_snapshot,)
            if (worker_session_snapshot := _coerce_worker_session(worker_session)) is not None
            else (),
            durable_outbox_records=tuple(durable_outbox_records),
            outbox_scope_id=outbox_scope_id,
        )


@dataclass(slots=True, frozen=True)
class SessionTransactionStoreCommit:
    work_sessions: tuple[WorkSession, ...] = ()
    runtime_generations: tuple[RuntimeGeneration, ...] = ()
    work_session_messages: tuple[WorkSessionMessage, ...] = ()
    conversation_heads: tuple[ConversationHead, ...] = ()
    session_events: tuple[SessionEvent, ...] = ()
    turn_records: tuple[AgentTurnRecord, ...] = ()
    tool_invocation_records: tuple[ToolInvocationRecord, ...] = ()
    artifact_refs: tuple[ArtifactRef, ...] = ()
    session_memory_items: tuple[SessionMemoryItem, ...] = ()
    resident_team_shells: tuple[ResidentTeamShell, ...] = ()

    @property
    def work_session(self) -> WorkSession | None:
        return self.work_sessions[0] if self.work_sessions else None

    @property
    def runtime_generation(self) -> RuntimeGeneration | None:
        return self.runtime_generations[0] if self.runtime_generations else None

    @property
    def work_session_message(self) -> WorkSessionMessage | None:
        return self.work_session_messages[0] if self.work_session_messages else None

    @property
    def conversation_head(self) -> ConversationHead | None:
        return self.conversation_heads[0] if self.conversation_heads else None

    @property
    def session_event(self) -> SessionEvent | None:
        return self.session_events[0] if self.session_events else None

    @property
    def turn_record(self) -> AgentTurnRecord | None:
        return self.turn_records[0] if self.turn_records else None

    @property
    def tool_invocation_record(self) -> ToolInvocationRecord | None:
        return self.tool_invocation_records[0] if self.tool_invocation_records else None

    @property
    def artifact_ref(self) -> ArtifactRef | None:
        return self.artifact_refs[0] if self.artifact_refs else None

    @property
    def session_memory_item(self) -> SessionMemoryItem | None:
        return self.session_memory_items[0] if self.session_memory_items else None

    @property
    def resident_team_shell(self) -> ResidentTeamShell | None:
        return self.resident_team_shells[0] if self.resident_team_shells else None


@dataclass(slots=True, frozen=True)
class DaemonTransactionStoreCommit:
    agent_slots: tuple[AgentSlot, ...] = ()
    agent_incarnations: tuple[AgentIncarnation, ...] = ()
    slot_health_events: tuple[SlotHealthEvent, ...] = ()
    session_attachments: tuple[SessionAttachment, ...] = ()
    provider_route_health_records: tuple[ProviderRouteHealth, ...] = ()

    @property
    def agent_slot(self) -> AgentSlot | None:
        return self.agent_slots[0] if self.agent_slots else None

    @property
    def agent_incarnation(self) -> AgentIncarnation | None:
        return self.agent_incarnations[0] if self.agent_incarnations else None

    @property
    def slot_health_event(self) -> SlotHealthEvent | None:
        return self.slot_health_events[0] if self.slot_health_events else None

    @property
    def session_attachment(self) -> SessionAttachment | None:
        return self.session_attachments[0] if self.session_attachments else None

    @property
    def provider_route_health(self) -> ProviderRouteHealth | None:
        if not self.provider_route_health_records:
            return None
        return self.provider_route_health_records[0]


@dataclass(slots=True, frozen=True)
class MailboxConsumeStoreCommit:
    recipient: str
    envelope_ids: tuple[str, ...]
    protocol_bus_cursor: ProtocolBusCursorCommit
    agent_session: AgentSession
    worker_session: WorkerSession | None = None

    @property
    def session_snapshot(self) -> AgentSession:
        return self.agent_session

    @property
    def slot_session(self) -> AgentSession:
        return self.agent_session

    def as_coordination_transaction(self) -> CoordinationTransactionStoreCommit:
        worker_session = _synchronize_worker_session_mailbox_cursor(
            self.worker_session,
            self.protocol_bus_cursor,
        )
        return CoordinationTransactionStoreCommit(
            mailbox_cursors=(self.protocol_bus_cursor,),
            session_snapshots=(self.agent_session,),
            worker_session_snapshots=(worker_session,) if worker_session is not None else (),
            outbox_scope_id=self.recipient,
        )


@dataclass(slots=True, frozen=True)
class DirectedTaskReceiptStoreCommit:
    task: TaskCard
    blackboard_entry: BlackboardEntry
    delivery_state: DeliveryState | None = None
    protocol_bus_cursor: ProtocolBusCursorCommit | None = None
    agent_session: AgentSession | None = None
    worker_session: WorkerSession | None = None
    post_commit_outbox: tuple[CoordinationOutboxRecord, ...] = ()

    @property
    def session_snapshot(self) -> AgentSession | None:
        return self.agent_session

    @property
    def slot_session(self) -> AgentSession | None:
        return self.agent_session

    @property
    def outbox(self) -> tuple[CoordinationOutboxRecord, ...]:
        return self.post_commit_outbox

    def as_coordination_transaction(self) -> CoordinationTransactionStoreCommit:
        worker_session = _synchronize_worker_session_mailbox_cursor(
            self.worker_session,
            self.protocol_bus_cursor,
        )
        return CoordinationTransactionStoreCommit(
            task_mutations=(self.task,),
            blackboard_entries=(self.blackboard_entry,),
            delivery_snapshots=(self.delivery_state,) if self.delivery_state is not None else (),
            mailbox_cursors=(self.protocol_bus_cursor,) if self.protocol_bus_cursor is not None else (),
            session_snapshots=(self.agent_session,) if self.agent_session is not None else (),
            worker_session_snapshots=(worker_session,) if worker_session is not None else (),
            durable_outbox_records=self.post_commit_outbox,
            outbox_scope_id=self.blackboard_entry.entry_id,
        )


@dataclass(slots=True, frozen=True)
class TeammateResultStoreCommit:
    task: TaskCard
    blackboard_entry: BlackboardEntry
    delivery_state: DeliveryState | None = None
    agent_session: AgentSession | None = None
    worker_session: WorkerSession | None = None
    post_commit_outbox: tuple[CoordinationOutboxRecord, ...] = ()

    @property
    def session_snapshot(self) -> AgentSession | None:
        return self.agent_session

    @property
    def slot_session(self) -> AgentSession | None:
        return self.agent_session

    @property
    def outbox(self) -> tuple[CoordinationOutboxRecord, ...]:
        return self.post_commit_outbox

    def as_coordination_transaction(self) -> CoordinationTransactionStoreCommit:
        return CoordinationTransactionStoreCommit(
            task_mutations=(self.task,),
            blackboard_entries=(self.blackboard_entry,),
            delivery_snapshots=(self.delivery_state,) if self.delivery_state is not None else (),
            session_snapshots=(self.agent_session,) if self.agent_session is not None else (),
            worker_session_snapshots=(self.worker_session,) if self.worker_session is not None else (),
            durable_outbox_records=self.post_commit_outbox,
            outbox_scope_id=self.blackboard_entry.entry_id,
        )


@dataclass(slots=True, frozen=True)
class AuthorityRequestStoreCommit:
    task: TaskCard
    authority_request: ScopeExtensionRequest
    blackboard_entry: BlackboardEntry
    delivery_state: DeliveryState | None = None
    agent_session: AgentSession | None = None
    worker_session: WorkerSession | None = None
    post_commit_outbox: tuple[CoordinationOutboxRecord, ...] = ()

    @property
    def session_snapshot(self) -> AgentSession | None:
        return self.agent_session

    @property
    def slot_session(self) -> AgentSession | None:
        return self.agent_session

    @property
    def outbox(self) -> tuple[CoordinationOutboxRecord, ...]:
        return self.post_commit_outbox

    def as_coordination_transaction(self) -> CoordinationTransactionStoreCommit:
        return CoordinationTransactionStoreCommit(
            task_mutations=(self.task,),
            blackboard_entries=(self.blackboard_entry,),
            delivery_snapshots=(self.delivery_state,) if self.delivery_state is not None else (),
            session_snapshots=(self.agent_session,) if self.agent_session is not None else (),
            worker_session_snapshots=(self.worker_session,) if self.worker_session is not None else (),
            durable_outbox_records=self.post_commit_outbox,
            outbox_scope_id=self.blackboard_entry.entry_id,
        )


@dataclass(slots=True, frozen=True)
class AuthorityDecisionStoreCommit:
    task: TaskCard
    authority_decision: AuthorityDecision
    blackboard_entry: BlackboardEntry
    delivery_state: DeliveryState | None = None
    agent_session: AgentSession | None = None
    worker_session: WorkerSession | None = None
    replacement_task: TaskCard | None = None
    post_commit_outbox: tuple[CoordinationOutboxRecord, ...] = ()

    @property
    def session_snapshot(self) -> AgentSession | None:
        return self.agent_session

    @property
    def slot_session(self) -> AgentSession | None:
        return self.agent_session

    @property
    def outbox(self) -> tuple[CoordinationOutboxRecord, ...]:
        return self.post_commit_outbox

    def as_coordination_transaction(self) -> CoordinationTransactionStoreCommit:
        return CoordinationTransactionStoreCommit(
            task_mutations=(self.task,),
            replacement_tasks=(self.replacement_task,) if self.replacement_task is not None else (),
            blackboard_entries=(self.blackboard_entry,),
            delivery_snapshots=(self.delivery_state,) if self.delivery_state is not None else (),
            session_snapshots=(self.agent_session,) if self.agent_session is not None else (),
            worker_session_snapshots=(self.worker_session,) if self.worker_session is not None else (),
            durable_outbox_records=self.post_commit_outbox,
            outbox_scope_id=self.blackboard_entry.entry_id,
        )


class OrchestrationStore(ABC):
    @abstractmethod
    async def save_group(self, group: Group) -> None:
        raise NotImplementedError

    @abstractmethod
    async def get_group(self, group_id: str) -> Group | None:
        raise NotImplementedError

    @abstractmethod
    async def save_team(self, team: Team) -> None:
        raise NotImplementedError

    @abstractmethod
    async def get_team(self, team_id: str) -> Team | None:
        raise NotImplementedError

    @abstractmethod
    async def list_teams(self, group_id: str) -> list[Team]:
        raise NotImplementedError

    @abstractmethod
    async def save_objective(self, objective: ObjectiveSpec) -> None:
        raise NotImplementedError

    @abstractmethod
    async def get_objective(self, objective_id: str) -> ObjectiveSpec | None:
        raise NotImplementedError

    @abstractmethod
    async def save_spec_node(self, node: SpecNode) -> None:
        raise NotImplementedError

    @abstractmethod
    async def list_spec_nodes(self, objective_id: str) -> list[SpecNode]:
        raise NotImplementedError

    @abstractmethod
    async def save_spec_edge(self, edge: SpecEdge) -> None:
        raise NotImplementedError

    @abstractmethod
    async def list_spec_edges(self, objective_id: str) -> list[SpecEdge]:
        raise NotImplementedError

    @abstractmethod
    async def save_task(self, task: TaskCard) -> None:
        raise NotImplementedError

    @abstractmethod
    async def get_task(self, task_id: str) -> TaskCard | None:
        raise NotImplementedError

    @abstractmethod
    async def list_tasks(
        self,
        group_id: str,
        team_id: str | None = None,
        *,
        lane_id: str | None = None,
        scope: str | None = None,
    ) -> list[TaskCard]:
        raise NotImplementedError

    @abstractmethod
    async def claim_task(
        self,
        *,
        task_id: str,
        owner_id: str,
        claim_session_id: str,
        claimed_at: str,
        claim_source: str,
    ) -> TaskCard | None:
        raise NotImplementedError

    @abstractmethod
    async def claim_next_task(
        self,
        *,
        group_id: str,
        owner_id: str,
        claim_session_id: str,
        claimed_at: str,
        claim_source: str,
        team_id: str | None = None,
        lane_id: str | None = None,
        scope: str | None = None,
    ) -> TaskCard | None:
        raise NotImplementedError

    @abstractmethod
    async def upsert_task_review_slot(
        self,
        slot: TaskReviewSlot,
        revision: TaskReviewRevision,
    ) -> None:
        raise NotImplementedError

    @abstractmethod
    async def list_task_review_slots(self, task_id: str) -> list[TaskReviewSlot]:
        raise NotImplementedError

    @abstractmethod
    async def list_task_review_revisions(
        self,
        task_id: str,
        reviewer_agent_id: str | None = None,
    ) -> list[TaskReviewRevision]:
        raise NotImplementedError

    @abstractmethod
    async def save_review_item(self, item: ReviewItemRef) -> None:
        raise NotImplementedError

    @abstractmethod
    async def get_review_item(self, item_id: str) -> ReviewItemRef | None:
        raise NotImplementedError

    @abstractmethod
    async def list_review_items(
        self,
        objective_id: str,
        *,
        item_kind: ReviewItemKind | None = None,
    ) -> list[ReviewItemRef]:
        raise NotImplementedError

    @abstractmethod
    async def save_team_position_review(self, review: TeamPositionReview) -> None:
        raise NotImplementedError

    @abstractmethod
    async def list_team_position_reviews(
        self,
        item_id: str,
        *,
        team_id: str | None = None,
    ) -> list[TeamPositionReview]:
        raise NotImplementedError

    @abstractmethod
    async def save_cross_team_leader_review(
        self,
        review: CrossTeamLeaderReview,
    ) -> None:
        raise NotImplementedError

    @abstractmethod
    async def list_cross_team_leader_reviews(
        self,
        item_id: str,
        *,
        reviewer_team_id: str | None = None,
        target_team_id: str | None = None,
    ) -> list[CrossTeamLeaderReview]:
        raise NotImplementedError

    @abstractmethod
    async def save_superleader_synthesis(
        self,
        synthesis: SuperLeaderSynthesis,
    ) -> None:
        raise NotImplementedError

    @abstractmethod
    async def get_superleader_synthesis(
        self,
        item_id: str,
    ) -> SuperLeaderSynthesis | None:
        raise NotImplementedError

    @abstractmethod
    async def save_leader_draft_plan(self, plan: LeaderDraftPlan) -> None:
        raise NotImplementedError

    @abstractmethod
    async def list_leader_draft_plans(
        self,
        objective_id: str,
        *,
        planning_round_id: str | None = None,
    ) -> list[LeaderDraftPlan]:
        raise NotImplementedError

    @abstractmethod
    async def save_leader_peer_review(self, review: LeaderPeerReview) -> None:
        raise NotImplementedError

    @abstractmethod
    async def list_leader_peer_reviews(
        self,
        objective_id: str,
        *,
        planning_round_id: str | None = None,
    ) -> list[LeaderPeerReview]:
        raise NotImplementedError

    @abstractmethod
    async def save_superleader_global_review(self, review: SuperLeaderGlobalReview) -> None:
        raise NotImplementedError

    @abstractmethod
    async def get_superleader_global_review(
        self,
        objective_id: str,
        *,
        planning_round_id: str | None = None,
    ) -> SuperLeaderGlobalReview | None:
        raise NotImplementedError

    @abstractmethod
    async def save_leader_revised_plan(self, plan: LeaderRevisedPlan) -> None:
        raise NotImplementedError

    @abstractmethod
    async def list_leader_revised_plans(
        self,
        objective_id: str,
        *,
        planning_round_id: str | None = None,
    ) -> list[LeaderRevisedPlan]:
        raise NotImplementedError

    @abstractmethod
    async def save_activation_gate_decision(self, decision: ActivationGateDecision) -> None:
        raise NotImplementedError

    @abstractmethod
    async def get_activation_gate_decision(
        self,
        objective_id: str,
        *,
        planning_round_id: str | None = None,
    ) -> ActivationGateDecision | None:
        raise NotImplementedError

    @abstractmethod
    async def save_handoff(self, handoff: HandoffRecord) -> None:
        raise NotImplementedError

    @abstractmethod
    async def list_handoffs(self, group_id: str) -> list[HandoffRecord]:
        raise NotImplementedError

    @abstractmethod
    async def save_authority_state(self, state: AuthorityState) -> None:
        raise NotImplementedError

    @abstractmethod
    async def get_authority_state(self, group_id: str) -> AuthorityState | None:
        raise NotImplementedError

    @abstractmethod
    async def save_blackboard_entry(self, entry: BlackboardEntry) -> None:
        raise NotImplementedError

    @abstractmethod
    async def list_blackboard_entries(self, blackboard_id: str) -> list[BlackboardEntry]:
        raise NotImplementedError

    @abstractmethod
    async def save_blackboard_snapshot(self, snapshot: BlackboardSnapshot) -> None:
        raise NotImplementedError

    @abstractmethod
    async def get_blackboard_snapshot(self, blackboard_id: str) -> BlackboardSnapshot | None:
        raise NotImplementedError

    @abstractmethod
    async def save_worker_record(self, record: WorkerRecord) -> None:
        raise NotImplementedError

    @abstractmethod
    async def get_worker_record(self, worker_id: str) -> WorkerRecord | None:
        raise NotImplementedError

    @abstractmethod
    async def list_worker_records(self) -> list[WorkerRecord]:
        raise NotImplementedError

    @abstractmethod
    async def save_agent_session(self, session: AgentSession) -> None:
        raise NotImplementedError

    @abstractmethod
    async def get_agent_session(self, session_id: str) -> AgentSession | None:
        raise NotImplementedError

    @abstractmethod
    async def list_agent_sessions(self) -> list[AgentSession]:
        raise NotImplementedError

    @abstractmethod
    async def save_worker_session(self, session: WorkerSession) -> None:
        raise NotImplementedError

    @abstractmethod
    async def get_worker_session(self, session_id: str) -> WorkerSession | None:
        raise NotImplementedError

    @abstractmethod
    async def list_worker_sessions(self) -> list[WorkerSession]:
        raise NotImplementedError

    @abstractmethod
    async def save_work_session(self, session: WorkSession) -> None:
        raise NotImplementedError

    @abstractmethod
    async def get_work_session(self, work_session_id: str) -> WorkSession | None:
        raise NotImplementedError

    @abstractmethod
    async def list_work_sessions(
        self,
        group_id: str,
        *,
        root_objective_id: str | None = None,
    ) -> list[WorkSession]:
        raise NotImplementedError

    @abstractmethod
    async def save_runtime_generation(self, generation: RuntimeGeneration) -> None:
        raise NotImplementedError

    @abstractmethod
    async def get_runtime_generation(
        self,
        runtime_generation_id: str,
    ) -> RuntimeGeneration | None:
        raise NotImplementedError

    @abstractmethod
    async def list_runtime_generations(
        self,
        work_session_id: str,
    ) -> list[RuntimeGeneration]:
        raise NotImplementedError

    @abstractmethod
    async def append_work_session_message(self, message: WorkSessionMessage) -> None:
        raise NotImplementedError

    @abstractmethod
    async def list_work_session_messages(
        self,
        work_session_id: str,
        *,
        runtime_generation_id: str | None = None,
    ) -> list[WorkSessionMessage]:
        raise NotImplementedError

    @abstractmethod
    async def save_conversation_head(self, head: ConversationHead) -> None:
        raise NotImplementedError

    @abstractmethod
    async def get_conversation_head(
        self,
        conversation_head_id: str,
    ) -> ConversationHead | None:
        raise NotImplementedError

    @abstractmethod
    async def list_conversation_heads(
        self,
        work_session_id: str,
        *,
        runtime_generation_id: str | None = None,
    ) -> list[ConversationHead]:
        raise NotImplementedError

    @abstractmethod
    async def append_session_event(self, event: SessionEvent) -> None:
        raise NotImplementedError

    @abstractmethod
    async def list_session_events(
        self,
        work_session_id: str,
        *,
        runtime_generation_id: str | None = None,
    ) -> list[SessionEvent]:
        raise NotImplementedError

    @abstractmethod
    async def find_latest_resumable_runtime_generation(
        self,
        work_session_id: str,
    ) -> RuntimeGeneration | None:
        raise NotImplementedError

    async def commit_session_transaction(
        self,
        commit: SessionTransactionStoreCommit,
    ) -> None:
        for work_session in commit.work_sessions:
            await self.save_work_session(work_session)
        for runtime_generation in commit.runtime_generations:
            await self.save_runtime_generation(runtime_generation)
        for message in commit.work_session_messages:
            await self.append_work_session_message(message)
        for conversation_head in commit.conversation_heads:
            await self.save_conversation_head(conversation_head)
        for session_event in commit.session_events:
            await self.append_session_event(session_event)
        for turn_record in commit.turn_records:
            await self.append_turn_record(turn_record)
        for tool_invocation_record in commit.tool_invocation_records:
            await self.append_tool_invocation_record(tool_invocation_record)
        for artifact_ref in commit.artifact_refs:
            await self.save_artifact_ref(artifact_ref)
        for session_memory_item in commit.session_memory_items:
            await self.save_session_memory_item(session_memory_item)
        for resident_team_shell in commit.resident_team_shells:
            await self.save_resident_team_shell(resident_team_shell)

    @abstractmethod
    async def append_turn_record(self, record: AgentTurnRecord) -> None:
        raise NotImplementedError

    @abstractmethod
    async def list_turn_records(
        self,
        work_session_id: str,
        *,
        runtime_generation_id: str | None = None,
        head_kind: str | None = None,
        scope_id: str | None = None,
        limit: int | None = None,
    ) -> list[AgentTurnRecord]:
        raise NotImplementedError

    @abstractmethod
    async def append_tool_invocation_record(
        self,
        record: ToolInvocationRecord,
    ) -> None:
        raise NotImplementedError

    @abstractmethod
    async def list_tool_invocation_records(
        self,
        work_session_id: str,
        *,
        runtime_generation_id: str | None = None,
        turn_record_id: str | None = None,
        limit: int | None = None,
    ) -> list[ToolInvocationRecord]:
        raise NotImplementedError

    @abstractmethod
    async def save_artifact_ref(self, artifact: ArtifactRef) -> None:
        raise NotImplementedError

    @abstractmethod
    async def list_artifact_refs(
        self,
        work_session_id: str,
        *,
        runtime_generation_id: str | None = None,
        turn_record_id: str | None = None,
        limit: int | None = None,
    ) -> list[ArtifactRef]:
        raise NotImplementedError

    @abstractmethod
    async def save_session_memory_item(self, item: SessionMemoryItem) -> None:
        raise NotImplementedError

    @abstractmethod
    async def list_session_memory_items(
        self,
        work_session_id: str,
        *,
        runtime_generation_id: str | None = None,
        head_kind: str | None = None,
        scope_id: str | None = None,
        include_archived: bool = False,
        limit: int | None = None,
    ) -> list[SessionMemoryItem]:
        raise NotImplementedError

    @abstractmethod
    async def save_resident_team_shell(self, shell: ResidentTeamShell) -> None:
        raise NotImplementedError

    @abstractmethod
    async def get_resident_team_shell(
        self,
        resident_team_shell_id: str,
    ) -> ResidentTeamShell | None:
        raise NotImplementedError

    @abstractmethod
    async def list_resident_team_shells(
        self,
        work_session_id: str,
    ) -> list[ResidentTeamShell]:
        raise NotImplementedError

    @abstractmethod
    async def find_latest_resident_team_shell(
        self,
        work_session_id: str,
    ) -> ResidentTeamShell | None:
        raise NotImplementedError

    @abstractmethod
    async def save_agent_slot(self, slot: AgentSlot) -> None:
        raise NotImplementedError

    @abstractmethod
    async def get_agent_slot(self, slot_id: str) -> AgentSlot | None:
        raise NotImplementedError

    @abstractmethod
    async def list_agent_slots(
        self,
        *,
        work_session_id: str | None = None,
        resident_team_shell_id: str | None = None,
    ) -> list[AgentSlot]:
        raise NotImplementedError

    @abstractmethod
    async def save_agent_incarnation(self, incarnation: AgentIncarnation) -> None:
        raise NotImplementedError

    @abstractmethod
    async def get_agent_incarnation(
        self,
        incarnation_id: str,
    ) -> AgentIncarnation | None:
        raise NotImplementedError

    @abstractmethod
    async def list_agent_incarnations(
        self,
        *,
        slot_id: str | None = None,
    ) -> list[AgentIncarnation]:
        raise NotImplementedError

    @abstractmethod
    async def append_slot_health_event(self, event: SlotHealthEvent) -> None:
        raise NotImplementedError

    @abstractmethod
    async def list_slot_health_events(
        self,
        slot_id: str,
        *,
        incarnation_id: str | None = None,
        limit: int | None = None,
    ) -> list[SlotHealthEvent]:
        raise NotImplementedError

    @abstractmethod
    async def save_session_attachment(self, attachment: SessionAttachment) -> None:
        raise NotImplementedError

    @abstractmethod
    async def get_session_attachment(
        self,
        attachment_id: str,
    ) -> SessionAttachment | None:
        raise NotImplementedError

    @abstractmethod
    async def list_session_attachments(
        self,
        work_session_id: str,
        *,
        resident_team_shell_id: str | None = None,
        include_closed: bool = True,
    ) -> list[SessionAttachment]:
        raise NotImplementedError

    @abstractmethod
    async def save_provider_route_health(self, route: ProviderRouteHealth) -> None:
        raise NotImplementedError

    @abstractmethod
    async def get_provider_route_health(
        self,
        route_key: str,
    ) -> ProviderRouteHealth | None:
        raise NotImplementedError

    @abstractmethod
    async def list_provider_route_health(
        self,
        *,
        role: str | None = None,
    ) -> list[ProviderRouteHealth]:
        raise NotImplementedError

    async def commit_daemon_transaction(
        self,
        commit: DaemonTransactionStoreCommit,
    ) -> None:
        for slot in commit.agent_slots:
            await self.save_agent_slot(slot)
        for incarnation in commit.agent_incarnations:
            await self.save_agent_incarnation(incarnation)
        for event in commit.slot_health_events:
            await self.append_slot_health_event(event)
        for attachment in commit.session_attachments:
            await self.save_session_attachment(attachment)
        for route in commit.provider_route_health_records:
            await self.save_provider_route_health(route)

    @abstractmethod
    async def list_reclaimable_worker_sessions(
        self,
        *,
        now: str,
        statuses: tuple[str, ...],
    ) -> list[WorkerSession]:
        raise NotImplementedError

    @abstractmethod
    async def reclaim_worker_session_lease(
        self,
        *,
        session_id: str,
        previous_lease_id: str | None,
        new_supervisor_id: str,
        new_lease_id: str,
        now: str,
        new_expires_at: str,
    ) -> WorkerSession | None:
        raise NotImplementedError

    @abstractmethod
    async def save_protocol_bus_cursor(
        self,
        *,
        stream: str,
        consumer: str,
        cursor: dict[str, Any],
    ) -> None:
        raise NotImplementedError

    @abstractmethod
    async def get_protocol_bus_cursor(
        self,
        *,
        stream: str,
        consumer: str,
    ) -> dict[str, Any] | None:
        raise NotImplementedError

    @abstractmethod
    async def commit_coordination_transaction(
        self,
        commit: CoordinationTransactionStoreCommit,
    ) -> None:
        raise NotImplementedError

    async def commit_mailbox_consume(
        self,
        commit: MailboxConsumeStoreCommit,
    ) -> None:
        await self.commit_coordination_transaction(
            CoordinationTransactionStoreCommit.from_legacy_commit(commit)
        )

    async def commit_directed_task_receipt(
        self,
        commit: DirectedTaskReceiptStoreCommit,
    ) -> None:
        await self.commit_coordination_transaction(
            CoordinationTransactionStoreCommit.from_legacy_commit(commit)
        )

    async def commit_teammate_result(
        self,
        commit: TeammateResultStoreCommit,
    ) -> None:
        await self.commit_coordination_transaction(
            CoordinationTransactionStoreCommit.from_legacy_commit(commit)
        )

    async def commit_authority_request(
        self,
        commit: AuthorityRequestStoreCommit,
    ) -> None:
        await self.commit_coordination_transaction(
            CoordinationTransactionStoreCommit.from_legacy_commit(commit)
        )

    async def commit_authority_decision(
        self,
        commit: AuthorityDecisionStoreCommit,
    ) -> None:
        await self.commit_coordination_transaction(
            CoordinationTransactionStoreCommit.from_legacy_commit(commit)
        )

    @abstractmethod
    async def list_coordination_outbox_records(self) -> list[CoordinationOutboxRecord]:
        raise NotImplementedError

    @abstractmethod
    async def save_delivery_state(self, state: DeliveryState) -> None:
        raise NotImplementedError

    @abstractmethod
    async def get_delivery_state(self, delivery_id: str) -> DeliveryState | None:
        raise NotImplementedError

    @abstractmethod
    async def list_delivery_states(self, objective_id: str) -> list[DeliveryState]:
        raise NotImplementedError
