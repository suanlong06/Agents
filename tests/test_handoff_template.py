"""Mẫu bàn giao (PR-2): ngắn, dẫn link artifact (path + SHA), model lấy từ ledger."""

import re
import unittest

from orchestrator.config import REPO_ROOT

TEMPLATE = REPO_ROOT / "docs" / "handoff_template.md"
LIMIT = 2048  # mặc định tạm (R16, SPEC mục 8)


class HandoffTemplateTest(unittest.TestCase):
    def setUp(self):
        self.text = TEMPLATE.read_text(encoding="utf-8")

    def test_has_required_sections_and_artifact_link(self):
        for h in ("## Đã làm", "## File đổi", "## Kết quả", "## Lỗi còn lại", "## Việc tiếp theo", "## Artifact"):
            self.assertIn(h, self.text)
        self.assertRegex(self.text, r"`\{artifact_path\}` @ `\{artifact_sha\}`")
        self.assertIn("{served_model}", self.text)

    def test_filled_example_fits_limit(self):
        filled = re.sub(r"\{[a-z_]+\}", "x" * 12, self.text)
        filled = filled.replace("x" * 12 + "` @ `" + "x" * 12, "runs/T-1/report.md` @ `" + "0" * 40)
        self.assertLessEqual(len(filled.encode("utf-8")), LIMIT)


if __name__ == "__main__":
    unittest.main()
