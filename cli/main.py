"""``sfcollect`` command line entry point."""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence
from pathlib import Path

from collector import __version__
from collector.models import ProgressSnapshot, ScanConfig, format_size
from collector.scanner import run_scan
from collector.serialization import format_duration, write_reports


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser."""
    parser = argparse.ArgumentParser(
        prog="sfcollect",
        description="Storage Forensics Collector - fast streaming scan of local, SAN, NAS, SMB and DFS storage.",
        epilog=(
            "example:\n  sfcollect scan --targets D:\\ R:\\ \\\\server\\finance --workers 8 "
            "--top-files 1000 --min-duplicate-size-mb 100 --output reports\n\n"
            "outputs are named <Target>_<YYYYMMDD-HHMM>_<Report> (e.g. D_20261007-1050_Files.tsv)"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"sfcollect {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    scan = sub.add_parser("scan", help="scan one or more targets", description="Scan one or more targets and write TSV reports + Summary.txt.", formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    scan.add_argument("--targets", nargs="+", required=True, metavar="PATH",
                      help=r"roots to scan, e.g. D:\ R:\ \\server\finance")
    scan.add_argument("--workers", type=int, default=8, help="parallel workers (default 8)")
    scan.add_argument("--top-files", type=int, default=1000, help="size of the Top-N largest files (default 1000)")
    scan.add_argument("--min-duplicate-size-mb", type=float, default=100.0,
                      help="minimum size (MB) for duplicate candidates (default 100)")
    scan.add_argument("--output", default="reports", help="output directory (default: reports)")
    scan.add_argument("--split-depth", type=int, default=1,
                      help="directory depth at which targets are split into work units (default 1)")
    scan.add_argument("--memory-limit-mb", type=int, default=500,
                      help="soft RSS limit triggering purges, 0 disables (default 500)")
    scan.add_argument("--max-directories", type=int, default=300_000,
                      help="max tracked directories before roll-up (default 300000)")
    scan.add_argument("--progress-interval", type=float, default=5.0, help="seconds between progress lines")
    scan.add_argument("--quiet", action="store_true", help="disable progress output")
    scan.add_argument("--log-file", default=None, help="log file (default: <output>/sfc.log)")
    return parser


def format_progress(s: ProgressSnapshot) -> str:
    """Render one progress line."""
    eta = "n/a" if s.eta_seconds is None else format_duration(s.eta_seconds)
    state = "DONE" if s.finished else "RUN "
    return (
        f"[{state}] Target {s.target} | Root folders {s.roots_done}/{s.roots_total} | "
        f"Files Scanned {s.files:,} | Data Volume {format_size(s.bytes)} | "
        f"Elapsed {format_duration(s.elapsed)} | {s.files_per_sec:,.0f} files/s | "
        f"{s.mb_per_sec:,.1f} MB/s | ETA {eta} | RSS {s.rss_bytes >> 20} MB"
    )


def _print_progress(snapshots: list[ProgressSnapshot]) -> None:
    for snap in snapshots:
        print(format_progress(snap), file=sys.stderr, flush=True)


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point. Returns the process exit code (0 ok, 1 partial failure, 130 interrupted)."""
    args = build_parser().parse_args(argv)
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)

    handler = logging.FileHandler(args.log_file or out / "sfc.log", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s\t%(levelname)s\t%(threadName)s\t%(message)s"))
    root = logging.getLogger("sfc")
    root.setLevel(logging.INFO)
    root.addHandler(handler)

    try:
        config = ScanConfig(
            targets=tuple(args.targets),
            workers=args.workers,
            top_files=args.top_files,
            min_duplicate_size_mb=args.min_duplicate_size_mb,
            output_dir=args.output,
            split_depth=args.split_depth,
            max_directories=args.max_directories,
            memory_limit_mb=args.memory_limit_mb,
            progress_interval=args.progress_interval,
        )
    except ValueError as exc:
        print(f"sfcollect: invalid argument: {exc}", file=sys.stderr)
        return 2

    reports = run_scan(config, None if args.quiet else _print_progress)

    exit_code = 0
    for report in reports:
        if report.failure:
            print(f"sfcollect: {report.target}: {report.failure}", file=sys.stderr)
            exit_code = 1
            continue
        for path in write_reports(report, out):
            print(f"wrote {path}")
        if report.interrupted:
            exit_code = 130
    root.removeHandler(handler)
    handler.close()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
