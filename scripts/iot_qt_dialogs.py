"""
I.O.T's windows other than the main one, in Qt: Settings, the Undo list, the small prompts, the progress
window, the resizer (confirmation + progress) and the full-screen duplicates viewer.
The shortcut and flag tables shown in Settings are in iot_dialogs.py (shared with the Tk version).
"""
import os
import threading
from concurrent.futures import ThreadPoolExecutor

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFontMetrics, QGuiApplication
from PySide6.QtWidgets import (QCheckBox, QDialog, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit,
                               QProgressBar, QScrollArea, QSlider, QVBoxLayout, QWidget)

import iot_history
import iot_platform
import iot_tools
import iot_utils
from iot_dialogs import FLAGS, SHORTCUTS
from iot_qt import (P, GradientDialog, MessageDialog, button, caps, half_tab_pixmap, keycap, label, paint_gradient,
                    pil_to_pixmap, transparent_box)


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
        outer.addLayout(_buttons_row(button("Close", callback=self.close)))
        self.setMinimumWidth(680)
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
            text, text_box = transparent_box(QVBoxLayout, 2)
            title = label(record.title, "faint" if record.reverted else "")
            title.setWordWrap(True)
            try:
                where = os.path.relpath(record.folder, os.path.dirname(app.parent_hub.rstrip("\\/")))
            except ValueError:
                where = record.folder
            text_box.addWidget(title)
            text_box.addWidget(label(f"{app.time_ago(record.time)}  ·  {where}", "muted"))
            line.addWidget(text, 1)
            if record.reverted:
                line.addWidget(label("Reverted", "faint"))
            else:
                line.addWidget(button("Revert", "small", callback=lambda r=record: app.perform_undo(r)))
            rows.addWidget(row)
        rows.addStretch(1)
        self.scroll.setWidget(holder)
        # As tall as the list, up to ~60% of the screen; beyond that it scrolls
        holder.adjustSize()
        screen = (self.screen() or QGuiApplication.primaryScreen()).availableGeometry()
        self.scroll.setFixedHeight(min(holder.sizeHint().height() + 4, int(screen.height() * 0.6)))
        if self.isVisible():
            self.adjustSize()

    def closeEvent(self, event):
        self.app._undo_window = None
        super().closeEvent(event)

    def reject(self):     # Esc
        self.close()


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
    if not confirm(app, f"{word} images", text, word, rows=rows):
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
