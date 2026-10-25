"""Ledger: model thực sự phục vụ, token delta, giá, cờ estimated, không cộng đôi (R5)."""

import unittest

from orchestrator import schemas
from orchestrator.audit import AuditLog
from orchestrator.config import DEFAULT_PRICES_PATH, REPO_ROOT
from orchestrator.ledger import Ledger, PriceTable
from tests.helpers import PRICES_FIXTURE, Emitter, StateDirTestCase


def ledger_lines(orch):
    return [l for l in orch.ledger.path.read_text(encoding="utf-8").splitlines() if l.strip()]


class PriceTableTest(unittest.TestCase):
    def test_fixture_prices(self):
        p = PriceTable.load(REPO_ROOT / PRICES_FIXTURE)
        self.assertEqual(p.version, "test-fixture")
        self.assertEqual(p.cost("claude-opus-5-5", 1_000_000, 100_000), 15.0)
        self.assertEqual(p.vendor("deepseek-v4.1-flash-1017"), "deepseek")
        self.assertIsNone(p.cost("model-khong-co-gia", 10, 10))
        self.assertIsNone(p.vendor(None))

    def test_repo_prices_are_marked_unverified(self):
        p = PriceTable.load(DEFAULT_PRICES_PATH)
        self.assertFalse(p.verified)
        self.assertIn("unverified", p.version)


class LedgerTest(StateDirTestCase):
    def setUp(self):
        super().setUp()
        self.agent.emit("task_created", "T-1")

    def test_record_has_r5_fields_and_matches_schema(self):
        self.agent.call("T-1", "c-1", inp=1000, out=200, effort="high")
        orch = self.open_orch()
        orch.fold(self.sources())
        (rec,) = Ledger.read(orch.ledger.path)
        schemas.validate("ledger", rec)
        self.assertEqual(
            {k: rec[k] for k in ("task_id", "attempt", "call_id", "agent", "served_model", "vendor",
                                 "model_id_source", "input_tokens", "output_tokens", "estimated",
                                 "effort", "prices_version")},
            {"task_id": "T-1", "attempt": 1, "call_id": "c-1", "agent": "implementer",
             "served_model": "claude-opus-5-5", "vendor": "anthropic", "model_id_source": "cli_metadata",
             "input_tokens": 1000, "output_tokens": 200, "estimated": False, "effort": "high",
             "prices_version": "test-fixture"},
        )
        self.assertAlmostEqual(rec["cost_usd"], 0.02)

    def test_retry_of_same_call_is_not_double_counted(self):
        self.agent.call("T-1", "c-1", inp=1_000_000, out=0)
        self.agent.call("T-1", "c-1", inp=1_000_000, out=0)  # retry: event mới, cùng key
        orch = self.open_orch()
        res = orch.fold(self.sources())
        self.assertEqual((res.applied, res.rejected), (3, 0))
        self.assertEqual(orch.ledger.totals("T-1")["cost_usd"], 10.0)
        self.assertEqual(len(ledger_lines(orch)), 1)
        results = [r["result"] for r in AuditLog.read(orch.audit.path) if r["action"] == "event.model_call"]
        self.assertEqual(results, ["applied", "duplicate_call"])

    def test_fallback_is_priced_by_served_model(self):
        self.agent.call("T-1", "c-1", inp=1_000_000, out=0, requested_model="claude-opus-5-5",
                        fallback_reason="quota_exhausted")
        fallback = Emitter(self.inbox, pid=5555, model_id="deepseek-v4.1-flash-1017",
                           model_id_source="router_log")
        fallback.call("T-1", "c-2", inp=1_000_000, out=0, requested_model="claude-opus-5-5",
                      fallback_reason="opus_down")
        orch = self.open_orch()
        orch.fold(self.sources())
        recs = {r["call_id"]: r for r in Ledger.read(orch.ledger.path)}
        self.assertEqual((recs["c-2"]["served_model"], recs["c-2"]["vendor"]),
                         ("deepseek-v4.1-flash-1017", "deepseek"))
        self.assertEqual(recs["c-2"]["fallback_reason"], "opus_down")
        self.assertEqual(recs["c-2"]["model_id_source"], "router_log")
        self.assertEqual(recs["c-2"]["cost_usd"], 1.0)
        self.assertEqual(orch.ledger.totals("T-1")["cost_usd"], 11.0)

    def test_fallback_reporting_the_same_call_again_is_not_double_counted(self):
        # Lời gọi c-1 được báo một lần với model yêu cầu, rồi báo lại sau fallback.
        self.agent.call("T-1", "c-1", inp=1_000_000, out=0)
        fallback = Emitter(self.inbox, pid=5555, model_id="deepseek-v4.1-flash-1017",
                           model_id_source="router_log")
        fallback.call("T-1", "c-1", inp=1_000_000, out=0, fallback_reason="opus_down")
        orch = self.open_orch()
        res = orch.fold(self.sources())
        self.assertEqual(res.rejected, 1)
        self.assertEqual(len(ledger_lines(orch)), 1)
        self.assertEqual(orch.ledger.totals("T-1")["cost_usd"], 10.0)
        rejected = [r for r in AuditLog.read(orch.audit.path) if r["result"] == "rejected"]
        self.assertEqual(len(rejected), 1)
        self.assertIn("served_model", rejected[0]["detail"]["error"])

    def test_key_includes_task_and_attempt(self):
        self.agent.emit("task_created", "T-2")
        self.agent.call("T-1", "c-1", attempt=1)
        self.agent.call("T-1", "c-1", attempt=2)
        self.agent.call("T-2", "c-1", attempt=1)
        orch = self.open_orch()
        orch.fold(self.sources())
        self.assertEqual(len(ledger_lines(orch)), 3)
        self.assertEqual(set(orch.ledger.cost_by_attempt("T-1")), {1, 2})

    def test_tokens_are_deltas_summed_per_call(self):
        self.agent.call("T-1", "c-1", inp=100, out=10)
        self.agent.call("T-1", "c-2", inp=50, out=5)
        orch = self.open_orch()
        orch.fold(self.sources())
        t = orch.ledger.totals("T-1")
        self.assertEqual((t["input_tokens"], t["output_tokens"], t["records"]), (150, 15, 2))

    def test_estimated_and_unpriced_calls(self):
        agy = Emitter(self.inbox, pid=7, model_id="gemini-4-argon-medium")
        agy.call("T-1", "c-1", inp=1000, out=1000, estimated=True)
        orch = self.open_orch()
        orch.fold(self.sources())
        (rec,) = Ledger.read(orch.ledger.path)
        self.assertTrue(rec["estimated"])
        self.assertIsNone(rec["cost_usd"], "model không có trong prices fixture: không đoán giá")
        t = orch.ledger.totals("T-1")
        self.assertEqual((t["estimated_calls"], t["unpriced_calls"]), (1, 1))
        self.assertEqual(orch.ledger.cost_by_attempt("T-1"), {1: None})

    def test_dedup_survives_restart(self):
        self.agent.call("T-1", "c-1", inp=1_000_000, out=0)
        orch = self.open_orch()
        orch.fold(self.sources())
        orch = self.restart(orch)
        Emitter(self.inbox, pid=9999).call("T-1", "c-1", inp=1_000_000, out=0)  # process mới gửi lại
        orch.fold(self.sources())
        self.assertEqual(len(ledger_lines(orch)), 1)
        self.assertEqual(orch.ledger.totals("T-1")["cost_usd"], 10.0)

    def test_torn_ledger_line_after_crash_is_closed_and_ignored(self):
        self.agent.call("T-1", "c-1", inp=1_000_000, out=0)
        orch = self.open_orch()
        orch.fold(self.sources())
        orch.close()
        with open(orch.ledger.path, "ab") as f:
            f.write(b'{"v":1,"task_id":"T-1","call_id":"c-9"')  # crash giữa lúc append
        self.agent.call("T-1", "c-2", inp=0, out=100_000)
        orch = self.open_orch()
        orch.fold(self.sources())
        self.assertEqual([r["call_id"] for r in Ledger.read(orch.ledger.path)], ["c-1", "c-2"])
        self.assertEqual(orch.ledger.totals("T-1")["cost_usd"], 15.0)


class FixBudgetThroughFoldTest(StateDirTestCase):
    """Ngân sách vòng fix lấy chi phí theo attempt từ ledger, cộng dồn theo task_id (R2)."""

    def test_fix_spend_over_budget_escalates(self):
        a = self.agent
        a.emit("task_created", "T-1")
        a.walk("T-1", "spec_draft", "spec_review")
        a.emit("transition", "T-1", {"to": "pr_opened", "pr": 10})
        a.call("T-1", "c-1", attempt=1, inp=100_000, out=0)   # 1 USD
        a.walk("T-1", "labelled", "ci_check", "gate3_review", "fix_list")
        a.emit("new_attempt", "T-1", {"pr": 11})
        a.call("T-1", "c-2", attempt=2, inp=350_000, out=0)   # 3.5 USD > 3 x 1 USD
        a.walk("T-1", "labelled", "ci_check", "gate3_review", "fix_list")
        orch = self.open_orch()
        orch.fold(self.sources())
        t = orch.snapshot["tasks"]["T-1"]
        self.assertEqual((t["state"], t["fix_rounds"], t["attempt"]), ("escalated", 2, 2))
        self.assertIn("ngân sách", t["escalation_reason"])
        audit = [r for r in AuditLog.read(orch.audit.path) if r["action"] == "event.transition"]
        self.assertEqual(audit[-1]["detail"]["effective"], "escalated")


if __name__ == "__main__":
    unittest.main()
