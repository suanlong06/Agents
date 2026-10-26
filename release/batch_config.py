"""Cấu hình chung cho digest / batch release / revert tự động (PR-4b, R16).

Mọi con số là MẶC ĐỊNH TẠM trong ``config/harness.yaml`` (mục ``batch``), không phải yêu cầu.

- ``merge_identities``: **allowlist danh tính GitHub được merge trong batch**. Mặc định RỖNG
  → batch không merge gì (fail-closed). Danh tính merge (login của token trong
  ``merge_token_env``) phải nằm trong allowlist và KHÁC tác giả PR (không ai merge PR của chính
  mình, SPEC mục 5.1). Có danh tính merge KHÔNG có nghĩa là có auto-merge: batch chỉ merge PR
  có phê duyệt của người dùng gắn ``(head SHA, patch-id)``.
- ``workdir``: clone làm việc riêng của batch (ngoài repo, R13). Người dùng tạo khi kích hoạt
  (``docs/runbooks/batch.md``); thiếu thì batch dừng, không tự clone.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Mapping

import yaml

from orchestrator.config import REPO_ROOT, ConfigError, _expand, _is_within
from pipeline import common

BATCH_AGENT = "batch"
_LOGIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")


@dataclass(frozen=True)
class BatchSettings:
    workdir: Path
    remote: str = "origin"
    base_branch: str = "main"
    max_prs: int = 5
    merge_identities: tuple[str, ...] = ()
    merge_token_env: str = "HARNESS_MERGE_TOKEN"
    merge_method: str = "merge"
    ci_timeout_s: float = 3600.0
    ci_poll_s: float = 30.0
    check_timeout_s: float = 900.0
    integration_prefix: str = "harness/batch/"
    revert_prefix: str = "revert/"
    tag_prefix: str = "rel-"
    check_name: str = "ci"
    app_slug: str = "github-actions"

    def __post_init__(self):
        if self.max_prs < 1:
            raise ConfigError("batch.max_prs phải >= 1")
        if self.ci_timeout_s <= 0 or self.ci_poll_s <= 0 or self.check_timeout_s <= 0:
            raise ConfigError("batch timeout/poll phải > 0")
        for login in self.merge_identities:
            if not isinstance(login, str) or not _LOGIN_RE.match(login):
                raise ConfigError(f"batch.merge_identities có login không hợp lệ: {login!r}")

    def merge_allowed(self, login: str | None) -> bool:
        return bool(login) and login.casefold() in {m.casefold() for m in self.merge_identities}

    def merge_token(self, env: Mapping[str, str] | None = None) -> str | None:
        env = os.environ if env is None else env
        return env.get(self.merge_token_env) or None

    @classmethod
    def from_mapping(cls, data: Mapping | None, *, env: Mapping[str, str] | None = None,
                     repo_root: Path = REPO_ROOT) -> "BatchSettings":
        data = dict(data or {})
        env = os.environ if env is None else env
        raw_workdir = data.get("workdir") or "%LOCALAPPDATA%\\hw\\rel"
        workdir = Path(_expand(str(raw_workdir), env))
        if not workdir.is_absolute():
            raise ConfigError(f"batch.workdir phải tuyệt đối: {raw_workdir!r}")
        if _is_within(workdir.resolve(), repo_root.resolve()):
            raise ConfigError(f"batch.workdir {workdir} nằm trong repo; phải ở ngoài (R13)")
        idents = data.get("merge_identities") or []
        if not isinstance(idents, list):
            raise ConfigError("batch.merge_identities phải là danh sách login")
        return cls(
            workdir=workdir, remote=str(data.get("remote", cls.remote)),
            base_branch=str(data.get("base_branch", cls.base_branch)),
            max_prs=int(data.get("max_prs", cls.max_prs)), merge_identities=tuple(idents),
            merge_token_env=str(data.get("merge_token_env", cls.merge_token_env)),
            merge_method=str(data.get("merge_method", cls.merge_method)),
            ci_timeout_s=float(data.get("ci_timeout_minutes", 60)) * 60,
            ci_poll_s=float(data.get("ci_poll_s", cls.ci_poll_s)),
            check_timeout_s=float(data.get("check_timeout_minutes", 15)) * 60,
            integration_prefix=str(data.get("integration_prefix", cls.integration_prefix)),
            revert_prefix=str(data.get("revert_prefix", cls.revert_prefix)),
            tag_prefix=str(data.get("tag_prefix", cls.tag_prefix)),
        )

    @classmethod
    def load(cls, path: Path | None = None, *, env: Mapping[str, str] | None = None) -> "BatchSettings":
        with open(Path(path) if path else common.DEFAULT_CONFIG_PATH, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        gh = data.get("github") or {}
        s = cls.from_mapping(data.get("batch"), env=env)
        return replace(s, check_name=gh.get("required_check", "ci"),
                       app_slug=gh.get("actions_app_slug", "github-actions"))


def record(audit, action: str, result: str, *, sha: str | None = None, pr: int | None = None,
           detail: dict | None = None, dedup_key: str | None = None) -> dict | None:
    """Một bản ghi audit của batch (agent ``batch``, không có model). ``result`` không dùng
    ``error``/``rejected`` cho diễn biến bình thường (``log_errors`` của kiểm tra read-only đếm
    chúng)."""
    return audit.append(action=action, result=result, agent=BATCH_AGENT, served_model=None, vendor=None,
                        model_id_source=None, sha=sha, task_id=None, attempt=None, pr=pr, detail=detail,
                        dedup_key=dedup_key)
