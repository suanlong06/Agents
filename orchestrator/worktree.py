"""Vòng đời worktree theo task-attempt (PR-1b, R6).

- Mỗi task-attempt một ``git worktree`` + một nhánh, cùng tên ``<task>-<attempt>``,
  nằm dưới ``worktree_root`` ngắn, cấu hình được (``config/harness.yaml``, ghi đè
  bằng ``HARNESS_WORKTREE_ROOT``), ngoài repo. Tên là đơn ánh: ``attempt`` là số
  nguyên không có ``-`` nên ``<task>-<attempt>`` tách ngược về đúng một cặp.
- Không chia sẻ: ``claim`` lấy ``PidLock`` (PID + start time) cho worktree; path đã
  tồn tại mà là của nhánh khác, hoặc không phải worktree đã đăng ký, thì từ chối.
- ``ensure`` idempotent: gọi lại (replay) trả về worktree cũ, không tạo thêm thư
  mục, nhánh hay thư mục admin ``.git/worktrees/<name>`` thứ hai.
- ``reap`` chỉ dọn lock, ``index.lock`` và worktree khi process sở hữu ĐÃ CHẾT
  (kể cả PID bị tái sử dụng với start time khác). Không dọn theo tuổi file. Nhánh
  được giữ lại nên commit của attempt không mất.
- Tắt ``gc.auto`` lúc agent chạy: env của agent mang ``gc.auto=0`` (qua
  ``GIT_CONFIG_COUNT``), và repo đặt ``gc.auto 0`` khi còn ít nhất một worktree có
  chủ còn sống; giá trị cũ được khôi phục khi không còn ai chạy (kể cả sau crash,
  ở lần ``reap`` kế tiếp).
- Không loại trừ ``worktree_root`` khỏi Windows Defender (R6).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Mapping

import yaml

from .config import DEFAULT_CONFIG_PATH, REPO_ROOT, ConfigError, _expand, _is_within
from .fsio import RetryPolicy, atomic_write_bytes
from .lock import PidLock, ProcessIdentity, break_if_dead, os_mutex, owner_alive, read_lock

ENV_WORKTREE_ROOT = "HARNESS_WORKTREE_ROOT"
# Mặc định tạm (R16): root ngắn để tránh giới hạn MAX_PATH của Windows.
DEFAULT_WORKTREE_ROOT = "%LOCALAPPDATA%\\hw"
DEFAULT_MAX_ROOT_LEN = 80

_TASK_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}-[1-9][0-9]*$")
# Config git cho mọi lệnh của agent và của orchestrator trong worktree.
AGENT_GIT_CONFIG: tuple[tuple[str, str], ...] = (("gc.auto", "0"), ("maintenance.auto", "false"))


class WorktreeError(RuntimeError):
    pass


class WorktreeConflict(WorktreeError):
    """Path/nhánh đã thuộc về thứ khác: không bao giờ dùng chung hay ghi đè."""


@dataclass(frozen=True)
class WorktreeSettings:
    root: Path
    max_root_len: int = DEFAULT_MAX_ROOT_LEN


def load_worktree_settings(
    path: Path | None = None,
    *,
    env: Mapping[str, str] | None = None,
    repo_root: Path = REPO_ROOT,
) -> WorktreeSettings:
    env = os.environ if env is None else env
    with open(Path(path) if path else DEFAULT_CONFIG_PATH, encoding="utf-8") as f:
        section = (yaml.safe_load(f) or {}).get("worktree") or {}
    raw = env.get(ENV_WORKTREE_ROOT) or section.get("root") or DEFAULT_WORKTREE_ROOT
    root = Path(_expand(str(raw), env))
    if not root.is_absolute():
        raise ConfigError(f"worktree root phải là đường dẫn tuyệt đối: {raw!r}")
    root = root.resolve()
    if _is_within(root, Path(repo_root).resolve()):
        raise ConfigError(f"worktree root {root} nằm trong repo {repo_root}")
    return WorktreeSettings(root, int(section.get("max_root_len", DEFAULT_MAX_ROOT_LEN)))


def agent_env(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """Env cho process agent: mọi lệnh git của nó chạy với ``gc.auto=0``."""
    env = dict(os.environ if base is None else base)
    n = int(env.get("GIT_CONFIG_COUNT") or 0)
    for key, value in AGENT_GIT_CONFIG:
        env[f"GIT_CONFIG_KEY_{n}"] = key
        env[f"GIT_CONFIG_VALUE_{n}"] = value
        n += 1
    env["GIT_CONFIG_COUNT"] = str(n)
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _key(path: Path | str) -> str:
    return os.path.normcase(os.path.normpath(os.path.realpath(str(path))))


@dataclass(frozen=True)
class Worktree:
    task_id: str
    attempt: int
    name: str
    path: Path
    branch: str
    created: bool


@dataclass(frozen=True)
class AgentRun:
    worktree: Worktree
    lock: PidLock
    env: dict


class WorktreeManager:
    """Quản lý worktree của MỘT repo dưới ``root``; lock nằm trong ``state_dir``."""

    LOCK_SUBDIR = Path("locks") / "worktrees"
    GC_MARKER = "gc-auto.json"

    def __init__(self, repo: Path, root: Path, state_dir: Path, *, git: str = "git",
                 max_root_len: int = DEFAULT_MAX_ROOT_LEN, retry: RetryPolicy | None = None):
        self.repo = Path(repo).resolve()
        self.git = git
        self.retry = retry or RetryPolicy()
        root = Path(root)
        if not root.is_absolute():
            raise WorktreeError(f"worktree root phải tuyệt đối: {root}")
        root.mkdir(parents=True, exist_ok=True)
        self.root = Path(os.path.realpath(root))
        if len(str(self.root)) > max_root_len:
            raise WorktreeError(f"worktree root dài {len(str(self.root))} > {max_root_len} ký tự: {self.root}")
        if _is_within(self.root, self.repo):
            raise WorktreeError(f"worktree root {self.root} nằm trong repo {self.repo}")
        self.lock_dir = Path(state_dir) / self.LOCK_SUBDIR
        self.lock_dir.mkdir(parents=True, exist_ok=True)
        common = self._git("rev-parse", "--git-common-dir").stdout.strip()
        self.common_dir = Path(os.path.realpath(self.repo / common))

    @classmethod
    def from_config(cls, repo: Path, state_dir: Path, settings: WorktreeSettings | None = None, **kw):
        s = settings or load_worktree_settings()
        return cls(repo, s.root, state_dir, max_root_len=s.max_root_len, **kw)

    # --- git -----------------------------------------------------------------

    def _git(self, *args: str, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
        cmd = [self.git, "-c", "core.longpaths=true"]
        for key, value in AGENT_GIT_CONFIG:
            cmd += ["-c", f"{key}={value}"]
        cmd += ["-C", str(cwd or self.repo), *args]
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              env=agent_env(), stdin=subprocess.DEVNULL)
        if check and proc.returncode != 0:
            raise WorktreeError(f"git {' '.join(args)} lỗi {proc.returncode}: {proc.stderr.strip()}")
        return proc

    def registered(self) -> dict[str, dict]:
        """``git worktree list --porcelain`` theo khóa đường dẫn chuẩn hóa."""
        out: dict[str, dict] = {}
        cur: dict = {}
        for line in self._git("worktree", "list", "--porcelain").stdout.splitlines() + [""]:
            if not line:
                if "worktree" in cur:
                    out[_key(cur["worktree"])] = cur
                cur = {}
                continue
            k, _, v = line.partition(" ")
            cur[k] = v
        return out

    def _branch_exists(self, branch: str) -> bool:
        return self._git("rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", check=False).returncode == 0

    # --- đặt tên ---------------------------------------------------------------

    @staticmethod
    def name_for(task_id: str, attempt: int) -> str:
        if not isinstance(task_id, str) or not _TASK_RE.match(task_id):
            raise WorktreeError(f"task_id không hợp lệ: {task_id!r}")
        if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
            raise WorktreeError(f"attempt phải là số nguyên >= 1: {attempt!r}")
        return f"{task_id}-{attempt}"

    def path_for(self, task_id: str, attempt: int) -> Path:
        return self.root / self.name_for(task_id, attempt)

    def lock_path(self, task_id: str, attempt: int) -> Path:
        return self.lock_dir / f"{self.name_for(task_id, attempt)}.lock"

    # --- tạo -------------------------------------------------------------------

    def ensure(self, task_id: str, attempt: int, *, base: str = "HEAD") -> Worktree:
        """Tạo worktree của task-attempt, hoặc trả về worktree đã có (idempotent)."""
        name = self.name_for(task_id, attempt)
        path = self.root / name
        if self._git("check-ref-format", "--branch", name, check=False).returncode != 0:
            raise WorktreeError(f"tên nhánh không hợp lệ: {name!r}")
        with os_mutex(self.lock_dir / "manager.guard"):
            entry = self.registered().get(_key(path))
            if entry is not None and not path.exists():
                # Thư mục bị xóa ngoài ý muốn: bỏ admin cũ để `add` dùng lại đúng tên,
                # không sinh ``.git/worktrees/<name>1``.
                self._git("worktree", "prune")
                entry = None
            if entry is not None:
                if entry.get("branch") != f"refs/heads/{name}":
                    raise WorktreeConflict(f"{path} đã là worktree của {entry.get('branch') or 'HEAD rời'}")
                return Worktree(task_id, attempt, name, path, name, created=False)
            if path.exists():
                raise WorktreeConflict(f"{path} đã tồn tại nhưng không phải worktree của repo này")
            if self._branch_exists(name):
                proc = self._git("worktree", "add", str(path), name, check=False)
            else:
                proc = self._git("worktree", "add", "-b", name, str(path), base, check=False)
            if proc.returncode != 0:
                raise WorktreeConflict(f"không tạo được worktree {name}: {proc.stderr.strip()}")
        return Worktree(task_id, attempt, name, path, name, created=True)

    # --- lock + chạy agent ---------------------------------------------------------

    def claim(self, task_id: str, attempt: int, *, owner: ProcessIdentity | None = None) -> PidLock:
        """Lấy lock của worktree cho ``owner`` (mặc định process hiện tại).

        Lock cũ của process đã chết được dọn cùng ``index.lock`` của worktree.
        """
        name = self.name_for(task_id, attempt)
        lock = PidLock(
            self.lock_path(task_id, attempt), owner=owner, retry=self.retry,
            meta={"task_id": task_id, "attempt": attempt, "worktree": str(self.root / name), "branch": name},
            on_break=lambda _holder: self._clear_index_lock(self.root / name),
        )
        return lock.acquire()

    @contextmanager
    def run(self, task_id: str, attempt: int, *, base: str = "HEAD",
            owner: ProcessIdentity | None = None) -> Iterator[AgentRun]:
        lock = self.claim(task_id, attempt, owner=owner)
        try:
            wt = self.ensure(task_id, attempt, base=base)
            self.suspend_gc()
            yield AgentRun(wt, lock, agent_env())
        finally:
            lock.release()
            self.resume_gc_if_idle()

    # --- gc.auto --------------------------------------------------------------------

    def _gc_guard(self):
        return os_mutex(self.lock_dir / "gc-auto.guard")

    def suspend_gc(self) -> None:
        marker = self.lock_dir / self.GC_MARKER
        with self._gc_guard():
            if not marker.exists():
                cur = self._git("config", "--local", "--get", "gc.auto", check=False)
                previous = cur.stdout.strip() if cur.returncode == 0 else None
                atomic_write_bytes(marker, json.dumps({"previous": previous}).encode("utf-8"), self.retry)
            self._git("config", "--local", "gc.auto", "0")

    def live_claims(self) -> list[str]:
        """Tên các worktree có lock mà chủ còn sống (lock hỏng tính là còn sống)."""
        return [p.stem for p in sorted(self.lock_dir.glob("*.lock"))
                if (rec := read_lock(p)) is not None and owner_alive(rec)]

    def resume_gc_if_idle(self) -> bool:
        marker = self.lock_dir / self.GC_MARKER
        with self._gc_guard():
            if not marker.exists() or self.live_claims():
                return False
            try:
                previous = json.loads(marker.read_text(encoding="utf-8")).get("previous")
            except (OSError, ValueError, AttributeError):
                previous = None
            if previous is None:
                self._git("config", "--local", "--unset", "gc.auto", check=False)
            else:
                self._git("config", "--local", "gc.auto", str(previous))
            marker.unlink()
            return True

    # --- dọn mồ côi -------------------------------------------------------------

    def _admin_dir(self, path: Path) -> Path | None:
        """Thư mục admin ``<common>/worktrees/<x>`` của worktree ``path``, nếu có."""
        base = self.common_dir / "worktrees"
        dotgit = path / ".git"
        candidate = None
        if dotgit.is_file():
            text = dotgit.read_text(encoding="utf-8", errors="replace").strip()
            if text.startswith("gitdir:"):
                candidate = Path(text[len("gitdir:"):].strip())
                if not candidate.is_absolute():
                    candidate = path / candidate
        elif base.is_dir():
            want = _key(dotgit)
            for d in base.iterdir():
                gd = d / "gitdir"
                if gd.is_file() and _key(gd.read_text(encoding="utf-8", errors="replace").strip()) == want:
                    candidate = d
                    break
        if candidate is None:
            return None
        candidate = Path(os.path.realpath(candidate))
        # Chỉ chạm vào thư mục admin thật của repo này.
        if not _is_within(candidate, base) or _key(candidate) == _key(base):
            return None
        return candidate

    def _clear_index_lock(self, path: Path) -> bool:
        admin = self._admin_dir(path)
        if admin is None:
            return False
        idx = admin / "index.lock"
        try:
            idx.unlink()
            return True
        except FileNotFoundError:
            return False

    def _remove_worktree(self, name: str) -> str:
        path = self.root / name
        self._clear_index_lock(path)
        entry = self.registered().get(_key(path))
        if entry is None:
            if path.exists():
                return "kept-unregistered-dir"  # không rõ là gì: không xóa
            self._git("worktree", "prune")
            return "pruned"
        if path.exists():
            self._git("worktree", "remove", "--force", str(path))
        self._git("worktree", "prune")
        return "removed"

    def reap(self) -> list[dict]:
        """Dọn worktree mồ côi: CHỈ khi process sở hữu lock đã chết.

        Không xét tuổi file. Lock hỏng hoặc chủ không xác định được thì giữ.
        Path luôn suy ra từ tên file lock, không tin trường ``worktree`` trong lock.
        """
        report = []
        for lock_path in sorted(self.lock_dir.glob("*.lock")):
            name = lock_path.stem
            if not _NAME_RE.match(name):
                report.append({"name": name, "action": "skipped-bad-name"})
                continue
            outcome: dict = {}

            def on_break(_holder, name=name, outcome=outcome):
                outcome["worktree"] = self._remove_worktree(name)

            try:
                holder = break_if_dead(lock_path, self.retry, on_break=on_break)
            except (WorktreeError, OSError) as exc:
                # Ví dụ: file trong worktree còn bị process con giữ. Lock được giữ
                # nguyên nên lần reap sau thử lại.
                report.append({"name": name, "action": "error", "error": str(exc)[:500]})
                continue
            if holder is None:
                report.append({"name": name, "action": "kept", "holder": read_lock(lock_path)})
            else:
                report.append({"name": name, "action": "reaped", "holder": holder, **outcome})
        self.resume_gc_if_idle()
        return report
