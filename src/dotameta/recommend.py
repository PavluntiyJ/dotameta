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

# How much a hero's week-over-week winrate trend nudges the ranking. Deliberately
# small: momentum is a tiebreaker between similar heroes, not evidence of skill.
TREND_WEIGHT = 0.25

CATEGORY_SPAM = "spam"
CATEGORY_LEARN = "learn"
CATEGORY_KEEP = "keep"
CATEGORY_DROP = "drop"


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
    reasons: list[str] = field(default_factory=list)
    roles: tuple[str, ...] = ()

    @property
    def mmr_per_100_games(self) -> float:
        return (2 * self.expected_winrate - 1) * 100 * MMR_PER_WIN

    def mmr_per_week(self, games_per_week: float) -> float:
        return (2 * self.expected_winrate - 1) * games_per_week * MMR_PER_WIN

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


def _categorise(games: int, expected: float, meta_entry: HeroMeta) -> str:
    if games >= COMFORT_GAMES:
        if expected >= 0.5:
            return CATEGORY_SPAM if meta_entry.winrate >= 0.485 else CATEGORY_KEEP
        return CATEGORY_DROP
    return CATEGORY_LEARN


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
        reasons.append(f"your record {personal:.1%} over {rec_games} games")
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
) -> list[Recommendation]:
    """Rank every eligible hero for this player, best first.

    `min_bracket_picks` filters out heroes the bracket has barely played, whose
    winrate is noise. `role` filters on the role tags OpenDota ships in /heroStats
    ("Carry", "Support", "Nuker", ...).
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
    pool = [rec for rec in recommendations if rec.category in (CATEGORY_SPAM, CATEGORY_KEEP)]
    pool = (pool + [r for r in recommendations if r.category == CATEGORY_LEARN])[:pool_size]

    if not pool:
        return {"pool": [], "expected_winrate": 0.0, "mmr_per_week": 0.0}

    expected = sum(rec.expected_winrate for rec in pool) / len(pool)
    pace = profile.games_per_week
    return {
        "pool": pool,
        "expected_winrate": expected,
        "mmr_per_100_games": (2 * expected - 1) * 100 * MMR_PER_WIN,
        "mmr_per_week": (2 * expected - 1) * pace * MMR_PER_WIN,
        "games_per_week": pace,
    }
