# View 5 — Logs

Snapshot: `views.logs`. Dữ liệu đến Muse VM qua snapshot ([handoff](00_snapshot_handoff.md)).

## Nguồn

- **Audit log** `audit.jsonl` (PR-1a): bản ghi có `seq` nguyên, như `AuditLog.read`. Snapshot
  giữ 200 bản ghi cuối (mặc định tạm, R16) và tổng số. Dòng `error`/`rejected` được tô đỏ.
- **Log theo agent**: event trong inbox của từng agent (`inbox/<agent>/events-*.jsonl` và các
  nguồn trong `state.json.offsets`), nhóm theo `agent`, mỗi agent 100 event cuối. Mỗi event ghi
  "đã fold" hoặc "chưa fold" (ghi sau lần fold cuối, chưa có hiệu lực trong state).
- **Quarantine**: số dòng event hỏng bị orchestrator cách ly trong `state.json`.

Payload và `detail` dài bị cắt ở 500 ký tự. Mọi text đều được escape khi dựng HTML (log là dữ
liệu không tin cậy).

## Provenance

Nguồn `audit.jsonl` + file event; số bản ghi = số bản ghi audit + số event; `as_of` = lần fold
cuối. Thiếu cả hai → "không có dữ liệu"; thiếu một → "thiếu một phần nguồn". Dòng hỏng bị bỏ qua
và được đếm trong huy hiệu.

## Tin cậy

Audit log best-effort, **không chống giả mạo** (R15), không có hash chain.

## Chỉ đọc

Adapter đọc file log mà không "sửa" dòng ghi dở (khác `Ledger`/`AuditLog` khi mở để ghi).
