"""Phần dùng chung của pipeline PR-3: cấu hình, JSON Schema, lọc ledger probe.

- Cấu hình nằm trong ``config/harness.yaml`` (mục ``gate3``, ``github``, ``fix_loop``)
  và ``config/routing.yaml`` (``probe.task_id``). Mọi con số là mặc định tạm (R16).
- Bản ghi ledger của probe (``task_id`` = ``probe.task_id``) KHÔNG phải công việc của
  task nào: bị loại khỏi chi phí theo task và khỏi writer set của Gate 3.
- Schema ``approval``/``verdict`` nạp trực tiếp từ ``schemas/`` (giống heartbeat), không
  đổi registry của PR-1a.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Mapping

import yaml
from jsonschema import Draft202012Validator
from jsonschema.exceptions import best_match

from orchestrator.config import DEFAULT_CONFIG_PATH, REPO_ROOT, FixPolicy

SCHEMA_DIR = REPO_ROOT / "schemas"
DEFAULT_ROUTING_PATH = REPO_ROOT / "config" / "routing.yaml"
DEFAULT_PROBE_TASK_ID = "probe"
# Chỉ các adapter thật của PR-2 được làm reviewer; stub (Codex/Cursor) không bao giờ.
REVIEWER_ADAPTERS = ("claude", "dsh", "agy")
GREEN_RULES = ("model", "vendor")


class PipelineConfigError(ValueError):
    pass


class PipelineSchemaError(ValueError):
    pass


# --- schema -------------------------------------------------------------------------


@lru_cache(maxsize=None)
def load_schema(name: str) -> dict:
    with open(SCHEMA_DIR / f"{name}.schema.json", encoding="utf-8") as f:
        return json.load(f)


@lru_cache(maxsize=None)
def _validator(name: str, definition: str | None) -> Draft202012Validator:
    schema = load_schema(name)
    if definition is not None:
        # Một $defs con, giữ $defs để "#/$defs/..." vẫn phân giải được.
        schema = dict(schema["$defs"][definition], **{"$defs": schema["$defs"]})
    return Draft202012Validator(schema)


def validate(name: str, obj: object, *, definition: str | None = None) -> None:
    err = best_match(_validator(name, definition).iter_errors(obj))
    if err is not None:
        where = "/".join(str(p) for p in err.absolute_path) or "<gốc>"
        label = f"{name}#{definition}" if definition else name
        raise PipelineSchemaError(f"{label}: {where}: {err.message}")


# --- cấu hình -------------------------------------------------------------------------


@dataclass(frozen=True)
class ReviewerCandidate:
    adapter: str
    model: str
    effort: str | None = None


@dataclass(frozen=True)
class Gate3Settings:
    reviewer_agent: str = "gate3"
    # Red/Owner-only LUÔN khác vendor (R11). Green: "model" = khác model là đủ (mặc định
    # đề xuất, SPEC mục 8 câu 10); "vendor" = chặt như Red.
    green_rule: str = "model"
    max_diff_bytes: int = 400_000
    candidates: tuple[ReviewerCandidate, ...] = ()


@dataclass(frozen=True)
class GitHubSettings:
    repo: str = ""
    owner_logins: tuple[str, ...] = ()
    token_env: str = "GITHUB_TOKEN"
    required_check: str = "ci"
    actions_app_slug: str = "github-actions"
    api_url: str = "https://api.github.com"


@dataclass(frozen=True)
class PipelineSettings:
    gate3: Gate3Settings = field(default_factory=Gate3Settings)
    github: GitHubSettings = field(default_factory=GitHubSettings)
    fix: FixPolicy = field(default_factory=FixPolicy)
    fingerprint_line_window: int = 10
    probe_task_id: str = DEFAULT_PROBE_TASK_ID


def _read_yaml(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise PipelineConfigError(f"{path}: gốc phải là mapping")
    return data


def _candidates(raw: object) -> tuple[ReviewerCandidate, ...]:
    if not isinstance(raw, list):
        raise PipelineConfigError("gate3.candidates phải là danh sách")
    out = []
    for i, c in enumerate(raw):
        if not isinstance(c, dict) or not {"adapter", "model"} <= set(c) or set(c) - {"adapter", "model", "effort"}:
            raise PipelineConfigError(f"gate3.candidates[{i}]: chỉ có adapter, model, effort")
        if c["adapter"] not in REVIEWER_ADAPTERS:
            raise PipelineConfigError(
                f"gate3.candidates[{i}]: adapter {c['adapter']!r} không thuộc {REVIEWER_ADAPTERS}"
            )
        out.append(ReviewerCandidate(str(c["adapter"]), str(c["model"]),
                                     None if c.get("effort") is None else str(c["effort"])))
    return tuple(out)


def load_pipeline_settings(path: Path | None = None, *, routing_path: Path | None = None) -> PipelineSettings:
    data = _read_yaml(Path(path) if path else DEFAULT_CONFIG_PATH)
    g3 = data.get("gate3") or {}
    gh = data.get("github") or {}
    fix = data.get("fix_loop") or {}
    green_rule = str(g3.get("green_rule", "model"))
    if green_rule not in GREEN_RULES:
        raise PipelineConfigError(f"gate3.green_rule phải thuộc {GREEN_RULES}")
    window = int(fix.get("fingerprint_line_window", 10))
    if window < 1:
        raise PipelineConfigError("fix_loop.fingerprint_line_window phải >= 1")
    routing = _read_yaml(Path(routing_path) if routing_path else DEFAULT_ROUTING_PATH)
    probe_task_id = str((routing.get("probe") or {}).get("task_id", DEFAULT_PROBE_TASK_ID))
    return PipelineSettings(
        gate3=Gate3Settings(
            reviewer_agent=str(g3.get("reviewer_agent", "gate3")),
            green_rule=green_rule,
            max_diff_bytes=int(g3.get("max_diff_bytes", Gate3Settings.max_diff_bytes)),
            candidates=_candidates(g3.get("candidates") or []),
        ),
        github=GitHubSettings(
            repo=str(gh.get("repo", "")),
            owner_logins=tuple(str(x) for x in gh.get("owner_logins") or ()),
            token_env=str(gh.get("token_env", "GITHUB_TOKEN")),
            required_check=str(gh.get("required_check", "ci")),
            actions_app_slug=str(gh.get("actions_app_slug", "github-actions")),
            api_url=str(gh.get("api_url", "https://api.github.com")),
        ),
        fix=FixPolicy(
            max_fix_rounds=int(fix.get("max_fix_rounds", FixPolicy.max_fix_rounds)),
            budget_multiplier=float(fix.get("budget_multiplier", FixPolicy.budget_multiplier)),
        ),
        fingerprint_line_window=window,
        probe_task_id=probe_task_id,
    )


def env_token(settings: GitHubSettings, env: Mapping[str, str] | None = None) -> str | None:
    """Token GitHub đọc từ biến môi trường mà config CHỈ ghi tên."""
    env = os.environ if env is None else env
    value = env.get(settings.token_env)
    return value or None


# --- ledger ---------------------------------------------------------------------------


def is_probe_row(row: Mapping, probe_task_id: str = DEFAULT_PROBE_TASK_ID) -> bool:
    return row.get("task_id") == probe_task_id


def check_task_id(task_id: str, probe_task_id: str = DEFAULT_PROBE_TASK_ID) -> str:
    if task_id == probe_task_id:
        raise PipelineConfigError(f"task_id {task_id!r} dành riêng cho probe, không phải task thật")
    return task_id


def task_rows(records: Iterable[Mapping], task_id: str, probe_task_id: str = DEFAULT_PROBE_TASK_ID) -> list[Mapping]:
    """Bản ghi ledger của ``task_id`` (mọi attempt), không gồm probe."""
    check_task_id(task_id, probe_task_id)
    return [r for r in records if r.get("task_id") == task_id and not is_probe_row(r, probe_task_id)]


def task_cost_by_attempt(records: Iterable[Mapping], task_id: str,
                         probe_task_id: str = DEFAULT_PROBE_TASK_ID) -> dict[int, float | None]:
    """Chi phí theo attempt (giống ``Ledger.cost_by_attempt``) nhưng loại probe.

    Lời gọi chưa có giá làm attempt đó ``None`` → ngân sách không xác định → fail-closed.
    """
    out: dict[int, float | None] = {}
    for r in task_rows(records, task_id, probe_task_id):
        a = int(r["attempt"])
        if r.get("cost_usd") is None or (a in out and out[a] is None):
            out[a] = None
        else:
            out[a] = round((out.get(a) or 0.0) + float(r["cost_usd"]), 9)
    return out
