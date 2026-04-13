from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone

from agent_orchestra.contracts.execution import WorkerRecord, WorkerSession, WorkerSupervisor
from agent_orchestra.contracts.execution import WorkerAssignment
from agent_orchestra.contracts.session_continuity import (
    ResidentTeamShell,
    ResumeGateDecision,
    ResumeGateMode,
    SessionEvent,
    ShellAttachDecision,
    ShellAttachDecisionMode,
    WorkSessionMessage,
)
from agent_orchestra.runtime.session_continuity import (
    SessionContinuityService,
    SessionContinuityState,
    SessionInspectSnapshot,
)
from agent_orchestra.runtime.resident_wake_service import ResidentWakeService
from agent_orchestra.storage.base import OrchestrationStore, SessionTransactionStoreCommit


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


@dataclass(slots=True)
class SessionResumeResult:
    action: str
    decision: ResumeGateDecision | ShellAttachDecision
    inspection: SessionInspectSnapshot | None = None
    continuity_state: SessionContinuityState | None = None
    recovered_records: tuple[WorkerRecord, ...] = ()
    metadata: dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "action": self.action,
            "decision": self.decision.to_dict(),
            "inspection": None if self.inspection is None else self.inspection.to_dict(),
            "continuity_state": None
            if self.continuity_state is None
            else {
                "work_session": self.continuity_state.work_session.to_dict(),
                "runtime_generation": self.continuity_state.runtime_generation.to_dict(),
                "conversation_heads": [
                    head.to_dict() for head in self.continuity_state.conversation_heads
                ],
            },
            "recovered_records": [record.to_dict() for record in self.recovered_records],
            "metadata": dict(self.metadata),
        }


class SessionDomainService:
    def __init__(
        self,
        *,
        store: OrchestrationStore,
        supervisor: WorkerSupervisor | None = None,
    ) -> None:
        self.store = store
        self.supervisor = supervisor
        self._continuity_service: SessionContinuityService | None = None

    def _service(self) -> SessionContinuityService:
        if self._continuity_service is None:
            self._continuity_service = SessionContinuityService(store=self.store)
        return self._continuity_service

    def _session_host(self):
        if self.supervisor is None:
            return None
        return getattr(self.supervisor, "session_host", None)

    def _wake_service(self) -> ResidentWakeService | None:
        session_host = self._session_host()
        if session_host is None:
            return None
        return ResidentWakeService(session_host=session_host)

    async def _provider_route_health_views_for_work_session(
        self,
        *,
        work_session_id: str,
        objective_id: str | None,
    ) -> tuple[dict[str, object], ...]:
        routes = await self.store.list_provider_route_health()
        matches: list[dict[str, object]] = []
        for route in routes:
            metadata = route.metadata if isinstance(route.metadata, Mapping) else {}
            work_session_value = _optional_string(metadata.get("work_session_id"))
            objective_value = _optional_string(metadata.get("objective_id"))
            if work_session_value:
                if work_session_value != work_session_id:
                    continue
            elif objective_id is not None:
                if objective_value != objective_id:
                    continue
            else:
                continue
            matches.append(route.to_dict())
        matches.sort(key=lambda item: str(item.get("route_key", "")))
        return tuple(matches)

    def _metadata_with_provider_route_health(
        self,
        base_metadata: Mapping[str, object] | None,
        provider_route_health: tuple[dict[str, object], ...],
    ) -> dict[str, object]:
        metadata: dict[str, object] = dict(base_metadata) if base_metadata is not None else {}
        metadata["provider_route_health"] = [dict(item) for item in provider_route_health]
        return metadata

    async def new_session(
        self,
        *,
        group_id: str,
        objective_id: str,
        title: str | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> SessionContinuityState:
        return await self._service().new_session(
            group_id=group_id,
            objective_id=objective_id,
            title=title,
            metadata=metadata,
        )

    async def list_sessions(
        self,
        *,
        group_id: str,
        root_objective_id: str | None = None,
    ):
        return await self._service().list_sessions(
            group_id=group_id,
            root_objective_id=root_objective_id,
        )

    async def inspect_session(self, work_session_id: str) -> SessionInspectSnapshot:
        snapshot = await self._service().inspect_session(work_session_id)
        resident_shell_views = await self._resident_shell_views_for_work_session(
            work_session_id=work_session_id,
            objective_id=snapshot.work_session.root_objective_id,
            resume_gate=snapshot.resume_gate,
        )
        provider_route_health = await self._provider_route_health_views_for_work_session(
            work_session_id=work_session_id,
            objective_id=snapshot.work_session.root_objective_id,
        )
        return replace(
            snapshot,
            resident_shell_views=resident_shell_views,
            provider_route_health=provider_route_health,
        )

    async def send_session_message(
        self,
        *,
        work_session_id: str,
        content: str,
        role: str = "user",
        scope_kind: str = "session",
        scope_id: str | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> WorkSessionMessage:
        return await self._service().append_session_message(
            work_session_id=work_session_id,
            content=content,
            role=role,
            scope_kind=scope_kind,
            scope_id=scope_id,
            metadata=metadata,
        )

    async def warm_resume(
        self,
        *,
        work_session_id: str,
        head_contracts: Mapping[tuple[str, str], Mapping[str, object]] | None = None,
    ) -> SessionContinuityState:
        return await self._service().warm_resume(
            work_session_id=work_session_id,
            head_contracts=head_contracts,
        )

    async def fork_session(
        self,
        *,
        work_session_id: str,
        title: str | None = None,
    ) -> SessionContinuityState:
        return await self._service().fork_session(
            work_session_id=work_session_id,
            title=title,
        )

    async def resume_gate(self, work_session_id: str):
        return await self._service().resume_gate(work_session_id)

    async def exact_wake(self, work_session_id: str) -> SessionResumeResult:
        decision = await self.resume_gate(work_session_id)
        initial_inspection = await self.inspect_session(work_session_id)
        provider_route_health = initial_inspection.provider_route_health
        if decision.mode != ResumeGateMode.EXACT_WAKE:
            return SessionResumeResult(
                action="resume_gate",
                decision=decision,
                inspection=initial_inspection,
                metadata=self._metadata_with_provider_route_health(
                    {"exact_wake_executed": False},
                    provider_route_health,
                ),
            )
        if self.supervisor is None:
            return SessionResumeResult(
                action="exact_wake_unavailable",
                decision=decision,
                inspection=initial_inspection,
                metadata=self._metadata_with_provider_route_health(
                    {
                        "exact_wake_executed": False,
                        "reason": "worker_supervisor_required",
                    },
                    provider_route_health,
                ),
            )
        objective_id = initial_inspection.work_session.root_objective_id
        runtime_generation_id = (
            None
            if initial_inspection.current_runtime_generation is None
            else initial_inspection.current_runtime_generation.runtime_generation_id
        )

        def _session_filter(session: WorkerSession) -> bool:
            metadata = session.metadata if isinstance(session.metadata, Mapping) else {}
            session_objective_id = _optional_string(metadata.get("objective_id"))
            if objective_id and session_objective_id not in {None, objective_id}:
                return False
            session_work_session_id = _optional_string(metadata.get("work_session_id"))
            if session_work_session_id is not None and session_work_session_id != work_session_id:
                return False
            session_runtime_generation_id = _optional_string(
                metadata.get("runtime_generation_id")
            )
            if (
                runtime_generation_id is not None
                and session_runtime_generation_id is not None
                and session_runtime_generation_id != runtime_generation_id
            ):
                return False
            return True

        recovered_records = await self.supervisor.recover_active_sessions(
            session_filter=_session_filter,
        )
        final_inspection = await self.inspect_session(work_session_id)
        metadata = self._metadata_with_provider_route_health(
            {
                "exact_wake_executed": True,
                "recovered_count": len(recovered_records),
                "objective_id": objective_id,
                "runtime_generation_id": runtime_generation_id,
                "slot_ids": sorted(
                    {
                        record.slot_id
                        for record in recovered_records
                        if record.slot_id is not None
                    }
                ),
                "incarnation_ids": sorted(
                    {
                        record.incarnation_id
                        for record in recovered_records
                        if record.incarnation_id is not None
                    }
                ),
            },
            final_inspection.provider_route_health,
        )
        return SessionResumeResult(
            action="exact_wake",
            decision=decision,
            inspection=final_inspection,
            recovered_records=tuple(recovered_records),
            metadata=metadata,
        )

    async def _resident_shells_for_work_session(
        self,
        *,
        work_session_id: str,
        objective_id: str | None = None,
    ) -> tuple[ResidentTeamShell, ...]:
        session_host = self._session_host()
        if session_host is None:
            return ()
        resident_shells = await session_host.list_resident_team_shells(
            work_session_id=work_session_id,
        )
        if not resident_shells and objective_id is not None:
            resident_shells = await session_host.list_resident_team_shells(
                objective_id=objective_id,
            )
        return resident_shells

    async def _preferred_shell_attach_decision(
        self,
        *,
        work_session_id: str,
        objective_id: str | None,
    ) -> tuple[ResidentTeamShell | None, ShellAttachDecision | None]:
        session_host = self._session_host()
        if session_host is None:
            return None, None
        resident_shells = await self._resident_shells_for_work_session(
            work_session_id=work_session_id,
            objective_id=objective_id,
        )
        if resident_shells:
            shell = resident_shells[-1]
            return (
                shell,
                await session_host.find_preferred_attach_target(
                    resident_team_shell_id=shell.resident_team_shell_id,
                ),
            )
        return None, await session_host.find_preferred_attach_target(
            work_session_id=work_session_id,
        )

    @staticmethod
    def _attach_decision_requires_approval_reject(
        attach_decision: ShellAttachDecision | None,
    ) -> bool:
        if attach_decision is None:
            return False
        approval_status = str(attach_decision.metadata.get("approval_status", "")).strip()
        return approval_status in {"pending", "denied"}

    @staticmethod
    def _wake_capability_for_shell(
        *,
        shell: ResidentTeamShell,
        attach_decision: ShellAttachDecision | None,
        resume_gate: ResumeGateDecision | None,
    ) -> str:
        if SessionDomainService._attach_decision_requires_approval_reject(attach_decision):
            return "not_available"
        if (
            attach_decision is not None
            and attach_decision.mode == ShellAttachDecisionMode.ATTACHED
        ):
            return "already_attached"
        if (
            attach_decision is not None
            and attach_decision.mode == ShellAttachDecisionMode.WOKEN
        ) or getattr(shell.status, "value", shell.status) == "quiescent":
            return "wake_shell"
        if resume_gate is not None and resume_gate.mode == ResumeGateMode.EXACT_WAKE:
            return "recover_exact_wake"
        return "not_available"

    async def _resident_shell_views_for_work_session(
        self,
        *,
        work_session_id: str,
        objective_id: str | None,
        resume_gate: ResumeGateDecision | None,
    ) -> tuple[dict[str, object], ...]:
        session_host = self._session_host()
        if session_host is None:
            return ()
        resident_shells = await self._resident_shells_for_work_session(
            work_session_id=work_session_id,
            objective_id=objective_id,
        )
        shell_views: list[dict[str, object]] = []
        for shell in resident_shells:
            attach_view = await session_host.build_shell_attach_view(
                resident_team_shell_id=shell.resident_team_shell_id,
            )
            attach_decision = await session_host.find_preferred_attach_target(
                resident_team_shell_id=shell.resident_team_shell_id,
            )
            attach_view["wake_capability"] = self._wake_capability_for_shell(
                shell=shell,
                attach_decision=attach_decision,
                resume_gate=resume_gate,
            )
            shell_views.append(attach_view)
        return tuple(shell_views)

    async def attach_session(
        self,
        work_session_id: str,
        force_warm_resume: bool = False,
    ) -> SessionResumeResult:
        inspection = await self.inspect_session(work_session_id)
        provider_route_health = inspection.provider_route_health
        _, attach_decision = await self._preferred_shell_attach_decision(
            work_session_id=work_session_id,
            objective_id=inspection.work_session.root_objective_id,
        )
        if self._attach_decision_requires_approval_reject(attach_decision):
            return SessionResumeResult(
                action="rejected",
                decision=attach_decision,
                inspection=inspection,
                metadata=self._metadata_with_provider_route_health(
                    dict(attach_decision.metadata),
                    provider_route_health,
                ),
            )
        if (
            attach_decision is not None
            and attach_decision.mode == ShellAttachDecisionMode.ATTACHED
        ):
            return SessionResumeResult(
                action="attached",
                decision=attach_decision,
                inspection=inspection,
                metadata=self._metadata_with_provider_route_health(
                    dict(attach_decision.metadata),
                    provider_route_health,
                ),
            )
        if (
            attach_decision is not None
            and attach_decision.mode == ShellAttachDecisionMode.WOKEN
            and not force_warm_resume
        ):
            return await self.wake_session(work_session_id)

        decision = await self.resume_gate(work_session_id)
        if decision.mode == ResumeGateMode.WARM_RESUME or (
            force_warm_resume
            and decision.mode in {ResumeGateMode.EXACT_WAKE, ResumeGateMode.INSPECT_ONLY}
        ):
            continuity = await self.warm_resume(work_session_id=work_session_id)
            refreshed_inspection = await self.inspect_session(work_session_id)
            metadata_base = {
                **(dict(attach_decision.metadata) if attach_decision is not None else {}),
                "forced": force_warm_resume,
            }
            metadata = self._metadata_with_provider_route_health(
                metadata_base,
                refreshed_inspection.provider_route_health,
            )
            return SessionResumeResult(
                action="warm_resumed",
                decision=attach_decision or decision,
                inspection=refreshed_inspection,
                continuity_state=continuity,
                metadata=metadata,
            )
        if decision.mode == ResumeGateMode.EXACT_WAKE:
            recovered = await self.exact_wake(work_session_id)
            metadata_base = {
                **(dict(attach_decision.metadata) if attach_decision is not None else {}),
                **dict(recovered.metadata),
            }
            metadata = self._metadata_with_provider_route_health(
                metadata_base,
                recovered.inspection.provider_route_health,
            )
            return SessionResumeResult(
                action="recovered",
                decision=attach_decision or recovered.decision,
                inspection=recovered.inspection,
                continuity_state=recovered.continuity_state,
                recovered_records=recovered.recovered_records,
                metadata=metadata,
            )
        final_inspection = await self.inspect_session(work_session_id)
        metadata_base = {
            **(dict(attach_decision.metadata) if attach_decision is not None else {}),
            "forced": force_warm_resume,
        }
        metadata = self._metadata_with_provider_route_health(
            metadata_base,
            final_inspection.provider_route_health,
        )
        return SessionResumeResult(
            action=decision.mode.value,
            decision=attach_decision or decision,
            inspection=final_inspection,
            metadata=metadata,
        )

    async def wake_session(
        self,
        work_session_id: str,
    ) -> SessionResumeResult:
        inspection = await self.inspect_session(work_session_id)
        provider_route_health = inspection.provider_route_health
        shell, attach_decision = await self._preferred_shell_attach_decision(
            work_session_id=work_session_id,
            objective_id=inspection.work_session.root_objective_id,
        )
        if self._attach_decision_requires_approval_reject(attach_decision):
            return SessionResumeResult(
                action="rejected",
                decision=attach_decision,
                inspection=inspection,
                metadata=self._metadata_with_provider_route_health(
                    dict(attach_decision.metadata),
                    provider_route_health,
                ),
            )
        if (
            attach_decision is not None
            and attach_decision.mode == ShellAttachDecisionMode.ATTACHED
        ):
            return SessionResumeResult(
                action="attached",
                decision=attach_decision,
                inspection=inspection,
                metadata=self._metadata_with_provider_route_health(
                    dict(attach_decision.metadata),
                    provider_route_health,
                ),
            )

        if (
            shell is not None
            and attach_decision is not None
            and attach_decision.mode == ShellAttachDecisionMode.WOKEN
        ):
            wake_service = self._wake_service()
            if wake_service is not None:
                wake_result = await wake_service.request_wake(shell=shell)
                if wake_result.wake_requested:
                    await self.store.commit_session_transaction(
                        SessionTransactionStoreCommit(
                            session_events=(
                                SessionEvent(
                                    work_session_id=work_session_id,
                                    runtime_generation_id=(
                                        None
                                        if inspection.current_runtime_generation is None
                                        else inspection.current_runtime_generation.runtime_generation_id
                                    ),
                                    event_kind="resident_shell_wake_requested",
                                    payload={
                                        "resident_team_shell_id": shell.resident_team_shell_id,
                                        "requested_session_ids": list(
                                            wake_result.requested_session_ids
                                        ),
                                    },
                                    created_at=_now_iso(),
                                ),
                            ),
                        )
                    )
                    refreshed_inspection = await self.inspect_session(work_session_id)
                    refreshed_shell, refreshed_attach_decision = await self._preferred_shell_attach_decision(
                        work_session_id=work_session_id,
                        objective_id=inspection.work_session.root_objective_id,
                    )
                    _ = refreshed_shell
                    decision_to_return = refreshed_attach_decision or attach_decision
                    metadata_base = {
                        **dict(decision_to_return.metadata),
                        "wake_requested_session_ids": list(
                            wake_result.requested_session_ids
                        ),
                    }
                    metadata = self._metadata_with_provider_route_health(
                        metadata_base,
                        refreshed_inspection.provider_route_health,
                    )
                    return SessionResumeResult(
                        action="woken",
                        decision=decision_to_return,
                        inspection=refreshed_inspection,
                        metadata=metadata,
                    )

        decision = await self.resume_gate(work_session_id)
        if decision.mode == ResumeGateMode.EXACT_WAKE:
            recovered = await self.exact_wake(work_session_id)
            metadata_base = {
                **(dict(attach_decision.metadata) if attach_decision is not None else {}),
                **dict(recovered.metadata),
            }
            metadata = self._metadata_with_provider_route_health(
                metadata_base,
                recovered.inspection.provider_route_health,
            )
            return SessionResumeResult(
                action="recovered",
                decision=attach_decision or recovered.decision,
                inspection=recovered.inspection,
                continuity_state=recovered.continuity_state,
                recovered_records=recovered.recovered_records,
                metadata=metadata,
            )

        rejected_decision = ShellAttachDecision(
            mode=ShellAttachDecisionMode.REJECTED,
            reason="detached wake service is not available for this resident shell.",
            target_shell_id=(None if shell is None else shell.resident_team_shell_id),
            target_work_session_id=work_session_id,
            target_runtime_generation_id=(
                None
                if inspection.current_runtime_generation is None
                else inspection.current_runtime_generation.runtime_generation_id
            ),
            metadata={
                **(dict(attach_decision.metadata) if attach_decision is not None else {}),
                "wake_capability": (
                    "not_available"
                    if shell is None
                    else self._wake_capability_for_shell(
                        shell=shell,
                        attach_decision=attach_decision,
                        resume_gate=decision,
                    )
                ),
            },
        )
        return SessionResumeResult(
            action="rejected",
            decision=rejected_decision,
            inspection=inspection,
            metadata=self._metadata_with_provider_route_health(
                dict(rejected_decision.metadata),
                provider_route_health,
            ),
        )

    async def apply_assignment_continuity(
        self,
        *,
        work_session_id: str,
        runtime_generation_id: str | None,
        assignment: WorkerAssignment,
    ) -> WorkerAssignment:
        return await self._service().apply_assignment_continuity(
            work_session_id=work_session_id,
            runtime_generation_id=runtime_generation_id,
            assignment=assignment,
        )

    async def record_worker_turn(
        self,
        *,
        work_session_id: str,
        runtime_generation_id: str | None,
        assignment: WorkerAssignment,
        record: WorkerRecord,
    ):
        return await self._service().record_worker_turn(
            work_session_id=work_session_id,
            runtime_generation_id=runtime_generation_id,
            assignment=assignment,
            record=record,
        )


__all__ = ["SessionDomainService", "SessionResumeResult"]
