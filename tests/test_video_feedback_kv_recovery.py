import json
import unittest
from unittest.mock import patch

from recovery_tools.video_feedback_kv import (
    CONFIRMATION_PHRASE,
    VideoFeedbackRecoveryError,
    _validated_rows,
    run,
)


VALID_ROWS = [
    {
        "keyword": "테스트",
        "report": json.dumps({"feedback": "내용"}, ensure_ascii=False),
        "created_at": "2026-08-01 03:04:05",
    }
]


class VideoFeedbackKvRecoveryTests(unittest.TestCase):
    def test_malformed_kv_json_is_rejected(self):
        payload = {"data": {"rows": [{"report": "not-json"}]}}
        with self.assertRaisesRegex(VideoFeedbackRecoveryError, "malformed"):
            _validated_rows(payload)

    def test_dry_run_never_restores_rows(self):
        with (
            patch.dict(
                "os.environ",
                {"CNMAKER_BASE": "https://kv.invalid", "CNMAKER_SECRET": "test-only"},
                clear=True,
            ),
            patch(
                "recovery_tools.video_feedback_kv.database.list_history",
                return_value=[],
            ),
            patch(
                "recovery_tools.video_feedback_kv.database.restore_video_feedback_rows"
            ) as restore,
            patch(
                "recovery_tools.video_feedback_kv._fetch_rows", return_value=VALID_ROWS
            ),
        ):
            result = run(apply=False)
        self.assertFalse(result["applied"])
        restore.assert_not_called()

    def test_apply_requires_confirmation_and_empty_database(self):
        with (
            patch.dict(
                "os.environ",
                {"CNMAKER_BASE": "https://kv.invalid", "CNMAKER_SECRET": "test-only"},
                clear=True,
            ),
            patch(
                "recovery_tools.video_feedback_kv.database.list_history",
                return_value=[],
            ),
            patch(
                "recovery_tools.video_feedback_kv.database.restore_video_feedback_rows"
            ) as restore,
            patch(
                "recovery_tools.video_feedback_kv._fetch_rows", return_value=VALID_ROWS
            ),
        ):
            with self.assertRaisesRegex(VideoFeedbackRecoveryError, "confirmation"):
                run(apply=True, confirmation="wrong")
        restore.assert_not_called()

        with (
            patch.dict(
                "os.environ",
                {"CNMAKER_BASE": "https://kv.invalid", "CNMAKER_SECRET": "test-only"},
                clear=True,
            ),
            patch(
                "recovery_tools.video_feedback_kv.database.list_history",
                return_value=[{"id": 1}],
            ),
            patch(
                "recovery_tools.video_feedback_kv.database.restore_video_feedback_rows"
            ) as restore,
            patch(
                "recovery_tools.video_feedback_kv._fetch_rows", return_value=VALID_ROWS
            ),
        ):
            with self.assertRaisesRegex(VideoFeedbackRecoveryError, "already exist"):
                run(apply=True, confirmation=CONFIRMATION_PHRASE)
        restore.assert_not_called()

    def test_confirmed_apply_restores_valid_rows_once(self):
        with (
            patch.dict(
                "os.environ",
                {"CNMAKER_BASE": "https://kv.invalid", "CNMAKER_SECRET": "test-only"},
                clear=True,
            ),
            patch(
                "recovery_tools.video_feedback_kv.database.list_history",
                return_value=[],
            ),
            patch(
                "recovery_tools.video_feedback_kv.database.restore_video_feedback_rows",
                return_value=1,
            ) as restore,
            patch(
                "recovery_tools.video_feedback_kv._fetch_rows", return_value=VALID_ROWS
            ),
        ):
            result = run(apply=True, confirmation=CONFIRMATION_PHRASE)
        self.assertTrue(result["applied"])
        self.assertEqual(result["restored_rows"], 1)
        restore.assert_called_once_with(VALID_ROWS)


if __name__ == "__main__":
    unittest.main()
