"""Manifest release từ ``git log prevTag..newTag`` (PR-4a, R10).

- Release = một **tag** trên ``main`` (xem ``docs/runbooks/release.md``). Manifest lấy từ
  git, KHÔNG BAO GIỜ từ danh sách tự ghi: ``git log --first-parent --reverse
  prevSHA..newSHA``. Mỗi commit first-parent trên ``main`` là MỘT PR (merge commit của
  GitHub hoặc squash) hay một commit thẳng; commit trong nhánh của PR đi theo merge commit
  của nó nên revert từng PR = revert đúng commit first-parent đó.
- Tag được phân giải MỘT lần ra SHA (``refs/tags/<tên>``) rồi chỉ dùng SHA; ``prevTag``
  phải là tổ tiên của ``newTag``. Thứ tự manifest: cũ → mới.
- ``revert_plan``: lệnh ``git revert`` từng PR theo thứ tự NGƯỢC manifest (mới → cũ), merge
  commit dùng ``-m 1``. Không có ``reset``, không có ``--force``: revert chỉ THÊM commit,
  nên commit merge sau release vẫn được giữ và đẩy lên không cần force-push. Áp dụng TAY
  theo ``docs/runbooks/revert.md`` trên một nhánh mới, qua PR. Conflict thì dừng
  (``git revert --abort``), đóng băng (kill-switch bật), báo động và mở incident; không tự
  giải conflict. Revert tự động là PR-4b.
- Chỉ đọc git: module chỉ chạy ``rev-parse``, ``merge-base --is-ancestor`` và ``log``.

CLI::

    python -m release.manifest v1 v2 [--repo .]            # in manifest JSON
    python -m release.manifest v1 v2 --revert-plan         # in lệnh revert, mới → cũ
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from pipeline import common

MANIFEST_VERSION = 1
SOURCE = "git log --first-parent --reverse prevTag..newTag"
MERGE_RE = re.compile(r"^Merge pull request #(\d+) from \S+")
SQUASH_RE = re.compile(r"\(#(\d+)\)\s*$")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_TAG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")
_FS, _RS = "\x1f", "\x1e"


class ManifestError(RuntimeError):
    pass


def _git_env() -> dict[str, str]:
    env = dict(os.environ)
    for k in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_EXTERNAL_DIFF"):
        env.pop(k, None)
    env["GIT_OPTIONAL_LOCKS"] = "0"  # lệnh đọc không ghi index
    return env


def _git(repo: Path, *args: str, ok_codes: tuple[int, ...] = (0,)) -> subprocess.CompletedProcess:
    proc = subprocess.run(["git", "-c", "core.quotepath=false", "-c", "log.showSignature=false",
                           "-C", str(repo), *args], capture_output=True, env=_git_env(),
                          stdin=subprocess.DEVNULL, check=False)
    if proc.returncode not in ok_codes:
        raise ManifestError(f"git {' '.join(args)}: {proc.stderr.decode('utf-8', 'replace').strip()}")
    return proc


def resolve_tag(repo: Path, tag: str) -> str:
    """SHA commit của ``refs/tags/<tag>``. Không nhận nhánh hay SHA: release là tag."""
    if not isinstance(tag, str) or not _TAG_RE.match(tag) or ".." in tag:
        raise ManifestError(f"tên tag không hợp lệ: {tag!r}")
    out = _git(repo, "rev-parse", "--verify", "--quiet", "--end-of-options",
               f"refs/tags/{tag}^{{commit}}", ok_codes=(0, 1))
    sha = out.stdout.decode().strip()
    if out.returncode != 0 or not _SHA_RE.match(sha):
        raise ManifestError(f"tag {tag!r} không tồn tại hoặc không trỏ tới commit")
    return sha


def _entry(seq: int, raw: str) -> dict:
    sha, parents, author, authored_at, subject = raw.split(_FS, 4)
    parents_l = parents.split()
    m = MERGE_RE.match(subject)
    if len(parents_l) > 1:
        kind, pr = "merge", int(m.group(1)) if m else None
    else:
        s = SQUASH_RE.search(subject)
        kind, pr = ("squash", int(s.group(1))) if s else ("direct", None)
    return {"seq": seq, "sha": sha, "parents": parents_l, "kind": kind, "pr": pr,
            "subject": subject, "author": author, "authored_at": authored_at}


def manifest_id(prev_sha: str, new_sha: str, entries: list[dict]) -> str:
    blob = json.dumps({"prev_sha": prev_sha, "new_sha": new_sha, "shas": [e["sha"] for e in entries]},
                      sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def compute_manifest(repo: Path | str, prev_tag: str, new_tag: str, *,
                     clock: Callable[[], str] | None = None, timezone: str = "Asia/Seoul") -> dict:
    """Manifest của release ``new_tag`` so với release trước ``prev_tag``."""
    repo = Path(repo)
    prev_sha, new_sha = resolve_tag(repo, prev_tag), resolve_tag(repo, new_tag)
    if prev_sha == new_sha:
        raise ManifestError(f"{prev_tag} và {new_tag} cùng trỏ tới {prev_sha}: release rỗng")
    anc = _git(repo, "merge-base", "--is-ancestor", prev_sha, new_sha, ok_codes=(0, 1))
    if anc.returncode != 0:
        raise ManifestError(f"{prev_tag} không phải tổ tiên của {new_tag}: lịch sử main đã bị viết lại?")
    out = _git(repo, "log", "--first-parent", "--reverse", "--no-color",
               f"--format=%H{_FS}%P{_FS}%an{_FS}%aI{_FS}%s{_RS}", f"{prev_sha}..{new_sha}").stdout
    raws = [r.strip("\n") for r in out.decode("utf-8", "replace").split(_RS) if r.strip("\n")]
    entries = [_entry(i, r) for i, r in enumerate(raws, start=1)]
    tz = ZoneInfo(timezone)
    now = clock() if clock else datetime.now(tz).isoformat(timespec="milliseconds")
    manifest = {
        "v": MANIFEST_VERSION, "source": SOURCE, "generated_at": now,
        "prev_tag": prev_tag, "new_tag": new_tag, "prev_sha": prev_sha, "new_sha": new_sha,
        "manifest_id": manifest_id(prev_sha, new_sha, entries), "entries": entries,
    }
    common.validate("manifest", manifest)
    return manifest


def revert_plan(manifest: dict) -> list[dict]:
    """Lệnh ``git revert`` từng PR, thứ tự NGƯỢC manifest. Không reset, không force."""
    common.validate("manifest", manifest)
    steps = []
    for e in reversed(manifest["entries"]):
        argv = ["git", "revert", "--no-edit"] + (["-m", "1"] if e["kind"] == "merge" else []) + [e["sha"]]
        steps.append({"seq": e["seq"], "sha": e["sha"], "pr": e["pr"], "kind": e["kind"], "argv": argv})
    return steps


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m release.manifest", description=__doc__.splitlines()[0])
    ap.add_argument("prev_tag")
    ap.add_argument("new_tag")
    ap.add_argument("--repo", default=".")
    ap.add_argument("--revert-plan", action="store_true", help="in lệnh git revert theo thứ tự ngược")
    args = ap.parse_args(argv)
    try:
        m = compute_manifest(args.repo, args.prev_tag, args.new_tag)
    except ManifestError as exc:
        print(f"lỗi: {exc}", file=sys.stderr)
        return 2
    if args.revert_plan:
        for step in revert_plan(m):
            print(" ".join(step["argv"]) + f"    # PR #{step['pr']}" if step["pr"] else " ".join(step["argv"]))
    else:
        print(json.dumps(m, ensure_ascii=True, indent=2))  # ASCII: an toàn với stdout cp1252
    return 0


if __name__ == "__main__":
    sys.exit(main())
