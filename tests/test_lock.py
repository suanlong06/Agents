"""Lock PID + start time (R6): chỉ dọn khi process sở hữu đã chết, không dọn theo tuổi."""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from orchestrator.config import REPO_ROOT
from orchestrator.lock import (
    LockHeld,
    LockLost,
    PidLock,
    ProcessIdentity,
    break_if_dead,
    owner_alive,
    probe_process,
    read_lock,
)

ANCIENT = 946684800  # 2000-01-01: lock "rất cũ" vẫn không được dọn nếu chủ còn sống


def spawn_sleeper(seconds: int = 60) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", f"import time; time.sleep({seconds})"])


def dead_identity() -> ProcessIdentity:
    """Định danh của một process thật vừa chết."""
    p = spawn_sleeper()
    ident = ProcessIdentity.of(p.pid)
    p.kill()
    p.wait()
    return ident


class TmpDirTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="harness-lock-")).resolve()
        self.lock_path = self.tmp / "locks" / "x.lock"
        self._procs: list[subprocess.Popen] = []

    def tearDown(self):
        for p in self._procs:
            if p.poll() is None:
                p.kill()
            p.wait()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def sleeper(self) -> tuple[subprocess.Popen, ProcessIdentity]:
        p = spawn_sleeper()
        self._procs.append(p)
        return p, ProcessIdentity.of(p.pid)


class ProcessIdentityTest(TmpDirTestCase):
    def test_current_process_is_alive(self):
        me = ProcessIdentity.current()
        self.assertEqual(me.pid, os.getpid())
        self.assertTrue(me.alive())
        self.assertEqual(ProcessIdentity.of(os.getpid()), me)

    def test_live_child_is_alive_and_dies(self):
        p, ident = self.sleeper()
        self.assertTrue(ident.alive())
        p.kill()
        p.wait()  # Popen vẫn giữ handle: OpenProcess còn được nhưng process đã thoát
        self.assertFalse(ident.alive())

    def test_reused_pid_with_different_start_time_is_dead(self):
        _p, ident = self.sleeper()
        self.assertFalse(ProcessIdentity(ident.pid, "1999-01-01T00:00:00.0000000Z").alive())
        self.assertFalse(ProcessIdentity(os.getpid(), "1999-01-01T00:00:00.0000000Z").alive())

    def test_nonexistent_pids_are_dead(self):
        for pid in (0, -1, 2**31 - 1):
            self.assertFalse(probe_process(pid).exists, pid)

    def test_unreadable_start_time_counts_as_alive(self):
        # Fail-closed: không đọc được start time thì không dám dọn.
        self.assertTrue(owner_alive({}))
        self.assertTrue(owner_alive({"pid": "x", "proc_start": "y"}))
        self.assertTrue(owner_alive(None))

    @unittest.skipUnless(sys.platform == "win32", "PID 4 là process System trên Windows")
    def test_protected_system_process_is_not_cleaned(self):
        self.assertTrue(probe_process(4).exists)
        probe = probe_process(4)
        self.assertIsNotNone(probe.proc_start)
        self.assertTrue(owner_alive({"pid": 4, "proc_start": probe.proc_start}))


class PidLockTest(TmpDirTestCase):
    def test_acquire_writes_pid_and_start_time_then_release_removes(self):
        lock = PidLock(self.lock_path, meta={"task_id": "T-1"}).acquire()
        rec = read_lock(self.lock_path)
        me = ProcessIdentity.current()
        self.assertEqual((rec["pid"], rec["proc_start"], rec["task_id"]), (me.pid, me.proc_start, "T-1"))
        lock.release()
        self.assertFalse(self.lock_path.exists())

    def test_reacquire_by_same_owner_is_idempotent(self):
        PidLock(self.lock_path).acquire()
        PidLock(self.lock_path).acquire()  # replay của chính process này
        self.assertTrue(self.lock_path.exists())

    def test_live_owner_lock_is_not_cleaned_even_if_ancient(self):
        _p, ident = self.sleeper()
        PidLock(self.lock_path, owner=ident).acquire()
        os.utime(self.lock_path, (ANCIENT, ANCIENT))
        before = self.lock_path.read_bytes()

        with self.assertRaises(LockHeld) as cm:
            PidLock(self.lock_path).acquire()
        self.assertEqual(cm.exception.holder["pid"], ident.pid)
        self.assertIsNone(break_if_dead(self.lock_path))
        self.assertEqual(self.lock_path.read_bytes(), before)

    def test_dead_owner_lock_is_cleaned_and_taken_over(self):
        dead = dead_identity()
        PidLock(self.lock_path, owner=dead).acquire()
        broken = []
        lock = PidLock(self.lock_path, on_break=broken.append).acquire()
        self.assertEqual([b["pid"] for b in broken], [dead.pid])
        self.assertEqual(read_lock(self.lock_path)["pid"], os.getpid())
        lock.release()

    def test_reused_pid_lock_is_cleaned(self):
        # PID đang sống (chính process test) nhưng start time trong lock là của process cũ.
        PidLock(self.lock_path, owner=ProcessIdentity(os.getpid(), "2001-01-01T00:00:00.0000000Z")).acquire()
        holder = break_if_dead(self.lock_path)
        self.assertEqual(holder["proc_start"], "2001-01-01T00:00:00.0000000Z")
        self.assertFalse(self.lock_path.exists())

    def test_fresh_dead_lock_is_cleaned_regardless_of_age(self):
        PidLock(self.lock_path, owner=dead_identity()).acquire()
        os.utime(self.lock_path, None)  # vừa ghi xong: tuổi không quan trọng
        self.assertIsNotNone(break_if_dead(self.lock_path))

    def test_corrupt_lock_is_never_cleaned(self):
        self.lock_path.parent.mkdir(parents=True)
        self.lock_path.write_bytes(b"{not json")
        os.utime(self.lock_path, (ANCIENT, ANCIENT))
        with self.assertRaises(LockHeld):
            PidLock(self.lock_path).acquire()
        self.assertIsNone(break_if_dead(self.lock_path))
        self.assertEqual(self.lock_path.read_bytes(), b"{not json")

    def test_on_break_failure_keeps_the_stale_lock(self):
        PidLock(self.lock_path, owner=dead_identity()).acquire()

        def boom(_holder):
            raise OSError("không dọn được tài nguyên")

        with self.assertRaises(OSError):
            break_if_dead(self.lock_path, on_break=boom)
        self.assertTrue(self.lock_path.exists())

    def test_release_refuses_to_delete_someone_elses_lock(self):
        lock = PidLock(self.lock_path).acquire()
        _p, other = self.sleeper()
        self.lock_path.write_text(json.dumps({"pid": other.pid, "proc_start": other.proc_start}))
        with self.assertRaises(LockLost):
            lock.release()
        self.assertEqual(read_lock(self.lock_path)["pid"], other.pid)

    def test_racing_processes_only_one_wins_a_dead_lock(self):
        PidLock(self.lock_path, owner=dead_identity()).acquire()
        script = (
            "import sys, time\n"
            "from orchestrator.lock import PidLock, LockHeld\n"
            "try:\n"
            "    l = PidLock(sys.argv[1]).acquire()\n"
            "except LockHeld:\n"
            "    print('HELD'); sys.exit(0)\n"
            "print('OK', flush=True); time.sleep(3); l.release()\n"
        )
        procs = [subprocess.Popen([sys.executable, "-c", script, str(self.lock_path)], cwd=REPO_ROOT,
                                  stdout=subprocess.PIPE, text=True) for _ in range(4)]
        outs = [p.communicate(timeout=60)[0].strip() for p in procs]
        self.assertEqual(sorted(outs), ["HELD", "HELD", "HELD", "OK"], outs)


class NeverByAgeTest(unittest.TestCase):
    def test_lock_and_worktree_cleanup_never_read_file_age(self):
        pattern = re.compile(r"st_mtime|st_ctime|st_atime|getmtime|getctime|getatime")
        for mod in ("lock.py", "worktree.py"):
            src = (REPO_ROOT / "orchestrator" / mod).read_text(encoding="utf-8")
            self.assertIsNone(pattern.search(src), f"{mod} không được dọn theo tuổi file")


if __name__ == "__main__":
    unittest.main()
