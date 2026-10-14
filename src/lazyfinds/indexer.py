"""Indexing: walk the chosen folders, notice what's new, changed or gone, read, embed, write.

A few worker threads read files (decoding, ffmpeg, OCR, rendering) while this thread embeds on the GPU and writes, so
the GPU rarely waits. A file's pieces are written in one transaction: it's searchable whole or not at all.
"""

from __future__ import annotations

import os
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import engine as E
from . import extract as X
from . import rules
from .store import Store


@dataclass
class Changes:
    found: int = 0
    added: int = 0
    changed: int = 0
    removed: int = 0
    passed_over: dict[str, int] = field(default_factory=dict)


@dataclass
class Progress:
    done: int = 0
    total: int = 0
    path: str = ""
    status: str = ""
    note: str | None = None


Kinder = Callable[[Path], "str | None"]

# Python's words when a thread pool was shut down under a file (a quit while indexing). Up to 0.1.4 such a file was
# stored as failed and never read again; it is read again once.
CUT_SHORT = "cannot schedule new futures after"


def walk(roots: list[Path], excluded: list[str], kind_of: Kinder) -> tuple[dict[str, tuple], dict[str, int]]:
    """Every file under `roots` that `kind_of` gives a kind: path -> (kind, size, mtime), and how many items were
    passed over, by reason."""
    found: dict[str, tuple] = {}
    passed: dict[str, int] = {}

    def note(reason: str):
        passed[reason] = passed.get(reason, 0) + 1

    def visit(folder: str, in_project: bool):
        try:
            entries = list(os.scandir(folder))
        except OSError:
            note("can't be read")
            return
        in_project = in_project or rules.is_project({e.name for e in entries})
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    reason = rules.folder_verdict(entry, in_project, excluded)
                    if reason:
                        note(reason)
                    else:
                        visit(entry.path, in_project)
                    continue
                if not entry.is_file(follow_symlinks=False):
                    continue
                kind = kind_of(Path(entry.path))
                if not kind:
                    continue
                st = entry.stat(follow_symlinks=False)
                reason = rules.file_verdict(st)
                if reason:
                    note(reason)
                    continue
                found[entry.path] = (kind, st.st_size, st.st_mtime)
            except OSError:
                note("can't be read")

    for root in roots:
        if root.is_dir():
            visit(str(root), False)
        elif root.is_file() and (kind := kind_of(root)):
            st = root.stat()
            found[str(root)] = (kind, st.st_size, st.st_mtime)
    return found, passed


def _inside(path: str, folders: list[str]) -> bool:
    p = os.path.normcase(path)
    return any(p == f or p.startswith(f + os.sep) for f in folders)


class Indexer:
    def __init__(self, store: Store, readers: int = 3, kind_of: Kinder = rules.kind_of,
                 read: Callable = X.read, backend: str | None = None):
        self.store = store
        self.readers = readers
        self.kind_of = kind_of
        self.read = read
        self.backend = backend
        self._engine: E.Engine | None = None

    def close(self):
        if self._engine:
            self._engine.close()
            self._engine = None

    def sync(self, roots: list[Path], excluded: list[str] | None = None) -> Changes:
        """Brings the file list in line with the folders. A folder that isn't there right now (an unplugged drive, an
        offline share) keeps its files: they come back with it. A folder taken off the list loses them."""
        found, passed = walk(roots, excluded or [], self.kind_of)
        changes = Changes(found=len(found), passed_over=passed)
        known = self.store.all_files()
        known_lower = {p.lower(): p for p in known}
        for path, (kind, size, mtime) in found.items():
            old = known.get(path) or known.get(known_lower.get(path.lower(), ""))
            if old is None:
                self.store.add_file(path, kind, size, mtime)
                changes.added += 1
            elif old[1] != size or abs(old[2] - mtime) > 0.001 or old[3] != kind:
                self.store.file_changed(old[0], kind, size, mtime)
                changes.changed += 1
        present = [os.path.normcase(str(r)).rstrip("\\/") for r in roots if r.exists()]
        absent = [os.path.normcase(str(r)).rstrip("\\/") for r in roots if not r.exists()]
        found_lower = {p.lower() for p in found}
        gone = [row[0] for path, row in known.items()
                if path.lower() not in found_lower and not _inside(path, absent)]
        if gone:
            self.store.remove(gone)
            changes.removed = len(gone)
        return changes

    def engine(self) -> E.Engine:
        if self._engine is not None and self._engine.outdated:
            self._engine.close()   # a GPU became ready while this one ran on the processor
        if self._engine is None or not self._engine.alive:
            self._engine = E.Engine(backend=self.backend)
        return self._engine

    def run(self, stop: Callable[[], bool] = lambda: False, each: Callable[[Progress], None] = lambda p: None,
            limit: int | None = None) -> Progress:
        """Reads and embeds every new file. Stops between files when `stop()` says so; the rest stay new."""
        self.store.use_model(E.VECTOR_SOURCE)
        jobs = self.store.to_read(limit)
        progress = Progress(total=len(jobs))
        if not jobs:
            return progress
        self.engine()
        with ThreadPoolExecutor(self.readers, thread_name_prefix="read") as pool:
            queue = list(jobs)
            ahead: list[tuple[tuple, Future]] = []
            while queue or ahead:
                while queue and len(ahead) < self.readers * 2 and not stop():
                    job = queue.pop(0)
                    ahead.append((job, pool.submit(self._read, job)))
                if not ahead:
                    break
                (file_id, path, kind), future = ahead.pop(0)
                try:
                    status, note = self._write(self.engine(), file_id, future.result())
                except E.EngineError:
                    raise
                except Exception as error:   # one odd file mustn't stop the rest
                    status, note = "failed", f"{type(error).__name__}: {error}"[:300]
                    self.store.mark(file_id, status, note)
                if status == "failed" and stop():
                    # Stopping (a quit, a pause) can cut a file short; that isn't the file's fault: read it next time.
                    self.store.mark(file_id, "new")
                    for _, f in ahead:
                        f.cancel()
                    break
                progress.done += 1
                progress.path, progress.status, progress.note = path, status, note
                each(progress)
                if stop():
                    for _, f in ahead:
                        f.cancel()
                    break
        return progress

    def _read(self, job):
        _, path, kind = job
        try:
            return self.read(Path(path), kind)
        except Exception as error:
            return error

    def _write(self, engine: E.Engine, file_id: int, reading) -> tuple[str, str | None]:
        if isinstance(reading, X.Skip):
            self.store.mark(file_id, "skipped", str(reading))
            return "skipped", str(reading)
        if isinstance(reading, Exception):
            note = str(reading) if isinstance(reading, X.Unreadable) else f"{type(reading).__name__}: {reading}"
            self.store.mark(file_id, "failed", note[:300])
            return "failed", note
        texts = [i for i, p in enumerate(reading.pieces) if p.embed and p.embed[0] == "text"]
        media = [i for i, p in enumerate(reading.pieces) if p.embed and p.embed[0] == "media"]
        vectors: dict[int, object] = {}
        if texts:
            vectors.update(zip(texts, engine.embed_texts([reading.pieces[i].embed[1] for i in texts])))
        if media:
            vectors.update(zip(media, engine.embed_media([reading.pieces[i].embed[1] for i in media])))
        if not engine.alive:
            raise E.EngineError("llama-server stopped")
        failed = sum(isinstance(v, Exception) for v in vectors.values())
        if vectors and failed == len(vectors) and not any(p.body for p in reading.pieces):
            first = next(v for v in vectors.values() if isinstance(v, Exception))
            self.store.mark(file_id, "failed", f"model: {first}"[:300])
            return "failed", str(first)
        with self.store.transaction():
            for i, p in enumerate(reading.pieces):
                v = vectors.get(i)
                self.store.add_piece(file_id, p.part, p.source, p.start, p.stop, p.line, p.body,
                                     None if v is None or isinstance(v, Exception) else v, p.words)
            note = reading.note or (f"{failed} pieces had no vector" if failed else None)
            self.store.mark(file_id, "read", note, reading.meta)
        parts: dict[str, int] = {}
        for p in reading.pieces:
            parts[p.part] = parts.get(p.part, 0) + 1
        return "read", ", ".join(f"{n} {k}" for k, n in parts.items())
