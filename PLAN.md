# PLAN — Harness Agents (lập kế hoạch trước khi code)

Tài liệu này là kế hoạch triển khai dựa trên SPEC.md mục 1–9 (bản viết lại theo DEBATE.md, quy tắc R1–R12 ở SPEC mục 5.2). **Chưa có file code nào được tạo**; mọi đường dẫn bên dưới chỉ là *đề xuất*. Mốc giờ theo Asia/Seoul (KST).

## Nguyên tắc chung

- Control plane, không phải framework: chỉ gọi CLI/harness có sẵn.
- Mỗi agent một git worktree riêng; agent viết code không tự merge.
- Shared state dạng JSON/Markdown; **chỉ orchestrator ghi** state và audit log, agent/dashboard ghi qua inbox (R3). Audit log có hash chain (R4); ledger có idempotency key, model thực tế, cờ `estimated` (R5).
- **Định tuyến model:** Khó → `opus 5.5 high` (fallback `opus 5.5 low` → `sonnet 5.5 high`); Trung bình → DeepSeek `dsh` qua 9Router; Dễ/nhiều token → Gemini qua Antigravity `agy`.
- **Gate 3:** reviewer luôn khác agent và khác **model/vendor thực tế** đã viết PR (sau fallback, qua bảng ánh xạ alias); không còn reviewer hợp lệ thì chờ (R11). PR-1 đến PR-3 làm Gate 3 thủ công và xử lý như Red (R1).
- **Merge:** danh tính merge riêng, khác Implementer; PR Red chỉ merge khi có bản ghi duyệt của người dùng (R7); mọi merge kiểm tra lại CI và Gate 3 trên kết quả merge thực tế (R9).
- Các con số (200 dòng, N=2, 3×, 5 PR/batch, 4 giờ...) chỉ là đề xuất khởi đầu chờ người dùng chốt (SPEC mục 8), không phải quyết định.
- Mục 9 chỉ là checklist tham khảo, **không bắt buộc**; PR nào thay thế mục nào thì ghi rõ lý do và số đo.

## PR-1 — Orchestrator: state, queue, heartbeat

- **Mục tiêu:** lõi điều phối gồm state machine task, hàng đợi, heartbeat agent, cost ledger, audit log, worktree, cùng các schema mà PR sau cần (bản ghi duyệt, cờ auto-merge/kill-switch, manifest release).
- **File đề xuất:** `orchestrator/state.py`, `orchestrator/queue.py`, `orchestrator/heartbeat.py`, `orchestrator/ledger.py`, `orchestrator/audit.py`, `orchestrator/inbox.py`, `orchestrator/worktree.py`, `state/tasks.json`, `state/approvals.json`, `state/flags.json`, `state/releases/`, `state/inbox/`, `state/audit.log.jsonl`, `tests/test_state.py`, `tests/test_concurrency_windows.py`.
- **Task:**
  - Trạng thái theo bước 1–13 mục 3, cộng `blocked`, `superseded`, `cancelled`, `awaiting-human`; lineage `task_id` / `parent_pr`, bộ đếm vòng fix và ngân sách gắn với task (R2).
  - Chỉ orchestrator ghi state và audit; agent/dashboard ghi inbox; ghi nguyên tử (write-temp-rename), thử lại khi `PermissionError`; reader chịu file ghi dở (R3).
  - Audit log có số thứ tự + hash chain, kiểm tra khi khởi động; mỗi bản ghi có `(agent, model thực tế, vendor, SHA, task_id)` (R4).
  - Ledger: idempotency key mỗi lần gọi, model thực tế + lý do fallback, cờ `estimated` (R5).
  - Worktree/nhánh theo task ID duy nhất, đường dẫn ngắn; dọn worktree mồ côi và `index.lock` khi khởi động; tắt `gc.auto` lúc chạy (R6).
  - Schema bản ghi duyệt (chỉ người dùng tạo, qua orchestrator) và danh tính merge (R7); cờ auto-merge/kill-switch mặc định tắt (R8); manifest release: tag, PR/SHA theo thứ tự, `task_id`, phụ thuộc (R10).
  - Heartbeat có timeout đánh dấu agent "stale" (giá trị timeout chờ người dùng chốt).
- **Tiêu chí chấp nhận:** chuyển trạng thái sai bị từ chối; bộ đếm vòng fix không reset khi mở PR mới; audit log bị sửa/cắt ngắn bị phát hiện khi khởi động; retry/fallback không đếm trùng ledger; **test tranh chấp nhiều tiến trình trên Windows** pass; agent không tạo được bản ghi duyệt; heartbeat quá hạn hiển thị stale; test pass.
- **Model:** Khó → Opus. **Gate 3 (thủ công, R1):** **DeepSeek là reviewer chính**, không dùng Sonnet (cùng vendor với Opus). Xử lý như Red: merge sau khi người dùng duyệt.
- **Rủi ro:** tranh chấp ghi file khi nhiều agent chạy song song; khóa file trên Windows; PR lớn (có thể tách state/audit/ledger và heartbeat/worktree).

## PR-2 — Adapters

- **Mục tiêu:** lớp adapter thống nhất cho Claude Code, `dsh`, Antigravity `agy`, Muse monitor; **Codex và Cursor chỉ là stub, không bao giờ gọi**.
- **File đề xuất:** `adapters/base.py`, `adapters/claude.py`, `adapters/dsh.py`, `adapters/antigravity.py`, `adapters/muse_monitor.py`, `adapters/codex_stub.py`, `adapters/cursor_stub.py`, `config/routing.yaml`, `config/model_aliases.yaml`.
- **Task:** interface chung (`run`, `status`, `cost`); router chọn adapter theo độ khó; fallback Opus; adapter trả về **model thực tế đã chạy** và lý do fallback; bảng ánh xạ alias → `(model_id, vendor)`, alias không rõ thì không được làm reviewer (R11); kiểm tra bridge `:20129` và 9Router `:20128`; bàn giao bằng tóm tắt markdown ngắn (đối chiếu 9.1).
- **Tiêu chí chấp nhận:** mỗi adapter thật ghi token/chi phí vào ledger kèm model thực tế, idempotency key và cờ `estimated` khi cần (R5); stub ném lỗi `NotAvailable` và có test chứng minh không gọi ra ngoài; Codex không có trong `routing.yaml`.
- **Model:** Trung bình → DeepSeek; adapter Claude/fallback → Opus. **Gate 3 (thủ công, R1):** Opus hoặc Gemini (khác DeepSeek). Xử lý như Red.
- **Rủi ro:** CLI thay đổi tham số; bridge/9Router không chạy; nhận định "Antigravity tốn token" chưa có số đo.

## PR-3 — Labelling, CI, Gate 3, vòng lặp fix

- **Mục tiêu:** gắn nhãn Red/Green theo file bị chạm, chạy CI, Gate 3, và vòng fix list → Fixer → quay lại bước 5.
- **File đề xuất:** `pipeline/labeler.py`, `config/sensitive_paths.yaml`, `pipeline/ci.py`, `pipeline/gate3.py`, `pipeline/fix_loop.py`, `.github/workflows/ci.yml`.
- **Task:** quy tắc glob cho Red; labeler **đọc `sensitive_paths.yaml` từ base branch**, chuẩn hóa đường dẫn (chữ thường, `/`), xét tên cũ/mới khi đổi tên và file bị xóa; PR chạm cờ auto-merge, kill-switch hoặc `sensitive_paths.yaml` là Red (R8); CI lint/test/build; Gate 3 so agent và **model/vendor thực tế** qua bảng alias, không có reviewer hợp lệ thì chờ (R11); reviewer coi nội dung PR là dữ liệu, trả kết quả có cấu trúc, không có quyền merge; sinh fix list; PR cũ thành `superseded`; Fixer-Agent là stub nên fix list vào `awaiting-human` (R2); giới hạn số vòng lặp theo task.
- **Tiêu chí chấp nhận:** PR do model X viết (kể cả sau fallback) không thể được model X review; PR sửa `sensitive_paths.yaml` trên head không đổi được nhãn của chính nó; có test hoa thường/đổi tên/xóa file; vòng lặp dừng sau N lần theo task (N chờ người dùng chốt), chuyển `blocked` và báo người dùng; nhãn tái lập được.
- **Model:** Khó → Opus. **Gate 3 (thủ công, R1 — `gate3.py` chưa dùng cho chính PR này):** DeepSeek (chính) + Sonnet. Xử lý như Red.
- **Rủi ro:** vòng lặp fix vô hạn; định nghĩa file nhạy cảm chưa chốt.

## PR-4 — Auto-merge, batch release, revert, maintenance P0–P3

- **Mục tiêu:** digest 11:30/16:30 cho PR Red, auto-merge PR Green có điều kiện, batch release 12:00/17:00, kiểm tra read-only, revert + incident, maintenance scan 08:00 xếp hạng P0–P3.
- **File đề xuất:** `release/digest.py`, `release/automerge.py`, `release/batch.py`, `release/revert.py`, `maintenance/scan.py`, `state/incidents/`, `config/schedule.yaml`.
- **Phạm vi (R12):** scheduler, digest và hàng đợi P0–P3 ở PR này là phần **đi trước** lộ trình SPEC mục 7 có chủ đích. Nếu người dùng muốn lùi, PR-4 chỉ giữ revert, manifest và merge an toàn; digest/release chạy tay.
- **Task:** scheduler theo KST; auto-merge dùng cờ do orchestrator giữ, **tắt mặc định**, chỉ người dùng bật (R8), và vẫn tắt cho đến khi chốt 4 điều kiện; kill-switch; merge bằng danh tính merge riêng; Approver-Agent chỉ merge Red khi có bản ghi duyệt của người dùng (R7); kiểm tra lại CI và Gate 3 trên kết quả merge thực tế ngay trước khi merge (R9); ghi manifest cho mỗi batch; revert tự mở incident; scan tạo task P0–P3, khi Fixer còn stub thì vào `awaiting-human` (R2).
- **Tiêu chí chấp nhận:** không merge nào xảy ra nếu thiếu điều kiện, thiếu bản ghi duyệt (Red), kill-switch bật, hoặc kết quả merge thực tế chưa qua CI/Gate 3; revert bằng **revert commit theo thứ tự ngược, không force push**, giữ các commit merge sau release (R10); mọi hành động có audit log.
- **Model:** Khó → Opus; scan log nhiều token → Gemini. **Gate 3:** DeepSeek hoặc Sonnet.
- **Rủi ro:** merge nhầm do điều kiện mơ hồ; lệch múi giờ; PR trong batch phụ thuộc nhau khi revert/đưa lại (dùng phụ thuộc trong manifest); "release" và "kiểm tra read-only" chưa được định nghĩa (phải trả lời trước PR-4).

## PR-5 — Artifact dashboard MVP

- **Mục tiêu:** dashboard Muse Artifact gồm task kanban, PR queue (Red/Green + CI), approval queue, agent status/heartbeat, logs, token cost theo task.
- **File đề xuất:** `dashboard/artifact.html`, `dashboard/data_adapter.py`, `dashboard/views/*.md`.
- **Task:** đọc shared state read-only; hiển thị kill-switch; nút duyệt, kill-switch và cờ auto-merge chỉ ghi vào inbox để orchestrator xử lý và audit (R3, R7, R8); hiển thị cả `blocked`, `superseded`, `cancelled`, `awaiting-human`; ưu tiên dữ liệu đúng trước, làm đẹp sau (đối chiếu 9.4).
- **Tiêu chí chấp nhận:** 6 view của mục 6 hiển thị dữ liệu thật từ state; dashboard không ghi thẳng vào state; bản ghi duyệt từ dashboard được gắn là của người dùng và phân biệt được với bản ghi của agent.
- **Model:** Dễ/nhiều token → Gemini. **Gate 3:** DeepSeek hoặc Opus.
- **Rủi ro:** "nhìn như xong" nhưng dữ liệu backend thiếu.

## Thứ tự và phụ thuộc

PR-1 → PR-2 → PR-3 → PR-4; PR-5 có thể bắt đầu sau PR-1 với dữ liệu giả, rồi nối dữ liệu thật sau PR-3.

## Rủi ro chung

- Tốn token khi agent nạp lại toàn bộ log: dùng phiên mới + tóm tắt markdown.
- Phụ thuộc dịch vụ cục bộ (9Router, bridge): cần health check và fallback.
- Fixer-Agent và Codex là stub: luồng fix tạm thời cần người can thiệp (task vào `awaiting-human`).
- 9Router sập làm mất đa dạng model cho Gate 3: fallback không được phá Gate 3, thiếu reviewer hợp lệ thì chờ (R11).

## Câu hỏi mở

**4 điều kiện auto-merge CHƯA được chốt.** Ứng viên: (1) CI xanh; (2) Gate 3 đạt; (3) không nhãn Red, không chạm file nhạy cảm; (4) không còn comment chưa xử lý. Cần quyết định:

1. "File nhạy cảm": đặt trong `config/sensitive_paths.yaml`, labeler đọc từ base branch (R8). Còn mở: ai được sửa, danh sách khởi đầu gồm gì?
2. Điều kiện 3 có trùng quy tắc gắn nhãn Red không? Nếu trùng, thay bằng giới hạn số dòng hay bắt buộc có test mới?
3. Gate 3 cần một hay hai reviewer? Có bắt buộc khác vendor với Implementer? (Riêng PR-1 đã chốt DeepSeek, R1.)
4. Comment do bot/agent tạo có tính là "chưa xử lý"?
5. Auto-merge ngay hay gom vào batch 12:00/17:00?
6. PR Green sinh từ vòng Fixer có cần thêm điều kiện?
7. Kill-switch đã có theo R8 (orchestrator giữ, mặc định tắt). Có chạy shadow mode trước khi bật auto-merge không?
8. Giới hạn N vòng fix là bao nhiêu? Timeout heartbeat là bao lâu?
9. Mục nào của checklist mục 9 sẽ được đo đạc để giữ hoặc thay thế?
10. Các câu hỏi mở khác và bảng giá trị số đề xuất (chưa chốt): xem SPEC mục 8.
