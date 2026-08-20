"""Fetch and normalise everything we know about one player.

Every personal endpoint is filtered to **ranked All Pick** (`lobby_type=7`,
`game_mode=22`). Without that filter a player's winrate, hero categories, lane
split and pace all silently absorb unranked and Turbo games, and the tool then
projects ranked MMR off them. The filters keep the projection on the intended population.

Note the asymmetry this creates, and do not paper over it: the bracket side of
the model comes from `/heroStats`, which OpenDota documents only as a public
per-medal aggregate. It is *not* guaranteed to be ranked-All-Pick-only, so the
two sides of the comparison are filtered differently. See README.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from .constants import format_rank_tier, medal_from_rank_tier
from .lanes import HeroLanes, lane_stats
from .opendota import OpenDotaClient
from .stats import winrate

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
    return bool(radiant_win) == (int(player_slot) < 128)


def is_ranked_all_pick(match: dict[str, Any]) -> bool:
    """Second line of defence: rows are filtered server-side, verified here.

    Match rows that predate the filter, or come from a cache written before it,
    would otherwise leak into the lane split.
    """
    lobby = match.get("lobby_type")
    mode = match.get("game_mode")
    if lobby is not None and int(lobby) != RANKED_LOBBY_TYPE:
        return False
    return not (mode is not None and int(mode) != ALL_PICK_GAME_MODE)


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
    times = sorted(int(m["start_time"]) for m in matches if m.get("start_time"))
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
    profile_block = raw.get("profile") or {}

    win_loss = client.player_win_loss(account_id, **ranked)
    hero_rows = client.player_heroes(account_id, **ranked)
    matches = client.player_matches(account_id, limit=match_sample, project=MATCH_FIELDS, **ranked)
    matches = [m for m in matches if is_ranked_all_pick(m)]

    heroes: dict[int, PlayerHero] = {}
    for row in hero_rows:
        games = int(row.get("games") or 0)
        if games <= 0:
            continue
        hero_id = int(row["hero_id"])
        heroes[hero_id] = PlayerHero(
            hero_id=hero_id,
            games=games,
            wins=int(row.get("win") or 0),
            last_played=int(row.get("last_played") or 0),
        )

    clock = time.time() if now is None else now
    recent_cutoff = clock - PACE_WINDOW_DAYS * SECONDS_PER_DAY
    recent = [m for m in matches if int(m.get("start_time") or 0) >= recent_cutoff]
    decided = [m for m in recent if match_outcome(m) is not None]
    recent_wins = sum(1 for m in decided if match_outcome(m))

    pace, pace_note = games_per_week(matches, now=clock, sample_limit=match_sample)

    return PlayerProfile(
        account_id=account_id,
        name=profile_block.get("personaname") or f"Player {account_id}",
        rank_tier=raw.get("rank_tier"),
        games=int(win_loss.get("win") or 0) + int(win_loss.get("lose") or 0),
        wins=int(win_loss.get("win") or 0),
        heroes=heroes,
        lanes=_with_coverage(lane_stats(matches, _decided_win), heroes),
        games_per_week=pace,
        pace_note=pace_note,
        recent_games=len(decided),
        recent_wins=recent_wins,
        data_status=_data_status(hero_rows, heroes),
    )


def _decided_win(match: dict[str, Any]) -> bool | None:
    return match_outcome(match)


# Kept as the documented public name for the win derivation.
is_win: Callable[[dict[str, Any]], bool | None] = match_outcome
