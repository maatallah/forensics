"""Bounded, mergeable, streaming aggregators.

Every structure here has a hard-ish memory bound and a ``merge`` method used
by the reducer. None of them is thread-safe *by design*: each is owned by a
single thread at a time (worker -> reducer hand-over through a ``Future``).
"""

from __future__ import annotations

import heapq
import os
import sys
from collections.abc import Iterator

_dirname = os.path.dirname

OTHER_EXTENSION = "<other>"
NO_EXTENSION = "<none>"


class TopFiles:
    """Top-N largest files using a min-heap (``heapq``).

    The heap root is the *smallest* of the N kept files, so a candidate is
    accepted or rejected in O(1) and inserted in O(log N). The full inventory
    is never sorted or stored.
    """

    __slots__ = ("capacity", "_heap", "threshold")

    def __init__(self, capacity: int) -> None:
        self.capacity: int = capacity
        self._heap: list[tuple[int, str, float]] = []
        #: Files with ``size <= threshold`` can be skipped by the caller
        #: without even building their path string.
        self.threshold: int = sys.maxsize if capacity <= 0 else -1

    def offer(self, size: int, path: str, mtime: float) -> None:
        """Consider one file for the Top-N (O(log N) worst case)."""
        heap = self._heap
        if len(heap) < self.capacity:
            heapq.heappush(heap, (size, path, mtime))
            if len(heap) == self.capacity:
                self.threshold = heap[0][0]
        elif size > heap[0][0]:
            heapq.heapreplace(heap, (size, path, mtime))
            self.threshold = heap[0][0]

    def merge(self, other: TopFiles) -> None:
        """Merge another heap (at most ``N`` pushes)."""
        for size, path, mtime in other._heap:
            self.offer(size, path, mtime)

    def __len__(self) -> int:
        return len(self._heap)

    def items_desc(self) -> list[tuple[int, str, float]]:
        """Return the kept files, largest first (sorts N items only)."""
        return sorted(self._heap, reverse=True)

    def largest(self) -> tuple[int, str, float] | None:
        """Return the largest kept file, if any."""
        return max(self._heap) if self._heap else None


class DirectoryTotals:
    """Per-directory ``[files, bytes]`` totals with bounded memory.

    Totals are *direct* (files located in the directory itself). When the
    number of tracked directories exceeds ``max_directories`` the smallest
    directories are **rolled up into their parent**: memory shrinks, grand
    totals stay exact, only the granularity of small directories is lost.
    """

    __slots__ = ("max_directories", "_data", "compactions")

    def __init__(self, max_directories: int) -> None:
        self.max_directories: int = max_directories
        self._data: dict[str, list[int]] = {}
        self.compactions: int = 0

    def add(self, directory: str, size: int) -> None:
        """Account one file of ``size`` bytes in ``directory``."""
        entry = self._data.get(directory)
        if entry is None:
            if len(self._data) >= self.max_directories:
                self.compact(int(self.max_directories * 0.75))
                if len(self._data) >= self.max_directories:
                    # Could not shrink (very flat tree): grow the soft limit
                    # to avoid compacting on every single insertion.
                    self.max_directories = int(len(self._data) * 1.25)
            self._data[directory] = [1, size]
        else:
            entry[0] += 1
            entry[1] += size

    def add_bulk(self, directory: str, files: int, size: int) -> None:
        """Account ``files`` files totalling ``size`` bytes (merge helper)."""
        entry = self._data.get(directory)
        if entry is None:
            if len(self._data) >= self.max_directories:
                self.compact(int(self.max_directories * 0.75))
                if len(self._data) >= self.max_directories:
                    self.max_directories = int(len(self._data) * 1.25)
            self._data[directory] = [files, size]
        else:
            entry[0] += files
            entry[1] += size

    def compact(self, target: int) -> None:
        """Roll the smallest directories up into their parents until at most
        ``target`` entries remain (or no further progress is possible)."""
        data = self._data
        for _ in range(32):
            excess = len(data) - target
            if excess <= 0:
                break
            victims = heapq.nsmallest(excess, data.items(), key=lambda kv: kv[1][1])
            moved = 0
            for path, (files, size) in victims:
                parent = _dirname(path)
                if not parent or parent == path:
                    continue  # scan roots are never rolled up
                del data[path]
                parent_entry = data.get(parent)
                if parent_entry is None:
                    data[parent] = [files, size]
                else:
                    parent_entry[0] += files
                    parent_entry[1] += size
                moved += 1
            if moved == 0:
                break
        self.compactions += 1

    def shrink(self) -> None:
        """Halve the number of tracked directories (memory pressure)."""
        if len(self._data) > 1000:
            self.compact(len(self._data) // 2)

    def merge(self, other: DirectoryTotals) -> None:
        """Merge another instance."""
        for path, (files, size) in other._data.items():
            self.add_bulk(path, files, size)
        self.compactions += other.compactions

    def __len__(self) -> int:
        return len(self._data)

    def items(self) -> Iterator[tuple[str, int, int]]:
        """Iterate ``(path, files, bytes)`` in arbitrary order."""
        for path, (files, size) in self._data.items():
            yield path, files, size

    def top(self, n: int) -> list[tuple[str, int, int]]:
        """Return the ``n`` largest directories by direct bytes."""
        return heapq.nlargest(n, self.items(), key=lambda t: t[2])


class ExtensionTotals:
    """Per-extension ``[files, bytes]`` totals; cardinality is capped.

    Once ``max_extensions`` distinct extensions exist, new ones are folded
    into the ``<other>`` bucket (protects against pathological file names).
    """

    __slots__ = ("max_extensions", "_data", "overflowed")

    def __init__(self, max_extensions: int) -> None:
        self.max_extensions: int = max_extensions
        self._data: dict[str, list[int]] = {}
        self.overflowed: bool = False

    def add(self, extension: str, size: int) -> None:
        """Account one file (``extension`` lower-case, with leading dot)."""
        entry = self._data.get(extension)
        if entry is None:
            if len(self._data) >= self.max_extensions:
                self.overflowed = True
                extension = OTHER_EXTENSION
                entry = self._data.get(extension)
                if entry is None:
                    self._data[extension] = [1, size]
                    return
            else:
                self._data[extension] = [1, size]
                return
        entry[0] += 1
        entry[1] += size

    def add_bulk(self, extension: str, files: int, size: int) -> None:
        """Account many files at once (merge helper)."""
        entry = self._data.get(extension)
        if entry is None:
            if len(self._data) >= self.max_extensions:
                self.overflowed = True
                extension = OTHER_EXTENSION
                entry = self._data.get(extension)
            if entry is None:
                self._data[extension] = [files, size]
                return
        entry[0] += files
        entry[1] += size

    def merge(self, other: ExtensionTotals) -> None:
        """Merge another instance."""
        for ext, (files, size) in other._data.items():
            self.add_bulk(ext, files, size)
        self.overflowed = self.overflowed or other.overflowed

    def __len__(self) -> int:
        return len(self._data)

    def items(self) -> Iterator[tuple[str, int, int]]:
        """Iterate ``(extension, files, bytes)``."""
        for ext, (files, size) in self._data.items():
            yield ext, files, size

    def sorted_by_bytes(self) -> list[tuple[str, int, int]]:
        """All extensions, largest total first (cardinality is capped)."""
        return sorted(self.items(), key=lambda t: t[2], reverse=True)


class Aggregates:
    """Real-time aggregates: ``total_files``, ``total_bytes``,
    ``directory_totals`` and ``extension_totals``."""

    __slots__ = ("total_files", "total_bytes", "directories", "extensions")

    def __init__(self, max_directories: int, max_extensions: int) -> None:
        self.total_files: int = 0
        self.total_bytes: int = 0
        self.directories = DirectoryTotals(max_directories)
        self.extensions = ExtensionTotals(max_extensions)

    def add(self, directory: str, extension: str, size: int) -> None:
        """Account one file (hot path)."""
        self.total_files += 1
        self.total_bytes += size
        self.directories.add(directory, size)
        self.extensions.add(extension, size)

    def merge(self, other: Aggregates) -> None:
        """Merge another instance."""
        self.total_files += other.total_files
        self.total_bytes += other.total_bytes
        self.directories.merge(other.directories)
        self.extensions.merge(other.extensions)

    def shrink(self) -> None:
        """Release memory under pressure."""
        self.directories.shrink()
