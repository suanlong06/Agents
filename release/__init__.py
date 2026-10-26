"""Release của Harness Agents (PR-4a thủ công, PR-4b tự động). Toàn bộ ``release/**`` là Owner-only (R8).

PR-4a:
- ``shadow_eval``: đánh giá 4 điều kiện auto-merge ỨNG VIÊN (SPEC mục 8, OPEN DISCUSSION)
  và CHỈ ghi bản ghi ``would-merge`` vào audit. Không có đường gọi merge nào.
- ``manifest``: manifest release từ ``git log prevTag..newTag`` (R10) và kế hoạch revert
  theo thứ tự ngược, không force-push.
- ``readonly_check``: kiểm tra read-only ở bước 11 (SPEC mục 8 câu 14): bộ kiểm "hành
  động không ghi gì" + smoke / health / so lỗi trong audit sau release; ``--expect-tag`` ghim
  lần kiểm vào đúng tag release (F2).

PR-4b (``docs/runbooks/batch.md``; CHƯA KÍCH HOẠT, ``scheduler.enabled: false``):
- ``schedule``: lịch KST (digest 11:30 / 16:30, hạn duyệt, batch 12:00 / 17:00), lỡ cửa sổ thì bỏ qua.
- ``digest``: đóng băng danh sách PR của cửa sổ (head SHA, patch-id, nhãn, CI).
- ``approval_binding``: phê duyệt của NGƯỜI DÙNG trên GitHub gắn ``(head SHA, patch-id)``.
- ``github_write``: client ghi hẹp; ``merge_pull`` là đường merge duy nhất và đòi phê duyệt.
- ``batch``: tích hợp tuần tự + CI trên kết quả tích hợp + merge tuần tự + tag + kiểm tra.
- ``release_check``: chạy kiểm tra read-only của CHÍNH tag release trong worktree detached.
- ``auto_revert``, ``incidents``: revert tự động theo R10 (mở PR revert, không merge), incident.
- ``runner``: kill-switch trước mọi hành động, chạy job đến hạn.

KHÔNG có auto-merge ở bất kỳ dạng nào: batch chỉ merge PR có phê duyệt của người dùng. Runner
chạy dưới OS user hiện tại; tách OS user + ACL thư mục state là điều kiện triển khai còn mở (R15).
"""
