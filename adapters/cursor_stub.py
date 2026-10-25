"""Stub Cursor / Fixer-Agent (PR-2). Cursor chưa dùng được (SPEC mục 4).

Không bao giờ được gọi; fix list vào ``awaiting-human`` (R2). Mọi thao tác ném
``NotAvailable`` (terminal: không retry, không fallback, R11). Module này chỉ import
``adapters.base``; test AST kiểm nó không import socket, subprocess hay http.
"""

from __future__ import annotations

from .base import StubAdapter


class CursorStub(StubAdapter):
    name = "cursor"
    reason = "Cursor/Fixer là stub: fix list vào awaiting-human, không bao giờ được gọi"
