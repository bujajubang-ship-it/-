import asyncio
import csv
import io
import json
import sqlite3
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import main
from plain_transcript_edit import (
    analyze_duplicates,
    render_csv,
    render_markdown,
    render_plain_text,
    split_sentences,
    validate_result,
)
from plain_transcript_edit_jobs import (
    PlainTranscriptEditJobManager,
    PlainTranscriptEditJobStore,
)


SCRIPT = """대표: 오늘은 좁은 주방의 문제를 설명합니다. 먼저 실제 결과부터 보여드리겠습니다.
배수 위치가 잘못되면 작업 동선이 길어집니다. 도면에서 배수와 세척기 위치를 함께 확인해야 합니다.
설치 전에는 잔반을 제거해야 합니다. 설치 전에는 잔반을 제거해야 합니다.
실사용자: 1년 동안 사용해보니 설거지 시간이 실제로 줄었습니다. A/S 부품 공급 여부도 꼭 확인하세요."""


def valid_result(summary="최초 구성"):
    return {
        "recommended_duration_seconds": 18,
        "core_message": "좁은 주방은 제품보다 동선 설계가 먼저다.",
        "strongest_opening": "S007~S007 실사용 후기",
        "biggest_problem": "일반 설명이 실제 증거보다 먼저 나온다.",
        "data_basis_note": "채널 데이터 표본이 부족하여 Business PT와 대본의 논리 구조를 중심으로 판단함",
        "overall_flow": [
            {"order": 1, "title": "실사용 증거", "sentence_start_id": "S007", "sentence_end_id": "S007", "action": "이동", "purpose": "증거 훅", "reason": "실제 후기가 강함", "evidence_basis": ["Business PT"], "transition_note": "화면 연결 확인 필요", "estimated_seconds": 4},
            {"order": 2, "title": "문제와 설계", "sentence_start_id": "S001", "sentence_end_id": "S004", "action": "유지", "purpose": "문제 해결", "reason": "논리 흐름", "evidence_basis": ["Low Data"], "transition_note": "영상 화면 직접 확인 필요", "estimated_seconds": 12},
            {"order": 3, "title": "구매 기준", "sentence_start_id": "S008", "sentence_end_id": "S008", "action": "유지", "purpose": "CTA", "reason": "A/S 기준", "evidence_basis": ["Business PT"], "transition_note": "B-roll로 연결 추천", "estimated_seconds": 3},
        ],
        "edit_table": [
            {"final_order": 1, "sentence_start_id": "S007", "sentence_end_id": "S007", "start_sentence": "실사용자: 1년 동안 사용해보니 설거지 시간이 실제로 줄었습니다.", "end_sentence": "실사용자: 1년 동안 사용해보니 설거지 시간이 실제로 줄었습니다.", "action": "이동", "purpose": "증거 훅", "edit_instruction": "완결 문장 전체 이동", "transition_note": "화면 연결 확인 필요", "reason": "후반 실제 후기 우선", "evidence_basis": ["Business PT"], "broll_note": "제품 작동 B-roll 추천", "estimated_seconds": 4},
            {"final_order": 2, "sentence_start_id": "S001", "sentence_end_id": "S004", "start_sentence": "대표: 오늘은 좁은 주방의 문제를 설명합니다.", "end_sentence": "도면에서 배수와 세척기 위치를 함께 확인해야 합니다.", "action": "유지", "purpose": "문제와 해결", "edit_instruction": "연속 유지", "transition_note": "같은 촬영본인지 불확실 · 영상 화면 직접 확인 필요", "reason": "맥락 보존", "evidence_basis": ["Low Data"], "broll_note": "도면 화면 추천", "estimated_seconds": 12},
            {"final_order": 3, "sentence_start_id": "S005", "sentence_end_id": "S005", "start_sentence": "설치 전에는 잔반을 제거해야 합니다.", "end_sentence": "설치 전에는 잔반을 제거해야 합니다.", "action": "축약", "purpose": "사용 전 주의", "edit_instruction": "중복 중 한 문장만 유지", "transition_note": "같은 촬영본 연속 유지", "reason": "가장 짧은 설명 선택", "evidence_basis": ["대본 중복"], "broll_note": "", "estimated_seconds": 3},
            {"final_order": 4, "sentence_start_id": "S008", "sentence_end_id": "S008", "start_sentence": "A/S 부품 공급 여부도 꼭 확인하세요.", "end_sentence": "A/S 부품 공급 여부도 꼭 확인하세요.", "action": "유지", "purpose": "구매 기준", "edit_instruction": "마지막에 유지", "transition_note": "B-roll로 연결 추천", "reason": "신뢰 확보", "evidence_basis": ["Business PT"], "broll_note": "부품 화면", "estimated_seconds": 3},
        ],
        "deletions": [
            {"sentence_start_id": "S006", "sentence_end_id": "S006", "start_sentence": "설치 전에는 잔반을 제거해야 합니다.", "end_sentence": "설치 전에는 잔반을 제거해야 합니다.", "reason": "동일 설명 반복"},
        ],
        "duplicates": [{"topic": "잔반 제거", "candidates": ["S005", "S006"], "selected": "S005", "reason": "동일 원문", "remaining_action": "S006 삭제"}],
        "condensations": [{"delete_sentence_ids": ["S006"], "keep_sentence_ids": ["S005"], "purpose_after_condensing": "사용 전 주의"}],
        "final_instructions": {
            "final_flow": ["실사용 증거", "문제와 설계", "구매 기준"],
            "final_sentence_order": ["S007", "S001~S004", "S008"],
            "delete_sentences": ["S005~S006"], "condense_sentences": ["S005~S006 중 하나만"],
            "move_sentence_groups": ["S007~S007 오프닝 이동"], "duplicate_decisions": ["S005 선택, S006 삭제"],
            "broll_positions": ["S007 제품 작동 화면"], "caption_emphasis": ["S008 A/S"],
            "connection_lines_needed": [], "must_keep_statements": ["S007", "S008"],
            "screen_review_required": ["S007→S001 화면 연결"], "expected_duration_seconds": 18,
        },
        "used_evidence": [{"source": "knowledge:business_pt", "claim": "실제 증거 우선", "sample_size": 0}],
        "revision_summary": summary,
    }


class FakeService:
    async def analyze(self, **_kwargs):
        return valid_result()

    async def revise(self, **_kwargs):
        return valid_result("실사용 후기를 더 앞에 유지")


class SentenceAndExportTests(unittest.TestCase):
    def test_sentence_ids_preserve_original_text_without_timecodes(self):
        sentences = split_sentences(SCRIPT)
        self.assertEqual([row["id"] for row in sentences], [f"S{i:03d}" for i in range(1, 9)])
        self.assertEqual(sentences[0]["text"], "대표: 오늘은 좁은 주방의 문제를 설명합니다.")
        self.assertEqual(sentences[6]["text"], "실사용자: 1년 동안 사용해보니 설거지 시간이 실제로 줄었습니다.")

    def test_duplicate_detection_and_continuous_ranges(self):
        sentences = split_sentences(SCRIPT)
        groups = analyze_duplicates(sentences)
        self.assertTrue(any(group["candidate_ids"] == ["S005", "S006"] for group in groups))
        validate_result(valid_result(), sentences, numeric_data_available=False)
        broken = valid_result()
        broken["edit_table"][0]["sentence_start_id"] = "S007 일부"
        with self.assertRaises(ValueError):
            validate_result(broken, sentences, numeric_data_available=False)
        unsupported_retention = valid_result()
        unsupported_retention["data_basis_note"] += " Retention 30초 유지율은 48%입니다."
        with self.assertRaisesRegex(ValueError, "Retention"):
            validate_result(
                unsupported_retention, sentences,
                numeric_data_available=True, ctr_data_available=True,
                retention_data_available=False,
            )

    def test_markdown_and_csv_exports(self):
        result = valid_result()
        version = {"version": 2, "result": result}
        markdown = render_markdown({"title": "주방 동선"}, version)
        self.assertIn("문장 기준 상세 편집표", markdown)
        self.assertIn("S007~S007", markdown)
        self.assertIn("삭제·중복·축약 목록", markdown)
        self.assertIn("최종 문장 배치 순서", markdown)
        self.assertIn("시작 문장", markdown)
        csv_text = render_csv(result)
        rows = list(csv.DictReader(io.StringIO(csv_text.lstrip("\ufeff"))))
        self.assertEqual(rows[0]["sentence_start_id"], "S007")
        self.assertEqual(rows[0]["action"], "이동")
        # 편집자 전달용은 장면 순서와 자막 문장만 담는다 (S코드·이유·근거는 넣지 않는다).
        sentences = split_sentences(SCRIPT)
        plain_text = render_plain_text(
            {"_project": {"title": "주방 동선"}, "sentences": sentences}, version)
        self.assertTrue(plain_text.startswith("주방 동선 — 편집 순서"))
        self.assertIn("1번째 장면", plain_text)
        self.assertIn("삭제할 자막", plain_text)
        self.assertIn("실사용자: 1년 동안 사용해보니 설거지 시간이 실제로 줄었습니다.", plain_text)
        for banned in ("S007", "[전체 흐름]", "[상세 편집 순서]", "이유:", "근거:", "화면:"):
            self.assertNotIn(banned, plain_text)

    def test_thirty_minute_scale_transcript_keeps_stable_ids(self):
        long_script = "\n".join(
            f"전문가: 사용법 설명 {index}입니다. 권장 온도와 A/S 기준을 확인합니다."
            for index in range(1, 451)
        )
        sentences = split_sentences(long_script)
        self.assertEqual(len(sentences), 900)
        self.assertEqual(sentences[0]["id"], "S001")
        self.assertEqual(sentences[-1]["id"], "S900")
        self.assertEqual(sentences[-1]["text"], "권장 온도와 A/S 기준을 확인합니다.")


class BackgroundJobTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        path = Path(self.temp.name) / "jobs.db"

        def connect():
            connection = sqlite3.connect(path, timeout=5)
            connection.row_factory = sqlite3.Row
            return connection

        self.connect = connect
        connection = connect()
        connection.execute("CREATE TABLE history (id INTEGER PRIMARY KEY AUTOINCREMENT,type TEXT,keyword TEXT,report TEXT,created_at TEXT DEFAULT CURRENT_TIMESTAMP)")
        connection.commit(); connection.close()
        self.store = PlainTranscriptEditJobStore(connect)

        def writer(type_, keyword, report):
            connection = connect()
            cursor = connection.execute("INSERT INTO history(type,keyword,report) VALUES (?,?,?)", (type_, keyword, json.dumps(report, ensure_ascii=False)))
            connection.commit(); row_id = cursor.lastrowid; connection.close(); return row_id

        def reader(row_id):
            connection = connect(); row = connection.execute("SELECT * FROM history WHERE id=?", (row_id,)).fetchone(); connection.close()
            if not row: return None
            data = dict(row); data["report"] = json.loads(data["report"]); return data

        def updater(row_id, report, keyword=None):
            connection = connect(); connection.execute("UPDATE history SET keyword=COALESCE(?,keyword),report=? WHERE id=?", (keyword, json.dumps(report, ensure_ascii=False), row_id)); connection.commit(); connection.close(); return True

        self.manager = PlainTranscriptEditJobManager(
            store=self.store, service_factory=FakeService,
            evidence_collector=lambda _topic: {"business_pt": {"source": "knowledge:business_pt", "sample_size": 0, "data": []}},
            history_writer=writer, history_reader=reader, history_updater=updater,
        )
        self.reader = reader

    def tearDown(self):
        self.temp.cleanup()

    async def test_initial_job_and_v2_v3_revisions_persist(self):
        request = {"title": "주방 동선 프로젝트", "topic": "좁은 주방", "script": SCRIPT, "target_duration_seconds": 20, "purpose": "상담", "additional_request": ""}
        initial = self.manager.enqueue_initial(request)
        self.assertTrue(await self.manager.process_once())
        done = self.store.get(initial["job_id"])
        self.assertEqual(done["status"], "done")
        self.assertEqual(done["sentence_count"], 8)
        history_id = done["result"]["history_id"]
        self.assertEqual(self.reader(history_id)["type"], "transcript_edit_guide")
        for message in ("실사용 후기를 더 앞에 둬.", "A/S는 마지막으로 보내."):
            revision = self.manager.enqueue_revision(history_id, message)
            self.assertTrue(await self.manager.process_once())
            self.assertEqual(self.store.get(revision["job_id"])["status"], "done")
        project = self.reader(history_id)["report"]
        self.assertEqual([row["version"] for row in project["versions"]], [1, 2, 3])
        self.assertEqual(project["_project"]["current_version"], 3)
        self.assertEqual(project["_project"]["mode"], "transcript_edit_guide")
        self.assertEqual(len(project["conversation"]), 4)
        guide_text = project["versions"][-1]["employee_guide_text"]
        self.assertIn("1번째 장면", guide_text)
        self.assertIn("삭제할 자막", guide_text)
        self.assertNotIn("[상세 편집 순서]", guide_text)
        self.assertEqual(project["_project"]["transcript_hash"], initial["transcript_hash"])

    async def test_same_transcript_reuses_sentence_and_evidence_checkpoint(self):
        request = {"title": "cache", "topic": "좁은 주방", "script": SCRIPT, "target_duration_seconds": 20, "purpose": "", "additional_request": ""}
        first = self.manager.enqueue_initial(request)
        await self.manager.process_once()
        self.assertEqual(self.store.get(first["job_id"])["status"], "done")
        second = self.manager.enqueue_initial(request)
        cached = self.store.get(second["job_id"], include_checkpoint=True)
        self.assertEqual(cached["retry_state"], "cache_hit")
        self.assertEqual(len(cached["checkpoint"]["sentences"]), 8)
        self.assertIn("evidence", cached["checkpoint"])

    async def test_stale_processing_job_is_requeued_from_checkpoint(self):
        request = {"title": "resume", "topic": "좁은 주방", "script": SCRIPT}
        job = self.store.create(kind="initial", request=request)
        self.store.update(
            job["job_id"], status="processing", attempt=1,
            checkpoint_json=json.dumps({"sentences": split_sentences(SCRIPT)}, ensure_ascii=False),
            heartbeat_at="2020-01-01T00:00:00Z",
        )
        self.assertEqual(self.store.recover_stale(stale_seconds=60), 1)
        resumed = self.store.get(job["job_id"], include_checkpoint=True)
        self.assertEqual(resumed["status"], "queued")
        self.assertEqual(resumed["retry_state"], "resumed_from_checkpoint")
        self.assertEqual(len(resumed["checkpoint"]["sentences"]), 8)


class ApiAndUiTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(main.app)

    def test_page_has_independent_transcript_guide_tab_and_legacy_edit_is_unchanged(self):
        response = self.client.get("/?tab=transcript-guide")
        self.assertEqual(response.status_code, 200)
        for element_id in (
            "tab-transcript-guide", "pane-transcript-guide", "tg-script-input", "tg-live-status",
            "tg-table-body", "tg-revision-input", "tg-version-select", "tg-employee-guide-text",
        ):
            self.assertIn(f'id="{element_id}"', response.text)
        self.assertIn("📋 직원용 편집 가이드 전체 복사", response.text)
        self.assertIn("downloadTranscriptGuide('markdown')", response.text)
        self.assertIn("downloadTranscriptGuide('txt')", response.text)
        self.assertNotIn('id="edit-mode-flow"', response.text)
        self.assertIn('id="edit-keyword-input"', response.text)
        self.assertIn('onclick="startEditAnalysis()"', response.text)
        edit_pane = response.text.split('id="pane-edit"', 1)[1].split('id="pane-transcript-guide"', 1)[0]
        self.assertNotIn('id="tg-', edit_pane)

    def test_create_job_route_is_background_and_has_status_url(self):
        class FakeManager:
            _worker_task = object()
            def enqueue_initial(self, request):
                self.request = request
                return {"job_id": "a" * 32}
        manager = FakeManager()
        with patch.object(main, "PLAIN_TRANSCRIPT_EDIT_JOBS", manager):
            response = self.client.post("/api/transcript-edit-guides/jobs", json={
                "title": "동선", "topic": "좁은 주방", "script": SCRIPT,
                "target_duration_seconds": 600, "purpose": "상담", "additional_request": "",
            })
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["job_id"], "a" * 32)
        self.assertEqual(manager.request["script"], SCRIPT)
        self.assertEqual(
            response.json()["status_url"],
            "/api/transcript-edit-guides/jobs/" + "a" * 32,
        )
        self.assertEqual(self.client.post("/api/edit-feedback/flow-jobs", json={}).status_code, 405)

    def test_project_list_uses_dedicated_history_type(self):
        summaries = [{"id": 21, "type": "transcript_edit_guide", "keyword": "동선", "created_at": "2026-08-26"}]
        row = {
            **summaries[0],
            "report": {
                "_project": {"mode": "transcript_edit_guide", "title": "동선", "topic": "좁은 주방", "current_version": 2},
                "current_result": {"recommended_duration_seconds": 600},
            },
        }
        with patch.object(main, "list_history", return_value=summaries) as mocked, patch.object(main, "get_history", return_value=row):
            response = self.client.get("/api/transcript-edit-guides/projects")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()[0]["type"], "transcript_edit_guide")
        self.assertEqual(response.json()[0]["current_version"], 2)
        mocked.assert_called_once_with("transcript_edit_guide", limit=200)

    def test_markdown_txt_and_csv_download_latest_or_selected_version(self):
        sentences = split_sentences(SCRIPT)
        project = {
            "_project": {"mode": "transcript_edit_guide", "title": "동선"},
            "sentences": sentences,
            "versions": [
                {"version": 1, "result": valid_result("v1")},
                {"version": 2, "result": valid_result("v2")},
            ],
        }
        row = {"id": 7, "type": "transcript_edit_guide", "keyword": "동선", "report": project}
        with patch.object(main, "get_history", return_value=row):
            markdown = self.client.get("/api/transcript-edit-guides/projects/7/download/markdown?version=1")
            txt_response = self.client.get("/api/transcript-edit-guides/projects/7/download/txt?version=2")
            csv_response = self.client.get("/api/transcript-edit-guides/projects/7/download/csv")
        self.assertEqual(markdown.status_code, 200)
        self.assertIn("버전: v1", markdown.text)
        self.assertEqual(txt_response.status_code, 200)
        self.assertIn("1번째 장면", txt_response.text)
        self.assertNotIn("[편집 담당자 전달용 최종본]", txt_response.text)
        self.assertEqual(csv_response.status_code, 200)
        self.assertIn("sentence_start_id", csv_response.text)


if __name__ == "__main__":
    unittest.main()
