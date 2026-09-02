"""올린 영상은 보드에서 감추고 달력에는 남긴다.

업로드까지 끝난 영상이 편집 목록에 계속 쌓이면 지금 할 일이 안 보인다.
그렇다고 지우면 언제 뭘 올렸는지 사라지므로, 감추기만 하고 꺼내는 버튼을 둔다.
"""

import re
import unittest
from pathlib import Path


class PipelineDoneHidingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.js = Path("static/app.js").read_text(encoding="utf-8")

    def test_upload_and_sns_count_as_done(self):
        match = re.search(r"const PIPELINE_DONE_STAGES = \[([^\]]*)\]", self.js)
        self.assertIsNotNone(match, "완료 단계 목록이 없습니다")
        self.assertIn("'uploaded'", match.group(1))
        self.assertIn("'sns'", match.group(1))

    def test_the_board_uses_the_filtered_list(self):
        match = re.search(r"function renderKanban\(\) \{(.*?)\n\}", self.js, re.S)
        self.assertIsNotNone(match)
        self.assertIn("boardVideosList()", match.group(1))
        self.assertNotIn("editVideosList()", match.group(1))

    def test_the_calendar_still_sees_every_video(self):
        match = re.search(r"function renderCalendar\(\) \{(.*?)\n\}", self.js, re.S)
        self.assertIsNotNone(match)
        # 달력은 plVideos 를 직접 돌아야 올린 영상이 그대로 남는다.
        self.assertIn("plVideos.forEach", match.group(1))
        self.assertNotIn("boardVideosList", match.group(1))

    def test_the_stage_counts_still_include_finished_videos(self):
        match = re.search(r"function renderPipelineSummary\(\) \{(.*?)\n\}", self.js, re.S)
        self.assertIsNotNone(match)
        self.assertIn("editVideosList()", match.group(1))

    def test_there_is_a_button_to_bring_them_back(self):
        self.assertIn("function togglePlDone()", self.js)
        self.assertIn("올린 영상", self.js)
        self.assertIn("pl_show_done", self.js)

    def test_hidden_videos_are_not_left_behind_a_stale_filter(self):
        match = re.search(r"function togglePlDone\(\) \{(.*?)\n\}", self.js, re.S)
        self.assertIsNotNone(match)
        self.assertIn("plFilterVal = null", match.group(1))

    def test_moving_a_video_to_uploaded_explains_where_it_went(self):
        match = re.search(r"async function setStage\(id, idx\) \{(.*?)\n\}", self.js, re.S)
        self.assertIsNotNone(match)
        self.assertIn("달력에는 그대로", match.group(1))


if __name__ == "__main__":
    unittest.main()
