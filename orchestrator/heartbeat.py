"""Heartbeat của agent và của orchestrator (PR-1b, R3/R16).

- Agent: ``HeartbeatWriter`` ghi nguyên tử ``inbox/<agent>/heartbeat-<pid>-<token>.json``
  trong inbox của CHÍNH nó (R3), mỗi ``interval_s`` giây.
- Orchestrator: ``HeartbeatMonitor.scan`` đọc mọi heartbeat agent và đánh dấu
  ``stale`` khi quá ``timeout_s`` (hoặc khi process ghi nó đã chết theo
  ``(pid, proc_start)``); ``publish`` ghi bản tổng hợp ``heartbeats.json`` kèm
  health của orchestrator. Chỉ orchestrator giữ ``WriterGuard`` mới ghi được.
- ``attach`` nối ``Orchestrator.on_write_failure`` (PR-1a) vào heartbeat: ghi state
  thất bại thì heartbeat của orchestrator chuyển ``error`` ngay, không im lặng.
- ``timeout_s``/``interval_s`` là MẶC ĐỊNH TẠM trong ``config/harness.yaml`` (R16).
- Best-effort, không chống giả mạo (R15).
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

import yaml
from jsonschema import Draft202012Validator
from jsonschema.exceptions import best_match

from .config import DEFAULT_CONFIG_PATH
from .fsio import RetryPolicy, atomic_write_bytes
from .inbox import INBOX_DIR, AgentInbox, proc_token
from .lock import ProcessIdentity

HEARTBEAT_VERSION = 1
SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schemas" / "heartbeat.schema.json"
SUMMARY_FILE = "heartbeats.json"


class HeartbeatError(ValueError):
    pass


@dataclass(frozen=True)
class HeartbeatPolicy:
    # Mặc định tạm (R16), chỉnh theo dữ liệu thật: agent ghi mỗi 15 giây; quá
    # 120 giây (8 nhịp) không có heartbeat mới thì ``stale``.
    timeout_s: float = 120.0
    interval_s: float = 15.0

    def __post_init__(self):
        if not (self.timeout_s > 0 and self.interval_s > 0):
            raise HeartbeatError("timeout_s và interval_s phải > 0")
        if self.interval_s >= self.timeout_s:
            raise HeartbeatError("interval_s phải nhỏ hơn timeout_s, nếu không agent khỏe vẫn bị stale")

    @classmethod
    def load(cls, path: Path | None = None) -> "HeartbeatPolicy":
        with open(Path(path) if path else DEFAULT_CONFIG_PATH, encoding="utf-8") as f:
            section = (yaml.safe_load(f) or {}).get("heartbeat") or {}
        return cls(
            timeout_s=float(section.get("timeout_s", cls.timeout_s)),
            interval_s=float(section.get("interval_s", cls.interval_s)),
        )


@lru_cache(maxsize=1)
def _validator() -> Draft202012Validator:
    with open(SCHEMA_PATH, encoding="utf-8") as f:
        return Draft202012Validator(json.load(f))


def validate(obj: object) -> None:
    err = best_match(_validator().iter_errors(obj))
    if err is not None:
        where = "/".join(str(p) for p in err.absolute_path) or "<gốc>"
        raise HeartbeatError(f"heartbeat: {where}: {err.message}")


# --- phía agent -----------------------------------------------------------------


class HeartbeatWriter:
    """Agent ghi heartbeat của process mình vào inbox của chính nó."""

    def __init__(self, inbox: AgentInbox, *, task_id: str | None = None, attempt: int | None = None,
                 retry: RetryPolicy | None = None):
        self.inbox = inbox
        self.path = inbox.heartbeat_path
        self.task_id, self.attempt = task_id, attempt
        self.retry = retry or RetryPolicy()
        self.beats = 0

    def beat(self, status: str = "ok", *, last_error: str | None = None) -> dict:
        rec = {
            "v": HEARTBEAT_VERSION,
            "kind": "harness-heartbeat",
            "agent": self.inbox.agent,
            "pid": self.inbox.identity.pid,
            "proc_start": self.inbox.identity.proc_start,
            "model_id": self.inbox.model_id,
            "task_id": self.task_id,
            "attempt": self.attempt,
            "beat": self.beats + 1,
            "ts": self.inbox.clock(),
            "status": status,
            "last_error": last_error,
        }
        validate(rec)
        atomic_write_bytes(self.path, json.dumps(rec, ensure_ascii=False, sort_keys=True).encode("utf-8"),
                           self.retry)
        self.beats += 1
        return rec

    def stop(self) -> dict:
        """Thoát sạch: ``stopped`` không bao giờ bị coi là ``stale``."""
        return self.beat("stopped")


# --- phía orchestrator ------------------------------------------------------------


def _read_json(path: Path, attempts: int = 3) -> object:
    # Agent có thể đang ``os.replace`` đúng lúc này (Windows: lỗi chia sẻ tạm thời).
    for i in range(attempts):
        try:
            return json.loads(path.read_bytes().decode("utf-8"))
        except (PermissionError, FileNotFoundError):
            if i == attempts - 1:
                raise
            time.sleep(0.02)


class HeartbeatMonitor:
    def __init__(self, state_dir: Path, policy: HeartbeatPolicy | None = None, *,
                 timezone: str = "Asia/Seoul", now: Callable[[], datetime] | None = None):
        self.state_dir = Path(state_dir)
        self.policy = policy or HeartbeatPolicy()
        tz = ZoneInfo(timezone)
        self.now = now or (lambda: datetime.now(tz))
        self.summary_path = self.state_dir / SUMMARY_FILE

    def _files(self) -> list[tuple[str, Path]]:
        root = self.state_dir / INBOX_DIR
        if not root.is_dir():
            return []
        out = []
        for d in sorted(p for p in root.iterdir() if p.is_dir()):
            out.extend((d.name, f) for f in sorted(d.glob("heartbeat-*.json")) if f.is_file())
        return out

    def _judge(self, agent_dir: str, path: Path, now: datetime) -> dict:
        source = path.relative_to(self.state_dir).as_posix()
        entry = {"agent": agent_dir, "source": source, "status": "invalid", "reason": None,
                 "pid": None, "proc_start": None, "task_id": None, "attempt": None,
                 "model_id": None, "ts": None, "age_s": None, "last_error": None}
        try:
            rec = _read_json(path)
            validate(rec)
            if rec.get("kind") != "harness-heartbeat":
                raise HeartbeatError("không phải heartbeat của agent")
            if os.path.normcase(rec["agent"]) != os.path.normcase(agent_dir):
                raise HeartbeatError(f"heartbeat của {rec['agent']!r} nằm trong inbox của {agent_dir!r}")
            if path.name != f"heartbeat-{rec['pid']}-{proc_token(rec['proc_start'])}.json":
                raise HeartbeatError("pid/proc_start không khớp tên file heartbeat")
            ts = datetime.fromisoformat(rec["ts"])
            if ts.tzinfo is None:
                raise HeartbeatError("ts thiếu múi giờ")
        except (OSError, ValueError, UnicodeDecodeError) as exc:
            entry["reason"] = str(exc)[:500]
            return entry
        age = (now - ts).total_seconds()
        entry.update({k: rec.get(k) for k in ("pid", "proc_start", "task_id", "attempt", "model_id",
                                              "last_error")})
        entry.update({"agent": rec["agent"], "ts": rec["ts"], "age_s": round(age, 3)})
        timeout = self.policy.timeout_s
        if rec["status"] == "stopped":
            entry["status"] = "stopped"
        elif age > timeout:
            entry["status"], entry["reason"] = "stale", f"không có heartbeat mới trong {age:.0f}s > {timeout:g}s"
        elif age < -timeout:
            entry["status"], entry["reason"] = "invalid", f"ts ở tương lai {-age:.0f}s"
        elif not ProcessIdentity(rec["pid"], rec["proc_start"]).alive():
            entry["status"], entry["reason"] = "stale", "process ghi heartbeat đã chết"
        else:
            entry["status"] = rec["status"]
        return entry

    def scan(self) -> list[dict]:
        now = self.now()
        return [self._judge(agent, path, now) for agent, path in self._files()]

    def summary(self, orchestrator_health: dict) -> dict:
        health = orchestrator_health or {}
        rec = {
            "v": HEARTBEAT_VERSION,
            "kind": "harness-heartbeats",
            "as_of": self.now().isoformat(timespec="milliseconds"),
            "timeout_s": self.policy.timeout_s,
            "orchestrator": {"status": "error" if health.get("status") == "error" else "ok",
                             "last_error": health.get("last_error")},
            "agents": self.scan(),
        }
        validate(rec)
        return rec

    def publish(self, orch) -> dict:
        """Ghi ``heartbeats.json`` (chỉ orchestrator giữ ``WriterGuard``)."""
        orch.guard.ensure_held()
        rec = self.summary(orch.health)
        atomic_write_bytes(self.summary_path,
                           json.dumps(rec, ensure_ascii=False, sort_keys=True).encode("utf-8"),
                           orch.config.write_retry)
        return rec

    def attach(self, orch) -> None:
        """Ghi state thất bại → heartbeat orchestrator = ``error`` ngay lập tức."""
        previous = orch.on_write_failure

        def on_failure(exc: BaseException) -> None:
            if previous:
                previous(exc)
            try:
                self.publish(orch)
            except OSError:
                # state_dir không ghi được nữa: health trong bộ nhớ vẫn là error và
                # fold vẫn ném StateWriteError cho bên gọi, nên không im lặng.
                pass

        orch.on_write_failure = on_failure

    @staticmethod
    def read_summary(state_dir: Path) -> dict | None:
        path = Path(state_dir) / SUMMARY_FILE
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))
