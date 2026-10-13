"""lazyfinds: the command line. Download once, choose folders, index, search, see what's in the index."""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from . import code as C
from . import engine as E
from . import paths, rules
from .settings import Settings


def say(*parts):
    print(*parts, flush=True)


def index_path() -> Path:
    return Path(os.environ.get("LAZYFINDS_INDEX") or paths.index_file())


def code_index_path() -> Path:
    return Path(os.environ.get("LAZYFINDS_CODE_INDEX") or paths.code_index_file())


def cmd_setup(args) -> int:
    from . import setup

    todo = setup.missing(args.backend)
    if not todo:
        say("Everything is downloaded.")
        return 0
    say(f"Downloading {len(todo)} files ({sum(i.size for i in todo) / 1e9:.2f} GB) into {paths.app_dir()} …")
    shown = [0.0]

    def progress(name, have, size):
        if time.monotonic() - shown[0] > 1 or have == size:
            shown[0] = time.monotonic()
            print(f"\r  {name}: {have / size:5.1%} of {size / 1e6:.0f} MB   ", end="", flush=True)

    setup.run(progress, args.backend)
    say("\nDone. Every file matches its published checksum.")
    return 0


def cmd_folders(args) -> int:
    s = Settings.load()

    def add(target: list[str], values):
        for v in values or []:
            p = str(Path(v).resolve())
            if p not in target:
                target.append(p)

    add(s.folders, args.add)
    add(s.excluded, args.exclude)
    add(s.code_folders, args.code)
    for v in args.remove or []:
        p = os.path.normcase(str(Path(v).resolve()))
        for name in ("folders", "excluded", "code_folders"):
            setattr(s, name, [x for x in getattr(s, name) if os.path.normcase(x) != p])
    s.save()
    for title, items in (("Folders to search", s.folders), ("Left out", s.excluded), ("Code folders", s.code_folders)):
        if items:
            say(title + ":")
            for f in items:
                say("  " + f)
    if not s.folders:
        say("No folders yet: lazyfinds folders --add <folder>")
    return 0


def _index(store_path: Path, roots: list[Path], excluded: list[str], code: bool, limit, readers) -> None:
    from .indexer import Indexer
    from .store import Store

    from . import extract as X

    store = Store(store_path)
    if code:
        indexer = Indexer(store, readers=readers, kind_of=lambda p: "code" if rules.is_code(p) else None,
                          read=C.reader_for(roots))
    else:
        indexer = Indexer(store, readers=readers, kind_of=rules.kind_of, read=X.read)
    t = time.time()
    changes = indexer.sync(roots, excluded)
    say(f"{'code' if code else 'files'}: {changes.found} found, {changes.added} new, {changes.changed} changed, "
        f"{changes.removed} gone ({time.time() - t:.1f} s)")
    for reason, n in sorted(changes.passed_over.items()):
        say(f"  passed over: {n} × {reason}")
    t = time.time()

    def each(p):
        say(f"[{p.done}/{p.total}] {p.status:7s} {Path(p.path).name[:60]}" + (f" — {p.note}" if p.note else ""))

    try:
        done = indexer.run(each=each, limit=limit)
        say(f"Read {done.done} files in {time.time() - t:.1f} s on {indexer._engine.backend if indexer._engine else '-'}.")
    finally:
        indexer.close()
        store.close()


def cmd_index(args) -> int:
    s = Settings.load()
    roots = [Path(f) for f in (args.folders or s.folders)]
    if not roots and not s.code_folders:
        say("No folders: lazyfinds folders --add <folder>, or lazyfinds index <folder>")
        return 1
    try:
        if roots:
            _index(index_path(), roots, s.excluded, False, args.limit, args.readers)
        if s.code_folders and not args.folders:
            _index(code_index_path(), [Path(f) for f in s.code_folders], s.excluded, True, args.limit, args.readers)
    except KeyboardInterrupt:
        say("Stopped. The rest is read next time.")
        return 130
    return 0


def cmd_search(args) -> int:
    from .search import Searcher, strong_count
    from .store import Store

    query = " ".join(args.query)
    code_query = C.query_of(query)
    store = Store(code_index_path() if code_query is not None else index_path())
    searcher = Searcher(store)
    t = time.time()
    with E.Engine(text_only=True, backend=args.backend) as engine:
        loaded = time.time() - t
        t = time.time()
        if code_query is not None:
            vector = engine.embed_texts([E.code_query_prompt(code_query)])[0]
            hits = C.search(searcher, code_query, vector, limit=args.limit)
        else:
            from . import banglish as B

            conversion = None if args.as_typed or not Settings.load().banglish else B.convert(query)
            vector = engine.embed_query(conversion.text if conversion else query)
            hits = searcher.search(query, vector, kinds=set(args.kind) if args.kind else None, limit=args.limit,
                                   also=conversion)
            if conversion:
                say(f"also searched: {conversion.shown}  (--as-typed searches only as typed)")
        took = time.time() - t
    strong = strong_count(hits)
    say(f"{len(hits)} results ({strong} strong) in {took * 1000:.0f} ms; model on {engine.backend}, "
        f"loaded in {loaded:.1f} s")
    for i, h in enumerate(hits):
        if i == strong and strong < len(hits):
            say("  — weaker matches —")
        place = ""
        if h.start is not None:
            if h.part == "inner_picture":
                place = f" picture {int(h.start)}" + (f" on slide {int(h.stop)}" if h.stop else "")
            elif h.part == "slide":
                place = f" slide {int(h.start)}"
            elif h.kind in ("audio", "video"):
                place = f" @ {int(h.start // 60)}:{int(h.start % 60):02d}"
            elif h.kind == "pdf":
                place = f" p.{int(h.start)}"
            elif h.kind == "code":
                place = f":{int(h.start)}"
        why = " + ".join(x for x in (f"meaning {h.meaning:.1f}" if h.meaning is not None else "",
                                     f"words {h.words:.0%}" if h.words else "") if x)
        say(f"{i + 1:2d}. [{h.kind}] {h.path}{place}   ({why})")
        if args.lines and h.line:
            say("     " + h.line[:160])
    return 0


def cmd_status(args) -> int:
    from . import ocr
    from .store import Store

    for title, path in (("Index", index_path()), ("Code index", code_index_path())):
        if not path.exists():
            continue
        store = Store(path)
        c = store.counts()
        say(f"{title}: {path}")
        say("  files: " + (", ".join(f"{n} {k}" for k, n in sorted(c["kinds"].items())) or "none"))
        say("  status: " + (", ".join(f"{n} {k}" for k, n in sorted(c["status"].items())) or "-"))
        say(f"  vectors: {c['vectors']}  ({store.get('vectors') or 'no model yet'})")
        if args.problems:
            for p, status, note in store.db.execute(
                    "SELECT path, status, note FROM files WHERE status IN ('failed', 'skipped') ORDER BY status, path"):
                say(f"  {status:7s} {p} — {note}")
        store.close()
    ready = [b for b in E.BACKENDS if paths.backend_ready(b)]
    say(f"Model: {'ready' if paths.models_ready() else 'missing (lazyfinds setup)'}; backends: {', '.join(ready) or '-'};"
        f" chosen: {E.choose_backend(Settings.load().backend)}")
    o = ocr.available()
    say(f"Words in pictures: Windows OCR {'yes' if o['windows'] else 'no'}, Bengali (Tesseract) "
        f"{'yes' if o['bengali'] else 'no'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except AttributeError:
            pass
    parser = argparse.ArgumentParser(prog="lazyfinds", description="Find files by what's in them. On-device.")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("setup", help="download the model and llama.cpp once")
    p.add_argument("--backend", choices=["auto", "cuda", "vulkan"], default="auto")
    p.set_defaults(run=cmd_setup)
    p = sub.add_parser("folders", help="choose what to search")
    p.add_argument("--add", nargs="*")
    p.add_argument("--remove", nargs="*")
    p.add_argument("--exclude", nargs="*")
    p.add_argument("--code", nargs="*", help="folders to search with 'code:'")
    p.set_defaults(run=cmd_folders)
    p = sub.add_parser("index", help="read new and changed files")
    p.add_argument("folders", nargs="*")
    p.add_argument("--limit", type=int)
    p.add_argument("--readers", type=int, default=3)
    p.set_defaults(run=cmd_index)
    p = sub.add_parser("search", help="describe what you're looking for ('code: …' searches code)")
    p.add_argument("query", nargs="+")
    p.add_argument("--kind", nargs="*", choices=["screenshot", "image", "pdf", "doc", "audio", "video"])
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--lines", action="store_true", help="show each result's line")
    p.add_argument("--backend", choices=list(E.BACKENDS))
    p.add_argument("--as-typed", action="store_true", help="don't search Banglish words in Bengali")
    p.set_defaults(run=cmd_search)
    p = sub.add_parser("status", help="what's in the index")
    p.add_argument("--problems", action="store_true", help="list skipped and unreadable files")
    p.set_defaults(run=cmd_status)
    args = parser.parse_args(argv)
    return args.run(args)


if __name__ == "__main__":
    raise SystemExit(main())
