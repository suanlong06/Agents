"""Bản ghi incident do runner mở (PR-4b, R10). Mẫu và trạng thái: ``docs/runbooks/incident.md``.

- File ``<state_dir>/incidents/<INC-YYYYMMDD-NN>.json`` (ngoài repo, R13), đúng
  ``schemas/incident.schema.json``; ``opened_by`` = ``runner`` (PR-4b) hoặc ``user`` (mở tay).
  Ghi nguyên tử, chỉ khi giữ ``WriterGuard`` (R3).
- **Đóng băng = incident ``open``/``frozen``**: còn incident như vậy thì batch release không
  chạy. File incident không đọc được hay sai schema cũng tính là đang mở (fail-closed). Chỉ
  người dùng chuyển incident sang ``mitigated``/``resolved`` (sửa file theo runbook).
- Runner KHÔNG ghi được kill-switch (chỉ người dùng đổi, R8): khi mở incident nó báo người dùng
  bật kill-switch; ``killswitch_engaged`` ghi đúng trạng thái đọc được lúc đó.
- Best-effort, không chống giả mạo (R15).
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from orchestrator.fsio import RetryPolicy, WriterGuard, atomic_write_bytes
from pipeline import common

INCIDENT_DIR = "incidents"
INCIDENT_VERSION = 1
ACTIVE_STATUSES = ("open", "frozen")
_ID_RE = re.compile(r"^INC-([0-9]{8})-([0-9]{2})\.json$")


def incident_dir(state_dir: Path) -> Path:
    return Path(state_dir) / INCIDENT_DIR


def active_incidents(state_dir: Path) -> list[dict]:
    """Incident đang chặn release: ``open``/``frozen``, hoặc file không đọc được (fail-closed)."""
    root = incident_dir(state_dir)
    if not root.is_dir():
        return []
    out = []
    for p in sorted(root.glob("*.json")):
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
            common.validate("incident", rec)
        except Exception as exc:  # noqa: BLE001 — không đọc được thì coi là đang mở
            out.append({"file": p.name, "status": "unreadable", "error": f"{type(exc).__name__}: {exc}"[:200]})
            continue
        if rec["status"] in ACTIVE_STATUSES:
            out.append({"file": p.name, "incident_id": rec["incident_id"], "status": rec["status"],
                        "summary": rec["summary"]})
    return out


def next_incident_id(state_dir: Path, now: datetime) -> str:
    day = now.strftime("%Y%m%d")
    used = [int(m.group(2)) for p in incident_dir(state_dir).glob(f"INC-{day}-*.json")
            if (m := _ID_RE.match(p.name)) and m.group(1) == day]
    n = max(used, default=0) + 1
    if n > 99:
        raise RuntimeError(f"quá 99 incident trong ngày {day}")
    return f"INC-{day}-{n:02d}"


def write_incident(state_dir: Path, guard: WriterGuard, rec: dict, retry: RetryPolicy | None = None) -> Path:
    guard.ensure_held()
    common.validate("incident", rec)
    root = incident_dir(state_dir)
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{rec['incident_id']}.json"
    data = json.dumps(rec, ensure_ascii=False, indent=2, sort_keys=False).encode("utf-8")
    atomic_write_bytes(path, data, retry or RetryPolicy())
    return path


def new_incident(*, incident_id: str, opened_at: str, trigger: str, summary: str, severity: str,
                 prev_tag: str | None, new_tag: str | None, manifest_id: str | None,
                 killswitch_engaged: bool, readonly_check: dict | None, note: str) -> dict:
    return {
        "v": INCIDENT_VERSION, "incident_id": incident_id, "opened_at": opened_at, "opened_by": "runner",
        "status": "open", "severity": severity, "trigger": trigger, "summary": summary[:2000],
        "github_issue": None,
        "release": {"prev_tag": prev_tag, "new_tag": new_tag, "manifest_id": manifest_id},
        "readonly_check": readonly_check, "killswitch_engaged": bool(killswitch_engaged),
        "revert": {"status": "not_started", "branch": None, "pr": None, "reverted": [], "conflict": None},
        "timeline": [{"ts": opened_at, "actor": "runner", "note": note[:2000]}],
    }
