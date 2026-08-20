# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Python CLI that recommends which Dota 2 heroes a player should spam to gain MMR, by
combining that player's OpenDota match history with the current meta **in their own rank
bracket**. Public open-source project (MIT); assume every change will be read by strangers.

## Commands

```bash
pip install -e ".[dev]"          # setup (Python 3.11+)

pytest                           # full suite, offline (sockets are blocked)
pytest tests/test_meta.py -k trend        # single test / pattern
pytest tests/test_recommend.py::test_the_two_cases_the_tool_exists_to_tell_apart

ruff check . && ruff format .    # CI runs `ruff format --check`
python -m build                  # sdist + wheel; CI installs the wheel clean

python -m dotameta recommend --account-id <ACCOUNT_ID> --why    # run without installing
dotameta meta --bracket 7                                   # installed entry point
```

Use only synthetic fixtures for tests; do not record live account IDs.

## Architecture

Data flows one way, and each layer is independently testable because none of them fetch:

```
opendota.py ─┐
stratz.py ───┴→  meta.py        ┐
  (HTTP)         (bracket)      │
              player.py+lanes   ├→  recommend.py  →  cli.py
              (one account)     │   (scoring)        (rich tables / --json)
              paste.py          ┘
              (pasted list)
```

Two entry paths produce the same `PlayerProfile`: fetched by account id, or parsed from a
pasted hero table (`--paste` / `--heroes-file`, which needs `--bracket` since a paste
carries no rank). Everything downstream is identical.

- **`opendota.py`** / **`stratz.py`** — the only modules that touch the network. Stratz
  needs `User-Agent: STRATZ_API` to get past Cloudflare, and reports query failures inside
  an HTTP 200 `errors` array, so a status-code check alone reads failure as success.
  `StratzClient.verify_access()` exists because GraphQL field names drift - run it against
  a live token before trusting `hero_win_rates`; the unit tests pin the transport contract,
  not the schema.
  `opendota.py` is the default and the only source that works without a token; it
  throttles, retries, and converts *every* transport failure into `OpenDotaError`. Nothing
  above either module should see a `requests` exception, a `JSONDecodeError`, or a
  traceback.
- **`cache.py`** — never loses a response to a cache problem, and never deletes a file it
  did not write (`--cache-dir` is user-supplied).
- **`meta.py`** — counts → `HeroMeta` per hero for one bracket. Both sources land in
  `assemble_meta()`, so baseline, pick share and Wilson bound are computed once.
- **`player.py`** — the personal endpoints → one `PlayerProfile`, ranked-All-Pick only.
- **`lanes.py`** — which lane the player plays each hero in, from `lane_role`.
- **`paste.py`** — parses a pasted hero table into the same shape as fetched data.
- **`recommend.py`** — the actual model. Everything else is plumbing for this file.
- **`stats.py`** — shrinkage and Wilson bounds; the reason rankings aren't noise.
- **`cli.py`** — argparse, Rich rendering, and the `--json` contract.

### The scoring model (read `recommend.py` before changing rankings)

Raw winrates are never ranked on. The chain is: bracket winrate shrunk toward 50% by
sample size → used as the prior for the player's own record (`PERSONAL_PRIOR_STRENGTH`,
~25 games before personal data dominates) → discounted by one standard error.

Three invariants hold this together. Breaking any of them reintroduces a shipped bug:

1. **`rank_key`, the MMR column and the pool all use `adjusted_winrate`.** The table once
   sorted on the discounted number while printing the optimistic one, advertising a 2-game
   thin-sample hero that the model itself valued negatively.
2. **`spam_plan` only admits heroes with `adjusted_winrate > 0.5`.** A short or empty pool
   is a valid, honest answer. Padding it by category produced a "climbing plan" with a
   negative projection.
3. **`drop` is decided on `expected`, `spam`/`keep` on `adjusted`.** These answer different
   questions — "are you losing?" versus "is the edge proven?". Deciding `drop` on the
   discounted number can label a positive but unproven record as a drop.

Projections are a **range**: low end discounted, high end blended. The low end is a
heuristic one-standard-error haircut with no coverage probability — never call it a
confidence interval, and never print the optimistic number alone.

Experience is a signal in its own right, not just a confidence input. The two cases that
define the product: 1000 games at 53% on a hero the bracket wins 51% with is `spam`; one
game on a hero the bracket wins 56% with is `risky`. If a change breaks either it is
wrong, whatever the aggregate metrics say — `test_the_two_cases_the_tool_exists_to_tell_apart`
pins them.

Tuning constants live at the top of `recommend.py`. Never adjust one to make a live
ranking look better; pair every change with a behavioural test in
`tests/test_recommend.py`.

## OpenDota specifics that bite

Verified against live payloads; do not "fix" the workarounds:

- **Personal endpoints must be filtered to ranked All Pick** (`lobby_type=7`,
  `game_mode=22`) — all three of `/wl`, `/heroes`, `/matches`, with the same window.
  Unfiltered personal matches would feed the wrong population into a ranked-MMR projection. `is_ranked_all_pick()` re-checks locally.
- `/heroStats` is a **public** per-medal aggregate. It is *not* documented as
  ranked-All-Pick-only, so the two sides of the model are filtered differently. Say so;
  do not claim symmetry the data does not have.
- `1_pick`/`1_win` … `8_pick`/`8_win` are the per-medal splits. **`8_*` is all zeros** —
  `resolve_bracket()` substitutes the nearest populated medal by absolute distance (ties
  break downward), and the CLI/JSON always report requested *and* resolved.
- `turbo_picks`/`turbo_wins` sit in the same payload. Never mix them in.
- `pub_pick_trend`/`pub_win_trend` are **global** bins with a **partial final bin**. Not
  bracket-specific, so they are informational and excluded from `rank_key`.
- `rank_tier` is two digits: tens = medal, ones = star (`74` = Divine 4).
- Match rows have no "did I win": derive it from `player_slot < 128` vs `radiant_win`.
  `match_outcome()` is **tri-state** — an undecidable row is excluded from wins *and* from
  the denominator, not counted as a loss.
- Adding `project=[...]` to `/players/{id}/matches` **replaces** part of the default field
  set; `MATCH_FIELDS` must list everything downstream reads. `start_time` disappeared this
  way once and silently broke pace.
- `lane_role`/`is_roaming` exist only on **parsed** matches — about a third. Lane
  *preference* is readable from few games; a lane *winrate* needs both an absolute sample
  and `MIN_LANE_COVERAGE`. A small parsed subset can badly misrepresent the full hero record. Report the **main** lane, never the highest-winrate one.
- Empty hero rows and *zeroed* hero rows mean different things: `DataStatus` separates
  `private_or_unavailable` from `empty_window` so an idle public account is not told to
  change a privacy setting.

## Constraints

- **Two sources, and the boundary between them is deliberate.** OpenDota is the default
  and needs nothing. Stratz (`stratz.py`, GraphQL, free Steam token) is used *only* where
  OpenDota structurally cannot answer: the Immortal bracket and real positions 1-5.
  `--source auto` will not switch sources for ordinary queries just because a token is
  present - that would move people's numbers under them and spend requests for nothing.
  Both sources feed `meta.assemble_meta()` so the aggregation cannot drift apart.
  dota2protracker and similar still have no public API; scraping them stays out of scope.
- **Tests must not hit the network** — `conftest.py` blocks sockets outright. Use the
  builders in `tests/factories.py` and the fake clients in `tests/test_*.py`.
- **`--json` owns stdout.** One `json.dump`, nothing else; Rich, prompts, warnings and
  errors go to stderr. On a nonzero exit stdout stays empty.
- Secrets only via `.env` / environment (`OPENDOTA_API_KEY`, `DOTAMETA_ACCOUNT_ID`) or
  `--api-key`. `.env` is an **allowlist** of those two names — a `.env` in the working
  directory is not necessarily the user's, and a loader would let it set `HTTPS_PROXY`.
  OpenDota requires a payment method for a key, so assume none is present.
- Validate CLI arguments before any request; negative counts are rejected by the argparse
  types in `cli.py`.
- `cli.py` calls `_force_utf8_output()` before printing: hero names and Rich's box drawing
  break on legacy Windows code pages.

## Honest limitations (keep these in the README)

No backtest or holdout against meta-only / personal-only / most-played baselines.
Historical winrate is not a causal estimate of future results. Current rank is applied to
the whole window. Patch, facet, solo/party, side, matchup and player improvement are
ignored. OpenDota's opted-in population is a selection bias. `--role` uses capability
tags, not positions 1–5. `MMR_PER_WIN = 25` assumes a symmetry Valve has never published.
The pool average assumes games are split evenly across it.
