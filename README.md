# lazyFinds

**Search your PC by what's inside your files.** Describe a photo, a page, a moment in a recording or a bit of code, in
English or Bengali, and lazyFinds finds it. Everything runs on your PC: no account, no cloud, no telemetry.

- "a bicycle in the park" or "পার্কে সাইকেল" finds the photo
- "washing machine won't drain" opens the manual at page 2
- "how volcanoes form" plays the recording from that moment
- "fireworks at night" plays the video from the shot with the fireworks
- "বিদ্যুৎ বিল" finds the Bengali payment screenshot; "881275" finds it too, though it's written "৮৮১২৭৫"
- **Banglish**: Bengali typed in English letters is searched in Bengali too. "atoshbaji" finds the fireworks video,
  "basha theke office er kaj" the contract, "kemon" the file "কেমন আছো.docx" in Name mode; English stays English
- "TXN-58K3Q" or "ORD-77A91X" finds the screenshot or email with exactly that code
- "code: kill the child process when the app exits" opens the function in VS Code at its line
- **Name mode** (Tab): type any part of any file or folder name on the whole PC ("ract si" → contract_signed.docx),
  with results as you type

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

## macOS

There's no macOS version yet: it has to be checked on a real Mac (vectors against Google's official model on Metal,
the menu bar app, opening files at their page or moment), and I don't have one. The code doesn't run on macOS as it is
(it calls Windows directly in a few places). lazyFinds is MIT-licensed, so if you have a Mac, fork it and port it:
[docs/macos-plan.md](docs/macos-plan.md) lists what differs on macOS, a plan for a platform layer, the pitfalls, and
the checks a port needs. A pull request that keeps Windows working is welcome.

## Install

Download `lazyFinds-Setup-<version>.exe` (about 3 MB) from
[Releases](https://github.com/raisulsohan/lazyFinds/releases) and run it. It downloads the app (about 52 MB) from the
same release, checks its SHA-256, and installs it for your user only (no admin rights) into
`%LOCALAPPDATA%\Programs\lazyFinds`, with a Start menu entry; it can start lazyFinds when you sign in. For a PC without
internet, use `lazyFinds-Setup-<version>-offline.exe`, which has everything inside. The installers aren't code-signed
yet, so Windows SmartScreen may ask first: choose "More info" → "Run anyway".

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
0.9 GB). If CUDA won't start on an NVIDIA PC (an older card or driver), lazyFinds runs on the processor and gets the
Vulkan build too (33 MB), then moves to it. Every file is checked against the SHA-256 its publisher lists. Then pick folders in Settings; lazyFinds reads
them in the background and lives in the system tray.

Press **Ctrl+Alt+Space** (change it in Settings) in any app and describe what you're looking for.

| Key | What it does |
|---|---|
| Tab | Switches between **Meaning** (what's inside files) and **Name** (any part of any name on the PC) |
| ↓ ↑ | Moves through the results |
| Enter | Opens the result where it matched: a PDF at its page, sound and video at the moment, code at its line |
| Space | Previews it (after you've moved to a result): the picture, the page, the passage, a player at the moment |
| Ctrl+Enter | Shows it in File Explorer |
| Ctrl+C | Copies the file, to paste anywhere |
| Esc | Closes the preview, then the panel |

Start a query with `code:` to search the folders chosen for code.

**Banglish.** When a query has Bengali typed in English letters, a quiet line under the field says what was searched
("also searched: আতশবাজি"). Click "search only as typed" there, or press **Ctrl+B**, to search that query as typed.
Turn it off in Settings.

**Name mode** keeps the names (never the contents) of every file and folder on the PC's fixed drives: about a million
on a typical PC, read in under a minute without admin rights, then kept up to date by watching the drives. Any part of
a name matches, spaces separate parts that must all be there, and exact names come first, then your own files before
the ones Windows and programs keep (AppData, Program Files, …). A search takes 15–40 ms. Turn it off in Settings.

## What it reads

Only the folders you choose:

| Kind | How |
|---|---|
| photos (JPEG, PNG, WebP, HEIC, AVIF, …) | by what they show (280 vision tokens, the model's default) |
| screenshots | by what they show, and by their words (Windows OCR; Tesseract for Bengali) |
| PDFs | page by page; scans and slides by their picture and OCR'd words; Bengali text layers checked against OCR |
| documents (txt, md, rtf, docx, odt, html) | in passages of ~300 tokens cut at sentence ends |
| Word and PowerPoint (docx, pptx, and the 97-2003 doc, ppt) | the text as above (slides one by one, with speaker notes); each picture inside by what it shows and its OCR'd words; alt text as words |
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
  kind of vector, so a page, a photo and a stretch of sound compete fairly. Files of one sort (voice messages, songs,
  screenshots) are measured from their own median: a Bengali query is closer to every Bengali voice message,
  whatever it says, and no longer fills the list with them.
- **Words:** FTS5 with Porter stemming for English; Bengali words lose their endings ("টমেটোর" → টমেটো) and match as
  prefixes.
- **Fusion:** reciprocal rank fusion of the two rankings; files holding every code or ID in the query come first, and
  files of the kind the query names ("…in a video") before others.
- **Banglish:** each Latin word is English, Banglish, or both ("rat" is রাত next to Banglish, a rat next to English),
  decided with the English dictionary in Tesseract's model, ranked by EmbeddingGemma's tokenizer. A Banglish word gets
  its likeliest Bengali spellings from phonetic rules (consonants, conjuncts, vowel signs, ং ঃ ঁ ৎ, and the usual
  ambiguities: sh/s, o/a, i/ee, z/j…), steered by the 40 000 words of Tesseract's Bengali dictionary. Meaning then
  reads the query in Bengali ("বাসা থেকে office এর কাজ"); words match as typed or as any of those spellings; Name mode
  looks for the Bengali names too. All offline, in a few milliseconds.

## Checked

- `tests/test_reference.py`: llama.cpp's vectors against Google's official model (sentence-transformers, float32) on
  our own inputs. Worst cosine: CUDA 0.998, Vulkan 0.999, CPU 0.998. A backend that fails isn't used.
- `scripts/eval.py`: 32 queries (9 in Bengali) over `scripts/make_testbed.py`'s files: 32/32 first and 8/8 at the
  right page or moment, on CUDA, Vulkan and the CPU alike. With Banglish on, the same 32/32 and 8/8.
- Banglish: 32 queries over the same files (spellings of the same words, Banglish mixed with English): 30/32 first,
  32/32 in the top 3, 10/10 at the right moment or page (as typed: 19/32 first). Name mode: 15/15 first (10 Bengali
  names found from Banglish, 5 Latin names still first). A converted query takes about 1 ms more (5 ms at most), for
  the spellings; any other query about 0.1 ms; the model still reads one prompt.

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
uv run lazyfinds search atoshbaji                       # --as-typed: no Banglish
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
uv run python scripts/make_wordlists.py      # Banglish word lists, from the OCR models and the model's tokenizer
```

Build the installer (needs Inno Setup 6: `winget install JRSoftware.InnoSetup`):

```powershell
powershell -ExecutionPolicy Bypass -File packaging/build.ps1
```

It writes three files to `packaging/Output`; upload all three to the release tagged `v<version>`, since the web
installer downloads `lazyFinds-<version>-app.7z` from there: `lazyFinds-Setup-<version>.exe` (web, ~3 MB),
`lazyFinds-<version>-app.7z` and `lazyFinds-Setup-<version>-offline.exe`.

`LAZYFINDS_HOME` moves all of lazyFinds' files; `LAZYFINDS_INDEX` and `LAZYFINDS_CODE_INDEX` move only the indexes.

## License

MIT. EmbeddingGemma 2 is Apache 2.0 (Google), llama.cpp MIT (ggml-org), Tesseract and its models Apache 2.0.
The Banglish word lists in `src/lazyfinds/data` are taken from the dictionaries inside Tesseract's English and Bengali
models (tessdata_best, Apache 2.0) and from EmbeddingGemma 2's tokenizer (Apache 2.0); the transliteration rules are
lazyFinds' own.
