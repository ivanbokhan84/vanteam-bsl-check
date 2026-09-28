# Changelog

All notable changes to this project are documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project follows [Semantic Versioning](https://semver.org/). The wrapper
version is independent of the BSL Language Server version.

## [1.1.0-rc.1] - 2026-09-28

Release candidate. Engine: BSL Language Server 1.0.7 (commit f377f95a), unmodified. Independent review pending.

### Added
- Any WARN/ERROR line in the engine log ends with code `3`: BSL LS 1.0.7 exits with `0` after skipping part of the analysis (broken `Configuration.xml`, invalid rule parameter, failed platform context).
- With `--source-dir` pointing at a Designer (`Configuration.xml`) or EDT (`Configuration/Configuration.mdo`) dump, a module whose metadata did not load ends with code `3`.
- The source of the 1C platform context is printed: the installed platform's syntax helper or built-in descriptions.
- 13 new tests: 56 in total, 11 of them integration tests.

### Changed
- Java below 21 ends with code `3`.
- Only the lsp4j severities `Error`, `Warning`, `Information`, `Hint` are accepted; the 0.24-era names are an unknown format and end with code `3`.
- JVM options are unchanged, re-measured on 1.0.7 against the default JVM with identical diagnostics.

### Known issues
- A check is 1.9–2.4 times slower than with 0.29.0 on the author's machine; most of the time goes to loading the installed platform's syntax helper.

## [1.0.0] - 2026-09-28

First public release. Engine: BSL Language Server 0.29.0, unmodified.

### Added
- `check_bsl.py`: two-level check — `oscript -check` and BSL Language Server `--analyze`; `--deep`, `--all`, `--quiet`.
- Explicit analysis scope: the module's own directory by default, nested folders excluded; `--source-dir` for a recursive context and metadata root; `--standalone` for a single file.
- Exit code `3` for an incomplete check: missing engine, timeout, JVM failure, known engine failure messages, no report, the module missing from the report, an unknown severity.
- OS-level lock: one BSL LS analysis per checkout, a second run waits up to 300 s.
- JVM options for short CLI runs: `-XX:ActiveProcessorCount=min(4, CPUs)`, `-XX:TieredStopAtLevel=1`.
- `bsl_new_findings.py`: new ERROR/WARN findings against a Git revision, with the baseline built from the same directory at that revision; an unconfirmed OneScript "unknown symbol" is reported as incomplete.
- `scripts/fetch_dependencies.py` and `DEPENDENCIES.json`: SHA-256-pinned download of the BSL LS JAR and an optional portable Temurin 21 JDK.
- 43 tests, four of them integration tests with real Java, BSL LS, OneScript and Git.

### Changed
- OneScript output is read as explicit UTF-8, and an unrecognised result is reported as incomplete instead of success.
- An explicitly set `BSL_LS_JAR` that does not exist is an error; it is no longer replaced by the bundled JAR.
- Diagnostics configuration: `Typo` and `UsingServiceTag` are disabled with a boolean `false`, as the 0.29.0 schema requires; the object form `{"enabled": false}` did not disable them.

[1.1.0-rc.1]: https://github.com/ivanbokhan84/vanteam-bsl-check/releases/tag/v1.1.0-rc.1
[1.0.0]: https://github.com/ivanbokhan84/vanteam-bsl-check/releases/tag/v1.0.0
