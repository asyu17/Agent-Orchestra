from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any


def _now() -> str:
    return datetime.now(UTC).isoformat()


class MailboxMessageKind(str, Enum):
    TEAMMATE_RESULT = "teammate_result"
    SYSTEM = "system"
    PERMISSION_REQUEST = "permission_request"
    PERMISSION_DECISION = "permission_decision"


class MailboxDeliveryMode(str, Enum):
    SUMMARY_ONLY = "summary_only"
    SUMMARY_PLUS_REF = "summary_plus_ref"
    FULL_TEXT = "full_text"


class MailboxVisibilityScope(str, Enum):
    SHARED = "shared"
    CONTROL_PRIVATE = "control-private"


@dataclass(slots=True)
class MailboxEnvelope:
    sender: str
    recipient: str
    subject: str
    envelope_id: str | None = None
    mailbox_id: str | None = None
    kind: MailboxMessageKind = MailboxMessageKind.SYSTEM
    group_id: str | None = None
    lane_id: str | None = None
    team_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    summary: str | None = None
    full_text_ref: str | None = None
    source_entry_id: str | None = None
    source_scope: str | None = None
    visibility_scope: MailboxVisibilityScope | str = MailboxVisibilityScope.CONTROL_PRIVATE
    delivery_mode: MailboxDeliveryMode = MailboxDeliveryMode.FULL_TEXT
    severity: str | None = None
    tags: tuple[str, ...] = ()
    created_at: str = field(default_factory=_now)
    acknowledged_at: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class MailboxCursor:
    recipient: str
    last_envelope_id: str | None = None
    acknowledged_ids: tuple[str, ...] = ()
    subscription_id: str | None = None


@dataclass(slots=True)
class MailboxSubscription:
    subscriber: str
    subscription_id: str | None = None
    recipient: str | None = None
    sender: str | None = None
    group_id: str | None = None
    lane_id: str | None = None
    team_id: str | None = None
    kinds: tuple[MailboxMessageKind | str, ...] = ()
    visibility_scopes: tuple[MailboxVisibilityScope | str, ...] = ()
    tags: tuple[str, ...] = ()
    delivery_mode: MailboxDeliveryMode = MailboxDeliveryMode.SUMMARY_PLUS_REF
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class MailboxDigest:
    subscription_id: str
    subscriber: str
    envelope_id: str
    sender: str
    recipient: str
    subject: str
    summary: str
    delivery_mode: MailboxDeliveryMode
    kind: MailboxMessageKind = MailboxMessageKind.SYSTEM
    mailbox_id: str | None = None
    full_text_ref: str | None = None
    group_id: str | None = None
    lane_id: str | None = None
    team_id: str | None = None
    source_entry_id: str | None = None
    source_scope: str | None = None
    visibility_scope: MailboxVisibilityScope | str | None = None
    severity: str | None = None
    created_at: str | None = None
    tags: tuple[str, ...] = ()
    payload: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


class MailboxBridge(ABC):
    @abstractmethod
    async def send(self, envelope: MailboxEnvelope) -> MailboxEnvelope:
        raise NotImplementedError

    @abstractmethod
    async def list_for_recipient(
        self,
        recipient: str,
        *,
        after_envelope_id: str | None = None,
    ) -> list[MailboxEnvelope]:
        raise NotImplementedError

    @abstractmethod
    async def acknowledge(self, recipient: str, envelope_ids: tuple[str, ...]) -> MailboxCursor:
        raise NotImplementedError

    @abstractmethod
    async def get_cursor(self, recipient: str) -> MailboxCursor:
        raise NotImplementedError

    async def list_message_pool(
        self,
        *,
        after_envelope_id: str | None = None,
    ) -> list[MailboxEnvelope]:
        raise NotImplementedError("This mailbox bridge does not expose a message pool.")

    async def ensure_subscription(self, subscription: MailboxSubscription) -> MailboxSubscription:
        raise NotImplementedError("This mailbox bridge does not support subscriptions.")

    async def list_for_subscription(
        self,
        subscriber: str,
        *,
        subscription_id: str,
        after_envelope_id: str | None = None,
    ) -> list[MailboxDigest]:
        raise NotImplementedError("This mailbox bridge does not support subscriptions.")

    async def acknowledge_subscription(
        self,
        subscriber: str,
        envelope_ids: tuple[str, ...],
        *,
        subscription_id: str,
    ) -> MailboxCursor:
        raise NotImplementedError("This mailbox bridge does not support subscriptions.")

    async def get_subscription_cursor(
        self,
        subscriber: str,
        *,
        subscription_id: str,
    ) -> MailboxCursor:
        raise NotImplementedError("This mailbox bridge does not support subscriptions.")

    async def poll_subscription(
        self,
        subscriber: str,
        *,
        subscription_id: str,
        limit: int = 100,
    ) -> tuple[MailboxDigest, ...]:
        cursor = await self.get_subscription_cursor(subscriber, subscription_id=subscription_id)
        items = await self.list_for_subscription(
            subscriber,
            subscription_id=subscription_id,
            after_envelope_id=cursor.last_envelope_id,
        )
        return tuple(items[:limit])
