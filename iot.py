"""
I.O.T  —  Image Organization Toolkit

Sorts large photo collections: browse a folder tree (the "hub") as a thumbnail grid, then split, rename,
copy, resize, de-duplicate, tag and delete with clicks or single keys. Every change can be undone until
I.O.T closes. Styled like V.O.T: a frameless window on a gradient, with see-through rounded panels.

Python packages:  pip install PySide6 pillow
Start folder: a folder passed on the command line, else the folder it was launched from (OneCommander),
else this script's folder. Drop a folder onto the window to open it.

The older Tkinter version (needs only Pillow) is kept as scripts/iot_tk.py.
"""
import os
import re
import sys
import json
import math
import time
import heapq
import queue
import shutil
import threading
import traceback
import subprocess
from collections import OrderedDict
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
# Our own modules live in the 'scripts' folder next to this file (keeps this folder down to a few items)
SCRIPTS_DIR = os.path.join(SCRIPT_DIR, "scripts")
sys.path.insert(0, SCRIPTS_DIR)


def _pause():
    try:
        if sys.stdin is not None and sys.stdin.isatty():
            input("\nPress ENTER to close...")
    except (EOFError, KeyboardInterrupt, RuntimeError):
        pass


def _require(module, pip_name):
    """A missing package is explained (and how to install it) instead of a bare traceback."""
    try:
        return __import__(module)
    except ImportError as err:
        text = (f"I.O.T needs the '{pip_name}' package, which isn't installed for this Python:\n{sys.executable}\n\n"
                f"Install it with:\n    \"{sys.executable}\" -m pip install {pip_name}\n\n({err})\n\n"
                "(The older Tkinter version needs only Pillow:  python scripts/iot_tk.py)")
        print(text, file=sys.stderr)
        if sys.platform.startswith("win"):
            try:
                import ctypes
                ctypes.windll.user32.MessageBoxW(None, text, "I.O.T couldn't start", 0x10)
            except Exception:
                pass
        _pause()
        sys.exit(1)


_require("PySide6", "PySide6")
_require("PIL", "pillow")

from PySide6.QtCore import QEvent, QPoint, QPointF, QRectF, Qt, QTimer  # noqa: E402
from PySide6.QtGui import (QColor, QCursor, QFont, QFontMetrics, QGuiApplication, QKeySequence,  # noqa: E402
                           QPainter, QPainterPath, QPen, QPixmap)
from PySide6.QtWidgets import (QApplication, QCheckBox, QFrame, QHBoxLayout, QLabel, QMainWindow,  # noqa: E402
                               QMenu, QScrollArea, QScrollBar, QSlider, QVBoxLayout, QWidget)

import iot_utils  # noqa: E402
import iot_history  # noqa: E402
import iot_platform  # noqa: E402
import iot_qt  # noqa: E402
import iot_qt_dialogs as dialogs  # noqa: E402
from iot_qt import (P, FRAMELESS, FlowLayout, FolderBar, GroupButton, Var, WindowControls, WindowRoot,  # noqa: E402
                    MessageDialog, bind_check, button, card, caps, dot_pixmap, label, later, pil_to_pixmap,
                    set_palette, set_round_corners, transparent_box)

iot_history.hide_path(os.path.join(SCRIPTS_DIR, "__pycache__"))  # Python's compiled cache: hidden, still used


def _resolve_config_dir():
    """Config and log live next to the script; if that folder is read-only, use the platform's usual place
    (%APPDATA%\\IOT on Windows, ~/Library/Application Support/IOT on macOS, ~/.config/IOT on Linux)."""
    try:
        probe = os.path.join(SCRIPT_DIR, ".iot_write_test")
        with open(probe, "w"):
            pass
        os.remove(probe)
        return SCRIPT_DIR
    except OSError:
        path = iot_platform.fallback_config_dir()
        os.makedirs(path, exist_ok=True)
        return path


CONFIG_DIR = _resolve_config_dir()
SETTINGS_FILE = os.path.join(CONFIG_DIR, "iot_settings.json")
CHANGE_LOG_FILE = os.path.join(CONFIG_DIR, "iot_change_log.txt")
CRASH_LOG_FILE = os.path.join(CONFIG_DIR, "iot_crash.log")
APP_NAME = "I.O.T"
APP_SUBTITLE = "Image Organization Toolkit"


def log_crash(kind, exc_type, exc, tb):
    """Writes an error to iot_crash.log with the system details, so problems on a Mac or Linux machine
    (where there's often no console to see them) can be sent back. Still printed as before, too."""
    traceback.print_exception(exc_type, exc, tb)
    try:
        import platform
        import PIL
        import PySide6
        from PySide6.QtCore import qVersion
        if os.path.exists(CRASH_LOG_FILE) and os.path.getsize(CRASH_LOG_FILE) > 1_000_000:
            os.replace(CRASH_LOG_FILE, CRASH_LOG_FILE + ".old")
        with open(CRASH_LOG_FILE, "a", encoding="utf-8") as handle:
            handle.write(f"{datetime.now():%Y-%m-%d %H:%M:%S}  {kind}  |  {platform.platform()}  |  "
                         f"Python {platform.python_version()}  |  Qt {qVersion()} (PySide6 {PySide6.__version__})  |  "
                         f"Pillow {PIL.__version__}\n")
            handle.write("".join(traceback.format_exception(exc_type, exc, tb)) + "\n")
    except Exception:
        pass


TILE_OVERHEAD = 40              # horizontal pixels a tile needs beyond its thumbnail (padding, border, gaps)
TILE_GAP = 14                   # vertical gap between rows of tiles
PREFETCH_SCREENS = 1            # screens of thumbnails decoded ahead (above and below) so scrolling is instant
THUMB_CACHE_BYTES = 160_000_000 # RAM budget for decoded thumbnails; least recently used are dropped first
INVALID_NAME_CHARS = set('<>:"/\\|?*')
WINDOWS_RESERVED_NAMES = {"con", "prn", "aux", "nul"} | {f"{p}{n}" for p in ("com", "lpt") for n in range(1, 10)}
THUMB_RETRY_SECONDS = 3         # a thumbnail that couldn't be read (e.g. drive spinning up) is tried again after this
THUMB_RETRY_LIMIT = 5           # ...this many times, then it's treated as a broken file
TAG_COLORS = ("red", "orange", "yellow", "green", "blue", "purple", "grey")  # folder tags; "tag_<name>" in the palette
TAGS_FILE_NAME = ".iot_tags.json"   # hidden file in the hub: {"tags": {"relative/folder": "green"}}
HEAL_INTERVAL_MS = 3000         # how often on-screen tiles still waiting for their picture are asked for again
UNDO_KEYS = iot_platform.keys("Ctrl+Z")   # Cmd+Z on macOS
FLAG_TARGET, FLAG_SIZE, FLAG_MODIFIED = iot_platform.FLAG_TARGET, iot_platform.FLAG_SIZE, iot_platform.FLAG_MODIFIED


def folder_name_problem(name):
    """Why `name` can't be used as a folder (or file-name prefix), or None when it's fine."""
    if any(char in INVALID_NAME_CHARS for char in name):
        return 'Names can\'t contain any of these characters:  < > : " / \\ | ? *'
    if set(name) <= {"."}:
        return "A name can't be only dots ('.' and '..' mean this folder and the folder above)."
    if name.endswith("."):
        return "Windows doesn't allow names that end with a dot."
    if name.split(".")[0].strip().lower() in WINDOWS_RESERVED_NAMES:
        return f"'{name}' is reserved by Windows and can't be used as a name."
    return None


def is_temporary_read_error(err):
    """True when the file couldn't be *read* (drive asleep or busy, network hiccup), so trying again
    later may work. Errors from the operating system carry an error code; a file that reads fine but
    won't decode (a broken image) doesn't, and isn't retried."""
    if isinstance(err, (FileNotFoundError, IsADirectoryError)):
        return False  # gone: the next folder refresh drops its tile
    return isinstance(err, OSError) and (err.errno is not None or getattr(err, "winerror", None) is not None)


class ThumbnailLoader:
    """
    Background thumbnail decoder with a priority queue: the most recently requested images
    (what's on screen now) are decoded first, and anything scrolled far out of view is skipped.
    """

    def __init__(self, result_queue, worker_count, disk_cache=None):
        self.out = result_queue
        self.disk_cache = disk_cache
        self.generation = 0
        self.wanted = frozenset()   # file names currently on or near the screen
        self._cond = threading.Condition()
        self._heap = []
        self._pending = {}
        self._in_flight = set()     # keys a worker is decoding right now
        self._seq = 0
        self._stop = False
        self._threads = []
        for number in range(worker_count):
            thread = threading.Thread(target=self._run, daemon=True, name=f"iot-thumb-{number}")
            thread.start()
            self._threads.append(thread)

    def request(self, keys):
        """keys: (folder, name, size) tuples in increasing priority order (last = decoded first)."""
        if not keys:
            return
        with self._cond:
            for key in keys:
                self._seq += 1
                self._pending[key] = self._seq
                heapq.heappush(self._heap, (-self._seq, key))
            if len(self._heap) > 4 * len(self._pending) + 5000:  # drop superseded entries now and then
                self._heap = [(-seq, key) for key, seq in self._pending.items()]
                heapq.heapify(self._heap)
            self._cond.notify_all()

    def is_busy(self, key):
        """True while `key` is queued or being decoded, so it isn't asked for twice."""
        with self._cond:
            return key in self._pending or key in self._in_flight

    def reset(self):
        with self._cond:
            self.generation += 1
            self._heap = []
            self._pending.clear()

    def stop(self, timeout=1.0):
        with self._cond:
            self._stop = True
            self._cond.notify_all()
        deadline = time.monotonic() + timeout
        for thread in self._threads:
            thread.join(max(0.0, deadline - time.monotonic()))

    def _run(self):
        while True:
            with self._cond:
                while not self._stop and not self._heap:
                    self._cond.wait()
                if self._stop:
                    return
                negative_seq, key = heapq.heappop(self._heap)
                if self._pending.get(key) != -negative_seq:
                    continue  # superseded by a newer request for the same image
                del self._pending[key]
                generation = self.generation
                wanted = self.wanted
                folder, name, size = key
                if name not in wanted:
                    continue
                self._in_flight.add(key)
            path = os.path.join(folder, name)
            failure = None
            try:
                if self.disk_cache is not None:
                    thumb, dims = self.disk_cache.load(path, size)
                else:
                    thumb, dims = iot_utils.load_thumbnail(path, size)
            except Exception as err:
                thumb, dims = None, None
                failure = "temporary" if is_temporary_read_error(err) else "broken"
            finally:
                with self._cond:
                    self._in_flight.discard(key)
            self.out.put((generation, folder, name, size, thumb, dims, failure))


def short_label(filename, limit=26):
    return filename if len(filename) <= limit else filename[:limit - 3] + "..."


def open_with_system(path):
    if sys.platform.startswith("win"):
        os.startfile(path)
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


def format_bytes(count):
    for unit in ("B", "KB", "MB", "GB"):
        if count < 1024 or unit == "GB":
            return f"{count:.0f} {unit}" if unit == "B" else f"{count:.1f} {unit}"
        count /= 1024.0


def time_ago(timestamp):
    seconds = int(time.time() - timestamp)
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60} min ago"
    return datetime.fromtimestamp(timestamp).strftime("%H:%M")


_RGBA = re.compile(r"rgba\((\d+),\s*(\d+),\s*(\d+),\s*(\d+)\)")


def qcolor(value):
    """A palette value ('#rrggbb', 'rgba(r,g,b,a)' or an (r, g, b, a) tuple) as a QColor."""
    if isinstance(value, tuple):
        return QColor(*value)
    match = _RGBA.match(value)
    return QColor(*(int(part) for part in match.groups())) if match else QColor(value)


# ==================================================================== the picture grid

class ThumbGrid(QWidget):
    """
    The picture grid. Every file has a cell worked out from its index, and only the rows on screen are
    painted, so a folder of any size costs the same (one widget, no per-picture widgets). Hover, clicks
    and the wheel are handled here and passed on to the app.
    """
    TOP = 6     # space above the first row
    PAD = 8     # space either side of a tile within its cell

    def __init__(self, app):
        super().__init__()
        self.app = app
        self.setObjectName("transparent")
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self.scrollbar = QScrollBar(Qt.Orientation.Vertical, self)
        self.scrollbar.valueChanged.connect(self._scrolled)
        self.cols, self.cell_w, self.row_h, self.rows = 1, 200.0, 260, 0
        self.hover_idx = None
        self.colours = {}
        self.name_font = QFont(self.font())
        self.name_font.setPointSizeF(9)
        self.bold_font = QFont(self.name_font)
        self.bold_font.setBold(True)
        self.refresh_colours()

    def refresh_colours(self):
        self.colours = {key: qcolor(P[key]) for key in (
            "row_bg", "row_border", "row_hover", "btn_hover", "accent", "group", "danger", "danger_bg", "muted",
            "text", "faint", "input_bg")}
        self.update()

    # --- geometry ---
    def view_width(self):
        return max(1, self.width() - self.scrollbar.sizeHint().width())

    def content_height(self):
        return self.TOP + self.rows * self.row_h + 8

    def offset(self):
        return self.scrollbar.value()

    def update_range(self):
        maximum = max(0, self.content_height() - self.height())
        self.scrollbar.setRange(0, maximum)
        self.scrollbar.setPageStep(max(1, self.height()))
        self.scrollbar.setSingleStep(max(10, self.row_h // 3))
        self.scrollbar.setVisible(maximum > 0)
        self.update()

    def scroll_to(self, y):
        self.scrollbar.setValue(int(max(0, min(self.scrollbar.maximum(), y))))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        bar = self.scrollbar.sizeHint().width()
        self.scrollbar.setGeometry(self.width() - bar, 0, bar, self.height())
        self.update_range()
        self.app._grid_resized()

    def _scrolled(self, _value):
        self.update()
        self.app._on_grid_scrolled()

    def tile_rect(self, idx):
        row, col = divmod(idx, self.cols)
        return QRectF(col * self.cell_w + self.PAD, self.TOP + row * self.row_h - self.offset(),
                      self.cell_w - 2 * self.PAD, self.row_h - TILE_GAP)

    def index_at(self, pos):
        """The picture under a point of this widget (inside its tile, not the gaps), or None."""
        count = len(self.app.all_files)
        if not count or pos.x() >= self.view_width():
            return None
        y = pos.y() + self.offset() - self.TOP
        if y < 0:
            return None
        row, col = int(y // self.row_h), int(pos.x() // self.cell_w)
        idx = row * self.cols + col
        if col >= self.cols or idx >= count or not self.tile_rect(idx).contains(QPointF(pos)):
            return None
        return idx

    def visible_rows(self):
        top, height = self.offset(), max(1, self.height())
        first = max(0, int((top - self.TOP) // self.row_h))
        last = min(self.rows - 1, int((top + height - self.TOP) // self.row_h))
        return first, last

    def update_index(self, idx):
        if idx is not None and 0 <= idx < len(self.app.all_files):
            self.update(self.tile_rect(idx).toAlignedRect().adjusted(-3, -3, 3, 3))

    # --- painting ---
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        app = self.app
        count = len(app.all_files)
        if not count:
            font = QFont(self.font())
            font.setPointSizeF(13)
            painter.setFont(font)
            painter.setPen(self.colours["muted"])
            painter.drawText(QRectF(0, 50, self.view_width(), 60), Qt.AlignmentFlag.AlignCenter, "No images in this folder")
            painter.end()
            return
        first, last = self.visible_rows()
        clip = QRectF(event.rect())
        for idx in range(first * self.cols, min(count, (last + 1) * self.cols)):
            rect = self.tile_rect(idx)
            if rect.intersects(clip):
                self._paint_tile(painter, idx, rect)
        painter.end()

    def _paint_tile(self, painter, idx, rect):
        app, c = self.app, self.colours
        name = app.all_files[idx]
        state = app.tile_state(idx, name)
        hovered = idx == self.hover_idx
        path = QPainterPath()
        path.addRoundedRect(rect.adjusted(1, 1, -1, -1), 14, 14)
        painter.fillPath(path, c["row_bg"])
        if hovered:
            painter.fillPath(path, c["btn_hover"])
        if state:
            painter.setPen(QPen(c[{"group": "group", "range": "accent", "dup": "danger"}[state]], 2))
        else:
            painter.setPen(QPen(c["row_hover"] if hovered else c["row_border"], 1))
        painter.drawPath(path)

        size = app.thumb_size
        square = QRectF(rect.center().x() - size / 2, rect.top() + 10, size, size)
        pixmap, failed = app.thumb_pixmap(name)
        if pixmap is not None:
            ratio = pixmap.devicePixelRatio()
            w, h = pixmap.width() / ratio, pixmap.height() / ratio
            painter.drawPixmap(QPointF(square.center().x() - w / 2, square.center().y() - h / 2), pixmap)
        else:
            box = QPainterPath()
            box.addRoundedRect(square.adjusted(size * 0.06, size * 0.06, -size * 0.06, -size * 0.06), 10, 10)
            painter.fillPath(box, c["danger_bg"] if failed else c["input_bg"])
            if failed:
                painter.setPen(c["danger"])
                painter.setFont(self.name_font)
                painter.drawText(square, Qt.AlignmentFlag.AlignCenter, "Can't read this image")

        metrics = QFontMetrics(self.bold_font if state else self.name_font)
        text_rect = QRectF(rect.left() + 10, square.bottom() + 8, rect.width() - 20, metrics.height() + 6)
        shown = metrics.elidedText(name, Qt.TextElideMode.ElideMiddle, int(text_rect.width() - 16))
        if state:
            pill = QPainterPath()
            width = min(text_rect.width(), metrics.horizontalAdvance(shown) + 18)
            pill.addRoundedRect(QRectF(text_rect.center().x() - width / 2, text_rect.top(), width, text_rect.height()), 8, 8)
            painter.fillPath(pill, c[{"group": "group", "range": "accent", "dup": "danger"}[state]])
            painter.setPen(QColor("#ffffff"))
            painter.setFont(self.bold_font)
        else:
            painter.setPen(c["text"] if hovered else c["muted"])
            painter.setFont(self.name_font)
        painter.drawText(text_rect, Qt.AlignmentFlag.AlignCenter, shown)

    # --- mouse ---
    def mouseMoveEvent(self, event):
        self.app.set_hover(self.index_at(event.position().toPoint()), event.globalPosition().toPoint())
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):
        self.app.set_hover(None)
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            idx = self.index_at(event.position().toPoint())
            if idx is not None:
                mods = event.modifiers()
                # Ctrl / Alt here are Cmd / Option on macOS (where Ctrl+Click means right-click)
                mode = ("ctrl" if mods & Qt.KeyboardModifier.ControlModifier else
                        "alt" if mods & Qt.KeyboardModifier.AltModifier else "click")
                self.app.handle_image_click(idx, mode)
            event.accept()
            return
        super().mousePressEvent(event)

    def contextMenuEvent(self, event):
        self.app.show_context_menu(event.globalPos())
        event.accept()

    def wheelEvent(self, event):
        self.app.grid_wheel(event)
        event.accept()


class HoverTip(QFrame):
    """The picture's name and size next to the pointer. Never takes focus or the mouse."""

    def __init__(self):
        super().__init__(None, Qt.WindowType.ToolTip | Qt.WindowType.FramelessWindowHint |
                         Qt.WindowType.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        box = QFrame()
        box.setObjectName("hovertip")
        outer.addWidget(box)
        lines = QVBoxLayout(box)
        lines.setContentsMargins(12, 7, 12, 8)
        lines.setSpacing(1)
        self.name = QLabel()
        self.name.setStyleSheet("font-weight: 700;")
        self.size = label("", "muted")
        lines.addWidget(self.name)
        lines.addWidget(self.size)

    def show_at(self, name, dims, global_pos):
        self.name.setText(name)
        self.size.setText(f"{dims[0]} × {dims[1]} px" if dims else "Size unknown")
        self.adjustSize()
        screen = QGuiApplication.screenAt(global_pos) or QGuiApplication.primaryScreen()
        right = screen.geometry().right()
        x = global_pos.x() + 15
        if x + self.width() > right:
            x = global_pos.x() - self.width() - 15
        self.move(x, global_pos.y() + 18)
        if not self.isVisible():
            self.show()


class ZoomPreview(QWidget):
    """Hover + Hold Z: the picture as large as the screen allows, over a frosted copy of the screen. It never
    takes the keyboard, so letting go of Z still reaches the main window."""

    def __init__(self, screen, pixmap, frost):
        super().__init__(None, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint |
                         Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.WindowDoesNotAcceptFocus |
                         Qt.WindowType.WindowTransparentForInput)   # the pointer stays "over" its picture
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.pixmap, self.frost = pixmap, frost
        area = screen.geometry()
        ratio = pixmap.devicePixelRatio()
        w, h = int(pixmap.width() / ratio), int(pixmap.height() / ratio)
        if frost is not None:
            self.setGeometry(area)
        else:   # no frost: only the picture covers the screen
            self.setGeometry(area.x() + (area.width() - w) // 2, area.y() + max(0, (area.height() - h) // 2), w, h)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        if self.frost is not None:
            painter.drawPixmap(self.rect(), self.frost)
        else:
            painter.fillRect(self.rect(), QColor("#0c0c0d"))
        ratio = self.pixmap.devicePixelRatio()
        w, h = self.pixmap.width() / ratio, self.pixmap.height() / ratio
        painter.drawPixmap(QPointF((self.width() - w) / 2, max(0.0, (self.height() - h) / 2)), self.pixmap)
        painter.end()


# ==================================================================== folder list popup

class _Clickable(QLabel):
    """A label that runs a callback when clicked (the folder list's ▸ / ▾ and the tag picker's circles)."""

    def __init__(self, text="", callback=None):
        super().__init__(text)
        self.callback = callback
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.callback:
            self.callback()
            event.accept()
            return
        super().mousePressEvent(event)


class TagPicker(QWidget):
    """A row of colour circles (plus an outlined 'no tag' one). Clicking outside closes it."""

    def __init__(self, app, path, global_pos, on_change=None):
        super().__init__(app, Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint |
                         Qt.WindowType.NoDropShadowWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.app, self.path, self.on_change = app, path, on_change
        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        box = QFrame()
        box.setObjectName("popup")
        outer.addWidget(box)
        row = QHBoxLayout(box)
        row.setContentsMargins(10, 8, 10, 8)
        row.setSpacing(4)
        current = app.folder_tag(path)
        for colour in TAG_COLORS + (None,):
            swatch = _Clickable(callback=lambda value=colour: self.choose(value))
            swatch.setPixmap(self._swatch(colour, colour == current))
            swatch.setToolTip(colour.capitalize() if colour else "No tag")
            row.addWidget(swatch)
        self.adjustSize()
        screen = (QGuiApplication.screenAt(global_pos) or QGuiApplication.primaryScreen()).availableGeometry()
        x = max(screen.left() + 4, min(global_pos.x() - 12, screen.right() - self.width() - 4))
        y = max(screen.top() + 4, min(global_pos.y() + 8, screen.bottom() - self.height() - 4))
        self.move(x, y)

    @staticmethod
    def _swatch(colour, current, size=26):
        ratio = 2
        pixmap = QPixmap(size * ratio, size * ratio)
        pixmap.setDevicePixelRatio(ratio)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if current:   # a ring round the current choice
            painter.setPen(QPen(QColor(P["muted"]), 1.5))
            painter.drawEllipse(QRectF(1.5, 1.5, size - 3, size - 3))
        if colour:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(P["tag_" + colour]))
            painter.drawEllipse(QRectF(6, 6, size - 12, size - 12))
        else:         # "no tag"
            painter.setPen(QPen(QColor(P["muted"]), 1.5))
            painter.drawEllipse(QRectF(7, 7, size - 14, size - 14))
        painter.end()
        return pixmap

    def choose(self, colour):
        self.close()
        self.app.set_folder_tag(self.path, colour)
        if self.on_change:
            self.on_change()


class _TreeRow(QFrame):
    """One folder in the folder list."""

    def __init__(self, popup, index, path):
        super().__init__()
        self.popup, self.index, self.path = popup, index, path
        self.setObjectName("treerow")
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def enterEvent(self, event):
        self.popup.hovered = self.index
        super().enterEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.popup.jump_to(self.path)
        elif event.button() == Qt.MouseButton.RightButton:
            self.popup.open_tags(self.path, event.globalPosition().toPoint())
        event.accept()

    def contextMenuEvent(self, event):   # macOS Ctrl+Click
        self.popup.open_tags(self.path, event.globalPos())
        event.accept()


class FolderListPopup(QWidget):
    """
    The folder list under the folder box: the whole hub as a tree with counts, sizes, flags and tags.
    Click a folder to open it, ▸ / ▾ to open or close a branch, + / − to open or close everything inside it,
    right-click to tag it. Hover + D / U resizes the branch; Hover + Ctrl (pressed and let go on its own)
    renames its images after their folders.
    """
    ROW_SPACING = 0

    def __init__(self, app):
        super().__init__(app, Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint |
                         Qt.WindowType.NoDropShadowWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.app = app
        self.hovered = None
        self.branch_end = []
        self.active_row = None
        self._ctrl_armed = False
        self._width = 0
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        frame = QFrame()
        frame.setObjectName("popup")
        outer.addWidget(frame)
        inner = QVBoxLayout(frame)
        inner.setContentsMargins(6, 6, 2, 6)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        inner.addWidget(self.scroll)
        registry = app.virtual_folder_path_expansion_registry
        active = app._norm(app.image_folder)
        for info in app.folder_info_list:   # open every branch the current folder sits in, so it can be shown
            norm = app._norm(info["path"])
            if norm != active and iot_history._same_or_inside(active, norm):
                registry[norm] = True
        self.rebuild()

    # --- building ---
    def rebuild(self):
        app = self.app
        registry = app.virtual_folder_path_expansion_registry
        infos = app.folder_info_list
        norms = [app._norm(info["path"]) for info in infos]
        active = app._norm(app.image_folder)
        # Where each folder's branch ends in the (depth-first) list...
        branch_end = [len(infos)] * len(infos)
        stack = []
        for i, item in enumerate(infos):
            while stack and infos[stack[-1]]["depth"] >= item["depth"]:
                branch_end[stack.pop()] = i
            stack.append(i)
        self.branch_end = branch_end
        # ...and running counts of closed and open branches, so "is anything below this still closed / open?"
        # is instant (for the + and − buttons)
        closed_before, open_before = [0], [0]
        for i in range(len(infos)):
            is_branch = branch_end[i] > i + 1
            is_open = registry.get(norms[i], infos[i]["depth"] == 0)
            closed_before.append(closed_before[-1] + (is_branch and not is_open))
            open_before.append(open_before[-1] + (is_branch and is_open))

        metrics = QFontMetrics(self.font())
        small = QFont(self.font())
        small.setPointSizeF(9)
        small_metrics = QFontMetrics(small)
        count_width = small_metrics.horizontalAdvance("9999 · Σ 99999") + 10
        size_width = small_metrics.horizontalAdvance("999.9 MB · Σ 999.9 GB") + 10
        holder = QWidget()
        holder.setObjectName("transparent")
        rows = QVBoxLayout(holder)
        rows.setContentsMargins(0, 0, 8, 0)
        rows.setSpacing(self.ROW_SPACING)
        self.active_row = None
        hide_below = None
        row_count = 0
        for index, info in enumerate(infos):
            depth = info["depth"]
            if hide_below is not None:
                if depth > hide_below:
                    continue
                hide_below = None
            norm = norms[index]
            has_children = branch_end[index] > index + 1
            expanded = registry.setdefault(norm, depth == 0)
            if has_children and not expanded:
                hide_below = depth
            is_active = norm == active
            pending = app._is_pending_delete(info["path"])

            row = _TreeRow(self, index, info["path"])
            row.setProperty("active", "true" if is_active else "false")
            line = QHBoxLayout(row)
            line.setContentsMargins(12 + depth * 18, 5, 6, 5)
            line.setSpacing(4)
            if has_children:
                arrow = _Clickable("▾" if expanded else "▸", lambda n=norm: self.toggle_branch(n))
                arrow.setObjectName("muted")
                arrow.setFixedWidth(16)
                arrow.setAlignment(Qt.AlignmentFlag.AlignCenter)
                line.addWidget(arrow)
            else:
                line.addSpacing(16 + line.spacing())
            dot = QLabel()
            dot.setFixedSize(16, 14)
            tag = app.folder_tag(info["path"])
            if tag:
                dot.setPixmap(dot_pixmap(P["tag_" + tag], 11))
            line.addWidget(dot)
            name = QLabel(info["name"] if depth else f"{info['name']}  (hub)")
            name.setObjectName("treename")
            name.setProperty("active", "true" if is_active else "false")
            name.setProperty("pending", "true" if pending else "false")
            if pending or is_active:
                font = QFont(name.font())
                font.setStrikeOut(pending)   # Q: crossed out until I.O.T closes
                font.setBold(is_active)
                name.setFont(font)
            line.addWidget(name)
            if has_children and expanded:   # + (open everything below) / − (close everything below)
                if closed_before[branch_end[index]] > closed_before[index + 1]:
                    line.addWidget(self._branch_button(True, index, norms))
                if open_before[branch_end[index]] > open_before[index + 1]:
                    line.addWidget(self._branch_button(False, index, norms))
            line.addSpacing(4)
            flags = app._folder_flags(info).strip()
            if flags:
                line.addWidget(QLabel(flags))
            line.addStretch(1)
            own, total = info["image_count"], info["total_image_count"]
            if total > own:   # sub-folders hold images too: the sum for the whole branch
                count_text = f"{own} · Σ {total}" if own else f"Σ {total}"
            else:
                count_text = str(own) if own else ""
            count = label(count_text, "treecount")
            count.setFixedWidth(count_width)
            count.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            line.addWidget(count)
            if app.tree_show_sizes:
                own_bytes, branch_bytes = info["bytes"], info["total_bytes"]
                if branch_bytes > own_bytes:
                    size_text = (f"{format_bytes(own_bytes)} · Σ {format_bytes(branch_bytes)}" if own_bytes
                                 else f"Σ {format_bytes(branch_bytes)}")
                else:
                    size_text = format_bytes(own_bytes) if own_bytes else ""
                size = label(size_text, "treecount")
                size.setFixedWidth(size_width)
                size.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                line.addWidget(size)
            rows.addWidget(row)
            row_count += 1
            if is_active:
                self.active_row = row
        rows.addStretch(1)
        position = self.scroll.verticalScrollBar().value()
        self.scroll.setWidget(holder)
        holder.adjustSize()

        # At least as wide as the folder box, wider when the names need it (the box is narrow in half-window
        # mode); it never shrinks while open, so opening a branch doesn't make it jump
        app = self.app
        anchor = app.folder_bar
        row_height = max(metrics.height() + 10, holder.sizeHint().height() // max(1, row_count))
        height = max(60, min(440, row_count * row_height + 14))
        needed = holder.sizeHint().width() + self.scroll.verticalScrollBar().sizeHint().width() + 16
        window = app.geometry()
        width = max(anchor.width(), self._width, min(max(needed, 460), 760, window.width() - 16))
        self._width = width
        corner = anchor.mapToGlobal(QPoint(0, anchor.height() + 4))
        left = max(window.left() + 8, min(corner.x(), window.right() - 8 - width))
        self.setGeometry(left, corner.y(), width, height)
        self.scroll.verticalScrollBar().setValue(position)

    def _branch_button(self, open_them, index, norms):
        widget = button("+" if open_them else "−", "mini", "Open all sub-folders" if open_them else "Close all sub-folders",
                        lambda: self.set_all_below(index, norms, open_them))
        widget.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        return widget

    def centre_on_active(self):
        if self.active_row is None:
            return
        bar = self.scroll.verticalScrollBar()
        view = self.scroll.viewport().height()
        bar.setValue(int(self.active_row.y() + self.active_row.height() / 2 - view / 2))

    # --- actions ---
    def toggle_branch(self, norm):
        registry = self.app.virtual_folder_path_expansion_registry
        registry[norm] = not registry.get(norm, False)
        self.rebuild()

    def set_all_below(self, index, norms, open_them):
        """+ / −: open (or close) every branch inside this folder, all the way down. The folder itself stays
        open, so closing leaves a tidy list of its direct sub-folders."""
        registry = self.app.virtual_folder_path_expansion_registry
        for j in range(index + 1, self.branch_end[index]):
            if self.branch_end[j] > j + 1:
                registry[norms[j]] = open_them
        self.rebuild()

    def jump_to(self, path):
        self.close()
        self.app.load_directory_target_sequence(path)

    def open_tags(self, path, global_pos):
        if self.app._tag_key(path) is not None:
            TagPicker(self.app, path, global_pos, on_change=lambda: self.rebuild() if self.isVisible() else None).show()

    def hovered_branch(self):
        """The folder under the pointer and every folder inside it (minus ones marked for deletion)."""
        index = self.hovered
        if index is None or self.app.busy or index >= len(self.branch_end):
            return []
        infos = self.app.folder_info_list
        return [infos[j]["path"] for j in range(index, self.branch_end[index])
                if not self.app._is_pending_delete(infos[j]["path"])]

    # --- keys: Hover + D / U resizes the branch, Hover + Ctrl (on its own) renames it ---
    def keyPressEvent(self, event):
        key = event.key()
        self._ctrl_armed = key == Qt.Key.Key_Control and not event.isAutoRepeat()
        plain = not (event.modifiers() & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier))
        if key in (Qt.Key.Key_D, Qt.Key.Key_U) and plain and not event.isAutoRepeat():
            folders = self.hovered_branch()
            if folders:
                self.close()
                self.app.run_resizer("down" if key == Qt.Key.Key_D else "up", folders)
            event.accept()
            return
        if key == Qt.Key.Key_Escape:
            self.close()
            return
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event):
        if event.key() == Qt.Key.Key_Control and self._ctrl_armed and not event.isAutoRepeat():
            self._ctrl_armed = False
            folders = self.hovered_branch()
            if folders:
                self.close()
                self.app.rename_folder_branch(folders)
            event.accept()
            return
        super().keyReleaseEvent(event)

    def leaveEvent(self, event):
        self.hovered = None
        super().leaveEvent(event)

    def hideEvent(self, event):
        super().hideEvent(event)
        self.app._tree_popup_closed(self)


# ==================================================================== the main window

class ImageOrganizationToolkit(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"{APP_NAME}  —  {APP_SUBTITLE}")
        self.setMinimumSize(980, 560)
        if FRAMELESS:
            self.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint |
                                Qt.WindowType.WindowMinMaxButtonsHint | Qt.WindowType.WindowSystemMenuHint)
        self.setAcceptDrops(True)

        # --- ENVIRONMENT PATHS ---
        self.parent_hub = self._resolve_start_folder()
        self.image_folder = self.parent_hub
        self.cache_folder = os.path.join(self.parent_hub, ".thumbs_cache")
        self.settings_path = SETTINGS_FILE
        self.change_log_path = CHANGE_LOG_FILE
        self.output_base = self.parent_hub
        self.valid_extensions = iot_utils.IMAGE_EXTENSIONS

        # --- SETTINGS (all persisted to iot_settings.json) ---
        self.anomaly_threshold_var = Var("30")
        self.down_target = 1800
        self.up_target = 1800
        self.jpg_quality = 90
        self.target_resolution = 1800
        self.thumb_size = 201
        self.rename_var = Var(True)
        self.skip_folder_var = Var(False)
        self.keep_cache_var = Var(True)   # "Disable Cache Deletion", on by default
        self.flags_enabled = {"target": True, "size": True, "modified": True}  # folder flags shown (Settings)
        self.frost_preview = True         # frosted screen behind the Hover + Hold Z preview (Settings)
        self.base_image = 5               # checks use the Nth picture, so covers / thumbnails at the start are skipped
        self.tree_show_sizes = True       # folder sizes in the folder list (Settings)
        self.strip_exif = False           # resized images saved without EXIF (camera model, GPS...) (Settings)
        self.scroll_speed = 100           # % of the normal mouse-wheel step in the picture grid (Settings)
        self.theme_name = self.load_settings()
        set_palette(self.theme_name)
        self._settings_base = self._current_settings()  # what was loaded; see save_settings

        # --- UNDO / RECYCLE / LOG ---
        self.session = iot_history.Session(self.parent_hub)
        self.change_log = iot_history.ChangeLog(CHANGE_LOG_FILE)
        self.history = iot_history.History(self.change_log)
        self.disk_cache = iot_utils.ThumbDiskCache(self.cache_folder)
        self._retired_hubs = []       # (session, disk cache) of hubs left via drag and drop; cleaned up on exit

        # --- FOLDER REGISTRY ---
        self.mapped_folders = []
        self.folder_info_list = []
        self.folder_info = {}
        self.modified_folders = set()
        self.pending_delete_folders = set()  # Q: deleted when I.O.T closes; crossed out in the folder list until then
        self.folder_tags = {}             # hub-relative folder key -> tag colour (right-click a folder)
        self._load_folder_tags()
        self.virtual_folder_path_expansion_registry = {}
        self._resolution_cache = {}   # (image path, mtime) -> (w, h); avoids reopening files on every rescan
        self._hash_cache = {}         # (image path, mtime, size) -> dHash
        self.scan_entire_directory_tree()

        # --- GRID STATE ---
        self.all_files = []
        self.file_index = {}
        self.first_selected_idx = None
        self.group_mode = False           # G: pick any pictures, Enter copies them into a folder
        self.group_picks = {}             # filename -> None, in the order they were picked
        self.image_cache = OrderedDict()  # (folder, filename, pixel size) -> QPixmap, least recently used first
        self.dims_cache = {}              # (folder, filename) -> original (w, h)
        self._failed_thumbs = set()       # (folder, filename) that couldn't be decoded; not retried
        self._thumb_retries = {}          # (folder, filename) -> (retry after, attempts) for reads that failed for now
        self.hovered_filename = None
        self.preview_window = None
        self._frost_pool = ThreadPoolExecutor(max_workers=1)
        self.z_key_is_pressed = False
        self.active_duplicate_filename = None
        self.busy = False             # True while a background batch (resize / duplicate scan) runs
        self.tree_popup_window = None
        self._tree_closed_at = 0.0
        self._undo_window = None
        self.last_folder_name = "Sequence"
        self._startup_done = False
        self._last_grid_width = 0
        self._last_ranges = None
        self._view_pending = False
        self._thumb_queue = queue.Queue()
        worker_count = max(2, min(6, (os.cpu_count() or 4) - 1))
        self._thumb_loader = ThumbnailLoader(self._thumb_queue, worker_count, self.disk_cache)

        # Every option change is saved straight away
        for var in (self.rename_var, self.skip_folder_var, self.keep_cache_var):
            var.trace_add("write", lambda *a: self.save_settings())

        self._save_soon = QTimer(self, singleShot=True, interval=600)
        self._save_soon.timeout.connect(self.save_settings)
        self._relayout_timer = QTimer(self, singleShot=True, interval=120)
        self._relayout_timer.timeout.connect(self._relayout_after_resize)
        self._zoom_timer = QTimer(self, singleShot=True, interval=150)
        self._zoom_timer.timeout.connect(self._apply_thumb_size)
        self._build_ui()
        iot_qt.on_theme_change(self._on_theme_changed)
        self.tip = HoverTip()
        self.load_directory_target_sequence(self.image_folder)

        self.drain_timer = QTimer(self, interval=15)
        self.drain_timer.timeout.connect(self._drain_thumb_queue)
        self.drain_timer.start()
        self.heal_timer = QTimer(self, interval=HEAL_INTERVAL_MS)
        self.heal_timer.timeout.connect(self._heal_thumbnails)
        self.heal_timer.start()
        QApplication.instance().installEventFilter(self)   # the single-key shortcuts (see eventFilter)

    # ------------------------------------------------------------------ setup helpers

    def _resolve_start_folder(self):
        # 1. An explicit directory handed over as a command-line argument
        if len(sys.argv) > 1 and os.path.isdir(sys.argv[1]):
            return os.path.abspath(sys.argv[1])
        # 2. The folder OneCommander launched us from
        active_working_dir = os.path.abspath(os.getcwd())
        if os.path.isdir(active_working_dir) and "onecommander" not in active_working_dir.lower():
            return active_working_dir
        # 3. The script's own folder (e.g. launched from Thonny)
        return SCRIPT_DIR

    def load_settings(self):
        """Reads iot_settings.json. Returns the saved theme name."""
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return "dark"
        try:
            self.anomaly_threshold_var.set(str(data.get("anomaly_threshold", self.anomaly_threshold_var.get())))
            self.down_target = int(data.get("down_target", self.down_target))
            self.up_target = int(data.get("up_target", self.up_target))
            self.jpg_quality = max(1, min(100, int(data.get("jpg_quality", self.jpg_quality))))
            self.target_resolution = int(data.get("target_resolution", self.target_resolution))
            self.thumb_size = max(60, min(300, int(data.get("thumb_size", self.thumb_size))))
            self.rename_var.set(bool(data.get("sequential_rename", True)))
            self.skip_folder_var.set(bool(data.get("skip_folder_creation", False)))
            self.keep_cache_var.set(bool(data.get("disable_cache_deletion", True)))
            self.frost_preview = bool(data.get("frosted_preview", True))
            self.base_image = max(1, int(data.get("base_image", self.base_image)))
            self.tree_show_sizes = bool(data.get("tree_show_sizes", True))
            self.strip_exif = bool(data.get("strip_exif", False))
            self.scroll_speed = max(50, min(400, round(int(data.get("scroll_speed", 100)) / 10) * 10))  # 50-400% in 10% steps
            saved_flags = data.get("folder_flags", {})
            if isinstance(saved_flags, dict):
                for key in self.flags_enabled:
                    self.flags_enabled[key] = bool(saved_flags.get(key, True))
        except (TypeError, ValueError):
            pass
        return data.get("theme", "dark") if data.get("theme") in ("dark", "light") else "dark"

    def _current_settings(self):
        return {
            "theme": getattr(self, "theme_name", "dark"),
            "sequential_rename": self.rename_var.get(),
            "skip_folder_creation": self.skip_folder_var.get(),
            "disable_cache_deletion": self.keep_cache_var.get(),
            "folder_flags": dict(self.flags_enabled),
            "frosted_preview": self.frost_preview,
            "base_image": self.base_image,
            "tree_show_sizes": self.tree_show_sizes,
            "strip_exif": self.strip_exif,
            "scroll_speed": self.scroll_speed,
            "thumb_size": self.thumb_size,
            "anomaly_threshold": self.anomaly_threshold_var.get(),
            "down_target": self.down_target,
            "up_target": self.up_target,
            "jpg_quality": self.jpg_quality,
            "target_resolution": self.target_resolution,
        }

    def save_settings(self):
        """
        Writes iot_settings.json. Several I.O.T windows can be open at once, so only the settings this
        window has changed are written; for the rest, whatever another window saved in the meantime is kept
        (otherwise closing one window would undo a change made in another).
        """
        mine = self._current_settings()
        data = dict(mine)
        base = getattr(self, "_settings_base", None)  # this window's values when it last loaded/saved
        if base is not None:
            try:
                with open(SETTINGS_FILE, "r", encoding="utf-8") as handle:
                    on_disk = json.load(handle)
            except (OSError, ValueError):
                on_disk = {}
            if isinstance(on_disk, dict):
                for key, value in on_disk.items():
                    if key in data and key in base and data[key] == base[key]:
                        data[key] = value  # unchanged here: keep the other window's value
        temp = SETTINGS_FILE + ".tmp"
        try:
            with open(temp, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2)
            os.replace(temp, SETTINGS_FILE)
            self._settings_base = mine
        except OSError as err:
            if hasattr(self, "status_bar_text"):
                self.log_status(f"Settings could not be saved: {err}", "warning")

    # ------------------------------------------------------------------ folder tree scanning

    def scan_entire_directory_tree(self):
        """Walks the whole folder tree once and computes the 🎯 / ❗ / ⚑ flags. After a change inside
        folders that are already known, refresh_folders() is much cheaper."""
        folders = []

        def walk(path, depth):
            """Returns (image count, bytes) of this folder plus everything below it."""
            subdirs, total_bytes, images = iot_utils.scan_folder(path, self.valid_extensions)
            subdirs.sort(key=iot_utils.natural_sort_key)
            info = {
                "path": path,
                "depth": depth,
                "name": os.path.basename(path.rstrip("\\/")) or path,
                "bytes": total_bytes,
                "image_count": len(images),
                "subdirs": subdirs,
                "res_mismatch": self._check_resolution_mismatch(path, images),
            }
            folders.append(info)
            subtree_total, subtree_bytes = len(images), total_bytes
            if depth < 40:  # guards against junction/symlink loops
                for folder in subdirs:
                    child_images, child_bytes = walk(os.path.join(path, folder), depth + 1)
                    subtree_total += child_images
                    subtree_bytes += child_bytes
            info["total_image_count"] = subtree_total
            info["total_bytes"] = subtree_bytes
            return subtree_total, subtree_bytes

        walk(self.parent_hub, 0)
        self.folder_info_list = folders
        self.folder_info = {info["path"]: info for info in folders}
        self.mapped_folders = [info["path"] for info in folders]
        self._recompute_folder_flags()

    def refresh_folders(self, paths):
        """
        After a change inside folders that are already known (deletes, resizes, renames in place, undo):
        re-reads just those folders and updates their counts, sizes and flags, plus their parents' Σ totals,
        instead of re-walking the whole hub. Falls back to a full scan when a folder was added, removed or
        renamed, since the tree itself changed.
        """
        recycle = iot_history.RECYCLE_DIR_NAME
        for path in dict.fromkeys(p for p in paths if p and recycle not in os.path.normcase(p)):
            info = self.folder_info.get(path)
            if info is None or not os.path.isdir(path):
                self.scan_entire_directory_tree()
                return
            subdirs, total_bytes, images = iot_utils.scan_folder(path, self.valid_extensions)
            subdirs.sort(key=iot_utils.natural_sort_key)
            if subdirs != info["subdirs"]:
                self.scan_entire_directory_tree()
                return
            image_change, byte_change = len(images) - info["image_count"], total_bytes - info["bytes"]
            info.update(bytes=total_bytes, image_count=len(images),
                        res_mismatch=self._check_resolution_mismatch(path, images))
            node = info
            while node is not None:  # this folder and every folder above it, up to the hub
                node["total_image_count"] += image_change
                node["total_bytes"] += byte_change
                node = None if node["depth"] == 0 else self.folder_info.get(os.path.dirname(node["path"]))
        self._recompute_folder_flags()

    def _recompute_folder_flags(self):
        """The ❗ (unusually large) and ⚑ (modified) flags, from the sizes already gathered: no disk access."""
        try:
            buffer_percentage = float(self.anomaly_threshold_var.get()) / 100.0
        except ValueError:
            buffer_percentage = 0.30
        folders = self.folder_info_list
        subfolder_sizes = [info["bytes"] for info in folders[1:] if info["bytes"] > 0]
        average_subfolder_size = sum(subfolder_sizes) / len(subfolder_sizes) if subfolder_sizes else 0
        max_allowed_threshold = average_subfolder_size * (1 + buffer_percentage)
        for info in folders:
            info["modified"] = info["path"] in self.modified_folders
            info["oversize"] = (not info["modified"]) and average_subfolder_size > 0 and info["bytes"] > max_allowed_threshold

    def _check_resolution_mismatch(self, folder, images):
        n = self.base_image  # Settings > Base image
        if len(images) < n:
            return False
        base_name, mtime = heapq.nsmallest(n, images, key=lambda item: iot_utils.natural_sort_key(item[0]))[n - 1]
        image_path = os.path.join(folder, base_name)
        cache_key = (image_path, mtime)
        size = self._resolution_cache.get(cache_key)
        if size is None:
            try:
                size = iot_utils.read_image_size(image_path)
            except Exception:
                size = False
            self._resolution_cache[cache_key] = size
        if not size:
            return False
        w, h = size
        return w != self.target_resolution and h != self.target_resolution

    def _folder_flags(self, info):
        if not info:
            return ""
        parts = []
        enabled = self.flags_enabled
        if info["res_mismatch"] and enabled["target"]:
            parts.append(FLAG_TARGET)
        if info["modified"]:
            if enabled["modified"]:
                parts.append(FLAG_MODIFIED)
        elif info["oversize"] and enabled["size"]:
            parts.append(FLAG_SIZE)
        return (" ".join(parts) + " ") if parts else ""

    def refresh_folder_button(self):
        """Updates the folder box (breadcrumb, tag, flags, image counts) and the status line info."""
        info = self.folder_info.get(self.image_folder)
        hub_name = os.path.basename(self.parent_hub.rstrip("\\/")) or self.parent_hub
        try:
            relative = os.path.relpath(self.image_folder, self.parent_hub)
        except ValueError:
            relative = self.image_folder
        parts = [] if relative in (".", "") else relative.split(os.sep)
        if parts:
            path_text, name = "  ›  ".join([hub_name] + parts[:-1]) + "  ›  ", parts[-1]
        else:
            path_text, name = "", hub_name
        pending = self._is_pending_delete(self.image_folder)
        count = len(self.all_files)
        count_text = f"{count} image{'s' if count != 1 else ''}"
        sizes = self.tree_show_sizes and info
        if sizes:
            count_text += f" ({format_bytes(info['bytes'])})"
        below = info["total_image_count"] - info["image_count"] if info else 0
        if below > 0:  # sub-folders hold images too: show the sum for the whole branch
            count_text += f"  ·  Σ {count + below}"
            if sizes:
                count_text += f" ({format_bytes(info['total_bytes'])})"
        if pending:
            count_text = "Deleted when I.O.T closes  ·  " + count_text
        tag = self.folder_tag(self.image_folder)
        self.folder_bar.set_folder(path_text, name, self._folder_flags(info).strip(), count_text,
                                   P["tag_" + tag] if tag else None, pending)
        self._update_status_info()

    @staticmethod
    def _norm(path):
        return os.path.normcase(os.path.normpath(path)).rstrip("\\/")

    # ------------------------------------------------------------------ building the window

    def _build_ui(self):
        root = WindowRoot(self)
        self.root = root
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(24, 50 if FRAMELESS else 22, 24, 12)
        outer.setSpacing(14)

        # header: name, folder box, buttons
        head = QHBoxLayout()
        head.setSpacing(10)
        titles = QVBoxLayout()
        titles.setSpacing(0)
        titles.addWidget(label(APP_NAME, "title"))
        titles.addWidget(label(APP_SUBTITLE.upper(), "subtitle"))
        head.addLayout(titles)
        head.addSpacing(24)
        self.folder_bar = FolderBar()
        self.folder_bar.clicked.connect(self.toggle_collapsible_tree_panel)
        self.folder_bar.right_clicked.connect(self._open_tag_picker_for_selector)
        self.folder_bar.setToolTip("Click: the folder list  ·  Right-click: tag this folder  ·  Drop a folder onto the "
                                   "window to open it")
        head.addWidget(self.folder_bar, 1)
        self._head_gap = QWidget()          # takes the folder box's place when it moves to its own row
        self._head_gap.setObjectName("transparent")
        self._head_gap.hide()
        head.addWidget(self._head_gap, 1)
        head.addSpacing(6)
        self._head = head
        self.undo_button = button("Undo", "", f"Recent changes, each with Revert  ({UNDO_KEYS} undoes the latest)",
                                  self.show_undo_window)
        self.change_log_button = button("Change Log", "ghost", "Every change I.O.T has made, in a text file",
                                        self.open_change_log)
        self.settings_button = button("Settings", "ghost", "Shortcuts, folder flags and processing options",
                                      self.open_settings)
        self.refresh_button = button("Refresh", "ghost", "Pick up changes made outside I.O.T  (F5)", self.refresh)
        self.theme_button = button("", "", "Light / dark", self.toggle_theme)
        self.theme_button.setMinimumWidth(104)
        for widget in (self.undo_button, self.change_log_button, self.settings_button, self.refresh_button,
                       self.theme_button):
            widget.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            head.addWidget(widget)
        outer.addLayout(head)
        self._folder_row = QHBoxLayout()   # narrow windows (half-window mode): the folder box goes here
        outer.addLayout(self._folder_row)
        if FRAMELESS:
            root.controls = WindowControls(self, root)

        # toolbar card
        toolbar = card()
        self.toolbar = toolbar
        self.toolbar_flow = FlowLayout(toolbar, h_spacing=18, v_spacing=10)
        self.toolbar_flow.setContentsMargins(16, 12, 16, 12)
        self.toolbar_flow.addWidget(self._options_group())
        self.toolbar_flow.addWidget(self._tools_group())
        self.toolbar_flow.addWidget(self._zoom_group())
        outer.addWidget(toolbar)

        # the grid
        self.grid = ThumbGrid(self)
        outer.addWidget(self.grid, 1)
        self.half_button = button("", "tab", "Half width (right side of the screen, room for Explorer to drag folders "
                                  "in) / back to full size", self.toggle_half_window)
        self.half_button.setParent(self.grid)
        self.half_button.setFixedSize(18, 72)
        self.half_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        # status line
        status = QHBoxLayout()
        status.setSpacing(10)
        self.status_dot = QLabel()
        self.status_dot.setFixedSize(10, 10)
        self.status_bar_text = label("Ready")
        self.status_selection_label = label("")
        self.status_info_label = label("", "muted")
        status.addWidget(self.status_dot, 0, Qt.AlignmentFlag.AlignVCenter)
        status.addWidget(self.status_bar_text)
        status.addStretch(1)
        status.addWidget(self.status_selection_label)
        status.addSpacing(18)
        status.addWidget(self.status_info_label)
        outer.addLayout(status)
        self.status_queue = []
        self._status_kind = "info"
        self.status_timer = QTimer(self, singleShot=True)
        self.status_timer.timeout.connect(self._display_next_status)

        self.create_context_menu()
        self._on_theme_changed()
        self._refresh_undo_button()

    def _options_group(self):
        box, row = transparent_box(QHBoxLayout, 9)
        row.addWidget(caps("Options"))
        for text, var, tip in (
                ("Sequential Rename", self.rename_var, "Moved pictures are renamed '<folder name> - 01', '- 02'..."),
                ("Skip Folder Creation", self.skip_folder_var, "A selected range stays in this folder (renamed if "
                                                               "Sequential Rename is on) instead of a new sub-folder"),
                ("Disable Cache Deletion", self.keep_cache_var, "Keep the thumbnail cache when I.O.T closes, so folders "
                                                                "open faster next time")):
            widget = bind_check(QCheckBox(text), var)
            widget.setObjectName("toolcheck")
            widget.setToolTip(tip)
            widget.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            row.addWidget(widget)
        return box

    def _tools_group(self):
        self._tool_texts = []   # (button, full text, short text): narrow windows drop the key hints
        box, row = transparent_box(QHBoxLayout, 5)
        row.addWidget(caps("Tools"))
        row.addSpacing(4)
        # Alphabetical; Group carries the animated border, Zoom is shown as a feature (not clickable)
        for text, key, command, tip in (
                ("Delete Folder", "Q", self.trigger_folder_delete, "Delete this folder when I.O.T closes (Q again or "
                                                                   "Undo keeps it)"),
                ("Downscale", "D", lambda: self.run_resizer("down"), "Shrink this folder's images (Settings: short edge)"),
                ("Find Dupe", "F", self.trigger_duplicate_hash_scan, "Find duplicates of the base image (98%+ match)"),
                ("Group", "G", None, ""),
                ("Rename Folder", "R", self.trigger_active_folder_rename, "Rename the folder on screen"),
                ("Upscale", "U", lambda: self.run_resizer("up"), "Enlarge this folder's images (Settings: short edge)"),
                ("Zoom", "Hover + Hold Z", None, "Hold Z with the pointer over a picture for a full-screen view")):
            if text == "Group":
                self.group_button = GroupButton("Group · G")
                self.group_button.setObjectName("tool")
                self.group_button.setToolTip("Group mode: click any pictures to pick them, then Enter copies them into "
                                             "a new folder. G or Esc leaves.")
                self.group_button.clicked.connect(lambda _=False: self.toggle_group_mode())
                self.group_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
                self.group_button.setMinimumWidth(
                    QFontMetrics(self.group_button.font()).horizontalAdvance("Group · 999") + 22)
                row.addWidget(self.group_button)
                continue
            widget = button(f"{text} · {key}", "tool" if command else "toolstatic", tip, command)
            self._tool_texts.append((widget, f"{text} · {key}", "Zoom · Hold Z" if command is None else text))
            if key not in tip:
                widget.setToolTip(f"{tip}  ({key})")
            widget.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            if command is None:
                widget.setCursor(Qt.CursorShape.ArrowCursor)
            row.addWidget(widget)
        return box

    def _zoom_group(self):
        box, row = transparent_box(QHBoxLayout, 8)
        row.addWidget(caps("Zoom"))
        fit = button("Fit", "ghost", "As many columns as fit, with no gap at the right", self.handle_auto_snap_zoom)
        fit.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        row.addWidget(fit)
        self.size_slider = QSlider(Qt.Orientation.Horizontal)
        self.size_slider.setRange(60, 300)
        self.size_slider.setValue(self.thumb_size)
        self.size_slider.setFixedWidth(92)
        self.size_slider.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.size_slider.setCursor(Qt.CursorShape.PointingHandCursor)
        self.size_slider.valueChanged.connect(self.on_slider_change)
        row.addWidget(self.size_slider)
        self.zoom_value_label = label(f"{self.thumb_size} px", "muted")
        self.zoom_value_label.setMinimumWidth(QFontMetrics(self.font()).horizontalAdvance("300 px") + 4)
        row.addWidget(self.zoom_value_label)
        return box

    def _place_floating(self):
        if hasattr(self, "half_button"):
            self.half_button.move(0, max(0, self.grid.height() // 2 - self.half_button.height() // 2))
            self.half_button.raise_()

    def _on_theme_changed(self):
        self.theme_button.setText("☀   Light" if self.theme_name == "dark" else "☾   Dark")
        self.theme_button.setToolTip("Switch to the light theme" if self.theme_name == "dark" else
                                     "Switch to the dark theme")
        self.folder_bar.paint_icon()
        self._draw_status_dot()
        self._sync_half_button()
        self.grid.refresh_colours()
        if self.tree_popup_window is not None:
            self.tree_popup_window.close()
        if self._undo_window is not None:
            self._undo_window.refresh()
        if hasattr(self, "all_files"):
            self.refresh_folder_button()
        self.root.update()

    def toggle_theme(self):
        self.set_theme("light" if self.theme_name == "dark" else "dark")

    def set_theme(self, name):
        if name not in ("dark", "light") or name == self.theme_name:
            return
        self.theme_name = name
        set_palette(name)
        self.save_settings()

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowStateChange:
            if self.root.controls:
                self.root.controls.sync()
            self._sync_half_button()
            self.root.update()
        elif event.type() == QEvent.Type.ActivationChange and self.isActiveWindow():
            self._heal_thumbnails(retry_now=True)   # e.g. back from Explorer after a drive woke up

    def showEvent(self, event):
        super().showEvent(event)
        if FRAMELESS:
            set_round_corners(self)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit_header()
        self.root.drag_limit = self.toolbar.geometry().top() if hasattr(self, "toolbar") else 110

    def _fit_header(self):
        """Narrow windows (half-window mode): the folder box gets a row of its own under the title, and the
        tool buttons drop their key hints, so nothing is squeezed or cut off."""
        if not hasattr(self, "_folder_row"):
            return
        narrow = self.width() < 1300
        if narrow != (self._folder_row.indexOf(self.folder_bar) >= 0):
            if narrow:
                self._head.removeWidget(self.folder_bar)
                self._folder_row.addWidget(self.folder_bar)
            else:
                self._folder_row.removeWidget(self.folder_bar)
                self._head.insertWidget(self._head.indexOf(self._head_gap), self.folder_bar, 1)
            self._head_gap.setVisible(narrow)
        compact = narrow and self.width() < 1100
        for widget, full, short in self._tool_texts:
            widget.setText(short if compact else full)
        self._refresh_group_button()

    # ------------------------------------------------------------------ status line

    def _draw_status_dot(self):
        color = {"success": P["success"], "warning": P["warning"], "error": P["danger"]}.get(self._status_kind, P["faint"])
        self.status_dot.setStyleSheet(f"background: {color}; border-radius: 5px;")
        self.status_selection_label.setStyleSheet(
            f"color: {P['group'] if self.group_mode else P['accent']}; font-weight: 700;")

    def log_status(self, message, msg_type="info"):
        """Queues a status line message. msg_type: 'info', 'success', 'warning', 'error'."""
        self.status_queue.append({"text": message, "type": msg_type})
        if len(self.status_queue) > 4:
            del self.status_queue[:-4]
        if not self.status_timer.isActive():
            self._display_next_status()

    def _display_next_status(self):
        if self.status_queue:
            message = self.status_queue.pop(0)
            self._status_kind = message["type"]
            self.status_bar_text.setText(message["text"])
            self._draw_status_dot()
            self.status_timer.start(5000 if message["type"] in ("warning", "error") else 3500)
        else:
            self._status_kind = "info"
            self.status_bar_text.setText("Ready")
            self._draw_status_dot()

    def _update_status_info(self):
        if not hasattr(self, "status_info_label"):
            return
        info = self.folder_info.get(self.image_folder)
        count = len(self.all_files)
        text = f"{count} image{'s' if count != 1 else ''}"
        if info:
            text += f"  ·  {format_bytes(info['bytes'])}"
        self.status_info_label.setText(text)
        if self.group_mode:
            picked = len(self.group_picks)
            selection = f"Group mode: {picked} picked  —  Enter to copy into a folder, Esc to leave"
        elif self.first_selected_idx is not None and 0 <= self.first_selected_idx < count:
            selection = f"Range start: {short_label(self.all_files[self.first_selected_idx], 40)}  —  click the last picture"
        else:
            selection = ""
        self.status_selection_label.setText(selection)
        self._draw_status_dot()

    # ------------------------------------------------------------------ folder list / tags (popups)

    def _close_tree_popup(self):
        if self.tree_popup_window is not None:
            self.tree_popup_window.close()

    def _tree_popup_closed(self, popup):
        if self.tree_popup_window is popup:
            self.tree_popup_window = None
            self._tree_closed_at = time.monotonic()
            popup.deleteLater()

    def toggle_collapsible_tree_panel(self):
        """The folder list (see FolderListPopup)."""
        if self.tree_popup_window is not None:
            self._close_tree_popup()
            return
        if time.monotonic() - self._tree_closed_at < 0.3:   # the click that closed it landed on the folder box
            return
        self.close_tooltip()
        popup = FolderListPopup(self)
        self.tree_popup_window = popup
        popup.show()
        popup.setFocus()
        later(popup.centre_on_active)

    def _open_tag_picker_for_selector(self):
        self._close_tree_popup()
        bar = self.folder_bar
        TagPicker(self, self.image_folder, bar.name_label.mapToGlobal(QPoint(-8, bar.height() - 4))).show()

    # ------------------------------------------------------------------ right-click menu

    def create_context_menu(self):
        menu = QMenu(self)
        self.context_menu = menu

        def command(text, shortcut, callback, parent=menu):
            action = parent.addAction(text, callback)
            if shortcut:
                action.setShortcut(QKeySequence(shortcut))
                action.setShortcutVisibleInContextMenu(True)
            return action

        command("Settings…", None, self.open_settings)
        command("Undo…", "Ctrl+Z", self.show_undo_window)
        command("Open Change Log", None, self.open_change_log)
        command("Refresh", "F5", self.refresh)
        menu.addSeparator()
        command("Jump to Top", None, lambda: self.grid.scroll_to(0))
        command("Jump to Middle", None, self.handle_jump_to_middle_scroll)
        command("Jump to Bottom", None, lambda: self.grid.scroll_to(self.grid.scrollbar.maximum()))
        menu.addSeparator()
        zoom = menu.addMenu("Grid Zoom")
        command("Fit to Width", None, self.handle_auto_snap_zoom, zoom)
        zoom.addSeparator()
        for text, value in (("Large (300 px)", 300), ("Medium (180 px)", 180), ("Small (60 px)", 60)):
            command(text, None, lambda v=value: self.size_slider.setValue(v), zoom)
        menu.addSeparator()
        command("Delete Folder", "Q", self.trigger_folder_delete)
        command("Downscale Images", "D", lambda: self.run_resizer("down"))
        command("Find Duplicates", "F", self.trigger_duplicate_hash_scan)
        command("Group Select", "G", self.toggle_group_mode)
        command("Rename Folder", "R", self.trigger_active_folder_rename)
        command("Upscale Images", "U", lambda: self.run_resizer("up"))
        menu.addSeparator()
        self._menu_checks = []
        for text, var in (("Sequential Rename", self.rename_var), ("Skip Folder Creation", self.skip_folder_var),
                          ("Disable Cache Deletion", self.keep_cache_var)):
            action = menu.addAction(text)
            action.setCheckable(True)
            action.triggered.connect(lambda checked, v=var: v.set(checked))
            self._menu_checks.append((action, var))
        # The shortcuts are shown in the menu but handled by eventFilter (single keys, held Z...)
        for action in menu.actions() + zoom.actions():
            action.setShortcutContext(Qt.ShortcutContext.WidgetShortcut)

    def show_context_menu(self, global_pos):
        for action, var in self._menu_checks:
            action.setChecked(bool(var.get()))
        self.close_tooltip()
        self.context_menu.popup(global_pos)

    def contextMenuEvent(self, event):
        self.show_context_menu(event.globalPos())
        event.accept()

    # ------------------------------------------------------------------ keys

    def eventFilter(self, obj, event):
        """The single-key shortcuts. They apply while the main window (or the Zoom preview) has the keyboard,
        not in dialogs, menus or the folder list; Z also needs its release, which shortcuts can't give."""
        kind = event.type()
        if kind not in (QEvent.Type.KeyPress, QEvent.Type.KeyRelease):
            return False
        if QApplication.activePopupWidget() is not None or QApplication.activeModalWidget() is not None:
            return False
        if not isinstance(obj, QWidget) or obj.window() is not self:
            return False
        if kind == QEvent.Type.KeyPress:
            return self._key_pressed(event)
        return self._key_released(event)

    def _key_pressed(self, event):
        key, repeat = event.key(), event.isAutoRepeat()
        mods = event.modifiers() & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier |
                                    Qt.KeyboardModifier.MetaModifier)
        K = Qt.Key
        if mods == Qt.KeyboardModifier.ControlModifier and key == K.Key_Z:   # Cmd+Z on macOS
            if not repeat:
                self.undo_latest()
            return True
        if mods:
            return False
        if key == K.Key_Z:
            if not repeat:
                self.handle_z_press()
            return True
        if key in (K.Key_W, K.Key_S):     # held down: keeps going through the folders
            self.shift_active_directory_index(-1 if key == K.Key_W else 1)
            return True
        if key == K.Key_Delete or (key == K.Key_Backspace and iot_platform.IS_MAC):  # Mac "delete" is Backspace
            self.handle_keyboard_delete()
            return True
        actions = {K.Key_D: lambda: self.run_resizer("down"), K.Key_U: lambda: self.run_resizer("up"),
                   K.Key_Q: self.trigger_folder_delete, K.Key_R: self.trigger_active_folder_rename,
                   K.Key_F: self.trigger_duplicate_hash_scan, K.Key_G: self.toggle_group_mode,
                   K.Key_Return: self.copy_group_selection, K.Key_Enter: self.copy_group_selection,
                   K.Key_Escape: lambda: self.toggle_group_mode() if self.group_mode else None,
                   K.Key_F5: self.refresh}
        action = actions.get(key)
        if action is None:
            return False
        if not repeat:
            later(action)   # after the key event: dialogs opened from here then get the keyboard cleanly
        return True

    def _key_released(self, event):
        if event.key() == Qt.Key.Key_Z and not event.isAutoRepeat():
            self.handle_z_release()
            return True
        return False

    # ------------------------------------------------------------------ folder loading / navigation

    def load_directory_target_sequence(self, target_path_directory, keep_place=False):
        """Loads a folder into the grid. keep_place: stay at the same row (the same folder, reloaded)."""
        anchor = self._first_visible_index() if keep_place else 0
        self.active_duplicate_filename = None
        self.image_folder = target_path_directory
        self.output_base = target_path_directory
        self.first_selected_idx = None
        self.group_picks = {}
        self._refresh_group_button()
        self.close_tooltip()
        self.last_folder_name = os.path.basename(target_path_directory.rstrip("\\/")) or "Sequence"
        self.all_files = iot_utils.list_images(target_path_directory, self.valid_extensions)
        self._rebuild_file_index()
        self.refresh_folder_button()
        self.grid.setFocus()
        if self._startup_done:
            self.render_grid()
            if anchor:
                self._scroll_row_to_top(min(anchor, max(0, len(self.all_files) - 1)) // self.grid.cols)

    def refresh(self):
        """F5 / Refresh: picks up changes made outside I.O.T. Re-reads the whole folder tree and the folder
        tags, then reloads the folder on screen where you were scrolled to, redrawing its thumbnails and
        retrying any that failed. If the folder is gone, moves up to the nearest one that still exists."""
        if self.busy:
            return
        self._close_tree_popup()
        self.close_tooltip()
        self._load_folder_tags()
        self.scan_entire_directory_tree()
        folder = self.image_folder
        while not os.path.isdir(folder) and self._norm(folder) != self._norm(self.parent_hub):
            parent = os.path.dirname(folder.rstrip("\\/"))
            if parent == folder:
                break
            folder = parent
        if not os.path.isdir(folder):
            folder = self.parent_hub
        if folder != self.image_folder:
            self.load_directory_target_sequence(folder)
            self.log_status(f"Refreshed: the folder was no longer there, so '{os.path.basename(folder)}' is shown.", "warning")
            return
        self.invalidate_folder_thumbnails(folder)
        self._thumb_retries = {key: value for key, value in self._thumb_retries.items() if key[0] != folder}
        if self._startup_done:
            self._sync_tiles_with_disk()
        else:
            self.all_files = iot_utils.list_images(folder, self.valid_extensions)
            self._rebuild_file_index()
        self.refresh_folder_button()
        self.log_status("Refreshed.", "success")

    def handle_jump_to_middle_scroll(self):
        total, view = self.grid.content_height(), self.grid.height()
        self.grid.scroll_to(0 if total <= view else total / 2.0 - view / 2.0)

    def handle_anomaly_recalculation_event(self):
        """Called after settings change."""
        self._close_tree_popup()
        self.scan_entire_directory_tree()
        self.refresh_folder_button()
        self.log_status("Settings saved and folder flags recalculated.", "success")

    def shift_active_directory_index(self, displacement_direction):
        """W/S folder cycling. Uses the image counts gathered during the tree scan, so no disk access."""
        if self.busy or not self.mapped_folders:
            return
        try:
            current_idx = self.mapped_folders.index(self.image_folder)
        except ValueError:
            current_idx = 0
        total = len(self.mapped_folders)
        for step in range(1, total + 1):
            candidate = self.mapped_folders[(current_idx + step * displacement_direction) % total]
            info = self.folder_info.get(candidate)
            if candidate == self.parent_hub or (info and info["image_count"] > 0
                                                and not self._is_pending_delete(candidate)):
                self.load_directory_target_sequence(candidate)
                return

    # ------------------------------------------------------------------ drag and drop

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        paths = [url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()]
        event.acceptProposedAction()
        later(lambda: self.open_dropped_paths(paths))

    def open_dropped_paths(self, paths):
        """A folder dropped from Explorer / OneCommander: show it. Folders outside the hub, and any
        folder with sub-folders, become the new hub."""
        folders = [os.path.abspath(path) for path in paths if os.path.isdir(path)]
        if not folders:
            self.log_status("Drop a folder to open it (files on their own aren't opened).", "warning")
            return
        if self.busy:
            self.log_status("Please wait for the current batch to finish before opening another folder.", "warning")
            return
        folder = folders[0]
        self._close_tree_popup()
        self.close_tooltip()
        # A folder with sub-folders always becomes the new hub, even one inside the current hub;
        # a plain folder inside the hub is just opened in place
        has_subfolders = bool(iot_utils.scan_folder(folder, self.valid_extensions)[0])
        if self._norm(folder) == self._norm(self.parent_hub):
            self.scan_entire_directory_tree()
            self.load_directory_target_sequence(self.parent_hub)
        elif iot_history._same_or_inside(folder, self.parent_hub) and not has_subfolders:
            # Use the scan's own spelling of the path so W/S cycling and the tree recognise it
            norm = self._norm(folder)
            known = next((info["path"] for info in self.folder_info_list if self._norm(info["path"]) == norm), None)
            if known is None:  # created since the last scan
                self.scan_entire_directory_tree()
                known = next((info["path"] for info in self.folder_info_list if self._norm(info["path"]) == norm), folder)
            self.load_directory_target_sequence(known)
        else:
            self._switch_hub(folder)
            self.load_directory_target_sequence(folder)
        note = f"  ({len(folders) - 1} other dropped folder(s) ignored)" if len(folders) > 1 else ""
        self.log_status(f"Opened '{os.path.basename(folder.rstrip(chr(92) + '/')) or folder}'.{note}", "success")

    def _switch_hub(self, folder):
        """Makes `folder` the parent hub: new folder tree, flags, recycle folder and thumbnail cache.
        The old hub's recycle folder and cache are kept until I.O.T closes, so undo still works."""
        self._thumb_loader.reset()
        self._retired_hubs.append((self.session, self.disk_cache))
        self.parent_hub = folder
        self.cache_folder = os.path.join(folder, ".thumbs_cache")
        self.session = iot_history.Session(folder)
        self.disk_cache = iot_utils.ThumbDiskCache(self.cache_folder)
        self._thumb_loader.disk_cache = self.disk_cache
        self._load_folder_tags()
        self.scan_entire_directory_tree()

    # ------------------------------------------------------------------ half-window toggle

    def _is_half_window(self):
        return not self.isMaximized()

    def _sync_half_button(self):
        if hasattr(self, "half_button"):
            # The chevron points to where the window will go: right when full size, left when halved
            self.half_button.setText("‹" if self._is_half_window() else "›")

    def toggle_half_window(self):
        """The slim tab on the left edge of the grid: the window takes the right half of the screen (room for
        Explorer on the left, for dragging folders in), and back to full size."""
        if self._is_half_window():
            self.setMinimumSize(980, 560)
            self.showMaximized()
        else:
            self.setMinimumSize(480, 560)   # the usual minimum is wider than half of most screens
            self.showNormal()
            screen = (self.screen() or QGuiApplication.primaryScreen()).availableGeometry()
            half = screen.width() // 2
            self.setGeometry(screen.x() + half, screen.y(), screen.width() - half, screen.height())
        later(self._sync_half_button, 50)

    # ------------------------------------------------------------------ grid layout and scrolling

    def _grid_resized(self):
        self._place_floating()
        self._relayout_timer.start()

    def _relayout_after_resize(self):
        width = self.grid.view_width()
        if not self._startup_done:
            if width < 200:
                return
            self._startup_done = True     # the window has its real size: first draw (fitted to the width)
            self._last_grid_width = width
            self.handle_auto_snap_zoom()
            later(self._check_leftover_recycle, 400)
            return
        if width == self._last_grid_width:
            self._update_view()   # height-only change: fill any new rows
            return
        self._last_grid_width = width
        self._relayout_keep_position()

    def grid_wheel(self, event):
        """Each wheel notch moves 10% of the visible height, scaled by Settings > Scroll speed."""
        view = self.grid.height()
        pixels = event.pixelDelta().y()
        if pixels:     # touchpads report pixels
            offset = -pixels * self.scroll_speed / 100
        else:
            offset = -event.angleDelta().y() / 120 * view * 0.10 * self.scroll_speed / 100
        if offset:
            self.grid.scroll_to(self.grid.offset() + offset)

    def _on_grid_scrolled(self):
        # The picture under the pointer is now a different one: follow it (Delete acts on what's shown there)
        if self.grid.underMouse():
            self.set_hover(self.grid.index_at(self.grid.mapFromGlobal(QCursor.pos())), QCursor.pos())
        self._schedule_view_update()

    def _schedule_view_update(self):
        if not self._view_pending:
            self._view_pending = True
            later(self._update_view)

    def on_slider_change(self, value):
        self.zoom_value_label.setText(f"{value} px")
        if value == self.thumb_size:
            return
        self.thumb_size = value
        if self._startup_done:
            self._zoom_timer.start()

    def handle_auto_snap_zoom(self):
        width = self._canvas_width()
        columns = max(1, width // (self.thumb_size + TILE_OVERHEAD))
        self.thumb_size = max(60, min(300, width // columns - TILE_OVERHEAD))
        self.size_slider.blockSignals(True)
        self.size_slider.setValue(self.thumb_size)
        self.size_slider.blockSignals(False)
        self._apply_thumb_size()

    def _canvas_width(self):
        width = self.grid.view_width()
        if width < 100:
            width = (self.screen() or QGuiApplication.primaryScreen()).availableGeometry().width() - 80
        return max(300, width)

    def _compute_columns(self):
        return max(1, self._canvas_width() // (self.thumb_size + TILE_OVERHEAD))

    def _recompute_layout(self):
        """Every file's cell position comes from its index, so the grid can be any height."""
        grid = self.grid
        grid.cols = self._compute_columns()
        grid.cell_w = self._canvas_width() / grid.cols
        metrics = QFontMetrics(grid.bold_font)
        grid.row_h = 10 + self.thumb_size + 8 + metrics.height() + 6 + 10 + TILE_GAP
        grid.rows = (len(self.all_files) + grid.cols - 1) // grid.cols
        grid.update_range()
        self._last_ranges = None

    def _first_visible_index(self):
        grid = self.grid
        if not self.all_files:
            return 0
        top_row = int(max(0, grid.offset() - grid.TOP) // max(1, grid.row_h))
        return min(len(self.all_files) - 1, max(0, top_row * grid.cols))

    def _relayout_keep_position(self):
        anchor_idx = self._first_visible_index()
        self._recompute_layout()
        self._scroll_row_to_top(anchor_idx // self.grid.cols)
        self._update_view(force=True)

    def _scroll_row_to_top(self, row):
        self.grid.scroll_to(row * self.grid.row_h)

    def render_grid(self):
        """Shows the current folder from the top. Cheap no matter how many files the folder holds."""
        self._thumb_loader.reset()
        self._drain_stale_results()
        self.close_tooltip()
        self._prune_cache()
        self._recompute_layout()
        self.grid.scroll_to(0)
        self._update_view(force=True)

    def _update_view(self, force=False):
        """Queues the thumbnails of the rows on screen, plus a screen's worth either side so scrolling shows
        images straight away (the rows on screen first)."""
        self._view_pending = False
        grid, files = self.grid, self.all_files
        n = len(files)
        grid.update()
        if n == 0:
            self._thumb_loader.wanted = frozenset()
            return
        first_row, last_row = grid.visible_rows()
        cols = grid.cols
        start, end = first_row * cols, min(n, (last_row + 1) * cols)
        screen_rows = int(grid.height() // max(1, grid.row_h)) + 1
        pf_start = max(0, first_row - screen_rows * PREFETCH_SCREENS) * cols
        pf_end = min(n, (last_row + 1 + screen_rows * PREFETCH_SCREENS) * cols)
        ranges = (start, end, pf_start, pf_end, n, self._thumb_px())
        if not force and ranges == self._last_ranges:
            return
        self._last_ranges = ranges
        self._thumb_loader.wanted = frozenset(files[pf_start:pf_end])
        order = files[pf_start:start] + files[end:pf_end][::-1] + files[start:end][::-1]
        self._request_thumbnails(order)

    def tile_state(self, idx, name):
        """How a tile is marked: 'group' (picked), 'range' (first of a range), 'dup' (found duplicate) or None."""
        if name in self.group_picks:
            return "group"
        if self.first_selected_idx is not None and idx == self.first_selected_idx:
            return "range"
        if name == self.active_duplicate_filename:
            return "dup"
        return None

    def _restyle_index(self, idx):
        self.grid.update_index(idx)

    def _set_selection(self, new_idx):
        old_idx = self.first_selected_idx
        self.first_selected_idx = new_idx
        self._restyle_index(old_idx)
        self._restyle_index(new_idx)
        self._update_status_info()

    def _set_duplicate_highlight(self, filename):
        old = self.active_duplicate_filename
        self.active_duplicate_filename = filename
        for name in (old, filename):
            self._restyle_index(self.file_index.get(name) if name else None)

    def _rebuild_file_index(self):
        self.file_index = {name: idx for idx, name in enumerate(self.all_files)}

    def _apply_thumb_size(self):
        self.zoom_value_label.setText(f"{self.thumb_size} px")
        self._thumb_loader.reset()
        self._drain_stale_results()
        self._prune_cache()
        self._relayout_keep_position()
        self._save_soon.start()

    # ------------------------------------------------------------------ thumbnails

    def _thumb_px(self):
        """Pixels to decode a thumbnail at: the zoom size on this screen (sharp at 150% scaling), up to the
        disk cache's 300 px copies."""
        ratio = self.devicePixelRatioF() if hasattr(self, "grid") else 1.0
        return min(iot_utils.ThumbDiskCache.BASE_SIZE, math.ceil(self.thumb_size * ratio))

    def thumb_pixmap(self, name):
        """(pixmap or None, failed) for a picture in the folder on screen."""
        key = (self.image_folder, name, self._thumb_px())
        pixmap = self.image_cache.get(key)
        if pixmap is not None:
            self.image_cache.move_to_end(key)
        return pixmap, (self.image_folder, name) in self._failed_thumbs

    def _cache_capacity(self):
        px = self._thumb_px()
        return max(200, THUMB_CACHE_BYTES // (px * px * 4))

    def _cache_put(self, key, pixmap):
        self.image_cache[key] = pixmap
        self.image_cache.move_to_end(key)
        capacity = self._cache_capacity()
        while len(self.image_cache) > capacity:
            self.image_cache.popitem(last=False)

    def _prune_cache(self):
        px = self._thumb_px()
        for key in [k for k in self.image_cache if k[2] != px]:
            del self.image_cache[key]

    def _drain_stale_results(self):
        try:
            while True:
                self._thumb_queue.get_nowait()
        except queue.Empty:
            pass

    def _request_thumbnails(self, names):
        folder, px = self.image_folder, self._thumb_px()
        now = time.monotonic()
        keys = [(folder, name, px) for name in names
                if (folder, name, px) not in self.image_cache and (folder, name) not in self._failed_thumbs
                and self._thumb_retries.get((folder, name), (0, 0))[0] <= now]
        self._thumb_loader.request(keys)

    def _heal_thumbnails(self, retry_now=False):
        """Asks again for on-screen pictures still without a thumbnail: ones whose read failed for now (once
        their retry time comes, or straight away when the window is reactivated), or that got stuck any other
        way. Only pictures on screen, so it costs next to nothing."""
        if not self._startup_done or not self.all_files:
            return
        folder, px = self.image_folder, self._thumb_px()
        if retry_now:
            for key, (_, attempts) in list(self._thumb_retries.items()):
                self._thumb_retries[key] = (0, attempts)
        first, last = self.grid.visible_rows()
        cols = self.grid.cols
        names = [name for name in self.all_files[first * cols:(last + 1) * cols]
                 if (folder, name, px) not in self.image_cache and (folder, name) not in self._failed_thumbs
                 and not self._thumb_loader.is_busy((folder, name, px))]
        if names:
            self._request_thumbnails(names[::-1])  # nearest the top of the screen last = loaded first

    def _drain_thumb_queue(self):
        """Main thread: turns finished PIL thumbnails into pixmaps within a small time budget per tick."""
        deadline = time.perf_counter() + 0.012
        changed = False
        px = self._thumb_px()
        try:
            while time.perf_counter() < deadline:
                generation, folder, filename, size, thumb, dims, failure = self._thumb_queue.get_nowait()
                if generation != self._thumb_loader.generation or folder != self.image_folder or size != px:
                    continue
                if thumb is None and failure == "temporary":
                    # Couldn't be read just now (e.g. the drive was asleep): keep the placeholder and try again soon
                    _, attempts = self._thumb_retries.get((folder, filename), (0, 0))
                    if attempts + 1 < THUMB_RETRY_LIMIT:
                        self._thumb_retries[(folder, filename)] = (time.monotonic() + THUMB_RETRY_SECONDS, attempts + 1)
                        continue
                self._thumb_retries.pop((folder, filename), None)
                if thumb is None:
                    self._failed_thumbs.add((folder, filename))
                else:
                    pixmap = pil_to_pixmap(thumb)
                    # Shown at the zoom size (or the picture's own size, if smaller), sharp on scaled screens
                    long_edge = min(self.thumb_size, max(dims)) if dims else self.thumb_size
                    pixmap.setDevicePixelRatio(max(pixmap.width(), pixmap.height()) / max(1, long_edge))
                    self._cache_put((folder, filename, size), pixmap)
                    self.dims_cache[(folder, filename)] = dims
                changed = True
        except queue.Empty:
            pass
        if changed:
            self.grid.update()

    def invalidate_folder_thumbnails(self, folder):
        """Forget in-memory thumbnails/sizes for a folder whose files changed."""
        for key in [k for k in self.image_cache if k[0] == folder]:
            del self.image_cache[key]
        for key in [k for k in self.dims_cache if k[0] == folder]:
            del self.dims_cache[key]
        self._failed_thumbs = {k for k in self._failed_thumbs if k[0] != folder}

    def _forget_names(self, folder, names):
        for name in names:
            self.dims_cache.pop((folder, name), None)
            self._failed_thumbs.discard((folder, name))
        for key in [k for k in self.image_cache if k[0] == folder and k[1] in names]:
            del self.image_cache[key]

    def _sync_tiles_with_disk(self, rename_map=None, focus_idx=None):
        """
        After files were deleted/moved/renamed: re-read the folder, carry renamed files' thumbnails
        over to their new names, and refill the grid.
        rename_map: {old_name: new_name_in_this_folder or None if it left the folder}
        focus_idx: picture whose row should end up at the top (default: keep the current top row)
        """
        folder, px = self.image_folder, self._thumb_px()
        new_files = iot_utils.list_images(folder, self.valid_extensions)
        if rename_map:
            moved = {}
            for old_name, new_name in rename_map.items():
                pixmap = self.image_cache.pop((folder, old_name, px), None)
                dims = self.dims_cache.pop((folder, old_name), None)
                self._failed_thumbs.discard((folder, old_name))
                if new_name:
                    moved[new_name] = (pixmap, dims)
            for new_name, (pixmap, dims) in moved.items():
                if pixmap is not None:
                    self._cache_put((folder, new_name, px), pixmap)
                if dims is not None:
                    self.dims_cache[(folder, new_name)] = dims
        self.all_files = new_files
        self._rebuild_file_index()
        if self.first_selected_idx is not None and self.first_selected_idx >= len(new_files):
            self.first_selected_idx = None
        self._thumb_loader.reset()
        self._drain_stale_results()
        anchor_idx = self._first_visible_index() if focus_idx is None else focus_idx
        self._recompute_layout()
        self._scroll_row_to_top(min(anchor_idx, max(0, len(new_files) - 1)) // self.grid.cols)
        self._update_view(force=True)
        self._update_status_info()

    # ------------------------------------------------------------------ hover / tooltip / preview

    def set_hover(self, idx, global_pos=None):
        grid = self.grid
        if idx is not None and idx >= len(self.all_files):
            idx = None
        if idx != grid.hover_idx:
            old, grid.hover_idx = grid.hover_idx, idx
            grid.update_index(old)
            grid.update_index(idx)
        self.hovered_filename = self.all_files[idx] if idx is not None else None
        if idx is None:
            self.tip.hide()
        elif global_pos is not None and not self.z_key_is_pressed:
            self.tip.show_at(self.hovered_filename, self.dims_cache.get((self.image_folder, self.hovered_filename)),
                             global_pos)

    def close_tooltip(self):
        if hasattr(self, "tip"):
            self.tip.hide()
        if hasattr(self, "grid"):
            self.set_hover(None)
        self.hovered_filename = None

    @property
    def currently_hovered_idx(self):
        return self.file_index.get(self.hovered_filename) if self.hovered_filename else None

    def handle_z_press(self):
        if self.z_key_is_pressed or self.hovered_filename is None:
            return
        self.z_key_is_pressed = True
        self.tip.hide()
        path = os.path.join(self.image_folder, self.hovered_filename)
        try:
            screen = self.screen() or QGuiApplication.primaryScreen()
            area, ratio = screen.geometry(), screen.devicePixelRatio()
            width, height = int(area.width() * ratio), int(area.height() * ratio)
            # The frosted backdrop is grabbed and blurred on a worker thread while the preview decodes
            frost_job = None
            if self.frost_preview:
                frost_job = self._frost_pool.submit(iot_utils.frosted_screen, width, height, P["grad_end"])
            preview, _ = iot_utils.load_preview_image(path, width, height)   # the whole screen, over the taskbar
            try:
                frost = frost_job.result(timeout=1.0) if frost_job else None
            except Exception:
                frost = None
            pixmap = pil_to_pixmap(preview)
            pixmap.setDevicePixelRatio(ratio)
            frost_pixmap = pil_to_pixmap(frost) if frost is not None else None
            self.preview_window = ZoomPreview(screen, pixmap, frost_pixmap)
            self.preview_window.show()
            self.preview_window.raise_()
        except Exception:
            # Stay "held" until Z is let go, so a failure isn't retried on every key repeat
            self._close_preview()

    def handle_z_release(self):
        self.z_key_is_pressed = False
        self._close_preview()

    def _close_preview(self):
        if self.preview_window is not None:
            self.preview_window.close()
            self.preview_window.deleteLater()
            self.preview_window = None

    # ------------------------------------------------------------------ history / undo

    time_ago = staticmethod(time_ago)

    def _record(self, kind, title, folder, steps, log_lines=(), target=None):
        record = self.history.add(kind, title, folder, steps, log_lines, target)
        self._refresh_undo_button()
        self._refresh_undo_window()
        return record

    def _stash(self, path):
        """Moves a file into this session's recycle folder. Returns the step for the undo record."""
        stash = self.session.stash_path(os.path.basename(path))
        iot_history.move_file(path, stash)
        return ("move", path, stash)

    def _refresh_undo_button(self):
        if not hasattr(self, "undo_button"):
            return
        count = sum(1 for record in self.history.records if not record.reverted)
        self.undo_button.setText(f"Undo  ·  {count}" if count else "Undo")
        self.undo_button.setEnabled(count > 0)

    def _refresh_undo_window(self):
        if self._undo_window is not None:
            self._undo_window.refresh()

    def undo_latest(self):
        record = self.history.latest_undoable()
        if record is None:
            self.log_status("Nothing to undo.", "info")
            return
        self.perform_undo(record)

    def perform_undo(self, record):
        if self.busy or record.reverted:
            return
        self.close_tooltip()
        old_folder = self.image_folder
        restored, problems, touched = self.history.undo(record)

        # A reverted folder rename moves the folder we may be looking at
        current = self.image_folder
        for step in record.steps:
            if step[0] == "tag_delete":
                self.pending_delete_folders.discard(step[1])
            if step[0] == "rename_dir":
                src, dst = step[1], step[2]
                if iot_history._same_or_inside(current, dst):
                    current = src + current[len(dst):]
                self.modified_folders = {src + p[len(dst):] if iot_history._same_or_inside(p, dst) else p
                                         for p in self.modified_folders}
                self.pending_delete_folders = {src + p[len(dst):] if iot_history._same_or_inside(p, dst) else p
                                               for p in self.pending_delete_folders}
                self._move_folder_tags(dst, src)
        for folder in touched | {record.folder}:
            self.invalidate_folder_thumbnails(folder)

        self.refresh_folders(touched | {record.folder})  # renamed or removed folders mean a full scan
        if not os.path.isdir(current):
            current = self.parent_hub
        if current != old_folder:
            self.load_directory_target_sequence(current)
        else:
            self._sync_tiles_with_disk()
            self.refresh_folder_button()

        if problems:
            self.log_status(f"Reverted with {len(problems)} problem(s): {problems[0]}", "warning")
        else:
            self.log_status(f"Reverted: {record.title}", "success")
        self._refresh_undo_button()
        self._refresh_undo_window()

    def show_undo_window(self):
        if self._undo_window is None:
            self._undo_window = dialogs.UndoWindow(self)
            self._undo_window.show()
        self._undo_window.raise_()
        self._undo_window.activateWindow()

    def open_settings(self):
        dialogs.SettingsDialog(self).exec()

    def open_change_log(self):
        if not os.path.exists(CHANGE_LOG_FILE):
            self._themed_notice("Change Log", "No changes have been logged yet.\n\nEvery delete, move, rename, resize "
                                              f"and undo will be recorded in:\n{CHANGE_LOG_FILE}")
            return
        try:
            open_with_system(CHANGE_LOG_FILE)
        except Exception as err:
            self._themed_notice("Change Log", f"Couldn't open the log:\n{err}\n\nIt's saved at:\n{CHANGE_LOG_FILE}")

    def _check_leftover_recycle(self):
        """If I.O.T didn't close normally last time, deleted files were never cleared: ask what to do."""
        leftovers = self.session.leftover_sessions()
        if not leftovers:
            return
        file_count = sum(len(files) for path in leftovers for _, _, files in os.walk(path))
        if file_count == 0:
            self.session.cleanup(include_leftovers=True)
            return
        dialog = MessageDialog(self, "Files from an earlier session",
                               f"I.O.T didn't close normally last time, so {file_count} file(s) deleted or replaced "
                               "during that session were never cleared. They're in the hidden '.iot_recycle' folder "
                               "in this hub.", buttons=("Delete Them", "Keep for Now", "Open Folder"), primary=1,
                               danger=0, kind="warn")
        dialog.exec()
        if dialog.choice == 0:
            for path in leftovers:
                shutil.rmtree(path, ignore_errors=True)
            self.log_status(f"Cleared {file_count} file(s) left over from an earlier session.", "success")
        elif dialog.choice == 2:
            try:
                open_with_system(self.session.root_dir)
            except Exception:
                pass

    # ------------------------------------------------------------------ deleting

    def handle_keyboard_delete(self):
        """Moves the hovered image to the recycle folder; the following pictures shift up into the gap."""
        if self.busy or self.hovered_filename is None:
            return
        filename_to_delete = self.hovered_filename
        idx = self.file_index.get(filename_to_delete)
        if idx is None:
            return
        folder = self.image_folder
        full_path = os.path.join(folder, filename_to_delete)
        try:
            step = self._stash(full_path)
        except OSError as err:
            self.log_status(f"Could not delete {filename_to_delete}: {err}", "error")
            return
        self._record("Delete", f"Deleted {filename_to_delete}", folder, [step], [filename_to_delete])

        if self.first_selected_idx == idx:
            self.first_selected_idx = None
        elif self.first_selected_idx is not None and idx < self.first_selected_idx:
            self.first_selected_idx -= 1
        self.all_files.pop(idx)
        self._rebuild_file_index()
        self._forget_names(folder, {filename_to_delete})
        self.refresh_folders([folder])
        grid = self.grid
        grid.rows = (len(self.all_files) + grid.cols - 1) // grid.cols
        grid.update_range()
        self._update_view(force=True)
        self.refresh_folder_button()
        # The next picture has moved under the pointer: it's the one hovered now
        self.set_hover(grid.index_at(grid.mapFromGlobal(QCursor.pos())) if grid.underMouse() else None, QCursor.pos())

    # ------------------------------------------------------------------ scrolling to a file

    def jump_to_specific_image_filename(self, filename):
        idx = self.file_index.get(filename)
        if idx is None:
            return
        self._set_selection(idx)
        self._scroll_to_index(idx)

    def _scroll_to_index(self, idx):
        grid = self.grid
        center_y = grid.TOP + (idx // grid.cols) * grid.row_h + grid.row_h / 2.0
        grid.scroll_to(center_y - grid.height() / 2.0)
        self._update_view()

    # ------------------------------------------------------------------ small themed dialogs

    def _themed_notice(self, title, text):
        dialogs.notice(self, title, text)

    def _ask_text(self, title, subtitle, field_label, default_value, confirm_text):
        """Themed single-field prompt. Returns the text, or None if cancelled."""
        self.close_tooltip()
        return dialogs.TextPrompt.ask(self, title, subtitle, field_label, default_value, confirm_text)

    def _confirm(self, title, heading, text, confirm_text, variant="primary"):
        """Themed yes / no dialog. Returns True when confirmed."""
        self.close_tooltip()
        return dialogs.confirm(self, title, f"{heading}\n\n{text}", confirm_text, danger=variant == "danger")

    def trigger_active_folder_rename(self):
        if self.busy:
            return
        if self.image_folder == self.parent_hub:
            self._themed_notice("Can't rename the hub", "The parent hub folder can't be renamed while I.O.T is working inside it.")
            return
        default_suggestion_name = os.path.basename(self.image_folder)
        if len(self.all_files) >= self.base_image:
            default_suggestion_name = os.path.splitext(self.all_files[self.base_image - 1])[0]
        elif self.all_files:
            default_suggestion_name = os.path.splitext(self.all_files[-1])[0]

        response = self._ask_text("Rename folder", f"Current name: {os.path.basename(self.image_folder)}",
                                  "New folder name", default_suggestion_name, "Rename")
        if not response or not response.strip():
            return
        clean_new_title = response.strip()
        problem = folder_name_problem(clean_new_title)
        if problem:
            self._themed_notice("Invalid name", problem)
            return
        old_path = self.image_folder
        new_path = os.path.join(os.path.dirname(old_path), clean_new_title)
        if new_path == old_path:
            return
        if os.path.lexists(new_path):
            self._themed_notice("Name already used", f"A folder called '{clean_new_title}' already exists here.")
            return
        try:
            os.rename(old_path, new_path)
        except OSError as disk_err:
            self._themed_notice("Couldn't rename folder", str(disk_err))
            return
        # Keep older undo records pointing at the right place, then record this rename
        self.history.rewrite_paths(old_path, new_path)
        self._record("Folder rename", f"Renamed folder '{os.path.basename(old_path)}' to '{clean_new_title}'",
                     os.path.dirname(old_path), [("rename_dir", old_path, new_path)],
                     [f"{os.path.basename(old_path)}  ->  {clean_new_title}"])
        self.modified_folders = {new_path + p[len(old_path):] if iot_history._same_or_inside(p, old_path) else p
                                 for p in self.modified_folders}
        self.pending_delete_folders = {new_path + p[len(old_path):] if iot_history._same_or_inside(p, old_path) else p
                                       for p in self.pending_delete_folders}
        self._move_folder_tags(old_path, new_path)
        self.modified_folders.add(new_path)
        self.scan_entire_directory_tree()
        self.load_directory_target_sequence(new_path)
        self.log_status(f"Folder renamed to '{clean_new_title}'.", "success")

    # ------------------------------------------------------------------ folder tags (right-click a folder)

    def _tags_file(self, hub=None):
        return os.path.join(hub or self.parent_hub, TAGS_FILE_NAME)

    def _tag_key(self, path, hub=None):
        """The folder's path relative to the hub ("." for the hub), so tags survive the hub being moved."""
        try:
            relative = os.path.relpath(path, hub or self.parent_hub)
        except ValueError:
            return None  # another drive: not part of this hub
        if relative == ".." or relative.startswith(".." + os.sep):
            return None
        return os.path.normcase(os.path.normpath(relative)).replace("\\", "/")

    def folder_tag(self, path):
        key = self._tag_key(path)
        return self.folder_tags.get(key) if key else None

    def _read_tags(self, hub):
        try:
            with open(self._tags_file(hub), "r", encoding="utf-8") as handle:
                saved = json.load(handle).get("tags", {})
        except (OSError, ValueError, AttributeError):
            return {}
        if not isinstance(saved, dict):
            return {}
        return {str(key): colour for key, colour in saved.items() if colour in TAG_COLORS}

    def _write_tags(self, hub, tags):
        path = self._tags_file(hub)
        try:
            if not tags:
                if os.path.exists(path):
                    os.remove(path)  # nothing tagged: don't leave an empty file behind
                return
            temp = path + ".tmp"
            with open(temp, "w", encoding="utf-8") as handle:
                json.dump({"tags": dict(sorted(tags.items()))}, handle, indent=2)
            os.replace(temp, path)
            iot_history.hide_path(path)
        except OSError as err:
            if hasattr(self, "status_bar_text"):
                self.log_status(f"Folder tags could not be saved: {err}", "warning")

    def _load_folder_tags(self):
        self.folder_tags = self._read_tags(self.parent_hub)

    def _save_folder_tags(self):
        self._write_tags(self.parent_hub, self.folder_tags)

    def set_folder_tag(self, path, colour):
        key = self._tag_key(path)
        if key is None:
            return
        if colour:
            self.folder_tags[key] = colour
        else:
            self.folder_tags.pop(key, None)
        self._save_folder_tags()
        self.refresh_folder_button()

    def _move_folder_tags(self, old_path, new_path):
        """A folder renamed in I.O.T keeps its tag, and so do the folders inside it."""
        old_key, new_key = self._tag_key(old_path), self._tag_key(new_path)
        if not old_key or not new_key or old_key == new_key:
            return
        moved = {}
        for key, colour in self.folder_tags.items():
            if key == old_key or key.startswith(old_key + "/"):
                moved[new_key + key[len(old_key):]] = colour
            else:
                moved[key] = colour
        if moved != self.folder_tags:
            self.folder_tags = moved
            self._save_folder_tags()

    def _drop_folder_tags(self, folder):
        """A deleted folder (and everything in it) loses its tags, in whichever hub it belonged to: a folder
        marked with Q may be from a hub that was left by dragging another one in."""
        hubs = [self.parent_hub] + [os.path.dirname(session.root_dir) for session, _ in self._retired_hubs]
        owners = [hub for hub in hubs if iot_history._same_or_inside(folder, hub)]
        if not owners:
            return
        hub = max(owners, key=len)  # the innermost hub containing it
        current = self._norm(hub) == self._norm(self.parent_hub)
        tags = self.folder_tags if current else self._read_tags(hub)
        key = self._tag_key(folder, hub)
        if not key:
            return
        kept = {k: v for k, v in tags.items() if not (k == key or k.startswith(key + "/"))}
        if kept != tags:
            if current:
                self.folder_tags = kept
            self._write_tags(hub, kept)

    # ------------------------------------------------------------------ delete folder (Q)

    def _is_pending_delete(self, path):
        return any(iot_history._same_or_inside(path, marked) for marked in self.pending_delete_folders)

    def trigger_folder_delete(self):
        """Q: marks the current folder for deletion. Nothing is removed until I.O.T closes, so Q again or
        Undo keeps it; until then it's crossed out in the folder list."""
        if self.busy:
            return
        folder = self.image_folder
        name = os.path.basename(folder.rstrip("\\/")) or folder
        if self._norm(folder) == self._norm(self.parent_hub):
            self._themed_notice("Can't delete the hub", "The parent hub folder can't be deleted while I.O.T is working inside it.")
            return
        if folder in self.pending_delete_folders:  # Q again: keep it after all
            self.pending_delete_folders.discard(folder)
            self.change_log.write("KEEP FOLDER", folder, ["No longer marked for deletion"])
            self.refresh_folder_button()
            self.log_status(f"'{name}' will be kept.", "success")
            return
        if self._is_pending_delete(folder):
            self.log_status("A folder above this one is already marked for deletion; open it and press Q to keep it.", "warning")
            return

        info = self.folder_info.get(folder) or {}
        images, total_bytes = info.get("total_image_count", len(self.all_files)), info.get("total_bytes", 0)
        sub_folders = sum(1 for other in self.folder_info_list
                          if other["path"] != folder and iot_history._same_or_inside(other["path"], folder))
        contents = f"{images} image{'s' if images != 1 else ''} ({format_bytes(total_bytes)})"
        if sub_folders:
            contents += f" and {sub_folders} sub-folder{'s' if sub_folders != 1 else ''}"
        if not self._confirm("Delete folder", f"Delete '{name}'?",
                             f"The folder and everything in it, {contents}, will be permanently deleted when I.O.T "
                             "closes. Until then it stays, crossed out in the folder list; press Q again or Undo "
                             f"({UNDO_KEYS}) to keep it.", "Delete", "danger"):
            return
        self.pending_delete_folders.add(folder)
        self._record("Folder delete", f"Marked folder '{name}' for deletion", os.path.dirname(folder),
                     [("tag_delete", folder)], [f"{name}: {contents}, deleted when I.O.T closes"])
        self.refresh_folder_button()
        self.log_status(f"'{name}' will be deleted when I.O.T closes  (Q again or {UNDO_KEYS} keeps it)", "warning")

    def _delete_pending_folders(self):
        """On closing: permanently deletes the folders marked with Q (parents before their sub-folders)."""
        for folder in sorted(self.pending_delete_folders, key=len):
            if not os.path.isdir(folder):
                continue
            try:
                shutil.rmtree(folder)
                self.change_log.write("FOLDER DELETED", folder, ["Deleted when I.O.T closed"])
                self._drop_folder_tags(folder)
            except OSError as err:
                self.change_log.write("FOLDER DELETE FAILED", folder, [str(err)])

    # ------------------------------------------------------------------ rename a branch (folder list: Hover + Ctrl)

    def rename_folder_branch(self, folders):
        """
        Every image in these folders is renamed after its own folder, with the usual sequential rule
        ('<folder> - 01', '- 02'... in the current order) and left in place. Folders already named that way,
        and ones whose name can't be used as a file name, are skipped. One confirmation, one Undo.
        """
        if self.busy:
            return
        jobs = []
        for folder in folders:
            names = iot_utils.list_images(folder, self.valid_extensions)
            tag = os.path.basename(folder.rstrip("\\/"))
            if not names or not tag or folder_name_problem(tag):
                continue
            padding = max(2, len(str(len(names))))
            wanted = [f"{tag} - {str(i).zfill(padding)}{os.path.splitext(name)[1]}" for i, name in enumerate(names, 1)]
            if wanted != names:
                jobs.append((folder, names, tag, wanted[0]))
        if not jobs:
            self.log_status("Nothing to rename: the images there are already named after their folders.", "info")
            return

        total = sum(len(names) for _, names, _, _ in jobs)
        top_name = os.path.basename(folders[0].rstrip("\\/")) or folders[0]
        examples = ",  ".join(f"'{first}'" for _, _, _, first in jobs[:3]) + ("  ..." if len(jobs) > 3 else "")
        in_folders = f" in {len(jobs)} folders" if len(jobs) > 1 else ""
        if not self._confirm("Rename images", f"Rename {total} image{'s' if total != 1 else ''}{in_folders}?",
                             f"Images in '{top_name}'{' and its sub-folders' if len(folders) > 1 else ''} will be renamed "
                             f"after their own folder, in their current order, and left in place: {examples}. "
                             f"Undo ({UNDO_KEYS}) puts the old names back.", "Rename"):
            return

        steps, errors, changed, current_map = [], [], [], None
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            for folder, names, tag, _ in jobs:
                rename_map, folder_errors, folder_steps = self._move_batch(names, folder, tag, True, source_dir=folder)
                steps.extend(folder_steps)
                errors.extend(folder_errors)
                if folder_steps:
                    changed.append(folder)
                if folder == self.image_folder:
                    current_map = rename_map
        finally:
            QApplication.restoreOverrideCursor()

        renamed = len([s for s in steps if s[0] == "move"])
        if steps:
            scope = f" in '{top_name}' and its sub-folders" if len(changed) > 1 else f" in '{top_name}'"
            log_lines = [f"{os.path.basename(folder)}: renamed after the folder" for folder in changed[:20]]
            if errors:
                log_lines.append(f"{len(errors)} file(s) could not be renamed: " + "; ".join(errors[:5]))
            self._record("Rename", f"Renamed {renamed} images after their folders{scope}", folders[0], steps, log_lines)
        for folder in changed:
            self.modified_folders.add(folder)
            if folder != self.image_folder:
                self.invalidate_folder_thumbnails(folder)
        if current_map is not None:
            self.first_selected_idx = None
            self._sync_tiles_with_disk(current_map)
        self.refresh_folders(changed)
        self.refresh_folder_button()
        if errors:
            self.log_status(f"Renamed {renamed} images; {len(errors)} could not be renamed: {errors[0]}", "error")
        else:
            self.log_status(f"Renamed {renamed} images after their folders  ({UNDO_KEYS} to undo)", "success")

    # ------------------------------------------------------------------ resizer

    def run_resizer(self, mode, folders=None):
        """folders: resize these folders' images (a folder list branch); default: the folder on screen."""
        if not self.busy:
            self.close_tooltip()
            dialogs.run_resizer(self, mode, folders)

    # ------------------------------------------------------------------ duplicate scan

    def _get_hash(self, path):
        try:
            stat = os.stat(path)
        except OSError:
            return None
        key = (path, stat.st_mtime_ns, stat.st_size)
        if key in self._hash_cache:
            return self._hash_cache[key]
        value = iot_utils.generate_image_difference_hash(path)
        self._hash_cache[key] = value
        return value

    def trigger_duplicate_hash_scan(self):
        """Scans the active folder for visual twins (98%+ dHash match) of the base image, in parallel."""
        if self.busy:
            return
        n = self.base_image
        if len(self.all_files) < n:
            self._themed_notice("Find duplicates", f"Duplicates are found by comparing against the {iot_utils.ordinal(n)} "
                                                   f"image (Settings > Base image), so the folder needs at least {n} images.")
            return
        self.close_tooltip()
        folder = self.image_folder
        files = list(self.all_files)
        baseline_filename = files[n - 1]
        baseline_full_path = os.path.join(folder, baseline_filename)
        progress = dialogs.ProgressDialog(self, "Finding duplicates",
                                          f"Comparing every image with the {iot_utils.ordinal(n)} image (the base "
                                          f"image):\n{baseline_filename}", len(files))
        progress.show()
        self.busy = True
        executor = ThreadPoolExecutor(max_workers=max(2, min(8, os.cpu_count() or 4)), thread_name_prefix="iot-hash")
        paths = [os.path.join(folder, name) for name in files]
        futures = [executor.submit(self._get_hash, path) for path in paths]
        executor.shutdown(wait=False)
        timer = QTimer(progress, interval=60)

        def poll():
            done_count = sum(1 for future in futures if future.done())
            progress.set_value(done_count)
            if done_count < len(futures):
                return
            timer.stop()
            progress.finish()
            self.busy = False
            base_hash = futures[n - 1].result()
            if base_hash is None:
                self.log_status(f"Could not read the baseline image '{baseline_filename}'.", "error")
                return
            matched_twin_paths = [baseline_full_path]
            for path, future in zip(paths, futures):
                if path == baseline_full_path:
                    continue
                comp_hash = future.result()
                if comp_hash is not None and iot_utils.hamming_distance(base_hash, comp_hash) <= 1:
                    matched_twin_paths.append(path)
                    if len(matched_twin_paths) >= 5:
                        break
            if len(matched_twin_paths) <= 1:
                self.log_status("No duplicates (98%+ match) found in this folder.", "info")
                return
            self.display_side_by_side_duplicates_window(matched_twin_paths)

        timer.timeout.connect(poll)
        timer.start()

    def display_side_by_side_duplicates_window(self, paths):
        self.duplicates_window = dialogs.DuplicatesViewer(self, paths)
        self.duplicates_window.show()
        self.duplicates_window.activateWindow()

    # ------------------------------------------------------------------ group mode (G)

    def toggle_group_mode(self):
        if self.busy and not self.group_mode:
            return
        self.group_mode = not self.group_mode
        picked_before = list(self.group_picks)
        self.group_picks = {}
        if self.group_mode:
            self._set_selection(None)  # a half-made range selection doesn't carry into group mode
            self.log_status("Group mode: click pictures to pick them, Enter copies them into a folder.", "info")
        self.group_button.set_active(self.group_mode)
        for name in picked_before:
            self._restyle_index(self.file_index.get(name))
        self._refresh_group_button()
        self._update_status_info()

    def _refresh_group_button(self):
        if not hasattr(self, "group_button"):
            return
        count = len(self.group_picks)
        compact = self.width() < 1100 and not self.isMaximized()
        self.group_button.setText(f"Group · {count}" if self.group_mode and count else "Group" if compact else "Group · G")

    def _toggle_group_pick(self, idx):
        name = self.all_files[idx]
        if name in self.group_picks:
            del self.group_picks[name]
        else:
            self.group_picks[name] = None
        self._restyle_index(idx)
        self._refresh_group_button()
        self._update_status_info()

    def copy_group_selection(self):
        """Enter in group mode: copy the picked pictures (names unchanged) into a new sub-folder."""
        if not self.group_mode or self.busy:
            return
        names = [name for name in self.group_picks if name in self.file_index]
        if not names:
            self.log_status("Nothing picked yet: click some pictures first (Esc leaves group mode).", "warning")
            return
        folder_name = os.path.basename(self.image_folder.rstrip("\\/")) or "Group"
        summary = f"{len(names)} picture{'s' if len(names) != 1 else ''}, copied with their names unchanged"
        tag = self._ask_text(f"Copy {len(names)} pictures into a folder", summary, "Folder name",
                             f"{folder_name} - Selection", "Copy")
        tag = (tag or "").strip()
        if not tag:
            return  # cancelled: stay in group mode with the picks intact
        problem = folder_name_problem(tag)
        if problem:
            self._themed_notice("Invalid name", problem)
            return

        source_dir = self.image_folder
        target_dir = os.path.join(self.output_base, tag)
        steps, errors = [], []
        try:
            if not os.path.isdir(target_dir):
                os.makedirs(target_dir)
                steps.append(("mkdir", target_dir))
        except OSError as err:
            self._themed_notice("Couldn't create folder", str(err))
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            for name in names:
                destination = os.path.join(target_dir, name)
                if os.path.exists(destination):
                    destination = self._unique_dupe_path(target_dir, name)
                try:
                    shutil.copy2(os.path.join(source_dir, name), destination)
                    steps.append(("created", destination))  # undo deletes the copy
                except (OSError, shutil.Error) as err:
                    errors.append(f"{name}: {err}")
        finally:
            QApplication.restoreOverrideCursor()

        copied = len(names) - len(errors)
        log_lines = [iot_history.describe_range(names)]
        if errors:
            log_lines.append(f"{len(errors)} file(s) could not be copied: " + "; ".join(errors[:5]))
        self._record("Copy", f"Copied {copied} files into '{tag}'", source_dir, steps, log_lines, target=target_dir)

        self.toggle_group_mode()  # done: leave group mode
        self.refresh_folders([source_dir, target_dir])  # a new folder means a full scan
        self.refresh_folder_button()
        if errors:
            self.log_status(f"Copied {copied} files; {len(errors)} could not be copied: {errors[0]}", "error")
        else:
            self.log_status(f"Copied {copied} files to '{tag}'  ({UNDO_KEYS} to undo)", "success")

    # ------------------------------------------------------------------ click handling / batch moves

    def handle_image_click(self, clicked_idx, mode="click"):
        """mode: 'click' (range into folder), 'ctrl' (range renamed in place), 'alt' (range delete)."""
        if self.busy:
            return
        if self.group_mode:  # every kind of click just picks / unpicks in group mode
            self._toggle_group_pick(clicked_idx)
            return

        if mode == "alt":
            if self.first_selected_idx is None:
                self._set_selection(clicked_idx)
                return
            start_range = min(self.first_selected_idx, clicked_idx)
            end_range = max(self.first_selected_idx, clicked_idx)
            files_to_delete = self.all_files[start_range:end_range + 1]
            self.close_tooltip()
            folder = self.image_folder
            steps, failed = [], 0
            for filename in files_to_delete:
                try:
                    steps.append(self._stash(os.path.join(folder, filename)))
                except OSError:
                    failed += 1
            deleted = [os.path.basename(step[1]) for step in steps]
            self._record("Delete", f"Deleted {len(deleted)} files ({short_label(deleted[0], 30)} … {short_label(deleted[-1], 30)})"
                         if len(deleted) > 1 else f"Deleted {deleted[0]}" if deleted else "Deleted nothing",
                         folder, steps, [iot_history.describe_range(deleted)])
            self._forget_names(folder, set(deleted))
            self.first_selected_idx = None
            self._sync_tiles_with_disk(focus_idx=start_range)  # the first picture after the range moves up to the top row
            self.refresh_folders([folder])
            self.refresh_folder_button()
            if failed:
                self.log_status(f"Deleted {len(deleted)} files; {failed} could not be deleted.", "error")
            else:
                self.log_status(f"Deleted {len(deleted)} files  ({UNDO_KEYS} to undo)", "success")
            return

        # Plain click and Ctrl+click both pick a two-picture range the same way: the first click marks
        # the start, the second click marks the end. Ctrl on the second click renames that range in
        # place (sequential names, no folder, nothing moved) whatever the checkboxes say.
        if self.first_selected_idx is None:
            self._set_selection(clicked_idx)
            return
        if clicked_idx <= self.first_selected_idx:
            self._set_selection(clicked_idx if clicked_idx < self.first_selected_idx else None)
            return
        start_idx = self.first_selected_idx
        end_idx = clicked_idx
        if mode != "ctrl" and self.skip_folder_var.get() and not self.rename_var.get():
            self._set_selection(None)
            self.log_status("Nothing to do: Skip Folder Creation keeps the pictures here and Sequential Rename is off, "
                            "so their names wouldn't change.", "warning")
            return
        files_to_move = self.all_files[start_idx:end_idx + 1]
        if mode == "ctrl":
            # Instant: no name box, uses the current suggested name (which then counts up)
            should_rename = True
            is_skip_folder = True
            folder_tag = (self.last_folder_name or "Sequence").strip()
        else:
            should_rename = self.rename_var.get()
            is_skip_folder = self.skip_folder_var.get()
            range_summary = f"{self.all_files[start_idx]}  →  {self.all_files[end_idx]}"
            folder_tag = self.ask_custom_dark_folder_dialog((self.last_folder_name or "Sequence").strip(),
                                                            len(files_to_move), range_summary, is_skip_folder)

        self.close_tooltip()
        folder_tag = (folder_tag or "").strip()
        if not folder_tag:
            self._set_selection(None)
            return
        problem = folder_name_problem(folder_tag)
        if problem:
            self._themed_notice("Invalid name", problem)
            self._set_selection(None)
            return

        target_dir = self.output_base if is_skip_folder else os.path.join(self.output_base, folder_tag)
        created_dir = False
        try:
            if not is_skip_folder and not os.path.isdir(target_dir):
                os.makedirs(target_dir)
                created_dir = True
        except OSError as err:
            self._themed_notice("Couldn't create folder", str(err))
            self._set_selection(None)
            return

        self.modified_folders.add(self.image_folder)
        num_match = re.search(r'(\d+)$', folder_tag)
        if num_match:
            raw_digits = num_match.group(1)
            self.last_folder_name = folder_tag[:-len(raw_digits)] + str(int(raw_digits) + 1).zfill(len(raw_digits))
        else:
            self.last_folder_name = folder_tag

        source_dir = self.image_folder
        rename_map, errors, steps = self._move_batch(files_to_move, target_dir, folder_tag, should_rename)
        if created_dir:
            steps.insert(0, ("mkdir", target_dir))
        moves = [s for s in steps if s[0] == "move"]
        log_lines = [iot_history.describe_range(os.path.basename(s[1]) for s in moves)]
        if moves and should_rename:
            log_lines.append("renamed to:  " + iot_history.describe_range(os.path.basename(s[2]) for s in moves))
        if errors:
            log_lines.append(f"{len(errors)} file(s) could not be moved: " + "; ".join(errors[:5]))
        if is_skip_folder:
            self._record("Rename", f"Renamed {len(files_to_move)} files as '{folder_tag} - ##'", source_dir, steps, log_lines)
        else:
            self._record("Move", f"Moved {len(files_to_move)} files into '{folder_tag}'", source_dir, steps, log_lines,
                         target=target_dir)
        self.first_selected_idx = None
        self._sync_tiles_with_disk(rename_map)
        if not is_skip_folder:  # files left this folder: start again from the top
            self.grid.scroll_to(0)
            self._update_view(force=True)
        self.refresh_folders([source_dir, target_dir])  # a new folder means a full scan
        self.refresh_folder_button()
        if errors:
            self.log_status(f"{len(errors)} file(s) could not be moved: {errors[0]}", "error")
        elif is_skip_folder:
            self.log_status(f"Renamed {len(files_to_move)} files as '{folder_tag} - ##'.", "success")
        else:
            self.log_status(f"Moved {len(files_to_move)} files to '{folder_tag}'.", "success")

    def _move_batch(self, files, target_dir, tag, should_rename, source_dir=None):
        """
        Two-phase move: everything goes to a temporary name first, then to its final name, so files
        inside the batch can't collide with each other when renaming in place.
        source_dir: the folder the files are in (default: the one on screen).
        Returns ({old_name: new_name_if_still_in_this_folder_else_None}, [errors], [undo steps]).
        """
        source_dir = source_dir or self.image_folder
        same_dir = os.path.normcase(os.path.abspath(target_dir)) == os.path.normcase(os.path.abspath(source_dir))
        padding_digits = max(2, len(str(len(files))))
        rename_map, errors, staged, steps = {}, [], [], []

        for sequence_idx, filename in enumerate(files, 1):
            ext = os.path.splitext(filename)[1]
            final_name = f"{tag} - {str(sequence_idx).zfill(padding_digits)}{ext}" if should_rename else filename
            if same_dir and final_name == filename:
                continue  # already correct
            temp_path = os.path.join(target_dir, f".iot_stage_{os.getpid()}_{sequence_idx}{ext}")
            try:
                shutil.move(os.path.join(source_dir, filename), temp_path)
                staged.append((filename, temp_path, final_name))
            except (OSError, shutil.Error) as err:
                errors.append(f"{filename}: {err}")

        for filename, temp_path, final_name in staged:
            destination = os.path.join(target_dir, final_name)
            if os.path.exists(destination):
                destination = self._unique_dupe_path(target_dir, final_name)
            try:
                os.rename(temp_path, destination)
                rename_map[filename] = os.path.basename(destination) if same_dir else None
                steps.append(("move", os.path.join(source_dir, filename), destination))
            except OSError as err:
                errors.append(f"{filename}: {err}")
                try:
                    shutil.move(temp_path, os.path.join(source_dir, filename))  # put it back
                except (OSError, shutil.Error):
                    pass
        return rename_map, errors, steps

    @staticmethod
    def _unique_dupe_path(folder, filename):
        base, ext = os.path.splitext(filename)
        candidate = os.path.join(folder, f"dupe_{base}{ext}")
        counter = 2
        while os.path.exists(candidate):
            candidate = os.path.join(folder, f"dupe{counter}_{base}{ext}")
            counter += 1
        return candidate

    def ask_custom_dark_folder_dialog(self, default_value, count, summary, is_skip_folder):
        """Returns the entered name as a string, or None if cancelled."""
        if is_skip_folder:
            title, confirm_text = f"Rename {count} files", "Rename"
        else:
            title, confirm_text = f"Move {count} files into a folder", "Move"
        return self._ask_text(title, summary, "Name", default_value, confirm_text)

    # ------------------------------------------------------------------ shutdown

    def check_and_cleanup_cache(self):
        for cache in [self.disk_cache] + [cache for _, cache in self._retired_hubs]:
            if self.keep_cache_var.get():
                cache.trim()
            else:
                cache.delete()

    def closeEvent(self, event):
        """The window's close button: clean up (recycle folder, folders marked with Q, and the cache unless
        it's being kept)."""
        if self.busy:
            self.log_status("Please wait for the current batch to finish (or cancel it) before closing.", "warning")
            event.ignore()
            return
        self.save_settings()
        self._thumb_loader.stop()
        self._delete_pending_folders()
        for session in [self.session] + [session for session, _ in self._retired_hubs]:
            session.cleanup()           # permanently removes this session's deleted files
        self.check_and_cleanup_cache()
        self.tip.close()
        self._close_preview()
        QApplication.instance().removeEventFilter(self)
        event.accept()
        QApplication.instance().quit()   # the Undo window may still be open

    def handle_manual_exit_cleanup(self):   # old name (Tk version)
        self.close()


DarkGridImageSorter = ImageOrganizationToolkit   # the Tk version's class name


# ==================================================================== start-up

def install_crash_logging():
    """Errors go to iot_crash.log (with the system details) and are shown in a dialog instead of vanishing."""
    import faulthandler
    try:
        faulthandler.enable(open(CRASH_LOG_FILE, "a", encoding="utf-8"))   # hard crashes (inside Qt) too
    except Exception:
        pass

    def hook(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            return
        log_crash("error", exc_type, exc, tb)
        try:
            if QApplication.instance():
                text = "".join(traceback.format_exception(exc_type, exc, tb))
                dialog = MessageDialog(None, f"{APP_NAME} error",
                                       f"Something went wrong. Details were saved to:\n{CRASH_LOG_FILE}\n\n"
                                       + text.strip().splitlines()[-1],
                                       buttons=("Copy details", "OK"), primary=1, kind="error")
                dialog.exec()
                if dialog.choice == 0:
                    QApplication.clipboard().setText(text)
        except Exception:
            pass

    sys.excepthook = hook
    threading.excepthook = lambda args: log_crash("error (thread)", args.exc_type, args.exc_value, args.exc_traceback)
    return hook


def create_app():
    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setStyle("Fusion")
    font = QFont("Segoe UI", 10) if sys.platform == "win32" else QFont(app.font().family(), 10)
    font.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
    app.setFont(font)
    app.setStyleSheet(iot_qt.build_qss())
    return app


def main():
    hook = install_crash_logging()
    try:
        app = create_app()
        window = ImageOrganizationToolkit()
        window.showMaximized()
        code = app.exec()
        # Everything was saved and cleaned up when the window closed. Leave straight away: Python's own
        # shutdown, tearing down Qt objects while worker threads are still alive, can occasionally crash
        # (seen once in testing), which would leave a misleading entry in iot_crash.log.
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(code)
    except SystemExit:
        raise
    except BaseException:
        hook(*sys.exc_info())
        print(f"\nDetails were saved to {CRASH_LOG_FILE}")
        if not QApplication.instance():
            _pause()
        sys.exit(1)


if __name__ == "__main__":
    main()
