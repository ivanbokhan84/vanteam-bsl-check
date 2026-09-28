# BSL Language Server files

* `.bsl-language-server.json` — the diagnostics configuration used by `check_bsl.py`.
* `bsl-language-server-<version>-exec.jar` — the engine, restored by `scripts/fetch_dependencies.py`, not stored in Git. The version and SHA-256 are pinned in `../../DEPENDENCIES.json`.
* `_tmp/` — temporary files of analysis runs, safe to delete.

The configuration keeps the project rules. `Typo` and `UsingServiceTag` are disabled with a boolean `false`: with the object form `{"enabled": false}` BSL Language Server keeps the rule on.

When moving to a new engine version, rerun the full test suite, including the integration tests, and compare the complete diagnostics by full file path before replacing the JAR.
