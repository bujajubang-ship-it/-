from __future__ import annotations

import inspect
from dataclasses import asdict, dataclass
from typing import Any, Awaitable, Callable, Union

from ..contracts import EvidenceEnvelope


ToolValue = Union[EvidenceEnvelope, Any]
ToolHandler = Callable[[dict[str, Any]], Union[ToolValue, Awaitable[ToolValue]]]


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: ToolHandler
    read_only: bool = True

    def as_openai_tool(self) -> dict[str, Any]:
        if not self.read_only:
            raise ValueError(f"Strategy tool must be read-only: {self.name}")
        return {
            "type": "function",
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
            "strict": True,
        }


class ReadOnlyToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolDefinition] = {}

    def register(self, definition: ToolDefinition) -> None:
        if not definition.read_only:
            raise ValueError("Only read-only tools may be registered.")
        if definition.name in self._tools:
            raise ValueError(f"Duplicate strategy tool: {definition.name}")
        schema = definition.parameters
        if schema.get("type") != "object":
            raise ValueError(f"Tool parameters must be an object schema: {definition.name}")
        if schema.get("additionalProperties") is not False:
            raise ValueError(
                f"Strict tool schema must set additionalProperties=false: {definition.name}"
            )
        properties = set((schema.get("properties") or {}).keys())
        required = set(schema.get("required") or [])
        if properties != required:
            raise ValueError(
                f"Strict tool schema must require every property: {definition.name}"
            )
        self._tools[definition.name] = definition

    def definitions_for(self, allowed_names: tuple[str, ...]) -> list[dict[str, Any]]:
        return [
            self._tools[name].as_openai_tool()
            for name in allowed_names
            if name in self._tools
        ]

    async def execute(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name not in self._tools:
            return asdict(
                EvidenceEnvelope(
                    data=None,
                    source=f"tool:{name}",
                    unavailable_reason="Tool is not registered for this deployment.",
                )
            )
        result = self._tools[name].handler(arguments)
        if inspect.isawaitable(result):
            result = await result
        if isinstance(result, EvidenceEnvelope):
            return asdict(result)
        return asdict(EvidenceEnvelope(data=result, source=f"tool:{name}"))
