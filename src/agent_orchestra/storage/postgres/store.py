from __future__ import annotations

import inspect
import json
from datetime import datetime, timezone
from collections.abc import Callable, Mapping
from dataclasses import asdict, is_dataclass
from enum import Enum
from typing import Any

from agent_orchestra.contracts.agent import AgentSession
from agent_orchestra.contracts.authority import AuthorityState
from agent_orchestra.contracts.blackboard import BlackboardEntry, BlackboardSnapshot
from agent_orchestra.contracts.delivery import DeliveryState, DeliveryStateKind, DeliveryStatus
from agent_orchestra.contracts.daemon import (
    AgentIncarnation,
    AgentSlot,
    ProviderRouteHealth,
    SessionAttachment,
    SessionAttachmentStatus,
    SlotHealthEvent,
)
from agent_orchestra.contracts.enums import (
    AuthorityStatus,
    BlackboardEntryKind,
    BlackboardKind,
    SpecEdgeKind,
    SpecNodeKind,
    SpecNodeStatus,
    TaskScope,
    TaskStatus,
    WorkerStatus,
)
from agent_orchestra.contracts.execution import WorkerHandle, WorkerRecord, WorkerSession, WorkerSessionStatus
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
from agent_orchestra.contracts.task import TaskCard, TaskProvenance, TaskSurfaceMutation
from agent_orchestra.contracts.task_review import TaskReviewRevision, TaskReviewSlot
from agent_orchestra.contracts.team import Group, Team
from agent_orchestra.storage.base import (
    AuthorityDecisionStoreCommit,
    AuthorityRequestStoreCommit,
    CoordinationOutboxRecord,
    CoordinationTransactionStoreCommit,
    DaemonTransactionStoreCommit,
    DirectedTaskReceiptStoreCommit,
    MailboxConsumeStoreCommit,
    OrchestrationStore,
    SessionTransactionStoreCommit,
    TeammateResultStoreCommit,
)
from agent_orchestra.storage.postgres.models import schema_statements


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_iso_timestamp(value: object) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        datetime.fromisoformat(value)
    except ValueError:
        return False
    return True


def _normalized_required_timestamp(
    value: object,
    *,
    owner_kind: str,
    field_name: str,
) -> str:
    if value is None:
        return _now_iso()
    text = str(value).strip()
    if not text:
        return _now_iso()
    if not _is_iso_timestamp(text):
        raise ValueError(f"{owner_kind} {field_name} must be an ISO-8601 timestamp")
    return text


def _normalized_optional_timestamp(
    value: object,
    *,
    owner_kind: str,
    field_name: str,
) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if not _is_iso_timestamp(text):
        raise ValueError(f"{owner_kind} {field_name} must be an ISO-8601 timestamp")
    return text


def _normalize_param(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    return value


def _jsonify(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return _jsonify(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _jsonify(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonify(item) for item in value]
    return str(value)


def _json_param(value: Any) -> str:
    return json.dumps(_jsonify(value), ensure_ascii=True, sort_keys=True)


def _mailbox_cursor_param(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, Mapping):
        return json.dumps(_jsonify(value), ensure_ascii=True, sort_keys=True)
    if isinstance(value, (list, tuple)):
        return json.dumps(_jsonify(value), ensure_ascii=True, sort_keys=True)
    return str(value)


def _mailbox_cursor_value(value: Any) -> dict[str, Any] | str | None:
    if value is None:
        return None
    if isinstance(value, Mapping):
        return _mapping(value)
    if isinstance(value, str):
        trimmed = value.strip()
        if not trimmed:
            return None
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return value
        if isinstance(parsed, Mapping):
            return _mapping(parsed)
        return parsed
    return str(value)


def _json_load(value: Any, *, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, str):
        return json.loads(value)
    return value


def _string_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    result: list[str] = []
    for item in value:
        if item is None:
            continue
        result.append(str(item))
    return tuple(result)


def _mapping(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): item for key, item in value.items()}


def _json_safe_copy(value: Any) -> Any:
    return json.loads(json.dumps(_jsonify(value), ensure_ascii=True, sort_keys=True))


def _normalize_worker_session_payload(payload: Any) -> dict[str, Any]:
    normalized = _mapping(_json_safe_copy(_json_load(payload, default={})))
    if normalized.get("status") == WorkerSessionStatus.CLOSED.value:
        normalized["status"] = WorkerSessionStatus.ABANDONED.value
    return normalized


def _deserialize_group(row: tuple[Any, ...]) -> Group:
    return Group(
        group_id=str(row[0]),
        display_name=str(row[1]) if row[1] is not None else None,
        metadata=_mapping(_json_load(row[2], default={})),
    )


def _deserialize_team(row: tuple[Any, ...]) -> Team:
    return Team(
        team_id=str(row[0]),
        group_id=str(row[1]),
        name=str(row[2]),
        member_ids=_string_tuple(_json_load(row[3], default=[])),
        metadata=_mapping(_json_load(row[4], default={})),
    )


def _deserialize_objective(payload: Any) -> ObjectiveSpec:
    data = _mapping(_json_load(payload, default={}))
    return ObjectiveSpec(
        objective_id=str(data["objective_id"]),
        group_id=str(data["group_id"]),
        title=str(data["title"]),
        description=str(data["description"]),
        success_metrics=_string_tuple(data.get("success_metrics", ())),
        hard_constraints=_string_tuple(data.get("hard_constraints", ())),
        budget=_mapping(data.get("budget", {})),
        metadata=_mapping(data.get("metadata", {})),
    )


def _deserialize_spec_node(payload: Any) -> SpecNode:
    data = _mapping(_json_load(payload, default={}))
    return SpecNode(
        node_id=str(data["node_id"]),
        objective_id=str(data["objective_id"]),
        kind=SpecNodeKind(data["kind"]),
        title=str(data["title"]),
        summary=str(data.get("summary", "")),
        scope=TaskScope(data["scope"]),
        lane_id=str(data["lane_id"]) if data.get("lane_id") is not None else None,
        team_id=str(data["team_id"]) if data.get("team_id") is not None else None,
        created_by=str(data["created_by"]) if data.get("created_by") is not None else None,
        status=SpecNodeStatus(data.get("status", SpecNodeStatus.OPEN.value)),
        metadata=_mapping(data.get("metadata", {})),
    )


def _deserialize_spec_edge(payload: Any) -> SpecEdge:
    data = _mapping(_json_load(payload, default={}))
    return SpecEdge(
        edge_id=str(data["edge_id"]),
        objective_id=str(data["objective_id"]),
        kind=SpecEdgeKind(data["kind"]),
        from_node_id=str(data["from_node_id"]),
        to_node_id=str(data["to_node_id"]),
        metadata=_mapping(data.get("metadata", {})),
    )


def _deserialize_task(payload: Any) -> TaskCard:
    data = _mapping(_json_load(payload, default={}))
    return TaskCard(
        task_id=str(data["task_id"]),
        goal=str(data["goal"]),
        lane=str(data["lane"]),
        group_id=str(data["group_id"]) if data.get("group_id") is not None else None,
        team_id=str(data["team_id"]) if data.get("team_id") is not None else None,
        scope=TaskScope(data.get("scope", TaskScope.TEAM.value)),
        owned_paths=_string_tuple(data.get("owned_paths", ())),
        allowed_inputs=_string_tuple(data.get("allowed_inputs", ())),
        output_artifacts=_string_tuple(data.get("output_artifacts", ())),
        verification_commands=_string_tuple(data.get("verification_commands", ())),
        handoff_to=_string_tuple(data.get("handoff_to", ())),
        merge_target=str(data["merge_target"]) if data.get("merge_target") is not None else None,
        owner_id=str(data["owner_id"]) if data.get("owner_id") is not None else None,
        authority_request_id=(
            str(data["authority_request_id"])
            if data.get("authority_request_id") is not None
            else None
        ),
        authority_request_payload=_mapping(data.get("authority_request_payload", {})),
        authority_decision_payload=_mapping(data.get("authority_decision_payload", {})),
        authority_boundary_class=(
            str(data["authority_boundary_class"])
            if data.get("authority_boundary_class") is not None
            else None
        ),
        authority_waiting_since=(
            str(data["authority_waiting_since"])
            if data.get("authority_waiting_since") is not None
            else None
        ),
        authority_resume_target=(
            str(data["authority_resume_target"])
            if data.get("authority_resume_target") is not None
            else None
        ),
        superseded_by_task_id=(
            str(data["superseded_by_task_id"])
            if data.get("superseded_by_task_id") is not None
            else None
        ),
        merged_into_task_id=(
            str(data["merged_into_task_id"])
            if data.get("merged_into_task_id") is not None
            else None
        ),
        claim_session_id=str(data["claim_session_id"]) if data.get("claim_session_id") is not None else None,
        claimed_at=str(data["claimed_at"]) if data.get("claimed_at") is not None else None,
        claim_source=str(data["claim_source"]) if data.get("claim_source") is not None else None,
        blocked_by=_string_tuple(data.get("blocked_by", ())),
        created_by=str(data["created_by"]) if data.get("created_by") is not None else None,
        derived_from=str(data["derived_from"]) if data.get("derived_from") is not None else None,
        reason=str(data.get("reason", "")),
        provenance=TaskProvenance.from_payload(data.get("provenance")) or TaskProvenance(),
        surface_mutation=(
            TaskSurfaceMutation.from_payload(data.get("surface_mutation"))
            or TaskSurfaceMutation()
        ),
        protected_read_only_fields=_string_tuple(data.get("protected_read_only_fields", ())),
        status=TaskStatus(data.get("status", TaskStatus.PENDING.value)),
    )


def _deserialize_task_review_slot(payload: Any) -> TaskReviewSlot:
    slot = TaskReviewSlot.from_payload(_mapping(_json_load(payload, default={})))
    if slot is None:
        raise ValueError("Invalid task review slot payload.")
    return slot


def _deserialize_task_review_revision(payload: Any) -> TaskReviewRevision:
    revision = TaskReviewRevision.from_payload(_mapping(_json_load(payload, default={})))
    if revision is None:
        raise ValueError("Invalid task review revision payload.")
    return revision


def _deserialize_review_item(payload: Any) -> ReviewItemRef:
    item = ReviewItemRef.from_payload(_mapping(_json_load(payload, default={})))
    if item is None:
        raise ValueError("Invalid review item payload.")
    return item


def _deserialize_team_position_review(payload: Any) -> TeamPositionReview:
    review = TeamPositionReview.from_payload(_mapping(_json_load(payload, default={})))
    if review is None:
        raise ValueError("Invalid team position review payload.")
    return review


def _deserialize_cross_team_leader_review(payload: Any) -> CrossTeamLeaderReview:
    review = CrossTeamLeaderReview.from_payload(_mapping(_json_load(payload, default={})))
    if review is None:
        raise ValueError("Invalid cross-team leader review payload.")
    return review


def _deserialize_superleader_synthesis(payload: Any) -> SuperLeaderSynthesis:
    synthesis = SuperLeaderSynthesis.from_payload(_mapping(_json_load(payload, default={})))
    if synthesis is None:
        raise ValueError("Invalid superleader synthesis payload.")
    return synthesis


def _deserialize_leader_draft_plan(payload: Any) -> LeaderDraftPlan:
    return LeaderDraftPlan.from_payload(_mapping(_json_load(payload, default={})))


def _deserialize_leader_peer_review(payload: Any) -> LeaderPeerReview:
    return LeaderPeerReview.from_payload(_mapping(_json_load(payload, default={})))


def _deserialize_superleader_global_review(payload: Any) -> SuperLeaderGlobalReview:
    return SuperLeaderGlobalReview.from_payload(_mapping(_json_load(payload, default={})))


def _deserialize_leader_revised_plan(payload: Any) -> LeaderRevisedPlan:
    return LeaderRevisedPlan.from_payload(_mapping(_json_load(payload, default={})))


def _deserialize_activation_gate_decision(payload: Any) -> ActivationGateDecision:
    return ActivationGateDecision.from_payload(_mapping(_json_load(payload, default={})))


def _deserialize_handoff(payload: Any) -> HandoffRecord:
    data = _mapping(_json_load(payload, default={}))
    return HandoffRecord(
        handoff_id=str(data["handoff_id"]),
        group_id=str(data["group_id"]),
        from_team_id=str(data["from_team_id"]),
        to_team_id=str(data["to_team_id"]),
        task_id=str(data["task_id"]),
        artifact_refs=_string_tuple(data.get("artifact_refs", ())),
        summary=str(data.get("summary", "")),
        contract_assertions=_string_tuple(data.get("contract_assertions", ())),
        verification_summary=_mapping(data.get("verification_summary", {})),
    )


def _deserialize_authority_state(payload: Any) -> AuthorityState:
    data = _mapping(_json_load(payload, default={}))
    return AuthorityState(
        group_id=str(data["group_id"]),
        status=AuthorityStatus(data.get("status", AuthorityStatus.PENDING.value)),
        accepted_handoffs=_string_tuple(data.get("accepted_handoffs", ())),
        updated_task_ids=_string_tuple(data.get("updated_task_ids", ())),
        summary=str(data.get("summary", "")),
    )


def _deserialize_blackboard_entry(payload: Any) -> BlackboardEntry:
    data = _mapping(_json_load(payload, default={}))
    return BlackboardEntry(
        entry_id=str(data["entry_id"]),
        blackboard_id=str(data["blackboard_id"]),
        group_id=str(data["group_id"]),
        kind=BlackboardKind(data["kind"]),
        entry_kind=BlackboardEntryKind(data["entry_kind"]),
        author_id=str(data["author_id"]),
        lane_id=str(data["lane_id"]) if data.get("lane_id") is not None else None,
        team_id=str(data["team_id"]) if data.get("team_id") is not None else None,
        task_id=str(data["task_id"]) if data.get("task_id") is not None else None,
        summary=str(data.get("summary", "")),
        payload=_mapping(data.get("payload", {})),
        created_at=str(data["created_at"]) if data.get("created_at") is not None else None,
    )


def _deserialize_blackboard_snapshot(payload: Any) -> BlackboardSnapshot:
    data = _mapping(_json_load(payload, default={}))
    return BlackboardSnapshot(
        blackboard_id=str(data["blackboard_id"]),
        group_id=str(data["group_id"]),
        kind=BlackboardKind(data["kind"]),
        lane_id=str(data["lane_id"]) if data.get("lane_id") is not None else None,
        team_id=str(data["team_id"]) if data.get("team_id") is not None else None,
        version=int(data.get("version", 0)),
        summary=str(data.get("summary", "")),
        latest_entry_ids=_string_tuple(data.get("latest_entry_ids", ())),
        open_blockers=_string_tuple(data.get("open_blockers", ())),
        open_proposals=_string_tuple(data.get("open_proposals", ())),
    )


def _deserialize_agent_session(payload: Any) -> AgentSession:
    data = _mapping(_json_load(payload, default={}))
    return AgentSession.from_dict(data)


def _deserialize_worker_handle(payload: Any) -> WorkerHandle:
    data = _mapping(_json_load(payload, default={}))
    return WorkerHandle(
        worker_id=str(data["worker_id"]),
        role=str(data["role"]),
        backend=str(data["backend"]),
        run_id=str(data["run_id"]) if data.get("run_id") is not None else None,
        process_id=int(data["process_id"]) if data.get("process_id") is not None else None,
        session_name=str(data["session_name"]) if data.get("session_name") is not None else None,
        transport_ref=str(data["transport_ref"]) if data.get("transport_ref") is not None else None,
        metadata=_mapping(data.get("metadata", {})),
    )


def _deserialize_worker_session(payload: Any) -> WorkerSession:
    return WorkerSession.from_dict(_normalize_worker_session_payload(payload))


def _deserialize_worker_record(payload: Any) -> WorkerRecord:
    data = _mapping(_json_load(payload, default={}))
    handle_payload = data.get("handle")
    session_payload = data.get("session")
    return WorkerRecord(
        worker_id=str(data["worker_id"]),
        assignment_id=str(data["assignment_id"]),
        backend=str(data["backend"]),
        role=str(data["role"]),
        status=WorkerStatus(data["status"]),
        handle=_deserialize_worker_handle(handle_payload) if handle_payload is not None else None,
        started_at=str(data["started_at"]) if data.get("started_at") is not None else None,
        ended_at=str(data["ended_at"]) if data.get("ended_at") is not None else None,
        last_heartbeat_at=str(data["last_heartbeat_at"]) if data.get("last_heartbeat_at") is not None else None,
        output_text=str(data.get("output_text", "")),
        error_text=str(data.get("error_text", "")),
        response_id=str(data["response_id"]) if data.get("response_id") is not None else None,
        usage=_mapping(data.get("usage", {})),
        metadata=_mapping(data.get("metadata", {})),
        session=_deserialize_worker_session(session_payload) if session_payload is not None else None,
    )


def _deserialize_delivery_state(payload: Any) -> DeliveryState:
    data = _mapping(_json_load(payload, default={}))
    mailbox_cursor = _mailbox_cursor_value(data.get("mailbox_cursor"))
    return DeliveryState(
        delivery_id=str(data["delivery_id"]),
        objective_id=str(data["objective_id"]),
        kind=DeliveryStateKind(data["kind"]),
        status=DeliveryStatus(data["status"]),
        lane_id=str(data["lane_id"]) if data.get("lane_id") is not None else None,
        team_id=str(data["team_id"]) if data.get("team_id") is not None else None,
        iteration=int(data.get("iteration", 0)),
        summary=str(data.get("summary", "")),
        pending_task_ids=_string_tuple(data.get("pending_task_ids", ())),
        active_task_ids=_string_tuple(data.get("active_task_ids", ())),
        completed_task_ids=_string_tuple(data.get("completed_task_ids", ())),
        blocked_task_ids=_string_tuple(data.get("blocked_task_ids", ())),
        latest_worker_ids=_string_tuple(data.get("latest_worker_ids", ())),
        mailbox_cursor=mailbox_cursor,
        metadata=_mapping(data.get("metadata", {})),
    )


def _deserialize_work_session(payload: Any) -> WorkSession:
    return WorkSession.from_payload(_mapping(_json_load(payload, default={})))


def _deserialize_runtime_generation(payload: Any) -> RuntimeGeneration:
    return RuntimeGeneration.from_payload(_mapping(_json_load(payload, default={})))


def _deserialize_work_session_message(payload: Any) -> WorkSessionMessage:
    return WorkSessionMessage.from_payload(_mapping(_json_load(payload, default={})))


def _deserialize_conversation_head(payload: Any) -> ConversationHead:
    return ConversationHead.from_payload(_mapping(_json_load(payload, default={})))


def _deserialize_session_event(payload: Any) -> SessionEvent:
    return SessionEvent.from_payload(_mapping(_json_load(payload, default={})))


def _deserialize_agent_turn_record(payload: Any) -> AgentTurnRecord:
    return AgentTurnRecord.from_payload(_mapping(_json_load(payload, default={})))


def _deserialize_tool_invocation_record(payload: Any) -> ToolInvocationRecord:
    return ToolInvocationRecord.from_payload(_mapping(_json_load(payload, default={})))


def _deserialize_artifact_ref(payload: Any) -> ArtifactRef:
    return ArtifactRef.from_payload(_mapping(_json_load(payload, default={})))


def _deserialize_session_memory_item(payload: Any) -> SessionMemoryItem:
    return SessionMemoryItem.from_payload(_mapping(_json_load(payload, default={})))


def _deserialize_resident_team_shell(payload: Any) -> ResidentTeamShell:
    return ResidentTeamShell.from_payload(_mapping(_json_load(payload, default={})))


def _deserialize_agent_slot(payload: Any) -> AgentSlot:
    return AgentSlot.from_payload(_mapping(_json_load(payload, default={})))


def _deserialize_agent_incarnation(payload: Any) -> AgentIncarnation:
    return AgentIncarnation.from_payload(_mapping(_json_load(payload, default={})))


def _deserialize_slot_health_event(payload: Any) -> SlotHealthEvent:
    return SlotHealthEvent.from_payload(_mapping(_json_load(payload, default={})))


def _deserialize_session_attachment(payload: Any) -> SessionAttachment:
    return SessionAttachment.from_payload(_mapping(_json_load(payload, default={})))


def _deserialize_provider_route_health(payload: Any) -> ProviderRouteHealth:
    return ProviderRouteHealth.from_payload(_mapping(_json_load(payload, default={})))


def _deserialize_coordination_outbox_record(payload: Any) -> CoordinationOutboxRecord:
    data = _mapping(_json_load(payload, default={}))
    return CoordinationOutboxRecord.from_payload(data)


def _task_claim_payload(
    *,
    owner_id: str,
    claim_session_id: str,
    claimed_at: str,
    claim_source: str,
) -> dict[str, Any]:
    return {
        "status": TaskStatus.IN_PROGRESS.value,
        "owner_id": owner_id,
        "claim_session_id": claim_session_id,
        "claimed_at": claimed_at,
        "claim_source": claim_source,
    }


def _resident_team_shell_latest_key(shell: ResidentTeamShell) -> tuple[str, str, str, str]:
    return (
        shell.last_progress_at or "",
        shell.updated_at or "",
        shell.created_at or "",
        shell.resident_team_shell_id,
    )


class PostgresOrchestrationStore(OrchestrationStore):
    supports_worker_session_coordination_transactions = True

    def __init__(
        self,
        dsn: str,
        schema: str = "agent_orchestra",
        connection_factory: Callable[[], Any] | None = None,
    ) -> None:
        self.dsn = dsn
        self.schema = schema
        self._connection_factory = connection_factory

    def get_schema_statements(self) -> tuple[str, ...]:
        return schema_statements(self.schema)

    async def _connect(self) -> Any:
        if self._connection_factory is not None:
            connection = self._connection_factory()
            return await _maybe_await(connection)
        try:
            import psycopg  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "psycopg is required for PostgresOrchestrationStore. Install the 'postgres' extra."
            ) from exc
        return await _maybe_await(psycopg.AsyncConnection.connect(self.dsn))

    async def _execute(
        self,
        query: str,
        params: tuple[Any, ...] = (),
        *,
        commit: bool = False,
        fetch: str | None = None,
    ) -> Any:
        connection = await self._connect()
        async with connection.cursor() as cursor:
            await cursor.execute(query, params)
            result = None
            if fetch == "one":
                result = await cursor.fetchone()
            elif fetch == "all":
                result = await cursor.fetchall()
        if commit:
            await _maybe_await(connection.commit())
        return result

    async def _execute_on_cursor(
        self,
        cursor: Any,
        query: str,
        params: tuple[Any, ...] = (),
        *,
        fetch: str | None = None,
    ) -> Any:
        await _maybe_await(cursor.execute(query, params))
        if fetch == "one":
            return await _maybe_await(cursor.fetchone())
        if fetch == "all":
            return await _maybe_await(cursor.fetchall())
        return None

    async def _run_write(self, operation: Callable[[Any], Any]) -> Any:
        connection = await self._connect()
        async with connection.cursor() as cursor:
            result = await _maybe_await(operation(cursor))
        await _maybe_await(connection.commit())
        return result

    async def _save_task_on_cursor(self, cursor: Any, task: TaskCard) -> None:
        await self._execute_on_cursor(
            cursor,
            f"""
            INSERT INTO {self.schema}.tasks (task_id, group_id, team_id, lane, goal, status, payload)
            VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (task_id) DO UPDATE SET
                group_id = EXCLUDED.group_id,
                team_id = EXCLUDED.team_id,
                lane = EXCLUDED.lane,
                goal = EXCLUDED.goal,
                status = EXCLUDED.status,
                payload = EXCLUDED.payload;
            """,
            (
                task.task_id,
                task.group_id,
                task.team_id,
                task.lane,
                task.goal,
                task.status.value,
                _json_param(task),
            ),
        )

    async def _save_task_review_slot_on_cursor(self, cursor: Any, slot: TaskReviewSlot) -> None:
        task_id = await self._require_task_on_cursor(
            cursor,
            task_id=slot.task_id,
            owner_kind="TaskReviewSlot",
        )
        await self._execute_on_cursor(
            cursor,
            f"""
            INSERT INTO {self.schema}.task_review_slots (
                task_id, reviewer_agent_id, reviewed_at, latest_revision_id, stance, payload
            )
            VALUES (%s, %s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (task_id, reviewer_agent_id) DO UPDATE SET
                reviewed_at = EXCLUDED.reviewed_at,
                latest_revision_id = EXCLUDED.latest_revision_id,
                stance = EXCLUDED.stance,
                payload = EXCLUDED.payload;
            """,
            (
                task_id,
                slot.reviewer_agent_id,
                slot.reviewed_at,
                slot.latest_revision_id,
                slot.stance.value,
                _json_param(slot.to_dict()),
            ),
        )

    async def _save_task_review_revision_on_cursor(
        self,
        cursor: Any,
        revision: TaskReviewRevision,
    ) -> None:
        task_id = await self._require_task_on_cursor(
            cursor,
            task_id=revision.task_id,
            owner_kind="TaskReviewRevision",
        )
        await self._execute_on_cursor(
            cursor,
            f"""
            INSERT INTO {self.schema}.task_review_revisions (
                revision_id, task_id, reviewer_agent_id, created_at, replaces_revision_id, stance, payload
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (revision_id) DO UPDATE SET
                task_id = EXCLUDED.task_id,
                reviewer_agent_id = EXCLUDED.reviewer_agent_id,
                created_at = EXCLUDED.created_at,
                replaces_revision_id = EXCLUDED.replaces_revision_id,
                stance = EXCLUDED.stance,
                payload = EXCLUDED.payload;
            """,
            (
                revision.revision_id,
                task_id,
                revision.reviewer_agent_id,
                revision.created_at,
                revision.replaces_revision_id,
                revision.stance.value,
                _json_param(revision.to_dict()),
            ),
        )

    async def _save_review_item_on_cursor(
        self,
        cursor: Any,
        item: ReviewItemRef,
    ) -> None:
        objective_id = await self._require_objective_on_cursor(
            cursor,
            objective_id=item.objective_id,
            owner_kind="ReviewItemRef",
        )
        source_task_id = await self._require_task_on_cursor(
            cursor,
            task_id=item.source_task_id,
            owner_kind="ReviewItemRef",
            required=False,
        )
        await self._execute_on_cursor(
            cursor,
            f"""
            INSERT INTO {self.schema}.review_items (
                item_id, objective_id, item_kind, lane_id, team_id, source_task_id, title, payload
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (item_id) DO UPDATE SET
                objective_id = EXCLUDED.objective_id,
                item_kind = EXCLUDED.item_kind,
                lane_id = EXCLUDED.lane_id,
                team_id = EXCLUDED.team_id,
                source_task_id = EXCLUDED.source_task_id,
                title = EXCLUDED.title,
                payload = EXCLUDED.payload;
            """,
            (
                item.item_id,
                objective_id,
                item.item_kind.value,
                item.lane_id,
                item.team_id,
                source_task_id,
                item.title,
                _json_param(item.to_dict()),
            ),
        )

    async def _save_team_position_review_on_cursor(
        self,
        cursor: Any,
        review: TeamPositionReview,
    ) -> None:
        item_id = await self._require_review_item_on_cursor(
            cursor,
            item_id=review.item_id,
            owner_kind="TeamPositionReview",
        )
        await self._execute_on_cursor(
            cursor,
            f"""
            INSERT INTO {self.schema}.team_position_reviews (
                position_review_id, item_id, item_kind, team_id, leader_id, reviewed_at, payload
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (position_review_id) DO UPDATE SET
                item_id = EXCLUDED.item_id,
                item_kind = EXCLUDED.item_kind,
                team_id = EXCLUDED.team_id,
                leader_id = EXCLUDED.leader_id,
                reviewed_at = EXCLUDED.reviewed_at,
                payload = EXCLUDED.payload;
            """,
            (
                review.position_review_id,
                item_id,
                review.item_kind.value,
                review.team_id,
                review.leader_id,
                review.reviewed_at,
                _json_param(review.to_dict()),
            ),
        )

    async def _save_cross_team_leader_review_on_cursor(
        self,
        cursor: Any,
        review: CrossTeamLeaderReview,
    ) -> None:
        item_id = await self._require_review_item_on_cursor(
            cursor,
            item_id=review.item_id,
            owner_kind="CrossTeamLeaderReview",
        )
        await self._execute_on_cursor(
            cursor,
            f"""
            INSERT INTO {self.schema}.cross_team_leader_reviews (
                cross_review_id, item_id, item_kind, reviewer_team_id, target_team_id, reviewed_at, payload
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (cross_review_id) DO UPDATE SET
                item_id = EXCLUDED.item_id,
                item_kind = EXCLUDED.item_kind,
                reviewer_team_id = EXCLUDED.reviewer_team_id,
                target_team_id = EXCLUDED.target_team_id,
                reviewed_at = EXCLUDED.reviewed_at,
                payload = EXCLUDED.payload;
            """,
            (
                review.cross_review_id,
                item_id,
                review.item_kind.value,
                review.reviewer_team_id,
                review.target_team_id,
                review.reviewed_at,
                _json_param(review.to_dict()),
            ),
        )

    async def _save_superleader_synthesis_on_cursor(
        self,
        cursor: Any,
        synthesis: SuperLeaderSynthesis,
    ) -> None:
        item_id = await self._require_review_item_on_cursor(
            cursor,
            item_id=synthesis.item_id,
            owner_kind="SuperLeaderSynthesis",
        )
        await self._execute_on_cursor(
            cursor,
            f"""
            INSERT INTO {self.schema}.superleader_syntheses (
                item_id, synthesis_id, item_kind, superleader_id, synthesized_at, payload
            )
            VALUES (%s, %s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (item_id) DO UPDATE SET
                synthesis_id = EXCLUDED.synthesis_id,
                item_kind = EXCLUDED.item_kind,
                superleader_id = EXCLUDED.superleader_id,
                synthesized_at = EXCLUDED.synthesized_at,
                payload = EXCLUDED.payload;
            """,
            (
                item_id,
                synthesis.synthesis_id,
                synthesis.item_kind.value,
                synthesis.superleader_id,
                synthesis.synthesized_at,
                _json_param(synthesis.to_dict()),
            ),
        )

    async def _save_leader_draft_plan_on_cursor(
        self,
        cursor: Any,
        plan: LeaderDraftPlan,
    ) -> None:
        await self._execute_on_cursor(
            cursor,
            f"""
            INSERT INTO {self.schema}.leader_draft_plans (
                plan_id, objective_id, planning_round_id, leader_id, lane_id, team_id, payload
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (plan_id) DO UPDATE SET
                objective_id = EXCLUDED.objective_id,
                planning_round_id = EXCLUDED.planning_round_id,
                leader_id = EXCLUDED.leader_id,
                lane_id = EXCLUDED.lane_id,
                team_id = EXCLUDED.team_id,
                payload = EXCLUDED.payload;
            """,
            (
                plan.plan_id,
                plan.objective_id,
                plan.planning_round_id,
                plan.leader_id,
                plan.lane_id,
                plan.team_id,
                _json_param(plan.to_dict()),
            ),
        )

    async def _save_leader_peer_review_on_cursor(
        self,
        cursor: Any,
        review: LeaderPeerReview,
    ) -> None:
        await self._execute_on_cursor(
            cursor,
            f"""
            INSERT INTO {self.schema}.leader_peer_reviews (
                review_id, objective_id, planning_round_id, reviewer_leader_id,
                reviewer_team_id, target_leader_id, target_team_id, payload
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (review_id) DO UPDATE SET
                objective_id = EXCLUDED.objective_id,
                planning_round_id = EXCLUDED.planning_round_id,
                reviewer_leader_id = EXCLUDED.reviewer_leader_id,
                reviewer_team_id = EXCLUDED.reviewer_team_id,
                target_leader_id = EXCLUDED.target_leader_id,
                target_team_id = EXCLUDED.target_team_id,
                payload = EXCLUDED.payload;
            """,
            (
                review.review_id,
                review.objective_id,
                review.planning_round_id,
                review.reviewer_leader_id,
                review.reviewer_team_id,
                review.target_leader_id,
                review.target_team_id,
                _json_param(review.to_dict()),
            ),
        )

    async def _save_superleader_global_review_on_cursor(
        self,
        cursor: Any,
        review: SuperLeaderGlobalReview,
    ) -> None:
        await self._execute_on_cursor(
            cursor,
            f"""
            INSERT INTO {self.schema}.superleader_global_reviews (
                objective_id, planning_round_id, review_id, superleader_id, payload
            )
            VALUES (%s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (objective_id, planning_round_id) DO UPDATE SET
                review_id = EXCLUDED.review_id,
                superleader_id = EXCLUDED.superleader_id,
                payload = EXCLUDED.payload;
            """,
            (
                review.objective_id,
                review.planning_round_id,
                review.review_id,
                review.superleader_id,
                _json_param(review.to_dict()),
            ),
        )

    async def _save_leader_revised_plan_on_cursor(
        self,
        cursor: Any,
        plan: LeaderRevisedPlan,
    ) -> None:
        await self._execute_on_cursor(
            cursor,
            f"""
            INSERT INTO {self.schema}.leader_revised_plans (
                plan_id, objective_id, planning_round_id, leader_id, lane_id, team_id, payload
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (plan_id) DO UPDATE SET
                objective_id = EXCLUDED.objective_id,
                planning_round_id = EXCLUDED.planning_round_id,
                leader_id = EXCLUDED.leader_id,
                lane_id = EXCLUDED.lane_id,
                team_id = EXCLUDED.team_id,
                payload = EXCLUDED.payload;
            """,
            (
                plan.plan_id,
                plan.objective_id,
                plan.planning_round_id,
                plan.leader_id,
                plan.lane_id,
                plan.team_id,
                _json_param(plan.to_dict()),
            ),
        )

    async def _save_activation_gate_decision_on_cursor(
        self,
        cursor: Any,
        decision: ActivationGateDecision,
    ) -> None:
        await self._execute_on_cursor(
            cursor,
            f"""
            INSERT INTO {self.schema}.activation_gate_decisions (
                objective_id, planning_round_id, decision_id, status, payload
            )
            VALUES (%s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (objective_id, planning_round_id) DO UPDATE SET
                decision_id = EXCLUDED.decision_id,
                status = EXCLUDED.status,
                payload = EXCLUDED.payload;
            """,
            (
                decision.objective_id,
                decision.planning_round_id,
                decision.decision_id,
                decision.status.value,
                _json_param(decision.to_dict()),
            ),
        )

    async def _save_blackboard_entry_on_cursor(self, cursor: Any, entry: BlackboardEntry) -> None:
        created_at = entry.created_at or _now_iso()
        payload = asdict(entry)
        payload["created_at"] = created_at
        await self._execute_on_cursor(
            cursor,
            f"""
            INSERT INTO {self.schema}.blackboard_entries (
                entry_id, blackboard_id, group_id, kind, entry_kind, lane_id, team_id, task_id, created_at, payload
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (entry_id) DO UPDATE SET
                blackboard_id = EXCLUDED.blackboard_id,
                group_id = EXCLUDED.group_id,
                kind = EXCLUDED.kind,
                entry_kind = EXCLUDED.entry_kind,
                lane_id = EXCLUDED.lane_id,
                team_id = EXCLUDED.team_id,
                task_id = EXCLUDED.task_id,
                created_at = EXCLUDED.created_at,
                payload = EXCLUDED.payload;
            """,
            (
                entry.entry_id,
                entry.blackboard_id,
                entry.group_id,
                entry.kind.value,
                entry.entry_kind.value,
                entry.lane_id,
                entry.team_id,
                entry.task_id,
                created_at,
                _json_param(payload),
            ),
        )

    async def _save_agent_session_on_cursor(self, cursor: Any, session: AgentSession) -> None:
        payload = _json_safe_copy(session.to_dict())
        await self._execute_on_cursor(
            cursor,
            f"""
            INSERT INTO {self.schema}.agent_sessions (
                session_id, agent_id, role, phase, objective_id, lane_id, team_id, last_progress_at, payload
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (session_id) DO UPDATE SET
                agent_id = EXCLUDED.agent_id,
                role = EXCLUDED.role,
                phase = EXCLUDED.phase,
                objective_id = EXCLUDED.objective_id,
                lane_id = EXCLUDED.lane_id,
                team_id = EXCLUDED.team_id,
                last_progress_at = EXCLUDED.last_progress_at,
                payload = EXCLUDED.payload;
            """,
            (
                session.session_id,
                session.agent_id,
                session.role,
                session.phase.value,
                session.objective_id,
                session.lane_id,
                session.team_id,
                session.last_progress_at,
                _json_param(payload),
            ),
        )

    async def _save_worker_session_on_cursor(self, cursor: Any, session: WorkerSession) -> None:
        payload = _normalize_worker_session_payload(session.to_dict())
        await self._execute_on_cursor(
            cursor,
            f"""
            INSERT INTO {self.schema}.worker_sessions (
                session_id,
                worker_id,
                assignment_id,
                backend,
                role,
                status,
                lifecycle_status,
                started_at,
                last_active_at,
                idle_since,
                last_response_id,
                supervisor_id,
                supervisor_lease_id,
                supervisor_lease_expires_at,
                reactivation_count,
                reattach_count,
                payload
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (session_id) DO UPDATE SET
                worker_id = EXCLUDED.worker_id,
                assignment_id = EXCLUDED.assignment_id,
                backend = EXCLUDED.backend,
                role = EXCLUDED.role,
                status = EXCLUDED.status,
                lifecycle_status = EXCLUDED.lifecycle_status,
                started_at = EXCLUDED.started_at,
                last_active_at = EXCLUDED.last_active_at,
                idle_since = EXCLUDED.idle_since,
                last_response_id = EXCLUDED.last_response_id,
                supervisor_id = EXCLUDED.supervisor_id,
                supervisor_lease_id = EXCLUDED.supervisor_lease_id,
                supervisor_lease_expires_at = EXCLUDED.supervisor_lease_expires_at,
                reactivation_count = EXCLUDED.reactivation_count,
                reattach_count = EXCLUDED.reattach_count,
                payload = EXCLUDED.payload;
            """,
            (
                session.session_id,
                session.worker_id,
                payload.get("assignment_id"),
                session.backend,
                session.role,
                payload.get("status"),
                payload.get("lifecycle_status"),
                payload.get("started_at"),
                payload.get("last_active_at"),
                payload.get("idle_since"),
                payload.get("last_response_id"),
                payload.get("supervisor_id"),
                payload.get("supervisor_lease_id"),
                payload.get("supervisor_lease_expires_at"),
                int(payload.get("reactivation_count", 0)),
                int(payload.get("reattach_count", 0)),
                _json_param(payload),
            ),
        )

    async def _save_protocol_bus_cursor_on_cursor(
        self,
        cursor: Any,
        *,
        stream: str,
        consumer: str,
        cursor_payload: dict[str, Any],
    ) -> None:
        payload = _json_safe_copy(cursor_payload)
        await self._execute_on_cursor(
            cursor,
            f"""
            INSERT INTO {self.schema}.protocol_bus_cursors (stream, consumer, payload)
            VALUES (%s, %s, %s::jsonb)
            ON CONFLICT (stream, consumer) DO UPDATE SET
                payload = EXCLUDED.payload;
            """,
            (
                stream,
                consumer,
                _json_param(payload),
            ),
        )

    async def _save_coordination_outbox_record_on_cursor(
        self,
        cursor: Any,
        *,
        outbox_id: str,
        record: CoordinationOutboxRecord,
    ) -> None:
        payload = _json_safe_copy(record.to_dict())
        await self._execute_on_cursor(
            cursor,
            f"""
            INSERT INTO {self.schema}.coordination_outbox (
                outbox_id, subject, recipient, sender, payload
            )
            VALUES (%s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (outbox_id) DO UPDATE SET
                subject = EXCLUDED.subject,
                recipient = EXCLUDED.recipient,
                sender = EXCLUDED.sender,
                payload = EXCLUDED.payload;
            """,
            (
                outbox_id,
                record.subject,
                record.recipient,
                record.sender,
                _json_param(payload),
            ),
        )

    async def _commit_coordination_on_cursor(
        self,
        cursor: Any,
        commit: CoordinationTransactionStoreCommit,
    ) -> None:
        for task in commit.task_mutations:
            await self._save_task_on_cursor(cursor, task)
        for task in commit.replacement_tasks:
            await self._save_task_on_cursor(cursor, task)
        for protocol_bus_cursor in commit.mailbox_cursors:
            await self._save_protocol_bus_cursor_on_cursor(
                cursor,
                stream=protocol_bus_cursor.stream,
                consumer=protocol_bus_cursor.consumer,
                cursor_payload=protocol_bus_cursor.cursor,
            )
        for blackboard_entry in commit.blackboard_entries:
            await self._save_blackboard_entry_on_cursor(cursor, blackboard_entry)
        for delivery_state in commit.delivery_snapshots:
            await self._save_delivery_state_on_cursor(cursor, delivery_state)
        for agent_session in commit.session_snapshots:
            await self._save_agent_session_on_cursor(cursor, agent_session)
        for worker_session in commit.worker_session_snapshots:
            await self._save_worker_session_on_cursor(cursor, worker_session)
        if commit.durable_outbox_records:
            scope_id = commit.effective_outbox_scope_id or "coordination"
            for index, record in enumerate(commit.durable_outbox_records):
                await self._save_coordination_outbox_record_on_cursor(
                    cursor,
                    outbox_id=f"{scope_id}:{index}",
                    record=record,
                )

    async def _save_delivery_state_on_cursor(self, cursor: Any, state: DeliveryState) -> None:
        await self._execute_on_cursor(
            cursor,
            f"""
            INSERT INTO {self.schema}.delivery_states (
                delivery_id, objective_id, kind, status, lane_id, team_id, iteration, mailbox_cursor, payload
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (delivery_id) DO UPDATE SET
                objective_id = EXCLUDED.objective_id,
                kind = EXCLUDED.kind,
                status = EXCLUDED.status,
                lane_id = EXCLUDED.lane_id,
                team_id = EXCLUDED.team_id,
                iteration = EXCLUDED.iteration,
                mailbox_cursor = EXCLUDED.mailbox_cursor,
                payload = EXCLUDED.payload;
            """,
            (
                state.delivery_id,
                state.objective_id,
                state.kind.value,
                state.status.value,
                state.lane_id,
                state.team_id,
                state.iteration,
                _mailbox_cursor_param(state.mailbox_cursor),
                _json_param(state),
            ),
        )

    async def _save_work_session_on_cursor(self, cursor: Any, session: WorkSession) -> None:
        created_at = _normalized_required_timestamp(
            session.created_at,
            owner_kind="WorkSession",
            field_name="created_at",
        )
        updated_at = _normalized_required_timestamp(
            session.updated_at,
            owner_kind="WorkSession",
            field_name="updated_at",
        )
        payload = session.to_dict()
        payload["created_at"] = created_at
        payload["updated_at"] = updated_at
        await self._execute_on_cursor(
            cursor,
            f"""
            INSERT INTO {self.schema}.work_sessions (
                work_session_id,
                group_id,
                root_objective_id,
                status,
                created_at,
                updated_at,
                current_runtime_generation_id,
                payload
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (work_session_id) DO UPDATE SET
                group_id = EXCLUDED.group_id,
                root_objective_id = EXCLUDED.root_objective_id,
                status = EXCLUDED.status,
                created_at = EXCLUDED.created_at,
                updated_at = EXCLUDED.updated_at,
                current_runtime_generation_id = EXCLUDED.current_runtime_generation_id,
                payload = EXCLUDED.payload;
            """,
            (
                session.work_session_id,
                session.group_id,
                session.root_objective_id,
                session.status,
                created_at,
                updated_at,
                session.current_runtime_generation_id,
                _json_param(payload),
            ),
        )

    async def _require_work_session_on_cursor(
        self,
        cursor: Any,
        *,
        work_session_id: str,
        owner_kind: str,
        allowed_new_work_session_ids: tuple[str, ...] = (),
    ) -> str:
        normalized_work_session_id = str(work_session_id).strip()
        if not normalized_work_session_id:
            raise ValueError(
                f"{owner_kind} work_session_id must reference an existing WorkSession"
            )
        if normalized_work_session_id in allowed_new_work_session_ids:
            return normalized_work_session_id
        row = await self._execute_on_cursor(
            cursor,
            f"""
            SELECT work_session_id
            FROM {self.schema}.work_sessions
            WHERE work_session_id = %s;
            """,
            (normalized_work_session_id,),
            fetch="one",
        )
        if row is None:
            raise ValueError(
                f"{owner_kind} work_session_id must reference an existing WorkSession"
            )
        return normalized_work_session_id

    async def _require_runtime_generation_on_cursor(
        self,
        cursor: Any,
        *,
        runtime_generation_id: str | None,
        work_session_id: str,
        owner_kind: str,
        required: bool = True,
        allowed_new_runtime_generation_ids: tuple[str, ...] = (),
    ) -> str | None:
        normalized_runtime_generation_id = (
            None if runtime_generation_id is None else str(runtime_generation_id).strip()
        )
        if normalized_runtime_generation_id is None or not normalized_runtime_generation_id:
            if required:
                raise ValueError(
                    f"{owner_kind} runtime_generation_id must reference an existing RuntimeGeneration"
                )
            return None
        if normalized_runtime_generation_id in allowed_new_runtime_generation_ids:
            return normalized_runtime_generation_id
        row = await self._execute_on_cursor(
            cursor,
            f"""
            SELECT work_session_id
            FROM {self.schema}.runtime_generations
            WHERE runtime_generation_id = %s;
            """,
            (normalized_runtime_generation_id,),
            fetch="one",
        )
        if row is None or str(row[0]).strip() != work_session_id:
            raise ValueError(
                f"{owner_kind} runtime_generation_id must reference an existing RuntimeGeneration"
            )
        return normalized_runtime_generation_id

    async def _require_agent_slot_on_cursor(
        self,
        cursor: Any,
        *,
        slot_id: str,
        owner_kind: str,
        expected_work_session_id: str | None = None,
        allowed_new_slot_ids: tuple[str, ...] = (),
    ) -> str:
        normalized_slot_id = str(slot_id).strip()
        if not normalized_slot_id:
            raise ValueError(f"{owner_kind} slot_id must reference an existing AgentSlot")
        if normalized_slot_id in allowed_new_slot_ids:
            return normalized_slot_id
        row = await self._execute_on_cursor(
            cursor,
            f"""
            SELECT work_session_id
            FROM {self.schema}.agent_slots
            WHERE slot_id = %s;
            """,
            (normalized_slot_id,),
            fetch="one",
        )
        if row is None:
            raise ValueError(f"{owner_kind} slot_id must reference an existing AgentSlot")
        if expected_work_session_id is not None and str(row[0]).strip() != expected_work_session_id:
            raise ValueError(f"{owner_kind} slot_id must reference an existing AgentSlot")
        return normalized_slot_id

    async def _require_agent_incarnation_on_cursor(
        self,
        cursor: Any,
        *,
        incarnation_id: str | None,
        owner_kind: str,
        expected_work_session_id: str | None = None,
        expected_slot_id: str | None = None,
        required: bool = True,
        allowed_new_incarnation_ids: tuple[str, ...] = (),
    ) -> str | None:
        normalized_incarnation_id = (
            None if incarnation_id is None else str(incarnation_id).strip()
        )
        if normalized_incarnation_id is None or not normalized_incarnation_id:
            if required:
                raise ValueError(
                    f"{owner_kind} incarnation_id must reference an existing AgentIncarnation"
                )
            return None
        if normalized_incarnation_id in allowed_new_incarnation_ids:
            return normalized_incarnation_id
        row = await self._execute_on_cursor(
            cursor,
            f"""
            SELECT slot_id, work_session_id
            FROM {self.schema}.agent_incarnations
            WHERE incarnation_id = %s;
            """,
            (normalized_incarnation_id,),
            fetch="one",
        )
        if row is None:
            raise ValueError(
                f"{owner_kind} incarnation_id must reference an existing AgentIncarnation"
            )
        if expected_slot_id is not None and str(row[0]).strip() != expected_slot_id:
            raise ValueError(
                f"{owner_kind} incarnation_id must reference an existing AgentIncarnation"
            )
        if expected_work_session_id is not None and str(row[1]).strip() != expected_work_session_id:
            raise ValueError(
                f"{owner_kind} incarnation_id must reference an existing AgentIncarnation"
            )
        return normalized_incarnation_id

    async def _require_turn_record_on_cursor(
        self,
        cursor: Any,
        *,
        turn_record_id: str | None,
        work_session_id: str,
        owner_kind: str,
        required: bool = True,
        allowed_new_turn_record_ids: tuple[str, ...] = (),
    ) -> str | None:
        normalized_turn_record_id = None if turn_record_id is None else str(turn_record_id).strip()
        if normalized_turn_record_id is None or not normalized_turn_record_id:
            if required:
                raise ValueError(
                    f"{owner_kind} turn_record_id must reference an existing AgentTurnRecord"
                )
            return None
        if normalized_turn_record_id in allowed_new_turn_record_ids:
            return normalized_turn_record_id
        row = await self._execute_on_cursor(
            cursor,
            f"""
            SELECT work_session_id
            FROM {self.schema}.agent_turn_records
            WHERE turn_record_id = %s;
            """,
            (normalized_turn_record_id,),
            fetch="one",
        )
        if row is None or str(row[0]).strip() != work_session_id:
            raise ValueError(
                f"{owner_kind} turn_record_id must reference an existing AgentTurnRecord"
            )
        return normalized_turn_record_id

    async def _require_tool_invocation_on_cursor(
        self,
        cursor: Any,
        *,
        tool_invocation_id: str | None,
        work_session_id: str,
        owner_kind: str,
        required: bool = True,
        allowed_new_tool_invocation_ids: tuple[str, ...] = (),
    ) -> str | None:
        normalized_tool_invocation_id = (
            None if tool_invocation_id is None else str(tool_invocation_id).strip()
        )
        if normalized_tool_invocation_id is None or not normalized_tool_invocation_id:
            if required:
                raise ValueError(
                    f"{owner_kind} tool_invocation_id must reference an existing ToolInvocationRecord"
                )
            return None
        if normalized_tool_invocation_id in allowed_new_tool_invocation_ids:
            return normalized_tool_invocation_id
        row = await self._execute_on_cursor(
            cursor,
            f"""
            SELECT work_session_id
            FROM {self.schema}.tool_invocation_records
            WHERE tool_invocation_id = %s;
            """,
            (normalized_tool_invocation_id,),
            fetch="one",
        )
        if row is None or str(row[0]).strip() != work_session_id:
            raise ValueError(
                f"{owner_kind} tool_invocation_id must reference an existing ToolInvocationRecord"
            )
        return normalized_tool_invocation_id

    async def _require_objective_on_cursor(
        self,
        cursor: Any,
        *,
        objective_id: str,
        owner_kind: str,
    ) -> str:
        normalized_objective_id = str(objective_id).strip()
        if not normalized_objective_id:
            raise ValueError(
                f"{owner_kind} objective_id must reference an existing ObjectiveSpec"
            )
        row = await self._execute_on_cursor(
            cursor,
            f"""
            SELECT objective_id
            FROM {self.schema}.objectives
            WHERE objective_id = %s;
            """,
            (normalized_objective_id,),
            fetch="one",
        )
        if row is None:
            raise ValueError(
                f"{owner_kind} objective_id must reference an existing ObjectiveSpec"
            )
        return normalized_objective_id

    async def _require_task_on_cursor(
        self,
        cursor: Any,
        *,
        task_id: str | None,
        owner_kind: str,
        required: bool = True,
    ) -> str | None:
        normalized_task_id = None if task_id is None else str(task_id).strip()
        if normalized_task_id is None or not normalized_task_id:
            if required:
                raise ValueError(f"{owner_kind} task_id must reference an existing TaskCard")
            return None
        row = await self._execute_on_cursor(
            cursor,
            f"""
            SELECT task_id
            FROM {self.schema}.tasks
            WHERE task_id = %s;
            """,
            (normalized_task_id,),
            fetch="one",
        )
        if row is None:
            raise ValueError(f"{owner_kind} task_id must reference an existing TaskCard")
        return normalized_task_id

    async def _require_review_item_on_cursor(
        self,
        cursor: Any,
        *,
        item_id: str,
        owner_kind: str,
    ) -> str:
        normalized_item_id = str(item_id).strip()
        if not normalized_item_id:
            raise ValueError(
                f"{owner_kind} item_id must reference an existing ReviewItemRef"
            )
        row = await self._execute_on_cursor(
            cursor,
            f"""
            SELECT item_id
            FROM {self.schema}.review_items
            WHERE item_id = %s;
            """,
            (normalized_item_id,),
            fetch="one",
        )
        if row is None:
            raise ValueError(
                f"{owner_kind} item_id must reference an existing ReviewItemRef"
            )
        return normalized_item_id

    async def _save_runtime_generation_on_cursor(
        self,
        cursor: Any,
        generation: RuntimeGeneration,
        *,
        allowed_new_work_session_ids: tuple[str, ...] = (),
    ) -> None:
        work_session_id = await self._require_work_session_on_cursor(
            cursor,
            work_session_id=generation.work_session_id,
            owner_kind="RuntimeGeneration",
            allowed_new_work_session_ids=allowed_new_work_session_ids,
        )
        created_at = _normalized_required_timestamp(
            generation.created_at,
            owner_kind="RuntimeGeneration",
            field_name="created_at",
        )
        closed_at = _normalized_optional_timestamp(
            generation.closed_at,
            owner_kind="RuntimeGeneration",
            field_name="closed_at",
        )
        payload = generation.to_dict()
        payload["created_at"] = created_at
        payload["closed_at"] = closed_at
        await self._execute_on_cursor(
            cursor,
            f"""
            INSERT INTO {self.schema}.runtime_generations (
                runtime_generation_id,
                work_session_id,
                generation_index,
                status,
                continuity_mode,
                created_at,
                closed_at,
                source_runtime_generation_id,
                group_id,
                objective_id,
                payload
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (runtime_generation_id) DO UPDATE SET
                work_session_id = EXCLUDED.work_session_id,
                generation_index = EXCLUDED.generation_index,
                status = EXCLUDED.status,
                continuity_mode = EXCLUDED.continuity_mode,
                created_at = EXCLUDED.created_at,
                closed_at = EXCLUDED.closed_at,
                source_runtime_generation_id = EXCLUDED.source_runtime_generation_id,
                group_id = EXCLUDED.group_id,
                objective_id = EXCLUDED.objective_id,
                payload = EXCLUDED.payload;
            """,
            (
                generation.runtime_generation_id,
                work_session_id,
                generation.generation_index,
                generation.status.value,
                generation.continuity_mode.value,
                created_at,
                closed_at,
                generation.source_runtime_generation_id,
                generation.group_id,
                generation.objective_id,
                _json_param(payload),
            ),
        )

    async def _append_work_session_message_on_cursor(
        self,
        cursor: Any,
        message: WorkSessionMessage,
        *,
        allowed_new_work_session_ids: tuple[str, ...] = (),
        allowed_new_runtime_generation_ids: tuple[str, ...] = (),
    ) -> None:
        work_session_id = await self._require_work_session_on_cursor(
            cursor,
            work_session_id=message.work_session_id,
            owner_kind="WorkSessionMessage",
            allowed_new_work_session_ids=allowed_new_work_session_ids,
        )
        runtime_generation_id = await self._require_runtime_generation_on_cursor(
            cursor,
            runtime_generation_id=message.runtime_generation_id,
            work_session_id=work_session_id,
            owner_kind="WorkSessionMessage",
            required=False,
            allowed_new_runtime_generation_ids=allowed_new_runtime_generation_ids,
        )
        created_at = _normalized_required_timestamp(
            message.created_at,
            owner_kind="WorkSessionMessage",
            field_name="created_at",
        )
        payload = message.to_dict()
        payload["work_session_id"] = work_session_id
        payload["runtime_generation_id"] = runtime_generation_id
        payload["created_at"] = created_at
        await self._execute_on_cursor(
            cursor,
            f"""
            INSERT INTO {self.schema}.work_session_messages (
                message_id,
                work_session_id,
                runtime_generation_id,
                created_at,
                payload
            )
            VALUES (%s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (message_id) DO UPDATE SET
                work_session_id = EXCLUDED.work_session_id,
                runtime_generation_id = EXCLUDED.runtime_generation_id,
                created_at = EXCLUDED.created_at,
                payload = EXCLUDED.payload;
            """,
            (
                message.message_id,
                work_session_id,
                runtime_generation_id,
                created_at,
                _json_param(payload),
            ),
        )

    async def _save_conversation_head_on_cursor(
        self,
        cursor: Any,
        head: ConversationHead,
        *,
        allowed_new_work_session_ids: tuple[str, ...] = (),
        allowed_new_runtime_generation_ids: tuple[str, ...] = (),
    ) -> None:
        work_session_id = await self._require_work_session_on_cursor(
            cursor,
            work_session_id=head.work_session_id,
            owner_kind="ConversationHead",
            allowed_new_work_session_ids=allowed_new_work_session_ids,
        )
        runtime_generation_id = await self._require_runtime_generation_on_cursor(
            cursor,
            runtime_generation_id=head.runtime_generation_id,
            work_session_id=work_session_id,
            owner_kind="ConversationHead",
            allowed_new_runtime_generation_ids=allowed_new_runtime_generation_ids,
        )
        updated_at = _normalized_required_timestamp(
            head.updated_at,
            owner_kind="ConversationHead",
            field_name="updated_at",
        )
        payload = head.to_dict()
        payload["work_session_id"] = work_session_id
        payload["runtime_generation_id"] = runtime_generation_id
        payload["updated_at"] = updated_at
        await self._execute_on_cursor(
            cursor,
            f"""
            INSERT INTO {self.schema}.conversation_heads (
                conversation_head_id,
                work_session_id,
                runtime_generation_id,
                head_kind,
                scope_id,
                updated_at,
                payload
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (conversation_head_id) DO UPDATE SET
                work_session_id = EXCLUDED.work_session_id,
                runtime_generation_id = EXCLUDED.runtime_generation_id,
                head_kind = EXCLUDED.head_kind,
                scope_id = EXCLUDED.scope_id,
                updated_at = EXCLUDED.updated_at,
                payload = EXCLUDED.payload;
            """,
            (
                head.conversation_head_id,
                work_session_id,
                runtime_generation_id,
                head.head_kind.value,
                head.scope_id,
                updated_at,
                _json_param(payload),
            ),
        )

    async def _append_session_event_on_cursor(
        self,
        cursor: Any,
        event: SessionEvent,
        *,
        allowed_new_work_session_ids: tuple[str, ...] = (),
        allowed_new_runtime_generation_ids: tuple[str, ...] = (),
    ) -> None:
        work_session_id = await self._require_work_session_on_cursor(
            cursor,
            work_session_id=event.work_session_id,
            owner_kind="SessionEvent",
            allowed_new_work_session_ids=allowed_new_work_session_ids,
        )
        runtime_generation_id = await self._require_runtime_generation_on_cursor(
            cursor,
            runtime_generation_id=event.runtime_generation_id,
            work_session_id=work_session_id,
            owner_kind="SessionEvent",
            required=False,
            allowed_new_runtime_generation_ids=allowed_new_runtime_generation_ids,
        )
        created_at = _normalized_required_timestamp(
            event.created_at,
            owner_kind="SessionEvent",
            field_name="created_at",
        )
        payload = event.to_dict()
        payload["work_session_id"] = work_session_id
        payload["runtime_generation_id"] = runtime_generation_id
        payload["created_at"] = created_at
        await self._execute_on_cursor(
            cursor,
            f"""
            INSERT INTO {self.schema}.session_events (
                session_event_id,
                work_session_id,
                runtime_generation_id,
                event_kind,
                created_at,
                payload
            )
            VALUES (%s, %s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (session_event_id) DO UPDATE SET
                work_session_id = EXCLUDED.work_session_id,
                runtime_generation_id = EXCLUDED.runtime_generation_id,
                event_kind = EXCLUDED.event_kind,
                created_at = EXCLUDED.created_at,
                payload = EXCLUDED.payload;
            """,
            (
                event.session_event_id,
                work_session_id,
                runtime_generation_id,
                event.event_kind,
                created_at,
                _json_param(payload),
            ),
        )

    async def _append_turn_record_on_cursor(
        self,
        cursor: Any,
        record: AgentTurnRecord,
        *,
        allowed_new_work_session_ids: tuple[str, ...] = (),
        allowed_new_runtime_generation_ids: tuple[str, ...] = (),
    ) -> None:
        work_session_id = await self._require_work_session_on_cursor(
            cursor,
            work_session_id=record.work_session_id,
            owner_kind="AgentTurnRecord",
            allowed_new_work_session_ids=allowed_new_work_session_ids,
        )
        runtime_generation_id = await self._require_runtime_generation_on_cursor(
            cursor,
            runtime_generation_id=record.runtime_generation_id,
            work_session_id=work_session_id,
            owner_kind="AgentTurnRecord",
            allowed_new_runtime_generation_ids=allowed_new_runtime_generation_ids,
        )
        created_at = _normalized_required_timestamp(
            record.created_at,
            owner_kind="AgentTurnRecord",
            field_name="created_at",
        )
        payload = record.to_dict()
        payload["work_session_id"] = work_session_id
        payload["runtime_generation_id"] = runtime_generation_id
        payload["created_at"] = created_at
        await self._execute_on_cursor(
            cursor,
            f"""
            INSERT INTO {self.schema}.agent_turn_records (
                turn_record_id,
                work_session_id,
                runtime_generation_id,
                head_kind,
                scope_id,
                assignment_id,
                created_at,
                payload
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (turn_record_id) DO UPDATE SET
                work_session_id = EXCLUDED.work_session_id,
                runtime_generation_id = EXCLUDED.runtime_generation_id,
                head_kind = EXCLUDED.head_kind,
                scope_id = EXCLUDED.scope_id,
                assignment_id = EXCLUDED.assignment_id,
                created_at = EXCLUDED.created_at,
                payload = EXCLUDED.payload;
            """,
            (
                record.turn_record_id,
                work_session_id,
                runtime_generation_id,
                record.head_kind.value,
                record.scope_id,
                record.assignment_id,
                created_at,
                _json_param(payload),
            ),
        )

    async def _append_tool_invocation_record_on_cursor(
        self,
        cursor: Any,
        record: ToolInvocationRecord,
        *,
        allowed_new_work_session_ids: tuple[str, ...] = (),
        allowed_new_runtime_generation_ids: tuple[str, ...] = (),
        allowed_new_turn_record_ids: tuple[str, ...] = (),
    ) -> None:
        work_session_id = await self._require_work_session_on_cursor(
            cursor,
            work_session_id=record.work_session_id,
            owner_kind="ToolInvocationRecord",
            allowed_new_work_session_ids=allowed_new_work_session_ids,
        )
        runtime_generation_id = await self._require_runtime_generation_on_cursor(
            cursor,
            runtime_generation_id=record.runtime_generation_id,
            work_session_id=work_session_id,
            owner_kind="ToolInvocationRecord",
            allowed_new_runtime_generation_ids=allowed_new_runtime_generation_ids,
        )
        turn_record_id = await self._require_turn_record_on_cursor(
            cursor,
            turn_record_id=record.turn_record_id,
            work_session_id=work_session_id,
            owner_kind="ToolInvocationRecord",
            required=False,
            allowed_new_turn_record_ids=allowed_new_turn_record_ids,
        )
        started_at = _normalized_required_timestamp(
            record.started_at,
            owner_kind="ToolInvocationRecord",
            field_name="started_at",
        )
        completed_at = _normalized_optional_timestamp(
            record.completed_at,
            owner_kind="ToolInvocationRecord",
            field_name="completed_at",
        )
        payload = record.to_dict()
        payload["work_session_id"] = work_session_id
        payload["runtime_generation_id"] = runtime_generation_id
        payload["turn_record_id"] = turn_record_id
        payload["started_at"] = started_at
        payload["completed_at"] = completed_at
        await self._execute_on_cursor(
            cursor,
            f"""
            INSERT INTO {self.schema}.tool_invocation_records (
                tool_invocation_id,
                turn_record_id,
                work_session_id,
                runtime_generation_id,
                started_at,
                completed_at,
                payload
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (tool_invocation_id) DO UPDATE SET
                turn_record_id = EXCLUDED.turn_record_id,
                work_session_id = EXCLUDED.work_session_id,
                runtime_generation_id = EXCLUDED.runtime_generation_id,
                started_at = EXCLUDED.started_at,
                completed_at = EXCLUDED.completed_at,
                payload = EXCLUDED.payload;
            """,
            (
                record.tool_invocation_id,
                turn_record_id,
                work_session_id,
                runtime_generation_id,
                started_at,
                completed_at,
                _json_param(payload),
            ),
        )

    async def _save_artifact_ref_on_cursor(
        self,
        cursor: Any,
        artifact: ArtifactRef,
        *,
        allowed_new_work_session_ids: tuple[str, ...] = (),
        allowed_new_runtime_generation_ids: tuple[str, ...] = (),
        allowed_new_turn_record_ids: tuple[str, ...] = (),
        allowed_new_tool_invocation_ids: tuple[str, ...] = (),
    ) -> None:
        work_session_id = await self._require_work_session_on_cursor(
            cursor,
            work_session_id=artifact.work_session_id,
            owner_kind="ArtifactRef",
            allowed_new_work_session_ids=allowed_new_work_session_ids,
        )
        runtime_generation_id = await self._require_runtime_generation_on_cursor(
            cursor,
            runtime_generation_id=artifact.runtime_generation_id,
            work_session_id=work_session_id,
            owner_kind="ArtifactRef",
            allowed_new_runtime_generation_ids=allowed_new_runtime_generation_ids,
        )
        turn_record_id = await self._require_turn_record_on_cursor(
            cursor,
            turn_record_id=artifact.turn_record_id,
            work_session_id=work_session_id,
            owner_kind="ArtifactRef",
            required=False,
            allowed_new_turn_record_ids=allowed_new_turn_record_ids,
        )
        tool_invocation_id = await self._require_tool_invocation_on_cursor(
            cursor,
            tool_invocation_id=artifact.tool_invocation_id,
            work_session_id=work_session_id,
            owner_kind="ArtifactRef",
            required=False,
            allowed_new_tool_invocation_ids=allowed_new_tool_invocation_ids,
        )
        payload = artifact.to_dict()
        payload["work_session_id"] = work_session_id
        payload["runtime_generation_id"] = runtime_generation_id
        payload["turn_record_id"] = turn_record_id
        payload["tool_invocation_id"] = tool_invocation_id
        await self._execute_on_cursor(
            cursor,
            f"""
            INSERT INTO {self.schema}.artifact_refs (
                artifact_ref_id,
                turn_record_id,
                tool_invocation_id,
                work_session_id,
                runtime_generation_id,
                artifact_kind,
                storage_kind,
                payload
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (artifact_ref_id) DO UPDATE SET
                turn_record_id = EXCLUDED.turn_record_id,
                tool_invocation_id = EXCLUDED.tool_invocation_id,
                work_session_id = EXCLUDED.work_session_id,
                runtime_generation_id = EXCLUDED.runtime_generation_id,
                artifact_kind = EXCLUDED.artifact_kind,
                storage_kind = EXCLUDED.storage_kind,
                payload = EXCLUDED.payload;
            """,
            (
                artifact.artifact_ref_id,
                turn_record_id,
                tool_invocation_id,
                work_session_id,
                runtime_generation_id,
                artifact.artifact_kind.value,
                artifact.storage_kind.value,
                _json_param(payload),
            ),
        )

    async def _save_session_memory_item_on_cursor(
        self,
        cursor: Any,
        item: SessionMemoryItem,
        *,
        allowed_new_work_session_ids: tuple[str, ...] = (),
        allowed_new_runtime_generation_ids: tuple[str, ...] = (),
    ) -> None:
        work_session_id = await self._require_work_session_on_cursor(
            cursor,
            work_session_id=item.work_session_id,
            owner_kind="SessionMemoryItem",
            allowed_new_work_session_ids=allowed_new_work_session_ids,
        )
        runtime_generation_id = await self._require_runtime_generation_on_cursor(
            cursor,
            runtime_generation_id=item.runtime_generation_id,
            work_session_id=work_session_id,
            owner_kind="SessionMemoryItem",
            allowed_new_runtime_generation_ids=allowed_new_runtime_generation_ids,
        )
        created_at = _normalized_required_timestamp(
            item.created_at,
            owner_kind="SessionMemoryItem",
            field_name="created_at",
        )
        archived_at = _normalized_optional_timestamp(
            item.archived_at,
            owner_kind="SessionMemoryItem",
            field_name="archived_at",
        )
        payload = item.to_dict()
        payload["work_session_id"] = work_session_id
        payload["runtime_generation_id"] = runtime_generation_id
        payload["created_at"] = created_at
        payload["archived_at"] = archived_at
        await self._execute_on_cursor(
            cursor,
            f"""
            INSERT INTO {self.schema}.session_memory_items (
                memory_item_id,
                work_session_id,
                runtime_generation_id,
                head_kind,
                scope_id,
                memory_kind,
                created_at,
                archived_at,
                payload
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (memory_item_id) DO UPDATE SET
                work_session_id = EXCLUDED.work_session_id,
                runtime_generation_id = EXCLUDED.runtime_generation_id,
                head_kind = EXCLUDED.head_kind,
                scope_id = EXCLUDED.scope_id,
                memory_kind = EXCLUDED.memory_kind,
                created_at = EXCLUDED.created_at,
                archived_at = EXCLUDED.archived_at,
                payload = EXCLUDED.payload;
            """,
            (
                item.memory_item_id,
                work_session_id,
                runtime_generation_id,
                item.head_kind.value,
                item.scope_id,
                item.memory_kind.value,
                created_at,
                archived_at,
                _json_param(payload),
            ),
        )

    async def _save_resident_team_shell_on_cursor(
        self,
        cursor: Any,
        shell: ResidentTeamShell,
        *,
        allowed_new_work_session_ids: tuple[str, ...] = (),
        allowed_new_runtime_generation_ids: tuple[str, ...] = (),
    ) -> None:
        work_session_id = await self._require_work_session_on_cursor(
            cursor,
            work_session_id=shell.work_session_id,
            owner_kind="ResidentTeamShell",
            allowed_new_work_session_ids=allowed_new_work_session_ids,
        )
        runtime_generation_id = await self._require_runtime_generation_on_cursor(
            cursor,
            runtime_generation_id=shell.runtime_generation_id,
            work_session_id=work_session_id,
            owner_kind="ResidentTeamShell",
            allowed_new_runtime_generation_ids=allowed_new_runtime_generation_ids,
        )
        created_at = _normalized_required_timestamp(
            shell.created_at,
            owner_kind="ResidentTeamShell",
            field_name="created_at",
        )
        updated_at = _normalized_required_timestamp(
            shell.updated_at,
            owner_kind="ResidentTeamShell",
            field_name="updated_at",
        )
        last_progress_at = _normalized_required_timestamp(
            shell.last_progress_at,
            owner_kind="ResidentTeamShell",
            field_name="last_progress_at",
        )
        payload = shell.to_dict()
        payload["work_session_id"] = work_session_id
        payload["runtime_generation_id"] = runtime_generation_id
        payload["created_at"] = created_at
        payload["updated_at"] = updated_at
        payload["last_progress_at"] = last_progress_at
        await self._execute_on_cursor(
            cursor,
            f"""
            INSERT INTO {self.schema}.resident_team_shells (
                resident_team_shell_id,
                work_session_id,
                group_id,
                objective_id,
                team_id,
                lane_id,
                runtime_generation_id,
                status,
                leader_slot_session_id,
                created_at,
                updated_at,
                last_progress_at,
                payload
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (resident_team_shell_id) DO UPDATE SET
                work_session_id = EXCLUDED.work_session_id,
                group_id = EXCLUDED.group_id,
                objective_id = EXCLUDED.objective_id,
                team_id = EXCLUDED.team_id,
                lane_id = EXCLUDED.lane_id,
                runtime_generation_id = EXCLUDED.runtime_generation_id,
                status = EXCLUDED.status,
                leader_slot_session_id = EXCLUDED.leader_slot_session_id,
                updated_at = EXCLUDED.updated_at,
                last_progress_at = EXCLUDED.last_progress_at,
                payload = EXCLUDED.payload;
            """,
            (
                shell.resident_team_shell_id,
                work_session_id,
                shell.group_id,
                shell.objective_id,
                shell.team_id,
                shell.lane_id,
                runtime_generation_id,
                shell.status.value,
                shell.leader_slot_session_id,
                created_at,
                updated_at,
                last_progress_at,
                _json_param(payload),
            ),
        )

    async def _save_agent_slot_on_cursor(
        self,
        cursor: Any,
        slot: AgentSlot,
        *,
        allowed_new_work_session_ids: tuple[str, ...] = (),
    ) -> None:
        work_session_id = await self._require_work_session_on_cursor(
            cursor,
            work_session_id=slot.work_session_id,
            owner_kind="AgentSlot",
            allowed_new_work_session_ids=allowed_new_work_session_ids,
        )
        created_at = _normalized_required_timestamp(
            slot.created_at,
            owner_kind="AgentSlot",
            field_name="created_at",
        )
        updated_at = _normalized_required_timestamp(
            slot.updated_at,
            owner_kind="AgentSlot",
            field_name="updated_at",
        )
        payload = slot.to_dict()
        payload["work_session_id"] = work_session_id
        payload["created_at"] = created_at
        payload["updated_at"] = updated_at
        await self._execute_on_cursor(
            cursor,
            f"""
            INSERT INTO {self.schema}.agent_slots (
                slot_id,
                work_session_id,
                resident_team_shell_id,
                role,
                status,
                desired_state,
                preferred_backend,
                preferred_transport_class,
                current_incarnation_id,
                current_lease_id,
                restart_count,
                last_failure_class,
                created_at,
                updated_at,
                payload
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (slot_id) DO UPDATE SET
                work_session_id = EXCLUDED.work_session_id,
                resident_team_shell_id = EXCLUDED.resident_team_shell_id,
                role = EXCLUDED.role,
                status = EXCLUDED.status,
                desired_state = EXCLUDED.desired_state,
                preferred_backend = EXCLUDED.preferred_backend,
                preferred_transport_class = EXCLUDED.preferred_transport_class,
                current_incarnation_id = EXCLUDED.current_incarnation_id,
                current_lease_id = EXCLUDED.current_lease_id,
                restart_count = EXCLUDED.restart_count,
                last_failure_class = EXCLUDED.last_failure_class,
                created_at = EXCLUDED.created_at,
                updated_at = EXCLUDED.updated_at,
                payload = EXCLUDED.payload;
            """,
            (
                slot.slot_id,
                work_session_id,
                slot.resident_team_shell_id,
                slot.role,
                slot.status.value,
                slot.desired_state,
                slot.preferred_backend,
                slot.preferred_transport_class,
                slot.current_incarnation_id,
                slot.current_lease_id,
                slot.restart_count,
                slot.last_failure_class.value if slot.last_failure_class is not None else None,
                created_at,
                updated_at,
                _json_param(payload),
            ),
        )

    async def _save_agent_incarnation_on_cursor(
        self,
        cursor: Any,
        incarnation: AgentIncarnation,
        *,
        allowed_new_work_session_ids: tuple[str, ...] = (),
        allowed_new_runtime_generation_ids: tuple[str, ...] = (),
        allowed_new_slot_ids: tuple[str, ...] = (),
    ) -> None:
        work_session_id = await self._require_work_session_on_cursor(
            cursor,
            work_session_id=incarnation.work_session_id,
            owner_kind="AgentIncarnation",
            allowed_new_work_session_ids=allowed_new_work_session_ids,
        )
        slot_id = await self._require_agent_slot_on_cursor(
            cursor,
            slot_id=incarnation.slot_id,
            owner_kind="AgentIncarnation",
            expected_work_session_id=work_session_id,
            allowed_new_slot_ids=allowed_new_slot_ids,
        )
        runtime_generation_id = await self._require_runtime_generation_on_cursor(
            cursor,
            runtime_generation_id=incarnation.runtime_generation_id,
            work_session_id=work_session_id,
            owner_kind="AgentIncarnation",
            required=False,
            allowed_new_runtime_generation_ids=allowed_new_runtime_generation_ids,
        )
        started_at = _normalized_required_timestamp(
            incarnation.started_at,
            owner_kind="AgentIncarnation",
            field_name="started_at",
        )
        ended_at = _normalized_optional_timestamp(
            incarnation.ended_at,
            owner_kind="AgentIncarnation",
            field_name="ended_at",
        )
        payload = incarnation.to_dict()
        payload["slot_id"] = slot_id
        payload["work_session_id"] = work_session_id
        payload["runtime_generation_id"] = runtime_generation_id
        payload["started_at"] = started_at
        payload["ended_at"] = ended_at
        await self._execute_on_cursor(
            cursor,
            f"""
            INSERT INTO {self.schema}.agent_incarnations (
                incarnation_id,
                slot_id,
                work_session_id,
                runtime_generation_id,
                status,
                backend,
                lease_id,
                restart_generation,
                started_at,
                ended_at,
                terminal_failure_class,
                payload
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (incarnation_id) DO UPDATE SET
                slot_id = EXCLUDED.slot_id,
                work_session_id = EXCLUDED.work_session_id,
                runtime_generation_id = EXCLUDED.runtime_generation_id,
                status = EXCLUDED.status,
                backend = EXCLUDED.backend,
                lease_id = EXCLUDED.lease_id,
                restart_generation = EXCLUDED.restart_generation,
                started_at = EXCLUDED.started_at,
                ended_at = EXCLUDED.ended_at,
                terminal_failure_class = EXCLUDED.terminal_failure_class,
                payload = EXCLUDED.payload;
            """,
            (
                incarnation.incarnation_id,
                slot_id,
                work_session_id,
                runtime_generation_id,
                incarnation.status.value,
                incarnation.backend,
                incarnation.lease_id,
                incarnation.restart_generation,
                started_at,
                ended_at,
                (
                    incarnation.terminal_failure_class.value
                    if incarnation.terminal_failure_class is not None
                    else None
                ),
                _json_param(payload),
            ),
        )

    async def _append_slot_health_event_on_cursor(
        self,
        cursor: Any,
        event: SlotHealthEvent,
        *,
        allowed_new_work_session_ids: tuple[str, ...] = (),
        allowed_new_slot_ids: tuple[str, ...] = (),
        allowed_new_incarnation_ids: tuple[str, ...] = (),
    ) -> None:
        work_session_id = await self._require_work_session_on_cursor(
            cursor,
            work_session_id=event.work_session_id,
            owner_kind="SlotHealthEvent",
            allowed_new_work_session_ids=allowed_new_work_session_ids,
        )
        slot_id = await self._require_agent_slot_on_cursor(
            cursor,
            slot_id=event.slot_id,
            owner_kind="SlotHealthEvent",
            expected_work_session_id=work_session_id,
            allowed_new_slot_ids=allowed_new_slot_ids,
        )
        incarnation_id = await self._require_agent_incarnation_on_cursor(
            cursor,
            incarnation_id=event.incarnation_id,
            owner_kind="SlotHealthEvent",
            expected_work_session_id=work_session_id,
            expected_slot_id=slot_id,
            required=False,
            allowed_new_incarnation_ids=allowed_new_incarnation_ids,
        )
        observed_at = _normalized_required_timestamp(
            event.observed_at,
            owner_kind="SlotHealthEvent",
            field_name="observed_at",
        )
        payload = event.to_dict()
        payload["slot_id"] = slot_id
        payload["work_session_id"] = work_session_id
        payload["incarnation_id"] = incarnation_id
        payload["observed_at"] = observed_at
        await self._execute_on_cursor(
            cursor,
            f"""
            INSERT INTO {self.schema}.slot_health_events (
                event_id,
                slot_id,
                incarnation_id,
                work_session_id,
                event_kind,
                failure_class,
                observed_at,
                payload
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (event_id) DO UPDATE SET
                slot_id = EXCLUDED.slot_id,
                incarnation_id = EXCLUDED.incarnation_id,
                work_session_id = EXCLUDED.work_session_id,
                event_kind = EXCLUDED.event_kind,
                failure_class = EXCLUDED.failure_class,
                observed_at = EXCLUDED.observed_at,
                payload = EXCLUDED.payload;
            """,
            (
                event.event_id,
                slot_id,
                incarnation_id,
                work_session_id,
                event.event_kind,
                event.failure_class.value if event.failure_class is not None else None,
                observed_at,
                _json_param(payload),
            ),
        )

    async def _save_session_attachment_on_cursor(
        self,
        cursor: Any,
        attachment: SessionAttachment,
        *,
        allowed_new_work_session_ids: tuple[str, ...] = (),
        allowed_new_slot_ids: tuple[str, ...] = (),
        allowed_new_incarnation_ids: tuple[str, ...] = (),
    ) -> None:
        work_session_id = await self._require_work_session_on_cursor(
            cursor,
            work_session_id=attachment.work_session_id,
            owner_kind="SessionAttachment",
            allowed_new_work_session_ids=allowed_new_work_session_ids,
        )
        slot_id = None
        if attachment.slot_id:
            slot_id = await self._require_agent_slot_on_cursor(
                cursor,
                slot_id=attachment.slot_id,
                owner_kind="SessionAttachment",
                expected_work_session_id=work_session_id,
                allowed_new_slot_ids=allowed_new_slot_ids,
            )
        incarnation_id = await self._require_agent_incarnation_on_cursor(
            cursor,
            incarnation_id=attachment.incarnation_id,
            owner_kind="SessionAttachment",
            expected_work_session_id=work_session_id,
            expected_slot_id=slot_id,
            required=False,
            allowed_new_incarnation_ids=allowed_new_incarnation_ids,
        )
        attached_at = _normalized_required_timestamp(
            attachment.attached_at,
            owner_kind="SessionAttachment",
            field_name="attached_at",
        )
        detached_at = _normalized_optional_timestamp(
            attachment.detached_at,
            owner_kind="SessionAttachment",
            field_name="detached_at",
        )
        payload = attachment.to_dict()
        payload["work_session_id"] = work_session_id
        payload["slot_id"] = slot_id
        payload["incarnation_id"] = incarnation_id
        payload["attached_at"] = attached_at
        payload["detached_at"] = detached_at
        await self._execute_on_cursor(
            cursor,
            f"""
            INSERT INTO {self.schema}.session_attachments (
                attachment_id,
                work_session_id,
                resident_team_shell_id,
                slot_id,
                incarnation_id,
                client_id,
                status,
                attached_at,
                detached_at,
                last_event_id,
                payload
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (attachment_id) DO UPDATE SET
                work_session_id = EXCLUDED.work_session_id,
                resident_team_shell_id = EXCLUDED.resident_team_shell_id,
                slot_id = EXCLUDED.slot_id,
                incarnation_id = EXCLUDED.incarnation_id,
                client_id = EXCLUDED.client_id,
                status = EXCLUDED.status,
                attached_at = EXCLUDED.attached_at,
                detached_at = EXCLUDED.detached_at,
                last_event_id = EXCLUDED.last_event_id,
                payload = EXCLUDED.payload;
            """,
            (
                attachment.attachment_id,
                work_session_id,
                attachment.resident_team_shell_id,
                slot_id,
                incarnation_id,
                attachment.client_id,
                attachment.status.value,
                attached_at,
                detached_at,
                attachment.last_event_id,
                _json_param(payload),
            ),
        )

    async def _save_provider_route_health_on_cursor(
        self,
        cursor: Any,
        route: ProviderRouteHealth,
    ) -> None:
        updated_at = _normalized_required_timestamp(
            route.updated_at,
            owner_kind="ProviderRouteHealth",
            field_name="updated_at",
        )
        payload = route.to_dict()
        payload["updated_at"] = updated_at
        await self._execute_on_cursor(
            cursor,
            f"""
            INSERT INTO {self.schema}.provider_route_health (
                route_key,
                role,
                backend,
                route_fingerprint,
                status,
                health_score,
                consecutive_failures,
                last_failure_class,
                cooldown_expires_at,
                preferred,
                updated_at,
                payload
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (route_key) DO UPDATE SET
                role = EXCLUDED.role,
                backend = EXCLUDED.backend,
                route_fingerprint = EXCLUDED.route_fingerprint,
                status = EXCLUDED.status,
                health_score = EXCLUDED.health_score,
                consecutive_failures = EXCLUDED.consecutive_failures,
                last_failure_class = EXCLUDED.last_failure_class,
                cooldown_expires_at = EXCLUDED.cooldown_expires_at,
                preferred = EXCLUDED.preferred,
                updated_at = EXCLUDED.updated_at,
                payload = EXCLUDED.payload;
            """,
            (
                route.route_key,
                route.role,
                route.backend,
                route.route_fingerprint,
                route.status.value,
                route.health_score,
                route.consecutive_failures,
                route.last_failure_class.value if route.last_failure_class is not None else None,
                route.cooldown_expires_at,
                route.preferred,
                updated_at,
                _json_param(payload),
            ),
        )

    async def _select_payload_one(self, table: str, key_column: str, key_value: Any) -> Any:
        row = await self._execute(
            f"SELECT payload FROM {self.schema}.{table} WHERE {key_column} = %s;",
            (_normalize_param(key_value),),
            fetch="one",
        )
        return None if row is None else row[0]

    async def _select_payload_many(
        self,
        table: str,
        *,
        filters: tuple[tuple[str, Any], ...] = (),
        order_by: str,
    ) -> list[Any]:
        clauses: list[str] = []
        params: list[Any] = []
        for column, value in filters:
            if value is None:
                continue
            clauses.append(f"{column} = %s")
            params.append(_normalize_param(value))
        query = f"SELECT payload FROM {self.schema}.{table}"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += f" ORDER BY {order_by};"
        rows = await self._execute(query, tuple(params), fetch="all")
        return [row[0] for row in rows]

    async def bootstrap(self) -> None:
        connection = await self._connect()
        async with connection.cursor() as cursor:
            for statement in self.get_schema_statements():
                await cursor.execute(statement)
        await _maybe_await(connection.commit())

    async def healthcheck(self) -> bool:
        row = await self._execute("SELECT 1;", fetch="one")
        return bool(row and row[0] == 1)

    async def save_group(self, group: Group) -> None:
        await self._execute(
            f"""
            INSERT INTO {self.schema}.groups (group_id, display_name, metadata)
            VALUES (%s, %s, %s::jsonb)
            ON CONFLICT (group_id) DO UPDATE SET
                display_name = EXCLUDED.display_name,
                metadata = EXCLUDED.metadata;
            """,
            (
                group.group_id,
                group.display_name,
                _json_param(group.metadata),
            ),
            commit=True,
        )

    async def get_group(self, group_id: str) -> Group | None:
        row = await self._execute(
            f"SELECT group_id, display_name, metadata FROM {self.schema}.groups WHERE group_id = %s;",
            (group_id,),
            fetch="one",
        )
        return None if row is None else _deserialize_group(row)

    async def save_team(self, team: Team) -> None:
        await self._execute(
            f"""
            INSERT INTO {self.schema}.teams (team_id, group_id, name, member_ids, metadata)
            VALUES (%s, %s, %s, %s::jsonb, %s::jsonb)
            ON CONFLICT (team_id) DO UPDATE SET
                group_id = EXCLUDED.group_id,
                name = EXCLUDED.name,
                member_ids = EXCLUDED.member_ids,
                metadata = EXCLUDED.metadata;
            """,
            (
                team.team_id,
                team.group_id,
                team.name,
                _json_param(team.member_ids),
                _json_param(team.metadata),
            ),
            commit=True,
        )

    async def get_team(self, team_id: str) -> Team | None:
        row = await self._execute(
            f"SELECT team_id, group_id, name, member_ids, metadata FROM {self.schema}.teams WHERE team_id = %s;",
            (team_id,),
            fetch="one",
        )
        return None if row is None else _deserialize_team(row)

    async def list_teams(self, group_id: str) -> list[Team]:
        rows = await self._execute(
            f"SELECT team_id, group_id, name, member_ids, metadata FROM {self.schema}.teams WHERE group_id = %s ORDER BY team_id;",
            (group_id,),
            fetch="all",
        )
        return [_deserialize_team(row) for row in rows]

    async def save_objective(self, objective: ObjectiveSpec) -> None:
        await self._execute(
            f"""
            INSERT INTO {self.schema}.objectives (objective_id, group_id, title, description, payload)
            VALUES (%s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (objective_id) DO UPDATE SET
                group_id = EXCLUDED.group_id,
                title = EXCLUDED.title,
                description = EXCLUDED.description,
                payload = EXCLUDED.payload;
            """,
            (
                objective.objective_id,
                objective.group_id,
                objective.title,
                objective.description,
                _json_param(objective),
            ),
            commit=True,
        )

    async def get_objective(self, objective_id: str) -> ObjectiveSpec | None:
        payload = await self._select_payload_one("objectives", "objective_id", objective_id)
        return None if payload is None else _deserialize_objective(payload)

    async def save_spec_node(self, node: SpecNode) -> None:
        await self._execute(
            f"""
            INSERT INTO {self.schema}.spec_nodes (node_id, objective_id, kind, title, scope, lane_id, team_id, status, payload)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (node_id) DO UPDATE SET
                objective_id = EXCLUDED.objective_id,
                kind = EXCLUDED.kind,
                title = EXCLUDED.title,
                scope = EXCLUDED.scope,
                lane_id = EXCLUDED.lane_id,
                team_id = EXCLUDED.team_id,
                status = EXCLUDED.status,
                payload = EXCLUDED.payload;
            """,
            (
                node.node_id,
                node.objective_id,
                node.kind.value,
                node.title,
                node.scope.value,
                node.lane_id,
                node.team_id,
                node.status.value,
                _json_param(node),
            ),
            commit=True,
        )

    async def list_spec_nodes(self, objective_id: str) -> list[SpecNode]:
        payloads = await self._select_payload_many(
            "spec_nodes",
            filters=(("objective_id", objective_id),),
            order_by="node_id",
        )
        return [_deserialize_spec_node(payload) for payload in payloads]

    async def save_spec_edge(self, edge: SpecEdge) -> None:
        await self._execute(
            f"""
            INSERT INTO {self.schema}.spec_edges (edge_id, objective_id, kind, from_node_id, to_node_id, payload)
            VALUES (%s, %s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (edge_id) DO UPDATE SET
                objective_id = EXCLUDED.objective_id,
                kind = EXCLUDED.kind,
                from_node_id = EXCLUDED.from_node_id,
                to_node_id = EXCLUDED.to_node_id,
                payload = EXCLUDED.payload;
            """,
            (
                edge.edge_id,
                edge.objective_id,
                edge.kind.value,
                edge.from_node_id,
                edge.to_node_id,
                _json_param(edge),
            ),
            commit=True,
        )

    async def list_spec_edges(self, objective_id: str) -> list[SpecEdge]:
        payloads = await self._select_payload_many(
            "spec_edges",
            filters=(("objective_id", objective_id),),
            order_by="edge_id",
        )
        return [_deserialize_spec_edge(payload) for payload in payloads]

    async def save_task(self, task: TaskCard) -> None:
        await self._run_write(lambda cursor: self._save_task_on_cursor(cursor, task))

    async def get_task(self, task_id: str) -> TaskCard | None:
        payload = await self._select_payload_one("tasks", "task_id", task_id)
        return None if payload is None else _deserialize_task(payload)

    async def list_tasks(
        self,
        group_id: str,
        team_id: str | None = None,
        *,
        lane_id: str | None = None,
        scope: str | None = None,
    ) -> list[TaskCard]:
        filters: list[tuple[str, Any]] = [("group_id", group_id)]
        if team_id is not None:
            filters.append(("team_id", team_id))
        if lane_id is not None:
            filters.append(("lane", lane_id))
        payloads = await self._select_payload_many(
            "tasks",
            filters=tuple((column, value) for column, value in filters if value is not None),
            order_by="task_id",
        )
        tasks = [_deserialize_task(payload) for payload in payloads]
        if scope is not None:
            tasks = [task for task in tasks if task.scope.value == scope]
        return tasks

    async def upsert_task_review_slot(
        self,
        slot: TaskReviewSlot,
        revision: TaskReviewRevision,
    ) -> None:
        async def _operation(cursor: Any) -> None:
            await self._save_task_review_revision_on_cursor(cursor, revision)
            await self._save_task_review_slot_on_cursor(cursor, slot)

        await self._run_write(_operation)

    async def list_task_review_slots(self, task_id: str) -> list[TaskReviewSlot]:
        payloads = await self._select_payload_many(
            "task_review_slots",
            filters=(("task_id", task_id),),
            order_by="reviewer_agent_id",
        )
        return [_deserialize_task_review_slot(payload) for payload in payloads]

    async def list_task_review_revisions(
        self,
        task_id: str,
        reviewer_agent_id: str | None = None,
    ) -> list[TaskReviewRevision]:
        filters: list[tuple[str, Any]] = [("task_id", task_id)]
        if reviewer_agent_id is not None:
            filters.append(("reviewer_agent_id", reviewer_agent_id))
        payloads = await self._select_payload_many(
            "task_review_revisions",
            filters=tuple(filters),
            order_by="created_at, revision_id",
        )
        return [_deserialize_task_review_revision(payload) for payload in payloads]

    async def save_review_item(self, item: ReviewItemRef) -> None:
        await self._run_write(lambda cursor: self._save_review_item_on_cursor(cursor, item))

    async def get_review_item(self, item_id: str) -> ReviewItemRef | None:
        payload = await self._select_payload_one("review_items", "item_id", item_id)
        return None if payload is None else _deserialize_review_item(payload)

    async def list_review_items(
        self,
        objective_id: str,
        *,
        item_kind: ReviewItemKind | None = None,
    ) -> list[ReviewItemRef]:
        filters: list[tuple[str, Any]] = [("objective_id", objective_id)]
        if item_kind is not None:
            filters.append(("item_kind", item_kind))
        payloads = await self._select_payload_many(
            "review_items",
            filters=tuple(filters),
            order_by="item_id",
        )
        return [_deserialize_review_item(payload) for payload in payloads]

    async def save_team_position_review(self, review: TeamPositionReview) -> None:
        await self._run_write(lambda cursor: self._save_team_position_review_on_cursor(cursor, review))

    async def list_team_position_reviews(
        self,
        item_id: str,
        *,
        team_id: str | None = None,
    ) -> list[TeamPositionReview]:
        filters: list[tuple[str, Any]] = [("item_id", item_id)]
        if team_id is not None:
            filters.append(("team_id", team_id))
        payloads = await self._select_payload_many(
            "team_position_reviews",
            filters=tuple(filters),
            order_by="reviewed_at, position_review_id",
        )
        return [_deserialize_team_position_review(payload) for payload in payloads]

    async def save_cross_team_leader_review(
        self,
        review: CrossTeamLeaderReview,
    ) -> None:
        await self._run_write(
            lambda cursor: self._save_cross_team_leader_review_on_cursor(cursor, review)
        )

    async def list_cross_team_leader_reviews(
        self,
        item_id: str,
        *,
        reviewer_team_id: str | None = None,
        target_team_id: str | None = None,
    ) -> list[CrossTeamLeaderReview]:
        filters: list[tuple[str, Any]] = [("item_id", item_id)]
        if reviewer_team_id is not None:
            filters.append(("reviewer_team_id", reviewer_team_id))
        if target_team_id is not None:
            filters.append(("target_team_id", target_team_id))
        payloads = await self._select_payload_many(
            "cross_team_leader_reviews",
            filters=tuple(filters),
            order_by="reviewed_at, cross_review_id",
        )
        return [_deserialize_cross_team_leader_review(payload) for payload in payloads]

    async def save_superleader_synthesis(
        self,
        synthesis: SuperLeaderSynthesis,
    ) -> None:
        await self._run_write(
            lambda cursor: self._save_superleader_synthesis_on_cursor(cursor, synthesis)
        )

    async def get_superleader_synthesis(
        self,
        item_id: str,
    ) -> SuperLeaderSynthesis | None:
        payload = await self._select_payload_one("superleader_syntheses", "item_id", item_id)
        return None if payload is None else _deserialize_superleader_synthesis(payload)

    async def save_leader_draft_plan(self, plan: LeaderDraftPlan) -> None:
        await self._run_write(lambda cursor: self._save_leader_draft_plan_on_cursor(cursor, plan))

    async def list_leader_draft_plans(
        self,
        objective_id: str,
        *,
        planning_round_id: str | None = None,
    ) -> list[LeaderDraftPlan]:
        filters: list[tuple[str, Any]] = [("objective_id", objective_id)]
        if planning_round_id is not None:
            filters.append(("planning_round_id", planning_round_id))
        payloads = await self._select_payload_many(
            "leader_draft_plans",
            filters=tuple(filters),
            order_by="planning_round_id, leader_id, plan_id",
        )
        return [_deserialize_leader_draft_plan(payload) for payload in payloads]

    async def save_leader_peer_review(self, review: LeaderPeerReview) -> None:
        await self._run_write(lambda cursor: self._save_leader_peer_review_on_cursor(cursor, review))

    async def list_leader_peer_reviews(
        self,
        objective_id: str,
        *,
        planning_round_id: str | None = None,
    ) -> list[LeaderPeerReview]:
        filters: list[tuple[str, Any]] = [("objective_id", objective_id)]
        if planning_round_id is not None:
            filters.append(("planning_round_id", planning_round_id))
        payloads = await self._select_payload_many(
            "leader_peer_reviews",
            filters=tuple(filters),
            order_by="planning_round_id, reviewer_leader_id, target_leader_id, review_id",
        )
        return [_deserialize_leader_peer_review(payload) for payload in payloads]

    async def save_superleader_global_review(self, review: SuperLeaderGlobalReview) -> None:
        await self._run_write(
            lambda cursor: self._save_superleader_global_review_on_cursor(cursor, review)
        )

    async def get_superleader_global_review(
        self,
        objective_id: str,
        *,
        planning_round_id: str | None = None,
    ) -> SuperLeaderGlobalReview | None:
        payloads = await self._select_payload_many(
            "superleader_global_reviews",
            filters=(("objective_id", objective_id),),
            order_by="planning_round_id, review_id",
        )
        if not payloads:
            return None
        if planning_round_id is None:
            return _deserialize_superleader_global_review(payloads[-1])
        filtered = [
            payload
            for payload in payloads
            if _deserialize_superleader_global_review(payload).planning_round_id == planning_round_id
        ]
        if not filtered:
            return None
        return _deserialize_superleader_global_review(filtered[-1])

    async def save_leader_revised_plan(self, plan: LeaderRevisedPlan) -> None:
        await self._run_write(lambda cursor: self._save_leader_revised_plan_on_cursor(cursor, plan))

    async def list_leader_revised_plans(
        self,
        objective_id: str,
        *,
        planning_round_id: str | None = None,
    ) -> list[LeaderRevisedPlan]:
        filters: list[tuple[str, Any]] = [("objective_id", objective_id)]
        if planning_round_id is not None:
            filters.append(("planning_round_id", planning_round_id))
        payloads = await self._select_payload_many(
            "leader_revised_plans",
            filters=tuple(filters),
            order_by="planning_round_id, leader_id, plan_id",
        )
        return [_deserialize_leader_revised_plan(payload) for payload in payloads]

    async def save_activation_gate_decision(self, decision: ActivationGateDecision) -> None:
        await self._run_write(
            lambda cursor: self._save_activation_gate_decision_on_cursor(cursor, decision)
        )

    async def get_activation_gate_decision(
        self,
        objective_id: str,
        *,
        planning_round_id: str | None = None,
    ) -> ActivationGateDecision | None:
        payloads = await self._select_payload_many(
            "activation_gate_decisions",
            filters=(("objective_id", objective_id),),
            order_by="planning_round_id, decision_id",
        )
        if not payloads:
            return None
        if planning_round_id is None:
            return _deserialize_activation_gate_decision(payloads[-1])
        filtered = [
            payload
            for payload in payloads
            if _deserialize_activation_gate_decision(payload).planning_round_id == planning_round_id
        ]
        if not filtered:
            return None
        return _deserialize_activation_gate_decision(filtered[-1])

    async def claim_task(
        self,
        *,
        task_id: str,
        owner_id: str,
        claim_session_id: str,
        claimed_at: str,
        claim_source: str,
    ) -> TaskCard | None:
        row = await self._execute(
            f"""
            UPDATE {self.schema}.tasks
            SET status = %s,
                payload = payload || %s::jsonb
            WHERE task_id = %s
              AND status = %s
              AND COALESCE(payload ->> 'owner_id', '') = ''
              AND COALESCE(payload -> 'blocked_by', '[]'::jsonb) = '[]'::jsonb
            RETURNING payload;
            """,
            (
                TaskStatus.IN_PROGRESS.value,
                _json_param(
                    _task_claim_payload(
                        owner_id=owner_id,
                        claim_session_id=claim_session_id,
                        claimed_at=claimed_at,
                        claim_source=claim_source,
                    )
                ),
                task_id,
                TaskStatus.PENDING.value,
            ),
            commit=True,
            fetch="one",
        )
        return None if row is None else _deserialize_task(row[0])

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
        where_clauses: list[str] = [
            "group_id = %s",
            "status = %s",
            "COALESCE(payload ->> 'owner_id', '') = ''",
            "COALESCE(payload -> 'blocked_by', '[]'::jsonb) = '[]'::jsonb",
        ]
        params: list[Any] = [group_id, TaskStatus.PENDING.value]
        if team_id is not None:
            where_clauses.append("team_id = %s")
            params.append(team_id)
        if lane_id is not None:
            where_clauses.append("lane = %s")
            params.append(lane_id)
        if scope is not None:
            where_clauses.append("payload ->> 'scope' = %s")
            params.append(scope)
        params.extend(
            [
                TaskStatus.IN_PROGRESS.value,
                _json_param(
                    _task_claim_payload(
                        owner_id=owner_id,
                        claim_session_id=claim_session_id,
                        claimed_at=claimed_at,
                        claim_source=claim_source,
                    )
                ),
            ]
        )

        row = await self._execute(
            f"""
            WITH candidate AS (
                SELECT task_id
                FROM {self.schema}.tasks
                WHERE {" AND ".join(where_clauses)}
                ORDER BY task_id
                FOR UPDATE SKIP LOCKED
                LIMIT 1
            )
            UPDATE {self.schema}.tasks AS tasks
            SET status = %s,
                payload = tasks.payload || %s::jsonb
            FROM candidate
            WHERE tasks.task_id = candidate.task_id
            RETURNING tasks.payload;
            """,
            tuple(params),
            commit=True,
            fetch="one",
        )
        return None if row is None else _deserialize_task(row[0])

    async def save_handoff(self, handoff: HandoffRecord) -> None:
        await self._execute(
            f"""
            INSERT INTO {self.schema}.handoffs (handoff_id, group_id, from_team_id, to_team_id, task_id, payload)
            VALUES (%s, %s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (handoff_id) DO UPDATE SET
                group_id = EXCLUDED.group_id,
                from_team_id = EXCLUDED.from_team_id,
                to_team_id = EXCLUDED.to_team_id,
                task_id = EXCLUDED.task_id,
                payload = EXCLUDED.payload;
            """,
            (
                handoff.handoff_id,
                handoff.group_id,
                handoff.from_team_id,
                handoff.to_team_id,
                handoff.task_id,
                _json_param(handoff),
            ),
            commit=True,
        )

    async def list_handoffs(self, group_id: str) -> list[HandoffRecord]:
        payloads = await self._select_payload_many(
            "handoffs",
            filters=(("group_id", group_id),),
            order_by="handoff_id",
        )
        return [_deserialize_handoff(payload) for payload in payloads]

    async def save_authority_state(self, state: AuthorityState) -> None:
        await self._execute(
            f"""
            INSERT INTO {self.schema}.authority_states (group_id, status, payload)
            VALUES (%s, %s, %s::jsonb)
            ON CONFLICT (group_id) DO UPDATE SET
                status = EXCLUDED.status,
                payload = EXCLUDED.payload;
            """,
            (
                state.group_id,
                state.status.value,
                _json_param(state),
            ),
            commit=True,
        )

    async def get_authority_state(self, group_id: str) -> AuthorityState | None:
        payload = await self._select_payload_one("authority_states", "group_id", group_id)
        return None if payload is None else _deserialize_authority_state(payload)

    async def save_blackboard_entry(self, entry: BlackboardEntry) -> None:
        await self._run_write(lambda cursor: self._save_blackboard_entry_on_cursor(cursor, entry))

    async def list_blackboard_entries(self, blackboard_id: str) -> list[BlackboardEntry]:
        payloads = await self._select_payload_many(
            "blackboard_entries",
            filters=(("blackboard_id", blackboard_id),),
            order_by="created_at, entry_id",
        )
        return [_deserialize_blackboard_entry(payload) for payload in payloads]

    async def save_blackboard_snapshot(self, snapshot: BlackboardSnapshot) -> None:
        await self._execute(
            f"""
            INSERT INTO {self.schema}.blackboard_snapshots (
                blackboard_id, group_id, kind, lane_id, team_id, version, payload
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (blackboard_id) DO UPDATE SET
                group_id = EXCLUDED.group_id,
                kind = EXCLUDED.kind,
                lane_id = EXCLUDED.lane_id,
                team_id = EXCLUDED.team_id,
                version = EXCLUDED.version,
                payload = EXCLUDED.payload;
            """,
            (
                snapshot.blackboard_id,
                snapshot.group_id,
                snapshot.kind.value,
                snapshot.lane_id,
                snapshot.team_id,
                snapshot.version,
                _json_param(snapshot),
            ),
            commit=True,
        )

    async def get_blackboard_snapshot(self, blackboard_id: str) -> BlackboardSnapshot | None:
        payload = await self._select_payload_one("blackboard_snapshots", "blackboard_id", blackboard_id)
        return None if payload is None else _deserialize_blackboard_snapshot(payload)

    async def save_worker_record(self, record: WorkerRecord) -> None:
        await self._execute(
            f"""
            INSERT INTO {self.schema}.worker_records (worker_id, assignment_id, backend, role, status, payload)
            VALUES (%s, %s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (worker_id) DO UPDATE SET
                assignment_id = EXCLUDED.assignment_id,
                backend = EXCLUDED.backend,
                role = EXCLUDED.role,
                status = EXCLUDED.status,
                payload = EXCLUDED.payload;
            """,
            (
                record.worker_id,
                record.assignment_id,
                record.backend,
                record.role,
                record.status.value,
                _json_param(record),
            ),
            commit=True,
        )

    async def get_worker_record(self, worker_id: str) -> WorkerRecord | None:
        payload = await self._select_payload_one("worker_records", "worker_id", worker_id)
        return None if payload is None else _deserialize_worker_record(payload)

    async def list_worker_records(self) -> list[WorkerRecord]:
        payloads = await self._select_payload_many(
            "worker_records",
            order_by="worker_id",
        )
        return [_deserialize_worker_record(payload) for payload in payloads]

    async def save_agent_session(self, session: AgentSession) -> None:
        await self._run_write(lambda cursor: self._save_agent_session_on_cursor(cursor, session))

    async def get_agent_session(self, session_id: str) -> AgentSession | None:
        payload = await self._select_payload_one("agent_sessions", "session_id", session_id)
        return None if payload is None else _deserialize_agent_session(payload)

    async def list_agent_sessions(self) -> list[AgentSession]:
        payloads = await self._select_payload_many(
            "agent_sessions",
            order_by="session_id",
        )
        return [_deserialize_agent_session(payload) for payload in payloads]

    async def save_worker_session(self, session: WorkerSession) -> None:
        await self._run_write(lambda cursor: self._save_worker_session_on_cursor(cursor, session))

    async def get_worker_session(self, session_id: str) -> WorkerSession | None:
        payload = await self._select_payload_one("worker_sessions", "session_id", session_id)
        return None if payload is None else _deserialize_worker_session(payload)

    async def list_worker_sessions(self) -> list[WorkerSession]:
        payloads = await self._select_payload_many(
            "worker_sessions",
            order_by="session_id",
        )
        return [_deserialize_worker_session(payload) for payload in payloads]

    async def save_work_session(self, session: WorkSession) -> None:
        await self._run_write(lambda cursor: self._save_work_session_on_cursor(cursor, session))

    async def get_work_session(self, work_session_id: str) -> WorkSession | None:
        payload = await self._select_payload_one("work_sessions", "work_session_id", work_session_id)
        return None if payload is None else _deserialize_work_session(payload)

    async def list_work_sessions(
        self,
        group_id: str,
        *,
        root_objective_id: str | None = None,
    ) -> list[WorkSession]:
        filters: list[tuple[str, Any]] = [("group_id", group_id)]
        if root_objective_id is not None:
            filters.append(("root_objective_id", root_objective_id))
        payloads = await self._select_payload_many(
            "work_sessions",
            filters=tuple(filters),
            order_by="created_at, work_session_id",
        )
        return [_deserialize_work_session(payload) for payload in payloads]

    async def save_runtime_generation(self, generation: RuntimeGeneration) -> None:
        await self._run_write(
            lambda cursor: self._save_runtime_generation_on_cursor(cursor, generation)
        )

    async def get_runtime_generation(
        self,
        runtime_generation_id: str,
    ) -> RuntimeGeneration | None:
        payload = await self._select_payload_one(
            "runtime_generations",
            "runtime_generation_id",
            runtime_generation_id,
        )
        return None if payload is None else _deserialize_runtime_generation(payload)

    async def list_runtime_generations(
        self,
        work_session_id: str,
    ) -> list[RuntimeGeneration]:
        payloads = await self._select_payload_many(
            "runtime_generations",
            filters=(("work_session_id", work_session_id),),
            order_by="generation_index, created_at, runtime_generation_id",
        )
        return [_deserialize_runtime_generation(payload) for payload in payloads]

    async def append_work_session_message(self, message: WorkSessionMessage) -> None:
        await self._run_write(
            lambda cursor: self._append_work_session_message_on_cursor(cursor, message)
        )

    async def list_work_session_messages(
        self,
        work_session_id: str,
        *,
        runtime_generation_id: str | None = None,
    ) -> list[WorkSessionMessage]:
        filters: list[tuple[str, Any]] = [("work_session_id", work_session_id)]
        if runtime_generation_id is not None:
            filters.append(("runtime_generation_id", runtime_generation_id))
        payloads = await self._select_payload_many(
            "work_session_messages",
            filters=tuple(filters),
            order_by="created_at, message_id",
        )
        return [_deserialize_work_session_message(payload) for payload in payloads]

    async def save_conversation_head(self, head: ConversationHead) -> None:
        await self._run_write(lambda cursor: self._save_conversation_head_on_cursor(cursor, head))

    async def get_conversation_head(
        self,
        conversation_head_id: str,
    ) -> ConversationHead | None:
        payload = await self._select_payload_one(
            "conversation_heads",
            "conversation_head_id",
            conversation_head_id,
        )
        return None if payload is None else _deserialize_conversation_head(payload)

    async def list_conversation_heads(
        self,
        work_session_id: str,
        *,
        runtime_generation_id: str | None = None,
    ) -> list[ConversationHead]:
        filters: list[tuple[str, Any]] = [("work_session_id", work_session_id)]
        if runtime_generation_id is not None:
            filters.append(("runtime_generation_id", runtime_generation_id))
        payloads = await self._select_payload_many(
            "conversation_heads",
            filters=tuple(filters),
            order_by="runtime_generation_id, updated_at, conversation_head_id",
        )
        return [_deserialize_conversation_head(payload) for payload in payloads]

    async def append_session_event(self, event: SessionEvent) -> None:
        await self._run_write(lambda cursor: self._append_session_event_on_cursor(cursor, event))

    async def list_session_events(
        self,
        work_session_id: str,
        *,
        runtime_generation_id: str | None = None,
    ) -> list[SessionEvent]:
        filters: list[tuple[str, Any]] = [("work_session_id", work_session_id)]
        if runtime_generation_id is not None:
            filters.append(("runtime_generation_id", runtime_generation_id))
        payloads = await self._select_payload_many(
            "session_events",
            filters=tuple(filters),
            order_by="created_at, session_event_id",
        )
        return [_deserialize_session_event(payload) for payload in payloads]

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
        return max(
            resumable,
            key=lambda item: (item.generation_index, item.created_at, item.runtime_generation_id),
        )

    async def append_turn_record(self, record: AgentTurnRecord) -> None:
        await self._run_write(lambda cursor: self._append_turn_record_on_cursor(cursor, record))

    async def list_turn_records(
        self,
        work_session_id: str,
        *,
        runtime_generation_id: str | None = None,
        head_kind: str | None = None,
        scope_id: str | None = None,
        limit: int | None = None,
    ) -> list[AgentTurnRecord]:
        filters: list[tuple[str, Any]] = [("work_session_id", work_session_id)]
        if runtime_generation_id is not None:
            filters.append(("runtime_generation_id", runtime_generation_id))
        if head_kind is not None:
            filters.append(("head_kind", getattr(head_kind, "value", head_kind)))
        if scope_id is not None:
            filters.append(("scope_id", scope_id))
        payloads = await self._select_payload_many(
            "agent_turn_records",
            filters=tuple(filters),
            order_by="created_at, turn_record_id",
        )
        if limit is not None and limit >= 0:
            payloads = payloads[-limit:] if limit else []
        return [_deserialize_agent_turn_record(payload) for payload in payloads]

    async def append_tool_invocation_record(
        self,
        record: ToolInvocationRecord,
    ) -> None:
        await self._run_write(
            lambda cursor: self._append_tool_invocation_record_on_cursor(cursor, record)
        )

    async def list_tool_invocation_records(
        self,
        work_session_id: str,
        *,
        runtime_generation_id: str | None = None,
        turn_record_id: str | None = None,
        limit: int | None = None,
    ) -> list[ToolInvocationRecord]:
        filters: list[tuple[str, Any]] = [("work_session_id", work_session_id)]
        if runtime_generation_id is not None:
            filters.append(("runtime_generation_id", runtime_generation_id))
        if turn_record_id is not None:
            filters.append(("turn_record_id", turn_record_id))
        payloads = await self._select_payload_many(
            "tool_invocation_records",
            filters=tuple(filters),
            order_by="started_at, tool_invocation_id",
        )
        if limit is not None and limit >= 0:
            payloads = payloads[-limit:] if limit else []
        return [_deserialize_tool_invocation_record(payload) for payload in payloads]

    async def save_artifact_ref(self, artifact: ArtifactRef) -> None:
        await self._run_write(lambda cursor: self._save_artifact_ref_on_cursor(cursor, artifact))

    async def list_artifact_refs(
        self,
        work_session_id: str,
        *,
        runtime_generation_id: str | None = None,
        turn_record_id: str | None = None,
        limit: int | None = None,
    ) -> list[ArtifactRef]:
        filters: list[tuple[str, Any]] = [("work_session_id", work_session_id)]
        if runtime_generation_id is not None:
            filters.append(("runtime_generation_id", runtime_generation_id))
        if turn_record_id is not None:
            filters.append(("turn_record_id", turn_record_id))
        payloads = await self._select_payload_many(
            "artifact_refs",
            filters=tuple(filters),
            order_by="artifact_kind, artifact_ref_id",
        )
        if limit is not None and limit >= 0:
            payloads = payloads[-limit:] if limit else []
        return [_deserialize_artifact_ref(payload) for payload in payloads]

    async def save_session_memory_item(self, item: SessionMemoryItem) -> None:
        await self._run_write(
            lambda cursor: self._save_session_memory_item_on_cursor(cursor, item)
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
        filters: list[tuple[str, Any]] = [("work_session_id", work_session_id)]
        if runtime_generation_id is not None:
            filters.append(("runtime_generation_id", runtime_generation_id))
        if head_kind is not None:
            filters.append(("head_kind", getattr(head_kind, "value", head_kind)))
        if scope_id is not None:
            filters.append(("scope_id", scope_id))
        payloads = await self._select_payload_many(
            "session_memory_items",
            filters=tuple(filters),
            order_by="created_at, memory_item_id",
        )
        items = [_deserialize_session_memory_item(payload) for payload in payloads]
        if not include_archived:
            items = [item for item in items if item.archived_at is None]
        if limit is not None and limit >= 0:
            items = items[-limit:] if limit else []
        return items

    async def save_resident_team_shell(self, shell: ResidentTeamShell) -> None:
        existing = await self.get_resident_team_shell(shell.resident_team_shell_id)
        normalized_shell = ResidentTeamShell.from_payload(shell.to_dict())
        if existing is not None and existing.created_at:
            normalized_shell.created_at = existing.created_at
        await self._run_write(
            lambda cursor: self._save_resident_team_shell_on_cursor(cursor, normalized_shell)
        )

    async def get_resident_team_shell(
        self,
        resident_team_shell_id: str,
    ) -> ResidentTeamShell | None:
        payload = await self._select_payload_one(
            "resident_team_shells",
            "resident_team_shell_id",
            resident_team_shell_id,
        )
        return None if payload is None else _deserialize_resident_team_shell(payload)

    async def list_resident_team_shells(
        self,
        work_session_id: str,
    ) -> list[ResidentTeamShell]:
        payloads = await self._select_payload_many(
            "resident_team_shells",
            filters=(("work_session_id", work_session_id),),
            order_by="created_at, resident_team_shell_id",
        )
        return [_deserialize_resident_team_shell(payload) for payload in payloads]

    async def find_latest_resident_team_shell(
        self,
        work_session_id: str,
    ) -> ResidentTeamShell | None:
        shells = await self.list_resident_team_shells(work_session_id)
        if not shells:
            return None
        return max(shells, key=_resident_team_shell_latest_key)

    async def save_agent_slot(self, slot: AgentSlot) -> None:
        await self._run_write(lambda cursor: self._save_agent_slot_on_cursor(cursor, slot))

    async def get_agent_slot(self, slot_id: str) -> AgentSlot | None:
        payload = await self._select_payload_one("agent_slots", "slot_id", slot_id)
        return None if payload is None else _deserialize_agent_slot(payload)

    async def list_agent_slots(
        self,
        *,
        work_session_id: str | None = None,
        resident_team_shell_id: str | None = None,
    ) -> list[AgentSlot]:
        filters: list[tuple[str, Any]] = []
        if work_session_id is not None:
            filters.append(("work_session_id", work_session_id))
        if resident_team_shell_id is not None:
            filters.append(("resident_team_shell_id", resident_team_shell_id))
        payloads = await self._select_payload_many(
            "agent_slots",
            filters=tuple(filters),
            order_by="work_session_id, role, slot_id",
        )
        return [_deserialize_agent_slot(payload) for payload in payloads]

    async def save_agent_incarnation(self, incarnation: AgentIncarnation) -> None:
        await self._run_write(
            lambda cursor: self._save_agent_incarnation_on_cursor(cursor, incarnation)
        )

    async def get_agent_incarnation(
        self,
        incarnation_id: str,
    ) -> AgentIncarnation | None:
        payload = await self._select_payload_one(
            "agent_incarnations",
            "incarnation_id",
            incarnation_id,
        )
        return None if payload is None else _deserialize_agent_incarnation(payload)

    async def list_agent_incarnations(
        self,
        *,
        slot_id: str | None = None,
    ) -> list[AgentIncarnation]:
        filters: list[tuple[str, Any]] = []
        if slot_id is not None:
            filters.append(("slot_id", slot_id))
        payloads = await self._select_payload_many(
            "agent_incarnations",
            filters=tuple(filters),
            order_by="slot_id, restart_generation, started_at, incarnation_id",
        )
        return [_deserialize_agent_incarnation(payload) for payload in payloads]

    async def append_slot_health_event(self, event: SlotHealthEvent) -> None:
        await self._run_write(lambda cursor: self._append_slot_health_event_on_cursor(cursor, event))

    async def list_slot_health_events(
        self,
        slot_id: str,
        *,
        incarnation_id: str | None = None,
        limit: int | None = None,
    ) -> list[SlotHealthEvent]:
        filters: list[tuple[str, Any]] = [("slot_id", slot_id)]
        if incarnation_id is not None:
            filters.append(("incarnation_id", incarnation_id))
        payloads = await self._select_payload_many(
            "slot_health_events",
            filters=tuple(filters),
            order_by="observed_at, event_id",
        )
        if limit is not None and limit >= 0:
            payloads = payloads[-limit:] if limit else []
        return [_deserialize_slot_health_event(payload) for payload in payloads]

    async def save_session_attachment(self, attachment: SessionAttachment) -> None:
        await self._run_write(
            lambda cursor: self._save_session_attachment_on_cursor(cursor, attachment)
        )

    async def get_session_attachment(
        self,
        attachment_id: str,
    ) -> SessionAttachment | None:
        payload = await self._select_payload_one(
            "session_attachments",
            "attachment_id",
            attachment_id,
        )
        return None if payload is None else _deserialize_session_attachment(payload)

    async def list_session_attachments(
        self,
        work_session_id: str,
        *,
        resident_team_shell_id: str | None = None,
        include_closed: bool = True,
    ) -> list[SessionAttachment]:
        filters: list[tuple[str, Any]] = [("work_session_id", work_session_id)]
        if resident_team_shell_id is not None:
            filters.append(("resident_team_shell_id", resident_team_shell_id))
        payloads = await self._select_payload_many(
            "session_attachments",
            filters=tuple(filters),
            order_by="attached_at, attachment_id",
        )
        attachments = [_deserialize_session_attachment(payload) for payload in payloads]
        if include_closed:
            return attachments
        return [
            attachment
            for attachment in attachments
            if attachment.status
            not in (
                SessionAttachmentStatus.CLOSED,
                SessionAttachmentStatus.DETACHED,
            )
        ]

    async def save_provider_route_health(self, route: ProviderRouteHealth) -> None:
        await self._run_write(
            lambda cursor: self._save_provider_route_health_on_cursor(cursor, route)
        )

    async def get_provider_route_health(
        self,
        route_key: str,
    ) -> ProviderRouteHealth | None:
        payload = await self._select_payload_one(
            "provider_route_health",
            "route_key",
            route_key,
        )
        return None if payload is None else _deserialize_provider_route_health(payload)

    async def list_provider_route_health(
        self,
        *,
        role: str | None = None,
    ) -> list[ProviderRouteHealth]:
        filters: list[tuple[str, Any]] = []
        if role is not None:
            filters.append(("role", role))
        payloads = await self._select_payload_many(
            "provider_route_health",
            filters=tuple(filters),
            order_by="route_key",
        )
        return [_deserialize_provider_route_health(payload) for payload in payloads]

    async def list_reclaimable_worker_sessions(
        self,
        *,
        now: str,
        statuses: tuple[str, ...],
    ) -> list[WorkerSession]:
        normalized_statuses: set[str] = set()
        for status in statuses:
            value = str(_normalize_param(status))
            if value == WorkerSessionStatus.CLOSED.value:
                value = WorkerSessionStatus.ABANDONED.value
            normalized_statuses.add(value)
        if not normalized_statuses:
            return []
        reclaimable: list[WorkerSession] = []
        for session in await self.list_worker_sessions():
            if session.status.value not in normalized_statuses:
                continue
            expires_at = session.supervisor_lease_expires_at
            if expires_at is None or expires_at >= now:
                continue
            reclaimable.append(session)
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
        lease_clause = "supervisor_lease_id IS NULL"
        params: list[Any] = [
            new_supervisor_id,
            new_lease_id,
            new_expires_at,
            now,
            _json_param(
                {
                    "supervisor_id": new_supervisor_id,
                    "supervisor_lease_id": new_lease_id,
                    "supervisor_lease_expires_at": new_expires_at,
                    "last_active_at": now,
                }
            ),
            session_id,
            WorkerSessionStatus.ASSIGNED.value,
            WorkerSessionStatus.ACTIVE.value,
            now,
        ]
        if previous_lease_id is not None:
            lease_clause = "supervisor_lease_id = %s"
            params.append(previous_lease_id)

        row = await self._execute(
            f"""
            UPDATE {self.schema}.worker_sessions
            SET supervisor_id = %s,
                supervisor_lease_id = %s,
                supervisor_lease_expires_at = %s,
                last_active_at = %s,
                payload = payload || %s::jsonb
            WHERE session_id = %s
              AND status IN (%s, %s)
              AND (supervisor_lease_expires_at IS NULL OR supervisor_lease_expires_at < %s)
              AND {lease_clause}
            RETURNING payload;
            """,
            tuple(params),
            commit=True,
            fetch="one",
        )
        return None if row is None else _deserialize_worker_session(row[0])

    async def save_protocol_bus_cursor(
        self,
        *,
        stream: str,
        consumer: str,
        cursor: dict[str, Any],
    ) -> None:
        await self._run_write(
            lambda db_cursor: self._save_protocol_bus_cursor_on_cursor(
                db_cursor,
                stream=stream,
                consumer=consumer,
                cursor_payload=cursor,
            )
        )

    async def get_protocol_bus_cursor(
        self,
        *,
        stream: str,
        consumer: str,
    ) -> dict[str, Any] | None:
        row = await self._execute(
            f"""
            SELECT payload
            FROM {self.schema}.protocol_bus_cursors
            WHERE stream = %s AND consumer = %s;
            """,
            (stream, consumer),
            fetch="one",
        )
        if row is None:
            return None
        return _json_safe_copy(_json_load(row[0], default={}))

    async def _commit_session_transaction_on_cursor(
        self,
        cursor: Any,
        commit: SessionTransactionStoreCommit,
    ) -> None:
        allowed_new_work_session_ids = tuple(
            work_session.work_session_id for work_session in commit.work_sessions
        )
        allowed_new_runtime_generation_ids = tuple(
            runtime_generation.runtime_generation_id
            for runtime_generation in commit.runtime_generations
        )
        allowed_new_turn_record_ids = tuple(
            turn_record.turn_record_id for turn_record in commit.turn_records
        )
        allowed_new_tool_invocation_ids = tuple(
            tool_invocation_record.tool_invocation_id
            for tool_invocation_record in commit.tool_invocation_records
        )
        for work_session in commit.work_sessions:
            await self._save_work_session_on_cursor(cursor, work_session)
        for runtime_generation in commit.runtime_generations:
            await self._save_runtime_generation_on_cursor(
                cursor,
                runtime_generation,
                allowed_new_work_session_ids=allowed_new_work_session_ids,
            )
        for message in commit.work_session_messages:
            await self._append_work_session_message_on_cursor(
                cursor,
                message,
                allowed_new_work_session_ids=allowed_new_work_session_ids,
                allowed_new_runtime_generation_ids=allowed_new_runtime_generation_ids,
            )
        for conversation_head in commit.conversation_heads:
            await self._save_conversation_head_on_cursor(
                cursor,
                conversation_head,
                allowed_new_work_session_ids=allowed_new_work_session_ids,
                allowed_new_runtime_generation_ids=allowed_new_runtime_generation_ids,
            )
        for session_event in commit.session_events:
            await self._append_session_event_on_cursor(
                cursor,
                session_event,
                allowed_new_work_session_ids=allowed_new_work_session_ids,
                allowed_new_runtime_generation_ids=allowed_new_runtime_generation_ids,
            )
        for turn_record in commit.turn_records:
            await self._append_turn_record_on_cursor(
                cursor,
                turn_record,
                allowed_new_work_session_ids=allowed_new_work_session_ids,
                allowed_new_runtime_generation_ids=allowed_new_runtime_generation_ids,
            )
        for tool_invocation_record in commit.tool_invocation_records:
            await self._append_tool_invocation_record_on_cursor(
                cursor,
                tool_invocation_record,
                allowed_new_work_session_ids=allowed_new_work_session_ids,
                allowed_new_runtime_generation_ids=allowed_new_runtime_generation_ids,
                allowed_new_turn_record_ids=allowed_new_turn_record_ids,
            )
        for artifact_ref in commit.artifact_refs:
            await self._save_artifact_ref_on_cursor(
                cursor,
                artifact_ref,
                allowed_new_work_session_ids=allowed_new_work_session_ids,
                allowed_new_runtime_generation_ids=allowed_new_runtime_generation_ids,
                allowed_new_turn_record_ids=allowed_new_turn_record_ids,
                allowed_new_tool_invocation_ids=allowed_new_tool_invocation_ids,
            )
        for session_memory_item in commit.session_memory_items:
            await self._save_session_memory_item_on_cursor(
                cursor,
                session_memory_item,
                allowed_new_work_session_ids=allowed_new_work_session_ids,
                allowed_new_runtime_generation_ids=allowed_new_runtime_generation_ids,
            )
        for resident_team_shell in commit.resident_team_shells:
            await self._save_resident_team_shell_on_cursor(
                cursor,
                resident_team_shell,
                allowed_new_work_session_ids=allowed_new_work_session_ids,
                allowed_new_runtime_generation_ids=allowed_new_runtime_generation_ids,
            )

    async def commit_session_transaction(
        self,
        commit: SessionTransactionStoreCommit,
    ) -> None:
        await self._run_write(
            lambda cursor: self._commit_session_transaction_on_cursor(cursor, commit)
        )

    async def _commit_daemon_transaction_on_cursor(
        self,
        cursor: Any,
        commit: DaemonTransactionStoreCommit,
    ) -> None:
        allowed_new_work_session_ids = tuple(
            slot.work_session_id for slot in commit.agent_slots
        )
        allowed_new_slot_ids = tuple(slot.slot_id for slot in commit.agent_slots)
        allowed_new_incarnation_ids = tuple(
            incarnation.incarnation_id for incarnation in commit.agent_incarnations
        )
        allowed_new_runtime_generation_ids = tuple(
            incarnation.runtime_generation_id
            for incarnation in commit.agent_incarnations
            if incarnation.runtime_generation_id
        )
        for slot in commit.agent_slots:
            await self._save_agent_slot_on_cursor(
                cursor,
                slot,
                allowed_new_work_session_ids=allowed_new_work_session_ids,
            )
        for incarnation in commit.agent_incarnations:
            await self._save_agent_incarnation_on_cursor(
                cursor,
                incarnation,
                allowed_new_work_session_ids=allowed_new_work_session_ids,
                allowed_new_runtime_generation_ids=allowed_new_runtime_generation_ids,
                allowed_new_slot_ids=allowed_new_slot_ids,
            )
        for event in commit.slot_health_events:
            await self._append_slot_health_event_on_cursor(
                cursor,
                event,
                allowed_new_work_session_ids=allowed_new_work_session_ids,
                allowed_new_slot_ids=allowed_new_slot_ids,
                allowed_new_incarnation_ids=allowed_new_incarnation_ids,
            )
        for attachment in commit.session_attachments:
            await self._save_session_attachment_on_cursor(
                cursor,
                attachment,
                allowed_new_work_session_ids=allowed_new_work_session_ids,
                allowed_new_slot_ids=allowed_new_slot_ids,
                allowed_new_incarnation_ids=allowed_new_incarnation_ids,
            )
        for route in commit.provider_route_health_records:
            await self._save_provider_route_health_on_cursor(cursor, route)

    async def commit_daemon_transaction(
        self,
        commit: DaemonTransactionStoreCommit,
    ) -> None:
        await self._run_write(
            lambda cursor: self._commit_daemon_transaction_on_cursor(cursor, commit)
        )

    async def commit_coordination_transaction(
        self,
        commit: CoordinationTransactionStoreCommit,
    ) -> None:
        await self._run_write(lambda cursor: self._commit_coordination_on_cursor(cursor, commit))

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
        payloads = await self._select_payload_many(
            "coordination_outbox",
            order_by="outbox_id",
        )
        return [_deserialize_coordination_outbox_record(payload) for payload in payloads]

    async def save_delivery_state(self, state: DeliveryState) -> None:
        await self._run_write(lambda cursor: self._save_delivery_state_on_cursor(cursor, state))

    async def get_delivery_state(self, delivery_id: str) -> DeliveryState | None:
        payload = await self._select_payload_one("delivery_states", "delivery_id", delivery_id)
        return None if payload is None else _deserialize_delivery_state(payload)

    async def list_delivery_states(self, objective_id: str) -> list[DeliveryState]:
        payloads = await self._select_payload_many(
            "delivery_states",
            filters=(("objective_id", objective_id),),
            order_by="delivery_id",
        )
        return [_deserialize_delivery_state(payload) for payload in payloads]
