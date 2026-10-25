"""Kill-switch fail-closed (PR-4a, R8, R15).

- Kill-switch là MỘT file JSON nằm NGOÀI repo, mặc định ``<state_dir>/killswitch.json``
  (``killswitch.file`` trong ``config/harness.yaml``). Kill-switch **đang bật** (``on``)
  nghĩa là mọi hành động tự động dừng.
- **Fail-closed:** chỉ coi là TẮT (``off``) khi file tồn tại, là file thường (không phải
  symlink/junction/thư mục), nhỏ hơn ``MAX_BYTES``, là JSON UTF-8 hợp lệ không trùng key,
  đúng schema dưới đây và ghi rõ ``"state": "off"``, ``"set_by": "user"``. Mọi trường hợp
  khác (thiếu file, không đọc được, bị khóa, JSON hỏng, version lạ, trường lạ, đường dẫn
  nằm trong repo, config không nạp được...) đều coi là BẬT. Đọc lại file ở MỖI lần
  ``check``, không cache, nên người dùng đổi file là có hiệu lực ở hành động kế tiếp.
- **Chỉ người dùng đổi kill-switch**, bằng tay theo ``docs/runbooks/release.md``. Module
  này KHÔNG có hàm ghi nào (test AST kiểm), dashboard không có nút kill-switch (SPEC mục
  6) và không agent/pipeline nào ghi file này.
- **Best-effort, KHÔNG chống giả mạo (R15):** cho tới khi agent chạy dưới Windows user
  riêng, agent cùng OS user về kỹ thuật vẫn ghi được file này. Kill-switch là phanh tay
  cho phần mềm hợp tác, không phải ranh giới bảo mật. Mỏ neo tin cậy vẫn là GitHub login
  + branch protection + required check (SPEC mục 5.2).

Định dạng file (mọi key khác đều bị coi là lỗi → BẬT)::

    {"v": 1, "state": "off", "set_by": "user",
     "set_at": "2026-10-26T09:00:00+09:00", "reason": "mở lại sau release v3"}

Nối vào code PR-1..PR-3: ``Adapter.run/probe``, ``Gate3.review``, ``FixLoop`` (vòng fix,
PR mới), ``ApprovalMirror.sync`` và ``release.shadow_eval`` nhận ``killswitch`` và gọi
``check(action)`` trước khi làm gì; kill-switch bật thì ném ``KillSwitchEngaged`` trước
mọi hiệu ứng. Runner tự động (PR-4b) PHẢI dựng các thành phần đó với một ``KillSwitch``.
Fold event và heartbeat không phải "hành động": chúng chỉ ghi lại những gì đã xảy ra,
nên vẫn chạy để audit/dashboard thấy được trạng thái lúc dừng.

CLI chỉ đọc: ``python -m orchestrator.killswitch`` in trạng thái (exit 0 = tắt, 3 = bật).
"""

from __future__ import annotations

import json
import os
import stat
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Mapping

import yaml

from .config import DEFAULT_CONFIG_PATH, REPO_ROOT, ConfigError, load_config

KILLSWITCH_VERSION = 1
DEFAULT_FILE = "killswitch.json"
MAX_BYTES = 4096
ON, OFF = "on", "off"
_KEYS = frozenset({"v", "state", "set_by", "set_at", "reason"})
_REQUIRED = frozenset({"v", "state", "set_by", "set_at"})
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


class KillSwitchEngaged(RuntimeError):
    """Kill-switch đang bật (hoặc không đọc được): hành động tự động bị dừng."""

    def __init__(self, action: str, status: "KillSwitchStatus"):
        self.action = action
        self.status = status
        super().__init__(f"kill-switch đang bật, dừng {action!r}: {status.reason}")


@dataclass(frozen=True)
class KillSwitchStatus:
    engaged: bool
    reason: str
    path: str | None
    set_by: str | None = None
    set_at: str | None = None
    note: str | None = None

    @property
    def state(self) -> str:
        return ON if self.engaged else OFF

    def to_dict(self) -> dict:
        return {**asdict(self), "state": self.state}


def _within(child: Path, parent: Path) -> bool:
    # realpath: thư mục cha là junction/symlink trỏ vào repo cũng bị bắt.
    c = os.path.normcase(os.path.realpath(child))
    p = os.path.normcase(os.path.realpath(parent))
    return c == p or c.startswith(p.rstrip("\\/") + os.sep)


def _no_duplicates(pairs):
    out = {}
    for k, v in pairs:
        if k in out:
            raise ValueError(f"key {k!r} lặp lại")
        out[k] = v
    return out


def _parse(raw: bytes) -> dict:
    data = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_no_duplicates)
    if not isinstance(data, dict):
        raise ValueError("gốc không phải object JSON")
    extra = set(data) - _KEYS
    if extra:
        raise ValueError(f"trường lạ {sorted(extra)}")
    missing = _REQUIRED - set(data)
    if missing:
        raise ValueError(f"thiếu trường {sorted(missing)}")
    v = data["v"]
    if isinstance(v, bool) or v != KILLSWITCH_VERSION:
        raise ValueError(f"version {v!r} không phải {KILLSWITCH_VERSION}")
    if data["state"] not in (ON, OFF):
        raise ValueError(f"state phải là {ON!r} hoặc {OFF!r}, nhận {data['state']!r}")
    if data["set_by"] != "user":
        raise ValueError(f"set_by phải là 'user', nhận {data['set_by']!r}: chỉ người dùng đổi kill-switch")
    if not isinstance(data["set_at"], str) or not data["set_at"].strip():
        raise ValueError("set_at phải là chuỗi thời điểm khác rỗng")
    if "reason" in data and not isinstance(data["reason"], str):
        raise ValueError("reason phải là chuỗi")
    return data


def read_status(path: Path | str | None, *, repo_root: Path = REPO_ROOT) -> KillSwitchStatus:
    """Trạng thái kill-switch. KHÔNG BAO GIỜ ném lỗi: mọi lỗi → ``engaged=True``."""
    if path is None:
        return KillSwitchStatus(True, "không xác định được đường dẫn kill-switch", None)
    shown = str(path)
    try:
        p = Path(path)
        if not p.is_absolute():
            return KillSwitchStatus(True, "đường dẫn kill-switch phải tuyệt đối", shown)
        if _within(p, repo_root):
            return KillSwitchStatus(True, "kill-switch nằm trong repo (một PR đổi được nó); phải ở ngoài repo",
                                    shown)
        try:
            st = os.lstat(p)
        except FileNotFoundError:
            return KillSwitchStatus(True, "file kill-switch không tồn tại", shown)
        if stat.S_ISLNK(st.st_mode) or getattr(st, "st_file_attributes", 0) & _REPARSE_POINT:
            return KillSwitchStatus(True, "file kill-switch là symlink/junction", shown)
        if not stat.S_ISREG(st.st_mode):
            return KillSwitchStatus(True, "kill-switch không phải file thường", shown)
        if st.st_size > MAX_BYTES:
            return KillSwitchStatus(True, f"file kill-switch lớn hơn {MAX_BYTES} byte", shown)
        with open(p, "rb") as f:
            raw = f.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            return KillSwitchStatus(True, f"file kill-switch lớn hơn {MAX_BYTES} byte", shown)
        data = _parse(raw)
    except Exception as exc:  # noqa: BLE001 — fail-closed với MỌI lỗi đọc/parse
        return KillSwitchStatus(True, f"không đọc được kill-switch: {type(exc).__name__}: {exc}"[:500], shown)
    meta = dict(set_by=data["set_by"], set_at=data["set_at"], note=data.get("reason"))
    if data["state"] == OFF:
        return KillSwitchStatus(False, "người dùng đã tắt kill-switch", shown, **meta)
    return KillSwitchStatus(True, "người dùng đã bật kill-switch", shown, **meta)


def killswitch_path(state_dir: Path, settings: Mapping | None) -> Path:
    """Đường dẫn file kill-switch: tương đối thì tính từ ``state_dir``."""
    raw = (settings or {}).get("file", DEFAULT_FILE)
    if not isinstance(raw, str) or not raw.strip():
        raise ConfigError("killswitch.file phải là chuỗi khác rỗng")
    p = Path(os.path.expandvars(raw))
    return p if p.is_absolute() else Path(state_dir) / p


class KillSwitch:
    """Bộ đọc kill-switch. Chỉ đọc; không có cách nào đổi trạng thái qua object này."""

    def __init__(self, path: Path | str | None, *, repo_root: Path = REPO_ROOT):
        self.path = None if path is None else Path(path)
        self.repo_root = Path(repo_root)

    @classmethod
    def from_config(cls, config_path: Path | None = None, *, env: Mapping[str, str] | None = None,
                    repo_root: Path = REPO_ROOT) -> "KillSwitch":
        """Dựng từ ``config/harness.yaml``. Config hỏng → path ``None`` → luôn bật."""
        try:
            cfg = load_config(config_path, env=env, repo_root=repo_root)
            with open(Path(config_path) if config_path else DEFAULT_CONFIG_PATH, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            return cls(killswitch_path(cfg.state_dir, data.get("killswitch")), repo_root=repo_root)
        except Exception:  # noqa: BLE001 — fail-closed
            return cls(None, repo_root=repo_root)

    def status(self) -> KillSwitchStatus:
        return read_status(self.path, repo_root=self.repo_root)

    @property
    def engaged(self) -> bool:
        return self.status().engaged

    def check(self, action: str) -> KillSwitchStatus:
        """Ném ``KillSwitchEngaged`` nếu kill-switch bật; gọi TRƯỚC mọi hiệu ứng của ``action``."""
        st = self.status()
        if st.engaged:
            raise KillSwitchEngaged(action, st)
        return st


def main(argv: list[str] | None = None) -> int:
    ks = KillSwitch.from_config()
    st = ks.status()
    # ASCII: không crash khi stdout là cp1252 (console/redirect trên Windows).
    print(json.dumps(st.to_dict(), ensure_ascii=True, indent=2))
    return 3 if st.engaged else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
