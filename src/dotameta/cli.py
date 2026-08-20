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
from .paste import parse_hero_list
from .player import PlayerHero, PlayerProfile, load_profile
from .recommend import recommend, spam_plan

STEAM64_BASE = 76561197960265728

CATEGORY_STYLE = {
    "spam": "bold green",
    "keep": "green",
    "risky": "yellow",
    "learn": "cyan",
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
        api_key=getattr(args, "api_key", None) or settings.api_key,
        cache_dir=Path(args.cache_dir),
        cache_ttl=args.cache_ttl,
        use_cache=not args.no_cache,
    )


def resolve_account(args: argparse.Namespace, settings: Settings) -> int:
    account_id = args.account_id or settings.account_id
    if not account_id:
        raise SystemExit("No account id. Pass --account-id, or set DOTAMETA_ACCOUNT_ID in .env")
    return account_id


def profile_from_paste(text: str, heroes: list[dict], bracket: int | None) -> tuple:
    """Build a PlayerProfile out of a pasted hero table.

    There is no account behind it, so rank cannot be detected and must be given
    with --bracket. Everything downstream treats it as a normal profile.
    """
    parsed = parse_hero_list(text, heroes)
    profile = PlayerProfile(
        account_id=0,
        name="pasted hero list",
        rank_tier=bracket * 10 if bracket else None,
        games=sum(hero.games for hero in parsed.heroes),
        wins=sum(hero.wins for hero in parsed.heroes),
        heroes={
            hero.hero_id: PlayerHero(hero_id=hero.hero_id, games=hero.games, wins=hero.wins)
            for hero in parsed.heroes
        },
    )
    return profile, parsed


def read_paste(args: argparse.Namespace) -> str | None:
    """Hero list from a file, or from stdin when the user pipes/pastes one."""
    if args.heroes_file:
        return Path(args.heroes_file).read_text(encoding="utf-8")
    if args.paste:
        if sys.stdin.isatty():
            print("Paste your hero list, then press Ctrl+Z (Windows) or Ctrl+D:")
        return sys.stdin.read()
    return None


# -- commands --------------------------------------------------------------
def cmd_recommend(args: argparse.Namespace, settings: Settings, console: Console) -> int:
    client = build_client(args, settings)
    hero_stats = client.hero_stats()

    pasted_text = read_paste(args)
    parsed = None
    if pasted_text is not None:
        if not args.bracket:
            raise SystemExit(
                "A pasted hero list carries no rank, so the meta bracket is unknown. "
                "Add --bracket 1-8 (1 Herald .. 8 Immortal)."
            )
        profile, parsed = profile_from_paste(pasted_text, client.heroes(), args.bracket)
        account_id = 0
    else:
        account_id = resolve_account(args, settings)
        profile = load_profile(client, account_id, recent_days=args.days)

    medal = args.bracket or profile.medal
    bracket = resolve_bracket(hero_stats, medal)
    meta = build_meta(hero_stats, bracket)

    recommendations = recommend(
        profile,
        meta,
        min_bracket_picks=args.min_picks,
        role=args.role,
        include_unplayed=not args.played_only,
        min_games=args.min_games,
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
    if parsed is not None:
        console.print(
            f"[dim]Read {len(parsed.heroes)} heroes / {parsed.total_games} games "
            f"from the pasted list.[/dim]"
        )
        if parsed.unmatched:
            shown = ", ".join(line[:30] for line in parsed.unmatched[:3])
            console.print(
                f"[yellow]Ignored {len(parsed.unmatched)} unrecognised line(s):[/yellow] {shown}"
            )
    elif not profile.has_match_data:
        console.print(
            "[yellow]No per-hero data from OpenDota.[/yellow] Enable "
            "'Expose Public Match Data' in the Dota 2 settings, then play a match. "
            "Showing pure meta recommendations for now."
        )

    shown = recommendations[: args.top]
    # A pasted list has no matches behind it, and unparsed accounts have no lane
    # data either - in both cases the column would be a wall of dashes.
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
        # Show the discounted projection: the optimistic one flatters a 2-game
        # hero with a number the model does not actually believe.
        mmr = rec.mmr_per_100_games_conservative
        colour = "green" if mmr >= 0 else "red"
        confidence = "low" if rec.games < 15 else ("ok" if rec.games < 50 else "high")
        confidence_colour = {"low": "red", "ok": "yellow", "high": "green"}[confidence]
        table.add_row(
            str(index),
            rec.name,
            record + personal,
            f"{rec.meta_winrate:.1%}",
            f"[{'green' if rec.edge_vs_meta > 0 else 'red'}]{rec.edge_vs_meta:+.1%}[/]"
            if rec.games
            else "-",
            *([rec.lane or "-"] if show_lanes else []),
            f"[{colour}]{mmr:+.0f}[/]",
            f"[{confidence_colour}]{confidence}[/]",
            f"[{CATEGORY_STYLE.get(rec.category, '')}]{rec.category}[/]",
        )
    console.print(table)

    if plan["pool"]:
        names = ", ".join(rec.name for rec in plan["pool"])
        # A range, not a point estimate: the gap between the ends is how much of
        # the projection rests on sample size rather than on demonstrated skill.
        optimistic_per_100 = (2 * plan["expected_winrate"] - 1) * 100 * MMR_PER_WIN
        console.print(
            f"\n[bold]Suggested pool:[/bold] {names}\n"
            f"  winrate {plan['adjusted_winrate']:.1%}-{plan['expected_winrate']:.1%} "
            f"-> [bold]{plan['mmr_per_100_games']:+.0f} to {optimistic_per_100:+.0f}[/bold]"
            f" MMR per 100 games\n"
            f"  [dim]low end assumes your edge is sample noise, high end assumes it is "
            f"real - more games on these heroes closes the gap[/dim]"
        )
        if plan["games_per_week"] >= 1:
            pace = plan["games_per_week"]
            console.print(
                f"  at {pace:.1f} games/week: {plan['mmr_per_week']:+.0f} to "
                f"{(2 * plan['expected_winrate'] - 1) * pace * MMR_PER_WIN:+.0f} MMR/week"
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
    parser.add_argument(
        "--api-key",
        help="OpenDota API key; overrides OPENDOTA_API_KEY. Only raises rate limits.",
    )
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
    rec.add_argument(
        "--min-games",
        type=int,
        default=0,
        help="ignore heroes you have played fewer than N times (0 keeps unplayed heroes)",
    )
    rec.add_argument(
        "--heroes-file",
        help="read a pasted hero list from a file instead of fetching an account",
    )
    rec.add_argument(
        "--paste",
        action="store_true",
        help="read a pasted hero list from stdin (Ctrl+Z on Windows, Ctrl+D elsewhere)",
    )
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
