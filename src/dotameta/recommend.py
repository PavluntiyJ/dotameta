"""The actual recommendation model: which hero should this player spam?

The question "what should I spam to climb" is really a question about expected MMR
per game, which decomposes into two things we can measure:

  1. how strong the hero is in *this player's bracket* (API meta counts), and
  2. how well *this player* performs on it (ranked-All-Pick hero counts).

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

# Games before a hero is a spam candidate rather than merely a working pick.
SPAM_GAMES = 50

CATEGORY_SPAM = "spam"  # you know it, it works: put your volume here
CATEGORY_KEEP = "keep"  # works, but not enough games to lean on it yet
CATEGORY_RISKY = "risky"  # too little evidence to commit volume to it
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
    relative_pick_frequency: float
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
        can advertise a thin-sample hero that the model itself values negatively
        and ranks accordingly.
        """
        return (2 * self.adjusted_winrate - 1) * 100 * MMR_PER_WIN

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
        """What the list is ordered by: the same number the MMR column shows."""
        return self.adjusted_winrate

    @property
    def is_evidence_backed(self) -> bool:
        """Positive after the confidence discount - i.e. worth actual volume.

        The one gate `spam_plan` may not skip. A hero can look good optimistically
        and still be a losing bet once its sample size is priced in.
        """
        return self.adjusted_winrate > 0.5


@dataclass
class SpamPlan:
    """The prescription: which heroes, and what the model thinks they are worth.

    `mmr_*` are ranges. The low end is the heuristic discounted estimate and the
    high end the blended one; the width shows how much of the projection rests on
    sample size rather than on demonstrated skill. `games_per_week` is None when
    no honest pace could be measured, and `pace_note` says why.
    """

    pool: list[Recommendation]
    expected_winrate: float = 0.0
    adjusted_winrate: float = 0.0
    games_per_week: float | None = None
    pace_note: str = ""

    def __bool__(self) -> bool:
        return bool(self.pool)

    @property
    def mmr_per_100_low(self) -> float | None:
        """None on an empty pool: no heroes means no projection, not a bad one.

        Defaulting the winrates to 0.0 and computing anyway reported -250 MMR/week
        for a player we had simply declined to prescribe anything to.
        """
        if not self.pool:
            return None
        return (2 * self.adjusted_winrate - 1) * 100 * MMR_PER_WIN

    @property
    def mmr_per_100_high(self) -> float | None:
        if not self.pool:
            return None
        return (2 * self.expected_winrate - 1) * 100 * MMR_PER_WIN

    @property
    def mmr_per_week_low(self) -> float | None:
        if not self.pool or self.games_per_week is None:
            return None
        return (2 * self.adjusted_winrate - 1) * self.games_per_week * MMR_PER_WIN

    @property
    def mmr_per_week_high(self) -> float | None:
        if not self.pool or self.games_per_week is None:
            return None
        return (2 * self.expected_winrate - 1) * self.games_per_week * MMR_PER_WIN


def meta_expectation(entry: HeroMeta) -> float:
    """Bracket winrate, pulled toward 50% when the bracket sample is thin."""
    return shrink_to_prior(entry.wins, entry.picks, 0.5, META_PRIOR_STRENGTH)


def uncertainty_discount(winrate: float, games: float, z: float = 1.0) -> float:
    """One standard error of the blended estimate, as a confidence penalty.

    This is a **heuristic** haircut, not a calibrated interval: it subtracts a
    single standard error and claims no coverage probability. Do not describe the
    result as a confidence bound, and do not tell a user the low end is "what
    happens if your edge is noise" - for an unplayed hero carrying a strong meta
    prior it can push a 56% expectation down to 46%, which is not a statement
    about that player at all.

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


def _categorise(games: int, expected: float, adjusted: float, meta_entry: HeroMeta) -> str:
    """Turn the numbers into the advice a player asked for.

    Two different questions, and conflating them mislabels heroes in both
    directions:

      * *Are you winning on it?* -> `expected`, the blended estimate.
      * *Is that edge proven enough to build a plan on?* -> `adjusted`.

    So `drop` means you are actually losing on the hero, and is decided on
    `expected`. A positive record can be unproven without being a loss.
    `spam` and `keep` require `adjusted` to be positive, so they never sit next
    to a negative projection; anything winning but not yet proven lands in
    `risky`.

    The two cases worth getting right are opposites:

      * 1000 games at 53% on a hero the bracket wins 51% with. You underperform
        the hero slightly, but you know it cold and it is strong - spam it.
      * 1 game on a hero the bracket wins 56% with. Worth trying, but calling it
        a climbing plan would be reading one coin flip as a trend.
    """
    if games == 0:
        # Never played. Only worth naming as something to learn if the bracket
        # itself does well on it; otherwise there is no personal record to drop.
        return CATEGORY_LEARN if meta_entry.winrate >= 0.5 else CATEGORY_RISKY
    if expected < 0.5:
        return CATEGORY_DROP
    if adjusted > 0.5:
        return CATEGORY_SPAM if games >= SPAM_GAMES else CATEGORY_KEEP
    return CATEGORY_RISKY


def _explain(
    rec_games: int,
    personal: float,
    entry: HeroMeta,
    expected: float,
    adjusted: float,
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
    # Pick frequency only. It is not a ban rate: a hero can be rare *because* it
    # gets banned, and OpenDota publishes no public ban statistics to separate
    # the two. The old "low ban risk" line asserted exactly that missing evidence.
    freq = entry.relative_pick_frequency
    if freq >= 2.0 or (freq <= 0.4 and entry.picks):
        reasons.append(f"picked {freq:.1f}x as often as an average hero")
    if entry.trend_label != "stable":
        reasons.append(
            f"global public winrate {entry.trend_label} ({entry.trend:+.1%}) - "
            f"informational, not used in ranking"
        )
    reasons.append(f"blended expectation {expected:.1%}")
    conservative_mmr = (2 * adjusted - 1) * 100 * MMR_PER_WIN
    reasons.append(
        f"adjusted winrate {adjusted:.1%} after the heuristic uncertainty discount; "
        f"used for ranking and conservative MMR {conservative_mmr:+.0f} per 100 games"
    )
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
        # An unplayed weak hero has neither personal evidence nor a meta reason
        # to mention it. Omitting it avoids presenting zero-game records as risky
        # or drop while preserving those verdicts for heroes the player tried.
        if played.games == 0 and entry.winrate < 0.5:
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
                relative_pick_frequency=entry.relative_pick_frequency,
                trend=entry.trend,
                category=_categorise(played.games, expected, adjusted, entry),
                mastery=mastery_of(played.games),
                lane=profile.hero_lanes(hero_id).summary(),
                reasons=_explain(played.games, played.winrate, entry, expected, adjusted),
                roles=entry.roles,
            )
        )

    results.sort(key=lambda rec: rec.rank_key, reverse=True)
    return results


def spam_plan(
    recommendations: list[Recommendation],
    profile: PlayerProfile,
    pool_size: int = 3,
) -> SpamPlan:
    """Pick a small pool to spam and project what it is worth.

    A pool rather than a single hero: one hero gets banned or contested, and Dota
    matchmaking punishes a one-trick when the hero is picked or countered. Three
    is the usual advice - enough coverage, few enough to actually master.

    The pool may come back shorter than `pool_size`, or empty. That is the point:
    every member must clear `is_evidence_backed`, so "you have nothing worth
    spamming yet" is a result the CLI can state rather than something to pad over.
    """
    eligible = [
        rec
        for rec in recommendations
        if rec.is_evidence_backed and rec.category in (CATEGORY_SPAM, CATEGORY_KEEP)
    ]
    pool = sorted(eligible, key=lambda rec: rec.rank_key, reverse=True)[:pool_size]
    pace = profile.games_per_week

    if not pool:
        return SpamPlan(pool=[], games_per_week=pace, pace_note=profile.pace_note)

    # Both ends are means over the pool, which assumes the player splits games
    # evenly across it - see the limitations section of the README.
    expected = sum(rec.expected_winrate for rec in pool) / len(pool)
    adjusted = sum(rec.adjusted_winrate for rec in pool) / len(pool)
    return SpamPlan(
        pool=pool,
        expected_winrate=expected,
        adjusted_winrate=adjusted,
        games_per_week=pace,
        pace_note=profile.pace_note,
    )
