"""Client GitHub REST tối thiểu, CHỈ ĐỌC (PR-3).

- Chỉ có GET. Không có hàm merge, push, approve hay ghi comment: pipeline không bao giờ
  merge (R7).
- Token đọc từ biến môi trường mà config chỉ ghi TÊN (``github.token_env``), không bao
  giờ ghi token vào file.
- ``http_get`` thay được trong test; mặc định dùng ``urllib`` với HTTPS.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Mapping

from .common import GitHubSettings, env_token

API_VERSION = "2022-11-28"
PER_PAGE = 100
MAX_PAGES = 50
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")

HttpGet = Callable[[str, Mapping[str, str], float], tuple[int, Any]]


class GitHubError(RuntimeError):
    pass


def urllib_get(url: str, headers: Mapping[str, str], timeout_s: float) -> tuple[int, Any]:
    req = urllib.request.Request(url, headers=dict(headers), method="GET")
    try:
        # GitHubClient chỉ nhận api_url https:// nên url luôn là HTTPS.
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read().decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            body = None
        return exc.code, body
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise GitHubError(f"GET {url}: {exc}") from exc


class GitHubClient:
    def __init__(self, repo: str, *, token: str | None = None, api_url: str = "https://api.github.com",
                 http_get: HttpGet | None = None, timeout_s: float = 20.0):
        if not _REPO_RE.match(repo or ""):
            raise GitHubError(f"repo phải có dạng owner/name, nhận {repo!r}")
        if not api_url.startswith("https://"):
            raise GitHubError(f"api_url phải là https://, nhận {api_url!r}")
        self.repo = repo
        self.api_url = api_url.rstrip("/")
        self._token = token
        self._get = http_get or urllib_get
        self.timeout_s = timeout_s

    @classmethod
    def from_settings(cls, settings: GitHubSettings, *, env: Mapping[str, str] | None = None,
                      http_get: HttpGet | None = None) -> "GitHubClient":
        return cls(settings.repo, token=env_token(settings, env), api_url=settings.api_url, http_get=http_get)

    def _headers(self) -> dict[str, str]:
        h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": API_VERSION,
             "User-Agent": "harness-agents-pipeline"}
        if self._token:
            h["Authorization"] = f"Bearer {self._token}"
        return h

    def get(self, path: str, params: Mapping[str, Any] | None = None) -> Any:
        url = f"{self.api_url}/repos/{self.repo}/{path.lstrip('/')}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        status, body = self._get(url, self._headers(), self.timeout_s)
        if status != 200:
            msg = body.get("message") if isinstance(body, dict) else None
            raise GitHubError(f"GET {url}: HTTP {status} {msg or ''}".strip())
        return body

    def paginate(self, path: str, params: Mapping[str, Any] | None = None, *, key: str | None = None) -> list:
        """Mọi phần tử qua các trang ``per_page=100`` (``key``: mảng nằm trong object)."""
        out: list = []
        for page in range(1, MAX_PAGES + 1):
            body = self.get(path, {**(params or {}), "per_page": PER_PAGE, "page": page})
            items = body.get(key) if key is not None and isinstance(body, dict) else body
            if not isinstance(items, list):
                raise GitHubError(f"{path}: phản hồi không có danh sách {key or ''}".strip())
            out.extend(items)
            if len(items) < PER_PAGE:
                return out
        raise GitHubError(f"{path}: quá {MAX_PAGES} trang")
