from __future__ import annotations

import sys
from pathlib import Path
from unittest import IsolatedAsyncioTestCase

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agent_orchestra.contracts.runner import RunnerTurnRequest, ToolDefinition
from agent_orchestra.runners.openai.adapter import OpenAIResponsesAgentRunner


class _FakeResponsesAPI:
    def __init__(self) -> None:
        self.last_payload: dict[str, object] | None = None

    async def create(self, **payload: object) -> dict[str, object]:
        self.last_payload = payload
        return {
            "id": "resp_001",
            "output_text": "hello from fake client",
            "status": "completed",
            "usage": {"input_tokens": 11, "output_tokens": 7},
        }


class _FakeClient:
    def __init__(self) -> None:
        self.responses = _FakeResponsesAPI()


class OpenAIRunnerTest(IsolatedAsyncioTestCase):
    async def test_build_request_payload_preserves_previous_response_id(self) -> None:
        runner = OpenAIResponsesAgentRunner(client=_FakeClient(), model="gpt-5")
        request = RunnerTurnRequest(
            agent_id="agent-1",
            instructions="You are a test agent.",
            input_text="hello",
            previous_response_id="resp_123",
            tools=(
                ToolDefinition(
                    name="store_note",
                    description="Store a note",
                    input_schema={"type": "object", "properties": {"text": {"type": "string"}}},
                ),
            ),
        )

        payload = runner.build_request_payload(request)

        self.assertEqual(payload["previous_response_id"], "resp_123")
        self.assertEqual(payload["model"], "gpt-5")
        self.assertEqual(payload["tools"][0]["name"], "store_note")

    async def test_run_turn_normalizes_fake_response(self) -> None:
        client = _FakeClient()
        runner = OpenAIResponsesAgentRunner(client=client, model="gpt-5")
        request = RunnerTurnRequest(
            agent_id="agent-1",
            instructions="You are a test agent.",
            input_text="hello",
        )

        result = await runner.run_turn(request)

        self.assertEqual(result.response_id, "resp_001")
        self.assertEqual(result.output_text, "hello from fake client")
        self.assertEqual(client.responses.last_payload["model"], "gpt-5")
