"""Runbook PR-4a khớp với code: mẫu incident đúng schema, lệnh kill-switch PowerShell trong
runbook thật sự bật/tắt được kill-switch fail-closed, không lệnh nào force-push hay reset, và
tuyên bố best-effort (R15) có mặt."""

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from orchestrator.config import REPO_ROOT
from orchestrator.killswitch import read_status
from pipeline import common

RUNBOOKS = REPO_ROOT / "docs" / "runbooks"
NAMES = ("release.md", "readonly_check.md", "revert.md", "incident.md")
FENCE = re.compile(r"```(\w*)\n(.*?)```", re.DOTALL)
FORBIDDEN = re.compile(r"push\s+.*(--force|--force-with-lease|\s-f\b|\s\+\S)|reset\s+--hard|\btag\s+-d\b|"
                       r"push\s+.*--delete|\bgit\s+reset\b")


def blocks(name: str, lang: str | None = None) -> list[str]:
    text = (RUNBOOKS / name).read_text(encoding="utf-8")
    return [body for l, body in FENCE.findall(text) if lang is None or l == lang]


class RunbookTest(unittest.TestCase):
    def test_all_runbooks_exist(self):
        for n in NAMES:
            self.assertTrue((RUNBOOKS / n).is_file(), n)

    def test_incident_template_matches_schema(self):
        (tpl,) = blocks("incident.md", "json")
        common.validate("incident", json.loads(tpl))

    def test_no_command_force_pushes_or_resets(self):
        for n in NAMES:
            for body in blocks(n):
                for line in body.splitlines():
                    with self.subTest(runbook=n, line=line):
                        self.assertIsNone(FORBIDDEN.search(line))

    def test_trust_statement_and_fail_closed_are_documented(self):
        release = " ".join((RUNBOOKS / "release.md").read_text(encoding="utf-8").split())
        for phrase in ("KHÔNG chống giả mạo", "Thiếu file, không đọc được hay sai định dạng = ĐANG BẬT",
                       "Chỉ người dùng", "không có nút trên dashboard", "would-merge",
                       "không bao giờ gọi API merge", "OPEN DISCUSSION"):
            self.assertIn(phrase, release)
        revert = " ".join((RUNBOOKS / "revert.md").read_text(encoding="utf-8").split())
        for phrase in ("thứ tự ngược manifest", "git revert --abort", "Không force-push", "incident"):
            self.assertIn(phrase, revert)

    def test_powershell_killswitch_commands_work_with_the_fail_closed_reader(self):
        shell = shutil.which("pwsh") or shutil.which("powershell")
        if shell is None:
            self.skipTest("không có PowerShell")
        (ps,) = blocks("release.md", "powershell")[:1]
        lines = [l for l in ps.splitlines() if l.strip() and not l.lstrip().startswith("#")]
        setup = [l for l in lines if l.startswith(("$ks =", "New-Item"))]
        on = next(l for l in lines if "state='on'" in l)
        off = next(l for l in lines if "state='off'" in l)
        tmp = Path(tempfile.mkdtemp(prefix="harness-ps-")).resolve()
        try:
            env = dict(os.environ, LOCALAPPDATA=str(tmp))
            path = tmp / "harness" / "killswitch.json"

            def run(cmd_line):
                script = "\n".join(setup + [cmd_line])
                proc = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-Command", script], env=env,
                                      capture_output=True, stdin=subprocess.DEVNULL, timeout=120)
                self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))

            self.assertTrue(read_status(path).engaged)  # chưa có file → bật
            run(on)
            st = read_status(path)
            self.assertTrue(st.engaged)
            self.assertEqual((st.set_by, st.note), ("user", "release"))
            run(off)
            st = read_status(path)
            self.assertFalse(st.engaged, st.reason)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
