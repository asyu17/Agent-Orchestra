from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Mapping

from agent_orchestra.contracts.agent import AgentSession
from agent_orchestra.contracts.authority import (
    AuthorityBoundaryClass,
    AuthorityCompletionLaneSnapshot,
    AuthorityCompletionRequestSnapshot,
    AuthorityCompletionStatus,
    AuthorityDecision,
    AuthorityPolicyAction,
    AuthorityReactorCycleOutput,
    ScopeExtensionRequest,
)
from agent_orchestra.contracts.delivery import DeliveryStatus
from agent_orchestra.contracts.enums import TaskScope, TaskStatus
from agent_orchestra.contracts.task import TaskCard
from agent_orchestra.storage.base import CoordinationOutboxRecord
from agent_orchestra.tools.mailbox import (
    MailboxDeliveryMode,
    MailboxEnvelope,
    MailboxMessageKind,
    MailboxVisibilityScope,
)

if TYPE_CHECKING:
    from agent_orchestra.runtime.group_runtime import GroupRuntime


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _request_id_from_payload(payload: Mapping[str, Any]) -> str | None:
    authority_request = payload.get("authority_request")
    if isinstance(authority_request, Mapping):
        request_id = authority_request.get("request_id")
        if isinstance(request_id, str) and request_id.strip():
            return request_id.strip()
    authority_decision = payload.get("authority_decision")
    if isinstance(authority_decision, Mapping):
        request_id = authority_decision.get("request_id")
        if isinstance(request_id, str) and request_id.strip():
            return request_id.strip()
    return None


def _metadata_str(metadata: Mapping[str, Any], key: str) -> str | None:
    raw = metadata.get(key)
    if raw is None:
        return None
    text = str(raw).strip()
    return text if text else None


def _metadata_int(metadata: Mapping[str, Any], key: str) -> int:
    raw = metadata.get(key)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def _outbox_request_id(record: CoordinationOutboxRecord) -> str | None:
    if not isinstance(record.payload, Mapping):
        return None
    return _request_id_from_payload(record.payload)


def _request_id_from_task(task: TaskCard) -> str | None:
    if isinstance(task.authority_request_payload, Mapping):
        request_id = task.authority_request_payload.get("request_id")
        if isinstance(request_id, str) and request_id.strip():
            return request_id.strip()
    if isinstance(task.authority_decision_payload, Mapping):
        request_id = task.authority_decision_payload.get("request_id")
        if isinstance(request_id, str) and request_id.strip():
            return request_id.strip()
    return None


def _decision_from_task(task: TaskCard) -> str:
    if isinstance(task.authority_decision_payload, Mapping):
        raw = task.authority_decision_payload.get("decision")
        if isinstance(raw, str):
            return raw.strip().lower()
    return ""


def _worker_from_task(
    task: TaskCard,
    *,
    outbox_records: tuple[CoordinationOutboxRecord, ...],
) -> str:
    if isinstance(task.authority_request_payload, Mapping):
        worker_id = task.authority_request_payload.get("worker_id")
        if isinstance(worker_id, str) and worker_id.strip():
            return worker_id.strip()
    if isinstance(task.authority_resume_target, str) and task.authority_resume_target.strip():
        return task.authority_resume_target.strip()
    request_id = _request_id_from_task(task)
    if request_id is None:
        return ""
    for record in outbox_records:
        if _outbox_request_id(record) != request_id:
            continue
        payload = record.payload if isinstance(record.payload, Mapping) else {}
        authority_request = payload.get("authority_request")
        if not isinstance(authority_request, Mapping):
            continue
        worker_id = authority_request.get("worker_id")
        if isinstance(worker_id, str) and worker_id.strip():
            return worker_id.strip()
    return ""


def _relay_subject_for_decision(decision: str) -> str:
    if decision == "escalate":
        return "authority.escalated"
    if decision in {"grant", "deny", "reroute"}:
        return "authority.decision"
    return "authority.request"


def _relay_record_for_request(
    *,
    request_id: str,
    outbox_by_request: Mapping[str, tuple[CoordinationOutboxRecord, ...]],
    decision: str,
) -> tuple[str | None, bool]:
    request_records = outbox_by_request.get(request_id, ())
    if not request_records:
        return None, False
    expected_subject = _relay_subject_for_decision(decision)
    for record in request_records:
        if record.subject == expected_subject:
            return expected_subject, True
    return request_records[0].subject, False


def _session_consumed_relay(
    *,
    request_id: str,
    decision: str,
    sessions: tuple[AgentSession, ...],
) -> bool:
    if not sessions:
        return False
    for session in sessions:
        metadata = session.metadata if isinstance(session.metadata, Mapping) else {}
        session_request_id = _metadata_str(metadata, "authority_request_id")
        if session_request_id != request_id:
            continue
        if decision == "escalate":
            if bool(metadata.get("authority_waiting")):
                return True
            session_decision = _metadata_str(metadata, "authority_last_decision")
            if session_decision == "escalate":
                return True
            continue
        session_decision = _metadata_str(metadata, "authority_last_decision")
        if session_decision == decision:
            return True
    return False


def _session_wake_recorded(sessions: tuple[AgentSession, ...]) -> bool:
    for session in sessions:
        metadata = session.metadata if isinstance(session.metadata, Mapping) else {}
        if _metadata_int(metadata, "wake_request_count") > 0:
            return True
        if _metadata_str(metadata, "last_wake_request_at") is not None:
            return True
    return False


def _is_reroute_candidate_preferred(existing: TaskCard, candidate: TaskCard) -> bool:
    if existing.authority_request_payload and not candidate.authority_request_payload:
        return False
    if candidate.authority_request_payload and not existing.authority_request_payload:
        return True
    if existing.superseded_by_task_id is not None and candidate.superseded_by_task_id is None:
        return False
    if candidate.superseded_by_task_id is not None and existing.superseded_by_task_id is None:
        return True
    return candidate.task_id < existing.task_id


@dataclass(slots=True)
class AuthorityControlEnvelopeDirective:
    recipient: str
    subject: str
    visibility_scope: MailboxVisibilityScope
    task: TaskCard
    authority_request: ScopeExtensionRequest
    authority_decision: AuthorityDecision
    source_entry_id: str
    source_scope: str
    summary: str
    replacement_task_id: str | None = None


@dataclass(slots=True)
class TeamAuthorityReactorResult:
    consumed_messages: tuple[MailboxEnvelope, ...]
    control_envelope_directives: tuple[AuthorityControlEnvelopeDirective, ...]
    cycle_output: AuthorityReactorCycleOutput

    def metadata_patch(self) -> dict[str, Any]:
        return self.cycle_output.to_metadata_patch(last_cycle_at=_utc_now_iso())


class TeamAuthorityReactor:
    def __init__(
        self,
        *,
        runtime: GroupRuntime,
        objective_id: str,
        group_id: str,
        lane_id: str,
        team_id: str,
        leader_id: str,
        superleader_recipient: str,
    ) -> None:
        self.runtime = runtime
        self.objective_id = objective_id
        self.group_id = group_id
        self.lane_id = lane_id
        self.team_id = team_id
        self.leader_id = leader_id
        self.superleader_recipient = superleader_recipient

    async def _create_reroute_replacement_task(
        self,
        *,
        pending_task: TaskCard,
        authority_request: ScopeExtensionRequest,
    ) -> TaskCard:
        return await self.runtime.submit_task(
            group_id=self.group_id,
            lane_id=self.lane_id,
            team_id=self.team_id,
            goal=f"Reroute repair for {pending_task.task_id}: {pending_task.goal}",
            scope=pending_task.scope,
            owned_paths=pending_task.owned_paths,
            handoff_to=pending_task.handoff_to,
            created_by=self.leader_id,
            derived_from=pending_task.task_id,
            reason=authority_request.reason or pending_task.reason,
            verification_commands=pending_task.verification_commands,
            slice_id=pending_task.slice_id,
            slice_mode=pending_task.slice_mode,
            depends_on_slice_ids=pending_task.depends_on_slice_ids,
            depends_on_task_ids=pending_task.depends_on_task_ids,
            parallel_group=pending_task.parallel_group,
        )

    async def process_authority_writebacks(
        self,
        mailbox_messages: tuple[MailboxEnvelope, ...],
    ) -> TeamAuthorityReactorResult | None:
        authority_messages = tuple(
            message
            for message in mailbox_messages
            if message.kind == MailboxMessageKind.SYSTEM and message.subject == "authority.decision"
        )
        if not authority_messages:
            return None

        consumed_messages: list[MailboxEnvelope] = []
        directives: list[AuthorityControlEnvelopeDirective] = []
        pending_request_ids: list[str] = []
        decision_request_ids: list[str] = []
        forwarded_request_ids: list[str] = []
        incomplete_request_ids: list[str] = []

        for message in authority_messages:
            payload = message.payload if isinstance(message.payload, Mapping) else {}
            authority_request = ScopeExtensionRequest.from_payload(payload.get("authority_request"))
            authority_decision = AuthorityDecision.from_payload(payload.get("authority_decision"))
            task_id = payload.get("task_id")
            request_id = _request_id_from_payload(payload)
            if request_id is not None:
                pending_request_ids.append(request_id)
            if (
                authority_request is None
                or authority_decision is None
                or not isinstance(task_id, str)
                or not task_id
            ):
                if request_id is not None:
                    incomplete_request_ids.append(request_id)
                continue
            task = await self.runtime.store.get_task(task_id)
            if task is None:
                incomplete_request_ids.append(authority_request.request_id)
                continue
            replacement_task_id_raw = payload.get("replacement_task_id")
            replacement_task_id = (
                replacement_task_id_raw
                if isinstance(replacement_task_id_raw, str) and replacement_task_id_raw
                else None
            )
            directives.append(
                AuthorityControlEnvelopeDirective(
                    recipient=authority_request.worker_id,
                    subject="authority.decision",
                    visibility_scope=MailboxVisibilityScope.CONTROL_PRIVATE,
                    task=task,
                    authority_request=authority_request,
                    authority_decision=authority_decision,
                    source_entry_id=message.source_entry_id or "",
                    source_scope=message.source_scope or "",
                    summary=message.summary,
                    replacement_task_id=replacement_task_id,
                )
            )
            consumed_messages.append(message)
            decision_request_ids.append(authority_request.request_id)
            forwarded_request_ids.append(authority_request.request_id)

        if not consumed_messages:
            return None
        cycle_output = AuthorityReactorCycleOutput(
            reactor_role="team",
            pending_request_ids=tuple(sorted(set(pending_request_ids))),
            decision_request_ids=tuple(sorted(set(decision_request_ids))),
            escalated_request_ids=(),
            forwarded_request_ids=tuple(sorted(set(forwarded_request_ids))),
            incomplete_request_ids=tuple(sorted(set(incomplete_request_ids))),
        )
        return TeamAuthorityReactorResult(
            consumed_messages=tuple(consumed_messages),
            control_envelope_directives=tuple(directives),
            cycle_output=cycle_output,
        )

    async def process_authority_requests(
        self,
        mailbox_messages: tuple[MailboxEnvelope, ...],
    ) -> TeamAuthorityReactorResult | None:
        authority_messages = tuple(
            message
            for message in mailbox_messages
            if message.kind == MailboxMessageKind.SYSTEM and message.subject == "authority.request"
        )
        if not authority_messages:
            return None

        pending_requests = {
            item.task.task_id: item
            for item in await self.runtime.list_pending_authority_requests(
                group_id=self.group_id,
                team_id=self.team_id,
                lane_id=self.lane_id,
            )
        }
        pending_request_ids = [
            item.authority_request.request_id
            for item in pending_requests.values()
        ]
        consumed_messages: list[MailboxEnvelope] = []
        directives: list[AuthorityControlEnvelopeDirective] = []
        decision_request_ids: list[str] = []
        escalated_request_ids: list[str] = []
        forwarded_request_ids: list[str] = []
        incomplete_request_ids: list[str] = []

        for message in authority_messages:
            payload = message.payload if isinstance(message.payload, Mapping) else {}
            task_id = payload.get("task_id")
            payload_request_id = _request_id_from_payload(payload)
            if not isinstance(task_id, str) or not task_id:
                if payload_request_id is not None:
                    incomplete_request_ids.append(payload_request_id)
                continue
            pending = pending_requests.get(task_id)
            if pending is None:
                if payload_request_id is not None:
                    incomplete_request_ids.append(payload_request_id)
                continue
            existing_decision = None
            if isinstance(pending.task.authority_decision_payload, Mapping):
                raw_decision = pending.task.authority_decision_payload.get("decision")
                if isinstance(raw_decision, str) and raw_decision:
                    existing_decision = raw_decision.strip().lower()
            if existing_decision in {"grant", "deny", "reroute", "escalate"}:
                consumed_messages.append(message)
                continue
            authority_request = pending.authority_request
            if pending.boundary_class == AuthorityBoundaryClass.SOFT_SCOPE.value:
                policy_action = self.runtime.authority_policy.soft_scope_action(authority_request)
                if policy_action == AuthorityPolicyAction.DENY:
                    authority_decision = AuthorityDecision(
                        request_id=authority_request.request_id,
                        decision="deny",
                        actor_id=self.leader_id,
                        scope_class=pending.boundary_class,
                        reason="Leader denied repo-local authority request by policy.",
                        resume_mode="blocked_terminal",
                        summary=f"Leader denied authority for task {pending.task.task_id}.",
                    )
                    commit = await self.runtime.commit_authority_decision(
                        objective_id=self.objective_id,
                        lane_id=self.lane_id,
                        team_id=self.team_id,
                        task_id=pending.task.task_id,
                        actor_id=self.leader_id,
                        authority_decision=authority_decision,
                    )
                    directives.append(
                        AuthorityControlEnvelopeDirective(
                            recipient=authority_request.worker_id,
                            subject="authority.decision",
                            visibility_scope=MailboxVisibilityScope.CONTROL_PRIVATE,
                            task=commit.task,
                            authority_request=authority_request,
                            authority_decision=authority_decision,
                            source_entry_id=commit.blackboard_entry.entry_id,
                            source_scope=commit.blackboard_entry.kind.value,
                            summary=commit.blackboard_entry.summary,
                        )
                    )
                    forwarded_request_ids.append(authority_request.request_id)
                elif policy_action == AuthorityPolicyAction.REROUTE:
                    replacement_task = await self._create_reroute_replacement_task(
                        pending_task=pending.task,
                        authority_request=authority_request,
                    )
                    authority_decision = AuthorityDecision(
                        request_id=authority_request.request_id,
                        decision="reroute",
                        actor_id=self.leader_id,
                        scope_class=pending.boundary_class,
                        reroute_task_id=replacement_task.task_id,
                        reason="Leader rerouted repo-local authority request by policy.",
                        resume_mode="replacement_task",
                        summary=(
                            f"Leader rerouted authority for task {pending.task.task_id} "
                            f"to replacement task {replacement_task.task_id}."
                        ),
                    )
                    commit = await self.runtime.commit_authority_decision(
                        objective_id=self.objective_id,
                        lane_id=self.lane_id,
                        team_id=self.team_id,
                        task_id=pending.task.task_id,
                        actor_id=self.leader_id,
                        authority_decision=authority_decision,
                        replacement_task=replacement_task,
                    )
                    directives.append(
                        AuthorityControlEnvelopeDirective(
                            recipient=authority_request.worker_id,
                            subject="authority.decision",
                            visibility_scope=MailboxVisibilityScope.CONTROL_PRIVATE,
                            task=commit.task,
                            authority_request=authority_request,
                            authority_decision=authority_decision,
                            source_entry_id=commit.blackboard_entry.entry_id,
                            source_scope=commit.blackboard_entry.kind.value,
                            summary=commit.blackboard_entry.summary,
                            replacement_task_id=(
                                commit.replacement_task.task_id
                                if commit.replacement_task is not None
                                else None
                            ),
                        )
                    )
                    forwarded_request_ids.append(authority_request.request_id)
                else:
                    authority_decision = AuthorityDecision(
                        request_id=authority_request.request_id,
                        decision="grant",
                        actor_id=self.leader_id,
                        scope_class=pending.boundary_class,
                        granted_paths=authority_request.requested_paths,
                        reason="Leader granted repo-local authority request.",
                        resume_mode="direct_reactivation",
                        summary=f"Leader granted authority for task {pending.task.task_id}.",
                    )
                    commit = await self.runtime.commit_authority_decision(
                        objective_id=self.objective_id,
                        lane_id=self.lane_id,
                        team_id=self.team_id,
                        task_id=pending.task.task_id,
                        actor_id=self.leader_id,
                        authority_decision=authority_decision,
                    )
                    directives.append(
                        AuthorityControlEnvelopeDirective(
                            recipient=authority_request.worker_id,
                            subject="authority.decision",
                            visibility_scope=MailboxVisibilityScope.CONTROL_PRIVATE,
                            task=commit.task,
                            authority_request=authority_request,
                            authority_decision=authority_decision,
                            source_entry_id=commit.blackboard_entry.entry_id,
                            source_scope=commit.blackboard_entry.kind.value,
                            summary=commit.blackboard_entry.summary,
                        )
                    )
                    forwarded_request_ids.append(authority_request.request_id)
            else:
                authority_decision = AuthorityDecision(
                    request_id=authority_request.request_id,
                    decision="escalate",
                    actor_id=self.leader_id,
                    scope_class=pending.boundary_class,
                    escalated_to=self.superleader_recipient,
                    reason="Leader escalated protected authority request.",
                    summary=f"Leader escalated authority for task {pending.task.task_id}.",
                )
                commit = await self.runtime.commit_authority_decision(
                    objective_id=self.objective_id,
                    lane_id=self.lane_id,
                    team_id=self.team_id,
                    task_id=pending.task.task_id,
                    actor_id=self.leader_id,
                    authority_decision=authority_decision,
                )
                directives.append(
                    AuthorityControlEnvelopeDirective(
                        recipient=self.superleader_recipient,
                        subject="authority.escalated",
                        visibility_scope=MailboxVisibilityScope.SHARED,
                        task=commit.task,
                        authority_request=authority_request,
                        authority_decision=authority_decision,
                        source_entry_id=commit.blackboard_entry.entry_id,
                        source_scope=commit.blackboard_entry.kind.value,
                        summary=commit.blackboard_entry.summary,
                    )
                )
                escalated_request_ids.append(authority_request.request_id)
            decision_request_ids.append(authority_request.request_id)
            consumed_messages.append(message)

        if not consumed_messages:
            return None
        cycle_output = AuthorityReactorCycleOutput(
            reactor_role="team",
            pending_request_ids=tuple(sorted(set(pending_request_ids))),
            decision_request_ids=tuple(sorted(set(decision_request_ids))),
            escalated_request_ids=tuple(sorted(set(escalated_request_ids))),
            forwarded_request_ids=tuple(sorted(set(forwarded_request_ids))),
            incomplete_request_ids=tuple(sorted(set(incomplete_request_ids))),
        )
        return TeamAuthorityReactorResult(
            consumed_messages=tuple(consumed_messages),
            control_envelope_directives=tuple(directives),
            cycle_output=cycle_output,
        )


class ObjectiveAuthorityRootReactor:
    def __init__(
        self,
        *,
        runtime: GroupRuntime,
        mailbox: Any,
    ) -> None:
        self.runtime = runtime
        self.mailbox = mailbox

    @staticmethod
    def _superleader_subscriber(objective_id: str) -> str:
        return f"superleader:{objective_id}"

    def _build_authority_decision(
        self,
        *,
        objective_id: str,
        authority_request: ScopeExtensionRequest,
        boundary_class: str,
        task_authority_decision_payload: Mapping[str, object] | None,
    ) -> AuthorityDecision | None:
        if not isinstance(task_authority_decision_payload, Mapping):
            return None
        prior_decision = str(task_authority_decision_payload.get("decision", "")).strip().lower()
        if prior_decision != "escalate":
            return None
        boundary_raw = (
            str(task_authority_decision_payload.get("scope_class", "")).strip()
            or boundary_class.strip()
        )
        try:
            boundary = AuthorityBoundaryClass(boundary_raw)
        except ValueError:
            return None
        if boundary == AuthorityBoundaryClass.SOFT_SCOPE:
            return None
        policy_action = self.runtime.authority_policy.escalated_boundary_action(boundary)
        if policy_action is None:
            return None
        request_id = authority_request.request_id.strip()
        if not request_id:
            return None
        actor_id = self._superleader_subscriber(objective_id)
        requested_paths = tuple(
            str(item).strip()
            for item in authority_request.requested_paths
            if str(item).strip()
        )
        if policy_action == AuthorityPolicyAction.DENY:
            return AuthorityDecision(
                request_id=request_id,
                decision="deny",
                actor_id=actor_id,
                scope_class=boundary.value,
                reason="Superleader denied escalated authority request by root policy.",
                resume_mode="blocked_terminal",
                summary=(
                    "Denied escalated authority request because the requested scope crosses "
                    "a root-protected boundary."
                ),
            )
        if policy_action == AuthorityPolicyAction.REROUTE:
            return AuthorityDecision(
                request_id=request_id,
                decision="reroute",
                actor_id=actor_id,
                scope_class=boundary.value,
                reason="Superleader rerouted escalated authority request by root policy.",
                resume_mode="replacement_task",
                summary="Rerouted escalated authority request to a replacement repair task.",
            )
        return AuthorityDecision(
            request_id=request_id,
            decision="grant",
            actor_id=actor_id,
            scope_class=boundary.value,
            granted_paths=requested_paths,
            reason="Superleader granted escalated authority request by root policy.",
            resume_mode="direct_reactivation",
            summary="Granted escalated authority request and resumed lane coordination.",
        )

    async def _build_reroute_replacement_task(
        self,
        *,
        blocker_lane_id: str,
        blocker_team_id: str,
        task: TaskCard,
        actor_id: str,
    ) -> TaskCard | None:
        group_id = str(task.group_id or "").strip()
        if not group_id:
            return None
        base_reason = task.reason.strip()
        reason_parts = [base_reason] if base_reason else []
        reason_parts.append("Superleader rerouted escalated authority request to replacement task.")
        return await self.runtime.submit_task(
            group_id=group_id,
            team_id=blocker_team_id,
            lane_id=blocker_lane_id,
            goal=task.goal.strip() or f"Rerouted authority recovery for {task.task_id}",
            scope=task.scope,
            owned_paths=tuple(task.owned_paths),
            handoff_to=tuple(task.handoff_to),
            created_by=actor_id,
            derived_from=task.task_id,
            reason=" ".join(reason_parts),
            verification_commands=tuple(task.verification_commands),
            slice_id=task.slice_id,
            slice_mode=task.slice_mode,
            depends_on_slice_ids=tuple(task.depends_on_slice_ids),
            depends_on_task_ids=tuple(task.depends_on_task_ids),
            parallel_group=task.parallel_group,
        )

    async def _publish_leader_authority_writeback(
        self,
        *,
        objective_id: str,
        group_id: str,
        lane_id: str,
        team_id: str,
        leader_recipient: str,
        task_id: str,
        authority_request: ScopeExtensionRequest,
        authority_decision: AuthorityDecision,
        summary: str,
        source_entry_id: str,
        source_scope: str,
        replacement_task_id: str | None,
    ) -> None:
        payload: dict[str, object] = {
            "task_id": task_id,
            "authority_request": authority_request.to_dict(),
            "authority_decision": authority_decision.to_dict(),
        }
        if replacement_task_id is not None:
            payload["replacement_task_id"] = replacement_task_id
        await self.mailbox.send(
            MailboxEnvelope(
                sender=self._superleader_subscriber(objective_id),
                recipient=leader_recipient,
                subject="authority.decision",
                mailbox_id=f"{group_id}:superleader:{objective_id}",
                kind=MailboxMessageKind.SYSTEM,
                group_id=group_id,
                lane_id=lane_id,
                team_id=team_id,
                summary=summary,
                full_text_ref=f"blackboard:{source_entry_id}",
                source_entry_id=source_entry_id,
                source_scope=source_scope,
                visibility_scope=MailboxVisibilityScope.CONTROL_PRIVATE,
                delivery_mode=MailboxDeliveryMode.SUMMARY_PLUS_REF,
                severity="info",
                tags=("authority_decision", f"authority_decision:{authority_decision.decision}"),
                payload=payload,
                metadata={},
            )
        )

    async def resolve_waiting_blockers(
        self,
        *,
        objective_id: str,
        group_id: str,
        pending_lane_ids: set[str],
        lane_states_by_id: Mapping[str, Any],
        lane_results_by_id: dict[str, Any],
        authority_blockers: tuple[str, ...],
    ) -> AuthorityReactorCycleOutput:
        pending_request_count = 0
        pending_request_ids: list[str] = []
        decision_request_ids: list[str] = []
        decision_lane_ids: set[str] = set()
        escalated_request_ids: list[str] = []
        forwarded_request_ids: list[str] = []
        incomplete_request_ids: list[str] = []

        for blocker_lane_id in authority_blockers:
            blocker_state = lane_states_by_id.get(blocker_lane_id)
            if blocker_state is None:
                continue
            blocker_team_id = str(getattr(blocker_state, "team_id", "")).strip()
            if not blocker_team_id:
                continue
            pending_requests = await self.runtime.list_pending_authority_requests(
                group_id=group_id,
                team_id=blocker_team_id,
                lane_id=blocker_lane_id,
            )
            pending_request_count += len(pending_requests)
            for pending_request in pending_requests:
                request_id = pending_request.authority_request.request_id
                pending_request_ids.append(request_id)
                prior_decision_payload = pending_request.task.authority_decision_payload
                if isinstance(prior_decision_payload, Mapping):
                    prior_decision = str(prior_decision_payload.get("decision", "")).strip().lower()
                    if prior_decision == "escalate":
                        escalated_request_ids.append(request_id)

                decision = self._build_authority_decision(
                    objective_id=objective_id,
                    authority_request=pending_request.authority_request,
                    boundary_class=pending_request.boundary_class,
                    task_authority_decision_payload=pending_request.task.authority_decision_payload,
                )
                if decision is None:
                    incomplete_request_ids.append(request_id)
                    continue

                replacement_task = None
                if decision.decision == "reroute":
                    replacement_task = await self._build_reroute_replacement_task(
                        blocker_lane_id=blocker_lane_id,
                        blocker_team_id=blocker_team_id,
                        task=pending_request.task,
                        actor_id=decision.actor_id,
                    )
                    if replacement_task is None:
                        incomplete_request_ids.append(request_id)
                        continue

                decision_commit = await self.runtime.commit_authority_decision(
                    objective_id=objective_id,
                    lane_id=blocker_lane_id,
                    team_id=blocker_team_id,
                    task_id=pending_request.task.task_id,
                    actor_id=decision.actor_id,
                    authority_decision=decision,
                    replacement_task=replacement_task,
                )
                session = getattr(blocker_state, "session", None)
                leader_recipient = (
                    session.coordinator_id
                    if session is not None and getattr(session, "coordinator_id", None)
                    else f"leader:{blocker_lane_id}"
                )
                await self._publish_leader_authority_writeback(
                    objective_id=objective_id,
                    group_id=group_id,
                    lane_id=blocker_lane_id,
                    team_id=blocker_team_id,
                    leader_recipient=leader_recipient,
                    task_id=decision_commit.task.task_id,
                    authority_request=pending_request.authority_request,
                    authority_decision=decision_commit.authority_decision,
                    summary=decision_commit.blackboard_entry.summary,
                    source_entry_id=decision_commit.blackboard_entry.entry_id,
                    source_scope=decision_commit.blackboard_entry.kind.value,
                    replacement_task_id=(
                        decision_commit.replacement_task.task_id
                        if decision_commit.replacement_task is not None
                        else None
                    ),
                )
                forwarded_request_ids.append(request_id)
                decision_request_ids.append(request_id)
                decision_lane_ids.add(blocker_lane_id)

                existing_result = lane_results_by_id.get(blocker_lane_id)
                resolved_delivery_state = decision_commit.delivery_state
                if resolved_delivery_state is None and existing_result is not None:
                    fallback_status = existing_result.delivery_state.status
                    if decision.decision == "grant":
                        fallback_status = DeliveryStatus.RUNNING
                    elif decision.decision == "deny":
                        fallback_status = DeliveryStatus.BLOCKED
                    elif decision.decision == "reroute":
                        fallback_status = DeliveryStatus.RUNNING
                    resolved_delivery_state = replace(
                        existing_result.delivery_state,
                        status=fallback_status,
                        summary=decision.summary or existing_result.delivery_state.summary,
                    )
                if resolved_delivery_state is not None:
                    blocker_state.status = resolved_delivery_state.status
                    blocker_state.iteration = resolved_delivery_state.iteration
                    blocker_state.summary = resolved_delivery_state.summary
                    blocker_state.latest_worker_ids = resolved_delivery_state.latest_worker_ids
                    blocker_state.waiting_on_lane_ids = ()
                if decision.decision in {"grant", "reroute"}:
                    pending_lane_ids.add(blocker_lane_id)
                    lane_results_by_id.pop(blocker_lane_id, None)
                    blocker_state.status = DeliveryStatus.PENDING
                    blocker_state.completed_in_batch = None
                    blocker_state.waiting_on_lane_ids = blocker_state.dependency_lane_ids
                elif decision.decision == "deny":
                    if existing_result is not None and resolved_delivery_state is not None:
                        lane_results_by_id[blocker_lane_id] = replace(
                            existing_result,
                            delivery_state=resolved_delivery_state,
                        )

        return AuthorityReactorCycleOutput(
            reactor_role="objective_root",
            pending_request_count=pending_request_count,
            pending_request_ids=tuple(dict.fromkeys(pending_request_ids)),
            decision_request_ids=tuple(dict.fromkeys(decision_request_ids)),
            decision_lane_ids=tuple(sorted(decision_lane_ids)),
            escalated_request_ids=tuple(dict.fromkeys(escalated_request_ids)),
            forwarded_request_ids=tuple(dict.fromkeys(forwarded_request_ids)),
            incomplete_request_ids=tuple(dict.fromkeys(incomplete_request_ids)),
        )


async def collect_lane_authority_completion_snapshot(
    *,
    runtime: GroupRuntime,
    objective_id: str,
    lane_id: str,
    team_id: str | None,
    group_id: str | None = None,
) -> AuthorityCompletionLaneSnapshot:
    resolved_group_id = group_id
    if resolved_group_id is None:
        objective = await runtime.store.get_objective(objective_id)
        if objective is not None:
            resolved_group_id = objective.group_id
    outbox_records = tuple(await runtime.store.list_coordination_outbox_records())
    if resolved_group_id is None:
        for record in outbox_records:
            metadata = record.metadata if isinstance(record.metadata, Mapping) else {}
            if metadata.get("lane_id") != lane_id:
                continue
            if team_id is not None and metadata.get("team_id") != team_id:
                continue
            record_group_id = metadata.get("group_id")
            if isinstance(record_group_id, str) and record_group_id:
                resolved_group_id = record_group_id
                break
    if resolved_group_id is None:
        raise ValueError(
            "group_id is required when objective does not exist and outbox does not carry lane metadata."
        )

    team_tasks = tuple(
        await runtime.store.list_tasks(
            resolved_group_id,
            team_id=team_id,
            lane_id=lane_id,
            scope=TaskScope.TEAM.value,
        )
    )
    all_sessions = tuple(await runtime.store.list_agent_sessions())
    teammate_sessions = tuple(
        session
        for session in all_sessions
        if session.role == "teammate"
        and session.lane_id == lane_id
        and (team_id is None or session.team_id == team_id)
        and (session.objective_id is None or session.objective_id == objective_id)
    )
    filtered_outbox = tuple(
        record
        for record in outbox_records
        if (
            (record.metadata.get("group_id") == resolved_group_id)
            and (record.metadata.get("lane_id") == lane_id)
            and (team_id is None or record.metadata.get("team_id") == team_id)
        )
    )
    outbox_by_request: dict[str, tuple[CoordinationOutboxRecord, ...]] = {}
    for record in filtered_outbox:
        request_id = _outbox_request_id(record)
        if request_id is None:
            continue
        outbox_by_request[request_id] = (*outbox_by_request.get(request_id, ()), record)

    sessions_by_request: dict[str, tuple[AgentSession, ...]] = {}
    for session in teammate_sessions:
        metadata = session.metadata if isinstance(session.metadata, Mapping) else {}
        request_id = _metadata_str(metadata, "authority_request_id")
        if request_id is None:
            continue
        sessions_by_request[request_id] = (*sessions_by_request.get(request_id, ()), session)

    request_tasks: dict[str, TaskCard] = {}
    for task in sorted(team_tasks, key=lambda item: item.task_id):
        request_id = _request_id_from_task(task)
        if request_id is None:
            continue
        current = request_tasks.get(request_id)
        if current is None or _is_reroute_candidate_preferred(current, task):
            request_tasks[request_id] = task

    snapshots: list[AuthorityCompletionRequestSnapshot] = []
    decision_counts: dict[str, int] = {}
    closed_request_ids: list[str] = []
    waiting_request_ids: list[str] = []
    incomplete_request_ids: list[str] = []
    relay_pending_request_ids: list[str] = []
    reroute_links: dict[str, str] = {}
    team_task_id_set = {task.task_id for task in team_tasks}

    for request_id, task in sorted(request_tasks.items(), key=lambda item: item[0]):
        decision = _decision_from_task(task)
        decision_scope = (
            str(task.authority_decision_payload.get("scope_class", ""))
            if isinstance(task.authority_decision_payload, Mapping)
            else ""
        )
        boundary_class = task.authority_boundary_class or decision_scope
        relay_subject, relay_published = _relay_record_for_request(
            request_id=request_id,
            outbox_by_request=outbox_by_request,
            decision=decision,
        )
        related_sessions = sessions_by_request.get(request_id, ())
        relay_consumed = _session_consumed_relay(
            request_id=request_id,
            decision=decision,
            sessions=related_sessions,
        )
        wake_recorded = _session_wake_recorded(related_sessions)
        replacement_task_id = None
        if task.superseded_by_task_id is not None:
            replacement_task_id = task.superseded_by_task_id
        elif isinstance(task.authority_decision_payload, Mapping):
            raw_replacement = task.authority_decision_payload.get("reroute_task_id")
            if isinstance(raw_replacement, str) and raw_replacement:
                replacement_task_id = raw_replacement

        if not decision:
            completion_status = AuthorityCompletionStatus.WAITING
        elif decision == "grant":
            if not relay_published:
                completion_status = AuthorityCompletionStatus.RELAY_PENDING
            elif (
                relay_consumed
                and wake_recorded
                and task.status in {TaskStatus.PENDING, TaskStatus.IN_PROGRESS, TaskStatus.COMPLETED}
            ):
                completion_status = AuthorityCompletionStatus.GRANT_RESUMED
            else:
                completion_status = AuthorityCompletionStatus.INCOMPLETE
        elif decision == "reroute":
            replacement_exists = (
                replacement_task_id is not None
                and replacement_task_id in team_task_id_set
            )
            if not relay_published:
                completion_status = AuthorityCompletionStatus.RELAY_PENDING
            elif (
                relay_consumed
                and task.superseded_by_task_id is not None
                and replacement_exists
                and task.status == TaskStatus.CANCELLED
            ):
                completion_status = AuthorityCompletionStatus.REROUTE_CLOSED
            else:
                completion_status = AuthorityCompletionStatus.INCOMPLETE
        elif decision == "deny":
            denied_terminal = (
                task.status == TaskStatus.BLOCKED
                and len(task.blocked_by) >= 1
                and task.blocked_by[0] == "authority.denied"
            )
            if not relay_published:
                completion_status = AuthorityCompletionStatus.RELAY_PENDING
            elif relay_consumed and denied_terminal:
                completion_status = AuthorityCompletionStatus.DENY_CLOSED
            else:
                completion_status = AuthorityCompletionStatus.INCOMPLETE
        elif decision == "escalate":
            completion_status = (
                AuthorityCompletionStatus.WAITING
                if relay_published
                else AuthorityCompletionStatus.RELAY_PENDING
            )
        else:
            completion_status = AuthorityCompletionStatus.INCOMPLETE

        decision_key = decision if decision else "pending"
        decision_counts[decision_key] = int(decision_counts.get(decision_key, 0)) + 1
        if completion_status in {
            AuthorityCompletionStatus.GRANT_RESUMED,
            AuthorityCompletionStatus.REROUTE_CLOSED,
            AuthorityCompletionStatus.DENY_CLOSED,
        }:
            closed_request_ids.append(request_id)
        elif completion_status == AuthorityCompletionStatus.WAITING:
            waiting_request_ids.append(request_id)
        elif completion_status == AuthorityCompletionStatus.RELAY_PENDING:
            relay_pending_request_ids.append(request_id)
        elif completion_status == AuthorityCompletionStatus.INCOMPLETE:
            incomplete_request_ids.append(request_id)

        if replacement_task_id is not None:
            reroute_links[request_id] = replacement_task_id

        snapshots.append(
            AuthorityCompletionRequestSnapshot(
                request_id=request_id,
                task_id=task.task_id,
                worker_id=_worker_from_task(task, outbox_records=filtered_outbox),
                boundary_class=boundary_class or "",
                decision=decision,
                completion_status=completion_status,
                relay_subject=relay_subject,
                relay_published=relay_published,
                relay_consumed=relay_consumed,
                wake_recorded=wake_recorded,
                replacement_task_id=replacement_task_id,
                terminal_task_status=task.status.value,
            )
        )

    return AuthorityCompletionLaneSnapshot(
        objective_id=objective_id,
        lane_id=lane_id,
        team_id=team_id,
        request_count=len(snapshots),
        decision_counts=decision_counts,
        closed_request_ids=tuple(closed_request_ids),
        waiting_request_ids=tuple(waiting_request_ids),
        incomplete_request_ids=tuple(incomplete_request_ids),
        relay_pending_request_ids=tuple(relay_pending_request_ids),
        reroute_links=reroute_links,
        validated=not (waiting_request_ids or relay_pending_request_ids or incomplete_request_ids),
        requests=tuple(snapshots),
    )
