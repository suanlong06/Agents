"""Nạp và kiểm JSON Schema trong ``schemas/`` (PR-1a).

- Mỗi schema có version; code đọc được dữ liệu của version kế tiếp (N+1) để code
  bản trước không crash khi đọc state bản sau, ví dụ sau một lần revert (R2).
  Trường lạ được bỏ qua khi đọc và giữ nguyên khi ghi lại.
- ``$ref`` giữa các schema (state → task) phân giải qua registry theo ``$id``.
"""

from __future__ import annotations

import copy
import json
from functools import lru_cache
from pathlib import Path

from jsonschema import Draft202012Validator
from jsonschema.exceptions import best_match
from referencing import Registry, Resource

SCHEMA_DIR = Path(__file__).resolve().parent.parent / "schemas"
NAMES = ("task", "event", "state", "ledger", "audit")

# Version mà code này ghi ra.
CURRENT_VERSION = {name: 1 for name in NAMES}
# Đọc được tới N+1: trường lạ bị bỏ qua, trạng thái lạ chỉ đọc.
READ_AHEAD = 1


class SchemaError(ValueError):
    """Dữ liệu sai schema hoặc version nằm ngoài cửa sổ đọc được."""


def _path(name: str) -> Path:
    if name not in NAMES:
        raise KeyError(f"schema không tồn tại: {name!r}")
    return SCHEMA_DIR / f"{name}.schema.json"


@lru_cache(maxsize=None)
def _raw(name: str) -> dict:
    with open(_path(name), encoding="utf-8") as f:
        return json.load(f)


def load_schema(name: str) -> dict:
    """Bản sao của schema ``name`` (sửa bản sao không ảnh hưởng cache)."""
    return copy.deepcopy(_raw(name))


@lru_cache(maxsize=None)
def _registry() -> Registry:
    resources = [(_raw(n)["$id"], Resource.from_contents(_raw(n))) for n in NAMES]
    return Registry().with_resources(resources)


@lru_cache(maxsize=None)
def _validator(name: str) -> Draft202012Validator:
    return Draft202012Validator(_raw(name), registry=_registry())


def validate(name: str, obj: object) -> None:
    """Ném ``SchemaError`` nếu ``obj`` không khớp schema ``name``."""
    err = best_match(_validator(name).iter_errors(obj))
    if err is not None:
        where = "/".join(str(p) for p in err.absolute_path) or "<gốc>"
        raise SchemaError(f"{name}: {where}: {err.message}")


def check_version(name: str, version: object) -> int:
    """Ném ``SchemaError`` nếu ``version`` không đọc được; trả về version."""
    if isinstance(version, bool) or not isinstance(version, int):
        raise SchemaError(f"{name}: version phải là số nguyên, nhận {version!r}")
    current = CURRENT_VERSION[name]
    if not 1 <= version <= current + READ_AHEAD:
        raise SchemaError(
            f"{name}: version {version} nằm ngoài cửa sổ đọc được 1..{current + READ_AHEAD}"
        )
    return version
