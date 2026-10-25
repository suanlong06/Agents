"""Audit log (PR-1a, R4).

Append-only JSONL, mỗi bản ghi có ``seq`` tăng liên tục (kể cả qua các lần khởi
động lại). Đủ trường theo R4: thời điểm KST, hành động, kết quả, agent, model
thực sự phục vụ, vendor, nguồn model_id, commit SHA, task_id, attempt, PR.

BEST-EFFORT, KHÔNG CHỐNG GIẢ MẠO cho tới khi tách OS user (R15). KHÔNG có hash
chain: agent cùng OS user vẫn sửa được file, nên hash chain không chặn được gì
(D§8). ``dedup_key`` chỉ để replay cùng event không ghi thêm bản ghi.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from . import schemas
from .fsio import WriterGuard, append_line, read_jsonl, terminate_torn_tail

AUDIT_VERSION = 1


class AuditLog:
    FILE = "audit.jsonl"

    def __init__(self, state_dir: Path, guard: WriterGuard, clock: Callable[[], str]):
        guard.ensure_held()
        self.path = Path(state_dir) / self.FILE
        self.guard = guard
        self._clock = clock
        terminate_torn_tail(self.path)
        existing = self.read(self.path)
        self._seq = max((r["seq"] for r in existing), default=0)
        self._keys = {r["dedup_key"] for r in existing if r.get("dedup_key")}

    @staticmethod
    def read(path: Path) -> list[dict]:
        """Mọi bản ghi đọc được theo thứ tự file; dòng hỏng bị bỏ qua."""
        out = []
        for _off, obj, _raw in read_jsonl(Path(path)):
            if obj is not None and isinstance(obj.get("seq"), int):
                out.append(obj)
        return out

    def append(
        self,
        *,
        action: str,
        result: str,
        agent: str,
        served_model: str | None,
        vendor: str | None,
        model_id_source: str | None,
        sha: str | None,
        task_id: str | None,
        attempt: int | None,
        pr: int | None = None,
        detail: dict | None = None,
        dedup_key: str | None = None,
    ) -> dict | None:
        """Append một bản ghi; trả về ``None`` nếu ``dedup_key`` đã có."""
        self.guard.ensure_held()
        if dedup_key and dedup_key in self._keys:
            return None
        rec = {
            "v": AUDIT_VERSION,
            "seq": self._seq + 1,
            "ts": self._clock(),
            "action": action,
            "result": result,
            "agent": agent,
            "served_model": served_model,
            "vendor": vendor,
            "model_id_source": model_id_source,
            "sha": sha,
            "task_id": task_id,
            "attempt": attempt,
            "pr": pr,
            "dedup_key": dedup_key,
        }
        if detail is not None:
            rec["detail"] = detail
        schemas.validate("audit", rec)
        append_line(self.path, rec)
        self._seq = rec["seq"]
        if dedup_key:
            self._keys.add(dedup_key)
        return rec
