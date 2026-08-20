"""Thin OpenDota REST client: caching, throttling and a stable-ish response shape.

Docs: https://docs.opendota.com/ - the API is free and public; an API key only
raises the rate limit. We deliberately use no other data source: sites such as
dota2protracker have no public API and scraping them would violate their terms.
"""

from __future__ import annotations

import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import requests

from .cache import Cache

BASE_URL = "https://api.opendota.com/api"


class OpenDotaError(RuntimeError):
    """Raised when OpenDota answers with something we cannot use."""


class OpenDotaClient:
    def __init__(
        self,
        api_key: str | None = None,
        cache_dir: Path | None = None,
        cache_ttl: int = 6 * 3600,
        use_cache: bool = True,
        min_interval: float = 1.05,
        timeout: float = 30.0,
        session: requests.Session | None = None,
    ):
        self.api_key = api_key
        # Free tier is 60 calls/minute; stay just under one call per second.
        self.min_interval = 0.0 if api_key else min_interval
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.headers.update(
            {"User-Agent": "dotameta/0.1 (+https://github.com/PavluntiyJ/dotameta)"}
        )
        self.cache = Cache(cache_dir or Path(".cache/opendota"), cache_ttl, use_cache)
        self._last_call = 0.0
        self.calls_made = 0

    # -- transport ---------------------------------------------------------
    def _throttle(self) -> None:
        if self.min_interval <= 0:
            return
        elapsed = time.monotonic() - self._last_call
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self._last_call = time.monotonic()

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        params = {k: v for k, v in (params or {}).items() if v is not None}
        cache_key = f"{path}?{sorted(params.items())}"
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached

        query = dict(params)
        if self.api_key:
            query["api_key"] = self.api_key

        for attempt in range(4):
            self._throttle()
            response = self.session.get(f"{BASE_URL}{path}", params=query, timeout=self.timeout)
            self.calls_made += 1
            if response.status_code == 429:
                # Rate limited: back off and retry rather than dying mid-report.
                time.sleep(2**attempt)
                continue
            if response.status_code >= 500:
                time.sleep(1 + attempt)
                continue
            if not response.ok:
                raise OpenDotaError(f"GET {path} -> {response.status_code}: {response.text[:200]}")
            data = response.json()
            self.cache.set(cache_key, data)
            return data
        raise OpenDotaError(f"GET {path} kept failing (rate limit or upstream error)")

    # -- endpoints ---------------------------------------------------------
    def heroes(self) -> list[dict[str, Any]]:
        """Static hero list: id, localized_name, primary_attr, attack_type, roles."""
        return self.get("/heroes")

    def hero_stats(self) -> list[dict[str, Any]]:
        """Per-hero public/pro pick and win counts, broken down by rank medal."""
        return self.get("/heroStats")

    def player(self, account_id: int) -> dict[str, Any]:
        return self.get(f"/players/{account_id}")

    def player_win_loss(self, account_id: int, **filters: Any) -> dict[str, Any]:
        return self.get(f"/players/{account_id}/wl", filters)

    def player_heroes(self, account_id: int, **filters: Any) -> list[dict[str, Any]]:
        """Per-hero record for the player: games, win, with_games, against_games..."""
        return self.get(f"/players/{account_id}/heroes", filters)

    def player_matches(
        self, account_id: int, limit: int = 100, **filters: Any
    ) -> list[dict[str, Any]]:
        return self.get(f"/players/{account_id}/matches", {"limit": limit, **filters})

    def player_totals(self, account_id: int, **filters: Any) -> list[dict[str, Any]]:
        return self.get(f"/players/{account_id}/totals", filters)

    def benchmarks(self, hero_id: int) -> dict[str, Any]:
        return self.get("/benchmarks", {"hero_id": hero_id})


def index_heroes(heroes: Iterable[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    return {int(hero["id"]): hero for hero in heroes}
