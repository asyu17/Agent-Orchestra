from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any

from agent_orchestra.contracts.execution import WorkerAssignment, WorkerRecord
from agent_orchestra.contracts.enums import WorkerStatus
from agent_orchestra.contracts.session_continuity import (
    ContinuationBundle,
    ConversationHead,
    ConversationHeadKind,
    ResumeGateDecision,
    ResumeGateMode,
    RuntimeGeneration,
    RuntimeGenerationContinuityMode,
    RuntimeGenerationStatus,
    SessionEvent,
    WorkSession,
    WorkSessionMessage,
)
from agent_orchestra.contracts.session_memory import HydrationBundle
from agent_orchestra.runtime.session_memory import SessionMemoryService
from agent_orchestra.storage.base import OrchestrationStore, SessionTransactionStoreCommit


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _mapping(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): item for key, item in value.items()}


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _enum_value(value: object) -> str:
    raw = getattr(value, "value", value)
    return str(raw).strip()


def _coerce_head_kind(value: object) -> ConversationHeadKind:
    if isinstance(value, ConversationHeadKind):
        return value
    try:
        return ConversationHeadKind(_enum_value(value))
    except ValueError:
        return ConversationHeadKind.WORKER


def _coerce_runtime_status(value: object) -> RuntimeGenerationStatus:
    if isinstance(value, RuntimeGenerationStatus):
        return value
    try:
        return RuntimeGenerationStatus(_enum_value(value))
    except ValueError:
        return RuntimeGenerationStatus.BOOTING


def _head_key(head_kind: object, scope_id: object) -> tuple[str, str]:
    return (_enum_value(head_kind), _optional_string(scope_id) or "")


def _is_head_contract_compatible(
    head: object,
    expected_contract: Mapping[str, Any] | None,
) -> tuple[bool, str | None]:
    if not expected_contract:
        return True, None
    mismatches: list[str] = []
    for key in ("backend", "provider", "model"):
        expected = _optional_string(expected_contract.get(key))
        if expected is None:
            continue
        actual = _optional_string(getattr(head, key, None))
        if actual is not None and actual != expected:
            mismatches.append(key)
    if mismatches:
        return False, ", ".join(f"{key} changed" for key in mismatches)
    return True, None


def _head_kind_and_scope_for_assignment(
    assignment: WorkerAssignment,
) -> tuple[ConversationHeadKind, str]:
    role = assignment.role.strip().lower()
    if "superleader" in role:
        return ConversationHeadKind.SUPERLEADER, assignment.objective_id or assignment.worker_id
    if "leader" in role:
        return ConversationHeadKind.LEADER_LANE, assignment.lane_id or assignment.worker_id
    if "teammate" in role:
        return ConversationHeadKind.TEAMMATE_SLOT, assignment.worker_id
    return ConversationHeadKind.WORKER, assignment.worker_id


def _checkpoint_summary_from_record(record: WorkerRecord) -> str:
    if record.output_text:
        text = record.output_text.strip()
        return text if len(text) <= 1000 else text[:997] + "..."
    if record.error_text:
        text = record.error_text.strip()
        return text if len(text) <= 1000 else text[:997] + "..."
    return ""


def _hydration_summary(bundle: HydrationBundle) -> dict[str, object]:
    artifact_kinds = sorted({ref.artifact_kind.value for ref in bundle.artifact_refs})
    memory_kinds = sorted({item.memory_kind.value for item in bundle.memory_items})
    coverage = {
        "turn_count": len(bundle.recent_turns),
        "tool_invocation_count": len(bundle.recent_tool_invocations),
        "artifact_ref_count": len(bundle.artifact_refs),
        "memory_item_count": len(bundle.memory_items),
    }
    readiness = bool(
        bundle.last_response_id
        or bundle.recent_turns
        or bundle.memory_items
        or bundle.artifact_refs
    )
    prompt_ready = bool(
        not bundle.last_response_id
        and (
            bundle.checkpoint_summary
            or bundle.recent_turns
            or bundle.memory_items
            or bundle.artifact_refs
        )
    )
    return {
        "head_kind": bundle.head_kind.value,
        "scope_id": bundle.scope_id,
        "continuation_mode": bundle.continuation_mode,
        "last_response_id": bundle.last_response_id,
        "checkpoint_summary_present": bool(bundle.checkpoint_summary),
        "invalidated_continuity_reasons": list(bundle.invalidated_continuity_reasons),
        "coverage": coverage,
        "memory_kinds": memory_kinds,
        "artifact_kinds": artifact_kinds,
        "hydration_ready": readiness,
        "prompt_ready": prompt_ready,
        "bundle_created_at": bundle.bundle_created_at,
    }


@dataclass(slots=True)
class SessionContinuityState:
    work_session: WorkSession
    runtime_generation: RuntimeGeneration
    conversation_heads: tuple[ConversationHead, ...] = ()


@dataclass(slots=True)
class SessionInspectSnapshot:
    work_session: WorkSession
    runtime_generations: tuple[RuntimeGeneration, ...] = ()
    current_runtime_generation: RuntimeGeneration | None = None
    conversation_heads: tuple[ConversationHead, ...] = ()
    work_session_messages: tuple[WorkSessionMessage, ...] = ()
    session_events: tuple[SessionEvent, ...] = ()
    resume_gate: ResumeGateDecision | None = None
    continuation_bundles: tuple[ContinuationBundle, ...] = ()
    hydration_bundles: tuple[HydrationBundle, ...] = ()
    hydration_summary: tuple[dict[str, object], ...] = ()
    resident_shell_views: tuple[dict[str, object], ...] = ()
    provider_route_health: tuple[dict[str, object], ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "work_session": self.work_session.to_dict(),
            "runtime_generations": [generation.to_dict() for generation in self.runtime_generations],
            "current_runtime_generation": (
                None
                if self.current_runtime_generation is None
                else self.current_runtime_generation.to_dict()
            ),
            "conversation_heads": [head.to_dict() for head in self.conversation_heads],
            "work_session_messages": [message.to_dict() for message in self.work_session_messages],
            "session_events": [event.to_dict() for event in self.session_events],
            "resume_gate": None if self.resume_gate is None else self.resume_gate.to_dict(),
            "continuation_bundles": [bundle.to_dict() for bundle in self.continuation_bundles],
            "hydration_bundles": [bundle.to_dict() for bundle in self.hydration_bundles],
            "hydration_summary": [dict(item) for item in self.hydration_summary],
            "resident_shell_views": [dict(view) for view in self.resident_shell_views],
            "provider_route_health": [dict(item) for item in self.provider_route_health],
        }


class SessionContinuityService:
    def __init__(self, *, store: OrchestrationStore) -> None:
        self.store = store
        self._session_memory_service = SessionMemoryService(store=store)

    async def list_sessions(
        self,
        *,
        group_id: str,
        root_objective_id: str | None = None,
    ) -> tuple[WorkSession, ...]:
        sessions = await self.store.list_work_sessions(
            group_id,
            root_objective_id=root_objective_id,
        )
        sessions.sort(
            key=lambda item: (
                str(getattr(item, "updated_at", "")),
                str(getattr(item, "created_at", "")),
                str(getattr(item, "work_session_id", "")),
            )
        )
        return tuple(sessions)

    async def new_session(
        self,
        *,
        group_id: str,
        objective_id: str,
        title: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> SessionContinuityState:
        now = _now_iso()
        work_session = WorkSession(
            group_id=group_id,
            root_objective_id=objective_id,
            title=(title or "").strip(),
            status="open",
            created_at=now,
            updated_at=now,
            metadata={str(key): value for key, value in (metadata or {}).items()},
        )
        runtime_generation = RuntimeGeneration(
            work_session_id=work_session.work_session_id,
            generation_index=0,
            status=RuntimeGenerationStatus.BOOTING,
            continuity_mode=RuntimeGenerationContinuityMode.FRESH,
            created_at=now,
            group_id=group_id,
            objective_id=objective_id,
        )
        work_session.current_runtime_generation_id = runtime_generation.runtime_generation_id
        message = WorkSessionMessage(
            work_session_id=work_session.work_session_id,
            runtime_generation_id=runtime_generation.runtime_generation_id,
            role="system",
            content="New session created.",
            content_kind="summary",
            created_at=now,
            metadata={"continuity_mode": RuntimeGenerationContinuityMode.FRESH.value},
        )
        events = (
            SessionEvent(
                work_session_id=work_session.work_session_id,
                runtime_generation_id=runtime_generation.runtime_generation_id,
                event_kind="runtime_generation_started",
                payload={"continuity_mode": runtime_generation.continuity_mode.value},
                created_at=now,
            ),
            SessionEvent(
                work_session_id=work_session.work_session_id,
                runtime_generation_id=runtime_generation.runtime_generation_id,
                event_kind="new_session_created",
                payload={"objective_id": objective_id},
                created_at=now,
            ),
        )
        await self.store.commit_session_transaction(
            SessionTransactionStoreCommit(
                work_sessions=(work_session,),
                runtime_generations=(runtime_generation,),
                work_session_messages=(message,),
                session_events=events,
            )
        )
        return SessionContinuityState(
            work_session=work_session,
            runtime_generation=runtime_generation,
            conversation_heads=(),
        )

    async def warm_resume(
        self,
        *,
        work_session_id: str,
        head_contracts: Mapping[tuple[str, str], Mapping[str, Any]] | None = None,
    ) -> SessionContinuityState:
        work_session = await self._require_work_session(work_session_id)
        generations = await self.store.list_runtime_generations(work_session_id)
        source_generation = await self._resolve_source_generation(work_session_id, generations)
        now = _now_iso()
        runtime_generation = RuntimeGeneration(
            work_session_id=work_session.work_session_id,
            generation_index=(max((generation.generation_index for generation in generations), default=-1) + 1),
            status=RuntimeGenerationStatus.BOOTING,
            continuity_mode=RuntimeGenerationContinuityMode.WARM_RESUME,
            created_at=now,
            source_runtime_generation_id=source_generation.runtime_generation_id if source_generation else None,
            group_id=work_session.group_id,
            objective_id=work_session.root_objective_id,
        )
        work_session.current_runtime_generation_id = runtime_generation.runtime_generation_id
        work_session.updated_at = now
        message = WorkSessionMessage(
            work_session_id=work_session.work_session_id,
            runtime_generation_id=runtime_generation.runtime_generation_id,
            role="system",
            content="Warm resume generation created.",
            content_kind="summary",
            created_at=now,
            metadata={"continuity_mode": RuntimeGenerationContinuityMode.WARM_RESUME.value},
        )
        session_events: list[SessionEvent] = [
            SessionEvent(
                work_session_id=work_session.work_session_id,
                runtime_generation_id=runtime_generation.runtime_generation_id,
                event_kind="runtime_generation_started",
                payload={"continuity_mode": runtime_generation.continuity_mode.value},
                created_at=now,
            ),
            SessionEvent(
                work_session_id=work_session.work_session_id,
                runtime_generation_id=runtime_generation.runtime_generation_id,
                event_kind="warm_resume_started",
                payload={
                    "source_runtime_generation_id": runtime_generation.source_runtime_generation_id,
                },
                created_at=now,
            ),
        ]
        source_generation_id = (
            source_generation.runtime_generation_id if source_generation is not None else None
        )
        copied_heads: list[ConversationHead] = []
        for head in await self.store.list_conversation_heads(work_session.work_session_id):
            if source_generation_id is not None and _optional_string(getattr(head, "runtime_generation_id", None)) != source_generation_id:
                continue
            normalized_kind = _coerce_head_kind(getattr(head, "head_kind", None))
            contract = None
            if head_contracts is not None:
                contract = head_contracts.get(
                    _head_key(normalized_kind, getattr(head, "scope_id", None))
                )
            compatible, invalidation_reason = _is_head_contract_compatible(head, contract)
            copied_head = ConversationHead(
                work_session_id=work_session.work_session_id,
                runtime_generation_id=runtime_generation.runtime_generation_id,
                head_kind=normalized_kind,
                scope_id=_optional_string(getattr(head, "scope_id", None)),
                backend=_optional_string(getattr(head, "backend", None)) or "",
                model=_optional_string(getattr(head, "model", None)) or "",
                provider=_optional_string(getattr(head, "provider", None)) or "",
                last_response_id=(
                    _optional_string(getattr(head, "last_response_id", None))
                    if compatible
                    else None
                ),
                checkpoint_summary=str(getattr(head, "checkpoint_summary", "") or ""),
                checkpoint_metadata=_mapping(getattr(head, "checkpoint_metadata", {})),
                source_agent_session_id=_optional_string(getattr(head, "source_agent_session_id", None)),
                source_worker_session_id=_optional_string(getattr(head, "source_worker_session_id", None)),
                updated_at=now,
                invalidated_at=now if not compatible else None,
                invalidation_reason=invalidation_reason if not compatible else None,
            )
            copied_heads.append(copied_head)
            if not compatible:
                session_events.append(
                    SessionEvent(
                        work_session_id=work_session.work_session_id,
                        runtime_generation_id=runtime_generation.runtime_generation_id,
                        event_kind="conversation_head_invalidated",
                        payload={
                            "conversation_head_id": copied_head.conversation_head_id,
                            "reason": invalidation_reason,
                        },
                        created_at=now,
                    )
                )
        await self.store.commit_session_transaction(
            SessionTransactionStoreCommit(
                work_sessions=(work_session,),
                runtime_generations=(runtime_generation,),
                work_session_messages=(message,),
                conversation_heads=tuple(copied_heads),
                session_events=tuple(session_events),
            )
        )
        return SessionContinuityState(
            work_session=work_session,
            runtime_generation=runtime_generation,
            conversation_heads=tuple(copied_heads),
        )

    async def fork_session(
        self,
        *,
        work_session_id: str,
        title: str | None = None,
    ) -> SessionContinuityState:
        source_work_session = await self._require_work_session(work_session_id)
        source_generation = await self.store.find_latest_resumable_runtime_generation(work_session_id)
        now = _now_iso()
        forked_session = WorkSession(
            group_id=source_work_session.group_id,
            root_objective_id=source_work_session.root_objective_id,
            title=(title or source_work_session.title).strip(),
            status="open",
            created_at=now,
            updated_at=now,
            fork_origin_work_session_id=source_work_session.work_session_id,
            metadata=dict(source_work_session.metadata),
        )
        runtime_generation = RuntimeGeneration(
            work_session_id=forked_session.work_session_id,
            generation_index=0,
            status=RuntimeGenerationStatus.BOOTING,
            continuity_mode=RuntimeGenerationContinuityMode.FORK_SEED,
            created_at=now,
            source_runtime_generation_id=(
                source_generation.runtime_generation_id if source_generation is not None else None
            ),
            group_id=forked_session.group_id,
            objective_id=forked_session.root_objective_id,
        )
        forked_session.current_runtime_generation_id = runtime_generation.runtime_generation_id
        message = WorkSessionMessage(
            work_session_id=forked_session.work_session_id,
            runtime_generation_id=runtime_generation.runtime_generation_id,
            role="system",
            content="Fork session created.",
            content_kind="summary",
            created_at=now,
            metadata={"source_work_session_id": source_work_session.work_session_id},
        )
        session_events = (
            SessionEvent(
                work_session_id=forked_session.work_session_id,
                runtime_generation_id=runtime_generation.runtime_generation_id,
                event_kind="runtime_generation_started",
                payload={"continuity_mode": runtime_generation.continuity_mode.value},
                created_at=now,
            ),
            SessionEvent(
                work_session_id=forked_session.work_session_id,
                runtime_generation_id=runtime_generation.runtime_generation_id,
                event_kind="fork_created",
                payload={"source_work_session_id": source_work_session.work_session_id},
                created_at=now,
            ),
        )
        copied_heads: list[ConversationHead] = []
        source_generation_id = (
            source_generation.runtime_generation_id if source_generation is not None else None
        )
        for head in await self.store.list_conversation_heads(source_work_session.work_session_id):
            if source_generation_id is not None and _optional_string(getattr(head, "runtime_generation_id", None)) != source_generation_id:
                continue
            copied_head = ConversationHead(
                work_session_id=forked_session.work_session_id,
                runtime_generation_id=runtime_generation.runtime_generation_id,
                head_kind=_coerce_head_kind(getattr(head, "head_kind", None)),
                scope_id=_optional_string(getattr(head, "scope_id", None)),
                backend=_optional_string(getattr(head, "backend", None)) or "",
                model=_optional_string(getattr(head, "model", None)) or "",
                provider=_optional_string(getattr(head, "provider", None)) or "",
                last_response_id=None,
                checkpoint_summary=str(getattr(head, "checkpoint_summary", "") or ""),
                checkpoint_metadata=_mapping(getattr(head, "checkpoint_metadata", {})),
                source_agent_session_id=_optional_string(getattr(head, "source_agent_session_id", None)),
                source_worker_session_id=_optional_string(getattr(head, "source_worker_session_id", None)),
                updated_at=now,
                invalidated_at=now,
                invalidation_reason="fork boundary",
            )
            copied_heads.append(copied_head)
        await self.store.commit_session_transaction(
            SessionTransactionStoreCommit(
                work_sessions=(forked_session,),
                runtime_generations=(runtime_generation,),
                work_session_messages=(message,),
                conversation_heads=tuple(copied_heads),
                session_events=session_events,
            )
        )
        return SessionContinuityState(
            work_session=forked_session,
            runtime_generation=runtime_generation,
            conversation_heads=tuple(copied_heads),
        )

    async def build_continuation_bundles(
        self,
        work_session_id: str,
        runtime_generation_id: str | None = None,
    ) -> tuple[ContinuationBundle, ...]:
        work_session = await self._require_work_session(work_session_id)
        active_runtime_generation_id = runtime_generation_id or _optional_string(
            work_session.current_runtime_generation_id
        )
        current_generation = None
        if active_runtime_generation_id is not None:
            current_generation = await self.store.get_runtime_generation(active_runtime_generation_id)
        heads = await self.store.list_conversation_heads(
            work_session_id,
            runtime_generation_id=active_runtime_generation_id,
        )
        bundles: list[ContinuationBundle] = []
        for head in heads:
            head_kind = _coerce_head_kind(getattr(head, "head_kind", None))
            bundles.append(
                ContinuationBundle(
                    work_session_id=work_session_id,
                    runtime_generation_id=active_runtime_generation_id or "",
                    head_kind=head_kind,
                    scope_id=_optional_string(getattr(head, "scope_id", None)),
                    checkpoint_summary=str(getattr(head, "checkpoint_summary", "")),
                    last_response_id=_optional_string(getattr(head, "last_response_id", None)),
                    runtime_status_summary={}
                    if current_generation is None
                    else {
                        "status": current_generation.status.value,
                        "continuity_mode": current_generation.continuity_mode.value,
                        "source_runtime_generation_id": current_generation.source_runtime_generation_id,
                    },
                    metadata={
                        "backend": _optional_string(getattr(head, "backend", None)),
                        "provider": _optional_string(getattr(head, "provider", None)),
                        "model": _optional_string(getattr(head, "model", None)),
                        "checkpoint_metadata": _mapping(getattr(head, "checkpoint_metadata", {})),
                        "source_agent_session_id": _optional_string(
                            getattr(head, "source_agent_session_id", None)
                        ),
                        "source_worker_session_id": _optional_string(
                            getattr(head, "source_worker_session_id", None)
                        ),
                        "updated_at": _optional_string(getattr(head, "updated_at", None)),
                        "invalidation_reason": _optional_string(
                            getattr(head, "invalidation_reason", None)
                        ),
                    },
                )
            )
        bundles.sort(key=lambda item: (item.head_kind.value, item.scope_id or ""))
        return tuple(bundles)

    async def inspect_session(self, work_session_id: str) -> SessionInspectSnapshot:
        work_session = await self._require_work_session(work_session_id)
        runtime_generations = tuple(await self.store.list_runtime_generations(work_session_id))
        current_runtime_generation_id = _optional_string(work_session.current_runtime_generation_id)
        current_runtime_generation = None
        if current_runtime_generation_id is not None:
            current_runtime_generation = await self.store.get_runtime_generation(current_runtime_generation_id)
        if current_runtime_generation is None and runtime_generations:
            current_runtime_generation = runtime_generations[-1]
            current_runtime_generation_id = current_runtime_generation.runtime_generation_id
        conversation_heads = tuple(await self.store.list_conversation_heads(work_session_id))
        active_heads = tuple(
            head
            for head in conversation_heads
            if current_runtime_generation_id is None
            or head.runtime_generation_id == current_runtime_generation_id
        )
        runtime_status_summary = (
            {}
            if current_runtime_generation is None
            else {
                "status": current_runtime_generation.status.value,
                "continuity_mode": current_runtime_generation.continuity_mode.value,
                "source_runtime_generation_id": current_runtime_generation.source_runtime_generation_id,
            }
        )
        hydration_bundles = (
            ()
            if current_runtime_generation_id is None
            else await self._session_memory_service.build_hydration_bundles(
                work_session_id=work_session_id,
                runtime_generation_id=current_runtime_generation_id,
                conversation_heads=active_heads,
                continuation_mode=runtime_status_summary.get("continuity_mode", ""),
                runtime_status_summary=runtime_status_summary,
            )
        )
        return SessionInspectSnapshot(
            work_session=work_session,
            runtime_generations=runtime_generations,
            current_runtime_generation=current_runtime_generation,
            conversation_heads=conversation_heads,
            work_session_messages=tuple(await self.store.list_work_session_messages(work_session_id)),
            session_events=tuple(await self.store.list_session_events(work_session_id)),
            resume_gate=await self.resume_gate(work_session_id),
            continuation_bundles=await self.build_continuation_bundles(
                work_session_id,
                runtime_generation_id=current_runtime_generation_id,
            ),
            hydration_bundles=hydration_bundles,
            hydration_summary=tuple(_hydration_summary(bundle) for bundle in hydration_bundles),
        )

    async def append_session_message(
        self,
        *,
        work_session_id: str,
        content: str,
        role: str = "user",
        scope_kind: str = "session",
        scope_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> WorkSessionMessage:
        work_session = await self._require_work_session(work_session_id)
        now = _now_iso()
        runtime_generation_id = _optional_string(work_session.current_runtime_generation_id)
        work_session.updated_at = now
        message = WorkSessionMessage(
            work_session_id=work_session_id,
            runtime_generation_id=runtime_generation_id,
            role=(role or "user").strip() or "user",
            scope_kind=(scope_kind or "session").strip() or "session",
            scope_id=_optional_string(scope_id),
            content=str(content),
            content_kind="text",
            created_at=now,
            metadata={str(key): value for key, value in (metadata or {}).items()},
        )
        event = SessionEvent(
            work_session_id=work_session_id,
            runtime_generation_id=runtime_generation_id,
            event_kind="work_session_message_appended",
            payload={
                "message_id": message.message_id,
                "role": message.role,
                "scope_kind": message.scope_kind,
                "scope_id": message.scope_id,
            },
            created_at=now,
        )
        await self.store.commit_session_transaction(
            SessionTransactionStoreCommit(
                work_sessions=(work_session,),
                work_session_messages=(message,),
                session_events=(event,),
            )
        )
        return message

    async def resume_gate(self, work_session_id: str) -> ResumeGateDecision:
        work_session = await self.store.get_work_session(work_session_id)
        if work_session is None:
            return ResumeGateDecision(
                mode=ResumeGateMode.REJECT,
                reason="Unknown work session.",
                target_work_session_id=work_session_id,
                requires_user_confirmation=False,
            )
        generation = await self.store.find_latest_resumable_runtime_generation(work_session_id)
        if generation is None:
            return ResumeGateDecision(
                mode=ResumeGateMode.REJECT,
                reason="No resumable runtime generation found.",
                target_work_session_id=work_session_id,
                requires_user_confirmation=False,
            )
        metadata = _mapping(getattr(generation, "metadata", {}))
        if metadata.get("ownership_ambiguous"):
            return ResumeGateDecision(
                mode=ResumeGateMode.INSPECT_ONLY,
                reason="Runtime ownership is ambiguous.",
                target_work_session_id=work_session_id,
                target_runtime_generation_id=generation.runtime_generation_id,
                requires_user_confirmation=True,
            )
        status = _coerce_runtime_status(getattr(generation, "status", None))
        if status in {RuntimeGenerationStatus.BOOTING, RuntimeGenerationStatus.ACTIVE}:
            return ResumeGateDecision(
                mode=ResumeGateMode.EXACT_WAKE,
                reason="Runtime generation is still live or reclaimable.",
                target_work_session_id=work_session_id,
                target_runtime_generation_id=generation.runtime_generation_id,
                requires_user_confirmation=False,
            )
        if status in {RuntimeGenerationStatus.QUIESCENT, RuntimeGenerationStatus.DETACHED}:
            return ResumeGateDecision(
                mode=ResumeGateMode.WARM_RESUME,
                reason="Durable state can be rebuilt via warm resume.",
                target_work_session_id=work_session_id,
                target_runtime_generation_id=generation.runtime_generation_id,
                requires_user_confirmation=False,
            )
        return ResumeGateDecision(
            mode=ResumeGateMode.REJECT,
            reason="Runtime generation is not resumable.",
            target_work_session_id=work_session_id,
            target_runtime_generation_id=generation.runtime_generation_id,
            requires_user_confirmation=False,
        )

    async def apply_assignment_continuity(
        self,
        *,
        work_session_id: str,
        runtime_generation_id: str | None,
        assignment: WorkerAssignment,
    ) -> WorkerAssignment:
        if runtime_generation_id is None:
            return assignment
        head_kind, scope_id = _head_kind_and_scope_for_assignment(assignment)
        matched_head: ConversationHead | None = None
        for head in await self.store.list_conversation_heads(work_session_id):
            if runtime_generation_id is not None and _optional_string(getattr(head, "runtime_generation_id", None)) != runtime_generation_id:
                continue
            if _head_key(getattr(head, "head_kind", None), getattr(head, "scope_id", None)) != _head_key(head_kind, scope_id):
                continue
            head_backend = _optional_string(getattr(head, "backend", None))
            if head_backend is not None and head_backend and head_backend != assignment.backend:
                continue
            matched_head = head
            break
        if matched_head is None:
            return assignment
        runtime_generation = await self.store.get_runtime_generation(runtime_generation_id)
        runtime_status_summary = (
            {}
            if runtime_generation is None
            else {
                "status": runtime_generation.status.value,
                "continuity_mode": runtime_generation.continuity_mode.value,
                "source_runtime_generation_id": runtime_generation.source_runtime_generation_id,
            }
        )
        hydration_bundle = await self._session_memory_service.build_hydration_bundle(
            work_session_id=work_session_id,
            runtime_generation_id=runtime_generation_id,
            conversation_head=matched_head,
            continuation_mode=str(runtime_status_summary.get("continuity_mode", "")),
            runtime_status_summary=runtime_status_summary,
            hydration_metadata=assignment.metadata,
        )
        metadata = dict(assignment.metadata)
        metadata["hydration_bundle"] = hydration_bundle.to_dict()
        last_response_id = _optional_string(getattr(matched_head, "last_response_id", None))
        if _optional_string(getattr(matched_head, "invalidation_reason", None)):
            last_response_id = None
        if assignment.previous_response_id:
            return replace(assignment, metadata=metadata)
        if last_response_id is not None:
            return replace(
                assignment,
                previous_response_id=last_response_id,
                metadata=metadata,
            )
        hydration_prompt = self._session_memory_service.render_hydration_prompt(
            hydration_bundle
        )
        if not hydration_prompt.strip():
            return replace(assignment, metadata=metadata)
        metadata["hydration_applied"] = True
        return replace(
            assignment,
            input_text=f"{hydration_prompt}\n\n{assignment.input_text}".strip(),
            metadata=metadata,
        )

    async def record_worker_turn(
        self,
        *,
        work_session_id: str,
        runtime_generation_id: str | None,
        assignment: WorkerAssignment,
        record: WorkerRecord,
    ) -> ConversationHead | None:
        if runtime_generation_id is None:
            work_session = await self.store.get_work_session(work_session_id)
            runtime_generation_id = (
                None
                if work_session is None
                else _optional_string(work_session.current_runtime_generation_id)
            )
        if runtime_generation_id is None:
            return None
        now = _now_iso()
        head_kind, scope_id = _head_kind_and_scope_for_assignment(assignment)
        existing_head: object | None = None
        for head in await self.store.list_conversation_heads(work_session_id):
            if _optional_string(getattr(head, "runtime_generation_id", None)) != runtime_generation_id:
                continue
            if _head_key(getattr(head, "head_kind", None), getattr(head, "scope_id", None)) == _head_key(head_kind, scope_id):
                existing_head = head
                break
        updated_head: ConversationHead | None = None
        if record.status == WorkerStatus.COMPLETED:
            provider = _optional_string(assignment.metadata.get("provider")) or _optional_string(
                getattr(existing_head, "provider", None)
            ) or ""
            model = _optional_string(assignment.metadata.get("model")) or _optional_string(
                getattr(existing_head, "model", None)
            ) or ""
            updated_head = ConversationHead(
                conversation_head_id=_optional_string(getattr(existing_head, "conversation_head_id", None)) or "",
                work_session_id=work_session_id,
                runtime_generation_id=runtime_generation_id,
                head_kind=head_kind,
                scope_id=scope_id,
                backend=assignment.backend,
                model=model,
                provider=provider,
                last_response_id=record.response_id,
                checkpoint_summary=_checkpoint_summary_from_record(record),
                checkpoint_metadata=_mapping(getattr(existing_head, "checkpoint_metadata", {})),
                checkpoint_id=_optional_string(getattr(existing_head, "checkpoint_id", None)),
                prompt_contract_version=_optional_string(
                    getattr(existing_head, "prompt_contract_version", None)
                ),
                toolset_hash=_optional_string(getattr(existing_head, "toolset_hash", None)),
                contract_fingerprint=_optional_string(
                    getattr(existing_head, "contract_fingerprint", None)
                ),
                source_agent_session_id=_optional_string(
                    record.metadata.get("agent_session_id") if isinstance(record.metadata, Mapping) else None
                ) or _optional_string(getattr(existing_head, "source_agent_session_id", None)),
                source_worker_session_id=_optional_string(
                    record.metadata.get("worker_session_id") if isinstance(record.metadata, Mapping) else None
                ) or _optional_string(getattr(existing_head, "source_worker_session_id", None)),
                updated_at=now,
            )
        memory_commit, _ = await self._session_memory_service.build_worker_turn_transaction(
            work_session_id=work_session_id,
            runtime_generation_id=runtime_generation_id,
            assignment=assignment,
            record=record,
        )
        await self.store.commit_session_transaction(
            SessionTransactionStoreCommit(
                conversation_heads=(
                    tuple() if updated_head is None else (updated_head,)
                ),
                session_events=(
                    ()
                    if updated_head is None
                    else (
                        SessionEvent(
                            work_session_id=work_session_id,
                            runtime_generation_id=runtime_generation_id,
                            event_kind="conversation_head_updated",
                            payload={
                                "conversation_head_id": updated_head.conversation_head_id,
                                "scope_id": updated_head.scope_id,
                                "head_kind": updated_head.head_kind.value,
                            },
                            created_at=now,
                        ),
                    )
                ),
                turn_records=memory_commit.turn_records,
                tool_invocation_records=memory_commit.tool_invocation_records,
                artifact_refs=memory_commit.artifact_refs,
                session_memory_items=memory_commit.session_memory_items,
            )
        )
        return updated_head

    async def _require_work_session(self, work_session_id: str) -> WorkSession:
        work_session = await self.store.get_work_session(work_session_id)
        if work_session is None:
            raise ValueError(f"Unknown work_session_id: {work_session_id}")
        return work_session

    async def _resolve_source_generation(
        self,
        work_session_id: str,
        generations: list[RuntimeGeneration] | tuple[RuntimeGeneration, ...],
    ) -> RuntimeGeneration | None:
        if generations:
            return generations[-1]
        return await self.store.find_latest_resumable_runtime_generation(work_session_id)


__all__ = [
    "ConversationHead",
    "SessionInspectSnapshot",
    "SessionContinuityService",
    "SessionContinuityState",
]
