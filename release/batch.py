"""Batch release 12:00 / 17:00 (PR-4b, SPEC mục 3 bước 10–12, R7, R9, R10).

KHÔNG CÓ AUTO-MERGE. Batch chỉ merge PR mà NGƯỜI DÙNG đã duyệt trên GitHub, gắn
``(head SHA, patch-id)``. PR chưa được duyệt (kể cả khi shadow evaluator ghi "would-merge yes",
CI xanh, Gate 3 pass) không bao giờ được merge: SPEC mục 8 chưa chốt và chưa có dữ liệu shadow.

Trình tự (giữ ``release.lock`` từ đầu tới hết bước kiểm tra và revert, R10):

1. Kill-switch tắt; không có incident ``open``/``frozen``; digest của cửa sổ đã đóng băng.
2. **Danh tính merge**: ``batch.merge_identities`` khác rỗng, login của token merge
   (``GET /user``) nằm trong allowlist. Không thì dừng, không merge gì.
3. Với từng PR trong digest (``batch_eligible``, tối đa ``max_prs``, còn lại ``deferred``): PR còn
   mở, base ``main``, head == head đã đóng băng, tác giả ≠ danh tính merge, CI xanh trên head,
   patch-id tính lại == patch-id đã đóng băng, review ``APPROVED`` mới nhất của người dùng trên
   đúng head, nộp trước hạn duyệt → ``ApprovalBinding``. Thiếu bất kỳ điều gì → bỏ qua + lý do.
4. **Tích hợp tuần tự, không dùng merge queue native**: nhánh ``harness/batch/<window>-<n>`` từ
   ``main`` hiện tại; merge từng PR theo thứ tự (``POST merges`` phía GitHub). Mỗi bước phải: không
   conflict, có parent ``[bước trước, head đã duyệt]``, patch-id của bước == patch-id đã duyệt
   (khác thì kèm range-diff), nhãn tại SHA merge không phải Owner-only (R8). PR rớt → dựng lại
   nhánh tích hợp KHÔNG có nó.
5. **CI trên kết quả tích hợp**: mở PR draft từ nhánh tích hợp (``ci.yml`` chạy trên
   ``pull_request``), chờ check run ``ci`` của GitHub Actions trên SHA cuối. Không xanh / quá hạn
   → không merge gì. PR tích hợp KHÔNG BAO GIỜ được merge; nó bị đóng sau đó.
6. **Merge tuần tự** vào ``main`` (``GitHubWriter.merge_pull`` với binding, ``sha`` = head đã
   duyệt): trước mỗi PR đọc lại phê duyệt trên GitHub và kiểm ``main`` chưa bị ai đổi; sau mỗi PR
   kiểm cây của commit merge == cây của bước tích hợp đã qua CI. Lệch → dừng, incident ``frozen``.
7. Tag ``rel-<window>`` trên ``main``; manifest từ ``git log prevTag..newTag`` (``release.manifest``);
   kiểm tra read-only của CHÍNH tag (``release.release_check``, F2) trong lúc vẫn giữ ``release.lock``.
8. Không đạt → revert tự động theo R10 (``release.auto_revert``): incident + PR revert chờ người
   dùng duyệt. Runner không merge PR revert.

Kill-switch được kiểm trước MỖI thao tác tự động (đọc/ghi GitHub, git, merge, tag, kiểm tra,
revert); bật giữa chừng → dừng ngay. Báo cáo ``<state_dir>/batches/<window_id>.json``
(``schemas/batch.schema.json``), audit agent ``batch``, thông báo qua Muse.
Best-effort (R15): runner chạy dưới OS user hiện tại; xem ``docs/runbooks/batch.md``.
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable

from orchestrator.fsio import RetryPolicy, WriterGuard, atomic_write_bytes
from orchestrator.killswitch import KillSwitchEngaged
from orchestrator.lock import LockHeld, PidLock
from pipeline import common
from pipeline.ci_status import FAILURE, SUCCESS, fetch_ci_status
from pipeline.github_api import GitHubClient
from pipeline.labeler import OWNER_ONLY, label_at_merge

from .approval_binding import ApprovalBinding, decide_binding, recheck_binding
from .batch_config import BatchSettings, record
from .digest import DigestError, pr_ref, read_digest
from .github_write import GitHubWriter, IntegrationConflict
from .gitops import Git
from .incidents import active_incidents
from .manifest import ManifestError, compute_manifest
from .readonly_check import BATCH_LOCK_PURPOSE, RELEASE_LOCK_FILE, current_audit_seq
from .schedule import Job

BATCH_DIR = "batches"
RELEASES_DIR = "releases"
REPORT_VERSION = 1


class BatchAbort(RuntimeError):
    """Batch phải dừng (kèm lý do cho báo cáo)."""


def report_path(state_dir: Path, window_id: str) -> Path:
    return Path(state_dir) / BATCH_DIR / f"{window_id}.json"


class BatchRelease:
    def __init__(self, *, state_dir: Path, guard: WriterGuard, audit, killswitch, client: GitHubClient,
                 writer: GitHubWriter | None, git: Git, settings: BatchSettings, owner_logins: Iterable[str],
                 notifier, release_check: Callable[..., dict], reverter, clock: Callable[[], datetime],
                 sleep: Callable[[float], None] = time.sleep, monotonic: Callable[[], float] = time.monotonic,
                 retry: RetryPolicy | None = None):
        if killswitch is None or not callable(getattr(killswitch, "check", None)):
            raise TypeError("BatchRelease cần kill-switch (F1, fail-closed)")
        guard.ensure_held()
        self.state_dir = Path(state_dir)
        self.guard = guard
        self.audit = audit
        self.killswitch = killswitch
        self.client = client
        self.writer = writer
        self.git = git
        self.settings = settings
        self.owners = tuple(owner_logins)
        if not self.owners:
            raise ValueError("github.owner_logins rỗng: không biết review của ai là phê duyệt")
        self.notifier = notifier
        self.release_check = release_check
        self.reverter = reverter
        self.clock = clock
        self.sleep = sleep
        self.monotonic = monotonic
        self.retry = retry or RetryPolicy()

    # --- tiện ích -------------------------------------------------------------------------

    def _ks(self, action: str) -> None:
        self.killswitch.check(f"batch.{action}")

    @property
    def _tracking(self) -> str:
        return f"refs/remotes/{self.settings.remote}/{self.settings.base_branch}"

    def _fetch_main(self) -> str:
        self.git.fetch(self.settings.remote, f"+refs/heads/{self.settings.base_branch}:{self._tracking}")
        return self.git.rev_parse(self._tracking)

    def _main_head(self) -> str:
        ref = self.client.get(f"git/ref/heads/{self.settings.base_branch}") or {}
        sha = (ref.get("object") or {}).get("sha")
        if not isinstance(sha, str) or len(sha) != 40:
            raise BatchAbort(f"không đọc được head của {self.settings.base_branch}")
        return sha

    def _skip(self, report: dict, pr: int, reason: str, **extra) -> None:
        report["skipped"].append({"pr": pr, "reason": reason, **extra})

    def _save(self, report: dict) -> None:
        common.validate("batch", report)
        p = report_path(self.state_dir, report["window_id"])
        p.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(p, json.dumps(report, ensure_ascii=False, indent=2).encode("utf-8"), self.retry)

    # --- chạy ------------------------------------------------------------------------------

    def run(self, job: Job) -> dict:
        self._ks("start")
        report = {
            "v": REPORT_VERSION, "window_id": job.window_id, "started_at": self.clock().isoformat(),
            "finished_at": None, "status": "running", "reason": None, "auto_merge": "disabled",
            "merge_identity": None, "candidates": [], "skipped": [], "deferred": [], "integration": None,
            "merged": [], "tag": None, "prev_tag": None, "manifest_id": None, "release_check": None,
            "revert": None, "incident_id": None,
        }
        lock = PidLock(self.state_dir / RELEASE_LOCK_FILE,
                       meta={"purpose": BATCH_LOCK_PURPOSE, "window_id": job.window_id})
        try:
            lock.acquire()
        except LockHeld as exc:
            report.update(status="skipped", reason=f"release.lock đang bị giữ: {exc.why}")
            return self._finish(report, notify="info")
        try:
            self._run_locked(job, report)
        except KillSwitchEngaged as exc:
            report.update(status="stopped_killswitch", reason=str(exc))
            if report["merged"]:
                self._freeze(report, "kill-switch bật giữa batch, sau khi đã merge một phần")
        except Exception as exc:  # noqa: BLE001 — mọi lỗi khác: dừng, báo, đóng băng nếu đã merge
            report.update(status="failed", reason=f"{type(exc).__name__}: {exc}"[:1000])
            self._cleanup_after_error(report)
            if report["merged"]:
                self._freeze(report, f"batch lỗi sau khi đã merge một phần: {report['reason']}")
        finally:
            lock.release()
        kind = "info" if report["status"] in ("released", "nothing_to_release", "skipped") else "escalated"
        return self._finish(report, notify=kind)

    def _finish(self, report: dict, *, notify: str) -> dict:
        pending = report.pop("_pending_branch", None)
        if pending:  # kill-switch bật lúc nhánh tích hợp còn đó: không dọn, chỉ ghi lại
            report.setdefault("cleanup_errors", []).append(f"nhánh {pending} còn trên GitHub (chưa dọn)")
        report["finished_at"] = self.clock().isoformat()
        self._save(report)
        record(self.audit, "batch.finished", report["status"], pr=None,
               detail={"window_id": report["window_id"], "reason": report["reason"],
                       "merged": [m["pr"] for m in report["merged"]], "tag": report["tag"]},
               dedup_key=f"batch:{report['window_id']}:{report['status']}")
        merged = ", ".join(f"#{m['pr']}" for m in report["merged"]) or "không PR nào"
        skipped = "; ".join(f"#{s['pr']}: {s['reason']}" for s in report["skipped"][:10])
        self.notifier.notify(kind=notify, task_id=None,
                             message=f"Batch {report['window_id']}: {report['status']}. Đã merge: {merged}."
                                     + (f" Lý do: {report['reason']}." if report["reason"] else "")
                                     + (f" Bỏ qua: {skipped}." if skipped else ""),
                             detail={"window_id": report["window_id"], "status": report["status"],
                                     "tag": report["tag"], "incident_id": report["incident_id"]})
        return report

    def _cleanup_after_error(self, report: dict) -> None:
        """Lỗi thường (không phải kill-switch): dọn nhánh/PR tích hợp còn mở, best-effort.
        Kill-switch bật thì KHÔNG dọn (dọn cũng là thao tác ghi); báo cáo ghi lại nhánh còn đó."""
        branch = (report.get("integration") or {}).get("branch") or report.pop("_pending_branch", None)
        report.pop("_pending_branch", None)
        if not branch or self.writer is None or self.killswitch.status().engaged:
            return
        if report.get("integration"):
            self._cleanup_integration(branch, report)
        else:
            self._delete_branch(branch, report)

    def _freeze(self, report: dict, summary: str) -> None:
        if report.get("incident_id"):
            return
        try:
            inc = self.reverter.open_incident(trigger="other", summary=summary, manifest=None,
                                              prev_tag=report.get("prev_tag"), new_tag=report.get("tag"),
                                              readonly_report=None, frozen=True)
            report["incident_id"] = inc["incident_id"]
        except Exception as exc:  # noqa: BLE001 — vẫn báo động qua thông báo
            report["reason"] = f"{report['reason']} (không mở được incident: {exc})"

    def _run_locked(self, job: Job, report: dict) -> None:
        s = self.settings
        active = active_incidents(self.state_dir)
        if active:
            report.update(status="skipped", reason=f"có incident đang mở/đóng băng: {active[:3]}")
            return
        try:
            digest = read_digest(self.state_dir, job.window_id)
        except DigestError as exc:
            report.update(status="skipped", reason=str(exc))
            return
        if digest is None:
            report.update(status="skipped", reason="không có digest đóng băng của cửa sổ (lỡ giờ digest?)")
            return
        if not s.merge_identities:
            report.update(status="skipped", reason="batch.merge_identities rỗng: chưa có danh tính merge nào")
            return
        if self.writer is None:
            report.update(status="skipped", reason=f"thiếu token danh tính merge ({s.merge_token_env})")
            return
        self._ks("whoami")
        login = self.writer.whoami()
        report["merge_identity"] = login
        if not s.merge_allowed(login):
            report.update(status="skipped", reason=f"danh tính {login!r} không có trong batch.merge_identities")
            return

        self._ks("fetch")
        self._fetch_main()
        bindings = self._select(job, digest, login, report)
        if not bindings:
            report.update(status="nothing_to_release", reason="không PR nào có phê duyệt hợp lệ của người dùng")
            return

        steps, base_sha, branch = self._integrate(job, bindings, report)
        if not steps:
            report.update(status="nothing_to_release", reason="mọi PR rớt khỏi tích hợp")
            return
        if not self._integration_ci(job, steps, branch, report):
            report.update(status="integration_ci_failed",
                          reason=f"CI trên kết quả tích hợp không xanh: {report['integration']['ci']}")
            return

        head = self._merge_all(job, steps, base_sha, report)
        self._cleanup_integration(branch, report)
        if not report["merged"]:
            report["status"] = "aborted_before_merge"
            return
        if len(report["merged"]) != len(steps):
            report["status"] = "partial"
            self._freeze(report, f"batch {job.window_id} dừng sau khi merge {len(report['merged'])}/{len(steps)} PR:"
                                 f" {report['reason']}")
            return
        self._release(job, head, report)

    # --- 3. chọn PR -----------------------------------------------------------------------

    def _select(self, job: Job, digest: dict, login: str, report: dict) -> list[ApprovalBinding]:
        bindings: list[ApprovalBinding] = []
        eligible = [e["pr"] for e in digest["entries"] if e["batch_eligible"]]
        if eligible:  # một lần fetch cho mọi head (refs/pull/N/head là của GitHub, có cả PR từ fork)
            self.git.fetch(self.settings.remote, *(f"+refs/pull/{n}/head:{pr_ref(n)}" for n in eligible))
        for e in digest["entries"]:
            n = e["pr"]
            if not e["batch_eligible"]:
                self._skip(report, n, "không đủ điều kiện lúc digest: " + "; ".join(e["reasons"]))
                continue
            if len(bindings) >= self.settings.max_prs:
                report["deferred"].append(n)
                continue
            b, why = self._evaluate(e, login, job.cutoff)
            if b is None:
                self._skip(report, n, why)
                continue
            bindings.append(b)
            report["candidates"].append(b.to_dict())
            record(self.audit, "batch.approval_bound", "ok", sha=b.head_sha, pr=n,
                   detail={"window_id": job.window_id, **b.to_dict()})
        return bindings

    def _evaluate(self, e: dict, login: str, cutoff: datetime) -> tuple[ApprovalBinding | None, str]:
        s, n = self.settings, e["pr"]
        self._ks("evaluate")
        pr = self.client.get(f"pulls/{n}") or {}
        if pr.get("state") != "open" or pr.get("merged"):
            return None, f"PR không còn mở ({pr.get('state')})"
        if pr.get("draft"):
            return None, "PR là draft"
        if ((pr.get("base") or {}).get("ref")) != s.base_branch:
            return None, f"base không phải {s.base_branch}"
        head = (pr.get("head") or {}).get("sha")
        if head != e["head_sha"]:
            return None, f"head đổi sau digest ({e['head_sha'][:10]} → {str(head)[:10]}): duyệt lại, cửa sổ sau"
        author = ((pr.get("user") or {}).get("login")) or ""
        if author.casefold() == login.casefold():
            return None, f"danh tính merge {login!r} là tác giả PR: không ai merge PR của chính mình"
        ci = fetch_ci_status(self.client, head, check_name=s.check_name, app_slug=s.app_slug)
        if ci.state != SUCCESS:
            return None, f"CI {ci.state} trên head"
        if self.git.rev_parse(pr_ref(n)) != head:
            return None, "head fetch được khác head trên GitHub"
        pid = self.git.patch_id(self.git.merge_base(self.git.rev_parse(self._tracking), head), head)
        if pid != e["patch_id"]:
            return None, f"patch-id đổi so với lúc đóng băng ({e['patch_id']} → {pid})"
        reviews = self.client.paginate(f"pulls/{n}/reviews")
        return decide_binding(pr, reviews, pr=n, head_sha=head, patch_id=pid, owner_logins=self.owners,
                              cutoff=cutoff)

    # --- 4. tích hợp tuần tự ---------------------------------------------------------------

    def _integrate(self, job: Job, bindings: list[ApprovalBinding], report: dict):
        s = self.settings
        remaining = list(bindings)
        for attempt in range(1, len(bindings) + 2):
            if not remaining:
                return [], None, None
            self._ks("integrate")
            base_sha = self._main_head()
            branch = f"{s.integration_prefix}{job.window_id}-{attempt}"
            self.writer.create_branch(branch, base_sha)
            report["_pending_branch"] = branch  # để dọn nếu lỗi trước khi nhánh vào report["integration"]
            merged, dropped = [], []
            for b in remaining:
                self._ks("integrate_pr")
                try:
                    sha = self.writer.merge_into_integration(
                        branch, b.head_sha, f"harness batch {job.window_id}: tích hợp PR #{b.pr}")
                except IntegrationConflict:
                    dropped.append((b, "conflict khi tích hợp vào batch: phê duyệt hết hiệu lực", None))
                    continue
                merged.append((b, sha))
            local = f"refs/harness/integration/{job.window_id}-{attempt}"
            self.git.fetch(s.remote, f"+refs/heads/{branch}:{local}")
            if merged and self.git.rev_parse(local) != merged[-1][1]:
                raise BatchAbort("nhánh tích hợp trên GitHub khác chuỗi merge vừa tạo")
            steps, prev = [], base_sha
            for b, sha in merged:
                why = self._check_step(prev, sha, b)
                if why is not None:
                    # Các bước sau dựng trên bước hỏng: kiểm lại ở lần dựng kế tiếp, không loại oan.
                    dropped.append((b, why[0], why[1]))
                    break
                steps.append({"pr": b.pr, "binding": b, "sha": sha, "parent": prev})
                prev = sha
            if not dropped:
                report["integration"] = {"branch": branch, "base_sha": base_sha, "head_sha": prev,
                                         "attempt": attempt, "pr": None, "ci": None,
                                         "steps": [{"pr": st["pr"], "sha": st["sha"]} for st in steps]}
                report.pop("_pending_branch", None)
                return steps, base_sha, branch
            for b, why, rdiff in dropped:
                self._skip(report, b.pr, why, **({"range_diff": rdiff} if rdiff else {}))
                record(self.audit, "batch.dropped", "skipped", sha=b.head_sha, pr=b.pr,
                       detail={"reason": why, "window_id": job.window_id})
            dropped_prs = {b.pr for b, _w, _r in dropped}
            remaining = [b for b in remaining if b.pr not in dropped_prs]
            report["candidates"] = [c for c in report["candidates"] if c["pr"] not in dropped_prs]
            self._delete_branch(branch, report)
            report.pop("_pending_branch", None)
        raise BatchAbort("không dựng được nhánh tích hợp ổn định")

    def _check_step(self, prev: str, sha: str, b: ApprovalBinding) -> tuple[str, str | None] | None:
        parents = self.git.parents(sha)
        if parents != [prev, b.head_sha]:
            return (f"bước tích hợp {sha[:10]} có parent {parents}, không phải [bước trước, head đã duyệt]", None)
        step_pid = self.git.patch_id(prev, sha)
        if step_pid != b.patch_id:
            mb = self.git.merge_base(prev, b.head_sha)
            rdiff = self.git.range_diff(mb, b.head_sha, prev, sha)
            return (f"patch-id khi tích hợp ({step_pid}) khác patch-id đã duyệt ({b.patch_id}): "
                    "phê duyệt hết hiệu lực, xem range-diff", rdiff)
        tier = label_at_merge(self.git.repo, sha).tier
        if tier == OWNER_ONLY:
            return ("nhãn tại SHA merge là Owner-only: người dùng merge tay qua GitHub UI", None)
        return None

    def _delete_branch(self, branch: str | None, report: dict) -> None:
        if not branch:
            return
        try:
            self.writer.delete_integration_branch(branch)
        except Exception as exc:  # noqa: BLE001 — dọn dẹp, không đổi kết quả batch
            report.setdefault("cleanup_errors", []).append(f"xóa {branch}: {exc}"[:300])

    # --- 5. CI trên kết quả tích hợp --------------------------------------------------------

    def _integration_ci(self, job: Job, steps: list[dict], branch: str, report: dict) -> bool:
        s, integ = self.settings, report["integration"]
        final = steps[-1]["sha"]
        self._ks("open_integration_pr")
        prs = ", ".join(f"#{st['pr']}" for st in steps)
        integ["pr"] = self.writer.open_pull(
            head=branch, draft=True,
            title=f"[harness batch {job.window_id}] CI trên kết quả tích hợp — KHÔNG MERGE PR NÀY",
            body=(f"PR draft chỉ để chạy CI (`{s.check_name}`) trên kết quả tích hợp {prs} theo thứ tự (R9). "
                  "Batch tự đóng PR này. Các PR gốc được merge riêng, mỗi PR với phê duyệt của người dùng."))
        deadline = self.monotonic() + s.ci_timeout_s
        while True:
            self._ks("wait_ci")
            ci = fetch_ci_status(self.client, final, check_name=s.check_name, app_slug=s.app_slug)
            integ["ci"] = ci.state
            if ci.state in (SUCCESS, FAILURE):
                break
            if self.monotonic() >= deadline:
                integ["ci"] = f"{ci.state} (quá {int(s.ci_timeout_s)}s)"
                break
            self.sleep(s.ci_poll_s)
        record(self.audit, "batch.integration_ci", ci.state, sha=final,
               detail={"window_id": job.window_id, "branch": branch, "pr": integ["pr"], "steps": prs})
        if integ["ci"] != SUCCESS:
            self._cleanup_integration(branch, report)
            return False
        return True

    def _cleanup_integration(self, branch: str, report: dict) -> None:
        integ = report.get("integration") or {}
        if integ.get("pr") and not integ.get("closed"):
            try:
                self.writer.close_pull(integ["pr"])
                integ["closed"] = True
            except Exception as exc:  # noqa: BLE001
                report.setdefault("cleanup_errors", []).append(f"đóng PR #{integ['pr']}: {exc}"[:300])
        if not integ.get("branch_deleted"):
            self._delete_branch(branch, report)
            integ["branch_deleted"] = True

    # --- 6. merge tuần tự --------------------------------------------------------------------

    def _merge_all(self, job: Job, steps: list[dict], base_sha: str, report: dict) -> str:
        expected = base_sha
        for st in steps:
            b: ApprovalBinding = st["binding"]
            self._ks("recheck_approval")
            ok, why = recheck_binding(self.client, b, owner_logins=self.owners, cutoff=job.cutoff)
            if not ok:
                report["reason"] = f"PR #{b.pr}: phê duyệt không còn hiệu lực ngay trước merge: {why}"
                return expected
            current = self._main_head()
            if current != expected:
                report["reason"] = (f"{self.settings.base_branch} đã đổi ngoài batch ({expected[:10]} → "
                                    f"{current[:10]}): dừng trước PR #{b.pr}")
                return expected
            self._ks("merge")
            merge_sha = self.writer.merge_pull(b, merge_method=self.settings.merge_method)
            report["merged"].append({"pr": b.pr, "merge_sha": merge_sha, "head_sha": b.head_sha,
                                     "patch_id": b.patch_id, "review_id": b.review_id, "reviewer": b.reviewer})
            record(self.audit, "batch.merged", "ok", sha=merge_sha, pr=b.pr,
                   detail={"head_sha": b.head_sha, "patch_id": b.patch_id, "review_id": b.review_id,
                           "reviewer": b.reviewer, "merge_identity": report["merge_identity"]})
            self._fetch_main()
            if self.git.parents(merge_sha) != [expected, b.head_sha] or \
                    self.git.tree_of(merge_sha) != self.git.tree_of(st["sha"]):
                raise BatchAbort(f"main sau merge PR #{b.pr} ({merge_sha[:10]}) khác bước tích hợp đã qua CI "
                                 f"({st['sha'][:10]})")
            expected = merge_sha
        return expected

    # --- 7–8. tag, manifest, kiểm tra, revert -------------------------------------------------

    def _prev_tag(self, tag: str, new_sha: str) -> str | None:
        proc = self.git.run("describe", "--tags", "--abbrev=0", "--first-parent",
                            "--match", f"{self.settings.tag_prefix}*", "--exclude", tag, new_sha, ok=(0, 128))
        out = proc.stdout.decode("utf-8", "replace").strip()
        return out or None

    def _release(self, job: Job, head: str, report: dict) -> None:
        s = self.settings
        tag = f"{s.tag_prefix}{job.window_id}"
        since_seq = current_audit_seq(self.state_dir)
        self._ks("tag")
        self.writer.create_tag(tag, head, f"release {tag} (batch {job.window_id}): "
                               + ", ".join(f"#{m['pr']}" for m in report["merged"]))
        report["tag"] = tag
        self.git.fetch(s.remote, f"+refs/tags/{tag}:refs/tags/{tag}", tags=True)  # cả tag release trước
        prev = self._prev_tag(tag, head)
        report["prev_tag"] = prev
        manifest = None
        if prev is not None:
            try:
                manifest = compute_manifest(self.git.repo, prev, tag)
            except ManifestError as exc:
                report["reason"] = f"không tính được manifest: {exc}"
        if manifest is not None:
            report["manifest_id"] = manifest["manifest_id"]
            mp = self.state_dir / RELEASES_DIR / f"{tag}.json"
            mp.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_bytes(mp, json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"), self.retry)
            in_manifest = sorted(e["pr"] for e in manifest["entries"] if e["pr"] is not None)
            merged = sorted(m["pr"] for m in report["merged"])
            if in_manifest != merged:
                report["manifest_note"] = (f"manifest (git log) có PR {in_manifest}, batch merge {merged}: "
                                           "có thay đổi ngoài batch trong release này")
        record(self.audit, "batch.tagged", "ok", sha=head,
               detail={"tag": tag, "prev_tag": prev, "manifest_id": report["manifest_id"]})

        self._ks("release_check")
        res = self.release_check(tag=tag, since_seq=since_seq)  # release.lock vẫn đang được giữ
        report["release_check"] = {"ok": bool(res.get("ok")), "exit_code": res.get("exit_code"),
                                   "error": res.get("error"), "report": res.get("report")}
        record(self.audit, "batch.release_check", "ok" if res.get("ok") else "failed", sha=head,
               detail={"tag": tag, "since_seq": since_seq})
        if res.get("ok"):
            report["status"] = "released"
            return
        report["status"] = "release_check_failed"
        summary = f"kiểm tra read-only sau release {tag} không đạt"
        if manifest is None:
            report["reason"] = (report["reason"] or "") + " không có manifest (không có tag release trước): " \
                                                         "không revert tự động được"
            self._freeze(report, summary + "; không có manifest để revert tự động")
            return
        try:
            rv = self.reverter.run(manifest=manifest, trigger="readonly_check_failed", summary=summary,
                                   readonly_report=res.get("report") if isinstance(res.get("report"), dict) else None)
        finally:  # revert dừng giữa chừng (kill-switch, lỗi git) vẫn giữ incident nó đã mở: không mở trùng
            report["incident_id"] = getattr(self.reverter, "last_incident_id", None) or report["incident_id"]
        report["revert"] = rv
