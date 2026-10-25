"""Adapter Muse monitor (PR-2): kênh theo dõi và thông báo cho người dùng.

Muse AI theo dõi tiến độ và báo người dùng (SPEC mục 1). Adapter này KHÔNG gọi model
nên không ghi ledger và ``cost`` luôn là 0:

- ``run`` / ``notify``: ghi một thông báo vào hộp thư đi
  ``<state_dir>/notifications/muse-<pid>-<token>.jsonl`` (mỗi process một file, một
  writer). Muse đọc từ đó. Router gọi khi task ``blocked`` vì ``dsh``/``agy`` sập (R11).
- ``status``: đọc (chỉ đọc) bản tổng hợp heartbeat do orchestrator publish.

Hộp thư nằm trong ``state_dir`` (ngoài repo, R13), không phải nguồn event của fold.
Best-effort, không chống giả mạo (R15).
"""

from __future__ import annotations

import threading
from datetime import datetime
from pathlib import Path
from typing import Callable, Mapping
from zoneinfo import ZoneInfo

from orchestrator.fsio import append_line, read_jsonl, terminate_torn_tail
from orchestrator.heartbeat import HeartbeatMonitor
from orchestrator.inbox import proc_token
from orchestrator.lock import ProcessIdentity

NOTIFY_DIR = "notifications"
NOTIFY_VERSION = 1
KINDS = ("blocked", "escalated", "info")


class MuseMonitor:
    name = "muse"

    def __init__(self, state_dir: Path, *, identity: ProcessIdentity | None = None,
                 timezone: str = "Asia/Seoul", clock: Callable[[], str] | None = None):
        self.state_dir = Path(state_dir)
        self.identity = identity or ProcessIdentity.current()
        tz = ZoneInfo(timezone)
        self.clock = clock or (lambda: datetime.now(tz).isoformat(timespec="milliseconds"))
        stem = f"{self.identity.pid}-{proc_token(self.identity.proc_start)}"
        self.path = self.state_dir / NOTIFY_DIR / f"muse-{stem}.jsonl"
        self._mutex = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        terminate_torn_tail(self.path)
        self.seq = max((o.get("seq", 0) for _off, o, _r in read_jsonl(self.path)
                        if o is not None and isinstance(o.get("seq"), int)), default=0)

    def notify(self, *, kind: str, task_id: str | None, message: str, detail: Mapping | None = None) -> dict:
        if kind not in KINDS:
            raise ValueError(f"loại thông báo không hợp lệ: {kind!r}")
        with self._mutex:
            rec = {
                "v": NOTIFY_VERSION, "seq": self.seq + 1, "ts": self.clock(), "kind": kind,
                "task_id": task_id, "message": message, "detail": dict(detail or {}),
                "pid": self.identity.pid, "proc_start": self.identity.proc_start,
            }
            append_line(self.path, rec)
            self.seq = rec["seq"]
            return rec

    # Interface chung: run / status / cost.

    def run(self, notification: Mapping) -> dict:
        return self.notify(**notification)

    def status(self) -> dict:
        return {"name": self.name, "heartbeats": HeartbeatMonitor.read_summary(self.state_dir),
                "notifications": len(self.read_all(self.state_dir))}

    def cost(self, _result=None) -> float:
        return 0.0  # không gọi model

    @staticmethod
    def read_all(state_dir: Path) -> list[dict]:
        root = Path(state_dir) / NOTIFY_DIR
        if not root.is_dir():
            return []
        out = []
        for p in sorted(root.glob("muse-*.jsonl")):
            out.extend(o for _off, o, _r in read_jsonl(p) if o is not None)
        return out
