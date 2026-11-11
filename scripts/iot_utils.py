import os
import re
import sys
import time
import shutil
import hashlib
import threading
from PIL import Image, PngImagePlugin

# Single source of truth for which files count as images across every module
IMAGE_EXTENSIONS = ('.png', '.jpg', '.jpeg', '.webp', '.bmp')

_DIGIT_SPLIT = re.compile(r'([0-9]+)')


def natural_sort_key(text_string):
    """Sorts string keys using natural alphanumeric order matching Windows Explorer logic layout rules."""
    if not isinstance(text_string, str):
        return [text_string]

    # Case-insensitive, like File Explorer
    processed_string = text_string.lower()

    # Windows sorts brackets '[' before arithmetic operators like '+'
    if processed_string.startswith('['):
        processed_string = "\x01" + processed_string
    elif processed_string.startswith('+'):
        processed_string = "\x02" + processed_string

    # re.split with a capture group always alternates text, digits, text, digits...
    # so odd positions are the digit runs. Using the position (not str.isdigit) avoids
    # crashing on characters like '²' that isdigit() accepts but int() rejects.
    parts = _DIGIT_SPLIT.split(processed_string)
    return [int(chunk) if position % 2 else chunk for position, chunk in enumerate(parts)]


def scan_folder(folder_path, image_exts=IMAGE_EXTENSIONS):
    """
    One directory pass that gathers everything the folder tree needs:
    (sub-folder names, total byte size of files, [(image_name, mtime_ns), ...]).
    os.scandir gets size and mtime from the directory listing itself on Windows,
    so this costs no extra disk access per file.
    """
    subdirs, images, total_bytes = [], [], 0
    try:
        with os.scandir(folder_path) as stream:
            for entry in stream:
                try:
                    if entry.is_dir():
                        if not entry.name.startswith('.'):
                            subdirs.append(entry.name)
                    elif entry.is_file(follow_symlinks=False):
                        if entry.name.startswith('.iot_'):
                            continue  # I.O.T's own hidden files (e.g. folder tags) aren't part of the folder's size
                        stat = entry.stat(follow_symlinks=False)
                        total_bytes += stat.st_size
                        if entry.name.lower().endswith(image_exts):
                            images.append((entry.name, stat.st_mtime_ns))
                except OSError:
                    continue
    except OSError:
        pass
    return subdirs, total_bytes, images


def list_images(folder_path, image_exts=IMAGE_EXTENSIONS):
    """Returns the image file names in a folder, naturally sorted."""
    names = []
    try:
        with os.scandir(folder_path) as stream:
            for entry in stream:
                if entry.name.lower().endswith(image_exts):
                    try:
                        if entry.is_file():
                            names.append(entry.name)
                    except OSError:
                        continue
    except OSError:
        return []
    names.sort(key=natural_sort_key)
    return names


def read_image_size(image_path):
    """Reads only the file header, so this is fast even for huge images."""
    with Image.open(image_path) as img:
        return img.size


# Camera EXIF orientation -> the transpose that makes the picture upright
_ORIENTATION_FIX = {
    2: Image.Transpose.FLIP_LEFT_RIGHT,
    3: Image.Transpose.ROTATE_180,
    4: Image.Transpose.FLIP_TOP_BOTTOM,
    5: Image.Transpose.TRANSPOSE,
    6: Image.Transpose.ROTATE_270,
    7: Image.Transpose.TRANSVERSE,
    8: Image.Transpose.ROTATE_90,
}


def _orientation(img):
    try:
        return img.getexif().get(0x0112, 1)
    except Exception:
        return 1


def _upright(img, orientation):
    method = _ORIENTATION_FIX.get(orientation)
    return img.transpose(method) if method is not None else img


def _display_size(size, orientation):
    """Width x height as the picture is shown (sideways camera shots swap them)."""
    return (size[1], size[0]) if orientation in (5, 6, 7, 8) else size


def read_display_size(image_path):
    with Image.open(image_path) as img:
        return _display_size(img.size, _orientation(img))


def _to_displayable(img):
    """Tk can only show a handful of modes; convert anything else."""
    if img.mode in ('RGB', 'RGBA', 'L'):
        return img
    if img.mode in ('LA', 'PA') or (img.mode == 'P' and 'transparency' in img.info):
        return img.convert('RGBA')
    return img.convert('RGB')


def load_thumbnail(image_path, size):
    """
    Returns (thumbnail PIL image, original (w, h)). Safe to call from a worker thread.
    draft() makes the JPEG decoder skip most of the pixel data (it decodes at 1/2, 1/4 or 1/8
    scale directly), which is by far the biggest speed-up for large photos.
    """
    with Image.open(image_path) as img:
        orientation = _orientation(img)
        original_size = _display_size(img.size, orientation)
        try:
            img.draft('RGB', (size, size))
        except Exception:
            pass
        img.load()  # images already smaller than `size` aren't decoded by thumbnail(); do it before the file closes
        if img.mode in ('P', 'PA', 'LA', 'I', 'I;16', 'F', 'CMYK', 'YCbCr'):
            img = _to_displayable(img)
        img.thumbnail((size, size), Image.Resampling.LANCZOS, reducing_gap=2.0)
        # Rotating the small thumbnail is far cheaper than rotating the full image first
        return _upright(_to_displayable(img), orientation), original_size


def ordinal(n):
    """1 -> '1st', 2 -> '2nd', 11 -> '11th', 23 -> '23rd'."""
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def frosted_screen(width, height, tint):
    """
    A blurred, tinted copy of the main screen, used behind the Z preview. Kept cheap: the screenshot
    is box-averaged down 12x before blurring (so the blur works on a tiny image) and stretched back
    up, which adds to the softness. Safe to call from a worker thread. Returns None if it fails.
    """
    try:
        from PIL import ImageFilter
        import iot_platform
        screen = iot_platform.grab_screen()
        if screen is None:
            return None
        small = screen.convert("RGB").reduce(12)
        small = small.filter(ImageFilter.GaussianBlur(2))
        small = Image.blend(small, Image.new("RGB", small.size, tint), 0.45)
        return small.resize((width, height), Image.Resampling.BILINEAR)
    except Exception:
        return None


def load_preview_image(image_path, max_w, max_h):
    """Fit-to-height full-screen preview, scaled down to fit width if needed. Upright, like the grid."""
    with Image.open(image_path) as img:
        orientation = _orientation(img)
        orig_w, orig_h = _display_size(img.size, orientation)
        new_h = max_h
        new_w = int(orig_w * (max_h / orig_h))
        if new_w > max_w:
            new_h = int(new_h * (max_w / new_w))
            new_w = max_w
        new_w, new_h = max(1, new_w), max(1, new_h)
        raw_size = _display_size((new_w, new_h), orientation)  # size before turning it upright
        try:
            img.draft('RGB', raw_size)
        except Exception:
            pass
        img = _to_displayable(img)
        resized = img.resize(raw_size, Image.Resampling.LANCZOS, reducing_gap=3.0)
        return _upright(resized, orientation), (orig_w, orig_h)


def generate_image_difference_hash(image_path, hash_size=8):
    """
    64-bit perceptual difference hash (dHash) returned as an int, or None if unreadable.
    Compare two hashes with hamming_distance().
    """
    try:
        with Image.open(image_path) as img:
            try:
                img.draft('L', (hash_size * 32, hash_size * 32))
            except Exception:
                pass
            # Shrink cheaply first; running LANCZOS over a full-resolution image just to
            # get a 9x8 result was the slow part of the old version.
            img.thumbnail((256, 256), Image.Resampling.BILINEAR, reducing_gap=2.0)
            img_resized = img.convert('L').resize((hash_size + 1, hash_size), Image.Resampling.LANCZOS)
            pixels = img_resized.tobytes()

        value = 0
        row_len = hash_size + 1
        for row in range(hash_size):
            base = row * row_len
            for col in range(hash_size):
                value = (value << 1) | (1 if pixels[base + col] > pixels[base + col + 1] else 0)
        return value
    except Exception:
        return None


def hamming_distance(hash_a, hash_b):
    return bin(hash_a ^ hash_b).count('1')


def get_folder_byte_size(folder_path):
    """Calculates the total byte size volume of a directory by summing up its file attributes."""
    return scan_folder(folder_path)[1]


class ThumbDiskCache:
    """
    Thumbnails saved to disk (<parent hub>/.thumbs_cache) so folders you've opened before load
    almost instantly. One 300px copy is stored per image and scaled down for smaller zoom sizes.
    Entries are keyed by the file's path, size and modified time, so an edited image never shows
    an old thumbnail.
    """
    BASE_SIZE = 300
    VERSION = "v2"  # bump when the thumbnail format changes, so old entries are simply ignored

    def __init__(self, folder):
        self.folder = folder
        self._ready = False
        self._broken = False
        self._lock = threading.Lock()

    def _ensure_folder(self):
        if self._ready:
            return True
        with self._lock:
            if not self._ready and not self._broken:
                try:
                    os.makedirs(self.folder, exist_ok=True)
                    if sys.platform.startswith("win"):
                        import ctypes
                        ctypes.windll.kernel32.SetFileAttributesW(self.folder, 0x02)  # hidden
                    self._ready = True
                except OSError:
                    self._broken = True  # read-only drive etc.: just work without a disk cache
        return self._ready

    def _key(self, path, stat):
        raw = f"{self.VERSION}|{os.path.normcase(os.path.abspath(path))}|{stat.st_mtime_ns}|{stat.st_size}"
        return hashlib.sha1(raw.encode("utf-8", "surrogatepass")).hexdigest()[:32]

    @staticmethod
    def _parse_dims(text):
        try:
            if isinstance(text, bytes):
                text = text.decode("ascii", "ignore")
            w, h = str(text).lower().split("x")
            return int(w), int(h)
        except Exception:
            return None

    def load(self, path, size):
        """Returns (thumbnail, original (w, h)). Safe to call from worker threads."""
        stat = os.stat(path)
        key = self._key(path, stat)
        thumb = dims = None
        for ext in (".jpg", ".png"):
            cached = os.path.join(self.folder, key + ext)
            try:
                with Image.open(cached) as img:
                    img.load()
                    dims = self._parse_dims(img.info.get("comment") or img.info.get("iot_dims"))
                    thumb = img.copy()
                self._mark_used(cached)
                break
            except FileNotFoundError:
                continue
            except Exception:
                try:
                    os.remove(cached)  # damaged cache file; rebuild it below
                except OSError:
                    pass
        if thumb is None:
            thumb, dims = load_thumbnail(path, self.BASE_SIZE)
            self._save(key, thumb, dims)
        elif dims is None:
            dims = read_display_size(path)
        if max(thumb.size) > size:
            thumb.thumbnail((size, size), Image.Resampling.LANCZOS)
        return _to_displayable(thumb), dims

    @staticmethod
    def _mark_used(cached):
        """trim() drops the entries with the oldest time first, so freshen an entry when it's used
        (at most once a day, to keep disk writes down) and the most-used thumbnails survive."""
        try:
            if os.stat(cached).st_mtime < time.time() - 86400:
                os.utime(cached, None)
        except OSError:
            pass

    def prebuild(self, path):
        """
        Makes sure an image has a cache entry, without returning it (used by iot_precache.py).
        Returns (status, error): status is 'cached' (already there), 'built' or 'failed'.
        """
        try:
            key = self._key(path, os.stat(path))
            if any(os.path.exists(os.path.join(self.folder, key + ext)) for ext in (".jpg", ".png")):
                return "cached", None
            thumb, dims = load_thumbnail(path, self.BASE_SIZE)
            if not self._save(key, thumb, dims):
                return "failed", "could not write to the cache folder"
            return "built", None
        except Exception as err:
            return "failed", str(err) or err.__class__.__name__

    def _save(self, key, thumb, dims):
        if not self._ensure_folder():
            return False
        dims_text = f"{dims[0]}x{dims[1]}"
        has_alpha = thumb.mode in ("RGBA", "LA")
        final = os.path.join(self.folder, key + (".png" if has_alpha else ".jpg"))
        temp = final + f".{threading.get_ident()}.tmp"
        try:
            if has_alpha:
                info = PngImagePlugin.PngInfo()
                info.add_text("iot_dims", dims_text)
                thumb.save(temp, "PNG", pnginfo=info, compress_level=1)
            else:
                image = thumb if thumb.mode in ("RGB", "L") else thumb.convert("RGB")
                image.save(temp, "JPEG", quality=88, comment=dims_text)
            os.replace(temp, final)
            return True
        except Exception:
            try:
                os.remove(temp)
            except OSError:
                pass
            return False

    def delete(self):
        shutil.rmtree(self.folder, ignore_errors=True)

    def trim(self, max_bytes=3_000_000_000, target_bytes=2_500_000_000):
        """When the cache is kept between sessions, drop the least recently used entries once it grows
        past ~3 GB (roughly 100,000 thumbnails), so a big pre-built cache isn't thrown away."""
        try:
            entries = []
            with os.scandir(self.folder) as stream:
                for entry in stream:
                    if entry.is_file():
                        stat = entry.stat()
                        entries.append((stat.st_mtime, stat.st_size, entry.path))
        except OSError:
            return
        total = sum(size for _, size, _ in entries)
        if total <= max_bytes:
            return
        for _, size, path in sorted(entries):
            try:
                os.remove(path)
                total -= size
            except OSError:
                continue
            if total <= target_bytes:
                break
