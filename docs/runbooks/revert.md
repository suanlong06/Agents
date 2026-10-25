# Runbook — Revert thủ công (PR-4a, R10)

> MVP: revert làm **tay**. Revert tự động là PR-4b. Mọi thay đổi lên `main` đi **qua PR**,
> người dùng merge qua GitHub UI (R7). **Không force-push, không reset.**

## Khi nào

- Kiểm tra read-only sau release không đạt (`docs/runbooks/readonly_check.md`).
- Có bug do release (bước 12 ở SPEC mục 3).

## Các bước

1. **Đóng băng:** bật kill-switch (lệnh ở `docs/runbooks/release.md`). Giữ bật tới khi xong.
2. **Mở incident** (`docs/runbooks/incident.md`), `status: open`.
3. **Manifest và kế hoạch revert** của release lỗi (tính từ `git log`, không từ danh sách tự ghi):

   ```powershell
   python -m release.manifest <tag-trước> <tag-lỗi> --revert-plan
   ```

   Lệnh in ra mỗi PR một `git revert`, theo **thứ tự ngược manifest** (mới → cũ). Merge commit
   có `-m 1` (giữ phía `main`).
4. **Nhánh revert từ `main` HIỆN TẠI** (không phải từ tag), để giữ mọi commit merge sau release:

   ```powershell
   git fetch origin
   git switch -c revert/<tag-lỗi> origin/main
   ```

5. **Chạy từng lệnh `git revert` theo đúng thứ tự in ra.** Sau mỗi lệnh kiểm `git status`.
6. **Conflict → dừng, đóng băng, báo động.** Không tự giải conflict:

   ```powershell
   git diff --name-only --diff-filter=U   # ghi lại file conflict vào incident
   git revert --abort
   ```

   - Kill-switch giữ **bật**.
   - Incident: `status: frozen`, `trigger: revert_conflict` (nếu mở vì conflict), điền
     `revert.conflict = {sha, files}`.
   - Báo động: comment vào GitHub issue của incident (và thông báo Muse nếu đang chạy).
   - Người dùng quyết định cách sửa (PR sửa tay, hoặc revert một phần qua PR riêng).
7. **Đẩy nhánh và mở PR** (không force):

   ```powershell
   git push origin revert/<tag-lỗi>
   ```

   PR đi qua labeller, CI, Gate 3 như mọi PR. Revert chạm `release/**`, `config/**`,
   `schemas/**`... là **Owner-only**: chỉ người dùng duyệt và merge (rủi ro còn mở D§2.8 #8.3
   khi người dùng vắng).
8. **Người dùng merge PR revert** qua GitHub UI. Vì nhánh revert đi ra từ `main` và chỉ THÊM
   commit, merge là fast-forward hoặc merge thường — không bao giờ cần force-push.
9. **Release lại** theo `docs/runbooks/release.md` (tag mới, manifest mới, kiểm tra read-only).
10. Cập nhật incident: `revert.status: merged`, `reverted` (SHA theo thứ tự ngược manifest),
    `status: mitigated` → `resolved` sau khi kiểm tra read-only đạt.

## Được kiểm bằng test

`tests/test_manifest.py` (Windows CI): manifest khớp `git log --first-parent prevTag..newTag`;
áp kế hoạch revert trên repo fixture rồi đẩy lên remote có `receive.denyNonFastForwards=true`
**không cần force**, mọi commit release và sau release vẫn còn trong lịch sử, thay đổi của
release bị gỡ, commit sau release được giữ; conflict làm `git revert` dừng, `--abort` trả về
trạng thái sạch.

## Không được làm

- `git push --force`, `--force-with-lease`, `git reset --hard`, xóa/dời tag release.
- Revert bằng cách checkout tag cũ rồi đẩy đè `main`.
- Tự giải conflict của revert, hoặc tắt kill-switch khi incident còn `open`/`frozen`.
