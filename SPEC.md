# SPEC — Harness Agents

> **Bản viết lại ngày 2026-10-25 theo DEBATE.md thật** (4 subagent Claude Code độc lập: Actor, Critic, Skeptic Red Team, Judge; phán quyết **ACCEPT WITH CHANGES**).
> Bản này **thay thế** bản SPEC viết theo DEBATE.md do agent điều phối tự tổng hợp ở commit `583454f`. Bản DEBATE đó vẫn còn trong lịch sử git, nhưng **không còn là căn cứ**.
> Quy tắc R1–R12 của bản trước được giữ khi không mâu thuẫn với DEBATE thật, và bị sửa hoặc thay khi DEBATE thật quyết khác (đối chiếu ở mục 5.4). DEBATE mục 6 (thay đổi bắt buộc trước PR-1) được áp dụng đầy đủ.
> 4 điều kiện auto-merge vẫn là **OPEN DISCUSSION** (mục 8). Mục 9 chỉ là **checklist tham khảo**. Mọi con số là **mặc định tạm trong config**, không phải yêu cầu.

Ký hiệu: `D§n` là mục n trong DEBATE.md. `S:Lnn` là dòng trong SPEC gốc (commit `39c32e4`), giống cách DEBATE.md trích dẫn.

## 1. Mục tiêu

Harness Agents là một **control plane** chạy trên máy Windows này. Nó điều phối nhiều agent AI qua dashboard **Muse Artifact**. **Muse AI** theo dõi tiến độ và thông báo cho người dùng. Hệ thống không phải framework: nó chỉ điều phối, ghi nhận và kiểm soát các công cụ agent sẵn có.

**Phạm vi MVP** (R12): PR-0, PR-1a, PR-1b, PR-2, PR-3, PR-4a, PR-5. PR-4b và PR-4c là **tính năng sau** (mục 7).

## 2. Vai trò

| Vai trò | Nhiệm vụ |
|---|---|
| Spec-Agent | Viết spec 1 trang |
| Approver-Agent | Duyệt spec cùng người dùng. Với PR Red: **đề xuất** merge. Trong MVP, Approver-Agent **không merge** (R7) |
| Implementer | Viết code, push bằng **bot account riêng**, mở PR (R7) |
| Reviewer Gate 3 | Review PR. Bắt buộc khác agent và khác mọi model **thực sự phục vụ** trong writer set (R11). Chỉ trả verdict, không có quyền merge |
| Fixer-Agent | **Stub** (chờ Cursor). Nhận fix list, mở PR mới. Khi còn là stub, fix list vào `awaiting-human` và người dùng điều phối việc sửa |
| Người dùng | Người duyệt cuối cùng. **Trong MVP là người duy nhất merge**, qua GitHub UI (R7) |

Spec-Agent, Approver-Agent và Fixer-Agent thay cho Hải, Anh, Luân và Cursor trong flowchart. Đây là các model agent, không phải người thật.

**Orchestrator** là thành phần hệ thống, không phải agent model. Nó là bên **duy nhất ghi runtime state** (R3), và runtime state nằm **ngoài repo** (R13). Trong MVP, orchestrator **không merge** và không có danh tính merge. Danh tính merge của bot là tính năng sau (mục 7).

## 3. Quy trình

Mọi mốc giờ đều theo **Asia/Seoul (KST)**. Quy trình 13 bước giữ nguyên tinh thần SPEC gốc. Cột "Trong MVP" ghi cách mỗi bước chạy cho tới khi có PR-4b/4c.

**Nhánh tính năng mới**

| # | Bước | Trong MVP |
|---|---|---|
| 1 | Nhu cầu → phân loại công việc | Như mô tả |
| 2 | Spec-Agent viết spec 1 trang | Như mô tả |
| 3 | Approver-Agent và người dùng duyệt spec | Bản ghi duyệt là review/comment của người dùng trên GitHub (R7) |
| 4 | Implementer mở PR | Push bằng bot account (R7) |
| 5 | Máy gắn nhãn **Red / Owner-only / Green** dựa trên file bị chạm (R8) | Có từ PR-3. Trước đó mọi PR coi như Owner-only (R1) |
| 6 | CI kiểm tra lint, test, build | GitHub Actions trên `windows-latest` (R14) |
| 7 | Gate 3: reviewer khác người viết review PR (R11) | PR-0..PR-3 làm thủ công (R1); từ PR-4a dùng `gate3.py` |
| 8 | Không đạt → fix list → Fixer-Agent mở PR mới → quay lại bước 5 | PR cũ thành `superseded`; vượt giới hạn thì `escalated` (R2). Fixer là stub nên fix list vào `awaiting-human` |
| 9 | Đạt: **Red** → digest 11:30 / 16:30 → duyệt → merge. **Green** → auto-merge khi đủ 4 điều kiện (mục 8) | Không có digest (tính năng sau). Mọi PR đều do **người dùng merge** qua GitHub UI. Auto-merge tắt; PR-4a chỉ ghi "would-merge" ở shadow mode |
| 10 | Batch release 12:00 và 17:00 | Thủ công, theo runbook PR-4a |
| 11 | Kiểm tra nhanh ở chế độ read-only | Phải được định nghĩa và có test trước khi merge PR-4a |
| 12 | Có bug → revert, release lại, mở incident | Thủ công theo R10 (PR-4a); tự động là PR-4b |
| 13 | Ổn → maintenance scan 08:00 xếp hạng P0–P3 → Fixer mở PR | Tính năng sau (PR-4c). Trong MVP, việc sửa lỗi đi qua hàng đợi `awaiting-human` |

**Nhánh sửa lỗi:** đi thẳng vào maintenance scan lúc 08:00 (PR-4c). Trong MVP: tạo task thủ công, vào `awaiting-human`.

## 4. Định tuyến model

Chọn model theo độ khó và mức ảnh hưởng của task:

| Mức | Đích | Model |
|---|---|---|
| Khó, ảnh hưởng cao | Claude Code native | `opus 5.5 high` → fallback `opus 5.5 low` → `sonnet 5.5 high` |
| Trung bình | DeepSeek harness `dsh` qua 9Router | `xh deepseek-v4.1-flash-1017` |
| Dễ, tốn nhiều token, thông tin nhiễu | Antigravity `agy` qua bridge `:20129` → 9Router `:20128` | `gemini-3.8-flash-medium`, `gemini-4-argon-medium` |

- **Fallback chỉ có trong chuỗi Opus** (S:L50). Tuyến `dsh` và `agy` **không** có fallback. Khi 9Router hoặc bridge sập: **chờ và thông báo** (R11).
- **Codex:** chưa có tài khoản và CLI. Chỉ có **stub adapter** để tích hợp sau. Codex **không bao giờ được gọi** và không có trong cấu hình định tuyến.
- **Cursor:** chưa dùng được. Fixer-Agent tạm thời là vai trò stub.
- **Fallback không bao giờ được phá Gate 3** (R11).

## 5. Nguyên tắc, tuyên bố tin cậy và quy tắc bắt buộc

### 5.1. Nguyên tắc chung

- **Control plane, không phải framework:** dùng lại CLI/harness có sẵn, không viết lại chúng.
- **Cô lập bằng worktree:** mỗi **task-attempt** một git worktree riêng, không chia sẻ giữa các agent (R6). Cách này chặt hơn "mỗi agent một worktree" của SPEC gốc, không trái với nó (D§3).
- **Không tự merge:** agent viết code không merge PR của chính nó. Trong MVP không agent nào merge; chỉ người dùng merge (R7).
- **Shared state:** dạng file JSON/JSONL mà người và agent đọc được, nằm **ngoài repo** (R13). Chỉ orchestrator ghi (R3).
- **Cost ledger:** ghi token và chi phí theo task, attempt và **model thực sự phục vụ** (R5).
- **Audit log:** append-only, ghi ai làm, model nào, lúc nào, trên task/PR/SHA nào, kết quả ra sao (R4). Mức tin cậy theo mục 5.2.
- **Fail-closed:** thiếu thông tin hoặc điều kiện thì dừng và chờ người dùng, không tự hạ chuẩn.

### 5.2. Tuyên bố tin cậy (R15)

Cho đến khi agent chạy dưới **một Windows user riêng, ít quyền** (điều kiện tiên quyết của PR-4b, mục 7):

- Agent chạy **cùng OS user** với orchestrator. Agent về kỹ thuật có thể ghi vào thư mục state, sửa inbox, cắt audit log, hoặc ghi file kill-switch (D§2.6 #6.5, N2).
- Vì vậy **state cục bộ và audit log chỉ là best-effort. Chúng KHÔNG chống giả mạo.** Không tài liệu, PR hay dashboard nào được tuyên bố ngược lại.
- **Mỏ neo tin cậy duy nhất** là đăng nhập GitHub của người dùng, branch protection trên `main`, và các required check chạy trên GitHub Actions (R14).
- **Dashboard chỉ đọc** trong MVP. Không có nút duyệt, không có nút kill-switch, không có endpoint ghi.
- Mức này chấp nhận được cho MVP vì **người dùng merge tay mọi PR** (D§4, chủ đề X).

### 5.3. Quy tắc bắt buộc R1–R17

**R1 — Quy tắc bootstrap cho PR-0..PR-3.** Khi `labeler.py` và `gate3.py` chưa tồn tại (trước khi PR-3 merge):
- **Người dùng merge tay**, qua GitHub UI. Mọi PR coi như Owner-only.
- **Gate 3 làm thủ công** bởi một model **khác vendor** với mọi model trong writer set. Kết quả ghi thành **comment trong PR**, gồm: writer `(agent, model, vendor)`, reviewer `(agent, model, vendor)`, **SHA được review**, verdict pass/fail kèm danh sách finding. Head SHA đổi sau review thì review mất hiệu lực, phải review lại.
- PR do Opus viết: reviewer là DeepSeek (hoặc Gemini). Không dùng Sonnet vì cùng vendor với Opus.
- **Không chấp nhận output CI dán tay** làm bằng chứng. Bằng chứng CI duy nhất là check run GitHub Actions gắn với SHA. (Riêng PR-0: chưa có CI trước nó, nên check run của chính PR-0 là bằng chứng, và người dùng đọc `ci.yml` bằng tay.)

**R2 — State machine và lineage.**
- Trạng thái task theo bước 1–13 ở mục 3, cộng các **trạng thái mở rộng** (được khai báo rõ là phần mở rộng so với SPEC gốc):
  - `escalated`: vượt N vòng fix hoặc ngân sách của task, hoặc không còn reviewer hợp lệ. Chờ người dùng quyết.
  - `blocked`: thiếu điều kiện ngoài (ví dụ 9Router/bridge sập, CI chưa có kết quả). Chờ và thông báo.
  - `superseded`: PR cũ khi PR mới được mở cho cùng task.
  - `cancelled`: người dùng hủy task.
  - `awaiting-human`: việc cần người làm, gồm mọi fix list và mọi task sửa lỗi khi Fixer còn là stub (giữ từ R2 bản trước).
- Lineage: mỗi task và PR mang `task_id`, `parent_pr`, `attempt`. Bộ đếm vòng fix và ngân sách cộng dồn theo **`task_id`**, không reset khi mở PR mới.
- Chuyển trạng thái không hợp lệ bị từ chối.
- Schema state **có version** và **tương thích N-1** (code bản trước đọc được state bản sau, để revert không làm crash).

**R3 — Một writer duy nhất, event idempotent.**
- Mỗi agent chỉ ghi **inbox của chính nó**. Orchestrator là bên **duy nhất** ghi state. Dashboard **không ghi** gì trong MVP (R15).
- Mỗi event mang `{agent, model_id, task, pid}` để giữ provenance "ai làm" (S:L64).
- **Event idempotent** (chống N6):
  - Mỗi event có id `(agent, pid, seq)`, cộng thêm thời điểm khởi động của process để PID tái sử dụng sau reboot không trùng id.
  - Áp lại cùng event không gây thêm hiệu ứng.
  - Offset đã fold được lưu **trong cùng snapshot nguyên tử** với state.
  - Dòng JSONL cuối ghi dở bị bỏ qua và đọc lại ở lần fold sau. Dòng hỏng bị cách ly, không làm kẹt fold.
  - Có **test crash-replay**.
- Ghi nguyên tử bằng write-temp + `os.replace`, **retry có giới hạn** khi gặp `PermissionError`/WinError 5. Thất bại cuối cùng chuyển heartbeat của orchestrator sang **lỗi**, không bao giờ im lặng. Offset chỉ tiến khi ghi thành công.
- Có **test tranh chấp nhiều tiến trình**, chạy trên Windows CI.

**R4 — Audit log (thay R4 bản trước).**
- Append-only, mỗi bản ghi có số thứ tự. Gồm: thời điểm (KST), hành động, kết quả, `agent`, **model thực sự phục vụ**, **vendor**, nguồn của model_id, **commit SHA**, `task_id`, `attempt`, PR nếu có.
- **Không yêu cầu hash chain trong MVP.** Judge bác hash chain vì nó không chặn được giả mạo khi agent cùng OS user (D§8). Tuyên bố tin cậy ở mục 5.2 thay cho nó. Không được mô tả audit log là "chống giả mạo".

**R5 — Ledger.**
- Mỗi lần gọi model là một bản ghi gồm: `task_id`, `attempt`, `agent`, **model thực sự phục vụ** (sau fallback, kèm lý do fallback), **nguồn của model_id** (metadata CLI hay log 9Router), **token dạng delta** (adapter nào báo usage cộng dồn theo phiên thì phải đổi ra delta), chi phí, cờ **`estimated`** (`true` khi tuyến không báo token, ví dụ `agy`).
- Giá lấy từ **`config/prices.yaml`**.
- **Key idempotent `(task, call_id, attempt)`**: retry hay replay không bị đếm trùng.

**R6 — Worktree và lock.**
- Mỗi **task-attempt** một worktree và một nhánh, tên `<task>-<attempt>`, không chia sẻ, nằm dưới một **root ngắn cấu hình được**.
- Lock lưu **PID + thời điểm khởi động của process**. Chỉ dọn lock (kể cả `index.lock`) và worktree mồ côi khi process sở hữu **đã chết**. Không dọn theo tuổi file.
- Tắt `gc.auto` trong lúc agent đang chạy (giữ từ R6 bản trước).
- Không loại trừ thư mục worktree khỏi Windows Defender (D§8).

**R7 — Danh tính merge và bản ghi duyệt (thay R7 bản trước).**
- Agent push bằng **một bot account riêng**. Bot **không** được merge.
- **Trong MVP chỉ người dùng merge, qua GitHub UI.** Approver-Agent chỉ đề xuất. Danh tính merge cho bot hoặc batch job là **tính năng sau** (mục 7).
- Đây là **lệch khỏi S:L12/L35** ("Approver-Agent bấm merge Red"). Người dùng phải ký xác nhận (mục 5.5).
- **Bản ghi duyệt có nguồn sự thật là GitHub review** của người dùng (CODEOWNERS), gắn với SHA. Từ PR-3, `approvals.jsonl` trong thư mục state **phản chiếu** GitHub review. Bản phản chiếu không bao giờ là nguồn sự thật.
- Vì branch protection bật "include administrators", người dùng không tự approve PR của chính mình được. Nên **từ PR-1a, PR phải do bot account mở**.

**R8 — Labeller, tầng Owner-only, cờ auto-merge, kill-switch.**
- Ba tầng nhãn: **Green**, **Red**, **Owner-only**. Owner-only là tầng chỉ người dùng được duyệt và merge. Danh sách khởi đầu đề xuất (chờ người dùng duyệt, mục 8): `config/**`, labeller, gate3, `.github/workflows/ci.yml`, audit, ledger, `release/**`, `schemas/**`, `CODEOWNERS`.
- Labeller và `config/sensitive_paths.yaml` **luôn chạy và đọc từ base ref**, không bao giờ từ PR head.
- **Green chỉ qua allowlist.** File không khớp allowlist thì mặc định Red.
- So khớp sau khi **casefold** và chuẩn hóa dấu `/`. Xét cả tên cũ, tên mới khi đổi tên, và file bị xóa.
- Luôn là Red (hoặc Owner-only): symlink, submodule, `.gitattributes`, `conftest.py`, `.pth`, `sitecustomize`, `pyproject.toml`, xóa test.
- Gắn nhãn lại **tại SHA được merge**.
- **Cờ auto-merge: mặc định tắt.** Trong MVP không có auto-merge thật; PR-4a chỉ chạy shadow evaluator.
- **Kill-switch chuyển sang PR-4a**, kiểu **fail-closed**: file thiếu hoặc không đọc được thì coi như kill-switch đang bật, mọi hành động tự động dừng. Chỉ người dùng đổi, không qua dashboard. Theo mục 5.2, đây là kiểm soát best-effort.
- Cài đặt labeller ở PR-3.

**R9 — Merge trên SHA đã kiểm.**
- Nhãn, CI và Gate 3 gắn với **đúng SHA được merge**. Head SHA đổi thì phải kiểm lại.
- Trong MVP, người dùng merge qua GitHub UI. Branch protection (R14) bảo đảm CI qua trên SHA đó; verdict Gate 3 phải ghi đúng SHA đó.
- Batch nhiều PR (PR-4b): merge tuần tự, chạy CI trên **kết quả tích hợp**. Batch job tự kiểm tra tích hợp, **không phụ thuộc merge queue native** của GitHub. Phê duyệt gắn `(head SHA, patch-id)`, chỉ còn hiệu lực khi rebase không conflict, có hiện range-diff.

**R10 — Revert và manifest release.**
- Revert bằng **`git revert` từng PR, theo thứ tự ngược manifest**, qua PR. **Không force-push, không reset.** Các commit merge sau release vẫn được giữ.
- Manifest lấy từ **`git log prevTag..newTag`**, không lấy từ danh sách tự ghi.
- Revert conflict thì **đóng băng và báo động**, không tự giải conflict.
- Giữ `release.lock` suốt bước kiểm tra. State tương thích N-1 (R2) để code cũ chạy được sau revert.
- MVP (PR-4a): quy trình revert và incident **thủ công** theo runbook. Revert tự động là PR-4b.

**R11 — Fallback không được phá Gate 3.**
- **Writer set** là tập model **thực sự phục vụ** mọi lời gọi viết code của task. Reviewer phải khác mọi model trong writer set. **Model không rõ thì coi là xung đột.**
- Red và Owner-only: 1 reviewer **khác vendor**. Green: khác model là đủ (mặc định đề xuất, mục 8).
- model_id lấy từ **metadata tầng transport hoặc log 9Router** (join theo request id), **không bao giờ** từ text do model tự in ra (chống N4). Có bảng alias 9Router → `(model_id, vendor)`; alias không rõ thì không được làm reviewer.
- Không còn reviewer hợp lệ thì task sang `escalated`, báo người dùng; không merge, không hạ chuẩn.
- Fallback chỉ trong chuỗi Opus (mục 4). Không có fallback tự chế cho `dsh`/`agy`, không có `fixer_proxy`.
- `NotAvailable` (stub Codex/Cursor) là lỗi **terminal**, không retry, không fallback.

**R12 — Phạm vi MVP (thay R12 bản trước).**
- MVP gồm **PR-0, PR-1a, PR-1b, PR-2, PR-3, PR-4a, PR-5**.
- **PR-4b** (scheduler, digest, batch release, revert tự động) và **PR-4c** (maintenance P0–P3) là **tính năng sau theo mục 7**. Bỏ cách hiểu "PR-4 đi trước lộ trình mục 7 có chủ đích" của bản trước.
- Tách PR-1: **PR-1a** = schemas, state store, event fold, ledger, audit log. **PR-1b** = worktree, lock, heartbeat, inbox. Kill-switch ở **PR-4a**.

**R13 — Runtime state ở ngoài repo.**
- Runtime state nằm ở **đường dẫn cấu hình được, ngoài repo**. Mặc định `%LOCALAPPDATA%\harness` (trên máy này: `C:\Users\water\AppData\Local\harness`). Đổi được khi tách OS user (`%LOCALAPPDATA%` theo từng user nên sẽ phải đổi).
- Git chỉ chứa **schemas và fixtures**. Không có `state/tasks.json`, `state/audit.log.jsonl` hay bất kỳ file runtime state nào trong repo.

**R14 — Branch protection và CI trước PR-1a.**
- **Branch protection trên `main` phải bật trước khi PR-1a được merge:**
  - Bắt buộc mở PR.
  - Cần 1 approving review từ CODEOWNERS (người dùng).
  - Required check `ci`, nguồn bắt buộc là GitHub Actions (chặn commit status giả theo N1).
  - Cấm force-push, cấm xóa nhánh.
  - Bật "include administrators".
  - Phải xác nhận gói GitHub của repo hỗ trợ các thiết lập này. Nếu không hỗ trợ: bot làm việc từ **fork không có quyền write**.
- **PR-0** thêm `ci.yml` tối thiểu (lint + test), **chỉ** chạy trên GitHub-hosted **`windows-latest`**. **Cấm self-hosted runner** trên máy điều khiển.
- Bước lập kế hoạch hiện tại **không tạo** `ci.yml`. File đó chỉ được tạo trong PR-0.

**R15 — Tuyên bố tin cậy.** Xem mục 5.2. Mọi PR phải giữ đúng tuyên bố này cho tới khi tách OS user.

**R16 — Con số là mặc định tạm.**
- N vòng fix, ngân sách, timeout heartbeat, giới hạn kích thước, các cutoff giờ, ngưỡng shadow mode, thông số circuit breaker... đều là **mặc định tạm trong config**, không phải yêu cầu. Chỉnh theo dữ liệu thật.
- **MVP dùng 1 reviewer Gate 3**, không phải 2. Hai reviewer song song là dual review của mục 7.

**R17 — Windows.**
- `tzdata` là phụ thuộc bắt buộc (`zoneinfo` trên Windows cần nó cho `Asia/Seoul`).
- **Mọi tiêu chí chấp nhận** đều được kiểm bằng test chạy trên Windows CI (`windows-latest`).

### 5.4. Đối chiếu R1–R12 bản trước

| R | Bản trước | Bây giờ | Căn cứ |
|---|---|---|---|
| R1 | Gate 3 thủ công PR-1..PR-3, ghi trong mô tả PR | **Sửa:** thêm PR-0, ghi thành PR comment, khác vendor, cấm CI dán tay, người dùng merge tay | D§6 #5 |
| R2 | 13 bước + blocked/superseded/cancelled/awaiting-human, lineage | **Sửa:** thêm `escalated`, `attempt`, schema có version N-1; giữ `awaiting-human` | D§6 #12 |
| R3 | Single-writer, agent và dashboard ghi inbox | **Sửa:** dashboard không ghi; event idempotent; retry có giới hạn → heartbeat lỗi | D§6 #10, #11 |
| R4 | Hash chain, dừng khi chuỗi đứt | **Thay:** bỏ hash chain bắt buộc; best-effort, tuyên bố tin cậy | D§6 #6, D§8 |
| R5 | Idempotency key, model thực tế, `estimated` | **Sửa:** thêm attempt, nguồn model_id, token delta, `prices.yaml`, key `(task, call_id, attempt)` | D§6 #13 |
| R6 | Worktree theo task, dọn mồ côi khi khởi động | **Sửa:** theo task-attempt; lock PID + start time; chỉ dọn khi process chết; giữ tắt `gc.auto` | D§6 #14 |
| R7 | Danh tính merge riêng; Approver merge Red khi có bản ghi duyệt | **Thay:** bot account push; chỉ người dùng merge qua GitHub UI; GitHub review là nguồn sự thật | D§6 #2 |
| R8 | Labeller từ base, cờ và kill-switch trong state | **Sửa:** thêm Owner-only, allowlist Green, casefold, kill-switch fail-closed ở PR-4a | D§6 #8, D§7 #4, #8 |
| R9 | Kiểm lại trên kết quả merge, kiểu merge queue | **Sửa:** gắn SHA; batch tự kiểm tích hợp, không phụ thuộc merge queue native | D§2.1 #1.1, #1.5; D§7 #12 |
| R10 | Revert ngược, manifest trong state PR-1 | **Sửa:** manifest từ `git log`, conflict thì đóng băng; thủ công ở 4a | D§7 #9, #11 |
| R11 | So model/vendor thực tế, alias, chờ | **Sửa:** writer set, model không rõ là xung đột, model_id từ transport, `NotAvailable` terminal | D§4 #3, #7; D§7 #1, #2 |
| R12 | PR-4 làm scheduler/digest/P0–P3 trước lộ trình | **Thay:** MVP không gồm 4b/4c | D§6 #1 |
| R13–R17 | — | **Mới** | D§6 #3, #4, #6, #7, #9, #14 |

### 5.5. Lệch SPEC gốc, cần người dùng ký

| Lệch | SPEC gốc | Bây giờ |
|---|---|---|
| Ai merge PR Red | Approver-Agent bấm merge sau digest (S:L12, L35) | Trong MVP chỉ người dùng merge qua GitHub UI; Approver chỉ đề xuất |
| Tầng Owner-only | Chỉ có Red/Green | Thêm Owner-only cho file kiểm soát chính hệ thống |
| Trạng thái mở rộng | Chỉ 13 bước | Thêm `escalated`, `blocked`, `superseded`, `cancelled`, `awaiting-human` |

## 6. Artifact MVP

1. **Task kanban:** các cột theo trạng thái quy trình, gồm cả `escalated`, `blocked`, `superseded`, `cancelled`, `awaiting-human`.
2. **PR queue:** nhãn Red / Owner-only / Green và trạng thái CI.
3. **Approval queue:** spec và PR đang chờ người dùng duyệt, kèm link sang GitHub (duyệt diễn ra trên GitHub, không trên dashboard).
4. **Agent status:** trạng thái và heartbeat của từng agent.
5. **Logs:** xem audit log và log của từng agent.
6. **Token cost theo task:** lấy từ cost ledger.

Quy tắc cho dashboard trong MVP:
- **Chỉ đọc.** Không nút duyệt, không nút kill-switch, không nút cờ auto-merge, không endpoint ghi.
- Mỗi view có huy hiệu provenance: file nguồn, số bản ghi, `as_of` = **thời điểm fold cuối cùng** (không phải giờ hiện tại).
- "Không có dữ liệu" phải hiển thị khác "0".
- Dữ liệu giả chỉ có trong fixture test. Watermark "FAKE" lấy từ trường dữ liệu `source: "fixture"`, không từ CSS.
- Có contract test theo schema (ví dụ: tổng chi phí trên dashboard = tổng ledger).
- Thao tác ghi chỉ được thêm sau khi tách OS user **và** đã xác minh cách Muse Artifact chạy (đọc file cục bộ hay cần server).

## 7. Tính năng sau

- **Điều kiện tiên quyết cho mọi mục dưới đây có merge tự động hoặc ghi từ dashboard:** tách một Windows user riêng, ít quyền cho agent, có ACL trên thư mục state.
- **PR-4b:** scheduled jobs, digest 11:30 và 16:30, batch release 12:00 và 17:00, revert tự động, danh tính merge của bot/batch job.
- **PR-4c:** maintenance scan 08:00 và hàng đợi P0–P3.
- Bật auto-merge thật (sau khi chốt mục 8 và có dữ liệu shadow mode).
- Thao tác ghi từ dashboard.
- So sánh dual review (hai reviewer song song).
- Replay task bị lỗi.

## 8. OPEN DISCUSSION FOR PR

> 📌 **Quyết định SPLIT (người dùng đã duyệt, 2026-10-25):**
> 1. **Mục 9 được DUYỆT** với vai trò checklist chỉ để tham khảo: đo sau MVP, rồi giữ hoặc thay theo quy tắc 9.5. Mục 9 **không ràng buộc code**.
> 2. **4 điều kiện auto-merge KHÔNG được duyệt** là nội dung cuối. Vẫn là **OPEN DISCUSSION**, hoãn đến trước khi PR-4a bật auto-merge, và sẽ quyết định dựa trên dữ liệu shadow evaluator của PR-4a. Danh sách 4 ứng viên hiện tại ở mục 8 chỉ là **bản nháp thảo luận**, không phải quyết định đã duyệt. Trong MVP auto-merge vẫn **TẮT**.

> ⚠️ **4 điều kiện auto-merge cho PR Green CHƯA được chốt.** Phần này dùng để thảo luận trong PR, chưa phải quyết định cuối. Trong MVP auto-merge tắt; PR-4a chỉ chạy shadow evaluator.

**Ứng viên điều kiện:**

1. CI xanh (lint, test, build đều pass).
2. Gate 3 đã đạt.
3. Không có nhãn Red/Owner-only và không chạm file nhạy cảm.
4. Không còn comment chưa xử lý.

**Câu hỏi cho người dùng** (từ D§9; mỗi câu kèm mặc định đề xuất, người dùng quyết định cuối):

| # | Câu hỏi | Mặc định đề xuất |
|---|---|---|
| 1 | Tạo bot account riêng cho agent? Ai giữ quyền merge? | **Có**, kèm CODEOWNERS bắt buộc review. Gói GitHub không hỗ trợ thì bot làm việc từ fork |
| 2 | Approver-Agent có được tự merge PR Red không thuộc Owner-only? (S:L12 so với S:L16) | **Không** trong MVP. Xem lại khi có dữ liệu shadow mode |
| 3 | Fixer: thủ công đến khi có Cursor, hay proxy tạm bằng agent khác? | **Thủ công** |
| 4 | P0 có được release ngoài cửa sổ (hotfix lane)? | **Không**, giữ S:L42; bug do release đã có bước 12 |
| 5 | Khi 9Router/bridge sập: chờ, hay fallback sang Claude có trần chi phí? | **Chờ và thông báo** |
| 6 | Làm PR-4b/4c ngay không? | **Không**, hoãn theo mục 7 |
| 7 | Chấp nhận Windows user riêng, ít quyền cho agent? | **Có**, trước PR-4b và trước mọi thao tác ghi từ dashboard |
| 8 | Auto-merge (khi bật) chạy ngay hay gom vào batch 12:00 / 17:00? | **Gom vào batch** |
| 9 | Duyệt danh sách Owner-only ban đầu (R8)? | **Đồng ý** |
| 10 | Quy tắc vendor của Gate 3? | Red và Owner-only: 1 reviewer khác vendor. Green: khác model là đủ |
| 11 | Comment do bot/agent tạo có tính là "chưa xử lý"? | Review thread chưa resolve thì tính, bất kể ai viết; comment chỉ mang tính thông tin thì không (mặc định của Judge, chưa tranh luận) |
| 12 | Allowlist Green hẹp (`docs/**` + test thêm mới) làm gần như mọi PR là Red. Green có cần "dùng được" không? | Giữ hẹp trong MVP, theo dõi số lần duyệt mỗi ngày, nới dần theo dữ liệu |
| 13 | Muse Artifact đọc file cục bộ hay chạy server? | Xác minh trước PR-5; trong lúc chờ, dashboard chỉ đọc |
| 14 | "Kiểm tra read-only" ở bước 11 gồm những gì? "Release" ra đâu với control plane cục bộ? | Định nghĩa trước khi merge PR-4a (ví dụ smoke test + health check + so sánh lỗi trong log) |
| 15 | Giới hạn N vòng fix và timeout heartbeat? | Mặc định tạm: N = 2, ngân sách cộng dồn theo task; timeout heartbeat cấu hình được, chỉnh theo dữ liệu thật |

**Câu hỏi còn lại:**

- Điều kiện 3 có trùng với chính quy tắc gắn nhãn không? Nếu trùng, thay bằng điều kiện khác (giới hạn diff, bắt buộc có test mới)?
- PR Green sinh ra từ vòng Fixer có cần thêm điều kiện không?
- Có đặt trần ngân sách token theo task và theo ngày không, mức bao nhiêu?
- Mục nào của checklist mục 9 sẽ được đo để giữ hay thay?

**Mặc định tạm trong config (R16) — KHÔNG PHẢI YÊU CẦU, chỉnh theo dữ liệu thật:**

| Chủ đề | Mặc định tạm |
|---|---|
| Thay điều kiện 3 bằng giới hạn diff | ≤200 dòng / ≤10 file, có test khi sửa src |
| Số vòng fix tối đa | N = 2 |
| Ngân sách token cho vòng fix | 3× chi phí gốc, cộng dồn theo `task_id` |
| Số PR tối đa mỗi batch (PR-4b) | 5 |
| Cảnh báo fix list chờ quá lâu | 4 giờ |
| Hạn duyệt PR Red trước batch (PR-4b) | 11:55 / 16:55 |
| Shadow mode trước khi bật auto-merge | 2 tuần hoặc 20 PR |
| Độ dài bàn giao | ≤2 KB, kèm link artifact (path + SHA) |
| Circuit breaker bridge/9Router | mở sau 3 lỗi, giữ 5 phút; probe bằng completion thật, timeout 2 giây |
| Banner dữ liệu cũ trên dashboard | `as_of` cũ hơn 5 phút |
| Timeout heartbeat | chọn trong PR-1b, ghi trong config |

## 9. CHECKLIST THAM KHẢO CHO PR — KHÔNG PHẢI CHUẨN BẮT BUỘC

> ⚠️ **Đọc trước khi dùng:** Phần này chỉ là checklist tham khảo để khi review PR thì đem ra
> đối chiếu và so sánh. **Không lấy các mục dưới đây làm gốc để bám vào hoàn toàn.**
> Nếu PR tìm được cách tốt hơn (ít token hơn, nhanh hơn, tin cậy hơn, dễ bảo trì hơn),
> thì thay checklist này bằng cách tốt hơn đó, và ghi rõ lý do thay.
> Checklist này không thay thế quy trình ở mục 3, định tuyến model ở mục 4,
> Gate 3, nhãn Red/Green, 4 điều kiện auto-merge ở mục 8, hay quy tắc bắt buộc R1–R17 ở mục 5.3.

**Nguồn tham khảo (tổng hợp, không phải bằng chứng duy nhất):**

- Video P1, Thân Công Hải, nhóm comailo, đăng 2026-10-19:
  https://facebook.com/groups/comailo/permalink/1009913868809293/
  (vận hành phòng 20 agent Codex + Claude, mẹo không lệch spec, ít tốn token, giao tiếp).
- Bài đăng hệ thống chat nối 4 máy tính, tác giả “lướt fb dạo”, nhóm Antigravity VN,
  đăng 2026-10-18, cùng bình luận và ảnh chụp bình luận ngày 2026-10-25:
  https://facebook.com/groups/945603251671521/permalink/1127183623513482/
  (định tuyến theo loại việc, đồng bộ log qua Google Drive, bàn giao bằng tóm tắt markdown,
  không móc CLI trực tiếp để trả lời).
- Lưu ý về nguồn: bản ghi video là bản ghi tự động nên có đoạn nhiễu; một số bình luận
  (ví dụ nhận xét Antigravity tốn token) là trải nghiệm của người bình luận, có người
  tự nói là chưa đo nguyên nhân. Dùng các mục dưới đây như giả thuyết cần kiểm chứng,
  không phải kết luận đã đo đạc.

### 9.1. Giao tiếp giữa các agent (đối chiếu, không bắt buộc)

- [ ] Agent mới có mở phiên mới, thay vì nạp lại toàn bộ log/session cũ không?
- [ ] Bàn giao có phải là tóm tắt markdown ngắn không: đã làm gì, đổi file nào,
      kết quả ra sao, lỗi còn lại là gì, việc tiếp theo là gì — thay vì gửi nguyên
      đoạn chat/code dài?
- [ ] Có mẫu bàn giao cố định và giới hạn độ dài không, để tránh kiểu bàn giao
      dài dòng mặc định?
- [ ] Nếu agent nhận không hiểu, nó có hỏi lại agent cũ qua kênh chat/hub không,
      thay vì người phải copy-paste nguyên log từ máy này sang máy kia?
- [ ] Log/file có được đồng bộ qua thư mục chung không (ví dụ thư mục chia sẻ /
      Google Drive), nhưng agent chỉ đọc phần cần thiết, không đọc lại tất cả?
- [ ] Có tránh nối trực tiếp output của CLI này sang input của CLI kia để trả lời
      không, vì cách đó có thể khiến agent phải nạp lại toàn bộ ngữ cảnh và tốn quota?
- [ ] Nếu PR đề xuất đồng bộ realtime thay cho “tóm tắt rồi đọc lại”, đã so sánh
      quota/token, độ tin cậy và độ phức tạp chưa? (Hiện tại, theo nguồn trên,
      đồng bộ realtime vẫn là thứ người trong luồng bình luận đang tìm, chưa phải
      cách đã chứng minh xong.)

### 9.2. Tổ chức công việc và workspace (đối chiếu, không bắt buộc)

- [ ] Có thư mục chung mà mọi agent đều đọc được (nội quy, quy trình, mẫu giao tiếp)
      và workspace riêng cho từng agent không?
- [ ] Mỗi agent có làm việc riêng, chia nhỏ theo từng mục, rồi chuyển qua bước
      ghép/merge, rồi chạy thử không?
- [ ] Việc có được giao theo luồng A–Z (từ nghiên cứu đến hoàn thành) không,
      thay vì giao từng việc nhỏ lẻ khiến agent lặp đi lặp lại cùng một lỗi?
- [ ] Có dùng chéo không: agent/model khác kiểm tra lại công việc, reviewer khác
      người viết (đã có Gate 3 ở mục 3 — checklist này chỉ để đối chiếu, không
      thay Gate 3)?

### 9.3. Tiết kiệm token/quota và định tuyến (đối chiếu, không bắt buộc)

- [ ] Việc nhỏ / hỏi linh tinh có đi kênh rẻ hơn (ví dụ API miễn phí) không, còn
      việc code mới có đi đúng máy/agent được chỉ đạo không?
- [ ] Các máy/agent khác có chỉ kiểm tra theo thứ tự sau khi có báo cáo không,
      thay vì cùng chạy lại toàn bộ từ đầu?
- [ ] Code đã xác nhận chạy không vấn đề mới được đẩy về máy chuyên chạy code /
      bước release tiếp theo?
- [ ] Định tuyến model ở mục 4 có còn khớp không, hay PR có phương án định tuyến
      tốt hơn theo độ khó và mức ảnh hưởng?
- [ ] Khi PR kết luận một công cụ/model “tốn token hơn”, đã có số đo thực tế chưa,
      hay mới chỉ là cảm nhận? (Ví dụ trong nguồn: có người báo cùng một harness
      chạy bình thường với nhiều công cụ khác nhưng tốn token với Antigravity,
      nhưng chính người đó cũng nói chưa đo nguyên nhân.)

### 9.4. Chất lượng: chống lệch spec và chống “nhìn như xong” (đối chiếu, không bắt buộc)

- [ ] Không chỉ nhìn giao diện chạy được: đã kiểm tra backend/module và test
      thực tế chưa? (Nguồn cảnh báo kiểu output frontend đủ nhưng backend thiếu.)
- [ ] Với task phức tạp, PR có bằng chứng test không, thay vì chỉ demo?
      (Nguồn cảnh báo: dù đã có harness và lập kế hoạch, task phức tạp vẫn có thể
      tốn thời gian, tốn token, nhảy từ lỗi này sang lỗi khác và sai kỳ vọng.)
- [ ] Đánh giá có dựa vào “task phức tạp hoàn thành đúng kỳ vọng” không, thay vì
      chỉ dựa vào “đã nối được chat / đã kết nối được các máy”?
- [ ] Ưu tiên có đúng thứ tự không: người dùng nói thì agent hiểu và tính năng
      đúng trước, làm đẹp giao diện sau?

### 9.5. Khi PR đề xuất thứ tốt hơn — quy tắc so sánh (bắt buộc ghi rõ)

- [ ] PR ghi rõ nó thay checklist nào ở mục 9, và vì sao cách mới tốt hơn
      (token/quota, tốc độ, độ tin cậy, dễ bảo trì)?
- [ ] Có số đo hoặc test so sánh không, thay vì chỉ cảm nhận?
- [ ] Cách mới có ảnh hưởng gì tới Gate 3, nhãn Red/Green, auto-merge,
      cost ledger và audit log không? Đã cập nhật các mục liên quan chưa?
- [ ] Nếu cách mới không tốt hơn, có giữ nguyên checklist tham khảo không,
      thay vì đổi chỉ vì nó mới?
- [ ] Sau khi có bằng chứng tốt hơn, checklist ở mục 9 có được cập nhật hoặc
      loại bỏ mục cũ không, thay vì bám cứng vào mục cũ?

**Kết thúc phần tham khảo.** Các mục trên không phải tiêu chí pass/fail mặc định
cho PR, trừ khi PR đó tự chọn dùng chúng làm tiêu chí và ghi rõ trong PR.
