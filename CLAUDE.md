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
opendota.py  →  meta.py       ┐
   (HTTP)       (bracket)     │
             player.py+lanes  ├→  recommend.py  →  cli.py
             (one account)    │   (scoring)        (rich tables / --json)
             paste.py         ┘
             (pasted list)
```

Two entry paths produce the same `PlayerProfile`: fetched by account id, or parsed from a
pasted hero table (`--paste` / `--heroes-file`, which needs `--bracket` since a paste
carries no rank). Everything downstream is identical.

- **`paste.py`** — parses a hero table the user pasted, producing the same shape as
  fetched data so the recommender cannot tell the difference.
- **`lanes.py`** — which lane the player plays each hero in, from `lane_role`.
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

Result is reported as a **range** of MMR per 100 games (`MMR_PER_WIN = 25`, an estimate —
Valve does not publish it): the low end from the discounted winrate, the high end from the
blended one. Never report only the optimistic number — the table used to rank on the
discounted winrate while printing the optimistic one, which advertised a 2-game hero at
an optimistic gain while the model itself ranked the hero negatively.

Experience is a signal in its own right, not just a confidence input. The two cases that
define the product: 1000 games at 53% on a hero the bracket wins 51% with is `spam`; one
game on a hero the bracket wins 56% with is `risky`. If a change breaks either, it is
wrong regardless of what the aggregate metrics say — `test_the_two_cases_the_tool_exists_to_tell_apart`
pins them.

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
- `lane_role` / `is_roaming` only exist on **parsed** matches — about a third of them. A
  hero's lane sample is therefore much smaller than its game count, so `lanes.py` gates a
  lane *winrate* behind both an absolute sample and `MIN_LANE_COVERAGE`; the lane
  *preference* needs far less. Do not relax this: a small parsed subset can badly misrepresent the full record.
- Adding `project=[...]` to `/players/{id}/matches` **replaces** part of the default field
  set — `start_time` disappears unless requested explicitly, silently breaking
  `games_per_week`.
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
- Secrets only via `.env` / environment (`OPENDOTA_API_KEY`, `DOTAMETA_ACCOUNT_ID`) or
  `--api-key`. The key only raises rate limits, and OpenDota requires a payment method to
  issue one, so the tool must keep working well without it — assume no key is present.
- `cli.py` calls `_force_utf8_output()` before printing: hero names and rich's box drawing
  break on legacy Windows code pages.
