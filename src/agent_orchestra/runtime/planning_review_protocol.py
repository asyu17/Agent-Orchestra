from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


def _required_text_field(mapping: dict[str, Any], field: str, *, location: str) -> str:
    value = mapping.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{location}.{field} must be a non-empty string")
    return value.strip()


def _optional_text_field(mapping: dict[str, Any], field: str) -> str:
    value = mapping.get(field)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string when provided")
    return value.strip()


def _optional_str_tuple(mapping: dict[str, Any], field: str, *, location: str) -> tuple[str, ...]:
    value = mapping.get(field)
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError(f"{location}.{field} must be a list of strings")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"{location}.{field} must contain non-empty strings")
        result.append(item.strip())
    return tuple(result)


@dataclass(slots=True)
class LeaderPeerReviewItem:
    target_leader_id: str
    target_team_id: str
    summary: str
    conflict_type: str
    severity: str
    affected_paths: tuple[str, ...] = ()
    affected_project_items: tuple[str, ...] = ()
    reason: str = ""
    suggested_change: str = ""
    requires_superleader_attention: bool = False
    full_text_ref: str | None = None


@dataclass(slots=True)
class LeaderPeerReviewOutput:
    summary: str
    reviews: tuple[LeaderPeerReviewItem, ...]


def parse_leader_peer_review_output(payload: str | dict[str, Any]) -> LeaderPeerReviewOutput:
    if isinstance(payload, str):
        try:
            raw_payload = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ValueError("Leader peer review output is not valid JSON") from exc
    elif isinstance(payload, dict):
        raw_payload = payload
    else:
        raise ValueError("Leader peer review output payload must be a JSON string or object")

    if not isinstance(raw_payload, dict):
        raise ValueError("Leader peer review output payload must be an object")

    summary = _required_text_field(raw_payload, "summary", location="leader peer review output")
    reviews_raw = raw_payload.get("reviews")
    if not isinstance(reviews_raw, list):
        raise ValueError("leader peer review output.reviews must be a list")

    reviews: list[LeaderPeerReviewItem] = []
    for index, raw_review in enumerate(reviews_raw):
        location = f"reviews[{index}]"
        if not isinstance(raw_review, dict):
            raise ValueError(f"{location} must be an object")
        target_leader_id = _required_text_field(raw_review, "target_leader_id", location=location)
        target_team_id = _required_text_field(raw_review, "target_team_id", location=location)
        conflict_type = _required_text_field(raw_review, "conflict_type", location=location)
        severity = _required_text_field(raw_review, "severity", location=location)
        review_summary = _required_text_field(raw_review, "summary", location=location)
        reason = _optional_text_field(raw_review, "reason")
        suggested_change = _optional_text_field(raw_review, "suggested_change")
        requires_superleader_attention = bool(raw_review.get("requires_superleader_attention", False))
        full_text_ref = raw_review.get("full_text_ref")
        if full_text_ref is not None and not isinstance(full_text_ref, str):
            raise ValueError(f"{location}.full_text_ref must be a string when provided")
        reviews.append(
            LeaderPeerReviewItem(
                target_leader_id=target_leader_id,
                target_team_id=target_team_id,
                summary=review_summary,
                conflict_type=conflict_type,
                severity=severity,
                affected_paths=_optional_str_tuple(raw_review, "affected_paths", location=location),
                affected_project_items=_optional_str_tuple(
                    raw_review,
                    "affected_project_items",
                    location=location,
                ),
                reason=reason,
                suggested_change=suggested_change,
                requires_superleader_attention=requires_superleader_attention,
                full_text_ref=full_text_ref.strip() if isinstance(full_text_ref, str) else None,
            )
        )
    return LeaderPeerReviewOutput(summary=summary, reviews=tuple(reviews))

