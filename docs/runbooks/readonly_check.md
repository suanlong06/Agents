# Runbook — Kiểm tra read-only (bước 11; SPEC mục 8 câu 14)

Code: `release/readonly_check.py`. Test: `tests/test_readonly_check.py` (chạy trên Windows CI).

## Định nghĩa

"Kiểm tra read-only" gồm hai phần.

### A. Bộ kiểm "hành động đề xuất không ghi gì" — `verify_readonly(action, watch=[...])`

Chạy một hành động đề xuất và chỉ **đạt** khi cả hai lớp không thấy thao tác ghi nào:

1. **Chặn trong process** (audit hook của Python, PEP 578). Trong lúc kiểm, các thao tác sau
   bị **chặn trước khi xảy ra** (ném `ReadOnlyViolation`, lớp con của `PermissionError`) và
   được ghi lại, kể cả khi hành động tự nuốt lỗi:
   - mở file ở chế độ ghi (`w`, `a`, `x`, `+`, hoặc cờ `O_WRONLY`/`O_RDWR`/`O_CREAT`/
     `O_APPEND`/`O_TRUNC`); xóa, đổi tên, `os.replace`; tạo/xóa thư mục; `chmod`, `utime`,
     `truncate`, link; `shutil` copy/move/rmtree; ghi registry;
   - chạy process (`subprocess`, `os.system`, `os.startfile`, `os.kill`...) — trừ khi
     `allow_command(argv)` cho phép, ví dụ `git_readonly_command` (chỉ `git log`, `rev-parse`,
     `cat-file`, `ls-tree`, `diff`, `show`, `rev-list`, `merge-base`...; không có `status` vì
     nó có thể ghi index);
   - mạng — trừ khi `allow_network=True`; khi đó request HTTP khác `GET`/`HEAD` (ví dụ
     `PUT .../pulls/N/merge`) vẫn bị chặn.

   Trong lúc kiểm `sys.dont_write_bytecode` bật để import không ghi `.pyc`.
2. **So ảnh hệ thống file** trước/sau của mọi thư mục trong `watch` (đường dẫn, kích thước,
   mtime, sha256). Lớp này bắt thao tác ghi mà lớp 1 không thấy: process con được cho phép,
   code native.

Báo cáo: `ok`, `readonly_ok`, `violations`, `fs_changes`, `error`, `result`.

### B. Kiểm tra sau release ở bước 11 — `post_release_check` / CLI

Chạy **bên trong** bộ kiểm A với `watch = state_dir`, và giữ `release.lock` trong
`state_dir` suốt lúc kiểm (R10; lock được lấy trước ảnh "trước" và nhả sau ảnh "sau" nên không
tính là thao tác ghi của phần được kiểm). Bốn probe, tất cả phải đạt:

| Probe | Đạt khi |
|---|---|
| `killswitch_engaged` | kill-switch đang **bật** (runbook release bật nó trước khi đổi tag), để không hành động tự động nào ghi `state_dir` cùng lúc |
| `smoke` | import được các module chính; mọi `schemas/*.schema.json` là JSON Schema 2020-12 hợp lệ; `config/harness.yaml` đọc được |
| `health` | `state.json` có, đọc được, đúng schema; `heartbeats.json` có và orchestrator không ở `error`. "Không có dữ liệu" là **không đạt**, khác với "0" |
| `log_errors` | số bản ghi audit có `result` ∈ {`error`, `rejected`} với `seq` > `since_seq` ≤ `readonly_check.max_new_errors` (mặc định tạm 0, R16) |

| `release_tag` | (PR-4b, F2) checkout chứa module đang chạy là **đúng tag release**: `git describe --tags --exact-match --match <tag> HEAD` == `<tag>` và `git diff --quiet HEAD` (không có thay đổi tracked) |

`ok` = mọi probe đạt **và** `readonly_ok` (chính bước kiểm không ghi gì).

Batch release tự động (PR-4b, `docs/runbooks/batch.md`) chạy lệnh này với `--release-lock-held`:
probe `release_lock_held` (batch đang giữ `release.lock` với `purpose: batch_release`, chủ lock còn
sống) thay cho `killswitch_engaged`, vì runner không ghi được kill-switch; batch giữ lock và
`WriterGuard` suốt bước kiểm tra nên không writer nào khác chạy.

## Cách chạy

```powershell
# Trước khi đổi tag (release.md bước 3):
python -m release.readonly_check seq
# Sau khi đổi tag, kill-switch vẫn bật, TỪ runtime checkout tại tag (--expect-tag bắt buộc):
python -m release.readonly_check run --since-seq <seq> --expect-tag <tag>
```

Exit code: `0` = đạt, `1` = không đạt. Lưu báo cáo JSON vào incident nếu không đạt.

## Không đạt thì sao

1. Giữ kill-switch **bật**.
2. Đọc probe không đạt trong báo cáo (`problems`, `new_errors`, `violations`, `fs_changes`).
3. Lỗi do release → `docs/runbooks/revert.md`. Mở incident theo `docs/runbooks/incident.md`
   với `trigger: readonly_check_failed` và đính kèm báo cáo.
4. `fs_changes` khác rỗng mà không phải do bước kiểm (ví dụ agent còn chạy và ghi heartbeat):
   dừng agent rồi chạy lại; nếu vẫn còn thì mở incident.

## Giới hạn (R15)

Best-effort. Audit hook không chặn được code native/ctypes hay process khác cùng OS user;
lớp so ảnh chỉ thấy thư mục được theo dõi. Đây là kiểm tra cho phần mềm hợp tác, không phải
sandbox, cho tới khi agent chạy dưới Windows user riêng.
