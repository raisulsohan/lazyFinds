# lazyFinds

**Search your PC by what's inside your files.** Describe a photo, a page, a moment in a recording or a bit of code, in
English or Bengali, and lazyFinds finds it. Everything runs on your PC: no account, no cloud, no telemetry.

- "a bicycle in the park" or "পার্কে সাইকেল" finds the photo
- "washing machine won't drain" opens the manual at page 2
- "how volcanoes form" plays the recording from that moment
- "fireworks at night" plays the video from the shot with the fireworks
- "বিদ্যুৎ বিল" finds the Bengali payment screenshot; "881275" finds it too, though it's written "৮৮১২৭৫"
- "TXN-58K3Q" or "ORD-77A91X" finds the screenshot or email with exactly that code
- "code: kill the child process when the app exits" opens the function in VS Code at its line

It runs Google DeepMind's [EmbeddingGemma 2](https://huggingface.co/google/embeddinggemma-2) (Apache 2.0), which puts
text, pictures, sound and video into one shared space, on [llama.cpp](https://github.com/ggml-org/llama.cpp).
Inspired by DigUp for macOS; written from scratch for Windows.

## Requirements

- Windows 10 or 11, 64-bit
- Any GPU (NVIDIA via CUDA, AMD or Intel via Vulkan), or none: the processor works too, about 5x slower
- [uv](https://docs.astral.sh/uv/) (`winget install astral-sh.uv`)
- For sound and video: ffmpeg on PATH (`winget install Gyan.FFmpeg`)
- For Bengali text in pictures and scans (optional): Tesseract OCR (`winget install UB-Mannheim.TesseractOCR`);
  Settings → "Set up…" fetches its Bengali and English models

## Install

Download `lazyFinds-Setup-<version>.exe` from [Releases](https://github.com/raisulsohan/lazyFinds/releases) and run
it. It installs for your user only (no admin rights) into `%LOCALAPPDATA%\Programs\lazyFinds`, with a Start menu
entry, and can start lazyFinds when you sign in. The installer isn't code-signed yet, so Windows SmartScreen may ask
first: choose "More info" → "Run anyway".

Uninstall from Settings → Apps. It asks whether to delete the downloaded model and the index too; your own files are
never touched.

## Run from source

```powershell
git clone <this repo> lazyFinds
cd lazyFinds
uv sync
uv run lazyfinds-app
```

The first start downloads the model and one llama.cpp build once (CUDA on an NVIDIA PC: 1.4 GB; Vulkan elsewhere:
0.9 GB). Every file is checked against the SHA-256 its publisher lists. Then pick folders in Settings; lazyFinds reads
them in the background and lives in the system tray.

Press **Ctrl+Alt+Space** (change it in Settings) in any app and describe what you're looking for.

| Key | What it does |
|---|---|
| ↓ ↑ | Moves through the results |
| Enter | Opens the result where it matched: a PDF at its page, sound and video at the moment, code at its line |
| Space | Previews it (after you've moved to a result): the picture, the page, the passage, a player at the moment |
| Ctrl+Enter | Shows it in File Explorer |
| Ctrl+C | Copies the file, to paste anywhere |
| Esc | Closes the preview, then the panel |

Start a query with `code:` to search the folders chosen for code.

## What it reads

Only the folders you choose:

| Kind | How |
|---|---|
| photos (JPEG, PNG, WebP, HEIC, AVIF, …) | by what they show (280 vision tokens, the model's default) |
| screenshots | by what they show, and by their words (Windows OCR; Tesseract for Bengali) |
| PDFs | page by page; scans and slides by their picture and OCR'd words; Bengali text layers checked against OCR |
| documents (txt, md, rtf, docx, odt, html) | in passages of ~300 tokens cut at sentence ends |
| sound (mp3, m4a, wav, flac, …) | in 20-second windows, 5 s overlap (the model hears at most 30 s at a time) |
| video | one frame per shot (ffmpeg scene detection), and the soundtrack as above |
| code (in code folders only) | in blocks cut at definitions; names split apart ("refreshAccessToken" → refresh access token) |

It never reads Windows' own folders, AppData, folders tools fill (node_modules, build output inside projects, …),
hidden or system items, links, OneDrive files that live only online (reading one would download it), half-finished
downloads, Office lock files, or anything that looks like a key or password store. A drive that's unplugged keeps its
files in the index until it's back. lazyFinds only reads your files; it never changes, moves or uploads them.

## How it finds things

```
your folders ─► readers (Pillow, pypdfium2, ffmpeg, Windows OCR, Tesseract) in worker threads
             ─► llama-server.exe on 127.0.0.1 (random port and key, in a job object that dies with lazyFinds)
             ─► SQLite: a 768-d vector per piece + an FTS5 index of every word (Bengali digits as ASCII digits)
your words   ─► the model's text part ─► meaning (numpy) and exact words (FTS5), fused
```

- **Meaning:** each piece's cosine with the query, in robust deviations (median and MAD) above the median of its own
  kind of vector, so a page, a photo and a stretch of sound compete fairly.
- **Words:** FTS5 with Porter stemming for English; Bengali words lose their endings ("টমেটোর" → টমেটো) and match as
  prefixes.
- **Fusion:** reciprocal rank fusion of the two rankings; files holding every code or ID in the query come first, and
  files of the kind the query names ("…in a video") before others.

## Checked

- `tests/test_reference.py`: llama.cpp's vectors against Google's official model (sentence-transformers, float32) on
  our own inputs. Worst cosine: CUDA 0.998, Vulkan 0.999, CPU 0.998. A backend that fails isn't used.
- `scripts/eval.py`: 32 queries (9 in Bengali) over `scripts/make_testbed.py`'s files: 32/32 first and 8/8 at the
  right page or moment, on CUDA, Vulkan and the CPU alike.

| On an RTX 3070 | CUDA | Vulkan | CPU (i7-13700KF) |
|---|---|---|---|
| photos per second | 21 | 15 | 1 |
| passages per second | 87 | 102 | 4 |
| one search | ~15 ms | ~15 ms | ~15 ms |

The first pass is the only heavy one; after that, new files are searchable a few seconds after they land. The
indexing model quits when there's nothing to read; the search model (text only) after 10 minutes unused.

## Command line

```powershell
uv run lazyfinds setup                       # the one-time download (--backend cuda|vulkan)
uv run lazyfinds folders --add D:\Photos     # --remove, --exclude, --code <folder>
uv run lazyfinds index                       # read new and changed files
uv run lazyfinds search a bicycle in the park --lines
uv run lazyfinds search "code: parse the settings file"
uv run lazyfinds status --problems
```

Files live in `%LOCALAPPDATA%\lazyFinds`: `models\`, `runtime\cuda|vulkan\`, `tessdata\`, `index\`, `settings.json`,
`logs\`. To remove lazyFinds, quit it, untick "Start with Windows" in Settings, and delete that folder.

## Development

```powershell
uv run pytest
uv run python scripts/make_testbed.py C:\tmp\testbed     # test files from Windows' own fonts, voices and wallpaper
$env:LAZYFINDS_INDEX = "C:\tmp\test.sqlite"; uv run lazyfinds index C:\tmp\testbed; uv run python scripts/eval.py
cd tools\reference; uv run python make_reference.py C:\tmp\testbed ..\..\tests\reference   # official vectors (CPU)
```

Build the installer (needs Inno Setup 6: `winget install JRSoftware.InnoSetup`):

```powershell
powershell -ExecutionPolicy Bypass -File packaging/build.ps1   # → packaging/Output/lazyFinds-Setup-<version>.exe
```

`LAZYFINDS_HOME` moves all of lazyFinds' files; `LAZYFINDS_INDEX` and `LAZYFINDS_CODE_INDEX` move only the indexes.

## License

MIT. EmbeddingGemma 2 is Apache 2.0 (Google), llama.cpp MIT (ggml-org), Tesseract and its models Apache 2.0.
