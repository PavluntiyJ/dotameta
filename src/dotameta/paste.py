"""Parse a hero list the user pasted in, instead of fetching one.

Not everyone wants to hand over an account id: profiles can be private, the
account may be a smurf, or the player just selected their hero table on Dotabuff
and hit Ctrl+C. That paste is messy - column order differs per site, numbers
carry thousands separators, and hero names come in several spellings.

Anything that survives is treated exactly like fetched data, so the recommender
downstream cannot tell the difference.

Recognised shapes (one hero per line, extra columns ignored):

    Pudge 1043 53%
    Pudge, 1043, 53.2%
    Pudge	1,043	53.24%	7:32
    Juggernaut 250 130          (games then wins, when the second number is
                                 not a percentage and is smaller than the first)
    Pudge                       (name only - counts as zero games)
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

# Heroes players still call by an old or shortened name. Keys and values are
# both normalised through `_normalise` before lookup.
ALIASES = {
    "nevermore": "shadow fiend",
    "zuus": "zeus",
    "necrolyte": "necrophos",
    "obsidian destroyer": "outworld destroyer",
    "outworld devourer": "outworld destroyer",
    "od": "outworld destroyer",
    "skeleton king": "wraith king",
    "wk": "wraith king",
    "doom bringer": "doom",
    "magnataur": "magnus",
    "windrunner": "windranger",
    "wr": "windranger",
    "furion": "nature's prophet",
    "np": "nature's prophet",
    "rattletrap": "clockwerk",
    "shredder": "timbersaw",
    "treant": "treant protector",
    "wisp": "io",
    "centaur": "centaur warrunner",
    "vengeful": "vengeful spirit",
    "vs": "vengeful spirit",
    "am": "anti-mage",
    "antimage": "anti-mage",
    "sf": "shadow fiend",
    "qop": "queen of pain",
    "tb": "terrorblade",
    "sk": "sand king",
    "es": "earthshaker",
    "cm": "crystal maiden",
    "pa": "phantom assassin",
    "pl": "phantom lancer",
    "dp": "death prophet",
    "lc": "legion commander",
    "ta": "templar assassin",
    "bh": "bounty hunter",
    "ss": "shadow shaman",
    "wd": "witch doctor",
    "dk": "dragon knight",
    "sd": "shadow demon",
    "veno": "venomancer",
    "sb": "spirit breaker",
    "ns": "night stalker",
    "om": "ogre magi",
    "void": "faceless void",
    "mag": "magnus",
}


@dataclass(frozen=True)
class ParsedHero:
    hero_id: int
    name: str
    games: int
    wins: int
    source_line: str = ""


@dataclass
class ParseResult:
    heroes: list[ParsedHero]
    unmatched: list[str]

    @property
    def total_games(self) -> int:
        return sum(hero.games for hero in self.heroes)


def _normalise(text: str) -> str:
    """Fold a hero name to something comparable across sites and spellings."""
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def build_name_index(heroes: Iterable[dict[str, Any]]) -> dict[str, int]:
    """Map every spelling we accept to a hero id."""
    index: dict[str, int] = {}
    for hero in heroes:
        hero_id = int(hero["id"])
        localised = hero.get("localized_name", "")
        index[_normalise(localised)] = hero_id
        # "npc_dota_hero_shadow_fiend" -> "shadow fiend"
        internal = str(hero.get("name", "")).replace("npc_dota_hero_", "")
        index[_normalise(internal.replace("_", " "))] = hero_id

    for alias, canonical in ALIASES.items():
        target = index.get(_normalise(canonical))
        if target is not None:
            index.setdefault(_normalise(alias), target)
    return index


def _match_hero(line: str, index: dict[str, int]) -> tuple[int, str] | None:
    """Find the hero name in a line, preferring the longest match.

    Longest-first matters: "Shadow Fiend" must not be read as "Shadow Shaman"
    via a shorter fragment, and "Anti-Mage" must beat a bare "Mage".
    """
    normalised = _normalise(line)
    if not normalised:
        return None
    if normalised in index:
        return index[normalised], normalised

    best: tuple[int, str] | None = None
    for name, hero_id in index.items():
        if not name:
            continue
        if re.search(rf"(?:^|\s){re.escape(name)}(?:\s|$)", normalised):
            if best is None or len(name) > len(best[1]):
                best = (hero_id, name)
    return best


def _strip_name(line: str, normalised_name: str) -> str:
    """Remove the matched hero name from the original line, punctuation and all.

    The name is known in normalised form ("nature s prophet"); in the raw line it
    may be written "Nature's Prophet". Each normalised word is matched literally
    and the gaps between them allowed to be any run of non-alphanumerics.
    """
    words = [re.escape(word) for word in normalised_name.split()]
    if not words:
        return line
    pattern = r"[^A-Za-z0-9]*".join(words)
    return re.sub(pattern, " ", line, count=1, flags=re.IGNORECASE)


# A number with optional thousands groups. The separator must be a comma
# followed by exactly three digits, so "1,250 700" reads as two columns rather
# than as 1250700 - a plain-space separator cannot be told apart from a column
# gap, and guessing wrong turns a hero's whole record into one number.
NUMBER_RE = re.compile(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?")


def _numbers(line: str) -> tuple[list[float], list[float]]:
    """Split a line's numbers into percentages and plain counts."""
    percentages = [float(n) for n in re.findall(r"(\d+(?:\.\d+)?)\s*%", line)]
    without_pct = re.sub(r"\d+(?:\.\d+)?\s*%", " ", line)
    # Drop clock-style values (7:32) so match duration is never read as a count.
    without_pct = re.sub(r"\d+:\d+", " ", without_pct)
    counts = [float(n.replace(",", "")) for n in NUMBER_RE.findall(without_pct)]
    return percentages, counts


def parse_hero_list(text: str, heroes: Iterable[dict[str, Any]]) -> ParseResult:
    """Read a pasted hero table into games/wins per hero.

    Lines that contain no recognisable hero name are collected in `unmatched` so
    the CLI can show the user what it ignored rather than silently dropping it.
    """
    # Materialise once: `heroes` is typed Iterable and was walked twice, which
    # silently produced an empty name table when a generator was passed.
    hero_list = list(heroes)
    index = build_name_index(hero_list)
    names = {int(h["id"]): h.get("localized_name", "") for h in hero_list}

    parsed: dict[int, ParsedHero] = {}
    unmatched: list[str] = []

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        match = _match_hero(line, index)
        if match is None:
            unmatched.append(line)
            continue

        hero_id, matched_name = match
        # Strip the hero name out of the *original* line, not the normalised one:
        # normalising turns "1,250" into "1 250", and the thousands separator has
        # to survive long enough to tell one column from two.
        remainder = _strip_name(line, matched_name)
        percentages, counts = _numbers(remainder)

        games = int(counts[0]) if counts else 0
        if percentages:
            wins = round(games * percentages[0] / 100)
        elif len(counts) >= 2 and counts[1] <= counts[0]:
            wins = int(counts[1])
        else:
            wins = 0

        existing = parsed.get(hero_id)
        if existing and existing.games >= games:
            continue
        parsed[hero_id] = ParsedHero(
            hero_id=hero_id,
            name=names.get(hero_id, matched_name),
            games=games,
            wins=min(wins, games),
            source_line=line,
        )

    return ParseResult(heroes=list(parsed.values()), unmatched=unmatched)
