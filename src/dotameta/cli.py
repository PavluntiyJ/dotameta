"""Command line entry point.

Subcommands:
    recommend  which heroes this account should spam, and what it is worth in MMR
    meta       the bracket's strongest heroes, independent of any player
    player     a short profile summary (rank, pace, hero pool)
    cache      inspect or clear the on-disk OpenDota cache
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict
from pathlib import Path

from rich.console import Console
from rich.table import Table

from .config import Settings
from .constants import MEDALS, MMR_PER_WIN
from .meta import build_meta, resolve_bracket, top_meta_heroes
from .opendota import OpenDotaClient, OpenDotaError
from .player import load_profile
from .recommend import recommend, spam_plan

STEAM64_BASE = 76561197960265728

CATEGORY_STYLE = {
    "spam": "bold green",
    "keep": "green",
    "learn": "yellow",
    "drop": "red",
}


def parse_account_id(value: str) -> int:
    """Turn whatever the user pasted into an OpenDota account id.

    Nobody looks up their 32-bit account id; they paste a profile link. All of
    these resolve to the same player:

        123456789
        76561198083722517                              (Steam64)
        https://www.opendota.com/players/123456789
        https://www.dotabuff.com/players/123456789/matches
        https://stratz.com/players/123456789
        https://steamcommunity.com/profiles/76561198083722517/

    The longest run of digits in the string is the id - trailing path segments,
    query strings and the "2" in "dota2" are all shorter than a real account id.
    """
    candidates = re.findall(r"\d+", value.strip())
    if not candidates:
        raise argparse.ArgumentTypeError(
            f"no account id found in {value!r} - paste a profile link or a numeric id"
        )
    number = int(max(candidates, key=len))
    return number - STEAM64_BASE if number > STEAM64_BASE else number


def build_client(args: argparse.Namespace, settings: Settings) -> OpenDotaClient:
    return OpenDotaClient(
        api_key=settings.api_key,
        cache_dir=Path(args.cache_dir),
        cache_ttl=args.cache_ttl,
        use_cache=not args.no_cache,
    )


def resolve_account(args: argparse.Namespace, settings: Settings) -> int:
    account_id = args.account_id or settings.account_id
    if not account_id:
        raise SystemExit("No account id. Pass --account-id, or set DOTAMETA_ACCOUNT_ID in .env")
    return account_id


# -- commands --------------------------------------------------------------
def cmd_recommend(args: argparse.Namespace, settings: Settings, console: Console) -> int:
    client = build_client(args, settings)
    account_id = resolve_account(args, settings)

    profile = load_profile(client, account_id, recent_days=args.days)
    hero_stats = client.hero_stats()

    medal = args.bracket or profile.medal
    bracket = resolve_bracket(hero_stats, medal)
    meta = build_meta(hero_stats, bracket)

    recommendations = recommend(
        profile,
        meta,
        min_bracket_picks=args.min_picks,
        role=args.role,
        include_unplayed=not args.played_only,
    )
    plan = spam_plan(recommendations, profile, pool_size=args.pool)
    bracket_label = MEDALS.get(bracket or 0, "all public matches")

    if args.json:
        payload = {
            "account_id": account_id,
            "rank": profile.rank_label,
            "bracket": bracket_label,
            "games_per_week": round(profile.games_per_week, 2),
            "recommendations": [asdict(rec) for rec in recommendations[: args.top]],
            "plan": {
                "pool": [rec.name for rec in plan["pool"]],
                "expected_winrate": plan.get("expected_winrate"),
                "mmr_per_week": plan.get("mmr_per_week"),
            },
        }
        console.print_json(json.dumps(payload, default=str))
        return 0

    console.print(
        f"\n[bold]{profile.name}[/bold] - {profile.rank_label} "
        f"| meta bracket: {bracket_label} "
        f"| {profile.games} games @ {profile.winrate:.1%} "
        f"in the last {args.days or 'all'} days"
    )
    if not profile.has_match_data:
        console.print(
            "[yellow]No per-hero data from OpenDota.[/yellow] Enable "
            "'Expose Public Match Data' in the Dota 2 settings, then play a match. "
            "Showing pure meta recommendations for now."
        )

    table = Table(title=f"Spam candidates ({bracket_label})", header_style="bold")
    table.add_column("#", justify="right", width=3)
    table.add_column("Hero")
    table.add_column("Your record", justify="right")
    table.add_column("Bracket WR", justify="right")
    table.add_column("Trend", justify="right")
    table.add_column("Expected", justify="right")
    table.add_column("MMR/100", justify="right")
    table.add_column("Verdict")

    for index, rec in enumerate(recommendations[: args.top], start=1):
        record = f"{rec.wins}/{rec.games}" if rec.games else "-"
        personal = f" ({rec.personal_winrate:.0%})" if rec.games else ""
        mmr = rec.mmr_per_100_games
        colour = "green" if mmr >= 0 else "red"
        table.add_row(
            str(index),
            rec.name,
            record + personal,
            f"{rec.meta_winrate:.1%}",
            f"{rec.trend:+.1%}" if rec.trend else "-",
            f"{rec.expected_winrate:.1%}",
            f"[{colour}]{mmr:+.0f}[/]",
            f"[{CATEGORY_STYLE.get(rec.category, '')}]{rec.category}[/]",
        )
    console.print(table)

    if plan["pool"]:
        names = ", ".join(rec.name for rec in plan["pool"])
        console.print(
            f"\n[bold]Suggested pool:[/bold] {names}\n"
            f"  expected winrate {plan['expected_winrate']:.1%} "
            f"-> {plan['mmr_per_100_games']:+.0f} MMR per 100 games"
        )
        if plan["games_per_week"] >= 1:
            console.print(
                f"  at your pace of {plan['games_per_week']:.1f} games/week: "
                f"{plan['mmr_per_week']:+.0f} MMR/week"
            )
        console.print(f"  [dim](assumes {MMR_PER_WIN} MMR per win)[/dim]")

    if args.why:
        console.print("\n[bold]Why:[/bold]")
        for rec in plan["pool"]:
            console.print(f"  [bold]{rec.name}[/bold]")
            for reason in rec.reasons:
                console.print(f"    - {reason}")

    console.print(f"\n[dim]{client.calls_made} OpenDota calls this run.[/dim]")
    return 0


def cmd_meta(args: argparse.Namespace, settings: Settings, console: Console) -> int:
    client = build_client(args, settings)
    hero_stats = client.hero_stats()
    bracket = resolve_bracket(hero_stats, args.bracket)
    meta = build_meta(hero_stats, bracket)
    entries = top_meta_heroes(meta, args.top, args.role)

    if args.json:
        console.print_json(json.dumps([asdict(entry) for entry in entries]))
        return 0

    label = MEDALS.get(bracket or 0, "all public matches")
    table = Table(title=f"Strongest heroes - {label}", header_style="bold")
    table.add_column("#", justify="right", width=3)
    table.add_column("Hero")
    table.add_column("Winrate", justify="right")
    table.add_column("Picks", justify="right")
    table.add_column("Contest", justify="right")
    table.add_column("Trend", justify="right")
    table.add_column("Roles")

    for index, entry in enumerate(entries, start=1):
        table.add_row(
            str(index),
            entry.name,
            f"{entry.winrate:.1%}",
            f"{entry.picks:,}",
            f"{entry.contest_rate:.1f}x",
            f"{entry.trend:+.1%}" if entry.trend else "-",
            ", ".join(entry.roles[:3]),
        )
    console.print(table)
    return 0


def cmd_player(args: argparse.Namespace, settings: Settings, console: Console) -> int:
    client = build_client(args, settings)
    profile = load_profile(client, resolve_account(args, settings), recent_days=args.days)

    console.print(
        f"\n[bold]{profile.name}[/bold] ({profile.account_id})\n"
        f"  rank         {profile.rank_label}\n"
        f"  window       last {args.days or 'all'} days\n"
        f"  record       {profile.wins}/{profile.games} ({profile.winrate:.1%})\n"
        f"  last 30 days {profile.recent_wins}/{profile.recent_games}"
        f" ({profile.recent_winrate:.1%})\n"
        f"  pace         {profile.games_per_week:.1f} games/week\n"
        f"  hero pool    {profile.hero_pool_size} heroes with 10+ games"
    )
    return 0


def cmd_cache(args: argparse.Namespace, settings: Settings, console: Console) -> int:
    client = build_client(args, settings)
    if args.clear:
        console.print(f"Removed {client.cache.clear()} cached responses.")
    else:
        directory = client.cache.directory
        count = len(list(directory.glob("*.json"))) if directory.exists() else 0
        console.print(f"{count} cached responses in {directory}")
    return 0


# -- argument parsing ------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dotameta",
        description="Hero spam and MMR-climb recommendations from OpenDota data.",
    )
    parser.add_argument("--cache-dir", default=".cache/opendota")
    parser.add_argument("--cache-ttl", type=int, default=6 * 3600)
    parser.add_argument("--no-cache", action="store_true", help="always hit the API")
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_account(sub: argparse.ArgumentParser) -> None:
        sub.add_argument(
            "--account-id",
            type=parse_account_id,
            help="profile link (OpenDota/Dotabuff/Stratz/Steam) or a numeric id",
        )
        sub.add_argument(
            "--days",
            type=int,
            default=90,
            help="only consider matches from the last N days (0 = all history)",
        )

    rec = subparsers.add_parser("recommend", help="what to spam")
    add_account(rec)
    rec.add_argument("--bracket", type=int, choices=range(1, 9), help="override rank medal")
    rec.add_argument("--role", help="filter by role, e.g. Carry / Support / Nuker")
    rec.add_argument("--top", type=int, default=15)
    rec.add_argument("--pool", type=int, default=3, help="heroes in the suggested pool")
    rec.add_argument("--min-picks", type=int, default=1000, help="bracket sample floor")
    rec.add_argument("--played-only", action="store_true", help="skip unplayed heroes")
    rec.add_argument("--why", action="store_true", help="explain the suggested pool")
    rec.add_argument("--json", action="store_true")
    rec.set_defaults(func=cmd_recommend)

    meta = subparsers.add_parser("meta", help="strongest heroes in a bracket")
    meta.add_argument("--bracket", type=int, choices=range(1, 9), default=None)
    meta.add_argument("--role")
    meta.add_argument("--top", type=int, default=20)
    meta.add_argument("--json", action="store_true")
    meta.set_defaults(func=cmd_meta)

    player = subparsers.add_parser("player", help="profile summary")
    add_account(player)
    player.set_defaults(func=cmd_player)

    cache = subparsers.add_parser("cache", help="inspect or clear the response cache")
    cache.add_argument("--clear", action="store_true")
    cache.set_defaults(func=cmd_cache)
    return parser


def _force_utf8_output() -> None:
    """Hero names contain non-ASCII glyphs and rich truncates with U+2026.

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

    console = Console()
    settings = Settings.from_env()
    try:
        return args.func(args, settings, console)
    except OpenDotaError as error:
        console.print(f"[red]OpenDota request failed:[/red] {error}")
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
