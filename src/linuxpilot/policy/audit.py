"""Audit log: one redacted JSON line per tool call (never typed text, secrets or clipboard)."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)
REDACTED_KEYS = frozenset({"text", "value", "secret", "option"})


def redact(args: Mapping[str, Any]) -> dict[str, Any]:
    """Arguments safe to store: free text becomes its length."""
    out: dict[str, Any] = {}
    for key, value in args.items():
        if key in REDACTED_KEYS and isinstance(value, str):
            out[key] = f"<{len(value)} chars>"
        elif key in REDACTED_KEYS and value is not None and not isinstance(value, bool | int | float):
            out[key] = "<redacted>"
        else:
            out[key] = value
    return out


@dataclass(slots=True)
class AuditEvent:
    tool: str
    args: dict[str, Any]
    outcome: str
    """``ok`` or an error code."""
    duration_ms: float
    time: float = 0.0


class AuditLog:
    """Appends events to a 0600 JSONL file; failures to write are logged, never raised."""

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self._lock = threading.Lock()

    def record(self, event: AuditEvent) -> None:
        if self.path is None:
            return
        event.time = event.time or time.time()
        line = json.dumps(asdict(event), ensure_ascii=False, default=str)
        try:
            with self._lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                with os.fdopen(fd, "a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
        except OSError as exc:
            log.warning("audit log not written: %s", exc)
