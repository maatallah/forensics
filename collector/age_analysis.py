"""Temporal (age) analysis based on the last-modification time."""

from __future__ import annotations

from bisect import bisect_right

DAY: int = 86_400

BUCKET_LABELS: tuple[str, ...] = (
    "<30 jours",
    "30-90 jours",
    "90-365 jours",
    "1-3 ans",
    ">3 ans",
)

_BOUNDS: tuple[int, ...] = (30 * DAY, 90 * DAY, 365 * DAY, 3 * 365 * DAY)


class AgeBuckets:
    """Five fixed counters; memory use is constant.

    Args:
        now: Reference timestamp (seconds since the epoch). It is captured
            once per scan so that every worker uses the same reference and
            partial results can be merged consistently.
    """

    __slots__ = ("now", "files", "bytes")

    def __init__(self, now: float) -> None:
        self.now: float = now
        self.files: list[int] = [0] * len(BUCKET_LABELS)
        self.bytes: list[int] = [0] * len(BUCKET_LABELS)

    @staticmethod
    def bucket_index(age_seconds: float) -> int:
        """Return the bucket index for an age (negative ages -> newest)."""
        return bisect_right(_BOUNDS, age_seconds)

    def add(self, mtime: float, size: int) -> None:
        """Account one file."""
        index = bisect_right(_BOUNDS, self.now - mtime)
        self.files[index] += 1
        self.bytes[index] += size

    def merge(self, other: AgeBuckets) -> None:
        """Merge another instance."""
        for i in range(len(BUCKET_LABELS)):
            self.files[i] += other.files[i]
            self.bytes[i] += other.bytes[i]

    def rows(self) -> list[tuple[str, int, int]]:
        """Return ``(label, files, bytes)`` for each bucket, in order."""
        return [(BUCKET_LABELS[i], self.files[i], self.bytes[i]) for i in range(len(BUCKET_LABELS))]
