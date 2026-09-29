#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
check_bsl.py - проверка BSL-модулей 1С в BSL Language Server (VANTEAM BSL Server check 1.0.0).

Только BSL Language Server. OneScript здесь не вызывается ни прямо, ни косвенно:
синтаксис OneScript проверяет отдельный tools/check_oscript.py.

Использование:
  python tools/check_bsl.py path/to/Module.bsl [--deep]
  python tools/check_bsl.py A/Module.bsl A/Other.bsl B/Module.bsl   # одна JVM на область
  python tools/check_bsl.py path/to/Module.bsl --source-dir src/cf   # контекст выгрузки
  python tools/check_bsl.py _temp/independent.bsl --standalone       # только сам файл
  python tools/check_bsl.py path/to/Module.bsl --json                # итог одним JSON в stdout

--deep принимается для совместимости с прежним вызовом и ничего не меняет.
--all принимается, печатает строку о check_oscript.py и выполняет только BSL LS.

Область анализа (печатается):
  по умолчанию - файлы .bsl/.os каталога модуля; вложенные каталоги (например _archive) не анализируются:
  при их наличии файлы каталога копируются во временный каталог;
  --source-dir - явный рекурсивный контекст и корень метаданных, каталог обязан содержать каждый модуль;
  --standalone - только сам файл.
Модули одной области проверяются одной JVM: BSL LS получает их через несколько --target.
Одновременно в проекте выполняется один анализ BSL LS; следующий ждёт его завершения.

Коды возврата:
  0 - нет ошибок и предупреждений
  1 - есть предупреждения (Warning), ошибок нет
  2 - есть ошибки (находки BSL LS уровня Error)
  3 - проверка не выполнена полностью: нет движка или Java 21+, неверные аргументы, таймаут, занят замок,
      код JVM не 0 (в том числе нехватка памяти), строка WARN/ERROR в логе движка, нет отчёта
      или модуля в отчёте, неизвестная severity, не загружены metadata при --source-dir с выгрузкой.
      Это не успех и не список находок.
  При нескольких модулях итог - наибольший код.

Движок - установка setup_bsl_server.py: каталог VANTEAM_BSL_HOME, по умолчанию %LOCALAPPDATA%\\vanteam-bsl-server.
  BSL_LS_JAR    - другой fat JAR (без CDS); заданный, но отсутствующий файл - ошибка;
  VANTEAM_BSL_JAVA - java.exe; иначе JDK установки, JAVA_HOME, PATH. Нужна Java 21+;
  VANTEAM_BSL_XMX  - размер кучи JVM вместо автоматического (512m; 1g - область больше 150 модулей
                     или 20 МБ, модули к проверке больше 4 МБ);
  VANTEAM_BSL_TIMEOUT - предел времени одной JVM, секунды.
Конфигурация BSL LS: tools/bsl_ls/.bsl-language-server.json проекта, иначе конфиг по умолчанию поставки.
"""
import argparse
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

VERSION = "1.0.0"
PRODUCT = "VANTEAM BSL Server check"
TOOLS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = TOOLS_DIR.parent
CONFIG_NAME = ".bsl-language-server.json"
# Конфиг проекта - там же, где его держала обёртка 1.1.0; иначе конфиг по умолчанию рядом с проверкой
# (каталог поставки checker\config) или в установке (его кладёт туда setup_bsl_server.py).
PROJECT_CONFIG = PROJECT_ROOT / "tools" / "bsl_ls" / CONFIG_NAME
BUNDLED_CONFIG = TOOLS_DIR / "config" / CONFIG_NAME
INSTALL_FILE = "install.json"
# Код возврата «проверка не выполнена»: отличается от 2 (найдены ошибки в коде).
INCOMPLETE = 3
SOURCE_SUFFIXES = (".bsl", ".os")
LOCK_TIMEOUT_SEC = 300
# Предел одной JVM: база плюс запас на каждую следующую цель и на размер области.
BSL_LS_TIMEOUT_SEC = 180
MINIMUM_JAVA = 21
# Куча JVM: 512m, 1g - для крупной области или крупных целей. Замеры форка (README, acceptance/xmx_probe.py):
# при 512m OutOfMemoryError - область 4089 модулей (153 МБ) с одной целью, 5 целей 10,7 МБ, 596 целей 47,5 МБ;
# одна цель 5,7 МБ - вдвое медленнее из-за GC; 150 целей 10,8 МБ мелкими модулями и области до 596 модулей
# (47,5 МБ) с одной целью проходят без замедления. Пороги взяты с запасом внутри проходящей зоны.
XMX_DEFAULT = "512m"
XMX_LARGE = "1g"
XMX_LARGE_MODULES = 150
XMX_LARGE_BYTES = 20 * 1000 * 1000
XMX_LARGE_TARGET_BYTES = 4 * 1000 * 1000
# Флаги короткого CLI-анализа (замеры фазы 2, REPORT-phase2.md, раздел 7): только JIT C1, видимые JVM
# процессоры (пулы ForkJoin, GC) не больше 4, выход при нехватке памяти вместо повторных попыток разбора.
JVM_OPTIONS = ("-XX:TieredStopAtLevel=1", f"-XX:ActiveProcessorCount={min(4, os.cpu_count() or 1)}",
               "-XX:+ExitOnOutOfMemoryError")
CACHE_PROPERTY = "-Dapp.platform-context.cache.path="
# Длиннее - цели --target уходят в файл аргументов (предел CreateProcess - 32 767 символов).
COMMAND_LINE_LIMIT = 24000
# Сообщения движка о неполном анализе при коде возврата JVM 0 (BSL LS 1.0.7 и форк v1.0.7-vanteam.1:
# DefaultDiagnosticComputer, ServerContext, BslContextHolder).
ENGINE_FAILURE_MARKERS = (
    "Diagnostic computation error.",
    "Can't parse configuration metadata",
    "Can't populate server context",
    "Exception in thread",
    "OutOfMemoryError",
    "platform context is disabled",
)
# Строка лога движка (logback Spring Boot) уровня WARN/ERROR. BSL LS при коде 0 так сообщает о пропущенной
# части анализа: битый Configuration.xml, неверный параметр правила, ошибка чтения, сбой справки платформы.
ENGINE_LOG_PROBLEM = re.compile(r"^\d{4}-\d\d-\d\dT\S+\s+(?:WARN|ERROR)\s+\d+\s+---\s.*$", re.MULTILINE)
PLATFORM_CONTEXT = re.compile(r"Loaded (\d+) platform contexts from 1C syntax helper")
PLATFORM_CACHE = re.compile(r"Platform context cache (hit|miss|written|unreadable|write failed|key failed)")
CDS_PROBLEM = re.compile(r"^\[[^\]]*\]\[(?:warning|error)\]\[cds[^\]]*\].*$", re.MULTILINE)
ANSI = re.compile(r"\x1b\[[0-9;]*m")
XMX_FORMAT = re.compile(r"^\d+[kKmMgG]?$")

# BSL LS json-reporter отдаёт severity lsp4j: Error/Warning/Information/Hint.
# Имена формата 0.24 (Critical/Major/Minor/Info) и любые другие - неизвестный формат, код 3.
SEVERITY_LABELS = {"Error": "ERROR  ", "Warning": "WARN   ", "Information": "INFO   ", "Hint": "HINT   "}
SEVERITY_ORDER = ["Error", "Warning", "Information", "Hint"]
RESULT_NAMES = {0: "ok", 1: "warnings", 2: "errors", INCOMPLETE: "incomplete"}


class Output:
    """Вывод проверки: info - ход работы (скрывается --quiet), error - причины неполной проверки и итог.

    В режиме --json строки не печатаются, а собираются в messages результата.
    """

    def __init__(self, quiet=False, collect=False):
        self.quiet, self.collect, self.messages = quiet, collect, []

    def _write(self, text):
        if self.collect:
            self.messages.append(ANSI.sub("", text))
        else:
            print(text, flush=True)

    def info(self, text):
        if not self.quiet or self.collect:
            self._write(text)

    def error(self, text):
        self._write(text)


class C:
    """ANSI-цвета; пустые, если вывод не в терминал или задан NO_COLOR."""
    RESET = BOLD = RED = YEL = GRN = CYN = GRY = ""

    @classmethod
    def enable(cls):
        cls.RESET, cls.BOLD, cls.RED, cls.YEL = "\033[0m", "\033[1m", "\033[31m", "\033[33m"
        cls.GRN, cls.CYN, cls.GRY = "\033[32m", "\033[36m", "\033[90m"


SEVERITY_COLORS = {"Error": "RED", "Warning": "YEL", "Information": "GRY", "Hint": "GRY"}


def setup_console(json_mode):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    if json_mode or os.environ.get("NO_COLOR") or not sys.stdout.isatty():
        return
    if os.name == "nt":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
        except Exception:
            return
    C.enable()


class CheckError(Exception):
    """Проверка не может быть выполнена; key - короткий машинный код причины."""

    def __init__(self, key, message):
        super().__init__(message)
        self.key = key


# ===== Установка, Java, движок =====
def install_home():
    """Каталог установки: VANTEAM_BSL_HOME, иначе %LOCALAPPDATA%\\vanteam-bsl-server."""
    explicit = os.environ.get("VANTEAM_BSL_HOME")
    if explicit:
        return Path(explicit)
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / ".local" / "share")
    return Path(base) / "vanteam-bsl-server"


def load_install(home):
    """install.json установки или None; повреждённый файл - ошибка, а не молчаливая работа без установки."""
    path = home / INSTALL_FILE
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise CheckError("invalid_install", f"{path} не читается: {error}") from None
    if not isinstance(data, dict):
        raise CheckError("invalid_install", f"{path}: ожидался объект JSON")
    return data


def java_executable(home_dir):
    return Path(home_dir) / "bin" / ("java.exe" if os.name == "nt" else "java")


def read_release(java_home):
    """Поля файла release JDK (JAVA_VERSION, JAVA_RUNTIME_VERSION ...) или {}."""
    values = {}
    try:
        text = (Path(java_home) / "release").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return values
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            values[key.strip()] = value.strip().strip('"')
    return values


def java_home_of(java):
    return Path(java).resolve().parent.parent


def jvm_fingerprint(java):
    """Отпечаток сборки JVM для привязки архива CDS: путь, версия из release, размер и время jvm и modules."""
    home = java_home_of(java)
    fingerprint = {"home": str(home), "runtime": read_release(home).get("JAVA_RUNTIME_VERSION")}
    for relative in ("bin/server/jvm.dll", "lib/server/libjvm.so", "lib/modules"):
        path = home / relative
        if path.is_file():
            stat = path.stat()
            fingerprint[relative] = [stat.st_size, stat.st_mtime_ns]
    return fingerprint


def parse_java_version(text):
    """major из «21.0.12», «1.8.0_431» или вывода java -version."""
    match = re.search(r'(?:version ")?(\d+)(?:\.(\d+))?', text or "")
    if not match:
        return None
    major = int(match.group(1))
    if major == 1 and match.group(2):
        return int(match.group(2))
    return major


def java_version(java):
    """(major, строка версии): из release JDK, иначе из java -version; (None, '') если не удалось."""
    release = read_release(java_home_of(java)).get("JAVA_VERSION")
    if release:
        return parse_java_version(release), release
    try:
        out = subprocess.run([str(java), "-version"], capture_output=True, text=True, timeout=30,
                             encoding="utf-8", errors="replace")
    except (OSError, subprocess.SubprocessError):
        return None, ""
    text = (out.stderr or "") + (out.stdout or "")
    match = re.search(r'version "([^"]+)"', text)
    return (parse_java_version(match.group(1)) if match else None), (match.group(1) if match else "")


def find_java(install):
    """(путь, источник). Порядок: VANTEAM_BSL_JAVA, JDK установки, JAVA_HOME, PATH."""
    explicit = os.environ.get("VANTEAM_BSL_JAVA")
    if explicit:
        # Заданный, но отсутствующий java - ошибка, а не молчаливая подмена другой Java.
        if Path(explicit).is_file():
            return explicit, "VANTEAM_BSL_JAVA"
        raise CheckError("missing_java", f"VANTEAM_BSL_JAVA={explicit}: файла нет")
    recorded = ((install or {}).get("java") or {}).get("path")
    if recorded and Path(recorded).is_file():
        return recorded, "JDK установки"
    java_home = os.environ.get("JAVA_HOME")
    if java_home and java_executable(java_home).is_file():
        return str(java_executable(java_home)), "JAVA_HOME"
    which = shutil.which("java")
    if which:
        return which, "PATH"
    raise CheckError("missing_java", f"java не найдена (нужна Java {MINIMUM_JAVA}+): "
                                     "задайте VANTEAM_BSL_JAVA или выполните setup_bsl_server.py --java <JDK>")


def file_stamp(path):
    stat = Path(path).stat()
    return [stat.st_size, stat.st_mtime_ns]


def jar_version(path):
    match = re.search(r"bsl-language-server-(.+?)(?:-exec)?\.jar$", Path(path).name)
    return match.group(1) if match else "?"


def resolve_engine():
    """Движок и Java: dict с путями запуска; CheckError, если проверка невозможна."""
    home = install_home()
    install = load_install(home)
    java, java_source = find_java(install)
    major, version_text = java_version(java)
    # Классы BSL LS 1.0.7 и зависимостей собраны под Java 21 (class file 65); ниже 21 запуск невозможен.
    if major is None or major < MINIMUM_JAVA:
        raise CheckError("old_java", f"Java {version_text or '?'} ({java}, {java_source}): "
                                     f"BSL LS 1.0.7 требует Java {MINIMUM_JAVA}+")
    engine = {"home": str(home), "java": str(java), "java_source": java_source, "java_version": version_text,
              "java_major": major, "notes": [], "cds_archive": None, "help_dir": None, "cache_dir": None,
              "tmp_root": None, "default_config": None}
    override = os.environ.get("BSL_LS_JAR")
    if override:
        # Заданный, но отсутствующий JAR - ошибка, а не молчаливая подмена установленным.
        if not Path(override).is_file():
            raise CheckError("missing_jar", f"BSL_LS_JAR={override}: файла нет")
        engine.update(jar=str(Path(override).resolve()), layout="fat JAR из BSL_LS_JAR, без CDS",
                      version=jar_version(override), sha256=None)
    elif install is None:
        raise CheckError("missing_install", f"нет установки BSL Server в {home} (нет {INSTALL_FILE}): "
                                            "выполните setup_bsl_server.py или задайте VANTEAM_BSL_HOME")
    else:
        record = install.get("engine") or {}
        fat = home / record.get("jar", "")
        if not record.get("jar") or not fat.is_file():
            raise CheckError("missing_jar", f"в установке {home} нет JAR движка: выполните setup_bsl_server.py")
        if record.get("stamp") and file_stamp(fat) != record["stamp"]:
            raise CheckError("changed_jar", f"JAR движка {fat} изменён после установки: "
                                            "выполните setup_bsl_server.py")
        engine.update(jar=str(fat), layout="fat JAR", version=record.get("version") or jar_version(fat),
                      sha256=record.get("sha256"))
        reason = cds_unusable_reason(home, install, java)
        if reason:
            engine["notes"].append(f"CDS не используется: {reason}")
        else:
            layout = install["layout"]
            engine.update(jar=str(home / layout["jar"]), layout="распакованный JAR + CDS",
                          cds_archive=str(home / install["cds"]["archive"]))
        help_record = install.get("help") or {}
        if help_record.get("dir") and all((home / help_record["dir"] / name).is_file()
                                          for name in help_record.get("files", {})):
            engine["help_dir"] = str(home / help_record["dir"])
        for key, name in (("cache_dir", "cache"), ("tmp_root", "tmp")):
            if install.get(key):
                engine[key] = str(home / install[key])
        if install.get("config") and (home / install["config"]).is_file():
            engine["default_config"] = str(home / install["config"])
    if engine["tmp_root"] is None:
        engine["tmp_root"] = str(home / "tmp")
    return engine


def cds_unusable_reason(home, install, java):
    """Почему распакованный JAR с архивом CDS нельзя использовать; None - можно."""
    cds, layout, record = install.get("cds") or {}, install.get("layout") or {}, install.get("engine") or {}
    if cds.get("status") != "ok":
        return cds.get("reason") or "архив не обучен (setup_bsl_server.py)"
    archive, jar = home / cds.get("archive", ""), home / layout.get("jar", "")
    if not cds.get("archive") or not archive.is_file():
        return "нет файла архива"
    if not layout.get("jar") or not jar.is_file():
        return "нет распакованного JAR"
    if layout.get("stamp") and file_stamp(jar) != layout["stamp"]:
        return "распакованный JAR изменён после обучения"
    if cds.get("jar_sha256") != record.get("sha256"):
        return "архив обучен на другом JAR"
    if cds.get("java") != jvm_fingerprint(java):
        return "архив обучен на другой сборке Java"
    return None


def choose_xmx(modules, size, target_size=0):
    """(значение -Xmx, источник). VANTEAM_BSL_XMX - явное значение; иначе по размеру области и целей."""
    explicit = os.environ.get("VANTEAM_BSL_XMX")
    if explicit:
        if not XMX_FORMAT.match(explicit.strip()):
            raise CheckError("invalid_xmx", f"VANTEAM_BSL_XMX={explicit}: ожидается размер вида 768m или 2g")
        return explicit.strip(), "VANTEAM_BSL_XMX"
    if modules > XMX_LARGE_MODULES or size > XMX_LARGE_BYTES:
        return XMX_LARGE, f"область больше {XMX_LARGE_MODULES} модулей или {XMX_LARGE_BYTES // 10**6} МБ"
    if target_size > XMX_LARGE_TARGET_BYTES:
        return XMX_LARGE, f"модули к проверке больше {XMX_LARGE_TARGET_BYTES // 10**6} МБ"
    return XMX_DEFAULT, "по умолчанию"


# ===== Замок анализа =====
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
def analysis_lock(timeout=LOCK_TIMEOUT_SEC, poll=0.5, out=None):
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
                if not waiting and out is not None:
                    out.info(f"      {C.YEL}Ожидание: в проекте уже идёт анализ BSL LS{C.RESET}")
                    waiting = True
                time.sleep(poll)
        try:
            yield
        finally:
            _unlock(stream)


# ===== Область анализа =====
def source_files(directory, recursive):
    files = directory.rglob("*") if recursive else directory.iterdir()
    return sorted(p for p in files if p.suffix.lower() in SOURCE_SUFFIXES and p.is_file())


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
            raise ValueError(f"Модуль вне области --source-dir {src_dir}: {src_path}")
        return src_dir, None
    src_dir = src_path.parent
    nested = any(p.suffix.lower() in SOURCE_SUFFIXES and p.parent != src_dir and p.is_file()
                 for p in src_dir.rglob("*"))
    if not nested:
        return src_dir, None
    return src_dir, source_files(src_dir, recursive=False)


def plan_groups(paths, source_dir=None, standalone=False):
    """(группы, отклонённые модули). Группа - одна JVM: область и её цели в порядке указания."""
    groups, rejected, by_scope, scopes = [], [], {}, {}
    for raw in paths:
        path = Path(raw)
        if not path.is_file():
            rejected.append((str(raw), "file_not_found", f"файл не найден: {raw}"))
            continue
        src_path = path.resolve()
        if src_path.suffix.lower() not in SOURCE_SUFFIXES:
            rejected.append((str(src_path), "not_bsl", f"не модуль .bsl/.os: {src_path}"))
            continue
        try:
            if source_dir or standalone:
                src_dir, staged = analysis_scope(src_path, source_dir, standalone)
            else:
                # Область по умолчанию одна на каталог: обход каталога - один раз, а не на каждый модуль.
                if src_path.parent not in scopes:
                    scopes[src_path.parent] = analysis_scope(src_path)
                src_dir, staged = scopes[src_path.parent]
        except ValueError as error:
            rejected.append((str(src_path), "outside_scope", str(error)))
            continue
        key = ("standalone", src_path) if standalone else (src_dir, staged is not None)
        group = by_scope.get(key)
        if group is None:
            group = {"scope_dir": src_dir, "staged": staged, "targets": [], "standalone": standalone,
                     "source_dir": bool(source_dir)}
            by_scope[key] = group
            groups.append(group)
        if src_path not in group["targets"]:
            group["targets"].append(src_path)
    for group in groups:
        files = group["staged"] if group["staged"] is not None else source_files(
            group["scope_dir"], recursive=True)
        group["modules"] = len(files)
        group["bytes"] = sum(p.stat().st_size for p in files)
        group["target_bytes"] = sum(p.stat().st_size for p in group["targets"])
    return groups, rejected


def describe_scope(group):
    if group["standalone"]:
        return "только файл (--standalone)"
    if group["staged"] is None:
        return f"{group['scope_dir']} рекурсивно, файлов .bsl/.os: {group['modules']}"
    return (f"{group['scope_dir']} без вложенных каталогов, файлов .bsl/.os: {group['modules']}"
            " (рекурсивный контекст: --source-dir)")


def metadata_expected(src_dir):
    """В каталоге есть корень выгрузки Designer или EDT: metadata цели обязаны загрузиться."""
    return ((src_dir / "Configuration.xml").is_file()
            or (src_dir / "Configuration" / "Configuration.mdo").is_file())


# ===== Конфигурация BSL LS =====
def resolve_config(engine):
    """(путь, источник) конфигурации BSL LS: проекта, иначе по умолчанию."""
    if PROJECT_CONFIG.is_file():
        return PROJECT_CONFIG, "проекта"
    for candidate in (BUNDLED_CONFIG, engine.get("default_config")):
        if candidate and Path(candidate).is_file():
            return Path(candidate), "по умолчанию"
    raise CheckError("missing_config", f"нет конфигурации BSL LS: ни {PROJECT_CONFIG}, ни конфига по умолчанию "
                                       f"({BUNDLED_CONFIG} или установки): выполните setup_bsl_server.py")


def absolute_from_project(value):
    """Относительный путь конфига - от корня проекта: так его разрешал BSL LS при cwd = корень проекта."""
    path = Path(value)
    return str(path if path.is_absolute() else (PROJECT_ROOT / path).resolve())


def build_settings(config, engine, source_dir):
    """(настройки для копии конфига, описание справки платформы). Исходный файл конфига не меняется."""
    try:
        settings = json.loads(Path(config).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as error:
        raise CheckError("invalid_configuration", f"конфигурация BSL LS {config} не читается: {error}") from None
    if not isinstance(settings, dict):
        raise CheckError("invalid_configuration", f"конфигурация BSL LS {config}: ожидался объект JSON")
    if source_dir is not None:
        # Явная область - также корень метаданных.
        settings["configurationRoot"] = str(source_dir)
    elif isinstance(settings.get("configurationRoot"), str):
        settings["configurationRoot"] = absolute_from_project(settings["configurationRoot"])
    platform = settings.get("v8platform")
    if platform is None:
        platform = {}
    if not isinstance(platform, dict):
        return settings, "v8platform конфига не объект - как задано"
    if platform.get("enabled") is False:
        return settings, "отключена конфигом (v8platform.enabled = false)"
    if platform.get("binPath"):
        platform["binPath"] = absolute_from_project(str(platform["binPath"]))
        settings["v8platform"] = platform
        return settings, f"binPath конфига: {platform['binPath']}"
    if engine.get("help_dir"):
        # targetVersion и прочие ключи проекта остаются как есть; подставляется только каталог справки.
        platform["binPath"] = engine["help_dir"]
        settings["v8platform"] = platform
        return settings, f"только русская справка установки: {engine['help_dir']}"
    return settings, "самая свежая установленная 1С (автоопределение BSL LS)"


# ===== Запуск и отчёт =====
def same_drive(first, second):
    return Path(first).anchor.lower() == Path(second).anchor.lower()


def bsl_ls_command(engine, src_dir, targets, java_tmp, report_dir, config, xmx, extra_jvm=()):
    """Команда BSL LS --analyze: одна JVM, цели через --target. extra_jvm - для обучения и сверки CDS."""
    cmd = [
        engine["java"],
        # JLine и разбор справки пишут во временный каталог: ASCII-путь установки вместо профиля пользователя.
        f"-Djava.io.tmpdir={java_tmp}",
        f"-Xmx{xmx}",
        *JVM_OPTIONS,
    ]
    if engine.get("cache_dir"):
        cmd.append(f"{CACHE_PROPERTY}{engine['cache_dir']}")
    if engine.get("cds_archive"):
        cmd.append(f"-XX:SharedArchiveFile={engine['cds_archive']}")
    cmd += [
        *extra_jvm,
        "-jar", str(engine["jar"]),
        "--analyze",
        "--srcDir", str(src_dir),
        "--reporter", "json",
        "--outputDir", str(report_dir),
        "--configuration", str(config),
        "--silent",
    ]
    target_args = [arg for target in targets for arg in ("--target", str(target))]
    if len(subprocess.list2cmdline(cmd + target_args)) <= COMMAND_LINE_LIMIT:
        return cmd + target_args
    # Сотни целей не помещаются в командную строку Windows (32 767 символов): picocli BSL LS читает их
    # из файла аргументов @файл. Формат picocli: значение в кавычках, обратная косая черта экранируется.
    args_file = Path(report_dir).parent / "targets.args"
    lines = []
    for target in targets:
        value = Path(target).as_posix() if os.name == "nt" else str(target)
        lines.append('--target "%s"' % value.replace("\\", "\\\\").replace('"', '\\"'))
    args_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return cmd + [f"@{args_file}"]


def engine_failure(output):
    """Первая строка лога WARN/ERROR в выводе JVM, иначе первый маркер неполного анализа; иначе None."""
    line = ENGINE_LOG_PROBLEM.search(output)
    if line:
        return line.group(0).strip()
    return next((marker for marker in ENGINE_FAILURE_MARKERS if marker in output), None)


def describe_platform(output, source):
    """Источник контекста платформы 1С по выводу BSL LS."""
    loaded = PLATFORM_CONTEXT.search(output)
    if not loaded:
        return f"встроенные описания BSL LS (справка 1С не загружена; {source})"
    cache = [match.group(1) for match in PLATFORM_CACHE.finditer(output)]
    cache_text = f", кэш: {' + '.join(cache)}" if cache else ""
    return f"синтакс-помощник 1С, контекстов: {loaded.group(1)}{cache_text}; {source}"


def report_path(raw_path, workspace):
    if raw_path.startswith("file:"):
        uri = urlsplit(raw_path)
        raw_path = url2pathname(("//" + uri.netloc if uri.netloc else "") + uri.path)
    path = Path(raw_path)
    if not path.is_absolute():
        path = Path(workspace) / path
    return path.resolve()


def report_file_infos(data):
    file_infos = data.get("fileinfos", data.get("fileInfos")) if isinstance(data, dict) else None
    if not isinstance(file_infos, list):
        raise ValueError("В отчёте отсутствует список fileinfos")
    return file_infos


def index_report(data, workspace):
    """{полный путь: [записи отчёта]} - один проход по отчёту на запуск, а не на каждую цель."""
    index = {}
    for info in report_file_infos(data):
        if isinstance(info, dict):
            index.setdefault(report_path(str(info.get("path", "")), workspace), []).append(info)
    return index


def target_findings(data, src_path, workspace, index=None):
    """(diagnostics, mdoRef) цели по полному пути отчёта; отсутствие цели не означает успех."""
    if index is None:
        index = index_report(data, workspace)
    matches = index.get(Path(src_path).resolve(), [])
    if len(matches) != 1:
        raise ValueError(f"Модуль должен встречаться в отчёте ровно один раз, найдено {len(matches)}: {src_path}")
    diagnostics = matches[0].get("diagnostics")
    if not isinstance(diagnostics, list):
        raise ValueError("В отчёте модуля отсутствует список diagnostics")
    for diagnostic in diagnostics:
        if not isinstance(diagnostic, dict) or diagnostic.get("severity") not in SEVERITY_LABELS:
            raise ValueError("В отчёте модуля отсутствует или неизвестна severity диагностики")
    return diagnostics, matches[0].get("mdoRef")


def finding(diagnostic):
    rng = diagnostic.get("range") or {}
    line0 = (rng.get("start") or {}).get("line")
    return {
        "severity": diagnostic["severity"],
        "code": diagnostic.get("code", "?"),
        # LSP line 0-based -> 1-based, как в редакторе
        "line": line0 + 1 if isinstance(line0, int) else None,
        "message": (diagnostic.get("message") or "").replace("\n", " ").strip(),
        "range": rng,
    }


def module_code(findings):
    severities = {item["severity"] for item in findings}
    if "Error" in severities:
        return 2
    if "Warning" in severities:
        return 1
    return 0


def timeout_for(group):
    explicit = os.environ.get("VANTEAM_BSL_TIMEOUT")
    if explicit:
        try:
            return max(1, int(explicit))
        except ValueError:
            raise CheckError("invalid_timeout", f"VANTEAM_BSL_TIMEOUT={explicit}: ожидается число секунд") from None
    return BSL_LS_TIMEOUT_SEC + 5 * (len(group["targets"]) - 1) + group["modules"] // 20


def output_tail(output, limit=1500):
    return ANSI.sub("", output)[-limit:]


def run_group(engine, group, config, config_source, out):
    """Одна JVM на область: (описание запуска, {путь цели: результат модуля})."""
    run = {"scope": describe_scope(group), "scope_dir": str(group["scope_dir"]),
           "targets": [str(t) for t in group["targets"]], "modules_in_scope": group["modules"],
           "bytes_in_scope": group["bytes"], "config": f"{config} ({config_source})", "incomplete": None}

    def fail(key, message, output=""):
        run["incomplete"] = key
        run["error"] = message
        out.error(f"      {C.RED}{message}{C.RESET}")
        if output:
            out.error(output_tail(output))
        return run, {str(t): incomplete_module(t, key, message) for t in group["targets"]}

    xmx, xmx_source = choose_xmx(group["modules"], group["bytes"], group["target_bytes"])
    run["xmx"] = xmx
    source_dir = group["scope_dir"] if group["source_dir"] else None
    settings, platform_source = build_settings(config, engine, source_dir)
    out.info(f"      Область: {run['scope']}; модулей к проверке: {len(group['targets'])}")
    out.info(f"      Конфиг: {run['config']}; -Xmx{xmx} ({xmx_source})")
    tmp_root = Path(engine["tmp_root"])
    tmp_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=str(tmp_root), prefix="run_", ignore_cleanup_errors=True) as tmpdir:
        tmp = Path(tmpdir)
        src_dir, targets = group["scope_dir"], list(group["targets"])
        if group["staged"] is not None:
            src_dir = tmp / "source"
            src_dir.mkdir()
            for path in group["staged"]:
                shutil.copy2(path, src_dir / path.name)
            targets = [src_dir / t.name for t in targets]
        report_dir, java_tmp = tmp / "report", tmp / "java"
        report_dir.mkdir()
        java_tmp.mkdir()
        config_copy = tmp / "configuration.json"
        config_copy.write_text(json.dumps(settings, ensure_ascii=False, indent=1), encoding="utf-8")
        cmd = bsl_ls_command(engine, src_dir, targets, java_tmp, report_dir, config_copy, xmx)
        # cwd - корень проекта: относительные пути разрешаются от него, а не от места вызова check_bsl.
        # BSL LS строит пути отчёта через relativize от каталога запуска и падает, если область на другом
        # диске; тогда JVM запускается из корня области (относительные пути конфига уже разрешены от проекта).
        cwd = PROJECT_ROOT if same_drive(PROJECT_ROOT, src_dir) else src_dir
        run["cwd"] = str(cwd)
        run["command"] = cmd
        timeout = timeout_for(group)
        started = time.monotonic()
        try:
            with analysis_lock(out=out):
                result = subprocess.run(cmd, capture_output=True, text=True, cwd=str(cwd), encoding="utf-8",
                                        errors="replace", timeout=timeout)
        except subprocess.TimeoutExpired:
            return fail("bsl_ls_timeout", f"BSL LS: превышено время ({timeout} с). Проверка не выполнена.")
        except TimeoutError as error:
            return fail("lock_busy", f"BSL LS не запущен: {error}")
        except OSError as error:
            return fail("bsl_ls_error", f"BSL LS не запущен: {error}")
        run["seconds"] = round(time.monotonic() - started, 1)
        run["jvm_exit_code"] = result.returncode
        output = (result.stdout or "") + (result.stderr or "")
        run["platform"] = describe_platform(output, platform_source)
        cds_problem = CDS_PROBLEM.search(output) if engine.get("cds_archive") else None
        if cds_problem:
            run["cds_warning"] = cds_problem.group(0)
            out.info(f"      {C.YEL}CDS: {cds_problem.group(0)} - анализ без ускорения, находки не меняются{C.RESET}")
        if result.returncode != 0:
            if "OutOfMemoryError" in output:
                return fail("out_of_memory", f"BSL LS: нехватка памяти JVM (-Xmx{xmx}), код {result.returncode}. "
                                             "Проверка не выполнена: увеличьте кучу через VANTEAM_BSL_XMX "
                                             "(например, вдвое).", output)
            if "is not among source files" in output:
                return fail("target_outside_scope", f"BSL LS: модуль вне файлов области (код {result.returncode},"
                                                    " в том числе исключён excludePaths). Проверка не выполнена.",
                            output)
            return fail("bsl_ls_failed", f"BSL LS завершился с кодом {result.returncode}. Проверка не выполнена.",
                        output)
        marker = engine_failure(output)
        if marker:
            return fail("engine_failure", f"BSL LS: «{marker}». Проверка неполная.", output)
        report_files = list(report_dir.glob("*.json"))
        if len(report_files) != 1:
            return fail("no_report", f"BSL LS: ожидался один JSON-отчёт, найдено {len(report_files)}.", output)
        try:
            data = json.loads(report_files[0].read_text(encoding="utf-8"))
            index = index_report(data, cwd)
        except (OSError, ValueError) as error:
            return fail("invalid_report", f"Отчёт BSL LS не принят: {error}")
        modules = {}
        for original, target in zip(group["targets"], targets):
            try:
                diagnostics, mdo_ref = target_findings(data, target, cwd, index)
            except ValueError as error:
                modules[str(original)] = incomplete_module(original, "invalid_report", f"Отчёт BSL LS не принят: {error}")
                continue
            # Без metadata mdoRef цели - URI файла: межмодульные правила молча не работают.
            if source_dir and metadata_expected(src_dir) and (not mdo_ref or str(mdo_ref).startswith("file:")):
                modules[str(original)] = incomplete_module(
                    original, "metadata_not_loaded", f"BSL LS: metadata выгрузки {src_dir} не загружены для модуля "
                                                     f"(mdoRef {mdo_ref!r}). Проверка неполная.")
                continue
            findings = [finding(d) for d in diagnostics]
            code = module_code(findings)
            modules[str(original)] = {"path": str(original), "exit_code": code, "result": RESULT_NAMES[code],
                                      "incomplete": None, "mdoRef": mdo_ref, "findings": findings,
                                      "counts": count_findings(findings)}
    out.info(f"      Контекст платформы: {run['platform']}")
    out.info(f"      Время JVM: {run['seconds']:.1f} с".replace(".", ","))
    return run, modules


def count_findings(findings):
    counts = {}
    for item in findings:
        counts[item["severity"]] = counts.get(item["severity"], 0) + 1
    return counts


def incomplete_module(path, key, message):
    return {"path": str(path), "exit_code": INCOMPLETE, "result": RESULT_NAMES[INCOMPLETE], "incomplete": key,
            "error": message, "mdoRef": None, "findings": [], "counts": {}}


def describe_engine(engine):
    sha = f", sha256 {engine['sha256'][:12]}…" if engine.get("sha256") else ""
    return (f"BSL Language Server {engine['version']} ({engine['layout']}{sha}); "
            f"Java {engine['java_version']} ({engine['java_source']}: {engine['java']})")


def analyze(paths, source_dir=None, standalone=False, out=None):
    """Ядро проверки: результат dict (exit_code, modules, runs, engine). Ход работы - в out (Output)."""
    out = out or Output()
    result = {"tool": PRODUCT, "version": VERSION, "engine": None, "runs": [], "modules": [], "notes": []}
    groups, rejected = plan_groups(paths, source_dir, standalone)
    modules = {}
    for path, key, message in rejected:
        out.error(f"{C.RED}{message}{C.RESET}")
        modules[path] = incomplete_module(path, key, message)
    if groups:
        try:
            engine = resolve_engine()
            result["engine"] = {k: v for k, v in engine.items() if k != "notes"}
            out.info(f"{C.CYN}{describe_engine(engine)}{C.RESET}")
            for note in engine["notes"]:
                out.info(f"      {C.YEL}{note}{C.RESET}")
                result["notes"].append(note)
            config, config_source = resolve_config(engine)
            for group in groups:
                run, group_modules = run_group(engine, group, config, config_source, out)
                result["runs"].append(run)
                modules.update(group_modules)
        except CheckError as error:
            out.error(f"{C.RED}{error}{C.RESET}")
            result["error"] = {"key": error.key, "message": str(error)}
            for group in groups:
                for target in group["targets"]:
                    modules.setdefault(str(target), incomplete_module(target, error.key, str(error)))
    order = [str(Path(p).resolve()) if Path(p).is_file() else str(p) for p in paths]
    seen = set()
    for path in order:
        if path in modules and path not in seen:
            seen.add(path)
            result["modules"].append(modules[path])
    result["exit_code"] = max((m["exit_code"] for m in result["modules"]), default=INCOMPLETE)
    result["result"] = RESULT_NAMES[result["exit_code"]]
    return result


def print_module(module, out):
    out.error(f"{C.BOLD}=== {module['path']} ==={C.RESET}")
    if module["incomplete"]:
        out.error(f"      {C.RED}Проверка не выполнена: {module['error']}{C.RESET}")
        return
    findings = module["findings"]
    if not findings:
        out.info(f"      {C.GRN}OK: 0 findings.{C.RESET}")
        return
    out.error(f"      {C.BOLD}Findings: {len(findings)}{C.RESET}")
    for severity in SEVERITY_ORDER:
        color = getattr(C, SEVERITY_COLORS[severity])
        for item in (f for f in findings if f["severity"] == severity):
            line = item["line"] if item["line"] is not None else "?"
            out.error(f"      {color}{SEVERITY_LABELS[severity]}{C.RESET} L{line:<4} "
                 f"{C.BOLD}{item['code']}{C.RESET}: {item['message']}")


def print_result(code, out):
    if code == 0:
        out.error(f"{C.GRN}{C.BOLD}=== РЕЗУЛЬТАТ: OK ==={C.RESET}")
    elif code == 1:
        out.error(f"{C.YEL}{C.BOLD}=== РЕЗУЛЬТАТ: WARNINGS ==={C.RESET}")
    elif code == 2:
        out.error(f"{C.RED}{C.BOLD}=== РЕЗУЛЬТАТ: ERRORS ==={C.RESET}")
    else:
        out.error(f"{C.RED}{C.BOLD}=== РЕЗУЛЬТАТ: ПРОВЕРКА НЕ ВЫПОЛНЕНА ==={C.RESET}")


class Parser(argparse.ArgumentParser):
    """Ошибка аргументов - код 3 (проверка не выполнена), а не 2 (найдены ошибки в коде)."""

    def error(self, message):
        self.print_usage(sys.stderr)
        print(f"{self.prog}: ошибка: {message}", file=sys.stderr)
        sys.exit(INCOMPLETE)


def build_parser():
    parser = Parser(prog="check_bsl.py", description=f"{PRODUCT} {VERSION}: проверка BSL-модулей в BSL Language "
                                                     "Server (OneScript - отдельно, tools/check_oscript.py)")
    parser.add_argument("files", nargs="+", metavar="модуль.bsl", help="один или несколько модулей .bsl/.os")
    parser.add_argument("--deep", action="store_true", help="принимается для совместимости, ничего не меняет")
    parser.add_argument("--all", action="store_true", help="принимается для совместимости: только BSL LS")
    parser.add_argument("--quiet", action="store_true", help="только находки и итог")
    parser.add_argument("--json", action="store_true", help="итог одним JSON-объектом в stdout")
    parser.add_argument("--version", action="version", version=f"{PRODUCT} {VERSION}")
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--source-dir", help="явный каталог контекста BSL LS (рекурсивно, корень метаданных)")
    scope.add_argument("--standalone", action="store_true",
                       help="только сам файл, без соседних модулей и метаданных")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    setup_console(args.json)
    out = Output(quiet=args.quiet, collect=args.json)
    if args.all:
        # Отдельной строкой в stderr при --json, чтобы stdout оставался одним JSON.
        note = "OneScript проверяется отдельно: tools/check_oscript.py"
        print(note, file=sys.stderr if args.json else sys.stdout, flush=True)
    result = analyze(args.files, source_dir=args.source_dir, standalone=args.standalone, out=out)
    if args.json:
        result["messages"] = out.messages
        print(json.dumps(result, ensure_ascii=False, indent=1, default=str))
    else:
        for module in result["modules"]:
            print_module(module, out)
        print_result(result["exit_code"], out)
    return result["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
