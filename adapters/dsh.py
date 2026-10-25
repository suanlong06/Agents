"""Adapter DeepSeek harness ``dsh`` qua 9Router (PR-2): tuyến "Trung bình".

- KHÔNG có fallback (R11): ``dsh`` hay 9Router sập thì router đưa task vào
  ``blocked`` và thông báo, không chuyển sang model khác.
- Model THỰC SỰ phục vụ lấy từ log 9Router join theo request id
  (``model_id_source = router_log``). Không join được thì model là không rõ.
- ``dsh`` báo usage CỘNG DỒN theo phiên (mặc định ``usage: cumulative`` trong
  ``routing.yaml``): đổi ra delta trước khi ghi ledger (R5). Usage trong log
  9Router là theo từng request nên được ưu tiên.
- Probe: completion thật gửi thẳng tới 9Router ``:20128``, timeout riêng.
"""

from __future__ import annotations

from .transport import RouterCliAdapter

DEFAULT_COMMAND = ("dsh", "--profile", "headless")


class DshAdapter(RouterCliAdapter):
    name = "dsh"
    vendors = frozenset({"deepseek"})

    def __init__(self, *, command=DEFAULT_COMMAND, usage_mode: str = "cumulative", **kw):
        super().__init__(command=command, usage_mode=usage_mode, **kw)
