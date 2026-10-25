"""Heartbeat (R3/R16): quá timeout thành ``stale``; timeout là mặc định tạm trong config."""

import json
import shutil
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from orchestrator.fsio import NotWriter, StateWriteError
from orchestrator.heartbeat import (
    HeartbeatError,
    HeartbeatMonitor,
    HeartbeatPolicy,
    HeartbeatWriter,
    validate,
)
from orchestrator.inbox import AgentInbox, InboxOrchestrator
from orchestrator.lock import ProcessIdentity
from tests.helpers import StateDirTestCase
from tests.test_lock import dead_identity

KST = ZoneInfo("Asia/Seoul")
T0 = datetime(2026, 10, 26, 10, 0, 0, tzinfo=KST)


class FakeClock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self) -> datetime:
        return self.t

    def iso(self) -> str:
        return self.t.isoformat(timespec="milliseconds")

    def advance(self, seconds: float) -> None:
        self.t += timedelta(seconds=seconds)


class HeartbeatTestCase(StateDirTestCase):
    def setUp(self):
        super().setUp()
        self.clock = FakeClock()
        self.policy = HeartbeatPolicy(timeout_s=120, interval_s=15)
        self.monitor = HeartbeatMonitor(self.state_dir, self.policy, now=self.clock)

    def writer(self, agent="implementer", identity=None, **kw) -> HeartbeatWriter:
        inbox = AgentInbox(self.state_dir, agent, model_id="claude-opus-5-5", identity=identity,
                           clock=self.clock.iso)
        return HeartbeatWriter(inbox, **kw)

    def statuses(self):
        return [(e["agent"], e["status"]) for e in self.monitor.scan()]


class PolicyTest(HeartbeatTestCase):
    def test_timeout_is_a_config_default(self):
        p = HeartbeatPolicy.load()
        self.assertEqual((p.timeout_s, p.interval_s), (120.0, 15.0))

    def test_timeout_can_be_tuned_in_config(self):
        cfg = self.tmp / "h.yaml"
        cfg.write_text("heartbeat:\n  timeout_s: 30\n  interval_s: 5\n", encoding="utf-8")
        self.assertEqual(HeartbeatPolicy.load(cfg), HeartbeatPolicy(30.0, 5.0))
        cfg.write_text("other: 1\n", encoding="utf-8")
        self.assertEqual(HeartbeatPolicy.load(cfg), HeartbeatPolicy())

    def test_interval_must_be_shorter_than_timeout(self):
        for t, i in ((10, 10), (10, 20), (0, 1), (10, 0)):
            with self.assertRaises(HeartbeatError):
                HeartbeatPolicy(t, i)


class StaleTest(HeartbeatTestCase):
    def test_writer_writes_only_into_its_own_inbox(self):
        w = self.writer(task_id="T-1", attempt=1)
        rec = w.beat()
        me = ProcessIdentity.current()
        self.assertEqual(w.path.parent, self.state_dir / "inbox" / "implementer")
        self.assertEqual((rec["agent"], rec["pid"], rec["proc_start"], rec["task_id"], rec["beat"]),
                         ("implementer", me.pid, me.proc_start, "T-1", 1))
        validate(json.loads(w.path.read_text(encoding="utf-8")))

    def test_heartbeat_past_timeout_becomes_stale(self):
        w = self.writer()
        w.beat()
        self.assertEqual(self.statuses(), [("implementer", "ok")])
        self.clock.advance(120)
        self.assertEqual(self.statuses(), [("implementer", "ok")], "đúng bằng timeout vẫn chưa stale")
        self.clock.advance(1)
        [entry] = self.monitor.scan()
        self.assertEqual(entry["status"], "stale")
        self.assertEqual(entry["age_s"], 121.0)
        self.assertIn("120", entry["reason"])

        w.beat()  # agent sống lại và ghi tiếp
        self.assertEqual(self.statuses(), [("implementer", "ok")])

    def test_timeout_follows_the_policy(self):
        self.monitor = HeartbeatMonitor(self.state_dir, HeartbeatPolicy(30, 5), now=self.clock)
        self.writer().beat()
        self.clock.advance(31)
        self.assertEqual(self.statuses(), [("implementer", "stale")])

    def test_error_status_is_reported_while_fresh(self):
        self.writer().beat("error", last_error="9Router sập")
        [entry] = self.monitor.scan()
        self.assertEqual((entry["status"], entry["last_error"]), ("error", "9Router sập"))
        self.clock.advance(500)
        self.assertEqual(self.statuses(), [("implementer", "stale")])

    def test_stopped_is_never_stale(self):
        w = self.writer()
        w.beat()
        w.stop()
        self.clock.advance(10_000)
        self.assertEqual(self.statuses(), [("implementer", "stopped")])

    def test_dead_writer_process_is_stale_before_timeout(self):
        self.writer(identity=dead_identity()).beat()
        [entry] = self.monitor.scan()
        self.assertEqual(entry["status"], "stale")
        self.assertIn("chết", entry["reason"])

    def test_each_process_has_its_own_heartbeat(self):
        self.writer().beat()
        self.writer(identity=dead_identity()).beat()
        self.assertEqual(sorted(e["status"] for e in self.monitor.scan()), ["ok", "stale"])


class InvalidTest(HeartbeatTestCase):
    def test_heartbeat_in_another_agents_inbox_is_invalid(self):
        w = self.writer("implementer")
        w.beat()
        other = self.state_dir / "inbox" / "reviewer"
        other.mkdir(parents=True)
        shutil.copy(w.path, other / w.path.name)
        by_dir = {e["source"].split("/")[1]: e for e in self.monitor.scan()}
        self.assertEqual(by_dir["implementer"]["status"], "ok")
        self.assertEqual(by_dir["reviewer"]["status"], "invalid")

    def test_corrupt_or_future_heartbeat_is_invalid(self):
        w = self.writer()
        w.path.parent.mkdir(parents=True, exist_ok=True)
        w.path.write_text("{oops", encoding="utf-8")
        self.assertEqual(self.statuses(), [("implementer", "invalid")])
        w.beat()
        self.clock.advance(-1000)
        self.assertEqual(self.statuses(), [("implementer", "invalid")])


class OrchestratorHeartbeatTest(HeartbeatTestCase):
    def open_inbox_orch(self):
        o = InboxOrchestrator(self.config).open()
        self._open.append(o)
        return o

    def test_publish_requires_writer_and_writes_summary(self):
        self.writer().beat()
        orch = self.open_inbox_orch()
        rec = self.monitor.publish(orch)
        disk = HeartbeatMonitor.read_summary(self.state_dir)
        self.assertEqual(disk, rec)
        validate(disk)
        self.assertEqual((disk["orchestrator"]["status"], disk["timeout_s"]), ("ok", 120))
        self.assertEqual([a["status"] for a in disk["agents"]], ["ok"])
        orch.close()
        with self.assertRaises(NotWriter):
            self.monitor.publish(orch)

    def test_state_write_failure_turns_orchestrator_heartbeat_to_error(self):
        orch = self.open_inbox_orch()
        self.monitor.attach(orch)

        def fail(_snap):
            raise StateWriteError("WinError 5: Access is denied")

        orch.store.save = fail
        with self.assertRaises(StateWriteError):
            orch.fold_inboxes()
        disk = HeartbeatMonitor.read_summary(self.state_dir)
        self.assertEqual(disk["orchestrator"]["status"], "error")
        self.assertIn("WinError 5", disk["orchestrator"]["last_error"])

    def test_shrunken_source_shows_as_orchestrator_error(self):
        inbox = AgentInbox(self.state_dir, "implementer", model_id="m")
        inbox.emit("task_created", "T-1")
        orch = self.open_inbox_orch()
        orch.fold_inboxes()
        inbox.events_path.write_bytes(b"")
        orch.fold_inboxes()
        rec = self.monitor.publish(orch)
        self.assertEqual(rec["orchestrator"]["status"], "error")
        self.assertIn("events-", rec["orchestrator"]["last_error"])
