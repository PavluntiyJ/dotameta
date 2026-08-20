"""Work out which lane a player actually plays each hero in.

OpenDota tags every match with `lane_role` (1 safe, 2 mid, 3 off, 4 jungle) and
an `is_roaming` flag. That is lane, **not** position: safe lane holds both the
carry and the hard support, so this module reports lanes and never invents a
"pos 1-5" number the data cannot support.

Useful anyway, because it answers the question a spammer actually has - not
"which hero", but "where do I put it". Lane records remain one recommendation per hero, not separate rankings.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

LANE_NAMES = {
    1: "safe",
    2: "mid",
    3: "off",
    4: "jungle",
}

ROAMING = "roam"

# `lane_role` only exists on matches OpenDota actually parsed - in practice
# around a third of them. So a hero's lane sample is much smaller than its game
# count, and the two thresholds below are deliberately different:
#
#   * which lane you play a hero in is a stable preference, readable from few games
#   * how well you do in that lane is a winrate, and needs a real sample
#
# A small parsed subset cannot support a confident lane winrate.
MIN_LANE_GAMES = 5
MIN_LANE_WINRATE_GAMES = 15

# The parsed games must also cover most of the games actually played.
MIN_LANE_COVERAGE = 0.5


@dataclass
class LaneRecord:
    games: int = 0
    wins: int = 0

    @property
    def winrate(self) -> float:
        return self.wins / self.games if self.games else 0.0


@dataclass
class HeroLanes:
    hero_id: int
    by_lane: dict[str, LaneRecord] = field(default_factory=dict)
    played_games: int = 0  # total games on the hero, parsed or not

    @property
    def total_games(self) -> int:
        return sum(record.games for record in self.by_lane.values())

    @property
    def main_lane(self) -> str | None:
        """The lane this hero is most often played in, if there is enough data."""
        if not self.by_lane:
            return None
        lane, record = max(self.by_lane.items(), key=lambda item: item[1].games)
        return lane if record.games >= MIN_LANE_GAMES else None

    @property
    def coverage(self) -> float:
        """Share of this hero's games that OpenDota parsed a lane for."""
        if not self.played_games:
            return 1.0 if self.total_games else 0.0
        return self.total_games / self.played_games

    @property
    def reported_lane(self) -> tuple[str, LaneRecord] | None:
        """The lane actually played most, with its record - never the best one.

        Picking the highest-winrate lane out of several was a raw-winrate
        ranking with a multiple-comparisons bias baked in: 10/15 mid beat 60/100
        safe on 66.7%, and the tool then told the player mid was their better
        lane. Where you play a hero is a preference we can read; which lane suits
        you better is a causal claim this data cannot support.
        """
        if self.coverage < MIN_LANE_COVERAGE:
            return None
        lane = self.main_lane
        if lane is None:
            return None
        record = self.by_lane[lane]
        if record.games < MIN_LANE_WINRATE_GAMES:
            return None
        return lane, record

    def summary(self) -> str:
        """Lane advice, with a winrate only when it is earned.

        "off 62% (21)"  - main lane, enough parsed games to state a winrate
        "off"           - we know where you play it, not how well
        ""              - not even the lane is known
        """
        reported = self.reported_lane
        if reported is not None:
            lane, record = reported
            return f"{lane} {record.winrate:.0%} ({record.games})"
        return self.main_lane or ""


def lane_of(match: dict[str, Any]) -> str | None:
    """Lane label for one match, or None when OpenDota could not classify it."""
    if match.get("is_roaming"):
        return ROAMING
    lane_role = match.get("lane_role")
    if lane_role is None:
        return None
    return LANE_NAMES.get(int(lane_role))


def lane_stats(
    matches: list[dict[str, Any]], outcome: Callable[[dict[str, Any]], bool | None]
) -> dict[int, HeroLanes]:
    """Per-hero lane breakdown from a player's match list.

    `outcome` is injected rather than imported to keep this module free of any
    dependency on how a win is derived from a player slot. It is tri-state:
    a match whose result cannot be determined is dropped entirely rather than
    counted as a loss.
    """
    result: dict[int, HeroLanes] = defaultdict(lambda: HeroLanes(hero_id=0))

    for match in matches:
        hero_id = match.get("hero_id")
        lane = lane_of(match)
        if hero_id is None or lane is None:
            continue
        hero_id = int(hero_id)
        entry = result[hero_id]
        entry.hero_id = hero_id
        won = outcome(match)
        if won is None:
            continue  # unknown result: not a win, and not a loss either
        record = entry.by_lane.setdefault(lane, LaneRecord())
        record.games += 1
        if won:
            record.wins += 1

    return dict(result)
