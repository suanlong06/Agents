"""Inbox riêng cho từng agent (R3): event mang provenance, được fold đúng một lần."""

import json
from collections import Counter

from orchestrator.audit import AuditLog
from orchestrator.inbox import (
    AgentInbox,
    InboxError,
    InboxOrchestrator,
    discover_sources,
    proc_token,
)
from orchestrator.lock import ProcessIdentity
from orchestrator.schemas import SchemaError
from orchestrator.state import StateStore
from tests.helpers import StateDirTestCase

OTHER = ProcessIdentity(4242, "2026-10-25T00:00:00.0000000Z")


class SimulatedCrash(BaseException):
    pass


class InboxTestCase(StateDirTestCase):
    def setUp(self):
        super().setUp()
        self.impl = AgentInbox(self.state_dir, "implementer", model_id="claude-opus-5-5",
                               model_id_source="cli_metadata", vendor="anthropic")
        self.rev = AgentInbox(self.state_dir, "reviewer", model_id="deepseek-v4", identity=OTHER,
                              model_id_source="router_log")

    def open_inbox_orch(self) -> InboxOrchestrator:
        o = InboxOrchestrator(self.config).open()
        self._open.append(o)
        return o

    def reopen(self, orch) -> InboxOrchestrator:
        orch.close()
        return self.open_inbox_orch()

    def seed(self):
        self.impl.emit("task_created", "T-1", {"kind": "feature", "title": "demo"})
        self.impl.emit("transition", "T-1", {"to": "spec_draft"})
        self.impl.emit("model_call", "T-1", {"call_id": "c-1", "attempt": 1, "input_tokens": 1_000_000,
                                             "output_tokens": 0, "estimated": False})
        self.rev.emit("transition", "T-1", {"to": "spec_review"})

    def event_audit(self, orch) -> Counter:
        return Counter(r["dedup_key"] for r in AuditLog.read(orch.audit.path)
                       if (r["dedup_key"] or "").startswith("event:"))


class AgentInboxTest(InboxTestCase):
    def test_each_agent_writes_its_own_directory(self):
        self.seed()
        me = ProcessIdentity.current()
        self.assertEqual(self.impl.events_path.parent, self.state_dir / "inbox" / "implementer")
        self.assertEqual(self.rev.events_path.parent, self.state_dir / "inbox" / "reviewer")
        self.assertEqual(self.impl.events_path.name, f"events-{me.pid}-{proc_token(me.proc_start)}.jsonl")
        self.assertEqual(discover_sources(self.state_dir), [self.impl.events_path, self.rev.events_path])

    def test_event_carries_agent_model_task_and_pid(self):
        ev = self.impl.emit("task_created", "T-1")
        me = ProcessIdentity.current()
        self.assertEqual((ev["agent"], ev["model_id"], ev["task_id"], ev["pid"], ev["proc_start"], ev["seq"]),
                         ("implementer", "claude-opus-5-5", "T-1", me.pid, me.proc_start, 1))
        on_disk = json.loads(self.impl.events_path.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(on_disk, ev)

    def test_agent_name_cannot_escape_its_inbox(self):
        for bad in ("../reviewer", "a/b", "a\\b", "", ".", "..", "x" * 65):
            with self.assertRaises(InboxError, msg=bad):
                AgentInbox(self.state_dir, bad, model_id=None)

    def test_reopened_inbox_continues_seq(self):
        self.impl.emit("task_created", "T-1")
        again = AgentInbox(self.state_dir, "implementer", model_id="claude-opus-5-5")
        ev = again.emit("transition", "T-1", {"to": "spec_draft"})
        self.assertEqual(ev["seq"], 2)

    def test_invalid_event_is_never_written(self):
        with self.assertRaises(SchemaError):
            self.impl.emit("not-a-type", "T-1")
        self.assertFalse(self.impl.events_path.exists() and self.impl.events_path.read_bytes())


class FoldOnceTest(InboxTestCase):
    def test_inbox_events_are_folded_exactly_once(self):
        self.seed()
        orch = self.open_inbox_orch()
        res = orch.fold_inboxes()
        self.assertEqual((res.applied, res.duplicates, res.quarantined), (4, 0, 0))
        self.assertEqual(orch.snapshot["tasks"]["T-1"]["state"], "spec_review")

        self.assertEqual(orch.fold_inboxes().applied, 0)
        orch = self.reopen(orch)
        self.assertEqual(orch.fold_inboxes().applied, 0)

        audit = self.event_audit(orch)
        self.assertEqual(len(audit), 4)
        self.assertEqual(max(audit.values()), 1)
        self.assertEqual(orch.ledger.totals("T-1")["records"], 1)

    def test_resent_lines_have_no_effect(self):
        self.seed()
        orch = self.open_inbox_orch()
        orch.fold_inboxes()
        raw = self.impl.events_path.read_bytes()
        with open(self.impl.events_path, "ab") as f:
            f.write(raw)
        res = orch.fold_inboxes()
        self.assertEqual((res.applied, res.duplicates), (0, 3))
        self.assertEqual(max(self.event_audit(orch).values()), 1)

    def test_crash_before_snapshot_then_replay_applies_once(self):
        self.seed()
        orch = self.open_inbox_orch()

        def crash(_snap):
            raise SimulatedCrash()

        orch.store.save = crash
        with self.assertRaises(SimulatedCrash):
            orch.fold_inboxes()
        self.assertFalse((self.state_dir / StateStore.FILE).exists())

        orch = self.reopen(orch)
        orch.fold_inboxes()
        orch = self.reopen(orch)
        self.assertEqual(orch.fold_inboxes().applied, 0)
        self.assertEqual(orch.snapshot["tasks"]["T-1"]["state"], "spec_review")
        self.assertEqual(len(self.event_audit(orch)), 4)
        self.assertEqual(max(self.event_audit(orch).values()), 1)
        self.assertEqual(orch.ledger.totals("T-1")["records"], 1)

    def test_new_events_and_new_process_files_are_picked_up(self):
        self.impl.emit("task_created", "T-1")
        orch = self.open_inbox_orch()
        self.assertEqual(orch.fold_inboxes().applied, 1)
        self.impl.emit("transition", "T-1", {"to": "spec_draft"})
        later = AgentInbox(self.state_dir, "implementer", model_id="m",
                           identity=ProcessIdentity(777, "2026-10-26T01:00:00.0000000Z"))
        later.emit("transition", "T-1", {"to": "spec_review"})
        res = orch.fold_inboxes()
        self.assertEqual((res.applied, res.duplicates), (2, 0))
        self.assertEqual(len(orch.snapshot["offsets"]), 2)

    def test_later_process_file_sorting_first_waits_for_its_prerequisite(self):
        # Thứ tự trên CI: inbox đầu pid 888, process sau pid 777 → file events-777-* xếp
        # trước events-888-*, nên spec_review được đọc trước spec_draft.
        first = AgentInbox(self.state_dir, "implementer", model_id="m",
                           identity=ProcessIdentity(888, "2026-10-26T00:00:00.0000000Z"))
        first.emit("task_created", "T-1")
        orch = self.open_inbox_orch()
        self.assertEqual(orch.fold_inboxes().applied, 1)
        first.emit("transition", "T-1", {"to": "spec_draft"})
        later = AgentInbox(self.state_dir, "implementer", model_id="m",
                           identity=ProcessIdentity(777, "2026-10-26T01:00:00.0000000Z"))
        later.emit("transition", "T-1", {"to": "spec_review"})
        self.assertLess(later.events_path.name, first.events_path.name)

        res = orch.fold_inboxes()
        self.assertEqual((res.applied, res.duplicates, res.rejected), (2, 0, 0))
        self.assertEqual(orch.snapshot["tasks"]["T-1"]["state"], "spec_review")
        self.assertEqual(len(orch.snapshot["offsets"]), 2)
        # Lần thử bị hoãn không ghi audit: mỗi event đúng một bản ghi, đều "applied".
        recs = [r for r in AuditLog.read(orch.audit.path) if (r["dedup_key"] or "").startswith("event:")]
        self.assertEqual(Counter(r["dedup_key"] for r in recs), Counter({k: 1 for k in self.event_audit(orch)}))
        self.assertEqual(len(recs), 3)
        self.assertEqual({r["result"] for r in recs}, {"applied"})

        self.assertEqual(orch.fold_inboxes().applied, 0)
        orch = self.reopen(orch)
        res = orch.fold_inboxes()
        self.assertEqual((res.applied, res.duplicates, res.rejected), (0, 0, 0))

    def test_unsatisfiable_event_is_rejected_once_after_passes_and_keeps_source_order(self):
        first = AgentInbox(self.state_dir, "implementer", model_id="m",
                           identity=ProcessIdentity(888, "2026-10-26T00:00:00.0000000Z"))
        first.emit("task_created", "T-1")
        later = AgentInbox(self.state_dir, "implementer", model_id="m",
                           identity=ProcessIdentity(777, "2026-10-26T01:00:00.0000000Z"))
        later.emit("transition", "T-1", {"to": "approved_merge"})  # không bao giờ hợp lệ ở đây
        later.emit("transition", "T-1", {"to": "spec_draft"})

        orch = self.open_inbox_orch()
        res = orch.fold_inboxes()
        # approved_merge bị hoãn rồi từ chối đúng một lần; spec_draft (sau nó trong cùng
        # nguồn) chờ tới khi nó được giải quyết rồi mới áp.
        self.assertEqual((res.applied, res.duplicates, res.rejected), (2, 0, 1))
        self.assertEqual(orch.snapshot["tasks"]["T-1"]["state"], "spec_draft")
        key = f"implementer|777|{later.identity.proc_start}"
        self.assertEqual(orch.snapshot["watermarks"][key], 2)
        results = Counter(r["result"] for r in AuditLog.read(orch.audit.path)
                          if (r["dedup_key"] or "").startswith("event:"))
        self.assertEqual(results, Counter({"applied": 2, "rejected": 1}))
        self.assertEqual(max(self.event_audit(orch).values()), 1)

        orch = self.reopen(orch)
        res = orch.fold_inboxes()
        self.assertEqual((res.applied, res.duplicates, res.rejected), (0, 0, 0))


class ProvenanceTest(InboxTestCase):
    def test_event_claiming_another_agent_is_quarantined(self):
        self.impl.emit("task_created", "T-1")
        forged = self.rev.event("transition", "T-1", {"to": "spec_draft"})
        forged.update(pid=self.impl.identity.pid, proc_start=self.impl.identity.proc_start, seq=99)
        with open(self.impl.events_path, "ab") as f:
            f.write(json.dumps(forged).encode() + b"\n")
        res = self.open_inbox_orch().fold_inboxes()
        self.assertEqual((res.applied, res.quarantined), (1, 1))

    def test_event_with_pid_not_matching_its_file_is_quarantined(self):
        self.impl.emit("task_created", "T-1")
        forged = self.impl.event("transition", "T-1", {"to": "spec_draft"})
        forged["pid"] = 1
        with open(self.impl.events_path, "ab") as f:
            f.write(json.dumps(forged).encode() + b"\n")
        orch = self.open_inbox_orch()
        res = orch.fold_inboxes()
        self.assertEqual((res.applied, res.quarantined), (1, 1))
        self.assertIn("không khớp", orch.snapshot["quarantine"][0]["error"])


class ShrunkenSourceTest(InboxTestCase):
    """Gate 3 advisory PR-1a #3: file nguồn ngắn hơn offset đã lưu không được crash fold."""

    def test_shrunken_source_sets_health_error_instead_of_crashing(self):
        self.seed()
        orch = self.open_inbox_orch()
        orch.fold_inboxes()
        name = self.impl.events_path.relative_to(self.state_dir).as_posix()
        saved = orch.snapshot["offsets"][name]
        self.assertEqual(orch.health["status"], "ok")

        self.impl.events_path.write_bytes(b"")  # bị cắt / thay thế
        self.rev.emit("transition", "T-1", {"to": "pr_opened"})
        for _ in range(3):  # gọi lặp lại: không ném, health vẫn error
            res = orch.fold_inboxes()
            self.assertIn(name, res.source_errors)
            self.assertEqual(orch.health["status"], "error")
            self.assertIn(name, orch.health["last_error"])
        self.assertEqual(orch.snapshot["offsets"][name], saved, "offset không lùi, không tiến")
        self.assertEqual(orch.snapshot["tasks"]["T-1"]["state"], "pr_opened", "nguồn khác vẫn được fold")

        errors = [r for r in AuditLog.read(orch.audit.path) if r["action"] == "fold.source"]
        self.assertEqual(len(errors), 1, "audit không bị spam mỗi vòng fold")
        self.assertEqual(errors[0]["result"], "error")

    def test_health_returns_to_ok_when_source_is_healthy_again(self):
        self.impl.emit("task_created", "T-1")
        orch = self.open_inbox_orch()
        orch.fold_inboxes()
        good = self.impl.events_path.read_bytes()
        self.impl.events_path.write_bytes(b"")
        orch.fold_inboxes()
        self.assertEqual(orch.health["status"], "error")
        self.impl.events_path.write_bytes(good)
        res = orch.fold_inboxes()
        self.assertEqual((res.source_errors, orch.health["status"]), ({}, "ok"))

    def test_plain_orchestrator_with_flat_source_also_survives(self):
        # Đường fold của PR-1a (nguồn phẳng inbox/<agent>.jsonl) cũng được sửa.
        self.agent.emit("task_created", "T-1")
        orch = self.open_orch()
        orch.fold(self.sources())
        self.inbox.write_bytes(b"")
        res = orch.fold(self.sources())
        self.assertEqual(list(res.source_errors), ["inbox/implementer.jsonl"])
        self.assertEqual(orch.health["status"], "error")
