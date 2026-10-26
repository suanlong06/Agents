# View 6 — Token cost theo task

Snapshot: `views.costs`. Dữ liệu đến Muse VM qua snapshot ([handoff](00_snapshot_handoff.md)).

## Nguồn

`ledger.jsonl` (cost ledger, PR-1a, R5). Đọc như `Ledger.read` (chỉ bản ghi đúng schema
`ledger`) và gộp theo key idempotent `(task_id, call_id, attempt)` như `Ledger`: bản ghi đầu
tiên thắng, bản trùng được đếm riêng và không cộng thêm.

## Hiển thị

- Mỗi task: số lời gọi, input/output token, chi phí USD, số lời gọi ước lượng (`estimated`),
  các model thực sự phục vụ, chi phí theo attempt.
- Dòng **Tổng ledger**.
- Chi phí lấy từ `cost_usd` đã ghi trong ledger (giá `config/prices.yaml` lúc ghi). Lời gọi của
  model chưa có giá (`cost_usd = null`) **không được tính là 0**: ô chi phí hiện "≥ X (n lời gọi
  chưa có giá)", và chi phí attempt đó là "không có dữ liệu".

## Contract (test trên Windows CI)

Tổng trên dashboard **bằng** tổng ledger: `views.costs.totals` = `Ledger.totals()` của
orchestrator, mỗi task bằng `Ledger.totals(task_id)`, và con số in trên trang
(`data-total-cost`) bằng tổng ledger định dạng bằng `fmt_usd`.

## Provenance

Nguồn `ledger.jsonl`; số bản ghi = số lời gọi duy nhất; `as_of` = lần fold cuối. Không có
`ledger.jsonl` → "không có dữ liệu" (không có dòng tổng). File rỗng → tổng `0`.

## Chỉ đọc

Không có nút đặt ngân sách hay đổi giá.
