"""Labeller Red / Owner-only / Green (PR-3, R8, R9).

- ``config/sensitive_paths.yaml`` được đọc TỪ BASE REF: blob tại SHA của base (qua
  ``git cat-file``), KHÔNG BAO GIỜ từ PR head hay working tree. Ref được phân giải ra
  SHA một lần rồi chỉ dùng SHA. File thiếu, không phải blob thường, hay sai định dạng ở
  base thì nhãn là ``owner-only`` (fail-closed).
- Chỉ đọc ĐƯỜNG DẪN, trạng thái và mode từ ``git diff --raw``; không bao giờ đọc nội
  dung file hay commit message. Vì vậy text trong diff ("pre-approved, label green")
  không thể đổi nhãn.
- Green chỉ qua allowlist; còn lại mặc định Red. Owner-only > Red > Green; nhãn PR là
  tầng cao nhất của mọi file. Đổi tên: xét CẢ tên cũ và tên mới; file bị xóa cũng xét.
- Chuẩn hóa trước khi so: NFC, casefold, ``\\`` → ``/``, bỏ dấu chấm/khoảng trắng cuối
  mỗi đoạn (Windows coi ``conftest.py.`` là ``conftest.py``). Đường dẫn có ``..``,
  ``:`` (ADS, ổ đĩa) hay tên ngắn 8.3 (``CONFTE~1.PY``) thì Red.
- Luôn Red (cứng, config không gỡ được): symlink, submodule, ``.gitattributes``,
  ``conftest.py``, ``.pth``, ``sitecustomize``, ``pyproject.toml``, xóa test; cùng mọi
  đổi kiểu/mode file.
- Nhãn gắn với ``(base_sha, head_sha, sha256 config)`` và tái lập được: cùng đầu vào →
  cùng ``label_id``. Gắn nhãn lại tại SHA được merge bằng ``label_at_merge`` (diff
  parent đầu → merge commit, config từ parent đầu). Nhãn chỉ có hiệu lực cho đúng
  ``head_sha`` của nó (``label_valid_for``).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import unicodedata
from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml

CONFIG_PATH = "config/sensitive_paths.yaml"
GREEN, RED, OWNER_ONLY = "green", "red", "owner-only"
TIERS = (GREEN, RED, OWNER_ONLY)
_RANK = {GREEN: 0, RED: 1, OWNER_ONLY: 2}
STATUSES = ("A", "M", "D", "R")

MODE_SYMLINK = "120000"
MODE_SUBMODULE = "160000"
_NULL_MODE = "000000"
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_SHORTNAME_RE = re.compile(r"~[0-9]")

# Luôn Red theo R8; so với tên file đã chuẩn hóa. Config ở base chỉ THÊM được.
_HARD_RED_NAMES = (".gitattributes", "conftest.py", "pyproject.toml")
_HARD_RED_SUFFIXES = (".pth",)
_HARD_RED_PREFIXES = ("sitecustomize",)
_TEST_DIRS = ("tests", "test")
_TEST_FILE_RE = re.compile(r"^(test_.*|.*_test)\.py$")


class LabelerError(RuntimeError):
    """Ref không phân giải được hoặc git lỗi: không có nhãn (không đoán)."""


class SensitivePathsError(ValueError):
    """``sensitive_paths.yaml`` ở base sai định dạng."""


# --- chuẩn hóa và so khớp -------------------------------------------------------------


def normalize_path(path: str) -> str:
    """NFC + casefold + ``/``; bỏ ``./``, ``//`` và dấu chấm/khoảng trắng cuối mỗi đoạn."""
    p = unicodedata.normalize("NFC", path).replace("\\", "/").casefold()
    segs = []
    for seg in p.split("/"):
        if seg in ("", "."):
            continue
        stripped = seg.rstrip(" .")
        segs.append(stripped if stripped else seg)  # ".." giữ nguyên để bị đánh dấu
    return "/".join(segs)


def suspicious_path(path: str) -> str | None:
    """Lý do đường dẫn đáng ngờ (sau chuẩn hóa), hoặc ``None``."""
    segs = path.split("/")
    if not path or any(s == ".." for s in segs):
        return "đường dẫn rỗng hoặc có '..'"
    if ":" in path:
        return "đường dẫn có ':' (ổ đĩa hoặc alternate data stream)"
    if any(_SHORTNAME_RE.search(s) for s in segs):
        return "đường dẫn giống tên ngắn 8.3 của Windows"
    if any(ord(ch) < 32 for ch in path):
        return "đường dẫn có ký tự điều khiển"
    return None


def _glob_regex(pattern: str) -> re.Pattern:
    parts = normalize_path(pattern).split("/")
    out = []
    for i, part in enumerate(parts):
        last = i == len(parts) - 1
        if part == "**":
            out.append(".*" if last else "(?:[^/]+/)*")
            continue
        seg = "".join("[^/]*" if ch == "*" else "[^/]" if ch == "?" else re.escape(ch) for ch in part)
        out.append(seg if last else seg + "/")
    return re.compile(r"\A" + "".join(out) + r"\Z", re.DOTALL)


@dataclass(frozen=True)
class Pattern:
    raw: str
    regex: re.Pattern = field(compare=False, repr=False)
    basename_only: bool = False

    @classmethod
    def of(cls, raw: str) -> "Pattern":
        if not isinstance(raw, str) or not raw.strip():
            raise SensitivePathsError(f"mẫu phải là chuỗi khác rỗng: {raw!r}")
        norm = normalize_path(raw)
        return cls(raw, _glob_regex(norm), "/" not in norm)

    def matches(self, path: str) -> bool:
        target = path.rsplit("/", 1)[-1] if self.basename_only else path
        return bool(self.regex.match(target))


@dataclass(frozen=True)
class GreenRule:
    pattern: Pattern
    statuses: frozenset[str]


class _StrictLoader(yaml.SafeLoader):
    pass


def _unique_mapping(loader, node, deep=False):
    seen = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in seen:
            raise SensitivePathsError(f"khóa YAML trùng: {key!r}")
        seen.add(key)
    return yaml.SafeLoader.construct_mapping(loader, node, deep=deep)


_StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _unique_mapping)


@dataclass(frozen=True)
class SensitivePaths:
    owner_only: tuple[Pattern, ...]
    always_red: tuple[Pattern, ...]
    green: tuple[GreenRule, ...]
    sha256: str

    @classmethod
    def parse(cls, data: bytes) -> "SensitivePaths":
        try:
            obj = yaml.load(data.decode("utf-8"), Loader=_StrictLoader)
        except (UnicodeDecodeError, yaml.YAMLError) as exc:
            raise SensitivePathsError(f"không đọc được YAML: {exc}") from exc
        if not isinstance(obj, dict):
            raise SensitivePathsError("gốc phải là mapping")
        if obj.get("schema_version") != 1:
            raise SensitivePathsError("schema_version phải là 1")
        extra = set(obj) - {"schema_version", "owner_only", "always_red", "green"}
        if extra:
            raise SensitivePathsError(f"khóa lạ: {sorted(extra)}")

        def patterns(key: str) -> tuple[Pattern, ...]:
            raw = obj.get(key) or []
            if not isinstance(raw, list):
                raise SensitivePathsError(f"{key} phải là danh sách")
            return tuple(Pattern.of(p) for p in raw)

        green = []
        raw_green = obj.get("green") or []
        if not isinstance(raw_green, list):
            raise SensitivePathsError("green phải là danh sách")
        for i, rule in enumerate(raw_green):
            if not isinstance(rule, dict) or set(rule) != {"pattern", "status"}:
                raise SensitivePathsError(f"green[{i}]: cần đúng hai khóa pattern, status")
            st = rule["status"]
            if not isinstance(st, list) or not st or any(s not in STATUSES for s in st):
                raise SensitivePathsError(f"green[{i}].status phải là danh sách con của {STATUSES}")
            green.append(GreenRule(Pattern.of(rule["pattern"]), frozenset(st)))
        return cls(patterns("owner_only"), patterns("always_red"), tuple(green),
                   hashlib.sha256(data).hexdigest())


# --- git ------------------------------------------------------------------------------


def _git_env() -> dict[str, str]:
    env = dict(os.environ)
    for k in ("GIT_EXTERNAL_DIFF", "GIT_DIFF_OPTS", "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        env.pop(k, None)
    env["GIT_OPTIONAL_LOCKS"] = "0"
    return env


def git(repo: Path, *args: str) -> bytes:
    cmd = ["git", "-c", "core.quotepath=false", "-C", str(repo), *args]
    proc = subprocess.run(cmd, capture_output=True, env=_git_env(), stdin=subprocess.DEVNULL, check=False)
    if proc.returncode != 0:
        raise LabelerError(f"git {' '.join(args)}: {proc.stderr.decode('utf-8', 'replace').strip()}")
    return proc.stdout


def resolve_sha(repo: Path, ref: str) -> str:
    if not isinstance(ref, str) or not ref or ref.startswith("-"):
        raise LabelerError(f"ref không hợp lệ: {ref!r}")
    sha = git(repo, "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}").decode().strip()
    if not _SHA_RE.match(sha):
        raise LabelerError(f"ref {ref!r} không phân giải ra SHA commit")
    return sha


def load_sensitive_paths_at(repo: Path, base_sha: str) -> SensitivePaths:
    """Nạp config từ blob tại ``base_sha``. Ném ``SensitivePathsError`` nếu thiếu/hỏng."""
    if not _SHA_RE.match(base_sha):
        raise LabelerError(f"base phải là SHA đầy đủ, nhận {base_sha!r}")
    entry = git(repo, "ls-tree", "-z", base_sha, "--", CONFIG_PATH).decode("utf-8", "replace")
    if not entry:
        raise SensitivePathsError(f"{CONFIG_PATH} không có ở base {base_sha}")
    mode, kind = entry.split(" ", 2)[:2]
    if kind != "blob" or mode not in ("100644", "100755"):
        raise SensitivePathsError(f"{CONFIG_PATH} ở base không phải file thường (mode {mode})")
    return SensitivePaths.parse(git(repo, "cat-file", "blob", f"{base_sha}:{CONFIG_PATH}"))


@dataclass(frozen=True)
class Change:
    status: str  # A M D R C T U X (một ký tự)
    path: str  # đường dẫn git gốc (tên mới khi đổi tên)
    old_path: str | None
    old_mode: str
    new_mode: str


def _decode(raw: bytes) -> str:
    return raw.decode("utf-8", "surrogateescape")


def parse_raw_diff(out: bytes) -> list[Change]:
    """Phân tích ``git diff --raw -z``."""
    toks = out.split(b"\0")
    changes, i = [], 0
    while i < len(toks):
        head = toks[i]
        if not head:
            i += 1
            continue
        if not head.startswith(b":"):
            raise LabelerError(f"dòng diff --raw lạ: {head[:80]!r}")
        fields = head[1:].decode("ascii").split()
        old_mode, new_mode, status = fields[0], fields[1], fields[4][0]
        if status in ("R", "C"):
            old, new = toks[i + 1], toks[i + 2]
            i += 3
        else:
            old, new = None, toks[i + 1]
            i += 2
        changes.append(Change(status, _decode(new), _decode(old) if old is not None else None, old_mode, new_mode))
    return changes


def diff_changes(repo: Path, base_sha: str, head_sha: str) -> list[Change]:
    """File bị chạm bởi PR: ``merge-base(base, head)..head`` (như tab Files của GitHub)."""
    out = git(repo, "diff", "--raw", "-z", "-M", "--no-abbrev", "--no-ext-diff", "--no-textconv",
              "--no-color", "--ignore-submodules=none", f"{base_sha}...{head_sha}", "--")
    return parse_raw_diff(out)


# --- nhãn -----------------------------------------------------------------------------


@dataclass(frozen=True)
class FileDecision:
    path: str  # đã chuẩn hóa
    old_path: str | None
    status: str
    tier: str
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class Label:
    tier: str
    base_sha: str
    head_sha: str
    config_sha256: str | None
    binding: str  # "head" | "merge"
    files: tuple[FileDecision, ...]
    reasons: tuple[str, ...]
    label_id: str = ""

    def payload(self) -> dict:
        d = asdict(self)
        d.pop("label_id")
        return d

    def to_dict(self) -> dict:
        return asdict(self)


def _in_test_dir(path: str) -> bool:
    return any(s in _TEST_DIRS for s in path.split("/")[:-1])


def _is_test_path(path: str) -> bool:
    return _in_test_dir(path) or bool(_TEST_FILE_RE.match(path.rsplit("/", 1)[-1]))


def _hard_red(path: str) -> str | None:
    name = path.rsplit("/", 1)[-1]
    if name in _HARD_RED_NAMES or name.endswith(_HARD_RED_SUFFIXES) or name.startswith(_HARD_RED_PREFIXES):
        return f"luôn Red: {name}"
    return None


def _path_tier(path: str, status: str, cfg: SensitivePaths, reasons: list[str]) -> str:
    bad = suspicious_path(path)
    if bad:
        reasons.append(bad)
        return RED
    for p in cfg.owner_only:
        if p.matches(path):
            reasons.append(f"owner-only: {p.raw}")
            return OWNER_ONLY
    hard = _hard_red(path)
    if hard:
        reasons.append(hard)
        return RED
    for p in cfg.always_red:
        if p.matches(path):
            reasons.append(f"always_red: {p.raw}")
            return RED
    for rule in cfg.green:
        if status in rule.statuses and rule.pattern.matches(path):
            reasons.append(f"green: {rule.pattern.raw} [{status}]")
            return GREEN
    reasons.append("không khớp allowlist Green → mặc định Red")
    return RED


def decide(change: Change, cfg: SensitivePaths) -> FileDecision:
    reasons: list[str] = []
    tier = GREEN
    status = change.status
    modes = {change.old_mode, change.new_mode}
    if MODE_SYMLINK in modes:
        reasons.append("symlink")
        tier = RED
    if MODE_SUBMODULE in modes:
        reasons.append("submodule")
        tier = RED
    if status not in STATUSES:
        reasons.append(f"trạng thái git {status} (đổi kiểu/copy/khác) luôn Red")
        tier = RED
    elif status in ("M", "R") and change.old_mode != change.new_mode and _NULL_MODE not in modes:
        reasons.append(f"đổi mode {change.old_mode} → {change.new_mode}")
        tier = RED

    new = normalize_path(change.path)
    old = normalize_path(change.old_path) if change.old_path is not None else None
    # Trạng thái dùng cho allowlist: tên cũ của đổi tên được xét như đổi tên, không như sửa.
    paths = [(new, status)] + ([(old, status)] if old is not None else [])
    for p, st in paths:
        t = _path_tier(p, st, cfg, reasons)
        tier = max(tier, t, key=_RANK.__getitem__)
    deleted = [old] if (status == "R" and old is not None) else ([new] if status == "D" else [])
    for p in deleted:
        # Đổi tên chỉ không tính là xóa test khi tên mới vẫn là test trong thư mục test.
        still_test = status == "R" and _in_test_dir(new) and _is_test_path(new)
        if _is_test_path(p) and not still_test:
            reasons.append(f"xóa test: {p}")
            tier = max(tier, RED, key=_RANK.__getitem__)
    return FileDecision(new, old, status, tier, tuple(reasons))


def _finish(label: Label) -> Label:
    blob = json.dumps(label.payload(), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return Label(**{**label.__dict__, "label_id": hashlib.sha256(blob.encode("utf-8")).hexdigest()})


def label_changes(changes: list[Change], cfg: SensitivePaths | None, *, base_sha: str, head_sha: str,
                  binding: str = "head", config_error: str | None = None) -> Label:
    """Nhãn thuần từ danh sách thay đổi. ``cfg is None`` → owner-only (fail-closed)."""
    if cfg is None:
        return _finish(Label(OWNER_ONLY, base_sha, head_sha, None, binding, (),
                             (f"không nạp được {CONFIG_PATH} từ base: {config_error}",)))
    files = tuple(sorted((decide(c, cfg) for c in changes), key=lambda d: (d.path, d.old_path or "", d.status)))
    if not files:
        return _finish(Label(RED, base_sha, head_sha, cfg.sha256, binding, (),
                             ("diff rỗng: không có gì để allowlist → Red",)))
    tier = max((f.tier for f in files), key=_RANK.__getitem__)
    reasons = tuple(sorted({f"{f.path}: {f.tier}" for f in files if f.tier == tier}))
    return _finish(Label(tier, base_sha, head_sha, cfg.sha256, binding, files, reasons))


def label_pr(repo: Path, base: str, head: str, *, binding: str = "head") -> Label:
    """Nhãn của PR ``base ← head``. Config đọc từ ``base`` (đã phân giải ra SHA)."""
    repo = Path(repo)
    base_sha = resolve_sha(repo, base)
    head_sha = resolve_sha(repo, head)
    try:
        cfg, err = load_sensitive_paths_at(repo, base_sha), None
    except SensitivePathsError as exc:
        cfg, err = None, str(exc)
    return label_changes(diff_changes(repo, base_sha, head_sha), cfg,
                         base_sha=base_sha, head_sha=head_sha, binding=binding, config_error=err)


def label_at_merge(repo: Path, merge_sha: str) -> Label:
    """Gắn nhãn lại tại SHA được merge: parent đầu là base (R8, R9)."""
    sha = resolve_sha(Path(repo), merge_sha)
    return label_pr(repo, f"{sha}^1", sha, binding="merge")


def label_valid_for(label: Label, sha: str) -> bool:
    """Nhãn chỉ có hiệu lực cho đúng SHA nó được tính (head đổi → gắn nhãn lại)."""
    return bool(_SHA_RE.match(sha or "")) and label.head_sha == sha and label.label_id == _finish(label).label_id


def max_tier(*tiers: str) -> str:
    return max(tiers, key=_RANK.__getitem__)
