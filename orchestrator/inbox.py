"""Inbox riêng cho từng agent (PR-1b, R3).

Bố cục trong ``state_dir`` (ngoài repo, R13)::

    inbox/<agent>/events-<pid>-<start>.jsonl     # event của MỘT process agent
    inbox/<agent>/heartbeat-<pid>-<start>.json   # heartbeat của process đó (heartbeat.py)

- Mỗi agent có một thư mục riêng; ``AgentInbox`` chỉ ghi vào thư mục của chính
  agent đó. Mỗi process có file event riêng nên mỗi file chỉ có MỘT writer (append
  từ nhiều process vào cùng file trên Windows không nguyên tử).
- Mỗi event mang ``{agent, model_id, task_id, pid}`` cùng ``proc_start`` và ``seq``:
  id idempotent ``(agent, pid, proc_start, seq)`` của fold (PR-1a).
- ``InboxOrchestrator`` fold mọi inbox và kiểm provenance: event nằm trong thư mục
  của agent A mà khai ``agent`` khác, hoặc khai ``pid``/``proc_start`` khác với
  tên file, bị cách ly. Đây chỉ là kiểm tra best-effort: agent cùng OS user vẫn ghi
  được thư mục của agent khác (R15).
"""

from __future__ import annotations

import hashlib
import os
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from . import schemas
from .fold import Orchestrator
from .fsio import append_line, read_jsonl, terminate_torn_tail
from .lock import ProcessIdentity

INBOX_DIR = "inbox"
EVENT_VERSION = 1
_AGENT_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_EVENTS_RE = re.compile(r"^events-(\d+)-([0-9a-f]{12})\.jsonl$")
_UNSET = object()


class InboxError(ValueError):
    pass


def check_agent_name(agent: str) -> str:
    if not isinstance(agent, str) or not _AGENT_RE.match(agent) or agent in (".", ".."):
        raise InboxError(f"tên agent không hợp lệ: {agent!r}")
    return agent


def proc_token(proc_start: str) -> str:
    """Token an toàn cho tên file, suy ra từ ``proc_start``."""
    return hashlib.sha1(proc_start.encode("utf-8")).hexdigest()[:12]


def agent_dir(state_dir: Path, agent: str) -> Path:
    return Path(state_dir) / INBOX_DIR / check_agent_name(agent)


class AgentInbox:
    """Phía agent: ghi event vào inbox của CHÍNH agent này, không chỗ nào khác."""

    def __init__(
        self,
        state_dir: Path,
        agent: str,
        *,
        model_id: str | None,
        model_id_source: str = "unknown",
        vendor: str | None = None,
        identity: ProcessIdentity | None = None,
        timezone: str = "Asia/Seoul",
        clock: Callable[[], str] | None = None,
    ):
        self.agent = check_agent_name(agent)
        self.dir = agent_dir(state_dir, agent)
        self.identity = identity or ProcessIdentity.current()
        self.model_id, self.model_id_source, self.vendor = model_id, model_id_source, vendor
        tz = ZoneInfo(timezone)
        self.clock = clock or (lambda: datetime.now(tz).isoformat(timespec="milliseconds"))
        stem = f"{self.identity.pid}-{proc_token(self.identity.proc_start)}"
        self.events_path = self.dir / f"events-{stem}.jsonl"
        self.heartbeat_path = self.dir / f"heartbeat-{stem}.json"
        self._mutex = threading.Lock()
        self.dir.mkdir(parents=True, exist_ok=True)
        terminate_torn_tail(self.events_path)
        # Cùng process mở lại inbox thì seq đi tiếp, không bắt đầu lại từ 1
        # (seq <= watermark sẽ bị fold coi là trùng và bỏ qua).
        self.seq = max((obj.get("seq", 0) for _o, obj, _r in read_jsonl(self.events_path)
                        if obj is not None and isinstance(obj.get("seq"), int)), default=0)

    def event(self, type_: str, task_id: str, payload: dict | None = None, *, sha: str | None = None,
              model_id: object = _UNSET, model_id_source: str | None = None) -> dict:
        ev = {
            "v": EVENT_VERSION,
            "agent": self.agent,
            "pid": self.identity.pid,
            "proc_start": self.identity.proc_start,
            "seq": self.seq + 1,
            "ts": self.clock(),
            "task_id": task_id,
            "type": type_,
            "model_id": self.model_id if model_id is _UNSET else model_id,
            "model_id_source": model_id_source or self.model_id_source,
            "vendor": self.vendor,
            "payload": dict(payload or {}),
        }
        if sha is not None:
            ev["sha"] = sha
        schemas.validate("event", ev)
        return ev

    def emit(self, type_: str, task_id: str, payload: dict | None = None, **kw) -> dict:
        with self._mutex:
            ev = self.event(type_, task_id, payload, **kw)
            append_line(self.events_path, ev)
            self.seq = ev["seq"]
            return ev


def discover_sources(state_dir: Path) -> list[Path]:
    """Mọi file event theo bố cục ``inbox/<agent>/events-*.jsonl``, thứ tự ổn định."""
    root = Path(state_dir) / INBOX_DIR
    if not root.is_dir():
        return []
    out = []
    for d in sorted(p for p in root.iterdir() if p.is_dir()):
        if not _AGENT_RE.match(d.name):
            continue
        out.extend(sorted(p for p in d.iterdir() if p.is_file() and _EVENTS_RE.match(p.name)))
    return out


def check_provenance(source: str, ev: dict) -> None:
    """Ném ``ValueError`` nếu event không khớp thư mục/tên file inbox chứa nó.

    ``source`` là đường dẫn tương đối trong ``state_dir`` (dạng POSIX). Nguồn không
    theo bố cục ``inbox/<agent>/events-<pid>-<token>.jsonl`` thì không kiểm.
    """
    parts = source.split("/")
    if len(parts) != 3 or parts[0] != INBOX_DIR:
        return
    m = _EVENTS_RE.match(parts[2])
    if not m:
        return
    if os.path.normcase(ev["agent"]) != os.path.normcase(parts[1]):
        raise ValueError(f"event của agent {ev['agent']!r} nằm trong inbox của {parts[1]!r}")
    if str(ev["pid"]) != m.group(1) or proc_token(ev["proc_start"]) != m.group(2):
        raise ValueError(f"pid/proc_start của event không khớp file inbox {parts[2]}")


class InboxOrchestrator(Orchestrator):
    """``Orchestrator`` fold từ các inbox theo agent, có kiểm provenance."""

    _current_source: str = ""

    def _source_name(self, path: Path) -> str:
        name = super()._source_name(path)
        self._current_source = name
        return name

    def _parse_event(self, raw: bytes) -> dict:  # type: ignore[override]
        ev = Orchestrator._parse_event(raw)
        check_provenance(self._current_source, ev)
        return ev

    def fold_inboxes(self):
        return self.fold(discover_sources(self.state_dir))
