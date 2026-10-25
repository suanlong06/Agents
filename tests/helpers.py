"""Tiện ích chung cho test. Mọi state_dir là thư mục tạm NGOÀI repo (R13)."""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from orchestrator.config import REPO_ROOT, load_config
from orchestrator.fold import Orchestrator

FIXTURES = REPO_ROOT / "tests" / "fixtures"
PRICES_FIXTURE = "tests/fixtures/prices.yaml"
PROC_START = "2026-10-25T09:00:00.000+09:00"


def make_config(state_dir: Path, **overrides):
    ov = {"prices_path": PRICES_FIXTURE}
    ov.update(overrides)
    return load_config(env={"HARNESS_STATE_DIR": str(state_dir)}, overrides=ov)


class Emitter:
    """Giả lập một agent ghi inbox JSONL của chính nó."""

    def __init__(self, path: Path, *, agent="implementer", pid=4242, proc_start=PROC_START,
                 model_id="claude-opus-5-5", model_id_source="cli_metadata"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.agent, self.pid, self.proc_start = agent, pid, proc_start
        self.model_id, self.model_id_source = model_id, model_id_source
        self.seq = 0

    def event(self, type_, task_id, payload=None, **over) -> dict:
        self.seq += 1
        ev = {
            "v": 1, "agent": self.agent, "pid": self.pid, "proc_start": self.proc_start,
            "seq": self.seq, "ts": f"2026-10-25T10:00:{self.seq % 60:02d}.000+09:00",
            "task_id": task_id, "type": type_, "model_id": self.model_id,
            "model_id_source": self.model_id_source, "payload": payload or {},
        }
        ev.update(over)
        return ev

    def write(self, ev: dict, *, newline=True) -> bytes:
        line = json.dumps(ev, separators=(",", ":")).encode("utf-8") + (b"\n" if newline else b"")
        with open(self.path, "ab") as f:
            f.write(line)
        return line

    def emit(self, type_, task_id, payload=None, **over) -> dict:
        ev = self.event(type_, task_id, payload, **over)
        self.write(ev)
        return ev

    def call(self, task_id, call_id, *, attempt=1, inp=1000, out=100, estimated=False, **payload) -> dict:
        p = {"call_id": call_id, "attempt": attempt, "input_tokens": inp,
             "output_tokens": out, "estimated": estimated}
        p.update(payload)
        return self.emit("model_call", task_id, p)

    def walk(self, task_id, *states, **over) -> None:
        for s in states:
            self.emit("transition", task_id, {"to": s}, **over)


class StateDirTestCase(unittest.TestCase):
    """Mỗi test một state_dir tạm riêng."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="harness-test-")).resolve()
        self.state_dir = self.tmp / "state"
        self.config = make_config(self.state_dir)
        self.inbox = self.state_dir / "inbox" / "implementer.jsonl"
        self.agent = Emitter(self.inbox)
        self.user = Emitter(self.state_dir / "inbox" / "user.jsonl", agent="user", pid=1,
                            model_id=None, model_id_source="unknown")
        self._open: list[Orchestrator] = []

    def tearDown(self):
        for o in self._open:
            o.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def open_orch(self, config=None) -> Orchestrator:
        o = Orchestrator(config or self.config).open()
        self._open.append(o)
        return o

    def restart(self, orch: Orchestrator) -> Orchestrator:
        """Giả lập orchestrator khởi động lại: đóng handle, mở lại từ đĩa."""
        orch.close()
        return self.open_orch()

    def sources(self):
        return sorted((self.state_dir / "inbox").glob("*.jsonl"))
