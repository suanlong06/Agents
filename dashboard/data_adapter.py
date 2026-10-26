"""Data adapter của dashboard (PR-5, SPEC mục 6), CHỈ ĐỌC.

Luồng dữ liệu (snapshot handoff, xem ``dashboard/views/00_snapshot_handoff.md``)::

    state_dir (máy này) --đọc--> data_adapter.py --> snapshot JSON --> artifact HTML đã dựng
                                                                          | chuyển sang Muse VM
                                                                          v
                                                                 Muse Artifact (chỉ xem)

- Muse Artifact được host trên Muse VM, KHÔNG đọc được ``state_dir`` cục bộ của máy này.
  Dashboard chỉ thấy snapshot được chuyển sang VM; làm mới = xuất một snapshot mới.
- **Chỉ đọc ``state_dir``**: module chỉ mở file state ở chế độ đọc và chỉ dùng các hàm
  đọc tĩnh (``read_snapshot``, ``read_jsonl``, ``read_status``...). Không dựng
  ``Orchestrator``/``Ledger``/``AuditLog``/``ApprovalMirror``: constructor của chúng đòi
  ``WriterGuard`` và có thể ghi (đóng dòng ghi dở). Thao tác ghi DUY NHẤT là
  ``_write_output``: ghi snapshot/HTML ra file người dùng chỉ định, từ chối mọi đường dẫn
  nằm trong ``state_dir`` hoặc trong repo (R13).
- **Provenance**: mỗi view có file nguồn, số bản ghi và ``as_of`` = thời điểm fold cuối
  cùng (``state.json``). ``as_of`` không bao giờ là giờ hiện tại hay chuỗi "now": giá trị
  không phải thời điểm ISO 8601 có múi giờ bị loại (``as_of = null`` kèm lỗi).
- **"Không có dữ liệu" khác "0"**: file nguồn không có → ``records = null``,
  ``status = "no-data"``; file có nhưng rỗng → ``records = 0``.
- **Watermark FAKE** lấy từ trường dữ liệu ``source`` của snapshot, không từ CSS.
  ``state.json`` mang ``source: "fixture"``, người gọi đánh dấu fixture, hoặc ``state_dir``
  nằm trong repo (runtime state không bao giờ ở trong repo, R13) thì snapshot là
  ``fixture``; không có cách hạ ``fixture`` về ``runtime``. Trang dựng ra gắn watermark
  cho mọi ``source`` khác ``runtime``.
- Không endpoint, không server, không nút: trang dựng ra là HTML tĩnh KHÔNG có JavaScript.
  Duyệt diễn ra trên GitHub; kill-switch chỉ để xem (SPEC mục 5.2, 6).
- Nhãn Red/Owner-only/Green và trạng thái CI không có file state riêng: nhãn lấy từ
  verdict Gate 3 trong event đã fold, CI chỉ biết ``failure`` từ event vòng fix
  ``source: "ci"``. Còn lại là "không có dữ liệu" kèm link sang check run trên GitHub;
  dashboard không tự suy ra CI xanh.
- Best-effort, không chống giả mạo (R15): snapshot chỉ phản chiếu ``state_dir`` lúc xuất.

CLI::

    python -m dashboard.data_adapter snapshot [--state-dir D] [--out snap.json] [--fixture]
    python -m dashboard.data_adapter render [--snapshot snap.json | --state-dir D] --out page.html [--fixture]
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Mapping
from urllib.parse import quote
from zoneinfo import ZoneInfo

import yaml

from orchestrator import schemas
from orchestrator.audit import AuditLog
from orchestrator.config import DEFAULT_CONFIG_PATH, REPO_ROOT, load_config
from orchestrator.fsio import read_jsonl
from orchestrator.heartbeat import SUMMARY_FILE
from orchestrator.heartbeat import validate as validate_heartbeat
from orchestrator.inbox import discover_sources
from orchestrator.killswitch import killswitch_path, read_status
from orchestrator.ledger import Ledger
from orchestrator.state import EXTENDED_STATES, STEP_STATES, StateStore, read_snapshot
from pipeline import common as pipeline_common
from pipeline.approvals import ApprovalMirror

SNAPSHOT_KIND = "harness-dashboard-snapshot"
SNAPSHOT_VERSION = 1
RUNTIME, FIXTURE = "runtime", "fixture"
DASHBOARD_DIR = Path(__file__).resolve().parent
TEMPLATE_PATH = DASHBOARD_DIR / "artifact.html"
SCHEMA_PATH = DASHBOARD_DIR / "snapshot.schema.json"

# Số dòng log đưa vào snapshot (mặc định tạm, R16): snapshot phải đủ nhỏ để chuyển sang VM.
AUDIT_TAIL = 200
AGENT_LOG_TAIL = 100
QUARANTINE_TAIL = 20
MAX_TEXT = 500

VIEW_ORDER = ("kanban", "pr_queue", "approvals", "agents", "logs", "costs")
VIEW_TITLES = {
    "kanban": "Task kanban",
    "pr_queue": "PR queue",
    "approvals": "Approval queue",
    "agents": "Agent status",
    "logs": "Logs",
    "costs": "Token cost theo task",
}
# Task đang chờ người dùng (bước 3: duyệt spec; bước 9: người dùng là người merge duy nhất).
APPROVAL_KINDS = {
    "spec_review": "spec",
    "approved_merge": "pr",
    "escalated": "escalated",
    "awaiting-human": "awaiting-human",
}
EVENTS_LABEL = "inbox/<agent>/events-*.jsonl"
GITHUB = "https://github.com/"
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
# Cùng pattern với ``$defs/as_of`` trong snapshot.schema.json.
_AS_OF_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?([+-]\d{2}:\d{2}|Z)$")
_UNSET = object()


class AdapterError(ValueError):
    pass


# --- tiện ích ---------------------------------------------------------------------------


def _within(child: Path, parent: Path) -> bool:
    c = os.path.normcase(os.path.realpath(child))
    p = os.path.normcase(os.path.realpath(parent))
    return c == p or c.startswith(p.rstrip("\\/") + os.sep)


def _err(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:MAX_TEXT]


def _short(x: object, n: int = MAX_TEXT) -> str | None:
    if x is None:
        return None
    s = x if isinstance(x, str) else json.dumps(x, ensure_ascii=False, sort_keys=True)
    return s if len(s) <= n else s[: n - 1] + "…"


def valid_as_of(value: object) -> tuple[str | None, str | None]:
    """``(as_of, lỗi)``. Chỉ nhận thời điểm ISO 8601 có múi giờ do lần fold cuối ghi."""
    if value is None:
        return None, None
    if not isinstance(value, str) or not _AS_OF_RE.match(value):
        return None, f"as_of không phải thời điểm ISO 8601 có múi giờ: {value!r}"[:200]
    try:
        datetime.fromisoformat(value)
    except ValueError:
        return None, f"as_of không phải thời điểm hợp lệ: {value!r}"[:200]
    return value, None


def _source(name: str, *, present: bool, records: int | None = None, skipped: int = 0,
            error: str | None = None) -> dict:
    return {"file": name, "present": present, "records": records, "skipped": skipped, "error": error}


def _provenance(sources: list[dict], records: int | None, as_of: str | None) -> dict:
    if any(s["error"] for s in sources):
        status = "error"
    elif not any(s["present"] for s in sources):
        status = "no-data"
    elif not all(s["present"] for s in sources):
        status = "partial"
    else:
        status = "ok"
    return {"sources": sources, "records": None if status == "no-data" else records,
            "as_of": as_of, "status": status}


def _pr_url(repo: str | None, pr: object) -> str | None:
    if not repo or isinstance(pr, bool) or not isinstance(pr, int) or pr < 1:
        return None
    return f"{GITHUB}{repo}/pull/{pr}"


def _search_url(repo: str | None, task_id: str) -> str | None:
    return f"{GITHUB}{repo}/pulls?q={quote('is:pr ' + task_id)}" if repo else None


def _github_url(url: object) -> str | None:
    """Chỉ giữ link https://github.com/... (dữ liệu phản chiếu là dữ liệu không tin cậy)."""
    return url if isinstance(url, str) and url.startswith(GITHUB) and '"' not in url else None


# --- đọc state_dir (chỉ đọc) ---------------------------------------------------------------


def _read_yaml(path: Path) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except (OSError, yaml.YAMLError):
        return {}
    return data if isinstance(data, dict) else {}


def _load_state(state_dir: Path) -> tuple[dict | None, dict]:
    path = state_dir / StateStore.FILE
    if not path.exists():
        return None, _source(StateStore.FILE, present=False)
    try:
        state = read_snapshot(path)
    except (OSError, ValueError) as exc:
        return None, _source(StateStore.FILE, present=True, error=_err(exc))
    return state, _source(StateStore.FILE, present=True, records=len(state["tasks"]))


def _load_jsonl(state_dir: Path, name: str, check: Callable[[dict], None]) -> tuple[list[dict] | None, dict]:
    """Bản ghi hợp lệ của một file JSONL; ``None`` khi file không có (khác danh sách rỗng)."""
    path = state_dir / name
    if not path.exists():
        return None, _source(name, present=False)
    recs, skipped = [], 0
    try:
        for _off, obj, _raw in read_jsonl(path):
            if obj is None:
                skipped += 1
                continue
            try:
                check(obj)
            except ValueError:
                skipped += 1
                continue
            recs.append(obj)
    except (OSError, ValueError) as exc:
        return None, _source(name, present=True, error=_err(exc))
    return recs, _source(name, present=True, records=len(recs), skipped=skipped)


def _check_ledger(obj: dict) -> None:
    schemas.validate("ledger", obj)  # đúng tiêu chí của Ledger.read


def _check_audit(obj: dict) -> None:
    if isinstance(obj.get("seq"), bool) or not isinstance(obj.get("seq"), int):
        raise ValueError("bản ghi audit thiếu seq")  # đúng tiêu chí của AuditLog.read


def _check_approval(obj: dict) -> None:
    pipeline_common.validate("approval", obj)  # đúng tiêu chí của ApprovalMirror.read


def _event_sources(state_dir: Path, state: dict | None) -> list[str]:
    """Nguồn event: các file đã fold (``offsets`` của state) cộng bố cục inbox chuẩn."""
    names = {p.relative_to(state_dir).as_posix() for p in discover_sources(state_dir)}
    for name in (state or {}).get("offsets") or {}:
        p = state_dir / name
        if _within(p, state_dir) and p.is_file():
            names.add(name)
    return sorted(names)


def _load_events(state_dir: Path, state: dict | None) -> tuple[list[dict] | None, dict]:
    names = _event_sources(state_dir, state)
    if not names:
        info = _source(EVENTS_LABEL, present=False)
        info["files"] = []
        return None, info
    offsets = (state or {}).get("offsets") or {}
    events, skipped, errors = [], 0, []
    for name in names:
        try:
            for off, obj, _raw in read_jsonl(state_dir / name):
                try:
                    if obj is None:
                        raise ValueError("JSON hỏng")
                    schemas.validate("event", obj)
                except ValueError:
                    skipped += 1
                    continue
                # Dòng bắt đầu trước offset đã lưu thì đã nằm trong lần fold cuối.
                events.append({"source": name, "offset": off, "folded": off < offsets.get(name, 0), "event": obj})
        except (OSError, ValueError) as exc:
            errors.append(f"{name}: {_err(exc)}")
    info = _source(EVENTS_LABEL, present=True, records=len(events), skipped=skipped,
                   error=("; ".join(errors))[:MAX_TEXT] or None)
    info["files"] = names
    return events, info


def _load_heartbeats(state_dir: Path) -> tuple[dict | None, dict]:
    path = state_dir / SUMMARY_FILE
    if not path.exists():
        return None, _source(SUMMARY_FILE, present=False)
    try:
        data = json.loads(path.read_bytes().decode("utf-8"))
        validate_heartbeat(data)
        if data.get("kind") != "harness-heartbeats":
            raise ValueError("không phải bản tổng hợp heartbeats của orchestrator")
    except (OSError, ValueError) as exc:
        return None, _source(SUMMARY_FILE, present=True, error=_err(exc))
    return data, _source(SUMMARY_FILE, present=True, records=len(data["agents"]))


# --- view -------------------------------------------------------------------------------


def _task_card(t: Mapping) -> dict:
    keys = ("task_id", "kind", "title", "state", "attempt", "pr", "parent_pr", "head_sha", "fix_rounds",
            "resume_state", "escalation_reason", "updated_at")
    return {k: t.get(k) for k in keys}


def _sorted_tasks(state: dict | None) -> list[dict]:
    if state is None:
        return []
    return sorted(state["tasks"].values(), key=lambda t: (str(t.get("updated_at") or ""), t["task_id"]), reverse=True)


def kanban_view(state: dict | None, state_info: dict, as_of: str | None) -> dict:
    columns = [{"state": s, "group": "step", "step": n, "tasks": []} for n, s in STEP_STATES.items()]
    columns += [{"state": s, "group": "extended", "step": None, "tasks": []} for s in EXTENDED_STATES]
    by_state = {c["state"]: c for c in columns}
    # Trạng thái lạ (state bản sau, N+1) vẫn hiện, không bị nuốt.
    unknown = {"state": "unknown", "group": "unknown", "step": None, "tasks": []}
    for t in _sorted_tasks(state):
        by_state.get(t["state"], unknown)["tasks"].append(_task_card(t))
    if unknown["tasks"]:
        columns.append(unknown)
    for c in columns:
        c["count"] = None if state is None else len(c["tasks"])
    records = None if state is None else len(state["tasks"])
    return {"provenance": _provenance([state_info], records, as_of), "columns": columns}


def _folded(events: list[dict] | None) -> list[dict]:
    return [e for e in events or [] if e["folded"]]


def _verdicts(events: list[dict] | None) -> dict[tuple[str, str], dict]:
    """Verdict Gate 3 hợp lệ trong event đã fold, theo ``(task_id, sha)``; bản sau thắng."""
    out: dict[tuple[str, str], dict] = {}
    for e in _folded(events):
        ev = e["event"]
        verdict = (ev.get("payload") or {}).get("verdict")
        if ev["type"] != "transition" or not isinstance(verdict, dict):
            continue
        try:
            pipeline_common.validate("verdict", verdict)
        except ValueError:
            continue
        out[(verdict["task_id"], verdict["sha"])] = {**verdict, "_source": e["source"]}
    return out


def _ci_failures(events: list[dict] | None) -> dict[tuple[str, str], dict]:
    """CI đỏ được vòng fix ghi lại (``fix_list`` với ``source: "ci"``), theo ``(task_id, sha)``."""
    out: dict[tuple[str, str], dict] = {}
    for e in _folded(events):
        ev, p = e["event"], e["event"].get("payload") or {}
        if ev["type"] == "transition" and p.get("to") == "fix_list" and p.get("source") == "ci" and ev.get("sha"):
            msg = next((f.get("message") for f in p.get("fix_list") or [] if isinstance(f, dict)), None)
            out[(ev["task_id"], ev["sha"])] = {"state": "failure", "basis": f"event vòng fix source=ci ({e['source']})",
                                               "detail": _short(msg)}
    return out


def pr_queue_view(state: dict | None, state_info: dict, events: list[dict] | None, events_info: dict,
                  repo: str | None, as_of: str | None) -> dict:
    verdicts, ci = _verdicts(events), _ci_failures(events)
    latest_by_task: dict[str, dict] = {}
    for (task_id, _sha), v in verdicts.items():
        latest_by_task[task_id] = v
    items = []
    for t in _sorted_tasks(state):
        rows = []
        if t.get("pr"):
            rows.append((t["attempt"], t["pr"], t.get("head_sha"), t["state"], True))
        for a in t.get("attempts") or []:
            if a.get("pr"):
                rows.append((a["attempt"], a["pr"], a.get("head_sha"), a.get("final_state"), False))
        for attempt, pr, sha, st, current in rows:
            label = None
            v = verdicts.get((t["task_id"], sha)) if sha else None
            if v is not None:
                label = {"tier": v["label"]["tier"], "sha": v["sha"], "valid": True, "verdict": v["verdict"],
                         "basis": f"verdict Gate 3 ({v['_source']})"}
            elif current and t["task_id"] in latest_by_task:
                old = latest_by_task[t["task_id"]]
                # Nhãn chỉ có hiệu lực cho đúng head SHA của nó (R9): SHA khác thì là nhãn cũ.
                label = {"tier": old["label"]["tier"], "sha": old["sha"], "valid": False, "verdict": old["verdict"],
                         "basis": f"verdict Gate 3 cho SHA khác ({old['_source']})"}
            items.append({
                "task_id": t["task_id"], "title": t.get("title"), "attempt": attempt, "pr": pr, "head_sha": sha,
                "state": st, "current": current, "label": label,
                "ci": ci.get((t["task_id"], sha)) if sha else None,
                "url": _pr_url(repo, pr), "checks_url": (_pr_url(repo, pr) + "/checks") if _pr_url(repo, pr) else None,
            })
    items.sort(key=lambda i: (not i["current"], -int(i["pr"])))
    records = None if state is None else len(items)
    return {"provenance": _provenance([state_info, events_info], records, as_of), "items": items}


def approvals_view(state: dict | None, state_info: dict, approvals: list[dict] | None, approvals_info: dict,
                   repo: str | None, as_of: str | None) -> dict:
    by_pr: dict[int, list[dict]] = defaultdict(list)
    for r in approvals or []:
        by_pr[r["pr"]].append(r)
    items = []
    for t in _sorted_tasks(state):
        kind = APPROVAL_KINDS.get(t["state"])
        if kind is None:
            continue
        pr = t.get("pr")
        latest = max(by_pr.get(pr, []), key=lambda r: (r.get("submitted_at") or "", r["review_id"]), default=None)
        review = None
        if latest is not None:
            review = {"state": latest["state"], "reviewer": latest["reviewer"], "commit_id": latest["commit_id"],
                      "submitted_at": latest.get("submitted_at"),
                      "matches_head": latest["commit_id"] == t.get("head_sha"),
                      "html_url": _github_url(latest.get("html_url"))}
        items.append({
            "kind": kind, "task_id": t["task_id"], "title": t.get("title"), "state": t["state"], "pr": pr,
            "head_sha": t.get("head_sha"), "reason": t.get("escalation_reason"),
            "url": _pr_url(repo, pr) or _search_url(repo, t["task_id"]), "latest_review": review,
        })
    records = None if state is None else len(items)
    return {"provenance": _provenance([state_info, approvals_info], records, as_of), "items": items}


def agents_view(summary: dict | None, info: dict, as_of: str | None) -> dict:
    keys = ("agent", "status", "reason", "pid", "proc_start", "task_id", "attempt", "model_id", "ts", "age_s",
            "last_error", "source")
    agents = [{k: a.get(k) for k in keys} for a in (summary or {}).get("agents", [])]
    return {
        "provenance": _provenance([info], None if summary is None else len(agents), as_of),
        "orchestrator": None if summary is None else summary["orchestrator"],
        "timeout_s": None if summary is None else summary["timeout_s"],
        # Lúc orchestrator publish heartbeats.json; age_s tính tại lúc đó, không tính lại theo giờ hiện tại.
        "published_at": None if summary is None else summary["as_of"],
        "agents": agents,
    }


def _event_row(e: dict) -> dict:
    ev = e["event"]
    return {"seq": ev["seq"], "ts": ev["ts"], "type": ev["type"], "task_id": ev["task_id"],
            "model_id": ev.get("model_id"), "sha": ev.get("sha"), "source": e["source"], "folded": e["folded"],
            "payload": _short(ev.get("payload"))}


def logs_view(audit: list[dict] | None, audit_info: dict, events: list[dict] | None, events_info: dict,
              state: dict | None, as_of: str | None) -> dict:
    by_agent: dict[str, list[dict]] = defaultdict(list)
    for e in events or []:
        by_agent[e["event"]["agent"]].append(e)
    agents = {
        name: {"count": len(evs), "folded": sum(1 for e in evs if e["folded"]),
               "tail": [_event_row(e) for e in evs[-AGENT_LOG_TAIL:]]}
        for name, evs in sorted(by_agent.items())
    }
    audit_keys = ("seq", "ts", "action", "result", "agent", "served_model", "task_id", "attempt", "pr", "sha")
    audit_tail = [{**{k: r.get(k) for k in audit_keys}, "detail": _short(r.get("detail"))}
                  for r in (audit or [])[-AUDIT_TAIL:]]
    quarantine = None if state is None else state.get("quarantine") or []
    present = [n for n in (audit, events) if n is not None]
    records = sum(len(n) for n in present) if present else None
    return {
        "provenance": _provenance([audit_info, events_info], records, as_of),
        "audit": {"count": None if audit is None else len(audit), "tail": audit_tail},
        "agents": agents,
        "quarantine": None if quarantine is None else {
            "count": len(quarantine),
            "tail": [{"source": q.get("source"), "offset": q.get("offset"), "error": _short(q.get("error")),
                      "ts": q.get("ts")} for q in quarantine[-QUARANTINE_TAIL:]],
        },
    }


def ledger_totals(records: Iterable[Mapping]) -> dict:
    """Cùng công thức với ``Ledger.totals`` (contract test: dashboard = ledger)."""
    recs = list(records)
    priced = [r["cost_usd"] for r in recs if r["cost_usd"] is not None]
    return {
        "records": len(recs),
        "input_tokens": sum(r["input_tokens"] for r in recs),
        "output_tokens": sum(r["output_tokens"] for r in recs),
        "cost_usd": round(sum(priced), 9),
        "unpriced_calls": len(recs) - len(priced),
        "estimated_calls": sum(1 for r in recs if r["estimated"]),
    }


def costs_view(ledger: list[dict] | None, info: dict, as_of: str | None) -> dict:
    if ledger is None:
        return {"provenance": _provenance([info], None, as_of), "tasks": [], "totals": None, "duplicates": None}
    # Như Ledger: key idempotent (task_id, call_id, attempt), bản ghi đầu tiên thắng.
    unique: dict[tuple, dict] = {}
    for r in ledger:
        unique.setdefault(Ledger.key(r), r)
    by_task: dict[str, list[dict]] = defaultdict(list)
    for r in unique.values():
        by_task[r["task_id"]].append(r)
    tasks = []
    for task_id, recs in sorted(by_task.items()):
        row = {"task_id": task_id, **ledger_totals(recs)}
        by_attempt: dict[int, float | None] = {}
        for r in recs:
            a = r["attempt"]
            if r["cost_usd"] is None or (a in by_attempt and by_attempt[a] is None):
                by_attempt[a] = None
            else:
                by_attempt[a] = round(by_attempt.get(a, 0.0) + r["cost_usd"], 9)
        row["by_attempt"] = [{"attempt": a, "cost_usd": c} for a, c in sorted(by_attempt.items())]
        row["models"] = sorted({r["served_model"] or "?" for r in recs})
        tasks.append(row)
    return {"provenance": _provenance([info], len(unique), as_of), "tasks": tasks,
            "totals": ledger_totals(unique.values()), "duplicates": len(ledger) - len(unique)}


def killswitch_view(state_dir: Path, settings: object, repo_root: Path) -> dict:
    """Trạng thái kill-switch CHỈ ĐỂ XEM (fail-closed như ``orchestrator.killswitch``)."""
    try:
        path = killswitch_path(state_dir, settings if isinstance(settings, Mapping) else None)
    except ValueError:
        path = None
    st = read_status(path, repo_root=repo_root).to_dict()
    shown = None
    if path is not None:
        try:
            shown = path.relative_to(state_dir).as_posix()
        except ValueError:
            shown = str(path)
    return {"view_only": True, "state": st["state"], "engaged": st["engaged"], "reason": st["reason"],
            "set_by": st["set_by"], "set_at": st["set_at"], "note": st["note"], "file": shown,
            "present": path is not None and os.path.lexists(path)}


def build_snapshot(state_dir: Path | str, *, source: str = RUNTIME, github_repo: object = _UNSET,
                   killswitch_settings: object = _UNSET, config_path: Path | None = None,
                   repo_root: Path = REPO_ROOT, exported_at: str | None = None) -> dict:
    """Đọc ``state_dir`` (chỉ đọc) và trả về snapshot di động của 6 view."""
    if source not in (RUNTIME, FIXTURE):
        raise AdapterError(f"source phải là {RUNTIME!r} hoặc {FIXTURE!r}, nhận {source!r}")
    state_dir = Path(state_dir).resolve()
    cfg = _read_yaml(Path(config_path) if config_path else DEFAULT_CONFIG_PATH)
    repo = (cfg.get("github") or {}).get("repo") if github_repo is _UNSET else github_repo
    repo = repo if isinstance(repo, str) and _REPO_RE.match(repo) else None
    ks_settings = cfg.get("killswitch") if killswitch_settings is _UNSET else killswitch_settings
    tz = cfg.get("timezone") if isinstance(cfg.get("timezone"), str) else "Asia/Seoul"

    state, state_info = _load_state(state_dir)
    as_of, as_of_error = valid_as_of(state.get("as_of")) if state is not None else (None, None)
    if as_of_error:
        state_info["error"] = as_of_error
    events, events_info = _load_events(state_dir, state)
    ledger, ledger_info = _load_jsonl(state_dir, Ledger.FILE, _check_ledger)
    audit, audit_info = _load_jsonl(state_dir, AuditLog.FILE, _check_audit)
    approvals, approvals_info = _load_jsonl(state_dir, ApprovalMirror.FILE, _check_approval)
    beats, beats_info = _load_heartbeats(state_dir)

    reasons = []
    if source == FIXTURE:
        reasons.append("người gọi đánh dấu fixture")
    if state is not None and state.get("source") == FIXTURE:
        reasons.append('state.json mang source: "fixture"')
    if _within(state_dir, repo_root):
        reasons.append("state_dir nằm trong repo; runtime state luôn ở ngoài repo (R13)")

    return {
        "kind": SNAPSHOT_KIND,
        "v": SNAPSHOT_VERSION,
        "source": FIXTURE if reasons else RUNTIME,
        "source_reasons": reasons,
        # Lúc XUẤT snapshot, không phải as_of: as_of luôn là thời điểm fold cuối cùng.
        "exported_at": exported_at or datetime.now(ZoneInfo(tz)).isoformat(timespec="seconds"),
        "state_dir": str(state_dir),
        "github_repo": repo,
        "fold": {"as_of": as_of, "as_of_error": as_of_error,
                 "fold_count": None if state is None else state.get("fold_count"), "state_file": state_info},
        "killswitch": killswitch_view(state_dir, ks_settings, Path(repo_root)),
        "views": {
            "kanban": kanban_view(state, state_info, as_of),
            "pr_queue": pr_queue_view(state, state_info, events, events_info, repo, as_of),
            "approvals": approvals_view(state, state_info, approvals, approvals_info, repo, as_of),
            "agents": agents_view(beats, beats_info, as_of),
            "logs": logs_view(audit, audit_info, events, events_info, state, as_of),
            "costs": costs_view(ledger, ledger_info, as_of),
        },
    }


# --- dựng HTML tĩnh (không JavaScript) -------------------------------------------------------

BODY_START, BODY_END = "<!--HARNESS:BODY-->", "<!--/HARNESS:BODY-->"
DATA_PLACEHOLDER = '<script type="application/json" id="harness-snapshot">null</script>'
NODATA = '<span class="nodata">không có dữ liệu</span>'
_JSON_IN_HTML = {ord("<"): "\\u003c", ord(">"): "\\u003e", ord("&"): "\\u0026"}


def _e(x: object) -> str:
    return html.escape("" if x is None else str(x), quote=True)


def _v(x: object) -> str:
    """Giá trị hoặc nhãn "không có dữ liệu"; ``0`` hiển thị là ``0``."""
    return NODATA if x is None else _e(x)


def fmt_usd(x: float | None) -> str | None:
    if x is None:
        return None
    return f"{x:.9f}".rstrip("0").rstrip(".")


def _link(url: str | None, text: str) -> str:
    if not url:
        return '<span class="muted">không có link (thiếu github.repo)</span>'
    return f'<a href="{_e(url)}" target="_blank" rel="noopener noreferrer">{_e(text)}</a>'


def _sha(s: object) -> str:
    return f'<code title="{_e(s)}">{_e(str(s)[:12])}</code>' if s else NODATA


def _table(headers: Iterable[str], rows: list[str], empty: str) -> str:
    if not rows:
        return f'<p class="empty">{empty}</p>'
    head = "".join(f"<th>{_e(h)}</th>" for h in headers)
    return f'<div class="table-wrap"><table><thead><tr>{head}</tr></thead><tbody>{"".join(rows)}</tbody></table></div>'


def _watermark(source: object) -> str:
    if source == RUNTIME:
        return ""
    if source == FIXTURE:
        text = 'FAKE · source: "fixture" · dữ liệu giả từ fixture, không phải state thật'
    else:
        text = f"FAKE? · source không xác định ({_short(source, 40)}) · không coi là dữ liệu thật"
    return f'<div class="watermark" role="note" data-watermark="{_e(source)}">{_e(text)}</div>'


def _fake_tag(source: object) -> str:
    return "" if source == RUNTIME else f'<span class="fake-tag" data-watermark="{_e(source)}">FAKE</span>'


def _badge(prov: Mapping) -> str:
    parts = []
    for s in prov["sources"]:
        txt = _e(s["file"])
        if not s["present"]:
            txt += ' <span class="nodata">(không có file)</span>'
        elif s["records"] is not None:
            txt += f" ({_e(s['records'])})"
        if s.get("skipped"):
            txt += f' <span class="warn">bỏ qua {_e(s["skipped"])} dòng hỏng</span>'
        if s.get("error"):
            txt += f' <span class="bad">lỗi: {_e(s["error"])}</span>'
        parts.append(txt)
    as_of = prov["as_of"]
    as_of_html = (f'<time data-as-of="{_e(as_of)}">{_e(as_of)}</time>' if as_of
                  else '<time data-as-of=""><span class="nodata">không có dữ liệu (chưa có lần fold nào)</span></time>')
    status = {"ok": "đủ dữ liệu", "partial": "thiếu một phần nguồn", "no-data": "không có dữ liệu",
              "error": "lỗi đọc nguồn"}[prov["status"]]
    return (f'<div class="provenance" data-status="{_e(prov["status"])}">'
            f'<span><b>Nguồn:</b> {"; ".join(parts)}</span>'
            f'<span><b>Số bản ghi:</b> <span data-records="{_e(prov["records"])}">{_v(prov["records"])}</span></span>'
            f'<span><b>as_of (lần fold cuối):</b> {as_of_html}</span>'
            f'<span class="status status-{_e(prov["status"])}">{status}</span></div>')


def _nodata_block(prov: Mapping) -> str:
    missing = ", ".join(s["file"] for s in prov["sources"] if not s["present"])
    return f'<p class="nodata-block">{NODATA}: chưa có {_e(missing)} trong state_dir lúc xuất snapshot.</p>'


def _render_kanban(view: Mapping) -> str:
    cols = []
    for c in view["columns"]:
        step = f"bước {c['step']}" if c["step"] else ("mở rộng" if c["group"] == "extended" else "lạ")
        cards = "".join(
            f'<article class="card"><div class="card-id">{_e(t["task_id"])}</div>'
            f'<div class="card-title">{_e(t["title"] or "")}</div>'
            f'<div class="card-meta">attempt {_e(t["attempt"])} · PR {_v(t["pr"])} · fix {_e(t["fix_rounds"])}</div>'
            + (f'<div class="card-meta warn">{_e(t["escalation_reason"])}</div>' if t.get("escalation_reason") else "")
            + (f'<div class="card-meta">chờ quay lại: {_e(t["resume_state"])}</div>' if t.get("resume_state") else "")
            + (f'<div class="card-meta">trạng thái lạ: {_e(t["state"])}</div>' if c["group"] == "unknown" else "")
            + f'<div class="card-meta muted">{_e(t["updated_at"])}</div></article>'
            for t in c["tasks"])
        cols.append(f'<div class="column col-{_e(c["group"])}" data-state="{_e(c["state"])}">'
                    f'<h3>{_e(c["state"])} <small>{_e(step)}</small> <span class="count">{_v(c["count"])}</span></h3>'
                    f'{cards}</div>')
    return f'<div class="kanban">{"".join(cols)}</div>'


def _render_label(label: Mapping | None) -> str:
    if label is None:
        return f'{NODATA} <small class="muted">(chưa có verdict Gate 3 cho SHA này)</small>'
    tier = {"red": "Red", "owner-only": "Owner-only", "green": "Green"}.get(label["tier"], label["tier"])
    stale = "" if label["valid"] else f' <small class="warn">cũ: cho SHA {_e(str(label["sha"])[:12])}</small>'
    return f'<span class="label label-{_e(label["tier"])}" title="{_e(label["basis"])}">{_e(tier)}</span>{stale}'


def _render_ci(ci: Mapping | None, checks_url: str | None) -> str:
    if ci is None:
        link = f" · {_link(checks_url, 'check run trên GitHub')}" if checks_url else ""
        return f"{NODATA}{link}"
    return f'<span class="ci ci-{_e(ci["state"])}" title="{_e(ci["basis"])}">{_e(ci["state"])}</span>'


def _render_pr_queue(view: Mapping) -> str:
    rows = [
        f'<tr class="{"" if i["current"] else "past"}"><td>{_link(i["url"], "#" + str(i["pr"]))}</td>'
        f'<td>{_e(i["task_id"])}<div class="muted">{_e(i["title"] or "")}</div></td><td>{_e(i["attempt"])}</td>'
        f'<td>{_e(i["state"])}</td><td>{_render_label(i["label"])}</td>'
        f'<td>{_render_ci(i["ci"], i["checks_url"])}</td><td>{_sha(i["head_sha"])}</td></tr>'
        for i in view["items"]]
    return _table(("PR", "Task", "Attempt", "Trạng thái", "Nhãn", "CI", "Head SHA"), rows, "0 PR trong state.")


def _render_approvals(view: Mapping) -> str:
    kinds = {"spec": "Spec chờ duyệt", "pr": "PR chờ người dùng review + merge", "escalated": "Escalated",
             "awaiting-human": "Chờ người dùng"}
    rows = []
    for i in view["items"]:
        r = i["latest_review"]
        if r is None:
            review = f'{NODATA} <small class="muted">(không có bản phản chiếu)</small>'
        else:
            head = "khớp head" if r["matches_head"] else "KHÔNG khớp head: phải duyệt lại"
            review = (f'{_e(r["state"])} bởi {_e(r["reviewer"])} trên {_sha(r["commit_id"])} '
                      f'<small class="{"" if r["matches_head"] else "warn"}">{head}</small>')
        link_text = f"Mở PR #{i['pr']} trên GitHub" if i["pr"] else "Tìm trên GitHub"
        rows.append(f'<tr><td>{_e(kinds[i["kind"]])}</td><td>{_e(i["task_id"])}<div class="muted">{_e(i["title"] or "")}'
                    f'</div></td><td>{_e(i["state"])}'
                    + (f'<div class="warn">{_e(i["reason"])}</div>' if i["reason"] else "")
                    + f'</td><td>{review}</td><td>{_link(i["url"], link_text)}</td></tr>')
    note = ('<p class="note">Duyệt diễn ra trên GitHub (review của người dùng gắn với SHA). Bản phản chiếu '
            '<code>approvals.jsonl</code> chỉ để xem, không phải nguồn sự thật. Dashboard không có thao tác duyệt.</p>')
    return note + _table(("Loại", "Task", "Trạng thái", "Review mới nhất (bản phản chiếu)", "GitHub"), rows,
                         "0 mục đang chờ người dùng.")


def _render_agents(view: Mapping) -> str:
    orch = view["orchestrator"]
    if orch is None:
        return f"<p>{NODATA}: heartbeats.json không đọc được.</p>"
    head = (f'<p>Orchestrator: <span class="hb hb-{_e(orch["status"])}">{_e(orch["status"])}</span>'
            + (f' <span class="bad">{_e(orch.get("last_error"))}</span>' if orch.get("last_error") else "")
            + f' · timeout {_e(view["timeout_s"])}s · heartbeats.json publish lúc <time>{_e(view["published_at"])}'
            '</time> (age_s tính tại lúc đó)</p>')
    rows = [
        f'<tr><td>{_e(a["agent"])}</td><td><span class="hb hb-{_e(a["status"])}">{_e(a["status"])}</span></td>'
        f'<td>{_v(a["ts"])}</td><td>{_v(a["age_s"])}</td><td>{_v(a["pid"])}</td><td>{_v(a["task_id"])}</td>'
        f'<td>{_v(a["model_id"])}</td><td>{_e(a["reason"] or a["last_error"] or "")}</td></tr>'
        for a in view["agents"]]
    return head + _table(("Agent", "Trạng thái", "Heartbeat", "age_s", "PID", "Task", "Model", "Lý do / lỗi"),
                         rows, "0 heartbeat agent trong heartbeats.json.")


def _render_logs(view: Mapping) -> str:
    audit = view["audit"]
    if audit["count"] is None:
        audit_html = f"<p>{NODATA}: chưa có audit.jsonl.</p>"
    else:
        rows = [f'<tr class="res-{_e(r["result"])}"><td>{_e(r["seq"])}</td><td>{_e(r["ts"])}</td><td>{_e(r["action"])}</td>'
                f'<td>{_e(r["result"])}</td><td>{_e(r["agent"])}</td><td>{_v(r["task_id"])}</td>'
                f'<td class="mono">{_e(r["detail"] or "")}</td></tr>' for r in reversed(audit["tail"])]
        audit_html = (f'<p class="muted">{len(audit["tail"])} / {audit["count"]} bản ghi cuối, mới nhất trước. '
                      'Best-effort, không chống giả mạo (R15).</p>'
                      + _table(("seq", "ts", "action", "result", "agent", "task", "detail"), rows, "0 bản ghi audit."))
    agent_parts = []
    for name, a in view["agents"].items():
        rows = [f'<tr><td>{_e(r["seq"])}</td><td>{_e(r["ts"])}</td><td>{_e(r["type"])}</td><td>{_e(r["task_id"])}</td>'
                f'<td>{"đã fold" if r["folded"] else "chưa fold"}</td><td class="mono">{_e(r["payload"] or "")}</td></tr>'
                for r in reversed(a["tail"])]
        agent_parts.append(f'<details><summary>{_e(name)} · {a["count"]} event ({a["folded"]} đã fold)</summary>'
                           + _table(("seq", "ts", "type", "task", "fold", "payload"), rows, "0 event.") + "</details>")
    agents_html = "".join(agent_parts) or f"<p>{NODATA}: chưa có file event trong inbox.</p>"
    q = view["quarantine"]
    q_html = ("" if not q or not q["count"] else
              f'<p class="warn">{q["count"]} dòng event bị cách ly (quarantine) trong state.json.</p>')
    return f"<h3>Audit log</h3>{audit_html}<h3>Log theo agent (inbox)</h3>{agents_html}{q_html}"


def _cost_cell(cost: float | None, unpriced: int) -> str:
    if unpriced:
        return f'≥ {_e(fmt_usd(cost))} <small class="warn">({unpriced} lời gọi chưa có giá)</small>'
    return _e(fmt_usd(cost))


def _render_costs(view: Mapping) -> str:
    if view["totals"] is None:
        return f"<p>{NODATA}: ledger.jsonl không đọc được.</p>"
    rows = [
        f'<tr data-task="{_e(t["task_id"])}" data-cost="{_e(fmt_usd(t["cost_usd"]))}"><td>{_e(t["task_id"])}</td>'
        f'<td>{t["records"]}</td><td>{t["input_tokens"]}</td><td>{t["output_tokens"]}</td>'
        f'<td>{_cost_cell(t["cost_usd"], t["unpriced_calls"])}</td><td>{t["estimated_calls"]}</td>'
        f'<td class="muted">{_e(", ".join(t["models"]))}</td></tr>' for t in view["tasks"]]
    tot = view["totals"]
    rows.append(
        f'<tr class="total" data-total-cost="{_e(fmt_usd(tot["cost_usd"]))}" data-total-records="{tot["records"]}">'
        f'<td>Tổng ledger</td><td>{tot["records"]}</td><td>{tot["input_tokens"]}</td><td>{tot["output_tokens"]}</td>'
        f'<td>{_cost_cell(tot["cost_usd"], tot["unpriced_calls"])}</td><td>{tot["estimated_calls"]}</td><td></td></tr>')
    dup = (f'<p class="warn">{view["duplicates"]} bản ghi trùng key (task, call_id, attempt) bị bỏ qua như Ledger.</p>'
           if view["duplicates"] else "")
    return ('<p class="note">USD theo <code>config/prices.yaml</code> tại lúc ghi ledger. Lời gọi chưa có giá không '
            'được tính là 0.</p>' + dup
            + _table(("Task", "Lời gọi", "Input token", "Output token", "Chi phí USD", "Ước lượng", "Model"),
                     rows, ""))


_RENDERERS = {"kanban": _render_kanban, "pr_queue": _render_pr_queue, "approvals": _render_approvals,
              "agents": _render_agents, "logs": _render_logs, "costs": _render_costs}


def _render_killswitch(ks: Mapping) -> str:
    state = "BẬT — mọi hành động tự động dừng" if ks["engaged"] else "TẮT"
    meta = " · ".join(x for x in (
        f"file: {_e(ks['file'])}" + ("" if ks["present"] else " (không có file → fail-closed)"),
        f"đặt bởi {_e(ks['set_by'])} lúc {_e(ks['set_at'])}" if ks.get("set_at") else "",
        _e(ks.get("note") or ""),
    ) if x)
    return (f'<div class="killswitch ks-{"on" if ks["engaged"] else "off"}" data-killswitch="{_e(ks["state"])}">'
            f'<b>Kill-switch: {state}</b> <span>{_e(ks["reason"])}</span><div class="muted">{meta}</div>'
            '<div class="muted">Chỉ xem. Đổi kill-switch bằng tay theo docs/runbooks/release.md, không qua dashboard.'
            '</div></div>')


def check_snapshot(snapshot: object) -> dict:
    if not isinstance(snapshot, dict) or snapshot.get("kind") != SNAPSHOT_KIND:
        raise AdapterError(f"không phải snapshot dashboard (kind phải là {SNAPSHOT_KIND!r})")
    v = snapshot.get("v")
    if isinstance(v, bool) or not isinstance(v, int) or not 1 <= v <= SNAPSHOT_VERSION:
        raise AdapterError(f"version snapshot {v!r} không đọc được")
    if set(VIEW_ORDER) - set(snapshot.get("views") or {}):
        raise AdapterError("snapshot thiếu view")
    return snapshot


def render_body(snapshot: Mapping) -> str:
    src = snapshot.get("source")
    fold = snapshot["fold"]
    as_of = (f'<time data-as-of="{_e(fold["as_of"])}">{_e(fold["as_of"])}</time>' if fold["as_of"]
             else f'<time data-as-of="">{NODATA} (chưa có lần fold nào)</time>')
    err = f' <span class="bad">{_e(fold["as_of_error"])}</span>' if fold.get("as_of_error") else ""
    reasons = "; ".join(snapshot.get("source_reasons") or [])
    header = (
        f'{_watermark(src)}<header class="page-head"><h1>Harness Agents — dashboard (chỉ đọc)</h1>'
        f'<div class="meta"><span>source: <b data-source="{_e(src)}">{_e(src)}</b>'
        + (f' <small class="muted">({_e(reasons)})</small>' if reasons else "")
        + f'</span><span>Lần fold cuối (as_of): {as_of}{err}</span>'
        f'<span>fold_count: {_v(fold["fold_count"])}</span>'
        f'<span>Repo: {_e(snapshot.get("github_repo") or "không rõ")}</span>'
        f'<span class="muted">Snapshot xuất lúc {_e(snapshot.get("exported_at"))} từ {_e(snapshot.get("state_dir"))} '
        '(không phải as_of)</span></div>'
        f'{_render_killswitch(snapshot["killswitch"])}'
        '<p class="trust">State và audit cục bộ chỉ là best-effort, không chống giả mạo (R15). Mỏ neo tin cậy là '
        'GitHub login + branch protection + required check. Dashboard không có thao tác ghi nào.</p></header>')
    sections = []
    for n, key in enumerate(VIEW_ORDER, 1):
        view = snapshot["views"][key]
        prov = view["provenance"]
        inner = _nodata_block(prov) if prov["status"] == "no-data" else _RENDERERS[key](view)
        sections.append(f'<section class="view" id="view-{key}" data-view="{key}"><div class="view-head">'
                        f'<h2>{n}. {_e(VIEW_TITLES[key])}</h2>{_fake_tag(src)}</div>{_badge(prov)}{inner}</section>')
    footer = ('<footer>Làm mới = xuất snapshot mới bằng <code>python -m dashboard.data_adapter render</code> trên máy '
              'chạy harness rồi chuyển file sang Muse VM. Muse Artifact không đọc được state_dir của máy đó.</footer>')
    return header + "<main>" + "".join(sections) + "</main>" + footer


def render_html(snapshot: Mapping, template: str | None = None) -> str:
    """Dựng trang tĩnh: thay phần thân mặc định ("chưa có snapshot") và nhúng snapshot JSON."""
    check_snapshot(snapshot)
    template = TEMPLATE_PATH.read_text(encoding="utf-8") if template is None else template
    start, end = template.find(BODY_START), template.find(BODY_END)
    if start < 0 or end < start or DATA_PLACEHOLDER not in template:
        raise AdapterError("artifact.html thiếu chỗ đánh dấu HARNESS:BODY hoặc harness-snapshot")
    out = template[: start + len(BODY_START)] + render_body(snapshot) + template[end:]
    # JSON nhúng chỉ là dữ liệu (type=application/json, không chạy); thoát < > & để không đóng được thẻ.
    data = json.dumps(snapshot, ensure_ascii=True, sort_keys=True).translate(_JSON_IN_HTML)
    at = out.find(DATA_PLACEHOLDER)
    filled = DATA_PLACEHOLDER[: -len("null</script>")] + data + "</script>"
    return out[:at] + filled + out[at + len(DATA_PLACEHOLDER):]


# --- xuất file (thao tác ghi duy nhất) ---------------------------------------------------


def _write_output(path: Path | str, text: str, *, forbidden: Iterable[Path | None]) -> Path:
    """Ghi file đầu ra do người dùng chỉ định. KHÔNG BAO GIỜ ghi vào state_dir hay repo."""
    path = Path(path).resolve()
    for root in forbidden:
        if root is not None and _within(path, Path(root)):
            raise AdapterError(f"từ chối ghi {path}: nằm trong {root} (state_dir chỉ đọc; runtime data không vào repo)")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    return path


def _eprint(msg: str) -> None:
    try:
        print(msg, file=sys.stderr)
    except UnicodeEncodeError:  # console cp1252 trên Windows
        print(msg.encode("ascii", "backslashreplace").decode("ascii"), file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m dashboard.data_adapter",
                                 description="Xuất snapshot dashboard CHỈ ĐỌC từ state_dir (PR-5).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("snapshot", help="in hoặc ghi snapshot JSON")
    s.add_argument("--state-dir", help="mặc định: state_dir trong config/harness.yaml")
    s.add_argument("--out", help="file JSON đầu ra (ngoài state_dir và repo); mặc định stdout")
    s.add_argument("--fixture", action="store_true", help='đánh dấu source: "fixture" (watermark FAKE)')
    r = sub.add_parser("render", help="dựng artifact HTML tĩnh từ snapshot")
    g = r.add_mutually_exclusive_group()
    g.add_argument("--snapshot", help="snapshot JSON đã xuất")
    g.add_argument("--state-dir", help="đọc state_dir và dựng luôn; mặc định: state_dir trong config")
    r.add_argument("--out", required=True, help="file HTML đầu ra (ngoài state_dir và repo)")
    r.add_argument("--fixture", action="store_true", help='đánh dấu source: "fixture" (watermark FAKE)')
    args = ap.parse_args(argv)

    try:
        if getattr(args, "snapshot", None):
            with open(args.snapshot, encoding="utf-8") as f:
                snap = check_snapshot(json.load(f))
            state_dir = Path(snap["state_dir"]) if isinstance(snap.get("state_dir"), str) else None
        else:
            state_dir = Path(args.state_dir) if args.state_dir else load_config().state_dir
            snap = build_snapshot(state_dir, source=FIXTURE if args.fixture else RUNTIME)
        forbidden = (state_dir, REPO_ROOT)
        if args.cmd == "render":
            text = render_html(snap)
        else:
            text = json.dumps(snap, ensure_ascii=True, sort_keys=True, indent=1) + "\n"
        if args.out:
            out = _write_output(args.out, text, forbidden=forbidden)
            _eprint(f"source={snap['source']} as_of={snap['fold']['as_of']} -> {out}")
        else:
            sys.stdout.write(text)
    except (OSError, ValueError) as exc:
        _eprint(f"lỗi: {_err(exc)}")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
