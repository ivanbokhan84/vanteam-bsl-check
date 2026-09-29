<h1 align="center">Vanteam BSL Check</h1>

<h4 align="center">Checks for 1C:Enterprise (BSL) modules in two separate commands — OneScript and BSL Language Server — with exit codes you can trust</h4>

<div align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-green" alt="License: MIT" /></a>
  <a href="https://www.python.org/"><img src="https://img.shields.io/badge/python-3.10%2B-blue" alt="Python 3.10+" /></a>
  <a href="https://github.com/ivanbokhan84/OneScript/tree/v2.2.0-vanteam.2"><img src="https://img.shields.io/badge/OneScript-2.2.0--vanteam.2-lightgrey" alt="OneScript 2.2.0-vanteam.2" /></a>
  <a href="https://github.com/1c-syntax/bsl-language-server/releases/tag/v0.29.0"><img src="https://img.shields.io/badge/BSL%20Language%20Server-0.29.0-lightgrey" alt="BSL Language Server 0.29.0" /></a>
  <a href="CHANGELOG.md"><img src="https://img.shields.io/badge/release-1.0.0-orange" alt="Release 1.0.0" /></a>
</div>
<br/>

<p align="center"><a href="https://ivanbokhan84.github.io/vanteam-bsl-check/"><img src="docs/cover.png" alt="Vanteam BSL Check: exit codes, tests and measurements" width="100%" /></a></p>

Project page with the test and measurement charts: **[ivanbokhan84.github.io/vanteam-bsl-check](https://ivanbokhan84.github.io/vanteam-bsl-check/)**.

Vanteam BSL Check is a set of small Python wrappers around two engines. Each engine has its own fork with the changes the checks rely on; this repository ties them together:

| Level | Command | Engine | Fork |
|---|---|---|---|
| 1. OneScript | `tools/check_oscript.py` | OneScript 2.2.0-vanteam.2, `oscript -checkall` | **[ivanbokhan84/OneScript](https://github.com/ivanbokhan84/OneScript)** — preprocessor fix and `-checkall`, tag [`v2.2.0-vanteam.2`](https://github.com/ivanbokhan84/OneScript/tree/v2.2.0-vanteam.2) |
| 2. BSL Language Server | `tools/check_bsl.py --deep` | BSL Language Server 0.29.0, `--analyze` | **[ivanbokhan84/bsl-language-server](https://github.com/ivanbokhan84/bsl-language-server)** — fork of 1.0.7 with a persistent platform-context cache and `--target`, release [`v1.0.7-vanteam.1`](https://github.com/ivanbokhan84/bsl-language-server/releases/tag/v1.0.7-vanteam.1); used by [1.2.0-rc.1](https://github.com/ivanbokhan84/vanteam-bsl-check/tree/release/1.2.0), `main` stays on the upstream 0.29.0 JAR |

Run both after every change to a module:

```shell
python tools/check_oscript.py path/to/Module.bsl          # 1. OneScript: syntax and code generator checks
python tools/check_bsl.py path/to/Module.bsl --deep       # 2. BSL Language Server: diagnostics
```

Both wrappers refuse to report success when the analysis did not actually happen: a missing engine, a missing report, a module absent from the report, an unknown severity or a crashed rule end with a separate "check not completed" code instead of a green result.

Maintained by **Ivan Bokhan**.

## Features

* **Every error of a module in one run (level 1).** `check_oscript.py` runs `oscript -checkall` once for all files. It reports every syntax and code generator error of a module — wrong argument count for the module's own methods, a procedure used as a function, a duplicate method (with its line), labels — instead of stopping at the first one. Unknown names of the 1C global context (`Справочники`, `Документы`, other common modules) are listed, not treated as errors.
* **Code under `#Если Сервер` is checked too.** OneScript defines no 1C preprocessor symbols, so any OneScript check skips the body of `#Если Сервер Тогда`. `check_oscript.py` also checks a copy of every module that has `#Если`, with the directive lines blanked out (line numbers unchanged); errors found only there are marked `[ветка #Если]`.
* **Honest results.** Exit code `3` of `check_bsl.py` means the check did not complete — the engine is missing, timed out, crashed, printed a known failure message, or produced no usable report. It is never folded into "OK". `check_oscript.py` uses `2` for a file that could not be checked and `3` for a missing engine.
* **Explicit analysis scope.** By default BSL LS sees the `.bsl`/`.os` files of the module's own directory; nested folders (for example archives next to the module) are left out. `--source-dir` sets a recursive context and the metadata root for a full Designer/EDT dump; `--standalone` checks the file alone. The scope is printed with every run.
* **Only new findings.** `bsl_new_findings.py` compares a module with its version in Git (default `HEAD`) and prints only the ERROR/WARN findings that appeared. Large legacy modules can carry hundreds of old warnings; the rule "no errors and no *new* warnings" stays usable.
* **One analysis at a time.** An OS-level file lock serialises BSL LS runs per checkout; a second run waits up to 300 s. The lock is released by the OS if its owner crashes.
* **Tuned for short CLI runs.** The JVM runs with `-XX:TieredStopAtLevel=1` and `-XX:ActiveProcessorCount=min(4, CPUs)`. An independent recalculation of the paired measurements on the author's machine (4 cores / 8 threads, pre-release build, four scenarios) gives CPU time ×0.28–0.38 and wall time ×0.63–0.92 of the default JVM; the only diagnostic differences were the two rules disabled on purpose. These settings target one-shot analysis, not a long-running language server in an editor.

## Requirements

| Component | Version | Notes |
|---|---|---|
| Python | 3.10+ | standard library only |
| OneScript | 2.2.0-vanteam.2 | for `check_oscript.py`; built from the [fork](https://github.com/ivanbokhan84/OneScript), see [OneScript](#onescript) |
| Java | 21+ | a portable Temurin 21 can be fetched into `tools/jdk21` |
| BSL Language Server | 0.29.0 | `bsl-language-server-0.29.0-exec.jar`, fetched and SHA-256-verified |
| Git | any recent | for `bsl_new_findings.py` |

Developed and tested on Windows 11. The code has POSIX branches (file lock, `java` on `PATH`), but other platforms have not been tested.

## Installation

```shell
git clone https://github.com/ivanbokhan84/vanteam-bsl-check.git
cd vanteam-bsl-check
python scripts/fetch_dependencies.py          # BSL LS JAR
python scripts/fetch_dependencies.py --jdk    # optional: portable JDK 21 for Windows x64
python scripts/fetch_dependencies.py --check  # verify what is present
```

BSL LS and JDK binaries are downloaded from their official GitHub releases and checked against the SHA-256 values pinned in [`DEPENDENCIES.json`](DEPENDENCIES.json). They are not stored in this repository. OneScript is built separately, see below.

## OneScript

`check_oscript.py` needs the OneScript build with `-checkall`. Binaries are not published; build it from the fork (.NET SDK 8+):

```shell
git clone -b v2.2.0-vanteam.2 https://github.com/ivanbokhan84/OneScript.git
cd OneScript
dotnet publish src/oscript/oscript.csproj -r win-x64 --self-contained -c Release -p:VersionPrefix=2.2.0 -p:VersionSuffix=vanteam.2 -p:PublishReadyToRun=true -o %LOCALAPPDATA%\Programs\OneScript-2.2.0-vanteam.2\bin
```

The wrapper looks for `VANTEAM_OSCRIPT`, then `%LOCALAPPDATA%\Programs\OneScript-2.2.0-vanteam.2\bin\oscript.exe`. Check: `oscript -version` prints `2.2.0-vanteam.2`.

Why the fork: on real 1C modules the stock `oscript -check` stops at the first unknown 1C name, so the code generator checks after it never run, and OneScript 1.9.4 sees nothing at all after it. The official 2.2.0 also takes a `#` inside a string of an inactive `#Если` branch for a directive. Measured on 76 modules of a 1C configuration: one `-checkall` run takes 2.4 s against 50–80 s for `-check` with a process per file; all 116 real modules are checked to the end against 10 with `-check`. Details and data are on the [fork's page](https://ivanbokhan84.github.io/OneScript/).

What OneScript does not check: the 1C compatibility mode. `СтрНайти`, `ТекущаяДата` and other 8.3 functions are accepted — BSL LS reports them (`DeprecatedFind` and others). Names that exist neither in OneScript nor in 1C (`НижнийРегистр`, `Симв`) appear in the "Проверить вручную" list: look for a typo there.

## Usage

```shell
python tools/check_oscript.py path/to/Module.bsl              # level 1: one module
python tools/check_oscript.py path/to/CommonModules           # level 1: a folder in one run
python tools/check_oscript.py                                 # level 1: src, cf_source, release/cf_source
python tools/check_oscript.py path --quiet --symbols          # no OK lines; unknown names per file

python tools/check_bsl.py path/to/Module.bsl --deep           # level 2: BSL Language Server
python tools/check_bsl.py path/to/Module.bsl --deep --quiet   # print findings only
python tools/check_bsl.py path/to/Module.bsl --deep --source-dir path/to/dump   # full configuration context
python tools/check_bsl.py path/to/Module.bsl --deep --standalone                # the file alone

python tools/bsl_new_findings.py path/to/Module.bsl            # new findings against HEAD
python tools/bsl_new_findings.py path/to/Module.bsl main~3     # ... against any revision
```

A single module checked without `--source-dir` does not see the rest of the configuration: cross-module and metadata diagnostics need the full dump.

`check_bsl.py` without `--deep` still runs the legacy level — `oscript -check` of the installed OneScript, one file, up to the first error — and `--all` runs it together with BSL LS. They are kept for compatibility with release 1.0.0; the OneScript level is `check_oscript.py` now.

### Exit codes

`check_oscript.py`

| Code | Meaning |
|---|---|
| `0` | no errors; unknown 1C names are not errors |
| `1` | errors: lines `ОШИБКА <file> стр N,M <text>` |
| `2` | a file could not be checked |
| `3` | OneScript 2.2.0-vanteam.2 not found |

`check_bsl.py`

| Code | Meaning |
|---|---|
| `0` | no errors and no warnings |
| `1` | warnings only |
| `2` | errors: a BSL LS finding of level Error (or a syntax error of the legacy OneScript level) |
| `3` | the check did not complete; this is neither a success nor a list of findings |

INFO and HINT findings never raise the code.

`bsl_new_findings.py`

| Code | Meaning |
|---|---|
| `0` | no new ERROR/WARN and no ERROR in the module |
| `1` | new ERROR/WARN, an ERROR in the module, or a OneScript error other than an unknown symbol |
| `2` | the check did not complete, including an unconfirmed OneScript "unknown symbol" |

## Configuration

* **Diagnostics** — [`tools/bsl_ls/.bsl-language-server.json`](tools/bsl_ls/.bsl-language-server.json). With `--source-dir` the wrapper writes a temporary copy with an absolute `configurationRoot`; the original file is never changed.
* **`BSL_LS_JAR`** — path to a specific JAR. A path that does not exist ends with code `3`; it is not silently replaced by the bundled one.
* **`VANTEAM_OSCRIPT`** — path to `oscript.exe` of 2.2.0-vanteam.2 for `check_oscript.py`.
* **Legacy OneScript level of `check_bsl.py`** — looked up at `C:\Program Files\OneScript\bin\oscript.exe`, then on `PATH`.
* **Java** — `tools/jdk21`, then `PATH`, then standard Windows install locations.

## Tests

```shell
python -m unittest tools/tests/test_check_oscript.py -v
python -m unittest tools/tests/test_check_bsl.py -v
```

`test_check_oscript.py` — 8 tests: 19 modules with known error lines in `tools/tests/fixtures/oscript_quality` (syntax, code generator, two errors in one module, valid modules), code under `#Если Сервер`, same-named methods in two branches, an unclosed `#Если`, a duplicate method line, a typo in the manual-check list, a missing engine. Seven of them need OneScript 2.2.0-vanteam.2.

`test_check_bsl.py` — 43 tests. Four integration tests run real Java, BSL LS, OneScript and Git on the fixtures in `tools/tests/fixtures`: syntax errors, and a minimal Designer configuration with two modules for cross-module diagnostics.

Tests that need an engine are skipped when it is missing or `BSL_TESTS_SKIP_INTEGRATION` is set. A skipped integration test is not a pass.

## Repository layout

```
tools/
  check_oscript.py              level 1: OneScript 2.2.0-vanteam.2 -checkall
  check_bsl.py                  level 2: BSL Language Server (--deep)
  bsl_new_findings.py           new findings against a Git revision
  bsl_ls/.bsl-language-server.json
  tests/                        unit and integration tests, fixtures
scripts/fetch_dependencies.py   restores the JAR and the optional JDK
DEPENDENCIES.json               pinned versions, URLs and SHA-256
```

## Versions

| Version | OneScript level | BSL LS | Status |
|---|---|---|---|
| [1.2.0-rc.1](https://github.com/ivanbokhan84/vanteam-bsl-check/tree/release/1.2.0) | `check_oscript.py`, OneScript 2.2.0-vanteam.2 | fork 1.0.7-vanteam.1 | release candidate: fork JAR with `--target`, platform-context cache, optional AppCDS; one module 7 s against 24 s with upstream 1.0.7, identical findings; independent review pending |
| `main` | `check_oscript.py`, OneScript 2.2.0-vanteam.2 | 0.29.0 | 1.0.0 with `check_oscript.py`, see [CHANGELOG.md](CHANGELOG.md) |
| [1.0.0](https://github.com/ivanbokhan84/vanteam-bsl-check/tree/v1.0.0) | `check_bsl.py`, `oscript -check` | 0.29.0 | stable, independently reviewed |
| [1.1.0-rc.1](https://github.com/ivanbokhan84/vanteam-bsl-check/tree/release/1.1.0) | `check_bsl.py`, `oscript -check` | 1.0.7 | release candidate, independent review pending |

## Third-party software

Nothing third-party is stored in this repository. The fetch script downloads:

* BSL Language Server — LGPL-3.0-or-later, © the 1c-syntax contributors, used unmodified as a separate program;
* Eclipse Temurin JDK — GPL-2.0 with the Classpath Exception, optional.

OneScript (MPL-2.0) is built separately from the fork [ivanbokhan84/OneScript](https://github.com/ivanbokhan84/OneScript), which keeps the upstream license and copyright notices.

## License

MIT — see [LICENSE](LICENSE).

## Credits

* [BSL Language Server](https://github.com/1c-syntax/bsl-language-server) by the 1c-syntax community; Vanteam fork: [ivanbokhan84/bsl-language-server](https://github.com/ivanbokhan84/bsl-language-server).
* [OneScript](https://github.com/EvilBeaver/OneScript) by EvilBeaver and the OneScript contributors; Vanteam fork: [ivanbokhan84/OneScript](https://github.com/ivanbokhan84/OneScript).
