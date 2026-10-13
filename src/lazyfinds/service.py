"""The app's two background workers, without any UI: indexing (with a folder watcher) and searching.

Indexing keeps two indexes in step with the folders: the main one, and the code one for the folders chosen for code.
It runs at start, a few seconds after files change, and every half hour. Its model (text + pictures + sound, ~1 GB)
runs only while there is something to read. Searching keeps a text-only model (~0.3 GB) while the panel is in use and
lets it go after ten minutes unused. Each worker has its own SQLite connection.
"""

from __future__ import annotations

import logging
import os
import queue
import threading
import time
from pathlib import Path
from typing import Callable

from . import code as C
from . import engine as E
from . import extract as X
from . import paths, rules
from .indexer import Indexer, Progress
from .search import Searcher
from .settings import Settings
from .store import Store

log = logging.getLogger("lazyfinds")


def index_path() -> Path:
    return Path(os.environ.get("LAZYFINDS_INDEX") or paths.index_file())


def code_index_path() -> Path:
    return Path(os.environ.get("LAZYFINDS_CODE_INDEX") or paths.code_index_file())


class IndexService:
    QUIET = 3.0          # seconds without changes before a pass
    PERIOD = 30 * 60

    def __init__(self, on_status: Callable[[dict], None] = lambda s: None):
        self.on_status = on_status
        self.paused = False
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._due = time.monotonic()
        self._observer = None
        self.status = {"state": "idle", "done": 0, "total": 0, "path": "", "error": None, "backend": None}
        self._thread = threading.Thread(target=self._loop, name="indexing", daemon=True)

    def start(self):
        self._thread.start()
        self.rewatch()

    def stop(self):
        self._stop.set()
        self._wake.set()
        if self._observer:
            self._observer.stop()

    def request(self, delay: float = 0.0):
        self._due = time.monotonic() + delay   # each change pushes the pass back: it runs once things go quiet
        self._wake.set()

    def pause(self, paused: bool):
        self.paused = paused
        self._emit(state="paused" if paused else "idle")
        if not paused:
            self.request()

    def _emit(self, **changes):
        self.status.update(changes)
        try:
            self.on_status(dict(self.status))
        except Exception:
            log.exception("status callback")

    def rewatch(self):
        from watchdog.events import FileSystemEventHandler
        from watchdog.observers import Observer

        if self._observer:
            self._observer.stop()
            self._observer = None
        s = Settings.load()
        folders = [f for f in dict.fromkeys(s.folders + s.code_folders) if os.path.isdir(f)]
        if not s.watch or not folders:
            return
        service = self

        class Changed(FileSystemEventHandler):
            def on_any_event(self, event):
                if event.is_directory and event.event_type == "modified":
                    return
                if _worth_a_look(getattr(event, "dest_path", "") or event.src_path):
                    service.request(IndexService.QUIET)

        observer = Observer()
        for folder in folders:
            try:
                observer.schedule(Changed(), folder, recursive=True)
            except OSError:
                log.warning("can't watch %s", folder)
        observer.daemon = True
        observer.start()
        self._observer = observer

    def _loop(self):
        while not self._stop.is_set():
            wait = self._due - time.monotonic()
            if wait > 0:
                self._wake.wait(min(wait, self.PERIOD))
                self._wake.clear()
                continue
            self._due = time.monotonic() + self.PERIOD
            if self.paused:
                continue
            try:
                self._pass()
            except E.EngineError as error:
                log.error("model: %s", error)
                self._emit(state="error", error=str(error))
                self._due = time.monotonic() + 60
            except Exception as error:
                log.exception("indexing pass")
                self._emit(state="error", error=f"{type(error).__name__}: {error}")

    def _pass(self):
        s = Settings.load()
        jobs = []
        if s.folders:
            jobs.append((index_path(), [Path(f) for f in s.folders], rules.kind_of, X.read))
        if s.code_folders:
            roots = [Path(f) for f in s.code_folders]
            jobs.append((code_index_path(), roots, lambda p: "code" if rules.is_code(p) else None,
                         C.reader_for(roots)))
        if not jobs:
            self._emit(state="idle", done=0, total=0)
            return
        for path, roots, kind_of, read in jobs:
            if self.paused or self._stop.is_set():
                break
            store = Store(path)
            indexer = Indexer(store, kind_of=kind_of, read=read, backend=s.backend)
            try:
                self._emit(state="scanning", error=None)
                changes = indexer.sync(roots, s.excluded)
                log.info("%s: %d found, %d new, %d changed, %d gone", path.name, changes.found, changes.added,
                         changes.changed, changes.removed)
                waiting = len(store.to_read())
                if waiting:
                    self._emit(state="indexing", done=0, total=waiting)

                    def each(p: Progress):
                        if p.status == "failed":
                            log.warning("unreadable: %s — %s", p.path, p.note)
                        backend = indexer._engine.backend if indexer._engine else None
                        self._emit(state="indexing", done=p.done, total=p.total, path=p.path, backend=backend)

                    indexer.run(stop=lambda: self.paused or self._stop.is_set(), each=each)
            finally:
                indexer.close()   # the model quits when there's nothing left to read
                store.close()
        self._emit(state="paused" if self.paused else "idle", path="")


def _worth_a_look(path: str) -> bool:
    """A change somewhere the walk goes (not our own files, not a hidden or tool folder)."""
    if os.path.normcase(path).startswith(os.path.normcase(str(paths.app_dir()))):
        return False
    return not any(part.startswith((".", "$")) or part.lower() in rules.NEVER_FOLDERS
                   for part in Path(path).parts[1:-1])


class QueryService:
    """Searches off the UI thread; the newest query wins. Each query answers twice: words at once (no model), then
    words and meaning. "code: …" searches the code index."""

    IDLE = 10 * 60

    def __init__(self, on_results: Callable[[int, str, list, bool], None]):
        self.on_results = on_results
        self._queue: queue.Queue = queue.Queue()
        self._engine: E.Engine | None = None
        self._engine_lock = threading.Lock()
        self._used = time.monotonic()
        self._searchers: dict[str, Searcher] = {}
        self.engine_error: str | None = None
        self.backend: str | None = None

    def start(self):
        threading.Thread(target=self._loop, name="search", daemon=True).start()
        threading.Thread(target=self._idle, name="search-idle", daemon=True).start()

    def submit(self, seq: int, text: str, kinds: set[str] | None = None):
        self._queue.put((seq, text, kinds))

    def warm(self):
        threading.Thread(target=self._get_engine, name="search-warm", daemon=True).start()

    def _get_engine(self) -> E.Engine | None:
        with self._engine_lock:
            self._used = time.monotonic()
            if self._engine is None or not self._engine.alive:
                try:
                    self._engine = E.Engine(text_only=True)
                    self.backend = self._engine.backend
                    self.engine_error = None
                except Exception as error:
                    self.engine_error = str(error)
                    log.error("search model: %s", error)
                    self._engine = None
            return self._engine

    def _idle(self):
        while True:
            time.sleep(30)
            with self._engine_lock:
                if self._engine and time.monotonic() - self._used > self.IDLE:
                    self._engine.close()
                    self._engine = None

    def _searcher(self, code: bool) -> Searcher:
        key = "code" if code else "main"
        if key not in self._searchers:
            self._searchers[key] = Searcher(Store(code_index_path() if code else index_path()))
        searcher = self._searchers[key]
        searcher.refresh()
        return searcher

    def body(self, piece: int, code: bool = False) -> str | None:
        return Searcher(Store(code_index_path() if code else index_path())).body(piece)

    def _loop(self):
        while True:
            item = self._queue.get()
            while not self._queue.empty():
                item = self._queue.get_nowait()
            seq, text, kinds = item
            try:
                self._run(seq, text, kinds)
            except Exception:
                log.exception("search %r", text)
                self.on_results(seq, text, [], True)

    def _run(self, seq: int, text: str, kinds):
        code_query = C.query_of(text)
        searcher = self._searcher(code_query is not None)
        query = code_query if code_query is not None else text
        if not query.strip():
            self.on_results(seq, text, [], True)
            return
        find = (lambda v: C.search(searcher, query, v, typing=True)) if code_query is not None else \
            (lambda v: searcher.search(query, v, kinds=kinds, typing=True))
        self.on_results(seq, text, find(None), False)
        if not self._queue.empty():
            return
        engine = self._get_engine()
        if engine is None:
            self.on_results(seq, text, find(None), True)
            return
        prompt = E.code_query_prompt(query) if code_query is not None else E.query_prompt(query)
        vector = engine.embed_texts([prompt])[0]
        self._used = time.monotonic()
        if self._queue.empty():
            self.on_results(seq, text, find(vector), True)

    def close(self):
        with self._engine_lock:
            if self._engine:
                self._engine.close()
                self._engine = None
