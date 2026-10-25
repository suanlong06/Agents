"""Bổ sung cho test_state_machine: các chuyển trạng thái sai còn lại và lineage (R2)."""

import unittest

from orchestrator.config import FixPolicy
from orchestrator.state import (
    InvalidTransition,
    TERMINAL_STATES,
    apply_transition,
    new_task,
    start_new_attempt,
)

NOW = "2026-10-25T10:00:00.000+09:00"
POLICY = FixPolicy(max_fix_rounds=2, budget_multiplier=3.0)


def go(task, *states, actor="implementer", **kw):
    for s in states:
        task = apply_transition(task, s, actor=actor, now=NOW, policy=POLICY, **kw)
    return task


def at_gate3(pr=10):
    t = go(new_task("T-1", now=NOW), "spec_draft", "spec_review")
    t = go(t, "pr_opened", pr=pr)
    return go(t, "labelled", "ci_check", "gate3_review")


class MoreInvalidTransitionsTest(unittest.TestCase):
    def assertRejected(self, task, to, actor="implementer", **kw):
        with self.assertRaises(InvalidTransition):
            apply_transition(task, to, actor=actor, now=NOW, policy=POLICY, **kw)

    def test_going_backwards_is_rejected(self):
        t = at_gate3()
        for to in ("ci_check", "labelled", "pr_opened", "intake"):
            self.assertRejected(t, to)

    def test_self_transition_is_rejected(self):
        t = at_gate3()
        self.assertRejected(t, "gate3_review")
        self.assertRejected(go(t, "escalated"), "escalated")

    def test_supersede_requires_user(self):
        t = at_gate3()
        self.assertRejected(t, "superseded")
        self.assertEqual(go(t, "superseded", actor="user")["state"], "superseded")
        self.assertIn("superseded", TERMINAL_STATES)

    def test_awaiting_human_exit_requires_user(self):
        t = go(new_task("T-1", kind="bugfix", now=NOW), "awaiting-human")
        self.assertRejected(t, "spec_draft")
        self.assertEqual(go(t, "spec_draft", actor="user")["state"], "spec_draft")

    def test_blocked_only_from_active_steps(self):
        t = go(at_gate3(), "escalated")
        self.assertRejected(t, "blocked")

    def test_resume_from_blocked_does_not_count_a_fix_round(self):
        t = go(at_gate3(), "fix_list")
        self.assertEqual(t["fix_rounds"], 1)
        t = go(t, "blocked", "fix_list")
        self.assertEqual((t["state"], t["fix_rounds"]), ("fix_list", 1))

    def test_reopening_pr_outside_new_attempt_is_rejected(self):
        t = go(at_gate3(pr=10), "fix_list", "awaiting-human")
        self.assertRejected(t, "pr_opened", actor="user")
        self.assertRejected(t, "pr_opened", actor="user", pr=11)


class MoreLineageTest(unittest.TestCase):
    def test_new_attempt_cannot_reuse_an_earlier_pr(self):
        t = go(at_gate3(pr=10), "fix_list")
        t = start_new_attempt(t, pr=11, sha=None, now=NOW)
        t = go(t, "labelled", "ci_check", "gate3_review", "fix_list")
        with self.assertRaises(InvalidTransition):
            start_new_attempt(t, pr=10, sha=None, now=NOW)

    def test_new_attempt_needs_an_existing_pr(self):
        t = go(new_task("T-1", kind="bugfix", now=NOW), "awaiting-human")
        with self.assertRaises(InvalidTransition):
            start_new_attempt(t, pr=5, sha=None, now=NOW)

    def test_rejected_new_attempt_leaves_task_untouched(self):
        t = go(at_gate3(pr=10), "fix_list")
        before = repr(t)
        with self.assertRaises(InvalidTransition):
            start_new_attempt(t, pr=10, sha=None, now=NOW)
        self.assertEqual(repr(t), before)

    def test_bugfix_parent_pr_is_kept_on_first_attempt(self):
        t = new_task("T-1", kind="bugfix", parent_pr=7, now=NOW)
        t = go(t, "spec_draft", "spec_review")
        t = go(t, "pr_opened", pr=20)
        self.assertEqual((t["parent_pr"], t["pr"]), (7, 20))
        t = go(t, "labelled", "ci_check", "gate3_review", "fix_list")
        t = start_new_attempt(t, pr=21, sha=None, now=NOW)
        self.assertEqual(t["attempts"][0]["parent_pr"], 7)
        self.assertEqual(t["parent_pr"], 20)


if __name__ == "__main__":
    unittest.main()
