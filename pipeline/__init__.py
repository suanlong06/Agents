"""Pipeline PR của Harness Agents (PR-3).

Module:
- ``labeler``: nhãn Red / Owner-only / Green theo file bị chạm, cấu hình đọc TỪ BASE
  REF (R8); nhãn gắn với cặp SHA và tái lập được.
- ``ci_status``: đọc check run GitHub Actions theo SHA; không chạy CI cục bộ, không
  nhận output CI dán tay (R1, R14).
- ``gate3``: Gate 3 theo writer set THỰC SỰ phục vụ (ledger), đúng 1 reviewer, quy tắc
  vendor R11; diff là dữ liệu trích dẫn; verdict theo ``schemas/verdict.schema.json``.
- ``fix_loop``: N vòng fix + ngân sách cộng dồn theo ``task_id``; fix list vào
  ``awaiting-human`` vì Fixer là STUB; vượt giới hạn thì ``escalated`` (R2).
- ``approvals``: phản chiếu GitHub review của người dùng vào ``approvals.jsonl``;
  GitHub là nguồn sự thật (R7).
- ``common``, ``github_api``: cấu hình, schema, lọc ledger probe, client GitHub REST.

Pipeline không merge, không push, không gọi Fixer/Codex (R7, R11).
"""
