"""Vòng fix (PR-3, R2, R16).

- Mỗi lần vào ``fix_list`` là một vòng fix. ``fix_rounds`` và ngân sách cộng dồn theo
  ``task_id``, KHÔNG reset khi mở PR mới (state machine của PR-1a giữ bộ đếm). Mặc định
  tạm trong ``config/harness.yaml``: N = 2 vòng, ngân sách các attempt sửa <= 3x chi phí
  attempt đầu. Chi phí lấy từ ledger, KHÔNG gồm probe.
- Vượt N vòng hoặc vượt ngân sách (hay ngân sách không xác định vì có lời gọi chưa có
  giá) → ``escalated``, chờ người dùng.
- Fixer-Agent là STUB: module này KHÔNG BAO GIỜ gọi Fixer, Cursor hay model nào. Fix list
  vào ``awaiting-human``; người dùng điều phối việc sửa rồi PR mới được mở qua
  ``open_fix_pr``: attempt cũ thành ``superseded``, ``parent_pr`` trỏ về PR cũ.
- Finding có fingerprint ``sha256(file đã chuẩn hóa, loại, cửa sổ dòng)``; cửa sổ dòng là
  mặc định tạm. Finding trùng fingerprint được gộp; fingerprint đã có ở vòng trước được
  đánh dấu ``recurring``.
- Vòng fix không ghi state: nó dự đoán bằng chính state machine (``apply_transition``)
  rồi ghi event vào inbox của CHÍNH nó (R3); orchestrator fold và quyết định cuối.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Iterable, Mapping
from zoneinfo import ZoneInfo

from orchestrator.config import FixPolicy
from orchestrator.state import apply_transition, start_new_attempt

from .common import DEFAULT_PROBE_TASK_ID, check_task_id, task_cost_by_attempt
from .labeler import normalize_path

DEFAULT_LINE_WINDOW = 10  # mặc định tạm (R16)
FIX_LOOP_AGENT = "fix-loop"


class FixLoopError(ValueError):
    pass


def fingerprint(file: str, type_: str, line: int, *, window: int = DEFAULT_LINE_WINDOW) -> str:
    """Fingerprint ổn định của một finding: (file, loại, vùng dòng)."""
    if window < 1:
        raise FixLoopError("cửa sổ dòng phải >= 1")
    bucket = max(0, int(line)) // window
    key = f"{normalize_path(file)}\0{type_.strip().casefold()}\0{bucket}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


@dataclass
class FixDecision:
    task_id: str
    state: str  # "awaiting-human" | "escalated"
    fix_rounds: int
    reason: str | None
    fix_list: list[dict] = field(default_factory=list)
    recurring: list[str] = field(default_factory=list)


class FixLoop:
    def __init__(self, policy: FixPolicy, *, line_window: int = DEFAULT_LINE_WINDOW,
                 probe_task_id: str = DEFAULT_PROBE_TASK_ID, events=None,
                 timezone: str = "Asia/Seoul", clock: Callable[[], str] | None = None):
        if policy.max_fix_rounds < 0 or policy.budget_multiplier < 0:
            raise FixLoopError("max_fix_rounds và budget_multiplier phải >= 0")
        self.policy = policy
        self.line_window = line_window
        self.probe_task_id = probe_task_id
        self.events = events
        self.agent = getattr(events, "agent", FIX_LOOP_AGENT)
        tz = ZoneInfo(timezone)
        self._clock = clock or (lambda: datetime.now(tz).isoformat(timespec="milliseconds"))

    def build_fix_list(self, findings: Iterable[Mapping]) -> list[dict]:
        """Gộp finding theo fingerprint (cửa sổ dòng của vòng fix), thứ tự ổn định."""
        merged: dict[str, dict] = {}
        for f in findings:
            fp = fingerprint(f["file"], f["type"], f.get("line_start", 0), window=self.line_window)
            if fp not in merged:
                merged[fp] = {"fingerprint": fp, "file": f["file"], "type": f["type"],
                              "line_start": f.get("line_start", 0), "line_end": f.get("line_end", f.get("line_start", 0)),
                              "severity": f.get("severity"), "message": f.get("message", ""), "count": 0}
            merged[fp]["count"] += 1
        return sorted(merged.values(), key=lambda x: (normalize_path(x["file"]), x["line_start"], x["type"], x["fingerprint"]))

    def on_verdict(self, task: Mapping, verdict: Mapping, ledger_records: Iterable[Mapping], *,
                   previous_fingerprints: Iterable[str] = ()) -> FixDecision:
        """Verdict Gate 3 ``fail`` → fix list → ``awaiting-human`` (hoặc ``escalated``)."""
        if verdict.get("verdict") != "fail":
            raise FixLoopError(f"vòng fix chỉ nhận verdict fail, nhận {verdict.get('verdict')!r}")
        if verdict.get("task_id") != task["task_id"]:
            raise FixLoopError("verdict thuộc task khác")
        if task.get("head_sha") and verdict.get("sha") != task["head_sha"]:
            raise FixLoopError(f"verdict cho SHA {verdict.get('sha')} nhưng head của task là {task['head_sha']}")
        return self._round(task, verdict.get("findings") or [], ledger_records, previous_fingerprints,
                           sha=verdict.get("sha"), source="gate3")

    def on_ci_failure(self, task: Mapping, ci, ledger_records: Iterable[Mapping], *,
                      previous_fingerprints: Iterable[str] = ()) -> FixDecision:
        """CI đỏ (check run GitHub Actions) cũng là một vòng fix."""
        if ci.state != "failure":
            raise FixLoopError(f"chỉ CI failure vào vòng fix, nhận {ci.state!r}")
        finding = {"file": "<ci>", "type": "ci-failure", "line_start": 0, "severity": "blocker",
                   "message": f"check run {ci.check_name} kết luận {ci.conclusion}: {ci.html_url or ''}".strip()}
        return self._round(task, [finding], ledger_records, previous_fingerprints, sha=ci.sha, source="ci")

    def _round(self, task: Mapping, findings, ledger_records, previous_fingerprints, *, sha, source) -> FixDecision:
        task_id = check_task_id(task["task_id"], self.probe_task_id)
        fix_list = self.build_fix_list(findings)
        if not fix_list:
            raise FixLoopError("fix list rỗng")
        prev = set(previous_fingerprints)
        recurring = [f["fingerprint"] for f in fix_list if f["fingerprint"] in prev]
        for f in fix_list:
            f["recurring"] = f["fingerprint"] in prev
        costs = task_cost_by_attempt(ledger_records, task_id, self.probe_task_id)
        # Dự đoán bằng đúng state machine mà orchestrator dùng khi fold.
        preview = apply_transition(dict(task), "fix_list", actor=self.agent, now=self._clock(),
                                   policy=self.policy, cost_by_attempt=costs)
        escalated = preview["state"] == "escalated"
        decision = FixDecision(task_id, "escalated" if escalated else "awaiting-human", preview["fix_rounds"],
                               preview["escalation_reason"] if escalated else None, fix_list, recurring)
        if self.events is not None:
            payload = {"to": "fix_list", "source": source, "fix_list": fix_list, "fix_round": preview["fix_rounds"]}
            # Vượt N vòng/ngân sách: orchestrator tự chuyển fix_list → escalated khi fold
            # (cùng apply_transition), nên không ghi thêm event nào.
            self.events.emit("transition", task_id, payload, sha=sha)
            if not escalated:
                # Fixer là stub: fix list chờ người, không gọi Fixer/Cursor.
                self.events.emit("transition", task_id,
                                 {"to": "awaiting-human", "reason": "Fixer là stub: fix list chờ người dùng",
                                  "fix_list": fix_list}, sha=sha)
        return decision

    def open_fix_pr(self, task: Mapping, *, pr: int, sha: str | None) -> dict:
        """PR mới cho cùng ``task_id`` thay PR cũ: PR cũ thành ``superseded`` (R2)."""
        preview = start_new_attempt(dict(task), pr=pr, sha=sha, now=self._clock())
        if self.events is not None:
            kw = {"sha": sha} if sha else {}
            self.events.emit("new_attempt", task["task_id"], {"pr": pr}, **kw)
        return preview
