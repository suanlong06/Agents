"""Worktree theo task-attempt (R6): tách biệt, idempotent khi replay, chỉ dọn khi chủ đã chết."""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from orchestrator.config import REPO_ROOT, ConfigError
from orchestrator.lock import LockHeld, PidLock, ProcessIdentity, read_lock
from orchestrator.worktree import (
    ENV_WORKTREE_ROOT,
    WorktreeConflict,
    WorktreeError,
    WorktreeManager,
    agent_env,
    load_worktree_settings,
)
from tests.test_lock import ANCIENT, dead_identity, spawn_sleeper

GIT_ID = ["-c", "user.name=harness-test", "-c", "user.email=harness-test@example.invalid",
          "-c", "commit.gpgsign=false"]


def git(cwd, *args, check=True) -> str:
    proc = subprocess.run(["git", *GIT_ID, "-C", str(cwd), *args], capture_output=True, text=True,
                          stdin=subprocess.DEVNULL)
    if check and proc.returncode != 0:
        raise AssertionError(f"git {args}: {proc.stderr}")
    return proc.stdout.strip()


class RepoTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="harness-wt-")).resolve()
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        (self.repo / "README.md").write_text("hello\n", encoding="utf-8")
        git(self.repo, "add", "README.md")
        git(self.repo, "commit", "-q", "-m", "init")
        self.state_dir = self.tmp / "state"
        self.root = self.tmp / "wt"
        self.mgr = WorktreeManager(self.repo, self.root, self.state_dir)
        self._procs: list[subprocess.Popen] = []

    def tearDown(self):
        for p in self._procs:
            if p.poll() is None:
                p.kill()
            p.wait()
        subprocess.run(["git", "-C", str(self.repo), "worktree", "prune"], capture_output=True)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def sleeper(self) -> tuple[subprocess.Popen, ProcessIdentity]:
        p = spawn_sleeper()
        self._procs.append(p)
        return p, ProcessIdentity.of(p.pid)

    def admin_dirs(self) -> list[str]:
        d = self.repo / ".git" / "worktrees"
        return sorted(p.name for p in d.iterdir()) if d.is_dir() else []

    def branches(self) -> list[str]:
        return sorted(git(self.repo, "branch", "--format=%(refname:short)").splitlines())

    def index_lock(self, wt) -> Path:
        return Path(git(wt.path, "rev-parse", "--path-format=absolute", "--git-path", "index.lock"))


class NamingTest(RepoTestCase):
    def test_two_attempts_of_one_task_get_different_worktrees(self):
        a1 = self.mgr.ensure("T-1", 1)
        a2 = self.mgr.ensure("T-1", 2)
        self.assertNotEqual(a1.path, a2.path)
        self.assertEqual((a1.path.name, a1.branch), ("T-1-1", "T-1-1"))
        self.assertEqual((a2.path.name, a2.branch), ("T-1-2", "T-1-2"))
        self.assertEqual(a1.path.parent, self.mgr.root)
        self.assertEqual(git(a1.path, "branch", "--show-current"), "T-1-1")
        self.assertEqual(git(a2.path, "branch", "--show-current"), "T-1-2")
        # Ghi ở attempt 1 không thấy ở attempt 2: không chia sẻ cây làm việc.
        (a1.path / "only-in-1.txt").write_text("x")
        self.assertFalse((a2.path / "only-in-1.txt").exists())
        self.assertEqual(self.admin_dirs(), ["T-1-1", "T-1-2"])

    def test_bad_task_or_attempt_is_refused(self):
        for task, attempt in (("../x", 1), ("a/b", 1), ("", 1), ("T-1", 0), ("T-1", True), ("T-1", "1")):
            with self.assertRaises(WorktreeError, msg=(task, attempt)):
                self.mgr.name_for(task, attempt)
        with self.assertRaises(WorktreeError):
            self.mgr.ensure("-bad", 1)  # tên nhánh git không hợp lệ

    def test_names_are_injective(self):
        names = {self.mgr.name_for(t, a) for t, a in [("T-1", 11), ("T-1-1", 1), ("T-11", 1), ("T", 11)]}
        self.assertEqual(len(names), 4)


class ReplayTest(RepoTestCase):
    def test_replayed_ensure_creates_no_duplicate_state(self):
        first = self.mgr.ensure("T-1", 1)
        again = self.mgr.ensure("T-1", 1)
        self.assertTrue(first.created)
        self.assertFalse(again.created)
        self.assertEqual(first.path, again.path)
        self.assertEqual(self.admin_dirs(), ["T-1-1"])
        self.assertEqual(self.branches(), ["T-1-1", "main"])
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["T-1-1"])
        self.assertEqual(len(self.mgr.registered()), 2)  # repo chính + T-1-1

    def test_replay_after_worktree_dir_vanished_reuses_the_same_name(self):
        wt = self.mgr.ensure("T-1", 1)
        (wt.path / "work.txt").write_text("x")
        git(wt.path, "add", "work.txt")
        git(wt.path, "commit", "-q", "-m", "work")
        shutil.rmtree(wt.path)
        again = self.mgr.ensure("T-1", 1)
        self.assertTrue(again.created)
        self.assertEqual(self.admin_dirs(), ["T-1-1"], "không được sinh .git/worktrees/T-1-11")
        self.assertTrue((again.path / "work.txt").exists(), "nhánh cũ được checkout lại, commit không mất")

    def test_existing_unknown_dir_is_never_clobbered(self):
        path = self.root / "T-1-1"
        path.mkdir(parents=True)
        (path / "precious.txt").write_text("keep")
        with self.assertRaises(WorktreeConflict):
            self.mgr.ensure("T-1", 1)
        self.assertEqual((path / "precious.txt").read_text(), "keep")

    def test_worktree_of_another_branch_is_not_shared(self):
        wt = self.mgr.ensure("T-1", 1)
        git(wt.path, "switch", "-q", "-c", "other")
        with self.assertRaises(WorktreeConflict):
            self.mgr.ensure("T-1", 1)

    @unittest.skipUnless(sys.platform == "win32", "NTFS không phân biệt hoa thường")
    def test_case_colliding_task_ids_do_not_share_a_worktree(self):
        self.mgr.ensure("T-1", 1)
        with self.assertRaises(WorktreeConflict):
            self.mgr.ensure("t-1", 1)


class ClaimAndReapTest(RepoTestCase):
    def test_live_owner_blocks_second_claim(self):
        _p, other = self.sleeper()
        self.mgr.claim("T-1", 1, owner=other)
        with self.assertRaises(LockHeld):
            self.mgr.claim("T-1", 1)
        self.mgr.claim("T-1", 2).release()  # attempt khác: lock khác

    def test_live_owner_worktree_is_not_reaped_even_if_ancient(self):
        _p, other = self.sleeper()
        wt = self.mgr.ensure("T-1", 1)
        lock = self.mgr.claim("T-1", 1, owner=other)
        idx = self.index_lock(wt)
        idx.write_text("")
        for f in (lock.path, idx):
            os.utime(f, (ANCIENT, ANCIENT))
        report = self.mgr.reap()
        self.assertEqual([(r["name"], r["action"]) for r in report], [("T-1-1", "kept")])
        self.assertTrue(lock.path.exists())
        self.assertTrue(idx.exists())
        self.assertTrue(wt.path.exists())

    def test_dead_owner_lock_index_lock_and_worktree_are_reaped(self):
        p, owner = self.sleeper()
        wt = self.mgr.ensure("T-1", 1)
        lock = self.mgr.claim("T-1", 1, owner=owner)
        (wt.path / "work.txt").write_text("x")
        git(wt.path, "add", "work.txt")
        git(wt.path, "commit", "-q", "-m", "work")
        idx = self.index_lock(wt)
        idx.write_text("")  # agent chết giữa lúc git đang giữ index
        p.kill()
        p.wait()

        report = self.mgr.reap()
        self.assertEqual([(r["name"], r["action"], r.get("worktree")) for r in report],
                         [("T-1-1", "reaped", "removed")])
        self.assertFalse(lock.path.exists())
        self.assertFalse(idx.exists())
        self.assertFalse(wt.path.exists())
        self.assertEqual(self.admin_dirs(), [])
        self.assertIn("T-1-1", self.branches(), "nhánh được giữ để không mất commit")
        self.assertEqual(self.mgr.reap(), [])

    def test_reused_pid_with_different_start_time_is_reaped(self):
        wt = self.mgr.ensure("T-1", 1)
        self.mgr.claim("T-1", 1, owner=ProcessIdentity(os.getpid(), "2001-01-01T00:00:00.0000000Z"))
        report = self.mgr.reap()
        self.assertEqual([r["action"] for r in report], ["reaped"])
        self.assertFalse(wt.path.exists())

    def test_claim_takes_over_dead_lock_and_clears_index_lock(self):
        wt = self.mgr.ensure("T-1", 1)
        self.mgr.claim("T-1", 1, owner=dead_identity())
        idx = self.index_lock(wt)
        idx.write_text("")
        lock = self.mgr.claim("T-1", 1)
        self.assertEqual(read_lock(lock.path)["pid"], os.getpid())
        self.assertFalse(idx.exists())
        self.assertTrue(wt.path.exists(), "tiếp quản lock không xóa worktree")
        git(wt.path, "status")  # git dùng được lại

    def test_reap_never_touches_paths_named_inside_the_lock_file(self):
        # Lock giả trỏ "worktree" vào repo chính: reap chỉ suy path từ tên file lock.
        PidLock(self.mgr.lock_path("T-9", 1), owner=dead_identity(),
                meta={"worktree": str(self.repo)}).acquire()
        idx = self.repo / ".git" / "index.lock"
        idx.write_text("")
        report = self.mgr.reap()
        self.assertEqual([(r["action"], r.get("worktree")) for r in report], [("reaped", "pruned")])
        self.assertTrue(idx.exists())
        self.assertTrue((self.repo / "README.md").exists())
        idx.unlink()

    def test_unregistered_dir_with_dead_lock_is_kept(self):
        path = self.root / "T-1-1"
        path.mkdir(parents=True)
        PidLock(self.mgr.lock_path("T-1", 1), owner=dead_identity()).acquire()
        report = self.mgr.reap()
        self.assertEqual(report[0].get("worktree"), "kept-unregistered-dir")
        self.assertTrue(path.exists())


class GcAutoTest(RepoTestCase):
    def gc_auto(self, cwd=None, env=None):
        proc = subprocess.run(["git", "-C", str(cwd or self.repo), "config", "--get", "gc.auto"],
                              capture_output=True, text=True, env=env)
        return proc.stdout.strip() or None

    def test_gc_auto_is_off_while_agent_runs_and_restored_after(self):
        git(self.repo, "config", "--local", "gc.auto", "1234")
        with self.mgr.run("T-1", 1) as run:
            self.assertEqual(self.gc_auto(), "0")
            self.assertEqual(self.gc_auto(run.worktree.path), "0")
            self.assertEqual(self.gc_auto(run.worktree.path, env=run.env), "0")
            with self.mgr.run("T-1", 2):
                pass
            self.assertEqual(self.gc_auto(), "0", "còn agent chạy thì chưa khôi phục")
        self.assertEqual(self.gc_auto(), "1234")
        self.assertFalse(self.mgr.lock_path("T-1", 1).exists())

    def test_gc_auto_unset_is_restored_to_unset(self):
        with self.mgr.run("T-1", 1):
            self.assertEqual(self.gc_auto(), "0")
        self.assertIsNone(self.gc_auto())

    def test_agent_env_forces_gc_auto_off_even_if_repo_says_otherwise(self):
        git(self.repo, "config", "--local", "gc.auto", "50")
        env = agent_env({"PATH": os.environ.get("PATH", ""), "SYSTEMROOT": os.environ.get("SYSTEMROOT", "")})
        self.assertEqual(self.gc_auto(env=env), "0")

    def test_gc_auto_is_restored_by_reap_after_agent_crash(self):
        git(self.repo, "config", "--local", "gc.auto", "77")
        p, owner = self.sleeper()
        self.mgr.ensure("T-1", 1)
        self.mgr.claim("T-1", 1, owner=owner)
        self.mgr.suspend_gc()
        self.assertEqual(self.mgr.reap()[0]["action"], "kept")
        self.assertEqual(self.gc_auto(), "0")
        p.kill()
        p.wait()
        self.mgr.reap()
        self.assertEqual(self.gc_auto(), "77")


class SettingsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="harness-wtcfg-")).resolve()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_default_root_comes_from_config(self):
        s = load_worktree_settings(env={"LOCALAPPDATA": str(self.tmp)})
        self.assertEqual(s.root, (self.tmp / "hw").resolve())
        self.assertEqual(s.max_root_len, 80)

    def test_env_overrides_root(self):
        s = load_worktree_settings(env={ENV_WORKTREE_ROOT: str(self.tmp / "w")})
        self.assertEqual(s.root, (self.tmp / "w").resolve())

    def test_root_inside_repo_or_relative_is_refused(self):
        with self.assertRaises(ConfigError):
            load_worktree_settings(env={ENV_WORKTREE_ROOT: str(REPO_ROOT / "wt")})
        with self.assertRaises(ConfigError):
            load_worktree_settings(env={ENV_WORKTREE_ROOT: "relative/wt"})

    def test_too_long_root_is_refused(self):
        repo = self.tmp / "repo"
        repo.mkdir()
        git(repo, "init", "-q")
        with self.assertRaises(WorktreeError):
            WorktreeManager(repo, self.tmp / ("x" * 40), self.tmp / "state", max_root_len=40)
        with self.assertRaises(WorktreeError):
            WorktreeManager(repo, repo / "wt", self.tmp / "state")


if __name__ == "__main__":
    unittest.main()
