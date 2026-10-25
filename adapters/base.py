"""Interface chung cho adapter (PR-2, R5/R11).

- Mỗi adapter có ``run`` (một lời gọi model), ``status`` (breaker, probe) và
  ``cost`` (giá theo ``config/prices.yaml``).
- Model THỰC SỰ phục vụ lấy từ metadata có cấu trúc (JSON của CLI, transport) hoặc
  log 9Router join theo request id. KHÔNG BAO GIỜ suy ra từ text model tự in ra:
  ``CallResult.output`` chỉ là dữ liệu. Không có metadata thì model là "không rõ",
  và model không rõ bị coi là xung đột ở Gate 3 (R11).
- Bảng alias → ``(model_id, vendor)``: alias trùng mà trỏ tới hai đích khác nhau
  (kể cả khác hoa thường, hay khóa YAML lặp) là lỗi khi nạp; alias không có trong
  bảng thì model là không rõ.
- ``NotAvailable`` (stub Codex/Cursor) là lỗi TERMINAL: không retry, không fallback,
  không chạm breaker. ``ServiceUnavailable`` là tuyến tạm sập (timeout, CLI lỗi,
  breaker mở); chỉ chuỗi Opus được fallback khi gặp nó.
- Usage cộng dồn theo phiên được đổi ra delta trước khi ghi ledger (R5).
- Circuit breaker và timeout là mặc định tạm trong config (R16).

Module này cố ý KHÔNG import socket, subprocess, http hay urllib (kể cả gián tiếp
qua module khác của repo): stub Codex/Cursor chỉ dựa vào nó (test AST kiểm).
"""

from __future__ import annotations

import math
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping

import yaml

MODEL_ID_SOURCES = ("cli_metadata", "router_log", "unknown")
UNKNOWN_PREFIX = "unknown:"
CHARS_PER_TOKEN = 4  # ước lượng thô khi tuyến không báo token (estimated = true)


# --- lỗi ---------------------------------------------------------------------


class AdapterError(Exception):
    """Gốc của mọi lỗi adapter."""


class NotAvailable(AdapterError):
    """Adapter chỉ là stub (Codex, Cursor). TERMINAL: không retry, không fallback."""

    terminal = True


class ServiceUnavailable(AdapterError):
    """Tuyến tạm sập: timeout, lỗi transport, CLI lỗi, completion rỗng, breaker mở."""

    terminal = False


class ConfigError(ValueError):
    """``routing.yaml`` hoặc ``model_aliases.yaml`` sai."""


class AliasConflict(ConfigError):
    """Một alias trỏ tới hai ``(model_id, vendor)`` khác nhau."""


# --- YAML không cho khóa trùng -------------------------------------------------


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(loader: yaml.SafeLoader, node: yaml.MappingNode, deep: bool = False):
    seen = set()
    for key_node, _value in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            dup = key in seen
        except TypeError:
            raise ConfigError(f"khóa YAML không hợp lệ ở dòng {key_node.start_mark.line + 1}") from None
        if dup:
            # PyYAML mặc định lấy giá trị cuối và im lặng: với alias đó là xung đột ẩn.
            raise ConfigError(f"khóa YAML trùng: {key!r} (dòng {key_node.start_mark.line + 1})")
        seen.add(key)
    return yaml.SafeLoader.construct_mapping(loader, node, deep=deep)


_UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping)


def load_yaml(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        data = yaml.load(f, Loader=_UniqueKeyLoader)  # _UniqueKeyLoader là SafeLoader con
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: gốc phải là mapping")
    return data


# --- model và alias ------------------------------------------------------------


@dataclass(frozen=True)
class ModelRef:
    model_id: str
    vendor: str


@dataclass(frozen=True)
class ServedModel:
    """Model thực sự phục vụ một lời gọi, kèm nguồn của model_id."""

    model_id: str | None  # model_id chuẩn; None khi không rõ
    vendor: str | None
    source: str  # cli_metadata | router_log | unknown
    raw: str | None = None  # đúng chuỗi metadata/log ghi

    def __post_init__(self):
        if self.source not in MODEL_ID_SOURCES:
            raise ValueError(f"nguồn model_id không hợp lệ: {self.source!r}")

    @classmethod
    def unknown(cls, raw: str | None = None, source: str = "unknown") -> "ServedModel":
        return cls(None, None, source, raw)

    @property
    def known(self) -> bool:
        return self.model_id is not None and self.vendor is not None and self.source != "unknown"

    @property
    def ledger_model(self) -> str | None:
        """Giá trị ``served_model`` trong ledger. Alias lạ giữ lại, có tiền tố ``unknown:``."""
        if self.known:
            return self.model_id
        return f"{UNKNOWN_PREFIX}{self.raw}" if self.raw else None


class ModelAliases:
    """Bảng alias → ``(model_id, vendor)``, không có xung đột."""

    def __init__(self, models: Mapping[str, str], aliases: Mapping[str, str] | None = None):
        self._models: dict[str, ModelRef] = {}
        self._index: dict[str, ModelRef] = {}
        for model_id, vendor in models.items():
            if not _nonempty_str(model_id) or not _nonempty_str(vendor):
                raise ConfigError(f"model {model_id!r} phải có vendor là chuỗi khác rỗng")
            ref = ModelRef(model_id.strip(), vendor.strip())
            self._add(model_id, ref)
            self._models[ref.model_id] = ref
        for alias, target in (aliases or {}).items():
            if not _nonempty_str(alias) or not _nonempty_str(target):
                raise ConfigError(f"alias {alias!r} → {target!r} phải là chuỗi khác rỗng")
            if target not in self._models:
                raise ConfigError(f"alias {alias!r} trỏ tới model chưa khai báo {target!r}")
            self._add(alias, self._models[target])

    @staticmethod
    def normalize(name: str) -> str:
        return name.strip().casefold()

    def _add(self, name: str, ref: ModelRef) -> None:
        key = self.normalize(name)
        cur = self._index.get(key)
        if cur is not None and cur != ref:
            raise AliasConflict(
                f"alias {name!r} trỏ tới cả ({cur.model_id}, {cur.vendor}) và ({ref.model_id}, {ref.vendor})"
            )
        self._index[key] = ref

    @classmethod
    def load(cls, path: Path) -> "ModelAliases":
        data = load_yaml(Path(path))
        if data.get("schema_version") != 1:
            raise ConfigError(f"{path}: schema_version phải là 1")
        unknown = set(data) - {"schema_version", "models", "aliases"}
        if unknown:
            raise ConfigError(f"{path}: khóa lạ {sorted(unknown)}")
        models = data.get("models") or {}
        if not isinstance(models, dict) or not models:
            raise ConfigError(f"{path}: models phải là mapping khác rỗng")
        flat = {}
        for model_id, spec in models.items():
            if not isinstance(spec, dict) or set(spec) != {"vendor"}:
                raise ConfigError(f"{path}: models.{model_id} chỉ có đúng khóa vendor")
            flat[model_id] = spec["vendor"]
        aliases = data.get("aliases") or {}
        if not isinstance(aliases, dict):
            raise ConfigError(f"{path}: aliases phải là mapping")
        return cls(flat, aliases)

    @property
    def models(self) -> dict[str, ModelRef]:
        return dict(self._models)

    def resolve(self, name: str | None) -> ModelRef | None:
        if not _nonempty_str(name):
            return None
        return self._index.get(self.normalize(name))

    def served(self, raw: str | None, source: str) -> ServedModel:
        """``ServedModel`` từ chuỗi metadata/log. Alias lạ → không rõ (giữ ``raw``)."""
        ref = self.resolve(raw)
        if ref is None:
            return ServedModel(None, None, source, raw if _nonempty_str(raw) else None)
        return ServedModel(ref.model_id, ref.vendor, source, raw)


def _nonempty_str(x: object) -> bool:
    return isinstance(x, str) and bool(x.strip())


def estimate_tokens(text: str | None) -> int:
    return math.ceil(len(text or "") / CHARS_PER_TOKEN)


# --- lời gọi -------------------------------------------------------------------


@dataclass(frozen=True)
class CallRequest:
    task_id: str
    attempt: int
    call_id: str
    prompt: str
    model: str  # model yêu cầu cho bước này
    effort: str | None = None
    agent: str = "implementer"
    cwd: str | None = None
    requested_model: str | None = None  # đầu chuỗi trước fallback; None = ``model``
    fallback_reason: str | None = None

    @property
    def requested(self) -> str:
        return self.requested_model or self.model


@dataclass(frozen=True)
class ModelUsage:
    served: ServedModel
    input_tokens: int  # DELTA của lời gọi này
    output_tokens: int
    estimated: bool

    def __post_init__(self):
        for n in (self.input_tokens, self.output_tokens):
            if isinstance(n, bool) or not isinstance(n, int) or n < 0:
                raise ValueError(f"token phải là số nguyên >= 0, nhận {n!r}")


@dataclass(frozen=True)
class CallResult:
    adapter: str
    request: CallRequest
    usages: tuple[ModelUsage, ...]
    # Text model trả về. CHỈ là dữ liệu: không bao giờ dùng để suy ra model (R11).
    output: str = ""
    request_ids: tuple[str, ...] = ()
    session_id: str | None = None

    @property
    def served_models(self) -> tuple[ServedModel, ...]:
        """Mọi model thực sự phục vụ lời gọi (đóng góp vào writer set ở Gate 3)."""
        out: list[ServedModel] = []
        for u in self.usages:
            if u.served not in out:
                out.append(u.served)
        return tuple(out)

    @property
    def all_known(self) -> bool:
        return bool(self.usages) and all(u.served.known for u in self.usages)


def merge_usages(usages) -> tuple[ModelUsage, ...]:
    """Gộp usage theo model đã phân giải: hai alias của cùng model là MỘT model phục vụ."""
    merged: dict[str | None, ModelUsage] = {}
    for u in usages:
        key = u.served.ledger_model
        cur = merged.get(key)
        if cur is None:
            merged[key] = u
        else:
            merged[key] = ModelUsage(cur.served, cur.input_tokens + u.input_tokens,
                                     cur.output_tokens + u.output_tokens, cur.estimated or u.estimated)
    return tuple(merged.values())


class CumulativeUsage:
    """Đổi usage cộng dồn theo phiên ra delta (R5).

    Lần đầu thấy một phiên trong process này thì cả số cộng dồn được tính là delta;
    bộ đếm giảm (phiên bị reset) thì số mới là delta. Hai trường hợp đó có thể đếm
    DƯ, không bao giờ đếm thiếu: ngân sách vòng fix vẫn fail-closed.
    """

    def __init__(self):
        self._last: dict[tuple, tuple[int, int]] = {}

    def seed(self, key: tuple, input_total: int, output_total: int) -> None:
        self._last[key] = (input_total, output_total)

    def delta(self, key: tuple, input_total: int, output_total: int) -> tuple[int, int]:
        last = self._last.get(key)
        self._last[key] = (input_total, output_total)
        if last is None or input_total < last[0] or output_total < last[1]:
            return input_total, output_total
        return input_total - last[0], output_total - last[1]


# --- circuit breaker -------------------------------------------------------------


@dataclass(frozen=True)
class BreakerPolicy:
    # Mặc định tạm (R16, SPEC mục 8): mở sau 3 lỗi liên tiếp, giữ 5 phút.
    failure_threshold: int = 3
    reset_after_s: float = 300.0

    def __post_init__(self):
        if self.failure_threshold < 1 or self.reset_after_s <= 0:
            raise ConfigError("circuit_breaker: failure_threshold >= 1 và reset_after_s > 0")


class CircuitBreaker:
    CLOSED, OPEN, HALF_OPEN = "closed", "open", "half_open"

    def __init__(self, policy: BreakerPolicy | None = None, clock: Callable[[], float] = time.monotonic):
        self.policy = policy or BreakerPolicy()
        self._clock = clock
        self.failures = 0
        self.opened_at: float | None = None
        self.last_error: str | None = None

    @property
    def state(self) -> str:
        if self.opened_at is None:
            return self.CLOSED
        if self._clock() - self.opened_at >= self.policy.reset_after_s:
            return self.HALF_OPEN
        return self.OPEN

    def before_call(self, what: str) -> str:
        st = self.state
        if st == self.OPEN:
            left = self.policy.reset_after_s - (self._clock() - self.opened_at)
            raise ServiceUnavailable(f"{what}: circuit breaker đang mở (còn {left:.0f}s): {self.last_error}")
        return st

    def record_success(self) -> None:
        self.failures, self.opened_at, self.last_error = 0, None, None

    def record_failure(self, error: str) -> None:
        was_half_open = self.state == self.HALF_OPEN
        self.failures += 1
        self.last_error = error
        if was_half_open or self.failures >= self.policy.failure_threshold:
            self.opened_at = self._clock()


# --- ledger ----------------------------------------------------------------------


def ledger_rows(result: CallResult, agent: str | None = None) -> list[dict]:
    """Bản ghi R5 cho mỗi model phục vụ một lời gọi.

    Một model → ``call_id`` giữ nguyên; nhiều model → ``<call_id>#<model>`` (ổn định
    khi replay, nên key idempotent ``(task, call_id, attempt)`` vẫn chặn đếm đôi).
    """
    req = result.request
    rows = []
    for u in result.usages:
        call_id = req.call_id
        if len(result.usages) > 1:
            call_id = f"{req.call_id}#{u.served.ledger_model or 'unknown'}"[:128]
        rows.append({
            "task_id": req.task_id,
            "attempt": req.attempt,
            "call_id": call_id,
            "agent": agent or req.agent,
            "requested_model": req.requested,
            "served_model": u.served.ledger_model,
            "vendor": u.served.vendor,
            "model_id_source": u.served.source,
            "fallback_reason": req.fallback_reason,
            "effort": req.effort,
            "input_tokens": u.input_tokens,
            "output_tokens": u.output_tokens,
            "estimated": u.estimated,
        })
    if len({r["call_id"] for r in rows}) != len(rows):
        raise ValueError(f"lời gọi {req.call_id}: hai usage cùng model, phải gộp trước khi ghi ledger")
    return rows


class InboxLedgerSink:
    """Phía agent: ghi event ``model_call`` vào inbox của CHÍNH agent (R3); orchestrator fold vào ledger."""

    def __init__(self, inbox):
        self.inbox = inbox

    def write(self, result: CallResult) -> list[dict]:
        out = []
        for row in ledger_rows(result, agent=self.inbox.agent):
            payload = {k: row[k] for k in ("call_id", "attempt", "requested_model", "fallback_reason",
                                           "effort", "input_tokens", "output_tokens", "estimated")}
            out.append(self.inbox.emit("model_call", row["task_id"], payload, model_id=row["served_model"],
                                       model_id_source=row["model_id_source"], vendor=row["vendor"]))
        return out


class DirectLedgerSink:
    """Phía orchestrator (đang giữ WriterGuard): ghi thẳng ``Ledger.record``."""

    def __init__(self, ledger):
        self.ledger = ledger

    def write(self, result: CallResult) -> list[tuple[str, dict]]:
        return [self.ledger.record(**row) for row in ledger_rows(result)]


# --- adapter ---------------------------------------------------------------------


@dataclass(frozen=True)
class AdapterStatus:
    name: str
    available: bool
    breakers: dict = field(default_factory=dict)
    probe_error: str | None = None


class Adapter(ABC):
    """Lớp nền cho adapter thật: breaker theo model, timeout mỗi lời gọi, ghi ledger."""

    name: str = ""
    vendors: frozenset[str] = frozenset()

    def __init__(
        self,
        *,
        aliases: ModelAliases,
        prices=None,
        ledger=None,
        breaker_policy: BreakerPolicy | None = None,
        call_timeout_s: float = 1800.0,
        probe_timeout_s: float = 2.0,
        probe_prompt: str = "Reply with exactly: OK",
        probe_task_id: str = "probe",
        clock: Callable[[], float] = time.monotonic,
        killswitch=None,
    ):
        if call_timeout_s <= 0 or probe_timeout_s <= 0:
            raise ConfigError(f"{self.name}: timeout phải > 0")
        self.aliases = aliases
        self.prices = prices
        self.ledger = ledger
        self.breaker_policy = breaker_policy or BreakerPolicy()
        self.call_timeout_s = float(call_timeout_s)
        self.probe_timeout_s = float(probe_timeout_s)
        self.probe_prompt = probe_prompt
        self.probe_task_id = probe_task_id
        self._clock = clock
        self._breakers: dict[str, CircuitBreaker] = {}
        # PR-4a: kill-switch (orchestrator.killswitch.KillSwitch, duck-typed để stub không
        # import thêm gì). Bật → ``check`` ném trước mọi lời gọi model, kể cả probe.
        self.killswitch = killswitch

    def breaker(self, model: str) -> CircuitBreaker:
        if model not in self._breakers:
            self._breakers[model] = CircuitBreaker(self.breaker_policy, self._clock)
        return self._breakers[model]

    @abstractmethod
    def _invoke(self, request: CallRequest, *, timeout_s: float, probe: bool) -> CallResult:
        """Một lời gọi thật. Ném ``ServiceUnavailable`` khi tuyến sập/timeout."""

    def _check_killswitch(self, what: str) -> None:
        if self.killswitch is not None:
            self.killswitch.check(f"{self.name}.{what}")

    def run(self, request: CallRequest) -> CallResult:
        self._check_killswitch("run")
        br = self.breaker(request.model)
        if br.before_call(f"{self.name}/{request.model}") == CircuitBreaker.HALF_OPEN:
            self.probe(request.model)  # thử lại bằng completion thật trước khi giao việc
        result = self._call(request, timeout_s=self.call_timeout_s, probe=False)
        self._write_ledger(result)
        return result

    def probe(self, model: str) -> CallResult:
        """Probe bằng completion THẬT (không phải TCP/200), có timeout riêng."""
        self._check_killswitch("probe")
        req = CallRequest(
            task_id=self.probe_task_id, attempt=1, prompt=self.probe_prompt, model=model,
            call_id=f"probe-{self.name}-{time.time_ns()}", agent="probe",
        )
        result = self._call(req, timeout_s=self.probe_timeout_s, probe=True)
        if not result.output.strip():
            self.breaker(model).record_failure("probe: completion rỗng")
            self._write_ledger(result)
            raise ServiceUnavailable(f"{self.name}/{model}: probe trả completion rỗng")
        self._write_ledger(result)  # probe cũng là một lời gọi model (R5)
        return result

    def _call(self, request: CallRequest, *, timeout_s: float, probe: bool) -> CallResult:
        br = self.breaker(request.model)
        try:
            result = self._invoke(request, timeout_s=timeout_s, probe=probe)
        except NotAvailable:
            raise  # terminal: không đụng breaker, không retry
        except ServiceUnavailable as exc:
            br.record_failure(f"{'probe: ' if probe else ''}{exc}")
            raise
        br.record_success()
        if result.usages:
            result = CallResult(result.adapter, result.request, merge_usages(result.usages),
                                result.output, result.request_ids, result.session_id)
        else:
            # Không có metadata nào: vẫn ghi một bản ghi "không rõ", không bao giờ im lặng.
            result = CallResult(
                result.adapter, result.request,
                (ModelUsage(ServedModel.unknown(), estimate_tokens(request.prompt),
                            estimate_tokens(result.output), True),),
                result.output, result.request_ids, result.session_id,
            )
        return result

    def _write_ledger(self, result: CallResult) -> None:
        if self.ledger is not None:
            self.ledger.write(result)

    def status(self, *, probe_model: str | None = None) -> AdapterStatus:
        probe_error = None
        if probe_model is not None:
            try:
                self.probe(probe_model)
            except ServiceUnavailable as exc:
                probe_error = str(exc)
        breakers = {m: {"state": b.state, "failures": b.failures, "last_error": b.last_error}
                    for m, b in self._breakers.items()}
        available = probe_error is None and all(b["state"] != CircuitBreaker.OPEN for b in breakers.values())
        return AdapterStatus(self.name, available, breakers, probe_error)

    def cost(self, result: CallResult) -> float | None:
        """Chi phí USD theo ``prices.yaml``; ``None`` nếu có model chưa có giá hoặc không rõ."""
        if self.prices is None:
            return None
        total = 0.0
        for u in result.usages:
            c = self.prices.cost(u.served.model_id if u.served.known else None, u.input_tokens, u.output_tokens)
            if c is None:
                return None
            total += c
        return round(total, 9)


class StubAdapter(Adapter):
    """Adapter chỉ có tên để tích hợp sau. KHÔNG BAO GIỜ được gọi: mọi thao tác ném ``NotAvailable``."""

    reason = "stub"

    def __init__(self, *_args, **_kwargs):  # không giữ cấu hình, không mở kết nối nào
        pass

    def _unavailable(self) -> NotAvailable:
        return NotAvailable(f"{self.name}: {self.reason}")

    def _invoke(self, request, *, timeout_s, probe):
        raise self._unavailable()

    def run(self, request=None):
        raise self._unavailable()

    def probe(self, model=None):
        raise self._unavailable()

    def status(self, **_kwargs):
        raise self._unavailable()

    def cost(self, result=None):
        raise self._unavailable()
