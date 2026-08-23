"""Entry point for the packaged Windows executable.

The wheel's `dotameta` command must keep its argparse behaviour: no arguments is
a usage error, because a terminal user typed something incomplete. Someone who
double-clicks an .exe typed nothing on purpose and expects a window, so this
entry point - and only this one - treats an empty argv as `ui`.

Build (see CONTRIBUTING.md):

    pyinstaller --onefile --name dotameta packaging/dotameta_app.py
"""

from __future__ import annotations

import sys

from dotameta.cli import main

if __name__ == "__main__":
    argv = sys.argv[1:] or ["ui"]
    sys.exit(main(argv))
