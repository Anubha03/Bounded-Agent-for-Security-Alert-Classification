"""Append-only JSONL trace, one event per line, flushed right away."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

EVENT_KINDS = frozenset(
    {"model_call", "tool_request", "tool_result", "routing", "state_change", "terminal"}
)

TERMINAL_STATUSES = frozenset(
    {
        "completed",         # every alert got settled
        "budget_exhausted",  # not enough tokens left for the next call
        "max_steps",         # hit the step cap
        "no_progress",       # too many turns in a row with nothing new
        "fatal_error",       # crash, provider failure or Bandit failure
        "config_invalid",    # config rejected before anything ran
    }
)

USAGE_KEYS = ("input", "output", "cache_read", "cache_write")


class Trace:
    def __init__(self, path: Path, run_id: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.run_id = run_id
        self._fh = self.path.open("w", encoding="utf-8")
        self._step = 0
        self._terminal: str | None = None
        self.usage: dict[str, int] = {"input": 0, "output": 0}

    @property
    def step(self) -> int:
        return self._step

    @property
    def ended(self) -> bool:
        return self._terminal is not None

    def event(self, kind: str, alert_id: str | None = None, **fields) -> dict:
        if kind not in EVENT_KINDS:
            raise ValueError(f"unknown event kind {kind!r}")
        if self._terminal is not None:
            raise RuntimeError("trace already has a terminal event")
        if kind == "model_call":
            for k, v in fields["usage"].items():
                if k not in USAGE_KEYS:
                    raise ValueError(f"unknown usage counter {k!r}")
                self.usage[k] = self.usage.get(k, 0) + int(v)
        self._step += 1
        record = {
            "step": self._step,
            "ts": time.time(),
            "run_id": self.run_id,
            "alert_id": alert_id,
            "kind": kind,
            **fields,
        }
        self._fh.write(json.dumps(record, default=str) + "\n")
        self._fh.flush()
        os.fsync(self._fh.fileno())
        return record

    def terminal(self, status: str, **fields) -> dict:
        if status not in TERMINAL_STATUSES:
            raise ValueError(f"unknown terminal status {status!r}")
        rec = self.event("terminal", None, status=status, usage_total=dict(self.usage), **fields)
        self._terminal = status
        return rec

    def close(self) -> None:
        if not self._fh.closed:
            self._fh.close()


def read_events(path: Path) -> list[dict]:
    return [json.loads(l) for l in Path(path).read_text(encoding="utf-8").splitlines() if l.strip()]
