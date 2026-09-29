# Changelog

All notable changes to this project are documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project follows [Semantic Versioning](https://semver.org/). The wrapper
version is independent of the BSL Language Server version.

## [Unreleased]

The OneScript level is a separate command now; each engine has its own fork.

### Added
- `check_oscript.py`: level 1 on OneScript 2.2.0-vanteam.2 (`oscript -checkall`, fork [ivanbokhan84/OneScript](https://github.com/ivanbokhan84/OneScript), tag `v2.2.0-vanteam.2`). One engine start for all files, every error of a module, unknown 1C names listed instead of stopping the check; a copy without directive lines for modules with `#Если`, so code under `#Если Сервер` is checked; the line of a duplicate method; a list of unknown names outside the 1C context for manual review. Exit codes `0`/`1`/`2`/`3`.
- `test_check_oscript.py`: 8 tests and 19 fixture modules with known error lines.
- README and project page: links to both forks — [ivanbokhan84/OneScript](https://github.com/ivanbokhan84/OneScript) and [ivanbokhan84/bsl-language-server](https://github.com/ivanbokhan84/bsl-language-server).

### Changed
- Recommended usage: `check_oscript.py` for level 1 and `check_bsl.py --deep` for level 2. `check_bsl.py` without `--deep` and with `--all` still runs the legacy `oscript -check` level, unchanged.
- `DEPENDENCIES.json`: OneScript 2.2.0-vanteam.2 from the fork.

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

[1.0.0]: https://github.com/ivanbokhan84/vanteam-bsl-check/releases/tag/v1.0.0
