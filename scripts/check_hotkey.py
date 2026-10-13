"""Checks the real path a person takes: start the app, press its hotkey (SendInput), see the panel come up; start it a
second time and see the first one answer. Uses LAZYFINDS_HOME / LAZYFINDS_INDEX like the app does.

    uv run python scripts/check_hotkey.py [dist/lazyFinds/lazyFinds.exe]   # the built app instead of the source
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import time
from ctypes import wintypes

from lazyfinds.app import parse_hotkey
from lazyfinds.settings import Settings

user32 = ctypes.windll.user32
VK = {0x2: 0x11, 0x4: 0x10, 0x1: 0x12, 0x8: 0x5B}  # MOD -> VK_CONTROL, VK_SHIFT, VK_MENU, VK_LWIN


def panel_visible() -> bool:
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def each(hwnd, _):
        buf = ctypes.create_unicode_buffer(64)
        user32.GetWindowTextW(hwnd, buf, 64)
        if buf.value == "lazyFinds" and user32.IsWindowVisible(hwnd):
            found.append(hwnd)
        return True

    user32.EnumWindows(each, 0)
    return bool(found)


def press(hotkey: str):
    mods, key = parse_hotkey(hotkey)
    keys = [VK[m] for m in (0x2, 0x4, 0x1, 0x8) if mods & m]
    for k in keys + [key]:
        user32.keybd_event(k, 0, 0, 0)
    for k in reversed(keys + [key]):
        user32.keybd_event(k, 0, 2, 0)


def main() -> int:
    hotkey = Settings.load().hotkey
    command = sys.argv[1:2] or [sys.executable, "-m", "lazyfinds.app"]
    app = subprocess.Popen(command, env=os.environ.copy())
    ok = True
    try:
        time.sleep(6)
        print("before hotkey, panel visible:", panel_visible())
        press(hotkey)
        time.sleep(1.5)
        shown = panel_visible()
        print(f"after {hotkey}: panel visible:", shown)
        ok &= shown
        press("Ctrl+Alt+F12" if hotkey != "Ctrl+Alt+F12" else hotkey)  # toggles it away again
        time.sleep(1)
        user32.keybd_event(0x1B, 0, 0, 0)
        user32.keybd_event(0x1B, 0, 2, 0)   # Esc
        time.sleep(1)
        print("after Esc: panel visible:", panel_visible())
        second = subprocess.run(command, env=os.environ.copy(), timeout=20)
        time.sleep(1.5)
        shown = panel_visible()
        print("second start exited with", second.returncode, "and the first one's panel is visible:", shown)
        ok &= shown and second.returncode == 0
    finally:
        app.terminate()
        app.wait(10)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
