"""Git cho batch release và revert tự động (PR-4b, R9, R10).

- Chạy trên **clone làm việc riêng của batch** (``batch.workdir``, ngoài repo, R13), không bao
  giờ trên checkout của người dùng hay runtime checkout.
- ``patch_id``: ``git diff base head | git patch-id --stable``. Phê duyệt gắn ``(head SHA,
  patch-id)`` (R9): cùng patch-id nghĩa là cùng thay đổi, bất kể base đã đi tới đâu.
- **Không force, không reset**: ``push`` từ chối refspec có ``+`` hay ``--force``; module không
  có lệnh ``reset``. Revert chỉ THÊM commit (R10).
- Xác thực push: token đọc từ biến môi trường (tên trong config), truyền cho git qua
  ``GIT_CONFIG_COUNT``/``GIT_CONFIG_KEY_n`` (header ``http.extraheader``), không bao giờ nằm
  trên dòng lệnh hay trong file config.
"""

from __future__ import annotations

import base64
import os
import re
import subprocess
from pathlib import Path
from typing import Mapping, Sequence

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,200}$")
_STRIP_ENV = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_EXTERNAL_DIFF", "GIT_CONFIG_COUNT")


class GitError(RuntimeError):
    def __init__(self, args: Sequence[str], code: int, stderr: str):
        self.args_, self.code, self.stderr = list(args), code, stderr
        super().__init__(f"git {' '.join(args)} (exit {code}): {stderr.strip()[:500]}")


def check_ref_name(name: str) -> str:
    if not isinstance(name, str) or not _REF_RE.match(name) or ".." in name or name.endswith((".lock", "/")):
        raise ValueError(f"tên ref không hợp lệ: {name!r}")
    return name


class Git:
    def __init__(self, repo: Path | str, *, timeout_s: float = 600.0):
        self.repo = Path(repo)
        self.timeout_s = timeout_s

    def _env(self, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        env = dict(os.environ)
        for k in _STRIP_ENV:
            env.pop(k, None)
        env.update(extra or {})
        return env

    def run(self, *args: str, ok: tuple[int, ...] = (0,), input: bytes | None = None,
            cwd: Path | None = None, env: Mapping[str, str] | None = None) -> subprocess.CompletedProcess:
        where = cwd or self.repo
        proc = subprocess.run(["git", "-c", "core.quotepath=false", "-c", "log.showSignature=false",
                               "-c", "core.autocrlf=false", "-C", str(where), *args],
                              input=input, capture_output=True, env=self._env(env),
                              stdin=None if input is not None else subprocess.DEVNULL,
                              timeout=self.timeout_s, check=False)
        if proc.returncode not in ok:
            raise GitError(args, proc.returncode, proc.stderr.decode("utf-8", "replace"))
        return proc

    def out(self, *args: str, cwd: Path | None = None) -> str:
        return self.run(*args, cwd=cwd).stdout.decode("utf-8", "replace").strip()

    # --- đọc ---------------------------------------------------------------------------

    def rev_parse(self, ref: str) -> str:
        sha = self.out("rev-parse", "--verify", "--quiet", "--end-of-options", f"{ref}^{{commit}}")
        if not SHA_RE.match(sha):
            raise GitError(["rev-parse", ref], 1, "không phân giải được ra commit")
        return sha

    def merge_base(self, a: str, b: str) -> str:
        return self.out("merge-base", a, b)

    def is_ancestor(self, a: str, b: str) -> bool:
        return self.run("merge-base", "--is-ancestor", a, b, ok=(0, 1)).returncode == 0

    def tree_of(self, sha: str) -> str:
        return self.out("rev-parse", "--verify", f"{sha}^{{tree}}")

    def parents(self, sha: str) -> list[str]:
        return self.out("rev-list", "--parents", "-n", "1", sha).split()[1:]

    def diff(self, base: str, head: str) -> bytes:
        return self.run("diff", "--no-color", "--no-ext-diff", "--no-renames", "--full-index", "--binary",
                        base, head, "--").stdout

    def patch_id(self, base: str, head: str) -> str | None:
        """patch-id ổn định của thay đổi ``base → head``; diff rỗng → ``None``."""
        diff = self.diff(base, head)
        if not diff.strip():
            return None
        out = self.run("patch-id", "--stable", input=diff).stdout.decode("ascii", "replace").split()
        return out[0] if out and re.match(r"^[0-9a-f]{40}$", out[0]) else None

    def range_diff(self, old_base: str, old_head: str, new_base: str, new_head: str, limit: int = 4000) -> str:
        proc = self.run("range-diff", "--no-color", f"{old_base}..{old_head}", f"{new_base}..{new_head}",
                        ok=(0, 1, 128))
        text = (proc.stdout or proc.stderr).decode("utf-8", "replace")
        return text if len(text) <= limit else text[:limit] + "\n…(cắt)"

    def tags_matching(self, prefix: str) -> list[str]:
        out = self.out("for-each-ref", "--format=%(refname:strip=2)", f"refs/tags/{prefix}*")
        return [t for t in out.splitlines() if t]

    def conflicted_files(self, cwd: Path) -> list[str]:
        out = self.out("diff", "--name-only", "--diff-filter=U", cwd=cwd)
        return [f for f in out.splitlines() if f]

    # --- ghi (chỉ trên clone làm việc của batch) ------------------------------------------

    def fetch(self, remote: str, *refspecs: str, tags: bool = False) -> None:
        """Cập nhật ref THEO DÕI cục bộ từ remote (``+`` trong refspec chỉ ghi đè ref cục bộ
        của clone làm việc; remote là nguồn sự thật, không bị đụng tới). ``tags``: lấy cả tag."""
        self.run("fetch", "--quiet", "--tags" if tags else "--no-tags", remote, *refspecs)

    def worktree_add(self, path: Path, ref: str, *, branch: str | None = None) -> None:
        args = ["worktree", "add", "--quiet"]
        if branch:
            args += ["-b", check_ref_name(branch)]
        else:
            args += ["--detach"]
        self.run(*args, str(path), ref)

    def worktree_remove(self, path: Path) -> None:
        self.run("worktree", "remove", "--force", str(path), ok=(0, 128))
        self.run("worktree", "prune", ok=(0, 128))

    def delete_local_branch(self, name: str) -> None:
        self.run("branch", "-D", check_ref_name(name), ok=(0, 1))

    def push(self, remote: str, refspec: str, *, token: str | None = None, cwd: Path | None = None) -> None:
        """Đẩy MỘT refspec, không bao giờ force (remote cấm non-fast-forward thì push lỗi)."""
        if refspec.startswith("+") or "--force" in refspec or refspec.startswith("-"):
            raise ValueError(f"cấm force-push: {refspec!r}")
        src, _, dst = refspec.partition(":")
        if not dst.startswith("refs/heads/") or dst in ("refs/heads/main", "refs/heads/master"):
            raise ValueError(f"batch chỉ đẩy nhánh của chính nó, không đẩy {dst!r}")
        env = {}
        if token:
            cred = base64.b64encode(f"x-access-token:{token}".encode("utf-8")).decode("ascii")
            env = {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "http.extraheader",
                   "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {cred}", "GIT_TERMINAL_PROMPT": "0"}
        self.run("push", "--quiet", remote, f"{src}:{dst}", cwd=cwd, env=env)
