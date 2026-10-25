# View 4 — Agent status

Snapshot: `views.agents`. Dữ liệu đến Muse VM qua snapshot ([handoff](00_snapshot_handoff.md)).

## Nguồn

`heartbeats.json`: bản tổng hợp do orchestrator publish (PR-1b, `HeartbeatMonitor.publish`),
kiểm theo `schemas/heartbeat.schema.json`. Dashboard không tự quét heartbeat từng agent.

## Hiển thị

- Health của orchestrator (`ok` / `error` kèm lỗi cuối).
- Mỗi agent: trạng thái (`ok`, `error`, `stopped`, `stale`, `invalid`), thời điểm heartbeat,
  `age_s`, PID, task, model, lý do hoặc lỗi.
- `age_s` và `stale` được tính **lúc orchestrator publish** (`published_at`), không tính lại
  theo giờ hiện tại trên VM. Snapshot cũ thì số liệu cũ, và trang ghi rõ lúc publish.
- `timeout_s` là mặc định tạm trong config (R16).

## Provenance

Nguồn `heartbeats.json`; số bản ghi = số agent; `as_of` = lần fold cuối của `state.json` (giống
mọi view). Không có `heartbeats.json` → "không có dữ liệu" (không biết orchestrator có khỏe
không). Có file mà danh sách agent rỗng → `0`.

## Chỉ đọc

Không có nút dừng/khởi động agent.
