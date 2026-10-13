"""Where lazyFinds keeps its files, and the pinned files it downloads once.

Every pin was read from its publisher: model files from Hugging Face's LFS metadata of ggml-org/embeddinggemma-2-GGUF
at the revision that was `main` on 2026-10-13, llama.cpp archives from GitHub's release digests for tag b11461, OCR
models from tesseract-ocr/tessdata_best at one commit.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path


def app_dir() -> Path:
    """%LOCALAPPDATA%\\lazyFinds, or $LAZYFINDS_HOME for test setups."""
    chosen = os.environ.get("LAZYFINDS_HOME")
    if chosen:
        return Path(chosen)
    local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(local) / "lazyFinds"


def models_dir() -> Path:
    return app_dir() / "models"


def runtime_dir(backend: str = "cuda") -> Path:
    """One complete llama.cpp build per GPU kind. The CPU runs on whichever build is there."""
    return app_dir() / "runtime" / backend


def tessdata_dir() -> Path:
    return app_dir() / "tessdata"


def index_file() -> Path:
    return app_dir() / "index" / "index.sqlite"


def code_index_file() -> Path:
    return app_dir() / "index" / "code.sqlite"


def settings_file() -> Path:
    return app_dir() / "settings.json"


def log_dir() -> Path:
    return app_dir() / "logs"


@dataclass(frozen=True)
class Pinned:
    name: str
    url: str
    size: int
    sha256: str
    unpack_to: str | None = None   # a zip unpacked into this folder (relative to app_dir), else saved as is
    save_to: str = "models"


MODEL_REVISION = "bfcd298762cc34d0357ece5ebdd31791a3a374d8"
_HF = f"https://huggingface.co/ggml-org/embeddinggemma-2-GGUF/resolve/{MODEL_REVISION}"
TEXT_MODEL = Pinned("embeddinggemma-2-Q8_0.gguf", f"{_HF}/embeddinggemma-2-Q8_0.gguf", 309_855_456,
                    "2188ac1deca4b77dffefd603c2776a9d76d9d74ec01841392982ebb840b09135")
MEDIA_MODEL = Pinned("mmproj-embeddinggemma-2-Q8_0.gguf", f"{_HF}/mmproj-embeddinggemma-2-Q8_0.gguf", 554_821_024,
                     "c4a8a52691ecef40618438928bdf9e68379b854e24166f292592353db0aab64f")

LLAMA_BUILD = "b11461"
_REL = f"https://github.com/ggml-org/llama.cpp/releases/download/{LLAMA_BUILD}"
LLAMA_CUDA = Pinned("llama-b11461-bin-win-cuda-13.4-x64.zip", f"{_REL}/llama-b11461-bin-win-cuda-13.4-x64.zip",
                    153_137_923, "62c91b76abe5a704304eb1d2a5041944953d438c1b1d9f6b56261ea36b0461cf", "runtime/cuda")
CUDART = Pinned("cudart-llama-bin-win-cuda-13.4-x64.zip", f"{_REL}/cudart-llama-bin-win-cuda-13.4-x64.zip",
                423_535_356, "738f8c251ac22b70c3ae6f83a10cf222725df0395246a2cf58f32bdb85fbe668", "runtime/cuda")
LLAMA_VULKAN = Pinned("llama-b11461-bin-win-vulkan-x64.zip", f"{_REL}/llama-b11461-bin-win-vulkan-x64.zip",
                      33_377_638, "6b9ca8ad3e43ac3e166d1997875709e4547214bb5bf1c2ebfdd17a2350a8380c", "runtime/vulkan")

TESSDATA_COMMIT = "e12c65a915945e4c28e237a9b52bc4a8f39a0cec"
_TD = f"https://github.com/tesseract-ocr/tessdata_best/raw/{TESSDATA_COMMIT}"
TESS_BEN = Pinned("ben.traineddata", f"{_TD}/ben.traineddata", 11_045_427,
                  "1cd0129288d1f74f6661c35e638e487bd146a66bb732ecd9e011e48dcb0df623", save_to="tessdata")
TESS_ENG = Pinned("eng.traineddata", f"{_TD}/eng.traineddata", 15_400_601,
                  "8280aed0782fe27257a68ea10fe7ef324ca0f8d85bd2fd145d1c2b560bcb66ba", save_to="tessdata")

MODEL_FILES = [TEXT_MODEL, MEDIA_MODEL]
BACKEND_FILES = {"cuda": [LLAMA_CUDA, CUDART], "vulkan": [LLAMA_VULKAN]}
OCR_FILES = [TESS_BEN, TESS_ENG]


def server_exe(backend: str) -> Path:
    if backend == "cpu":   # the CPU runs on any build: its CPU code is in every one
        for b in ("vulkan", "cuda"):
            if backend_ready(b):
                return runtime_dir(b) / "llama-server.exe"
        return runtime_dir("vulkan") / "llama-server.exe"
    return runtime_dir(backend) / "llama-server.exe"


def backend_ready(backend: str) -> bool:
    if backend == "cpu":
        return any(backend_ready(b) for b in ("cuda", "vulkan"))
    marker = {"cuda": "ggml-cuda.dll", "vulkan": "ggml-vulkan.dll"}[backend]
    folder = runtime_dir(backend)
    return (folder / "llama-server.exe").is_file() and (folder / marker).is_file()


def has_nvidia() -> bool:
    """An NVIDIA driver is installed (its CUDA driver library is in System32)."""
    return (Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "nvcuda.dll").is_file()


def models_ready() -> bool:
    return all((models_dir() / f.name).is_file() for f in MODEL_FILES) and \
        any(backend_ready(b) for b in ("cuda", "vulkan"))


def _saved_path() -> list[str]:
    """PATH as Windows has it saved now. A program installed after lazyFinds started (winget adds its folder to the
    user's PATH) isn't on this process's PATH until a restart, but it is here."""
    import winreg

    folders: list[str] = []
    for root, key in ((winreg.HKEY_CURRENT_USER, "Environment"),
                      (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment")):
        try:
            with winreg.OpenKey(root, key) as k:
                value, _ = winreg.QueryValueEx(k, "Path")
            folders += [os.path.expandvars(p) for p in str(value).split(";") if p.strip()]
        except OSError:
            continue
    return folders


def find_program(name: str, also: list[Path] | None = None) -> Path | None:
    """`name`.exe on PATH, on the PATH Windows has saved, or in `also`."""
    found = shutil.which(name)
    if found:
        return Path(found)
    local = Path(os.environ.get("LOCALAPPDATA", ""))
    for folder in _saved_path() + [str(local / "Microsoft" / "WinGet" / "Links")] + [str(f) for f in also or []]:
        candidate = Path(folder) / f"{name}.exe"
        if candidate.is_file():
            return candidate
    return None


FFMPEG_WINGET_ID = "Gyan.FFmpeg"
TESSERACT_WINGET_ID = "UB-Mannheim.TesseractOCR"


def ffmpeg_exe(name: str = "ffmpeg") -> Path | None:
    """ffmpeg or ffprobe, wherever winget or the user put it."""
    packages = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Packages"
    return find_program(name, sorted(packages.glob("Gyan.FFmpeg*/*/bin")))


def ffmpeg_ready() -> bool:
    return ffmpeg_exe("ffmpeg") is not None and ffmpeg_exe("ffprobe") is not None


def tesseract_exe() -> Path | None:
    places = [Path(base) / "Tesseract-OCR" for base in (os.environ.get("ProgramFiles"),
                                                         os.environ.get("ProgramFiles(x86)"),
                                                         str(Path(os.environ.get("LOCALAPPDATA", "")) / "Programs"))
              if base]
    return find_program("tesseract", places)


def bengali_ocr_ready() -> bool:
    return tesseract_exe() is not None and all((tessdata_dir() / f.name).is_file() for f in OCR_FILES)
