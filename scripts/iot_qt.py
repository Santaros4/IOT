"""
I.O.T's look in Qt (PySide6): the same visual style as V.O.T - a frameless window on a diagonal gradient,
with see-through rounded panels - plus the small custom widgets the main window and dialogs share.

Palettes, the style sheet and most widgets are V.O.T's, extended with what I.O.T needs (thumbnail tiles,
the folder list, tag colours, key-caps, the zoom slider).
"""
import sys

from PySide6.QtCore import QPoint, QPointF, QRect, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (QColor, QConicalGradient, QFont, QFontDatabase, QFontMetrics, QGuiApplication, QImage,
                           QLinearGradient, QPainter, QPainterPath, QPen, QPixmap)
from PySide6.QtWidgets import (QApplication, QDialog, QFrame, QGraphicsDropShadowEffect, QGridLayout, QHBoxLayout,
                               QLabel, QLayout, QPushButton, QSizePolicy, QVBoxLayout, QWidget)

FRAMELESS = True          # False = use the normal title bar

# ==================================================================== palettes

PALETTES = {
    "dark": {
        "grad_start": "#000000", "grad_end": "#0a1f5c",
        "text": "#e8ecf8", "muted": "#8ea0c8", "field": "#aab8da", "faint": "#5d6b92",
        "accent": "#4d8dff", "accent_strong": "#2f6bff", "accent_soft": "rgba(77,141,255,60)",
        "success": "#3ddc97", "danger": "#ff6b7a", "warning": "#f5b54a", "group": "#b48cff",
        "card_bg": "rgba(255,255,255,12)", "card_border": "rgba(255,255,255,26)",
        "row_bg": "rgba(6,14,40,170)", "row_border": "rgba(120,150,255,38)", "row_hover": "rgba(120,160,255,110)",
        "info_bg": "rgba(0,0,0,70)", "info_border": "rgba(140,170,255,30)",
        "input_bg": "rgba(0,0,0,120)", "input_border": "rgba(140,170,255,50)",
        "btn_bg": "rgba(255,255,255,20)", "btn_border": "rgba(255,255,255,34)", "btn_hover": "rgba(255,255,255,36)",
        "btn_pressed": "rgba(255,255,255,14)", "btn_disabled_text": "rgba(232,236,248,80)",
        "btn_disabled_bg": "rgba(255,255,255,8)",
        "bar_bg": "rgba(0,0,0,140)", "bar_border": "rgba(140,170,255,45)",
        "scroll": "rgba(255,255,255,45)", "scroll_hover": "rgba(255,255,255,80)",
        "check_border": "rgba(140,170,255,80)", "check_bg": "rgba(0,0,0,120)",
        "tooltip_bg": "#0d1838", "tooltip_border": "#2f4a9a", "menu_bg": "#0b1430", "menu_border": "#2f4a9a",
        "win_btn": "#c8d2ee", "edge": (120, 150, 255, 60), "thumb_bg": "#050b1e",
        "danger_bg": "rgba(255,90,110,40)", "danger_border": "rgba(255,107,122,90)",
        "danger_hover": "rgba(255,90,110,80)", "shadow": (0, 0, 0, 160), "divider": (255, 255, 255, 18),
        "keycap": "rgba(255,255,255,26)", "row_active": "rgba(77,141,255,70)",
        "tag_red": "#f05252", "tag_orange": "#f59e0b", "tag_yellow": "#facc15", "tag_green": "#22c55e",
        "tag_blue": "#3b82f6", "tag_purple": "#a855f7", "tag_grey": "#9ca3af",
    },
    # Light: the same diagonal, from white to a soft periwinkle, with frosted white panels and the same blues
    "light": {
        "grad_start": "#ffffff", "grad_end": "#c3d3ff",
        "text": "#0f1b3d", "muted": "#56668f", "field": "#3f507c", "faint": "#9aa6c6",
        "accent": "#2f6bff", "accent_strong": "#2f6bff", "accent_soft": "rgba(47,107,255,45)",
        "success": "#13a06b", "danger": "#e0475a", "warning": "#c98410", "group": "#8a5cf0",
        "card_bg": "rgba(255,255,255,130)", "card_border": "rgba(47,80,170,38)",
        "row_bg": "rgba(255,255,255,185)", "row_border": "rgba(60,100,220,45)", "row_hover": "rgba(47,107,255,140)",
        "info_bg": "rgba(236,241,255,200)", "info_border": "rgba(60,100,220,35)",
        "input_bg": "rgba(255,255,255,225)", "input_border": "rgba(60,100,220,70)",
        "btn_bg": "rgba(255,255,255,175)", "btn_border": "rgba(47,80,170,55)", "btn_hover": "rgba(255,255,255,250)",
        "btn_pressed": "rgba(222,231,255,255)", "btn_disabled_text": "rgba(15,27,61,90)",
        "btn_disabled_bg": "rgba(255,255,255,90)",
        "bar_bg": "rgba(255,255,255,190)", "bar_border": "rgba(60,100,220,60)",
        "scroll": "rgba(15,27,61,55)", "scroll_hover": "rgba(15,27,61,95)",
        "check_border": "rgba(60,100,220,120)", "check_bg": "rgba(255,255,255,230)",
        "tooltip_bg": "#ffffff", "tooltip_border": "#9db4ec", "menu_bg": "#ffffff", "menu_border": "#9db4ec",
        "win_btn": "#33426b", "edge": (60, 100, 220, 70), "thumb_bg": "#dfe6f8",
        "danger_bg": "rgba(224,71,90,28)", "danger_border": "rgba(224,71,90,110)",
        "danger_hover": "rgba(224,71,90,60)", "shadow": (30, 50, 120, 60), "divider": (15, 27, 61, 22),
        "keycap": "rgba(15,27,61,16)", "row_active": "rgba(47,107,255,45)",
        "tag_red": "#dc2626", "tag_orange": "#ea580c", "tag_yellow": "#ca8a04", "tag_green": "#16a34a",
        "tag_blue": "#2563eb", "tag_purple": "#9333ea", "tag_grey": "#6b7280",
    },
}
P = dict(PALETTES["dark"])   # the current palette (changed in place by set_palette)
_theme_listeners = []


def on_theme_change(callback):
    _theme_listeners.append(callback)


def set_palette(name):
    P.clear()
    P.update(PALETTES[name])
    app = QApplication.instance()
    if app:
        app.setStyleSheet(build_qss())
    for listener in list(_theme_listeners):
        try:
            listener()
        except RuntimeError:   # a listener whose widget has gone
            _theme_listeners.remove(listener)


def build_qss():
    p = P
    return f"""
* {{ color: {p['text']}; }}
QWidget#transparent {{ background: transparent; border: none; }}
QLabel#title {{ font-size: 24pt; font-weight: 800; letter-spacing: 2px; }}
QLabel#subtitle {{ color: {p['muted']}; font-size: 10pt; font-weight: 600; }}
QLabel#h2 {{ font-size: 15pt; font-weight: 800; }}
QLabel#muted {{ color: {p['muted']}; }}
QLabel#faint {{ color: {p['faint']}; }}
QLabel#fieldlabel {{ color: {p['field']}; font-weight: 700; font-size: 9pt; letter-spacing: 1px; }}
QLabel#key {{ color: {p['muted']}; }}
QLabel#empty {{ color: {p['muted']}; font-size: 12pt; }}
QLabel#keycap {{ background: {p['keycap']}; border-radius: 6px; padding: 2px 8px; font-weight: 700; }}
QFrame#card {{ background: {p['card_bg']}; border: 1px solid {p['card_border']}; border-radius: 20px; }}
QFrame#info {{ background: {p['info_bg']}; border: 1px solid {p['info_border']}; border-radius: 14px; }}
QFrame#info QLabel, QFrame#card QLabel, QFrame#tile QLabel, QFrame#popup QLabel, QFrame#treerow QLabel
    {{ background: transparent; border: none; }}
QFrame#tile {{ background: {p['row_bg']}; border: 1px solid {p['row_border']}; border-radius: 14px; }}
QFrame#tile[state="hover"] {{ border: 1px solid {p['row_hover']}; }}
QFrame#tile[state="range"] {{ border: 2px solid {p['accent']}; }}
QFrame#tile[state="group"] {{ border: 2px solid {p['group']}; }}
QFrame#tile[state="dup"] {{ border: 2px solid {p['danger']}; }}
QLabel#tilename {{ color: {p['muted']}; font-size: 9pt; }}
QLabel#tilename[state="hover"] {{ color: {p['text']}; }}
QLabel#tilename[state="range"] {{ color: {p['accent']}; font-weight: 700; }}
QLabel#tilename[state="group"] {{ color: {p['group']}; font-weight: 700; }}
QLabel#tilename[state="dup"] {{ color: {p['danger']}; font-weight: 700; }}
QFrame#duptile {{ background: {p['row_bg']}; border: 1px solid {p['row_border']}; border-radius: 18px; }}
QFrame#duptile:hover {{ border: 2px solid {p['accent']}; }}
QFrame#duptile QLabel {{ background: transparent; border: none; }}
QFrame#popup {{ background: {p['menu_bg']}; border: 1px solid {p['menu_border']}; border-radius: 12px; }}
QFrame#treerow {{ background: transparent; border: none; border-radius: 8px; }}
QFrame#treerow:hover {{ background: {p['btn_hover']}; }}
QFrame#treerow[active="true"] {{ background: {p['row_active']}; }}
QLabel#treename[active="true"] {{ color: {p['accent']}; font-weight: 700; }}
QLabel#treename[pending="true"] {{ color: {p['faint']}; }}
QLabel#treecount {{ color: {p['faint']}; font-size: 9pt; }}
QPushButton#mini {{ padding: 0; border-radius: 6px; min-width: 20px; max-width: 20px; min-height: 20px; max-height: 20px;
    font-size: 11pt; font-weight: 800; color: {p['muted']}; }}
QPushButton#mini:hover {{ color: {p['accent']}; border: 1px solid {p['accent']}; }}
QLineEdit {{ background: {p['input_bg']}; border: 1px solid {p['input_border']};
    border-radius: 10px; padding: 6px 10px; selection-background-color: {p['accent_strong']}; }}
QLineEdit:focus {{ border: 1px solid {p['accent']}; }}
QPushButton {{ background: {p['btn_bg']}; border: 1px solid {p['btn_border']};
    border-radius: 12px; padding: 9px 20px; font-weight: 700; }}
QPushButton:hover {{ background: {p['btn_hover']}; }}
QPushButton:pressed {{ background: {p['btn_pressed']}; }}
QPushButton:disabled {{ color: {p['btn_disabled_text']}; background: {p['btn_disabled_bg']}; }}
QPushButton#primary {{ border: none; color: #ffffff;
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #3a78ff, stop:1 #1c3fae); }}
QPushButton#primary:hover {{ background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #5a8fff, stop:1 #2650cc); }}
QPushButton#danger {{ background: {p['danger_bg']}; border: 1px solid {p['danger_border']}; }}
QPushButton#danger:hover {{ background: {p['danger_hover']}; }}
QPushButton#small {{ padding: 6px 14px; border-radius: 10px; }}
QPushButton#tool {{ padding: 5px 10px; border-radius: 9px; font-size: 9pt; }}
QPushButton#toolstatic {{ padding: 5px 10px; border-radius: 9px; font-size: 9pt; background: transparent; border: 1px dashed {p['btn_border']}; }}
QPushButton#toolstatic:hover {{ background: transparent; }}
QPushButton#static {{ padding: 6px 14px; border-radius: 10px; background: transparent; border: 1px dashed {p['btn_border']}; }}
QPushButton#static:hover {{ background: transparent; }}
QPushButton#ghost {{ background: transparent; border: none; padding: 6px 10px; }}
QPushButton#ghost:hover {{ background: {p['btn_hover']}; }}
QPushButton#tab {{ padding: 0; border-radius: 9px; font-size: 14pt; font-weight: 800; color: {p['muted']}; }}
QPushButton#tab:hover {{ color: {p['text']}; border: 1px solid {p['accent']}; }}
QFrame#folderbar {{ background: {p['input_bg']}; border: 1px solid {p['input_border']}; border-radius: 12px; }}
QFrame#folderbar:hover {{ border: 1px solid {p['accent']}; }}
QFrame#folderbar QLabel {{ background: transparent; border: none; }}
QProgressBar {{ background: {p['bar_bg']}; border: 1px solid {p['bar_border']}; border-radius: 6px;
    min-height: 10px; max-height: 10px; }}
QProgressBar::chunk {{ border-radius: 5px;
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #1f4fd8, stop:1 #29b6f6); }}
QScrollArea {{ background: transparent; border: none; }}
QScrollBar:vertical {{ background: transparent; width: 14px; margin: 4px; }}
QScrollBar::handle:vertical {{ background: {p['scroll']}; border-radius: 3px; min-height: 50px; }}
QScrollBar::handle:vertical:hover {{ background: {p['scroll_hover']}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical,
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ height: 0; background: none; }}
QCheckBox {{ spacing: 9px; background: transparent; }}
QCheckBox#toolcheck {{ font-size: 9pt; spacing: 7px; }}
QCheckBox::indicator {{ width: 18px; height: 18px; border-radius: 6px;
    border: 1px solid {p['check_border']}; background: {p['check_bg']}; }}
QCheckBox::indicator:checked {{ background: {p['accent_strong']}; border: 1px solid {p['accent']}; }}
QSlider::groove:horizontal {{ height: 6px; border-radius: 3px; background: {p['bar_bg']}; border: 1px solid {p['bar_border']}; }}
QSlider::sub-page:horizontal {{ height: 6px; border-radius: 3px;
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #1f4fd8, stop:1 #4d8dff); }}
QSlider::handle:horizontal {{ width: 16px; height: 16px; margin: -6px 0; border-radius: 8px;
    background: #ffffff; border: 2px solid {p['accent']}; }}
QSlider::handle:horizontal:hover, QSlider::handle:horizontal:focus {{ border: 3px solid {p['accent']}; }}
QMenu {{ background: {p['menu_bg']}; border: 1px solid {p['menu_border']}; border-radius: 10px; padding: 6px; }}
QMenu::item {{ padding: 7px 26px 7px 30px; border-radius: 7px; background: transparent; }}
QMenu::item:selected {{ background: {p['accent_strong']}; color: #ffffff; }}
QMenu::item:disabled {{ color: {p['faint']}; }}
QMenu::separator {{ height: 1px; background: {p['card_border']}; margin: 5px 10px; }}
QMenu::indicator {{ width: 14px; height: 14px; left: 9px; border-radius: 4px; border: 1px solid {p['check_border']}; }}
QMenu::indicator:checked {{ background: {p['accent_strong']}; border: 1px solid {p['accent']}; }}
QPushButton#winbtn, QPushButton#winclose {{ background: transparent; border: none; border-radius: 8px;
    padding: 0; min-width: 50px; max-width: 50px; min-height: 36px; max-height: 36px; color: {p['win_btn']}; }}
QPushButton#winbtn:hover {{ background: {p['btn_hover']}; }}
QPushButton#winbtn:pressed {{ background: {p['btn_pressed']}; }}
QPushButton#winclose:hover {{ background: #e81123; color: #ffffff; }}
QPushButton#winclose:pressed {{ background: #b40f1d; }}
QToolTip {{ background: {p['tooltip_bg']}; color: {p['text']}; border: 1px solid {p['tooltip_border']};
    border-radius: 8px; padding: 6px; }}
QFrame#hovertip {{ background: {p['tooltip_bg']}; border: 1px solid {p['tooltip_border']}; border-radius: 8px; }}
QFrame#hovertip QLabel {{ background: transparent; border: none; }}
"""


# ==================================================================== small helpers

def later(callback, ms=0):
    QTimer.singleShot(ms, callback)


class Var:
    """A tiny stand-in for tkinter's BooleanVar / StringVar: a value plus watchers (trace_add)."""

    def __init__(self, value=False):
        self._value = value
        self._watchers = []

    def get(self):
        return self._value

    def set(self, value):
        if value == self._value:
            return
        self._value = value
        for watcher in list(self._watchers):
            watcher("var", "", "write")

    def trace_add(self, _mode, callback):
        self._watchers.append(callback)


def paint_gradient(widget):
    painter = QPainter(widget)
    rect = widget.rect()
    gradient = QLinearGradient(QPointF(rect.topLeft()), QPointF(rect.bottomRight()))
    gradient.setColorAt(0.0, QColor(P["grad_start"]))
    gradient.setColorAt(1.0, QColor(P["grad_end"]))
    painter.fillRect(rect, gradient)
    painter.end()


def set_round_corners(widget):
    """Windows 11: ask DWM for rounded corners + shadow on a frameless window."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        pref = ctypes.c_int(2)  # DWMWCP_ROUND
        ctypes.windll.dwmapi.DwmSetWindowAttribute(int(widget.winId()), 33, ctypes.byref(pref), ctypes.sizeof(pref))
    except Exception:
        pass


def repolish(widget):
    widget.style().unpolish(widget)
    widget.style().polish(widget)
    widget.update()


def set_prop(widget, name, value):
    """Sets a style-sheet property (e.g. a tile's state) and re-applies the style if it changed."""
    if widget.property(name) != value:
        widget.setProperty(name, value)
        repolish(widget)


def add_shadow(widget, blur=40, offset=8):
    shadow = QGraphicsDropShadowEffect(widget)
    shadow.setBlurRadius(blur)
    shadow.setOffset(0, offset)
    shadow.setColor(QColor(*P["shadow"]))
    widget.setGraphicsEffect(shadow)
    on_theme_change(lambda: shadow.setColor(QColor(*P["shadow"])))
    return shadow


def card(parent=None):
    frame = QFrame(parent)
    frame.setObjectName("card")
    add_shadow(frame)
    return frame


def button(text, kind="", tip="", callback=None):
    widget = QPushButton(text)
    if kind:
        widget.setObjectName(kind)
    if tip:
        widget.setToolTip(tip)
    widget.setCursor(Qt.CursorShape.PointingHandCursor)
    if callback:
        widget.clicked.connect(lambda _=False: callback())
    return widget


def label(text="", kind="", wrap=False):
    widget = QLabel(text)
    if kind:
        widget.setObjectName(kind)
    if wrap:
        widget.setWordWrap(True)
    return widget


def caps(text):
    return label(text.upper(), "fieldlabel")


def keycap(text):
    return label(text, "keycap")


def transparent_box(layout_class=QHBoxLayout, spacing=10):
    box = QWidget()
    box.setObjectName("transparent")
    layout = layout_class(box)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(spacing)
    return box, layout


def _icon_font():
    families = set(QFontDatabase.families())
    for family in ("Segoe Fluent Icons", "Segoe MDL2 Assets"):
        if family in families:
            return family
    return None


def pil_to_pixmap(image):
    """PIL image -> QPixmap (GUI thread only). Keeps transparency."""
    if image.mode == "RGBA":
        data = image.tobytes("raw", "RGBA")
        qimage = QImage(data, image.width, image.height, 4 * image.width, QImage.Format.Format_RGBA8888)
    else:
        image = image.convert("RGB")
        data = image.tobytes("raw", "RGB")
        qimage = QImage(data, image.width, image.height, 3 * image.width, QImage.Format.Format_RGB888)
    return QPixmap.fromImage(qimage.copy())


def dot_pixmap(colour, size=12, outline=False):
    """A tag dot (or the outlined 'no tag' circle)."""
    ratio = 2
    pixmap = QPixmap(size * ratio, size * ratio)
    pixmap.setDevicePixelRatio(ratio)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    if outline:
        painter.setPen(QPen(QColor(P["muted"]), 1.5))
        painter.drawEllipse(QRectF(2, 2, size - 4, size - 4))
    else:
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(colour))
        painter.drawEllipse(QRectF(1, 1, size - 2, size - 2))
    painter.end()
    return pixmap


def folder_icon(width, height, enabled=True):
    pixmap = QPixmap(width * 2, height * 2)
    pixmap.setDevicePixelRatio(2)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    back = QColor(P["accent"] if enabled else P["faint"])
    front = QColor("#7fb0ff" if enabled else P["faint"])
    w, h = width, height
    tab = QPainterPath()
    tab.moveTo(1, 2)
    tab.lineTo(w * 0.38, 2)
    tab.lineTo(w * 0.5, h * 0.22)
    tab.lineTo(w - 1, h * 0.22)
    tab.lineTo(w - 1, h - 1)
    tab.lineTo(1, h - 1)
    tab.closeSubpath()
    painter.fillPath(tab, back)
    body = QPainterPath()
    body.addRoundedRect(QRectF(1, h * 0.36, w - 2, h * 0.64 - 1), 2, 2)
    painter.fillPath(body, front)
    painter.end()
    return pixmap


# ==================================================================== window chrome (as V.O.T)

class WindowControls(QWidget):
    """Minimise / maximise-restore / close buttons for the frameless main window."""

    def __init__(self, win, parent):
        super().__init__(parent)
        self.win = win
        family = _icon_font()
        self.glyphs = ({"min": "", "max": "", "restore": "", "close": ""} if family else
                       {"min": "—", "max": "☐", "restore": "❐", "close": "✕"})
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(4)
        self.b_min = self._button("min", "winbtn", "Minimise", family)
        self.b_max = self._button("max", "winbtn", "Maximise", family)
        self.b_close = self._button("close", "winclose", "Close", family)
        for widget in (self.b_min, self.b_max, self.b_close):
            row.addWidget(widget)
        self.b_min.clicked.connect(win.showMinimized)
        self.b_max.clicked.connect(self.toggle_max)
        self.b_close.clicked.connect(win.close)
        self.adjustSize()

    def _button(self, key, kind, tip, family):
        widget = QPushButton(self.glyphs[key])
        widget.setObjectName(kind)
        widget.setToolTip(tip)
        widget.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        widget.setFont(QFont(family, 10) if family else QFont(widget.font().family(), 12))
        return widget

    def toggle_max(self):
        if self.win.isMaximized():
            self.win.showNormal()
        else:
            self.win.showMaximized()

    def sync(self):
        maxed = self.win.isMaximized()
        self.b_max.setText(self.glyphs["restore" if maxed else "max"])
        self.b_max.setToolTip("Restore" if maxed else "Maximise")


def _edges_at(widget, pos, margin=8):
    rect, edges = widget.rect(), Qt.Edge(0)
    if pos.x() <= margin:
        edges |= Qt.Edge.LeftEdge
    if pos.x() >= rect.width() - margin:
        edges |= Qt.Edge.RightEdge
    if pos.y() <= margin:
        edges |= Qt.Edge.TopEdge
    if pos.y() >= rect.height() - margin:
        edges |= Qt.Edge.BottomEdge
    return edges


def _cursor_for(edges):
    L, R, T, B = Qt.Edge.LeftEdge, Qt.Edge.RightEdge, Qt.Edge.TopEdge, Qt.Edge.BottomEdge
    if (edges & L and edges & T) or (edges & R and edges & B):
        return Qt.CursorShape.SizeFDiagCursor
    if (edges & R and edges & T) or (edges & L and edges & B):
        return Qt.CursorShape.SizeBDiagCursor
    if edges & L or edges & R:
        return Qt.CursorShape.SizeHorCursor
    if edges & T or edges & B:
        return Qt.CursorShape.SizeVerCursor
    return None


class WindowRoot(QWidget):
    """Central widget of the frameless window: paints the gradient, drags from the header, resizes from edges."""

    def __init__(self, win):
        super().__init__()
        self.win = win
        self.drag_limit = 110          # dragging allowed above this y (the header)
        self.controls = None
        self.setMouseTracking(True)

    def _edges(self, pos):
        if not FRAMELESS or self.win.isMaximized() or self.win.isFullScreen():
            return Qt.Edge(0)
        return _edges_at(self, pos)

    def mouseMoveEvent(self, event):
        if not event.buttons():
            cursor = _cursor_for(self._edges(event.position().toPoint()))
            self.setCursor(cursor) if cursor else self.unsetCursor()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):
        self.unsetCursor()
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        if FRAMELESS and event.button() == Qt.MouseButton.LeftButton:
            pos = event.position().toPoint()
            handle = self.win.windowHandle()
            edges = self._edges(pos)
            if edges and handle:
                handle.startSystemResize(edges)
                event.accept()
                return
            if pos.y() < self.drag_limit and handle:
                handle.startSystemMove()       # native move: keeps Windows snap
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        if FRAMELESS and event.button() == Qt.MouseButton.LeftButton and event.position().y() < self.drag_limit:
            if self.controls:
                self.controls.toggle_max()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.controls:
            self.controls.move(self.width() - self.controls.width() - 10, 10)
            self.controls.raise_()

    def paintEvent(self, event):
        paint_gradient(self)
        if FRAMELESS and not self.win.isMaximized():
            painter = QPainter(self)
            painter.setPen(QPen(QColor(*P["edge"]), 1))
            painter.drawRect(self.rect().adjusted(0, 0, -1, -1))
            painter.end()


class GradientDialog(QDialog):
    """Themed dialog: its own title strip + close button, drags from the strip."""
    BAR = 58

    def __init__(self, parent=None, title=""):
        super().__init__(parent)
        self._bar_fitted = False
        self._centred = False
        self._title = self._close = None
        self.setWindowTitle(title)
        if FRAMELESS:
            self.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
            self._title = QLabel(title, self)
            self._title.setObjectName("h2")
            self._title.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            self._title.adjustSize()
            family = _icon_font()
            self._close = QPushButton("" if family else "✕", self)
            self._close.setObjectName("winclose")
            self._close.setToolTip("Close")
            self._close.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            self._close.setFont(QFont(family, 10) if family else QFont(self.font().family(), 12))
            self._close.clicked.connect(self.reject)

    def _place_chrome(self):
        if self._title:
            self._title.adjustSize()
            self._title.move(26, (self.BAR - self._title.height()) // 2 + 2)
            self._close.adjustSize()
            self._close.move(self.width() - self._close.width() - 10, 10)
            self._close.raise_()

    def _fit_bar(self):
        layout = self.layout()
        if FRAMELESS and layout and not self._bar_fitted:
            margins = layout.contentsMargins()
            layout.setContentsMargins(margins.left(), self.BAR, margins.right(), margins.bottom())
            self._bar_fitted = True

    def showEvent(self, event):
        self._fit_bar()
        super().showEvent(event)
        if FRAMELESS:
            self._place_chrome()
            set_round_corners(self)
        if not self._centred:   # every dialog opens in the middle of the screen (later re-fits keep their place)
            self._centred = True
            self.centre_on_screen()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._place_chrome()

    def centre_on_screen(self):
        """Middle of the screen the main window is on (not the middle of the window, which may be half-width).
        Call from showEvent: the title strip is added as the dialog appears, so its final size is only known then."""
        layout = self.layout()
        if layout:
            layout.activate()
        self.resize(self.size().expandedTo(self.sizeHint()))   # grow to fit the strip; keep sizes a dialog chose
        anchor = self.parentWidget() or self
        screen = (anchor.screen() or QGuiApplication.primaryScreen()).availableGeometry()
        frame = self.frameGeometry()
        frame.moveCenter(screen.center())
        self.move(max(screen.left(), frame.left()), max(screen.top(), frame.top()))

    def paintEvent(self, event):
        paint_gradient(self)
        if FRAMELESS:
            painter = QPainter(self)
            painter.setPen(QPen(QColor(*P["edge"]), 1))
            painter.drawRect(self.rect().adjusted(0, 0, -1, -1))
            painter.setPen(QPen(QColor(*P["divider"]), 1))
            painter.drawLine(18, self.BAR - 6, self.width() - 18, self.BAR - 6)
            painter.end()

    def mousePressEvent(self, event):
        if FRAMELESS and event.button() == Qt.MouseButton.LeftButton and self.windowHandle():
            if event.position().y() < self.BAR:
                self.windowHandle().startSystemMove()
                event.accept()
                return
        super().mousePressEvent(event)


class MessageDialog(GradientDialog):
    """Themed message: icon, text, optional rows of details, buttons. .choice is the index pressed."""

    def __init__(self, parent, title, text, buttons=("OK",), primary=0, danger=None, kind="info", rows=()):
        super().__init__(parent, title)
        self.choice = -1
        outer = QVBoxLayout(self)
        outer.setContentsMargins(28, 24, 28, 22)
        outer.setSpacing(18)
        top = QHBoxLayout()
        top.setSpacing(18)
        color = {"warn": P["warning"], "question": P["accent"], "error": P["danger"]}.get(kind, P["accent"])
        glyph = {"warn": "!", "question": "?", "error": "✖"}.get(kind, "i")
        icon = QLabel(glyph)
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon.setFixedSize(44, 44)
        tint = QColor(color)
        icon.setStyleSheet(f"border-radius: 22px; background: rgba({tint.red()},{tint.green()},{tint.blue()},50); "
                           f"color: {color}; border: 2px solid {color}; font-size: 16pt; font-weight: 800;")
        message = QLabel(text)
        message.setWordWrap(True)
        message.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        message.setMinimumWidth(520)
        top.addWidget(icon, 0, Qt.AlignmentFlag.AlignTop)
        top.addWidget(message, 1)
        outer.addLayout(top)
        if rows:
            box = QFrame()
            box.setObjectName("info")
            grid = QGridLayout(box)
            grid.setContentsMargins(18, 14, 18, 14)
            grid.setHorizontalSpacing(18)
            grid.setVerticalSpacing(7)
            for number, (key, value) in enumerate(rows):
                grid.addWidget(label(key, "key"), number, 0, Qt.AlignmentFlag.AlignTop)
                shown = label(value, "faint" if not key else "")
                shown.setWordWrap(True)
                grid.addWidget(shown, number, 1)
            grid.setColumnStretch(1, 1)
            outer.addWidget(box)
        row = QHBoxLayout()
        row.addStretch(1)
        self.buttons = []
        for index, text_ in enumerate(buttons):
            widget = button(text_, "danger" if index == danger else "primary" if index == primary else "")
            widget.setMinimumWidth(110)
            widget.clicked.connect(lambda _=False, i=index: self._pick(i))
            if index == primary:
                widget.setDefault(True)
                widget.setFocus()
            row.addWidget(widget)
            self.buttons.append(widget)
        outer.addLayout(row)

    def _pick(self, index):
        self.choice = index
        self.accept()


class ElidedLabel(QLabel):
    """One line, shortened with … to fit; the full text is in the tooltip when shortened."""

    def __init__(self, text="", parent=None, mode=Qt.TextElideMode.ElideRight):
        super().__init__(parent)
        self._full = text
        self._mode = mode
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setMinimumWidth(10)
        self._refresh()

    def setFullText(self, text):
        if text != self._full:
            self._full = text
            self._refresh()

    def fullText(self):
        return self._full

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._refresh()

    def _refresh(self):
        shown = QFontMetrics(self.font()).elidedText(self._full, self._mode, max(10, self.width()))
        super().setText(shown)
        self.setToolTip(self._full if shown != self._full else "")


class FlowLayout(QLayout):
    """Lays its items out left to right and wraps onto the next row when they don't fit (toolbar groups)."""

    def __init__(self, parent=None, h_spacing=36, v_spacing=12):
        super().__init__(parent)
        self._items = []
        self._h, self._v = h_spacing, v_spacing

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, index):
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index):
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self):
        return Qt.Orientation(0)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._do_layout(QRect(0, 0, width, 0), True)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._do_layout(rect, False)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        margins = self.contentsMargins()
        return size + QSize(margins.left() + margins.right(), margins.top() + margins.bottom())

    def rows(self, width):
        """How many rows the items take at this width (for tests and layout checks)."""
        margins = self.contentsMargins()
        right = width - margins.right()
        x, count = margins.left(), 1
        for item in self._items:
            hint = item.sizeHint()
            if x + hint.width() > right and x > margins.left():
                count += 1
                x = margins.left()
            x += hint.width() + self._h
        return count

    def _do_layout(self, rect, test_only):
        margins = self.contentsMargins()
        area = rect.adjusted(margins.left(), margins.top(), -margins.right(), -margins.bottom())
        x, y, line_height = area.x(), area.y(), 0
        line = []   # (item, x, size) on the current row; placed once its height is known, centred in it

        def place():
            if not test_only:
                for item, left, hint in line:
                    item.setGeometry(QRect(QPoint(left, y + (line_height - hint.height()) // 2), hint))
            line.clear()

        for item in self._items:
            hint = item.sizeHint()
            next_x = x + hint.width() + self._h
            if next_x - self._h > area.right() + 1 and line_height > 0:
                place()
                x = area.x()
                y += line_height + self._v
                next_x = x + hint.width() + self._h
                line_height = 0
            line.append((item, x, hint))
            x = next_x
            line_height = max(line_height, hint.height())
        place()
        return y + line_height - rect.y() + margins.bottom()


class GroupButton(QPushButton):
    """The Group button: in group mode a light runs round its border."""

    def __init__(self, text):
        super().__init__(text)
        self.setObjectName("small")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.active = False
        self.phase = 0.0
        self.timer = QTimer(self, interval=40)
        self.timer.timeout.connect(self._step)

    def set_active(self, active):
        self.active = active
        if active:
            self.timer.start()
        else:
            self.timer.stop()
        self.update()

    def _step(self):
        self.phase = (self.phase + 6) % 360
        self.update()

    def paintEvent(self, event):
        super().paintEvent(event)
        if not self.active:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect()).adjusted(1.5, 1.5, -1.5, -1.5)
        gradient = QConicalGradient(rect.center(), -self.phase)
        color = QColor(P["accent"])
        clear = QColor(color)
        clear.setAlpha(30)
        gradient.setColorAt(0.0, color)
        gradient.setColorAt(0.35, clear)
        gradient.setColorAt(0.65, clear)
        gradient.setColorAt(1.0, color)
        painter.setPen(QPen(gradient, 2.5))
        painter.drawRoundedRect(rect, 10, 10)
        painter.end()


def bind_check(box, var):
    """A checkbox that follows a Var and sets it when clicked."""
    box.setChecked(bool(var.get()))
    box.setCursor(Qt.CursorShape.PointingHandCursor)
    box.toggled.connect(lambda checked: var.set(checked) if var.get() != checked else None)

    def follow(*_):
        if box.isChecked() != bool(var.get()):
            box.blockSignals(True)
            box.setChecked(bool(var.get()))
            box.blockSignals(False)
    var.trace_add("write", follow)
    return box


class FolderBar(QFrame):
    """The folder box in the header: where you are in the hub, the folder's tag and flags, and its counts.
    Click: the folder list. Right-click: tag the folder."""
    clicked = Signal()
    right_clicked = Signal()

    def __init__(self):
        super().__init__()
        self.setObjectName("folderbar")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        row = QHBoxLayout(self)
        row.setContentsMargins(16, 10, 16, 10)
        row.setSpacing(0)
        self.icon = QLabel()
        self.icon.setFixedSize(24, 20)
        row.addWidget(self.icon)
        row.addSpacing(10)
        self.path_label = ElidedLabel("", mode=Qt.TextElideMode.ElideMiddle)
        self.path_label.setObjectName("muted")
        self.dot = QLabel()
        self.dot.setFixedSize(18, 14)
        self.name_label = QLabel("")
        self.name_label.setStyleSheet("font-weight: 800;")
        self.flags_label = QLabel("")
        self.count_label = ElidedLabel("", mode=Qt.TextElideMode.ElideLeft)
        self.count_label.setObjectName("faint")
        self.count_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.chevron = label("▾", "muted")
        row.addWidget(self.path_label, 1)
        row.addWidget(self.dot)
        row.addWidget(self.name_label, 0)
        row.addSpacing(8)
        row.addWidget(self.flags_label, 0)
        row.addSpacing(16)
        row.addWidget(self.count_label, 1)
        row.addSpacing(10)
        row.addWidget(self.chevron)
        self.paint_icon()

    def paint_icon(self):
        self.icon.setPixmap(folder_icon(24, 20))

    def set_folder(self, path_part, name_part, flags, count_text, tag_colour, struck):
        self.path_label.setFullText(path_part)
        self.path_label.setMaximumWidth(QFontMetrics(self.path_label.font()).horizontalAdvance(path_part) + 4)
        self.name_label.setText(name_part)
        font = self.name_label.font()
        font.setStrikeOut(bool(struck))
        self.name_label.setFont(font)
        self.flags_label.setText(flags)
        self.count_label.setFullText(count_text)
        if tag_colour:
            self.dot.setPixmap(dot_pixmap(tag_colour, 12))
            self.dot.show()
        else:
            self.dot.hide()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
            event.accept()
            return
        if event.button() == Qt.MouseButton.RightButton:
            self.right_clicked.emit()
            event.accept()
            return
        super().mousePressEvent(event)

    def contextMenuEvent(self, event):   # macOS Ctrl+Click arrives here
        self.right_clicked.emit()
        event.accept()


def half_tab_pixmap(halved, width=14, height=30):
    """The half-window tab, drawn small for the Settings shortcut list."""
    ratio = 2
    pixmap = QPixmap(width * ratio, height * ratio)
    pixmap.setDevicePixelRatio(ratio)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    path = QPainterPath()
    path.addRoundedRect(QRectF(-8, 0.5, width + 7.5, height - 1), 6, 6)
    painter.fillPath(path, QColor(255, 255, 255, 30) if P["text"].startswith("#e") else QColor(15, 27, 61, 22))
    painter.setPen(QPen(QColor(P["muted"]), 1.6))
    mid, reach = height / 2, min(5, height * 0.2)
    left, right = width * 0.33, width * 0.65
    x1, x2 = (right, left) if halved else (left, right)
    painter.drawPolyline([QPointF(x1, mid - reach), QPointF(x2, mid), QPointF(x1, mid + reach)])
    painter.end()
    return pixmap
