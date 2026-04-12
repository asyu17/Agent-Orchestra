from __future__ import annotations

from agent_orchestra.bus.base import EventBus
from agent_orchestra.contracts.events import OrchestraEvent


class InMemoryEventBus(EventBus):
    def __init__(self) -> None:
        self.published_events: list[OrchestraEvent] = []

    async def publish(self, event: OrchestraEvent) -> None:
        self.published_events.append(event)
