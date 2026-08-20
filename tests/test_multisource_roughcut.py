import asyncio
import json
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from edit_job_queue import EditJobQueue
from edit_pipeline import EditPipeline, PermanentEditJobError
from edit_project_store import EditProjectStore
from edit_project_store import public_project
from edit_render_service import EditRenderService
from multisource_roughcut import (
    apply_story_reasoning, apply_visual_quality, bounded_story_candidates, build_story_plan, deduplicate_segments,
    ensure_multisource, new_source, plan_transcript_chunks,
    semantic_segments, validate_timeline,
)


def database(path: Path):
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE history (id INTEGER PRIMARY KEY AUTOINCREMENT, type TEXT NOT NULL, keyword TEXT NOT NULL, report TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP)"
    )
    connection.commit()
    connection.close()
    return lambda: sqlite3.connect(path, timeout=10)


def source(filename: str, speaker: str, text: str, *, duration: float = 12) -> dict:
    item = new_source(filename=filename, speaker=speaker)
    item["status"] = "SOURCE_ANALYZED"
    item["media"] = {"duration": duration, "has_audio": True, "width": 1920, "height": 1080}
    item["duration"] = duration
    item["transcript"] = {"text": text, "segments": [{"start": 1, "end": 7, "text": text}]}
    semantic_segments(item)
    return item


class MultiSourceModelTests(unittest.TestCase):
    def test_five_sources_are_independent_and_legacy_is_preserved(self):
        project = {"project_mode": "multisource_roughcut", "sources": [
            source(f"source-{index}.mp4", f"speaker-{index}", f"서로 다른 설명 {index}")
            for index in range(5)
        ]}
        ensure_multisource(project)
        project["sources"][2]["status"] = "FAILED_ANALYSIS"
        self.assertEqual(len(project["sources"]), 5)
        self.assertTrue(all(row["segments"] for row in project["sources"] if row["status"] == "SOURCE_ANALYZED"))
        legacy = {"source": {"filename": "old.mp4", "media": {"duration": 10}}, "transcript": {"segments": []}}
        ensure_multisource(legacy)
        self.assertEqual(legacy["sources"][0]["source_id"], "legacy-source")

    def test_sixty_minute_chunk_plan_resumes_completed_chunk(self):
        item = new_source(filename="hour.mp4")
        item["media"] = {"duration": 3600, "has_audio": True}
        chunks = plan_transcript_chunks(item, chunk_seconds=600)
        self.assertEqual(len(chunks), 6)
        chunks[0]["status"] = "completed"
        chunks[0]["transcript_status"] = "completed"
        chunks[0]["transcript"] = {"text": "cached"}
        planned_again = plan_transcript_chunks(item, chunk_seconds=600)
        self.assertEqual(planned_again[0]["transcript"]["text"], "cached")
        self.assertEqual(planned_again[0]["status"], "completed")

    def test_duplicate_statement_prefers_real_user_proof(self):
        owner = source("owner.mp4", "대표", "초음파세척기를 사용하면 설거지가 편해집니다")
        user = source("review.mp4", "2년 실사용자", "2년 써봤는데 설거지 인원이 실제로 줄었어요")
        owner["segments"][0].update({"role": "proof", "quality": 0.72})
        user["segments"][0].update({"role": "proof", "quality": 0.94})
        # Give both statements explicit shared product evidence so the lexical
        # pre-grouping can recognize them without another API call.
        owner["segments"][0]["transcript"] += " 초음파세척기 설거지 인원"
        user["segments"][0]["transcript"] += " 초음파세척기 설거지 인원"
        groups = deduplicate_segments([owner, user])
        chosen = next(row for row in user["segments"] if row["selected"])
        self.assertEqual(groups[0]["selected_segment_id"], chosen["segment_id"])
        self.assertFalse(owner["segments"][0]["selected"])

    def test_story_is_grounded_and_revision_only_reorders_cached_segments(self):
        proof = source("review.mp4", "실사용자", "2년 실제 사용했고 설거지 인원이 줄었습니다")
        caution = source("owner.mp4", "대표", "구매 전에는 업장 용량을 꼭 확인해야 합니다")
        proof["segments"][0].update({"role": "proof", "importance": 0.95, "selected": True})
        caution["segments"][0].update({"role": "purchase_caution", "importance": 0.9, "selected": True})
        project = {"project_mode": "multisource_roughcut", "sources": [proof, caution], "settings": {}, "evidence_trace": []}
        first = build_story_plan(project)
        self.assertEqual(first["timeline"][0]["source_id"], proof["source_id"])
        reasoning = {
            "recommended_direction": "주의사항을 먼저",
            "ordered_segments": [
                {"segment_id": caution["segments"][0]["segment_id"], "role": "hook", "reason": "사용자 요청", "keep": True},
                {"segment_id": proof["segments"][0]["segment_id"], "role": "proof", "reason": "증거", "keep": True},
            ],
            "editor_notes": [], "channel_evidence_confidence": "low",
        }
        revised = apply_story_reasoning(project, reasoning)
        self.assertEqual(revised["timeline"][0]["source_id"], caution["source_id"])
        validate_timeline(revised["timeline"], project["sources"])

    def test_visual_evidence_changes_quote_quality_and_has_explicit_fallback(self):
        item = source("site.mp4", "대표", "현장 배수 공사를 먼저 확인합니다")
        base = item["segments"][0]["quality"]
        item["visual_analysis"] = {"status": "succeeded", "segments": [{
            "start_time": 0, "end_time": 10, "visual_score": 0.95,
            "edit_decision": "highlight", "frame_ids": ["frame-1"],
        }]}
        apply_visual_quality(item)
        self.assertGreater(item["segments"][0]["quality"], base)
        self.assertEqual(item["segments"][0]["visual_frame_ids"], ["frame-1"])
        item["visual_analysis"] = {"status": "failed"}
        apply_visual_quality(item)
        self.assertEqual(item["segments"][0]["visual_evidence_status"], "audio_only_fallback")

    def test_long_story_candidates_are_bounded_and_public_payload_hides_raw_segments(self):
        item = source("hour.mp4", "대표", "구매 전 주의사항")
        item["segments"] = [
            {**item["segments"][0], "segment_id": f"s-{index}", "selected": True,
             "role": "purchase_caution", "importance": index / 200}
            for index in range(200)
        ]
        candidates = bounded_story_candidates([item], per_role=10, max_total=120)
        self.assertEqual(len(candidates), 10)
        public = public_project({"id": 1, "report": {"sources": [item]}})
        self.assertNotIn("segments", public["sources"][0]["transcript"])
        self.assertIn("preview", public["sources"][0]["transcript"])


class MultiSourceRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.connect = database(self.root / "history.db")
        self.queue = EditJobQueue(self.connect)

    def tearDown(self):
        self.temp.cleanup()

    def test_chunk_timeout_retry_is_idempotent_and_stale_worker_recovers(self):
        job = self.queue.enqueue(1, "source_analysis", payload={"source_id": "a"}, idempotency_key="source:1:a", max_attempts=4)
        claimed = self.queue.claim("worker")
        retried = self.queue.fail(claimed["job_id"], TimeoutError("OpenAI timeout"), retryable=True)
        self.assertEqual(retried["status"], "queued")
        duplicate = self.queue.enqueue(1, "source_analysis", payload={"source_id": "a"}, idempotency_key="source:1:a")
        self.assertEqual(duplicate["job_id"], job["job_id"])
        with self.connect() as connection:
            report = json.loads(connection.execute("SELECT report FROM history WHERE id=?", (job["job_id"],)).fetchone()[0])
            report["next_retry_at"] = None
            connection.execute("UPDATE history SET report=? WHERE id=?", (json.dumps(report), job["job_id"]))
            connection.commit()
        claimed = self.queue.claim("worker-2")
        with self.connect() as connection:
            report = json.loads(connection.execute("SELECT report FROM history WHERE id=?", (claimed["job_id"],)).fetchone()[0])
            report["heartbeat_at"] = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
            connection.execute("UPDATE history SET report=? WHERE id=?", (json.dumps(report), claimed["job_id"]))
            connection.commit()
        self.assertEqual(self.queue.recover_stale(stale_seconds=60), [claimed["job_id"]])

    def test_approval_gate_blocks_renderer_job(self):
        store = EditProjectStore(self.connect, storage_root=self.root / "media")
        project_id = store.create(keyword="gate", project={
            "project_uuid": "a" * 32, "project_mode": "multisource_roughcut",
            "sources": [], "status": "proposed", "approved_version": None,
        })
        with self.assertRaises(PermanentEditJobError):
            asyncio.run(EditPipeline(store).rough_cut_rendering({"project_id": project_id, "job_id": 9, "payload": {}}))


class MultiSourceChunkResumeTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_classification_retries_only_failed_chunk(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            connect = database(root / "history.db")
            store = EditProjectStore(connect, storage_root=root / "media")
            project_uuid = "b" * 32
            directory = store.project_dir(project_uuid, create=True)
            (directory / "source.mp4").write_bytes(b"source")
            item = new_source(filename="source.mp4")
            item.update({"storage_name": "source.mp4", "storage_key": "", "status": "UPLOAD_COMPLETE"})
            project_id = store.create(keyword="resume", project={
                "project_uuid": project_uuid, "project_mode": "multisource_roughcut",
                "sources": [item], "uploads_finalized": False, "settings": {}, "status": "uploading",
            })

            class FakeIngest:
                transcribe_calls = 0

                def __init__(self, _store): pass
                @staticmethod
                def probe(_path): return {"duration": 1200, "has_audio": True, "width": 1280, "height": 720}
                @staticmethod
                def extract_audio(_path, output, _duration, start=0): output.write_bytes(b"audio")
                async def transcribe(self, _audio):
                    self.__class__.transcribe_calls += 1
                    return {"text": "구매 전 용량을 확인하세요", "segments": [{"start": 1, "end": 4, "text": "구매 전 용량을 확인하세요"}], "provider": "fake"}
                @staticmethod
                def detect_silences_chunked(_path, _duration): return []
                @staticmethod
                def detect_scenes(_path, _duration): return []

            class FakeAnalysis:
                classify_calls = 0
                async def classify_multisource_chunk(self, *, source, segments):
                    self.__class__.classify_calls += 1
                    if self.__class__.classify_calls == 2:
                        raise TimeoutError("OpenAI timeout")
                    return {"segments": [{
                        "segment_id": row["segment_id"], "topic": "구매 주의", "role": "purchase_caution",
                        "importance": 0.9, "quality": 0.8, "confidence": 0.9, "reason": "구체적 주의사항",
                    } for row in segments]}

            pipeline = EditPipeline(store)
            visual = {"status": "failed", "fallback_used": True, "frame_results": [], "segments": []}
            job = {"project_id": project_id, "job_id": 10, "attempt": 1, "max_attempts": 4, "payload": {"source_id": item["source_id"]}}
            with patch("edit_pipeline.MediaIngestService", FakeIngest), patch(
                "edit_pipeline.EditAnalysisService", FakeAnalysis
            ), patch.object(pipeline, "_run_visual_analysis", AsyncMock(return_value=visual)):
                with self.assertRaises(TimeoutError):
                    await pipeline.source_analysis(job)
                saved = store.get(project_id)["report"]["sources"][0]
                self.assertEqual([chunk["transcript_status"] for chunk in saved["transcript_chunks"]], ["completed", "completed"])
                job["attempt"] = 2
                await pipeline.source_analysis(job)
            self.assertEqual(FakeIngest.transcribe_calls, 2)
            self.assertEqual(FakeAnalysis.classify_calls, 3)
            final = store.get(project_id)["report"]["sources"][0]
            self.assertEqual(final["status"], "SOURCE_ANALYZED")
            self.assertTrue(all(chunk["status"] == "completed" for chunk in final["transcript_chunks"]))


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg required")
class MultiSourceRenderTests(unittest.TestCase):
    def test_two_source_rough_cut_is_real_mp4_with_audio(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = {}
            rows = []
            timeline = []
            for index, frequency in enumerate((440, 660), start=1):
                sid = f"source-{index}"
                path = root / f"{sid}.mp4"
                subprocess.run([
                    "ffmpeg", "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", f"testsrc2=size=320x180:rate=20",
                    "-f", "lavfi", "-i", f"sine=frequency={frequency}:sample_rate=44100",
                    "-t", "2", "-c:v", "libx264", "-c:a", "aac", str(path), "-y",
                ], check=True)
                paths[sid] = path
                rows.append({"source_id": sid, "media": {"duration": 2, "has_audio": True}})
                timeline.append({"source_id": sid, "source_start": 0.2, "source_end": 1.4})
            output = root / "rough.mp4"
            result = EditRenderService().render_multisource_timeline(
                sources=paths, source_rows=rows, output=output, timeline=timeline,
                profile="preview_720p",
            )
            self.assertTrue(output.is_file())
            self.assertGreater(result["size_bytes"], 0)
            probe = subprocess.run([
                "ffprobe", "-v", "error", "-show_entries", "stream=codec_type",
                "-of", "csv=p=0", str(output),
            ], capture_output=True, text=True, check=True)
            self.assertIn("video", probe.stdout)
            self.assertIn("audio", probe.stdout)
            self.assertFalse(list(root.glob(".*.part.mp4")))
