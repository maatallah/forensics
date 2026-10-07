"""TSV / text report writers (UTF-8, TAB separated, ``\\n`` line endings)."""

from __future__ import annotations

import heapq
import re
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path

from .models import PartialResult, TargetReport, format_size

_TRANSLATE = str.maketrans({"\t": " ", "\r": " ", "\n": " "})
_LABEL_RE = re.compile(r"[^A-Za-z0-9._-]+")


def target_label(target: str) -> str:
    r"""Derive a file-name prefix from a target (``D:\`` -> ``D``)."""
    return _LABEL_RE.sub("_", target).strip("_") or "root"


def report_prefix(report: TargetReport) -> str:
    """File-name prefix of a report: ``<label>_<YYYYMMDD-HHMM>`` (scan start, local time)."""
    return f"{report.label}_{datetime.fromtimestamp(report.start):%Y%m%d-%H%M}"


def _clean(value: str) -> str:
    return value.translate(_TRANSLATE)


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")


def format_duration(seconds: float) -> str:
    """Format seconds as ``HH:MM:SS``."""
    seconds = int(max(seconds, 0))
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def _write_tsv(path: Path, header: tuple[str, ...], rows: Iterable[Iterable[object]]) -> None:
    """Stream rows to disk; nothing is materialised."""
    with open(path, "w", encoding="utf-8", errors="replace", newline="\n") as fh:
        fh.write("\t".join(header) + "\n")
        for row in rows:
            fh.write("\t".join(_clean(str(c)) for c in row) + "\n")


def write_files(path: Path, result: PartialResult) -> None:
    """``Files.tsv``: the Top-N largest files."""
    rows = (
        (rank, size, format_size(size), _iso(mtime), _extension_of(p), p)
        for rank, (size, p, mtime) in enumerate(result.top_files.items_desc(), start=1)
    )
    _write_tsv(path, ("Rank", "SizeBytes", "HumanSize", "LastModified", "Extension", "Path"), rows)


def _extension_of(path: str) -> str:
    name = path.rsplit("\\", 1)[-1].rsplit("/", 1)[-1]
    dot = name.rfind(".")
    return name[dot:].lower() if 0 < dot < len(name) - 1 else "<sans_extension>"


def write_directories(path: Path, result: PartialResult) -> None:
    """``Directories.tsv``: per-directory direct totals, largest first."""
    ordered = sorted(result.aggregates.directories.items(), key=lambda t: t[2], reverse=True)
    rows = ((p, files, size, format_size(size)) for p, files, size in ordered)
    _write_tsv(path, ("Path", "Files", "Bytes", "HumanSize"), rows)


def write_extensions(path: Path, result: PartialResult) -> None:
    """``Extensions.tsv``: per-extension totals, largest first."""
    total = max(result.total_bytes, 1)
    rows = (
        (ext or "<sans_extension>", files, size, format_size(size), f"{size * 100 / total:.2f}")
        for ext, files, size in result.aggregates.extensions.sorted_by_bytes()
    )
    _write_tsv(path, ("Extension", "Files", "Bytes", "HumanSize", "PercentBytes"), rows)


def write_age_buckets(path: Path, result: PartialResult) -> None:
    """``AgeBuckets.tsv``."""
    rows = ((label, files, size, format_size(size)) for label, files, size in result.ages.rows())
    _write_tsv(path, ("Bucket", "Files", "Bytes", "HumanSize"), rows)


def write_duplicates(path: Path, result: PartialResult) -> None:
    """``Duplicates.tsv``: one row per path of every candidate group."""

    def rows() -> Iterable[tuple[int, str, int, str]]:
        for size, count, paths in result.duplicates.groups():
            human = format_size(size)
            for p in paths:
                yield size, human, count, p

    _write_tsv(path, ("SizeBytes", "SizeHuman", "Count", "Path"), rows())


def build_summary(report: TargetReport) -> str:
    """Render ``Summary.txt`` for one target in French."""
    result = report.result
    assert result is not None
    duration = max(report.end - report.start, 0.0)
    lines: list[str] = []
    add = lines.append

    add("Storage Forensics Collector - Résumé du scan")
    add("=" * 44)
    add(f"Cible :                 {report.target}")
    add(f"Scan Start :            {_iso(report.start)}")
    add(f"Scan End :              {_iso(report.end)}")
    add(f"Duration :              {format_duration(duration)} ({duration:.1f} s)")
    add(f"Files Scanned :         {result.total_files:,}")
    add(f"Total Size :            {format_size(result.total_bytes)} ({result.total_bytes:,} octets)")
    if duration > 0:
        add(f"Débit :                 {result.total_files / duration:,.0f} fichiers/s, "
            f"{result.total_bytes / duration / 1048576:,.1f} Mo/s")
    if report.interrupted:
        add("Statut :                INTERROMPU (résultats partiels)")

    largest = result.top_files.largest()
    add("")
    if largest:
        add(f"Largest File :          {format_size(largest[0])} ({largest[0]:,} octets)  {largest[1]}")
    else:
        add("Largest File :          n/a")
    top_dirs = result.aggregates.directories.top(20)
    if top_dirs:
        p, files, size = top_dirs[0]
        add(f"Largest Directory :     {format_size(size)} ({files:,} fichiers)  {p}")
    else:
        add("Largest Directory :     n/a")

    add("")
    add("Top 20 Directories (taille directe)")
    add("-" * 35)
    for i, (p, files, size) in enumerate(top_dirs, start=1):
        add(f"{i:>3}  {format_size(size):>12}  {files:>12,} fichiers  {p}")

    add("")
    add("Top 20 Extensions")
    add("-" * 17)
    top_ext = heapq.nlargest(20, result.aggregates.extensions.items(), key=lambda t: t[2])
    for i, (ext, files, size) in enumerate(top_ext, start=1):
        add(f"{i:>3}  {(ext or '<sans_extension>'):<18} {format_size(size):>12}  {files:>12,} fichiers")

    add("")
    add("Age Distribution (dernière modification)")
    add("-" * 40)
    for label, files, size in result.ages.rows():
        add(f"     {label:<14} {files:>14,} fichiers  {format_size(size):>12}")

    groups = result.duplicates.groups()
    reclaimable = sum(s * (c - 1) for s, c, _ in groups)
    add("")
    add("Duplicate Candidates (même taille, contenu NON vérifié)")
    add("-" * 55)
    add(f"     Groupes :                  {len(groups):,}")
    add(f"     Fichiers dans groupes :    {sum(c for _, c, _ in groups):,}")
    add(f"     Potentiel récupérable :    {format_size(reclaimable)} ({reclaimable:,} octets)")

    add("")
    add("Diagnostics")
    add("-" * 11)
    errors = result.errors
    add(f"     Accès refusés :            {errors.get('access_denied', 0):,}")
    add(f"     Chemins trop longs :       {errors.get('path_too_long', 0):,}")
    add(f"     Supprimés pendant scan :   {errors.get('removed_during_scan', 0):,}")
    add(f"     Autres erreurs :           {errors.get('other_error', 0) + errors.get('worker_failure', 0):,}")
    add(f"     Compactages répertoires :  {result.aggregates.directories.compactions:,}")
    add(f"     Débordement extensions :   {'oui' if result.aggregates.extensions.overflowed else 'non'}")
    add(f"     Tailles doublons purgées/ignorées : {result.duplicates.purged:,} / {result.duplicates.dropped:,}")
    return "\n".join(lines) + "\n"


def write_summary(path: Path, report: TargetReport) -> None:
    """``Summary.txt``."""
    path.write_text(build_summary(report), encoding="utf-8", errors="replace", newline="\n")


def write_reports(report: TargetReport, output_dir: str | Path) -> list[Path]:
    """Write the six mandatory exports of one target and return their paths."""
    if report.result is None:
        return []
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    prefix = report_prefix(report)
    written = {
        "Files.tsv": write_files,
        "Directories.tsv": write_directories,
        "Extensions.tsv": write_extensions,
        "AgeBuckets.tsv": write_age_buckets,
        "Duplicates.tsv": write_duplicates,
    }
    paths: list[Path] = []
    for suffix, writer in written.items():
        target = out / f"{prefix}_{suffix}"
        writer(target, report.result)
        paths.append(target)
    summary = out / f"{prefix}_Summary.txt"
    write_summary(summary, report)
    paths.append(summary)
    return paths
