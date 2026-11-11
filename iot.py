import os
import sys
import re
import json
import time
import heapq
import queue
import shutil
import ctypes
import threading
import traceback
import subprocess
import tkinter as tk
import tkinter.font as tkfont
from collections import OrderedDict
from datetime import datetime
from tkinter import ttk
from concurrent.futures import ThreadPoolExecutor
from PIL import Image, ImageTk

# Our own modules live in the 'scripts' folder next to this file (keeps this folder down to a few items)
SCRIPTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts")
sys.path.insert(0, SCRIPTS_DIR)

import iot_utils
import iot_dialogs
import iot_tools
import iot_theme
import iot_history
import iot_platform
from iot_theme import Button, CheckBox, ThemeToggle, Slider, Logo, caps_label, vertical_divider

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
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


def log_crash(kind, exc_type, exc, tb):
    """Writes an error to iot_crash.log with the system details, so problems on a Mac or Linux machine
    (where there's often no console to see them) can be sent back. Still printed as before, too."""
    traceback.print_exception(exc_type, exc, tb)
    try:
        import platform
        import PIL
        if os.path.exists(CRASH_LOG_FILE) and os.path.getsize(CRASH_LOG_FILE) > 1_000_000:
            os.replace(CRASH_LOG_FILE, CRASH_LOG_FILE + ".old")
        with open(CRASH_LOG_FILE, "a", encoding="utf-8") as handle:
            handle.write(f"{datetime.now():%Y-%m-%d %H:%M:%S}  {kind}  |  {platform.platform()}  |  "
                         f"Python {platform.python_version()}  |  Tk {tk.TkVersion}  |  Pillow {PIL.__version__}\n")
            handle.write("".join(traceback.format_exception(exc_type, exc, tb)) + "\n")
    except Exception:
        pass

TILE_OVERHEAD = 40              # horizontal pixels a tile needs beyond its thumbnail (padding, border, gaps)
BUFFER_ROWS = 2                 # rows of real tiles kept just above/below the screen
PREFETCH_SCREENS = 1            # screens of thumbnails decoded ahead (above and below) so scrolling is instant
THUMB_CACHE_BYTES = 160_000_000 # RAM budget for decoded thumbnails; least recently used are dropped first
INVALID_NAME_CHARS = set('<>:"/\\|?*')
WINDOWS_RESERVED_NAMES = {"con", "prn", "aux", "nul"} | {f"{p}{n}" for p in ("com", "lpt") for n in range(1, 10)}
THUMB_RETRY_SECONDS = 3         # a thumbnail that couldn't be read (e.g. drive spinning up) is tried again after this
THUMB_RETRY_LIMIT = 5           # ...this many times, then it's treated as a broken file
TAG_COLORS = ("red", "orange", "yellow", "green", "blue", "purple", "grey")  # folder tags; "tag_<name>" in the theme
TAGS_FILE_NAME = ".iot_tags.json"   # hidden file in the hub: {"tags": {"relative/folder": "green"}}
HEAL_INTERVAL_MS = 3000         # how often on-screen tiles still waiting for their picture are asked for again
UNDO_KEYS = iot_platform.keys("Ctrl+Z")   # Cmd+Z on macOS


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

FLAG_TARGET, FLAG_SIZE, FLAG_MODIFIED = iot_platform.FLAG_TARGET, iot_platform.FLAG_SIZE, iot_platform.FLAG_MODIFIED


class Tile:
    """
    One on-screen thumbnail cell. Only enough tiles to cover the visible rows exist; as you scroll,
    tiles that leave the screen are handed the next files coming into view. Tk cannot draw a widget
    taller than ~32,000 px, which is why one big frame holding every tile stopped showing images
    after several hundred.
    """
    __slots__ = ("filename", "frame", "img_label", "txt_label", "photo", "loaded", "item_id", "idx")


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


class DarkGridImageSorter:
    def __init__(self, root):
        self.root = root
        self.root.title("I.O.T  —  Image Organization Toolkit")
        # Errors inside button / key handlers are logged (the app keeps running, as before)
        self.root.report_callback_exception = lambda exc_type, exc, tb: log_crash("error", exc_type, exc, tb)
        self.root.minsize(980, 560)

        # Keep the window invisible (but still drawn) until it has finished setting itself up, so the
        # one-off maximise refresh in _finish_startup can't be seen as a flicker.
        self._set_window_alpha(0.0)
        self.root.after(4000, lambda: self._set_window_alpha(1.0))  # safety net: never stay invisible

        # Maximise to the monitor's usable area (taskbar excluded).
        self._maximize_window()
        self._frame_refreshed = False

        # --- ENVIRONMENT PATHS ---
        self.parent_hub = self._resolve_start_folder()
        self.image_folder = self.parent_hub
        self.cache_folder = os.path.join(self.parent_hub, '.thumbs_cache')
        self.settings_path = SETTINGS_FILE
        self.change_log_path = CHANGE_LOG_FILE
        self.output_base = self.parent_hub
        self.valid_extensions = iot_utils.IMAGE_EXTENSIONS

        # --- SETTINGS (all persisted to iot_settings.json) ---
        self.anomaly_threshold_var = tk.StringVar(value="30")
        self.down_target = 1800
        self.up_target = 1800
        self.jpg_quality = 90
        self.target_resolution = 1800
        self.thumb_size = 201
        self.rename_var = tk.BooleanVar(value=True)
        self.skip_folder_var = tk.BooleanVar(value=False)
        self.keep_cache_var = tk.BooleanVar(value=True)   # "Disable Cache Deletion", on by default
        self.flags_enabled = {"target": True, "size": True, "modified": True}  # folder flags shown (Settings)
        self.frost_preview = True         # frosted screen behind the Hover + Hold Z preview (Settings)
        self.base_image = 5               # checks use the Nth picture, so covers / thumbnails at the start are skipped
        self.tree_show_sizes = True       # folder sizes in the folder list (Settings)
        self.strip_exif = False           # resized images saved without EXIF (camera model, GPS...) (Settings)
        self.scroll_speed = 100           # % of the normal mouse-wheel step in the picture grid (Settings)
        theme_name = self.load_settings()

        self.theme = iot_theme.Theme(root, theme_name)
        self._settings_base = self._current_settings()  # what was loaded; see save_settings
        self.theme.paint(self.root, bg="bg")
        self.apply_title_bar_theme(self.root)

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
        self._tag_picker = None
        self._load_folder_tags()
        self._struck_fonts = {}
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
        self._group_anim_job = None
        self._group_anim_phase = 0.0
        self.image_cache = OrderedDict()  # (folder, filename, size) -> PhotoImage, least recently used first
        self.dims_cache = {}              # (folder, filename) -> original (w, h)
        self._failed_thumbs = set()       # (folder, filename) that couldn't be decoded; not retried
        self._thumb_retries = {}          # (folder, filename) -> (retry after, attempts) for reads that failed for now
        self.hovered_filename = None
        self._hover_tile = None
        self.preview_window = None
        self.preview_photo = None
        self.preview_backdrop = None      # frosted copy of the screen behind the Z preview
        self._frost_photo = None
        self._frost_pool = ThreadPoolExecutor(max_workers=1)
        self.z_key_is_pressed = False
        self._z_release_job = None
        self.active_duplicate_filename = None
        self.busy = False             # True while a background batch (resize / duplicate scan) runs
        self.tree_popup_window = None
        self._tree_canvas = None
        self._tree_closed_at = 0.0
        self._undo_window = None
        self.last_folder_name = "Sequence"

        # Virtual grid layout
        self._visible = {}            # file index -> Tile currently shown
        self._visible_by_name = {}
        self._tile_pool = []          # hidden tiles ready for reuse
        self._cols = 1
        self._cell_w = 200.0
        self._row_h = 260
        self._rows = 0
        self._row_height_cache = {}
        self._last_ranges = None
        self._last_yview = None
        self._view_job = None
        self._hover_job = None
        self._save_job = None
        self._empty_text_id = None
        self._resize_job = None
        self._zoom_job = None
        self._last_canvas_width = 0
        self._startup_done = False
        self._placeholders = {}
        self._error_images = {}
        self._thumb_queue = queue.Queue()
        worker_count = max(2, min(6, (os.cpu_count() or 4) - 1))
        self._thumb_loader = ThumbnailLoader(self._thumb_queue, worker_count, self.disk_cache)

        # Every option change is saved straight away
        for var in (self.rename_var, self.skip_folder_var, self.keep_cache_var):
            var.trace_add("write", lambda *a: self.save_settings())

        # Build the interface
        self.create_header()
        self.create_toolbar()
        self.create_status_bar()
        self.create_scrollable_grid()
        self.create_right_click_context_menu()
        self._create_tooltip()
        iot_platform.enable_drop(self.root, self.open_dropped_paths)  # macOS / Linux: needs tkinterdnd2
        iot_platform.after_window_built(self.root)
        self.load_directory_target_sequence(self.image_folder)  # grid is drawn once the window is sized

        # Keyboard event hooks
        for event in iot_platform.DELETE_EVENTS:  # Delete (and Backspace on Mac keyboards)
            self.root.bind(event, self.handle_keyboard_delete)
        for key in ("z", "Z"):
            self.root.bind(f"<KeyPress-{key}>", self.handle_z_press)
            self.root.bind(f"<KeyRelease-{key}>", self.handle_z_release)
        for key in ("r", "R"):
            self.root.bind(f"<Key-{key}>", lambda e: self.trigger_active_folder_rename())
        for key in ("f", "F"):
            self.root.bind(f"<Key-{key}>", lambda e: self.trigger_duplicate_hash_scan())
        for key in ("q", "Q"):
            self.root.bind(f"<Key-{key}>", lambda e: self.trigger_folder_delete())
        for key in ("g", "G"):
            self.root.bind(f"<Key-{key}>", lambda e: self.toggle_group_mode())
        self.root.bind("<Return>", lambda e: self.copy_group_selection())
        self.root.bind("<KP_Enter>", lambda e: self.copy_group_selection())
        self.root.bind("<Escape>", lambda e: self.toggle_group_mode() if self.group_mode else None)
        for key in ("d", "D"):
            self.root.bind(f"<Key-{key}>", lambda e: self.run_resizer("down"))
        for key in ("u", "U"):
            self.root.bind(f"<Key-{key}>", lambda e: self.run_resizer("up"))
        self.root.bind("<F5>", lambda e: self.refresh())
        self.root.bind(f"<{iot_platform.MOD}-z>", lambda e: self.undo_latest())   # Ctrl+Z (Cmd+Z on macOS)
        self.root.bind(f"<{iot_platform.MOD}-Z>", lambda e: self.undo_latest())
        self.root.bind_all("w", lambda e: self.shift_active_directory_index(-1))
        self.root.bind_all("s", lambda e: self.shift_active_directory_index(1))
        self.root.bind_all("W", lambda e: self.shift_active_directory_index(-1))
        self.root.bind_all("S", lambda e: self.shift_active_directory_index(1))

        # The window's close button cleans up (recycle folder, and the cache unless it's being kept)
        self.root.protocol("WM_DELETE_WINDOW", self.handle_manual_exit_cleanup)
        self.root.after(15, self._drain_thumb_queue)
        self.root.after(HEAL_INTERVAL_MS, self._heal_thumbnails_loop)
        self.root.bind("<FocusIn>", lambda e: self._heal_thumbnails(retry_now=True) if e.widget is self.root else None,
                       add="+")
        self.root.after(50, self._finish_startup)

    # ------------------------------------------------------------------ theme colour shortcuts
    # (kept so older code that used these names still gets the right colour for the current theme)

    @property
    def bg_dark(self):
        return self.theme.c["bg"]

    @property
    def bg_panel(self):
        return self.theme.c["surface"]

    @property
    def fg_white(self):
        return self.theme.c["text"]

    @property
    def accent_blue(self):
        return self.theme.c["accent"]

    @property
    def tile_bg(self):
        return self.theme.c["tile"]

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
            "theme": self.theme.name if hasattr(self, "theme") else "dark",
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
                self.log_status(f"Settings could not be saved: {err}", 'warning')

    def _save_settings_soon(self):
        if self._save_job is not None:
            self.root.after_cancel(self._save_job)
        self._save_job = self.root.after(600, self._save_settings_now)

    def _save_settings_now(self):
        self._save_job = None
        self.save_settings()

    def _maximize_window(self):
        iot_platform.maximize(self.root)

    def _set_window_alpha(self, value):
        try:
            self.root.attributes('-alpha', value)
        except tk.TclError:
            pass

    def _refresh_maximized_frame(self):
        """
        Windows leaves a white strip down the right edge of a Tk window that starts maximised.
        Restoring and re-maximising once (what pressing maximise twice does) clears it; this is done
        at startup while the window is still transparent.
        """
        if not sys.platform.startswith("win"):
            return
        try:
            if self.root.state() == 'zoomed':
                self.root.state('normal')
                self.root.update_idletasks()
                self.root.state('zoomed')
                self.root.update()
        except tk.TclError:
            pass

    def _finish_startup(self, attempts=0, last_width=None):
        """Draws the grid only once the maximised window has settled on its final size."""
        if not self._frame_refreshed:
            self._frame_refreshed = True
            self._refresh_maximized_frame()
        self.root.update_idletasks()
        width = self.canvas.winfo_width()
        if (width < 200 or width != last_width) and attempts < 40:
            self.root.after(60, lambda: self._finish_startup(attempts + 1, width))
            return
        self._startup_done = True
        self._last_canvas_width = width
        self.apply_title_bar_theme(self.root)
        self.handle_auto_snap_zoom()
        self.root.update_idletasks()
        self._set_window_alpha(1.0)
        self.root.after(400, self._check_leftover_recycle)

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
        """Updates the folder selector (breadcrumb + flags + image count) and the status bar info."""
        info = self.folder_info.get(self.image_folder)
        hub_name = os.path.basename(self.parent_hub.rstrip("\\/")) or self.parent_hub
        try:
            relative = os.path.relpath(self.image_folder, self.parent_hub)
        except ValueError:
            relative = self.image_folder
        parts = [] if relative in (".", "") else relative.split(os.sep)
        if parts:
            self._selector_path_text = "  ›  ".join([hub_name] + parts[:-1]) + "  ›  "
            self.folder_name_label.config(text=parts[-1])
        else:
            self._selector_path_text = ""
            self.folder_name_label.config(text=hub_name)
        self.folder_flags_label.config(text=self._folder_flags(info).strip())
        tag = self.folder_tag(self.image_folder)
        if tag:
            self._draw_tag_dot(self.folder_tag_dot, self.theme.c["surface_alt"], tag)
            self.folder_tag_dot.pack(side=tk.LEFT, padx=(0, 4), before=self.folder_name_label)
        else:
            self.folder_tag_dot.pack_forget()
        pending = self._is_pending_delete(self.image_folder)
        self.folder_name_label.config(font=self._struck_font(self.theme.f["body_strong"]) if pending
                                      else self.theme.f["body_strong"])
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
        self._selector_count_text = count_text
        self._fit_folder_selector()
        self._update_status_info()

    def _fit_folder_selector(self, event=None):
        """Keeps the folder name readable when the box is narrow (e.g. half-window mode): the image count
        goes first (it's in the status bar too), then the path is shortened to '…', then hidden."""
        path_text = getattr(self, "_selector_path_text", "")
        count_text = getattr(self, "_selector_count_text", "")
        selector = self.folder_selector
        available = selector.winfo_width()
        if available <= 1:  # not laid out yet: show everything, re-fitted on the first <Configure>
            self.folder_path_label.config(text=path_text)
            self.folder_count_label.config(text=count_text)
            return
        if not hasattr(self, "_selector_fonts"):  # made once: creating Tk fonts on every <Configure> adds up
            self._selector_fonts = (tkfont.Font(font=self.folder_path_label.cget("font")),
                                    tkfont.Font(font=self.folder_count_label.cget("font")))
        font, small = self._selector_fonts
        fixed = sum(w.winfo_reqwidth() for w in (self.folder_name_label, self.folder_flags_label))
        fixed += self.folder_tag_dot.winfo_reqwidth() if self.folder_tag_dot.winfo_ismapped() else 0
        fixed += 18 + 20 + 16 + 20 + 12  # folder icon, padding, chevron and its padding, borders
        for path, count in ((path_text, count_text), (path_text, ""), ("…  ›  " if path_text else "", ""), ("", "")):
            if fixed + font.measure(path) + (small.measure(count) + 10 if count else 0) <= available:
                break
        self.folder_path_label.config(text=path)
        self.folder_count_label.config(text=count)

    @staticmethod
    def _norm(path):
        return os.path.normcase(os.path.normpath(path)).rstrip("\\/")

    # ------------------------------------------------------------------ window chrome

    def apply_title_bar_theme(self, window):
        """
        Matches the Windows title bar and window border to the current theme. Windows only repaints
        a window frame when its size changes, so the frame is told to redraw itself (SWP_FRAMECHANGED).
        """
        if not sys.platform.startswith("win"):
            return
        try:
            window.update_idletasks()
            user32 = ctypes.windll.user32
            dwmapi = ctypes.windll.dwmapi
            hwnd = user32.GetParent(window.winfo_id())
            c = self.theme.c

            def colorref(hex_color):
                r, g, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
                return ctypes.c_uint(r | (g << 8) | (b << 16))

            dark = ctypes.c_int(1 if self.theme.is_dark else 0)
            for attribute in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE (19 on early Windows 10)
                if dwmapi.DwmSetWindowAttribute(hwnd, attribute, ctypes.byref(dark), ctypes.sizeof(dark)) == 0:
                    break
            # Windows 11 only (ignored on Windows 10): border, caption and caption text colours
            for attribute, color in ((34, c["border"]), (35, c["surface"]), (36, c["text"])):
                value = colorref(color)
                dwmapi.DwmSetWindowAttribute(hwnd, attribute, ctypes.byref(value), ctypes.sizeof(value))
            SWP_NOSIZE, SWP_NOMOVE, SWP_NOZORDER, SWP_NOACTIVATE, SWP_FRAMECHANGED = 0x1, 0x2, 0x4, 0x10, 0x20
            user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, SWP_NOSIZE | SWP_NOMOVE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED)
        except Exception:
            pass

    apply_immersive_dark_title_bar = apply_title_bar_theme  # old name, still used by some dialogs

    def set_theme(self, name):
        if not self.theme.set(name):
            return
        self._error_images.clear()
        for tile in list(self._visible.values()) + self._tile_pool:
            self._style_tile(tile, tile.idx if tile.idx is not None else -1)
        self._close_tree_popup()
        self.apply_title_bar_theme(self.root)
        for child in self.root.winfo_children():
            if isinstance(child, tk.Toplevel) and child.winfo_viewable():
                self.apply_title_bar_theme(child)
        if self._startup_done:
            self._update_view(force=True)
        self.save_settings()

    # ------------------------------------------------------------------ header / toolbar / status bar

    def create_header(self):
        t = self.theme
        header = t.paint(tk.Frame(self.root, bd=0), bg="surface")
        header.pack(fill=tk.X, side=tk.TOP)
        inner = t.paint(tk.Frame(header, bd=0), bg="surface")
        inner.pack(fill=tk.X, padx=18, pady=10)

        # Brand
        brand = t.paint(tk.Frame(inner, bd=0), bg="surface")
        brand.pack(side=tk.LEFT)
        Logo(brand, t, size=30).pack(side=tk.LEFT, padx=(0, 10))
        words = t.paint(tk.Frame(brand, bd=0), bg="surface")
        words.pack(side=tk.LEFT)
        t.paint(tk.Label(words, text="I.O.T", font=t.f["title"], bd=0, anchor="w"), bg="surface", fg="text").pack(anchor="w")
        t.paint(tk.Label(words, text="Image Organization Toolkit", font=t.f["caption"], bd=0, anchor="w"), bg="surface", fg="text_muted").pack(anchor="w")

        # Right-hand actions (packed right-to-left)
        ThemeToggle(inner, t, self.set_theme).pack(side=tk.RIGHT, padx=(14, 0))
        vertical_divider(inner, t, 28).pack(side=tk.RIGHT, padx=(14, 0))
        Button(inner, t, "Refresh", self.refresh, variant="ghost", hint="F5").pack(side=tk.RIGHT, padx=(4, 0))
        Button(inner, t, "Settings", lambda: iot_dialogs.show_controls_settings_window(self), variant="ghost").pack(side=tk.RIGHT, padx=(4, 0))
        Button(inner, t, "Change Log", self.open_change_log, variant="ghost").pack(side=tk.RIGHT, padx=(4, 0))
        self.undo_button = Button(inner, t, "Undo", self.show_undo_window, variant="secondary", hint=UNDO_KEYS)
        self.undo_button.pack(side=tk.RIGHT, padx=(4, 0))

        # Folder selector
        selector = tk.Frame(inner, bd=0, highlightthickness=1, cursor="hand2")
        t.paint(selector, bg="surface_alt", highlightbackground="border", highlightcolor="border")
        selector.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(36, 18), ipady=5)
        self.folder_selector = selector
        icon = tk.Canvas(selector, width=18, height=14, bd=0, highlightthickness=0)
        icon.pack(side=tk.LEFT, padx=(12, 8))

        def draw_folder_icon():
            c = t.c
            icon.configure(bg=c["surface_alt"])
            icon.delete("all")
            icon.create_polygon(1, 2, 7, 2, 9, 4, 17, 4, 17, 13, 1, 13, fill=c["accent"], outline="")
            icon.create_rectangle(1, 6, 17, 13, fill=c["accent_hover"], outline="")
        t.listen(draw_folder_icon, owner=icon)
        draw_folder_icon()

        self.folder_path_label = t.paint(tk.Label(selector, font=t.f["body"], bd=0), bg="surface_alt", fg="text_muted")
        self.folder_path_label.pack(side=tk.LEFT)
        self.folder_tag_dot = tk.Canvas(selector, width=16, height=14, bd=0, highlightthickness=0)
        self.folder_name_label = t.paint(tk.Label(selector, font=t.f["body_strong"], bd=0), bg="surface_alt", fg="text")
        self.folder_name_label.pack(side=tk.LEFT)
        self.folder_flags_label = t.paint(tk.Label(selector, font=t.f["body"], bd=0, padx=6), bg="surface_alt", fg="text")
        self.folder_flags_label.pack(side=tk.LEFT)
        chevron = self._arrow_canvas(selector, t.c["surface_alt"], t.c["text_muted"], "down")
        chevron.pack(side=tk.RIGHT, padx=(8, 12))
        t.listen(lambda: self._draw_arrow(chevron, t.c["surface_alt"], t.c["text_muted"], "down"), owner=chevron)
        self.folder_count_label = t.paint(tk.Label(selector, font=t.f["small"], bd=0), bg="surface_alt", fg="text_faint")
        self.folder_count_label.pack(side=tk.RIGHT)
        self.folder_menu_btn = selector  # name kept for compatibility

        selector.bind("<Configure>", self._fit_folder_selector, add="+")
        selector_parts = (selector, icon, self.folder_path_label, self.folder_tag_dot, self.folder_name_label,
                          self.folder_flags_label, chevron, self.folder_count_label)

        def hover(on):
            selector.configure(highlightbackground=t.c["accent"] if on else t.c["border"])
        for widget in selector_parts:
            widget.bind("<Button-1>", lambda e: self.toggle_collapsible_tree_panel())
            iot_platform.bind_right_click(widget, lambda e: self._open_tag_picker_for_selector())
            widget.bind("<Enter>", lambda e: hover(True))
            widget.bind("<Leave>", lambda e: hover(False))
            widget.configure(cursor="hand2")

        t.paint(tk.Frame(self.root, height=1, bd=0), bg="divider").pack(fill=tk.X, side=tk.TOP)
        self._refresh_undo_button()

    def create_toolbar(self):
        t = self.theme
        bar = t.paint(tk.Frame(self.root, bd=0), bg="surface")
        bar.pack(fill=tk.X, side=tk.TOP)
        inner = t.paint(tk.Frame(bar, bd=0), bg="surface")
        inner.pack(fill=tk.X, padx=18, pady=8)
        # One frame per possible row; the groups below flow into them (see _layout_toolbar)
        rows = [t.paint(tk.Frame(inner, bd=0), bg="surface") for _ in range(3)]

        # Options
        options = t.paint(tk.Frame(inner, bd=0), bg="surface")
        caps_label(options, t, "Options").pack(side=tk.LEFT, padx=(0, 12))
        CheckBox(options, t, "Sequential Rename", self.rename_var).pack(side=tk.LEFT, padx=(0, 16))
        CheckBox(options, t, "Skip Folder Creation", self.skip_folder_var).pack(side=tk.LEFT, padx=(0, 16))
        CheckBox(options, t, "Disable Cache Deletion", self.keep_cache_var).pack(side=tk.LEFT, padx=(0, 6))

        divider = vertical_divider(inner, t, 24)

        # Tools
        tools = t.paint(tk.Frame(inner, bd=0), bg="surface")
        caps_label(tools, t, "Tools").pack(side=tk.LEFT, padx=(0, 12))
        # Alphabetical; Group is built separately because it carries the animated border
        for text, key, command in (("Delete Folder", "Q", self.trigger_folder_delete),
                                   ("Downscale", "D", lambda: self.run_resizer("down")),
                                   ("Find Dupe", "F", self.trigger_duplicate_hash_scan),
                                   ("Group", "G", None),
                                   ("Rename Folder", "R", self.trigger_active_folder_rename),
                                   ("Upscale", "U", lambda: self.run_resizer("up")),
                                   ("Zoom", "Hover + Hold Z", None)):
            if text == "Group":
                self._create_group_button(tools)
                continue
            # Zoom (hold Z over a picture for a full-screen view) is shown as a feature, not clickable
            Button(tools, t, text, command, variant="secondary", hint=key, padx=8, pady=4, font=t.f["small"],
                   static=command is None).pack(side=tk.LEFT, padx=(0, 6))

        # Zoom
        zoom = t.paint(tk.Frame(inner, bd=0), bg="surface")
        caps_label(zoom, t, "Zoom").pack(side=tk.LEFT, padx=(0, 8))
        Button(zoom, t, "Fit", self.handle_auto_snap_zoom, variant="ghost", padx=8, pady=3, font=t.f["small"]).pack(side=tk.LEFT)
        self.size_slider = Slider(zoom, t, 60, 300, self.thumb_size, command=self.on_slider_change, length=120)
        self.size_slider.pack(side=tk.LEFT, padx=(6, 6))
        self.zoom_value_label = t.paint(tk.Label(zoom, text=f"{self.thumb_size} px", font=t.f["small"], width=6, anchor="w", bd=0), bg="surface", fg="text_muted")
        self.zoom_value_label.pack(side=tk.LEFT)

        self._toolbar = {"rows": rows, "options": options, "divider": divider, "tools": tools, "zoom": zoom, "layout": None}
        bar.bind("<Configure>", lambda e: self._layout_toolbar(e.width - 36))
        self._layout_toolbar(10_000)

        t.paint(tk.Frame(self.root, height=1, bd=0), bg="divider").pack(fill=tk.X, side=tk.TOP)

    def _layout_toolbar(self, width):
        """One row when it fits; on narrow windows (e.g. the half-window mode) the groups wrap onto
        extra rows instead of being cut off at the right edge."""
        tb = self._toolbar
        options, divider, tools, zoom = tb["options"], tb["divider"], tb["tools"], tb["zoom"]
        gap = 36  # divider and its padding
        widths = {g: g.winfo_reqwidth() for g in (options, tools, zoom)}
        if widths[options] + gap + widths[tools] + 24 + widths[zoom] <= width:
            layout = ((options, tools, zoom),)
        elif widths[options] + gap + widths[tools] <= width:
            layout = ((options, tools), (zoom,))
        else:
            layout = ((options,), (tools,), (zoom,))
        if layout == tb["layout"]:
            return
        tb["layout"] = layout

        for widget in (options, divider, tools, zoom) + tuple(tb["rows"]):
            widget.pack_forget()
        for row_frame, groups in zip(tb["rows"], layout):
            row_frame.pack(fill=tk.X, pady=(0 if row_frame is tb["rows"][0] else 6, 0))
            for group in groups:
                if group is tools and options in groups:
                    divider.pack(in_=row_frame, side=tk.LEFT, padx=18)
                    divider.lift(row_frame)
                # Zoom sits at the right end when it shares the row, otherwise at the start of its own row
                side = tk.RIGHT if group is zoom and len(groups) > 1 else tk.LEFT
                group.pack(in_=row_frame, side=side)
                group.lift(row_frame)  # row frames were created first; keep the groups drawn on top

    def create_status_bar(self):
        t = self.theme
        self.status_queue = []
        self.status_display_timer = None
        self._status_kind = "info"
        bar = t.paint(tk.Frame(self.root, bd=0), bg="surface")
        bar.pack(fill=tk.X, side=tk.BOTTOM)
        t.paint(tk.Frame(bar, height=1, bd=0), bg="divider").pack(fill=tk.X, side=tk.TOP)
        inner = t.paint(tk.Frame(bar, bd=0), bg="surface")
        inner.pack(fill=tk.X, padx=18, pady=5)
        self.status_dot = tk.Canvas(inner, width=8, height=8, bd=0, highlightthickness=0)
        self.status_dot.pack(side=tk.LEFT, padx=(0, 8))
        self.status_bar_text = t.paint(tk.Label(inner, text="Ready", font=t.f["small"], bd=0, anchor="w"), bg="surface", fg="text")
        self.status_bar_text.pack(side=tk.LEFT)
        self.status_info_label = t.paint(tk.Label(inner, font=t.f["small"], bd=0), bg="surface", fg="text_muted")
        self.status_info_label.pack(side=tk.RIGHT)
        self.status_selection_label = t.paint(tk.Label(inner, font=t.f["small"], bd=0, padx=14), bg="surface", fg="accent")
        self.status_selection_label.pack(side=tk.RIGHT)
        self.status_bar = self.status_bar_text  # name kept for compatibility
        t.listen(self._draw_status_dot, owner=self.status_dot)
        self._draw_status_dot()

    def _draw_status_dot(self):
        c = self.theme.c
        color = {"success": c["success"], "warning": c["warning"], "error": c["danger"]}.get(self._status_kind, c["text_faint"])
        self.status_dot.configure(bg=c["surface"])
        self.status_dot.delete("all")
        self.status_dot.create_oval(0, 0, 8, 8, fill=color, outline="")

    def _update_status_info(self):
        if not hasattr(self, "status_info_label"):
            return
        info = self.folder_info.get(self.image_folder)
        count = len(self.all_files)
        text = f"{count} image{'s' if count != 1 else ''}"
        if info:
            text += f"  ·  {format_bytes(info['bytes'])}"
        self.status_info_label.config(text=text)
        if self.group_mode:
            picked = len(self.group_picks)
            self.status_selection_label.config(text=f"Group mode: {picked} picked  —  Enter to copy into a folder, Esc to leave")
        elif self.first_selected_idx is not None and 0 <= self.first_selected_idx < count:
            self.status_selection_label.config(text=f"Range start: {short_label(self.all_files[self.first_selected_idx], 40)}  —  click the last picture")
        else:
            self.status_selection_label.config(text="")

    # ------------------------------------------------------------------ status messages

    def log_status(self, message, msg_type='info'):
        """Queues a status bar message. msg_type: 'info', 'success', 'warning', 'error'."""
        self.status_queue.append({'text': message, 'type': msg_type})
        if len(self.status_queue) > 4:
            del self.status_queue[:-4]
        if self.status_display_timer is None:
            self._display_next_status()

    def _display_next_status(self):
        if self.status_queue:
            message_data = self.status_queue.pop(0)
            self._status_kind = message_data['type']
            self.status_bar_text.config(text=message_data['text'])
            self._draw_status_dot()
            hold = 5000 if message_data['type'] in ('warning', 'error') else 3500
            self.status_display_timer = self.root.after(hold, self._display_next_status)
        else:
            self._status_kind = "info"
            self.status_bar_text.config(text="Ready")
            self._draw_status_dot()
            self.status_display_timer = None

    # ------------------------------------------------------------------ folder tree popup

    def _close_tree_popup(self, event=None):
        self._close_tag_picker()
        tags = self.root.bindtags()
        if "IOTFolderListKeys" in tags:  # the folder list's keys only apply while it's open
            self.root.bindtags(tuple(tag for tag in tags if tag != "IOTFolderListKeys"))
        if self.tree_popup_window is not None:
            try:
                self.tree_popup_window.destroy()
            except tk.TclError:
                pass
            self.tree_popup_window = None
            self._tree_canvas = None
            self._tree_closed_at = time.monotonic()

    def toggle_collapsible_tree_panel(self):
        """Collapsible folder tree. Rows are built from the scan data in a single O(n) pass."""
        if self.tree_popup_window is not None:
            self._close_tree_popup()
            return
        if time.monotonic() - self._tree_closed_at < 0.3:
            return
        t = self.theme
        c = t.c

        popup = tk.Toplevel(self.root)
        popup.wm_overrideredirect(True)
        popup.configure(bg=c["surface_alt"], bd=0, highlightbackground=c["border"], highlightthickness=1)
        self.tree_popup_window = popup

        anchor = self.folder_selector
        x = anchor.winfo_rootx()
        y = anchor.winfo_rooty() + anchor.winfo_height() + 4
        w = anchor.winfo_width()
        popup_width = [0]

        panel_canvas = tk.Canvas(popup, bg=c["surface_alt"], borderwidth=0, highlightthickness=0)
        panel_scrollbar = ttk.Scrollbar(popup, orient=tk.VERTICAL, command=panel_canvas.yview, style="IOT.Vertical.TScrollbar")
        content = tk.Frame(panel_canvas, bg=c["surface_alt"])
        content.bind("<Configure>", lambda e: panel_canvas.configure(scrollregion=panel_canvas.bbox("all")))
        window_id = panel_canvas.create_window((0, 0), window=content, anchor="nw")
        panel_canvas.bind("<Configure>", lambda e: panel_canvas.itemconfigure(window_id, width=e.width))
        panel_canvas.configure(yscrollcommand=panel_scrollbar.set)
        panel_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, pady=6)
        panel_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self._tree_canvas = panel_canvas

        registry = self.virtual_folder_path_expansion_registry
        active_norm = self._norm(self.image_folder)
        active_row = [None]
        # Open every branch the current folder sits in, so it can be shown (and centred) when the list opens
        for info in self.folder_info_list:
            norm_path = self._norm(info["path"])
            if norm_path != active_norm and iot_history._same_or_inside(active_norm, norm_path):
                registry[norm_path] = True

        def toggle_branch(norm_path):
            registry[norm_path] = not registry.get(norm_path, False)
            redraw()

        def jump_to(path):
            self._close_tree_popup()
            self.load_directory_target_sequence(path)

        hovered = {"index": None, "branch_end": None}  # the row under the pointer, for Hover + D / U

        def hovered_branch():
            """The folder under the pointer and every folder inside it (minus ones marked for deletion)."""
            index, branch_end = hovered["index"], hovered["branch_end"]
            if index is None or branch_end is None or self.busy:
                return []
            infos = self.folder_info_list
            return [infos[j]["path"] for j in range(index, branch_end[index])
                    if not self._is_pending_delete(infos[j]["path"])]

        def resize_branch(mode):
            """Hover + D / U in the folder list: resize the folder under the pointer and everything inside it
            (in place, names unchanged), after the usual confirmation."""
            folders = hovered_branch()
            if folders:
                self._close_tree_popup()
                self.run_resizer(mode, folders)

        def rename_branch():
            """Hover + Ctrl in the folder list: rename every image in the folder under the pointer and its
            sub-folders after its own folder, left in place, after a confirmation."""
            folders = hovered_branch()
            if folders:
                self._close_tree_popup()
                self.rename_folder_branch(folders)

        def set_all_below(index, branch_end, norms, open_them):
            """The + / - buttons: open (or close) every branch inside this folder, all the way down.
            The folder itself stays open, so closing leaves a tidy list of its direct sub-folders."""
            for j in range(index + 1, branch_end[index]):
                if branch_end[j] > j + 1:
                    registry[norms[j]] = open_them
            redraw()

        def redraw():
            for widget in content.winfo_children():
                widget.destroy()
            infos = self.folder_info_list
            norms = [self._norm(info["path"]) for info in infos]
            # Where each folder's branch ends in the (depth-first) list...
            branch_end = [len(infos)] * len(infos)
            open_stack = []
            for i, item in enumerate(infos):
                while open_stack and infos[open_stack[-1]]["depth"] >= item["depth"]:
                    branch_end[open_stack.pop()] = i
                open_stack.append(i)
            # ...and running counts of closed and open branches, so "is anything below this still closed / open?"
            # is instant (for the + and - buttons)
            closed_before, open_before = [0], [0]
            for i in range(len(infos)):
                is_branch = branch_end[i] > i + 1
                is_open = registry.get(norms[i], infos[i]["depth"] == 0)
                closed_before.append(closed_before[-1] + (is_branch and not is_open))
                open_before.append(open_before[-1] + (is_branch and is_open))
            hovered["branch_end"] = branch_end
            hide_below_depth = None
            row_count = 0
            for index, info in enumerate(infos):
                depth = info["depth"]
                if hide_below_depth is not None:
                    if depth > hide_below_depth:
                        continue
                    hide_below_depth = None

                norm_path = self._norm(info["path"])
                has_children = index + 1 < len(infos) and infos[index + 1]["depth"] > depth
                is_expanded = registry.setdefault(norm_path, depth == 0)
                if has_children and not is_expanded:
                    hide_below_depth = depth
                is_active = norm_path == active_norm
                row_bg = c["accent_soft"] if is_active else c["surface_alt"]

                row = tk.Frame(content, bg=row_bg, bd=0)
                row.pack(fill=tk.X)
                row_count += 1
                if is_active:
                    active_row[0] = row
                tk.Frame(row, width=12 + depth * 18, bg=row_bg).pack(side=tk.LEFT)

                parts = [row]
                if has_children:
                    toggle_label = self._arrow_canvas(row, row_bg, c["text_muted"], "down" if is_expanded else "right")
                    toggle_label.pack(side=tk.LEFT, padx=(2, 4))
                    toggle_label.bind("<Button-1>", lambda e, p=norm_path: toggle_branch(p))
                    toggle_label.configure(cursor="hand2")
                else:
                    tk.Frame(row, width=20, height=14, bg=row_bg).pack(side=tk.LEFT)

                tag_dot = tk.Canvas(row, width=16, height=14, bd=0, highlightthickness=0, cursor="hand2")
                self._draw_tag_dot(tag_dot, row_bg, self.folder_tag(info["path"]))  # empty slot when untagged
                tag_dot.pack(side=tk.LEFT)
                title_font = t.f["body_strong"] if is_active else t.f["body"]
                pending = self._is_pending_delete(info["path"])  # Q: crossed out until I.O.T closes
                title_label = tk.Label(row, text=info["name"] if depth else f"{info['name']}  (hub)", bg=row_bg,
                                       fg=c["accent"] if is_active else (c["text_faint"] if pending else c["text"]),
                                       font=self._struck_font(title_font) if pending else title_font,
                                       cursor="hand2", bd=0, pady=5)
                title_label.pack(side=tk.LEFT, padx=(2, 6))
                flags = self._folder_flags(info).strip()
                flag_label = tk.Label(row, text=flags, bg=row_bg, fg=c["text"], font=t.f["small"], bd=0)
                flag_label.pack(side=tk.LEFT)
                own, total = info["image_count"], info["total_image_count"]
                if total > own:  # sub-folders hold images too: show the sum for the whole branch
                    count_text = f"{own} · Σ {total}" if own else f"Σ {total}"
                else:
                    count_text = str(own) if own else ""
                # Fixed-width, right-aligned columns so counts and sizes line up from row to row
                count_label = tk.Label(row, text=count_text, bg=row_bg, width=9, anchor="e",
                                       fg=c["text_faint"], font=t.f["small"], bd=0, padx=14)
                count_label.pack(side=tk.RIGHT)
                parts += [tag_dot, title_label, flag_label, count_label]
                branch_buttons = []  # + (open everything below) and - (close everything below), after the name

                def add_branch_button(open_them, i=index):
                    button = tk.Canvas(row, width=18, height=18, bd=0, highlightthickness=0, bg=row_bg, cursor="hand2")

                    def draw(hover=False, canvas=button):
                        canvas.delete("all")
                        iot_theme.round_rect(canvas, 1.5, 1.5, 16.5, 16.5, 4, fill=c["button_hover"] if hover else c["button"],
                                             outline=c["accent"] if hover else c["border"])
                        line = c["accent"] if hover else c["text_muted"]
                        canvas.create_line(5.5, 9, 12.5, 9, fill=line, width=1.6)
                        if open_them:
                            canvas.create_line(9, 5.5, 9, 12.5, fill=line, width=1.6)
                    draw()
                    button.pack(side=tk.LEFT, padx=(0, 4), before=flag_label)
                    button.bind("<Button-1>", lambda e: set_all_below(i, branch_end, norms, open_them))
                    button.bind("<Enter>", lambda e: draw(True), add="+")
                    button.bind("<Leave>", lambda e: draw(False), add="+")
                    iot_theme.Tooltip(t, "Open all sub-folders" if open_them else "Close all sub-folders", button)
                    branch_buttons.append(button)
                if has_children and is_expanded:
                    if closed_before[branch_end[index]] > closed_before[index + 1]:
                        add_branch_button(True)    # something further down is still closed
                    if open_before[branch_end[index]] > open_before[index + 1]:
                        add_branch_button(False)   # something further down is open
                if branch_buttons:
                    branch_buttons[-1].pack_configure(padx=(0, 6))
                if self.tree_show_sizes:
                    own_bytes, branch_bytes = info["bytes"], info["total_bytes"]
                    if branch_bytes > own_bytes:  # same "own · Σ branch" style as the image counts
                        size_text = (f"{format_bytes(own_bytes)} · Σ {format_bytes(branch_bytes)}" if own_bytes
                                     else f"Σ {format_bytes(branch_bytes)}")
                    else:
                        size_text = format_bytes(own_bytes) if own_bytes else ""
                    size_label = tk.Label(row, text=size_text, bg=row_bg, fg=c["text_faint"], font=t.f["small"], bd=0,
                                          width=19, anchor="e")
                    size_label.pack(side=tk.RIGHT)
                    parts.append(size_label)

                def set_row_bg(color, widgets=parts + branch_buttons):
                    for widget in widgets:
                        widget.configure(bg=color)
                for element in parts + branch_buttons:
                    element.bind("<Enter>", lambda e, i=index: hovered.__setitem__("index", i), add="+")
                if not is_active:  # keep the row highlighted while the pointer is on + / -
                    for button in branch_buttons:
                        button.bind("<Enter>", lambda e, f=set_row_bg: f(c["button_hover"]), add="+")
                        button.bind("<Leave>", lambda e, f=set_row_bg: f(c["surface_alt"]), add="+")
                for element in parts:
                    element.bind("<Button-1>", lambda e, p=info["path"]: jump_to(p))
                    iot_platform.bind_right_click(element, lambda e, p=info["path"]: self._open_tag_picker(
                        popup, e.x_root, e.y_root, p, redraw))
                    if not is_active:
                        element.bind("<Enter>", lambda e, f=set_row_bg: f(c["button_hover"]), add="+")
                        element.bind("<Leave>", lambda e, f=set_row_bg: f(c["surface_alt"]), add="+")

            height = max(60, min(440, row_count * 28 + 14))
            # At least as wide as the selector, wider when the folder names need it (the selector is
            # narrow in half-window mode); it never shrinks while open, so expanding a branch doesn't jump
            content.update_idletasks()
            needed = content.winfo_reqwidth() + panel_scrollbar.winfo_reqwidth() + 8
            window_left = self.root.winfo_rootx()
            window_right = window_left + self.root.winfo_width()
            width = max(w, popup_width[0], min(max(needed, 420), 720, window_right - window_left - 16))
            popup_width[0] = width
            left = max(window_left + 8, min(x, window_right - 8 - width))  # keep it inside the app window
            popup.wm_geometry(f"{width}x{height}+{left}+{y}")

        redraw()
        popup.lift()
        # Centre the list on the current folder (only on opening, so expanding a branch doesn't jump).
        # Waits until the list has its real height: run too early, the height is ~1px and the folder ends
        # up half cut off at the top. Row positions come from the rows' requested heights, which are
        # known before anything is drawn.
        def centre_on_active(attempt=0):
            if self.tree_popup_window is not popup or active_row[0] is None:
                return
            popup.update_idletasks()
            view_height = panel_canvas.winfo_height()
            if view_height < 40 and attempt < 25:
                popup.after(20, centre_on_active, attempt + 1)
                return
            rows = content.winfo_children()
            total_height = sum(row.winfo_reqheight() for row in rows)
            if total_height <= view_height:
                return
            index = rows.index(active_row[0])
            row_top = sum(row.winfo_reqheight() for row in rows[:index])
            top = row_top + active_row[0].winfo_reqheight() / 2 - view_height / 2
            panel_canvas.configure(scrollregion=(0, 0, content.winfo_reqwidth(), total_height))
            panel_canvas.yview_moveto(max(0.0, min(1.0, top / total_height)))
        centre_on_active()
        popup.bind("<Escape>", self._close_tree_popup)
        popup.bind("<Leave>", lambda e: hovered.__setitem__("index", None) if e.widget is popup else None)
        # Keys while the list is open, handled by a binding tag of their own that sees every key press:
        # Hover + D / U resizes the branch; Ctrl (Cmd on macOS) on its own - pressed and let go with no other
        # key - renames it. The tag is put on the main window too while the list is open, because on macOS a
        # borderless popup may not get keyboard focus, so the keys arrive there instead.
        ctrl_tap = {"armed": False}

        def key_pressed(event):
            ctrl_tap["armed"] = event.keysym in iot_platform.MOD_KEYSYMS
            if event.keysym in ("d", "D", "u", "U") and hovered["index"] is not None:
                resize_branch("down" if event.keysym in ("d", "D") else "up")
                return "break"

        def key_released(event):
            if event.keysym in iot_platform.MOD_KEYSYMS and ctrl_tap["armed"]:
                ctrl_tap["armed"] = False
                if hovered["index"] is not None:
                    rename_branch()
                    return "break"
        for window in (popup, self.root):
            if "IOTFolderListKeys" not in window.bindtags():
                window.bindtags(("IOTFolderListKeys",) + window.bindtags())
        popup.bind_class("IOTFolderListKeys", "<KeyPress>", key_pressed)
        popup.bind_class("IOTFolderListKeys", "<KeyRelease>", key_released)
        popup.bind("<FocusOut>", lambda e: self._close_tree_popup() if e.widget is popup else None)
        popup.focus_force()

    def _arrow_canvas(self, parent, bg, fg, direction):
        canvas = tk.Canvas(parent, width=16, height=14, bd=0, highlightthickness=0)
        self._draw_arrow(canvas, bg, fg, direction)
        return canvas

    @staticmethod
    def _draw_arrow(canvas, bg, fg, direction):
        """Small triangle drawn on a canvas, so it doesn't depend on the font having the glyph."""
        canvas.configure(bg=bg)
        canvas.delete("all")
        if direction == "down":
            canvas.create_polygon(4, 5, 12, 5, 8, 10, fill=fg, outline="")
        else:
            canvas.create_polygon(6, 3, 11, 7, 6, 11, fill=fg, outline="")

    # ------------------------------------------------------------------ context menu

    def _themed_menu(self, parent):
        menu = tk.Menu(parent, tearoff=0, bd=0, relief=tk.FLAT, font=self.theme.f["body"], activeborderwidth=0)
        self.theme.paint(menu, bg="surface_alt", fg="text", activebackground="accent", activeforeground="accent_text",
                         selectcolor="accent")
        return menu

    def create_right_click_context_menu(self):
        menu = self._themed_menu(self.root)
        self.context_menu = menu
        menu.add_command(label="Settings…", command=lambda: iot_dialogs.show_controls_settings_window(self))
        menu.add_command(label="Undo…", accelerator=UNDO_KEYS, command=self.show_undo_window)
        menu.add_command(label="Open Change Log", command=self.open_change_log)
        menu.add_command(label="Refresh", accelerator="F5", command=self.refresh)
        menu.add_separator()
        menu.add_command(label="Jump to Top", command=lambda: self.canvas.yview_moveto(0.0))
        menu.add_command(label="Jump to Middle", command=self.handle_jump_to_middle_scroll)
        menu.add_command(label="Jump to Bottom", command=lambda: self.canvas.yview_moveto(1.0))
        menu.add_separator()
        self.zoom_submenu = self._themed_menu(menu)
        self.zoom_submenu.add_command(label="Fit to Width", command=self.handle_auto_snap_zoom)
        self.zoom_submenu.add_separator()
        self.zoom_submenu.add_command(label="Large (300 px)", command=lambda: self.size_slider.set(300))
        self.zoom_submenu.add_command(label="Medium (180 px)", command=lambda: self.size_slider.set(180))
        self.zoom_submenu.add_command(label="Small (60 px)", command=lambda: self.size_slider.set(60))
        menu.add_cascade(label="Grid Zoom", menu=self.zoom_submenu)
        menu.add_separator()
        menu.add_command(label="Delete Folder", accelerator="Q", command=self.trigger_folder_delete)
        menu.add_command(label="Downscale Images", accelerator="D", command=lambda: self.run_resizer("down"))
        menu.add_command(label="Find Duplicates", accelerator="F", command=self.trigger_duplicate_hash_scan)
        menu.add_command(label="Group Select", accelerator="G", command=self.toggle_group_mode)
        menu.add_command(label="Rename Folder", accelerator="R", command=self.trigger_active_folder_rename)
        menu.add_command(label="Upscale Images", accelerator="U", command=lambda: self.run_resizer("up"))
        menu.add_separator()
        menu.add_checkbutton(label="Sequential Rename", variable=self.rename_var)
        menu.add_checkbutton(label="Skip Folder Creation", variable=self.skip_folder_var)
        menu.add_checkbutton(label="Disable Cache Deletion", variable=self.keep_cache_var)

    def display_floating_context_menu(self, event):
        try:
            self.context_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.context_menu.grab_release()

    # ------------------------------------------------------------------ folder loading / navigation

    def load_directory_target_sequence(self, target_path_directory):
        """Loads a folder into the grid."""
        self.active_duplicate_filename = None
        self.image_folder = target_path_directory
        self.output_base = target_path_directory
        self.first_selected_idx = None
        self.group_picks = {}
        if hasattr(self, "group_button"):
            self._refresh_group_button()
        self.close_tooltip()
        self.last_folder_name = os.path.basename(target_path_directory.rstrip("\\/")) or "Sequence"

        self.all_files = iot_utils.list_images(target_path_directory, self.valid_extensions)
        self._rebuild_file_index()
        self.refresh_folder_button()
        self.root.focus_set()

        if self._startup_done:
            self.render_grid()
            self.canvas.yview_moveto(0.0)

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
            self.log_status(f"Refreshed: the folder was no longer there, so '{os.path.basename(folder)}' is shown.", 'warning')
            return
        self.invalidate_folder_thumbnails(folder)
        self._thumb_retries = {key: value for key, value in self._thumb_retries.items() if key[0] != folder}
        if self._startup_done:
            self._sync_tiles_with_disk()
        else:
            self.all_files = iot_utils.list_images(folder, self.valid_extensions)
            self._rebuild_file_index()
        self.refresh_folder_button()
        self.log_status("Refreshed.", 'success')

    def handle_jump_to_middle_scroll(self):
        total = self._total_height()
        view = self.canvas.winfo_height()
        if total <= view:
            self.canvas.yview_moveto(0.0)
            return
        self.canvas.yview_moveto(max(0.0, min(1.0, (total / 2.0 - view / 2.0) / total)))

    def handle_anomaly_recalculation_event(self):
        """Called after settings change."""
        self._close_tree_popup()
        self.scan_entire_directory_tree()
        self.refresh_folder_button()
        self.root.focus_set()
        self.log_status("Settings saved and folder flags recalculated.", 'success')

    def shift_active_directory_index(self, displacement_direction):
        """W/S folder cycling. Uses the image counts gathered during the tree scan, so no disk access."""
        focused_widget = self.root.focus_get()
        if focused_widget is not None and focused_widget.winfo_class() == "Entry":
            return
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

    def open_dropped_paths(self, paths):
        """A folder dropped from Explorer / OneCommander: show it. Folders outside the hub, and any
        folder with sub-folders, become the new hub."""
        folders = [os.path.abspath(path) for path in paths if os.path.isdir(path)]
        if not folders:
            self.log_status("Drop a folder to open it (files on their own aren't opened).", 'warning')
            return
        if self.busy:
            self.log_status("Please wait for the current batch to finish before opening another folder.", 'warning')
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
        self.log_status(f"Opened '{os.path.basename(folder.rstrip(chr(92) + '/')) or folder}'.{note}", 'success')

    def _switch_hub(self, folder):
        """Makes `folder` the parent hub: new folder tree, flags, recycle folder and thumbnail cache.
        The old hub's recycle folder and cache are kept until I.O.T closes, so undo still works."""
        self._thumb_loader.reset()
        self._retired_hubs.append((self.session, self.disk_cache))
        self.parent_hub = folder
        self.cache_folder = os.path.join(folder, '.thumbs_cache')
        self.session = iot_history.Session(folder)
        self.disk_cache = iot_utils.ThumbDiskCache(self.cache_folder)
        self._thumb_loader.disk_cache = self.disk_cache
        self._load_folder_tags()
        self.scan_entire_directory_tree()

    # ------------------------------------------------------------------ half-window toggle

    def _create_half_window_button(self):
        """Slim tab floating on the left edge of the grid: shrinks the window to the right half of the
        screen (room for Explorer on the left, for dragging folders in) and back to full size."""
        t = self.theme
        button = tk.Canvas(self.outer_frame, width=18, height=64, bd=0, highlightthickness=0, cursor="hand2")
        button.place(x=0, rely=0.5, anchor="w")
        button._hover = False
        self._half_button = button

        def redraw():
            button.configure(bg=t.c["bg"])
            # Chevron points to where the window will go: right when full size, left when halved
            iot_theme.draw_half_window_tab(button, t.c, 18, 64, halved=self._is_half_window(), hover=button._hover)

        def set_hover(value):
            button._hover = value
            redraw()

        button.bind("<Enter>", lambda e: set_hover(True))
        button.bind("<Leave>", lambda e: set_hover(False))
        button.bind("<Button-1>", lambda e: self.toggle_half_window())
        button.redraw = redraw
        t.listen(redraw, owner=button)
        self.root.bind("<Configure>", lambda e: redraw() if e.widget is self.root else None, add="+")
        redraw()

    def _is_half_window(self):
        return not iot_platform.is_maximized(self.root)

    def toggle_half_window(self):
        if self._is_half_window():
            self.root.minsize(980, 560)
            self._maximize_window()
        else:
            self.root.minsize(480, 560)  # the usual 980px minimum is wider than half of most screens
            self._snap_to_right_half()
        self.root.after(50, self._half_button.redraw)

    def _snap_to_right_half(self):
        """Fills the right half of the monitor the window is on (taskbar excluded); see iot_platform."""
        iot_platform.snap_right_half(self.root)

    # ------------------------------------------------------------------ grid canvas

    def create_scrollable_grid(self):
        t = self.theme
        self.outer_frame = t.paint(tk.Frame(self.root, bd=0, highlightthickness=0), bg="bg")
        self.outer_frame.pack(fill=tk.BOTH, expand=True)

        self.canvas = tk.Canvas(self.outer_frame, borderwidth=0, highlightthickness=0)
        t.paint(self.canvas, bg="bg")
        self.scrollbar = ttk.Scrollbar(self.outer_frame, orient=tk.VERTICAL, command=self.canvas.yview, style="IOT.Vertical.TScrollbar")

        # Every scroll (wheel, scrollbar drag, jump) reports here, which is where on-screen tiles get refilled
        self.canvas.configure(yscrollcommand=self._on_canvas_yscroll, yscrollincrement=0)
        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(10, 0), pady=(6, 0))
        self.scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        self.root.bind_all("<MouseWheel>", self.handle_mouse_wheel)
        self.root.bind_all("<Button-4>", self.handle_mouse_wheel)
        self.root.bind_all("<Button-5>", self.handle_mouse_wheel)
        iot_platform.bind_right_click(self.canvas, self.display_floating_context_menu)
        self.canvas.bind("<Configure>", self.handle_canvas_configure)
        self._create_half_window_button()

    @staticmethod
    def _is_inside(widget, container):
        if widget is None or container is None:
            return False
        path, base = str(widget), str(container)
        return path == base or path.startswith(base + ".")

    def handle_mouse_wheel(self, event):
        try:
            widget = self.root.winfo_containing(event.x_root, event.y_root)
        except (KeyError, tk.TclError):
            return
        if widget is None:
            return
        if event.num == 4:
            steps = -1
        elif event.num == 5:
            steps = 1
        elif abs(event.delta) >= 120:
            steps = -int(event.delta / 120)
        else:
            steps = -1 if event.delta > 0 else 1   # high-resolution wheels/touchpads send small deltas
        if self.tree_popup_window is not None and self._is_inside(widget, self.tree_popup_window):
            self._tree_canvas.yview_scroll(steps, "units")
        elif self._undo_window is not None and self._is_inside(widget, self._undo_window):
            if self._undo_bar.winfo_ismapped():  # the list is long enough to scroll
                self._undo_canvas.yview_scroll(steps, "units")
            return
        elif self._is_inside(widget, self.canvas):
            # Each wheel notch moves 10% of the visible height, scaled by Settings > Scroll speed
            total, view = self._total_height(), self.canvas.winfo_height()
            if total > view:
                top = self.canvas.canvasy(0)
                offset = steps * view * 0.10 * self.scroll_speed / 100
                self.canvas.yview_moveto(max(0.0, min(1.0, (top + offset) / total)))

    def _on_canvas_yscroll(self, first, last):
        self.scrollbar.set(first, last)
        yview = (first, last)
        if yview != self._last_yview:
            self._last_yview = yview
            # The tile under the mouse now shows a different file: drop the hover so [Delete] can't hit
            # the image that just scrolled away, then pick up whatever is under the pointer once it settles.
            if self.hovered_filename is not None:
                self.close_tooltip()
            if self._hover_job is not None:
                self.root.after_cancel(self._hover_job)
            self._hover_job = self.root.after(150, self._redetect_hover)
        self._schedule_view_update()

    def _schedule_view_update(self):
        if self._view_job is None:
            self._view_job = self.root.after_idle(self._update_view)

    def handle_canvas_configure(self, event):
        if not self._startup_done:
            return
        if self._resize_job is not None:
            self.root.after_cancel(self._resize_job)
        self._resize_job = self.root.after(120, self._relayout_after_resize)

    def _relayout_after_resize(self):
        self._resize_job = None
        width = self.canvas.winfo_width()
        if width == self._last_canvas_width:
            self._schedule_view_update()  # height-only change: just fill any new rows
            return
        self._last_canvas_width = width
        self._relayout_keep_position()

    def on_slider_change(self, value):
        new_size = int(float(value))
        self.zoom_value_label.config(text=f"{new_size} px")
        if new_size == self.thumb_size:
            return
        self.thumb_size = new_size
        if not self._startup_done:
            return
        if self._zoom_job is not None:
            self.root.after_cancel(self._zoom_job)
        self._zoom_job = self.root.after(150, self._apply_thumb_size)

    def handle_auto_snap_zoom(self):
        width = self._canvas_width()
        columns = max(1, width // (self.thumb_size + TILE_OVERHEAD))
        self.thumb_size = max(60, min(300, width // columns - TILE_OVERHEAD))
        self.size_slider.set(self.thumb_size)   # on_slider_change sees an unchanged size and does nothing
        self._apply_thumb_size()

    def _canvas_width(self):
        width = self.canvas.winfo_width()
        if width < 100:
            width = self.root.winfo_screenwidth() - 20
        return max(300, width)

    def _compute_columns(self):
        return max(1, self._canvas_width() // (self.thumb_size + TILE_OVERHEAD))

    # ------------------------------------------------------------------ virtual grid layout

    def _recompute_layout(self):
        """Every file's cell position comes from its index, so the scroll area can be any height
        without any widget ever being taller than the screen."""
        width = self._canvas_width()
        self._cols = self._compute_columns()
        self._cell_w = width / self._cols
        self._row_h = self._measure_row_height()
        self._rows = (len(self.all_files) + self._cols - 1) // self._cols
        total_height = self._rows * self._row_h + 8
        self.canvas.configure(scrollregion=(0, 0, width, max(total_height, 1)))
        self._last_ranges = None

    def _measure_row_height(self):
        size = self.thumb_size
        if size not in self._row_height_cache:
            probe = self._acquire_tile()
            probe.img_label.config(image=self._get_placeholder(), width=size, height=size)
            probe.txt_label.config(text="Measure")
            probe.frame.update_idletasks()
            self._row_height_cache[size] = probe.frame.winfo_reqheight() + 14
            self._release_tile(probe)
        return self._row_height_cache[size]

    def _total_height(self):
        return self._rows * self._row_h + 8

    def _first_visible_index(self):
        if not self.all_files:
            return 0
        top_row = int(self.canvas.canvasy(0) // max(1, self._row_h))
        return min(len(self.all_files) - 1, max(0, top_row * self._cols))

    def _relayout_keep_position(self):
        anchor_idx = self._first_visible_index()
        self._recompute_layout()
        self._scroll_row_to_top(anchor_idx // self._cols)
        self._update_view(force=True)

    def _scroll_row_to_top(self, row):
        total = self._total_height()
        if total <= self.canvas.winfo_height():
            self.canvas.yview_moveto(0.0)
        else:
            self.canvas.yview_moveto(max(0.0, min(1.0, (row * self._row_h) / total)))

    def render_grid(self):
        """Shows the current folder from the top. Cheap no matter how many files the folder holds."""
        self._thumb_loader.reset()
        self._drain_stale_results()
        self.close_tooltip()
        for tile in list(self._visible.values()):
            self._release_tile(tile)
        self._visible = {}
        self._visible_by_name = {}
        self._prune_cache()
        self._recompute_layout()
        self.canvas.yview_moveto(0.0)
        self._update_view(force=True)

    def _update_view(self, force=False):
        """Makes sure exactly the rows on (and just around) the screen have tiles, and queues their
        thumbnails, plus a screen's worth either side so scrolling shows images straight away."""
        self._view_job = None
        n = len(self.all_files)
        if n == 0:
            for tile in list(self._visible.values()):
                self._release_tile(tile)
            self._visible, self._visible_by_name = {}, {}
            self._thumb_loader.wanted = frozenset()
            self._show_empty_message(True)
            return
        self._show_empty_message(False)

        cols, row_h = self._cols, max(1, self._row_h)
        view_h = max(1, self.canvas.winfo_height())
        top = self.canvas.canvasy(0)
        first_row = max(0, int(top // row_h) - BUFFER_ROWS)
        last_row = min(self._rows - 1, int((top + view_h) // row_h) + BUFFER_ROWS)
        start, end = first_row * cols, min(n, (last_row + 1) * cols)

        screen_rows = int(view_h // row_h) + 1
        pf_start = max(0, first_row - screen_rows * PREFETCH_SCREENS) * cols
        pf_end = min(n, (last_row + 1 + screen_rows * PREFETCH_SCREENS) * cols)

        ranges = (start, end, pf_start, pf_end, n)
        if not force and ranges == self._last_ranges:
            return
        self._last_ranges = ranges

        old = self._visible
        self._visible = {}
        spare_by_name = {}
        for idx, tile in old.items():
            if not force and start <= idx < end and idx < n and self.all_files[idx] == tile.filename:
                self._visible[idx] = tile
            else:
                spare_by_name[tile.filename] = tile
        for idx in range(start, end):
            if idx in self._visible:
                continue
            name = self.all_files[idx]
            tile = spare_by_name.pop(name, None)
            if tile is None or force:
                if tile is None:
                    tile = spare_by_name.popitem()[1] if spare_by_name else self._acquire_tile()
                self._assign_tile(tile, name)
            self._place_tile(tile, idx)
            self._visible[idx] = tile
        for tile in spare_by_name.values():
            self._release_tile(tile)
        self._visible_by_name = {tile.filename: tile for tile in self._visible.values()}

        files = self.all_files
        self._thumb_loader.wanted = frozenset(files[pf_start:pf_end])
        order = files[pf_start:start] + files[end:pf_end][::-1] + files[start:end][::-1]
        self._request_thumbnails(order)

    def _show_empty_message(self, show):
        if self._empty_text_id is not None:
            self.canvas.delete(self._empty_text_id)
            self._empty_text_id = None
        if show:
            self._empty_text_id = self.canvas.create_text(
                self._canvas_width() // 2, 80, text="No images in this folder",
                font=self.theme.f["heading"], fill=self.theme.c["text_faint"])

    def _acquire_tile(self):
        if self._tile_pool:
            return self._tile_pool.pop()
        t = self.theme
        tile = Tile()
        tile.filename = None
        tile.idx = None
        tile.photo = None
        tile.loaded = False
        tile.frame = tk.Frame(self.canvas, bd=0, highlightthickness=2)
        tile.img_label = tk.Label(tile.frame, image=self._get_placeholder(), width=self.thumb_size, height=self.thumb_size, bd=0)
        tile.img_label.pack(padx=8, pady=(8, 4))
        tile.txt_label = tk.Label(tile.frame, text="", font=t.f["small"], bd=0, padx=6, pady=4)
        tile.txt_label.pack(fill=tk.X)
        for widget in (tile.frame, tile.img_label, tile.txt_label):
            widget.bind("<Button-1>", lambda e, t=tile: self._on_tile_click(t, "click"))
            # Ctrl / Alt here are Cmd / Option on macOS (where Ctrl+Click means right-click)
            widget.bind(f"<{iot_platform.MOD}-Button-1>", lambda e, t=tile: self._on_tile_click(t, "ctrl"))
            widget.bind(f"<{iot_platform.ALT}-Button-1>", lambda e, t=tile: self._on_tile_click(t, "alt"))
            iot_platform.bind_right_click(widget, self.display_floating_context_menu)
            widget.bind("<Enter>", lambda e, t=tile: self.show_tooltip(e, t))
            widget.bind("<Leave>", lambda e, t=tile: self._on_tile_leave(t))
        tile.item_id = self.canvas.create_window(0, 0, window=tile.frame, anchor="n", state="hidden")
        self._style_tile(tile, -1)
        return tile

    def _release_tile(self, tile):
        self.canvas.itemconfigure(tile.item_id, state="hidden")
        if tile is self._hover_tile:
            self._hover_tile = None
        tile.filename = None
        tile.idx = None
        tile.photo = None
        tile.loaded = False
        tile.img_label.config(image=self._get_placeholder())
        self._tile_pool.append(tile)

    def _assign_tile(self, tile, name):
        tile.filename = name
        key = (self.image_folder, name, self.thumb_size)
        photo = self._cache_get(key)
        if photo is None and (self.image_folder, name) in self._failed_thumbs:
            photo = self._get_error_image(self.thumb_size)
        tile.photo = photo
        tile.loaded = photo is not None
        tile.img_label.config(image=photo or self._get_placeholder(), width=self.thumb_size, height=self.thumb_size)
        tile.txt_label.config(text=short_label(name))

    def _place_tile(self, tile, idx):
        row, col = divmod(idx, self._cols)
        x = col * self._cell_w + self._cell_w / 2.0
        y = row * self._row_h + 8
        self.canvas.coords(tile.item_id, x, y)
        self.canvas.itemconfigure(tile.item_id, width=max(1, int(self._cell_w) - 16), state="normal")
        tile.idx = idx
        self._style_tile(tile, idx)

    def _style_tile(self, tile, idx):
        c = self.theme.c
        selected = self.first_selected_idx is not None and idx == self.first_selected_idx
        hovered = tile is self._hover_tile
        duplicate = tile.filename is not None and tile.filename == self.active_duplicate_filename
        picked = tile.filename is not None and tile.filename in self.group_picks
        bg = c["tile_hover"] if hovered else c["tile"]
        if picked:
            border = c["group"]
        elif selected:
            border = c["accent"]
        elif duplicate:
            border = c["danger"]
        elif hovered:
            border = c["accent_soft"]
        else:
            border = c["tile_border"]
        tile.frame.config(bg=bg, highlightbackground=border, highlightcolor=border)
        tile.img_label.config(bg=bg)
        if picked:
            tile.txt_label.config(bg=c["group"], fg=c["accent_text"])
        elif selected:
            tile.txt_label.config(bg=c["accent"], fg=c["accent_text"])
        elif duplicate:
            tile.txt_label.config(bg=c["danger"], fg=c["accent_text"])
        else:
            tile.txt_label.config(bg=bg, fg=c["text"] if hovered else c["text_muted"])

    def _restyle_index(self, idx):
        tile = self._visible.get(idx) if idx is not None else None
        if tile is not None:
            self._style_tile(tile, idx)

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
        self._zoom_job = None
        self.zoom_value_label.config(text=f"{self.thumb_size} px")
        self._thumb_loader.reset()
        self._drain_stale_results()
        self._prune_cache()
        self._relayout_keep_position()
        self._save_settings_soon()

    # ------------------------------------------------------------------ thumbnails

    def _get_placeholder(self):
        image = self._placeholders.get("blank")
        if image is None:
            image = tk.PhotoImage(width=1, height=1)
            self._placeholders["blank"] = image
        return image

    def _get_error_image(self, size):
        image = self._error_images.get(size)
        if image is None:
            image = ImageTk.PhotoImage(Image.new('RGB', (size, size), color=self.theme.c["error_thumb"]))
            self._error_images[size] = image
        return image

    def _cache_capacity(self):
        return max(200, THUMB_CACHE_BYTES // (self.thumb_size * self.thumb_size * 4))

    def _cache_get(self, key):
        photo = self.image_cache.get(key)
        if photo is not None:
            self.image_cache.move_to_end(key)
        return photo

    def _cache_put(self, key, photo):
        self.image_cache[key] = photo
        self.image_cache.move_to_end(key)
        capacity = self._cache_capacity()
        while len(self.image_cache) > capacity:
            self.image_cache.popitem(last=False)

    def _prune_cache(self):
        for key in [k for k in self.image_cache if k[2] != self.thumb_size]:
            del self.image_cache[key]

    def _drain_stale_results(self):
        try:
            while True:
                self._thumb_queue.get_nowait()
        except queue.Empty:
            pass

    def _request_thumbnails(self, names):
        folder, size = self.image_folder, self.thumb_size
        now = time.monotonic()
        keys = [(folder, name, size) for name in names
                if (folder, name, size) not in self.image_cache and (folder, name) not in self._failed_thumbs
                and self._thumb_retries.get((folder, name), (0, 0))[0] <= now]
        self._thumb_loader.request(keys)

    def _heal_thumbnails(self, retry_now=False):
        """Asks again for on-screen pictures that still show the placeholder: ones whose read failed for now
        (once their retry time comes, or straight away when the window is reactivated), or that got stuck
        any other way. Only tiles on screen, so it costs next to nothing."""
        if not self._startup_done or not self._visible:
            return
        folder, size = self.image_folder, self.thumb_size
        if retry_now:
            for key, (_, attempts) in list(self._thumb_retries.items()):
                self._thumb_retries[key] = (0, attempts)
        names = [tile.filename for idx, tile in sorted(self._visible.items(), reverse=True)
                 if not tile.loaded and tile.filename
                 and not self._thumb_loader.is_busy((folder, tile.filename, size))]
        if names:
            self._request_thumbnails(names)  # nearest the top of the screen last = loaded first

    def _heal_thumbnails_loop(self):
        try:
            self._heal_thumbnails()
        finally:
            self.root.after(HEAL_INTERVAL_MS, self._heal_thumbnails_loop)

    def _drain_thumb_queue(self):
        """Main thread: turns finished PIL thumbnails into Tk images within a small time budget per tick."""
        deadline = time.perf_counter() + 0.012
        try:
            while time.perf_counter() < deadline:
                generation, folder, filename, size, thumb, dims, failure = self._thumb_queue.get_nowait()
                if generation != self._thumb_loader.generation or folder != self.image_folder or size != self.thumb_size:
                    continue
                if thumb is None and failure == "temporary":
                    # Couldn't be read just now (e.g. the drive was asleep): keep the placeholder and try again soon
                    _, attempts = self._thumb_retries.get((folder, filename), (0, 0))
                    if attempts + 1 < THUMB_RETRY_LIMIT:
                        self._thumb_retries[(folder, filename)] = (time.monotonic() + THUMB_RETRY_SECONDS, attempts + 1)
                        continue
                if thumb is None:
                    self._thumb_retries.pop((folder, filename), None)
                    self._failed_thumbs.add((folder, filename))
                    photo = self._get_error_image(size)
                else:
                    self._thumb_retries.pop((folder, filename), None)
                    photo = ImageTk.PhotoImage(thumb)
                    self._cache_put((folder, filename, size), photo)
                    self.dims_cache[(folder, filename)] = dims
                tile = self._visible_by_name.get(filename)
                if tile is not None:
                    tile.photo = photo
                    tile.loaded = True
                    tile.img_label.config(image=photo)
        except queue.Empty:
            pass
        except tk.TclError:
            pass
        self.root.after(15, self._drain_thumb_queue)

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
        over to their new names, and refill the on-screen tiles.
        rename_map: {old_name: new_name_in_this_folder or None if it left the folder}
        focus_idx: picture whose row should end up at the top (default: keep the current top row)
        """
        folder, size = self.image_folder, self.thumb_size
        new_files = iot_utils.list_images(folder, self.valid_extensions)

        if rename_map:
            moved = {}
            for old_name, new_name in rename_map.items():
                photo = self.image_cache.pop((folder, old_name, size), None)
                dims = self.dims_cache.pop((folder, old_name), None)
                self._failed_thumbs.discard((folder, old_name))
                if new_name:
                    moved[new_name] = (photo, dims)
            for new_name, (photo, dims) in moved.items():
                if photo is not None:
                    self._cache_put((folder, new_name, size), photo)
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
        self._scroll_row_to_top(min(anchor_idx, max(0, len(new_files) - 1)) // self._cols)
        self._update_view(force=True)
        self._update_status_info()

    # ------------------------------------------------------------------ hover / tooltip / preview

    def _create_tooltip(self):
        t = self.theme
        self.tooltip_window = tk.Toplevel(self.root)
        self.tooltip_window.withdraw()
        self.tooltip_window.wm_overrideredirect(True)
        t.paint(self.tooltip_window, bg="surface_alt", highlightbackground="border")
        self.tooltip_window.configure(bd=0, highlightthickness=1)
        self._tip_name = t.paint(tk.Label(self.tooltip_window, font=t.f["body_strong"], padx=10, pady=0, bd=0), bg="surface_alt", fg="text")
        self._tip_name.pack(anchor="w", pady=(6, 0))
        self._tip_res = t.paint(tk.Label(self.tooltip_window, font=t.f["small"], padx=10, pady=0, bd=0), bg="surface_alt", fg="text_muted")
        self._tip_res.pack(anchor="w", pady=(1, 6))
        self._tooltip_visible = False

    def show_tooltip(self, event, tile):
        if tile.filename is None:
            return
        dims = self.dims_cache.get((self.image_folder, tile.filename))
        self._tip_name.config(text=tile.filename)
        self._tip_res.config(text=f"{dims[0]} × {dims[1]} px" if dims else "Size unknown")
        self.tooltip_window.update_idletasks()  # may flush a pending scroll notice, which clears the hover
        self.hovered_filename = tile.filename
        if self._hover_tile is not tile:
            previous = self._hover_tile
            self._hover_tile = tile
            if previous is not None and previous.idx is not None:
                self._style_tile(previous, previous.idx)
            if tile.idx is not None:
                self._style_tile(tile, tile.idx)
        tooltip_width = self.tooltip_window.winfo_reqwidth()
        screen_width = self.root.winfo_screenwidth()
        x = event.x_root - tooltip_width - 15 if event.x_root + 15 + tooltip_width > screen_width else event.x_root + 15
        self.tooltip_window.wm_geometry(f"+{x}+{event.y_root + 18}")
        if not self._tooltip_visible:
            self.tooltip_window.deiconify()
            self._tooltip_visible = True
        self.tooltip_window.lift()

    def _tile_under_pointer(self):
        try:
            x, y = self.root.winfo_pointerxy()
            widget = self.root.winfo_containing(x, y)
        except (KeyError, tk.TclError):
            return None, 0, 0
        if widget is None:
            return None, x, y
        for tile in self._visible.values():
            if self._is_inside(widget, tile.frame):
                return tile, x, y
        return None, x, y

    def _redetect_hover(self):
        self._hover_job = None
        tile, x, y = self._tile_under_pointer()
        if tile is not None and tile.filename is not None:
            self.show_tooltip(type("PointerEvent", (), {"x_root": x, "y_root": y})(), tile)

    def _on_tile_leave(self, tile):
        self.root.after(25, lambda: self._verify_hover(tile))

    def _verify_hover(self, tile):
        if tile.filename is None or self.hovered_filename != tile.filename:
            return
        under, _, _ = self._tile_under_pointer()
        if under is tile:
            return
        self.close_tooltip()

    def close_tooltip(self, event=None):
        if getattr(self, "_tooltip_visible", False):
            self.tooltip_window.withdraw()
            self._tooltip_visible = False
        self.hovered_filename = None
        previous = getattr(self, "_hover_tile", None)
        self._hover_tile = None
        if previous is not None and previous.idx is not None:
            try:
                self._style_tile(previous, previous.idx)
            except tk.TclError:
                pass

    @property
    def currently_hovered_idx(self):
        return self.file_index.get(self.hovered_filename) if self.hovered_filename else None

    def handle_z_press(self, event):
        if self._z_release_job is not None:  # an auto-repeat release just before this press: still held
            self.root.after_cancel(self._z_release_job)
            self._z_release_job = None
            return
        if self.z_key_is_pressed or self.hovered_filename is None:
            return
        self.z_key_is_pressed = True
        full_img_path = os.path.join(self.image_folder, self.hovered_filename)
        try:
            screen_width = self.root.winfo_screenwidth()
            screen_height = self.root.winfo_screenheight()
            # The frosted backdrop is grabbed and blurred on a worker thread while the preview decodes
            frost_job = None
            if self.frost_preview:
                frost_job = self._frost_pool.submit(iot_utils.frosted_screen, screen_width, screen_height, self.theme.c["bg"])
            # The picture uses the whole screen, over the taskbar
            preview, _ = iot_utils.load_preview_image(full_img_path, screen_width, screen_height)
            try:
                frost = frost_job.result(timeout=1.0) if frost_job else None
            except Exception:
                frost = None
            if frost is not None:
                self.preview_backdrop = tk.Toplevel(self.root)
                self.preview_backdrop.wm_overrideredirect(True)
                self._frost_photo = ImageTk.PhotoImage(frost)
                tk.Label(self.preview_backdrop, image=self._frost_photo, bd=0, highlightthickness=0).pack()
                self.preview_backdrop.wm_geometry(f"{screen_width}x{screen_height}+0+0")
                self.preview_backdrop.attributes("-topmost", True)  # over the taskbar too
                self.preview_backdrop.lift()
            new_w, new_h = preview.size
            self.preview_window = tk.Toplevel(self.root)
            self.preview_window.wm_overrideredirect(True)
            self.preview_window.configure(bg="#0c0c0d")
            self.preview_photo = ImageTk.PhotoImage(preview)
            tk.Label(self.preview_window, image=self.preview_photo, bg="#0c0c0d", bd=0, highlightthickness=0).pack(expand=True)
            self.preview_window.wm_geometry(f"{new_w}x{new_h}+{(screen_width - new_w) // 2}+{max(0, (screen_height - new_h) // 2)}")
            self.preview_window.attributes("-topmost", True)
            self.preview_window.lift()
        except Exception:
            # Stay "held" until Z is let go: otherwise key repeat would retry (and re-grab the screen) ~30x a second
            self._close_preview()

    def handle_z_release(self, event):
        if event is not None and iot_platform.KEY_REPEAT_SENDS_RELEASE:
            # Linux: wait a moment; a press straight after means the key is still held (auto-repeat)
            if self._z_release_job is None:
                self._z_release_job = self.root.after(60, self._finish_z_release)
            return
        self._finish_z_release()

    def _finish_z_release(self):
        self._z_release_job = None
        self.z_key_is_pressed = False
        self._close_preview()

    def _close_preview(self):
        if self.preview_window:
            self.preview_window.destroy()
            self.preview_window = None
            self.preview_photo = None
        if self.preview_backdrop:
            self.preview_backdrop.destroy()
            self.preview_backdrop = None
            self._frost_photo = None

    # ------------------------------------------------------------------ history / undo

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
        self.undo_button.set_text(f"Undo  {count}" if count else "Undo")
        self.undo_button.set_enabled(count > 0)

    def undo_latest(self):
        focused = self.root.focus_get()
        if focused is not None and focused.winfo_class() == "Entry":
            return
        record = self.history.latest_undoable()
        if record is None:
            self.log_status("Nothing to undo.", 'info')
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
            self.log_status(f"Reverted with {len(problems)} problem(s): {problems[0]}", 'warning')
        else:
            self.log_status(f"Reverted: {record.title}", 'success')
        self._refresh_undo_button()
        self._refresh_undo_window()

    def show_undo_window(self):
        if self._undo_window is not None:
            try:
                self._undo_window.lift()
                self._undo_window.focus_set()
                return
            except tk.TclError:
                self._undo_window = None
        t = self.theme
        win = iot_theme.make_dialog(self, "Undo", modal=False)
        self._undo_window = win
        iot_theme.dialog_header(win, t, "Recent changes",
                                f"The last {iot_history.HISTORY_LIMIT} changes made in this session. Deleted files and the "
                                "originals of resized images are kept until I.O.T closes.")
        # The list scrolls once it's taller than ~60% of the screen (20 changes don't fit otherwise)
        holder = t.paint(tk.Frame(win, bd=0), bg="surface")
        holder.pack(fill=tk.BOTH, expand=True, padx=22, pady=(6, 0))
        canvas = t.paint(tk.Canvas(holder, bd=0, highlightthickness=0), bg="surface")
        bar = ttk.Scrollbar(holder, orient=tk.VERTICAL, command=canvas.yview, style="IOT.Vertical.TScrollbar")
        canvas.configure(yscrollcommand=bar.set)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self._undo_list = t.paint(tk.Frame(canvas, bd=0), bg="surface")
        canvas.create_window((0, 0), window=self._undo_list, anchor="nw", tags="list")
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure("list", width=e.width))
        self._undo_canvas, self._undo_bar = canvas, bar

        def close():
            self._undo_window = None
            win.destroy()
        iot_theme.dialog_buttons(win, t, [("Close", close, "secondary")])
        win.protocol("WM_DELETE_WINDOW", close)
        win.bind("<Escape>", lambda e: close())
        self._refresh_undo_window()
        iot_theme.show_dialog(self, win, min_width=600)

    def _refresh_undo_window(self):
        win = self._undo_window
        if win is None:
            return
        try:
            if not win.winfo_exists():
                self._undo_window = None
                return
        except tk.TclError:
            self._undo_window = None
            return
        t, c = self.theme, self.theme.c
        for child in self._undo_list.winfo_children():
            child.destroy()
        records = list(reversed(self.history.records))
        if not records:
            t.paint(tk.Label(self._undo_list, text="Nothing to undo yet.", font=t.f["body"], pady=24, bd=0),
                    bg="surface", fg="text_faint").pack(fill=tk.X)
        for record in records:
            row = t.paint(tk.Frame(self._undo_list, bd=0, highlightthickness=1), bg="surface_alt", highlightbackground="border")
            row.pack(fill=tk.X, pady=(0, 6))
            if record.reverted:
                t.paint(tk.Label(row, text="Reverted", font=t.f["small"], bd=0, padx=14), bg="surface_alt", fg="text_faint").pack(side=tk.RIGHT)
            else:
                Button(row, t, "Revert", lambda r=record: self.perform_undo(r), variant="secondary", padx=14, pady=4,
                       font=t.f["small"]).pack(side=tk.RIGHT, padx=10)
            badge = tk.Label(row, text=record.kind, font=t.f["label_caps"], bd=0, padx=8, pady=2, width=13)
            t.paint(badge, bg="accent_soft", fg="accent")
            badge.pack(side=tk.LEFT, padx=(10, 12), pady=10)
            text = t.paint(tk.Frame(row, bd=0), bg="surface_alt")
            text.pack(side=tk.LEFT, fill=tk.X, expand=True, pady=6)
            t.paint(tk.Label(text, text=record.title, font=t.f["body"], bd=0, anchor="w"),
                    bg="surface_alt", fg="text_faint" if record.reverted else "text").pack(fill=tk.X)
            try:
                where = os.path.relpath(record.folder, os.path.dirname(self.parent_hub.rstrip("\\/")))
            except ValueError:
                where = record.folder
            t.paint(tk.Label(text, text=f"{time_ago(record.time)}  ·  {where}", font=t.f["caption"], bd=0, anchor="w"),
                    bg="surface_alt", fg="text_muted").pack(fill=tk.X)
        self._fit_undo_list()

    def _fit_undo_list(self):
        """Sizes the Undo list to its rows, up to ~60% of the screen; beyond that it scrolls."""
        win, canvas, bar, rows = self._undo_window, self._undo_canvas, self._undo_bar, self._undo_list
        rows.update_idletasks()
        needed, width = rows.winfo_reqheight(), rows.winfo_reqwidth()
        limit = int(self.root.winfo_screenheight() * 0.6)
        canvas.configure(width=width, height=min(needed, limit), scrollregion=(0, 0, width, needed))
        if needed > limit:
            bar.pack(side=tk.RIGHT, fill=tk.Y, padx=(6, 0))
        else:
            bar.pack_forget()
            canvas.yview_moveto(0.0)
        if win.winfo_ismapped():  # already open: grow / shrink with the list (it was sized once on opening)
            win.update_idletasks()
            win.geometry(f"{max(600, win.winfo_reqwidth())}x{win.winfo_reqheight()}")

    def open_change_log(self):
        if not os.path.exists(CHANGE_LOG_FILE):
            self._themed_notice("Change Log", "No changes have been logged yet.\n\nEvery delete, move, rename, resize and undo "
                                              f"will be recorded in:\n{CHANGE_LOG_FILE}")
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
        t = self.theme
        win = iot_theme.make_dialog(self, "Files from an earlier session")
        iot_theme.dialog_header(win, t, "Files from an earlier session",
                                f"I.O.T didn't close normally last time, so {file_count} file(s) deleted or replaced during "
                                "that session were never cleared. They're in the hidden '.iot_recycle' folder in this hub.")

        def open_folder():
            try:
                open_with_system(self.session.root_dir)
            except Exception:
                pass

        def delete_them():
            for path in leftovers:
                shutil.rmtree(path, ignore_errors=True)
            self.log_status(f"Cleared {file_count} file(s) left over from an earlier session.", 'success')
            win.destroy()
        iot_theme.dialog_buttons(win, t, [("Delete Them", delete_them, "danger"), ("Keep for Now", win.destroy, "secondary"),
                                          ("Open Folder", open_folder, "ghost")])
        win.bind("<Escape>", lambda e: win.destroy())
        iot_theme.show_dialog(self, win, min_width=480)

    # ------------------------------------------------------------------ deleting

    def handle_keyboard_delete(self, event=None):
        """Moves the hovered image to the recycle folder; the following tiles shift up into the gap."""
        if self.busy or self.hovered_filename is None:
            return
        filename_to_delete = self.hovered_filename
        idx = self.file_index.get(filename_to_delete)
        if idx is None:
            return
        self.close_tooltip()
        folder = self.image_folder
        full_path = os.path.join(folder, filename_to_delete)
        try:
            step = self._stash(full_path)
        except OSError as err:
            self.log_status(f"Could not delete {filename_to_delete}: {err}", 'error')
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

        self._rows = (len(self.all_files) + self._cols - 1) // self._cols
        self.canvas.configure(scrollregion=(0, 0, self._canvas_width(), max(self._total_height(), 1)))
        self._update_view(force=False)
        self.refresh_folder_button()

    # ------------------------------------------------------------------ scrolling to a file

    def jump_to_specific_image_filename(self, filename):
        idx = self.file_index.get(filename)
        if idx is None:
            return
        self._set_selection(idx)
        self._scroll_to_index(idx)

    def _scroll_to_index(self, idx):
        total = self._total_height()
        view = self.canvas.winfo_height()
        if total <= view:
            self.canvas.yview_moveto(0.0)
            return
        center_y = (idx // self._cols) * self._row_h + self._row_h / 2.0
        self.canvas.yview_moveto(max(0.0, min(1.0, (center_y - view / 2.0) / total)))
        self._update_view()
        self._restyle_index(idx)

    # ------------------------------------------------------------------ small themed dialogs

    def _themed_notice(self, title, text):
        t = self.theme
        win = iot_theme.make_dialog(self, title)
        iot_theme.dialog_header(win, t, title, text)
        iot_theme.dialog_buttons(win, t, [("OK", win.destroy, "primary")])
        for key in ("<Return>", "<Escape>"):
            win.bind(key, lambda e: win.destroy())
        iot_theme.show_dialog(self, win, min_width=420)
        return win

    def _ask_text(self, title, subtitle, label, default_value, confirm_text):
        """Themed single-field prompt. Returns the text, or None if cancelled."""
        t = self.theme
        win = iot_theme.make_dialog(self, title)
        iot_theme.dialog_header(win, t, title, subtitle)
        body = t.paint(tk.Frame(win, bd=0), bg="surface")
        body.pack(fill=tk.X, padx=22, pady=(8, 0))
        t.paint(tk.Label(body, text=label, font=t.f["small"], bd=0, anchor="w"), bg="surface", fg="text_muted").pack(fill=tk.X, pady=(0, 4))
        value = tk.StringVar(value=default_value)
        field = iot_theme.entry(body, t, value)
        field.pack(fill=tk.X, ipady=6)
        result = [None]

        def confirm(event=None):
            result[0] = value.get()
            win.destroy()
        iot_theme.dialog_buttons(win, t, [(confirm_text, confirm, "primary"), ("Cancel", win.destroy, "secondary")])
        win.bind("<Return>", confirm)
        win.bind("<Escape>", lambda e: win.destroy())
        iot_theme.show_dialog(self, win, min_width=460)
        field.focus_set()
        field.select_range(0, tk.END)
        field.icursor(tk.END)
        self.root.wait_window(win)
        return result[0]

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
        self.log_status(f"Folder renamed to '{clean_new_title}'.", 'success')

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
                self.log_status(f"Folder tags could not be saved: {err}", 'warning')

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

    def _draw_tag_dot(self, canvas, bg, colour):
        canvas.configure(bg=bg)
        canvas.delete("all")
        if colour:
            canvas.create_oval(3, 2, 13, 12, fill=self.theme.c["tag_" + colour], outline="")

    def _close_tag_picker(self, event=None):
        picker = self._tag_picker
        self._tag_picker = None
        if picker is not None:
            try:
                picker.grab_release()
                picker.destroy()
            except tk.TclError:
                pass

    def _open_tag_picker_for_selector(self):
        self._close_tree_popup()
        selector = self.folder_selector
        x = self.folder_name_label.winfo_rootx() - 8
        y = selector.winfo_rooty() + selector.winfo_height() + 4
        self._open_tag_picker(self.root, x, y, self.image_folder, None)

    def _open_tag_picker(self, window, x_root, y_root, path, on_change):
        """A row of colour circles (plus an outlined 'no tag' one) floating inside `window`. It sits inside
        the same window, so opening it from the folder list doesn't close the list."""
        self._close_tag_picker()
        if self._tag_key(path) is None:
            return
        t, c = self.theme, self.theme.c
        current = self.folder_tag(path)
        picker = tk.Frame(window, bg=c["surface"], bd=0, highlightthickness=1, highlightbackground=c["border"])
        size = 24

        def choose(colour):
            self._close_tag_picker()
            self.set_folder_tag(path, colour)
            if on_change is not None and self.tree_popup_window is not None:
                on_change()

        for colour in TAG_COLORS + (None,):
            swatch = tk.Canvas(picker, width=size, height=size, bd=0, highlightthickness=0, bg=c["surface"], cursor="hand2")
            swatch.pack(side=tk.LEFT, padx=(6 if colour == TAG_COLORS[0] else 1, 6 if colour is None else 1), pady=6)
            if colour == current or (colour is None and current is None):
                swatch.create_oval(1, 1, size - 1, size - 1, outline=c["text_muted"], width=1.5)  # the current choice
            if colour:
                swatch.create_oval(5, 5, size - 5, size - 5, fill=c["tag_" + colour], outline="")
            else:
                swatch.create_oval(6, 6, size - 6, size - 6, outline=c["text_muted"], width=1.5)  # "no tag"
            swatch.bind("<Button-1>", lambda e, value=colour: choose(value))

        picker.update_idletasks()
        width, height = picker.winfo_reqwidth(), picker.winfo_reqheight()
        left = x_root - window.winfo_rootx() - 10
        top = y_root - window.winfo_rooty() + 8
        left = max(4, min(left, window.winfo_width() - width - 4))
        top = max(4, min(top, window.winfo_height() - height - 4))
        picker.place(x=left, y=top)
        picker.lift()
        self._tag_picker = picker

        # Any click outside the circles closes it (while it holds the grab, outside clicks come here first)
        def outside_click(event):
            if not str(event.widget).startswith(str(picker)):
                self._close_tag_picker()
                return "break"
            x, y = event.x_root, event.y_root
            px, py = picker.winfo_rootx(), picker.winfo_rooty()
            if not (px <= x < px + picker.winfo_width() and py <= y < py + picker.winfo_height()):
                self._close_tag_picker()
                return "break"
        picker.bind("<ButtonPress>", outside_click)
        try:
            picker.grab_set()
        except tk.TclError:
            pass

    # ------------------------------------------------------------------ delete folder (Q)

    def _is_pending_delete(self, path):
        return any(iot_history._same_or_inside(path, marked) for marked in self.pending_delete_folders)

    def _struck_font(self, font):
        """The same font with a line through it (for folders marked for deletion)."""
        key = repr(font)
        if key not in self._struck_fonts:
            struck = tkfont.Font(font=font)
            struck.configure(overstrike=1)
            self._struck_fonts[key] = struck
        return self._struck_fonts[key]

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
            self.log_status(f"'{name}' will be kept.", 'success')
            return
        if self._is_pending_delete(folder):
            self.log_status("A folder above this one is already marked for deletion; open it and press Q to keep it.", 'warning')
            return

        info = self.folder_info.get(folder) or {}
        images, total_bytes = info.get("total_image_count", len(self.all_files)), info.get("total_bytes", 0)
        sub_folders = sum(1 for other in self.folder_info_list
                          if other["path"] != folder and iot_history._same_or_inside(other["path"], folder))
        contents = f"{images} image{'s' if images != 1 else ''} ({format_bytes(total_bytes)})"
        if sub_folders:
            contents += f" and {sub_folders} sub-folder{'s' if sub_folders != 1 else ''}"

        t = self.theme
        win = iot_theme.make_dialog(self, "Delete folder")
        iot_theme.dialog_header(win, t, f"Delete '{name}'?",
                                f"The folder and everything in it, {contents}, will be permanently deleted when "
                                "I.O.T closes. Until then it stays, crossed out in the folder list; press Q again "
                                f"or Undo ({UNDO_KEYS}) to keep it.")
        confirmed = [False]

        def confirm(event=None):
            confirmed[0] = True
            win.destroy()
        iot_theme.dialog_buttons(win, t, [("Delete", confirm, "danger"), ("Cancel", win.destroy, "secondary")])
        win.bind("<Return>", confirm)
        win.bind("<Escape>", lambda e: win.destroy())
        iot_theme.show_dialog(self, win, min_width=460)
        self.root.wait_window(win)
        if not confirmed[0]:
            return

        self.pending_delete_folders.add(folder)
        self._record("Folder delete", f"Marked folder '{name}' for deletion", os.path.dirname(folder),
                     [("tag_delete", folder)], [f"{name}: {contents}, deleted when I.O.T closes"])
        self.refresh_folder_button()
        self.log_status(f"'{name}' will be deleted when I.O.T closes  (Q again or {UNDO_KEYS} keeps it)", 'warning')

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

    def _confirm(self, title, heading, text, confirm_text, variant="primary"):
        """Themed yes / no dialog. Returns True when confirmed."""
        t = self.theme
        win = iot_theme.make_dialog(self, title)
        iot_theme.dialog_header(win, t, heading, text)
        confirmed = [False]

        def confirm(event=None):
            confirmed[0] = True
            win.destroy()
        iot_theme.dialog_buttons(win, t, [(confirm_text, confirm, variant), ("Cancel", win.destroy, "secondary")])
        win.bind("<Return>", confirm)
        win.bind("<Escape>", lambda e: win.destroy())
        iot_theme.show_dialog(self, win, min_width=480)
        self.root.wait_window(win)
        return confirmed[0]

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
            self.log_status("Nothing to rename: the images there are already named after their folders.", 'info')
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

        self.close_tooltip()
        steps, errors, changed, current_map = [], [], [], None
        self.root.config(cursor="watch")
        self.root.update_idletasks()
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
            self.root.config(cursor="")

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
            self.log_status(f"Renamed {renamed} images; {len(errors)} could not be renamed: {errors[0]}", 'error')
        else:
            self.log_status(f"Renamed {renamed} images after their folders  ({UNDO_KEYS} to undo)", 'success')

    # ------------------------------------------------------------------ resizer

    def run_resizer(self, mode, folders=None):
        """folders: resize these folders' images (a folder list branch); default: the folder on screen."""
        if not self.busy:
            iot_tools.run_image_resizer_thread(self, mode, folders)

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

        t = self.theme
        folder = self.image_folder
        files = list(self.all_files)
        baseline_filename = files[n - 1]
        baseline_full_path = os.path.join(folder, baseline_filename)

        progress_box = iot_theme.make_dialog(self, "Finding duplicates")
        iot_theme.dialog_header(progress_box, t, "Finding duplicates",
                                f"Comparing every image with the {iot_utils.ordinal(n)} image (the base image):\n{baseline_filename}")
        bar_frame = t.paint(tk.Frame(progress_box, bd=0), bg="surface")
        bar_frame.pack(fill=tk.X, padx=22, pady=(10, 22))
        prog_bar = ttk.Progressbar(bar_frame, orient=tk.HORIZONTAL, mode='determinate', maximum=len(files), style="IOT.Horizontal.TProgressbar")
        prog_bar.pack(fill=tk.X)
        iot_theme.show_dialog(self, progress_box, min_width=440)

        self.busy = True
        executor = ThreadPoolExecutor(max_workers=max(2, min(8, os.cpu_count() or 4)), thread_name_prefix="iot-hash")
        paths = [os.path.join(folder, name) for name in files]
        futures = [executor.submit(self._get_hash, path) for path in paths]
        executor.shutdown(wait=False)

        def poll():
            done_count = sum(1 for future in futures if future.done())
            prog_bar['value'] = done_count
            if done_count < len(futures):
                progress_box.after(60, poll)
                return
            try:
                progress_box.grab_release()
                progress_box.destroy()
            except tk.TclError:
                pass
            self.busy = False

            base_hash = futures[n - 1].result()
            if base_hash is None:
                self.log_status(f"Could not read the baseline image '{baseline_filename}'.", 'error')
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
                self.log_status("No duplicates (98%+ match) found in this folder.", 'info')
                return
            self.display_side_by_side_duplicates_window(matched_twin_paths)

        progress_box.after(60, poll)

    def display_side_by_side_duplicates_window(self, array_paths):
        c = self.theme.c
        dup_window = tk.Toplevel(self.root)
        dup_window.wm_overrideredirect(True)
        dup_window.configure(bg="#0c0c0d")
        s_w, s_h = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        dup_window.wm_geometry(f"{s_w}x{s_h}+0+0")

        lbl_title_banner = tk.Label(dup_window, text=f"{len(array_paths)} matching images  ·  click one to jump to it  ·  Esc to close",
                                    bg="#17181b", fg="#e7e8ea", font=self.theme.f["body_strong"], pady=10)
        lbl_title_banner.pack(fill=tk.X)
        frame_container = tk.Frame(dup_window, bg="#0c0c0d")
        frame_container.pack(fill=tk.BOTH, expand=True, padx=20, pady=20)

        max_allocated_tile_width = (s_w - 60) // len(array_paths)
        max_allocated_tile_height = s_h - 120
        self.twin_photos_references_keepalive = []

        def make_jump_callback(target_filename):
            def callback(event):
                dup_window.destroy()
                self._set_duplicate_highlight(target_filename)
                self.jump_to_specific_image_filename(target_filename)
            return callback

        for path_file in array_paths:
            try:
                orig_w, orig_h = iot_utils.read_display_size(path_file)
                ratio = min(max_allocated_tile_width / orig_w, max_allocated_tile_height / orig_h)
                preview, _ = iot_utils.load_preview_image(path_file, max(1, int(orig_w * ratio)), max(1, int(orig_h * ratio)))
                resized_photo = ImageTk.PhotoImage(preview)
                self.twin_photos_references_keepalive.append(resized_photo)

                tile_box = tk.Frame(frame_container,bg="#141416", bd=0, highlightthickness=1,
                                    highlightbackground="#2c2f35", cursor="hand2")
                tile_box.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=10)
                lbl_img = tk.Label(tile_box, image=resized_photo, bg="#0c0c0d")
                lbl_img.pack(fill=tk.BOTH, expand=True, pady=10)
                lbl_txt = tk.Label(tile_box, text=f"{os.path.basename(path_file)}\n{orig_w} × {orig_h}", bg="#141416",
                                   fg="#e7e8ea", font=self.theme.f["small"])
                lbl_txt.pack(side=tk.BOTTOM, pady=6)

                callback = make_jump_callback(os.path.basename(path_file))
                for widget in (tile_box, lbl_img, lbl_txt):
                    widget.bind("<Button-1>", callback)
                    widget.bind("<Enter>", lambda e, b=tile_box: b.configure(highlightbackground=c["accent"]))
                    widget.bind("<Leave>", lambda e, b=tile_box: b.configure(highlightbackground="#2c2f35"))
            except Exception:
                pass

        lbl_title_banner.bind("<Button-1>", lambda e: dup_window.destroy())
        frame_container.bind("<Button-1>", lambda e: dup_window.destroy())
        dup_window.bind("<Escape>", lambda e: dup_window.destroy())
        dup_window.focus_force()

    # ------------------------------------------------------------------ group mode (G)

    def _create_group_button(self, parent):
        """The Group button sits in a canvas 2px larger all round, where the animated border is drawn."""
        t = self.theme
        wrap = t.paint(tk.Canvas(parent, bd=0, highlightthickness=0), bg="surface")
        wrap.pack(side=tk.LEFT, padx=(0, 6))
        self.group_button = Button(wrap, t, "Group", self.toggle_group_mode, variant="secondary", hint="G",
                                   padx=8, pady=4, font=t.f["small"])
        wrap.create_window(2, 2, window=self.group_button, anchor="nw")
        self._group_wrap = wrap

        def fit(event):
            wrap.configure(width=event.width + 4, height=event.height + 4)
            self._draw_group_border()
        self.group_button.bind("<Configure>", fit, add="+")
        t.listen(self._draw_group_border, owner=wrap)

    def _draw_group_border(self):
        """Thin border with a bright segment running round it while group mode is on."""
        wrap = self._group_wrap
        wrap.delete("border")
        if not self.group_mode:
            return
        c = self.theme.c
        width, height = int(wrap.cget("width")), int(wrap.cget("height"))
        wrap.create_rectangle(0, 0, width - 1, height - 1, outline=c["accent_soft"], width=1, tags="border")
        x0, y0, x1, y1 = 1, 1, width - 1, height - 1
        corners = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
        edges = [x1 - x0, y1 - y0, x1 - x0, y1 - y0]
        perimeter = sum(edges)
        starts = [sum(edges[:k]) for k in range(4)]  # distance round the border at which each corner sits

        def point_at(distance):
            distance %= perimeter
            for k in range(4):
                if distance <= starts[k] + edges[k]:
                    (ax, ay), (bx, by) = corners[k], corners[(k + 1) % 4]
                    f = (distance - starts[k]) / edges[k] if edges[k] else 0
                    return ax + (bx - ax) * f, ay + (by - ay) * f
            return corners[0]

        head = self._group_anim_phase * perimeter
        length = perimeter * 0.3
        points = [point_at(head)]
        for corner_distance, corner in sorted(zip(starts + [s + perimeter for s in starts], corners * 2)):
            if head < corner_distance < head + length:
                points.append(corner)
        points.append(point_at(head + length))
        wrap.create_line(*[v for p in points for v in p], fill=c["accent"], width=2, capstyle=tk.ROUND,
                         joinstyle=tk.ROUND, tags="border")

    def _animate_group_border(self):
        self._group_anim_job = None
        if not self.group_mode:
            return
        self._group_anim_phase = (self._group_anim_phase + 0.012) % 1.0
        self._draw_group_border()
        self._group_anim_job = self.root.after(40, self._animate_group_border)

    def toggle_group_mode(self):
        focused = self.root.focus_get()
        if focused is not None and focused.winfo_class() == "Entry":
            return
        if self.busy and not self.group_mode:
            return
        self.group_mode = not self.group_mode
        picked_before = list(self.group_picks)
        self.group_picks = {}
        if self.group_mode:
            self._set_selection(None)  # a half-made range selection doesn't carry into group mode
            self.log_status("Group mode: click pictures to pick them, Enter copies them into a folder.", 'info')
            self._animate_group_border()
        else:
            if self._group_anim_job is not None:
                self.root.after_cancel(self._group_anim_job)
                self._group_anim_job = None
            self._draw_group_border()
        for name in picked_before:
            self._restyle_index(self.file_index.get(name))
        self._refresh_group_button()
        self._update_status_info()

    def _refresh_group_button(self):
        count = len(self.group_picks)
        self.group_button.set_text(f"Group · {count}" if self.group_mode and count else "Group")

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
            self.log_status("Nothing picked yet: click some pictures first (Esc leaves group mode).", 'warning')
            return
        self.close_tooltip()
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
        self.root.config(cursor="watch")
        self.root.update_idletasks()
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
            self.root.config(cursor="")

        copied = len(names) - len(errors)
        log_lines = [iot_history.describe_range(names)]
        if errors:
            log_lines.append(f"{len(errors)} file(s) could not be copied: " + "; ".join(errors[:5]))
        self._record("Copy", f"Copied {copied} files into '{tag}'", source_dir, steps, log_lines, target=target_dir)

        self.toggle_group_mode()  # done: leave group mode
        self.refresh_folders([source_dir, target_dir])  # a new folder means a full scan
        self.refresh_folder_button()
        if errors:
            self.log_status(f"Copied {copied} files; {len(errors)} could not be copied: {errors[0]}", 'error')
        else:
            self.log_status(f"Copied {copied} files to '{tag}'  ({UNDO_KEYS} to undo)", 'success')

    # ------------------------------------------------------------------ click handling / batch moves

    def _on_tile_click(self, tile, mode):
        idx = self.file_index.get(tile.filename)
        if idx is not None:
            self.handle_image_click(idx, mode)
        return "break"

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
                self.log_status(f"Deleted {len(deleted)} files; {failed} could not be deleted.", 'error')
            else:
                self.log_status(f"Deleted {len(deleted)} files  ({UNDO_KEYS} to undo)", 'success')
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
                            "so their names wouldn't change.", 'warning')
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
            folder_tag = self.ask_custom_dark_folder_dialog((self.last_folder_name or "Sequence").strip(), len(files_to_move), range_summary, is_skip_folder)

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
            self.canvas.yview_moveto(0.0)
            self._update_view(force=True)
        self.refresh_folders([source_dir, target_dir])  # a new folder means a full scan
        self.refresh_folder_button()
        if errors:
            self.log_status(f"{len(errors)} file(s) could not be moved: {errors[0]}", 'error')
        elif is_skip_folder:
            self.log_status(f"Renamed {len(files_to_move)} files as '{folder_tag} - ##'.", 'success')
        else:
            self.log_status(f"Moved {len(files_to_move)} files to '{folder_tag}'.", 'success')

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
            title, confirm = f"Rename {count} files", "Rename"
        else:
            title, confirm = f"Move {count} files into a folder", "Move"
        return self._ask_text(title, summary, "Name", default_value, confirm)

    # ------------------------------------------------------------------ shutdown

    def check_and_cleanup_cache(self):
        for cache in [self.disk_cache] + [cache for _, cache in self._retired_hubs]:
            if self.keep_cache_var.get():
                cache.trim()
            else:
                cache.delete()

    def handle_manual_exit_cleanup(self):
        """Runs when the window's close button is pressed."""
        if self.busy:
            self.log_status("Please wait for the current batch to finish (or cancel it) before closing.", 'warning')
            return
        self.save_settings()
        self._thumb_loader.stop()
        self._delete_pending_folders()
        for session in [self.session] + [session for session, _ in self._retired_hubs]:
            session.cleanup()           # permanently removes this session's deleted files
        self.check_and_cleanup_cache()
        self.root.destroy()


if __name__ == "__main__":
    try:
        root = tk.Tk()
        app = DarkGridImageSorter(root)
        root.mainloop()
    except Exception:
        print("\n" + "=" * 50 + "\n❌ CRITICAL INTERFACE ERROR:\n" + "=" * 50)
        log_crash("crash", *sys.exc_info())
        print(f"\nDetails were saved to {CRASH_LOG_FILE}")
        input("\nPress ENTER to close window...")
