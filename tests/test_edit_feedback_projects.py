import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import main


class EditFeedbackProjectTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(main.app)

    def test_edit_feedback_page_exposes_project_browser(self):
        response = self.client.get("/?tab=edit")
        self.assertEqual(response.status_code, 200)
        self.assertIn('id="edit-projects-list"', response.text)
        self.assertIn('id="edit-projects-count"', response.text)
        self.assertIn("newEditFeedbackProject()", response.text)

        frontend = Path("static/app.js").read_text(encoding="utf-8")
        self.assertIn("loadEditFeedbackProjects()", frontend)
        self.assertIn("openEditFeedbackProject", frontend)
        self.assertIn("/api/edit-feedback/projects", frontend)
        self.assertIn("startPlainTranscriptEditAnalysis", frontend)

    def test_project_metadata_preserves_inputs_and_report(self):
        request = main.EditFeedbackRequest(
            keyword="주방 동선",
            script="오늘 현장의 주방 동선을 설명합니다.",
            product_url=" https://example.com/product ",
        )
        payload = main.edit_feedback_project_report(
            {"market_fit_score": 82, "overall_assessment": "좋음"}, request,
        )
        self.assertEqual(payload["market_fit_score"], 82)
        self.assertEqual(payload["_project"]["name"], "주방 동선")
        self.assertEqual(payload["_project"]["script"], request.script)
        self.assertEqual(payload["_project"]["product_url"], "https://example.com/product")

    def test_edit_project_list_uses_existing_history_rows(self):
        rows = [{
            "id": 12, "type": "edit", "keyword": "가스레인지",
            "created_at": "2026-08-24 16:30:00",
        }]
        full = {
            **rows[0],
            "report": {"market_fit_score": 70, "_project": {"name": "가스레인지"}},
        }
        with patch.object(main, "list_history", return_value=rows) as mocked, patch.object(
            main, "get_history", return_value=full,
        ):
            response = self.client.get("/api/edit-feedback/projects")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()[0]["title"], "가스레인지")
        self.assertEqual(response.json()[0]["mode"], "legacy_edit_feedback")
        mocked.assert_called_once_with("edit", limit=200)


if __name__ == "__main__":
    unittest.main()
