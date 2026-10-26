"""Runner PR-4b: kill-switch kiểm TRƯỚC mọi hành động và dừng khi BẬT; lỡ cửa sổ thì bỏ qua và
thông báo; job bị ngắt không chạy lại; lịch KHÔNG được kích hoạt (``scheduler.enabled: false``);
``build_runner`` truyền MỘT ``KillSwitch`` thật, tường minh cho mọi thành phần (F1)."""

import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from orchestrator.audit import AuditLog
from orchestrator.config import DEFAULT_CONFIG_PATH
from orchestrator.fsio import WriterGuard
from orchestrator.killswitch import KillSwitch
from release import runner as runner_mod
from release.readonly_check import snapshot
from release.runner import Runner, RunnerError, build_runner, read_sched_state, status
from release.schedule import SchedulerSettings
from tests.batch_fixtures import ListNotifier, kst
from tests.killswitch_fixtures import write_killswitch


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="hr-")).resolve()
        self.state_dir = self.tmp / "state"
        self.state_dir.mkdir()
        self.ks_path = self.state_dir / "killswitch.json"
        write_killswitch(self.ks_path, "off")
        self.ks = KillSwitch(self.ks_path)
        self.guard = WriterGuard(self.state_dir).acquire()
        self.now = kst(11, 31)
        self.audit = AuditLog(self.state_dir, self.guard, clock=lambda: self.now.isoformat())
        self.notifier = ListNotifier()
        self.calls = []

    def tearDown(self):
        self.guard.release()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def handler(self, kind, result=None, exc=None, hook=None):
        def run(job):
            self.calls.append(job.job_id)
            if hook:
                hook(job)
            if exc:
                raise exc
            return result or {"status": "frozen" if kind == "digest" else "released"}
        return run

    def make(self, killswitch=None, **handlers):
        h = {"digest": self.handler("digest"), "batch": self.handler("batch"), **handlers}
        return Runner(killswitch=killswitch or self.ks, settings=SchedulerSettings(), state_dir=self.state_dir,
                      guard=self.guard, handlers=h, notifier=self.notifier, audit=self.audit,
                      clock=lambda: self.now)


class KillSwitchTest(Base):
    def test_killswitch_is_required(self):
        for bad in (None, object()):
            with self.subTest(bad=bad), self.assertRaises(TypeError):
                self.make(killswitch=bad) if bad is not None else Runner(
                    killswitch=None, settings=SchedulerSettings(), state_dir=self.state_dir, guard=self.guard,
                    handlers={"digest": print, "batch": print}, notifier=self.notifier, audit=self.audit,
                    clock=lambda: self.now)

    def test_engaged_killswitch_runs_nothing_and_writes_nothing(self):
        for state in ("on", None, "garbage"):
            with self.subTest(state=state):
                if state is None:
                    self.ks_path.unlink(missing_ok=True)  # thiếu file = BẬT
                elif state == "garbage":
                    self.ks_path.write_text("{hỏng", encoding="utf-8")
                else:
                    write_killswitch(self.ks_path, "on")
                before = snapshot([self.state_dir])
                out = self.make().tick(kst(11, 31))
                self.assertEqual([o["status"] for o in out], ["stopped"])
                self.assertEqual(self.calls, [])
                self.assertEqual(self.notifier.rows, [])
                self.assertEqual(snapshot([self.state_dir]), before)

    def test_killswitch_turned_on_between_jobs_stops_the_tick(self):
        def engage(job):
            write_killswitch(self.ks_path, "on")

        # grace 40 phút để digest 11:30 và batch 12:00 cùng đến hạn ở một tick lúc 12:01.
        s = SchedulerSettings(grace=SchedulerSettings().grace * 4)
        r = Runner(killswitch=self.ks, settings=s, state_dir=self.state_dir, guard=self.guard,
                   handlers={"digest": self.handler("digest", hook=engage), "batch": self.handler("batch")},
                   notifier=self.notifier, audit=self.audit, clock=lambda: self.now)
        out = r.tick(kst(12, 1))
        self.assertEqual(self.calls, ["digest:20261026-1200"])  # batch KHÔNG chạy
        self.assertEqual([o["status"] for o in out], ["ok", "stopped"])
        st = read_sched_state(self.state_dir)
        self.assertIsNone(st["last_tick"])  # dừng giữa chừng: lần sau xét lại
        self.assertNotIn("batch:20261026-1200", st["jobs"])


class ScheduleFlowTest(Base):
    def test_digest_then_batch(self):
        r = self.make()
        out = r.tick(kst(11, 31))
        self.assertEqual([(o["job_id"], o["status"]) for o in out], [("digest:20261026-1200", "ok")])
        self.assertEqual(r.tick(kst(11, 45)), [])
        out = r.tick(kst(12, 0, 5))
        self.assertEqual([(o["job_id"], o["status"], o["result"]) for o in out],
                         [("batch:20261026-1200", "ok", "released")])
        self.assertEqual(self.calls, ["digest:20261026-1200", "batch:20261026-1200"])
        st = read_sched_state(self.state_dir)
        self.assertEqual(st["last_tick"], kst(12, 0, 5).isoformat())

    def test_missed_windows_are_skipped_with_notification(self):
        r = self.make()
        r.tick(kst(11, 0))
        out = r.tick(kst(12, 30))  # runner dừng 11:00 → 12:30
        self.assertEqual([(o["job_id"], o["status"]) for o in out],
                         [("digest:20261026-1200", "missed"), ("batch:20261026-1200", "missed")])
        self.assertEqual(self.calls, [])
        self.assertEqual(len(self.notifier.rows), 2)
        self.assertIn("BỎ QUA", self.notifier.rows[0]["message"])
        actions = [(a["action"], a["result"]) for a in AuditLog.read(self.audit.path)]
        self.assertIn(("runner.batch", "missed"), actions)

    def test_batch_without_frozen_digest_is_skipped(self):
        r = self.make()
        r.tick(kst(11, 0))
        r.tick(kst(11, 50))  # digest trễ 20 phút → missed
        out = r.tick(kst(12, 1))
        self.assertEqual([(o["job_id"], o["status"]) for o in out], [("batch:20261026-1200", "skipped")])
        self.assertEqual(self.calls, [])
        self.assertIn("không có digest", self.notifier.rows[-1]["message"])

    def test_failed_job_is_reported_and_not_retried(self):
        r = self.make(digest=self.handler("digest", exc=RuntimeError("git hỏng")))
        out = r.tick(kst(11, 31))
        self.assertEqual(out[0]["status"], "failed")
        self.assertEqual(self.notifier.rows[-1]["kind"], "escalated")
        self.assertEqual(r.tick(kst(11, 33)), [])
        self.assertEqual(self.calls, ["digest:20261026-1200"])

    def test_interrupted_job_is_not_rerun(self):
        (self.state_dir / "scheduler.json").write_text(json.dumps({
            "v": 1, "last_tick": kst(11, 59).isoformat(), "jobs": {
                "digest:20261026-1200": {"status": "ok", "at": kst(11, 30).isoformat()},
                "batch:20261026-1200": {"status": "started", "at": kst(12, 0).isoformat()}}}), encoding="utf-8")
        out = self.make().tick(kst(12, 2))
        self.assertEqual([(o["job_id"], o["status"]) for o in out], [("batch:20261026-1200", "interrupted")])
        self.assertEqual(self.calls, [])
        self.assertEqual(self.notifier.rows[0]["kind"], "escalated")

    def test_corrupt_scheduler_state_fails_closed(self):
        (self.state_dir / "scheduler.json").write_text("{hỏng", encoding="utf-8")
        with self.assertRaises(RunnerError):
            self.make().tick(kst(11, 31))
        self.assertEqual(self.calls, [])


class ActivationTest(Base):
    def test_serve_refuses_when_not_enabled(self):
        with self.assertRaisesRegex(RunnerError, "enabled"):
            self.make().serve(should_stop=lambda: False, sleep=lambda s: None)
        self.assertEqual(self.calls, [])

    def test_serve_ticks_when_enabled(self):
        r = self.make()
        r.settings = SchedulerSettings(enabled=True)
        stops = iter([False, False, True])
        sleeps = []
        r.serve(should_stop=lambda: next(stops), sleep=sleeps.append)
        self.assertEqual(sleeps, [30.0, 30.0])

    def test_cli_tick_and_serve_refuse_with_repo_config(self):
        # config/harness.yaml của repo: scheduler.enabled = false → không dựng runner, không ghi gì.
        self.assertFalse(SchedulerSettings.load().enabled)
        for cmd in ("tick", "serve"):
            with self.subTest(cmd=cmd), mock.patch.object(runner_mod, "build_runner",
                                                          side_effect=AssertionError("không được dựng")):
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    self.assertEqual(runner_mod.main([cmd]), 2)
                self.assertIn("enabled", err.getvalue())


class BuildRunnerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="hb-cfg-")).resolve()
        data = yaml.safe_load(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))
        data["batch"]["workdir"] = str(self.tmp / "w" / "rel")
        self.cfg = self.tmp / "harness.yaml"
        self.cfg.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
        self.state = self.tmp / "state"
        self.env = {**os.environ, "HARNESS_STATE_DIR": str(self.state), "HARNESS_MERGE_TOKEN": "t"}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_one_real_killswitch_passed_explicitly_to_every_component(self):
        runner, parts = build_runner(self.cfg, env=self.env)
        try:
            ks = runner.killswitch
            self.assertIsInstance(ks, KillSwitch)
            self.assertEqual(ks.path, self.state / "killswitch.json")
            for name in ("digest", "batch", "reverter"):
                self.assertIs(parts[name].killswitch, ks, name)
            self.assertTrue(ks.engaged)  # chưa có file → BẬT (fail-closed)
            out = runner.tick()
            self.assertEqual(out[0]["status"], "stopped")
            self.assertIsNotNone(parts["writer"])
        finally:
            parts["guard"].release()

    def test_without_merge_token_there_is_no_writer(self):
        env = {k: v for k, v in self.env.items() if k != "HARNESS_MERGE_TOKEN"}
        _runner, parts = build_runner(self.cfg, env=env)
        try:
            self.assertIsNone(parts["writer"])
            self.assertIsNone(parts["batch"].writer)
        finally:
            parts["guard"].release()

    def test_missing_workdir_clone_fails_the_job_not_silently(self):
        runner, parts = build_runner(self.cfg, env=self.env)
        try:
            with self.assertRaisesRegex(RunnerError, "chưa có clone"):
                runner.handlers["digest"](None)
        finally:
            parts["guard"].release()

    def test_status_is_readonly_and_states_the_os_user_prerequisite(self):
        self.state.mkdir()
        before = snapshot([self.state])
        st = status(self.cfg, env=self.env)
        self.assertEqual(snapshot([self.state]), before)
        self.assertFalse(st["scheduler_enabled"])
        self.assertEqual(st["auto_merge"], "disabled")
        self.assertEqual(st["merge_identities"], [])
        self.assertTrue(st["killswitch"]["engaged"])
        self.assertIn("OS user", st["os_user"])
        self.assertEqual(len(st["jobs_today"]), 4)


if __name__ == "__main__":
    unittest.main()
