# View 1 — Task kanban

Snapshot: `views.kanban`. Dữ liệu đến Muse VM qua snapshot ([handoff](00_snapshot_handoff.md));
VM không đọc `state_dir`.

## Nguồn

`state.json` (snapshot duy nhất do orchestrator ghi khi fold, PR-1a). Đọc bằng
`orchestrator.state.read_snapshot` (chỉ đọc, kiểm schema `state`, đọc được version N+1).

## Hiển thị

- Một cột cho mỗi bước 1–13 (`intake` … `maintenance`, theo `STEP_STATES`) và một cột cho mỗi
  trạng thái mở rộng: `escalated`, `blocked`, `superseded`, `cancelled`, `awaiting-human`.
- Trạng thái lạ (state do version sau ghi) vào cột riêng `unknown`, không bị bỏ qua.
- Thẻ task: `task_id`, tiêu đề, attempt, PR, số vòng fix, lý do escalate, bước sẽ quay lại khi
  đang `blocked`, `updated_at`. Mới cập nhật trước.
- `superseded` ở đây là task đã kết thúc ở `superseded`. Attempt cũ bị thay bằng PR mới nằm
  trong lịch sử của task và hiện ở [PR queue](02_pr_queue.md).

## Provenance, "không có dữ liệu" và "0"

- Nguồn `state.json`, số bản ghi = số task, `as_of` = `state.json.as_of` (lần fold cuối).
- Không có `state.json` → cả view "không có dữ liệu", số task ở mỗi cột cũng là "không có dữ
  liệu". Có `state.json` mà không có task → mọi cột hiện `0`.
- `as_of` sai định dạng (ví dụ `"now"`) → `as_of = null`, view ở trạng thái lỗi.

## Chỉ đọc

Không kéo thả, không đổi trạng thái. Đổi trạng thái chỉ qua event mà orchestrator fold.
