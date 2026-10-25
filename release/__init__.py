"""Release thủ công của Harness Agents (PR-4a). Toàn bộ ``release/**`` là Owner-only (R8).

Module:
- ``shadow_eval``: đánh giá 4 điều kiện auto-merge ỨNG VIÊN (SPEC mục 8, OPEN DISCUSSION)
  và CHỈ ghi bản ghi ``would-merge`` vào audit. Không có đường gọi merge nào.
- ``manifest``: manifest release từ ``git log prevTag..newTag`` (R10) và kế hoạch revert
  theo thứ tự ngược, không force-push.
- ``readonly_check``: kiểm tra read-only ở bước 11 (SPEC mục 8 câu 14): bộ kiểm "hành
  động không ghi gì" + smoke / health / so lỗi trong audit sau release.

Trong MVP không có merge tự động, không có revert tự động (PR-4b): release, revert và
incident làm tay theo ``docs/runbooks/``.
"""
