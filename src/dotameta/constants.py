"""Rank brackets and other Dota constants that OpenDota does not hand us directly."""

from __future__ import annotations

# OpenDota `rank_tier` is a two-digit number: tens = medal, ones = star.
# heroStats exposes public pick/win counts per medal as "<medal>_pick" / "<medal>_win".
MEDALS: dict[int, str] = {
    1: "Herald",
    2: "Guardian",
    3: "Crusader",
    4: "Archon",
    5: "Legend",
    6: "Ancient",
    7: "Divine",
    8: "Immortal",
}

MEDAL_BY_NAME: dict[str, int] = {name.lower(): medal for medal, name in MEDALS.items()}

# One win is worth roughly this much MMR in ranked matchmaking. Valve does not publish
# the number and it drifts with uncertainty/calibration, so treat it as an estimate.
MMR_PER_WIN = 25


def medal_from_rank_tier(rank_tier: int | None) -> int | None:
    """Herald..Immortal medal (1-8) from an OpenDota rank_tier, or None if unranked."""
    if not rank_tier:
        return None
    return rank_tier // 10


def format_rank_tier(rank_tier: int | None) -> str:
    medal = medal_from_rank_tier(rank_tier)
    if medal is None:
        return "Unranked"
    name = MEDALS.get(medal, f"Tier {medal}")
    star = rank_tier % 10 if rank_tier else 0
    return f"{name} {star}" if star else name


def parse_bracket(value: str | int | None) -> int | None:
    """Accept a medal number (1-8) or a medal name ('divine') from the CLI."""
    if value is None:
        return None
    if isinstance(value, int):
        return value if 1 <= value <= 8 else None
    text = str(value).strip().lower()
    if text.isdigit():
        medal = int(text)
        return medal if 1 <= medal <= 8 else None
    return MEDAL_BY_NAME.get(text)
