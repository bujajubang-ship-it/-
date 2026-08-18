import json
import asyncio
import time
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from strategy_brain import BrainSettings, StrategyBrain, StrategyMode
from strategy_brain.contracts import EvidenceEnvelope
from strategy_brain.modes import MODE_REGISTRY
from strategy_brain.providers import OpenAIResponsesProvider
from strategy_brain.tools import ReadOnlyToolRegistry, ToolDefinition


class FakeResponses:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return self._responses.pop(0)


class FakeClient:
    def __init__(self, responses):
        self.responses = FakeResponses(responses)


class BrainSettingsTests(unittest.TestCase):
    def test_requested_openai_defaults_are_configurable(self):
        with patch.dict(os.environ, {}, clear=True):
            settings = BrainSettings.from_env()
        self.assertEqual(settings.provider, "openai")
        self.assertEqual(settings.fallback_provider, "anthropic")
        self.assertEqual(settings.openai_model, "gpt-5.6-sol")
        self.assertEqual(settings.reasoning_effort, "max")
        self.assertFalse(settings.store_responses)

    def test_all_inventory_modes_are_registered(self):
        expected = {mode for mode in StrategyMode}
        self.assertEqual(set(MODE_REGISTRY), expected)


class ToolRegistryTests(unittest.IsolatedAsyncioTestCase):
    async def test_tool_result_has_evidence_metadata(self):
        registry = ReadOnlyToolRegistry()
        registry.register(
            ToolDefinition(
                name="get_recent_videos",
                description="Get recent videos.",
                parameters={
                    "type": "object",
                    "properties": {},
                    "required": [],
                    "additionalProperties": False,
                },
                handler=lambda _: EvidenceEnvelope(
                    data=[{"title": "example"}],
                    source="youtube_analytics",
                    collected_at="2026-08-17T00:00:00Z",
                    sample_size=1,
                ),
            )
        )
        result = await registry.execute("get_recent_videos", {})
        self.assertEqual(result["source"], "youtube_analytics")
        self.assertEqual(result["sample_size"], 1)

    async def test_write_tool_is_rejected(self):
        registry = ReadOnlyToolRegistry()
        with self.assertRaises(ValueError):
            registry.register(
                ToolDefinition(
                    name="delete_content",
                    description="Not allowed.",
                    parameters={"type": "object", "properties": {}},
                    handler=lambda _: None,
                    read_only=False,
                )
            )

    async def test_non_strict_tool_schema_is_rejected(self):
        registry = ReadOnlyToolRegistry()
        with self.assertRaises(ValueError):
            registry.register(
                ToolDefinition(
                    name="get_recent_videos",
                    description="Invalid schema.",
                    parameters={"type": "object", "properties": {}},
                    handler=lambda _: None,
                )
            )


class OpenAIProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_structured_request_uses_model_effort_and_schema(self):
        response = SimpleNamespace(
            id="resp_1",
            output=[],
            output_text='{"answer":"ok"}',
            usage={"input_tokens": 10, "output_tokens": 2},
        )
        client = FakeClient([response])
        settings = BrainSettings(provider="openai")
        provider = OpenAIResponsesProvider(settings=settings, client=client)
        brain = StrategyBrain(provider)
        schema = {
            "type": "object",
            "properties": {"answer": {"type": "string"}},
            "required": ["answer"],
            "additionalProperties": False,
        }
        request = brain.build_request(
            StrategyMode.PLANNING,
            "다음 영상을 기획해줘",
            "근거 있는 기획을 만든다.",
            output_schema=schema,
        )
        result = await brain.run(request)

        call = client.responses.calls[0]
        self.assertEqual(call["model"], "gpt-5.6-sol")
        self.assertEqual(call["reasoning"], {"effort": "max", "context": "all_turns"})
        self.assertFalse(call["store"])
        self.assertTrue(call["text"]["format"]["strict"])
        self.assertEqual(result.parsed, {"answer": "ok"})

    async def test_function_call_is_executed_and_returned_to_model(self):
        call = SimpleNamespace(
            type="function_call",
            name="get_recent_videos",
            arguments="{}",
            call_id="call_1",
            model_dump=lambda exclude_none=True: {
                "type": "function_call",
                "name": "get_recent_videos",
                "arguments": "{}",
                "call_id": "call_1",
            },
        )
        first = SimpleNamespace(id="resp_1", output=[call], output_text="", usage={})
        second = SimpleNamespace(
            id="resp_2", output=[], output_text="분석 완료", usage={}
        )
        client = FakeClient([first, second])
        provider = OpenAIResponsesProvider(
            settings=BrainSettings(provider="openai"), client=client
        )

        registry = ReadOnlyToolRegistry()
        registry.register(
            ToolDefinition(
                name="get_recent_videos",
                description="Get recent videos.",
                parameters={
                    "type": "object",
                    "properties": {},
                    "required": [],
                    "additionalProperties": False,
                },
                handler=lambda _: EvidenceEnvelope(
                    data=[{"views": 100}], source="youtube_analytics", sample_size=1
                ),
            )
        )
        brain = StrategyBrain(provider, registry)
        request = brain.build_request(
            StrategyMode.STRATEGY_CHAT,
            "최근 성과를 봐줘",
            "필요한 성과를 조회한다.",
        )
        result = await brain.run(request)

        self.assertEqual(result.text, "분석 완료")
        self.assertEqual(result.tool_calls, 1)
        second_input = client.responses.calls[1]["input"]
        tool_output = next(x for x in second_input if x.get("type") == "function_call_output")
        decoded = json.loads(tool_output["output"])
        self.assertEqual(decoded["source"], "youtube_analytics")

    async def test_parallel_function_calls_execute_concurrently(self):
        calls = [
            SimpleNamespace(
                type="function_call",
                name=name,
                arguments="{}",
                call_id=f"call_{index}",
                model_dump=lambda exclude_none=True, name=name, index=index: {
                    "type": "function_call",
                    "name": name,
                    "arguments": "{}",
                    "call_id": f"call_{index}",
                },
            )
            for index, name in enumerate(("one", "two"), start=1)
        ]
        client = FakeClient([
            SimpleNamespace(id="r1", output=calls, output_text="", usage={}),
            SimpleNamespace(id="r2", output=[], output_text="done", usage={}),
        ])
        registry = ReadOnlyToolRegistry()

        async def slow(_args):
            await asyncio.sleep(0.05)
            return EvidenceEnvelope(data=True, source="test")

        for name in ("one", "two"):
            registry.register(
                ToolDefinition(
                    name=name,
                    description=name,
                    parameters={
                        "type": "object",
                        "properties": {},
                        "required": [],
                        "additionalProperties": False,
                    },
                    handler=slow,
                )
            )
        provider = OpenAIResponsesProvider(
            settings=BrainSettings(provider="openai"), client=client
        )
        request = SimpleNamespace(
            mode=StrategyMode.STRATEGY_CHAT,
            reasoning_effort="high",
            instructions="test",
            input="test",
            tools=[],
            output_schema=None,
            output_schema_name=None,
            metadata={},
        )
        started = time.perf_counter()
        result = await provider.generate(request, registry.execute)
        elapsed = time.perf_counter() - started

        self.assertEqual(result.text, "done")
        self.assertLess(elapsed, 0.09)

    async def test_strategy_chat_reserves_reasoning_and_visible_output_budget(self):
        provider = OpenAIResponsesProvider(
            settings=BrainSettings(provider="openai"),
            client=SimpleNamespace(responses=SimpleNamespace()),
        )
        request = SimpleNamespace(
            mode=StrategyMode.STRATEGY_CHAT,
            reasoning_effort="medium",
            instructions="test",
            tools=[],
            output_schema=None,
            output_schema_name=None,
            metadata={},
        )
        kwargs = provider._request_kwargs(request, "test")
        self.assertEqual(kwargs["reasoning"]["effort"], "medium")
        self.assertEqual(kwargs["max_output_tokens"], 12000)
        self.assertEqual(kwargs["text"]["verbosity"], "low")


if __name__ == "__main__":
    unittest.main()
