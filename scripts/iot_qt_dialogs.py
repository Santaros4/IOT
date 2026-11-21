"""
I.O.T's windows other than the main one, in Qt: Settings, the Undo list, the small prompts, the progress
window, the resizer (confirmation + progress), Compress Folder (C) and the full-screen duplicates viewer.
The shortcut, flag and confirmation tables shown in Settings are in iot_dialogs.py.
"""
import os
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor

from datetime import datetime

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QFontMetrics, QGuiApplication, QIcon, QPixmap
from PySide6.QtWidgets import (QCheckBox, QDialog, QFileDialog, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit,
                               QListWidget, QListWidgetItem, QProgressBar, QScrollArea, QSlider, QVBoxLayout, QWidget)

import iot_history
import iot_platform
import iot_tools
import iot_utils
from iot_dialogs import CONFIRMATIONS, FLAGS, SHORTCUTS
from iot_qt import (P, ElidedLabel, GradientDialog, MessageDialog, button, caps, flag_html, flag_label, half_tab_pixmap,
                    keycap, label, paint_gradient, pil_to_pixmap, transparent_box)


def _buttons_row(*widgets):
    row = QHBoxLayout()
    row.addStretch(1)
    for widget in widgets:
        widget.setMinimumWidth(110)
        row.addWidget(widget)
    return row


# ==================================================================== small prompts

def notice(parent, title, text, kind="info"):
    MessageDialog(parent, title, text, kind=kind).exec()


def confirm(parent, title, text, confirm_text, danger=False, rows=()):
    """Themed yes / no. Returns True when confirmed."""
    dialog = MessageDialog(parent, title, text, buttons=(confirm_text, "Cancel"), primary=0,
                           danger=0 if danger else None, kind="warn" if danger else "question", rows=rows)
    if danger:   # Enter still confirms
        dialog.buttons[0].setDefault(True)
        dialog.buttons[0].setFocus()
    dialog.exec()
    return dialog.choice == 0


class TextPrompt(GradientDialog):
    """One text field. ask() returns the text, or None if cancelled."""

    def __init__(self, parent, title, subtitle, field_label, value, confirm_text, extra_text=None):
        super().__init__(parent, title)
        self.extra_chosen = False
        outer = QVBoxLayout(self)
        outer.setContentsMargins(28, 18, 28, 22)
        outer.setSpacing(10)
        if subtitle:
            outer.addWidget(label(subtitle, "muted", wrap=True))
            outer.addSpacing(6)
        outer.addWidget(caps(field_label))
        self.field = QLineEdit(value)
        self.field.setMinimumWidth(480)
        outer.addWidget(self.field)
        outer.addSpacing(10)
        ok = button(confirm_text, "primary", callback=self.accept)
        ok.setDefault(True)
        row = _buttons_row(button("Cancel", callback=self.reject), ok)
        if extra_text:   # e.g. "Existing Folder…": on the left, apart from Cancel / OK
            extra = button(extra_text, "", "", self._extra)
            row.insertWidget(0, extra)
        outer.addLayout(row)
        self.field.selectAll()
        self.field.setFocus()

    def _extra(self):
        self.extra_chosen = True
        self.accept()

    @classmethod
    def ask(cls, parent, title, subtitle, field_label, value, confirm_text, extra_text=None):
        """The text, None if cancelled, or EXTRA when the extra button was pressed."""
        dialog = cls(parent, title, subtitle, field_label, value, confirm_text, extra_text)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        return EXTRA if dialog.extra_chosen else dialog.field.text()


EXTRA = object()   # TextPrompt.ask: the extra button was pressed


class FolderPickerDialog(GradientDialog):
    """Pick a folder in the hub (Move to an existing folder). Type to narrow the list; Enter or a double-click picks."""

    def __init__(self, app, title, subtitle, exclude=()):
        super().__init__(app, title)
        self.app, self.chosen = app, None
        outer = QVBoxLayout(self)
        outer.setContentsMargins(28, 14, 28, 22)
        outer.setSpacing(10)
        if subtitle:
            outer.addWidget(label(subtitle, "muted", wrap=True))
        self.search = QLineEdit()
        self.search.setPlaceholderText("Type to search folders")
        self.search.setClearButtonEnabled(True)
        outer.addWidget(self.search)
        self.list = QListWidget()
        self.list.setMinimumSize(560, 360)
        outer.addWidget(self.list, 1)
        excluded = {app._norm(p) for p in exclude}
        self.folders = []
        for info in app.folder_info_list:
            path = info["path"]
            if app._norm(path) in excluded or app._is_pending_delete(path):
                continue
            name = info["name"] if info["depth"] else f"{info['name']}  (hub)"
            self.folders.append((path, info["depth"], name))
        self._fill()
        self.search.textChanged.connect(lambda _t: self._fill())
        self.search.returnPressed.connect(self._pick_current)
        self.list.itemDoubleClicked.connect(lambda item: self._pick(item))
        ok = button("Move Here", "primary", callback=self._pick_current)
        ok.setDefault(True)
        outer.addLayout(_buttons_row(button("Cancel", callback=self.reject), ok))
        self.search.setFocus()

    def _fill(self):
        query = self.search.text().strip().lower()
        self.list.clear()
        for path, depth, name in self.folders:
            if query and query not in name.lower():
                continue
            item = QListWidgetItem(("      " * depth if not query else "") + name)
            if query:   # where it is, since the tree's indents are gone
                item.setToolTip(path)
                parent = os.path.relpath(os.path.dirname(path), os.path.dirname(self.app.parent_hub.rstrip("\\/")))
                item.setText(f"{name}      ·  {parent}" if depth else name)
            item.setData(Qt.ItemDataRole.UserRole, path)
            self.list.addItem(item)
        if self.list.count():
            self.list.setCurrentRow(0)

    def _pick_current(self):
        item = self.list.currentItem()
        if item is not None:
            self._pick(item)

    def _pick(self, item):
        self.chosen = item.data(Qt.ItemDataRole.UserRole)
        self.accept()

    @classmethod
    def ask(cls, app, title, subtitle, exclude=()):
        dialog = cls(app, title, subtitle, exclude)
        return dialog.chosen if dialog.exec() == QDialog.DialogCode.Accepted else None


class ProgressDialog(GradientDialog):
    """A batch in progress: a line of text, a bar and (optionally) Cancel. Closing it means Cancel."""

    def __init__(self, parent, title, text, maximum, on_cancel=None):
        super().__init__(parent, title)
        self.setModal(True)
        self.on_cancel = on_cancel
        self._done = False
        outer = QVBoxLayout(self)
        outer.setContentsMargins(28, 18, 28, 22)
        outer.setSpacing(10)
        self.text = label(text, "muted", wrap=True)
        self.text.setMinimumWidth(460)
        outer.addWidget(self.text)
        self.bar = QProgressBar()
        self.bar.setTextVisible(False)
        self.bar.setRange(0, max(1, maximum))
        outer.addSpacing(4)
        outer.addWidget(self.bar)
        if on_cancel:
            outer.addSpacing(10)
            outer.addLayout(_buttons_row(button("Cancel", callback=self.reject)))
        else:
            outer.addSpacing(8)

    def set_value(self, value, text=None):
        self.bar.setValue(value)
        if text is not None:
            self.text.setText(text)

    def reject(self):
        if self._done:
            super().reject()
        elif self.on_cancel:
            self.on_cancel()

    def finish(self):
        self._done = True
        self.accept()


# ==================================================================== Settings

class SettingsDialog(GradientDialog):
    """Shortcuts reference and every setting. Processing, folder flags and confirmations open their own small windows
    (Choose…). Everything is a private copy until Save, so Cancel really discards."""

    def __init__(self, app):
        super().__init__(app, "Settings")
        self.app = app
        outer = QVBoxLayout(self)
        outer.setContentsMargins(28, 14, 28, 22)
        outer.setSpacing(12)
        outer.addWidget(label("Shortcuts and options. (Hover for Details)", "muted"))
        # The two columns sit in a scroll area: on a small or highly scaled screen (1080p at 125%, say) the window
        # would otherwise be taller than the screen and Save would be out of reach. Normally it never scrolls.
        self.body = QWidget()
        self.body.setObjectName("transparent")
        columns = QHBoxLayout(self.body)
        columns.setContentsMargins(0, 0, 0, 0)
        columns.setSpacing(44)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setWidget(self.body)
        outer.addWidget(self.scroll, 1)

        # --- left: shortcuts
        left = QVBoxLayout()
        left.setSpacing(8)
        columns.addLayout(left, 1)
        prefix = "Folder list: "   # shown under their own heading, so the key column stays narrow
        main = [(k, t) for k, t in SHORTCUTS if not k.startswith(prefix)]
        in_list = [(k[len(prefix):], t) for k, t in SHORTCUTS if k.startswith(prefix)]
        left.addWidget(caps("Keyboard & mouse"))
        keys_width = max(keycap(iot_platform.keys(k)).sizeHint().width() for k, _ in main + in_list)
        left.addWidget(self._shortcut_table(main, keys_width, half_tab=True))
        if in_list:
            left.addSpacing(8)
            left.addWidget(caps("In the folder list"))
            left.addWidget(self._shortcut_table(in_list, keys_width))
        left.addSpacing(22)   # the same breathing room as above the title
        left.addStretch(1)

        # --- right: the settings (private copies)
        try:   # shown as a percentage of the average (130 = 30% above it); stored as the amount above (30)
            threshold = float(app.anomaly_threshold_var.get()) + 100
        except ValueError:
            threshold = 130.0
        self.processing = {"ignore": app.ignore_files, "threshold": threshold, "down": app.down_target, "up": app.up_target, "quality": app.jpg_quality,
                           "strip_exif": app.strip_exif}
        self.flags = dict(app.flags_enabled)
        self.confirmations = dict(app.confirmations)
        right = QVBoxLayout()
        right.setSpacing(8)
        columns.addLayout(right, 0)
        self.processing_summary = self._choose_row(right, "Processing", "Ignored files, resize sizes, JPEG quality, EXIF",
                                                   self._choose_processing)
        right.addSpacing(6)
        self.flags_summary = self._choose_row(right, "Folder flags", "Which flags the folder list and folder box show",
                                              self._choose_flags)
        right.addSpacing(6)
        self.confirm_summary = self._choose_row(right, "Confirmations", "Which actions ask \"are you sure?\" first",
                                                self._choose_confirmations)
        self._show_summaries()
        right.addSpacing(10)

        right.addWidget(caps("Compress (C)"))
        zip_box, zip_row = transparent_box(QHBoxLayout, 8)
        zip_tip = ("Where the zips made by Compress (C) go. Leave it empty to put a folder's zip next to the folder, "
                   "and a selection's zip in the folder it's from.")
        self.zip_dest = QLineEdit(app.zip_destination)
        self.zip_dest.setPlaceholderText("Next to the folder")
        self.zip_dest.setClearButtonEnabled(True)
        self.zip_dest.setMinimumWidth(260)
        self.zip_dest.setToolTip(zip_tip)
        zip_label = label("Zips go to")
        zip_label.setToolTip(zip_tip)
        zip_row.addWidget(zip_label)
        zip_row.addWidget(self.zip_dest, 1)
        zip_row.addWidget(button("Browse…", "small", "Choose the folder for zips", self._browse_zip))
        right.addWidget(zip_box)
        right.addSpacing(10)

        right.addWidget(caps("Folders"))
        self.empty_offer = QCheckBox("Offer to delete a folder you've emptied")
        self.empty_offer.setChecked(app.offer_empty_delete)
        self.empty_offer.setToolTip("After you move or delete the last picture out of a folder, I.O.T asks whether to "
                                    "delete the empty folder when it closes (like Q). Only folders left completely empty.")
        right.addWidget(self.empty_offer)
        self.existing = QCheckBox("Offer existing folders when moving a range")
        self.existing.setChecked(app.offer_existing_folders)
        self.existing.setToolTip("The box that asks for a folder name when you move a range gets an 'Existing "
                                 "Folder…' button, to move the pictures into a folder that's already in the hub "
                                 "(names unchanged).")
        right.addWidget(self.existing)
        trash = iot_platform.TRASH_NAME
        self.recycle = QCheckBox(f"Send deleted files to the {trash} when I.O.T closes")
        self.recycle.setChecked(app.use_recycle_bin and app.trash_available())
        if app.trash_available():
            self.recycle.setToolTip(f"Instead of deleting them permanently, pictures you deleted (and the originals of "
                                    f"resized or rotated ones) and folders marked with Q go to the {trash}, where you "
                                    f"can still restore them.")
        else:
            self.recycle.setEnabled(False)
            self.recycle.setToolTip("Needs the small send2trash package:  pip install send2trash")
        right.addWidget(self.recycle)
        right.addSpacing(10)

        right.addWidget(caps("Display"))
        self.frost = QCheckBox("Frosted screen behind Zoom preview")
        self.frost.setChecked(app.frost_preview)
        self.frost.setToolTip("Blurs the rest of the screen while Hover + Z shows an image. "
                              "Untick on slower computers if the preview feels sluggish.")
        self.sizes = QCheckBox("Folder sizes in the folder list")
        self.sizes.setChecked(app.tree_show_sizes)
        self.sizes.setToolTip("Shows how much space each folder uses in the folder drop-down list, "
                              "with a Σ total for folders that have sub-folders.")
        right.addWidget(self.frost)
        right.addWidget(self.sizes)
        tip_box, tip_row = transparent_box(QHBoxLayout, 14)
        tip_row.addWidget(label("Hover tip shows"))
        self.hover_boxes = {}
        for key, text, value, tip in (
                ("size", "File size", app.hover_size, "The file's size on disk, next to its pixel size"),
                ("taken", "Date taken", app.hover_taken, "When the photo was taken (from the camera's EXIF data)"),
                ("camera", "Camera", app.hover_camera, "The camera or phone that took it (from the EXIF data)")):
            box = QCheckBox(text)
            box.setChecked(value)
            box.setToolTip(tip)
            tip_row.addWidget(box)
            self.hover_boxes[key] = box
        tip_row.addStretch(1)
        right.addWidget(tip_box)
        speed_box, speed_row = transparent_box(QHBoxLayout, 10)
        speed_tip = ("How far the picture grid moves for each notch of the mouse wheel (100% is the normal speed). "
                     "Click the slider, then use the arrow keys to adjust it in 10% steps.")
        speed_label = label("Scroll speed")
        speed_label.setToolTip(speed_tip)
        self.speed = QSlider(Qt.Orientation.Horizontal)
        self.speed.setRange(5, 40)              # tens of percent: 50% - 400% in 10% steps
        self.speed.setSingleStep(1)
        self.speed.setPageStep(5)
        self.speed.setValue(app.scroll_speed // 10)
        self.speed.setFixedWidth(170)
        self.speed.setToolTip(speed_tip)
        self.speed.setCursor(Qt.CursorShape.PointingHandCursor)
        self.speed_value = label(f"{app.scroll_speed}%", "muted")
        self.speed_value.setMinimumWidth(QFontMetrics(self.font()).horizontalAdvance("400%") + 6)
        self.speed.valueChanged.connect(lambda v: self.speed_value.setText(f"{v * 10}%"))
        speed_row.addWidget(speed_label)
        speed_row.addWidget(self.speed)
        speed_row.addWidget(self.speed_value)
        speed_row.addStretch(1)
        right.addWidget(speed_box)
        right.addSpacing(10)
        path = label(f"Settings file:\n{app.settings_path}", "faint", wrap=True)
        path.setMaximumWidth(380)
        path.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        right.addWidget(path)
        right.addStretch(1)

        self.error = label("", wrap=True)
        self.error.setStyleSheet(f"color: {P['danger']};")
        self.error.hide()
        outer.addWidget(self.error)
        save = button("Save", "primary", callback=self.save)
        save.setDefault(True)
        outer.addLayout(_buttons_row(button("Cancel", callback=self.reject), save))
        self._fit_to_screen()

    def _fit_to_screen(self):
        """As tall as the settings need, but never taller than the screen: then the settings scroll instead."""
        self.body.adjustSize()
        need = self.body.sizeHint()
        screen = (self.app.screen() or QGuiApplication.primaryScreen()).availableGeometry()
        self.scroll.setFixedHeight(need.height() + 2)          # first as tall as the settings...
        self.layout().activate()
        whole = self.layout().sizeHint().height() + (0 if self._bar_fitted else self.BAR - self.layout().contentsMargins().top())
        over = whole - (screen.height() - 16)
        scrolls = over > 0
        if scrolls:                                            # ...then shorter by what doesn't fit
            self.scroll.setFixedHeight(max(240, need.height() + 2 - over))
        bar = self.scroll.verticalScrollBar().sizeHint().width()
        self.scroll.setMinimumWidth(min(need.width() + (bar + 8 if scrolls else 2), screen.width() - 80))

    @staticmethod
    def _choose_row(layout, title, tip, callback):
        """A section heading, a one-line summary and a Choose… button that opens the section's own window."""
        layout.addWidget(caps(title))
        box, row = transparent_box(QHBoxLayout, 10)
        summary = label("", "muted")
        summary.setToolTip(tip)
        row.addWidget(summary, 1)
        row.addWidget(button("Choose…", "small", tip, callback))
        layout.addWidget(box)
        return summary

    @staticmethod
    def _shortcut_table(rows, keys_width, half_tab=False):
        box = QWidget()
        box.setObjectName("transparent")
        grid = QGridLayout(box)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(6)
        grid.setColumnMinimumWidth(0, keys_width)
        for row, (keys, text) in enumerate(rows):
            grid.addWidget(keycap(iot_platform.keys(keys)), row, 0, Qt.AlignmentFlag.AlignLeft)
            grid.addWidget(label(iot_platform.keys(text), "muted"), row, 1)
        if half_tab:   # the half-window tab on the left edge of the grid, drawn small
            tab = QLabel()
            tab.setPixmap(half_tab_pixmap(False))
            grid.addWidget(tab, len(rows), 0, Qt.AlignmentFlag.AlignLeft)
            grid.addWidget(label("Press to half width, press again to maximize (for drag & drop)", "muted"),
                           len(rows), 1)
        grid.setColumnStretch(1, 1)
        return box

    def _fail(self, text):
        self.error.setText(text)
        self.error.show()

    def _show_summaries(self):
        p = self.processing
        ignoring = f"Ignore first {p['ignore']}" if p["ignore"] else "Ignore none"
        self.processing_summary.setText(f"{ignoring}  ·  resize to {p['down']} / {p['up']} px"
                                        f"  ·  quality {p['quality']}%")
        shown = [symbol for key, symbol, _ in FLAGS if self.flags.get(key, True)]
        self.flags_summary.setText(flag_html("Showing  " + "  ".join(shown)) if shown else "All hidden")
        asking = sum(1 for key, *_ in CONFIRMATIONS if self.confirmations.get(key))
        self.confirm_summary.setText(f"{asking} of {len(CONFIRMATIONS)} actions ask first")

    def _choose_processing(self):
        dialog = ProcessingDialog(self, self.processing)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.processing = dialog.values
            self._show_summaries()

    def _choose_flags(self):
        dialog = FlagsDialog(self, self.flags)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.flags = dialog.result_values()
            self._show_summaries()

    def _choose_confirmations(self):
        dialog = ConfirmationsDialog(self, self.confirmations)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.confirmations = dialog.result_values()
            self._show_summaries()

    def _show_confirm_summary(self):   # (older name)
        self._show_summaries()

    def _browse_zip(self):
        start = self.zip_dest.text().strip() or os.path.dirname(self.app.image_folder.rstrip("\\/"))
        chosen = QFileDialog.getExistingDirectory(self, "Where should zips go?", start)
        if chosen:
            self.zip_dest.setText(os.path.normpath(chosen))

    def save(self):
        app, p = self.app, self.processing
        zip_dest = self.zip_dest.text().strip().strip('"')
        if zip_dest and not os.path.isdir(zip_dest):
            self._fail("The folder for zips doesn't exist. Choose one with Browse, or leave it empty to put zips "
                       "next to the folder.")
            return
        app.zip_destination = os.path.normpath(zip_dest) if zip_dest else ""
        app.confirmations = dict(self.confirmations)
        app.anomaly_threshold_var.set(f"{p['threshold'] - 100:g}")
        app.down_target, app.up_target, app.jpg_quality = p["down"], p["up"], p["quality"]
        app.ignore_files, app.strip_exif = p["ignore"], p["strip_exif"]
        app.flags_enabled.update(self.flags)
        app.frost_preview = self.frost.isChecked()
        app.tree_show_sizes = self.sizes.isChecked()
        app.scroll_speed = self.speed.value() * 10
        app.hover_size = self.hover_boxes["size"].isChecked()
        app.hover_taken = self.hover_boxes["taken"].isChecked()
        app.hover_camera = self.hover_boxes["camera"].isChecked()
        app.offer_empty_delete = self.empty_offer.isChecked()
        app.offer_existing_folders = self.existing.isChecked()
        if self.recycle.isEnabled():
            app.use_recycle_bin = self.recycle.isChecked()
        app.save_settings()
        self.accept()
        app.handle_anomaly_recalculation_event()


class ProcessingDialog(GradientDialog):
    """Settings > Processing: the base image, the flags' thresholds, the resize sizes, JPEG quality and EXIF."""

    def __init__(self, parent, values):
        super().__init__(parent, "Processing")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(28, 14, 28, 22)
        outer.setSpacing(12)
        outer.addWidget(label("Hover over a setting for what it does.", "muted"))
        box = QFrame()
        box.setObjectName("info")
        fields = QGridLayout(box)
        fields.setContentsMargins(18, 14, 18, 14)
        fields.setHorizontalSpacing(12)
        fields.setVerticalSpacing(8)
        self.edits = {}
        for row, (key, text, suffix, tip) in enumerate((
                ("ignore", "Ignore files", "at the start of each folder",
                 "Covers or thumbnails at the start of a folder are skipped by the resolution check, the rename "
                 "suggestion, the resize estimate and Find Duplicates."),
                ("threshold", f"{iot_platform.FLAG_SIZE} Unusual size threshold", "% of average",
                 "(Optional) Intended to help auto-detect potential issues"),
                ("down", f"{iot_platform.FLAG_DOWN} Downscale short edge to", "px",
                 "Your preferred resolution when Downscaling. Folders whose first picture (after the ignored ones) "
                 f"has a longer short edge get {iot_platform.FLAG_DOWN} and only Downscale"),
                ("up", f"{iot_platform.FLAG_UP} Upscale short edge to", "px",
                 "Your preferred resolution when Upscaling. Folders whose first picture (after the ignored ones) "
                 f"has a shorter short edge get {iot_platform.FLAG_UP} and only Upscale"),
                ("quality", "JPEG quality", "%",
                 "Significant file size decrease with no noticeable loss of quality"))):
            name = flag_label(text)
            name.setToolTip(tip)
            value = values[key]
            edit = QLineEdit(f"{value:g}" if isinstance(value, float) else str(value))
            edit.setFixedWidth(84)
            edit.setAlignment(Qt.AlignmentFlag.AlignCenter)
            edit.setToolTip(tip)
            fields.addWidget(name, row, 0)
            fields.addWidget(edit, row, 1)
            fields.addWidget(label(suffix, "muted"), row, 2)
            self.edits[key] = edit
        self.exif = QCheckBox("Strip EXIF data from resized images")
        self.exif.setChecked(values["strip_exif"])
        self.exif.setToolTip("Removes information like Camera model & GPS co-ords, leave unchecked if unsure")
        fields.addWidget(self.exif, len(self.edits), 0, 1, 3)
        outer.addWidget(box)
        self.error = label("", wrap=True)
        self.error.setStyleSheet(f"color: {P['danger']};")
        self.error.hide()
        outer.addWidget(self.error)
        done = button("Done", "primary", callback=self.done_clicked)
        done.setDefault(True)
        outer.addLayout(_buttons_row(button("Cancel", callback=self.reject), done))
        self.values = dict(values)

    def done_clicked(self):
        edits = self.edits
        try:
            threshold = float(edits["threshold"].text())
            down, up = int(edits["down"].text()), int(edits["up"].text())
            quality = max(1, min(100, int(edits["quality"].text())))
            ignore = int(edits["ignore"].text())
            if min(down, up) <= 0 or ignore < 0:
                raise ValueError
        except ValueError:
            self.error.setText("Every field needs a whole number (Ignore files can be 0; the others more than 0).")
            self.error.show()
            return
        if threshold < 100:
            self.error.setText("Unusual size threshold needs to be 100 or more (100 = the average folder size).")
            self.error.show()
            return
        self.values = {"ignore": ignore, "threshold": threshold, "down": down, "up": up,
                       "quality": quality, "strip_exif": self.exif.isChecked()}
        self.accept()


class FlagsDialog(GradientDialog):
    """Settings > Folder flags: which of the folder list's flags are shown."""

    def __init__(self, parent, values):
        super().__init__(parent, "Folder flags")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(28, 14, 28, 22)
        outer.setSpacing(12)
        outer.addWidget(label("Flags point out folders that may need attention. Untick the ones you don't want to see.",
                              "muted", wrap=True))
        box = QFrame()
        box.setObjectName("info")
        grid = QGridLayout(box)
        grid.setContentsMargins(18, 14, 18, 14)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(10)
        self.boxes = {}
        for row, (key, symbol, text) in enumerate(FLAGS):
            tick = QCheckBox()
            tick.setChecked(values.get(key, True))
            tick.setCursor(Qt.CursorShape.PointingHandCursor)
            self.boxes[key] = tick
            grid.addWidget(tick, row, 0)
            grid.addWidget(flag_label(symbol), row, 1)
            grid.addWidget(label(text), row, 2)
        grid.setColumnStretch(2, 1)
        outer.addWidget(box)
        done = button("Done", "primary", callback=self.accept)
        done.setDefault(True)
        outer.addLayout(_buttons_row(button("Cancel", callback=self.reject), done))

    def result_values(self):
        return {key: box.isChecked() for key, box in self.boxes.items()}


class ConfirmationsDialog(GradientDialog):
    """Settings > Confirmations: one tick box per action that changes files. Ticked = asks "are you sure?" first."""

    def __init__(self, parent, values):
        super().__init__(parent, "Confirmations")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(28, 14, 28, 22)
        outer.setSpacing(12)
        outer.addWidget(label("Tick an action to have it ask \"are you sure?\" before it changes anything. Untick it to "
                              "skip the question. Undo can reverse every one of them until I.O.T closes.", "muted",
                              wrap=True))
        box = QFrame()
        box.setObjectName("info")
        grid = QGridLayout(box)
        grid.setContentsMargins(18, 14, 18, 14)
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(10)
        self.boxes = {}
        for row, (key, keys, text, default) in enumerate(CONFIRMATIONS):
            tick = QCheckBox()
            tick.setChecked(bool(values.get(key, default)))
            tick.setCursor(Qt.CursorShape.PointingHandCursor)
            tick.setToolTip("Ask first" if not default else "Ask first (on by default)")
            self.boxes[key] = tick
            grid.addWidget(tick, row, 0)
            grid.addWidget(keycap(iot_platform.keys(keys)), row, 1, Qt.AlignmentFlag.AlignLeft)
            grid.addWidget(label(iot_platform.keys(text)), row, 2)
            grid.addWidget(label("asks by default" if default else "doesn't ask by default", "faint"), row, 3)
        grid.setColumnStretch(2, 1)
        outer.addWidget(box)
        presets = QHBoxLayout()
        presets.setSpacing(8)
        for text, tip, choose in (("All on", "Every action asks first", lambda key, default: True),
                                  ("All off", "No action asks first (Undo still works)", lambda key, default: False),
                                  ("Defaults", "I.O.T's own choice: the quick, undoable actions don't ask",
                                   lambda key, default: default)):
            presets.addWidget(button(text, "small", tip, lambda c=choose: self._set_all(c)))
        presets.addStretch(1)
        outer.addLayout(presets)
        done = button("Done", "primary", callback=self.accept)
        done.setDefault(True)
        outer.addLayout(_buttons_row(button("Cancel", callback=self.reject), done))

    def _set_all(self, choose):
        for key, _, _, default in CONFIRMATIONS:
            self.boxes[key].setChecked(choose(key, default))

    def result_values(self):
        return {key: box.isChecked() for key, box in self.boxes.items()}


# ==================================================================== Undo list

class UndoWindow(GradientDialog):
    """The last changes of this session, newest first, each with Revert. Stays open while you work."""

    def __init__(self, app):
        super().__init__(app, "Undo")
        self.app = app
        self.setModal(False)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(28, 14, 28, 22)
        outer.setSpacing(12)
        outer.addWidget(label(f"The last {iot_history.HISTORY_LIMIT} changes made in this session. Deleted files and "
                              "the originals of resized images are kept until I.O.T closes.", "muted", wrap=True))
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        outer.addWidget(self.scroll, 1)
        outer.addLayout(_buttons_row(button("Close", callback=self.reject)))
        screen = (app.screen() or QGuiApplication.primaryScreen()).availableGeometry()
        self.resize(max(760, int(screen.width() * 0.4)), 400)
        self.setMinimumWidth(640)
        self._fit_bar()   # room for the title strip now, so the first size already includes it
        self.refresh()

    def refresh(self):
        app = self.app
        holder = QWidget()
        holder.setObjectName("transparent")
        rows = QVBoxLayout(holder)
        rows.setContentsMargins(0, 0, 6, 0)
        rows.setSpacing(8)
        records = list(reversed(app.history.records))
        if not records:
            empty = label("Nothing to undo yet.", "faint")
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            empty.setMinimumHeight(70)
            rows.addWidget(empty)
        for record in records:
            row = QFrame()
            row.setObjectName("info")
            line = QHBoxLayout(row)
            line.setContentsMargins(14, 10, 14, 10)
            line.setSpacing(14)
            badge = label(record.kind.upper(), "fieldlabel")
            badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
            badge.setFixedWidth(118)
            badge.setStyleSheet(f"background: {P['accent_soft']}; color: {P['accent']}; border-radius: 8px; padding: 4px;")
            line.addWidget(badge)
            # One line each, shortened with … (full text in the tooltip): a long title or a deep folder path
            # mustn't widen the list and push the Revert buttons out of sight
            text, text_box = transparent_box(QVBoxLayout, 2)
            title = ElidedLabel(record.title)
            if record.reverted:
                title.setObjectName("faint")
            try:
                where = os.path.relpath(record.folder, os.path.dirname(app.parent_hub.rstrip("\\/")))
            except ValueError:
                where = record.folder
            place = ElidedLabel(f"{app.time_ago(record.time)}  ·  {where}", mode=Qt.TextElideMode.ElideMiddle)
            place.setObjectName("muted")
            text_box.addWidget(title)
            text_box.addWidget(place)
            line.addWidget(text, 1)
            if record.reverted:
                line.addWidget(label("Reverted", "faint"))
            else:
                line.addWidget(button("Revert", "small", callback=lambda r=record: app.perform_undo(r)))
            rows.addWidget(row)
        rows.addStretch(1)
        # The old list may hold the Revert button being clicked right now: hand it over for deletion once the
        # click has finished (setWidget alone would delete it on the spot, mid-click)
        old = self.scroll.takeWidget()
        if old is not None:
            old.deleteLater()
        self.scroll.setWidget(holder)
        # As tall as the list, up to ~60% of the screen; beyond that it scrolls
        holder.adjustSize()
        screen = (self.screen() or QGuiApplication.primaryScreen()).availableGeometry()
        self.scroll.setFixedHeight(min(holder.sizeHint().height() + 4, int(screen.height() * 0.6)))
        self.resize(self.width(), self.sizeHint().height())   # keep the width; fit the height to the list

    def done(self, result):
        """Every way of closing ends here: Close, Esc and the title-bar X call reject(), and close() goes through
        reject() too (QDialog). Overriding reject() / closeEvent to call close() instead made Qt ignore the close."""
        if self.app._undo_window is self:
            self.app._undo_window = None
        super().done(result)
        self.deleteLater()


# ==================================================================== resizer (D / U)

def run_resizer(app, mode="down", folders=None):
    """Batch resizer: confirmation with an estimate, then a progress window while worker
    threads resize. folders: every image in these folders (a folder list branch); default: the folder on screen.
    Images are resized in place, keep their names, and the originals go to the session recycle folder."""
    if app.busy:
        return
    if folders is None:
        jobs = [(app.image_folder, app.name_ordered_files())]
    else:
        jobs = [(path, iot_utils.list_images(path, app.valid_extensions)) for path in folders]
    jobs = [(path, names) for path, names in jobs if names]
    if not jobs:
        app.log_status("Resizer: no images to resize there.", "warning")
        return

    word = "Downscale" if mode == "down" else "Upscale"
    target = app.down_target if mode == "down" else app.up_target
    quality = app.jpg_quality   # same for both: quality 100 made upscales ~3x bigger for no visible gain
    folder = folders[0] if folders else app.image_folder
    items = [(path, name) for path, names in jobs for name in names]
    files = [os.path.join(path, name) for path, name in items]
    top_name = os.path.basename(folder.rstrip("\\/")) or folder
    if folders is not None and len(folders) > 1:
        subs = len(folders) - 1
        where = f"'{top_name}' and its {subs} sub-folder{'s' if subs != 1 else ''}"
    else:
        where = f"'{top_name}'"
    in_folders = f" in {len(jobs)} folders" if len(jobs) > 1 else ""

    sample_folder, sample_files = jobs[0]
    estimate = iot_tools._estimate_resolution(sample_folder, sample_files, mode, target, app.base_image)
    if len(sample_files) < app.base_image:
        sample = "last picture"
    else:
        sample = f"first picture after the {app.ignore_files} ignored" if app.ignore_files else "first picture"
    if len(jobs) > 1:
        sample += f" of '{os.path.basename(sample_folder.rstrip(chr(92) + '/'))}'"
    rows = [("Altered resolution", estimate), ("", f"Estimate from the {sample}")] if estimate else []
    rows += [("Short edge", f"{target} px"), ("JPEG / WebP quality", f"{quality}%"),
             ("EXIF data", "Removed" if app.strip_exif else "Kept"), ("Undo", "Originals are kept until I.O.T closes")]
    text = (f"{word} {len(files)} {'image' if len(files) == 1 else 'images'}{in_folders}?\n\n"
            f"Images in {where} will be resized in place, names unchanged, so their short edge is {target} px"
            f"{' (only ones larger than that)' if mode == 'down' else ' (only ones smaller than that)'}.")
    if app.asks("resize") and not confirm(app, f"{word} images", text, word, rows=rows):
        return

    app.busy = True
    cancel_event = threading.Event()
    futures = []

    def cancel():
        cancel_event.set()
        for future in futures:
            future.cancel()
        progress.set_value(progress.bar.value(), "Cancelling… finishing the images already in progress")

    progress = ProgressDialog(app, f"{word.rstrip('e')}ing images…", f"0 of {len(files)}", len(files), cancel)
    progress.show()
    # PIL releases the GIL while decoding / resizing / encoding, so threads give real parallelism here
    executor = ThreadPoolExecutor(max_workers=max(2, min(8, os.cpu_count() or 4)), thread_name_prefix="iot-resize")
    futures.extend(executor.submit(iot_tools._resize_one, path, mode, target, quality, cancel_event,
                                   app.session.stash_path, app.strip_exif) for path in files)
    executor.shutdown(wait=False)
    timer = QTimer(progress, interval=100)

    def poll():
        done = sum(1 for future in futures if future.done())
        progress.set_value(done, None if cancel_event.is_set() else f"{done} of {len(files)}")
        if done < len(files):
            return
        timer.stop()
        finish()

    def finish():
        tally = {"converted": 0, "skipped": 0, "animated": 0, "failed": 0, "cancelled": 0}
        steps, changed, changed_folders = [], [], []
        for (path, name), future in zip(items, futures):
            if future.cancelled():
                tally["cancelled"] += 1
                continue
            status, file_steps = future.result()
            tally[status] += 1
            if file_steps:
                steps.extend(file_steps)
                changed.append(name)
                if path not in changed_folders:
                    changed_folders.append(path)
        progress.finish()
        app.busy = False
        if tally["converted"]:
            scope = f" in '{top_name}' and its sub-folders" if len(changed_folders) > 1 else ""
            app._record("Resize", f"{word}d {tally['converted']} images to {target} px{scope}", folder, steps,
                        [iot_history.describe_range(changed, "image") + f"  ->  {target} px short edge"]
                        + ([f"in {len(changed_folders)} folders"] if len(changed_folders) > 1 else [])
                        + ([f"{tally['skipped']} already within bounds, left unchanged"] if tally["skipped"] else []))
            for changed_folder in changed_folders:
                app.modified_folders.add(changed_folder)
                app.invalidate_folder_thumbnails(changed_folder)
            app.refresh_folders(changed_folders)
            if app.image_folder in changed_folders:
                app.load_directory_target_sequence(app.image_folder, keep_place=True)
            else:
                app.refresh_folder_button()
        message = f"{word}: {tally['converted']} images resized"
        if tally["skipped"]:
            message += f", {tally['skipped']} already within bounds"
        if tally["cancelled"]:
            message += f", {tally['cancelled']} cancelled"
        if tally["animated"]:
            message += f", {tally['animated']} animated left as they are (resizing keeps only the first frame)"
        if tally["failed"]:
            message += f", {tally['failed']} failed (unreadable or locked)"
        app.log_status(message + ".", "error" if tally["failed"] else "success")

    timer.timeout.connect(poll)
    timer.start()


# ==================================================================== Compress Folder (C)

# Already compressed: zipping them again takes far longer for next to no saving, so they're stored as they are
ALREADY_COMPRESSED = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic", ".heif", ".avif", ".jxl", ".zip", ".7z",
                      ".rar", ".gz", ".mp4", ".mov", ".mkv", ".avi", ".webm", ".mp3", ".m4a", ".flac", ".pdf"}


def _size_text(count):
    for unit in ("B", "KB", "MB", "GB"):
        if count < 1024 or unit == "GB":
            return f"{count:.0f} {unit}" if unit == "B" else f"{count:.1f} {unit}"
        count /= 1024.0


def zip_name_for(folder_name, destination):
    """The zip's name (without .zip): the folder's own name, or, if a zip of that name is already there, the name
    with letters taken off the end one at a time ('Kyoto' -> 'Kyot' -> 'Kyo'...) until it's free."""
    name = folder_name
    while name:
        if not os.path.lexists(os.path.join(destination, name + ".zip")):
            return name
        name = name[:-1].rstrip(" .")   # Windows doesn't allow names ending in a space or a dot
    number = 2                          # every shortened name is taken too (very unlikely): number it instead
    while os.path.lexists(os.path.join(destination, f"{folder_name} ({number}).zip")):
        number += 1
    return f"{folder_name} ({number})"


def _folder_contents(folder):
    """Every file and sub-folder inside `folder`, leaving out hidden ones: I.O.T's own (thumbnail cache, recycle
    folder, tags, files mid-write) and system clutter such as .DS_Store."""
    files, folders = [], []
    for root, dirs, names in os.walk(folder):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        folders.extend(os.path.join(root, d) for d in dirs)
        files.extend(os.path.join(root, n) for n in sorted(names) if not n.startswith("."))
    return files, folders


def _write_zip(temp_path, folder, files, folders, progress, cancel_event, top=None):
    """Worker thread: writes the zip. Paths inside it start with `top` (default: the folder's name), so unzipping gives
    the folder back. Returns ('done' | 'cancelled' | 'failed', [files that couldn't be read], error text)."""
    top = top or os.path.basename(folder.rstrip("\\/")) or "Folder"
    skipped = []

    def inside(path):
        return top + "/" + os.path.relpath(path, folder).replace(os.sep, "/")
    try:
        with zipfile.ZipFile(temp_path, "w", allowZip64=True) as archive:
            archive.writestr(zipfile.ZipInfo(top + "/"), "")
            for path in folders:   # empty sub-folders are kept too
                archive.writestr(zipfile.ZipInfo(inside(path) + "/"), "")
            for path in files:
                if cancel_event.is_set():
                    return "cancelled", skipped, ""
                stored = os.path.splitext(path)[1].lower() in ALREADY_COMPRESSED
                try:
                    archive.write(path, inside(path),
                                  compress_type=zipfile.ZIP_STORED if stored else zipfile.ZIP_DEFLATED,
                                  compresslevel=None if stored else 6)
                    progress["bytes"] += os.path.getsize(path)
                except OSError as err:
                    skipped.append(f"{os.path.basename(path)}: {err.strerror or err}")
                progress["files"] += 1
    except Exception as err:   # disk full, destination gone...
        return "failed", skipped, str(err) or err.__class__.__name__
    return "done", skipped, ""


def _remove_quietly(path):
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


def run_compress(app, names=None, title=None, on_done=None):
    """C / Compress. Without `names`: a zip of the folder on screen (the folder itself, everything in it and its
    sub-folders), named after the folder, saved where Settings says (or next to the folder). With `names` (a range or
    group picks): a zip of just those pictures, inside a folder called `title`, saved where Settings says (or in the
    folder they're from). Asks first; Undo deletes the zip. on_done() runs once the zip is made."""
    if app.busy:
        return
    folder = app.image_folder
    selection = names is not None
    if selection:
        top = title
        files = [os.path.join(folder, name) for name in names]
        folders = []
    else:
        top = os.path.basename(folder.rstrip("\\/")) or folder
        files, folders = _folder_contents(folder)
    if not files:
        notice(app, "Compress", f"'{top}' has no files to compress.")
        return
    total_bytes = 0
    for path in files:
        try:
            total_bytes += os.path.getsize(path)
        except OSError:
            pass

    chosen = app.zip_destination
    beside = folder if selection else os.path.dirname(folder.rstrip("\\/"))
    destination = chosen if chosen and os.path.isdir(chosen) else beside
    name = zip_name_for(top, destination)
    final_path = os.path.join(destination, name + ".zip")
    contents = f"{len(files)} file{'s' if len(files) != 1 else ''} ({_size_text(total_bytes)})"
    if folders:
        contents += f" in {len(folders) + 1} folders"
    rows = [("Contents", contents), ("Saved as", final_path)]
    if name != top:
        rows.append(("", f"'{top}.zip' is already there, so letters were taken off the end of the name"))
    if chosen and destination != chosen:
        rows.append(("", f"The folder chosen in Settings isn't available ({chosen}), so the zip goes "
                         f"{'in the folder they are from' if selection else 'next to the folder'} instead"))
    rows += [("Compression", "Pictures are stored as they are (they're compressed already); other files are "
                             "compressed"),
             ("Undo", "Deletes the zip. The pictures themselves aren't changed" if selection else
                      "Deletes the zip. The folder itself isn't changed")]
    if selection:
        first, last = os.path.basename(files[0]), os.path.basename(files[-1])
        which = f"{first}  →  {last}" if len(files) > 1 else first
        text = (f"Compress {len(files)} picture{'s' if len(files) != 1 else ''}?\n\n{which} go into one zip file, "
                f"inside a folder called '{top}'.")
    else:
        text = (f"Compress '{top}'?\n\nEverything in the folder, including its sub-folders, goes into one zip file, "
                "with the folder itself inside it.")
    if app.asks("compress") and not confirm(app, "Compress", text, "Compress", rows=rows):
        return

    app.busy = True
    cancel_event = threading.Event()
    progress = {"files": 0, "bytes": 0}
    temp_path = os.path.join(destination, f".iot_zip_{os.getpid()}_{name}.tmp")   # hidden from scans until done
    result = {}

    def cancel():
        cancel_event.set()
        window.set_value(window.bar.value(), "Cancelling…")

    window = ProgressDialog(app, f"Compressing '{top}'…", f"0 of {len(files)} files", max(1, total_bytes), cancel)
    window.bar.setRange(0, max(1, total_bytes // 1024))   # KB, so very large folders still fit the bar's range
    window.show()
    worker = threading.Thread(target=lambda: result.update(zip(("status", "skipped", "error"), _write_zip(
        temp_path, folder, files, folders, progress, cancel_event, top))), daemon=True, name="iot-zip")
    worker.start()
    timer = QTimer(window, interval=100)

    def poll():
        if not cancel_event.is_set():
            window.set_value(progress["bytes"] // 1024, f"{progress['files']} of {len(files)} files  ·  "
                                                         f"{_size_text(progress['bytes'])} of {_size_text(total_bytes)}")
        if worker.is_alive():
            return
        timer.stop()
        finish()

    def finish():
        window.finish()
        app.busy = False
        status, skipped, error = result.get("status", "failed"), result.get("skipped", []), result.get("error", "")
        if status != "done":
            _remove_quietly(temp_path)
            if status == "cancelled":
                app.log_status("Compress cancelled: no zip was made.", "info")
            else:
                notice(app, "Couldn't compress the folder", f"The zip of '{top}' couldn't be written:\n{error}",
                       kind="error")
            return
        target = os.path.join(destination, zip_name_for(top, destination) + ".zip")   # in case one appeared meanwhile
        try:
            os.replace(temp_path, target)
        except OSError as err:
            _remove_quietly(temp_path)
            notice(app, "Couldn't compress the folder", f"The zip couldn't be saved as {target}:\n{err}", kind="error")
            return
        undo_keys = iot_platform.keys("Ctrl+Z")
        log_lines = [f"{contents}  ->  {target}"]
        if skipped:
            log_lines.append(f"{len(skipped)} file(s) couldn't be read and were left out: " + "; ".join(skipped[:5]))
        app._record("Compress", f"Compressed '{top}' into '{os.path.basename(target)}'", folder,
                    [("created", target)], log_lines, target=destination)
        app.refresh_folders([destination])   # only re-read if it's a folder in the hub
        app.refresh_folder_button()
        if skipped:
            app.log_status(f"Compressed '{top}', but {len(skipped)} file(s) couldn't be read and were left out: "
                           f"{skipped[0]}", "warning")
        else:
            app.log_status(f"Compressed '{top}' into {target}  ({undo_keys} deletes the zip)", "success")
        if on_done:
            on_done()

    timer.timeout.connect(poll)
    timer.start()


# ==================================================================== duplicates window (F)

class _ClickPicture(QLabel):
    def __init__(self, callback):
        super().__init__()
        self.callback = callback
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.callback()
        event.accept()


class _DupTile(QFrame):
    """One copy in the Duplicates window: the picture, its details, Keep, Open and Show in Folder."""

    def __init__(self, window, path, pixmap, details, badges):
        super().__init__()
        self.window, self.path = window, path
        self.setObjectName("duptile")
        box = QVBoxLayout(self)
        box.setContentsMargins(16, 14, 16, 14)
        box.setSpacing(6)
        picture = _ClickPicture(lambda: window.app.open_path_externally(path))
        picture.setPixmap(pixmap)
        picture.setAlignment(Qt.AlignmentFlag.AlignCenter)
        picture.setToolTip("Open in your default image viewer")
        box.addWidget(picture, 1)
        name_row, names = transparent_box(QHBoxLayout, 6)
        name = ElidedLabel(os.path.basename(path), mode=Qt.TextElideMode.ElideMiddle)
        name.setStyleSheet("font-weight: 700;")
        names.addWidget(name, 1)
        for text in badges:   # "Largest", "Newest": usually what decides which copy to keep
            badge = label(text.upper(), "fieldlabel")
            badge.setStyleSheet(f"background: {P['accent_soft']}; color: {P['accent']}; border-radius: 7px; "
                                "padding: 2px 7px;")
            names.addWidget(badge)
        box.addWidget(name_row)
        box.addWidget(label(details, "muted"))
        actions, row = transparent_box(QHBoxLayout, 8)
        self.keep = QCheckBox("Keep")
        self.keep.setChecked(True)
        self.keep.setCursor(Qt.CursorShape.PointingHandCursor)
        self.keep.toggled.connect(lambda _on: window.update_buttons())
        row.addWidget(self.keep)
        row.addStretch(1)
        row.addWidget(button("Open", "small", "Open in your default image viewer",
                             lambda: window.app.open_path_externally(path)))
        row.addWidget(button("Show in Folder", "small", "Close this window and scroll to it in the grid",
                             lambda: window.show_in_folder(path)))
        box.addWidget(actions)


class DuplicatesWindow(GradientDialog):
    """F: every set of duplicates in the folder. The sets are listed on the left (↑ / ↓); the chosen set is shown on
    the right, side by side, with each copy's details. Untick the copies you don't want and press Delete Unticked
    (they go to the recycle folder; Undo brings them back)."""

    def __init__(self, app, sets):
        super().__init__(app, "Duplicates")
        self.app = app
        self.sets = [list(s) for s in sets]
        self.tiles = []
        self.keep_state = {}   # path -> ticked, so re-laying out (a resize) keeps your choices
        self._relayout = QTimer(self, singleShot=True, interval=120)
        self._relayout.timeout.connect(lambda: self.show_set(self.list.currentRow()))
        screen = (app.screen() or QGuiApplication.primaryScreen()).availableGeometry()
        self.resize(int(screen.width() * 0.88), int(screen.height() * 0.86))
        outer = QVBoxLayout(self)
        outer.setContentsMargins(28, 14, 28, 22)
        outer.setSpacing(12)
        self.summary = label("", "muted", wrap=True)
        outer.addWidget(self.summary)
        body = QHBoxLayout()
        body.setSpacing(18)
        outer.addLayout(body, 1)
        self.list = QListWidget()
        self.list.setFixedWidth(300)
        self.list.setIconSize(QSize(72, 54))
        self.list.currentRowChanged.connect(self.show_set)
        body.addWidget(self.list)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        body.addWidget(self.scroll, 1)
        self.delete_button = button("Delete Unticked", "danger", "Delete the copies you've unticked (Undo brings them "
                                    "back)", self.delete_unticked)
        close = button("Close", callback=self.reject)
        outer.addLayout(_buttons_row(self.delete_button, close))
        self._fill_list()
        self.list.setFocus()

    # --- the list of sets
    def _fill_list(self, row=0):
        self.list.blockSignals(True)
        self.list.clear()
        for paths in self.sets:
            item = QListWidgetItem(f"{os.path.basename(paths[0])}\n{len(paths)} copies")
            try:
                thumb, _ = self.app.disk_cache.load(paths[0], 108)
                icon = pil_to_pixmap(thumb)
                item.setIcon(QIcon(icon))
            except Exception:
                pass
            self.list.addItem(item)
        self.list.blockSignals(False)
        extra = sum(len(s) - 1 for s in self.sets)
        self.summary.setText(f"{len(self.sets)} set{'s' if len(self.sets) != 1 else ''} of near-identical pictures "
                             f"(98%+ alike), {extra} extra cop{'ies' if extra != 1 else 'y'}. Untick the copies you don't "
                             "want, then Delete Unticked: they go to the recycle folder, and Undo brings them back. "
                             "↑ / ↓ move through the sets; Esc closes.")
        if self.sets:
            self.list.setCurrentRow(min(row, len(self.sets) - 1))
            self.show_set(self.list.currentRow())

    # --- the chosen set
    def show_set(self, row):
        old = self.scroll.takeWidget()
        if old is not None:
            old.deleteLater()
        self.tiles = []
        if not 0 <= row < len(self.sets):
            return
        paths = self.sets[row]
        holder, line = transparent_box(QHBoxLayout, 18)
        view = self.scroll.viewport().size()
        shown = min(len(paths), 4)   # more than four copies: they scroll sideways
        tile_w = max(260, (view.width() - 18 * (shown - 1)) // shown - 34)
        tile_h = max(200, view.height() - 170)
        ratio = self.devicePixelRatioF()
        infos = []
        for path in paths:
            try:
                stat = os.stat(path)
                width, height = iot_utils.read_display_size(path)
            except Exception:
                stat, width, height = None, 0, 0
            taken = self.app._photo_info_for(path, stat)[0] if stat else None
            when = taken or (datetime.fromtimestamp(stat.st_mtime).strftime("%Y:%m:%d %H:%M:%S") if stat else "")
            infos.append((path, stat, width, height, when, bool(taken)))
        def only_best(key):   # a label only when one copy really stands out (identical copies get none)
            values = [key(i) for i in infos]
            best = max(values)
            return infos[values.index(best)][0] if values.count(best) == 1 else None
        largest = only_best(lambda i: (i[2] * i[3], i[1].st_size if i[1] else 0))
        newest = only_best(lambda i: i[4])
        for path, stat, width, height, when, from_camera in infos:
            try:
                scale = min(tile_w / max(1, width), tile_h / max(1, height))
                preview, _ = iot_utils.load_preview_image(path, max(1, int(width * scale * ratio)),
                                                          max(1, int(height * scale * ratio)))
                pixmap = pil_to_pixmap(preview)
                pixmap.setDevicePixelRatio(ratio)
            except Exception:
                pixmap = QPixmap()
            size = _size_text(stat.st_size) if stat else "?"
            date = self.app._readable_date(when) if when else ""
            details = f"{width} × {height} px  ·  {size}" + (f"\n{'Taken' if from_camera else 'Modified'} {date}" if date else "")
            badges = [b for b, chosen in (("Largest", largest), ("Newest", newest)) if chosen == path and len(paths) > 1]
            tile = _DupTile(self, path, pixmap, details, badges)
            tile.keep.setChecked(self.keep_state.get(path, True))
            tile.keep.toggled.connect(lambda on, p=path: self.keep_state.__setitem__(p, on))
            tile.setFixedWidth(tile_w + 34)
            line.addWidget(tile)
            self.tiles.append(tile)
        line.addStretch(1)
        self.scroll.setWidget(holder)
        self.update_buttons()

    def update_buttons(self):
        unticked = [t for t in self.tiles if not t.keep.isChecked()]
        kept = len(self.tiles) - len(unticked)
        self.delete_button.setEnabled(bool(unticked) and kept > 0)
        self.delete_button.setText(f"Delete {len(unticked)} Unticked" if unticked else "Delete Unticked")
        self.delete_button.setToolTip("Keep at least one copy" if unticked and not kept else
                                      "Delete the copies you've unticked (Undo brings them back)")

    def delete_unticked(self):
        row = self.list.currentRow()
        doomed = [t.path for t in self.tiles if not t.keep.isChecked()]
        if not doomed or len(doomed) == len(self.tiles):
            return
        deleted = set(self.app.delete_duplicate_copies(doomed))
        remaining = [p for p in self.sets[row] if p not in deleted]
        if len(remaining) > 1:
            self.sets[row] = remaining
        else:
            del self.sets[row]   # sorted out
        if not self.sets:
            self.accept()
            return
        self._fill_list(row)

    def showEvent(self, event):
        super().showEvent(event)
        self._relayout.start()   # the copies are sized to the window, which only has its real size now

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.isVisible():
            self._relayout.start()

    def show_in_folder(self, path):
        self.accept()
        self.app.show_picture(os.path.basename(path))

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Up, Qt.Key.Key_Down) and not self.list.hasFocus():
            step = -1 if event.key() == Qt.Key.Key_Up else 1
            self.list.setCurrentRow(max(0, min(self.list.count() - 1, self.list.currentRow() + step)))
            return
        super().keyPressEvent(event)
