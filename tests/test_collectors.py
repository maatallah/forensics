"""Unit tests for aggregators, age analysis and duplicate detection."""

from __future__ import annotations

import random

from collector.age_analysis import DAY, BUCKET_LABELS, AgeBuckets
from collector.aggregators import DirectoryTotals, ExtensionTotals, TopFiles
from collector.duplicate_detector import DuplicateDetector


def test_top_files_keeps_only_n_largest() -> None:
    rng = random.Random(1)
    sizes = [rng.randrange(1, 10**9) for _ in range(5000)]
    top = TopFiles(10)
    for i, s in enumerate(sizes):
        if s > top.threshold:
            top.offer(s, f"f{i}", 0.0)
    assert len(top) == 10
    assert [t[0] for t in top.items_desc()] == sorted(sizes, reverse=True)[:10]
    assert top.largest()[0] == max(sizes)


def test_top_files_zero_capacity_and_merge() -> None:
    assert TopFiles(0).threshold > 10**12
    a, b = TopFiles(3), TopFiles(3)
    for i in range(5):
        a.offer(i, f"a{i}", 0.0)
        b.offer(i + 10, f"b{i}", 0.0)
    a.merge(b)
    assert [t[0] for t in a.items_desc()] == [14, 13, 12]


def test_directory_compaction_preserves_totals() -> None:
    d = DirectoryTotals(100)
    expected_files = expected_bytes = 0
    for i in range(2000):
        d.add(f"/root/a{i % 40}/b{i}", i + 1)
        expected_files += 1
        expected_bytes += i + 1
    assert len(d) <= 200
    assert d.compactions > 0
    assert sum(f for _, f, _ in d.items()) == expected_files
    assert sum(b for _, _, b in d.items()) == expected_bytes


def test_directory_merge_and_top() -> None:
    a, b = DirectoryTotals(1000), DirectoryTotals(1000)
    a.add("/x", 5)
    b.add("/x", 7)
    b.add("/y", 100)
    a.merge(b)
    assert a.top(1) == [("/y", 1, 100)]
    assert dict((p, s) for p, _, s in a.items())["/x"] == 12


def test_extension_overflow_goes_to_other() -> None:
    e = ExtensionTotals(2)
    for ext in (".a", ".b", ".c", ".d"):
        e.add(ext, 10)
    data = {x: (f, s) for x, f, s in e.items()}
    assert e.overflowed
    assert data["<autres>"] == (2, 20)
    assert sum(f for f, _ in data.values()) == 4


def test_age_buckets_boundaries() -> None:
    now = 1_000_000_000.0
    b = AgeBuckets(now)
    for days in (0, 29, 30, 89, 90, 364, 365, 1094, 1095, 5000, -3):
        b.add(now - days * DAY, 1)
    assert b.files == [3, 2, 2, 2, 2]
    assert [r[0] for r in b.rows()] == list(BUCKET_LABELS)


def test_duplicates_group_by_size_and_threshold() -> None:
    d = DuplicateDetector(min_size=100, max_paths=2)
    d.offer(50, "small1")
    d.offer(50, "small2")
    for p in ("a", "b", "c"):
        d.offer(200, p)
    d.offer(300, "solo")
    groups = d.groups()
    assert groups == [(200, 3, ["a", "b"])]


def test_duplicates_purge_and_merge() -> None:
    d = DuplicateDetector(min_size=1, max_sizes=8)
    for size in range(1, 30):
        d.offer(size, f"p{size}")
    assert len(d) <= 8
    assert d.purged > 0
    other = DuplicateDetector(min_size=1, max_sizes=100)
    other.offer(29, "q")
    d.merge(other)
    assert any(size == 29 and count == 2 for size, count, _ in d.groups())
