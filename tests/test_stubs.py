"""Stub Codex/Cursor (PR-2, R11): ném NotAvailable, không import socket/subprocess/http (AST)."""

import ast
import sys
import unittest
from pathlib import Path

from adapters.base import CallRequest, NotAvailable, ServiceUnavailable
from adapters.codex_stub import CodexStub
from adapters.cursor_stub import CursorStub
from orchestrator.config import REPO_ROOT

STUBS = (REPO_ROOT / "adapters" / "codex_stub.py", REPO_ROOT / "adapters" / "cursor_stub.py")
# Module mở mạng, chạy process hay nạp code động. Kiểm cả gói con (http.client...).
FORBIDDEN = {
    "socket", "subprocess", "http", "urllib", "urllib3", "requests", "httpx", "aiohttp", "asyncio",
    "ssl", "ftplib", "smtplib", "telnetlib", "xmlrpc", "socketserver", "multiprocessing", "ctypes",
    "importlib", "pty", "webbrowser", "select", "selectors", "os", "shutil", "uuid",
}
FORBIDDEN_CALLS = {"__import__", "eval", "exec", "compile"}
REPO_PACKAGES = {"adapters", "orchestrator"}


def _module_path(name: str) -> Path | None:
    parts = name.split(".")
    if parts[0] not in REPO_PACKAGES:
        return None
    p = REPO_ROOT.joinpath(*parts)
    if p.with_suffix(".py").is_file():
        return p.with_suffix(".py")
    if (p / "__init__.py").is_file():
        return p / "__init__.py"
    return None


def _package_of(path: Path) -> str:
    rel = path.relative_to(REPO_ROOT).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts.pop()
    else:
        parts.pop()
    return ".".join(parts)


def import_closure(path: Path) -> tuple[dict[Path, set[str]], dict[Path, set[str]]]:
    """Duyệt AST: mọi module của repo mà ``path`` import (bắc cầu), và import/lời gọi của từng module."""
    imports: dict[Path, set[str]] = {}
    calls: dict[Path, set[str]] = {}
    todo = [path]
    while todo:
        cur = todo.pop()
        if cur in imports:
            continue
        tree = ast.parse(cur.read_text(encoding="utf-8"), filename=str(cur))
        names: set[str] = set()
        called: set[str] = set()
        pkg = _package_of(cur)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    base = pkg.split(".")[: len(pkg.split(".")) - (node.level - 1)]
                    mod = ".".join(base + ([node.module] if node.module else []))
                else:
                    mod = node.module or ""
                names.add(mod)
                names.update(f"{mod}.{a.name}" for a in node.names)  # ``from adapters import base``
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                called.add(node.func.id)
        imports[cur] = names
        calls[cur] = called
        for n in names:
            p = _module_path(n)
            if p is not None and p not in imports:
                todo.append(p)
    return imports, calls


def forbidden_in(path: Path) -> list[str]:
    imports, calls = import_closure(path)
    bad = []
    for mod, names in imports.items():
        for n in names:
            if n.split(".")[0] in FORBIDDEN:
                bad.append(f"{mod.name}: import {n}")
        for c in calls[mod] & FORBIDDEN_CALLS:
            bad.append(f"{mod.name}: gọi {c}()")
    return bad


class StubStaticTest(unittest.TestCase):
    def test_stubs_import_no_network_or_process_modules_transitively(self):
        for stub in STUBS:
            with self.subTest(stub=stub.name):
                imports, _ = import_closure(stub)
                self.assertIn(REPO_ROOT / "adapters" / "base.py", imports)
                self.assertEqual(forbidden_in(stub), [])

    def test_checker_catches_forbidden_imports(self):
        # Đối chứng: module transport thật có import subprocess/urllib, checker phải bắt.
        bad = forbidden_in(REPO_ROOT / "adapters" / "transport.py")
        self.assertTrue(any("subprocess" in b for b in bad))
        self.assertTrue(any("urllib" in b for b in bad))

    def test_loading_stubs_does_not_load_network_modules(self):
        # Kiểm lúc chạy trong process sạch: import stub không kéo socket/subprocess/http.
        import subprocess  # chỉ trong test

        code = ("import sys; import adapters.codex_stub, adapters.cursor_stub; "
                "bad=[m for m in ('socket','subprocess','http.client','urllib.request','ssl') if m in sys.modules]; "
                "print(','.join(bad))")
        out = subprocess.run([sys.executable, "-I", "-c", f"import sys; sys.path.insert(0, {str(REPO_ROOT)!r}); {code}"],
                             capture_output=True, text=True, timeout=60, check=True)
        self.assertEqual(out.stdout.strip(), "")


class StubRuntimeTest(unittest.TestCase):
    REQ = CallRequest(task_id="T-1", attempt=1, call_id="c-1", prompt="x", model="any")

    def test_every_operation_raises_not_available(self):
        for stub in (CodexStub(), CursorStub()):
            for op in (lambda s: s.run(self.REQ), lambda s: s.status(), lambda s: s.cost(None),
                       lambda s: s.probe("any"), lambda s: s._invoke(self.REQ, timeout_s=1, probe=False)):
                with self.subTest(stub=stub.name), self.assertRaises(NotAvailable):
                    op(stub)

    def test_not_available_is_terminal_not_a_transient_error(self):
        self.assertTrue(NotAvailable.terminal)
        self.assertFalse(issubclass(NotAvailable, ServiceUnavailable))
        self.assertFalse(issubclass(ServiceUnavailable, NotAvailable))


if __name__ == "__main__":
    unittest.main()
