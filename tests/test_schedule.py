"""Lịch KST (PR-4b): digest 11:30 / 16:30, hạn duyệt 11:55 / 16:55, batch 12:00 / 17:00 (mặc định tạm),
lỡ cửa sổ thì bỏ qua (``missed``), ``zoneinfo`` + ``tzdata`` trên Windows (R17)."""

import tempfile
import unittest
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from release.schedule import (
    BATCH,
    DIGEST,
    MISSED,
    RUN,
    ScheduleError,
    SchedulerSettings,
    WindowSpec,
    job_for,
    jobs_on,
    plan,
)

KST = ZoneInfo("Asia/Seoul")
S = SchedulerSettings()


def at(h, m, day=26):
    return datetime(2026, 10, day, h, m, tzinfo=KST)


class ConfigTest(unittest.TestCase):
    def test_repo_config_has_kst_windows_and_is_not_enabled(self):
        s = SchedulerSettings.load()
        self.assertFalse(s.enabled)  # PR-4b KHÔNG kích hoạt lịch chạy thật
        self.assertEqual(s.timezone, "Asia/Seoul")
        self.assertEqual([(w.digest, w.cutoff, w.batch) for w in s.windows],
                         [(time(11, 30), time(11, 55), time(12, 0)), (time(16, 30), time(16, 55), time(17, 0))])
        self.assertEqual(s.grace, timedelta(minutes=10))

    def test_tzdata_resolves_asia_seoul(self):
        self.assertEqual(datetime(2026, 1, 1, tzinfo=ZoneInfo("Asia/Seoul")).utcoffset(), timedelta(hours=9))

    def test_invalid_windows_are_rejected(self):
        with self.assertRaises(ScheduleError):
            WindowSpec(time(12, 0), time(11, 55), time(12, 0))  # digest sau cutoff
        with self.assertRaises(ScheduleError):
            SchedulerSettings(windows=(WindowSpec(time(11, 30), time(11, 55), time(12, 0)),
                                       WindowSpec(time(11, 45), time(11, 50), time(12, 30))))  # chồng nhau
        for bad in ({"windows": [{"digest": "25:00", "cutoff": "11:55", "batch": "12:00"}]},
                    {"windows": "11:30"}, {"enabled": "yes"}, {"grace_minutes": 0}):
            with self.subTest(bad=bad), self.assertRaises(ScheduleError):
                SchedulerSettings.from_mapping(bad)

    def test_load_from_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "h.yaml"
            p.write_text("timezone: Asia/Seoul\nscheduler:\n  enabled: true\n  windows:\n"
                         "    - {digest: '09:00', cutoff: '09:20', batch: '09:30'}\n", encoding="utf-8")
            s = SchedulerSettings.load(p)
        self.assertTrue(s.enabled)
        self.assertEqual([j.window_id for j in jobs_on(datetime(2026, 10, 26).date(), s)], ["20261026-0930"] * 2)


class JobsTest(unittest.TestCase):
    def test_jobs_of_a_day(self):
        jobs = jobs_on(datetime(2026, 10, 26).date(), S)
        self.assertEqual([(j.kind, j.window_id, j.at.strftime("%H:%M")) for j in jobs],
                         [(DIGEST, "20261026-1200", "11:30"), (BATCH, "20261026-1200", "12:00"),
                          (DIGEST, "20261026-1700", "16:30"), (BATCH, "20261026-1700", "17:00")])
        self.assertTrue(all(j.at.utcoffset() == timedelta(hours=9) for j in jobs))
        b = job_for("20261026-1700", BATCH, S)
        self.assertEqual((b.cutoff, b.digest_at), (at(16, 55), at(16, 30)))
        with self.assertRaises(ScheduleError):
            job_for("20261026-1300", BATCH, S)


class PlanTest(unittest.TestCase):
    def ids(self, dues):
        return [(d.job.job_id, d.mode) for d in dues]

    def test_due_within_grace_runs(self):
        dues = plan(at(11, 31), done=(), last_tick=at(11, 29), settings=S)
        self.assertEqual(self.ids(dues), [("digest:20261026-1200", RUN)])
        self.assertEqual(plan(at(11, 29), done=(), last_tick=at(11, 28), settings=S), [])

    def test_utc_and_naive_inputs(self):
        utc = datetime(2026, 10, 26, 3, 1, tzinfo=timezone.utc)  # 12:01 KST
        dues = plan(utc, done={"digest:20261026-1200"}, last_tick=at(11, 59), settings=S)
        self.assertEqual(self.ids(dues), [("batch:20261026-1200", RUN)])
        with self.assertRaises(ScheduleError):
            plan(datetime(2026, 10, 26, 12, 1), done=(), last_tick=None, settings=S)  # không đoán giờ máy

    def test_missed_window_is_skipped_not_run_late(self):
        # Runner dừng 11:00 → 12:30: digest 11:30 và batch 12:00 đều quá 10 phút → missed.
        dues = plan(at(12, 30), done=(), last_tick=at(11, 0), settings=S)
        self.assertEqual(self.ids(dues), [("digest:20261026-1200", MISSED), ("batch:20261026-1200", MISSED)])
        self.assertEqual(dues[1].late_by, timedelta(minutes=30))
        # Đúng biên grace vẫn chạy; quá 1 giây là missed.
        self.assertEqual(plan(at(12, 10), done={"digest:20261026-1200"}, last_tick=at(11, 59), settings=S)[0].mode, RUN)
        late = datetime(2026, 10, 26, 12, 10, 1, tzinfo=KST)
        self.assertEqual(plan(late, done={"digest:20261026-1200"}, last_tick=at(11, 59), settings=S)[0].mode, MISSED)

    def test_done_jobs_and_lookback(self):
        self.assertEqual(plan(at(12, 1), done={"digest:20261026-1200", "batch:20261026-1200"},
                              last_tick=at(11, 0), settings=S), [])
        # Lần đầu chạy (không có last_tick): chỉ xét từ đầu ngày KST hôm nay.
        dues = plan(at(16, 31), done=(), last_tick=None, settings=S)
        self.assertEqual([d.job.job_id for d in dues],
                         ["digest:20261026-1200", "batch:20261026-1200", "digest:20261026-1700"])
        self.assertEqual([d.mode for d in dues], [MISSED, MISSED, RUN])
        # Máy tắt 3 ngày: chỉ nhìn lại tối đa 24 giờ.
        dues = plan(at(11, 31), done=(), last_tick=at(11, 0, day=22), settings=S)
        self.assertEqual(dues[0].job.job_id, "batch:20261025-1200")  # 25/10 11:31 trở đi
        self.assertEqual(len(dues), 4)
        self.assertTrue(all(d.mode == MISSED for d in dues[:-1]))
        self.assertEqual(self.ids(dues[-1:]), [("digest:20261026-1200", RUN)])


if __name__ == "__main__":
    unittest.main()
