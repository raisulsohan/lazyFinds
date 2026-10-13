"""Words: how text is kept for exact-word search, how a query becomes FTS5 terms, and how long text is cut into
passages for the model.

- Text is folded the same way on both sides: Unicode NFKC, case-folded, invisible format characters (zero-width
  joiners, direction marks, soft hyphens: category Cf) removed, and Bengali digits written as ASCII digits, so
  "৮৮১২৭৫" and "881275" are the same word.
- English endings are SQLite's Porter stemmer's job. Bengali attaches its endings to the word ("টমেটোর", "চারাগুলো"),
  so a Bengali query word loses a known ending and matches as a prefix.
"""

from __future__ import annotations

import re
import unicodedata

BENGALI_DIGITS = str.maketrans("০১২৩৪৫৬৭৮৯", "0123456789")


def is_bengali(ch: str) -> bool:
    return "ঀ" <= ch <= "৿"


def fold(text: str) -> str:
    """The form text is indexed and searched in."""
    text = unicodedata.normalize("NFKC", text).casefold().translate(BENGALI_DIGITS)
    return "".join(ch for ch in text if unicodedata.category(ch) != "Cf")


def is_word_char(ch: str) -> bool:
    return unicodedata.category(ch)[0] in "LNM"   # letters, digits, and marks (Bengali vowel signs)


def split_words(text: str) -> list[str]:
    return re.findall(r"[\wঀ-৿]+", text)


# -- queries

ENGLISH_STOP = set("""a an and are as at be but by can do does for from had has have how i if in into is it its me my
of on or our so that the their them there these they this to was we were what when where which who why will with you
your find show me any some""".split())
BENGALI_STOP = set("""এবং ও আর কি কী কে কোন কোথায় যে যা এই সেই ওই একটা একটি আমার আমি আমাদের তার তাদের থেকে জন্য
নিয়ে দিয়ে সাথে মধ্যে করে হয় হবে ছিল আছে খুঁজে দেখাও দাও""".split())

# Words that name a kind of file: the query asks for that kind ("…in a video", "ভিডিওতে").
KIND_WORDS = {
    "photo": "image", "photos": "image", "picture": "image", "pictures": "image", "image": "image", "images": "image",
    "screenshot": "screenshot", "screenshots": "screenshot", "screen": "screenshot",
    "pdf": "pdf", "pdfs": "pdf", "manual": "pdf", "scan": "pdf", "scanned": "pdf",
    "document": "doc", "documents": "doc", "doc": "doc", "docs": "doc", "note": "doc", "notes": "doc",
    "audio": "audio", "recording": "audio", "recordings": "audio", "podcast": "audio", "song": "audio",
    "voice": "audio", "video": "video", "videos": "video", "clip": "video", "movie": "video", "film": "video",
    "ছবি": "image", "ফটো": "image", "স্ক্রিনশট": "screenshot", "পিডিএফ": "pdf", "নথি": "doc", "নোট": "doc",
    "অডিও": "audio", "রেকর্ডিং": "audio", "গান": "audio", "ভিডিও": "video", "ভিডিওতে": "video", "ক্লিপ": "video",
}

# Bengali case and plural endings, longest first. A word keeps at least two letters.
BENGALI_ENDINGS = ["গুলোর", "গুলোতে", "গুলো", "গুলি", "দেরকে", "দের", "টিতে", "টার", "টির", "েরা", "য়ের", "তে", "কে",
                   "ের", "টি", "টা", "রা", "র", "য়", "ে"]


def bengali_stem(word: str) -> str:
    for ending in BENGALI_ENDINGS:
        if word.endswith(ending) and len(word) - len(ending) >= 2:
            return word[: -len(ending)]
    return word


def query_terms(query: str) -> list[str]:
    """The query's searchable words, folded, without stopwords and words that only name a kind of file."""
    out: list[str] = []
    for word in split_words(fold(query)):
        if len(word) < 2 or word in ENGLISH_STOP or word in BENGALI_STOP or word in KIND_WORDS:
            continue
        if word not in out:
            out.append(word)
    return out


def fts_term(word: str, typing: bool = False) -> str:
    """One FTS5 term: Bengali as a prefix of its stem, the word being typed as a prefix, the rest whole."""
    if any(is_bengali(c) for c in word):
        return '"' + bengali_stem(word) + '"*'
    return '"' + word.replace('"', "") + '"' + ("*" if typing else "")


def kinds_asked(query: str) -> set[str]:
    return {KIND_WORDS[w] for w in split_words(fold(query)) if w in KIND_WORDS}


def is_code_like(word: str) -> bool:
    """IDs, codes and long numbers ("txn", "58k3q", "881275"): things a vector can't hold, so words decide."""
    return any(c.isdigit() for c in word) and (len(word) >= 4 or any(c.isalpha() for c in word))


# -- passages for the model


SENTENCE_END = re.compile(r"(?<=[.!?।])\s+|\n{2,}")


def passages(text: str, size: int = 1200, limit: int = 4000) -> list[str]:
    """Cuts text into passages of about `size` characters (~300 tokens) at sentence ends, each starting with the last
    sentence of the one before, so a thought that spans the cut is whole in one of them."""
    text = re.sub(r"[ \t ]+", " ", text.replace("\r\n", "\n")).strip()
    if not text:
        return []
    sentences = [s.strip() for s in SENTENCE_END.split(text) if s and s.strip()]
    out: list[str] = []
    current: list[str] = []
    length = 0
    for sentence in sentences:
        while len(sentence) > size:   # a sentence longer than a passage (a table, a list without full stops)
            cut = sentence.rfind(" ", 0, size)
            cut = cut if cut > size // 2 else size
            if current:
                out.append(" ".join(current))
                current, length = [], 0
            out.append(sentence[:cut].strip())
            sentence = sentence[cut:].strip()
        if length + len(sentence) > size and current:
            out.append(" ".join(current))
            current, length = [current[-1]] if len(current[-1]) < size // 3 else [], 0
            length = sum(len(s) + 1 for s in current)
        current.append(sentence)
        length += len(sentence) + 1
        if len(out) >= limit:
            break
    if current and len(out) < limit:
        out.append(" ".join(current))
    return out


def mostly_words(text: str) -> bool:
    """True when letters make up at least 40% of what isn't space. A table of numbers isn't worth a meaning vector;
    its exact words still are."""
    letters = other = 0
    for ch in text:
        if ch.isspace():
            continue
        if unicodedata.category(ch)[0] in "LM":
            letters += 1
        else:
            other += 1
    return letters >= 3 and letters >= 0.4 * (letters + other)


def preview(text: str, limit: int = 200) -> str:
    """One line for the results list."""
    line = re.sub(r"\s+", " ", text[: limit * 4]).strip()
    return line if len(line) <= limit else line[: limit - 1].rstrip() + "…"


def word_spans(text: str, terms: list[str]) -> tuple[str, list[tuple[int, int]]]:
    """Where the query's words are in `text`, as (start, end) spans of whole words, sorted and not overlapping. Spans
    are in `text` as written; if folding changed its length (rare ligatures), in the folded text, which is returned."""
    folded = fold(text)
    if len(folded) != len(text):
        text = folded
    spans = []
    for term in terms:
        stem = bengali_stem(term) if any(is_bengali(c) for c in term) else term
        for m in re.finditer(re.escape(stem), folded):
            a, b = m.start(), m.end()
            if a > 0 and is_word_char(folded[a - 1]):
                continue
            while b < len(folded) and is_word_char(folded[b]):   # the whole word it starts ("drain" → "drains")
                b += 1
            spans.append((a, b))
    spans.sort()
    kept: list[tuple[int, int]] = []
    for a, b in spans:
        if not kept or a >= kept[-1][1]:
            kept.append((a, b))
    return text, kept


def mark(text: str, terms: list[str], width: int = 180) -> str | None:
    """A line of `text` around the first query word it holds, the words marked «like this». None when it has none."""
    text, spans = word_spans(text, terms)
    if not spans:
        return None
    start = max(0, spans[0][0] - width // 3)
    end = min(len(text), start + width)
    out, cursor = [], start
    for a, b in spans:
        if a < cursor or b > end:
            continue
        out.append(text[cursor:a] + "«" + text[a:b] + "»")
        cursor = b
    out.append(text[cursor:end])
    line = re.sub(r"\s+", " ", "".join(out)).strip()
    return ("…" if start > 0 else "") + line + ("…" if end < len(text) else "")
