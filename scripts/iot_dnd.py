"""
Windows window hooks for I.O.T (Windows only, no extra packages):

* Drag and drop from Explorer / OneCommander onto a Tk window.
* Stopping a lone Alt press from freezing the window (see suppress_alt_menu).


Uses the classic WM_DROPFILES message: the window is marked as accepting files and its window
procedure is wrapped, so drops are caught before Tk sees them. Dropping onto any widget inside the
window works, because Windows passes the drop up to the nearest window that accepts files.
"""
import sys
import ctypes
import collections
import traceback

WM_DROPFILES = 0x0233
WM_COPYDATA = 0x004A
WM_COPYGLOBALDATA = 0x0049
WM_SYSCOMMAND = 0x0112
SC_KEYMENU = 0xF100
GWLP_WNDPROC = -4
MSGFLT_ALLOW = 1

_hooks = {}  # hwnd -> (new wndproc, old wndproc); the ctypes callback must stay referenced

if sys.platform.startswith("win"):
    from ctypes import wintypes
    LRESULT = ctypes.c_ssize_t
    WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

    # Private DLL handles, so these argtypes don't affect ctypes.windll used elsewhere
    _user32 = ctypes.WinDLL("user32")
    _shell32 = ctypes.WinDLL("shell32")
    _user32.SetWindowLongPtrW.restype = ctypes.c_void_p
    _user32.SetWindowLongPtrW.argtypes = (wintypes.HWND, ctypes.c_int, ctypes.c_void_p)
    _user32.CallWindowProcW.restype = LRESULT
    _user32.CallWindowProcW.argtypes = (ctypes.c_void_p, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
    _user32.GetParent.restype = wintypes.HWND
    _user32.GetParent.argtypes = (wintypes.HWND,)
    _user32.ChangeWindowMessageFilterEx.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.DWORD, ctypes.c_void_p)
    _shell32.DragAcceptFiles.argtypes = (wintypes.HWND, wintypes.BOOL)
    _shell32.DragQueryFileW.restype = wintypes.UINT
    _shell32.DragQueryFileW.argtypes = (wintypes.HANDLE, wintypes.UINT, wintypes.LPWSTR, wintypes.UINT)
    _shell32.DragFinish.argtypes = (wintypes.HANDLE,)


def _dropped_paths(hdrop):
    paths = []
    count = _shell32.DragQueryFileW(hdrop, 0xFFFFFFFF, None, 0)
    for index in range(count):
        length = _shell32.DragQueryFileW(hdrop, index, None, 0)
        buffer = ctypes.create_unicode_buffer(length + 1)
        _shell32.DragQueryFileW(hdrop, index, buffer, length + 1)
        paths.append(buffer.value)
    _shell32.DragFinish(hdrop)
    return paths


def enable_drop(widget, callback):
    """Calls callback([paths]) on the Tk thread whenever files or folders are dropped onto widget.
    Returns False when drag and drop isn't available (not Windows, or the hook failed)."""
    if not sys.platform.startswith("win"):
        return False
    try:
        widget.update_idletasks()
        hwnd = widget.winfo_id()
        if hwnd in _hooks:
            return True
        pending = collections.deque()

        # Tk must not be called from inside the window procedure (it crashes the interpreter), so
        # drops are queued there and handed over by this timer on the Tk thread.
        def deliver():
            while pending:
                try:
                    callback(pending.popleft())
                except Exception:
                    traceback.print_exc()
            widget.after(100, deliver)

        def window_proc(h, message, wparam, lparam):
            if message == WM_DROPFILES:
                try:
                    pending.append(_dropped_paths(wparam))
                except Exception:
                    pass
                return 0
            return _user32.CallWindowProcW(_hooks[hwnd][1], h, message, wparam, lparam)

        new_proc = WNDPROC(window_proc)
        _hooks[hwnd] = (new_proc, None)
        old_proc = _user32.SetWindowLongPtrW(hwnd, GWLP_WNDPROC, ctypes.cast(new_proc, ctypes.c_void_p))
        if not old_proc:
            del _hooks[hwnd]
            return False
        _hooks[hwnd] = (new_proc, old_proc)
        # If I.O.T runs as administrator, Windows blocks drops from a normal Explorer unless allowed
        for message in (WM_DROPFILES, WM_COPYDATA, WM_COPYGLOBALDATA):
            try:
                _user32.ChangeWindowMessageFilterEx(hwnd, message, MSGFLT_ALLOW, None)
            except Exception:
                pass
        _shell32.DragAcceptFiles(hwnd, True)
        deliver()
        return True
    except Exception:
        return False


def suppress_alt_menu(root):
    """
    On Windows, pressing and releasing Alt on its own puts a window into an invisible "menu mode"
    (waiting for a menu key). I.O.T has no menu bar, so the window just seems frozen until focus
    moves to another program, which can happen after Alt+Click range deletes. This ignores only that
    Alt-alone request; Alt+Space (the window menu) and every Alt+Click keep working.
    """
    if not sys.platform.startswith("win"):
        return False
    try:
        root.update_idletasks()
        frame = _user32.GetParent(root.winfo_id())  # the outer window that owns the title bar
        if not frame or frame in _hooks:
            return bool(frame)

        def window_proc(h, message, wparam, lparam):
            # lparam 0 = triggered by Alt alone (Alt+Space and Alt+letter pass a character)
            if message == WM_SYSCOMMAND and (wparam & 0xFFF0) == SC_KEYMENU and lparam == 0:
                return 0
            return _user32.CallWindowProcW(_hooks[frame][1], h, message, wparam, lparam)

        new_proc = WNDPROC(window_proc)
        _hooks[frame] = (new_proc, None)
        old_proc = _user32.SetWindowLongPtrW(frame, GWLP_WNDPROC, ctypes.cast(new_proc, ctypes.c_void_p))
        if not old_proc:
            del _hooks[frame]
            return False
        _hooks[frame] = (new_proc, old_proc)
        return True
    except Exception:
        return False
