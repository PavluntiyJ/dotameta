"""Runtime settings, resolved from CLI flags, environment and an optional .env.

There is no config file format on purpose: the only two things worth persisting
are an API key and a default account id, and both belong in the environment so
they never end up in a commit.

`.env` is read from the current directory, which may not be the user's own file -
think of a cloned repo, or running the tool inside someone else's project. So it
is an **allowlist**, not a loader: only the two keys below are ever applied.
Without that, a hostile `.env` could set `HTTPS_PROXY` or `REQUESTS_CA_BUNDLE`
and quietly redirect or intercept every request this tool makes.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ENV_FILE = ".env"

# The only names a .env file may contribute. Everything else is ignored.
ALLOWED_KEYS = frozenset({"OPENDOTA_API_KEY", "DOTAMETA_ACCOUNT_ID", "STRATZ_API_TOKEN"})


def load_dotenv(path: str | Path | None = None) -> dict[str, str]:
    """Read allowlisted KEY=VALUE pairs. Real environment variables always win.

    Returns what it applied, so callers can tell "absent" from "ignored".
    """
    # Resolved at call time, not at import time, so tests can redirect it.
    file = Path(ENV_FILE if path is None else path)
    applied: dict[str, str] = {}
    try:
        # utf-8-sig so a BOM-prefixed file written by a Windows editor still
        # yields "OPENDOTA_API_KEY" rather than "﻿OPENDOTA_API_KEY".
        text = file.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return applied

    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key not in ALLOWED_KEYS:
            continue
        value = value.strip().strip("'\"")
        if os.environ.setdefault(key, value) == value:
            applied[key] = value
    return applied


@dataclass
class Settings:
    api_key: str | None = None
    account_id: int | None = None
    account_id_error: str | None = None
    stratz_token: str | None = None

    @property
    def has_stratz(self) -> bool:
        """Whether the richer Stratz source can be used at all.

        Stratz needs a token for every request, so its absence is not a
        degraded mode - it is simply unavailable, and OpenDota stays the default.
        """
        return bool(self.stratz_token)

    @classmethod
    def from_env(cls) -> Settings:
        load_dotenv()
        account_id, error = _account_id_from_env()
        return cls(
            api_key=os.environ.get("OPENDOTA_API_KEY") or None,
            account_id=account_id,
            account_id_error=error,
            stratz_token=os.environ.get("STRATZ_API_TOKEN") or None,
        )


def _account_id_from_env() -> tuple[int | None, str | None]:
    """Parse DOTAMETA_ACCOUNT_ID with the same rules as `--account-id`.

    Previously this accepted only bare digits and never converted a Steam64 id,
    so the same value behaved differently depending on where it was written. A
    malformed value is reported rather than silently ignored.
    """
    raw = os.environ.get("DOTAMETA_ACCOUNT_ID", "").strip()
    if not raw:
        return None, None
    from .cli import parse_account_id  # local import: cli imports this module

    try:
        return parse_account_id(raw), None
    except Exception as error:  # argparse.ArgumentTypeError
        return None, f"DOTAMETA_ACCOUNT_ID is not usable: {error}"
