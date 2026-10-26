"""Hàng rào merge của PR-4b (R7, R9, HARD RULE "không auto-merge").

- ``GitHubWriter``: allowlist thao tác; không đụng ``main`` ngoài ``merge_pull``; ``merge_pull`` đòi
  ``ApprovalBinding`` THẬT và gửi ``sha`` đã duyệt; không có endpoint force.
- AST: trong toàn repo, ``merge_pull`` chỉ được gọi ở ``release/batch.py`` (đúng một chỗ); chỉ
  ``release/github_write.py`` chứa endpoint merge; shadow evaluator, digest, runner, schedule không
  chạm tới đường merge.
- ``decide_binding``: phê duyệt chỉ là review APPROVED mới nhất của người dùng, đúng head, trước hạn.
- F1: kill-switch là tham số BẮT BUỘC (Adapter, build_adapters, Gate3, FixLoop, ApprovalMirror).
- ``Git.push`` từ chối force và ``main``; kiểm tra sau release chạy code CỦA TAG (process con, cwd).
"""

import ast
import json
import shutil
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from orchestrator.config import REPO_ROOT
from release.approval_binding import ApprovalBinding, decide_binding
from release.github_write import GitHubWriteError, GitHubWriter, IntegrationConflict
from release.gitops import Git
from release.release_check import PinnedReleaseCheck
from tests.batch_fixtures import git, out

KST = ZoneInfo("Asia/Seoul")
SHA = "a" * 40
PID = "b" * 40
CUTOFF = datetime(2026, 10, 26, 11, 55, tzinfo=KST)


def binding(**over):
    kw = dict(pr=7, head_sha=SHA, patch_id=PID, review_id=11, reviewer="suanlong06",
              submitted_at="2026-10-26T11:40:00+09:00")
    kw.update(over)
    return ApprovalBinding(**kw)


class Recorder:
    def __init__(self, responses=None):
        self.calls = []
        self.responses = responses or {}

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append((method, url, body))
        for (m, suffix), resp in self.responses.items():
            if m == method and url.endswith(suffix):
                return resp
        return 201, {"sha": "c" * 40, "number": 5, "login": "harness-merger"}


class WriterTest(unittest.TestCase):
    def writer(self, rec):
        return GitHubWriter("suanlong06/Agents", token="t", http=rec)

    def test_branch_operations_never_touch_main_or_foreign_branches(self):
        rec = Recorder()
        w = self.writer(rec)
        for bad in ("main", "feature/x", "refs/heads/main", "harness/batch/../main", "-x"):
            with self.subTest(bad=bad), self.assertRaises((GitHubWriteError, ValueError)):
                w.create_branch(bad, SHA)
        for bad in ("main", "revert/rel-1", "feature"):
            with self.subTest(bad=bad), self.assertRaises((GitHubWriteError, ValueError)):
                w.merge_into_integration(bad, SHA, "m")
            with self.subTest(bad=bad), self.assertRaises((GitHubWriteError, ValueError)):
                w.delete_integration_branch(bad)
        with self.assertRaises(GitHubWriteError):
            w.create_tag("v1", SHA, "m")  # chỉ tag_prefix
        self.assertEqual(rec.calls, [])

    def test_integration_conflict_and_no_op(self):
        w = self.writer(Recorder({("POST", "/merges"): (409, {"message": "conflict"})}))
        with self.assertRaises(IntegrationConflict):
            w.merge_into_integration("harness/batch/20261026-1200-1", SHA, "m")
        w = self.writer(Recorder({("POST", "/merges"): (204, None)}))
        with self.assertRaises(GitHubWriteError):
            w.merge_into_integration("harness/batch/20261026-1200-1", SHA, "m")

    def test_merge_pull_requires_a_real_binding_and_pins_the_approved_sha(self):
        rec = Recorder({("PUT", "/pulls/7/merge"): (200, {"merged": True, "sha": "d" * 40})})
        w = self.writer(rec)

        class Fake:
            pr, head_sha, patch_id = 7, SHA, PID

        for bad in (None, 7, {"pr": 7, "head_sha": SHA}, Fake()):
            with self.subTest(bad=bad), self.assertRaises(GitHubWriteError):
                w.merge_pull(bad)
        with self.assertRaises(GitHubWriteError):
            w.merge_pull(binding(), merge_method="squash")
        self.assertEqual(rec.calls, [])
        self.assertEqual(w.merge_pull(binding()), "d" * 40)
        ((method, url, body),) = rec.calls
        self.assertEqual((method, body), ("PUT", {"sha": SHA, "merge_method": "merge"}))
        self.assertTrue(url.endswith("/repos/suanlong06/Agents/pulls/7/merge"))

    def test_merge_refused_by_github_raises(self):
        w = self.writer(Recorder({("PUT", "/pulls/7/merge"): (405, {"message": "review required"})}))
        with self.assertRaisesRegex(GitHubWriteError, "405"):
            w.merge_pull(binding())

    def test_api_surface_has_no_force_or_generic_write(self):
        public = {n for n in dir(GitHubWriter) if not n.startswith("_")}
        self.assertEqual(public, {"whoami", "create_branch", "merge_into_integration", "delete_integration_branch",
                                  "open_pull", "close_pull", "create_tag", "merge_pull"})
        src = (REPO_ROOT / "release" / "github_write.py").read_text(encoding="utf-8")
        self.assertNotIn('"force"', src)
        self.assertNotIn("'force'", src)

    def test_invalid_binding_cannot_be_constructed(self):
        for over in ({"pr": 0}, {"head_sha": "x"}, {"patch_id": ""}, {"review_id": True}, {"reviewer": ""}):
            with self.subTest(over=over), self.assertRaises(ValueError):
                binding(**over)


class MergeCallGraphAstTest(unittest.TestCase):
    """Đường merge vào main: DUY NHẤT ``GitHubWriter.merge_pull``, gọi ở DUY NHẤT ``release/batch.py``."""

    def py_files(self):
        for pkg in ("orchestrator", "pipeline", "release", "adapters", "dashboard"):
            root = REPO_ROOT / pkg
            if root.is_dir():
                yield from sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)

    def test_merge_pull_is_called_only_by_batch_once(self):
        sites = []
        for p in self.py_files():
            tree = ast.parse(p.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "merge_pull":
                    sites.append(p.relative_to(REPO_ROOT).as_posix())
        self.assertEqual(sites, ["release/batch.py"])

    def test_only_github_write_holds_the_merge_endpoint(self):
        hits = []
        for p in self.py_files():
            src = p.read_text(encoding="utf-8")
            if "/merge\"" in src or "/merge'" in src or "}/merge" in src:
                hits.append(p.relative_to(REPO_ROOT).as_posix())
        self.assertEqual(hits, ["release/github_write.py"])

    def test_shadow_digest_runner_schedule_never_import_the_writer(self):
        for name in ("shadow_eval", "digest", "schedule", "approval_binding", "incidents"):
            tree = ast.parse((REPO_ROOT / "release" / f"{name}.py").read_text(encoding="utf-8"))
            mods = {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)} | \
                   {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
            with self.subTest(module=name):
                self.assertFalse({m for m in mods if "github_write" in m or m == "batch" or m.endswith(".batch")})

    def test_no_auto_merge_switch_exists(self):
        import yaml

        cfg = yaml.safe_load((REPO_ROOT / "config" / "harness.yaml").read_text(encoding="utf-8"))
        flat = json.dumps(cfg).casefold()
        self.assertNotIn("auto_merge", flat)
        self.assertNotIn("automerge", flat)
        self.assertEqual(cfg["batch"]["merge_identities"], [])
        self.assertFalse(cfg["scheduler"]["enabled"])


class DecideBindingTest(unittest.TestCase):
    def pr(self, head=SHA):
        return {"head": {"sha": head}}

    def rv(self, rid, state="APPROVED", sha=SHA, login="suanlong06", at="2026-10-26T02:40:00Z"):
        return {"id": rid, "state": state, "commit_id": sha, "user": {"login": login}, "submitted_at": at}

    def decide(self, reviews, head=SHA, pid=PID):
        return decide_binding(self.pr(head), reviews, pr=7, head_sha=SHA, patch_id=pid, owner_logins=["suanlong06"],
                              cutoff=CUTOFF)

    def test_valid_binding(self):
        b, _ = self.decide([self.rv(1)])
        self.assertEqual((b.pr, b.head_sha, b.patch_id, b.review_id), (7, SHA, PID, 1))
        self.assertEqual(b.submitted_at, "2026-10-26T02:40:00+00:00")

    def test_every_missing_condition_blocks(self):
        cases = {
            "không review": ([], SHA, PID),
            "head đổi": ([self.rv(1)], "c" * 40, PID),
            "SHA khác": ([self.rv(1, sha="c" * 40)], SHA, PID),
            "người khác": ([self.rv(1, login="bot")], SHA, PID),
            "sau hạn": ([self.rv(1, at="2026-10-26T02:56:00Z")], SHA, PID),
            "changes requested sau đó": ([self.rv(1), self.rv(2, "CHANGES_REQUESTED", at="2026-10-26T02:41:00Z")],
                                         SHA, PID),
            "dismiss": ([self.rv(1), self.rv(2, "DISMISSED", at="2026-10-26T02:41:00Z")], SHA, PID),
            "không patch-id": ([self.rv(1)], SHA, None),
            "thiếu submitted_at": ([self.rv(1, at=None)], SHA, PID),
        }
        for name, (reviews, head, pid) in cases.items():
            with self.subTest(name):
                b, why = self.decide(reviews, head, pid)
                self.assertIsNone(b)
                self.assertTrue(why)

    def test_comment_after_approval_does_not_revoke(self):
        b, _ = self.decide([self.rv(1), self.rv(2, "COMMENTED", at="2026-10-26T02:50:00Z")])
        self.assertIsNotNone(b)


class GitPushGuardTest(unittest.TestCase):
    def test_push_refuses_force_and_main(self):
        g = Git(Path("."))
        for bad in ("+HEAD:refs/heads/revert/x", "HEAD:refs/heads/main", "HEAD:main", "--force", "HEAD:refs/tags/x"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                g.push("origin", bad)


class PinnedReleaseCheckTest(unittest.TestCase):
    """Kiểm tra chạy code CỦA TAG: process con với cwd = worktree detached tại tag (F2)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="hpc-")).resolve()
        self.repo = self.tmp / "clone"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        pkg = self.repo / "release"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("", encoding="utf-8")
        # "readonly_check" của TAG: in báo cáo cho biết nó chạy từ đâu và với tham số nào.
        (pkg / "readonly_check.py").write_text(
            "import json, os, subprocess, sys\n"
            "tag = sys.argv[sys.argv.index('--expect-tag') + 1]\n"
            "desc = subprocess.run(['git', 'describe', '--tags', '--exact-match', '--match', tag, 'HEAD'],\n"
            "                      capture_output=True, text=True).stdout.strip()\n"
            "ok = desc == tag and '--release-lock-held' in sys.argv\n"
            "print(json.dumps({'ok': ok, 'describe': desc, 'file': __file__, 'argv': sys.argv[1:],\n"
            "                  'state_dir': os.environ.get('HARNESS_STATE_DIR')}))\n"
            "sys.exit(0 if ok else 1)\n", encoding="utf-8")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "release code")
        git(self.repo, "tag", "-a", "rel-20261026-1200", "-m", "r")
        (pkg / "readonly_check.py").write_text("import sys\nprint('{\"ok\": true}')\n", encoding="utf-8")
        git(self.repo, "commit", "-q", "-am", "code mới hơn trên main (không phải tag)")

    def tearDown(self):
        from tests.batch_fixtures import rmtree
        rmtree(self.tmp)

    def test_runs_the_tags_own_code_in_a_detached_worktree(self):
        chk = PinnedReleaseCheck(git=Git(self.repo), state_dir=self.tmp / "state", worktree_root=self.tmp / "wt")
        res = chk(tag="rel-20261026-1200", since_seq=7)
        self.assertTrue(res["ok"], res)
        rep = res["report"]
        self.assertEqual(rep["describe"], "rel-20261026-1200")
        self.assertTrue(rep["file"].startswith(str(self.tmp / "wt")))
        self.assertIn("--release-lock-held", rep["argv"])
        self.assertEqual(rep["argv"][rep["argv"].index("--since-seq") + 1], "7")
        self.assertEqual(rep["state_dir"], str(self.tmp / "state"))
        self.assertEqual(res["argv"][1:4], ["-E", "-s", "-B"])
        self.assertFalse(Path(res["cwd"]).exists())  # worktree đã dọn
        self.assertNotIn("chk-", out(self.repo, "worktree", "list"))

    def test_bad_output_or_timeout_fails_closed(self):
        import subprocess

        def garbage(argv, **kw):
            return subprocess.CompletedProcess(argv, 0, b"not json", b"boom")

        chk = PinnedReleaseCheck(git=Git(self.repo), state_dir=self.tmp, worktree_root=self.tmp / "wt", run=garbage)
        self.assertFalse(chk(tag="rel-20261026-1200", since_seq=0)["ok"])

        def slow(argv, **kw):
            raise subprocess.TimeoutExpired(argv, 1)

        chk = PinnedReleaseCheck(git=Git(self.repo), state_dir=self.tmp, worktree_root=self.tmp / "wt", run=slow)
        res = chk(tag="rel-20261026-1200", since_seq=0)
        self.assertFalse(res["ok"])
        self.assertIn("quá", res["error"])


class KillSwitchRequiredTest(unittest.TestCase):
    """F1: không còn mặc định ``killswitch=None`` ("không kiểm") ở thành phần nào."""

    def test_constructors_require_a_killswitch(self):
        import inspect

        from adapters.base import Adapter
        from adapters.router import build_adapters
        from pipeline.approvals import ApprovalMirror
        from pipeline.fix_loop import FixLoop
        from pipeline.gate3 import Gate3
        from release.auto_revert import AutoRevert
        from release.batch import BatchRelease
        from release.digest import DigestBuilder
        from release.runner import Runner

        for fn in (Adapter.__init__, build_adapters, ApprovalMirror.__init__, FixLoop.__init__, Gate3.__init__,
                   AutoRevert.__init__, BatchRelease.__init__, DigestBuilder.__init__, Runner.__init__):
            with self.subTest(fn=fn.__qualname__):
                p = inspect.signature(fn).parameters["killswitch"]
                self.assertIs(p.default, inspect.Parameter.empty)
                self.assertEqual(p.kind, inspect.Parameter.KEYWORD_ONLY)

    def test_none_or_missing_is_rejected(self):
        from adapters.base import ConfigError
        from adapters.claude import ClaudeAdapter
        from orchestrator.config import FixPolicy
        from pipeline.fix_loop import FixLoop, FixLoopError
        from pipeline.gate3 import Gate3, Gate3Error
        from tests.test_gate3 import ALIASES, FakeReviewer, settings

        with self.assertRaises(ConfigError):
            ClaudeAdapter(aliases=ALIASES, killswitch=None)
        with self.assertRaises(TypeError):
            ClaudeAdapter(aliases=ALIASES)
        with self.assertRaises(FixLoopError):
            FixLoop(FixPolicy(), killswitch=None)
        with self.assertRaises(TypeError):
            FixLoop(FixPolicy())
        with self.assertRaises(Gate3Error):
            Gate3(aliases=ALIASES, adapters={"dsh": FakeReviewer("dsh")}, settings=settings(), killswitch=None)
        with self.assertRaises(TypeError):
            Gate3(aliases=ALIASES, adapters={"dsh": FakeReviewer("dsh")}, settings=settings())


if __name__ == "__main__":
    unittest.main()
