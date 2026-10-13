# A macOS version of lazyFinds: the plan

lazyFinds runs on Windows only. A macOS version was planned in October 2026 and then put aside: it has to be checked on
a real Mac (see "Checks" below), and the author has none. This is the plan, for whoever has a Mac and wants to build it.
lazyFinds is MIT-licensed: fork it, port it, and if you like, send it back as a pull request.

## Ground rules

- Take nothing from DigUp (a macOS app with a similar idea): no code, no tuned numbers, no word lists, no test data, and
  don't read its source for solutions. lazyFinds was written from scratch, and a port should be too.
- Keep the Windows version working exactly as it is. All existing tests must still pass on Windows.
- Check vectors against Google's official model before anything ships: `tests/test_reference.py` must pass on Metal
  and on the CPU (cosine >= 0.995). A backend that fails isn't used.

## The platform layer

Everything that differs by OS moves out of the shared code into two modules with the same functions:

```
src/lazyfinds/platform/__init__.py   # picks one by sys.platform: `from .platform import OS`
src/lazyfinds/platform/windows.py    # today's Windows code, moved, behaving the same
src/lazyfinds/platform/macos.py      # new
```

Shared code (engine, store, search, text, indexer, extract, names, code search, UI) calls only `OS`. A test on every OS
checks that both modules have the same names and signatures. Keep `macos.py` importable on Windows (pyobjc and Carbon
imported inside functions only), so its pure logic (player arguments, AppleScript text, the LaunchAgent plist, rule
verdicts on fake `st_flags`) is tested on Windows too.

| Area | Function or value | Windows (moved as is) | macOS (new) |
|---|---|---|---|
| Folder | `app_dir()` | `%LOCALAPPDATA%\lazyFinds` | `~/Library/Application Support/lazyFinds` |
| Engine | `BACKENDS`, `auto_backend()` | cuda, vulkan, cpu | metal, cpu |
| | `llama_files(backend)` | CUDA zip + cudart, or the Vulkan zip | `llama-b11461-bin-macos-arm64.tar.gz` (below) |
| | `server_exe()`, `backend_ready()`, `device_arg()` | `llama-server.exe`, `ggml-cuda.dll`, `CUDA0` / `Vulkan0` | `llama-server`, the Metal dylib, `MTL0` |
| | `spawn_server()`: the server dies with lazyFinds | a job object | a small guard process that ends the server when lazyFinds' pid ends |
| | `cpu_threads()` | cores − 2, at most 12 | the performance cores (`sysctl hw.perflevel0.physicalcpu`) |
| | `NO_WINDOW` (subprocess kwargs) | `creationflags=CREATE_NO_WINDOW` | none |
| Rules | `folder_verdict()`, `file_verdict()`, `is_bundle()` | hidden, system, reparse-point and OneDrive attributes | `UF_HIDDEN`, `SF_DATALESS`, links, the folders below |
| | `is_screenshot()` | names, Screenshots folder | names, and the `com.apple.metadata:kMDItemIsScreenCapture` xattr |
| OCR | `system_ocr(image)`, `OCR_NAME` | Windows.Media.Ocr | Vision (`VNRecognizeTextRequest`, pyobjc) |
| Tools | `find_program()`, `ffmpeg_exe()`, `tesseract_exe()`, `install_tool()` | PATH, saved PATH, winget | PATH, `/opt/homebrew/bin`, `/usr/local/bin`; `brew install` in Terminal |
| Opening | `open_plain`, `reveal`, `open_pdf`, `open_media_at`, `open_code_at`, `default_app_name` | `os.startfile`, `explorer /select`, AssocQueryString | `open`, `open -R`, Skim, QuickTime, IINA, VLC, mpv, VS Code (below) |
| UI | `Hotkey` (register, unregister, parse), `DEFAULT_HOTKEY` | RegisterHotKey + `nativeEvent` | Carbon `RegisterEventHotKey`, ⌥⌘Space |
| | `app_started()`, `bring_to_front()`, `prepare_panel()` | AppUserModelID, SetForegroundWindow | no Dock icon, activate the app, panel on every Space and over full-screen apps |
| | `WORDS` (labels) | "File Explorer", "PC", Ctrl, Consolas | "Finder", "Mac", ⌘, Menlo |
| Login | `startup_enabled()`, `set_startup()` | HKCU `Run` | a LaunchAgent in `~/Library/LaunchAgents` |
| Names | `name_roots()`, `name_skip()`, `SYSTEM_PARTS` | fixed drives | home and local `/Volumes` (not Time Machine); `/` only after the user says yes |
| | `instance_name()` | `USERNAME` | `USER` |

What changes in shared files:

- `paths.py`: the Windows llama.cpp pins move to `windows.py`; the model and tessdata pins stay. Re-export the old names.
- `setup.py`: also unpacks `.tar.gz` (`tarfile`, `filter="data"`: it keeps the dylib symlinks and the execute bit).
- `engine.py`: the job object moves out; it calls `ctypes.WinDLL` at import today, which fails on macOS.
- `rules.py`: the shared lists stay; `st_file_attributes` (Windows only) moves behind `OS`.
- `ocr.py`: Tesseract and the choice between readers stay shared; only `windows_text` becomes `OS.system_ocr`.
- `opener.py`: a thin front for `OS`, so `app.py` keeps its calls.
- `names.py`, `app.py`, `code.py`, `extract.py`, `cli.py`: Windows calls (`windll`, `winreg`, `os.startfile`,
  `CREATE_NO_WINDOW`) go through `OS`.
- `pyproject.toml`: `winrt-*` get `; sys_platform == 'win32'`; `pyobjc-framework-Vision`, `-Quartz` and `-Cocoa` get
  `; sys_platform == 'darwin'`. Run `uv lock` without `--upgrade`, so Windows keeps its versions.

## macOS details

- **Engine.** llama.cpp tag b11461 (the one Windows uses) has `llama-b11461-bin-macos-arm64.tar.gz`: 12,007,683 bytes,
  SHA-256 `6cbf2bcbfe616e9e14f1e326fe34d15fbe127846ad4275f076b36694101ff2c1` (GitHub's release digest). Apple Silicon
  first; the CPU runs on the same build (`--device none -ngl 0`). Check the archive's file list before writing
  `backend_ready()`.
- **Never read:** `~/Library` (also in Name mode), `/System`, the insides of `/Applications` bundles, `.Trash`, hidden
  folders, Time Machine volumes (`Backups.backupdb`, `*.backup` folders), and iCloud files that live only online: their
  `SF_DATALESS` flag (0x40000000 in `st_flags`) means reading one downloads it. `.app` and `.photoslibrary` bundles are
  single items.
- **Screenshots:** macOS names them "Screenshot … at …" (older: "Screen Shot …"; other languages differ), and marks
  them with the xattr above. Python's `os.getxattr` is Linux-only: call libc's `getxattr` through ctypes.
- **OCR:** Vision for the words in screenshots and scans. It has no Bengali: keep Tesseract (`brew install tesseract`)
  for Bengali, as on Windows.
- **Opening at the place:** PDFs at the page in Skim through AppleScript when it's installed (Preview can't be told a
  page), else open normally. Audio and video at the moment: QuickTime through AppleScript (`current time`), IINA, VLC
  and mpv through their command lines; any other player, lazyFinds' own player, as on Windows. Code in VS Code at the
  line: its CLI is inside the app bundle (`Contents/Resources/app/bin/code`) when `code` isn't on PATH. Reveal with
  `open -R`; copy as a file with Qt's file URL on the clipboard.
- **Menu bar app:** the tray icon becomes a menu bar item with a template icon (`QIcon.setIsMask(True)`), no Dock icon
  (`LSUIElement` in the bundle; `NSApp.setActivationPolicy_` when run from source).
- **Hotkey:** pyobjc has no wrapper for `RegisterEventHotKey`; call Carbon through ctypes. It needs no permission.
- **Start at login:** a LaunchAgent, not SMAppService: SMAppService wants a properly signed bundle, and an ad-hoc
  signature changes with every build.
- **Name mode:** walk the home folder and local `/Volumes` with the rules above; keep up to date with FSEvents
  (watchdog uses it). Ask before reading all of `/`.
- **Packaging:** a PyInstaller `.app` (arm64), ad-hoc signed (`codesign --force --deep -s -`), in a DMG made with
  `hdiutil create … -format UDZO` (the app and a link to `/Applications`). The model still downloads on first start, so
  the DMG stays small.

## Pitfalls found while planning

- **Gatekeeper.** Without an Apple Developer ID the app is unsigned. Since macOS 15 (Sequoia), right-click → Open no
  longer gets past Gatekeeper: open the app once, then System Settings → Privacy & Security → "Open Anyway". On macOS
  14 and earlier, right-click → Open works. The README and release notes must say both.
- **⌥⌘Space** is "Show Finder search window" on a stock Mac. Registering it may succeed and still never fire. Tell the
  user how to turn that shortcut off, or pick another default.
- **Finder's PATH.** An app opened from Finder gets only `/usr/bin:/bin:/usr/sbin:/sbin`, so Homebrew's ffmpeg and
  Tesseract look missing unless `find_program` looks in `/opt/homebrew/bin` and `/usr/local/bin` itself.
- **Permission prompts.** macOS asks once each for Desktop, Documents, Downloads and removable or network volumes;
  Name mode's first walk brings them all at once. With an ad-hoc signature it may ask again after an update. Explain
  this on the first-run screen.
- **The testbed.** `scripts/make_testbed.py` uses Windows' fonts, voices and a wallpaper. On a Mac: Apple Color Emoji
  and Kohinoor Bangla, `say` voices (Samantha, Daniel) through ffmpeg, a picture from `/System/Library/Desktop
  Pictures`. The wallpapers differ, so have the testbed write the photo's description to a `testbed.json` that
  `scripts/eval.py` reads (no file: today's query, so Windows is unchanged).

## Checks (they need a Mac)

1. `uv run pytest` on Windows and on the Mac; macOS tests next to the Windows ones, skipped by platform.
2. `tests/test_reference.py` on Metal and on the CPU: worst cosine >= 0.995 for each.
3. `scripts/make_testbed.py`, then `lazyfinds index` and `scripts/eval.py`: report first-place and right-place counts.
4. The real app: the hotkey, the panel, Tab between Meaning and Name, Space to preview, Enter opening a PDF at its page
   and a recording or video at its moment, reveal in Finder, copy and paste a file.
5. The DMG on a clean Mac: install, the Gatekeeper step, first-start download, start at login.

A GitHub Actions macOS arm64 runner can run 1 and the CPU half of 2, and build the DMG. Whether Metal works there is
unproven, and it can't stand in for 4.

## Release

One version for both: Windows installers from `packaging/build.ps1` as today, and
`lazyFinds-<version>-macos-arm64.dmg` from a macOS build script, all on one GitHub release, with the Gatekeeper steps
in the notes.
