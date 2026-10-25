# SPEC — Harness Agents

> Bản viết lại ngày 2026-10-25 theo DEBATE.md mục 4. 12 thay đổi bắt buộc được ghi thành quy tắc **R1–R12** ở mục 5.2.
> Quy trình (mục 3), định tuyến model (mục 4), vai trò (mục 2), Artifact MVP (mục 6) và checklist tham khảo (mục 9) giữ nguyên tinh thần.
> 4 điều kiện auto-merge và mọi con số đề xuất ở mục 8 **vẫn chưa được chốt**.

## 1. Mục tiêu

Harness Agents là một **control plane** chạy trên máy Windows này. Nó điều phối nhiều agent AI qua dashboard **Muse Artifact**. **Muse AI** theo dõi tiến độ và thông báo cho người dùng. Hệ thống không phải framework: nó chỉ điều phối, ghi nhận và kiểm soát các công cụ agent sẵn có.

## 2. Vai trò

| Vai trò | Nhiệm vụ |
|---|---|
| Spec-Agent | Viết spec 1 trang |
| Approver-Agent | Duyệt spec cùng người dùng; bấm merge PR Red sau digest, **chỉ khi đã có bản ghi duyệt của người dùng** (R7) |
| Implementer | Viết code, mở PR |
| Reviewer Gate 3 | Review PR; bắt buộc khác agent và khác model **thực tế** đã viết (R11); chỉ trả kết quả, không có quyền merge |
| Fixer-Agent | **Stub** (chờ Cursor); nhận fix list, mở PR mới |
| Người dùng | Người duyệt cuối cùng; người duy nhất tạo được bản ghi duyệt và bật/tắt auto-merge |

Spec-Agent, Approver-Agent và Fixer-Agent thay cho Hải, Anh, Luân và Cursor trong flowchart. Đây là các model agent, không phải người thật.

**Orchestrator** là thành phần hệ thống, không phải agent model. Nó là writer duy nhất của state và audit log (R3), giữ cờ auto-merge và kill-switch (R8), và thực hiện merge bằng một **danh tính merge riêng**, khác Implementer (R7).

## 3. Quy trình

Mọi mốc giờ đều theo **Asia/Seoul (KST)**.

**Nhánh tính năng mới**

1. Nhu cầu → phân loại công việc.
2. Spec-Agent viết spec 1 trang.
3. Approver-Agent và người dùng duyệt spec.
4. Implementer mở PR.
5. Máy gắn nhãn **Red/Green** dựa trên các file bị chạm (R8).
6. CI kiểm tra lint, test, build.
7. Gate 3: reviewer khác người viết review PR (R1, R11).
8. Không đạt → viết fix list → Fixer-Agent mở PR mới (PR cũ thành `superseded`, bộ đếm vòng fix gắn với task, R2) → quay lại bước 5.
9. Đạt:
   - **Red** → vào digest lúc 11:30 và 16:30 → người dùng duyệt (bản ghi duyệt, R7) → Approver bấm merge.
   - **Green** → chỉ auto-merge khi đủ 4 điều kiện (mục 8, chưa chốt) **và** cờ auto-merge đang bật (R8).
   - Mọi merge đều kiểm tra lại CI và Gate 3 trên kết quả merge thực tế (R9).
10. Batch release lúc 12:00 và 17:00 (có manifest release, R10).
11. Kiểm tra nhanh ở chế độ read-only.
12. Có bug → revert theo R10, release lại, mở incident.
13. Ổn → maintenance scan lúc 08:00 xếp hạng lỗi P0–P3 → Fixer mở PR (khi Fixer còn là stub: task vào `awaiting-human`, R2).

**Nhánh sửa lỗi:** đi thẳng vào maintenance scan lúc 08:00.

## 4. Định tuyến model

Chọn model theo độ khó và mức ảnh hưởng của task:

| Mức | Đích | Model |
|---|---|---|
| Khó, ảnh hưởng cao | Claude Code native | `opus 5.5 high` → fallback `opus 5.5 low` → `sonnet 5.5 high` |
| Trung bình | DeepSeek harness `dsh` qua 9Router | `xh deepseek-v4.1-flash-1017` |
| Dễ, tốn nhiều token, thông tin nhiễu | Antigravity `agy` qua bridge `:20129` → 9Router `:20128` | `gemini-3.8-flash-medium`, `gemini-4-argon-medium` |

- **Codex:** chưa có tài khoản và CLI. Hệ thống chỉ có một **stub adapter** để tích hợp sau; Codex **không bao giờ được gọi** và không có trong cấu hình định tuyến.
- **Cursor:** chưa dùng được. Fixer-Agent tạm thời là vai trò stub.
- **Fallback** không bao giờ được phá Gate 3 (R11).

## 5. Nguyên tắc và quy tắc bắt buộc

### 5.1. Nguyên tắc chung

- **Control plane, không phải framework:** dùng lại CLI/harness có sẵn, không viết lại chúng.
- **Cô lập bằng worktree:** mỗi task làm việc trong một git worktree riêng (R6).
- **Không tự merge:** agent viết code không được merge PR của chính nó; merge dùng danh tính merge riêng (R7).
- **Shared state:** trạng thái chung lưu dạng file mà cả người lẫn agent đọc được (JSON/Markdown); chỉ orchestrator được ghi (R3).
- **Cost ledger:** ghi token và chi phí theo từng task và từng model thực tế (R5).
- **Audit log:** append-only, có hash chain, ghi mọi hành động: ai làm, model nào, lúc nào, trên task/PR/SHA nào, kết quả ra sao (R4).
- **Fail-closed:** khi thiếu thông tin hoặc điều kiện, hệ thống dừng và chờ người dùng, không tự hạ chuẩn.

### 5.2. Quy tắc bắt buộc R1–R12

**R1 — Gate 3 thủ công cho PR-1 đến PR-3.** Khi `gate3.py` chưa tồn tại (trước khi PR-3 merge), Gate 3 làm thủ công:
- Reviewer khác agent và khác model thực tế của writer.
- Mô tả PR ghi: writer `(agent, model, vendor)`, reviewer `(agent, model, vendor)`, SHA được review, kết quả pass/fail kèm danh sách lỗi.
- **PR-1 do Opus viết: DeepSeek là reviewer chính.** Không dùng Sonnet làm reviewer chính vì cùng vendor với Opus.
- Khi labeler chưa có, PR-1 đến PR-3 được xử lý như Red: chỉ merge sau khi người dùng duyệt.

**R2 — State machine mở rộng và lineage.** Trạng thái task theo các bước ở mục 3, cộng thêm:
- `blocked`: vượt giới hạn vòng fix hoặc ngân sách, hoặc thiếu điều kiện; chờ người dùng.
- `superseded`: PR cũ khi Fixer mở PR mới cho cùng task; tự đóng hoặc đánh dấu.
- `cancelled`: người dùng hủy task.
- `awaiting-human`: việc cần người làm, gồm mọi fix list và mọi task P0–P3 do scan tạo ra khi Fixer còn là stub.

Mỗi PR mang `task_id` và `parent_pr`. Bộ đếm vòng fix và ngân sách gắn với **task**, không reset khi mở PR mới. Chuyển trạng thái không hợp lệ bị từ chối.

**R3 — Một writer duy nhất.** Chỉ orchestrator ghi `state/*.json` và audit log. Agent và dashboard chỉ ghi vào inbox; orchestrator gộp inbox vào state. Ghi nguyên tử, thử lại khi gặp `PermissionError`. Reader chịu được file đang ghi dở (bỏ qua và đọc lại). Phải có **test tranh chấp nhiều tiến trình trên Windows**.

**R4 — Toàn vẹn audit log.** Mỗi bản ghi có số thứ tự và hash của bản ghi trước (hash chain). Orchestrator kiểm tra chuỗi khi khởi động; chuỗi đứt thì dừng và báo người dùng. Mỗi bản ghi gồm: thời điểm (KST), hành động, kết quả, `agent`, **model thực tế đã chạy**, **vendor**, **commit SHA**, `task_id` (và PR nếu có).

**R5 — Ledger.** Mỗi lần gọi model là một bản ghi gồm: **idempotency key** (retry hay fallback không bị đếm trùng), **model thực tế** sau fallback kèm lý do fallback, token, chi phí, và cờ **`estimated`** (`true` khi tuyến không báo token, ví dụ `agy`).

**R6 — Vòng đời worktree.** Mỗi task một worktree và một nhánh, tên theo task ID duy nhất, đường dẫn ngắn. Khi khởi động: dọn worktree mồ côi và `index.lock` cũ. Tắt `gc.auto` trong lúc agent đang chạy.

**R7 — Bản ghi duyệt và danh tính merge.**
- Bản ghi duyệt lưu trong state (schema thuộc PR-1): người duyệt, đối tượng (spec hoặc PR kèm SHA), thời điểm, nguồn.
- **Chỉ người dùng** tạo được bản ghi duyệt, qua orchestrator, có audit. Agent không tạo hay sửa được. Bản ghi của người dùng phải phân biệt được với mọi bản ghi do agent tạo.
- Merge dùng **danh tính merge riêng**, khác Implementer, được ghi vào audit.
- Approver-Agent **chỉ merge PR Red khi có bản ghi duyệt của người dùng** cho đúng PR và SHA đó.

**R8 — Cờ auto-merge, kill-switch và labeler.**
- Cờ auto-merge và kill-switch nằm trong state do orchestrator giữ. **Mặc định: tắt.** Chỉ người dùng bật/tắt được, qua orchestrator, có audit.
- Mọi PR chạm cờ, kill-switch hoặc `sensitive_paths.yaml` đều là Red.
- Labeler **luôn đọc `config/sensitive_paths.yaml` từ base branch** (main), không bao giờ từ PR head.
- Labeler **chuẩn hóa đường dẫn** (chữ thường, dấu `/`) trước khi so khớp; xét cả tên cũ và tên mới khi đổi tên, và cả file bị xóa.
- Nguyên tắc có hiệu lực ngay; phần cài đặt làm ở PR-3.

**R9 — Merge an toàn.** Ngay trước khi merge (Red hay Green), CI và Gate 3 phải hợp lệ trên **kết quả merge thực tế** (PR ghép với base hiện tại, kiểu merge queue). Nếu kết quả khác SHA đã kiểm thì phải kiểm lại. Cài đặt ở PR-4.

**R10 — Revert và manifest release.**
- Revert bằng **revert commit theo thứ tự ngược**, **không force push**, không reset. Các commit merge sau release vẫn được giữ. Trạng thái đích: thay đổi của batch bị gỡ, không phải main quay về tag cũ.
- Manifest release (schema thuộc state PR-1): tag, danh sách PR và SHA theo thứ tự merge, `task_id`, phụ thuộc giữa các PR. Khi đưa lại PR, giữ đúng thứ tự phụ thuộc.

**R11 — Fallback không được phá Gate 3.**
- Gate 3 so **model và vendor thực tế** (do adapter trả về sau fallback), không so model dự kiến.
- Có bảng ánh xạ alias chuẩn (ví dụ alias 9Router → `(model_id, vendor)`). Alias không rõ thì không được làm reviewer.
- Nếu không còn reviewer hợp lệ thì PR **chờ** và báo người dùng; không merge, không hạ chuẩn.

**R12 — Phạm vi PR-4 so với mục 7.** Xem phần làm rõ ở mục 7.

## 6. Artifact MVP

1. **Task kanban:** các cột theo trạng thái quy trình, gồm cả `blocked`, `superseded`, `cancelled`, `awaiting-human`.
2. **PR queue:** nhãn Red/Green và trạng thái CI.
3. **Approval queue:** spec và PR Red đang chờ duyệt.
4. **Agent status:** trạng thái và heartbeat của từng agent.
5. **Logs:** xem audit log và log của từng agent.
6. **Token cost theo task:** lấy từ cost ledger.

Dashboard đọc state ở chế độ read-only. Thao tác ghi (duyệt, kill-switch, cờ auto-merge) chỉ đi qua orchestrator (R3, R7, R8).

## 7. Tính năng sau

- Scheduled jobs (digest, release, maintenance scan).
- Digest 11:30 và 16:30.
- Hàng đợi P0–P3.
- So sánh dual review (hai reviewer song song).
- Replay task bị lỗi.

> **Làm rõ phạm vi (R12):** PLAN cho PR-4 làm scheduler cho digest, batch release, maintenance scan và hàng đợi P0–P3. Đây là phần **đi trước lộ trình mục 7 có chủ đích**, vì bước 9–13 ở mục 3 cần nó. Dual review và replay vẫn là tính năng sau. Auto-merge vẫn tắt cho tới khi chốt mục 8. Nếu người dùng muốn lùi scheduler, PR-4 chỉ giữ revert, manifest và merge an toàn; digest và release chạy tay.

## 8. OPEN DISCUSSION FOR PR

> ⚠️ **4 điều kiện auto-merge cho PR Green CHƯA được chốt.** Phần này dùng để thảo luận trong PR, chưa phải quyết định cuối.

**Ứng viên điều kiện:**

1. CI xanh (lint, test, build đều pass).
2. Gate 3 đã đạt.
3. Không có nhãn Red và không chạm file nhạy cảm.
4. Không còn comment chưa xử lý.

**Câu hỏi cần quyết định:**

- Ai được phép sửa `config/sensitive_paths.yaml`, và danh sách khởi đầu gồm gì? (Gợi ý từ DEBATE: thêm `.gitattributes`, cấu hình lint/test, lockfile, `tests/**`, code nút duyệt và kill-switch.)
- Điều kiện 3 có trùng với chính quy tắc gắn nhãn Red không? Nếu trùng, nên thay bằng điều kiện khác (ví dụ giới hạn số dòng thay đổi, hoặc bắt buộc có test mới)?
- Gate 3 cần một hay hai reviewer? Reviewer có bắt buộc khác cả vendor với Implementer không? (Riêng PR-1 đã chốt DeepSeek làm reviewer chính, R1.)
- Comment do bot/agent tạo có tính là "chưa xử lý" không?
- Auto-merge thực hiện ngay hay gom vào batch release 12:00/17:00?
- PR Green tạo ra trong vòng lặp Fixer có cần thêm điều kiện không?
- Có chạy shadow mode trước khi bật auto-merge không?

**Giá trị số do Actor đề xuất — CHỈ LÀ ĐỀ XUẤT KHỞI ĐẦU, CHỜ NGƯỜI DÙNG CHỐT, KHÔNG PHẢI QUYẾT ĐỊNH:**

| Chủ đề | Đề xuất khởi đầu |
|---|---|
| Thay điều kiện 3 bằng giới hạn diff | ≤200 dòng / ≤10 file, có test khi sửa src |
| Số vòng fix tối đa | N = 2 |
| Ngân sách token cho vòng fix | 3× chi phí gốc |
| Số PR tối đa mỗi batch | 5 |
| Cảnh báo fix list chờ quá lâu | 4 giờ |
| Hạn duyệt PR Red trước batch | 11:55 / 16:55 (25 phút trước batch) |
| Shadow mode trước khi bật auto-merge | 2 tuần hoặc 20 PR |
| Fixer chạy song song | tối đa 2 việc |
| Độ dài bàn giao | ≤2 KB |
| Circuit breaker bridge/9Router | mở sau 3 lỗi, giữ 5 phút; probe timeout 2 giây, cache 30 giây |
| Banner dữ liệu cũ trên dashboard | dữ liệu cũ hơn 5 phút |
| Timeout heartbeat | chưa có đề xuất |

**Câu hỏi mở khác:**

- "Release" nghĩa là gì với control plane chạy cục bộ: release ra đâu? Bước "kiểm tra nhanh read-only" kiểm tra cụ thể những gì? (Phải trả lời trước PR-4.)
- P0 có đường hotfix ngoài batch không, hay luôn chờ digest và release?
- Muse Artifact đọc trực tiếp file state cục bộ được không, hay cần bước xuất snapshot?
- Có cho phép `fixer_proxy` (tắt mặc định) trước khi có Cursor không? Việc này đổi vai trò stub ở mục 2.
- Có đặt trần ngân sách token theo task và theo ngày không, mức bao nhiêu?
- Có chấp nhận loại trừ Windows Defender cho thư mục worktree và state không (cần quyền admin)?
- Mục nào của checklist mục 9 sẽ được đo để giữ hay thay?

## 9. CHECKLIST THAM KHẢO CHO PR — KHÔNG PHẢI CHUẨN BẮT BUỘC

> ⚠️ **Đọc trước khi dùng:** Phần này chỉ là checklist tham khảo để khi review PR thì đem ra
> đối chiếu và so sánh. **Không lấy các mục dưới đây làm gốc để bám vào hoàn toàn.**
> Nếu PR tìm được cách tốt hơn (ít token hơn, nhanh hơn, tin cậy hơn, dễ bảo trì hơn),
> thì thay checklist này bằng cách tốt hơn đó, và ghi rõ lý do thay.
> Checklist này không thay thế quy trình ở mục 3, định tuyến model ở mục 4,
> Gate 3, nhãn Red/Green, 4 điều kiện auto-merge ở mục 8, hay quy tắc bắt buộc R1–R12 ở mục 5.2.

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
