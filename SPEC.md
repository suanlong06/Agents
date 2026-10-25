# SPEC — Harness Agents

## 1. Mục tiêu

Harness Agents là một **control plane** chạy trên máy Windows này. Nó điều phối nhiều agent AI qua dashboard **Muse Artifact**. **Muse AI** theo dõi tiến độ và thông báo cho người dùng. Hệ thống không phải framework: nó chỉ điều phối, ghi nhận và kiểm soát các công cụ agent sẵn có.

## 2. Vai trò

| Vai trò | Nhiệm vụ |
|---|---|
| Spec-Agent | Viết spec 1 trang |
| Approver-Agent | Duyệt spec cùng người dùng; bấm merge PR Red sau digest |
| Implementer | Viết code, mở PR |
| Reviewer Gate 3 | Review PR; bắt buộc khác agent/model đã viết |
| Fixer-Agent | **Stub** (chờ Cursor); nhận fix list, mở PR mới |
| Người dùng | Người duyệt cuối cùng |

Spec-Agent, Approver-Agent và Fixer-Agent thay cho Hải, Anh, Luân và Cursor trong flowchart. Đây là các model agent, không phải người thật.

## 3. Quy trình

Mọi mốc giờ đều theo **Asia/Seoul (KST)**.

**Nhánh tính năng mới**

1. Nhu cầu → phân loại công việc.
2. Spec-Agent viết spec 1 trang.
3. Approver-Agent và người dùng duyệt spec.
4. Implementer mở PR.
5. Máy gắn nhãn **Red/Green** dựa trên các file bị chạm.
6. CI kiểm tra lint, test, build.
7. Gate 3: reviewer khác người viết review PR.
8. Không đạt → viết fix list → Fixer-Agent mở PR mới → quay lại bước 5.
9. Đạt:
   - **Red** → vào digest lúc 11:30 và 16:30 → Approver bấm merge.
   - **Green** → chỉ auto-merge khi đủ 4 điều kiện (xem mục 8).
10. Batch release lúc 12:00 và 17:00.
11. Kiểm tra nhanh ở chế độ read-only.
12. Có bug → revert PR, release lại, mở incident.
13. Ổn → maintenance scan lúc 08:00 xếp hạng lỗi P0–P3 → Fixer mở PR.

**Nhánh sửa lỗi:** đi thẳng vào maintenance scan lúc 08:00.

## 4. Định tuyến model

Chọn model theo độ khó và mức ảnh hưởng của task:

| Mức | Đích | Model |
|---|---|---|
| Khó, ảnh hưởng cao | Claude Code native | `opus 5.5 high` → fallback `opus 5.5 low` → `sonnet 5.5 high` |
| Trung bình | DeepSeek harness `dsh` qua 9Router | `xh deepseek-v4.1-flash-1017` |
| Dễ, tốn nhiều token, thông tin nhiễu | Antigravity `agy` qua bridge `:20129` → 9Router `:20128` | `gemini-3.8-flash-medium`, `gemini-4-argon-medium` |

- **Codex:** chưa có tài khoản và CLI. Chỉ làm **stub adapter** để tích hợp sau, **không bao giờ gọi**.
- **Cursor:** chưa dùng được. Fixer-Agent tạm thời là vai trò stub.

## 5. Nguyên tắc

- **Control plane, không phải framework:** dùng lại CLI/harness có sẵn, không viết lại chúng.
- **Cô lập bằng worktree:** mỗi agent làm việc trong một git worktree riêng.
- **Không tự merge:** agent viết code không được merge PR của chính nó.
- **Shared state:** trạng thái chung lưu dạng file mà cả người lẫn agent đọc được (ví dụ JSON/Markdown).
- **Cost ledger:** ghi token và chi phí theo từng task và từng model.
- **Audit log:** ghi lại mọi hành động (append-only): ai làm, lúc nào, trên task/PR nào, kết quả ra sao.

## 6. Artifact MVP

1. **Task kanban:** các cột theo trạng thái quy trình.
2. **PR queue:** nhãn Red/Green và trạng thái CI.
3. **Approval queue:** spec và PR Red đang chờ duyệt.
4. **Agent status:** trạng thái và heartbeat của từng agent.
5. **Logs:** xem audit log và log của từng agent.
6. **Token cost theo task:** lấy từ cost ledger.

## 7. Tính năng sau

- Scheduled jobs (digest, release, maintenance scan).
- Digest 11:30 và 16:30.
- Hàng đợi P0–P3.
- So sánh dual review (hai reviewer song song).
- Replay task bị lỗi.

## 8. OPEN DISCUSSION FOR PR

> ⚠️ **4 điều kiện auto-merge cho PR Green CHƯA được chốt.** Phần này dùng để thảo luận trong PR, chưa phải quyết định cuối.

**Ứng viên điều kiện:**

1. CI xanh (lint, test, build đều pass).
2. Gate 3 đã đạt.
3. Không có nhãn Red và không chạm file nhạy cảm.
4. Không còn comment chưa xử lý.

**Câu hỏi cần quyết định:**

- Định nghĩa "file nhạy cảm" ở đâu (file cấu hình riêng hay danh sách glob)? Ai được phép sửa danh sách đó?
- Điều kiện 3 có trùng với chính quy tắc gắn nhãn Red không? Nếu trùng, nên thay bằng điều kiện khác (ví dụ giới hạn số dòng thay đổi, hoặc bắt buộc có test mới)?
- Gate 3 cần một hay hai reviewer? Reviewer có bắt buộc khác cả họ model (vendor) với Implementer không?
- Comment do bot/agent tạo có tính là "chưa xử lý" không?
- Auto-merge thực hiện ngay hay gom vào batch release 12:00/17:00?
- PR Green tạo ra trong vòng lặp Fixer có cần thêm điều kiện không?
- Có cần kill-switch để người dùng tắt toàn bộ auto-merge từ dashboard không?

## 9. CHECKLIST THAM KHẢO CHO PR — KHÔNG PHẢI CHUẨN BẮT BUỘC

> ⚠️ **Đọc trước khi dùng:** Phần này chỉ là checklist tham khảo để khi review PR thì đem ra
> đối chiếu và so sánh. **Không lấy các mục dưới đây làm gốc để bám vào hoàn toàn.**
> Nếu PR tìm được cách tốt hơn (ít token hơn, nhanh hơn, tin cậy hơn, dễ bảo trì hơn),
> thì thay checklist này bằng cách tốt hơn đó, và ghi rõ lý do thay.
> Checklist này không thay thế quy trình ở mục 3, định tuyến model ở mục 4,
> Gate 3, nhãn Red/Green, hay 4 điều kiện auto-merge ở mục 8.

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
