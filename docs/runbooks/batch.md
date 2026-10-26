# Runbook — Scheduler, digest, batch release, revert tự động (PR-4b)

> **Trạng thái sau khi merge PR-4b: CHƯA KÍCH HOẠT.** `scheduler.enabled: false` và
> `batch.merge_identities: []` trong `config/harness.yaml`. Không có tác vụ Windows Task Scheduler
> nào được tạo. Kích hoạt là **quyết định riêng của người dùng** (mục "Kích hoạt" bên dưới).
> Mốc giờ theo Asia/Seoul (KST). `R<n>` là quy tắc ở SPEC mục 5.3.

## Không có auto-merge

- **Auto-merge vẫn TẮT và không được cài ở bất kỳ dạng nào.** Batch chỉ merge PR mà **người dùng đã
  duyệt trên GitHub** (review `APPROVED` của login trong `github.owner_logins`), gắn
  **(head SHA, patch-id)**, nộp **trước hạn duyệt**. PR chưa được duyệt không bao giờ được merge,
  kể cả khi CI xanh, Gate 3 pass hay shadow evaluator ghi "would-merge yes".
- 4 điều kiện auto-merge ở SPEC mục 8 vẫn là **OPEN DISCUSSION** và chưa có dữ liệu shadow: không
  config, cờ hay đường code nào ở PR-4b merge PR chưa được duyệt.
- PR **Owner-only** không bao giờ vào batch: người dùng merge tay qua GitHub UI.
- PR revert do runner mở **không** được runner merge: chỉ người dùng duyệt và merge.

## OS user và mức tin cậy (R15) — điều kiện triển khai còn mở

- Runner chạy dưới **OS user hiện tại** (cùng user với người dùng và agent). Token merge nằm trong
  biến môi trường của user đó; state, audit, incident và kill-switch **chỉ là best-effort, KHÔNG
  chống giả mạo**.
- **Tách Windows user riêng, ít quyền cho agent + ACL trên thư mục state** (và đổi `state_dir`
  khỏi `%LOCALAPPDATA%` của người dùng) vẫn là **điều kiện triển khai còn mở**, phải xong **trước khi
  bật bất kỳ auto-merge hay thao tác ghi nào từ dashboard** (SPEC mục 5.2, 7). PR-4b không làm việc
  này và không thay đổi tuyên bố tin cậy.
- Mỏ neo tin cậy vẫn là GitHub login + branch protection + required check `ci` (R14): GitHub vẫn
  đòi review CODEOWNERS và check `ci` cho mỗi lệnh merge của batch.

## Lịch (mặc định tạm, R16)

| Cửa sổ | Digest (đóng băng) | Hạn duyệt | Batch |
|---|---|---|---|
| trưa | 11:30 | 11:55 | 12:00 |
| chiều | 16:30 | 16:55 | 17:00 |

- `zoneinfo` + gói `tzdata` (R17). Cấu hình ở `scheduler` trong `config/harness.yaml`.
- **Lỡ cửa sổ** (quá `grace_minutes`, mặc định tạm 10 phút, vì máy tắt, runner dừng hay kill-switch
  bật): job bị **bỏ qua và thông báo**, không chạy bù. Batch không có digest đóng băng của chính cửa
  sổ đó thì cũng bỏ qua.
- Job đang chạy mà runner chết → `interrupted`, không chạy lại, báo động (có thể đã merge một phần).

## Digest (11:30 / 16:30)

`release/digest.py` ghi **một lần** `<state_dir>\digests\<window_id>.json`
(`schemas/digest.schema.json`): mỗi PR đang mở vào `main` với head SHA, merge-base, **patch-id**
(`git diff | git patch-id --stable`), nhãn (labeller, config đọc từ base) và CI trên head.
`batch_eligible` = CI xanh, không Owner-only, có patch-id. File đã có thì không bao giờ ghi lại; PR
mở hay đổi head sau giờ digest thuộc cửa sổ sau. Người dùng nhận thông báo kèm hạn duyệt.

## Batch release (12:00 / 17:00)

`release/batch.py`, giữ `release.lock` (`purpose: batch_release`) **từ đầu tới hết bước kiểm tra
và revert** (R10):

1. Kill-switch tắt; không có incident `open`/`frozen`; có digest đóng băng.
2. **Danh tính merge:** `batch.merge_identities` khác rỗng; login của token trong
   `batch.merge_token_env` (`GET /user`) thuộc allowlist; danh tính merge **khác tác giả PR**.
3. Mỗi PR `batch_eligible` (tối đa `max_prs` = 5, còn lại hoãn): còn mở, head == head đóng băng, CI
   xanh, patch-id tính lại == patch-id đóng băng, review mới nhất của người dùng là `APPROVED` trên
   đúng head và trước hạn → **phê duyệt gắn (head SHA, patch-id)**.
4. **Tích hợp tuần tự, không dùng merge queue native:** nhánh `harness/batch/<window>-<n>` từ `main`;
   merge từng PR theo thứ tự digest. Bước nào conflict, có patch-id khác patch-id đã duyệt (thông
   báo kèm **range-diff**) hoặc nhãn Owner-only tại SHA merge → PR đó rớt, phê duyệt hết hiệu lực,
   nhánh tích hợp được dựng lại không có nó.
5. **CI trên kết quả tích hợp:** PR **draft** từ nhánh tích hợp (`ci.yml` chạy trên `pull_request`);
   chờ check run `ci` của GitHub Actions trên SHA cuối (tối đa `ci_timeout_minutes`). Không xanh →
   không merge gì. PR tích hợp **không bao giờ được merge**, batch đóng nó.
6. **Merge tuần tự** từng PR vào `main` (`PUT pulls/N/merge` với `sha` = head đã duyệt, kiểu merge
   commit). Trước mỗi PR: đọc lại phê duyệt trên GitHub, kiểm `main` chưa bị đổi ngoài batch. Sau mỗi
   PR: cây của commit merge phải == cây bước tích hợp đã qua CI. Lệch hay dừng giữa chừng sau khi đã
   merge → incident `frozen`, không tag.
7. **Tag** `rel-<window_id>`, **manifest** từ `git log prevTag..newTag` (`release/manifest.py`) lưu ở
   `<state_dir>\releases\`, rồi **kiểm tra read-only của chính tag** (`release/release_check.py`):
   worktree detached tại tag, `python -E -s -B -m release.readonly_check run --since-seq N
   --expect-tag <tag> --release-lock-held` với `cwd` = worktree đó (F2).
8. **Không đạt → revert tự động (R10)**, `release/auto_revert.py`: incident (`opened_by: runner`);
   nhánh `revert/<tag>` từ `main` hiện tại; `git revert` từng PR theo thứ tự **ngược** manifest
   (merge commit `-m 1`); không force-push, không reset; đẩy nhánh và **mở PR revert chờ người dùng
   duyệt**. Conflict → `git revert --abort`, incident `frozen` (`revert.conflict = {sha, files}`),
   báo động, không tự giải conflict.

Kill-switch được kiểm **trước mọi thao tác tự động** (tick, mỗi job, mỗi lệnh GitHub/git, mỗi merge,
tag, kiểm tra, revert). Bật giữa chừng → dừng ngay. Runner **không ghi được kill-switch** (chỉ người
dùng đổi, R8): khi mở incident nó thông báo yêu cầu người dùng bật kill-switch.

Báo cáo: `<state_dir>\batches\<window_id>.json` (`schemas/batch.schema.json`, `auto_merge: disabled`),
audit agent `batch`, thông báo Muse ở `<state_dir>\notifications\`.

## Xem trạng thái (chỉ đọc)

```powershell
python -m release.runner status
```

In: `scheduler_enabled`, kill-switch, allowlist danh tính merge, có token merge không, lịch hôm nay,
incident đang mở. Không ghi gì.

## Kích hoạt (quyết định riêng của người dùng, SAU khi merge PR-4b)

Chưa làm trong PR-4b. Trước khi kích hoạt, người dùng cân nhắc điều kiện "OS user" ở trên.

1. Tạo bot/danh tính merge trên GitHub (khác bot push của agent), cấp quyền merge tối thiểu; đặt
   token vào biến môi trường `HARNESS_MERGE_TOKEN` của user chạy runner (không ghi vào file).
2. Thêm login đó vào `batch.merge_identities` qua một PR (Owner-only vì là `config/**`).
3. Tạo clone làm việc của batch (ngoài repo):

   ```powershell
   git clone https://github.com/suanlong06/Agents.git "$env:LOCALAPPDATA\hw\rel"
   git -C "$env:LOCALAPPDATA\hw\rel" config user.name "harness-batch"
   git -C "$env:LOCALAPPDATA\hw\rel" config user.email "harness-batch@users.noreply.github.com"
   ```

4. Đặt `scheduler.enabled: true` qua một PR (Owner-only).
5. Chạy `python -m release.runner serve` từ runtime checkout tại tag release (`release.md`), hoặc
   đăng ký một tác vụ Task Scheduler chạy lệnh đó. Tắt kill-switch theo `release.md` khi sẵn sàng.

Branch protection: nếu bật "Require branches to be up to date before merging", GitHub từ chối merge
PR thứ hai trở đi của batch (head không chứa merge của PR trước); batch dừng, đóng băng và báo động.

## Sau sự cố

- Incident `open`/`frozen` chặn mọi batch. Người dùng xử lý theo `incident.md` / `revert.md`: duyệt
  và merge PR revert (nếu có), release lại, rồi chuyển incident sang `mitigated` → `resolved`.
- `scheduler.json` hỏng → runner dừng (fail-closed); người dùng kiểm tra rồi xóa file.

## Không được làm

- Không bật merge cho PR chưa có phê duyệt của người dùng, dưới bất kỳ cờ hay config nào.
- Không `git push --force`, `--force-with-lease`, `git reset` trên `main`, tag release hay nhánh revert.
- Không merge PR tích hợp `harness/batch/*`; không merge PR revert thay người dùng.
- Không kích hoạt lịch trước khi người dùng quyết định.
