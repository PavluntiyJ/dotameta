"""Normalize OpenDota history or a Stratz aggregate into `PlayerProfile`.

Every OpenDota personal endpoint is filtered to **ranked All Pick** (`lobby_type=7`,
`game_mode=22`). Without that filter a player's winrate, hero categories, lane
split and pace all silently absorb unranked and Turbo games, and the tool then
projects ranked MMR off the wrong population.

Note the asymmetry this creates, and do not paper over it: the bracket side of
the model comes from `/heroStats`, which OpenDota documents only as a public
per-medal aggregate. It is *not* guaranteed to be ranked-All-Pick-only, so the
two sides of the comparison are filtered differently. The optional Stratz path
uses a separately filtered ranked-All-Pick aggregate only after its unfiltered
all-history aggregate passes the coverage checks below. See README.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from .constants import format_rank_tier, medal_from_rank_tier
from .lanes import HeroLanes, lane_stats
from .opendota import (
    OpenDotaClient,
    OpenDotaError,
    validate_match_rows,
    validate_player,
    validate_player_heroes,
    validate_player_win_loss,
)
from .stats import winrate
from .stratz import PLAYER_MATCH_DEPTH, StratzClient, StratzError

SECONDS_PER_WEEK = 7 * 24 * 3600
SECONDS_PER_DAY = 24 * 3600

# Ranked All Pick. Applied to every personal endpoint so the player side of the
# model is the same population the MMR projection talks about.
RANKED_LOBBY_TYPE = 7
ALL_PICK_GAME_MODE = 22

# Fields the rest of the package reads off a match row. `project` replaces part
# of OpenDota's default field set, so anything used downstream must be listed
# here explicitly - `start_time` vanished this way once already.
MATCH_FIELDS = [
    "start_time",
    "hero_id",
    "lane_role",
    "is_roaming",
    "player_slot",
    "radiant_win",
    "lobby_type",
    "game_mode",
]

# Trailing window used to measure current pace, and the smallest sample we will
# quote a pace from.
PACE_WINDOW_DAYS = 30
MIN_PACE_MATCHES = 3

# Stratz's aggregate can cover at most PLAYER_MATCH_DEPTH matches. Requiring at
# least 95% of that expected sample prevents a private/partial response from
# masquerading as a small hero pool. Equality is accepted.
MIN_STRATZ_COVERAGE_PERCENT = 95


class DataStatus(StrEnum):
    """Why a player might have no usable hero data.

    "no rows" and "rows, but all zero games in this window" mean opposite things
    to the user - one is a privacy setting to change, the other is just an idle
    account - and telling them apart is the difference between useful advice and
    a wrong instruction.
    """

    AVAILABLE = "available"
    EMPTY_WINDOW = "empty_window"
    PRIVATE_OR_UNAVAILABLE = "private_or_unavailable"


@dataclass(frozen=True)
class PlayerHero:
    hero_id: int
    games: int
    wins: int
    last_played: int = 0

    @property
    def winrate(self) -> float:
        return winrate(self.wins, self.games)


@dataclass
class PlayerProfile:
    account_id: int | None
    name: str
    rank_tier: int | None
    games: int
    wins: int
    heroes: dict[int, PlayerHero] = field(default_factory=dict)
    lanes: dict[int, HeroLanes] = field(default_factory=dict)
    games_per_week: float | None = None
    pace_note: str = ""
    recent_games: int = 0
    recent_wins: int = 0
    data_status: DataStatus = DataStatus.AVAILABLE

    @property
    def medal(self) -> int | None:
        return medal_from_rank_tier(self.rank_tier)

    @property
    def rank_label(self) -> str:
        return format_rank_tier(self.rank_tier)

    @property
    def winrate(self) -> float:
        return winrate(self.wins, self.games)

    @property
    def recent_winrate(self) -> float:
        return winrate(self.recent_wins, self.recent_games)

    @property
    def has_match_data(self) -> bool:
        return bool(self.heroes)

    def hero(self, hero_id: int) -> PlayerHero:
        return self.heroes.get(hero_id) or PlayerHero(hero_id=hero_id, games=0, wins=0)

    def hero_lanes(self, hero_id: int) -> HeroLanes:
        return self.lanes.get(hero_id) or HeroLanes(hero_id=hero_id)

    @property
    def hero_pool_size(self) -> int:
        """Heroes with enough games to count as "known" rather than tried once."""
        return sum(1 for hero in self.heroes.values() if hero.games >= 10)


def match_outcome(match: dict[str, Any]) -> bool | None:
    """True won, False lost, **None when the row cannot say**.

    Slots 0-127 are Radiant, 128+ are Dire; OpenDota never stores "did I win".
    Returning None rather than False matters: a row missing `player_slot` or
    `radiant_win` used to count as a loss, inflating the denominator with games
    whose result we simply do not know.
    """
    player_slot = match.get("player_slot")
    radiant_win = match.get("radiant_win")
    if player_slot is None or radiant_win is None:
        return None
    if type(player_slot) is not int or player_slot < 0 or type(radiant_win) is not bool:
        raise OpenDotaError(
            "GET /players/{account_id}/matches returned malformed match outcome fields"
        )
    return radiant_win == (player_slot < 128)


def is_ranked_all_pick(match: dict[str, Any]) -> bool:
    """Second line of defence: rows are filtered server-side, verified here.

    Match rows that predate the filter, or come from a cache written before it,
    would otherwise leak into the lane split.
    """
    lobby = match.get("lobby_type")
    mode = match.get("game_mode")
    if lobby is not None and (type(lobby) is not int or lobby < 0):
        raise OpenDotaError(
            "GET /players/{account_id}/matches returned malformed match filter fields"
        )
    if mode is not None and (type(mode) is not int or mode < 0):
        raise OpenDotaError(
            "GET /players/{account_id}/matches returned malformed match filter fields"
        )
    if lobby is not None and lobby != RANKED_LOBBY_TYPE:
        return False
    return not (mode is not None and mode != ALL_PICK_GAME_MODE)


def games_per_week(
    matches: list[dict[str, Any]],
    now: float | None = None,
    window_days: int = PACE_WINDOW_DAYS,
    sample_limit: int | None = None,
) -> tuple[float | None, str]:
    """Current pace over a trailing window ending *now*, plus why it may be absent.

    The old formula divided the match count by the span between the first and
    last match, which measures how densely someone played during a burst rather
    than how much they play. Two games an hour apart came out as 336 games/week,
    and a heavy month two years ago read as the current pace.

    Returns (pace, note). `None` means we refuse to quote one - the CLI then
    suppresses the MMR/week line instead of printing an invented number.
    """
    now = time.time() if now is None else now
    times = []
    for match in matches:
        start_time = match.get("start_time")
        if start_time is None or start_time == 0:
            continue
        if type(start_time) is not int or start_time < 0:
            raise OpenDotaError(
                "GET /players/{account_id}/matches returned malformed match timestamp fields"
            )
        times.append(start_time)
    times.sort()
    if not times:
        return None, "no dated matches in this window"

    # A full page means OpenDota truncated the history; the oldest match in hand
    # is not the oldest match played, so any rate from it is a floor, not a rate.
    truncated = sample_limit is not None and len(matches) >= sample_limit

    window = window_days * SECONDS_PER_DAY
    cutoff = now - window
    recent = [t for t in times if t >= cutoff]

    if truncated and times[0] > cutoff:
        return None, "match history truncated - pace would be understated"
    if len(recent) < MIN_PACE_MATCHES:
        played_ago = int((now - times[-1]) / SECONDS_PER_DAY)
        return None, (
            f"only {len(recent)} ranked game(s) in the last {window_days} days"
            f" (last played {played_ago}d ago)"
        )
    return len(recent) * SECONDS_PER_WEEK / window, ""


def _with_coverage(
    lanes: dict[int, HeroLanes], heroes: dict[int, PlayerHero]
) -> dict[int, HeroLanes]:
    """Tell each lane record how many games it is speaking for.

    Without this a hero's lane winrate is quoted off however few matches got
    parsed, with no way to tell that it covers a third of the real record.
    """
    for hero_id, entry in lanes.items():
        played = heroes.get(hero_id)
        entry.played_games = played.games if played else entry.total_games
    return lanes


def _data_status(hero_rows: list[dict[str, Any]], heroes: dict[int, PlayerHero]) -> DataStatus:
    if heroes:
        return DataStatus.AVAILABLE
    # Rows came back but every one has zero games: the profile is public, the
    # window is just empty. Telling this player to change a privacy setting
    # would be wrong.
    if hero_rows:
        return DataStatus.EMPTY_WINDOW
    return DataStatus.PRIVATE_OR_UNAVAILABLE


def load_profile(
    client: OpenDotaClient,
    account_id: int,
    recent_days: int | None = 90,
    match_sample: int = 200,
    now: float | None = None,
) -> PlayerProfile:
    """Pull profile, per-hero record and a recent match sample in one go.

    `recent_days` scopes the per-hero record to a recent window so a hero you
    spammed three patches ago does not dominate the recommendation. Pass None to
    use the player's whole history. `now` is injectable so pace is testable.
    """
    ranked: dict[str, Any] = {
        "lobby_type": RANKED_LOBBY_TYPE,
        "game_mode": ALL_PICK_GAME_MODE,
        "date": recent_days,
    }

    raw = client.player(account_id)
    validate_player(raw)
    profile_block = raw.get("profile") or {}

    win_loss = client.player_win_loss(account_id, **ranked)
    validate_player_win_loss(win_loss)
    hero_rows = client.player_heroes(account_id, **ranked)
    validate_player_heroes(hero_rows)
    matches = client.player_matches(account_id, limit=match_sample, project=MATCH_FIELDS, **ranked)
    validate_match_rows(matches)
    matches = [m for m in matches if is_ranked_all_pick(m)]

    heroes: dict[int, PlayerHero] = {}
    for row in hero_rows:
        games = row["games"]
        if games <= 0:
            continue
        hero_id = row["hero_id"]
        heroes[hero_id] = PlayerHero(
            hero_id=hero_id,
            games=games,
            wins=row["win"],
            last_played=row.get("last_played") or 0,
        )

    clock = time.time() if now is None else now
    recent_cutoff = clock - PACE_WINDOW_DAYS * SECONDS_PER_DAY
    recent = [m for m in matches if (m.get("start_time") or 0) >= recent_cutoff]
    decided = [m for m in recent if match_outcome(m) is not None]
    recent_wins = sum(1 for m in decided if match_outcome(m))

    if recent_days is not None and recent_days < PACE_WINDOW_DAYS:
        pace = None
        pace_note = (
            f"requested history is only {recent_days} days; "
            f"pace requires the full {PACE_WINDOW_DAYS}-day window"
        )
    else:
        pace, pace_note = games_per_week(matches, now=clock, sample_limit=match_sample)

    return PlayerProfile(
        account_id=account_id,
        name=profile_block.get("personaname") or f"Player {account_id}",
        rank_tier=raw.get("rank_tier"),
        games=win_loss["win"] + win_loss["lose"],
        wins=win_loss["win"],
        heroes=heroes,
        lanes=_with_coverage(lane_stats(matches, match_outcome), heroes),
        games_per_week=pace,
        pace_note=pace_note,
        recent_games=len(decided),
        recent_wins=recent_wins,
        data_status=_data_status(hero_rows, heroes),
    )


def load_stratz_profile(client: StratzClient, account_id: int) -> PlayerProfile:
    """Build a profile only from Stratz's deliberately sparse hero aggregate.

    Stratz does not supply rank, name, dates, or lanes on this path. Those fields
    stay unknown rather than borrowing OpenDota row assumptions. An unfiltered
    aggregate validates access and coverage, but only the ranked-All-Pick rows
    enter the profile. Incomplete or anonymous aggregates expose no hero records.
    """
    raw = client.player_hero_performance(account_id)
    unavailable = PlayerProfile(
        account_id=account_id,
        name=f"Player {account_id}",
        rank_tier=None,
        games=0,
        wins=0,
        games_per_week=None,
        pace_note="Stratz hero aggregates have no match dates",
        data_status=DataStatus.PRIVATE_OR_UNAVAILABLE,
    )
    if raw is None or raw["isAnonymous"]:
        return unavailable

    total_matches = raw["matchCount"]
    coverage_rows = raw["allHistoryHeroesPerformance"]
    ranked_rows = raw["rankedAllPickHeroesPerformance"]
    covered_matches = sum(row["matchCount"] for row in coverage_rows)
    ranked_matches = sum(row["matchCount"] for row in ranked_rows)
    expected_matches = min(total_matches, PLAYER_MATCH_DEPTH)
    if covered_matches > expected_matches or ranked_matches > expected_matches:
        raise StratzError("Stratz player hero aggregates exceeded the plausible match count")
    if total_matches <= PLAYER_MATCH_DEPTH and ranked_matches > covered_matches:
        raise StratzError("Stratz ranked hero aggregate exceeded the all-history aggregate")
    if total_matches == 0:
        unavailable.data_status = DataStatus.EMPTY_WINDOW
        return unavailable
    if covered_matches * 100 < expected_matches * MIN_STRATZ_COVERAGE_PERCENT:
        return unavailable

    heroes = {
        row["heroId"]: PlayerHero(
            hero_id=row["heroId"],
            games=row["matchCount"],
            wins=row["winCount"],
        )
        for row in ranked_rows
        if row["matchCount"] > 0
    }
    if not heroes:
        unavailable.data_status = DataStatus.EMPTY_WINDOW
        return unavailable
    return PlayerProfile(
        account_id=account_id,
        name=f"Player {account_id}",
        rank_tier=None,
        games=ranked_matches,
        wins=sum(hero.wins for hero in heroes.values()),
        heroes=heroes,
        games_per_week=None,
        pace_note="Stratz hero aggregates have no match dates",
        data_status=DataStatus.AVAILABLE,
    )
