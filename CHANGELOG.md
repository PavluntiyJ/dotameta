# Changelog

## 0.5.0 - 2026-08-23

### Added

- Recommendation JSON now includes a `personal` summary with games, wins,
  winrate, heroes played, and data availability. Fields are null when the source
  genuinely did not supply them rather than being replaced by zero.
- Finite OpenDota windows below the model's personal-prior sample carry a warning
  suggesting a 365-day or all-history comparison.
- JSON-mode failures emit one structured error object to stderr with a stable
  code, optional field, and human-readable message; stdout remains empty. The
  browser UI localizes these codes and highlights the related input. An
  interrupted run and an internal failure carry their own codes, so a caller can
  tell a bad request from a bug in the tool.

### Changed

- The public JSON contract is now schema 3.
- `player --json` reports `games`, `wins` and `hero_pool_size` as null when the
  source did not supply them, instead of as zeros that read like observations.
  The human summary prints `unavailable` in the same case.
- The terminal and browser summaries show the personal sample represented by the
  recommendation document.

### Fixed

- `edge_vs_meta` is now null for an unplayed hero instead of exposing a synthetic
  zero. The browser renders that contract directly instead of correcting it.
- The browser no longer replaces a precise diagnostic with a general translated
  one: it shows the CLI's own wording, and uses its own only for codes whose
  message names a command-line flag or an environment variable.
- Paste mode no longer writes an interactive prompt while running with `--json`.

## 0.4.3 - 2026-08-23

### Changed

- The table separates heroes you have played from meta candidates you have not,
  so a thin recent history no longer reads as a generic tier list.
- Filters say what they actually filter: `Meta bracket`, `Capability tag`
  (Valve tags, not matchmaking positions), `Meta position` (which does not
  filter your own record), `Personal history`, `Max pool size`. Personal history
  is a set of presets including all history, which was previously reachable only
  by typing `0` into a number box.
- Meta position is offered only when a Stratz token is configured. The page is
  told whether one exists, never its value, so a control that would fail every
  time is no longer offered; a collapsible note explains what works without it.
- A verdict legend explains why `keep` can rank above `spam`, the verdict column
  sorts in that order rather than alphabetically, and the empty-pool message
  says `conservative evidence threshold` rather than `confidence bar` and offers
  a wider window in one click.
- Long or all-history windows carry a warning that the current rank is applied
  to every match in them.
- Stat cards note that the bracket meta is a public per-medal aggregate, and
  that MMR per week uses the recent pace.
- README documents the SmartScreen steps and that the console window is the
  server.

### Fixed

- An unplayed hero showed `0.0 pp` against the meta, coloured as a loss. There
  is no comparison to make without a personal record, so the cell is now empty.
  The underlying `edge_vs_meta: 0.0` in the JSON is a contract change and waits
  for the next schema version.
- A hero in the suggested pool could be missing from the table when `Table rows`
  was smaller than the pool, leaving no way to open its reasons.
- After a failed request the page showed the initial "paste an account id" hint,
  which claimed an empty field that was not empty. Errors now keep the previous
  result on screen, labelled as such.
- Trends and edges smaller than 0.05 pp are shown as a plain zero rather than as
  `-0.0`.

## 0.4.2 - 2026-08-23

### Added

- The browser UI speaks English and Russian. The initial language follows the
  browser and the choice is remembered; a switch in the header changes it
  without refetching anything. Only text the page owns is translated: labels,
  column headers, verdict labels, medal names and the disclaimer.

### Changed

- Verdict labels, bracket names and the rank medal are shown in the selected
  language. `category` in the JSON stays the CLI's English value, and warnings
  and per-hero reasons are shown exactly as the CLI wrote them, because they are
  its sentences rather than the page's.
- The `Position` hint was shortened; in Russian it wrapped and pushed its own
  field out of the row.

### Fixed

- A regular expression in the page used a backslash-b escape, which Python turns into a
  backspace character before the browser ever sees it. The page now contains no
  backslashes at all, and a test keeps it that way.

## 0.4.1 - 2026-08-23

### Changed

- The browser UI was redesigned: summary cards, a pool of hero cards, verdict
  pills carrying an icon as well as a colour, sortable columns, per-row reasons
  and tags on click, loading and empty states, an inline icon and version, and
  remembered form values. Hero portraits are fetched by the browser from Valve's
  CDN, with initials shown whenever that is unavailable, and the id-to-portrait
  map comes from OpenDota's public constants endpoint. Nothing on the page is
  computed: it still renders the CLI's `--json` document.
- The UI answers a second accessibility and responsiveness pass: a single
  column below 640px, keyboard-operable row details with `aria-expanded`,
  sortable columns that keep `th`/`aria-sort` semantics and can be cycled back
  to the CLI's own ranking, live-region status and `role="alert"` errors, a
  visible focus ring, `prefers-reduced-motion`, a table caption and a focusable
  scroll region.
- The form now names things accurately: `Hero tag` rather than `Role`, because
  those are OpenDota capability tags, with a separate `Position` field for real
  positions 1-5. A configured `DOTAMETA_ACCOUNT_ID` is filled into the account
  field, which lets the field be required. A substituted bracket is shown as
  `Divine → Ancient` rather than only the substitute, sources are labelled
  `Player` and `Meta`, the table column is `MMR / 100 low`, and the MMR per win
  in the footer comes from the JSON instead of a hardcoded 25.
- Hero portraits use stale-while-revalidate: a saved map is applied at once and
  refreshed in the background, portraits hydrate into rows that already
  rendered initials, and a hero whose image fails is not requested again.
- Responses carry a Content-Security-Policy that permits those two hosts and no
  third-party code at all, plus `Referrer-Policy: no-referrer`. `/icon.svg` is
  served for the tab icon and `/favicon.ico` answers 204 instead of a JSON 404.
- The Windows executable ships with an application icon and filled-in file
  properties (product, version, copyright), so a legitimate unsigned build stops
  looking like an anonymous binary. `packaging/build_exe.py` builds it and
  `packaging/make_icon.py` generates the icon.

### Fixed

- The `played only` checkbox inherited the text inputs' minimum width, which
  left it detached from its own label.
- Importing `dotameta.ui` emitted a `SyntaxWarning`: a regular expression in
  the page contained `\?`, which Python reads as an escape before JavaScript
  ever sees it.
- Numeric table headers were left-aligned over right-aligned numbers, a losing
  hero drew an empty progress track, and hidden panels still painted their
  borders because a class with `display` outranks the user agent `[hidden]`
  rule.
- `.gitignore` now covers built executables and pasted screenshots, so a stray
  `git add -A` cannot commit a 13 MB binary to a public repository.

## 0.4.0 - 2026-08-23

### Added

- `dotameta ui` serves a local browser page that runs the same commands the
  terminal does and renders their `--json` documents. It binds loopback only,
  refuses non-loopback `Host` headers, and translates query parameters through
  an allowlist rather than forwarding them as arguments.
- A single-file Windows executable, built from `packaging/dotameta_app.py`,
  attached to the release. It needs no Python, works as the ordinary command
  line tool, and opens the UI when it is started with no arguments. It is not
  code-signed, so SmartScreen warns on first run.

### Changed

- CI reads the package version from `_version.py` instead of hardcoding it, so a
  version bump can no longer leave the artifact checks testing stale names.
- Install instructions lead with Windows and with the wheel attached to the
  release, since the project is not published on PyPI.

## 0.3.0 - 2026-08-23

### Added

- Optional documented Stratz API support for Immortal meta, positions 1-5, and
  ranked-All-Pick personal aggregate fallback.
- Per-source cache inspection and cleanup, source attribution in JSON, stricter
  payload validation, and clean sdist/wheel packaging checks.

### Changed

- JSON schema version is now 2, including separate `player_source` and
  `meta_source` attribution and structured per-source cache output.
- Recommendations expose the adjusted ranking value in `--why`, use a clearly
  labeled heuristic projection range, and omit weak unplayed heroes.
- API credentials are environment-only; the command-line OpenDota key flag was
  removed.

### Security

- Stratz personal aggregates require at least 95% all-history coverage before
  hero rows are used, and anonymous or incomplete results remain unavailable.
- Stratz cache entries are isolated by a one-way token fingerprint, raw tokens
  are never stored, and tokens with different permissions cannot share results.
- Non-success API errors are status-only and do not reflect response bodies;
  cache cleanup only removes verified dotameta entries.
