"""The index, in one SQLite file (the code index is a second file of the same shape).

    files   one row per file: where it is, what kind, size and time to notice changes, how reading it went
    pieces  what a file was read into: a picture, a page, a passage, a stretch of sound, a scene of a video, a block
            of code. Each has its place in the file, a line to show, its words as written, and its vector
            (768 x float16, L2-normalized) when it has one
    words   FTS5 over each piece's folded words, plus one row per file for its name and folders

Writes go through one connection holding `lock`; readers open their own connection (WAL lets them run side by side).
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

import numpy as np

from . import text as T

SCHEMA = """
CREATE TABLE IF NOT EXISTS info(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS files(
    id INTEGER PRIMARY KEY,
    path TEXT NOT NULL UNIQUE COLLATE NOCASE,
    kind TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'new',     -- new | read | skipped | failed
    note TEXT,
    meta TEXT,
    read_at REAL
);
CREATE INDEX IF NOT EXISTS files_status ON files(status);
CREATE TABLE IF NOT EXISTS pieces(
    id INTEGER PRIMARY KEY,
    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    part TEXT NOT NULL,          -- name | picture | screen_text | page | page_picture | passage | sound | scene | code
    source TEXT,                 -- what made the vector: text | image | audio (NULL: no vector)
    start REAL,                  -- page, passage number, second, or line
    stop REAL,
    line TEXT,                   -- what the results list shows
    body TEXT,                   -- the words as written
    vector BLOB
);
CREATE INDEX IF NOT EXISTS pieces_file ON pieces(file_id);
"""
# Marks (M*) belong to words: without them a Bengali vowel sign would split its word in two.
WORDS_TABLE = ("CREATE VIRTUAL TABLE IF NOT EXISTS words USING fts5(name, body, "
               "tokenize=\"porter unicode61 remove_diacritics 2 categories 'L* N* M* Co'\")")
ORDER = "CASE kind WHEN 'screenshot' THEN 0 WHEN 'doc' THEN 1 WHEN 'pdf' THEN 2 WHEN 'image' THEN 3 " \
        "WHEN 'audio' THEN 4 WHEN 'video' THEN 5 ELSE 6 END"   # quick, word-rich kinds first


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None, timeout=30)
        self.lock = threading.RLock()
        for pragma in ("journal_mode=WAL", "synchronous=NORMAL", "foreign_keys=ON"):
            self.db.execute(f"PRAGMA {pragma}")
        tables = {row[0] for row in self.db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        if "segments" in tables:
            # An index from lazyFinds' first version: a cache, so it's rebuilt rather than converted.
            for table in ("fts", "segments", "files", "meta"):
                self.db.execute(f"DROP TABLE IF EXISTS {table}")
            self.db.execute("VACUUM")
        self.db.executescript(SCHEMA)
        self.db.execute(WORDS_TABLE)

    def close(self):
        self.db.close()

    def get(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM info WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def put(self, key: str, value: str):
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO info VALUES(?, ?)", (key, value))

    class _Transaction:
        def __init__(self, store: "Store"):
            self.store = store

        def __enter__(self):
            self.store.lock.acquire()
            self.store.db.execute("BEGIN IMMEDIATE")

        def __exit__(self, error, *_):
            try:
                self.store.db.execute("ROLLBACK" if error else "COMMIT")
            finally:
                self.store.lock.release()

    def transaction(self):
        return Store._Transaction(self)

    def use_model(self, source: str) -> bool:
        """Records which model made this index's vectors. A different one empties every file's pieces so all are read
        again: two models' vectors don't compare. True when that happened."""
        current = self.get("vectors")
        if current == source:
            return False
        with self.transaction():
            if current is not None:
                self.db.execute("DELETE FROM words WHERE rowid IN (SELECT id FROM pieces WHERE part != 'name')")
                self.db.execute("DELETE FROM pieces WHERE part != 'name'")
                self.db.execute("UPDATE files SET status = 'new', note = NULL, meta = NULL")
            self.db.execute("INSERT OR REPLACE INTO info VALUES('vectors', ?)", (source,))
        return current is not None

    # -- files

    def all_files(self) -> dict[str, tuple[int, int, float, str]]:
        return {row[0]: row[1:] for row in self.db.execute("SELECT path, id, size, mtime, kind FROM files")}

    @staticmethod
    def name_words(path: str) -> tuple[str, str]:
        """The file's name, and the folders above it from the user's home down, as words."""
        p = Path(path)
        name = p.stem.replace("_", " ").replace("-", " ").replace(".", " ")
        parent = str(p.parent)
        home = str(Path.home())
        if parent.lower().startswith(home.lower()):
            parent = parent[len(home):]
        folders = " ".join(part for part in Path(parent).parts if not part.endswith(("\\", ":")))
        return T.fold(f"{name} {p.suffix.lstrip('.')}"), T.fold(folders)

    def add_file(self, path: str, kind: str, size: int, mtime: float) -> int:
        with self.transaction():
            file_id = self.db.execute("INSERT INTO files(path, kind, size, mtime) VALUES(?, ?, ?, ?)",
                                      (path, kind, size, mtime)).lastrowid
            piece = self.db.execute("INSERT INTO pieces(file_id, part) VALUES(?, 'name')", (file_id,)).lastrowid
            self.db.execute("INSERT INTO words(rowid, name, body) VALUES(?, ?, ?)", (piece, *self.name_words(path)))
        return file_id

    def file_changed(self, file_id: int, kind: str, size: int, mtime: float):
        with self.transaction():
            self._drop_pieces(file_id, keep_name=True)
            self.db.execute("UPDATE files SET kind = ?, size = ?, mtime = ?, status = 'new', note = NULL, meta = NULL "
                            "WHERE id = ?", (kind, size, mtime, file_id))

    def remove(self, file_ids: list[int]):
        with self.transaction():
            for file_id in file_ids:
                self._drop_pieces(file_id, keep_name=False)
                self.db.execute("DELETE FROM files WHERE id = ?", (file_id,))

    def _drop_pieces(self, file_id: int, keep_name: bool):
        only = "AND part != 'name'" if keep_name else ""
        self.db.execute(f"DELETE FROM words WHERE rowid IN (SELECT id FROM pieces WHERE file_id = ? {only})", (file_id,))
        self.db.execute(f"DELETE FROM pieces WHERE file_id = ? {only}", (file_id,))

    def to_read(self, limit: int | None = None) -> list[tuple[int, str, str]]:
        sql = f"SELECT id, path, kind FROM files WHERE status = 'new' ORDER BY {ORDER}, size"
        return list(self.db.execute(sql + (f" LIMIT {int(limit)}" if limit else "")))

    def mark(self, file_id: int, status: str, note: str | None = None, meta: dict | None = None):
        with self.lock:
            self.db.execute("UPDATE files SET status = ?, note = ?, meta = ?, read_at = ? WHERE id = ?",
                            (status, note, json.dumps(meta, ensure_ascii=False) if meta else None, time.time(),
                             file_id))

    def add_piece(self, file_id: int, part: str, source: str | None, start=None, stop=None, line=None, body=None,
                  vector: np.ndarray | None = None, words: str | None = None):
        """Inside `transaction()`."""
        blob = np.asarray(vector, dtype=np.float16).tobytes() if vector is not None else None
        piece = self.db.execute(
            "INSERT INTO pieces(file_id, part, source, start, stop, line, body, vector) VALUES(?,?,?,?,?,?,?,?)",
            (file_id, part, source if blob else None, start, stop, line, body, blob)).lastrowid
        if body or words:
            text = "\n".join(t for t in (body, words) if t)
            self.db.execute("INSERT INTO words(rowid, name, body) VALUES(?, '', ?)", (piece, T.fold(text)))

    def counts(self) -> dict:
        kinds = dict(self.db.execute("SELECT kind, COUNT(*) FROM files GROUP BY kind"))
        status = dict(self.db.execute("SELECT status, COUNT(*) FROM files GROUP BY status"))
        vectors = self.db.execute("SELECT COUNT(*) FROM pieces WHERE vector IS NOT NULL").fetchone()[0]
        return {"kinds": kinds, "status": status, "vectors": vectors}
