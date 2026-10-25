"""Lock an toàn khi crash: PID + thời điểm khởi động process (PR-1b, R6).

- File lock là JSON ``{pid, proc_start, ...}``. ``proc_start`` lấy từ OS
  (Windows: ``GetProcessTimes``; Linux: ``/proc/<pid>/stat`` + ``boot_id``), nên
  PID bị tái sử dụng bởi process khác có start time khác → lock được coi là của
  process ĐÃ CHẾT.
- Chỉ dọn lock khi process sở hữu đã chết. **Không bao giờ dọn theo tuổi file**:
  không có chỗ nào trong module này đọc ``mtime``.
- Fail-closed: không đọc được start time (ví dụ ``ERROR_ACCESS_DENIED``) hoặc file
  lock hỏng thì coi là đang bị giữ, không dọn.
- Kiểm tra-rồi-dọn-rồi-tạo chạy dưới một mutex OS (``<lock>.guard``) để hai bên
  cùng thấy lock cũ không xóa nhầm lock mới của nhau. Mutex OS tự nhả khi process
  chết nên bản thân nó không bao giờ cần dọn.
"""

from __future__ import annotations

import json
import os
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Callable, Iterator

from .fsio import RetryPolicy, atomic_write_bytes

LOCK_VERSION = 1
LOCK_KIND = "harness-lock"


class LockHeld(RuntimeError):
    """Lock đang bị một process còn sống giữ (hoặc không xác định được chủ)."""

    def __init__(self, path: Path, holder: dict | None, why: str):
        self.path, self.holder, self.why = Path(path), holder, why
        super().__init__(f"{path}: {why}; chủ lock = {holder!r}")


class LockLost(RuntimeError):
    """Nhả lock nhưng file lock không còn là của mình."""


# --- nhận diện process --------------------------------------------------------


@dataclass(frozen=True)
class ProcessProbe:
    exists: bool
    proc_start: str | None  # None khi tồn tại nhưng không đọc được start time


_FILETIME_EPOCH = datetime(1601, 1, 1, tzinfo=timezone.utc)


def _format_filetime(ft: int) -> str:
    """FILETIME (100ns từ 1601, UTC) → chuỗi ISO UTC 7 chữ số lẻ, không mất độ chính xác."""
    secs, rem = divmod(ft, 10_000_000)
    dt = _FILETIME_EPOCH + timedelta(seconds=secs)
    return f"{dt:%Y-%m-%dT%H:%M:%S}.{rem:07d}Z"


def _probe_windows(pid: int) -> ProcessProbe:
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.GetProcessTimes.argtypes = (wintypes.HANDLE,) + (ctypes.POINTER(wintypes.FILETIME),) * 4
    k32.GetProcessTimes.restype = wintypes.BOOL
    k32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    k32.GetExitCodeProcess.restype = wintypes.BOOL
    k32.CloseHandle.argtypes = (wintypes.HANDLE,)

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    ERROR_ACCESS_DENIED = 5

    h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        err = ctypes.get_last_error()
        # ERROR_INVALID_PARAMETER (87): không có process nào mang PID này.
        return ProcessProbe(err == ERROR_ACCESS_DENIED, None)
    try:
        code = wintypes.DWORD()
        if not k32.GetExitCodeProcess(h, ctypes.byref(code)):
            return ProcessProbe(True, None)
        if code.value != STILL_ACTIVE:
            # Đã thoát; handle còn mở ở đâu đó nên OpenProcess vẫn được.
            return ProcessProbe(False, None)
        times = [wintypes.FILETIME() for _ in range(4)]
        if not k32.GetProcessTimes(h, *(ctypes.byref(t) for t in times)):
            return ProcessProbe(True, None)
        created = times[0]
        return ProcessProbe(True, _format_filetime((created.dwHighDateTime << 32) | created.dwLowDateTime))
    finally:
        k32.CloseHandle(h)


@lru_cache(maxsize=1)
def _linux_boot_id() -> str:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return "unknown-boot"


def _probe_linux(pid: int) -> ProcessProbe:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except FileNotFoundError:
        return ProcessProbe(False, None)
    except OSError:
        return ProcessProbe(True, None)
    fields = stat[stat.rindex(")") + 2:].split()
    if fields[0] in ("Z", "X"):  # zombie / đã chết
        return ProcessProbe(False, None)
    return ProcessProbe(True, f"linux:{_linux_boot_id()}:{fields[19]}")


def _probe_posix(pid: int) -> ProcessProbe:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return ProcessProbe(False, None)
    except PermissionError:
        return ProcessProbe(True, None)
    return ProcessProbe(True, None)


def probe_process(pid: int) -> ProcessProbe:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return ProcessProbe(False, None)
    if sys.platform == "win32":
        return _probe_windows(pid)
    if sys.platform.startswith("linux"):
        return _probe_linux(pid)
    return _probe_posix(pid)


@dataclass(frozen=True)
class ProcessIdentity:
    """``(pid, proc_start)``: định danh một process, không trùng khi PID bị tái sử dụng."""

    pid: int
    proc_start: str

    @classmethod
    def of(cls, pid: int) -> "ProcessIdentity":
        p = probe_process(pid)
        if not p.exists:
            raise ProcessLookupError(f"không có process sống với PID {pid}")
        if p.proc_start is None:
            raise PermissionError(f"không đọc được thời điểm khởi động của PID {pid}")
        return cls(pid, p.proc_start)

    @classmethod
    def current(cls) -> "ProcessIdentity":
        return _current_identity(os.getpid())

    def alive(self) -> bool:
        """``True`` nếu chính process này còn sống.

        PID không tồn tại → chết. PID tồn tại nhưng start time khác → PID đã bị tái
        sử dụng, process gốc đã chết. Không đọc được start time → coi là còn sống
        (fail-closed: thà không dọn còn hơn dọn nhầm).
        """
        p = probe_process(self.pid)
        if not p.exists:
            return False
        if p.proc_start is None:
            return True
        return p.proc_start == self.proc_start


@lru_cache(maxsize=4)
def _current_identity(pid: int) -> ProcessIdentity:
    p = probe_process(pid)
    if p.proc_start is not None:
        return ProcessIdentity(pid, p.proc_start)
    # Nền tảng không đọc được start time: dùng thời điểm import, vẫn ổn định trong process.
    return ProcessIdentity(pid, f"approx:{time.time_ns()}")


def owner_alive(record: dict | None) -> bool:
    """Chủ của một bản ghi lock còn sống không. Bản ghi hỏng → ``True`` (fail-closed)."""
    if not isinstance(record, dict):
        return True
    pid, start = record.get("pid"), record.get("proc_start")
    if not isinstance(pid, int) or isinstance(pid, bool) or not isinstance(start, str) or not start:
        return True
    return ProcessIdentity(pid, start).alive()


# --- mutex OS ngắn hạn ----------------------------------------------------------


@contextmanager
def os_mutex(path: Path, timeout_s: float = 10.0, poll_s: float = 0.02) -> Iterator[None]:
    """Mutex liên tiến trình bằng khóa byte trên ``path``; OS tự nhả khi process chết."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "a+b")
    deadline = time.monotonic() + timeout_s
    try:
        while True:
            try:
                if sys.platform == "win32":
                    import msvcrt

                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"không lấy được mutex {path} sau {timeout_s}s") from None
                time.sleep(poll_s)
        try:
            yield
        finally:
            if sys.platform == "win32":
                import msvcrt

                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    finally:
        fh.close()


def _unlink_retry(path: Path, policy: RetryPolicy) -> None:
    """Xóa file; trên Windows reader đang mở file làm ``unlink`` lỗi tạm thời."""
    delay = policy.backoff_initial_s
    for attempt in range(1, policy.max_attempts + 1):
        try:
            path.unlink()
            return
        except FileNotFoundError:
            return
        except PermissionError:
            if attempt == policy.max_attempts:
                raise
            time.sleep(delay)
            delay = min(delay * 2, policy.backoff_max_s)


# --- PidLock -----------------------------------------------------------------------


def read_lock(path: Path) -> dict | None:
    """Đọc file lock; ``None`` nếu không có. File hỏng trả ``{}`` (chủ không rõ)."""
    try:
        raw = Path(path).read_bytes()
    except FileNotFoundError:
        return None
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return obj if isinstance(obj, dict) else {}


class PidLock:
    """Lock dạng file mang ``{pid, proc_start}`` của process sở hữu (R6).

    ``owner`` mặc định là process hiện tại; orchestrator có thể giữ lock thay cho
    process agent con bằng ``ProcessIdentity.of(child.pid)``.
    """

    def __init__(
        self,
        path: Path,
        *,
        owner: ProcessIdentity | None = None,
        meta: dict | None = None,
        retry: RetryPolicy | None = None,
        clock=None,
        on_break: Callable[[dict], None] | None = None,
    ):
        self.path = Path(path)
        self.guard_path = self.path.with_name(self.path.name + ".guard")
        self.owner = owner or ProcessIdentity.current()
        self.meta = dict(meta or {})
        self.retry = retry or RetryPolicy()
        self._clock = clock or (lambda: datetime.now(timezone.utc).isoformat(timespec="milliseconds"))
        # Gọi dưới mutex, TRƯỚC khi xóa lock của process đã chết: dọn tài nguyên mà
        # lock đó bảo vệ (ví dụ ``index.lock`` của worktree). Ném lỗi thì lock cũ giữ nguyên.
        self.on_break = on_break
        self.held = False

    def record(self) -> dict:
        rec = {"v": LOCK_VERSION, "kind": LOCK_KIND, "pid": self.owner.pid,
               "proc_start": self.owner.proc_start, "acquired_at": self._clock()}
        rec.update({k: v for k, v in self.meta.items() if k not in rec})
        return rec

    def _is_mine(self, rec: dict | None) -> bool:
        return (isinstance(rec, dict) and rec.get("pid") == self.owner.pid
                and rec.get("proc_start") == self.owner.proc_start)

    def acquire(self) -> "PidLock":
        """Lấy lock. Lock cũ của process đã chết thì dọn; còn sống thì ném ``LockHeld``."""
        with os_mutex(self.guard_path):
            holder = read_lock(self.path)
            if holder is not None and not self._is_mine(holder):
                if owner_alive(holder):
                    why = "file lock hỏng, không rõ chủ" if not holder else "process sở hữu còn sống"
                    raise LockHeld(self.path, holder or None, why)
                if self.on_break:
                    self.on_break(holder)
                _unlink_retry(self.path, self.retry)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_bytes(self.path, json.dumps(self.record(), sort_keys=True).encode("utf-8"), self.retry)
        self.held = True
        return self

    def release(self) -> None:
        if not self.held:
            return
        with os_mutex(self.guard_path):
            holder = read_lock(self.path)
            self.held = False
            if holder is None:
                return
            if not self._is_mine(holder):
                raise LockLost(f"{self.path} không còn là lock của {self.owner}: {holder!r}")
            _unlink_retry(self.path, self.retry)

    def __enter__(self) -> "PidLock":
        return self.acquire()

    def __exit__(self, *exc) -> None:
        self.release()


def break_if_dead(
    path: Path,
    retry: RetryPolicy | None = None,
    *,
    on_break: Callable[[dict], None] | None = None,
) -> dict | None:
    """Dọn lock nếu (và chỉ nếu) chủ của nó đã chết. Trả về bản ghi đã dọn hoặc ``None``.

    Không xét tuổi file. Lock hỏng hoặc chủ còn sống / không xác định → giữ nguyên.
    ``on_break`` chạy dưới mutex trước khi xóa lock; nó ném lỗi thì lock được giữ để
    lần dọn sau thử lại.
    """
    path = Path(path)
    with os_mutex(path.with_name(path.name + ".guard")):
        holder = read_lock(path)
        if not holder or owner_alive(holder):
            return None
        if on_break:
            on_break(holder)
        _unlink_retry(path, retry or RetryPolicy())
        return holder
