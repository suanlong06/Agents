"""Kiểm tra read-only (bước 11; PR-4a; SPEC mục 8 câu 14).

A. ``verify_readonly``: hành động đề xuất chỉ đạt khi không ghi gì — chặn trong process
   (audit hook) VÀ so ảnh hệ thống file; ghi, đổi tên, xóa, tạo thư mục, chạy process, mở
   mạng đều bị bắt, kể cả khi hành động nuốt lỗi; process con được phép mà ghi file thì lớp
   so ảnh bắt.
B. ``post_release_check``: smoke + health + so lỗi audit sau release, chạy khi kill-switch
   bật, giữ ``release.lock``, và chính nó không ghi gì vào ``state_dir``.
"""

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

from orchestrator.config import REPO_ROOT
from orchestrator.fold import Orchestrator
from orchestrator.heartbeat import HeartbeatMonitor
from orchestrator.killswitch import KillSwitch
from orchestrator.lock import PidLock, ProcessIdentity
from release import readonly_check as rc
from release.readonly_check import (
    ReadOnlyCheckError,
    ReadOnlyViolation,
    current_audit_seq,
    git_readonly_command,
    post_release_check,
    snapshot,
    verify_readonly,
)
from tests.helpers import Emitter, make_config
from tests.killswitch_fixtures import write_killswitch


class Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="harness-ro-")).resolve()
        self.dir = self.tmp / "watched"
        self.dir.mkdir()
        (self.dir / "a.txt").write_text("a", encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class VerifyReadonlyTest(Tmp):
    def assertBlocked(self, action, kind="write", **kw):
        rep = verify_readonly(action, watch=[self.dir], **kw)
        self.assertFalse(rep.ok, rep)
        self.assertFalse(rep.readonly_ok)
        self.assertIn(kind, {v.kind for v in rep.violations}, rep.violations)
        return rep

    def test_pure_read_passes(self):
        def read():
            return sorted(p.name for p in self.dir.iterdir()), (self.dir / "a.txt").read_text(encoding="utf-8")

        rep = verify_readonly(read, watch=[self.dir])
        self.assertTrue(rep.ok, rep)
        self.assertEqual(rep.result, (["a.txt"], "a"))
        self.assertEqual((rep.violations, rep.fs_changes, rep.error), ([], [], None))

    def test_writes_are_blocked_before_they_happen(self):
        cases = {
            "open w": lambda: open(self.dir / "b.txt", "w"),
            "open a": lambda: open(self.dir / "a.txt", "a"),
            "open r+": lambda: open(self.dir / "a.txt", "r+"),
            "open x": lambda: open(self.dir / "n.txt", "xb"),
            "write_text": lambda: (self.dir / "a.txt").write_text("đè", encoding="utf-8"),
            "os.open O_CREAT": lambda: os.open(self.dir / "c.txt", os.O_CREAT | os.O_WRONLY),
            "os.replace": lambda: os.replace(self.dir / "a.txt", self.dir / "z.txt"),
            "os.remove": lambda: os.remove(self.dir / "a.txt"),
            "mkdir": lambda: (self.dir / "sub").mkdir(),
            "rmtree": lambda: shutil.rmtree(self.dir),
            "copyfile": lambda: shutil.copyfile(self.dir / "a.txt", self.dir / "copy.txt"),
            "touch": lambda: (self.dir / "t.txt").touch(),
            "utime": lambda: os.utime(self.dir / "a.txt"),
            "chmod": lambda: os.chmod(self.dir / "a.txt", 0o444),
            "tempfile": lambda: tempfile.mkstemp(dir=self.tmp),
        }
        for name, action in cases.items():
            with self.subTest(name):
                self.assertBlocked(action)
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["a.txt"])
        self.assertEqual((self.dir / "a.txt").read_text(encoding="utf-8"), "a")

    def test_swallowed_violation_still_fails(self):
        def sneaky():
            try:
                (self.dir / "b.txt").write_text("x", encoding="utf-8")
            except PermissionError:
                return "giả vờ ổn"
            return "đã ghi"

        rep = self.assertBlocked(sneaky)
        self.assertEqual(rep.result, "giả vờ ổn")
        self.assertIsNone(rep.error)
        self.assertTrue(issubclass(ReadOnlyViolation, PermissionError))

    def test_processes_are_blocked_unless_allowed(self):
        argv = ["git", "version"]
        self.assertBlocked(lambda: subprocess.run(argv, capture_output=True, stdin=subprocess.DEVNULL),
                           kind="process")
        self.assertBlocked(lambda: os.system("echo hi"), kind="process")
        rep = verify_readonly(lambda: subprocess.run(argv, capture_output=True, stdin=subprocess.DEVNULL).returncode,
                              watch=[self.dir], allow_command=git_readonly_command)
        self.assertTrue(rep.ok, rep)
        self.assertEqual(rep.result, 0)

    def test_git_readonly_allowlist(self):
        self.assertTrue(git_readonly_command(["git", "-c", "a=b", "-C", "x y", "log", "--oneline"]))
        self.assertTrue(git_readonly_command([r"C:\Program Files\Git\cmd\git.exe", "rev-parse", "HEAD"]))
        for argv in (["git", "commit", "-m", "x"], ["git", "push", "--force"], ["git", "reset", "--hard"],
                     ["git", "-C", "x"], ["git"], ["python", "-c", "1"], [], ["git", "status"],
                     ["git", "revert", "abc"], ["git", "tag", "v1"]):
            with self.subTest(argv=argv):
                self.assertFalse(git_readonly_command(argv))

    def test_git_write_command_blocked_even_with_git_allowlist(self):
        subprocess.run(["git", "init", "-q", str(self.dir)], check=True, capture_output=True)
        before = snapshot([self.dir])
        rep = verify_readonly(lambda: subprocess.run(["git", "-C", str(self.dir), "tag", "x"], capture_output=True,
                                                     stdin=subprocess.DEVNULL),
                              watch=[self.dir], allow_command=git_readonly_command)
        self.assertFalse(rep.ok)
        self.assertEqual(before, snapshot([self.dir]))

    def test_network_is_blocked_by_default(self):
        def connect():
            s = socket.socket()
            s.settimeout(0.5)
            try:
                s.connect(("127.0.0.1", 9))
            finally:
                s.close()

        self.assertBlocked(connect, kind="network")
        self.assertBlocked(lambda: urllib.request.urlopen("http://127.0.0.1:9/", timeout=0.5), kind="network")

    def test_allowed_network_still_blocks_non_get_http(self):
        req = urllib.request.Request("http://127.0.0.1:9/repos/x/y/pulls/1/merge", data=b"{}", method="PUT")
        rep = self.assertBlocked(lambda: urllib.request.urlopen(req, timeout=0.5), kind="network", allow_network=True)
        self.assertIn("PUT", rep.violations[0].detail)

    def test_child_process_write_is_caught_by_snapshot_layer(self):
        target = self.dir / "child.txt"
        code = f"open({str(target)!r}, 'w').write('1')"
        rep = verify_readonly(lambda: subprocess.run([sys.executable, "-c", code], stdin=subprocess.DEVNULL).returncode,
                              watch=[self.dir], allow_command=lambda argv: True)
        self.assertEqual(rep.violations, [])
        self.assertFalse(rep.ok)
        self.assertEqual(rep.fs_changes, [f"+ {target}"])

    def test_other_thread_write_is_blocked(self):
        errors = []

        def action():
            def worker():
                try:
                    (self.dir / "thread.txt").write_text("x", encoding="utf-8")
                except PermissionError as exc:
                    errors.append(exc)

            t = threading.Thread(target=worker)
            t.start()
            t.join()

        rep = verify_readonly(action, watch=[self.dir])
        self.assertFalse(rep.ok)
        self.assertEqual(len(errors), 1)

    def test_imports_do_not_write_bytecode_and_guard_is_released_after(self):
        pyc_flag = sys.dont_write_bytecode
        rep = verify_readonly(lambda: sys.dont_write_bytecode, watch=[self.dir])
        self.assertTrue(rep.result)
        self.assertEqual(sys.dont_write_bytecode, pyc_flag)
        (self.dir / "after.txt").write_text("ok", encoding="utf-8")  # ngoài phiên: ghi bình thường
        self.assertTrue((self.dir / "after.txt").exists())

    def test_action_error_is_reported_not_raised(self):
        rep = verify_readonly(lambda: 1 / 0, watch=[self.dir])
        self.assertFalse(rep.ok)
        self.assertTrue(rep.readonly_ok)
        self.assertIn("ZeroDivisionError", rep.error)

    def test_nested_sessions_are_refused(self):
        rep = verify_readonly(lambda: verify_readonly(lambda: None))
        self.assertIn("ReadOnlyCheckError", rep.error)
        self.assertTrue(issubclass(ReadOnlyCheckError, RuntimeError))

    def test_state_writer_is_a_write_action(self):
        # "Hành động đề xuất" là mở orchestrator (writer duy nhất của state): phải bị bắt.
        state = self.tmp / "state"
        state.mkdir()
        rep = verify_readonly(lambda: Orchestrator(make_config(state)).open(), watch=[state])
        self.assertFalse(rep.ok)
        self.assertIn("write", {v.kind for v in rep.violations})
        self.assertEqual(list(state.iterdir()), [])


class PostReleaseCheckTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="harness-ro-rel-")).resolve()
        self.state_dir = self.tmp / "state"
        self.config = make_config(self.state_dir)
        self.ks_path = self.state_dir / "killswitch.json"
        self.agent = Emitter(self.state_dir / "inbox" / "implementer.jsonl")
        self.agent.emit("task_created", "T-1", {"kind": "feature", "title": "demo"})
        self.agent.walk("T-1", "spec_draft")
        self.fold_and_publish()
        write_killswitch(self.ks_path, "on")  # runbook: kill-switch bật suốt release
        self.ks = KillSwitch(self.ks_path)
        self.since = current_audit_seq(self.state_dir)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def fold_and_publish(self):
        with Orchestrator(self.config) as orch:
            orch.fold(sorted((self.state_dir / "inbox").glob("*.jsonl")))
            HeartbeatMonitor(self.state_dir).publish(orch)

    def check(self, **kw):
        kw.setdefault("killswitch", self.ks)
        kw.setdefault("since_seq", self.since)
        return post_release_check(self.state_dir, **kw)

    def probe(self, rep, name):
        return next(p for p in rep["probes"] if p["name"] == name)

    def test_healthy_release_passes_and_check_itself_writes_nothing(self):
        self.assertGreater(self.since, 0)
        before = snapshot([self.state_dir])
        rep = self.check(lock=False)
        self.assertTrue(rep["ok"], json.dumps(rep, ensure_ascii=False, indent=1))
        self.assertTrue(rep["readonly_ok"])
        self.assertEqual([p["name"] for p in rep["probes"]], ["killswitch_engaged", "smoke", "health", "log_errors"])
        self.assertEqual(snapshot([self.state_dir]), before)  # byte-identical
        self.assertGreater(self.probe(rep, "smoke")["detail"]["schemas"], 8)

    def test_release_lock_is_held_during_the_check_and_released_after(self):
        seen = {}
        real = rc._run_probes

        def spy(state_dir, *a):
            seen["lock"] = json.loads((state_dir / "release.lock").read_text(encoding="utf-8"))
            return real(state_dir, *a)

        with mock.patch.object(rc, "_run_probes", spy):
            rep = self.check()
        self.assertTrue(rep["ok"], rep)
        self.assertEqual(seen["lock"]["purpose"], "readonly_check")
        self.assertFalse((self.state_dir / "release.lock").exists())

    def test_release_lock_held_by_another_live_process_fails(self):
        other = ProcessIdentity.of(os.getppid())
        if not other.alive():
            self.skipTest("không có process cha còn sống để giữ lock")
        with PidLock(self.state_dir / "release.lock", owner=other):
            rep = self.check()
        self.assertFalse(rep["ok"])
        self.assertIn("release.lock", rep["error"])

    def test_new_errors_in_audit_after_release_fail(self):
        # Sau release: event hỏng (chuyển trạng thái sai) → audit "rejected".
        self.agent.walk("T-1", "maintenance")
        self.fold_and_publish()
        rep = self.check(lock=False)
        self.assertFalse(rep["ok"])
        self.assertTrue(rep["readonly_ok"])
        le = self.probe(rep, "log_errors")
        self.assertFalse(le["passed"])
        self.assertEqual(le["detail"]["errors_after"], 1)
        self.assertTrue(self.check(lock=False, max_new_errors=1)["ok"])
        self.assertTrue(self.check(lock=False, since_seq=current_audit_seq(self.state_dir))["ok"])

    def test_killswitch_off_fails_the_check(self):
        write_killswitch(self.ks_path, "off")
        rep = self.check(lock=False)
        self.assertFalse(rep["ok"])
        self.assertFalse(self.probe(rep, "killswitch_engaged")["passed"])

    def test_missing_or_broken_state_fails_health(self):
        (self.state_dir / "heartbeats.json").unlink()
        rep = self.check(lock=False)
        self.assertFalse(self.probe(rep, "health")["passed"])
        (self.state_dir / "state.json").write_text("{hỏng", encoding="utf-8")
        rep = self.check(lock=False)
        self.assertFalse(rep["ok"])
        self.assertEqual(len(self.probe(rep, "health")["detail"]["problems"]), 2)

    def test_orchestrator_error_fails_health(self):
        hb = self.state_dir / "heartbeats.json"
        data = json.loads(hb.read_text(encoding="utf-8"))
        data["orchestrator"] = {"status": "error", "last_error": "WinError 5"}
        hb.write_text(json.dumps(data), encoding="utf-8")
        rep = self.check(lock=False)
        self.assertFalse(self.probe(rep, "health")["passed"])

    def test_smoke_failure_is_reported(self):
        res = rc.probe_smoke(REPO_ROOT, modules=("khong_co_module_nay",))
        self.assertFalse(res.passed)
        self.assertIn("khong_co_module_nay", res.detail["problems"][0])
        broken = self.tmp / "repo"
        (broken / "schemas").mkdir(parents=True)
        (broken / "schemas" / "x.schema.json").write_text('{"type": 12}', encoding="utf-8")
        res = rc.probe_smoke(broken, modules=())
        self.assertFalse(res.passed)
        self.assertEqual(len(res.detail["problems"]), 2)  # schema sai + thiếu harness.yaml

    def test_invalid_since_seq_raises(self):
        for bad in (-1, True, "3", None):
            with self.subTest(bad=bad), self.assertRaises(ReadOnlyCheckError):
                self.check(since_seq=bad)

    def test_cli(self):
        import contextlib
        import io

        env = {"HARNESS_STATE_DIR": str(self.state_dir)}
        with mock.patch.dict(os.environ, env):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                self.assertEqual(rc.main(["seq"]), 0)
            self.assertEqual(int(buf.getvalue()), self.since)
            # F2: --expect-tag là BẮT BUỘC.
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                rc.main(["run", "--since-seq", str(self.since)])
            # Checkout của repo test không phải tag release → không đạt.
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = rc.main(["run", "--since-seq", str(self.since), "--expect-tag", "rel-khong-co"])
            rep = json.loads(buf.getvalue())
            self.assertEqual(code, 1, rep)
            self.assertFalse(self.probe(rep, "release_tag")["passed"])
            # Probe tag đạt (giả lập checkout đúng tag) → toàn bộ đạt; probe nhận đúng REPO_ROOT + tag.
            seen = []

            def tag_ok(root, tag):
                seen.append((root, tag))
                return rc.ProbeResult("release_tag", True, {"describe": tag})

            buf = io.StringIO()
            with mock.patch.object(rc, "probe_release_tag", tag_ok), contextlib.redirect_stdout(buf):
                code = rc.main(["run", "--since-seq", str(self.since), "--expect-tag", "rel-20261026-1200"])
            rep = json.loads(buf.getvalue())
            self.assertEqual(code, 0, rep)
            self.assertTrue(rep["ok"])
            self.assertEqual(seen, [(REPO_ROOT, "rel-20261026-1200")])

    def test_release_lock_mode_requires_the_batch_lock_and_does_not_take_it(self):
        write_killswitch(self.ks_path, "off")  # batch chạy khi kill-switch TẮT
        rep = self.check(quiesce="release_lock")
        self.assertFalse(rep["ok"])
        self.assertEqual([p["name"] for p in rep["probes"]][0], "release_lock_held")
        self.assertFalse((self.state_dir / "release.lock").exists())  # không tự lấy lock
        with PidLock(self.state_dir / "release.lock", meta={"purpose": "readonly_check"}):
            self.assertFalse(self.check(quiesce="release_lock")["ok"])  # sai purpose
        with PidLock(self.state_dir / "release.lock", meta={"purpose": rc.BATCH_LOCK_PURPOSE}):
            rep = self.check(quiesce="release_lock")
            self.assertTrue(rep["ok"], json.dumps(rep, ensure_ascii=False, indent=1))
            self.assertTrue((self.state_dir / "release.lock").exists())  # vẫn là lock của batch
        with self.assertRaises(ReadOnlyCheckError):
            self.check(quiesce="khac")


class ReleaseTagProbeTest(Tmp):
    """F2: kiểm tra read-only phải chạy trên checkout của ĐÚNG tag release."""

    def git(self, *args):
        proc = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                               "-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false", "-C", str(self.repo), *args],
                              capture_output=True, stdin=subprocess.DEVNULL)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def setUp(self):
        super().setUp()
        self.repo = self.tmp / "checkout"
        self.repo.mkdir()
        self.git("init", "-q", "-b", "main")
        (self.repo / "a.txt").write_text("1\n", encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "c1")
        self.git("tag", "-a", "rel-20261026-1200", "-m", "release")
        self.git("tag", "-a", "rel-khac", "-m", "tag khác cùng commit")

    def test_exact_tag_checkout_passes(self):
        res = rc.probe_release_tag(self.repo, "rel-20261026-1200")
        self.assertTrue(res.passed, res.detail)
        self.assertEqual(res.detail["describe"], "rel-20261026-1200")

    def test_other_commit_dirty_tree_or_bad_name_fail(self):
        (self.repo / "a.txt").write_text("2\n", encoding="utf-8")
        res = rc.probe_release_tag(self.repo, "rel-20261026-1200")
        self.assertFalse(res.passed)
        self.assertTrue(res.detail["tracked_changes"])
        self.git("commit", "-q", "-am", "c2")
        self.assertFalse(rc.probe_release_tag(self.repo, "rel-20261026-1200").passed)
        self.assertFalse(rc.probe_release_tag(self.repo, "rel-*").passed)
        self.assertFalse(rc.probe_release_tag(self.tmp / "khong-phai-repo", "rel-20261026-1200").passed)

    def test_probe_runs_git_inside_the_readonly_guard(self):
        rep = verify_readonly(rc.probe_release_tag, self.repo, "rel-20261026-1200",
                              allow_command=git_readonly_command, watch=[self.repo])
        self.assertTrue(rep.ok, rep.to_dict())
        self.assertTrue(rep.result.passed)


if __name__ == "__main__":
    unittest.main()
