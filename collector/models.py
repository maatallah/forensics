"""Core data models shared by every collector module.

This module deliberately has no runtime dependency on the other collector
modules (only ``TYPE_CHECKING`` imports) so that it can be imported from
anywhere without creating cycles.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .age_analysis import AgeBuckets
    from .aggregators import Aggregates, TopFiles
    from .duplicate_detector import DuplicateDetector

MB: int = 1024 * 1024
"""Number of bytes in one (binary) megabyte."""

_UNITS: tuple[str, ...] = ("B", "KB", "MB", "GB", "TB", "PB", "EB")


def format_size(num_bytes: int | float) -> str:
    """Return a human readable binary size (1 KB = 1024 B).

    >>> format_size(0)
    '0 B'
    >>> format_size(1536)
    '1.50 KB'
    """
    value = float(num_bytes)
    unit = 0
    while value >= 1024.0 and unit < len(_UNITS) - 1:
        value /= 1024.0
        unit += 1
    if unit == 0:
        return f"{int(value)} B"
    return f"{value:.2f} {_UNITS[unit]}"


@dataclass(frozen=True, slots=True)
class Limits:
    """Memory-bounding limits handed to one set of aggregators.

    Every limit is *soft*: when it is reached a purge/compaction mechanism
    runs (see each aggregator) instead of the scan being interrupted.
    """

    top_files: int
    min_duplicate_size_bytes: int
    max_directories: int
    max_extensions: int
    max_duplicate_sizes: int
    max_duplicate_paths: int


@dataclass(frozen=True, slots=True)
class ScanConfig:
    """Immutable configuration of one scan run (all targets)."""

    targets: tuple[str, ...]
    workers: int = 8
    top_files: int = 1000
    min_duplicate_size_mb: float = 100.0
    output_dir: str = "reports"
    split_depth: int = 1
    max_directories: int = 300_000
    max_extensions: int = 20_000
    max_duplicate_sizes: int = 300_000
    max_duplicate_paths: int = 25
    memory_limit_mb: int = 500
    progress_interval: float = 5.0

    def __post_init__(self) -> None:
        if not self.targets:
            raise ValueError("at least one target is required")
        if self.workers < 1:
            raise ValueError("workers must be >= 1")
        if self.top_files < 0:
            raise ValueError("top_files must be >= 0")
        if self.min_duplicate_size_mb < 0:
            raise ValueError("min_duplicate_size_mb must be >= 0")
        if self.split_depth < 1:
            raise ValueError("split_depth must be >= 1")
        if self.max_directories < 100:
            raise ValueError("max_directories must be >= 100")

    @property
    def min_duplicate_size_bytes(self) -> int:
        """Duplicate-candidate size threshold in bytes."""
        return int(self.min_duplicate_size_mb * MB)

    def reducer_limits(self) -> Limits:
        """Limits applied to the final (reducer) result."""
        return Limits(
            top_files=self.top_files,
            min_duplicate_size_bytes=self.min_duplicate_size_bytes,
            max_directories=self.max_directories,
            max_extensions=self.max_extensions,
            max_duplicate_sizes=self.max_duplicate_sizes,
            max_duplicate_paths=self.max_duplicate_paths,
        )

    def worker_limits(self) -> Limits:
        """Limits applied to each worker's partial result.

        The global budgets are divided by the number of workers (with a
        floor) so that ``workers * partial + reducer`` stays inside the
        overall memory budget.
        """
        w = self.workers
        return Limits(
            top_files=self.top_files,
            min_duplicate_size_bytes=self.min_duplicate_size_bytes,
            max_directories=max(20_000, self.max_directories // w),
            max_extensions=self.max_extensions,
            max_duplicate_sizes=max(20_000, self.max_duplicate_sizes // w),
            max_duplicate_paths=self.max_duplicate_paths,
        )


@dataclass(frozen=True, slots=True)
class WorkUnit:
    """One schedulable piece of work.

    ``recursive=False`` means "only the files directly inside ``path``";
    it is used for the upper levels of the split tree.
    """

    target: str
    path: str
    recursive: bool = True


class UnitStats:
    """Live counters of a single work unit.

    Only the owning worker thread writes to it; the main thread merely reads
    the two integers (atomic under the GIL) to display progress.
    """

    __slots__ = ("files", "bytes")

    def __init__(self) -> None:
        self.files: int = 0
        self.bytes: int = 0


@dataclass(slots=True)
class PartialResult:
    """Everything a worker (or the reducer) accumulates.

    A worker owns its instance exclusively while scanning and hands it over
    through a ``Future``; the reducer then merges it in the main thread. No
    instance is ever shared between threads, hence no locks are needed.
    """

    aggregates: Aggregates
    top_files: TopFiles
    ages: AgeBuckets
    duplicates: DuplicateDetector
    errors: Counter[str] = field(default_factory=Counter)

    @property
    def total_files(self) -> int:
        """Number of files accounted for."""
        return self.aggregates.total_files

    @property
    def total_bytes(self) -> int:
        """Sum of the sizes of all accounted files."""
        return self.aggregates.total_bytes

    def merge(self, other: PartialResult) -> None:
        """Merge ``other`` into ``self`` (reduce step)."""
        self.aggregates.merge(other.aggregates)
        self.top_files.merge(other.top_files)
        self.ages.merge(other.ages)
        self.duplicates.merge(other.duplicates)
        self.errors.update(other.errors)

    def shrink(self) -> None:
        """Aggressively release memory (called under memory pressure)."""
        self.aggregates.shrink()
        self.duplicates.shrink()


@dataclass(slots=True)
class TargetReport:
    """Final outcome for one target."""

    target: str
    label: str
    start: float
    end: float
    result: PartialResult | None
    roots_total: int = 0
    roots_done: int = 0
    interrupted: bool = False
    failure: str | None = None


@dataclass(frozen=True, slots=True)
class ProgressSnapshot:
    """Point-in-time progress of one target, for display."""

    target: str
    roots_done: int
    roots_total: int
    files: int
    bytes: int
    elapsed: float
    files_per_sec: float
    mb_per_sec: float
    eta_seconds: float | None
    rss_bytes: int
    finished: bool

