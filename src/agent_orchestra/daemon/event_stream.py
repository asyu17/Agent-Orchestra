from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from uuid import uuid4


@dataclass(slots=True)
class EventSubscription:
    subscription_id: str
    work_session_id: str | None
    queue: asyncio.Queue[dict[str, Any] | None]


class EventStreamHub:
    def __init__(self, *, replay_limit: int = 256) -> None:
        self._subscriptions: dict[str, EventSubscription] = {}
        self._recent_events: deque[dict[str, Any]] = deque(maxlen=max(replay_limit, 0))

    def subscribe(self, *, work_session_id: str | None = None) -> EventSubscription:
        subscription_id = f"sub-{uuid4().hex}"
        normalized = work_session_id.strip() if isinstance(work_session_id, str) else None
        if normalized == "":
            normalized = None
        buffered_events = [
            dict(event)
            for event in self._recent_events
            if normalized is None
            or str(event.get("work_session_id") or "").strip() == normalized
        ]
        subscription = EventSubscription(
            subscription_id=subscription_id,
            work_session_id=normalized,
            queue=asyncio.Queue(),
        )
        self._subscriptions[subscription_id] = subscription
        for event in buffered_events:
            subscription.queue.put_nowait(event)
        return subscription

    def unsubscribe(self, subscription_id: str) -> None:
        subscription = self._subscriptions.pop(subscription_id, None)
        if subscription is None:
            return
        subscription.queue.put_nowait(None)

    def publish(self, event: Mapping[str, Any], *, replay: bool = True) -> int:
        payload = {str(key): value for key, value in event.items()}
        if replay and self._recent_events.maxlen != 0:
            self._recent_events.append(dict(payload))
        work_session_id = payload.get("work_session_id")
        delivered = 0
        for subscription in tuple(self._subscriptions.values()):
            if (
                subscription.work_session_id is not None
                and str(work_session_id or "").strip() != subscription.work_session_id
            ):
                continue
            try:
                subscription.queue.put_nowait(dict(payload))
                delivered += 1
            except asyncio.QueueFull:
                continue
        return delivered

    def close(self) -> None:
        for subscription in tuple(self._subscriptions.values()):
            subscription.queue.put_nowait(None)
        self._subscriptions.clear()
        self._recent_events.clear()
