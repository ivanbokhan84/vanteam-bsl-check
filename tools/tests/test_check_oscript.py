# -*- coding: utf-8 -*-
"""Регресс tools/check_oscript.py: каталог модулей с известными ошибками и ветки препроцессора.

Запуск из корня проекта: python -m unittest tools/tests/test_check_oscript.py -v
Нужен OneScript 2.2.0-vanteam.2 (переменная VANTEAM_OSCRIPT или %LOCALAPPDATA%\\Programs\\OneScript-2.2.0-vanteam.2);
без него, а также при BSL_TESTS_SKIP_INTEGRATION=1 тесты с движком пропускаются.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest

ИНСТРУМЕНТЫ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
СКРИПТ = os.path.join(ИНСТРУМЕНТЫ, 'check_oscript.py')
КАТАЛОГ = os.path.join(ИНСТРУМЕНТЫ, 'tests', 'fixtures', 'oscript_quality')
sys.path.insert(0, ИНСТРУМЕНТЫ)
import check_oscript  # noqa: E402

ПРОПУСК = ('нет OneScript 2.2.0-vanteam.2' if check_oscript.движок() is None else
           'BSL_TESTS_SKIP_INTEGRATION' if os.environ.get('BSL_TESTS_SKIP_INTEGRATION') else None)
с_движком = unittest.skipIf(ПРОПУСК is not None, ПРОПУСК or '')
ОШИБКА = re.compile(r'^  ОШИБКА   (\S+)  стр (\d+),(\d+)  (.*?)(  \[ветка #Если\])?$', re.M)


def проверить(*пути, env=None, cwd=None):
    рез = subprocess.run([sys.executable, СКРИПТ, *пути], capture_output=True, cwd=cwd,
                         env=dict(os.environ, PYTHONIOENCODING='utf-8', **(env or {})))
    вывод = рез.stdout.decode('utf-8', 'replace') + рез.stderr.decode('utf-8', 'replace')
    return рез.returncode, вывод.replace('\r\n', '\n')


def ошибки(вывод):
    итог = {}
    for m in ОШИБКА.finditer(вывод):
        итог.setdefault(os.path.basename(m.group(1)), []).append((int(m.group(2)), m.group(4), bool(m.group(5))))
    return итог


def модуль(папка, имя, *строки):
    with open(os.path.join(папка, имя), 'w', encoding='utf-8-sig', newline='') as f:
        f.write('\r\n'.join(строки) + '\r\n')


@с_движком
class Каталог(unittest.TestCase):
    def test_все_случаи_каталога(self):
        with open(os.path.join(КАТАЛОГ, 'expected.json'), encoding='utf-8') as f:
            ожидание = json.load(f)
        rc, вывод = проверить(КАТАЛОГ)
        найдено = ошибки(вывод)
        self.assertEqual(rc, 1, вывод)
        for файл, случай in ожидание.items():
            строки = {e[0] for e in найдено.get(файл, [])}
            if случай['kind'] == 'valid':
                self.assertNotIn(файл, найдено, вывод)
            else:
                self.assertTrue(set(случай['lines']) & строки, '%s: ждали строки %s, получили %s' % (
                    файл, случай['lines'], sorted(строки)))
        self.assertIn('не проверено 0', вывод)


@с_движком
class Препроцессор(unittest.TestCase):
    def setUp(self):
        self.папка = tempfile.mkdtemp(prefix='test_check_oscript_')

    def test_ошибка_внутри_если_сервер(self):
        модуль(self.папка, 'm.bsl', '#Если Сервер Или ТолстыйКлиентОбычноеПриложение Тогда', 'Процедура А() Экспорт',
               '\tБ = 1 +;', 'КонецПроцедуры', '#КонецЕсли')
        rc, вывод = проверить(self.папка)
        self.assertEqual(rc, 1, вывод)
        self.assertEqual(ошибки(вывод)['m.bsl'], [(3, 'Ошибка в выражении', True)])

    def test_одноимённые_методы_веток_не_ошибка(self):
        модуль(self.папка, 'm.bsl', '#Если Клиент Тогда', 'Процедура П() Экспорт', '\tЦвет = "#000";', 'КонецПроцедуры',
               '#Иначе', 'Процедура П() Экспорт', 'КонецПроцедуры', '#КонецЕсли')
        rc, вывод = проверить(self.папка)
        self.assertEqual(rc, 0, вывод)

    def test_незакрытый_если(self):
        модуль(self.папка, 'm.bsl', '#Если Сервер Тогда', 'Процедура П()', 'КонецПроцедуры')
        rc, вывод = проверить(self.папка)
        self.assertEqual(rc, 1, вывод)
        self.assertIn('#КонецЕсли', вывод)

    def test_повтор_метода_со_строкой(self):
        модуль(self.папка, 'm.bsl', 'Процедура Т()', 'КонецПроцедуры', '', 'Процедура Т()', 'КонецПроцедуры')
        rc, вывод = проверить(self.папка)
        self.assertEqual(ошибки(вывод)['m.bsl'][0][:2], (4, 'Метод Т уже определен'))

    def test_опечатка_в_списке_для_ручной_проверки(self):
        модуль(self.папка, 'm.bsl', 'Процедура П()', '\tЗначение = 1;', '\tСообщить(Значенме);',
               '\tСпр = Справочники.Номенклатура;', 'КонецПроцедуры')
        rc, вывод = проверить(self.папка)
        self.assertEqual(rc, 0, вывод)
        ручные = [s for s in вывод.splitlines() if s.startswith('Проверить вручную')]
        self.assertTrue(ручные and 'Значенме' in ручные[0] and 'Справочники' not in ручные[0], вывод)


class Окружение(unittest.TestCase):
    def test_нет_движка(self):
        rc, вывод = проверить(КАТАЛОГ, env={'VANTEAM_OSCRIPT': r'C:\нет\oscript.exe'})
        self.assertEqual(rc, 3, вывод)

    @с_движком
    def test_нет_файлов(self):
        rc, вывод = проверить(cwd=tempfile.mkdtemp(prefix='test_check_oscript_'))
        self.assertEqual(rc, 0, вывод)


if __name__ == '__main__':
    unittest.main()
