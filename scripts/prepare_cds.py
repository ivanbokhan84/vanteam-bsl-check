#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Подготовка AppCDS для BSL Language Server: распаковка JAR и обучение архива классов.

AppCDS сокращает запуск JVM (замер BSL Server 29.09.2026 на распакованном JAR: CPU x0,80-0,90,
время x0,70-0,81). Архив годен только для того же JAR и той же сборки JDK; при несовпадении JVM
молча работает без него, поэтому check_bsl.py подключает архив только при совпадении отметки
stamp.json. После смены JAR или JDK скрипт запускается заново.

Запуск из корня проекта:
  python scripts/prepare_cds.py          распаковать, обучить, проверить отображение, записать отметку
  python scripts/prepare_cds.py --check  показать, подключится ли архив
Каталог: tools/bsl_ls/cds/<имя JAR>/ (в git не входит).
"""
import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import check_bsl as checker  # noqa: E402

TRAINING_SOURCE = ROOT / "tools" / "tests" / "fixtures" / "bsl_check" / "errors"


def analyze(java, jar, cds_options, tmp, jvm_extra=()):
    """Короткий --analyze на обучающем каталоге; (код, вывод)."""
    report = Path(tmp) / "report"
    report.mkdir(exist_ok=True)
    cmd = [java, f"-Djava.io.tmpdir={tmp}", "-Xmx512m", *checker.JVM_OPTIONS, *cds_options, *jvm_extra,
           "-jar", str(jar), "--analyze", "--srcDir", str(TRAINING_SOURCE),
           "--reporter", "json", "--outputDir", str(report), "--silent"]
    if checker.BSL_LS_CONFIG.exists():
        cmd += ["--configuration", str(checker.BSL_LS_CONFIG)]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                            cwd=ROOT, timeout=600)
    return result.returncode, (result.stdout or "") + (result.stderr or "")


def main():
    parser = argparse.ArgumentParser(description="AppCDS для BSL Language Server")
    parser.add_argument("--check", action="store_true", help="только проверить отметку")
    args = parser.parse_args()

    jar, java = checker.find_bsl_ls_jar(), checker.find_java()
    if not jar or not java:
        print("нет JAR или Java: сначала python scripts/fetch_dependencies.py [--jdk]")
        return 3
    if args.check:
        layout = checker.cds_layout(jar, java)
        print(f"AppCDS для {Path(jar).name}: {'подключится' if layout else 'нет или устарел'}")
        return 0 if layout else 1

    base = checker.BSL_LS_DIR / "cds" / Path(jar).stem
    if base.exists():
        shutil.rmtree(base)
    base.parent.mkdir(parents=True, exist_ok=True)
    print(f"распаковка {Path(jar).name} -> {base}")
    subprocess.run([java, "-Djarmode=tools", "-jar", str(jar), "extract", "--destination", str(base)],
                   check=True, cwd=ROOT)
    extracted, archive = base / Path(jar).name, base / "bslls.jsa"
    if not extracted.is_file():
        print(f"после распаковки нет {extracted}")
        return 3

    tmp_root = checker.BSL_LS_DIR / "_tmp"
    tmp_root.mkdir(parents=True, exist_ok=True)
    cache = [f"-Dapp.platform-context.cache.path={checker.platform_cache_dir()}"] if checker.is_fork_jar(jar) else []
    with tempfile.TemporaryDirectory(dir=str(tmp_root)) as tmp:
        print("обучение архива (один --analyze)")
        code, output = analyze(java, extracted, [f"-XX:ArchiveClassesAtExit={archive}"], tmp, cache)
        if code != 0 or not archive.is_file():
            print(f"обучение не удалось, код {code}\n{output[-1500:]}")
            shutil.rmtree(base, ignore_errors=True)
            return 3
    with tempfile.TemporaryDirectory(dir=str(tmp_root)) as tmp:
        # -Xshare:on: если архив не отображается, JVM не стартует - иначе он молча не работал бы.
        code, output = analyze(java, extracted, [f"-XX:SharedArchiveFile={archive}", "-Xshare:on"], tmp, cache)
        if code != 0:
            print(f"архив не отображается, код {code}\n{output[-1500:]}")
            shutil.rmtree(base, ignore_errors=True)
            return 3

    stamp = checker.cds_stamp(jar, java)
    if stamp is None:
        print("не удалось снять отметку JAR и JDK")
        return 3
    (base / "stamp.json").write_text(checker.json.dumps(stamp, ensure_ascii=False, indent=1), encoding="utf-8")
    size = archive.stat().st_size / 2**20
    print(f"готово: {archive} ({size:.0f} МиБ), check_bsl.py подключит его для {Path(jar).name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
