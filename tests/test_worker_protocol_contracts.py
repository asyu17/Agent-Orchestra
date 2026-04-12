from __future__ import annotations

import sys
from pathlib import Path
from unittest import TestCase

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agent_orchestra.contracts.worker_protocol import (
    WorkerExecutionContract,
    WorkerFinalReport,
    WorkerFinalStatus,
    WorkerLease,
    WorkerLeasePolicy,
    WorkerLeaseStatus,
    WorkerLifecycleEvent,
    WorkerLifecycleStatus,
    WorkerRoleProfile,
)
from agent_orchestra.contracts.authority import ScopeExtensionRequest


class WorkerProtocolContractsTest(TestCase):
    def test_worker_execution_contract_stores_protocol_requirements(self) -> None:
        contract = WorkerExecutionContract(
            contract_id="leader_coordination_v1",
            mode="leader_coordination",
            allow_subdelegation=True,
            require_final_report=True,
            require_verification_results=False,
            required_verification_commands=("python3 -m unittest tests.test_worker_protocol_contracts -v",),
            required_artifact_kinds=("mailbox_digest",),
        )

        self.assertEqual(contract.contract_id, "leader_coordination_v1")
        self.assertTrue(contract.allow_subdelegation)
        self.assertEqual(
            contract.required_verification_commands,
            ("python3 -m unittest tests.test_worker_protocol_contracts -v",),
        )
        self.assertEqual(contract.required_artifact_kinds, ("mailbox_digest",))

    def test_worker_lease_policy_rejects_invalid_deadlines(self) -> None:
        with self.assertRaises(ValueError):
            WorkerLeasePolicy(
                accept_deadline_seconds=0,
                renewal_timeout_seconds=60,
                hard_deadline_seconds=600,
            )

    def test_worker_lease_policy_rejects_hard_deadline_shorter_than_renewal_window(self) -> None:
        with self.assertRaises(ValueError):
            WorkerLeasePolicy(
                accept_deadline_seconds=10,
                renewal_timeout_seconds=120,
                hard_deadline_seconds=60,
            )

    def test_worker_final_report_requires_terminal_status(self) -> None:
        report = WorkerFinalReport(
            assignment_id="assignment-1",
            worker_id="worker-1",
            terminal_status=WorkerFinalStatus.COMPLETED,
            summary="done",
        )

        self.assertEqual(report.terminal_status, WorkerFinalStatus.COMPLETED)
        self.assertEqual(report.summary, "done")

    def test_worker_final_report_can_carry_authority_request(self) -> None:
        report = WorkerFinalReport(
            assignment_id="assignment-1",
            worker_id="worker-1",
            terminal_status=WorkerFinalStatus.BLOCKED,
            summary="Need broader edit scope before continuing.",
            authority_request=ScopeExtensionRequest(
                request_id="auth-req-1",
                assignment_id="assignment-1",
                worker_id="worker-1",
                task_id="task-1",
                requested_paths=("src/agent_orchestra/self_hosting/bootstrap.py",),
                reason="Need to repair an out-of-scope bootstrap import blocker.",
            ),
        )

        payload = report.to_dict()

        self.assertEqual(report.authority_request.request_id, "auth-req-1")
        self.assertEqual(payload["authority_request"]["task_id"], "task-1")
        self.assertEqual(
            payload["authority_request"]["requested_paths"],
            ["src/agent_orchestra/self_hosting/bootstrap.py"],
        )

    def test_worker_lease_to_dict_is_json_safe(self) -> None:
        lease = WorkerLease(
            lease_id="lease-1",
            assignment_id="assignment-1",
            worker_id="worker-1",
            issued_at="2026-04-04T00:00:00Z",
            accepted_at="2026-04-04T00:00:10Z",
            renewed_at="2026-04-04T00:00:20Z",
            expires_at="2026-04-04T00:01:20Z",
            hard_deadline_at="2026-04-04T00:10:00Z",
            status=WorkerLeaseStatus.ACTIVE,
        )

        payload = lease.to_dict()

        self.assertEqual(payload["status"], WorkerLeaseStatus.ACTIVE.value)
        self.assertEqual(payload["assignment_id"], "assignment-1")

    def test_worker_lifecycle_event_to_dict_includes_status_and_kind(self) -> None:
        event = WorkerLifecycleEvent(
            event_id="event-1",
            assignment_id="assignment-1",
            worker_id="worker-1",
            kind="accepted",
            status=WorkerLifecycleStatus.ACCEPTED,
            phase="coordination",
            summary="accepted assignment",
        )

        payload = event.to_dict()

        self.assertEqual(payload["kind"], "accepted")
        self.assertEqual(payload["status"], WorkerLifecycleStatus.ACCEPTED.value)

    def test_worker_role_profile_materializes_execution_policy(self) -> None:
        profile = WorkerRoleProfile(
            profile_id="leader_coordination_codex",
            backend="codex_cli",
            execution_contract=WorkerExecutionContract(
                contract_id="leader_coordination_v1",
                mode="leader_coordination",
                allow_subdelegation=True,
                require_final_report=True,
                require_verification_results=False,
            ),
            lease_policy=WorkerLeasePolicy(
                accept_deadline_seconds=30,
                renewal_timeout_seconds=120,
                hard_deadline_seconds=1200,
            ),
            keep_session_idle=True,
            reactivate_idle_session=True,
            fallback_idle_timeout_seconds=120,
            fallback_hard_timeout_seconds=1200,
            fallback_max_attempts=1,
            fallback_resume_on_timeout=False,
            fallback_allow_relaunch=False,
            fallback_provider_unavailable_backoff_initial_seconds=15.0,
            fallback_provider_unavailable_backoff_multiplier=2.0,
            fallback_provider_unavailable_backoff_max_seconds=120.0,
        )

        policy = profile.to_execution_policy()

        self.assertEqual(policy.role_profile_id, "leader_coordination_codex")
        self.assertEqual(policy.execution_contract.contract_id, "leader_coordination_v1")
        self.assertEqual(policy.lease_policy.accept_deadline_seconds, 30)
        self.assertEqual(policy.max_attempts, 1)
        self.assertFalse(policy.resume_on_timeout)
        self.assertFalse(policy.allow_relaunch)
        self.assertEqual(policy.provider_unavailable_backoff_initial_seconds, 15.0)
        self.assertEqual(policy.provider_unavailable_backoff_multiplier, 2.0)
        self.assertEqual(policy.provider_unavailable_backoff_max_seconds, 120.0)

    def test_worker_role_profile_to_dict_uses_contract_owned_shape(self) -> None:
        profile = WorkerRoleProfile(
            profile_id="teammate_code_edit_codex",
            backend="codex_cli",
            execution_contract=WorkerExecutionContract(
                contract_id="teammate_code_edit_v1",
                mode="teammate_code_edit",
                require_final_report=True,
                require_verification_results=True,
                required_verification_commands=("python3 -m unittest tests.test_worker_protocol_contracts -v",),
                completion_requires_verification_success=True,
                required_artifact_kinds=("summary", "verification"),
            ),
            lease_policy=WorkerLeasePolicy(
                accept_deadline_seconds=30,
                renewal_timeout_seconds=120,
                hard_deadline_seconds=1800,
            ),
            fallback_idle_timeout_seconds=120,
            fallback_hard_timeout_seconds=2400,
            fallback_max_attempts=1,
            fallback_resume_on_timeout=False,
            fallback_allow_relaunch=False,
            fallback_provider_unavailable_backoff_initial_seconds=15.0,
            fallback_provider_unavailable_backoff_multiplier=2.0,
            fallback_provider_unavailable_backoff_max_seconds=120.0,
        )

        payload = profile.to_dict()

        self.assertEqual(payload["profile_id"], "teammate_code_edit_codex")
        self.assertEqual(payload["backend"], "codex_cli")
        self.assertEqual(
            payload["execution_contract"]["contract_id"],
            "teammate_code_edit_v1",
        )
        self.assertTrue(payload["execution_contract"]["require_verification_results"])
        self.assertEqual(
            payload["execution_contract"]["required_verification_commands"],
            ["python3 -m unittest tests.test_worker_protocol_contracts -v"],
        )
        self.assertTrue(payload["execution_contract"]["completion_requires_verification_success"])
        self.assertEqual(payload["lease_policy"]["accept_deadline_seconds"], 30)
        self.assertEqual(payload["fallback_idle_timeout_seconds"], 120)
        self.assertEqual(payload["fallback_hard_timeout_seconds"], 2400)
        self.assertEqual(payload["fallback_max_attempts"], 1)
        self.assertFalse(payload["fallback_resume_on_timeout"])
        self.assertFalse(payload["fallback_allow_relaunch"])
        self.assertEqual(payload["fallback_provider_unavailable_backoff_initial_seconds"], 15.0)
        self.assertEqual(payload["fallback_provider_unavailable_backoff_multiplier"], 2.0)
        self.assertEqual(payload["fallback_provider_unavailable_backoff_max_seconds"], 120.0)
