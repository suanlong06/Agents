"""Event fold idempotent và crash-replay (R3)."""

import json
import os
import subprocess
import sys
import unittest
from collections import Counter

from orchestrator.audit import AuditLog
from orchestrator.config import REPO_ROOT
from orchestrator.state import StateStore
from tests.helpers import PRICES_FIXTURE, Emitter, StateDirTestCase


class SimulatedCrash(BaseException):
    """Giả lập process chết: không phải Exception nên không bị code nào nuốt."""


def seed_task_with_calls(agent: Emitter):
    agent.emit("task_created", "T-1", {"kind": "feature", "title": "demo"})
    agent.walk("T-1", "spec_draft", "spec_review")
    agent.call("T-1", "c-1", inp=1_000_000, out=0)   # 10 USD theo prices fixture
    agent.call("T-1", "c-2", inp=0, out=100_000)     # 5 USD


def read_disk_snapshot(state_dir):
    return json.loads((state_dir / StateStore.FILE).read_text(encoding="utf-8"))


class FoldBasicsTest(StateDirTestCase):
    def test_replaying_the_same_event_has_no_extra_effect(self):
        seed_task_with_calls(self.agent)
        orch = self.open_orch()
        first = orch.fold(self.sources())
        self.assertEqual(first.applied, 5)
        snap = json.loads(json.dumps(orch.snapshot["tasks"]))
        audit_n = len(AuditLog.read(orch.audit.path))

        # Agent gửi lại nguyên văn các dòng cũ (cùng id agent/pid/proc_start/seq).
        raw = self.inbox.read_bytes()
        with open(self.inbox, "ab") as f:
            f.write(raw)
        again = orch.fold(self.sources())
        self.assertEqual((again.applied, again.duplicates), (0, 5))
        self.assertEqual(orch.snapshot["tasks"], snap)
        self.assertEqual(orch.ledger.totals("T-1")["cost_usd"], 15.0)
        self.assertEqual(len(AuditLog.read(orch.audit.path)), audit_n)

    def test_fold_with_no_new_lines_changes_nothing(self):
        seed_task_with_calls(self.agent)
        orch = self.open_orch()
        orch.fold(self.sources())
        res = orch.fold(self.sources())
        self.assertEqual((res.applied, res.duplicates, res.quarantined), (0, 0, 0))

    def test_offset_watermark_and_tasks_live_in_one_snapshot(self):
        seed_task_with_calls(self.agent)
        orch = self.open_orch()
        orch.fold(self.sources())
        disk = read_disk_snapshot(self.state_dir)
        self.assertEqual(disk["offsets"]["inbox/implementer.jsonl"], self.inbox.stat().st_size)
        self.assertEqual(disk["watermarks"][f"implementer|4242|{self.agent.proc_start}"], 5)
        self.assertEqual(disk["tasks"]["T-1"]["state"], "spec_review")
        self.assertEqual(disk["source"], "runtime")
        self.assertEqual(disk["as_of"], orch.snapshot["as_of"])

    def test_partial_trailing_line_is_skipped_then_read(self):
        self.agent.emit("task_created", "T-1")
        line = self.agent.write(self.agent.event("transition", "T-1", {"to": "spec_draft"}), newline=False)
        # Cắt đôi dòng cuối như thể agent đang ghi dở.
        data = self.inbox.read_bytes()
        cut = len(data) - len(line) // 2
        self.inbox.write_bytes(data[:cut])

        orch = self.open_orch()
        res = orch.fold(self.sources())
        self.assertEqual((res.applied, res.quarantined), (1, 0))
        self.assertEqual(res.pending_partial, ["inbox/implementer.jsonl"])
        self.assertEqual(orch.snapshot["tasks"]["T-1"]["state"], "intake")
        offset = orch.snapshot["offsets"]["inbox/implementer.jsonl"]
        self.assertEqual(offset, len(data) - len(line))

        with open(self.inbox, "ab") as f:
            f.write(data[cut:] + b"\n")
        res = orch.fold(self.sources())
        self.assertEqual((res.applied, res.quarantined, res.pending_partial), (1, 0, []))
        self.assertEqual(orch.snapshot["tasks"]["T-1"]["state"], "spec_draft")

    def test_corrupt_line_is_quarantined_and_does_not_block_fold(self):
        self.agent.emit("task_created", "T-1")
        with open(self.inbox, "ab") as f:
            f.write(b'{"v":1,"agent":"implementer", BROKEN\n')
            f.write(b'{"v":1,"type":"transition"}\n')  # JSON đúng nhưng sai schema
        self.agent.walk("T-1", "spec_draft")
        orch = self.open_orch()
        res = orch.fold(self.sources())
        self.assertEqual((res.applied, res.quarantined), (2, 2))
        self.assertEqual(orch.snapshot["tasks"]["T-1"]["state"], "spec_draft")
        q = read_disk_snapshot(self.state_dir)["quarantine"]
        self.assertEqual([e["source"] for e in q], ["inbox/implementer.jsonl"] * 2)
        self.assertIn("BROKEN", q[0]["raw"])

    def test_event_version_outside_window_is_quarantined(self):
        self.agent.emit("task_created", "T-1", v=99)
        res = self.open_orch().fold(self.sources())
        self.assertEqual((res.applied, res.quarantined), (0, 1))

    def test_reused_pid_with_new_process_start_is_a_new_event(self):
        self.agent.emit("task_created", "T-1")
        rebooted = Emitter(self.inbox, proc_start="2026-10-26T08:00:00.000+09:00")
        rebooted.walk("T-1", "spec_draft")  # cùng agent, pid, seq=1 nhưng process khác
        res = self.open_orch().fold(self.sources())
        self.assertEqual((res.applied, res.duplicates), (2, 0))

    def test_source_outside_state_dir_is_refused(self):
        orch = self.open_orch()
        with self.assertRaises(ValueError):
            orch.fold([self.tmp / "elsewhere.jsonl"])


class CrashReplayTest(StateDirTestCase):
    """Crash trước và sau khi lưu snapshot: không áp event hai lần, không cộng đôi ledger."""

    def assert_applied_exactly_once(self, orch):
        t = orch.snapshot["tasks"]["T-1"]
        self.assertEqual(t["state"], "spec_review")
        totals = orch.ledger.totals("T-1")
        self.assertEqual((totals["records"], totals["cost_usd"]), (2, 15.0))
        lines = [l for l in orch.ledger.path.read_text(encoding="utf-8").splitlines() if l.strip()]
        self.assertEqual(len(lines), 2, "ledger.jsonl không được có dòng trùng")
        audit = AuditLog.read(orch.audit.path)
        keys = Counter(r["dedup_key"] for r in audit if r["dedup_key"])
        self.assertEqual(len(keys), 5)
        self.assertEqual(max(keys.values()), 1, "mỗi event chỉ có một bản ghi audit")
        self.assertEqual([r["seq"] for r in audit], list(range(1, len(audit) + 1)))

    def test_crash_before_snapshot_is_saved(self):
        seed_task_with_calls(self.agent)
        orch = self.open_orch()

        def crash(_snap):
            raise SimulatedCrash()

        orch.store.save = crash
        with self.assertRaises(SimulatedCrash):
            orch.fold(self.sources())
        # Ledger và audit đã được append, snapshot thì chưa.
        self.assertEqual(orch.ledger.totals("T-1")["records"], 2)
        self.assertFalse((self.state_dir / StateStore.FILE).exists())

        orch = self.restart(orch)
        self.assertEqual(orch.snapshot["offsets"], {})
        res = orch.fold(self.sources())
        self.assertEqual(res.applied, 5)  # áp lại lên snapshot cũ...
        self.assert_applied_exactly_once(orch)  # ...nhưng hiệu ứng chỉ một lần

    def test_crash_right_after_snapshot_is_saved(self):
        seed_task_with_calls(self.agent)
        orch = self.open_orch()
        real_save = orch.store.save

        def save_then_crash(snap):
            real_save(snap)
            raise SimulatedCrash()

        orch.store.save = save_then_crash
        with self.assertRaises(SimulatedCrash):
            orch.fold(self.sources())

        orch = self.restart(orch)
        res = orch.fold(self.sources())
        self.assertEqual((res.applied, res.duplicates), (0, 0))  # offset đã tiến
        self.assert_applied_exactly_once(orch)

    def test_crash_after_snapshot_even_if_offset_were_lost(self):
        # Phòng thủ chiều sâu: offset bị mất nhưng watermark vẫn chặn áp lại.
        seed_task_with_calls(self.agent)
        orch = self.open_orch()
        orch.fold(self.sources())
        orch.snapshot["offsets"] = {}
        res = orch.fold(self.sources())
        self.assertEqual((res.applied, res.duplicates), (0, 5))
        self.assert_applied_exactly_once(orch)

    def test_real_process_killed_before_snapshot_then_replayed(self):
        """Process thật bị giết (os._exit, không chạy finally) giữa ledger/audit và snapshot."""
        seed_task_with_calls(self.agent)
        script = (
            "import os, sys\n"
            "from orchestrator.config import load_config\n"
            "from orchestrator.fold import Orchestrator\n"
            "cfg = load_config(overrides={'prices_path': sys.argv[1]})\n"
            "o = Orchestrator(cfg).open()\n"
            "o.store.save = lambda snap: os._exit(17)\n"
            "o.fold(sorted((cfg.state_dir / 'inbox').glob('*.jsonl')))\n"
            "os._exit(0)\n"
        )
        env = dict(os.environ, HARNESS_STATE_DIR=str(self.state_dir))
        proc = subprocess.run([sys.executable, "-c", script, PRICES_FIXTURE], cwd=REPO_ROOT,
                              env=env, capture_output=True, timeout=60)
        self.assertEqual(proc.returncode, 17, proc.stderr.decode(errors="replace"))
        self.assertFalse((self.state_dir / StateStore.FILE).exists())

        orch = self.open_orch()  # OS đã nhả WriterGuard khi process chết
        orch.fold(self.sources())
        self.assert_applied_exactly_once(orch)
        # Chạy lại lần nữa vẫn không đổi.
        orch = self.restart(orch)
        orch.fold(self.sources())
        self.assert_applied_exactly_once(orch)


if __name__ == "__main__":
    unittest.main()
