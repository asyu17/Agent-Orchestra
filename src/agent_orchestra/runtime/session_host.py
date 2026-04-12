from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace
from datetime import datetime, timezone
import json
import hashlib
from typing import Any

from agent_orchestra.contracts.agent import (
    AgentSession,
    TeammateActivationProfile,
    AgentWorkerSessionTruth,
    CoordinatorSessionState,
    SessionBinding,
    TeammateSlotSessionState,
)
from agent_orchestra.contracts.execution import (
    ResidentCoordinatorPhase,
    ResidentCoordinatorSession,
    WorkerSession,
    WorkerSessionStatus,
)
from agent_orchestra.contracts.session_continuity import (
    ResidentTeamShell,
    ResidentTeamShellStatus,
    ShellAttachDecision,
    ShellAttachDecisionMode,
    ConversationHeadKind,
)
from agent_orchestra.contracts.session_memory import (
    AgentTurnActorRole,
    AgentTurnKind,
    AgentTurnRecord,
    AgentTurnStatus,
    ArtifactRef,
    ArtifactRefKind,
    ArtifactStorageKind,
    ToolInvocationKind,
    ToolInvocationRecord,
)
from agent_orchestra.runtime.session_memory import SessionMemoryService
from agent_orchestra.storage.base import MailboxConsumeStoreCommit, OrchestrationStore, ProtocolBusCursorCommit
from agent_orchestra.tools.permission_protocol import PermissionDecision, PermissionRequest


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_iso_timestamp(value: object) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        datetime.fromisoformat(value)
    except ValueError:
        return False
    return True


def _authority_completion_status(metadata: Mapping[str, Any]) -> str:
    decision_raw = metadata.get("authority_last_decision")
    decision = decision_raw.strip().lower() if isinstance(decision_raw, str) else ""
    waiting = bool(metadata.get("authority_waiting"))
    relay_consumed = bool(metadata.get("authority_relay_consumed"))
    wake_recorded = bool(metadata.get("authority_wake_recorded"))
    if decision == "grant":
        return "grant_resumed" if wake_recorded else "incomplete"
    if decision == "reroute":
        return "reroute_closed"
    if decision == "deny":
        return "deny_closed"
    if decision == "escalate":
        return "waiting"
    if waiting:
        return "relay_pending" if relay_consumed else "waiting"
    if relay_consumed:
        return "relay_pending"
    return "incomplete"


def _patch_authority_completion_status(metadata: dict[str, Any]) -> None:
    metadata["authority_completion_status"] = _authority_completion_status(metadata)


def _teammate_slot_from_agent_id(agent_id: str) -> int | None:
    marker = ":teammate:"
    if marker not in agent_id:
        return None
    suffix = agent_id.rsplit(marker, 1)[1].strip()
    try:
        slot = int(suffix)
    except ValueError:
        return None
    return slot if slot > 0 else None


def _activation_profile_is_runnable(profile: TeammateActivationProfile) -> bool:
    return (
        not profile.is_empty()
        and profile.backend is not None
        and profile.working_dir is not None
    )


def _coordinator_session_sort_key(
    session: CoordinatorSessionState,
) -> tuple[str, int, int, int, str]:
    return (
        session.last_progress_at or "",
        session.cycle_count,
        session.prompt_turn_count,
        session.mailbox_poll_count,
        session.session_id,
    )


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _mapping_payload(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): item for key, item in value.items()}


def _summary_text(value: object, *, limit: int = 400) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _artifact_hash(payload: object) -> str:
    raw = json.dumps(payload, ensure_ascii=True, sort_keys=True).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _continuity_ids_from_metadata(
    metadata: Mapping[str, Any],
) -> tuple[str | None, str | None]:
    work_session_id = _optional_string(metadata.get("work_session_id"))
    runtime_generation_id = _optional_string(metadata.get("runtime_generation_id"))
    if not work_session_id or not runtime_generation_id:
        return None, None
    return work_session_id, runtime_generation_id


def _resident_shell_approval_queue(metadata: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    queue = metadata.get("approval_queue")
    if not isinstance(queue, Mapping):
        return {}
    normalized: dict[str, dict[str, Any]] = {}
    for raw_kind, raw_entry in queue.items():
        approval_kind = _optional_string(raw_kind)
        if approval_kind is None:
            continue
        normalized[approval_kind] = _mapping_payload(raw_entry)
    return normalized


def _resident_shell_approval_status(
    metadata: Mapping[str, Any],
    *,
    approval_kind: str,
    target_session_id: str | None = None,
) -> str | None:
    approval_entry = _resident_shell_approval_queue(metadata).get(approval_kind, {})
    if target_session_id is not None:
        target_entries = approval_entry.get("targets")
        if isinstance(target_entries, Mapping):
            target_entry = target_entries.get(target_session_id)
            if isinstance(target_entry, Mapping):
                status = _optional_string(target_entry.get("status"))
            else:
                status = _optional_string(approval_entry.get("status"))
        else:
            status = _optional_string(approval_entry.get("status"))
    else:
        status = _optional_string(approval_entry.get("status"))
    if status not in {"pending", "approved", "denied"}:
        return None
    return status


def _resident_team_shell_status_from_phase(
    phase: ResidentCoordinatorPhase,
) -> ResidentTeamShellStatus:
    mapping = {
        ResidentCoordinatorPhase.BOOTING: ResidentTeamShellStatus.BOOTING,
        ResidentCoordinatorPhase.RUNNING: ResidentTeamShellStatus.ATTACHED,
        ResidentCoordinatorPhase.IDLE: ResidentTeamShellStatus.IDLE,
        ResidentCoordinatorPhase.WAITING_FOR_MAILBOX: ResidentTeamShellStatus.WAITING_FOR_MAILBOX,
        ResidentCoordinatorPhase.WAITING_FOR_SUBORDINATES: ResidentTeamShellStatus.WAITING_FOR_SUBORDINATES,
        ResidentCoordinatorPhase.QUIESCENT: ResidentTeamShellStatus.QUIESCENT,
        ResidentCoordinatorPhase.SHUTDOWN_REQUESTED: ResidentTeamShellStatus.CLOSED,
        ResidentCoordinatorPhase.FAILED: ResidentTeamShellStatus.FAILED,
    }
    return mapping.get(phase, ResidentTeamShellStatus.ATTACHED)


def _resident_team_shell_sort_key(
    shell: ResidentTeamShell,
) -> tuple[str, str, str, str]:
    return (
        shell.last_progress_at or "",
        shell.updated_at or "",
        shell.created_at or "",
        shell.resident_team_shell_id,
    )


def _resident_team_shell_id_from_scope(
    *,
    work_session_id: str,
    objective_id: str,
    lane_id: str,
    team_id: str,
) -> str:
    digest = hashlib.sha1(
        f"{work_session_id}|{objective_id}|{lane_id}|{team_id}".encode("utf-8")
    ).hexdigest()[:16]
    return f"residentteamshell_{digest}"


class ResidentSessionHost(ABC):
    def _session_memory_service(self) -> SessionMemoryService | None:
        store = getattr(self, "store", None)
        if store is None:
            return None
        service = getattr(self, "_session_memory_service_instance", None)
        if service is None:
            service = SessionMemoryService(store=store)
            setattr(self, "_session_memory_service_instance", service)
        return service

    async def _record_turn(
        self,
        *,
        work_session_id: str | None,
        runtime_generation_id: str | None,
        head_kind: ConversationHeadKind,
        scope_id: str | None,
        actor_role: AgentTurnActorRole,
        turn_kind: AgentTurnKind,
        input_summary: str,
        output_summary: str,
        status: AgentTurnStatus = AgentTurnStatus.COMPLETED,
        metadata: dict[str, object] | None = None,
    ) -> AgentTurnRecord | None:
        service = self._session_memory_service()
        if service is None:
            return None
        turn_record = await service.record_role_turn(
            work_session_id=work_session_id,
            runtime_generation_id=runtime_generation_id,
            head_kind=head_kind,
            scope_id=scope_id,
            actor_role=actor_role,
            assignment_id=None,
            turn_kind=turn_kind,
            input_summary=_summary_text(input_summary),
            output_summary=_summary_text(output_summary),
            status=status,
            created_at=_now_iso(),
            metadata=metadata,
            ensure_conversation_head=True,
            head_checkpoint_summary=_summary_text(output_summary),
            head_backend="resident_session_host",
            head_model="session_host",
            head_provider="agent_orchestra",
        )
        return turn_record

    async def _record_tool_invocation(
        self,
        *,
        turn_record: AgentTurnRecord | None,
        tool_kind: ToolInvocationKind,
        tool_name: str,
        input_summary: str,
        output_summary: str,
        metadata: dict[str, object] | None = None,
    ) -> None:
        if turn_record is None:
            return
        store = getattr(self, "store", None)
        if store is None:
            return
        tool_record = ToolInvocationRecord(
            turn_record_id=turn_record.turn_record_id,
            work_session_id=turn_record.work_session_id,
            runtime_generation_id=turn_record.runtime_generation_id,
            tool_name=tool_name,
            tool_kind=tool_kind,
            input_summary=_summary_text(input_summary),
            output_summary=_summary_text(output_summary),
            status="completed",
            started_at=turn_record.created_at,
            completed_at=turn_record.created_at,
            metadata=dict(metadata or {}),
        )
        await store.append_tool_invocation_record(tool_record)

    async def _record_tool_invocation_without_turn(
        self,
        *,
        work_session_id: str | None,
        runtime_generation_id: str | None,
        tool_kind: ToolInvocationKind,
        tool_name: str,
        input_summary: str,
        output_summary: str,
        metadata: dict[str, object] | None = None,
    ) -> None:
        store = getattr(self, "store", None)
        if store is None or work_session_id is None or runtime_generation_id is None:
            return
        tool_record = ToolInvocationRecord(
            turn_record_id=None,
            work_session_id=work_session_id,
            runtime_generation_id=runtime_generation_id,
            tool_name=tool_name,
            tool_kind=tool_kind,
            input_summary=_summary_text(input_summary),
            output_summary=_summary_text(output_summary),
            status="completed",
            started_at=_now_iso(),
            completed_at=_now_iso(),
            metadata=dict(metadata or {}),
        )
        await store.append_tool_invocation_record(tool_record)

    async def _record_artifact(
        self,
        *,
        turn_record: AgentTurnRecord | None,
        artifact_kind: ArtifactRefKind,
        uri: str,
        payload: object,
        metadata: dict[str, object] | None = None,
    ) -> None:
        if turn_record is None:
            return
        store = getattr(self, "store", None)
        if store is None:
            return
        serialized = json.dumps(payload, ensure_ascii=True)
        artifact = ArtifactRef(
            turn_record_id=turn_record.turn_record_id,
            work_session_id=turn_record.work_session_id,
            runtime_generation_id=turn_record.runtime_generation_id,
            artifact_kind=artifact_kind,
            storage_kind=ArtifactStorageKind.INLINE_JSON,
            uri_or_path=uri,
            content_hash=_artifact_hash(payload),
            size_bytes=len(serialized),
            metadata=dict(metadata or {}),
        )
        await store.save_artifact_ref(artifact)

    @staticmethod
    def _sync_worker_session_truth(session: AgentSession) -> AgentSession:
        truth = AgentWorkerSessionTruth.from_agent_session(session)
        metadata = dict(session.metadata)
        include_none_keys: tuple[str, ...] = ()
        if (
            "bound_worker_session_id" in metadata
            or truth.bound_worker_session_id is not None
        ):
            include_none_keys += ("bound_worker_session_id",)
        if (
            "current_worker_session_id" in metadata
            or truth.current_worker_session_id is not None
        ):
            include_none_keys += ("current_worker_session_id",)
        metadata.update(
            truth.to_metadata_patch(
                include_none_keys=include_none_keys,
            )
        )
        if truth.last_worker_session_id is None:
            metadata.pop("last_worker_session_id", None)
        return replace(
            session,
            current_worker_session_id=truth.current_worker_session_id,
            last_worker_session_id=truth.last_worker_session_id,
            metadata=metadata,
        )

    @staticmethod
    def _binding_with_worker_session_lease(
        *,
        binding: SessionBinding | None,
        worker_session: WorkerSession,
    ) -> SessionBinding | None:
        if binding is None:
            return None
        synchronized = SessionBinding.from_dict(binding.to_dict())
        return replace(
            synchronized,
            supervisor_id=worker_session.supervisor_id,
            lease_id=worker_session.supervisor_lease_id,
            lease_expires_at=worker_session.supervisor_lease_expires_at,
        )

    async def register_session(self, session: AgentSession) -> AgentSession:
        return await self.save_session(session)

    async def load_or_create_session(self, session: AgentSession) -> AgentSession:
        current = await self.load_session(session.session_id)
        if current is not None:
            return current
        return await self.register_session(session)

    async def record_worker_session_projection(
        self,
        *,
        worker_session: WorkerSession,
        binding: SessionBinding | None = None,
        phase: ResidentCoordinatorPhase,
        reason: str = "",
    ) -> AgentSession:
        projected = await self.project_worker_session_state(
            worker_session=worker_session,
            binding=binding,
            phase=phase,
            reason=reason,
        )
        return await self.save_session(projected)

    async def load_or_create_slot_session(
        self,
        *,
        session_id: str,
        agent_id: str,
        objective_id: str | None = None,
        lane_id: str | None = None,
        team_id: str | None = None,
        role: str = "teammate",
        phase: ResidentCoordinatorPhase = ResidentCoordinatorPhase.IDLE,
        metadata: Mapping[str, Any] | None = None,
    ) -> AgentSession:
        slot_metadata = {"activation_epoch": 1}
        if metadata is not None:
            slot_metadata.update({str(key): value for key, value in metadata.items()})
        return await self.load_or_create_session(
            AgentSession(
                session_id=session_id,
                agent_id=agent_id,
                role=role,
                phase=phase,
                objective_id=objective_id,
                lane_id=lane_id,
                team_id=team_id,
                metadata=slot_metadata,
            )
        )

    async def load_teammate_activation_profile(
        self,
        session_id: str,
    ) -> TeammateActivationProfile | None:
        session = await self.load_session(session_id)
        if session is None:
            return None
        profile = TeammateActivationProfile.from_metadata(session.metadata)
        return None if profile.is_empty() else profile

    async def record_teammate_activation_profile(
        self,
        session_id: str,
        *,
        activation_profile: TeammateActivationProfile,
    ) -> AgentSession:
        if activation_profile.is_empty():
            current = await self.load_session(session_id)
            if current is None:
                raise ValueError(f"Unknown session_id: {session_id}")
            return current
        current = await self.load_session(session_id)
        if current is None:
            raise ValueError(f"Unknown session_id: {session_id}")
        metadata = dict(current.metadata)
        metadata["activation_profile"] = activation_profile.to_metadata_payload()
        return await self.update_session(
            session_id,
            metadata=metadata,
        )

    async def load_or_create_coordinator_session(
        self,
        *,
        session_id: str,
        coordinator_id: str,
        objective_id: str,
        lane_id: str | None = None,
        team_id: str | None = None,
        role: str = "leader",
        phase: ResidentCoordinatorPhase = ResidentCoordinatorPhase.BOOTING,
        host_owner_coordinator_id: str | None = None,
        runtime_task_id: str | None = None,
        mailbox_cursor: str | None = None,
        last_progress_at: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> AgentSession:
        coordinator_metadata = {
            str(key): value for key, value in (metadata or {}).items()
        }
        projected_state = CoordinatorSessionState(
            session_id=session_id,
            coordinator_id=coordinator_id,
            role=role,
            phase=phase,
            objective_id=objective_id,
            lane_id=lane_id,
            team_id=team_id,
            host_owner_coordinator_id=host_owner_coordinator_id,
            runtime_task_id=runtime_task_id,
            mailbox_cursor=mailbox_cursor,
            last_progress_at=last_progress_at,
            metadata=coordinator_metadata,
        )
        return await self.load_or_create_session(
            AgentSession(
                session_id=session_id,
                agent_id=coordinator_id,
                role=role,
                phase=phase,
                objective_id=objective_id,
                lane_id=lane_id,
                team_id=team_id,
                mailbox_cursor=CoordinatorSessionState.mailbox_cursor_payload(mailbox_cursor),
                last_progress_at=last_progress_at,
                metadata=projected_state.to_metadata_patch(),
            )
        )

    async def load_coordinator_session(
        self,
        session_id: str,
    ) -> CoordinatorSessionState | None:
        session = await self.load_session(session_id)
        if session is None:
            return None
        return CoordinatorSessionState.from_agent_session(session)

    @abstractmethod
    async def _save_persisted_resident_team_shell(
        self,
        shell: ResidentTeamShell,
    ) -> ResidentTeamShell:
        raise NotImplementedError

    @abstractmethod
    async def _get_persisted_resident_team_shell(
        self,
        resident_team_shell_id: str,
    ) -> ResidentTeamShell | None:
        raise NotImplementedError

    @abstractmethod
    async def _list_persisted_resident_team_shells(
        self,
        *,
        work_session_id: str | None = None,
    ) -> tuple[ResidentTeamShell, ...]:
        raise NotImplementedError

    @staticmethod
    def _resident_team_shell_scope_from_session(
        session: AgentSession,
    ) -> tuple[str, str, str, str] | None:
        objective_id = _optional_string(session.objective_id)
        lane_id = _optional_string(session.lane_id)
        if objective_id is None or lane_id is None:
            return None
        metadata = session.metadata if isinstance(session.metadata, Mapping) else {}
        work_session_id = _optional_string(metadata.get("work_session_id")) or ""
        team_id = _optional_string(session.team_id) or _optional_string(metadata.get("team_id")) or ""
        return (work_session_id, objective_id, lane_id, team_id)

    @staticmethod
    def _resident_team_shell_scope_from_shell(
        shell: ResidentTeamShell,
    ) -> tuple[str, str, str, str] | None:
        objective_id = _optional_string(shell.objective_id)
        lane_id = _optional_string(shell.lane_id)
        if objective_id is None or lane_id is None:
            return None
        return (
            _optional_string(shell.work_session_id) or "",
            objective_id,
            lane_id,
            _optional_string(shell.team_id) or "",
        )

    @staticmethod
    def _resident_team_shell_scope_matches(
        *,
        scope: tuple[str, str, str, str],
        work_session_id: str | None = None,
        objective_id: str | None = None,
        lane_id: str | None = None,
        team_id: str | None = None,
    ) -> bool:
        scope_work_session_id, scope_objective_id, scope_lane_id, scope_team_id = scope
        if work_session_id is not None and scope_work_session_id != work_session_id:
            return False
        if objective_id is not None and scope_objective_id != objective_id:
            return False
        if lane_id is not None and scope_lane_id != lane_id:
            return False
        if team_id is not None and scope_team_id != team_id:
            return False
        return True

    async def _find_persisted_resident_team_shell_for_scope(
        self,
        scope: tuple[str, str, str, str],
    ) -> ResidentTeamShell | None:
        work_session_id, objective_id, lane_id, team_id = scope
        shells = await self._list_persisted_resident_team_shells(
            work_session_id=work_session_id or None,
        )
        matched = [
            shell
            for shell in shells
            if self._resident_team_shell_scope_from_shell(shell) == scope
            or (
                shell.objective_id == objective_id
                and shell.lane_id == lane_id
                and (shell.team_id or "") == team_id
            )
        ]
        if not matched:
            return None
        return max(matched, key=_resident_team_shell_sort_key)

    async def _resident_team_shell_sessions_for_scope(
        self,
        scope: tuple[str, str, str, str],
    ) -> tuple[AgentSession | None, tuple[AgentSession, ...]]:
        work_session_id, objective_id, lane_id, team_id = scope
        sessions = await self.list_sessions(
            objective_id=objective_id,
            lane_id=lane_id,
            team_id=team_id or None,
        )
        matched_sessions = []
        for session in sessions:
            session_scope = self._resident_team_shell_scope_from_session(session)
            if session_scope is None:
                continue
            session_work_session_id, _, _, session_team_id = session_scope
            if work_session_id and session_work_session_id and session_work_session_id != work_session_id:
                continue
            if team_id and session_team_id != team_id:
                continue
            matched_sessions.append(session)
        leader_sessions = [session for session in matched_sessions if session.role == "leader"]
        leader_session = None
        if leader_sessions:
            leader_session = max(
                leader_sessions,
                key=lambda item: _coordinator_session_sort_key(
                    CoordinatorSessionState.from_agent_session(item)
                ),
            )
        teammate_sessions = [
            session
            for session in matched_sessions
            if session.role == "teammate"
            and _teammate_slot_from_agent_id(session.agent_id) is not None
        ]
        teammate_sessions.sort(
            key=lambda item: (
                _teammate_slot_from_agent_id(item.agent_id) or 0,
                item.session_id,
            )
        )
        return leader_session, tuple(teammate_sessions)

    @staticmethod
    def _resident_team_shell_status_from_sessions(
        *,
        leader_session: AgentSession | None,
        teammate_sessions: tuple[AgentSession, ...],
        base_shell: ResidentTeamShell | None,
    ) -> ResidentTeamShellStatus:
        if leader_session is not None:
            return _resident_team_shell_status_from_phase(leader_session.phase)
        phases = {session.phase for session in teammate_sessions}
        if ResidentCoordinatorPhase.FAILED in phases:
            return ResidentTeamShellStatus.FAILED
        if ResidentCoordinatorPhase.RUNNING in phases:
            return ResidentTeamShellStatus.ATTACHED
        if ResidentCoordinatorPhase.WAITING_FOR_MAILBOX in phases:
            return ResidentTeamShellStatus.WAITING_FOR_MAILBOX
        if ResidentCoordinatorPhase.WAITING_FOR_SUBORDINATES in phases:
            return ResidentTeamShellStatus.WAITING_FOR_SUBORDINATES
        if ResidentCoordinatorPhase.IDLE in phases:
            return ResidentTeamShellStatus.IDLE
        if ResidentCoordinatorPhase.QUIESCENT in phases:
            return ResidentTeamShellStatus.QUIESCENT
        if base_shell is not None:
            return base_shell.status
        return ResidentTeamShellStatus.BOOTING

    @staticmethod
    def _resident_team_shell_progress_timestamp(
        *,
        leader_session: AgentSession | None,
        teammate_sessions: tuple[AgentSession, ...],
        base_shell: ResidentTeamShell | None,
    ) -> str:
        candidates = [
            leader_session.last_progress_at if leader_session is not None else None,
            *(session.last_progress_at for session in teammate_sessions),
            base_shell.last_progress_at if base_shell is not None else None,
            base_shell.updated_at if base_shell is not None else None,
            base_shell.created_at if base_shell is not None else None,
        ]
        normalized = [value for value in candidates if isinstance(value, str) and value.strip()]
        return max(normalized) if normalized else _now_iso()

    @staticmethod
    def _resident_team_shell_attach_state(
        *,
        leader_session: AgentSession | None,
        teammate_sessions: tuple[AgentSession, ...],
        status: ResidentTeamShellStatus,
        base_shell: ResidentTeamShell | None,
    ) -> dict[str, Any]:
        approval_metadata = (
            base_shell.metadata if base_shell is not None else {}
        )
        preferred_session = leader_session
        if preferred_session is None:
            preferred_session = next(
                (
                    session
                    for session in teammate_sessions
                    if session.current_binding is not None
                ),
                teammate_sessions[0] if teammate_sessions else None,
            )
        binding = preferred_session.current_binding if preferred_session is not None else None
        payload: dict[str, Any] = {
            "status": status.value,
            "preferred_session_id": (
                preferred_session.session_id if preferred_session is not None else None
            ),
            "preferred_role": preferred_session.role if preferred_session is not None else None,
            "leader_phase": (
                leader_session.phase.value if leader_session is not None else None
            ),
            "active_teammate_slot_session_ids": [
                session.session_id
                for session in teammate_sessions
                if session.phase == ResidentCoordinatorPhase.RUNNING
            ],
            "idle_teammate_slot_session_ids": [
                session.session_id
                for session in teammate_sessions
                if session.phase == ResidentCoordinatorPhase.IDLE
            ],
            "waiting_teammate_slot_session_ids": [
                session.session_id
                for session in teammate_sessions
                if session.phase in {
                    ResidentCoordinatorPhase.WAITING_FOR_MAILBOX,
                    ResidentCoordinatorPhase.WAITING_FOR_SUBORDINATES,
                }
            ],
            "mailbox_pressure_directive_count": (
                (len(leader_session.current_directive_ids) if leader_session is not None else 0)
                + sum(len(session.current_directive_ids) for session in teammate_sessions)
            ),
            "attach_approval_status": _resident_shell_approval_status(
                approval_metadata,
                approval_kind="attach",
                target_session_id=(
                    preferred_session.session_id if preferred_session is not None else None
                ),
            ),
            "idle_wait_approval_status": _resident_shell_approval_status(
                approval_metadata,
                approval_kind="idle_wait",
                target_session_id=(
                    preferred_session.session_id if preferred_session is not None else None
                ),
            ),
        }
        if binding is not None:
            payload.update(
                {
                    "backend": binding.backend,
                    "binding_type": binding.binding_type,
                    "supervisor_id": binding.supervisor_id,
                    "lease_id": binding.lease_id,
                    "lease_expires_at": binding.lease_expires_at,
                }
            )
        elif base_shell is not None:
            for key in (
                "backend",
                "binding_type",
                "supervisor_id",
                "lease_id",
                "lease_expires_at",
            ):
                if key in base_shell.attach_state:
                    payload[key] = base_shell.attach_state[key]
        return {
            key: value
            for key, value in payload.items()
            if value is not None or key.endswith("_ids") or key == "mailbox_pressure_directive_count"
        }

    async def _project_resident_team_shell(
        self,
        *,
        scope: tuple[str, str, str, str],
        base_shell: ResidentTeamShell | None = None,
    ) -> tuple[ResidentTeamShell | None, AgentSession | None, tuple[AgentSession, ...]]:
        leader_session, teammate_sessions = await self._resident_team_shell_sessions_for_scope(
            scope
        )
        work_session_id, objective_id, lane_id, team_id = scope
        derived_work_session_id = (
            _optional_string(
                (leader_session.metadata if leader_session is not None else {}).get("work_session_id")
            )
            or next(
                (
                    _optional_string(session.metadata.get("work_session_id"))
                    for session in teammate_sessions
                    if isinstance(session.metadata, Mapping)
                    and _optional_string(session.metadata.get("work_session_id")) is not None
                ),
                None,
            )
            or (base_shell.work_session_id if base_shell is not None else "")
            or work_session_id
        )
        hinted_shell_id = (
            _optional_string(
                (leader_session.metadata if leader_session is not None else {}).get(
                    "resident_team_shell_id"
                )
            )
            or next(
                (
                    _optional_string(session.metadata.get("resident_team_shell_id"))
                    for session in teammate_sessions
                    if isinstance(session.metadata, Mapping)
                    and _optional_string(session.metadata.get("resident_team_shell_id")) is not None
                ),
                None,
            )
        )
        if base_shell is None:
            if hinted_shell_id is not None:
                base_shell = await self._get_persisted_resident_team_shell(hinted_shell_id)
            if (
                base_shell is not None
                and self._resident_team_shell_scope_from_shell(base_shell) != scope
            ):
                base_shell = None
        if base_shell is None:
            base_shell = await self._find_persisted_resident_team_shell_for_scope(
                (derived_work_session_id or "", objective_id, lane_id, team_id)
            )
        if base_shell is None and leader_session is None and not teammate_sessions:
            return None, None, ()
        now_iso = _now_iso()
        group_id = (
            _optional_string(
                (leader_session.metadata if leader_session is not None else {}).get("group_id")
            )
            or next(
                (
                    _optional_string(session.metadata.get("group_id"))
                    for session in teammate_sessions
                    if isinstance(session.metadata, Mapping)
                    and _optional_string(session.metadata.get("group_id")) is not None
                ),
                None,
            )
            or (base_shell.group_id if base_shell is not None else "")
        )
        runtime_generation_id = (
            _optional_string(
                (leader_session.metadata if leader_session is not None else {}).get(
                    "runtime_generation_id"
                )
            )
            or next(
                (
                    _optional_string(session.metadata.get("runtime_generation_id"))
                    for session in teammate_sessions
                    if isinstance(session.metadata, Mapping)
                    and _optional_string(session.metadata.get("runtime_generation_id")) is not None
                ),
                None,
            )
            or (base_shell.runtime_generation_id if base_shell is not None else "")
        )
        status = self._resident_team_shell_status_from_sessions(
            leader_session=leader_session,
            teammate_sessions=teammate_sessions,
            base_shell=base_shell,
        )
        last_progress_at = self._resident_team_shell_progress_timestamp(
            leader_session=leader_session,
            teammate_sessions=teammate_sessions,
            base_shell=base_shell,
        )
        shell = ResidentTeamShell(
            resident_team_shell_id=(
                base_shell.resident_team_shell_id
                if base_shell is not None
                else _resident_team_shell_id_from_scope(
                    work_session_id=derived_work_session_id or "",
                    objective_id=objective_id,
                    lane_id=lane_id,
                    team_id=team_id,
                )
            ),
            work_session_id=(
                derived_work_session_id
                or work_session_id
                or (base_shell.work_session_id if base_shell is not None else "")
            ),
            group_id=group_id or (base_shell.group_id if base_shell is not None else ""),
            objective_id=objective_id,
            team_id=team_id or (base_shell.team_id if base_shell is not None else ""),
            lane_id=lane_id,
            runtime_generation_id=runtime_generation_id,
            status=status,
            leader_slot_session_id=(
                leader_session.session_id
                if leader_session is not None
                else (base_shell.leader_slot_session_id if base_shell is not None else None)
            ),
            teammate_slot_session_ids=(
                [session.session_id for session in teammate_sessions]
                if teammate_sessions
                else (
                    list(base_shell.teammate_slot_session_ids)
                    if base_shell is not None
                    else []
                )
            ),
            attach_state=self._resident_team_shell_attach_state(
                leader_session=leader_session,
                teammate_sessions=teammate_sessions,
                status=status,
                base_shell=base_shell,
            ),
            created_at=(
                base_shell.created_at
                if base_shell is not None and base_shell.created_at
                else now_iso
            ),
            updated_at=now_iso,
            last_progress_at=last_progress_at,
            metadata=dict(base_shell.metadata) if base_shell is not None else {},
        )
        return shell, leader_session, teammate_sessions

    async def _sync_resident_team_shell_projection(
        self,
        *,
        session: AgentSession,
    ) -> None:
        scope = self._resident_team_shell_scope_from_session(session)
        if scope is None:
            return
        shell, _, _ = await self._project_resident_team_shell(scope=scope)
        if shell is None:
            return
        if not await self._can_persist_resident_team_shell(shell):
            return
        await self._save_persisted_resident_team_shell(shell)

    async def _can_persist_resident_team_shell(
        self,
        shell: ResidentTeamShell,
    ) -> bool:
        work_session_id = _optional_string(shell.work_session_id)
        if work_session_id is None:
            return False
        store = getattr(self, "store", None)
        if store is None:
            return True
        work_session = await store.get_work_session(work_session_id)
        return work_session is not None

    async def list_resident_team_shells(
        self,
        *,
        work_session_id: str | None = None,
        objective_id: str | None = None,
        lane_id: str | None = None,
        team_id: str | None = None,
    ) -> tuple[ResidentTeamShell, ...]:
        normalized_work_session_id = _optional_string(work_session_id)
        normalized_objective_id = _optional_string(objective_id)
        normalized_lane_id = _optional_string(lane_id)
        normalized_team_id = _optional_string(team_id)
        projected_by_scope: dict[tuple[str, str, str, str], ResidentTeamShell | None] = {}
        persisted_shells = await self._list_persisted_resident_team_shells(
            work_session_id=normalized_work_session_id,
        )
        for shell in persisted_shells:
            scope = self._resident_team_shell_scope_from_shell(shell)
            if scope is None:
                continue
            if not self._resident_team_shell_scope_matches(
                scope=scope,
                work_session_id=normalized_work_session_id,
                objective_id=normalized_objective_id,
                lane_id=normalized_lane_id,
                team_id=normalized_team_id,
            ):
                continue
            projected_by_scope[scope] = shell
        sessions = await self.list_sessions(
            objective_id=normalized_objective_id,
            lane_id=normalized_lane_id,
            team_id=normalized_team_id,
        )
        for session in sessions:
            scope = self._resident_team_shell_scope_from_session(session)
            if scope is None:
                continue
            if not self._resident_team_shell_scope_matches(
                scope=scope,
                work_session_id=normalized_work_session_id,
                objective_id=normalized_objective_id,
                lane_id=normalized_lane_id,
                team_id=normalized_team_id,
            ):
                continue
            projected_by_scope.setdefault(scope, None)
        projected_shells: list[ResidentTeamShell] = []
        for scope, base_shell in projected_by_scope.items():
            projected_shell, _, _ = await self._project_resident_team_shell(
                scope=scope,
                base_shell=base_shell,
            )
            if projected_shell is not None:
                projected_shells.append(projected_shell)
        projected_shells.sort(key=_resident_team_shell_sort_key)
        return tuple(projected_shells)

    async def inspect_resident_team_shell(
        self,
        *,
        resident_team_shell_id: str | None = None,
        work_session_id: str | None = None,
        objective_id: str | None = None,
        lane_id: str | None = None,
        team_id: str | None = None,
    ) -> ResidentTeamShell | None:
        normalized_shell_id = _optional_string(resident_team_shell_id)
        base_shell = None
        if normalized_shell_id is not None:
            base_shell = await self._get_persisted_resident_team_shell(normalized_shell_id)
        scope = None
        if base_shell is not None:
            scope = self._resident_team_shell_scope_from_shell(base_shell)
        else:
            normalized_objective_id = _optional_string(objective_id)
            normalized_lane_id = _optional_string(lane_id)
            if normalized_objective_id is not None and normalized_lane_id is not None:
                scope = (
                    _optional_string(work_session_id) or "",
                    normalized_objective_id,
                    normalized_lane_id,
                    _optional_string(team_id) or "",
                )
        if scope is None:
            return base_shell
        projected_shell, _, _ = await self._project_resident_team_shell(
            scope=scope,
            base_shell=base_shell,
        )
        return projected_shell

    async def record_resident_shell_approval(
        self,
        *,
        approval_kind: str,
        status: str,
        request: PermissionRequest | Mapping[str, Any] | None = None,
        decision: PermissionDecision | Mapping[str, Any] | None = None,
        requested_by: str | None = None,
        reviewer: str | None = None,
        reason: str | None = None,
        resident_team_shell_id: str | None = None,
        work_session_id: str | None = None,
        objective_id: str | None = None,
        lane_id: str | None = None,
        team_id: str | None = None,
        target_session_id: str | None = None,
        target_mode: str | None = None,
    ) -> ResidentTeamShell:
        normalized_approval_kind = _optional_string(approval_kind)
        if normalized_approval_kind is None:
            raise ValueError("approval_kind is required")
        normalized_status = _optional_string(status)
        if normalized_status not in {"pending", "approved", "denied"}:
            raise ValueError(f"Unsupported approval status: {status}")

        shell = None
        if target_session_id is not None:
            target_session = await self.load_session(target_session_id)
            if target_session is not None:
                scope = self._resident_team_shell_scope_from_session(target_session)
                if scope is not None:
                    shell, _, _ = await self._project_resident_team_shell(scope=scope)
        if shell is None:
            shell = await self.inspect_resident_team_shell(
                resident_team_shell_id=resident_team_shell_id,
                work_session_id=work_session_id,
                objective_id=objective_id,
                lane_id=lane_id,
                team_id=team_id,
            )
        if shell is None:
            raise ValueError("No resident team shell is available for approval recording.")

        request_payload = (
            request.to_dict()
            if isinstance(request, PermissionRequest)
            else _mapping_payload(request)
        )
        decision_payload = (
            decision.to_dict()
            if isinstance(decision, PermissionDecision)
            else _mapping_payload(decision)
        )
        metadata = dict(shell.metadata)
        approval_queue = _resident_shell_approval_queue(metadata)
        current_entry = dict(approval_queue.get(normalized_approval_kind, {}))
        now_iso = _now_iso()
        created_at = current_entry.get("created_at")
        if not _is_iso_timestamp(created_at):
            created_at = now_iso

        request_id = (
            _optional_string(request_payload.get("request_id"))
            or _optional_string(decision_payload.get("request_id"))
            or _optional_string(current_entry.get("request_id"))
        )
        entry_reviewer = (
            _optional_string(decision_payload.get("reviewer"))
            or _optional_string(reviewer)
            or _optional_string(current_entry.get("reviewer"))
        )
        entry_reason = (
            _optional_string(decision_payload.get("reason"))
            or _optional_string(reason)
            or _optional_string(current_entry.get("reason"))
        )
        entry_requested_by = (
            _optional_string(requested_by)
            or _optional_string(request_payload.get("requester"))
            or _optional_string(current_entry.get("requested_by"))
        )
        entry_target_session_id = (
            _optional_string(target_session_id)
            or _optional_string(current_entry.get("target_session_id"))
        )
        entry_target_mode = (
            _optional_string(target_mode)
            or _optional_string(current_entry.get("target_mode"))
        )
        next_entry: dict[str, Any] = {
            "approval_kind": normalized_approval_kind,
            "status": normalized_status,
            "requested_by": entry_requested_by,
            "request_id": request_id,
            "reviewer": entry_reviewer,
            "reason": entry_reason,
            "target_session_id": entry_target_session_id,
            "target_mode": entry_target_mode,
            "created_at": created_at,
            "updated_at": now_iso,
            "pending": normalized_status == "pending",
            "approved": normalized_status == "approved",
        }
        if request_payload:
            next_entry["request"] = request_payload
        elif "request" in current_entry:
            next_entry["request"] = current_entry["request"]
        if decision_payload:
            next_entry["decision"] = decision_payload
        elif "decision" in current_entry:
            next_entry["decision"] = current_entry["decision"]
        if entry_target_session_id is not None:
            current_targets = current_entry.get("targets")
            normalized_targets: dict[str, dict[str, Any]] = {}
            if isinstance(current_targets, Mapping):
                for raw_target_session_id, raw_target_entry in current_targets.items():
                    normalized_target_session_id = _optional_string(raw_target_session_id)
                    if normalized_target_session_id is None:
                        continue
                    normalized_targets[normalized_target_session_id] = _mapping_payload(
                        raw_target_entry
                    )
            normalized_targets[entry_target_session_id] = {
                key: value
                for key, value in next_entry.items()
                if key != "targets" and value is not None
            }
            next_entry["targets"] = normalized_targets
        approval_queue[normalized_approval_kind] = {
            key: value
            for key, value in next_entry.items()
            if value is not None
        }
        metadata["approval_queue"] = approval_queue
        stored_shell = await self._save_persisted_resident_team_shell(
            replace(
                shell,
                metadata=metadata,
                updated_at=now_iso,
            )
        )
        scope = self._resident_team_shell_scope_from_shell(stored_shell)
        if scope is not None:
            _, leader_session, teammate_sessions = await self._project_resident_team_shell(
                scope=scope,
                base_shell=stored_shell,
            )
            for session in (
                *(tuple() if leader_session is None else (leader_session,)),
                *teammate_sessions,
            ):
                session_metadata = session.metadata if isinstance(session.metadata, Mapping) else {}
                if (
                    _optional_string(session_metadata.get("resident_team_shell_id"))
                    == stored_shell.resident_team_shell_id
                ):
                    continue
                await self.update_session(
                    session.session_id,
                    metadata={
                        "resident_team_shell_id": stored_shell.resident_team_shell_id,
                    },
                )
        work_session_id, runtime_generation_id = _continuity_ids_from_metadata(
            {
                "work_session_id": stored_shell.work_session_id,
                "runtime_generation_id": stored_shell.runtime_generation_id,
            }
        )
        turn_record = await self._record_turn(
            work_session_id=work_session_id,
            runtime_generation_id=runtime_generation_id,
            head_kind=ConversationHeadKind.LEADER_LANE,
            scope_id=_optional_string(stored_shell.lane_id),
            actor_role=AgentTurnActorRole.SYSTEM,
            turn_kind=AgentTurnKind.LEADER_DECISION,
            input_summary=f"resident shell approval {normalized_approval_kind}",
            output_summary=f"{normalized_status} approval recorded",
            metadata={
                "approval_kind": normalized_approval_kind,
                "status": normalized_status,
                "resident_team_shell_id": stored_shell.resident_team_shell_id,
                "target_session_id": entry_target_session_id,
                "target_mode": entry_target_mode,
                "request_id": request_id,
                "reviewer": entry_reviewer,
            },
        )
        await self._record_tool_invocation(
            turn_record=turn_record,
            tool_kind=ToolInvocationKind.PROTOCOL_TOOL,
            tool_name="resident.shell.approval",
            input_summary=normalized_approval_kind,
            output_summary=normalized_status,
            metadata={
                "resident_team_shell_id": stored_shell.resident_team_shell_id,
                "request_id": request_id,
            },
        )
        return stored_shell

    async def find_preferred_attach_target(
        self,
        *,
        resident_team_shell_id: str | None = None,
        work_session_id: str | None = None,
        objective_id: str | None = None,
        lane_id: str | None = None,
        team_id: str | None = None,
    ) -> ShellAttachDecision:
        shell = await self.inspect_resident_team_shell(
            resident_team_shell_id=resident_team_shell_id,
            work_session_id=work_session_id,
            objective_id=objective_id,
            lane_id=lane_id,
            team_id=team_id,
        )
        if shell is None:
            return ShellAttachDecision(
                mode=ShellAttachDecisionMode.REJECTED,
                reason="No resident team shell is available for the requested scope.",
            )
        scope = self._resident_team_shell_scope_from_shell(shell)
        leader_session = None
        teammate_sessions: tuple[AgentSession, ...] = ()
        if scope is not None:
            _, leader_session, teammate_sessions = await self._project_resident_team_shell(
                scope=scope,
                base_shell=shell,
            )
        preferred_session = leader_session
        if preferred_session is None:
            preferred_session = next(
                (
                    session
                    for session in teammate_sessions
                    if session.current_binding is not None
                ),
                teammate_sessions[0] if teammate_sessions else None,
            )
        binding = preferred_session.current_binding if preferred_session is not None else None
        mode = ShellAttachDecisionMode.REJECTED
        reason = "No live resident attach target is available."
        if preferred_session is not None:
            if binding is not None:
                if shell.status == ResidentTeamShellStatus.RECOVERING:
                    mode = ShellAttachDecisionMode.RECOVERED
                    reason = "Resident shell is recovering through a live host binding."
                else:
                    mode = ShellAttachDecisionMode.ATTACHED
                    reason = "Resident shell remains live on the current host binding."
            elif shell.status in {
                ResidentTeamShellStatus.ATTACHED,
                ResidentTeamShellStatus.IDLE,
                ResidentTeamShellStatus.WAITING_FOR_MAILBOX,
                ResidentTeamShellStatus.WAITING_FOR_SUBORDINATES,
                ResidentTeamShellStatus.BOOTING,
            }:
                mode = ShellAttachDecisionMode.WOKEN
                reason = "Resident shell is live but needs a wake path before attach."
            elif shell.status == ResidentTeamShellStatus.RECOVERING:
                mode = ShellAttachDecisionMode.RECOVERED
                reason = "Resident shell needs recovery before attach."
            elif shell.status == ResidentTeamShellStatus.QUIESCENT:
                mode = ShellAttachDecisionMode.WOKEN
                reason = "Resident shell is quiescent and can be woken."
            elif shell.status in {
                ResidentTeamShellStatus.CLOSED,
                ResidentTeamShellStatus.FAILED,
            }:
                mode = ShellAttachDecisionMode.WARM_RESUMED
                reason = "Resident shell is no longer live and needs warm resume."
        approval_queue = _resident_shell_approval_queue(shell.metadata)
        approval_entry = approval_queue.get("attach", {})
        approval_status = _optional_string(approval_entry.get("status"))
        metadata: dict[str, Any] = {
            "resident_team_shell_id": shell.resident_team_shell_id,
            "status": shell.status.value,
        }
        if preferred_session is not None:
            metadata["preferred_session_id"] = preferred_session.session_id
            metadata["preferred_role"] = preferred_session.role
            metadata["phase"] = preferred_session.phase.value
        if binding is not None:
            metadata.update(
                {
                    "backend": binding.backend,
                    "binding_type": binding.binding_type,
                    "supervisor_id": binding.supervisor_id,
                    "lease_id": binding.lease_id,
                    "lease_expires_at": binding.lease_expires_at,
                }
            )
        if approval_status is not None:
            metadata["approval_status"] = approval_status
        approval_target_mode = _optional_string(approval_entry.get("target_mode"))
        if approval_target_mode is not None:
            metadata["approval_target_mode"] = approval_target_mode
        if approval_status in {"pending", "denied"}:
            action = approval_target_mode or mode.value
            reason = (
                f"Resident shell attach approval is pending before {action}."
                if approval_status == "pending"
                else f"Resident shell attach approval was denied before {action}."
            )
            return ShellAttachDecision(
                mode=ShellAttachDecisionMode.REJECTED,
                reason=reason,
                target_shell_id=shell.resident_team_shell_id,
                target_work_session_id=_optional_string(shell.work_session_id),
                target_runtime_generation_id=_optional_string(shell.runtime_generation_id),
                metadata=metadata,
            )
        return ShellAttachDecision(
            mode=mode,
            reason=reason,
            target_shell_id=shell.resident_team_shell_id,
            target_work_session_id=_optional_string(shell.work_session_id),
            target_runtime_generation_id=_optional_string(shell.runtime_generation_id),
            metadata=metadata,
        )

    async def build_shell_attach_view(
        self,
        *,
        resident_team_shell_id: str | None = None,
        work_session_id: str | None = None,
        objective_id: str | None = None,
        lane_id: str | None = None,
        team_id: str | None = None,
    ) -> dict[str, Any]:
        shell = await self.inspect_resident_team_shell(
            resident_team_shell_id=resident_team_shell_id,
            work_session_id=work_session_id,
            objective_id=objective_id,
            lane_id=lane_id,
            team_id=team_id,
        )
        if shell is None:
            return {}
        scope = self._resident_team_shell_scope_from_shell(shell)
        leader_session = None
        teammate_sessions: tuple[AgentSession, ...] = ()
        if scope is not None:
            _, leader_session, teammate_sessions = await self._project_resident_team_shell(
                scope=scope,
                base_shell=shell,
            )
        attach_recommendation = await self.find_preferred_attach_target(
            resident_team_shell_id=shell.resident_team_shell_id,
        )
        runnable_teammate_slots = await self.list_runnable_teammate_slot_sessions(
            objective_id=shell.objective_id or None,
            lane_id=shell.lane_id or None,
            team_id=shell.team_id or None,
        )
        leader_binding = (
            leader_session.current_binding
            if leader_session is not None
            else None
        )
        waiting_phases = {
            ResidentCoordinatorPhase.WAITING_FOR_MAILBOX,
            ResidentCoordinatorPhase.WAITING_FOR_SUBORDINATES,
        }
        slot_summary = {
            "total": (1 if leader_session is not None else 0) + len(teammate_sessions),
            "active": (
                1
                if leader_session is not None and leader_session.phase == ResidentCoordinatorPhase.RUNNING
                else 0
            )
            + sum(
                1
                for session in teammate_sessions
                if session.phase == ResidentCoordinatorPhase.RUNNING
            ),
            "idle": (
                1
                if leader_session is not None and leader_session.phase == ResidentCoordinatorPhase.IDLE
                else 0
            )
            + sum(
                1 for session in teammate_sessions if session.phase == ResidentCoordinatorPhase.IDLE
            ),
            "waiting": (
                1
                if leader_session is not None and leader_session.phase in waiting_phases
                else 0
            )
            + sum(1 for session in teammate_sessions if session.phase in waiting_phases),
            "runnable": len(runnable_teammate_slots),
        }
        approval_queue = _resident_shell_approval_queue(shell.metadata)
        attach_view = {
            "resident_team_shell_id": shell.resident_team_shell_id,
            "work_session_id": shell.work_session_id,
            "group_id": shell.group_id,
            "objective_id": shell.objective_id,
            "lane_id": shell.lane_id,
            "team_id": shell.team_id,
            "runtime_generation_id": shell.runtime_generation_id,
            "status": shell.status.value,
            "leader_slot": (
                {
                    "session_id": leader_session.session_id,
                    "phase": leader_session.phase.value,
                    "last_progress_at": leader_session.last_progress_at,
                    "backend": leader_binding.backend if leader_binding is not None else None,
                    "lease_id": leader_binding.lease_id if leader_binding is not None else None,
                    "lease_expires_at": (
                        leader_binding.lease_expires_at if leader_binding is not None else None
                    ),
                    "has_binding": leader_binding is not None,
                }
                if leader_session is not None
                else None
            ),
            "teammate_slots": [
                {
                    "session_id": session.session_id,
                    "phase": session.phase.value,
                    "current_task_id": session.metadata.get("current_task_id"),
                    "current_worker_session_id": session.current_worker_session_id,
                    "last_worker_session_id": session.last_worker_session_id,
                    "idle_since": session.metadata.get("idle_since"),
                    "last_active_at": session.metadata.get("last_active_at"),
                }
                for session in teammate_sessions
            ],
            "mailbox_pressure": {
                "directive_count": shell.attach_state.get("mailbox_pressure_directive_count", 0),
                "leader_mailbox_cursor": (
                    leader_session.mailbox_cursor.get("last_envelope_id")
                    if leader_session is not None
                    else None
                ),
            },
            "open_runnable_work": {
                "runnable_teammate_slot_session_ids": [
                    session.session_id for session in runnable_teammate_slots
                ]
            },
            "last_progress_at": shell.last_progress_at,
            "slot_summary": slot_summary,
            "attach_state": dict(shell.attach_state),
            "approval_queue": approval_queue,
            "attach_recommendation": attach_recommendation.to_dict(),
        }
        work_session_id, runtime_generation_id = _continuity_ids_from_metadata(
            {
                "work_session_id": shell.work_session_id,
                "runtime_generation_id": shell.runtime_generation_id,
            }
        )
        turn_record = await self._record_turn(
            work_session_id=work_session_id,
            runtime_generation_id=runtime_generation_id,
            head_kind=ConversationHeadKind.LEADER_LANE,
            scope_id=_optional_string(shell.lane_id),
            actor_role=AgentTurnActorRole.SYSTEM,
            turn_kind=AgentTurnKind.LEADER_DECISION,
            input_summary="resident shell attach view",
            output_summary=f"status={shell.status.value}",
            metadata={
                "resident_team_shell_id": shell.resident_team_shell_id,
                "status": shell.status.value,
                "preferred_session_id": attach_view.get("leader_slot", {}) and attach_view["leader_slot"].get("session_id"),
            },
        )
        await self._record_artifact(
            turn_record=turn_record,
            artifact_kind=ArtifactRefKind.HYDRATION_INPUT,
            uri=f"resident-shell:{shell.resident_team_shell_id}:attach-view",
            payload=attach_view,
            metadata={"lane_id": shell.lane_id, "team_id": shell.team_id},
        )
        return attach_view

    async def list_sessions(
        self,
        *,
        role: str | None = None,
        objective_id: str | None = None,
        lane_id: str | None = None,
        team_id: str | None = None,
        agent_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> tuple[AgentSession, ...]:
        sessions = await self._list_sessions()
        matched: list[AgentSession] = []
        for session in sessions:
            if role is not None and session.role != role:
                continue
            if objective_id is not None and session.objective_id != objective_id:
                continue
            if lane_id is not None and session.lane_id != lane_id:
                continue
            if team_id is not None and session.team_id != team_id:
                continue
            if agent_id is not None and session.agent_id != agent_id:
                continue
            if metadata is not None:
                session_metadata = session.metadata if isinstance(session.metadata, Mapping) else {}
                if any(
                    session_metadata.get(str(key)) != value
                    for key, value in metadata.items()
                ):
                    continue
            matched.append(session)
        matched.sort(
            key=lambda item: (
                item.objective_id or "",
                item.lane_id or "",
                item.team_id or "",
                item.session_id,
            )
        )
        return tuple(matched)

    async def list_coordinator_sessions(
        self,
        *,
        role: str | None = None,
        objective_id: str | None = None,
        lane_id: str | None = None,
        team_id: str | None = None,
        host_owner_coordinator_id: str | None = None,
    ) -> tuple[CoordinatorSessionState, ...]:
        metadata_filter = None
        if host_owner_coordinator_id is not None:
            metadata_filter = {
                "host_owner_coordinator_id": host_owner_coordinator_id,
            }
        sessions = await self.list_sessions(
            role=role,
            objective_id=objective_id,
            lane_id=lane_id,
            team_id=team_id,
            metadata=metadata_filter,
        )
        return tuple(
            CoordinatorSessionState.from_agent_session(session)
            for session in sessions
        )

    async def list_coordinator_sessions_by_lane(
        self,
        *,
        role: str | None = None,
        objective_id: str | None = None,
        team_id: str | None = None,
        host_owner_coordinator_id: str | None = None,
    ) -> dict[str, CoordinatorSessionState]:
        sessions = await self.list_coordinator_sessions(
            role=role,
            objective_id=objective_id,
            team_id=team_id,
            host_owner_coordinator_id=host_owner_coordinator_id,
        )
        sessions_by_lane: dict[str, CoordinatorSessionState] = {}
        for session in sessions:
            if session.lane_id is None:
                continue
            current = sessions_by_lane.get(session.lane_id)
            if current is None or _coordinator_session_sort_key(session) >= _coordinator_session_sort_key(current):
                sessions_by_lane[session.lane_id] = session
        return sessions_by_lane

    async def list_teammate_slot_sessions(
        self,
        *,
        objective_id: str | None = None,
        lane_id: str | None = None,
        team_id: str | None = None,
        require_activation_profile: bool = False,
    ) -> tuple[AgentSession, ...]:
        sessions = await self.list_sessions(
            role="teammate",
            objective_id=objective_id,
            lane_id=lane_id,
            team_id=team_id,
        )
        matched: list[AgentSession] = []
        for session in sessions:
            if _teammate_slot_from_agent_id(session.agent_id) is None:
                continue
            if require_activation_profile:
                profile = TeammateActivationProfile.from_metadata(session.metadata)
                if not _activation_profile_is_runnable(profile):
                    continue
            matched.append(session)
        matched.sort(
            key=lambda item: (
                objective_id or item.objective_id or "",
                lane_id or item.lane_id or "",
                team_id or item.team_id or "",
                _teammate_slot_from_agent_id(item.agent_id) or 0,
                item.session_id,
            )
        )
        return tuple(matched)

    async def list_runnable_teammate_slot_sessions(
        self,
        *,
        objective_id: str | None = None,
        lane_id: str | None = None,
        team_id: str | None = None,
    ) -> tuple[AgentSession, ...]:
        return await self.list_teammate_slot_sessions(
            objective_id=objective_id,
            lane_id=lane_id,
            team_id=team_id,
            require_activation_profile=True,
        )

    async def record_coordinator_session_state(
        self,
        session_id: str,
        *,
        coordinator_session: ResidentCoordinatorSession,
        host_owner_coordinator_id: str | None = None,
        runtime_task_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        last_progress_at: str | None = None,
    ) -> AgentSession:
        current = await self.load_session(session_id)
        if current is None:
            raise ValueError(f"Unknown session_id: {session_id}")
        existing_state = CoordinatorSessionState.from_agent_session(current)
        merged_metadata = dict(existing_state.metadata)
        if metadata is not None:
            merged_metadata.update({str(key): value for key, value in metadata.items()})
        projected_state = CoordinatorSessionState.from_resident_session(
            session_id=session_id,
            coordinator_session=coordinator_session,
            host_owner_coordinator_id=(
                host_owner_coordinator_id
                if host_owner_coordinator_id is not None
                else existing_state.host_owner_coordinator_id
            ),
            runtime_task_id=(
                runtime_task_id
                if runtime_task_id is not None
                else existing_state.runtime_task_id
            ),
            last_progress_at=last_progress_at if last_progress_at is not None else _now_iso(),
            metadata=merged_metadata,
        )
        mailbox_cursor = None
        if projected_state.mailbox_cursor is not None:
            mailbox_cursor = CoordinatorSessionState.mailbox_cursor_payload(
                projected_state.mailbox_cursor
            )
        return await self.update_session(
            session_id,
            phase=projected_state.phase,
            reason=projected_state.last_reason,
            mailbox_cursor=mailbox_cursor,
            last_progress_at=projected_state.last_progress_at,
            metadata=projected_state.to_metadata_patch(),
        )

    @abstractmethod
    async def save_session(self, session: AgentSession) -> AgentSession:
        raise NotImplementedError

    @abstractmethod
    async def load_session(self, session_id: str) -> AgentSession | None:
        raise NotImplementedError

    @abstractmethod
    async def _list_sessions(self) -> tuple[AgentSession, ...]:
        raise NotImplementedError

    @abstractmethod
    async def bind_session(self, session_id: str, binding: SessionBinding) -> AgentSession:
        raise NotImplementedError

    async def bind_transport(self, session_id: str, binding: SessionBinding) -> AgentSession:
        return await self.bind_session(session_id, binding)

    @abstractmethod
    async def mark_phase(
        self,
        session_id: str,
        phase: ResidentCoordinatorPhase,
        *,
        reason: str = "",
    ) -> AgentSession:
        raise NotImplementedError

    @abstractmethod
    async def reclaim_session(
        self,
        session_id: str,
        *,
        new_supervisor_id: str,
        new_lease_id: str,
        new_expires_at: str,
    ) -> AgentSession:
        raise NotImplementedError

    async def update_session(
        self,
        session_id: str,
        *,
        phase: ResidentCoordinatorPhase | None = None,
        reason: str | None = None,
        mailbox_cursor: Mapping[str, Any] | None = None,
        subscription_cursors: Mapping[str, Mapping[str, Any]] | None = None,
        current_directive_ids: tuple[str, ...] | None = None,
        last_progress_at: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        lease_id: str | None = None,
        lease_expires_at: str | None = None,
    ) -> AgentSession:
        projected = await self.project_session_update(
            session_id,
            phase=phase,
            reason=reason,
            mailbox_cursor=mailbox_cursor,
            subscription_cursors=subscription_cursors,
            current_directive_ids=current_directive_ids,
            last_progress_at=last_progress_at,
            metadata=metadata,
            lease_id=lease_id,
            lease_expires_at=lease_expires_at,
        )
        return await self.save_session(projected)

    async def project_session_update(
        self,
        session_id: str,
        *,
        session: AgentSession | None = None,
        phase: ResidentCoordinatorPhase | None = None,
        reason: str | None = None,
        mailbox_cursor: Mapping[str, Any] | None = None,
        subscription_cursors: Mapping[str, Mapping[str, Any]] | None = None,
        current_directive_ids: tuple[str, ...] | None = None,
        last_progress_at: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        lease_id: str | None = None,
        lease_expires_at: str | None = None,
    ) -> AgentSession:
        current = session if session is not None else await self.load_session(session_id)
        if current is None:
            raise ValueError(f"Unknown session_id: {session_id}")
        updated = current
        if mailbox_cursor is not None:
            updated = replace(updated, mailbox_cursor={str(key): value for key, value in mailbox_cursor.items()})
        if subscription_cursors is not None:
            normalized_subscription_cursors = {
                str(key): dict(value)
                for key, value in subscription_cursors.items()
            }
            updated = replace(updated, subscription_cursors=normalized_subscription_cursors)
        if current_directive_ids is not None:
            updated = replace(updated, current_directive_ids=tuple(str(item) for item in current_directive_ids))
        if last_progress_at is not None:
            updated = replace(updated, last_progress_at=last_progress_at)
        if metadata is not None:
            merged_metadata = dict(updated.metadata)
            merged_metadata.update({str(key): value for key, value in metadata.items()})
            updated = replace(updated, metadata=merged_metadata)
        if lease_id is not None:
            updated = replace(updated, lease_id=lease_id)
        if lease_expires_at is not None:
            updated = replace(updated, lease_expires_at=lease_expires_at)
        if phase is not None or reason is not None:
            updated = replace(
                updated,
                phase=phase if phase is not None else updated.phase,
                last_reason=reason if reason is not None else updated.last_reason,
            )
        return self._sync_worker_session_truth(updated)

    async def read_worker_session_truth(self, session_id: str) -> AgentWorkerSessionTruth:
        session = await self.load_session(session_id)
        if session is None:
            raise ValueError(f"Unknown session_id: {session_id}")
        return AgentWorkerSessionTruth.from_agent_session(session)

    async def snapshot_session(self, session_id: str) -> dict[str, Any] | None:
        session = await self.load_session(session_id)
        if session is None:
            return None
        return session.to_dict()

    async def project_worker_session_state(
        self,
        *,
        worker_session: WorkerSession,
        binding: SessionBinding | None = None,
        phase: ResidentCoordinatorPhase,
        reason: str = "",
        session: AgentSession | None = None,
    ) -> AgentSession:
        current = session if session is not None else await self.load_session(worker_session.session_id)
        metadata = dict(current.metadata) if current is not None else {}
        metadata.update(dict(worker_session.metadata))
        metadata.setdefault("worker_session_id", worker_session.session_id)
        current_worker_session_id = (
            worker_session.session_id
            if worker_session.status in {
                WorkerSessionStatus.ASSIGNED,
                WorkerSessionStatus.ACTIVE,
                WorkerSessionStatus.IDLE,
            }
            else None
        )
        last_worker_session_id = worker_session.session_id or str(metadata.get("last_worker_session_id", "") or "") or None
        metadata["bound_worker_session_id"] = current_worker_session_id
        metadata["current_worker_session_id"] = current_worker_session_id
        metadata["last_worker_session_id"] = last_worker_session_id

        claimed_task_ids = current.claimed_task_ids if current is not None else ()
        subscription_cursors = current.subscription_cursors if current is not None else {}
        current_directive_ids = current.current_directive_ids if current is not None else ()
        mailbox_cursor = (
            dict(current.mailbox_cursor)
            if current is not None and current.mailbox_cursor
            else dict(worker_session.mailbox_cursor)
        )
        current_binding = (
            SessionBinding.from_dict(binding.to_dict())
            if binding is not None
            else (
                self._binding_with_worker_session_lease(
                    binding=current.current_binding,
                    worker_session=worker_session,
                )
                if current is not None and current.current_binding is not None
                else None
            )
        )

        return self._sync_worker_session_truth(
            AgentSession(
                session_id=worker_session.session_id,
                agent_id=current.agent_id if current is not None else worker_session.worker_id,
                role=current.role if current is not None else worker_session.role,
                phase=phase,
                objective_id=current.objective_id if current is not None else metadata.get("objective_id"),
                lane_id=current.lane_id if current is not None else metadata.get("lane_id"),
                team_id=current.team_id if current is not None else metadata.get("team_id"),
                mailbox_cursor=mailbox_cursor,
                subscription_cursors=dict(subscription_cursors),
                claimed_task_ids=tuple(claimed_task_ids),
                current_directive_ids=tuple(current_directive_ids),
                current_binding=current_binding,
                current_worker_session_id=current_worker_session_id,
                last_worker_session_id=last_worker_session_id,
                lease_id=worker_session.supervisor_lease_id,
                lease_expires_at=worker_session.supervisor_lease_expires_at,
                last_reason=reason or str(metadata.get("last_reason", "")),
                metadata=metadata,
            )
        )

    async def commit_mailbox_consume(
        self,
        session_id: str,
        *,
        recipient: str,
        envelope_ids: tuple[str, ...],
        current_directive_ids: tuple[str, ...] = (),
        reason: str = "",
        persist_cursor: Callable[[str, dict[str, str | None]], Awaitable[None]] | None = None,
        acknowledge_bridge: Callable[[str, tuple[str, ...]], Awaitable[str | None]] | None = None,
    ) -> AgentSession:
        current = await self.load_session(session_id)
        if current is None:
            raise ValueError(f"Unknown session_id: {session_id}")
        if not envelope_ids:
            if reason:
                return await self.update_session(session_id, reason=reason)
            return current

        projected = await self.project_mailbox_consume(
            session_id,
            session=current,
            recipient=recipient,
            envelope_ids=envelope_ids,
            current_directive_ids=current_directive_ids,
            reason=reason,
        )
        cursor_payload = dict(projected.mailbox_cursor)
        if persist_cursor is not None:
            await persist_cursor(recipient, dict(cursor_payload))

        if acknowledge_bridge is not None:
            acknowledged_last_envelope = await acknowledge_bridge(recipient, envelope_ids)
            if (
                isinstance(acknowledged_last_envelope, str)
                and acknowledged_last_envelope
                and acknowledged_last_envelope != cursor_payload["last_envelope_id"]
            ):
                projected = await self.project_mailbox_consume(
                    session_id,
                    session=current,
                    recipient=recipient,
                    envelope_ids=envelope_ids,
                    current_directive_ids=current_directive_ids,
                    reason=reason,
                    acknowledged_last_envelope_id=acknowledged_last_envelope,
                )
                cursor_payload = dict(projected.mailbox_cursor)
                if persist_cursor is not None:
                    await persist_cursor(recipient, dict(cursor_payload))

        return await self.save_session(projected)

    async def project_mailbox_consume(
        self,
        session_id: str,
        *,
        recipient: str,
        envelope_ids: tuple[str, ...],
        current_directive_ids: tuple[str, ...] = (),
        reason: str = "",
        session: AgentSession | None = None,
        acknowledged_last_envelope_id: str | None = None,
    ) -> AgentSession:
        current = session if session is not None else await self.load_session(session_id)
        if current is None:
            raise ValueError(f"Unknown session_id: {session_id}")
        if not envelope_ids:
            if not reason:
                return current
            return await self.project_session_update(
                session_id,
                session=current,
                reason=reason,
            )
        last_envelope_id = acknowledged_last_envelope_id or envelope_ids[-1]
        cursor_payload: dict[str, str | None] = {
            "stream": "mailbox",
            "event_id": last_envelope_id,
            "last_envelope_id": last_envelope_id,
        }
        return await self.project_session_update(
            session_id,
            session=current,
            mailbox_cursor=cursor_payload,
            current_directive_ids=current_directive_ids,
            reason=reason if reason else None,
        )

    async def project_authority_relay_state(
        self,
        session_id: str,
        *,
        request_id: str,
        task_id: str,
        relay_subject: str,
        relay_envelope_id: str | None = None,
        actor_id: str | None = None,
        reason: str = "",
        relay_consumed_at: str | None = None,
        session: AgentSession | None = None,
    ) -> AgentSession:
        current = session if session is not None else await self.load_session(session_id)
        if current is None:
            raise ValueError(f"Unknown session_id: {session_id}")
        normalized_request_id = request_id.strip()
        normalized_task_id = task_id.strip()
        normalized_relay_subject = relay_subject.strip()
        if not normalized_request_id:
            raise ValueError("request_id must be non-empty")
        if not normalized_task_id:
            raise ValueError("task_id must be non-empty")
        if not normalized_relay_subject:
            raise ValueError("relay_subject must be non-empty")

        now_iso = (
            relay_consumed_at
            if relay_consumed_at is not None and _is_iso_timestamp(relay_consumed_at)
            else _now_iso()
        )
        metadata = dict(current.metadata)
        metadata["authority_request_id"] = normalized_request_id
        metadata["authority_waiting_task_id"] = normalized_task_id
        metadata["authority_last_relay_subject"] = normalized_relay_subject
        if relay_envelope_id is not None and relay_envelope_id.strip():
            metadata["authority_last_relay_envelope_id"] = relay_envelope_id.strip()
        if actor_id is not None and actor_id.strip():
            metadata["authority_last_relay_actor_id"] = actor_id.strip()
        if reason:
            metadata["authority_last_reason"] = reason
        metadata["authority_relay_published"] = True
        metadata["authority_relay_consumed"] = True
        metadata["authority_last_relay_consumed_at"] = now_iso
        if "authority_waiting" not in metadata:
            metadata["authority_waiting"] = True
        if metadata.get("authority_waiting") and not _is_iso_timestamp(metadata.get("authority_waiting_since")):
            metadata["authority_waiting_since"] = now_iso
        if "authority_wake_recorded" not in metadata:
            metadata["authority_wake_recorded"] = False
        _patch_authority_completion_status(metadata)
        phase = (
            ResidentCoordinatorPhase.WAITING_FOR_MAILBOX
            if metadata.get("authority_waiting")
            else current.phase
        )
        projected_reason = reason if reason else "Authority control relay consumed."
        return await self.project_session_update(
            session_id,
            session=current,
            phase=phase,
            reason=projected_reason,
            metadata=metadata,
            last_progress_at=now_iso,
        )

    async def record_authority_relay_state(
        self,
        session_id: str,
        *,
        request_id: str,
        task_id: str,
        relay_subject: str,
        relay_envelope_id: str | None = None,
        actor_id: str | None = None,
        reason: str = "",
        relay_consumed_at: str | None = None,
    ) -> AgentSession:
        projected = await self.project_authority_relay_state(
            session_id,
            request_id=request_id,
            task_id=task_id,
            relay_subject=relay_subject,
            relay_envelope_id=relay_envelope_id,
            actor_id=actor_id,
            reason=reason,
            relay_consumed_at=relay_consumed_at,
        )
        stored = await self.save_session(projected)
        work_session_id, runtime_generation_id = _continuity_ids_from_metadata(
            stored.metadata if isinstance(stored.metadata, Mapping) else {}
        )
        await self._record_tool_invocation_without_turn(
            work_session_id=work_session_id,
            runtime_generation_id=runtime_generation_id,
            tool_kind=ToolInvocationKind.AUTHORITY_ACTION,
            tool_name="authority.relay",
            input_summary=request_id,
            output_summary=relay_subject,
            metadata={"task_id": task_id, "actor_id": actor_id},
        )
        return stored

    async def project_authority_wait_state(
        self,
        session_id: str,
        *,
        request_id: str,
        task_id: str,
        boundary_class: str | None = None,
        reason: str = "",
        requested_by: str | None = None,
        session: AgentSession | None = None,
    ) -> AgentSession:
        current = session if session is not None else await self.load_session(session_id)
        if current is None:
            raise ValueError(f"Unknown session_id: {session_id}")
        normalized_request_id = request_id.strip()
        normalized_task_id = task_id.strip()
        if not normalized_request_id:
            raise ValueError("request_id must be non-empty")
        if not normalized_task_id:
            raise ValueError("task_id must be non-empty")

        now_iso = _now_iso()
        metadata = dict(current.metadata)
        metadata["authority_request_id"] = normalized_request_id
        metadata["authority_waiting_task_id"] = normalized_task_id
        metadata["authority_waiting"] = True
        metadata["authority_waiting_since"] = now_iso
        if boundary_class is not None:
            metadata["authority_boundary_class"] = str(boundary_class)
        if requested_by is not None:
            metadata["authority_last_requested_by"] = str(requested_by)
        if reason:
            metadata["authority_last_reason"] = reason
        metadata["authority_wake_recorded"] = False
        if "authority_relay_consumed" not in metadata:
            metadata["authority_relay_consumed"] = False
        if "authority_relay_published" not in metadata:
            metadata["authority_relay_published"] = False
        _patch_authority_completion_status(metadata)

        return await self.project_session_update(
            session_id,
            session=current,
            phase=ResidentCoordinatorPhase.WAITING_FOR_MAILBOX,
            reason=reason if reason else "Waiting for authority decision.",
            metadata=metadata,
            last_progress_at=now_iso,
        )

    async def record_authority_wait_state(
        self,
        session_id: str,
        *,
        request_id: str,
        task_id: str,
        boundary_class: str | None = None,
        reason: str = "",
        requested_by: str | None = None,
    ) -> AgentSession:
        projected = await self.project_authority_wait_state(
            session_id,
            request_id=request_id,
            task_id=task_id,
            boundary_class=boundary_class,
            reason=reason,
            requested_by=requested_by,
        )
        stored = await self.save_session(projected)
        work_session_id, runtime_generation_id = _continuity_ids_from_metadata(
            stored.metadata if isinstance(stored.metadata, Mapping) else {}
        )
        await self._record_tool_invocation_without_turn(
            work_session_id=work_session_id,
            runtime_generation_id=runtime_generation_id,
            tool_kind=ToolInvocationKind.AUTHORITY_ACTION,
            tool_name="authority.wait",
            input_summary=request_id,
            output_summary=reason or "Waiting for authority decision.",
            metadata={"task_id": task_id, "boundary_class": boundary_class},
        )
        return stored

    async def project_authority_decision_state(
        self,
        session_id: str,
        *,
        request_id: str,
        task_id: str,
        decision: str,
        actor_id: str | None = None,
        resume_target: str | None = None,
        reason: str = "",
        relay_subject: str | None = None,
        relay_envelope_id: str | None = None,
        relay_consumed_at: str | None = None,
        session: AgentSession | None = None,
    ) -> AgentSession:
        current = session if session is not None else await self.load_session(session_id)
        if current is None:
            raise ValueError(f"Unknown session_id: {session_id}")
        normalized_request_id = request_id.strip()
        normalized_task_id = task_id.strip()
        normalized_decision = decision.strip().lower()
        if not normalized_request_id:
            raise ValueError("request_id must be non-empty")
        if not normalized_task_id:
            raise ValueError("task_id must be non-empty")
        if normalized_decision not in {"grant", "deny", "reroute", "escalate"}:
            raise ValueError(f"Unsupported authority decision: {decision}")

        now_iso = _now_iso()
        metadata = dict(current.metadata)
        metadata["authority_request_id"] = normalized_request_id
        metadata["authority_waiting_task_id"] = normalized_task_id
        metadata["authority_last_decision"] = normalized_decision
        metadata["authority_last_decision_at"] = now_iso
        if actor_id is not None:
            metadata["authority_last_decision_actor_id"] = str(actor_id)
        if resume_target is not None:
            metadata["authority_resume_target"] = str(resume_target)
        if reason:
            metadata["authority_last_reason"] = reason
        if relay_subject is not None and relay_subject.strip():
            metadata["authority_last_relay_subject"] = relay_subject.strip()
            metadata["authority_relay_published"] = True
            metadata["authority_relay_consumed"] = True
            metadata["authority_last_relay_consumed_at"] = (
                relay_consumed_at
                if relay_consumed_at is not None and _is_iso_timestamp(relay_consumed_at)
                else now_iso
            )
            if relay_envelope_id is not None and relay_envelope_id.strip():
                metadata["authority_last_relay_envelope_id"] = relay_envelope_id.strip()
            if actor_id is not None and actor_id.strip():
                metadata["authority_last_relay_actor_id"] = actor_id.strip()
        else:
            if "authority_relay_consumed" not in metadata:
                metadata["authority_relay_consumed"] = False
            if "authority_relay_published" not in metadata:
                metadata["authority_relay_published"] = False

        if normalized_decision == "escalate":
            metadata["authority_wake_recorded"] = False
            metadata["authority_waiting"] = True
            if not _is_iso_timestamp(metadata.get("authority_waiting_since")):
                metadata["authority_waiting_since"] = now_iso
            _patch_authority_completion_status(metadata)
            phase = ResidentCoordinatorPhase.WAITING_FOR_MAILBOX
            projected_reason = reason if reason else "Authority decision escalated; waiting for authority root."
            return await self.project_session_update(
                session_id,
                session=current,
                phase=phase,
                reason=projected_reason,
                metadata=metadata,
                last_progress_at=now_iso,
            )

        metadata["authority_waiting"] = False
        metadata["authority_waiting_since"] = None

        if normalized_decision == "grant":
            slot_state = TeammateSlotSessionState.from_metadata(metadata)
            slot_state.wake_request_count = (slot_state.wake_request_count or 0) + 1
            slot_state.last_wake_request_at = now_iso
            slot_state.last_active_at = now_iso
            slot_state.idle_since = None
            if reason:
                slot_state.last_wake_reason = reason
            if actor_id is not None:
                slot_state.last_wake_requested_by = str(actor_id)
            metadata.update(slot_state.to_metadata_patch(include_none_keys=("idle_since",)))
            metadata["authority_wake_recorded"] = True
            phase = ResidentCoordinatorPhase.RUNNING
            projected_reason = reason if reason else "Authority granted; wake request recorded."
        elif normalized_decision == "reroute":
            metadata["authority_wake_recorded"] = False
            phase = ResidentCoordinatorPhase.IDLE
            projected_reason = reason if reason else "Authority rerouted task ownership."
        else:
            metadata["authority_wake_recorded"] = False
            phase = ResidentCoordinatorPhase.IDLE
            projected_reason = reason if reason else "Authority denied."
        _patch_authority_completion_status(metadata)

        return await self.project_session_update(
            session_id,
            session=current,
            phase=phase,
            reason=projected_reason,
            metadata=metadata,
            last_progress_at=now_iso,
        )

    async def record_authority_decision_state(
        self,
        session_id: str,
        *,
        request_id: str,
        task_id: str,
        decision: str,
        actor_id: str | None = None,
        resume_target: str | None = None,
        reason: str = "",
        relay_subject: str | None = None,
        relay_envelope_id: str | None = None,
        relay_consumed_at: str | None = None,
    ) -> AgentSession:
        projected = await self.project_authority_decision_state(
            session_id,
            request_id=request_id,
            task_id=task_id,
            decision=decision,
            actor_id=actor_id,
            resume_target=resume_target,
            reason=reason,
            relay_subject=relay_subject,
            relay_envelope_id=relay_envelope_id,
            relay_consumed_at=relay_consumed_at,
        )
        stored = await self.save_session(projected)
        work_session_id, runtime_generation_id = _continuity_ids_from_metadata(
            stored.metadata if isinstance(stored.metadata, Mapping) else {}
        )
        await self._record_tool_invocation_without_turn(
            work_session_id=work_session_id,
            runtime_generation_id=runtime_generation_id,
            tool_kind=ToolInvocationKind.AUTHORITY_ACTION,
            tool_name="authority.decision",
            input_summary=request_id,
            output_summary=decision,
            metadata={"task_id": task_id, "actor_id": actor_id, "resume_target": resume_target},
        )
        return stored

    async def record_activation_intent(
        self,
        session_id: str,
        *,
        reason: str = "",
        requested_by: str | None = None,
        activation_epoch: int | None = None,
    ) -> AgentSession:
        current = await self.load_session(session_id)
        if current is None:
            raise ValueError(f"Unknown session_id: {session_id}")
        now_iso = _now_iso()
        state = TeammateSlotSessionState.from_metadata(current.metadata)
        if activation_epoch is not None:
            state.activation_epoch = int(activation_epoch)
        else:
            state.activation_epoch = (state.activation_epoch or 0) + 1
        state.last_activation_intent_at = now_iso
        if reason:
            state.last_activation_reason = reason
        if requested_by is not None:
            state.last_activation_requested_by = requested_by
        metadata = dict(current.metadata)
        metadata.update(state.to_metadata_patch())
        return await self.update_session(
            session_id,
            phase=ResidentCoordinatorPhase.WAITING_FOR_MAILBOX,
            reason=reason if reason else "Activation intent recorded.",
            metadata=metadata,
            last_progress_at=now_iso,
        )

    async def record_wake_request(
        self,
        session_id: str,
        *,
        reason: str = "",
        requested_by: str | None = None,
    ) -> AgentSession:
        current = await self.load_session(session_id)
        if current is None:
            raise ValueError(f"Unknown session_id: {session_id}")
        now_iso = _now_iso()
        state = TeammateSlotSessionState.from_metadata(current.metadata)
        state.wake_request_count = (state.wake_request_count or 0) + 1
        state.last_wake_request_at = now_iso
        state.last_active_at = now_iso
        state.idle_since = None
        if reason:
            state.last_wake_reason = reason
        if requested_by is not None:
            state.last_wake_requested_by = requested_by
        metadata = dict(current.metadata)
        metadata.update(state.to_metadata_patch(include_none_keys=("idle_since",)))
        return await self.update_session(
            session_id,
            phase=ResidentCoordinatorPhase.RUNNING,
            reason=reason if reason else "Wake request recorded.",
            metadata=metadata,
            last_progress_at=now_iso,
        )

    async def record_teammate_slot_state(
        self,
        session_id: str,
        *,
        activation_epoch: int | None = None,
        current_task_id: str | None = None,
        current_claim_session_id: str | None = None,
        last_claim_source: str | None = None,
        current_worker_session_id: str | None = None,
        last_worker_session_id: str | None = None,
        idle: bool,
        reason: str = "",
    ) -> AgentSession:
        projected = await self.project_teammate_slot_state(
            session_id,
            activation_epoch=activation_epoch,
            current_task_id=current_task_id,
            current_claim_session_id=current_claim_session_id,
            last_claim_source=last_claim_source,
            current_worker_session_id=current_worker_session_id,
            last_worker_session_id=last_worker_session_id,
            idle=idle,
            reason=reason,
        )
        return await self.save_session(projected)

    async def project_teammate_activation_state(
        self,
        session_id: str,
        *,
        session: AgentSession | None = None,
        activation_epoch: int | None = None,
        current_claim_session_id: str | None = None,
        last_claim_source: str | None = None,
        current_directive_ids: tuple[str, ...] = (),
        reason: str = "",
    ) -> AgentSession:
        projected = await self.project_teammate_slot_state(
            session_id,
            session=session,
            activation_epoch=activation_epoch,
            current_task_id=None,
            current_claim_session_id=current_claim_session_id,
            last_claim_source=last_claim_source,
            current_worker_session_id=None,
            last_worker_session_id=None,
            idle=True,
            reason=reason,
        )
        return await self.project_session_update(
            session_id,
            session=projected,
            current_directive_ids=current_directive_ids,
        )

    async def project_teammate_slot_state(
        self,
        session_id: str,
        *,
        session: AgentSession | None = None,
        activation_epoch: int | None = None,
        current_task_id: str | None = None,
        current_claim_session_id: str | None = None,
        last_claim_source: str | None = None,
        current_worker_session_id: str | None = None,
        last_worker_session_id: str | None = None,
        idle: bool,
        reason: str = "",
    ) -> AgentSession:
        current = session if session is not None else await self.load_session(session_id)
        if current is None:
            raise ValueError(f"Unknown session_id: {session_id}")
        metadata = dict(current.metadata)
        state = TeammateSlotSessionState.from_metadata(metadata)
        if activation_epoch is not None:
            state.activation_epoch = int(activation_epoch)
        state.current_task_id = current_task_id
        state.current_claim_session_id = current_claim_session_id
        if last_claim_source is not None:
            state.last_claim_source = last_claim_source
        state.current_worker_session_id = current_worker_session_id
        if last_worker_session_id is not None:
            state.last_worker_session_id = last_worker_session_id
        now_iso = _now_iso()
        if not idle:
            state.last_active_at = now_iso
            state.idle_since = None
        else:
            state.idle_since = state.idle_since if _is_iso_timestamp(state.idle_since) else now_iso
        metadata.update(
            state.to_metadata_patch(
                include_none_keys=(
                    "current_task_id",
                    "current_claim_session_id",
                    "current_worker_session_id",
                    "idle_since",
                )
            )
        )
        phase = ResidentCoordinatorPhase.IDLE if idle else ResidentCoordinatorPhase.RUNNING
        return await self.project_session_update(
            session_id,
            session=current,
            phase=phase,
            reason=reason,
            metadata=metadata,
        )


class StoreBackedResidentSessionHost(ResidentSessionHost):
    def __init__(self, store: OrchestrationStore) -> None:
        self.store = store

    async def _project_coordination_worker_session(
        self,
        *,
        session_id: str,
        agent_session: AgentSession,
        mailbox_cursor: Mapping[str, Any] | None = None,
    ) -> WorkerSession | None:
        persisted_worker_session = await self.store.get_worker_session(session_id)
        if persisted_worker_session is None:
            return None
        metadata = dict(persisted_worker_session.metadata)
        if agent_session.objective_id is not None:
            metadata["objective_id"] = agent_session.objective_id
        if agent_session.lane_id is not None:
            metadata["lane_id"] = agent_session.lane_id
        if agent_session.team_id is not None:
            metadata["team_id"] = agent_session.team_id
        return replace(
            WorkerSession.from_dict(persisted_worker_session.to_dict()),
            mailbox_cursor=(
                {str(key): value for key, value in mailbox_cursor.items()}
                if mailbox_cursor is not None
                else dict(agent_session.mailbox_cursor)
            ),
            metadata=metadata,
        )

    async def _project_loaded_session(
        self,
        session: AgentSession,
    ) -> AgentSession:
        worker_session = await self.store.get_worker_session(session.session_id)
        if worker_session is None:
            return AgentSession.from_dict(session.to_dict())
        projected = await self.project_worker_session_state(
            worker_session=worker_session,
            phase=session.phase,
            reason=session.last_reason,
            session=session,
        )
        return AgentSession.from_dict(projected.to_dict())

    async def _save_persisted_resident_team_shell(
        self,
        shell: ResidentTeamShell,
    ) -> ResidentTeamShell:
        await self.store.save_resident_team_shell(shell)
        stored = await self.store.get_resident_team_shell(shell.resident_team_shell_id)
        if stored is None:
            return ResidentTeamShell.from_payload(shell.to_dict())
        return ResidentTeamShell.from_payload(stored.to_dict())

    async def _get_persisted_resident_team_shell(
        self,
        resident_team_shell_id: str,
    ) -> ResidentTeamShell | None:
        shell = await self.store.get_resident_team_shell(resident_team_shell_id)
        if shell is None:
            return None
        return ResidentTeamShell.from_payload(shell.to_dict())

    async def _list_persisted_resident_team_shells(
        self,
        *,
        work_session_id: str | None = None,
    ) -> tuple[ResidentTeamShell, ...]:
        if work_session_id is None:
            return ()
        return tuple(await self.store.list_resident_team_shells(work_session_id))

    async def save_session(self, session: AgentSession) -> AgentSession:
        synchronized = self._sync_worker_session_truth(session)
        stored = AgentSession.from_dict(synchronized.to_dict())
        await self.store.save_agent_session(stored)
        await self._sync_resident_team_shell_projection(session=stored)
        return AgentSession.from_dict(stored.to_dict())

    async def load_session(self, session_id: str) -> AgentSession | None:
        session = await self.store.get_agent_session(session_id)
        if session is None:
            return None
        return await self._project_loaded_session(session)

    async def _list_sessions(self) -> tuple[AgentSession, ...]:
        sessions = await self.store.list_agent_sessions()
        projected: list[AgentSession] = []
        for session in sessions:
            projected.append(await self._project_loaded_session(session))
        return tuple(projected)

    async def get_session(self, session_id: str) -> AgentSession | None:
        return await self.load_session(session_id)

    async def bind_session(self, session_id: str, binding: SessionBinding) -> AgentSession:
        session = await self.load_session(session_id)
        if session is None:
            raise ValueError(f"Unknown session_id: {session_id}")
        updated = replace(session, current_binding=SessionBinding.from_dict(binding.to_dict()))
        return await self.save_session(updated)

    async def mark_phase(
        self,
        session_id: str,
        phase: ResidentCoordinatorPhase,
        *,
        reason: str = "",
    ) -> AgentSession:
        session = await self.load_session(session_id)
        if session is None:
            raise ValueError(f"Unknown session_id: {session_id}")
        updated = replace(session, phase=phase, last_reason=reason)
        return await self.save_session(updated)

    async def reclaim_session(
        self,
        session_id: str,
        *,
        new_supervisor_id: str,
        new_lease_id: str,
        new_expires_at: str,
    ) -> AgentSession:
        session = await self.load_session(session_id)
        if session is None:
            raise ValueError(f"Unknown session_id: {session_id}")
        binding = session.current_binding or SessionBinding(
            session_id=session.session_id,
            backend="unknown",
            binding_type="ephemeral",
        )
        updated_binding = replace(
            binding,
            supervisor_id=new_supervisor_id,
            lease_id=new_lease_id,
            lease_expires_at=new_expires_at,
        )
        updated = replace(session, current_binding=updated_binding)
        return await self.save_session(updated)

    async def commit_mailbox_consume(
        self,
        session_id: str,
        *,
        recipient: str,
        envelope_ids: tuple[str, ...],
        current_directive_ids: tuple[str, ...] = (),
        reason: str = "",
        persist_cursor: Callable[[str, dict[str, str | None]], Awaitable[None]] | None = None,
        acknowledge_bridge: Callable[[str, tuple[str, ...]], Awaitable[str | None]] | None = None,
    ) -> AgentSession:
        current = await self.load_session(session_id)
        if current is None:
            raise ValueError(f"Unknown session_id: {session_id}")
        if not envelope_ids:
            if reason:
                return await self.update_session(session_id, reason=reason)
            return current

        projected = await self.project_mailbox_consume(
            session_id,
            session=current,
            recipient=recipient,
            envelope_ids=envelope_ids,
            current_directive_ids=current_directive_ids,
            reason=reason,
        )
        cursor_payload = {
            str(key): (str(value) if value is not None else None)
            for key, value in dict(projected.mailbox_cursor).items()
        }
        worker_session_snapshot = await self._project_coordination_worker_session(
            session_id=session_id,
            agent_session=projected,
            mailbox_cursor=cursor_payload,
        )
        await self.store.commit_mailbox_consume(
            MailboxConsumeStoreCommit(
                recipient=recipient,
                envelope_ids=envelope_ids,
                protocol_bus_cursor=ProtocolBusCursorCommit(
                    stream="mailbox",
                    consumer=recipient,
                    cursor=cursor_payload,
                ),
                agent_session=projected,
                worker_session=worker_session_snapshot,
            )
        )
        if (
            worker_session_snapshot is not None
            and not getattr(
                self.store, "supports_worker_session_coordination_transactions", False
            )
        ):
            await self.store.save_worker_session(worker_session_snapshot)
        if acknowledge_bridge is not None:
            await acknowledge_bridge(recipient, envelope_ids)
        persisted = await self.load_session(session_id)
        if persisted is not None:
            await self._sync_resident_team_shell_projection(session=persisted)
        work_session_id, runtime_generation_id = _continuity_ids_from_metadata(
            projected.metadata if isinstance(projected.metadata, Mapping) else {}
        )
        await self._record_tool_invocation_without_turn(
            work_session_id=work_session_id,
            runtime_generation_id=runtime_generation_id,
            tool_kind=ToolInvocationKind.MAILBOX_COMMIT,
            tool_name="mailbox.commit",
            input_summary=",".join(envelope_ids),
            output_summary=f"committed {len(envelope_ids)} mailbox envelopes",
            metadata={"recipient": recipient},
        )
        return persisted or projected


class InMemoryResidentSessionHost(ResidentSessionHost):
    def __init__(self) -> None:
        self._sessions: dict[str, AgentSession] = {}
        self._resident_team_shells: dict[str, ResidentTeamShell] = {}

    async def _save_persisted_resident_team_shell(
        self,
        shell: ResidentTeamShell,
    ) -> ResidentTeamShell:
        stored = ResidentTeamShell.from_payload(shell.to_dict())
        self._resident_team_shells[stored.resident_team_shell_id] = stored
        return ResidentTeamShell.from_payload(stored.to_dict())

    async def _get_persisted_resident_team_shell(
        self,
        resident_team_shell_id: str,
    ) -> ResidentTeamShell | None:
        shell = self._resident_team_shells.get(resident_team_shell_id)
        if shell is None:
            return None
        return ResidentTeamShell.from_payload(shell.to_dict())

    async def _list_persisted_resident_team_shells(
        self,
        *,
        work_session_id: str | None = None,
    ) -> tuple[ResidentTeamShell, ...]:
        shells = [
            ResidentTeamShell.from_payload(shell.to_dict())
            for shell in self._resident_team_shells.values()
            if work_session_id is None or shell.work_session_id == work_session_id
        ]
        shells.sort(key=_resident_team_shell_sort_key)
        return tuple(shells)

    async def save_session(self, session: AgentSession) -> AgentSession:
        synchronized = self._sync_worker_session_truth(session)
        stored = AgentSession.from_dict(synchronized.to_dict())
        self._sessions[stored.session_id] = stored
        await self._sync_resident_team_shell_projection(session=stored)
        return AgentSession.from_dict(stored.to_dict())

    async def load_session(self, session_id: str) -> AgentSession | None:
        session = self._sessions.get(session_id)
        if session is None:
            return None
        return AgentSession.from_dict(session.to_dict())

    async def _list_sessions(self) -> tuple[AgentSession, ...]:
        return tuple(
            AgentSession.from_dict(session.to_dict())
            for session in self._sessions.values()
        )

    async def get_session(self, session_id: str) -> AgentSession | None:
        return await self.load_session(session_id)

    async def bind_session(self, session_id: str, binding: SessionBinding) -> AgentSession:
        session = self._require_session(session_id)
        updated = replace(session, current_binding=SessionBinding.from_dict(binding.to_dict()))
        return await self.save_session(updated)

    async def mark_phase(
        self,
        session_id: str,
        phase: ResidentCoordinatorPhase,
        *,
        reason: str = "",
    ) -> AgentSession:
        session = self._require_session(session_id)
        updated = replace(session, phase=phase, last_reason=reason)
        return await self.save_session(updated)

    async def reclaim_session(
        self,
        session_id: str,
        *,
        new_supervisor_id: str,
        new_lease_id: str,
        new_expires_at: str,
    ) -> AgentSession:
        session = self._require_session(session_id)
        binding = session.current_binding or SessionBinding(
            session_id=session.session_id,
            backend="unknown",
            binding_type="ephemeral",
        )
        updated_binding = replace(
            binding,
            supervisor_id=new_supervisor_id,
            lease_id=new_lease_id,
            lease_expires_at=new_expires_at,
        )
        updated = replace(session, current_binding=updated_binding)
        return await self.save_session(updated)

    def _require_session(self, session_id: str) -> AgentSession:
        try:
            return self._sessions[session_id]
        except KeyError as exc:
            raise ValueError(f"Unknown session_id: {session_id}") from exc
