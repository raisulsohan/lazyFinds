"""Writes packaging/lazyFinds.ico from the app's own icon drawing (16 to 256 px)."""

import sys
from pathlib import Path

from PIL import Image
from PySide6.QtCore import QBuffer, QIODevice
from PySide6.QtWidgets import QApplication

from lazyfinds.app import make_icon

app = QApplication(sys.argv[:1])
icon = make_icon(256)
buffer = QBuffer()
buffer.open(QIODevice.OpenModeFlag.WriteOnly)
icon.pixmap(256, 256).save(buffer, "PNG")
image = Image.open(__import__("io").BytesIO(bytes(buffer.data())))
out = Path(__file__).with_name("lazyFinds.ico")
image.save(out, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print(out)
