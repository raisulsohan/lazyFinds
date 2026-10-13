"""lazyFinds' own evaluation: queries in English and Bengali over scripts/make_testbed.py's folder, Banglish ones
(Bengali typed in Latin letters) over the same folder, and Banglish ones in Name mode over a small tree of Bengali
names. For each query it prints where the expected file landed, at what place in it, and how far its best piece stood
out. Every query runs as the panel runs it, Banglish on; the Banglish ones also as typed, for the difference; and the
time Banglish adds to a query is measured.

    $env:LAZYFINDS_INDEX = "C:\\tmp\\test.sqlite"
    uv run lazyfinds index C:\\tmp\\testbed
    uv run python scripts/eval.py              # --as-typed: everything without Banglish
"""

from __future__ import annotations

import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

from lazyfinds import banglish as B
from lazyfinds import engine as E
from lazyfinds import names as N
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


# Bengali typed in Latin letters, alone and with English: (query, expected file, place in it or None). The first seven
# were measured before Banglish search existed; the rest are other spellings of the same words, other words, and
# Banglish mixed with English.
BANGLISH = [
    ("biddut bil", "Screenshot 2026-09-12 081920.png", None),
    ("tomato chara lagano", "খাতা.txt", None),
    ("parke cycle", "PXL_20260412_081500.jpg", None),
    ("jonmo nibondhon", "scan_0031.pdf", None),
    ("jonmodiner cake", "PXL_20260412_190233.jpg", None),
    ("atoshbaji", "VID_20260815_1830.mp4", (16, 24)),
    ("basha theke office er kaj", "contract_signed.docx", None),
    ("atashbaji", "VID_20260815_1830.mp4", (16, 24)),
    ("atoshbazi", "VID_20260815_1830.mp4", (16, 24)),
    ("bidyut bil", "Screenshot 2026-09-12 081920.png", None),
    ("jonmo nibondhan", "scan_0031.pdf", None),
    ("jonmodiner kek", "PXL_20260412_190233.jpg", None),
    ("parke saikel", "PXL_20260412_081500.jpg", None),
    ("tometo chara", "খাতা.txt", None),
    ("chhader bagane tomato", "খাতা.txt", None),
    ("joiba sar", "খাতা.txt", None),
    ("rate atoshbaji", "VID_20260815_1830.mp4", (16, 24)),
    ("penguin er video", "VID_20260815_1830.mp4", (8, 16)),
    ("statione train", "VID_20260815_1830.mp4", (0, 8)),
    ("pahare ski", "PXL_20260301_101010.jpg", None),
    ("samudrer niche mach", "PXL_20260519_154422.jpg", None),
    ("surjomukhi ful", "PXL_20260720_120000.jpg", None),
    ("basay boshe kaj", "contract_signed.docx", None),
    ("taka transfer hoyni", "Screenshot 2026-09-02 093012.png", None),
    ("dater daktar", "Screenshot 2026-09-10 170501.png", None),
    ("biddut biler screenshot", "Screenshot 2026-09-12 081920.png", None),
    ("agneyogiri kivabe toiri hoy", "REC_0193.mp3", (15, 48)),
    ("taka shonchoyer upay", "REC_0193.mp3", (0, 20)),
    ("washing machine er pani ber hocche na", "manual.pdf", (2, 2)),
    ("motor er warranty koto bochor", "manual.pdf", (3, 3)),
    ("chocolate chip cookie banano", "notes-17.md", None),
    ("keyboard er order", "mail-0412.html", None),
]

# Name mode: names on a PC, Bengali and English, and Banglish and English queries for them.
NAME_TREE = ["Documents/কেমন আছো.docx", "Documents/খাতা.txt", "Documents/জন্ম নিবন্ধন সনদ.pdf",
             "Documents/পরীক্ষার রুটিন.pdf", "Documents/বাজারের তালিকা.xlsx", "Documents/contract_signed.docx",
             "Documents/report.txt", "Documents/bill_2024.pdf", "Documents/mobile_backup.zip",
             "Photos/বিদ্যুৎ বিল সেপ্টেম্বর.png", "Photos/birthday cake.jpg", "Photos/ছবি/", "Videos/আতশবাজি ২০২৬.mp4",
             "Music/প্রিয় গান.mp3", "Music/rong.mp3"]
NAME_QUERIES = [
    ("kemon", "কেমন আছো.docx"),
    ("kemon acho", "কেমন আছো.docx"),
    ("khata", "খাতা.txt"),
    ("jonmo nibondhon", "জন্ম নিবন্ধন সনদ.pdf"),
    ("biddut bil", "বিদ্যুৎ বিল সেপ্টেম্বর.png"),
    ("atoshbaji", "আতশবাজি ২০২৬.mp4"),
    ("priyo gaan", "প্রিয় গান.mp3"),
    ("bajarer talika", "বাজারের তালিকা.xlsx"),
    ("chobi", "ছবি"),
    ("porikkhar rutin", "পরীক্ষার রুটিন.pdf"),
    # names in Latin letters stay first for their own queries
    ("report", "report.txt"),
    ("birthday cake", "birthday cake.jpg"),
    ("contract", "contract_signed.docx"),
    ("bill", "bill_2024.pdf"),
    ("rong", "rong.mp3"),
]


def search(searcher: Searcher, engine: E.Engine, query: str, banglish: bool):
    """As the panel searches: in its Bengali form when it has Banglish words. Returns the hits, the conversion, the
    vector, and the milliseconds spent converting, embedding and ranking."""
    t = time.perf_counter()
    conversion = B.convert(query) if banglish else None
    convert_ms = (time.perf_counter() - t) * 1000
    t = time.perf_counter()
    vector = engine.embed_texts([E.query_prompt(conversion.text if conversion else query)])[0]
    embed_ms = (time.perf_counter() - t) * 1000
    t = time.perf_counter()
    hits = searcher.search(query, vector, also=conversion)
    rank_ms = (time.perf_counter() - t) * 1000
    return hits, conversion, vector, (convert_ms, embed_ms, rank_ms)


def run_set(title: str, queries, searcher, engine, store, banglish: bool, quiet: bool = False) -> dict:
    """Prints a line per query (unless quiet)."""
    first = top3 = placed = 0
    gaps, missed = [], []
    by_row = [Path(store.db.execute("SELECT path FROM files WHERE id = ?", (int(f),)).fetchone()[0]).name
              for f in searcher.file_ids]
    if not quiet:
        print(f"\n{title}")
    for query, name, place in queries:
        hits, conversion, vector, ms = search(searcher, engine, query, banglish)
        names = [Path(h.path).name for h in hits]
        rank = names.index(name) + 1 if name in names else None
        first += rank == 1
        top3 += bool(rank and rank <= 3)
        # How far the expected file's best piece stood out, and the best other file's.
        stand = searcher.standing(searcher.vectors @ vector)
        by_file: dict[str, float] = {}
        for row, f in enumerate(by_row):
            by_file[f] = max(by_file.get(f, -99), float(stand[row]))
        mine = by_file.get(name, float("nan"))
        other = max((v for k, v in by_file.items() if k != name), default=float("nan"))
        gaps.append((mine, other))
        where = ""
        if rank and place:
            at = hits[rank - 1].start
            ok = at is not None and place[0] <= at <= place[1]
            placed += ok
            where = f" at {at:g}{'' if ok else ' WRONG PLACE'}"
        if rank != 1:
            missed.append(query)
        if quiet:
            continue
        mark = "✓" if rank == 1 else ("~" if rank else "✗")
        also = f"  [{conversion.shown}]" if conversion else ""
        print(f"{mark} {query!r:40} → {rank or '-'}/{len(hits)} ({strong_count(hits)} strong){where}  "
              f"meaning {mine:4.1f} vs {other:4.1f}  {sum(ms):.0f} ms{also}" +
              ("" if rank == 1 else f"  top: {names[:3]}"))
    with_place = sum(1 for q in queries if q[2])
    if not quiet:
        mine = np.array([g[0] for g in gaps])
        other = np.array([g[1] for g in gaps])
        print(f"first: {first}/{len(queries)}   top 3: {top3}/{len(queries)}   right place: {placed}/{with_place}")
        print(f"meaning of the expected file: min {np.nanmin(mine):.1f}, median {np.nanmedian(mine):.1f}; "
              f"best other file: median {np.nanmedian(other):.1f}, max {np.nanmax(other):.1f}")
    return {"first": first, "top3": top3, "placed": placed, "with_place": with_place, "missed": missed}


def run_names(banglish: bool) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        os.environ["LAZYFINDS_NAMES"] = str(root / "names.sqlite")
        for rel in NAME_TREE:
            p = root / "pc" / rel
            if rel.endswith("/"):
                p.mkdir(parents=True, exist_ok=True)
            else:
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text("x", encoding="utf-8")
        names = N.Names()
        names.scan(drives=[str(root / "pc")])
        first, missed = 0, []
        print(f"\nName mode, Banglish {'on' if banglish else 'off'}")
        for query, name in NAME_QUERIES:
            t = time.perf_counter()
            conversion = B.convert(query) if banglish else None
            found = names.search(query, also=B.name_queries(query, conversion) if conversion else ())
            ms = (time.perf_counter() - t) * 1000
            got = [f.name for f in found]
            rank = got.index(name) + 1 if name in got else None
            first += rank == 1
            if rank != 1:
                missed.append(query)
            mark = "✓" if rank == 1 else ("~" if rank else "✗")
            also = f"  [{conversion.shown}]" if conversion else ""
            print(f"{mark} {query!r:24} → {rank or '-'}/{len(got)}  {ms:.1f} ms{also}" +
                  ("" if rank == 1 else f"  top: {got[:3]}"))
        print(f"first: {first}/{len(NAME_QUERIES)}")
        return {"first": first, "missed": missed}


def main() -> int:
    banglish = "--as-typed" not in sys.argv
    store = Store(Path(os.environ.get("LAZYFINDS_INDEX") or paths.index_file()))
    searcher = Searcher(store)
    searcher.load()
    B.warm()
    state = "on" if banglish else "off"
    with E.Engine(text_only=True) as engine:
        engine.embed_texts([E.query_prompt("warm up"), E.query_prompt("warm up again")])
        base = run_set(f"English and Bengali, Banglish {state}", QUERIES, searcher, engine, store, banglish)
        mixed = run_set(f"Banglish, Banglish {state}", BANGLISH, searcher, engine, store, banglish)
        before = run_set("", BANGLISH, searcher, engine, store, False, quiet=True) if banglish else mixed
        # What Banglish adds to a query's time: converting it (spellings not cached yet), and ranking with the
        # spellings as alternatives. Either way the model reads one prompt, so embedding costs the same.
        added: dict[bool, list[float]] = {True: [], False: []}
        for query, _, _ in BANGLISH + QUERIES:
            B.spellings.cache_clear()
            t = time.perf_counter()
            conversion = B.convert(query)
            convert_ms = (time.perf_counter() - t) * 1000
            vector = engine.embed_query(conversion.text if conversion else query)
            ranks = []
            for also in (conversion, None):
                t = time.perf_counter()
                for _ in range(5):
                    searcher.search(query, vector, also=also)
                ranks.append((time.perf_counter() - t) * 1000 / 5)
            added[conversion is not None].append(convert_ms + ranks[0] - ranks[1])
    names_on = run_names(banglish)
    names_off = run_names(False) if banglish else names_on

    print("\nSummary")
    print(f"  English and Bengali: {base['first']}/{len(QUERIES)} first, {base['placed']}/{base['with_place']} at the "
          f"right place")
    for title, r in [("Banglish as typed", before)] + ([("Banglish converted", mixed)] if banglish else []):
        print(f"  {title + ':':20} {r['first']}/{len(BANGLISH)} first, {r['top3']}/{len(BANGLISH)} in the top 3, "
              f"{r['placed']}/{r['with_place']} at the right place")
    print(f"  Name mode:           as typed {names_off['first']}/{len(NAME_QUERIES)} first, converted "
          f"{names_on['first']}/{len(NAME_QUERIES)} first")
    if added[True]:
        print(f"  time Banglish adds to a converted query: median {statistics.median(added[True]):.1f} ms, "
              f"max {max(added[True]):.1f} ms; to another query (looking, not converting): median "
              f"{statistics.median(added[False]):.1f} ms, max {max(added[False]):.1f} ms")
    if mixed["missed"]:
        print(f"  not first (Banglish): {mixed['missed']}")
    if names_on["missed"]:
        print(f"  not first (Name mode): {names_on['missed']}")
    return 0 if base["first"] == len(QUERIES) and base["placed"] == base["with_place"] else 1


if __name__ == "__main__":
    for s in (sys.stdout, sys.stderr):
        s.reconfigure(encoding="utf-8")
    raise SystemExit(main())
