# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Python CLI that recommends which Dota 2 heroes a player should spam to gain MMR, by
combining that player's OpenDota match history with the current meta **in their own rank
bracket**. Public open-source project (MIT); assume every change will be read by strangers.

## Commands

```bash
pip install -e ".[dev]"          # setup (Python 3.11+)

pytest                           # full suite, offline
pytest tests/test_meta.py -k trend        # single test / pattern
pytest tests/test_recommend.py::test_categories

ruff check . && ruff format .    # lint + format (CI runs `ruff format --check`)

python -m dotameta recommend --account-id <ACCOUNT_ID> --why    # run without installing
dotameta meta --bracket 7                                   # installed entry point
```

Use only synthetic fixtures for tests; do not record live account IDs.

## Architecture

Data flows one way, and each layer is independently testable because none of them fetch:

```
opendota.py  →  meta.py      ┐
   (HTTP)       (bracket)    ├→  recommend.py  →  cli.py
             player.py       ┘   (scoring)        (rich tables / --json)
             (one player)
```

- **`opendota.py`** — the only module that touches the network. Throttles to ~1 req/s
  (free tier is 60/min), retries 429/5xx, and writes every response through `cache.py`
  (6h TTL, `.cache/opendota`). Nothing downstream knows HTTP exists.
- **`meta.py`** — converts `/heroStats` into `HeroMeta` per hero for one rank bracket.
- **`player.py`** — converts the player endpoints into one `PlayerProfile`.
- **`recommend.py`** — the actual model. Everything else is plumbing for this file.
- **`stats.py`** — shrinkage and Wilson bounds; the reason rankings aren't noise.
- **`cli.py`** — argparse subcommands, rich rendering, `--json` for every command.

### The scoring model (read `recommend.py` before changing rankings)

Raw winrates are never ranked on. The chain is: bracket winrate shrunk toward 50% by
sample size → used as the prior for the player's own record (`PERSONAL_PRIOR_STRENGTH`,
~25 games before personal data dominates) → discounted by its own standard error →
nudged by week-over-week meta momentum (`TREND_WEIGHT`, deliberately small).

Result is reported as **MMR per 100 games** (`MMR_PER_WIN = 25`, an estimate — Valve does
not publish it) so a recommendation can be sanity-checked against reality rather than
being an opaque score.

Tuning constants live at the top of `recommend.py`. Changing one changes every
recommendation, so pair any change with a test in `tests/test_recommend.py` that pins the
behaviour being claimed.

## OpenDota specifics that bite

These were verified against live payloads; do not "fix" the workarounds:

- `/heroStats` splits public matches per rank medal as `1_pick`/`1_win` … `8_pick`/`8_win`
  (1 = Herald, 8 = Immortal). **`8_*` is currently all zeros** — `resolve_bracket()` walks
  down to the nearest populated medal, which is why an Immortal player gets Divine numbers.
- Same payload carries `turbo_picks`/`turbo_wins`. Never mix them in: turbo winrates do
  not transfer to ranked All Pick.
- `pub_pick_trend`/`pub_win_trend` are weekly arrays, oldest first — the momentum signal.
- Player `rank_tier` is two digits: tens = medal, ones = star (`74` = Divine 4).
- Match rows have no "did I win" field: derive it from `player_slot < 128` vs
  `radiant_win` (`player.is_win`).
- A player who has not enabled *Expose Public Match Data* returns an empty hero list.
  `PlayerProfile.has_match_data` exists so the CLI degrades to meta-only advice loudly
  rather than silently ranking nothing.

## Constraints

- **OpenDota is the only data source.** dota2protracker and similar sites have no public
  API; scraping them violates their terms and is out of scope. Bracket meta is computed
  from OpenDota's own public match data instead. (This was a deliberate project decision,
  not an oversight.)
- **Tests must not hit the network.** The live meta shifts every patch. Use the payload
  builders in `tests/factories.py`.
- Secrets only via `.env` / environment (`OPENDOTA_API_KEY`, `DOTAMETA_ACCOUNT_ID`). The
  API key only raises rate limits — the tool must keep working without one.
- `cli.py` calls `_force_utf8_output()` before printing: hero names and rich's box drawing
  break on legacy Windows code pages.
