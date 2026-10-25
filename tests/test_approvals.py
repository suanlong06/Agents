"""Phản chiếu GitHub review vào approvals.jsonl; GitHub là nguồn sự thật (PR-3, R7)."""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from orchestrator.fsio import NotWriter, WriterGuard
from pipeline import common
from pipeline.approvals import ApprovalMirror, decide_approval, github_approval
from pipeline.github_api import GitHubClient

SHA = "1" * 40
OLD = "2" * 40
OWNER = ["suanlong06"]


def review(id_, state, *, login="suanlong06", sha=SHA, at="2026-10-26T10:00:00Z"):
    return {"id": id_, "user": {"login": login}, "state": state, "commit_id": sha, "submitted_at": at,
            "html_url": f"https://github.com/suanlong06/Agents/pull/7#pullrequestreview-{id_}"}


class FakeGitHub:
    """GitHub giả: trả PR và danh sách review theo đường dẫn."""

    def __init__(self, reviews, head=SHA):
        self.reviews = reviews
        self.head = head

    def __call__(self, url, headers, timeout_s):
        if "/reviews" in url:
            return 200, list(self.reviews)
        return 200, {"number": 7, "head": {"sha": self.head}}


class DecideTest(unittest.TestCase):
    def test_owner_approval_on_exact_sha(self):
        d = decide_approval([review(1, "APPROVED")], SHA, OWNER)
        self.assertTrue(d.approved)
        self.assertEqual((d.review_id, d.reviewer), (1, "suanlong06"))

    def test_approval_of_older_sha_does_not_count(self):
        d = decide_approval([review(1, "APPROVED", sha=OLD)], SHA, OWNER)
        self.assertFalse(d.approved)
        self.assertIn("duyệt lại", d.reason)

    def test_latest_decisive_review_wins(self):
        rs = [review(1, "APPROVED", at="2026-10-26T10:00:00Z"),
              review(2, "CHANGES_REQUESTED", at="2026-10-26T11:00:00Z")]
        self.assertFalse(decide_approval(rs, SHA, OWNER).approved)
        rs.append(review(3, "APPROVED", at="2026-10-26T12:00:00Z"))
        self.assertTrue(decide_approval(rs, SHA, OWNER).approved)
        rs.append(review(4, "COMMENTED", at="2026-10-26T13:00:00Z"))  # comment không hủy duyệt
        self.assertTrue(decide_approval(rs, SHA, OWNER).approved)
        rs.append(review(5, "DISMISSED", at="2026-10-26T14:00:00Z"))
        self.assertFalse(decide_approval(rs, SHA, OWNER).approved)

    def test_only_owner_reviews_count_case_insensitively(self):
        self.assertFalse(decide_approval([review(1, "APPROVED", login="harness-bot")], SHA, OWNER).approved)
        self.assertTrue(decide_approval([review(1, "APPROVED", login="SuanLong06")], SHA, OWNER).approved)

    def test_empty_owner_list_is_an_error(self):
        with self.assertRaises(ValueError):
            decide_approval([], SHA, [])

    def test_github_approval_requires_pr_head_to_equal_sha(self):
        client = GitHubClient("suanlong06/Agents", http_get=FakeGitHub([review(1, "APPROVED")], head=OLD))
        d = github_approval(client, 7, SHA, OWNER)
        self.assertFalse(d.approved)
        client = GitHubClient("suanlong06/Agents", http_get=FakeGitHub([review(1, "APPROVED")]))
        self.assertTrue(github_approval(client, 7, SHA, OWNER).approved)


class SchemaTest(unittest.TestCase):
    def test_new_schemas_are_valid_draft_2020_12(self):
        from jsonschema import Draft202012Validator

        for name in ("approval", "verdict"):
            Draft202012Validator.check_schema(common.load_schema(name))


class MirrorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="harness-appr-")).resolve()
        self.state_dir = self.tmp / "state"
        self.state_dir.mkdir()
        self.guard = WriterGuard(self.state_dir).acquire()
        self.gh = FakeGitHub([])
        self.client = GitHubClient("suanlong06/Agents", http_get=self.gh)

    def tearDown(self):
        self.guard.release()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def mirror(self):
        return ApprovalMirror(self.state_dir, self.guard, self.client, owner_logins=OWNER,
                              clock=lambda: "2026-10-26T20:00:00.000+09:00")

    def test_mirrors_owner_reviews_idempotently(self):
        self.gh.reviews = [review(1, "APPROVED"), review(2, "APPROVED", login="harness-bot"),
                           review(3, "PENDING"), review(4, "COMMENTED")]
        m = self.mirror()
        res = m.sync(7)
        self.assertEqual([r["review_id"] for r in res.recorded], [1, 4])
        self.assertEqual(res.skipped, 2)
        again = self.mirror().sync(7)  # khởi động lại: không ghi trùng
        self.assertEqual((again.recorded, again.duplicates), ([], 2))
        recs = ApprovalMirror.read(m.path)
        self.assertEqual(len(recs), 2)
        for r in recs:
            common.validate("approval", r)
            self.assertEqual((r["source"], r["repo"], r["pr"]), ("github", "suanlong06/Agents", 7))

    def test_dismissal_appends_a_new_record(self):
        self.gh.reviews = [review(1, "APPROVED")]
        m = self.mirror()
        m.sync(7)
        self.gh.reviews = [review(1, "DISMISSED")]
        res = m.sync(7)
        self.assertEqual([r["state"] for r in res.recorded], ["DISMISSED"])
        self.assertEqual([r["state"] for r in ApprovalMirror.read(m.path)], ["APPROVED", "DISMISSED"])

    def test_mirror_lives_in_state_dir_and_needs_writer_guard(self):
        m = self.mirror()
        self.assertEqual(m.path.parent, self.state_dir)
        self.guard.release()
        with self.assertRaises(NotWriter):
            self.mirror()
        self.guard.acquire()

    def test_tampered_mirror_never_approves(self):
        # GitHub là nguồn sự thật: thêm bản ghi APPROVED giả vào mirror không duyệt được gì.
        m = self.mirror()
        fake = {"v": 1, "mirrored_at": "x", "source": "github", "repo": "suanlong06/Agents", "pr": 7,
                "review_id": 99, "reviewer": "suanlong06", "state": "APPROVED", "commit_id": SHA,
                "submitted_at": None, "html_url": None}
        with open(m.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(fake) + "\n")
        self.assertEqual(len(ApprovalMirror.read(m.path)), 1)
        self.assertFalse(github_approval(self.client, 7, SHA, OWNER).approved)


if __name__ == "__main__":
    unittest.main()
