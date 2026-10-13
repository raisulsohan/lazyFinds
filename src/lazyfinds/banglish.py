"""Banglish: Bengali typed in Latin letters ("atoshbaji", "basha theke office er kaj"), searched in Bengali too.

Spelling. A Latin word has many Bengali spellings: "sh" is শ, ষ or স; "o" after a consonant is the inherent vowel
or ো; "i" is ি or ী; two consonants in a row are a conjunct ("jonmo" জন্ম) or two letters ("atoshbaji" আতশবাজি);
a doubled one is often a য-ফলা ("biddut" বিদ্যুৎ). Each choice has a cost (how far it is from the usual way of
writing), and a beam search over the word keeps the cheapest spellings. The words in Tesseract's Bengali dictionary
(data/bengali.txt.xz, Apache 2.0) steer it: a spelling that stops being the start of any Bengali word pays once, and
a finished spelling that is a word, or a word and one of its endings, is preferred. Among words, the common ones (a
whole word in the model's tokenizer, data/bengali_common.txt.xz) come first: "chobi" is ছবি before চবি.

English or Banglish. Every Latin word is one of:
- English: one of the 15 000 commonest English words (data/english.txt.xz, most common first), or one with an English
  ending on it ("penguins").
- Banglish: not in the English dictionary at all.
- Both: an English word that is also everyday Banglish ("rat" রাত, "kaj" কাজ, "to" তো, "er" এর), the same with a
  Bengali ending ("rate" রাতে, "kaje" কাজে), or a rarer word of the English dictionary ("pounce", but "acho" and
  "rutin" are in it too).
A Banglish word whose spelling is a known Bengali word counts. Then, if the query has a Banglish word or Bengali
letters, its Both words and its other Banglish words count too; a query of Both words alone ("ami kaj") counts when
it has no English word. Otherwise nothing is converted: "rat in the kitchen" stays English.

Measured on samples (scripts/eval.py has the queries): of 3 000 rare words of the English dictionary, none is
converted, alone or in an English sentence; of 173 everyday Banglish words, 65% are converted alone (the rest are
English words too: "bari", "kaj", "office") and 96% next to another Banglish word.
"""

from __future__ import annotations

import bisect
import itertools
import lzma
import re
import threading
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from . import text as T

DATA = Path(__file__).resolve().parent / "data"

# -- the sounds: Latin letters -> Bengali, with a cost for each less usual spelling

# Consonants. Output in lazyFinds' folded form (T.fold), like the dictionary and the index.
CONSONANTS: dict[str, list[tuple[str, float]]] = {
    "k": [("ক", 0)], "kh": [("খ", 0), ("ক্ষ", 0.5)], "kk": [("ক্ক", 0), ("ক্য", 0.4)], "kkh": [("ক্ষ", 0)],
    "ksh": [("ক্ষ", 0)], "x": [("ক্স", 0.2)], "q": [("ক", 0.3)],
    "g": [("গ", 0)], "gh": [("ঘ", 0)], "gg": [("গ্গ", 0.3), ("জ্ঞ", 0.3)], "gy": [("জ্ঞ", 0.4), ("গ্য", 0.4)],
    "ng": [("ঙ", 0.4), ("ঙ্গ", 0.5)],
    "c": [("ক", 0.4), ("চ", 0.5), ("স", 0.6)], "ch": [("চ", 0), ("ছ", 0.3)], "chh": [("ছ", 0)],
    "cch": [("চ্ছ", 0)],
    "j": [("জ", 0), ("য", 0.5)], "jh": [("ঝ", 0)], "jj": [("জ্জ", 0), ("জ্য", 0.4)], "z": [("জ", 0), ("য", 0.3)],
    "t": [("ত", 0), ("ট", 0.4)], "th": [("থ", 0), ("ঠ", 0.4)], "tt": [("ত্ত", 0), ("ত্য", 0.3), ("ট্ট", 0.4)],
    "d": [("দ", 0), ("ড", 0.4)], "dh": [("ধ", 0), ("ঢ", 0.6)], "dd": [("দ্দ", 0), ("দ্য", 0.3), ("ড্ড", 0.5)],
    "n": [("ন", 0), ("ণ", 0.5)], "nn": [("ন্ন", 0), ("ন্য", 0.3), ("ণ্ণ", 0.8)],
    "p": [("প", 0)], "ph": [("ফ", 0)], "f": [("ফ", 0)], "pp": [("প্প", 0)],
    "b": [("ব", 0)], "bh": [("ভ", 0)], "v": [("ভ", 0), ("ব", 0.4)], "bb": [("ব্ব", 0), ("ব্য", 0.3)],
    "m": [("ম", 0)], "mm": [("ম্ম", 0)],
    "r": [("র", 0), ("ড়", 0.5)], "rh": [("ঢ়", 0.3), ("ড়", 0.3)], "rr": [("ড়", 0.4), ("র্র", 1.0)],
    "l": [("ল", 0)], "ll": [("ল্ল", 0), ("ল্য", 0.3)],
    "sh": [("শ", 0), ("ষ", 0.3), ("স", 0.4)], "s": [("স", 0), ("শ", 0.3), ("ষ", 0.7)],
    "ss": [("স্য", 0.3), ("শ্ব", 0.4), ("স্ব", 0.4), ("শ্য", 0.4), ("স্স", 0.6)],
    "shsh": [("শ্ব", 0.3), ("শ্য", 0.4), ("শ্শ", 0.6)],
    "h": [("হ", 0)],
}

# Vowels: (after a consonant, on its own, cost). "" after a consonant is its inherent vowel.
VOWELS: dict[str, list[tuple[str, str, float]]] = {
    "a": [("া", "আ", 0), ("", "অ", 0.8), ("্যা", "অ্যা", 1.0)],
    "aa": [("া", "আ", 0)],
    "o": [("", "অ", 0), ("ো", "ও", 0.2)],
    "oo": [("ু", "উ", 0), ("ূ", "ঊ", 0.4)],
    "i": [("ি", "ই", 0), ("ী", "ঈ", 0.3)],
    "ee": [("ী", "ঈ", 0), ("ি", "ই", 0.2)],
    "u": [("ু", "উ", 0), ("ূ", "ঊ", 0.3)],
    "e": [("ে", "এ", 0), ("্যা", "অ্যা", 0.8)],
    "oi": [("ৈ", "ঐ", 0.5)],
    "ou": [("ৌ", "ঔ", 0.5)],
    "ow": [("ৌ", "ঔ", 0.6)],
    "ri": [("ৃ", "ঋ", 0.6)],
    "rri": [("ৃ", "ঋ", 0.2)],
}

JOIN = 0.15        # two consonants as a conjunct (জন্ম)
APART = 0.45       # two consonants with the inherent vowel between them (আতশবাজি)
SPLIT = 1.0        # a digraph read as two letters ("s"+"h" for "sh")
OFF_WORDS = 2.0    # paid once, when the spelling stops being the start of any Bengali word
WITH_ENDING = 0.6  # a finished spelling that is a word plus one of its endings
UNKNOWN = 2.0      # a finished spelling that isn't a word at all
COMMON = 0.6       # taken off a word common enough to be one of the model's tokens (half for one with an ending)
BEAM = 300

HASANTA = "্"


def _fold_tables():
    for key, options in CONSONANTS.items():
        CONSONANTS[key] = [(T.fold(b), c) for b, c in options]
    for key, options in VOWELS.items():
        VOWELS[key] = [(T.fold(s), T.fold(i), c) for s, i, c in options]


_fold_tables()
LONGEST = max(len(k) for k in list(CONSONANTS) + list(VOWELS))


# -- the word lists


class _Lists:
    def __init__(self):
        self.lock = threading.Lock()
        self._bengali: list[str] | None = None
        self._bengali_set: set[str] = set()
        self._english: dict[str, int] | None = None
        self._common: set[str] | None = None

    @staticmethod
    def _read(name: str) -> list[str]:
        try:
            return lzma.decompress((DATA / name).read_bytes()).decode("utf-8").split()
        except (OSError, lzma.LZMAError):
            return []

    @property
    def bengali(self) -> list[str]:
        with self.lock:
            if self._bengali is None:
                self._bengali = sorted(self._read("bengali.txt.xz"))
                self._bengali_set = set(self._bengali)
            return self._bengali

    def is_word(self, word: str) -> bool:
        self.bengali
        return word in self._bengali_set

    def starts_a_word(self, prefix: str) -> bool:
        words = self.bengali
        i = bisect.bisect_left(words, prefix)
        return i < len(words) and words[i].startswith(prefix)

    @property
    def common(self) -> set[str]:
        with self.lock:
            if self._common is None:
                self._common = set(self._read("bengali_common.txt.xz"))
            return self._common

    @property
    def english(self) -> dict[str, int]:
        """English word -> its place, most common first."""
        with self.lock:
            if self._english is None:
                self._english = {w: i for i, w in enumerate(self._read("english.txt.xz"))}
            return self._english


LISTS = _Lists()


def warm():
    """Loads the word lists (about 0.1 s), so the first query doesn't wait for them."""
    LISTS.bengali
    LISTS.common
    LISTS.english


ENDINGS = [T.fold(e) for e in T.BENGALI_ENDINGS + ["কে", "দের", "টা", "টি", "গুলো", "ও", "ই", "েই", "তেই", "টাই"]]


def _known(word: str) -> float | None:
    """What a finished spelling pays for being a word: less for a common word, WITH_ENDING for a word plus one of
    its endings; None when it is neither."""
    if LISTS.is_word(word) or word in LISTS.common:
        return -COMMON if word in LISTS.common else 0.0
    for ending in ENDINGS:
        stem = word[: -len(ending)]
        if word.endswith(ending) and len(stem) >= 2 and (LISTS.is_word(stem) or stem in LISTS.common):
            return WITH_ENDING - (COMMON / 2 if stem in LISTS.common else 0.0)
    return None


# -- spelling


@dataclass(frozen=True)
class Spelling:
    text: str     # folded, as in the index
    cost: float
    known: bool   # a Bengali word, or a word and an ending


def _units(word: str, i: int):
    """Every sound that starts at word[i]: (length, kind, options), kind "c" consonant or "v" vowel."""
    for n in range(min(LONGEST, len(word) - i), 0, -1):
        piece = word[i:i + n]
        if piece in CONSONANTS:
            yield n, "c", CONSONANTS[piece]
        if piece in VOWELS:
            yield n, "v", VOWELS[piece]


DIGRAPHS = {k for k in CONSONANTS if len(k) > 1} | {"aa", "oo", "ee"}


def _split_penalty(word: str, i: int, n: int) -> float:
    """A single letter read on its own where it starts a longer sound ("s" of "sh", "o" of "oo"). Not for "oi" and
    "ou": বই and কই are as usual as বৈ."""
    if n == 1 and i + 1 < len(word) and word[i:i + 2] in DIGRAPHS:
        return SPLIT if word[i + 1] == "h" or word[i] == word[i + 1] else SPLIT / 2
    return 0.0


def _consonant_next(word: str, at: int) -> bool:
    return at < len(word) and word[at] not in "aeiouwy"


def spell(word: str, k: int = 4) -> tuple[Spelling, ...]:
    """The k likeliest Bengali spellings of a Latin word (a-z), best first."""
    return spellings(word.lower())[:k]


@lru_cache(maxsize=4096)
def spellings(word: str) -> tuple[Spelling, ...]:
    """The likeliest Bengali spellings of a Latin word (lower case a-z), best first; at most eight."""
    if not word or not word.isascii() or not word.isalpha():
        return ()
    # A state: (cost with penalties, text, kind of the last sound: "s" start, "c" consonant, "v" vowel, "x" a sign
    # after which nothing joins (ং ঃ ৎ), whether the text still starts a Bengali word).
    beams: list[dict[tuple[str, str], tuple[float, bool]]] = [dict() for _ in range(len(word) + 1)]
    beams[0][("", "s")] = (0.0, True)

    def add(at: int, text: str, kind: str, cost: float, inside: bool):
        if inside and not LISTS.starts_a_word(text):
            inside = False
            cost += OFF_WORDS
        key = (text, kind)
        old = beams[at].get(key)
        if old is None or cost < old[0]:
            beams[at][key] = (cost, inside)

    for i in range(len(word)):
        states = sorted(beams[i].items(), key=lambda kv: kv[1][0])[:BEAM]
        for (text, last), (cost, inside) in states:
            letter = word[i]
            for n, kind, options in _units(word, i):
                extra = _split_penalty(word, i, n)
                if kind == "c":
                    for bengali, c in options:
                        if last == "c":
                            add(i + n, text + HASANTA + bengali, "c", cost + c + extra + JOIN, inside)
                            add(i + n, text + bengali, "c", cost + c + extra + APART, inside)
                        else:
                            add(i + n, text + bengali, "c", cost + c + extra, inside)
                else:
                    for sign, alone, c in options:
                        if last == "c":
                            add(i + n, text + sign, "v", cost + c + extra, inside)
                        elif not (word[i:i + n] == "ri" and last == "v"):
                            add(i + n, text + alone, "v", cost + c + extra, inside)
            # y and w depend on what comes before them.
            if letter == "y":
                if last == "c" and text.endswith("্য"):   # "biddyut": the doubled letter was the য-ফলা already
                    add(i + 1, text, "c", cost + 0.1, inside)
                elif last == "c":
                    add(i + 1, text + "্য", "c", cost, inside)
                else:
                    add(i + 1, text + T.fold("য়"), "c", cost, inside)
                    add(i + 1, text + "য", "c", cost + 0.5, inside)
                    if last == "s":
                        add(i + 1, text + "ই", "v", cost + 0.6, inside)
            elif letter == "n" and (word.startswith("ch", i + 1) or word.startswith("j", i + 1)):
                add(i + 1, text + ("্" if last == "c" else "") + "ঞ", "c", cost + 0.2, inside)   # shonchoy সঞ্চয়
            elif letter == "n" and word[i + 1:i + 2] in ("k", "g") and not word.startswith("ng", i):
                add(i + 1, text + ("্" if last == "c" else "") + "ঙ", "c", cost + 0.3, inside)   # ongko অঙ্ক
            elif letter == "w":
                if last == "c":
                    add(i + 1, text + "্ব", "c", cost + 0.3, inside)
                    add(i + 1, text + "ো", "v", cost + 0.6, inside)
                else:
                    add(i + 1, text + "ও", "v", cost, inside)
                    if word[i + 1:i + 2] in ("a", "e", "i"):   # khawa খাওয়া, hawa হাওয়া
                        add(i + 1, text + "ও" + T.fold("য়"), "c", cost, inside)
            # Signs that close a syllable: ং for "ng", ঃ for "h", ৎ for "t", ঁ for "n" before a consonant.
            if last in ("c", "v"):
                if word.startswith("ng", i) and (i + 2 == len(word) or _consonant_next(word, i + 2)):
                    add(i + 2, text + "ং", "x", cost, inside)
                elif word.startswith("ng", i):
                    add(i + 2, text + "ং", "x", cost + 0.6, inside)
                if letter == "h" and (i + 1 == len(word) or _consonant_next(word, i + 1)) and last == "v":
                    add(i + 1, text + "ঃ", "x", cost + 0.9, inside)
                if letter == "t" and (i + 1 == len(word) or (_consonant_next(word, i + 1) and word[i + 1] != "h")):
                    add(i + 1, text + "ৎ", "x", cost + 0.35, inside)
                if letter == "n" and last == "v" and _consonant_next(word, i + 1) and word[i + 1] != "g":
                    add(i + 1, text + "ঁ", "v", cost + 0.9, inside)

    finished: dict[str, Spelling] = {}
    for (text, last), (cost, inside) in beams[len(word)].items():
        if not text or text.endswith(HASANTA):
            continue
        known = _known(text)
        # OFF_WORDS only steered the search; a finished spelling pays for what it is.
        total = cost - (0.0 if inside else OFF_WORDS) + (known if known is not None else UNKNOWN)
        old = finished.get(text)
        if old is None or total < old.cost:
            finished[text] = Spelling(text, round(total, 3), known is not None)
    return tuple(sorted(finished.values(), key=lambda s: (s.cost, len(s.text)))[:8])


# -- English or Banglish

# Everyday Banglish words that are in the English list too (most are whole-word tokens of the model, which learned
# them from Banglish on the web). With Banglish around them they are read as Banglish, even when the list has them
# among the commonest English words.
BOTH = set("""
ami amar amake amra amader tumi tomar tomake tomra apni apnar apnara se tar take tara tader o or ora oder era eder
ei eta ekta ekti oi ota sei seta kono keu ki ke kar kake kothay koi kokhon keno kemon koto je ja jar jodi tobe kintu
ar aar ba na nai nei ha accha thik er re te theke jonno diye niye sathe songe moddhe pore age kache pashe upore niche
kori koro kore korbo kole hoy hoye hobe holo hole chilo ache achi ase jabo jai jao jay gelo dekho dekhi bolo boli
khai khao pai dao dibo nibo rakho rakhi chai chao lage pari bari basha ghor ghar kaj kam din rat raat mon man ma baba
bhai bon chele meye bou lok desh gram dokan bazar taka bil jol pani cha dudh bhat dal mach dim alu tel lal nil kalo
sob shob kom beshi bhalo valo boro choto notun nam gan gaan khela pore mane tala chabi ghori phul gach boi path to
mal kal aj ajke kalke ekhon tokhon ekhane okhane shudhu
""".split())

ENGLISH_ENDINGS = (("ies", "y"), ("es", ""), ("s", ""), ("ed", ""), ("ed", "e"), ("ing", ""), ("ing", "e"), ("ly", ""))
COMMON_ENGLISH = 15_000


def english(word: str) -> str | None:
    """"common" for one of the commonest English words, or one with an English ending ("penguins", "skiing");
    "rare" for another word of the list; None for a word that isn't English."""
    place = LISTS.english.get(word)
    if place is not None:
        return "common" if place < COMMON_ENGLISH else "rare"
    for ending, back in ENGLISH_ENDINGS:
        stem = word[: -len(ending)] + back
        if word.endswith(ending) and len(stem) >= 3 and not everyday(stem):
            place = LISTS.english.get(stem)
            if place is not None:
                return "common" if place < COMMON_ENGLISH else "rare"
    return None


BANGLISH_ENDINGS = ("e", "er", "te", "ke", "ra", "der", "r", "o", "i")


def everyday(word: str) -> bool:
    """An everyday Banglish word, as it is or with a Bengali ending ("rate" রাতে, "kajer" কাজের)."""
    return word in BOTH or any(word.endswith(e) and word[: -len(e)] in BOTH and len(word) - len(e) >= 2
                               for e in BANGLISH_ENDINGS)


def latin(word: str) -> bool:
    return len(word) >= 2 and word.isascii() and word.isalpha()


def decide(words: list[str], bengali_letters: bool = False) -> list[bool]:
    """Which of the query's words (folded) are Banglish. `bengali_letters`: the query also has words in Bengali."""
    kinds = []
    for w in words:
        if not latin(w):
            kinds.append("skip")
        elif english(w):
            kinds.append("en" if english(w) == "common" and not everyday(w) else "both")
        else:
            kinds.append("bn")
    best = {w: spell(w, 1) for w, kind in zip(words, kinds) if kind in ("bn", "both")}
    sure = [kind == "bn" and bool(best[w]) and best[w][0].known for w, kind in zip(words, kinds)]
    context = any(sure) or bengali_letters or ("en" not in kinds and kinds.count("both") >= 2)
    return [kind != "skip" and kind != "en" and (sure[i] or context) for i, kind in enumerate(kinds)]


# -- a query in both forms


@dataclass
class Conversion:
    words: dict[str, list[str]]   # Latin word (folded) -> its Bengali spellings (folded), likeliest first
    text: str                     # the query with each of those words in its likeliest spelling

    @property
    def shown(self) -> str:
        return unicodedata.normalize("NFC", self.text)


WORD = re.compile(r"[A-Za-z]+")


def convert(query: str, k: int = 3) -> Conversion | None:
    """The query's Banglish words and their spellings; None when it has none."""
    folded = T.fold(query)
    words = T.split_words(folded)
    if not any(latin(w) for w in words):
        return None
    banglish = decide(words, bengali_letters=any(T.is_bengali(c) for c in folded))
    chosen: dict[str, list[str]] = {}
    for w, yes in zip(words, banglish):
        if yes and w not in chosen:
            spellings = spell(w, k + 2)
            if spellings:
                best = spellings[0].cost
                chosen[w] = [s.text for s in spellings if s.cost <= best + 1.0 and (s.known or not spellings[0].known)
                             ][:k]
    if not chosen:
        return None
    text = WORD.sub(lambda m: chosen[m.group(0).lower()][0] if m.group(0).lower() in chosen else m.group(0), query)
    return Conversion(chosen, text)


# Folding splits য় ড় ঢ় into a letter and a nukta; a file name typed on a keyboard may have them as one letter.
COMPOSED = {"য়": "য়", "ড়": "ড়", "ঢ়": "ঢ়"}


def name_queries(query: str, conversion: Conversion, limit: int = 8) -> list[str]:
    """Name searches for the Bengali forms of a query, likeliest first (the query as typed isn't among them)."""
    pieces = query.split()
    options = []
    for piece in pieces:
        forms = conversion.words.get(T.fold(piece))
        options.append([unicodedata.normalize("NFC", f) for f in forms] if forms else [piece])
    out: list[str] = []
    for combo in sorted(itertools.product(*[range(len(o)) for o in options]), key=sum)[:limit]:
        q = " ".join(o[i] for o, i in zip(options, combo))
        for variant in (q, _composed(q)):
            if variant not in out:
                out.append(variant)
    return out


def _composed(text: str) -> str:
    for split, whole in COMPOSED.items():
        text = text.replace(split, whole)
    return text
