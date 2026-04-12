from __future__ import annotations

from enum import Enum


class TaskStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    RUNNING = "in_progress"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    BLOCKED = "blocked"
    WAITING_FOR_AUTHORITY = "waiting_for_authority"
    FAILED = "failed"


class TaskScope(str, Enum):
    OBJECTIVE = "objective"
    LEADER_LANE = "leader_lane"
    TEAM = "team"


class WorkerStatus(str, Enum):
    CREATED = "created"
    LAUNCHED = "launched"
    RUNNING = "running"
    IDLE = "idle"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class SpecNodeKind(str, Enum):
    OBJECTIVE = "objective"
    LEADER_TASK = "leader_task"
    TEAMMATE_TASK = "teammate_task"
    OBJECTIVE_GATE = "objective_gate"
    TEAM_GATE = "team_gate"


class SpecNodeStatus(str, Enum):
    OPEN = "open"
    SATISFIED = "satisfied"
    BLOCKED = "blocked"
    SUPERSEDED = "superseded"


class SpecEdgeKind(str, Enum):
    DECOMPOSES_TO = "decomposes_to"
    DEPENDS_ON = "depends_on"
    GATED_BY = "gated_by"
    SUPERSEDES = "supersedes"


class BlackboardKind(str, Enum):
    LEADER_LANE = "leader_lane"
    TEAM = "team"


class BlackboardEntryKind(str, Enum):
    DIRECTIVE = "directive"
    ARTIFACT_REF = "artifact_ref"
    EXECUTION_REPORT = "execution_report"
    VERIFICATION_RESULT = "verification_result"
    BLOCKER = "blocker"
    PROPOSAL = "proposal"
    DECISION = "decision"
    BUDGET_UPDATE = "budget_update"
    SUMMARY_SNAPSHOT = "summary_snapshot"


class AuthorityStatus(str, Enum):
    PENDING = "pending"
    TEAM_COMPLETE = "team_complete"
    AUTHORITY_COMPLETE = "authority_complete"
    OBJECTIVE_COMPLETE = "objective_complete"


class EventKind(str, Enum):
    GROUP_CREATED = "group.created"
    TEAM_CREATED = "team.created"
    TASK_SUBMITTED = "task.submitted"
    HANDOFF_RECORDED = "handoff.recorded"
    AUTHORITY_UPDATED = "authority.updated"
    RUNNER_TEXT_DELTA = "runner.text_delta"
    RUNNER_COMPLETED = "runner.completed"
