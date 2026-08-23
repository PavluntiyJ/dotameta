# Changelog

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
