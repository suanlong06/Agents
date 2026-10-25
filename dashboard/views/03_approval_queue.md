# View 3 — Approval queue

Snapshot: `views.approvals`. Dữ liệu đến Muse VM qua snapshot ([handoff](00_snapshot_handoff.md)).

**Duyệt diễn ra trên GitHub, không trên dashboard.** View này chỉ liệt kê việc đang chờ người
dùng và dẫn link sang GitHub.

## Nguồn

- `state.json`: task đang chờ người dùng:
  - `spec_review` → "Spec chờ duyệt" (bước 3);
  - `approved_merge` → "PR chờ người dùng review + merge" (Gate 3 đạt; người dùng là người merge
    duy nhất trong MVP, R7);
  - `escalated`, `awaiting-human` → việc cần người dùng quyết định (kèm lý do nếu có).
- `approvals.jsonl`: **bản phản chiếu** GitHub review của người dùng (PR-3). Hiện review mới nhất
  của PR và nó có gắn với đúng head SHA không ("khớp head" / "KHÔNG khớp head: phải duyệt lại").
  Bản phản chiếu chỉ để xem, không phải nguồn sự thật; quyết định duyệt luôn đọc lại từ GitHub
  (`pipeline.approvals.github_approval`).

## Link

- Có PR → `https://github.com/<repo>/pull/<n>`.
- Spec chưa có PR → trang tìm PR trên GitHub theo `task_id`.
- `html_url` trong bản phản chiếu chỉ được giữ nếu bắt đầu bằng `https://github.com/`.
- Repo lấy từ `github.repo` trong `config/harness.yaml`; thiếu thì không có link.

## Provenance

Nguồn `state.json` + `approvals.jsonl`; số bản ghi = số mục đang chờ; `as_of` = lần fold cuối.
Thiếu `approvals.jsonl` → view "thiếu một phần nguồn" và cột review hiện "không có dữ liệu".
Không có `state.json` → cả view "không có dữ liệu".

## Chỉ đọc

Không có nút duyệt, từ chối hay merge.
