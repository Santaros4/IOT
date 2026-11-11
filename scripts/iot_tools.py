import os
import threading
import tkinter as tk
from tkinter import ttk
from concurrent.futures import ThreadPoolExecutor
from PIL import Image

import iot_theme
import iot_history
import iot_utils

# Save each file back in the format its extension says.
_FORMAT_BY_EXTENSION = {'.jpg': 'JPEG', '.jpeg': 'JPEG', '.png': 'PNG', '.webp': 'WEBP', '.bmp': 'BMP'}


def _compute_new_size(w, h, mode, target_bound):
    """Short-edge scaling. Returns None when the image doesn't need touching."""
    if mode == "down" and not (w > target_bound and h > target_bound):
        return None
    if mode == "up" and not (w < target_bound or h < target_bound):
        return None
    if w < h:
        return target_bound, max(1, int(h * (target_bound / w)))
    return max(1, int(w * (target_bound / h))), target_bound


def _estimate_resolution(folder, files, mode, target_bound, base_image=5):
    """'6000 × 4000  →  2700 × 1800 px' for the base image (the last one in smaller folders), as the
    picture is shown. Header-only reads, so it's instant. Returns None if the image can't be read."""
    sample = files[base_image - 1] if len(files) >= base_image else files[-1]
    path = os.path.join(folder, sample)
    try:
        raw_w, raw_h = iot_utils.read_image_size(path)
        shown = iot_utils.read_display_size(path)
    except Exception:
        return None
    rotated = shown != (raw_w, raw_h)  # sideways camera shot: the resizer works on the stored size
    new_size = _compute_new_size(raw_w, raw_h, mode, target_bound)
    before = f"{shown[0]} × {shown[1]}"
    if new_size is None:
        return f"{before} px  (unchanged)"
    new_w, new_h = (new_size[1], new_size[0]) if rotated else new_size
    return f"{before}  →  {new_w} × {new_h} px"


def _resize_one(img_path, mode, target_bound, quality, cancel_event, stash_path, strip_exif=False):
    """
    Runs on a worker thread. Returns (status, undo_steps). status is 'converted', 'skipped',
    'failed' or 'cancelled'. The original file is moved to the session recycle folder (not
    overwritten), so the resize can be undone until I.O.T closes.
    """
    if cancel_event.is_set():
        return 'cancelled', []
    temp_path = img_path + ".iot_tmp"
    try:
        with Image.open(img_path) as img:
            new_size = _compute_new_size(img.size[0], img.size[1], mode, target_bound)
            if new_size is None:
                return 'skipped', []

            ext = os.path.splitext(img_path)[1].lower()
            save_format = _FORMAT_BY_EXTENSION.get(ext, img.format or 'JPEG')
            exif = img.info.get('exif')
            icc = img.info.get('icc_profile')
            orientation = iot_utils._orientation(img)

            if mode == "down":
                try:
                    img.draft(None, new_size)  # JPEG: decode at reduced scale, still >= target
                except Exception:
                    pass

            if img.mode in ('P', 'PA'):
                img = img.convert('RGBA' if (img.mode == 'PA' or 'transparency' in img.info) else 'RGB')
            resized = img.resize(new_size, Image.Resampling.LANCZOS, reducing_gap=3.0)
            if strip_exif:
                # Without EXIF the orientation tag is gone too, so turn the pixels upright instead
                resized = iot_utils._upright(resized, orientation)
                exif = None

        save_kwargs = {}
        if save_format == 'JPEG':
            if resized.mode not in ('RGB', 'L', 'CMYK'):
                resized = resized.convert('RGB')
            save_kwargs['quality'] = quality
            save_kwargs['optimize'] = True  # smarter encoding: a few % smaller, identical picture
        elif save_format == 'WEBP':
            save_kwargs['quality'] = quality
        if icc and save_format in ('JPEG', 'PNG', 'WEBP'):
            save_kwargs['icc_profile'] = icc
        if exif and save_format in ('JPEG', 'WEBP', 'PNG'):
            save_kwargs['exif'] = exif  # keeps camera orientation, so photos don't flip sideways

        resized.save(temp_path, format=save_format, **save_kwargs)

        # Keep the original for undo, then put the resized file in its place
        backup = stash_path(os.path.basename(img_path))
        iot_history.move_file(img_path, backup)
        try:
            os.replace(temp_path, img_path)
        except OSError:
            iot_history.move_file(backup, img_path)
            raise
        return 'converted', [("move", img_path, backup), ("created", img_path)]
    except Exception:
        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except OSError:
            pass
        return 'failed', []


def run_image_resizer_thread(app_instance, mode="down", folders=None):
    """
    Batch resizer. mode='down' bounds down to target; mode='up' scales up to target.
    folders: resize every image in these folders (e.g. a folder and all its sub-folders, from the folder
    list); by default just the folder on screen. Images are resized in place and keep their names.
    Work runs on background threads so the window stays responsive.
    """
    app = app_instance
    t = app.theme
    if getattr(app, 'busy', False):
        return
    if folders is None:
        jobs = [(app.image_folder, list(app.all_files))]
    else:
        jobs = [(path, iot_utils.list_images(path, app.valid_extensions)) for path in folders]
    jobs = [(path, names) for path, names in jobs if names]
    if not jobs:
        app.log_status("Resizer: no images to resize there.", 'warning')
        return

    title_word = "Downscale" if mode == "down" else "Upscale"
    target_bound = app.down_target if mode == "down" else app.up_target
    active_quality = app.jpg_quality  # same for both: quality 100 made upscales ~3x bigger for no visible gain
    folder = folders[0] if folders else app.image_folder  # the top folder (named in the dialog and the undo list)
    items = [(path, name) for path, names in jobs for name in names]  # (folder, file name) for every image
    files = [os.path.join(path, name) for path, name in items]
    top_name = os.path.basename(folder.rstrip("\\/")) or folder
    if folders is not None and len(folders) > 1:
        sub_count = len(folders) - 1
        where = f"'{top_name}' and its {sub_count} sub-folder{'s' if sub_count != 1 else ''}"
    else:
        where = f"'{top_name}'"
    image_word = "image" if len(files) == 1 else "images"
    in_folders = f" in {len(jobs)} folders" if len(jobs) > 1 else ""

    # ---- Confirmation ----
    confirmed = [False]
    confirm_window = iot_theme.make_dialog(app, f"{title_word} images")
    iot_theme.dialog_header(confirm_window, t, f"{title_word} {len(files)} {image_word}{in_folders}?",
                            f"Images in {where} will be resized in place, names unchanged, so their short edge is "
                            f"{target_bound} px{' (only ones larger than that)' if mode == 'down' else ' (only ones smaller than that)'}.")
    details = t.paint(tk.Frame(confirm_window, bd=0, highlightthickness=1), bg="surface_alt", highlightbackground="border")
    details.pack(fill=tk.X, padx=22, pady=(8, 0))
    base_image = getattr(app, "base_image", 5)
    sample_folder, sample_files = jobs[0]
    estimate = _estimate_resolution(sample_folder, sample_files, mode, target_bound, base_image)
    sample_name = f"{iot_utils.ordinal(base_image)} image" if len(sample_files) >= base_image else "Last image"
    if len(jobs) > 1:
        sample_name += f" of '{os.path.basename(sample_folder.rstrip(chr(92) + '/'))}'"
    rows = ([("Altered resolution", estimate), ("", f"Estimate from the {sample_name[0].lower() + sample_name[1:]}")]
            if estimate else [])
    for label, value in rows + [("Short edge", f"{target_bound} px"), ("JPEG / WebP quality", f"{active_quality}%"),
                         ("EXIF data", "Removed" if getattr(app, "strip_exif", False) else "Kept"),
                         ("Undo", "Originals are kept until I.O.T closes")]:
        row = t.paint(tk.Frame(details, bd=0), bg="surface_alt")
        row.pack(fill=tk.X, padx=12, pady=3)
        t.paint(tk.Label(row, text=label, font=t.f["small"], bd=0, width=20, anchor="w"), bg="surface_alt", fg="text_muted").pack(side=tk.LEFT)
        note = label == ""  # the small "estimate from…" line under Altered resolution
        t.paint(tk.Label(row, text=value, font=t.f["caption"] if note else t.f["small"], bd=0, anchor="w"),
                bg="surface_alt", fg="text_faint" if note else "text").pack(side=tk.LEFT)

    def handle_confirm_click(event=None):
        confirmed[0] = True
        confirm_window.destroy()

    iot_theme.dialog_buttons(confirm_window, t, [(title_word, handle_confirm_click, "primary"),
                                                 ("Cancel", confirm_window.destroy, "secondary")])
    confirm_window.bind("<Return>", handle_confirm_click)
    confirm_window.bind("<Escape>", lambda e: confirm_window.destroy())
    iot_theme.show_dialog(app, confirm_window, min_width=460)

    app.root.wait_window(confirm_window)
    if not confirmed[0]:
        return

    # ---- Progress window ----
    app.busy = True
    res_box = iot_theme.make_dialog(app, f"{title_word} in progress")
    iot_theme.dialog_header(res_box, t, f"{title_word.rstrip('e')}ing images…")
    body = t.paint(tk.Frame(res_box, bd=0), bg="surface")
    body.pack(fill=tk.X, padx=22, pady=(6, 0))
    status_label = t.paint(tk.Label(body, text=f"0 of {len(files)}", font=t.f["small"], bd=0, anchor="w"), bg="surface", fg="text_muted")
    status_label.pack(fill=tk.X, pady=(0, 6))
    p_bar = ttk.Progressbar(body, orient=tk.HORIZONTAL, mode='determinate', maximum=len(files), style="IOT.Horizontal.TProgressbar")
    p_bar.pack(fill=tk.X)

    cancel_event = threading.Event()
    futures = []

    def request_cancel():
        cancel_event.set()
        for future in futures:
            future.cancel()
        status_label.config(text="Cancelling… finishing the images already in progress")

    iot_theme.dialog_buttons(res_box, t, [("Cancel", request_cancel, "secondary")])
    res_box.protocol("WM_DELETE_WINDOW", request_cancel)
    iot_theme.show_dialog(app, res_box, min_width=460)

    # PIL releases the GIL while decoding/resizing/encoding, so threads give real parallelism here
    worker_count = max(2, min(8, os.cpu_count() or 4))
    executor = ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="iot-resize")
    strip_exif = getattr(app, "strip_exif", False)
    futures.extend(executor.submit(_resize_one, path, mode, target_bound, active_quality,
                                   cancel_event, app.session.stash_path, strip_exif) for path in files)
    executor.shutdown(wait=False)

    def poll_progress():
        done_count = sum(1 for future in futures if future.done())
        p_bar['value'] = done_count
        if not cancel_event.is_set():
            status_label.config(text=f"{done_count} of {len(files)}")
        if done_count < len(files):
            res_box.after(100, poll_progress)
            return
        finish()

    def finish():
        tally = {'converted': 0, 'skipped': 0, 'failed': 0, 'cancelled': 0}
        steps, changed, changed_folders = [], [], []
        for (path, name), future in zip(items, futures):
            if future.cancelled():
                tally['cancelled'] += 1
                continue
            status, file_steps = future.result()
            tally[status] += 1
            if file_steps:
                steps.extend(file_steps)
                changed.append(name)
                if path not in changed_folders:
                    changed_folders.append(path)
        try:
            res_box.grab_release()
            res_box.destroy()
        except tk.TclError:
            pass
        app.busy = False

        if tally['converted']:
            scope = f" in '{top_name}' and its sub-folders" if len(changed_folders) > 1 else ""
            app._record("Resize", f"{title_word}d {tally['converted']} images to {target_bound} px{scope}", folder, steps,
                        [iot_history.describe_range(changed, "image") + f"  ->  {target_bound} px short edge"]
                        + ([f"in {len(changed_folders)} folders"] if len(changed_folders) > 1 else [])
                        + ([f"{tally['skipped']} already within bounds, left unchanged"] if tally['skipped'] else []))
            for changed_folder in changed_folders:
                app.modified_folders.add(changed_folder)
                app.invalidate_folder_thumbnails(changed_folder)
            app.refresh_folders(changed_folders)
            if app.image_folder in changed_folders:
                app.load_directory_target_sequence(app.image_folder)
            else:
                app.refresh_folder_button()

        message = f"{title_word}: {tally['converted']} images resized"
        if tally['skipped']:
            message += f", {tally['skipped']} already within bounds"
        if tally['cancelled']:
            message += f", {tally['cancelled']} cancelled"
        if tally['failed']:
            message += f", {tally['failed']} failed (unreadable or locked)"
            app.log_status(message + ".", 'error')
        else:
            app.log_status(message + ".", 'success')

    res_box.after(100, poll_progress)
