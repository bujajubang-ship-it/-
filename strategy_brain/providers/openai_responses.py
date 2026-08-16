from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any, AsyncIterator

from ..config import BrainSettings
from ..contracts import BrainRequest, BrainResult, EvidenceEnvelope, ToolExecutor


def _dump_output_item(item: Any) -> dict[str, Any]:
    if isinstance(item, dict):
        return item
    if hasattr(item, "model_dump"):
        return item.model_dump(exclude_none=True)
    raise TypeError(f"Unsupported Responses output item: {type(item)!r}")


def _usage_dict(response: Any) -> dict[str, Any]:
    usage = getattr(response, "usage", None)
    if usage is None:
        return {}
    if hasattr(usage, "model_dump"):
        return usage.model_dump(exclude_none=True)
    if isinstance(usage, dict):
        return usage
    return {}


class OpenAIResponsesProvider:
    """Responses API adapter with a bounded read-only function-call loop.

    It is not connected to any production endpoint yet.  A client can be
    injected for contract tests, so importing this module does not require the
    OpenAI SDK until the provider is actually instantiated.
    """

    def __init__(self, settings: BrainSettings | None = None, client: Any = None):
        self.settings = settings or BrainSettings.from_env()
        if client is None:
            try:
                from openai import AsyncOpenAI
            except ImportError as exc:
                raise RuntimeError(
                    "The OpenAI SDK is required to enable the OpenAI strategy provider."
                ) from exc
            client = AsyncOpenAI()
        self.client = client

    def _request_kwargs(
        self, request: BrainRequest, input_items: str | list[dict[str, Any]]
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": self.settings.openai_model,
            "reasoning": {"effort": self.settings.reasoning_effort},
            "instructions": request.instructions,
            "input": input_items,
            "store": self.settings.store_responses,
        }
        if request.tools:
            kwargs["tools"] = request.tools
        if request.output_schema is not None:
            kwargs["text"] = {
                "format": {
                    "type": "json_schema",
                    "name": request.output_schema_name or f"{request.mode.value}_result",
                    "strict": True,
                    "schema": request.output_schema,
                }
            }
        if request.metadata:
            kwargs["metadata"] = request.metadata
        return kwargs

    @staticmethod
    def _normalise_input(value: str | list[dict[str, Any]]) -> list[dict[str, Any]]:
        if isinstance(value, str):
            return [{"role": "user", "content": value}]
        return list(value)

    @staticmethod
    def _function_calls(response: Any) -> list[Any]:
        return [
            item
            for item in getattr(response, "output", [])
            if getattr(item, "type", None) == "function_call"
        ]

    @staticmethod
    async def _tool_output(call: Any, executor: ToolExecutor) -> dict[str, Any]:
        try:
            arguments = json.loads(call.arguments or "{}")
            if not isinstance(arguments, dict):
                raise ValueError("Tool arguments must be a JSON object.")
            result = await executor(call.name, arguments)
            if isinstance(result, EvidenceEnvelope):
                result = asdict(result)
            output = json.dumps(result, ensure_ascii=False, default=str)
        except Exception as exc:
            output = json.dumps(
                {
                    "data": None,
                    "source": f"tool:{getattr(call, 'name', 'unknown')}",
                    "unavailable_reason": str(exc),
                },
                ensure_ascii=False,
            )
        return {
            "type": "function_call_output",
            "call_id": call.call_id,
            "output": output,
        }

    async def generate(
        self, request: BrainRequest, tool_executor: ToolExecutor | None = None
    ) -> BrainResult:
        input_items = self._normalise_input(request.input)
        total_tool_calls = 0

        for _ in range(self.settings.max_tool_rounds):
            response = await self.client.responses.create(
                **self._request_kwargs(request, input_items)
            )
            calls = self._function_calls(response)
            if not calls:
                status = getattr(response, "status", "completed")
                if status not in (None, "completed"):
                    raise RuntimeError(f"OpenAI response did not complete: {status}")
                text = getattr(response, "output_text", "") or ""
                if request.output_schema is not None and not text:
                    raise RuntimeError("Structured OpenAI response contained no output text.")
                parsed = json.loads(text) if request.output_schema is not None else None
                return BrainResult(
                    text=text,
                    parsed=parsed,
                    response_id=getattr(response, "id", None),
                    usage=_usage_dict(response),
                    tool_calls=total_tool_calls,
                    raw_response=response,
                )
            if tool_executor is None:
                raise RuntimeError("The model requested a tool but no tool executor was supplied.")

            total_tool_calls += len(calls)
            input_items.extend(_dump_output_item(item) for item in response.output)
            for call in calls:
                input_items.append(await self._tool_output(call, tool_executor))

        raise RuntimeError(
            f"Strategy Brain exceeded {self.settings.max_tool_rounds} tool rounds."
        )

    async def stream(
        self, request: BrainRequest, tool_executor: ToolExecutor | None = None
    ) -> AsyncIterator[str]:
        """Stream visible text while preserving the same bounded tool loop."""

        input_items = self._normalise_input(request.input)
        for _ in range(self.settings.max_tool_rounds):
            stream = await self.client.responses.create(
                **self._request_kwargs(request, input_items), stream=True
            )
            completed = None
            async for event in stream:
                event_type = getattr(event, "type", "")
                if event_type == "response.output_text.delta":
                    delta = getattr(event, "delta", "")
                    if delta:
                        yield delta
                elif event_type == "response.completed":
                    completed = getattr(event, "response", None)

            if completed is None:
                raise RuntimeError("OpenAI stream ended without response.completed.")
            calls = self._function_calls(completed)
            if not calls:
                return
            if tool_executor is None:
                raise RuntimeError("The model requested a tool but no tool executor was supplied.")
            input_items.extend(_dump_output_item(item) for item in completed.output)
            for call in calls:
                input_items.append(await self._tool_output(call, tool_executor))

        raise RuntimeError(
            f"Strategy Brain exceeded {self.settings.max_tool_rounds} tool rounds."
        )
