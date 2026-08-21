import asyncio
import json
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import main
from business_edit_review import _preview_plan
from edit_analysis_service import EditAnalysisService
from edit_learning_service import EditFeedbackService, build_editing_benchmarks
from edit_job_queue import EditJobQueue
from edit_pipeline import EditPipeline
from edit_plan_service import build_render_timeline, plan_diff, prepare_plan
from edit_project_store import EditProjectStore, public_project
from edit_render_service import EditRenderError, EditRenderService
from edit_visual_service import (
    VISUAL_FALLBACK_MESSAGE,
    TimecodedFrameExtractor,
    build_audio_visual_segments,
    fuse_plan_with_visual,
)
from media_ingest import MediaIngestService, MediaValidationError, TranscriptionError
from strategy_brain.brain import StrategyBrain
from strategy_brain.contracts import BrainResult, EvidenceEnvelope
from strategy_brain.tools import ReadOnlyToolRegistry


def make_database(path: Path):
    connection = sqlite3.connect(path)
    connection.execute(
        """
        CREATE TABLE history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT NOT NULL,
            keyword TEXT NOT NULL,
            report TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    connection.execute(
        "INSERT INTO history(type,keyword,report) VALUES('midform','existing','{\"safe\":true}')"
    )
    connection.commit()
    connection.close()
    return lambda: sqlite3.connect(path)


def sample_plan(short=True):
    return {
        "recommended_direction": "문제 장면을 훅으로 이동하고 반복을 제거",
        "target_length_seconds": 0,
        "create_short_highlight": short,
        "short_target_seconds": 30,
        "editor_notes": ["제품 클로즈업 B-roll은 수동 추가"],
        "enhancements": [
            {
                "id": "broll-1", "start_time": 1.4, "end_time": 2.0,
                "type": "broll", "instruction": "통로 전체컷 삽입",
                "asset_requirements": ["개선 후 통로 전체컷"], "overlay_text": "통로 확보",
                "priority": "high", "confidence": 0.9, "reason": "변화를 증명",
                "render_mode": "suggestion_only",
            }
        ],
        "segments": [
            {
                "id": "hook",
                "start_time": 2.0,
                "end_time": 2.8,
                "action": "use_as_hook",
                "reason": "문제가 가장 선명함",
                "confidence": 0.95,
                "expected_effect": "초반 이탈 감소",
                "destination": "opening",
            },
            {
                "id": "repeat",
                "start_time": 0.8,
                "end_time": 1.4,
                "action": "cut",
                "reason": "같은 설명 반복",
                "confidence": 0.9,
                "expected_effect": "정보 밀도 증가",
                "destination": "",
            },
            {
                "id": "short",
                "start_time": 2.0,
                "end_time": 3.2,
                "action": "use_as_short_clip",
                "reason": "독립된 문제/결과",
                "confidence": 0.88,
                "expected_effect": "쇼츠 훅",
                "destination": "",
            },
        ],
    }


def sample_diagnosis():
    return {
        "overall_summary": "반복을 줄이고 결과 장면을 앞으로 옮긴다.",
        "strong_points": ["현장 증거"],
        "weak_points": ["도입 반복"],
        "recommended_direction": "현장 결과 우선",
        "estimated_problems": ["초반 이탈"],
        "suggested_final_length": 3.0,
        "suggested_hook_range": {"start_time": 2.0, "end_time": 2.8, "reason": "결과 장면"},
        "channel_basis": [{"source": "retention", "insight": "설명형 도입 약함", "confidence": "medium"}],
        "data_limitations": ["샘플 프로젝트"],
        "strategy_alignment": {
            "status": "partial", "matched_promises": ["문제 우선"],
            "conflicts": [], "worksheet_priorities": ["통로 전체컷"],
        },
        "plan": sample_plan(),
    }


class FakeUpload:
    filename = "source.mp4"

    def __init__(self, data: bytes):
        self.data = data
        self.offset = 0

    async def read(self, amount):
        chunk = self.data[self.offset : self.offset + amount]
        self.offset += len(chunk)
        return chunk


class FakeProvider:
    def __init__(self, parsed):
        self.parsed = parsed
        self.requests = []

    async def generate(self, request, _executor=None):
        self.requests.append(request)
        return BrainResult(text=json.dumps(self.parsed), parsed=self.parsed)

    async def stream(self, request, _executor=None):
        if False:
            yield ""


class VisualFakeProvider(FakeProvider):
    async def generate(self, request, _executor=None):
        self.requests.append(request)
        if request.output_schema_name != "edit_visual_frames":
            return BrainResult(text=json.dumps(self.parsed), parsed=self.parsed)
        frames = []
        content = request.input[0]["content"]
        for item in content:
            if item.get("type") != "input_text" or not str(item.get("text") or "").startswith("FRAME "):
                continue
            _, frame_id, _, clock = item["text"].split(" ", 3)
            hours, minutes, seconds = clock.split(":")
            at = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
            frames.append({
                "frame_id": frame_id, "timecode_seconds": at,
                "description": "주방 바닥 공사 장면", "shaking_score": 0.1,
                "focus_score": 0.9, "brightness_score": 0.8, "occlusion_score": 0.1,
                "site_value_tags": ["바닥공사", "철거"], "speech_alignment_score": 0.85,
                "thumbnail_candidate": True, "visual_score": 0.9,
                "edit_decision": "highlight", "reason": "바닥 철거 작업이 명확함",
            })
        parsed = {"frames": frames}
        return BrainResult(text=json.dumps(parsed), parsed=parsed)


class FakeRetrieval:
    def _value(self, name):
        return EvidenceEnvelope(data=[{"name": name}], source=name, sample_size=1)

    def compare_similar_videos(self, _args): return self._value("similar")
    def get_retention_patterns(self, _args): return self._value("retention")
    def get_channel_strategy_snapshot(self, _args): return self._value("channel_snapshot")
    def search_business_pt_knowledge(self, _args): return self._value("business_pt")
    def search_feedback_history(self, _args): return self._value("feedback")
    def search_previous_worksheets(self, _args): return self._value("worksheets")
    def search_long_term_memory(self, _args): return self._value("memory")


class EditDirectorUnitTests(unittest.TestCase):
    def test_business_preview_respects_keep_seconds_and_review_gate(self):
        review = {"revised_edl": [
            {"start_time": 0, "end_time": 20, "action": "shorten_candidate", "suggested_keep_seconds": 5, "requires_user_review": False, "reason": "repeat"},
            {"start_time": 20, "end_time": 50, "action": "cut_candidate", "suggested_keep_seconds": 3, "requires_user_review": True, "reason": "important"},
            {"start_time": 50, "end_time": 60, "action": "highlight", "suggested_keep_seconds": 10, "requires_user_review": False, "reason": "proof"},
        ]}
        safe = _preview_plan(review, 60, apply_review_candidates=False)
        proposal = _preview_plan(review, 60, apply_review_candidates=True)
        self.assertEqual(safe["estimated_output_duration"], 45)
        self.assertEqual(proposal["estimated_output_duration"], 18)
        self.assertTrue(any(item["action"] == "protected_user_review" for item in safe["render_timeline"]))
        self.assertTrue(proposal["contains_unapproved_review_simulation"])

    def test_store_is_additive_and_redacts_paths(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            connect = make_database(root / "history.db")
            store = EditProjectStore(connect, storage_root=root / "media")
            project_uuid = uuid.uuid4().hex
            store.project_dir(project_uuid, create=True)
            project_id = store.create(
                keyword="raw",
                project={
                    "project_uuid": project_uuid,
                    "status": "proposed",
                    "source": {"filename": "a.mp4", "storage_name": "source.mp4", "media": {}},
                    "transcript": {"text": "private transcript"},
                    "evidence_snapshot": {"large": True},
                    "outputs": {},
                    "plan_versions": [],
                },
            )
            row = store.get(project_id)
            public = public_project(row)
            self.assertNotIn("storage_name", public["source"])
            self.assertNotIn("evidence_snapshot", public)
            self.assertEqual(public["transcript"]["preview"], "private transcript")
            with sqlite3.connect(root / "history.db") as connection:
                existing = connection.execute("SELECT COUNT(*) FROM history WHERE type='midform'").fetchone()[0]
                project_type = connection.execute("SELECT type FROM history WHERE id=?", (project_id,)).fetchone()[0]
            self.assertEqual(existing, 1)
            self.assertEqual(project_type, "edit_project")

    def test_raw_and_rough_upload_are_streamed_and_empty_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store = EditProjectStore(make_database(root / "h.db"), storage_root=root / "media")
            ingest = MediaIngestService(store)
            for label in ("raw_footage", "rough_cut"):
                project_uuid = uuid.uuid4().hex
                path, size, _ = asyncio.run(ingest.persist_upload(FakeUpload(label.encode()), project_uuid))
                self.assertEqual(path.read_bytes(), label.encode())
                self.assertGreater(size, 0)
            with self.assertRaises(MediaValidationError):
                asyncio.run(ingest.persist_upload(FakeUpload(b""), uuid.uuid4().hex))

    def test_transcript_normalization_success_and_failure(self):
        result = MediaIngestService._normalize_transcript(
            {"text": "문제와 결과", "segments": [{"start": 0, "end": 2, "text": "문제와 결과"}]},
            provider="test",
        )
        self.assertEqual(result["segments"][0]["end"], 2.0)
        with self.assertRaises(TranscriptionError):
            MediaIngestService._normalize_transcript({"text": "", "segments": []}, provider="test")

    def test_plan_builds_move_cut_shorten_and_version_diff(self):
        prepared = prepare_plan(sample_plan(), 4.0)
        timeline = prepared["render_timeline"]
        self.assertEqual(timeline[0]["action"], "move_to_hook")
        self.assertFalse(any(item["source_start"] < 1.4 and item["source_end"] > 0.8 for item in timeline[1:]))
        self.assertTrue(prepared["short_timeline"])
        changed = dict(prepared)
        changed["recommended_direction"] = "현장 분위기 보존"
        self.assertTrue(plan_diff(prepared, changed))

    def test_short_reel_always_keeps_separate_highlight_export(self):
        plan = sample_plan(short=False)
        prepared = prepare_plan(plan, 4.0, target_format="short_reel")
        self.assertTrue(prepared["create_short_highlight"])
        self.assertTrue(prepared["short_timeline"])

    def test_channel_evidence_and_structured_diagnosis(self):
        provider = FakeProvider(sample_diagnosis())
        service = EditAnalysisService(
            retrieval=FakeRetrieval(),
            brain=StrategyBrain(provider, ReadOnlyToolRegistry()),
        )
        evidence, trace, strategy = asyncio.run(
            service.collect_evidence(topic="베이커리", purpose="조회수형", strategy_id=None)
        )
        self.assertEqual(len(trace), 8)
        self.assertIn("retention", evidence)
        self.assertIn("editing_benchmarks", evidence)
        diagnosis = asyncio.run(
            service.diagnose(
                transcript={"segments": [{"start": 0, "end": 2, "text": "주방이 좁습니다"}]},
                media={"duration": 4},
                silences=[],
                scenes=[2.0],
                settings={"video_type": "raw_footage", "target_length_seconds": 0},
                evidence=evidence,
                strategy=strategy,
            )
        )
        self.assertEqual(diagnosis["plan"]["segments"][0]["action"], "use_as_hook")
        self.assertEqual(provider.requests[0].mode.value, "edit_director")
        self.assertEqual(provider.requests[0].tools, [])

    def test_benchmarks_do_not_invent_missing_retention_or_knowledge(self):
        empty = build_editing_benchmarks({})
        self.assertIsNone(empty["retention_30s_median"])
        self.assertFalse(empty["decision_rules"])
        self.assertEqual(len(empty["limitations"]), 3)

    def test_responses_visual_input_and_original_timecodes_are_grounded(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            frames = []
            for index, at in enumerate((2.0, 4.0), start=1):
                path = root / f"frame-{index}.jpg"
                path.write_bytes(b"small-jpeg-payload")
                frames.append({
                    "frame_id": f"frame-{index:04d}", "timecode_seconds": at,
                    "timecode": f"00:00:0{int(at)}.000", "kind": "periodic", "path": str(path),
                })
            provider = VisualFakeProvider(sample_diagnosis())
            service = EditAnalysisService(brain=StrategyBrain(provider, ReadOnlyToolRegistry()))
            result = asyncio.run(service.analyze_visual_frames(
                manifest={
                    "schema_version": 1, "status": "extracted", "frames": frames,
                    "frame_count": 2, "effective_interval_seconds": 2.0, "duration_seconds": 6.0,
                },
                transcript={"segments": [{"start": 1, "end": 5, "text": "바닥 타일을 철거합니다"}]},
                media={"duration": 6.0},
            ))
            self.assertEqual(result["status"], "succeeded")
            self.assertEqual([item["timecode_seconds"] for item in result["frame_results"]], [2.0, 4.0])
            request_content = provider.requests[0].input[0]["content"]
            self.assertEqual(sum(item.get("type") == "input_image" for item in request_content), 2)
            self.assertNotIn("path", result["frames"][0])
            self.assertEqual(result["segments"][0]["edit_decision"], "highlight")

    def test_visual_failure_has_explicit_audio_only_fallback(self):
        with tempfile.TemporaryDirectory() as td:
            pipeline = EditPipeline(EditProjectStore(make_database(Path(td) / "h.db")))
            result = asyncio.run(pipeline._run_visual_analysis(
                source=Path("/definitely/missing.mp4"), transcript={"segments": []},
                media={"duration": 60}, scenes=[], analysis=EditAnalysisService(brain=StrategyBrain(FakeProvider({}), ReadOnlyToolRegistry())),
            ))
            self.assertEqual(result["status"], "failed")
            self.assertTrue(result["fallback_used"])
            self.assertEqual(result["message"], VISUAL_FALLBACK_MESSAGE)


class FakeFeedbackAnalytics:
    def compare_video_performance(self, _ids):
        return [{"views": 1200, "average_view_percentage": 48.0, "data_through": "2026-08-17"}]

    def get_video_retention(self, _video_id):
        return {"retention_30s_estimate": 0.61, "data_through": "2026-08-17"}

    def get_reach_for_videos(self, _ids):
        return {"video-1": {"thumbnail_ctr": {"value": 8.2}, "thumbnail_impressions": {"value": 2000}}}


class FakeMemory:
    def __init__(self): self.rows = []
    def record(self, **kwargs): self.rows.append(kwargs); return len(self.rows)


class EditDirectorFeedbackTests(unittest.TestCase):
    def test_actual_retention_is_compared_to_approved_edit_decisions(self):
        memory = FakeMemory()
        service = EditFeedbackService(analytics=FakeFeedbackAnalytics(), memories=memory)
        project = {
            "settings": {"content_strategy_id": 7},
            "upload_feedback": {"video_id": "video-1"},
            "approved_version": 1,
            "evidence_snapshot": {"editing_benchmarks": {"retention_30s_median": 0.55, "average_view_percentage_median": 42.0}},
            "plan_versions": [{"version": 1, "plan": sample_plan()}],
        }
        result = service.evaluate(10, project)
        self.assertEqual(result["status"], "measured")
        self.assertTrue(all(item["status"] == "effective" for item in result["decision_outcomes"]))
        self.assertEqual(result["actual"]["thumbnail_ctr"], 8.2)
        self.assertEqual(memory.rows[0]["memory_type"], "edit_learning")


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg required")
class EditDirectorRenderTests(unittest.TestCase):
    def _sample(self, directory: Path, duration=4.0):
        path = directory / "source.mp4"
        subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=25",
                "-f", "lavfi", "-i", "sine=frequency=880:sample_rate=44100",
                "-t", str(duration), "-c:v", "libx264", "-pix_fmt", "yuv420p",
                "-c:a", "aac", str(path), "-y",
            ],
            check=True,
        )
        return path

    def test_metadata_too_short_and_render_outputs(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = self._sample(root)
            media = MediaIngestService.probe(source)
            self.assertGreaterEqual(media["duration"], 3.9)
            prepared = prepare_plan(sample_plan(), media["duration"])
            outputs, log = EditRenderService().render_project(
                source=source,
                directory=root,
                plan=prepared,
                media=media,
                version=1,
            )
            self.assertTrue((root / outputs["full"]["storage_name"]).exists())
            self.assertTrue((root / outputs["short"]["storage_name"]).exists())
            self.assertTrue((root / outputs["decision"]["storage_name"]).exists())
            self.assertTrue(log)
            reused, _ = EditRenderService().render_project(
                source=source, directory=root, plan=prepared, media=media, version=1
            )
            self.assertTrue(reused["full"]["reused"])
            self.assertTrue(reused["short"]["reused"])
            short = self._sample(root, duration=1.0)
            with self.assertRaises(MediaValidationError):
                MediaIngestService.probe(short)
            with self.assertRaises(EditRenderError):
                EditRenderService(ffmpeg="/missing/ffmpeg").render_timeline(
                    source=source,
                    output=root / "failed.mp4",
                    timeline=prepared["render_timeline"],
                    duration=media["duration"],
                    has_audio=True,
                )
            self.assertFalse((root / ".failed.part.mp4").exists())

    def test_render_uses_seek_bounded_inputs_and_bounded_threads(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = self._sample(root)
            media = MediaIngestService.probe(source)
            prepared = prepare_plan(sample_plan(short=False), media["duration"])
            with patch.dict("os.environ", {"EDIT_FFMPEG_THREADS": "1"}):
                output = EditRenderService().render_timeline(
                    source=source,
                    output=root / "bounded.mp4",
                    timeline=prepared["render_timeline"],
                    duration=media["duration"],
                    has_audio=True,
                )
            self.assertGreater(output["size_bytes"], 0)
            self.assertTrue((root / "bounded.mp4").exists())

        expression = EditRenderService._filter(
            prepared["render_timeline"], has_audio=True
        )
        self.assertNotIn("trim=", expression)
        for index in range(len(prepared["render_timeline"])):
            self.assertIn(f"[{index}:v]", expression)
            self.assertIn(f"[{index}:a]", expression)

    def test_sixty_second_visual_smoke_manifest_scores_and_edl(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "visual-60s.mp4"
            subprocess.run([
                "ffmpeg", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=1",
                "-t", "60", "-c:v", "libx264", "-preset", "ultrafast", str(source), "-y",
            ], check=True)
            manifest = TimecodedFrameExtractor().extract(
                source=source, output_dir=root / "frames", duration=60,
                scene_times=[7.5, 35.5], interval_seconds=1,
            )
            self.assertGreaterEqual(manifest["frame_count"], 60)
            self.assertTrue(any(item["timecode_seconds"] == 8.0 for item in manifest["frames"]))
            results = []
            for frame in manifest["frames"]:
                at = frame["timecode_seconds"]
                if at < 8:
                    decision, score, reason = "cut", 0.15, "이동 중 화면 흔들림, 설명 없음, 현장 정보 낮음"
                elif at < 25:
                    decision, score, reason = "keep", 0.82, "주방 바닥 타일 철거 장면이 명확하고 설명도 일치함"
                elif at < 36:
                    decision, score, reason = "shorten", 0.68, "화면은 좋지만 설명이 반복됨"
                elif at < 53:
                    decision, score, reason = "highlight", 0.94, "작업자가 바닥을 깨는 장면이 강하고 썸네일 후보 가능"
                else:
                    decision, score, reason = "keep", 0.65, "현장 흐름 유지"
                results.append({
                    **{key: frame[key] for key in ("frame_id", "timecode_seconds", "timecode")},
                    "visual_score": score, "speech_alignment_score": 0.8 if at >= 8 else 0.1,
                    "edit_decision": decision, "reason": reason,
                    "thumbnail_candidate": decision == "highlight", "site_value_tags": ["바닥공사"],
                })
            transcript = {"segments": [
                {"start": 8, "end": 24, "text": "바닥 타일을 철거합니다"},
                {"start": 25, "end": 52, "text": "같은 설명 뒤 작업자가 바닥을 깨고 있습니다"},
            ]}
            scored = build_audio_visual_segments(
                results, transcript, duration=60, interval_seconds=1,
            )
            self.assertEqual([item["edit_decision"] for item in scored[:4]], ["cut", "keep", "shorten", "highlight"])
            raw_plan = sample_plan(short=False)
            raw_plan["segments"] = [
                {"id": f"smoke-{i}", "start_time": item["start_time"], "end_time": item["end_time"],
                 "action": item["edit_decision"], "reason": item["reason"], "confidence": item["context_score"],
                 "expected_effect": "multimodal smoke", "destination": ""}
                for i, item in enumerate(scored)
            ]
            fused = fuse_plan_with_visual(raw_plan, {"status": "succeeded", "segments": scored})
            prepared = prepare_plan(fused, 60)
            self.assertEqual(prepared["decision_basis"], "audio_transcript+visual_frames")
            self.assertTrue(any(item["action"] == "highlight" for item in prepared["segments"]))
            self.assertTrue(any(item["action"] == "visual_highlight" for item in prepared["render_timeline"]))
            self.assertFalse(any(item["source_start"] < 7.9 and item["source_end"] > 0.1 for item in prepared["render_timeline"]))
            preview = root / "visual-preview-720p.mp4"
            EditRenderService().render_timeline(
                source=source, output=preview,
                timeline=prepared["render_timeline"], duration=60,
                has_audio=False, profile="preview_720p",
            )
            rendered = MediaIngestService.probe(preview)
            self.assertTrue(preview.exists())
            self.assertGreater(rendered["duration"], 47)
            self.assertLess(rendered["duration"], 49)


class FakeIngest(MediaIngestService):
    async def inspect_and_transcribe(self, path, media):
        return (
            {"text": "주방이 좁습니다. 해결 결과입니다.", "segments": [{"start": 0, "end": media["duration"], "text": "주방이 좁습니다"}], "provider": "test"},
            [],
            [2.0],
        )


class FailingIngest(FakeIngest):
    async def inspect_and_transcribe(self, path, media):
        raise TranscriptionError("받아쓰기 실패")


class FakeAnalysis:
    async def collect_evidence(self, **_kwargs):
        return ({"retention": {"data": [], "unavailable_reason": "test"}}, [{"source": "retention", "unavailable": True}], None)

    async def diagnose(self, **_kwargs):
        return sample_diagnosis()

    async def revise(self, **kwargs):
        plan = sample_plan()
        plan["recommended_direction"] = kwargs["user_request"]
        plan["create_short_highlight"] = True
        return {"revision_summary": "사용자 요청 반영", "plan": plan}


class EditDirectorResponseContractTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(main.app)

    def test_edit_api_http_and_validation_errors_are_json(self):
        cases = (
            self.client.get("/api/edit-projects/not-an-integer"),
            self.client.put("/api/edit-projects/999999/approve"),
            self.client.get("/api/edit-projects/999999/missing-route"),
        )
        for response in cases:
            with self.subTest(status=response.status_code):
                self.assertIn(response.status_code, (404, 405, 422))
                self.assertTrue(response.headers["content-type"].startswith("application/json"))
                self.assertIsInstance(response.json().get("error"), str)
                self.assertEqual(response.headers.get("x-content-type-options"), "nosniff")

    def test_editor_frontend_guards_json_and_sse_and_skips_success_refresh(self):
        source = (Path(__file__).parents[1] / "static" / "app.js").read_text(encoding="utf-8")
        editor = source[source.index("let edSelectedFile"):source.index("// ===== ✂️ 자동 컷편집 =====")]
        self.assertIn("async function edParseJsonResponse", editor)
        self.assertIn("async function edFetchJson", editor)
        self.assertIn("text/event-stream", editor)
        self.assertIn("서버가 예상하지 않은 HTML 응답", editor)
        self.assertIn("if (edCurrentProject && !renderCompleted)", editor)
        self.assertIn("if (data.direct_upload)", editor)
        self.assertIn("direct_upload_enabled: true", editor)
        self.assertIn("if (!edStorageState) await edLoadStorage()", editor)
        self.assertNotIn("await response.json()", editor)
        self.assertNotIn(".then(r => r.json())", editor)
        self.assertNotIn("Unexpected token", editor)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg required")
class EditDirectorApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.connect = make_database(self.root / "history.db")
        self.store = EditProjectStore(self.connect, storage_root=self.root / "media")
        self.client = TestClient(main.app)
        self.sample = self.root / "upload.mp4"
        subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "testsrc2=size=160x120:rate=20",
                "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100",
                "-t", "4", "-c:v", "libx264", "-c:a", "aac", str(self.sample), "-y",
            ],
            check=True,
        )

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def events(response):
        return [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]

    def test_direct_tab_url_serves_editor_without_special_query_validation(self):
        response = self.client.get("/?tab=edit-director")
        self.assertEqual(response.status_code, 200)
        self.assertIn('id="pane-edit-director"', response.text)
        self.assertIn('value="raw_footage"', response.text)
        self.assertIn('value="rough_cut"', response.text)
        self.assertIn('id="ed-preview-status"', response.text)
        self.assertIn('id="ed-preview-actions"', response.text)
        self.assertIn('id="ed-final-status"', response.text)

    def test_upload_diagnosis_revision_approval_gate_render_and_history(self):
        store_factory = lambda: self.store
        ingest_factory = lambda store=None: FakeIngest(store or self.store)
        queue = EditJobQueue(self.connect)
        with patch.object(main, "EditProjectStore", store_factory), patch.object(
            main, "MediaIngestService", ingest_factory
        ), patch.object(main, "EditAnalysisService", FakeAnalysis), patch.object(
            main, "EDIT_JOB_QUEUE", queue
        ):
            ids = []
            for video_type in ("raw_footage", "rough_cut"):
                with open(self.sample, "rb") as source:
                    response = self.client.post(
                        "/api/edit-projects/analyze",
                        files={"file": (f"{video_type}.mp4", source, "video/mp4")},
                        data={"video_type": video_type, "target_format": "mid_form", "purpose": "조회수형", "topic": video_type},
                    )
                self.assertEqual(response.status_code, 200)
                done = next(event for event in self.events(response) if event["step"] == "done")
                ids.append(done["project"]["id"])
                self.assertEqual(done["project"]["status"], "proposed")

            project_id = ids[0]
            blocked = self.client.post(f"/api/edit-projects/{project_id}/render")
            self.assertEqual(blocked.status_code, 409)

            revised = self.client.post(
                f"/api/edit-projects/{project_id}/revise",
                json={"message": "오프닝은 살리고 쇼츠도 만들자"},
            )
            revised_done = next(event for event in self.events(revised) if event["step"] == "done")
            self.assertEqual(len(revised_done["project"]["plan_versions"]), 2)
            self.assertTrue(revised_done["project"]["plan_versions"][-1]["diff"])

            stale = self.client.post(
                f"/api/edit-projects/{project_id}/approve", json={"version": 1}
            )
            self.assertEqual(stale.status_code, 409)

            approved = self.client.post(
                f"/api/edit-projects/{project_id}/approve", json={"version": 2}
            )
            self.assertEqual(approved.status_code, 200)
            rendered = self.client.post(f"/api/edit-projects/{project_id}/render")
            render_done = next(event for event in self.events(rendered) if event["step"] == "done")
            self.assertEqual(render_done["project"]["status"], "completed")
            self.assertIn("full", render_done["project"]["outputs"])
            self.assertIn("short", render_done["project"]["outputs"])
            output = self.client.get(f"/api/edit-projects/{project_id}/outputs/full")
            self.assertEqual(output.status_code, 200)
            self.assertGreater(len(output.content), 1000)
            linked = self.client.post(
                f"/api/edit-projects/{project_id}/link-upload", json={"video_id": "video-test-id"}
            )
            self.assertEqual(linked.status_code, 200)
            self.assertEqual(self.store.get(project_id)["report"]["upload_feedback"]["video_id"], "video-test-id")
            with sqlite3.connect(self.root / "history.db") as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM history WHERE type='midform'").fetchone()[0], 1)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM history WHERE type='edit_project'").fetchone()[0], 2)

    def test_transcription_failure_preserves_project_and_source(self):
        with patch.object(main, "EditProjectStore", lambda: self.store), patch.object(
            main, "MediaIngestService", lambda store=None: FailingIngest(store or self.store)
        ), patch.object(main, "EditAnalysisService", FakeAnalysis):
            with open(self.sample, "rb") as source:
                response = self.client.post(
                    "/api/edit-projects/analyze",
                    files={"file": ("failure.mp4", source, "video/mp4")},
                    data={"video_type": "raw_footage", "target_format": "mid_form"},
                )
            error = next(event for event in self.events(response) if event["step"] == "error")
            self.assertTrue(error["source_preserved"])
            row = self.store.get(error["project_id"])
            self.assertEqual(row["report"]["status"], "analysis_failed")
            self.assertTrue(self.store.resolve_media_path(row["report"], "source").exists())


if __name__ == "__main__":
    unittest.main()
