<h1 align="center">Vanteam BSL Check</h1>

<h4 align="center">A two-level checker for 1C:Enterprise (BSL) modules: a fast OneScript syntax pass and a deep BSL Language Server analysis, with exit codes you can trust</h4>

<div align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-green" alt="License: MIT" /></a>
  <a href="https://www.python.org/"><img src="https://img.shields.io/badge/python-3.10%2B-blue" alt="Python 3.10+" /></a>
  <a href="https://github.com/1c-syntax/bsl-language-server/releases/tag/v0.29.0"><img src="https://img.shields.io/badge/BSL%20Language%20Server-0.29.0-lightgrey" alt="BSL Language Server 0.29.0" /></a>
  <a href="CHANGELOG.md"><img src="https://img.shields.io/badge/release-1.0.0-orange" alt="Release 1.0.0" /></a>
</div>
<br/>

<p align="center"><a href="https://ivanbokhan84.github.io/vanteam-bsl-check/"><img src="docs/cover.png" alt="Vanteam BSL Check: exit codes, tests and measurements" width="100%" /></a></p>

Project page with the test and measurement charts: **[ivanbokhan84.github.io/vanteam-bsl-check](https://ivanbokhan84.github.io/vanteam-bsl-check/)**.

Vanteam BSL Check is a small Python wrapper around two existing tools:

* **[OneScript](https://github.com/EvilBeaver/OneScript)** `oscript -check` — a quick syntax check, about a second per module;
* **[BSL Language Server](https://github.com/1c-syntax/bsl-language-server)** `--analyze` — the full set of diagnostics: cognitive complexity, deprecated methods, unused variables, cross-module calls and more.

Neither engine is modified. The wrapper decides *what* is analysed, runs the engines, and refuses to report success when the analysis did not actually happen: a missing report, a module absent from the report, an unknown severity or a crashed rule all end with a separate "check not completed" code instead of a green result.

Maintained by **Ivan Bokhan**.

## Features

* **Honest results.** Exit code `3` means the check did not complete — the engine is missing, timed out, crashed, printed a known failure message, or produced no usable report. It is never folded into "OK".
* **Explicit analysis scope.** By default BSL LS sees the `.bsl`/`.os` files of the module's own directory; nested folders (for example archives next to the module) are left out. `--source-dir` sets a recursive context and the metadata root for a full Designer/EDT dump; `--standalone` checks the file alone. The scope is printed with every run.
* **Only new findings.** `bsl_new_findings.py` compares a module with its version in Git (default `HEAD`) and prints only the ERROR/WARN findings that appeared. Large legacy modules can carry hundreds of old warnings; the rule "no errors and no *new* warnings" stays usable.
* **One analysis at a time.** An OS-level file lock serialises BSL LS runs per checkout; a second run waits up to 300 s. The lock is released by the OS if its owner crashes.
* **Tuned for short CLI runs.** The JVM runs with `-XX:TieredStopAtLevel=1` and `-XX:ActiveProcessorCount=min(4, CPUs)`. An independent recalculation of the paired measurements on the author's machine (4 cores / 8 threads, pre-release build, four scenarios) gives CPU time ×0.28–0.38 and wall time ×0.63–0.92 of the default JVM; the only diagnostic differences were the two rules disabled on purpose. These settings target one-shot analysis, not a long-running language server in an editor.

## Requirements

| Component | Version | Notes |
|---|---|---|
| Python | 3.10+ | standard library only |
| Java | 21+ | a portable Temurin 21 can be fetched into `tools/jdk21` |
| BSL Language Server | 0.29.0 | `bsl-language-server-0.29.0-exec.jar`, fetched and SHA-256-verified |
| OneScript | 1.9.4 tested | installed separately, see [OneScript version](#onescript-version) |
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

Binaries are downloaded from the official GitHub releases of BSL Language Server and Eclipse Temurin and checked against the SHA-256 values pinned in [`DEPENDENCIES.json`](DEPENDENCIES.json). They are not stored in this repository.

## Usage

```shell
python tools/check_bsl.py path/to/Module.bsl                  # OneScript syntax check
python tools/check_bsl.py path/to/Module.bsl --deep           # BSL Language Server only
python tools/check_bsl.py path/to/Module.bsl --all            # both levels
python tools/check_bsl.py path/to/Module.bsl --all --quiet    # print findings only

python tools/check_bsl.py path/to/Module.bsl --deep --source-dir path/to/dump   # full configuration context
python tools/check_bsl.py path/to/Module.bsl --deep --standalone                # the file alone

python tools/bsl_new_findings.py path/to/Module.bsl            # new findings against HEAD
python tools/bsl_new_findings.py path/to/Module.bsl main~3     # ... against any revision
```

A single module checked without `--source-dir` does not see the rest of the configuration: cross-module and metadata diagnostics need the full dump.

### Exit codes

`check_bsl.py`

| Code | Meaning |
|---|---|
| `0` | no errors and no warnings |
| `1` | warnings only |
| `2` | errors: a BSL LS finding of level Error, or a OneScript syntax error |
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
* **OneScript** — looked up at `C:\Program Files\OneScript\bin\oscript.exe`, then on `PATH`.
* **Java** — `tools/jdk21`, then `PATH`, then standard Windows install locations.

## OneScript version

`oscript -check` stops at the first error. On real 1C modules this is usually `Неизвестный символ` (unknown symbol): the 1C global context — `Документы`, `Справочники`, other common modules — does not exist in OneScript. `check_bsl.py` reports it as an error; `bsl_new_findings.py` treats an unconfirmed unknown symbol as an incomplete check, because it cannot tell a missing 1C global from a typo in a local variable.

OneScript **2.2.0** has a preprocessor bug: inside an inactive `#Если` branch, a `#` in a string or a comment is taken for a directive. `"#000000"` in an HTML colour becomes a false syntax error, and `// #КонецЕсли` can hide a missing `#КонецЕсли`. OneScript 1.9.4 is not affected. A fixed 2.2.0 build is available from the fork [ivanbokhan84/OneScript](https://github.com/ivanbokhan84/OneScript).

## Tests

```shell
python -m unittest tools/tests/test_check_bsl.py -v
```

43 tests. Four integration tests run real Java, BSL LS, OneScript and Git on the fixtures in `tools/tests/fixtures`: syntax errors, and a minimal Designer configuration with two modules for cross-module diagnostics. They are skipped when a tool is missing or `BSL_TESTS_SKIP_INTEGRATION` is set. A skipped integration test is not a pass.

## Repository layout

```
tools/
  check_bsl.py                  two-level checker
  bsl_new_findings.py           new findings against a Git revision
  bsl_ls/.bsl-language-server.json
  tests/                        unit and integration tests, fixtures
scripts/fetch_dependencies.py   restores the JAR and the optional JDK
DEPENDENCIES.json               pinned versions, URLs and SHA-256
```

## Versions

| Version | BSL LS | Status |
|---|---|---|
| [1.0.0](https://github.com/ivanbokhan84/vanteam-bsl-check/tree/v1.0.0) | 0.29.0 | stable, independently reviewed, `main` |
| [1.1.0-rc.1](https://github.com/ivanbokhan84/vanteam-bsl-check/tree/release/1.1.0) | 1.0.7 | release candidate, independent review pending |

See [CHANGELOG.md](CHANGELOG.md).

## Third-party software

Nothing third-party is stored in this repository. The fetch script downloads:

* BSL Language Server — LGPL-3.0-or-later, © the 1c-syntax contributors, used unmodified as a separate program;
* Eclipse Temurin JDK — GPL-2.0 with the Classpath Exception, optional.

OneScript (MPL-2.0) is installed separately.

## License

MIT — see [LICENSE](LICENSE).

## Credits

* [BSL Language Server](https://github.com/1c-syntax/bsl-language-server) by the 1c-syntax community.
* [OneScript](https://github.com/EvilBeaver/OneScript) by EvilBeaver and the OneScript contributors.
