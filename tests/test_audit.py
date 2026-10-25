"""Audit log: append-only, có seq, đủ trường R4, best-effort, không hash chain (R4/R15)."""

import unittest

from orchestrator import schemas
from orchestrator.audit import AuditLog
from orchestrator.fsio import NotWriter, WriterGuard
from tests.helpers import StateDirTestCase

R4_FIELDS = {"seq", "ts", "action", "result", "agent", "served_model", "vendor", "model_id_source",
             "sha", "task_id", "attempt", "pr"}


class AuditTest(StateDirTestCase):
    def test_records_have_r4_fields_and_match_schema(self):
        self.agent.emit("task_created", "T-1")
        self.agent.emit("transition", "T-1", {"to": "spec_draft"}, sha="abcdef1")
        orch = self.open_orch()
        orch.fold(self.sources())
        recs = AuditLog.read(orch.audit.path)
        self.assertEqual(len(recs), 2)
        for r in recs:
            schemas.validate("audit", r)
            self.assertLessEqual(R4_FIELDS, set(r))
            self.assertTrue(r["ts"].endswith("+09:00"), "thời điểm theo KST")
        self.assertEqual((recs[1]["served_model"], recs[1]["vendor"], recs[1]["sha"]),
                         ("claude-opus-5-5", "anthropic", "abcdef1"))

    def test_seq_is_contiguous_across_restarts(self):
        self.agent.emit("task_created", "T-1")
        orch = self.open_orch()
        orch.fold(self.sources())
        orch = self.restart(orch)
        self.agent.walk("T-1", "spec_draft", "spec_review")
        orch.fold(self.sources())
        self.assertEqual([r["seq"] for r in AuditLog.read(orch.audit.path)], [1, 2, 3])

    def test_dedup_key_writes_once(self):
        orch = self.open_orch()
        kw = dict(action="x", result="ok", agent="orchestrator", served_model=None, vendor=None,
                  model_id_source=None, sha=None, task_id=None, attempt=None)
        self.assertIsNotNone(orch.audit.append(dedup_key="k-1", **kw))
        self.assertIsNone(orch.audit.append(dedup_key="k-1", **kw))
        orch = self.restart(orch)
        self.assertIsNone(orch.audit.append(dedup_key="k-1", **kw))
        self.assertEqual(len(AuditLog.read(orch.audit.path)), 1)

    def test_torn_tail_does_not_break_seq(self):
        self.agent.emit("task_created", "T-1")
        orch = self.open_orch()
        orch.fold(self.sources())
        orch.close()
        with open(orch.audit.path, "ab") as f:
            f.write(b'{"v":1,"seq":2,"act')
        self.agent.walk("T-1", "spec_draft")
        orch = self.open_orch()
        orch.fold(self.sources())
        self.assertEqual([r["seq"] for r in AuditLog.read(orch.audit.path)], [1, 2])

    def test_no_hash_chain(self):
        props = set(schemas.load_schema("audit")["properties"])
        self.assertFalse({p for p in props if "hash" in p or "sig" in p})
        self.assertIn("KHÔNG CHỐNG GIẢ MẠO", schemas.load_schema("audit")["description"])

    def test_append_requires_writer_guard(self):
        orch = self.open_orch()
        orch.close()
        with self.assertRaises(NotWriter):
            orch.audit.append(action="x", result="ok", agent="a", served_model=None, vendor=None,
                              model_id_source=None, sha=None, task_id=None, attempt=None)
        with self.assertRaises(NotWriter):
            AuditLog(self.state_dir, WriterGuard(self.state_dir), lambda: "t")


if __name__ == "__main__":
    unittest.main()
