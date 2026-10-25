"""Nạp cấu hình harness (PR-1a).

- ``state_dir`` cấu hình được, mặc định ``%LOCALAPPDATA%\\harness``; ghi đè bằng
  biến môi trường ``HARNESS_STATE_DIR``. Bắt buộc nằm NGOÀI repo (R13).
- Mọi con số (N vòng fix, ngân sách, retry...) là mặc định tạm (R16).
- Thời điểm theo ``Asia/Seoul``; trên Windows cần gói ``tzdata`` (R17).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Mapping
from zoneinfo import ZoneInfo

import yaml

from .fsio import RetryPolicy

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = REPO_ROOT / "config" / "harness.yaml"
DEFAULT_PRICES_PATH = REPO_ROOT / "config" / "prices.yaml"
ENV_STATE_DIR = "HARNESS_STATE_DIR"

_WIN_VAR = re.compile(r"%([A-Za-z_][A-Za-z0-9_]*)%")


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class FixPolicy:
    # Mặc định tạm (R16): N = 2, ngân sách vòng fix = 3x chi phí attempt đầu.
    max_fix_rounds: int = 2
    budget_multiplier: float = 3.0


@dataclass(frozen=True)
class HarnessConfig:
    state_dir: Path
    timezone: str = "Asia/Seoul"
    fix: FixPolicy = field(default_factory=FixPolicy)
    write_retry: RetryPolicy = field(default_factory=RetryPolicy)
    quarantine_max_raw_bytes: int = 4096
    prices_path: Path = DEFAULT_PRICES_PATH
    repo_root: Path = REPO_ROOT

    def now(self) -> datetime:
        return datetime.now(ZoneInfo(self.timezone))


def _expand(raw: str, env: Mapping[str, str]) -> str:
    def sub(m: re.Match) -> str:
        name = m.group(1)
        if name not in env:
            raise ConfigError(f"biến môi trường %{name}% chưa đặt; hãy đặt {ENV_STATE_DIR}")
        return env[name]

    return os.path.expanduser(_WIN_VAR.sub(sub, raw))


def _is_within(child: Path, parent: Path) -> bool:
    c = os.path.normcase(str(child))
    p = os.path.normcase(str(parent))
    return c == p or c.startswith(p.rstrip("\\/") + os.sep)


def resolve_state_dir(raw: str, env: Mapping[str, str], repo_root: Path) -> Path:
    path = Path(_expand(raw, env))
    if not path.is_absolute():
        raise ConfigError(f"state_dir phải là đường dẫn tuyệt đối: {raw!r}")
    path = path.resolve()
    if _is_within(path, repo_root.resolve()):
        # R13: runtime state không bao giờ nằm trong repo.
        raise ConfigError(f"state_dir {path} nằm trong repo {repo_root}; runtime state phải ở ngoài repo")
    return path


def load_config(
    path: Path | None = None,
    *,
    env: Mapping[str, str] | None = None,
    repo_root: Path = REPO_ROOT,
    overrides: Mapping[str, object] | None = None,
) -> HarnessConfig:
    env = os.environ if env is None else env
    path = Path(path) if path else DEFAULT_CONFIG_PATH
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    data.update(overrides or {})

    raw_state_dir = env.get(ENV_STATE_DIR) or data.get("state_dir")
    if not raw_state_dir:
        raise ConfigError("thiếu state_dir")
    fix = data.get("fix_loop") or {}
    retry = data.get("atomic_write") or {}
    prices = data.get("prices_path")
    return HarnessConfig(
        state_dir=resolve_state_dir(str(raw_state_dir), env, repo_root),
        timezone=data.get("timezone", "Asia/Seoul"),
        fix=FixPolicy(
            max_fix_rounds=int(fix.get("max_fix_rounds", FixPolicy.max_fix_rounds)),
            budget_multiplier=float(fix.get("budget_multiplier", FixPolicy.budget_multiplier)),
        ),
        write_retry=RetryPolicy(
            max_attempts=int(retry.get("max_attempts", RetryPolicy.max_attempts)),
            backoff_initial_s=float(retry.get("backoff_initial_s", RetryPolicy.backoff_initial_s)),
            backoff_max_s=float(retry.get("backoff_max_s", RetryPolicy.backoff_max_s)),
        ),
        quarantine_max_raw_bytes=int(data.get("quarantine_max_raw_bytes", 4096)),
        prices_path=(repo_root / prices) if prices else DEFAULT_PRICES_PATH,
        repo_root=repo_root,
    )
