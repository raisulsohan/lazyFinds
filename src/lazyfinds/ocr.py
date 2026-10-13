"""Words in pictures (screenshots, scanned pages), for exact-word search. Two readers, both on-device:

- Windows' own OCR (Windows.Media.Ocr): fast and good at the languages installed in Windows, but it has no Bengali.
- Tesseract with the Bengali + English models from tessdata_best, when Tesseract is installed and the user keeps
  "Bengali text in pictures" on. When it finds Bengali in a picture, its reading is used; otherwise Windows' is.
"""

from __future__ import annotations

import asyncio
import io
import subprocess
import threading

from PIL import Image

from . import paths

_lock = threading.Lock()
_windows = None
_windows_failed = False
BENGALI = range(0x0980, 0x0A00)


def _windows_engine():
    global _windows, _windows_failed
    if _windows is None and not _windows_failed:
        try:
            from winrt.windows.media.ocr import OcrEngine

            _windows = OcrEngine.try_create_from_user_profile_languages()
            _windows_failed = _windows is None
        except Exception:
            _windows_failed = True
    return _windows


def _prepared(image: Image.Image, longest: int) -> Image.Image:
    """Small text reads better a little bigger; huge pictures are cut down to what the reader takes."""
    rgb = image.convert("RGB")
    side = max(rgb.size)
    if side < 1200:
        factor = min(2.0, 1200 / side)
        rgb = rgb.resize((int(rgb.width * factor), int(rgb.height * factor)), Image.Resampling.LANCZOS)
    elif side > longest:
        rgb.thumbnail((longest, longest), Image.Resampling.LANCZOS)
    return rgb


def windows_text(image: Image.Image) -> str:
    with _lock:
        engine = _windows_engine()
        if engine is None:
            return ""
        try:
            from winrt.windows.graphics.imaging import BitmapPixelFormat, SoftwareBitmap
            from winrt.windows.media.ocr import OcrEngine
            from winrt.windows.storage.streams import DataWriter

            rgb = _prepared(image, OcrEngine.max_image_dimension)
            r, g, b = rgb.split()
            alpha = Image.new("L", rgb.size, 255)
            writer = DataWriter()
            writer.write_bytes(Image.merge("RGBA", (b, g, r, alpha)).tobytes())   # BGRA, as SoftwareBitmap wants
            bitmap = SoftwareBitmap.create_copy_from_buffer(writer.detach_buffer(), BitmapPixelFormat.BGRA8,
                                                            rgb.width, rgb.height)

            async def recognize():
                return await engine.recognize_async(bitmap)

            result = asyncio.run(recognize())
            return "\n".join(line.text for line in result.lines).strip()
        except Exception:
            return ""


def tesseract_text(image: Image.Image) -> str:
    """Bengali + English through Tesseract, the picture passed on stdin (no temporary files)."""
    exe = paths.tesseract_exe()
    if exe is None or not paths.bengali_ocr_ready():
        return ""
    buffer = io.BytesIO()
    _prepared(image, 4000).save(buffer, "PNG", compress_level=1)
    try:
        run = subprocess.run([str(exe), "stdin", "stdout", "--tessdata-dir", str(paths.tessdata_dir()), "-l",
                              "ben+eng", "--psm", "3"], input=buffer.getvalue(), capture_output=True, timeout=60,
                             creationflags=subprocess.CREATE_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    lines = (line.strip() for line in run.stdout.decode("utf-8", "replace").splitlines())
    return "\n".join(line for line in lines if line)


def has_bengali(text: str, at_least: int = 4) -> bool:
    return sum(ord(c) in BENGALI for c in text) >= at_least


def text_of(image: Image.Image, bengali: bool | None = None) -> str:
    """The words in `image`, or "" when neither reader finds any."""
    if bengali is None:
        from .settings import Settings

        bengali = Settings.load().bengali_ocr
    words = windows_text(image)
    if bengali:
        other = tesseract_text(image)
        if has_bengali(other):
            return other
    return words


def available() -> dict[str, bool]:
    return {"windows": _windows_engine() is not None, "bengali": paths.bengali_ocr_ready()}
