"""Word 97-2003 (.doc) and PowerPoint 97-2003 (.ppt) files: their text and their pictures.

Both are OLE compound files (MS-CFB), a small file system of streams in one file. Word keeps its text in pieces that
a piece table in the table stream places (MS-DOC); PowerPoint keeps records that a persist directory places, slide by
slide (MS-PPT). Both draw pictures with OfficeArt records (MS-ODRAW): a shape's properties name its picture (`pib`, a
number in the picture store) and hold its alt text, and the picture's bytes sit in a store record or in a stream of
their own.
"""

from __future__ import annotations

import re
import struct
import zlib
from dataclasses import dataclass

MAGIC = bytes.fromhex("D0CF11E0A1B11AE1")
LAST_SECTOR = 0xFFFFFFFA   # sector numbers at or above this mark ends, free sectors and FAT sectors


class Damaged(Exception):
    pass


class Locked(Exception):
    pass


@dataclass
class Picture:
    key: str             # the same picture has the same key
    data: bytes          # a file Pillow opens (PNG, JPEG, BMP, TIFF, EMF)
    alt: str | None = None
    slide: int | None = None


# -- the compound file


class Compound:
    """The streams at the top of a compound file. An embedded file keeps its own streams in a storage below them,
    out of reach here, so an embedded Word file's text isn't taken for this file's."""

    def __init__(self, data: bytes):
        if len(data) < 512 or data[:8] != MAGIC:
            raise Damaged("not an Office 97-2003 file")
        self.data = data
        shift, mini_shift = struct.unpack_from("<HH", data, 0x1E)
        if shift not in (9, 12):
            raise Damaged("unknown sector size")
        self.size, self.mini_size = 1 << shift, 1 << mini_shift
        n_fat, dir_start, _, self.cutoff, minifat_start, _, difat_start, n_difat = struct.unpack_from("<8I", data, 0x2C)
        difat = list(struct.unpack_from("<109I", data, 0x4C))
        per = self.size // 4 - 1
        sector, seen = difat_start, set()
        while sector < LAST_SECTOR and sector not in seen and len(seen) < n_difat:
            seen.add(sector)
            entries = struct.unpack(f"<{per + 1}I", self._sector(sector))
            difat.extend(entries[:per])
            sector = entries[per]
        fat = b"".join(self._sector(s) for s in difat[:n_fat] if s < LAST_SECTOR)
        self.fat = struct.unpack(f"<{len(fat) // 4}I", fat)
        directory = self._big(dir_start)
        self.entries = [directory[i:i + 128] for i in range(0, len(directory) - 127, 128)]
        if not self.entries:
            raise Damaged("no directory")
        root = self.entries[0]
        self.mini_stream = self._big(struct.unpack_from("<I", root, 116)[0], struct.unpack_from("<I", root, 120)[0])
        minifat = self._big(minifat_start)
        self.minifat = struct.unpack(f"<{len(minifat) // 4}I", minifat)
        self.top: dict[str, bytes] = {}
        stack, visited = [struct.unpack_from("<I", root, 76)[0]], set()
        while stack:
            i = stack.pop()
            if i >= len(self.entries) or i in visited:
                continue
            visited.add(i)
            e = self.entries[i]
            left, right = struct.unpack_from("<II", e, 68)
            stack += [left, right]
            length = min(struct.unpack_from("<H", e, 64)[0], 64)
            self.top[e[:max(0, length - 2)].decode("utf-16-le", "replace")] = e

    def _sector(self, n: int) -> bytes:
        at = (n + 1) * self.size
        if at >= len(self.data):
            raise Damaged("a sector past the end")
        return self.data[at:at + self.size].ljust(self.size, b"\0")

    @staticmethod
    def _chain(start: int, table) -> list[int]:
        out, n = [], start
        while n < len(table) and len(out) <= len(table):   # ends at an end mark; a loop can't run forever
            out.append(n)
            n = table[n]
        return out

    def _big(self, start: int, size: int | None = None) -> bytes:
        data = b"".join(self._sector(n) for n in self._chain(start, self.fat))
        return data if size is None else data[:size]

    def has(self, name: str) -> bool:
        return name in self.top and self.top[name][66] == 2

    def stream(self, name: str) -> bytes:
        e = self.top.get(name)
        if e is None or e[66] != 2:
            raise KeyError(name)
        start, size = struct.unpack_from("<II", e, 116)
        if size >= self.cutoff:
            return self._big(start, size)
        m = self.mini_size
        return b"".join(self.mini_stream[n * m:(n + 1) * m] for n in self._chain(start, self.minifat))[:size]


# -- OfficeArt records (also the shape of every PowerPoint record)


def records(buf: bytes, start: int, end: int, deep: bool = True):
    """(where the data starts, instance, type, length) of each record in buf[start:end], inside containers too when
    `deep`. A record that runs past the end stops the walk."""
    pos = start
    while pos + 8 <= end:
        head, kind, length = struct.unpack_from("<HHI", buf, pos)
        data = pos + 8
        if data + length > end:
            return
        yield data, head >> 4, kind, length
        if deep and head & 0xF == 0xF:
            yield from records(buf, data, data + length)
        pos = data + length


def _properties(buf: bytes, data: int, instance: int, length: int) -> dict[int, int | bytes]:
    """An OfficeArtFOPT: property number -> value; a complex property's value is its bytes."""
    if 6 * instance > length:
        return {}
    out: dict[int, int | bytes] = {}
    extra, end = data + 6 * instance, data + length
    for k in range(instance):
        opid, op = struct.unpack_from("<HI", buf, data + 6 * k)
        if opid & 0x8000:
            out[opid & 0x3FFF] = buf[extra:min(extra + op, end)]
            extra += op
        else:
            out[opid & 0x3FFF] = op
    return out


def _shapes(buf: bytes, start: int, end: int):
    """(picture number in the store, alt text) of each shape that shows a picture."""
    for data, _, kind, length in records(buf, start, end):
        if kind != 0xF004:   # OfficeArtSpContainer
            continue
        props: dict = {}
        for d, instance, k, n in records(buf, data, data + length, deep=False):
            if k in (0xF00B, 0xF121, 0xF122):   # the shape's properties, in up to three tables
                props.update(_properties(buf, d, instance, n))
        pib = props.get(0x0104) or props.get(0x0186)   # the picture, or a picture filling the shape
        if isinstance(pib, int) and pib > 0:
            alt = props.get(0x0381)
            alt = alt.decode("utf-16-le", "replace").split("\0")[0].strip() if isinstance(alt, bytes) else ""
            yield pib, alt or None


def _bmp(dib: bytes) -> bytes:
    """A device-independent bitmap with the file header it lacks."""
    header, bits, compression, colours = (struct.unpack_from("<I", dib, 0)[0], struct.unpack_from("<H", dib, 14)[0],
                                          struct.unpack_from("<I", dib, 16)[0], struct.unpack_from("<I", dib, 32)[0])
    palette = colours or (1 << bits if bits <= 8 else 0)
    masks = 12 if compression == 3 and header == 40 else 0
    return b"BM" + struct.pack("<IHHI", 14 + len(dib), 0, 0, 14 + header + masks + 4 * palette) + dib


def blip(buf: bytes, pos: int) -> tuple[str, bytes] | None:
    """The picture in the BLIP record at `pos`: (its key, a file Pillow opens), or None for a kind Pillow can't."""
    if pos + 8 > len(buf):
        return None
    head, kind, length = struct.unpack_from("<HHI", buf, pos)
    data, end = pos + 8, pos + 8 + length
    if end > len(buf) or length < 17:
        return None
    key = buf[data:data + 16].hex()
    skip = 32 if (head >> 4) & 1 else 16   # an odd instance carries a second 16-byte id
    try:
        if kind == 0xF01A:   # EMF, maybe deflated, after a 34-byte header
            h = data + skip
            cb_save, compression = struct.unpack_from("<I", buf, h + 28)[0], buf[h + 32]
            raw = buf[h + 34:min(h + 34 + cb_save, end)]
            return key, zlib.decompress(raw) if compression == 0 else raw
        if kind in (0xF01D, 0xF01E, 0xF029, 0xF02A):   # JPEG, PNG, TIFF, CMYK JPEG, after a tag byte
            return key, buf[data + skip + 1:end]
        if kind == 0xF01F:   # DIB
            return key, _bmp(buf[data + skip + 1:end])
    except (zlib.error, struct.error):
        return None
    return None   # WMF and Mac PICT


def _store(buf: bytes, start: int, end: int, delay: bytes) -> list[tuple[str, bytes] | None]:
    """The picture store (OfficeArtBStoreContainer) in buf[start:end]: each entry's picture, in order. A picture
    sits in its entry, or in the `delay` stream where the entry points."""
    out: list[tuple[str, bytes] | None] = []
    for data, instance, kind, length in records(buf, start, end):
        if kind != 0xF001:
            continue
        for d, _, k, n in records(buf, data, data + length, deep=False):
            if k != 0xF007:   # a BLIP stored directly
                out.append(blip(buf, d - 8))
                continue
            refs, delay_at = struct.unpack_from("<II", buf, d + 24) if n >= 36 else (0, 0xFFFFFFFF)
            inside = d + 36 + (buf[d + 33] if n >= 36 else 0)
            if n < 36 or refs == 0:
                out.append(None)
            elif inside + 8 <= d + n:
                out.append(blip(buf, inside))
            elif delay_at != 0xFFFFFFFF:
                out.append(blip(delay, delay_at))
            else:
                out.append(None)
        break
    return out


def _pictures(placed, store, slide: int | None = None) -> list[Picture]:
    out = []
    for pib, alt in placed:
        found = store[pib - 1] if pib <= len(store) else None
        if found:
            out.append(Picture(found[0], found[1], alt, slide))
    return out


# -- Word


_WORD_CHARS = str.maketrans({"\r": "\n", "\x0b": "\n", "\x0c": "\n", "\x0e": "\n", "\x07": "\t", "\x1e": "-",
                             **{chr(c): None for c in range(32) if chr(c) not in "\t\n\r\x0b\x0c\x0e\x07\x1e"}})


def _without_field_codes(text: str) -> str:
    """Fields are code (\\x13), result (\\x14) and end (\\x15), nested: keep only what a reader sees."""
    if "\x13" not in text:
        return text
    out, fields = [], []   # each open field: True while in its code
    for token in re.split(r"([\x13\x14\x15])", text):
        if token == "\x13":
            fields.append(True)
        elif token == "\x14":
            if fields:
                fields[-1] = False
        elif token == "\x15":
            if fields:
                fields.pop()
        elif not any(fields):
            out.append(token)
    return "".join(out)


def doc(data: bytes) -> tuple[str, list[Picture]]:
    """A Word 97-2003 file's text (body, notes, headers, text boxes) and pictures, with their alt text."""
    cf = Compound(data)
    word = cf.stream("WordDocument")
    if len(word) < 0x22 or struct.unpack_from("<H", word, 0)[0] != 0xA5EC:
        raise Damaged("not a Word document")
    version, flags = struct.unpack_from("<H", word, 2)[0], struct.unpack_from("<H", word, 0x0A)[0]
    if flags & 0x0100:
        raise Locked("locked with a password")
    if version < 0xC0:   # Word 6 and 95: one run of 8-bit text
        start, stop = struct.unpack_from("<II", word, 0x18)
        return _without_field_codes(word[start:stop].decode("cp1252", "replace")).translate(_WORD_CHARS), []
    table = cf.stream("1Table" if flags & 0x0200 else "0Table")
    pos = 32
    pos += 2 + 2 * struct.unpack_from("<H", word, pos)[0]
    counts = struct.unpack_from(f"<{struct.unpack_from('<H', word, pos)[0]}i", word, pos + 2)
    pos += 2 + 4 * len(counts)
    n_pairs = struct.unpack_from("<H", word, pos)[0]
    pairs = struct.unpack_from(f"<{2 * n_pairs}I", word, pos + 2)

    def pair(i: int) -> tuple[int, int]:
        return (pairs[2 * i], pairs[2 * i + 1]) if i < n_pairs else (0, 0)

    total = sum(max(0, c) for c in counts[3:11])   # main text, notes, headers, comments, end notes, text boxes
    at, length = pair(33)   # the piece table
    clx = table[at:at + length]
    i = 0
    while i < len(clx) and clx[i] == 1:   # formatting kept with the pieces
        i += 3 + struct.unpack_from("<h", clx, i + 1)[0]
    if i + 5 > len(clx) or clx[i] != 2:
        raise Damaged("no piece table")
    plc = clx[i + 5:i + 5 + struct.unpack_from("<I", clx, i + 1)[0]]
    n = (len(plc) - 4) // 12
    cps = struct.unpack_from(f"<{n + 1}I", plc, 0)
    parts = []
    for k in range(n):
        fc = struct.unpack_from("<I", plc, 4 * (n + 1) + 8 * k + 2)[0]
        count = cps[k + 1] - cps[k]
        if count <= 0:
            continue
        if fc & 0x40000000:   # 8-bit text
            at = (fc & 0x3FFFFFFF) // 2
            parts.append(word[at:at + count].decode("cp1252", "replace"))
        else:
            at = fc & 0x3FFFFFFF
            parts.append(word[at:at + 2 * count].decode("utf-16-le", "replace"))
    text = "".join(parts)
    text = _without_field_codes(text[:total] if total else text).translate(_WORD_CHARS)
    return text, _doc_pictures(cf, word, table, pair(50))


def _doc_pictures(cf: Compound, word: bytes, table: bytes, art: tuple[int, int]) -> list[Picture]:
    out: list[Picture] = []
    # Pictures in the line of text: each is a shape, then its picture, in the data stream
    data = cf.stream("Data") if cf.has("Data") else b""
    pos = 0
    while (pos := data.find(b"\x0f\x00\x04\xf0", pos)) >= 0:
        length = struct.unpack_from("<I", data, pos + 4)[0] if pos + 8 <= len(data) else 0
        end = pos + 8 + length
        if end > len(data):
            pos += 4
            continue
        placed = list(_shapes(data, pos, end))
        store = []
        for d, _, kind, n in records(data, end, len(data), deep=False):
            if kind == 0xF007:
                store.append(blip(data, d + 36 + data[d + 33]) if n > 36 else None)
            elif 0xF018 <= kind <= 0xF117:
                store.append(blip(data, d - 8))
            else:
                break
            end = d + n
        out += _pictures(placed, store)
        pos = max(end, pos + 4)
    # Floating pictures: shapes in the drawings after the picture store, all in the table stream
    at, length = art
    if length and at + length <= len(table):
        stop = at + length
        if at + 8 <= stop:
            first = at + 8 + struct.unpack_from("<I", table, at + 4)[0]
            store = _store(table, at, min(first, stop), word)
            pos = first
            while pos + 9 <= stop:   # each drawing: a byte saying where (body or header), then its container
                n = struct.unpack_from("<I", table, pos + 5)[0]
                out += _pictures(_shapes(table, pos + 1, min(pos + 9 + n, stop)), store)
                pos += 9 + n
    return out


# -- PowerPoint


def _text_atom(buf: bytes, data: int, kind: int, length: int) -> str | None:
    if kind == 0x0FA0:   # TextCharsAtom
        raw = buf[data:data + length].decode("utf-16-le", "replace")
    elif kind == 0x0FA8:   # TextBytesAtom
        raw = buf[data:data + length].decode("latin-1")
    else:
        return None
    lines = (line.strip() for line in raw.replace("\r", "\n").replace("\x0b", "\n").split("\n"))
    return "\n".join(line for line in lines if line and line not in ("*", "‹#›")) or None   # "*": a slide number


def _persist(cf: Compound, stream: bytes) -> tuple[dict[int, int], int | None]:
    """Where each persist id's record is, newest edit winning, and the document's persist id."""
    try:
        user = cf.stream("Current User")
        token, edit = struct.unpack_from("<II", user, 12)
    except (KeyError, struct.error):
        return {}, None
    if token == 0xF3D1C4DF:
        raise Locked("locked with a password")
    directories, document, seen = [], None, set()
    while edit and edit + 8 <= len(stream) and edit not in seen:
        seen.add(edit)
        if struct.unpack_from("<H", stream, edit + 2)[0] != 0x0FF5:   # UserEditAtom
            break
        last, directory, doc_ref = struct.unpack_from("<III", stream, edit + 16)
        document = document or doc_ref
        directories.append(directory)
        edit = last
    where: dict[int, int] = {}
    for directory in reversed(directories):   # oldest first, so newer entries replace older
        if directory + 8 > len(stream):
            continue
        end = directory + 8 + struct.unpack_from("<I", stream, directory + 4)[0]
        p = directory + 8
        while p + 4 <= min(end, len(stream)):
            v = struct.unpack_from("<I", stream, p)[0]
            first, count = v & 0xFFFFF, v >> 20
            for k in range(count):
                if p + 8 + 4 * k <= len(stream):
                    where[first + k] = struct.unpack_from("<I", stream, p + 4 + 4 * k)[0]
            p += 4 + 4 * count
    return where, document


def _record(stream: bytes, at: int | None, kind: int) -> tuple[int, int] | None:
    """(start, end) of the data of the record of `kind` at `at`."""
    if at is None or at + 8 > len(stream) or struct.unpack_from("<H", stream, at + 2)[0] != kind:
        return None
    return at + 8, min(len(stream), at + 8 + struct.unpack_from("<I", stream, at + 4)[0])


def ppt(data: bytes) -> tuple[list[tuple[int, str]], list[Picture]]:
    """A PowerPoint 97-2003 file's slides in show order: (number, text with alt text and speaker notes), and their
    pictures."""
    cf = Compound(data)
    if cf.has("EncryptedSummary"):
        raise Locked("locked with a password")
    stream = cf.stream("PowerPoint Document")
    delay = cf.stream("Pictures") if cf.has("Pictures") else b""
    where, document = _persist(cf, stream)
    found = _record(stream, where.get(document), 0x03E8) if document is not None else None
    if found is None:   # no edit history: the last document record in the file
        tops = [(d, d + n) for d, _, k, n in records(stream, 0, len(stream), deep=False) if k == 0x03E8]
        if not tops:
            raise Damaged("no presentation")
        found, where = tops[-1], {}
    doc_start, doc_end = found
    store = []
    for d, _, k, n in records(stream, doc_start, doc_end, deep=False):
        if k == 0x040B:   # the drawing group: the picture store
            store = _store(stream, d, d + n, delay)
    # The slide list: each slide's persist id, then the text of its title and body. The notes list: each notes
    # page's id (what a slide names its notes by) and persist id.
    listed: list[tuple[int, list[str]]] = []
    notes_pages: dict[int, int] = {}
    for d, instance, k, n in records(stream, doc_start, doc_end, deep=False):
        if k == 0x0FF0 and instance == 0:
            for d2, _, k2, n2 in records(stream, d, d + n, deep=False):
                if k2 == 0x03F3:
                    listed.append((struct.unpack_from("<I", stream, d2)[0], []))
                elif listed and (t := _text_atom(stream, d2, k2, n2)):
                    listed[-1][1].append(t)
        elif k == 0x0FF0 and instance == 2:
            for d2, _, k2, n2 in records(stream, d, d + n, deep=False):
                if k2 == 0x03F3 and n2 >= 16:
                    persist, page = struct.unpack_from("<I", stream, d2)[0], struct.unpack_from("<I", stream, d2 + 12)[0]
                    notes_pages[page] = persist
    if not where:   # no persist directory: the slides as they lie in the file
        physical = [d - 8 for d, _, k, _ in records(stream, 0, len(stream), deep=False) if k == 0x03EE]
        where = {i + 1: at for i, at in enumerate(physical)}
        listed = [(i + 1, listed[i][1] if i < len(listed) else []) for i in range(len(physical))]
    slides: list[tuple[int, str]] = []
    pictures: list[Picture] = []
    for number, (ref, texts) in enumerate(listed, 1):
        texts = list(texts)
        slide = _record(stream, where.get(ref), 0x03EE)
        notes = None
        if slide:
            for d, _, k, n in records(stream, *slide):
                if k == 0x03EF and n >= 20:   # SlideAtom: names the slide's notes page
                    page = notes_pages.get(struct.unpack_from("<I", stream, d + 16)[0])
                    notes = _record(stream, where.get(page), 0x03F0) if page is not None else None
                elif t := _text_atom(stream, d, k, n):
                    texts.append(t)
            shown = _pictures(_shapes(stream, *slide), store, number)
            pictures += shown
            texts += [p.alt for p in shown if p.alt]
        if notes:
            texts += [t for d, _, k, n in records(stream, *notes) if (t := _text_atom(stream, d, k, n))]
        unique = list(dict.fromkeys(texts))
        slides.append((number, "\n".join(unique)))
    return slides, pictures
