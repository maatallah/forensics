"""Duplicate *candidate* detection by file size (V2).

Files are grouped by exact size; no content is read. Two files of equal size
are only *candidates*: a later version will confirm with partial/full hashes.

Memory bounds
-------------
* Only files ``>= min_size`` are tracked.
* At most ``max_paths`` paths are kept per size group (the count stays exact).
* At most ``max_sizes`` distinct sizes are tracked. When full, the smallest
  *singleton* sizes (no duplicate seen yet, least valuable) are purged.
  Purging can under-count a group; ``purged``/``dropped`` expose this.
"""

from __future__ import annotations

import heapq


class _Group:
    """Files sharing one size."""

    __slots__ = ("count", "paths")

    def __init__(self, count: int, paths: list[str]) -> None:
        self.count: int = count
        self.paths: list[str] = paths


class DuplicateDetector:
    """Size-based duplicate candidate collector."""

    __slots__ = ("min_size", "max_sizes", "max_paths", "_groups", "purged", "dropped")

    def __init__(self, min_size: int, max_sizes: int = 300_000, max_paths: int = 25) -> None:
        self.min_size: int = min_size
        self.max_sizes: int = max_sizes
        self.max_paths: int = max_paths
        self._groups: dict[int, _Group] = {}
        self.purged: int = 0
        self.dropped: int = 0

    def offer(self, size: int, path: str) -> None:
        """Consider one file; ignored when empty or below the threshold."""
        if size <= 0 or size < self.min_size:
            return
        group = self._groups.get(size)
        if group is None:
            self._insert(size, 1, [path])
        else:
            group.count += 1
            if len(group.paths) < self.max_paths:
                group.paths.append(path)

    def _insert(self, size: int, count: int, paths: list[str]) -> None:
        if len(self._groups) >= self.max_sizes:
            self._purge(max(1, self.max_sizes // 4))
            if len(self._groups) >= self.max_sizes:
                self.dropped += count
                return
        self._groups[size] = _Group(count, paths)

    def _purge(self, how_many: int) -> None:
        """Drop the ``how_many`` smallest singleton sizes."""
        singles = (s for s, g in self._groups.items() if g.count == 1)
        for size in heapq.nsmallest(how_many, singles):
            del self._groups[size]
            self.purged += 1

    def shrink(self) -> None:
        """Release memory under pressure: purge half of the singletons."""
        singles = sum(1 for g in self._groups.values() if g.count == 1)
        if singles:
            self._purge(max(1, singles // 2))

    def merge(self, other: DuplicateDetector) -> None:
        """Merge another detector."""
        self.purged += other.purged
        self.dropped += other.dropped
        for size, group in other._groups.items():
            mine = self._groups.get(size)
            if mine is None:
                self._insert(size, group.count, list(group.paths))
            else:
                mine.count += group.count
                room = self.max_paths - len(mine.paths)
                if room > 0:
                    mine.paths.extend(group.paths[:room])

    def __len__(self) -> int:
        return len(self._groups)

    def groups(self) -> list[tuple[int, int, list[str]]]:
        """Return ``(size, count, paths)`` for groups with ``count >= 2``,
        ordered by reclaimable bytes (``size * (count - 1)``) descending."""
        found = [(s, g.count, g.paths) for s, g in self._groups.items() if g.count >= 2]
        found.sort(key=lambda t: t[0] * (t[1] - 1), reverse=True)
        return found
