from __future__ import annotations

from typing import Any, AsyncIterator

from .contracts import BrainProvider, BrainRequest, BrainResult, StrategyMode
from .modes import build_instructions, get_mode_spec
from .tools import ReadOnlyToolRegistry


class StrategyBrain:
    def __init__(
        self,
        provider: BrainProvider,
        tools: ReadOnlyToolRegistry | None = None,
    ) -> None:
        self.provider = provider
        self.tools = tools or ReadOnlyToolRegistry()

    def build_request(
        self,
        mode: StrategyMode,
        task_input: str | list[dict[str, Any]],
        task_instructions: str,
        *,
        output_schema: dict[str, Any] | None = None,
        output_schema_name: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> BrainRequest:
        spec = get_mode_spec(mode)
        if spec.structured_output and output_schema is None:
            raise ValueError(f"Mode {mode.value} requires an output schema.")
        return BrainRequest(
            mode=mode,
            instructions=build_instructions(mode, task_instructions),
            input=task_input,
            tools=self.tools.definitions_for(spec.allowed_tools),
            output_schema=output_schema,
            output_schema_name=output_schema_name,
            metadata=metadata or {},
        )

    async def run(self, request: BrainRequest) -> BrainResult:
        return await self.provider.generate(request, self.tools.execute)

    async def stream(self, request: BrainRequest) -> AsyncIterator[str]:
        async for delta in self.provider.stream(request, self.tools.execute):
            yield delta
