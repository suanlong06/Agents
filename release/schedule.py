"""Lịch KST cho digest và batch release (PR-4b, SPEC mục 3 bước 9–10, R16, R17).

- Mọi mốc giờ theo ``Asia/Seoul`` qua ``zoneinfo`` (Windows cần gói ``tzdata``, R17). Đầu vào
  ``now`` phải là datetime có múi giờ; giờ UTC hay giờ máy đều được đổi sang KST trước khi so.
- Mỗi **cửa sổ** có 3 mốc (mặc định tạm trong ``scheduler.windows`` của
  ``config/harness.yaml``, R16):

  | digest (đóng băng danh sách) | hạn duyệt (cutoff) | batch release |
  |---|---|---|
  | 11:30 | 11:55 | 12:00 |
  | 16:30 | 16:55 | 17:00 |

  ``window_id`` = ``YYYYMMDD-HHMM`` của giờ batch (ví dụ ``20261026-1200``); digest và batch
  của cùng cửa sổ dùng chung id.
- **Lỡ cửa sổ thì bỏ qua và thông báo**: job đến hạn mà quá ``grace_minutes`` (mặc định tạm
  10 phút) vẫn chưa chạy (máy tắt, runner dừng, kill-switch bật...) thì KHÔNG chạy bù; runner
  ghi ``missed`` và báo người dùng. Batch không bao giờ chạy trễ sang giờ người dùng không chờ.
- Module này thuần: không đọc/ghi file, không mạng. ``plan`` chỉ trả job nào đến hạn.
- ``enabled`` mặc định ``false``: PR-4b KHÔNG kích hoạt lịch chạy thật. Bật là quyết định riêng
  của người dùng sau khi merge (``docs/runbooks/batch.md``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Iterable, Mapping
from zoneinfo import ZoneInfo

import yaml

from pipeline import common

DIGEST, BATCH = "digest", "batch"
RUN, MISSED = "run", "missed"
MAX_LOOKBACK = timedelta(hours=24)
_HHMM = re.compile(r"^([01][0-9]|2[0-3]):([0-5][0-9])$")


class ScheduleError(ValueError):
    pass


def _hhmm(raw: object, what: str) -> time:
    if not isinstance(raw, str) or not _HHMM.match(raw):
        raise ScheduleError(f"{what} phải có dạng HH:MM (KST), nhận {raw!r}")
    h, m = raw.split(":")
    return time(int(h), int(m))


@dataclass(frozen=True)
class WindowSpec:
    digest: time
    cutoff: time
    batch: time

    def __post_init__(self):
        if not (self.digest < self.cutoff <= self.batch):
            raise ScheduleError(f"cửa sổ phải có digest < cutoff <= batch, nhận {self}")


DEFAULT_WINDOWS = (
    WindowSpec(time(11, 30), time(11, 55), time(12, 0)),
    WindowSpec(time(16, 30), time(16, 55), time(17, 0)),
)


@dataclass(frozen=True)
class SchedulerSettings:
    enabled: bool = False
    timezone: str = "Asia/Seoul"
    tick_s: float = 30.0
    grace: timedelta = timedelta(minutes=10)
    windows: tuple[WindowSpec, ...] = DEFAULT_WINDOWS

    def __post_init__(self):
        if not self.windows:
            raise ScheduleError("scheduler.windows rỗng")
        ordered = sorted(self.windows, key=lambda w: w.digest)
        for a, b in zip(ordered, ordered[1:]):
            if b.digest < a.batch:
                raise ScheduleError(f"hai cửa sổ chồng nhau: {a} và {b}")
        if self.grace <= timedelta(0) or self.tick_s <= 0:
            raise ScheduleError("grace_minutes và tick_s phải > 0")
        ZoneInfo(self.timezone)  # tzdata thiếu → ZoneInfoNotFoundError ngay khi nạp

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @classmethod
    def from_mapping(cls, data: Mapping | None, *, timezone: str = "Asia/Seoul") -> "SchedulerSettings":
        data = dict(data or {})
        enabled = data.get("enabled", False)
        if not isinstance(enabled, bool):
            raise ScheduleError("scheduler.enabled phải là true/false")
        raw_windows = data.get("windows")
        windows = DEFAULT_WINDOWS
        if raw_windows is not None:
            if not isinstance(raw_windows, list):
                raise ScheduleError("scheduler.windows phải là danh sách")
            windows = tuple(WindowSpec(_hhmm(w.get("digest"), "digest"), _hhmm(w.get("cutoff"), "cutoff"),
                                       _hhmm(w.get("batch"), "batch"))
                            for w in raw_windows if isinstance(w, Mapping)) or ()
            if len(windows) != len(raw_windows):
                raise ScheduleError("mỗi phần tử scheduler.windows phải là mapping digest/cutoff/batch")
        return cls(enabled=enabled, timezone=timezone, tick_s=float(data.get("tick_s", cls.tick_s)),
                   grace=timedelta(minutes=float(data.get("grace_minutes", 10))), windows=windows)

    @classmethod
    def load(cls, path: Path | None = None) -> "SchedulerSettings":
        with open(Path(path) if path else common.DEFAULT_CONFIG_PATH, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return cls.from_mapping(data.get("scheduler"), timezone=data.get("timezone", "Asia/Seoul"))


@dataclass(frozen=True)
class Job:
    kind: str           # "digest" | "batch"
    window_id: str      # YYYYMMDD-HHMM của giờ batch (KST)
    at: datetime        # giờ chạy của job này (KST)
    digest_at: datetime
    cutoff: datetime
    batch_at: datetime

    @property
    def job_id(self) -> str:
        return f"{self.kind}:{self.window_id}"

    def to_dict(self) -> dict:
        return {"kind": self.kind, "window_id": self.window_id, "job_id": self.job_id,
                "at": self.at.isoformat(), "digest_at": self.digest_at.isoformat(),
                "cutoff": self.cutoff.isoformat(), "batch_at": self.batch_at.isoformat()}


@dataclass(frozen=True)
class Due:
    job: Job
    mode: str           # "run" | "missed"
    late_by: timedelta = field(default=timedelta(0))


def to_kst(now: datetime, settings: SchedulerSettings) -> datetime:
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ScheduleError("now phải là datetime có múi giờ (không đoán giờ máy)")
    return now.astimezone(settings.tz)


def jobs_on(day: date, settings: SchedulerSettings) -> list[Job]:
    tz = settings.tz
    out = []
    for w in settings.windows:
        d_at, c_at, b_at = (datetime.combine(day, t, tzinfo=tz) for t in (w.digest, w.cutoff, w.batch))
        wid = b_at.strftime("%Y%m%d-%H%M")
        out.append(Job(DIGEST, wid, d_at, d_at, c_at, b_at))
        out.append(Job(BATCH, wid, b_at, d_at, c_at, b_at))
    return sorted(out, key=lambda j: (j.at, j.kind != DIGEST))


def job_for(window_id: str, kind: str, settings: SchedulerSettings) -> Job:
    """Job của một ``window_id`` (để batch tìm digest của chính cửa sổ đó)."""
    try:
        day = datetime.strptime(window_id, "%Y%m%d-%H%M").date()
    except ValueError as exc:
        raise ScheduleError(f"window_id không hợp lệ: {window_id!r}") from exc
    for j in jobs_on(day, settings):
        if j.window_id == window_id and j.kind == kind:
            return j
    raise ScheduleError(f"không có cửa sổ {window_id!r} trong scheduler.windows")


def plan(now: datetime, *, done: Iterable[str], last_tick: datetime | None,
         settings: SchedulerSettings) -> list[Due]:
    """Job đến hạn lúc ``now`` chưa có trong ``done``: ``run`` nếu còn trong ``grace``, quá thì
    ``missed`` (bỏ qua, thông báo). Chỉ xét job từ ``last_tick`` (tối đa 24 giờ trước) — lần đầu
    chạy thì từ đầu ngày KST hôm nay."""
    now = to_kst(now, settings)
    done = set(done)
    floor = datetime.combine(now.date(), time(0), tzinfo=settings.tz)
    if last_tick is not None:
        floor = max(to_kst(last_tick, settings), now - MAX_LOOKBACK)
    out = []
    day = floor.date()
    while day <= now.date():
        for j in jobs_on(day, settings):
            if j.job_id in done or j.at > now or j.at < floor:
                continue
            late = now - j.at
            out.append(Due(j, RUN if late <= settings.grace else MISSED, late))
        day += timedelta(days=1)
    return out
