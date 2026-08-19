from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from typing import Any, AsyncIterator

from ..config import BrainSettings
from ..contracts import BrainRequest, BrainResult, EvidenceEnvelope, ToolExecutor


_PROMPT_CACHE_REQUEST_KEYS = frozenset(
    {"prompt_cache_key", "prompt_cache_options", "prompt_cache_retention", "ttl"}
)


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

    A client can be injected for contract tests, so importing this module does
    not require the OpenAI SDK until the provider is actually instantiated.
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
            client = AsyncOpenAI(timeout=240.0, max_retries=1)
        self.client = client

    def _request_kwargs(
        self, request: BrainRequest, input_items: str | list[dict[str, Any]]
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": self.settings.openai_model,
            "reasoning": {
                "effort": request.reasoning_effort or self.settings.reasoning_effort,
                "context": "all_turns",
            },
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
        if request.mode.value == "channel_analysis":
            # The channel button is an interactive flow. This is ample for the
            # strict report schema while preventing an unexpectedly long answer.
            kwargs["max_output_tokens"] = 8000
        elif request.mode.value == "strategy_chat":
            # Reasoning tokens count toward this limit.  A rich evidence bundle
            # can otherwise consume the full budget before visible text starts.
            kwargs["max_output_tokens"] = 12000
        kwargs["text"] = {
            **kwargs.get("text", {}),
            "verbosity": (
                "high"
                if request.mode.value in {"planning", "midform_planning", "shortform_planning", "worksheet", "postmortem"}
                else "low" if request.mode.value == "strategy_chat" else "medium"
            ),
        }
        return kwargs

    @staticmethod
    def _sanitize_request_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
        """Keep explicit prompt-cache controls out of every Responses call.

        The OpenAI SDK exposes cache arguments and merges ``extra_body`` after
        normal parameters.  This application relies on implicit caching, so
        neither surface is allowed to add model-specific cache controls.
        """

        sanitized = {
            key: value
            for key, value in kwargs.items()
            if key not in _PROMPT_CACHE_REQUEST_KEYS
        }
        extra_body = sanitized.get("extra_body")
        if isinstance(extra_body, dict):
            def strip_cache_fields(value: Any) -> Any:
                if isinstance(value, dict):
                    return {
                        key: strip_cache_fields(item)
                        for key, item in value.items()
                        if key not in _PROMPT_CACHE_REQUEST_KEYS
                        and not key.startswith("prompt_cache_")
                    }
                if isinstance(value, list):
                    return [strip_cache_fields(item) for item in value]
                return value

            cleaned_extra = strip_cache_fields(extra_body)
            if cleaned_extra:
                sanitized["extra_body"] = cleaned_extra
            else:
                sanitized.pop("extra_body", None)
        return sanitized

    def _final_request_kwargs(
        self,
        request: BrainRequest,
        input_items: str | list[dict[str, Any]],
        *,
        stream: bool = False,
    ) -> dict[str, Any]:
        """Build the exact kwargs passed to ``responses.create``.

        This is deliberately the last merge point before the SDK call so a
        wrapper, test hook, or future request builder cannot reintroduce cache
        fields after sanitization.
        """

        kwargs = self._request_kwargs(request, input_items)
        if stream:
            kwargs["stream"] = True
        return self._sanitize_request_kwargs(kwargs)

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
            safe_error = f"{type(exc).__name__}: {str(exc)[:240]}"
            output = json.dumps(
                {
                    "data": None,
                    "source": f"tool:{getattr(call, 'name', 'unknown')}",
                    "unavailable_reason": safe_error,
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
            request_kwargs = self._final_request_kwargs(request, input_items)
            response = await self.client.responses.create(**request_kwargs)
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
            input_items.extend(
                await asyncio.gather(
                    *(self._tool_output(call, tool_executor) for call in calls)
                )
            )

        raise RuntimeError(
            f"Strategy Brain exceeded {self.settings.max_tool_rounds} tool rounds."
        )

    async def stream(
        self, request: BrainRequest, tool_executor: ToolExecutor | None = None
    ) -> AsyncIterator[str]:
        """Stream visible text while preserving the same bounded tool loop."""

        input_items = self._normalise_input(request.input)
        for _ in range(self.settings.max_tool_rounds):
            request_kwargs = self._final_request_kwargs(
                request, input_items, stream=True
            )
            stream = await self.client.responses.create(**request_kwargs)
            completed = None
            visible_parts: list[str] = []
            async for event in stream:
                event_type = getattr(event, "type", "")
                if event_type == "response.output_text.delta":
                    delta = getattr(event, "delta", "")
                    if delta:
                        visible_parts.append(delta)
                        yield delta
                elif event_type in {
                    "response.completed",
                    "response.incomplete",
                    "response.failed",
                }:
                    completed = getattr(event, "response", None)

            if completed is None:
                raise RuntimeError("OpenAI stream ended without response.completed.")
            status = getattr(completed, "status", None)
            if status not in (None, "completed"):
                details = getattr(completed, "incomplete_details", None)
                reason = getattr(details, "reason", None) or status
                raise RuntimeError(f"OpenAI response did not complete: {reason}")
            # Some transports may coalesce the terminal response without delta
            # events. Preserve streaming semantics while avoiding a blank answer.
            if not visible_parts:
                final_text = getattr(completed, "output_text", "") or ""
                if final_text:
                    yield final_text
            calls = self._function_calls(completed)
            if not calls:
                return
            if tool_executor is None:
                raise RuntimeError("The model requested a tool but no tool executor was supplied.")
            input_items.extend(_dump_output_item(item) for item in completed.output)
            input_items.extend(
                await asyncio.gather(
                    *(self._tool_output(call, tool_executor) for call in calls)
                )
            )

        raise RuntimeError(
            f"Strategy Brain exceeded {self.settings.max_tool_rounds} tool rounds."
        )
