"""lazyFinds' own evaluation: queries in English and Bengali over scripts/make_testbed.py's folder. For each query it
prints where the expected file landed, at what place in it, and how far its best piece stood out.

    $env:LAZYFINDS_INDEX = "C:\\tmp\\test.sqlite"
    uv run lazyfinds index C:\\tmp\\testbed
    uv run python scripts/eval.py
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import numpy as np

from lazyfinds import engine as E
from lazyfinds import paths
from lazyfinds.search import Searcher, strong_count
from lazyfinds.store import Store

# (query, expected file, (first, last) place in it or None)
QUERIES = [
    ("a bicycle in the park", "PXL_20260412_081500.jpg", None),
    ("পার্কে সাইকেল", "PXL_20260412_081500.jpg", None),
    ("birthday cake with candles", "PXL_20260412_190233.jpg", None),
    ("জন্মদিনের কেক", "PXL_20260412_190233.jpg", None),
    ("skiing in the snowy mountains", "PXL_20260301_101010.jpg", None),
    ("tropical fish underwater", "PXL_20260519_154422.jpg", None),
    ("a field of sunflowers", "PXL_20260720_120000.jpg", None),
    ("mountain lake at dusk", "PXL_20260601_203000.jpg", None),
    ("not enough money for the transfer", "Screenshot 2026-09-02 093012.png", None),
    ("TXN-58K3Q", "Screenshot 2026-09-02 093012.png", None),
    ("python missing pandas module", "Screenshot 2026-09-05 221147.png", None),
    ("when is my dentist appointment", "Screenshot 2026-09-10 170501.png", None),
    ("বিদ্যুৎ বিল", "Screenshot 2026-09-12 081920.png", None),
    ("881275", "Screenshot 2026-09-12 081920.png", None),
    ("can I work from home", "contract_signed.docx", None),
    ("বাসা থেকে অফিসের কাজ", "contract_signed.docx", None),
    ("chocolate chip cookie recipe", "notes-17.md", None),
    ("growing tomatoes on the roof", "খাতা.txt", None),
    ("টমেটোর চারা", "খাতা.txt", None),
    ("ORD-77A91X", "mail-0412.html", None),
    ("keyboard order confirmation", "mail-0412.html", None),
    ("washing machine won't drain", "manual.pdf", (2, 2)),
    ("how long is the motor warranty", "manual.pdf", (3, 3)),
    ("birth certificate", "scan_0031.pdf", None),
    ("জন্ম নিবন্ধন", "scan_0031.pdf", None),
    ("২০১৮৪৪৭৭১৯", "scan_0031.pdf", None),
    ("how volcanoes form", "REC_0193.mp3", (15, 48)),
    ("tips for saving money", "REC_0193.mp3", (0, 20)),
    ("a steam train at the station", "VID_20260815_1830.mp4", (0, 8)),
    ("penguins in a video", "VID_20260815_1830.mp4", (8, 16)),
    ("fireworks at night", "VID_20260815_1830.mp4", (16, 24)),
    ("আতশবাজি", "VID_20260815_1830.mp4", (16, 24)),
]


def main() -> int:
    store = Store(Path(os.environ.get("LAZYFINDS_INDEX") or paths.index_file()))
    searcher = Searcher(store)
    searcher.load()
    first = top3 = placed = 0
    gaps = []
    with E.Engine(text_only=True) as engine:
        for query, name, place in QUERIES:
            vector = engine.embed_query(query)
            t = time.perf_counter()
            hits = searcher.search(query, vector)
            ms = (time.perf_counter() - t) * 1000
            names = [Path(h.path).name for h in hits]
            rank = names.index(name) + 1 if name in names else None
            first += rank == 1
            top3 += bool(rank and rank <= 3)
            # How far the expected file's best piece stood out, and the best other file's.
            stand = searcher.standing(searcher.vectors @ vector)
            by_file: dict[str, float] = {}
            for row in range(len(stand)):
                f = store.db.execute("SELECT path FROM files WHERE id = ?", (int(searcher.file_ids[row]),)).fetchone()[0]
                by_file[Path(f).name] = max(by_file.get(Path(f).name, -99), float(stand[row]))
            mine = by_file.get(name, float("nan"))
            other = max((v for k, v in by_file.items() if k != name), default=float("nan"))
            gaps.append((mine, other))
            where = ""
            if rank and place:
                at = hits[rank - 1].start
                ok = at is not None and place[0] <= at <= place[1]
                placed += ok
                where = f" at {at:g}{'' if ok else ' WRONG PLACE'}"
            mark = "✓" if rank == 1 else ("~" if rank else "✗")
            print(f"{mark} {query!r:40} → {rank or '-'}/{len(hits)} ({strong_count(hits)} strong){where}  "
                  f"meaning {mine:4.1f} vs {other:4.1f}  {ms:.0f} ms" + ("" if rank == 1 else f"  top: {names[:3]}"))
    mine = np.array([g[0] for g in gaps])
    other = np.array([g[1] for g in gaps])
    print(f"\nfirst: {first}/{len(QUERIES)}   top 3: {top3}/{len(QUERIES)}   right place: {placed}/"
          f"{sum(1 for q in QUERIES if q[2])}")
    print(f"meaning of the expected file: min {np.nanmin(mine):.1f}, median {np.nanmedian(mine):.1f}; "
          f"best other file: median {np.nanmedian(other):.1f}, max {np.nanmax(other):.1f}")
    return 0 if first == len(QUERIES) else 1


if __name__ == "__main__":
    for s in (sys.stdout, sys.stderr):
        s.reconfigure(encoding="utf-8")
    raise SystemExit(main())
