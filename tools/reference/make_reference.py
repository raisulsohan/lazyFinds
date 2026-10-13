"""Makes lazyFinds' reference vectors with Google's official EmbeddingGemma 2 (sentence-transformers, float32, CPU).

The inputs are our own: two pictures and a recording from scripts/make_testbed.py-style drawing and Windows' speech
voices, and our own English and Bengali texts. tests/test_reference.py then checks that llama.cpp makes the same
vectors (cosine >= 0.995) on every backend we ship.

    cd tools/reference
    uv run python make_reference.py <testbed folder> ../../tests/reference
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np
import soundfile
import torch
from PIL import Image
from sentence_transformers import SentenceTransformer

MODEL = "google/embeddinggemma-2"
REVISION = "914f7f89142e33e77833254d9c9b90c3cef7303b"

TEXTS = {
    "query-en": "task: search result | query: why won't my washing machine drain",
    "query-bn": "task: search result | query: ছাদে টমেটো চাষ",
    "query-code": "task: code retrieval | query: read a csv file and sum one column",
    "doc-en": "title: Saturday baking | text: Cream the butter with brown sugar, beat in one egg, fold in flour "
              "and chocolate chips, then bake small balls of dough for eleven minutes.",
    "doc-bn": "title: খাতা | text: বড় টবে দোআঁশ মাটি আর জৈব সার মেশান। প্রতিদিন অন্তত ছয় ঘণ্টা রোদ লাগে।",
}
SPEECH = ("The train to the coast leaves every hour from platform four. Buy your ticket at the machine before you "
          "board, and keep it until the end of the trip.")


def make_inputs(testbed: Path, out: Path):
    out.mkdir(parents=True, exist_ok=True)
    shutil.copy(testbed / "Photos" / "PXL_20260412_081500.jpg", out / "bicycle.jpg")
    shutil.copy(testbed / "Photos" / "Screenshots" / "Screenshot 2026-09-02 093012.png", out / "banking.png")
    wav = out / "speech.wav"
    if not wav.exists():
        raw = out / "speech-22k.wav"
        subprocess.run(["powershell", "-NoProfile", "-Command",
                        "Add-Type -AssemblyName System.Speech; $s = New-Object System.Speech.Synthesis."
                        f"SpeechSynthesizer; $s.SelectVoice('Microsoft David Desktop'); $s.SetOutputToWaveFile('{raw}'); "
                        f"$s.Speak('{SPEECH}'); $s.Dispose()"], check=True)
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(raw), "-ac", "1", "-ar", "16000", str(wav)], check=True)
        raw.unlink()


def main(testbed: Path, out: Path):
    make_inputs(testbed, out)
    torch.set_grad_enabled(False)
    model = SentenceTransformer(MODEL, revision=REVISION, device="cpu", model_kwargs={"torch_dtype": torch.float32})
    vectors: dict[str, list[float]] = {}
    for name, text in TEXTS.items():
        vectors[name] = model.encode(text, normalize_embeddings=True).tolist()
    processor = model[0].processor
    images = processor.image_processor
    for budget in (280, 140, 70):
        images.max_soft_tokens = budget
        for name in ("bicycle.jpg", "banking.png"):
            v = model.encode({"image": Image.open(out / name).convert("RGB")}, normalize_embeddings=True)
            vectors[f"{Path(name).stem}-{budget}"] = v.tolist()
    images.max_soft_tokens = 280
    audio, rate = soundfile.read(out / "speech.wav", dtype="float32")
    assert rate == 16000, rate
    seconds = len(audio) / rate
    for cut in (8, int(seconds)):
        piece = audio[: cut * rate]
        vectors[f"speech-{cut}s"] = model.encode({"audio": {"array": piece, "sampling_rate": rate}},
                                                 normalize_embeddings=True).tolist()
    meta = {"model": MODEL, "revision": REVISION, "dtype": "float32", "device": "cpu",
            "speech_seconds": round(seconds, 2), "vectors": vectors}
    (out / "vectors.json").write_text(json.dumps(meta), encoding="utf-8")
    names = list(vectors)
    m = np.array([vectors[n] for n in names])
    print(f"{len(names)} vectors, {m.shape[1]} dims, speech {seconds:.1f} s")
    print("bicycle-280 vs 140:", float(m[names.index("bicycle-280")] @ m[names.index("bicycle-140")]))
    print(f"speech 8s vs full:", float(m[names.index("speech-8s")] @ m[names.index(f"speech-{int(seconds)}s")]))


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]))
