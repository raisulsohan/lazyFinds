"""Code search: "code: read a csv and sum a column" finds the function, in the folders chosen for code.

Code has an index of its own (paths.code_index_file), so it never crowds other searches. A source file is cut into
blocks at its definitions (def, class, function, func, fn, struct, …), each at most MAX_LINES lines; a notebook into
its cells. Each block is embedded as a document titled with its path in its folder, and its identifiers are added to
its words split apart ("refreshAccessToken" → "refresh access token"), so a search for either finds it. A query that
is a single name goes to where that name is defined, not where it's used.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from . import engine as E
from . import extract as X
from . import text as T

PREFIX = "code:"
MAX_LINES = 60
MAX_CHARS = 2400
MAX_FILE = 1_000_000          # bigger source files were made by a program
DEFINITION = re.compile(
    r"^\s{0,4}(?:export\s+|public\s+|private\s+|protected\s+|internal\s+|static\s+|async\s+|pub(?:\(crate\))?\s+)*"
    r"(?:def|class|function|func|fn|struct|enum|interface|trait|impl|type|module|namespace|record|object|"
    r"macro_rules!|CREATE\s+(?:OR\s+REPLACE\s+)?(?:FUNCTION|PROCEDURE|TABLE|VIEW))\b", re.IGNORECASE)
NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")


def query_of(text: str) -> str | None:
    """What's typed after "code:", or None when the query isn't a code search."""
    stripped = text.lstrip()
    return stripped[len(PREFIX):].strip() if stripped.lower().startswith(PREFIX) else None


def split_name(name: str) -> list[str]:
    """"refreshAccessToken" → ["refresh", "access", "token"]; "read_csv" → ["read", "csv"]; "HTTPServer" → ["http",
    "server"]."""
    parts = re.findall(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+", name)
    return [p.lower() for p in parts if len(p) > 1]


def name_words(body: str) -> str:
    seen: list[str] = []
    for name in set(NAME.findall(body)):
        parts = split_name(name)
        if len(parts) > 1:
            seen.extend(parts)
    return " ".join(sorted(set(seen)))


def blocks(lines: list[str]) -> list[tuple[int, int]]:
    """(first, last) line numbers, from 1, of the blocks a source file is cut into."""
    starts = [0]
    for i, line in enumerate(lines):
        if i and DEFINITION.match(line):
            # Decorators, attributes and comments right above a definition belong to it.
            a = i
            while a - 1 > starts[-1] and lines[a - 1].strip().startswith(("@", "#[", "//", "#", "/*", "*")):
                a -= 1
            starts.append(a)
    out: list[tuple[int, int]] = []
    for a, b in zip(starts, starts[1:] + [len(lines)]):
        piece_start = a
        while piece_start < b:
            end = min(b, piece_start + MAX_LINES)
            if end < b:   # a long block breaks at a blank line when there is one
                blank = max((i for i in range(piece_start + MAX_LINES // 2, end) if not lines[i].strip()), default=None)
                end = blank + 1 if blank is not None else end
            if any(line.strip() for line in lines[piece_start:end]):
                out.append((piece_start + 1, end))
            piece_start = end
    return out


def read_code(path: Path, root: Path | None = None) -> X.Reading:
    if path.stat().st_size > MAX_FILE:
        raise X.Skip("too big for hand-written code")
    title = str(path.relative_to(root)) if root and path.is_relative_to(root) else path.name
    if path.suffix.lower() == ".ipynb":
        return _read_notebook(path, title)
    text = X.decode(path.read_bytes())
    lines = text.splitlines()
    if lines and sum(len(line) for line in lines) / len(lines) > 300:
        raise X.Skip("minified")
    reading = X.Reading(meta={"lines": len(lines)})
    for first, last in blocks(lines):
        body = "\n".join(lines[first - 1:last])[:MAX_CHARS * 2]
        reading.pieces.append(X.Piece("code", "text", start=first, stop=last, line=_summary(body), body=body,
                                      embed=("text", E.document_prompt(title, body[:MAX_CHARS])),
                                      words=name_words(body)))
    if not reading.pieces:
        raise X.Skip("no code")
    return reading


def _read_notebook(path: Path, title: str) -> X.Reading:
    try:
        cells = json.loads(X.decode(path.read_bytes())).get("cells", [])
    except ValueError:
        raise X.Unreadable("not a notebook")
    reading = X.Reading(meta={"cells": len(cells)})
    for number, cell in enumerate(cells, 1):
        source = cell.get("source", "")
        body = "".join(source) if isinstance(source, list) else str(source)
        if body.strip():
            reading.pieces.append(X.Piece("cell", "text", start=number, stop=number, line=_summary(body), body=body,
                                          embed=("text", E.document_prompt(title, body[:MAX_CHARS]))))
    if not reading.pieces:
        raise X.Skip("empty notebook")
    return reading


def _summary(body: str) -> str:
    for line in body.splitlines():
        s = line.strip()
        if s and not s.startswith(("#", "//", "/*", "*", "@", "import ", "from ", "using ", "#include")):
            return T.preview(s, 160)
    return T.preview(body, 160)


def definition_line(name: str, body: str, first_line: int) -> int | None:
    """The line in `body` (starting at `first_line`) that defines `name`, if one does."""
    pattern = re.compile(rf"(?:\b(?:def|class|function|func|fn|struct|enum|interface|trait|type|const|let|var|val)\s+"
                         rf"\*?{re.escape(name)}\b|\b{re.escape(name)}\s*(?::[^=]*)?=\s*(?:function|async|\(|lambda))")
    for i, line in enumerate(body.splitlines()):
        if pattern.search(line):
            return first_line + i
    return None


def reader_for(roots: list[Path]):
    """read(path, kind) for the indexer: code files, titled by their path inside their chosen folder."""
    def read(path: Path, kind: str) -> X.Reading:
        root = next((r for r in roots if path.is_relative_to(r)), None)
        return read_code(path, root)
    return read


def editor_name() -> str | None:
    for editor, name in (("code", "VS Code"), ("cursor", "Cursor")):
        if shutil.which(editor):
            return name
    return None


def open_at(path: str, line: int | None):
    """Opens a code file at a line: VS Code (or Cursor) when installed, else whatever opens the file type."""
    for editor in ("code", "cursor"):
        exe = shutil.which(editor)
        if exe:
            subprocess.Popen([exe, "-g", f"{path}:{int(line or 1)}"], creationflags=subprocess.CREATE_NO_WINDOW)
            return
    os.startfile(path)


def search(searcher, query: str, vector, limit: int = 40, typing: bool = False):
    """Code results for what's typed after "code:". A single name (refreshAccessToken, read_csv) puts the blocks that
    define it first, each pointing at the defining line."""
    hits = searcher.search(query, vector, limit=limit, typing=typing)
    name = query.strip().rstrip("()")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", name):
        return hits
    name = name.split(".")[-1]
    defined = []
    for hit in hits:
        body = searcher.body(hit.piece) or ""
        line = definition_line(name, body, int(hit.start or 1))
        if line is not None:
            hit.start = line
            defined.append(hit)
    return defined + [h for h in hits if h not in defined]
