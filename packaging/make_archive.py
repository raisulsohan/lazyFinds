"""Packs dist/lazyFinds into packaging/Output/lazyFinds-<version>-app.7z (LZMA2) for the web installer, and prints the
defines Inno Setup needs: its SHA-256 and its unpacked size.

    uv run --no-sync python packaging/make_archive.py 0.1.4
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import py7zr

ROOT = Path(__file__).resolve().parent.parent


def main(version: str) -> None:
    source = ROOT / "dist" / "lazyFinds"
    out = ROOT / "packaging" / "Output" / f"lazyFinds-{version}-app.7z"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.unlink(missing_ok=True)
    files = sorted(p for p in source.rglob("*") if p.is_file())
    unpacked = sum(p.stat().st_size for p in files)
    with py7zr.SevenZipFile(out, "w", filters=[{"id": py7zr.FILTER_LZMA2, "preset": 9}]) as archive:
        for path in files:
            archive.write(path, str(path.relative_to(source)))
    digest = hashlib.sha256(out.read_bytes()).hexdigest()
    print(f"/DArchiveSha={digest} /DArchiveUnpacked={unpacked} /DArchiveName={out.name}")
    print(f"{out}  {out.stat().st_size / 1e6:.1f} MB (unpacks to {unpacked / 1e6:.0f} MB)", file=sys.stderr)


if __name__ == "__main__":
    main(sys.argv[1])
