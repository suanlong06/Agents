"""Contract test của dashboard Muse Artifact MVP (PR-5, SPEC mục 6).

Tiêu chí chấp nhận (Windows CI):
- contract test theo schema, gồm tổng chi phí trên dashboard = tổng ledger;
- không có endpoint hay thao tác ghi nào (adapter chỉ đọc state_dir; trang không có
  JavaScript, form, nút);
- dữ liệu fixture luôn có watermark FAKE, lấy từ trường ``source`` chứ không từ CSS;
- ``as_of`` là thời điểm fold cuối, không bao giờ là "now";
- "không có dữ liệu" khác "0".

State dùng ở đây do CHÍNH code orchestrator ghi (fold event thật) vào thư mục tạm ngoài repo.
"""

from __future__ import annotations

import ast
import contextlib
import io
import json
import re
import shutil
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path

from jsonschema import Draft202012Validator

from dashboard import data_adapter as da
from orchestrator import schemas
from orchestrator.audit import AuditLog
from orchestrator.fold import Orchestrator
from orchestrator.fsio import append_line
from orchestrator.heartbeat import HeartbeatMonitor, HeartbeatWriter
from orchestrator.heartbeat import validate as validate_heartbeat
from orchestrator.inbox import AgentInbox
from orchestrator.ledger import Ledger
from orchestrator.state import EXTENDED_STATES, STEP_STATES
from pipeline import common as pipeline_common
from release.readonly_check import snapshot as fs_snapshot
from release.readonly_check import verify_readonly
from tests.helpers import FIXTURES, Emitter, make_config

REPO = "suanlong06/Agents"
FOLD_AS_OF = "2026-10-25T10:30:00.000+09:00"
EXPORTED_AT = "2026-10-26T08:00:00+09:00"
SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40
SHA_OLD = "d" * 40
ADAPTER_SOURCE = Path(da.__file__)
TEMPLATE = da.TEMPLATE_PATH


def _verdict(task_id, pr, sha, tier, attempt=1):
    return {
        "v": 1, "task_id": task_id, "attempt": attempt, "pr": pr, "sha": sha,
        "label": {"tier": tier, "label_id": "e" * 64, "base_sha": "f" * 40},
        "writer_set": [{"model_id": "claude-opus-5-5", "vendor": "anthropic", "raw": "claude-opus-5-5", "known": True}],
        "reviewer": {"agent": "gate3", "adapter": "dsh", "model_id": "deepseek-v4.1-flash-1017", "vendor": "deepseek",
                     "model_id_source": "router_log"},
        "verdict": "pass", "reason": None, "summary": "ok", "findings": [],
        "evidence": [{"file": "a.py", "quote": "x = 1"}], "injection_detected": False,
        "created_at": "2026-10-25T10:20:00.000+09:00",
    }


def build_runtime_state(state_dir: Path) -> dict:
    """Dựng state_dir bằng code thật: event inbox → fold → state/ledger/audit; heartbeat; mirror."""
    impl = Emitter(state_dir / "inbox" / "implementer.jsonl")
    user = Emitter(state_dir / "inbox" / "user.jsonl", agent="user", pid=1, model_id=None, model_id_source="unknown")
    gate3 = Emitter(state_dir / "inbox" / "gate3.jsonl", agent="gate3", pid=7, model_id="deepseek-v4.1-flash-1017",
                    model_id_source="router_log")
    fix = Emitter(state_dir / "inbox" / "fix-loop.jsonl", agent="fix-loop", pid=8, model_id=None,
                  model_id_source="unknown")

    for tid in ("T-INTAKE", "T-SPEC", "T-MERGE", "T-CI", "T-ESC", "T-BLOCK", "T-CANCEL", "T-RETRY", "T-XSS"):
        title = "<script>alert(1)</script>" if tid == "T-XSS" else f"task {tid}"
        impl.emit("task_created", tid, {"title": title})
    impl.walk("T-SPEC", "spec_draft", "spec_review")
    impl.walk("T-MERGE", "spec_draft", "spec_review")
    impl.emit("transition", "T-MERGE", {"to": "pr_opened", "pr": 21}, sha=SHA_A)
    impl.walk("T-MERGE", "labelled", "ci_check", "gate3_review")
    gate3.emit("transition", "T-MERGE", {"to": "approved_merge", "reason": None,
                                         "verdict": _verdict("T-MERGE", 21, SHA_A, "red")}, sha=SHA_A)
    impl.walk("T-CI", "spec_draft", "spec_review")
    impl.emit("transition", "T-CI", {"to": "pr_opened", "pr": 31}, sha=SHA_B)
    impl.walk("T-CI", "labelled", "ci_check")
    fix_list = [{"file": "<ci>", "type": "ci-failure", "line_start": 0, "message": "check run ci kết luận failure"}]
    fix.emit("transition", "T-CI", {"to": "fix_list", "source": "ci", "fix_list": fix_list, "fix_round": 1}, sha=SHA_B)
    fix.emit("transition", "T-CI", {"to": "awaiting-human", "reason": "Fixer là stub"}, sha=SHA_B)
    impl.emit("transition", "T-ESC", {"to": "escalated", "reason": "không có reviewer hợp lệ"})
    impl.walk("T-BLOCK", "blocked")
    user.walk("T-CANCEL", "cancelled")
    impl.walk("T-RETRY", "spec_draft", "spec_review")
    impl.emit("transition", "T-RETRY", {"to": "pr_opened", "pr": 51}, sha=SHA_OLD)
    impl.walk("T-RETRY", "labelled", "ci_check")
    fix.emit("transition", "T-RETRY", {"to": "fix_list", "source": "gate3", "fix_list": fix_list}, sha=SHA_OLD)
    fix.emit("transition", "T-RETRY", {"to": "awaiting-human"}, sha=SHA_OLD)
    fix.emit("new_attempt", "T-RETRY", {"pr": 52}, sha=SHA_C)
    # Ledger: giá theo tests/fixtures/prices.yaml; một model chưa có giá; một lời gọi ước lượng.
    impl.call("T-MERGE", "c1", inp=1000, out=100)
    impl.call("T-MERGE", "c2", inp=2000, out=300)
    gate3.call("T-MERGE", "g1", inp=500, out=50)
    impl.emit("model_call", "T-CI", {"call_id": "c1", "attempt": 1, "input_tokens": 4000, "output_tokens": 10,
                                     "estimated": False}, model_id="mystery-model")
    impl.call("T-RETRY", "c1", inp=100, out=10, estimated=True)
    impl.call("T-RETRY", "c2", attempt=2, inp=300, out=30)

    orch = Orchestrator(make_config(state_dir), clock=lambda: FOLD_AS_OF).open()
    try:
        orch.fold(sorted((state_dir / "inbox").glob("*.jsonl")))
        # Event ghi SAU lần fold cuối: log hiện là "chưa fold", view từ state không dùng nó.
        impl.emit("transition", "T-INTAKE", {"to": "spec_draft"})
        inbox = AgentInbox(state_dir, "implementer", model_id="claude-opus-5-5", model_id_source="cli_metadata")
        HeartbeatWriter(inbox, task_id="T-MERGE", attempt=1).beat()
        HeartbeatMonitor(state_dir).publish(orch)
        totals = {"all": orch.ledger.totals(),
                  "tasks": {t: orch.ledger.totals(t) for t in ("T-MERGE", "T-CI", "T-RETRY")}}
    finally:
        orch.close()
    append_line(state_dir / "approvals.jsonl", {
        "v": 1, "mirrored_at": "2026-10-25T10:25:00.000+09:00", "source": "github", "repo": REPO, "pr": 21,
        "review_id": 9001, "reviewer": "suanlong06", "state": "APPROVED", "commit_id": SHA_A,
        "submitted_at": "2026-10-25T10:24:00Z", "html_url": f"https://github.com/{REPO}/pull/21#pullrequestreview-9001"})
    (state_dir / "killswitch.json").write_text(json.dumps(
        {"v": 1, "state": "off", "set_by": "user", "set_at": "2026-10-25T09:00:00+09:00", "reason": "test"}),
        encoding="utf-8")
    return totals


class _Page(HTMLParser):
    """Thu thẻ, thuộc tính, text và nội dung <style>/<script> của trang dựng ra."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tags: list[tuple[str, dict]] = []
        self.text: list[str] = []
        self.styles: list[str] = []
        self.scripts: list[tuple[dict, str]] = []
        self._in: tuple[str, dict] | None = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        self.tags.append((tag, a))
        if tag in ("style", "script"):
            self._in = (tag, a)

    def handle_endtag(self, tag):
        if self._in and self._in[0] == tag:
            self._in = None

    def handle_data(self, data):
        if self._in and self._in[0] == "style":
            self.styles.append(data)
        elif self._in and self._in[0] == "script":
            self.scripts.append((self._in[1], data))
        else:
            self.text.append(data)

    @classmethod
    def parse(cls, text: str) -> "_Page":
        p = cls()
        p.feed(text)
        p.close()
        return p

    def find(self, tag=None, **attrs) -> list[dict]:
        return [a for t, a in self.tags if (tag is None or t == tag)
                and all(a.get(k.replace("_", "-")) == v for k, v in attrs.items())]

    def with_attr(self, name: str) -> list[dict]:
        return [a for _t, a in self.tags if name in a]


class DashboardTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="harness-dash-")).resolve()
        cls.state_dir = cls.tmp / "state"
        cls.state_dir.mkdir()
        cls.ledger_totals = build_runtime_state(cls.state_dir)
        cls.snap = cls.build(cls.state_dir)
        cls.html = da.render_html(cls.snap)
        cls.page = _Page.parse(cls.html)
        with open(da.SCHEMA_PATH, encoding="utf-8") as f:
            cls.schema_validator = Draft202012Validator(json.load(f))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @staticmethod
    def build(state_dir, **kw):
        kw.setdefault("github_repo", REPO)
        kw.setdefault("exported_at", EXPORTED_AT)
        return da.build_snapshot(state_dir, **kw)

    def new_dir(self, name: str) -> Path:
        d = self.tmp / name
        if d.exists():
            shutil.rmtree(d)
        d.mkdir()
        return d

    def assertSchemaValid(self, snap):
        errors = sorted(self.schema_validator.iter_errors(snap), key=lambda e: list(e.absolute_path))
        self.assertEqual([f"{list(e.absolute_path)}: {e.message}" for e in errors], [])


class SchemaContractTest(DashboardTestBase):
    def test_snapshot_matches_dashboard_schema(self):
        self.assertSchemaValid(self.snap)
        self.assertSchemaValid(self.build(self.new_dir("empty")))
        self.assertSchemaValid(self.build(self.state_dir, source=da.FIXTURE))

    def test_inputs_are_read_with_the_harness_schemas(self):
        # Dashboard đọc đúng các file mà code PR-1a..PR-4a ghi, theo đúng schema của chúng.
        state = json.loads((self.state_dir / "state.json").read_text(encoding="utf-8"))
        schemas.validate("state", state)
        for r in Ledger.read(self.state_dir / "ledger.jsonl"):
            schemas.validate("ledger", r)
        for r in AuditLog.read(self.state_dir / "audit.jsonl"):
            schemas.validate("audit", r)
        validate_heartbeat(json.loads((self.state_dir / "heartbeats.json").read_text(encoding="utf-8")))
        for line in (self.state_dir / "approvals.jsonl").read_text(encoding="utf-8").splitlines():
            pipeline_common.validate("approval", json.loads(line))
        prov = {k: v["provenance"] for k, v in self.snap["views"].items()}
        self.assertEqual(prov["kanban"]["records"], len(state["tasks"]))
        self.assertEqual(prov["costs"]["records"], len(Ledger.read(self.state_dir / "ledger.jsonl")))
        self.assertEqual(self.snap["views"]["logs"]["audit"]["count"],
                         len(AuditLog.read(self.state_dir / "audit.jsonl")))
        self.assertEqual(prov["agents"]["records"], 1)
        for p in prov.values():
            self.assertEqual(p["status"], "ok", p)

    def test_cost_totals_equal_ledger_totals(self):
        costs = self.snap["views"]["costs"]
        # Tổng trên dashboard = Ledger.totals() của orchestrator = tính lại từ ledger.jsonl.
        self.assertEqual(costs["totals"], self.ledger_totals["all"])
        self.assertEqual(costs["totals"], da.ledger_totals(Ledger.read(self.state_dir / "ledger.jsonl")))
        by_task = {t["task_id"]: t for t in costs["tasks"]}
        for task_id, expected in self.ledger_totals["tasks"].items():
            got = {k: by_task[task_id][k] for k in expected}
            self.assertEqual(got, expected, task_id)
        self.assertAlmostEqual(sum(t["cost_usd"] for t in costs["tasks"]), costs["totals"]["cost_usd"], places=9)
        self.assertEqual(sum(t["records"] for t in costs["tasks"]), costs["totals"]["records"])
        # Con số in trên trang cũng chính là tổng ledger.
        total_row = self.page.find("tr", **{"class": "total"})
        self.assertEqual(len(total_row), 1)
        self.assertEqual(total_row[0]["data-total-cost"], da.fmt_usd(self.ledger_totals["all"]["cost_usd"]))
        self.assertEqual(total_row[0]["data-total-records"], str(self.ledger_totals["all"]["records"]))
        for task_id, expected in self.ledger_totals["tasks"].items():
            row = [a for a in self.page.find("tr") if a.get("data-task") == task_id]
            self.assertEqual(row[0]["data-cost"], da.fmt_usd(expected["cost_usd"]))

    def test_known_prices_give_expected_costs(self):
        # Giá test: opus 10/50, deepseek 1/2 USD / 1M token.
        merge = next(t for t in self.snap["views"]["costs"]["tasks"] if t["task_id"] == "T-MERGE")
        expected = (1000 * 10 + 100 * 50 + 2000 * 10 + 300 * 50 + 500 * 1 + 50 * 2) / 1_000_000
        self.assertAlmostEqual(merge["cost_usd"], expected, places=9)
        self.assertEqual(merge["unpriced_calls"], 0)

    def test_unpriced_calls_are_not_reported_as_zero(self):
        ci = next(t for t in self.snap["views"]["costs"]["tasks"] if t["task_id"] == "T-CI")
        self.assertEqual((ci["unpriced_calls"], ci["by_attempt"]), (1, [{"attempt": 1, "cost_usd": None}]))
        self.assertIn("1 lời gọi chưa có giá", self.html)

    def test_duplicate_ledger_keys_are_counted_once_like_ledger(self):
        d = self.new_dir("dup")
        shutil.copy(self.state_dir / "ledger.jsonl", d / "ledger.jsonl")
        first = (d / "ledger.jsonl").read_text(encoding="utf-8").splitlines()[0]
        with open(d / "ledger.jsonl", "a", encoding="utf-8") as f:
            f.write(first + "\n")
        costs = self.build(d)["views"]["costs"]
        self.assertEqual(costs["totals"], self.ledger_totals["all"])
        self.assertEqual(costs["duplicates"], 1)

    def test_kanban_has_every_pipeline_state_including_extended(self):
        cols = self.snap["views"]["kanban"]["columns"]
        self.assertEqual([c["state"] for c in cols], list(STEP_STATES.values()) + list(EXTENDED_STATES))
        for s in ("escalated", "blocked", "superseded", "cancelled", "awaiting-human"):
            self.assertEqual(self.page.find("div", data_state=s)[0]["class"], "column col-extended")
        where = {t["task_id"]: c["state"] for c in cols for t in c["tasks"]}
        self.assertEqual(where, {
            "T-INTAKE": "intake", "T-SPEC": "spec_review", "T-MERGE": "approved_merge", "T-CI": "awaiting-human",
            "T-ESC": "escalated", "T-BLOCK": "blocked", "T-CANCEL": "cancelled", "T-RETRY": "pr_opened",
            "T-XSS": "intake",
        })
        counts = {c["state"]: c["count"] for c in cols}
        self.assertEqual((counts["intake"], counts["superseded"]), (2, 0))

    def test_unknown_future_state_gets_its_own_column(self):
        d = self.new_dir("future")
        shutil.copy(FIXTURES / "state_v2_future.json", d / "state.json")
        snap = self.build(d)
        unknown = [c for c in snap["views"]["kanban"]["columns"] if c["group"] == "unknown"]
        self.assertEqual([t["task_id"] for t in unknown[0]["tasks"]], ["T-9"])
        self.assertIn("shadow_eval", da.render_html(snap))

    def test_pr_queue_shows_labels_ci_and_superseded_attempts(self):
        items = {(i["task_id"], i["pr"]): i for i in self.snap["views"]["pr_queue"]["items"]}
        self.assertEqual(set(items), {("T-MERGE", 21), ("T-CI", 31), ("T-RETRY", 52), ("T-RETRY", 51)})
        merge = items[("T-MERGE", 21)]
        self.assertEqual((merge["label"]["tier"], merge["label"]["valid"], merge["label"]["sha"]), ("red", True, SHA_A))
        self.assertIsNone(merge["ci"])  # không có nguồn CI xanh trong state: không tự suy ra
        self.assertEqual(merge["url"], f"https://github.com/{REPO}/pull/21")
        self.assertEqual(merge["checks_url"], f"https://github.com/{REPO}/pull/21/checks")
        self.assertEqual(items[("T-CI", 31)]["ci"]["state"], "failure")
        self.assertIsNone(items[("T-CI", 31)]["label"])
        old = items[("T-RETRY", 51)]
        self.assertEqual((old["state"], old["current"], old["head_sha"]), ("superseded", False, SHA_OLD))
        self.assertEqual(items[("T-RETRY", 52)]["state"], "pr_opened")
        self.assertEqual(self.page.find("span", **{"class": "label label-red"})[0]["title"],
                         "verdict Gate 3 (inbox/gate3.jsonl)")

    def test_label_for_other_sha_is_marked_stale(self):
        d = self.new_dir("stale")
        snap_state = json.loads((self.state_dir / "state.json").read_text(encoding="utf-8"))
        snap_state["tasks"]["T-MERGE"]["head_sha"] = SHA_C
        (d / "state.json").write_text(json.dumps(snap_state), encoding="utf-8")
        shutil.copytree(self.state_dir / "inbox", d / "inbox")
        item = next(i for i in self.build(d)["views"]["pr_queue"]["items"] if i["task_id"] == "T-MERGE")
        self.assertEqual((item["label"]["tier"], item["label"]["valid"], item["label"]["sha"]), ("red", False, SHA_A))

    def test_approval_queue_lists_specs_and_prs_and_links_to_github(self):
        items = {i["task_id"]: i for i in self.snap["views"]["approvals"]["items"]}
        self.assertEqual({k: i["kind"] for k, i in items.items()}, {
            "T-SPEC": "spec", "T-MERGE": "pr", "T-CI": "awaiting-human", "T-ESC": "escalated"})
        self.assertEqual(items["T-MERGE"]["url"], f"https://github.com/{REPO}/pull/21")
        self.assertTrue(items["T-SPEC"]["url"].startswith(f"https://github.com/{REPO}/pulls?q="))
        review = items["T-MERGE"]["latest_review"]
        self.assertEqual((review["state"], review["matches_head"]), ("APPROVED", True))
        self.assertIsNone(items["T-SPEC"]["latest_review"])
        self.assertEqual(items["T-ESC"]["reason"], "không có reviewer hợp lệ")
        self.assertIn("Duyệt diễn ra trên GitHub", self.html)

    def test_agent_status_comes_from_heartbeats(self):
        view = self.snap["views"]["agents"]
        self.assertEqual(view["orchestrator"]["status"], "ok")
        self.assertEqual([(a["agent"], a["status"], a["task_id"]) for a in view["agents"]],
                         [("implementer", "ok", "T-MERGE")])
        self.assertEqual(view["timeout_s"], 120.0)

    def test_logs_show_audit_and_per_agent_events_with_fold_flag(self):
        logs = self.snap["views"]["logs"]
        self.assertGreater(logs["audit"]["count"], 0)
        self.assertEqual(sorted(logs["agents"]), ["fix-loop", "gate3", "implementer", "user"])
        impl = logs["agents"]["implementer"]
        # Một event ghi sau lần fold cuối: có trong log nhưng đánh dấu chưa fold.
        self.assertEqual(impl["count"] - impl["folded"], 1)
        self.assertFalse(impl["tail"][-1]["folded"])
        self.assertEqual(logs["quarantine"]["count"], 0)
        self.assertIn("event.transition", self.html)

    def test_killswitch_is_shown_view_only(self):
        ks = self.snap["killswitch"]
        self.assertEqual((ks["view_only"], ks["state"], ks["present"], ks["file"]), (True, "off", True, "killswitch.json"))
        missing = self.build(self.new_dir("noks"))["killswitch"]
        self.assertEqual((missing["engaged"], missing["present"]), (True, False))  # fail-closed
        self.assertEqual(self.page.find("div", data_killswitch="off")[0]["class"], "killswitch ks-off")

    def test_untrusted_text_is_escaped_and_non_github_links_dropped(self):
        self.assertNotIn("<script>alert(1)</script>", self.html)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", self.html)
        d = self.new_dir("badlink")
        for name in ("state.json",):
            shutil.copy(self.state_dir / name, d / name)
        rec = json.loads((self.state_dir / "approvals.jsonl").read_text(encoding="utf-8").splitlines()[0])
        rec["html_url"] = "javascript:alert(1)"
        append_line(d / "approvals.jsonl", rec)
        snap = self.build(d)
        merge = next(i for i in snap["views"]["approvals"]["items"] if i["task_id"] == "T-MERGE")
        self.assertIsNone(merge["latest_review"]["html_url"])
        self.assertNotIn("javascript:", da.render_html(snap))

    def test_embedded_snapshot_round_trips_and_cannot_close_its_tag(self):
        script = [s for a, s in self.page.scripts if a.get("id") == "harness-snapshot"]
        self.assertEqual(json.loads(script[0]), self.snap)
        self.assertEqual(self.html.count("</script>"), 1)


class ReadOnlyTest(DashboardTestBase):
    WRITE_ATTRS = {"write", "write_text", "write_bytes", "replace", "rename", "unlink", "remove", "mkdir",
                   "makedirs", "rmdir", "touch", "chmod", "truncate", "dump", "symlink_to", "rmtree", "copy",
                   "copyfile", "move", "writelines"}
    WRITER_NAMES = {"Orchestrator", "InboxOrchestrator", "WriterGuard", "StateStore", "Ledger", "AuditLog",
                    "ApprovalMirror", "HeartbeatMonitor", "HeartbeatWriter", "AgentInbox", "append_line",
                    "atomic_write_bytes", "terminate_torn_tail"}

    def _functions(self, tree):
        """``{node: tên hàm bao quanh}`` cho mọi node trong module."""
        owner = {}
        for fn in ast.walk(tree):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for node in ast.walk(fn):
                    owner.setdefault(node, fn.name)
        return owner

    def test_build_snapshot_never_writes_to_state_dir(self):
        before = fs_snapshot([self.state_dir])
        rep = verify_readonly(lambda: self.build(self.state_dir), watch=[self.state_dir])
        self.assertTrue(rep.ok, rep.to_dict())
        self.assertEqual((rep.violations, rep.fs_changes, rep.error), ([], [], None))
        self.assertEqual(rep.result["views"], self.snap["views"])
        rep = verify_readonly(lambda: da.render_html(self.build(self.state_dir)), watch=[self.state_dir])
        self.assertTrue(rep.ok, rep.to_dict())
        self.assertEqual(fs_snapshot([self.state_dir]), before)

    def test_adapter_reads_torn_tail_without_repairing_it(self):
        # Ledger/AuditLog "sửa" dòng ghi dở khi mở; adapter không được làm vậy.
        d = self.new_dir("torn")
        shutil.copy(self.state_dir / "ledger.jsonl", d / "ledger.jsonl")
        with open(d / "ledger.jsonl", "ab") as f:
            f.write(b'{"v":1,"task_id":"T-X"')
        raw = (d / "ledger.jsonl").read_bytes()
        rep = verify_readonly(lambda: self.build(d), watch=[d])
        self.assertTrue(rep.ok, rep.to_dict())
        self.assertEqual((d / "ledger.jsonl").read_bytes(), raw)
        self.assertEqual(rep.result["views"]["costs"]["totals"], self.ledger_totals["all"])

    def test_cli_writes_only_the_requested_output_outside_state_dir_and_repo(self):
        before = fs_snapshot([self.state_dir])
        out_dir = self.new_dir("out")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(da.main(["snapshot", "--state-dir", str(self.state_dir), "--out",
                                      str(out_dir / "snap.json")]), 0)
            self.assertEqual(da.main(["render", "--snapshot", str(out_dir / "snap.json"), "--out",
                                      str(out_dir / "page.html")]), 0)
            # Từ chối ghi vào state_dir (chỉ đọc) và vào repo (R13).
            self.assertEqual(da.main(["snapshot", "--state-dir", str(self.state_dir), "--out",
                                      str(self.state_dir / "snap.json")]), 2)
            self.assertEqual(da.main(["render", "--snapshot", str(out_dir / "snap.json"), "--out",
                                      str(self.state_dir / "inbox" / "x.html")]), 2)
            self.assertEqual(da.main(["render", "--state-dir", str(self.state_dir), "--out",
                                      str(da.DASHBOARD_DIR / "rendered.html")]), 2)
        self.assertFalse((da.DASHBOARD_DIR / "rendered.html").exists())
        self.assertEqual(fs_snapshot([self.state_dir]), before)
        self.assertEqual(sorted(p.name for p in out_dir.iterdir()), ["page.html", "snap.json"])
        snap = json.loads((out_dir / "snap.json").read_text(encoding="utf-8"))
        self.assertEqual(snap["views"], self.snap["views"])
        self.assertEqual(snap["fold"]["as_of"], FOLD_AS_OF)
        self.assertNoWriteAction((out_dir / "page.html").read_text(encoding="utf-8"), "page.html")

    def test_write_output_refuses_state_dir(self):
        with self.assertRaises(da.AdapterError):
            da._write_output(self.state_dir / "x.json", "{}", forbidden=[self.state_dir])
        self.assertFalse((self.state_dir / "x.json").exists())

    def test_adapter_source_has_no_write_path_into_state(self):
        tree = ast.parse(ADAPTER_SOURCE.read_text(encoding="utf-8"))
        owner = self._functions(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None)
                where = owner.get(node)
                if name in self.WRITE_ATTRS:
                    is_stdout = (isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Attribute)
                                 and fn.value.attr == "stdout")
                    self.assertTrue(where == "_write_output" or is_stdout, f"dòng {node.lineno}: {name}() trong {where}")
                if name == "open":
                    modes = list(node.args[1:2]) + [k.value for k in node.keywords if k.arg == "mode"]
                    for m in modes:
                        self.assertIsInstance(m, ast.Constant, f"dòng {node.lineno}")
                        if set(m.value) & set("wax+"):
                            self.assertEqual(where, "_write_output", f"dòng {node.lineno}: open({m.value!r})")
                self.assertNotIn(name, self.WRITER_NAMES, f"dòng {node.lineno}: không dựng writer của state")
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    self.assertNotIn(alias.name, self.WRITER_NAMES - {"Ledger", "AuditLog", "StateStore",
                                                                      "ApprovalMirror"})

    def test_adapter_has_no_server_or_network(self):
        tree = ast.parse(ADAPTER_SOURCE.read_text(encoding="utf-8"))
        banned = ("http", "socket", "socketserver", "urllib.request", "requests", "flask", "fastapi", "aiohttp",
                  "wsgiref", "xmlrpc", "subprocess", "shutil", "tempfile")
        for node in ast.walk(tree):
            mods = []
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                mods = [node.module or ""]
            for m in mods:
                self.assertFalse(any(m == b or m.startswith(b + ".") for b in banned), f"import {m}")

    def assertNoWriteAction(self, text: str, where: str):
        page = _Page.parse(text)
        for tag, attrs in page.tags:
            self.assertNotIn(tag, {"form", "button", "input", "select", "textarea", "iframe", "object", "embed",
                                   "frame", "frameset", "applet", "link", "base", "img", "svg", "audio", "video"},
                             f"{where}: <{tag}>")
            for k in attrs:
                self.assertFalse(k.startswith("on"), f"{where}: thuộc tính {k}")
                self.assertNotIn(k, {"formaction", "action", "method", "contenteditable", "draggable"}, where)
            if tag == "a":
                self.assertTrue(attrs["href"].startswith("https://github.com/"), f"{where}: href {attrs['href']}")
            if tag == "meta" and attrs.get("http-equiv", "").lower() == "refresh":
                self.fail(f"{where}: meta refresh")
        for attrs, _body in page.scripts:
            self.assertEqual(attrs.get("type"), "application/json", f"{where}: <script> chạy được")
        lowered = text.lower()
        for word in ("fetch(", "xmlhttprequest", "websocket", "sendbeacon", "eventsource", "javascript:",
                     "postmessage", "localstorage", "indexeddb"):
            self.assertNotIn(word, lowered, f"{where}: {word}")
        csp = [a for t, a in page.tags if t == "meta" and a.get("http-equiv") == "Content-Security-Policy"]
        for d in ("script-src 'none'", "form-action 'none'", "connect-src 'none'"):
            self.assertIn(d, csp[0]["content"], where)

    def test_no_write_endpoint_or_action_in_template_or_rendered_pages(self):
        self.assertNoWriteAction(TEMPLATE.read_text(encoding="utf-8"), "artifact.html")
        self.assertNoWriteAction(self.html, "runtime")
        self.assertNoWriteAction(da.render_html(self.build(self.state_dir, source=da.FIXTURE)), "fixture")
        self.assertNoWriteAction(da.render_html(self.build(self.new_dir("empty2"))), "empty")

    def test_no_approve_killswitch_or_automerge_controls(self):
        for text in (TEMPLATE.read_text(encoding="utf-8"), self.html):
            page = _Page.parse(text)
            visible = " ".join(page.text).lower()
            for phrase in ("approve this", "merge now", "enable auto-merge", "bật auto-merge", "nhấn để duyệt"):
                self.assertFalse(phrase in visible, phrase)
            self.assertEqual([a for _t, a in page.tags if a.get("role") in ("button", "switch", "checkbox")], [])


class WatermarkTest(DashboardTestBase):
    def watermarks(self, text: str) -> list[dict]:
        return _Page.parse(text).find("div", **{"class": "watermark"})

    def test_fixture_snapshot_is_always_watermarked_from_the_source_field(self):
        snap = self.build(self.state_dir, source=da.FIXTURE)
        self.assertEqual(snap["source"], "fixture")
        page_text = da.render_html(snap)
        page = _Page.parse(page_text)
        marks = self.watermarks(page_text)
        self.assertEqual([m["data-watermark"] for m in marks], ["fixture"])
        self.assertIn('FAKE · source: "fixture"', " ".join(page.text))
        self.assertEqual(len(page.find("span", **{"class": "fake-tag"})), len(da.VIEW_ORDER))

    def test_fixture_state_file_cannot_be_laundered_to_runtime(self):
        d = self.new_dir("fx")
        shutil.copy(FIXTURES / "state_v1.json", d / "state.json")
        snap = self.build(d, source=da.RUNTIME)
        self.assertEqual(snap["source"], "fixture")
        self.assertIn('state.json mang source: "fixture"', snap["source_reasons"])
        self.assertEqual(len(self.watermarks(da.render_html(snap))), 1)

    def test_state_dir_inside_repo_is_always_fixture(self):
        snap = self.build(FIXTURES)
        self.assertEqual(snap["source"], "fixture")
        self.assertEqual(len(self.watermarks(da.render_html(snap))), 1)

    def test_runtime_snapshot_has_no_watermark(self):
        self.assertEqual(self.snap["source"], "runtime")
        self.assertEqual(self.watermarks(self.html), [])
        self.assertNotIn("FAKE", " ".join(self.page.text))

    def test_missing_or_unknown_source_is_watermarked(self):
        for src in (None, "demo", "Runtime"):
            snap = json.loads(json.dumps(self.snap))
            snap["source"] = src
            self.assertEqual(len(self.watermarks(da.render_html(snap))), 1, src)

    def test_watermark_is_never_produced_by_css(self):
        for text in (TEMPLATE.read_text(encoding="utf-8"), self.html):
            css = " ".join(_Page.parse(text).styles)
            self.assertFalse("FAKE" in css, "chữ FAKE không được nằm trong CSS")
            self.assertIsNone(re.search(r"content\s*:", css), "CSS không được sinh chữ")
        # Template chưa có dữ liệu: không có phần tử watermark nào; chỉ data_adapter thêm khi source != runtime.
        self.assertEqual(self.watermarks(TEMPLATE.read_text(encoding="utf-8")), [])


class AsOfTest(DashboardTestBase):
    def as_of_values(self, text: str) -> list[str]:
        return [a["data-as-of"] for a in _Page.parse(text).with_attr("data-as-of")]

    def test_as_of_is_the_last_fold_time(self):
        self.assertEqual(self.snap["fold"]["as_of"], FOLD_AS_OF)
        for key, view in self.snap["views"].items():
            self.assertEqual(view["provenance"]["as_of"], FOLD_AS_OF, key)
        self.assertNotEqual(self.snap["exported_at"], FOLD_AS_OF)
        values = self.as_of_values(self.html)
        self.assertEqual(len(values), 1 + len(da.VIEW_ORDER))
        self.assertEqual(set(values), {FOLD_AS_OF})

    def test_as_of_does_not_follow_the_clock(self):
        a = self.build(self.state_dir, exported_at="2030-01-01T00:00:00+09:00")
        b = da.build_snapshot(self.state_dir, github_repo=REPO)  # exported_at = giờ hiện tại
        self.assertEqual(a["fold"]["as_of"], b["fold"]["as_of"])
        self.assertEqual(a["views"], b["views"])
        self.assertNotEqual(b["fold"]["as_of"], b["exported_at"])

    def test_no_fold_means_null_as_of_not_now(self):
        snap = self.build(self.new_dir("nofold"))
        self.assertIsNone(snap["fold"]["as_of"])
        for view in snap["views"].values():
            self.assertIsNone(view["provenance"]["as_of"])
        text = da.render_html(snap)
        self.assertEqual(set(self.as_of_values(text)), {""})
        self.assertIn("chưa có lần fold nào", text)
        self.assertNotIn(">now<", text.lower())

    def test_now_string_in_state_is_rejected(self):
        for bad in ("now", "NOW", "2026-10-25 10:00", "2026-10-25T10:00:00"):
            d = self.new_dir("now")
            state = json.loads((FIXTURES / "state_v1.json").read_text(encoding="utf-8"))
            state["as_of"] = bad
            (d / "state.json").write_text(json.dumps(state), encoding="utf-8")
            snap = self.build(d)
            self.assertIsNone(snap["fold"]["as_of"], bad)
            self.assertIn("as_of", snap["fold"]["as_of_error"])
            self.assertEqual(snap["views"]["kanban"]["provenance"]["status"], "error")
            self.assertSchemaValid(snap)
            self.assertNotIn(bad, self.as_of_values(da.render_html(snap)))

    def test_schema_rejects_now_as_as_of(self):
        snap = json.loads(json.dumps(self.snap))
        snap["views"]["costs"]["provenance"]["as_of"] = "now"
        self.assertTrue(list(self.schema_validator.iter_errors(snap)))
        self.assertEqual(da.valid_as_of("now")[0], None)


class NoDataVersusZeroTest(DashboardTestBase):
    def test_missing_sources_are_no_data_not_zero(self):
        snap = self.build(self.new_dir("none"))
        for key, view in snap["views"].items():
            prov = view["provenance"]
            self.assertEqual((prov["status"], prov["records"]), ("no-data", None), key)
        self.assertTrue(all(c["count"] is None for c in snap["views"]["kanban"]["columns"]))
        self.assertIsNone(snap["views"]["costs"]["totals"])
        page = _Page.parse(da.render_html(snap))
        self.assertEqual({a["data-records"] for a in page.with_attr("data-records")}, {""})
        self.assertEqual({a["data-status"] for a in page.with_attr("data-status")}, {"no-data"})
        self.assertEqual(page.find("tr", **{"class": "total"}), [])
        self.assertIn("không có dữ liệu", " ".join(page.text))

    def test_present_but_empty_sources_show_zero(self):
        d = self.new_dir("zero")
        state = json.loads((FIXTURES / "state_v1.json").read_text(encoding="utf-8"))
        state.update(source="runtime", tasks={})
        (d / "state.json").write_text(json.dumps(state), encoding="utf-8")
        for name in ("ledger.jsonl", "audit.jsonl", "approvals.jsonl"):
            (d / name).write_bytes(b"")
        snap = self.build(d)
        costs = snap["views"]["costs"]
        self.assertEqual((costs["provenance"]["status"], costs["provenance"]["records"]), ("ok", 0))
        self.assertEqual(costs["totals"]["cost_usd"], 0)
        self.assertEqual(snap["views"]["kanban"]["provenance"]["records"], 0)
        self.assertTrue(all(c["count"] == 0 for c in snap["views"]["kanban"]["columns"]))
        self.assertEqual(snap["views"]["approvals"]["provenance"]["records"], 0)
        page = _Page.parse(da.render_html(snap))
        self.assertEqual(page.find("tr", **{"class": "total"})[0]["data-total-cost"], "0")
        self.assertIn("0", [a["data-records"] for a in page.with_attr("data-records")])

    def test_zero_and_no_data_render_differently(self):
        self.assertEqual(da._v(0), "0")
        self.assertNotEqual(da._v(None), da._v(0))
        self.assertIn("không có dữ liệu", da._v(None))
        self.assertEqual(da.fmt_usd(0.0), "0")
        self.assertIsNone(da.fmt_usd(None))

    def test_partial_sources_are_flagged(self):
        d = self.new_dir("partial")
        shutil.copy(self.state_dir / "state.json", d / "state.json")
        snap = self.build(d)
        self.assertEqual(snap["views"]["approvals"]["provenance"]["status"], "partial")
        merge = next(i for i in snap["views"]["approvals"]["items"] if i["task_id"] == "T-MERGE")
        self.assertIsNone(merge["latest_review"])


class ViewDocsTest(unittest.TestCase):
    def test_one_doc_per_view_and_snapshot_handoff_is_documented(self):
        docs = {p.name: p.read_text(encoding="utf-8") for p in (da.DASHBOARD_DIR / "views").glob("*.md")}
        handoff = docs.pop("00_snapshot_handoff.md")
        for fact in ("Muse VM", "không đọc", "snapshot mới", "python -m dashboard.data_adapter render"):
            self.assertIn(fact, handoff)
        self.assertEqual(len(docs), len(da.VIEW_ORDER))
        for key in da.VIEW_ORDER:
            doc = [text for text in docs.values() if f"`views.{key}`" in text]
            self.assertEqual(len(doc), 1, key)
            self.assertIn("00_snapshot_handoff.md", doc[0], key)
            self.assertIn("Chỉ đọc", doc[0], key)


if __name__ == "__main__":
    unittest.main()
