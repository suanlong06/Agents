"""Phản chiếu GitHub review của người dùng vào ``approvals.jsonl`` (PR-3, R7, R13, R15).

- **GitHub là nguồn sự thật.** Bản ghi duyệt là GitHub review của người dùng (login
  trong ``github.owner_logins``, khớp CODEOWNERS), gắn với ``commit_id``. Quyết định
  "đã duyệt chưa" (``github_approval``) luôn đọc lại từ GitHub, KHÔNG BAO GIỜ đọc
  ``approvals.jsonl``: bản phản chiếu chỉ để dashboard xem, sửa nó không duyệt được gì.
- ``approvals.jsonl`` nằm trong ``state_dir`` (ngoài repo), append-only, chỉ orchestrator
  (giữ ``WriterGuard``) ghi (R3). Key idempotent ``(pr, review_id, state)``: đồng bộ lại
  không ghi trùng; review bị dismiss thành một bản ghi mới với ``state`` = ``DISMISSED``.
- Chỉ ĐỌC GitHub; không approve, không merge (R7). Best-effort, không chống giả mạo (R15).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Mapping

from orchestrator.fsio import WriterGuard, append_line, read_jsonl, terminate_torn_tail

from . import common
from .github_api import SHA_RE, GitHubClient

APPROVAL_VERSION = 1
MIRRORED_STATES = ("APPROVED", "CHANGES_REQUESTED", "COMMENTED", "DISMISSED")
# Trạng thái quyết định duyệt của một người (COMMENTED/PENDING không đổi quyết định).
DECISIVE_STATES = ("APPROVED", "CHANGES_REQUESTED", "DISMISSED")


@dataclass(frozen=True)
class ApprovalDecision:
    approved: bool
    sha: str
    reason: str
    review_id: int | None = None
    reviewer: str | None = None


def _owners(owner_logins: Iterable[str]) -> frozenset[str]:
    owners = frozenset(o.casefold() for o in owner_logins if o)
    if not owners:
        raise ValueError("github.owner_logins rỗng: không biết review của ai là bản ghi duyệt")
    return owners


def _login(review: Mapping) -> str | None:
    user = review.get("user") or {}
    login = user.get("login") if isinstance(user, Mapping) else None
    return login if isinstance(login, str) and login else None


def decide_approval(reviews: Iterable[Mapping], sha: str, owner_logins: Iterable[str]) -> ApprovalDecision:
    """Quyết định từ danh sách review LẤY TỪ GITHUB: review quyết định mới nhất của người
    dùng phải là ``APPROVED`` và gắn đúng ``sha``."""
    owners = _owners(owner_logins)
    if not SHA_RE.match(sha or ""):
        return ApprovalDecision(False, sha, "SHA không hợp lệ")
    mine = [r for r in reviews
            if (_login(r) or "").casefold() in owners and r.get("state") in DECISIVE_STATES]
    if not mine:
        return ApprovalDecision(False, sha, "người dùng chưa có review duyệt nào trên GitHub")
    last = max(mine, key=lambda r: (r.get("submitted_at") or "", int(r.get("id") or 0)))
    rid, who = last.get("id"), _login(last)
    if last.get("state") != "APPROVED":
        return ApprovalDecision(False, sha, f"review mới nhất là {last.get('state')}", rid, who)
    if last.get("commit_id") != sha:
        return ApprovalDecision(False, sha, f"duyệt SHA {last.get('commit_id')}, không phải {sha}: phải duyệt lại",
                                rid, who)
    return ApprovalDecision(True, sha, "người dùng đã duyệt đúng SHA trên GitHub", rid, who)


def github_approval(client: GitHubClient, pr: int, sha: str, owner_logins: Iterable[str]) -> ApprovalDecision:
    """Đọc PR và review từ GitHub (nguồn sự thật). Head PR khác ``sha`` → chưa duyệt."""
    head = ((client.get(f"pulls/{int(pr)}") or {}).get("head") or {}).get("sha")
    if head != sha:
        return ApprovalDecision(False, sha, f"head của PR là {head}, không phải {sha}")
    return decide_approval(client.paginate(f"pulls/{int(pr)}/reviews"), sha, owner_logins)


@dataclass
class SyncResult:
    recorded: list[dict] = field(default_factory=list)
    duplicates: int = 0
    skipped: int = 0


class ApprovalMirror:
    FILE = "approvals.jsonl"

    def __init__(self, state_dir: Path, guard: WriterGuard, client: GitHubClient, *,
                 owner_logins: Iterable[str], clock: Callable[[], str], killswitch):
        guard.ensure_held()
        self.path = Path(state_dir) / self.FILE
        self.guard = guard
        self.client = client
        self.owners = _owners(owner_logins)
        self._clock = clock
        # PR-4a: kill-switch bật → không đọc GitHub, không ghi bản phản chiếu.
        # PR-4b (F1): BẮT BUỘC, không có mặc định "không kiểm" (fail-closed).
        if killswitch is None or not callable(getattr(killswitch, "check", None)):
            raise TypeError("ApprovalMirror cần kill-switch có check(action) (F1, fail-closed)")
        self.killswitch = killswitch
        terminate_torn_tail(self.path)
        self._keys = {self.key(r) for r in self.read(self.path)}

    @staticmethod
    def key(rec: Mapping) -> tuple[int, int, str]:
        return (rec["pr"], rec["review_id"], rec["state"])

    @staticmethod
    def read(path: Path) -> list[dict]:
        """Bản ghi phản chiếu hợp lệ (CHỈ để xem; không dùng để quyết định duyệt)."""
        out = []
        for _off, obj, _raw in read_jsonl(Path(path)):
            if obj is None:
                continue
            try:
                common.validate("approval", obj)
            except common.PipelineSchemaError:
                continue
            out.append(obj)
        return out

    def _record_for(self, pr: int, review: Mapping) -> dict | None:
        login = _login(review)
        if login is None or login.casefold() not in self.owners:
            return None
        if review.get("state") not in MIRRORED_STATES or not SHA_RE.match(review.get("commit_id") or ""):
            return None
        rid = review.get("id")
        if isinstance(rid, bool) or not isinstance(rid, int) or rid < 1:
            return None
        return {
            "v": APPROVAL_VERSION, "mirrored_at": self._clock(), "source": "github", "repo": self.client.repo,
            "pr": int(pr), "review_id": rid, "reviewer": login, "state": review["state"],
            "commit_id": review["commit_id"], "submitted_at": review.get("submitted_at"),
            "html_url": review.get("html_url"),
        }

    def sync(self, pr: int) -> SyncResult:
        """Đọc review của ``pr`` từ GitHub, append review mới của người dùng."""
        self.guard.ensure_held()
        self.killswitch.check("approvals.sync")
        result = SyncResult()
        for review in self.client.paginate(f"pulls/{int(pr)}/reviews"):
            rec = self._record_for(pr, review)
            if rec is None:
                result.skipped += 1
                continue
            if self.key(rec) in self._keys:
                result.duplicates += 1
                continue
            common.validate("approval", rec)
            append_line(self.path, rec)
            self._keys.add(self.key(rec))
            result.recorded.append(rec)
        return result
