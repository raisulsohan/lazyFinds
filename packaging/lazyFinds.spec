# PyInstaller build of lazyFinds: one folder with lazyFinds.exe (the tray app, no console) and lazyfinds-cli.exe.
#   uv run pyinstaller packaging/lazyFinds.spec --noconfirm
# The model, llama.cpp and OCR models are not in it: the app downloads them on first start.

import os

from PyInstaller.utils.hooks import collect_all

HERE = SPECPATH  # noqa: F821  (PyInstaller sets it)

datas, binaries, hidden = [], [], []
for package in ("winrt", "pypdfium2", "pypdfium2_raw", "pillow_heif", "watchdog"):
    d, b, h = collect_all(package)
    datas += d
    binaries += b
    hidden += h

excluded = ["tkinter", "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.Qt3DCore", "PySide6.QtQuick",
            "PySide6.QtQml", "PySide6.QtCharts", "PySide6.QtDataVisualization", "PySide6.QtPdf", "PySide6.QtDesigner",
            "pytest", "torch", "matplotlib"]


def analysis(script):
    return Analysis([os.path.join(HERE, script)], pathex=[os.path.join(HERE, "..", "src")], binaries=binaries, datas=datas, hiddenimports=hidden,
                    excludes=excluded, noarchive=False)


app = analysis("launch_app.py")
cli = analysis("launch_cli.py")
app_exe = EXE(PYZ(app.pure), app.scripts, [], exclude_binaries=True, name="lazyFinds", icon=os.path.join(HERE, "lazyFinds.ico"),
              console=False, version=os.path.join(HERE, "version.txt"))
cli_exe = EXE(PYZ(cli.pure), cli.scripts, [], exclude_binaries=True, name="lazyfinds-cli", icon=os.path.join(HERE, "lazyFinds.ico"),
              console=True, version=os.path.join(HERE, "version.txt"))
# Qt's software OpenGL fallback and its own translations: lazyFinds needs neither (~27 MB).
def trimmed(toc):
    return [entry for entry in toc if not (entry[0].lower().endswith("opengl32sw.dll")
                                           or "pyside6/translations" in entry[0].lower().replace("\\", "/"))]


COLLECT(app_exe, trimmed(app.binaries), trimmed(app.datas), cli_exe, trimmed(cli.binaries), trimmed(cli.datas),
        name="lazyFinds")
