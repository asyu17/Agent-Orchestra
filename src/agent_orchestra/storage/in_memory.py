from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from dataclasses import asdict, is_dataclass
from enum import Enum
from typing import Any

from agent_orchestra.contracts.agent import AgentSession
from agent_orchestra.contracts.authority import AuthorityState
from agent_orchestra.contracts.blackboard import BlackboardEntry, BlackboardSnapshot
from agent_orchestra.contracts.delivery import DeliveryState
from agent_orchestra.contracts.enums import TaskStatus
from agent_orchestra.contracts.execution import WorkerRecord, WorkerSession, WorkerSessionStatus
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
    RuntimeGenerationStatus,
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
from agent_orchestra.storage.base import (
    AuthorityDecisionStoreCommit,
    AuthorityRequestStoreCommit,
    CoordinationOutboxRecord,
    CoordinationTransactionStoreCommit,
    DirectedTaskReceiptStoreCommit,
    MailboxConsumeStoreCommit,
    OrchestrationStore,
    SessionTransactionStoreCommit,
    TeammateResultStoreCommit,
)


def _jsonify(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return _jsonify(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _jsonify(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonify(item) for item in value]
    return value


def _json_safe_copy(value: Any) -> Any:
    return json.loads(json.dumps(_jsonify(value), ensure_ascii=True, sort_keys=True))


def _normalize_worker_session_payload(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = _json_safe_copy(payload)
    if normalized.get("status") == WorkerSessionStatus.CLOSED.value:
        normalized["status"] = WorkerSessionStatus.ABANDONED.value
    return normalized


def _resident_team_shell_latest_key(shell: ResidentTeamShell) -> tuple[str, str, str, str]:
    return (
        shell.last_progress_at or "",
        shell.updated_at or "",
        shell.created_at or "",
        shell.resident_team_shell_id,
    )


class InMemoryOrchestrationStore(OrchestrationStore):
    def __init__(self) -> None:
        self.groups: dict[str, Group] = {}
        self.teams: dict[str, Team] = {}
        self.objectives: dict[str, ObjectiveSpec] = {}
        self.spec_nodes: dict[str, SpecNode] = {}
        self.spec_edges: dict[str, SpecEdge] = {}
        self.tasks: dict[str, TaskCard] = {}
        self.task_review_slots: dict[tuple[str, str], TaskReviewSlot] = {}
        self.task_review_revisions: dict[str, TaskReviewRevision] = {}
        self.review_items: dict[str, ReviewItemRef] = {}
        self.team_position_reviews: dict[str, TeamPositionReview] = {}
        self.cross_team_leader_reviews: dict[str, CrossTeamLeaderReview] = {}
        self.superleader_syntheses: dict[str, SuperLeaderSynthesis] = {}
        self.leader_draft_plans: dict[str, LeaderDraftPlan] = {}
        self.leader_peer_reviews: dict[str, LeaderPeerReview] = {}
        self.superleader_global_reviews: dict[tuple[str, str], SuperLeaderGlobalReview] = {}
        self.leader_revised_plans: dict[str, LeaderRevisedPlan] = {}
        self.activation_gate_decisions: dict[tuple[str, str], ActivationGateDecision] = {}
        self.handoffs: dict[str, HandoffRecord] = {}
        self.authority_states: dict[str, AuthorityState] = {}
        self.blackboard_entries: dict[str, BlackboardEntry] = {}
        self.blackboard_snapshots: dict[str, BlackboardSnapshot] = {}
        self.worker_records: dict[str, WorkerRecord] = {}
        self.agent_sessions: dict[str, AgentSession] = {}
        self.worker_sessions: dict[str, WorkerSession] = {}
        self.work_sessions: dict[str, WorkSession] = {}
        self.runtime_generations: dict[str, RuntimeGeneration] = {}
        self.work_session_messages: dict[str, WorkSessionMessage] = {}
        self.conversation_heads: dict[str, ConversationHead] = {}
        self.session_events: dict[str, SessionEvent] = {}
        self.turn_records: dict[str, AgentTurnRecord] = {}
        self.tool_invocation_records: dict[str, ToolInvocationRecord] = {}
        self.artifact_refs: dict[str, ArtifactRef] = {}
        self.session_memory_items: dict[str, SessionMemoryItem] = {}
        self.resident_team_shells: dict[str, ResidentTeamShell] = {}
        self.protocol_bus_cursors: dict[tuple[str, str], dict[str, Any]] = {}
        self.delivery_states: dict[str, DeliveryState] = {}
        self.coordination_outbox_records: list[CoordinationOutboxRecord] = []
        self._task_claim_lock = asyncio.Lock()
        self._task_review_lock = asyncio.Lock()
        self._hierarchical_review_lock = asyncio.Lock()
        self._coordination_commit_lock = asyncio.Lock()

    async def save_group(self, group: Group) -> None:
        self.groups[group.group_id] = group

    async def get_group(self, group_id: str) -> Group | None:
        return self.groups.get(group_id)

    async def save_team(self, team: Team) -> None:
        self.teams[team.team_id] = team

    async def get_team(self, team_id: str) -> Team | None:
        return self.teams.get(team_id)

    async def list_teams(self, group_id: str) -> list[Team]:
        return [team for team in self.teams.values() if team.group_id == group_id]

    async def save_objective(self, objective: ObjectiveSpec) -> None:
        self.objectives[objective.objective_id] = objective

    async def get_objective(self, objective_id: str) -> ObjectiveSpec | None:
        return self.objectives.get(objective_id)

    async def save_spec_node(self, node: SpecNode) -> None:
        self.spec_nodes[node.node_id] = node

    async def list_spec_nodes(self, objective_id: str) -> list[SpecNode]:
        return [node for node in self.spec_nodes.values() if node.objective_id == objective_id]

    async def save_spec_edge(self, edge: SpecEdge) -> None:
        self.spec_edges[edge.edge_id] = edge

    async def list_spec_edges(self, objective_id: str) -> list[SpecEdge]:
        return [edge for edge in self.spec_edges.values() if edge.objective_id == objective_id]

    async def save_task(self, task: TaskCard) -> None:
        self.tasks[task.task_id] = task

    async def get_task(self, task_id: str) -> TaskCard | None:
        return self.tasks.get(task_id)

    async def list_tasks(
        self,
        group_id: str,
        team_id: str | None = None,
        *,
        lane_id: str | None = None,
        scope: str | None = None,
    ) -> list[TaskCard]:
        tasks = [task for task in self.tasks.values() if task.group_id == group_id]
        if team_id is not None:
            tasks = [task for task in tasks if task.team_id == team_id]
        if lane_id is not None:
            tasks = [task for task in tasks if task.lane == lane_id]
        if scope is not None:
            tasks = [task for task in tasks if getattr(task.scope, "value", task.scope) == scope]
        return tasks

    async def claim_task(
        self,
        *,
        task_id: str,
        owner_id: str,
        claim_session_id: str,
        claimed_at: str,
        claim_source: str,
    ) -> TaskCard | None:
        async with self._task_claim_lock:
            task = self.tasks.get(task_id)
            if task is None or not self._is_claimable(task):
                return None
            self._apply_claim(
                task,
                owner_id=owner_id,
                claim_session_id=claim_session_id,
                claimed_at=claimed_at,
                claim_source=claim_source,
            )
            return task

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
        async with self._task_claim_lock:
            candidates = [task for task in self.tasks.values() if task.group_id == group_id]
            if team_id is not None:
                candidates = [task for task in candidates if task.team_id == team_id]
            if lane_id is not None:
                candidates = [task for task in candidates if task.lane == lane_id]
            if scope is not None:
                candidates = [
                    task for task in candidates if getattr(task.scope, "value", task.scope) == scope
                ]
            candidates = [task for task in candidates if self._is_claimable(task)]
            if not candidates:
                return None
            task = min(candidates, key=lambda item: item.task_id)
            self._apply_claim(
                task,
                owner_id=owner_id,
                claim_session_id=claim_session_id,
                claimed_at=claimed_at,
                claim_source=claim_source,
            )
            return task

    async def upsert_task_review_slot(
        self,
        slot: TaskReviewSlot,
        revision: TaskReviewRevision,
    ) -> None:
        async with self._task_review_lock:
            stored_slot = TaskReviewSlot.from_payload(_json_safe_copy(slot.to_dict()))
            stored_revision = TaskReviewRevision.from_payload(_json_safe_copy(revision.to_dict()))
            if stored_slot is None or stored_revision is None:
                raise ValueError("Unable to persist task review slot or revision.")
            self.task_review_slots[(stored_slot.task_id, stored_slot.reviewer_agent_id)] = stored_slot
            self.task_review_revisions[stored_revision.revision_id] = stored_revision

    async def list_task_review_slots(self, task_id: str) -> list[TaskReviewSlot]:
        slots = [
            slot
            for key, slot in self.task_review_slots.items()
            if key[0] == task_id
        ]
        return [
            TaskReviewSlot.from_payload(_json_safe_copy(slot.to_dict()))
            for slot in sorted(slots, key=lambda item: item.reviewer_agent_id)
        ]

    async def list_task_review_revisions(
        self,
        task_id: str,
        reviewer_agent_id: str | None = None,
    ) -> list[TaskReviewRevision]:
        revisions = [
            revision
            for revision in self.task_review_revisions.values()
            if revision.task_id == task_id
            and (reviewer_agent_id is None or revision.reviewer_agent_id == reviewer_agent_id)
        ]
        return [
            TaskReviewRevision.from_payload(_json_safe_copy(revision.to_dict()))
            for revision in sorted(revisions, key=lambda item: (item.created_at, item.revision_id))
            if TaskReviewRevision.from_payload(_json_safe_copy(revision.to_dict())) is not None
        ]

    async def save_review_item(self, item: ReviewItemRef) -> None:
        async with self._hierarchical_review_lock:
            stored = ReviewItemRef.from_payload(_json_safe_copy(item.to_dict()))
            if stored is None:
                raise ValueError("Unable to persist review item.")
            self.review_items[stored.item_id] = stored

    async def get_review_item(self, item_id: str) -> ReviewItemRef | None:
        item = self.review_items.get(item_id)
        if item is None:
            return None
        return ReviewItemRef.from_payload(_json_safe_copy(item.to_dict()))

    async def list_review_items(
        self,
        objective_id: str,
        *,
        item_kind: ReviewItemKind | None = None,
    ) -> list[ReviewItemRef]:
        items = [
            item
            for item in self.review_items.values()
            if item.objective_id == objective_id
            and (item_kind is None or item.item_kind == item_kind)
        ]
        return [
            ReviewItemRef.from_payload(_json_safe_copy(item.to_dict()))
            for item in sorted(items, key=lambda current: current.item_id)
            if ReviewItemRef.from_payload(_json_safe_copy(item.to_dict())) is not None
        ]

    async def save_team_position_review(self, review: TeamPositionReview) -> None:
        async with self._hierarchical_review_lock:
            stored = TeamPositionReview.from_payload(_json_safe_copy(review.to_dict()))
            if stored is None:
                raise ValueError("Unable to persist team position review.")
            self.team_position_reviews[stored.position_review_id] = stored

    async def list_team_position_reviews(
        self,
        item_id: str,
        *,
        team_id: str | None = None,
    ) -> list[TeamPositionReview]:
        reviews = [
            review
            for review in self.team_position_reviews.values()
            if review.item_id == item_id and (team_id is None or review.team_id == team_id)
        ]
        return [
            TeamPositionReview.from_payload(_json_safe_copy(review.to_dict()))
            for review in sorted(reviews, key=lambda current: (current.reviewed_at, current.position_review_id))
            if TeamPositionReview.from_payload(_json_safe_copy(review.to_dict())) is not None
        ]

    async def save_cross_team_leader_review(
        self,
        review: CrossTeamLeaderReview,
    ) -> None:
        async with self._hierarchical_review_lock:
            stored = CrossTeamLeaderReview.from_payload(_json_safe_copy(review.to_dict()))
            if stored is None:
                raise ValueError("Unable to persist cross-team leader review.")
            self.cross_team_leader_reviews[stored.cross_review_id] = stored

    async def list_cross_team_leader_reviews(
        self,
        item_id: str,
        *,
        reviewer_team_id: str | None = None,
        target_team_id: str | None = None,
    ) -> list[CrossTeamLeaderReview]:
        reviews = [
            review
            for review in self.cross_team_leader_reviews.values()
            if review.item_id == item_id
            and (reviewer_team_id is None or review.reviewer_team_id == reviewer_team_id)
            and (target_team_id is None or review.target_team_id == target_team_id)
        ]
        return [
            CrossTeamLeaderReview.from_payload(_json_safe_copy(review.to_dict()))
            for review in sorted(reviews, key=lambda current: (current.reviewed_at, current.cross_review_id))
            if CrossTeamLeaderReview.from_payload(_json_safe_copy(review.to_dict())) is not None
        ]

    async def save_superleader_synthesis(
        self,
        synthesis: SuperLeaderSynthesis,
    ) -> None:
        async with self._hierarchical_review_lock:
            stored = SuperLeaderSynthesis.from_payload(_json_safe_copy(synthesis.to_dict()))
            if stored is None:
                raise ValueError("Unable to persist superleader synthesis.")
            self.superleader_syntheses[stored.item_id] = stored

    async def get_superleader_synthesis(
        self,
        item_id: str,
    ) -> SuperLeaderSynthesis | None:
        synthesis = self.superleader_syntheses.get(item_id)
        if synthesis is None:
            return None
        return SuperLeaderSynthesis.from_payload(_json_safe_copy(synthesis.to_dict()))

    async def save_leader_draft_plan(self, plan: LeaderDraftPlan) -> None:
        async with self._hierarchical_review_lock:
            stored = LeaderDraftPlan.from_payload(_json_safe_copy(plan.to_dict()))
            self.leader_draft_plans[stored.plan_id] = stored

    async def list_leader_draft_plans(
        self,
        objective_id: str,
        *,
        planning_round_id: str | None = None,
    ) -> list[LeaderDraftPlan]:
        plans = [
            plan
            for plan in self.leader_draft_plans.values()
            if plan.objective_id == objective_id
            and (planning_round_id is None or plan.planning_round_id == planning_round_id)
        ]
        plans.sort(key=lambda current: (current.planning_round_id, current.leader_id, current.plan_id))
        return [LeaderDraftPlan.from_payload(_json_safe_copy(plan.to_dict())) for plan in plans]

    async def save_leader_peer_review(self, review: LeaderPeerReview) -> None:
        async with self._hierarchical_review_lock:
            stored = LeaderPeerReview.from_payload(_json_safe_copy(review.to_dict()))
            self.leader_peer_reviews[stored.review_id] = stored

    async def list_leader_peer_reviews(
        self,
        objective_id: str,
        *,
        planning_round_id: str | None = None,
    ) -> list[LeaderPeerReview]:
        reviews = [
            review
            for review in self.leader_peer_reviews.values()
            if review.objective_id == objective_id
            and (planning_round_id is None or review.planning_round_id == planning_round_id)
        ]
        reviews.sort(
            key=lambda current: (
                current.planning_round_id,
                current.reviewer_leader_id,
                current.target_leader_id,
                current.review_id,
            )
        )
        return [LeaderPeerReview.from_payload(_json_safe_copy(review.to_dict())) for review in reviews]

    async def save_superleader_global_review(self, review: SuperLeaderGlobalReview) -> None:
        async with self._hierarchical_review_lock:
            stored = SuperLeaderGlobalReview.from_payload(_json_safe_copy(review.to_dict()))
            self.superleader_global_reviews[(stored.objective_id, stored.planning_round_id)] = stored

    async def get_superleader_global_review(
        self,
        objective_id: str,
        *,
        planning_round_id: str | None = None,
    ) -> SuperLeaderGlobalReview | None:
        if planning_round_id is not None:
            review = self.superleader_global_reviews.get((objective_id, planning_round_id))
            return (
                None
                if review is None
                else SuperLeaderGlobalReview.from_payload(_json_safe_copy(review.to_dict()))
            )
        matches = [
            review
            for (stored_objective_id, _round_id), review in self.superleader_global_reviews.items()
            if stored_objective_id == objective_id
        ]
        if not matches:
            return None
        review = sorted(matches, key=lambda current: (current.planning_round_id, current.review_id))[-1]
        return SuperLeaderGlobalReview.from_payload(_json_safe_copy(review.to_dict()))

    async def save_leader_revised_plan(self, plan: LeaderRevisedPlan) -> None:
        async with self._hierarchical_review_lock:
            stored = LeaderRevisedPlan.from_payload(_json_safe_copy(plan.to_dict()))
            self.leader_revised_plans[stored.plan_id] = stored

    async def list_leader_revised_plans(
        self,
        objective_id: str,
        *,
        planning_round_id: str | None = None,
    ) -> list[LeaderRevisedPlan]:
        plans = [
            plan
            for plan in self.leader_revised_plans.values()
            if plan.objective_id == objective_id
            and (planning_round_id is None or plan.planning_round_id == planning_round_id)
        ]
        plans.sort(key=lambda current: (current.planning_round_id, current.leader_id, current.plan_id))
        return [LeaderRevisedPlan.from_payload(_json_safe_copy(plan.to_dict())) for plan in plans]

    async def save_activation_gate_decision(self, decision: ActivationGateDecision) -> None:
        async with self._hierarchical_review_lock:
            stored = ActivationGateDecision.from_payload(_json_safe_copy(decision.to_dict()))
            self.activation_gate_decisions[(stored.objective_id, stored.planning_round_id)] = stored

    async def get_activation_gate_decision(
        self,
        objective_id: str,
        *,
        planning_round_id: str | None = None,
    ) -> ActivationGateDecision | None:
        if planning_round_id is not None:
            decision = self.activation_gate_decisions.get((objective_id, planning_round_id))
            return (
                None
                if decision is None
                else ActivationGateDecision.from_payload(_json_safe_copy(decision.to_dict()))
            )
        matches = [
            decision
            for (stored_objective_id, _round_id), decision in self.activation_gate_decisions.items()
            if stored_objective_id == objective_id
        ]
        if not matches:
            return None
        decision = sorted(
            matches,
            key=lambda current: (current.planning_round_id, current.decision_id),
        )[-1]
        return ActivationGateDecision.from_payload(_json_safe_copy(decision.to_dict()))

    def _is_claimable(self, task: TaskCard) -> bool:
        return task.status == TaskStatus.PENDING and task.owner_id is None and not task.blocked_by

    def _apply_claim(
        self,
        task: TaskCard,
        *,
        owner_id: str,
        claim_session_id: str,
        claimed_at: str,
        claim_source: str,
    ) -> None:
        task.status = TaskStatus.IN_PROGRESS
        task.owner_id = owner_id
        task.claim_session_id = claim_session_id
        task.claimed_at = claimed_at
        task.claim_source = claim_source

    async def save_handoff(self, handoff: HandoffRecord) -> None:
        self.handoffs[handoff.handoff_id] = handoff

    async def list_handoffs(self, group_id: str) -> list[HandoffRecord]:
        return [handoff for handoff in self.handoffs.values() if handoff.group_id == group_id]

    async def save_authority_state(self, state: AuthorityState) -> None:
        self.authority_states[state.group_id] = state

    async def get_authority_state(self, group_id: str) -> AuthorityState | None:
        return self.authority_states.get(group_id)

    async def save_blackboard_entry(self, entry: BlackboardEntry) -> None:
        self.blackboard_entries[entry.entry_id] = entry

    async def list_blackboard_entries(self, blackboard_id: str) -> list[BlackboardEntry]:
        return [
            entry
            for entry in self.blackboard_entries.values()
            if entry.blackboard_id == blackboard_id
        ]

    async def save_blackboard_snapshot(self, snapshot: BlackboardSnapshot) -> None:
        self.blackboard_snapshots[snapshot.blackboard_id] = snapshot

    async def get_blackboard_snapshot(self, blackboard_id: str) -> BlackboardSnapshot | None:
        return self.blackboard_snapshots.get(blackboard_id)

    async def save_worker_record(self, record: WorkerRecord) -> None:
        self.worker_records[record.worker_id] = record

    async def get_worker_record(self, worker_id: str) -> WorkerRecord | None:
        return self.worker_records.get(worker_id)

    async def list_worker_records(self) -> list[WorkerRecord]:
        return list(self.worker_records.values())

    async def save_agent_session(self, session: AgentSession) -> None:
        self.agent_sessions[session.session_id] = AgentSession.from_dict(
            _json_safe_copy(session.to_dict())
        )

    async def get_agent_session(self, session_id: str) -> AgentSession | None:
        session = self.agent_sessions.get(session_id)
        if session is None:
            return None
        return AgentSession.from_dict(_json_safe_copy(session.to_dict()))

    async def list_agent_sessions(self) -> list[AgentSession]:
        return [
            AgentSession.from_dict(_json_safe_copy(session.to_dict()))
            for session in sorted(self.agent_sessions.values(), key=lambda item: item.session_id)
        ]

    async def save_worker_session(self, session: WorkerSession) -> None:
        payload = _normalize_worker_session_payload(session.to_dict())
        self.worker_sessions[session.session_id] = WorkerSession.from_dict(payload)

    async def get_worker_session(self, session_id: str) -> WorkerSession | None:
        session = self.worker_sessions.get(session_id)
        if session is None:
            return None
        return WorkerSession.from_dict(_normalize_worker_session_payload(session.to_dict()))

    async def list_worker_sessions(self) -> list[WorkerSession]:
        return [
            WorkerSession.from_dict(_normalize_worker_session_payload(session.to_dict()))
            for session in sorted(self.worker_sessions.values(), key=lambda item: item.session_id)
        ]

    async def save_work_session(self, session: WorkSession) -> None:
        self.work_sessions[session.work_session_id] = WorkSession.from_payload(
            _json_safe_copy(session.to_dict())
        )

    async def get_work_session(self, work_session_id: str) -> WorkSession | None:
        session = self.work_sessions.get(work_session_id)
        if session is None:
            return None
        return WorkSession.from_payload(_json_safe_copy(session.to_dict()))

    async def list_work_sessions(
        self,
        group_id: str,
        *,
        root_objective_id: str | None = None,
    ) -> list[WorkSession]:
        sessions = [
            session
            for session in self.work_sessions.values()
            if session.group_id == group_id
            and (root_objective_id is None or session.root_objective_id == root_objective_id)
        ]
        sessions.sort(key=lambda item: (item.created_at, item.work_session_id))
        return [WorkSession.from_payload(_json_safe_copy(session.to_dict())) for session in sessions]

    async def save_runtime_generation(self, generation: RuntimeGeneration) -> None:
        self.runtime_generations[generation.runtime_generation_id] = RuntimeGeneration.from_payload(
            _json_safe_copy(generation.to_dict())
        )

    async def get_runtime_generation(
        self,
        runtime_generation_id: str,
    ) -> RuntimeGeneration | None:
        generation = self.runtime_generations.get(runtime_generation_id)
        if generation is None:
            return None
        return RuntimeGeneration.from_payload(_json_safe_copy(generation.to_dict()))

    async def list_runtime_generations(
        self,
        work_session_id: str,
    ) -> list[RuntimeGeneration]:
        generations = [
            generation
            for generation in self.runtime_generations.values()
            if generation.work_session_id == work_session_id
        ]
        generations.sort(
            key=lambda item: (item.generation_index, item.created_at, item.runtime_generation_id)
        )
        return [
            RuntimeGeneration.from_payload(_json_safe_copy(generation.to_dict()))
            for generation in generations
        ]

    async def append_work_session_message(self, message: WorkSessionMessage) -> None:
        self.work_session_messages[message.message_id] = WorkSessionMessage.from_payload(
            _json_safe_copy(message.to_dict())
        )

    async def list_work_session_messages(
        self,
        work_session_id: str,
        *,
        runtime_generation_id: str | None = None,
    ) -> list[WorkSessionMessage]:
        messages = [
            message
            for message in self.work_session_messages.values()
            if message.work_session_id == work_session_id
            and (
                runtime_generation_id is None
                or message.runtime_generation_id == runtime_generation_id
            )
        ]
        messages.sort(key=lambda item: (item.created_at, item.message_id))
        return [
            WorkSessionMessage.from_payload(_json_safe_copy(message.to_dict()))
            for message in messages
        ]

    async def save_conversation_head(self, head: ConversationHead) -> None:
        self.conversation_heads[head.conversation_head_id] = ConversationHead.from_payload(
            _json_safe_copy(head.to_dict())
        )

    async def get_conversation_head(
        self,
        conversation_head_id: str,
    ) -> ConversationHead | None:
        head = self.conversation_heads.get(conversation_head_id)
        if head is None:
            return None
        return ConversationHead.from_payload(_json_safe_copy(head.to_dict()))

    async def list_conversation_heads(
        self,
        work_session_id: str,
        *,
        runtime_generation_id: str | None = None,
    ) -> list[ConversationHead]:
        heads = [
            head
            for head in self.conversation_heads.values()
            if head.work_session_id == work_session_id
            and (
                runtime_generation_id is None
                or head.runtime_generation_id == runtime_generation_id
            )
        ]
        heads.sort(
            key=lambda item: (
                item.runtime_generation_id,
                item.head_kind.value,
                item.scope_id or "",
                item.conversation_head_id,
            )
        )
        return [ConversationHead.from_payload(_json_safe_copy(head.to_dict())) for head in heads]

    async def append_session_event(self, event: SessionEvent) -> None:
        self.session_events[event.session_event_id] = SessionEvent.from_payload(
            _json_safe_copy(event.to_dict())
        )

    async def list_session_events(
        self,
        work_session_id: str,
        *,
        runtime_generation_id: str | None = None,
    ) -> list[SessionEvent]:
        events = [
            event
            for event in self.session_events.values()
            if event.work_session_id == work_session_id
            and (
                runtime_generation_id is None
                or event.runtime_generation_id == runtime_generation_id
            )
        ]
        events.sort(key=lambda item: (item.created_at, item.session_event_id))
        return [SessionEvent.from_payload(_json_safe_copy(event.to_dict())) for event in events]

    async def find_latest_resumable_runtime_generation(
        self,
        work_session_id: str,
    ) -> RuntimeGeneration | None:
        resumable_statuses = {
            RuntimeGenerationStatus.BOOTING,
            RuntimeGenerationStatus.ACTIVE,
            RuntimeGenerationStatus.QUIESCENT,
            RuntimeGenerationStatus.DETACHED,
        }
        generations = await self.list_runtime_generations(work_session_id)
        resumable = [
            generation for generation in generations if generation.status in resumable_statuses
        ]
        if not resumable:
            return None
        latest = max(
            resumable,
            key=lambda item: (item.generation_index, item.created_at, item.runtime_generation_id),
        )
        return RuntimeGeneration.from_payload(_json_safe_copy(latest.to_dict()))

    async def append_turn_record(self, record: AgentTurnRecord) -> None:
        self.turn_records[record.turn_record_id] = AgentTurnRecord.from_payload(
            _json_safe_copy(record.to_dict())
        )

    async def list_turn_records(
        self,
        work_session_id: str,
        *,
        runtime_generation_id: str | None = None,
        head_kind: str | None = None,
        scope_id: str | None = None,
        limit: int | None = None,
    ) -> list[AgentTurnRecord]:
        head_kind_value = getattr(head_kind, "value", head_kind)
        records = [
            record
            for record in self.turn_records.values()
            if record.work_session_id == work_session_id
            and (
                runtime_generation_id is None
                or record.runtime_generation_id == runtime_generation_id
            )
            and (
                head_kind_value is None
                or record.head_kind.value == str(head_kind_value)
            )
            and (scope_id is None or record.scope_id == scope_id)
        ]
        records.sort(key=lambda item: (item.created_at, item.turn_record_id))
        if limit is not None and limit >= 0:
            records = records[-limit:] if limit else []
        return [AgentTurnRecord.from_payload(_json_safe_copy(record.to_dict())) for record in records]

    async def append_tool_invocation_record(
        self,
        record: ToolInvocationRecord,
    ) -> None:
        self.tool_invocation_records[record.tool_invocation_id] = ToolInvocationRecord.from_payload(
            _json_safe_copy(record.to_dict())
        )

    async def list_tool_invocation_records(
        self,
        work_session_id: str,
        *,
        runtime_generation_id: str | None = None,
        turn_record_id: str | None = None,
        limit: int | None = None,
    ) -> list[ToolInvocationRecord]:
        records = [
            record
            for record in self.tool_invocation_records.values()
            if record.work_session_id == work_session_id
            and (
                runtime_generation_id is None
                or record.runtime_generation_id == runtime_generation_id
            )
            and (turn_record_id is None or record.turn_record_id == turn_record_id)
        ]
        records.sort(key=lambda item: (item.started_at, item.tool_invocation_id))
        if limit is not None and limit >= 0:
            records = records[-limit:] if limit else []
        return [
            ToolInvocationRecord.from_payload(_json_safe_copy(record.to_dict()))
            for record in records
        ]

    async def save_artifact_ref(self, artifact: ArtifactRef) -> None:
        self.artifact_refs[artifact.artifact_ref_id] = ArtifactRef.from_payload(
            _json_safe_copy(artifact.to_dict())
        )

    async def list_artifact_refs(
        self,
        work_session_id: str,
        *,
        runtime_generation_id: str | None = None,
        turn_record_id: str | None = None,
        limit: int | None = None,
    ) -> list[ArtifactRef]:
        artifacts = [
            artifact
            for artifact in self.artifact_refs.values()
            if artifact.work_session_id == work_session_id
            and (
                runtime_generation_id is None
                or artifact.runtime_generation_id == runtime_generation_id
            )
            and (turn_record_id is None or artifact.turn_record_id == turn_record_id)
        ]
        artifacts.sort(key=lambda item: (item.uri_or_path, item.artifact_ref_id))
        if limit is not None and limit >= 0:
            artifacts = artifacts[-limit:] if limit else []
        return [ArtifactRef.from_payload(_json_safe_copy(artifact.to_dict())) for artifact in artifacts]

    async def save_session_memory_item(self, item: SessionMemoryItem) -> None:
        self.session_memory_items[item.memory_item_id] = SessionMemoryItem.from_payload(
            _json_safe_copy(item.to_dict())
        )

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
        head_kind_value = getattr(head_kind, "value", head_kind)
        items = [
            item
            for item in self.session_memory_items.values()
            if item.work_session_id == work_session_id
            and (
                runtime_generation_id is None
                or item.runtime_generation_id == runtime_generation_id
            )
            and (
                head_kind_value is None
                or item.head_kind.value == str(head_kind_value)
            )
            and (scope_id is None or item.scope_id == scope_id)
            and (include_archived or item.archived_at is None)
        ]
        items.sort(key=lambda item: (item.created_at, item.memory_item_id))
        if limit is not None and limit >= 0:
            items = items[-limit:] if limit else []
        return [
            SessionMemoryItem.from_payload(_json_safe_copy(item.to_dict()))
            for item in items
        ]

    async def save_resident_team_shell(self, shell: ResidentTeamShell) -> None:
        payload = _json_safe_copy(shell.to_dict())
        existing = self.resident_team_shells.get(shell.resident_team_shell_id)
        if existing is not None and existing.created_at:
            payload["created_at"] = existing.created_at
        self.resident_team_shells[shell.resident_team_shell_id] = ResidentTeamShell.from_payload(payload)

    async def get_resident_team_shell(
        self,
        resident_team_shell_id: str,
    ) -> ResidentTeamShell | None:
        shell = self.resident_team_shells.get(resident_team_shell_id)
        if shell is None:
            return None
        return ResidentTeamShell.from_payload(_json_safe_copy(shell.to_dict()))

    async def list_resident_team_shells(
        self,
        work_session_id: str,
    ) -> list[ResidentTeamShell]:
        shells = [
            shell
            for shell in self.resident_team_shells.values()
            if shell.work_session_id == work_session_id
        ]
        shells.sort(key=lambda item: (item.created_at, item.resident_team_shell_id))
        return [ResidentTeamShell.from_payload(_json_safe_copy(shell.to_dict())) for shell in shells]

    async def find_latest_resident_team_shell(
        self,
        work_session_id: str,
    ) -> ResidentTeamShell | None:
        shells = await self.list_resident_team_shells(work_session_id)
        if not shells:
            return None
        latest = max(shells, key=_resident_team_shell_latest_key)
        return ResidentTeamShell.from_payload(_json_safe_copy(latest.to_dict()))

    async def list_reclaimable_worker_sessions(
        self,
        *,
        now: str,
        statuses: tuple[str, ...],
    ) -> list[WorkerSession]:
        status_set = {str(status) for status in statuses}
        reclaimable: list[WorkerSession] = []
        for session in self.worker_sessions.values():
            if session.status.value not in status_set:
                continue
            expires_at = session.supervisor_lease_expires_at
            if expires_at is None or expires_at >= now:
                continue
            reclaimable.append(
                WorkerSession.from_dict(_normalize_worker_session_payload(session.to_dict()))
            )
        reclaimable.sort(key=lambda item: item.session_id)
        return reclaimable

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
        session = self.worker_sessions.get(session_id)
        if session is None:
            return None
        if previous_lease_id is not None and session.supervisor_lease_id != previous_lease_id:
            return None
        if session.status not in (WorkerSessionStatus.ASSIGNED, WorkerSessionStatus.ACTIVE):
            return None
        expires_at = session.supervisor_lease_expires_at
        if expires_at is not None and expires_at >= now:
            return None

        updated_payload = _normalize_worker_session_payload(session.to_dict())
        updated_payload["supervisor_id"] = new_supervisor_id
        updated_payload["supervisor_lease_id"] = new_lease_id
        updated_payload["supervisor_lease_expires_at"] = new_expires_at
        updated_payload["last_active_at"] = now
        updated = WorkerSession.from_dict(updated_payload)
        self.worker_sessions[session_id] = updated
        return WorkerSession.from_dict(_normalize_worker_session_payload(updated.to_dict()))

    async def save_protocol_bus_cursor(
        self,
        *,
        stream: str,
        consumer: str,
        cursor: dict[str, Any],
    ) -> None:
        self.protocol_bus_cursors[(stream, consumer)] = _json_safe_copy(cursor)

    async def get_protocol_bus_cursor(
        self,
        *,
        stream: str,
        consumer: str,
    ) -> dict[str, Any] | None:
        value = self.protocol_bus_cursors.get((stream, consumer))
        if value is None:
            return None
        return _json_safe_copy(value)

    def _store_agent_session(self, session: AgentSession) -> None:
        self.agent_sessions[session.session_id] = AgentSession.from_dict(
            _json_safe_copy(session.to_dict())
        )

    async def _commit_coordination_state(
        self,
        commit: CoordinationTransactionStoreCommit,
    ) -> None:
        async with self._coordination_commit_lock:
            for task in commit.task_mutations:
                self.tasks[task.task_id] = task
            for task in commit.replacement_tasks:
                self.tasks[task.task_id] = task
            for cursor_commit in commit.mailbox_cursors:
                self.protocol_bus_cursors[(cursor_commit.stream, cursor_commit.consumer)] = (
                    _json_safe_copy(cursor_commit.cursor)
                )
            for blackboard_entry in commit.blackboard_entries:
                self.blackboard_entries[blackboard_entry.entry_id] = blackboard_entry
            for delivery_state in commit.delivery_snapshots:
                self.delivery_states[delivery_state.delivery_id] = delivery_state
            for agent_session in commit.session_snapshots:
                self._store_agent_session(agent_session)
            if commit.durable_outbox_records:
                self.coordination_outbox_records.extend(
                    CoordinationOutboxRecord.from_payload(
                        _json_safe_copy(record.to_dict())
                    )
                    for record in commit.durable_outbox_records
                )

    async def commit_session_transaction(
        self,
        commit: SessionTransactionStoreCommit,
    ) -> None:
        async with self._coordination_commit_lock:
            for work_session in commit.work_sessions:
                self.work_sessions[work_session.work_session_id] = WorkSession.from_payload(
                    _json_safe_copy(work_session.to_dict())
                )
            for runtime_generation in commit.runtime_generations:
                self.runtime_generations[runtime_generation.runtime_generation_id] = (
                    RuntimeGeneration.from_payload(
                        _json_safe_copy(runtime_generation.to_dict())
                    )
                )
            for message in commit.work_session_messages:
                self.work_session_messages[message.message_id] = WorkSessionMessage.from_payload(
                    _json_safe_copy(message.to_dict())
                )
            for conversation_head in commit.conversation_heads:
                self.conversation_heads[conversation_head.conversation_head_id] = (
                    ConversationHead.from_payload(
                        _json_safe_copy(conversation_head.to_dict())
                    )
                )
            for session_event in commit.session_events:
                self.session_events[session_event.session_event_id] = SessionEvent.from_payload(
                    _json_safe_copy(session_event.to_dict())
                )
            for turn_record in commit.turn_records:
                self.turn_records[turn_record.turn_record_id] = AgentTurnRecord.from_payload(
                    _json_safe_copy(turn_record.to_dict())
                )
            for tool_invocation_record in commit.tool_invocation_records:
                self.tool_invocation_records[tool_invocation_record.tool_invocation_id] = (
                    ToolInvocationRecord.from_payload(
                        _json_safe_copy(tool_invocation_record.to_dict())
                    )
                )
            for artifact_ref in commit.artifact_refs:
                self.artifact_refs[artifact_ref.artifact_ref_id] = ArtifactRef.from_payload(
                    _json_safe_copy(artifact_ref.to_dict())
                )
            for session_memory_item in commit.session_memory_items:
                self.session_memory_items[session_memory_item.memory_item_id] = (
                    SessionMemoryItem.from_payload(
                        _json_safe_copy(session_memory_item.to_dict())
                    )
                )
            for resident_shell in commit.resident_team_shells:
                payload = _json_safe_copy(resident_shell.to_dict())
                existing = self.resident_team_shells.get(resident_shell.resident_team_shell_id)
                if existing is not None and existing.created_at:
                    payload["created_at"] = existing.created_at
                self.resident_team_shells[resident_shell.resident_team_shell_id] = (
                    ResidentTeamShell.from_payload(payload)
                )

    async def commit_coordination_transaction(
        self,
        commit: CoordinationTransactionStoreCommit,
    ) -> None:
        await self._commit_coordination_state(commit)

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

    async def list_coordination_outbox_records(self) -> list[CoordinationOutboxRecord]:
        return [
            CoordinationOutboxRecord.from_payload(_json_safe_copy(record.to_dict()))
            for record in self.coordination_outbox_records
        ]

    async def save_delivery_state(self, state: DeliveryState) -> None:
        self.delivery_states[state.delivery_id] = state

    async def get_delivery_state(self, delivery_id: str) -> DeliveryState | None:
        return self.delivery_states.get(delivery_id)

    async def list_delivery_states(self, objective_id: str) -> list[DeliveryState]:
        return [state for state in self.delivery_states.values() if state.objective_id == objective_id]
