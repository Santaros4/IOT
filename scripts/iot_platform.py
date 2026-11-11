"""
Everything that differs between Windows, macOS and Linux lives here, so the rest of I.O.T stays the same
on all three. Windows behaviour is the reference: nothing here changes how I.O.T works on Windows.

* Keys and mouse buttons: Ctrl-style shortcuts use Cmd on macOS, Alt is Option there, right-click is a
  different button number on macOS, and the Mac "delete" key is Backspace.
* Window management: maximising, and snapping to the right half of the screen (half-window mode).
* The screen grab behind the Hover + Hold Z preview.
* Drag and drop: a built-in Windows hook; on macOS / Linux the optional `tkinterdnd2` package.
* Flag symbols (colour emoji can crash Tk on Linux), and where settings go if the program folder is read-only.
"""
import os
import sys

IS_WINDOWS = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"
IS_LINUX = not IS_WINDOWS and not IS_MAC

# ---------------------------------------------------------------- keys and mouse

MOD = "Command" if IS_MAC else "Control"          # Tk modifier for Ctrl-style shortcuts (Ctrl+Z, Ctrl+Click...)
MOD_LABEL = "Cmd" if IS_MAC else "Ctrl"           # ...and what the user sees
ALT = "Option" if IS_MAC else "Alt"               # Tk modifier for Alt+Click
ALT_LABEL = "Option" if IS_MAC else "Alt"
MOD_KEYSYMS = ("Meta_L", "Meta_R") if IS_MAC else ("Control_L", "Control_R")  # the modifier key itself (taps)

# Right-click: button 3 on Windows / Linux, button 2 on macOS, where Ctrl+Click also means right-click
RIGHT_CLICK_EVENTS = ("<Button-2>", "<Control-Button-1>") if IS_MAC else ("<Button-3>",)
# The key that deletes the picture under the pointer (Mac keyboards label Backspace "delete")
DELETE_EVENTS = ("<Delete>", "<BackSpace>") if IS_MAC else ("<Delete>",)
# X11 key auto-repeat sends release + press pairs while a key is held; Hold Z must not treat those as let go
KEY_REPEAT_SENDS_RELEASE = IS_LINUX


def keys(text):
    """Shortcut text as this platform names it: 'Ctrl+Z' -> 'Cmd+Z', 'Alt+Click' -> 'Option+Click' on macOS."""
    if not IS_MAC:
        return text
    return text.replace("Ctrl", MOD_LABEL).replace("Alt", ALT_LABEL)


def bind_right_click(widget, handler, add=None):
    for event in RIGHT_CLICK_EVENTS:
        widget.bind(event, handler, add=add)


# ---------------------------------------------------------------- symbols

# Windows and macOS draw colour emoji; Tk on Linux can crash (X error BadLength) when a colour emoji font is
# picked for a character, so plain symbols are used there.
if IS_LINUX:
    FLAG_TARGET, FLAG_SIZE, FLAG_MODIFIED = "◎", "!", "⚑"      # ◎ ! ⚑
else:
    FLAG_TARGET, FLAG_SIZE, FLAG_MODIFIED = "\U0001F3AF", "❗", "⚑"  # 🎯 ❗ ⚑


# ---------------------------------------------------------------- files

def fallback_config_dir():
    """Where settings and the change log go when the program folder can't be written to."""
    if IS_WINDOWS:
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    elif IS_MAC:
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "IOT")


# ---------------------------------------------------------------- windows

def maximize(root):
    for attempt in (lambda: root.state("zoomed"),               # Windows / macOS
                    lambda: root.attributes("-zoomed", True)):  # Linux (X11)
        try:
            attempt()
            return
        except Exception:
            continue
    root.geometry(f"{root.winfo_screenwidth()}x{root.winfo_screenheight()}+0+0")


def is_maximized(root):
    try:
        if root.state() == "zoomed":
            return True
    except Exception:
        pass
    if IS_LINUX:
        try:
            return bool(int(root.attributes("-zoomed")))  # X11 reports maximised here, not in state()
        except Exception:
            return False
    return False


def unmaximize(root):
    if IS_LINUX:
        try:
            root.attributes("-zoomed", False)
        except Exception:
            pass
    try:
        root.state("normal")
    except Exception:
        pass


def snap_right_half(root):
    """Fills the right half of the screen the window is on (taskbar / menu bar excluded where known)."""
    unmaximize(root)
    root.update()  # let the restore finish so the frame can be measured
    if IS_WINDOWS and _snap_right_half_windows(root):
        return
    width, height = root.winfo_screenwidth(), root.winfo_screenheight()
    top = 28 if IS_MAC else 0                     # macOS menu bar
    bottom = 80 if IS_MAC else 60                 # Dock / panel: a safe margin, the exact size isn't known
    root.geometry(f"{width - width // 2}x{height - top - bottom}+{width // 2}+{top}")


def _snap_right_half_windows(root):
    try:
        import ctypes
        import ctypes.wintypes as wintypes
        user32 = ctypes.windll.user32
        hwnd = user32.GetParent(root.winfo_id())

        class MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_ulong), ("rcMonitor", wintypes.RECT),
                        ("rcWork", wintypes.RECT), ("dwFlags", ctypes.c_ulong)]
        info = MONITORINFO()
        info.cbSize = ctypes.sizeof(MONITORINFO)
        monitor = user32.MonitorFromWindow(hwnd, 2)  # MONITOR_DEFAULTTONEAREST
        user32.GetMonitorInfoW(monitor, ctypes.byref(info))
        work = info.rcWork
        half = (work.right - work.left) // 2
        # Windows 10/11 frames have invisible resize borders; grow by them so the visible edges line up
        outer, visible = wintypes.RECT(), wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(outer))
        if ctypes.windll.dwmapi.DwmGetWindowAttribute(hwnd, 9, ctypes.byref(visible), ctypes.sizeof(visible)) != 0:
            visible = outer  # DWMWA_EXTENDED_FRAME_BOUNDS unavailable: no adjustment
        left, top = visible.left - outer.left, visible.top - outer.top
        right, bottom = outer.right - visible.right, outer.bottom - visible.bottom
        # Tk's geometry is the inside size plus the outer frame's position; going through Tk
        # (not SetWindowPos) stops it putting its own remembered size back afterwards
        frame_w = (outer.right - outer.left) - root.winfo_width()
        frame_h = (outer.bottom - outer.top) - root.winfo_height()
        width = (work.right - work.left - half) + left + right - frame_w
        height = (work.bottom - work.top) + top + bottom - frame_h
        root.geometry(f"{width}x{height}+{work.left + half - left}+{work.top - top}")
        return True
    except Exception:
        return False


# ---------------------------------------------------------------- screen grab (frosted Zoom background)

def grab_screen():
    """A screenshot of the main screen as a PIL image, or None where it isn't possible: macOS without Screen
    Recording permission returns just the wallpaper, and Wayland desktops refuse (no frost there)."""
    try:
        from PIL import ImageGrab
        return ImageGrab.grab()
    except Exception:
        return None


# ---------------------------------------------------------------- drag and drop

def enable_drop(root, callback):
    """Calls callback([paths]) when files or folders are dropped onto the window. Windows: built in.
    macOS / Linux: needs the optional tkinterdnd2 package (pip install tkinterdnd2). Returns True if enabled."""
    if IS_WINDOWS:
        import iot_dnd
        return iot_dnd.enable_drop(root, callback)
    try:
        from tkinterdnd2 import TkinterDnD, DND_FILES
        TkinterDnD._require(root)  # loads the tkdnd extension into this (plain) Tk window
        TkinterDnD.DnDWrapper.drop_target_register(root, DND_FILES)

        def dropped(event):
            callback(list(root.tk.splitlist(event.data)))  # paths with spaces arrive wrapped in {braces}
            return event.action
        TkinterDnD.DnDWrapper.dnd_bind(root, "<<Drop>>", dropped)
        return True
    except Exception:
        return False


def after_window_built(root):
    """Platform fix-ups once the main window exists."""
    if IS_WINDOWS:
        import iot_dnd
        iot_dnd.suppress_alt_menu(root)  # a lone Alt press (e.g. after Alt+Click) would freeze the window
