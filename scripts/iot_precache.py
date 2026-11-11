"""
I.O.T thumbnail pre-cacher.

Builds I.O.T's thumbnail cache ahead of time (overnight, or whenever is convenient), so even huge
folders open instantly. It uses exactly the same cache code as I.O.T itself, so everything it makes
is picked up by the app.

Usage
  Run it from the same folder you open in I.O.T (the parent hub), or pass that folder:
      python scripts\\iot_precache.py "D:\\Pictures\\Hub"      (from the folder iot.py is in)
  Options
      --workers N      number of images processed at once (default: one per CPU core, max 8)
      --clean-old      delete thumbnails left by the old version of this script without asking
      --no-pause       don't wait for ENTER at the end (for Task Scheduler / batch files)
      --normal-priority  run at normal priority (by default it runs at low priority on Windows,
                         so the computer stays responsive while it works)

Safe to stop at any time with Ctrl+C: everything finished so far is kept, and the next run carries
on where it left off. Images that haven't changed are skipped in a fraction of a second.

Note: tick "Disable Cache Deletion" in I.O.T (it's on by default), otherwise I.O.T deletes the
cache when it closes.
"""
import os
import sys
import time
import argparse
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import iot_utils  # noqa: E402

CACHE_DIR_NAME = ".thumbs_cache"


def find_images(root):
    """Same folders and file types I.O.T shows (hidden '.'-folders such as caches are skipped)."""
    paths = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted((d for d in dirnames if not d.startswith(".")), key=iot_utils.natural_sort_key)
        for name in sorted(filenames, key=iot_utils.natural_sort_key):
            if name.lower().endswith(iot_utils.IMAGE_EXTENSIONS):
                paths.append(os.path.join(dirpath, name))
    return paths


def find_old_cache(root):
    """Thumbnails written by the old version of this script ('<name>_<size>.cache' in every folder)."""
    files, folders, total = [], [], 0
    for dirpath, dirnames, filenames in os.walk(root):
        if os.path.basename(dirpath) == CACHE_DIR_NAME:
            old = [os.path.join(dirpath, f) for f in filenames if f.endswith(".cache")]
            for path in old:
                try:
                    total += os.path.getsize(path)
                except OSError:
                    pass
            files += old
            if os.path.dirname(dirpath) != root:
                folders.append(dirpath)
            dirnames[:] = []
    return files, folders, total


def remove_old_cache(files, folders):
    removed = 0
    for path in files:
        try:
            os.remove(path)
            removed += 1
        except OSError:
            pass
    for folder in folders:
        try:
            os.rmdir(folder)  # only if nothing else is in it
        except OSError:
            pass
    return removed


def lower_priority():
    if sys.platform.startswith("win"):
        try:
            import ctypes
            BELOW_NORMAL_PRIORITY_CLASS = 0x4000
            ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), BELOW_NORMAL_PRIORITY_CLASS)
        except Exception:
            pass
    else:
        try:
            os.nice(10)
        except Exception:
            pass


def format_duration(seconds):
    seconds = int(seconds)
    if seconds >= 3600:
        return f"{seconds // 3600}h {seconds % 3600 // 60:02d}m"
    return f"{seconds // 60}:{seconds % 60:02d}"


class Progress:
    """One self-updating console line, redrawn at most 8 times a second."""

    def __init__(self, total):
        self.total = total
        self.counts = {"built": 0, "cached": 0, "failed": 0}
        self.start = time.monotonic()
        self._last_draw = 0.0

    def add(self, status, force=False):
        self.counts[status] += 1
        now = time.monotonic()
        if force or now - self._last_draw >= 0.125:
            self._last_draw = now
            self.draw()

    def draw(self):
        done = sum(self.counts.values())
        elapsed = max(1e-6, time.monotonic() - self.start)
        built = self.counts["built"]
        rate = built / elapsed
        remaining = self.total - done
        eta = f"ETA {format_duration(remaining / rate)}" if rate > 0 and remaining else ""
        width = 28
        filled = int(width * done / self.total) if self.total else width
        bar = "#" * filled + "-" * (width - filled)
        line = (f"\r  [{bar}] {done}/{self.total}  {rate:5.1f} img/s  {eta:<12}"
                f" new {built}  already cached {self.counts['cached']}  failed {self.counts['failed']}")
        sys.stdout.write(line)
        sys.stdout.flush()


def precache(root, workers, clean_old):
    root = os.path.abspath(root)
    cache_dir = os.path.join(root, CACHE_DIR_NAME)
    print(f"I.O.T thumbnail pre-cacher")
    print(f"  Folder : {root}")
    print(f"  Cache  : {cache_dir}")
    print()

    old_files, old_folders, old_bytes = find_old_cache(root)
    if old_files:
        print(f"Found {len(old_files)} thumbnails from the old version of this script "
              f"({old_bytes / 1_048_576:.1f} MB). I.O.T no longer uses them.")
        answer = "y" if clean_old else ""
        if not clean_old and sys.stdin and sys.stdin.isatty():
            answer = input("  Delete them? [y/N] ").strip().lower()
        if answer == "y":
            print(f"  Deleted {remove_old_cache(old_files, old_folders)} old thumbnails.")
        else:
            print("  Left them in place (run with --clean-old to remove them).")
        print()

    print("Scanning for images...")
    scan_start = time.monotonic()
    paths = find_images(root)
    print(f"  {len(paths)} images in {time.monotonic() - scan_start:.1f}s")
    if not paths:
        print("Nothing to do.")
        return 0

    cache = iot_utils.ThumbDiskCache(cache_dir)
    progress = Progress(len(paths))
    failures = []
    print(f"Building thumbnails with {workers} worker(s). Ctrl+C stops safely and keeps what's done.\n")

    executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="precache")
    futures = {executor.submit(cache.prebuild, path): path for path in paths}
    interrupted = False
    try:
        for future in as_completed(futures):
            status, error = future.result()
            if status == "failed":
                failures.append((futures[future], error))
            progress.add(status)
    except KeyboardInterrupt:
        interrupted = True
        for future in futures:
            future.cancel()
    finally:
        executor.shutdown(wait=True)
    progress.draw()
    print("\n")

    elapsed = time.monotonic() - progress.start
    counts = progress.counts
    if interrupted:
        print(f"Stopped. {counts['built']} new thumbnails were saved; run again to finish the rest.")
    else:
        print(f"Done in {format_duration(elapsed)}: {counts['built']} new, {counts['cached']} already cached, "
              f"{counts['failed']} failed.")
    if failures:
        print("\nCouldn't read these images (corrupt, locked or unsupported):")
        for path, error in failures[:15]:
            print(f"  {os.path.relpath(path, root)}  -  {error}")
        if len(failures) > 15:
            print(f"  ...and {len(failures) - 15} more")
    print('\nReminder: keep "Disable Cache Deletion" ticked in I.O.T, or the cache is deleted when it closes.')
    return 1 if failures else 0


def main():
    parser = argparse.ArgumentParser(description="Pre-build I.O.T's thumbnail cache.")
    parser.add_argument("folder", nargs="?", default=os.getcwd(), help="the parent hub you open in I.O.T (default: current folder)")
    parser.add_argument("--workers", type=int, default=max(1, min(8, os.cpu_count() or 4)))
    parser.add_argument("--clean-old", action="store_true")
    parser.add_argument("--no-pause", action="store_true")
    parser.add_argument("--normal-priority", action="store_true")
    args = parser.parse_args()

    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass
    if not args.normal_priority:
        lower_priority()

    exit_code = 0
    try:
        if not os.path.isdir(args.folder):
            print(f"Folder not found: {args.folder}")
            exit_code = 2
        else:
            exit_code = precache(args.folder, max(1, args.workers), args.clean_old)
    except Exception:
        traceback.print_exc()
        exit_code = 3
    if not args.no_pause:
        try:
            input("\nPress ENTER to close...")
        except (EOFError, KeyboardInterrupt):
            pass
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
