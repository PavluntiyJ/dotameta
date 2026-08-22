"""Thin OpenDota REST client: caching, throttling and a stable-ish response shape.

Docs: https://docs.opendota.com/ - the API is free and public; an API key only
raises the rate limit. OpenDota is the default source; optional Stratz support is
kept behind its own client and used only at the source-selection boundary.

Everything that can go wrong on the wire leaves this module as `OpenDotaError`.
Nothing above it should ever see a `requests` exception or a `json` error, so the
CLI can print one line instead of a traceback.
"""

from __future__ import annotations

import json
import math
import random
import time
from datetime import UTC
from email.utils import parsedate_to_datetime
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


def _retry_after(response: requests.Response, attempt: int, now: float | None = None) -> float:
    """Parse RFC Retry-After delta-seconds or HTTP-date, else use backoff."""
    header = response.headers.get("Retry-After")
    if isinstance(header, str):
        value = header.strip()
        if value.isascii() and value.isdecimal():
            digits = value.lstrip("0") or "0"
            if len(digits) > len(str(int(BACKOFF_CAP))):
                return BACKOFF_CAP
            return min(int(digits), BACKOFF_CAP)
        try:
            retry_at = parsedate_to_datetime(value)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=UTC)
            delay = retry_at.timestamp() - (time.time() if now is None else now)
            if math.isfinite(delay) and delay >= 0:
                return min(delay, BACKOFF_CAP)
        except (TypeError, ValueError, OverflowError):
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
        wall_clock=time.time,
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
        self._wall_clock = wall_clock
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
            validate_payload(path, cached)
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
                raise OpenDotaError(f"GET {path} failed due to a request error") from error

            self.calls_made += 1

            if response.status_code == 429:
                last_problem = "rate limited (HTTP 429)"
                if last:
                    break
                self._sleep(_retry_after(response, attempt, self._wall_clock()))
                continue
            if response.status_code >= 500:
                last_problem = f"upstream error (HTTP {response.status_code})"
                if last:
                    break
                self._sleep(_retry_after(response, attempt, self._wall_clock()))
                continue
            if not response.ok:
                raise OpenDotaError(f"GET {path} failed (HTTP {response.status_code})")

            try:
                data = response.json()
            except (ValueError, json.JSONDecodeError) as error:
                raise OpenDotaError(f"GET {path} returned HTTP 200 but not JSON") from error

            validate_payload(path, data)
            # A cache failure must never discard a response we already paid for.
            self.cache.set(cache_key, data)
            return data

        raise OpenDotaError(f"GET {path} gave up after {MAX_ATTEMPTS} attempts: {last_problem}")

    # -- endpoints ---------------------------------------------------------
    def heroes(self) -> list[dict[str, Any]]:
        """Static hero list used for hero names and role tags."""
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


def _malformed(endpoint: str, category: str) -> OpenDotaError:
    return OpenDotaError(f"GET {endpoint} returned malformed {category}")


def _is_int(value: Any, *, positive: bool = False) -> bool:
    return type(value) is int and value >= (1 if positive else 0)


def _rows(data: Any, endpoint: str, category: str) -> list[dict[str, Any]]:
    if not isinstance(data, list):
        raise OpenDotaError(f"GET {endpoint} returned {type(data).__name__}, expected a list")
    if any(not isinstance(row, dict) for row in data):
        raise _malformed(endpoint, f"{category} row objects")
    return data


def _object(data: Any, endpoint: str) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise OpenDotaError(f"GET {endpoint} returned {type(data).__name__}, expected an object")
    return data


def _count_pair(
    row: dict[str, Any],
    games_key: str,
    wins_key: str,
    endpoint: str,
    category: str,
    *,
    required: bool = False,
) -> None:
    games_present = games_key in row
    wins_present = wins_key in row
    if required and not (games_present and wins_present):
        raise _malformed(endpoint, category)
    if not games_present and not wins_present:
        return
    if not (games_present and wins_present):
        raise _malformed(endpoint, category)
    games, wins = row[games_key], row[wins_key]
    if not _is_int(games) or not _is_int(wins) or wins > games:
        raise _malformed(endpoint, category)


def _validate_hero_details(
    row: dict[str, Any], endpoint: str, *, require_name: bool = False
) -> None:
    if not _is_int(row.get("id"), positive=True):
        raise _malformed(endpoint, "hero identifier fields")
    name = row.get("localized_name")
    if require_name and (not isinstance(name, str) or not name.strip()):
        raise _malformed(endpoint, "hero text fields")
    if name is not None and not isinstance(name, str):
        raise _malformed(endpoint, "hero text fields")
    roles = row.get("roles")
    if roles is not None and (
        not isinstance(roles, list) or any(not isinstance(role, str) for role in roles)
    ):
        raise _malformed(endpoint, "hero role fields")


def validate_heroes(data: Any) -> list[dict[str, Any]]:
    rows = _rows(data, "/heroes", "hero")
    for row in rows:
        _validate_hero_details(row, "/heroes", require_name=True)
    return rows


def validate_hero_stats(data: Any) -> list[dict[str, Any]]:
    endpoint = "/heroStats"
    rows = _rows(data, endpoint, "hero stat")
    for row in rows:
        _validate_hero_details(row, endpoint, require_name=True)
        _count_pair(row, "pub_pick", "pub_win", endpoint, "public count fields", required=True)
        for medal in range(1, 9):
            _count_pair(
                row,
                f"{medal}_pick",
                f"{medal}_win",
                endpoint,
                "bracket count fields",
            )

        picks = row.get("pub_pick_trend")
        wins = row.get("pub_win_trend")
        if picks is None and wins is None:
            continue
        if not isinstance(picks, list) or not isinstance(wins, list) or len(picks) != len(wins):
            raise _malformed(endpoint, "trend fields")
        if any(
            not _is_int(pick) or not _is_int(win) or win > pick
            for pick, win in zip(picks, wins, strict=True)
        ):
            raise _malformed(endpoint, "trend fields")
    return rows


def validate_player(data: Any) -> dict[str, Any]:
    endpoint = "/players/{account_id}"
    row = _object(data, endpoint)
    rank_tier = row.get("rank_tier")
    if rank_tier is not None and not _is_int(rank_tier):
        raise _malformed(endpoint, "rank fields")
    profile = row.get("profile")
    if profile is not None and not isinstance(profile, dict):
        raise _malformed(endpoint, "profile fields")
    if isinstance(profile, dict):
        name = profile.get("personaname")
        if name is not None and not isinstance(name, str):
            raise _malformed(endpoint, "profile fields")
    return row


def validate_player_win_loss(data: Any) -> dict[str, Any]:
    endpoint = "/players/{account_id}/wl"
    row = _object(data, endpoint)
    if not _is_int(row.get("win")) or not _is_int(row.get("lose")):
        raise _malformed(endpoint, "win/loss count fields")
    return row


def validate_player_heroes(data: Any) -> list[dict[str, Any]]:
    endpoint = "/players/{account_id}/heroes"
    rows = _rows(data, endpoint, "player hero")
    for row in rows:
        if not _is_int(row.get("hero_id"), positive=True):
            raise _malformed(endpoint, "hero identifier fields")
        _count_pair(row, "games", "win", endpoint, "hero count fields", required=True)
        last_played = row.get("last_played")
        if last_played is not None and not _is_int(last_played):
            raise _malformed(endpoint, "hero timestamp fields")
    return rows


def validate_match_rows(data: Any) -> list[dict[str, Any]]:
    endpoint = "/players/{account_id}/matches"
    rows = _rows(data, endpoint, "match")
    for row in rows:
        if not _is_int(row.get("hero_id"), positive=True):
            raise _malformed(endpoint, "match hero identifier fields")
        for key in ("lane_role", "player_slot", "lobby_type", "game_mode", "start_time"):
            if row.get(key) is not None and not _is_int(row[key]):
                category = "match lane fields" if key == "lane_role" else "match numeric fields"
                raise _malformed(endpoint, category)
        for key in ("is_roaming", "radiant_win"):
            if row.get(key) is not None and type(row[key]) is not bool:
                category = "match lane fields" if key == "is_roaming" else "match outcome fields"
                raise _malformed(endpoint, category)
    return rows


def validate_payload(path: str, data: Any) -> None:
    """Validate consumed endpoint fields before caching and after cache reads."""
    if path == "/heroes":
        validate_heroes(data)
        return
    if path == "/heroStats":
        validate_hero_stats(data)
        return

    parts = path.strip("/").split("/")
    if len(parts) == 2 and parts[0] == "players":
        validate_player(data)
    elif len(parts) == 3 and parts[0] == "players" and parts[2] == "wl":
        validate_player_win_loss(data)
    elif len(parts) == 3 and parts[0] == "players" and parts[2] == "heroes":
        validate_player_heroes(data)
    elif len(parts) == 3 and parts[0] == "players" and parts[2] == "matches":
        validate_match_rows(data)
    else:
        _object(data, path)


def _user_agent() -> str:
    from . import __version__

    return f"dotameta/{__version__} (+https://github.com/PavluntiyJ/dotameta)"
