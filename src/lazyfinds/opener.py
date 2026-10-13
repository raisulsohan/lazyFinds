"""Opening a result where it matched: a PDF at its page, a recording or video at its moment.

Windows has no common way to open a file "at" a place, so this asks which app opens the file type (AssocQueryString)
and uses that app's own command line when it's one we know. For any other app the file just opens; for sound and video
the caller then plays it in lazyFinds' own player, at the moment.
"""

from __future__ import annotations

import ctypes
import os
import subprocess
from ctypes import wintypes
from pathlib import Path
from urllib.parse import quote


def default_app(extension: str) -> str | None:
    """The program that opens `extension` (".pdf"), as a path, or None."""
    size = wintypes.DWORD(1024)
    buffer = ctypes.create_unicode_buffer(1024)
    # ASSOCF_NOTRUNCATE, ASSOCSTR_EXECUTABLE
    result = ctypes.windll.shlwapi.AssocQueryStringW(0x20, 2, extension, None, buffer, ctypes.byref(size))
    return buffer.value if result == 0 and buffer.value and os.path.isfile(buffer.value) else None


def _launch(args: list[str]) -> bool:
    try:
        subprocess.Popen(args, creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
                         close_fds=True)
        return True
    except OSError:
        return False


BROWSERS = {"chrome.exe", "msedge.exe", "brave.exe", "vivaldi.exe", "opera.exe", "firefox.exe"}


def open_pdf(path: str, page: int | None) -> None:
    app = default_app(".pdf")
    name = Path(app).name.lower() if app else ""
    if page and page > 1 and app:
        if name in BROWSERS:
            url = "file:///" + quote(path.replace("\\", "/"), safe="/:") + f"#page={page}"
            if _launch([app, url]):
                return
        if name.startswith("sumatrapdf"):
            if _launch([app, "-page", str(page), path]):
                return
        if name in ("acrobat.exe", "acrord32.exe"):
            if _launch([app, "/A", f"page={page}", path]):
                return
    os.startfile(path)


def _player_args(app: str, path: str, seconds: float) -> list[str] | None:
    name = Path(app).name.lower()
    s = max(0.0, seconds)
    if name == "vlc.exe":
        return [app, f"--start-time={s:.1f}", path]
    if name.startswith("mpv"):
        return [app, f"--start={s:.1f}", path]
    if name.startswith(("mpc-hc", "mpc-be")):
        return [app, path, "/start", str(int(s * 1000))]
    if name.startswith("potplayer"):
        h, rem = divmod(int(s), 3600)
        return [app, path, f"/seek={h:02d}:{rem // 60:02d}:{rem % 60:02d}"]
    return None


def open_media_at(path: str, seconds: float | None) -> bool:
    """Opens sound or video at `seconds` in the default player when it takes a start time. False when it doesn't
    (the caller plays it itself), True when it's open."""
    app = default_app(Path(path).suffix.lower())
    if seconds and seconds > 1 and app:
        args = _player_args(app, path, seconds)
        if args:
            return _launch(args)
        return False
    os.startfile(path)
    return True


def open_plain(path: str) -> None:
    os.startfile(path)


def reveal(path: str) -> None:
    """Shows the file selected in File Explorer."""
    subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])


def default_app_name(extension: str) -> str | None:
    app = default_app(extension)
    return Path(app).stem if app else None
