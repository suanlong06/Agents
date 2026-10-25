"""Adapter Antigravity ``agy`` qua bridge ``:20129`` → 9Router ``:20128`` (PR-2): tuyến "Dễ".

- KHÔNG có fallback (R11): bridge hay 9Router sập thì task ``blocked`` và thông báo.
- Model THỰC SỰ phục vụ lấy từ log 9Router join theo request id.
- ``agy`` không báo token (``tokens_reported: false``): token lấy từ log 9Router nếu
  có, không thì ước lượng theo độ dài và ghi ``estimated = true`` (R5).
- Probe: completion thật gửi tới bridge ``:20129``, timeout riêng.
"""

from __future__ import annotations

from .transport import RouterCliAdapter

DEFAULT_COMMAND = ("agy", "-p", "--output-format", "json", "--model", "{model}")


class AntigravityAdapter(RouterCliAdapter):
    name = "agy"
    vendors = frozenset({"google"})

    def __init__(self, *, command=DEFAULT_COMMAND, tokens_reported: bool = False, **kw):
        super().__init__(command=command, tokens_reported=tokens_reported, **kw)
