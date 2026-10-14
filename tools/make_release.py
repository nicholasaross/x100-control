#!/usr/bin/env python3
"""Build the release zip, stamped with a build ID that ``x100ctl --version`` prints.

    python3 tools/make_release.py                 # writes ../x100-control.zip
    python3 tools/make_release.py --print-build   # just the ID of the files here

The build ID is the first 8 hex digits of a SHA-256 over every released file's path,
contents and executable bit. x100/_build.py, which holds the ID, is left out of the
hash. Two zips with the same ID hold the same files, and any change gives a new ID.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import os
import sys
import zipfile
from pathlib import Path
from typing import List

ROOT = Path(__file__).resolve().parents[1]
PREFIX = "x100-control"            # the zip unpacks into this folder
STAMP = Path("x100/_build.py")

# Never released: per-install state and secrets, caches, build output, test assets.
SKIP_DIRS = {"__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache", ".git", "build", "dist",
             "certs", "clips", "models", "test-models", "test-voices"}
SKIP_PATHS = {Path("tests/data"), Path("config/inputs.json"), STAMP}
SKIP_SUFFIXES = {".pyc", ".pyo", ".wav", ".zip", ".env"}   # *.env hold tokens; *.env.example ship


def released_files(root: Path = ROOT) -> List[Path]:
    """Paths relative to root, sorted, of everything that goes in the zip (except the stamp)."""
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        here = Path(dirpath).relative_to(root)
        dirnames[:] = [d for d in dirnames
                       if d not in SKIP_DIRS and not d.startswith(".venv") and not d.endswith(".egg-info")
                       and here / d not in SKIP_PATHS]
        for name in filenames:
            rel = here / name
            if rel not in SKIP_PATHS and rel.suffix not in SKIP_SUFFIXES:
                found.append(rel)
    return sorted(found, key=lambda p: p.as_posix())


def build_id(files: List[Path], root: Path = ROOT) -> str:
    digest = hashlib.sha256()
    for rel in files:
        path = root / rel
        digest.update(rel.as_posix().encode() + b"\0")
        digest.update(b"x" if os.access(path, os.X_OK) else b"-")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()[:8]


def write_stamp(build: str, date: str, root: Path = ROOT) -> None:
    (root / STAMP).write_text(
        '"""Written by tools/make_release.py: which release this is (not part of its own hash)."""\n\n'
        f'BUILD = "{build}"\nBUILD_DATE = "{date}"\n')


def write_zip(files: List[Path], out: Path, root: Path = ROOT) -> None:
    tmp = out.with_suffix(".tmp")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel in [*files, STAMP]:
            info = zipfile.ZipInfo.from_file(root / rel, f"{PREFIX}/{rel.as_posix()}")  # keeps the mode bits
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, (root / rel).read_bytes())
    tmp.replace(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, default=ROOT.parent / f"{PREFIX}.zip")
    ap.add_argument("--print-build", action="store_true", help="print the build ID of the files here and exit")
    args = ap.parse_args(argv)
    files = released_files()
    build = build_id(files)
    if args.print_build:
        print(build)
        return 0
    date = dt.date.today().isoformat()
    write_stamp(build, date)
    write_zip(files, args.out)
    print(f"{args.out}: {len(files) + 1} files, build {build} ({date})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
