"""Build the Windows executable, with an icon and real file properties.

An unsigned binary always costs the user a SmartScreen prompt. What it should
not also cost is a blank Properties dialog and a default icon, because those are
what make a legitimate build look like malware. This script pins both, and takes
the version from `_version.py` so a release cannot ship a file describing itself
as an older one.

    py -m pip install pyinstaller
    py packaging/build_exe.py

The result is `dist/dotameta-<version>-windows-x64.exe`. It must be run on
Windows: PyInstaller does not cross-compile.
"""

from __future__ import annotations

import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENTRY = ROOT / "packaging" / "dotameta_app.py"
ICON = ROOT / "packaging" / "dotameta.ico"

# The VSVersionInfo resource Windows reads for the Properties dialog. Fields
# left blank here show up blank there, which is exactly the look to avoid.
VERSION_RESOURCE = """VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={numbers},
    prodvers={numbers},
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0),
  ),
  kids=[
    StringFileInfo([
      StringTable(
        '040904B0',
        [StringStruct('CompanyName', 'Pavel Jevstignejev'),
         StringStruct('FileDescription', 'Dota 2 hero recommendations from OpenDota'),
         StringStruct('FileVersion', '{version}'),
         StringStruct('InternalName', 'dotameta'),
         StringStruct('LegalCopyright', 'Copyright (c) 2026 Pavel Jevstignejev. MIT License.'),
         StringStruct('OriginalFilename', '{filename}'),
         StringStruct('ProductName', 'dotameta'),
         StringStruct('ProductVersion', '{version}')])
    ]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"""


def package_version() -> str:
    source = (ROOT / "src" / "dotameta" / "_version.py").read_text(encoding="utf-8")
    match = re.search(r'__version__ = "([^"]+)"', source)
    if match is None:
        raise SystemExit("could not read __version__ from src/dotameta/_version.py")
    return match.group(1)


def main() -> int:
    try:
        import PyInstaller.__main__
    except ImportError:
        raise SystemExit("PyInstaller is not installed: py -m pip install pyinstaller") from None
    if sys.platform != "win32":
        raise SystemExit("a Windows executable must be built on Windows")
    if not ICON.exists():
        raise SystemExit(f"missing {ICON}; run: py packaging/make_icon.py")

    version = package_version()
    name = f"dotameta-{version}-windows-x64"
    # Windows wants four numbers; the project uses three.
    numbers = tuple(int(part) for part in version.split(".")[:3]) + (0,)

    with tempfile.TemporaryDirectory() as work:
        resource = Path(work) / "version_info.txt"
        resource.write_text(
            VERSION_RESOURCE.format(numbers=numbers, version=version, filename=f"{name}.exe"),
            encoding="utf-8",
        )
        PyInstaller.__main__.run(
            [
                str(ENTRY),
                "--onefile",
                "--name",
                name,
                "--icon",
                str(ICON),
                "--version-file",
                str(resource),
                "--distpath",
                str(ROOT / "dist"),
                "--workpath",
                f"{work}/build",
                "--specpath",
                work,
                "--noconfirm",
                "--clean",
            ]
        )
    print(f"\nbuilt dist/{name}.exe")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
