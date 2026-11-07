"""
veil-journal — append-only action + policy event log.

Fail-closed: if the journal cannot be written, the broker must deny.
That is a security property, not a logging nicety.
"""
from __future__ import annotations

import json
import time
from pathlib import Path


class JournalError(Exception):
    pass


class Journal:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, event: dict) -> dict:
        event = {"ts": time.time(), **event}
        try:
            with open(self.path, "a") as f:
                f.write(json.dumps(event, sort_keys=True) + "\n")
        except OSError as e:
            raise JournalError(f"journal write failed: {e}") from e
        return event
