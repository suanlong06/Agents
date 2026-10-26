# Dashboard — snapshot handoff sang Muse VM (PR-5, SPEC mục 6)

Code: `dashboard/data_adapter.py`, template `dashboard/artifact.html`, schema snapshot
`dashboard/snapshot.schema.json`. Test: `tests/test_dashboard_contract.py` (Windows CI).

## Vì sao là snapshot, không phải đọc trực tiếp

- **Muse Artifact được host trên Muse VM, không phải trên máy chạy harness.** Nó không đọc
  được `state_dir` cục bộ của máy này (mặc định `%LOCALAPPDATA%\harness`, tức
  `C:\Users\water\AppData\Local\harness`). Đây là câu trả lời cho SPEC mục 8 câu 13 mà PR-5
  dựa vào.
- Vì vậy dashboard **chỉ thấy một snapshot**: file do `data_adapter.py` xuất ra trên máy
  harness rồi được chuyển sang Muse VM. **Làm mới = một snapshot mới.** Trang không tự làm
  mới, không gọi mạng, không có server hay endpoint nào trên máy harness.
- Không mở cổng, không chạy server cục bộ để VM đọc vào: như thế là thêm một endpoint, trái
  với "chỉ đọc, không endpoint" (SPEC mục 5.2, 6).

## Quy trình

Trên máy chạy harness (PowerShell, từ thư mục repo):

```powershell
# 1. Một bước: đọc state_dir (chỉ đọc) và dựng trang tĩnh có nhúng snapshot.
python -m dashboard.data_adapter render --out "$env:TEMP\harness-dashboard.html"

# Hoặc hai bước: xuất snapshot JSON, rồi dựng trang từ snapshot đó.
python -m dashboard.data_adapter snapshot --out "$env:TEMP\harness-snapshot.json"
python -m dashboard.data_adapter render --snapshot "$env:TEMP\harness-snapshot.json" --out "$env:TEMP\harness-dashboard.html"
```

2. Người dùng chuyển `harness-dashboard.html` (một file tự chứa: HTML + CSS + snapshot JSON
   nhúng) sang Muse VM và mở nó làm Artifact. `data_adapter.py` **không** tự tải lên đâu cả.
3. Muốn số liệu mới: chạy lại bước 1 và chuyển lại file. Trang cũ vẫn hiện snapshot cũ cùng
   `as_of` cũ của nó.

`--state-dir D` đọc một `state_dir` khác (mặc định lấy từ `config/harness.yaml` /
`HARNESS_STATE_DIR`). `--fixture` đánh dấu snapshot là dữ liệu giả.

## Quy tắc của snapshot

- **Chỉ đọc `state_dir`.** Adapter chỉ mở file state ở chế độ đọc và chỉ dùng hàm đọc tĩnh;
  không dựng `Orchestrator`, `Ledger`, `AuditLog`, `ApprovalMirror` (constructor của chúng
  giữ `WriterGuard` và có thể đóng dòng ghi dở, tức là ghi). Test chạy adapter bên trong
  `release.readonly_check.verify_readonly` (audit hook + so ảnh thư mục) và kiểm AST.
- **File đầu ra** chỉ được ghi ra đường dẫn người dùng chỉ định, và adapter **từ chối** mọi
  đường dẫn nằm trong `state_dir` hoặc trong repo (runtime data không vào git, R13). Mặc định
  `snapshot` in ra stdout.
- **Provenance mỗi view:** file nguồn, số bản ghi, `as_of`. `as_of` = thời điểm fold cuối
  cùng ghi trong `state.json`, **không bao giờ là giờ hiện tại hay chuỗi "now"**; giá trị không
  phải ISO 8601 có múi giờ bị loại (`as_of = null`, view chuyển trạng thái lỗi). `exported_at`
  là lúc xuất snapshot và được ghi riêng, không thay cho `as_of`.
- **"Không có dữ liệu" khác "0":** file nguồn không có → `records = null`, view hiện "không có
  dữ liệu"; file có nhưng rỗng → `records = 0`, view hiện `0`.
- **Watermark FAKE** do adapter sinh ra từ trường dữ liệu `source` của snapshot, không từ CSS.
  Snapshot là `fixture` khi người gọi dùng `--fixture`, khi `state.json` mang
  `source: "fixture"`, hoặc khi `state_dir` nằm trong repo. Không có cách hạ `fixture` về
  `runtime`. Trang gắn watermark cho mọi `source` khác `runtime` (kể cả thiếu hay lạ).
- **Không thao tác ghi trên trang:** không JavaScript, không form, nút, input; CSP
  `script-src 'none'; form-action 'none'; connect-src 'none'`. Link duy nhất là link
  `https://github.com/...`. Duyệt diễn ra trên GitHub. Kill-switch chỉ để xem.

## Tin cậy (R15)

Snapshot chỉ phản chiếu `state_dir` lúc xuất. State, audit và snapshot đều **best-effort,
không chống giả mạo**: agent cùng OS user ghi được `state_dir`, và file snapshot có thể bị sửa
trên đường chuyển sang VM. Dashboard không bao giờ là nguồn sự thật cho duyệt hay merge; mỏ
neo tin cậy là GitHub login + branch protection + required check `ci`.

## Dữ liệu thật lúc làm PR-5 (2026-10-26)

`C:\Users\water\AppData\Local\harness` lúc đó **chỉ có thư mục `jobs\`** (file trạng thái của
trình chạy job bên ngoài dùng để chạy các phiên PR; không thuộc schema harness, dashboard không
đọc). **Chưa có** `state.json`, `ledger.jsonl`, `audit.jsonl`, `heartbeats.json`,
`approvals.jsonl`, `killswitch.json` hay `inbox\`. Snapshot thật vì vậy hiện **"không có dữ
liệu" ở cả 6 view** và kill-switch **BẬT** (thiếu file → fail-closed). Theo PLAN, PR-5 **không
merge khi chưa có dữ liệu thật** từ state.

## View

1. [Task kanban](01_task_kanban.md)
2. [PR queue](02_pr_queue.md)
3. [Approval queue](03_approval_queue.md)
4. [Agent status](04_agent_status.md)
5. [Logs](05_logs.md)
6. [Token cost theo task](06_token_cost.md)
