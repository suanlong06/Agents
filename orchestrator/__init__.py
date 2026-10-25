"""Orchestrator của Harness Agents (PR-1a): bên DUY NHẤT ghi runtime state (R3).

Module:
- ``schemas``: nạp và kiểm JSON Schema trong ``schemas/``, cửa sổ version N-1/N+1.
- ``state``: state machine bước 1–13 + trạng thái mở rộng, lineage, ``StateStore``.
- ``fold``: fold event idempotent từ inbox JSONL vào snapshot nguyên tử.
- ``ledger``: cost ledger theo lời gọi model, giá từ ``config/prices.yaml``.
- ``audit``: audit log append-only có số thứ tự, best-effort, không hash chain.
- ``killswitch`` (PR-4a): kill-switch fail-closed, chỉ đọc; thiếu/hỏng = bật; best-effort.
"""
