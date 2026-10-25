"""Manifest release và revert thủ công (PR-4a, R10) trên repo fixture.

- Manifest khớp ``git log --first-parent prevTag..newTag`` (merge commit, squash, commit thẳng).
- Revert theo runbook (``git revert`` từng PR, thứ tự ngược manifest, trên nhánh mới từ
  ``main``) được đẩy lên một remote CẤM non-fast-forward mà không cần force-push, và giữ
  commit merge sau release.
- Revert conflict: ``git revert`` dừng, ``--abort`` trả về trạng thái sạch, incident ``frozen``.
"""

import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from pipeline import common
from release import manifest as manifest_mod
from release.manifest import ManifestError, compute_manifest, revert_plan

GIT_ID = ["-c", "user.name=harness-test", "-c", "user.email=harness-test@example.invalid",
          "-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false", "-c", "core.autocrlf=false"]
NOW = "2026-10-26T12:00:00.000+09:00"


def git(cwd, *args, check=True) -> subprocess.CompletedProcess:
    proc = subprocess.run(["git", *GIT_ID, "-C", str(cwd), *args], capture_output=True,
                          stdin=subprocess.DEVNULL, check=False)
    if check and proc.returncode != 0:
        raise AssertionError(f"git {args}: {proc.stderr.decode('utf-8', 'replace')}")
    return proc


def out(cwd, *args) -> str:
    return git(cwd, *args).stdout.decode("utf-8", "replace").strip()


def rmtree(path: Path) -> None:
    def onexc(func, p, _exc):  # object git là read-only trên Windows
        os.chmod(p, stat.S_IWRITE)
        func(p)

    shutil.rmtree(path, onexc=onexc)


class FixtureRepo(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="harness-rel-")).resolve()
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")

    def tearDown(self):
        rmtree(self.tmp)

    def write(self, rel, text):
        p = self.repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(text.encode("utf-8"))

    def commit(self, msg, **files) -> str:
        for rel, text in files.items():
            self.write(rel.replace("__", "/"), text)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", msg)
        return out(self.repo, "rev-parse", "HEAD")

    def merge_pr(self, number, branch, **files) -> str:
        """Giống nút merge của GitHub: nhánh có 2 commit, merge --no-ff vào main."""
        git(self.repo, "switch", "-q", "-c", branch)
        items = list(files.items())
        self.commit(f"{branch}: phần 1", **dict(items[:1]))
        self.commit(f"{branch}: phần 2", **(dict(items[1:]) or {f"{branch}.log": "2\n"}))
        git(self.repo, "switch", "-q", "main")
        git(self.repo, "merge", "-q", "--no-ff", "-m", f"Merge pull request #{number} from bot/{branch}", branch)
        return out(self.repo, "rev-parse", "HEAD")

    def tag(self, name):
        git(self.repo, "tag", "-a", name, "-m", f"release {name}")

    def build_release(self):
        """v1 → (PR #1 merge, PR #2 squash, commit thẳng) → v2 → PR #3 merge SAU release."""
        self.commit("base", **{"app.txt": "line1\nline2\n"})
        self.tag("v1")
        self.pr1 = self.merge_pr(1, "f1", **{"a.txt": "A\n", "a2.txt": "A2\n"})
        self.pr2 = self.commit("Đổi dòng 2 (#2)", **{"app.txt": "line1\nline2 v2\n"})
        self.direct = self.commit("chore: ghi chú", **{"notes.txt": "n\n"})
        self.tag("v2")
        self.pr3 = self.merge_pr(3, "f3", **{"c.txt": "C sau release\n"})


class ManifestTest(FixtureRepo):
    def test_manifest_matches_git_log(self):
        self.build_release()
        m = compute_manifest(self.repo, "v1", "v2", clock=lambda: NOW)
        common.validate("manifest", m)
        expected = out(self.repo, "log", "--first-parent", "--reverse", "--format=%H", "v1..v2").splitlines()
        self.assertEqual([e["sha"] for e in m["entries"]], expected)
        self.assertEqual(expected, out(self.repo, "rev-list", "--first-parent", "--reverse", "v1..v2").splitlines())
        self.assertEqual([e["sha"] for e in m["entries"]], [self.pr1, self.pr2, self.direct])
        self.assertEqual([(e["kind"], e["pr"]) for e in m["entries"]], [("merge", 1), ("squash", 2), ("direct", None)])
        for e in m["entries"]:
            self.assertEqual(e["parents"], out(self.repo, "rev-parse", f"{e['sha']}^@").split())
            self.assertEqual(e["subject"], out(self.repo, "log", "-1", "--format=%s", e["sha"]))
        self.assertEqual(m["prev_sha"], out(self.repo, "rev-parse", "v1^{commit}"))
        self.assertEqual(m["new_sha"], out(self.repo, "rev-parse", "v2^{commit}"))
        self.assertNotIn(self.pr3, [e["sha"] for e in m["entries"]])  # commit sau release không thuộc v2
        # Commit trong nhánh PR không là entry riêng: chúng đi theo merge commit.
        all_commits = out(self.repo, "rev-list", "v1..v2").splitlines()
        self.assertEqual(len(all_commits), 5)
        self.assertEqual(m["manifest_id"], compute_manifest(self.repo, "v1", "v2")["manifest_id"])

    def test_revert_plan_is_reverse_order_without_force_or_reset(self):
        self.build_release()
        m = compute_manifest(self.repo, "v1", "v2")
        plan = revert_plan(m)
        self.assertEqual([s["sha"] for s in plan], [self.direct, self.pr2, self.pr1])
        self.assertEqual(plan[-1]["argv"], ["git", "revert", "--no-edit", "-m", "1", self.pr1])
        self.assertEqual(plan[0]["argv"], ["git", "revert", "--no-edit", self.direct])
        for s in plan:
            self.assertEqual(s["argv"][:2], ["git", "revert"])
            self.assertFalse({"--force", "-f", "reset", "push", "--hard"} & set(s["argv"]))

    def test_tag_validation(self):
        self.build_release()
        git(self.repo, "switch", "-q", "-c", "lech", "v1")
        self.commit("nhánh lệch", **{"x.txt": "x\n"})
        self.tag("lech-tag")
        git(self.repo, "switch", "-q", "main")
        cases = [("v1", "khong-co"), ("main", "v2"), ("v1", "v1"), ("lech-tag", "v2"), ("-x", "v2"),
                 ("v1..v2", "v2"), ("v1", "v2 ")]
        for prev, new in cases:
            with self.subTest(prev=prev, new=new), self.assertRaises(ManifestError):
                compute_manifest(self.repo, prev, new)

    def test_cli(self):
        self.build_release()
        import contextlib
        import io

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(manifest_mod.main(["v1", "v2", "--repo", str(self.repo), "--revert-plan"]), 0)
        lines = buf.getvalue().splitlines()
        self.assertEqual(len(lines), 3)
        self.assertTrue(lines[0].startswith(f"git revert --no-edit {self.direct}"))
        self.assertIn("-m 1", lines[2])
        self.assertIn("# PR #1", lines[2])
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(manifest_mod.main(["v1", "khong-co", "--repo", str(self.repo)]), 2)


class RevertTest(FixtureRepo):
    def setUp(self):
        super().setUp()
        self.remote = self.tmp / "remote.git"
        git(self.tmp, "init", "-q", "--bare", "-b", "main", str(self.remote))
        # Giống branch protection: cấm non-fast-forward (force-push) và cấm xóa nhánh.
        git(self.remote, "config", "receive.denyNonFastForwards", "true")
        git(self.remote, "config", "receive.denyDeletes", "true")

    def apply_plan(self, plan) -> list[subprocess.CompletedProcess]:
        results = []
        for step in plan:
            argv = step["argv"]
            self.assertEqual(argv[0], "git")
            res = git(self.repo, *argv[1:], check=False)
            results.append(res)
            if res.returncode != 0:
                break
        return results

    def test_revert_needs_no_force_push_and_keeps_commits_after_release(self):
        self.build_release()
        git(self.repo, "remote", "add", "origin", str(self.remote))
        git(self.repo, "push", "-q", "origin", "main", "--tags")
        old_main = out(self.remote, "rev-parse", "main")
        self.assertEqual(old_main, self.pr3)

        # Runbook: nhánh revert từ origin/main HIỆN TẠI (không phải từ tag), revert từng PR
        # theo thứ tự ngược manifest.
        m = compute_manifest(self.repo, "v1", "v2")
        git(self.repo, "fetch", "-q", "origin")
        git(self.repo, "switch", "-q", "-c", "revert/v2", "origin/main")
        results = self.apply_plan(revert_plan(m))
        self.assertTrue(all(r.returncode == 0 for r in results), [r.stderr for r in results])
        self.assertEqual(len(results), 3)

        # Đẩy nhánh và "merge PR" (fast-forward main) KHÔNG có --force, remote cấm non-ff.
        git(self.repo, "push", "-q", "origin", "revert/v2")
        git(self.repo, "push", "-q", "origin", "revert/v2:main")
        new_main = out(self.remote, "rev-parse", "main")
        self.assertEqual(new_main, out(self.repo, "rev-parse", "HEAD"))
        self.assertEqual(git(self.remote, "merge-base", "--is-ancestor", old_main, new_main, check=False).returncode, 0)

        # Lịch sử không bị viết lại: mọi commit của release và sau release vẫn còn.
        for sha in [e["sha"] for e in m["entries"]] + [self.pr3]:
            self.assertEqual(git(self.remote, "merge-base", "--is-ancestor", sha, new_main, check=False).returncode, 0)
        self.assertEqual(int(out(self.remote, "rev-list", "--count", f"{old_main}..{new_main}")), 3)

        # Nội dung: thay đổi của v2 đã bị gỡ; commit sau release (PR #3) được giữ.
        tree = {p: out(self.remote, "show", f"{new_main}:{p}") for p in
                out(self.remote, "ls-tree", "-r", "--name-only", new_main).splitlines()}
        self.assertEqual(tree["app.txt"], "line1\nline2")
        self.assertEqual(tree["c.txt"], "C sau release")
        for gone in ("a.txt", "a2.txt", "notes.txt"):
            self.assertNotIn(gone, tree)
        # So với v1, chỉ còn khác đúng các file của PR #3 (merge sau release).
        self.assertEqual(out(self.remote, "diff", "v1", new_main, "--name-only").splitlines(), ["c.txt", "f3.log"])

    def test_remote_really_rejects_force_push(self):
        # Chứng minh remote fixture thật sự cấm viết lại lịch sử (bài test trên không "ăn may").
        self.build_release()
        git(self.repo, "remote", "add", "origin", str(self.remote))
        git(self.repo, "push", "-q", "origin", "main", "--tags")
        res = git(self.repo, "push", "-q", "--force", "origin", "v1^{commit}:refs/heads/main", check=False)
        self.assertNotEqual(res.returncode, 0)
        self.assertEqual(out(self.remote, "rev-parse", "main"), self.pr3)

    def test_revert_conflict_stops_and_aborts_cleanly_then_freezes(self):
        self.commit("base", **{"app.txt": "x\n"})
        self.tag("v1")
        bad = self.commit("Sửa app (#1)", **{"app.txt": "y\n"})
        self.tag("v2")
        self.commit("Sửa app lần nữa (#2)", **{"app.txt": "z\n"})  # sau release, cùng dòng
        head = out(self.repo, "rev-parse", "HEAD")
        m = compute_manifest(self.repo, "v1", "v2")
        git(self.repo, "switch", "-q", "-c", "revert/v2")
        results = self.apply_plan(revert_plan(m))
        self.assertNotEqual(results[-1].returncode, 0)  # conflict: git dừng, không tự giải
        conflicted = out(self.repo, "diff", "--name-only", "--diff-filter=U").splitlines()
        self.assertEqual(conflicted, ["app.txt"])
        git(self.repo, "revert", "--abort")
        self.assertEqual(out(self.repo, "rev-parse", "HEAD"), head)
        self.assertEqual(out(self.repo, "status", "--porcelain"), "")
        self.assertEqual((self.repo / "app.txt").read_text(encoding="utf-8"), "z\n")

        # Đóng băng + incident theo runbook (incident.md): trạng thái frozen, có conflict.
        incident = {
            "v": 1, "incident_id": "INC-20261026-01", "opened_at": NOW, "opened_by": "user",
            "status": "frozen", "severity": "sev2", "trigger": "revert_conflict",
            "summary": "revert v2 conflict ở app.txt", "github_issue": None,
            "release": {"prev_tag": "v1", "new_tag": "v2", "manifest_id": m["manifest_id"]},
            "readonly_check": None, "killswitch_engaged": True,
            "revert": {"status": "conflict", "branch": "revert/v2", "pr": None, "reverted": [],
                       "conflict": {"sha": bad, "files": conflicted}},
            "timeline": [{"ts": NOW, "actor": "user", "note": "git revert conflict; --abort; kill-switch bật"}],
        }
        common.validate("incident", incident)


if __name__ == "__main__":
    unittest.main()
