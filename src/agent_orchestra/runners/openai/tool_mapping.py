from __future__ import annotations

from agent_orchestra.contracts.runner import ToolDefinition


def map_tools_to_openai(tools: tuple[ToolDefinition, ...]) -> list[dict[str, object]]:
    mapped: list[dict[str, object]] = []
    for tool in tools:
        mapped.append(
            {
                "type": "function",
                "name": tool.name,
                "description": tool.description,
                "parameters": dict(tool.input_schema),
            }
        )
    return mapped
