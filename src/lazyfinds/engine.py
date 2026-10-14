"""EmbeddingGemma 2 on llama.cpp's server (CUDA, Vulkan or CPU), in a child process on 127.0.0.1.

Text, images and audio all land in one 768-dimensional space, mean-pooled and L2-normalized. The server is started on a
free port with a random API key, inside a Windows job object, so it never outlives lazyFinds and nothing else on the
machine can use it. Images are resized here exactly as Google's processor resizes them (checked against transformers'
own function), then sent as PNG so the server's own resizing never runs (`--image-min-tokens 1`).
tests/test_reference.py checks every backend against the official model.
"""

from __future__ import annotations

import base64
import ctypes
import http.client
import io
import json
import logging
import math
import os
import re
import secrets
import socket
import struct
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from ctypes import wintypes
from pathlib import Path

import numpy as np
from PIL import Image

from . import paths

logger = logging.getLogger("lazyfinds")

DIM = 768
BUDGETS = (70, 140, 280, 560, 1120)
SLOTS = 4
MAX_TOKENS = 2048          # one input must fit one micro-batch (non-causal model, no KV cache between inputs)
VECTOR_SOURCE = f"embeddinggemma-2@{paths.MODEL_REVISION[:7]} q8_0 llama.cpp-{paths.LLAMA_BUILD} vectors1"


# The task prefixes from Google's model card (text only; pictures and sound take none).
def query_prompt(text: str) -> str:
    return f"task: search result | query: {text}"


def code_query_prompt(text: str) -> str:
    return f"task: code retrieval | query: {text}"


def document_prompt(title: str | None, text: str) -> str:
    return f"title: {title or 'none'} | text: {text}"


TILE = 48   # the vision encoder reads 16 px patches and pools them 3 x 3: a soft token covers 48 x 48 px


def target_size(height: int, width: int, budget: int) -> tuple[int, int] | None:
    """The size Google's image processor gives a picture for `budget` soft tokens: the same shape, scaled to cover
    budget x 48 x 48 pixels, each side cut down to whole tiles. A very thin picture keeps one tile on its short side.
    Same results as transformers' get_aspect_ratio_preserving_size (15,000 random sizes compared)."""
    scale = math.sqrt(budget * TILE * TILE / (height * width))
    tiles_h, tiles_w = int(scale * height / TILE), int(scale * width / TILE)
    if tiles_h == 0 and tiles_w == 0:
        return None
    if tiles_h == 0:
        tiles_h, tiles_w = 1, min(width // height, budget)
    elif tiles_w == 0:
        tiles_w, tiles_h = 1, min(height // width, budget)
    return tiles_h * TILE, tiles_w * TILE


def image_payload(image: Image.Image, budget: int) -> str:
    """The image at its final size for `budget`, as base64 PNG (lossless, so the server sees these exact pixels)."""
    image = image.convert("RGB")
    size = target_size(image.height, image.width, budget)
    if size is None:
        raise ValueError("image too thin to embed")
    if size != (image.height, image.width):
        image = image.resize((size[1], size[0]), Image.Resampling.BICUBIC)
    buffer = io.BytesIO()
    image.save(buffer, "PNG", compress_level=1)
    return base64.b64encode(buffer.getvalue()).decode("ascii")


AUDIO_RATE = 16_000   # the model card: mono, 16 kHz


def wav_bytes(samples: np.ndarray) -> bytes:
    """16-bit mono PCM at 16 kHz, as a WAV file in memory."""
    data = np.asarray(samples, dtype="<i2").tobytes()
    fmt = struct.pack("<HHIIHH", 1, 1, AUDIO_RATE, AUDIO_RATE * 2, 2, 16)
    return (b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVE" + b"fmt " + struct.pack("<I", 16) + fmt +
            b"data" + struct.pack("<I", len(data)) + data)


class EngineError(RuntimeError):
    pass


# Windows job object: the server dies with us, whichever way we exit.
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


class _IOCounters(ctypes.Structure):
    _fields_ = [(n, ctypes.c_ulonglong) for n in ("r", "w", "o", "rb", "wb", "ob")]


class _BasicLimits(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", _BasicLimits), ("IoInfo", _IOCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]


_kernel32.CreateJobObjectW.restype = wintypes.HANDLE
_kernel32.OpenProcess.restype = wintypes.HANDLE
_JOB = None


def _kill_with_us(pid: int) -> None:
    global _JOB
    if _JOB is None:
        job = _kernel32.CreateJobObjectW(None, None)
        limits = _ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        _kernel32.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits))
        _JOB = job
    handle = _kernel32.OpenProcess(0x0101, False, pid)  # PROCESS_TERMINATE | PROCESS_SET_QUOTA
    if handle:
        _kernel32.AssignProcessToJobObject(_JOB, handle)
        _kernel32.CloseHandle(handle)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


BACKENDS = ("cuda", "vulkan", "cpu")
FAILED: set[str] = set()   # GPU backends that wouldn't start in this run; Settings clears it when the choice changes


def backends(preferred: str | None = None) -> list[str]:
    """The backends to try, best first: the one asked for (settings: "auto", "cuda", "vulkan", "cpu") when it's there,
    else CUDA on an NVIDIA PC, Vulkan on any other GPU. The processor is always last, so search still works when the GPU
    won't start: a driver too old for the CUDA 13 build, a card it has no kernels for, no Vulkan driver at all."""
    if preferred == "cpu":
        return ["cpu"]
    gpus = [preferred] if preferred in BACKENDS and paths.backend_ready(preferred) else []
    if paths.has_nvidia() and paths.backend_ready("cuda"):
        gpus.append("cuda")
    if paths.backend_ready("vulkan"):
        gpus.append("vulkan")
    return [b for b in dict.fromkeys(gpus) if b not in FAILED] + ["cpu"]


def choose_backend(preferred: str | None = None) -> str:
    """The backend the next engine starts on."""
    return backends(preferred)[0]


_STAMP = re.compile(r"^\d+\.\d+\.\d+\.\d+ ([IWED]) ")


def _reason(log: Path, since: int) -> str:
    """The server's last error line in its log after `since`, without the timestamp ("" when it wrote none)."""
    try:
        with open(log, "rb") as f:
            f.seek(since)
            lines = f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return ""
    for line in reversed(lines):
        stamp = _STAMP.match(line)
        if (stamp.group(1) == "E") if stamp else "error" in line.lower():
            return line[stamp.end() if stamp else 0:].strip()
    return ""


class Engine:
    """One llama-server. `text_only` loads just the text model (310 MB): all a search needs."""

    def __init__(self, text_only: bool = False, log: Path | None = None, backend: str | None = None):
        self.text_only = text_only
        if backend is None:
            from .settings import Settings

            backend = Settings.load().backend
        if not paths.models_ready():
            raise EngineError("the model isn't downloaded yet: run `lazyfinds setup`")
        self.marker = ""
        self._process: subprocess.Popen | None = None
        self._pool = ThreadPoolExecutor(SLOTS, thread_name_prefix="embed")
        self._log_path = log or (paths.log_dir() / ("server-text.log" if text_only else "server.log"))
        for self.backend in backends(backend):
            self.port = _free_port()
            self.key = secrets.token_urlsafe(24)
            self._local = threading.local()
            try:
                self._start()
                return
            except Exception as error:
                self._stop()
                if self.backend == "cpu":
                    self._pool.shutdown(wait=False)
                    raise EngineError(str(error)) from error
                FAILED.add(self.backend)
                logger.warning("model: %s didn't start, trying the next backend (%s)", self.backend, error)

    # -- process

    def _start(self) -> None:
        models = paths.models_dir()
        exe = paths.server_exe(self.backend)
        gpu = self.backend != "cpu"
        args = [str(exe), "-m", str(models / paths.TEXT_MODEL.name),
                "--embeddings", "--pooling", "mean", "-ngl", "999" if gpu else "0",
                "-c", str(MAX_TOKENS * SLOTS), "-b", str(MAX_TOKENS), "-ub", str(MAX_TOKENS),
                "-np", str(SLOTS), "-kvu", "--host", "127.0.0.1", "--port", str(self.port),
                "--api-key", self.key, "--no-webui", "--offline"]
        if gpu:
            args += ["--device", "CUDA0" if self.backend == "cuda" else "Vulkan0"]
        else:
            # Leave a few cores free: indexing on the CPU shouldn't make the PC sluggish.
            threads = max(1, min((os.cpu_count() or 4) - 2, 12))
            args += ["--device", "none", "-t", str(threads), "-tb", str(threads)]
        if not self.text_only:
            args += ["--mmproj", str(models / paths.MEDIA_MODEL.name),
                     "--image-min-tokens", "1", "--image-max-tokens", "1120"]
            if not gpu:
                args += ["--no-mmproj-offload"]
        else:
            args += ["--no-mmproj"]
        args += os.environ.get("LAZYFINDS_SERVER_ARGS", "").split()   # dev experiments only
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        out = open(self._log_path, "ab")
        since = out.tell()
        self._process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                                         cwd=str(exe.parent), creationflags=subprocess.CREATE_NO_WINDOW)
        out.close()
        _kill_with_us(self._process.pid)

        def stopped(when: str) -> EngineError:
            why = _reason(self._log_path, since) or f"see {self._log_path}"
            code = self._process.returncode
            return EngineError(f"llama-server on {self.backend} stopped {when} (exit {code}): {why}")

        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            if self._process.poll() is not None:
                raise stopped("while loading")
            try:
                status, body = self._request("GET", "/health", None, timeout=2)
                if status == 200:
                    break
            except OSError:
                pass
            time.sleep(0.25)
        else:
            raise EngineError(f"llama-server on {self.backend} didn't start in 3 minutes")
        try:
            _, props = self._request("GET", "/props", None)
            self.marker = props.get("media_marker", "<__media__>")
            self._warm_up()
        except Exception:
            # A GPU it can't really use (no kernels for that card) often loads, then dies on its first input.
            time.sleep(0.5)
            if self._process.poll() is not None:
                raise stopped("on its first input") from None
            raise

    def _warm_up(self) -> None:
        """The first call of each kind builds the GPU's kernels (Vulkan compiles its shaders then: 32 passages took 12 s
        cold, 0.3 s warm). Pay that now, while nothing waits on it."""
        self._embed([query_prompt("warm up"), document_prompt(None, "warm up " * 200)])
        if not self.text_only:
            picture = image_payload(Image.new("RGB", (480, 320), (128, 140, 150)), 70)
            sound = wav_bytes(np.zeros(16_000, dtype=np.int16))
            self.embed_media([picture, base64.b64encode(sound).decode("ascii")])

    def close(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
        self._stop()

    def _stop(self) -> None:
        if self._process and self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(5)
            except subprocess.TimeoutExpired:
                self._process.kill()
        self._process = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    @property
    def alive(self) -> bool:
        return self._process is not None and self._process.poll() is None

    # -- HTTP (one keep-alive connection per thread)

    def _request(self, method: str, path: str, body, timeout: float = 600):
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = {"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"}
        for attempt in (1, 2):
            conn = getattr(self._local, "conn", None)
            fresh = conn is None
            if fresh:
                conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
                self._local.conn = conn
            try:
                conn.request(method, path, data, headers)
                response = conn.getresponse()
                raw = response.read()
                break
            except (OSError, http.client.HTTPException):
                conn.close()
                self._local.conn = None
                # The server closes keep-alive connections left idle: a reused one may be dead. Retry once on a new one.
                if fresh or attempt == 2:
                    raise
        try:
            parsed = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            parsed = None
        return response.status, parsed

    def _embed(self, content) -> list[np.ndarray]:
        """POST /embeddings: one vector per prompt, L2-normalized float32."""
        status, out = self._request("POST", "/embeddings", {"content": content})
        if status != 200 or not isinstance(out, list):
            message = (out or {}).get("error", {}).get("message") if isinstance(out, dict) else None
            raise EngineError(message or f"embedding request failed ({status})")
        vectors = []
        for item in sorted(out, key=lambda x: x["index"]):
            v = item["embedding"]
            if v and isinstance(v[0], list):
                v = v[0]
            a = np.asarray(v, dtype=np.float32)
            n = float(np.linalg.norm(a))
            vectors.append(a / n if n > 0 else a)
        return vectors

    # -- public

    def embed_texts(self, texts: list[str], batch: int = 16) -> list[np.ndarray]:
        """Prompted texts (`query_prompt`, `document_prompt`). A text too long for one input is cut, keeping its start."""
        groups = [texts[i:i + batch] for i in range(0, len(texts), batch)]
        results = list(self._pool.map(self._embed_text_group, groups))
        return [v for group in results for v in group]

    def _embed_text_group(self, texts: list[str]) -> list[np.ndarray]:
        try:
            return self._embed(texts)
        except EngineError:
            if len(texts) > 1:
                return [v for t in texts for v in self._embed_text_group([t])]
            text = texts[0]
            # Too many tokens for one micro-batch: halve until it fits.
            while len(text) > 200:
                text = text[: len(text) // 2]
                try:
                    return self._embed([text])
                except EngineError:
                    continue
            raise

    def embed_media(self, payloads: list[str]) -> list[np.ndarray | Exception]:
        """Pictures (`image_payload`) and sound (16 kHz mono WAV of at most 30 s), base64. One vector each, or the
        error that input met: one bad picture mustn't sink its neighbours."""
        if self.text_only:
            raise EngineError("this engine was started text-only")

        def one(payload: str):
            try:
                return self._embed({"prompt_string": self.marker, "multimodal_data": [payload]})[0]
            except Exception as error:
                return error

        return list(self._pool.map(one, payloads))

    def embed_images(self, images: list[Image.Image], budget: int) -> list[np.ndarray | Exception]:
        if budget not in BUDGETS:
            raise ValueError(f"budget must be one of {BUDGETS}")
        return self.embed_media([image_payload(image, budget) for image in images])

    def embed_audio(self, wavs: list[bytes]) -> list[np.ndarray | Exception]:
        return self.embed_media([base64.b64encode(wav).decode("ascii") for wav in wavs])

    def embed_query(self, text: str) -> np.ndarray:
        return self.embed_texts([query_prompt(text)])[0]
