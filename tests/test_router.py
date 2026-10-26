"""Router (PR-2, R11): allowlist, fallback chỉ trong chuỗi Opus, dsh/agy sập → blocked + thông báo."""

import copy
import itertools
import unittest
from unittest import mock

from adapters import router as router_mod
from adapters.antigravity import AntigravityAdapter
from adapters.base import CallRequest, ConfigError, InboxLedgerSink, ModelAliases, NotAvailable, load_yaml
from adapters.claude import ClaudeAdapter
from adapters.codex_stub import CodexStub
from adapters.cursor_stub import CursorStub
from adapters.dsh import DshAdapter
from adapters.muse_monitor import MuseMonitor
from adapters.router import (
    ADAPTER_ALLOWLIST,
    DEFAULT_ALIASES_PATH,
    DEFAULT_ROUTING_PATH,
    DIFFICULTIES,
    Router,
    RoutingConfig,
    build_adapters,
    validate_routing,
)
from orchestrator.inbox import AgentInbox, InboxOrchestrator
from orchestrator.ledger import Ledger
from orchestrator.lock import ProcessIdentity
from tests.adapter_fakes import RID_ENV, FakeRouter9, FakeRunner, claude_json, opus_usage, timeout
from tests.helpers import StateDirTestCase
from tests.killswitch_fixtures import OpenKillSwitch

ALIASES = ModelAliases.load(DEFAULT_ALIASES_PATH)
ROUTER_ID = ProcessIdentity(7001, "2026-10-26T00:00:00.0000000Z")
MUSE_ID = ProcessIdentity(7002, "2026-10-26T00:00:00.0000000Z")


def repo_routing_data() -> dict:
    return load_yaml(DEFAULT_ROUTING_PATH)


def model_of(argv):
    return argv[argv.index("--model") + 1] if "--model" in argv else None


class RoutingConfigValidationTest(unittest.TestCase):
    def validate(self, data):
        return validate_routing(data, ALIASES)

    def mutate(self, fn):
        data = copy.deepcopy(repo_routing_data())
        fn(data)
        return data

    def test_repo_routing_is_valid_and_matches_spec_section_4(self):
        cfg = RoutingConfig.load(DEFAULT_ROUTING_PATH, ALIASES)
        hard = cfg.routes["hard"]
        self.assertEqual(hard.adapter, "claude")
        self.assertEqual([(s.model, s.effort) for s in hard.chain],
                         [("claude-opus-5-5", "high"), ("claude-opus-5-5", "low"), ("claude-sonnet-5-5", "high")])
        self.assertEqual((cfg.routes["medium"].adapter, len(cfg.routes["medium"].chain)), ("dsh", 1))
        self.assertEqual((cfg.routes["easy"].adapter, len(cfg.routes["easy"].chain)), ("agy", 1))
        self.assertEqual((cfg.breaker.failure_threshold, cfg.breaker.reset_after_s), (3, 300.0))
        self.assertEqual(cfg.adapters["dsh"]["probe_timeout_s"], 2.0)

    def test_codex_and_cursor_are_not_in_routing_yaml_at_all(self):
        text = DEFAULT_ROUTING_PATH.read_text(encoding="utf-8").casefold()
        self.assertNotIn("codex", text)
        self.assertNotIn("cursor", text)
        self.assertNotIn("codex", ADAPTER_ALLOWLIST)
        self.assertNotIn("cursor", ADAPTER_ALLOWLIST)
        self.assertEqual(set(router_mod._registry()), set(ADAPTER_ALLOWLIST))

    def test_any_mention_of_stub_names_is_rejected(self):
        cases = [
            lambda d: d["routes"]["hard"].update(adapter="codex"),
            lambda d: d["adapters"].update(codex={"command": ["codex"]}),
            lambda d: d["routes"]["hard"]["chain"].append({"model": "gpt-codex-5"}),
            lambda d: d["adapters"]["claude"].update(command=["cursor-agent", "-p"]),
            lambda d: d["routes"]["medium"].update(adapter="Cursor"),
            lambda d: d["probe"].update(prompt="ask CODEX"),
        ]
        for fn in cases:
            with self.subTest(fn=fn), self.assertRaises(ConfigError):
                self.validate(self.mutate(fn))

    def test_adapter_outside_allowlist_is_rejected(self):
        with self.assertRaisesRegex(ConfigError, "allowlist"):
            self.validate(self.mutate(lambda d: d["routes"]["easy"].update(adapter="openhands")))
        with self.assertRaises(ConfigError):
            self.validate(self.mutate(lambda d: d["adapters"].update(openhands={"command": ["oh"]})))

    def test_fallback_chain_only_for_opus_route(self):
        add_step = [{"model": "deepseek-v4.1-flash-1017"}, {"model": "deepseek-v4.1-flash-1017"}]
        with self.assertRaisesRegex(ConfigError, "fallback chỉ có trong chuỗi Opus"):
            self.validate(self.mutate(lambda d: d["routes"]["medium"].update(chain=add_step)))
        with self.assertRaisesRegex(ConfigError, "fallback chỉ có trong chuỗi Opus"):
            self.validate(self.mutate(lambda d: d["routes"]["easy"]["chain"].append(
                {"model": "gemini-4-argon-medium"})))

    def test_chain_cannot_leave_vendor_or_start_without_opus(self):
        with self.assertRaisesRegex(ConfigError, "không phục vụ qua"):
            self.validate(self.mutate(lambda d: d["routes"]["hard"]["chain"].append(
                {"model": "deepseek-v4.1-flash-1017"})))
        with self.assertRaisesRegex(ConfigError, "Opus"):
            self.validate(self.mutate(lambda d: d["routes"]["hard"].update(
                chain=[{"model": "claude-sonnet-5-5"}, {"model": "claude-opus-5-5"}])))

    def test_unknown_model_key_difficulty_and_missing_route_are_rejected(self):
        bad = [
            lambda d: d["routes"]["medium"]["chain"][0].update(model="deepseek-chua-co"),
            lambda d: d["routes"]["hard"]["chain"][0].update(effort="turbo"),
            lambda d: d["routes"].update(trivial={"adapter": "agy", "chain": [{"model": "gemini-3.8-flash-medium"}]}),
            lambda d: d["routes"].pop("easy"),
            lambda d: d.update(fallback={"dsh": "claude"}),
            lambda d: d["adapters"]["claude"].update(probe_url="http://127.0.0.1:1/"),
            lambda d: d["adapters"]["dsh"].update(call_timeout_s=0),
            lambda d: d["circuit_breaker"].update(failure_threshold=0),
            lambda d: d["adapters"]["dsh"].update(command=["dsh", "--profile", "headless"]),
            lambda d: d["adapters"]["agy"].update(api_key_env="sk-live-not-a-variable-name"),
            lambda d: d["adapters"]["claude"].update(api_key_env="ANTHROPIC_API_KEY"),
        ]
        for fn in bad:
            with self.subTest(fn=fn), self.assertRaises(ConfigError):
                self.validate(self.mutate(fn))

    def test_duplicate_yaml_key_is_rejected(self):
        text = DEFAULT_ROUTING_PATH.read_text(encoding="utf-8")
        text = text.replace("routes:\n", "routes:\n  medium:\n    adapter: agy\n    chain: [{model: gemini-3.8-flash-medium}]\n", 1)
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "routing.yaml"
            p.write_text(text, encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, "trùng"):
                RoutingConfig.load(p, ALIASES)

    def test_non_loopback_probe_url_fails_when_building(self):
        cfg = self.validate(self.mutate(lambda d: d["adapters"]["agy"].update(probe_url="http://example.com/v1")))
        with self.assertRaises(ConfigError):
            build_adapters(cfg, ALIASES, runner=FakeRunner(), killswitch=OpenKillSwitch())


class RouterTestCase(StateDirTestCase):
    def setUp(self):
        super().setUp()
        self.routing = RoutingConfig.load(DEFAULT_ROUTING_PATH, ALIASES)
        self.agent_inbox = AgentInbox(self.state_dir, "implementer", model_id=None, model_id_source="unknown")
        self.router_inbox = AgentInbox(self.state_dir, "router", model_id=None, identity=ROUTER_ID)
        self.muse = MuseMonitor(self.state_dir, identity=MUSE_ID)
        self.r9 = FakeRouter9(self.tmp / "9router" / "requests.jsonl")
        self.agent_inbox.emit("task_created", "T-1")
        self.agent_inbox.emit("transition", "T-1", {"to": "spec_draft"})

    def make_router(self, claude_script=(), dsh_script=(), agy_script=()):
        sink = InboxLedgerSink(self.agent_inbox)
        common = dict(aliases=ALIASES, ledger=sink, breaker_policy=self.routing.breaker,
                      killswitch=OpenKillSwitch())
        adapters = {
            "claude": ClaudeAdapter(runner=FakeRunner(*claude_script), **common),
            "dsh": DshAdapter(runner=FakeRunner(*dsh_script), router_log=self.r9.log,
                              request_id_env=(RID_ENV,), **common),
            "agy": AntigravityAdapter(runner=FakeRunner(*agy_script), router_log=self.r9.log,
                                      request_id_env=(RID_ENV,), **common),
        }
        return Router(self.routing, adapters, notifier=self.muse, events=self.router_inbox)

    def dispatch(self, router, difficulty, call_id="c-1"):
        return router.dispatch(difficulty, task_id="T-1", attempt=1, call_id=call_id, prompt="làm việc")

    def fold(self):
        orch = InboxOrchestrator(self.config).open()
        self._open.append(orch)
        orch.fold_inboxes()
        return orch


def claude_ok(argv, stdin, env):
    return claude_json(opus_usage(100, 10, model=model_of(argv)))


class RouterNeverSelectsCodexTest(RouterTestCase):
    def test_no_failure_pattern_ever_reaches_a_stub(self):
        """Mọi độ khó x mọi tổ hợp bước lỗi: chỉ adapter trong allowlist được gọi, stub thì không bao giờ."""
        stub_calls = []

        def spy(name):
            def fn(*a, **k):
                stub_calls.append(name)
                raise NotAvailable(name)
            return fn

        patches = [mock.patch.object(cls, meth, spy(f"{cls.__name__}.{meth}"))
                   for cls in (CodexStub, CursorStub) for meth in ("run", "probe", "status", "cost", "_invoke")]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

        n = 0
        for difficulty in DIFFICULTIES:
            route = self.routing.routes[difficulty]
            for fails in itertools.product((False, True), repeat=len(route.chain)):
                script = [timeout() if f else claude_ok for f in fails]
                if route.adapter == "claude":
                    router = self.make_router(claude_script=script)
                elif route.adapter == "dsh":
                    router = self.make_router(dsh_script=[timeout() if fails[0] else
                                                          self.r9.serve("xh/deepseek-v4.1-flash-1017")])
                else:
                    router = self.make_router(agy_script=[timeout() if fails[0] else
                                                          self.r9.serve("google/gemini-3.8-flash-medium")])
                n += 1
                outcome = self.dispatch(router, difficulty, call_id=f"c-{n}")
                called = {name for name, ad in router.adapters.items() if ad.runner.calls}
                self.assertLessEqual(called, {route.adapter})
                self.assertTrue(called <= set(ADAPTER_ALLOWLIST))
                for a in outcome.attempts:
                    self.assertIn(a.adapter, ADAPTER_ALLOWLIST)
                    self.assertNotIn("codex", a.model.casefold())
                if outcome.result is not None:
                    for m in outcome.result.served_models:
                        self.assertNotIn("codex", (m.model_id or "").casefold())
                self.assertEqual(outcome.status, "blocked" if all(fails) else "ok")
        self.assertEqual(stub_calls, [])
        self.assertEqual(n, 8 + 2 + 2)

    def test_router_refuses_stub_adapters_under_any_name(self):
        good = self.make_router().adapters
        for name, stub in (("codex", CodexStub()), ("cursor", CursorStub()),
                           ("claude", CodexStub()), ("dsh", CursorStub())):
            adapters = dict(good)
            adapters[name] = stub
            with self.subTest(name=name), self.assertRaises(ConfigError):
                Router(self.routing, adapters)

    def test_unknown_difficulty_is_rejected(self):
        router = self.make_router()
        for d in ("codex", "fix", ""):
            with self.assertRaises(ConfigError):
                router.select(d)


class OpusChainFallbackTest(RouterTestCase):
    def test_fallback_records_reason_and_actual_served_model(self):
        router = self.make_router(claude_script=[timeout(), claude_ok])
        outcome = self.dispatch(router, "hard")
        self.assertEqual(outcome.status, "ok")
        req = outcome.result.request
        self.assertEqual((req.model, req.effort, req.requested_model, req.call_id),
                         ("claude-opus-5-5", "low", "claude-opus-5-5", "c-1.fb1"))
        self.assertIn("claude-opus-5-5/high", req.fallback_reason)
        (rec,) = Ledger.read(self.fold().ledger.path)
        self.assertEqual((rec["served_model"], rec["effort"], rec["requested_model"]),
                         ("claude-opus-5-5", "low", "claude-opus-5-5"))
        self.assertIn("timeout", rec["fallback_reason"])

    def test_fallback_to_sonnet_is_recorded_as_sonnet(self):
        router = self.make_router(claude_script=[timeout(), timeout(), claude_ok])
        outcome = self.dispatch(router, "hard")
        self.assertEqual([m.model_id for m in outcome.result.served_models], ["claude-sonnet-5-5"])
        (rec,) = Ledger.read(self.fold().ledger.path)
        self.assertEqual((rec["served_model"], rec["vendor"], rec["requested_model"]),
                         ("claude-sonnet-5-5", "anthropic", "claude-opus-5-5"))
        self.assertIn("claude-opus-5-5/low", rec["fallback_reason"])

    def test_exhausted_opus_chain_blocks_and_notifies_without_leaving_claude(self):
        router = self.make_router(claude_script=[timeout(), timeout(), timeout()])
        outcome = self.dispatch(router, "hard")
        self.assertEqual(outcome.status, "blocked")
        self.assertEqual(len(outcome.attempts), 3)
        self.assertEqual((router.adapters["dsh"].runner.calls, router.adapters["agy"].runner.calls), ([], []))
        (note,) = MuseMonitor.read_all(self.state_dir)
        self.assertEqual((note["kind"], note["task_id"]), ("blocked", "T-1"))
        self.assertEqual(self.fold().snapshot["tasks"]["T-1"]["state"], "blocked")

    def test_not_available_is_terminal_no_fallback(self):
        router = self.make_router(claude_script=[NotAvailable("gone"), claude_ok])
        with self.assertRaises(NotAvailable):
            self.dispatch(router, "hard")
        self.assertEqual(len(router.adapters["claude"].runner.calls), 1)
        self.assertEqual(MuseMonitor.read_all(self.state_dir), [])


class DshAgyHaveNoFallbackTest(RouterTestCase):
    def check_blocked(self, difficulty, adapter):
        script = {f"{adapter}_script": [timeout()]}
        router = self.make_router(**script)
        outcome = self.dispatch(router, difficulty)
        self.assertEqual(outcome.status, "blocked")
        self.assertIn("không có fallback", outcome.reason)
        # Không retry, không chuyển sang tuyến khác.
        self.assertEqual(len(router.adapters[adapter].runner.calls), 1)
        for other in set(ADAPTER_ALLOWLIST) - {adapter}:
            self.assertEqual(router.adapters[other].runner.calls, [])
        (note,) = MuseMonitor.read_all(self.state_dir)
        self.assertEqual((note["kind"], note["task_id"], note["detail"]["adapter"]), ("blocked", "T-1", adapter))
        orch = self.fold()
        task = orch.snapshot["tasks"]["T-1"]
        self.assertEqual((task["state"], task["resume_state"]), ("blocked", "spec_draft"))
        self.assertEqual(Ledger.read(orch.ledger.path), [])  # không có lời gọi nào thành công

    def test_dsh_down_blocks_and_notifies(self):
        self.check_blocked("medium", "dsh")

    def test_agy_down_blocks_and_notifies(self):
        self.check_blocked("easy", "agy")

    def test_open_breaker_blocks_without_calling_cli(self):
        router = self.make_router(dsh_script=[timeout(), timeout(), timeout()])
        for i in range(4):
            self.assertEqual(self.dispatch(router, "medium", call_id=f"c-{i}").status, "blocked")
        self.assertEqual(len(router.adapters["dsh"].runner.calls), 3)
        self.assertIn("circuit breaker", MuseMonitor.read_all(self.state_dir)[-1]["message"])

    def test_medium_route_passes_selected_model_to_dsh(self):
        router = self.make_router(dsh_script=[self.r9.serve("xh/deepseek-v4.1-flash-1017", inp=7, out=3)])
        self.assertEqual(self.dispatch(router, "medium").status, "ok")
        (call,) = router.adapters["dsh"].runner.calls
        self.assertEqual(model_of(call["argv"]), self.routing.routes["medium"].chain[0].model)

    def test_repo_routing_builds_probe_auth_from_environment_variable_name(self):
        for name in ("dsh", "agy"):
            self.assertEqual(self.routing.adapters[name]["api_key_env"], "NINEROUTER_API_KEY")
        built = build_adapters(self.routing, ALIASES, runner=FakeRunner(), router_log=self.r9.log,
                               killswitch=OpenKillSwitch())
        medium = CallRequest(task_id="T-1", attempt=1, call_id="c", prompt="p", model="deepseek-v4.1-flash-1017")
        self.assertEqual(model_of(built["dsh"].argv(medium)), "deepseek-v4.1-flash-1017")
        with mock.patch.dict("os.environ", {"NINEROUTER_API_KEY": "k-env"}):
            self.assertEqual(built["dsh"].probe_headers("r-1")["Authorization"], "Bearer k-env")
            self.assertEqual(built["agy"].probe_headers("r-1")["Authorization"], "Bearer k-env")
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertNotIn("Authorization", built["dsh"].probe_headers("r-1"))

    def test_dsh_success_writes_ledger(self):
        router = self.make_router(dsh_script=[self.r9.serve("xh/deepseek-v4.1-flash-1017", inp=7, out=3)])
        self.assertEqual(self.dispatch(router, "medium").status, "ok")
        (rec,) = Ledger.read(self.fold().ledger.path)
        self.assertEqual((rec["served_model"], rec["model_id_source"], rec["fallback_reason"]),
                         ("deepseek-v4.1-flash-1017", "router_log", None))


class MuseMonitorTest(StateDirTestCase):
    def test_notify_status_cost(self):
        muse = MuseMonitor(self.state_dir, identity=MUSE_ID)
        muse.run({"kind": "info", "task_id": None, "message": "xin chào"})
        self.assertEqual(muse.cost(), 0.0)
        self.assertEqual(muse.status()["notifications"], 1)
        again = MuseMonitor(self.state_dir, identity=MUSE_ID)  # mở lại: seq đi tiếp
        self.assertEqual(again.notify(kind="info", task_id="T-1", message="2")["seq"], 2)
        with self.assertRaises(ValueError):
            muse.notify(kind="merge", task_id="T-1", message="x")
        self.assertFalse((self.state_dir / "inbox" / "muse").exists())  # không phải nguồn event


if __name__ == "__main__":
    unittest.main()
