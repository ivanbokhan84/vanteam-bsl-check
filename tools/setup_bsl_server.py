#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
setup_bsl_server.py - установка и проверка движка VANTEAM BSL Server check на машине.

Идемпотентно: при тех же JAR, JDK и справке повторный запуск ничего не скачивает, не распаковывает
и не обучает заново, а только сверяет установку.

  python setup_bsl_server.py                                   # в VANTEAM_BSL_HOME или %LOCALAPPDATA%\\vanteam-bsl-server
  python setup_bsl_server.py --home D:\\vanteam\\bsl-server --java D:\\Java\\jdk-21.0.12.1+1
  python setup_bsl_server.py --jar <bsl-language-server-...-exec.jar>   # JAR с диска вместо скачивания
  python setup_bsl_server.py --platform-bin "C:\\Program Files\\1cv8\\8.3.27.1719\\bin"
  python setup_bsl_server.py --retrain-cds                     # переобучить архив CDS принудительно
  python setup_bsl_server.py --check                           # только проверка, установка не меняется

Шаги установки:
  1. JAR релиза v1.0.7-vanteam.1 (скачивание со сверкой размера и SHA-256) или --jar;
  2. распаковка (java -Djarmode=tools -jar <jar> extract);
  3. каталог справки только на русском: копии shcntx_ru.hbk, shlang_ru.hbk, shlang_root.hbk из bin самой
     свежей установленной 1С или из --platform-bin;
  4. конфиг BSL LS по умолчанию, каталоги кэша справки и временных файлов;
  5. AppCDS: прогрев кэша справки, обучение (-XX:ArchiveClassesAtExit) на фикстуре поставки и сверка
     отображения (-Xlog:class+load): доля классов из архива не меньше 80%, иначе предупреждение и работа без CDS.
     Архив переобучается при смене JAR или сборки JDK;
  6. install.json: версии, SHA-256, пути, дата обучения CDS.
--check сверяет файлы и SHA-256 и выполняет один анализ фикстуры (как обычная проверка); файлы установки
не меняются. Кэш справки при этом может быть записан, если он пуст, - как при любой проверке.

Код возврата: 0 - установка исправна (предупреждения допустимы), 1 - есть проблемы, 3 - неверные аргументы.
"""
import argparse
import datetime
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS_DIR))
import check_bsl  # noqa: E402

RELEASE_VERSION = "1.0.7-vanteam.1"
RELEASE_NAME = f"bsl-language-server-{RELEASE_VERSION}-exec.jar"
RELEASE_URL = ("https://github.com/ivanbokhan84/bsl-language-server/releases/download/"
               f"v{RELEASE_VERSION}/{RELEASE_NAME}")
RELEASE_SHA256 = "0b75fa1235513d970a4125f172dd4515e82b8f30a3f80836991f530ef02c205b"
RELEASE_SIZE = 130710342
# Конфиг по умолчанию - побайтная копия конфига обёртки 1.1.0.
CONFIG_SHA256 = "aa0942c87aad5985f67c1e5a5d417cdca50e614a4ef636d18809b24fcfe63fd7"
HELP_FILES = ("shcntx_ru.hbk", "shlang_ru.hbk", "shlang_root.hbk")
CDS_MIN_SHARE = 0.80
FIXTURES = TOOLS_DIR / "tests" / "fixtures" / "bsl_check"
# Ожидаемые находки фикстуры errors: реальные ошибки BSL, не зависят от справки платформы.
ERRORS_EXPECTED = {"ProcedureReturnsValue", "ParseError", "UnreachableCode"}
RUN_TIMEOUT_SEC = 600
EXTRACT_TIMEOUT_SEC = 300


class SetupError(Exception):
    pass


def log(text=""):
    print(text, flush=True)


def now():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def rel(home, path):
    return Path(path).relative_to(home).as_posix()


def write_json(path, data):
    """Атомарная запись: читатель видит либо старый, либо новый файл целиком."""
    part = path.with_name(path.name + ".part")
    part.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(part, path)


def remove_tree(path):
    """Удаление без исключений: файл может держать работающая проверка, тогда он останется до следующего раза."""
    def unlock(function, name, _):
        try:
            os.chmod(name, 0o666)
            function(name)
        except OSError:
            pass
    if Path(path).is_dir():
        shutil.rmtree(path, onerror=unlock)
    elif Path(path).exists():
        try:
            os.chmod(path, 0o666)
            Path(path).unlink()
        except OSError:
            pass


# ===== Java =====
def java_from_argument(value):
    path = Path(value)
    return check_bsl.java_executable(path) if path.is_dir() else path


def java_candidates(install, explicit):
    if explicit:
        yield str(java_from_argument(explicit)), "--java"
        return
    if os.environ.get("VANTEAM_BSL_JAVA"):
        yield os.environ["VANTEAM_BSL_JAVA"], "VANTEAM_BSL_JAVA"
    recorded = ((install or {}).get("java") or {}).get("path")
    if recorded:
        yield recorded, "JDK установки"
    if os.environ.get("JAVA_HOME"):
        yield str(check_bsl.java_executable(os.environ["JAVA_HOME"])), "JAVA_HOME"
    which = shutil.which("java")
    if which:
        yield which, "PATH"
    for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramW6432")):
        if not base:
            continue
        for vendor in ("Eclipse Adoptium", "Java", "Microsoft", "Zulu", "BellSoft"):
            for home in sorted(Path(base, vendor).glob("jdk-2*"), reverse=True):
                yield str(check_bsl.java_executable(home)), f"{base}\\{vendor}"


def select_java(install, explicit):
    """Первая Java 21+ из кандидатов; --java - только она."""
    rejected = []
    for path, source in java_candidates(install, explicit):
        if not Path(path).is_file():
            rejected.append(f"{path} ({source}): файла нет")
            continue
        major, version = check_bsl.java_version(path)
        if major is None or major < check_bsl.MINIMUM_JAVA:
            rejected.append(f"{path} ({source}): Java {version or '?'}")
            continue
        return {"path": str(Path(path).resolve()), "source": source, "version": version,
                "fingerprint": check_bsl.jvm_fingerprint(path)}
    raise SetupError(f"нет Java {check_bsl.MINIMUM_JAVA}+ (укажите --java <JDK>): " + "; ".join(rejected or ["ничего"]))


# ===== JAR =====
def download(url, dest, expected_sha, expected_size):
    part = dest.with_name(dest.name + ".part")
    digest, done, shown = hashlib.sha256(), 0, -1
    log(f"  скачивание {url}")
    with urllib.request.urlopen(url, timeout=120) as response, open(part, "wb") as stream:
        total = int(response.headers.get("Content-Length") or expected_size or 0)
        for chunk in iter(lambda: response.read(1 << 20), b""):
            stream.write(chunk)
            digest.update(chunk)
            done += len(chunk)
            if total and done * 10 // total != shown:
                shown = done * 10 // total
                log(f"  {done / 2**20:.0f} из {total / 2**20:.0f} МиБ")
    if (expected_size and done != expected_size) or digest.hexdigest() != expected_sha:
        part.unlink()
        raise SetupError(f"скачанный JAR не совпал с релизом: {done} байт, sha256 {digest.hexdigest()} "
                         f"(ожидалось {expected_size} байт, {expected_sha})")
    os.replace(part, dest)


def ensure_jar(home, install, jar_argument):
    """Запись engine: JAR в engine/<sha12>/<имя>. Скачивание или копия --jar только при отличии."""
    record = (install or {}).get("engine") or {}
    if jar_argument:
        source = Path(jar_argument).resolve()
        if not source.is_file():
            raise SetupError(f"--jar {source}: файла нет")
        sha, name, origin = sha256(source), source.name, str(source)
    else:
        sha, name, origin = RELEASE_SHA256, RELEASE_NAME, RELEASE_URL
    dest = home / "engine" / sha[:12] / name
    if (record.get("sha256") == sha and record.get("jar") == rel(home, dest) and dest.is_file()
            and record.get("stamp") == check_bsl.file_stamp(dest)):
        log(f"JAR: без изменений, {dest}")
        return record, False
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.is_file() and sha256(dest) == sha:
        log(f"JAR: уже на месте, {dest}")
    elif jar_argument:
        part = dest.with_name(dest.name + ".part")
        shutil.copy2(source, part)
        if sha256(part) != sha:
            part.unlink()
            raise SetupError(f"копия {source} не совпала по SHA-256")
        os.replace(part, dest)
        log(f"JAR: скопирован {source} -> {dest}")
    else:
        download(RELEASE_URL, dest, RELEASE_SHA256, RELEASE_SIZE)
        log(f"JAR: скачан и сверен, {dest}")
    pinned = sha == RELEASE_SHA256
    if not pinned:
        log(f"  ПРЕДУПРЕЖДЕНИЕ: SHA-256 {sha} не совпадает с релизом {RELEASE_VERSION}: установлен другой JAR")
    record = {"version": RELEASE_VERSION if pinned else check_bsl.jar_version(dest), "jar": rel(home, dest),
              "sha256": sha, "size": dest.stat().st_size, "stamp": check_bsl.file_stamp(dest),
              "source": origin, "release": pinned}
    return record, True


def ensure_layout(home, install, engine_record, java):
    """Распакованный layout рядом с JAR: engine/<sha12>/extracted."""
    record = (install or {}).get("layout") or {}
    fat = home / engine_record["jar"]
    dest = fat.parent / "extracted"
    main = dest / fat.name
    libs = len(list((dest / "lib").glob("*.jar"))) if (dest / "lib").is_dir() else 0
    if (record.get("from_sha256") == engine_record["sha256"] and main.is_file()
            and record.get("stamp") == check_bsl.file_stamp(main) and record.get("libs") == libs):
        log(f"Распаковка: без изменений, {dest} (lib: {libs})")
        return record, False
    staging = fat.parent / "extracted.new"
    remove_tree(staging)
    cmd = [java["path"], "-Djarmode=tools", "-jar", str(fat), "extract", "--destination", str(staging)]
    log("Распаковка: " + subprocess.list2cmdline(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                            timeout=EXTRACT_TIMEOUT_SEC)
    staged_main = staging / fat.name
    libs = len(list((staging / "lib").glob("*.jar"))) if (staging / "lib").is_dir() else 0
    if result.returncode != 0 or not staged_main.is_file() or not libs:
        raise SetupError(f"распаковка не удалась (код {result.returncode}, lib: {libs}):\n"
                         f"{(result.stdout + result.stderr)[-2000:]}")
    remove_tree(dest)
    if dest.exists():
        raise SetupError(f"{dest} занят (идёт проверка?): повторите установку позже")
    os.replace(staging, dest)
    record = {"dir": rel(home, dest), "jar": rel(home, main), "stamp": check_bsl.file_stamp(main),
              "libs": libs, "from_sha256": engine_record["sha256"], "extracted_at": now()}
    log(f"Распаковка: {dest} (lib: {libs})")
    return record, True


# ===== Справка платформы =====
def version_key(text):
    return tuple(int(part) for part in re.findall(r"\d+", text)) or (0,)


def platform_bins():
    """Каталоги bin установленных платформ 1С со справкой, от свежей к старой."""
    roots = {os.environ.get("ProgramFiles"), os.environ.get("ProgramW6432"), os.environ.get("ProgramFiles(x86)"),
             r"C:\Program Files", r"C:\Program Files (x86)"}
    found = {}
    for root in filter(None, roots):
        for version_dir in Path(root, "1cv8").glob("*"):
            bin_dir = version_dir / "bin"
            if re.match(r"^\d+(\.\d+)+$", version_dir.name) and all((bin_dir / n).is_file() for n in HELP_FILES):
                found.setdefault(str(bin_dir).lower(), (version_key(version_dir.name), version_dir.name, bin_dir))
    return [(name, path) for _, name, path in sorted(found.values(), reverse=True)]


def ensure_help(home, install, platform_bin, disabled):
    """Каталог help/<версия> с копиями трёх файлов справки; None - справка установки не используется."""
    if disabled:
        log("Справка: не используется (--no-help), BSL LS сам найдёт установленную 1С")
        return None, False
    if platform_bin:
        bin_dir = Path(platform_bin).resolve()
        missing = [n for n in HELP_FILES if not (bin_dir / n).is_file()]
        if missing:
            raise SetupError(f"--platform-bin {bin_dir}: нет {', '.join(missing)}")
        version = bin_dir.parent.name if re.match(r"^\d+(\.\d+)+$", bin_dir.parent.name) else "custom"
    else:
        bins = platform_bins()
        if not bins:
            log("Справка: установленная 1С не найдена, BSL LS будет использовать встроенные описания")
            return None, False
        version, bin_dir = bins[0]
    dest = home / "help" / version
    record = (install or {}).get("help") or {}
    same = all((dest / n).is_file() and check_bsl.file_stamp(dest / n) == check_bsl.file_stamp(bin_dir / n)
               for n in HELP_FILES)
    if same and record.get("dir") == rel(home, dest) and record.get("source") == str(bin_dir):
        log(f"Справка: без изменений, {dest}")
        return record, False
    dest.mkdir(parents=True, exist_ok=True)
    for name in HELP_FILES:
        # copy2 сохраняет время изменения: оно входит в ключ кэша справки форка.
        shutil.copy2(bin_dir / name, dest / name)
    files = {n: {"size": (dest / n).stat().st_size, "sha256": sha256(dest / n)} for n in HELP_FILES}
    log(f"Справка: {bin_dir} -> {dest} ({', '.join(HELP_FILES)})")
    return {"dir": rel(home, dest), "platform_version": version, "source": str(bin_dir), "files": files,
            "copied_at": now()}, True


def ensure_config(home):
    source = check_bsl.BUNDLED_CONFIG
    if sha256(source) != CONFIG_SHA256:
        raise SetupError(f"{source}: SHA-256 не совпадает с конфигом по умолчанию {CONFIG_SHA256[:12]}…")
    dest = home / "config" / check_bsl.CONFIG_NAME
    if dest.is_file() and dest.read_bytes() == source.read_bytes():
        return rel(home, dest), False
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, dest)
    log(f"Конфиг по умолчанию: {dest}")
    return rel(home, dest), True


# ===== Запуски движка: прогрев, обучение и сверка CDS =====
def fixture_workspace(work):
    """Копия фикстуры поставки: выгрузка xmod (metadata, межмодульные вызовы) и модуль с ошибками."""
    source = work / "fixture"
    shutil.copytree(FIXTURES / "xmod", source)
    (source / "Extra").mkdir()
    shutil.copy2(FIXTURES / "errors" / "Module.bsl", source / "Extra" / "Module.bsl")
    targets = [source / "CommonModules" / "Поставщик" / "Ext" / "Module.bsl",
               source / "CommonModules" / "Потребитель" / "Ext" / "Module.bsl",
               source / "Extra" / "Module.bsl"]
    return source, targets


def run_fixture(engine, work, extra_jvm=()):
    """Анализ фикстуры той же командой, что и проверка; (находки по целям, вывод JVM, секунды)."""
    work.mkdir(parents=True, exist_ok=True)
    src_dir, targets = fixture_workspace(work)
    report_dir, java_tmp = work / "report", work / "java"
    report_dir.mkdir()
    java_tmp.mkdir()
    settings, _ = check_bsl.build_settings(check_bsl.BUNDLED_CONFIG, engine, src_dir)
    config = work / "configuration.json"
    config.write_text(json.dumps(settings, ensure_ascii=False, indent=1), encoding="utf-8")
    cmd = check_bsl.bsl_ls_command(engine, src_dir, targets, java_tmp, report_dir, config,
                                   check_bsl.XMX_DEFAULT, extra_jvm)
    started = time.monotonic()
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                cwd=str(src_dir), timeout=RUN_TIMEOUT_SEC)
    except subprocess.TimeoutExpired:
        raise SetupError(f"анализ фикстуры дольше {RUN_TIMEOUT_SEC} с") from None
    seconds = time.monotonic() - started
    output = (result.stdout or "") + (result.stderr or "")
    if result.returncode != 0:
        raise SetupError(f"анализ фикстуры: код JVM {result.returncode}\n{check_bsl.output_tail(output, 3000)}")
    marker = check_bsl.engine_failure(output)
    if marker:
        raise SetupError(f"анализ фикстуры: «{marker}»\n{check_bsl.output_tail(output, 3000)}")
    reports = list(report_dir.glob("*.json"))
    if len(reports) != 1:
        raise SetupError(f"анализ фикстуры: отчётов {len(reports)}")
    data = json.loads(reports[0].read_text(encoding="utf-8"))
    findings = {}
    for target in targets:
        try:
            diagnostics, mdo_ref = check_bsl.target_findings(data, target, src_dir)
        except ValueError as error:
            raise SetupError(f"анализ фикстуры: {error}") from None
        findings[target.relative_to(src_dir).as_posix()] = {
            # mdoRef модуля без metadata - URI файла с путём рабочего каталога прогона: для сверки не важен.
            "mdoRef": "file" if str(mdo_ref).startswith("file:") else mdo_ref,
            "findings": sorted(json.dumps(check_bsl.finding(d), ensure_ascii=False, sort_keys=True)
                               for d in diagnostics)}
    codes = {json.loads(f)["code"] for f in findings["Extra/Module.bsl"]["findings"]}
    if not ERRORS_EXPECTED <= codes:
        raise SetupError(f"анализ фикстуры: в модуле с ошибками нет {sorted(ERRORS_EXPECTED - codes)}")
    consumer = findings["CommonModules/Потребитель/Ext/Module.bsl"]
    if consumer["mdoRef"] != "CommonModule.Потребитель":
        raise SetupError(f"анализ фикстуры: metadata xmod не загружены (mdoRef {consumer['mdoRef']!r})")
    return findings, output, seconds


def class_share(log_path):
    lines = Path(log_path).read_text(encoding="utf-8", errors="replace").splitlines()
    loaded = [line for line in lines if "[class,load]" in line]
    shared = sum("source: shared objects file" in line for line in loaded)
    return {"classes": len(loaded), "from_archive": shared, "share": round(shared / max(len(loaded), 1), 3)}


def runtime_engine(home, java, jar, help_record, cache_dir, archive=None):
    return {"java": java["path"], "jar": str(jar), "cache_dir": str(cache_dir), "cds_archive": archive,
            "help_dir": str(home / help_record["dir"]) if help_record else None}


def train_cds(home, java, engine_record, layout_record, help_record, cache_dir, tmp_root):
    """Прогрев кэша, обучение и сверка архива. Запись cds: status ok | disabled | failed."""
    jar = home / layout_record["jar"]
    engine = runtime_engine(home, java, jar, help_record, cache_dir)
    tag = hashlib.sha256(json.dumps(java["fingerprint"], sort_keys=True).encode()).hexdigest()[:8]
    archive = home / "cds" / f"bsl-ls-{engine_record['sha256'][:12]}-{tag}.jsa"
    archive.parent.mkdir(parents=True, exist_ok=True)
    fresh = archive.with_name(archive.stem + ".new.jsa")
    remove_tree(fresh)
    record = {"archive": rel(home, archive), "jar_sha256": engine_record["sha256"], "java": java["fingerprint"],
              "trained_at": now()}
    with tempfile.TemporaryDirectory(dir=str(tmp_root), prefix="setup_", ignore_cleanup_errors=True) as tmp:
        tmp = Path(tmp)
        try:
            log("CDS: прогрев кэша справки (анализ фикстуры без архива)")
            plain, _, seconds = run_fixture(engine, tmp / "prime")
            log(f"  {seconds:.1f} с")
            log("CDS: обучение (-XX:ArchiveClassesAtExit)")
            _, _, seconds = run_fixture(engine, tmp / "train", [f"-XX:ArchiveClassesAtExit={fresh}"])
            log(f"  {seconds:.1f} с")
            if not fresh.is_file():
                raise SetupError("JVM не создала архив CDS")
            log("CDS: сверка отображения (-Xlog:class+load)")
            share, same = verify_archive(engine, fresh, tmp / "verify", plain)
        except SetupError as error:
            remove_tree(fresh)
            log(f"  ПРЕДУПРЕЖДЕНИЕ: CDS не обучен: {error}")
            return dict(record, status="failed", reason=f"обучение не удалось: {str(error).splitlines()[0]}")
    record.update(share)
    if not same:
        remove_tree(fresh)
        log("  ПРЕДУПРЕЖДЕНИЕ: находки с архивом и без него различаются - CDS отключён")
        return dict(record, status="disabled", reason="находки с архивом отличаются от находок без архива")
    if share["share"] < CDS_MIN_SHARE:
        remove_tree(fresh)
        log(f"  ПРЕДУПРЕЖДЕНИЕ: из архива {share['share']:.0%} классов (< {CDS_MIN_SHARE:.0%}) - работа без CDS")
        return dict(record, status="disabled",
                    reason=f"из архива {share['share']:.0%} классов, меньше {CDS_MIN_SHARE:.0%}")
    remove_tree(archive)
    os.replace(fresh, archive)
    log(f"  из архива {share['from_archive']} из {share['classes']} классов ({share['share']:.1%}), "
        f"{archive} {archive.stat().st_size / 2**20:.0f} МиБ")
    return dict(record, status="ok", size=archive.stat().st_size)


def verify_archive(engine, archive, work, expected=None):
    """Анализ с архивом и журналом загрузки классов: (доля из архива, находки совпали с expected)."""
    work.mkdir(parents=True, exist_ok=True)
    class_log = work / "classload.log"
    engine = dict(engine, cds_archive=str(archive))
    findings, output, seconds = run_fixture(engine, work / "run", [f"-Xlog:class+load=info:file={class_log}"])
    share = class_share(class_log)
    share["seconds"] = round(seconds, 1)
    problem = check_bsl.CDS_PROBLEM.search(output)
    if problem:
        share["warning"] = problem.group(0)
    return share, (expected is None or findings == expected)


def cds_needs_training(home, install, engine_record, java, force, layout_changed):
    cds = (install or {}).get("cds") or {}
    if force:
        return "указан --retrain-cds"
    if not cds:
        return "архива ещё нет"
    if layout_changed:
        return "JAR распакован заново"
    if cds.get("jar_sha256") != engine_record["sha256"]:
        return "сменился JAR"
    if cds.get("java") != java["fingerprint"]:
        return "сменилась сборка JDK"
    if cds.get("status") == "failed":
        return "прошлое обучение не удалось"
    if cds.get("status") == "ok" and not (home / cds.get("archive", "")).is_file():
        return "нет файла архива"
    return None


def cleanup(home, install):
    """Удалить неиспользуемые версии движка, архивы CDS, справку и старые временные каталоги."""
    keep = {Path(home / install["engine"]["jar"]).parent.resolve()}
    for path in (home / "engine").glob("*"):
        if path.resolve() not in keep:
            remove_tree(path)
    archive = (install.get("cds") or {}).get("archive")
    for path in (home / "cds").glob("*.jsa"):
        if not archive or path.resolve() != (home / archive).resolve():
            remove_tree(path)
    help_dir = (install.get("help") or {}).get("dir")
    for path in (home / "help").glob("*"):
        if not help_dir or path.resolve() != (home / help_dir).resolve():
            remove_tree(path)
    stale = time.time() - 24 * 3600
    for path in (home / "tmp").glob("*"):
        try:
            if path.stat().st_mtime < stale:
                remove_tree(path)
        except OSError:
            pass


def user_env(name):
    """Значение переменной пользователя из реестра (то, что задаёт setx), или None."""
    if os.name != "nt":
        return None
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            return winreg.QueryValueEx(key, name)[0]
    except OSError:
        return None


# ===== Установка =====
def install_mode(args, home):
    home.mkdir(parents=True, exist_ok=True)
    if not str(home).isascii():
        log(f"ПРЕДУПРЕЖДЕНИЕ: путь установки {home} не ASCII: JVM и JLine надёжнее работают с ASCII-путём "
            "(задайте --home, например D:\\vanteam\\bsl-server)")
    install = check_bsl.load_install(home)
    java = select_java(install, args.java)
    log(f"Java: {java['path']} ({java['source']}), версия {java['version']}")
    engine_record, _ = ensure_jar(home, install, args.jar)
    layout_record, layout_changed = ensure_layout(home, install, engine_record, java)
    help_record, _ = ensure_help(home, install, args.platform_bin, args.no_help)
    config, _ = ensure_config(home)
    cache_dir, tmp_root = home / "cache", home / "tmp"
    cache_dir.mkdir(exist_ok=True)
    tmp_root.mkdir(exist_ok=True)
    reason = cds_needs_training(home, install, engine_record, java, args.retrain_cds, layout_changed)
    if reason:
        log(f"CDS: обучение, причина - {reason}")
        cds = train_cds(home, java, engine_record, layout_record, help_record, cache_dir, tmp_root)
    else:
        cds = install["cds"]
        log(f"CDS: без изменений ({cds.get('status')}, обучен {cds.get('trained_at')})")
    java_record = {k: java[k] for k in ("path", "version", "fingerprint")}
    new = {
        "product": check_bsl.PRODUCT, "checker_version": check_bsl.VERSION,
        "installed_at": (install or {}).get("installed_at") or now(), "updated_at": now(),
        "engine": engine_record, "layout": layout_record, "java": java_record, "cds": cds, "help": help_record,
        "config": config, "config_sha256": CONFIG_SHA256, "cache_dir": "cache", "tmp_root": "tmp",
    }
    if install and all(install.get(k) == new[k] for k in new if k not in ("updated_at",)):
        log("install.json: без изменений")
    else:
        write_json(home / check_bsl.INSTALL_FILE, new)
        log(f"install.json: записан {home / check_bsl.INSTALL_FILE}")
    cleanup(home, new)
    env_value = os.environ.get("VANTEAM_BSL_HOME") or user_env("VANTEAM_BSL_HOME")
    default_home = Path(os.environ.get("LOCALAPPDATA") or "") / "vanteam-bsl-server"
    if (env_value and Path(env_value).resolve() != home.resolve()) or (
            not env_value and home.resolve() != default_home.resolve()):
        log(f"Для проверки задайте переменную пользователя: setx VANTEAM_BSL_HOME \"{home}\"")
    log(f"Готово: {home}, CDS {cds.get('status')}")
    return 0


# ===== Проверка =====
class Report:
    def __init__(self):
        self.problems, self.warnings = 0, 0

    def ok(self, text):
        log(f"  OK    {text}")

    def warn(self, text):
        self.warnings += 1
        log(f"  WARN  {text}")

    def fail(self, text):
        self.problems += 1
        log(f"  FAIL  {text}")


def check_mode(args, home):
    report = Report()
    log(f"Проверка установки {home}")
    try:
        install = check_bsl.load_install(home)
    except check_bsl.CheckError as error:
        install = None
        report.fail(str(error))
    if install is None:
        report.fail(f"нет {home / check_bsl.INSTALL_FILE}: выполните setup_bsl_server.py")
        return finish(report)
    report.ok(f"install.json: проверка {install.get('checker_version')}, обновлён {install.get('updated_at')}")
    engine_record = install.get("engine") or {}
    jar = home / engine_record.get("jar", "-")
    if jar.is_file() and sha256(jar) == engine_record.get("sha256"):
        text = f"JAR {engine_record.get('version')}: SHA-256 {engine_record['sha256'][:12]}… совпадает"
        if engine_record["sha256"] == RELEASE_SHA256:
            report.ok(text + f" (релиз {RELEASE_VERSION})")
        else:
            report.warn(text + f", но это не релиз {RELEASE_VERSION}")
    else:
        report.fail(f"JAR {jar}: нет файла или SHA-256 не совпадает с install.json")
    layout = install.get("layout") or {}
    main = home / layout.get("jar", "-")
    libs = len(list((main.parent / "lib").glob("*.jar"))) if (main.parent / "lib").is_dir() else 0
    if main.is_file() and check_bsl.file_stamp(main) == layout.get("stamp") and libs == layout.get("libs"):
        report.ok(f"распакованный JAR: {main} (lib: {libs})")
    else:
        report.warn(f"распакованный JAR {main}: нет или изменён (lib: {libs}) - проверка пойдёт на fat JAR")
    java_record = install.get("java") or {}
    java_ok = False
    if java_record.get("path") and Path(java_record["path"]).is_file():
        major, version = check_bsl.java_version(java_record["path"])
        java_ok = major is not None and major >= check_bsl.MINIMUM_JAVA
        (report.ok if java_ok else report.fail)(f"JDK установки: {java_record['path']}, Java {version}")
    else:
        report.warn(f"JDK установки {java_record.get('path')}: нет файла")
    try:
        selected, source = check_bsl.find_java(install)
        report.ok(f"проверка возьмёт Java: {selected} ({source})")
    except check_bsl.CheckError as error:
        selected = None
        report.fail(f"проверка не найдёт Java: {error}")
    cds = install.get("cds") or {}
    reason = check_bsl.cds_unusable_reason(home, install, selected) if selected else "нет Java"
    if reason is None:
        report.ok(f"CDS: {cds.get('archive')}, обучен {cds.get('trained_at')}, из архива {cds.get('share', 0):.1%}")
    else:
        report.warn(f"CDS не используется: {reason} (setup_bsl_server.py переобучит)")
    help_record = install.get("help")
    if help_record:
        help_dir = home / help_record.get("dir", "-")
        bad = [n for n, info in (help_record.get("files") or {}).items()
               if not (help_dir / n).is_file() or sha256(help_dir / n) != info.get("sha256")]
        if bad or set(help_record.get("files") or {}) != set(HELP_FILES):
            report.fail(f"справка {help_dir}: не совпадают {bad or 'состав файлов'}")
        else:
            report.ok(f"справка только ru: {help_dir} (1С {help_record.get('platform_version')})")
        bins = platform_bins()
        if bins and version_key(bins[0][0]) > version_key(help_record.get("platform_version", "")):
            report.warn(f"установлена более свежая 1С {bins[0][0]}: справка установки от "
                        f"{help_record.get('platform_version')} - выполните setup_bsl_server.py")
    else:
        report.warn("каталога справки нет: BSL LS сам ищет установленную 1С")
    config = home / install.get("config", "-")
    if config.is_file() and sha256(config) == CONFIG_SHA256:
        report.ok(f"конфиг по умолчанию: {config} (sha256 {CONFIG_SHA256[:12]}…)")
    else:
        report.fail(f"конфиг по умолчанию {config}: нет или SHA-256 не {CONFIG_SHA256[:12]}…")
    for key in ("cache_dir", "tmp_root"):
        path = home / install.get(key, "-")
        (report.ok if path.is_dir() else report.fail)(f"каталог {key}: {path}")
    env_value, user_value = os.environ.get("VANTEAM_BSL_HOME"), user_env("VANTEAM_BSL_HOME")
    for label, value in (("процесса", env_value), ("пользователя (setx)", user_value)):
        if value and Path(value).resolve() == home.resolve():
            report.ok(f"VANTEAM_BSL_HOME {label} = {value}")
        elif os.name == "nt" or label == "процесса":
            report.warn(f"VANTEAM_BSL_HOME {label} = {value!r}, не {home}")
    if args.no_run:
        log("  анализ фикстуры пропущен (--no-run)")
    elif selected and jar.is_file():
        smoke(report, home, install, selected, reason is None)
    return finish(report)


def smoke(report, home, install, java_path, use_cds):
    """Анализ фикстуры командой проверки; при CDS - с журналом загрузки классов."""
    help_record = install.get("help")
    jar = home / (install["layout"]["jar"] if use_cds else install["engine"]["jar"])
    engine = runtime_engine(home, {"path": java_path}, jar, help_record, home / install["cache_dir"])
    tmp_root = home / install["tmp_root"]
    try:
        with tempfile.TemporaryDirectory(dir=str(tmp_root), prefix="check_", ignore_cleanup_errors=True) as tmp:
            if use_cds:
                share, _ = verify_archive(engine, home / install["cds"]["archive"], Path(tmp))
                text = (f"анализ фикстуры с CDS: {share['seconds']:.1f} с, из архива {share['from_archive']} "
                        f"из {share['classes']} классов ({share['share']:.1%})")
                if share["share"] >= CDS_MIN_SHARE and "warning" not in share:
                    report.ok(text)
                else:
                    report.warn(text + f"; {share.get('warning', 'меньше порога')}")
            else:
                _, _, seconds = run_fixture(engine, Path(tmp) / "run")
                report.ok(f"анализ фикстуры без CDS: {seconds:.1f} с, находки ожидаемые")
    except SetupError as error:
        report.fail(f"анализ фикстуры: {error}")


def finish(report):
    if report.problems:
        log(f"Итог: есть проблемы ({report.problems}), предупреждений {report.warnings}")
        return 1
    log(f"Итог: установка исправна, предупреждений {report.warnings}")
    return 0


class Parser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        print(f"{self.prog}: ошибка: {message}", file=sys.stderr)
        sys.exit(3)


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    parser = Parser(prog="setup_bsl_server.py", description=__doc__.split("\n\n")[0].strip(),
                    formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--home", help="каталог установки (по умолчанию VANTEAM_BSL_HOME или "
                                       "%%LOCALAPPDATA%%\\vanteam-bsl-server)")
    parser.add_argument("--jar", help="exec.jar движка с диска вместо скачивания релиза")
    parser.add_argument("--java", help="java.exe или каталог JDK 21+")
    parser.add_argument("--platform-bin", help="каталог bin платформы 1С со справкой")
    parser.add_argument("--no-help", action="store_true", help="не создавать каталог справки только ru")
    parser.add_argument("--retrain-cds", action="store_true", help="переобучить архив CDS")
    parser.add_argument("--check", action="store_true", help="только проверка, без изменений")
    parser.add_argument("--no-run", action="store_true", help="при --check не запускать анализ фикстуры")
    args = parser.parse_args(argv)
    home = Path(args.home).resolve() if args.home else check_bsl.install_home().resolve()
    if args.check:
        return check_mode(args, home)
    home.mkdir(parents=True, exist_ok=True)
    lock_path = home / "setup.lock"
    with lock_path.open("a+b") as stream:
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        try:
            check_bsl._try_lock(stream)
        except OSError:
            log(f"setup уже выполняется для {home}")
            return 1
        try:
            return install_mode(args, home)
        except (SetupError, check_bsl.CheckError, OSError, subprocess.SubprocessError) as error:
            log(f"ОШИБКА: {error}")
            return 1
        finally:
            check_bsl._unlock(stream)


if __name__ == "__main__":
    sys.exit(main())
