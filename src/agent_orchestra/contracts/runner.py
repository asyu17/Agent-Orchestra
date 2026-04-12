from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from typing import Any

from agent_orchestra.contracts.enums import EventKind


@dataclass(slots=True)
class ToolDefinition:
    name: str
    description: str
    input_schema: Mapping[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class RunnerTurnRequest:
    agent_id: str
    instructions: str
    input_text: str
    conversation: tuple[Mapping[str, Any], ...] = ()
    tools: tuple[ToolDefinition, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)
    previous_response_id: str | None = None


@dataclass(slots=True)
class RunnerStreamEvent:
    kind: EventKind
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class RunnerTurnResult:
    response_id: str | None
    output_text: str
    status: str
    usage: dict[str, Any] = field(default_factory=dict)
    raw_payload: dict[str, Any] = field(default_factory=dict)
    protocol_events: tuple[dict[str, Any], ...] = ()
    final_report: dict[str, Any] | None = None


@dataclass(slots=True)
class RunnerHealth:
    healthy: bool
    provider: str
    detail: str = ""


class AgentRunner(ABC):
    @abstractmethod
    async def run_turn(self, request: RunnerTurnRequest) -> RunnerTurnResult:
        raise NotImplementedError

    @abstractmethod
    async def stream_turn(self, request: RunnerTurnRequest) -> AsyncIterator[RunnerStreamEvent]:
        raise NotImplementedError

    @abstractmethod
    async def cancel(self, run_id: str) -> None:
        raise NotImplementedError

    @abstractmethod
    async def healthcheck(self) -> RunnerHealth:
        raise NotImplementedError
