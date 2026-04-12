from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
from pathlib import Path
from typing import Any


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-orchestra-worker-process")
    parser.add_argument("--assignment-file", required=True)
    parser.add_argument("--result-file", required=True)
    parser.add_argument("--protocol-state-file")
    return parser


def _coerce_mapping(value: object) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    return None


def _resolve_protocol_payload(assignment: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    metadata = assignment.get("metadata", {})
    if not isinstance(metadata, dict):
        metadata = {}
    execution_contract = _coerce_mapping(assignment.get("execution_contract"))
    if execution_contract is None:
        execution_contract = _coerce_mapping(metadata.get("execution_contract"))
    lease_policy = _coerce_mapping(assignment.get("lease_policy"))
    if lease_policy is None:
        lease_policy = _coerce_mapping(metadata.get("lease_policy"))
    return execution_contract, lease_policy


def _string_mapping(value: object) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    return {str(key): item for key, item in value.items()}


def _now_timestamp() -> str:
    return datetime.now(UTC).isoformat()


def _append_protocol_event(
    *,
    protocol_state: dict[str, Any],
    assignment_id: str,
    worker_id: str,
    status: str,
    phase: str,
    kind: str,
    summary: str,
) -> None:
    events = protocol_state.setdefault("protocol_events", [])
    if not isinstance(events, list):
        events = []
        protocol_state["protocol_events"] = events
    event_id = f"{assignment_id}:{len(events) + 1}"
    events.append(
        {
            "event_id": event_id,
            "assignment_id": assignment_id,
            "worker_id": worker_id,
            "status": status,
            "phase": phase,
            "kind": kind,
            "timestamp": _now_timestamp(),
            "summary": summary,
        }
    )


def _write_protocol_state(path: Path | None, protocol_state: dict[str, Any]) -> None:
    if path is None:
        return
    path.write_text(json.dumps(protocol_state, ensure_ascii=False), encoding="utf-8")


def run_once(
    assignment_file: str,
    result_file: str,
    *,
    protocol_state_file: str | None = None,
) -> dict[str, object]:
    assignment = json.loads(Path(assignment_file).read_text(encoding="utf-8"))
    metadata = assignment.get("metadata", {})
    if not isinstance(metadata, dict):
        metadata = {}
    execution_contract, lease_policy = _resolve_protocol_payload(assignment)
    protocol_enabled = protocol_state_file is not None and (
        execution_contract is not None or lease_policy is not None
    )
    protocol_path = Path(protocol_state_file) if protocol_enabled and protocol_state_file is not None else None
    protocol_state: dict[str, Any] = {"protocol_events": []}
    if execution_contract is not None:
        protocol_state["execution_contract"] = dict(execution_contract)
    if lease_policy is not None:
        protocol_state["lease_policy"] = dict(lease_policy)
    if protocol_enabled:
        _append_protocol_event(
            protocol_state=protocol_state,
            assignment_id=assignment["assignment_id"],
            worker_id=assignment["worker_id"],
            status="accepted",
            phase="accepted",
            kind="accepted",
            summary="Worker accepted assignment.",
        )
        _append_protocol_event(
            protocol_state=protocol_state,
            assignment_id=assignment["assignment_id"],
            worker_id=assignment["worker_id"],
            status="running",
            phase="checkpoint",
            kind="checkpoint",
            summary="Worker reached execution checkpoint.",
        )
        _write_protocol_state(protocol_path, protocol_state)
    if metadata.get("fail"):
        authority_request = _string_mapping(metadata.get("authority_request"))
        if protocol_enabled:
            protocol_state["final_report"] = {
                "assignment_id": assignment["assignment_id"],
                "worker_id": assignment["worker_id"],
                "terminal_status": "blocked" if authority_request is not None else "failed",
                "summary": metadata.get("error_text", "simulated failure"),
                "artifact_refs": [],
                "verification_results": [],
                "retry_hint": "check worker metadata and rerun assignment",
                **({"authority_request": authority_request} if authority_request is not None else {}),
            }
            _write_protocol_state(protocol_path, protocol_state)
        result = {
            "worker_id": assignment["worker_id"],
            "assignment_id": assignment["assignment_id"],
            "status": "failed",
            "output_text": "",
            "error_text": metadata.get("error_text", "simulated failure"),
            "response_id": f"resp-{assignment['assignment_id']}",
            "usage": {},
            "raw_payload": {"backend": assignment.get("backend"), "role": assignment.get("role")},
        }
    else:
        if protocol_enabled:
            protocol_state["final_report"] = {
                "assignment_id": assignment["assignment_id"],
                "worker_id": assignment["worker_id"],
                "terminal_status": "completed",
                "summary": metadata.get(
                    "simulated_output",
                    f"worker:{assignment['worker_id']} completed assignment:{assignment['assignment_id']}",
                ),
                "artifact_refs": [],
                "verification_results": [],
            }
            _write_protocol_state(protocol_path, protocol_state)
        result = {
            "worker_id": assignment["worker_id"],
            "assignment_id": assignment["assignment_id"],
            "status": "completed",
            "output_text": metadata.get(
                "simulated_output",
                f"worker:{assignment['worker_id']} completed assignment:{assignment['assignment_id']}",
            ),
            "error_text": "",
            "response_id": f"resp-{assignment['assignment_id']}",
            "usage": {},
            "raw_payload": {"backend": assignment.get("backend"), "role": assignment.get("role")},
        }
    if protocol_enabled:
        result["raw_payload"]["protocol_events"] = tuple(protocol_state.get("protocol_events", []))
        result["raw_payload"]["final_report"] = dict(protocol_state.get("final_report", {}))
    Path(result_file).write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    return result


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    run_once(
        args.assignment_file,
        args.result_file,
        protocol_state_file=args.protocol_state_file,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
