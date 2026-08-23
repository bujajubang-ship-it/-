import json
import sqlite3
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import main
from conservative_rough_cut import (
    ROUGH_CUT_MODE,
    apply_script_choices,
    initialize_script_editor,
)
from edit_job_queue import EditJobQueue
from edit_plan_service import prepare_plan
from edit_project_store import EditProjectStore
from edit_render_service import EditRenderService
from media_ingest import MediaIngestService
from multisource_roughcut import build_story_plan, semantic_segments


def database(path: Path):
    connection = sqlite3.connect(path)
    connection.execute(
        """CREATE TABLE history (
        id INTEGER PRIMARY KEY AUTOINCREMENT, type TEXT NOT NULL,
        keyword TEXT NOT NULL, report TEXT NOT NULL,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP)"""
    )
    connection.commit()
    connection.close()
    return lambda: sqlite3.connect(path)


def transcript():
    return {"segments": [
        {"start": 0.0, "end": 8.0, "text": "오늘은 초음파세척기 현장을 설명합니다."},
        {"start": 8.1, "end": 15.0, "text": "이 제품은 먼저 물을 채우고"},
        {"start": 15.1, "end": 24.0, "text": "세제를 넣은 다음 작동합니다."},
        {"start": 25.0, "end": 35.0, "text": "실제 식당에서는 설거지 인원이 줄었습니다."},
        {"start": 36.5, "end": 48.0, "text": "구매 전에는 업장 용량을 확인해야 합니다."},
        {"start": 49.0, "end": 60.0, "text": "설치와 사후관리까지 상담해 드립니다."},
    ]}


class ConservativePlanTests(unittest.TestCase):
    def test_short_target_is_soft_and_partial_sentence_cut_is_rejected(self):
        plan = prepare_plan(
            {
                "target_length_seconds": 5,
                "segments": [{
                    "start_time": 12, "end_time": 18, "action": "cut",
                    "reason": "짧은 목표 시간", "confidence": 0.9,
                }],
            },
            60,
            transcript=transcript(),
            rough_cut_mode=ROUGH_CUT_MODE,
            source_filename="sample.mp4",
        )
        self.assertEqual(plan["rough_cut_mode"], ROUGH_CUT_MODE)
        self.assertTrue(plan["target_duration_used_as_soft_guide"])
        self.assertGreater(plan["estimated_output_duration"], 5)
        self.assertEqual(plan["estimated_output_duration"], 60)
        self.assertEqual(plan["rough_cut_log"]["rejected_cuts_due_to_mid_sentence"], 1)
        self.assertEqual(plan["segments"][0]["action"], "keep")

    def test_one_sentence_of_continuing_topic_is_not_removed(self):
        same_topic = {"segments": [
            {"start": 0, "end": 6, "text": "초음파세척기 제품을 먼저 설명합니다."},
            {"start": 6.1, "end": 12, "text": "초음파세척기 제품은 물을 채워 사용합니다."},
            {"start": 12.1, "end": 18, "text": "초음파세척기 제품은 세제도 중요합니다."},
        ]}
        plan = prepare_plan(
            {"segments": [{"start_time": 0, "end_time": 6, "action": "cut"}]},
            18, transcript=same_topic, rough_cut_mode=ROUGH_CUT_MODE,
        )
        self.assertEqual(plan["estimated_output_duration"], 18)
        self.assertEqual(plan["rough_cut_log"]["rejected_cuts_due_to_context_break"], 1)

    def test_missing_transcript_keeps_source_instead_of_guessing_cut_boundaries(self):
        plan = prepare_plan(
            {"segments": [{"start_time": 3, "end_time": 12, "action": "cut"}]},
            30, transcript={}, rough_cut_mode=ROUGH_CUT_MODE,
        )
        self.assertEqual(plan["estimated_output_duration"], 30)
        self.assertEqual(plan["segments"][0]["action"], "keep")
        self.assertEqual(plan["rough_cut_log"]["rejected_cuts_due_to_mid_sentence"], 1)
        self.assertTrue(plan["segments"][0]["viewer_confusion_risk"])

    def test_script_delete_restore_rebuilds_plan_without_model(self):
        prepared = prepare_plan(
            {"segments": []}, 60, transcript=transcript(),
            rough_cut_mode=ROUGH_CUT_MODE, source_filename="sample.mp4",
        )
        project = {
            "source": {"filename": "sample.mp4", "media": {"duration": 60}},
            "transcript": transcript(),
            "plan_versions": [{"version": 1, "status": "approved", "plan": prepared}],
        }
        state = initialize_script_editor(project)
        chosen = state["transcript_segments"][1]
        deleted = apply_script_choices(state, deleted_ids={chosen["segment_id"]}, restored_ids=set())
        timeline = deleted["user_modified_edit_plan"]["render_timeline"]
        self.assertFalse(any(
            row["source_start"] < chosen["original_end_time"]
            and row["source_end"] > chosen["original_start_time"]
            for row in timeline
        ))
        self.assertEqual(deleted["user_modified_edit_plan"]["rough_cut_log"]["user_deleted_blocks"], 1)
        restored = apply_script_choices(deleted, deleted_ids=set(), restored_ids={chosen["segment_id"]})
        self.assertEqual(restored["user_modified_edit_plan"]["estimated_output_duration"], 60)
        self.assertTrue(next(row for row in restored["transcript_segments"] if row["segment_id"] == chosen["segment_id"])["keep"])

    def test_multisource_target_is_also_only_a_soft_guide(self):
        source = {
            "source_id": "source-a", "filename": "interview.mp4", "speaker": "실사용자",
            "media": {"duration": 30},
            "transcript": {"segments": [
                {"start": 1, "end": 12, "text": "2년 사용한 실제 경험을 설명합니다."},
                {"start": 13, "end": 25, "text": "설거지 인원이 줄어든 결과를 설명합니다."},
            ]},
        }
        semantic_segments(source)
        for row in source["segments"]:
            row["selected"] = True
        plan = build_story_plan({
            "project_mode": "multisource_roughcut", "sources": [source],
            "settings": {"target_length_seconds": 3}, "evidence_trace": [],
        })
        self.assertGreater(plan["estimated_output_duration"], 3)
        self.assertTrue(all(row["speech_boundary_ok"] for row in plan["timeline"]))
        self.assertTrue(plan["target_duration_used_as_soft_guide"])

    def test_modified_timeline_renders_with_ffmpeg_without_ai(self):
        if not EditRenderService().ffmpeg:
            self.skipTest("ffmpeg unavailable")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "source.mp4"
            output = root / "rough.mp4"
            subprocess.run([
                "ffmpeg", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=20",
                "-f", "lavfi", "-i", "sine=frequency=440", "-t", "6",
                "-c:v", "libx264", "-c:a", "aac", str(source), "-y",
            ], check=True)
            local_transcript = {"segments": [
                {"start": 0, "end": 2, "text": "첫 문장입니다."},
                {"start": 2, "end": 4, "text": "삭제할 문장입니다."},
                {"start": 4, "end": 6, "text": "마지막 문장입니다."},
            ]}
            prepared = prepare_plan(
                {"segments": []}, 6, transcript=local_transcript,
                rough_cut_mode=ROUGH_CUT_MODE,
            )
            project = {
                "source": {"filename": "source.mp4", "media": {"duration": 6}},
                "transcript": local_transcript,
                "plan_versions": [{"version": 1, "status": "approved", "plan": prepared}],
            }
            state = initialize_script_editor(project)
            middle = state["transcript_segments"][1]["segment_id"]
            modified = apply_script_choices(state, deleted_ids={middle}, restored_ids=set())
            plan = modified["user_modified_edit_plan"]
            EditRenderService().render_timeline(
                source=source, output=output, timeline=plan["render_timeline"],
                duration=6, has_audio=True, profile="preview_720p",
            )
            media = MediaIngestService.probe(output)
            self.assertAlmostEqual(media["duration"], 4, delta=0.4)


class ConservativeScriptApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.connect = database(self.root / "history.db")
        self.store = EditProjectStore(self.connect, storage_root=self.root / "media")
        self.queue = EditJobQueue(self.connect)
        prepared = prepare_plan(
            {"segments": []}, 60, transcript=transcript(),
            rough_cut_mode=ROUGH_CUT_MODE, source_filename="sample.mp4",
        )
        self.project_id = self.store.create(keyword="script", project={
            "schema_version": 3, "project_uuid": uuid.uuid4().hex,
            "status": "approved", "approved_version": 1,
            "source": {"filename": "sample.mp4", "media": {"duration": 60}},
            "transcript": transcript(), "outputs": {"preview": {"filename": "preview.mp4"}},
            "preview_state": "succeeded", "conversation": [],
            "plan_versions": [{"version": 1, "status": "approved", "plan": prepared}],
        })
        project = self.store.get(self.project_id)["report"]
        project["rough_cut_script_editor"] = initialize_script_editor(project)
        self.store.save(self.project_id, project)
        self.client = TestClient(main.app)

    def tearDown(self):
        self.temp.cleanup()

    def test_toggle_restore_and_refresh_persist_without_ai(self):
        factory = lambda *args, **kwargs: self.store
        with patch.object(main, "EditProjectStore", factory), patch.object(
            main, "EDIT_JOB_QUEUE", self.queue
        ), patch.object(main, "EditAnalysisService", side_effect=AssertionError("AI must not run")):
            initial = self.client.get(f"/api/edit-projects/{self.project_id}").json()
            segment_id = initial["rough_cut_script_editor"]["transcript_segments"][1]["segment_id"]
            deleted = self.client.post(
                f"/api/edit-projects/{self.project_id}/edit-script/toggle",
                json={"segment_id": segment_id, "deleted": True},
            )
            self.assertEqual(deleted.status_code, 200)
            self.assertIn(segment_id, deleted.json()["script_editor"]["deleted_segment_ids"])
            refreshed = self.client.get(f"/api/edit-projects/{self.project_id}").json()
            self.assertIn(segment_id, refreshed["rough_cut_script_editor"]["deleted_segment_ids"])
            saved = self.client.post(f"/api/edit-projects/{self.project_id}/edit-script/save")
            self.assertEqual(saved.status_code, 200)
            self.assertEqual(saved.json()["version"], 2)
            restored = self.client.post(
                f"/api/edit-projects/{self.project_id}/edit-script/toggle",
                json={"segment_id": segment_id, "deleted": False},
            )
            self.assertEqual(restored.status_code, 200)
            self.assertIn(segment_id, restored.json()["script_editor"]["restored_segment_ids"])

    def test_editor_controls_are_present(self):
        response = self.client.get("/?tab=edit-director")
        self.assertEqual(response.status_code, 200)
        for element_id in (
            "ed-roughcut-summary", "ed-script-controls", "ed-script-panel", "ed-script-list",
        ):
            self.assertIn(f'id="{element_id}"', response.text)


if __name__ == "__main__":
    unittest.main()
