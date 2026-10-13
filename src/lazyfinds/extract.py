"""Readers: what each kind of file holds, as pieces ready for the model and for exact-word search.

Each reader returns a `Reading`: pieces with their place in the file, a line to show, the words as written, and what the
model should embed (a prompted text, or a picture or sound already encoded at its final size). Readers run in worker
threads; only the embedding happens on the GPU thread.

Defaults come from Google's model card (280 vision tokens per picture, 140 per video frame, 16 kHz mono sound, at most
30 s of sound per input, which we measured) and from measuring lazyFinds' own testbed (scripts/eval.py).
"""

from __future__ import annotations

import base64
import html.parser
import io
import json
import posixpath
import re
import subprocess
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps

from . import engine as E
from . import ocr, paths
from . import text as T

try:
    import pillow_heif

    pillow_heif.register_heif_opener()
except Exception:
    pass

Image.MAX_IMAGE_PIXELS = 400_000_000

PICTURE_TOKENS = 280       # the model's default
SCREEN_TOKENS = 280
PAGE_TOKENS = 280
SCENE_TOKENS = 140         # the model's default for video frames
SMALLEST_PICTURE = 100_000 # pixels: icons, thumbnails and buttons are below this
PAGE_WORDS = 25            # a page with fewer words in its text layer is read as a picture (a scan, a slide)
MAX_PAGES = 3000
MAX_PASSAGES = 4000
MAX_INNER_PICTURES = 300   # pictures read from one Word or PowerPoint file
SOUND_WINDOW = 20.0        # seconds of sound per vector (the model hears at most 30)
SOUND_STEP = 15.0          # windows overlap by 5 s, so a sentence on a boundary is whole in one
QUIET = 0.004              # RMS below this is silence
SCENE_CHANGE = 0.30        # ffmpeg's scene score for a new shot
SCENE_MAX_GAP = 30.0       # a frame at least this often, even in one long shot
MAX_SCENES = 600


class Skip(Exception):
    """Nothing worth a vector here (too small, silent, empty, locked): recorded as skipped, not as an error."""


class Unreadable(Exception):
    pass


@dataclass
class Piece:
    part: str                     # picture | screen_text | page | page_picture | passage | slide | inner_picture
                                  # | sound | scene
    source: str | None = None     # text | image | audio: what the vector is made from
    start: float | None = None    # an inner picture: its number among the document's pictures
    stop: float | None = None     # an inner picture: the slide it's on
    line: str | None = None
    body: str | None = None
    embed: tuple | None = None    # ("text", prompt) or ("media", base64)
    words: str | None = None      # more words for exact-word search only (never shown)


@dataclass
class Reading:
    pieces: list[Piece] = field(default_factory=list)
    meta: dict = field(default_factory=dict)
    note: str | None = None


# -- pictures


def open_picture(path: Path | io.BytesIO) -> Image.Image:
    try:
        image = Image.open(path)
        image.seek(0)                      # the first frame of a GIF or a multi-page TIFF
        image = ImageOps.exif_transpose(image)
        return image.convert("RGB")
    except Exception as error:
        raise Unreadable(f"can't read the picture ({type(error).__name__})")


def taken_on(path: Path) -> str | None:
    try:
        exif = Image.open(path).getexif()
        return str(exif.get_ifd(0x8769).get(0x9003) or exif.get(0x0132) or "") or None
    except Exception:
        return None


def read_picture(path: Path, screenshot: bool) -> Reading:
    picture = open_picture(path)
    if picture.width * picture.height < SMALLEST_PICTURE:
        raise Skip(f"too small to be a photo ({picture.width}x{picture.height})")
    reading = Reading(meta={"width": picture.width, "height": picture.height})
    if taken := taken_on(path):
        reading.meta["taken"] = taken
    tokens = SCREEN_TOKENS if screenshot else PICTURE_TOKENS
    reading.pieces.append(Piece("picture", "image", embed=("media", E.image_payload(picture, tokens))))
    if screenshot:
        words = ocr.text_of(picture)
        if words:
            reading.pieces.append(Piece("screen_text", line=T.preview(words), body=words))
    return reading


# -- PDF


def _bigrams(text: str) -> set[str]:
    s = re.sub(r"\s+", "", T.fold(text))
    return {s[i:i + 2] for i in range(len(s) - 1)}


def text_layer_is_broken(layer: str, ocr_text: str) -> bool:
    """Some PDF makers store Bengali in drawing order, not reading order (vowel signs before their consonants), so the
    text layer doesn't match what the page shows. Compared with what OCR reads off the rendered page, such a layer
    shares few character pairs."""
    a, b = _bigrams(layer), _bigrams(ocr_text)
    return bool(a and b) and len(a & b) / max(1, len(a | b)) < 0.45


def read_pdf(path: Path) -> Reading:
    import pypdfium2 as pdfium

    try:
        pdf = pdfium.PdfDocument(str(path))
    except pdfium.PdfiumError as error:
        if "password" in str(error).lower():
            raise Skip("locked with a password")
        raise Unreadable(f"can't open the PDF ({error})")
    try:
        total = len(pdf)
        if total == 0:
            raise Skip("no pages")
        title = (pdf.get_metadata_dict().get("Title") or "").strip() or path.stem
        reading = Reading(meta={"pages": total})
        pictured = 0
        for index in range(min(total, MAX_PAGES)):
            number = index + 1
            page = pdf[index]
            try:
                textpage = page.get_textpage()
                layer = textpage.get_text_bounded().replace("\r\n", "\n").strip()
                textpage.close()
            except Exception:
                layer = ""
            words = len(T.split_words(layer))
            bengali = ocr.has_bengali(layer, 20)
            if words >= PAGE_WORDS and not bengali:
                page.close()
                _add_page_text(reading, title, number, layer)
                continue
            w, h = page.get_size()
            picture = page.render(scale=1500 / max(w, h, 1)).to_pil().convert("RGB")
            page.close()
            if words >= PAGE_WORDS:   # Bengali text: keep the layer unless OCR shows it's garbled
                seen = ocr.tesseract_text(picture)
                _add_page_text(reading, title, number, seen if seen and text_layer_is_broken(layer, seen) else layer)
                continue
            seen = ocr.text_of(picture) if words < PAGE_WORDS else ""
            pictured += 1
            reading.pieces.append(Piece("page_picture", "image", start=number, line=T.preview(seen) if seen else None,
                                        body=seen or None, embed=("media", E.image_payload(picture, PAGE_TOKENS))))
        reading.meta["pictured_pages"] = pictured
        if total > MAX_PAGES:
            reading.note = f"read the first {MAX_PAGES} of {total} pages"
        return reading
    finally:
        pdf.close()


def _add_page_text(reading: Reading, title: str, number: int, text: str):
    for passage in T.passages(text) or [text]:
        embed = ("text", E.document_prompt(title, passage)) if T.mostly_words(passage) else None
        reading.pieces.append(Piece("page", "text", start=number, line=T.preview(passage), body=passage, embed=embed))


# -- documents


class _HTMLText(html.parser.HTMLParser):
    BLOCKS = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "table", "pre"}
    HIDDEN = {"script", "style", "head", "noscript", "template", "svg"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.HIDDEN:
            self.hidden += 1
        elif tag in self.BLOCKS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.HIDDEN:
            self.hidden = max(0, self.hidden - 1)
        elif tag in self.BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def html_text(source: str) -> str:
    parser = _HTMLText()
    parser.feed(source)
    text = "".join(parser.parts)
    return re.sub(r"\n\s*\n+", "\n\n", re.sub(r"[ \t]+", " ", text)).strip()


def decode(raw: bytes) -> str:
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16", "replace")
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            pass
    return raw.decode("latin-1")


def _xml_paragraphs(xml: str, paragraph_end: str) -> str:
    xml = xml.replace(paragraph_end, "\n").replace("<w:tab/>", "\t").replace("<w:br/>", "\n")
    xml = xml.replace("<text:tab/>", "\t").replace("<text:line-break/>", "\n")
    return html.unescape(re.sub(r"<[^>]+>", "", xml))


# Word and PowerPoint files are zips of XML. A picture in one is a file in the zip (word/media/image3.png), placed by
# an element that names it through the part's relationships (r:embed="rId7" -> media/image3.png) and holds its alt
# text (descr="..."). Each picture is read like a photo; its alt text is read as words where the picture stands.

INNER_TYPES = {".png", ".jpg", ".jpeg", ".jfif", ".gif", ".bmp", ".tif", ".tiff", ".webp", ".emf", ".wmf"}
_PLACED = re.compile(r"<(w:drawing|w:pict|p:pic|p:sp)\b.*?</\1>", re.S)   # what can show a picture
_ALT_OR_PICTURE = re.compile(r'\b(?:descr|alt|title)="([^"]+)"|\br:(?:embed|id)="([^"]+)"')
# What Office writes after alt text it made up itself
_MADE_UP = re.compile(r"\s*(?:Description automatically generated|AI-generated content may be incorrect).*$",
                      re.S | re.I)
_ENDS = "</w:p></a:p>"


@dataclass
class InnerPicture:
    member: str                   # the picture's file in the zip
    alt: str | None = None
    slide: int | None = None


def _relations(z: zipfile.ZipFile, part: str) -> dict[str, tuple[str, str]]:
    """A part's relationships: id -> (type, the zip member it points at). Links out of the file are left out."""
    folder, name = part.rsplit("/", 1)
    try:
        xml = z.read(f"{folder}/_rels/{name}.rels").decode("utf-8", "replace")
    except KeyError:
        return {}
    out = {}
    for tag in re.findall(r"<Relationship\b[^>]*>", xml):
        a = dict(re.findall(r'(\w+)="([^"]*)"', tag))
        if "Id" not in a or a.get("TargetMode") == "External":
            continue
        target = html.unescape(a.get("Target", ""))
        member = target.lstrip("/") if target.startswith("/") else posixpath.normpath(posixpath.join(folder, target))
        out[a["Id"]] = (a.get("Type", ""), member)
    return out


def _alt_text(raw: str) -> str | None:
    return _MADE_UP.sub("", html.unescape(raw)).strip() or None


def _with_alt_text(xml: str, relations: dict, found: list[InnerPicture], slide: int | None = None) -> str:
    """`xml` with the alt text of each picture written after it as text; the pictures added to `found`."""

    def one(m: re.Match) -> str:
        alt, alts = None, []
        for a in _ALT_OR_PICTURE.finditer(m.group(0)):
            if a.group(1):
                alt = alt or _alt_text(a.group(1))   # alt text comes before the picture it describes; descr first
                continue
            member = relations.get(a.group(2), ("", ""))[1]
            if posixpath.splitext(member)[1].lower() in INNER_TYPES:
                found.append(InnerPicture(member, alt, slide))
                if alt and alt not in alts:
                    alts.append(alt)
                alt = None
        # A paragraph end in either format, so each alt text is a line of its own
        return m.group(0) + "".join(f"{_ENDS}{html.escape(a, quote=False)}{_ENDS}" for a in alts)

    # Office writes a second copy of each drawing for old readers (mc:Fallback): read the first only
    return _PLACED.sub(one, re.sub(r"<mc:Fallback\b.*?</mc:Fallback>", "", xml, flags=re.S))


def _docx(z: zipfile.ZipFile) -> tuple[str, list[InnerPicture]]:
    """The text, and the pictures of the body (not the logo in the header)."""
    parts = [n for n in z.namelist() if re.fullmatch(r"word/(document|header\d*|footer\d*|footnotes)\.xml", n)]
    parts.sort(key=lambda n: n != "word/document.xml")
    pictures: list[InnerPicture] = []
    texts = []
    for n in parts:
        xml = z.read(n).decode("utf-8", "replace")
        if n == "word/document.xml":
            xml = _with_alt_text(xml, _relations(z, n), pictures)
        texts.append(_xml_paragraphs(xml, "</w:p>"))
    return "\n".join(texts), pictures


def docx_text(path: Path) -> str:
    with zipfile.ZipFile(path) as z:
        return _docx(z)[0]


def _slide_text(xml: str) -> str:
    xml = re.sub(r"<a:fld\b.*?</a:fld>", "", xml, flags=re.S)   # slide numbers and dates
    xml = re.sub(r"<a:br\b", "\n<a:br", xml)
    return _xml_paragraphs(xml, "</a:p>").strip()


def _pptx(z: zipfile.ZipFile) -> tuple[list[tuple[int, str]], list[InnerPicture]]:
    """Each slide's text (with its speaker notes) in the order the presentation shows them, and their pictures."""
    relations = _relations(z, "ppt/presentation.xml")
    order = re.findall(r'<p:sldId\b[^>]*?\br:id="([^"]+)"', z.read("ppt/presentation.xml").decode("utf-8", "replace"))
    slides: list[tuple[int, str]] = []
    pictures: list[InnerPicture] = []
    for number, rid in enumerate(order, 1):
        member = relations.get(rid, ("", ""))[1]
        try:
            xml = z.read(member).decode("utf-8", "replace")
        except KeyError:
            continue
        own = _relations(z, member)
        texts = [_slide_text(_with_alt_text(xml, own, pictures, number))]
        for kind, target in own.values():
            if kind.endswith("/notesSlide"):
                try:
                    texts.append(_slide_text(z.read(target).decode("utf-8", "replace")))
                except KeyError:
                    pass
        slides.append((number, "\n".join(t for t in texts if t)))
    return slides, pictures


def _unique(pictures: list[InnerPicture]) -> list[InnerPicture]:
    """One of each picture file (a logo on every slide is read once), where it first shows."""
    seen: dict[str, InnerPicture] = {}
    for p in pictures:
        if p.member not in seen:
            seen[p.member] = p
        elif not seen[p.member].alt:
            seen[p.member].alt = p.alt
    return list(seen.values())


def _inner_pictures(z: zipfile.ZipFile, ext: str) -> list[InnerPicture]:
    return _unique(_docx(z)[1] if ext == ".docx" else _pptx(z)[1])


def _read_inner(z: zipfile.ZipFile, pictures: list[InnerPicture]) -> list[Piece]:
    out: list[Piece] = []
    for number, p in enumerate(_unique(pictures), 1):
        if len(out) >= MAX_INNER_PICTURES:
            break
        try:
            picture = open_picture(io.BytesIO(z.read(p.member)))
        except (Unreadable, KeyError):
            continue
        if picture.width * picture.height < SMALLEST_PICTURE:   # icons, bullets, logos
            continue
        try:
            payload = E.image_payload(picture, PICTURE_TOKENS)
        except ValueError:   # a thin strip: a divider line, a banner
            continue
        seen = ocr.text_of(picture)
        out.append(Piece("inner_picture", "image", start=number, stop=p.slide,
                         line=p.alt or (T.preview(seen) if seen else None),
                         body="\n".join(t for t in (p.alt, seen) if t) or None, embed=("media", payload)))
    return out


def inner_picture(path: Path, number: int) -> Image.Image:
    """Picture `number` of a Word or PowerPoint file, as its inner_picture piece counts them."""
    with zipfile.ZipFile(path) as z:
        pictures = _inner_pictures(z, path.suffix.lower())
        return open_picture(io.BytesIO(z.read(pictures[number - 1].member)))


def odt_text(path: Path) -> str:
    with zipfile.ZipFile(path) as z:
        return _xml_paragraphs(z.read("content.xml").decode("utf-8", "replace"), "</text:p>")


def rtf_text(source: str) -> str:
    """Enough RTF for search: text outside destination groups, \\par as a line break, \\'hh and \\uN decoded."""
    out: list[str] = []
    skip_depth: list[bool] = []
    skipping = False
    skip_groups = {"fonttbl", "colortbl", "stylesheet", "info", "pict", "object", "themedata", "datastore",
                   "listtable", "listoverridetable", "rsidtbl", "latentstyles", "generator", "header", "footer"}
    tokens = re.finditer(r"\\u(-?\d+) ?\??|\\'([0-9a-fA-F]{2})|\\([a-zA-Z]+)(-?\d+)? ?|\\(.)|([{}])|([^\\{}]+)",
                         source)
    for m in tokens:
        uni, hexa, word, _, symbol, brace, plain = m.groups()
        if brace == "{":
            skip_depth.append(skipping)
        elif brace == "}":
            skipping = skip_depth.pop() if skip_depth else False
        elif skipping:
            continue
        elif uni is not None:
            out.append(chr(int(uni) % 0x10000))
        elif hexa:
            out.append(bytes([int(hexa, 16)]).decode("cp1252", "replace"))
        elif word:
            if word in skip_groups:
                skipping = True
            elif word in ("par", "line", "row", "sect", "page"):
                out.append("\n")
            elif word in ("tab", "cell"):
                out.append("\t")
        elif symbol:
            if symbol == "*":
                skipping = True
            elif symbol in "\\{}":
                out.append(symbol)
        elif plain:
            out.append(plain.replace("\r", "").replace("\n", ""))
    return "".join(out)


def read_document(path: Path) -> Reading:
    ext = path.suffix.lower()
    slides: list[tuple[int, str]] | None = None
    pictures: list[Piece] = []
    try:
        if ext in (".docx", ".pptx"):
            with zipfile.ZipFile(path) as z:
                if ext == ".docx":
                    text, inner = _docx(z)
                else:
                    slides, inner = _pptx(z)
                    text = "\n".join(t for _, t in slides)
                pictures = _read_inner(z, inner)
        elif ext == ".odt":
            text = odt_text(path)
        elif ext == ".rtf":
            text = rtf_text(decode(path.read_bytes()))
        elif ext in (".html", ".htm"):
            text = html_text(decode(path.read_bytes()))
        else:
            text = decode(path.read_bytes())
    except (zipfile.BadZipFile, KeyError) as error:
        raise Unreadable(f"can't read the document ({error})")
    if slides is None:
        passages = [(number, "passage", p) for number, p in enumerate(T.passages(text, limit=MAX_PASSAGES), 1)]
    else:   # a slide's place is its number
        passages = [(number, "slide", p) for number, t in slides for p in T.passages(t)][:MAX_PASSAGES]
    if not passages and not pictures:
        raise Skip("no text")
    reading = Reading(meta={"passages": len(passages), "characters": len(text)})
    if slides is not None:
        reading.meta["slides"] = len(slides)
    if pictures:
        reading.meta["pictures"] = len(pictures)
    for number, part, passage in passages:
        embed = ("text", E.document_prompt(path.stem, passage)) if T.mostly_words(passage) else None
        reading.pieces.append(Piece(part, "text", start=number, line=T.preview(passage), body=passage, embed=embed))
    reading.pieces.extend(pictures)
    return reading


# -- sound and video, through ffmpeg


FFMPEG_MISSING = "ffmpeg isn't installed (sound and video need it)"


def tool(name: str) -> str:
    found = paths.ffmpeg_exe(name)
    if found is None:
        raise Unreadable(f"{FFMPEG_MISSING}: Settings → Install ffmpeg, or winget install {paths.FFMPEG_WINGET_ID}")
    return str(found)


def probe(path: Path) -> dict:
    run = subprocess.run([tool("ffprobe"), "-v", "error", "-show_entries",
                          "format=duration:stream=codec_type,width,height", "-of", "json", str(path)],
                         capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        return json.loads(run.stdout or b"{}")
    except ValueError:
        return {}


def duration_of(info: dict) -> float:
    try:
        return max(0.0, float(info.get("format", {}).get("duration") or 0))
    except (TypeError, ValueError):
        return 0.0


def sound_windows(path: Path) -> list[tuple[float, float, str]]:
    """(start, end, base64 WAV) for each `SOUND_WINDOW` of sound, `SOUND_STEP` apart, silence left out. Decodes as a
    stream: memory holds one window, whatever the length."""
    rate = E.AUDIO_RATE
    size, step = int(SOUND_WINDOW * rate), int(SOUND_STEP * rate)
    process = subprocess.Popen([tool("ffmpeg"), "-v", "error", "-nostdin", "-i", str(path), "-vn", "-ac", "1",
                                "-ar", str(rate), "-f", "s16le", "-"], stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
    out: list[tuple[float, float, str]] = []
    pending = np.zeros(0, dtype=np.int16)
    offset = 0
    leftover = b""

    def keep(samples: np.ndarray, at: int):
        level = float(np.sqrt(np.mean((samples.astype(np.float32) / 32768) ** 2))) if len(samples) else 0.0
        if level >= QUIET:
            out.append((at / rate, (at + len(samples)) / rate,
                        base64.b64encode(E.wav_bytes(samples)).decode("ascii")))

    while chunk := process.stdout.read(1 << 16):
        chunk = leftover + chunk
        cut = len(chunk) - len(chunk) % 2
        leftover = chunk[cut:]
        pending = np.concatenate([pending, np.frombuffer(chunk[:cut], dtype="<i2")])
        while len(pending) >= size:
            keep(pending[:size], offset)
            pending = pending[step:]
            offset += step
    process.wait()
    tail_start = 0 if offset == 0 else size - step     # what the last full window already covered
    if len(pending) - tail_start >= 2 * rate:
        keep(pending, offset)
    return out


def read_sound(path: Path) -> Reading:
    info = probe(path)
    windows = sound_windows(path)
    if not windows:
        raise Skip("silent, or under 2 seconds")
    reading = Reading(meta={"duration": round(duration_of(info) or windows[-1][1], 1)})
    for start, stop, wav in windows:
        reading.pieces.append(Piece("sound", "audio", start=start, stop=stop, embed=("media", wav)))
    return reading


def scenes(path: Path, info: dict, longest: int = 960) -> list[tuple[float, str]]:
    """One frame per shot (ffmpeg's scene detection), and one at least every `SCENE_MAX_GAP` seconds in a long shot,
    as (second, picture payload). Frames come through a pipe as raw RGB with their times on stderr."""
    video = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), None)
    if not video or not video.get("width"):
        return []
    width, height = int(video["width"]), int(video["height"])
    scale = min(1.0, longest / max(width, height))
    w, h = max(2, int(width * scale) // 2 * 2), max(2, int(height * scale) // 2 * 2)
    pick = (f"select='eq(n\\,0)+gt(scene\\,{SCENE_CHANGE})+isnan(prev_selected_t)"
            f"+gte(t-prev_selected_t\\,{SCENE_MAX_GAP})',showinfo,scale={w}:{h}")
    process = subprocess.Popen([tool("ffmpeg"), "-v", "info", "-nostdin", "-i", str(path), "-an", "-vf", pick,
                                "-fps_mode", "vfr", "-pix_fmt", "rgb24", "-f", "rawvideo", "-"],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW)
    times: list[float] = []

    def read_times():
        for raw in process.stderr:
            m = re.search(rb"pts_time:\s*([0-9.]+)", raw)
            if m and b"Parsed_showinfo" in raw:
                times.append(float(m.group(1)))

    import threading

    reader = threading.Thread(target=read_times, daemon=True)
    reader.start()
    frames: list[np.ndarray] = []
    size = w * h * 3
    while len(frames) < MAX_SCENES:
        data = process.stdout.read(size)
        if len(data) < size:
            break
        frames.append(np.frombuffer(data, dtype=np.uint8).reshape(h, w, 3))
    process.stdout.close()
    process.wait()
    reader.join(5)
    out = []
    for i, frame in enumerate(frames):
        second = times[i] if i < len(times) else 0.0
        out.append((second, E.image_payload(Image.fromarray(frame), SCENE_TOKENS)))
    return out


def read_video(path: Path) -> Reading:
    info = probe(path)
    kinds = {s.get("codec_type") for s in info.get("streams", [])}
    duration = duration_of(info)
    shots = scenes(path, info) if "video" in kinds else []
    sound = sound_windows(path) if "audio" in kinds else []
    if not shots and not sound:
        raise Skip("no picture or sound")
    reading = Reading(meta={"duration": round(duration, 1), "scenes": len(shots), "sound_windows": len(sound)})
    for i, (second, payload) in enumerate(shots):
        stop = shots[i + 1][0] if i + 1 < len(shots) else duration
        reading.pieces.append(Piece("scene", "image", start=second, stop=stop, embed=("media", payload)))
    for start, stop, wav in sound:
        reading.pieces.append(Piece("sound", "audio", start=start, stop=stop, embed=("media", wav)))
    return reading


def read(path: Path, kind: str) -> Reading:
    if kind in ("image", "screenshot"):
        return read_picture(path, kind == "screenshot")
    if kind == "pdf":
        return read_pdf(path)
    if kind == "doc":
        return read_document(path)
    if kind == "audio":
        return read_sound(path)
    if kind == "video":
        return read_video(path)
    raise Skip(f"no reader for {kind}")


def thumbnail(path: Path, kind: str, place: float | None, size: int = 320,
              part: str | None = None) -> Image.Image | None:
    """A small picture for the results list and the preview: the picture, the PDF page, the video's frame there, or
    the picture inside a document."""
    try:
        if part == "inner_picture":
            image = inner_picture(path, int(place or 1))
        elif kind in ("image", "screenshot"):
            image = open_picture(path)
        elif kind == "pdf":
            import pypdfium2 as pdfium

            pdf = pdfium.PdfDocument(str(path))
            page = pdf[max(0, int(place or 1) - 1)]
            w, h = page.get_size()
            image = page.render(scale=size * 2 / max(w, h, 1)).to_pil().convert("RGB")
            pdf.close()
        elif kind == "video":
            run = subprocess.run([tool("ffmpeg"), "-v", "error", "-nostdin", "-ss", f"{max(0.0, place or 0):.2f}",
                                  "-i", str(path), "-frames:v", "1", "-vf", f"scale={size * 2}:-2", "-f", "image2pipe",
                                  "-c:v", "png", "-"], capture_output=True, timeout=20,
                                 creationflags=subprocess.CREATE_NO_WINDOW)
            image = Image.open(io.BytesIO(run.stdout)).convert("RGB")
        else:
            return None
        image.thumbnail((size * 2, size * 2), Image.Resampling.LANCZOS)
        return image
    except Exception:
        return None
