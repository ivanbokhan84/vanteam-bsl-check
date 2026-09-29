# BSL Language Server files

* `.bsl-language-server.json` — the diagnostics configuration used by `check_bsl.py`.
* `bsl-language-server-<version>-exec.jar` — the engine, restored by `scripts/fetch_dependencies.py`, not stored in Git. The version and SHA-256 are pinned in `../../DEPENDENCIES.json`.
* `_tmp/` — temporary files of analysis runs, safe to delete.
* `_cache/` — the platform-context cache of the fork JAR (`BSL_LS_CACHE` overrides), safe to delete: the next run parses the syntax helper again.
* `cds/<jar>/` — the unpacked JAR and AppCDS archive made by `scripts/prepare_cds.py`, with `stamp.json` of the JAR and JDK they fit.

The configuration keeps the project rules. `Typo` and `UsingServiceTag` are disabled with a boolean `false`: with the object form `{"enabled": false}` BSL Language Server keeps the rule on.

When moving to a new engine version, rerun the full test suite, including the integration tests, and compare the complete diagnostics by full file path before replacing the JAR.
