# -*- coding: utf-8 -*-
"""Новые находки BSL Language Server в модуле относительно версии из git (по умолчанию HEAD).

Только BSL LS: OneScript здесь не вызывается (синтаксис OneScript - отдельно, tools/check_oscript.py).
Ядро - check_bsl.analyze из того же каталога: та же установка, область, защиты и коды неполной проверки.

Большие модули несут сотни старых предупреждений; правило проекта - 0 ERROR и 0 НОВЫХ WARN в
затронутом модуле. Скрипт проверяет текущий файл и его версию из git в той же области анализа,
сравнивает находки по ключу (уровень, правило, сообщение, текст строки без отступов) как мультимножества
и печатает только появившиеся. Уровень HINT не выводится.

База - все файлы области анализа в ревизии, байты как при checkout (git cat-file --filters: autocrlf, фильтры):
  по умолчанию - файлы .bsl/.os каталога модуля (область check_bsl по умолчанию);
  --source-dir DIR - все файлы каталога рекурсивно: модули и метаданные выгрузки;
  --standalone - только сам модуль.
Базовый прогон пропускается только если набор и байты всех файлов области совпадают с рабочим деревом.
Файла нет в ревизии - база пустая, все находки новые.

Код возврата: 0 - нет новых ERROR/WARN и ERROR в модуле; 1 - есть новые ERROR/WARN или ERROR в модуле;
2 - проверка не выполнена (проверка BSL LS неполная, сбой git, неверные аргументы).

Запуск из корня проекта:
  python tools/bsl_new_findings.py <путь Module.bsl> [ревизия] [--source-dir DIR | --standalone]
"""
import argparse
import collections
import os
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PurePosixPath

ИНСТРУМЕНТЫ = Path(__file__).resolve().parent
sys.path.insert(0, str(ИНСТРУМЕНТЫ))
import check_bsl  # noqa: E402

# Каталог временной базы; None - временный каталог установки BSL Server (tmp), на её диске.
ВРЕМЕННЫЕ = None
РАСШИРЕНИЯ = check_bsl.SOURCE_SUFFIXES
УРОВНИ = {'Error': 'ERROR', 'Warning': 'WARN', 'Information': 'INFO', 'Hint': 'HINT'}
ПОТОКИ_GIT = 8


class СбойПроверки(RuntimeError):
    """Проверка не выполнена: результат нельзя считать ни успехом, ни списком находок."""


def git(корень, *аргументы):
    рез = subprocess.run(['git', *аргументы], capture_output=True, cwd=корень)
    if рез.returncode != 0:
        raise СбойПроверки('git %s: %s' % (' '.join(аргументы),
                                           рез.stderr.decode('utf-8', 'replace').strip()))
    return рез.stdout


def область(путь, source_dir, standalone):
    """(каталог области, режим): 'каталог' - .bsl/.os каталога, 'рекурсивно' - все файлы, 'файл' - только модуль."""
    if standalone:
        return путь.parent, 'файл'
    if source_dir:
        каталог = Path(source_dir).resolve()
        if not каталог.is_dir() or not путь.is_relative_to(каталог):
            raise СбойПроверки('Модуль вне области --source-dir %s: %s' % (каталог, путь))
        return каталог, 'рекурсивно'
    return путь.parent, 'каталог'


def входит(относительный, режим, имя_модуля):
    if режим == 'файл':
        return относительный == имя_модуля
    if режим == 'каталог':
        return '/' not in относительный and PurePosixPath(относительный).suffix.lower() in РАСШИРЕНИЯ
    return True


def файлы_каталога(каталог, режим, имя_модуля):
    """Файлы области в рабочем дереве: {путь относительно области (posix): байты}."""
    обход = каталог.rglob('*') if режим == 'рекурсивно' else каталог.iterdir()
    итог = {}
    for п in обход:
        отн = п.relative_to(каталог).as_posix()
        if п.is_file() and входит(отн, режим, имя_модуля):
            итог[отн] = п.read_bytes()
    return итог


def файлы_ревизии(корень, ревизия, каталог, режим, имя_модуля):
    """Файлы области в ревизии; байты как при checkout (autocrlf, фильтры)."""
    путь_каталога = [] if каталог == '.' else [каталог + '/']
    ключи = ['-r'] if режим == 'рекурсивно' else []
    пути = {}
    for запись in git(корень, 'ls-tree', '-z', *ключи, ревизия, '--', *путь_каталога).split(b'\0'):
        if not запись:
            continue
        заголовок, путь = запись.decode('utf-8').split('\t', 1)
        отн = путь[len(каталог) + 1:] if каталог != '.' else путь
        if заголовок.split()[1] == 'blob' and входит(отн, режим, имя_модуля):
            пути[отн] = путь
    # Пакетный cat-file --batch --filters пишет в заголовке размер до фильтра - разбор по нему ненадёжен,
    # поэтому по одному процессу на файл, параллельно.
    with ThreadPoolExecutor(max_workers=ПОТОКИ_GIT) as пул:
        байты = пул.map(lambda путь: git(корень, 'cat-file', '--filters', '%s:%s' % (ревизия, путь)),
                        пути.values())
        return dict(zip(пути, байты))


def проверить(путь, source_dir=None, standalone=False):
    """Находки BSL LS без HINT: [(уровень, правило, сообщение, текст строки, номер)]; неполная - СбойПроверки."""
    вывод = check_bsl.Output(collect=True)
    итог = check_bsl.analyze([str(путь)], source_dir=source_dir, standalone=standalone, out=вывод)
    модули = итог['modules']
    if len(модули) != 1 or модули[0]['incomplete'] or итог['exit_code'] not in (0, 1, 2):
        причина = модули[0].get('error') if модули else 'нет результата'
        raise СбойПроверки('check_bsl не выполнил проверку %s: %s\n%s' % (
            путь, причина, '\n'.join(вывод.messages)[-3000:]))
    строки = Path(путь).read_text(encoding='utf-8-sig', errors='replace').split('\n')
    находки = []
    for н in модули[0]['findings']:
        уровень = УРОВНИ[н['severity']]
        if уровень == 'HINT':
            continue
        номер = н['line'] or 0
        текст = строки[номер - 1].strip() if 0 < номер <= len(строки) else ''
        находки.append((уровень, н['code'], н['message'], текст, номер))
    return находки


def временный_корень():
    корень = Path(ВРЕМЕННЫЕ) if ВРЕМЕННЫЕ else check_bsl.install_home() / 'tmp'
    корень.mkdir(parents=True, exist_ok=True)
    return корень


def базовые_находки(путь, корень, ревизия, стало, source_dir=None, standalone=False):
    каталог, режим = область(путь, source_dir, standalone)
    отн_каталог = каталог.relative_to(корень).as_posix()
    отн_модуль = путь.relative_to(каталог).as_posix()
    база = файлы_ревизии(корень, ревизия, отн_каталог, режим, путь.name)
    if отн_модуль not in база:
        print('В ревизии %s файла нет: база пустая, все находки новые.' % ревизия)
        return []
    if база == файлы_каталога(каталог, режим, путь.name):
        print('Область совпадает с ревизией %s: базовый прогон не нужен.' % ревизия)
        return стало
    print('База: ревизия %s, файлов области: %d' % (ревизия, len(база)))
    with tempfile.TemporaryDirectory(prefix='bslbase_', dir=временный_корень(),
                                     ignore_cleanup_errors=True) as временный:
        for отн, байты in база.items():
            файл = Path(временный) / отн
            файл.parent.mkdir(parents=True, exist_ok=True)
            файл.write_bytes(байты)
        return проверить(Path(временный) / отн_модуль, временный if режим == 'рекурсивно' else None,
                         режим == 'файл')


class Parser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        print('%s: ошибка: %s' % (self.prog, message), file=sys.stderr)
        sys.exit(2)


def main(argv=None):
    parser = Parser(prog='bsl_new_findings.py', description='Новые находки BSL LS относительно ревизии git')
    parser.add_argument('module', help='путь к модулю .bsl/.os')
    parser.add_argument('revision', nargs='?', default='HEAD', help='ревизия git, по умолчанию HEAD')
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument('--source-dir', help='явный каталог контекста BSL LS (как у check_bsl.py)')
    scope.add_argument('--standalone', action='store_true', help='только сам модуль')
    args = parser.parse_args(argv)
    for поток in (sys.stdout, sys.stderr):
        try:
            поток.reconfigure(errors='replace')
        except (AttributeError, ValueError):
            pass
    путь = Path(args.module).resolve()
    ревизия = args.revision
    try:
        if not путь.is_file():
            raise СбойПроверки('файл не найден: %s' % путь)
        корень = Path(git(путь.parent, 'rev-parse', '--show-toplevel').decode('utf-8').strip()).resolve()
        git(корень, 'rev-parse', '--verify', '--quiet', ревизия + '^{commit}')
        if args.source_dir and not Path(args.source_dir).resolve().is_relative_to(корень):
            raise СбойПроверки('--source-dir вне репозитория %s' % корень)
        стало = проверить(путь, args.source_dir, args.standalone)
        было = collections.Counter(н[:4] for н in базовые_находки(путь, корень, ревизия, стало,
                                                                   args.source_dir, args.standalone))
    except (OSError, ValueError, UnicodeDecodeError, СбойПроверки) as ошибка:
        print(ошибка)
        print('== проверка не выполнена')
        return 2
    новые = []
    for н in стало:
        ключ = н[:4]
        if было[ключ] > 0:
            было[ключ] -= 1
        else:
            новые.append(н)
    for уровень, правило, сообщение, _, номер in sorted(новые, key=lambda х: х[4]):
        print('НОВОЕ %-5s L%-5d %s: %s' % (уровень, номер, правило, сообщение))
    серьёзных = sum(1 for н in новые if н[0] in ('ERROR', 'WARN'))
    ошибок = sum(1 for н in стало if н[0] == 'ERROR')
    print('== новых ERROR/WARN: %d, новых INFO: %d, ERROR всего в модуле: %d' % (
        серьёзных, len(новые) - серьёзных, ошибок))
    return 1 if серьёзных or ошибок else 0


if __name__ == '__main__':
    sys.exit(main())
