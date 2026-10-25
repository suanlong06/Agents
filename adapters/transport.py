"""Transport cho adapter thật (PR-2): CLI có timeout, probe HTTP, log 9Router.

- ``SubprocessRunner``: chạy CLI, prompt đi qua **stdin** (không bao giờ nằm trên
  dòng lệnh: tránh giới hạn độ dài và chèn lệnh khi CLI là ``.cmd`` trên Windows),
  timeout mỗi lời gọi, quá hạn thì giết cả cây process.
- ``HttpJsonClient``: POST JSON tới endpoint **loopback** (bridge ``:20129``, 9Router
  ``:20128``), bỏ qua proxy, timeout mỗi lời gọi. Dùng cho probe bằng completion thật.
- ``RouterLog``: đọc log JSONL của 9Router, join theo request id để lấy model THỰC
  SỰ phục vụ và usage theo từng request (vốn là delta).
- ``CliAdapter`` / ``RouterCliAdapter``: lớp nền cho ``claude``, ``dsh``, ``agy``.

⚠️ Tham số CLI, tên biến môi trường mang request id và định dạng log 9Router là
GIẢ ĐỊNH ghi trong ``config/routing.yaml``, phải đối chiếu với bản cài thật (PLAN
PR-2, rủi ro "CLI đổi tham số"). Sai giả định thì kết quả là model "không rõ"
(fail-closed ở Gate 3), không bao giờ là model đoán từ stdout.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence

from .base import (
    Adapter,
    CallRequest,
    CallResult,
    ConfigError,
    CumulativeUsage,
    ModelUsage,
    ServedModel,
    ServiceUnavailable,
    estimate_tokens,
)

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_ALLOWED_PLACEHOLDERS = ("{model}", "{effort}")


class TransportTimeout(ServiceUnavailable):
    pass


def new_request_id() -> str:
    return f"harness-{uuid.uuid4().hex}"


# --- CLI -------------------------------------------------------------------------


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


class SubprocessRunner:
    """Chạy một CLI với timeout. Quá hạn thì giết cả cây process và ném ``TransportTimeout``."""

    def __call__(self, argv: Sequence[str], *, stdin: str, timeout_s: float,
                 env: Mapping[str, str] | None = None, cwd: str | None = None) -> CommandResult:
        exe = shutil.which(argv[0])
        if exe is None:
            raise ServiceUnavailable(f"không tìm thấy CLI {argv[0]!r} trên PATH")
        full_env = dict(os.environ)
        full_env.update(env or {})
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
        try:
            proc = subprocess.Popen(
                [exe, *argv[1:]], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                cwd=cwd, env=full_env, creationflags=flags, encoding="utf-8", errors="replace",
            )
        except OSError as exc:
            raise ServiceUnavailable(f"không chạy được {argv[0]!r}: {exc}") from exc
        try:
            out, err = proc.communicate(stdin, timeout=timeout_s)
        except subprocess.TimeoutExpired:
            self._kill_tree(proc)
            raise TransportTimeout(f"{argv[0]} quá timeout {timeout_s:g}s") from None
        return CommandResult(proc.returncode, out or "", err or "")

    @staticmethod
    def _kill_tree(proc: subprocess.Popen) -> None:
        if sys.platform == "win32":
            # ``.cmd`` của npm chạy qua cmd.exe: kill() chỉ giết cmd.exe, node vẫn sống.
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                           capture_output=True, check=False, timeout=30)
        proc.kill()
        try:
            proc.communicate(timeout=5)
        except (subprocess.TimeoutExpired, ValueError, OSError):
            pass


def parse_json_object(stdout: str) -> dict | None:
    """Metadata có cấu trúc: stdout phải là ĐÚNG MỘT object JSON, không thì ``None``.

    Không dò tìm JSON lẫn trong text: một dòng ``{"model": ...}`` do model tự in ra
    không bao giờ được coi là metadata.
    """
    try:
        obj = json.loads(stdout.strip())
    except (json.JSONDecodeError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


def _int_or_none(x: object) -> int | None:
    if isinstance(x, bool) or not isinstance(x, int) or x < 0:
        return None
    return x


# --- HTTP (probe) ----------------------------------------------------------------


def check_loopback_url(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "http" or (parts.hostname or "") not in LOOPBACK_HOSTS:
        raise ConfigError(f"probe_url phải là http:// tới loopback (bridge/9Router cục bộ): {url!r}")
    return url


class HttpJsonClient:
    """POST JSON tới loopback, bỏ qua proxy, timeout mỗi lời gọi."""

    def __init__(self):
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def post(self, url: str, payload: dict, *, headers: Mapping[str, str], timeout_s: float) -> dict:
        check_loopback_url(url)
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"), method="POST",
            headers={"Content-Type": "application/json", **headers},
        )
        try:
            with self._opener.open(req, timeout=timeout_s) as resp:
                body = resp.read()
        except urllib.error.HTTPError as exc:
            raise ServiceUnavailable(f"{url}: HTTP {exc.code}") from None
        except TimeoutError:
            raise TransportTimeout(f"{url}: quá timeout {timeout_s:g}s") from None
        except (urllib.error.URLError, OSError) as exc:
            raise ServiceUnavailable(f"{url}: {exc}") from None
        try:
            obj = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ServiceUnavailable(f"{url}: phản hồi không phải JSON: {exc}") from None
        if not isinstance(obj, dict):
            raise ServiceUnavailable(f"{url}: phản hồi JSON không phải object")
        return obj


# --- log 9Router -----------------------------------------------------------------


def dig(obj: object, dotted: str) -> object:
    for part in dotted.split("."):
        if not isinstance(obj, dict):
            return None
        obj = obj.get(part)
    return obj


@dataclass(frozen=True)
class RouterLogFormat:
    # GIẢ ĐỊNH về log 9Router, chỉnh trong routing.yaml khi đã đối chiếu log thật.
    request_id_field: str = "request_id"
    model_field: str = "model"
    input_tokens_field: str = "usage.prompt_tokens"
    output_tokens_field: str = "usage.completion_tokens"


class RouterLog:
    """Join log JSONL của 9Router theo request id."""

    def __init__(self, path: Path, fmt: RouterLogFormat | None = None, *, wait_s: float = 2.0,
                 poll_s: float = 0.1, max_scan_bytes: int = 8 << 20,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic):
        self.path = Path(path)
        self.fmt = fmt or RouterLogFormat()
        self.wait_s, self.poll_s, self.max_scan_bytes = wait_s, poll_s, max_scan_bytes
        self._sleep, self._clock = sleep, clock

    def entries(self, request_ids: Sequence[str]) -> list[dict]:
        """Mọi bản ghi mang một trong ``request_ids``. Chờ tối đa ``wait_s`` vì log ghi trễ."""
        wanted = {r for r in request_ids if r}
        if not wanted:
            return []
        deadline = self._clock() + self.wait_s
        while True:
            found = self._scan(wanted)
            if found or self._clock() >= deadline:
                return found
            self._sleep(self.poll_s)

    def _scan(self, wanted: set[str]) -> list[dict]:
        try:
            with open(self.path, "rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                start = max(0, size - self.max_scan_bytes)
                f.seek(start)
                data = f.read()
        except OSError:
            return []
        lines = data.split(b"\n")
        if start > 0:
            lines = lines[1:]  # dòng đầu bị cắt giữa chừng
        out = []
        for raw in lines:
            if not raw.strip():
                continue
            try:
                rec = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue  # dòng ghi dở hoặc hỏng
            if isinstance(rec, dict) and str(dig(rec, self.fmt.request_id_field)) in wanted:
                out.append(rec)
        return out


# --- lớp nền adapter CLI -----------------------------------------------------------


class CliAdapter(Adapter):
    """Adapter chạy một CLI có sẵn. ``command`` chỉ được chứa ``{model}`` và ``{effort}``."""

    def __init__(self, *, command: Sequence[str], runner=None, env: Mapping[str, str] | None = None,
                 usage_mode: str = "delta", **kw):
        super().__init__(**kw)
        if not command or not all(isinstance(a, str) and a for a in command):
            raise ConfigError(f"{self.name}: command phải là list chuỗi khác rỗng")
        for a in command:
            rest = a
            for ph in _ALLOWED_PLACEHOLDERS:
                rest = rest.replace(ph, "")
            if "{" in rest or "}" in rest:
                # Prompt không bao giờ lên dòng lệnh; chỉ có model và effort từ config.
                raise ConfigError(f"{self.name}: command chỉ được dùng {_ALLOWED_PLACEHOLDERS}: {a!r}")
        if usage_mode not in ("delta", "cumulative"):
            raise ConfigError(f"{self.name}: usage phải là delta hoặc cumulative")
        self.command = tuple(command)
        self.runner = runner or SubprocessRunner()
        self.env = dict(env or {})
        self.usage_mode = usage_mode
        self.cumulative = CumulativeUsage()

    def argv(self, request: CallRequest) -> list[str]:
        out: list[str] = []
        for a in self.command:
            if "{effort}" in a and not request.effort:
                if out and out[-1].startswith("-"):
                    out.pop()  # bỏ cả cờ ``--effort`` khi không có effort
                continue
            out.append(a.replace("{model}", request.model).replace("{effort}", request.effort or ""))
        return out

    def execute(self, request: CallRequest, *, timeout_s: float,
                extra_env: Mapping[str, str] | None = None) -> CommandResult:
        res = self.runner(self.argv(request), stdin=request.prompt, timeout_s=timeout_s,
                          env={**self.env, **(extra_env or {})}, cwd=request.cwd)
        if res.returncode != 0:
            tail = (res.stderr.strip() or res.stdout.strip())[-300:]
            raise ServiceUnavailable(f"{self.name}/{request.model}: exit {res.returncode}: {tail}")
        return res

    def to_delta(self, key: tuple, input_tokens: int, output_tokens: int) -> tuple[int, int]:
        if self.usage_mode == "cumulative":
            return self.cumulative.delta(key, input_tokens, output_tokens)
        return input_tokens, output_tokens


class RouterCliAdapter(CliAdapter):
    """CLI đi qua 9Router: model thực sự phục vụ lấy từ log 9Router join theo request id.

    Adapter sinh một request id, truyền cho CLI qua biến môi trường (``request_id_env``)
    và join log theo đúng id đó. Không có bản ghi log khớp thì model là KHÔNG RÕ, kể
    cả khi stdout ghi tên model hay request id nào đó.
    """

    def __init__(self, *, router_log: RouterLog | None = None, request_id_env: Sequence[str] = (),
                 tokens_reported: bool = True, probe_url: str | None = None,
                 request_id_header: str = "X-Request-Id", http: HttpJsonClient | None = None, **kw):
        super().__init__(**kw)
        self.router_log = router_log
        self.request_id_env = tuple(request_id_env)
        self.tokens_reported = tokens_reported
        self.probe_url = check_loopback_url(probe_url) if probe_url else None
        self.request_id_header = request_id_header
        self.http = http or HttpJsonClient()

    def _invoke(self, request: CallRequest, *, timeout_s: float, probe: bool) -> CallResult:
        rid = new_request_id()
        if probe and self.probe_url:
            return self._probe_http(request, rid, timeout_s)
        res = self.execute(request, timeout_s=timeout_s, extra_env={n: rid for n in self.request_id_env})
        meta = parse_json_object(res.stdout)
        # Chỉ join theo request id DO ADAPTER SINH. Không lấy id từ stdout: CLI in text
        # thì stdout chính là text model, model có thể in id của request khác (N4).
        rids = [rid]
        output = meta.get("result") if meta is not None and isinstance(meta.get("result"), str) else res.stdout
        fallback_tokens = self._cli_tokens(request, meta, output)
        usages = self._usages(rids, fallback_tokens)
        session = meta.get("session_id") if meta is not None and isinstance(meta.get("session_id"), str) else None
        return CallResult(self.name, request, usages, output, tuple(rids), session)

    def _cli_tokens(self, request: CallRequest, meta: dict | None, output: str) -> tuple[int, int, bool]:
        """Token khi log 9Router không có: usage CLI (đổi ra delta) hoặc ước lượng."""
        if self.tokens_reported and meta is not None:
            usage = meta.get("usage") if isinstance(meta.get("usage"), dict) else {}
            inp, out = _int_or_none(usage.get("input_tokens")), _int_or_none(usage.get("output_tokens"))
            if inp is not None and out is not None:
                key = (meta.get("session_id") or f"call:{request.call_id}",)
                d_in, d_out = self.to_delta(key, inp, out)
                return d_in, d_out, False
        return estimate_tokens(request.prompt), estimate_tokens(output), True

    def _usages(self, rids: Sequence[str], fallback: tuple[int, int, bool]) -> tuple[ModelUsage, ...]:
        entries = self.router_log.entries(rids) if self.router_log is not None else []
        fmt = self.router_log.fmt if self.router_log is not None else None
        # Gộp theo model ĐÃ PHÂN GIẢI: hai alias của cùng model là một model phục vụ.
        by_model: "OrderedDict[str | None, tuple[ServedModel, list[dict]]]" = OrderedDict()
        for e in entries:
            raw = dig(e, fmt.model_field)
            served = self.aliases.served(raw if isinstance(raw, str) else None, "router_log")
            by_model.setdefault(served.ledger_model, (served, []))[1].append(e)
        if not by_model:
            # Không join được: model KHÔNG RÕ. Không đọc tên model từ stdout (R11).
            inp, out, est = fallback
            return (ModelUsage(ServedModel.unknown(), inp, out, est),)
        usages, first = [], True
        for served, group in by_model.values():
            ins = [_int_or_none(dig(e, fmt.input_tokens_field)) for e in group]
            outs = [_int_or_none(dig(e, fmt.output_tokens_field)) for e in group]
            if all(x is not None for x in ins + outs):
                usages.append(ModelUsage(served, sum(ins), sum(outs), False))
            elif first:
                # Log không có token: dùng usage CLI (đã đổi ra delta) hoặc ước lượng.
                inp, out, est = fallback
                usages.append(ModelUsage(served, inp, out, est or len(by_model) > 1))
            else:
                usages.append(ModelUsage(served, 0, 0, True))
            first = False
        return tuple(usages)

    def _probe_http(self, request: CallRequest, rid: str, timeout_s: float) -> CallResult:
        resp = self.http.post(
            self.probe_url,
            {"model": request.model, "messages": [{"role": "user", "content": request.prompt}],
             "max_tokens": 16, "stream": False},
            headers={self.request_id_header: rid}, timeout_s=timeout_s,
        )
        choices = resp.get("choices")
        text = ""
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            msg = choices[0].get("message")
            text = msg.get("content") if isinstance(msg, dict) and isinstance(msg.get("content"), str) else ""
        usage = resp.get("usage") if isinstance(resp.get("usage"), dict) else {}
        inp, out = _int_or_none(usage.get("prompt_tokens")), _int_or_none(usage.get("completion_tokens"))
        if inp is not None and out is not None:
            fallback = (inp, out, False)
        else:
            fallback = (estimate_tokens(request.prompt), estimate_tokens(text), True)
        usages = self._usages([rid], fallback)
        if not usages[0].served.known and isinstance(resp.get("model"), str):
            # Trường ``model`` của phản hồi là metadata tầng transport (R11), không phải text model in ra.
            usages = (ModelUsage(self.aliases.served(resp["model"], "cli_metadata"), *fallback),)
        return CallResult(self.name, request, usages, text, (rid,), None)
