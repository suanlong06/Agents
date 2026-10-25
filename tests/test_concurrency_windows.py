"""Tranh chấp nhiều tiến trình (R3): reader giữ file không làm mất cập nhật, hoặc báo lỗi rõ.

Trên Windows, ``open()`` của Python không cấp FILE_SHARE_DELETE nên một reader đang
mở ``state.json`` làm ``os.replace`` lỗi ``PermissionError`` (WinError 5/32).
"""

import json
import os
import subprocess
import sys
import threading
import time
import unittest

from orchestrator.audit import AuditLog
from orchestrator.config import REPO_ROOT
from orchestrator.fold import Orchestrator
from orchestrator.fsio import RetryPolicy, StateWriteError, WriterBusy, atomic_write_bytes
from orchestrator.state import StateStore
from tests.helpers import StateDirTestCase, make_config

WINDOWS = sys.platform == "win32"


class AtomicWriteRetryTest(StateDirTestCase):
    """Chạy được trên mọi OS: giả lập reader bằng ``replace`` ném PermissionError."""

    def test_retry_until_reader_lets_go(self):
        self.state_dir.mkdir(parents=True, exist_ok=True)
        target = self.state_dir / "x.json"
        target.write_bytes(b"old")
        calls = []

        def flaky_replace(src, dst):
            calls.append(1)
            if len(calls) < 3:
                raise PermissionError(32, "file đang bị process khác giữ")
            os.replace(src, dst)

        n = atomic_write_bytes(target, b"new", RetryPolicy(5, 0.0, 0.0), sleep=lambda s: None,
                               replace=flaky_replace)
        self.assertEqual((n, target.read_bytes()), (3, b"new"))
        self.assertEqual(sorted(p.name for p in self.state_dir.iterdir() if p.is_file()), ["x.json"])

    def test_give_up_keeps_old_content_and_raises(self):
        self.state_dir.mkdir(parents=True, exist_ok=True)
        target = self.state_dir / "x.json"
        target.write_bytes(b"old")

        def always_locked(src, dst):
            raise PermissionError(5, "Access is denied")

        with self.assertRaises(StateWriteError):
            atomic_write_bytes(target, b"new", RetryPolicy(3, 0.0, 0.0), sleep=lambda s: None,
                               replace=always_locked)
        self.assertEqual(target.read_bytes(), b"old")
        self.assertEqual(sorted(p.name for p in self.state_dir.iterdir() if p.is_file()), ["x.json"])


@unittest.skipUnless(WINDOWS, "khóa file kiểu Windows")
class ReaderHoldsStateFileTest(StateDirTestCase):
    def fast_retry_config(self, attempts):
        return make_config(self.state_dir, atomic_write={
            "max_attempts": attempts, "backoff_initial_s": 0.02, "backoff_max_s": 0.1})

    def test_reader_released_in_time_loses_no_update(self):
        self.agent.emit("task_created", "T-1")
        orch = self.open_orch(self.fast_retry_config(40))
        orch.fold(self.sources())
        self.agent.walk("T-1", "spec_draft")
        fh = open(orch.store.path, "rb")
        threading.Timer(0.3, fh.close).start()
        orch.fold(self.sources())
        self.assertTrue(fh.closed)
        disk = json.loads(orch.store.path.read_text(encoding="utf-8"))
        self.assertEqual(disk["tasks"]["T-1"]["state"], "spec_draft")
        self.assertEqual(orch.health["status"], "ok")

    def test_reader_never_releases_reports_error_and_offset_does_not_advance(self):
        self.agent.emit("task_created", "T-1")
        orch = self.open_orch(self.fast_retry_config(3))
        orch.fold(self.sources())
        before = orch.store.path.read_bytes()
        failures = []
        orch.on_write_failure = failures.append
        self.agent.walk("T-1", "spec_draft")
        with open(orch.store.path, "rb"):
            with self.assertRaises(StateWriteError):
                orch.fold(self.sources())
        self.assertEqual(orch.store.path.read_bytes(), before)
        self.assertEqual(orch.health["status"], "error")
        self.assertEqual(len(failures), 1)
        errors = [r for r in AuditLog.read(orch.audit.path) if r["action"] == "state.save"]
        self.assertEqual(errors[-1]["result"], "error")
        # Offset không tiến: lần fold sau (reader đã nhả) áp đúng event còn lại, một lần.
        res = orch.fold(self.sources())
        self.assertEqual(res.applied, 1)
        self.assertEqual(orch.snapshot["tasks"]["T-1"]["state"], "spec_draft")
        self.assertEqual(orch.health["status"], "ok")


READER = """
import json, sys, time
path, stop = sys.argv[1], time.time() + float(sys.argv[2])
reads = bad = 0
while time.time() < stop:
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        continue
    reads += 1
    try:
        json.loads(data)
    except ValueError:
        bad += 1
print(reads, bad)
"""


class MultiProcessTest(StateDirTestCase):
    def test_reader_process_hammering_state_loses_no_update(self):
        self.agent.emit("task_created", "T-1")
        cfg = make_config(self.state_dir, atomic_write={
            "max_attempts": 200, "backoff_initial_s": 0.005, "backoff_max_s": 0.05})
        orch = self.open_orch(cfg)
        orch.fold(self.sources())
        reader = subprocess.Popen([sys.executable, "-c", READER, str(orch.store.path), "4"],
                                  stdout=subprocess.PIPE, cwd=REPO_ROOT)
        try:
            for i in range(30):
                self.agent.call("T-1", f"c-{i}", inp=1000, out=0)
                orch.fold(self.sources())
        finally:
            out, _ = reader.communicate(timeout=60)
        reads, bad = map(int, out.split())
        self.assertGreater(reads, 0)
        self.assertEqual(bad, 0, "reader không bao giờ thấy snapshot ghi dở")
        disk = json.loads(orch.store.path.read_text(encoding="utf-8"))
        self.assertEqual(disk["watermarks"][f"implementer|4242|{self.agent.proc_start}"], 31)
        self.assertEqual(orch.ledger.totals("T-1")["records"], 30)

    def test_second_orchestrator_process_is_refused(self):
        orch = self.open_orch()
        script = (
            "import sys\n"
            "from orchestrator.config import load_config\n"
            "from orchestrator.fold import Orchestrator\n"
            "from orchestrator.fsio import WriterBusy\n"
            "try:\n"
            "    Orchestrator(load_config()).open()\n"
            "except WriterBusy:\n"
            "    sys.exit(3)\n"
            "sys.exit(0)\n"
        )
        env = dict(os.environ, HARNESS_STATE_DIR=str(self.state_dir))
        proc = subprocess.run([sys.executable, "-c", script], cwd=REPO_ROOT, env=env,
                              capture_output=True, timeout=60)
        self.assertEqual(proc.returncode, 3, proc.stderr.decode(errors="replace"))
        orch.close()
        proc = subprocess.run([sys.executable, "-c", script], cwd=REPO_ROOT, env=env,
                              capture_output=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode(errors="replace"))

    def test_second_orchestrator_in_same_process_is_refused(self):
        self.open_orch()
        with self.assertRaises(WriterBusy):
            Orchestrator(self.config).open()


if __name__ == "__main__":
    unittest.main()
