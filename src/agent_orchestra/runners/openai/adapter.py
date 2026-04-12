from __future__ import annotations

import inspect
from collections.abc import AsyncIterator
from typing import Any

from agent_orchestra.contracts.enums import EventKind
from agent_orchestra.contracts.runner import (
    AgentRunner,
    RunnerHealth,
    RunnerStreamEvent,
    RunnerTurnRequest,
    RunnerTurnResult,
)
from agent_orchestra.runners.openai.tool_mapping import map_tools_to_openai


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _coerce_raw_payload(response: Any) -> dict[str, Any]:
    if isinstance(response, dict):
        return dict(response)
    payload: dict[str, Any] = {}
    for key in ("id", "output_text", "status", "usage"):
        if hasattr(response, key):
            payload[key] = getattr(response, key)
    return payload


class OpenAIResponsesAgentRunner(AgentRunner):
    def __init__(self, *, client: Any | None = None, model: str = "gpt-5-mini") -> None:
        self._client = client
        self.model = model
        self._cancelled_run_ids: set[str] = set()

    async def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            from openai import AsyncOpenAI  # type: ignore
        except ImportError as exc:
            raise RuntimeError("openai is required for OpenAIResponsesAgentRunner. Install the 'openai' extra.") from exc
        self._client = AsyncOpenAI()
        return self._client

    def build_request_payload(self, request: RunnerTurnRequest) -> dict[str, object]:
        input_items = list(request.conversation)
        input_items.append({"role": "user", "content": request.input_text})
        payload: dict[str, object] = {
            "model": self.model,
            "instructions": request.instructions,
            "input": input_items,
        }
        if request.previous_response_id:
            payload["previous_response_id"] = request.previous_response_id
        if request.tools:
            payload["tools"] = map_tools_to_openai(request.tools)
        return payload

    async def run_turn(self, request: RunnerTurnRequest) -> RunnerTurnResult:
        client = await self._get_client()
        payload = self.build_request_payload(request)
        response = await _maybe_await(client.responses.create(**payload))
        raw_payload = _coerce_raw_payload(response)
        return RunnerTurnResult(
            response_id=raw_payload.get("id"),
            output_text=str(raw_payload.get("output_text", "")),
            status=str(raw_payload.get("status", "completed")),
            usage=dict(raw_payload.get("usage", {})),
            raw_payload=raw_payload,
        )

    async def stream_turn(self, request: RunnerTurnRequest) -> AsyncIterator[RunnerStreamEvent]:
        result = await self.run_turn(request)
        if result.output_text:
            yield RunnerStreamEvent(
                kind=EventKind.RUNNER_TEXT_DELTA,
                payload={"text": result.output_text, "response_id": result.response_id},
            )
        yield RunnerStreamEvent(
            kind=EventKind.RUNNER_COMPLETED,
            payload={"response_id": result.response_id, "status": result.status},
        )

    async def cancel(self, run_id: str) -> None:
        self._cancelled_run_ids.add(run_id)

    async def healthcheck(self) -> RunnerHealth:
        try:
            await self._get_client()
        except Exception as exc:  # pragma: no cover - defensive path
            return RunnerHealth(healthy=False, provider="openai", detail=str(exc))
        return RunnerHealth(healthy=bool(self.model), provider="openai", detail=f"model={self.model}")
