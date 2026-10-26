"""Client GitHub có quyền GHI, hẹp, chỉ cho batch release (PR-4b, R7, R9, R10).

- ``pipeline.github_api.GitHubClient`` vẫn CHỈ ĐỌC; mọi thao tác ghi nằm ở đây, dùng token
  riêng của **danh tính merge** (biến môi trường ``batch.merge_token_env``), không phải token
  đọc của pipeline.
- Thao tác được phép (allowlist method + path, không có endpoint ghi chung chung):

  | Thao tác | Endpoint | Giới hạn |
  |---|---|---|
  | ``whoami`` | ``GET /user`` | xác định danh tính merge |
  | ``create_branch`` | ``POST git/refs`` | chỉ nhánh ``integration_prefix``/``revert_prefix`` |
  | ``merge_into_integration`` | ``POST merges`` | base CHỈ là nhánh ``integration_prefix`` |
  | ``delete_integration_branch`` | ``DELETE git/refs/heads/…`` | chỉ ``integration_prefix`` |
  | ``open_pull`` / ``close_pull`` | ``POST pulls`` / ``PATCH pulls/N`` | PR tích hợp (draft) / PR revert |
  | ``create_tag`` | ``POST git/tags`` + ``POST git/refs`` | chỉ ``tag_prefix``, không ghi đè |
  | ``merge_pull`` | ``PUT pulls/N/merge`` | CẦN ``ApprovalBinding``; gửi ``sha`` = head đã duyệt |

- ``merge_pull`` là đường DUY NHẤT trong repo merge vào ``main`` (test AST kiểm chỉ
  ``release/batch.py`` gọi nó). Nó đòi một ``ApprovalBinding`` (review ``APPROVED`` của người
  dùng gắn ``(head SHA, patch-id)``) và gửi ``sha`` để GitHub từ chối nếu head đã đổi. Không có
  auto-merge: không có tham số, cờ hay đường code nào merge PR chưa được người dùng duyệt.
- Không force: không có endpoint ``PATCH git/refs`` (cập nhật ref có ``force``), không xóa tag,
  không ghi lên ``main`` ngoài ``merge_pull``.
- Best-effort (R15): token nằm trong biến môi trường của OS user hiện tại; tách OS user + ACL
  thư mục state vẫn là điều kiện triển khai còn mở (``docs/runbooks/batch.md``).
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Mapping

from pipeline.github_api import SHA_RE

from .approval_binding import ApprovalBinding
from .gitops import check_ref_name

API_VERSION = "2022-11-28"
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
MERGE_METHODS = ("merge",)  # merge commit: cây của main sau merge == cây của bước tích hợp

HttpRequest = Callable[[str, str, Mapping[str, str], Any, float], tuple[int, Any]]


class GitHubWriteError(RuntimeError):
    def __init__(self, msg: str, status: int | None = None):
        self.status = status
        super().__init__(msg)


class IntegrationConflict(GitHubWriteError):
    """``POST merges`` trả 409: PR không merge sạch vào nhánh tích hợp."""


def urllib_request(method: str, url: str, headers: Mapping[str, str], body: Any, timeout_s: float) -> tuple[int, Any]:
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=dict(headers), method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        # GitHubWriter chỉ nhận api_url https:// nên url luôn là HTTPS.
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw.decode("utf-8")) if raw else None)
    except urllib.error.HTTPError as exc:
        try:
            body_out = json.loads(exc.read().decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            body_out = None
        return exc.code, body_out
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise GitHubWriteError(f"{method} {url}: {exc}") from exc


class GitHubWriter:
    def __init__(self, repo: str, *, token: str | None, api_url: str = "https://api.github.com",
                 http: HttpRequest | None = None, timeout_s: float = 30.0, base_branch: str = "main",
                 integration_prefix: str = "harness/batch/", revert_prefix: str = "revert/",
                 tag_prefix: str = "rel-"):
        if not _REPO_RE.match(repo or ""):
            raise GitHubWriteError(f"repo phải có dạng owner/name, nhận {repo!r}")
        if not api_url.startswith("https://"):
            raise GitHubWriteError(f"api_url phải là https://, nhận {api_url!r}")
        for name, pfx in (("integration_prefix", integration_prefix), ("revert_prefix", revert_prefix),
                          ("tag_prefix", tag_prefix)):
            if not pfx or not re.match(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$", pfx) or pfx.rstrip("/") == base_branch:
                raise GitHubWriteError(f"{name} không hợp lệ: {pfx!r}")
        self.repo = repo
        self.api_url = api_url.rstrip("/")
        self._token = token
        self._http = http or urllib_request
        self.timeout_s = timeout_s
        self.base_branch = check_ref_name(base_branch)
        self.integration_prefix = integration_prefix
        self.revert_prefix = revert_prefix
        self.tag_prefix = tag_prefix

    # --- HTTP ------------------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": API_VERSION,
             "User-Agent": "harness-agents-batch"}
        if self._token:
            h["Authorization"] = f"Bearer {self._token}"
        return h

    def _call(self, method: str, path: str, body: Any = None, *, ok: tuple[int, ...] = (200, 201),
              repo_scoped: bool = True) -> tuple[int, Any]:
        url = f"{self.api_url}/repos/{self.repo}/{path.lstrip('/')}" if repo_scoped else f"{self.api_url}/{path}"
        status, out = self._http(method, url, self._headers(), body, self.timeout_s)
        if status not in ok:
            msg = out.get("message") if isinstance(out, dict) else None
            raise GitHubWriteError(f"{method} {url}: HTTP {status} {msg or ''}".strip(), status)
        return status, out

    # --- thao tác --------------------------------------------------------------------

    def whoami(self) -> str:
        _s, out = self._call("GET", "user", ok=(200,), repo_scoped=False)
        login = (out or {}).get("login")
        if not isinstance(login, str) or not login:
            raise GitHubWriteError("GET /user không trả login")
        return login

    def _own_branch(self, name: str, *, revert_ok: bool) -> str:
        check_ref_name(name)
        prefixes = (self.integration_prefix,) + ((self.revert_prefix,) if revert_ok else ())
        if not name.startswith(prefixes) or name == self.base_branch:
            raise GitHubWriteError(f"batch không được đụng nhánh {name!r} (chỉ {prefixes})")
        return name

    def create_branch(self, name: str, sha: str) -> None:
        self._own_branch(name, revert_ok=True)
        if not SHA_RE.match(sha or ""):
            raise GitHubWriteError(f"SHA không hợp lệ: {sha!r}")
        self._call("POST", "git/refs", {"ref": f"refs/heads/{name}", "sha": sha}, ok=(201,))

    def merge_into_integration(self, branch: str, head_sha: str, message: str) -> str:
        """Merge ``head_sha`` vào nhánh TÍCH HỢP phía GitHub (cùng engine với nút merge PR).

        Trả SHA merge commit. 409 → ``IntegrationConflict``; 204 (không có gì để merge) → lỗi."""
        if not branch.startswith(self.integration_prefix):
            raise GitHubWriteError(f"merge_into_integration chỉ nhận nhánh {self.integration_prefix}*")
        self._own_branch(branch, revert_ok=False)
        if not SHA_RE.match(head_sha or ""):
            raise GitHubWriteError(f"SHA không hợp lệ: {head_sha!r}")
        try:
            status, out = self._call("POST", "merges", {"base": branch, "head": head_sha, "commit_message": message},
                                     ok=(201, 204))
        except GitHubWriteError as exc:
            if exc.status == 409:
                raise IntegrationConflict(f"{head_sha} conflict khi tích hợp vào {branch}", 409) from exc
            raise
        sha = (out or {}).get("sha") if status == 201 else None
        if not isinstance(sha, str) or not SHA_RE.match(sha):
            raise GitHubWriteError(f"merges trả {status} không có merge commit (PR rỗng hoặc đã có trong base?)")
        return sha

    def delete_integration_branch(self, branch: str) -> None:
        if not branch.startswith(self.integration_prefix):
            raise GitHubWriteError(f"chỉ xóa được nhánh {self.integration_prefix}*")
        self._own_branch(branch, revert_ok=False)
        self._call("DELETE", f"git/refs/heads/{urllib.parse.quote(branch, safe='/')}", ok=(204,))

    def open_pull(self, *, head: str, title: str, body: str, draft: bool) -> int:
        self._own_branch(head, revert_ok=True)
        _s, out = self._call("POST", "pulls", {"head": head, "base": self.base_branch, "title": title,
                                               "body": body, "draft": bool(draft),
                                               "maintainer_can_modify": False}, ok=(201,))
        number = (out or {}).get("number")
        if isinstance(number, bool) or not isinstance(number, int) or number < 1:
            raise GitHubWriteError("POST pulls không trả số PR")
        return number

    def close_pull(self, number: int) -> None:
        self._call("PATCH", f"pulls/{int(number)}", {"state": "closed"}, ok=(200,))

    def create_tag(self, name: str, sha: str, message: str) -> None:
        """Annotated tag ``name`` trên ``sha``. Không ghi đè tag có sẵn (GitHub trả 422)."""
        check_ref_name(name)
        if not name.startswith(self.tag_prefix) or not SHA_RE.match(sha or ""):
            raise GitHubWriteError(f"tag {name!r}/{sha!r} không hợp lệ (chỉ {self.tag_prefix}*)")
        _s, out = self._call("POST", "git/tags", {"tag": name, "message": message, "object": sha, "type": "commit"},
                             ok=(201,))
        tag_sha = (out or {}).get("sha")
        if not isinstance(tag_sha, str) or not SHA_RE.match(tag_sha):
            raise GitHubWriteError("POST git/tags không trả SHA object tag")
        self._call("POST", "git/refs", {"ref": f"refs/tags/{name}", "sha": tag_sha}, ok=(201,))

    def merge_pull(self, binding: ApprovalBinding, *, merge_method: str = "merge", commit_title: str | None = None) -> str:
        """Merge PR ``binding.pr`` vào ``main``: đường DUY NHẤT, chỉ với phê duyệt của người dùng.

        ``sha`` = ``binding.head_sha``: head đổi sau khi duyệt thì GitHub từ chối (409). Branch
        protection (review CODEOWNERS, check ``ci``) vẫn do GitHub thi hành."""
        if type(binding) is not ApprovalBinding:  # noqa: E721 — không nhận object giả dạng
            raise GitHubWriteError("merge_pull cần ApprovalBinding (phê duyệt của người dùng)")
        if merge_method not in MERGE_METHODS:
            raise GitHubWriteError(f"merge_method {merge_method!r} không được phép (chỉ {MERGE_METHODS})")
        body = {"sha": binding.head_sha, "merge_method": merge_method}
        if commit_title:
            body["commit_title"] = commit_title
        _s, out = self._call("PUT", f"pulls/{binding.pr}/merge", body, ok=(200,))
        sha = (out or {}).get("sha")
        if not (out or {}).get("merged") or not isinstance(sha, str) or not SHA_RE.match(sha):
            raise GitHubWriteError(f"PR #{binding.pr} không merge được: {out!r}"[:300])
        return sha
