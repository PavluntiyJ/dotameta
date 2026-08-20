"""Runtime settings, resolved from CLI flags, environment and an optional .env.

There is no config file format on purpose: the only two things worth persisting
are an API key and a default account id, and both belong in the environment so
they never end up in a commit.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ENV_FILE = ".env"


def load_dotenv(path: str | Path = ENV_FILE) -> None:
    """Minimal KEY=VALUE loader. Existing environment variables always win."""
    file = Path(path)
    if not file.exists():
        return
    for line in file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


@dataclass
class Settings:
    api_key: str | None = None
    account_id: int | None = None
    cache_dir: Path = Path(".cache/opendota")
    cache_ttl: int = 6 * 3600
    use_cache: bool = True

    @classmethod
    def from_env(cls) -> Settings:
        load_dotenv()
        raw_account = os.environ.get("DOTAMETA_ACCOUNT_ID", "").strip()
        return cls(
            api_key=os.environ.get("OPENDOTA_API_KEY") or None,
            account_id=int(raw_account) if raw_account.isdigit() else None,
        )
