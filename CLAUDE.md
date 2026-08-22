# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

A public MIT-licensed Python CLI that recommends which Dota 2 heroes a player
should spam to gain MMR. It combines ranked-All-Pick personal records with the
current bracket meta from OpenDota and, optionally, Stratz.

## Commands

```bash
python -m pip install -e ".[dev]"  # setup (Python 3.11+)
pytest                             # full suite, offline; sockets are blocked
pytest tests/test_meta.py -k trend
pytest tests/test_recommend.py::test_the_two_cases_the_tool_exists_to_tell_apart
ruff check .
ruff format --check .
python -m build                    # build sdist and wheel
python -m dotameta meta --bracket 7
```

CI validates and clean-installs both the sdist and wheel. Do not use a personal
account as a live smoke fixture. Unit tests and docstrings use clearly synthetic
IDs; any deliberate manual API verification must use an account the operator is
authorized to query and must not be recorded in the tree.

## Architecture

Data flows one way, and fetching is confined to two clients:

```text
opendota.py ─┐
stratz.py ───┴─> meta.py ─────────┐
              player.py + lanes.py├─> recommend.py ─> cli.py
              paste.py ───────────┘
```

Account, Stratz aggregate, and pasted-list paths all produce `PlayerProfile`.
Everything downstream is source-independent.

- **`opendota.py`** is the default REST client. It validates payloads, throttles,
  retries, caches, and converts transport/JSON failures into `OpenDotaError`.
- **`stratz.py`** is the optional GraphQL client for Immortal meta, positions
  1-5, and personal hero aggregates. It requires `User-Agent: STRATZ_API` and
  treats GraphQL's HTTP-200 `errors` array as failure. `verify_access(account_id)`
  deliberately has no default account and exercises all production query paths.
- **`cache.py`** serves both clients. Cache failure must never lose a response,
  and cleanup must never delete a file the tool did not write.
- **`meta.py`** converts source counts to `HeroMeta`. Both sources land in
  `assemble_meta()` so baseline, pick share, and Wilson calculations stay shared.
- **`player.py`** normalizes OpenDota history or the Stratz aggregate into one
  ranked-All-Pick `PlayerProfile`.
- **`lanes.py`** derives OpenDota lane summaries from parsed `lane_role` rows.
- **`paste.py`** parses copied hero tables into the same profile shape.
- **`recommend.py`** owns scoring and verdicts; read it before changing rankings.
- **`stats.py`** owns generic shrinkage and Wilson formulas.
- **`cli.py`** owns argument validation, source selection, Rich output, and the
  public JSON contract.

## Source Boundaries

OpenDota is zero-token and remains the ordinary default. Stratz requires
`STRATZ_API_TOKEN` and is used only for:

- real Immortal bracket data because OpenDota `8_*` counts are empty;
- real positions 1-5 because OpenDota publishes lanes, not positions;
- ranked-All-Pick personal hero aggregates when explicitly selected or when an
  auto-mode OpenDota profile has no personal hero rows.

`--source auto` must not move an ordinary public OpenDota profile merely because
a token exists. It may select Stratz meta for Immortal/position and independently
fall back to Stratz personal data when OpenDota personal heroes are unavailable.
Explicit Stratz recommendations require `--bracket` because the personal
aggregate has no rank.

The Stratz player aggregate invariant is security- and correctness-sensitive:
use only separately filtered ranked-All-Pick rows, expose no rows for anonymous
accounts, and require unfiltered all-history coverage of at least 95% of
`min(matchCount, PLAYER_MATCH_DEPTH)`. Stratz personal rows have no display name,
rank, recency, lanes, or pace. Auto fallback may retain the identity/rank already
fetched from OpenDota, but must not invent the other fields.

## Scoring Invariants

Raw win rates are never ranked. Bracket win rate is shrunk toward 50%, then used
as the prior for the player's record (`PERSONAL_PRIOR_STRENGTH`, about 25 games),
then discounted by one standard error.

1. `rank_key`, the human `MMR/100` column, and pool admission use
   `adjusted_winrate`.
2. `spam_plan` admits only heroes with `adjusted_winrate > 0.5`; a short or empty
   pool is an honest result.
3. `drop` is decided on blended `expected`; `spam`/`keep` are decided on
   `adjusted`.
4. Weak unplayed heroes are omitted. Strong unplayed heroes may be `learn`.
5. `--why` must include the adjusted ranking number and conservative MMR value.

Projection output is a range: adjusted/discounted low end and blended high end.
The low end is a heuristic one-standard-error haircut with no coverage
probability. Never call it a confidence interval, say it assumes the edge is
noise, or print only the optimistic number.

Experience is a signal, not only a confidence input. The defining cases remain:
1000 games at 53% where the bracket wins 51% is `spam`; one game where the bracket
wins 56% is `risky`. Tune constants only with a behavioral test in
`tests/test_recommend.py`.

## OpenDota Details

- Filter `/wl`, `/heroes`, and `/matches` identically to ranked All Pick
  (`lobby_type=7`, `game_mode=22`) and the requested window.
- `/heroStats` is public per-medal data, not documented as ranked-All-Pick-only.
  Do not claim symmetry with personal records.
- `resolve_bracket()` substitutes the nearest populated medal; ties break down.
  Requested and resolved brackets must remain visible in CLI/JSON.
- Never mix `turbo_picks`/`turbo_wins` into ranked projections.
- `pub_*_trend` is global and has a partial final bin. It is informational and
  excluded from ranking.
- `rank_tier` uses medal in the tens and star in the ones (`74` = Divine 4).
- Match outcome is tri-state, derived from `player_slot` and `radiant_win`.
  Undecidable rows leave both wins and denominator.
- `project` replaces default match fields. `MATCH_FIELDS` must list every field
  downstream code reads.
- `lane_role` exists only on parsed matches. Report the main lane, and require
  absolute sample plus `MIN_LANE_COVERAGE` before reporting lane win rate.
- Empty hero rows and zeroed hero rows differ. Preserve `DataStatus` distinctions.

## Constraints

- Tests never access the network. Use factories and fake clients.
- `--json` writes exactly one document to stdout. All other output goes to
  stderr, and nonzero exits leave stdout empty.
- The `.env` allowlist contains exactly `OPENDOTA_API_KEY`,
  `STRATZ_API_TOKEN`, and `DOTAMETA_ACCOUNT_ID`. Credentials have no CLI flag.
- Validate local arguments before constructing clients or making requests.
- `_force_utf8_output()` must run before printing for legacy Windows consoles.
- Scraping sites without a documented API remains out of scope.

## Honest Limitations

Keep these in the README: no backtest or holdout against obvious baselines;
historical win rate is not causal; current rank applies to the whole OpenDota
window; patch, facet, solo/party, side, matchup, and improvement are ignored;
OpenDota's opted-in population is selected; `--role` is capability tags;
Stratz aggregates omit recency/lanes/pace/rank/name; `MMR_PER_WIN = 25` is
unpublished by Valve; and the pool average assumes an even game split.
