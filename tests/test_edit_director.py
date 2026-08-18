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
from edit_analysis_service import EditAnalysisService
from edit_plan_service import build_render_timeline, plan_diff, prepare_plan
from edit_project_store import EditProjectStore, public_project
from edit_render_service import EditRenderError, EditRenderService
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


class FakeRetrieval:
    def _value(self, name):
        return EvidenceEnvelope(data=[{"name": name}], source=name, sample_size=1)

    def compare_similar_videos(self, _args): return self._value("similar")
    def get_retention_patterns(self, _args): return self._value("retention")
    def search_business_pt_knowledge(self, _args): return self._value("business_pt")
    def search_feedback_history(self, _args): return self._value("feedback")
    def search_previous_worksheets(self, _args): return self._value("worksheets")
    def search_long_term_memory(self, _args): return self._value("memory")


class EditDirectorUnitTests(unittest.TestCase):
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

    def test_channel_evidence_and_structured_diagnosis(self):
        provider = FakeProvider(sample_diagnosis())
        service = EditAnalysisService(
            retrieval=FakeRetrieval(),
            brain=StrategyBrain(provider, ReadOnlyToolRegistry()),
        )
        evidence, trace, strategy = asyncio.run(
            service.collect_evidence(topic="베이커리", purpose="조회수형", strategy_id=None)
        )
        self.assertEqual(len(trace), 6)
        self.assertIn("retention", evidence)
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

    def test_upload_diagnosis_revision_approval_gate_render_and_history(self):
        store_factory = lambda: self.store
        ingest_factory = lambda store=None: FakeIngest(store or self.store)
        with patch.object(main, "EditProjectStore", store_factory), patch.object(
            main, "MediaIngestService", ingest_factory
        ), patch.object(main, "EditAnalysisService", FakeAnalysis):
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
