"""Bước 11 trong batch: kiểm tra read-only CỦA CHÍNH TAG release (PR-4b, F2, R10).

- Batch không kiểm bằng code của runner đang chạy: nó checkout tag release vừa tạo vào một
  worktree detached (trong clone làm việc của batch) rồi chạy, như process con với ``cwd`` =
  worktree đó::

      python -E -s -B -m release.readonly_check run --since-seq N --expect-tag <tag> --release-lock-held

  ``-m`` đặt ``cwd`` (tag) lên đầu ``sys.path`` nên module được import là của tag; probe
  ``release_tag`` trong chính lần kiểm khẳng định ``git describe --tags --exact-match`` == tag.
  ``-E`` bỏ ``PYTHONPATH`` của runner; ``HARNESS_STATE_DIR`` trỏ tới state thật.
- Batch giữ ``release.lock`` (``purpose`` = ``batch_release``) và ``WriterGuard`` SUỐT bước này
  (``--release-lock-held``); process con chỉ đọc.
- Kết quả: ``ok`` chỉ khi exit 0 VÀ báo cáo JSON có ``"ok": true``. Timeout, JSON hỏng, exit lạ →
  không đạt (fail-closed) → revert tự động theo R10.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Callable

from .gitops import Git, check_ref_name


class PinnedReleaseCheck:
    def __init__(self, *, git: Git, state_dir: Path, worktree_root: Path, python: str = sys.executable,
                 run: Callable[..., subprocess.CompletedProcess] = subprocess.run, timeout_s: float = 900.0):
        self.git = git
        self.state_dir = Path(state_dir)
        self.worktree_root = Path(worktree_root)
        self.python = python
        self._run = run
        self.timeout_s = timeout_s

    def argv(self, tag: str, since_seq: int) -> list[str]:
        return [self.python, "-E", "-s", "-B", "-m", "release.readonly_check", "run",
                "--since-seq", str(int(since_seq)), "--expect-tag", check_ref_name(tag), "--release-lock-held"]

    def __call__(self, *, tag: str, since_seq: int) -> dict:
        check_ref_name(tag)
        path = self.worktree_root / f"chk-{tag.replace('/', '_')}"
        if path.exists():
            self.git.worktree_remove(path)
        self.worktree_root.mkdir(parents=True, exist_ok=True)
        self.git.worktree_add(path, f"refs/tags/{tag}")
        argv = self.argv(tag, since_seq)
        env = dict(os.environ, HARNESS_STATE_DIR=str(self.state_dir), PYTHONDONTWRITEBYTECODE="1")
        out: dict = {"ok": False, "argv": argv, "cwd": str(path), "exit_code": None, "report": None, "error": None}
        try:
            proc = self._run(argv, cwd=str(path), env=env, capture_output=True, stdin=subprocess.DEVNULL,
                             timeout=self.timeout_s, check=False)
            out["exit_code"] = proc.returncode
            try:
                out["report"] = json.loads((proc.stdout or b"").decode("utf-8", "replace"))
            except ValueError:
                out["error"] = ("báo cáo không phải JSON: "
                                + (proc.stderr or b"").decode("utf-8", "replace")[-1500:])
            report = out["report"]
            out["ok"] = proc.returncode == 0 and isinstance(report, dict) and report.get("ok") is True
        except subprocess.TimeoutExpired:
            out["error"] = f"kiểm tra read-only quá {self.timeout_s}s"
        except OSError as exc:
            out["error"] = f"không chạy được kiểm tra: {exc}"
        finally:
            self.git.worktree_remove(path)
        return out
