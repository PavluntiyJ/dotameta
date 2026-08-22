# Changelog

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
