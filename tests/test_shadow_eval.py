"""Shadow evaluator (PR-4a, R8, SPEC mục 8): 4 điều kiện ứng viên, CHỈ ghi "would-merge" vào
audit, KHÔNG BAO GIỜ gọi merge — chứng minh bằng AST (không có đường gọi merge trong module
và các module của repo mà nó import) và bằng mock (mạng/process/git bị cài bẫy khi chạy)."""

import ast
import json
import re
import shutil
import socket
import subprocess
import tempfile
import unittest
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from orchestrator.audit import AuditLog
from orchestrator.config import REPO_ROOT
from orchestrator.fsio import WriterGuard
from orchestrator.killswitch import KillSwitch, KillSwitchEngaged
from pipeline import github_api
from pipeline.ci_status import CiStatus
from pipeline.gate3 import Gate3
from pipeline.labeler import Change, label_changes
from release import shadow_eval
from release.readonly_check import verify_readonly
from release.shadow_eval import (
    CONDITIONS,
    ShadowError,
    ShadowEvaluator,
    ShadowSettings,
    label_id_of,
    shadow_progress,
)
from tests.killswitch_fixtures import OpenKillSwitch, write_killswitch
from tests.test_gate3 import ALIASES, CFG, DIFF, PASS_OUT, FakeReviewer, row, settings

SHADOW_SRC = REPO_ROOT / "release" / "shadow_eval.py"
KST = ZoneInfo("Asia/Seoul")
BASE = "a" * 40
SHA = "d" * 40
OTHER = "e" * 40
NOW = "2026-10-26T12:00:00.000+09:00"
DOCS_DIFF = """\
diff --git a/docs/guide.md b/docs/guide.md
index 1111111..2222222 100644
--- a/docs/guide.md
+++ b/docs/guide.md
@@ -1 +1,2 @@
 # guide
+Hướng dẫn chạy release thủ công.
"""
DOCS_PASS = json.dumps({"verdict": "pass", "summary": "ok", "findings": [], "evidence": [
    {"file": "docs/guide.md", "line_start": 2, "line_end": 2, "quote": "Hướng dẫn chạy release thủ công."}]})


def green_label(head=SHA):
    return label_changes([Change("M", "docs/guide.md", None, "100644", "100644")], CFG, base_sha=BASE, head_sha=head)


def red_label(head=SHA):
    return label_changes([Change("M", "src/app.py", None, "100644", "100644")], CFG, base_sha=BASE, head_sha=head)


def pass_verdict(lbl, sha=SHA, *, diff=DOCS_DIFF, output=DOCS_PASS) -> dict:
    """Verdict THẬT từ Gate 3 (reviewer giả trả pass có bằng chứng)."""
    adapters = {n: FakeReviewer(n, output=output) for n in ("claude", "dsh", "agy")}
    g = Gate3(aliases=ALIASES, adapters=adapters, settings=settings(), clock=lambda: NOW,
              killswitch=OpenKillSwitch())
    out = g.review(task_id="T-1", attempt=1, sha=sha, label=lbl, diff=diff,
                   ledger_records=[row("claude-opus-5-5")], pr=7)
    assert out.verdict["verdict"] == "pass", out.verdict
    return out.verdict


def ci_ok(sha=SHA, state="success"):
    return CiStatus(sha, state, "ci", 99, "success" if state == "success" else None)


RESOLVED = [{"id": "t1", "is_resolved": True}, {"id": "t2", "isResolved": True}]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="harness-shadow-")).resolve()
        self.state_dir = self.tmp / "state"
        self.state_dir.mkdir()
        self.guard = WriterGuard(self.state_dir).acquire()
        self.audit = AuditLog(self.state_dir, self.guard, lambda: NOW)
        self.ks = KillSwitch(write_killswitch(self.state_dir / "killswitch.json", "off"))
        self.ev = ShadowEvaluator(audit=self.audit, killswitch=self.ks, clock=lambda: NOW)
        self.label = green_label()
        self.verdict = pass_verdict(self.label)

    def tearDown(self):
        self.guard.release()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def inputs(self, **over):
        d = dict(pr=7, sha=SHA, ci=ci_ok(), verdict=self.verdict, label=self.label,
                 review_threads=list(RESOLVED), task_id="T-1", attempt=1)
        d.update(over)
        return d

    def records(self):
        return AuditLog.read(self.audit.path)


class ConditionsTest(Base):
    def test_all_four_conditions_pass_records_would_merge_yes(self):
        d = self.ev.run(**self.inputs())
        self.assertTrue(d.would_merge)
        self.assertEqual([c.name for c in d.conditions], list(CONDITIONS))
        recs = self.records()
        self.assertEqual(len(recs), 1)
        r = recs[0]
        self.assertEqual((r["action"], r["result"], r["agent"], r["sha"], r["pr"]),
                         ("would-merge", "yes", "shadow-eval", SHA, 7))
        self.assertEqual(r["detail"]["auto_merge"], "disabled")
        self.assertEqual(r["detail"]["mode"], "shadow")
        self.assertEqual(r["detail"]["failed"], [])

    def test_each_condition_alone_blocks_would_merge(self):
        cases = {
            "ci_green": [dict(ci=None), dict(ci=ci_ok(state="failure")), dict(ci=ci_ok(state="pending")),
                         dict(ci=ci_ok(sha=OTHER))],
            "gate3_pass": [dict(verdict=None), dict(verdict={**self.verdict, "verdict": "fail"}),
                           dict(verdict={**self.verdict, "sha": OTHER}), dict(verdict={"verdict": "pass"}),
                           dict(verdict={**self.verdict, "label": {**self.verdict["label"], "label_id": "0" * 64}})],
            "not_sensitive": [dict(label=None), dict(label=green_label(head=OTHER))],
            "no_unresolved_comments": [dict(review_threads=None),
                                       dict(review_threads=RESOLVED + [{"id": "t3", "is_resolved": False}]),
                                       dict(review_threads=[{"id": "t4"}]),
                                       dict(review_threads=[{"id": "t5", "is_resolved": "true"}])],
        }
        for cond, variants in cases.items():
            for i, over in enumerate(variants):
                with self.subTest(cond=cond, variant=i):
                    d = self.ev.evaluate(**self.inputs(**over))
                    self.assertFalse(d.would_merge)
                    failed = [c.name for c in d.conditions if not c.passed]
                    self.assertIn(cond, failed)

    def test_red_or_owner_only_label_never_would_merge(self):
        lbl = red_label()
        d = self.ev.evaluate(**self.inputs(label=lbl, verdict=pass_verdict(lbl, diff=DIFF, output=PASS_OUT)))
        self.assertFalse(d.would_merge)
        self.assertEqual([c.name for c in d.conditions if not c.passed], ["not_sensitive"])

    def test_tampered_label_is_rejected(self):
        tampered = self.label.to_dict()
        tampered["tier"] = "green"
        tampered["files"] = [dict(f, tier="green") for f in red_label().to_dict()["files"]]
        d = self.ev.evaluate(**self.inputs(label=tampered))
        self.assertFalse(d.would_merge)
        self.assertIn("bị sửa", d.conditions[2].reason)
        self.assertEqual(label_id_of(self.label.to_dict()), self.label.label_id)

    def test_no_review_threads_counts_as_resolved_only_when_list_is_given(self):
        self.assertTrue(self.ev.evaluate(**self.inputs(review_threads=[])).would_merge)
        self.assertFalse(self.ev.evaluate(**self.inputs(review_threads=None)).would_merge)

    def test_invalid_pr_or_sha_raise(self):
        for over in (dict(pr=0), dict(pr=True), dict(sha="D" * 40), dict(sha="abc")):
            with self.subTest(over=over), self.assertRaises(ShadowError):
                self.ev.evaluate(**self.inputs(**over))

    def test_alt_diff_limit_is_informational_only(self):
        d = self.ev.evaluate(**self.inputs(diff_stats={"lines": 500, "files": 3, "touches_src": False,
                                                       "has_tests": False}))
        self.assertTrue(d.would_merge)  # không ảnh hưởng quyết định
        self.assertFalse(d.alt_diff_limit["passed"])
        d2 = self.ev.evaluate(**self.inputs(diff_stats={"lines": 50, "files": 2, "touches_src": True,
                                                        "has_tests": True}))
        self.assertTrue(d2.alt_diff_limit["passed"])
        d3 = self.ev.evaluate(**self.inputs(diff_stats={"lines": 50}))
        self.assertFalse(d3.alt_diff_limit["passed"])

    def test_record_is_idempotent_per_sha_and_outcome(self):
        self.ev.run(**self.inputs(ci=ci_ok(state="pending")))
        self.ev.run(**self.inputs(ci=ci_ok(state="pending")))  # cùng kết quả: không ghi thêm
        self.ev.run(**self.inputs())  # CI xong: kết quả mới → bản ghi mới
        recs = self.records()
        self.assertEqual([r["result"] for r in recs], ["no", "yes"])
        self.assertEqual(recs[0]["detail"]["failed"], ["ci_green"])

    def test_only_action_ever_written_is_would_merge(self):
        for over in (dict(), dict(ci=None), dict(label=None), dict(review_threads=None)):
            self.ev.run(**self.inputs(**over))
        self.assertEqual({r["action"] for r in self.records()}, {"would-merge"})


class KillSwitchTest(Base):
    def test_engaged_killswitch_stops_before_evaluating_or_recording(self):
        (self.state_dir / "killswitch.json").unlink()  # thiếu file → bật
        with self.assertRaises(KillSwitchEngaged):
            self.ev.run(**self.inputs())
        d = self.ev.evaluate(**self.inputs())
        with self.assertRaises(KillSwitchEngaged):
            self.ev.record(d)
        self.assertEqual(self.records(), [])

    def test_killswitch_is_required(self):
        with self.assertRaises(ShadowError):
            ShadowEvaluator(audit=self.audit, killswitch=None)
        with self.assertRaises(ShadowError):
            ShadowEvaluator(audit=None, killswitch=self.ks)


class ProgressTest(Base):
    def test_thresholds_two_weeks_or_twenty_prs_and_never_enable_merge(self):
        start = datetime(2026, 10, 1, 9, 0, tzinfo=KST)
        recs = [{"action": "would-merge", "agent": "shadow-eval", "pr": n, "result": "yes",
                 "ts": (start + timedelta(hours=n)).isoformat()} for n in range(1, 6)]
        recs.append({"action": "event.transition", "agent": "x", "pr": 99, "ts": start.isoformat()})
        p = shadow_progress(recs, now=start + timedelta(days=3))
        self.assertEqual((p["prs"], p["threshold_reached"], p["auto_merge"]), (5, False, "disabled"))
        p = shadow_progress(recs, now=start + timedelta(days=14, hours=1))
        self.assertEqual((p["threshold_reached"], p["auto_merge"]), (True, "disabled"))
        many = [dict(r, pr=n) for n, r in enumerate(recs[:1] * 20, start=1)]
        p = shadow_progress(many, now=start + timedelta(days=1))
        self.assertEqual((p["prs"], p["threshold_reached"], p["auto_merge"]), (20, True, "disabled"))
        self.assertFalse(shadow_progress([], now=start)["threshold_reached"])

    def test_settings_come_from_config_as_temporary_defaults(self):
        s = ShadowSettings.load()
        self.assertEqual((s.min_days, s.min_prs), (14, 20))


# --- chứng minh không có đường gọi merge ---------------------------------------------------

# Module mạng, process, git hay pipeline có quyền gọi GitHub/git: shadow_eval và mọi module
# của repo mà nó import (bắc cầu) không được import chúng.
FORBIDDEN_IMPORTS = {
    "subprocess", "socket", "http", "urllib", "urllib3", "requests", "httpx", "aiohttp", "ssl", "ftplib",
    "smtplib", "xmlrpc", "socketserver", "multiprocessing", "ctypes", "importlib", "webbrowser", "pty",
    "asyncio", "concurrent", "pipeline.github_api", "pipeline.labeler", "pipeline.gate3",
    "pipeline.approvals", "pipeline.ci_status", "pipeline.fix_loop", "adapters", "release.manifest",
    "orchestrator.worktree",
}
FORBIDDEN_BUILTINS = {"__import__", "eval", "exec", "compile", "breakpoint"}
OS_PROCESS_CALLS = re.compile(r"^(system|popen|startfile|spawn\w*|exec\w*|fork\w*|kill\w*|posix_spawn\w*)$")
MERGE_STRINGS = re.compile(r"/merge\b|merge_method|mergepullrequest|automerge|\bgit\s+merge\b|/pulls/\d+/merge",
                           re.IGNORECASE)
REPO_PACKAGES = {"orchestrator", "pipeline", "release", "adapters"}


def _resolve(name: str, pkg: str, level: int) -> str:
    if level == 0:
        return name
    base = pkg.split(".")
    if level > 1:
        base = base[: len(base) - (level - 1)]
    return ".".join([*base, name] if name else base)


def _module_file(name: str) -> Path | None:
    parts = name.split(".")
    if parts[0] not in REPO_PACKAGES:
        return None
    p = REPO_ROOT.joinpath(*parts)
    if p.with_suffix(".py").is_file():
        return p.with_suffix(".py")
    if (p / "__init__.py").is_file():
        return p / "__init__.py"
    return None


def _package(path: Path) -> str:
    rel = path.relative_to(REPO_ROOT).with_suffix("")
    parts = list(rel.parts)
    parts.pop()
    return ".".join(parts)


def _callee(node: ast.Call) -> str | None:
    f = node.func
    if isinstance(f, ast.Attribute):
        return f.attr
    if isinstance(f, ast.Name):
        return f.id
    return None


def scan(path: Path) -> tuple[set[str], list[str], set[Path]]:
    """(module được import, vi phạm, module repo cần duyệt tiếp)."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports, problems, nxt = set(), [], set()
    pkg = _package(path)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            base = _resolve(node.module or "", pkg, node.level)
            mods = [base] + [f"{base}.{a.name}" for a in node.names]
        else:
            mods = []
        for m in mods:
            imports.add(m)
            f = _module_file(m)
            if f is not None:
                nxt.add(f)
        if isinstance(node, ast.Call):
            name = _callee(node)
            if name and "merge" in name.casefold():
                problems.append(f"{path.name}:{node.lineno}: gọi {name}()")
            if isinstance(node.func, ast.Name) and node.func.id in FORBIDDEN_BUILTINS:
                problems.append(f"{path.name}:{node.lineno}: gọi {node.func.id}()")
            if isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name) \
                    and node.func.value.id == "os" and OS_PROCESS_CALLS.match(node.func.attr):
                problems.append(f"{path.name}:{node.lineno}: os.{node.func.attr}()")
            if isinstance(node.func, ast.Name) and node.func.id == "getattr" and len(node.args) >= 2:
                attr = node.args[1]
                if not (isinstance(attr, ast.Constant) or (isinstance(attr, ast.Name) and attr.id == "key")):
                    problems.append(f"{path.name}:{node.lineno}: getattr động")
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and MERGE_STRINGS.search(node.value):
            problems.append(f"{path.name}:{getattr(node, 'lineno', '?')}: chuỗi merge API {node.value[:60]!r}")
    for m in imports:
        if any(m == f or m.startswith(f + ".") for f in FORBIDDEN_IMPORTS):
            problems.append(f"{path.name}: import {m}")
    return imports, problems, nxt


class NoMergePathAstTest(unittest.TestCase):
    def test_shadow_eval_and_its_repo_import_closure_have_no_merge_call_path(self):
        seen, todo, problems = set(), [SHADOW_SRC], []
        while todo:
            cur = todo.pop()
            if cur in seen:
                continue
            seen.add(cur)
            _imports, probs, nxt = scan(cur)
            problems.extend(probs)
            todo.extend(nxt - seen)
        self.assertIn(SHADOW_SRC, seen)
        self.assertIn(REPO_ROOT / "pipeline" / "common.py", seen)  # closure thật sự được duyệt
        self.assertEqual(problems, [])

    def test_scanner_catches_a_merge_call(self):
        # Tự kiểm bộ quét: một module có lời gọi merge thì phải bị bắt.
        tmp = Path(tempfile.mkdtemp(prefix="harness-ast-")).resolve()
        try:
            bad = REPO_ROOT / "release" / "shadow_eval.py"
            src = bad.read_text(encoding="utf-8") + (
                "\n\ndef _oops(client, n):\n    import subprocess\n"
                "    client.merge_pull_request(n)\n    return client.put(f'pulls/{n}/merge')\n")
            fake = tmp / "release"
            fake.mkdir()
            (fake / "shadow_eval.py").write_text(src, encoding="utf-8")
            with mock.patch(f"{__name__}.REPO_ROOT", tmp):
                _i, probs, _n = scan(fake / "shadow_eval.py")
            joined = "\n".join(probs)
            self.assertIn("merge_pull_request", joined)
            self.assertIn("import subprocess", joined)
            self.assertIn("chuỗi merge API", joined)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_evaluator_api_has_no_merge_capability(self):
        for obj in (shadow_eval, ShadowEvaluator):
            names = [n for n in dir(obj) if "merge" in n.casefold() and callable(getattr(obj, n))]
            self.assertEqual(names, [], obj)
        # Constructor không nhận client GitHub hay hàm merge nào.
        import inspect

        params = set(inspect.signature(ShadowEvaluator.__init__).parameters)
        self.assertEqual(params, {"self", "audit", "killswitch", "settings", "timezone", "clock"})
        # Client GitHub của pipeline cũng chỉ có GET.
        self.assertEqual([n for n in dir(github_api.GitHubClient) if "merge" in n.casefold()], [])


class NoMergePathMockTest(Base):
    """Chạy evaluator với cả 4 điều kiện đạt trong lúc mạng, process và git bị cài bẫy."""

    def test_would_merge_yes_triggers_no_network_process_or_git(self):
        trap = mock.Mock(side_effect=AssertionError("shadow evaluator không được ra mạng/chạy process"))
        audit_spy = mock.Mock(spec=["append"], wraps=self.audit)
        audit_spy.append.side_effect = self.audit.append
        ev = ShadowEvaluator(audit=audit_spy, killswitch=self.ks, clock=lambda: NOW)
        with mock.patch.object(urllib.request, "urlopen", trap), \
                mock.patch("http.client.HTTPConnection.request", trap), \
                mock.patch("http.client.HTTPSConnection.request", trap), \
                mock.patch.object(socket.socket, "connect", trap), \
                mock.patch.object(subprocess, "Popen", trap), \
                mock.patch.object(subprocess, "run", trap), \
                mock.patch.object(github_api.GitHubClient, "get", trap), \
                mock.patch.object(github_api, "urllib_get", trap):
            d = ev.run(**self.inputs())
        self.assertTrue(d.would_merge)
        trap.assert_not_called()
        self.assertEqual(audit_spy.mock_calls[0][0], "append")
        self.assertEqual(len(audit_spy.mock_calls), 1)
        self.assertEqual(audit_spy.append.call_args.kwargs["action"], "would-merge")
        self.assertEqual([r["result"] for r in self.records()], ["yes"])

    def test_evaluate_is_readonly_under_the_readonly_check(self):
        # Kiểm tra read-only (bước 11) xác nhận: đánh giá không ghi, không mạng, không process.
        rep = verify_readonly(self.ev.evaluate, watch=[self.state_dir], **self.inputs())
        self.assertTrue(rep.ok, rep)
        self.assertTrue(rep.result.would_merge)


if __name__ == "__main__":
    unittest.main()
