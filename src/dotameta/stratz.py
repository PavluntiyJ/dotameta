"""Optional Stratz GraphQL source.

OpenDota stays the default and the zero-config path. Stratz is added where
OpenDota structurally cannot give us usable data:

  * **The Immortal bracket.** `/heroStats` ships `8_pick`/`8_win` as all zeros, so
    every Immortal player silently gets Divine numbers.
  * **Real positions.** OpenDota exposes `lane_role`, which is a lane, not a
    position - safe lane holds both the carry and the hard support - and only on
    the ~third of matches it parsed. Stratz exposes POSITION_1..POSITION_5.
  * **Personal hero fallback.** Stratz can sometimes supply a deliberately sparse
    ranked-All-Pick aggregate when OpenDota has no personal hero rows.

Access needs a token (free, Steam sign-in, no payment method). Without one this
module is simply unavailable; nothing degrades.

Two operational gotchas, both load-bearing:

  * Stratz sits behind Cloudflare and rejects unrecognised clients. The
    `User-Agent: STRATZ_API` header is what gets a programmatic request through -
    a normal browser-ish UA is challenged.
  * GraphQL answers HTTP 200 with an `errors` array. A transport-only check would
    read a failed query as success, so `errors` is inspected explicitly.

The queries below are written against the published schema shape. Field names in
a GraphQL schema drift, so `verify_access()` exists to check them against a live
token rather than assuming they still hold.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import time
from collections.abc import Callable
from datetime import UTC
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

import requests

from .cache import Cache

ENDPOINT = "https://api.stratz.com/graphql"

# Cloudflare lets this through where a browser-like UA is challenged.
USER_AGENT = "STRATZ_API"

MAX_ATTEMPTS = 4
BACKOFF_CAP = 30.0

# Stratz medal names, indexed to match dotameta's 1-8 medal numbering.
BRACKETS = {
    1: "HERALD",
    2: "GUARDIAN",
    3: "CRUSADER",
    4: "ARCHON",
    5: "LEGEND",
    6: "ANCIENT",
    7: "DIVINE",
    8: "IMMORTAL",
}

POSITIONS = {
    1: "POSITION_1",
    2: "POSITION_2",
    3: "POSITION_3",
    4: "POSITION_4",
    5: "POSITION_5",
}

RANKED_ALL_PICK = "ALL_PICK_RANKED"

# heroesPerformance can inspect at most this many matches in one request. Keep
# this beside the query so coverage checks in player.py use the same limit.
PLAYER_MATCH_DEPTH = 10_000


class StratzError(RuntimeError):
    """Raised when Stratz answers with something we cannot use."""


def _backoff(attempt: int) -> float:
    return min(2**attempt, BACKOFF_CAP) * (0.5 + random.random() / 2)


def _retry_after(response: requests.Response, attempt: int, now: float | None = None) -> float:
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


def _object(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise StratzError(f"Stratz {path} was not an object")
    return value


def _list(value: Any, path: str) -> list[Any]:
    if not isinstance(value, list):
        raise StratzError(f"Stratz {path} was not a list")
    return value


def _count(value: Any, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise StratzError(f"Stratz {path} was not a non-negative integer")
    return value


def _hero_win_rate_rows(data: dict[str, Any]) -> list[dict[str, int]]:
    stats = _object(data.get("heroStats"), "heroStats")
    rows = _list(stats.get("winWeek"), "heroStats.winWeek")
    normalised: list[dict[str, int]] = []
    seen: set[int] = set()
    for index, value in enumerate(rows):
        path = f"heroStats.winWeek[{index}]"
        row = _object(value, path)
        hero_id = _count(row.get("heroId"), f"{path}.heroId")
        matches = _count(row.get("matchCount"), f"{path}.matchCount")
        wins = _count(row.get("winCount"), f"{path}.winCount")
        if hero_id == 0 or hero_id in seen or wins > matches:
            raise StratzError(f"Stratz {path} had invalid counts")
        seen.add(hero_id)
        normalised.append({"heroId": hero_id, "matchCount": matches, "winCount": wins})
    return normalised


def _player_hero_performance(data: dict[str, Any]) -> dict[str, Any] | None:
    if "player" not in data:
        raise StratzError("Stratz response had no player field")
    value = data.get("player")
    if value is None:
        return None
    player = _object(value, "player")
    match_count = _count(player.get("matchCount"), "player.matchCount")
    steam_account = _object(player.get("steamAccount"), "player.steamAccount")
    anonymous = steam_account.get("isAnonymous")
    if not isinstance(anonymous, bool):
        raise StratzError("Stratz player.steamAccount.isAnonymous was not a boolean")

    def performance_rows(field: str) -> list[dict[str, int]]:
        rows = _list(player.get(field), f"player.{field}")
        heroes: list[dict[str, int]] = []
        seen: set[int] = set()
        for index, value in enumerate(rows):
            path = f"player.{field}[{index}]"
            row = _object(value, path)
            hero_id = _count(row.get("heroId"), f"{path}.heroId")
            matches = _count(row.get("matchCount"), f"{path}.matchCount")
            wins = _count(row.get("winCount"), f"{path}.winCount")
            if hero_id == 0 or hero_id in seen or wins > matches:
                raise StratzError(f"Stratz {path} had invalid counts")
            seen.add(hero_id)
            heroes.append({"heroId": hero_id, "matchCount": matches, "winCount": wins})
        return heroes

    return {
        "matchCount": match_count,
        "isAnonymous": anonymous,
        "allHistoryHeroesPerformance": performance_rows("allHistoryHeroesPerformance"),
        "rankedAllPickHeroesPerformance": performance_rows("rankedAllPickHeroesPerformance"),
    }


def _player_position_rows(data: dict[str, Any]) -> list[dict[str, Any]]:
    if "player" not in data:
        raise StratzError("Stratz response had no player field")
    value = data.get("player")
    if value is None:
        return []
    player = _object(value, "player")
    matches = _list(player.get("matches"), "player.matches")
    rows: list[dict[str, Any]] = []
    for match_index, value in enumerate(matches):
        match_path = f"player.matches[{match_index}]"
        match = _object(value, match_path)
        players = _list(match.get("players"), f"{match_path}.players")
        for player_index, entry in enumerate(players):
            path = f"{match_path}.players[{player_index}]"
            row = _object(entry, path)
            hero_id = _count(row.get("heroId"), f"{path}.heroId")
            if hero_id == 0 or row.get("position") not in POSITIONS.values():
                raise StratzError(f"Stratz {path} had invalid hero or position fields")
            if not isinstance(row.get("isVictory"), bool):
                raise StratzError(f"Stratz {path}.isVictory was not a boolean")
            rows.append(row)
    return rows


class StratzClient:
    def __init__(
        self,
        token: str,
        cache_dir: Path | None = None,
        cache_ttl: int = 6 * 3600,
        use_cache: bool = True,
        timeout: float = 30.0,
        session: requests.Session | None = None,
        sleep=time.sleep,
        wall_clock=time.time,
    ):
        if not token:
            raise StratzError("a Stratz token is required; set STRATZ_API_TOKEN")
        self._token = token
        self._cache_namespace = hashlib.sha256(token.encode("utf-8")).hexdigest()
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            }
        )
        self.cache = Cache(cache_dir or Path(".cache/stratz"), cache_ttl, use_cache)
        self._sleep = sleep
        self._wall_clock = wall_clock
        self.calls_made = 0

    def query(
        self,
        document: str,
        variables: dict[str, Any] | None = None,
        validator: Callable[[dict[str, Any]], Any] | None = None,
    ) -> dict[str, Any]:
        """Run one GraphQL query, with the same failure contract as OpenDotaClient.

        The raw token is never cache-key material. A full one-way fingerprint
        isolates permission-sensitive responses belonging to different tokens.
        """
        variables = variables or {}
        encoded_variables = json.dumps(variables, sort_keys=True, separators=(",", ":"))
        cache_key = f"stratz:{self._cache_namespace}:{document}:{encoded_variables}"
        cache_miss = object()
        cached = self.cache.get(cache_key, cache_miss)
        if cached is not cache_miss:
            data = _object(cached, "cached data")
            if validator is not None:
                validator(data)
            return data

        payload = {"query": document, "variables": variables}
        last_problem = "no attempts made"

        for attempt in range(MAX_ATTEMPTS):
            final = attempt == MAX_ATTEMPTS - 1
            try:
                response = self.session.post(ENDPOINT, json=payload, timeout=self.timeout)
            except requests.Timeout as error:
                last_problem = f"timed out after {self.timeout}s"
                if final:
                    raise StratzError(f"Stratz query: {last_problem}") from error
                self._sleep(_backoff(attempt))
                continue
            except requests.ConnectionError as error:
                last_problem = "could not reach api.stratz.com"
                if final:
                    raise StratzError(f"Stratz query: {last_problem}") from error
                self._sleep(_backoff(attempt))
                continue
            except requests.RequestException as error:
                raise StratzError(f"Stratz query failed: {error}") from error

            self.calls_made += 1

            if response.status_code in (401, 403):
                # Do not retry: a bad token will stay bad, and Cloudflare
                # challenges are not solved by asking again.
                raise StratzError(
                    "Stratz rejected the request (HTTP "
                    f"{response.status_code}). Check STRATZ_API_TOKEN is valid."
                )
            if response.status_code == 429:
                last_problem = "rate limited (HTTP 429)"
                if final:
                    break
                self._sleep(_retry_after(response, attempt, self._wall_clock()))
                continue
            if response.status_code >= 500:
                last_problem = f"upstream error (HTTP {response.status_code})"
                if final:
                    break
                self._sleep(_retry_after(response, attempt, self._wall_clock()))
                continue
            if not response.ok:
                raise StratzError(f"Stratz request failed (HTTP {response.status_code})")

            try:
                body = response.json()
            except (ValueError, json.JSONDecodeError) as error:
                raise StratzError("Stratz returned HTTP 200 but not JSON") from error

            if not isinstance(body, dict):
                raise StratzError(f"Stratz returned {type(body).__name__}, expected an object")
            # GraphQL reports query failures inside a 200. Silence here would let a
            # broken query read as an empty-but-successful result.
            errors = body.get("errors")
            if errors:
                if not isinstance(errors, list):
                    raise StratzError("Stratz GraphQL errors was not a list")
                messages = "; ".join(
                    str(item.get("message", item)) if isinstance(item, dict) else str(item)
                    for item in errors[:3]
                )
                raise StratzError(f"Stratz GraphQL error: {messages}")
            data = body.get("data")
            if not isinstance(data, dict):
                raise StratzError("Stratz response had no data object")

            if validator is not None:
                validator(data)
            self.cache.set(cache_key, data)
            return data

        raise StratzError(f"Stratz gave up after {MAX_ATTEMPTS} attempts: {last_problem}")

    # -- queries -----------------------------------------------------------
    def hero_win_rates(self, medal: int, position: int | None = None) -> list[dict[str, Any]]:
        """Pick/win counts per hero for one medal, optionally for one position.

        This is the query that makes Immortal reachable at all, and the only way
        to get per-position numbers rather than per-lane ones.
        """
        bracket = BRACKETS.get(medal)
        if bracket is None:
            raise StratzError(f"no Stratz bracket for medal {medal}")
        position_filter = ""
        if position is not None:
            name = POSITIONS.get(position)
            if name is None:
                raise StratzError(f"no Stratz position for {position}")
            position_filter = f", positionIds: [{name}]"

        document = f"""
        {{
          heroStats {{
            winWeek(
              bracketIds: [{bracket}]
              gameModeIds: [{RANKED_ALL_PICK}]{position_filter}
              take: 1
            ) {{
              heroId
              matchCount
              winCount
            }}
          }}
        }}
        """
        data = self.query(document, validator=_hero_win_rate_rows)
        return _hero_win_rate_rows(data)

    def player_hero_performance(self, account_id: int) -> dict[str, Any] | None:
        """Coverage and ranked-All-Pick hero aggregates in one player query.

        The unfiltered alias is used only to prove access and coverage against
        matchCount. Recommendations use only the separately filtered alias. The
        payload has no rank, display name, dates, or lanes.
        """
        document = """
        query PlayerHeroPerformance($id: Long!) {
          player(steamAccountId: $id) {
            matchCount
            steamAccount {
              isAnonymous
            }
            allHistoryHeroesPerformance: heroesPerformance(take: 200, request: { take: 10000 }) {
              heroId
              matchCount
              winCount
            }
            rankedAllPickHeroesPerformance: heroesPerformance(
              take: 200,
              request: { take: 10000, gameModeIds: [ALL_PICK_RANKED] }
            ) {
              heroId
              matchCount
              winCount
            }
          }
        }
        """
        data = self.query(document, {"id": account_id}, validator=_player_hero_performance)
        return _player_hero_performance(data)

    def player_positions(self, account_id: int, take: int = 200) -> list[dict[str, Any]]:
        """Per-match hero and position for one player.

        Positions here are Stratz's own classification, not OpenDota's lane_role,
        and are present on every match rather than only on parsed ones.
        """
        document = """
        query PlayerPositions($id: Long!, $take: Int!) {
          player(steamAccountId: $id) {
            matches(request: { take: $take, gameModeIds: [ALL_PICK_RANKED] }) {
              id
              players(steamAccountId: $id) {
                heroId
                position
                isVictory
              }
            }
          }
        }
        """
        data = self.query(
            document,
            {"id": account_id, "take": take},
            validator=_player_position_rows,
        )
        return _player_position_rows(data)

    def verify_access(self, account_id: int) -> dict[str, Any]:
        """Read-only checks of the production meta and personal query paths.

        Calling the real methods keeps this check from drifting into a token-only
        probe while either production document has stopped matching the schema.
        The caller must deliberately provide an account it is authorized to query;
        there is no built-in live smoke account.
        """
        meta_rows = self.hero_win_rates(medal=8)
        player = self.player_hero_performance(account_id)
        position_rows = self.player_positions(account_id, take=1)
        return {
            "ok": bool(meta_rows) and player is not None,
            "meta_hero_count": len(meta_rows),
            "player_found": player is not None,
            "position_rows": len(position_rows),
        }
