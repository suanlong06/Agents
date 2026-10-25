"""Fake transport cho test adapter (PR-2). Không gọi CLI hay mạng thật."""

from __future__ import annotations

import json
from pathlib import Path

from adapters.base import ModelAliases
from adapters.transport import CommandResult, RouterLog, TransportTimeout

ALIASES = ModelAliases(
    {"claude-opus-5-5": "anthropic", "claude-sonnet-5-5": "anthropic",
     "deepseek-v4.1-flash-1017": "deepseek", "gemini-3.8-flash-medium": "google"},
    {"xh/deepseek-v4.1-flash-1017": "deepseek-v4.1-flash-1017",
     "google/gemini-3.8-flash-medium": "gemini-3.8-flash-medium"},
)
RID_ENV = "NINEROUTER_REQUEST_ID"


def claude_json(model_usage: dict | None, *, result="xong", session="s-1", is_error=False, **extra) -> str:
    obj = {"type": "result", "subtype": "error_during_execution" if is_error else "success",
           "is_error": is_error, "result": result, "session_id": session}
    if model_usage is not None:
        obj["modelUsage"] = model_usage
    obj.update(extra)
    return json.dumps(obj)


def opus_usage(inp=1000, out=200, model="claude-opus-5-5", cache_read=0, cache_create=0) -> dict:
    return {model: {"inputTokens": inp, "outputTokens": out, "cacheReadInputTokens": cache_read,
                    "cacheCreationInputTokens": cache_create, "costUSD": 0.0}}


class FakeRunner:
    """Thay ``SubprocessRunner``. Mỗi phần tử ``script`` là chuỗi stdout, ``CommandResult``,
    exception để ném, hoặc hàm ``(argv, stdin, env) -> chuỗi | CommandResult``."""

    def __init__(self, *script):
        self.script = list(script)
        self.calls: list[dict] = []

    def __call__(self, argv, *, stdin, timeout_s, env=None, cwd=None):
        self.calls.append({"argv": list(argv), "stdin": stdin, "timeout_s": timeout_s,
                           "env": dict(env or {}), "cwd": cwd})
        if not self.script:
            raise AssertionError(f"FakeRunner hết kịch bản, lời gọi thừa: {argv}")
        step = self.script.pop(0)
        if callable(step) and not isinstance(step, type):
            step = step(argv, stdin, env or {})
        if isinstance(step, BaseException) or (isinstance(step, type) and issubclass(step, BaseException)):
            raise step
        if isinstance(step, CommandResult):
            return step
        return CommandResult(0, step, "")


def timeout() -> TransportTimeout:
    return TransportTimeout("quá timeout (giả lập)")


class FakeRouter9:
    """Giả lập 9Router: ghi log JSONL theo request id mà adapter truyền qua env."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch()
        self.log = RouterLog(self.path, wait_s=0.0)

    def write(self, request_id, model, inp=None, out=None, **extra):
        rec = {"request_id": request_id, "model": model, **extra}
        if inp is not None:
            rec["usage"] = {"prompt_tokens": inp, "completion_tokens": out}
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")

    def serve(self, model, *, inp=None, out=None, stdout="", entries=1):
        """Bước kịch bản FakeRunner: ghi ``entries`` bản ghi log cho request id rồi trả stdout."""

        def step(argv, stdin, env):
            for _ in range(entries):
                self.write(env[RID_ENV], model, inp, out)
            return stdout

        return step
