import iot_platform

SHORTCUTS = (
    ("Click, Click", "Select a range (first and last image) and move into a folder"),
    ("Click, Ctrl+Click", "Instantly rename the selected range, leave in place"),
    ("Click, Alt+Click", "Delete a range (no confirmation; Undo restores it)"),
    ("Hover + Delete", "Delete the image under the pointer (no confirmation)"),
    ("Hover + Hold Z", "Full-screen preview of the image under the pointer"),
    ("W / S", "Previous / Next folder"),
    ("Q", "Delete current folder when IOT closes (Q again or Undo keeps it)"),
    ("R", "Rename the current folder"),
    ("F", "Find duplicates of the base image (98%+ match)"),
    ("D / U", "Downscale / Upscale the folder"),
    ("Folder list: Hover + D / U", "Downscale / Upscale a folder and all its sub-folders"),
    ("Folder list: Hover + Ctrl", "Rename a folder's and its sub-folders' images after their folder"),
    ("G", "Group mode: click any images to select them (G or Esc to leave)"),
    ("Enter", "In group mode: copy selected images into a new folder (also general confirmation)"),
    ("Ctrl+Z", "Undo the most recent change"),
    ("F5", "Refresh: pick up changes made outside I.O.T"),
)

FLAGS = (  # (setting key, symbol, explanation); symbols differ on Linux, see iot_platform
    ("target", iot_platform.FLAG_TARGET, "Resolution check: chosen base image doesn't match typical resolution"),
    ("size", iot_platform.FLAG_SIZE, "Size check: folder is unusually large (possible duplicates)"),
    ("modified", iot_platform.FLAG_MODIFIED, "Modified: you've changed this folder in this session"),
)


def show_controls_settings_window(app_instance):
    """Shortcuts reference and processing settings, styled to the active theme."""
    import tkinter as tk          # Tk version only; the Qt Settings window is in iot_qt_dialogs
    import iot_theme
    app = app_instance
    t = app.theme
    win = iot_theme.make_dialog(app, "Settings")
    iot_theme.dialog_header(win, t, "Settings", "Shortcuts, folder flags and processing options. (Hover for Details)")

    body = t.paint(tk.Frame(win, bd=0), bg="surface")
    body.pack(fill=tk.BOTH, expand=True, padx=22, pady=(8, 0))
    left = t.paint(tk.Frame(body, bd=0), bg="surface")
    left.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 44))
    right = t.paint(tk.Frame(body, bd=0), bg="surface")
    right.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 8))

    # --- Shortcuts ---
    iot_theme.caps_label(left, t, "Keyboard & mouse").pack(anchor="w", pady=(0, 6))
    table = t.paint(tk.Frame(left, bd=0), bg="surface")
    table.pack(fill=tk.X)
    list_prefix = "Folder list: "  # shown under their own heading, so the key column stays narrow
    main_shortcuts = [(keys, text) for keys, text in SHORTCUTS if not keys.startswith(list_prefix)]
    list_shortcuts = [(keys[len(list_prefix):], text) for keys, text in SHORTCUTS if keys.startswith(list_prefix)]

    def add_shortcut(grid, row, keys, text):
        keys, text = iot_platform.keys(keys), iot_platform.keys(text)  # e.g. Cmd / Option on macOS
        key_label = tk.Label(grid, text=keys, font=t.f["keycap"], bd=0, padx=6, pady=1)
        t.paint(key_label, bg="keycap", fg="text")
        key_label.grid(row=row, column=0, sticky="w", pady=3)
        t.paint(tk.Label(grid, text=text, font=t.f["small"], bd=0, anchor="w"), bg="surface", fg="text_muted").grid(
            row=row, column=1, sticky="w", padx=(12, 0), pady=3)

    for row, (keys, text) in enumerate(main_shortcuts):
        add_shortcut(table, row, keys, text)

    # The half-window tab on the left edge of the grid, drawn the same way as the real one
    tab = tk.Canvas(table, width=14, height=30, bd=0, highlightthickness=0)

    def draw_tab():
        tab.configure(bg=t.c["surface"])
        iot_theme.draw_half_window_tab(tab, t.c, 14, 30)
    t.listen(draw_tab, owner=tab)
    draw_tab()
    tab.grid(row=len(main_shortcuts), column=0, sticky="w", pady=3)
    t.paint(tk.Label(table, text="Press to half width, press again to maximize (for drag & drop)", font=t.f["small"],
                     bd=0, anchor="w"), bg="surface", fg="text_muted").grid(row=len(main_shortcuts), column=1, sticky="w", padx=(12, 0), pady=3)

    if list_shortcuts:
        iot_theme.caps_label(left, t, "In the folder list").pack(anchor="w", pady=(14, 6))
        list_table = t.paint(tk.Frame(left, bd=0), bg="surface")
        list_table.pack(fill=tk.X)
        for row, (keys, text) in enumerate(list_shortcuts):
            add_shortcut(list_table, row, keys, text)
        # same key-column width as the table above, so both lists line up
        table.update_idletasks()
        list_table.grid_columnconfigure(0, minsize=table.grid_bbox(0, 0)[2] if table.grid_bbox(0, 0) else 0)

    # Folder flags, each with a tick box to show or hide it (private copies, so Cancel really discards)
    iot_theme.caps_label(left, t, "Folder flags").pack(anchor="w", pady=(16, 6))
    flag_vars = {}
    for key, symbol, text in FLAGS:
        line = t.paint(tk.Frame(left, bd=0), bg="surface")
        line.pack(fill=tk.X, pady=2)
        flag_vars[key] = tk.BooleanVar(value=app.flags_enabled.get(key, True))
        iot_theme.CheckBox(line, t, "", flag_vars[key]).pack(side=tk.LEFT)
        t.paint(tk.Label(line, text=symbol, font=t.f["body"], bd=0, width=3), bg="surface", fg="text").pack(side=tk.LEFT)
        t.paint(tk.Label(line, text=text, font=t.f["small"], bd=0, anchor="w"), bg="surface", fg="text_muted").pack(side=tk.LEFT)

    # --- Processing settings ---
    iot_theme.caps_label(right, t, "Processing").pack(anchor="w", pady=(0, 6))
    fields = t.paint(tk.Frame(right, bd=0), bg="surface")
    fields.pack(fill=tk.X)

    def add_field(row, label, value, suffix, hint):
        label_widget = t.paint(tk.Label(fields, text=label, font=t.f["small"], bd=0, anchor="w"), bg="surface", fg="text")
        label_widget.grid(row=row, column=0, sticky="w", pady=6)
        iot_theme.Tooltip(t, hint, label_widget)
        var = tk.StringVar(value=str(value))
        iot_theme.entry(fields, t, var, width=8, justify=tk.CENTER).grid(row=row, column=1, padx=(14, 6), pady=6, ipady=4)
        t.paint(tk.Label(fields, text=suffix, font=t.f["small"], bd=0, anchor="w"), bg="surface", fg="text_muted").grid(
            row=row, column=2, sticky="w")
        return var

    # A private copy of each value, so Cancel really discards
    base_var = add_field(0, "Base image", app.base_image, "which file in folder?",
                         "Intended to ensure calculations aren't based on thumbnails/covers etc")
    # Shown as a percentage of the average (130 = 30% above it); stored as the amount above (30)
    try:
        threshold_shown = f"{float(app.anomaly_threshold_var.get()) + 100:g}"
    except ValueError:
        threshold_shown = "130"
    anomaly_var = add_field(1, f"{iot_platform.FLAG_SIZE} Unusual size threshold", threshold_shown, "% of average",
                            "(Optional) Intended to help auto-detect potential issues")
    res_rule_var = add_field(2, f"{iot_platform.FLAG_TARGET} Target resolution", app.target_resolution, "px",
                             "(Optional) To check if folders match your typical preferred resolution")
    down_var = add_field(3, "Downscale short edge to", app.down_target, "px",
                         "Your preferred resolution when Downscaling (Length or Width must be at least)")
    up_var = add_field(4, "Upscale short edge to", app.up_target, "px",
                       "Your preferred resolution when Upscaling (Length or Width must be at least)")
    quality_var = add_field(5, "JPEG quality", app.jpg_quality, "%",
                            "Significant file size decrease with no noticeable loss of quality")
    exif_var = tk.BooleanVar(value=app.strip_exif)
    exif_box = iot_theme.CheckBox(right, t, "Strip EXIF data from resized images", exif_var)
    exif_box.pack(anchor="w", pady=(4, 0))
    iot_theme.Tooltip(t, "Removes information like Camera model & GPS co-ords, leave unchecked if unsure",
                      exif_box.label)

    # --- Display ---
    iot_theme.caps_label(right, t, "Display").pack(anchor="w", pady=(16, 6))
    frost_var = tk.BooleanVar(value=app.frost_preview)
    frost_box = iot_theme.CheckBox(right, t, "Frosted screen behind Zoom preview", frost_var)
    frost_box.pack(anchor="w")
    sizes_var = tk.BooleanVar(value=app.tree_show_sizes)
    sizes_box = iot_theme.CheckBox(right, t, "Folder sizes in the folder list", sizes_var)
    sizes_box.pack(anchor="w", pady=(6, 0))
    speed_row = t.paint(tk.Frame(right, bd=0), bg="surface")
    speed_row.pack(anchor="w", pady=(8, 0))
    speed_label = t.paint(tk.Label(speed_row, text="Scroll speed", font=t.f["body"], bd=0), bg="surface", fg="text")
    speed_label.pack(side=tk.LEFT, padx=(0, 10))
    speed_value = t.paint(tk.Label(speed_row, text=f"{app.scroll_speed}%", font=t.f["small"], bd=0, width=5, anchor="w"),
                          bg="surface", fg="text_muted")
    speed_var = [app.scroll_speed]

    def on_speed(value):
        value = int(round(value / 10.0) * 10)  # 10% steps
        speed_var[0] = value
        speed_value.config(text=f"{value}%")
    speed_slider = iot_theme.Slider(speed_row, t, 50, 400, app.scroll_speed, command=on_speed, length=150, step=10)
    speed_slider.pack(side=tk.LEFT)
    speed_value.pack(side=tk.LEFT, padx=(8, 0))
    iot_theme.Tooltip(t, "How far the picture grid moves for each notch of the mouse wheel (100% is the normal speed). "
                         "Click the slider, then use the arrow keys to adjust it in 10% steps.", speed_label, speed_slider)
    iot_theme.Tooltip(t, "Shows how much space each folder uses in the folder drop-down list, "
                         "with a \u03a3 total for folders that have sub-folders.", sizes_box.label)
    iot_theme.Tooltip(t, "Blurs the rest of the screen while Hover + Hold Z shows an image. "
                         "Untick on slower computers if the preview feels sluggish.", frost_box.label)

    t.paint(tk.Label(right, text=f"Settings file:\n{app.settings_path}",
                     font=t.f["caption"], bd=0, anchor="w", justify=tk.LEFT, wraplength=300),
            bg="surface", fg="text_faint").pack(anchor="w", pady=(14, 0))

    error_label = t.paint(tk.Label(win, text="", font=t.f["small"], bd=0, anchor="w"), bg="surface", fg="danger")

    def save_and_recalculate_profiles(event=None):
        try:
            anomaly_value = float(anomaly_var.get()) - 100  # back to "% above average"
            down_value = int(down_var.get())
            up_value = int(up_var.get())
            quality_value = max(1, min(100, int(quality_var.get())))
            resolution_value = int(res_rule_var.get())
            base_value = int(base_var.get())
            if min(down_value, up_value, resolution_value, base_value) <= 0:
                raise ValueError
        except ValueError:
            error_label.config(text="Every field needs a positive whole number.")
            error_label.pack(fill=tk.X, padx=22, pady=(8, 0), before=buttons[0].master)
            return
        if anomaly_value < 0:
            error_label.config(text="Unusual size threshold needs to be 100 or more (100 = the average folder size).")
            error_label.pack(fill=tk.X, padx=22, pady=(8, 0), before=buttons[0].master)
            return

        app.anomaly_threshold_var.set(f"{anomaly_value:g}")
        app.down_target = down_value
        app.up_target = up_value
        app.jpg_quality = quality_value
        app.target_resolution = resolution_value
        app.base_image = base_value
        for key, var in flag_vars.items():
            app.flags_enabled[key] = bool(var.get())
        app.frost_preview = bool(frost_var.get())
        app.tree_show_sizes = bool(sizes_var.get())
        app.strip_exif = bool(exif_var.get())
        app.scroll_speed = speed_var[0]
        app.save_settings()

        win.destroy()
        app.handle_anomaly_recalculation_event()

    buttons = iot_theme.dialog_buttons(win, t, [("Save", save_and_recalculate_profiles, "primary"),
                                                ("Cancel", win.destroy, "secondary")])
    win.bind("<Return>", save_and_recalculate_profiles)
    win.bind("<Escape>", lambda e: win.destroy())
    iot_theme.show_dialog(app, win, min_width=860)

