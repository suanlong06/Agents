"""Phê duyệt gắn ``(head SHA, patch-id)`` cho batch release (PR-4b, R7, R9).

- **Nguồn sự thật là GitHub review của người dùng** (``github.owner_logins``), đọc lại từ
  GitHub mỗi lần (``pipeline.approvals.decide_approval``), KHÔNG BAO GIỜ từ ``approvals.jsonl``.
- Một ``ApprovalBinding`` chỉ được tạo khi: head hiện tại của PR == ``head_sha`` đã đóng băng
  trong digest; review quyết định MỚI NHẤT của người dùng là ``APPROVED`` trên đúng
  ``head_sha``; review nộp **trước hạn duyệt** (cutoff, mặc định tạm 11:55 / 16:55); và
  ``patch_id`` là patch-id của thay đổi đã được đóng băng.
- Batch chỉ merge PR có binding, và binding chỉ còn hiệu lực khi thay đổi tích hợp vào batch có
  CÙNG patch-id (rebase/merge không conflict và không đổi nội dung; khác thì kèm range-diff).
- **Không có auto-merge**: không có đường nào tạo binding mà không có review ``APPROVED`` của
  người dùng. Shadow evaluator ("would-merge") không bao giờ tạo binding (SPEC mục 8 chưa chốt).
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Iterable, Mapping

from pipeline.approvals import decide_approval
from pipeline.github_api import SHA_RE, GitHubClient

PATCH_ID_RE = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True)
class ApprovalBinding:
    pr: int
    head_sha: str
    patch_id: str
    review_id: int
    reviewer: str
    submitted_at: str

    def __post_init__(self):
        if isinstance(self.pr, bool) or not isinstance(self.pr, int) or self.pr < 1:
            raise ValueError(f"pr không hợp lệ: {self.pr!r}")
        if not SHA_RE.match(self.head_sha or ""):
            raise ValueError(f"head_sha không hợp lệ: {self.head_sha!r}")
        if not PATCH_ID_RE.match(self.patch_id or ""):
            raise ValueError(f"patch_id không hợp lệ: {self.patch_id!r}")
        if isinstance(self.review_id, bool) or not isinstance(self.review_id, int) or self.review_id < 1:
            raise ValueError(f"review_id không hợp lệ: {self.review_id!r}")
        if not self.reviewer:
            raise ValueError("thiếu reviewer")

    def to_dict(self) -> dict:
        return asdict(self)


def _parse_ts(raw: object) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else None


def decide_binding(pr_obj: Mapping, reviews: Iterable[Mapping], *, pr: int, head_sha: str, patch_id: str | None,
                   owner_logins: Iterable[str], cutoff: datetime) -> tuple[ApprovalBinding | None, str]:
    """Quyết định thuần từ JSON GitHub (PR + reviews). ``None`` kèm lý do khi không có binding."""
    if cutoff.tzinfo is None:
        raise ValueError("cutoff phải có múi giờ")
    if not patch_id or not PATCH_ID_RE.match(patch_id):
        return None, "không có patch-id của thay đổi (diff rỗng?)"
    head = ((pr_obj or {}).get("head") or {}).get("sha")
    if head != head_sha:
        return None, f"head của PR là {head}, không phải {head_sha} đã đóng băng: phải duyệt lại ở cửa sổ sau"
    reviews = list(reviews)
    decision = decide_approval(reviews, head_sha, owner_logins)
    if not decision.approved:
        return None, decision.reason
    review = next((r for r in reviews if r.get("id") == decision.review_id), None)
    submitted = _parse_ts((review or {}).get("submitted_at"))
    if submitted is None:
        return None, "review duyệt không có submitted_at hợp lệ"
    if submitted > cutoff:
        return None, f"duyệt lúc {submitted.isoformat()} sau hạn {cutoff.isoformat()}: vào cửa sổ sau"
    return ApprovalBinding(pr=int(pr), head_sha=head_sha, patch_id=patch_id, review_id=int(decision.review_id),
                           reviewer=str(decision.reviewer), submitted_at=submitted.isoformat()), decision.reason


def bind_approval(client: GitHubClient, *, pr: int, head_sha: str, patch_id: str | None,
                  owner_logins: Iterable[str], cutoff: datetime) -> tuple[ApprovalBinding | None, str]:
    """Đọc PR và review từ GitHub (nguồn sự thật) rồi ``decide_binding``."""
    pr_obj = client.get(f"pulls/{int(pr)}") or {}
    reviews = client.paginate(f"pulls/{int(pr)}/reviews")
    return decide_binding(pr_obj, reviews, pr=pr, head_sha=head_sha, patch_id=patch_id,
                          owner_logins=owner_logins, cutoff=cutoff)


def recheck_binding(client: GitHubClient, binding: ApprovalBinding, *, owner_logins: Iterable[str],
                    cutoff: datetime) -> tuple[bool, str]:
    """Đọc lại GitHub ngay trước khi merge: review có bị dismiss/đổi, head có đổi không."""
    again, reason = bind_approval(client, pr=binding.pr, head_sha=binding.head_sha, patch_id=binding.patch_id,
                                  owner_logins=owner_logins, cutoff=cutoff)
    if again is None:
        return False, reason
    if again.review_id != binding.review_id:
        return False, f"review duyệt đã đổi ({binding.review_id} → {again.review_id})"
    return True, reason
