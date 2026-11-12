"""Append-only audit log: one JSON object per line in <log-dir>/audit.jsonl."""

from __future__ import annotations

import json
import os
from collections import deque

FIELDS = (
    "ts", "op", "principal", "pid", "cgroup", "cmdline", "project", "reason", "request", "result",
    "before", "after", "source", "peer",
)  # fmt: skip


class AuditLog:
    def __init__(self, log_dir: str):
        self.path = os.path.join(log_dir, "audit.jsonl")

    def append(self, entry: dict) -> None:
        row = {k: entry.get(k) for k in FIELDS}
        if row["cmdline"]:
            row["cmdline"] = row["cmdline"][:200]
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "a") as f:
            f.write(json.dumps(row, separators=(",", ":")) + "\n")

    def tail(self, n: int) -> list[dict]:
        try:
            with open(self.path) as f:
                lines = deque(f, maxlen=max(n, 0))
        except FileNotFoundError:
            return []
        return [json.loads(line) for line in lines]
