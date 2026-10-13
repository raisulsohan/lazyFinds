"""Builds a folder of test files for lazyFinds from what every Windows 11 PC has: its wallpapers, the Segoe UI Emoji and
Nirmala UI fonts (Bengali, shaped by Qt), the built-in speech voices (System.Speech) and ffmpeg. File names say nothing
about what's inside, so only meaning or exact words can find them.

    uv run python scripts/make_testbed.py <folder>
"""

from __future__ import annotations

import subprocess
import sys
import zipfile
from pathlib import Path

from PIL import Image
from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QColor, QFont, QGuiApplication, QImage, QPainter

W, H = 1600, 1066


def canvas(w: int, h: int, color) -> tuple[QImage, QPainter]:
    image = QImage(w, h, QImage.Format.Format_RGB32)
    image.fill(QColor(*color) if isinstance(color, tuple) else QColor(color))
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
    return image, painter


def scene(path: Path, sky, ground, things: list[tuple[str, int, int, int]], horizon: float = 0.6):
    """A simple picture: sky, ground, and emoji drawn large (glyph, x, y, size in points)."""
    image, p = canvas(W, H, sky)
    p.fillRect(0, int(H * horizon), W, H, QColor(*ground))
    for glyph, x, y, size in things:
        p.setFont(QFont("Segoe UI Emoji", size))
        p.drawText(QRect(x, y, size * 2, size * 2), Qt.AlignmentFlag.AlignCenter, glyph)
    p.end()
    image.save(str(path), quality=92)


def text_image(path: Path, lines: list[tuple[str, int, str]], size=(1440, 900), bg="#f4f4f4", bar=None):
    """A screenshot-like picture of text lines (text, point size, color). Bengali is shaped by Qt."""
    image, p = canvas(*size, bg)
    y = 40
    if bar:
        p.fillRect(0, 0, size[0], 44, QColor(bar[1]))
        p.setPen(QColor("white"))
        p.setFont(QFont("Segoe UI", 13))
        p.drawText(16, 29, bar[0])
        y = 120
    for text, points, color in lines:
        bengali = any("\u0980" <= c <= "\u09ff" for c in text)
        p.setFont(QFont("Nirmala UI" if bengali else "Segoe UI", points))
        p.setPen(QColor(color))
        y += int(points * 2.1)
        p.drawText(80, y, text)
    p.end()
    image.save(str(path))


def text_pdf(path: Path, pages: list[list[str]]):
    """A small PDF with a real text layer (Helvetica, ASCII), one list of lines per page."""
    objects = ["<< /Type /Catalog /Pages 2 0 R >>", None, "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    kids = []
    for lines in pages:
        esc = [line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)") for line in lines]
        body = "BT /F1 12 Tf 15 TL 56 790 Td " + " ".join(f"({line}) '" for line in esc) + " ET"
        objects.append(f"<< /Length {len(body)} >>\nstream\n{body}\nendstream")
        objects.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents {len(objects)} 0 R "
                       "/Resources << /Font << /F1 3 0 R >> >> >>")
        kids.append(f"{len(objects)} 0 R")
    objects[1] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(kids)} >>"
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for i, obj in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{obj}\nendobj\n".encode("latin-1")
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    path.write_bytes(out)


def docx(path: Path, paragraphs: list[str]):
    body = "".join(f'<w:p><w:r><w:t xml:space="preserve">{p}</w:t></w:r></w:p>' for p in paragraphs)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/'
                   'package/2006/content-types"><Default Extension="xml" ContentType="application/xml"/><Override '
                   'PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.'
                   'wordprocessingml.document.main+xml"/></Types>')
        z.writestr("word/document.xml", '<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/'
                   f'wordprocessingml/2006/main"><w:body>{body}</w:body></w:document>')


def speak(path: Path, text: str, voice: str):
    note = Path(str(path) + ".txt")
    note.write_text(text, encoding="utf-8")
    subprocess.run(["powershell", "-NoProfile", "-Command",
                    "Add-Type -AssemblyName System.Speech; $s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
                    f"$s.SelectVoice('{voice}'); $s.SetOutputToWaveFile('{path}'); "
                    f"$s.Speak([IO.File]::ReadAllText('{note}')); $s.Dispose()"], check=True)
    note.unlink()


def ffmpeg(*args):
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True)


def main(root: Path):
    QGuiApplication.instance() or QGuiApplication([])  # fonts and text shaping need it
    pics, shots, docs = root / "Photos", root / "Photos/Screenshots", root / "Documents"
    music, videos, work = root / "Recordings", root / "Videos", root / "_work"
    for d in (pics, shots, docs, music, videos, work):
        d.mkdir(parents=True, exist_ok=True)

    # Photos, named the way phones name them.
    scene(pics / "PXL_20260412_081500.jpg", (150, 205, 240), (95, 160, 80),
          [("🚲", 560, 420, 200), ("🌳", 120, 250, 160), ("🌳", 1250, 260, 150)])
    scene(pics / "PXL_20260412_190233.jpg", (245, 225, 240), (200, 160, 130),
          [("🎂", 560, 300, 230), ("🎈", 150, 120, 130), ("🎁", 1200, 480, 140)], horizon=0.72)
    scene(pics / "PXL_20260301_101010.jpg", (180, 215, 245), (245, 250, 255), [("🏔️", 380, 40, 260), ("⛷️", 900, 520, 180)])
    scene(pics / "PXL_20260519_154422.jpg", (40, 110, 170), (210, 190, 140),
          [("🐠", 300, 250, 160), ("🐟", 900, 380, 140), ("🪸", 650, 640, 120)], horizon=0.85)
    scene(pics / "PXL_20260720_120000.jpg", (130, 190, 250), (90, 140, 40),
          [("🌻", 200, 520, 150), ("🌻", 650, 500, 160), ("🌻", 1100, 520, 150)])
    # The one real photo: Windows' own wallpaper of a mountain lake at dusk.
    Image.open(r"C:\Windows\Web\Screen\img102.jpg").convert("RGB").save(pics / "PXL_20260601_203000.jpg", quality=92)

    text_image(shots / "Screenshot 2026-09-02 093012.png", [
        ("Transfer failed", 30, "#b3261e"), ("Insufficient balance in account ending 4410.", 18, "#222"),
        ("Reference: TXN-58K3Q", 18, "#222"), ("You can try again after adding money.", 16, "#555")],
        bar=("Mobile Banking", "#1f3a68"))
    text_image(shots / "Screenshot 2026-09-05 221147.png", [
        ("Traceback (most recent call last):", 16, "#ddd"), ('  File "report.py", line 3, in <module>', 16, "#ddd"),
        ("    import pandas as pd", 16, "#ddd"), ("ModuleNotFoundError: No module named 'pandas'", 18, "#ff7b72")],
        bg="#1e1e1e", bar=("PowerShell", "#333"))
    text_image(shots / "Screenshot 2026-09-10 170501.png", [
        ("Dentist appointment", 28, "#0b57d0"), ("Monday, 21 September, 10:30", 20, "#222"),
        ("Dr. Rahman, Green Road clinic, 2nd floor", 18, "#444")], bar=("Calendar", "#0b57d0"))
    text_image(shots / "Screenshot 2026-09-12 081920.png", [
        ("বিদ্যুৎ বিল পরিশোধ সফল", 28, "#1b5e20"), ("হিসাব নং ৮৮১২৭৫", 20, "#222"), ("পরিমাণ ২,৪৫০ টাকা", 20, "#222"),
        ("লেনদেন আইডি BLX7Q2", 18, "#444")], bar=("Payment", "#2e7d32"))

    docx(docs / "contract_signed.docx", [
        "EMPLOYMENT AGREEMENT", "1. Position. The employee joins as a junior data analyst.",
        "2. Working hours. Core hours are 10:00 to 16:00, Sunday to Thursday.",
        "3. Remote work. The employee may work from home up to two days per week, agreed with the team lead "
        "one week in advance. Equipment for home use is provided by the company.",
        "4. Leave. Annual leave is twenty working days."])
    (docs / "notes-17.md").write_text(
        "# Saturday baking\n\nCream 200 g butter with 150 g brown sugar. Beat in one egg and vanilla. Fold in 280 g "
        "flour, a pinch of salt and 200 g chocolate chips. Bake small balls of dough at 180 °C for 11 minutes.\n",
        encoding="utf-8")
    (docs / "খাতা.txt").write_text(
        "ছাদের বাগানে টমেটো চারা লাগানোর নিয়ম: বড় টবে দোআঁশ মাটি আর জৈব সার মেশান। প্রতিদিন অন্তত ছয় ঘণ্টা রোদ "
        "লাগে। চারা বড় হলে বাঁশের কাঠি দিয়ে বেঁধে দিন, আর সকালে পানি দিন।\n", encoding="utf-8")
    (docs / "mail-0412.html").write_text(
        "<html><head><title>Order</title><style>.x{}</style></head><body><h2>Order ORD-77A91X confirmed</h2>"
        "<p>Mechanical keyboard, brown switches &amp; spare keycaps</p><p>Arrives in 3 days.</p></body></html>",
        encoding="utf-8")
    text_pdf(docs / "manual.pdf", [
        ["WASHER MODEL WX-7", "", "Thank you for choosing our washing machine.",
         "Read these instructions before the first wash.", "Keep the packaging until the first wash is done."] * 2,
        ["TROUBLESHOOTING", "", "Error E21: the water does not drain.",
         "The drain pump filter may be blocked by lint or a coin.",
         "Open the small door at the bottom front, unscrew the filter and clean it."] * 2,
        ["WARRANTY", "", "The motor has a ten-year warranty.",
         "Other parts are covered for two years from the date of purchase."] * 2])
    letter = work / "scan.png"
    text_image(letter, [("জন্ম নিবন্ধন সনদ", 34, "#111"), ("নাম: আরিফ হোসেন", 22, "#111"),
                        ("জন্ম তারিখ: ১২ মার্চ ২০১৮", 22, "#111"), ("নিবন্ধন নং ২০১৮৪৪৭৭১৯", 22, "#111"),
                        ("ইউনিয়ন পরিষদ কার্যালয়", 20, "#333")], size=(1240, 1754), bg="#fbf8ef")
    Image.open(letter).convert("RGB").save(docs / "scan_0031.pdf", resolution=150)

    # A recording with two topics, long enough for several windows.
    speak(work / "a.wav", "Hello and welcome. First, a few tips on saving money. Write down every expense for one "
          "month. Then set a budget for food, transport and rent, and move ten percent of your salary into savings on "
          "the day you are paid. Cancel subscriptions you no longer use, and compare prices before big purchases. "
          "An emergency fund of three months of expenses gives you peace of mind.", "Microsoft Zira Desktop")
    speak(work / "b.wav", "Our second topic today is volcanoes. A volcano forms where molten rock, called magma, "
          "rises from deep inside the Earth. Pressure builds up under the crust until the magma breaks through. "
          "When it reaches the surface it is called lava. Over many eruptions, layers of lava and ash build the "
          "mountain higher.", "Microsoft David Desktop")
    ffmpeg("-i", str(work / "a.wav"), "-i", str(work / "b.wav"), "-filter_complex", "[0][1]concat=n=2:v=0:a=1",
           "-c:a", "libmp3lame", "-q:a", "4", str(music / "REC_0193.mp3"))

    # A video in three shots, with a narration.
    frames = [work / f"{i}.jpg" for i in range(3)]
    scene(frames[0], (200, 200, 210), (90, 90, 95), [("🚂", 520, 330, 250), ("🚉", 120, 120, 150)])
    scene(frames[1], (190, 225, 245), (240, 248, 255), [("🐧", 380, 420, 200), ("🐧", 820, 450, 180)])
    scene(frames[2], (10, 10, 40), (20, 20, 30), [("🎆", 300, 120, 230), ("🎇", 900, 200, 200)])
    speak(work / "v.wav", "A day out. In the morning we caught the train at the station. At the zoo we watched the "
          "penguins. At night there were fireworks over the river.", "Microsoft Zira Desktop")
    inputs = []
    for f in frames:
        inputs += ["-loop", "1", "-t", "8", "-i", str(f)]
    ffmpeg(*inputs, "-i", str(work / "v.wav"), "-filter_complex",
           "[0]scale=1280:720,setsar=1[a];[1]scale=1280:720,setsar=1[b];[2]scale=1280:720,setsar=1[c];"
           "[a][b][c]concat=n=3:v=1:a=0,format=yuv420p[v]", "-map", "[v]", "-map", "3:a", "-r", "25",
           "-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac", "-t", "24", str(videos / "VID_20260815_1830.mp4"))

    for f in work.iterdir():
        f.unlink()
    work.rmdir()
    print(f"Testbed in {root}")


if __name__ == "__main__":
    main(Path(sys.argv[1]).resolve())
