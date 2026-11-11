"""
Light / dark theming and the small set of flat, modern widgets the I.O.T interface is built from.

Every coloured widget registers which palette colour each of its options uses, so switching theme
re-colours the live interface in place (no rebuild, no lost scroll position).
"""
import sys
import tkinter as tk
from tkinter import ttk, font as tkfont

PALETTES = {
    "dark": {
        "bg": "#17181b",          # window / grid background
        "surface": "#1f2125",     # header, toolbar, status bar, dialogs
        "surface_alt": "#26292e", # inputs, popups, raised areas
        "border": "#32363c",
        "divider": "#2a2d32",
        "text": "#e7e8ea",
        "text_muted": "#9ba0a8",
        "text_faint": "#6c717a",
        "accent": "#3b8cff",
        "accent_hover": "#5a9eff",
        "accent_soft": "#1d3557",
        "accent_text": "#ffffff",
        "button": "#2b2e34",
        "button_hover": "#353940",
        "button_pressed": "#40444c",
        "keycap": "#34383f",
        "tile": "#212328",
        "tile_hover": "#282b31",
        "tile_border": "#2c2f35",
        "danger": "#f05252",
        "danger_hover": "#f46b6b",
        "warning": "#f5a524",
        "success": "#2fbf71",
        "error_thumb": "#3a3d43",
        "group": "#1a3a9e",       # pictures picked in group mode (G)
        # Folder tag colours (right-click a folder)
        "tag_red": "#f05252", "tag_orange": "#f59e0b", "tag_yellow": "#facc15", "tag_green": "#22c55e",
        "tag_blue": "#3b82f6", "tag_purple": "#a855f7", "tag_grey": "#9ca3af",
    },
    "light": {
        "bg": "#eef0f3",
        "surface": "#ffffff",
        "surface_alt": "#f6f7f9",
        "border": "#d8dce2",
        "divider": "#e6e9ed",
        "text": "#1d2127",
        "text_muted": "#596170",
        "text_faint": "#8a919c",
        "accent": "#1f6feb",
        "accent_hover": "#3b82f6",
        "accent_soft": "#dbe8ff",
        "accent_text": "#ffffff",
        "button": "#eef0f3",
        "button_hover": "#e2e6eb",
        "button_pressed": "#d5dae1",
        "keycap": "#e4e7ec",
        "tile": "#ffffff",
        "tile_hover": "#f4f7fd",
        "tile_border": "#dde1e7",
        "danger": "#d93025",
        "danger_hover": "#e8473d",
        "warning": "#b76e00",
        "success": "#1a8a4a",
        "error_thumb": "#d9dde3",
        "group": "#172f6e",
        "tag_red": "#dc2626", "tag_orange": "#ea580c", "tag_yellow": "#ca8a04", "tag_green": "#16a34a",
        "tag_blue": "#2563eb", "tag_purple": "#9333ea", "tag_grey": "#6b7280",
    },
}


class Theme:
    def __init__(self, root, name="dark"):
        self.root = root
        self.name = name if name in PALETTES else "dark"
        self.c = dict(PALETTES[self.name])
        self._painted = []     # (widget, {option: palette key})
        self._listeners = []   # (owner widget or None, callback)
        self._init_fonts()
        self.style = ttk.Style(root)
        try:
            self.style.theme_use("clam")
        except tk.TclError:
            pass
        self._configure_ttk()

    # ---------------------------------------------------------------- fonts

    def _init_fonts(self):
        families = set(tkfont.families(self.root))
        base = next((f for f in ("Segoe UI", "Segoe UI Variable Text", "SF Pro Text", "Helvetica Neue",
                                 "Ubuntu", "Cantarell", "Noto Sans", "DejaVu Sans", "Liberation Sans")
                     if f in families), "TkDefaultFont")
        strong_family = "Segoe UI Semibold" if "Segoe UI Semibold" in families else None
        mono = next((f for f in ("Cascadia Mono", "Consolas", "Menlo", "DejaVu Sans Mono", "Noto Sans Mono")
                     if f in families), "TkFixedFont")

        def strong(size):
            return (strong_family, size) if strong_family else (base, size, "bold")

        self.f = {
            "caption": (base, 8),
            "small": (base, 9),
            "body": (base, 10),
            "body_strong": strong(10),
            "label_caps": strong(8),
            "title": strong(12),
            "heading": strong(11),
            "mono": (mono, 9),
            "keycap": (base, 8),
            "icon": (base, 11),
        }

    # ---------------------------------------------------------------- registration

    def paint(self, widget, **roles):
        """Colour a widget from the palette and keep it in sync with theme changes.
        Example: theme.paint(label, bg="surface", fg="text_muted")"""
        self._apply(widget, roles)
        self._painted.append((widget, roles))
        return widget

    def _apply(self, widget, roles):
        widget.configure(**{option: self.c[key] for option, key in roles.items()})

    def listen(self, callback, owner=None):
        """Call callback() after every theme change (dropped automatically when owner is destroyed)."""
        self._listeners.append((owner, callback))

    def set(self, name):
        if name not in PALETTES or name == self.name:
            return False
        self.name = name
        self.c = dict(PALETTES[name])
        alive = []
        for widget, roles in self._painted:
            try:
                if widget.winfo_exists():
                    self._apply(widget, roles)
                    alive.append((widget, roles))
            except tk.TclError:
                pass
        self._painted = alive
        self._configure_ttk()
        listeners = []
        for owner, callback in self._listeners:
            try:
                if owner is not None and not owner.winfo_exists():
                    continue
                callback()
                listeners.append((owner, callback))
            except tk.TclError:
                pass
        self._listeners = listeners
        return True

    @property
    def is_dark(self):
        return self.name == "dark"

    # ---------------------------------------------------------------- ttk widgets

    def _configure_ttk(self):
        c, style = self.c, self.style
        for orient in ("Vertical", "Horizontal"):
            name = f"IOT.{orient}.TScrollbar"
            style.configure(name, gripcount=0, relief=tk.FLAT, borderwidth=0, arrowsize=12,
                            background=c["button"], troughcolor=c["bg"], bordercolor=c["bg"],
                            lightcolor=c["button"], darkcolor=c["button"], arrowcolor=c["text_muted"])
            style.map(name,
                      background=[("pressed", c["button_pressed"]), ("active", c["button_hover"])],
                      lightcolor=[("pressed", c["button_pressed"]), ("active", c["button_hover"])],
                      darkcolor=[("pressed", c["button_pressed"]), ("active", c["button_hover"])],
                      arrowcolor=[("disabled", c["text_faint"])])
        style.configure("IOT.Horizontal.TProgressbar", troughcolor=c["surface_alt"], background=c["accent"],
                        bordercolor=c["border"], lightcolor=c["accent"], darkcolor=c["accent"], thickness=8)


# ==================================================================== drawing helpers

def round_rect(canvas, x1, y1, x2, y2, r, **kwargs):
    """Rounded rectangle on a Canvas (a smoothed polygon)."""
    r = max(0, min(r, (x2 - x1) / 2, (y2 - y1) / 2))
    points = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
              x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
    return canvas.create_polygon(points, smooth=True, splinesteps=12, **kwargs)


def draw_half_window_tab(canvas, c, width, height, halved=False, hover=False):
    """The half-window toggle: a tab rounded on its right with a chevron pointing where the window will go."""
    canvas.delete("all")
    round_rect(canvas, -10, 0.5, width - 0.5, height - 0.5, min(8, width / 2),
               fill=c["button_hover"] if hover else c["button"], outline=c["border"])
    mid, reach = height / 2, min(7, height * 0.22)
    left, right = width * 0.33, width * 0.61
    x1, x2 = (right, left) if halved else (left, right)
    canvas.create_line(x1, mid - reach, x2, mid, x1, mid + reach, fill=c["accent"] if hover else c["text_muted"],
                       width=2, capstyle=tk.ROUND, joinstyle=tk.ROUND)


# ==================================================================== widgets

class Button(tk.Frame):
    """
    Flat button. variant: 'primary' | 'secondary' | 'ghost' | 'danger'.
    hint: optional keyboard shortcut shown as a small key-cap after the text.
    surface: palette key of whatever the button sits on (used by 'ghost').
    static: looks the same but isn't clickable (no hover, press or hand cursor), for showing a shortcut.
    """
    VARIANTS = {
        "primary": ("accent", "accent_hover", "accent_hover", "accent_text"),
        "secondary": ("button", "button_hover", "button_pressed", "text"),
        "ghost": (None, "button_hover", "button_pressed", "text"),
        "danger": ("danger", "danger_hover", "danger_hover", "accent_text"),
    }

    def __init__(self, parent, theme, text, command=None, variant="secondary", hint=None,
                 surface="surface", padx=12, pady=5, font=None, width=None, static=False):
        super().__init__(parent, bd=0, highlightthickness=0, cursor="hand2")
        self.static = static
        self.theme = theme
        self.command = command
        self.variant = variant
        self.surface = surface
        self.enabled = True
        self._hover = False
        self._pressed = False
        self.label = tk.Label(self, text=text, bd=0, padx=0, pady=pady, font=font or theme.f["body"], width=width or 0)
        self.hint = None
        if hint:
            self.label.pack(side=tk.LEFT, padx=(padx, 7))
            self.hint = tk.Label(self, text=hint, bd=0, padx=5, pady=0, font=theme.f["keycap"])
            self.hint.pack(side=tk.LEFT, padx=(0, max(4, padx - 3)))
        else:
            self.label.pack(side=tk.LEFT, padx=padx)
        for widget in [] if static else self._parts():
            widget.bind("<Enter>", self._on_enter)
            widget.bind("<Leave>", self._on_leave)
            widget.bind("<ButtonPress-1>", self._on_press)
            widget.bind("<ButtonRelease-1>", self._on_release)
        theme.listen(self.refresh, owner=self)
        self.refresh()

    def _parts(self):
        return [w for w in (self, self.label, self.hint) if w is not None]

    def refresh(self):
        c = self.theme.c
        base, hover, pressed, fg = self.VARIANTS[self.variant]
        if not self.enabled:
            bg = c[base] if base else c[self.surface]
            fg_color = c["text_faint"]
        else:
            if self._pressed:
                bg = c[pressed]
            elif self._hover:
                bg = c[hover]
            else:
                bg = c[base] if base else c[self.surface]
            fg_color = c[fg]
        for widget in (self, self.label):
            widget.configure(bg=bg)
        self.label.configure(fg=fg_color)
        if self.hint is not None:
            on_accent = self.variant in ("primary", "danger")
            self.hint.configure(bg=c["accent_hover"] if on_accent else c["keycap"],
                                fg=c["accent_text"] if on_accent else c["text_muted"])
        cursor = "hand2" if self.enabled and not self.static else "arrow"
        for widget in self._parts():
            widget.configure(cursor=cursor)

    def _on_enter(self, _event=None):
        self._hover = True
        self.refresh()

    def _on_leave(self, _event=None):
        # Leave fires when moving between the button's own parts; check the pointer really left
        self.after(10, self._check_leave)

    def _check_leave(self):
        try:
            x, y = self.winfo_pointerxy()
            widget = self.winfo_containing(x, y)
        except (KeyError, tk.TclError):
            widget = None
        if widget not in self._parts():
            self._hover = False
            self._pressed = False
            self.refresh()

    def _on_press(self, _event=None):
        if self.enabled:
            self._pressed = True
            self.refresh()

    def _on_release(self, _event=None):
        was_pressed = self._pressed
        self._pressed = False
        self.refresh()
        if was_pressed and self.enabled and self._hover and self.command:
            self.command()

    def set_enabled(self, enabled):
        self.enabled = bool(enabled)
        self.refresh()

    def set_text(self, text):
        self.label.configure(text=text)

    def invoke(self):
        if self.enabled and self.command:
            self.command()


class CheckBox(tk.Frame):
    """Modern checkbox bound to a BooleanVar."""
    SIZE = 16

    def __init__(self, parent, theme, text, variable, surface="surface"):
        super().__init__(parent, bd=0, highlightthickness=0, cursor="hand2")
        self.theme, self.var, self.surface = theme, variable, surface
        self._hover = False
        self.box = tk.Canvas(self, width=self.SIZE, height=self.SIZE, bd=0, highlightthickness=0)
        self.box.pack(side=tk.LEFT, pady=2)
        self.label = tk.Label(self, text=text, bd=0, padx=7, font=theme.f["body"])
        self.label.pack(side=tk.LEFT)
        for widget in (self, self.box, self.label):
            widget.bind("<Button-1>", self._toggle)
            widget.bind("<Enter>", lambda e: self._set_hover(True))
            widget.bind("<Leave>", lambda e: self._set_hover(False))
        self._trace = variable.trace_add("write", lambda *a: self.redraw())
        self.bind("<Destroy>", self._on_destroy, add="+")
        theme.listen(self.redraw, owner=self)
        self.redraw()

    def _on_destroy(self, event):
        if event.widget is self:
            try:
                self.var.trace_remove("write", self._trace)
            except (tk.TclError, ValueError):
                pass

    def _toggle(self, _event=None):
        self.var.set(not self.var.get())

    def _set_hover(self, value):
        self._hover = value
        self.redraw()

    def redraw(self):
        c, s = self.theme.c, self.SIZE
        bg = c[self.surface]
        for widget in (self, self.box, self.label):
            widget.configure(bg=bg)
        self.label.configure(fg=c["text"])
        self.box.delete("all")
        if self.var.get():
            round_rect(self.box, 0.5, 0.5, s - 0.5, s - 0.5, 4, fill=c["accent_hover"] if self._hover else c["accent"], outline="")
            self.box.create_line(4, 8.5, 7, 11.5, 12.5, 5, fill=c["accent_text"], width=2, capstyle=tk.ROUND, joinstyle=tk.ROUND)
        else:
            round_rect(self.box, 0.5, 0.5, s - 0.5, s - 0.5, 4, fill=c["surface_alt"],
                       outline=c["accent"] if self._hover else c["text_faint"], width=1.2)


class ThemeToggle(tk.Canvas):
    """Two-segment switch: sun (light) on the left, crescent moon (dark) on the right."""
    W, H = 74, 30

    def __init__(self, parent, theme, on_change, surface="surface"):
        super().__init__(parent, width=self.W, height=self.H, bd=0, highlightthickness=0, cursor="hand2")
        self.theme, self.on_change, self.surface = theme, on_change, surface
        self._hover_side = None
        self.bind("<Button-1>", self._click)
        self.bind("<Motion>", self._motion)
        self.bind("<Leave>", lambda e: self._set_hover(None))
        theme.listen(self.redraw, owner=self)
        self.redraw()

    def _side_at(self, x):
        return "light" if x < self.W / 2 else "dark"

    def _motion(self, event):
        self._set_hover(self._side_at(event.x))

    def _set_hover(self, side):
        if side != self._hover_side:
            self._hover_side = side
            self.redraw()

    def _click(self, event):
        side = self._side_at(event.x)
        if side != self.theme.name:
            self.on_change(side)

    def redraw(self):
        c = self.theme.c
        w, h = self.W, self.H
        self.configure(bg=c[self.surface])
        self.delete("all")
        round_rect(self, 0.5, 0.5, w - 0.5, h - 0.5, h / 2, fill=c["button"], outline=c["border"])
        half = w / 2
        active = self.theme.name
        # Highlight pill behind the active side, faint pill behind a hovered inactive side
        for side, x1 in (("light", 3), ("dark", half)):
            x2 = x1 + half - 3
            if side == active:
                round_rect(self, x1, 3, x2, h - 3, (h - 6) / 2, fill=c["accent"], outline="")
            elif side == self._hover_side:
                round_rect(self, x1, 3, x2, h - 3, (h - 6) / 2, fill=c["button_hover"], outline="")
        # Sun
        sun_color = c["accent_text"] if active == "light" else c["text_muted"]
        cx, cy = half / 2 + 1.5, h / 2
        self.create_oval(cx - 3.6, cy - 3.6, cx + 3.6, cy + 3.6, fill=sun_color, outline="")
        import math
        for k in range(8):
            angle = k * math.pi / 4
            r1, r2 = 5.6, 8.0
            self.create_line(cx + r1 * math.cos(angle), cy + r1 * math.sin(angle),
                             cx + r2 * math.cos(angle), cy + r2 * math.sin(angle),
                             fill=sun_color, width=1.6, capstyle=tk.ROUND)
        # Crescent moon: a disc with an offset disc in the background colour cut out of it
        moon_color = c["accent_text"] if active == "dark" else c["text_muted"]
        behind = c["accent"] if active == "dark" else (c["button_hover"] if self._hover_side == "dark" else c["button"])
        mx, my, r = half + half / 2 - 1.5, h / 2, 6.6
        self.create_oval(mx - r, my - r, mx + r, my + r, fill=moon_color, outline="")
        self.create_oval(mx - r + 4.6, my - r - 2.4, mx + r + 4.6, my + r - 2.4, fill=behind, outline="")


class Slider(tk.Canvas):
    """Slim slider with a filled track and round knob. set() notifies `command`, like tk.Scale.
    step: when given, clicking the slider (or Tab) gives it keyboard focus and the arrow keys move it
    one step at a time, landing on whole steps."""

    def __init__(self, parent, theme, from_, to, value, command=None, length=150, surface="surface", step=None):
        super().__init__(parent, width=length, height=22, bd=0, highlightthickness=0, cursor="hand2",
                         takefocus=1 if step else 0)
        self.theme, self.from_, self.to, self.command, self.surface = theme, from_, to, command, surface
        self.length = length
        self.value = value
        self.step = step
        self._active = False
        self._focused = False
        if step:
            self.bind("<Button-1>", lambda e: self.focus_set(), add="+")
            for key, direction in (("Left", -1), ("Down", -1), ("Right", 1), ("Up", 1)):
                self.bind(f"<{key}>", lambda e, d=direction: self._nudge(d))
            self.bind("<FocusIn>", lambda e: self._set_focused(True))
            self.bind("<FocusOut>", lambda e: self._set_focused(False))
        self.bind("<Button-1>", self._drag, add="+")
        self.bind("<B1-Motion>", self._drag)
        self.bind("<ButtonRelease-1>", lambda e: self._set_active(False))
        self.bind("<Enter>", lambda e: self._set_active(True))
        self.bind("<Leave>", lambda e: self._set_active(False))
        theme.listen(self.redraw, owner=self)
        self.redraw()

    def _set_active(self, value):
        self._active = value
        self.redraw()

    def _set_focused(self, value):
        self._focused = value
        self.redraw()

    def _nudge(self, direction):
        """Arrow keys: one step left or right, snapping to whole steps (e.g. 133 -> 140 or 130)."""
        snapped = round(self.value / self.step) * self.step
        target = snapped + direction * self.step if snapped == self.value else (
            snapped if (snapped - self.value) * direction > 0 else snapped + direction * self.step)
        self.set(target)
        return "break"

    def _x_for(self, value):
        pad = 8
        return pad + (value - self.from_) / (self.to - self.from_) * (self.length - 2 * pad)

    def _drag(self, event):
        pad = 8
        fraction = min(1.0, max(0.0, (event.x - pad) / (self.length - 2 * pad)))
        self.set(round(self.from_ + fraction * (self.to - self.from_)))

    def get(self):
        return self.value

    def set(self, value):
        self.value = int(min(self.to, max(self.from_, value)))
        self.redraw()
        if self.command:
            self.command(self.value)  # like tk.Scale; the receiver ignores unchanged values

    def redraw(self):
        c = self.theme.c
        self.configure(bg=c[self.surface])
        self.delete("all")
        y = 11
        x = self._x_for(self.value)
        self.create_line(8, y, self.length - 8, y, fill=c["border"], width=4, capstyle=tk.ROUND)
        self.create_line(8, y, x, y, fill=c["accent"], width=4, capstyle=tk.ROUND)
        r = 7 if self._active else 6
        if self._focused:  # has keyboard focus: the arrow keys move it
            self.create_oval(x - r - 3, y - r - 3, x + r + 3, y + r + 3, fill=c["accent_soft"], outline="")
        self.create_oval(x - r, y - r, x + r, y + r, fill=c["accent_text"] if not self.theme.is_dark else "#ffffff",
                         outline=c["accent"], width=2)


class Logo(tk.Canvas):
    """Small app mark: an accent tile with a 2x2 image grid."""

    def __init__(self, parent, theme, size=28, surface="surface"):
        super().__init__(parent, width=size, height=size, bd=0, highlightthickness=0)
        self.theme, self.size, self.surface = theme, size, surface
        theme.listen(self.redraw, owner=self)
        self.redraw()

    def redraw(self):
        c, s = self.theme.c, self.size
        self.configure(bg=c[self.surface])
        self.delete("all")
        round_rect(self, 0, 0, s, s, 7, fill=c["accent"], outline="")
        gap, pad = 3, 7
        cell = (s - 2 * pad - gap) / 2
        for row in range(2):
            for col in range(2):
                x = pad + col * (cell + gap)
                y = pad + row * (cell + gap)
                round_rect(self, x, y, x + cell, y + cell, 1.5, fill=c["accent_text"], outline="")


# ==================================================================== layout helpers

class Tooltip:
    """Small themed hint that appears after the pointer rests on `widgets` for a moment."""
    DELAY_MS = 400

    def __init__(self, theme, text, *widgets, wraplength=320):
        self.theme, self.text, self.wraplength = theme, text, wraplength
        self.widgets = widgets
        self._job = None
        self._tip = None
        for widget in widgets:
            widget.bind("<Enter>", self._schedule, add="+")
            widget.bind("<Leave>", self._hide, add="+")
            widget.bind("<ButtonPress>", self._hide, add="+")
            widget.bind("<Destroy>", self._hide, add="+")

    def _schedule(self, event):
        self._cancel()
        self._job = event.widget.after(self.DELAY_MS, lambda: self._show(event.widget))

    def _cancel(self):
        if self._job is not None:
            try:
                self.widgets[0].after_cancel(self._job)
            except tk.TclError:
                pass
            self._job = None

    def _show(self, widget):
        self._job = None
        if self._tip is not None:
            return
        c = self.theme.c
        tip = tk.Toplevel(widget)
        tip.wm_overrideredirect(True)
        tip.attributes("-topmost", True)  # above modal dialogs
        tip.configure(bg=c["surface_alt"], highlightthickness=1, highlightbackground=c["border"])
        tk.Label(tip, text=self.text, font=self.theme.f["small"], bg=c["surface_alt"], fg=c["text"], bd=0,
                 padx=10, pady=6, justify=tk.LEFT, wraplength=self.wraplength).pack()
        tip.wm_geometry(f"+{widget.winfo_rootx()}+{widget.winfo_rooty() + widget.winfo_height() + 6}")
        self._tip = tip

    def _hide(self, _event=None):
        self._cancel()
        if self._tip is not None:
            try:
                self._tip.destroy()
            except tk.TclError:
                pass
            self._tip = None


def caps_label(parent, theme, text, surface="surface"):
    return theme.paint(tk.Label(parent, text=text.upper(), font=theme.f["label_caps"], bd=0),
                       bg=surface, fg="text_faint")


def vertical_divider(parent, theme, height=26):
    line = tk.Frame(parent, width=1, height=height, bd=0)
    theme.paint(line, bg="divider")
    return line


def entry(parent, theme, variable, width=None, justify=tk.LEFT, font=None):
    widget = tk.Entry(parent, textvariable=variable, relief=tk.FLAT, bd=0, highlightthickness=1,
                      font=font or theme.f["body"], justify=justify)
    if width:
        widget.configure(width=width)
    theme.paint(widget, bg="surface_alt", fg="text", insertbackground="text",
                highlightbackground="border", highlightcolor="accent",
                selectbackground="accent", selectforeground="accent_text")
    return widget


def make_dialog(app, title, modal=True):
    """A themed, non-resizable dialog window centred over the main window."""
    theme = app.theme
    win = tk.Toplevel(app.root)
    win.withdraw()
    win.title(title)
    win.resizable(False, False)
    win.transient(app.root)
    theme.paint(win, bg="surface")
    win._iot_modal = modal
    return win


def show_dialog(app, win, min_width=380):
    """Size the dialog to its content, centre it over the main window and show it."""
    win.update_idletasks()
    width = max(min_width, win.winfo_reqwidth())
    height = win.winfo_reqheight()
    root = app.root
    try:
        x = root.winfo_rootx() + (root.winfo_width() - width) // 2
        y = root.winfo_rooty() + max(40, (root.winfo_height() - height) // 3)
    except tk.TclError:
        x = y = 100
    win.geometry(f"{width}x{height}+{max(0, x)}+{max(0, y)}")
    win.deiconify()
    app.apply_title_bar_theme(win)
    if getattr(win, "_iot_modal", True):
        try:
            win.grab_set()
        except tk.TclError:
            pass
    win.focus_set()


def dialog_header(parent, theme, title, subtitle=None):
    box = theme.paint(tk.Frame(parent, bd=0), bg="surface")
    box.pack(fill=tk.X, padx=22, pady=(18, 6))
    theme.paint(tk.Label(box, text=title, font=theme.f["title"], anchor="w", justify=tk.LEFT), bg="surface", fg="text").pack(fill=tk.X)
    if subtitle:
        theme.paint(tk.Label(box, text=subtitle, font=theme.f["small"], anchor="w", justify=tk.LEFT, wraplength=440),
                    bg="surface", fg="text_muted").pack(fill=tk.X, pady=(3, 0))
    return box


def dialog_buttons(parent, theme, buttons):
    """buttons: list of (text, command, variant). Laid out right-aligned, first item right-most."""
    bar = theme.paint(tk.Frame(parent, bd=0), bg="surface")
    bar.pack(fill=tk.X, side=tk.BOTTOM, padx=22, pady=(10, 18))
    made = []
    for text, command, variant in buttons:
        button = Button(bar, theme, text, command, variant=variant, padx=16, pady=6)
        button.pack(side=tk.RIGHT, padx=(8, 0))
        made.append(button)
    return made


def is_windows():
    return sys.platform.startswith("win")
