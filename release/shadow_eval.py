"""Shadow evaluator (PR-4a, R8, SPEC mục 7–8): CHỈ GHI "would-merge", KHÔNG BAO GIỜ MERGE.

- Đánh giá 4 điều kiện auto-merge ỨNG VIÊN cho PR Green ở SPEC mục 8. 4 điều kiện là
  **OPEN DISCUSSION**, chưa được duyệt; module này chỉ thu dữ liệu để người dùng quyết
  trước PR-4b. Auto-merge vẫn TẮT trong MVP: không có cờ, config hay đường code nào ở đây
  bật được merge.
- **Không có đường gọi merge.** Module không nhận client GitHub, không import module mạng,
  process hay git (``pipeline.github_api``, ``pipeline.labeler``, ``subprocess``...), và
  không gọi hàm nào có "merge" trong tên. Đầu vào là DỮ LIỆU đã thu sẵn (check run CI,
  verdict Gate 3, nhãn, review thread). Test AST và test mock trong
  ``tests/test_shadow_eval.py`` chứng minh điều này.
- Hiệu ứng DUY NHẤT: một bản ghi audit ``action="would-merge"``, ``result`` = ``yes`` hoặc
  ``no`` (kèm điều kiện nào đạt/không đạt), qua ``AuditLog.append`` của orchestrator.
  Đánh giá lại cùng SHA với cùng kết quả không ghi thêm (``dedup_key``).
- Fail-closed: thiếu dữ liệu, dữ liệu gắn SHA khác (R9), nhãn bị sửa (``label_id`` không
  khớp), verdict sai schema → điều kiện đó KHÔNG đạt.
- Kill-switch bật → dừng trước khi đánh giá và trước khi ghi audit.
- Ngưỡng shadow (2 tuần hoặc 20 PR) là mặc định tạm trong ``config/harness.yaml`` (R16).
  ``shadow_progress`` chỉ báo đã đủ dữ liệu để XEM XÉT hay chưa; nó không bật gì cả.

Điều kiện (tên ổn định, dùng trong audit):

1. ``ci_green``: check run ``ci`` của GitHub Actions kết luận ``success`` trên đúng SHA.
2. ``gate3_pass``: verdict Gate 3 đúng schema, đúng SHA, ``pass``, và review đúng nhãn này.
3. ``not_sensitive``: nhãn Green toàn phần, tính trên đúng SHA, không bị sửa.
4. ``no_unresolved_comments``: mọi review thread đã resolve, bất kể ai viết (SPEC mục 8
   câu 11). Không có dữ liệu thread → không đạt.

Thông tin thêm (KHÔNG ảnh hưởng ``would_merge``): ``alt_diff_limit`` đo phương án thay
điều kiện 3 bằng giới hạn diff (≤200 dòng / ≤10 file, có test khi sửa src — mặc định tạm).
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping
from zoneinfo import ZoneInfo

import yaml

from pipeline import common

SHADOW_AGENT = "shadow-eval"
AUDIT_ACTION = "would-merge"
YES, NO = "yes", "no"
CONDITIONS = ("ci_green", "gate3_pass", "not_sensitive", "no_unresolved_comments")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class ShadowError(ValueError):
    pass


@dataclass(frozen=True)
class ShadowSettings:
    # Mặc định tạm (R16, SPEC mục 8): 2 tuần hoặc 20 PR.
    min_days: float = 14
    min_prs: int = 20
    alt_max_lines: int = 200
    alt_max_files: int = 10

    @classmethod
    def load(cls, path: Path | None = None) -> "ShadowSettings":
        with open(Path(path) if path else common.DEFAULT_CONFIG_PATH, encoding="utf-8") as f:
            data = (yaml.safe_load(f) or {}).get("shadow") or {}
        s = cls(min_days=float(data.get("min_days", cls.min_days)), min_prs=int(data.get("min_prs", cls.min_prs)),
                alt_max_lines=int(data.get("alt_max_lines", cls.alt_max_lines)),
                alt_max_files=int(data.get("alt_max_files", cls.alt_max_files)))
        if s.min_days < 0 or s.min_prs < 0:
            raise ShadowError("shadow.min_days và shadow.min_prs phải >= 0")
        return s


@dataclass(frozen=True)
class ConditionResult:
    name: str
    passed: bool
    reason: str


@dataclass(frozen=True)
class ShadowDecision:
    pr: int
    sha: str
    would_merge: bool
    conditions: tuple[ConditionResult, ...]
    evaluated_at: str
    task_id: str | None = None
    attempt: int | None = None
    alt_diff_limit: dict | None = None

    def detail(self) -> dict:
        d = {
            "mode": "shadow",
            "auto_merge": "disabled",
            "conditions_status": "OPEN DISCUSSION (SPEC mục 8)",
            "evaluated_at": self.evaluated_at,
            "conditions": [asdict(c) for c in self.conditions],
            "failed": [c.name for c in self.conditions if not c.passed],
        }
        if self.alt_diff_limit is not None:
            d["alt_diff_limit"] = self.alt_diff_limit
        return d

    def dedup_key(self) -> str:
        vec = json.dumps([[c.name, c.passed] for c in self.conditions] + [self.would_merge, self.alt_diff_limit],
                         sort_keys=True, separators=(",", ":"))
        return f"shadow:{self.pr}:{self.sha}:{hashlib.sha256(vec.encode('utf-8')).hexdigest()[:16]}"


# --- đọc đầu vào (object hoặc mapping) ----------------------------------------------------


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _as_dict(obj: Any) -> dict | None:
    if obj is None:
        return None
    if isinstance(obj, Mapping):
        return dict(obj)
    to_dict = getattr(obj, "to_dict", None)
    return dict(to_dict()) if callable(to_dict) else None


def label_id_of(label: Mapping) -> str:
    """Tính lại ``label_id`` như ``pipeline.labeler`` (sha256 JSON chuẩn của payload)."""
    payload = {k: v for k, v in label.items() if k != "label_id"}
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# --- 4 điều kiện ứng viên -----------------------------------------------------------------


def check_ci(ci: Any, sha: str) -> ConditionResult:
    name = "ci_green"
    if ci is None:
        return ConditionResult(name, False, "không có trạng thái CI")
    if _get(ci, "sha") != sha:
        return ConditionResult(name, False, f"trạng thái CI của SHA {_get(ci, 'sha')!r}, không phải {sha}")
    state = _get(ci, "state")
    if state != "success":
        return ConditionResult(name, False, f"CI {state!r}, không phải success")
    return ConditionResult(name, True, f"check run {_get(ci, 'check_name', 'ci')} success "
                                       f"(id {_get(ci, 'check_run_id')})")


def check_gate3(verdict: Mapping | None, sha: str, label: Mapping | None) -> ConditionResult:
    name = "gate3_pass"
    if verdict is None:
        return ConditionResult(name, False, "chưa có verdict Gate 3")
    try:
        common.validate("verdict", verdict)
    except common.PipelineSchemaError as exc:
        return ConditionResult(name, False, f"verdict sai schema: {exc}"[:300])
    if verdict["sha"] != sha:
        return ConditionResult(name, False, f"verdict cho SHA {verdict['sha']}, không phải {sha}")
    if verdict["verdict"] != "pass":
        return ConditionResult(name, False, f"verdict {verdict['verdict']!r}")
    if label is not None and verdict["label"]["label_id"] != label.get("label_id"):
        return ConditionResult(name, False, "verdict review một nhãn khác nhãn hiện tại")
    return ConditionResult(name, True, "Gate 3 pass trên đúng SHA")


def check_not_sensitive(label: Mapping | None, sha: str) -> ConditionResult:
    name = "not_sensitive"
    if label is None:
        return ConditionResult(name, False, "chưa có nhãn")
    if label.get("head_sha") != sha:
        return ConditionResult(name, False, f"nhãn tính cho SHA {label.get('head_sha')!r}, không phải {sha}")
    if not isinstance(label.get("label_id"), str) or label["label_id"] != label_id_of(label):
        return ConditionResult(name, False, "label_id không khớp nội dung nhãn (nhãn bị sửa)")
    if label.get("tier") != "green":
        return ConditionResult(name, False, f"nhãn {label.get('tier')!r}")
    files = label.get("files") or ()
    if not files:
        return ConditionResult(name, False, "nhãn không có file nào")
    bad = [_get(f, "path") for f in files if _get(f, "tier") != "green"]
    if bad:
        return ConditionResult(name, False, f"file không Green: {bad[:5]}")
    return ConditionResult(name, True, f"Green toàn phần ({len(files)} file)")


def check_threads(review_threads: Iterable[Mapping] | None) -> ConditionResult:
    name = "no_unresolved_comments"
    if review_threads is None:
        return ConditionResult(name, False, "không có dữ liệu review thread")
    total, open_ = 0, 0
    for t in review_threads:
        total += 1
        resolved = _get(t, "is_resolved", _get(t, "isResolved"))
        if resolved is not True:  # thiếu cờ hay giá trị lạ → coi là chưa resolve
            open_ += 1
    if open_:
        return ConditionResult(name, False, f"{open_}/{total} review thread chưa resolve")
    return ConditionResult(name, True, f"{total} review thread, đều đã resolve")


def alt_diff_limit(diff_stats: Mapping | None, settings: ShadowSettings) -> dict | None:
    """Phương án thay điều kiện 3 (SPEC mục 8, mặc định tạm). Chỉ để thu dữ liệu."""
    if diff_stats is None:
        return None
    lines, files = diff_stats.get("lines"), diff_stats.get("files")
    touches_src, has_tests = diff_stats.get("touches_src"), diff_stats.get("has_tests")
    known = all(isinstance(x, int) and not isinstance(x, bool) for x in (lines, files)) and \
        all(isinstance(x, bool) for x in (touches_src, has_tests))
    passed = known and lines <= settings.alt_max_lines and files <= settings.alt_max_files and \
        (not touches_src or has_tests)
    return {"passed": bool(passed), "lines": lines, "files": files, "touches_src": touches_src,
            "has_tests": has_tests, "max_lines": settings.alt_max_lines, "max_files": settings.alt_max_files}


# --- evaluator ----------------------------------------------------------------------------


class ShadowEvaluator:
    """Đánh giá 4 điều kiện và ghi bản ghi ``would-merge``. Không có thao tác nào khác.

    ``audit``: object có ``append(**kw)`` (``orchestrator.audit.AuditLog`` của orchestrator
    đang giữ ``WriterGuard``). ``killswitch``: BẮT BUỘC, có ``check(action)``.
    """

    def __init__(self, *, audit, killswitch, settings: ShadowSettings | None = None,
                 timezone: str = "Asia/Seoul", clock: Callable[[], str] | None = None):
        if killswitch is None or not callable(getattr(killswitch, "check", None)):
            raise ShadowError("shadow evaluator cần kill-switch (fail-closed)")
        if audit is None:
            raise ShadowError("shadow evaluator cần audit log")
        self.audit = audit
        self.killswitch = killswitch
        self.settings = settings or ShadowSettings()
        tz = ZoneInfo(timezone)
        self._clock = clock or (lambda: datetime.now(tz).isoformat(timespec="milliseconds"))

    def evaluate(self, *, pr: int, sha: str, ci: Any = None, verdict: Mapping | None = None,
                 label: Any = None, review_threads: Iterable[Mapping] | None = None,
                 task_id: str | None = None, attempt: int | None = None,
                 diff_stats: Mapping | None = None) -> ShadowDecision:
        """Đánh giá thuần (không ghi gì)."""
        if isinstance(pr, bool) or not isinstance(pr, int) or pr < 1:
            raise ShadowError(f"pr phải là số nguyên >= 1, nhận {pr!r}")
        if not isinstance(sha, str) or not _SHA_RE.match(sha):
            raise ShadowError(f"SHA phải là 40 ký tự hex thường, nhận {sha!r}")
        lbl = _as_dict(label)
        conds = (
            check_ci(ci, sha),
            check_gate3(verdict, sha, lbl),
            check_not_sensitive(lbl, sha),
            check_threads(None if review_threads is None else list(review_threads)),
        )
        return ShadowDecision(pr=pr, sha=sha, would_merge=all(c.passed for c in conds), conditions=conds,
                              evaluated_at=self._clock(), task_id=task_id, attempt=attempt,
                              alt_diff_limit=alt_diff_limit(diff_stats, self.settings))

    def record(self, decision: ShadowDecision) -> dict | None:
        """Ghi bản ghi ``would-merge`` vào audit. ``None`` nếu đã ghi đúng kết quả này."""
        self.killswitch.check("shadow_eval.record")
        return self.audit.append(
            action=AUDIT_ACTION, result=YES if decision.would_merge else NO, agent=SHADOW_AGENT,
            served_model=None, vendor=None, model_id_source=None, sha=decision.sha,
            task_id=decision.task_id, attempt=decision.attempt, pr=decision.pr,
            detail=decision.detail(), dedup_key=decision.dedup_key(),
        )

    def run(self, **inputs) -> ShadowDecision:
        """Kill-switch → đánh giá → ghi audit. Trả quyết định (không merge gì cả)."""
        self.killswitch.check("shadow_eval.run")
        decision = self.evaluate(**inputs)
        self.record(decision)
        return decision


def shadow_progress(audit_records: Iterable[Mapping], *, now: datetime,
                    settings: ShadowSettings | None = None) -> dict:
    """Tiến độ shadow từ audit: số ngày từ bản ghi đầu và số PR khác nhau đã đánh giá.

    ``threshold_reached`` chỉ nghĩa là đủ dữ liệu để người dùng XEM XÉT 4 điều kiện;
    ``auto_merge`` luôn là ``disabled`` trong PR-4a.
    """
    s = settings or ShadowSettings()
    recs = [r for r in audit_records if r.get("action") == AUDIT_ACTION and r.get("agent") == SHADOW_AGENT]
    prs = {r.get("pr") for r in recs if r.get("pr") is not None}
    started = min((datetime.fromisoformat(r["ts"]) for r in recs), default=None)
    days = (now - started).total_seconds() / 86400 if started is not None else 0.0
    return {
        "started_at": started.isoformat(timespec="milliseconds") if started else None,
        "days": round(days, 3), "prs": len(prs), "records": len(recs),
        "would_merge_yes": sum(1 for r in recs if r.get("result") == YES),
        "min_days": s.min_days, "min_prs": s.min_prs,
        "threshold_reached": bool(recs) and (days >= s.min_days or len(prs) >= s.min_prs),
        "auto_merge": "disabled",
    }
