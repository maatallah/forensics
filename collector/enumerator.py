"""High-performance, streaming file-system enumeration.

Design notes
------------
* ``os.scandir`` only: on Windows the size and mtime come from the directory
  listing itself, so ``entry.stat()`` costs **no extra system call**.
* Iterative walk with an explicit stack: no recursion limit, and memory is
  bounded by the number of *pending directories* (not files).
* Symbolic links and junctions are never followed (avoids cycles and double
  counting, important on DFS and SMB).
* Every OS error is classified and reported through a callback; enumeration
  then continues with the next entry.
"""

from __future__ import annotations

import errno
import os
import threading
from collections.abc import Callable, Iterator

ErrorCallback = Callable[[str, str, OSError], None]
"""``callback(path, category, exception)``."""

ACCESS_DENIED = "access_denied"
PATH_TOO_LONG = "path_too_long"
REMOVED = "removed_during_scan"
OTHER = "other_error"

_SEP = os.sep
_WIN_MAX_PATH_SAFE = 248
_WIN_ERROR_FILENAME_EXCED_RANGE = 206


def normalize_target(target: str) -> str:
    """Normalise a user supplied target (``D:`` -> ``D:\\``, strip quotes)."""
    target = target.strip().strip('"')
    if os.name == "nt" and len(target) == 2 and target[1] == ":":
        return target + "\\"
    # Windows PowerShell may leave a trailing quote on ``"D:\"`` arguments.
    return target


def fs_path(path: str) -> str:
    r"""Return a path usable for system calls (adds ``\\?\`` when too long)."""
    if os.name == "nt" and len(path) >= _WIN_MAX_PATH_SAFE and not path.startswith("\\\\?\\"):
        if path.startswith("\\\\"):
            return "\\\\?\\UNC\\" + path[2:]
        return "\\\\?\\" + path
    return path


def classify_error(exc: OSError) -> str:
    """Map an ``OSError`` to one of the error categories above."""
    if getattr(exc, "winerror", None) == _WIN_ERROR_FILENAME_EXCED_RANGE or exc.errno == errno.ENAMETOOLONG:
        return PATH_TOO_LONG
    if isinstance(exc, PermissionError):
        return ACCESS_DENIED
    if isinstance(exc, (FileNotFoundError, NotADirectoryError)):
        return REMOVED
    return OTHER


def join_path(directory: str, name: str) -> str:
    """Join a directory and an entry name without ``os.path`` overhead."""
    if directory[-1:] == _SEP:
        return directory + name
    return directory + _SEP + name


def iter_subdirs(path: str) -> list[str]:
    """Return the immediate sub-directories of ``path`` (errors ignored).

    Used by the planner only; the real error reporting happens when the
    directory is scanned by a worker.
    """
    result: list[str] = []
    try:
        with os.scandir(fs_path(path)) as it:
            for entry in it:
                try:
                    if entry.is_dir(follow_symlinks=False) and not entry.is_junction():
                        result.append(join_path(path, entry.name))
                except OSError:
                    continue
    except OSError:
        pass
    return result


def walk_files(
    root: str,
    on_error: ErrorCallback,
    cancel: threading.Event | None = None,
    recursive: bool = True,
) -> Iterator[tuple[str, str, int, float]]:
    """Stream every regular file below ``root``.

    Yields ``(directory, name, size_bytes, mtime)`` tuples. The ``directory``
    string object is shared by all files of the same directory (no per-file
    string allocation for it). Nothing is retained after yielding.

    Args:
        root: Directory to enumerate.
        on_error: Called for every recoverable error; enumeration continues.
        cancel: Optional event; when set the walk stops at the next directory.
        recursive: When false only the direct files of ``root`` are produced.
    """
    stack: list[str] = [root]
    pop = stack.pop
    push = stack.append
    scandir = os.scandir

    while stack:
        if cancel is not None and cancel.is_set():
            return
        current = pop()
        prefix = current if current[-1:] == _SEP else current + _SEP
        try:
            iterator = scandir(fs_path(current))
        except OSError as exc:
            on_error(current, classify_error(exc), exc)
            continue

        with iterator:
            while True:
                try:
                    entry = next(iterator)
                except StopIteration:
                    break
                except OSError as exc:
                    on_error(current, classify_error(exc), exc)
                    break

                size = -1
                mtime = 0.0
                try:
                    if entry.is_dir(follow_symlinks=False):
                        if recursive and not entry.is_junction():
                            push(prefix + entry.name)
                    elif entry.is_file(follow_symlinks=False):
                        st = entry.stat(follow_symlinks=False)
                        size = st.st_size
                        mtime = st.st_mtime
                except OSError as exc:
                    on_error(prefix + entry.name, classify_error(exc), exc)
                    continue

                if size >= 0:
                    yield current, entry.name, size, mtime
