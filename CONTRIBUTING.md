# Contributing

Use Python 3.11 or newer and an editable development install.

Windows (PowerShell):

```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
py -m pip install -e ".[dev]"
pytest
ruff check .
ruff format --check .
py -m build
```

Linux and macOS:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e ".[dev]"
pytest
ruff check .
ruff format --check .
python -m build
```

## Windows Executable

`packaging/dotameta_app.py` is the entry point for a click-to-run build. It is
the only place where an empty argv means `ui`; the wheel's `dotameta` command
keeps argparse's usage error, because a terminal user who typed nothing made a
mistake and someone who double-clicked an icon did not.

```powershell
py -m pip install pyinstaller
py packaging/build_exe.py
dist\dotameta-<version>-windows-x64.exe --help
```

`build_exe.py` reads the version from `_version.py`, names the file after it,
attaches `packaging/dotameta.ico`, and fills in the Windows file properties.
None of that is decoration: an unsigned binary with a default icon and a blank
Properties dialog is indistinguishable from something hostile, and the build
already costs the user a SmartScreen prompt.

Regenerate the icon with `py packaging/make_icon.py` after editing its shapes.

PyInstaller is not a project dependency: it is only needed to produce a release
executable, and the build must run on Windows to produce a Windows binary.

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
