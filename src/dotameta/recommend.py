"""The actual recommendation model: which hero should this player spam?

The question "what should I spam to climb" is really a question about expected MMR
per game, which decomposes into two things we can measure:

  1. how strong the hero is in *this player's bracket* (OpenDota /heroStats), and
  2. how well *this player* performs on it (OpenDota /players/{id}/heroes).

Neither is trustworthy alone. Bracket winrates are stable but say nothing about
you; personal winrates are about you but usually come from a handful of games. So
the personal record is shrunk toward the bracket expectation (see stats.py), and
the result is discounted by its own uncertainty before ranking. A hero you have
never played can still be recommended, it just cannot outrank a hero you already
win on unless the meta gap is large.

The output is expressed as MMR per 100 games rather than as an opaque score, so
the recommendation can be sanity-checked against reality.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .constants import MMR_PER_WIN
from .meta import HeroMeta
from .player import PlayerProfile
from .stats import clamp, shrink_to_prior

# How many games of evidence the bracket baseline is worth when blending in a
# player's own record. ~25 games before your winrate dominates the prior.
PERSONAL_PRIOR_STRENGTH = 25.0

# Same idea for the bracket itself: a hero with 300 picks in a bracket is pulled
# toward 50% much harder than one with 300k.
META_PRIOR_STRENGTH = 3000.0

# Games on a hero before we call it "comfortable" rather than "experimental".
COMFORT_GAMES = 20

# Games before a hero is a spam candidate rather than merely a working pick.
SPAM_GAMES = 50

# How much a hero's week-over-week winrate trend nudges the ranking. Deliberately
# small: momentum is a tiebreaker between similar heroes, not evidence of skill.
TREND_WEIGHT = 0.25

CATEGORY_SPAM = "spam"  # you know it, it works: put your volume here
CATEGORY_KEEP = "keep"  # works, but not enough games to lean on it yet
CATEGORY_RISKY = "risky"  # bracket likes it, you have barely played it
CATEGORY_LEARN = "learn"  # strong in your bracket, you have never played it
CATEGORY_DROP = "drop"  # you keep playing it and keep losing

# Experience tiers. Games on a hero are not just a confidence input - they are a
# reason to prefer it. A hero with 1000 games behind it is a known quantity: you
# have seen every matchup on it, and your winrate on it will not swing.
MASTERY_TIERS = (
    (300, "mastered"),
    (100, "experienced"),
    (30, "practiced"),
    (1, "thin"),
    (0, "untested"),
)


@dataclass
class Recommendation:
    hero_id: int
    name: str
    games: int
    wins: int
    personal_winrate: float
    meta_winrate: float
    expected_winrate: float  # blended, before the uncertainty discount
    adjusted_winrate: float  # what we actually rank on
    contest_rate: float
    trend: float
    category: str
    mastery: str = "untested"
    lane: str = ""
    reasons: list[str] = field(default_factory=list)
    roles: tuple[str, ...] = ()

    @property
    def mmr_per_100_games(self) -> float:
        """Optimistic projection: takes the blended winrate at face value.

        Only meaningful next to `mmr_per_100_games_conservative`. On a hero with
        two games played, the gap between the two is the whole story.
        """
        return (2 * self.expected_winrate - 1) * 100 * MMR_PER_WIN

    @property
    def mmr_per_100_games_conservative(self) -> float:
        """Projection from the winrate we actually rank on, after the discount.

        This is the number to show a user. Reporting the optimistic one instead
        can advertise a thin-sample hero that the model itself values negatively.
        """
        return (2 * self.adjusted_winrate - 1) * 100 * MMR_PER_WIN

    def mmr_per_week(self, games_per_week: float) -> float:
        return (2 * self.adjusted_winrate - 1) * games_per_week * MMR_PER_WIN

    @property
    def edge_vs_meta(self) -> float:
        """Your winrate minus the hero's winrate in your bracket.

        The number a player actually reasons with: positive means you get more
        out of this hero than your bracket does, which is the part of a spam pick
        that is about you rather than about the patch.
        """
        if not self.games:
            return 0.0
        return self.personal_winrate - self.meta_winrate

    @property
    def rank_key(self) -> float:
        """Uncertainty-discounted winrate, nudged by meta momentum."""
        return self.adjusted_winrate + TREND_WEIGHT * self.trend


def meta_expectation(entry: HeroMeta) -> float:
    """Bracket winrate, pulled toward 50% when the bracket sample is thin."""
    return shrink_to_prior(entry.wins, entry.picks, 0.5, META_PRIOR_STRENGTH)


def uncertainty_discount(winrate: float, games: float, z: float = 1.0) -> float:
    """Standard error of the blended estimate, used as a confidence penalty.

    Ranking on `expected - discount` is what stops a 4-game 75% hero from topping
    the list: its discount is huge, a 300-game hero's is nearly zero.
    """
    effective = games + PERSONAL_PRIOR_STRENGTH
    return z * math.sqrt(max(winrate * (1 - winrate), 0.01) / effective)


def mastery_of(games: int) -> str:
    for threshold, label in MASTERY_TIERS:
        if games >= threshold:
            return label
    return "untested"


def _categorise(games: int, expected: float, meta_entry: HeroMeta) -> str:
    """Turn the numbers into the advice a player asked for.

    The two cases worth getting right are opposites:

      * 1000 games at 53% on a hero the bracket wins 55% with. You underperform
        the hero slightly, but you know it cold and it is strong - spam it.
      * 1 game on a hero the bracket wins 56% with. Worth trying, but calling it
        a climbing plan would be reading one coin flip as a trend.
    """
    if games >= SPAM_GAMES and expected >= 0.5:
        return CATEGORY_SPAM
    if games >= COMFORT_GAMES:
        return CATEGORY_KEEP if expected >= 0.5 else CATEGORY_DROP
    if games == 0:
        return CATEGORY_LEARN
    # Under the comfort threshold, judge on the blend rather than on the bracket
    # alone: 15 games at 60% on a hero the bracket dislikes is worth another look,
    # not a verdict of "stop playing it".
    return CATEGORY_RISKY if expected >= 0.5 else CATEGORY_DROP


def _explain(
    rec_games: int,
    personal: float,
    entry: HeroMeta,
    expected: float,
) -> list[str]:
    reasons: list[str] = []
    if entry.winrate:
        reasons.append(
            f"bracket winrate {entry.winrate:.1%} over {entry.picks:,} picks"
            f" ({entry.delta_vs_baseline:+.1%} vs average)"
        )
    if rec_games:
        edge = personal - entry.winrate
        reasons.append(
            f"your record {personal:.1%} over {rec_games} games "
            f"({mastery_of(rec_games)}, {edge:+.1%} vs the bracket on this hero)"
        )
    else:
        reasons.append("you have not played it in this window")
    if entry.contest_rate >= 2.0:
        reasons.append(f"heavily contested ({entry.contest_rate:.1f}x average pick rate)")
    elif entry.contest_rate <= 0.4 and entry.picks:
        reasons.append(f"rarely picked ({entry.contest_rate:.1f}x average) - low ban risk")
    if entry.trend_label != "stable":
        reasons.append(f"{entry.trend_label} in the meta ({entry.trend:+.1%} week over week)")
    reasons.append(f"blended expectation {expected:.1%}")
    return reasons


def recommend(
    profile: PlayerProfile,
    meta: dict[int, HeroMeta],
    min_bracket_picks: int = 1000,
    role: str | None = None,
    include_unplayed: bool = True,
    min_games: int = 0,
) -> list[Recommendation]:
    """Rank every eligible hero for this player, best first.

    `min_bracket_picks` filters out heroes the bracket has barely played, whose
    winrate is noise. `min_games` does the same for the player's own record, which
    matters for veterans: someone with 80+ heroes played has, by chance alone, a
    few sitting at 85% over 20 games. The discount ranks those correctly but they
    still clutter the list, so a floor is the honest way to read a deep pool.
    `role` filters on the role tags OpenDota ships in /heroStats.
    """
    results: list[Recommendation] = []

    for hero_id, entry in meta.items():
        if entry.picks < min_bracket_picks:
            continue
        if role and role.lower() not in {r.lower() for r in entry.roles}:
            continue

        played = profile.hero(hero_id)
        if not include_unplayed and played.games == 0:
            continue
        if min_games and 0 < played.games < min_games:
            continue

        prior = meta_expectation(entry)
        expected = shrink_to_prior(played.wins, played.games, prior, PERSONAL_PRIOR_STRENGTH)
        expected = clamp(expected, 0.2, 0.8)
        adjusted = expected - uncertainty_discount(expected, played.games)

        results.append(
            Recommendation(
                hero_id=hero_id,
                name=entry.name,
                games=played.games,
                wins=played.wins,
                personal_winrate=played.winrate,
                meta_winrate=entry.winrate,
                expected_winrate=expected,
                adjusted_winrate=adjusted,
                contest_rate=entry.contest_rate,
                trend=entry.trend,
                category=_categorise(played.games, expected, entry),
                mastery=mastery_of(played.games),
                lane=profile.hero_lanes(hero_id).summary(),
                reasons=_explain(played.games, played.winrate, entry, expected),
                roles=entry.roles,
            )
        )

    results.sort(key=lambda rec: rec.rank_key, reverse=True)
    return results


def spam_plan(
    recommendations: list[Recommendation],
    profile: PlayerProfile,
    pool_size: int = 3,
) -> dict[str, object]:
    """Pick a small pool to spam and project the MMR it is worth.

    A pool rather than a single hero: one hero gets banned or contested, and Dota
    matchmaking punishes a one-trick when the hero is picked or countered. Three is
    the usual advice - enough coverage, few enough to actually master.
    """
    # Fill the pool with heroes the player can actually rely on before reaching
    # for ones they have barely touched.
    by_category: list[str] = [
        CATEGORY_SPAM,
        CATEGORY_KEEP,
        CATEGORY_RISKY,
        CATEGORY_LEARN,
    ]
    pool: list[Recommendation] = []
    for category in by_category:
        if len(pool) >= pool_size:
            break
        pool += [rec for rec in recommendations if rec.category == category]
    pool = pool[:pool_size]

    if not pool:
        return {
            "pool": [],
            "expected_winrate": 0.0,
            "adjusted_winrate": 0.0,
            "mmr_per_100_games": 0.0,
            "mmr_per_week": 0.0,
            "games_per_week": profile.games_per_week,
        }

    expected = sum(rec.expected_winrate for rec in pool) / len(pool)
    # Project from the discounted winrate, not the optimistic one: a projection
    # built on thin samples is exactly the number a user would act on.
    adjusted = sum(rec.adjusted_winrate for rec in pool) / len(pool)
    pace = profile.games_per_week
    return {
        "pool": pool,
        "expected_winrate": expected,
        "adjusted_winrate": adjusted,
        "mmr_per_100_games": (2 * adjusted - 1) * 100 * MMR_PER_WIN,
        "mmr_per_week": (2 * adjusted - 1) * pace * MMR_PER_WIN,
        "games_per_week": pace,
    }
