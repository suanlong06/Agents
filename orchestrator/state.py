"""State machine, lineage và state store (PR-1a, R2/R3/R13).

- Trạng thái theo bước 1–13 (SPEC mục 3) cộng 5 trạng thái MỞ RỘNG so với SPEC
  gốc: ``escalated``, ``blocked``, ``superseded``, ``cancelled``, ``awaiting-human``.
- Lineage: ``task_id`` cố định, mỗi PR mới là một ``attempt`` mới, ``parent_pr``
  trỏ về PR của attempt trước; attempt cũ kết thúc ở ``superseded``.
- ``fix_rounds`` và ngân sách vòng fix cộng dồn theo ``task_id``, KHÔNG reset khi
  mở PR mới. Vượt N vòng hoặc vượt ngân sách thì vào ``escalated``.
- Chuyển trạng thái sai ném ``InvalidTransition``; task gốc không bị sửa.
- Trạng thái lạ (do state bản sau ghi) đọc được nhưng không chuyển được.
- ``StateStore`` đọc/ghi snapshot duy nhất trong ``state_dir`` (ngoài repo), ghi
  nguyên tử và chỉ khi giữ ``WriterGuard``.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Mapping

from . import schemas
from .config import FixPolicy
from .fsio import RetryPolicy, WriterGuard, atomic_write_bytes

USER = "user"

STEP_STATES: dict[int, str] = {
    1: "intake",
    2: "spec_draft",
    3: "spec_review",
    4: "pr_opened",
    5: "labelled",
    6: "ci_check",
    7: "gate3_review",
    8: "fix_list",
    9: "approved_merge",
    10: "batch_release",
    11: "readonly_check",
    12: "revert_incident",
    13: "maintenance",
}
EXTENDED_STATES: tuple[str, ...] = ("escalated", "blocked", "superseded", "cancelled", "awaiting-human")
ALL_STATES: tuple[str, ...] = tuple(STEP_STATES.values()) + EXTENDED_STATES
TERMINAL_STATES = frozenset({"maintenance", "superseded", "cancelled"})

# Cạnh tiến giữa các bước. Fixer là stub nên fix list đi vào ``awaiting-human``;
# PR mới của vòng fix đi qua ``start_new_attempt`` chứ không qua cạnh nào ở đây.
_STEP_EDGES: dict[str, frozenset[str]] = {
    "intake": frozenset({"spec_draft", "awaiting-human"}),
    "spec_draft": frozenset({"spec_review"}),
    "spec_review": frozenset({"pr_opened", "spec_draft"}),
    "pr_opened": frozenset({"labelled"}),
    "labelled": frozenset({"ci_check"}),
    "ci_check": frozenset({"gate3_review", "fix_list"}),
    "gate3_review": frozenset({"approved_merge", "fix_list"}),
    "fix_list": frozenset({"awaiting-human"}),
    "approved_merge": frozenset({"batch_release"}),
    "batch_release": frozenset({"readonly_check"}),
    "readonly_check": frozenset({"maintenance", "revert_incident"}),
    "revert_incident": frozenset({"batch_release", "awaiting-human"}),
}
# Lối ra khỏi trạng thái chờ người dùng: chỉ actor ``user``.
_USER_EXITS: dict[str, frozenset[str]] = {
    "escalated": frozenset({"awaiting-human"}),
    "awaiting-human": frozenset({"spec_draft", "pr_opened"}),
}
# Quyết định kết thúc task: chỉ actor ``user``, từ mọi trạng thái chưa kết thúc.
_USER_ONLY_TARGETS = frozenset({"cancelled", "superseded"})
# Được vào ``blocked`` (rồi quay lại đúng bước cũ) từ mọi bước chưa kết thúc.
_BLOCKABLE = frozenset(STEP_STATES.values()) - TERMINAL_STATES
# ``new_attempt`` chỉ hợp lệ khi đang có fix list (ở máy hoặc ở người).
_NEW_ATTEMPT_FROM = frozenset({"fix_list", "awaiting-human"})


class InvalidTransition(ValueError):
    """Chuyển trạng thái hoặc thao tác lineage không hợp lệ."""


def new_task(
    task_id: str,
    *,
    kind: str = "feature",
    title: str = "",
    parent_pr: int | None = None,
    now: str,
) -> dict:
    if kind not in ("feature", "bugfix"):
        raise InvalidTransition(f"kind không hợp lệ: {kind!r}")
    return {
        "task_id": task_id,
        "kind": kind,
        "title": title,
        "state": "intake",
        "attempt": 1,
        "pr": None,
        "parent_pr": parent_pr,
        "head_sha": None,
        "fix_rounds": 0,
        "resume_state": None,
        "escalation_reason": None,
        "attempts": [],
        "created_at": now,
        "updated_at": now,
    }


def _allowed(cur: str, to: str, actor: str, task: dict) -> bool:
    if to in _USER_ONLY_TARGETS:
        return actor == USER
    if to == "escalated":
        return cur != "escalated"
    if cur == "blocked":
        return to == task.get("resume_state")
    if to == "blocked":
        return cur in _BLOCKABLE
    if cur in _USER_EXITS:
        return actor == USER and to in _USER_EXITS[cur]
    return to in _STEP_EDGES.get(cur, frozenset())


def _budget_problem(cost_by_attempt: Mapping[int, float | None] | None, multiplier: float) -> str | None:
    """Lý do vượt ngân sách vòng fix, hoặc ``None`` nếu còn trong ngân sách.

    Ngân sách = ``multiplier`` x chi phí attempt đầu, cho tổng chi phí các attempt
    sửa (attempt >= 2) của cùng ``task_id``. Chi phí ``None`` (model chưa có giá)
    làm ngân sách không xác định → fail-closed.
    """
    if not cost_by_attempt:
        return None
    fix = {int(a): c for a, c in cost_by_attempt.items() if int(a) > 1}
    if not fix:
        return None
    first = cost_by_attempt.get(1, 0.0)
    if first is None or any(c is None for c in fix.values()):
        return "ngân sách vòng fix không xác định: có lời gọi model chưa có giá trong prices.yaml"
    spent = sum(fix.values())
    limit = multiplier * first
    if spent > limit:
        return f"vượt ngân sách vòng fix: {spent:.6f} USD > {multiplier:g} x {first:.6f} USD"
    return None


def apply_transition(
    task: dict,
    to: str,
    *,
    actor: str,
    now: str,
    policy: FixPolicy,
    cost_by_attempt: Mapping[int, float | None] | None = None,
    pr: int | None = None,
    sha: str | None = None,
    reason: str | None = None,
) -> dict:
    """Trả về bản sao task sau khi chuyển sang ``to``; ném ``InvalidTransition``.

    Vào ``fix_list`` là một vòng fix: ``fix_rounds`` tăng; vượt N vòng hoặc vượt
    ngân sách thì trạng thái hiệu lực là ``escalated`` thay cho ``fix_list``.
    """
    cur = task["state"]
    if cur not in ALL_STATES:
        raise InvalidTransition(
            f"trạng thái hiện tại {cur!r} không biết (state bản sau?), chỉ đọc: {cur} → {to}"
        )
    if to not in ALL_STATES:
        raise InvalidTransition(f"trạng thái đích không biết: {cur} → {to}")
    if cur in TERMINAL_STATES:
        raise InvalidTransition(f"{cur} là trạng thái kết thúc: {cur} → {to}")
    if not _allowed(cur, to, actor, task):
        raise InvalidTransition(f"chuyển trạng thái không hợp lệ: {cur} → {to} (actor={actor})")
    if pr is not None and task["pr"] is not None and pr != task["pr"]:
        raise InvalidTransition(
            f"không được đổi PR {task['pr']} → {pr} trong transition; PR mới phải qua new_attempt"
        )
    if to == "pr_opened" and task["pr"] is not None:
        raise InvalidTransition(f"task đã có PR {task['pr']}; PR mới phải qua new_attempt: {cur} → {to}")

    t = copy.deepcopy(task)
    if pr is not None:
        t["pr"] = pr
    if sha:
        t["head_sha"] = sha
    t["updated_at"] = now

    effective = to
    if cur == "blocked":
        t["resume_state"] = None
    elif to == "blocked":
        t["resume_state"] = cur
    elif to == "fix_list":
        # Quay lại fix_list từ blocked không phải vòng mới (nhánh trên).
        t["fix_rounds"] = task["fix_rounds"] + 1
        if t["fix_rounds"] > policy.max_fix_rounds:
            effective = "escalated"
            t["escalation_reason"] = (
                f"fix_rounds {t['fix_rounds']} vượt giới hạn N = {policy.max_fix_rounds}"
            )
        else:
            problem = _budget_problem(cost_by_attempt, policy.budget_multiplier)
            if problem:
                effective = "escalated"
                t["escalation_reason"] = problem
    if to == "escalated":
        t["escalation_reason"] = reason or f"{actor} chuyển task sang escalated"
    t["state"] = effective
    return t


def start_new_attempt(task: dict, *, pr: int, sha: str | None, now: str) -> dict:
    """Mở PR mới cho cùng ``task_id``: attempt cũ thành ``superseded``.

    ``fix_rounds`` giữ nguyên (cộng dồn theo task_id); attempt mới vào ``pr_opened``.
    """
    cur = task["state"]
    if cur not in _NEW_ATTEMPT_FROM:
        raise InvalidTransition(f"new_attempt chỉ hợp lệ từ fix_list/awaiting-human, đang ở {cur}")
    if task["pr"] is None:
        raise InvalidTransition("new_attempt cần một PR hiện tại để thay thế")
    used = {task["pr"]} | {a["pr"] for a in task["attempts"]}
    if pr in used:
        raise InvalidTransition(f"new_attempt phải dùng PR mới, PR {pr} đã thuộc task {task['task_id']}")

    t = copy.deepcopy(task)
    t["attempts"].append({
        "attempt": task["attempt"],
        "pr": task["pr"],
        "parent_pr": task["parent_pr"],
        "head_sha": task["head_sha"],
        "final_state": "superseded",
    })
    t.update(
        attempt=task["attempt"] + 1,
        pr=pr,
        parent_pr=task["pr"],
        head_sha=sha,
        state="pr_opened",
        resume_state=None,
        updated_at=now,
    )
    return t


def empty_snapshot() -> dict:
    return {
        "schema_version": schemas.CURRENT_VERSION["state"],
        "kind": "harness-state",
        "source": "runtime",
        "as_of": None,
        "fold_count": 0,
        "offsets": {},
        "watermarks": {},
        "tasks": {},
        "quarantine": [],
    }


def read_snapshot(path: Path) -> dict:
    """Đọc và kiểm một snapshot (không cần quyền ghi). Trường lạ giữ nguyên."""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise schemas.SchemaError(f"{path}: snapshot không phải object")
    schemas.check_version("state", data.get("schema_version"))
    schemas.validate("state", data)
    return data


class StateStore:
    """Snapshot state duy nhất: tasks + offsets + watermarks trong MỘT file."""

    FILE = "state.json"

    def __init__(self, state_dir: Path, guard: WriterGuard, retry: RetryPolicy):
        self.path = Path(state_dir) / self.FILE
        self.guard = guard
        self.retry = retry

    def load(self) -> dict:
        if not self.path.exists():
            return empty_snapshot()
        return read_snapshot(self.path)

    def save(self, snapshot: dict) -> int:
        """Ghi nguyên tử; ném ``StateWriteError`` khi hết retry (file cũ giữ nguyên)."""
        self.guard.ensure_held()
        schemas.validate("state", snapshot)
        data = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, indent=1).encode("utf-8")
        return atomic_write_bytes(self.path, data + b"\n", self.retry)
