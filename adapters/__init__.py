"""Adapter cho các harness agent có sẵn (PR-2, R5/R11).

Module:
- ``base``: interface chung (``run``, ``status``, ``cost``), lỗi ``NotAvailable``
  (terminal) và ``ServiceUnavailable``, bảng alias ``(model_id, vendor)``, circuit
  breaker, đổi usage cộng dồn ra delta, sink ghi ledger. KHÔNG import socket,
  subprocess hay http để stub Codex/Cursor dựa vào nó mà vẫn sạch.
- ``transport``: chạy CLI có timeout, probe HTTP bằng completion thật, join log
  9Router theo request id; lớp nền cho adapter CLI.
- ``claude``, ``dsh``, ``antigravity``: adapter thật. ``muse_monitor``: kênh thông báo.
- ``codex_stub``, ``cursor_stub``: STUB, mọi lời gọi ném ``NotAvailable``. Không bao
  giờ được gọi và không có trong ``config/routing.yaml``.
- ``router``: nạp/validate ``routing.yaml`` theo allowlist, chọn adapter theo độ khó,
  fallback chỉ trong chuỗi Opus.
"""
