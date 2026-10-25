"""CI theo SHA chỉ từ check run GitHub Actions (PR-3, R1, R14): không CI cục bộ, không
output dán tay, check run giả từ app khác bị bỏ qua."""

import ast
import inspect
import unittest
import urllib.parse

from orchestrator.config import REPO_ROOT
from pipeline import ci_status
from pipeline.ci_status import (
    FAILURE,
    MISSING,
    PENDING,
    SUCCESS,
    evaluate,
    fetch_ci_status,
)
from pipeline.github_api import GitHubClient, GitHubError

SHA = "0123456789abcdef0123456789abcdef01234567"
OTHER = "f" * 40


def run(id_, *, name="ci", slug="github-actions", sha=SHA, status="completed", conclusion="success",
        started="2026-10-26T10:00:00Z"):
    return {"id": id_, "name": name, "app": {"slug": slug}, "head_sha": sha, "status": status,
            "conclusion": conclusion, "started_at": started, "html_url": f"https://github.com/x/y/runs/{id_}"}


class FakeHttp:
    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = []

    def __call__(self, url, headers, timeout_s):
        self.calls.append((url, dict(headers)))
        return self.pages.pop(0)


class EvaluateTest(unittest.TestCase):
    def test_success(self):
        st = evaluate([run(1)], SHA)
        self.assertEqual((st.state, st.passed, st.check_run_id, st.next_state), (SUCCESS, True, 1, "gate3_review"))

    def test_failure_and_non_success_conclusions_fail_closed(self):
        for concl in ("failure", "cancelled", "timed_out", "skipped", "neutral", "action_required", None):
            with self.subTest(conclusion=concl):
                st = evaluate([run(1, conclusion=concl)], SHA)
                self.assertEqual((st.state, st.next_state), (FAILURE, "fix_list"))

    def test_pending_and_missing_block(self):
        self.assertEqual(evaluate([run(1, status="in_progress", conclusion=None)], SHA).state, PENDING)
        st = evaluate([], SHA)
        self.assertEqual((st.state, st.next_state, st.passed), (MISSING, "blocked", False))

    def test_check_run_from_another_app_is_ignored(self):
        # Commit status/check giả cùng tên "ci" (N1) không được tính.
        st = evaluate([run(9, slug="evil-app")], SHA)
        self.assertEqual(st.state, MISSING)
        self.assertEqual(len(st.ignored), 1)
        st = evaluate([run(9, slug="evil-app"), run(1, conclusion="failure")], SHA)
        self.assertEqual(st.state, FAILURE)

    def test_check_run_for_another_sha_or_name_is_ignored(self):
        self.assertEqual(evaluate([run(1, sha=OTHER)], SHA).state, MISSING)
        self.assertEqual(evaluate([run(1, name="lint")], SHA).state, MISSING)

    def test_latest_rerun_wins(self):
        runs = [run(1, conclusion="failure", started="2026-10-26T10:00:00Z"),
                run(2, conclusion="success", started="2026-10-26T11:00:00Z")]
        self.assertEqual(evaluate(runs, SHA).state, SUCCESS)
        self.assertEqual(evaluate(list(reversed(runs)), SHA).check_run_id, 2)
        runs.append(run(3, status="queued", conclusion=None, started="2026-10-26T12:00:00Z"))
        self.assertEqual(evaluate(runs, SHA).state, PENDING)

    def test_sha_must_be_full_lowercase_hex(self):
        for bad in ("abc123", SHA.upper(), "", None, SHA + "0"):
            with self.subTest(sha=bad), self.assertRaises(ValueError):
                evaluate([], bad)

    def test_pasted_text_is_not_accepted(self):
        with self.assertRaises((GitHubError, TypeError, AttributeError)):
            evaluate(["ci: success\n231 passed"], SHA)
        with self.assertRaises((ValueError, TypeError)):
            fetch_ci_status(None, "=== 231 passed in 35s ===")  # type: ignore[arg-type]


class FetchTest(unittest.TestCase):
    def test_reads_check_runs_by_sha_with_pagination(self):
        page1 = {"total_count": 101, "check_runs": [run(i, name="other") for i in range(1, 100)] + [run(100)]}
        page2 = {"total_count": 101, "check_runs": [run(101, conclusion="failure", started="2026-10-26T09:00:00Z")]}
        http = FakeHttp([(200, page1), (200, page2)])
        client = GitHubClient("suanlong06/Agents", token="t0k", http_get=http)
        st = fetch_ci_status(client, SHA)
        self.assertEqual((st.state, st.check_run_id), (SUCCESS, 100))
        url, headers = http.calls[0]
        parsed = urllib.parse.urlparse(url)
        self.assertEqual(parsed.path, f"/repos/suanlong06/Agents/commits/{SHA}/check-runs")
        q = urllib.parse.parse_qs(parsed.query)
        self.assertEqual((q["check_name"], q["page"]), (["ci"], ["1"]))
        self.assertEqual(headers["Authorization"], "Bearer t0k")
        self.assertEqual(len(http.calls), 2)

    def test_http_error_raises(self):
        client = GitHubClient("o/r", http_get=FakeHttp([(404, {"message": "Not Found"})]))
        with self.assertRaises(GitHubError):
            fetch_ci_status(client, SHA)

    def test_client_refuses_non_https_and_bad_repo(self):
        with self.assertRaises(GitHubError):
            GitHubClient("o/r", api_url="http://api.github.com")
        with self.assertRaises(GitHubError):
            GitHubClient("o/r/../x")


class NoLocalCiTest(unittest.TestCase):
    def test_module_never_runs_processes(self):
        tree = ast.parse((REPO_ROOT / "pipeline" / "ci_status.py").read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                imported.add(node.module.split(".")[0])
        self.assertFalse(imported & {"subprocess", "os", "pty", "multiprocessing", "shutil"})

    def test_public_functions_take_no_text_or_log_parameter(self):
        for fn in (evaluate, fetch_ci_status):
            params = set(inspect.signature(fn).parameters)
            self.assertFalse(params & {"output", "log", "text", "stdout", "pasted", "report"})
        self.assertFalse(hasattr(ci_status, "run_local_ci"))


if __name__ == "__main__":
    unittest.main()
