# Contributing

Use Python 3.11 or newer and an editable development install:

```bash
python -m venv .venv
. .venv/bin/activate                    # Windows: .venv\Scripts\activate
python -m pip install -e ".[dev]"
pytest
ruff check .
ruff format --check .
python -m build
```

## Project Rules

**Tests never hit the network.** `tests/conftest.py` blocks sockets. Build
OpenDota REST and Stratz GraphQL payloads with fixtures and fake clients; live
meta changes and remote API availability are not test inputs.

**Use only documented API paths.** The supported remote sources are the public
[OpenDota API](https://docs.opendota.com/) and the documented
[Stratz GraphQL API](https://stratz.com/api). Do not add scraping for Dotabuff,
dota2protracker, or other websites. User-supplied pasted/exported data is fine.

**Preserve the source boundary.** OpenDota is the zero-token default. Stratz is
optional and limited to the cases documented in the README: Immortal meta,
positions 1-5, and a coverage-checked ranked-All-Pick personal aggregate.
Both meta paths must converge in `meta.assemble_meta()`.

**Scoring changes need behavioral tests.** The formulas are implemented and
documented in `stats.py` and `recommend.py`: bracket shrinkage, personal
shrinkage, the heuristic one-standard-error discount, verdicts, and projection
ranges. Pin the behavior being changed in `tests/test_recommend.py`; a live
ranking that merely looks better is not reviewable evidence.

**Keep output explainable.** Every displayed value must trace to an API field or
a documented formula. `rank_key`, conservative `MMR/100`, and pool eligibility
must continue to use `adjusted_winrate`, and `--why` must disclose that ranking
number. Do not describe the heuristic haircut as a confidence interval or as a
claim that the player's edge is noise.

**Be careful with MMR claims.** Valve does not publish a universal MMR per win;
`MMR_PER_WIN = 25` is an assumption in `constants.py`. Projections are estimates,
not guarantees.

**Treat data and credentials as private.** Never commit API tokens, real account
fixtures, cache payloads, or personal analysis. Tests and docstrings should use
clearly synthetic account IDs.
