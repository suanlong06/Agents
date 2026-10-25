"""Router: chọn adapter theo độ khó, fallback CHỈ trong chuỗi Opus (PR-2, R11).

- ``routing.yaml`` được validate theo ``ADAPTER_ALLOWLIST`` (trong code, không trong
  config, để config không tự mở rộng được danh sách). Codex/Cursor không có trong
  allowlist; tên của chúng xuất hiện ở bất kỳ đâu trong ``routing.yaml`` là lỗi.
- Chuỗi nhiều bước chỉ được phép với adapter ``claude`` và mọi bước phải là model
  vendor ``anthropic``, bước đầu là Opus. ``dsh`` và ``agy`` đúng một bước.
- ``ServiceUnavailable`` ở chuỗi Opus → bước kế tiếp, ghi ``fallback_reason``.
  ``dsh``/``agy`` sập, hoặc hết chuỗi Opus → task ``blocked`` + thông báo, không
  fallback ra ngoài (R11).
- ``NotAvailable`` là terminal: ném ngay, không thử bước kế tiếp.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .base import (
    Adapter,
    BreakerPolicy,
    CallRequest,
    CallResult,
    ConfigError,
    ModelAliases,
    NotAvailable,
    ServiceUnavailable,
    StubAdapter,
    load_yaml,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ROUTING_PATH = REPO_ROOT / "config" / "routing.yaml"
DEFAULT_ALIASES_PATH = REPO_ROOT / "config" / "model_aliases.yaml"

ADAPTER_ALLOWLIST = ("claude", "dsh", "agy")
FALLBACK_ADAPTERS = frozenset({"claude"})  # chỉ chuỗi Opus có fallback
ADAPTER_VENDORS = {"claude": frozenset({"anthropic"}), "dsh": frozenset({"deepseek"}),
                   "agy": frozenset({"google"})}
DIFFICULTIES = ("hard", "medium", "easy")
EFFORTS = ("low", "medium", "high", "xhigh", "max")
FORBIDDEN_NAMES = ("codex", "cursor")

_TOP_KEYS = {"schema_version", "routes", "adapters", "circuit_breaker", "probe", "router_log"}
_ROUTE_KEYS = {"adapter", "chain"}
_STEP_KEYS = {"model", "effort"}
_ADAPTER_KEYS = {"command", "call_timeout_s", "probe_timeout_s", "usage", "tokens_reported",
                 "request_id_env", "probe_url", "request_id_header"}
_ROUTER_ADAPTERS = frozenset({"dsh", "agy"})
_ROUTER_LOG_KEYS = {"path", "request_id_field", "model_field", "input_tokens_field",
                    "output_tokens_field", "wait_s"}


@dataclass(frozen=True)
class Step:
    model: str
    effort: str | None = None


@dataclass(frozen=True)
class Route:
    difficulty: str
    adapter: str
    chain: tuple[Step, ...]


@dataclass(frozen=True)
class RoutingConfig:
    routes: dict[str, Route]
    adapters: dict[str, dict]
    breaker: BreakerPolicy
    probe: dict
    router_log: dict | None

    @classmethod
    def load(cls, path: Path | None = None, aliases: ModelAliases | None = None) -> "RoutingConfig":
        path = Path(path) if path else DEFAULT_ROUTING_PATH
        aliases = aliases or ModelAliases.load(DEFAULT_ALIASES_PATH)
        return validate_routing(load_yaml(path), aliases, where=str(path))


def _walk_strings(obj: object):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield str(k)
            yield from _walk_strings(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _walk_strings(v)
    elif isinstance(obj, str):
        yield obj


def _pos_number(v: object, what: str) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0:
        raise ConfigError(f"{what} phải là số > 0, nhận {v!r}")
    return float(v)


def _check_keys(obj: object, allowed: set, what: str) -> dict:
    if not isinstance(obj, dict):
        raise ConfigError(f"{what} phải là mapping")
    extra = set(obj) - allowed
    if extra:
        raise ConfigError(f"{what}: khóa lạ {sorted(map(str, extra))}")
    return obj


def validate_routing(data: Mapping, aliases: ModelAliases, *, where: str = "routing.yaml") -> RoutingConfig:
    for s in _walk_strings(data):
        low = s.casefold()
        for name in FORBIDDEN_NAMES:
            if name in low:
                raise ConfigError(f"{where}: {name!r} không bao giờ được có trong định tuyến (stub): {s!r}")
    _check_keys(data, _TOP_KEYS, where)
    if data.get("schema_version") != 1:
        raise ConfigError(f"{where}: schema_version phải là 1")

    adapters_raw = _check_keys(data.get("adapters"), set(ADAPTER_ALLOWLIST), f"{where}: adapters")
    adapters: dict[str, dict] = {}
    for name, spec in adapters_raw.items():
        spec = dict(_check_keys(spec, _ADAPTER_KEYS, f"{where}: adapters.{name}"))
        cmd = spec.get("command")
        if not isinstance(cmd, list) or not cmd or not all(isinstance(a, str) and a for a in cmd):
            raise ConfigError(f"{where}: adapters.{name}.command phải là list chuỗi khác rỗng")
        for k in ("call_timeout_s", "probe_timeout_s"):
            if k in spec:
                spec[k] = _pos_number(spec[k], f"{where}: adapters.{name}.{k}")
        if spec.get("usage", "delta") not in ("delta", "cumulative"):
            raise ConfigError(f"{where}: adapters.{name}.usage phải là delta hoặc cumulative")
        if not isinstance(spec.get("tokens_reported", True), bool):
            raise ConfigError(f"{where}: adapters.{name}.tokens_reported phải là bool")
        if name not in _ROUTER_ADAPTERS:
            bad = {"request_id_env", "probe_url", "request_id_header", "tokens_reported"} & set(spec)
            if bad:
                raise ConfigError(f"{where}: adapters.{name} không đi qua 9Router, không dùng {sorted(bad)}")
        env = spec.get("request_id_env", [])
        if not isinstance(env, list) or not all(isinstance(e, str) and e for e in env):
            raise ConfigError(f"{where}: adapters.{name}.request_id_env phải là list chuỗi")
        adapters[name] = spec

    routes_raw = _check_keys(data.get("routes"), set(DIFFICULTIES), f"{where}: routes")
    missing = set(DIFFICULTIES) - set(routes_raw)
    if missing:
        raise ConfigError(f"{where}: thiếu route cho {sorted(missing)}")
    routes: dict[str, Route] = {}
    for diff in DIFFICULTIES:
        r = _check_keys(routes_raw[diff], _ROUTE_KEYS, f"{where}: routes.{diff}")
        adapter = r.get("adapter")
        if adapter not in ADAPTER_ALLOWLIST:
            raise ConfigError(f"{where}: routes.{diff}.adapter {adapter!r} không thuộc allowlist {ADAPTER_ALLOWLIST}")
        if adapter not in adapters:
            raise ConfigError(f"{where}: routes.{diff} dùng adapter {adapter!r} chưa cấu hình trong adapters")
        chain_raw = r.get("chain")
        if not isinstance(chain_raw, list) or not chain_raw:
            raise ConfigError(f"{where}: routes.{diff}.chain phải là list khác rỗng")
        if len(chain_raw) > 1 and adapter not in FALLBACK_ADAPTERS:
            raise ConfigError(f"{where}: routes.{diff}: fallback chỉ có trong chuỗi Opus; {adapter!r} không có fallback")
        chain = []
        for i, st in enumerate(chain_raw):
            st = _check_keys(st, _STEP_KEYS, f"{where}: routes.{diff}.chain[{i}]")
            ref = aliases.resolve(st.get("model"))
            if ref is None:
                raise ConfigError(f"{where}: routes.{diff}.chain[{i}]: model {st.get('model')!r} không có trong bảng alias")
            if ref.vendor not in ADAPTER_VENDORS[adapter]:
                raise ConfigError(f"{where}: routes.{diff}.chain[{i}]: {ref.model_id} ({ref.vendor}) "
                                  f"không phục vụ qua {adapter!r}")
            effort = st.get("effort")
            if effort is not None and effort not in EFFORTS:
                raise ConfigError(f"{where}: routes.{diff}.chain[{i}].effort {effort!r} không hợp lệ")
            chain.append(Step(ref.model_id, effort))
        if len(chain) > 1 and "opus" not in chain[0].model.casefold():
            raise ConfigError(f"{where}: routes.{diff}: chuỗi fallback phải bắt đầu bằng Opus")
        routes[diff] = Route(diff, adapter, tuple(chain))

    cb = _check_keys(data.get("circuit_breaker") or {}, {"failure_threshold", "reset_after_s"},
                     f"{where}: circuit_breaker")
    thr = cb.get("failure_threshold", BreakerPolicy.failure_threshold)
    if isinstance(thr, bool) or not isinstance(thr, int):
        raise ConfigError(f"{where}: circuit_breaker.failure_threshold phải là số nguyên")
    breaker = BreakerPolicy(thr, _pos_number(cb.get("reset_after_s", BreakerPolicy.reset_after_s),
                                             f"{where}: circuit_breaker.reset_after_s"))
    probe = dict(_check_keys(data.get("probe") or {}, {"prompt", "task_id"}, f"{where}: probe"))
    router_log = data.get("router_log")
    if router_log is not None:
        router_log = dict(_check_keys(router_log, _ROUTER_LOG_KEYS, f"{where}: router_log"))
        if not isinstance(router_log.get("path"), str) or not router_log["path"]:
            raise ConfigError(f"{where}: router_log.path phải là chuỗi")
    return RoutingConfig(routes, adapters, breaker, probe, router_log)


# --- dựng adapter --------------------------------------------------------------------


def _registry() -> dict[str, type[Adapter]]:
    from .antigravity import AntigravityAdapter
    from .claude import ClaudeAdapter
    from .dsh import DshAdapter

    return {"claude": ClaudeAdapter, "dsh": DshAdapter, "agy": AntigravityAdapter}


def build_router_log(routing: RoutingConfig):
    if routing.router_log is None:
        return None
    from .transport import RouterLog, RouterLogFormat

    cfg = dict(routing.router_log)
    path = Path(os.path.expandvars(cfg.pop("path")))
    wait = float(cfg.pop("wait_s", 2.0))
    return RouterLog(path, RouterLogFormat(**cfg), wait_s=wait)


def build_adapters(routing: RoutingConfig, aliases: ModelAliases, *, prices=None, ledger=None,
                   runner=None, http=None, router_log=None, clock=None) -> dict[str, Adapter]:
    """Dựng adapter cho mọi tên trong allowlist có cấu hình. Không bao giờ dựng stub."""
    reg = _registry()
    if router_log is None:
        router_log = build_router_log(routing)
    out: dict[str, Adapter] = {}
    for name, spec in routing.adapters.items():
        kw = dict(aliases=aliases, prices=prices, ledger=ledger, breaker_policy=routing.breaker,
                  command=tuple(spec["command"]), runner=runner)
        if clock is not None:
            kw["clock"] = clock
        for src, dst in (("call_timeout_s", "call_timeout_s"), ("probe_timeout_s", "probe_timeout_s"),
                         ("usage", "usage_mode")):
            if src in spec:
                kw[dst] = spec[src]
        if "prompt" in routing.probe:
            kw["probe_prompt"] = routing.probe["prompt"]
        if "task_id" in routing.probe:
            kw["probe_task_id"] = routing.probe["task_id"]
        if name in _ROUTER_ADAPTERS:
            kw.update(router_log=router_log, http=http,
                      request_id_env=tuple(spec.get("request_id_env", ())))
            for k in ("tokens_reported", "probe_url", "request_id_header"):
                if k in spec:
                    kw[k] = spec[k]
        out[name] = reg[name](**kw)
    return out


# --- router ----------------------------------------------------------------------------


@dataclass(frozen=True)
class StepAttempt:
    adapter: str
    model: str
    effort: str | None
    call_id: str
    error: str | None


@dataclass(frozen=True)
class RouteOutcome:
    status: str  # "ok" | "blocked"
    route: Route
    result: CallResult | None = None
    attempts: tuple[StepAttempt, ...] = ()
    reason: str | None = None


@dataclass
class Router:
    routing: RoutingConfig
    adapters: Mapping[str, Adapter]
    notifier: object | None = None  # có ``notify(kind=, task_id=, message=, detail=)``, ví dụ MuseMonitor
    events: object | None = None  # có ``emit(type, task_id, payload)``, ví dụ AgentInbox của router

    def __post_init__(self):
        for name, ad in self.adapters.items():
            if name not in ADAPTER_ALLOWLIST:
                raise ConfigError(f"adapter {name!r} không thuộc allowlist {ADAPTER_ALLOWLIST}")
            if isinstance(ad, StubAdapter) or not isinstance(ad, Adapter):
                raise ConfigError(f"adapter {name!r} là stub hoặc không phải Adapter thật; không bao giờ định tuyến tới")
            if ad.name != name:
                raise ConfigError(f"adapter đăng ký tên {name!r} nhưng là {ad.name!r}")
        for route in self.routing.routes.values():
            if route.adapter not in self.adapters:
                raise ConfigError(f"route {route.difficulty} cần adapter {route.adapter!r} chưa được dựng")

    def select(self, difficulty: str) -> Route:
        if difficulty not in self.routing.routes:
            raise ConfigError(f"độ khó không hợp lệ: {difficulty!r} (chỉ {DIFFICULTIES})")
        return self.routing.routes[difficulty]

    def dispatch(self, difficulty: str, *, task_id: str, attempt: int, call_id: str, prompt: str,
                 agent: str = "implementer", cwd: str | None = None) -> RouteOutcome:
        route = self.select(difficulty)
        adapter = self.adapters[route.adapter]
        head = route.chain[0]
        attempts: list[StepAttempt] = []
        reason: str | None = None
        for i, step in enumerate(route.chain):
            req = CallRequest(
                task_id=task_id, attempt=attempt, call_id=call_id if i == 0 else f"{call_id}.fb{i}",
                prompt=prompt, model=step.model, effort=step.effort, agent=agent, cwd=cwd,
                requested_model=head.model, fallback_reason=reason,
            )
            try:
                result = adapter.run(req)
            except NotAvailable:
                raise  # terminal: không thử bước kế tiếp
            except ServiceUnavailable as exc:
                attempts.append(StepAttempt(route.adapter, step.model, step.effort, req.call_id, str(exc)))
                reason = f"{step.model}/{step.effort or '-'} không phục vụ được: {exc}"[:500]
                continue
            attempts.append(StepAttempt(route.adapter, step.model, step.effort, req.call_id, None))
            return RouteOutcome("ok", route, result, tuple(attempts))
        return self._block(route, task_id, tuple(attempts))

    def _block(self, route: Route, task_id: str, attempts: tuple[StepAttempt, ...]) -> RouteOutcome:
        if len(route.chain) > 1:
            why = f"hết chuỗi fallback {route.adapter} ({len(route.chain)} bước)"
        else:
            why = f"tuyến {route.adapter} sập, không có fallback (R11)"
        reason = f"{why}: {attempts[-1].error}"[:500]
        if self.events is not None:
            self.events.emit("transition", task_id, {"to": "blocked", "reason": reason})
        if self.notifier is not None:
            self.notifier.notify(kind="blocked", task_id=task_id, message=reason, detail={
                "difficulty": route.difficulty, "adapter": route.adapter,
                "attempts": [{"model": a.model, "effort": a.effort, "call_id": a.call_id, "error": a.error}
                             for a in attempts],
            })
        return RouteOutcome("blocked", route, None, attempts, reason)


def default_router(*, prices=None, ledger=None, notifier=None, events=None, **kw) -> Router:
    aliases = ModelAliases.load(DEFAULT_ALIASES_PATH)
    routing = RoutingConfig.load(DEFAULT_ROUTING_PATH, aliases)
    adapters = build_adapters(routing, aliases, prices=prices, ledger=ledger, **kw)
    return Router(routing, adapters, notifier=notifier, events=events)

