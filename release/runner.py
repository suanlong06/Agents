"""Runner tự động của PR-4b: lịch KST → digest → batch release → revert tự động.

- **Kill-switch trước mọi hành động tự động** (R8): ``tick`` đọc kill-switch đầu tiên và không
  làm gì (không chạy job, không ghi gì) khi nó BẬT; kiểm lại trước MỖI job; digest/batch/revert
  kiểm lại trước MỖI thao tác bên trong. Kill-switch luôn là ``KillSwitch`` THẬT, được truyền
  TƯỜNG MINH cho mọi thành phần (F1): không có mặc định "không kiểm".
- Job lỡ cửa sổ (quá ``grace_minutes``, kể cả vì kill-switch bật lúc đó) bị **bỏ qua và thông
  báo**, không chạy bù. Job đang ``started`` khi runner chết thì không chạy lại: ``interrupted``,
  báo động (batch có thể đã merge một phần).
- Trạng thái lịch: ``<state_dir>/scheduler.json`` (ngoài repo, R13), ghi nguyên tử, chỉ khi giữ
  ``WriterGuard`` (runner là writer duy nhất của state trong lúc chạy, R3).
- **KHÔNG kích hoạt lịch**: ``scheduler.enabled`` mặc định ``false``; ``tick``/``serve`` từ chối
  chạy khi chưa bật. Không có tác vụ Windows Task Scheduler nào được tạo. Bật là quyết định riêng
  của người dùng sau khi merge (``docs/runbooks/batch.md``).
- **Không có auto-merge**: batch chỉ merge PR có phê duyệt của người dùng (``release.batch``).
- **OS user**: runner chạy dưới OS user HIỆN TẠI. Tách Windows user riêng ít quyền + ACL trên
  thư mục state vẫn là **điều kiện triển khai còn mở** trước khi bật bất kỳ auto-merge hay thao
  tác ghi nào từ dashboard (SPEC mục 5.2, 7; R15). State, audit và kill-switch vẫn chỉ là
  best-effort, không chống giả mạo.

CLI::

    python -m release.runner status     # chỉ đọc: kill-switch, lịch hôm nay, incident, enabled
    python -m release.runner tick       # một lượt (CHỈ khi scheduler.enabled = true)
    python -m release.runner serve      # vòng lặp (CHỈ khi scheduler.enabled = true)
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Mapping

from orchestrator.fsio import RetryPolicy, WriterGuard, atomic_write_bytes
from orchestrator.killswitch import KillSwitchEngaged

from .batch_config import record
from .schedule import BATCH, DIGEST, MISSED, SchedulerSettings, jobs_on, plan, to_kst

SCHED_FILE = "scheduler.json"
SCHED_VERSION = 1
KEEP_DAYS = 14
OS_USER_NOTE = ("runner chạy dưới OS user hiện tại; tách OS user + ACL thư mục state là điều kiện "
                "triển khai còn mở trước mọi auto-merge hay thao tác ghi từ dashboard (R15)")


class RunnerError(RuntimeError):
    pass


def read_sched_state(state_dir: Path) -> dict:
    p = Path(state_dir) / SCHED_FILE
    if not p.exists():
        return {"v": SCHED_VERSION, "last_tick": None, "jobs": {}}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("jobs"), dict) or data.get("v") != SCHED_VERSION:
            raise ValueError("cấu trúc lạ")
    except Exception as exc:  # noqa: BLE001 — fail-closed: không đoán job nào đã chạy
        raise RunnerError(f"{SCHED_FILE} hỏng ({type(exc).__name__}: {exc}); người dùng kiểm tra rồi xóa file") from exc
    return data


class Runner:
    def __init__(self, *, killswitch, settings: SchedulerSettings, state_dir: Path, guard: WriterGuard,
                 handlers: Mapping[str, Callable], notifier, audit, clock: Callable[[], datetime],
                 retry: RetryPolicy | None = None):
        if killswitch is None or not callable(getattr(killswitch, "check", None)) \
                or not callable(getattr(killswitch, "status", None)):
            raise TypeError("Runner cần kill-switch có status()/check() (F1, fail-closed)")
        if set(handlers) != {DIGEST, BATCH}:
            raise ValueError(f"handlers phải có đúng {DIGEST!r} và {BATCH!r}")
        guard.ensure_held()
        self.killswitch = killswitch
        self.settings = settings
        self.state_dir = Path(state_dir)
        self.guard = guard
        self.handlers = dict(handlers)
        self.notifier = notifier
        self.audit = audit
        self.clock = clock
        self.retry = retry or RetryPolicy()

    def _save(self, state: dict) -> None:
        self.guard.ensure_held()
        p = self.state_dir / SCHED_FILE
        p.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(p, json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8"),
                           self.retry)

    def _mark(self, state: dict, job, status: str, **extra) -> dict:
        rec = {"status": status, "kind": job.kind, "window_id": job.window_id, "at": job.at.isoformat(),
               "updated_at": self.clock().isoformat(), **extra}
        state["jobs"][job.job_id] = rec
        self._save(state)
        return {"job_id": job.job_id, **rec}

    def tick(self, now: datetime | None = None) -> list[dict]:
        now = to_kst(now or self.clock(), self.settings)
        st = self.killswitch.status()
        if st.engaged:  # kill-switch BẬT: không chạy gì, không ghi gì
            return [{"job_id": None, "status": "stopped", "reason": f"kill-switch bật: {st.reason}"}]
        state = read_sched_state(self.state_dir)
        out: list[dict] = []
        for jid, rec in sorted(state["jobs"].items()):
            if rec.get("status") == "started":
                rec.update(status="interrupted", updated_at=self.clock().isoformat())
                self._save(state)
                self.notifier.notify(kind="escalated", task_id=None,
                                     message=f"{jid} bị ngắt giữa chừng (runner dừng/crash). Không chạy lại; "
                                             "kiểm tra batches/ và GitHub (có thể đã merge một phần).",
                                     detail={"job_id": jid})
                out.append({"job_id": jid, **rec})
        last = datetime.fromisoformat(state["last_tick"]) if state.get("last_tick") else None
        stopped = False
        for due in plan(now, done=state["jobs"].keys(), last_tick=last, settings=self.settings):
            job = due.job
            try:
                self.killswitch.check(f"runner.{job.kind}")
            except KillSwitchEngaged as exc:
                out.append({"job_id": job.job_id, "status": "stopped", "reason": str(exc)})
                stopped = True
                break
            if due.mode == MISSED:
                out.append(self._mark(state, job, "missed", late_s=int(due.late_by.total_seconds())))
                record(self.audit, f"runner.{job.kind}", "missed", detail={"window_id": job.window_id,
                                                                          "late_s": int(due.late_by.total_seconds())},
                       dedup_key=f"runner:{job.job_id}:missed")
                self.notifier.notify(kind="info", task_id=None,
                                     message=f"Lỡ cửa sổ {job.kind} {job.window_id} (trễ "
                                             f"{int(due.late_by.total_seconds() // 60)} phút): BỎ QUA, không chạy bù.",
                                     detail={"job_id": job.job_id})
                continue
            if job.kind == BATCH:
                dig = state["jobs"].get(f"{DIGEST}:{job.window_id}") or {}
                if dig.get("status") != "ok":
                    out.append(self._mark(state, job, "skipped", reason=f"digest của cửa sổ: {dig.get('status')}"))
                    self.notifier.notify(kind="info", task_id=None,
                                         message=f"Batch {job.window_id} BỎ QUA: không có digest đóng băng "
                                                 f"({dig.get('status') or 'chưa chạy'}).",
                                         detail={"job_id": job.job_id})
                    continue
            self._mark(state, job, "started")
            try:
                result = self.handlers[job.kind](job)
            except KillSwitchEngaged as exc:
                out.append(self._mark(state, job, "stopped", reason=str(exc)[:500]))
                stopped = True
                break
            except Exception as exc:  # noqa: BLE001 — báo động, không chạy lại
                reason = f"{type(exc).__name__}: {exc}"[:1000]
                out.append(self._mark(state, job, "failed", reason=reason))
                record(self.audit, f"runner.{job.kind}", "failed", detail={"window_id": job.window_id,
                                                                          "reason": reason})
                self.notifier.notify(kind="escalated", task_id=None, message=f"{job.job_id} lỗi: {reason}",
                                     detail={"job_id": job.job_id})
                continue
            out.append(self._mark(state, job, "ok", result=(result or {}).get("status")))
        if not stopped:
            state["last_tick"] = now.isoformat()
            horizon = now - timedelta(days=KEEP_DAYS)
            state["jobs"] = {k: v for k, v in state["jobs"].items()
                             if datetime.fromisoformat(v["at"]) >= horizon or v.get("status") == "started"}
            self._save(state)
        return out

    def serve(self, *, should_stop: Callable[[], bool] = lambda: False,
              sleep: Callable[[float], None] = time.sleep) -> None:
        if not self.settings.enabled:
            raise RunnerError("scheduler.enabled = false: lịch CHƯA được kích hoạt (quyết định của người dùng)")
        while not should_stop():
            self.tick()
            sleep(self.settings.tick_s)


# --- dựng runner thật ---------------------------------------------------------------------


def build_runner(config_path: Path | None = None, *, env: Mapping[str, str] | None = None,
                 guard: WriterGuard | None = None):
    """Dựng runner với các thành phần THẬT. Kill-switch: ``KillSwitch.from_config`` (fail-closed),
    MỘT object, truyền TƯỜNG MINH cho Runner, DigestBuilder, BatchRelease và AutoRevert (F1)."""
    import os

    from adapters.muse_monitor import MuseMonitor
    from orchestrator.audit import AuditLog
    from orchestrator.config import load_config
    from orchestrator.killswitch import KillSwitch
    from pipeline.common import load_pipeline_settings
    from pipeline.github_api import GitHubClient

    from .auto_revert import AutoRevert
    from .batch import BatchRelease
    from .batch_config import BatchSettings
    from .digest import DigestBuilder
    from .github_write import GitHubWriter
    from .gitops import Git
    from .release_check import PinnedReleaseCheck

    env = os.environ if env is None else env
    cfg = load_config(config_path, env=env)
    sched = SchedulerSettings.load(config_path)
    bs = BatchSettings.load(config_path, env=env)
    ps = load_pipeline_settings(config_path)
    killswitch = KillSwitch.from_config(config_path, env=env)
    tz = sched.tz

    def now() -> datetime:
        return datetime.now(tz)

    guard = guard or WriterGuard(cfg.state_dir).acquire()
    audit = AuditLog(cfg.state_dir, guard, clock=lambda: now().isoformat(timespec="milliseconds"))
    notifier = MuseMonitor(cfg.state_dir, timezone=sched.timezone)
    client = GitHubClient.from_settings(ps.github, env=env)
    token = bs.merge_token(env)
    writer = GitHubWriter(ps.github.repo, token=token, api_url=ps.github.api_url, base_branch=bs.base_branch,
                          integration_prefix=bs.integration_prefix, revert_prefix=bs.revert_prefix,
                          tag_prefix=bs.tag_prefix) if token else None
    git = Git(bs.workdir)
    common_kw = dict(state_dir=cfg.state_dir, guard=guard, audit=audit, killswitch=killswitch, notifier=notifier)
    reverter = AutoRevert(git=git, writer=writer, settings=bs, clock=now, token=token, retry=cfg.write_retry,
                          **common_kw)
    digest = DigestBuilder(client=client, git=git, settings=bs, clock=now, retry=cfg.write_retry, **common_kw)
    batch = BatchRelease(client=client, writer=writer, git=git, settings=bs, owner_logins=ps.github.owner_logins,
                         release_check=PinnedReleaseCheck(git=git, state_dir=cfg.state_dir,
                                                          worktree_root=bs.workdir.parent,
                                                          timeout_s=bs.check_timeout_s),
                         reverter=reverter, clock=now, retry=cfg.write_retry, **common_kw)

    def need_workdir(fn):
        def run(job):
            if not (bs.workdir / ".git").exists():
                raise RunnerError(f"batch.workdir {bs.workdir} chưa có clone (người dùng tạo khi kích hoạt)")
            return fn(job)
        return run

    runner = Runner(killswitch=killswitch, settings=sched, state_dir=cfg.state_dir, guard=guard,
                    handlers={DIGEST: need_workdir(digest.build), BATCH: need_workdir(batch.run)},
                    notifier=notifier, audit=audit, clock=now, retry=cfg.write_retry)
    return runner, {"digest": digest, "batch": batch, "reverter": reverter, "killswitch": killswitch,
                    "guard": guard, "writer": writer}


def status(config_path: Path | None = None, *, env: Mapping[str, str] | None = None) -> dict:
    """Chỉ đọc: không lấy WriterGuard, không ghi gì."""
    from orchestrator.config import load_config
    from orchestrator.killswitch import KillSwitch

    from .batch_config import BatchSettings
    from .incidents import active_incidents

    cfg = load_config(config_path, env=env)
    sched = SchedulerSettings.load(config_path)
    bs = BatchSettings.load(config_path, env=env)
    ks = KillSwitch.from_config(config_path, env=env).status()
    today = datetime.now(sched.tz).date()
    try:
        st = read_sched_state(cfg.state_dir)
    except RunnerError as exc:
        st = {"error": str(exc)}
    return {
        "scheduler_enabled": sched.enabled, "auto_merge": "disabled",
        "killswitch": ks.to_dict(), "merge_identities": list(bs.merge_identities),
        "merge_token_present": bool(bs.merge_token(env)), "workdir": str(bs.workdir),
        "jobs_today": [j.to_dict() for j in jobs_on(today, sched)], "scheduler_state": st,
        "active_incidents": active_incidents(cfg.state_dir), "os_user": OS_USER_NOTE,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m release.runner", description=__doc__.splitlines()[0])
    ap.add_argument("cmd", choices=("status", "tick", "serve"))
    args = ap.parse_args(argv)
    if args.cmd == "status":
        print(json.dumps(status(), ensure_ascii=True, indent=2))
        return 0
    if not SchedulerSettings.load().enabled:
        print("scheduler.enabled = false: lịch CHƯA được kích hoạt. Xem docs/runbooks/batch.md.".encode(
            "ascii", "backslashreplace").decode("ascii"), file=sys.stderr)
        return 2
    runner, parts = build_runner()
    try:
        if args.cmd == "tick":
            print(json.dumps(runner.tick(), ensure_ascii=True, indent=2))
        else:
            runner.serve()
    finally:
        parts["guard"].release()
    return 0


if __name__ == "__main__":
    sys.exit(main())
