"""Optional Stratz GraphQL source.

OpenDota stays the default and the zero-config path. Stratz is added for the two
things OpenDota structurally cannot give us, both of which this tool documented
as limitations:

  * **The Immortal bracket.** `/heroStats` ships `8_pick`/`8_win` as all zeros, so
    every Immortal player silently gets Divine numbers.
  * **Real positions.** OpenDota exposes `lane_role`, which is a lane, not a
    position - safe lane holds both the carry and the hard support - and only on
    the ~third of matches it parsed. Stratz exposes POSITION_1..POSITION_5.

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

import json
import random
import time
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


class StratzError(RuntimeError):
    """Raised when Stratz answers with something we cannot use."""


def _backoff(attempt: int) -> float:
    return min(2**attempt, BACKOFF_CAP) * (0.5 + random.random() / 2)


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
    ):
        if not token:
            raise StratzError("a Stratz token is required; set STRATZ_API_TOKEN")
        self._token = token
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
        self.calls_made = 0

    def query(self, document: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        """Run one GraphQL query, with the same failure contract as OpenDotaClient.

        The token is never part of the cache key - it is a credential, and it does
        not change the response body.
        """
        variables = variables or {}
        cache_key = f"stratz:{document}:{sorted(variables.items())}"
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached

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
                header = response.headers.get("Retry-After")
                delay = _backoff(attempt)
                if header:
                    try:
                        delay = min(float(header), BACKOFF_CAP)
                    except ValueError:
                        pass
                self._sleep(delay)
                continue
            if response.status_code >= 500:
                last_problem = f"upstream error (HTTP {response.status_code})"
                if final:
                    break
                self._sleep(_backoff(attempt))
                continue
            if not response.ok:
                raise StratzError(f"Stratz -> {response.status_code}: {response.text[:200]}")

            try:
                body = response.json()
            except (ValueError, json.JSONDecodeError) as error:
                raise StratzError("Stratz returned HTTP 200 but not JSON") from error

            if not isinstance(body, dict):
                raise StratzError(f"Stratz returned {type(body).__name__}, expected an object")
            # GraphQL reports query failures inside a 200. Silence here would let a
            # broken query read as an empty-but-successful result.
            if body.get("errors"):
                messages = "; ".join(str(item.get("message", item)) for item in body["errors"][:3])
                raise StratzError(f"Stratz GraphQL error: {messages}")
            data = body.get("data")
            if not isinstance(data, dict):
                raise StratzError("Stratz response had no data object")

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
        data = self.query(document)
        rows = (data.get("heroStats") or {}).get("winWeek") or []
        if not isinstance(rows, list):
            raise StratzError("Stratz winWeek did not return a list")
        return rows

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
        data = self.query(document, {"id": account_id, "take": take})
        player = data.get("player") or {}
        matches = player.get("matches") or []
        rows: list[dict[str, Any]] = []
        for match in matches:
            for entry in match.get("players") or []:
                rows.append(entry)
        return rows

    def verify_access(self) -> dict[str, Any]:
        """Cheapest possible call that proves the token and the schema both work.

        Run this before trusting any of the queries above: GraphQL field names
        drift, and a schema change should surface as one clear message rather
        than as an empty meta table.
        """
        data = self.query("{ constants { heroes { id displayName } } }")
        heroes = (data.get("constants") or {}).get("heroes") or []
        return {"ok": bool(heroes), "hero_count": len(heroes)}
