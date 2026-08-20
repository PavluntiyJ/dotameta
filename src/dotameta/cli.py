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
    cache      inspect or clear the on-disk OpenDota cache
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.markup import escape
from rich.table import Table

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
from .player import DataStatus, PlayerHero, PlayerProfile, load_profile
from .recommend import Recommendation, SpamPlan, recommend, spam_plan
from .stratz import StratzClient, StratzError

SCHEMA_VERSION = 1

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


class CliError(Exception):
    """A user-facing problem: printed as one line, never as a traceback."""


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


def parse_account_id(value: str) -> int:
    """Turn an id or a known profile URL into an OpenDota account id.

    Accepted:

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
        api_key=getattr(args, "api_key", None) or settings.api_key,
        cache_dir=Path(args.cache_dir),
        cache_ttl=args.cache_ttl,
        use_cache=not args.no_cache,
    )


def resolve_account(args: argparse.Namespace, settings: Settings) -> int:
    account_id = args.account_id or settings.account_id
    if not account_id:
        # A malformed env value must not read as "nothing configured".
        if settings.account_id_error:
            raise CliError(settings.account_id_error)
        raise CliError("No account id. Pass --account-id, or set DOTAMETA_ACCOUNT_ID in .env")
    return account_id


def read_paste(args: argparse.Namespace, err: Console) -> str:
    """Hero list from a file, or from stdin."""
    if args.heroes_file:
        path = Path(args.heroes_file)
        try:
            return path.read_text(encoding="utf-8")
        except FileNotFoundError:
            raise CliError(f"no such file: {path}") from None
        except UnicodeDecodeError:
            raise CliError(f"{path} is not UTF-8 text") from None
        except OSError as error:
            raise CliError(f"could not read {path}: {error.strerror or error}") from None
    if sys.stdin.isatty():
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
            "no heroes recognised in the pasted list - expected lines like 'Pudge 1043 53%'"
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


@dataclass
class MetaResult:
    """The bracket table plus an honest record of where it came from."""

    meta: dict
    source: str
    requested_bracket: int | None
    resolved_bracket: int | None
    position: int | None = None


def load_meta(
    args: argparse.Namespace,
    settings: Settings,
    client: OpenDotaClient,
    requested_bracket: int | None,
    warnings: list[str],
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
        if not settings.has_stratz:
            raise CliError(
                "Stratz needs a token: sign in with Steam at https://stratz.com/api "
                "(free, no payment method) and set STRATZ_API_TOKEN in .env"
            )
        medal = requested_bracket or 8
        stratz = StratzClient(
            token=settings.stratz_token or "",
            cache_dir=Path(args.cache_dir).parent / "stratz",
            cache_ttl=args.cache_ttl,
            use_cache=not args.no_cache,
        )
        try:
            rows = stratz.hero_win_rates(medal=medal, position=position)
        except StratzError as error:
            raise CliError(str(error)) from error
        if not rows:
            raise CliError(f"Stratz returned no hero rows for medal {medal}")
        meta = build_meta_from_stratz(rows, hero_info_from_opendota(client.heroes()))
        return MetaResult(meta, "stratz", requested_bracket, medal, position)

    if position is not None:
        warnings.append("--position needs Stratz; OpenDota publishes lanes, not positions 1-5")
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
        raise CliError("use either --paste or --heroes-file, not both")
    wants_paste = bool(args.paste or args.heroes_file)
    if wants_paste and not args.bracket:
        raise CliError(
            "a pasted hero list carries no rank, so --bracket is required (1 Herald .. 8 Immortal)"
        )
    account_id = None if wants_paste else resolve_account(args, settings)

    warnings: list[str] = []
    client = build_client(args, settings)

    parsed: ParseResult | None = None
    window_days: int | None
    if wants_paste:
        profile, parsed = profile_from_paste(read_paste(args, err), client.heroes(), args.bracket)
        window_days = None
        if parsed.unmatched:
            warnings.append(f"{len(parsed.unmatched)} pasted line(s) not recognised")
    else:
        assert account_id is not None
        profile = load_profile(client, account_id, recent_days=args.days)
        window_days = args.days

    requested_bracket = args.bracket or profile.medal
    result = load_meta(args, settings, client, requested_bracket, warnings)
    bracket, meta = result.resolved_bracket, result.meta

    if profile.data_status is DataStatus.PRIVATE_OR_UNAVAILABLE:
        warnings.append("no hero data: profile is private or has no public matches")
    elif profile.data_status is DataStatus.EMPTY_WINDOW:
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
                "account_id": account_id,
                "mode": "personal" if profile.has_match_data else "meta_only",
                "data_status": str(profile.data_status),
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

    _render_human(args, out, err, profile, parsed, bracket, recommendations, plan, warnings)
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
) -> None:
    bracket_label = MEDALS.get(bracket or 0, "all public matches")
    window = "no window (pasted list)" if parsed else f"last {args.days or 'all'} days"
    out.print(
        f"\n[bold]{escape(profile.name)}[/bold] - {escape(profile.rank_label)} "
        f"| meta bracket: {bracket_label} "
        f"| {profile.games} ranked games @ {profile.winrate:.1%}, {window}"
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
        err.print(
            "[yellow]Enable 'Expose Public Match Data' in the Dota 2 settings[/yellow] "
            "if this is your account; showing meta-only advice."
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
        edge = (
            f"[{'green' if rec.edge_vs_meta > 0 else 'red'}]{rec.edge_vs_meta:+.1%}[/]"
            if rec.games
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
    client = build_client(args, settings)
    profile = load_profile(client, resolve_account(args, settings), recent_days=args.days)
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
                "name": profile.name,
                "rank": profile.rank_label,
                "data_status": str(profile.data_status),
                "window_days": args.days,
                "games": profile.games,
                "wins": profile.wins,
                "games_per_week": profile.games_per_week,
                "pace_note": profile.pace_note or None,
                "hero_pool_size": profile.hero_pool_size,
            }
        )
        return 0

    window = f"last {args.days} days" if args.days else "all history"
    out.print(
        f"\n[bold]{escape(profile.name)}[/bold] ({profile.account_id})\n"
        f"  rank         {escape(profile.rank_label)}\n"
        f"  window       {window}\n"
        f"  record       {profile.wins}/{profile.games} ({profile.winrate:.1%}) ranked\n"
        f"  last 30 days {profile.recent_wins}/{profile.recent_games}"
        f" ({profile.recent_winrate:.1%})\n"
        f"  pace         {pace}\n"
        f"  hero pool    {profile.hero_pool_size} heroes with 10+ games"
    )
    return 0


def cmd_cache(args: argparse.Namespace, settings: Settings, out: Console, err: Console) -> int:
    client = build_client(args, settings)
    if args.clear:
        message = f"Removed {client.cache.clear()} cached responses."
    else:
        message = f"{client.cache.entries()} cached responses in {client.cache.directory}"
    if args.json:
        emit_json({"schema_version": SCHEMA_VERSION, "message": message})
    else:
        out.print(message)
    return 0


# -- argument parsing ------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dotameta",
        description="Hero spam and MMR-climb recommendations from OpenDota data.",
    )
    parser.add_argument("--cache-dir", default=".cache/opendota")
    parser.add_argument("--cache-ttl", type=nonnegative_int, default=6 * 3600)
    parser.add_argument("--no-cache", action="store_true", help="always hit the API")
    parser.add_argument(
        "--api-key",
        help="OpenDota API key; overrides OPENDOTA_API_KEY. Only raises rate limits.",
    )
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

    def add_source(sub: argparse.ArgumentParser) -> None:
        sub.add_argument(
            "--source",
            choices=("auto", "opendota", "stratz"),
            default="auto",
            help=(
                "meta source. auto (default) uses Stratz only where OpenDota "
                "cannot answer - Immortal, or --position - and OpenDota otherwise"
            ),
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
    add_source(rec)
    rec.set_defaults(func=cmd_recommend)

    meta = subparsers.add_parser("meta", help="strongest heroes in a bracket")
    meta.add_argument("--bracket", type=int, choices=range(1, 9), default=None)
    meta.add_argument("--role")
    meta.add_argument("--position", type=int, choices=range(1, 6))
    meta.add_argument("--top", type=positive_int, default=20)
    meta.add_argument("--json", action="store_true")
    add_source(meta)
    meta.set_defaults(func=cmd_meta)

    player = subparsers.add_parser("player", help="profile summary")
    add_account(player)
    player.set_defaults(func=cmd_player)

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
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "days", None) == 0:
        args.days = None

    out = Console()
    err = Console(stderr=True)
    settings = Settings.from_env()
    try:
        return args.func(args, settings, out, err)
    except CliError as error:
        err.print(f"[red]error:[/red] {escape(str(error))}")
        return 2
    except OpenDotaError as error:
        err.print(f"[red]OpenDota request failed:[/red] {escape(str(error))}")
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
