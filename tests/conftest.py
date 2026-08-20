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


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Hard-block sockets for the whole suite.

    The rule "tests never hit the network" was documented but not enforced; a
    stray real client in a new test would have quietly made live calls and coupled
    CI to whatever the live meta looked like that day.
    """
    import socket

    def refuse(*args, **kwargs):
        raise RuntimeError("network access is disabled in tests")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch, tmp_path):
    """Never let the developer's own .env or exported keys reach a test.

    The suite started failing the moment a real STRATZ_API_TOKEN existed in the
    working directory: `Settings.from_env()` reads `.env` from the cwd, so tests
    asserting "no token" quietly became tests of whoever ran them.
    """
    import dotameta.config as config

    for name in config.ALLOWED_KEYS:
        monkeypatch.delenv(name, raising=False)
    # Point the loader at a directory that has no .env at all.
    monkeypatch.setattr(config, "ENV_FILE", str(tmp_path / ".env"))
