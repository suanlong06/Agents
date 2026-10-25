# DEBATE — Phản biện PLAN.md (Actor-Critic + Multi-Agent Debate)

Tài liệu này ghi lại cuộc tranh luận về PLAN.md (đối chiếu với SPEC.md). Nó không sửa SPEC.md hay PLAN.md và không chứa code. Tham chiếu dòng: `P:Lnn` là PLAN.md, `S:§n` / `S:Lnn` là SPEC.md.

> ⚠️ **Nguồn gốc nội dung — đọc trước khi dùng**
>
> - **Vòng 1 — Actor:** đã chạy thật bằng một subagent riêng. Lập trường của Actor ở dưới được tóm tắt từ chính đầu ra đó.
> - **Vòng 1 — Critic và Skeptic Red Team:** đã được khởi chạy bằng subagent, nhưng phiên làm việc bị ngắt trước khi có kết quả. **Không có đầu ra nào của hai subagent này được ghi nhận.**
> - **Vòng 2 (Actor phản hồi, Critic phản bác lại) và Judge:** chưa chạy bằng subagent.
> - Người dùng yêu cầu không chạy lại subagent. Vì vậy, các điểm phản biện, kịch bản red team, phản hồi vòng 2 và phán quyết Judge dưới đây là **phần tổng hợp của agent điều phối**. Agent này đóng thay các vai đó, dựa trên SPEC.md, PLAN.md và đầu ra thật của Actor. Đây **không phải** ý kiến độc lập của các subagent riêng biệt.
> - Nếu cần một cuộc phản biện thật sự độc lập, hãy chạy lại Critic, Skeptic và Judge bằng subagent rồi so sánh với tài liệu này.

---

## 1. Tóm tắt các vòng tranh luận

### Vòng 1 — Actor đề xuất (subagent thật)

Lập trường chung: **chấp nhận PLAN.md, chỉ cần thay đổi nhỏ.** Lý do Actor đưa ra:

- Kế hoạch **fail-closed**: auto-merge tắt mặc định, có kill-switch, và tiêu chí chấp nhận cấm merge khi thiếu điều kiện (P:L45–46).
- Câu hỏi mở được để mở (P:L69–81), không bịa ra câu trả lời.
- Thứ tự PR-1 → PR-4 xây lõi state và audit trước khi có bất kỳ thứ gì merge hay release được.
- Mục 9 của SPEC được coi là giả thuyết cần đo, không phải luật.

Các đề xuất cụ thể của Actor:

| # | Chủ đề | Đề xuất của Actor |
|---|---|---|
| 1 | Auto-merge | CI xanh trên head SHA đã rebase. Gate 3 gắn với cùng SHA đó. Thay điều kiện 3 bằng "diff ≤200 dòng / ≤10 file, có test khi sửa src". Merge trong cửa sổ batch. Chạy shadow mode 2 tuần hoặc 20 PR. |
| 2 | File nhạy cảm | Danh sách glob trong `config/sensitive_paths.yaml`, gồm cả chính file danh sách. Lấy file thay đổi bằng `git diff --name-status merge-base...HEAD`. Kiểm tra cả tên cũ và tên mới khi đổi tên. Labeler lỗi thì gắn Red. |
| 3 | Gate 3 | Ghi `(agent, model_id, vendor)` cho người viết và người review. PR Green: 1 reviewer khác model. PR Red hoặc task khó: 2 reviewer, ít nhất 1 khác vendor. PR-1 do Opus viết thì DeepSeek review, không dùng Sonnet. |
| 4 | Vòng lặp fix | Tối đa N=2 vòng, cộng ngân sách token bằng 3× chi phí gốc. Vượt giới hạn thì chuyển `blocked`. Cùng một lỗi lặp lại 2 lần thì leo thang. PR Green từ Fixer phải qua Gate 3 mới. |
| 5 | Stub | Test monkeypatch `socket`/`subprocess`. Codex không bao giờ có trong `routing.yaml`. Thêm `fixer_proxy` tùy chọn, tắt mặc định. Muse báo khi fix list chờ quá 4 giờ. |
| 6 | Windows | Chỉ orchestrator ghi state; agent ghi vào inbox riêng. `os.replace` thử lại 5 lần có jitter. Worktree đặt ở `C:\wt\<task-id>`. Bật `core.longpaths`. Loại trừ Defender. Prune worktree hằng đêm. |
| 7 | Bridge / 9Router | Probe có timeout 2 giây, cache 30 giây. Circuit breaker mở sau 3 lỗi, giữ 5 phút. `dsh` lỗi thì chuyển sang Sonnet. `agy` lỗi thì chuyển sang `dsh` hoặc Sonnet. Ghi lý do fallback vào ledger. |
| 8 | Batch / revert | Squash commit có trailer `Task-ID`. Gắn tag release và ghi manifest. Lỗi thì revert cả batch, sau đó đưa lại từng PR hoặc chia đôi để tìm lỗi. Tối đa 5 PR mỗi batch. |
| 9 | Digest | Dùng `zoneinfo("Asia/Seoul")`. Chốt danh sách Red lúc 11:30 / 16:30. Hạn duyệt 11:55 / 16:55. Có chính sách cho lần chạy bị lỡ. Test với đồng hồ cố định. |
| 10 | Maintenance | Scan read-only trên tag release mới nhất. Scan, release và revert dùng chung `release.lock`. P0 bị ép thành Red. Fixer chạy tối đa 2 việc cùng lúc. |
| 11 | Dashboard | `data_adapter.py` là nguồn duy nhất. Mỗi view hiện nguồn, số bản ghi và thời điểm dữ liệu. Có watermark FAKE DATA. Có contract test. Hiện banner khi dữ liệu cũ hơn 5 phút. |
| 12 | Token | Bàn giao tối đa 2 KB. Không nối CLI sang CLI. Ghi `estimated=true` khi không có số đo. So sánh chi phí sau 2 tuần. |

### Vòng 1 — Critic và Skeptic Red Team

Hai subagent đã được khởi chạy song song nhưng **bị ngắt, không có kết quả**. Mục 2 bên dưới là phần phản biện do agent điều phối tổng hợp thay, theo đúng hai vai:

- **Critic:** chỉ ra lỗ hổng và mâu thuẫn với SPEC.
- **Skeptic:** dựng kịch bản phá vỡ theo từng bước.

### Vòng 2 — Actor phản hồi (tổng hợp)

Cột "Phản hồi Actor" trong mục 2 lấy từ **các biện pháp có thật trong đầu ra vòng 1 của Actor** khi chúng trả lời được điểm phản biện. Nếu Actor chưa đề cập, ô đó ghi "chưa có".

### Judge tổng hợp (tổng hợp)

Xem mục 3 và mục 4.

---

## 2. Các điểm phản biện theo chủ đề

Mức độ: **NGHIÊM TRỌNG** (critical) · **CAO** (high) · **TRUNG BÌNH** (medium) · **THẤP** (low).
Nguồn: **C** = vai Critic, **S** = vai Skeptic Red Team.

### 2.1. Điều kiện auto-merge chưa chốt

| ID | Nguồn | Mức | Vấn đề | Phản hồi Actor | Judge |
|---|---|---|---|---|---|
| 1.1 | S | NGHIÊM TRỌNG | **SHA cũ.** CI và Gate 3 pass trên SHA A. Trước batch 12:00, main có thêm PR khác. Merge A lên main mới, kết quả chưa từng được test. | Có một phần: "CI trên head SHA đã rebase". Nhưng chưa nói phải kiểm tra lại **ngay lúc merge**. | Bắt buộc: kiểm tra lại CI và Gate 3 trên kết quả merge thực tế ngay trước khi merge (kiểu merge queue). |
| 1.2 | C | CAO | Actor tự đặt ngưỡng 200 dòng / 10 file và thay điều kiện 3. S:§8 ghi rõ đây là phần thảo luận mở trong PR, chưa phải quyết định. | Actor thừa nhận đây là con số đoán. | Không đưa vào PLAN như quyết định. Ghi thành phương án cần người dùng chốt. |
| 1.3 | S | CAO | Cờ "auto-merge tắt" và kill-switch chỉ là file hoặc config. Một agent có thể sửa nó trong PR hoặc ghi thẳng vào state. | Chưa có. | Bắt buộc: cờ do orchestrator giữ. Chỉ người dùng được bật, mọi thay đổi đều ghi audit. Mọi PR chạm cờ này là Red. |
| 1.4 | C | CAO | Không rõ **ai bấm merge** khi auto-merge. S:§5 cấm agent viết code tự merge PR của chính nó, nhưng PLAN chưa định nghĩa danh tính merge (bot hay orchestrator). | Chưa có. | Bắt buộc: định nghĩa danh tính merge riêng, khác Implementer, và ghi vào audit. |

### 2.2. Nhãn Red/Green và định nghĩa file nhạy cảm

| ID | Nguồn | Mức | Vấn đề | Phản hồi Actor | Judge |
|---|---|---|---|---|---|
| 2.1 | S | NGHIÊM TRỌNG | **Tự gỡ glob.** Một PR vừa xóa một glob trong `sensitive_paths.yaml` vừa sửa file mà glob đó bảo vệ. Nếu labeler đọc config từ PR head thì PR thành Green. | Có một phần: sửa danh sách thì là Red. Chưa nói labeler đọc config từ đâu. | Bắt buộc: labeler **luôn đọc config từ base branch (main)**, không bao giờ từ PR head. |
| 2.2 | S | CAO | **Lách glob.** NTFS không phân biệt hoa thường, nên `Config/x.yaml` có thể lọt qua glob `config/**`. Glob cũng có thể bị lách bằng symlink hoặc đổi tên. | Có một phần: kiểm tra cả tên cũ và tên mới. | Bắt buộc: chuẩn hóa đường dẫn (chữ thường, dấu `/`) trước khi so khớp. Thêm test cho các trường hợp hoa thường, đổi tên và xóa. |
| 2.3 | C | CAO | Danh sách của Actor thiếu các file **làm yếu CI**: `.gitattributes`, cấu hình lint/test (`pyproject.toml`, `setup.cfg`, `pytest.ini`), lockfile phụ thuộc, `tests/**`, và code nút duyệt hoặc kill-switch trên dashboard. | Chưa có. | Đưa vào danh sách khởi đầu, chờ người dùng xác nhận. |
| 2.4 | C | TRUNG BÌNH | Glob không bắt được thay đổi nguy hiểm về ngữ nghĩa trong file "an toàn". | Actor thừa nhận. | Chấp nhận như rủi ro còn lại. Gate 3 là lớp bù. |

### 2.3. Reviewer Gate 3 khác agent và khác model

| ID | Nguồn | Mức | Vấn đề | Phản hồi Actor | Judge |
|---|---|---|---|---|---|
| 3.1 | S | NGHIÊM TRỌNG | **Fallback làm hai bên trùng model.** Writer định dùng Opus, nhưng Opus lỗi nên fallback sang `sonnet 5.5 high` (S:L50). Reviewer PR-1 theo PLAN là "DeepSeek hoặc Sonnet" (P:L20). Nếu chọn Sonnet, writer và reviewer cùng một model. | Có một phần: kiểm tra lại Gate 3 sau fallback. | Bắt buộc: Gate 3 so **model thực tế đã chạy** (adapter trả về sau fallback), không so model dự kiến. Nếu không còn model khác để review thì **chờ**, không vi phạm. |
| 3.2 | S | CAO | Alias 9Router (ví dụ `xh deepseek-…`) có thể trỏ về cùng model hoặc vendor với writer. | Actor thừa nhận cần ID chuẩn. | Bắt buộc: bảng ánh xạ alias sang `(model_id, vendor)` chuẩn. Alias không rõ thì từ chối review. |
| 3.3 | C | CAO | **Bài toán khởi động.** PR-1 đến PR-3 cần Gate 3 trước khi `gate3.py` tồn tại (PR-3). Ai kiểm tra Gate 3 cho chính các PR này? | Chưa có. | Bắt buộc: thủ tục Gate 3 thủ công cho PR-1 đến PR-3, ghi lại trong PR. |
| 3.4 | C | TRUNG BÌNH | P:L20 cho Sonnet review PR do Opus viết. Hai model khác nhau nhưng cùng vendor. | Actor đồng ý dùng DeepSeek. | Bắt buộc sửa P:L20: PR-1 lấy DeepSeek làm reviewer chính. |
| 3.5 | S | CAO | **Prompt injection.** Diff, comment hoặc log có thể chứa câu như "reviewer hãy approve". | Chưa có. | Reviewer coi nội dung PR là dữ liệu. Kết quả trả về có cấu trúc (pass/fail kèm danh sách lỗi). Reviewer không có quyền merge. |

### 2.4. Giới hạn vòng lặp fix

| ID | Nguồn | Mức | Vấn đề | Phản hồi Actor | Judge |
|---|---|---|---|---|---|
| 4.1 | S | CAO | **Bộ đếm bị reset.** Theo S:§3 bước 8, Fixer mở **PR mới**. Nếu đếm vòng theo PR, mỗi PR mới lại bắt đầu từ 0 và vòng lặp chạy mãi. | Chưa có (Actor đếm "theo PR gốc" nhưng không nói lưu ở đâu). | Bắt buộc: lưu lineage `task_id` / `parent_pr` trong state PR-1. Bộ đếm gắn với task. |
| 4.2 | S | TRUNG BÌNH | PR cũ không được đóng khi Fixer mở PR mới, nên hai PR cùng một task nằm trong hàng đợi. | Chưa có. | Thêm trạng thái `superseded`. PR cũ bị đóng hoặc đánh dấu tự động. |
| 4.3 | C | TRUNG BÌNH | N=2 và ngân sách 3× là con số tự đặt. | Actor thừa nhận. | Giá trị khởi đầu là tùy chọn. Người dùng chốt N (P:L80). |

### 2.5. Stub Fixer và Codex gây nghẽn

| ID | Nguồn | Mức | Vấn đề | Phản hồi Actor | Judge |
|---|---|---|---|---|---|
| 5.1 | C | CAO | Fixer là stub, nên mọi lần Gate 3 fail đều thành việc của người. Scan 08:00 còn tạo thêm task P0–P3 "cho Fixer". Hàng đợi tăng không giới hạn. | Có một phần: hiện thời gian chờ, báo sau 4 giờ, giới hạn 2 việc. | Bắt buộc: trạng thái `awaiting-human` rõ ràng. Khi Fixer còn là stub, scan chỉ tạo task ở trạng thái này. |
| 5.2 | C | TRUNG BÌNH | `fixer_proxy` của Actor thực chất đổi SPEC (S:L15, S:L55 ghi Fixer là stub chờ Cursor). Đây là mở rộng phạm vi. | Actor để mặc định tắt. | Tùy chọn. Cần người dùng quyết. |
| 5.3 | S | THẤP | Stub Codex có thể bị gọi gián tiếp qua router. | Có: monkeypatch `socket`/`subprocess`, Codex không có trong routing. | Chấp nhận như Actor đề xuất. |

### 2.6. Khóa file trên Windows và cô lập worktree

| ID | Nguồn | Mức | Vấn đề | Phản hồi Actor | Judge |
|---|---|---|---|---|---|
| 6.1 | S | CAO | Trên Windows, `os.replace` lỗi khi dashboard hoặc agent đang mở file đích. Nhiều tiến trình cùng append vào `audit.log.jsonl` thì các dòng bị xen lẫn. | Có: một writer duy nhất, inbox, thử lại khi gặp `PermissionError`. | Bắt buộc đưa vào PR-1, kèm **test tranh chấp nhiều tiến trình chạy trên Windows**. |
| 6.2 | S | CAO | Orchestrator crash để lại `.git/index.lock`, khóa worktree hoặc worktree mồ côi. Lần chạy sau `git worktree add` lỗi vì nhánh đang được dùng ở worktree khác. | Có một phần: prune hằng đêm. | Bắt buộc: dọn dẹp và khôi phục khi khởi động. Tên worktree và nhánh lấy theo task ID duy nhất. |
| 6.3 | S | CAO | "Audit log append-only" chỉ là một file, tiến trình nào cũng cắt ngắn hoặc sửa được. | Chưa có. | Bắt buộc: số thứ tự kèm hash chain, kiểm tra khi khởi động, chỉ orchestrator được ghi. |
| 6.4 | C | TRUNG BÌNH | Các worktree dùng chung object store. `git gc` tự động có thể chạy đúng lúc agent đang làm. Đường dẫn dài vượt MAX_PATH. | Có: `core.longpaths`, đường dẫn ngắn. | Tắt `gc.auto` trong lúc chạy. Loại trừ Defender là tùy chọn vì cần quyền admin. |

### 2.7. Phụ thuộc bridge :20129 và 9Router :20128

| ID | Nguồn | Mức | Vấn đề | Phản hồi Actor | Judge |
|---|---|---|---|---|---|
| 7.1 | C | CAO | 9Router là điểm lỗi duy nhất cho 2 trên 3 tuyến. Khi nó sập, mọi việc dồn sang Claude: tốn chi phí và **mất đa dạng model cho Gate 3**. | Có: circuit breaker, fallback, ghi ledger. | Bắt buộc theo nguyên tắc ở 3.1: fallback không được vi phạm Gate 3. Có cảnh báo chi phí. |
| 7.2 | S | TRUNG BÌNH | Một dịch vụ khác chiếm cổng 20128 hoặc 20129. Probe TCP vẫn báo "khỏe". | Có một phần: probe HTTP nhẹ. | Health check phải xác minh đúng dịch vụ, ví dụ endpoint trả danh sách model mong đợi. |
| 7.3 | C | THẤP | Trạng thái dịch vụ chưa có trong view Agent status (S:§6). | Có: hiện trên Agent status. | Chấp nhận. |

### 2.8. Batch release và revert khi có nhiều PR

| ID | Nguồn | Mức | Vấn đề | Phản hồi Actor | Judge |
|---|---|---|---|---|---|
| 8.1 | S | NGHIÊM TRỌNG | P:L46 viết "revert đưa main về trạng thái trước release". Nếu làm theo kiểu reset về tag kèm force push, mọi hotfix merge **sau** release sẽ mất. | Có một phần: revert cả batch về tag trước. | Bắt buộc sửa câu chữ: dùng **revert commit theo thứ tự ngược**, không force push. Định nghĩa rõ trạng thái đích là các thay đổi của batch bị gỡ, còn commit sau release vẫn giữ. |
| 8.2 | C | CAO | PLAN không có manifest release, nên không biết batch gồm những PR nào. | Có: tag và manifest. | Bắt buộc: schema manifest nằm trong state của PR-1. |
| 8.3 | S | CAO | PR trong batch phụ thuộc lẫn nhau. Revert từng phần hoặc đưa lại từng PR thì PR sau hỏng. | Có một phần: chia đôi để tìm lỗi. | Ghi phụ thuộc vào manifest. Khi đưa lại, giữ đúng thứ tự phụ thuộc. |
| 8.4 | C | CAO | "Kiểm tra nhanh read-only" (S:L38) và "release" chưa được định nghĩa: release ra đâu, kiểm tra những gì. | Chưa có. | Câu hỏi mở bắt buộc phải trả lời trước PR-4. |

### 2.9. Thời điểm digest

| ID | Nguồn | Mức | Vấn đề | Phản hồi Actor | Judge |
|---|---|---|---|---|---|
| 9.1 | S | CAO | PR được duyệt lúc 16:59, đúng lúc batch 17:00 đang chạy. Nó lọt vào batch một nửa, hoặc vào batch mà không qua digest. | Có: chốt danh sách và hạn duyệt. | Chấp nhận cơ chế chốt danh sách. Giờ hạn duyệt chờ người dùng chốt. |
| 9.2 | C | CAO | S:L12 cho Approver-Agent (một agent) bấm merge PR Red. Nhưng S:L16 nói người dùng là người duyệt cuối. PLAN chưa nói duyệt được ghi ở đâu. | Chưa có. | Bắt buộc: Approver-Agent chỉ merge Red khi có **bản ghi duyệt của người dùng** trong state. |
| 9.3 | C | TRUNG BÌNH | S:§7 xếp scheduled jobs và digest vào "Tính năng sau", nhưng PR-4 lại làm chúng. Hai tài liệu mâu thuẫn về phạm vi. | Chưa có. | Bắt buộc ghi rõ trong PLAN: hoặc PR-4 đi trước lộ trình của SPEC, hoặc lùi phần scheduler. |
| 9.4 | S | TRUNG BÌNH | Máy ngủ hoặc đồng hồ lệch làm lỡ digest hay release. | Có: chính sách lần chạy bị lỡ, test đồng hồ cố định. | Chấp nhận. |

### 2.10. Maintenance P0–P3 chồng lấn với release

| ID | Nguồn | Mức | Vấn đề | Phản hồi Actor | Judge |
|---|---|---|---|---|---|
| 10.1 | S | CAO | Scan 08:00 tạo P0 cho một lỗi đã được revert hoặc đã có incident. Kết quả là task và incident bị trùng. | Chưa có. | Khử trùng bằng fingerprint lỗi, đối chiếu với các incident đang mở. |
| 10.2 | C | TRUNG BÌNH | P0 phải chờ digest 11:30 và release 12:00. Lỗi báo lúc 09:00 theo nhánh sửa lỗi (S:L42) chờ gần 23 giờ mới được scan. | Có một phần: P0 vào digest gần nhất. | Câu hỏi mở: có cần đường hotfix ngoài batch cho P0 không. |
| 10.3 | S | TRUNG BÌNH | Scan chạy đúng lúc release hoặc revert, nên đọc phải trạng thái dở dang. | Có: `release.lock`, scan trên tag. | Chấp nhận. |

### 2.11. Dữ liệu Artifact đúng hay chỉ "nhìn như xong"

| ID | Nguồn | Mức | Vấn đề | Phản hồi Actor | Judge |
|---|---|---|---|---|---|
| 11.1 | C | CAO | PR-5 bắt đầu bằng dữ liệu giả (P:L61). Đây đúng là rủi ro "nhìn như xong" mà S:§9.4 cảnh báo. | Có: watermark FAKE DATA, contract test. | Bắt buộc: tiêu chí chấp nhận PR-5 phải kiểm được, ví dụ fixture state cho ra đúng số liệu hiển thị và tổng cost bằng tổng ledger. |
| 11.2 | S | CAO | P:L54 nói dashboard "đọc read-only", nhưng P:L55 lại cho có nút kill-switch và nút duyệt. Một agent có thể ghi thẳng file duyệt và giả làm người dùng. | Chưa có. | Bắt buộc: mọi thao tác ghi đi qua orchestrator (inbox) và ghi audit. Phải phân biệt được bản ghi duyệt của người dùng với bản ghi của agent. |
| 11.3 | S | TRUNG BÌNH | Dashboard đọc phải JSON đang ghi dở, hoặc inbox chưa được gộp vào state. | Có: thời điểm dữ liệu, banner dữ liệu cũ. | Chấp nhận. Reader bỏ qua và thử lại khi parse lỗi. |
| 11.4 | C | TRUNG BÌNH | Chưa rõ Muse Artifact có đọc được file cục bộ hay không. | Actor thừa nhận giới hạn của Muse. | Câu hỏi mở. |

### 2.12. Chi phí token và nạp lại log

| ID | Nguồn | Mức | Vấn đề | Phản hồi Actor | Judge |
|---|---|---|---|---|---|
| 12.1 | S | TRUNG BÌNH | Ledger đếm trùng khi retry hoặc fallback. | Chưa có. | Bắt buộc trong PR-1: mỗi lần gọi có idempotency key, và ghi model thực tế đã chạy. |
| 12.2 | C | TRUNG BÌNH | Ledger chỉ ghi lại, không có gì chặn việc tiêu tốn. | Có một phần: ngân sách 3× cho vòng fix. | Tùy chọn: trần ngân sách theo task và theo ngày, kèm cảnh báo. |
| 12.3 | C | TRUNG BÌNH | `agy` có thể không báo token, nên so sánh chi phí giữa các tuyến bị sai. | Có: ghi `estimated=true`. | Chấp nhận. |
| 12.4 | C | THẤP | Reviewer và Fixer bị nạp toàn bộ log. | Có: bàn giao ≤2 KB, phiên mới. | Chấp nhận. Mẫu bàn giao là tùy chọn và cần đo (S:§9.5). |

### 2.13. Vấn đề xuyên suốt

| ID | Mức | Vấn đề |
|---|---|---|
| X.1 | CAO | Bài toán khởi động Gate 3 và nhãn cho chính PR-1 đến PR-3 (xem 3.3). |
| X.2 | CAO | Không có nơi lưu bản ghi duyệt và danh tính merge (xem 1.4, 9.2, 11.2). |
| X.3 | TRUNG BÌNH | State machine "bước 1–13" (P:L18) thiếu các trạng thái `blocked`, `superseded`, `cancelled`, `awaiting-human`. |
| X.4 | TRUNG BÌNH | Mâu thuẫn phạm vi giữa S:§7 và PR-4 (xem 9.3). |
| X.5 | TRUNG BÌNH | Một số tiêu chí chấp nhận không đo được, ví dụ "nhãn tái lập được" (P:L37) và "dữ liệu thật" (P:L55). |
| X.6 | THẤP | PR-1 quá lớn: state, queue, heartbeat, ledger, audit và worktree cùng lúc. Nên cân nhắc tách. |

---

## 3. Phán quyết của Judge

**Phán quyết: CHẤP NHẬN CÓ THAY ĐỔI (Accept with changes).**

Lý do:

- Khung PLAN.md đúng: fail-closed, thứ tự xây lõi trước, câu hỏi mở được để mở, mục 9 được coi là giả thuyết. Không cần viết lại.
- Có **5 điểm NGHIÊM TRỌNG** (1.1, 2.1, 3.1, 8.1, cùng tổ hợp X.2/11.2 về danh tính duyệt). Phần lớn liên quan tới **schema state và audit của PR-1**. Nếu không chốt trước, PR-1 sẽ phải làm lại.
- Các con số Actor đưa ra (200 dòng, N=2, 3×, 5 PR, 4 giờ, 25 phút) **không được ghi vào PLAN như quyết định**. Chúng chỉ là giá trị khởi đầu chờ người dùng chốt, vì S:§8 vẫn là phần thảo luận mở.
- Việc bật auto-merge vẫn bị chặn cho đến khi người dùng chốt 4 điều kiện. Điều này không chặn PR-1 đến PR-3.

---

## 4. Thay đổi BẮT BUỘC trong PLAN trước PR-1

1. **Thủ tục Gate 3 khởi động:** định nghĩa Gate 3 thủ công cho PR-1 đến PR-3. Sửa P:L20 để PR-1 (Opus viết) lấy **DeepSeek làm reviewer chính**, không dùng Sonnet cùng vendor. *(3.3, 3.4, X.1)*
2. **Mở rộng state machine của PR-1:** thêm `blocked`, `superseded`, `cancelled`, `awaiting-human`. Thêm lineage `task_id` / `parent_pr` để bộ đếm vòng fix gắn với task, không gắn với PR. *(4.1, 4.2, 5.1, X.3)*
3. **Một writer duy nhất:** chỉ orchestrator ghi `state/*.json` và audit log. Agent và dashboard ghi qua inbox. Thử lại khi gặp `PermissionError`. Reader chịu được file đang ghi dở. Tiêu chí chấp nhận có **test tranh chấp nhiều tiến trình trên Windows**. *(6.1, 11.3)*
4. **Toàn vẹn audit log:** số thứ tự kèm hash chain, kiểm tra khi khởi động. Mỗi bản ghi có `(agent, model thực tế, vendor, SHA, task_id)`. *(6.3, 3.1)*
5. **Ledger:** ghi model thực tế sau fallback, idempotency key cho mỗi lần gọi, cờ `estimated`. *(12.1, 12.3, 3.1)*
6. **Vòng đời worktree:** đường dẫn ngắn và tên theo task ID duy nhất. Dọn worktree mồ côi và `index.lock` khi khởi động. Tắt `gc.auto` trong lúc chạy. *(6.2, 6.4)*
7. **Bản ghi duyệt và danh tính merge:** định nghĩa nơi lưu bản ghi duyệt (trong schema state của PR-1). Chỉ người dùng tạo được bản ghi duyệt, qua orchestrator, và có audit. Danh tính merge phải khác Implementer. Approver-Agent chỉ merge Red khi có bản ghi duyệt của người dùng. *(1.4, 9.2, 11.2, X.2)*
8. **Cờ auto-merge và kill-switch:** lưu trong state do orchestrator giữ, mặc định tắt. Chỉ người dùng được bật, có audit. Ghi nguyên tắc: **labeler đọc `sensitive_paths.yaml` từ base branch** và chuẩn hóa đường dẫn trước khi so khớp. Nguyên tắc ghi ngay, phần cài đặt làm ở PR-3. *(1.3, 2.1, 2.2)*
9. **Nguyên tắc merge an toàn:** kiểm tra lại CI và Gate 3 trên kết quả merge thực tế ngay trước khi merge. Ghi vào PLAN ngay, cài đặt ở PR-4. *(1.1)*
10. **Ngữ nghĩa revert:** sửa P:L46 thành "revert commit theo thứ tự ngược, không force push, giữ các commit sau release". Thêm schema manifest release (tag, danh sách SHA, phụ thuộc) vào state của PR-1. *(8.1, 8.2, 8.3)*
11. **Fallback không được vi phạm Gate 3:** Gate 3 so model và vendor **thực tế** qua bảng ánh xạ alias chuẩn. Nếu không còn model hợp lệ để review thì chờ. *(3.1, 3.2, 7.1)*
12. **Làm rõ phạm vi:** ghi rõ việc PR-4 làm scheduler và digest là đi trước S:§7 ("Tính năng sau"), hoặc lùi phần đó lại. *(9.3, X.4)*

---

## 5. Thay đổi TÙY CHỌN

- Shadow mode cho auto-merge (gợi ý 2 tuần hoặc 20 PR) trước khi bật. *(1.2)*
- Giá trị khởi đầu: N=2 vòng fix, ngân sách token 3× chi phí gốc, tối đa 5 PR mỗi batch, cảnh báo fix list sau 4 giờ, Fixer chạy tối đa 2 việc cùng lúc.
- `fixer_proxy` (tắt mặc định) để giảm nghẽn khi chưa có Cursor. Cần người dùng đồng ý vì nó đổi vai trò stub trong SPEC. *(5.2)*
- Mở rộng danh sách glob khởi đầu: `.gitattributes`, cấu hình lint/test, lockfile, `tests/**`, code nút duyệt và kill-switch. *(2.3)*
- Circuit breaker (3 lỗi / 5 phút). Health check xác minh đúng dịch vụ trên cổng 20128 và 20129. *(7.2)*
- Khử trùng P0 bằng fingerprint và đối chiếu incident. *(10.1)*
- Trần ngân sách token theo task và theo ngày, kèm cảnh báo. *(12.2)*
- Mẫu bàn giao ≤2 KB với 5 trường. Đo hiệu quả theo S:§9.5. *(12.4)*
- Loại trừ Windows Defender cho thư mục worktree và state (cần quyền admin). *(6.4)*
- Dashboard: watermark FAKE DATA, hiện thời điểm dữ liệu, banner dữ liệu cũ, phân biệt "không có dữ liệu" với "0". *(11.1, 11.3)*
- Tách PR-1 thành hai PR: state/audit/ledger và heartbeat/worktree. *(X.6)*

---

## 6. Câu hỏi mở (người dùng quyết định)

1. **4 điều kiện auto-merge** (S:§8): giữ điều kiện 3 hay thay bằng giới hạn diff hoặc bắt buộc có test? Comment do bot tạo có tính là "chưa xử lý" không? Merge ngay hay gom vào batch? PR Green từ vòng Fixer có cần thêm điều kiện không?
2. Gate 3 cần một hay hai reviewer? Có bắt buộc khác **vendor** không, hay chỉ cần khác model?
3. Ai được sửa `sensitive_paths.yaml`, và danh sách khởi đầu gồm những gì?
4. N vòng fix bằng bao nhiêu, và timeout heartbeat bao lâu? (P:L80)
5. Hạn duyệt PR Red trước batch là mấy giờ (ví dụ 11:55 / 16:55)?
6. P0 có đường hotfix ngoài batch không, hay luôn chờ digest và release?
7. "Release" nghĩa là gì với một control plane chạy cục bộ: release ra đâu? Bước "kiểm tra nhanh read-only" kiểm tra cụ thể những gì?
8. Muse Artifact có đọc trực tiếp file state cục bộ được không, hay cần một bước xuất snapshot?
9. Có cho phép `fixer_proxy` trước khi có Cursor không?
10. Có đặt trần ngân sách token không, và mức bao nhiêu?
11. Có chấp nhận loại trừ Windows Defender cho thư mục worktree và state không?
12. Mục nào của checklist S:§9 sẽ được đo để giữ hay thay? (P:L81)
13. Có muốn chạy lại Critic, Skeptic và Judge bằng subagent độc lập để kiểm chứng tài liệu này không?
