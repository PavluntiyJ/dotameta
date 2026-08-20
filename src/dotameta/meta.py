"""Turn OpenDota /heroStats into a per-bracket picture of the current meta.

`/heroStats` returns one row per hero with public pick/win counts split by rank
medal, exposed as "<medal>_pick" / "<medal>_win" (1 = Herald .. 8 = Immortal),
plus `pub_pick_trend` / `pub_win_trend`: short global bins (oldest first, final
bin partial) covering all brackets at once - see `winrate_trend`.

Two things about that payload bite in practice and are handled here:

  * Some brackets are empty. Immortal (`8_*`) is routinely all zeros, so asking
    for it naively yields a meta where every hero has 0 picks. `resolve_bracket`
    substitutes the nearest populated medal instead.
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
    relative_pick_frequency: float  # normalised so 1.0 == an average hero
    delta_vs_baseline: float  # winrate minus the bracket's average winrate
    trend: float = 0.0  # global short-term winrate movement; NOT bracket-specific
    primary_attr: str = ""
    roles: tuple[str, ...] = ()

    @property
    def is_meta(self) -> bool:
        """Picked at least as often as an average hero and above 50% win."""
        return self.relative_pick_frequency >= 1.0 and self.winrate >= 0.5

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
    """Nearest populated medal by absolute distance, preferring the lower one.

    Immortal is the usual casualty: `8_pick` is all zeros in current payloads, so
    an Immortal player transparently gets Divine (7) numbers.

    Scanning all the way down before looking up - as this did originally - could
    answer Herald for an empty Archon while Legend sat populated one step away.
    Ties break downward: a bracket below you understates how strong a hero will
    look for you, which is the safer direction to be wrong in.
    """
    if medal is None:
        return None
    if bracket_total_picks(hero_stats, medal) > 0:
        return medal
    candidates = sorted(
        (c for c in range(1, 9) if c != medal),
        key=lambda c: (abs(c - medal), c > medal),
    )
    for candidate in candidates:
        if bracket_total_picks(hero_stats, candidate) > 0:
            return candidate
    return None  # nothing usable anywhere: fall back to all public matches


def winrate_trend(row: dict[str, Any], window: int = 3) -> float:
    """Short-term movement in this hero's **global public** winrate.

    Three things the name must not hide:

      * It is global. `pub_*_trend` is not split by medal, so it says nothing
        about the bracket the rest of this module reports on. Informational only;
        it does not enter the ranking.
      * The final bin is partial - live payloads show a last bin a fraction of
        its neighbours' size - so it is dropped rather than compared.
      * The halves compared are equal-length completed bins. An unequal split
        would report a difference in bin length as a difference in winrate.

    Returns 0.0 when the arrays are missing, mismatched, or too short.
    """
    picks = list(row.get("pub_pick_trend") or [])
    wins = list(row.get("pub_win_trend") or [])
    if len(picks) != len(wins) or len(picks) < 2 * window + 1:
        return 0.0
    picks, wins = picks[:-1], wins[:-1]  # drop the partial current bin

    recent_picks = sum(picks[-window:])
    recent_wins = sum(wins[-window:])
    older_picks = sum(picks[-2 * window : -window])
    older_wins = sum(wins[-2 * window : -window])
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
    counts = {int(row["id"]): bracket_counts(row, medal) for row in hero_stats}
    info = {
        int(row["id"]): {
            "name": row.get("localized_name", f"hero {row['id']}"),
            "primary_attr": row.get("primary_attr", ""),
            "roles": tuple(row.get("roles") or ()),
            "trend": winrate_trend(row),
        }
        for row in hero_stats
    }
    return assemble_meta(counts, info, min_picks)


def assemble_meta(
    counts: dict[int, tuple[int, int]],
    info: dict[int, dict[str, Any]],
    min_picks: int = 200,
) -> dict[int, HeroMeta]:
    """Turn raw (picks, wins) per hero into ranked `HeroMeta`.

    Shared by every source so the aggregation - baseline, pick share, Wilson
    bound - is computed identically no matter where the counts came from. A
    second source that recomputed any of this by hand would drift from the first
    without anyone noticing.
    """
    total_picks = sum(picks for picks, _ in counts.values())
    total_wins = sum(wins for _, wins in counts.values())
    baseline = winrate(total_wins, total_picks) or 0.5
    hero_count = sum(1 for picks, _ in counts.values() if picks > 0) or 1
    average_pick_rate = 1.0 / hero_count

    meta: dict[int, HeroMeta] = {}
    for hero_id, (picks, wins) in counts.items():
        details = info.get(hero_id, {})
        pick_rate = picks / total_picks if total_picks else 0.0
        rate = winrate(wins, picks)
        meta[hero_id] = HeroMeta(
            hero_id=hero_id,
            name=details.get("name", f"hero {hero_id}"),
            picks=picks,
            wins=wins,
            winrate=rate,
            winrate_lb=wilson_lower_bound(wins, picks) if picks >= min_picks else 0.0,
            pick_rate=pick_rate,
            relative_pick_frequency=(pick_rate / average_pick_rate if average_pick_rate else 0.0),
            delta_vs_baseline=rate - baseline if picks else 0.0,
            trend=float(details.get("trend") or 0.0),
            primary_attr=details.get("primary_attr", ""),
            roles=tuple(details.get("roles") or ()),
        )
    return meta


def build_meta_from_stratz(
    rows: list[dict[str, Any]],
    hero_info: dict[int, dict[str, Any]],
    min_picks: int = 200,
) -> dict[int, HeroMeta]:
    """Build the same `HeroMeta` table from Stratz `heroStats.winWeek` rows.

    Stratz returns only `heroId`/`matchCount`/`winCount`, so names, attributes and
    role tags still come from OpenDota's free `/heroes` endpoint - it needs no
    token and never changes between patches.

    `trend` stays 0.0: the OpenDota trend arrays are a global public signal that
    does not describe a Stratz bracket, and inventing one here would be worse
    than having none.
    """
    counts = {
        int(row["heroId"]): (int(row.get("matchCount") or 0), int(row.get("winCount") or 0))
        for row in rows
        if row.get("heroId") is not None
    }
    return assemble_meta(counts, hero_info, min_picks)


def hero_info_from_opendota(heroes: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    """Names, attributes and role tags, keyed by hero id."""
    return {
        int(hero["id"]): {
            "name": hero.get("localized_name", f"hero {hero['id']}"),
            "primary_attr": hero.get("primary_attr", ""),
            "roles": tuple(hero.get("roles") or ()),
        }
        for hero in heroes
    }


def baseline_winrate(meta: dict[int, HeroMeta]) -> float:
    """Average winrate across the bracket - should sit very close to 0.5."""
    total_picks = sum(entry.picks for entry in meta.values())
    total_wins = sum(entry.wins for entry in meta.values())
    return winrate(total_wins, total_picks) or 0.5


def top_meta_heroes(
    meta: dict[int, HeroMeta],
    limit: int = 20,
    role: str | None = None,
    min_picks: int = 200,
) -> list[HeroMeta]:
    """Strongest heroes in the bracket.

    Thin-sample heroes are excluded outright rather than merely sorted last:
    with a large limit, a zeroed lower bound still let a 30-pick hero appear in
    a table headed "strongest heroes".
    """
    entries = [entry for entry in meta.values() if entry.picks >= min_picks]
    if role:
        wanted = role.lower()
        entries = [e for e in entries if wanted in {r.lower() for r in e.roles}]
    return sorted(entries, key=lambda entry: entry.winrate_lb, reverse=True)[:limit]
