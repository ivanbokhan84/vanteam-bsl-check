# Changelog

All notable changes to this project are documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project follows [Semantic Versioning](https://semver.org/). The wrapper
version is independent of the BSL Language Server version.

## [2.0.0] - 2026-09-29

First stable release of 2.x, now on `main`. The code is the same as in 2.0.0-rc.1: the latest versions of both checkers, `check_oscript.py` on OneScript 2.2.0-vanteam.2 and VANTEAM BSL Server check 1.0.1 on the BSL Language Server fork 1.0.7-vanteam.1. See 2.0.0-rc.1 below for the breaking changes against 1.x.

### Verified
- 109 tests on a fresh clone: 82 unit and 19 integration tests of VANTEAM BSL Server check 1.0.1 on real Java 21 and the fork JAR, 8 tests of `check_oscript.py` on OneScript 2.2.0-vanteam.2. All pass, none skipped.
- The copied files of VANTEAM BSL Server check equal its release zip byte for byte (`docs/bsl-server-check/SOURCE.json`).
- Acceptance of VANTEAM BSL Server check in the BSL Server project: against Vanteam BSL Check 1.1.0 on five real modules, identical findings in every run, wall time ×0.17–0.38.

## [2.0.0-rc.1] - 2026-09-29

Release candidate. The BSL Language Server level is now VANTEAM BSL Server check 1.0.1, maintained in the BSL Server project and copied here unchanged from its release asset `vanteam-bsl-server-check-1.0.1.zip` (SHA-256 `1c61c22e6f50e5e5a886d528c4425dbb9a73d8566c008331b3e328990f8e7d2b`). The OneScript level stays `check_oscript.py`. One wrapper per engine instead of two diverging copies of the BSL LS wrapper.

### Changed — breaking
- `check_bsl.py` runs BSL Language Server only. Without flags it no longer runs the legacy `oscript -check` level; `--deep` and `--all` are accepted and change nothing (`--all` prints a hint about `check_oscript.py`).
- The engine is installed once per machine by `tools/setup_bsl_server.py` into `VANTEAM_BSL_HOME` (default `%LOCALAPPDATA%\vanteam-bsl-server`) instead of `tools/bsl_ls`. Environment variables are `VANTEAM_BSL_*`; `BSL_LS_CACHE` and `BSL_LS_XMX` of 1.2.0-rc.1 are gone.

### Added
- Several modules of one directory in one JVM with several `--target`; `--json`; a Russian-only syntax helper; AppCDS with a JDK fingerprint; a fix for a module on another drive than the checked directory (see [docs/bsl-server-check/CHANGELOG.md](docs/bsl-server-check/CHANGELOG.md)).
- `docs/bsl-server-check/`: README, CHANGELOG, VERSION of the bundled checker and `SOURCE.json` with the SHA-256 of every copied file.
- Tests of the bundled checker: 82 unit and 19 integration tests; with `test_check_oscript.py` 109 in total.

### Removed
- `scripts/prepare_cds.py` and the own fork support of 1.2.0-rc.1 in `check_bsl.py`: `setup_bsl_server.py` and the bundled checker do it.

## [1.2.0-rc.1] - 2026-09-29

Release candidate. Engines: BSL Language Server fork [1.0.7-vanteam.1](https://github.com/ivanbokhan84/bsl-language-server/releases/tag/v1.0.7-vanteam.1) and OneScript fork [2.2.0-vanteam.2](https://github.com/ivanbokhan84/OneScript/tree/v2.2.0-vanteam.2). Includes the changes of 1.1.0-rc.1. Independent review pending.

The OneScript level is a separate command now; each engine has its own fork.

### Added
- `check_oscript.py`: level 1 on OneScript 2.2.0-vanteam.2 (`oscript -checkall`). One engine start for all files, every error of a module, unknown 1C names listed instead of stopping the check; a copy without directive lines for modules with `#Если`, so code under `#Если Сервер` is checked; the line of a duplicate method; a list of unknown names outside the 1C context for manual review. Exit codes `0`/`1`/`2`/`3`. 8 tests and 19 fixture modules with known error lines, kept with CRLF.
- The BSL LS fork JAR: `--target` for the checked module and a persistent platform-context cache (`BSL_LS_CACHE`, default `tools/bsl_ls/_cache`). The upstream JAR without the fork suffix keeps working without them.
- `scripts/prepare_cds.py`: unpacked JAR and a trained AppCDS archive, used by `check_bsl.py` only when the stamp of the JAR and JDK matches.
- `-XX:+ExitOnOutOfMemoryError` and `--silent` on every run; heap 512 MB, 1 GB for a scope of 200 files and more, `BSL_LS_XMX` overrides; timeout 600 s for large scopes.
- The platform line of the output shows the cache result (`hit`, `miss`, `written`).
- 7 unit tests for the fork command, JAR choice, heap, out-of-memory exit and AppCDS stamp: 63 in `test_check_bsl.py`, 11 of them integration tests.
- README and project page link both forks.

### Changed
- Recommended usage: `check_oscript.py` for level 1 and `check_bsl.py --deep` for level 2. `check_bsl.py` without `--deep` and with `--all` still runs the legacy `oscript -check` level.
- `DEPENDENCIES.json`: the BSL LS fork JAR, SHA-256 `0b75fa1235513d970a4125f172dd4515e82b8f30a3f80836991f530ef02c205b`; OneScript 2.2.0-vanteam.2 from the fork.
- JAR choice: the fork of a version before the upstream JAR of the same version.

### Measured
- One module on the author's machine, cache warm and AppCDS prepared: 7 s with the fork JAR against 24 s with the upstream 1.0.7 JAR, identical findings.

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

[1.0.0]: https://github.com/ivanbokhan84/vanteam-bsl-check/releases/tag/v1.0.0

[1.2.0-rc.1]: https://github.com/ivanbokhan84/vanteam-bsl-check/tree/release/1.2.0
[1.1.0-rc.1]: https://github.com/ivanbokhan84/vanteam-bsl-check/tree/release/1.1.0
[2.0.0]: https://github.com/ivanbokhan84/vanteam-bsl-check/releases/tag/v2.0.0
[2.0.0-rc.1]: https://github.com/ivanbokhan84/vanteam-bsl-check/releases/tag/v2.0.0-rc.1
