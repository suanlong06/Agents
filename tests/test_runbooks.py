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
NAMES = ("release.md", "readonly_check.md", "revert.md", "incident.md", "batch.md")
FENCE = re.compile(r"```(\w*)\n(.*?)```", re.DOTALL)
FORBIDDEN = re.compile(r"push\s+.*(--force|--force-with-lease|\s-f\b|\s\+\S)|reset\s+--hard|\btag\s+-d\b|"
                       r"push\s+.*--delete|\bgit\s+reset\b")


def working_powershell(candidates=("pwsh", "powershell", "powershell.exe"), *, which=shutil.which,
                       run=subprocess.run) -> str | None:
    """PowerShell đầu tiên CHẠY ĐƯỢC (PR-4b, F3).

    ``which("pwsh")`` có thể trỏ tới một stub hỏng (ví dụ alias App Execution trong
    ``WindowsApps`` khi gói chưa cài xong): probe ``-Command`` một lệnh vô hại trước, stub
    không chạy được thì thử ứng viên kế tiếp (``powershell.exe`` của Windows)."""
    seen = set()
    for name in candidates:
        path = which(name)
        if not path or os.path.normcase(path) in seen:
            continue
        seen.add(os.path.normcase(path))
        try:
            proc = run([path, "-NoProfile", "-NonInteractive", "-Command", "Write-Output probe-ok"],
                       capture_output=True, stdin=subprocess.DEVNULL, timeout=60)
        except (OSError, subprocess.SubprocessError):
            continue
        if proc.returncode == 0 and b"probe-ok" in (proc.stdout or b""):
            return path
    return None


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

    def test_batch_runbook_states_no_auto_merge_and_the_os_user_prerequisite(self):
        batch = " ".join((RUNBOOKS / "batch.md").read_text(encoding="utf-8").split())
        for phrase in ("CHƯA KÍCH HOẠT", "Auto-merge vẫn TẮT", "OPEN DISCUSSION", "(head SHA, patch-id)",
                       "OS user hiện tại", "điều kiện triển khai còn mở", "ACL trên thư mục state",
                       "KHÔNG chống giả mạo", "không dùng merge queue native", "CI trên kết quả tích hợp",
                       "bỏ qua và thông báo", "release.lock", "thứ tự **ngược** manifest",
                       "chỉ người dùng duyệt và merge"):
            self.assertIn(phrase, batch)

    def test_release_step7_pins_the_check_to_the_released_tag(self):
        # F2: lệnh kiểm tra ở runbook phải có --expect-tag và chạy từ runtime checkout.
        cmds = [l.strip() for b in blocks("release.md", "powershell") + blocks("readonly_check.md", "powershell")
                for l in b.splitlines() if "readonly_check run" in l]
        self.assertTrue(cmds)
        for c in cmds:
            self.assertIn("--expect-tag", c)
        self.assertIn("Set-Location <runtime-checkout>", "\n".join(blocks("release.md", "powershell")))

    def test_broken_pwsh_stub_falls_back_to_windows_powershell(self):
        # F3: pwsh trỏ tới stub hỏng (exit khác 0 / không chạy được) → thử powershell.exe.
        paths = {"pwsh": r"C:\Users\u\AppData\Local\Microsoft\WindowsApps\pwsh.exe",
                 "powershell": r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"}
        calls = []

        def run(argv, **kw):
            calls.append(argv[0])
            if argv[0] == paths["pwsh"]:
                return subprocess.CompletedProcess(argv, 9009, b"", b"stub")
            self.assertEqual(argv[1:4], ["-NoProfile", "-NonInteractive", "-Command"])
            return subprocess.CompletedProcess(argv, 0, b"probe-ok\r\n", b"")

        self.assertEqual(working_powershell(which=paths.get, run=run), paths["powershell"])
        self.assertEqual(calls, [paths["pwsh"], paths["powershell"]])

        def run_oserror(argv, **kw):
            raise OSError("stub không chạy được")

        self.assertIsNone(working_powershell(which=paths.get, run=run_oserror))
        self.assertIsNone(working_powershell(which=lambda _n: None, run=run))

    def test_powershell_killswitch_commands_work_with_the_fail_closed_reader(self):
        shell = working_powershell()
        if shell is None:
            self.skipTest("không có PowerShell chạy được (pwsh/powershell.exe)")
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
