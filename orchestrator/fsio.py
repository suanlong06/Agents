"""I/O file cho orchestrator (PR-1a).

- Ghi nguyên tử: write-temp + ``os.replace``, retry có giới hạn khi Windows trả
  ``PermissionError`` (WinError 5/32) vì một reader (dashboard, Defender...) đang
  giữ file đích (R3).
- Append JSONL + fsync; đọc JSONL chỉ lấy các dòng đã kết thúc bằng ``\\n``
  (dòng cuối ghi dở bị bỏ qua và đọc lại ở lần sau).
- ``WriterGuard``: khóa OS độc quyền trên ``state_dir`` để chỉ MỘT orchestrator
  ghi state/ledger/audit (R3). Khóa do OS giữ nên tự nhả khi process chết; không
  có logic dọn lock cũ (việc đó thuộc PR-1b).
"""

from __future__ import annotations

import json
import os
import secrets
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator


class StateWriteError(OSError):
    """Ghi nguyên tử thất bại sau khi đã hết số lần retry."""


class WriterBusy(RuntimeError):
    """Đã có một orchestrator khác giữ quyền ghi ``state_dir``."""


class NotWriter(RuntimeError):
    """Gọi thao tác ghi khi không giữ ``WriterGuard``."""


@dataclass(frozen=True)
class RetryPolicy:
    # Các con số là mặc định tạm trong config (R16), không phải yêu cầu.
    max_attempts: int = 8
    backoff_initial_s: float = 0.05
    backoff_max_s: float = 0.5


def dumps_line(obj: dict) -> bytes:
    """Một bản ghi JSONL: UTF-8, key sắp xếp, kết thúc bằng ``\\n``."""
    return (json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def atomic_write_bytes(
    path: Path,
    data: bytes,
    policy: RetryPolicy,
    *,
    sleep: Callable[[float], None] = time.sleep,
    replace: Callable[[Path, Path], None] = os.replace,
) -> int:
    """Ghi ``data`` vào ``path`` nguyên tử. Trả về số lần thử ``os.replace``.

    Ném ``StateWriteError`` khi hết retry; khi đó file đích giữ nguyên nội dung cũ
    và file tạm bị xóa.
    """
    path = Path(path)
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}-{secrets.token_hex(4)}")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    delay = policy.backoff_initial_s
    last_exc: BaseException | None = None
    try:
        for attempt in range(1, policy.max_attempts + 1):
            try:
                replace(tmp, path)
                return attempt
            except PermissionError as exc:  # WinError 5 / 32: file đích đang bị giữ
                last_exc = exc
                if attempt < policy.max_attempts:
                    sleep(delay)
                    delay = min(delay * 2, policy.backoff_max_s)
        raise StateWriteError(
            f"không ghi được {path} sau {policy.max_attempts} lần thử: {last_exc}"
        ) from last_exc
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass  # file tạm mồ côi chỉ tốn chỗ, không làm hỏng state


def append_line(path: Path, obj: dict) -> None:
    """Append một bản ghi JSONL rồi fsync."""
    with open(path, "ab") as f:
        f.write(dumps_line(obj))
        f.flush()
        os.fsync(f.fileno())


def terminate_torn_tail(path: Path) -> bool:
    """Nếu file kết thúc bằng một dòng ghi dở (crash giữa lúc append), thêm ``\\n``.

    Không xóa byte nào (append-only): mảnh ghi dở thành một dòng hỏng mà reader
    bỏ qua. Trả về ``True`` nếu đã phải thêm ``\\n``.
    """
    path = Path(path)
    if not path.exists() or path.stat().st_size == 0:
        return False
    with open(path, "rb") as f:
        f.seek(-1, os.SEEK_END)
        if f.read(1) == b"\n":
            return False
    with open(path, "ab") as f:
        f.write(b"\n")
        f.flush()
        os.fsync(f.fileno())
    return True


def iter_complete_lines(path: Path, offset: int) -> tuple[list[tuple[int, bytes]], int]:
    """Đọc từ byte ``offset``; trả về ``([(offset_dòng, nội dung)], offset_mới)``.

    Chỉ trả các dòng đã có ``\\n``. Phần đuôi chưa có ``\\n`` (đang ghi dở) không
    được tính, ``offset_mới`` dừng ngay trước nó để lần fold sau đọc lại.
    """
    path = Path(path)
    if not path.exists():
        return [], offset
    size = path.stat().st_size
    if size < offset:
        raise ValueError(f"{path} ngắn hơn offset đã lưu ({size} < {offset}); file bị cắt hoặc thay thế")
    with open(path, "rb") as f:
        f.seek(offset)
        data = f.read()
    lines: list[tuple[int, bytes]] = []
    pos = 0
    while True:
        nl = data.find(b"\n", pos)
        if nl == -1:
            break
        lines.append((offset + pos, data[pos:nl].rstrip(b"\r")))
        pos = nl + 1
    return lines, offset + pos


def read_jsonl(path: Path) -> Iterator[tuple[int, dict | None, bytes]]:
    """Đọc mọi dòng hoàn chỉnh: ``(offset, obj hoặc None nếu hỏng, raw)``."""
    lines, _ = iter_complete_lines(path, 0)
    for off, raw in lines:
        if not raw.strip():
            continue
        try:
            obj = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            yield off, None, raw
            continue
        yield off, (obj if isinstance(obj, dict) else None), raw


class WriterGuard:
    """Khóa ghi độc quyền cho một ``state_dir`` (R3: một writer duy nhất).

    Windows: ``msvcrt.locking`` trên byte 0 của ``orchestrator.writer.lock``.
    POSIX: ``fcntl.flock``. OS nhả khóa khi handle đóng hoặc process chết.
    """

    FILE = "orchestrator.writer.lock"

    def __init__(self, state_dir: Path):
        self.path = Path(state_dir) / self.FILE
        self._fh = None

    @property
    def held(self) -> bool:
        return self._fh is not None

    def ensure_held(self) -> None:
        if not self.held:
            raise NotWriter("chỉ orchestrator giữ WriterGuard mới được ghi state/ledger/audit")

    def acquire(self) -> "WriterGuard":
        if self.held:
            return self
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+b")
        try:
            if sys.platform == "win32":
                import msvcrt

                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            fh.close()
            raise WriterBusy(f"state_dir đang có writer khác: {self.path}") from exc
        self._fh = fh
        return self

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            if sys.platform == "win32":
                import msvcrt

                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        finally:
            self._fh.close()
            self._fh = None

    def __enter__(self) -> "WriterGuard":
        return self.acquire()

    def __exit__(self, *exc) -> None:
        self.release()
