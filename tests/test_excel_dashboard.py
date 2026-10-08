"""Tests for the optional Excel dashboard (``collector.excel_dashboard``)."""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

import pytest

import cli.main as cli_main
from cli.main import main
from collector.excel_dashboard import (
    SHEET_DASHBOARD,
    DashboardError,
    build_dashboard,
    human_size_fr,
    parse_summary,
    resolve_prefix,
    thousands_fr,
)
from collector.models import MB, ScanConfig
from collector.scanner import run_scan
from collector.serialization import write_reports

SHEETS = (
    "Tableau de bord", "Plus gros fichiers", "Répertoires", "Extensions",
    "Âge", "Doublons", "Résumé", "Données graphiques",
)


def make_tree(root: Path) -> None:
    """A tiny tree with two equal large files (duplicate candidate)."""
    for rel, size in {"a/one.txt": 10, "a/deep/two.bin": 3 * MB, "b/three.bin": 3 * MB}.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * size)


def scan_and_export(root: Path, out: Path) -> Path:
    """Run a scan and write the exports; return their common prefix."""
    (report,) = run_scan(ScanConfig(
        targets=(str(root),), workers=1, top_files=10,
        min_duplicate_size_mb=1, output_dir=str(out), progress_interval=0.01,
    ))
    paths = write_reports(report, out)
    return resolve_prefix(paths[0])


def parts(xlsx: Path) -> list[str]:
    with zipfile.ZipFile(xlsx) as zf:
        assert zf.testzip() is None
        return zf.namelist()


def workbook_xml(xlsx: Path) -> str:
    with zipfile.ZipFile(xlsx) as zf:
        return zf.read("xl/workbook.xml").decode("utf-8")


def shared_strings(xlsx: Path) -> str:
    with zipfile.ZipFile(xlsx) as zf:
        return zf.read("xl/sharedStrings.xml").decode("utf-8")


# ---------------------------------------------------------------- unit helpers
def test_resolve_prefix() -> None:
    assert resolve_prefix(r"reports\H_20261007-1529_Files.tsv") == Path(r"reports\H_20261007-1529")
    assert resolve_prefix(Path("reports/H_20261007-1529")) == Path("reports/H_20261007-1529")
    assert resolve_prefix("H_20261007-1529_Dashboard.xlsx") == Path("H_20261007-1529")
    assert resolve_prefix("H_20261007-1529_Summary.txt") == Path("H_20261007-1529")
    assert resolve_prefix("whatever.log") == Path("whatever.log")


def test_human_size_fr() -> None:
    assert human_size_fr(0) == "0 o"
    assert human_size_fr(1023) == "1023 o"
    assert human_size_fr(1024) == "1,00 Ko"
    assert human_size_fr(1536) == "1,50 Ko"
    assert human_size_fr(1024**3) == "1,00 Go"
    assert human_size_fr(3 * 1024**4) == "3,00 To"


def test_thousands_fr() -> None:
    assert thousands_fr(0) == "0"
    assert thousands_fr(1234567) == "1\u202f234\u202f567"


def test_parse_summary(tmp_path: Path) -> None:
    path = tmp_path / "S_Summary.txt"
    path.write_text(
        "Storage Forensics Collector - Résumé du scan\n"
        "============================================\n"
        "Cible :                 D:\\\n"
        "Scan Start :            2026-10-07T10:50:00+01:00\n"
        "Files Scanned :         10,620,000\n"
        "Statut :                INTERROMPU (résultats partiels)\n"
        "Top 20 Directories (taille directe)\n"
        "  1     142.52 GB             3 fichiers  H:\\data\n",
        encoding="utf-8",
    )
    values = parse_summary(path)
    assert values["Cible"] == "D:\\"
    assert values["Files Scanned"] == "10,620,000"
    assert values["Statut"].startswith("INTERROMPU")
    # A path containing a colon must not be mistaken for a key/value line.
    assert "Top 20 Directories (taille directe)" not in values
    assert parse_summary(tmp_path / "absent.txt") == {}


# ------------------------------------------------------------------ building
def test_build_dashboard_end_to_end(tmp_path: Path) -> None:
    root, out = tmp_path / "data", tmp_path / "reports"
    make_tree(root)
    prefix = scan_and_export(root, out)

    xlsx = build_dashboard(prefix)
    assert xlsx == Path(f"{prefix}_Dashboard.xlsx")
    assert xlsx.is_file()

    names = parts(xlsx)
    assert "xl/workbook.xml" in names
    assert sum(n.startswith("xl/charts/chart") for n in names) == 4
    assert sum(n.startswith("xl/tables/table") for n in names) == 5

    book = workbook_xml(xlsx)
    for sheet in SHEETS:
        assert f'name="{sheet}"' in book
    assert f'name="{SHEET_DASHBOARD}"' in book
    strings = shared_strings(xlsx)
    assert "FICHIERS SCANNÉS" in strings
    assert "plus gros répertoires sur" not in strings  # not truncated at default cap


def test_build_dashboard_row_cap(tmp_path: Path) -> None:
    root, out = tmp_path / "data", tmp_path / "reports"
    make_tree(root)
    prefix = scan_and_export(root, out)

    xlsx = build_dashboard(prefix, max_rows=1)
    assert "plus gros répertoires sur" in shared_strings(xlsx)


def test_build_dashboard_from_any_export_file(tmp_path: Path) -> None:
    root, out = tmp_path / "data", tmp_path / "reports"
    make_tree(root)
    prefix = scan_and_export(root, out)

    xlsx = build_dashboard(Path(f"{prefix}_Files.tsv"), output=tmp_path / "custom.xlsx")
    assert xlsx == tmp_path / "custom.xlsx"
    assert xlsx.is_file()


def test_build_dashboard_without_summary(tmp_path: Path) -> None:
    """Header-only exports and no Summary.txt must still produce a workbook."""
    prefix = tmp_path / "X_20261007-0000"
    for name, header in {
        "Files.tsv": "Rank\tSizeBytes\tHumanSize\tLastModified\tExtension\tPath",
        "Directories.tsv": "Path\tFiles\tBytes\tHumanSize",
        "Extensions.tsv": "Extension\tFiles\tBytes\tHumanSize\tPercentBytes",
        "AgeBuckets.tsv": "Bucket\tFiles\tBytes\tHumanSize",
        "Duplicates.tsv": "SizeBytes\tSizeHuman\tCount\tPath",
    }.items():
        Path(f"{prefix}_{name}").write_text(header + "\n", encoding="utf-8")

    xlsx = build_dashboard(prefix)
    assert xlsx.is_file()
    strings = shared_strings(xlsx)
    assert "Summary.txt introuvable" in strings
    assert "aucune donnée" in strings  # empty chart boxes


def test_build_dashboard_missing_exports(tmp_path: Path) -> None:
    with pytest.raises(DashboardError, match="exports introuvables"):
        build_dashboard(tmp_path / "nope")


def test_build_dashboard_without_xlsxwriter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "xlsxwriter", None)
    with pytest.raises(DashboardError, match="XlsxWriter est requis"):
        build_dashboard(tmp_path / "nope")


# ----------------------------------------------------------------------- CLI
def test_cli_scan_excel(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root, out = tmp_path / "data", tmp_path / "out"
    make_tree(root)
    code = main(["scan", "--targets", str(root), "--output", str(out), "--excel", "--quiet"])
    capsys.readouterr()
    assert code == 0
    assert len(list(out.glob("*_Dashboard.xlsx"))) == 1
    assert len(list(out.glob("*_Summary.txt"))) == 1


def test_cli_scan_excel_failure_keeps_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A dashboard failure never changes the scan exit code."""
    root, out = tmp_path / "data", tmp_path / "out"
    make_tree(root)

    def boom(*_args: object, **_kwargs: object) -> Path:
        raise DashboardError("boom")

    monkeypatch.setattr(cli_main, "build_dashboard", boom)
    code = main(["scan", "--targets", str(root), "--output", str(out), "--excel", "--quiet"])
    captured = capsys.readouterr()
    assert code == 0
    assert "boom" in captured.err


def test_cli_dashboard_from_directory(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root, out = tmp_path / "data", tmp_path / "out"
    make_tree(root)
    assert main(["scan", "--targets", str(root), "--output", str(out), "--quiet"]) == 0
    capsys.readouterr()

    assert main(["dashboard", str(out)]) == 0
    assert "wrote " in capsys.readouterr().out
    assert len(list(out.glob("*_Dashboard.xlsx"))) == 1


def test_cli_dashboard_from_prefix(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The documented form: a bare prefix that does not exist as a path."""
    root, out = tmp_path / "data", tmp_path / "out"
    make_tree(root)
    assert main(["scan", "--targets", str(root), "--output", str(out), "--quiet"]) == 0
    capsys.readouterr()

    prefix = resolve_prefix(next(out.glob("*_Files.tsv")))
    assert not prefix.exists()  # the prefix is only a file-name stem

    assert main(["dashboard", str(prefix), "--output", str(tmp_path / "out" / "synthese.xlsx")]) == 0
    capsys.readouterr()
    assert (tmp_path / "out" / "synthese.xlsx").is_file()


def test_cli_dashboard_errors(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # Unknown source -> invalid argument.
    assert main(["dashboard", str(tmp_path / "missing")]) == 2
    assert "source introuvable" in capsys.readouterr().err

    # Directory without any export -> invalid argument.
    assert main(["dashboard", str(tmp_path)]) == 2
    assert "aucun export de scan" in capsys.readouterr().err

    # Several scans in one directory -> the prefix must be given.
    (tmp_path / "a_Files.tsv").write_text("Rank\n", encoding="utf-8")
    (tmp_path / "b_Files.tsv").write_text("Rank\n", encoding="utf-8")
    assert main(["dashboard", str(tmp_path)]) == 2
    assert "précisez un préfixe" in capsys.readouterr().err

    # Complete prefix but incomplete exports -> build failure.
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "c_Files.tsv").write_text("Rank\n", encoding="utf-8")
    assert main(["dashboard", str(broken)]) == 1
    assert "exports introuvables" in capsys.readouterr().err
