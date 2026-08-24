from __future__ import annotations

import argparse
from pathlib import Path
import re
import sys


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app_version import APP_VERSION


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate a release tag and generate Windows version metadata.")
    parser.add_argument("--tag", default="")
    args = parser.parse_args()

    tag = args.tag.strip().lstrip("v")
    if tag and tag != APP_VERSION:
        raise RuntimeError(f"release tag v{tag} does not match app_version.py ({APP_VERSION})")
    if not re.fullmatch(r"\d+\.\d+\.\d+", APP_VERSION):
        raise RuntimeError(f"invalid APP_VERSION: {APP_VERSION}")

    major, minor, patch = (int(part) for part in APP_VERSION.split("."))
    content = f'''VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=({major}, {minor}, {patch}, 0),
    prodvers=({major}, {minor}, {patch}, 0),
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable(
        '040904B0',
        [
          StringStruct('CompanyName', 'yinghuasuan'),
          StringStruct('FileDescription', 'yhs-fish-plugin'),
          StringStruct('FileVersion', '{APP_VERSION}'),
          StringStruct('ProductName', 'yhs-fish-plugin'),
          StringStruct('ProductVersion', '{APP_VERSION}')
        ]
      )
    ]),
    VarFileInfo([VarStruct('Translation', [2052, 1200])])
  ]
)
'''
    (ROOT / "version_info.txt").write_text(content, encoding="utf-8")
    print(APP_VERSION)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
