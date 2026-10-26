"""Revert tự động theo R10 (PR-4b, SPEC mục 3 bước 12). Cùng các bước với
``docs/runbooks/revert.md``, chỉ khác là runner làm thay người:

1. Mở incident (``opened_by: runner``, ``status: open``) — còn incident ``open``/``frozen`` thì
   batch sau không chạy. Runner không ghi được kill-switch: thông báo yêu cầu NGƯỜI DÙNG bật nó.
2. Manifest lấy từ ``git log prevTag..newTag`` (``release.manifest``), không từ danh sách tự ghi.
3. Nhánh ``revert/<tag>`` đi ra từ ``main`` HIỆN TẠI (giữ mọi commit merge sau release), trong
   một worktree của clone làm việc; ``git revert`` từng PR theo thứ tự NGƯỢC manifest (merge
   commit ``-m 1``). Không force-push, không reset: revert chỉ THÊM commit.
4. **Conflict → dừng, đóng băng, báo động**: ``git revert --abort``, incident ``frozen`` với
   ``revert.conflict = {sha, files}``, thông báo ``escalated``. Không tự giải conflict.
5. Không conflict → đẩy nhánh (không force) và **mở PR revert**. PR revert đi qua labeller, CI,
   Gate 3 như mọi PR và **chỉ được merge khi người dùng duyệt** (thường là Owner-only vì chạm
   ``release/**``...). Runner KHÔNG BAO GIỜ merge PR revert: không có auto-merge.
6. Kill-switch được kiểm trước mỗi thao tác; bật giữa chừng thì dừng (incident vẫn ``open``).
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Callable

from orchestrator.fsio import RetryPolicy, WriterGuard

from .batch_config import BatchSettings, record
from .gitops import Git
from .incidents import new_incident, next_incident_id, write_incident
from .manifest import revert_plan


class AutoRevert:
    def __init__(self, *, state_dir: Path, guard: WriterGuard, audit, killswitch, git: Git, writer,
                 settings: BatchSettings, notifier, clock: Callable[[], datetime], token: str | None,
                 retry: RetryPolicy | None = None):
        if killswitch is None or not callable(getattr(killswitch, "check", None)):
            raise TypeError("AutoRevert cần kill-switch (F1, fail-closed)")
        self.state_dir = Path(state_dir)
        self.guard = guard
        self.audit = audit
        self.killswitch = killswitch
        self.git = git
        self.writer = writer
        self.settings = settings
        self.notifier = notifier
        self.clock = clock
        self.token = token
        self.retry = retry or RetryPolicy()
        self.last_incident_id: str | None = None  # incident mở ở lần run gần nhất (batch tránh mở trùng)

    def _save(self, inc: dict, note: str) -> None:
        inc["timeline"].append({"ts": self.clock().isoformat(), "actor": "runner", "note": note[:2000]})
        write_incident(self.state_dir, self.guard, inc, self.retry)

    def open_incident(self, *, trigger: str, summary: str, manifest: dict | None, prev_tag: str | None,
                      new_tag: str | None, readonly_report: dict | None, frozen: bool = False) -> dict:
        now = self.clock()
        inc = new_incident(
            incident_id=next_incident_id(self.state_dir, now), opened_at=now.isoformat(), trigger=trigger,
            summary=summary, severity="sev2", prev_tag=(manifest or {}).get("prev_tag", prev_tag),
            new_tag=(manifest or {}).get("new_tag", new_tag), manifest_id=(manifest or {}).get("manifest_id"),
            killswitch_engaged=self.killswitch.status().engaged, readonly_check=readonly_report,
            note=f"runner mở incident: {summary}. NGƯỜI DÙNG: bật kill-switch (runner không ghi được nó).")
        if frozen:
            inc["status"] = "frozen"
        write_incident(self.state_dir, self.guard, inc, self.retry)
        self.last_incident_id = inc["incident_id"]
        record(self.audit, "incident.opened", inc["status"], sha=None,
               detail={"incident_id": inc["incident_id"], "trigger": trigger, "summary": summary[:500]})
        return inc

    def run(self, *, manifest: dict, trigger: str, summary: str, readonly_report: dict | None) -> dict:
        self.last_incident_id = None
        self.killswitch.check("auto_revert.start")
        inc = self.open_incident(trigger=trigger, summary=summary, manifest=manifest, prev_tag=None,
                                 new_tag=None, readonly_report=readonly_report)
        steps = revert_plan(manifest)
        branch = f"{self.settings.revert_prefix}{manifest['new_tag']}"
        out = {"incident_id": inc["incident_id"], "branch": branch, "status": None, "pr": None, "reverted": []}
        if not steps:
            inc["revert"]["status"] = "not_needed"
            self._save(inc, "manifest rỗng: không có gì để revert")
            out["status"] = "not_needed"
            return out
        s = self.settings
        tracking = f"refs/remotes/{s.remote}/{s.base_branch}"
        self.killswitch.check("auto_revert.fetch")
        self.git.fetch(s.remote, f"+refs/heads/{s.base_branch}:{tracking}")
        base_sha = self.git.rev_parse(tracking)
        wt = self.settings.workdir.parent / f"rv-{manifest['new_tag'].replace('/', '_')}"
        if wt.exists():
            self.git.worktree_remove(wt)
        self.git.delete_local_branch(branch)
        self.git.worktree_add(wt, base_sha, branch=branch)
        inc["revert"].update(status="in_progress", branch=branch)
        self._save(inc, f"bắt đầu revert {len(steps)} PR từ main {base_sha[:10]}, thứ tự ngược manifest")
        try:
            for st in steps:
                self.killswitch.check("auto_revert.revert")
                proc = self.git.run(*st["argv"][1:], cwd=wt, ok=(0, 1, 128))
                if proc.returncode != 0:
                    files = self.git.conflicted_files(wt)
                    self.git.run("revert", "--abort", cwd=wt, ok=(0, 128))
                    inc["status"] = "frozen"
                    inc["revert"].update(status="conflict" if files else "in_progress", reverted=out["reverted"],
                                         conflict={"sha": st["sha"], "files": files} if files else None)
                    why = (f"git revert {st['sha'][:10]} (PR #{st['pr']}) conflict: {files}" if files else
                           f"git revert {st['sha'][:10]} lỗi: {proc.stderr.decode('utf-8', 'replace')[:300]}")
                    self._save(inc, f"{why}. Đã --abort. ĐÓNG BĂNG, chờ người dùng; không tự giải conflict")
                    record(self.audit, "auto_revert.conflict", "frozen", sha=st["sha"], pr=st["pr"],
                           detail={"incident_id": inc["incident_id"], "files": files})
                    self.notifier.notify(kind="escalated", task_id=None,
                                         message=f"{inc['incident_id']}: revert tự động DỪNG — {why}. "
                                                 "Bật kill-switch; xử lý theo docs/runbooks/revert.md.",
                                         detail={"incident_id": inc["incident_id"], "files": files})
                    out.update(status="conflict", conflict={"sha": st["sha"], "files": files})
                    return out
                out["reverted"].append(st["sha"])
            self.killswitch.check("auto_revert.push")
            self.git.push(s.remote, f"HEAD:refs/heads/{branch}", token=self.token, cwd=wt)
            self.killswitch.check("auto_revert.open_pr")
            pr = self.writer.open_pull(
                head=branch, draft=False,
                title=f"Revert {manifest['new_tag']} (tự động, R10) — CẦN NGƯỜI DÙNG DUYỆT",
                body=(f"Incident {inc['incident_id']}: {summary}\n\nRevert {len(steps)} PR theo thứ tự ngược "
                      f"manifest `{manifest['manifest_id']}` (`git log {manifest['prev_tag']}..{manifest['new_tag']}`)."
                      "\n\nRunner KHÔNG merge PR này. Chỉ người dùng duyệt và merge (R7)."))
            inc["revert"].update(status="pr_opened", pr=pr, reverted=out["reverted"], conflict=None)
            self._save(inc, f"đã mở PR revert #{pr}; chờ người dùng duyệt và merge")
            record(self.audit, "auto_revert.pr_opened", "ok", pr=pr,
                   detail={"incident_id": inc["incident_id"], "branch": branch, "reverted": out["reverted"]})
            self.notifier.notify(kind="escalated", task_id=None,
                                 message=f"{inc['incident_id']}: release {manifest['new_tag']} không đạt kiểm tra; "
                                         f"đã mở PR revert #{pr}. Bật kill-switch, duyệt và merge PR revert, "
                                         "rồi release lại theo docs/runbooks/batch.md.",
                                 detail={"incident_id": inc["incident_id"], "pr": pr})
            out.update(status="pr_opened", pr=pr)
            return out
        finally:
            self.git.worktree_remove(wt)
            self.git.delete_local_branch(branch)
