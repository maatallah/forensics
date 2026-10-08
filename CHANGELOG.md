# Changelog

## 0.3.0 - 2026-10-08

### Added
- Excel dashboard (`<Préfixe>_Dashboard.xlsx`): 8 sheets (KPI banner, top files,
  directories, extensions, age, duplicates, summary, chart data), 4 charts,
  5 filterable Excel tables, built from the TSV exports without rescanning.
- `sfcollect scan --excel` builds the dashboard right after the exports; a
  dashboard failure only warns and never changes the scan exit code.
- `sfcollect dashboard SOURCE [--output FILE] [--max-rows N]` builds it later
  from a prefix, any export file, or a directory containing one scan.
- Optional extra: `pip install -e ".[excel]"` (XlsxWriter >= 3.1); the core
  stays dependency-free and imports XlsxWriter lazily.

### Changed
- Pinned the ruff rule set in `pyproject.toml` so the lint gate no longer
  changes when ruff widens its defaults.
- `mypy --strict` now passes (xlsxwriter override, POSIX-only `os.sysconf`
  annotated).

## 0.2.0 - 2026-10-07

### Added
- Streaming `os.scandir` enumerator (no recursion, junction/symlink safe, `\\?\` long paths).
- Worker -> Partial Result -> Reducer parallel architecture (no shared mutable state).
- Top-N largest files (min-heap), real-time aggregates, age buckets.
- Size-based duplicate candidates with bounded memory.
- Memory bounds: directory roll-up, extension overflow bucket, duplicate purge, RSS watchdog.
- TSV (UTF-8, TAB) exports and `Summary.txt`; `sfcollect scan` CLI with progress/ETA.
- Unit and integration tests.
