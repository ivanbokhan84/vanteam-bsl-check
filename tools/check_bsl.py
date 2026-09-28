#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
check_bsl.py — двухуровневая проверка BSL-модулей 1С

Уровни:
  - быстрая: oscript -check (синтаксис, ~0.5 сек) — по умолчанию
  - глубокая: BSL Language Server CLI (когнитивная сложность, DeprecatedFind, BSLLS) — флаг --deep
  - --all: оба уровня

Использование:
  python tools/check_bsl.py path/to/Module.bsl
  python tools/check_bsl.py path/to/Module.bsl --deep
  python tools/check_bsl.py path/to/Module.bsl --all
  python tools/check_bsl.py path/to/Module.bsl --all --quiet
  python tools/check_bsl.py path/to/Module.bsl --deep --source-dir src/cf
  python tools/check_bsl.py _temp/independent.bsl --deep --standalone

Область BSL LS (печатается в выводе):
  по умолчанию — файлы .bsl/.os каталога цели; вложенные каталоги (например _archive)
  не анализируются: при их наличии файлы каталога копируются во временный каталог;
  --source-dir — явный рекурсивный контекст, каталог обязан содержать цель;
  --standalone — только сам файл.
Одновременно в проекте выполняется один анализ BSL LS; следующий ждёт его завершения.
oscript -check работает без метаданных и контекста формы и останавливается на первой
ошибке — обычно «Неизвестный символ» внешнего модуля или объекта метаданных.

Exit codes:
  0 — нет ошибок и предупреждений
  1 — есть warnings (но не errors)
  2 — есть errors (находки BSL LS уровня Error или ошибка oscript -check)
  3 — проверка не выполнена полностью (нет инструмента, таймаут, занят замок, сбой JVM
      или правила, нет отчёта или цели в отчёте); это не успех и не список находок

Требования:
  - OneScript: C:\\Program Files\\OneScript\\bin\\oscript.exe (для --quick / по умолчанию)
  - Java 21+ (portable в tools/jdk21/ или системная) + tools/bsl_ls/bsl-language-server-*.jar (для --deep / --all)
  - env BSL_LS_JAR — переопределение пути к jar (опционально)
"""
import argparse
import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import url2pathname

PROJECT_ROOT = Path(__file__).resolve().parent.parent
BSL_LS_DIR = PROJECT_ROOT / "tools" / "bsl_ls"
BSL_LS_CONFIG = BSL_LS_DIR / ".bsl-language-server.json"
OSCRIPT_DEFAULT = r"C:\Program Files\OneScript\bin\oscript.exe"
# Код возврата «проверка не выполнена»: отличается от 2 (найдены ошибки в коде).
INCOMPLETE = 3
SOURCE_SUFFIXES = (".bsl", ".os")
LOCK_TIMEOUT_SEC = 300
# Короткий CLI-анализ, замер 27.09.2026:
# TieredStopAtLevel=1 - только JIT C1 (C2 не успевает окупиться за 10-30 с): CPU x0,33-0,39;
# ActiveProcessorCount - видимое JVM число процессоров (пулы ForkJoin, GC), не квота: CPU и RSS ниже.
# Вместе: wall x0,78-0,85, CPU x0,32-0,37, пик RSS x0,73-0,88; находки совпали по всем полям.
JVM_OPTIONS = (f"-XX:ActiveProcessorCount={min(4, os.cpu_count() or 1)}", "-XX:TieredStopAtLevel=1")
# Сообщения движка о неполном анализе при коде возврата JVM 0
# (строки из байткода BSL LS 0.29.0: DiagnosticComputer, ServerContext).
ENGINE_FAILURE_MARKERS = (
    "Diagnostic computation error.",
    "Can't parse configuration metadata",
    "Can't populate server context",
    "Exception in thread",
    "OutOfMemoryError",
)
# Portable JDK 21 (Eclipse Temurin) — предпочтительный, не требует admin/PATH
PORTABLE_JDK21 = PROJECT_ROOT / "tools" / "jdk21" / "bin" / "java.exe"


# ===== ANSI цвета (Windows 10+ поддерживает) =====
class C:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    RED = "\033[31m"
    YEL = "\033[33m"
    GRN = "\033[32m"
    CYN = "\033[36m"
    GRY = "\033[90m"


def enable_ansi_windows():
    """В Windows 10+ нужно включить VT100 в консоли."""
    if os.name == "nt":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
        except Exception:
            pass


# ===== Поиск инструментов =====
def find_oscript():
    """Найти oscript.exe."""
    if Path(OSCRIPT_DEFAULT).exists():
        return OSCRIPT_DEFAULT
    which = shutil.which("oscript")
    return which


def _jar_version_key(path_str):
    """Извлечь tuple версии из имени bsl-language-server-X.Y.Z-exec.jar для сортировки."""
    import re
    name = Path(path_str).name
    m = re.search(r"bsl-language-server-(\d+)\.(\d+)\.(\d+)", name)
    if not m:
        return (0, 0, 0)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def find_bsl_ls_jar():
    """Найти BSL LS jar. Приоритет:
    1. env BSL_LS_JAR (явное переопределение)
    2. Самая свежая версия (по semver) среди bsl-language-server-*-exec.jar в tools/bsl_ls/
    """
    env_jar = os.environ.get("BSL_LS_JAR")
    if env_jar:
        # Заданный, но отсутствующий JAR - ошибка, а не молчаливая подмена штатным.
        return env_jar if Path(env_jar).is_file() else None
    pattern = str(BSL_LS_DIR / "bsl-language-server-*-exec.jar")
    found = glob.glob(pattern)
    if not found:
        return None
    # Сортировка по semver (X.Y.Z), а не лексикографически — иначе 0.9.0 > 0.29.0
    found.sort(key=_jar_version_key)
    return found[-1]


def find_java():
    """Найти java.exe. Приоритет:
    1. Portable tools/jdk21/bin/java.exe (предпочтительный — JDK 21 LTS под BSL LS 0.29+)
    2. Portable tools/bsl_ls/jdk*/bin/java.exe (legacy location)
    3. Системная java (PATH)
    4. Стандартные пути установки Windows
    """
    # 1. Portable JDK 21 в tools/jdk21/
    if PORTABLE_JDK21.exists():
        return str(PORTABLE_JDK21)
    # 2. Legacy portable в tools/bsl_ls/jdk*/
    portable_glob = list(BSL_LS_DIR.glob("jdk*/bin/java.exe"))
    if portable_glob:
        return str(portable_glob[0])
    # 3. PATH
    which = shutil.which("java")
    if which:
        return which
    # 4. Стандартные пути Windows
    candidates = [
        r"C:\Program Files\Java\jdk-21\bin\java.exe",
        r"C:\Program Files\Eclipse Adoptium\jdk-21.0.0.0-hotspot\bin\java.exe",
        r"C:\Program Files\Java\jdk-17\bin\java.exe",
        r"C:\Program Files\Eclipse Adoptium\jdk-17.0.0.0-hotspot\bin\java.exe",
    ]
    for c in candidates:
        if Path(c).exists():
            return c
    return None


def java_version_major(java_path):
    """Вернёт major version Java (8, 11, 17, 21...)."""
    try:
        out = subprocess.run([java_path, "-version"], capture_output=True, text=True, timeout=10)
        text = (out.stderr or "") + (out.stdout or "")
        # "java version \"17.0.13\""   или   "openjdk version \"1.8.0_431\""
        import re
        m = re.search(r'version "(\d+)(?:\.(\d+))?', text)
        if not m:
            return None
        major = int(m.group(1))
        if major == 1 and m.group(2):
            return int(m.group(2))
        return major
    except Exception:
        return None


# ===== Быстрая проверка через oscript =====
def run_oscript_check(file_path, quiet=False):
    """oscript -check <file> — синтаксис BSL. Возвращает (exit_code, errors_list)."""
    oscript = find_oscript()
    if not oscript:
        print(f"{C.RED}oscript.exe не найден ({OSCRIPT_DEFAULT}){C.RESET}")
        return INCOMPLETE, ["oscript_not_found"]

    if not quiet:
        print(f"{C.CYN}[1/2] oscript -check{C.RESET}  {file_path}")

    try:
        # Без -encoding=utf-8 oscript пишет в канал в OEM-кодировке консоли (cp866),
        # и чтение как UTF-8 превращало русский текст ошибки в нечитаемые символы.
        result = subprocess.run(
            [oscript, "-encoding=utf-8", "-check", str(file_path)],
            capture_output=True,
            timeout=30,
        )
        out = (result.stdout or b"").decode("utf-8", errors="replace")
        err = (result.stderr or b"").decode("utf-8", errors="replace")
        combined = (out + err).strip()
    except subprocess.TimeoutExpired:
        print(f"{C.RED}oscript timeout (>30s){C.RESET}")
        return INCOMPLETE, ["oscript_timeout"]
    except Exception as e:
        print(f"{C.RED}oscript error: {e}{C.RESET}")
        return INCOMPLETE, [str(e)]

    if result.returncode == 0 and ("No errors." in combined or not combined):
        if not quiet:
            print(f"      {C.GRN}OK: No errors.{C.RESET}")
        return 0, []
    if result.returncode != 0 and "Ошибка в строке" in combined:
        print(f"      {C.RED}ОШИБКИ синтаксиса:{C.RESET}")
        for line in combined.splitlines():
            print(f"      {C.RED}|{C.RESET} {line}")
        return 2, combined.splitlines()
    # Нет ни «No errors.», ни места ошибки: результат oscript не распознан.
    print(f"{C.RED}oscript: неожиданный результат (код {result.returncode}):{C.RESET}")
    print(combined[-1000:])
    return INCOMPLETE, ["oscript_unexpected"]


# ===== Глубокая проверка через BSL LS =====
# BSL LS 0.29 json-reporter отдаёт severity в LSP-стиле: Error/Warning/Information/Hint.
# Старые версии (0.24) использовали Critical/Major/Minor/Info - оставлены для совместимости.
SEVERITY_MAP = {
    "Error": ("ERROR  ", C.RED),
    "Critical": ("CRIT   ", C.RED),
    "Warning": ("WARN   ", C.YEL),
    "Major": ("MAJOR  ", C.YEL),
    "Minor": ("MINOR  ", C.YEL),
    "Information": ("INFO   ", C.GRY),
    "Info": ("INFO   ", C.GRY),
    "Hint": ("HINT   ", C.GRY),
}

# Порядок вывода находок (от тяжёлых к лёгким)
SEVERITY_ORDER = ["Critical", "Error", "Warning", "Major", "Minor", "Information", "Info", "Hint"]


def _try_lock(stream):
    """Неблокирующий захват первого байта; OSError, если замок занят."""
    stream.seek(0)
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(stream):
    stream.seek(0)
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


@contextmanager
def analysis_lock(timeout=LOCK_TIMEOUT_SEC, poll=0.5):
    """Один анализ BSL LS на проект: следующий ждёт до timeout секунд, затем TimeoutError.

    Замок держит ОС на открытом файле, поэтому аварийное завершение владельца его снимает.
    Не управляет JVM редактора и сторонними прямыми запусками BSL LS.
    """
    lock_path = PROJECT_ROOT / "_temp" / "bsl-analysis.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as stream:
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        deadline = time.monotonic() + timeout
        waiting = False
        while True:
            try:
                _try_lock(stream)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"замок {lock_path} занят дольше {timeout} с") from None
                if not waiting:
                    print(f"      {C.YEL}Ожидание: в проекте уже идёт анализ BSL LS{C.RESET}")
                    waiting = True
                time.sleep(poll)
        try:
            yield
        finally:
            _unlock(stream)


def analysis_scope(src_path, source_dir=None, standalone=False):
    """Область BSL LS: (каталог, файлы для копирования во временный каталог или None).

    None - анализ на месте. По умолчанию область - файлы .bsl/.os каталога цели; BSL LS
    обходит --srcDir рекурсивно, поэтому при вложенных каталогах файлы копируются.
    """
    if standalone:
        return src_path.parent, [src_path]
    if source_dir:
        src_dir = Path(source_dir).resolve()
        if not src_dir.is_dir() or not src_path.is_relative_to(src_dir):
            raise ValueError("Целевой файл должен находиться внутри --source-dir")
        return src_dir, None
    src_dir = src_path.parent
    nested = any(p.suffix.lower() in SOURCE_SUFFIXES and p.parent != src_dir and p.is_file()
                 for p in src_dir.rglob("*"))
    if not nested:
        return src_dir, None
    return src_dir, sorted(p for p in src_dir.iterdir()
                           if p.is_file() and p.suffix.lower() in SOURCE_SUFFIXES)


def describe_scope(src_dir, staged, standalone):
    if standalone:
        return "только файл (--standalone)"
    if staged is None:
        count = sum(1 for p in src_dir.rglob("*") if p.suffix.lower() in SOURCE_SUFFIXES)
        return f"{src_dir} рекурсивно, файлов .bsl/.os: {count}"
    return (f"{src_dir} без вложенных каталогов, файлов .bsl/.os: {len(staged)}"
            " (рекурсивный контекст: --source-dir)")


def target_findings(data, src_path, workspace):
    """Сопоставляет полный путь отчёта с целью; отсутствие цели не означает успех."""
    file_infos = data.get("fileinfos", data.get("fileInfos"))
    if not isinstance(file_infos, list):
        raise ValueError("В отчёте отсутствует список fileinfos")
    matches = []
    for info in file_infos:
        raw_path = info.get("path", "")
        if raw_path.startswith("file:"):
            uri = urlsplit(raw_path)
            raw_path = url2pathname(("//" + uri.netloc if uri.netloc else "") + uri.path)
        path = Path(raw_path)
        if not path.is_absolute():
            path = workspace / path
        if path.resolve() == src_path:
            matches.append(info)
    if len(matches) != 1:
        raise ValueError(f"Целевой файл должен встречаться в отчёте ровно один раз: {src_path}")
    diagnostics = matches[0].get("diagnostics")
    if not isinstance(diagnostics, list):
        raise ValueError("В отчёте цели отсутствует список diagnostics")
    for diagnostic in diagnostics:
        if not isinstance(diagnostic, dict) or diagnostic.get("severity") not in SEVERITY_MAP:
            raise ValueError("В отчёте цели отсутствует или неизвестна severity диагностики")
    return diagnostics


def bsl_ls_command(java, jar, src_dir, tmpdir, report_dir, config=None, jvm_options=()):
    """Команда BSL LS --analyze."""
    cmd = [
        java,
        # JLine native lib BSL LS падает на кириллических путях в дефолтном Java tmpdir
        # (например C:\Users\Пользователь\AppData\Local\Temp) — форсируем ASCII-путь рядом с jar.
        f"-Djava.io.tmpdir={tmpdir}",
        "-Xmx512m",
        *jvm_options,
        "-jar", str(jar),
        "--analyze",
        "--srcDir", str(src_dir),
        "--reporter", "json",
        "--outputDir", str(report_dir),
    ]
    if config is not None:
        cmd += ["--configuration", str(config)]
    return cmd


def engine_failure(output):
    """Первый маркер неполного анализа в выводе JVM или None."""
    return next((marker for marker in ENGINE_FAILURE_MARKERS if marker in output), None)


def run_bsl_ls_check(file_path, quiet=False, source_dir=None, standalone=False):
    """BSL Language Server --analyze. Возвращает (exit_code, findings_count_dict)."""
    jar = find_bsl_ls_jar()
    if not jar:
        print(f"{C.RED}bsl-language-server-*-exec.jar не найден в {BSL_LS_DIR} "
              f"или по BSL_LS_JAR={os.environ.get('BSL_LS_JAR', '')}{C.RESET}")
        print(f"{C.YEL}Скачать: https://github.com/1c-syntax/bsl-language-server/releases/latest{C.RESET}")
        return INCOMPLETE, {"missing_jar": 1}

    java = find_java()
    if not java:
        print(f"{C.RED}java не найдена (нужна Java 21+ для BSL LS 0.29+){C.RESET}")
        print(f"{C.YEL}Portable JDK 21 ожидается в tools/jdk21/ — см. tools/bsl_ls/README.md{C.RESET}")
        return INCOMPLETE, {"missing_java": 1}

    jv = java_version_major(java)
    # BSL LS 0.29.0 скомпилирован для Java 21 (class file 65.0). Java <21 даст UnsupportedClassVersionError.
    # Старые версии BSL LS (0.24 и ниже) работают на Java 17 — допускаем 17+, но рекомендуем 21+.
    if jv is not None and jv < 17:
        print(f"{C.RED}Java {jv} слишком старая. BSL LS 0.29+ требует Java 21+ (0.24 — Java 17+).{C.RESET}")
        print(f"{C.YEL}Установить portable JDK 21 в tools/jdk21/ — см. tools/bsl_ls/README.md{C.RESET}")
        return INCOMPLETE, {"old_java": 1}

    src_path = Path(file_path).resolve()
    try:
        src_dir, staged = analysis_scope(src_path, source_dir, standalone)
    except ValueError as error:
        print(f"{C.RED}{error}{C.RESET}")
        return INCOMPLETE, {"invalid_source": 1}
    if not quiet:
        print(f"{C.CYN}[2/2] BSL Language Server{C.RESET} (Java {jv}, jar {jar})")
        print(f"      Область: {describe_scope(src_dir, staged, standalone)}")

    bsl_tmp_root = BSL_LS_DIR / "_tmp"
    bsl_tmp_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=str(bsl_tmp_root)) as tmpdir:
        if staged is not None:
            src_dir = Path(tmpdir) / "source"
            src_dir.mkdir()
            for path in staged:
                shutil.copy2(path, src_dir / path.name)
            src_path = src_dir / src_path.name
        report_dir = Path(tmpdir) / "report"
        report_dir.mkdir()
        config = BSL_LS_CONFIG if BSL_LS_CONFIG.exists() else None
        if source_dir:
            # Явная область - также корень метаданных. Относительный '.' рабочего
            # конфига иначе разрешается от PROJECT_ROOT и теряет контекст цели.
            try:
                settings = json.loads(config.read_text(encoding="utf-8-sig")) if config else {}
                settings["configurationRoot"] = str(src_dir)
                config = Path(tmpdir) / "configuration.json"
                config.write_text(json.dumps(settings, ensure_ascii=False), encoding="utf-8")
            except (OSError, ValueError, TypeError) as error:
                print(f"{C.RED}Конфигурация BSL LS не принята: {error}{C.RESET}")
                return INCOMPLETE, {"invalid_configuration": 1}
        cmd = bsl_ls_command(java, jar, src_dir, tmpdir, report_dir,
                             config, JVM_OPTIONS)
        try:
            with analysis_lock():
                # cwd фиксирован: относительные configurationRoot и пути отчёта BSL LS
                # разрешаются от каталога запуска JVM, а не от места вызова check_bsl.
                result = subprocess.run(cmd, capture_output=True, text=True, cwd=PROJECT_ROOT,
                                        encoding="utf-8", errors="replace", timeout=120)
        except subprocess.TimeoutExpired:
            print(f"{C.RED}BSL LS timeout (>120s){C.RESET}")
            return INCOMPLETE, {"bsl_ls_timeout": 1}
        except Exception as e:
            print(f"{C.RED}BSL LS error: {e}{C.RESET}")
            return INCOMPLETE, {"bsl_ls_error": 1}

        output = (result.stdout or "") + (result.stderr or "")
        if result.returncode != 0:
            print(f"{C.RED}BSL LS завершился с кодом {result.returncode}{C.RESET}")
            print(output[-1000:])
            return INCOMPLETE, {"bsl_ls_failed": 1}
        marker = engine_failure(output)
        if marker:
            print(f"{C.RED}BSL LS: «{marker}». Проверка неполная.{C.RESET}")
            print(output[-1000:])
            return INCOMPLETE, {"engine_failure": 1}

        report_files = list(report_dir.glob("*.json"))
        if len(report_files) != 1:
            print(f"{C.RED}BSL LS: ожидался один JSON-отчёт, найдено {len(report_files)}.{C.RESET}")
            print(output[-1000:])
            return INCOMPLETE, {"no_report": 1}

        try:
            data = json.loads(report_files[0].read_text(encoding="utf-8"))
            findings = target_findings(data, src_path, PROJECT_ROOT)
        except Exception as e:
            print(f"{C.RED}Отчёт BSL LS не принят: {e}{C.RESET}")
            return INCOMPLETE, {"invalid_report": 1}

    # Группировка по severity
    by_sev = {}
    for d in findings:
        sev = d.get("severity", "Info")
        by_sev.setdefault(sev, []).append(d)

    # Вывод
    if not findings:
        if not quiet:
            print(f"      {C.GRN}OK: 0 findings.{C.RESET}")
        return 0, {}

    print(f"      {C.BOLD}Findings: {len(findings)}{C.RESET}")
    # Печатаем известные severity в порядке тяжести, затем любые неизвестные (на случай новых версий)
    for sev in SEVERITY_ORDER + [s for s in by_sev if s not in SEVERITY_ORDER]:
        items = by_sev.get(sev, [])
        if not items:
            continue
        label, color = SEVERITY_MAP.get(sev, (sev, C.GRY))
        for d in items:
            rng = d.get("range") or {}
            line0 = (rng.get("start") or {}).get("line")
            # LSP line 0-based -> человекочитаемый 1-based (как в редакторе)
            line = (line0 + 1) if isinstance(line0, int) else "?"
            code = d.get("code", "?")
            msg = d.get("message", "").replace("\n", " ").strip()
            print(f"      {color}{label}{C.RESET} L{line:<4} {C.BOLD}{code}{C.RESET}: {msg}")

    # Exit code
    has_errors = bool(by_sev.get("Error") or by_sev.get("Critical"))
    has_warnings = bool(by_sev.get("Warning") or by_sev.get("Major") or by_sev.get("Minor"))
    if has_errors:
        return 2, {sev: len(items) for sev, items in by_sev.items()}
    if has_warnings:
        return 1, {sev: len(items) for sev, items in by_sev.items()}
    return 0, {sev: len(items) for sev, items in by_sev.items()}


# ===== Main =====
def main():
    enable_ansi_windows()
    parser = argparse.ArgumentParser(
        description="Двухуровневая проверка BSL модулей (oscript + BSL Language Server)",
    )
    parser.add_argument("file", help="путь к .bsl файлу")
    parser.add_argument("--deep", action="store_true", help="только глубокая проверка (BSL LS)")
    parser.add_argument("--all", action="store_true", help="обе проверки")
    parser.add_argument("--quiet", action="store_true", help="меньше вывода")
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--source-dir", help="явный каталог контекста BSL LS (рекурсивно)")
    scope.add_argument("--standalone", action="store_true",
                       help="только независимый файл, без соседних модулей и метаданных")
    args = parser.parse_args()

    file_path = Path(args.file)
    if not file_path.is_file():
        print(f"{C.RED}файл не найден: {file_path}{C.RESET}")
        return INCOMPLETE
    if file_path.suffix.lower() != ".bsl":
        print(f"{C.YEL}предупреждение: расширение не .bsl ({file_path.suffix}){C.RESET}")

    print(f"{C.BOLD}=== {file_path} ==={C.RESET}")

    do_quick = not args.deep or args.all
    do_deep = args.deep or args.all

    quick_rc = 0
    deep_rc = 0

    if do_quick:
        quick_rc, _ = run_oscript_check(file_path, quiet=args.quiet)

    if do_deep:
        deep_rc, _ = run_bsl_ls_check(file_path, quiet=args.quiet,
                                    source_dir=args.source_dir, standalone=args.standalone)

    final = max(quick_rc, deep_rc)
    if final == 0:
        print(f"{C.GRN}{C.BOLD}=== РЕЗУЛЬТАТ: OK ==={C.RESET}")
    elif final == 1:
        print(f"{C.YEL}{C.BOLD}=== РЕЗУЛЬТАТ: WARNINGS ==={C.RESET}")
    elif final == 2:
        print(f"{C.RED}{C.BOLD}=== РЕЗУЛЬТАТ: ERRORS ==={C.RESET}")
    else:
        print(f"{C.RED}{C.BOLD}=== РЕЗУЛЬТАТ: ПРОВЕРКА НЕ ВЫПОЛНЕНА ==={C.RESET}")
    return final


if __name__ == "__main__":
    sys.exit(main())
