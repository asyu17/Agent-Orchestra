from __future__ import annotations

from agent_orchestra.contracts.handoff import HandoffRecord


def build_handoff_contract(record: HandoffRecord) -> dict[str, object]:
    return {
        "handoff_id": record.handoff_id,
        "from_team_id": record.from_team_id,
        "to_team_id": record.to_team_id,
        "task_id": record.task_id,
        "artifacts": list(record.artifact_refs),
        "summary": record.summary,
    }
