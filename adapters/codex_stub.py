"""Stub Codex (PR-2). Chưa có tài khoản và CLI (SPEC mục 4).

Codex KHÔNG BAO GIỜ được gọi và không có trong ``config/routing.yaml``. Mọi thao tác
ném ``NotAvailable`` (terminal: không retry, không fallback, R11). Module này chỉ
import ``adapters.base``; test AST kiểm nó không import socket, subprocess hay http.
"""

from __future__ import annotations

from .base import StubAdapter


class CodexStub(StubAdapter):
    name = "codex"
    reason = "Codex là stub: chưa có tài khoản và CLI, không bao giờ được gọi"
