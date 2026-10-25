"""Schema có version, tương thích N-1 (R2) và runtime state ở ngoài repo (R13)."""

import json
import os
import shutil
import sys
import unittest
from pathlib import Path

from orchestrator import schemas
from orchestrator.config import ConfigError, REPO_ROOT, load_config
from orchestrator.state import StateStore, read_snapshot
from tests.helpers import FIXTURES, StateDirTestCase


class VersionWindowTest(unittest.TestCase):
    def test_current_and_next_version_are_readable(self):
        for name in schemas.NAMES:
            self.assertEqual(schemas.check_version(name, 1), 1)
            self.assertEqual(schemas.check_version(name, 2), 2)

    def test_versions_outside_window_are_refused(self):
        for bad in (0, 3, 99, None, "1", True):
            with self.assertRaises(schemas.SchemaError):
                schemas.check_version("event", bad)

    def test_all_schemas_are_valid_draft_2020_12(self):
        from jsonschema import Draft202012Validator

        for name in schemas.NAMES:
            Draft202012Validator.check_schema(schemas.load_schema(name))


class StateCompatTest(StateDirTestCase):
    def install_state(self, fixture):
        self.state_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy(FIXTURES / fixture, self.state_dir / StateStore.FILE)

    def test_v1_state_fixture_is_readable(self):
        snap = read_snapshot(FIXTURES / "state_v1.json")
        t = snap["tasks"]["T-1"]
        self.assertEqual((t["attempt"], t["pr"], t["parent_pr"], t["fix_rounds"]), (2, 11, 10, 1))

    def test_future_state_is_readable_and_unknown_fields_are_kept(self):
        # Code bản N đọc state do bản N+1 ghi (ví dụ sau revert) mà không crash.
        self.install_state("state_v2_future.json")
        self.user.emit("task_created", "T-2")
        orch = self.open_orch()
        self.assertEqual(orch.snapshot["tasks"]["T-9"]["state"], "shadow_eval")
        orch.fold(self.sources())
        disk = json.loads((self.state_dir / StateStore.FILE).read_text(encoding="utf-8"))
        self.assertEqual(disk["future_top_level"], {"note": "trường do version 2 thêm vào"})
        self.assertEqual(disk["tasks"]["T-9"]["future_field"], [1, 2, 3])
        self.assertIn("T-2", disk["tasks"])

    def test_task_in_unknown_state_is_read_only_through_fold(self):
        self.install_state("state_v2_future.json")
        self.user.walk("T-9", "cancelled")
        orch = self.open_orch()
        res = orch.fold(self.sources())
        self.assertEqual(res.rejected, 1)
        self.assertEqual(orch.snapshot["tasks"]["T-9"]["state"], "shadow_eval")

    def test_state_version_too_new_is_refused_loudly(self):
        self.state_dir.mkdir(parents=True, exist_ok=True)
        data = json.loads((FIXTURES / "state_v2_future.json").read_text(encoding="utf-8"))
        data["schema_version"] = 3
        (self.state_dir / StateStore.FILE).write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(schemas.SchemaError):
            self.open_orch()

    def test_event_fixture_with_next_version_event_folds(self):
        inbox = self.state_dir / "inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        shutil.copy(FIXTURES / "events_v1.jsonl", inbox / "spec-agent.jsonl")
        orch = self.open_orch()
        res = orch.fold(self.sources())
        self.assertEqual((res.applied, res.quarantined), (4, 0))
        self.assertEqual(orch.snapshot["tasks"]["T-1"]["state"], "spec_review")
        self.assertAlmostEqual(orch.ledger.totals("T-1")["cost_usd"], 0.02)


def repo_files():
    out = set()
    for root, dirs, files in os.walk(REPO_ROOT):
        dirs[:] = [d for d in dirs if d not in (".git", "__pycache__", ".pytest_cache")]
        out.update(os.path.relpath(os.path.join(root, f), REPO_ROOT) for f in files)
    return out


class RuntimeStateOutsideRepoTest(StateDirTestCase):
    def test_state_dir_inside_repo_is_refused(self):
        for inside in (REPO_ROOT, REPO_ROOT / "state"):
            with self.assertRaises(ConfigError):
                load_config(env={"HARNESS_STATE_DIR": str(inside)})

    @unittest.skipUnless(sys.platform == "win32", "đường dẫn mặc định %LOCALAPPDATA% chỉ có trên Windows")
    def test_default_state_dir_is_localappdata_harness(self):
        cfg = load_config(env={"LOCALAPPDATA": r"C:\Users\water\AppData\Local"})
        self.assertEqual(cfg.state_dir, Path(r"C:\Users\water\AppData\Local\harness"))

    def test_env_overrides_state_dir(self):
        self.assertEqual(self.config.state_dir, self.state_dir)

    def test_fold_writes_nothing_into_repo(self):
        before = repo_files()
        self.agent.emit("task_created", "T-1")
        self.agent.call("T-1", "c-1")
        orch = self.open_orch()
        orch.fold(self.sources())
        self.assertEqual(repo_files(), before)
        written = {p.name for p in self.state_dir.iterdir()}
        self.assertLessEqual({"state.json", "ledger.jsonl", "audit.jsonl"}, written)


if __name__ == "__main__":
    unittest.main()
