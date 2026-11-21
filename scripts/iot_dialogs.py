import iot_platform

SHORTCUTS = (
    ("Click, Click", "Select a range (first and last image) and move into a folder"),
    ("Click, Ctrl+Click", "Instantly rename the selected range, leave in place"),
    ("Click, Alt+Click", "Delete a range (Undo restores it; Settings > Confirmations can make it ask first)"),
    ("Hover + Delete", "Delete the image under the pointer (asks first only if set in Confirmations)"),
    ("Hover + Z", "Full-screen preview (Z or Esc closes it, ← / → show the previous / next image)"),
    ("Hover + [ / ]", "Rotate the image left / right (in group mode: every picked image)"),
    ("Double-click", "Open the image in your default image viewer"),
    ("W / S", "Previous / Next folder"),
    ("Q", "Delete current folder when IOT closes (Q again or Undo keeps it)"),
    ("R", "Rename the current folder"),
    ("Click, Hold R + Click", "Rename a range in place (in group mode: the picked images + the clicked one)"),
    ("C", "Compress the current folder into a zip (Settings: where it goes)"),
    ("Click, Hold C + Click", "Compress a range into a zip (in group mode: the picked images + the clicked one)"),
    ("F", "Find duplicates in the folder (98%+ alike); greyed out where none were found"),
    ("D / U", "Downscale / Upscale the folder"),
    ("Folder list: Hover + D / U", "Downscale / Upscale a folder and all its sub-folders"),
    ("Folder list: Hover + Ctrl", "Rename a folder's and its sub-folders' images after their folder"),
    ("G", "Group mode: click any images to select them (G or Esc to leave)"),
    ("Enter", "In group mode: copy selected images into a new folder (also general confirmation)"),
    ("Ctrl+Z", "Undo the most recent change"),
    ("F5", "Refresh: pick up changes made outside I.O.T"),
)

# Actions that can ask "are you sure?" first (Settings > Confirmations). (key, action, what asking means, default):
# the defaults are I.O.T's own workflow - quick actions that Undo can reverse don't ask, bigger ones do.
CONFIRMATIONS = (
    ("delete_image", "Hover + Delete", "Delete the picture under the pointer", False),
    ("delete_range", "Click, Alt+Click", "Delete a range of pictures", False),
    ("rename_range", "Click, Ctrl+Click", "Rename a range in place, using the suggested name", False),
    ("delete_folder", "Q", "Mark the current folder for deletion when I.O.T closes", True),
    ("resize", "D / U", "Downscale / Upscale (also Hover + D / U in the folder list)", True),
    ("rename_branch", "Hover + Ctrl", "Folder list: rename a folder's and its sub-folders' images", True),
    ("compress", "C", "Compress the folder (or a selection) into a zip", True),
    ("undo", "Ctrl+Z", "Undo the most recent change (the Undo window's Revert buttons never ask)", False),
)

FLAGS = (  # (setting key, symbol, explanation); symbols differ on Linux, see iot_platform
    ("target", f"{iot_platform.FLAG_DOWN} {iot_platform.FLAG_UP}",
     "Needs resizing: the first (after any ignored) picture's short edge is above your Downscale size or below "
     "your Upscale"),
    ("size", iot_platform.FLAG_SIZE, "Size check: folder is unusually large (possible duplicates)"),
    ("modified", iot_platform.FLAG_MODIFIED, "Modified: you've changed this folder in this session"),
)
