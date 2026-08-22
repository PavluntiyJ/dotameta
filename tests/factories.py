"""Builders for payloads shaped like real OpenDota responses."""

from __future__ import annotations


def hero_row(
    hero_id: int,
    name: str,
    *,
    picks: int,
    winrate: float,
    medal: int = 5,
    roles: tuple[str, ...] = ("Carry",),
    trend: tuple[list[int], list[int]] | None = None,
) -> dict:
    """One /heroStats row, with pick/win counts filled in for a single bracket."""
    wins = round(picks * winrate)
    row = {
        "id": hero_id,
        "localized_name": name,
        "roles": list(roles),
        "pub_pick": picks,
        "pub_win": wins,
        f"{medal}_pick": picks,
        f"{medal}_win": wins,
    }
    for other in range(1, 9):
        row.setdefault(f"{other}_pick", 0)
        row.setdefault(f"{other}_win", 0)
    if trend:
        row["pub_pick_trend"], row["pub_win_trend"] = trend
    return row
