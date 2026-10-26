"""Digest 11:30 / 16:30: ĐÓNG BĂNG danh sách PR của cửa sổ batch (PR-4b, SPEC mục 3 bước 9).

- Lúc digest (giờ KST, mặc định tạm), runner đọc PR đang mở vào ``main`` từ GitHub (chỉ đọc),
  fetch head của từng PR vào clone làm việc của batch và ghi **một lần**
  ``<state_dir>/digests/<window_id>.json`` (``schemas/digest.schema.json``). File đã có thì không
  bao giờ ghi lại: PR mở hay đổi head sau giờ digest thuộc cửa sổ sau.
- Mỗi mục đóng băng ``(head SHA, merge-base, patch-id)``, nhãn (labeller, config đọc từ base),
  trạng thái CI trên head (check run GitHub Actions). ``batch_eligible`` = CI xanh, không
  Owner-only, có patch-id. Owner-only KHÔNG BAO GIỜ vào batch: người dùng merge tay qua GitHub UI.
- Digest KHÔNG phải bản ghi duyệt. Batch còn cần review ``APPROVED`` của người dùng trên GitHub,
  đúng head đã đóng băng, nộp trước hạn duyệt (``release.approval_binding``).
- Kill-switch bật → dừng trước mọi thao tác (đọc GitHub, fetch, ghi file).
- Thông báo cho người dùng qua Muse (``notifications/``): danh sách + hạn duyệt.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Callable

from orchestrator.fsio import RetryPolicy, WriterGuard, atomic_write_bytes
from pipeline import common
from pipeline.ci_status import fetch_ci_status
from pipeline.github_api import GitHubClient
from pipeline.labeler import OWNER_ONLY, label_pr

from .batch_config import BatchSettings, record
from .gitops import Git
from .schedule import Job

DIGEST_DIR = "digests"
DIGEST_VERSION = 1


class DigestError(RuntimeError):
    pass


def digest_path(state_dir: Path, window_id: str) -> Path:
    return Path(state_dir) / DIGEST_DIR / f"{window_id}.json"


def read_digest(state_dir: Path, window_id: str) -> dict | None:
    """Digest đã đóng băng của cửa sổ; ``None`` nếu chưa có. File hỏng/sai schema → ``DigestError``."""
    p = digest_path(state_dir, window_id)
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        common.validate("digest", d)
    except Exception as exc:  # noqa: BLE001
        raise DigestError(f"digest {p.name} hỏng: {type(exc).__name__}: {exc}"[:300]) from exc
    if d["window_id"] != window_id:
        raise DigestError(f"digest {p.name} mang window_id {d['window_id']!r}")
    return d


def pr_ref(n: int) -> str:
    return f"refs/harness/pr/{int(n)}"


def render(digest: dict) -> str:
    lines = [f"Digest cửa sổ {digest['window_id']} (đóng băng {digest['frozen_at']}).",
             f"Hạn duyệt trên GitHub: {digest['cutoff']}. Batch: {digest['batch_at']}.",
             "Batch CHỈ merge PR có review APPROVED của bạn trên đúng head SHA dưới đây; "
             "Owner-only bạn merge tay qua GitHub UI.", ""]
    for e in digest["entries"]:
        flag = "✓ vào batch nếu bạn duyệt" if e["batch_eligible"] else "✗ " + "; ".join(e["reasons"])
        lines.append(f"- #{e['pr']} {e['title']} [{e['tier']}, CI {e['ci']}] head {e['head_sha'][:10]} — {flag}")
    if not digest["entries"]:
        lines.append("- (không có PR nào)")
    for x in digest["excluded"]:
        lines.append(f"- #{x['pr']} bỏ qua: {x['reason']}")
    return "\n".join(lines)


class DigestBuilder:
    def __init__(self, *, state_dir: Path, guard: WriterGuard, audit, killswitch, client: GitHubClient, git: Git,
                 settings: BatchSettings, notifier, clock: Callable[[], datetime], retry: RetryPolicy | None = None):
        if killswitch is None or not callable(getattr(killswitch, "check", None)):
            raise TypeError("DigestBuilder cần kill-switch (F1, fail-closed)")
        guard.ensure_held()
        self.state_dir = Path(state_dir)
        self.guard = guard
        self.audit = audit
        self.killswitch = killswitch
        self.client = client
        self.git = git
        self.settings = settings
        self.notifier = notifier
        self.clock = clock
        self.retry = retry or RetryPolicy()

    def build(self, job: Job) -> dict:
        self.killswitch.check("digest.build")
        existing = read_digest(self.state_dir, job.window_id)
        if existing is not None:
            return {"status": "already_frozen", "window_id": job.window_id, "entries": len(existing["entries"])}
        s = self.settings
        tracking = f"refs/remotes/{s.remote}/{s.base_branch}"
        self.git.fetch(s.remote, f"+refs/heads/{s.base_branch}:{tracking}")
        base_sha = self.git.rev_parse(tracking)
        pulls = self.client.paginate("pulls", {"state": "open", "base": s.base_branch,
                                               "sort": "created", "direction": "asc"})
        entries, excluded = [], []
        ready = []
        for p in sorted(pulls, key=lambda x: int(x.get("number") or 0)):
            if p.get("draft"):
                excluded.append({"pr": int(p["number"]), "reason": "draft"})
            else:
                ready.append(p)
        if ready:
            self.killswitch.check("digest.fetch_prs")
            self.git.fetch(s.remote, *(f"+refs/pull/{int(p['number'])}/head:{pr_ref(p['number'])}" for p in ready))
        for p in ready:
            n = int(p["number"])
            head = ((p.get("head") or {}).get("sha")) or ""
            if self.git.rev_parse(pr_ref(n)) != head:
                excluded.append({"pr": n, "reason": "head đổi trong lúc đóng băng: vào cửa sổ sau"})
                continue
            entries.append(self._entry(p, n, head, base_sha))
        digest = {
            "v": DIGEST_VERSION, "window_id": job.window_id, "frozen_at": self.clock().isoformat(),
            "cutoff": job.cutoff.isoformat(), "batch_at": job.batch_at.isoformat(), "repo": self.client.repo,
            "base_branch": s.base_branch, "base_sha": base_sha, "entries": entries, "excluded": excluded,
        }
        common.validate("digest", digest)
        self.killswitch.check("digest.freeze")
        self.guard.ensure_held()
        path = digest_path(self.state_dir, job.window_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():  # một writer (WriterGuard), nhưng vẫn không bao giờ ghi đè danh sách đã đóng băng
            raise DigestError(f"{path.name} đã tồn tại: danh sách đã đóng băng")
        atomic_write_bytes(path, json.dumps(digest, ensure_ascii=False, indent=2).encode("utf-8"), self.retry)
        eligible = [e["pr"] for e in entries if e["batch_eligible"]]
        record(self.audit, "digest.frozen", "ok", sha=base_sha,
               detail={"window_id": job.window_id, "prs": [e["pr"] for e in entries], "eligible": eligible},
               dedup_key=f"digest:{job.window_id}")
        self.notifier.notify(kind="info", task_id=None, message=render(digest),
                             detail={"window_id": job.window_id, "eligible": eligible, "cutoff": digest["cutoff"]})
        return {"status": "frozen", "window_id": job.window_id, "entries": len(entries), "eligible": eligible}

    def _entry(self, p: dict, n: int, head: str, base_sha: str) -> dict:
        s = self.settings
        mb = self.git.merge_base(base_sha, head)
        pid = self.git.patch_id(mb, head)
        label = label_pr(self.git.repo, mb, head)
        ci = fetch_ci_status(self.client, head, check_name=s.check_name, app_slug=s.app_slug)
        reasons = []
        if label.tier == OWNER_ONLY:
            reasons.append("Owner-only: người dùng merge tay qua GitHub UI, không vào batch")
        if ci.state != "success":
            reasons.append(f"CI {ci.state} trên head")
        if pid is None:
            reasons.append("diff rỗng so với merge-base")
        return {
            "pr": n, "title": str(p.get("title") or "")[:300], "author": ((p.get("user") or {}).get("login")),
            "html_url": p.get("html_url"), "head_sha": head, "merge_base": mb, "patch_id": pid,
            "tier": label.tier, "label_id": label.label_id, "ci": ci.state,
            "batch_eligible": not reasons, "reasons": reasons,
        }
