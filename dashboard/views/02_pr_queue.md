# View 2 — PR queue

Snapshot: `views.pr_queue`. Dữ liệu đến Muse VM qua snapshot ([handoff](00_snapshot_handoff.md)).

## Nguồn

- `state.json`: mỗi task có PR cho một dòng attempt hiện tại; mỗi attempt cũ có PR (trong
  `attempts`, `final_state = superseded`) cho thêm một dòng.
- Event đã fold trong inbox (`inbox/<agent>/events-*.jsonl` và các nguồn có trong
  `state.json.offsets`). Chỉ dùng event có offset nằm trước offset đã fold, để view khớp với
  `as_of`.

## Nhãn Red / Owner-only / Green

Labeller (PR-3) không ghi nhãn ra file riêng trong `state_dir`. Nhãn duy nhất có trong state là
`label` bên trong verdict Gate 3 mà `gate3.py` ghi vào event `transition` (payload `verdict`,
kiểm theo `schemas/verdict.schema.json`).

- Verdict cho **đúng head SHA** của dòng → hiện nhãn.
- Chỉ có verdict cho SHA khác của cùng task → hiện nhãn đó kèm "cũ: cho SHA …" (nhãn chỉ có
  hiệu lực cho đúng SHA, R9).
- Chưa có verdict → "không có dữ liệu". PR chưa qua Gate 3 sẽ ở trạng thái này cho tới khi có
  runner ghi nhãn vào state.

## Trạng thái CI

Nguồn sự thật của CI là check run GitHub Actions (PR-3, R14); `state_dir` không lưu nó.

- CI đỏ mà vòng fix đã ghi lại (event `fix_list` với `source: "ci"` cho đúng SHA) → `failure`.
- Ngoài ra là "không có dữ liệu" kèm link sang `…/pull/<n>/checks` trên GitHub. Dashboard
  **không** tự suy ra CI xanh từ trạng thái task.

## Provenance

Nguồn `state.json` + file event; số bản ghi = số dòng PR; `as_of` = lần fold cuối. Không có
`state.json` → "không có dữ liệu". Có state mà chưa task nào có PR → `0`.

## Chỉ đọc

Link PR mở GitHub. Không có nút merge, nút chạy lại CI hay đổi nhãn.
