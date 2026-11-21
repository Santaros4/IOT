"""The resizer's work (the dialogs that drive it are in iot_qt_dialogs.run_resizer)."""
import os
from PIL import Image

import iot_history
import iot_utils

# Save each file back in the format its extension says.
_FORMAT_BY_EXTENSION = {'.jpg': 'JPEG', '.jpeg': 'JPEG', '.png': 'PNG', '.webp': 'WEBP', '.bmp': 'BMP', '.gif': 'GIF',
                        '.avif': 'AVIF', '.heic': 'HEIF', '.heif': 'HEIF'}
_QUALITY_FORMATS = ('JPEG', 'WEBP', 'AVIF', 'HEIF')   # formats the JPEG / WebP quality setting applies to


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


# EXIF orientation after turning a picture a quarter turn clockwise / anticlockwise (non-mirrored values only)
_TURN_CLOCKWISE = {1: 6, 6: 3, 3: 8, 8: 1}
_TURN_ANTICLOCKWISE = {value: key for key, value in _TURN_CLOCKWISE.items()}


def _jpeg_with_orientation(data, value):
    """The JPEG bytes with their EXIF orientation tag changed to `value`, nothing else touched (lossless), or None
    when the file has no orientation tag to change."""
    if data[:2] != b"\xff\xd8":
        return None
    pos = 2
    while pos + 4 <= len(data) and data[pos] == 0xFF:
        marker = data[pos + 1]
        if marker in (0xD9, 0xDA):          # end of image / start of the picture data: no EXIF before it
            return None
        length = int.from_bytes(data[pos + 2:pos + 4], "big")
        segment = pos + 4
        if marker == 0xE1 and data[segment:segment + 6] == b"Exif\x00\x00":
            tiff = segment + 6
            order = "little" if data[tiff:tiff + 2] == b"II" else "big"
            ifd = tiff + int.from_bytes(data[tiff + 4:tiff + 8], order)
            count = int.from_bytes(data[ifd:ifd + 2], order)
            for number in range(count):
                entry = ifd + 2 + number * 12
                if int.from_bytes(data[entry:entry + 2], order) == 0x0112:
                    if int.from_bytes(data[entry + 2:entry + 4], order) != 3:   # not a SHORT: leave it alone
                        return None
                    out = bytearray(data)
                    out[entry + 8:entry + 10] = value.to_bytes(2, order)
                    return bytes(out)
            return None
        pos = segment + length - 2
    return None


def _rotate_one(img_path, clockwise, quality, stash_path):
    """
    Turns a picture a quarter turn. Returns (status, undo_steps, lossless): status 'rotated', 'animated' (left alone)
    or 'failed'. JPEGs that carry an EXIF orientation tag just get the tag changed (no quality lost); anything else
    is turned and saved again (PNG / BMP / GIF stay lossless, JPEG / WebP / AVIF / HEIC are re-saved at the quality
    setting). The original goes to the session recycle folder, so Undo puts it back.
    """
    temp_path = img_path + ".iot_tmp"
    try:
        with Image.open(img_path) as img:
            if getattr(img, "is_animated", False):
                return 'animated', [], False
            orientation = iot_utils._orientation(img)
            ext = os.path.splitext(img_path)[1].lower()
            save_format = _FORMAT_BY_EXTENSION.get(ext, img.format or 'JPEG')
            turn = _TURN_CLOCKWISE if clockwise else _TURN_ANTICLOCKWISE
            new_bytes = None
            if save_format == 'JPEG' and orientation in turn:
                with open(img_path, "rb") as handle:
                    new_bytes = _jpeg_with_orientation(handle.read(), turn[orientation])
            if new_bytes is None:
                icc = img.info.get('icc_profile')
                exif = img.getexif()
                picture = iot_utils._upright(img.copy(), orientation)   # as it's shown, then a quarter turn
                if picture.mode in ('P', 'PA') and save_format not in ('GIF', 'PNG'):
                    picture = picture.convert('RGBA' if picture.mode == 'PA' or 'transparency' in img.info else 'RGB')
                picture = picture.transpose(Image.Transpose.ROTATE_270 if clockwise else Image.Transpose.ROTATE_90)
                if 0x0112 in exif:
                    exif[0x0112] = 1        # the pixels are upright now
                save_kwargs = {}
                if save_format in _QUALITY_FORMATS:
                    save_kwargs['quality'] = quality
                if save_format == 'JPEG' and picture.mode not in ('RGB', 'L', 'CMYK'):
                    picture = picture.convert('RGB')
                if icc and save_format in ('JPEG', 'PNG', 'WEBP', 'AVIF', 'HEIF'):
                    save_kwargs['icc_profile'] = icc
                if len(exif) and save_format in ('JPEG', 'WEBP', 'PNG', 'AVIF', 'HEIF'):
                    save_kwargs['exif'] = exif.tobytes()
                picture.save(temp_path, format=save_format, **save_kwargs)
        if new_bytes is not None:
            with open(temp_path, "wb") as handle:
                handle.write(new_bytes)
        backup = stash_path(os.path.basename(img_path))
        iot_history.move_file(img_path, backup)
        try:
            iot_history.retry_if_busy(os.replace, temp_path, img_path)
        except OSError:
            iot_history.move_file(backup, img_path)
            raise
        return 'rotated', [("move", img_path, backup), ("created", img_path)], new_bytes is not None
    except Exception:
        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except OSError:
            pass
        return 'failed', [], False


def _resize_one(img_path, mode, target_bound, quality, cancel_event, stash_path, strip_exif=False):
    """
    Runs on a worker thread. Returns (status, undo_steps). status is 'converted', 'skipped', 'animated'
    (left alone: saving would keep only the first frame), 'failed' or 'cancelled'. The original file is moved to the session recycle folder (not
    overwritten), so the resize can be undone until I.O.T closes.
    """
    if cancel_event.is_set():
        return 'cancelled', []
    temp_path = img_path + ".iot_tmp"
    try:
        with Image.open(img_path) as img:
            if getattr(img, "is_animated", False):
                return 'animated', []
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
        elif save_format in _QUALITY_FORMATS:
            save_kwargs['quality'] = quality
        if icc and save_format in ('JPEG', 'PNG', 'WEBP', 'AVIF', 'HEIF'):
            save_kwargs['icc_profile'] = icc
        if exif and save_format in ('JPEG', 'WEBP', 'PNG', 'AVIF', 'HEIF'):
            save_kwargs['exif'] = exif  # keeps camera orientation, so photos don't flip sideways

        resized.save(temp_path, format=save_format, **save_kwargs)

        # Keep the original for undo, then put the resized file in its place
        backup = stash_path(os.path.basename(img_path))
        iot_history.move_file(img_path, backup)
        try:
            iot_history.retry_if_busy(os.replace, temp_path, img_path)
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
