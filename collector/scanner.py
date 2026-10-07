"""Scan orchestration: planning, parallel workers, reducer, progress.

Architecture
------------
::

    plan  ->  WorkUnit, WorkUnit, ...        (round-robin across targets)
    Worker(unit) -> PartialResult            (private state, no sharing)
    Reducer (main thread) <- PartialResult   (merged then discarded)

* Workers are threads: ``os.scandir`` releases the GIL during system calls,
  which is where an I/O-bound scan spends its time (local, SAN, SMB, DFS).
* Each worker owns a private :class:`PartialResult`; ownership is handed to
  the reducer through a ``Future``. Because the reducer runs only in the main
  thread and workers never touch shared mutable state, **no lock and no
  shared dictionary exist**.
* At most ``workers`` partial results are alive at once, plus the reducer.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from itertools import zip_longest

from .age_analysis import AgeBuckets
from .aggregators import Aggregates, TopFiles
from .duplicate_detector import DuplicateDetector
from .enumerator import iter_subdirs, normalize_target, walk_files
from .models import (
    Limits,
    PartialResult,
    ProgressSnapshot,
    ScanConfig,
    TargetReport,
    UnitStats,
    WorkUnit,
)

LOG = logging.getLogger("sfc.scanner")
LOG.addHandler(logging.NullHandler())

_PUBLISH_MASK = 0xFF  # publish live counters every 256 files
_CHECK_MASK = 0xFFFF  # check memory pressure every 65 536 files


# --------------------------------------------------------------------------
# Memory probing
# --------------------------------------------------------------------------
def current_rss_bytes() -> int:
    """Return the resident memory of this process (0 if unknown)."""
    try:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            class _PMC(ctypes.Structure):
                _fields_ = [
                    ("cb", wintypes.DWORD),
                    ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            kernel32 = ctypes.WinDLL("kernel32")
            psapi = ctypes.WinDLL("psapi")
            kernel32.GetCurrentProcess.restype = wintypes.HANDLE
            psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PMC), wintypes.DWORD]
            psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
            counters = _PMC()
            counters.cb = ctypes.sizeof(_PMC)
            if psapi.GetProcessMemoryInfo(kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
                return int(counters.WorkingSetSize)
            return 0
        with open("/proc/self/statm", encoding="ascii") as fh:
            return int(fh.read().split()[1]) * os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError, AttributeError, IndexError):
        return 0


# --------------------------------------------------------------------------
# Worker
# --------------------------------------------------------------------------
def new_result(limits: Limits, scan_ts: float) -> PartialResult:
    """Create an empty :class:`PartialResult` bounded by ``limits``."""
    return PartialResult(
        aggregates=Aggregates(limits.max_directories, limits.max_extensions),
        top_files=TopFiles(limits.top_files),
        ages=AgeBuckets(scan_ts),
        duplicates=DuplicateDetector(
            limits.min_duplicate_size_bytes,
            limits.max_duplicate_sizes,
            limits.max_duplicate_paths,
        ),
    )


def scan_unit(
    unit: WorkUnit,
    limits: Limits,
    scan_ts: float,
    stats: UnitStats,
    cancel: threading.Event,
    pressure: threading.Event,
) -> PartialResult:
    """Scan one work unit in streaming mode and return its partial result.

    Each file is analysed once and immediately dropped; only bounded
    aggregates survive. Errors are logged and counted, never raised.
    """
    result = new_result(limits, scan_ts)
    errors = result.errors

    def on_error(path: str, category: str, exc: OSError) -> None:
        errors[category] += 1
        LOG.warning("%s | %s | %s", category, path, exc)

    add_aggregate = result.aggregates.add
    add_age = result.ages.add
    top = result.top_files
    offer_top = top.offer
    duplicates = result.duplicates
    offer_duplicate = duplicates.offer
    min_dup = max(1, duplicates.min_size)
    sep = os.sep

    files = 0
    total = 0
    try:
        for directory, name, size, mtime in walk_files(unit.path, on_error, cancel, unit.recursive):
            files += 1
            total += size
            dot = name.rfind(".")
            extension = name[dot:].lower() if 0 < dot < len(name) - 1 else ""
            add_aggregate(directory, extension, size)
            add_age(mtime, size)

            if size > top.threshold or size >= min_dup:
                path = directory + name if directory[-1:] == sep else directory + sep + name
                if size > top.threshold:
                    offer_top(size, path, mtime)
                if size >= min_dup:
                    offer_duplicate(size, path)

            if not files & _PUBLISH_MASK:
                stats.files = files
                stats.bytes = total
                if not files & _CHECK_MASK and pressure.is_set():
                    result.shrink()
    except Exception:  # noqa: BLE001 - a worker must never kill the scan
        errors["worker_failure"] += 1
        LOG.exception("unexpected failure while scanning %s", unit.path)
    finally:
        stats.files = files
        stats.bytes = total
    return result


# --------------------------------------------------------------------------
# Planning
# --------------------------------------------------------------------------
def plan_units(target: str, split_depth: int) -> tuple[list[WorkUnit], int]:
    """Split ``target`` into work units.

    Directories above ``split_depth`` become non-recursive units (their
    direct files only); directories at ``split_depth`` become recursive
    units. Returns ``(units, root_folder_count)``.
    """
    units: list[WorkUnit] = []
    frontier = [target]
    roots = 0
    for level in range(split_depth):
        following: list[str] = []
        for directory in frontier:
            units.append(WorkUnit(target, directory, recursive=False))
            following.extend(iter_subdirs(directory))
        if level == 0:
            roots = len(following)
        frontier = following
    units.extend(WorkUnit(target, directory, recursive=True) for directory in frontier)
    return units, roots


def _round_robin(groups: list[list[WorkUnit]]) -> Iterator[WorkUnit]:
    """Interleave units of several targets so they all progress together."""
    sentinel = object()
    for batch in zip_longest(*groups, fillvalue=sentinel):
        for item in batch:
            if item is not sentinel:
                yield item  # type: ignore[misc]


def _is_volume_root(target: str) -> bool:
    drive, rest = os.path.splitdrive(target)
    return bool(drive) and rest in ("", "\\", "/") or target == "/"


class _TargetState:
    """Reducer-side state of one target (main thread only)."""

    def __init__(self, target: str, label: str, units_total: int, roots: int, limits: Limits, scan_ts: float) -> None:
        self.target = target
        self.label = label
        self.units_total = units_total
        self.units_done = 0
        self.roots_total = roots
        self.result = new_result(limits, scan_ts)
        self.live: list[UnitStats] = []
        self.done_files = 0
        self.done_bytes = 0
        self.start = time.time()
        self.end = 0.0
        self.expected_bytes = 0
        if _is_volume_root(target):
            try:
                import shutil

                self.expected_bytes = shutil.disk_usage(target).used
            except OSError:
                self.expected_bytes = 0

    @property
    def finished(self) -> bool:
        return self.units_done >= self.units_total

    def snapshot(self, rss: int) -> ProgressSnapshot:
        live = tuple(self.live)
        files = self.done_files + sum(s.files for s in live)
        nbytes = self.done_bytes + sum(s.bytes for s in live)
        now = self.end if self.finished and self.end else time.time()
        elapsed = max(now - self.start, 1e-6)
        rate = nbytes / elapsed
        eta: float | None = None
        if self.finished:
            eta = 0.0
        elif self.expected_bytes > 0 and rate > 0:
            eta = max(self.expected_bytes - nbytes, 0) / rate
        elif self.units_done > 0:
            eta = elapsed * (self.units_total - self.units_done) / self.units_done
        return ProgressSnapshot(
            target=self.target,
            roots_done=self.units_done,
            roots_total=self.units_total,
            files=files,
            bytes=nbytes,
            elapsed=elapsed,
            files_per_sec=files / elapsed,
            mb_per_sec=rate / (1024 * 1024),
            eta_seconds=eta,
            rss_bytes=rss,
            finished=self.finished,
        )


def _unique_labels(targets: list[str], label_fn: Callable[[str], str]) -> list[str]:
    seen: dict[str, int] = {}
    labels: list[str] = []
    for target in targets:
        base = label_fn(target)
        seen[base] = seen.get(base, 0) + 1
        labels.append(base if seen[base] == 1 else f"{base}_{seen[base]}")
    return labels


# --------------------------------------------------------------------------
# Orchestrator
# --------------------------------------------------------------------------
def run_scan(
    config: ScanConfig,
    on_progress: Callable[[list[ProgressSnapshot]], None] | None = None,
    cancel: threading.Event | None = None,
) -> list[TargetReport]:
    """Scan every target of ``config`` and return one report per target.

    Args:
        config: Scan configuration.
        on_progress: Called every ``progress_interval`` seconds (and once at
            the end) with one snapshot per target.
        cancel: Optional cancellation event. ``KeyboardInterrupt`` also
            triggers a graceful stop; partial results are still returned.
    """
    from .serialization import target_label

    cancel = cancel if cancel is not None else threading.Event()
    pressure = threading.Event()
    scan_ts = time.time()
    worker_limits = config.worker_limits()
    reducer_limits = config.reducer_limits()

    targets = [normalize_target(t) for t in config.targets]
    labels = _unique_labels(targets, target_label)

    reports: list[TargetReport] = []
    states: list[_TargetState] = []
    unit_groups: list[list[WorkUnit]] = []
    for target, label in zip(targets, labels, strict=True):
        if not os.path.isdir(target):
            LOG.error("target not found or not a directory: %s", target)
            reports.append(
                TargetReport(target, label, time.time(), time.time(), None, failure="target not found or inaccessible")
            )
            continue
        units, roots = plan_units(target, config.split_depth)
        state = _TargetState(target, label, len(units), roots, reducer_limits, scan_ts)
        states.append(state)
        unit_groups.append(units)
        LOG.info("planned %s: %d units, %d root folders", target, len(units), roots)

    by_target = {s.target: s for s in states}
    pending: deque[WorkUnit] = deque(_round_robin(unit_groups))
    in_flight: dict[Future[PartialResult], tuple[_TargetState, UnitStats]] = {}
    interrupted = False
    last_report = 0.0
    memory_limit = config.memory_limit_mb * 1024 * 1024

    def emit(force: bool = False) -> None:
        nonlocal last_report
        now = time.monotonic()
        if on_progress is None or not (force or now - last_report >= config.progress_interval):
            return
        last_report = now
        rss = current_rss_bytes()
        on_progress([s.snapshot(rss) for s in states])

    def reduce(future: Future[PartialResult]) -> None:
        state, stats = in_flight.pop(future)
        try:
            partial = future.result()
        except Exception:  # noqa: BLE001
            LOG.exception("worker crashed for target %s", state.target)
            state.result.errors["worker_failure"] += 1
        else:
            state.result.merge(partial)
            state.done_files += partial.total_files
            state.done_bytes += partial.total_bytes
        state.live.remove(stats)
        state.units_done += 1
        if state.finished:
            state.end = time.time()

    try:
        with ThreadPoolExecutor(max_workers=config.workers, thread_name_prefix="sfc-worker") as pool:
            try:
                while pending or in_flight:
                    while pending and len(in_flight) < config.workers and not cancel.is_set():
                        unit = pending.popleft()
                        state = by_target[unit.target]
                        stats = UnitStats()
                        state.live.append(stats)
                        future = pool.submit(scan_unit, unit, worker_limits, scan_ts, stats, cancel, pressure)
                        in_flight[future] = (state, stats)
                    if not in_flight:
                        break  # cancelled and drained
                    done, _ = wait(list(in_flight), timeout=1.0, return_when=FIRST_COMPLETED)
                    for future in done:
                        reduce(future)

                    if memory_limit > 0:
                        rss = current_rss_bytes()
                        if rss > memory_limit and not pressure.is_set():
                            LOG.warning("memory pressure: RSS %d MB > limit %d MB", rss >> 20, config.memory_limit_mb)
                            pressure.set()
                            for state in states:
                                state.result.shrink()
                        elif pressure.is_set() and rss < memory_limit * 0.85:
                            pressure.clear()
                    emit()
            except KeyboardInterrupt:
                interrupted = True
                cancel.set()
                LOG.warning("interrupted: waiting for workers to stop")
                for future in list(in_flight):
                    try:
                        future.result()
                    except Exception:  # noqa: BLE001
                        pass
                    reduce(future)
    finally:
        if cancel.is_set():
            interrupted = True
        end = time.time()
        for state in states:
            if not state.end:
                state.end = end
        emit(force=True)

    for state in states:
        reports.append(
            TargetReport(
                target=state.target,
                label=state.label,
                start=state.start,
                end=state.end,
                result=state.result,
                roots_total=state.units_total,
                roots_done=state.units_done,
                interrupted=interrupted and not state.finished,
            )
        )
    return reports
