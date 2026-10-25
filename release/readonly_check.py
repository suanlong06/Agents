"""Kiểm tra read-only (bước 11; PR-4a; SPEC mục 8 câu 14).

Định nghĩa (chi tiết trong ``docs/runbooks/readonly_check.md``):

**A. Bộ kiểm "hành động không ghi gì"** — ``verify_readonly(action, watch=...)`` chạy một
hành động đề xuất và chỉ ``ok`` khi CẢ HAI lớp đều không thấy thao tác ghi:

1. *Chặn trong process* bằng audit hook của Python (PEP 578): trong lúc kiểm, mọi sự kiện
   ghi bị CHẶN TRƯỚC KHI xảy ra (ném ``ReadOnlyViolation``, lớp con của
   ``PermissionError``) và được ghi lại, kể cả khi hành động nuốt lỗi:
   ``open`` ở chế độ ghi (``w``/``a``/``x``/``+`` hoặc cờ ``O_WRONLY``/``O_RDWR``/
   ``O_CREAT``/``O_APPEND``/``O_TRUNC``), xóa/đổi tên/``os.replace``, tạo/xóa thư mục,
   ``chmod``/``utime``/``truncate``/link, ``shutil`` copy/move/rmtree, ghi registry, chạy
   process (``subprocess``, ``os.system``, ``os.startfile``, ``os.kill``...) và mạng.
   Mạng chỉ được mở khi ``allow_network=True``, và khi đó request HTTP khác GET/HEAD vẫn
   bị chặn. Process chỉ được chạy khi ``allow_command(argv)`` cho phép (ví dụ
   ``git_readonly_command``: ``git log``/``rev-parse``/``cat-file``... với
   ``GIT_OPTIONAL_LOCKS=0``). Trong lúc kiểm ``sys.dont_write_bytecode`` bật để import
   không ghi ``.pyc``.
2. *So ảnh hệ thống file* trước/sau của mọi thư mục ``watch`` (đường dẫn, kích thước,
   mtime, sha256): bắt cả thao tác ghi mà lớp 1 không thấy (process con được cho phép,
   thread khác, code native).

**B. Kiểm tra sau release ở bước 11** — ``post_release_check`` chạy BÊN TRONG bộ kiểm A
(``watch`` = ``state_dir``), giữ ``release.lock`` suốt lúc kiểm (R10), gồm:

- ``killswitch_engaged``: kill-switch phải đang BẬT trong lúc kiểm (runbook release bật nó
  trước khi đổi tag), để không hành động tự động nào ghi ``state_dir`` cùng lúc.
- ``smoke``: import các module chính, mọi file ``schemas/*.schema.json`` là JSON Schema
  2020-12 hợp lệ, ``config/harness.yaml`` đọc được.
- ``health``: ``state.json`` đọc được và đúng schema; ``heartbeats.json`` có và
  orchestrator không ở trạng thái ``error``.
- ``log_errors``: số bản ghi audit lỗi (``result`` ∈ ``error``/``rejected``) có ``seq`` >
  ``since_seq`` (seq lúc release) không vượt ``max_new_errors`` (mặc định tạm 0, R16).

Best-effort (R15): audit hook không chặn được code native/ctypes hay process khác cùng OS
user; lớp 2 chỉ thấy thư mục được theo dõi. Đây là kiểm tra cho phần mềm hợp tác, không
phải sandbox.

CLI::

    python -m release.readonly_check seq                    # seq audit hiện tại (ghi lại trước release)
    python -m release.readonly_check run --since-seq N      # báo cáo JSON; exit 0 = đạt, 1 = không đạt
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import shlex
import sys
import threading
import traceback
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

SNAPSHOT_HASH_MAX = 64 * 1024 * 1024
ERROR_RESULTS = frozenset({"error", "rejected"})
SMOKE_MODULES = (
    "orchestrator.config", "orchestrator.schemas", "orchestrator.state", "orchestrator.fold",
    "orchestrator.audit", "orchestrator.ledger", "orchestrator.heartbeat", "orchestrator.inbox",
    "orchestrator.lock", "orchestrator.worktree", "orchestrator.killswitch",
    "adapters.base", "adapters.router",
    "pipeline.common", "pipeline.labeler", "pipeline.ci_status", "pipeline.gate3", "pipeline.fix_loop",
    "pipeline.approvals", "release.manifest", "release.shadow_eval",
)

_WRITE_FLAGS = 0
for _name in ("O_WRONLY", "O_RDWR", "O_APPEND", "O_CREAT", "O_TRUNC", "O_EXCL", "O_TEMPORARY"):
    _WRITE_FLAGS |= getattr(os, _name, 0)

FS_WRITE_EVENTS = frozenset({
    "os.remove", "os.rename", "os.rmdir", "os.mkdir", "os.chmod", "os.chown", "os.chflags", "os.lchflags",
    "os.truncate", "os.link", "os.symlink", "os.utime", "os.setxattr", "os.removexattr",
    "shutil.copyfile", "shutil.copymode", "shutil.copystat", "shutil.copytree", "shutil.rmtree",
    "shutil.move", "shutil.chown", "shutil.make_archive", "shutil.unpack_archive",
    "winreg.CreateKey", "winreg.DeleteKey", "winreg.DeleteValue", "winreg.SetValue", "winreg.SaveKey",
    "winreg.LoadKey", "winreg.DisableReflectionKey", "winreg.EnableReflectionKey",
})
PROCESS_EVENTS = frozenset({
    "os.system", "os.exec", "os.posix_spawn", "os.spawn", "os.startfile", "os.fork", "os.forkpty",
    "os.kill", "os.killpg", "pty.spawn", "_winapi.TerminateProcess",
})
# ``_winapi.CreateProcess`` không có ở đây: ``subprocess.Popen`` đã được xét (và có thể được
# ``allow_command`` cho phép) ngay trước nó.
NETWORK_EVENTS = frozenset({
    "socket.connect", "socket.sendto", "socket.sendmsg", "socket.bind", "ftplib.connect",
    "smtplib.connect", "imaplib.open", "poplib.connect", "nntplib.connect", "telnetlib.Telnet.open",
})
SAFE_HTTP_METHODS = frozenset({"GET", "HEAD"})
GIT_READONLY_SUBCOMMANDS = frozenset({
    "log", "rev-parse", "cat-file", "ls-tree", "ls-files", "diff", "show", "rev-list", "merge-base",
    "for-each-ref", "show-ref", "describe", "name-rev", "blame", "grep", "version",
})


class ReadOnlyViolation(PermissionError):
    """Thao tác ghi bị chặn trong lúc kiểm read-only."""


class ReadOnlyCheckError(RuntimeError):
    pass


@dataclass(frozen=True)
class Violation:
    kind: str  # "write" | "process" | "network"
    event: str
    detail: str


@dataclass
class ReadOnlyReport:
    ok: bool
    readonly_ok: bool
    violations: list[Violation] = field(default_factory=list)
    fs_changes: list[str] = field(default_factory=list)
    error: str | None = None
    result: Any = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["result"] = self.result if _jsonable(self.result) else repr(self.result)
        return d


def _jsonable(x: Any) -> bool:
    try:
        json.dumps(x)
        return True
    except (TypeError, ValueError):
        return False


# --- lớp 1: audit hook ------------------------------------------------------------------


class _Session:
    def __init__(self, allow_network: bool, allow_command: Callable[[Sequence[str]], bool] | None):
        self.allow_network = allow_network
        self.allow_command = allow_command
        self.violations: list[Violation] = []


_active: _Session | None = None
_install_lock = threading.Lock()
_session_lock = threading.Lock()
_installed = False


def _short(x: Any, n: int = 300) -> str:
    try:
        s = repr(x)
    except Exception:  # noqa: BLE001
        s = f"<{type(x).__name__}>"
    return s if len(s) <= n else s[:n] + "…"


def _is_write_open(args: tuple) -> bool:
    path = args[0] if args else None
    if isinstance(path, int):
        return False  # bọc fd đã có (pipe của process con): fd đó đã được xét lúc mở
    if isinstance(path, (str, bytes, os.PathLike)) and             os.path.normcase(os.fsdecode(path)) == os.path.normcase(os.devnull):
        return False  # thiết bị null: không ghi gì xuống đĩa
    mode = args[1] if len(args) > 1 else None
    flags = args[2] if len(args) > 2 else 0
    if isinstance(mode, str) and any(c in mode for c in "wax+"):
        return True
    return isinstance(flags, int) and bool(flags & _WRITE_FLAGS)


def _argv(args: tuple) -> list[str]:
    raw = args[1] if len(args) > 1 else None
    if isinstance(raw, (bytes, os.PathLike)):
        raw = os.fsdecode(raw)
    if isinstance(raw, str):
        # Windows: sự kiện mang command line đã ghép (list2cmdline) → tách lại.
        raw = [a[1:-1] if len(a) > 1 and a[0] == a[-1] == '"' else a for a in shlex.split(raw, posix=False)]
    out = []
    for a in raw or ():
        out.append(os.fsdecode(a) if isinstance(a, (bytes, os.PathLike)) else str(a))
    return out


def _http_method(data: Any) -> str | None:
    if isinstance(data, (bytes, bytearray, memoryview)):
        head = bytes(data[:16]).split(b" ", 1)[0]
        return head.decode("ascii", "replace").upper() if head else None
    return None


def _classify(s: _Session, event: str, args: tuple) -> tuple[str, str] | None:
    if event == "open":
        return ("write", _short(args[:2])) if _is_write_open(args) else None
    if event in FS_WRITE_EVENTS:
        return "write", _short(args)
    if event == "subprocess.Popen":
        argv = _argv(args)
        if s.allow_command is not None and argv and s.allow_command(argv):
            return None
        return "process", _short(argv)
    if event in PROCESS_EVENTS:
        return "process", _short(args)
    if event in NETWORK_EVENTS:
        return None if s.allow_network else ("network", _short(args))
    if event == "urllib.Request":
        method = (args[3] if len(args) > 3 and args[3] else ("POST" if len(args) > 1 and args[1] else "GET"))
        if not s.allow_network:
            return "network", _short(args[:1])
        return None if str(method).upper() in SAFE_HTTP_METHODS else ("network", f"HTTP {method} {_short(args[:1])}")
    if event == "http.client.send":
        method = _http_method(args[1] if len(args) > 1 else None)
        if method and method not in SAFE_HTTP_METHODS and method.isalpha():
            return "network", f"HTTP {method}"
        return None
    return None


def _hook(event: str, args: tuple) -> None:
    s = _active
    if s is None:
        return
    hit = _classify(s, event, args)
    if hit is None:
        return
    kind, detail = hit
    s.violations.append(Violation(kind, event, detail))
    raise ReadOnlyViolation(f"read-only: {event} bị chặn ({kind}): {detail}")


def _install() -> None:
    global _installed
    with _install_lock:
        if not _installed:
            sys.addaudithook(_hook)  # không gỡ được; không làm gì khi không có phiên kiểm
            _installed = True


@contextmanager
def _guard(session: _Session):
    global _active
    _install()
    if not _session_lock.acquire(blocking=False):
        raise ReadOnlyCheckError("đang có một phiên kiểm read-only khác")
    old_bytecode = sys.dont_write_bytecode
    try:
        sys.dont_write_bytecode = True
        _active = session
        yield session
    finally:
        _active = None
        sys.dont_write_bytecode = old_bytecode
        _session_lock.release()


# --- lớp 2: ảnh hệ thống file --------------------------------------------------------------


def snapshot(roots: Iterable[Path | str]) -> dict[str, tuple]:
    """``{đường dẫn: (loại, kích thước, mtime_ns, sha256)}`` của mọi thứ dưới ``roots``."""
    out: dict[str, tuple] = {}
    for root in roots:
        root = Path(root)
        if not root.exists():
            out[str(root)] = ("absent",)
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames.sort()
            out[dirpath] = ("dir",)
            for name in sorted(filenames):
                p = os.path.join(dirpath, name)
                try:
                    st = os.stat(p)
                    digest = None
                    if st.st_size <= SNAPSHOT_HASH_MAX:
                        h = hashlib.sha256()
                        with open(p, "rb") as f:
                            for chunk in iter(lambda: f.read(1 << 20), b""):
                                h.update(chunk)
                        digest = h.hexdigest()
                    out[p] = ("file", st.st_size, st.st_mtime_ns, digest)
                except OSError as exc:
                    out[p] = ("unreadable", type(exc).__name__)
    return out


def diff_snapshots(before: dict, after: dict) -> list[str]:
    changes = []
    for p in sorted(set(before) | set(after)):
        if p not in before:
            changes.append(f"+ {p}")
        elif p not in after:
            changes.append(f"- {p}")
        elif before[p] != after[p]:
            changes.append(f"~ {p}")
    return changes


# --- A: bộ kiểm hành động ---------------------------------------------------------------


def git_readonly_command(argv: Sequence[str]) -> bool:
    """Cho phép ``git <lệnh đọc>`` (bỏ qua ``-c k=v`` / ``-C dir`` / ``--no-pager``)."""
    if not argv or Path(argv[0]).name.lower() not in ("git", "git.exe"):
        return False
    i = 1
    while i < len(argv):
        a = argv[i]
        if a in ("-c", "-C"):
            i += 2
            continue
        if a in ("--no-pager", "--no-optional-locks"):
            i += 1
            continue
        break
    return i < len(argv) and argv[i] in GIT_READONLY_SUBCOMMANDS


def verify_readonly(action: Callable[..., Any], *args, watch: Iterable[Path | str] = (),
                    allow_network: bool = False,
                    allow_command: Callable[[Sequence[str]], bool] | None = None, **kwargs) -> ReadOnlyReport:
    """Chạy ``action(*args, **kwargs)`` và kiểm nó không ghi gì (2 lớp, xem docstring module)."""
    roots = [Path(w) for w in watch]
    before = snapshot(roots)
    session = _Session(allow_network, allow_command)
    result, error = None, None
    with _guard(session):
        try:
            result = action(*args, **kwargs)
        except ReadOnlyViolation as exc:
            error = f"ReadOnlyViolation: {exc}"
        except Exception as exc:  # noqa: BLE001 — báo cáo, không làm sập bên gọi
            error = f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=5)}"[:2000]
    changes = diff_snapshots(before, snapshot(roots))
    readonly_ok = not session.violations and not changes
    return ReadOnlyReport(ok=readonly_ok and error is None, readonly_ok=readonly_ok,
                          violations=list(session.violations), fs_changes=changes, error=error, result=result)


# --- B: kiểm tra sau release ------------------------------------------------------------


@dataclass(frozen=True)
class ProbeResult:
    name: str
    passed: bool
    detail: dict


def probe_killswitch(killswitch) -> ProbeResult:
    st = killswitch.status()
    return ProbeResult("killswitch_engaged", st.engaged, {
        "state": st.state, "reason": st.reason,
        "why": "kiểm tra chạy khi kill-switch BẬT để không hành động tự động nào ghi state_dir cùng lúc",
    })


def probe_smoke(repo_root: Path, modules: Sequence[str] = SMOKE_MODULES) -> ProbeResult:
    import yaml
    from jsonschema import Draft202012Validator

    problems = []
    for m in modules:
        try:
            importlib.import_module(m)
        except Exception as exc:  # noqa: BLE001
            problems.append(f"import {m}: {type(exc).__name__}: {exc}")
    schemas = sorted((Path(repo_root) / "schemas").glob("*.schema.json"))
    for p in schemas:
        try:
            Draft202012Validator.check_schema(json.loads(p.read_text(encoding="utf-8")))
        except Exception as exc:  # noqa: BLE001
            problems.append(f"{p.name}: {type(exc).__name__}: {exc}"[:300])
    try:
        with open(Path(repo_root) / "config" / "harness.yaml", encoding="utf-8") as f:
            if not isinstance(yaml.safe_load(f), dict):
                problems.append("config/harness.yaml: gốc không phải mapping")
    except Exception as exc:  # noqa: BLE001
        problems.append(f"config/harness.yaml: {type(exc).__name__}: {exc}")
    return ProbeResult("smoke", not problems, {"modules": len(modules), "schemas": len(schemas),
                                               "problems": problems})


def probe_health(state_dir: Path) -> ProbeResult:
    from orchestrator.heartbeat import HeartbeatMonitor
    from orchestrator.state import StateStore, read_snapshot

    problems, detail = [], {}
    state_path = Path(state_dir) / StateStore.FILE
    try:
        snap = read_snapshot(state_path)
        detail["tasks"] = len(snap.get("tasks") or {})
        detail["quarantined"] = len(snap.get("quarantine") or [])
    except FileNotFoundError:
        problems.append(f"không có {StateStore.FILE}: không có dữ liệu (khác với 0)")
    except Exception as exc:  # noqa: BLE001
        problems.append(f"{StateStore.FILE}: {type(exc).__name__}: {exc}"[:300])
    try:
        hb = HeartbeatMonitor.read_summary(Path(state_dir))
        if hb is None:
            problems.append("không có heartbeats.json: không biết orchestrator có khỏe không")
        else:
            orch = (hb.get("orchestrator") or {})
            detail["orchestrator"] = orch.get("status")
            detail["heartbeats_as_of"] = hb.get("as_of")
            detail["agents"] = {a.get("agent"): a.get("status") for a in hb.get("agents") or []}
            if orch.get("status") != "ok":
                problems.append(f"orchestrator {orch.get('status')!r}: {orch.get('last_error')}")
    except Exception as exc:  # noqa: BLE001
        problems.append(f"heartbeats.json: {type(exc).__name__}: {exc}"[:300])
    detail["problems"] = problems
    return ProbeResult("health", not problems, detail)


def probe_log_errors(state_dir: Path, since_seq: int, max_new_errors: int) -> ProbeResult:
    from orchestrator.audit import AuditLog

    recs = AuditLog.read(Path(state_dir) / AuditLog.FILE)
    before = [r for r in recs if r["seq"] <= since_seq and r.get("result") in ERROR_RESULTS]
    after = [r for r in recs if r["seq"] > since_seq and r.get("result") in ERROR_RESULTS]
    return ProbeResult("log_errors", len(after) <= max_new_errors, {
        "since_seq": since_seq, "max_seq": max((r["seq"] for r in recs), default=0),
        "errors_before": len(before), "errors_after": len(after), "max_new_errors": max_new_errors,
        "new_errors": [{"seq": r["seq"], "action": r["action"], "result": r["result"]} for r in after[:20]],
    })


def current_audit_seq(state_dir: Path) -> int:
    from orchestrator.audit import AuditLog

    return max((r["seq"] for r in AuditLog.read(Path(state_dir) / AuditLog.FILE)), default=0)


def _run_probes(state_dir: Path, repo_root: Path, killswitch, since_seq: int, max_new_errors: int) -> list[dict]:
    probes = [
        probe_killswitch(killswitch),
        probe_smoke(repo_root),
        probe_health(state_dir),
        probe_log_errors(state_dir, since_seq, max_new_errors),
    ]
    return [asdict(p) for p in probes]


def post_release_check(state_dir: Path | str, *, since_seq: int, killswitch, repo_root: Path | str | None = None,
                       max_new_errors: int = 0, lock: bool = True) -> dict:
    """Kiểm tra bước 11. Trả báo cáo JSON; ``ok`` chỉ khi mọi probe đạt VÀ không ghi gì."""
    from orchestrator.config import REPO_ROOT
    from orchestrator.lock import LockHeld, PidLock

    if isinstance(since_seq, bool) or not isinstance(since_seq, int) or since_seq < 0:
        raise ReadOnlyCheckError(f"since_seq phải là số nguyên >= 0, nhận {since_seq!r}")
    state_dir = Path(state_dir)
    repo_root = Path(repo_root) if repo_root else REPO_ROOT
    release_lock = PidLock(state_dir / "release.lock", meta={"purpose": "readonly_check"}) if lock else None
    if release_lock is not None:
        try:
            release_lock.acquire()  # ghi lock TRƯỚC ảnh "trước"; không thuộc phần được kiểm
        except LockHeld as exc:
            return {"ok": False, "readonly_ok": None, "probes": [], "error": f"release.lock đang bị giữ: {exc}"}
    try:
        rep = verify_readonly(_run_probes, state_dir, repo_root, killswitch, since_seq, max_new_errors,
                              watch=[state_dir])
    finally:
        if release_lock is not None:
            release_lock.release()
    probes = rep.result or []
    return {
        "ok": rep.ok and bool(probes) and all(p["passed"] for p in probes),
        "readonly_ok": rep.readonly_ok,
        "probes": probes,
        "violations": [asdict(v) for v in rep.violations],
        "fs_changes": rep.fs_changes,
        "error": rep.error,
    }


def main(argv: list[str] | None = None) -> int:
    import yaml

    from orchestrator.config import DEFAULT_CONFIG_PATH, load_config
    from orchestrator.killswitch import KillSwitch

    ap = argparse.ArgumentParser(prog="python -m release.readonly_check")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("seq", help="in seq audit hiện tại (ghi lại TRƯỚC khi đổi tag)")
    run = sub.add_parser("run", help="kiểm tra read-only sau release")
    run.add_argument("--since-seq", type=int, required=True)
    args = ap.parse_args(argv)
    cfg = load_config()
    if args.cmd == "seq":
        print(current_audit_seq(cfg.state_dir))
        return 0
    with open(DEFAULT_CONFIG_PATH, encoding="utf-8") as f:
        max_new = int(((yaml.safe_load(f) or {}).get("readonly_check") or {}).get("max_new_errors", 0))
    report = post_release_check(cfg.state_dir, since_seq=args.since_seq, killswitch=KillSwitch.from_config(),
                                max_new_errors=max_new)
    print(json.dumps(report, ensure_ascii=True, indent=2))  # ASCII: an toàn với stdout cp1252
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
