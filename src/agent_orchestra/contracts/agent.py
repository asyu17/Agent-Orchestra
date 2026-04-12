from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping

from agent_orchestra.contracts.execution import ResidentCoordinatorPhase, ResidentCoordinatorSession
from agent_orchestra.contracts.worker_protocol import (
    WorkerExecutionContract,
    WorkerLeasePolicy,
    WorkerRoleProfile,
)


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return str(value)


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _mailbox_cursor_last_envelope_id(cursor: object) -> str | None:
    if not isinstance(cursor, Mapping):
        return None
    for key in ("last_envelope_id", "event_id", "offset"):
        last_envelope_id = _optional_str(cursor.get(key))
        if last_envelope_id is not None:
            return last_envelope_id
    mailbox_cursor = cursor.get("mailbox")
    if isinstance(mailbox_cursor, Mapping):
        return _mailbox_cursor_last_envelope_id(mailbox_cursor)
    return None


@dataclass(slots=True)
class Agent:
    agent_id: str
    role_kind: str
    scope_id: str
    profile_id: str
    session_id: str
    mailbox_binding: str
    task_surface_binding: str
    blackboard_binding: str
    policy_bundle: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class RolePolicy:
    role_kind: str
    visible_scopes: tuple[str, ...] = ()
    writable_scopes: tuple[str, ...] = ()
    can_spawn_subordinates: bool = False
    allowed_subordinate_roles: tuple[str, ...] = ()
    can_run_prompt_turn: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class AuthorityPolicy:
    mutation_scopes: tuple[str, ...] = ()
    readable_scopes: tuple[str, ...] = ()
    allowed_directive_roles: tuple[str, ...] = ()
    can_create_tasks: bool = False
    can_spawn_subordinates: bool = False
    can_write_blackboard: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ClaimPolicy:
    allow_directed_claims: bool = True
    allow_autonomous_claims: bool = False
    claim_scopes: tuple[str, ...] = ()
    require_reason: bool = False
    require_derived_from: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class PromptTriggerPolicy:
    allow_prompt_turns: bool = True
    trigger_kinds: tuple[str, ...] = ()
    max_idle_cycles_before_prompt: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class AgentActionKind(str, Enum):
    RUN_PROMPT_TURN = "run_prompt_turn"
    CLAIM_TASK = "claim_task"
    UPDATE_TASK = "update_task"
    APPEND_BLACKBOARD_ENTRY = "append_blackboard_entry"
    SEND_MESSAGE = "send_message"
    SUBSCRIBE_MESSAGES = "subscribe_messages"
    UNSUBSCRIBE_MESSAGES = "unsubscribe_messages"
    SPAWN_SUBORDINATE = "spawn_subordinate"
    IDLE = "idle"
    ESCALATE = "escalate"
    SHUTDOWN = "shutdown"


@dataclass(slots=True)
class AgentAction:
    kind: AgentActionKind
    agent_id: str
    reason: str = ""
    target_task_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class SessionBinding:
    session_id: str
    backend: str
    binding_type: str = "ephemeral"
    transport_locator: dict[str, Any] = field(default_factory=dict)
    supervisor_id: str | None = None
    lease_id: str | None = None
    lease_expires_at: str | None = None
    handle_snapshot: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "backend": self.backend,
            "binding_type": self.binding_type,
            "transport_locator": _json_safe(dict(self.transport_locator)),
            "supervisor_id": self.supervisor_id,
            "lease_id": self.lease_id,
            "lease_expires_at": self.lease_expires_at,
            "handle_snapshot": _json_safe(dict(self.handle_snapshot)),
            "metadata": _json_safe(dict(self.metadata)),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> SessionBinding:
        return cls(
            session_id=str(payload["session_id"]),
            backend=str(payload["backend"]),
            binding_type=str(payload.get("binding_type", "ephemeral")),
            transport_locator=dict(payload.get("transport_locator", {})),
            supervisor_id=str(payload["supervisor_id"]) if payload.get("supervisor_id") is not None else None,
            lease_id=str(payload["lease_id"]) if payload.get("lease_id") is not None else None,
            lease_expires_at=(
                str(payload["lease_expires_at"]) if payload.get("lease_expires_at") is not None else None
            ),
            handle_snapshot=dict(payload.get("handle_snapshot", {})),
            metadata=dict(payload.get("metadata", {})),
        )


@dataclass(slots=True)
class AgentSession:
    session_id: str
    agent_id: str
    role: str
    phase: ResidentCoordinatorPhase = ResidentCoordinatorPhase.BOOTING
    objective_id: str | None = None
    lane_id: str | None = None
    team_id: str | None = None
    mailbox_cursor: dict[str, Any] = field(default_factory=dict)
    subscription_cursors: dict[str, dict[str, Any]] = field(default_factory=dict)
    claimed_task_ids: tuple[str, ...] = ()
    current_directive_ids: tuple[str, ...] = ()
    current_binding: SessionBinding | None = None
    current_worker_session_id: str | None = None
    last_worker_session_id: str | None = None
    lease_id: str | None = None
    lease_expires_at: str | None = None
    last_progress_at: str | None = None
    last_reason: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "agent_id": self.agent_id,
            "role": self.role,
            "phase": self.phase.value,
            "objective_id": self.objective_id,
            "lane_id": self.lane_id,
            "team_id": self.team_id,
            "mailbox_cursor": _json_safe(dict(self.mailbox_cursor)),
            "subscription_cursors": _json_safe(dict(self.subscription_cursors)),
            "claimed_task_ids": list(self.claimed_task_ids),
            "current_directive_ids": list(self.current_directive_ids),
            "current_binding": self.current_binding.to_dict() if self.current_binding is not None else None,
            "current_worker_session_id": self.current_worker_session_id,
            "last_worker_session_id": self.last_worker_session_id,
            "lease_id": self.lease_id,
            "lease_expires_at": self.lease_expires_at,
            "last_progress_at": self.last_progress_at,
            "last_reason": self.last_reason,
            "metadata": _json_safe(dict(self.metadata)),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> AgentSession:
        binding_payload = payload.get("current_binding")
        binding = SessionBinding.from_dict(binding_payload) if isinstance(binding_payload, Mapping) else None
        return cls(
            session_id=str(payload["session_id"]),
            agent_id=str(payload["agent_id"]),
            role=str(payload["role"]),
            phase=ResidentCoordinatorPhase(str(payload.get("phase", ResidentCoordinatorPhase.BOOTING.value))),
            objective_id=str(payload["objective_id"]) if payload.get("objective_id") is not None else None,
            lane_id=str(payload["lane_id"]) if payload.get("lane_id") is not None else None,
            team_id=str(payload["team_id"]) if payload.get("team_id") is not None else None,
            mailbox_cursor=dict(payload.get("mailbox_cursor", {})),
            subscription_cursors={
                str(key): dict(value) for key, value in dict(payload.get("subscription_cursors", {})).items()
            },
            claimed_task_ids=tuple(str(item) for item in payload.get("claimed_task_ids", ())),
            current_directive_ids=tuple(str(item) for item in payload.get("current_directive_ids", ())),
            current_binding=binding,
            current_worker_session_id=(
                str(payload["current_worker_session_id"])
                if payload.get("current_worker_session_id") is not None
                else None
            ),
            last_worker_session_id=(
                str(payload["last_worker_session_id"])
                if payload.get("last_worker_session_id") is not None
                else None
            ),
            lease_id=str(payload["lease_id"]) if payload.get("lease_id") is not None else None,
            lease_expires_at=(
                str(payload["lease_expires_at"]) if payload.get("lease_expires_at") is not None else None
            ),
            last_progress_at=(
                str(payload["last_progress_at"]) if payload.get("last_progress_at") is not None else None
            ),
            last_reason=str(payload.get("last_reason", "")),
            metadata=dict(payload.get("metadata", {})),
        )


@dataclass(slots=True, frozen=True)
class AgentWorkerSessionTruth:
    agent_session_id: str
    bound_worker_session_id: str | None = None
    current_worker_session_id: str | None = None
    last_worker_session_id: str | None = None
    source: str = "agent_session"

    @staticmethod
    def _optional_str(value: object) -> str | None:
        if value is None:
            return None
        text = str(value)
        return text if text else None

    @classmethod
    def from_agent_session(cls, session: AgentSession) -> AgentWorkerSessionTruth:
        explicit_current = cls._optional_str(session.current_worker_session_id)
        explicit_last = cls._optional_str(session.last_worker_session_id)
        metadata = session.metadata if isinstance(session.metadata, Mapping) else {}
        metadata_bound = cls._optional_str(metadata.get("bound_worker_session_id"))
        metadata_current = cls._optional_str(metadata.get("current_worker_session_id"))
        metadata_last = cls._optional_str(metadata.get("last_worker_session_id"))

        current = explicit_current or metadata_bound or metadata_current
        last = explicit_last or metadata_last or current
        source = (
            "agent_session_fields"
            if explicit_current is not None or explicit_last is not None
            else "metadata"
        )

        return cls(
            agent_session_id=session.session_id,
            bound_worker_session_id=current,
            current_worker_session_id=current,
            last_worker_session_id=last,
            source=source,
        )

    def to_metadata_patch(
        self,
        *,
        include_none_keys: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "bound_worker_session_id": self.bound_worker_session_id,
            "current_worker_session_id": self.current_worker_session_id,
            "last_worker_session_id": self.last_worker_session_id,
        }
        include_none = set(include_none_keys)
        return {
            key: value
            for key, value in payload.items()
            if value is not None or key in include_none
        }


_COORDINATOR_SESSION_METADATA_KEYS = frozenset(
    {
        "host_owner_coordinator_id",
        "runtime_task_id",
        "cycle_count",
        "prompt_turn_count",
        "claimed_task_count",
        "subordinate_dispatch_count",
        "mailbox_poll_count",
        "active_subordinate_ids",
    }
)


@dataclass(slots=True)
class CoordinatorSessionState:
    session_id: str
    coordinator_id: str
    role: str
    phase: ResidentCoordinatorPhase
    objective_id: str | None = None
    lane_id: str | None = None
    team_id: str | None = None
    host_owner_coordinator_id: str | None = None
    runtime_task_id: str | None = None
    cycle_count: int = 0
    prompt_turn_count: int = 0
    claimed_task_count: int = 0
    subordinate_dispatch_count: int = 0
    mailbox_poll_count: int = 0
    active_subordinate_ids: tuple[str, ...] = ()
    mailbox_cursor: str | None = None
    last_reason: str = ""
    last_progress_at: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_agent_session(cls, session: AgentSession) -> CoordinatorSessionState:
        metadata = session.metadata if isinstance(session.metadata, Mapping) else {}
        active_subordinate_ids = metadata.get("active_subordinate_ids", ())
        if isinstance(active_subordinate_ids, str):
            active_subordinate_ids = (active_subordinate_ids,)
        elif not isinstance(active_subordinate_ids, (list, tuple)):
            active_subordinate_ids = ()
        projected_metadata = {
            str(key): value
            for key, value in metadata.items()
            if str(key) not in _COORDINATOR_SESSION_METADATA_KEYS
        }
        return cls(
            session_id=session.session_id,
            coordinator_id=session.agent_id,
            role=session.role,
            phase=session.phase,
            objective_id=session.objective_id,
            lane_id=session.lane_id,
            team_id=session.team_id,
            host_owner_coordinator_id=_optional_str(metadata.get("host_owner_coordinator_id")),
            runtime_task_id=_optional_str(metadata.get("runtime_task_id")),
            cycle_count=_optional_int(metadata.get("cycle_count")) or 0,
            prompt_turn_count=_optional_int(metadata.get("prompt_turn_count")) or 0,
            claimed_task_count=_optional_int(metadata.get("claimed_task_count")) or 0,
            subordinate_dispatch_count=_optional_int(metadata.get("subordinate_dispatch_count")) or 0,
            mailbox_poll_count=_optional_int(metadata.get("mailbox_poll_count")) or 0,
            active_subordinate_ids=tuple(
                str(item) for item in active_subordinate_ids if str(item)
            ),
            mailbox_cursor=_mailbox_cursor_last_envelope_id(session.mailbox_cursor),
            last_reason=session.last_reason,
            last_progress_at=session.last_progress_at,
            metadata=projected_metadata,
        )

    @classmethod
    def from_resident_session(
        cls,
        *,
        session_id: str,
        coordinator_session: ResidentCoordinatorSession,
        host_owner_coordinator_id: str | None = None,
        runtime_task_id: str | None = None,
        last_progress_at: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> CoordinatorSessionState:
        projected_metadata = dict(coordinator_session.metadata)
        if metadata is not None:
            projected_metadata.update({str(key): value for key, value in metadata.items()})
        return cls(
            session_id=session_id,
            coordinator_id=coordinator_session.coordinator_id,
            role=coordinator_session.role,
            phase=coordinator_session.phase,
            objective_id=coordinator_session.objective_id,
            lane_id=coordinator_session.lane_id,
            team_id=coordinator_session.team_id,
            host_owner_coordinator_id=host_owner_coordinator_id,
            runtime_task_id=runtime_task_id,
            cycle_count=coordinator_session.cycle_count,
            prompt_turn_count=coordinator_session.prompt_turn_count,
            claimed_task_count=coordinator_session.claimed_task_count,
            subordinate_dispatch_count=coordinator_session.subordinate_dispatch_count,
            mailbox_poll_count=coordinator_session.mailbox_poll_count,
            active_subordinate_ids=tuple(str(item) for item in coordinator_session.active_subordinate_ids if str(item)),
            mailbox_cursor=_optional_str(coordinator_session.mailbox_cursor),
            last_reason=coordinator_session.last_reason,
            last_progress_at=last_progress_at,
            metadata=projected_metadata,
        )

    @staticmethod
    def mailbox_cursor_payload(last_envelope_id: str | None) -> dict[str, Any]:
        if last_envelope_id is None:
            return {}
        return {
            "stream": "mailbox",
            "event_id": last_envelope_id,
            "last_envelope_id": last_envelope_id,
        }

    def to_metadata_patch(
        self,
        *,
        include_none_keys: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "host_owner_coordinator_id": self.host_owner_coordinator_id,
            "runtime_task_id": self.runtime_task_id,
            "cycle_count": self.cycle_count,
            "prompt_turn_count": self.prompt_turn_count,
            "claimed_task_count": self.claimed_task_count,
            "subordinate_dispatch_count": self.subordinate_dispatch_count,
            "mailbox_poll_count": self.mailbox_poll_count,
            "active_subordinate_ids": list(self.active_subordinate_ids),
        }
        payload.update({str(key): value for key, value in self.metadata.items()})
        include_none = set(include_none_keys)
        return {
            key: value
            for key, value in payload.items()
            if value is not None or key in include_none
        }


@dataclass(slots=True)
class TeammateSlotSessionState:
    activation_epoch: int | None = None
    current_task_id: str | None = None
    current_claim_session_id: str | None = None
    last_claim_source: str | None = None
    current_worker_session_id: str | None = None
    last_worker_session_id: str | None = None
    last_active_at: str | None = None
    idle_since: str | None = None
    last_activation_intent_at: str | None = None
    last_activation_reason: str | None = None
    last_activation_requested_by: str | None = None
    wake_request_count: int | None = None
    last_wake_request_at: str | None = None
    last_wake_reason: str | None = None
    last_wake_requested_by: str | None = None

    @classmethod
    def from_metadata(cls, metadata: Mapping[str, Any]) -> TeammateSlotSessionState:
        def _optional_int(key: str) -> int | None:
            raw = metadata.get(key)
            if raw is None:
                return None
            try:
                return int(raw)
            except (TypeError, ValueError):
                return None

        def _optional_str(key: str) -> str | None:
            raw = metadata.get(key)
            if raw is None:
                return None
            text = str(raw)
            return text if text else None

        return cls(
            activation_epoch=_optional_int("activation_epoch"),
            current_task_id=_optional_str("current_task_id"),
            current_claim_session_id=_optional_str("current_claim_session_id"),
            last_claim_source=_optional_str("last_claim_source"),
            current_worker_session_id=_optional_str("current_worker_session_id"),
            last_worker_session_id=_optional_str("last_worker_session_id"),
            last_active_at=_optional_str("last_active_at"),
            idle_since=_optional_str("idle_since"),
            last_activation_intent_at=_optional_str("last_activation_intent_at"),
            last_activation_reason=_optional_str("last_activation_reason"),
            last_activation_requested_by=_optional_str("last_activation_requested_by"),
            wake_request_count=_optional_int("wake_request_count"),
            last_wake_request_at=_optional_str("last_wake_request_at"),
            last_wake_reason=_optional_str("last_wake_reason"),
            last_wake_requested_by=_optional_str("last_wake_requested_by"),
        )

    def to_metadata_patch(
        self,
        *,
        include_none_keys: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "activation_epoch": self.activation_epoch,
            "current_task_id": self.current_task_id,
            "current_claim_session_id": self.current_claim_session_id,
            "last_claim_source": self.last_claim_source,
            "current_worker_session_id": self.current_worker_session_id,
            "last_worker_session_id": self.last_worker_session_id,
            "last_active_at": self.last_active_at,
            "idle_since": self.idle_since,
            "last_activation_intent_at": self.last_activation_intent_at,
            "last_activation_reason": self.last_activation_reason,
            "last_activation_requested_by": self.last_activation_requested_by,
            "wake_request_count": self.wake_request_count,
            "last_wake_request_at": self.last_wake_request_at,
            "last_wake_reason": self.last_wake_reason,
            "last_wake_requested_by": self.last_wake_requested_by,
        }
        include_none = set(include_none_keys)
        return {
            key: value
            for key, value in payload.items()
            if value is not None or key in include_none
        }


@dataclass(slots=True)
class TeammateActivationProfile:
    backend: str | None = None
    working_dir: str | None = None
    role_profile_id: str | None = None
    role_profile: WorkerRoleProfile | None = None

    def __post_init__(self) -> None:
        if self.role_profile is not None:
            if self.backend is None:
                self.backend = self.role_profile.backend
            if self.role_profile_id is None:
                self.role_profile_id = self.role_profile.profile_id

    @classmethod
    def from_metadata(
        cls,
        metadata: Mapping[str, Any],
    ) -> TeammateActivationProfile:
        payload = metadata.get("activation_profile")
        if not isinstance(payload, Mapping):
            return cls()
        role_profile_payload = payload.get("role_profile")
        role_profile = (
            cls._role_profile_from_payload(role_profile_payload)
            if isinstance(role_profile_payload, Mapping)
            else None
        )
        role_profile_id = _optional_str(payload.get("role_profile_id"))
        if role_profile_id is None and role_profile is not None:
            role_profile_id = role_profile.profile_id
        return cls(
            backend=_optional_str(payload.get("backend")),
            working_dir=_optional_str(payload.get("working_dir")),
            role_profile_id=role_profile_id,
            role_profile=role_profile,
        )

    @staticmethod
    def _role_profile_from_payload(
        payload: Mapping[str, Any],
    ) -> WorkerRoleProfile | None:
        profile_id = _optional_str(payload.get("profile_id"))
        backend = _optional_str(payload.get("backend"))
        execution_contract_payload = payload.get("execution_contract")
        lease_policy_payload = payload.get("lease_policy")
        if (
            profile_id is None
            or backend is None
            or not isinstance(execution_contract_payload, Mapping)
            or not isinstance(lease_policy_payload, Mapping)
        ):
            return None
        contract_id = _optional_str(execution_contract_payload.get("contract_id"))
        mode = _optional_str(execution_contract_payload.get("mode"))
        if contract_id is None or mode is None:
            return None
        try:
            execution_contract = WorkerExecutionContract(
                contract_id=contract_id,
                mode=mode,
                allow_subdelegation=bool(execution_contract_payload.get("allow_subdelegation", False)),
                require_final_report=bool(execution_contract_payload.get("require_final_report", True)),
                require_verification_results=bool(
                    execution_contract_payload.get("require_verification_results", False)
                ),
                required_verification_commands=tuple(
                    str(item)
                    for item in execution_contract_payload.get("required_verification_commands", ())
                    if str(item)
                ),
                completion_requires_verification_success=bool(
                    execution_contract_payload.get("completion_requires_verification_success", False)
                ),
                required_artifact_kinds=tuple(
                    str(item)
                    for item in execution_contract_payload.get("required_artifact_kinds", ())
                    if str(item)
                ),
                progress_policy=dict(execution_contract_payload.get("progress_policy", {})),
                metadata=dict(execution_contract_payload.get("metadata", {})),
            )
            lease_policy = WorkerLeasePolicy(
                accept_deadline_seconds=float(lease_policy_payload["accept_deadline_seconds"]),
                renewal_timeout_seconds=float(lease_policy_payload["renewal_timeout_seconds"]),
                hard_deadline_seconds=float(lease_policy_payload["hard_deadline_seconds"]),
                renew_on_event_kinds=tuple(
                    str(item)
                    for item in lease_policy_payload.get("renew_on_event_kinds", ())
                    if str(item)
                ),
                max_silence_seconds=(
                    float(lease_policy_payload["max_silence_seconds"])
                    if lease_policy_payload.get("max_silence_seconds") is not None
                    else None
                ),
            )
            return WorkerRoleProfile(
                profile_id=profile_id,
                backend=backend,
                execution_contract=execution_contract,
                lease_policy=lease_policy,
                keep_session_idle=bool(payload.get("keep_session_idle", False)),
                reactivate_idle_session=bool(payload.get("reactivate_idle_session", True)),
                fallback_idle_timeout_seconds=(
                    float(payload["fallback_idle_timeout_seconds"])
                    if payload.get("fallback_idle_timeout_seconds") is not None
                    else None
                ),
                fallback_hard_timeout_seconds=(
                    float(payload["fallback_hard_timeout_seconds"])
                    if payload.get("fallback_hard_timeout_seconds") is not None
                    else None
                ),
                fallback_max_attempts=int(payload.get("fallback_max_attempts", 2)),
                fallback_attempt_timeout_seconds=(
                    float(payload["fallback_attempt_timeout_seconds"])
                    if payload.get("fallback_attempt_timeout_seconds") is not None
                    else None
                ),
                fallback_resume_on_timeout=bool(payload.get("fallback_resume_on_timeout", True)),
                fallback_resume_on_failure=bool(payload.get("fallback_resume_on_failure", False)),
                fallback_allow_relaunch=bool(payload.get("fallback_allow_relaunch", True)),
                fallback_escalate_after_attempts=bool(
                    payload.get("fallback_escalate_after_attempts", True)
                ),
                fallback_backoff_seconds=float(payload.get("fallback_backoff_seconds", 0.0)),
                fallback_provider_unavailable_backoff_initial_seconds=float(
                    payload.get("fallback_provider_unavailable_backoff_initial_seconds", 0.0)
                ),
                fallback_provider_unavailable_backoff_multiplier=float(
                    payload.get("fallback_provider_unavailable_backoff_multiplier", 2.0)
                ),
                fallback_provider_unavailable_backoff_max_seconds=float(
                    payload.get("fallback_provider_unavailable_backoff_max_seconds", 0.0)
                ),
                metadata=dict(payload.get("metadata", {})),
            )
        except (KeyError, TypeError, ValueError):
            return None

    def is_empty(self) -> bool:
        return (
            self.backend is None
            and self.working_dir is None
            and self.role_profile_id is None
            and self.role_profile is None
        )

    def merged_with(
        self,
        fallback: TeammateActivationProfile | None,
    ) -> TeammateActivationProfile:
        if fallback is None:
            return TeammateActivationProfile(
                backend=self.backend,
                working_dir=self.working_dir,
                role_profile_id=self.role_profile_id,
                role_profile=self.role_profile,
            )
        return TeammateActivationProfile(
            backend=self.backend if self.backend is not None else fallback.backend,
            working_dir=(
                self.working_dir
                if self.working_dir is not None
                else fallback.working_dir
            ),
            role_profile_id=(
                self.role_profile_id
                if self.role_profile_id is not None
                else fallback.role_profile_id
            ),
            role_profile=(
                self.role_profile
                if self.role_profile is not None
                else fallback.role_profile
            ),
        )

    def to_metadata_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if self.backend is not None:
            payload["backend"] = self.backend
        if self.working_dir is not None:
            payload["working_dir"] = self.working_dir
        if self.role_profile_id is not None:
            payload["role_profile_id"] = self.role_profile_id
        if self.role_profile is not None:
            payload["role_profile"] = self.role_profile.to_dict()
        return payload
