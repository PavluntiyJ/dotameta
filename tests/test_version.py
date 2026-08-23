from __future__ import annotations

import tomllib
from pathlib import Path

import dotameta
from dotameta._version import __version__


def test_public_version_has_one_source():
    pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))

    assert pyproject["tool"]["setuptools"]["dynamic"]["version"] == {
        "attr": "dotameta._version.__version__"
    }
    assert __version__ == "0.5.0"
    assert dotameta.__version__ == __version__
