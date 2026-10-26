"""Vòng fix (PR-3, R2): N vòng + ngân sách theo task_id, fingerprint finding, PR cũ
superseded, fix list vào awaiting-human vì Fixer là stub, vượt giới hạn thì escalated."""

import ast
import shutil
import tempfile
import unittest
from pathlib import Path

from orchestrator.config import REPO_ROOT, FixPolicy
from orchestrator.inbox import AgentInbox, InboxOrchestrator
from orchestrator.lock import ProcessIdentity
from orchestrator.state import InvalidTransition, new_task
from pipeline.ci_status import CiStatus
from pipeline.common import PipelineConfigError, task_cost_by_attempt
from pipeline.fix_loop import FixLoop, FixLoopError, fingerprint
from tests.helpers import make_config
from tests.killswitch_fixtures import OpenKillSwitch

SHA1, SHA2, SHA3 = "1" * 40, "2" * 40, "3" * 40
NOW = "2026-10-26T10:00:00.000+09:00"


def finding(file="src/app.py", type_="bug", line=3, **kw):
    return {"file": file, "type": type_, "line_start": line, "line_end": line, "severity": "major",
            "message": "lỗi", **kw}


def verdict(task_id="T-1", sha=SHA1, findings=None, v="fail"):
    return {"task_id": task_id, "sha": sha, "verdict": v, "findings": findings or [finding()]}


def task_at(state="gate3_review", *, fix_rounds=0, attempt=1, pr=10, sha=SHA1):
    t = new_task("T-1", now=NOW)
    t.update(state=state, fix_rounds=fix_rounds, attempt=attempt, pr=pr, head_sha=sha)
    return t


def cost_row(attempt, cost, task="T-1"):
    return {"task_id": task, "attempt": attempt, "cost_usd": cost, "agent": "implementer"}


class FingerprintTest(unittest.TestCase):
    def test_stable_across_case_separators_and_nearby_lines(self):
        a = fingerprint("src/App.py", "Bug", 3)
        self.assertEqual(a, fingerprint("SRC\\app.py", "bug ", 7))
        self.assertNotEqual(a, fingerprint("src/app.py", "bug", 13))
        self.assertNotEqual(a, fingerprint("src/app.py", "security", 3))
        self.assertNotEqual(a, fingerprint("src/other.py", "bug", 3))
        self.assertEqual(fingerprint("x", "t", 0, window=50), fingerprint("x", "t", 49, window=50))
        with self.assertRaises(FixLoopError):
            fingerprint("x", "t", 1, window=0)

    def test_fix_list_merges_duplicates_and_is_ordered(self):
        loop = FixLoop(FixPolicy(), killswitch=OpenKillSwitch())
        fl = loop.build_fix_list([finding(line=21), finding(line=4), finding(file="SRC/APP.PY", line=5),
                                  finding(type_="style", line=1)])
        self.assertEqual([(f["line_start"], f["type"], f["count"]) for f in fl],
                         [(1, "style", 1), (4, "bug", 2), (21, "bug", 1)])


class DecisionTest(unittest.TestCase):
    def test_first_failure_goes_to_awaiting_human(self):
        d = FixLoop(FixPolicy(max_fix_rounds=2), killswitch=OpenKillSwitch()).on_verdict(task_at(), verdict(), [cost_row(1, 1.0)])
        self.assertEqual((d.state, d.fix_rounds, d.reason), ("awaiting-human", 1, None))
        self.assertEqual(len(d.fix_list), 1)

    def test_rounds_beyond_n_escalate(self):
        d = FixLoop(FixPolicy(max_fix_rounds=2), killswitch=OpenKillSwitch()).on_verdict(task_at(fix_rounds=2, attempt=3), verdict(),
                                                            [cost_row(1, 1.0)])
        self.assertEqual((d.state, d.fix_rounds), ("escalated", 3))
        self.assertIn("N = 2", d.reason)

    def test_budget_is_cumulative_per_task_and_excludes_probe(self):
        loop = FixLoop(FixPolicy(max_fix_rounds=5, budget_multiplier=3.0), killswitch=OpenKillSwitch())
        t = task_at(fix_rounds=1, attempt=2)
        rows = [cost_row(1, 1.0), cost_row(2, 1.5), cost_row(2, 1.4)]
        self.assertEqual(loop.on_verdict(t, verdict(), rows).state, "awaiting-human")
        over = rows + [cost_row(2, 0.2)]
        d = loop.on_verdict(t, verdict(), over)
        self.assertEqual(d.state, "escalated")
        self.assertIn("ngân sách", d.reason)
        # Probe tốn bao nhiêu cũng không tính vào chi phí của task.
        probes = rows + [cost_row(2, 500.0, task="probe"), cost_row(1, 0.0, task="probe")]
        self.assertEqual(loop.on_verdict(t, verdict(), probes).state, "awaiting-human")
        self.assertEqual(task_cost_by_attempt(probes, "T-1"), {1: 1.0, 2: 2.9})
        with self.assertRaises(PipelineConfigError):
            task_cost_by_attempt(probes, "probe")

    def test_unpriced_fix_call_escalates_fail_closed(self):
        loop = FixLoop(FixPolicy(max_fix_rounds=5), killswitch=OpenKillSwitch())
        d = loop.on_verdict(task_at(fix_rounds=1, attempt=2), verdict(), [cost_row(1, 1.0), cost_row(2, None)])
        self.assertEqual(d.state, "escalated")

    def test_recurring_findings_are_flagged(self):
        loop = FixLoop(FixPolicy(), killswitch=OpenKillSwitch())
        first = loop.on_verdict(task_at(), verdict(), [])
        again = loop.on_verdict(task_at(), verdict(findings=[finding(line=5), finding(file="b.py")]), [],
                                previous_fingerprints=[f["fingerprint"] for f in first.fix_list])
        self.assertEqual(len(again.recurring), 1)
        self.assertEqual([(f["file"], f["recurring"]) for f in again.fix_list], [("b.py", False), ("src/app.py", True)])

    def test_rejects_non_fail_wrong_task_and_stale_sha(self):
        loop = FixLoop(FixPolicy(), killswitch=OpenKillSwitch())
        for bad in (verdict(v="pass"), verdict(v="escalated"), verdict(task_id="T-9"), verdict(sha=SHA2)):
            with self.subTest(verdict=bad), self.assertRaises(FixLoopError):
                loop.on_verdict(task_at(), bad, [])

    def test_ci_failure_is_a_fix_round(self):
        ci = CiStatus(SHA1, "failure", "ci", 5, "failure", "https://github.com/x/y/runs/5")
        d = FixLoop(FixPolicy(), killswitch=OpenKillSwitch()).on_ci_failure(task_at("ci_check"), ci, [])
        self.assertEqual((d.state, d.fix_list[0]["type"]), ("awaiting-human", "ci-failure"))
        with self.assertRaises(FixLoopError):
            FixLoop(FixPolicy(), killswitch=OpenKillSwitch()).on_ci_failure(task_at("ci_check"), CiStatus(SHA1, "success", "ci"), [])


class FixerIsStubTest(unittest.TestCase):
    def test_fix_loop_never_calls_a_fixer_or_any_model(self):
        tree = ast.parse((REPO_ROOT / "pipeline" / "fix_loop.py").read_text(encoding="utf-8"))
        modules = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules |= {a.name for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                modules.add(node.module or "")
        self.assertFalse({m for m in modules if m.split(".")[0] in ("adapters", "subprocess", "socket", "urllib")})
        src = (REPO_ROOT / "pipeline" / "fix_loop.py").read_text(encoding="utf-8")
        for word in ("CursorStub", "CodexStub", ".run(", "dispatch("):
            self.assertNotIn(word, src)


class FoldIntegrationTest(unittest.TestCase):
    """Ba PR liên tiếp của CÙNG task_id: vòng 3 vượt N = 2 → escalated."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="harness-fix-")).resolve()
        self.state_dir = self.tmp / "state"
        self.config = make_config(self.state_dir)
        self.impl = AgentInbox(self.state_dir, "implementer", model_id="claude-opus-5-5",
                               model_id_source="cli_metadata",
                               identity=ProcessIdentity(201, "2026-10-26T09:00:00.000+09:00"))
        self.fix_inbox = AgentInbox(self.state_dir, "fix-loop", model_id=None,
                                    identity=ProcessIdentity(202, "2026-10-26T09:00:01.000+09:00"))
        self.orch = InboxOrchestrator(self.config).open()
        self.loop = FixLoop(self.config.fix, events=self.fix_inbox, killswitch=OpenKillSwitch())

    def tearDown(self):
        self.orch.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def fold(self):
        res = self.orch.fold_inboxes()
        self.assertEqual(res.quarantined, 0)
        return self.orch.snapshot["tasks"]["T-1"]

    def to_review(self, sha):
        for s in ("labelled", "ci_check", "gate3_review"):
            self.impl.emit("transition", "T-1", {"to": s}, sha=sha)
        return self.fold()

    def test_three_prs_same_task_id_escalate_after_n_rounds(self):
        self.assertEqual(self.config.fix.max_fix_rounds, 2)
        self.impl.emit("task_created", "T-1")
        for s in ("spec_draft", "spec_review"):
            self.impl.emit("transition", "T-1", {"to": s})
        self.impl.emit("transition", "T-1", {"to": "pr_opened", "pr": 10}, sha=SHA1)
        task = self.to_review(SHA1)

        # Vòng 1: PR 10 fail → awaiting-human (Fixer là stub).
        d = self.loop.on_verdict(task, verdict(sha=SHA1), self.orch.ledger.records())
        task = self.fold()
        self.assertEqual((d.state, task["state"], task["fix_rounds"]), ("awaiting-human", "awaiting-human", 1))

        # Người dùng sửa, PR 11 thay PR 10: PR 10 superseded.
        preview = self.loop.open_fix_pr(task, pr=11, sha=SHA2)
        task = self.fold()
        self.assertEqual(task, {**preview, "updated_at": task["updated_at"]})
        self.assertEqual((task["attempt"], task["pr"], task["parent_pr"]), (2, 11, 10))
        self.assertEqual(task["attempts"][0]["pr"], 10)
        self.assertEqual(task["attempts"][0]["final_state"], "superseded")
        task = self.to_review(SHA2)

        # Vòng 2 trên PR 11: bộ đếm không reset theo PR.
        d = self.loop.on_verdict(task, verdict(sha=SHA2), self.orch.ledger.records())
        task = self.fold()
        self.assertEqual((d.state, task["state"], task["fix_rounds"]), ("awaiting-human", "awaiting-human", 2))
        self.loop.open_fix_pr(task, pr=12, sha=SHA3)
        task = self.fold()
        self.assertEqual([a["pr"] for a in task["attempts"]], [10, 11])
        task = self.to_review(SHA3)

        # Vòng 3 vượt N = 2 → escalated, không có awaiting-human.
        d = self.loop.on_verdict(task, verdict(sha=SHA3), self.orch.ledger.records())
        task = self.fold()
        self.assertEqual((d.state, task["state"], task["fix_rounds"]), ("escalated", "escalated", 3))
        self.assertIn("N = 2", task["escalation_reason"])
        self.assertEqual(self.orch.fold_inboxes().rejected, 0)
        with self.assertRaises(InvalidTransition):
            self.loop.open_fix_pr(task, pr=13, sha=SHA1)  # escalated: chỉ người dùng gỡ


if __name__ == "__main__":
    unittest.main()
