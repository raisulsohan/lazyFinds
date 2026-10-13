"""Name search: type any part of a file or folder name and see every match on the PC at once.

The names of everything on the PC's fixed drives are kept in their own SQLite file (index/names.sqlite): each folder's
path once, and each entry's name, size and date. For searching they're held in memory as one lower-cased string with a
line per entry, so finding a piece of a name is one str.find (C speed) across the whole PC: a few milliseconds for a
million names. Spaces separate pieces that must all be in the name, as in Everything.

At start the saved list loads at once; a background walk (about a minute, no admin rights) brings it up to date, and
watching the drives keeps it there. Only names are read here, never what's inside files.
"""

from __future__ import annotations

import bisect
import ctypes
import logging
import os
import sqlite3
import stat
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import paths, rules

log = logging.getLogger("lazyfinds")
SKIP = {"$recycle.bin", "system volume information", "$windows.~bt", "$windows.~ws", "config.msi"}


def fixed_drives() -> list[str]:
    k = ctypes.windll.kernel32
    mask = k.GetLogicalDrives()
    drives = [f"{chr(65 + i)}:\\" for i in range(26) if mask >> i & 1]
    return [d for d in drives if k.GetDriveTypeW(d) == 3]   # DRIVE_FIXED


def names_file() -> Path:
    return Path(os.environ.get("LAZYFINDS_NAMES") or (paths.app_dir() / "index" / "names.sqlite"))


@dataclass
class Found:
    path: str
    name: str
    is_dir: bool
    size: int
    modified: float
    marks: list[tuple[int, int]]   # where the typed pieces are in `name`


class Names:
    """The PC's names in memory, searchable at any length of query."""

    def __init__(self):
        self.lock = threading.RLock()
        self.folders: list[str] = []        # folder id -> path
        self.parent: list[int] = []         # entry -> folder id
        self.name: list[str] = []           # entry -> name as written
        self.size: list[int] = []
        self.mtime: list[float] = []
        self.is_dir: bytearray = bytearray()
        self.blob = "\n"                    # every name, lower-cased, each between line breaks
        self.starts: list[int] = []         # entry -> where its name starts in blob
        self.in_blob = 0                    # entries before this are in blob; ones added since aren't yet
        self.gone: set[int] = set()         # entries removed since the last full walk
        self.loaded_at = 0.0
        self.scanning = False
        self.count = 0

    # -- building

    def _rebuild_blob(self):
        """One string of every name. Entries removed since the last walk stay out of searches through `gone`."""
        starts, pos = [], 1
        for n in self.name:
            starts.append(pos)
            pos += len(n) + 1
        self.blob = "\n" + "\n".join(n.lower() for n in self.name) + "\n"
        self.starts = starts
        self.in_blob = len(self.name)
        self.count = len(self.name) - len(self.gone)

    def load(self) -> bool:
        """The saved list, if there is one. True when it loaded."""
        f = names_file()
        if not f.exists():
            return False
        try:
            db = sqlite3.connect(f)
            folders = [row[0] for row in db.execute("SELECT path FROM folders ORDER BY id")]
            rows = db.execute("SELECT folder, name, is_dir, size, mtime FROM entries ORDER BY id").fetchall()
            db.close()
        except sqlite3.Error:
            return False
        with self.lock:
            self.folders = folders
            self.gone = set()
            self.__dict__.pop("_folder_index", None)
            self.__dict__.pop("_system", None)
            self.parent = [r[0] for r in rows]
            self.name = [r[1] for r in rows]
            self.is_dir = bytearray(r[2] for r in rows)
            self.size = [r[3] for r in rows]
            self.mtime = [r[4] for r in rows]
            self._rebuild_blob()
            self.loaded_at = f.stat().st_mtime
        return True

    def scan(self, drives: list[str] | None = None, excluded: list[str] | None = None,
             each: Callable[[int], None] = lambda n: None) -> int:
        """Walks the drives and replaces the list (searches keep using the old one until the new one is ready)."""
        self.scanning = True
        try:
            folders: list[str] = []
            parent: list[int] = []
            name: list[str] = []
            is_dir = bytearray()
            size: list[int] = []
            mtime: list[float] = []
            out = [os.path.normcase(e.rstrip("\\/")) for e in excluded or []]
            for drive in drives or fixed_drives():
                stack = [drive]
                while stack:
                    folder = stack.pop()
                    try:
                        entries = os.scandir(folder)
                    except OSError:
                        continue
                    folder_id = len(folders)
                    folders.append(folder)
                    with entries:
                        for e in entries:
                            try:
                                st = e.stat(follow_symlinks=False)
                            except OSError:
                                continue
                            directory = stat.S_ISDIR(st.st_mode)
                            if directory:
                                low = e.name.lower()
                                if low in SKIP or st.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                                    continue
                                here = os.path.normcase(e.path)
                                if any(here == x or here.startswith(x + os.sep) for x in out):
                                    continue
                                stack.append(e.path)
                            parent.append(folder_id)
                            name.append(e.name)
                            is_dir.append(1 if directory else 0)
                            size.append(0 if directory else st.st_size)
                            mtime.append(st.st_mtime)
                    if len(name) % 50_000 < 200:
                        each(len(name))
            with self.lock:
                self.folders, self.parent, self.name = folders, parent, name
                self.gone = set()
                self.__dict__.pop("_folder_index", None)
                self.__dict__.pop("_system", None)
                self.is_dir, self.size, self.mtime = is_dir, size, mtime
                self._rebuild_blob()
            self.save()
            return len(name)
        finally:
            self.scanning = False

    def save(self):
        f = names_file()
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_suffix(".tmp")
        tmp.unlink(missing_ok=True)
        db = sqlite3.connect(tmp)
        db.executescript("CREATE TABLE folders(id INTEGER PRIMARY KEY, path TEXT);"
                         "CREATE TABLE entries(id INTEGER PRIMARY KEY, folder INTEGER, name TEXT, is_dir INTEGER, "
                         "size INTEGER, mtime REAL);")
        with self.lock:
            db.executemany("INSERT INTO folders VALUES(?, ?)", enumerate(self.folders))
            db.executemany("INSERT INTO entries VALUES(?, ?, ?, ?, ?, ?)",
                           ((i, self.parent[i], self.name[i], self.is_dir[i], self.size[i], self.mtime[i])
                            for i in range(len(self.name)) if i not in self.gone))
        db.commit()
        db.close()
        tmp.replace(f)
        self.loaded_at = time.time()

    # -- live changes

    def _folder_id(self, folder: str) -> int:
        try:
            return self._folder_index[os.path.normcase(folder)]
        except (AttributeError, KeyError):
            self._folder_index = {os.path.normcase(p): i for i, p in enumerate(self.folders)}
            if os.path.normcase(folder) not in self._folder_index:
                self.folders.append(folder)
                self._folder_index[os.path.normcase(folder)] = len(self.folders) - 1
            return self._folder_index[os.path.normcase(folder)]

    def added(self, path: str):
        """A file or folder appeared (or changed): it's findable right away."""
        try:
            st = os.stat(path, follow_symlinks=False)
        except OSError:
            return
        folder, entry = os.path.split(path)
        with self.lock:
            self.removed(path)
            fid = self._folder_id(folder)
            self.parent.append(fid)
            self.name.append(entry)
            directory = stat.S_ISDIR(st.st_mode)
            self.is_dir.append(1 if directory else 0)
            self.size.append(0 if directory else st.st_size)
            self.mtime.append(st.st_mtime)
            self.count += 1
            if len(self.name) - self.in_blob > 5000:   # many new names: fold them into the big string
                self._rebuild_blob()

    def removed(self, path: str):
        folder, entry = os.path.split(path)
        with self.lock:
            fid = self._folder_id(folder)
            for i in self._find(entry.lower(), limit=10_000, whole=True):
                if self.parent[i] == fid:
                    self.gone.add(i)
                    self.count -= 1

    # -- searching

    SYSTEM_PARTS = ("\\appdata\\", "\\windows\\", "\\program files", "\\programdata\\", "\\$", "\\temp\\",
                    "\\node_modules\\", "\\site-packages\\", "\\.git\\", "\\.venv\\", "\\cache\\", "\\winsxs\\")

    def _is_system(self, folder_id: int) -> bool:
        """Folders Windows and programs fill: their names come after the user's own (they're found all the same)."""
        cache = self.__dict__.setdefault("_system", {})
        if folder_id not in cache:
            low = self.folders[folder_id].lower() + "\\"
            cache[folder_id] = any(part in low for part in self.SYSTEM_PARTS)
        return cache[folder_id]

    def _entry_at(self, offset: int) -> int:
        return bisect.bisect_right(self.starts, offset) - 1

    def _find(self, piece: str, limit: int, whole: bool = False) -> list[int]:
        """Entries whose name holds `piece` (lower case; with `whole`, is exactly it), at most `limit`: those in the
        big string, then the few added since it was built."""
        out: list[int] = []
        seen = -1
        start = 0
        blob = self.blob
        needle = f"\n{piece}\n" if whole else piece
        while len(out) < limit:
            at = blob.find(needle, start)
            if at < 0:
                break
            i = self._entry_at(at + 1 if whole else at)
            start = at + 1
            if i != seen and i not in self.gone:
                out.append(i)
                seen = i
        for i in range(self.in_blob, len(self.name)):
            if len(out) >= limit:
                break
            low = self.name[i].lower()
            if i not in self.gone and (low == piece if whole else piece in low):
                out.append(i)
        return out

    def search(self, query: str, limit: int = 200, folders_only: bool = False,
               extensions: set[str] | None = None) -> list[Found]:
        pieces = [p for p in query.lower().split() if p]
        if not pieces:
            return []
        with self.lock:
            # The longest piece is the rarest: scan for it, then check the others in each candidate's name. A piece of
            # one or two letters is in hundreds of thousands of names: rank a sample, as the list shows 200 anyway.
            first = max(pieces, key=len)
            others = [p for p in pieces if p is not first]
            candidates = self._find(first, limit=20_000 if len(first) >= 3 else 3_000)
            hits = []
            for i in candidates:
                low = self.name[i].lower()
                if others and not all(p in low for p in others):
                    continue
                if folders_only and not self.is_dir[i]:
                    continue
                if extensions is not None and (self.is_dir[i] or rules.extension(low) not in extensions):
                    continue
                hits.append(i)
            whole = " ".join(pieces)

            def rank(i: int):
                low = self.name[i].lower()
                stem = low.rsplit(".", 1)[0] if not self.is_dir[i] else low
                at = low.find(first)
                word_start = at == 0 or not low[at - 1].isalnum()
                return (-(stem == whole or low == whole), self._is_system(self.parent[i]), -low.startswith(first),
                        -word_start, len(low), -self.mtime[i])

            hits.sort(key=rank)
            out = []
            for i in hits[:limit]:
                low = self.name[i].lower()
                marks = []
                for p in pieces:
                    at = low.find(p)
                    if at >= 0:
                        marks.append((at, at + len(p)))
                out.append(Found(path=os.path.join(self.folders[self.parent[i]], self.name[i]), name=self.name[i],
                                 is_dir=bool(self.is_dir[i]), size=self.size[i], modified=self.mtime[i],
                                 marks=sorted(marks)))
            return out


class NameService:
    """Keeps a Names up to date in the background: load the saved list, walk the drives, then watch them."""

    RESCAN = 6 * 3600

    def __init__(self, on_status: Callable[[str], None] = lambda s: None):
        self.names = Names()
        self.on_status = on_status
        self._observer = None
        self._stop = threading.Event()
        self._changes: list[tuple[str, str]] = []
        self._changes_lock = threading.Lock()

    def start(self):
        threading.Thread(target=self._run, name="names", daemon=True).start()

    def stop(self):
        self._stop.set()
        if self._observer:
            self._observer.stop()

    def _run(self):
        from .settings import Settings

        if self.names.load():
            self.on_status(f"{self.names.count:,} names")
        while not self._stop.is_set():
            s = Settings.load()
            if s.name_search:
                t = time.time()
                try:
                    n = self.names.scan(excluded=s.excluded, each=lambda k: self.on_status(f"reading names… {k:,}"))
                    log.info("names: %d entries in %.0f s", n, time.time() - t)
                    self.on_status(f"{n:,} names")
                    self._watch()
                except Exception:
                    log.exception("name scan")
            if self._stop.wait(self.RESCAN):
                break

    def _watch(self):
        from watchdog.events import FileSystemEventHandler
        from watchdog.observers import Observer

        if self._observer:
            return
        service = self

        class Changed(FileSystemEventHandler):
            def on_created(self, event):
                service.names.added(event.src_path)

            def on_deleted(self, event):
                service.names.removed(event.src_path)

            def on_moved(self, event):
                service.names.removed(event.src_path)
                service.names.added(event.dest_path)

        observer = Observer()
        for drive in fixed_drives():
            try:
                observer.schedule(Changed(), drive, recursive=True)
            except OSError:
                log.warning("can't watch %s for names", drive)
        observer.daemon = True
        observer.start()
        self._observer = observer
