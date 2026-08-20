# Contributing

```bash
python -m venv .venv && . .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
pytest && ruff check . && ruff format .
```

## Ground rules specific to this project

**Tests never hit the network.** The live meta changes every patch; a test that reads it
is a test that fails on Tuesday for reasons unrelated to your change. Build payloads with
the helpers in `tests/factories.py` instead.

**OpenDota is the only data source.** Adding a scraper for a site without a public API
(dota2protracker, Dotabuff, Stratz's non-free tiers) is out of scope — it breaks on every
redesign and violates those sites' terms. A documented import path for data a user
exported themselves is fine.

**Scoring changes need a test that pins the behaviour you claim.** If you argue that
matchup data should outweigh personal winrate, add the fixture where the two disagree and
assert which one wins. "Rankings look better now" is not reviewable.

**Keep the output explainable.** Every number the CLI prints should be traceable to an
OpenDota field or a documented formula in `stats.py`. `--why` must stay honest: if a hero
is recommended, the reasons listed are the reasons it ranked.

**Be careful with MMR claims.** Valve does not publish MMR per win; 25 is an estimate and
`MMR_PER_WIN` is a single constant in `constants.py` for that reason. Do not present
projections as guarantees in UI text.
