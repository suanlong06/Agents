"""Thế giới giả cho test PR-4b: remote git THẬT (bare) + GitHub giả chạy merge bằng git thật.

- ``remote.git``: repo bare đóng vai GitHub (cấm non-fast-forward: không ai force-push được).
- ``dev``: clone để "bot" viết PR (đẩy nhánh và ``refs/pull/N/head``).
- ``work``: clone làm việc của batch (``batch.workdir``).
- ``FakeGitHub``: REST tối thiểu. ``POST merges`` / ``PUT pulls/N/merge`` merge bằng ``git merge
  --no-ff`` trong clone ``server`` rồi đẩy lên remote, như engine merge của GitHub. ``PUT merge``
  còn mô phỏng branch protection: head phải đúng ``sha`` gửi lên và có review APPROVED của owner.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import tempfile
import urllib.parse
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from orchestrator.config import REPO_ROOT
from orchestrator.fsio import WriterGuard
from orchestrator.audit import AuditLog
from orchestrator.killswitch import KillSwitch
from pipeline.github_api import GitHubClient
from release.batch_config import BatchSettings
from release.github_write import GitHubWriter
from release.gitops import Git
from tests.killswitch_fixtures import write_killswitch

KST = ZoneInfo("Asia/Seoul")
REPO = "suanlong06/Agents"
OWNER = "suanlong06"
BOT = "harness-bot"
MERGER = "harness-merger"
GIT_ENV = {"GIT_AUTHOR_NAME": "harness-test", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "harness-test", "GIT_COMMITTER_EMAIL": "t@example.invalid",
           "GIT_CONFIG_NOSYSTEM": "1"}


def git(cwd, *args, check=True, input=None) -> subprocess.CompletedProcess:
    env = dict(os.environ, **GIT_ENV)
    proc = subprocess.run(["git", "-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false", "-c", "core.autocrlf=false",
                           "-C", str(cwd), *args], capture_output=True, env=env, input=input,
                          stdin=None if input is not None else subprocess.DEVNULL, check=False)
    if check and proc.returncode != 0:
        raise AssertionError(f"git {args}: {proc.stderr.decode('utf-8', 'replace')}")
    return proc


def out(cwd, *args) -> str:
    return git(cwd, *args).stdout.decode("utf-8", "replace").strip()


def rmtree(path: Path) -> None:
    def onexc(func, p, _exc):
        os.chmod(p, stat.S_IWRITE)
        func(p)

    shutil.rmtree(path, onexc=onexc)


def kst(h, m, s=0, day=26) -> datetime:
    return datetime(2026, 10, day, h, m, s, tzinfo=KST)


class ListNotifier:
    def __init__(self):
        self.rows = []

    def notify(self, *, kind, task_id, message, detail=None):
        self.rows.append({"kind": kind, "message": message, "detail": dict(detail or {})})
        return self.rows[-1]


class FakeGitHub:
    def __init__(self, world: "World"):
        self.w = world
        self.pulls: dict[int, dict] = {}
        self.reviews: dict[int, list[dict]] = {}
        self.check_runs: dict[str, list[dict]] = {}
        self.ci_policy = lambda sha: "success"  # CI tự chạy cho SHA chưa có check run (PR tích hợp)
        self.login = MERGER
        self.calls: list[tuple[str, str, object]] = []
        self.merge_order: list[int] = []
        self.next_pr = 900
        self.next_review = 5000
        self.fail_merge_for: set[int] = set()

    # --- dữ liệu ---

    def add_pull(self, n, *, head, branch, author=BOT, title=None, draft=False):
        self.pulls[n] = {"number": n, "state": "open", "draft": draft, "merged": False, "title": title or f"PR {n}",
                         "html_url": f"https://github.com/{REPO}/pull/{n}", "user": {"login": author},
                         "base": {"ref": "main"}, "head": {"sha": head, "ref": branch}}
        self.reviews.setdefault(n, [])

    def review(self, n, *, state="APPROVED", sha=None, login=OWNER, at="2026-10-26T02:40:00Z"):
        self.next_review += 1
        self.reviews[n].append({"id": self.next_review, "state": state, "user": {"login": login},
                                "commit_id": sha or self.pulls[n]["head"]["sha"], "submitted_at": at,
                                "html_url": f"https://github.com/{REPO}/pull/{n}#r{self.next_review}"})
        return self.next_review

    def set_ci(self, sha, conclusion="success", status="completed", app="github-actions"):
        self.check_runs[sha] = [{"id": len(self.check_runs) + 1, "name": "ci", "head_sha": sha, "status": status,
                                 "conclusion": conclusion if status == "completed" else None,
                                 "app": {"slug": app}, "started_at": "2026-10-26T02:00:00Z",
                                 "html_url": "https://github.com/x"}]

    # --- HTTP ---

    def http_get(self, url, headers, timeout_s):
        return self.request("GET", url, headers, None, timeout_s)

    def request(self, method, url, headers, body, timeout_s):
        u = urllib.parse.urlparse(url)
        path = u.path
        q = dict(urllib.parse.parse_qsl(u.query))
        self.calls.append((method, path, body))
        if path == "/user":
            return 200, {"login": self.login}
        prefix = f"/repos/{REPO}/"
        assert path.startswith(prefix), path
        rest = path[len(prefix):]
        parts = rest.split("/")
        if method == "GET":
            if q.get("page", "1") != "1":
                return 200, {"check_runs": []} if rest.endswith("check-runs") else []
            if rest == "pulls":
                return 200, [p for p in self.pulls.values() if p["state"] == "open" and p["base"]["ref"] == q.get("base")]
            if parts[0] == "pulls" and len(parts) == 2:
                return (200, self.pulls[int(parts[1])]) if int(parts[1]) in self.pulls else (404, {"message": "nf"})
            if parts[0] == "pulls" and parts[2:] == ["reviews"]:
                return 200, list(self.reviews.get(int(parts[1]), []))
            if parts[0] == "commits" and parts[2:] == ["check-runs"]:
                sha = parts[1]
                if sha not in self.check_runs:
                    c = self.ci_policy(sha)
                    self.set_ci(sha, None, "in_progress") if c == "pending" else self.set_ci(sha, c)
                return 200, {"check_runs": self.check_runs[sha]}
            if rest == "git/ref/heads/main":
                return 200, {"object": {"sha": self.w.remote_sha("refs/heads/main")}}
        if method == "POST" and rest == "git/refs":
            ref, sha = body["ref"], body["sha"]
            if git(self.w.remote, "rev-parse", "--verify", "--quiet", ref, check=False).returncode == 0:
                return 422, {"message": "Reference already exists"}
            git(self.w.remote, "update-ref", ref, sha)
            return 201, {"ref": ref}
        if method == "POST" and rest == "merges":
            sha = self.w.server_merge(body["base"], body["head"], body["commit_message"])
            return (409, {"message": "Merge conflict"}) if sha is None else (201, {"sha": sha})
        if method == "POST" and rest == "pulls":
            self.next_pr += 1
            n = self.next_pr
            head = self.w.remote_sha(f"refs/heads/{body['head']}")
            git(self.w.remote, "update-ref", f"refs/pull/{n}/head", head)  # như GitHub: mọi PR có refs/pull/N/head
            self.add_pull(n, head=head, branch=body["head"], author=self.login, title=body["title"],
                          draft=body["draft"])
            return 201, {"number": n}
        if method == "PATCH" and parts[0] == "pulls":
            self.pulls[int(parts[1])]["state"] = body["state"]
            return 200, self.pulls[int(parts[1])]
        if method == "DELETE" and rest.startswith("git/refs/heads/"):
            git(self.w.remote, "update-ref", "-d", "refs/heads/" + urllib.parse.unquote(rest[len("git/refs/heads/"):]))
            return 204, None
        if method == "POST" and rest == "git/tags":  # object tag CHƯA có ref (ref do POST git/refs tạo)
            obj = (f"object {body['object']}\ntype commit\ntag {body['tag']}\n"
                   f"tagger harness-test <t@example.invalid> 1793000000 +0900\n\n{body['message']}\n")
            return 201, {"sha": git(self.w.remote, "mktag", input=obj.encode("utf-8")).stdout.decode().strip()}
        if method == "PUT" and parts[0] == "pulls" and parts[2:] == ["merge"]:
            n = int(parts[1])
            p = self.pulls[n]
            if p["head"]["sha"] != body["sha"]:
                return 409, {"message": "Head branch was modified"}
            approved = [r for r in self.reviews[n] if r["user"]["login"] == OWNER and r["state"] == "APPROVED"
                        and r["commit_id"] == body["sha"]]
            if not approved or n in self.fail_merge_for:
                return 405, {"message": "At least 1 approving review is required"}
            sha = self.w.server_merge("main", body["sha"], f"Merge pull request #{n} from {BOT}/{p['head']['ref']}")
            if sha is None:
                return 405, {"message": "not mergeable"}
            p.update(state="closed", merged=True)
            self.merge_order.append(n)
            return 200, {"merged": True, "sha": sha}
        raise AssertionError(f"GitHub giả không hỗ trợ {method} {path}")

    def writes(self):
        return [(m, p) for m, p, _b in self.calls if m != "GET"]

    def merge_puts(self):
        return [p for m, p, _b in self.calls if m == "PUT"]


class World:
    """Remote bare + clone dev/work/server + GitHub giả + state_dir thật."""

    def __init__(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="hb-")).resolve()
        self.remote = self.tmp / "remote.git"
        self.dev = self.tmp / "dev"
        self.work = self.tmp / "w" / "rel"
        self.server = self.tmp / "srv"
        self.state_dir = self.tmp / "state"
        self.state_dir.mkdir()
        git(self.tmp, "init", "-q", "--bare", "-b", "main", str(self.remote))
        git(self.remote, "config", "receive.denyNonFastForwards", "true")
        git(self.tmp, "clone", "-q", str(self.remote), str(self.dev))
        (self.dev / "config").mkdir()
        shutil.copyfile(REPO_ROOT / "config" / "sensitive_paths.yaml", self.dev / "config" / "sensitive_paths.yaml")
        self.write("docs/guide.md", "# guide\n\nline 1\nline 2\nline 3\n")
        self.write("src/app.py", "A = 1\nB = 2\nC = 3\n")
        git(self.dev, "add", "-A")
        git(self.dev, "commit", "-q", "-m", "init")
        git(self.dev, "tag", "-a", "rel-20261025-1700", "-m", "release trước")
        git(self.dev, "push", "-q", "origin", "main", "--tags")
        self.work.parent.mkdir(parents=True, exist_ok=True)
        git(self.tmp, "clone", "-q", str(self.remote), str(self.work))
        git(self.work, "config", "user.name", "harness-test")
        git(self.work, "config", "user.email", "t@example.invalid")
        # "Server" = worktree detached CỦA CHÍNH remote: dùng chung object, merge không cần fetch/push.
        git(self.remote, "worktree", "add", "-q", "--detach", str(self.server), "main")
        self.gh = FakeGitHub(self)
        self.ks_path = self.state_dir / "killswitch.json"
        write_killswitch(self.ks_path, "off")
        self.killswitch = KillSwitch(self.ks_path)
        self.guard = WriterGuard(self.state_dir).acquire()
        self.now = kst(12, 0, 30)
        self.audit = AuditLog(self.state_dir, self.guard, clock=lambda: self.now.isoformat(timespec="milliseconds"))
        self.notifier = ListNotifier()
        self.settings = BatchSettings(workdir=self.work, merge_identities=(MERGER,), ci_poll_s=1, ci_timeout_s=5)
        self.client = GitHubClient(REPO, http_get=self.gh.http_get)
        self.writer = GitHubWriter(REPO, token="t-merge", http=self.gh.request)
        self.git = Git(self.work)

    def close(self):
        self.guard.release()
        rmtree(self.tmp)

    def write(self, rel, text, root=None):
        p = (root or self.dev) / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(text.encode("utf-8"))

    def remote_sha(self, ref) -> str:
        loose = self.remote / ref  # ref lỏng (update-ref/push ghi): đọc thẳng, đỡ một process git
        if ref.startswith("refs/") and loose.is_file():
            return loose.read_text(encoding="ascii").strip()
        return out(self.remote, "rev-parse", ref)

    def server_merge(self, base, head, message) -> str | None:
        """Engine merge của GitHub giả: ``git merge --no-ff`` trong worktree server, rồi tiến ref
        ``base`` theo kiểu compare-and-swap (chỉ fast-forward từ đúng giá trị cũ, không force)."""
        old = self.remote_sha(f"refs/heads/{base}")
        git(self.server, "checkout", "-q", "--detach", old)
        proc = git(self.server, "merge", "--no-ff", "-q", "-m", message, head, check=False)
        if proc.returncode != 0:
            git(self.server, "merge", "--abort", check=False)
            return None
        sha = out(self.server, "rev-parse", "HEAD")
        git(self.remote, "update-ref", f"refs/heads/{base}", sha, old)
        return sha

    def make_pr(self, n, branch, files: dict, *, author=BOT, base="main", approve=True, ci="success",
                title=None, update=False) -> str:
        """PR mới từ ``base``; ``update=True``: thêm commit lên head hiện tại của PR (head đổi)."""
        git(self.dev, "fetch", "-q", "origin")
        git(self.dev, "checkout", "-q", "-B", branch, f"origin/{branch}" if update else f"origin/{base}")
        for rel, text in files.items():
            self.write(rel, text)
        git(self.dev, "add", "-A")
        git(self.dev, "commit", "-q", "-m", f"{branch}: thay đổi")
        sha = out(self.dev, "rev-parse", "HEAD")
        git(self.dev, "push", "-q", "origin", f"HEAD:refs/heads/{branch}", f"HEAD:refs/pull/{n}/head")
        self.gh.add_pull(n, head=sha, branch=branch, author=author, title=title)
        if ci:
            self.gh.set_ci(sha, ci)
        if approve:
            self.gh.review(n, sha=sha)
        return sha

    def push_main(self, files: dict, msg="commit thẳng ngoài batch") -> str:
        git(self.dev, "fetch", "-q", "origin")
        git(self.dev, "checkout", "-q", "-B", "main", "origin/main")
        for rel, text in files.items():
            self.write(rel, text)
        git(self.dev, "add", "-A")
        git(self.dev, "commit", "-q", "-m", msg)
        git(self.dev, "push", "-q", "origin", "HEAD:refs/heads/main")
        return out(self.dev, "rev-parse", "HEAD")

    def file_at(self, ref, rel) -> str:
        git(self.work, "fetch", "-q", "origin", "+refs/heads/*:refs/remotes/origin/*")
        return out(self.work, "show", f"{ref}:{rel}")

    def read_json(self, rel):
        return json.loads((self.state_dir / rel).read_text(encoding="utf-8"))
