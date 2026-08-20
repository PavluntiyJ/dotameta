"""Turn OpenDota /heroStats into a per-bracket picture of the current meta.

`/heroStats` returns one row per hero with public pick/win counts split by rank
medal, exposed as "<medal>_pick" / "<medal>_win" (1 = Herald .. 8 = Immortal),
plus `pub_pick_trend` / `pub_win_trend`: week-by-week arrays (oldest first) that
let us tell a hero on the way up from one on the way down.

Two things about that payload bite in practice and are handled here:

  * Some brackets are empty. Immortal (`8_*`) is routinely all zeros, so asking
    for it naively yields a meta where every hero has 0 picks. `resolve_bracket`
    walks down to the nearest populated medal instead.
  * `turbo_picks` / `turbo_wins` exist alongside the pub numbers. They are never
    used here - turbo winrates do not transfer to ranked all-pick.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .stats import wilson_lower_bound, winrate


@dataclass(frozen=True)
class HeroMeta:
    hero_id: int
    name: str
    picks: int
    wins: int
    winrate: float
    winrate_lb: float
    pick_rate: float  # share of all hero slots picked in the bracket
    contest_rate: float  # pick_rate normalised so 1.0 == an average hero
    delta_vs_baseline: float  # winrate minus the bracket's average winrate
    trend: float = 0.0  # recent-week winrate minus earlier-week winrate
    primary_attr: str = ""
    roles: tuple[str, ...] = ()

    @property
    def is_meta(self) -> bool:
        """Picked at least as often as an average hero and above 50% win."""
        return self.contest_rate >= 1.0 and self.winrate >= 0.5

    @property
    def trend_label(self) -> str:
        if self.trend >= 0.015:
            return "rising"
        if self.trend <= -0.015:
            return "falling"
        return "stable"


def bracket_counts(row: dict[str, Any], medal: int | None) -> tuple[int, int]:
    """(picks, wins) for one hero in one medal bracket.

    `medal=None` means "all public matches". Missing brackets return (0, 0)
    rather than raising - OpenDota omits or zeroes keys for empty brackets.
    """
    if medal is None:
        return int(row.get("pub_pick") or 0), int(row.get("pub_win") or 0)
    pick_key, win_key = f"{medal}_pick", f"{medal}_win"
    if pick_key in row:
        return int(row.get(pick_key) or 0), int(row.get(win_key) or 0)
    return 0, 0


def bracket_total_picks(hero_stats: list[dict[str, Any]], medal: int | None) -> int:
    return sum(bracket_counts(row, medal)[0] for row in hero_stats)


def resolve_bracket(hero_stats: list[dict[str, Any]], medal: int | None) -> int | None:
    """Nearest medal that actually has data, searching downward then upward.

    Immortal is the usual casualty: `8_pick` is all zeros in current payloads, so
    a Divine/Immortal player transparently gets Divine (7) numbers.
    """
    if medal is None:
        return None
    if bracket_total_picks(hero_stats, medal) > 0:
        return medal
    for candidate in range(medal - 1, 0, -1):
        if bracket_total_picks(hero_stats, candidate) > 0:
            return candidate
    for candidate in range(medal + 1, 9):
        if bracket_total_picks(hero_stats, candidate) > 0:
            return candidate
    return None  # nothing usable anywhere: fall back to all public matches


def winrate_trend(row: dict[str, Any], recent_weeks: int = 2) -> float:
    """Recent winrate minus earlier winrate, from the pub_*_trend arrays.

    Positive means the hero has been winning more lately - either a buff landed or
    players are figuring the hero out. Returns 0.0 when the arrays are absent or
    too short to compare.
    """
    picks = row.get("pub_pick_trend") or []
    wins = row.get("pub_win_trend") or []
    if len(picks) != len(wins) or len(picks) <= recent_weeks:
        return 0.0
    recent_picks, recent_wins = sum(picks[-recent_weeks:]), sum(wins[-recent_weeks:])
    older_picks, older_wins = sum(picks[:-recent_weeks]), sum(wins[:-recent_weeks])
    if recent_picks < 500 or older_picks < 500:
        return 0.0
    return winrate(recent_wins, recent_picks) - winrate(older_wins, older_picks)


def build_meta(
    hero_stats: list[dict[str, Any]],
    medal: int | None,
    min_picks: int = 200,
) -> dict[int, HeroMeta]:
    """Index every hero's standing in one bracket, keyed by hero id.

    Heroes below `min_picks` are kept - you may still want to look at them - but
    their `winrate_lb` is zeroed so ranking pushes them down on its own.
    """
    rows = [(row, *bracket_counts(row, medal)) for row in hero_stats]

    total_picks = sum(picks for _, picks, _ in rows)
    total_wins = sum(wins for _, _, wins in rows)
    baseline = winrate(total_wins, total_picks) or 0.5
    hero_count = sum(1 for _, picks, _ in rows if picks > 0) or 1
    average_pick_rate = 1.0 / hero_count

    meta: dict[int, HeroMeta] = {}
    for row, picks, wins in rows:
        pick_rate = picks / total_picks if total_picks else 0.0
        rate = winrate(wins, picks)
        meta[int(row["id"])] = HeroMeta(
            hero_id=int(row["id"]),
            name=row.get("localized_name", f"hero {row['id']}"),
            picks=picks,
            wins=wins,
            winrate=rate,
            winrate_lb=wilson_lower_bound(wins, picks) if picks >= min_picks else 0.0,
            pick_rate=pick_rate,
            contest_rate=pick_rate / average_pick_rate if average_pick_rate else 0.0,
            delta_vs_baseline=rate - baseline if picks else 0.0,
            trend=winrate_trend(row),
            primary_attr=row.get("primary_attr", ""),
            roles=tuple(row.get("roles") or ()),
        )
    return meta


def baseline_winrate(meta: dict[int, HeroMeta]) -> float:
    """Average winrate across the bracket - should sit very close to 0.5."""
    total_picks = sum(entry.picks for entry in meta.values())
    total_wins = sum(entry.wins for entry in meta.values())
    return winrate(total_wins, total_picks) or 0.5


def top_meta_heroes(
    meta: dict[int, HeroMeta], limit: int = 20, role: str | None = None
) -> list[HeroMeta]:
    entries = list(meta.values())
    if role:
        wanted = role.lower()
        entries = [e for e in entries if wanted in {r.lower() for r in e.roles}]
    return sorted(entries, key=lambda entry: entry.winrate_lb, reverse=True)[:limit]
