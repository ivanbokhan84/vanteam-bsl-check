# -*- coding: utf-8 -*-
"""Новые находки BSL Language Server в модуле относительно версии из git (по умолчанию HEAD).

Большие модули несут сотни старых предупреждений; правило проекта - 0 ERROR и 0 НОВЫХ WARN в
затронутом модуле. Скрипт прогоняет tools/check_bsl.py --all по текущему файлу и --deep по его
версии из git, сравнивает находки по ключу (уровень, правило, сообщение, текст строки без отступов)
как мультимножества и печатает только появившиеся. Уровень HINT не выводится.

Контекст базы: область check_bsl по умолчанию - файлы .bsl/.os каталога цели, поэтому база
собирается из файлов того же каталога в ревизии (с теми же фильтрами git, что при checkout).
Базовый прогон пропускается только если набор и байты всех этих файлов совпадают с рабочим деревом.
Файла нет в ревизии - база пустая, все находки новые.

oscript -check выполняется только для текущей версии: standalone, без метаданных, до первой ошибки.
Его результат печатается отдельно. «Неизвестный символ» без подтверждения происхождения означает
неполную проверку (код 2), а не успешный результат; прочая ошибка - ERROR.

Код возврата: 0 - нет новых ERROR/WARN и ERROR в модуле; 1 - есть новые ERROR/WARN, ERROR в модуле
или ошибка oscript кроме «Неизвестный символ»; 2 - проверка не выполнена (сбой check_bsl или git,
нет итога, число разобранных находок не совпало с итогом).

Запуск из корня проекта: python tools/bsl_new_findings.py <путь Module.bsl> [ревизия]
"""
import collections
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath

ИНСТРУМЕНТЫ = Path(__file__).resolve().parent
ПРОВЕРКА = ИНСТРУМЕНТЫ / 'check_bsl.py'
ВРЕМЕННЫЕ = ИНСТРУМЕНТЫ.parent / '_temp'
РАСШИРЕНИЯ = ('.bsl', '.os')
ШАБЛОН = re.compile(r'^\s+(ERROR|WARN|INFO|HINT)\s+L(\d+)\s+(\w+): (.*)$')
ИТОГ = re.compile(r'^\s+Findings: (\d+)\s*$')
СТРОКА_OSCRIPT = re.compile(r'^\s+\| (.*)$')


class СбойПроверки(RuntimeError):
    """Проверка не выполнена: результат нельзя считать ни успехом, ни списком находок."""


def git(корень, *аргументы):
    рез = subprocess.run(['git', *аргументы], capture_output=True, cwd=корень)
    if рез.returncode != 0:
        raise СбойПроверки('git %s: %s' % (' '.join(аргументы),
                                           рез.stderr.decode('utf-8', 'replace').strip()))
    return рез.stdout


def файлы_каталога(каталог):
    return {п.name: п.read_bytes() for п in каталог.iterdir()
            if п.is_file() and п.suffix.lower() in РАСШИРЕНИЯ}


def файлы_ревизии(корень, ревизия, каталог):
    """Файлы .bsl/.os каталога в ревизии; байты как при checkout (autocrlf, фильтры)."""
    путь_каталога = [] if каталог == '.' else [каталог + '/']
    итог = {}
    for запись in git(корень, 'ls-tree', '-z', ревизия, '--', *путь_каталога).split(b'\0'):
        if not запись:
            continue
        заголовок, путь = запись.decode('utf-8').split('\t', 1)
        if заголовок.split()[1] == 'blob' and PurePosixPath(путь).suffix.lower() in РАСШИРЕНИЯ:
            итог[PurePosixPath(путь).name] = git(корень, 'cat-file', '--filters',
                                                  '%s:%s' % (ревизия, путь))
    return итог


def разобрать_вывод(вывод, путь):
    """(находки BSL LS без HINT, строки первой ошибки oscript) из вывода check_bsl."""
    строки = Path(путь).read_text(encoding='utf-8-sig').split('\n')
    находки, oscript = [], []
    итог, разобрано, внутри_oscript = None, 0, False
    for стр in вывод.split('\n'):
        if 'ОШИБКИ синтаксиса:' in стр:
            внутри_oscript = True
            continue
        m = СТРОКА_OSCRIPT.match(стр)
        if внутри_oscript and m:
            oscript.append(m.group(1))
            continue
        внутри_oscript = False
        if 'OK: 0 findings.' in стр:
            итог = 0
        m = ИТОГ.match(стр)
        if m:
            итог = int(m.group(1))
        m = ШАБЛОН.match(стр)
        if not m:
            continue
        разобрано += 1
        if m.group(1) == 'HINT':
            continue
        номер = int(m.group(2))
        текст = строки[номер - 1].strip() if 0 < номер <= len(строки) else ''
        находки.append((m.group(1), m.group(3), m.group(4).strip(), текст, номер))
    if итог is None or итог != разобрано:
        raise СбойПроверки('Итог BSL LS не совпал с разобранными находками: итог %s, разобрано %d'
                           % (итог, разобрано))
    return находки, oscript


def проверить(путь, уровень):
    среда = dict(os.environ, PYTHONIOENCODING='utf-8')
    рез = subprocess.run([sys.executable, str(ПРОВЕРКА), str(путь), уровень],
                         capture_output=True, env=среда)
    вывод = re.sub(r'\x1b\[[0-9;]*m', '', рез.stdout.decode('utf-8', 'replace'))
    # 0/1/2 - проверка выполнена (2 - найдены ошибки в коде); 3 и прочее - не выполнена.
    if рез.returncode not in (0, 1, 2) or '=== РЕЗУЛЬТАТ:' not in вывод:
        raise СбойПроверки('check_bsl не выполнил проверку (код %d):\n%s%s' % (
            рез.returncode, вывод[-2000:], рез.stderr.decode('utf-8', 'replace')[-1000:]))
    return разобрать_вывод(вывод, путь)


def базовые_находки(путь, корень, ревизия, стало):
    каталог = путь.parent.relative_to(корень).as_posix()
    база = файлы_ревизии(корень, ревизия, каталог)
    if путь.name not in база:
        print('В ревизии %s файла нет: база пустая, все находки новые.' % ревизия)
        return []
    if база == файлы_каталога(путь.parent):
        print('Каталог цели совпадает с ревизией %s: базовый прогон не нужен.' % ревизия)
        return стало
    ВРЕМЕННЫЕ.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='bslbase_', dir=ВРЕМЕННЫЕ) as временный:
        for имя, байты in база.items():
            (Path(временный) / имя).write_bytes(байты)
        находки, _ = проверить(Path(временный) / путь.name, '--deep')
    return находки


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    путь = Path(sys.argv[1]).resolve()
    ревизия = sys.argv[2] if len(sys.argv) > 2 else 'HEAD'
    try:
        корень = Path(git(путь.parent, 'rev-parse', '--show-toplevel').decode('utf-8').strip()).resolve()
        git(корень, 'rev-parse', '--verify', '--quiet', ревизия + '^{commit}')
        стало, oscript = проверить(путь, '--all')
        было = collections.Counter(н[:4] for н in базовые_находки(путь, корень, ревизия, стало))
    except (OSError, ValueError, UnicodeDecodeError, СбойПроверки) as ошибка:
        print(ошибка)
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
    ошибка_oscript = False
    неполная_oscript = False
    if oscript:
        первая = ' / '.join(oscript)
        if 'Неизвестный символ' in первая:
            print('oscript (standalone, до первой ошибки): %s - происхождение символа не подтверждено, '
                  'синтаксис после этой строки не проверен; проверка не выполнена полностью' % первая)
            неполная_oscript = True
        else:
            print('oscript ERROR: %s' % первая)
            ошибка_oscript = True
    серьёзных = sum(1 for н in новые if н[0] in ('ERROR', 'WARN'))
    ошибок = sum(1 for н in стало if н[0] == 'ERROR')
    print('== новых ERROR/WARN: %d, новых INFO: %d, ERROR всего в модуле: %d, ошибка oscript: %s' % (
        серьёзных, len(новые) - серьёзных, ошибок, 'да' if ошибка_oscript else 'нет'))
    if неполная_oscript:
        return 2
    return 1 if серьёзных or ошибок or ошибка_oscript else 0


if __name__ == '__main__':
    sys.exit(main())
