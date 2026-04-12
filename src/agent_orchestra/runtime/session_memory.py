from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent_orchestra.contracts.enums import WorkerStatus
from agent_orchestra.contracts.execution import (
    VerificationCommandResult,
    WorkerAssignment,
    WorkerRecord,
)
from agent_orchestra.contracts.session_continuity import ConversationHead, ConversationHeadKind
from agent_orchestra.contracts.session_memory import (
    AgentTurnActorRole,
    AgentTurnKind,
    AgentTurnRecord,
    AgentTurnStatus,
    ArtifactRef,
    ArtifactRefKind,
    ArtifactStorageKind,
    HydrationBundle,
    SessionMemoryItem,
    SessionMemoryKind,
    ToolInvocationKind,
    ToolInvocationRecord,
)
from agent_orchestra.storage.base import OrchestrationStore, SessionTransactionStoreCommit

DEFAULT_RECENT_TURN_LIMIT = 5
DEFAULT_MEMORY_ITEM_LIMIT = 10
DEFAULT_TOOL_INVOCATION_LIMIT = 10
DEFAULT_ARTIFACT_REF_LIMIT = 20
DEFAULT_SUMMARY_LIMIT = 800
DEFAULT_PROMPT_LINE_LIMIT = 400
DEFAULT_TOOL_OUTPUT_LIMIT = 400
_REDACTION_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9]{12,}"),
    re.compile(r"Bearer\s+[A-Za-z0-9._\-]{10,}"),
)


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


def _coerce_head_kind(value: object) -> ConversationHeadKind:
    if isinstance(value, ConversationHeadKind):
        return value
    try:
        return ConversationHeadKind(str(value))
    except ValueError:
        return ConversationHeadKind.WORKER


def _head_matches(
    head: ConversationHead,
    *,
    runtime_generation_id: str,
    head_kind: ConversationHeadKind,
    scope_id: str | None,
) -> bool:
    return (
        _optional_string(getattr(head, "runtime_generation_id", None)) == runtime_generation_id
        and _coerce_head_kind(getattr(head, "head_kind", None)) == head_kind
        and _optional_string(getattr(head, "scope_id", None)) == _optional_string(scope_id)
    )


def _turn_status_for_record(record: WorkerRecord) -> AgentTurnStatus:
    if record.status == WorkerStatus.COMPLETED:
        return AgentTurnStatus.COMPLETED
    metadata = record.metadata if isinstance(record.metadata, Mapping) else {}
    if metadata.get("timeout_kind") or metadata.get("termination_signal"):
        return AgentTurnStatus.INTERRUPTED
    if metadata.get("protocol_failure_reason") or metadata.get("supervisor_timeout_path"):
        return AgentTurnStatus.INTERRUPTED
    return AgentTurnStatus.FAILED


def _compact_text(value: object, *, limit: int) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    text = " ".join(text.split())
    for pattern in _REDACTION_PATTERNS:
        text = pattern.sub("REDACTED", text)
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _summary_text(value: object, *, limit: int = DEFAULT_SUMMARY_LIMIT) -> str:
    return _compact_text(value, limit=limit)


def _artifact_hash(payload: object) -> str:
    raw = json.dumps(payload, ensure_ascii=True, sort_keys=True).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        return digest.hexdigest()


def _coerce_int(value: object, *, default: int) -> int:
    try:
        if value is None:
            return default
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


def _hydration_limits_from_metadata(metadata: Mapping[str, Any] | None) -> dict[str, int]:
    payload = _mapping(metadata or {})
    hydration = _mapping(payload.get("hydration"))
    if not hydration:
        hydration = _mapping(payload.get("hydration_limits"))
    return {
        "recent_turn_limit": _coerce_int(
            hydration.get("recent_turn_limit"),
            default=DEFAULT_RECENT_TURN_LIMIT,
        ),
        "memory_item_limit": _coerce_int(
            hydration.get("memory_item_limit"),
            default=DEFAULT_MEMORY_ITEM_LIMIT,
        ),
        "tool_invocation_limit": _coerce_int(
            hydration.get("tool_invocation_limit"),
            default=DEFAULT_TOOL_INVOCATION_LIMIT,
        ),
        "artifact_ref_limit": _coerce_int(
            hydration.get("artifact_ref_limit"),
            default=DEFAULT_ARTIFACT_REF_LIMIT,
        ),
        "summary_limit": _coerce_int(
            hydration.get("summary_limit"),
            default=DEFAULT_SUMMARY_LIMIT,
        ),
    }


def _hydration_limit_overrides(metadata: Mapping[str, Any] | None) -> dict[str, int]:
    payload = _mapping(metadata or {})
    hydration = payload.get("hydration")
    if not isinstance(hydration, Mapping):
        hydration = payload.get("hydration_limits")
    if not isinstance(hydration, Mapping):
        return {}
    overrides: dict[str, int] = {}
    if "recent_turn_limit" in hydration:
        overrides["recent_turn_limit"] = _coerce_int(
            hydration.get("recent_turn_limit"),
            default=DEFAULT_RECENT_TURN_LIMIT,
        )
    if "memory_item_limit" in hydration:
        overrides["memory_item_limit"] = _coerce_int(
            hydration.get("memory_item_limit"),
            default=DEFAULT_MEMORY_ITEM_LIMIT,
        )
    if "tool_invocation_limit" in hydration:
        overrides["tool_invocation_limit"] = _coerce_int(
            hydration.get("tool_invocation_limit"),
            default=DEFAULT_TOOL_INVOCATION_LIMIT,
        )
    if "artifact_ref_limit" in hydration:
        overrides["artifact_ref_limit"] = _coerce_int(
            hydration.get("artifact_ref_limit"),
            default=DEFAULT_ARTIFACT_REF_LIMIT,
        )
    if "summary_limit" in hydration:
        overrides["summary_limit"] = _coerce_int(
            hydration.get("summary_limit"),
            default=DEFAULT_SUMMARY_LIMIT,
        )
    return overrides


def _artifact_label(artifact: ArtifactRef) -> str:
    path = _optional_string(artifact.uri_or_path) or ""
    if not path:
        return ""
    candidate = Path(path)
    if candidate.is_absolute():
        return f".../{candidate.name}"
    return path


def _tool_records_from_verification_results(
    *,
    turn_record_id: str,
    work_session_id: str,
    runtime_generation_id: str,
    verification_results: Sequence[VerificationCommandResult],
    fallback_timestamp: str,
) -> tuple[ToolInvocationRecord, ...]:
    records: list[ToolInvocationRecord] = []
    for item in verification_results:
        command = item.requested_command or item.command
        summary = f"returncode={item.returncode}"
        if item.stdout:
            summary += f"; stdout={_summary_text(item.stdout, limit=DEFAULT_TOOL_OUTPUT_LIMIT)}"
        if item.stderr:
            summary += f"; stderr={_summary_text(item.stderr, limit=DEFAULT_TOOL_OUTPUT_LIMIT)}"
        records.append(
            ToolInvocationRecord(
                turn_record_id=turn_record_id,
                work_session_id=work_session_id,
                runtime_generation_id=runtime_generation_id,
                tool_name=(item.command.split()[0] if item.command.strip() else command),
                tool_kind=ToolInvocationKind.LOCAL_COMMAND,
                input_summary=command,
                output_summary=summary,
                status="completed" if item.returncode == 0 else "failed",
                started_at=fallback_timestamp,
                completed_at=fallback_timestamp,
                metadata={
                    "command": item.command,
                    "requested_command": item.requested_command,
                    "returncode": item.returncode,
                },
            )
        )
    return tuple(records)


class SessionMemoryService:
    def __init__(self, *, store: OrchestrationStore) -> None:
        self.store = store

    def make_turn_record(
        self,
        *,
        work_session_id: str,
        runtime_generation_id: str,
        head_kind: ConversationHeadKind | str,
        scope_id: str | None,
        actor_role: AgentTurnActorRole | str,
        assignment_id: str | None,
        turn_kind: AgentTurnKind | str,
        input_summary: str,
        output_summary: str,
        response_id: str | None = None,
        status: AgentTurnStatus | str = AgentTurnStatus.COMPLETED,
        created_at: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> AgentTurnRecord:
        return AgentTurnRecord.from_payload(
            {
                "work_session_id": work_session_id,
                "runtime_generation_id": runtime_generation_id,
                "head_kind": getattr(head_kind, "value", head_kind),
                "scope_id": scope_id,
                "actor_role": getattr(actor_role, "value", actor_role),
                "assignment_id": assignment_id,
                "turn_kind": getattr(turn_kind, "value", turn_kind),
                "input_summary": input_summary,
                "output_summary": output_summary,
                "response_id": response_id,
                "status": getattr(status, "value", status),
                "created_at": created_at or _now_iso(),
                "metadata": dict(metadata or {}),
            }
        )

    def make_memory_item(
        self,
        *,
        work_session_id: str,
        runtime_generation_id: str,
        head_kind: ConversationHeadKind | str,
        scope_id: str | None,
        memory_kind: SessionMemoryKind | str,
        importance: int,
        summary: str,
        source_turn_record_ids: Sequence[str] = (),
        source_artifact_ref_ids: Sequence[str] = (),
        supersedes_memory_item_id: str | None = None,
        created_at: str | None = None,
        archived_at: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> SessionMemoryItem:
        return SessionMemoryItem.from_payload(
            {
                "work_session_id": work_session_id,
                "runtime_generation_id": runtime_generation_id,
                "head_kind": getattr(head_kind, "value", head_kind),
                "scope_id": scope_id,
                "memory_kind": getattr(memory_kind, "value", memory_kind),
                "importance": importance,
                "summary": summary,
                "source_turn_record_ids": list(source_turn_record_ids),
                "source_artifact_ref_ids": list(source_artifact_ref_ids),
                "supersedes_memory_item_id": supersedes_memory_item_id,
                "created_at": created_at or _now_iso(),
                "archived_at": archived_at,
                "metadata": dict(metadata or {}),
            }
        )

    def make_tool_invocation_record(
        self,
        *,
        work_session_id: str,
        runtime_generation_id: str,
        tool_name: str,
        tool_kind: ToolInvocationKind | str,
        input_summary: str,
        output_summary: str,
        status: str,
        turn_record_id: str | None = None,
        started_at: str | None = None,
        completed_at: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> ToolInvocationRecord:
        timestamp = started_at or _now_iso()
        return ToolInvocationRecord.from_payload(
            {
                "turn_record_id": turn_record_id,
                "work_session_id": work_session_id,
                "runtime_generation_id": runtime_generation_id,
                "tool_name": tool_name,
                "tool_kind": getattr(tool_kind, "value", tool_kind),
                "input_summary": input_summary,
                "output_summary": output_summary,
                "status": status,
                "started_at": timestamp,
                "completed_at": completed_at or timestamp,
                "metadata": dict(metadata or {}),
            }
        )

    def make_artifact_ref(
        self,
        *,
        work_session_id: str,
        runtime_generation_id: str,
        artifact_kind: ArtifactRefKind | str,
        storage_kind: ArtifactStorageKind | str,
        uri_or_path: str,
        content_hash: str,
        size_bytes: int,
        turn_record_id: str | None = None,
        tool_invocation_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> ArtifactRef:
        return ArtifactRef.from_payload(
            {
                "turn_record_id": turn_record_id,
                "tool_invocation_id": tool_invocation_id,
                "work_session_id": work_session_id,
                "runtime_generation_id": runtime_generation_id,
                "artifact_kind": getattr(artifact_kind, "value", artifact_kind),
                "storage_kind": getattr(storage_kind, "value", storage_kind),
                "uri_or_path": uri_or_path,
                "content_hash": content_hash,
                "size_bytes": size_bytes,
                "metadata": dict(metadata or {}),
            }
        )

    def make_inline_artifact_ref(
        self,
        *,
        work_session_id: str,
        runtime_generation_id: str,
        artifact_kind: ArtifactRefKind | str,
        uri_or_path: str,
        payload: object,
        turn_record_id: str | None = None,
        tool_invocation_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> ArtifactRef:
        raw = json.dumps(payload, ensure_ascii=True, sort_keys=True)
        return self.make_artifact_ref(
            work_session_id=work_session_id,
            runtime_generation_id=runtime_generation_id,
            artifact_kind=artifact_kind,
            storage_kind=ArtifactStorageKind.INLINE_JSON,
            uri_or_path=uri_or_path,
            content_hash=_artifact_hash(payload),
            size_bytes=len(raw.encode("utf-8")),
            turn_record_id=turn_record_id,
            tool_invocation_id=tool_invocation_id,
            metadata=metadata,
        )

    async def build_conversation_head(
        self,
        *,
        work_session_id: str | None,
        runtime_generation_id: str | None,
        head_kind: ConversationHeadKind | str,
        scope_id: str | None,
        checkpoint_summary: str,
        backend: str = "",
        model: str = "",
        provider: str = "",
        last_response_id: str | None = None,
        checkpoint_metadata: Mapping[str, Any] | None = None,
        source_agent_session_id: str | None = None,
        source_worker_session_id: str | None = None,
        updated_at: str | None = None,
    ) -> ConversationHead | None:
        normalized_work_session_id = _optional_string(work_session_id)
        normalized_runtime_generation_id = _optional_string(runtime_generation_id)
        if normalized_work_session_id is None or normalized_runtime_generation_id is None:
            return None
        normalized_head_kind = _coerce_head_kind(head_kind)
        normalized_scope_id = _optional_string(scope_id)
        existing_head: ConversationHead | None = None
        for head in await self.store.list_conversation_heads(
            normalized_work_session_id,
            runtime_generation_id=normalized_runtime_generation_id,
        ):
            if _head_matches(
                head,
                runtime_generation_id=normalized_runtime_generation_id,
                head_kind=normalized_head_kind,
                scope_id=normalized_scope_id,
            ):
                existing_head = head
                break
        merged_checkpoint_metadata = (
            dict(existing_head.checkpoint_metadata)
            if existing_head is not None
            else {}
        )
        merged_checkpoint_metadata.update(dict(checkpoint_metadata or {}))
        persisted = ConversationHead(
            conversation_head_id=(
                existing_head.conversation_head_id if existing_head is not None else ""
            ),
            work_session_id=normalized_work_session_id,
            runtime_generation_id=normalized_runtime_generation_id,
            head_kind=normalized_head_kind,
            scope_id=normalized_scope_id,
            backend=_optional_string(backend) or (
                "" if existing_head is None else existing_head.backend
            ),
            model=_optional_string(model) or (
                "" if existing_head is None else existing_head.model
            ),
            provider=_optional_string(provider) or (
                "" if existing_head is None else existing_head.provider
            ),
            last_response_id=(
                _optional_string(last_response_id)
                or (None if existing_head is None else existing_head.last_response_id)
            ),
            checkpoint_summary=(
                _optional_string(checkpoint_summary)
                or ("" if existing_head is None else existing_head.checkpoint_summary)
            ),
            checkpoint_metadata=merged_checkpoint_metadata,
            checkpoint_id=None if existing_head is None else existing_head.checkpoint_id,
            prompt_contract_version=(
                None if existing_head is None else existing_head.prompt_contract_version
            ),
            toolset_hash=None if existing_head is None else existing_head.toolset_hash,
            contract_fingerprint=(
                None if existing_head is None else existing_head.contract_fingerprint
            ),
            source_agent_session_id=(
                _optional_string(source_agent_session_id)
                or (None if existing_head is None else existing_head.source_agent_session_id)
            ),
            source_worker_session_id=(
                _optional_string(source_worker_session_id)
                or (None if existing_head is None else existing_head.source_worker_session_id)
            ),
            updated_at=updated_at or _now_iso(),
            invalidated_at=None if existing_head is None else existing_head.invalidated_at,
            invalidation_reason=(
                None if existing_head is None else existing_head.invalidation_reason
            ),
        )
        return persisted

    async def upsert_conversation_head(
        self,
        *,
        work_session_id: str | None,
        runtime_generation_id: str | None,
        head_kind: ConversationHeadKind | str,
        scope_id: str | None,
        checkpoint_summary: str,
        backend: str = "",
        model: str = "",
        provider: str = "",
        last_response_id: str | None = None,
        checkpoint_metadata: Mapping[str, Any] | None = None,
        source_agent_session_id: str | None = None,
        source_worker_session_id: str | None = None,
        updated_at: str | None = None,
    ) -> ConversationHead | None:
        persisted = await self.build_conversation_head(
            work_session_id=work_session_id,
            runtime_generation_id=runtime_generation_id,
            head_kind=head_kind,
            scope_id=scope_id,
            checkpoint_summary=checkpoint_summary,
            backend=backend,
            model=model,
            provider=provider,
            last_response_id=last_response_id,
            checkpoint_metadata=checkpoint_metadata,
            source_agent_session_id=source_agent_session_id,
            source_worker_session_id=source_worker_session_id,
            updated_at=updated_at,
        )
        if persisted is None:
            return None
        await self.store.commit_session_transaction(
            SessionTransactionStoreCommit(conversation_heads=(persisted,))
        )
        return persisted

    async def build_role_turn_transaction(
        self,
        *,
        work_session_id: str | None,
        runtime_generation_id: str | None,
        head_kind: ConversationHeadKind | str,
        scope_id: str | None,
        actor_role: AgentTurnActorRole | str,
        assignment_id: str | None,
        turn_kind: AgentTurnKind | str,
        input_summary: str,
        output_summary: str,
        response_id: str | None = None,
        status: AgentTurnStatus | str = AgentTurnStatus.COMPLETED,
        created_at: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        source_agent_session_id: str | None = None,
        source_worker_session_id: str | None = None,
        tool_records: Sequence[ToolInvocationRecord] = (),
        artifact_refs: Sequence[ArtifactRef] = (),
        memory_items: Sequence[SessionMemoryItem] = (),
        ensure_conversation_head: bool = False,
        head_checkpoint_summary: str | None = None,
        head_backend: str = "",
        head_model: str = "",
        head_provider: str = "",
        head_checkpoint_metadata: Mapping[str, Any] | None = None,
    ) -> tuple[SessionTransactionStoreCommit, AgentTurnRecord | None]:
        normalized_work_session_id = _optional_string(work_session_id)
        normalized_runtime_generation_id = _optional_string(runtime_generation_id)
        if normalized_work_session_id is None or normalized_runtime_generation_id is None:
            return SessionTransactionStoreCommit(), None
        timestamp = created_at or _now_iso()
        turn_record = self.make_turn_record(
            work_session_id=normalized_work_session_id,
            runtime_generation_id=normalized_runtime_generation_id,
            head_kind=head_kind,
            scope_id=scope_id,
            actor_role=actor_role,
            assignment_id=assignment_id,
            turn_kind=turn_kind,
            input_summary=input_summary,
            output_summary=output_summary,
            response_id=response_id,
            status=status,
            created_at=timestamp,
            metadata=metadata,
        )
        turn_record.source_agent_session_id = _optional_string(source_agent_session_id)
        turn_record.source_worker_session_id = _optional_string(source_worker_session_id)

        persisted_tool_records: list[ToolInvocationRecord] = []
        for item in tool_records:
            persisted = ToolInvocationRecord.from_payload(item.to_dict())
            persisted.turn_record_id = persisted.turn_record_id or turn_record.turn_record_id
            if not persisted.work_session_id:
                persisted.work_session_id = normalized_work_session_id
            if not persisted.runtime_generation_id:
                persisted.runtime_generation_id = normalized_runtime_generation_id
            persisted_tool_records.append(persisted)

        persisted_artifact_refs: list[ArtifactRef] = []
        for item in artifact_refs:
            persisted = ArtifactRef.from_payload(item.to_dict())
            persisted.turn_record_id = persisted.turn_record_id or turn_record.turn_record_id
            if not persisted.work_session_id:
                persisted.work_session_id = normalized_work_session_id
            if not persisted.runtime_generation_id:
                persisted.runtime_generation_id = normalized_runtime_generation_id
            persisted_artifact_refs.append(persisted)

        persisted_memory_items: list[SessionMemoryItem] = []
        for item in memory_items:
            persisted = SessionMemoryItem.from_payload(item.to_dict())
            if not persisted.work_session_id:
                persisted.work_session_id = normalized_work_session_id
            if not persisted.runtime_generation_id:
                persisted.runtime_generation_id = normalized_runtime_generation_id
            if not persisted.source_turn_record_ids:
                persisted.source_turn_record_ids = (turn_record.turn_record_id,)
            persisted_memory_items.append(persisted)

        conversation_heads: tuple[ConversationHead, ...] = ()
        if ensure_conversation_head:
            head = await self.build_conversation_head(
                work_session_id=normalized_work_session_id,
                runtime_generation_id=normalized_runtime_generation_id,
                head_kind=head_kind,
                scope_id=scope_id,
                checkpoint_summary=(
                    _optional_string(head_checkpoint_summary)
                    or _optional_string(output_summary)
                    or _optional_string(input_summary)
                    or ""
                ),
                backend=head_backend,
                model=head_model,
                provider=head_provider,
                last_response_id=response_id,
                checkpoint_metadata=head_checkpoint_metadata,
                source_agent_session_id=source_agent_session_id,
                source_worker_session_id=source_worker_session_id,
                updated_at=timestamp,
            )
            if head is not None:
                conversation_heads = (head,)

        return (
            SessionTransactionStoreCommit(
                conversation_heads=conversation_heads,
                turn_records=(turn_record,),
                tool_invocation_records=tuple(persisted_tool_records),
                artifact_refs=tuple(persisted_artifact_refs),
                session_memory_items=tuple(persisted_memory_items),
            ),
            turn_record,
        )

    async def record_role_turn(
        self,
        *,
        work_session_id: str | None,
        runtime_generation_id: str | None,
        head_kind: ConversationHeadKind | str,
        scope_id: str | None,
        actor_role: AgentTurnActorRole | str,
        assignment_id: str | None,
        turn_kind: AgentTurnKind | str,
        input_summary: str,
        output_summary: str,
        response_id: str | None = None,
        status: AgentTurnStatus | str = AgentTurnStatus.COMPLETED,
        created_at: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        source_agent_session_id: str | None = None,
        source_worker_session_id: str | None = None,
        tool_records: Sequence[ToolInvocationRecord] = (),
        artifact_refs: Sequence[ArtifactRef] = (),
        memory_items: Sequence[SessionMemoryItem] = (),
        ensure_conversation_head: bool = False,
        head_checkpoint_summary: str | None = None,
        head_backend: str = "",
        head_model: str = "",
        head_provider: str = "",
        head_checkpoint_metadata: Mapping[str, Any] | None = None,
    ) -> AgentTurnRecord | None:
        commit, turn_record = await self.build_role_turn_transaction(
            work_session_id=work_session_id,
            runtime_generation_id=runtime_generation_id,
            head_kind=head_kind,
            scope_id=scope_id,
            actor_role=actor_role,
            assignment_id=assignment_id,
            turn_kind=turn_kind,
            input_summary=input_summary,
            output_summary=output_summary,
            response_id=response_id,
            status=status,
            created_at=created_at,
            metadata=metadata,
            source_agent_session_id=source_agent_session_id,
            source_worker_session_id=source_worker_session_id,
            tool_records=tool_records,
            artifact_refs=artifact_refs,
            memory_items=memory_items,
            ensure_conversation_head=ensure_conversation_head,
            head_checkpoint_summary=head_checkpoint_summary,
            head_backend=head_backend,
            head_model=head_model,
            head_provider=head_provider,
            head_checkpoint_metadata=head_checkpoint_metadata,
        )
        if turn_record is None:
            return None
        await self.store.commit_session_transaction(commit)
        return turn_record

    async def build_worker_turn_transaction(
        self,
        *,
        work_session_id: str,
        runtime_generation_id: str,
        assignment: WorkerAssignment,
        record: WorkerRecord,
    ) -> tuple[SessionTransactionStoreCommit, AgentTurnRecord]:
        summary_limit = _hydration_limit_overrides(assignment.metadata).get(
            "summary_limit", DEFAULT_SUMMARY_LIMIT
        )
        head_kind, scope_id = _head_kind_and_scope_for_assignment(assignment)
        timestamp = record.ended_at or record.started_at or _now_iso()
        turn_record = self.make_turn_record(
            work_session_id=work_session_id,
            runtime_generation_id=runtime_generation_id,
            head_kind=head_kind,
            scope_id=scope_id,
            actor_role=AgentTurnActorRole.WORKER,
            assignment_id=assignment.assignment_id,
            turn_kind=AgentTurnKind.WORKER_RESULT,
            input_summary=_summary_text(assignment.input_text, limit=summary_limit),
            output_summary=_summary_text(
                record.output_text or record.error_text, limit=summary_limit
            ),
            response_id=record.response_id,
            status=_turn_status_for_record(record),
            created_at=timestamp,
            metadata={
                "backend": assignment.backend,
                "provider": assignment.metadata.get("provider"),
                "model": assignment.metadata.get("model"),
                "task_id": assignment.task_id,
                "claim_source": assignment.metadata.get("claim_source"),
                "claim_session_id": assignment.metadata.get("claim_session_id"),
            },
        )

        verification_results = tuple(
            result
            for result in (
                VerificationCommandResult.from_payload(item)
                for item in _mapping(record.metadata).get("verification_results", ())
            )
            if result is not None
        )
        tool_records = list(
            _tool_records_from_verification_results(
            turn_record_id=turn_record.turn_record_id,
            work_session_id=work_session_id,
            runtime_generation_id=runtime_generation_id,
            verification_results=verification_results,
            fallback_timestamp=timestamp,
            )
        )

        artifact_ids: list[str] = []
        metadata = _mapping(record.metadata)
        final_report = _mapping(metadata.get("final_report"))
        artifact_refs: list[ArtifactRef] = []
        if final_report:
            artifact = ArtifactRef(
                turn_record_id=turn_record.turn_record_id,
                work_session_id=work_session_id,
                runtime_generation_id=runtime_generation_id,
                artifact_kind=ArtifactRefKind.FINAL_REPORT,
                storage_kind=ArtifactStorageKind.INLINE_JSON,
                uri_or_path=f"worker-record:{record.worker_id}:final-report",
                content_hash=_artifact_hash(final_report),
                size_bytes=len(json.dumps(final_report, ensure_ascii=True)),
                metadata={"scope_id": scope_id, "head_kind": head_kind.value},
            )
            artifact_refs.append(artifact)
            artifact_ids.append(artifact.artifact_ref_id)
            for ref in final_report.get("artifact_refs", ()):
                ref_text = _optional_string(ref)
                if ref_text is None:
                    continue
                generated_artifact = ArtifactRef(
                    turn_record_id=turn_record.turn_record_id,
                    work_session_id=work_session_id,
                    runtime_generation_id=runtime_generation_id,
                    artifact_kind=ArtifactRefKind.GENERATED_FILE,
                    storage_kind=(
                        ArtifactStorageKind.EXTERNAL_REF
                        if Path(ref_text).is_absolute()
                        else ArtifactStorageKind.REPO_PATH
                    ),
                    uri_or_path=ref_text,
                    content_hash=_artifact_hash(ref_text),
                    size_bytes=len(ref_text.encode("utf-8")),
                    metadata={"scope_id": scope_id, "head_kind": head_kind.value},
                )
                artifact_refs.append(generated_artifact)
                artifact_ids.append(generated_artifact.artifact_ref_id)
        protocol_events = metadata.get("protocol_events")
        if isinstance(protocol_events, (list, tuple)) and protocol_events:
            protocol_artifact = ArtifactRef(
                turn_record_id=turn_record.turn_record_id,
                work_session_id=work_session_id,
                runtime_generation_id=runtime_generation_id,
                artifact_kind=ArtifactRefKind.PROTOCOL_EVENTS,
                storage_kind=ArtifactStorageKind.INLINE_JSON,
                uri_or_path=f"worker-record:{record.worker_id}:protocol-events",
                content_hash=_artifact_hash(protocol_events),
                size_bytes=len(json.dumps(protocol_events, ensure_ascii=True)),
                metadata={"scope_id": scope_id, "head_kind": head_kind.value},
            )
            artifact_refs.append(protocol_artifact)
            artifact_ids.append(protocol_artifact.artifact_ref_id)
        protocol_state_file = None
        if record.handle is not None and isinstance(record.handle.metadata, Mapping):
            protocol_state_file = _optional_string(record.handle.metadata.get("protocol_state_file"))
        if protocol_state_file:
            path = Path(protocol_state_file)
            artifact = ArtifactRef(
                turn_record_id=turn_record.turn_record_id,
                work_session_id=work_session_id,
                runtime_generation_id=runtime_generation_id,
                artifact_kind=ArtifactRefKind.PROTOCOL_STATE,
                storage_kind=ArtifactStorageKind.EXTERNAL_REF,
                uri_or_path=str(path),
                content_hash=_file_hash(path) if path.exists() else "",
                size_bytes=path.stat().st_size if path.exists() else 0,
                metadata={"scope_id": scope_id, "head_kind": head_kind.value},
            )
            artifact_refs.append(artifact)
            artifact_ids.append(artifact.artifact_ref_id)

        memory_updates = await self._build_memory_item_updates(
            work_session_id=work_session_id,
            runtime_generation_id=runtime_generation_id,
            head_kind=head_kind,
            scope_id=scope_id,
            turn_record=turn_record,
            final_report=final_report,
            record=record,
            artifact_ids=artifact_ids,
            timestamp=timestamp,
            summary_limit=summary_limit,
        )
        return (
            SessionTransactionStoreCommit(
                turn_records=(turn_record,),
                tool_invocation_records=tuple(tool_records),
                artifact_refs=tuple(artifact_refs),
                session_memory_items=memory_updates,
            ),
            turn_record,
        )

    async def record_worker_turn(
        self,
        *,
        work_session_id: str,
        runtime_generation_id: str,
        assignment: WorkerAssignment,
        record: WorkerRecord,
    ) -> AgentTurnRecord:
        commit, turn_record = await self.build_worker_turn_transaction(
            work_session_id=work_session_id,
            runtime_generation_id=runtime_generation_id,
            assignment=assignment,
            record=record,
        )
        await self.store.commit_session_transaction(commit)
        return turn_record

    async def _build_memory_item_updates(
        self,
        *,
        work_session_id: str,
        runtime_generation_id: str,
        head_kind: ConversationHeadKind,
        scope_id: str,
        turn_record: AgentTurnRecord,
        final_report: Mapping[str, Any],
        record: WorkerRecord,
        artifact_ids: Sequence[str],
        timestamp: str,
        summary_limit: int,
    ) -> tuple[SessionMemoryItem, ...]:
        if final_report:
            terminal_status = _optional_string(final_report.get("terminal_status")) or ""
            if terminal_status == "completed":
                summary = _summary_text(
                    final_report.get("summary") or record.output_text,
                    limit=summary_limit,
                )
                kind = SessionMemoryKind.HANDOFF
                importance = 7
            else:
                summary = _summary_text(
                    final_report.get("blocker")
                    or final_report.get("summary")
                    or record.error_text,
                    limit=summary_limit,
                )
                kind = SessionMemoryKind.OPEN_LOOP
                importance = 9
        elif record.status != WorkerStatus.COMPLETED:
            summary = _summary_text(
                record.error_text or record.output_text, limit=summary_limit
            )
            kind = SessionMemoryKind.OPEN_LOOP
            importance = 8
        else:
            summary = _summary_text(record.output_text, limit=summary_limit)
            kind = SessionMemoryKind.HANDOFF
            importance = 5
        if not summary:
            return ()
        previous_items = await self.store.list_session_memory_items(
            work_session_id,
            runtime_generation_id=runtime_generation_id,
            head_kind=head_kind,
            scope_id=scope_id,
            include_archived=False,
        )
        supersedes_memory_item_id = None
        updates: list[SessionMemoryItem] = []
        for item in reversed(previous_items):
            if item.memory_kind != kind:
                continue
            if item.summary == summary:
                return ()
            archived = self.make_memory_item(
                work_session_id=item.work_session_id,
                runtime_generation_id=item.runtime_generation_id,
                head_kind=item.head_kind,
                scope_id=item.scope_id,
                memory_kind=item.memory_kind,
                importance=item.importance,
                summary=item.summary,
                source_turn_record_ids=item.source_turn_record_ids,
                source_artifact_ref_ids=item.source_artifact_ref_ids,
                supersedes_memory_item_id=item.supersedes_memory_item_id,
                created_at=item.created_at,
                archived_at=timestamp,
                metadata=item.metadata,
            )
            archived.memory_item_id = item.memory_item_id
            updates.append(archived)
            supersedes_memory_item_id = item.memory_item_id
            break
        updates.append(
            self.make_memory_item(
                work_session_id=work_session_id,
                runtime_generation_id=runtime_generation_id,
                head_kind=head_kind,
                scope_id=scope_id,
                memory_kind=kind,
                importance=importance,
                summary=summary,
                source_turn_record_ids=(turn_record.turn_record_id,),
                source_artifact_ref_ids=artifact_ids,
                supersedes_memory_item_id=supersedes_memory_item_id,
                created_at=timestamp,
                metadata={"status": record.status.value},
            )
        )
        return tuple(updates)

    async def build_hydration_bundle(
        self,
        *,
        work_session_id: str,
        runtime_generation_id: str,
        conversation_head: ConversationHead,
        continuation_mode: str,
        runtime_status_summary: Mapping[str, Any] | None = None,
        delivery_state_summary: Mapping[str, Any] | None = None,
        mailbox_summary: Mapping[str, Any] | None = None,
        blackboard_summary: Mapping[str, Any] | None = None,
        task_surface_authority: Mapping[str, Any] | None = None,
        shell_attach_summary: Mapping[str, Any] | None = None,
        recent_turn_limit: int | None = None,
        hydration_metadata: Mapping[str, Any] | None = None,
    ) -> HydrationBundle:
        limits = {
            "recent_turn_limit": DEFAULT_RECENT_TURN_LIMIT,
            "memory_item_limit": DEFAULT_MEMORY_ITEM_LIMIT,
            "tool_invocation_limit": DEFAULT_TOOL_INVOCATION_LIMIT,
            "artifact_ref_limit": DEFAULT_ARTIFACT_REF_LIMIT,
            "summary_limit": DEFAULT_SUMMARY_LIMIT,
        }
        limits.update(_hydration_limit_overrides(getattr(conversation_head, "checkpoint_metadata", None)))
        limits.update(_hydration_limit_overrides(hydration_metadata))
        if recent_turn_limit is not None:
            limits["recent_turn_limit"] = _coerce_int(
                recent_turn_limit, default=limits["recent_turn_limit"]
            )
        head_kind = _coerce_head_kind(conversation_head.head_kind)
        recent_turns = tuple(
            await self.store.list_turn_records(
                work_session_id,
                runtime_generation_id=runtime_generation_id,
                head_kind=head_kind,
                scope_id=conversation_head.scope_id,
                limit=limits["recent_turn_limit"],
            )
        )
        recent_tool_invocations: list[ToolInvocationRecord] = []
        artifact_refs: list[ArtifactRef] = []
        for turn in recent_turns:
            recent_tool_invocations.extend(
                await self.store.list_tool_invocation_records(
                    work_session_id,
                    runtime_generation_id=runtime_generation_id,
                    turn_record_id=turn.turn_record_id,
                )
            )
            artifact_refs.extend(
                await self.store.list_artifact_refs(
                    work_session_id,
                    runtime_generation_id=runtime_generation_id,
                    turn_record_id=turn.turn_record_id,
                )
            )
        recent_tool_invocations.sort(
            key=lambda item: (item.started_at, item.tool_invocation_id)
        )
        if limits["tool_invocation_limit"] >= 0:
            recent_tool_invocations = (
                recent_tool_invocations[-limits["tool_invocation_limit"] :]
                if limits["tool_invocation_limit"]
                else []
            )
        artifact_refs.sort(key=lambda item: (item.artifact_kind.value, item.artifact_ref_id))
        if limits["artifact_ref_limit"] >= 0:
            artifact_refs = (
                artifact_refs[-limits["artifact_ref_limit"] :]
                if limits["artifact_ref_limit"]
                else []
            )
        memory_items = tuple(
            await self.store.list_session_memory_items(
                work_session_id,
                runtime_generation_id=runtime_generation_id,
                head_kind=conversation_head.head_kind,
                scope_id=conversation_head.scope_id,
                include_archived=False,
                limit=limits["memory_item_limit"],
            )
        )
        invalidated_reasons = tuple(
            reason
            for reason in (conversation_head.invalidation_reason,)
            if isinstance(reason, str) and reason.strip()
        )
        coverage = {
            "turn_count": len(recent_turns),
            "tool_invocation_count": len(recent_tool_invocations),
            "artifact_ref_count": len(artifact_refs),
            "memory_item_count": len(memory_items),
        }
        return HydrationBundle(
            work_session_id=work_session_id,
            runtime_generation_id=runtime_generation_id,
            head_kind=head_kind,
            scope_id=conversation_head.scope_id,
            conversation_head_id=conversation_head.conversation_head_id,
            continuation_mode=continuation_mode,
            last_response_id=conversation_head.last_response_id,
            checkpoint_summary=conversation_head.checkpoint_summary,
            recent_turns=recent_turns,
            recent_tool_invocations=tuple(recent_tool_invocations),
            artifact_refs=tuple(artifact_refs),
            memory_items=memory_items,
            runtime_status_summary=dict(runtime_status_summary or {}),
            delivery_state_summary=dict(delivery_state_summary or {}),
            mailbox_summary=dict(mailbox_summary or {}),
            blackboard_summary=dict(blackboard_summary or {}),
            task_surface_authority=dict(task_surface_authority or {}),
            shell_attach_summary=dict(shell_attach_summary or {}),
            invalidated_continuity_reasons=invalidated_reasons,
            bundle_created_at=_now_iso(),
            metadata={
                "backend": conversation_head.backend,
                "provider": conversation_head.provider,
                "model": conversation_head.model,
                "hydration_limits": dict(limits),
                "coverage": coverage,
            },
        )

    async def build_hydration_bundles(
        self,
        *,
        work_session_id: str,
        runtime_generation_id: str,
        conversation_heads: Sequence[ConversationHead],
        continuation_mode: str,
        runtime_status_summary: Mapping[str, Any] | None = None,
        shell_attach_summary_by_scope: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> tuple[HydrationBundle, ...]:
        bundles: list[HydrationBundle] = []
        for head in conversation_heads:
            bundles.append(
                await self.build_hydration_bundle(
                    work_session_id=work_session_id,
                    runtime_generation_id=runtime_generation_id,
                    conversation_head=head,
                    continuation_mode=continuation_mode,
                    runtime_status_summary=runtime_status_summary,
                    shell_attach_summary=(
                        {}
                        if shell_attach_summary_by_scope is None
                        else shell_attach_summary_by_scope.get(head.scope_id or "", {})
                    ),
                )
            )
        bundles.sort(key=lambda item: (item.head_kind.value, item.scope_id or ""))
        return tuple(bundles)

    def render_hydration_prompt(self, bundle: HydrationBundle) -> str:
        lines = ["Session Hydration"]
        if bundle.continuation_mode:
            lines.append(f"Continuation Mode: {bundle.continuation_mode}")
        if bundle.invalidated_continuity_reasons:
            reasons = ", ".join(bundle.invalidated_continuity_reasons)
            lines.append(f"Continuity Invalidated: {reasons}")
        if bundle.checkpoint_summary:
            lines.append(
                f"Checkpoint: {_compact_text(bundle.checkpoint_summary, limit=DEFAULT_PROMPT_LINE_LIMIT)}"
            )
        lines.append(
            "Coverage: "
            f"turns={len(bundle.recent_turns)} "
            f"memory={len(bundle.memory_items)} "
            f"artifacts={len(bundle.artifact_refs)} "
            f"tools={len(bundle.recent_tool_invocations)}"
        )
        if bundle.recent_turns:
            lines.append("Recent Turns:")
            for turn in bundle.recent_turns:
                summary = turn.output_summary or turn.input_summary
                lines.append(
                    f"- {turn.turn_kind.value}: "
                    f"{_compact_text(summary, limit=DEFAULT_PROMPT_LINE_LIMIT)}"
                )
        if bundle.memory_items:
            lines.append("Memory Items:")
            for item in bundle.memory_items:
                lines.append(
                    f"- {item.memory_kind.value}: "
                    f"{_compact_text(item.summary, limit=DEFAULT_PROMPT_LINE_LIMIT)}"
                )
        if bundle.recent_tool_invocations:
            lines.append("Recent Tools:")
            for invocation in bundle.recent_tool_invocations:
                summary = invocation.output_summary or invocation.input_summary
                lines.append(
                    f"- {invocation.tool_name}: "
                    f"{_compact_text(summary, limit=DEFAULT_PROMPT_LINE_LIMIT)}"
                )
        if bundle.artifact_refs:
            lines.append("Artifacts:")
            for artifact in bundle.artifact_refs:
                label = _artifact_label(artifact) or artifact.artifact_kind.value
                lines.append(f"- {artifact.artifact_kind.value}: {label}")
        return "\n".join(lines)


__all__ = ["SessionMemoryService"]
