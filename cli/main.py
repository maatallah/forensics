"""``sfcollect`` command line entry point."""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence
from pathlib import Path

from collector import __version__
from collector.excel_dashboard import DEFAULT_MAX_ROWS, DashboardError, build_dashboard, resolve_prefix
from collector.models import ProgressSnapshot, ScanConfig, format_size
from collector.scanner import run_scan
from collector.serialization import format_duration, write_reports


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser."""
    parser = argparse.ArgumentParser(
        prog="sfcollect",
        description="Storage Forensics Collector - scan streaming haute performance pour stockages local, SAN, NAS, SMB et DFS.",
        epilog=(
            "exemple :\n  sfcollect scan --targets D:\\ R:\\ \\\\serveur\\finance --workers 8 "
            "--top-files 1000 --min-duplicate-size-mb 100 --output reports\n"
            "  sfcollect scan --targets D:\\ --excel\n\n"
            "tableau de bord à partir d'exports existants :\n"
            "  sfcollect dashboard reports\\D_20261007-1050\n\n"
            "les exports sont nommés <Cible>_<AAAAMMJJ-HHMM>_<Rapport> (ex. D_20261007-1050_Files.tsv)"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"sfcollect {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    scan = sub.add_parser(
        "scan",
        help="scanner une ou plusieurs cibles",
        description="Scanner une ou plusieurs cibles et générer les rapports TSV + Summary.txt.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    scan.add_argument(
        "--targets",
        nargs="+",
        required=True,
        metavar="CHEMIN",
        help=r"racines à scanner, ex. D:\ R:\ \\serveur\partage",
    )
    scan.add_argument("--workers", type=int, default=8, help="nombre de workers parallèles")
    scan.add_argument("--top-files", type=int, default=1000, help="taille du Top-N des plus gros fichiers")
    scan.add_argument(
        "--min-duplicate-size-mb",
        type=float,
        default=100.0,
        help="taille minimale (Mo) pour les candidats doublons",
    )
    scan.add_argument("--output", default="reports", help="dossier de destination des exports")
    scan.add_argument(
        "--split-depth",
        type=int,
        default=1,
        help="profondeur de découpage des cibles en unités de travail",
    )
    scan.add_argument(
        "--memory-limit-mb",
        type=int,
        default=500,
        help="limite RSS logicielle (Mo) déclenchant la purge, 0 désactive",
    )
    scan.add_argument(
        "--max-directories",
        type=int,
        default=300_000,
        help="nombre max de répertoires suivis avant roll-up parent",
    )
    scan.add_argument(
        "--progress-interval",
        type=float,
        default=5.0,
        help="intervalle en secondes entre deux affichages de progression",
    )
    scan.add_argument("--quiet", action="store_true", help="désactiver l'affichage de progression")
    scan.add_argument("--log-file", default=None, help="fichier de log (défaut : <output>/sfc.log)")
    scan.add_argument(
        "--excel",
        action="store_true",
        help="générer <Préfixe>_Dashboard.xlsx après les exports (nécessite XlsxWriter)",
    )

    dash = sub.add_parser(
        "dashboard",
        help="construire un tableau de bord Excel à partir d'exports existants",
        description=(
            "Construire <Préfixe>_Dashboard.xlsx à partir des exports TSV d'un scan "
            "déjà réalisé (aucun rescanner). Nécessite XlsxWriter."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    dash.add_argument(
        "source",
        metavar="SOURCE",
        help=r"préfixe d'exports, fichier d'export ou dossier, ex. reports\H_20261007-1529",
    )
    dash.add_argument("--output", default=None, help="fichier .xlsx de destination (défaut : <préfixe>_Dashboard.xlsx)")
    dash.add_argument(
        "--max-rows",
        type=int,
        default=DEFAULT_MAX_ROWS,
        help="nombre maximal de lignes par feuille volumineuse",
    )
    return parser


def format_progress(s: ProgressSnapshot) -> str:
    """Render one progress line in French."""
    eta = "n/a" if s.eta_seconds is None else format_duration(s.eta_seconds)
    state = "TERM" if s.finished else "SCAN"
    return (
        f"[{state}] Cible {s.target} | Dossiers racines {s.roots_done}/{s.roots_total} | "
        f"Fichiers scannés {s.files:,} | Volume données {format_size(s.bytes)} | "
        f"Écoulé {format_duration(s.elapsed)} | {s.files_per_sec:,.0f} fichiers/s | "
        f"{s.mb_per_sec:,.1f} Mo/s | ETA {eta} | RSS {s.rss_bytes >> 20} Mo"
    )


def _print_progress(snapshots: list[ProgressSnapshot]) -> None:
    for snap in snapshots:
        print(format_progress(snap), file=sys.stderr, flush=True)


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point. Returns the exit code (0 ok, 1 failure, 2 bad argument, 130 interrupted)."""
    args = build_parser().parse_args(argv)
    if args.command == "dashboard":
        return _dashboard_command(args)
    return _scan_command(args)


def _dashboard_command(args: argparse.Namespace) -> int:
    """Build a dashboard from existing exports. Exit codes: 0 ok, 1 build failed, 2 bad source."""
    try:
        prefix = _resolve_dashboard_source(args.source)
    except DashboardError as exc:
        print(f"sfcollect: {exc}", file=sys.stderr)
        return 2
    try:
        path = build_dashboard(prefix, args.output, args.max_rows)
    except DashboardError as exc:
        print(f"sfcollect: {exc}", file=sys.stderr)
        return 1
    print(f"wrote {path}")
    return 0


def _resolve_dashboard_source(source: str) -> Path:
    """Return the export prefix of ``SOURCE`` (prefix, export file, or directory of exports)."""
    path = Path(source)
    if path.is_dir():
        prefixes = sorted({resolve_prefix(found) for found in path.glob("*_Files.tsv")})
        if not prefixes:
            raise DashboardError(f"aucun export de scan dans {path}")
        if len(prefixes) > 1:
            names = ", ".join(p.name for p in prefixes[:3])
            more = " …" if len(prefixes) > 3 else ""
            raise DashboardError(
                f"{path} contient {len(prefixes)} scans ({names}{more}) : précisez un préfixe"
            )
        return prefixes[0]
    # A bare prefix (``reports\H_20261007-1050``) is not a path on disk: look for its exports.
    prefix = resolve_prefix(path)
    if path.exists() or Path(f"{prefix}_Files.tsv").is_file():
        return prefix
    raise DashboardError(f"source introuvable : {source}")


def _scan_command(args: argparse.Namespace) -> int:
    """Run ``sfcollect scan``. Returns 0 ok, 1 target failure, 2 bad argument, 130 interrupted."""
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
        paths = write_reports(report, out)
        for path in paths:
            print(f"wrote {path}")
        if args.excel and paths:
            try:
                dashboard = build_dashboard(resolve_prefix(paths[0]))
            except DashboardError as exc:
                # The scan itself succeeded: warn, but do not change the exit code.
                print(f"sfcollect: {report.target}: {exc}", file=sys.stderr)
            else:
                print(f"wrote {dashboard}")
        if report.interrupted:
            exit_code = 130
    root.removeHandler(handler)
    handler.close()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
