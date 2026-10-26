"""Batch release PR-4b (R7, R9, R10) trên remote git THẬT + GitHub giả.

Chứng minh:
- KHÔNG có auto-merge: PR không có review APPROVED của người dùng trên đúng head (kể cả CI xanh),
  duyệt SHA cũ, duyệt sau hạn, duyệt bởi người khác, CHANGES_REQUESTED → không bao giờ có lệnh merge.
- Merge TUẦN TỰ đúng thứ tự digest, CI chạy trên KẾT QUẢ TÍCH HỢP (PR draft), cây main sau mỗi merge
  == cây bước tích hợp đã qua CI; không dùng merge queue native.
- Phê duyệt gắn (head SHA, patch-id): head đổi sau digest, conflict khi tích hợp, patch-id đổi khi
  tích hợp (kèm range-diff) → bỏ qua.
- Danh tính merge: allowlist rỗng / danh tính lạ / danh tính là tác giả → không merge.
- release.lock giữ suốt bước kiểm tra; manifest từ git log; kiểm tra không đạt → revert tự động
  (PR revert mở, KHÔNG merge, incident); revert conflict → incident frozen.
- Kill-switch bật giữa batch → dừng ngay, không thao tác ghi nào nữa.
"""

import json
import unittest
from dataclasses import replace

from orchestrator.lock import read_lock, owner_alive
from pipeline import common
from release.auto_revert import AutoRevert
from release.batch import BatchRelease
from release.digest import DigestBuilder, read_digest
from release.incidents import active_incidents
from release.readonly_check import BATCH_LOCK_PURPOSE
from release.schedule import SchedulerSettings, job_for
from tests.batch_fixtures import MERGER, OWNER, World, git, kst, out
from tests.killswitch_fixtures import write_killswitch

SCHED = SchedulerSettings()
WINDOW = "20261026-1200"


class FakeCheck:
    """Bước 11 giả: ghi lại lúc được gọi release.lock có đang bị batch giữ không."""

    def __init__(self, world, ok=True, side_effect=None):
        self.w, self.ok, self.side_effect, self.calls = world, ok, side_effect, []

    def __call__(self, *, tag, since_seq):
        lock = read_lock(self.w.state_dir / "release.lock")
        self.calls.append({"tag": tag, "since_seq": since_seq, "lock": lock, "alive": owner_alive(lock)})
        if self.side_effect:
            self.side_effect()
        return {"ok": self.ok, "exit_code": 0 if self.ok else 1,
                "report": {"ok": self.ok, "probes": [{"name": "log_errors", "passed": self.ok}]}, "error": None}


class FlipKillSwitch:
    """Kill-switch THẬT; NGƯỜI DÙNG bật nó (ghi file) đúng lúc batch sắp làm ``flip_at``."""

    def __init__(self, world, flip_at):
        self.w, self.flip_at, self.checks = world, flip_at, []

    def status(self):
        return self.w.killswitch.status()

    def check(self, action):
        self.checks.append(action)
        if action == self.flip_at:
            write_killswitch(self.w.ks_path, "on")
        return self.w.killswitch.check(action)


class BatchTestCase(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.check = FakeCheck(self.w)

    def tearDown(self):
        self.w.close()

    def components(self, *, killswitch=None, settings=None, writer="default"):
        w = self.w
        ks = killswitch or w.killswitch
        st = settings or w.settings
        wr = w.writer if writer == "default" else writer
        kw = dict(state_dir=w.state_dir, guard=w.guard, audit=w.audit, killswitch=ks, notifier=w.notifier)
        reverter = AutoRevert(git=w.git, writer=wr, settings=st, clock=lambda: w.now, token=None, **kw)
        digest = DigestBuilder(client=w.client, git=w.git, settings=st, clock=lambda: kst(11, 30), **kw)
        batch = BatchRelease(client=w.client, writer=wr, git=w.git, settings=st, owner_logins=[OWNER],
                             release_check=self.check, reverter=reverter, clock=lambda: w.now,
                             sleep=lambda s: None, **kw)
        return digest, batch

    def run_window(self, **kw):
        digest, batch = self.components(**kw)
        digest.build(job_for(WINDOW, "digest", SCHED))
        return batch.run(job_for(WINDOW, "batch", SCHED))

    def main_sha(self):
        return self.w.remote_sha("refs/heads/main")


class HappyPathTest(BatchTestCase):
    def test_two_approved_prs_merge_sequentially_after_integrated_ci_then_release(self):
        w = self.w
        h1 = w.make_pr(11, "docs-a", {"docs/a.md": "A\n"})
        h2 = w.make_pr(12, "src-b", {"src/app.py": "A = 1\nB = 2\nC = 30\n"})
        base = self.main_sha()
        rep = self.run_window()
        self.assertEqual(rep["status"], "released", json.dumps(rep, ensure_ascii=False, indent=1))
        self.assertEqual(rep["auto_merge"], "disabled")
        self.assertEqual(w.gh.merge_order, [11, 12])
        self.assertEqual([m["pr"] for m in rep["merged"]], [11, 12])
        self.assertEqual([m["head_sha"] for m in rep["merged"]], [h1, h2])
        # Lệnh merge gửi đúng head đã duyệt; CI chạy trên SHA cuối của nhánh tích hợp (PR draft).
        puts = [(p, b) for m, p, b in w.gh.calls if m == "PUT"]
        self.assertEqual([b["sha"] for _p, b in puts], [h1, h2])
        integ = rep["integration"]
        self.assertEqual(integ["base_sha"], base)
        self.assertEqual(integ["ci"], "success")
        draft = w.gh.pulls[integ["pr"]]
        self.assertTrue(draft["draft"])
        self.assertEqual(draft["state"], "closed")  # PR tích hợp KHÔNG BAO GIỜ được merge
        self.assertFalse(draft["merged"])
        self.assertNotIn(integ["pr"], w.gh.merge_order)
        # Cây main == cây bước tích hợp cuối đã qua CI; main là chuỗi merge commit tuần tự.
        main = self.main_sha()
        self.assertEqual(out(w.work, "rev-parse", f"{main}^{{tree}}"),
                         out(w.work, "rev-parse", f"{integ['head_sha']}^{{tree}}"))
        m1, m2 = (m["merge_sha"] for m in rep["merged"])
        self.assertEqual(out(w.work, "rev-list", "--parents", "-n", "1", m2).split()[1:], [m1, h2])
        self.assertEqual(out(w.work, "rev-list", "--parents", "-n", "1", m1).split()[1:], [base, h1])
        # Không merge queue native: không có endpoint merge queue nào được gọi.
        self.assertFalse([p for _m, p, _b in w.gh.calls if "queue" in p])
        # Tag + manifest từ git log; kiểm tra chạy với release.lock của batch đang được giữ.
        self.assertEqual((rep["tag"], rep["prev_tag"]), ("rel-20261026-1200", "rel-20261025-1700"))
        manifest = w.read_json("releases/rel-20261026-1200.json")
        self.assertEqual([e["pr"] for e in manifest["entries"]], [11, 12])
        self.assertEqual(manifest["manifest_id"], rep["manifest_id"])
        (call,) = self.check.calls
        self.assertEqual(call["tag"], "rel-20261026-1200")
        self.assertEqual(call["lock"]["purpose"], BATCH_LOCK_PURPOSE)
        self.assertTrue(call["alive"])
        self.assertFalse((w.state_dir / "release.lock").exists())  # nhả sau khi xong
        # Báo cáo đúng schema; audit có binding + merge; nhánh tích hợp đã xóa.
        common.validate("batch", w.read_json(f"batches/{WINDOW}.json"))
        actions = [r["action"] for r in w.audit.read(w.audit.path)]
        for a in ("digest.frozen", "batch.approval_bound", "batch.integration_ci", "batch.merged", "batch.tagged",
                  "batch.release_check", "batch.finished"):
            self.assertIn(a, actions)
        self.assertNotEqual(git(w.remote, "rev-parse", "--verify", "--quiet",
                                f"refs/heads/harness/batch/{WINDOW}-1", check=False).returncode, 0)
        self.assertEqual(active_incidents(w.state_dir), [])

    def test_max_prs_defers_the_rest(self):
        for n in (11, 12, 13):
            self.w.make_pr(n, f"d{n}", {f"docs/{n}.md": f"{n}\n"})
        rep = self.run_window(settings=replace(self.w.settings, max_prs=2))
        self.assertEqual(rep["status"], "released")
        self.assertEqual(self.w.gh.merge_order, [11, 12])
        self.assertEqual(rep["deferred"], [13])


class NoAutoMergeTest(BatchTestCase):
    """HARD RULE: không có phê duyệt của người dùng → không có lệnh merge, dù mọi thứ khác xanh."""

    def test_unapproved_wrong_sha_late_foreign_or_changes_requested_never_merge(self):
        w = self.w
        w.make_pr(21, "no-review", {"docs/21.md": "x\n"}, approve=False)
        w.make_pr(22, "old-sha", {"docs/22.md": "x\n"}, approve=False)
        w.gh.review(22, sha="0" * 40)  # duyệt một SHA khác
        w.make_pr(23, "late", {"docs/23.md": "x\n"}, approve=False)
        w.gh.review(23, at="2026-10-26T02:58:00Z")  # 11:58 KST, sau hạn 11:55
        w.make_pr(24, "foreign", {"docs/24.md": "x\n"}, approve=False)
        w.gh.review(24, login="someone-else")
        w.make_pr(25, "changes", {"docs/25.md": "x\n"}, approve=True)
        w.gh.review(25, state="CHANGES_REQUESTED", at="2026-10-26T02:50:00Z")
        before = self.main_sha()
        rep = self.run_window()
        self.assertEqual(rep["status"], "nothing_to_release")
        self.assertEqual(w.gh.merge_puts(), [])
        self.assertEqual(self.main_sha(), before)
        self.assertEqual({s["pr"] for s in rep["skipped"]}, {21, 22, 23, 24, 25})
        self.assertEqual(rep["candidates"], [])
        reasons = {s["pr"]: s["reason"] for s in rep["skipped"]}
        self.assertIn("sau hạn", reasons[23])
        self.assertIn("CHANGES_REQUESTED", reasons[25])
        # Không tạo nhánh, PR, tag hay ghi gì lên GitHub.
        self.assertEqual(w.gh.writes(), [])

    def test_shadow_would_merge_yes_does_not_count_as_approval(self):
        # Shadow evaluator chỉ ghi audit "would-merge": batch không đọc nó làm phê duyệt.
        w = self.w
        sha = w.make_pr(26, "shadow", {"docs/26.md": "x\n"}, approve=False)
        w.audit.append(action="would-merge", result="yes", agent="shadow-eval", served_model=None, vendor=None,
                       model_id_source=None, sha=sha, task_id=None, attempt=None, pr=26)
        rep = self.run_window()
        self.assertEqual(rep["status"], "nothing_to_release")
        self.assertEqual(w.gh.merge_puts(), [])

    def test_owner_only_and_red_ci_are_never_batched(self):
        w = self.w
        w.make_pr(27, "owner", {"release/x.py": "X = 1\n"})
        w.make_pr(28, "red-ci", {"src/app.py": "A = 9\nB = 2\nC = 3\n"}, ci="failure")
        rep = self.run_window()
        self.assertEqual(w.gh.merge_puts(), [])
        d = read_digest(w.state_dir, WINDOW)
        tiers = {e["pr"]: (e["tier"], e["batch_eligible"]) for e in d["entries"]}
        self.assertEqual(tiers[27], ("owner-only", False))
        self.assertEqual(tiers[28], ("red", False))
        self.assertEqual(rep["status"], "nothing_to_release")


class MergeIdentityTest(BatchTestCase):
    def test_empty_allowlist_or_unknown_identity_merges_nothing(self):
        self.w.make_pr(31, "a", {"docs/31.md": "x\n"})
        rep = self.run_window(settings=replace(self.w.settings, merge_identities=()))
        self.assertEqual(rep["status"], "skipped")
        self.assertIn("merge_identities", rep["reason"])
        self.assertEqual(self.w.gh.merge_puts(), [])

    def test_identity_not_in_allowlist(self):
        self.w.make_pr(32, "a", {"docs/32.md": "x\n"})
        self.w.gh.login = "intruder"
        rep = self.run_window()
        self.assertEqual(rep["status"], "skipped")
        self.assertEqual(rep["merge_identity"], "intruder")
        self.assertEqual(self.w.gh.merge_puts(), [])

    def test_no_merge_token_means_no_writer(self):
        self.w.make_pr(33, "a", {"docs/33.md": "x\n"})
        rep = self.run_window(writer=None)
        self.assertEqual(rep["status"], "skipped")
        self.assertIn("HARNESS_MERGE_TOKEN", rep["reason"])

    def test_merge_identity_never_merges_its_own_pr(self):
        self.w.make_pr(34, "mine", {"docs/34.md": "x\n"}, author=MERGER)
        self.w.make_pr(35, "theirs", {"docs/35.md": "y\n"})
        rep = self.run_window()
        self.assertEqual(self.w.gh.merge_order, [35])
        self.assertIn("tác giả", next(s["reason"] for s in rep["skipped"] if s["pr"] == 34))


class BindingTest(BatchTestCase):
    def test_head_changed_after_digest_is_skipped(self):
        w = self.w
        w.make_pr(41, "moving", {"docs/41.md": "v1\n"})
        digest, batch = self.components()
        digest.build(job_for(WINDOW, "digest", SCHED))
        new = w.make_pr(41, "moving", {"docs/41.md": "v2\n"}, approve=False, update=True)
        w.gh.review(41, sha=new)  # người dùng duyệt head mới — vẫn khác head đã đóng băng
        rep = batch.run(job_for(WINDOW, "batch", SCHED))
        self.assertEqual(w.gh.merge_puts(), [])
        self.assertIn("head đổi sau digest", rep["skipped"][0]["reason"])

    def test_integration_conflict_drops_only_the_conflicting_pr(self):
        w = self.w
        w.make_pr(42, "p1", {"src/app.py": "A = 1\nB = 20\nC = 3\n"})
        w.make_pr(43, "p2", {"src/app.py": "A = 1\nB = 200\nC = 3\n"})
        w.make_pr(44, "p3", {"docs/44.md": "ok\n"})
        rep = self.run_window()
        self.assertEqual(rep["status"], "released", rep)
        self.assertEqual(w.gh.merge_order, [42, 44])
        self.assertIn("conflict", next(s["reason"] for s in rep["skipped"] if s["pr"] == 43))

    def test_patch_id_change_during_integration_invalidates_approval_with_range_diff(self):
        w = self.w
        # PR 46 sửa dòng cách dòng PR 45 sửa MỘT dòng: merge sạch, nhưng dòng của PR 45 nằm trong
        # ngữ cảnh diff của PR 46 → patch-id khi tích hợp khác patch-id đã duyệt.
        lines = [f"line {i}" for i in range(1, 10)]
        w.push_main({"docs/long.md": "\n".join(lines) + "\n"}, msg="thêm docs/long.md")
        v45, v46 = list(lines), list(lines)
        v45[2], v46[4] = "line 3 CHANGED", "line 5 CHANGED"
        w.make_pr(45, "p1", {"docs/long.md": "\n".join(v45) + "\n"})
        w.make_pr(46, "p2", {"docs/long.md": "\n".join(v46) + "\n"})
        rep = self.run_window()
        skipped = {s["pr"]: s for s in rep["skipped"]}
        self.assertEqual(w.gh.merge_order, [45])
        self.assertIn("patch-id", skipped[46]["reason"])
        self.assertTrue(skipped[46].get("range_diff"))
        self.assertEqual(rep["status"], "released")
        # Nhánh tích hợp được dựng lại KHÔNG có PR rớt; nhánh cũ đã xóa.
        self.assertEqual(rep["integration"]["attempt"], 2)
        self.assertEqual([s["pr"] for s in rep["integration"]["steps"]], [45])

    def test_approval_dismissed_between_integration_and_merge_stops(self):
        w = self.w
        w.make_pr(47, "a", {"docs/47.md": "x\n"})
        w.make_pr(48, "b", {"docs/48.md": "y\n"})
        orig = w.gh.ci_policy

        def ci_then_dismiss(sha):  # trong lúc chờ CI tích hợp, người dùng đổi ý với PR 48
            w.gh.review(48, state="DISMISSED", at="2026-10-26T02:45:00Z")
            return orig(sha)

        w.gh.ci_policy = ci_then_dismiss
        rep = self.run_window()
        self.assertEqual(w.gh.merge_order, [47])
        self.assertEqual(rep["status"], "partial")
        self.assertIn("phê duyệt không còn hiệu lực", rep["reason"])
        self.assertTrue(rep["incident_id"])  # merge một phần → đóng băng
        self.assertEqual(active_incidents(w.state_dir)[0]["status"], "frozen")
        self.assertIsNone(rep["tag"])


class IntegrationCiTest(BatchTestCase):
    def test_red_integrated_ci_merges_nothing(self):
        w = self.w
        w.make_pr(51, "a", {"docs/51.md": "x\n"})
        w.make_pr(52, "b", {"docs/52.md": "y\n"})
        w.gh.ci_policy = lambda sha: "failure"
        rep = self.run_window()
        self.assertEqual(rep["status"], "integration_ci_failed")
        self.assertEqual(w.gh.merge_puts(), [])
        self.assertEqual(w.gh.pulls[rep["integration"]["pr"]]["state"], "closed")

    def test_pending_ci_times_out(self):
        w = self.w
        w.make_pr(53, "a", {"docs/53.md": "x\n"})
        w.gh.ci_policy = lambda sha: "pending"
        digest, batch = self.components()
        digest.build(job_for(WINDOW, "digest", SCHED))
        clock = iter(range(0, 1000, 2))
        batch.monotonic = lambda: next(clock)
        sleeps = []
        batch.sleep = sleeps.append
        rep = batch.run(job_for(WINDOW, "batch", SCHED))
        self.assertEqual(rep["status"], "integration_ci_failed")
        self.assertIn("quá", rep["integration"]["ci"])
        self.assertTrue(sleeps)
        self.assertEqual(w.gh.merge_puts(), [])

    def test_error_while_waiting_for_ci_cleans_up_integration_and_merges_nothing(self):
        w = self.w
        w.make_pr(55, "a", {"docs/55.md": "x\n"})

        def boom(sha):
            raise RuntimeError("GitHub API sập")

        w.gh.ci_policy = boom
        rep = self.run_window()
        self.assertEqual(rep["status"], "failed")
        self.assertIn("GitHub API sập", rep["reason"])
        self.assertEqual(w.gh.merge_puts(), [])
        self.assertIsNone(rep["incident_id"])  # chưa merge gì → không đóng băng
        self.assertEqual(w.gh.pulls[rep["integration"]["pr"]]["state"], "closed")
        self.assertNotEqual(git(w.remote, "rev-parse", "--verify", "--quiet",
                                f"refs/heads/harness/batch/{WINDOW}-1", check=False).returncode, 0)

    def test_main_moved_by_someone_else_stops_before_merge(self):
        w = self.w
        w.make_pr(54, "a", {"docs/54.md": "x\n"})
        orig = w.gh.ci_policy

        def ci_and_push(sha):
            w.push_main({"docs/guide.md": "# guide\n\nđổi ngoài batch\n"})
            return orig(sha)

        w.gh.ci_policy = ci_and_push
        rep = self.run_window()
        self.assertEqual(rep["status"], "aborted_before_merge")
        self.assertIn("đã đổi ngoài batch", rep["reason"])
        self.assertEqual(w.gh.merge_puts(), [])


class KillSwitchTest(BatchTestCase):
    def test_engaged_killswitch_stops_before_anything(self):
        self.w.make_pr(61, "a", {"docs/61.md": "x\n"})
        digest, batch = self.components()
        digest.build(job_for(WINDOW, "digest", SCHED))
        write_killswitch(self.w.ks_path, "on")
        calls = len(self.w.gh.calls)
        from orchestrator.killswitch import KillSwitchEngaged
        with self.assertRaises(KillSwitchEngaged):
            batch.run(job_for(WINDOW, "batch", SCHED))
        self.assertEqual(len(self.w.gh.calls), calls)
        self.assertFalse((self.w.state_dir / "batches").exists())

    def test_flip_mid_batch_stops_immediately_and_freezes_partial_merge(self):
        w = self.w
        w.make_pr(62, "a", {"docs/62.md": "x\n"})
        w.make_pr(63, "b", {"docs/63.md": "y\n"})
        digest, _ = self.components()
        digest.build(job_for(WINDOW, "digest", SCHED))
        # Người dùng bật kill-switch ngay sau khi PR 62 được merge (lần "merge" thứ hai bị chặn).
        flip = FlipKillSwitch(w, None)
        merges = []

        def check(action):
            flip.checks.append(action)
            if action == "batch.merge":
                merges.append(action)
                if len(merges) == 2:
                    write_killswitch(w.ks_path, "on")
            return w.killswitch.check(action)

        flip.check = check
        _, batch = self.components(killswitch=flip)
        rep = batch.run(job_for(WINDOW, "batch", SCHED))
        self.assertEqual(rep["status"], "stopped_killswitch")
        self.assertEqual(w.gh.merge_order, [62])
        self.assertEqual(len(w.gh.merge_puts()), 1)
        self.assertTrue(rep["incident_id"])  # merge một phần → đóng băng
        # Sau khi bật: không merge, không tag, không kiểm tra, không thao tác nào khác.
        self.assertEqual(flip.checks[-1], "batch.merge")
        self.assertEqual(self.check.calls, [])
        self.assertIsNone(rep["tag"])
        self.assertFalse([p for m, p, _b in w.gh.calls if m == "POST" and p.endswith("git/tags")])


class ReleaseCheckAndRevertTest(BatchTestCase):
    def test_failed_check_opens_revert_pr_and_incident_without_merging_it(self):
        w = self.w
        w.make_pr(71, "a", {"docs/71.md": "x\n"})
        w.make_pr(72, "b", {"src/app.py": "A = 1\nB = 2\nC = 33\n"})
        self.check.ok = False
        rep = self.run_window()
        self.assertEqual(rep["status"], "release_check_failed")
        rv = rep["revert"]
        self.assertEqual(rv["status"], "pr_opened")
        revert_pr = w.gh.pulls[rv["pr"]]
        self.assertEqual(revert_pr["head"]["ref"], "revert/rel-20261026-1200")
        self.assertFalse(revert_pr["draft"])
        self.assertNotIn(rv["pr"], w.gh.merge_order)  # runner KHÔNG merge PR revert
        self.assertEqual(w.gh.merge_order, [71, 72])
        # Nhánh revert đi ra từ main hiện tại, chỉ THÊM commit, revert theo thứ tự NGƯỢC manifest.
        main = self.main_sha()
        head = w.remote_sha("refs/heads/revert/rel-20261026-1200")
        self.assertTrue(git(w.work, "merge-base", "--is-ancestor", main, head, check=False).returncode == 0)
        subjects = out(w.work, "log", "--format=%s", f"{main}..{head}").splitlines()
        self.assertEqual(len(subjects), 2)
        self.assertIn("#72", subjects[1])  # cũ nhất trong log = revert đầu tiên = PR mới nhất
        self.assertIn("#71", subjects[0])
        self.assertEqual(w.file_at(head, "src/app.py"), "A = 1\nB = 2\nC = 3")
        (inc,) = active_incidents(w.state_dir)
        rec = w.read_json(f"incidents/{inc['incident_id']}.json")
        common.validate("incident", rec)
        self.assertEqual((rec["opened_by"], rec["trigger"], rec["status"]), ("runner", "readonly_check_failed", "open"))
        self.assertEqual(rec["revert"]["status"], "pr_opened")
        self.assertEqual(rec["release"]["manifest_id"], rep["manifest_id"])
        self.assertIn("kill-switch", rec["timeline"][0]["note"])
        self.assertEqual(w.notifier.rows[-1]["kind"], "escalated")
        # Incident đang mở → batch sau không chạy.
        w.make_pr(73, "c", {"docs/73.md": "z\n"})
        w.now = kst(17, 0, 30)
        digest, batch = self.components()
        digest.build(job_for("20261026-1700", "digest", SCHED))
        rep2 = batch.run(job_for("20261026-1700", "batch", SCHED))
        self.assertEqual(rep2["status"], "skipped")
        self.assertIn("incident", rep2["reason"])

    def test_revert_conflict_freezes_and_opens_no_pr(self):
        w = self.w
        w.make_pr(74, "a", {"src/app.py": "A = 1\nB = 2\nC = 44\n"})

        def later_commit():  # sau release, ai đó sửa đúng dòng PR 74 đã sửa → revert conflict
            w.push_main({"src/app.py": "A = 1\nB = 2\nC = 4444\n"})

        self.check.ok, self.check.side_effect = False, later_commit
        rep = self.run_window()
        rv = rep["revert"]
        self.assertEqual(rv["status"], "conflict")
        self.assertIn("src/app.py", rv["conflict"]["files"])
        self.assertIsNone(rv["pr"])
        self.assertNotEqual(git(w.remote, "rev-parse", "--verify", "--quiet", "refs/heads/revert/rel-20261026-1200",
                                check=False).returncode, 0)
        (inc,) = active_incidents(w.state_dir)
        self.assertEqual(inc["status"], "frozen")
        rec = w.read_json(f"incidents/{inc['incident_id']}.json")
        self.assertEqual(rec["revert"]["status"], "conflict")
        self.assertEqual(git(w.work, "status", "--porcelain").stdout, b"")  # clone làm việc sạch

    def test_release_lock_held_by_someone_else_skips(self):
        from orchestrator.lock import PidLock, ProcessIdentity
        import os
        other = ProcessIdentity.of(os.getppid())
        if not other.alive():
            self.skipTest("không có process cha còn sống")
        self.w.make_pr(75, "a", {"docs/75.md": "x\n"})
        digest, batch = self.components()
        digest.build(job_for(WINDOW, "digest", SCHED))
        with PidLock(self.w.state_dir / "release.lock", owner=other, meta={"purpose": "readonly_check"}):
            rep = batch.run(job_for(WINDOW, "batch", SCHED))
        self.assertEqual(rep["status"], "skipped")
        self.assertIn("release.lock", rep["reason"])
        self.assertEqual(self.w.gh.merge_puts(), [])


class DigestTest(BatchTestCase):
    def test_digest_is_frozen_once_and_never_rewritten(self):
        w = self.w
        h = w.make_pr(81, "a", {"docs/81.md": "x\n"})
        w.make_pr(82, "draft", {"docs/82.md": "x\n"})
        w.gh.pulls[82]["draft"] = True
        digest, _ = self.components()
        r1 = digest.build(job_for(WINDOW, "digest", SCHED))
        self.assertEqual(r1["status"], "frozen")
        path = w.state_dir / "digests" / f"{WINDOW}.json"
        raw = path.read_bytes()
        d = json.loads(raw)
        common.validate("digest", d)
        (e,) = d["entries"]
        self.assertEqual((e["pr"], e["head_sha"], e["tier"], e["ci"], e["batch_eligible"]), (81, h, "green", "success", True))
        mb = out(w.work, "merge-base", d["base_sha"], h)
        diff = git(w.work, "diff", "--full-index", "--binary", "--no-renames", mb, h, "--").stdout
        expected = git(w.work, "patch-id", "--stable", input=diff).stdout.decode().split()[0]
        self.assertEqual(e["patch_id"], expected)
        self.assertEqual(d["excluded"], [{"pr": 82, "reason": "draft"}])
        self.assertEqual((d["cutoff"], d["batch_at"]), ("2026-10-26T11:55:00+09:00", "2026-10-26T12:00:00+09:00"))
        w.make_pr(83, "late-pr", {"docs/83.md": "x\n"})
        r2 = digest.build(job_for(WINDOW, "digest", SCHED))
        self.assertEqual(r2["status"], "already_frozen")
        self.assertEqual(path.read_bytes(), raw)
        self.assertIn("11:55", w.notifier.rows[0]["message"])

    def test_killswitch_on_writes_no_digest(self):
        write_killswitch(self.w.ks_path, "on")
        digest, _ = self.components()
        from orchestrator.killswitch import KillSwitchEngaged
        with self.assertRaises(KillSwitchEngaged):
            digest.build(job_for(WINDOW, "digest", SCHED))
        self.assertFalse((self.w.state_dir / "digests").exists())
        self.assertEqual(self.w.gh.calls, [])

    def test_batch_without_digest_is_skipped(self):
        self.w.make_pr(84, "a", {"docs/84.md": "x\n"})
        _, batch = self.components()
        rep = batch.run(job_for(WINDOW, "batch", SCHED))
        self.assertEqual(rep["status"], "skipped")
        self.assertIn("digest", rep["reason"])
        self.assertEqual(self.w.gh.merge_puts(), [])


if __name__ == "__main__":
    unittest.main()
