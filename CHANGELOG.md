# Changelog

## 0.2.0 - 2026-10-07

### Added
- Streaming `os.scandir` enumerator (no recursion, junction/symlink safe, `\\?\` long paths).
- Worker -> Partial Result -> Reducer parallel architecture (no shared mutable state).
- Top-N largest files (min-heap), real-time aggregates, age buckets.
- Size-based duplicate candidates with bounded memory.
- Memory bounds: directory roll-up, extension overflow bucket, duplicate purge, RSS watchdog.
- TSV (UTF-8, TAB) exports and `Summary.txt`; `sfc scan` CLI with progress/ETA.
- Unit and integration tests.
