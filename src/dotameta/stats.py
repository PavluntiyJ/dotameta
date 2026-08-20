"""Small statistics helpers.

Winrates in Dota data are almost always ratios over small, uneven samples: a hero
with 3 games at 100% is not better than one with 400 games at 55%. Everything the
recommender ranks on therefore goes through a shrinkage or interval estimate here
rather than through a raw ratio.
"""

from __future__ import annotations

import math


def winrate(wins: float, games: float) -> float:
    """Plain ratio; 0.0 for an empty sample (callers should weight by `games`)."""
    return wins / games if games else 0.0


def wilson_lower_bound(wins: float, games: float, z: float = 1.96) -> float:
    """Lower edge of the Wilson score interval - a sample-size-aware winrate.

    Small samples get pulled hard toward 0, so ranking by this value never puts a
    2-game hero on top. `z=1.96` is the lower edge of the standard two-sided 95%
    Wilson interval; it is not a one-sided 95% bound, and the interval as a whole
    is what carries the 95%, not this edge on its own.
    """
    if games <= 0:
        return 0.0
    p = wins / games
    denominator = 1 + z**2 / games
    centre = p + z**2 / (2 * games)
    margin = z * math.sqrt((p * (1 - p) + z**2 / (4 * games)) / games)
    return max(0.0, (centre - margin) / denominator)


def shrink_to_prior(wins: float, games: float, prior: float, strength: float = 20.0) -> float:
    """Bayesian shrinkage: a hero you played 5 times barely moves off the prior.

    `strength` is "how many games of evidence the prior is worth". 20 means a
    player needs ~20 games on a hero before their own winrate dominates the
    bracket baseline.
    """
    if games <= 0:
        return prior
    return (wins + strength * prior) / (games + strength)


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))
