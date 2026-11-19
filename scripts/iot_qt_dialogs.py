"""
I.O.T's windows other than the main one, in Qt: Settings, the Undo list, the small prompts, the progress
window, the resizer (confirmation + progress), Compress Folder (C) and the full-screen duplicates viewer.
The shortcut and flag tables shown in Settings are in iot_dialogs.py (shared with the Tk version).
"""
import os
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFontMetrics, QGuiApplication
from PySide6.QtWidgets import (QCheckBox, QDialog, QFileDialog, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit,
                               QProgressBar, QScrollArea, QSlider, QVBoxLayout, QWidget)

import iot_history
import iot_platform
import iot_tools
import iot_utils
from iot_dialogs import CONFIRMATIONS, FLAGS, SHORTCUTS
from iot_qt import (P, ElidedLabel, GradientDialog, MessageDialog, button, caps, half_tab_pixmap, keycap, label,
                    paint_gradient, pil_to_pixmap, transparent_box)


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
    if danger:   # Enter still confirms, as in the Tk version
        dialog.buttons[0].setDefault(True)
        dialog.buttons[0].setFocus()
    dialog.exec()
    return dialog.choice == 0


class TextPrompt(GradientDialog):
    """One text field. ask() returns the text, or None if cancelled."""

    def __init__(self, parent, title, subtitle, field_label, value, confirm_text):
        super().__init__(parent, title)
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
        outer.addLayout(_buttons_row(button("Cancel", callback=self.reject), ok))
        self.field.selectAll()
        self.field.setFocus()

    @classmethod
    def ask(cls, parent, title, subtitle, field_label, value, confirm_text):
        dialog = cls(parent, title, subtitle, field_label, value, confirm_text)
        return dialog.field.text() if dialog.exec() == QDialog.DialogCode.Accepted else None


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
    """Shortcuts reference, folder flags, and the processing / display settings (private copies, so Cancel
    really discards; applied on Save)."""

    def __init__(self, app):
        super().__init__(app, "Settings")
        self.app = app
        outer = QVBoxLayout(self)
        outer.setContentsMargins(28, 14, 28, 22)
        outer.setSpacing(12)
        outer.addWidget(label("Shortcuts, folder flags and processing options. (Hover for Details)", "muted"))
        columns = QHBoxLayout()
        columns.setSpacing(44)
        outer.addLayout(columns, 1)

        # --- left: shortcuts and flags
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
        left.addSpacing(8)
        left.addWidget(caps("Folder flags"))
        self.flag_boxes = {}
        flags = QGridLayout()
        flags.setHorizontalSpacing(10)
        flags.setVerticalSpacing(6)
        for row, (key, symbol, text) in enumerate(FLAGS):
            box = QCheckBox()
            box.setChecked(app.flags_enabled.get(key, True))
            box.setCursor(Qt.CursorShape.PointingHandCursor)
            self.flag_boxes[key] = box
            flags.addWidget(box, row, 0)
            flags.addWidget(label(symbol), row, 1)
            flags.addWidget(label(text, "muted"), row, 2)
        flags.setColumnStretch(2, 1)
        left.addLayout(flags)
        left.addStretch(1)

        # --- right: processing and display
        right = QVBoxLayout()
        right.setSpacing(8)
        columns.addLayout(right, 0)
        right.addWidget(caps("Processing"))
        fields = QGridLayout()
        fields.setHorizontalSpacing(12)
        fields.setVerticalSpacing(8)
        right.addLayout(fields)
        try:   # shown as a percentage of the average (130 = 30% above it); stored as the amount above (30)
            threshold = f"{float(app.anomaly_threshold_var.get()) + 100:g}"
        except ValueError:
            threshold = "130"
        self.edits = {}
        for row, (key, text, value, suffix, tip) in enumerate((
                ("base", "Base image", app.base_image, "which file in folder?",
                 "Intended to ensure calculations aren't based on thumbnails/covers etc"),
                ("threshold", f"{iot_platform.FLAG_SIZE} Unusual size threshold", threshold, "% of average",
                 "(Optional) Intended to help auto-detect potential issues"),
                ("target", f"{iot_platform.FLAG_TARGET} Target resolution", app.target_resolution, "px",
                 "(Optional) To check if folders match your typical preferred resolution"),
                ("down", "Downscale short edge to", app.down_target, "px",
                 "Your preferred resolution when Downscaling (Length or Width must be at least)"),
                ("up", "Upscale short edge to", app.up_target, "px",
                 "Your preferred resolution when Upscaling (Length or Width must be at least)"),
                ("quality", "JPEG quality", app.jpg_quality, "%",
                 "Significant file size decrease with no noticeable loss of quality"))):
            name = label(text)
            name.setToolTip(tip)
            edit = QLineEdit(str(value))
            edit.setFixedWidth(84)
            edit.setAlignment(Qt.AlignmentFlag.AlignCenter)
            edit.setToolTip(tip)
            fields.addWidget(name, row, 0)
            fields.addWidget(edit, row, 1)
            fields.addWidget(label(suffix, "muted"), row, 2)
            self.edits[key] = edit
        self.exif = QCheckBox("Strip EXIF data from resized images")
        self.exif.setChecked(app.strip_exif)
        self.exif.setToolTip("Removes information like Camera model & GPS co-ords, leave unchecked if unsure")
        right.addWidget(self.exif)
        right.addSpacing(10)
        right.addWidget(caps("Compress folder (C)"))
        zip_box, zip_row = transparent_box(QHBoxLayout, 8)
        zip_tip = ("Where the zip made by Compress Folder (C) goes. Leave it empty to put the zip next to the folder "
                   "(in the folder above it).")
        self.zip_dest = QLineEdit(app.zip_destination)
        self.zip_dest.setPlaceholderText("Next to the folder")
        self.zip_dest.setClearButtonEnabled(True)
        self.zip_dest.setMinimumWidth(260)
        self.zip_dest.setToolTip(zip_tip)
        zip_label = label("Zips go to")
        zip_label.setToolTip(zip_tip)
        zip_row.addWidget(zip_label)
        zip_row.addWidget(self.zip_dest, 1)
        zip_row.addWidget(button("Browse…", "small", "Choose the folder for compressed folders", self._browse_zip))
        right.addWidget(zip_box)
        right.addSpacing(10)
        right.addWidget(caps("Confirmations"))
        self.confirmations = dict(app.confirmations)   # private copy: applied on Save
        confirm_box, confirm_row = transparent_box(QHBoxLayout, 10)
        self.confirm_summary = label("", "muted")
        confirm_row.addWidget(self.confirm_summary, 1)
        choose = button("Choose…", "small", "Pick which actions ask \"are you sure?\" first", self._choose_confirmations)
        confirm_row.addWidget(choose)
        right.addWidget(confirm_box)
        self._show_confirm_summary()
        right.addSpacing(10)
        right.addWidget(caps("Display"))
        self.frost = QCheckBox("Frosted screen behind Zoom preview")
        self.frost.setChecked(app.frost_preview)
        self.frost.setToolTip("Blurs the rest of the screen while Hover + Hold Z shows an image. "
                              "Untick on slower computers if the preview feels sluggish.")
        self.sizes = QCheckBox("Folder sizes in the folder list")
        self.sizes.setChecked(app.tree_show_sizes)
        self.sizes.setToolTip("Shows how much space each folder uses in the folder drop-down list, "
                              "with a Σ total for folders that have sub-folders.")
        right.addWidget(self.frost)
        right.addWidget(self.sizes)
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

    def _show_confirm_summary(self):
        asking = sum(1 for key, *_ in CONFIRMATIONS if self.confirmations.get(key))
        self.confirm_summary.setText(f"{asking} of {len(CONFIRMATIONS)} actions ask first")

    def _choose_confirmations(self):
        dialog = ConfirmationsDialog(self, self.confirmations)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.confirmations = dialog.result_values()
            self._show_confirm_summary()

    def _browse_zip(self):
        start = self.zip_dest.text().strip() or os.path.dirname(self.app.image_folder.rstrip("\\/"))
        chosen = QFileDialog.getExistingDirectory(self, "Where should compressed folders go?", start)
        if chosen:
            self.zip_dest.setText(os.path.normpath(chosen))

    def save(self):
        app, edits = self.app, self.edits
        try:
            anomaly = float(edits["threshold"].text()) - 100     # back to "% above average"
            down, up = int(edits["down"].text()), int(edits["up"].text())
            quality = max(1, min(100, int(edits["quality"].text())))
            resolution, base = int(edits["target"].text()), int(edits["base"].text())
            if min(down, up, resolution, base) <= 0:
                raise ValueError
        except ValueError:
            self._fail("Every field needs a positive whole number.")
            return
        if anomaly < 0:
            self._fail("Unusual size threshold needs to be 100 or more (100 = the average folder size).")
            return
        zip_dest = self.zip_dest.text().strip().strip('"')
        if zip_dest and not os.path.isdir(zip_dest):
            self._fail("The folder for compressed folders doesn't exist. Choose one with Browse, or leave it empty "
                       "to put zips next to the folder.")
            return
        app.zip_destination = os.path.normpath(zip_dest) if zip_dest else ""
        app.confirmations = dict(self.confirmations)
        app.anomaly_threshold_var.set(f"{anomaly:g}")
        app.down_target, app.up_target, app.jpg_quality = down, up, quality
        app.target_resolution, app.base_image = resolution, base
        for key, box in self.flag_boxes.items():
            app.flags_enabled[key] = box.isChecked()
        app.frost_preview = self.frost.isChecked()
        app.tree_show_sizes = self.sizes.isChecked()
        app.strip_exif = self.exif.isChecked()
        app.scroll_speed = self.speed.value() * 10
        app.save_settings()
        self.accept()
        app.handle_anomaly_recalculation_event()


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
    """Batch resizer, as the Tk version: confirmation with an estimate, then a progress window while worker
    threads resize. folders: every image in these folders (a folder list branch); default: the folder on screen.
    Images are resized in place, keep their names, and the originals go to the session recycle folder."""
    if app.busy:
        return
    if folders is None:
        jobs = [(app.image_folder, list(app.all_files))]
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
    sample = f"{iot_utils.ordinal(app.base_image)} image" if len(sample_files) >= app.base_image else "last image"
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
        tally = {"converted": 0, "skipped": 0, "failed": 0, "cancelled": 0}
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


def _write_zip(temp_path, folder, files, folders, progress, cancel_event):
    """Worker thread: writes the zip. Paths inside it start with the folder's name, so unzipping gives the folder
    back. Returns ('done' | 'cancelled' | 'failed', [files that couldn't be read], error text)."""
    top = os.path.basename(folder.rstrip("\\/")) or "Folder"
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


def run_compress(app):
    """C / Compress Folder: a zip of the folder on screen (the folder itself, everything in it and its sub-folders),
    named after the folder, saved where Settings says (or next to the folder). Asks first; Undo deletes the zip."""
    if app.busy:
        return
    folder = app.image_folder
    top = os.path.basename(folder.rstrip("\\/")) or folder
    files, folders = _folder_contents(folder)
    if not files:
        notice(app, "Compress folder", f"'{top}' has no files to compress.")
        return
    total_bytes = 0
    for path in files:
        try:
            total_bytes += os.path.getsize(path)
        except OSError:
            pass

    chosen = app.zip_destination
    destination = chosen if chosen and os.path.isdir(chosen) else os.path.dirname(folder.rstrip("\\/"))
    name = zip_name_for(top, destination)
    final_path = os.path.join(destination, name + ".zip")
    contents = f"{len(files)} file{'s' if len(files) != 1 else ''} ({_size_text(total_bytes)})"
    if folders:
        contents += f" in {len(folders) + 1} folders"
    rows = [("Contents", contents), ("Saved as", final_path)]
    if name != top:
        rows.append(("", f"'{top}.zip' is already there, so letters were taken off the end of the name"))
    if chosen and destination != chosen:
        rows.append(("", f"The folder chosen in Settings isn't available ({chosen}), so the zip goes next to "
                         "the folder instead"))
    rows += [("Compression", "Pictures are stored as they are (they're compressed already); other files are "
                             "compressed"),
             ("Undo", "Deletes the zip. The folder itself isn't changed")]
    text = (f"Compress '{top}'?\n\nEverything in the folder, including its sub-folders, goes into one zip file, with "
            "the folder itself inside it.")
    if app.asks("compress") and not confirm(app, "Compress folder", text, "Compress", rows=rows):
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
        temp_path, folder, files, folders, progress, cancel_event))), daemon=True, name="iot-zip")
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

    timer.timeout.connect(poll)
    timer.start()


# ==================================================================== duplicates viewer (F)

class _DupTile(QFrame):
    def __init__(self, callback):
        super().__init__()
        self.callback = callback
        self.setObjectName("duptile")
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def mousePressEvent(self, event):
        self.callback()
        event.accept()


class DuplicatesViewer(QWidget):
    """Full screen, on the app's gradient: the base image and its matches side by side, each on a panel.
    Click one to jump to it; Esc, Close or a click on the background closes."""

    def __init__(self, app, paths):
        super().__init__(None, Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint)
        self.app = app
        self.setWindowTitle("Duplicates")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        screen = app.screen() or QGuiApplication.primaryScreen()
        area = screen.geometry()
        ratio = screen.devicePixelRatio()
        self.setGeometry(area)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(34, 28, 34, 30)
        outer.setSpacing(22)

        # header: what this is, how to use it, and Close
        head = QHBoxLayout()
        head.setSpacing(16)
        titles = QVBoxLayout()
        titles.setSpacing(2)
        titles.addWidget(label("Duplicates", "h2"))
        titles.addWidget(label(f"{len(paths)} matching images (98%+ match with the base image)  ·  click one to jump "
                               "to it in the grid  ·  Esc to close", "muted"))
        head.addLayout(titles, 1)
        head.addWidget(button("Close", "", "Back to the grid  (Esc)", self.close))
        outer.addLayout(head)

        row = QHBoxLayout()
        row.setSpacing(22)
        outer.addLayout(row, 1)
        tile_w = (area.width() - 68 - 22 * (len(paths) - 1)) // len(paths) - 36
        tile_h = area.height() - 300
        for number, path in enumerate(paths):
            try:
                orig_w, orig_h = iot_utils.read_display_size(path)
                scale = min(tile_w / orig_w, tile_h / orig_h)
                preview, _ = iot_utils.load_preview_image(path, max(1, int(orig_w * scale * ratio)),
                                                          max(1, int(orig_h * scale * ratio)))
                pixmap = pil_to_pixmap(preview)
                pixmap.setDevicePixelRatio(ratio)
            except Exception:
                continue
            tile = _DupTile(lambda name=os.path.basename(path): self.jump(name))
            box = QVBoxLayout(tile)
            box.setContentsMargins(18, 16, 18, 16)
            box.setSpacing(6)
            tag = caps("Base image" if number == 0 else f"Match {number}")
            tag.setAlignment(Qt.AlignmentFlag.AlignCenter)
            box.addWidget(tag)
            picture = QLabel()
            picture.setPixmap(pixmap)
            picture.setAlignment(Qt.AlignmentFlag.AlignCenter)
            box.addWidget(picture, 1)
            name = label(os.path.basename(path))
            name.setStyleSheet("font-weight: 700;")
            name.setAlignment(Qt.AlignmentFlag.AlignCenter)
            size = label(f"{orig_w} × {orig_h} px", "muted")
            size.setAlignment(Qt.AlignmentFlag.AlignCenter)
            box.addWidget(name)
            box.addWidget(size)
            row.addWidget(tile, 1)

    def paintEvent(self, event):
        paint_gradient(self)

    def jump(self, name):
        self.close()
        self.app._set_duplicate_highlight(name)
        self.app.jump_to_specific_image_filename(name)

    def mousePressEvent(self, event):   # the background (tiles handle their own clicks)
        self.close()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.close()
            return
        super().keyPressEvent(event)
