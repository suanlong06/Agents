"""Adapter thật (PR-2): ledger R5, model thực sự phục vụ, delta, probe, timeout, breaker."""

import http.server
import json
import sys
import threading
import time
import unittest

from adapters.antigravity import AntigravityAdapter
from adapters.base import (
    BreakerPolicy,
    CallRequest,
    CircuitBreaker,
    ConfigError,
    CumulativeUsage,
    DirectLedgerSink,
    InboxLedgerSink,
    ModelAliases,
    NotAvailable,
    ServiceUnavailable,
    estimate_tokens,
)
from adapters.claude import ClaudeAdapter
from adapters.dsh import DshAdapter
from adapters.transport import (
    CommandResult,
    HttpJsonClient,
    SubprocessRunner,
    TransportTimeout,
)
from orchestrator import schemas
from orchestrator.config import REPO_ROOT
from orchestrator.inbox import AgentInbox, InboxOrchestrator
from orchestrator.ledger import Ledger, PriceTable
from tests.adapter_fakes import ALIASES, RID_ENV, FakeRouter9, FakeRunner, claude_json, opus_usage, timeout
from tests.helpers import PRICES_FIXTURE, StateDirTestCase

PRICES = PriceTable.load(REPO_ROOT / PRICES_FIXTURE)
R5_FIELDS = ("task_id", "attempt", "agent", "call_id", "requested_model", "served_model", "vendor",
             "model_id_source", "fallback_reason", "effort", "input_tokens", "output_tokens",
             "estimated", "cost_usd", "prices_version")


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def req(model="claude-opus-5-5", *, effort="high", call_id="c-1", prompt="viết hàm cộng", **kw):
    return CallRequest(task_id="T-1", attempt=1, call_id=call_id, prompt=prompt, model=model,
                       effort=effort, **kw)


class AdapterTestCase(StateDirTestCase):
    def setUp(self):
        super().setUp()
        self.inbox = AgentInbox(self.state_dir, "implementer", model_id=None, model_id_source="unknown")
        self.inbox.emit("task_created", "T-1")
        self.sink = InboxLedgerSink(self.inbox)
        self.r9 = FakeRouter9(self.tmp / "9router" / "requests.jsonl")
        self.clock = FakeClock()

    def claude(self, *script, **kw):
        kw.setdefault("ledger", self.sink)
        kw.setdefault("aliases", ALIASES)
        return ClaudeAdapter(prices=PRICES, runner=FakeRunner(*script), clock=self.clock, **kw)

    def dsh(self, *script, **kw):
        kw.setdefault("ledger", self.sink)
        kw.setdefault("aliases", ALIASES)
        return DshAdapter(prices=PRICES, runner=FakeRunner(*script), router_log=self.r9.log,
                          request_id_env=(RID_ENV,), clock=self.clock, **kw)

    def agy(self, *script, **kw):
        kw.setdefault("ledger", self.sink)
        kw.setdefault("aliases", ALIASES)
        return AntigravityAdapter(prices=PRICES, runner=FakeRunner(*script),
                                  router_log=self.r9.log, request_id_env=(RID_ENV,), clock=self.clock, **kw)

    def folded_ledger(self) -> list[dict]:
        orch = InboxOrchestrator(self.config).open()
        self._open.append(orch)
        res = orch.fold_inboxes()
        self.assertEqual((res.rejected, res.quarantined), (0, 0))
        return Ledger.read(orch.ledger.path)


class EveryRealAdapterWritesR5Test(AdapterTestCase):
    def test_claude_dsh_agy_each_write_full_r5_record(self):
        claude = self.claude(claude_json(opus_usage(1000, 200, cache_read=500)))
        dsh = self.dsh(self.r9.serve("xh/deepseek-v4.1-flash-1017", inp=3000, out=400))
        agy = self.agy(self.r9.serve("google/gemini-3.8-flash-medium",
                                     stdout=json.dumps({"result": "tóm tắt xong"})))
        claude.run(req(call_id="c-claude"))
        dsh.run(req("deepseek-v4.1-flash-1017", effort=None, call_id="c-dsh"))
        agy.run(req("gemini-3.8-flash-medium", effort=None, call_id="c-agy", prompt="x" * 400))

        recs = {r["call_id"]: r for r in self.folded_ledger()}
        self.assertEqual(set(recs), {"c-claude", "c-dsh", "c-agy"})
        for rec in recs.values():
            schemas.validate("ledger", rec)
            for f in R5_FIELDS:
                self.assertIn(f, rec)
            self.assertEqual((rec["task_id"], rec["attempt"], rec["agent"]), ("T-1", 1, "implementer"))
            self.assertIsNone(rec["fallback_reason"])
            self.assertEqual(rec["prices_version"], "test-fixture")

        c = recs["c-claude"]
        self.assertEqual((c["served_model"], c["vendor"], c["model_id_source"]),
                         ("claude-opus-5-5", "anthropic", "cli_metadata"))
        self.assertEqual((c["input_tokens"], c["output_tokens"], c["estimated"], c["effort"]),
                         (1500, 200, False, "high"))
        self.assertAlmostEqual(c["cost_usd"], (1500 * 10 + 200 * 50) / 1e6)

        d = recs["c-dsh"]
        self.assertEqual((d["served_model"], d["vendor"], d["model_id_source"]),
                         ("deepseek-v4.1-flash-1017", "deepseek", "router_log"))
        self.assertEqual((d["input_tokens"], d["output_tokens"], d["estimated"]), (3000, 400, False))
        self.assertAlmostEqual(d["cost_usd"], (3000 * 1 + 400 * 2) / 1e6)

        a = recs["c-agy"]
        self.assertEqual((a["served_model"], a["vendor"], a["model_id_source"]),
                         ("gemini-3.8-flash-medium", "google", "router_log"))
        # agy không báo token: ước lượng, cờ estimated = true (R5).
        self.assertEqual((a["input_tokens"], a["estimated"]), (100, True))
        self.assertEqual(a["output_tokens"], estimate_tokens("tóm tắt xong"))

    def test_direct_sink_writes_same_r5_record(self):
        orch = self.open_orch()
        adapter = self.claude(claude_json(opus_usage(10, 5)), ledger=DirectLedgerSink(orch.ledger))
        adapter.run(req())
        (rec,) = Ledger.read(orch.ledger.path)
        schemas.validate("ledger", rec)
        self.assertEqual((rec["served_model"], rec["model_id_source"], rec["input_tokens"]),
                         ("claude-opus-5-5", "cli_metadata", 10))

    def test_replayed_call_is_not_double_counted(self):
        adapter = self.claude(claude_json(opus_usage(1000, 0)))
        result = adapter.run(req())
        self.sink.write(result)  # replay cùng lời gọi
        recs = self.folded_ledger()
        self.assertEqual(len(recs), 1)

    def test_multiple_served_models_are_separate_ledger_records(self):
        usage = {**opus_usage(100, 10), **opus_usage(50, 5, model="claude-sonnet-5-5")}
        result = self.claude(claude_json(usage)).run(req())
        self.assertEqual({m.model_id for m in result.served_models}, {"claude-opus-5-5", "claude-sonnet-5-5"})
        recs = {r["call_id"]: r for r in self.folded_ledger()}
        self.assertEqual(set(recs), {"c-1#claude-opus-5-5", "c-1#claude-sonnet-5-5"})

    def test_two_aliases_of_one_model_merge_into_one_record(self):
        usage = {**opus_usage(100, 10), **opus_usage(50, 5, model="claude-opus-5-5[1m]")}
        aliases = ModelAliases({"claude-opus-5-5": "anthropic"}, {"claude-opus-5-5[1m]": "claude-opus-5-5"})
        result = self.claude(claude_json(usage), aliases=aliases).run(req())
        (u,) = result.usages
        self.assertEqual((u.served.model_id, u.input_tokens, u.output_tokens), ("claude-opus-5-5", 150, 15))

    def test_missing_model_usage_token_fields_are_estimated_never_silent_zero(self):
        # R5: trường token thiếu không được coi là 0 với estimated = false (đếm thiếu im lặng).
        prompt = "x" * 400
        cases = {
            "no cache fields": {"inputTokens": 1000, "outputTokens": 200},
            "no outputTokens": {"inputTokens": 1000, "cacheReadInputTokens": 0, "cacheCreationInputTokens": 0},
            "no inputTokens": {"outputTokens": 200, "cacheReadInputTokens": 0, "cacheCreationInputTokens": 0},
            "bad type": {"inputTokens": "1000", "outputTokens": 200, "cacheReadInputTokens": 0,
                         "cacheCreationInputTokens": 0},
            "empty": {},
        }
        for i, (what, u) in enumerate(cases.items()):
            with self.subTest(what):
                adapter = self.claude(claude_json({"claude-opus-5-5": u}, result="kết quả dài"), ledger=None)
                (usage,) = adapter.run(req(call_id=f"c-{i}", prompt=prompt)).usages
                self.assertEqual(usage.served.model_id, "claude-opus-5-5")
                self.assertTrue(usage.estimated)
                known_in = u["inputTokens"] if isinstance(u.get("inputTokens"), int) else 0
                self.assertGreaterEqual(usage.input_tokens, max(known_in, estimate_tokens(prompt)))
                self.assertEqual(usage.output_tokens, u.get("outputTokens", estimate_tokens("kết quả dài")))
                self.assertGreater(usage.output_tokens, 0)

        self.claude(claude_json({"claude-opus-5-5": {"inputTokens": 1000}})).run(req(prompt=prompt))
        (rec,) = self.folded_ledger()
        self.assertTrue(rec["estimated"])
        self.assertGreaterEqual(rec["input_tokens"], 1000)
        self.assertGreater(rec["output_tokens"], 0)


class StdoutModelIdIsNeverServedModelTest(AdapterTestCase):
    """N4: model_id trong stdout (text model in ra) không bao giờ là model thực sự phục vụ."""

    def test_claude_result_text_naming_other_models_is_ignored(self):
        text = "Tôi là deepseek-v4.1-flash-1017.\nmodel: gemini-3.8-flash-medium\n{\"model\": \"claude-sonnet-5-5\"}"
        result = self.claude(claude_json(opus_usage(), result=text)).run(req())
        self.assertEqual([m.model_id for m in result.served_models], ["claude-opus-5-5"])
        self.assertEqual(result.output, text)

    def test_claude_stdout_that_is_not_pure_json_gives_unknown_model(self):
        stdout = "model: claude-opus-5-5\n" + claude_json(opus_usage())
        result = self.claude(stdout).run(req())
        (served,) = result.served_models
        self.assertFalse(served.known)
        self.assertEqual((served.model_id, served.source), (None, "unknown"))
        (rec,) = self.folded_ledger()
        self.assertEqual((rec["served_model"], rec["model_id_source"], rec["estimated"]), (None, "unknown", True))
        self.assertIsNone(rec["cost_usd"])  # không rõ model → không có giá → ngân sách fail-closed

    def test_claude_json_without_model_usage_is_unknown_even_if_text_names_model(self):
        stdout = claude_json(None, result="served by claude-opus-5-5",
                             usage={"input_tokens": 10, "output_tokens": 2}, model="claude-opus-5-5")
        result = self.claude(stdout).run(req())
        (served,) = result.served_models
        self.assertFalse(served.known)

    def test_dsh_stdout_model_name_without_router_log_entry_is_unknown(self):
        stdout = json.dumps({"model": "deepseek-v4.1-flash-1017", "result": "served by deepseek-v4.1-flash-1017"})
        result = self.dsh(stdout).run(req("deepseek-v4.1-flash-1017", effort=None))
        (served,) = result.served_models
        self.assertEqual((served.known, served.source), (False, "unknown"))

    def test_dsh_stdout_cannot_borrow_another_requests_router_entry(self):
        self.r9.write("harness-other-request", "xh/deepseek-v4.1-flash-1017", 10, 10)
        stdout = json.dumps({"request_id": "harness-other-request", "request_ids": ["harness-other-request"]})
        result = self.dsh(stdout).run(req("deepseek-v4.1-flash-1017", effort=None))
        self.assertFalse(result.served_models[0].known)

    def test_router_log_wins_over_stdout(self):
        # Log 9Router nói model là Gemini (không phải model yêu cầu): ghi đúng Gemini.
        step = self.r9.serve("google/gemini-3.8-flash-medium", inp=5, out=5,
                             stdout="Tôi là deepseek-v4.1-flash-1017")
        result = self.dsh(step).run(req("deepseek-v4.1-flash-1017", effort=None))
        self.assertEqual([(m.model_id, m.vendor, m.source) for m in result.served_models],
                         [("gemini-3.8-flash-medium", "google", "router_log")])

    def test_agy_unknown_router_alias_is_unknown_not_guessed(self):
        step = self.r9.serve("mystery-model-x", stdout="model: gemini-3.8-flash-medium")
        result = self.agy(step).run(req("gemini-3.8-flash-medium", effort=None))
        (served,) = result.served_models
        self.assertFalse(served.known)
        self.assertEqual((served.raw, served.ledger_model, served.source),
                         ("mystery-model-x", "unknown:mystery-model-x", "router_log"))
        (rec,) = self.folded_ledger()
        self.assertEqual((rec["served_model"], rec["vendor"]), ("unknown:mystery-model-x", None))


class CumulativeToDeltaTest(AdapterTestCase):
    def test_tracker(self):
        t = CumulativeUsage()
        self.assertEqual(t.delta(("s",), 100, 10), (100, 10))
        self.assertEqual(t.delta(("s",), 250, 30), (150, 20))
        self.assertEqual(t.delta(("s",), 250, 30), (0, 0))
        self.assertEqual(t.delta(("s",), 40, 5), (40, 5))  # bộ đếm reset: số mới là delta
        self.assertEqual(t.delta(("other",), 7, 1), (7, 1))

    def test_dsh_cumulative_session_usage_is_recorded_as_delta(self):
        def out(inp, o):
            return self.r9.serve("xh/deepseek-v4.1-flash-1017", stdout=json.dumps(
                {"session_id": "sess-1", "result": "ok", "usage": {"input_tokens": inp, "output_tokens": o}}))

        adapter = self.dsh(out(1000, 100), out(2500, 300))
        adapter.run(req("deepseek-v4.1-flash-1017", effort=None, call_id="c-1"))
        adapter.run(req("deepseek-v4.1-flash-1017", effort=None, call_id="c-2"))
        recs = {r["call_id"]: r for r in self.folded_ledger()}
        self.assertEqual((recs["c-1"]["input_tokens"], recs["c-1"]["output_tokens"]), (1000, 100))
        self.assertEqual((recs["c-2"]["input_tokens"], recs["c-2"]["output_tokens"]), (1500, 200))
        self.assertFalse(recs["c-2"]["estimated"])

    def test_router_log_per_request_usage_is_preferred(self):
        step = self.r9.serve("xh/deepseek-v4.1-flash-1017", inp=40, out=4, entries=3, stdout=json.dumps(
            {"session_id": "s", "usage": {"input_tokens": 99999, "output_tokens": 9999}}))
        result = self.dsh(step).run(req("deepseek-v4.1-flash-1017", effort=None))
        (u,) = result.usages
        self.assertEqual((u.input_tokens, u.output_tokens, u.estimated), (120, 12, False))

    def test_claude_cumulative_mode(self):
        adapter = self.claude(claude_json(opus_usage(100, 10)), claude_json(opus_usage(300, 25)),
                              usage_mode="cumulative")
        adapter.run(req(call_id="c-1"))
        r2 = adapter.run(req(call_id="c-2"))
        self.assertEqual((r2.usages[0].input_tokens, r2.usages[0].output_tokens), (200, 15))


class TimeoutBreakerProbeTest(AdapterTestCase):
    def test_per_call_timeout_is_passed_and_timeout_is_service_unavailable(self):
        adapter = self.claude(timeout(), call_timeout_s=42)
        with self.assertRaises(ServiceUnavailable):
            adapter.run(req())
        self.assertEqual(adapter.runner.calls[0]["timeout_s"], 42)

    def test_breaker_opens_after_threshold_then_probes_with_real_completion(self):
        ok = claude_json(opus_usage(1, 1), result="OK")
        adapter = self.claude(timeout(), timeout(), timeout(), ok, ok,
                              breaker_policy=BreakerPolicy(3, 300), probe_timeout_s=7)
        for _ in range(3):
            with self.assertRaises(ServiceUnavailable):
                adapter.run(req())
        self.assertEqual(adapter.breaker("claude-opus-5-5").state, CircuitBreaker.OPEN)
        with self.assertRaisesRegex(ServiceUnavailable, "circuit breaker"):
            adapter.run(req())
        self.assertEqual(len(adapter.runner.calls), 3)  # breaker mở: không gọi CLI
        self.assertFalse(adapter.status().available)

        self.clock.t += 300
        self.assertEqual(adapter.breaker("claude-opus-5-5").state, CircuitBreaker.HALF_OPEN)
        adapter.run(req(call_id="c-after"))
        probe_call, real_call = adapter.runner.calls[3:]
        self.assertEqual((probe_call["stdin"], probe_call["timeout_s"]), ("Reply with exactly: OK", 7))
        self.assertEqual(real_call["stdin"], "viết hàm cộng")
        self.assertEqual(adapter.breaker("claude-opus-5-5").state, CircuitBreaker.CLOSED)
        # Probe là một lời gọi model thật: cũng vào ledger (R5).
        tasks = sorted(r["task_id"] for r in self.folded_ledger())
        self.assertEqual(tasks, ["T-1", "probe"])

    def test_failed_probe_in_half_open_reopens_breaker(self):
        adapter = self.claude(timeout(), timeout(), breaker_policy=BreakerPolicy(1, 60))
        with self.assertRaises(ServiceUnavailable):
            adapter.run(req())
        self.clock.t += 60
        with self.assertRaises(ServiceUnavailable):
            adapter.run(req())
        self.assertEqual(adapter.breaker("claude-opus-5-5").state, CircuitBreaker.OPEN)
        self.assertEqual(len(adapter.runner.calls), 2)  # chỉ probe, không giao việc thật

    def test_probe_with_empty_completion_fails(self):
        adapter = self.claude(claude_json(opus_usage(1, 0), result="   "))
        with self.assertRaisesRegex(ServiceUnavailable, "rỗng"):
            adapter.probe("claude-opus-5-5")

    def test_status_probe_reports_error(self):
        adapter = self.claude(CommandResult(1, "", "rate limited"))
        st = adapter.status(probe_model="claude-opus-5-5")
        self.assertFalse(st.available)
        self.assertIn("rate limited", st.probe_error)

    def test_breaker_is_per_model(self):
        adapter = self.claude(timeout(), claude_json(opus_usage(model="claude-sonnet-5-5")),
                              breaker_policy=BreakerPolicy(1, 300))
        with self.assertRaises(ServiceUnavailable):
            adapter.run(req())
        adapter.run(req("claude-sonnet-5-5", call_id="c-2"))  # Opus mở breaker không chặn Sonnet

    def test_not_available_is_terminal_and_does_not_touch_breaker(self):
        class Gone(ClaudeAdapter):
            def _invoke(self, request, *, timeout_s, probe):
                self.calls = getattr(self, "calls", 0) + 1
                raise NotAvailable("không có")

        adapter = Gone(aliases=ALIASES, runner=FakeRunner(), breaker_policy=BreakerPolicy(1, 300))
        for _ in range(3):
            with self.assertRaises(NotAvailable):
                adapter.run(req())
        self.assertEqual(adapter.breaker("claude-opus-5-5").state, CircuitBreaker.CLOSED)
        self.assertEqual(adapter.calls, 3)

    def test_nonzero_exit_and_is_error_are_service_unavailable(self):
        adapter = self.claude(CommandResult(2, "", "boom"), claude_json(opus_usage(), is_error=True))
        with self.assertRaisesRegex(ServiceUnavailable, "exit 2"):
            adapter.run(req())
        with self.assertRaisesRegex(ServiceUnavailable, "is_error"):
            adapter.run(req())


class RouterProbeHttpTest(AdapterTestCase):
    class FakeHttp:
        def __init__(self, r9, model="xh/deepseek-v4.1-flash-1017", log=True, text="OK"):
            self.r9, self.model, self.log, self.text = r9, model, log, text
            self.calls = []

        def post(self, url, payload, *, headers, timeout_s):
            self.calls.append((url, payload, dict(headers), timeout_s))
            if self.log:
                self.r9.write(headers["X-Request-Id"], self.model, 3, 1)
            return {"model": "claude-opus-5-5", "choices": [{"message": {"content": self.text}}],
                    "usage": {"prompt_tokens": 3, "completion_tokens": 1}}

    def test_dsh_probe_is_real_completion_joined_with_router_log(self):
        http = self.FakeHttp(self.r9)
        adapter = self.dsh(http=http, probe_url="http://127.0.0.1:20128/v1/chat/completions", probe_timeout_s=2)
        result = adapter.probe("deepseek-v4.1-flash-1017")
        url, payload, headers, t = http.calls[0]
        self.assertEqual((url, t), ("http://127.0.0.1:20128/v1/chat/completions", 2))
        self.assertEqual(payload["messages"][0]["content"], "Reply with exactly: OK")
        # Trường model của phản hồi nói Opus, log 9Router nói DeepSeek: log 9Router thắng.
        self.assertEqual([(m.model_id, m.source) for m in result.served_models],
                         [("deepseek-v4.1-flash-1017", "router_log")])
        self.assertEqual(adapter.runner.calls, [])  # probe không chạy CLI

    def test_probe_url_must_be_loopback(self):
        with self.assertRaises(ConfigError):
            self.dsh(probe_url="http://10.0.0.5:20128/v1/chat/completions")

    PROBE_URL = "http://127.0.0.1:20128/v1/chat/completions"

    def test_probe_sends_bearer_key_from_environment_when_configured(self):
        http = self.FakeHttp(self.r9)
        adapter = self.dsh(http=http, probe_url=self.PROBE_URL, api_key_env="NINEROUTER_API_KEY",
                           environ={"NINEROUTER_API_KEY": "k-test-123"})
        adapter.probe("deepseek-v4.1-flash-1017")
        headers = http.calls[0][2]
        self.assertEqual(headers["Authorization"], "Bearer k-test-123")
        self.assertIn("X-Request-Id", headers)

    def test_probe_without_configured_key_sends_no_authorization(self):
        for kw in ({}, {"api_key_env": "NINEROUTER_API_KEY", "environ": {}},
                   {"api_key_env": "NINEROUTER_API_KEY", "environ": {"NINEROUTER_API_KEY": ""}}):
            with self.subTest(kw=kw):
                http = self.FakeHttp(self.r9)
                self.dsh(http=http, probe_url=self.PROBE_URL, **kw).probe("deepseek-v4.1-flash-1017")
                self.assertNotIn("Authorization", http.calls[0][2])

    def test_api_key_env_must_be_a_variable_name_not_a_secret(self):
        for bad in ("sk-live-abc123", "Bearer abc", "", "1KEY", 42):
            with self.subTest(bad=bad), self.assertRaises(ConfigError):
                self.dsh(api_key_env=bad)

    def test_keyed_router_probe_with_key_closes_breaker(self):
        """9Router có khóa: probe không gửi khóa thì 401 mãi, breaker bị khóa vĩnh viễn."""
        outer = self

        class KeyedHttp(self.FakeHttp):
            def post(self, url, payload, *, headers, timeout_s):
                if headers.get("Authorization") != "Bearer k-test":
                    self.calls.append((url, payload, dict(headers), timeout_s))
                    raise ServiceUnavailable(f"{url}: HTTP 401")
                return super().post(url, payload, headers=headers, timeout_s=timeout_s)

        def half_open_adapter(environ):
            adapter = outer.dsh(timeout(), outer.r9.serve("xh/deepseek-v4.1-flash-1017", inp=1, out=1),
                                http=KeyedHttp(outer.r9), probe_url=outer.PROBE_URL,
                                api_key_env="NINEROUTER_API_KEY", environ=environ,
                                breaker_policy=BreakerPolicy(1, 60))
            with outer.assertRaises(ServiceUnavailable):
                adapter.run(req("deepseek-v4.1-flash-1017", effort=None))
            outer.clock.t += 60
            return adapter

        adapter = half_open_adapter({})
        with self.assertRaises(ServiceUnavailable):
            adapter.run(req("deepseek-v4.1-flash-1017", effort=None, call_id="c-2"))
        self.assertEqual(adapter.breaker("deepseek-v4.1-flash-1017").state, CircuitBreaker.OPEN)

        adapter = half_open_adapter({"NINEROUTER_API_KEY": "k-test"})
        adapter.run(req("deepseek-v4.1-flash-1017", effort=None, call_id="c-3"))
        self.assertEqual(adapter.breaker("deepseek-v4.1-flash-1017").state, CircuitBreaker.CLOSED)


class CostTest(AdapterTestCase):
    def test_cost_uses_prices_and_unknown_model_has_no_cost(self):
        adapter = self.claude(claude_json(opus_usage(1_000_000, 0)), "không phải json", ledger=None)
        self.assertEqual(adapter.cost(adapter.run(req())), 10.0)
        self.assertIsNone(adapter.cost(adapter.run(req(call_id="c-2"))))


class CommandTemplateTest(unittest.TestCase):
    def test_prompt_never_on_command_line(self):
        adapter = ClaudeAdapter(aliases=ALIASES, runner=FakeRunner())
        argv = adapter.argv(req(prompt="rm -rf & calc.exe"))
        self.assertNotIn("rm -rf & calc.exe", " ".join(argv))
        self.assertEqual(argv, ["claude", "-p", "--output-format", "json", "--model", "claude-opus-5-5",
                                "--effort", "high"])
        self.assertNotIn("--effort", adapter.argv(req(effort=None)))
        with self.assertRaises(ConfigError):
            ClaudeAdapter(aliases=ALIASES, command=["claude", "-p", "{prompt}"])

    def test_dsh_passes_selected_model_and_command_without_model_is_rejected(self):
        # SPEC mục 4: model chọn theo độ khó phải tới được CLI, không để CLI dùng model mặc định.
        runner = FakeRunner("{}")
        DshAdapter(aliases=ALIASES, runner=runner).run(req("deepseek-v4.1-flash-1017", effort=None))
        argv = runner.calls[0]["argv"]
        self.assertEqual(argv[argv.index("--model") + 1], "deepseek-v4.1-flash-1017")
        for cls, cmd in ((DshAdapter, ["dsh", "--profile", "headless"]),
                         (AntigravityAdapter, ["agy", "-p"]), (ClaudeAdapter, ["claude", "-p"])):
            with self.subTest(cls=cls.__name__), self.assertRaisesRegex(ConfigError, "model"):
                cls(aliases=ALIASES, command=cmd)


class SubprocessRunnerTest(unittest.TestCase):
    def test_prompt_goes_through_stdin(self):
        res = SubprocessRunner()([sys.executable, "-c", "import sys; print(sys.stdin.read().upper())"],
                                 stdin="xin chao", timeout_s=60)
        self.assertEqual((res.returncode, res.stdout.strip()), (0, "XIN CHAO"))

    def test_timeout_kills_process_and_raises(self):
        start = time.monotonic()
        with self.assertRaises(TransportTimeout):
            SubprocessRunner()([sys.executable, "-c", "import time; time.sleep(60)"], stdin="", timeout_s=1)
        self.assertLess(time.monotonic() - start, 30)

    def test_missing_cli_is_service_unavailable(self):
        with self.assertRaises(ServiceUnavailable):
            SubprocessRunner()(["harness-cli-khong-ton-tai-xyz"], stdin="", timeout_s=5)


class HttpJsonClientTest(unittest.TestCase):
    def serve(self, handler_body):
        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                handler_body(self)

            def log_message(self, *a):
                pass

        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        srv.daemon_threads = True
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return f"http://127.0.0.1:{srv.server_address[1]}/v1/chat/completions"

    @staticmethod
    def reply(h, code, body: bytes):
        h.rfile.read(int(h.headers["Content-Length"]))
        h.send_response(code)
        h.send_header("Content-Length", str(len(body)))
        h.end_headers()
        h.wfile.write(body)

    def test_post_returns_json(self):
        url = self.serve(lambda h: self.reply(h, 200, json.dumps(
            {"model": "m", "seen_id": h.headers["X-Request-Id"]}).encode()))
        obj = HttpJsonClient().post(url, {"x": 1}, headers={"X-Request-Id": "r-1"}, timeout_s=5)
        self.assertEqual(obj["seen_id"], "r-1")

    def test_hanging_upstream_times_out(self):
        url = self.serve(lambda h: time.sleep(5))
        with self.assertRaises(ServiceUnavailable):
            HttpJsonClient().post(url, {}, headers={}, timeout_s=0.5)

    def test_http_429_is_service_unavailable(self):
        url = self.serve(lambda h: self.reply(h, 429, b"{}"))
        with self.assertRaisesRegex(ServiceUnavailable, "429"):
            HttpJsonClient().post(url, {}, headers={}, timeout_s=5)

    def test_non_loopback_is_rejected(self):
        with self.assertRaises(ConfigError):
            HttpJsonClient().post("http://example.com/v1", {}, headers={}, timeout_s=1)


if __name__ == "__main__":
    unittest.main()
