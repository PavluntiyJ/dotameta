"""Fixtures shaped like real OpenDota payloads.

Every test here is offline on purpose: the recommender's value is in its scoring
decisions, and those must be pinned against fixed numbers rather than against a
live meta that changes every patch.
"""

from __future__ import annotations

import pytest

from dotameta.player import PlayerHero, PlayerProfile
from factories import hero_row


@pytest.fixture
def hero_stats() -> list[dict]:
    return [
        hero_row(1, "Strong Meta Hero", picks=100_000, winrate=0.55),
        hero_row(2, "Average Hero", picks=100_000, winrate=0.50),
        hero_row(3, "Weak Hero", picks=100_000, winrate=0.45),
        hero_row(4, "Tiny Sample Hero", picks=30, winrate=0.90),
        hero_row(5, "Support Hero", picks=80_000, winrate=0.52, roles=("Support",)),
    ]


@pytest.fixture
def profile() -> PlayerProfile:
    return PlayerProfile(
        account_id=1,
        name="Tester",
        rank_tier=51,  # Legend 1
        games=300,
        wins=150,
        heroes={
            # Long, convincing record on a hero the bracket dislikes.
            3: PlayerHero(hero_id=3, games=200, wins=120),
            # Tiny hot streak that must not outrank the above.
            2: PlayerHero(hero_id=2, games=4, wins=4),
        },
        games_per_week=10.0,
    )
