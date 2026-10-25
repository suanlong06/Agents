"""Event fold idempotent — orchestrator là writer duy nhất (PR-1a, R3).

Vòng fold:
1. Đọc mỗi nguồn JSONL từ offset đã lưu; chỉ lấy dòng hoàn chỉnh (dòng cuối ghi
   dở bị bỏ qua, offset dừng trước nó để lần sau đọc lại).
2. Dòng hỏng hoặc sai schema → cách ly vào ``quarantine`` của snapshot, fold đi tiếp.
3. Event id ``(agent, pid, proc_start, seq)``: seq <= watermark của
   ``agent|pid|proc_start`` thì không gây thêm hiệu ứng.
4. Áp event lên bản sao snapshot; hiệu ứng phụ (ledger, audit) idempotent theo key.
5. Ghi snapshot (tasks + offsets + watermarks) trong MỘT lần ghi nguyên tử. Ghi
   thất bại thì offset không tiến, health = error, ném ``StateWriteError``.

Crash trước khi ghi snapshot: lần chạy sau áp lại event lên snapshot cũ; ledger
và audit bỏ qua bản ghi đã có theo key → không áp hai lần, không cộng đôi.
Crash sau khi ghi snapshot: watermark/offset đã tiến → event không được áp lại.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from . import schemas
from .audit import AuditLog
from .config import HarnessConfig
from .fsio import StateWriteError, WriterGuard, iter_complete_lines
from .ledger import Ledger, LedgerConflict, PriceTable
from .state import (
    InvalidTransition,
    StateStore,
    apply_transition,
    new_task,
    start_new_attempt,
)

ORCHESTRATOR_AGENT = "orchestrator"


@dataclass
class FoldResult:
    applied: int = 0
    duplicates: int = 0
    rejected: int = 0
    quarantined: int = 0
    pending_partial: list[str] = field(default_factory=list)
    # Nguồn không đọc được từ offset đã lưu (ví dụ file bị cắt ngắn): bị bỏ qua ở
    # lần fold này, offset giữ nguyên, health = error.
    source_errors: dict[str, str] = field(default_factory=dict)


def event_source_key(ev: dict) -> str:
    return f"{ev['agent']}|{ev['pid']}|{ev['proc_start']}"


def event_id(ev: dict) -> str:
    return f"{event_source_key(ev)}|{ev['seq']}"


class Orchestrator:
    """Mặt tiền ghi duy nhất cho state, ledger và audit trong một ``state_dir``."""

    def __init__(self, config: HarnessConfig, *, clock: Callable[[], str] | None = None):
        self.config = config
        self.state_dir = config.state_dir
        self._clock = clock or (lambda: config.now().isoformat(timespec="milliseconds"))
        self.guard = WriterGuard(self.state_dir)
        self.health: dict = {"status": "ok", "last_error": None}
        # PR-1b sẽ nối hook này vào heartbeat của orchestrator.
        self.on_write_failure: Callable[[BaseException], None] | None = None
        self.store: StateStore | None = None
        self.ledger: Ledger | None = None
        self.audit: AuditLog | None = None
        self.snapshot: dict | None = None

    # --- vòng đời -----------------------------------------------------------

    def open(self) -> "Orchestrator":
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.guard.acquire()
        try:
            self.store = StateStore(self.state_dir, self.guard, self.config.write_retry)
            self.snapshot = self.store.load()
            prices = PriceTable.load(self.config.prices_path)
            self.ledger = Ledger(self.state_dir, prices, self.guard, self._clock)
            self.audit = AuditLog(self.state_dir, self.guard, self._clock)
        except BaseException:
            self.guard.release()
            raise
        return self

    def close(self) -> None:
        self.guard.release()

    def __enter__(self) -> "Orchestrator":
        return self.open()

    def __exit__(self, *exc) -> None:
        self.close()

    # --- fold ---------------------------------------------------------------

    def _source_name(self, path: Path) -> str:
        path = Path(path).resolve()
        try:
            return path.relative_to(self.state_dir).as_posix()
        except ValueError:
            raise ValueError(f"nguồn event {path} phải nằm trong state_dir {self.state_dir}") from None

    def fold(self, sources: Iterable[Path]) -> FoldResult:
        self.guard.ensure_held()
        result = FoldResult()
        working = copy.deepcopy(self.snapshot)
        for src in sources:
            name = self._source_name(src)
            offset = working["offsets"].get(name, 0)
            try:
                lines, new_offset = iter_complete_lines(Path(src), offset)
            except ValueError as exc:
                # File ngắn hơn offset đã lưu (bị cắt hoặc thay thế). Không crash-loop
                # với health ok: bỏ qua nguồn này, giữ offset, báo lỗi lên health.
                result.source_errors[name] = str(exc)
                self._report_source_error(name, offset, exc)
                continue
            if Path(src).exists() and Path(src).stat().st_size > new_offset:
                result.pending_partial.append(name)
            for line_off, raw in lines:
                if not raw.strip():
                    continue
                try:
                    ev = self._parse_event(raw)
                except (schemas.SchemaError, ValueError) as exc:
                    self._quarantine(working, name, line_off, raw, str(exc))
                    result.quarantined += 1
                    continue
                self._apply(working, ev, result)
            working["offsets"][name] = new_offset

        working["as_of"] = self._clock()
        working["fold_count"] += 1
        try:
            self.store.save(working)
        except StateWriteError as exc:
            self._report_write_failure(exc)
            raise
        self.snapshot = working
        if result.source_errors:
            name, err = next(iter(result.source_errors.items()))
            self.health = {"status": "error", "last_error": f"nguồn {name}: {err}"}
        else:
            self.health = {"status": "ok", "last_error": None}
        return result

    def _report_source_error(self, name: str, offset: int, exc: BaseException) -> None:
        # dedup_key theo (nguồn, offset): fold lặp lại không ghi thêm bản ghi audit.
        try:
            self.audit.append(
                action="fold.source", result="error", agent=ORCHESTRATOR_AGENT,
                served_model=None, vendor=None, model_id_source=None, sha=None,
                task_id=None, attempt=None, detail={"source": name, "offset": offset, "error": str(exc)},
                dedup_key=f"source_error:{name}:{offset}",
            )
        except OSError:
            pass

    def _report_write_failure(self, exc: BaseException) -> None:
        # Không bao giờ im lặng: health = error, ghi audit, gọi hook heartbeat.
        self.health = {"status": "error", "last_error": str(exc)}
        try:
            self.audit.append(
                action="state.save", result="error", agent=ORCHESTRATOR_AGENT,
                served_model=None, vendor=None, model_id_source=None, sha=None,
                task_id=None, attempt=None, detail={"error": str(exc)},
            )
        except OSError:
            pass
        if self.on_write_failure:
            self.on_write_failure(exc)

    @staticmethod
    def _parse_event(raw: bytes) -> dict:
        try:
            ev = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"JSON hỏng: {exc}") from exc
        if not isinstance(ev, dict):
            raise ValueError("event không phải object")
        schemas.check_version("event", ev.get("v"))
        schemas.validate("event", ev)
        return ev

    def _quarantine(self, working: dict, source: str, offset: int, raw: bytes, error: str) -> None:
        cap = self.config.quarantine_max_raw_bytes
        working["quarantine"].append({
            "source": source,
            "offset": offset,
            "error": error[:500],
            "raw": raw[:cap].decode("utf-8", errors="replace"),
            "ts": self._clock(),
        })

    def _apply(self, working: dict, ev: dict, result: FoldResult) -> None:
        src_key = event_source_key(ev)
        if ev["seq"] <= working["watermarks"].get(src_key, 0):
            result.duplicates += 1  # đã áp: không thêm hiệu ứng
            return
        working["watermarks"][src_key] = ev["seq"]

        tasks = working["tasks"]
        task_id = ev["task_id"]
        p = ev["payload"]
        now = ev["ts"]
        outcome, detail = "applied", None
        try:
            if ev["type"] == "task_created":
                if task_id in tasks:
                    raise InvalidTransition(f"task {task_id} đã tồn tại")
                tasks[task_id] = new_task(
                    task_id, kind=p.get("kind", "feature"), title=p.get("title", ""),
                    parent_pr=p.get("parent_pr"), now=now,
                )
            elif ev["type"] == "transition":
                task = self._require_task(tasks, task_id)
                tasks[task_id] = apply_transition(
                    task, p["to"], actor=ev["agent"], now=now, policy=self.config.fix,
                    cost_by_attempt=self.ledger.cost_by_attempt(task_id),
                    pr=p.get("pr"), sha=ev.get("sha"), reason=p.get("reason"),
                )
                if tasks[task_id]["state"] != p["to"]:
                    detail = {"requested": p["to"], "effective": tasks[task_id]["state"],
                              "reason": tasks[task_id]["escalation_reason"]}
            elif ev["type"] == "new_attempt":
                task = self._require_task(tasks, task_id)
                tasks[task_id] = start_new_attempt(task, pr=p["pr"], sha=ev.get("sha"), now=now)
            elif ev["type"] == "model_call":
                status, _ = self.ledger.record(
                    task_id=task_id, attempt=p["attempt"], call_id=p["call_id"], agent=ev["agent"],
                    served_model=ev["model_id"], vendor=ev.get("vendor"),
                    model_id_source=ev["model_id_source"], requested_model=p.get("requested_model"),
                    fallback_reason=p.get("fallback_reason"), effort=p.get("effort"),
                    input_tokens=p["input_tokens"], output_tokens=p["output_tokens"],
                    estimated=p["estimated"],
                )
                outcome = "applied" if status == "recorded" else "duplicate_call"
        except (InvalidTransition, LedgerConflict) as exc:
            outcome, detail = "rejected", {"error": str(exc)}
            result.rejected += 1
        else:
            result.applied += 1

        task = tasks.get(task_id)
        attempt = p.get("attempt") if ev["type"] == "model_call" else (task or {}).get("attempt")
        self.audit.append(
            action=f"event.{ev['type']}", result=outcome, agent=ev["agent"],
            served_model=ev.get("model_id"),
            vendor=ev.get("vendor") or self.ledger.prices.vendor(ev.get("model_id")),
            model_id_source=ev.get("model_id_source"),
            sha=ev.get("sha") or (task or {}).get("head_sha"),
            task_id=task_id, attempt=attempt, pr=(task or {}).get("pr"),
            detail=detail, dedup_key=f"event:{event_id(ev)}",
        )

    @staticmethod
    def _require_task(tasks: dict, task_id: str) -> dict:
        if task_id not in tasks:
            raise InvalidTransition(f"task {task_id} chưa tồn tại")
        return tasks[task_id]
