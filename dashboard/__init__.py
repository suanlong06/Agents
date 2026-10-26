"""Dashboard Muse Artifact MVP (PR-5), CHỈ ĐỌC.

- ``data_adapter``: đọc ``state_dir`` ở chế độ chỉ đọc, xuất snapshot JSON di động và
  dựng ``artifact.html`` tĩnh từ snapshot đó.
- Muse Artifact chạy trên Muse VM, không đọc được ``state_dir`` của máy này: dashboard
  chỉ thấy snapshot được chuyển sang VM (``dashboard/views/00_snapshot_handoff.md``).
"""
