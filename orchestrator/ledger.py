"""Cost ledger (PR-1a, R5).

- Một bản ghi JSONL cho mỗi lời gọi model: ``task_id``, ``attempt``, ``agent``,
  model THỰC SỰ phục vụ (sau fallback) kèm lý do fallback, nguồn model_id, token
  DELTA, chi phí, cờ ``estimated``.
- Giá lấy từ ``config/prices.yaml``; model không có giá thì ``cost_usd = null``
  (không đoán giá). Bản ghi mang ``prices_version``.
- Key idempotent ``(task_id, call_id, attempt)``: retry, replay sau crash hay
  fallback báo lại cùng lời gọi không bị cộng đôi. Cùng key mà nội dung khác thì
  ném ``LedgerConflict`` thay vì ghi đè hay cộng thêm.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

import yaml

from . import schemas
from .fsio import WriterGuard, append_line, read_jsonl, terminate_torn_tail

LEDGER_VERSION = 1
TOKENS_PER_UNIT = 1_000_000

# Trường xác định nội dung một lời gọi; ``ts``, ``cost_usd``, ``prices_version``
# có thể khác giữa các lần replay (đồng hồ, bảng giá) nên không so.
_IDENTITY_FIELDS = (
    "agent", "requested_model", "served_model", "vendor", "model_id_source",
    "fallback_reason", "effort", "input_tokens", "output_tokens", "estimated",
)


class LedgerConflict(ValueError):
    """Cùng key ``(task_id, call_id, attempt)`` nhưng nội dung khác bản đã ghi."""


class PriceError(ValueError):
    """``prices.yaml`` sai định dạng."""


@dataclass(frozen=True)
class ModelPrice:
    vendor: str | None
    input_per_1m: float
    output_per_1m: float


class PriceTable:
    """Bảng giá USD / 1M token, nạp từ ``prices.yaml``."""

    def __init__(self, models: Mapping[str, ModelPrice], *, version: str | None, verified: bool):
        self.models = dict(models)
        self.version = version
        self.verified = verified

    @classmethod
    def load(cls, path: Path) -> "PriceTable":
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        if data.get("schema_version") != 1:
            raise PriceError(f"{path}: schema_version phải là 1")
        if data.get("currency", "USD") != "USD" or data.get("unit", "per_1m_tokens") != "per_1m_tokens":
            raise PriceError(f"{path}: chỉ hỗ trợ USD theo per_1m_tokens")
        models = {}
        for model_id, spec in (data.get("models") or {}).items():
            try:
                inp, out = float(spec["input"]), float(spec["output"])
            except (KeyError, TypeError, ValueError) as exc:
                raise PriceError(f"{path}: giá của {model_id!r} sai định dạng") from exc
            if inp < 0 or out < 0:
                raise PriceError(f"{path}: giá của {model_id!r} âm")
            models[str(model_id)] = ModelPrice(spec.get("vendor"), inp, out)
        version = data.get("version")
        return cls(models, version=None if version is None else str(version),
                   verified=bool(data.get("verified", False)))

    def vendor(self, model_id: str | None) -> str | None:
        price = self.models.get(model_id) if model_id else None
        return price.vendor if price else None

    def cost(self, model_id: str | None, input_tokens: int, output_tokens: int) -> float | None:
        price = self.models.get(model_id) if model_id else None
        if price is None:
            return None
        usd = (input_tokens * price.input_per_1m + output_tokens * price.output_per_1m) / TOKENS_PER_UNIT
        return round(usd, 9)


class Ledger:
    """Ledger append-only trong ``state_dir``; chỉ orchestrator giữ guard được ghi."""

    FILE = "ledger.jsonl"

    def __init__(self, state_dir: Path, prices: PriceTable, guard: WriterGuard, clock: Callable[[], str]):
        guard.ensure_held()
        self.path = Path(state_dir) / self.FILE
        self.prices = prices
        self.guard = guard
        self._clock = clock
        # Crash giữa lúc append để lại dòng dở: đóng nó lại để dòng sau không dính vào.
        terminate_torn_tail(self.path)
        self._index: dict[tuple[str, str, int], dict] = {}
        for rec in self.read(self.path):
            self._index.setdefault(self.key(rec), rec)

    @staticmethod
    def key(rec: Mapping) -> tuple[str, str, int]:
        return (rec["task_id"], rec["call_id"], rec["attempt"])

    @staticmethod
    def read(path: Path) -> list[dict]:
        """Mọi bản ghi hợp lệ trên đĩa; dòng hỏng (ghi dở) bị bỏ qua."""
        out = []
        for _off, obj, _raw in read_jsonl(Path(path)):
            if obj is None:
                continue
            try:
                schemas.validate("ledger", obj)
            except schemas.SchemaError:
                continue
            out.append(obj)
        return out

    def record(
        self,
        *,
        task_id: str,
        attempt: int,
        call_id: str,
        agent: str,
        served_model: str | None,
        vendor: str | None,
        model_id_source: str,
        requested_model: str | None,
        fallback_reason: str | None,
        effort: str | None,
        input_tokens: int,
        output_tokens: int,
        estimated: bool,
    ) -> tuple[str, dict]:
        """Ghi một lời gọi. Trả về ``("recorded", rec)`` hoặc ``("duplicate", rec_cũ)``."""
        self.guard.ensure_held()
        rec = {
            "v": LEDGER_VERSION,
            "ts": self._clock(),
            "task_id": task_id,
            "attempt": attempt,
            "call_id": call_id,
            "agent": agent,
            "requested_model": requested_model,
            "served_model": served_model,
            "vendor": vendor or self.prices.vendor(served_model),
            "model_id_source": model_id_source,
            "fallback_reason": fallback_reason,
            "effort": effort,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "estimated": estimated,
            "cost_usd": self.prices.cost(served_model, input_tokens, output_tokens),
            "prices_version": self.prices.version,
        }
        key = self.key(rec)
        existing = self._index.get(key)
        if existing is not None:
            diff = [f for f in _IDENTITY_FIELDS if existing.get(f) != rec[f]]
            if diff:
                raise LedgerConflict(
                    f"lời gọi {key} đã ghi với nội dung khác ở {', '.join(diff)}; không cộng thêm"
                )
            return "duplicate", existing
        schemas.validate("ledger", rec)
        append_line(self.path, rec)
        self._index[key] = rec
        return "recorded", rec

    def records(self, task_id: str | None = None) -> list[dict]:
        return [r for r in self._index.values() if task_id is None or r["task_id"] == task_id]

    def cost_by_attempt(self, task_id: str) -> dict[int, float | None]:
        """Chi phí theo attempt của một task; ``None`` nếu attempt có lời gọi chưa có giá."""
        out: dict[int, float | None] = {}
        for r in self.records(task_id):
            a = r["attempt"]
            if r["cost_usd"] is None or (a in out and out[a] is None):
                out[a] = None
            else:
                out[a] = round(out.get(a, 0.0) + r["cost_usd"], 9)
        return out

    def totals(self, task_id: str | None = None) -> dict:
        recs = self.records(task_id)
        priced = [r["cost_usd"] for r in recs if r["cost_usd"] is not None]
        return {
            "records": len(recs),
            "input_tokens": sum(r["input_tokens"] for r in recs),
            "output_tokens": sum(r["output_tokens"] for r in recs),
            "cost_usd": round(sum(priced), 9),
            "unpriced_calls": len(recs) - len(priced),
            "estimated_calls": sum(1 for r in recs if r["estimated"]),
        }
