# Runbook — Incident (PR-4a, R10)

> MVP: incident mở và cập nhật **tay** bởi người dùng. Schema: `schemas/incident.schema.json`.
> PR-4b: batch release tự động (khi được kích hoạt) cũng mở incident với `opened_by: "runner"`
> (`release/incidents.py`). Incident `open`/`frozen` — hoặc file incident không đọc được — chặn mọi
> batch. Chỉ người dùng chuyển incident sang `mitigated`/`resolved`.
> Bản ghi nằm trong state (ngoài repo, R13), **best-effort, không chống giả mạo** (R15).

## Khi nào mở

- Kiểm tra read-only sau release không đạt (`trigger: readonly_check_failed`).
- Có bug do release (`bug_report`).
- `git revert` conflict khi làm `docs/runbooks/revert.md` (`revert_conflict`).
- Kill-switch không đọc được mà không rõ vì sao (`killswitch_unreadable`) — có thể là dấu hiệu
  bị ghi đè (R15).

## Các bước

1. **Bật kill-switch** (`docs/runbooks/release.md`). Incident `open`/`frozen` thì kill-switch
   luôn bật.
2. **Mở GitHub issue** nhãn `incident`, tiêu đề `INC-YYYYMMDD-NN: <tóm tắt>`. Thảo luận diễn ra
   ở issue; GitHub là nơi người dùng xác nhận.
3. **Tạo bản ghi** `%LOCALAPPDATA%\harness\incidents\INC-YYYYMMDD-NN.json` theo mẫu dưới đây
   (UTF-8). `incident_id` theo ngày KST, `NN` tăng dần trong ngày.
4. **Làm revert** nếu cần (`docs/runbooks/revert.md`); cập nhật `revert` và `timeline` sau mỗi
   bước.
5. **Đóng:** sau khi release lại và kiểm tra read-only đạt → `status: resolved`, thêm dòng
   `timeline`, người dùng tắt kill-switch, đóng issue.

## Trạng thái

| `status` | Nghĩa |
|---|---|
| `open` | Đang xử lý, kill-switch bật |
| `frozen` | Revert conflict hoặc chưa rõ nguyên nhân: không làm gì thêm cho tới khi người dùng quyết |
| `mitigated` | PR revert đã merge, chờ release lại và kiểm tra read-only |
| `resolved` | Kiểm tra read-only của release mới đạt, kill-switch đã tắt |

## Mẫu bản ghi

```json
{
  "v": 1,
  "incident_id": "INC-20261026-01",
  "opened_at": "2026-10-26T12:20:00+09:00",
  "opened_by": "user",
  "status": "open",
  "severity": "sev2",
  "trigger": "readonly_check_failed",
  "summary": "log_errors: 3 bản ghi audit rejected sau release rel-20261026-1200",
  "github_issue": 42,
  "release": {
    "prev_tag": "rel-20261025-1700",
    "new_tag": "rel-20261026-1200",
    "manifest_id": "0000000000000000000000000000000000000000000000000000000000000000"
  },
  "readonly_check": null,
  "killswitch_engaged": true,
  "revert": {
    "status": "not_started",
    "branch": null,
    "pr": null,
    "reverted": [],
    "conflict": null
  },
  "timeline": [
    {"ts": "2026-10-26T12:20:00+09:00", "actor": "user", "note": "kiểm tra read-only không đạt; kill-switch bật"}
  ]
}
```

`manifest_id` lấy từ manifest của release lỗi (`python -m release.manifest ...`). `readonly_check`
là báo cáo JSON của `python -m release.readonly_check run` nếu có. Mẫu này được test kiểm theo
schema (`tests/test_runbooks.py`).
