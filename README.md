# dotameta

`dotameta` recommends which Dota 2 heroes to spam to climb MMR. It combines a
player's ranked All Pick hero record with the current meta in that player's rank
bracket, then reports an explainable MMR-per-100-games estimate.

OpenDota is the default, zero-token source. An optional Stratz token adds real
Immortal and position 1-5 meta data, and can recover a ranked-All-Pick personal
hero aggregate when OpenDota's personal hero history is unavailable.

## Install

`dotameta` is not published on PyPI, so `pip install dotameta` will not find
it. Everything below comes from the files attached to the
[latest release](https://github.com/PavluntiyJ/dotameta/releases/latest): a
Windows executable that needs nothing installed, and a wheel that needs Python
3.11 or newer. The version number in those file names changes with each
release.

### Windows

The simplest path needs no Python at all. Download
`dotameta-0.4.3-windows-x64.exe` from the
[latest release](https://github.com/PavluntiyJ/dotameta/releases/latest) and
double-click it: the tool opens its [browser UI](#browser-ui). The same file
works as the command line tool from PowerShell:

```powershell
.\dotameta-0.4.3-windows-x64.exe recommend --account-id <ACCOUNT_ID>
```

The executable is not code-signed, so Windows SmartScreen shows a warning the
first time. The safe order is:

1. Compare the download against the release notes:
   `Get-FileHash <file> -Algorithm SHA256`. If it differs, delete the file.
2. Click **More info** in the SmartScreen dialog.
3. Check that the app name is the file you just downloaded and that the
   publisher is listed as unknown, which is expected for an unsigned build.
4. Click **Run anyway**.

Double-clicking opens a console window and a browser tab. The console window is
the tool itself: it serves the page, and closing it stops the page. Close it, or
press Ctrl+C in it, when you are done.

To install it as a normal `dotameta` command instead, use the wheel:

1. Install Python 3.11 or newer from
   [python.org](https://www.python.org/downloads/windows/) and tick
   **Add python.exe to PATH** in the installer. Then open PowerShell and check
   it:

   ```powershell
   py --version
   ```

   If PowerShell answers that `py` is not recognized, Python is not installed or
   was installed without the PATH option; run the installer again.

2. Download `dotameta-0.4.3-py3-none-any.whl` from the
   [latest release](https://github.com/PavluntiyJ/dotameta/releases/latest).
   Each release publishes the SHA-256 of its files, so the download can be
   checked before it is installed:

   ```powershell
   Get-FileHash "$HOME\Downloads\dotameta-0.4.3-py3-none-any.whl" -Algorithm SHA256
   ```

3. Install the downloaded file:

   ```powershell
   py -m pip install --user "$HOME\Downloads\dotameta-0.4.3-py3-none-any.whl"
   ```

4. Run it:

   ```powershell
   dotameta --help
   ```

   If PowerShell reports that `dotameta` is not recognized, its install
   directory is not on `PATH`. Reopening the terminal usually fixes it; the
   module form always works and needs no `PATH` entry:

   ```powershell
   py -m dotameta --help
   ```

Upgrading means downloading the newer wheel and installing it the same way with
`--upgrade`. `py -m pip uninstall dotameta` removes the tool.

To keep `dotameta` and its dependencies out of your user site-packages, install
it with [pipx](https://pipx.pypa.io/) instead of step 3:

```powershell
py -m pip install --user pipx
py -m pipx ensurepath
# reopen PowerShell, then:
pipx install "$HOME\Downloads\dotameta-0.4.3-py3-none-any.whl"
```

### Linux and macOS

Download the same wheel from the
[latest release](https://github.com/PavluntiyJ/dotameta/releases/latest) and
install it into a virtual environment:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install ~/Downloads/dotameta-0.4.3-py3-none-any.whl
dotameta --help
```

Or install it as an isolated command with pipx:

```bash
pipx install ~/Downloads/dotameta-0.4.3-py3-none-any.whl
```

### From source

With git installed, either platform can install the current `main` directly:

```bash
pipx install git+https://github.com/PavluntiyJ/dotameta.git
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
dotameta ui                                         # local browser interface
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

## Browser UI

`dotameta ui` serves a small local page for people who would rather not use a
terminal:

```bash
dotameta ui                 # opens http://127.0.0.1:8765/ in the browser
dotameta ui --port 9000 --no-browser
```

The page is a front end, not a second product. Every request runs the same
command a terminal user would run and renders that command's `--json` document,
so the page and the CLI cannot disagree. Query parameters pass through an
allowlist, so nothing the page sends can become an arbitrary argument.

The page is available in English and Russian; it follows the browser's language
and remembers the choice. Meta position is offered only when a Stratz token is
configured, because OpenDota publishes lanes rather than positions; the page is
told whether a token exists, never what it is. Only text the page owns is translated. Warnings and
per-hero reasons are shown exactly as the CLI wrote them, and the JSON contract
does not change with the language, because translating it would make `--json`
depend on a display setting.

The server binds `127.0.0.1` only and refuses requests whose `Host` header is
not a loopback name, so another site open in the same browser cannot use the
API. Credentials stay in the environment and are never sent to the page. It is a
local convenience, not a service to expose to a network.

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
