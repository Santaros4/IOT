"""
Undo history, the per-session recycle folder, and the permanent change log.

* Deleted files (and the originals of resized images) are moved into a hidden recycle folder inside
  the parent hub, so they stay on the same drive and moving them is instant. The folder is emptied
  and removed when I.O.T closes.
* The last 20 alterations are kept as undo records. Each record is a list of simple steps
  (file moved, file created, folder created, folder renamed) that can be played backwards.
* Every alteration and every undo is appended to a plain-text log with the folder it happened in.
"""
import os
import sys
import time
import shutil
import ctypes
import itertools
import threading
from datetime import datetime

RECYCLE_DIR_NAME = ".iot_recycle"
HISTORY_LIMIT = 20
LOG_ROTATE_BYTES = 5_000_000


def hide_path(path):
    """Gives a folder the Windows 'hidden' attribute (no-op elsewhere; dot-names are hidden there)."""
    if sys.platform.startswith("win"):
        try:
            ctypes.windll.kernel32.SetFileAttributesW(str(path), 0x02)
        except Exception:
            pass


def move_file(src, dst):
    """Moves a file without ever overwriting. Same drive = instant rename; other drive = copy."""
    if os.path.lexists(dst):
        raise FileExistsError(f"'{os.path.basename(dst)}' already exists")
    try:
        os.rename(src, dst)
    except OSError:
        if os.path.exists(src) and not os.path.lexists(dst):
            shutil.move(src, dst)
        else:
            raise


def _process_running(pid):
    """True if a process with this id is still running (an I.O.T window that owns a recycle folder)."""
    if pid == os.getpid():
        return True
    if sys.platform.startswith("win"):
        try:
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if not handle:
                return False
            try:
                code = ctypes.c_ulong()
                return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259  # STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return True  # can't tell: assume it's alive, so nothing it still needs gets deleted
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except OSError:
        return True


def unique_path(folder, filename, prefix="restored_"):
    base, ext = os.path.splitext(filename)
    candidate = os.path.join(folder, f"{prefix}{base}{ext}")
    counter = 2
    while os.path.lexists(candidate):
        candidate = os.path.join(folder, f"{prefix}{counter}_{base}{ext}")
        counter += 1
    return candidate


class Session:
    """Recycle folder for this run of I.O.T."""

    def __init__(self, hub):
        self.root_dir = os.path.join(hub, RECYCLE_DIR_NAME)
        self.dir = os.path.join(self.root_dir, datetime.now().strftime("%Y-%m-%d_%H%M%S") + f"_{os.getpid()}")
        self._counter = itertools.count(1)
        self._lock = threading.Lock()
        self._created = False

    def stash_path(self, original_name):
        """A unique path inside the recycle folder to move a file to. Thread safe."""
        with self._lock:
            number = next(self._counter)
            if not self._created or not os.path.isdir(self.dir):  # (re)create it if it has gone missing
                os.makedirs(self.dir, exist_ok=True)
                hide_path(self.root_dir)
                self._created = True
        return os.path.join(self.dir, f"{number:06d}__{original_name}")

    def leftover_sessions(self):
        """Recycle folders from earlier runs that didn't close normally (e.g. a crash). A folder whose
        I.O.T window is still running (another window open on the same hub) is in use, not left over."""
        try:
            names = os.listdir(self.root_dir)
        except OSError:
            return []
        leftovers = []
        for name in names:
            path = os.path.join(self.root_dir, name)
            if path == self.dir:
                continue
            owner = name.rsplit("_", 1)[-1]
            if owner.isdigit() and _process_running(int(owner)):
                continue
            leftovers.append(path)
        return leftovers

    def cleanup(self, include_leftovers=False):
        targets = [self.dir] + (self.leftover_sessions() if include_leftovers else [])
        for path in targets:
            shutil.rmtree(path, ignore_errors=True)
        try:
            os.rmdir(self.root_dir)  # only succeeds when nothing else is left in it
        except OSError:
            pass


class ChangeLog:
    """Append-only, human-readable record of every change made to files and folders."""

    def __init__(self, path):
        self.path = path

    def write(self, action, folder, lines=(), target=None):
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        header = f"{stamp}  {action:<15} {folder}"
        if target:
            header += f"  ->  {target}"
        text = header + "\n" + "".join(f"        {line}\n" for line in lines) + "\n"
        try:
            if os.path.exists(self.path) and os.path.getsize(self.path) > LOG_ROTATE_BYTES:
                os.replace(self.path, self.path + ".old")
            with open(self.path, "a", encoding="utf-8") as handle:
                handle.write(text)
        except OSError:
            pass


def describe_range(names, noun="file"):
    """'photo 3.jpg ... photo 7.jpg  (5 files)' - keeps the change log readable for big batches."""
    names = list(names)
    if not names:
        return f"0 {noun}s"
    if len(names) == 1:
        return names[0]
    return f"{names[0]}  ...  {names[-1]}  ({len(names)} {noun}s)"


class Record:
    _ids = itertools.count(1)

    def __init__(self, kind, title, folder, steps, target=None):
        self.id = next(Record._ids)
        self.time = time.time()
        self.kind = kind          # e.g. "Delete", "Move", "Rename", "Resize", "Folder rename"
        self.title = title        # one-line description shown in the Undo window
        self.folder = folder
        self.target = target
        self.steps = steps        # [("move", src, dst) | ("created", path) | ("mkdir", path) | ("rename_dir", src, dst)]
        self.reverted = False


def _same_or_inside(path, prefix):
    norm_path, norm_prefix = os.path.normcase(path), os.path.normcase(prefix)
    return norm_path == norm_prefix or norm_path.startswith(norm_prefix.rstrip("\\/") + os.sep)


class History:
    def __init__(self, change_log, limit=HISTORY_LIMIT):
        self.log = change_log
        self.limit = limit
        self.records = []  # oldest first

    def add(self, kind, title, folder, steps, log_lines=(), target=None):
        if not steps:
            return None
        record = Record(kind, title, folder, steps, target)
        self.records.append(record)
        del self.records[:-self.limit]
        self.log.write(kind.upper(), folder, log_lines, target)
        return record

    def latest_undoable(self):
        for record in reversed(self.records):
            if not record.reverted:
                return record
        return None

    def rewrite_paths(self, old_prefix, new_prefix, skip=None):
        """Keeps older records valid after a folder is renamed."""
        def fix(path):
            if isinstance(path, str) and _same_or_inside(path, old_prefix):
                return new_prefix + path[len(old_prefix):]
            return path
        for record in self.records:
            if record is skip:
                continue
            record.folder = fix(record.folder)
            record.target = fix(record.target)
            record.steps = [tuple([step[0]] + [fix(part) for part in step[1:]]) for step in record.steps]

    def undo(self, record):
        """
        Plays a record's steps backwards. Returns (restored_count, problems, touched_folders).
        Moves are reversed in two phases (everything to a temporary name, then to its original name)
        so files renamed in place can swap names back without colliding.
        """
        problems, touched = [], set()
        restored = 0
        pid = os.getpid()

        for step in reversed(record.steps):
            kind = step[0]
            if kind == "created":
                path = step[1]
                try:
                    if os.path.exists(path):
                        os.remove(path)
                    touched.add(os.path.dirname(path))
                except OSError as err:
                    problems.append(f"{os.path.basename(path)}: {err}")
            elif kind == "rename_dir":
                src, dst = step[1], step[2]
                if not os.path.isdir(dst):
                    problems.append(f"Folder '{os.path.basename(dst)}' no longer exists")
                elif os.path.lexists(src):
                    problems.append(f"A folder named '{os.path.basename(src)}' already exists")
                else:
                    try:
                        os.rename(dst, src)
                        restored += 1
                        touched.update({os.path.dirname(src), src})
                        self.rewrite_paths(dst, src, skip=record)
                    except OSError as err:
                        problems.append(f"Folder rename: {err}")

        staged = []
        moves = [step for step in record.steps if step[0] == "move"]
        for number, (_, src, dst) in enumerate(reversed(moves)):
            if not os.path.exists(dst):
                problems.append(f"{os.path.basename(src)}: the file is no longer at {dst}")
                continue
            source_dir = os.path.dirname(src)
            try:
                os.makedirs(source_dir, exist_ok=True)
                temp = os.path.join(source_dir, f".iot_undo_{pid}_{number}{os.path.splitext(src)[1]}")
                move_file(dst, temp)
                staged.append((temp, src))
                touched.update({source_dir, os.path.dirname(dst)})
            except OSError as err:
                problems.append(f"{os.path.basename(src)}: {err}")
        for temp, src in staged:
            target = src
            if os.path.lexists(src):
                target = unique_path(os.path.dirname(src), os.path.basename(src))
                problems.append(f"'{os.path.basename(src)}' was taken, restored as '{os.path.basename(target)}'")
            try:
                move_file(temp, target)
                restored += 1
            except OSError as err:
                problems.append(f"{os.path.basename(src)}: {err}")

        for step in reversed(record.steps):
            if step[0] == "mkdir":
                try:
                    os.rmdir(step[1])  # only removes it if it's empty again
                    touched.add(os.path.dirname(step[1]))
                except OSError:
                    pass

        record.reverted = True
        lines = [f"Reverted: {record.title}"] + [f"Problem: {p}" for p in problems]
        self.log.write("UNDO", record.folder, lines)
        return restored, problems, touched
