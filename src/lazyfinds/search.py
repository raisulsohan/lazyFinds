"""Search: meaning and exact words, each ranked on its own, then fused.

Meaning. The query's cosine with every piece's vector. Cosines of different kinds of vectors sit at different levels
for the same query (text pieces higher than pictures or sound), so each is put on the scale of its own kind: how many
robust deviations (1.4826 x the median absolute deviation) it stands above that kind's median. A piece "stands out"
at MEANING_GATE or more. A file takes its best piece.

Within a kind, files of one sort (the same kind of file and file type: WhatsApp voice messages, songs, screenshots,
phone videos) can sit together above or below the rest for reasons that aren't the query's meaning: a query in Bengali
is about two deviations closer to every Bengali voice message than to other sound, whatever the message says, so "লাল
জামা" put 9 voice messages in the top 10 of a real index. Each such group of at least SMALL_KIND pieces is moved to its
own median, on its kind's scale: a piece counts by how far it stands above files of its own sort. Over 28 picture
queries on that index, voice messages in the top 10 went from 56 to 18 (the rest are the few that match in English
too) and photos and videos from 211 to 253, with English queries as they were.

Words. SQLite FTS5 over file names, folder names, document and page text and the words in pictures. A file whose
pieces hold every query word matches fully; one that holds most of a longer query, partly.

Banglish. A query with Bengali typed in Latin letters ("atoshbaji", see banglish.py) is searched in its Bengali form:
its meaning is the Bengali form's (English words in it stay as typed), and each Banglish word matches as itself or any
of its likely Bengali spellings. The meaning of the query as typed isn't used: the model reads "jonmodiner cake" as a
cake recipe as much as a birthday, and keeping each file's better score of the two forms put 8 of 32 Banglish test
queries below another file, against 3 with the Bengali form alone.

Fusion. Reciprocal rank fusion of the two rankings (Cormack, Clarke & Büttcher, 2009): a file near the top of both
comes first. Before that, two tiers: files holding every code or ID the query has ("TXN-58K3Q", "881275") come
first, and files of the kind the query names ("…in a video") come before files of other kinds.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

import numpy as np

from . import text as T
from .banglish import Conversion
from .store import Store

MEANING_GATE = 3.0     # robust deviations above the median of its kind
STRONG = 4.5           # a meaning match this clear is never folded under "weaker matches"
FUSION_K = 4           # small: the head of each ranking should count much more than its tail
SMALL_KIND = 25        # fewer vectors of a kind than this: use the median of all vectors instead


@dataclass
class Hit:
    path: str
    kind: str
    score: float
    meaning: float | None      # robust deviations of the best piece, when it stood out
    words: float               # share of the query's words the file holds (0 to 1)
    strong: bool
    piece: int
    part: str
    start: float | None
    stop: float | None
    line: str | None
    modified: float
    size: int
    meta: dict = field(default_factory=dict)


def _spread(values: np.ndarray) -> float:
    if len(values) == 0:
        return 1.0
    mad = float(np.median(np.abs(values - np.median(values))))
    return max(1.4826 * mad, 0.01)


class Searcher:
    def __init__(self, store: Store):
        self.store = store
        self.vectors: np.ndarray | None = None
        self.piece_ids = np.zeros(0, dtype=np.int64)
        self.file_ids = np.zeros(0, dtype=np.int64)
        self.sources = np.zeros(0, dtype=object)
        self.kind: dict[int, str] = {}
        self.source_rows: dict[str, np.ndarray] = {}     # kind of vector -> its rows
        self.groups: list[tuple[str, np.ndarray]] = []   # (kind of vector, rows) of each group of one sort of file
        self._seen = None

    def _version(self):
        return self.store.db.execute("SELECT COUNT(*), COALESCE(MAX(id), 0) FROM pieces").fetchone()

    def load(self):
        rows = self.store.db.execute(
            "SELECT id, file_id, source, vector FROM pieces WHERE vector IS NOT NULL ORDER BY id").fetchall()
        self.vectors = (np.frombuffer(b"".join(r[3] for r in rows), dtype=np.float16).reshape(len(rows), -1)
                        .astype(np.float32)) if rows else None
        self.piece_ids = np.array([r[0] for r in rows], dtype=np.int64)
        self.file_ids = np.array([r[1] for r in rows], dtype=np.int64)
        self.sources = np.array([r[2] for r in rows], dtype=object)
        self.kind = dict(self.store.db.execute("SELECT id, kind FROM files"))
        # Groups of one sort of file within each kind of vector: (kind of vector, kind of file, file type).
        types = {f: os.path.splitext(p)[1].lower() for f, p in self.store.db.execute("SELECT id, path FROM files")}
        members: dict[tuple, list[int]] = {}
        by_source: dict[str, list[int]] = {}
        for i, (f, source) in enumerate(zip(self.file_ids.tolist(), self.sources.tolist())):
            members.setdefault((source, self.kind.get(f), types.get(f)), []).append(i)
            by_source.setdefault(source, []).append(i)
        self.source_rows = {source: np.array(rows) for source, rows in by_source.items()}
        self.groups = [(key[0], np.array(rows)) for key, rows in members.items() if len(rows) >= SMALL_KIND]
        self._seen = self._version()

    def refresh(self):
        if self._seen != self._version():
            self.load()

    # -- meaning

    def standing(self, cosines: np.ndarray) -> np.ndarray:
        """Each cosine in robust deviations, on the scale of its kind of vector, above the median of its group of one
        sort of file (or of its kind, when the group is small)."""
        out = np.empty(len(cosines), dtype=np.float64)
        overall = (float(np.median(cosines)), _spread(cosines))
        spreads = {}
        for source, rows in self.source_rows.items():
            if len(rows) >= SMALL_KIND:
                centre, spread = float(np.median(cosines[rows])), _spread(cosines[rows])
            else:
                centre, spread = overall
            spreads[source] = spread
            out[rows] = (cosines[rows] - centre) / spread
        for source, rows in self.groups:
            # Only the centre moves; the kind's spread stays, so a group of very alike files (songs) isn't stretched.
            out[rows] = (cosines[rows] - float(np.median(cosines[rows]))) / spreads[source]
        return out

    def _meaning(self, vector, allowed) -> dict[int, tuple[float, int]]:
        if vector is None or self.vectors is None:
            return {}
        stand = self.standing(self.vectors @ np.asarray(vector, dtype=np.float32))
        best: dict[int, tuple[float, int]] = {}
        for row in np.argsort(-stand)[:5000]:
            f = int(self.file_ids[row])
            if f not in best and allowed(f):
                best[f] = (float(stand[row]), int(self.piece_ids[row]))
        return best

    # -- words

    def _words(self, terms: list[str], typing: bool, allowed,
               spellings: dict[str, list[str]] | None = None) -> dict[int, tuple[float, float, int]]:
        """file -> (share of the terms it holds, its best bm25, the piece that matched best). A term with Bengali
        `spellings` matches as itself or any of them."""
        if not terms:
            return {}
        held: dict[int, set[int]] = {}
        best: dict[int, tuple[float, int, bool]] = {}
        for i, term in enumerate(terms):
            fts = T.fts_term(term, typing and i == len(terms) - 1)
            if spellings and term in spellings:
                fts = " OR ".join([fts] + [T.fts_term(s) for s in spellings[term]])
            try:
                rows = self.store.db.execute(
                    "SELECT p.file_id, p.id, bm25(words, 5.0, 1.0), p.part FROM words "
                    "JOIN pieces p ON p.id = words.rowid WHERE words MATCH ? LIMIT 3000", (fts,)).fetchall()
            except Exception:   # a term FTS5 can't parse is left out
                continue
            for file_id, piece, rank, part in rows:
                if not allowed(file_id):
                    continue
                held.setdefault(file_id, set()).add(i)
                is_name = part == "name"
                old = best.get(file_id)
                # Prefer a piece with the words in its text over the file name: that's what the preview shows.
                if old is None or (old[2] and not is_name) or (old[2] == is_name and rank < old[0]):
                    best[file_id] = (rank, piece, is_name)
        return {f: (len(held[f]) / len(terms), best[f][0], best[f][1]) for f in held}

    # -- both

    def search(self, query: str, vector=None, kinds: set[str] | None = None, limit: int = 40,
               typing: bool = False, also: Conversion | None = None) -> list[Hit]:
        """`also`: the query's Banglish words and their Bengali spellings; then `vector` is the Bengali form's."""
        if self._seen is None:
            self.load()

        def allowed(f: int) -> bool:
            return kinds is None or self.kind.get(f) in kinds

        terms = T.query_terms(query)
        asked = T.kinds_asked(query)
        spellings: dict[str, list[str]] = {}
        if also:
            # A Banglish word that is a Bengali stopword ("theke" থেকে) or names a kind of file ("chobi" ছবি) is
            # left out, as it would be in Bengali.
            for term in list(terms):
                forms = also.words.get(term)
                if not forms:
                    continue
                if forms[0] in T.BENGALI_STOP or forms[0] in T.KIND_WORDS:
                    terms.remove(term)
                    asked |= T.kinds_asked(forms[0])
                else:
                    spellings[term] = forms
        codes = [t for t in terms if T.is_code_like(t)]
        meaning = self._meaning(vector, allowed)
        words = self._words(terms, typing, allowed, spellings)

        enough = 1.0 if len(terms) < 3 else 0.66
        by_meaning = sorted((f for f, (s, _) in meaning.items() if s >= MEANING_GATE), key=lambda f: -meaning[f][0])
        by_words = sorted((f for f, (share, _, _) in words.items() if share >= enough),
                          key=lambda f: (-words[f][0], words[f][1]))
        rank_m = {f: i + 1 for i, f in enumerate(by_meaning)}
        rank_w = {f: i + 1 for i, f in enumerate(by_words)}
        candidates = set(rank_m) | set(rank_w)
        if not candidates and meaning:
            # Nothing stands out (a one-word query in a small index): offer the closest few as weak matches.
            near = [f for f, (s, _) in sorted(meaning.items(), key=lambda kv: -kv[1][0])[:3]
                    if s >= MEANING_GATE * 0.6]
            rank_m = {f: i + 1 for i, f in enumerate(near)}
            candidates = set(near)

        def order(f: int):
            fused = sum(1 / (FUSION_K + r[f]) for r in (rank_m, rank_w) if f in r)
            holds_codes = bool(codes) and words.get(f, (0,))[0] == 1.0
            return (holds_codes, self.kind.get(f) in asked, fused, meaning.get(f, (0,))[0])

        ranked = sorted(candidates, key=order, reverse=True)[:limit]
        marked = terms + [s for forms in spellings.values() for s in forms]
        return [self._hit(f, order(f)[2], meaning.get(f), words.get(f), f in rank_m, marked) for f in ranked]

    def _hit(self, f: int, score: float, meaning, words, by_meaning: bool, terms: list[str]) -> Hit:
        db = self.store.db
        piece = meaning[1] if (by_meaning or not words) else words[2]
        path, kind, mtime, size, meta = db.execute(
            "SELECT path, kind, mtime, size, meta FROM files WHERE id = ?", (f,)).fetchone()
        part, start, stop, line, body = db.execute(
            "SELECT part, start, stop, line, body FROM pieces WHERE id = ?", (piece,)).fetchone()
        if part == "name":   # matched by name only: point at the file's first piece
            row = db.execute("SELECT id, part, start, stop, line, body FROM pieces WHERE file_id = ? AND part != 'name' "
                             "ORDER BY id LIMIT 1", (f,)).fetchone()
            if row:
                piece, part, start, stop, line, body = row
        if words and kind != "code":   # code keeps its own summary line
            # Quote the query's words from the piece we point at, or from a picture's own words.
            marked = T.mark(body, terms) if body else None
            if marked is None:
                other = db.execute("SELECT part, body FROM pieces WHERE id = ?", (words[2],)).fetchone()
                if other and other[1] and (other[0] == "screen_text" or not body):
                    marked = T.mark(other[1], terms)
            line = marked or line
        strength = meaning[0] if meaning else None
        share = words[0] if words else 0.0
        return Hit(path=path, kind=kind, score=score, meaning=strength if by_meaning else None, words=share,
                   strong=(strength or 0) >= STRONG or share == 1.0, piece=piece, part=part, start=start, stop=stop,
                   line=line, modified=mtime, size=size, meta=json.loads(meta) if meta else {})

    def body(self, piece: int) -> str | None:
        row = self.store.db.execute("SELECT body FROM pieces WHERE id = ?", (piece,)).fetchone()
        return row[0] if row else None


def strong_count(hits: list[Hit]) -> int:
    """How many results come before "weaker matches": the leading run of strong ones (all of them if none is)."""
    n = 0
    while n < len(hits) and hits[n].strong:
        n += 1
    return n or len(hits)
