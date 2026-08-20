# dotameta

Tells you **which Dota 2 heroes to spam to climb MMR**, using your own match history and
the current meta in your rank bracket — both pulled from the public
[OpenDota API](https://docs.opendota.com/).

It answers one question with a number you can check: *if I spam these heroes, how much MMR
per 100 games is that worth?*

```bash
dotameta recommend --account-id <ACCOUNT_ID>
```

## Install

```bash
git clone https://github.com/PavluntiyJ/dotameta
cd dotameta
python -m venv .venv && . .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

Requires Python 3.11+. No API key is needed; OpenDota's free tier is enough for personal
use. If you hit the rate limit, get a key at
[opendota.com/api-keys](https://www.opendota.com/api-keys) and pass it either way:

```bash
export OPENDOTA_API_KEY=...        # or put it in .env, see .env.example
dotameta --api-key ... recommend --account-id ...
```

A key only raises the rate limit — it also lifts the client's 1 req/s self-throttle.
Nothing else about the output changes.

## Usage

```bash
dotameta recommend --account-id <ACCOUNT_ID>      # what to spam, with an MMR projection
dotameta recommend --role Support --why       # supports only, with reasoning
dotameta recommend --played-only              # rank only heroes you already play
dotameta recommend --min-games 25             # ignore heroes with a thin personal sample
dotameta recommend --days 365                 # widen the window if you play irregularly
dotameta meta --bracket 7 --top 20            # strongest heroes in Divine, no player needed
dotameta player --account-id <ACCOUNT_ID>         # rank, pace, hero pool
dotameta cache --clear                        # drop cached API responses
```

`--account-id` accepts a pasted profile link or a raw id - all of these are the same
player:

```bash
dotameta recommend --account-id <ACCOUNT_ID>
dotameta recommend --account-id https://www.opendota.com/players/<ACCOUNT_ID>
dotameta recommend --account-id https://www.dotabuff.com/players/<ACCOUNT_ID>/matches
dotameta recommend --account-id https://stratz.com/players/<ACCOUNT_ID>
dotameta recommend --account-id https://steamcommunity.com/profiles/<STEAM64_ID>/
```

Dotabuff and Stratz are only used as a source of the id - all data comes from OpenDota.
Set `DOTAMETA_ACCOUNT_ID` in `.env` to skip the flag entirely. Every command takes `--json` where a
table would otherwise print, so the output can feed something else.

> Your per-hero record is only visible to OpenDota if **Expose Public Match Data** is
> enabled in the Dota 2 settings. Without it the tool still works, but falls back to pure
> meta advice and says so.

## How the recommendation works

"What should I spam" decomposes into two measurable things: how strong a hero is *in your
bracket*, and how well *you* play it. Neither is trustworthy alone — bracket winrates say
nothing about you, and personal winrates usually come from a handful of games.

1. **Bracket meta** — `/heroStats` gives public pick/win counts split by rank medal
   (Herald…Immortal). A hero's bracket winrate is shrunk toward 50% in proportion to how
   thin its sample is.
2. **Your record** — `/players/{id}/heroes`, scoped to a recent window (`--days`, default
   90) so a hero you spammed three patches ago does not dominate.
3. **Blend** — your record is shrunk toward the bracket expectation. It takes roughly 25
   games on a hero before your own winrate outweighs the bracket baseline.
4. **Discount** — the blended estimate is penalised by its own standard error, so a 4-game
   75% hero cannot outrank a 300-game 56% one.
5. **Momentum** — `pub_win_trend` week-over-week movement acts as a small tiebreaker
   between otherwise similar heroes.
6. **Projection** — reported as a **range** of MMR per 100 games (25 MMR per win). The
   low end assumes your edge over the bracket is sample noise, the high end assumes it is
   real. A wide range means "you need more games on this hero before trusting it"; the two
   ends converge as your sample grows.

Each hero lands in one of four buckets: `spam` (you win on it and the meta agrees), `keep`
(you win on it despite the meta), `learn` (strong in your bracket, you haven't played it),
`drop` (you keep playing it and keep losing).

The tool suggests a **pool of three** rather than one hero, because ranked All Pick has a
ban phase and a one-trick gets denied or countered.

## Data sources

OpenDota only. Sites like dota2protracker have no public API, and scraping them would
violate their terms — so bracket meta is computed from OpenDota's own public match data
instead. Everything the tool concludes is reproducible from endpoints anyone can call.

Responses are cached under `.cache/opendota` for 6 hours (`--cache-ttl`, `--no-cache`) to
stay inside the free rate limit, which is 60 requests/minute.

## Development

```bash
pytest                        # full suite, fully offline
pytest tests/test_meta.py -k trend
ruff check . && ruff format .
```

Tests use fixed fixtures shaped like real OpenDota payloads, never the live API — the meta
changes every patch and a test that depends on it is a test that fails on Tuesday.

## Roadmap

- Matchup and synergy signals (`with_games` / `against_games`) for counter-pick advice
- Position inference from lane/role data instead of OpenDota's coarse role tags
- Draft-time recommendations given the heroes already picked
- Streak and tilt detection from match timestamps

Contributions welcome — see [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT. Copyright (c) 2026 Pavel Jevstignejev. Not affiliated with Valve or OpenDota. Dota 2 is a trademark of Valve Corporation.
