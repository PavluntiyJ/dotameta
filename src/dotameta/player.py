"""Fetch and normalise everything we know about one player.

Only public match data is visible to OpenDota. If a player has not enabled
"Expose Public Match Data" in the Dota client, `/players/{id}/heroes` comes back
empty and the recommender degrades to pure meta advice - `PlayerProfile.has_match_data`
signals that so the CLI can say so out loud instead of silently ranking nothing.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from .constants import format_rank_tier, medal_from_rank_tier
from .lanes import HeroLanes, lane_stats
from .opendota import OpenDotaClient
from .stats import winrate

SECONDS_PER_WEEK = 7 * 24 * 3600


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
    account_id: int
    name: str
    rank_tier: int | None
    games: int
    wins: int
    heroes: dict[int, PlayerHero] = field(default_factory=dict)
    lanes: dict[int, HeroLanes] = field(default_factory=dict)
    games_per_week: float = 0.0
    recent_games: int = 0
    recent_wins: int = 0

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


def is_win(match: dict[str, Any]) -> bool:
    """Slots 0-127 are Radiant, 128+ are Dire - OpenDota never stores "did I win"."""
    player_slot = match.get("player_slot")
    radiant_win = match.get("radiant_win")
    if player_slot is None or radiant_win is None:
        return False
    return bool(radiant_win) == (int(player_slot) < 128)


def _games_per_week(matches: list[dict[str, Any]]) -> float:
    """Recent pace, used to convert a winrate edge into MMR per week."""
    times = sorted(int(m["start_time"]) for m in matches if m.get("start_time"))
    if len(times) < 2:
        return 0.0
    span = times[-1] - times[0]
    if span <= 0:
        return 0.0
    # Ignore a long idle gap before the sample by measuring only the played span.
    return len(times) * SECONDS_PER_WEEK / span


def load_profile(
    client: OpenDotaClient,
    account_id: int,
    recent_days: int | None = 90,
    match_sample: int = 200,
) -> PlayerProfile:
    """Pull profile, per-hero record and a recent match sample in one go.

    `recent_days` scopes the per-hero record to a recent window so a hero you
    spammed three patches ago does not dominate the recommendation. Pass None to
    use the player's whole history.
    """
    raw = client.player(account_id)
    profile_block = raw.get("profile") or {}

    win_loss = client.player_win_loss(account_id, date=recent_days)
    hero_rows = client.player_heroes(account_id, date=recent_days)
    matches = client.player_matches(
        account_id,
        limit=match_sample,
        date=recent_days,
        project=["start_time", "hero_id", "lane_role", "is_roaming"],
    )

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

    recent_cutoff = time.time() - 30 * 24 * 3600
    recent = [m for m in matches if int(m.get("start_time") or 0) >= recent_cutoff]
    recent_wins = sum(1 for m in recent if is_win(m))

    return PlayerProfile(
        account_id=account_id,
        name=profile_block.get("personaname") or f"Player {account_id}",
        rank_tier=raw.get("rank_tier"),
        games=int(win_loss.get("win") or 0) + int(win_loss.get("lose") or 0),
        wins=int(win_loss.get("win") or 0),
        heroes=heroes,
        lanes=_with_coverage(lane_stats(matches, is_win), heroes),
        games_per_week=_games_per_week(matches),
        recent_games=len(recent),
        recent_wins=recent_wins,
    )
