from __future__ import annotations

from abc import ABC, abstractmethod

from agent_orchestra.contracts.events import OrchestraEvent


class EventBus(ABC):
    @abstractmethod
    async def publish(self, event: OrchestraEvent) -> None:
        raise NotImplementedError
