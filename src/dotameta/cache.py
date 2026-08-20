"""Filesystem cache for OpenDota responses.

OpenDota's free tier is 60 requests/minute and 2000 calls/day, and a single
recommendation run touches a dozen endpoints. Caching keeps iteration on the
scoring logic from burning the daily budget.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any


class Cache:
    def __init__(self, directory: Path, ttl_seconds: int = 6 * 3600, enabled: bool = True):
        self.directory = directory
        self.ttl_seconds = ttl_seconds
        self.enabled = enabled

    def _path(self, key: str) -> Path:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
        return self.directory / f"{digest}.json"

    def get(self, key: str) -> Any | None:
        if not self.enabled:
            return None
        path = self._path(key)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        if time.time() - payload.get("stored_at", 0) > self.ttl_seconds:
            return None
        return payload.get("value")

    def set(self, key: str, value: Any) -> None:
        if not self.enabled:
            return
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self._path(key)
        payload = {"stored_at": time.time(), "key": key, "value": value}
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.replace(path)

    def clear(self) -> int:
        if not self.directory.exists():
            return 0
        removed = 0
        for path in self.directory.glob("*.json"):
            path.unlink()
            removed += 1
        return removed
