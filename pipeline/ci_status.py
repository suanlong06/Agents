"""Trạng thái CI theo SHA, chỉ từ check run GitHub Actions (PR-3, R1, R9, R14).

- Nguồn duy nhất: ``GET /repos/{repo}/commits/{sha}/check-runs``. Chỉ check run có
  ``app.slug == "github-actions"``, đúng tên required check (``ci``) và đúng
  ``head_sha`` mới được tính; check run cùng tên từ app khác (commit status giả, N1) bị
  bỏ qua và ghi lại trong ``ignored``.
- KHÔNG chạy CI cục bộ (module không import subprocess/os) và KHÔNG nhận output CI dán
  tay: mọi hàm chỉ nhận SHA hoặc JSON check run của GitHub, không có tham số text/log.
- Check run chạy lại: lấy lần mới nhất. Chưa xong → ``pending``; không có → ``missing``;
  kết luận khác ``success`` (kể cả ``skipped``, ``neutral``) → ``failure`` (fail-closed).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping

from .github_api import SHA_RE, GitHubClient, GitHubError

SUCCESS, FAILURE, PENDING, MISSING = "success", "failure", "pending", "missing"
# Bước kế tiếp trong state machine (orchestrator/state.py): CI đỏ → fix list; chưa có kết
# quả → blocked (chờ và thông báo, R2).
NEXT_STATE = {SUCCESS: "gate3_review", FAILURE: "fix_list", PENDING: "blocked", MISSING: "blocked"}


@dataclass(frozen=True)
class CiStatus:
    sha: str
    state: str
    check_name: str
    check_run_id: int | None = None
    conclusion: str | None = None
    html_url: str | None = None
    ignored: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return self.state == SUCCESS

    @property
    def next_state(self) -> str:
        return NEXT_STATE[self.state]


def _check_sha(sha: str) -> str:
    if not isinstance(sha, str) or not SHA_RE.match(sha):
        raise ValueError(f"SHA phải là 40 ký tự hex thường, nhận {sha!r}")
    return sha


def evaluate(check_runs: Iterable[Mapping], sha: str, *, check_name: str = "ci",
             app_slug: str = "github-actions") -> CiStatus:
    """Trạng thái CI của ``sha`` từ danh sách check run (JSON của GitHub)."""
    _check_sha(sha)
    mine, ignored = [], []
    for run in check_runs:
        if not isinstance(run, Mapping):
            raise GitHubError("check run phải là object JSON của GitHub")
        if run.get("name") != check_name:
            continue
        slug = (run.get("app") or {}).get("slug")
        if slug != app_slug:
            ignored.append(f"{run.get('id')}: app {slug!r} không phải {app_slug}")
            continue
        if run.get("head_sha") != sha:
            ignored.append(f"{run.get('id')}: head_sha {run.get('head_sha')!r} khác {sha}")
            continue
        mine.append(run)
    if not mine:
        return CiStatus(sha, MISSING, check_name, ignored=tuple(ignored))
    run = max(mine, key=lambda r: (r.get("started_at") or "", int(r.get("id") or 0)))
    conclusion = run.get("conclusion")
    if run.get("status") != "completed":
        state = PENDING
    else:
        state = SUCCESS if conclusion == "success" else FAILURE
    return CiStatus(sha, state, check_name, int(run["id"]) if run.get("id") is not None else None,
                    conclusion, run.get("html_url"), tuple(ignored))


def fetch_ci_status(client: GitHubClient, sha: str, *, check_name: str = "ci",
                    app_slug: str = "github-actions") -> CiStatus:
    """Đọc check run của ``sha`` từ GitHub và đánh giá. Lỗi mạng/API → ``GitHubError``."""
    _check_sha(sha)
    runs = client.paginate(f"commits/{sha}/check-runs", {"check_name": check_name, "filter": "all"},
                           key="check_runs")
    return evaluate(runs, sha, check_name=check_name, app_slug=app_slug)
