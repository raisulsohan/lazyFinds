"""lazyFinds for Windows: a tray icon, a global hotkey, and a search panel over the index.

Ctrl+Shift+Space (changeable) opens the panel from any app. Type what you're looking for: words match at once, meaning
follows a moment later. Enter opens the result where it matched, Space previews it, Ctrl+Enter shows it in File
Explorer, Ctrl+C copies the file. Bengali typed in Latin letters is searched in Bengali too; a line under the field
says so, and Ctrl+B searches that query as typed.
"""

from __future__ import annotations

import ctypes
import html
import logging
import logging.handlers
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from ctypes import wintypes
from pathlib import Path

from PySide6.QtCore import QEvent, QMimeData, QObject, QPoint, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import (QAction, QColor, QCursor, QGuiApplication, QIcon, QImage, QLinearGradient, QPainter,
                           QPainterPath, QPen, QPixmap)
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog, QFileDialog, QFrame, QHBoxLayout, QLabel, QLineEdit,
                               QListWidget, QListWidgetItem, QMenu, QMessageBox, QProgressBar, QPushButton,
                               QSizePolicy, QSlider, QSystemTrayIcon, QTextBrowser, QVBoxLayout, QWidget)

from . import banglish as B
from . import code as C
from . import extract, opener, paths, rules
from . import text as T
from .search import Hit, strong_count
from .service import IndexService, QueryService, code_index_path, index_path
from .settings import Settings

log = logging.getLogger("lazyfinds")
APP_NAME = "lazyFinds"

KIND_LABEL = {"screenshot": "Screenshot", "image": "Picture", "pdf": "PDF", "doc": "Document", "audio": "Audio",
              "video": "Video", "code": "Code"}
KIND_GLYPH = {"pdf": "📄", "doc": "📝", "audio": "🎵", "video": "🎬", "image": "🖼️", "screenshot": "🖥️", "code": "💻"}
NAME_FILTERS = [("All", None), ("Folders", "folders"), ("Pictures", rules.KINDS["image"]),
                ("Documents", rules.KINDS["doc"] | rules.KINDS["pdf"] | {"doc", "xls", "xlsx", "ppt", "pptx", "csv"}),
                ("Audio", rules.KINDS["audio"]), ("Video", rules.KINDS["video"])]
FILTERS = [("All", None), ("Pictures", {"image", "screenshot"}), ("PDFs", {"pdf"}), ("Docs", {"doc"}),
           ("Audio", {"audio"}), ("Video", {"video"})]


def make_icon(size: int = 64) -> QIcon:
    """A blue tile with a white magnifier."""
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    g = QLinearGradient(0, 0, size, size)
    g.setColorAt(0, QColor("#4f8cff"))
    g.setColorAt(1, QColor("#2a4fd6"))
    path = QPainterPath()
    path.addRoundedRect(0, 0, size, size, size * 0.22, size * 0.22)
    p.fillPath(path, g)
    pen = QPen(QColor("white"), size * 0.1)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    p.setPen(pen)
    r = size * 0.22
    c = QPoint(int(size * 0.44), int(size * 0.42))
    p.drawEllipse(c, int(r), int(r))
    p.drawLine(int(c.x() + r * 0.72), int(c.y() + r * 0.72), int(size * 0.76), int(size * 0.76))
    p.end()
    return QIcon(pix)


def pil_to_qimage(image) -> QImage:
    rgba = image.convert("RGBA")
    data = rgba.tobytes()
    return QImage(data, rgba.width, rgba.height, rgba.width * 4, QImage.Format.Format_RGBA8888).copy()


def fmt_time(seconds: float) -> str:
    s = int(max(0, seconds))
    h, rem = divmod(s, 3600)
    return f"{h}:{rem // 60:02d}:{rem % 60:02d}" if h else f"{rem // 60}:{rem % 60:02d}"


def place_of(hit: Hit) -> str:
    if hit.start is None:
        return ""
    if hit.kind in ("audio", "video"):
        return f"at {fmt_time(hit.start)}"
    if hit.kind == "pdf":
        return f"page {int(hit.start)}"
    if hit.kind == "code":
        return f"line {int(hit.start)}" if hit.part == "code" else f"cell {int(hit.start)}"
    return ""


# -- global hotkey (RegisterHotKey on the panel's window)

MODS = {"ctrl": 0x2, "control": 0x2, "shift": 0x4, "alt": 0x1, "win": 0x8}
WM_HOTKEY = 0x0312


def parse_hotkey(text: str) -> tuple[int, int] | None:
    mods, key = 0, None
    for part in text.replace(" ", "").split("+"):
        low = part.lower()
        if low in MODS:
            mods |= MODS[low]
        elif low == "space":
            key = 0x20
        elif len(part) == 1 and part.isalnum():
            key = ord(part.upper())
        elif low.startswith("f") and low[1:].isdigit() and 1 <= int(low[1:]) <= 24:
            key = 0x6F + int(low[1:])
        else:
            return None
    return (mods | 0x4000, key) if key and mods else None   # MOD_NOREPEAT


class Bridge(QObject):
    """Signals from the worker threads, delivered on the UI thread."""
    results = Signal(int, str, object, bool, str)   # seq, query, hits, final, its Bengali form ("" for none)
    name_status = Signal(str)
    status = Signal(object)
    thumb = Signal(str, QImage)
    setup_progress = Signal(str, int, int)
    setup_done = Signal(object)


# -- one result row


class ResultRow(QWidget):
    def __init__(self, hit: Hit):
        super().__init__()
        self.hit = hit
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 6, 10, 6)
        layout.setSpacing(12)
        self.thumb = QLabel(KIND_GLYPH.get(hit.kind, "📄"))
        self.thumb.setFixedSize(64, 48)
        self.thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.thumb.setObjectName("thumb")
        layout.addWidget(self.thumb)
        text = QVBoxLayout()
        text.setSpacing(1)
        name = Path(hit.path).name
        title = QLabel(html.escape(name))
        title.setObjectName("title")
        title.setTextFormat(Qt.TextFormat.RichText)
        folder = str(Path(hit.path).parent)
        home = str(Path.home())
        if folder.lower().startswith(home.lower()):
            folder = "~" + folder[len(home):]
        sub = " · ".join(x for x in [KIND_LABEL.get(hit.kind, hit.kind), place_of(hit), folder] if x)
        subtitle = QLabel(html.escape(sub))
        subtitle.setObjectName("sub")
        text.addWidget(title)
        text.addWidget(subtitle)
        if hit.line:
            e = html.escape(hit.line[:220]).replace("«", "<b>").replace("»", "</b>")
            excerpt = QLabel(e)
            excerpt.setObjectName("excerpt")
            excerpt.setTextFormat(Qt.TextFormat.RichText)
            excerpt.setWordWrap(False)
            text.addWidget(excerpt)
        layout.addLayout(text, 1)

    def set_thumb(self, image: QImage):
        pix = QPixmap.fromImage(image).scaled(64, 48, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                                              Qt.TransformationMode.SmoothTransformation)
        x, y = (pix.width() - 64) // 2, (pix.height() - 48) // 2
        self.thumb.setPixmap(pix.copy(x, y, 64, 48))


class NameRow(QWidget):
    """A name result: the file's own icon, the name with the typed pieces in bold, where it is, size and date."""

    icons = None

    def __init__(self, hit: Hit):
        super().__init__()
        from PySide6.QtCore import QFileInfo
        from PySide6.QtWidgets import QFileIconProvider

        if NameRow.icons is None:
            NameRow.icons = (QFileIconProvider(), {})
        provider, cache = NameRow.icons
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 3, 10, 3)
        layout.setSpacing(10)
        icon_label = QLabel()
        key = "<folder>" if hit.kind == "folder" else (Path(hit.path).suffix.lower() or "<none>")
        if key in (".exe", ".lnk", ".ico", ".url"):   # these carry their own icons
            key = hit.path
        if key not in cache:
            cache[key] = provider.icon(QFileInfo(hit.path)).pixmap(24, 24)
        icon_label.setPixmap(cache[key])
        icon_label.setFixedSize(26, 26)
        layout.addWidget(icon_label)
        name = Path(hit.path).name
        marked, cursor = [], 0
        for a, b in hit.meta.get("marks", []):
            if a < cursor:
                continue
            marked.append(html.escape(name[cursor:a]) + "<b>" + html.escape(name[a:b]) + "</b>")
            cursor = b
        marked.append(html.escape(name[cursor:]))
        title = QLabel("".join(marked))
        title.setObjectName("name")
        title.setTextFormat(Qt.TextFormat.RichText)
        text = QVBoxLayout()
        text.setSpacing(0)
        text.addWidget(title)
        where = QLabel()
        where.setObjectName("sub")
        where.setTextFormat(Qt.TextFormat.PlainText)
        folder = str(Path(hit.path).parent)
        # A long path is shortened in the middle, so the size and date stay in view.
        where.setText(where.fontMetrics().elidedText(folder, Qt.TextElideMode.ElideMiddle, 500))
        where.setToolTip(folder)
        where.setMinimumWidth(0)
        text.addWidget(where)
        layout.addLayout(text, 1)
        facts = [] if hit.kind == "folder" else [human_size(hit.size)]
        facts.append(time.strftime("%Y-%m-%d %H:%M", time.localtime(hit.modified)) if hit.modified else "")
        side = QLabel("\n".join(f for f in facts if f))
        side.setObjectName("sub")
        side.setTextFormat(Qt.TextFormat.PlainText)
        side.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        side.setFixedWidth(110)
        layout.addWidget(side)


def human_size(n: float) -> str:
    for unit in ("bytes", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:,.0f} {unit}" if unit == "bytes" else f"{n:,.1f} {unit}"
        n /= 1024
    return ""


def name_hit(found) -> Hit:
    """A name result as a Hit, so opening, copying and previewing work as for any result."""
    kind = "folder" if found.is_dir else (rules.kind_of(Path(found.path)) or "file")
    return Hit(path=found.path, kind=kind, score=0.0, meaning=None, words=1.0, strong=True, piece=0, part="name",
               start=None, stop=None, line=None, modified=found.modified, size=found.size,
               meta={"marks": found.marks})


# -- the search panel


PANEL_STYLE = """
#card { background: palette(window); border: 1px solid rgba(128,128,128,0.35); border-radius: 14px; }
QLineEdit#query { font-size: 20px; padding: 10px 12px; border: none; background: transparent; }
QListWidget { border: none; background: transparent; outline: none; }
QListWidget::item { border-radius: 8px; margin: 1px 6px; }
QListWidget::item:selected { background: rgba(79,140,255,0.22); }
QLabel#title { font-size: 14px; font-weight: 600; }
QLabel#name { font-size: 13px; }
QPushButton#mode { border: 1px solid rgba(128,128,128,0.45); padding: 3px 12px; font-size: 12px; font-weight: 600; }
QPushButton#mode:checked { background: rgba(79,140,255,0.85); color: white; border-color: rgba(79,140,255,0.9); }
QLabel#sub { color: rgba(128,128,128,0.95); font-size: 12px; }
QLabel#excerpt { color: palette(text); font-size: 12px; }
QLabel#thumb { background: rgba(128,128,128,0.12); border-radius: 6px; font-size: 22px; }
QLabel#footer, QLabel#section { color: rgba(128,128,128,0.95); font-size: 11px; padding: 2px 14px; }
QLabel#also { color: rgba(128,128,128,0.95); font-size: 12px; padding: 0 14px 2px 14px; }
QPushButton#chip { border: 1px solid rgba(128,128,128,0.35); border-radius: 11px; padding: 2px 10px; font-size: 12px; }
QPushButton#chip:checked { background: rgba(79,140,255,0.25); border-color: rgba(79,140,255,0.8); }
"""


class SearchPanel(QWidget):
    def __init__(self, app: "LazyFindsApp"):
        super().__init__(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool |
                         Qt.WindowType.WindowStaysOnTopHint)
        self.app = app
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setWindowTitle(APP_NAME)
        self.setStyleSheet(PANEL_STYLE)
        self.resize(760, 520)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 8)
        card = QFrame()
        card.setObjectName("card")
        outer.addWidget(card)
        box = QVBoxLayout(card)
        box.setContentsMargins(6, 6, 6, 6)
        box.setSpacing(4)
        self.query = QLineEdit()
        self.query.setObjectName("query")
        self.query.setPlaceholderText("Describe what you're looking for…")
        self.query.textChanged.connect(self._typed)
        self.query.installEventFilter(self)
        box.addWidget(self.query)
        # "also searched: আতশবাজি" when a Banglish query was searched in Bengali; a click or Ctrl+B searches it as typed.
        self.also = QLabel()
        self.also.setObjectName("also")
        self.also.setTextFormat(Qt.TextFormat.RichText)
        self.also.linkActivated.connect(lambda _: self.toggle_banglish())
        self.also.hide()
        box.addWidget(self.also)
        self.as_typed: str | None = None   # the query to search only as typed
        chips = QHBoxLayout()
        chips.setContentsMargins(10, 0, 10, 4)
        chips.setSpacing(0)
        self.mode_buttons = {}
        for mode, label, tip in (("meaning", "Meaning", "Describe what's inside a file"),
                                 ("name", "Name", "Any part of a file or folder name, on the whole PC")):
            b = QPushButton(label)
            b.setObjectName("mode")
            b.setCheckable(True)
            b.setToolTip(tip + " (Tab switches)")
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            b.clicked.connect(lambda _=False, m=mode: self.set_mode(m))
            chips.addWidget(b)
            self.mode_buttons[mode] = b
        chips.addSpacing(12)
        self.chip_row = QHBoxLayout()
        self.chip_row.setSpacing(4)
        chips.addLayout(self.chip_row)
        chips.addStretch(1)
        box.addLayout(chips)
        self.filters: list[QPushButton] = []
        self.kinds = None
        self.mode = ""
        self.list = QListWidget()
        self.list.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.list.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        self.list.itemDoubleClicked.connect(lambda item: self.open_current())
        box.addWidget(self.list, 1)
        self.footer = QLabel()
        self.footer.setObjectName("footer")
        box.addWidget(self.footer)
        self.seq = 0
        self.moved = False
        self.rows: dict[str, list[ResultRow]] = {}
        self.thumbs: dict[str, QImage] = {}
        self.debounce = QTimer(self, singleShot=True, interval=120)
        self.debounce.timeout.connect(self._submit)
        self.hotkey_id = 1
        self.hotkey_ok = False
        self.set_mode(Settings.load().search_mode, save=False)

    # hotkey

    def register_hotkey(self, text: str) -> bool:
        user32 = ctypes.windll.user32
        hwnd = int(self.winId())
        user32.UnregisterHotKey(wintypes.HWND(hwnd), self.hotkey_id)
        parsed = parse_hotkey(text)
        self.hotkey_ok = bool(parsed) and bool(user32.RegisterHotKey(wintypes.HWND(hwnd), self.hotkey_id, *parsed))
        return self.hotkey_ok

    def nativeEvent(self, event_type, message):
        if event_type == b"windows_generic_MSG":
            msg = wintypes.MSG.from_address(int(message))
            if msg.message == WM_HOTKEY and msg.wParam == self.hotkey_id:
                self.toggle()
                return True, 0
        return False, 0

    # showing

    def toggle(self):
        if self.isVisible() and self.isActiveWindow():
            self.hide()
        else:
            self.summon()

    def summon(self):
        screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        geo = screen.availableGeometry()
        self.move(geo.x() + (geo.width() - self.width()) // 2, geo.y() + int(geo.height() * 0.18))
        self.show()
        self.raise_()
        self.activateWindow()
        ctypes.windll.user32.SetForegroundWindow(wintypes.HWND(int(self.winId())))
        self.query.setFocus()
        self.query.selectAll()
        self.app.query.warm()
        self.update_footer()

    def event(self, e):
        if e.type() == QEvent.Type.WindowDeactivate and not self.app.preview.isVisible():
            QTimer.singleShot(0, self._maybe_hide)
        return super().event(e)

    def _maybe_hide(self):
        active = QApplication.activeWindow()
        if active is None or active is self:
            if not self.isActiveWindow():
                self.hide()

    # typing and filters

    def _typed(self, _):
        self.moved = False
        self.debounce.start()

    def set_mode(self, mode: str, save: bool = True):
        mode = mode if mode in ("meaning", "name") else "meaning"
        for m, b in self.mode_buttons.items():
            b.setChecked(m == mode)
        if mode != self.mode:
            self.mode = mode
            for b in self.filters:
                b.deleteLater()
            self.filters = []
            for label, kinds in (NAME_FILTERS if mode == "name" else FILTERS):
                b = QPushButton(label)
                b.setObjectName("chip")
                b.setCheckable(True)
                b.setChecked(kinds is None)
                b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
                b.clicked.connect(lambda _=False, k=kinds, btn=b: self._filter(k, btn))
                self.chip_row.addWidget(b)
                self.filters.append(b)
            self.kinds = None
            self.query.setPlaceholderText("Any part of a file or folder name…" if mode == "name"
                                          else "Describe what you're looking for…")
            if save:
                s = Settings.load()
                s.search_mode = mode
                s.save()
            self.list.clear()   # the other mode's results mustn't linger while this one's arrive
            if self.query.text().strip():
                self._submit()
        self.update_footer()

    def _filter(self, kinds, button):
        for b in self.filters:
            b.setChecked(b is button)
        self.kinds = kinds
        self._submit()

    def _banglish(self) -> str:
        """How this query treats Banglish: "on", "typed" (turned off for it) or "off" (turned off in Settings)."""
        if not Settings.load().banglish:
            return "off"
        return "typed" if self.as_typed == self.query.text().strip() else "on"

    def toggle_banglish(self):
        """Searches the query as typed, or in Bengali again."""
        if not self.also.isVisible():
            return
        text = self.query.text().strip()
        self.as_typed = None if self.as_typed == text else text
        self._submit()

    def _submit(self):
        self.seq += 1
        if self.mode == "name":
            self.app.find_names(self.seq, self.query.text(), self.kinds, self._banglish())
        else:
            self.app.query.submit(self.seq, self.query.text(), self.kinds, self._banglish())

    def _show_also(self, also: str):
        if not also:
            self.also.hide()
            return
        shown = html.escape(also)
        if self._banglish() == "typed":
            self.also.setText(f"searched as typed · <a href='on'>also search <b>{shown}</b></a> (Ctrl+B)")
        else:
            self.also.setText(f"also searched: <b>{shown}</b> · <a href='typed'>search only as typed</a> (Ctrl+B)")
        self.also.show()

    def show_results(self, seq: int, text: str, hits: list[Hit], final: bool, also: str = ""):
        if seq != self.seq:
            return
        self._show_also(also)
        current = self.current_hit()
        self.list.clear()
        self.rows.clear()
        strong = strong_count(hits)
        for i, hit in enumerate(hits):
            if i == strong and final:
                sep = QListWidgetItem()
                sep.setFlags(Qt.ItemFlag.NoItemFlags)
                label = QLabel("Weaker matches")
                label.setObjectName("section")
                sep.setSizeHint(QSize(10, 22))
                self.list.addItem(sep)
                self.list.setItemWidget(sep, label)
            item = QListWidgetItem()
            if hit.part == "name":
                row = NameRow(hit)
                item.setSizeHint(row.sizeHint().expandedTo(QSize(10, 40)))
                item.setData(Qt.ItemDataRole.UserRole, hit)
                self.list.addItem(item)
                self.list.setItemWidget(item, row)
                continue
            row = ResultRow(hit)
            item.setSizeHint(row.sizeHint().expandedTo(QSize(10, 60)))
            item.setData(Qt.ItemDataRole.UserRole, hit)
            self.list.addItem(item)
            self.list.setItemWidget(item, row)
            key = self._thumb_key(hit)
            self.rows.setdefault(key, []).append(row)
            if key in self.thumbs:
                row.set_thumb(self.thumbs[key])
            elif hit.kind in ("image", "screenshot", "pdf", "video"):
                self.app.load_thumb(key, hit)
        # Once you've moved to a result, it stays selected when meaning results arrive; otherwise the top one is.
        target = 0
        if current and self.moved:
            for i in range(self.list.count()):
                h = self.list.item(i).data(Qt.ItemDataRole.UserRole)
                if h and h.path == current.path:
                    target = i
                    break
        self._select(target if self.list.count() else -1)
        if text.strip() and final and not hits:
            self.footer.setText("No name holds that. Tab searches by meaning." if self.mode == "name" else
                                "Nothing found. Try other words, or describe what it shows. Tab searches names.")
        else:
            self.update_footer(searching=not final and bool(text.strip()))

    @staticmethod
    def _thumb_key(hit: Hit) -> str:
        loc = int(hit.start) if hit.start is not None and hit.kind in ("pdf", "video") else 0
        return f"{hit.path}|{loc}"

    def set_thumb(self, key: str, image: QImage):
        self.thumbs[key] = image
        if len(self.thumbs) > 300:
            self.thumbs.pop(next(iter(self.thumbs)))
        for row in self.rows.get(key, []):
            row.set_thumb(image)

    def update_footer(self, searching: bool = False):
        if self.mode == "name":
            self.footer.setText("   ".join(x for x in (self.app.name_status,
                                "↩ open · Ctrl+↩ show in folder · Ctrl+C copy · Tab: search by meaning") if x))
            return
        s = self.app.index_status
        parts = []
        if searching:
            parts.append("Searching by meaning…")
        if s.get("state") == "indexing":
            parts.append(f"Reading files {s['done']}/{s['total']}")
        elif s.get("state") == "scanning":
            parts.append("Looking for new files…")
        elif s.get("state") == "paused":
            parts.append("Indexing paused")
        elif s.get("state") == "error":
            parts.append("Problem: " + (s.get("error") or "")[:80])
        if self.app.query.engine_error:
            parts.append("Meaning search unavailable: " + self.app.query.engine_error[:60])
        parts.append("↩ open · Space preview · Ctrl+↩ show in folder · code: searches code · Tab: search names")
        self.footer.setText("   ".join(parts))

    # keys

    def _select(self, row: int):
        if row < 0:
            return
        item = self.list.item(row)
        if item and item.flags() & Qt.ItemFlag.ItemIsSelectable:
            self.list.setCurrentRow(row)
            self.list.scrollToItem(item)

    def _step(self, delta: int):
        n = self.list.count()
        if not n:
            return
        row = self.list.currentRow()
        while True:
            row = max(0, min(n - 1, row + delta))
            item = self.list.item(row)
            if item.flags() & Qt.ItemFlag.ItemIsSelectable or row in (0, n - 1):
                break
        self._select(row)
        self.moved = True
        if self.app.preview.isVisible():
            self.preview_current()

    def current_hit(self) -> Hit | None:
        item = self.list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def eventFilter(self, obj, e):
        if obj is self.query and e.type() == QEvent.Type.KeyPress:
            key, mods = e.key(), e.modifiers()
            ctrl = bool(mods & Qt.KeyboardModifier.ControlModifier)
            if key in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab):
                self.set_mode("name" if self.mode == "meaning" else "meaning")
                return True
            if key == Qt.Key.Key_Down:
                self._step(1)
                return True
            if key == Qt.Key.Key_Up:
                self._step(-1)
                return True
            if key in (Qt.Key.Key_PageDown, Qt.Key.Key_PageUp):
                self._step(5 if key == Qt.Key.Key_PageDown else -5)
                return True
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self.reveal_current() if ctrl else self.open_current()
                return True
            if key == Qt.Key.Key_Escape:
                if self.app.preview.isVisible():
                    self.app.preview.close()
                else:
                    self.hide()
                return True
            if key == Qt.Key.Key_Space and self.moved and not ctrl:
                self.preview_current()
                return True
            if ctrl and key == Qt.Key.Key_C and not self.query.hasSelectedText():
                self.copy_current()
                return True
            if ctrl and key == Qt.Key.Key_B:
                self.toggle_banglish()
                return True
            if ctrl and key == Qt.Key.Key_Comma:
                self.app.show_settings()
                return True
        return super().eventFilter(obj, e)

    # actions

    def open_current(self):
        hit = self.current_hit()
        if not hit:
            return
        if not os.path.exists(hit.path):
            self.footer.setText("That file isn't there any more. It leaves the index on the next pass.")
            return
        self.hide()
        try:
            if hit.kind == "pdf":
                opener.open_pdf(hit.path, int(hit.start) if hit.start else None)
            elif hit.kind == "code":
                C.open_at(hit.path, hit.start if hit.part == "code" else None)
            elif hit.kind in ("audio", "video"):
                if not opener.open_media_at(hit.path, hit.start):
                    self.app.preview.show_hit(hit, autoplay=True)
            else:
                opener.open_plain(hit.path)
        except OSError as error:
            QMessageBox.warning(None, APP_NAME, f"Can't open {hit.path}:\n{error}")

    def reveal_current(self):
        hit = self.current_hit()
        if hit:
            self.hide()
            opener.reveal(hit.path)

    def copy_current(self):
        hit = self.current_hit()
        if hit:
            data = QMimeData()
            data.setUrls([QUrl.fromLocalFile(hit.path)])
            data.setText(hit.path)
            QGuiApplication.clipboard().setMimeData(data)
            self.footer.setText(f"Copied {Path(hit.path).name}. Paste it anywhere.")

    def preview_current(self):
        hit = self.current_hit()
        if hit and (hit.part != "name" or hit.kind in ("image", "screenshot", "pdf", "audio", "video")):
            self.app.preview.show_hit(hit, autoplay=hit.kind in ("audio", "video"))


# -- preview (Space): the picture, the PDF page, the passage, or the recording at its moment


class Preview(QWidget):
    def __init__(self, app: "LazyFindsApp"):
        super().__init__(None, Qt.WindowType.Tool | Qt.WindowType.WindowStaysOnTopHint)
        self.app = app
        self.hit: Hit | None = None
        self.player = None
        self.setWindowTitle(f"{APP_NAME} preview")
        self.resize(900, 640)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        self.title = QLabel()
        self.title.setStyleSheet("font-size: 14px; font-weight: 600;")
        layout.addWidget(self.title)
        self.picture = QLabel()
        self.picture.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.picture.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        layout.addWidget(self.picture, 1)
        self.text = QTextBrowser()
        layout.addWidget(self.text, 1)
        self.video_box = QWidget()
        vb = QVBoxLayout(self.video_box)
        vb.setContentsMargins(0, 0, 0, 0)
        self.video_slot = QVBoxLayout()
        vb.addLayout(self.video_slot, 1)
        controls = QHBoxLayout()
        self.play_button = QPushButton("Pause")
        self.play_button.clicked.connect(self._toggle_play)
        self.back_button = QPushButton("−10 s")
        self.back_button.clicked.connect(lambda: self._jump(-10_000))
        self.fwd_button = QPushButton("+10 s")
        self.fwd_button.clicked.connect(lambda: self._jump(10_000))
        self.seek = QSlider(Qt.Orientation.Horizontal)
        self.seek.sliderMoved.connect(lambda v: self.player and self.player.setPosition(v))
        self.time = QLabel("0:00 / 0:00")
        for w in (self.back_button, self.play_button, self.fwd_button):
            controls.addWidget(w)
        controls.addWidget(self.seek, 1)
        controls.addWidget(self.time)
        vb.addLayout(controls)
        layout.addWidget(self.video_box, 1)
        buttons = QHBoxLayout()
        self.page_label = QLabel()
        buttons.addWidget(self.page_label)
        buttons.addStretch(1)
        self.open_button = QPushButton("Open")
        self.open_button.clicked.connect(self._open)
        reveal = QPushButton("Show in folder")
        reveal.clicked.connect(lambda: self.hit and opener.reveal(self.hit.path))
        buttons.addWidget(reveal)
        buttons.addWidget(self.open_button)
        layout.addLayout(buttons)
        self.page_image: QImage | None = None

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key.Key_Escape, Qt.Key.Key_Space) and not (self.player and e.key() == Qt.Key.Key_Space):
            self.close()
        elif e.key() == Qt.Key.Key_Space and self.player:
            self._toggle_play()
        else:
            super().keyPressEvent(e)

    def closeEvent(self, e):
        self._stop_player()
        super().closeEvent(e)
        if self.app.panel.isVisible():
            self.app.panel.activateWindow()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._fit_picture()

    def _fit_picture(self):
        if self.page_image is not None and self.picture.isVisible():
            self.picture.setPixmap(QPixmap.fromImage(self.page_image).scaled(
                self.picture.size(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))

    def show_hit(self, hit: Hit, autoplay: bool = False):
        self._stop_player()
        self.hit = hit
        self.page_image = None
        self.title.setText(html.escape(Path(hit.path).name) + ("  ·  " + place_of(hit) if place_of(hit) else ""))
        self.picture.hide()
        self.text.hide()
        self.video_box.hide()
        self.page_label.setText("")
        app = C.editor_name() if hit.kind == "code" else opener.default_app_name(Path(hit.path).suffix.lower())
        self.open_button.setText(f"Open in {app}" if app and app.lower() != "openwith" else "Open")
        if hit.kind in ("image", "screenshot", "pdf"):
            image = extract.thumbnail(Path(hit.path), hit.kind, hit.start, size=700)
            if image is not None:
                self.page_image = pil_to_qimage(image)
                self.picture.show()
            if hit.kind == "pdf" and hit.start:
                self.page_label.setText(f"Page {int(hit.start)} of {hit.meta.get('pages', '?')}")
        elif hit.kind in ("doc", "code"):
            body = self.app.query.body(hit.piece, code=hit.kind == "code") or hit.line or ""
            typed = self.app.panel.query.text()
            typed = C.query_of(typed) if C.query_of(typed) is not None else typed
            body, marks = T.word_spans(body, T.query_terms(typed))
            parts, cursor = [], 0
            for start, end in marks:
                parts.append(html.escape(body[cursor:start]))
                parts.append(f"<a name='m'></a><b style='background:#ffe58a;color:black'>"
                             f"{html.escape(body[start:end])}</b>")
                cursor = end
            parts.append(html.escape(body[cursor:]))
            font = "font-family:Consolas,monospace;font-size:13px" if hit.kind == "code" else "font-size:14px"
            self.text.setHtml(f"<div style='{font}; white-space:pre-wrap'>{''.join(parts)}</div>")
            self.text.show()
            if marks:
                QTimer.singleShot(0, lambda: self.text.scrollToAnchor("m"))
        elif hit.kind in ("audio", "video"):
            self._start_player(hit, autoplay)
        self.show()
        self.raise_()
        self.activateWindow()
        QTimer.singleShot(0, self._fit_picture)

    # the player

    def _start_player(self, hit: Hit, autoplay: bool):
        from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
        from PySide6.QtMultimediaWidgets import QVideoWidget

        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.player.setAudioOutput(self.audio)
        if hit.kind == "video":
            self.video = QVideoWidget()
            self.video_slot.addWidget(self.video)
            self.player.setVideoOutput(self.video)
        else:
            self.video = QLabel("🎵")
            self.video.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.video.setStyleSheet("font-size: 64px;")
            self.video_slot.addWidget(self.video)
        self.player.durationChanged.connect(lambda d: self.seek.setRange(0, int(d)))
        self.player.positionChanged.connect(self._position)
        start = int((hit.start or 0) * 1000)

        def ready(status):
            if status == QMediaPlayer.MediaStatus.LoadedMedia and self.player:
                self.player.setPosition(start)
                if autoplay:
                    self.player.play()

        self.player.mediaStatusChanged.connect(ready)
        self.player.setSource(QUrl.fromLocalFile(hit.path))
        self.play_button.setText("Pause" if autoplay else "Play")
        self.video_box.show()

    def _stop_player(self):
        if self.player:
            self.player.stop()
            self.player.deleteLater()
            self.player = None
            while self.video_slot.count():
                w = self.video_slot.takeAt(0).widget()
                if w:
                    w.deleteLater()

    def _position(self, ms: int):
        if not self.seek.isSliderDown():
            self.seek.setValue(ms)
        total = self.player.duration() if self.player else 0
        self.time.setText(f"{fmt_time(ms / 1000)} / {fmt_time(total / 1000)}")

    def _toggle_play(self):
        from PySide6.QtMultimedia import QMediaPlayer

        if not self.player:
            return
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
            self.play_button.setText("Play")
        else:
            self.player.play()
            self.play_button.setText("Pause")

    def _jump(self, delta_ms: int):
        if self.player:
            self.player.setPosition(max(0, self.player.position() + delta_ms))

    def _open(self):
        if not self.hit:
            return
        position = self.player.position() / 1000 if self.player else self.hit.start
        self._stop_player()
        self.close()
        if self.hit.kind == "pdf":
            opener.open_pdf(self.hit.path, int(self.hit.start) if self.hit.start else None)
        elif self.hit.kind == "code":
            C.open_at(self.hit.path, self.hit.start)
        elif self.hit.kind in ("audio", "video"):
            if not opener.open_media_at(self.hit.path, position):
                opener.open_plain(self.hit.path)
        else:
            opener.open_plain(self.hit.path)


# -- settings


class SettingsWindow(QWidget):
    tools_changed = Signal()

    def __init__(self, app: "LazyFindsApp"):
        super().__init__(None)
        self.app = app
        self.setWindowTitle(f"{APP_NAME} settings")
        self.setWindowIcon(make_icon())
        self.resize(640, 560)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("<b>Folders to search</b> — pictures, PDFs, documents, audio and video in them "
                                "(and their subfolders)."))
        self.folders = QListWidget()
        layout.addWidget(self.folders, 1)
        row = QHBoxLayout()
        add = QPushButton("Add folder…")
        add.clicked.connect(self._add)
        remove = QPushButton("Remove")
        remove.clicked.connect(lambda: self._remove(self.folders, "folders"))
        row.addWidget(add)
        row.addWidget(remove)
        row.addStretch(1)
        layout.addLayout(row)
        layout.addWidget(QLabel("<b>Never read</b> — folders to leave out."))
        self.excluded = QListWidget()
        self.excluded.setMaximumHeight(90)
        layout.addWidget(self.excluded)
        row = QHBoxLayout()
        add_ex = QPushButton("Leave out a folder…")
        add_ex.clicked.connect(self._exclude)
        remove_ex = QPushButton("Remove")
        remove_ex.clicked.connect(lambda: self._remove(self.excluded, "excluded"))
        row.addWidget(add_ex)
        row.addWidget(remove_ex)
        row.addStretch(1)
        layout.addLayout(row)
        layout.addWidget(QLabel("<b>Code folders</b> — searched only when the query starts with <code>code:</code>, "
                                "and opened in VS Code at the line."))
        self.code_folders = QListWidget()
        self.code_folders.setMaximumHeight(90)
        layout.addWidget(self.code_folders)
        row = QHBoxLayout()
        add_code = QPushButton("Add code folder…")
        add_code.clicked.connect(self._add_code)
        remove_code = QPushButton("Remove")
        remove_code.clicked.connect(lambda: self._remove(self.code_folders, "code_folders"))
        row.addWidget(add_code)
        row.addWidget(remove_code)
        row.addStretch(1)
        layout.addLayout(row)
        row = QHBoxLayout()
        row.addWidget(QLabel("Run the model on:"))
        self.backend = QComboBox()
        for value, label in (("auto", "Automatic"), ("cuda", "NVIDIA GPU (CUDA)"), ("vulkan", "Any GPU (Vulkan)"),
                             ("cpu", "Processor only (slow)")):
            self.backend.addItem(label, value)
        self.backend.currentIndexChanged.connect(self._backend)
        row.addWidget(self.backend)
        self.backend_state = QLabel()
        row.addWidget(self.backend_state)
        row.addStretch(1)
        layout.addLayout(row)
        row = QHBoxLayout()
        self.bengali = QCheckBox("Read Bengali text in pictures and scans (Tesseract)")
        self.bengali.toggled.connect(lambda on: self._save(lambda s: setattr(s, "bengali_ocr", on)))
        row.addWidget(self.bengali)
        self.bengali_setup = QPushButton("Set up…")
        self.bengali_setup.clicked.connect(self._setup_bengali)
        row.addWidget(self.bengali_setup)
        self.bengali_state = QLabel()
        row.addWidget(self.bengali_state)
        row.addStretch(1)
        layout.addLayout(row)
        row = QHBoxLayout()
        row.addWidget(QLabel("Audio and video:"))
        self.ffmpeg_state = QLabel()
        row.addWidget(self.ffmpeg_state)
        self.ffmpeg_setup = QPushButton("Install ffmpeg…")
        self.ffmpeg_setup.clicked.connect(self._setup_ffmpeg)
        row.addWidget(self.ffmpeg_setup)
        row.addStretch(1)
        layout.addLayout(row)
        self.tools_changed.connect(self._show_tools)
        self._waiting: dict[str, tuple[float, object, object]] = {}
        self._poll = QTimer(self, interval=3000)
        self._poll.timeout.connect(self._check_installs)
        row = QHBoxLayout()
        row.addWidget(QLabel("Shortcut:"))
        self.hotkey = QLineEdit()
        self.hotkey.setMaximumWidth(200)
        self.hotkey.editingFinished.connect(self._hotkey)
        row.addWidget(self.hotkey)
        self.hotkey_state = QLabel()
        row.addWidget(self.hotkey_state)
        row.addStretch(1)
        layout.addLayout(row)
        self.watch = QCheckBox("Read new and changed files as they appear")
        self.watch.toggled.connect(self._watch)
        layout.addWidget(self.watch)
        self.name_search = QCheckBox("Keep every file and folder name on this PC for Name search (names only, "
                                     "never what's inside)")
        self.name_search.toggled.connect(lambda on: self._save(lambda s: setattr(s, "name_search", on)))
        layout.addWidget(self.name_search)
        self.banglish = QCheckBox("Search Bengali typed in English letters in Bengali too (\"atoshbaji\" finds "
                                  "আতশবাজি)")
        self.banglish.toggled.connect(lambda on: self._save(lambda s: setattr(s, "banglish", on)))
        layout.addWidget(self.banglish)
        self.startup = QCheckBox("Start lazyFinds when I sign in to Windows")
        self.startup.toggled.connect(self._startup)
        layout.addWidget(self.startup)
        self.stats = QLabel()
        self.stats.setWordWrap(True)
        layout.addWidget(self.stats)
        row = QHBoxLayout()
        now = QPushButton("Look for new files now")
        now.clicked.connect(lambda: self.app.indexing.request())
        row.addWidget(now)
        row.addStretch(1)
        layout.addLayout(row)

    def showEvent(self, e):
        s = Settings.load()
        self.folders.clear()
        self.folders.addItems(s.folders)
        self.excluded.clear()
        self.excluded.addItems(s.excluded)
        self.code_folders.clear()
        self.code_folders.addItems(s.code_folders)
        self.backend.blockSignals(True)
        self.backend.setCurrentIndex(max(0, self.backend.findData(s.backend)))
        self.backend.blockSignals(False)
        self._show_backend()
        self.bengali.blockSignals(True)
        self.bengali.setChecked(s.bengali_ocr)
        self.bengali.blockSignals(False)
        self._show_tools()
        self.hotkey.setText(s.hotkey)
        self.hotkey_state.setText("✓ active" if self.app.panel.hotkey_ok else "✗ taken by another app")
        self.watch.blockSignals(True)
        self.watch.setChecked(s.watch)
        self.watch.blockSignals(False)
        self.name_search.blockSignals(True)
        self.name_search.setChecked(s.name_search)
        self.name_search.blockSignals(False)
        self.banglish.blockSignals(True)
        self.banglish.setChecked(s.banglish)
        self.banglish.blockSignals(False)
        self.startup.blockSignals(True)
        self.startup.setChecked(startup_enabled())
        self.startup.blockSignals(False)
        self.refresh_stats()
        super().showEvent(e)

    def refresh_stats(self):
        try:
            from .store import Store

            store = Store(index_path())
            c = store.counts()
            store.close()
            names = {"screenshot": ("screenshot", "screenshots"), "image": ("picture", "pictures"),
                     "pdf": ("PDF", "PDFs"), "doc": ("document", "documents"), "audio": ("recording", "recordings"),
                     "video": ("video", "videos")}
            files = ", ".join(f"{n} {names.get(k, (k, k))[n != 1]}" for k, n in sorted(c["kinds"].items()))
            status = c["status"]
            text = (f"In the index: {files or 'nothing yet'}. {status.get('read', 0)} read, "
                    f"{status.get('new', 0)} waiting, {status.get('skipped', 0)} skipped, "
                    f"{status.get('failed', 0)} unreadable. {c['vectors']} vectors.")
            if code_index_path().exists():
                code_store = Store(code_index_path())
                text += f" Code: {code_store.counts()['kinds'].get('code', 0)} files."
                code_store.close()
            self.stats.setText(text)
        except Exception as error:
            self.stats.setText(f"Index: {error}")

    def _add_code(self):
        folder = QFileDialog.getExistingDirectory(self, "Add a folder of code")
        if folder:
            folder = os.path.normpath(folder)
            self._save(lambda s: s.code_folders.append(folder) if folder not in s.code_folders else None)
            self.code_folders.addItem(folder)
            self.app.folders_changed()

    def _backend(self):
        value = self.backend.currentData()
        self._save(lambda s: setattr(s, "backend", value))
        self.app.query.close()   # the next search starts the model on the new choice
        self._show_backend()

    def _show_backend(self):
        from . import engine as E

        chosen = E.choose_backend(self.backend.currentData())
        names = {"cuda": "CUDA", "vulkan": "Vulkan", "cpu": "the processor"}
        missing = self.backend.currentData() in ("cuda", "vulkan") and not paths.backend_ready(
            self.backend.currentData())
        self.backend_state.setText(f"runs on {names[chosen]}" + (" (that one isn't downloaded)" if missing else ""))

    # -- helper programs (ffmpeg, Tesseract): installed with winget, noticed without a restart

    def _show_tools(self):
        self._show_bengali()
        installing = paths.FFMPEG_WINGET_ID in self._waiting
        ready = paths.ffmpeg_ready()
        self.ffmpeg_setup.setVisible(not ready and not installing)
        self.ffmpeg_state.setText("✓ ffmpeg found" if ready else "installing ffmpeg…" if installing else
                                  "need ffmpeg (free) to be read")

    def _show_bengali(self):
        ready = paths.bengali_ocr_ready()
        has_tesseract = paths.tesseract_exe() is not None
        installing = paths.TESSERACT_WINGET_ID in self._waiting
        self.bengali_setup.setVisible(not ready and not installing)
        self.bengali_state.setText("✓ ready" if ready else "installing Tesseract…" if installing else
                                   "needs its language files" if has_tesseract else "needs Tesseract")

    def _winget(self, package: str, title: str, ready, then) -> None:
        """Opens a window that installs `package` with winget, then checks every few seconds (for 15 minutes) whether
        it's there; once it is, runs `then`."""
        import shutil
        import subprocess

        if shutil.which("winget") is None:
            QMessageBox.information(self, APP_NAME, f"winget isn't available on this PC. Install {title} yourself, "
                                    f"or get winget (App Installer) from the Microsoft Store.")
            return
        subprocess.Popen(["cmd", "/c", "start", f"Installing {title}", "winget", "install", "--id", package, "-e",
                          "--accept-package-agreements", "--accept-source-agreements"])
        self._waiting[package] = (time.monotonic() + 900, ready, then)
        self._poll.start()
        self._show_tools()

    def _check_installs(self):
        for package, (deadline, ready, then) in list(self._waiting.items()):
            if ready():
                del self._waiting[package]
                then()
            elif time.monotonic() > deadline:
                del self._waiting[package]
        if not self._waiting:
            self._poll.stop()
        self._show_tools()

    def _setup_ffmpeg(self):
        answer = QMessageBox.question(
            self, APP_NAME, "Audio and video are read with ffmpeg (free; GPL license; about 100 MB, the gyan.dev "
            "build through winget). Install it now? A window shows the install.")
        if answer == QMessageBox.StandardButton.Yes:
            # Once it's there, the next pass reads the sound and video files that waited for it.
            self._winget(paths.FFMPEG_WINGET_ID, "ffmpeg", paths.ffmpeg_ready, lambda: self.app.indexing.request())

    def _setup_bengali(self):
        if paths.tesseract_exe() is None:
            answer = QMessageBox.question(
                self, APP_NAME, "Bengali text in pictures needs Tesseract OCR (free, Apache 2.0, about 50 MB, by UB "
                "Mannheim through winget), and then its Bengali and English models (26 MB). Install them now? A "
                "window shows the install; Windows may ask for permission.")
            if answer == QMessageBox.StandardButton.Yes:
                self._winget(paths.TESSERACT_WINGET_ID, "Tesseract OCR",
                             lambda: paths.tesseract_exe() is not None, self._get_ocr_models)
            return
        self._get_ocr_models()

    def _get_ocr_models(self):
        from . import setup

        if paths.bengali_ocr_ready():
            self._show_tools()
            return
        self.bengali_state.setText("downloading the Bengali and English models (26 MB)…")

        def work():
            try:
                setup.run(only=paths.OCR_FILES)
            except Exception as error:
                log.warning("OCR models: %s", error)
            finally:
                self.tools_changed.emit()   # a signal: safe to send from this thread

        threading.Thread(target=work, daemon=True).start()

    def _save(self, change):
        s = Settings.load()
        change(s)
        s.save()

    def _add(self):
        folder = QFileDialog.getExistingDirectory(self, "Add a folder to search")
        if folder:
            folder = os.path.normpath(folder)
            self._save(lambda s: s.folders.append(folder) if folder not in s.folders else None)
            self.folders.addItem(folder)
            self.app.folders_changed()

    def _exclude(self):
        folder = QFileDialog.getExistingDirectory(self, "Leave a folder out")
        if folder:
            folder = os.path.normpath(folder)
            self._save(lambda s: s.excluded.append(folder) if folder not in s.excluded else None)
            self.excluded.addItem(folder)
            self.app.folders_changed()

    def _remove(self, widget: QListWidget, field: str):
        item = widget.currentItem()
        if not item:
            return
        value = item.text()
        self._save(lambda s: setattr(s, field, [f for f in getattr(s, field) if f != value]))
        widget.takeItem(widget.row(item))
        self.app.folders_changed()

    def _hotkey(self):
        text = self.hotkey.text().strip()
        if not parse_hotkey(text):
            self.hotkey_state.setText("✗ use e.g. Ctrl+Shift+Space or Alt+F")
            return
        ok = self.app.panel.register_hotkey(text)
        self.hotkey_state.setText("✓ active" if ok else "✗ taken by another app")
        if ok:
            self._save(lambda s: setattr(s, "hotkey", text))

    def _watch(self, on: bool):
        self._save(lambda s: setattr(s, "watch", on))
        self.app.indexing.rewatch()

    def _startup(self, on: bool):
        set_startup(on)


# -- start with Windows (HKCU Run, only when the user ticks it)

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def launch_command() -> str:
    exe = Path(sys.argv[0])
    if exe.suffix.lower() == ".exe" and exe.exists():
        return f'"{exe}"'
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    return f'"{pythonw if pythonw.exists() else sys.executable}" -m lazyfinds.app'


def startup_enabled() -> bool:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, APP_NAME)
            return True
    except OSError:
        return False


def set_startup(on: bool) -> None:
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if on:
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, launch_command())
        else:
            try:
                winreg.DeleteValue(key, APP_NAME)
            except OSError:
                pass


# -- first run: the model download


class SetupDialog(QDialog):
    def __init__(self, bridge: Bridge):
        super().__init__()
        from . import setup

        self.setWindowTitle(f"{APP_NAME} setup")
        self.setWindowIcon(make_icon())
        todo = setup.missing()
        total = sum(i.size for i in todo)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            f"<b>lazyFinds needs its model once</b><br>EmbeddingGemma 2 (Google DeepMind, Apache 2.0) and llama.cpp "
            f"({'CUDA' if paths.has_nvidia() else 'Vulkan'}): {total / 1e9:.2f} GB from Hugging Face and GitHub."
            f"<br>After this, nothing you index leaves this PC."))
        self.label = QLabel("")
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        layout.addWidget(self.label)
        layout.addWidget(self.bar)
        row = QHBoxLayout()
        row.addStretch(1)
        self.start = QPushButton("Download")
        self.start.clicked.connect(self._start)
        cancel = QPushButton("Not now")
        cancel.clicked.connect(self.reject)
        row.addWidget(cancel)
        row.addWidget(self.start)
        layout.addLayout(row)
        self.bridge = bridge
        bridge.setup_progress.connect(self._progress)
        bridge.setup_done.connect(self._done)

    def _start(self):
        from . import setup

        self.start.setEnabled(False)

        def work():
            try:
                setup.run(lambda n, a, b: self.bridge.setup_progress.emit(n, a, b))
                self.bridge.setup_done.emit(None)
            except Exception as error:
                self.bridge.setup_done.emit(error)

        threading.Thread(target=work, daemon=True).start()

    def _progress(self, name, have, size):
        self.label.setText(f"{name}: {have / 1e6:.0f} of {size / 1e6:.0f} MB")
        self.bar.setValue(int(1000 * have / max(size, 1)))

    def _done(self, error):
        if error:
            self.label.setText(f"Download failed: {error}")
            self.start.setEnabled(True)
        else:
            self.accept()


# -- the app


class LazyFindsApp(QObject):
    def __init__(self, qt: QApplication):
        super().__init__()
        self.qt = qt
        self.bridge = Bridge()
        self.index_status: dict = {"state": "idle", "done": 0, "total": 0}
        self.thumb_pool = ThreadPoolExecutor(3, thread_name_prefix="thumb")
        self.query = QueryService(lambda *a: self.bridge.results.emit(*a))
        self.indexing = IndexService(lambda s: self.bridge.status.emit(s))
        from .names import NameService

        self.name_status = ""
        self.names = NameService(lambda text: self.bridge.name_status.emit(text))
        self.name_pool = ThreadPoolExecutor(1, thread_name_prefix="names")
        self.panel = SearchPanel(self)
        self.preview = Preview(self)
        self.settings_window: SettingsWindow | None = None
        self.bridge.results.connect(self.panel.show_results)
        self.bridge.status.connect(self._status)
        self.bridge.thumb.connect(self.panel.set_thumb)
        self.bridge.name_status.connect(self._name_status)
        self.tray = QSystemTrayIcon(make_icon(), self)
        self.tray.setToolTip(APP_NAME)
        menu = QMenu()
        self.search_action = QAction("Search…", menu)
        self.search_action.triggered.connect(self.panel.summon)
        self.status_action = QAction("Idle", menu)
        self.status_action.setEnabled(False)
        self.pause_action = QAction("Pause indexing", menu)
        self.pause_action.triggered.connect(self._toggle_pause)
        settings_action = QAction("Settings…", menu)
        settings_action.triggered.connect(self.show_settings)
        quit_action = QAction("Quit lazyFinds", menu)
        quit_action.triggered.connect(self.quit)
        for a in (self.search_action, self.status_action, self.pause_action, settings_action):
            menu.addAction(a)
        menu.addSeparator()
        menu.addAction(quit_action)
        self.menu = menu
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(lambda reason: self.panel.summon()
                                    if reason == QSystemTrayIcon.ActivationReason.Trigger else None)

    def start(self):
        self.tray.show()
        s = Settings.load()
        if not self.panel.register_hotkey(s.hotkey):
            self.tray.showMessage(APP_NAME, f"{s.hotkey} is taken by another app. Pick another in Settings.",
                                  QSystemTrayIcon.MessageIcon.Warning)
        self.search_action.setText(f"Search…\t{s.hotkey}")
        if not paths.models_ready():
            if SetupDialog(self.bridge).exec() != QDialog.DialogCode.Accepted:
                self.tray.showMessage(APP_NAME, "Search needs the model. Open Settings to download it later.")
        self.query.start()
        self.indexing.start()
        self.names.start()
        if not s.folders:
            self.show_settings()
        elif not s.first_run_done:
            self.tray.showMessage(APP_NAME, f"Reading your folders. Press {s.hotkey} to search any time.")
        if not s.first_run_done:
            s.first_run_done = True
            s.save()

    def _status(self, status: dict):
        self.index_status = status
        state = status.get("state")
        if state == "indexing":
            text = f"Reading files: {status['done']} of {status['total']}"
        elif state == "scanning":
            text = "Looking for new files…"
        elif state == "paused":
            text = "Indexing paused"
        elif state == "error":
            text = "Problem: " + (status.get("error") or "")[:60]
        else:
            text = "Up to date"
        self.status_action.setText(text)
        self.tray.setToolTip(f"{APP_NAME} — {text}")
        if self.panel.isVisible():
            self.panel.update_footer()
        if self.settings_window and self.settings_window.isVisible() and state in ("idle", "paused"):
            self.settings_window.refresh_stats()

    def _name_status(self, text: str):
        self.name_status = text
        if self.panel.isVisible() and self.panel.mode == "name":
            self.panel.update_footer()

    def find_names(self, seq: int, text: str, kinds, banglish: str = "on"):
        """Name search off the UI thread; the answer comes back as ordinary results. A Banglish query also finds names
        written in Bengali ("kemon" → কেমন আছো.docx)."""
        def work():
            if seq != self.panel.seq:   # a newer query was typed meanwhile
                return
            if not text.strip():
                self.bridge.results.emit(seq, text, [], True, "")
                return
            conversion = B.convert(text) if banglish != "off" else None
            also = B.name_queries(text, conversion) if conversion and banglish == "on" else ()
            found = self.names.names.search(text, folders_only=kinds == "folders",
                                            extensions=kinds if isinstance(kinds, (set, frozenset)) else None,
                                            also=also)
            self.bridge.results.emit(seq, text, [name_hit(f) for f in found], True,
                                     conversion.shown if conversion else "")

        self.name_pool.submit(work)

    def _toggle_pause(self):
        paused = not self.indexing.paused
        self.indexing.pause(paused)
        self.pause_action.setText("Resume indexing" if paused else "Pause indexing")

    def folders_changed(self):
        self.indexing.rewatch()
        self.indexing.request(1.0)

    def show_settings(self):
        if self.settings_window is None:
            self.settings_window = SettingsWindow(self)
        self.settings_window.show()
        self.settings_window.raise_()
        self.settings_window.activateWindow()

    def load_thumb(self, key: str, hit: Hit):
        def work():
            image = extract.thumbnail(Path(hit.path), hit.kind, hit.start, size=96)
            if image is not None:
                self.bridge.thumb.emit(key, pil_to_qimage(image))

        self.thumb_pool.submit(work)

    def quit(self):
        self.indexing.stop()
        self.names.stop()
        self.query.close()
        self.tray.hide()
        self.qt.quit()


def setup_logging():
    paths.log_dir().mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(paths.log_dir() / "app.log", maxBytes=2_000_000, backupCount=2,
                                                   encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(threadName)s: %(message)s"))
    root = logging.getLogger("lazyfinds")
    root.setLevel(logging.INFO)
    root.addHandler(handler)


def main() -> int:
    setup_logging()
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("lazyFinds.app")
    qt = QApplication(sys.argv)
    qt.setQuitOnLastWindowClosed(False)
    qt.setApplicationName(APP_NAME)
    qt.setWindowIcon(make_icon())
    # One lazyFinds at a time: a second start shows the first one's panel.
    name = f"lazyFinds-{os.environ.get('USERNAME', 'user')}"
    if os.environ.get("LAZYFINDS_HOME"):   # a separate test setup runs beside the real one
        import hashlib

        name += "-" + hashlib.sha1(os.environ["LAZYFINDS_HOME"].lower().encode()).hexdigest()[:8]
    probe = QLocalSocket()
    probe.connectToServer(name)
    if probe.waitForConnected(300):
        probe.write(b"show")
        probe.flush()
        probe.waitForBytesWritten(300)
        return 0
    QLocalServer.removeServer(name)
    server = QLocalServer()
    server.listen(name)
    app = LazyFindsApp(qt)
    server.newConnection.connect(lambda: (server.nextPendingConnection(), app.panel.summon()))
    app.start()
    if "--show" in sys.argv:
        QTimer.singleShot(300, app.panel.summon)
    log.info("started")
    return qt.exec()


if __name__ == "__main__":
    raise SystemExit(main())
