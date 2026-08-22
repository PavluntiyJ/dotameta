"""Filesystem cache shared by the OpenDota and Stratz clients.

Caching keeps repeated recommendation runs inside API budgets without making
availability depend on successful cache reads or writes.

Two rules shape this module:

  * **Never lose a response to a cache problem.** A read or write failure is
    logged-by-returning, never raised: the caller already paid for that HTTP
    request and must still get it.
  * **Never delete a file we did not write.** `--cache-dir` is user-supplied, so
    `clear()` verifies each file is one of ours - correct envelope, and a name
    matching the digest of the key inside it - before unlinking. Globbing
    `*.json` in a directory someone pointed at their home folder is not
    acceptable behaviour for a cleanup flag.

Cached payloads include personal match history. They live under the cache
directory until they expire and are cleared; `dotameta cache --clear` removes
them, and `--no-cache` avoids writing them at all.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1


def digest_for(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]


class Cache:
    def __init__(self, directory: Path, ttl_seconds: int = 6 * 3600, enabled: bool = True):
        self.directory = directory
        self.ttl_seconds = ttl_seconds
        self.enabled = enabled

    def _path(self, key: str) -> Path:
        return self.directory / f"{digest_for(key)}.json"

    # -- reading -----------------------------------------------------------
    def get(self, key: str, default: Any = None) -> Any:
        if not self.enabled:
            return default
        path = self._path(key)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, ValueError):
            return default  # missing, unreadable, or not JSON: just a miss

        if not self._is_valid_envelope(payload):
            return default
        if payload["digest"] != digest_for(key) or payload["digest"] != path.stem:
            return default

        stored_at = payload["stored_at"]
        now = time.time()
        # A timestamp in the future means a clock jump or a hand-edited file;
        # trusting it would pin a stale entry forever.
        if stored_at > now + 60:
            return default
        if now - stored_at > self.ttl_seconds:
            return default
        return payload["value"]

    @staticmethod
    def _is_valid_envelope(payload: Any) -> bool:
        """Structurally valid JSON is still a miss unless it is *our* envelope."""
        return (
            isinstance(payload, dict)
            and payload.get("schema") == SCHEMA_VERSION
            and isinstance(payload.get("stored_at"), (int, float))
            and not isinstance(payload.get("stored_at"), bool)
            and "value" in payload
            and isinstance(payload.get("digest"), str)
        )

    # -- writing -----------------------------------------------------------
    def set(self, key: str, value: Any) -> None:
        if not self.enabled:
            return
        payload = {
            "schema": SCHEMA_VERSION,
            "stored_at": time.time(),
            "digest": digest_for(key),
            "value": value,
        }
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            # A unique temp file per writer: a fixed "<digest>.tmp" let two
            # processes writing the same key interleave into one corrupt file.
            handle, tmp_name = tempfile.mkstemp(dir=self.directory, prefix=".tmp-", suffix=".json")
            try:
                with os.fdopen(handle, "w", encoding="utf-8") as file:
                    json.dump(payload, file)
                os.replace(tmp_name, self._path(key))
            except BaseException:
                Path(tmp_name).unlink(missing_ok=True)
                raise
        except (OSError, TypeError, ValueError):
            # Caching is an optimisation. Losing it must not lose the response.
            return

    # -- maintenance -------------------------------------------------------
    def _is_own_entry(self, path: Path) -> bool:
        stem = path.stem
        if len(stem) != 32 or not all(c in "0123456789abcdef" for c in stem):
            return False
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, ValueError):
            return False
        # The digest recorded inside must match the filename: a file that merely
        # looks like ours, or was renamed, is left alone.
        return self._is_valid_envelope(payload) and payload["digest"] == stem

    def clear(self) -> int:
        """Remove our own entries. Returns how many were deleted."""
        if not self.directory.exists():
            return 0
        removed = 0
        for path in self.directory.glob("*.json"):
            if not self._is_own_entry(path):
                continue
            try:
                path.unlink()
            except OSError:
                continue
            removed += 1
        return removed

    def entries(self) -> int:
        if not self.directory.exists():
            return 0
        return sum(1 for path in self.directory.glob("*.json") if self._is_own_entry(path))
