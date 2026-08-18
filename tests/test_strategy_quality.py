import asyncio
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from strategy_brain.chat_service import build_openai_input
from strategy_brain.context_builder import (
    classify_strategy_intent,
    prefetch_strategy_evidence,
)
from strategy_context import merge_strategy_revision
from strategy_memory import StrategyMemoryRepository, remember_interaction


class RecordingRegistry:
    def __init__(self, delay=0.0, unavailable=()):
        self.delay = delay
        self.unavailable = set(unavailable)
        self.calls = []
        self.trace = []

    async def execute(self, name, arguments):
        self.calls.append((name, arguments))
        if self.delay:
            await asyncio.sleep(self.delay)
        result = {
            "data": [] if name not in self.unavailable else None,
            "source": f"source:{name}",
            "sample_size": 3 if name not in self.unavailable else 0,
            "freshness": "current",
            "unavailable_reason": "empty" if name in self.unavailable else None,
        }
        self.trace.append(
            {
                "tool": name,
                "source": result["source"],
                "sample_size": result["sample_size"],
                "freshness": result["freshness"],
                "unavailable": name in self.unavailable,
            }
        )
        return result


class StrategyIntentTests(unittest.IsolatedAsyncioTestCase):
    def test_a_to_e_questions_route_to_specific_evidence_plans(self):
        cases = {
            "다음 영상 뭐 찍을까?": "next_video",
            "요즘 우리 채널 방향이 맞는 것 같아?": "channel_direction",
            "9평 베이커리 주방 동선 영상 찍으려고 하는데 어떻게 기획할까?": "topic_plan",
            "이 주제로 제목이랑 썸네일 최종 결정해줘.": "title_thumbnail",
            "최근 영상들에서 내가 놓치고 있는 공통 문제가 뭐야?": "common_problems",
        }
        for question, expected in cases.items():
            with self.subTest(question=question):
                self.assertEqual(classify_strategy_intent(question).name, expected)

    async def test_next_video_prefetch_guarantees_all_required_sources_in_parallel(self):
        registry = RecordingRegistry(delay=0.06)
        started = time.perf_counter()
        intent, evidence = await prefetch_strategy_evidence(
            "다음 영상 뭐 찍을까?", [], registry
        )
        elapsed = time.perf_counter() - started
        self.assertEqual(intent.name, "next_video")
        self.assertLess(elapsed, 0.25)
        self.assertTrue(
            {
                "get_channel_strategy_snapshot", "get_retention_patterns",
                "get_ctr_performance", "compare_title_patterns",
                "compare_thumbnail_patterns", "search_previous_plans",
                "search_feedback_history", "get_content_pipeline",
                "search_business_pt_knowledge", "get_recent_trends",
                "search_long_term_memory", "search_chat_memory",
            }.issubset(evidence)
        )

    async def test_empty_source_is_explicit_and_does_not_block_other_evidence(self):
        registry = RecordingRegistry(unavailable={"get_retention_patterns"})
        _, evidence = await prefetch_strategy_evidence(
            "최근 영상들에서 내가 놓치고 있는 공통 문제가 뭐야?", [], registry
        )
        self.assertEqual(evidence["get_retention_patterns"]["unavailable_reason"], "empty")
        self.assertEqual(evidence["get_channel_strategy_snapshot"]["sample_size"], 3)

    def test_followup_title_question_inherits_previous_topic(self):
        history = [{"role": "user", "content": "9평 베이커리 주방 동선 영상을 기획하자"}]
        intent = classify_strategy_intent("이 주제로 제목이랑 썸네일 최종 결정해줘", history)
        self.assertIn("9평", intent.query)
        self.assertIn("베이커리", intent.query)


class StrategyMemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "memory.db"

        def connect():
            connection = sqlite3.connect(self.path)
            connection.row_factory = sqlite3.Row
            return connection

        self.connect = connect
        self.repo = StrategyMemoryRepository(connect)
        self.repo.init_schema()

    def tearDown(self):
        self.temp.cleanup()

    def test_duplicate_memory_is_idempotent_and_superseded_memory_is_retained(self):
        first = self.repo.record(memory_type="decision", content="현장형 영상을 우선한다")
        duplicate = self.repo.record(memory_type="decision", content="현장형 영상을 우선한다")
        replacement = self.repo.record(memory_type="decision", content="제품형을 먼저 테스트한다")
        self.assertEqual(first, duplicate)
        self.assertTrue(self.repo.supersede(first, replacement))
        self.assertEqual([row["id"] for row in self.repo.search("", limit=10)], [replacement])
        all_rows = self.repo.search("", limit=10, include_superseded=True)
        self.assertEqual(len(all_rows), 2)
        self.assertTrue(next(row for row in all_rows if row["id"] == first)["superseded"])

    def test_only_meaningful_decision_is_promoted_from_chat(self):
        self.assertIsNone(
            remember_interaction("다음 영상 뭐 찍을까?", "추천", repository=self.repo)
        )
        self.assertIsNone(
            remember_interaction("이 주제로 제목을 확정해줘", "추천", repository=self.repo)
        )
        memory_id = remember_interaction(
            "이번에는 문제형 제목으로 확정하자",
            "좋습니다. 1순위는 좁아서 망하는 주방입니다.",
            trace=[{"source": "youtube_analytics", "sample_size": 20}],
            repository=self.repo,
        )
        self.assertIsInstance(memory_id, int)
        stored = self.repo.search("문제형", limit=5)[0]
        self.assertEqual(stored["memory_type"], "decision")
        self.assertEqual(stored["evidence"][0]["source"], "youtube_analytics")

        replacement = remember_interaction(
            "이전 결정 대신 이번에는 현장형 제목으로 바꾸기로 확정하자",
            "현장 문제를 앞세웁니다.", repository=self.repo,
        )
        active = self.repo.search("", limit=10)
        self.assertEqual(active[0]["id"], replacement)
        self.assertNotIn(memory_id, [item["id"] for item in active])


class SharedStrategyContextTests(unittest.TestCase):
    def test_title_only_revision_preserves_structure_and_updates_related_fields(self):
        existing = {
            "topic": "9평 베이커리 동선", "structure": ["현장 문제"],
            "recommended_title": "기존 제목", "thumbnail": {"text": "기존"},
            "hook": "기존 훅", "evidence": [],
        }
        generated = {
            **existing,
            "topic": "모델이 바꾼 주제", "structure": ["새 구조"],
            "recommended_title": "9평 주방, 넓히지 말고 이것부터",
            "selected_title": "9평 주방, 넓히지 말고 이것부터",
            "title_candidates": ["후보"], "content_promise": "동선을 고친다",
            "evidence": [{"source": "analytics"}],
        }
        merged = merge_strategy_revision(existing, generated, "제목만 바꿔줘")
        self.assertEqual(merged["topic"], existing["topic"])
        self.assertEqual(merged["structure"], existing["structure"])
        self.assertEqual(merged["recommended_title"], generated["recommended_title"])

    def test_chat_context_uses_recent_turns_not_entire_transcript(self):
        history = [
            {"role": "user" if index % 2 == 0 else "assistant", "content": str(index)}
            for index in range(30)
        ]
        items = build_openai_input("현재", history)
        self.assertEqual(len(items), 5)
        self.assertEqual(items[0]["content"][0]["text"], "26")

    def test_chat_context_bounds_long_generated_answers(self):
        history = [{"role": "assistant", "content": "가" * 10_000}]
        items = build_openai_input("현재", history)
        self.assertEqual(len(items[0]["content"][0]["text"]), 6_000)


if __name__ == "__main__":
    unittest.main()
