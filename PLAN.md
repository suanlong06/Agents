# PLAN — Harness Agents (lập kế hoạch trước khi code)

Tài liệu này là kế hoạch triển khai dựa trên SPEC.md mục 1–9. **Chưa có file code nào được tạo**; mọi đường dẫn bên dưới chỉ là *đề xuất*. Mốc giờ theo Asia/Seoul (KST).

## Nguyên tắc chung

- Control plane, không phải framework: chỉ gọi CLI/harness có sẵn.
- Mỗi agent một git worktree riêng; agent viết code không tự merge.
- Shared state dạng JSON/Markdown; cost ledger và audit log append-only.
- **Định tuyến model:** Khó → `opus 5.5 high` (fallback `opus 5.5 low` → `sonnet 5.5 high`); Trung bình → DeepSeek `dsh` qua 9Router; Dễ/nhiều token → Gemini qua Antigravity `agy`.
- **Gate 3:** reviewer luôn khác agent/model đã viết PR.
- Mục 9 chỉ là checklist tham khảo, **không bắt buộc**; PR nào thay thế mục nào thì ghi rõ lý do và số đo.

## PR-1 — Orchestrator: state, queue, heartbeat

- **Mục tiêu:** lõi điều phối gồm state machine task, hàng đợi, heartbeat agent, cost ledger, audit log.
- **File đề xuất:** `orchestrator/state.py`, `orchestrator/queue.py`, `orchestrator/heartbeat.py`, `orchestrator/ledger.py`, `orchestrator/audit.py`, `state/tasks.json`, `state/audit.log.jsonl`, `tests/test_state.py`.
- **Task:** định nghĩa trạng thái theo bước 1–13 mục 3; ghi file nguyên tử (write-temp-rename); heartbeat có timeout đánh dấu agent "stale"; tạo worktree cho từng task.
- **Tiêu chí chấp nhận:** chuyển trạng thái sai bị từ chối; audit log chỉ append; heartbeat quá hạn hiển thị stale; test pass.
- **Model:** Khó → Opus. **Gate 3:** DeepSeek hoặc Sonnet (khác Opus phiên viết).
- **Rủi ro:** tranh chấp ghi file khi nhiều agent chạy song song; khóa file trên Windows.

## PR-2 — Adapters

- **Mục tiêu:** lớp adapter thống nhất cho Claude Code, `dsh`, Antigravity `agy`, Muse monitor; **Codex và Cursor chỉ là stub, không bao giờ gọi**.
- **File đề xuất:** `adapters/base.py`, `adapters/claude.py`, `adapters/dsh.py`, `adapters/antigravity.py`, `adapters/muse_monitor.py`, `adapters/codex_stub.py`, `adapters/cursor_stub.py`, `config/routing.yaml`.
- **Task:** interface chung (`run`, `status`, `cost`); router chọn adapter theo độ khó; fallback Opus; kiểm tra bridge `:20129` và 9Router `:20128`; bàn giao bằng tóm tắt markdown ngắn (đối chiếu 9.1).
- **Tiêu chí chấp nhận:** mỗi adapter thật trả về token/chi phí vào ledger; stub ném lỗi `NotAvailable` và có test chứng minh không gọi ra ngoài.
- **Model:** Trung bình → DeepSeek; adapter Claude/fallback → Opus. **Gate 3:** Opus hoặc Gemini (khác DeepSeek).
- **Rủi ro:** CLI thay đổi tham số; bridge/9Router không chạy; nhận định "Antigravity tốn token" chưa có số đo.

## PR-3 — Labelling, CI, Gate 3, vòng lặp fix

- **Mục tiêu:** gắn nhãn Red/Green theo file bị chạm, chạy CI, Gate 3, và vòng fix list → Fixer → quay lại bước 5.
- **File đề xuất:** `pipeline/labeler.py`, `config/sensitive_paths.yaml`, `pipeline/ci.py`, `pipeline/gate3.py`, `pipeline/fix_loop.py`, `.github/workflows/ci.yml`.
- **Task:** quy tắc glob cho Red; CI lint/test/build; Gate 3 kiểm tra reviewer ≠ writer (agent và model); sinh fix list; Fixer-Agent là stub nên fix list tạm vào hàng đợi chờ người/agent khác; giới hạn số vòng lặp.
- **Tiêu chí chấp nhận:** PR do model X viết không thể được model X review; vòng lặp dừng sau N lần và báo người dùng; nhãn tái lập được.
- **Model:** Khó → Opus. **Gate 3:** DeepSeek + Sonnet.
- **Rủi ro:** vòng lặp fix vô hạn; định nghĩa file nhạy cảm chưa chốt.

## PR-4 — Auto-merge, batch release, revert, maintenance P0–P3

- **Mục tiêu:** digest 11:30/16:30 cho PR Red, auto-merge PR Green có điều kiện, batch release 12:00/17:00, kiểm tra read-only, revert + incident, maintenance scan 08:00 xếp hạng P0–P3.
- **File đề xuất:** `release/digest.py`, `release/automerge.py`, `release/batch.py`, `release/revert.py`, `maintenance/scan.py`, `state/incidents/`, `config/schedule.yaml`.
- **Task:** scheduler theo KST; auto-merge **tắt mặc định** cho đến khi chốt 4 điều kiện; kill-switch; revert tự mở incident; scan tạo task P0–P3 cho Fixer.
- **Tiêu chí chấp nhận:** không merge nào xảy ra nếu thiếu điều kiện hoặc kill-switch bật; revert đưa main về trạng thái trước release; mọi hành động có audit log.
- **Model:** Khó → Opus; scan log nhiều token → Gemini. **Gate 3:** DeepSeek hoặc Sonnet.
- **Rủi ro:** merge nhầm do điều kiện mơ hồ; lệch múi giờ; release gom nhiều PR khó tách khi revert.

## PR-5 — Artifact dashboard MVP

- **Mục tiêu:** dashboard Muse Artifact gồm task kanban, PR queue (Red/Green + CI), approval queue, agent status/heartbeat, logs, token cost theo task.
- **File đề xuất:** `dashboard/artifact.html`, `dashboard/data_adapter.py`, `dashboard/views/*.md`.
- **Task:** đọc shared state read-only; hiển thị kill-switch; ưu tiên dữ liệu đúng trước, làm đẹp sau (đối chiếu 9.4).
- **Tiêu chí chấp nhận:** 6 view của mục 6 hiển thị dữ liệu thật từ state; không có thao tác ghi ngoài kill-switch và nút duyệt.
- **Model:** Dễ/nhiều token → Gemini. **Gate 3:** DeepSeek hoặc Opus.
- **Rủi ro:** "nhìn như xong" nhưng dữ liệu backend thiếu.

## Thứ tự và phụ thuộc

PR-1 → PR-2 → PR-3 → PR-4; PR-5 có thể bắt đầu sau PR-1 với dữ liệu giả, rồi nối dữ liệu thật sau PR-3.

## Rủi ro chung

- Tốn token khi agent nạp lại toàn bộ log: dùng phiên mới + tóm tắt markdown.
- Phụ thuộc dịch vụ cục bộ (9Router, bridge): cần health check và fallback.
- Fixer-Agent và Codex là stub: luồng fix tạm thời cần người can thiệp.

## Câu hỏi mở

**4 điều kiện auto-merge CHƯA được chốt.** Ứng viên: (1) CI xanh; (2) Gate 3 đạt; (3) không nhãn Red, không chạm file nhạy cảm; (4) không còn comment chưa xử lý. Cần quyết định:

1. "File nhạy cảm" định nghĩa ở file cấu hình hay danh sách glob? Ai được sửa?
2. Điều kiện 3 có trùng quy tắc gắn nhãn Red không? Nếu trùng, thay bằng giới hạn số dòng hay bắt buộc có test mới?
3. Gate 3 cần một hay hai reviewer? Có bắt buộc khác vendor với Implementer?
4. Comment do bot/agent tạo có tính là "chưa xử lý"?
5. Auto-merge ngay hay gom vào batch 12:00/17:00?
6. PR Green sinh từ vòng Fixer có cần thêm điều kiện?
7. Có cần kill-switch trên dashboard không (kế hoạch đề xuất: có)?
8. Giới hạn N vòng fix là bao nhiêu? Timeout heartbeat là bao lâu?
9. Mục nào của checklist mục 9 sẽ được đo đạc để giữ hoặc thay thế?
