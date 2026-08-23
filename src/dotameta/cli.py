"""Command line entry point.

Two output contracts live here and must not bleed into each other:

  * **Human mode** renders Rich tables to stdout and diagnostics to stderr.
  * **`--json`** writes one JSON document to stdout with `json.dump` and nothing
    else - no Rich, no prompts, no progress. Everything a human would want to
    read goes to stderr, so `dotameta ... --json | jq` always works.

Argument validation happens before any network call, so a typo costs nothing.

Subcommands:
    recommend  which heroes this account should spam, and what it is worth
    meta       the bracket's strongest heroes, independent of any player
    player     a short profile summary (rank, pace, hero pool)
    cache      inspect or clear the on-disk API caches
    ui         serve the local browser interface, which runs those same commands
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.markup import escape
from rich.table import Table

from .cache import Cache
from .config import Settings
from .constants import MEDALS, MMR_PER_WIN
from .meta import (
    build_meta,
    build_meta_from_stratz,
    hero_info_from_opendota,
    resolve_bracket,
    top_meta_heroes,
)
from .opendota import OpenDotaClient, OpenDotaError
from .paste import ParseResult, parse_hero_list
from .player import (
    PACE_WINDOW_DAYS,
    DataStatus,
    PlayerHero,
    PlayerProfile,
    load_profile,
    load_stratz_profile,
)
from .recommend import PERSONAL_PRIOR_STRENGTH, Recommendation, SpamPlan, recommend, spam_plan
from .stratz import StratzClient, StratzError

SCHEMA_VERSION = 3

STEAM64_BASE = 76561197960265728
# Steam32 ids are positive and below 2^32; anything larger is a Steam64 or junk.
MAX_ACCOUNT_ID = 2**32
MIN_STEAM64 = STEAM64_BASE

# Hosts whose profile URLs we understand. Anything else must be a bare id, so a
# random long number elsewhere in a pasted URL cannot become the account id.
PROFILE_HOSTS = {
    "opendota.com": "players",
    "www.opendota.com": "players",
    "dotabuff.com": "players",
    "www.dotabuff.com": "players",
    "stratz.com": "players",
    "www.stratz.com": "players",
    "steamcommunity.com": "profiles",
    "www.steamcommunity.com": "profiles",
}

CATEGORY_STYLE = {
    "spam": "bold green",
    "keep": "green",
    "risky": "yellow",
    "learn": "cyan",
    "drop": "red",
}

ERROR_CODES = frozenset(
    {
        "account_id_missing",
        "account_id_invalid",
        "bracket_required",
        "interrupted",
        "internal_error",
        "invalid_argument",
        "invalid_request",
        "opendota_unavailable",
        "paste_conflict",
        "paste_invalid",
        "position_requires_stratz",
        "stratz_token_required",
        "stratz_unavailable",
        "ui_unavailable",
    }
)


class CliError(Exception):
    """A user-facing problem: printed as one line, never as a traceback."""

    def __init__(self, message: str, code: str | None = None, field: str | None = None):
        if code is not None and code not in ERROR_CODES:
            raise ValueError(f"unknown CLI error code {code!r}")
        super().__init__(message)
        self.code = code
        self.field = field


# -- argument types --------------------------------------------------------
def _int_or_fail(value: str) -> int:
    try:
        return int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a whole number, got {value!r}") from None


def positive_int(value: str) -> int:
    number = _int_or_fail(value)
    if number < 1:
        raise argparse.ArgumentTypeError(f"must be 1 or more, got {number}")
    return number


def nonnegative_int(value: str) -> int:
    number = _int_or_fail(value)
    if number < 0:
        raise argparse.ArgumentTypeError(f"must be 0 or more, got {number}")
    return number


def port_number(value: str) -> int:
    number = _int_or_fail(value)
    if not 1 <= number <= 65535:
        raise argparse.ArgumentTypeError(f"must be a port between 1 and 65535, got {number}")
    return number


def parse_account_id(value: str) -> int:
    """Turn an id or a known profile URL into a Dota account id.

    Accepted forms, using a synthetic account fixture:

        123456789
        76561198083722517                              (Steam64)
        https://www.opendota.com/players/123456789
        https://www.dotabuff.com/players/123456789/matches
        https://stratz.com/players/123456789
        https://steamcommunity.com/profiles/76561198083722517/

    Deliberately *not* "the longest run of digits in the string": that happily
    read a 16-digit match id out of a URL query and used it as an account id.
    Only the segment after a known host's profile prefix counts.
    """
    text = value.strip()
    if not text:
        raise argparse.ArgumentTypeError("empty account id")

    if text.isdigit():
        return _as_account_id(int(text), value)

    match = re.match(r"^(?:https?://)?([^/]+)/(.*)$", text, flags=re.IGNORECASE)
    if not match:
        raise argparse.ArgumentTypeError(
            f"not an account id or a recognised profile URL: {value!r}"
        )
    host, rest = match.group(1).lower(), match.group(2)
    prefix = PROFILE_HOSTS.get(host)
    if prefix is None:
        raise argparse.ArgumentTypeError(
            f"unrecognised profile host {host!r}; pass a numeric account id instead"
        )
    segments = [segment for segment in rest.split("?")[0].split("/") if segment]
    if len(segments) < 2 or segments[0].lower() != prefix or not segments[1].isdigit():
        raise argparse.ArgumentTypeError(
            f"could not find an id in {value!r}; expected {host}/{prefix}/<id>"
        )
    return _as_account_id(int(segments[1]), value)


def _as_account_id(number: int, original: str) -> int:
    if number >= MIN_STEAM64:
        number -= STEAM64_BASE
    if not 0 < number < MAX_ACCOUNT_ID:
        raise argparse.ArgumentTypeError(f"account id out of range in {original!r}")
    return number


# -- wiring ----------------------------------------------------------------
def build_client(args: argparse.Namespace, settings: Settings) -> OpenDotaClient:
    return OpenDotaClient(
        api_key=settings.api_key,
        cache_dir=Path(args.cache_dir),
        cache_ttl=args.cache_ttl,
        use_cache=not args.no_cache,
    )


def build_stratz_client(args: argparse.Namespace, settings: Settings) -> StratzClient:
    if not settings.has_stratz:
        raise CliError(
            "Stratz needs a token: sign in with Steam at https://stratz.com/api "
            "(free, no payment method) and set STRATZ_API_TOKEN in .env",
            "stratz_token_required",
            "source",
        )
    return StratzClient(
        token=settings.stratz_token or "",
        cache_dir=Path(args.cache_dir).parent / "stratz",
        cache_ttl=args.cache_ttl,
        use_cache=not args.no_cache,
    )


def resolve_account(args: argparse.Namespace, settings: Settings) -> int:
    account_id = args.account_id or settings.account_id
    if not account_id:
        # A malformed env value must not read as "nothing configured".
        if settings.account_id_error:
            raise CliError(settings.account_id_error, "account_id_invalid", "account-id")
        raise CliError(
            "No account id. Pass --account-id, or set DOTAMETA_ACCOUNT_ID in .env",
            "account_id_missing",
            "account-id",
        )
    return account_id


def read_paste(args: argparse.Namespace, err: Console) -> str:
    """Hero list from a file, or from stdin."""
    if args.heroes_file:
        path = Path(args.heroes_file)
        try:
            return path.read_text(encoding="utf-8")
        except FileNotFoundError:
            raise CliError(f"no such file: {path}", "paste_invalid", "heroes-file") from None
        except UnicodeDecodeError:
            raise CliError(f"{path} is not UTF-8 text", "paste_invalid", "heroes-file") from None
        except OSError as error:
            raise CliError(
                f"could not read {path}: {error.strerror or error}",
                "paste_invalid",
                "heroes-file",
            ) from None
    if sys.stdin.isatty() and not args.json:
        # stderr, never stdout: a prompt on stdout corrupts --json output.
        err.print("Paste your hero list, then press Ctrl+Z (Windows) or Ctrl+D:")
    return sys.stdin.read()


def profile_from_paste(
    text: str, heroes: list[dict[str, Any]], bracket: int
) -> tuple[PlayerProfile, ParseResult]:
    """Build a PlayerProfile out of a pasted hero table.

    There is no account behind it, so rank cannot be detected and no time window
    is known - `games_per_week` stays None and no per-week figure is printed.
    """
    parsed = parse_hero_list(text, heroes)
    if not parsed.heroes:
        raise CliError(
            "no heroes recognised in the pasted list - expected lines like 'Pudge 1043 53%'",
            "paste_invalid",
        )
    profile = PlayerProfile(
        account_id=None,
        name="pasted hero list",
        rank_tier=bracket * 10,
        games=sum(hero.games for hero in parsed.heroes),
        wins=sum(hero.wins for hero in parsed.heroes),
        heroes={
            hero.hero_id: PlayerHero(hero_id=hero.hero_id, games=hero.games, wins=hero.wins)
            for hero in parsed.heroes
        },
        games_per_week=None,
        pace_note="a pasted list carries no dates, so no pace can be measured",
    )
    return profile, parsed


# -- serialisation ---------------------------------------------------------
def recommendation_json(rec: Recommendation) -> dict[str, Any]:
    return {
        "hero_id": rec.hero_id,
        "name": rec.name,
        "games": rec.games,
        "wins": rec.wins,
        "personal_winrate": rec.personal_winrate,
        "meta_winrate": rec.meta_winrate,
        "edge_vs_meta": rec.edge_vs_meta,
        "expected_winrate": rec.expected_winrate,
        "adjusted_winrate": rec.adjusted_winrate,
        "mmr_per_100_optimistic": rec.mmr_per_100_games,
        "mmr_per_100_conservative": rec.mmr_per_100_games_conservative,
        "relative_pick_frequency": rec.relative_pick_frequency,
        "global_trend": rec.trend,
        "category": rec.category,
        "mastery": rec.mastery,
        "lane": rec.lane or None,
        "evidence_backed": rec.is_evidence_backed,
        "roles": list(rec.roles),
        "reasons": list(rec.reasons),
    }


def personal_json(profile: PlayerProfile, source: str) -> dict[str, Any]:
    """Expose only personal totals that the selected source actually supplied.

    Stratz uses zero-valued fields internally when an aggregate is anonymous or
    incomplete, but those are absence sentinels rather than observed results.
    OpenDota's win/loss endpoint remains usable independently of private hero
    rows, so its totals stay visible while the unavailable hero count does not.
    """
    unavailable = profile.data_status is DataStatus.PRIVATE_OR_UNAVAILABLE
    stratz_unavailable = source == "stratz" and unavailable
    games = None if stratz_unavailable else profile.games
    wins = None if stratz_unavailable else profile.wins
    heroes_played = (
        None if unavailable else sum(1 for hero in profile.heroes.values() if hero.games > 0)
    )
    return {
        "games": games,
        "wins": wins,
        "winrate": profile.winrate if games else None,
        "heroes_played": heroes_played,
        "data_status": str(profile.data_status),
    }


def plan_json(plan: SpamPlan) -> dict[str, Any]:
    return {
        # Full records, not names: the pool must be reproducible from the JSON
        # even when --top cut those heroes out of `recommendations`.
        "pool": [recommendation_json(rec) for rec in plan.pool],
        "expected_winrate": plan.expected_winrate if plan else None,
        "adjusted_winrate": plan.adjusted_winrate if plan else None,
        "mmr_per_100_conservative": plan.mmr_per_100_low,
        "mmr_per_100_optimistic": plan.mmr_per_100_high,
        "mmr_per_week_conservative": plan.mmr_per_week_low,
        "mmr_per_week_optimistic": plan.mmr_per_week_high,
        "games_per_week": plan.games_per_week,
        "pace_note": plan.pace_note or None,
        "mmr_per_win_assumed": MMR_PER_WIN,
    }


def bracket_json(requested: int | None, resolved: int | None) -> dict[str, Any]:
    return {
        "requested": {"id": requested, "label": MEDALS.get(requested or 0)},
        "resolved": {
            "id": resolved,
            "label": MEDALS.get(resolved or 0, "all public matches"),
        },
        "fallback_applied": requested != resolved,
    }


def emit_json(payload: dict[str, Any]) -> None:
    json.dump(payload, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")


def emit_error_json(code: str, message: str, field: str | None = None) -> None:
    """Write the machine-readable diagnostic without weakening stdout as a signal."""
    json.dump(
        {"error": {"code": code, "field": field, "message": message}},
        sys.stderr,
        default=str,
    )
    sys.stderr.write("\n")


def argparse_error(stderr: str) -> tuple[str, str | None, str]:
    """Classify argparse's captured prose while preserving its useful message."""
    lines = stderr.strip().splitlines()
    message = lines[-1] if lines else "invalid command arguments"
    marker = "error: "
    if marker in message:
        message = message.split(marker, 1)[1]
    field = None
    argument = "argument --"
    if message.startswith(argument) and ": " in message:
        name, message = message[len(argument) :].split(": ", 1)
        field = name
    code = "account_id_invalid" if field == "account-id" else "invalid_argument"
    return code, field, message


@dataclass
class MetaResult:
    """The bracket table plus an honest record of where it came from."""

    meta: dict
    source: str
    requested_bracket: int | None
    resolved_bracket: int | None
    position: int | None = None


@dataclass
class PersonalResult:
    profile: PlayerProfile
    source: str
    window_days: int | None
    stratz_client: StratzClient | None = None


def load_personal(
    args: argparse.Namespace,
    settings: Settings,
    client: OpenDotaClient,
    account_id: int,
) -> PersonalResult:
    """Select personal hero data without changing ordinary `auto` profiles."""
    if args.source == "stratz":
        stratz = build_stratz_client(args, settings)
        try:
            profile = load_stratz_profile(stratz, account_id)
        except StratzError as error:
            raise CliError(str(error), "stratz_unavailable") from error
        return PersonalResult(profile, "stratz", None, stratz)

    profile = load_profile(client, account_id, recent_days=args.days)
    if (
        args.source == "auto"
        and profile.data_status is DataStatus.PRIVATE_OR_UNAVAILABLE
        and settings.has_stratz
    ):
        stratz = build_stratz_client(args, settings)
        try:
            fallback = load_stratz_profile(stratz, account_id)
        except StratzError as error:
            raise CliError(str(error), "stratz_unavailable") from error
        # The fallback is for hero aggregates only. Identity and rank remain the
        # OpenDota values already fetched; no other unavailable fields are mixed.
        fallback.name = profile.name
        fallback.rank_tier = profile.rank_tier
        return PersonalResult(fallback, "stratz", None, stratz)
    return PersonalResult(profile, "opendota", args.days)


def load_meta(
    args: argparse.Namespace,
    settings: Settings,
    client: OpenDotaClient,
    requested_bracket: int | None,
    warnings: list[str],
    stratz_client: StratzClient | None = None,
) -> MetaResult:
    """Pick a meta source and build the bracket table.

    `auto` uses Stratz only where OpenDota structurally cannot answer - the
    Immortal bracket, or a real position filter - and OpenDota everywhere else.
    Silently switching sources for every query would change numbers under people
    who added a token for one feature, and would spend requests for nothing.
    """
    position = getattr(args, "position", None)
    needs_stratz = requested_bracket == 8 or position is not None
    source = args.source
    if source == "auto":
        source = "stratz" if (settings.has_stratz and needs_stratz) else "opendota"

    if source == "stratz":
        if requested_bracket is None:
            raise CliError(
                "--bracket is required when Stratz supplies meta data",
                "bracket_required",
                "bracket",
            )
        medal = requested_bracket
        stratz = stratz_client or build_stratz_client(args, settings)
        try:
            rows = stratz.hero_win_rates(medal=medal, position=position)
        except StratzError as error:
            raise CliError(str(error), "stratz_unavailable") from error
        if not rows:
            raise CliError(f"Stratz returned no hero rows for medal {medal}", "stratz_unavailable")
        if not any(row.get("matchCount", 0) > 0 for row in rows):
            raise CliError(
                f"Stratz returned no usable hero data for medal {medal}",
                "stratz_unavailable",
            )
        meta = build_meta_from_stratz(rows, hero_info_from_opendota(client.heroes()))
        return MetaResult(meta, "stratz", requested_bracket, medal, position)

    if position is not None:
        raise CliError(
            "--position requires Stratz; OpenDota does not publish positions 1-5",
            "position_requires_stratz",
            "position",
        )
    hero_stats = client.hero_stats()
    resolved = resolve_bracket(hero_stats, requested_bracket)
    if requested_bracket != resolved:
        warnings.append(
            f"no OpenDota data for {MEDALS.get(requested_bracket or 0, 'that bracket')}; "
            f"using {MEDALS.get(resolved or 0, 'all public matches')}"
            + (
                " - set STRATZ_API_TOKEN for real Immortal numbers"
                if requested_bracket == 8
                else ""
            )
        )
    return MetaResult(build_meta(hero_stats, resolved), "opendota", requested_bracket, resolved)


# -- commands --------------------------------------------------------------
def cmd_recommend(args: argparse.Namespace, settings: Settings, out: Console, err: Console) -> int:
    # Everything local is validated before a single request goes out.
    if args.paste and args.heroes_file:
        raise CliError("use either --paste or --heroes-file, not both", "paste_conflict")
    wants_paste = bool(args.paste or args.heroes_file)
    if wants_paste and not args.bracket:
        raise CliError(
            "a pasted hero list carries no rank, so --bracket is required (1 Herald .. 8 Immortal)",
            "bracket_required",
            "bracket",
        )
    if not wants_paste and args.source == "stratz" and not args.bracket:
        raise CliError(
            "--bracket is required with Stratz personal data because it has no rank",
            "bracket_required",
            "bracket",
        )
    if args.position is not None:
        if args.source == "opendota":
            raise CliError(
                "--position requires Stratz; OpenDota does not publish positions 1-5",
                "position_requires_stratz",
                "position",
            )
        if not settings.has_stratz:
            raise CliError(
                "--position requires STRATZ_API_TOKEN",
                "position_requires_stratz",
                "position",
            )
    if args.source == "stratz" and not settings.has_stratz:
        raise CliError(
            "--source stratz requires STRATZ_API_TOKEN", "stratz_token_required", "source"
        )
    account_id = None if wants_paste else resolve_account(args, settings)

    warnings: list[str] = []
    client = build_client(args, settings)

    parsed: ParseResult | None = None
    stratz_client: StratzClient | None = None
    window_days: int | None
    if wants_paste:
        profile, parsed = profile_from_paste(read_paste(args, err), client.heroes(), args.bracket)
        player_source = "paste"
        window_days = None
        warnings.append(
            "pasted totals cannot prove game mode; projections assume the table was filtered "
            "to ranked All Pick"
        )
        if parsed.unmatched:
            warnings.append(f"{len(parsed.unmatched)} pasted line(s) not recognised")
    else:
        assert account_id is not None
        personal = load_personal(args, settings, client, account_id)
        profile = personal.profile
        player_source = personal.source
        window_days = personal.window_days
        stratz_client = personal.stratz_client
        if player_source == "stratz":
            warnings.append(
                "Stratz personal heroes are ranked-All-Pick aggregates capped at 10,000 matches; "
                "the rows contain no rank, recency, lanes, or pace"
            )

    if window_days is not None and profile.games < PERSONAL_PRIOR_STRENGTH:
        wider_window = "--days 365 or --days 0" if window_days < 365 else "--days 0"
        warnings.append(
            f"only {profile.games} ranked All Pick games were found in the last "
            f"{window_days} days; below {int(PERSONAL_PRIOR_STRENGTH)} games, the player's "
            f"record does not yet outweigh the bracket prior, so {wider_window} may produce "
            "a usable comparison"
        )

    requested_bracket = args.bracket or profile.medal
    result = load_meta(
        args, settings, client, requested_bracket, warnings, stratz_client=stratz_client
    )
    bracket, meta = result.resolved_bracket, result.meta

    if profile.data_status is DataStatus.PRIVATE_OR_UNAVAILABLE:
        if player_source == "stratz":
            warnings.append(
                "no hero data: the Stratz aggregate is anonymous, incomplete, or unavailable"
            )
        else:
            warnings.append("no hero data: profile is private or OpenDota has no public matches")
    elif profile.data_status is DataStatus.EMPTY_WINDOW:
        if player_source == "stratz":
            warnings.append("Stratz has no ranked-All-Pick aggregate rows for this account")
        else:
            warnings.append("profile is public but has no ranked games in this window")

    recommendations = recommend(
        profile,
        meta,
        min_bracket_picks=args.min_picks,
        role=args.role,
        include_unplayed=not args.played_only,
        min_games=args.min_games,
    )
    plan = spam_plan(recommendations, profile, pool_size=args.pool)
    if not plan:
        warnings.append("no hero clears the confidence bar; no spam pool recommended")

    if args.json:
        emit_json(
            {
                "schema_version": SCHEMA_VERSION,
                "source": "paste" if wants_paste else "account",
                "player_source": player_source,
                "account_id": account_id,
                "mode": "personal" if profile.has_match_data else "meta_only",
                "data_status": str(profile.data_status),
                "personal": personal_json(profile, player_source),
                "window_days": window_days,
                "rank": profile.rank_label,
                "bracket": bracket_json(requested_bracket, bracket),
                "meta_source": result.source,
                "position": result.position,
                "warnings": warnings,
                "paste": (
                    {
                        "heroes_read": len(parsed.heroes),
                        "games_read": parsed.total_games,
                        "unmatched_lines": list(parsed.unmatched),
                    }
                    if parsed is not None
                    else None
                ),
                "recommendations": [
                    recommendation_json(rec) for rec in recommendations[: args.top]
                ],
                "plan": plan_json(plan),
            }
        )
        return 0

    _render_human(
        args,
        out,
        err,
        profile,
        parsed,
        bracket,
        recommendations,
        plan,
        warnings,
        player_source,
    )
    err.print(f"[dim]{client.calls_made} OpenDota calls this run.[/dim]")
    return 0


def _render_human(
    args: argparse.Namespace,
    out: Console,
    err: Console,
    profile: PlayerProfile,
    parsed: ParseResult | None,
    bracket: int | None,
    recommendations: list[Recommendation],
    plan: SpamPlan,
    warnings: list[str],
    player_source: str,
) -> None:
    bracket_label = MEDALS.get(bracket or 0, "all public matches")
    if parsed:
        window = "no window (pasted list)"
    elif player_source == "stratz":
        window = "all available history (Stratz)"
    else:
        window = f"last {args.days or 'all'} days"
    if parsed is not None:
        record_kind = "games in pasted table"
    elif player_source == "stratz":
        record_kind = "ranked All Pick games"
    else:
        record_kind = "ranked games"
    personal = personal_json(profile, player_source)
    out.print(
        f"\n[bold]{escape(profile.name)}[/bold] - {escape(profile.rank_label)} "
        f"| meta bracket: {bracket_label} | {window}"
    )
    if personal["games"] is None:
        out.print(f"[dim]Personal sample: unavailable ({personal['data_status']}).[/dim]")
    else:
        winrate_label = f" ({personal['winrate']:.1%})" if personal["winrate"] is not None else ""
        heroes_label = (
            f", {personal['heroes_played']} heroes played"
            if personal["heroes_played"] is not None
            else ", heroes played unavailable"
        )
        out.print(
            f"[dim]Personal sample: {personal['games']} {record_kind}, "
            f"{personal['wins']} wins{winrate_label}{heroes_label}; "
            f"status {personal['data_status']}.[/dim]"
        )
    if parsed is not None:
        out.print(
            f"[dim]Read {len(parsed.heroes)} heroes / {parsed.total_games} games "
            f"from the pasted list.[/dim]"
        )
        for line in parsed.unmatched[:3]:
            err.print(f"[yellow]ignored:[/yellow] {escape(line[:60])}")
    for warning in warnings:
        err.print(f"[yellow]note:[/yellow] {escape(warning)}")
    if profile.data_status is DataStatus.PRIVATE_OR_UNAVAILABLE:
        if player_source == "opendota":
            err.print(
                "[yellow]Enable 'Expose Public Match Data' in the Dota 2 settings[/yellow] "
                "if this is your account; showing meta-only advice."
            )
        elif player_source == "stratz":
            err.print(
                "[yellow]Stratz did not provide a complete public hero aggregate;[/yellow] "
                "showing meta-only advice."
            )

    shown = recommendations[: args.top]
    show_lanes = any(rec.lane for rec in shown)

    table = Table(title=f"Spam candidates ({bracket_label})", header_style="bold")
    table.add_column("#", justify="right", width=3)
    table.add_column("Hero")
    table.add_column("Record", justify="right")
    table.add_column("Meta", justify="right")
    table.add_column("vs Meta", justify="right")
    if show_lanes:
        table.add_column("Lane")
    table.add_column("MMR/100", justify="right", no_wrap=True)
    table.add_column("Conf", justify="center")
    table.add_column("Verdict", no_wrap=True)

    for index, rec in enumerate(shown, start=1):
        record = f"{rec.wins}/{rec.games}" if rec.games else "-"
        personal = f" ({rec.personal_winrate:.0%})" if rec.games else ""
        mmr = rec.mmr_per_100_games_conservative
        colour = "green" if mmr >= 0 else "red"
        confidence = "low" if rec.games < 15 else ("ok" if rec.games < 50 else "high")
        confidence_colour = {"low": "red", "ok": "yellow", "high": "green"}[confidence]
        edge_value = rec.edge_vs_meta
        edge = (
            f"[{'green' if edge_value > 0 else 'red'}]{edge_value:+.1%}[/]"
            if edge_value is not None
            else "-"
        )
        table.add_row(
            str(index),
            escape(rec.name),
            record + personal,
            f"{rec.meta_winrate:.1%}",
            edge,
            *([rec.lane or "-"] if show_lanes else []),
            f"[{colour}]{mmr:+.0f}[/]",
            f"[{confidence_colour}]{confidence}[/]",
            f"[{CATEGORY_STYLE.get(rec.category, '')}]{rec.category}[/]",
        )
    out.print(table)

    if not plan:
        out.print(
            "\n[bold]No spam pool.[/bold] Nothing here stays positive once its sample "
            "size is priced in - play more games on your best heroes before committing."
        )
    else:
        names = ", ".join(escape(rec.name) for rec in plan.pool)
        short = "" if len(plan.pool) == args.pool else f" (only {len(plan.pool)} qualify)"
        out.print(
            f"\n[bold]Suggested pool:[/bold] {names}{short}\n"
            f"  winrate {plan.adjusted_winrate:.1%}-{plan.expected_winrate:.1%} "
            f"-> [bold]{plan.mmr_per_100_low:+.0f} to {plan.mmr_per_100_high:+.0f}[/bold]"
            f" MMR per 100 games\n"
            f"  [dim]low end is a heuristic one-standard-error haircut, not a "
            f"confidence interval; it narrows as you play more of these heroes[/dim]"
        )
        if plan.games_per_week is not None:
            out.print(
                f"  at {plan.games_per_week:.1f} ranked games/week: "
                f"{plan.mmr_per_week_low:+.0f} to {plan.mmr_per_week_high:+.0f} MMR/week"
            )
        elif plan.pace_note:
            out.print(f"  [dim]no MMR/week: {escape(plan.pace_note)}[/dim]")
        out.print(f"  [dim](assumes {MMR_PER_WIN} MMR per win)[/dim]")

    if args.why:
        out.print("\n[bold]Why:[/bold]")
        for rec in plan.pool:
            out.print(f"  [bold]{escape(rec.name)}[/bold]")
            for reason in rec.reasons:
                out.print(f"    - {escape(reason)}")


def cmd_meta(args: argparse.Namespace, settings: Settings, out: Console, err: Console) -> int:
    if args.source == "stratz" and args.bracket is None:
        raise CliError(
            "--bracket is required when Stratz supplies meta data",
            "bracket_required",
            "bracket",
        )
    if args.position is not None:
        if args.source == "opendota":
            raise CliError(
                "--position requires Stratz; OpenDota does not publish positions 1-5",
                "position_requires_stratz",
                "position",
            )
        if not settings.has_stratz:
            raise CliError(
                "--position requires STRATZ_API_TOKEN",
                "position_requires_stratz",
                "position",
            )
        if args.bracket is None:
            raise CliError("--bracket is required with --position", "bracket_required", "bracket")
    if args.source == "stratz" and not settings.has_stratz:
        raise CliError(
            "--source stratz requires STRATZ_API_TOKEN", "stratz_token_required", "source"
        )
    client = build_client(args, settings)
    warnings: list[str] = []
    result = load_meta(args, settings, client, args.bracket, warnings)
    bracket, meta = result.resolved_bracket, result.meta
    entries = top_meta_heroes(meta, args.top, args.role)

    if args.json:
        emit_json(
            {
                "schema_version": SCHEMA_VERSION,
                "bracket": bracket_json(args.bracket, bracket),
                "meta_source": result.source,
                "position": result.position,
                "warnings": warnings,
                "heroes": [
                    {
                        "hero_id": e.hero_id,
                        "name": e.name,
                        "picks": e.picks,
                        "wins": e.wins,
                        "winrate": e.winrate,
                        "winrate_lower_bound": e.winrate_lb,
                        "relative_pick_frequency": e.relative_pick_frequency,
                        "global_trend": e.trend,
                        "roles": list(e.roles),
                    }
                    for e in entries
                ],
            }
        )
        return 0

    label = MEDALS.get(bracket or 0, "all public matches")
    table = Table(title=f"Strongest heroes - {label}", header_style="bold")
    table.add_column("#", justify="right", width=3)
    table.add_column("Hero")
    table.add_column("Winrate", justify="right")
    table.add_column("Picks", justify="right")
    table.add_column("Pick freq", justify="right")
    table.add_column("Roles")

    for index, entry in enumerate(entries, start=1):
        table.add_row(
            str(index),
            escape(entry.name),
            f"{entry.winrate:.1%}",
            f"{entry.picks:,}",
            f"{entry.relative_pick_frequency:.1f}x",
            ", ".join(entry.roles[:3]),
        )
    out.print(table)
    for warning in warnings:
        err.print(f"[yellow]note:[/yellow] {escape(warning)}")
    if result.position is None:
        err.print(
            "[dim]Roles are OpenDota capability tags (Carry, Nuker, ...), not positions 1-5. "
            "Use --position with a Stratz token for real positions.[/dim]"
        )
    return 0


def cmd_player(args: argparse.Namespace, settings: Settings, out: Console, err: Console) -> int:
    if args.source == "stratz" and not settings.has_stratz:
        raise CliError(
            "--source stratz requires STRATZ_API_TOKEN", "stratz_token_required", "source"
        )
    account_id = resolve_account(args, settings)
    client = build_client(args, settings)
    personal = load_personal(args, settings, client, account_id)
    profile = personal.profile
    summary = personal_json(profile, personal.source)
    hero_pool_size = None if summary["heroes_played"] is None else profile.hero_pool_size
    pace = (
        f"{profile.games_per_week:.1f} ranked games/week"
        if profile.games_per_week is not None
        else f"unavailable ({profile.pace_note})"
    )

    if args.json:
        emit_json(
            {
                "schema_version": SCHEMA_VERSION,
                "account_id": profile.account_id,
                "player_source": personal.source,
                "name": profile.name,
                "rank": profile.rank_label,
                "data_status": str(profile.data_status),
                "window_days": personal.window_days,
                "games": summary["games"],
                "wins": summary["wins"],
                "games_per_week": profile.games_per_week,
                "pace_note": profile.pace_note or None,
                "hero_pool_size": hero_pool_size,
            }
        )
        return 0

    window = (
        "all available history (capped at 10,000 matches)"
        if personal.source == "stratz"
        else (f"last {args.days} days" if args.days else "all history")
    )
    if summary["games"] is None:
        detail = (
            "  record       unavailable\n"
            "  recency      unavailable (aggregate has no match dates)\n"
        )
    elif personal.source == "stratz":
        detail = (
            f"  record       {profile.wins}/{profile.games} ranked All Pick\n"
            "  recency      unavailable (aggregate has no match dates)\n"
        )
    else:
        recent_window = min(args.days, PACE_WINDOW_DAYS) if args.days else PACE_WINDOW_DAYS
        detail = (
            f"  record       {profile.wins}/{profile.games} ({profile.winrate:.1%}) ranked\n"
            f"  last {recent_window} days {profile.recent_wins}/{profile.recent_games}"
            f" ({profile.recent_winrate:.1%})\n"
        )
    status_detail = ""
    if profile.data_status is DataStatus.PRIVATE_OR_UNAVAILABLE:
        reason = (
            "anonymous or the hero aggregate is unavailable/incomplete in Stratz"
            if personal.source == "stratz"
            else "profile is private or OpenDota has no public hero data"
        )
        status_detail = f"\n  hero data    unavailable ({reason})"
    elif profile.data_status is DataStatus.EMPTY_WINDOW:
        reason = (
            "Stratz has no ranked-All-Pick aggregate rows for this account"
            if personal.source == "stratz"
            else "public profile, but no ranked All Pick games are in this window"
        )
        status_detail = f"\n  hero data    empty ({reason})"
    hero_pool = (
        f"{hero_pool_size} heroes with 10+ games" if hero_pool_size is not None else "unavailable"
    )
    out.print(
        f"\n[bold]{escape(profile.name)}[/bold] ({profile.account_id})\n"
        f"  rank         {escape(profile.rank_label)}\n"
        f"  window       {window}\n"
        f"{detail}"
        f"  pace         {pace}\n"
        f"  hero pool    {hero_pool}"
        f"{status_detail}"
    )
    return 0


def cmd_cache(args: argparse.Namespace, settings: Settings, out: Console, err: Console) -> int:
    opendota_dir = Path(args.cache_dir)
    caches = {
        "opendota": Cache(opendota_dir, args.cache_ttl, enabled=not args.no_cache),
        "stratz": Cache(opendota_dir.parent / "stratz", args.cache_ttl, enabled=not args.no_cache),
    }
    counts = {
        source: cache.clear() if args.clear else cache.entries() for source, cache in caches.items()
    }
    counts["total"] = sum(counts.values())
    action = "clear" if args.clear else "inspect"
    if args.json:
        emit_json(
            {
                "schema_version": SCHEMA_VERSION,
                "action": action,
                "counts": counts,
                "directories": {source: str(cache.directory) for source, cache in caches.items()},
            }
        )
    else:
        verb = "removed" if args.clear else "found"
        for source, cache in caches.items():
            out.print(f"{source}: {verb} {counts[source]} cached responses in {cache.directory}")
        out.print(f"total: {counts['total']} cached responses")
    return 0


# -- argument parsing ------------------------------------------------------
def cmd_ui(args: argparse.Namespace, settings: Settings, out: Console, err: Console) -> int:
    # Imported here, not at module scope: ui.py drives this module, and the UI's
    # dependencies should not load for people who only ever use the terminal.
    from .ui import serve

    try:
        server = serve("127.0.0.1", args.port, open_browser=not args.no_browser)
    except OSError as error:
        raise CliError(f"could not open port {args.port}: {error}", "ui_unavailable") from None
    host, port = server.server_address[0], server.server_address[1]
    out.print(f"dotameta UI on [bold]http://{host}:{port}/[/bold]")
    err.print("[dim]The page runs the same commands this terminal does. Ctrl+C to stop.[/dim]")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        err.print("[dim]stopped[/dim]")
    finally:
        server.server_close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dotameta",
        description="Hero spam and MMR-climb recommendations from OpenDota and Stratz data.",
    )
    parser.add_argument("--cache-dir", default=".cache/opendota")
    parser.add_argument("--cache-ttl", type=nonnegative_int, default=6 * 3600)
    parser.add_argument("--no-cache", action="store_true", help="always hit the API")
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_account(sub: argparse.ArgumentParser) -> None:
        sub.add_argument(
            "--account-id",
            type=parse_account_id,
            help="numeric account id, or an OpenDota/Dotabuff/Stratz/Steam profile URL",
        )
        sub.add_argument(
            "--days",
            type=nonnegative_int,
            default=90,
            help="only consider matches from the last N days (0 = all history)",
        )
        sub.add_argument("--json", action="store_true")

    def add_source(sub: argparse.ArgumentParser, help_text: str) -> None:
        sub.add_argument(
            "--source",
            choices=("auto", "opendota", "stratz"),
            default="auto",
            help=help_text,
        )

    rec = subparsers.add_parser("recommend", help="what to spam")
    add_account(rec)
    rec.add_argument("--bracket", type=int, choices=range(1, 9), help="override rank medal")
    rec.add_argument("--role", help="filter by OpenDota role tag, e.g. Carry / Support")
    rec.add_argument(
        "--position",
        type=int,
        choices=range(1, 6),
        help="real position 1-5 (requires a Stratz token)",
    )
    rec.add_argument("--top", type=positive_int, default=15)
    rec.add_argument("--pool", type=positive_int, default=3, help="heroes in the pool")
    rec.add_argument("--min-picks", type=nonnegative_int, default=1000)
    rec.add_argument("--min-games", type=nonnegative_int, default=0)
    rec.add_argument("--played-only", action="store_true", help="skip unplayed heroes")
    rec.add_argument("--heroes-file", help="read a pasted hero list from a file")
    rec.add_argument("--paste", action="store_true", help="read a hero list from stdin")
    rec.add_argument("--why", action="store_true", help="explain the suggested pool")
    add_source(
        rec,
        "personal and meta source. auto keeps ordinary profiles on OpenDota, uses Stratz "
        "personal heroes only when OpenDota has none, and Stratz meta only for Immortal "
        "or --position; explicit Stratz requires --bracket",
    )
    rec.set_defaults(func=cmd_recommend)

    meta = subparsers.add_parser("meta", help="strongest heroes in a bracket")
    meta.add_argument("--bracket", type=int, choices=range(1, 9), default=None)
    meta.add_argument("--role")
    meta.add_argument("--position", type=int, choices=range(1, 6))
    meta.add_argument("--top", type=positive_int, default=20)
    meta.add_argument("--json", action="store_true")
    add_source(
        meta,
        "meta source. auto uses Stratz only for Immortal or --position, and OpenDota otherwise",
    )
    meta.set_defaults(func=cmd_meta)

    player = subparsers.add_parser("player", help="profile summary")
    add_account(player)
    add_source(
        player,
        "personal hero source. auto keeps OpenDota unless hero data is unavailable, then "
        "uses Stratz when a token exists",
    )
    player.set_defaults(func=cmd_player)

    ui = subparsers.add_parser("ui", help="open the local browser interface")
    ui.add_argument("--port", type=port_number, default=8765)
    ui.add_argument("--no-browser", action="store_true", help="do not open a browser window")
    ui.set_defaults(func=cmd_ui)

    cache = subparsers.add_parser("cache", help="inspect or clear the response cache")
    cache.add_argument("--clear", action="store_true")
    cache.add_argument("--json", action="store_true")
    cache.set_defaults(func=cmd_cache)
    return parser


def _force_utf8_output() -> None:
    """Hero names contain non-ASCII glyphs and Rich truncates with U+2026.

    Windows consoles still default to a legacy code page, which renders both as
    replacement characters, so ask the streams for UTF-8 before anything prints.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


def main(argv: list[str] | None = None) -> int:
    _force_utf8_output()
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    json_requested = "--json" in raw_argv
    parser = build_parser()
    if json_requested:
        parser_stderr = io.StringIO()
        parse_failure: tuple[str, str | None, str, int] | None = None
        with contextlib.redirect_stderr(parser_stderr):
            try:
                args = parser.parse_args(raw_argv)
            except SystemExit as error:
                if not error.code:
                    raise
                code, field, message = argparse_error(parser_stderr.getvalue())
                parse_failure = (code, field, message, int(error.code))
        if parse_failure is not None:
            code, field, message, exit_code = parse_failure
            emit_error_json(code, message, field)
            return exit_code
    else:
        args = parser.parse_args(raw_argv)
    if getattr(args, "days", None) == 0:
        args.days = None

    out = Console()
    err = Console(stderr=True)
    try:
        settings = Settings.from_env()
        return args.func(args, settings, out, err)
    except CliError as error:
        if json_requested:
            emit_error_json(error.code or "invalid_request", str(error), error.field)
        else:
            err.print(f"[red]error:[/red] {escape(str(error))}")
        return 2
    except OpenDotaError as error:
        if json_requested:
            emit_error_json("opendota_unavailable", str(error))
        else:
            err.print(f"[red]OpenDota request failed:[/red] {escape(str(error))}")
        return 2
    except StratzError as error:
        if json_requested:
            emit_error_json("stratz_unavailable", str(error))
        else:
            err.print(f"[red]Stratz request failed:[/red] {escape(str(error))}")
        return 2
    except KeyboardInterrupt:
        if json_requested:
            emit_error_json("interrupted", "command interrupted")
        return 130
    except Exception as error:
        # A JSON consumer cannot read a traceback, so the failure is reported in
        # the contract's shape. It gets its own code: telling a caller that its
        # request was invalid, when the tool itself broke, sends them to fix the
        # wrong thing. Human mode still raises, where the traceback is useful.
        if not json_requested:
            raise
        message = str(error) or type(error).__name__
        emit_error_json("internal_error", message)
        return 2


if __name__ == "__main__":
    sys.exit(main())
