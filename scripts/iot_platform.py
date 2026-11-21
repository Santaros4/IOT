"""
The few things that differ between Windows, macOS and Linux. Qt handles most differences itself (Cmd for Ctrl,
right-click, drag and drop, key auto-repeat); what's left lives here. Windows behaviour is the reference.

* Shortcut names as each platform calls them (Cmd / Option on macOS).
* Flag symbols (colour emoji aren't installed on every Linux desktop).
* Where settings go if the program folder is read-only.
* The screen grab behind the frosted Zoom preview.
"""
import os
import sys

IS_WINDOWS = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"
IS_LINUX = not IS_WINDOWS and not IS_MAC

MOD_LABEL = "Cmd" if IS_MAC else "Ctrl"           # what the user sees for Ctrl-style shortcuts
TRASH_NAME = "Recycle Bin" if IS_WINDOWS else "Trash"
ALT_LABEL = "Option" if IS_MAC else "Alt"


def keys(text):
    """Shortcut text as this platform names it: 'Ctrl+Z' -> 'Cmd+Z', 'Alt+Click' -> 'Option+Click' on macOS."""
    if not IS_MAC:
        return text
    return text.replace("Ctrl", MOD_LABEL).replace("Alt", ALT_LABEL)


# ---------------------------------------------------------------- symbols

# Windows and macOS draw colour emoji; many Linux desktops have no colour emoji font, so plain symbols are used there.
if IS_LINUX:
    FLAG_SIZE, FLAG_MODIFIED = "!", "⚑"
else:
    FLAG_SIZE, FLAG_MODIFIED = "❗", "⚑"
# The resize flags are text arrows that I.O.T colours itself (iot_qt.flag_html), not emoji. Windows has the heavy
# arrows in Segoe UI Symbol; elsewhere the plain heavy arrows (text style, not emoji) are in the usual fonts.
if IS_WINDOWS:
    FLAG_DOWN, FLAG_UP, FLAG_ARROW_FONT = "\U0001F847", "\U0001F845", "Segoe UI Symbol"   # 🡇 🡅
else:
    FLAG_DOWN, FLAG_UP, FLAG_ARROW_FONT = "\u2B07\uFE0E", "\u2B06\uFE0E", ""             # ⬇ ⬆


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


# ---------------------------------------------------------------- screen grab (frosted Zoom background)

def grab_screen():
    """A screenshot of the main screen as a PIL image, or None where it isn't possible: macOS without Screen
    Recording permission returns just the wallpaper, and Wayland desktops refuse (no frost there)."""
    try:
        from PIL import ImageGrab
        return ImageGrab.grab()
    except Exception:
        return None
