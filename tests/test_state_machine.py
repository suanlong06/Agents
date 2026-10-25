"""State machine: bước 1–13 + trạng thái mở rộng, lineage, bộ đếm vòng fix (R2)."""

import unittest

from orchestrator import schemas
from orchestrator.audit import AuditLog
from orchestrator.config import FixPolicy
from orchestrator.state import (
    ALL_STATES,
    EXTENDED_STATES,
    STEP_STATES,
    InvalidTransition,
    apply_transition,
    new_task,
    start_new_attempt,
)
from tests.helpers import StateDirTestCase

NOW = "2026-10-25T10:00:00.000+09:00"
POLICY = FixPolicy(max_fix_rounds=2, budget_multiplier=3.0)


def go(task, *states, actor="implementer", policy=POLICY, **kw):
    for s in states:
        task = apply_transition(task, s, actor=actor, now=NOW, policy=policy, **kw)
    return task


def at_gate3(pr=10):
    t = new_task("T-1", now=NOW)
    t = go(t, "spec_draft", "spec_review")
    t = apply_transition(t, "pr_opened", actor="implementer", now=NOW, policy=POLICY, pr=pr)
    return go(t, "labelled", "ci_check", "gate3_review")


class StatesTest(unittest.TestCase):
    def test_steps_1_to_13_plus_extensions(self):
        self.assertEqual(sorted(STEP_STATES), list(range(1, 14)))
        self.assertEqual(set(EXTENDED_STATES),
                         {"escalated", "blocked", "superseded", "cancelled", "awaiting-human"})
        self.assertEqual(len(ALL_STATES), 18)

    def test_schema_documents_same_states_as_code(self):
        known = schemas.load_schema("task")["$defs"]["known_states"]["enum"]
        self.assertEqual(list(known), list(ALL_STATES))

    def test_happy_path_through_13_steps(self):
        t = at_gate3()
        t = go(t, "approved_merge", "batch_release", "readonly_check", "maintenance")
        self.assertEqual(t["state"], "maintenance")
        self.assertEqual((t["attempt"], t["pr"], t["fix_rounds"]), (1, 10, 0))


class InvalidTransitionTest(unittest.TestCase):
    def assertRejected(self, task, to, actor="implementer"):
        before = dict(task)
        with self.assertRaises(InvalidTransition):
            apply_transition(task, to, actor=actor, now=NOW, policy=POLICY)
        self.assertEqual(task, before, "task gốc không được bị sửa khi bị từ chối")

    def test_skipping_steps_is_rejected(self):
        t = new_task("T-1", now=NOW)
        for to in ("approved_merge", "ci_check", "gate3_review", "maintenance", "pr_opened"):
            self.assertRejected(t, to)

    def test_merge_without_ci_and_gate3_is_rejected(self):
        t = go(new_task("T-1", now=NOW), "spec_draft", "spec_review", "pr_opened", "labelled")
        self.assertRejected(t, "approved_merge")
        t = go(t, "ci_check")
        self.assertRejected(t, "approved_merge")

    def test_terminal_states_have_no_exit(self):
        for terminal in ("maintenance", "cancelled", "superseded"):
            t = dict(new_task("T-1", now=NOW), state=terminal)
            for to in ALL_STATES:
                self.assertRejected(t, to, actor="user")

    def test_unknown_target_state_is_rejected(self):
        self.assertRejected(new_task("T-1", now=NOW), "merged_by_bot")

    def test_unknown_current_state_is_read_only(self):
        # State bản sau (N+1) có trạng thái lạ: đọc được nhưng không chuyển.
        t = dict(new_task("T-1", now=NOW), state="shadow_eval")
        self.assertRejected(t, "spec_draft")

    def test_cancel_requires_user(self):
        t = new_task("T-1", now=NOW)
        self.assertRejected(t, "cancelled", actor="implementer")
        self.assertEqual(go(t, "cancelled", actor="user")["state"], "cancelled")

    def test_escalated_exit_requires_user(self):
        t = go(at_gate3(), "escalated")
        self.assertRejected(t, "awaiting-human", actor="implementer")
        self.assertEqual(go(t, "awaiting-human", actor="user")["state"], "awaiting-human")

    def test_blocked_resumes_only_to_previous_state(self):
        t = go(at_gate3(), "blocked")
        self.assertEqual(t["resume_state"], "gate3_review")
        self.assertRejected(t, "approved_merge")
        t = go(t, "gate3_review")
        self.assertEqual((t["state"], t["resume_state"]), ("gate3_review", None))

    def test_changing_pr_inside_transition_is_rejected(self):
        t = at_gate3(pr=10)
        with self.assertRaises(InvalidTransition):
            apply_transition(t, "fix_list", actor="reviewer", now=NOW, policy=POLICY, pr=99)

    def test_new_attempt_only_from_fix_list(self):
        with self.assertRaises(InvalidTransition):
            start_new_attempt(at_gate3(), pr=11, sha=None, now=NOW)

    def test_new_attempt_must_use_a_new_pr(self):
        t = go(at_gate3(pr=10), "fix_list")
        with self.assertRaises(InvalidTransition):
            start_new_attempt(t, pr=10, sha=None, now=NOW)


class FixRoundsTest(unittest.TestCase):
    def test_fix_round_counter_does_not_reset_on_new_pr(self):
        t = go(at_gate3(pr=10), "fix_list", "awaiting-human")
        self.assertEqual(t["fix_rounds"], 1)

        t = start_new_attempt(t, pr=11, sha="aaaaaaa", now=NOW)
        self.assertEqual((t["task_id"], t["attempt"], t["pr"], t["parent_pr"]), ("T-1", 2, 11, 10))
        self.assertEqual(t["fix_rounds"], 1, "PR mới cùng task_id không được reset bộ đếm")
        self.assertEqual(t["attempts"][-1], {"attempt": 1, "pr": 10, "parent_pr": None,
                                             "head_sha": None, "final_state": "superseded"})

        t = go(t, "labelled", "ci_check", "gate3_review", "fix_list")
        self.assertEqual(t["fix_rounds"], 2)
        t = start_new_attempt(t, pr=12, sha="bbbbbbb", now=NOW)
        self.assertEqual((t["attempt"], t["parent_pr"], t["fix_rounds"]), (3, 11, 2))

        # Vòng thứ 3 vượt N = 2 → escalated, không phải fix_list.
        t = go(t, "labelled", "ci_check", "gate3_review", "fix_list")
        self.assertEqual(t["state"], "escalated")
        self.assertEqual(t["fix_rounds"], 3)
        self.assertIn("fix_rounds", t["escalation_reason"])
        self.assertEqual([a["pr"] for a in t["attempts"]], [10, 11])

    def test_n_is_a_config_default_not_a_constant(self):
        t = at_gate3()
        relaxed = FixPolicy(max_fix_rounds=5)
        for pr in (11, 12, 13):
            t = go(t, "fix_list", policy=relaxed)
            t = start_new_attempt(t, pr=pr, sha=None, now=NOW)
            t = go(t, "labelled", "ci_check", "gate3_review", policy=relaxed)
        t = go(t, "fix_list", policy=relaxed)
        self.assertEqual((t["state"], t["fix_rounds"]), ("fix_list", 4))

    def test_fix_budget_accumulates_by_task_id(self):
        t = at_gate3()
        # attempt 2 + 3 tốn 3.5 > 3 x 1.0 của attempt đầu → escalated.
        t = go(t, "fix_list", cost_by_attempt={1: 1.0, 2: 2.0, 3: 1.5})
        self.assertEqual(t["state"], "escalated")
        self.assertIn("ngân sách", t["escalation_reason"])
        ok = go(at_gate3(), "fix_list", cost_by_attempt={1: 1.0, 2: 2.9})
        self.assertEqual(ok["state"], "fix_list")


class FoldStateMachineTest(StateDirTestCase):
    """Chuyển trạng thái sai đi qua fold: bị từ chối, state không đổi, có audit."""

    def test_invalid_transition_event_is_rejected_and_audited(self):
        self.agent.emit("task_created", "T-1", {"kind": "feature"})
        self.agent.walk("T-1", "approved_merge")
        orch = self.open_orch()
        res = orch.fold(self.sources())
        self.assertEqual((res.applied, res.rejected), (1, 1))
        self.assertEqual(orch.snapshot["tasks"]["T-1"]["state"], "intake")
        rejected = [r for r in AuditLog.read(orch.audit.path) if r["result"] == "rejected"]
        self.assertEqual(len(rejected), 1)
        self.assertIn("intake → approved_merge", rejected[0]["detail"]["error"])

    def test_fix_rounds_survive_new_pr_through_fold_and_restart(self):
        a = self.agent
        a.emit("task_created", "T-1")
        a.walk("T-1", "spec_draft", "spec_review")
        a.emit("transition", "T-1", {"to": "pr_opened", "pr": 10})
        a.walk("T-1", "labelled", "ci_check", "gate3_review", "fix_list", "awaiting-human")
        a.emit("new_attempt", "T-1", {"pr": 11}, sha="abcdef1")
        orch = self.open_orch()
        orch.fold(self.sources())
        orch = self.restart(orch)
        t = orch.snapshot["tasks"]["T-1"]
        self.assertEqual((t["attempt"], t["pr"], t["parent_pr"], t["fix_rounds"]), (2, 11, 10, 1))
        a.walk("T-1", "labelled", "ci_check", "gate3_review", "fix_list")
        orch.fold(self.sources())
        self.assertEqual(orch.snapshot["tasks"]["T-1"]["fix_rounds"], 2)


if __name__ == "__main__":
    unittest.main()
