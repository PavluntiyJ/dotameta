"""Thin OpenDota REST client: caching, throttling and a stable-ish response shape.

Docs: https://docs.opendota.com/ - the API is free and public; an API key only
raises the rate limit. We deliberately use no other data source: sites such as
dota2protracker have no public API and scraping them would violate their terms.

Everything that can go wrong on the wire leaves this module as `OpenDotaError`.
Nothing above it should ever see a `requests` exception or a `json` error, so the
CLI can print one line instead of a traceback.
"""

from __future__ import annotations

import json
import random
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import requests

from .cache import Cache

BASE_URL = "https://api.opendota.com/api"

MAX_ATTEMPTS = 4
BACKOFF_CAP = 30.0
# Even with a key the free-for-all rate is finite and shared; keep a floor so a
# key cannot turn this into an unthrottled scraper.
KEYED_MIN_INTERVAL = 0.05


class OpenDotaError(RuntimeError):
    """Raised when OpenDota answers with something we cannot use."""


def _retry_after(response: requests.Response, attempt: int) -> float:
    """Honour Retry-After when the server sends one, else capped backoff."""
    header = response.headers.get("Retry-After")
    if header:
        try:
            return min(float(header), BACKOFF_CAP)
        except ValueError:
            pass
    return _backoff(attempt)


def _backoff(attempt: int) -> float:
    """Exponential with jitter, so retries from many clients do not resonate."""
    return min(2**attempt, BACKOFF_CAP) * (0.5 + random.random() / 2)


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
        sleep=time.sleep,
        clock=time.monotonic,
    ):
        self.api_key = api_key
        # Free tier is 60 calls/minute; stay just under one call per second.
        self.min_interval = KEYED_MIN_INTERVAL if api_key else min_interval
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": _user_agent()})
        self.cache = Cache(cache_dir or Path(".cache/opendota"), cache_ttl, use_cache)
        self._sleep = sleep
        self._clock = clock
        self._last_call = 0.0
        self.calls_made = 0

    # -- transport ---------------------------------------------------------
    def _throttle(self) -> None:
        if self.min_interval <= 0:
            return
        elapsed = self._clock() - self._last_call
        if elapsed < self.min_interval:
            self._sleep(self.min_interval - elapsed)
        self._last_call = self._clock()

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        params = {k: v for k, v in (params or {}).items() if v is not None}
        # The key is never part of the cache key: it is a credential, and it does
        # not change the response body.
        cache_key = f"{path}?{sorted(params.items())}"
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached

        query = dict(params)
        if self.api_key:
            query["api_key"] = self.api_key

        last_problem = "no attempts made"
        for attempt in range(MAX_ATTEMPTS):
            last = attempt == MAX_ATTEMPTS - 1
            self._throttle()
            try:
                response = self.session.get(f"{BASE_URL}{path}", params=query, timeout=self.timeout)
            except requests.Timeout as error:
                last_problem = f"timed out after {self.timeout}s"
                if last:
                    raise OpenDotaError(f"GET {path}: {last_problem}") from error
                self._sleep(_backoff(attempt))
                continue
            except requests.ConnectionError as error:
                # DNS, TLS, refused connections, dropped proxies.
                last_problem = "could not reach api.opendota.com"
                if last:
                    raise OpenDotaError(f"GET {path}: {last_problem}") from error
                self._sleep(_backoff(attempt))
                continue
            except requests.RequestException as error:
                raise OpenDotaError(f"GET {path} failed: {error}") from error

            self.calls_made += 1

            if response.status_code == 429:
                last_problem = "rate limited (HTTP 429)"
                if last:
                    break
                self._sleep(_retry_after(response, attempt))
                continue
            if response.status_code >= 500:
                last_problem = f"upstream error (HTTP {response.status_code})"
                if last:
                    break
                self._sleep(_backoff(attempt))
                continue
            if not response.ok:
                raise OpenDotaError(f"GET {path} -> {response.status_code}: {response.text[:200]}")

            try:
                data = response.json()
            except (ValueError, json.JSONDecodeError) as error:
                raise OpenDotaError(f"GET {path} returned HTTP 200 but not JSON") from error

            _check_shape(path, data)
            # A cache failure must never discard a response we already paid for.
            self.cache.set(cache_key, data)
            return data

        raise OpenDotaError(f"GET {path} gave up after {MAX_ATTEMPTS} attempts: {last_problem}")

    # -- endpoints ---------------------------------------------------------
    def heroes(self) -> list[dict[str, Any]]:
        """Static hero list: id, localized_name, primary_attr, attack_type, roles."""
        return self.get("/heroes")

    def hero_stats(self) -> list[dict[str, Any]]:
        """Per-hero public pick and win counts, broken down by rank medal.

        OpenDota documents this as a public aggregate. It is **not** guaranteed to
        be ranked-All-Pick-only, unlike the personal endpoints below, so the two
        sides of the model are not filtered identically. Do not claim otherwise.
        """
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


def _check_shape(path: str, data: Any) -> None:
    """A 200 with the wrong top-level shape is an upstream change.

    Failing here beats a TypeError three modules away, and stops a bad payload
    from being cached for six hours.
    """
    expects_list = path in ("/heroes", "/heroStats") or path.endswith(
        ("/heroes", "/matches", "/totals")
    )
    if expects_list and not isinstance(data, list):
        raise OpenDotaError(f"GET {path} returned {type(data).__name__}, expected a list")
    if not expects_list and not isinstance(data, dict):
        raise OpenDotaError(f"GET {path} returned {type(data).__name__}, expected an object")


def _user_agent() -> str:
    from . import __version__

    return f"dotameta/{__version__} (+https://github.com/PavluntiyJ/dotameta)"


def index_heroes(heroes: Iterable[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    return {int(hero["id"]): hero for hero in heroes}
