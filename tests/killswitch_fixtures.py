"""Tiện ích kill-switch cho test PR-4a. File kill-switch luôn nằm trong thư mục tạm NGOÀI repo."""

from __future__ import annotations

import json
from pathlib import Path

from orchestrator.killswitch import KillSwitch


def ks_record(state: str = "off", **over) -> dict:
    rec = {"v": 1, "state": state, "set_by": "user", "set_at": "2026-10-26T09:00:00+09:00",
           "reason": "test"}
    rec.update(over)
    return rec


def write_killswitch(path: Path, state: str = "off", **over) -> Path:
    """Giả lập NGƯỜI DÙNG ghi file kill-switch bằng tay (code của harness không có hàm ghi)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(ks_record(state, **over)), encoding="utf-8")
    return path


def released(path: Path) -> KillSwitch:
    """Kill-switch đã được người dùng TẮT (cho phép hành động tự động)."""
    return KillSwitch(write_killswitch(path, "off"))


class SpyKillSwitch:
    """Kill-switch giả đếm số lần được kiểm; ``engaged`` quyết định có ném hay không."""

    def __init__(self, path: Path):
        self.inner = KillSwitch(path)
        self.checks: list[str] = []

    def check(self, action: str):
        self.checks.append(action)
        return self.inner.check(action)

    def status(self):
        return self.inner.status()
