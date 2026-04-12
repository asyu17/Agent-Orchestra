from __future__ import annotations

import asyncio
import inspect
import json
import signal
import subprocess
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from agent_orchestra.contracts.agent import AgentSession, SessionBinding
from agent_orchestra.contracts.authority import ScopeExtensionRequest
from agent_orchestra.contracts.enums import WorkerStatus
from agent_orchestra.contracts.execution import (
    LaunchBackend,
    ResidentCoordinatorPhase,
    VerificationCommandResult,
    pop_preferred_equivalent_verification_result,
    WorkerAssignment,
    WorkerAttemptDecision,
    WorkerEscalation,
    WorkerExecutionPolicy,
    WorkerFailureKind,
    WorkerHandle,
    WorkerProviderRoute,
    WorkerRecord,
    WorkerResult,
    WorkerSession,
    WorkerSessionStatus,
    WorkerSupervisor,
)
from agent_orchestra.contracts.runner import AgentRunner, RunnerTurnRequest
from agent_orchestra.contracts.worker_protocol import (
    WorkerExecutionContract,
    WorkerFinalReport,
    WorkerFinalStatus,
    WorkerLeasePolicy,
    WorkerLifecycleEvent,
    WorkerLifecycleStatus,
)
from agent_orchestra.runtime.session_host import ResidentSessionHost, StoreBackedResidentSessionHost
from agent_orchestra.runtime.transport_adapter import DefaultTransportAdapter, TransportAdapter
from agent_orchestra.storage.base import OrchestrationStore


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _parse_iso_datetime(value: str | None) -> datetime:
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            pass
    return datetime.now(UTC)


def _signal_name(signum: int) -> str | None:
    try:
        return signal.Signals(signum).name
    except ValueError:
        return None


def _policy_metadata(policy: WorkerExecutionPolicy) -> dict[str, object]:
    return {
        "max_attempts": policy.max_attempts,
        "attempt_timeout_seconds": policy.attempt_timeout_seconds,
        "idle_timeout_seconds": policy.idle_timeout_seconds,
        "hard_timeout_seconds": policy.hard_timeout_seconds,
        "resume_on_timeout": policy.resume_on_timeout,
        "resume_on_failure": policy.resume_on_failure,
        "allow_relaunch": policy.allow_relaunch,
        "escalate_after_attempts": policy.escalate_after_attempts,
        "backoff_seconds": policy.backoff_seconds,
        "provider_unavailable_backoff_initial_seconds": (
            policy.provider_unavailable_backoff_initial_seconds
        ),
        "provider_unavailable_backoff_multiplier": policy.provider_unavailable_backoff_multiplier,
        "provider_unavailable_backoff_max_seconds": policy.provider_unavailable_backoff_max_seconds,
        "keep_session_idle": policy.keep_session_idle,
        "reactivate_idle_session": policy.reactivate_idle_session,
        "fallback_on_provider_unavailable": policy.fallback_on_provider_unavailable,
        "provider_fallbacks": [
            {
                "route_id": route.route_id,
                "backend": route.backend,
                "metadata": dict(route.metadata),
                "environment": dict(route.environment),
                "backoff_seconds": route.backoff_seconds,
            }
            for route in policy.provider_fallbacks
        ],
        "provider_unavailable_error_types": list(policy.provider_unavailable_error_types),
        "provider_unavailable_substrings": list(policy.provider_unavailable_substrings),
    }


_MISSING = object()
_SESSION_RECORD_PREFIX = "__worker_session__:"
_SESSION_RECORD_KIND = "worker_session"


@dataclass(slots=True)
class _ActiveWorkerSession:
    session_id: str
    worker_id: str
    backend: str
    role: str
    handle: WorkerHandle
    status: WorkerSessionStatus
    assignment_id: str | None = None
    lifecycle_status: str | None = None
    started_at: str | None = None
    last_active_at: str | None = None
    idle_since: str | None = None
    protocol_cursor: dict[str, object] = field(default_factory=dict)
    mailbox_cursor: dict[str, object] = field(default_factory=dict)
    supervisor_id: str | None = None
    supervisor_lease_id: str | None = None
    supervisor_lease_expires_at: str | None = None
    reattach_count: int = 0
    last_assignment_id: str | None = None
    last_response_id: str | None = None
    reactivation_count: int = 0
    persist_final_session: bool = False
    metadata: dict[str, object] = field(default_factory=dict)


class DefaultWorkerSupervisor(WorkerSupervisor):
    def __init__(
        self,
        *,
        store: OrchestrationStore,
        launch_backends: Mapping[str, LaunchBackend],
        runner: AgentRunner | None = None,
        poll_interval_seconds: float = 0.05,
        default_timeout_seconds: float = 5.0,
        default_execution_policy: WorkerExecutionPolicy | None = None,
        session_host: ResidentSessionHost | None = None,
        transport_adapter: TransportAdapter | None = None,
    ) -> None:
        self.store = store
        self.launch_backends = dict(launch_backends)
        self.runner = runner
        self.poll_interval_seconds = poll_interval_seconds
        self.default_timeout_seconds = default_timeout_seconds
        self.default_execution_policy = default_execution_policy or WorkerExecutionPolicy()
        self.session_host = session_host or StoreBackedResidentSessionHost(store)
        self.transport_adapter = transport_adapter or DefaultTransportAdapter()
        self.transport_adapter.set_launch_backends(self.launch_backends)
        self._worker_sessions: dict[str, _ActiveWorkerSession] = {}
        self._default_supervisor_id = f"supervisor:{id(self):x}"

    def _session_binding_from_state(
        self,
        *,
        state: _ActiveWorkerSession,
        handle: WorkerHandle | None,
    ) -> SessionBinding:
        return self.transport_adapter.binding_from_handle(
            session_id=state.session_id,
            backend=state.backend,
            binding_type="resident",
            supervisor_id=state.supervisor_id,
            lease_id=state.supervisor_lease_id,
            lease_expires_at=state.supervisor_lease_expires_at,
            handle=handle,
            metadata=dict(state.metadata),
        )

    def _bound_worker_session_id_from_state(
        self,
        *,
        state: _ActiveWorkerSession,
    ) -> str | None:
        if state.status in {
            WorkerSessionStatus.ASSIGNED,
            WorkerSessionStatus.ACTIVE,
            WorkerSessionStatus.IDLE,
        }:
            return state.session_id
        return None

    def _agent_session_from_state(
        self,
        *,
        state: _ActiveWorkerSession,
        assignment: WorkerAssignment | None,
        handle: WorkerHandle,
        phase: ResidentCoordinatorPhase,
        reason: str = "",
    ) -> AgentSession:
        binding = self._session_binding_from_state(state=state, handle=handle)
        metadata = dict(state.metadata)
        metadata.setdefault("worker_session_id", state.session_id)
        current_worker_session_id = self._bound_worker_session_id_from_state(state=state)
        last_worker_session_id = state.session_id or str(metadata.get("last_worker_session_id", "") or "") or None
        metadata["bound_worker_session_id"] = current_worker_session_id
        metadata["current_worker_session_id"] = current_worker_session_id
        metadata["last_worker_session_id"] = last_worker_session_id
        if assignment is not None:
            metadata.setdefault("assignment_id", assignment.assignment_id)
            metadata.setdefault("group_id", assignment.group_id)
            metadata.setdefault("task_id", assignment.task_id)
            if assignment.objective_id is not None:
                metadata.setdefault("objective_id", assignment.objective_id)
            if assignment.team_id is not None:
                metadata.setdefault("team_id", assignment.team_id)
            if assignment.lane_id is not None:
                metadata.setdefault("lane_id", assignment.lane_id)
        claimed_ids = metadata.get("claimed_task_ids")
        if isinstance(claimed_ids, (list, tuple)):
            claimed_task_ids = tuple(str(item) for item in claimed_ids if isinstance(item, str))
        else:
            claimed_task_ids = ()
        return AgentSession(
            session_id=state.session_id,
            agent_id=state.worker_id,
            role=state.role,
            phase=phase,
            objective_id=assignment.objective_id if assignment is not None else metadata.get("objective_id"),
            lane_id=assignment.lane_id if assignment is not None else metadata.get("lane_id"),
            team_id=assignment.team_id if assignment is not None else metadata.get("team_id"),
            mailbox_cursor=dict(state.mailbox_cursor),
            subscription_cursors=dict(metadata.get("subscription_cursors") or {}),
            claimed_task_ids=claimed_task_ids,
            current_binding=binding,
            current_worker_session_id=current_worker_session_id,
            last_worker_session_id=last_worker_session_id,
            lease_id=state.supervisor_lease_id,
            lease_expires_at=state.supervisor_lease_expires_at,
            last_reason=reason or str(metadata.get("last_reason", "")),
            metadata=metadata,
        )

    async def _write_host_session(
        self,
        *,
        worker_session: WorkerSession,
        handle: WorkerHandle,
        phase: ResidentCoordinatorPhase,
        reason: str = "",
    ) -> None:
        binding = self.transport_adapter.binding_from_handle(
            session_id=worker_session.session_id,
            backend=worker_session.backend,
            handle=handle,
            binding_type="resident",
            supervisor_id=worker_session.supervisor_id,
            lease_id=worker_session.supervisor_lease_id,
            lease_expires_at=worker_session.supervisor_lease_expires_at,
            metadata=dict(worker_session.metadata),
        )
        session = await self.session_host.record_worker_session_projection(
            worker_session=worker_session,
            binding=binding,
            phase=phase,
            reason=reason,
        )
        await self.session_host.update_session(
            session.session_id,
            phase=phase,
            reason=reason,
            lease_id=worker_session.supervisor_lease_id,
            lease_expires_at=worker_session.supervisor_lease_expires_at,
        )

    def _worker_session_from_state(
        self,
        *,
        state: _ActiveWorkerSession,
        assignment: WorkerAssignment,
        handle: WorkerHandle,
        status: WorkerSessionStatus,
    ) -> WorkerSession:
        return WorkerSession(
            session_id=state.session_id,
            worker_id=state.worker_id,
            assignment_id=state.assignment_id or assignment.assignment_id,
            backend=state.backend,
            role=state.role,
            status=status,
            lifecycle_status=state.lifecycle_status,
            started_at=state.started_at,
            last_active_at=state.last_active_at,
            idle_since=state.idle_since if status == WorkerSessionStatus.IDLE else None,
            protocol_cursor=dict(state.protocol_cursor),
            mailbox_cursor=dict(state.mailbox_cursor),
            supervisor_id=state.supervisor_id,
            supervisor_lease_id=state.supervisor_lease_id,
            supervisor_lease_expires_at=state.supervisor_lease_expires_at,
            reattach_count=state.reattach_count,
            last_assignment_id=state.last_assignment_id or assignment.assignment_id,
            last_response_id=state.last_response_id,
            reactivation_count=state.reactivation_count,
            handle_snapshot=self.transport_adapter.snapshot_handle(handle),
            metadata=dict(state.metadata),
        )

    def _finalized_phase_for_session(
        self,
        *,
        record: WorkerRecord,
        session_state: _ActiveWorkerSession,
    ) -> ResidentCoordinatorPhase:
        if session_state.status == WorkerSessionStatus.IDLE:
            return ResidentCoordinatorPhase.IDLE
        if record.status == WorkerStatus.COMPLETED:
            return ResidentCoordinatorPhase.QUIESCENT
        return ResidentCoordinatorPhase.FAILED

    async def _call_store_method(self, name: str, *args: object, **kwargs: object) -> object:
        method = getattr(self.store, name, None)
        if method is None:
            return _MISSING
        result = method(*args, **kwargs)
        if inspect.isawaitable(result):
            return await result
        return result

    def _session_record_worker_id(self, session_id: str) -> str:
        return f"{_SESSION_RECORD_PREFIX}{session_id}"

    def _session_from_record(self, record: WorkerRecord | None) -> WorkerSession | None:
        if record is None:
            return None
        if record.session is not None:
            return record.session
        kind = record.metadata.get("record_kind")
        if kind != _SESSION_RECORD_KIND:
            return None
        payload = record.metadata.get("worker_session")
        if not isinstance(payload, Mapping):
            return None
        try:
            return WorkerSession.from_dict(payload)
        except Exception:
            return None

    async def _load_persisted_session(self, session_id: str) -> WorkerSession | None:
        persisted = await self._call_store_method("get_worker_session", session_id)
        if isinstance(persisted, WorkerSession):
            return persisted
        if isinstance(persisted, Mapping):
            try:
                return WorkerSession.from_dict(persisted)
            except Exception:
                return None
        if persisted is not _MISSING:
            return None
        record = await self.store.get_worker_record(self._session_record_worker_id(session_id))
        return self._session_from_record(record)

    async def _persist_session(self, session: WorkerSession) -> None:
        persisted = await self._call_store_method("save_worker_session", session)
        if persisted is not _MISSING:
            return
        await self.store.save_worker_record(
            WorkerRecord(
                worker_id=self._session_record_worker_id(session.session_id),
                assignment_id=session.last_assignment_id or "",
                backend=session.backend,
                role=session.role,
                status=WorkerStatus.IDLE if session.status == WorkerSessionStatus.IDLE else WorkerStatus.COMPLETED,
                metadata={
                    "record_kind": _SESSION_RECORD_KIND,
                    "worker_session": session.to_dict(),
                },
                session=session,
            )
        )

    def _handle_from_session(self, session: WorkerSession) -> WorkerHandle:
        return self.transport_adapter.handle_from_worker_session(
            session,
            backend=session.backend,
        )

    async def _hydrate_idle_session(
        self,
        *,
        session_id: str,
        assignment: WorkerAssignment,
    ) -> _ActiveWorkerSession | None:
        persisted = await self._load_persisted_session(session_id)
        if persisted is None or persisted.status != WorkerSessionStatus.IDLE:
            return None
        handle = self._handle_from_session(persisted)
        state = _ActiveWorkerSession(
            session_id=persisted.session_id,
            worker_id=persisted.worker_id,
            backend=persisted.backend,
            role=persisted.role,
            handle=handle,
            status=persisted.status,
            assignment_id=persisted.assignment_id,
            lifecycle_status=persisted.lifecycle_status,
            started_at=persisted.started_at,
            last_active_at=persisted.last_active_at,
            idle_since=persisted.idle_since,
            protocol_cursor=dict(persisted.protocol_cursor),
            mailbox_cursor=dict(persisted.mailbox_cursor),
            supervisor_id=persisted.supervisor_id,
            supervisor_lease_id=persisted.supervisor_lease_id,
            supervisor_lease_expires_at=persisted.supervisor_lease_expires_at,
            reattach_count=persisted.reattach_count,
            last_assignment_id=persisted.last_assignment_id,
            last_response_id=persisted.last_response_id,
            reactivation_count=persisted.reactivation_count,
            persist_final_session=True,
            metadata=dict(persisted.metadata),
        )
        if (
            state.worker_id != assignment.worker_id
            or state.backend != assignment.backend
            or state.role != assignment.role
            or not self._supports_reactivation(state.handle)
        ):
            return None
        return state

    def _coerce_execution_contract(
        self,
        payload: object,
    ) -> WorkerExecutionContract | None:
        if payload is None:
            return None
        if isinstance(payload, WorkerExecutionContract):
            return payload
        if not isinstance(payload, Mapping):
            return None
        required_artifact_kinds = payload.get("required_artifact_kinds", ())
        required_verification_commands = payload.get("required_verification_commands", ())
        progress_policy = payload.get("progress_policy", {})
        metadata = payload.get("metadata", {})
        if not isinstance(required_verification_commands, (list, tuple)):
            required_verification_commands = ()
        if not isinstance(progress_policy, Mapping):
            progress_policy = {}
        if not isinstance(metadata, Mapping):
            metadata = {}
        return WorkerExecutionContract(
            contract_id=str(payload.get("contract_id", "contract")),
            mode=str(payload.get("mode", "unspecified")),
            allow_subdelegation=bool(payload.get("allow_subdelegation", False)),
            require_final_report=bool(payload.get("require_final_report", True)),
            require_verification_results=bool(payload.get("require_verification_results", False)),
            required_verification_commands=tuple(
                str(item) for item in required_verification_commands if isinstance(item, str)
            ),
            completion_requires_verification_success=bool(
                payload.get("completion_requires_verification_success", False)
            ),
            required_artifact_kinds=tuple(str(item) for item in required_artifact_kinds if isinstance(item, str)),
            progress_policy={str(key): value for key, value in progress_policy.items()},
            metadata={str(key): value for key, value in metadata.items()},
        )

    def _resolved_execution_contract(
        self,
        *,
        assignment: WorkerAssignment | None,
        policy: WorkerExecutionPolicy | None,
    ) -> WorkerExecutionContract | None:
        sources: list[object] = []
        if assignment is not None:
            sources.extend((assignment.execution_contract, assignment.metadata.get("execution_contract")))
        if policy is not None:
            sources.append(policy.execution_contract)
        for payload in sources:
            contract = self._coerce_execution_contract(payload)
            if contract is not None:
                return contract
        return None

    def _coerce_lease_policy(
        self,
        payload: object,
    ) -> WorkerLeasePolicy | None:
        if payload is None:
            return None
        if isinstance(payload, WorkerLeasePolicy):
            return payload
        if not isinstance(payload, Mapping):
            return None
        renew_on_event_kinds = payload.get(
            "renew_on_event_kinds",
            ("accepted", "checkpoint", "phase_changed", "verifying"),
        )
        max_silence_seconds = payload.get("max_silence_seconds")
        if max_silence_seconds is not None:
            max_silence_seconds = float(max_silence_seconds)
        return WorkerLeasePolicy(
            accept_deadline_seconds=float(payload.get("accept_deadline_seconds", 1.0)),
            renewal_timeout_seconds=float(payload.get("renewal_timeout_seconds", 60.0)),
            hard_deadline_seconds=float(payload.get("hard_deadline_seconds", 600.0)),
            renew_on_event_kinds=tuple(
                str(item) for item in renew_on_event_kinds if isinstance(item, str) and item
            ),
            max_silence_seconds=max_silence_seconds,
        )

    def _resolved_lease_policy(
        self,
        *,
        assignment: WorkerAssignment | None,
        policy: WorkerExecutionPolicy | None,
    ) -> WorkerLeasePolicy | None:
        sources: list[object] = []
        if assignment is not None:
            sources.extend((assignment.lease_policy, assignment.metadata.get("lease_policy")))
        if policy is not None:
            sources.append(policy.lease_policy)
        for payload in sources:
            lease_policy = self._coerce_lease_policy(payload)
            if lease_policy is not None:
                return lease_policy
        return None

    def _protocol_events_from_result(self, result: WorkerResult) -> tuple[WorkerLifecycleEvent, ...]:
        payload = result.raw_payload.get("protocol_events")
        if payload is None:
            return ()
        if not isinstance(payload, (list, tuple)):
            raise ValueError("protocol_events must be a list or tuple")
        events: list[WorkerLifecycleEvent] = []
        for index, item in enumerate(payload):
            if isinstance(item, WorkerLifecycleEvent):
                events.append(item)
                continue
            if not isinstance(item, Mapping):
                raise ValueError("protocol event entry must be a mapping")
            status_raw = item.get("status", WorkerLifecycleStatus.RUNNING.value)
            status = WorkerLifecycleStatus(str(status_raw))
            phase = str(item.get("phase", status.value))
            timestamp = item.get("timestamp")
            if timestamp is not None:
                timestamp = str(timestamp)
            metadata = item.get("metadata", {})
            if not isinstance(metadata, Mapping):
                metadata = {}
            events.append(
                WorkerLifecycleEvent(
                    event_id=str(item.get("event_id", f"event-{index + 1}")),
                    assignment_id=str(item.get("assignment_id", result.assignment_id)),
                    worker_id=str(item.get("worker_id", result.worker_id)),
                    status=status,
                    phase=phase,
                    timestamp=timestamp,
                    summary=str(item.get("summary", "")),
                    metadata={str(key): value for key, value in metadata.items()},
                )
            )
        return tuple(events)

    def _final_report_from_result(self, result: WorkerResult) -> WorkerFinalReport | None:
        payload = result.raw_payload.get("final_report")
        if payload is None:
            return None
        if isinstance(payload, WorkerFinalReport):
            return payload
        if not isinstance(payload, Mapping):
            raise ValueError("final_report must be a mapping")
        verification_results = payload.get("verification_results", ())
        if not isinstance(verification_results, (list, tuple)):
            verification_results = ()
        pending_verification_commands = payload.get("pending_verification_commands", ())
        if not isinstance(pending_verification_commands, (list, tuple)):
            pending_verification_commands = ()
        artifact_refs = payload.get("artifact_refs", ())
        if not isinstance(artifact_refs, (list, tuple)):
            artifact_refs = ()
        missing_dependencies = payload.get("missing_dependencies", ())
        if not isinstance(missing_dependencies, (list, tuple)):
            missing_dependencies = ()
        metadata = payload.get("metadata", {})
        if not isinstance(metadata, Mapping):
            metadata = {}
        authority_request = ScopeExtensionRequest.from_payload(payload.get("authority_request"))
        terminal_status = WorkerFinalStatus(str(payload.get("terminal_status")))
        return WorkerFinalReport(
            assignment_id=str(payload.get("assignment_id", result.assignment_id)),
            worker_id=str(payload.get("worker_id", result.worker_id)),
            terminal_status=terminal_status,
            summary=str(payload.get("summary", "")),
            artifact_refs=tuple(str(item) for item in artifact_refs if isinstance(item, str)),
            verification_results=tuple(
                dict(item) for item in verification_results if isinstance(item, Mapping)
            ),
            pending_verification_commands=tuple(
                str(item) for item in pending_verification_commands if isinstance(item, str)
            ),
            blocker=str(payload.get("blocker", "")),
            missing_dependencies=tuple(
                str(item) for item in missing_dependencies if isinstance(item, str)
            ),
            retry_hint=str(payload.get("retry_hint", "")),
            authority_request=authority_request,
            metadata={str(key): value for key, value in metadata.items()},
        )

    def _serialize_final_report(self, report: WorkerFinalReport) -> dict[str, object]:
        return {
            "assignment_id": report.assignment_id,
            "worker_id": report.worker_id,
            "terminal_status": report.terminal_status.value,
            "summary": report.summary,
            "artifact_refs": list(report.artifact_refs),
            "verification_results": [dict(item) for item in report.verification_results],
            "pending_verification_commands": list(report.pending_verification_commands),
            "blocker": report.blocker,
            "missing_dependencies": list(report.missing_dependencies),
            "retry_hint": report.retry_hint,
            "authority_request": (
                report.authority_request.to_dict()
                if report.authority_request is not None
                else None
            ),
            "metadata": dict(report.metadata),
        }

    def _authoritative_verification_results(
        self,
        final_report: WorkerFinalReport | None,
    ) -> tuple[VerificationCommandResult, ...]:
        if final_report is None:
            return ()
        results: list[VerificationCommandResult] = []
        for item in final_report.verification_results:
            parsed = VerificationCommandResult.from_payload(item)
            if parsed is not None:
                results.append(parsed)
        return tuple(results)

    def _validate_required_verification_commands(
        self,
        *,
        contract: WorkerExecutionContract,
        final_report: WorkerFinalReport | None,
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        if final_report is None or not contract.required_verification_commands:
            return (), ()
        authoritative_results = list(self._authoritative_verification_results(final_report))
        missing: list[str] = []
        failed: list[str] = []
        for command in contract.required_verification_commands:
            matched_result = pop_preferred_equivalent_verification_result(
                command=command,
                results=authoritative_results,
            )
            if matched_result is None:
                missing.append(command)
                continue
            if matched_result.returncode != 0:
                failed.append(command)
        return tuple(missing), tuple(failed)

    def _protocol_expected(
        self,
        *,
        contract: WorkerExecutionContract | None,
        lease_policy: WorkerLeasePolicy | None,
        events: tuple[WorkerLifecycleEvent, ...],
        final_report: WorkerFinalReport | None,
    ) -> bool:
        return (
            contract is not None
            or lease_policy is not None
            or bool(events)
            or final_report is not None
        )

    def _mark_protocol_failure(
        self,
        record: WorkerRecord,
        *,
        metadata: dict[str, object],
        reason: str,
        error_text: str,
    ) -> WorkerRecord:
        record.status = WorkerStatus.FAILED
        record.error_text = error_text
        metadata["protocol_failure_reason"] = reason
        metadata["lifecycle_status"] = WorkerLifecycleStatus.FAILED.value
        record.metadata = metadata
        return self._apply_failure_attribution(record=record, handle=record.handle)

    def _build_protocol_lease_snapshot(
        self,
        *,
        assignment_id: str,
        worker_id: str,
        events: tuple[WorkerLifecycleEvent, ...],
        accepted: bool,
        completed: bool,
    ) -> dict[str, object]:
        issued_at = _now()
        accepted_at: str | None = None
        renewed_at: str | None = None
        for event in events:
            if event.timestamp:
                issued_at = event.timestamp
                break
        for event in events:
            if event.status == WorkerLifecycleStatus.ACCEPTED and event.timestamp:
                accepted_at = event.timestamp
                break
        for event in reversed(events):
            if event.timestamp:
                renewed_at = event.timestamp
                break
        if completed and accepted:
            status = "closed"
        elif accepted:
            status = "active"
        else:
            status = "pending_accept"
        return {
            "lease_id": f"{assignment_id}:{worker_id}:lease",
            "assignment_id": assignment_id,
            "worker_id": worker_id,
            "issued_at": issued_at,
            "accepted_at": accepted_at,
            "renewed_at": renewed_at,
            "expires_at": renewed_at or issued_at,
            "hard_deadline_at": renewed_at or issued_at,
            "status": status,
        }

    def _lifecycle_status_from_protocol(
        self,
        *,
        record: WorkerRecord,
        events: tuple[WorkerLifecycleEvent, ...],
        final_report: WorkerFinalReport | None,
    ) -> WorkerLifecycleStatus:
        if final_report is not None:
            if final_report.terminal_status == WorkerFinalStatus.COMPLETED:
                return WorkerLifecycleStatus.COMPLETED
            if final_report.terminal_status == WorkerFinalStatus.BLOCKED:
                return WorkerLifecycleStatus.BLOCKED
            if final_report.terminal_status == WorkerFinalStatus.FAILED:
                return WorkerLifecycleStatus.FAILED
            return WorkerLifecycleStatus.ABANDONED
        if events:
            return events[-1].status
        if record.status == WorkerStatus.COMPLETED:
            return WorkerLifecycleStatus.COMPLETED
        if record.status == WorkerStatus.FAILED:
            return WorkerLifecycleStatus.FAILED
        if record.status == WorkerStatus.RUNNING:
            return WorkerLifecycleStatus.RUNNING
        if record.status == WorkerStatus.LAUNCHED:
            return WorkerLifecycleStatus.ASSIGNED
        return WorkerLifecycleStatus.ABANDONED

    def _last_message_exists_at_failure(
        self,
        *,
        metadata: Mapping[str, object],
        handle: WorkerHandle | None,
    ) -> bool | None:
        raw_last_message_exists = metadata.get("last_message_exists_at_failure")
        if isinstance(raw_last_message_exists, bool):
            return raw_last_message_exists
        fallback_last_message_exists = metadata.get("last_message_exists")
        if isinstance(fallback_last_message_exists, bool):
            return fallback_last_message_exists
        if handle is None:
            return None
        last_message_path = handle.metadata.get("last_message_file")
        if not isinstance(last_message_path, str) or not last_message_path:
            return None
        return Path(last_message_path).exists()

    def _last_protocol_progress_at_from_events(
        self,
        events: tuple[WorkerLifecycleEvent, ...],
    ) -> str | None:
        for event in reversed(events):
            timestamp = event.timestamp
            if isinstance(timestamp, str) and timestamp:
                return timestamp
        return None

    def _apply_failure_attribution(
        self,
        *,
        record: WorkerRecord,
        handle: WorkerHandle | None = None,
    ) -> WorkerRecord:
        metadata = dict(record.metadata)
        if record.status != WorkerStatus.FAILED:
            if "backend_cancel_invoked" in metadata:
                metadata["backend_cancel_invoked"] = bool(metadata.get("backend_cancel_invoked"))
            record.metadata = metadata
            return record

        timeout_failure = bool(metadata.get("supervisor_timeout_path")) or (
            isinstance(metadata.get("timeout_kind"), str) and bool(str(metadata.get("timeout_kind")))
        )
        protocol_failure_reason = metadata.get("protocol_failure_reason")
        protocol_failure = isinstance(protocol_failure_reason, str) and bool(protocol_failure_reason)
        exit_code = metadata.get("exit_code")
        process_termination = isinstance(exit_code, int) and exit_code < 0

        if process_termination:
            signal_number = -exit_code
            metadata.setdefault("termination_signal", signal_number)
            signal_name = _signal_name(signal_number)
            if signal_name is not None:
                metadata.setdefault("termination_signal_name", signal_name)

        last_message_exists = self._last_message_exists_at_failure(metadata=metadata, handle=handle or record.handle)
        if last_message_exists is not None:
            metadata["last_message_exists_at_failure"] = last_message_exists

        if "last_protocol_progress_at" not in metadata:
            events, _ = self._protocol_events_and_report(
                assignment_id=record.assignment_id,
                worker_id=record.worker_id,
                payload=metadata,
            )
            last_protocol_progress_at = self._last_protocol_progress_at_from_events(events)
            if last_protocol_progress_at is not None:
                metadata["last_protocol_progress_at"] = last_protocol_progress_at

        existing_tags = metadata.get("failure_tags")
        tags: set[str] = set()
        if isinstance(existing_tags, (list, tuple)):
            tags.update(str(item) for item in existing_tags if isinstance(item, str) and item)
        if timeout_failure:
            tags.add("timeout_failure")
        if protocol_failure:
            tags.add("protocol_contract_failure")
        if process_termination:
            tags.add("process_termination")
            signal_name = metadata.get("termination_signal_name")
            if isinstance(signal_name, str) and signal_name:
                tags.add(f"signal_{signal_name.lower()}")

        metadata["supervisor_timeout_path"] = bool(timeout_failure)
        metadata["failure_is_timeout"] = bool(timeout_failure)
        metadata["failure_is_protocol_contract"] = bool(protocol_failure)
        metadata["failure_is_process_termination"] = bool(process_termination)
        metadata["failure_tags"] = sorted(tags)
        metadata["backend_cancel_invoked"] = bool(
            metadata.get("backend_cancel_invoked")
            or (handle is not None and bool(handle.metadata.get("backend_cancel_invoked")))
        )
        record.metadata = metadata
        return record

    def _apply_protocol_contract(
        self,
        *,
        record: WorkerRecord,
        assignment: WorkerAssignment,
        policy: WorkerExecutionPolicy | None = None,
    ) -> WorkerRecord:
        contract = self._resolved_execution_contract(assignment=assignment, policy=policy)
        lease_policy = self._resolved_lease_policy(assignment=assignment, policy=policy)
        try:
            result = WorkerResult(
                worker_id=record.worker_id,
                assignment_id=record.assignment_id,
                status=record.status,
                output_text=record.output_text,
                error_text=record.error_text,
                response_id=record.response_id,
                usage=dict(record.usage),
                raw_payload=dict(record.metadata),
            )
            events = self._protocol_events_from_result(result)
            final_report = self._final_report_from_result(result)
        except Exception as exc:
            metadata = dict(record.metadata)
            metadata["supervision_mode"] = "protocol_first"
            return self._mark_protocol_failure(
                record,
                metadata=metadata,
                reason="invalid_protocol_payload",
                error_text=f"Invalid protocol payload: {exc}",
            )

        if not self._protocol_expected(
            contract=contract,
            lease_policy=lease_policy,
            events=events,
            final_report=final_report,
        ):
            return record
        if (
            record.backend == "in_process"
            and not events
            and final_report is None
            and "protocol_failure_reason" not in record.metadata
        ):
            return record

        metadata = dict(record.metadata)
        metadata["supervision_mode"] = "protocol_first"
        metadata["protocol_event_count"] = len(events)
        existing_protocol_failure_reason = metadata.get("protocol_failure_reason")
        metadata["protocol_events"] = [
            {
                "event_id": event.event_id,
                "assignment_id": event.assignment_id,
                "worker_id": event.worker_id,
                "status": event.status.value,
                "phase": event.phase,
                "timestamp": event.timestamp,
                "summary": event.summary,
                "metadata": dict(event.metadata),
            }
            for event in events
        ]
        metadata["final_report"] = (
            self._serialize_final_report(final_report) if final_report is not None else None
        )
        if lease_policy is not None:
            metadata["lease_policy"] = {
                "accept_deadline_seconds": lease_policy.accept_deadline_seconds,
                "renewal_timeout_seconds": lease_policy.renewal_timeout_seconds,
                "hard_deadline_seconds": lease_policy.hard_deadline_seconds,
                "renew_on_event_kinds": list(lease_policy.renew_on_event_kinds),
                "max_silence_seconds": lease_policy.max_silence_seconds,
            }
        if contract is not None:
            metadata["execution_contract"] = {
                "contract_id": contract.contract_id,
                "mode": contract.mode,
                "allow_subdelegation": contract.allow_subdelegation,
                "require_final_report": contract.require_final_report,
                "require_verification_results": contract.require_verification_results,
                "required_verification_commands": list(contract.required_verification_commands),
                "completion_requires_verification_success": (
                    contract.completion_requires_verification_success
                ),
                "required_artifact_kinds": list(contract.required_artifact_kinds),
            }

        accepted = any(event.status == WorkerLifecycleStatus.ACCEPTED for event in events)
        completed = record.status == WorkerStatus.COMPLETED
        last_protocol_progress_at = self._last_protocol_progress_at_from_events(events)
        if last_protocol_progress_at is not None:
            metadata["last_protocol_progress_at"] = last_protocol_progress_at
        metadata["lease"] = self._build_protocol_lease_snapshot(
            assignment_id=record.assignment_id,
            worker_id=record.worker_id,
            events=events,
            accepted=accepted,
            completed=completed,
        )
        if isinstance(existing_protocol_failure_reason, str) and existing_protocol_failure_reason:
            metadata["lifecycle_status"] = WorkerLifecycleStatus.FAILED.value
            record.metadata = metadata
            return self._apply_failure_attribution(record=record, handle=record.handle)

        if not accepted:
            return self._mark_protocol_failure(
                record,
                metadata=metadata,
                reason="missing_accept",
                error_text="Protocol contract requires an accepted lifecycle event.",
            )

        if contract is not None and contract.require_final_report and final_report is None:
            return self._mark_protocol_failure(
                record,
                metadata=metadata,
                reason="missing_final_report",
                error_text="Protocol contract requires a final_report.",
            )

        if (
            contract is not None
            and contract.require_verification_results
            and not self._authoritative_verification_results(final_report)
        ):
            return self._mark_protocol_failure(
                record,
                metadata=metadata,
                reason="missing_verification_results",
                error_text="Protocol contract requires authoritative verification_results.",
            )

        if final_report is not None:
            if final_report.assignment_id != record.assignment_id:
                return self._mark_protocol_failure(
                    record,
                    metadata=metadata,
                    reason="final_report_assignment_mismatch",
                    error_text="Protocol final_report assignment_id mismatch.",
                )
            if (
                record.status == WorkerStatus.COMPLETED
                and final_report.terminal_status != WorkerFinalStatus.COMPLETED
            ):
                return self._mark_protocol_failure(
                    record,
                    metadata=metadata,
                    reason="final_status_mismatch",
                    error_text="Worker completed but protocol final_report terminal_status is not completed.",
                )

        if (
            contract is not None
            and contract.completion_requires_verification_success
            and final_report is not None
            and final_report.terminal_status == WorkerFinalStatus.COMPLETED
        ):
            missing_commands, failed_commands = self._validate_required_verification_commands(
                contract=contract,
                final_report=final_report,
            )
            if missing_commands:
                metadata["missing_required_verification_commands"] = list(missing_commands)
                return self._mark_protocol_failure(
                    record,
                    metadata=metadata,
                    reason="missing_required_verification_commands",
                    error_text=(
                        "Protocol final_report did not cover required verification commands: "
                        + ", ".join(missing_commands)
                    ),
                )
            if failed_commands:
                metadata["failed_required_verification_commands"] = list(failed_commands)
                return self._mark_protocol_failure(
                    record,
                    metadata=metadata,
                    reason="required_verification_commands_failed",
                    error_text=(
                        "Protocol final_report reported failing required verification commands: "
                        + ", ".join(failed_commands)
                    ),
                )

        lifecycle_status = self._lifecycle_status_from_protocol(
            record=record,
            events=events,
            final_report=final_report,
        )
        metadata["lifecycle_status"] = lifecycle_status.value
        record.metadata = metadata
        return record

    async def run_assignment_with_policy(
        self,
        assignment: WorkerAssignment,
        *,
        launch: Callable[[WorkerAssignment], Awaitable[WorkerHandle]],
        resume: Callable[[WorkerHandle, WorkerAssignment | None], Awaitable[WorkerHandle]],
        cancel: Callable[[WorkerHandle], Awaitable[None]] | None = None,
        policy: WorkerExecutionPolicy | None = None,
    ) -> WorkerRecord:
        active_policy = policy or self.default_execution_policy
        attempts: list[dict[str, object]] = []
        route_history: list[dict[str, object]] = []
        active_record: WorkerRecord | None = None
        routes = self._provider_routes(assignment, active_policy)
        route_index = 0
        active_handle: WorkerHandle | None = None
        active_session: _ActiveWorkerSession | None = None
        active_assignment = assignment
        last_failure_kind: WorkerFailureKind | None = None
        backend_cancel_invoked = False

        while route_index < len(routes):
            route = routes[route_index]
            active_assignment = self._assignment_for_route(assignment, route)
            active_session = await self._find_idle_session(active_assignment, active_policy)
            active_handle = active_session.handle if active_session is not None else None
            operation = "reactivate" if active_session is not None else "launch"
            route_attempts = 0

            while route_attempts < active_policy.max_attempts:
                if operation == "resume":
                    if active_handle is None:
                        raise RuntimeError("Cannot resume without an existing worker handle.")
                    try:
                        active_handle = await resume(active_handle, active_assignment)
                    except BaseException as exc:
                        if active_policy.allow_relaunch:
                            await self._sleep_with_backoff(
                                policy=active_policy,
                                route=route,
                                attempt_number=None,
                                failure_kind=None,
                            )
                            operation = "launch"
                            continue
                        active_record = await self.fail(active_handle, exc)
                        escalation = self._build_escalation(
                            assignment=active_assignment,
                            attempt_count=len(attempts),
                            reason="resume_unavailable",
                            record=active_record,
                        )
                        active_record.metadata["backend_cancel_invoked"] = backend_cancel_invoked
                        return await self._finalize_record(
                            active_record,
                            assignment=active_assignment,
                            policy=active_policy,
                            attempts=attempts,
                            escalation=escalation,
                            handle=active_handle,
                            session_state=active_session,
                            provider_routing=self._provider_routing_metadata(
                                routes=routes,
                                route_history=route_history,
                                selected_route=route.route_id,
                                exhausted=False,
                            ),
                        )
                elif operation == "reactivate":
                    if active_session is None:
                        raise RuntimeError("Cannot reactivate without an idle worker session.")
                    active_handle = active_session.handle
                    try:
                        active_handle = await resume(active_handle, active_assignment)
                    except BaseException as exc:
                        self._worker_sessions.pop(active_session.session_id, None)
                        failed_session = active_session
                        active_session = None
                        if active_policy.allow_relaunch:
                            await self._sleep_with_backoff(
                                policy=active_policy,
                                route=route,
                                attempt_number=None,
                                failure_kind=None,
                            )
                            operation = "launch"
                            continue
                        active_record = await self.fail(active_handle, exc)
                        escalation = self._build_escalation(
                            assignment=active_assignment,
                            attempt_count=len(attempts),
                            reason="reactivate_unavailable",
                            record=active_record,
                        )
                        active_record.metadata["backend_cancel_invoked"] = backend_cancel_invoked
                        return await self._finalize_record(
                            active_record,
                            assignment=active_assignment,
                            policy=active_policy,
                            attempts=attempts,
                            escalation=escalation,
                            handle=active_handle,
                            session_state=failed_session,
                            provider_routing=self._provider_routing_metadata(
                                routes=routes,
                                route_history=route_history,
                                selected_route=route.route_id,
                                exhausted=False,
                            ),
                        )
                    active_session.handle = active_handle
                    active_session.status = WorkerSessionStatus.ACTIVE
                    active_session.idle_since = None
                    active_session.last_active_at = _now()
                    active_session.last_assignment_id = active_assignment.assignment_id
                    active_session.reactivation_count += 1
                    await self.start(active_handle, active_assignment)
                else:
                    active_handle = await launch(active_assignment)
                    await self.start(active_handle, active_assignment)

                active_session = self._prepare_session(
                    assignment=active_assignment,
                    handle=active_handle,
                    policy=active_policy,
                    existing=active_session,
                )
                if active_session is not None:
                    await self._persist_active_session(
                        state=active_session,
                        assignment=active_assignment,
                        handle=active_handle,
                        policy=active_policy,
                        force_new_lease_id=operation != "resume",
                    )

                route_attempts += 1
                attempt_number = len(attempts) + 1
                active_record = await self.wait(
                    active_handle,
                    timeout_seconds=active_policy.attempt_timeout_seconds,
                    policy=active_policy,
                    assignment=active_assignment,
                    session_state=active_session,
                )
                active_record = self._apply_protocol_contract(
                    record=active_record,
                    assignment=active_assignment,
                    policy=active_policy,
                )
                failure_kind = self._classify_failure_kind(record=active_record, policy=active_policy)
                last_failure_kind = failure_kind
                has_fallback_remaining = route_index < (len(routes) - 1)
                decision = self._decide_next_action(
                    record=active_record,
                    handle=active_handle,
                    policy=active_policy,
                    attempt_number=route_attempts,
                    failure_kind=failure_kind,
                    has_fallback_remaining=has_fallback_remaining,
                )
                attempts.append(
                    self._build_attempt_record(
                        attempt_number=attempt_number,
                        operation=operation,
                        decision=decision,
                        record=active_record,
                        handle=active_handle,
                        session_state=active_session,
                        provider_route=route,
                        failure_kind=failure_kind,
                    )
                )

                if decision == WorkerAttemptDecision.COMPLETE:
                    route_history.append(
                        self._route_history_entry(
                            route=route,
                            attempt_count=route_attempts,
                            outcome="completed",
                            failure_kind=None,
                            record=active_record,
                        )
                    )
                    active_record.metadata["backend_cancel_invoked"] = backend_cancel_invoked
                    return await self._finalize_record(
                        active_record,
                        assignment=active_assignment,
                        policy=active_policy,
                        attempts=attempts,
                        handle=active_handle,
                        session_state=active_session,
                        provider_routing=self._provider_routing_metadata(
                            routes=routes,
                            route_history=route_history,
                            selected_route=route.route_id,
                            exhausted=False,
                        ),
                    )
                if decision == WorkerAttemptDecision.RESUME:
                    operation = "resume"
                    continue
                if decision == WorkerAttemptDecision.RETRY:
                    backend_cancel_invoked = (
                        await self._cancel_if_needed(cancel, active_handle, active_record)
                        or backend_cancel_invoked
                    )
                    if active_session is not None:
                        self._worker_sessions.pop(active_session.session_id, None)
                        active_session = None
                    backoff_seconds = await self._sleep_with_backoff(
                        policy=active_policy,
                        route=route,
                        attempt_number=route_attempts,
                        failure_kind=failure_kind,
                    )
                    attempts[-1]["backoff_seconds"] = backoff_seconds
                    operation = "launch"
                    continue
                if decision == WorkerAttemptDecision.FALLBACK:
                    backend_cancel_invoked = (
                        await self._cancel_if_needed(cancel, active_handle, active_record)
                        or backend_cancel_invoked
                    )
                    if active_session is not None:
                        self._worker_sessions.pop(active_session.session_id, None)
                        active_session = None
                    route_history.append(
                        self._route_history_entry(
                            route=route,
                            attempt_count=route_attempts,
                            outcome="provider_exhausted",
                            failure_kind=failure_kind,
                            record=active_record,
                        )
                    )
                    route_index += 1
                    next_route = routes[route_index]
                    backoff_seconds = await self._sleep_with_backoff(
                        policy=active_policy,
                        route=next_route,
                        attempt_number=route_attempts,
                        failure_kind=failure_kind,
                    )
                    attempts[-1]["backoff_seconds"] = backoff_seconds
                    break

                backend_cancel_invoked = (
                    await self._cancel_if_needed(cancel, active_handle, active_record)
                    or backend_cancel_invoked
                )
                if active_session is not None:
                    self._worker_sessions.pop(active_session.session_id, None)
                outcome = (
                    "provider_exhausted"
                    if failure_kind == WorkerFailureKind.PROVIDER_UNAVAILABLE
                    else "attempts_exhausted"
                )
                route_history.append(
                    self._route_history_entry(
                        route=route,
                        attempt_count=route_attempts,
                        outcome=outcome,
                        failure_kind=failure_kind,
                        record=active_record,
                    )
                )
                escalation = self._build_escalation(
                    assignment=active_assignment,
                    attempt_count=len(attempts),
                    reason=outcome,
                    record=active_record,
                    failure_kind=failure_kind,
                    provider_route=route,
                    route_history=route_history,
                )
                active_record.metadata["backend_cancel_invoked"] = backend_cancel_invoked
                return await self._finalize_record(
                    active_record,
                    assignment=active_assignment,
                    policy=active_policy,
                    attempts=attempts,
                    escalation=escalation,
                    handle=active_handle,
                    session_state=active_session,
                    provider_routing=self._provider_routing_metadata(
                        routes=routes,
                        route_history=route_history,
                        selected_route=route.route_id,
                        exhausted=failure_kind == WorkerFailureKind.PROVIDER_UNAVAILABLE,
                    ),
                )
            else:
                continue

            if route_index >= len(routes):
                break

        if active_record is None:
            fallback_handle = WorkerHandle(
                worker_id=assignment.worker_id,
                role=assignment.role,
                backend=assignment.backend,
                run_id=assignment.assignment_id,
            )
            active_record = await self.fail(fallback_handle, "Worker execution produced no attempts")
        if route_history:
            selected_route = route_history[-1].get("route_id")
            exhausted = last_failure_kind == WorkerFailureKind.PROVIDER_UNAVAILABLE
        else:
            selected_route = routes[0].route_id if routes else assignment.backend
            exhausted = False
        escalation = self._build_escalation(
            assignment=active_assignment,
            attempt_count=len(attempts),
            reason="provider_exhausted" if exhausted else "attempts_exhausted",
            record=active_record,
            failure_kind=last_failure_kind,
            provider_route=routes[-1] if routes else None,
            route_history=route_history,
        )
        active_record.metadata["backend_cancel_invoked"] = backend_cancel_invoked
        return await self._finalize_record(
            active_record,
            assignment=active_assignment,
            policy=active_policy,
            attempts=attempts,
            escalation=escalation,
            handle=active_handle,
            session_state=active_session,
            provider_routing=self._provider_routing_metadata(
                routes=routes,
                route_history=route_history,
                selected_route=str(selected_route),
                exhausted=exhausted,
            ),
        )

    def _build_attempt_record(
        self,
        *,
        attempt_number: int,
        operation: str,
        decision: WorkerAttemptDecision,
        record: WorkerRecord,
        handle: WorkerHandle,
        session_state: _ActiveWorkerSession | None,
        provider_route: WorkerProviderRoute,
        failure_kind: WorkerFailureKind | None,
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "attempt": attempt_number,
            "operation": operation,
            "decision": decision.value,
            "status": record.status.value,
            "handle": self.transport_adapter.snapshot_handle(handle),
            "provider_route": {
                "route_id": provider_route.route_id,
                "backend": provider_route.backend,
            },
        }
        if session_state is not None:
            payload["session_id"] = session_state.session_id
            payload["session_status"] = session_state.status.value
        provider_name = self._provider_name(provider_route=provider_route, record=record)
        if provider_name is not None:
            payload["provider_route"]["provider_name"] = provider_name
        if record.output_text:
            payload["output_text"] = record.output_text
        if record.error_text:
            payload["error_text"] = record.error_text
        error_type = record.metadata.get("error_type")
        if isinstance(error_type, str) and error_type:
            payload["error_type"] = error_type
        if failure_kind is not None:
            payload["failure_kind"] = failure_kind.value
        return payload

    def _decide_next_action(
        self,
        *,
        record: WorkerRecord,
        handle: WorkerHandle,
        policy: WorkerExecutionPolicy,
        attempt_number: int,
        failure_kind: WorkerFailureKind,
        has_fallback_remaining: bool,
    ) -> WorkerAttemptDecision:
        if record.status == WorkerStatus.COMPLETED:
            return WorkerAttemptDecision.COMPLETE

        has_attempts_remaining = attempt_number < policy.max_attempts
        timeout_failure = failure_kind == WorkerFailureKind.TIMEOUT
        provider_unavailable_failure = failure_kind == WorkerFailureKind.PROVIDER_UNAVAILABLE
        supports_resume = self._supports_resume(handle)

        if has_attempts_remaining:
            if timeout_failure and policy.resume_on_timeout and supports_resume:
                return WorkerAttemptDecision.RESUME
            if not timeout_failure and policy.resume_on_failure and supports_resume:
                return WorkerAttemptDecision.RESUME
            if provider_unavailable_failure and policy.fallback_on_provider_unavailable:
                return WorkerAttemptDecision.RETRY
            if policy.allow_relaunch:
                return WorkerAttemptDecision.RETRY

        if (
            provider_unavailable_failure
            and policy.fallback_on_provider_unavailable
            and has_fallback_remaining
        ):
            return WorkerAttemptDecision.FALLBACK

        if policy.escalate_after_attempts:
            return WorkerAttemptDecision.ESCALATE
        return WorkerAttemptDecision.COMPLETE

    def _supports_resume(self, handle: WorkerHandle) -> bool:
        return bool(handle.metadata.get("resume_supported"))

    def _supports_reactivation(self, handle: WorkerHandle) -> bool:
        return bool(handle.metadata.get("reactivate_supported"))

    def _is_timeout_failure(self, record: WorkerRecord) -> bool:
        error_type = record.metadata.get("error_type")
        return isinstance(error_type, str) and error_type in {"TimeoutExpired", "TimeoutError"}

    def _classify_failure_kind(
        self,
        *,
        record: WorkerRecord,
        policy: WorkerExecutionPolicy,
    ) -> WorkerFailureKind | None:
        if record.status == WorkerStatus.COMPLETED:
            return None
        if self._is_timeout_failure(record):
            return WorkerFailureKind.TIMEOUT
        explicit_failure_kind = record.metadata.get("failure_kind")
        if explicit_failure_kind == WorkerFailureKind.PROVIDER_UNAVAILABLE.value:
            return WorkerFailureKind.PROVIDER_UNAVAILABLE
        error_type = record.metadata.get("error_type")
        if isinstance(error_type, str) and error_type in policy.provider_unavailable_error_types:
            return WorkerFailureKind.PROVIDER_UNAVAILABLE
        error_text = record.error_text.lower()
        for token in policy.provider_unavailable_substrings:
            if token and token.lower() in error_text:
                return WorkerFailureKind.PROVIDER_UNAVAILABLE
        return WorkerFailureKind.ORDINARY

    def _provider_routes(
        self,
        assignment: WorkerAssignment,
        policy: WorkerExecutionPolicy,
    ) -> tuple[WorkerProviderRoute, ...]:
        primary_route_id = assignment.metadata.get("provider_route_id")
        if not isinstance(primary_route_id, str) or not primary_route_id.strip():
            primary_route_id = assignment.backend
        primary_metadata: dict[str, object] = {}
        provider_name = assignment.metadata.get("provider_name")
        if isinstance(provider_name, str) and provider_name.strip():
            primary_metadata["provider_name"] = provider_name.strip()
        primary_route = WorkerProviderRoute(
            route_id=primary_route_id.strip(),
            backend=assignment.backend,
            metadata=primary_metadata,
        )
        return (primary_route, *policy.provider_fallbacks)

    def _assignment_for_route(
        self,
        assignment: WorkerAssignment,
        route: WorkerProviderRoute,
    ) -> WorkerAssignment:
        metadata = dict(assignment.metadata)
        metadata.update(route.metadata)
        metadata["provider_route_id"] = route.route_id
        environment = dict(assignment.environment)
        environment.update(route.environment)
        return replace(
            assignment,
            backend=route.backend,
            metadata=metadata,
            environment=environment,
        )

    async def _sleep_with_backoff(
        self,
        *,
        policy: WorkerExecutionPolicy,
        route: WorkerProviderRoute,
        attempt_number: int | None,
        failure_kind: WorkerFailureKind | None,
    ) -> float:
        backoff_seconds = route.backoff_seconds if route.backoff_seconds is not None else policy.backoff_seconds
        if (
            failure_kind == WorkerFailureKind.PROVIDER_UNAVAILABLE
            and attempt_number is not None
            and attempt_number >= 1
            and policy.provider_unavailable_backoff_initial_seconds > 0
        ):
            exponent = attempt_number - 1
            exponential_seconds = (
                policy.provider_unavailable_backoff_initial_seconds
                * (policy.provider_unavailable_backoff_multiplier ** exponent)
            )
            capped_seconds = policy.provider_unavailable_backoff_max_seconds
            if capped_seconds > 0:
                backoff_seconds = min(capped_seconds, exponential_seconds)
            else:
                backoff_seconds = exponential_seconds
        if backoff_seconds > 0:
            await asyncio.sleep(backoff_seconds)
        return backoff_seconds

    def _provider_name(
        self,
        *,
        provider_route: WorkerProviderRoute,
        record: WorkerRecord,
    ) -> str | None:
        provider_name = record.metadata.get("provider_name")
        if isinstance(provider_name, str) and provider_name.strip():
            return provider_name.strip()
        configured = provider_route.metadata.get("provider_name")
        if isinstance(configured, str) and configured.strip():
            return configured.strip()
        return None

    def _route_history_entry(
        self,
        *,
        route: WorkerProviderRoute,
        attempt_count: int,
        outcome: str,
        failure_kind: WorkerFailureKind | None,
        record: WorkerRecord,
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "route_id": route.route_id,
            "backend": route.backend,
            "attempt_count": attempt_count,
            "outcome": outcome,
        }
        provider_name = self._provider_name(provider_route=route, record=record)
        if provider_name is not None:
            payload["provider_name"] = provider_name
        if failure_kind is not None:
            payload["failure_kind"] = failure_kind.value
        if record.error_text:
            payload["last_error"] = record.error_text
        if route.backoff_seconds is not None:
            payload["backoff_seconds"] = route.backoff_seconds
        return payload

    def _provider_routing_metadata(
        self,
        *,
        routes: tuple[WorkerProviderRoute, ...],
        route_history: list[dict[str, object]],
        selected_route: str,
        exhausted: bool,
    ) -> dict[str, object]:
        return {
            "initial_route": routes[0].route_id,
            "selected_route": selected_route,
            "configured_routes": [route.route_id for route in routes],
            "configured_fallback_count": max(len(routes) - 1, 0),
            "attempted_route_count": len(route_history),
            "route_history": list(route_history),
            "exhausted": exhausted,
        }

    def _session_id_for_assignment(self, assignment: WorkerAssignment) -> str:
        session_id = assignment.metadata.get("worker_session_id")
        if isinstance(session_id, str) and session_id.strip():
            return session_id.strip()
        return f"{assignment.backend}:{assignment.role}:{assignment.worker_id}"

    def _supports_durable_active_session(self, handle: WorkerHandle) -> bool:
        return handle.backend != "in_process"

    def _resolved_supervisor_id(
        self,
        *,
        assignment: WorkerAssignment,
        state: _ActiveWorkerSession | None = None,
    ) -> str:
        supervisor_id = assignment.metadata.get("supervisor_id")
        if isinstance(supervisor_id, str) and supervisor_id.strip():
            return supervisor_id.strip()
        if state is not None and isinstance(state.supervisor_id, str) and state.supervisor_id.strip():
            return state.supervisor_id.strip()
        return self._default_supervisor_id

    def _session_lease_window_seconds(
        self,
        *,
        lease_policy: WorkerLeasePolicy | None,
        policy: WorkerExecutionPolicy | None,
    ) -> float:
        if lease_policy is not None:
            if lease_policy.max_silence_seconds is not None:
                return max(lease_policy.max_silence_seconds, 0.0)
            return max(lease_policy.renewal_timeout_seconds, 0.0)
        if policy is not None:
            if policy.idle_timeout_seconds is not None:
                return max(policy.idle_timeout_seconds, 0.0)
            if policy.attempt_timeout_seconds is not None:
                return max(policy.attempt_timeout_seconds, 0.0)
            if policy.hard_timeout_seconds is not None:
                return max(policy.hard_timeout_seconds, 0.0)
        return max(self.default_timeout_seconds, 0.0)

    def _lease_expiry_from_now(
        self,
        *,
        now: datetime,
        lease_window_seconds: float,
    ) -> str:
        return (now + timedelta(seconds=max(lease_window_seconds, 0.0))).isoformat()

    def _refresh_session_lease(
        self,
        *,
        state: _ActiveWorkerSession,
        assignment: WorkerAssignment,
        policy: WorkerExecutionPolicy | None,
        lease_policy: WorkerLeasePolicy | None,
        force_new_lease_id: bool = False,
    ) -> None:
        now = datetime.now(UTC)
        now_iso = now.isoformat()
        lease_window_seconds = self._session_lease_window_seconds(lease_policy=lease_policy, policy=policy)
        state.supervisor_id = self._resolved_supervisor_id(assignment=assignment, state=state)
        if force_new_lease_id or not state.supervisor_lease_id:
            configured_lease_id = assignment.metadata.get("supervisor_lease_id")
            if isinstance(configured_lease_id, str) and configured_lease_id.strip():
                state.supervisor_lease_id = configured_lease_id.strip()
            else:
                state.supervisor_lease_id = (
                    f"{assignment.assignment_id}:{state.session_id}:{int(now.timestamp() * 1_000_000)}"
                )
        state.supervisor_lease_expires_at = self._lease_expiry_from_now(
            now=now,
            lease_window_seconds=lease_window_seconds,
        )
        state.last_active_at = now_iso

    async def _persist_active_session(
        self,
        *,
        state: _ActiveWorkerSession,
        assignment: WorkerAssignment,
        handle: WorkerHandle,
        policy: WorkerExecutionPolicy,
        force_new_lease_id: bool = False,
    ) -> None:
        lease_policy = self._resolved_lease_policy(assignment=assignment, policy=policy)
        execution_contract = self._resolved_execution_contract(assignment=assignment, policy=policy)
        self._refresh_session_lease(
            state=state,
            assignment=assignment,
            policy=policy,
            lease_policy=lease_policy,
            force_new_lease_id=force_new_lease_id,
        )
        state.handle = handle
        state.status = WorkerSessionStatus.ACTIVE
        state.assignment_id = assignment.assignment_id
        state.last_assignment_id = assignment.assignment_id
        state.idle_since = None
        if not state.lifecycle_status:
            state.lifecycle_status = WorkerLifecycleStatus.RUNNING.value
        state.metadata.update(
            {
                "group_id": assignment.group_id,
                "task_id": assignment.task_id,
                "worker_session_id": state.session_id,
                "bound_worker_session_id": state.session_id,
                "current_worker_session_id": state.session_id,
                "last_worker_session_id": state.session_id,
            }
        )
        if assignment.objective_id is not None:
            state.metadata["objective_id"] = assignment.objective_id
        if assignment.team_id is not None:
            state.metadata["team_id"] = assignment.team_id
        if assignment.lane_id is not None:
            state.metadata["lane_id"] = assignment.lane_id
        if assignment.working_dir is not None:
            state.metadata["working_dir"] = assignment.working_dir
        if assignment.environment:
            state.metadata["environment"] = dict(assignment.environment)
        if execution_contract is not None:
            state.metadata["execution_contract"] = execution_contract.to_dict()
        if lease_policy is not None:
            state.metadata["lease_policy"] = lease_policy.to_dict()
        self._worker_sessions[state.session_id] = state
        active_worker_session = self._worker_session_from_state(
            state=state,
            assignment=assignment,
            handle=handle,
            status=WorkerSessionStatus.ACTIVE,
        )
        await self._persist_session(active_worker_session)
        await self._write_host_session(
            worker_session=active_worker_session,
            handle=handle,
            phase=ResidentCoordinatorPhase.RUNNING,
            reason="enter_native_wait",
        )

    def _assignment_from_session(self, session: WorkerSession) -> WorkerAssignment:
        metadata = dict(session.metadata)
        metadata.setdefault("worker_session_id", session.session_id)
        if session.supervisor_id is not None:
            metadata["supervisor_id"] = session.supervisor_id
        if session.supervisor_lease_id is not None:
            metadata["supervisor_lease_id"] = session.supervisor_lease_id
        environment_payload = metadata.get("environment", {})
        environment = (
            {str(key): str(value) for key, value in environment_payload.items()}
            if isinstance(environment_payload, Mapping)
            else {}
        )
        working_dir = metadata.get("working_dir")
        if not isinstance(working_dir, str) or not working_dir:
            working_dir = None
        execution_contract = self._coerce_execution_contract(metadata.get("execution_contract"))
        lease_policy = self._coerce_lease_policy(metadata.get("lease_policy"))
        objective_id = metadata.get("objective_id")
        team_id = metadata.get("team_id")
        lane_id = metadata.get("lane_id")
        return WorkerAssignment(
            assignment_id=session.assignment_id or session.last_assignment_id or session.session_id,
            worker_id=session.worker_id,
            group_id=str(metadata.get("group_id", "")),
            task_id=str(metadata.get("task_id", "")),
            role=session.role,
            backend=session.backend,
            instructions=str(metadata.get("instructions", "")),
            input_text=str(metadata.get("input_text", "")),
            objective_id=str(objective_id) if objective_id is not None else None,
            team_id=str(team_id) if team_id is not None else None,
            lane_id=str(lane_id) if lane_id is not None else None,
            working_dir=working_dir,
            environment=environment,
            metadata=metadata,
            execution_contract=execution_contract,
            lease_policy=lease_policy,
        )

    def _active_state_from_session(
        self,
        session: WorkerSession,
        *,
        handle: WorkerHandle,
    ) -> _ActiveWorkerSession:
        return _ActiveWorkerSession(
            session_id=session.session_id,
            worker_id=session.worker_id,
            backend=session.backend,
            role=session.role,
            handle=handle,
            status=session.status,
            assignment_id=session.assignment_id,
            lifecycle_status=session.lifecycle_status,
            started_at=session.started_at,
            last_active_at=session.last_active_at,
            idle_since=session.idle_since,
            protocol_cursor=dict(session.protocol_cursor),
            mailbox_cursor=dict(session.mailbox_cursor),
            supervisor_id=session.supervisor_id,
            supervisor_lease_id=session.supervisor_lease_id,
            supervisor_lease_expires_at=session.supervisor_lease_expires_at,
            reattach_count=session.reattach_count,
            last_assignment_id=session.last_assignment_id,
            last_response_id=session.last_response_id,
            reactivation_count=session.reactivation_count,
            persist_final_session=False,
            metadata=dict(session.metadata),
        )

    def _prepare_session(
        self,
        *,
        assignment: WorkerAssignment,
        handle: WorkerHandle,
        policy: WorkerExecutionPolicy,
        existing: _ActiveWorkerSession | None = None,
    ) -> _ActiveWorkerSession | None:
        supports_idle_reactivation = policy.keep_session_idle and self._supports_reactivation(handle)
        if not supports_idle_reactivation and not self._supports_durable_active_session(handle):
            return None
        session_id = self._session_id_for_assignment(assignment)
        state = existing or self._worker_sessions.get(session_id)
        if state is None:
            state = _ActiveWorkerSession(
                session_id=session_id,
                worker_id=assignment.worker_id,
                backend=handle.backend,
                role=handle.role,
                handle=handle,
                status=WorkerSessionStatus.ACTIVE,
                started_at=_now(),
            )
        state.handle = handle
        state.status = WorkerSessionStatus.ACTIVE
        state.assignment_id = assignment.assignment_id
        state.lifecycle_status = state.lifecycle_status or WorkerLifecycleStatus.RUNNING.value
        state.idle_since = None
        state.last_active_at = _now()
        state.last_assignment_id = assignment.assignment_id
        state.persist_final_session = supports_idle_reactivation
        return state

    async def _find_idle_session(
        self,
        assignment: WorkerAssignment,
        policy: WorkerExecutionPolicy,
    ) -> _ActiveWorkerSession | None:
        if not policy.reactivate_idle_session:
            return None
        session_id = self._session_id_for_assignment(assignment)
        state = self._worker_sessions.get(session_id)
        if state is None:
            state = await self._hydrate_idle_session(session_id=session_id, assignment=assignment)
            if state is not None:
                self._worker_sessions[session_id] = state
        if state is None or state.status != WorkerSessionStatus.IDLE:
            return None
        if (
            state.worker_id != assignment.worker_id
            or state.backend != assignment.backend
            or state.role != assignment.role
        ):
            return None
        if not self._supports_reactivation(state.handle):
            return None
        return state

    def _snapshot_session(
        self,
        state: _ActiveWorkerSession,
        *,
        status: WorkerSessionStatus,
        record: WorkerRecord,
        handle: WorkerHandle,
    ) -> WorkerSession:
        return WorkerSession(
            session_id=state.session_id,
            worker_id=state.worker_id,
            assignment_id=state.assignment_id or state.last_assignment_id or record.assignment_id,
            backend=state.backend,
            role=state.role,
            status=status,
            lifecycle_status=state.lifecycle_status,
            started_at=state.started_at,
            last_active_at=state.last_active_at,
            idle_since=state.idle_since,
            protocol_cursor=dict(state.protocol_cursor),
            mailbox_cursor=dict(state.mailbox_cursor),
            supervisor_id=state.supervisor_id,
            supervisor_lease_id=state.supervisor_lease_id,
            supervisor_lease_expires_at=state.supervisor_lease_expires_at,
            reattach_count=state.reattach_count,
            last_assignment_id=state.last_assignment_id or record.assignment_id,
            last_response_id=state.last_response_id or record.response_id,
            reactivation_count=state.reactivation_count,
            handle_snapshot=self.transport_adapter.snapshot_handle(handle),
            metadata=dict(state.metadata),
        )

    async def _cancel_if_needed(
        self,
        cancel: Callable[[WorkerHandle], Awaitable[None]] | None,
        handle: WorkerHandle,
        record: WorkerRecord,
    ) -> bool:
        if cancel is None:
            return False
        if not self._should_cancel(handle, record):
            return False
        handle.metadata["backend_cancel_invoked"] = True
        record.metadata["backend_cancel_invoked"] = True
        try:
            await cancel(handle)
        except BaseException:
            return True
        return True

    def _should_cancel(self, handle: WorkerHandle, record: WorkerRecord) -> bool:
        if self._is_timeout_failure(record):
            return True
        process = handle.metadata.get("_process")
        if process is not None and hasattr(process, "poll") and process.poll() is None:
            return True
        return handle.session_name is not None

    def _build_escalation(
        self,
        *,
        assignment: WorkerAssignment,
        attempt_count: int,
        reason: str,
        record: WorkerRecord,
        failure_kind: WorkerFailureKind | None = None,
        provider_route: WorkerProviderRoute | None = None,
        route_history: list[dict[str, object]] | None = None,
    ) -> WorkerEscalation:
        metadata: dict[str, object] = {"task_id": assignment.task_id}
        if assignment.objective_id is not None:
            metadata["objective_id"] = assignment.objective_id
        if assignment.team_id is not None:
            metadata["team_id"] = assignment.team_id
        if assignment.lane_id is not None:
            metadata["lane_id"] = assignment.lane_id
        error_type = record.metadata.get("error_type")
        if isinstance(error_type, str) and error_type:
            metadata["error_type"] = error_type
        if failure_kind is not None:
            metadata["failure_kind"] = failure_kind.value
        if provider_route is not None:
            metadata["provider_route_id"] = provider_route.route_id
            provider_name = self._provider_name(provider_route=provider_route, record=record)
            if provider_name is not None:
                metadata["provider_name"] = provider_name
        if route_history:
            metadata["route_history"] = list(route_history)
        return WorkerEscalation(
            assignment_id=assignment.assignment_id,
            worker_id=assignment.worker_id,
            attempt_count=attempt_count,
            reason=reason,
            backend=assignment.backend,
            last_error=record.error_text,
            metadata=metadata,
        )

    async def _finalize_record(
        self,
        record: WorkerRecord,
        *,
        assignment: WorkerAssignment,
        policy: WorkerExecutionPolicy,
        attempts: list[dict[str, object]],
        escalation: WorkerEscalation | None = None,
        handle: WorkerHandle | None = None,
        session_state: _ActiveWorkerSession | None = None,
        provider_routing: dict[str, object] | None = None,
    ) -> WorkerRecord:
        metadata = dict(record.metadata)
        metadata["task_id"] = assignment.task_id
        if assignment.objective_id is not None:
            metadata["objective_id"] = assignment.objective_id
        if assignment.team_id is not None:
            metadata["team_id"] = assignment.team_id
        if assignment.lane_id is not None:
            metadata["lane_id"] = assignment.lane_id
        metadata["execution_policy"] = _policy_metadata(policy)
        metadata["attempt_count"] = len(attempts)
        metadata["attempts"] = list(attempts)
        metadata["attempt_history"] = list(attempts)
        metadata["escalated"] = escalation is not None
        if attempts:
            failure_kind = attempts[-1].get("failure_kind")
            if isinstance(failure_kind, str) and failure_kind:
                metadata["failure_kind"] = failure_kind
        if provider_routing is not None:
            metadata["provider_routing"] = dict(provider_routing)
        metadata["backend_cancel_invoked"] = bool(
            metadata.get("backend_cancel_invoked")
            or (handle is not None and bool(handle.metadata.get("backend_cancel_invoked")))
        )
        session: WorkerSession | None = None
        active_handle = handle or record.handle
        if session_state is not None and active_handle is not None:
            session_state.handle = active_handle
            session_state.assignment_id = record.assignment_id
            session_state.last_assignment_id = record.assignment_id
            session_state.last_response_id = record.response_id
            session_state.last_active_at = record.ended_at or _now()
            lifecycle_status = metadata.get("lifecycle_status")
            if isinstance(lifecycle_status, str) and lifecycle_status:
                session_state.lifecycle_status = lifecycle_status
            elif record.status == WorkerStatus.COMPLETED:
                session_state.lifecycle_status = WorkerLifecycleStatus.COMPLETED.value
            else:
                session_state.lifecycle_status = WorkerLifecycleStatus.FAILED.value
            if session_state.supervisor_lease_expires_at is None:
                session_state.supervisor_lease_expires_at = session_state.last_active_at
            if (
                record.status == WorkerStatus.COMPLETED
                and policy.keep_session_idle
                and session_state.persist_final_session
            ):
                session_state.status = WorkerSessionStatus.IDLE
                session_state.idle_since = record.ended_at or _now()
                self._worker_sessions[session_state.session_id] = session_state
                session = self._snapshot_session(
                    session_state,
                    status=WorkerSessionStatus.IDLE,
                    record=record,
                    handle=active_handle,
                )
                await self._persist_session(session)
                await self._write_host_session(
                    worker_session=session,
                    handle=active_handle,
                    phase=self._finalized_phase_for_session(
                        record=record,
                        session_state=session_state,
                    ),
                    reason=record.metadata.get("protocol_failure_reason") or "",
                )
            else:
                if session_state.persist_final_session:
                    terminal_status = WorkerSessionStatus.CLOSED
                else:
                    terminal_status = (
                        WorkerSessionStatus.COMPLETED
                        if record.status == WorkerStatus.COMPLETED
                        else WorkerSessionStatus.FAILED
                )
                session_state.status = terminal_status
                session_state.idle_since = None
                self._worker_sessions.pop(session_state.session_id, None)
                persisted_terminal_session = self._snapshot_session(
                    session_state,
                    status=terminal_status,
                    record=record,
                    handle=active_handle,
                )
                await self._persist_session(persisted_terminal_session)
                await self._write_host_session(
                    worker_session=persisted_terminal_session,
                    handle=active_handle,
                    phase=self._finalized_phase_for_session(
                        record=record,
                        session_state=session_state,
                    ),
                    reason=record.metadata.get("protocol_failure_reason") or "",
                )
                if session_state.persist_final_session:
                    session = persisted_terminal_session
        if escalation is not None:
            metadata["escalation"] = {
                "assignment_id": escalation.assignment_id,
                "worker_id": escalation.worker_id,
                "attempt_count": escalation.attempt_count,
                "reason": escalation.reason,
                "backend": escalation.backend,
                "last_error": escalation.last_error,
                "metadata": dict(escalation.metadata),
            }
        record.metadata = metadata
        record = self._apply_failure_attribution(record=record, handle=active_handle)
        record.session = session
        await self.store.save_worker_record(record)
        return record

    async def recover_active_sessions(
        self,
        *,
        policy: WorkerExecutionPolicy | None = None,
        session_filter: Callable[[WorkerSession], bool] | None = None,
    ) -> list[WorkerRecord]:
        active_policy = policy or self.default_execution_policy
        now_iso = _now()
        reclaimable = await self._call_store_method(
            "list_reclaimable_worker_sessions",
            now=now_iso,
            statuses=(
                WorkerSessionStatus.ASSIGNED.value,
                WorkerSessionStatus.ACTIVE.value,
            ),
        )
        if reclaimable is _MISSING or reclaimable is None:
            return []
        if not isinstance(reclaimable, list):
            return []

        recovered_records: list[WorkerRecord] = []
        for candidate in reclaimable:
            if isinstance(candidate, WorkerSession):
                session = candidate
            elif isinstance(candidate, Mapping):
                try:
                    session = WorkerSession.from_dict(candidate)
                except Exception:
                    continue
            else:
                continue
            if session_filter is not None and not session_filter(session):
                continue

            assignment = self._assignment_from_session(session)
            lease_policy = self._resolved_lease_policy(assignment=assignment, policy=active_policy)
            lease_window_seconds = self._session_lease_window_seconds(
                lease_policy=lease_policy,
                policy=active_policy,
            )
            new_lease_id = f"reclaim:{session.session_id}:{uuid4().hex}"
            new_expires_at = (
                _parse_iso_datetime(now_iso) + timedelta(seconds=lease_window_seconds)
            ).isoformat()
            reclaimed = await self._call_store_method(
                "reclaim_worker_session_lease",
                session_id=session.session_id,
                previous_lease_id=session.supervisor_lease_id,
                new_supervisor_id=self._resolved_supervisor_id(assignment=assignment),
                new_lease_id=new_lease_id,
                now=now_iso,
                new_expires_at=new_expires_at,
            )
            if reclaimed is _MISSING or reclaimed is None:
                continue
            if isinstance(reclaimed, WorkerSession):
                live_session = reclaimed
            elif isinstance(reclaimed, Mapping):
                try:
                    live_session = WorkerSession.from_dict(reclaimed)
                except Exception:
                    continue
            else:
                continue

            handle = self._handle_from_session(live_session)
            active_state = self._active_state_from_session(live_session, handle=handle)
            await self._write_host_session(
                worker_session=live_session,
                handle=handle,
                phase=ResidentCoordinatorPhase.RUNNING,
                reason="hydrate_recoverable_session",
            )
            await self.session_host.reclaim_session(
                session_id=live_session.session_id,
                new_supervisor_id=self._resolved_supervisor_id(assignment=assignment),
                new_lease_id=new_lease_id,
                new_expires_at=new_expires_at,
            )
            locator = self.transport_adapter.locator_from_worker_session(live_session)
            backend = self.launch_backends.get(live_session.backend)
            if backend is None or locator is None:
                failure = await self.fail(
                    handle,
                    RuntimeError(f"Cannot recover active session `{live_session.session_id}`."),
                )
                failure.metadata["recovered_via"] = "reclaim_failed"
                failure.metadata["recovery_failure_reason"] = (
                    "missing_backend" if backend is None else "missing_transport_locator"
                )
                state = self._prepare_session(
                    assignment=assignment,
                    handle=handle,
                    policy=active_policy,
                    existing=active_state,
                )
                if state is None:
                    state = self._active_state_from_session(live_session, handle=handle)
                recovered_records.append(
                    await self._finalize_record(
                        failure,
                        assignment=assignment,
                        policy=active_policy,
                        attempts=[],
                        handle=handle,
                        session_state=state,
                    )
                )
                continue

            try:
                handle = await backend.reattach(locator, assignment)
            except BaseException as exc:
                failure = await self.fail(handle, exc)
                failure.metadata["recovered_via"] = "reattach_failed"
                failure.metadata["recovery_failure_reason"] = "reattach_error"
                failure.metadata["recovered_session_id"] = live_session.session_id
                state = self._active_state_from_session(live_session, handle=handle)
                recovered_records.append(
                    await self._finalize_record(
                        failure,
                        assignment=assignment,
                        policy=active_policy,
                        attempts=[],
                        handle=handle,
                        session_state=state,
                    )
                )
                continue

            state = self._prepare_session(
                assignment=assignment,
                handle=handle,
                policy=active_policy,
                existing=self._active_state_from_session(live_session, handle=handle),
            )
            if state is None:
                state = self._active_state_from_session(live_session, handle=handle)
            state.reattach_count += 1
            state.status = WorkerSessionStatus.ACTIVE
            self._worker_sessions[state.session_id] = state
            await self._persist_active_session(
                state=state,
                assignment=assignment,
                handle=handle,
                policy=active_policy,
                force_new_lease_id=False,
            )
            record = await self.wait(
                handle,
                timeout_seconds=active_policy.attempt_timeout_seconds,
                policy=active_policy,
                assignment=assignment,
                session_state=state,
            )
            record = self._apply_protocol_contract(
                record=record,
                assignment=assignment,
                policy=active_policy,
            )
            record.metadata["recovered_via"] = "process_reattach"
            record.metadata["recovered_session_id"] = live_session.session_id
            record.metadata["recovered_lease_id"] = live_session.supervisor_lease_id
            recovered_records.append(
                await self._finalize_record(
                    record,
                    assignment=assignment,
                    policy=active_policy,
                    attempts=[],
                    handle=handle,
                    session_state=state,
                )
            )

        return recovered_records

    async def start(self, handle: WorkerHandle, assignment: WorkerAssignment) -> None:
        launched = WorkerRecord(
            worker_id=handle.worker_id,
            assignment_id=assignment.assignment_id,
            backend=handle.backend,
            role=handle.role,
            status=WorkerStatus.LAUNCHED,
            handle=handle,
            started_at=_now(),
            metadata={"task_id": assignment.task_id},
        )
        await self.store.save_worker_record(launched)

        if handle.backend == "in_process":
            if self.runner is None:
                await self.fail(handle, "AgentRunner is required for in-process execution")
                return
            running = WorkerRecord(
                worker_id=handle.worker_id,
                assignment_id=assignment.assignment_id,
                backend=handle.backend,
                role=handle.role,
                status=WorkerStatus.RUNNING,
                handle=handle,
                started_at=launched.started_at,
                last_heartbeat_at=_now(),
                metadata={"task_id": assignment.task_id},
            )
            await self.store.save_worker_record(running)
            try:
                result = await self.runner.run_turn(
                    RunnerTurnRequest(
                        agent_id=assignment.worker_id,
                        instructions=assignment.instructions,
                        input_text=assignment.input_text,
                        conversation=assignment.conversation,
                        previous_response_id=assignment.previous_response_id,
                        metadata={
                            "group_id": assignment.group_id,
                            "team_id": assignment.team_id,
                            "task_id": assignment.task_id,
                            "assignment_id": assignment.assignment_id,
                            "backend": assignment.backend,
                            **(
                                {"execution_contract": assignment.execution_contract.to_dict()}
                                if assignment.execution_contract is not None
                                else {}
                            ),
                            **(
                                {"lease_policy": assignment.lease_policy.to_dict()}
                                if assignment.lease_policy is not None
                                else {}
                            ),
                            **(
                                {"role_profile_id": assignment.role_profile.profile_id}
                                if assignment.role_profile is not None
                                else {}
                            ),
                            **assignment.metadata,
                        },
                    )
                )
            except BaseException as exc:
                await self.fail(handle, exc)
                return
            await self.complete(
                handle,
                WorkerResult(
                    worker_id=assignment.worker_id,
                    assignment_id=assignment.assignment_id,
                    status=WorkerStatus.COMPLETED,
                    output_text=result.output_text,
                    response_id=result.response_id,
                    usage=dict(result.usage),
                    raw_payload={
                        **dict(result.raw_payload),
                        **(
                            {"protocol_events": tuple(result.protocol_events)}
                            if getattr(result, "protocol_events", ())
                            else {}
                        ),
                        **(
                            {"final_report": result.final_report}
                            if getattr(result, "final_report", None) is not None
                            else {}
                        ),
                    },
                ),
            )
            return

        running = WorkerRecord(
            worker_id=handle.worker_id,
            assignment_id=assignment.assignment_id,
            backend=handle.backend,
            role=handle.role,
            status=WorkerStatus.RUNNING,
            handle=handle,
            started_at=launched.started_at,
            last_heartbeat_at=_now(),
            metadata={"task_id": assignment.task_id},
        )
        await self.store.save_worker_record(running)

    def _protocol_state_path(self, handle: WorkerHandle) -> Path | None:
        value = handle.metadata.get("protocol_state_file")
        if not isinstance(value, str) or not value:
            return None
        return Path(value)

    def _should_use_protocol_wait(
        self,
        *,
        handle: WorkerHandle,
        contract: WorkerExecutionContract | None,
        lease_policy: WorkerLeasePolicy | None,
    ) -> bool:
        if handle.backend == "in_process":
            return False
        if self._protocol_state_path(handle) is None:
            return False
        return True

    def _resolved_hard_timeout(
        self,
        *,
        timeout_seconds: float | None,
        policy: WorkerExecutionPolicy | None,
    ) -> float:
        if policy is not None:
            if policy.hard_timeout_seconds is not None:
                return policy.hard_timeout_seconds
            if policy.attempt_timeout_seconds is not None:
                return policy.attempt_timeout_seconds
        return timeout_seconds if timeout_seconds is not None else self.default_timeout_seconds

    async def _read_json_mapping(
        self,
        path: Path,
        *,
        missing_ok: bool = False,
    ) -> dict[str, object] | None:
        last_error: json.JSONDecodeError | None = None
        for _ in range(5):
            try:
                raw_text = path.read_text(encoding="utf-8")
            except FileNotFoundError:
                if missing_ok:
                    return None
                raise
            try:
                payload = json.loads(raw_text)
                break
            except json.JSONDecodeError as exc:
                last_error = exc
                await asyncio.sleep(self.poll_interval_seconds)
        else:
            raise last_error if last_error is not None else RuntimeError("Failed to decode worker result JSON.")
        if not isinstance(payload, Mapping):
            raise RuntimeError(f"Expected JSON object in {path}")
        return {str(key): value for key, value in payload.items()}

    def _merge_protocol_payload(
        self,
        *,
        raw_payload: Mapping[str, object],
        protocol_payload: Mapping[str, object] | None,
    ) -> dict[str, object]:
        merged = dict(raw_payload)
        if not protocol_payload:
            return merged
        if "protocol_events" in protocol_payload:
            merged["protocol_events"] = protocol_payload["protocol_events"]
        if "final_report" in protocol_payload:
            merged["final_report"] = protocol_payload["final_report"]
        return merged

    async def _read_out_of_process_result(
        self,
        handle: WorkerHandle,
        path: Path,
        *,
        protocol_payload: Mapping[str, object] | None = None,
        extra_metadata: Mapping[str, object] | None = None,
    ) -> WorkerRecord:
        payload = await self._read_json_mapping(path)
        if payload is None:  # pragma: no cover - defensive path
            raise RuntimeError(f"Missing worker result JSON: {path}")
        status = WorkerStatus(payload["status"])
        result = WorkerResult(
            worker_id=payload["worker_id"],
            assignment_id=payload["assignment_id"],
            status=status,
            output_text=payload.get("output_text", ""),
            error_text=payload.get("error_text", ""),
            response_id=payload.get("response_id"),
            usage=dict(payload.get("usage", {})),
            raw_payload=self._merge_protocol_payload(
                raw_payload=dict(payload.get("raw_payload", {})),
                protocol_payload=protocol_payload,
            ),
        )
        if status == WorkerStatus.COMPLETED:
            record = await self.complete(handle, result)
        else:
            record = await self.fail(handle, result)
        if extra_metadata:
            record.metadata.update(dict(extra_metadata))
        record = self._apply_failure_attribution(record=record, handle=handle)
        if extra_metadata or record.status == WorkerStatus.FAILED:
            await self.store.save_worker_record(record)
        return record

    async def _fail_with_timeout(
        self,
        handle: WorkerHandle,
        *,
        timeout_kind: str,
        timeout_seconds: float,
        reason: str | None = None,
        protocol_payload: Mapping[str, object] | None = None,
        extra_metadata: Mapping[str, object] | None = None,
    ) -> WorkerRecord:
        record = await self.fail(
            handle,
            TimeoutError(
                f"Timed out waiting for worker result ({timeout_kind} timeout after {timeout_seconds:.3f}s)"
            ),
        )
        if protocol_payload:
            record.metadata.update(self._merge_protocol_payload(raw_payload=record.metadata, protocol_payload=protocol_payload))
        record.metadata["timeout_kind"] = timeout_kind
        record.metadata["supervisor_timeout_path"] = True
        if reason is not None:
            record.metadata["protocol_failure_reason"] = reason
        if extra_metadata:
            record.metadata.update(dict(extra_metadata))
        record = self._apply_failure_attribution(record=record, handle=handle)
        await self.store.save_worker_record(record)
        return record

    def _protocol_events_and_report(
        self,
        *,
        assignment_id: str,
        worker_id: str,
        payload: Mapping[str, object] | None,
    ) -> tuple[tuple[WorkerLifecycleEvent, ...], WorkerFinalReport | None]:
        if not payload:
            return (), None
        result = WorkerResult(
            worker_id=worker_id,
            assignment_id=assignment_id,
            status=WorkerStatus.RUNNING,
            raw_payload=dict(payload),
        )
        return self._protocol_events_from_result(result), self._final_report_from_result(result)

    def _event_renews_lease(
        self,
        *,
        event: WorkerLifecycleEvent,
        lease_policy: WorkerLeasePolicy,
    ) -> bool:
        event_tokens = {
            token
            for token in (event.kind, event.phase, event.status.value)
            if token
        }
        return any(token in event_tokens for token in lease_policy.renew_on_event_kinds)

    def _protocol_cursor_from_event(
        self,
        *,
        assignment_id: str,
        event: WorkerLifecycleEvent,
    ) -> dict[str, str | None]:
        return {
            "assignment_id": assignment_id,
            "event_id": event.event_id,
            "event_status": event.status.value,
            "event_phase": event.phase or event.kind or None,
            "event_kind": event.kind or None,
            "observed_at": _now(),
        }

    async def _wait_with_protocol(
        self,
        handle: WorkerHandle,
        *,
        timeout_seconds: float | None,
        policy: WorkerExecutionPolicy | None,
        assignment: WorkerAssignment | None,
        contract: WorkerExecutionContract | None,
        lease_policy: WorkerLeasePolicy | None,
        session_state: _ActiveWorkerSession | None = None,
    ) -> WorkerRecord:
        result_file = handle.transport_ref
        protocol_state_path = self._protocol_state_path(handle)
        if result_file is None:
            return await self.fail(handle, "Missing result file for out-of-process worker")
        if protocol_state_path is None:
            return await self.fail(handle, "Missing protocol state file for protocol-native worker wait")

        result_path = Path(result_file)
        loop = asyncio.get_running_loop()
        started_at = loop.time()
        last_renewal_at = started_at
        last_observed_event_ids: set[str] = set()
        last_protocol_progress_at: str | None = None
        process = handle.metadata.get("_process")
        hard_timeout_seconds = self._resolved_hard_timeout(timeout_seconds=timeout_seconds, policy=policy)
        if lease_policy is not None:
            hard_timeout_seconds = min(hard_timeout_seconds, lease_policy.hard_deadline_seconds)

        while True:
            protocol_payload = await self._read_json_mapping(protocol_state_path, missing_ok=True)
            events, _final_report = self._protocol_events_and_report(
                assignment_id=assignment.assignment_id if assignment is not None else (handle.run_id or handle.worker_id),
                worker_id=handle.worker_id,
                payload=protocol_payload,
            )
            accepted = any(event.status == WorkerLifecycleStatus.ACCEPTED for event in events)
            for event in events:
                if event.event_id in last_observed_event_ids:
                    continue
                last_observed_event_ids.add(event.event_id)
                renews_lease = lease_policy is not None and self._event_renews_lease(
                    event=event,
                    lease_policy=lease_policy,
                )
                if renews_lease:
                    last_renewal_at = loop.time()
                event_timestamp = event.timestamp if isinstance(event.timestamp, str) and event.timestamp else _now()
                last_protocol_progress_at = event_timestamp
                if session_state is not None and assignment is not None and policy is not None:
                    cursor_payload = self._protocol_cursor_from_event(
                        assignment_id=assignment.assignment_id,
                        event=event,
                    )
                    session_state.protocol_cursor = dict(cursor_payload)
                    session_state.lifecycle_status = event.status.value
                    await self._persist_active_session(
                        state=session_state,
                        assignment=assignment,
                        handle=handle,
                        policy=policy,
                        force_new_lease_id=False,
                    )
                    consumer = session_state.supervisor_id or self._default_supervisor_id
                    await self._call_store_method(
                        "save_protocol_bus_cursor",
                        stream="lifecycle",
                        consumer=consumer,
                        cursor=dict(cursor_payload),
                    )

            if result_path.exists():
                result_metadata: dict[str, object] = {"protocol_wait_mode": "native"}
                if last_protocol_progress_at is not None:
                    result_metadata["last_protocol_progress_at"] = last_protocol_progress_at
                return await self._read_out_of_process_result(
                    handle,
                    result_path,
                    protocol_payload=protocol_payload,
                    extra_metadata=result_metadata,
                )

            now = loop.time()
            if lease_policy is not None and not accepted:
                if (now - started_at) >= lease_policy.accept_deadline_seconds:
                    return await self._fail_with_timeout(
                        handle,
                        timeout_kind="accept",
                        timeout_seconds=lease_policy.accept_deadline_seconds,
                        reason="accept_deadline_exceeded",
                        protocol_payload=protocol_payload,
                        extra_metadata={
                            "protocol_wait_mode": "native",
                            **(
                                {"last_protocol_progress_at": last_protocol_progress_at}
                                if last_protocol_progress_at is not None
                                else {}
                            ),
                        },
                    )
            elif lease_policy is not None:
                renewal_timeout_seconds = lease_policy.max_silence_seconds
                if renewal_timeout_seconds is None:
                    renewal_timeout_seconds = lease_policy.renewal_timeout_seconds
                if (now - last_renewal_at) >= renewal_timeout_seconds:
                    return await self._fail_with_timeout(
                        handle,
                        timeout_kind="renewal",
                        timeout_seconds=renewal_timeout_seconds,
                        reason="lease_renewal_timeout",
                        protocol_payload=protocol_payload,
                        extra_metadata={
                            "protocol_wait_mode": "native",
                            **(
                                {"last_protocol_progress_at": last_protocol_progress_at}
                                if last_protocol_progress_at is not None
                                else {}
                            ),
                        },
                    )

            if (now - started_at) >= hard_timeout_seconds:
                return await self._fail_with_timeout(
                    handle,
                    timeout_kind="hard",
                    timeout_seconds=hard_timeout_seconds,
                    reason="hard_deadline_exceeded",
                    protocol_payload=protocol_payload,
                    extra_metadata={
                        "protocol_wait_mode": "native",
                        **(
                            {"last_protocol_progress_at": last_protocol_progress_at}
                            if last_protocol_progress_at is not None
                            else {}
                        ),
                    },
                )

            if process is not None and hasattr(process, "poll") and process.poll() is not None and not result_path.exists():
                await asyncio.sleep(self.poll_interval_seconds)
                continue

            await asyncio.sleep(self.poll_interval_seconds)

    async def wait(
        self,
        handle: WorkerHandle,
        timeout_seconds: float | None = None,
        policy: WorkerExecutionPolicy | None = None,
        assignment: WorkerAssignment | None = None,
        session_state: _ActiveWorkerSession | None = None,
    ) -> WorkerRecord:
        if handle.backend == "in_process":
            record = await self.store.get_worker_record(handle.worker_id)
            if record is None:
                raise RuntimeError(f"Unknown worker_id: {handle.worker_id}")
            return record

        contract = self._resolved_execution_contract(assignment=assignment, policy=policy)
        lease_policy = self._resolved_lease_policy(assignment=assignment, policy=policy)
        if self._should_use_protocol_wait(
            handle=handle,
            contract=contract,
            lease_policy=lease_policy,
        ):
            return await self._wait_with_protocol(
                handle,
                timeout_seconds=timeout_seconds,
                policy=policy,
                assignment=assignment,
                contract=contract,
                lease_policy=lease_policy,
                session_state=session_state,
            )
        return await self.fail(
            handle,
            "Out-of-process worker missing protocol_state_file required for protocol-native wait.",
        )

    async def complete(self, handle: WorkerHandle, result: WorkerResult) -> WorkerRecord:
        record = WorkerRecord(
            worker_id=handle.worker_id,
            assignment_id=result.assignment_id,
            backend=handle.backend,
            role=handle.role,
            status=result.status,
            handle=handle,
            ended_at=_now(),
            output_text=result.output_text,
            error_text=result.error_text,
            response_id=result.response_id,
            usage=dict(result.usage),
            metadata=dict(result.raw_payload),
        )
        await self.store.save_worker_record(record)
        return record

    async def fail(self, handle: WorkerHandle, error: BaseException | str | object) -> WorkerRecord:
        assignment_id = handle.run_id or ""
        error_text = ""
        response_id = None
        usage: dict[str, object] = {}
        metadata: dict[str, object] = {}
        if isinstance(error, WorkerResult):
            assignment_id = error.assignment_id
            error_text = error.error_text or "worker failed"
            response_id = error.response_id
            usage = dict(error.usage)
            metadata = dict(error.raw_payload)
        elif isinstance(error, BaseException):
            error_text = str(error)
            metadata = {"error_type": error.__class__.__name__}
        else:
            error_text = str(error)
        record = WorkerRecord(
            worker_id=handle.worker_id,
            assignment_id=assignment_id,
            backend=handle.backend,
            role=handle.role,
            status=WorkerStatus.FAILED,
            handle=handle,
            ended_at=_now(),
            error_text=error_text,
            response_id=response_id,
            usage=usage,
            metadata=metadata,
        )
        record = self._apply_failure_attribution(record=record, handle=handle)
        await self.store.save_worker_record(record)
        return record
