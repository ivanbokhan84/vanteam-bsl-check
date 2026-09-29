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
      или правила, строка WARN/ERROR в логе движка, нет отчёта или цели в отчёте,
      не загружены metadata при --source-dir с выгрузкой); это не успех и не список находок

Требования:
  - OneScript: C:\\Program Files\\OneScript\\bin\\oscript.exe (для --quick / по умолчанию)
  - Java 21+ (portable в tools/jdk21/ или системная) + tools/bsl_ls/bsl-language-server-*.jar (для --deep / --all);
    релиз проверен на JAR форка 1.0.7-vanteam.1 (https://github.com/ivanbokhan84/bsl-language-server)
    и штатном BSL LS 1.0.7
  - env BSL_LS_JAR — переопределение пути к jar (опционально)
  - env BSL_LS_CACHE — каталог кэша справки платформы для JAR форка (по умолчанию tools/bsl_ls/_cache)
  - env BSL_LS_XMX — размер кучи JVM (по умолчанию 512m, для области от 200 файлов - 1g)
BSL LS 1.0.7 по умолчанию берёт контекст платформы из синтакс-помощника установленной 1С
(самая свежая версия на машине), без неё - встроенные описания; источник печатается в выводе.
JAR форка хранит разобранную справку в постоянном кэше (попадание вдвое сокращает запуск) и
считает диагностики только для цели (--target), находки те же, что у штатного 1.0.7.
Всегда: --silent; -XX:+ExitOnOutOfMemoryError (нехватка памяти - код JVM 3, у обёртки - 3).
AppCDS подключается, если его подготовил scripts/prepare_cds.py для этих JAR и JDK.
"""
import argparse
import glob
import json
import os
import re
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
BSL_LS_TIMEOUT_SEC = 120
# Короткий CLI-анализ BSL LS 1.0.7, замер 28.09.2026 (6 сценариев, прогрев + 5 раундов):
# TieredStopAtLevel=1 - только JIT C1; ActiveProcessorCount - видимое JVM число процессоров
# (пулы ForkJoin/анализа, GC), не квота. Вместе против штатной JVM: wall x0,84-0,92, CPU x0,41-0,44,
# пик RSS x0,85-0,87; диагностики совпали по всем полям. Один CPU4 не снижает CPU на 15% - отклонён.
# ExitOnOutOfMemoryError: без него форк при нехватке кучи пишет ERROR и выходит с 0, а штатный
# 1.0.7 может разбирать справку повторно; с ним JVM завершается с кодом 3 за секунды.
JVM_OPTIONS = (f"-XX:ActiveProcessorCount={min(4, os.cpu_count() or 1)}", "-XX:TieredStopAtLevel=1",
               "-XX:+ExitOnOutOfMemoryError")
# Куча: 512m хватает модулю и мини-конфигурации; штатный 1.0.7 на 596 модулях при 512m падает
# с OutOfMemoryError, при 1g проходит (замер BSL Server 29.09.2026).
LARGE_SCOPE_FILES = 200
LARGE_SCOPE_TIMEOUT_SEC = 600
# JAR форка ivanbokhan84/bsl-language-server: постоянный кэш справки платформы и --target.
FORK_JAR = re.compile(r"-vanteam\.\d+-exec\.jar$", re.IGNORECASE)
PLATFORM_CACHE = re.compile(r"Platform context cache (hit|miss|written|unreadable|write failed|key failed)")
# Сообщения движка о неполном анализе при коде возврата JVM 0
# (строки исходников BSL LS 1.0.7: DefaultDiagnosticComputer, ServerContext).
ENGINE_FAILURE_MARKERS = (
    "Diagnostic computation error.",
    "Can't parse configuration metadata",
    "Can't populate server context",
    "Exception in thread",
    "OutOfMemoryError",
)
# Строка лога движка (logback Spring Boot) уровня WARN/ERROR. BSL LS 1.0.7 при коде 0 так сообщает
# о пропущенной части анализа: битый Configuration.xml (WARN mdclasses «Can't read file», metadata
# пусты), неверный параметр правила, ошибка чтения, сбой загрузки встроенных типов или платформы.
ENGINE_LOG_PROBLEM = re.compile(r"^\d{4}-\d\d-\d\dT\S+\s+(?:WARN|ERROR)\s+\d+\s+---\s.*$", re.MULTILINE)
PLATFORM_CONTEXT = re.compile(r"Loaded (\d+) platform contexts from 1C syntax helper")
MINIMUM_JAVA = 21
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
    """Tuple версии из имени bsl-language-server-X.Y.Z[-vanteam.N]-exec.jar для сортировки.

    JAR форка той же базовой версии идёт после штатного: его находки те же, запуск дешевле.
    """
    name = Path(path_str).name
    m = re.search(r"bsl-language-server-(\d+)\.(\d+)\.(\d+)(?:-vanteam\.(\d+))?-exec\.jar$", name)
    if not m:
        return (0, 0, 0, 0, 0)
    fork = m.group(4)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)), 1 if fork else 0, int(fork or 0))


def is_fork_jar(jar):
    """JAR форка ivanbokhan84/bsl-language-server: понимает --target и кэш справки платформы."""
    return bool(FORK_JAR.search(Path(jar).name))


def platform_cache_dir():
    """Каталог кэша справки форка: BSL_LS_CACHE или ASCII-путь рядом с JAR, как java.io.tmpdir."""
    return Path(os.environ.get("BSL_LS_CACHE") or BSL_LS_DIR / "_cache")


def heap_option(file_count):
    """-Xmx по env BSL_LS_XMX или по числу файлов области."""
    value = os.environ.get("BSL_LS_XMX")
    if value:
        return f"-Xmx{value}"
    return "-Xmx1g" if file_count >= LARGE_SCOPE_FILES else "-Xmx512m"


def cds_stamp(jar, java):
    """Отметка JAR и сборки JDK для AppCDS: архив годен только для них, иначе JVM молча без него."""
    try:
        java_exe = Path(shutil.which(java) or java).resolve()
        modules = java_exe.parent.parent / "lib" / "modules"
        jar_stat, modules_stat = Path(jar).stat(), modules.stat()
    except (OSError, TypeError):
        return None
    return {"jar": Path(jar).name, "jar_size": jar_stat.st_size, "jar_mtime_ns": jar_stat.st_mtime_ns,
            "java": str(java_exe), "jdk_modules_size": modules_stat.st_size,
            "jdk_modules_mtime_ns": modules_stat.st_mtime_ns}


def cds_layout(jar, java):
    """(архив .jsa, распакованный JAR), если scripts/prepare_cds.py подготовил их для этих JAR и JDK."""
    base = BSL_LS_DIR / "cds" / Path(jar).stem
    archive, extracted, stamp_file = base / "bslls.jsa", base / Path(jar).name, base / "stamp.json"
    if not (archive.is_file() and extracted.is_file() and stamp_file.is_file()):
        return None
    try:
        stamp = json.loads(stamp_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    current = cds_stamp(jar, java)
    return (archive, extracted) if current is not None and stamp == current else None


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
    1. Portable tools/jdk21/bin/java.exe (предпочтительный — JDK 21 LTS под BSL LS 1.0.7)
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
# BSL LS 1.0.7 json-reporter отдаёт severity lsp4j: Error/Warning/Information/Hint.
# Имена формата 0.24 (Critical/Major/Minor/Info) и любые другие - неизвестный формат, код 3.
SEVERITY_MAP = {
    "Error": ("ERROR  ", C.RED),
    "Warning": ("WARN   ", C.YEL),
    "Information": ("INFO   ", C.GRY),
    "Hint": ("HINT   ", C.GRY),
}

# Порядок вывода находок (от тяжёлых к лёгким)
SEVERITY_ORDER = ["Error", "Warning", "Information", "Hint"]


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


def metadata_expected(src_dir):
    """В каталоге есть корень выгрузки Designer или EDT: metadata цели обязаны загрузиться."""
    return ((src_dir / "Configuration.xml").is_file()
            or (src_dir / "Configuration" / "Configuration.mdo").is_file())


def target_findings(data, src_path, workspace):
    """(diagnostics, mdoRef) цели по полному пути отчёта; отсутствие цели не означает успех."""
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
    return diagnostics, matches[0].get("mdoRef")


def bsl_ls_command(java, jar, src_dir, tmpdir, report_dir, config=None, jvm_options=(),
                   heap="-Xmx512m", target=None, cache_dir=None, cds=None):
    """Команда BSL LS --analyze.

    target - --target форка: диагностики только цели, контекст - вся область;
    cache_dir - кэш справки платформы форка; cds - (архив, распакованный JAR) AppCDS.
    """
    cmd = [
        java,
        # JLine native lib BSL LS падает на кириллических путях в дефолтном Java tmpdir
        # (например C:\Users\Пользователь\AppData\Local\Temp) — форсируем ASCII-путь рядом с jar.
        f"-Djava.io.tmpdir={tmpdir}",
        heap,
        *jvm_options,
    ]
    if cds is not None:
        archive, jar = cds
        cmd.append(f"-XX:SharedArchiveFile={archive}")
    if cache_dir is not None:
        cmd.append(f"-Dapp.platform-context.cache.path={cache_dir}")
    cmd += [
        "-jar", str(jar),
        "--analyze",
        "--srcDir", str(src_dir),
    ]
    if target is not None:
        cmd += ["--target", str(target)]
    cmd += [
        "--reporter", "json",
        "--outputDir", str(report_dir),
        # Без --silent JLine при Git usr/bin в PATH запускает десятки дочерних процессов.
        "--silent",
    ]
    if config is not None:
        cmd += ["--configuration", str(config)]
    return cmd


def engine_failure(output):
    """Первый маркер неполного анализа или первая строка лога WARN/ERROR в выводе JVM; иначе None."""
    marker = next((marker for marker in ENGINE_FAILURE_MARKERS if marker in output), None)
    if marker:
        return marker
    line = ENGINE_LOG_PROBLEM.search(output)
    return line.group(0).strip() if line else None


def describe_platform(output):
    """Источник контекста платформы 1С по выводу BSL LS 1.0.7."""
    loaded = PLATFORM_CONTEXT.search(output)
    cache = PLATFORM_CACHE.search(output)
    cache_note = f", кэш справки: {cache.group(1)}" if cache else ""
    if loaded:
        return f"синтакс-помощник установленной 1С, контекстов: {loaded.group(1)}{cache_note}"
    return "встроенные описания BSL LS (1С не найдена или контекст отключён)" + cache_note


def scope_file_count(src_dir, staged, standalone):
    """Число файлов .bsl/.os области: от него зависят куча и таймаут."""
    if standalone or staged is not None:
        return len(staged or [])
    return sum(1 for p in src_dir.rglob("*") if p.suffix.lower() in SOURCE_SUFFIXES)


def run_bsl_ls_check(file_path, quiet=False, source_dir=None, standalone=False):
    """BSL Language Server --analyze. Возвращает (exit_code, findings_count_dict)."""
    jar = find_bsl_ls_jar()
    if not jar:
        print(f"{C.RED}bsl-language-server-*-exec.jar не найден в {BSL_LS_DIR} "
              f"или по BSL_LS_JAR={os.environ.get('BSL_LS_JAR', '')}{C.RESET}")
        print(f"{C.YEL}Скачать с проверкой SHA-256: python scripts/fetch_dependencies.py{C.RESET}")
        return INCOMPLETE, {"missing_jar": 1}

    java = find_java()
    if not java:
        print(f"{C.RED}java не найдена (нужна Java {MINIMUM_JAVA}+ для BSL LS 1.0.7){C.RESET}")
        print(f"{C.YEL}Portable JDK 21 ожидается в tools/jdk21/ — см. tools/bsl_ls/README.md{C.RESET}")
        return INCOMPLETE, {"missing_java": 1}

    jv = java_version_major(java)
    # Все классы BSL LS 1.0.7 и зависимостей - не новее class file 65.0 (Java 21); ниже 21 запуск невозможен.
    if jv is not None and jv < MINIMUM_JAVA:
        print(f"{C.RED}Java {jv} слишком старая. BSL LS 1.0.7 требует Java {MINIMUM_JAVA}+.{C.RESET}")
        print(f"{C.YEL}Установить portable JDK 21 в tools/jdk21/ — см. tools/bsl_ls/README.md{C.RESET}")
        return INCOMPLETE, {"old_java": 1}

    src_path = Path(file_path).resolve()
    try:
        src_dir, staged = analysis_scope(src_path, source_dir, standalone)
    except ValueError as error:
        print(f"{C.RED}{error}{C.RESET}")
        return INCOMPLETE, {"invalid_source": 1}
    fork = is_fork_jar(jar)
    file_count = scope_file_count(src_dir, staged, standalone)
    heap = heap_option(file_count)
    timeout = LARGE_SCOPE_TIMEOUT_SEC if file_count >= LARGE_SCOPE_FILES else BSL_LS_TIMEOUT_SEC
    cache_dir = platform_cache_dir() if fork else None
    cds = cds_layout(jar, java)
    if not quiet:
        print(f"{C.CYN}[2/2] BSL Language Server{C.RESET} (Java {jv}, jar {jar})")
        print(f"      Область: {describe_scope(src_dir, staged, standalone)}")
        print(f"      JVM: {heap}, AppCDS: {'да' if cds else 'нет'}"
              + (f"; форк: --target, кэш справки {cache_dir}" if fork else ""))

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
        if cache_dir is not None:
            cache_dir.mkdir(parents=True, exist_ok=True)
        cmd = bsl_ls_command(java, jar, src_dir, tmpdir, report_dir, config, JVM_OPTIONS,
                             heap=heap, target=src_path if fork else None, cache_dir=cache_dir, cds=cds)
        try:
            with analysis_lock():
                # cwd фиксирован: относительные configurationRoot и пути отчёта BSL LS
                # разрешаются от каталога запуска JVM, а не от места вызова check_bsl.
                result = subprocess.run(cmd, capture_output=True, text=True, cwd=PROJECT_ROOT,
                                        encoding="utf-8", errors="replace", timeout=timeout)
        except subprocess.TimeoutExpired:
            print(f"{C.RED}BSL LS timeout (>{timeout}s){C.RESET}")
            return INCOMPLETE, {"bsl_ls_timeout": 1}
        except Exception as e:
            print(f"{C.RED}BSL LS error: {e}{C.RESET}")
            return INCOMPLETE, {"bsl_ls_error": 1}

        output = (result.stdout or "") + (result.stderr or "")
        if result.returncode != 0:
            print(f"{C.RED}BSL LS завершился с кодом {result.returncode}{C.RESET}")
            if "OutOfMemoryError" in output:
                print(f"{C.YEL}Не хватило кучи {heap}: задайте больше через BSL_LS_XMX, например 2g.{C.RESET}")
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
            findings, mdo_ref = target_findings(data, src_path, PROJECT_ROOT)
        except Exception as e:
            print(f"{C.RED}Отчёт BSL LS не принят: {e}{C.RESET}")
            return INCOMPLETE, {"invalid_report": 1}
        # Без metadata mdoRef цели - URI файла: межмодульные правила молча не работают.
        if source_dir and metadata_expected(src_dir) and (not mdo_ref or mdo_ref.startswith("file:")):
            print(f"{C.RED}BSL LS: metadata выгрузки {src_dir} не загружены для цели "
                  f"(mdoRef {mdo_ref!r}). Проверка неполная.{C.RESET}")
            return INCOMPLETE, {"metadata_not_loaded": 1}
    if not quiet:
        print(f"      Контекст платформы: {describe_platform(output)}")

    # Группировка по severity
    by_sev = {}
    for d in findings:
        by_sev.setdefault(d["severity"], []).append(d)

    # Вывод
    if not findings:
        if not quiet:
            print(f"      {C.GRN}OK: 0 findings.{C.RESET}")
        return 0, {}

    print(f"      {C.BOLD}Findings: {len(findings)}{C.RESET}")
    # target_findings допускает только известные severity; печать в порядке тяжести.
    for sev in SEVERITY_ORDER:
        items = by_sev.get(sev, [])
        if not items:
            continue
        label, color = SEVERITY_MAP[sev]
        for d in items:
            rng = d.get("range") or {}
            line0 = (rng.get("start") or {}).get("line")
            # LSP line 0-based -> человекочитаемый 1-based (как в редакторе)
            line = (line0 + 1) if isinstance(line0, int) else "?"
            code = d.get("code", "?")
            msg = d.get("message", "").replace("\n", " ").strip()
            print(f"      {color}{label}{C.RESET} L{line:<4} {C.BOLD}{code}{C.RESET}: {msg}")

    # Exit code
    has_errors = bool(by_sev.get("Error"))
    has_warnings = bool(by_sev.get("Warning"))
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
