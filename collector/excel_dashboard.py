"""Tableau de bord Excel (optionnel, nécessite ``XlsxWriter``).

Le classeur est construit **à partir des exports TSV** (et de ``Summary.txt``
s'il existe). Il peut donc être produit juste après un scan
(``sfcollect scan ... --excel``) ou plus tard, sans rescanner
(``sfcollect dashboard reports\\H_20261007-1529``).

Choix techniques
----------------
* Les TSV sont lus en flux, ligne par ligne.
* Les feuilles potentiellement volumineuses (répertoires, doublons) sont
  plafonnées à ``max_rows`` lignes. Les TSV sont déjà triés par signifiance
  décroissante (taille pour fichiers/répertoires/extensions, octets récupérables
  pour les doublons) : ce sont donc les lignes les plus utiles qui sont gardées ;
  la liste complète reste dans le TSV. Cela borne la mémoire (XlsxWriter garde
  les cellules en mémoire jusqu'à l'écriture) et garde Excel réactif.
* Toutes les valeurs sont écrites typées (nombres, dates) : tri, filtres et
  graphiques natifs fonctionnent. Les chemins sont écrits comme texte brut
  (jamais interprétés comme formules ou URL).
* Les totaux (fichiers, octets) sont recalculés depuis ``Extensions.tsv``,
  qui est exact même après les purges mémoire du scan.
"""

from __future__ import annotations

import csv
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from . import __version__

GB: int = 1024**3
EXCEL_MAX_ROWS: int = 1_048_575
DEFAULT_MAX_ROWS: int = 100_000

REPORT_SUFFIXES: tuple[str, ...] = (
    "Files.tsv",
    "Directories.tsv",
    "Extensions.tsv",
    "AgeBuckets.tsv",
    "Duplicates.tsv",
    "Summary.txt",
    "Dashboard.xlsx",
)

SHEET_DASHBOARD = "Tableau de bord"
SHEET_FILES = "Plus gros fichiers"
SHEET_DIRS = "Répertoires"
SHEET_EXT = "Extensions"
SHEET_AGE = "Âge"
SHEET_DUP = "Doublons"
SHEET_SUMMARY = "Résumé"
SHEET_CHARTS = "Données graphiques"

NAVY = "#1F3864"
BLUE = "#2E75B6"
TEAL = "#00897B"
ORANGE = "#ED7D31"
RED = "#C00000"
GREEN = "#548235"
PURPLE = "#7030A0"
GREY = "#595959"
LIGHT = "#F2F5FA"
AGE_COLORS: tuple[str, ...] = ("#70AD47", "#A9D18E", "#FFC000", "#ED7D31", "#C00000")
PIE_COLORS: tuple[str, ...] = (
    "#1F3864", "#2E75B6", "#00897B", "#70AD47", "#FFC000",
    "#ED7D31", "#C00000", "#7030A0", "#A5A5A5",
)

TOP_EXTENSIONS_PIE = 8
TOP_DIRECTORIES_CHART = 15
TOP_EXTENSIONS_COUNT = 10
TOP_FILES_DASHBOARD = 10


class DashboardError(RuntimeError):
    """Le tableau de bord ne peut pas être produit (message en français)."""


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def resolve_prefix(source: str | Path) -> Path:
    r"""Return the export prefix from a prefix or any export file name.

    ``reports\H_20261007-1529_Files.tsv`` -> ``reports\H_20261007-1529``.
    """
    path = Path(source)
    for suffix in REPORT_SUFFIXES:
        tail = "_" + suffix
        if path.name.endswith(tail):
            return path.with_name(path.name[: -len(tail)])
    return path


def human_size_fr(num_bytes: float) -> str:
    """Taille lisible en unités françaises (1 Ko = 1024 o), virgule décimale."""
    units = ("o", "Ko", "Mo", "Go", "To", "Po", "Eo")
    value = float(num_bytes)
    unit = 0
    while value >= 1024.0 and unit < len(units) - 1:
        value /= 1024.0
        unit += 1
    if unit == 0:
        return f"{int(value)} o"
    return f"{value:.2f} {units[unit]}".replace(".", ",")


def thousands_fr(number: int) -> str:
    """``1234567`` -> ``1 234 567`` (espace fine insécable)."""
    return f"{number:,}".replace(",", "\u202f")


def _rows(path: Path) -> Iterator[list[str]]:
    """Stream the data rows of a TSV export (header skipped)."""
    with open(path, encoding="utf-8", errors="replace", newline="") as fh:
        reader = csv.reader(fh, delimiter="\t", quoting=csv.QUOTE_NONE)
        next(reader, None)
        for row in reader:
            if row:
                yield row


def _int(value: str) -> int:
    try:
        return int(value.replace(",", "").replace("\u202f", "").replace(" ", ""))
    except ValueError:
        return 0


_KEY_VALUE = re.compile(r"^\s*([^:\n]+?)\s*:\s+(.*\S)\s*$")


def parse_summary(path: Path) -> dict[str, str]:
    """Parse the ``Clé : valeur`` lines of ``Summary.txt`` (FR or EN)."""
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        match = _KEY_VALUE.match(line)
        if match:
            values.setdefault(match.group(1).strip(), match.group(2).strip())
    return values


def _pick(values: dict[str, str], *keys: str) -> str:
    for key in keys:
        if key in values:
            return values[key]
    return ""


def _parse_datetime(value: str) -> datetime | None:
    """ISO 8601 -> naive local wall time (Excel has no time zones)."""
    try:
        return datetime.fromisoformat(value.strip()).replace(tzinfo=None)
    except ValueError:
        return None


def _short_path(path: str, limit: int = 45) -> str:
    """Last two components of a path, left-truncated to ``limit`` chars."""
    parts = [p for p in re.split(r"[\\/]", path) if p]
    label = "\\".join(parts[-2:]) if len(parts) > 1 else path
    if len(label) > limit:
        label = "…" + label[-(limit - 1):]
    return label


def _left_truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else "…" + text[-(limit - 1):]


# --------------------------------------------------------------------------
# Builder
# --------------------------------------------------------------------------
@dataclass
class _Stats:
    total_files: int = 0
    total_bytes: int = 0
    extensions: list[tuple[str, int, int]] = field(default_factory=list)
    ages: list[tuple[str, int, int]] = field(default_factory=list)
    top_files: list[tuple[int, str]] = field(default_factory=list)
    top_dirs: list[tuple[str, int, int]] = field(default_factory=list)
    dir_rows: int = 0
    dir_written: int = 0
    dup_groups: int = 0
    dup_files: int = 0
    dup_reclaimable: int = 0
    dup_rows: int = 0
    dup_written: int = 0


class _Builder:
    """Fills one XlsxWriter workbook from the TSV exports."""

    def __init__(self, workbook: Any, prefix: Path, inputs: dict[str, Path], summary: dict[str, str],
                 max_rows: int) -> None:
        self.wb = workbook
        self.prefix = prefix
        self.inputs = inputs
        self.summary = summary
        self.max_rows = max_rows
        self.stats = _Stats()
        self.n_pie = 0
        self.n_age = 0
        self.n_dirs = 0
        self.n_count = 0
        self.fmt = self._formats()
        # Creation order = tab order.
        self.ws_dash = workbook.add_worksheet(SHEET_DASHBOARD)
        self.ws_files = workbook.add_worksheet(SHEET_FILES)
        self.ws_dirs = workbook.add_worksheet(SHEET_DIRS)
        self.ws_ext = workbook.add_worksheet(SHEET_EXT)
        self.ws_age = workbook.add_worksheet(SHEET_AGE)
        self.ws_dup = workbook.add_worksheet(SHEET_DUP)
        self.ws_sum = workbook.add_worksheet(SHEET_SUMMARY)
        self.ws_chart = workbook.add_worksheet(SHEET_CHARTS)

    # ------------------------------------------------------------------ util
    def _formats(self) -> dict[str, Any]:
        base = {"font_name": "Calibri", "font_size": 10}

        def make(**kw: Any) -> Any:
            return self.wb.add_format({**base, **kw})

        return {
            "text": make(),
            "int": make(num_format="#,##0"),
            "gb": make(num_format="#,##0.00"),
            "pct": make(num_format="0.00%"),
            "date": make(num_format="dd/mm/yyyy hh:mm"),
            "mono": self.wb.add_format({"font_name": "Consolas", "font_size": 10}),
            "banner": make(bold=True, font_size=20, font_color="white", bg_color=NAVY, valign="vcenter", indent=1),
            "subtitle": make(font_color="white", bg_color=NAVY, valign="top", indent=1),
            "nav": make(font_color=BLUE, underline=1, align="center"),
            "section": make(bold=True, font_size=12, font_color=NAVY, bottom=2, bottom_color=NAVY),
            "th": make(bold=True, font_color="white", bg_color=NAVY, align="center"),
            "td": make(bottom=1, bottom_color="#D9D9D9"),
            "td_int": make(bottom=1, bottom_color="#D9D9D9", align="center"),
            "td_size": make(bottom=1, bottom_color="#D9D9D9", align="right", bold=True, font_color=NAVY),
            "note": make(italic=True, font_color=GREY, font_size=9),
            "empty": make(italic=True, font_color=GREY, align="center", valign="vcenter"),
        }

    def _table(self, ws: Any, name: str, columns: list[tuple[str, float]], n_rows: int) -> None:
        """Declare an Excel table over already written data (header row 0)."""
        ws.add_table(0, 0, max(n_rows, 1), len(columns) - 1, {
            "name": name,
            "style": "Table Style Medium 2",
            "columns": [{"header": header} for header, _ in columns],
        })
        for index, (_, width) in enumerate(columns):
            ws.set_column(index, index, width)
        ws.freeze_panes(1, 0)

    def _data_bar(self, ws: Any, col: int, n_rows: int, color: str = BLUE) -> None:
        if n_rows > 0:
            ws.conditional_format(1, col, n_rows, col, {"type": "data_bar", "bar_color": color, "bar_solid": True})

    # ---------------------------------------------------------------- sheets
    def build(self) -> None:
        """Fill every sheet (totals first, dashboard last)."""
        self._extensions()
        self._ages()
        self._files()
        self._directories()
        self._duplicates()
        self._summary_sheet()
        self._chart_data()
        self._dashboard()
        self.ws_dash.activate()
        self.ws_chart.hide()
        target = _pick(self.summary, "Cible", "Target") or self.prefix.name
        self.wb.set_properties({
            "title": f"Storage Forensics Collector - {target}",
            "author": f"sfcollect {__version__}",
            "comments": f"Généré à partir de {self.prefix.name}_*.tsv",
        })

    def _extensions(self) -> None:
        s, f, ws = self.stats, self.fmt, self.ws_ext
        rows = [(r[0] or "<sans_extension>", _int(r[1]), _int(r[2])) for r in _rows(self.inputs["Extensions.tsv"])
                if len(r) >= 3]
        rows.sort(key=lambda t: t[2], reverse=True)
        s.extensions = rows
        s.total_files = sum(t[1] for t in rows)
        s.total_bytes = sum(t[2] for t in rows)
        total_b, total_f = max(s.total_bytes, 1), max(s.total_files, 1)
        for i, (ext, files, size) in enumerate(rows, start=1):
            ws.write_string(i, 0, ext, f["text"])
            ws.write_number(i, 1, files, f["int"])
            ws.write_number(i, 2, size, f["int"])
            ws.write_number(i, 3, size / GB, f["gb"])
            ws.write_number(i, 4, size / total_b, f["pct"])
            ws.write_number(i, 5, files / total_f, f["pct"])
        self._table(ws, "TExtensions", [("Extension", 20), ("Fichiers", 13), ("Taille (octets)", 19),
                                        ("Taille (Go)", 13), ("% du volume", 13), ("% des fichiers", 14)], len(rows))
        self._data_bar(ws, 3, len(rows))

    def _ages(self) -> None:
        s, f, ws = self.stats, self.fmt, self.ws_age
        s.ages = [(r[0], _int(r[1]), _int(r[2])) for r in _rows(self.inputs["AgeBuckets.tsv"]) if len(r) >= 3]
        total_b = max(sum(t[2] for t in s.ages), 1)
        total_f = max(sum(t[1] for t in s.ages), 1)
        for i, (label, files, size) in enumerate(s.ages, start=1):
            ws.write_string(i, 0, label, f["text"])
            ws.write_number(i, 1, files, f["int"])
            ws.write_number(i, 2, size, f["int"])
            ws.write_number(i, 3, size / GB, f["gb"])
            ws.write_number(i, 4, size / total_b, f["pct"])
            ws.write_number(i, 5, files / total_f, f["pct"])
        self._table(ws, "TAge", [("Tranche d'âge", 18), ("Fichiers", 13), ("Taille (octets)", 19),
                                 ("Taille (Go)", 13), ("% du volume", 13), ("% des fichiers", 14)], len(s.ages))
        self._data_bar(ws, 3, len(s.ages), ORANGE)

    def _files(self) -> None:
        s, f, ws = self.stats, self.fmt, self.ws_files
        written = 0
        for row in _rows(self.inputs["Files.tsv"]):
            if len(row) < 6 or written >= self.max_rows:
                continue
            written += 1
            size, path = _int(row[1]), row[5]
            ws.write_number(written, 0, _int(row[0]), f["int"])
            ws.write_number(written, 1, size, f["int"])
            ws.write_number(written, 2, size / GB, f["gb"])
            mtime = _parse_datetime(row[3])
            if mtime is not None:
                ws.write_datetime(written, 3, mtime, f["date"])
            else:
                ws.write_string(written, 3, row[3], f["text"])
            ws.write_string(written, 4, row[4], f["text"])
            ws.write_string(written, 5, path, f["text"])
            if len(s.top_files) < TOP_FILES_DASHBOARD:
                s.top_files.append((size, path))
        self._table(ws, "TFichiers", [("Rang", 8), ("Taille (octets)", 19), ("Taille (Go)", 13),
                                      ("Dernière modification", 21), ("Extension", 12), ("Chemin", 110)], written)
        self._data_bar(ws, 2, written)

    def _directories(self) -> None:
        s, f, ws = self.stats, self.fmt, self.ws_dirs
        total_b = max(s.total_bytes, 1)
        for row in _rows(self.inputs["Directories.tsv"]):
            if len(row) < 3:
                continue
            s.dir_rows += 1
            if s.dir_written >= self.max_rows:
                continue  # keep counting for the truncation note
            path, files, size = row[0], _int(row[1]), _int(row[2])
            s.dir_written += 1
            r = s.dir_written
            ws.write_string(r, 0, path, f["text"])
            ws.write_number(r, 1, files, f["int"])
            ws.write_number(r, 2, size, f["int"])
            ws.write_number(r, 3, size / GB, f["gb"])
            ws.write_number(r, 4, size / total_b, f["pct"])
            if len(s.top_dirs) < TOP_DIRECTORIES_CHART:
                s.top_dirs.append((path, files, size))
        self._table(ws, "TRepertoires", [("Chemin", 100), ("Fichiers", 12), ("Taille (octets)", 19),
                                         ("Taille (Go)", 13), ("% du volume", 13)], s.dir_written)
        self._data_bar(ws, 3, s.dir_written)

    def _duplicates(self) -> None:
        s, f, ws = self.stats, self.fmt, self.ws_dup
        previous_size = -1
        group = 0
        for row in _rows(self.inputs["Duplicates.tsv"]):
            if len(row) < 4:
                continue
            size, count, path = _int(row[0]), _int(row[2]), row[3]
            first = size != previous_size
            if first:
                previous_size = size
                group += 1
                s.dup_groups += 1
                s.dup_files += count
                s.dup_reclaimable += size * max(count - 1, 0)
            s.dup_rows += 1
            if s.dup_written >= self.max_rows:
                continue
            s.dup_written += 1
            r = s.dup_written
            ws.write_number(r, 0, group, f["int"])
            ws.write_number(r, 1, size, f["int"])
            ws.write_number(r, 2, size / GB, f["gb"])
            ws.write_number(r, 3, count, f["int"])
            if first:
                ws.write_number(r, 4, size * max(count - 1, 0) / GB, f["gb"])
            else:
                ws.write_blank(r, 4, None, f["gb"])
            ws.write_string(r, 5, path, f["text"])
        self._table(ws, "TDoublons", [("Groupe", 9), ("Taille unitaire (octets)", 22), ("Taille (Go)", 12),
                                      ("Copies", 9), ("Récupérable (Go)", 17), ("Chemin", 110)], s.dup_written)
        self._data_bar(ws, 4, s.dup_written, RED)

    def _summary_sheet(self) -> None:
        ws, f = self.ws_sum, self.fmt
        ws.set_column(0, 0, 130)
        path = self.inputs["Summary.txt"]
        if not path.is_file():
            ws.write_string(0, 0, f"{path.name} introuvable : résumé non disponible.", f["note"])
            return
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        for i, line in enumerate(lines[: self.max_rows]):
            ws.write_string(i, 0, line, f["mono"])

    def _chart_data(self) -> None:
        """Small, chart-ready ranges on a hidden sheet (short labels)."""
        s, ws = self.stats, self.ws_chart
        heads = ("Extension", "Go", "", "Tranche", "Go", "Fichiers", "", "Répertoire", "Go", "", "Extension", "Fichiers")
        ws.write_row(0, 0, heads)

        pie = s.extensions[:TOP_EXTENSIONS_PIE]
        rest = sum(t[2] for t in s.extensions[TOP_EXTENSIONS_PIE:])
        pie_rows = [(ext, size / GB) for ext, _, size in pie] + ([("Autres", rest / GB)] if rest > 0 else [])
        for i, (label, value) in enumerate(pie_rows, start=1):
            ws.write_string(i, 0, label)
            ws.write_number(i, 1, value)
        self.n_pie = len(pie_rows)

        for i, (label, files, size) in enumerate(s.ages, start=1):
            ws.write_string(i, 3, label)
            ws.write_number(i, 4, size / GB)
            ws.write_number(i, 5, files)
        self.n_age = len(s.ages)

        # Bar charts draw the first category at the bottom: write ascending.
        for i, (path, _, size) in enumerate(reversed(s.top_dirs), start=1):
            ws.write_string(i, 7, _short_path(path))
            ws.write_number(i, 8, size / GB)
        self.n_dirs = len(s.top_dirs)

        by_count = sorted(s.extensions, key=lambda t: t[1], reverse=True)[:TOP_EXTENSIONS_COUNT]
        for i, (ext, files, _) in enumerate(reversed(by_count), start=1):
            ws.write_string(i, 10, ext)
            ws.write_number(i, 11, files)
        self.n_count = len(by_count)

    # ------------------------------------------------------------- dashboard
    def _tile(self, col: int, label: str, value: int | float | str, sub: str, accent: str,
              num_format: str = "#,##0") -> None:
        ws = self.ws_dash
        sides = {"bg_color": LIGHT, "left": 5, "left_color": "white", "right": 5, "right_color": "white",
                 "font_name": "Calibri", "indent": 1}
        label_fmt = self.wb.add_format({**sides, "font_size": 9, "font_color": GREY, "bold": True,
                                        "top": 5, "top_color": accent, "valign": "bottom"})
        value_fmt = self.wb.add_format({**sides, "font_size": 18, "bold": True, "font_color": accent,
                                        "valign": "vcenter", "align": "left", "num_format": num_format})
        sub_fmt = self.wb.add_format({**sides, "font_size": 9, "font_color": GREY, "italic": True, "valign": "top"})
        ws.merge_range(4, col, 4, col + 2, label.upper(), label_fmt)
        if isinstance(value, str):
            ws.merge_range(5, col, 5, col + 2, value, value_fmt)
        else:
            ws.merge_range(5, col, 5, col + 2, "", value_fmt)
            ws.write_number(5, col, value, value_fmt)
        ws.merge_range(6, col, 6, col + 2, sub, sub_fmt)

    def _chart_base(self, chart: Any, title: str, width: int, height: int) -> None:
        chart.set_title({"name": title, "name_font": {"size": 12, "bold": True, "color": NAVY}})
        chart.set_size({"width": width, "height": height})
        chart.set_chartarea({"border": {"color": "#D9D9D9"}})

    def _empty_box(self, row: int, col: int, last_row: int, last_col: int, title: str) -> None:
        self.ws_dash.merge_range(row, col, last_row, last_col, f"{title}\n(aucune donnée)", self.fmt["empty"])

    def _dashboard(self) -> None:
        s, f, ws, wb = self.stats, self.fmt, self.ws_dash, self.wb
        S = SHEET_CHARTS
        ws.hide_gridlines(2)
        ws.set_zoom(90)
        ws.set_tab_color(NAVY)
        ws.set_column(0, 0, 2)
        ws.set_column(1, 18, 9.5)
        ws.set_column(19, 19, 2)

        # Banner ------------------------------------------------------------
        summ = self.summary
        target = _pick(summ, "Cible", "Target") or self.prefix.name
        start = _parse_datetime(_pick(summ, "Scan Start"))
        end = _parse_datetime(_pick(summ, "Scan End"))
        duration = _pick(summ, "Duration").split(" (")[0]
        interrupted = bool(_pick(summ, "Statut", "Status"))
        ws.set_row(0, 36)
        ws.set_row(1, 22)
        ws.merge_range(0, 0, 0, 19, "Storage Forensics Collector — Tableau de bord", f["banner"])
        if summ:
            parts = [f"Cible : {target}"]
            if start:
                parts.append(f"Début : {start:%d/%m/%Y %H:%M}")
            if end:
                parts.append(f"Fin : {end:%d/%m/%Y %H:%M}")
            if duration:
                parts.append(f"Durée : {duration}")
            parts.append("Statut : INTERROMPU (résultats partiels)" if interrupted else "Statut : terminé")
            subtitle = "     •     ".join(parts)
        else:
            subtitle = f"Cible : {target}     •     Summary.txt absent : horaires non disponibles"
        ws.merge_range(1, 0, 1, 19, subtitle, f["subtitle"])

        # Navigation ----------------------------------------------------------
        for col, sheet in zip((1, 4, 7, 10, 13, 16),
                              (SHEET_FILES, SHEET_DIRS, SHEET_EXT, SHEET_AGE, SHEET_DUP, SHEET_SUMMARY), strict=True):
            ws.merge_range(2, col, 2, col + 2, "", f["nav"])
            ws.write_url(2, col, f"internal:'{sheet}'!A1", f["nav"], string=f"→ {sheet}")

        # KPI tiles -----------------------------------------------------------
        ws.set_row(3, 8)
        ws.set_row(4, 20)
        ws.set_row(5, 34)
        ws.set_row(6, 20)
        self._tile(1, "Fichiers scannés", s.total_files, f"{thousands_fr(len(s.extensions))} extensions distinctes",
                   NAVY)
        self._tile(4, "Volume total", human_size_fr(s.total_bytes), f"{thousands_fr(s.total_bytes)} octets", BLUE)
        if s.top_files:
            big_size, big_path = s.top_files[0]
            self._tile(7, "Plus gros fichier", human_size_fr(big_size), _left_truncate(_short_path(big_path), 40),
                       TEAL)
        else:
            self._tile(7, "Plus gros fichier", "n/d", "aucun fichier", TEAL)
        self._tile(10, "Doublons candidats", human_size_fr(s.dup_reclaimable),
                   f"récupérables · {thousands_fr(s.dup_groups)} groupes · {thousands_fr(s.dup_files)} fichiers",
                   RED)
        old_bytes = s.ages[-1][2] if s.ages else 0
        old_label = s.ages[-1][0] if s.ages else ">3 ans"
        self._tile(13, f"Données {old_label}", old_bytes / max(s.total_bytes, 1),
                   f"{human_size_fr(old_bytes)} non modifiés", ORANGE, "0.0%")
        if summ:
            denied = _int(_pick(summ, "Accès refusés", "Access denied") or "0")
            errors = denied + sum(_int(_pick(summ, *keys) or "0") for keys in (
                ("Chemins trop longs", "Path too long"),
                ("Supprimés pendant scan", "Removed during scan"),
                ("Autres erreurs", "Other errors"),
            ))
            self._tile(16, "Erreurs de lecture", errors, f"dont {thousands_fr(denied)} accès refusés", PURPLE)
        else:
            self._tile(16, "Erreurs de lecture", "n/d", "Summary.txt absent", PURPLE)

        # Row 8: extensions doughnut + age combo -----------------------------
        if self.n_pie:
            pie = wb.add_chart({"type": "doughnut"})
            pie.add_series({
                "name": "Volume (Go)",
                "categories": [S, 1, 0, self.n_pie, 0],
                "values": [S, 1, 1, self.n_pie, 1],
                "data_labels": {"percentage": True, "font": {"size": 9, "color": "white", "bold": True}},
                "points": [{"fill": {"color": PIE_COLORS[i % len(PIE_COLORS)]}} for i in range(self.n_pie)],
            })
            pie.set_hole_size(50)
            pie.set_legend({"position": "right", "font": {"size": 9}})
            self._chart_base(pie, "Répartition du volume par extension", 612, 340)
            ws.insert_chart(8, 1, pie, {"x_offset": 0, "y_offset": 4})
        else:
            self._empty_box(8, 1, 24, 9, "Répartition du volume par extension")

        if self.n_age:
            col = wb.add_chart({"type": "column"})
            col.add_series({
                "name": "Volume (Go)",
                "categories": [S, 1, 3, self.n_age, 3],
                "values": [S, 1, 4, self.n_age, 4],
                "points": [{"fill": {"color": AGE_COLORS[i % len(AGE_COLORS)]}} for i in range(self.n_age)],
                "data_labels": {"value": True, "num_format": "#,##0", "font": {"size": 9}},
                "gap": 60,
            })
            line = wb.add_chart({"type": "line"})
            line.add_series({
                "name": "Fichiers",
                "categories": [S, 1, 3, self.n_age, 3],
                "values": [S, 1, 5, self.n_age, 5],
                "y2_axis": True,
                "line": {"color": NAVY, "width": 2.25},
                "marker": {"type": "circle", "size": 6, "fill": {"color": NAVY}, "border": {"color": NAVY}},
            })
            col.combine(line)
            col.set_y_axis({"name": "Volume (Go)", "num_format": "#,##0",
                            "major_gridlines": {"visible": True, "line": {"color": "#E7E7E7"}}})
            line.set_y2_axis({"name": "Fichiers", "num_format": "#,##0"})
            col.set_legend({"position": "bottom", "font": {"size": 9}})
            self._chart_base(col, "Âge des données (dernière modification)", 612, 340)
            ws.insert_chart(8, 10, col, {"x_offset": 0, "y_offset": 4})
        else:
            self._empty_box(8, 10, 24, 18, "Âge des données")

        # Row 26: top directories ----------------------------------------------
        if self.n_dirs:
            bar = wb.add_chart({"type": "bar"})
            bar.add_series({
                "name": "Volume (Go)",
                "categories": [S, 1, 7, self.n_dirs, 7],
                "values": [S, 1, 8, self.n_dirs, 8],
                "fill": {"color": BLUE},
                "data_labels": {"value": True, "num_format": "#,##0.0", "font": {"size": 9}},
                "gap": 40,
            })
            bar.set_legend({"none": True})
            bar.set_x_axis({"num_format": "#,##0", "major_gridlines": {"visible": True, "line": {"color": "#E7E7E7"}}})
            self._chart_base(bar, f"Top {self.n_dirs} des répertoires par volume (Go, fichiers directs)", 1224, 400)
            ws.insert_chart(26, 1, bar, {"x_offset": 0, "y_offset": 4})
        else:
            self._empty_box(26, 1, 45, 18, "Top des répertoires")

        # Row 47: extensions by count + top files list ------------------------
        if self.n_count:
            cnt = wb.add_chart({"type": "bar"})
            cnt.add_series({
                "name": "Fichiers",
                "categories": [S, 1, 10, self.n_count, 10],
                "values": [S, 1, 11, self.n_count, 11],
                "fill": {"color": TEAL},
                "data_labels": {"value": True, "num_format": "#,##0", "font": {"size": 9}},
                "gap": 40,
            })
            cnt.set_legend({"none": True})
            cnt.set_x_axis({"num_format": "#,##0", "major_gridlines": {"visible": True, "line": {"color": "#E7E7E7"}}})
            self._chart_base(cnt, f"Top {self.n_count} des extensions par nombre de fichiers", 612, 340)
            ws.insert_chart(47, 1, cnt, {"x_offset": 0, "y_offset": 4})
        else:
            self._empty_box(47, 1, 63, 9, "Extensions par nombre de fichiers")

        ws.merge_range(47, 10, 47, 18, f"Top {TOP_FILES_DASHBOARD} des plus gros fichiers", f["section"])
        ws.write_string(48, 10, "#", f["th"])
        ws.write_string(48, 11, "Taille", f["th"])
        ws.merge_range(48, 12, 48, 18, "Fichier", f["th"])
        for i, (size, path) in enumerate(s.top_files, start=1):
            r = 48 + i
            ws.set_row(r, 22)
            ws.write_number(r, 10, i, f["td_int"])
            ws.write_string(r, 11, human_size_fr(size), f["td_size"])
            ws.merge_range(r, 12, r, 18, _left_truncate(path, 75), f["td"])
            if len(path) > 75:
                ws.write_comment(r, 12, path, {"x_scale": 3, "y_scale": 0.6})
        ws.write_url(48 + TOP_FILES_DASHBOARD + 2, 10, f"internal:'{SHEET_FILES}'!A1", f["nav"],
                     string="→ liste complète")

        # Footer notes --------------------------------------------------------
        notes = [
            "Doublons candidats : fichiers de taille strictement identique (≥ seuil du scan) ; contenu NON vérifié.",
            "Taille d'un répertoire = fichiers directement contenus (hors sous-répertoires).",
        ]
        if s.dir_written < s.dir_rows:
            notes.append(f"Feuille « {SHEET_DIRS} » : {thousands_fr(s.dir_written)} plus gros répertoires sur "
                         f"{thousands_fr(s.dir_rows)} (liste complète dans {self.prefix.name}_Directories.tsv).")
        if s.dup_written < s.dup_rows:
            notes.append(f"Feuille « {SHEET_DUP} » : {thousands_fr(s.dup_written)} lignes sur "
                         f"{thousands_fr(s.dup_rows)} (liste complète dans {self.prefix.name}_Duplicates.tsv).")
        notes.append(f"Généré le {datetime.now():%d/%m/%Y %H:%M} par sfcollect {__version__} "
                     f"à partir de {self.prefix.name}_*.tsv")
        for i, note in enumerate(notes):
            ws.write_string(65 + i, 1, note, f["note"])

        ws.set_landscape()
        ws.set_paper(9)
        ws.fit_to_pages(1, 0)
        ws.set_margins(left=0.3, right=0.3, top=0.4, bottom=0.4)
        ws.print_area(0, 0, 65 + len(notes), 19)


def _discard(workbook: Any, target: Path) -> None:
    """Close a half-built workbook and drop the partial file (best effort)."""
    try:
        workbook.close()
    except Exception:
        pass  # the build already failed; only the file handle matters here
    try:
        target.unlink(missing_ok=True)
    except OSError:
        pass


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------
def build_dashboard(source: str | Path, output: str | Path | None = None,
                    max_rows: int = DEFAULT_MAX_ROWS) -> Path:
    r"""Build ``<prefix>_Dashboard.xlsx`` from the TSV exports of one target.

    Args:
        source: Export prefix (``reports\H_20261007-1529``) or any export file.
        output: Destination ``.xlsx`` (default: ``<prefix>_Dashboard.xlsx``).
        max_rows: Row cap for the large sheets (directories, duplicates).

    Returns:
        Path of the written workbook.

    Raises:
        DashboardError: XlsxWriter missing, exports missing, or file locked.
    """
    try:
        import xlsxwriter
        from xlsxwriter.exceptions import XlsxWriterException
    except ImportError as exc:  # pragma: no cover - depends on environment
        raise DashboardError(
            'XlsxWriter est requis : python -m pip install "XlsxWriter>=3.1" (ou pip install -e .[excel])'
        ) from exc

    prefix = resolve_prefix(source)
    inputs = {suffix: Path(f"{prefix}_{suffix}") for suffix in REPORT_SUFFIXES[:6]}
    missing = [str(p) for suffix, p in inputs.items() if suffix != "Summary.txt" and not p.is_file()]
    if missing:
        raise DashboardError("exports introuvables : " + ", ".join(missing))

    max_rows = max(1, min(max_rows, EXCEL_MAX_ROWS - 1))
    target = Path(output) if output else Path(f"{prefix}_Dashboard.xlsx")
    target.parent.mkdir(parents=True, exist_ok=True)

    workbook = xlsxwriter.Workbook(str(target), {
        "strings_to_numbers": False,
        "strings_to_formulas": False,
        "strings_to_urls": False,
    })
    try:
        _Builder(workbook, prefix, inputs, parse_summary(inputs["Summary.txt"]), max_rows).build()
    except (OSError, XlsxWriterException) as exc:
        _discard(workbook, target)
        raise DashboardError(f"échec de la construction du classeur : {exc}") from exc
    except BaseException:
        _discard(workbook, target)
        raise
    try:
        workbook.close()
    except (OSError, XlsxWriterException) as exc:
        raise DashboardError(f"impossible d'écrire {target} (fichier ouvert dans Excel ?) : {exc}") from exc
    return target
