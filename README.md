# dotameta

`dotameta` recommends which Dota 2 heroes to spam to climb MMR. It combines a
player's ranked All Pick hero record with the current meta in that player's rank
bracket, then reports an explainable MMR-per-100-games estimate.

OpenDota is the default, zero-token source. An optional Stratz token adds real
Immortal and position 1-5 meta data, and can recover a ranked-All-Pick personal
hero aggregate when OpenDota's personal hero history is unavailable.

## Install

Requires Python 3.11+. Install the current GitHub version in an isolated
environment with [pipx](https://pipx.pypa.io/):

```bash
pipx install git+https://github.com/PavluntiyJ/dotameta.git
dotameta --help
```

Or use a virtual environment and pip:

```bash
python -m venv .venv
. .venv/bin/activate                    # Windows: .venv\Scripts\activate
python -m pip install git+https://github.com/PavluntiyJ/dotameta.git
```

The normal OpenDota path needs no API key. See [CONTRIBUTING.md](CONTRIBUTING.md)
for an editable development install.

## Usage

```bash
dotameta recommend --account-id <ACCOUNT_ID>        # recommendation and projection
dotameta recommend --account-id <ACCOUNT_ID> --why  # include ranking reasons
dotameta recommend --role Support --played-only
dotameta recommend --min-games 25 --days 365
dotameta meta --bracket 7 --top 20                  # Divine, no player needed
dotameta player --account-id <ACCOUNT_ID>
dotameta cache                                      # inspect both API caches
dotameta cache --clear                              # clear both API caches
```

`--account-id` accepts a raw account ID or an OpenDota, Dotabuff, Stratz, or
Steam profile URL. The profile sites are only used to parse the ID; the selected
API source supplies the data. Set `DOTAMETA_ACCOUNT_ID` to omit the flag.

Every command accepts `--json`. JSON owns stdout; prompts, warnings, diagnostics,
and errors go to stderr, and a failed command leaves stdout empty.

### Paste Mode

A copied hero table can be used without an API-visible personal history. A paste
carries no rank, so `--bracket` is required. It also cannot prove which modes the
source table includes: filter the table to ranked All Pick before copying it.
Paste-mode projections explicitly warn that they assume this filtering:

```bash
dotameta recommend --paste --bracket 5
dotameta recommend --heroes-file heroes.txt --bracket 7
```

Rows such as `Pudge 1043 53%`, `Nature's Prophet 1,204 49.9%`, and
`Juggernaut 250 130` are accepted. Unrecognized or invalid rows are reported,
not silently coerced.

## API Setup

`dotameta` talks directly to two APIs: OpenDota for ordinary use and, when
configured, Stratz for Immortal, position, and personal-aggregate data.

### OpenDota API

Ordinary use needs no OpenDota key. An optional `OPENDOTA_API_KEY` can provide
higher limits; see OpenDota's official [API key page](https://www.opendota.com/api-keys)
for current access conditions and limits.

### Stratz API

1. [Sign in to Stratz with Steam](https://stratz.com/api).
2. Under **My Tokens**, find the default token and put it only in
   `STRATZ_API_TOKEN` in `.env` or the process environment.
3. Test access without querying an account:

```bash
dotameta meta --source stratz --bracket 7 --top 3
```

The Stratz API page displays current call quotas and application or higher-tier
token choices, including options labelled Individual and Multi. These are
optional; consult Stratz's current terms for availability, conditions, and any
cost.

For local setup, create `.env` in the directory where you run `dotameta` (a
source checkout includes `.env.example`) and fill only the values you need:

```dotenv
OPENDOTA_API_KEY=
STRATZ_API_TOKEN=
DOTAMETA_ACCOUNT_ID=
```

The repository ignores `.env`, and the loader allowlists only
`OPENDOTA_API_KEY`, `STRATZ_API_TOKEN`, and `DOTAMETA_ACCOUNT_ID`. If you run the
tool from another project, ensure that project also ignores `.env`.
`DOTAMETA_ACCOUNT_ID` is only a convenient default player ID, not a credential.

Never paste an API key or token into an issue, chat, or CLI command. The observed
Stratz default-token UI has no self-service revoke button; if a token is exposed,
remove it from local files and environments, then contact Stratz through its
official support or Discord for invalidation or replacement. See
[SECURITY.md](SECURITY.md) for reporting and local-cache guidance.

## Data Sources

### OpenDota

OpenDota is the default and needs no token. Personal `/wl`, `/heroes`, and
`/matches` requests are consistently filtered to ranked All Pick and to `--days`
(90 by default; `0` means all history). OpenDota also supplies public per-medal
meta counts, hero names, capability tags, rank, recency, pace, and parsed lane
data.

OpenDota documents `/heroStats` as a public per-medal aggregate, not specifically
ranked-All-Pick-only. The personal and meta populations are therefore not
perfectly symmetric. Its Immortal `8_*` counts are currently empty, so without
Stratz the nearest populated medal is used and reported as a fallback.

### Stratz

Stratz requires `STRATZ_API_TOKEN`; see [API Setup](#api-setup). It is used for:

- real Immortal bracket counts;
- real position 1-5 filters via `--position`;
- a ranked-All-Pick personal hero aggregate when OpenDota personal hero rows are
  unavailable, or when `--source stratz` is explicit.

```bash
dotameta recommend --account-id <ACCOUNT_ID> --bracket 8
dotameta meta --bracket 8 --position 4
dotameta recommend --account-id <ACCOUNT_ID> --bracket 5 --source stratz
```

Explicit Stratz recommendations require `--bracket`: its personal aggregate has
no rank from which to infer one. It also has no display name, match dates,
recency window, lanes, or pace. It is capped at 10,000 matches. Before any Stratz
personal rows are exposed, dotameta requires the unfiltered all-history aggregate
to cover at least 95% of `min(account match count, 10,000)`; incomplete or
anonymous results are treated as unavailable. Only the separately filtered
ranked-All-Pick rows enter recommendations.

`--source auto` preserves ordinary public OpenDota profiles even when a Stratz
token exists. It uses Stratz meta only for Immortal or `--position`, and may use
the Stratz personal aggregate only when OpenDota personal hero data is
unavailable. In that fallback, OpenDota's already-fetched name and rank remain in
use. `--source opendota` never falls back.

## Cache

OpenDota responses are cached under `.cache/opendota` and Stratz responses under
`.cache/stratz` for six hours by default. These files can contain personal match
or aggregate data. `dotameta cache` inspects both, `dotameta cache --clear`
removes only entries written by this tool, and `--no-cache` avoids reads and
writes. Stratz entries are isolated by a one-way token fingerprint so tokens with
different account permissions never share cached responses; raw tokens are not
stored in cache files.

## How It Works

Raw win rates are never ranked directly:

1. Bracket win rate is shrunk toward 50% according to its sample size.
2. The player's record is shrunk toward that bracket expectation; about 25 games
   are needed before personal data dominates the prior.
3. One standard error is subtracted as a heuristic uncertainty haircut. This is
   not a confidence interval and claims no coverage probability.
4. `adjusted_winrate`, after that haircut, orders heroes, drives the table's
   `MMR/100`, and gates the suggested pool.

The human recommendation table shows `Hero`, personal `Record`, bracket `Meta`,
`vs Meta`, optional `Lane`, conservative `MMR/100`, sample `Conf`, and `Verdict`.
The pool projection is a range: the low end uses adjusted win rate and the high
end uses the pre-haircut blended expectation. Both assume 25 MMR per win. The low
end is only the model's heuristic haircut, not a claim that the player's edge is
noise.

`spam` and `keep` require adjusted win rate above 50%; `drop` instead means the
blended expectation is below 50%. A short or empty pool is valid. Strong unplayed
heroes may appear as `learn`, but weak unplayed heroes are omitted because there
is neither personal evidence nor a meta reason to mention them. `--why` includes
the adjusted ranking win rate and its conservative MMR value, so the explanation
matches the ordering.

Global OpenDota win-rate trend is informational only and never changes a
bracket-specific ranking. Lane is the player's main parsed OpenDota lane, not the
best-performing lane, and lane win rate is shown only with adequate sample and
coverage. Stratz `--position` is the path for actual positions 1-5.

## JSON

The current public JSON contract is `schema_version: 2`. Recommendation output
identifies `player_source` and `meta_source` separately, because auto mode can use
OpenDota for one and Stratz for the other. It also includes requested and resolved
brackets, `data_status`, warnings, `expected_winrate`, `adjusted_winrate`, and
optimistic/conservative projection fields. Player output includes
`player_source`; cache output reports per-source directories and counts plus a
total. Paste recommendations include the ranked-All-Pick assumption in
`warnings`. A requested OpenDota history shorter than 30 days reports
`games_per_week: null` and explains the suppressed 30-day pace in `pace_note`.
No separate formal schema file is published.

## Limitations

- The model has no backtest or holdout comparison against meta-only,
  personal-only, or most-played baselines.
- Historical win rate is not a causal estimate of future results.
- Current rank is applied to the whole OpenDota window.
- Patch, facet, solo/party, side, matchup, and player improvement are ignored.
- OpenDota's opted-in population creates selection bias in its bracket data.
- `--role` uses capability tags, not positions 1-5.
- Pasted tables cannot prove their game mode; projections assume the user
  filtered them to ranked All Pick.
- Stratz personal aggregates lose recency, lanes, pace, rank, and display name.
- Valve does not publish a universal MMR-per-win value; 25 is an assumption.
- The pool projection assumes games are split evenly across its heroes.

The project is maintenance-ready: fixes, API compatibility updates, tests, and
documentation improvements are welcome, but no roadmap promises additional
product scope. See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT. Copyright (c) 2026 Pavel Jevstignejev. Source at
[PavluntiyJ/dotameta](https://github.com/PavluntiyJ/dotameta). Not affiliated
with Valve, OpenDota, or Stratz. Dota 2 is a trademark of Valve Corporation.
