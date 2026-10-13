"""Builds the two word lists Banglish search uses, from files lazyFinds already downloads, both Apache 2.0:

- bengali.txt.xz: every word in the dictionary inside Tesseract's Bengali model (tessdata_best ben.traineddata),
  folded as lazyFinds folds text. The transliterator prefers spellings that are real Bengali words.
- bengali_common.txt.xz: the Bengali words that are a whole word in EmbeddingGemma 2's tokenizer (Apache 2.0). A
  tokenizer gives whole-word tokens to the words that are common on the web, so these are the usual spellings ("ছবি",
  not "চবি").
- english.txt.xz: the lower-case words of the dictionary inside Tesseract's English model (83 000). First the ones
  that are also a whole word in EmbeddingGemma 2's tokenizer, in the tokenizer's order (roughly most common first):
  a tokenizer gives whole-word tokens to words common on the web. Then the rest, alphabetically. The dictionary has
  rare words that are everyday Banglish too ("kaj", "bari"), so only the head of the list is sure English.

Needs Tesseract (combine_tessdata.exe and dawg2wordlist.exe come with it), the OCR models (Settings → "Set up…") and
the model. Run it again only if those pinned files change.

    uv run python scripts/make_wordlists.py
"""

from __future__ import annotations

import lzma
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

from lazyfinds import paths
from lazyfinds import text as T

OUT = Path(__file__).resolve().parent.parent / "src" / "lazyfinds" / "data"


def dictionary(traineddata: Path, lang: str) -> list[str]:
    tesseract = paths.tesseract_exe()
    if tesseract is None:
        raise SystemExit("Tesseract isn't installed")
    tools = tesseract.parent
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        copy = work / traineddata.name
        copy.write_bytes(traineddata.read_bytes())
        subprocess.run([str(tools / "combine_tessdata.exe"), "-u", str(copy), str(work / lang) + "."], check=True,
                       capture_output=True)
        out = work / "words.txt"
        subprocess.run([str(tools / "dawg2wordlist.exe"), str(work / f"{lang}.lstm-unicharset"),
                        str(work / f"{lang}.lstm-word-dawg"), str(out)], check=True, capture_output=True)
        return out.read_text(encoding="utf-8").split()


def gguf_tokens(model: Path) -> list[str]:
    """tokenizer.ggml.tokens from a GGUF file's header."""
    sizes = {0: "B", 1: "b", 2: "H", 3: "h", 4: "I", 5: "i", 6: "f", 7: "?", 10: "Q", 11: "q", 12: "d"}
    with open(model, "rb") as f:
        def read(fmt):
            return struct.unpack("<" + fmt, f.read(struct.calcsize("<" + fmt)))[0]

        def string():
            return f.read(read("Q")).decode("utf-8", "replace")

        def value(kind):
            if kind == 8:
                return string()
            if kind == 9:
                inner, n = read("I"), read("Q")
                return [value(inner) for _ in range(n)]
            return read(sizes[kind])

        if f.read(4) != b"GGUF":
            raise SystemExit(f"{model} isn't a GGUF file")
        read("I")
        read("Q")
        for _ in range(read("Q")):
            key, kind = string(), read("I")
            if key == "tokenizer.ggml.tokens":
                return value(kind)
            value(kind)
    raise SystemExit("no tokenizer in the model")


def save(name: str, words: list[str]) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    data = ("\n".join(words) + "\n").encode("utf-8")
    (OUT / name).write_bytes(lzma.compress(data, preset=9))
    print(f"{name}: {len(words)} words, {len((OUT / name).read_bytes()) // 1024} KB")


def main() -> int:
    tessdata = paths.tessdata_dir()
    bengali = set()
    for word in dictionary(tessdata / "ben.traineddata", "ben"):
        word = T.fold(word)
        if len(word) >= 2 and all(T.is_bengali(c) for c in word):
            bengali.add(word)
    save("bengali.txt.xz", sorted(bengali))

    tokens = list(dict.fromkeys(t[1:] for t in gguf_tokens(paths.models_dir() / paths.TEXT_MODEL.name)
                                if t.startswith("▁")))
    save("bengali_common.txt.xz", sorted({T.fold(t) for t in tokens
                                          if len(t) >= 2 and all(T.is_bengali(c) for c in t)}))
    words = {w for w in dictionary(tessdata / "eng.traineddata", "eng")
             if len(w) >= 2 and w.isascii() and w.isalpha() and w.islower()}
    english = [t for t in tokens if t in words]
    english += sorted(words - set(english))
    save("english.txt.xz", english)
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
