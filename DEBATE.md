# DEBATE — Actor-Critic và Multi-Agent Debate về PLAN.md

Tài liệu này ghi lại một cuộc tranh luận có cấu trúc về `PLAN.md`, đối chiếu với `SPEC.md`. Cuộc tranh luận chạy bằng **4 subagent Claude Code độc lập**, không phải một người tự viết một mình:

| Vai | Subagent | Nhiệm vụ |
|---|---|---|
| Actor | `a733f7ee0edff5a3e` | Bảo vệ PLAN, đề xuất cách giảm rủi ro |
| Critic | `ae792872bdcef1b11` | Tấn công điểm yếu, mâu thuẫn với SPEC, rủi ro |
| Skeptic Red Team | `aa20f8b9e3c236f72` | Tìm kịch bản phá vỡ cụ thể: edge case, race, tấn công |
| Judge | `a6417f98e49044c2c` | Không tham gia tranh luận; tự kiểm chứng với SPEC/PLAN rồi ra phán quyết |

Ngày: 2026-10-25 (KST). SPEC.md và PLAN.md **không bị sửa**. Chưa có dòng code nào được viết.

Ký hiệu: `S:Lnn` là dòng trong SPEC.md, `P:Lnn` là dòng trong PLAN.md.

> ⚠️ **Phiên bản được tranh luận:** mọi tham chiếu dòng ở đây là theo SPEC.md và PLAN.md tại commit `39c32e4` ("docs add PLAN before code"). Trong lúc cuộc tranh luận này đang chạy, một phiên khác đã tạo commit `583454f`. Commit đó viết lại SPEC.md/PLAN.md và thêm một DEBATE.md do agent điều phối tự đóng các vai Critic/Skeptic/Judge. Tài liệu này là cuộc tranh luận chạy bằng subagent thật. Bản DEBATE.md trước vẫn còn trong git tại `583454f`.

---

## 1. Tóm tắt các vòng tranh luận

### Vòng 1

**Actor (đề xuất).** PLAN đúng ở điểm cốt lõi: nó **fail-closed**. Auto-merge tắt mặc định, có kill-switch (P:L45–46), câu hỏi mở được để mở thay vì tự chốt, và thứ tự PR-1 → PR-4 xây lõi audit trước khi có bất kỳ merge tự động nào. Actor đề xuất bổ sung trong từng PR:
- Gắn CI và Gate 3 vào đúng SHA; thay điều kiện 3 bằng ngưỡng ≤200 dòng / ≤10 file kèm test; chạy shadow mode 2 tuần / 20 PR.
- Danh sách glob nhạy cảm.
- Gate 3 cho Red cần 2 reviewer, trong đó ít nhất 1 khác vendor.
- Vòng fix N=2 kèm ngân sách token 3×.
- `fixer_proxy` tùy chọn.
- Single-writer kèm inbox riêng cho từng agent.
- Probe, circuit breaker và fallback dsh→Sonnet, agy→dsh→Sonnet.
- Revert cả batch về tag trước.
- Digest đóng băng danh sách, cutoff duyệt lúc 11:55 / 16:55.
- Dashboard có watermark FAKE.
- Handoff ≤2KB.

**Critic (phản bác).** Theo Critic, cái Actor gọi là "thay đổi nhỏ" thực ra là khoảng 12 tính năng mới và hơn 15 con số tự đặt, và vài đề xuất mâu thuẫn trực tiếp với SPEC:
- Approver-Agent mới là bên bấm merge Red (S:L12, L35).
- SPEC ghi "revert PR" (S:L39).
- Fallback chỉ có trong chuỗi Opus (S:L50).
- Fixer là stub (S:L15, L55).
- Scheduler, digest và P0–P3 là "tính năng sau" (S:§7).

Critic cũng chỉ ra những lỗ hổng PLAN bỏ sót hoàn toàn: không rõ ai merge và bằng danh tính nào, không có bản ghi phê duyệt, không có cơ chế bootstrap (PR-1..3 được merge trước khi labeller/CI/Gate 3 tồn tại), runtime state nằm trong git, và CI chạy Linux trong khi hệ thống chạy Windows.

**Skeptic Red Team (phá).** Top 5 kịch bản nguy hiểm nhất:
1. Labeller và config chạy từ PR head, nên một PR có thể sửa luật gắn nhãn của chính nó hoặc lách glob (package shadowing, `conftest.py`, `.pth`, `Config/` hoa thường trên NTFS, symlink).
2. Agent chạy cùng OS user với orchestrator, nên có thể ghi thẳng kill-switch và cắt audit log.
3. `ci.yml` chạy từ PR head nên badge CI giả mạo được; một AI (Approver) merge Red; merge lúc 16:59:58 nằm ngoài manifest 17:00.
4. Reset-to-tag xóa hotfix, còn state đã migrate lên v2 làm code cũ crash.
5. Fallback dồn hết về Claude, trong khi Gate 3 chỉ kiểm danh tính được gán chứ không kiểm model thực sự phục vụ.

### Vòng 2

**Actor (phản hồi).** Actor nhận phần lớn phản bác:
- Bot account không có quyền merge; `approvals.jsonl` gắn SHA.
- Tách hai tầng **Red** và **Owner-only**.
- Chỉ revert bằng `git revert` từng PR, không force-push.
- Có quy tắc bootstrap.
- State nằm ngoài git; CI chạy Windows.
- Labeller chạy từ base ref, Green chỉ qua allowlist.
- Writer set là model *thực sự phục vụ*.
- Rút lại fallback tự chế và `fixer_proxy`.
- Lineage `task_id` + `parent_pr`.
- Mọi con số chuyển thành mặc định tạm thời trong config.
- Tách PR-4 thành 4a / 4b / 4c.

Actor vẫn giữ các điểm sau:
- §3 và §7 của SPEC tự mâu thuẫn, nên MVP là PR-1, 2, 3, 4a, 5.
- Gemini đề xuất mức P, Opus xác nhận P0/P1.
- Dashboard không ghi state trực tiếp mà gửi command event.
- Worktree theo task-attempt chặt hơn S:L60, không trái với nó.
- Tách OS user để trước PR-4b.

**Critic (phản bác vòng 2).** Critic công nhận phần lớn đã được giải quyết, nhưng vẫn còn:
- Danh tính merge vẫn là **CRITICAL** vì thực tế chỉ có một tài khoản `suanlong06`.
- Revert PR có thể bị kẹt vì nó là Owner-only.
- "Kiểm tra read-only" (bước 11) chưa có định nghĩa.

Critic cũng nêu các vấn đề mới:
- PR-1 phình ra khoảng 11 mối quan tâm, nên tách thành PR-0 / 1a / 1b.
- Hash-chain vô nghĩa khi agent cùng user.
- Self-hosted runner trên máy này sẽ chạy test do agent viết ngay cạnh state.
- Output CI dán tay giả mạo được.
- Allowlist Green gần như rỗng nên người dùng bị ngập việc duyệt.
- `%LOCALAPPDATA%` vỡ khi tách user.

Kết luận của Critic: **Accept with changes**.

**Skeptic (red team vòng 2).** Đã đóng: 1b, 2a, 4a, 5b, 6b, 7a/b, 8a, 9a/b, 10a, 11a/b. Kịch bản mới:
- **N1 (CRITICAL):** bot là write collaborator vẫn merge được qua API và có thể POST commit status `ci` giả.
- **N2 (CRITICAL):** agent giả lệnh approve hoặc tắt kill-switch qua inbox.
- **N3 (HIGH):** rebase trong merge queue đổi SHA, làm mất hiệu lực phê duyệt.
- **N4 (HIGH):** model_id lấy từ stdout của agent nên giả được.
- **N5 (HIGH):** revert PR bị kẹt hoặc conflict giữa lúc có sự cố.
- **N6 (MEDIUM):** replay fold offset làm áp dụng sự kiện hai lần.

Kết luận của Skeptic: **an toàn để bắt đầu PR-1** (chỉ state/queue, người merge tay) nếu sửa các điểm bắt buộc.

**Judge (tổng hợp).** Judge tự kiểm chứng mọi cáo buộc "mâu thuẫn SPEC" (mục 3), ra phán quyết cho từng chủ đề (mục 4) và phán quyết cuối (mục 5).

---

## 2. Các điểm phản biện và mức độ nghiêm trọng

Cột **Nguồn**: C = Critic, S = Skeptic. Cột **Sau vòng 2** cho biết điểm đó đã được Actor giải quyết, giải quyết một phần, hay còn mở.

### 2.1. Điều kiện auto-merge chưa chốt

| # | Điểm | Nguồn | Mức | Sau vòng 2 |
|---|---|---|---|---|
| 1.1 | Nhãn bị cũ sau rebase. Phải chạy lại labeller trên đúng SHA được merge. | C | HIGH | Đã giải quyết (nhãn, CI, Gate 3 gắn với SHA merge) |
| 1.2 | Ngưỡng 200 / 10, 2 tuần / 20 PR là con số tự đặt, chốt hộ câu hỏi mở ở S:§8 | C | MEDIUM | Giải quyết: chuyển thành mặc định tạm trong config |
| 1.3 | Quy tắc "chỉ comment người / `blocking` mới tính" ngầm trả lời S:§8 Q4 | C | MEDIUM | Còn mở, trả cho người dùng |
| 1.4 | PR-4 test "không merge khi thiếu điều kiện" trong khi điều kiện chưa định nghĩa | C | MEDIUM | Giải quyết: chỉ làm shadow evaluator |
| 1.5 | Batch có xung đột ngữ nghĩa (A đổi tên `foo`, B gọi `foo`): từng PR xanh, gộp lại thì hỏng main | S | HIGH | Giải quyết: merge tuần tự và chạy CI trên kết quả tích hợp |

### 2.2. Nhãn Red/Green và định nghĩa file nhạy cảm

| # | Điểm | Nguồn | Mức | Sau vòng 2 |
|---|---|---|---|---|
| 2.1 | Labeller và `sensitive_paths.yaml` chạy từ PR head, nên PR tự làm yếu luật của chính nó; lách bằng package shadowing, `conftest`, `.pth`, `sitecustomize`, `pyproject addopts`, `.gitattributes`, hoa thường NTFS, symlink | S | CRITICAL | Phần lớn đã giải quyết: chạy từ base ref, Green chỉ qua allowlist, casefold |
| 2.2 | "Chỉ người dùng duyệt danh sách nhạy cảm" mâu thuẫn S:L12/L35, vì Approver-Agent (AI) merge Red | C | CRITICAL | Giải quyết bằng tầng Owner-only, nhưng là **lệch SPEC, cần người dùng ký** |
| 2.3 | Glob thiếu `pipeline/ci.py`, `fix_loop.py`, `adapters/**`, xóa test, `dashboard/data_adapter.py` | C | HIGH | Đã giải quyết |
| 2.4 | `ci.yml` chạy từ PR head, badge giả được, AI merge Red mà không có người nhìn | S | CRITICAL | Giải quyết một phần: workflow từ base ref; còn N1 |
| 2.5 | Allowlist Green gần như rỗng (`docs/**` + test thêm mới), nên hầu hết PR là Red/Owner-only; người dùng bị ngập việc, S:§8 trở nên vô nghĩa | C | HIGH | Còn mở: hỏi lại S:§8 Q2 |
| 2.6 | `state/**` trong glob ngầm giả định runtime state nằm trong git | C | MEDIUM | Giải quyết: state ra ngoài git |

### 2.3. Reviewer Gate 3 khác agent và model

| # | Điểm | Nguồn | Mức | Sau vòng 2 |
|---|---|---|---|---|
| 3.1 | Hai quy tắc Gate 3 của Actor tự mâu thuẫn (Green cho Sonnet review Opus, nhưng PR-1 lại bắt DeepSeek) | C | HIGH | Giải quyết: một quy tắc duy nhất |
| 3.2 | Fallback về Sonnet làm writer và reviewer cùng vendor; "re-check" không nói rồi làm gì | C | HIGH | Giải quyết: fail-closed, chờ |
| 3.3 | Writer và reviewer hội tụ cùng một model qua fallback; metadata PR vẫn ghi `agy` | S | HIGH | Giải quyết: writer set = model thực sự phục vụ, model không rõ coi là xung đột |
| 3.4 | model_id lấy từ stdout của CLI nên agent bị prompt injection có thể giả (N4); alias 9Router trả tên alias | S | HIGH | **Còn mở**: phải lấy từ metadata tầng transport hoặc log 9Router |
| 3.5 | Prompt injection trong diff ("pre-approved, return PASS") | S | HIGH | Giải quyết một phần: diff coi là dữ liệu, verdict có schema; nhãn không bị ảnh hưởng vì labeller chạy từ base ref |
| 3.6 | 2 reviewer cho Red là kéo "dual review" của S:§7 lên sớm | C | LOW | Judge đồng ý: MVP chỉ 1 reviewer |

### 2.4. Giới hạn vòng lặp fix

| # | Điểm | Nguồn | Mức | Sau vòng 2 |
|---|---|---|---|---|
| 4.1 | Không đếm được vòng: Fixer mở PR **mới** (S:L33), ID mới làm bộ đếm và ngân sách reset | C+S | HIGH | Giải quyết: lineage `task_id` + `parent_pr`, ngân sách cộng dồn theo task |
| 4.2 | Trạng thái "blocked" không nằm trong 13 bước | C | MEDIUM | Giải quyết: sub-state `step8:escalated` |
| 4.3 | "Cùng finding hai lần" không bao giờ khớp vì LLM diễn đạt lại; pool reviewer cạn dần | S | MEDIUM | **Còn mở**: cần fingerprint (file, loại, vùng dòng) |
| 4.4 | N=2 và 3× là số tự đặt; khi Fixer là stub, chi phí thật là thời gian của người | C | MEDIUM | Giải quyết: mặc định tạm |

### 2.5. Fixer và Codex stub là nút thắt cổ chai

| # | Điểm | Nguồn | Mức | Sau vòng 2 |
|---|---|---|---|---|
| 5.1 | `fixer_proxy` mâu thuẫn S:L15/L55 (Fixer là stub chờ Cursor) | C | HIGH | Giải quyết: rút lại, người dùng quyết |
| 5.2 | P0 không có tác giả (S:L40 "Fixer mở PR" mà Fixer là stub) | C | HIGH | Còn mở: hàng đợi thủ công |
| 5.3 | `NotAvailable` bị retry hoặc fallback chung bắt mất, rồi task chạy sang Opus hoặc "done" với output rỗng | S | MEDIUM | Giải quyết: lỗi terminal, `routing.yaml` có allowlist |
| 5.4 | Test monkeypatch socket chỉ chứng minh một đường gọi; cần test router không bao giờ chọn Codex | C | MEDIUM | Giải quyết một phần |
| 5.5 | Không còn reviewer hợp lệ (9Router chết, chỉ còn Claude) nên P0 treo im lặng | S | HIGH | Giải quyết một phần: escalate cho người |

### 2.6. Khóa file trên Windows và cô lập worktree

| # | Điểm | Nguồn | Mức | Sau vòng 2 |
|---|---|---|---|---|
| 6.1 | Reader (dashboard, Defender) giữ file làm `os.replace` lỗi WinError 5; hết retry thì mất cập nhật; audit và state lệch nhau | C+S | HIGH | Giải quyết: offset chỉ tiến sau khi ghi thành công, heartbeat chuyển lỗi, không im lặng |
| 6.2 | Single-writer làm mất provenance "ai làm" (S:L64); có dòng JSONL ghi dở | C | HIGH | Giải quyết: event mang `{agent, model_id, task, pid}`, bỏ qua dòng cuối dở |
| 6.3 | Lock mồ côi sau crash; xóa theo tuổi làm hỏng index; trùng đường dẫn worktree khi replay | S | MEDIUM | Giải quyết: lock lưu PID + start time, worktree `<task>-<attempt>` |
| 6.4 | Replay fold offset (crash trước khi lưu offset) làm ledger cộng đôi, lệnh áp hai lần (N6) | S | MEDIUM | **Còn mở**: event id + apply idempotent + offset nằm trong snapshot nguyên tử |
| 6.5 | Agent cùng OS user với orchestrator nên ghi được kill-switch, cắt được audit log, giả được lệnh inbox (1a/N2) | S | CRITICAL | **Còn mở**: tách user được hoãn; Judge yêu cầu tuyên bố tin cậy trung thực |
| 6.6 | Runtime `state/*.json` nằm trong repo nên mỗi worktree có bản riêng, state phân tách | C | HIGH | Giải quyết: state ra ngoài git |
| 6.7 | `%LOCALAPPDATA%` theo từng user nên vỡ khi tách OS user | C+S | MEDIUM | Giải quyết: đường dẫn cấu hình được |
| 6.8 | Loại trừ Defender cần quyền admin và giảm an toàn đúng chỗ code agent chạy | C | MEDIUM | Judge bác đề xuất loại trừ Defender |
| 6.9 | S:L60 nói worktree theo agent, P:L18 nói theo task | C | MEDIUM | Judge: **không mâu thuẫn**, task-attempt chặt hơn |

### 2.7. Phụ thuộc bridge 20129 và 9Router 20128

| # | Điểm | Nguồn | Mức | Sau vòng 2 |
|---|---|---|---|---|
| 7.1 | Fallback dsh→Sonnet, agy→dsh→Sonnet là tự chế (S:L50 chỉ có chuỗi Opus), vượt vendor và đẩy việc nhiều token sang Sonnet đắt | C | HIGH | Giải quyết: rút lại, chờ và thông báo |
| 7.2 | Probe TCP/200 báo khỏe trong khi upstream 429 hoặc treo; không có timeout nên breaker không bao giờ ngắt | S | HIGH | Giải quyết: probe bằng completion thật, timeout mỗi lời gọi |
| 7.3 | 9Router là điểm lỗi đơn cho 2/3 tuyến | Actor tự nhận | MEDIUM | Chấp nhận rủi ro, degraded mode |
| 7.4 | Process lạ nghe trên `:20128` vẫn qua probe | S | LOW | Còn mở |

### 2.8. Batch release và revert khi batch có nhiều PR

| # | Điểm | Nguồn | Mức | Sau vòng 2 |
|---|---|---|---|---|
| 8.1 | Revert cả batch về tag trước mâu thuẫn S:L39 ("revert PR"); nếu là reset thì phải force-push main | C | CRITICAL | Giải quyết: `git revert` từng PR theo thứ tự ngược manifest |
| 8.2 | Reset-to-tag xóa hotfix; state đã migrate v2 làm code cũ crash | S | CRITICAL | Giải quyết: revert-only, state tương thích N-1, giữ `release.lock` suốt bước kiểm tra |
| 8.3 | Revert PR chạm `release/**` là Owner-only nên kẹt khi người dùng vắng; revert theo thứ tự ngược có thể conflict (N5) | C+S | HIGH | **Còn mở**: cần lớp revert được ủy quyền trước, hoặc đóng băng và báo động |
| 8.4 | "Kiểm tra read-only" (bước 11) không có định nghĩa và không có test | C | MEDIUM | **Còn mở** |
| 8.5 | Thêm lại từng PR mỗi cửa sổ: 5 PR mất khoảng 2,5 ngày; check flaky làm revert batch tốt | S | MEDIUM | Giải quyết một phần: bisect trong staging worktree |
| 8.6 | Merge queue native của GitHub có thể không có trên repo cá nhân | Judge | MEDIUM | Batch job phải tự kiểm tra tích hợp |

### 2.9. Thời điểm digest

| # | Điểm | Nguồn | Mức | Sau vòng 2 |
|---|---|---|---|---|
| 9.1 | S:§7 ghi scheduler/digest là "tính năng sau" nhưng PR-4 lại xây | C | HIGH | Giải quyết: chuyển sang PR-4b, ngoài MVP |
| 9.2 | Không rõ ai duyệt Red: Approver-Agent một mình hay người dùng | C | HIGH | Còn mở: câu hỏi cho người dùng |
| 9.3 | Approver merge lúc 16:59:58 rơi ngoài manifest 17:00, nên "revert batch" bỏ sót | S | HIGH | Giải quyết: chỉ batch job merge, manifest lấy từ `git log prevTag..newTag` |
| 9.4 | Laptop ngủ qua cửa sổ, khi thức dậy chạy bù digest và release liền nhau, không có thời gian duyệt | S | MEDIUM | Giải quyết: lỡ cửa sổ thì bỏ qua và thông báo |
| 9.5 | `zoneinfo` trên Windows cần gói `tzdata` | C | MEDIUM | Giải quyết |
| 9.6 | Rebase trong merge queue đổi SHA, phê duyệt mất hiệu lực, PR Red không bao giờ ship hoặc ràng buộc bị nới lỏng (N3) | S | HIGH | **Còn mở**: gắn với (head SHA, patch-id), chỉ hợp lệ khi rebase không conflict |
| 9.7 | Chạy lại CI tích hợp trên Windows không vừa khoảng 11:55–12:00 | C | MEDIUM | Còn mở: timeout thì bỏ qua cửa sổ |

### 2.10. Maintenance P0–P3 chồng lấn với release

| # | Điểm | Nguồn | Mức | Sau vòng 2 |
|---|---|---|---|---|
| 10.1 | Gemini (tầng dễ) phân loại P0 (tác động cao) | C | HIGH → hạ LOW | Judge: **không mâu thuẫn** (S:L52 giao quét log nhiễu cho Gemini); thêm Opus xác nhận P0/P1 |
| 10.2 | Bug phát hiện lúc 09:00 phải chờ khoảng 23 giờ | C | HIGH → hạ MEDIUM | Judge: đúng theo chữ, nhưng là **thiết kế của SPEC** (S:L42); bug do release đã có bước 12 xử lý |
| 10.3 | P0 trùng cho bug đã revert, Fixer sửa code không còn trên main | S | MEDIUM | Giải quyết: dedup theo signature |
| 10.4 | Scan treo trong khi giữ `release.lock`; không có làn hotfix P0 | S | MEDIUM | Giải quyết một phần: scan đọc tag bất biến, không cần lock; vẫn chưa có lock TTL |
| 10.5 | Hàng đợi P0–P3 là "tính năng sau" theo S:§7 | C | LOW | Giải quyết: chuyển sang PR-4c |

### 2.11. Artifact: dữ liệu đúng hay chỉ "nhìn như xong"

| # | Điểm | Nguồn | Mức | Sau vòng 2 |
|---|---|---|---|---|
| 11.1 | Kill-switch và nút duyệt là thao tác ghi, phá vỡ single-writer; approve cần danh tính và SHA | C | HIGH | Giải quyết một phần: command event; Judge chốt **dashboard chỉ đọc trong MVP** |
| 11.2 | Không PR nào sở hữu schema state; dữ liệu giả sau PR-1 đúng là rủi ro "nhìn như xong" (S:§9.4) | C | HIGH | Giải quyết: PR-1 sở hữu `schemas/`; PR-5 không được merge khi chưa có dữ liệu thật |
| 11.3 | Snapshot trông tươi (`as_of=now`) trong khi fold bị kẹt vì một dòng inbox hỏng | S | HIGH | Giải quyết: `as_of` = lần fold cuối, cách ly dòng hỏng |
| 11.4 | CSRF: trang web bất kỳ có thể POST vào endpoint approve trên localhost | S | HIGH | Giải quyết: không có lệnh ghi trong MVP |
| 11.5 | Watermark FAKE bằng CSS bị một PR "làm đẹp" xóa mất | S | MEDIUM | Giải quyết: watermark lấy từ trường dữ liệu `source:"fixture"` |
| 11.6 | Chưa xác minh Muse Artifact đọc file cục bộ hay chạy server bằng cách nào | C | MEDIUM | **Còn mở** |

### 2.12. Chi phí token và việc nạp lại log

| # | Điểm | Nguồn | Mức | Sau vòng 2 |
|---|---|---|---|---|
| 12.1 | Ledger cộng sai: CLI báo usage cộng dồn theo phiên; lời gọi timeout phía client vẫn bị tính tiền nhưng không được ghi | S | MEDIUM | Giải quyết một phần: key idempotent `(task, call_id, attempt)`; vẫn phải xử lý delta hay cộng dồn cho từng adapter |
| 12.2 | Trường `estimated`, giá và quota/đô la phải có từ PR-1 trong khi adapter đến ở PR-2 | C | MEDIUM | Giải quyết: thêm trường và `config/prices.yaml` |
| 12.3 | "≤2KB, 5 trường" biến checklist không bắt buộc (S:§9.1) thành yêu cầu, không đo đạc theo S:§9.5 | C | MEDIUM | Giải quyết: mặc định tạm |
| 12.4 | Biện pháp chống nạp lại log (P:L65) không có tiêu chí đo được | C | LOW | Còn mở: đo số lần đọc log mỗi task |
| 12.5 | Handoff 2KB đánh rơi lỗi còn lại, nên agent sau vẫn đọc lại toàn bộ log | S | LOW–MEDIUM | Giải quyết một phần: handoff dẫn link artifact (path + SHA) |

### 2.13. Vấn đề xuyên suốt (PLAN thiếu hoàn toàn)

| # | Điểm | Nguồn | Mức | Sau vòng 2 |
|---|---|---|---|---|
| X.1 | Danh tính merge: một tài khoản `suanlong06` nên không thể ép "không tự merge" (S:L61) | C | CRITICAL | **Còn mở**: phải là điều kiện tiên quyết, không chỉ là câu hỏi |
| X.2 | Bot là write collaborator trên repo cá nhân vẫn merge được qua API và POST được status `ci` giả (N1) | S | CRITICAL | **Còn mở** |
| X.3 | Không có bản ghi phê duyệt (ai, khi nào, SHA nào, quyết định gì) | C | CRITICAL | Giải quyết: `approvals.jsonl`; GitHub review là nguồn sự thật |
| X.4 | Bootstrap: PR-1..3 được merge trước khi labeller/CI/Gate 3 tồn tại | C | HIGH | Giải quyết: quy tắc bootstrap thủ công |
| X.5 | CI GitHub mặc định chạy Linux nên bỏ sót lỗi khóa và đường dẫn Windows | C | HIGH | Giải quyết: `windows-latest` |
| X.6 | Self-hosted runner trên máy này chạy test do agent viết ngay cạnh state và token | C | HIGH | Giải quyết: chỉ dùng GitHub-hosted |
| X.7 | Output CI dán tay vào PR giả mạo được | C+S | MEDIUM | Giải quyết: có CI từ PR-0 |
| X.8 | PR-1 phình to (~11 mối quan tâm, Owner-only, review tay) | C | HIGH | Giải quyết: tách PR-0 / 1a / 1b |
| X.9 | PR-4 quá to, chứa 3 tính năng S:§7 cùng revert và auto-merge | C | MEDIUM | Giải quyết: tách 4a / 4b / 4c |
| X.10 | Tiêu chí chấp nhận không test được ("hiển thị dữ liệu thật", "revert đưa main về trạng thái trước") | C | MEDIUM | Giải quyết một phần |

---

## 3. Judge tự kiểm chứng các cáo buộc "mâu thuẫn SPEC"

| Cáo buộc | Kết quả | Ghi chú |
|---|---|---|
| S:L12/L35: Approver-Agent (AI) bấm merge Red | **Đúng** | Tầng Owner-only có thể biện minh bằng S:L16 ("người dùng duyệt cuối") và S:§8 Q1, nhưng vẫn là lệch SPEC; cần người dùng ký văn bản |
| S:L39: "revert PR" ngược với revert cả batch | **Đúng một phần** | Reset-to-tag + force-push thì trái. Revert *qua PR* thì không. Gốc rễ là P:L46 viết mơ hồ |
| S:L50: fallback chỉ trong chuỗi Opus | **Đúng** | Fallback dsh/agy là tự chế. P:L66 ("health check và fallback") cần gắn vào S:L50 |
| S:L15/L55: `fixer_proxy` ngược với stub | **Đúng một phần** | Proxy đóng vai Fixer là trái. P:L36 ("người/agent khác") thì hợp lệ. "P0 không có tác giả" là lỗ hổng thật |
| S:§7: scheduler, digest, P0–P3 là tính năng sau | **Đúng** | Ngoài ra P:L38 ("DeepSeek + Sonnet") ngầm dùng 2 reviewer, chồng với dual review của §7 và S:§8 Q3 đang mở |
| S:L60: worktree theo agent hay theo task | **Sai** | Task-attempt chặt hơn, miễn là không chia sẻ giữa các agent |
| S:L61: không ép được "không tự merge" | **Đúng**, là lỗ hổng thực thi | Không phải mâu thuẫn văn bản |
| S:L42: bug phải chờ khoảng 23 giờ | **Đúng theo chữ** | Nhưng đó là thiết kế của SPEC |
| Gemini phân loại P0 trái định tuyến | **Sai** | S:L52 giao quét log nhiễu cho Gemini |
| Runtime `state/*.json` trong git (P:L17) | **Đúng, nghiêm trọng** | Gây phân tách state giữa các worktree |

---

## 4. Phán quyết từng chủ đề của Judge

| # | Chủ đề | Bên thắng | Rủi ro còn lại | Phán quyết ngắn |
|---|---|---|---|---|
| 1 | Điều kiện auto-merge chưa chốt | Actor | LOW (khi còn tắt), thành HIGH khi bật | Tắt mặc định, shadow mode, chỉ merge trong cửa sổ batch |
| 2 | Red/Green và file nhạy cảm | Critic + Skeptic | HIGH | Labeller chạy từ base ref, file không rõ thì mặc định Red, casefold, có tầng Owner-only, gắn nhãn lại tại SHA merge |
| 3 | Gate 3 khác agent và model | Critic | MEDIUM | Writer set = model thực sự phục vụ, model không rõ coi là xung đột. MVP 1 reviewer; khác vendor cho Red/Owner-only; 2 reviewer để §7 |
| 4 | Giới hạn vòng fix | Critic | LOW | Lineage task_id + parent_pr + attempt; N vòng + ngân sách (tạm); trạng thái escalated/blocked rõ ràng |
| 5 | Fixer/Codex stub | Critic | LOW | Rút `fixer_proxy`; người dùng điều phối fix; Codex không bao giờ được gọi và có test chứng minh |
| 6 | Khóa file Windows và worktree | Actor (Critic sửa) | MEDIUM | Single-writer + inbox; retry có giới hạn và báo lỗi to; state ngoài repo |
| 7 | Bridge 20129 / 9Router 20128 | Critic | LOW | Probe thật + timeout + breaker; chờ và thông báo thay vì fallback; `NotAvailable` là terminal |
| 8 | Batch release và revert nhiều PR | Critic + Skeptic | HIGH | Revert PR theo thứ tự ngược manifest, không force-push, manifest từ git log; conflict thì đóng băng và báo động |
| 9 | Thời điểm digest | Actor | LOW | zoneinfo + tzdata, digest đóng băng, cutoff, lỡ cửa sổ thì bỏ qua; ngoài MVP theo §7 |
| 10 | Maintenance P0–P3 chồng release | Chia đôi | MEDIUM | Ngoài MVP (4c); Gemini đề xuất, Opus xác nhận; dedup theo fingerprint; giữ S:L42 |
| 11 | Artifact: dữ liệu đúng hay nhìn như xong | Critic | MEDIUM | Dashboard **chỉ đọc** cho đến khi tách OS user và xác minh runtime Muse; dữ liệu giả chỉ trong fixture test; contract test theo schema |
| 12 | Token cost và nạp lại log | Actor (Skeptic sửa) | LOW | Ledger ghi delta + cờ `estimated` + `prices.yaml`; mẫu handoff; đo sau 2 tuần |
| X | Agent cùng OS user với orchestrator | Skeptic (chẩn đoán), Critic (thời điểm) | CRITICAL khi đã có merge tự động hoặc dashboard ghi | Chấp nhận được cho PR-1 vì người merge tay mọi thứ |

---

## 5. Phán quyết của Judge

# ✅ ACCEPT WITH CHANGES (chấp nhận, kèm thay đổi)

Phần đúng của PLAN: thứ tự triển khai hợp lý, tư thế fail-closed (auto-merge tắt mặc định, có kill-switch), và câu hỏi mở được để mở một cách trung thực.

PLAN còn **bốn khuyết tật phải sửa trước khi viết code**:
1. Runtime state nằm trong git.
2. Không có cổng bootstrap cho PR-1..PR-3.
3. Danh tính merge chưa được định nghĩa.
4. Tính năng S:§7 lọt vào MVP.

Cả bốn đều sửa được mà không phải thiết kế lại. Critic và Skeptic đều đồng ý là có thể bắt đầu PR-1 sau khi sửa các điểm bắt buộc dưới đây.

> Ghi chú của người điều phối: vòng 2 có một chỗ lệch về cách tách PR-4. Actor đề xuất 4a = revert / incident / release thủ công. Judge đặt revert tự động vào 4b. Tài liệu này thống nhất như sau: **4a** = kill-switch fail-closed + shadow evaluator + quy trình revert/incident **thủ công**; **4b** = scheduler + digest + batch release + revert tự động; **4c** = maintenance P0–P3.

---

## 6. Thay đổi BẮT BUỘC trong PLAN trước PR-1

### (a) Quyết định và thiết lập cần có trước khi bắt đầu PR-1

1. **Phạm vi MVP.** PLAN ghi rõ MVP gồm PR-0, PR-1a, PR-1b, PR-2, PR-3, PR-4a, PR-5. PR-4b (scheduler, digest, batch release, revert tự động) và PR-4c (P0–P3) được đánh dấu "S:§7 tính năng sau". Nội dung của 4a / 4b / 4c phải được viết ra.
2. **Danh tính merge.** Agent push bằng một bot account riêng. Trong MVP chỉ người dùng merge, qua GitHub UI. PLAN phải ghi rõ đây là lệch khỏi S:L12/L35 ("Approver-Agent *đề xuất*, người hoặc batch job *thực hiện* merge"), và người dùng ký xác nhận.
3. **Branch protection trên `main`, bật trước khi PR-1 được merge.**
   - Bắt buộc mở PR.
   - Cần 1 approving review từ CODEOWNERS (người dùng).
   - Required check `ci` với nguồn bắt buộc là GitHub Actions, để chặn status giả theo N1.
   - Cấm force-push và cấm xóa nhánh.
   - Bật "include administrators".
   - Phải xác nhận gói GitHub của repo hỗ trợ các thiết lập này. Nếu không hỗ trợ, cho bot làm việc từ fork không có quyền write.
4. **PR-0.** Thêm `ci.yml` tối thiểu (lint + test), **chỉ** chạy trên GitHub-hosted `windows-latest`. Cấm self-hosted runner trên máy điều khiển.
5. **Quy tắc bootstrap cho PR-0..PR-3.**
   - Người dùng merge tay.
   - Gate 3 là review thủ công bởi một model khác vendor, ghi thành comment trong PR.
   - Không chấp nhận output CI dán tay làm bằng chứng.
6. **Tuyên bố tin cậy trong PLAN.** Cho đến khi tách OS user:
   - State cục bộ và audit log chỉ là best-effort, **không chống giả mạo**.
   - Mỏ neo tin cậy duy nhất là đăng nhập GitHub của người dùng và các required check.
   - Dashboard **chỉ đọc**.
7. **Con số tạm thời.** N vòng fix, timeout heartbeat, giới hạn kích thước, các cutoff giờ đều ghi là *mặc định tạm trong config*, không phải yêu cầu. Sửa P:L38 thành 1 reviewer, vì 2 reviewer là dual review của S:§7.

### (b) Nội dung PR-1 phải có

8. **Tách PR-1.**
   - **PR-1a:** schemas, state store, event fold, ledger, audit log.
   - **PR-1b:** worktree, lock, heartbeat, inbox.
   - Kill-switch chuyển sang PR-4a.
9. **Vị trí state.** Runtime state nằm ở đường dẫn **cấu hình được, ngoài repo** (mặc định `%LOCALAPPDATA%\harness`, đổi được khi tách user). Git chỉ chứa schemas và fixtures, nên **bỏ** `state/tasks.json` và `state/audit.log.jsonl` khỏi danh sách file đề xuất ở P:L17.
10. **Single-writer.**
    - Agent chỉ ghi inbox của chính nó; orchestrator là bên duy nhất ghi state.
    - Mỗi event mang `{agent, model_id, task, pid}` để giữ provenance "ai làm" (S:L64).
    - `os.replace` retry có giới hạn; thất bại cuối cùng đưa heartbeat sang lỗi, không bao giờ im lặng.
11. **Event idempotent (chống N6).**
    - Mỗi event có id `(agent, pid, seq)`.
    - Áp lại cùng event không gây thêm hiệu ứng.
    - Offset lưu trong cùng snapshot nguyên tử.
    - Bỏ qua dòng JSONL cuối ghi dở.
    - Có test crash-replay.
12. **State machine.**
    - Gồm bước 1–13, cộng `escalated`, `blocked`, `superseded`, `cancelled`, khai báo rõ là phần mở rộng.
    - Trường lineage `task_id`, `parent_pr`, `attempt`.
    - Schema có version và tương thích N-1.
13. **Trường ledger.** `task_id`, `attempt`, `agent`, **model thực sự phục vụ**, nguồn của model_id (CLI hay log 9Router), token dạng delta, chi phí, cờ `estimated`, giá lấy từ `config/prices.yaml`. Key idempotent `(task, call_id, attempt)`.
14. **Worktree và CI.**
    - Mỗi task-attempt một worktree, không chia sẻ, nằm dưới root ngắn cấu hình được.
    - Lock lưu PID + start time; chỉ dọn lock khi process đã chết.
    - Phụ thuộc `tzdata`.
    - Mọi tiêu chí chấp nhận được kiểm bằng test chạy trên Windows CI.

---

## 7. Thay đổi TÙY CHỌN hoặc làm sau

| # | PR | Thay đổi |
|---|---|---|
| 1 | PR-2 | Probe bằng completion thật, timeout mỗi lời gọi, circuit breaker; `NotAvailable` là terminal, không retry; `routing.yaml` validate theo allowlist adapter |
| 2 | PR-2 | Lấy model_id và vendor từ metadata có cấu trúc hoặc log 9Router (join theo request id), không bao giờ từ text model tự in ra (chống N4) |
| 3 | PR-2 | Test tĩnh (AST) cho thấy stub Codex/Cursor không import socket/subprocess/http; test router không bao giờ chọn Codex qua mọi đường fallback |
| 4 | PR-3 | Labeller chạy từ base ref, allowlist mặc định Red, casefold, symlink/submodule/`.gitattributes`/`conftest`/`.pth`/`pyproject`/xóa test là Red; danh sách Owner-only; gắn nhãn lại tại SHA merge |
| 5 | PR-3 | Gate 3 kiểm theo writer set thực sự phục vụ; vòng fix áp N vòng + ngân sách cộng dồn theo `task_id`; fingerprint finding (file, loại, vùng dòng) |
| 6 | PR-3 | `approvals.jsonl` phản chiếu GitHub review; GitHub là nguồn sự thật |
| 7 | PR-3 | Prompt cho reviewer coi diff là dữ liệu trích dẫn, verdict theo schema có bằng chứng |
| 8 | PR-4a | Kill-switch fail-closed (thiếu hoặc không đọc được thì coi như tắt); engine điều kiện chạy shadow mode ghi "would-merge" (mặc định tạm 2 tuần / 20 PR) |
| 9 | PR-4a | Định nghĩa và test cho "kiểm tra read-only" (bước 11); quy trình revert/incident thủ công |
| 10 | PR-4b | **Điều kiện tiên quyết:** tách OS user ít quyền cho agent (ACL trên thư mục state) trước mọi merge tự động hoặc mọi thao tác ghi từ dashboard |
| 11 | PR-4b | Manifest lấy từ `git log prevTag..newTag`; `release.lock` giữ suốt bước kiểm tra; revert PR theo thứ tự ngược; conflict thì đóng băng và báo động; tùy chọn lớp revert được ủy quyền trước khi `tree(revert) == tree(prevTag)` |
| 12 | PR-4b | Phê duyệt gắn (head SHA, patch-id), chỉ hợp lệ khi rebase không conflict, hiện range-diff; lỡ cửa sổ thì bỏ qua; batch job tự kiểm tra tích hợp, không phụ thuộc merge queue native |
| 13 | PR-4c | Dedup P0 theo fingerprint; Gemini đề xuất mức P, Opus xác nhận P0/P1; lock có TTL |
| 14 | PR-5 | Huy hiệu provenance (file nguồn, số bản ghi, `as_of` = lần fold cuối); "không có dữ liệu" khác "0"; watermark từ trường dữ liệu; contract test (tổng chi phí = tổng ledger); thao tác ghi chỉ sau khi tách OS user và xác minh runtime Muse |
| 15 | Sau MVP | Đo các mục ở S:§9 (kích thước handoff, số lần đọc log mỗi task, chi phí theo model) trước khi giữ hoặc thay thế, đúng như S:§9.5 |

---

## 8. Đề xuất bị Judge bác

| Đề xuất | Lý do bác |
|---|---|
| Audit log hash-chain neo ra ngoài máy, ngay trong PR-1 | Không chặn được giả mạo khi agent cùng user. Tuyên bố tin cậy trung thực (mục 6, điểm 6) giải quyết việc này mà không tốn chi phí |
| `%ProgramData%` + ACL ngay bây giờ | Quá sớm khi chưa tách user. Chỉ cần đường dẫn cấu hình được |
| Loại trừ thư mục worktree khỏi Windows Defender | Giảm an toàn đúng chỗ code agent chạy. Chỉ xem xét khi đã đo được tranh chấp khóa và có sự đồng ý của người dùng |
| Kênh lệnh qua named pipe / HTTP với token trong bộ nhớ | Không cần, vì MVP không có lệnh ghi cục bộ |
| Fallback tự chế và `fixer_proxy` | Vượt quá SPEC; Actor đã rút lại |
| 2 reviewer cho Red trong MVP | Đó là dual review của S:§7 |
| Output CI dán tay | Không cần khi đã có PR-0 |
| "Đóng băng Green" khi degraded | Vô nghĩa khi auto-merge đang tắt. Thay bằng "mọi cổng đều fail-closed" |
| Coi ≤200 dòng / ≤10 file, 11:55 / 16:55, 2KB, 20 PR là luật | Số tự đặt, chỉ giữ làm mặc định tạm |
| Thổi phồng mức độ | "Chờ 23 giờ" là thiết kế SPEC; "Gemini trái định tuyến" không phải mâu thuẫn. Cả hai bị hạ mức |
| Phụ thuộc merge queue native của GitHub | Có thể không có trên repo cá nhân |

---

## 9. Câu hỏi mở cho người dùng

Mỗi câu kèm **mặc định đề xuất** từ cuộc tranh luận. Người dùng quyết định cuối cùng.

| # | Câu hỏi | Mặc định đề xuất |
|---|---|---|
| 1 | Có tạo bot account riêng cho agent không? Ai giữ quyền merge? | **Có**, kèm CODEOWNERS bắt buộc review. Nếu gói GitHub không hỗ trợ thì bot làm việc từ fork |
| 2 | Approver-Agent có được tự merge PR Red không thuộc Owner-only không? (S:L12 so với S:L16) | **Không** trong MVP: Approver đề xuất, người dùng merge. Xem lại khi có dữ liệu shadow mode |
| 3 | Fixer: thủ công cho đến khi có Cursor, hay cho proxy tạm thời bằng agent khác? | **Thủ công** |
| 4 | P0 có được release ngoài cửa sổ (hotfix lane) không? | **Không**, giữ S:L42; bug do release đã có bước 12 (revert ngay) |
| 5 | Khi 9Router/bridge sập: chờ, hay cho fallback sang Claude có trần chi phí? | **Chờ và thông báo** |
| 6 | Có làm PR-4b/4c (scheduler, digest, batch, P0–P3) ngay không? | **Không**, hoãn theo S:§7; digest và release chạy thủ công |
| 7 | Có chấp nhận một Windows user riêng, ít quyền, cho agent không? | **Có**, trước PR-4b và trước mọi thao tác ghi từ dashboard |
| 8 | Auto-merge (khi bật) chạy ngay hay gom vào batch 12:00 / 17:00? | **Gom vào batch** |
| 9 | Duyệt danh sách Owner-only ban đầu: `config/**`, labeller, gate3, `ci.yml`, audit, ledger, `release/**`, schemas, CODEOWNERS? | **Đồng ý** |
| 10 | Quy tắc vendor của Gate 3? | Red và Owner-only: 1 reviewer **khác vendor**. Green: khác model là đủ |
| 11 | Comment do bot/agent tạo có tính là "chưa xử lý" không? (S:§8 Q4) | Mặc định của Judge, chưa tranh luận: review thread chưa resolve thì tính, bất kể ai viết; comment chỉ mang tính thông tin thì không |
| 12 | Allowlist Green hẹp (`docs/**` + test thêm mới) khiến gần như mọi PR là Red. Green có cần "dùng được" không? (hỏi lại S:§8 Q2) | Giữ hẹp trong MVP, theo dõi số lần duyệt mỗi ngày của người dùng, nới dần theo dữ liệu |
| 13 | Muse Artifact đọc file cục bộ hay chạy server bằng cách nào? | Cần xác minh trước PR-5; trong lúc chờ, dashboard chỉ đọc |
| 14 | "Kiểm tra read-only" ở bước 11 gồm những gì? | Cần định nghĩa trước PR-4a (ví dụ smoke test + health check + so sánh lỗi trong log) |
| 15 | Giới hạn N vòng fix và timeout heartbeat là bao nhiêu? (P:L80) | Mặc định tạm: N=2, ngân sách cộng dồn theo task; timeout heartbeat cấu hình được, chỉnh theo dữ liệu thật |
