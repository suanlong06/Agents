"""Kill-switch fail-closed (PR-4a, R8, R15): thiếu/hỏng/không đọc được → BẬT; chỉ đọc, không
có hàm ghi; bật thì mọi hành động tự động (model, Gate 3, vòng fix, phản chiếu duyệt) dừng
trước mọi hiệu ứng."""

import ast
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from adapters.base import CallRequest
from adapters.router import Router, RoutingConfig, build_adapters
from orchestrator import killswitch as ks_mod
from orchestrator.config import REPO_ROOT, FixPolicy
from orchestrator.fsio import WriterGuard
from orchestrator.killswitch import KillSwitch, KillSwitchEngaged, read_status
from pipeline.approvals import ApprovalMirror
from pipeline.ci_status import CiStatus
from pipeline.fix_loop import FixLoop
from pipeline.gate3 import Gate3
from pipeline.github_api import GitHubClient
from tests.killswitch_fixtures import ks_record, write_killswitch
from tests.test_fix_loop import task_at, verdict
from tests.test_gate3 import ALIASES, DIFF, FakeReviewer, ListSink, label, row, settings

KS_SOURCE = REPO_ROOT / "orchestrator" / "killswitch.py"


class TmpDir(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="harness-ks-")).resolve()
        self.path = self.tmp / "state" / "killswitch.json"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_raw(self, data: bytes) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_bytes(data)


class FailClosedTest(TmpDir):
    def assertEngaged(self, reason_part=None):
        st = KillSwitch(self.path).status()
        self.assertTrue(st.engaged, st)
        self.assertEqual(st.state, "on")
        if reason_part:
            self.assertIn(reason_part, st.reason)
        with self.assertRaises(KillSwitchEngaged) as cm:
            KillSwitch(self.path).check("test.action")
        self.assertEqual(cm.exception.action, "test.action")
        return st

    def test_missing_file_is_on(self):
        self.assertEngaged("không tồn tại")

    def test_missing_directory_is_on(self):
        self.path = self.tmp / "khong-co" / "x" / "killswitch.json"
        self.assertEngaged("không tồn tại")

    def test_explicit_off_by_user_is_the_only_way_to_release(self):
        write_killswitch(self.path, "off")
        st = KillSwitch(self.path).check("test.action")
        self.assertFalse(st.engaged)
        self.assertEqual((st.state, st.set_by), ("off", "user"))

    def test_explicit_on_is_on(self):
        write_killswitch(self.path, "on")
        self.assertEngaged("bật")

    def test_broken_contents_are_on(self):
        cases = {
            "rỗng": b"",
            "khoảng trắng": b"   \n",
            "JSON hỏng": b'{"v": 1, "state": "off"',
            "rác nhị phân": b"\xff\xfe\x00garbage",
            "mảng": b'["off"]',
            "chuỗi": b'"off"',
            "null": b"null",
            "UTF-16": json.dumps(ks_record()).encode("utf-16"),
        }
        for name, data in cases.items():
            with self.subTest(name):
                self.write_raw(data)
                self.assertEngaged("không đọc được")

    def test_wrong_schema_is_on(self):
        cases = {
            "version lạ": ks_record(v=2),
            "version bool": ks_record(v=True),
            "version chuỗi": ks_record(v="1"),
            "OFF hoa": ks_record("OFF"),
            "disabled": ks_record("disabled"),
            "state false": ks_record(False),
            "agent ghi": ks_record(set_by="implementer"),
            "dashboard ghi": ks_record(set_by="dashboard"),
            "trường lạ": ks_record(extra=1),
            "set_at rỗng": ks_record(set_at=" "),
            "reason số": ks_record(reason=1),
        }
        for name, rec in cases.items():
            with self.subTest(name):
                self.write_raw(json.dumps(rec).encode("utf-8"))
                self.assertEngaged()
        for missing in ("v", "state", "set_by", "set_at"):
            with self.subTest(missing=missing):
                rec = ks_record()
                del rec[missing]
                self.write_raw(json.dumps(rec).encode("utf-8"))
                self.assertEngaged("thiếu")

    def test_duplicate_keys_are_ambiguous_so_on(self):
        self.write_raw(b'{"v": 1, "state": "on", "set_by": "user", "set_at": "x", "state": "off"}')
        self.assertEngaged("lặp lại")

    def test_oversized_file_is_on(self):
        self.write_raw(json.dumps(ks_record(reason="x" * (ks_mod.MAX_BYTES + 10))).encode("utf-8"))
        self.assertEngaged("lớn hơn")

    def test_directory_instead_of_file_is_on(self):
        self.path.mkdir(parents=True)
        self.assertEngaged("không phải file thường")

    def test_unreadable_file_is_on(self):
        write_killswitch(self.path, "off")
        for exc in (PermissionError(13, "Access is denied"), OSError(32, "sharing violation"), MemoryError()):
            with self.subTest(type(exc).__name__), mock.patch("builtins.open", side_effect=exc):
                self.assertEngaged("không đọc được")

    def test_read_status_never_raises(self):
        write_killswitch(self.path, "off")
        with mock.patch.object(ks_mod.os, "lstat", side_effect=RuntimeError("bất ngờ")):
            self.assertTrue(read_status(self.path).engaged)
        for bad in (None, 123, "relative/killswitch.json", Path("killswitch.json")):
            with self.subTest(bad=bad):
                self.assertTrue(read_status(bad).engaged)

    def test_path_inside_repo_is_on(self):
        st = read_status(REPO_ROOT / "killswitch.json")
        self.assertTrue(st.engaged)
        self.assertIn("trong repo", st.reason)

    def test_symlink_is_on(self):
        target = write_killswitch(self.tmp / "elsewhere.json", "off")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.symlink(target, self.path)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"không tạo được symlink (cần quyền trên Windows): {exc}")
        self.assertEngaged("symlink")

    def test_utf8_bom_from_windows_powershell_is_accepted(self):
        self.write_raw(b"\xef\xbb\xbf" + json.dumps(ks_record()).encode("utf-8"))
        self.assertFalse(KillSwitch(self.path).status().engaged)

    def test_reread_on_every_check_no_cache(self):
        ks = KillSwitch(self.path)
        self.assertTrue(ks.engaged)
        write_killswitch(self.path, "off")
        self.assertFalse(ks.engaged)
        write_killswitch(self.path, "on")
        with self.assertRaises(KillSwitchEngaged):
            ks.check("x")
        self.path.unlink()  # xóa file = bật (phanh khẩn cấp)
        self.assertTrue(ks.engaged)


class ConfigTest(TmpDir):
    def test_default_path_is_inside_state_dir_outside_repo(self):
        state = self.tmp / "state"
        ks = KillSwitch.from_config(env={"HARNESS_STATE_DIR": str(state)})
        self.assertEqual(ks.path, state / "killswitch.json")
        self.assertTrue(ks.engaged)  # chưa có file → bật
        write_killswitch(ks.path, "off")
        self.assertFalse(ks.engaged)

    def test_broken_config_is_on(self):
        bad = self.tmp / "harness.yaml"
        bad.write_text("state_dir: [không: hợp lệ\n", encoding="utf-8")
        ks = KillSwitch.from_config(bad, env={"HARNESS_STATE_DIR": str(self.tmp / "state")})
        self.assertIsNone(ks.path)
        self.assertTrue(ks.engaged)
        ks2 = KillSwitch.from_config(env={})  # %LOCALAPPDATA% chưa đặt
        if ks2.path is None:
            self.assertTrue(ks2.engaged)

    def test_cli_exit_code(self):
        state = self.tmp / "state"
        with mock.patch.dict(os.environ, {"HARNESS_STATE_DIR": str(state)}), \
                mock.patch("builtins.print"):
            self.assertEqual(ks_mod.main([]), 3)
            write_killswitch(state / "killswitch.json", "off")
            self.assertEqual(ks_mod.main([]), 0)


class NoWriteApiTest(unittest.TestCase):
    """Chỉ người dùng đổi kill-switch: module không có thao tác ghi file nào (AST)."""

    WRITE_ATTRS = {"write", "write_text", "write_bytes", "replace", "rename", "unlink", "remove", "mkdir",
                   "makedirs", "rmdir", "touch", "chmod", "truncate", "atomic_write_bytes", "append_line",
                   "dump", "symlink_to", "rmtree", "copy", "copyfile", "move"}

    def test_module_has_no_write_operations(self):
        tree = ast.parse(KS_SOURCE.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None)
                self.assertNotIn(name, self.WRITE_ATTRS, f"dòng {node.lineno}: {name}()")
                if name == "open":
                    modes = [a for a in node.args[1:2]] + [k.value for k in node.keywords if k.arg == "mode"]
                    for m in modes:
                        self.assertIsInstance(m, ast.Constant, f"dòng {node.lineno}: mode open phải là hằng")
                        self.assertFalse(set(m.value) & set("wax+"), f"dòng {node.lineno}: open({m.value!r})")
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [a.name for a in node.names] + [getattr(node, "module", None) or ""]
                for n in names:
                    self.assertNotIn(n, ("shutil", "fsio", "atomic_write_bytes", "append_line", "tempfile"))

    def test_killswitch_object_exposes_no_setter(self):
        public = {n for n in dir(KillSwitch) if not n.startswith("_")}
        self.assertEqual(public, {"from_config", "status", "engaged", "check"})


class IntegrationTest(TmpDir):
    """Kill-switch bật → hành động dừng TRƯỚC mọi hiệu ứng; tắt → chạy như cũ."""

    def setUp(self):
        super().setUp()
        self.ks = KillSwitch(self.path)  # chưa có file → BẬT

    def test_adapter_run_and_probe_stop_before_any_model_call(self):
        sink = ListSink()
        ad = FakeReviewer("dsh", sink=sink)
        ad.killswitch = self.ks
        req = CallRequest(task_id="T-1", attempt=1, prompt="x", model="deepseek-v4.1-flash-1017", call_id="c1")
        with self.assertRaises(KillSwitchEngaged):
            ad.run(req)
        with self.assertRaises(KillSwitchEngaged):
            ad.probe("deepseek-v4.1-flash-1017")
        self.assertEqual((ad.calls, sink.rows), ([], []))
        write_killswitch(self.path, "off")
        ad.run(req)
        self.assertEqual(len(ad.calls), 1)
        self.assertEqual(len(sink.rows), 1)

    def test_build_adapters_and_router_honour_killswitch(self):
        routing = RoutingConfig.load(aliases=ALIASES)
        calls = []

        def runner(argv, **kw):
            calls.append(argv)
            raise AssertionError("không được chạy CLI khi kill-switch bật")

        adapters = build_adapters(routing, ALIASES, runner=runner, killswitch=self.ks)
        self.assertTrue(adapters)
        for ad in adapters.values():
            self.assertIs(ad.killswitch, self.ks)

        class Events:
            def __init__(self):
                self.emitted = []

            def emit(self, *a, **kw):
                self.emitted.append(a)

        ev = Events()
        router = Router(routing, adapters, events=ev)
        for difficulty in ("hard", "medium", "easy"):
            with self.subTest(difficulty), self.assertRaises(KillSwitchEngaged):
                router.dispatch(difficulty, task_id="T-1", attempt=1, call_id="c", prompt="x")
        self.assertEqual((calls, ev.emitted), ([], []))  # không gọi CLI, không chuyển blocked

    def test_gate3_review_stops_before_reviewer_and_events(self):
        adapters = {n: FakeReviewer(n) for n in ("claude", "dsh", "agy")}

        class Events:
            emitted = []

            def emit(self, *a, **kw):
                self.emitted.append(a)

        g = Gate3(aliases=ALIASES, adapters=adapters, settings=settings(), events=Events(), killswitch=self.ks)
        with self.assertRaises(KillSwitchEngaged):
            g.review(task_id="T-1", attempt=1, sha="c" * 40, label=label(), diff=DIFF,
                     ledger_records=[row("claude-opus-5-5")], pr=7)
        self.assertEqual(sum(len(a.calls) for a in adapters.values()), 0)
        self.assertEqual(Events.emitted, [])
        write_killswitch(self.path, "off")
        out = g.review(task_id="T-1", attempt=1, sha="c" * 40, label=label(), diff=DIFF,
                       ledger_records=[row("claude-opus-5-5")], pr=7)
        self.assertEqual(out.verdict["verdict"], "pass")

    def test_fix_loop_stops_before_emitting(self):
        class Events:
            agent = "fix-loop"
            emitted = []

            def emit(self, *a, **kw):
                self.emitted.append(a)

        loop = FixLoop(FixPolicy(), events=Events(), killswitch=self.ks)
        with self.assertRaises(KillSwitchEngaged):
            loop.on_verdict(task_at(), verdict(), [])
        ci = CiStatus("1" * 40, "failure", "ci", 1, "failure")
        with self.assertRaises(KillSwitchEngaged):
            loop.on_ci_failure(task_at(state="ci_check"), ci, [])
        with self.assertRaises(KillSwitchEngaged):
            loop.open_fix_pr(task_at(state="awaiting-human"), pr=11, sha="2" * 40)
        self.assertEqual(Events.emitted, [])
        write_killswitch(self.path, "off")
        self.assertEqual(loop.on_verdict(task_at(), verdict(), []).state, "awaiting-human")

    def test_approval_mirror_sync_stops_before_github(self):
        requests = []

        def http_get(url, headers, timeout_s):
            requests.append(url)
            return 200, []

        state = self.tmp / "state"
        state.mkdir(parents=True, exist_ok=True)
        with WriterGuard(state) as guard:
            m = ApprovalMirror(state, guard, GitHubClient("suanlong06/Agents", http_get=http_get),
                               owner_logins=["suanlong06"], clock=lambda: "t", killswitch=self.ks)
            with self.assertRaises(KillSwitchEngaged):
                m.sync(7)
            self.assertEqual(requests, [])
            self.assertFalse(m.path.exists())
            write_killswitch(self.path, "off")
            m.sync(7)
            self.assertEqual(len(requests), 1)

    def test_killswitch_engaged_is_not_an_adapter_error(self):
        # Router/Gate 3 bắt AdapterError để fallback/thử ứng viên kế; kill-switch không được
        # bị nuốt như "tuyến sập".
        from adapters.base import AdapterError

        self.assertFalse(issubclass(KillSwitchEngaged, AdapterError))


if __name__ == "__main__":
    unittest.main()
