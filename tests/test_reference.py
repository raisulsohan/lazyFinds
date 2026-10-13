"""llama.cpp's vectors against Google's official EmbeddingGemma 2 (sentence-transformers, float32), on our own inputs.

tools/reference/make_reference.py made tests/reference/vectors.json. Every backend lazyFinds runs on (CUDA, Vulkan,
CPU) must pass this before it's used: an index never mixes vectors that don't compare. Needs the model files.
"""

import json
from pathlib import Path

import numpy as np
import pytest
import soundfile
from PIL import Image

from lazyfinds import engine as E
from lazyfinds import paths

REF = Path(__file__).parent / "reference"
AGREEMENT = 0.995
TEXTS = {
    "query-en": E.query_prompt("why won't my washing machine drain"),
    "query-bn": E.query_prompt("ছাদে টমেটো চাষ"),
    "query-code": E.code_query_prompt("read a csv file and sum one column"),
    "doc-en": E.document_prompt("Saturday baking", "Cream the butter with brown sugar, beat in one egg, fold in flour "
                                "and chocolate chips, then bake small balls of dough for eleven minutes."),
    "doc-bn": E.document_prompt("খাতা", "বড় টবে দোআঁশ মাটি আর জৈব সার মেশান। প্রতিদিন অন্তত ছয় ঘণ্টা রোদ লাগে।"),
}


def reference() -> dict:
    return json.loads((REF / "vectors.json").read_text(encoding="utf-8"))


def made_vectors(backend: str | None) -> dict[str, np.ndarray]:
    made = {}
    with E.Engine(backend=backend) as engine:
        for name, v in zip(TEXTS, engine.embed_texts(list(TEXTS.values()))):
            made[name] = v
        for budget in (280, 140, 70):
            for stem, file in (("bicycle", "bicycle.jpg"), ("banking", "banking.png")):
                made[f"{stem}-{budget}"] = engine.embed_images([Image.open(REF / file)], budget)[0]
        audio, rate = soundfile.read(REF / "speech.wav", dtype="int16")
        seconds = len(audio) / rate
        for cut in (8, int(seconds)):
            made[f"speech-{cut}s"] = engine.embed_audio([E.wav_bytes(audio[: cut * rate])])[0]
    return made


def check(backend: str | None):
    expected = reference()["vectors"]
    made = made_vectors(backend)
    assert set(made) == set(expected)
    worst = []
    for name, vector in sorted(made.items()):
        assert not isinstance(vector, Exception), f"{name}: {vector}"
        cosine = float(np.dot(vector, np.asarray(expected[name], dtype=np.float32)))
        worst.append((cosine, name))
    print(f"\n{backend or 'default'}: " + ", ".join(f"{n} {c:.4f}" for c, n in sorted(worst)[:4]))
    bad = [(n, round(c, 4)) for c, n in worst if c < AGREEMENT]
    assert not bad, f"{backend}: vectors differ from the official model: {bad}"


@pytest.mark.skipif(not paths.models_ready(), reason="model files not downloaded")
@pytest.mark.parametrize("backend", [b for b in ("cuda", "vulkan", "cpu") if paths.backend_ready(b)])
def test_backend_matches_official_model(backend):
    check(backend)


def test_image_size_rule():
    # The Gemma image processor: fill the token budget with 48 px tiles (16 px patches pooled 3 x 3), keep the shape.
    assert E.target_size(900, 1440, 280) == (624, 1008)
    assert E.target_size(1066, 1600, 70) == (288, 480)
