"""Candidate-editable tool registry whose commands are executed by the injected Mini environment."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ToolRegistration:
    schema: dict[str, Any]
    build_command: Callable[[Mapping[str, Any]], str]
    smoke_arguments: dict[str, Any]


def _build_bash_command(arguments: Mapping[str, Any]) -> str:
    return arguments["command"]


BASH_TOOL = {
    "type": "function",
    "function": {
        "name": "bash",
        "description": "Execute a bash command",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The bash command to execute",
                },
            },
            "required": ["command"],
        },
    },
}


TOOL_REGISTRY: dict[str, ToolRegistration] = {
    "bash": ToolRegistration(
        schema=BASH_TOOL,
        build_command=_build_bash_command,
        smoke_arguments={"command": "true"},
    ),
}


def get_tool_schemas() -> list[dict[str, Any]]:
    return [registration.schema for registration in TOOL_REGISTRY.values()]


def build_tool_action(tool_name: str, arguments: Mapping[str, Any], tool_call_id: str) -> dict[str, Any]:
    registration = TOOL_REGISTRY.get(tool_name)
    if registration is None:
        raise ValueError(f"Unknown tool '{tool_name}'")
    if not isinstance(arguments, Mapping):
        raise ValueError(f"Tool '{tool_name}' arguments must be an object")
    command = registration.build_command(arguments)
    if tool_name != "bash" and (not isinstance(command, str) or not command):
        raise ValueError(f"Tool '{tool_name}' must build a non-empty shell command")
    if tool_name == "bash":
        return {"command": command, "tool_call_id": tool_call_id}
    return {
        "command": command,
        "tool_call_id": tool_call_id,
        "tool_name": tool_name,
        "tool_arguments": dict(arguments),
    }


__all__ = [
    "BASH_TOOL",
    "TOOL_REGISTRY",
    "ToolRegistration",
    "build_tool_action",
    "get_tool_schemas",
]
