"""The one-time downloads, each checked against the checksum its publisher lists (paths.py).

The model always; one llama.cpp build: CUDA on a PC with an NVIDIA driver, Vulkan on any other (both run on the
processor too), and Vulkan as well when CUDA won't start there (engine.py fetches it); and, when asked for, Tesseract's
Bengali and English models. Downloads resume (HTTP range) under a
temporary name; only a verified file gets its final name.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable

from . import paths


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(8 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _download(item: paths.Pinned, dest: Path, progress: Callable[[str, int, int], None]) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    have = part.stat().st_size if part.exists() else 0
    if have < item.size:
        request = urllib.request.Request(item.url, headers={"Range": f"bytes={have}-"} if have else {})
        with urllib.request.urlopen(request, timeout=60) as response:
            if have and response.status != 206:
                have = 0   # the server ignored the range: start over
            with open(part, "ab" if have else "wb") as out:
                while block := response.read(1 << 20):
                    out.write(block)
                    have += len(block)
                    progress(item.name, have, item.size)
    if part.stat().st_size != item.size or _sha256(part) != item.sha256:
        part.unlink(missing_ok=True)
        raise RuntimeError(f"{item.name}: the download doesn't match its published checksum; try again")
    part.replace(dest)


def _done_marker(item: paths.Pinned) -> Path:
    return paths.app_dir() / item.unpack_to / f".{item.name}.ok"


def _present(item: paths.Pinned) -> bool:
    if item.unpack_to:
        return _done_marker(item).is_file()
    return (paths.app_dir() / item.save_to / item.name).is_file()


def wanted(backend: str = "auto") -> list[paths.Pinned]:
    """What this PC needs for search to work: the model and one llama.cpp build."""
    if backend == "auto":
        backend = "cuda" if paths.has_nvidia() else "vulkan"
    return paths.MODEL_FILES + paths.BACKEND_FILES[backend]


def missing(backend: str = "auto") -> list[paths.Pinned]:
    return [item for item in wanted(backend) if not _present(item)]


def run(progress: Callable[[str, int, int], None] = lambda n, a, b: None, backend: str = "auto",
        only: list[paths.Pinned] | None = None) -> None:
    downloads = paths.app_dir() / "downloads"
    for item in [i for i in (only if only is not None else wanted(backend)) if not _present(i)]:
        if not item.unpack_to:
            _download(item, paths.app_dir() / item.save_to / item.name, progress)
            continue
        archive = downloads / item.name
        if not archive.exists():
            _download(item, archive, progress)
        target = paths.app_dir() / item.unpack_to
        staging = target.with_name(target.name + ".part")
        shutil.rmtree(staging, ignore_errors=True)
        with zipfile.ZipFile(archive) as z:
            z.extractall(staging)
        (staging / _done_marker(item).name).write_text(item.sha256)
        if target.exists():
            # A second archive for the same folder (CUDA's runtime libraries): add its files.
            for entry in staging.iterdir():
                os.replace(entry, target / entry.name)
            staging.rmdir()
        else:
            # The whole build at once: a lazyFinds running meanwhile never starts half of one.
            target.parent.mkdir(parents=True, exist_ok=True)
            staging.replace(target)
        archive.unlink()
    shutil.rmtree(downloads, ignore_errors=True)
