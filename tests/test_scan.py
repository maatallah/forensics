"""Enumerator, scanner, serialization and CLI tests."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from cli.main import main
from collector.enumerator import classify_error, walk_files
from collector.models import MB, ScanConfig, format_size
from collector.scanner import plan_units, run_scan


def make_tree(root: Path) -> int:
    """Create a small tree; return the total byte count."""
    layout = {
        "a/one.txt": 10,
        "a/two.TXT": 20,
        "a/deep/three.bin": 3 * MB,
        "b/four.bin": 3 * MB,
        "b/noext": 5,
        "top.log": 7,
    }
    total = 0
    for rel, size in layout.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x" * size)
        total += size
    return total


def test_walk_files_streams_all_and_reports_errors(tmp_path: Path) -> None:
    total = make_tree(tmp_path)
    errors: list[str] = []
    seen = list(walk_files(str(tmp_path), lambda p, c, e: errors.append(c)))
    assert len(seen) == 6
    assert sum(s for _, _, s, _ in seen) == total
    assert errors == []

    list(walk_files(str(tmp_path / "missing"), lambda p, c, e: errors.append(c)))
    assert errors == ["removed_during_scan"]


def test_walk_non_recursive(tmp_path: Path) -> None:
    make_tree(tmp_path)
    seen = list(walk_files(str(tmp_path), lambda *_: None, recursive=False))
    assert [n for _, n, _, _ in seen] == ["top.log"]


def test_classify_error() -> None:
    assert classify_error(PermissionError()) == "access_denied"
    assert classify_error(FileNotFoundError()) == "removed_during_scan"
    assert classify_error(OSError(36, "too long")) == "path_too_long" or os.name == "nt"
    assert classify_error(OSError(5, "io")) == "other_error"


def test_plan_units(tmp_path: Path) -> None:
    make_tree(tmp_path)
    units, roots = plan_units(str(tmp_path), 1)
    assert roots == 2
    assert [u.recursive for u in units] == [False, True, True]


@pytest.mark.parametrize("workers", [1, 4])
def test_full_scan_and_exports(tmp_path: Path, workers: int) -> None:
    root = tmp_path / "data"
    total = make_tree(root)
    out = tmp_path / "reports"
    config = ScanConfig(
        targets=(str(root),), workers=workers, top_files=2,
        min_duplicate_size_mb=1, output_dir=str(out), progress_interval=0.01,
    )
    snaps = []
    (report,) = run_scan(config, snaps.append)
    result = report.result
    assert result is not None
    assert result.total_files == 6
    assert result.total_bytes == total
    assert [t[0] for t in result.top_files.items_desc()] == [3 * MB, 3 * MB]
    assert result.duplicates.groups()[0][:2] == (3 * MB, 2)
    assert dict((e, f) for e, f, _ in result.aggregates.extensions.items())[".txt"] == 2
    assert sum(result.ages.files) == 6
    assert snaps and snaps[-1][0].finished

    from collector.serialization import report_prefix, write_reports

    paths = write_reports(report, out)
    names = sorted(p.name for p in paths)
    label = report_prefix(report)
    assert names == sorted(f"{label}_{s}" for s in (
        "Files.tsv", "Directories.tsv", "Extensions.tsv", "AgeBuckets.tsv", "Duplicates.tsv", "Summary.txt"))
    dups = (out / f"{label}_Duplicates.tsv").read_text(encoding="utf-8").splitlines()
    assert dups[0] == "SizeBytes\tSizeHuman\tCount\tPath"
    assert len(dups) == 3 and dups[1].split("\t")[:3] == [str(3 * MB), "3.00 MB", "2"]
    age = (out / f"{label}_AgeBuckets.tsv").read_text(encoding="utf-8").splitlines()
    assert age[0] == "Bucket\tFiles\tBytes\tHumanSize" and age[1].startswith("<30 days\t6\t")
    summary = (out / f"{label}_Summary.txt").read_text(encoding="utf-8")
    for key in ("Scan Start", "Scan End", "Duration", "Files Scanned", "Total Size", "Largest File",
                "Largest Directory", "Top 20 Directories", "Top 20 Extensions", "Age Distribution",
                "Duplicate Candidates"):
        assert key in summary


def test_missing_target_is_reported_not_fatal(tmp_path: Path) -> None:
    good = tmp_path / "g"
    make_tree(good)
    reports = run_scan(ScanConfig(targets=(str(tmp_path / "nope"), str(good)), workers=2))
    assert reports[0].failure and reports[0].result is None
    assert reports[1].result is not None and reports[1].result.total_files == 6


def test_cli_scan(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = tmp_path / "data"
    make_tree(root)
    out = tmp_path / "out"
    code = main(["scan", "--targets", str(root), "--workers", "2", "--top-files", "5",
                 "--min-duplicate-size-mb", "1", "--output", str(out), "--quiet"])
    assert code == 0
    assert len(list(out.glob("*_Summary.txt"))) == 1
    assert main(["scan", "--targets", str(root), "--workers", "0", "--output", str(out)]) == 2


def test_format_size() -> None:
    assert format_size(0) == "0 B"
    assert format_size(1536) == "1.50 KB"
    assert format_size(5 * 1024**4) == "5.00 TB"
