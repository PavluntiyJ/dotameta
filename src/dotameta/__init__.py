"""Hero spam and MMR-climb recommendations built from OpenDota data."""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _version

__version__ = "0.2.0"

try:
    # Prefer the installed distribution's version so a built wheel and the source
    # tree can never disagree; fall back when running from a plain checkout.
    __version__ = _version("dotameta")
except PackageNotFoundError:  # pragma: no cover - only when not installed
    pass

__all__ = ["__version__"]
