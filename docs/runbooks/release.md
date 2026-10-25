# Runbook — Release thủ công (PR-4a)

> MVP: **không có merge tự động, không có batch release tự động** (PR-4b). Người dùng làm
> mọi bước dưới đây bằng tay. Mốc giờ theo Asia/Seoul (KST). `R<n>` là quy tắc ở SPEC mục 5.3.

## "Release" là gì với control plane cục bộ (SPEC mục 8 câu 14)

- **Release = một annotated tag trên `main`** (ví dụ `rel-20261026-1200`), đặt trên commit
  mà người dùng đã merge qua GitHub UI và có check `ci` xanh.
- Control plane chạy trên máy Windows này từ **một checkout ở chế độ detached HEAD tại tag
  release** ("runtime checkout"). Release ra = chuyển runtime checkout sang tag mới. Không
  có server nào khác để deploy.
- Runtime state luôn nằm ngoài repo (`%LOCALAPPDATA%\harness`, R13) và tương thích N-1 (R2),
  nên đổi tag (kể cả sau revert) không làm hỏng state.
- Manifest của release luôn tính từ `git log prevTag..newTag` (`release/manifest.py`, R10),
  không bao giờ từ danh sách tự ghi.

## Kill-switch (R8, R15)

- File: `%LOCALAPPDATA%\harness\killswitch.json` (`killswitch.file` trong `config/harness.yaml`,
  tương đối thì tính từ `state_dir`). **Thiếu file, không đọc được hay sai định dạng = ĐANG BẬT.**
  Chỉ `"state": "off"` với `"set_by": "user"` mới là tắt.
- **Chỉ người dùng** đổi file này, bằng tay. Không có hàm ghi trong code, không có nút trên
  dashboard. Agent và pipeline không bao giờ ghi file này.
- Kill-switch bật → `Adapter.run/probe`, `Gate3.review`, vòng fix, `ApprovalMirror.sync` và
  shadow evaluator dừng trước mọi hiệu ứng (ném `KillSwitchEngaged`). Fold event và heartbeat
  vẫn chạy để audit/dashboard thấy trạng thái lúc dừng.
- **Best-effort, KHÔNG chống giả mạo:** cho tới khi tách Windows user riêng cho agent, agent
  cùng OS user vẫn có thể ghi file này. Đây là phanh tay cho phần mềm hợp tác, không phải ranh
  giới bảo mật. Mỏ neo tin cậy vẫn là GitHub login + branch protection + required check `ci`.

Lệnh PowerShell (người dùng chạy):

```powershell
$ks = Join-Path $env:LOCALAPPDATA 'harness\killswitch.json'
New-Item -ItemType Directory -Force -Path (Split-Path $ks) | Out-Null
# BẬT (dừng mọi hành động tự động):
[ordered]@{v=1; state='on'; set_by='user'; set_at=(Get-Date).ToString('o'); reason='release'} | ConvertTo-Json | Set-Content -Encoding utf8 -Path $ks
# TẮT (cho phép lại) - chỉ sau khi kiểm tra read-only đạt:
[ordered]@{v=1; state='off'; set_by='user'; set_at=(Get-Date).ToString('o'); reason='readonly check ok'} | ConvertTo-Json | Set-Content -Encoding utf8 -Path $ks
# Xem trạng thái (chỉ đọc; exit 0 = tắt, 3 = bật):
python -m orchestrator.killswitch
```

Phanh khẩn cấp: xóa file (`Remove-Item $ks`) cũng là BẬT.

## Các bước release

1. **Điều kiện:** mọi PR của đợt này đã được người dùng merge qua GitHub UI; check `ci` của
   GitHub Actions xanh trên SHA `main` sẽ gắn tag (R9, R14). Không có incident đang `open`/`frozen`.
2. **Bật kill-switch** (lệnh ở trên). Giữ bật cho tới hết bước 7.
3. **Ghi lại seq audit hiện tại** (mốc để so lỗi sau release):

   ```powershell
   python -m release.readonly_check seq
   ```

4. **Gắn tag** trên đúng SHA của `origin/main` và đẩy tag (chỉ thêm tag, không force):

   ```powershell
   git fetch origin
   git tag -a rel-YYYYMMDD-HHMM -m "release" origin/main
   git push origin rel-YYYYMMDD-HHMM
   ```

5. **Manifest** so với tag release trước, lưu vào state (ngoài repo):

   ```powershell
   python -m release.manifest <tag-trước> rel-YYYYMMDD-HHMM > "$env:LOCALAPPDATA\harness\releases\rel-YYYYMMDD-HHMM.json"
   ```

   (tạo thư mục `releases` nếu chưa có). Đọc lại danh sách PR trong manifest.
6. **Chuyển runtime checkout** sang tag mới:

   ```powershell
   git -C <runtime-checkout> fetch origin --tags
   git -C <runtime-checkout> switch --detach rel-YYYYMMDD-HHMM
   ```

7. **Kiểm tra read-only** (bước 11, `docs/runbooks/readonly_check.md`), kill-switch vẫn bật:

   ```powershell
   python -m release.readonly_check run --since-seq <seq ở bước 3>
   ```

   - **Đạt** (exit 0, `"ok": true`): người dùng **tắt kill-switch**. Xong.
   - **Không đạt**: giữ kill-switch bật, làm `docs/runbooks/revert.md` và mở incident theo
     `docs/runbooks/incident.md`.

## Shadow evaluator (không phải merge)

`release/shadow_eval.py` chỉ ghi bản ghi audit `action="would-merge"` (`result` = `yes`/`no`
kèm điều kiện nào đạt) cho mỗi PR được đánh giá. Nó **không bao giờ gọi API merge** và không
bật được auto-merge. 4 điều kiện là **OPEN DISCUSSION** (SPEC mục 8); ngưỡng shadow 2 tuần hoặc
20 PR là mặc định tạm (`shadow` trong `config/harness.yaml`, R16). Đủ ngưỡng chỉ nghĩa là đủ dữ
liệu để người dùng xem xét trước PR-4b.

## Không được làm

- Không `git push --force`, `--force-with-lease`, `git reset` trên `main` hay trên tag release.
- Không xóa hay dời tag release đã đẩy. Sửa lỗi = release mới (hoặc revert rồi release mới).
- Không tắt kill-switch qua dashboard, script của agent, hay khi kiểm tra read-only chưa đạt.
