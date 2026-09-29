"""Модульные тесты VANTEAM BSL Server check: процессы подменены, JVM не запускается.

Сбой не становится успехом, найденная ERROR не становится сбоем, OneScript не вызывается.
Запуск из каталога checker: python -m unittest tests.test_check_bsl -v
Интеграционные тесты на реальном JAR и Java - tests/test_integration.py.
"""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))
import check_bsl as checker  # noqa: E402
import bsl_new_findings as comparison  # noqa: E402
import setup_bsl_server as setup  # noqa: E402

FIXTURES = TOOLS / 'tests' / 'fixtures' / 'bsl_check'
FAKE_JAVA = TOOLS / 'tests' / 'fake_java.py'
CLEAN_MODULE = ('// Модуль для теста.\r\n//\r\n// Возвращаемое значение:\r\n//  Число - 1.\r\n//\r\n'
                'Функция Один() Экспорт\r\n\tВозврат 1;\r\nКонецФункции\r\n')
BAD_PROCEDURE = 'Процедура Плохая() Экспорт\r\n\tВозврат 1;\r\nКонецПроцедуры\r\n'
# Строки лога настоящего BSL LS 1.0.7 и форка при коде выхода 0.
LOG_WARN = ("2026-09-28T10:12:56.826+03:00  WARN 31768 --- [BSL Language Server] [configuration-0] "
            "c.g._.b.r.common.xstream.ExtendXStream   : Can't read file 'Configuration.xml'")
LOG_ERROR = ("2026-09-28T10:13:29.376+03:00 ERROR 16508 --- [BSL Language Server] [           main] "
             "c.g._.b.l.c.d.ParametersDeserializer     : Can't deserialize parameter configuration")
LOG_PLATFORM_DISABLED = ("2026-09-29T12:00:00.000+03:00 ERROR 4242 --- [BSL Language Server] [-types-warmup-1] "
                         "c.g._.b.l.t.r.BslContextHolder          : Failed to load platform contexts from 1C syntax "
                         "helper, platform context is disabled for this workspace: java.lang.OutOfMemoryError")
LOG_INFO = ("2026-09-28T10:09:46.750+03:00  INFO 35664 --- [BSL Language Server] [           main] "
            "c.g._.b.l.reporters.JsonReporter         : JSON report saved to report\\bsl-json.json")
LOG_PLATFORM = ("2026-09-28T09:59:59.969+03:00  INFO 31572 --- [BSL Language Server] [-types-warmup-1] "
                "_.b.l.t.r.PlatformContextProviderFactory : Loaded 2495 platform contexts from 1C syntax helper")
LOG_CACHE_HIT = ("2026-09-29T18:13:10.147+03:00  INFO 15436 --- [BSL Language Server] [-types-warmup-1] "
                 "c.g._.b.l.t.r.PlatformContextCache       : Platform context cache hit: D:\\cache\\x.bin (958 ms)")
OOM = 'Terminating due to java.lang.OutOfMemoryError: Java heap space'
ENV_KEYS = ('VANTEAM_BSL_HOME', 'VANTEAM_BSL_JAVA', 'BSL_LS_JAR', 'VANTEAM_BSL_XMX', 'VANTEAM_BSL_TIMEOUT',
            'JAVA_HOME')


def make_jdk(root, version='21.0.12', java_name=None):
    """Каталог JDK с release и пустым java: версия читается из release, запуск подменяется в тестах."""
    jdk = Path(root)
    (jdk / 'bin' / 'server').mkdir(parents=True, exist_ok=True)
    (jdk / 'lib').mkdir(exist_ok=True)
    java = jdk / 'bin' / (java_name or checker.java_executable(jdk).name)
    java.write_bytes(b'')
    (jdk / 'bin' / 'server' / 'jvm.dll').write_bytes(b'jvm')
    (jdk / 'lib' / 'modules').write_bytes(b'modules')
    (jdk / 'release').write_text(f'JAVA_VERSION="{version}"\nJAVA_RUNTIME_VERSION="{version}+7-LTS"\n',
                                 encoding='utf-8')
    return java


def make_install(home, java, cds=True, help_dir=True):
    """Установка как после setup_bsl_server.py, с фейковыми файлами движка."""
    home = Path(home)
    fat = home / 'engine' / 'abc' / 'bsl-language-server-1.0.7-vanteam.1-exec.jar'
    main = fat.parent / 'extracted' / fat.name
    main.parent.mkdir(parents=True)
    fat.write_bytes(b'fat')
    main.write_bytes(b'main')
    archive = home / 'cds' / 'a.jsa'
    archive.parent.mkdir()
    archive.write_bytes(b'cds')
    (home / 'config').mkdir()
    shutil.copy2(checker.BUNDLED_CONFIG, home / 'config' / checker.CONFIG_NAME)
    for name in ('cache', 'tmp'):
        (home / name).mkdir()
    install = {
        'checker_version': checker.VERSION,
        'engine': {'version': '1.0.7-vanteam.1', 'jar': 'engine/abc/' + fat.name, 'sha256': 'f' * 64,
                   'stamp': checker.file_stamp(fat)},
        'layout': {'jar': 'engine/abc/extracted/' + fat.name, 'stamp': checker.file_stamp(main), 'libs': 0},
        'java': {'path': str(java)},
        'cds': ({'status': 'ok', 'archive': 'cds/a.jsa', 'jar_sha256': 'f' * 64,
                 'java': checker.jvm_fingerprint(java)} if cds else {'status': 'disabled', 'reason': 'тест'}),
        'help': None, 'config': 'config/' + checker.CONFIG_NAME, 'cache_dir': 'cache', 'tmp_root': 'tmp',
    }
    if help_dir:
        help_path = home / 'help' / '8.3.27.1719'
        help_path.mkdir(parents=True)
        for name in setup.HELP_FILES:
            (help_path / name).write_bytes(name.encode())
        install['help'] = {'dir': 'help/8.3.27.1719', 'platform_version': '8.3.27.1719',
                           'files': {n: {} for n in setup.HELP_FILES}}
    (home / checker.INSTALL_FILE).write_text(json.dumps(install), encoding='utf-8')
    return install


def write_install(home, install):
    (Path(home) / checker.INSTALL_FILE).write_text(json.dumps(install), encoding='utf-8')


class Call:
    def __init__(self, cmd, kwargs):
        self.cmd, self.kwargs = cmd, kwargs
        self.src_dir = Path(cmd[cmd.index('--srcDir') + 1])
        self.targets = [Path(cmd[i + 1]) for i, arg in enumerate(cmd) if arg == '--target']
        self.args_file = next((Path(arg[1:]) for arg in cmd if arg.startswith('@')), None)
        self.args_file_format = None
        if self.args_file:
            # Формат picocli: --target "путь" в строке, обратная косая черта экранирована.
            lines = self.args_file.read_text(encoding='utf-8').splitlines()
            self.args_file_format = all(line.startswith('--target "') and line.endswith('"') for line in lines)
            for line in lines:
                self.targets.append(Path(line[len('--target "'):-1].replace('\\\\', '\\')))
        self.config = json.loads(Path(cmd[cmd.index('--configuration') + 1]).read_text(encoding='utf-8'))
        self.files = sorted(p.relative_to(self.src_dir).as_posix() for p in self.src_dir.rglob('*')
                            if p.suffix in checker.SOURCE_SUFFIXES)

    def jvm(self):
        return self.cmd[1:self.cmd.index('-jar')]

    def option(self, prefix):
        return next((arg[len(prefix):] for arg in self.cmd if arg.startswith(prefix)), None)


class EngineCase(unittest.TestCase):
    """Проект, установка и модуль во временном каталоге; JVM подменяется функцией."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.home = self.root / 'home'
        self.home.mkdir()
        self.java = make_jdk(self.root / 'jdk')
        self.install = make_install(self.home, self.java)
        self.source = self.root / 'source'
        self.source.mkdir()
        self.target = self.source / 'Module.bsl'
        self.target.write_text('// модуль\n', encoding='utf-8')
        self.stack = contextlib.ExitStack()
        self.stdout = self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.stack.enter_context(patch.dict(os.environ))
        for key in ENV_KEYS:
            os.environ.pop(key, None)
        os.environ['VANTEAM_BSL_HOME'] = str(self.home)
        for name, value in [('PROJECT_ROOT', self.root),
                            ('PROJECT_CONFIG', self.root / 'tools' / 'bsl_ls' / checker.CONFIG_NAME)]:
            self.stack.enter_context(patch.object(checker, name, value))
        self.stack.enter_context(patch.object(checker.shutil, 'which', return_value=None))
        self.calls = []

    def tearDown(self):
        self.stack.close()
        self.temp.cleanup()

    def analyze(self, report, code=0, output='', reports=1, paths=None, **scope):
        """Подменяет JVM: пишет отчёт (dict или функция от Call) в --outputDir; возвращает результат analyze."""
        def run(cmd, **kwargs):
            call = Call(cmd, kwargs)
            self.calls.append(call)
            data = report(call) if callable(report) else report
            if data is not None:
                for number in range(reports):
                    dest = Path(cmd[cmd.index('--outputDir') + 1]) / f'bsl-json{number}.json'
                    dest.write_text(json.dumps(data), encoding='utf-8')
            return subprocess.CompletedProcess(cmd, code, output, '')
        self.out = checker.Output(collect=True)
        with patch.object(checker.subprocess, 'run', side_effect=run):
            return checker.analyze(paths or [self.target], out=self.out, **scope)

    def run_report(self, report, **kwargs):
        """(код, счётчики первого модуля) - как run_bsl_ls_check обёртки 1.1.0."""
        result = self.analyze(report, **kwargs)
        return result['exit_code'], result['modules'][0]['counts']

    def report(self, diagnostics=None, path=None, mdo_ref=None, relative=False):
        path = path or self.target
        info = {'path': str(path.relative_to(self.root)) if relative else path.as_uri(),
                'diagnostics': diagnostics or []}
        if mdo_ref is not None:
            info['mdoRef'] = mdo_ref
        return {'fileinfos': [info]}

    def targets_report(self, call, diagnostics=None):
        """Отчёт как у форка с --target: по одной записи на цель."""
        return {'fileinfos': [{'path': t.as_uri(), 'mdoRef': '', 'diagnostics': diagnostics or []}
                              for t in call.targets]}


class ReportTests(EngineCase):
    """Защиты отчёта из 1.1.0: ровно один отчёт, одна цель по полному пути, известная severity."""

    def test_empty_diagnostics_are_success(self):
        self.assertEqual(self.run_report(self.report()), (0, {}))

    def test_relative_report_path_is_resolved_from_jvm_directory(self):
        self.assertEqual(self.run_report(self.report(relative=True)), (0, {}))

    def test_missing_report_is_incomplete(self):
        self.assertEqual(self.run_report(None)[0], checker.INCOMPLETE)

    def test_ambiguous_reports_are_incomplete(self):
        self.assertEqual(self.run_report(self.report(), reports=2)[0], checker.INCOMPLETE)

    def test_nonzero_exit_is_incomplete_even_with_clean_report(self):
        self.assertEqual(self.run_report(self.report(), code=1)[0], checker.INCOMPLETE)

    def test_engine_failure_messages_are_incomplete(self):
        for marker in checker.ENGINE_FAILURE_MARKERS:
            with self.subTest(marker=marker):
                self.assertEqual(self.run_report(self.report(), output=f'x {marker} y')[0], checker.INCOMPLETE)

    def test_word_error_alone_is_not_engine_failure(self):
        self.assertEqual(self.run_report(self.report(), output='ERROR counter: 0'), (0, {}))

    def test_other_module_with_same_basename_does_not_match(self):
        other = self.root / 'other' / 'Module.bsl'
        report = {'fileinfos': [{'path': other.as_uri(), 'diagnostics': []}]}
        self.assertEqual(self.run_report(report)[0], checker.INCOMPLETE)

    def test_other_module_diagnostics_are_excluded(self):
        report = self.report()
        report['fileinfos'].append({'path': (self.root / 'other' / 'Module.bsl').as_uri(),
                                    'diagnostics': [{'severity': 'Error'}]})
        self.assertEqual(self.run_report(report), (0, {}))

    def test_missing_diagnostics_is_incomplete(self):
        report = self.report()
        del report['fileinfos'][0]['diagnostics']
        self.assertEqual(self.run_report(report)[0], checker.INCOMPLETE)

    def test_duplicate_target_is_incomplete(self):
        report = self.report()
        report['fileinfos'] *= 2
        self.assertEqual(self.run_report(report)[0], checker.INCOMPLETE)

    def test_severity_is_preserved_and_error_is_not_failure(self):
        for severity, code in [('Error', 2), ('Warning', 1), ('Information', 0), ('Hint', 0)]:
            with self.subTest(severity=severity):
                self.assertEqual(self.run_report(self.report([{'severity': severity}])), (code, {severity: 1}))
        self.assertNotEqual(2, checker.INCOMPLETE)

    def test_unknown_or_missing_severity_is_incomplete(self):
        for diagnostic in ({}, {'severity': 1}, {'severity': 'NewSeverity'}, None):
            with self.subTest(diagnostic=diagnostic):
                self.assertEqual(self.run_report(self.report([diagnostic]))[0], checker.INCOMPLETE)

    def test_legacy_severity_names_are_incomplete(self):
        for severity in ('Critical', 'Major', 'Minor', 'Info'):
            with self.subTest(severity=severity):
                self.assertEqual(self.run_report(self.report([{'severity': severity}]))[0], checker.INCOMPLETE)

    def test_engine_log_warn_or_error_is_incomplete(self):
        for line in (LOG_WARN, LOG_ERROR, 'prefix\r\n' + LOG_ERROR + '\r\n'):
            with self.subTest(line=line):
                self.assertEqual(self.run_report(self.report(), output=line)[0], checker.INCOMPLETE)
        self.assertEqual(self.run_report(self.report(), output='\n'.join((LOG_INFO, LOG_PLATFORM, LOG_CACHE_HIT))),
                         (0, {}))
        self.assertEqual(checker.engine_failure(LOG_ERROR), LOG_ERROR)

    def test_platform_context_disabled_error_is_incomplete(self):
        # Форк при нехватке памяти на разборе справки пишет ERROR и выходит с кодом 0 без справки.
        result = self.analyze(self.report(), output=LOG_PLATFORM_DISABLED)
        self.assertEqual(result['exit_code'], checker.INCOMPLETE)
        self.assertEqual(result['runs'][0]['incomplete'], 'engine_failure')
        self.assertIn('platform context is disabled', result['runs'][0]['error'])

    def test_out_of_memory_exit_is_incomplete(self):
        result = self.analyze(None, code=3, output=OOM)
        self.assertEqual(result['exit_code'], checker.INCOMPLETE)
        self.assertEqual(result['modules'][0]['incomplete'], 'out_of_memory')
        self.assertIn('VANTEAM_BSL_XMX', result['modules'][0]['error'])

    def test_target_rejected_by_engine_is_incomplete(self):
        output = ("2026-09-29T12:00:00.000+03:00 ERROR 1 --- [BSL Language Server] [main] "
                  "c.g._.b.l.cli.AnalyzeCommand : Target file `X` is not among source files of `Y`")
        result = self.analyze(None, code=1, output=output)
        self.assertEqual(result['exit_code'], checker.INCOMPLETE)
        self.assertEqual(result['modules'][0]['incomplete'], 'target_outside_scope')

    def test_platform_context_source_is_printed(self):
        for output, expected in ((LOG_PLATFORM + '\n' + LOG_CACHE_HIT, 'контекстов: 2495, кэш: hit'),
                                 ('', 'встроенные описания BSL LS')):
            with self.subTest(expected=expected):
                result = self.analyze(self.report(), output=output)
                self.assertEqual(result['exit_code'], 0)
                self.assertIn(expected, result['runs'][0]['platform'])
                self.assertTrue(any('Контекст платформы: ' in m and expected in m for m in self.out.messages))


class EngineSelectionTests(EngineCase):
    """Установка, Java, CDS, флаги JVM, кэш, справка и куча."""

    def test_jvm_command(self):
        self.analyze(self.report())
        call = self.calls[0]
        self.assertEqual(call.kwargs['cwd'], str(self.root))
        self.assertNotIn('--workspaceDir', call.cmd)
        jvm = call.jvm()
        self.assertEqual(call.cmd[0], str(self.java))
        for flag in ('-XX:TieredStopAtLevel=1', '-XX:+ExitOnOutOfMemoryError',
                     f'-XX:ActiveProcessorCount={min(4, os.cpu_count() or 1)}', '-Xmx512m'):
            self.assertIn(flag, jvm)
        self.assertEqual(call.option('-Dapp.platform-context.cache.path='), str(self.home / 'cache'))
        self.assertTrue(Path(call.option('-Djava.io.tmpdir=')).is_relative_to(self.home / 'tmp'))
        self.assertIn('--silent', call.cmd)
        self.assertEqual(call.targets, [self.target])
        self.assertEqual(call.cmd[call.cmd.index('--reporter') + 1], 'json')

    def test_cds_used_with_extracted_jar_only_for_trained_java(self):
        self.analyze(self.report())
        call = self.calls[-1]
        self.assertEqual(call.option('-XX:SharedArchiveFile='), str(self.home / 'cds' / 'a.jsa'))
        self.assertEqual(Path(call.cmd[call.cmd.index('-jar') + 1]).parent.name, 'extracted')
        # Другая сборка JDK: архив не подходит - fat JAR без CDS и заметка в выводе.
        other = make_jdk(self.root / 'jdk2', '21.0.13')
        os.environ['VANTEAM_BSL_JAVA'] = str(other)
        result = self.analyze(self.report())
        call = self.calls[-1]
        self.assertIsNone(call.option('-XX:SharedArchiveFile='))
        self.assertEqual(Path(call.cmd[call.cmd.index('-jar') + 1]).parent.name, 'abc')
        self.assertEqual(result['exit_code'], 0)
        self.assertTrue(any('другой сборке Java' in n for n in result['notes']))

    def test_cds_not_used_when_disabled_or_missing(self):
        for change in ('disabled', 'archive', 'layout'):
            with self.subTest(change=change):
                install = json.loads(json.dumps(self.install))
                if change == 'disabled':
                    install['cds'] = {'status': 'disabled', 'reason': 'меньше 80%'}
                elif change == 'archive':
                    install['cds']['archive'] = 'cds/absent.jsa'
                else:
                    install['layout']['stamp'] = [0, 0]
                write_install(self.home, install)
                self.analyze(self.report())
                self.assertIsNone(self.calls[-1].option('-XX:SharedArchiveFile='))

    def test_java_search_order(self):
        installed = self.java
        java_home = make_jdk(self.root / 'java_home')
        on_path = make_jdk(self.root / 'on_path')
        explicit = make_jdk(self.root / 'explicit')
        os.environ['JAVA_HOME'] = str(java_home.parent.parent)
        install = checker.load_install(self.home)
        with patch.object(checker.shutil, 'which', return_value=str(on_path)):
            self.assertEqual(checker.find_java(install), (str(installed), 'JDK установки'))
            os.environ['VANTEAM_BSL_JAVA'] = str(explicit)
            self.assertEqual(checker.find_java(install), (str(explicit), 'VANTEAM_BSL_JAVA'))
            del os.environ['VANTEAM_BSL_JAVA']
            self.assertEqual(checker.find_java({}), (str(java_home), 'JAVA_HOME'))
            del os.environ['JAVA_HOME']
            self.assertEqual(checker.find_java({}), (str(on_path), 'PATH'))
        os.environ['VANTEAM_BSL_JAVA'] = str(self.root / 'absent' / 'java.exe')
        with self.assertRaises(checker.CheckError):
            checker.find_java(install)

    def test_old_java_is_incomplete_without_jvm_run(self):
        os.environ['VANTEAM_BSL_JAVA'] = str(make_jdk(self.root / 'jdk17', '17.0.13'))
        result = self.analyze(self.report())
        self.assertEqual(result['exit_code'], checker.INCOMPLETE)
        self.assertEqual(result['modules'][0]['incomplete'], 'old_java')
        self.assertEqual(self.calls, [])

    def test_java_version_from_release_or_version_output(self):
        self.assertEqual(checker.java_version(self.java), (21, '21.0.12'))
        self.assertEqual(checker.parse_java_version('1.8.0_431'), 8)
        bare = self.root / 'bare' / 'bin' / 'java.exe'
        bare.parent.mkdir(parents=True)
        bare.write_bytes(b'')
        output = subprocess.CompletedProcess([], 0, '', 'openjdk version "17.0.19" 2026-07-21')
        with patch.object(checker.subprocess, 'run', return_value=output):
            self.assertEqual(checker.java_version(bare), (17, '17.0.19'))

    def test_missing_installation_is_incomplete(self):
        os.environ['VANTEAM_BSL_HOME'] = str(self.root / 'absent_home')
        os.environ['VANTEAM_BSL_JAVA'] = str(self.java)
        result = self.analyze(self.report())
        self.assertEqual((result['exit_code'], result['modules'][0]['incomplete']),
                         (checker.INCOMPLETE, 'missing_install'))
        self.assertEqual(self.calls, [])

    def test_changed_engine_jar_is_incomplete(self):
        fat = self.home / self.install['engine']['jar']
        fat.write_bytes(b'other bytes')
        result = self.analyze(self.report())
        self.assertEqual(result['modules'][0]['incomplete'], 'changed_jar')
        self.assertEqual(self.calls, [])

    def test_bsl_ls_jar_override(self):
        jar = self.root / 'bsl-language-server-9.9.9-exec.jar'
        jar.write_bytes(b'')
        os.environ['BSL_LS_JAR'] = str(jar)
        result = self.analyze(self.report())
        call = self.calls[-1]
        self.assertEqual(call.cmd[call.cmd.index('-jar') + 1], str(jar))
        self.assertIsNone(call.option('-XX:SharedArchiveFile='))
        self.assertEqual(result['engine']['version'], '9.9.9')
        # Заданный, но отсутствующий JAR - ошибка, а не молчаливая подмена установленным.
        os.environ['BSL_LS_JAR'] = str(self.root / 'absent.jar')
        calls = len(self.calls)
        result = self.analyze(self.report())
        self.assertEqual(result['modules'][0]['incomplete'], 'missing_jar')
        self.assertEqual(len(self.calls), calls)

    def test_xmx_by_scope_size_and_override(self):
        self.assertEqual(checker.choose_xmx(1, 1000)[0], '512m')
        self.assertEqual(checker.choose_xmx(checker.XMX_LARGE_MODULES, checker.XMX_LARGE_BYTES)[0], '512m')
        self.assertEqual(checker.choose_xmx(checker.XMX_LARGE_MODULES + 1, 1000)[0], '1g')
        self.assertEqual(checker.choose_xmx(2, checker.XMX_LARGE_BYTES + 1)[0], '1g')
        self.assertEqual(checker.choose_xmx(2, 10**6, checker.XMX_LARGE_TARGET_BYTES)[0], '512m')
        self.assertEqual(checker.choose_xmx(2, 10**6, checker.XMX_LARGE_TARGET_BYTES + 1)[0], '1g')
        for count in range(3):
            (self.source / f'Other{count}.bsl').write_text('// сосед\n', encoding='utf-8')
        with patch.object(checker, 'XMX_LARGE_MODULES', 3):
            self.analyze(self.report())
        self.assertIn('-Xmx1g', self.calls[-1].jvm())
        with patch.object(checker, 'XMX_LARGE_TARGET_BYTES', 5):
            self.analyze(self.report())
        self.assertIn('-Xmx1g', self.calls[-1].jvm())
        os.environ['VANTEAM_BSL_XMX'] = '2g'
        self.analyze(self.report())
        self.assertIn('-Xmx2g', self.calls[-1].jvm())
        os.environ['VANTEAM_BSL_XMX'] = 'много'
        calls = len(self.calls)
        result = self.analyze(self.report())
        self.assertEqual(result['exit_code'], checker.INCOMPLETE)
        self.assertEqual(len(self.calls), calls)

    def test_help_bin_path_is_injected_into_config_copy(self):
        original = json.loads(checker.BUNDLED_CONFIG.read_text(encoding='utf-8'))
        self.analyze(self.report())
        config = self.calls[-1].config
        self.assertEqual(config['v8platform'], {'binPath': str(self.home / 'help' / '8.3.27.1719')})
        self.assertEqual(config['diagnostics'], original['diagnostics'])
        self.assertEqual(json.loads(checker.BUNDLED_CONFIG.read_text(encoding='utf-8')), original)

    def test_project_config_is_used_and_its_platform_settings_are_kept(self):
        checker.PROJECT_CONFIG.parent.mkdir(parents=True)
        cases = [({'targetVersion': '8.2.16'},
                  {'targetVersion': '8.2.16', 'binPath': str(self.home / 'help' / '8.3.27.1719')}),
                 ({'binPath': 'C:\\1C\\bin', 'targetVersion': '8.3.10'},
                  {'binPath': 'C:\\1C\\bin', 'targetVersion': '8.3.10'}),
                 ({'binPath': 'platform/bin'}, {'binPath': str((self.root / 'platform' / 'bin').resolve())}),
                 ({'enabled': False}, {'enabled': False})]
        for platform, expected in cases:
            with self.subTest(platform=platform):
                settings = {'configurationRoot': '.', 'v8platform': platform,
                            'diagnostics': {'parameters': {'Typo': False}}}
                checker.PROJECT_CONFIG.write_text(json.dumps(settings), encoding='utf-8')
                result = self.analyze(self.report())
                self.assertIn('(проекта)', result['runs'][0]['config'])
                config = self.calls[-1].config
                self.assertEqual(config['v8platform'], expected)
                # Относительный configurationRoot - от корня проекта, как при запуске JVM из него.
                self.assertEqual(config['configurationRoot'], str(self.root))
                self.assertEqual(json.loads(checker.PROJECT_CONFIG.read_text(encoding='utf-8')), settings)

    def test_no_help_dir_leaves_platform_autodetect(self):
        install = json.loads(json.dumps(self.install))
        install['help'] = None
        write_install(self.home, install)
        result = self.analyze(self.report())
        self.assertNotIn('v8platform', self.calls[-1].config)
        self.assertIn('автоопределение', result['runs'][0]['platform'])

    def test_cross_drive_scope_runs_from_scope_directory(self):
        # BSL LS строит пути отчёта через relativize от каталога запуска: другой диск - запуск из области.
        with patch.object(checker, 'same_drive', return_value=False):
            result = self.analyze(self.report())
        self.assertEqual(result['exit_code'], 0)
        self.assertEqual(self.calls[-1].kwargs['cwd'], str(self.source))
        self.assertEqual(self.calls[-1].config['configurationRoot'], str(self.root))


class ScopeTests(EngineCase):
    """Область анализа, несколько модулей, замок и аргументы."""

    def test_default_scope_is_directory_in_place(self):
        (self.source / 'Second.bsl').write_text('// сосед\n', encoding='utf-8')
        self.assertEqual(self.run_report(self.report()), (0, {}))
        call = self.calls[0]
        self.assertEqual(call.src_dir, self.source)
        self.assertEqual(call.files, ['Module.bsl', 'Second.bsl'])
        self.assertEqual(call.targets, [self.target])

    def test_nested_directories_are_not_analyzed(self):
        (self.source / 'Second.bsl').write_text('// сосед\n', encoding='utf-8')
        (self.source / 'Описание.txt').write_text('не модуль', encoding='utf-8')
        (self.source / '_archive').mkdir()
        (self.source / '_archive' / 'Old.bsl').write_text('// архив\n', encoding='utf-8')
        result = self.analyze(lambda call: self.targets_report(call))
        self.assertEqual(result['exit_code'], 0)
        call = self.calls[0]
        self.assertNotEqual(call.src_dir, self.source)
        self.assertEqual(call.files, ['Module.bsl', 'Second.bsl'])
        self.assertEqual(result['modules'][0]['path'], str(self.target))

    def test_staged_scope_ignores_report_for_original_path(self):
        (self.source / '_archive').mkdir()
        (self.source / '_archive' / 'Old.bsl').write_text('// архив\n', encoding='utf-8')
        self.assertEqual(self.run_report(self.report())[0], checker.INCOMPLETE)

    def test_explicit_scope_is_recursive_and_must_contain_target(self):
        nested = self.source / 'nested'
        nested.mkdir()
        (nested / 'Other.bsl').write_text('// другой\n', encoding='utf-8')
        self.assertEqual(self.run_report(self.report(), source_dir=str(self.root)), (0, {}))
        self.assertEqual(self.calls[0].files, ['source/Module.bsl', 'source/nested/Other.bsl'])
        self.assertEqual(self.calls[0].targets, [self.target])
        self.assertEqual(self.calls[0].config['configurationRoot'], str(self.root))

    def test_target_outside_source_dir_is_incomplete_without_jvm(self):
        nested = self.source / 'nested'
        nested.mkdir()
        result = self.analyze(self.report(), source_dir=str(nested))
        self.assertEqual(result['exit_code'], checker.INCOMPLETE)
        self.assertEqual(result['modules'][0]['incomplete'], 'outside_scope')
        self.assertEqual(self.calls, [])

    def test_designer_source_dir_requires_loaded_metadata(self):
        uri = self.target.as_uri()
        self.assertEqual(self.run_report(self.report(mdo_ref=uri), source_dir=str(self.source)), (0, {}))
        (self.source / 'Configuration.xml').write_text('<MetaDataObject/>', encoding='utf-8')
        for mdo_ref in (uri, '', None):
            with self.subTest(mdo_ref=mdo_ref):
                result = self.analyze(self.report(mdo_ref=mdo_ref), source_dir=str(self.source))
                self.assertEqual(result['modules'][0]['incomplete'], 'metadata_not_loaded')
        self.assertEqual(self.run_report(self.report(mdo_ref='CommonModule.Потребитель'),
                                         source_dir=str(self.source)), (0, {}))
        # Без --source-dir metadata не ожидаются: область по умолчанию - только каталог модуля.
        self.assertEqual(self.run_report(self.report(mdo_ref=uri)), (0, {}))

    def test_edt_source_dir_requires_loaded_metadata(self):
        (self.source / 'Configuration').mkdir()
        (self.source / 'Configuration' / 'Configuration.mdo').write_text('<mdclass/>', encoding='utf-8')
        self.assertEqual(self.run_report(self.report(mdo_ref=self.target.as_uri()), source_dir=str(self.source))[0],
                         checker.INCOMPLETE)

    def test_standalone_analyzes_only_target(self):
        (self.source / 'Second.bsl').write_text('// сосед\n', encoding='utf-8')
        result = self.analyze(lambda call: self.targets_report(call), standalone=True)
        self.assertEqual(result['exit_code'], 0)
        self.assertEqual(self.calls[0].files, ['Module.bsl'])

    def test_several_modules_one_jvm_per_scope(self):
        second = self.source / 'Second.bsl'
        second.write_text('// второй\n', encoding='utf-8')
        other_dir = self.root / 'other'
        other_dir.mkdir()
        third = other_dir / 'Module.bsl'
        third.write_text('// третий\n', encoding='utf-8')
        severities = {self.target: 'Warning', second: 'Error', third: 'Information'}

        def report(call):
            return {'fileinfos': [{'path': t.as_uri(), 'diagnostics': [{'severity': severities[t], 'code': 'X'}]}
                                  for t in call.targets]}
        result = self.analyze(report, paths=[self.target, third, second, self.target])
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.calls[0].targets, [self.target, second])
        self.assertEqual(self.calls[1].targets, [third])
        self.assertEqual([m['path'] for m in result['modules']], [str(self.target), str(third), str(second)])
        self.assertEqual([m['exit_code'] for m in result['modules']], [1, 0, 2])
        self.assertEqual(result['exit_code'], 2)

    def test_source_dir_several_modules_single_jvm(self):
        first = self.root / 'CommonModules' / 'А' / 'Ext' / 'Module.bsl'
        second = self.root / 'CommonModules' / 'Б' / 'Ext' / 'Module.bsl'
        for path in (first, second):
            path.parent.mkdir(parents=True)
            path.write_text('// модуль\n', encoding='utf-8')
        result = self.analyze(lambda call: self.targets_report(call), paths=[first, second],
                              source_dir=str(self.root))
        self.assertEqual(result['exit_code'], 0)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0].targets, [first, second])

    def test_hundreds_of_modules_go_through_argument_file(self):
        many = self.root / ('каталог_с_длинным_именем_' * 4)
        many.mkdir()
        paths = []
        for number in range(400):
            path = many / f'ОбщийМодульСДлиннымИменем{number:04}.bsl'
            path.write_text('// модуль\n', encoding='utf-8')
            paths.append(path)
        result = self.analyze(lambda call: self.targets_report(call), paths=paths)
        self.assertEqual(result['exit_code'], 0)
        self.assertEqual(len(self.calls), 1)
        call = self.calls[0]
        self.assertIsNotNone(call.args_file)
        self.assertNotIn('--target', call.cmd)
        self.assertTrue(call.args_file_format)
        self.assertEqual(call.targets, paths)
        self.assertLessEqual(len(subprocess.list2cmdline(call.cmd)), checker.COMMAND_LINE_LIMIT)
        self.assertEqual(len(result['modules']), 400)

    def test_missing_module_does_not_hide_others(self):
        result = self.analyze(lambda call: self.targets_report(call), paths=[self.root / 'absent.bsl', self.target])
        self.assertEqual([m['exit_code'] for m in result['modules']], [checker.INCOMPLETE, 0])
        self.assertEqual(result['exit_code'], checker.INCOMPLETE)
        self.assertEqual(len(self.calls), 1)

    def test_non_module_file_is_incomplete(self):
        text = self.source / 'Описание.txt'
        text.write_text('не модуль', encoding='utf-8')
        result = self.analyze(self.report(), paths=[text])
        self.assertEqual(result['modules'][0]['incomplete'], 'not_bsl')
        self.assertEqual(self.calls, [])

    def test_busy_lock_is_incomplete(self):
        @contextlib.contextmanager
        def busy(*args, **kwargs):
            raise TimeoutError('занят')
            yield
        with patch.object(checker, 'analysis_lock', busy):
            result = self.analyze(self.report())
        self.assertEqual(result['modules'][0]['incomplete'], 'lock_busy')
        self.assertEqual(self.calls, [])

    def test_timeout_is_incomplete(self):
        def run(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd, kwargs['timeout'])
        with patch.object(checker.subprocess, 'run', side_effect=run):
            result = checker.analyze([self.target], out=checker.Output(collect=True))
        self.assertEqual(result['modules'][0]['incomplete'], 'bsl_ls_timeout')


class CommandLineTests(EngineCase):
    """main(): флаги --deep/--all/--json/--quiet, коды аргументов, отсутствие вызова OneScript."""

    def main(self, *args, report=None, code=0, output=''):
        commands = []

        def run(cmd, **kwargs):
            commands.append(list(cmd))
            call = Call(cmd, kwargs)
            data = report(call) if report else self.targets_report(call)
            if data is not None:
                (Path(cmd[cmd.index('--outputDir') + 1]) / 'bsl-json.json').write_text(json.dumps(data),
                                                                                          encoding='utf-8')
            return subprocess.CompletedProcess(cmd, code, output, '')

        def forbidden(*args, **kwargs):
            commands.append(['Popen', *map(str, args[0] if args else [])])
            raise AssertionError('неожиданный процесс')
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(checker.subprocess, 'run', side_effect=run), \
                patch.object(checker.subprocess, 'Popen', side_effect=forbidden), \
                patch.object(os, 'system', side_effect=forbidden), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                exit_code = checker.main([str(a) for a in args])
            except SystemExit as error:
                exit_code = error.code
        return exit_code, stdout.getvalue(), stderr.getvalue(), commands

    def test_deep_flag_changes_nothing(self):
        def stable(command):
            # Временные каталоги прогона различаются, остальное должно совпасть.
            return [part for part in command if not part.startswith('-Djava.io.tmpdir=')
                    and str(self.home / 'tmp') not in part]
        plain = self.main(self.target)
        deep = self.main(self.target, '--deep')
        self.assertEqual((plain[0], deep[0]), (0, 0))
        self.assertEqual((len(plain[3]), len(deep[3])), (1, 1))
        self.assertEqual(stable(plain[3][0]), stable(deep[3][0]))
        self.assertIn('=== РЕЗУЛЬТАТ: OK ===', deep[1])

    def test_all_flag_prints_note_and_runs_only_bsl_ls(self):
        code, stdout, _, commands = self.main(self.target, '--all')
        self.assertEqual(code, 0)
        self.assertEqual(stdout.count('OneScript проверяется отдельно: tools/check_oscript.py'), 1)
        self.assertEqual(len(commands), 1)
        self.assertEqual(commands[0][0], str(self.java))

    def test_no_oscript_call(self):
        oscript_dir = self.root / 'oscript' / 'bin'
        oscript_dir.mkdir(parents=True)
        (oscript_dir / 'oscript.exe').write_bytes(b'')
        os.environ['PATH'] = str(oscript_dir) + os.pathsep + os.environ.get('PATH', '')
        for args in ([self.target], [self.target, '--all'], [self.target, '--deep'],
                     [self.target, '--all', '--standalone']):
            with self.subTest(args=args):
                code, _, _, commands = self.main(*args)
                self.assertEqual(code, 0)
                self.assertTrue(commands)
                for command in commands:
                    self.assertEqual(command[0], str(self.java))
                    self.assertFalse(any('oscript' in part.lower() for part in command), command)

    def test_json_output(self):
        code, stdout, stderr, _ = self.main(self.target, '--json', '--all',
                                            report=lambda call: self.targets_report(
                                                call, [{'severity': 'Warning', 'code': 'LineLength',
                                                        'message': 'длинная', 'range': {'start': {'line': 0}}}]))
        self.assertEqual(code, 1)
        data = json.loads(stdout)
        self.assertEqual((data['exit_code'], data['result'], data['version']), (1, 'warnings', checker.VERSION))
        module = data['modules'][0]
        self.assertEqual(module['path'], str(self.target))
        self.assertEqual(module['findings'][0]['line'], 1)
        self.assertEqual(module['findings'][0]['code'], 'LineLength')
        self.assertEqual(len(data['runs']), 1)
        self.assertIn('OneScript проверяется отдельно', stderr)

    def test_quiet_keeps_findings_and_failure_reasons(self):
        code, stdout, _, _ = self.main(self.target, '--quiet')
        self.assertEqual(code, 0)
        self.assertNotIn('Область:', stdout)
        self.assertIn('=== РЕЗУЛЬТАТ: OK ===', stdout)
        code, stdout, _, _ = self.main(self.target, '--quiet', report=lambda call: None, code=3, output=OOM)
        self.assertEqual(code, checker.INCOMPLETE)
        self.assertIn('нехватка памяти', stdout)
        self.assertIn('ПРОВЕРКА НЕ ВЫПОЛНЕНА', stdout)

    def test_argument_errors_are_incomplete(self):
        for args in ([self.target, '--unknown'], [self.target, '--standalone', '--source-dir', self.source], []):
            with self.subTest(args=args):
                self.assertEqual(self.main(*args)[0], checker.INCOMPLETE)

    def test_missing_target_file_is_incomplete(self):
        code, stdout, _, commands = self.main(self.root / 'absent.bsl', '--deep')
        self.assertEqual(code, checker.INCOMPLETE)
        self.assertEqual(commands, [])
        self.assertIn('файл не найден', stdout)


class LockTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.stack = contextlib.ExitStack()
        self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.stack.enter_context(patch.object(checker, 'PROJECT_ROOT', self.root))

    def tearDown(self):
        self.stack.close()
        self.temp.cleanup()

    def test_second_owner_times_out_then_lock_is_free(self):
        with checker.analysis_lock():
            with self.assertRaises(TimeoutError):
                with checker.analysis_lock(timeout=0.3, poll=0.05):
                    self.fail('Второй анализ получил занятый замок')
        with checker.analysis_lock(timeout=0):
            pass

    def test_waiting_owner_gets_lock_after_release(self):
        acquired = threading.Event()

        def holder():
            with checker.analysis_lock():
                acquired.set()
                time.sleep(0.5)
        thread = threading.Thread(target=holder)
        thread.start()
        acquired.wait(5)
        started = time.monotonic()
        with checker.analysis_lock(timeout=10, poll=0.05):
            waited = time.monotonic() - started
        thread.join()
        self.assertGreater(waited, 0.2)

    def test_lock_released_after_owner_crash(self):
        code = ('import sys, time\nfrom pathlib import Path\nsys.path.insert(0, sys.argv[1])\n'
                'import check_bsl as c\nc.PROJECT_ROOT = Path(sys.argv[2])\n'
                'with c.analysis_lock():\n    print("locked", flush=True)\n    time.sleep(120)\n')
        owner = subprocess.Popen([sys.executable, '-c', code, str(TOOLS), str(self.root)],
                                 stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(owner.stdout.readline().strip(), 'locked')
            with self.assertRaises(TimeoutError):
                with checker.analysis_lock(timeout=0.3, poll=0.05):
                    pass
        finally:
            owner.kill()
            owner.wait(10)
            owner.stdout.close()
        with checker.analysis_lock(timeout=10, poll=0.05):
            pass


@unittest.skipUnless(os.name == 'nt', 'фейковая java запускается через java.cmd - только Windows')
class FakeJavaTests(unittest.TestCase):
    """check_bsl.py отдельным процессом с фейковой java: весь путь командной строки до кода возврата."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        jdk = self.root / 'jdk'
        make_jdk(jdk)
        self.java = jdk / 'bin' / 'java.cmd'
        self.java.write_text(f'@"{sys.executable}" "{FAKE_JAVA}" %*\r\n@exit /b %ERRORLEVEL%\r\n', encoding='utf-8')
        self.home = self.root / 'home'
        self.home.mkdir()
        make_install(self.home, self.java)
        self.module = self.root / 'src' / 'Module.bsl'
        self.module.parent.mkdir()
        self.module.write_text('// модуль\n', encoding='utf-8')
        self.log = self.root / 'java.log'
        oscript = self.root / 'oscript'
        oscript.mkdir()
        self.marker = self.root / 'oscript-called'
        for name in ('oscript.cmd', 'oscript.bat'):
            (oscript / name).write_text(f'@echo called> "{self.marker}"\r\n', encoding='utf-8')
        self.env = {k: v for k, v in os.environ.items() if k not in ENV_KEYS}
        self.env.update(VANTEAM_BSL_HOME=str(self.home), PYTHONIOENCODING='utf-8', FAKE_JAVA_LOG=str(self.log),
                        PATH=str(oscript) + os.pathsep + os.environ.get('PATH', ''))

    def tearDown(self):
        self.temp.cleanup()

    def check(self, *args, **env):
        result = subprocess.run([sys.executable, str(TOOLS / 'check_bsl.py'), *map(str, args)], capture_output=True,
                                env=dict(self.env, **env), cwd=self.root, timeout=120)
        return result.returncode, result.stdout.decode('utf-8', 'replace')

    def jvm_calls(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text(encoding='utf-8').splitlines()]

    def test_out_of_memory_is_incomplete(self):
        code, output = self.check(self.module, '--deep', FAKE_JAVA_MODE='oom')
        self.assertEqual(code, checker.INCOMPLETE, output)
        self.assertIn('нехватка памяти JVM (-Xmx512m)', output)
        self.assertIn('Terminating due to java.lang.OutOfMemoryError', output)
        self.assertIn('ПРОВЕРКА НЕ ВЫПОЛНЕНА', output)

    def test_platform_context_disabled_is_incomplete(self):
        code, output = self.check(self.module, '--deep', FAKE_JAVA_MODE='platform_disabled')
        self.assertEqual(code, checker.INCOMPLETE, output)
        self.assertIn('platform context is disabled', output)

    def test_errors_and_warnings_codes(self):
        for severity, expected in (('Error', 2), ('Warning', 1), ('Information', 0)):
            with self.subTest(severity=severity):
                diagnostics = json.dumps([{'severity': severity, 'code': 'Rule', 'message': 'текст',
                                           'range': {'start': {'line': 0}}}])
                code, output = self.check(self.module, '--deep', FAKE_JAVA_DIAGNOSTICS=diagnostics)
                self.assertEqual(code, expected, output)
                self.assertIn('Rule: текст', output)

    def test_several_modules_share_one_jvm_and_no_oscript(self):
        second = self.module.parent / 'Second.bsl'
        second.write_text('// второй\n', encoding='utf-8')
        code, output = self.check(self.module, second, '--all')
        self.assertEqual(code, 0, output)
        calls = self.jvm_calls()
        self.assertEqual(len(calls), 1)
        targets = [calls[0][i + 1] for i, arg in enumerate(calls[0]) if arg == '--target']
        self.assertEqual([Path(t) for t in targets], [self.module, second])
        self.assertIn('--silent', calls[0])
        self.assertIn('OneScript проверяется отдельно: tools/check_oscript.py', output)
        self.assertFalse(self.marker.exists(), 'вызван oscript')


def git(repo, *args):
    subprocess.run(['git', *args], cwd=repo, check=True, capture_output=True)


def init_repo(repo):
    repo.mkdir(parents=True, exist_ok=True)
    git(repo, 'init', '-q')
    for key, value in [('core.autocrlf', 'true'), ('user.name', 'Тест'),
                       ('user.email', 'test@example.invalid'), ('commit.gpgsign', 'false')]:
        git(repo, 'config', key, value)


class ComparisonTests(unittest.TestCase):
    """bsl_new_findings: база из всех файлов области в ревизии, ERROR - результат, не сбой."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp.name).resolve() / 'repo'
        self.module_dir = self.repo / 'mod'
        self.module_dir.mkdir(parents=True)
        init_repo(self.repo)
        self.target = self.module_dir / 'Module.bsl'
        self.sibling = self.module_dir / 'Second.bsl'
        self.target.write_bytes(CLEAN_MODULE.encode('utf-8'))
        self.sibling.write_bytes('// сосед\r\n'.encode('utf-8'))
        git(self.repo, 'add', '.')
        git(self.repo, 'commit', '-q', '-m', 'база')
        self.stack = contextlib.ExitStack()
        self.output = self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.stack.enter_context(patch.object(comparison, 'ВРЕМЕННЫЕ', Path(self.temp.name) / 'tmp'))

    def tearDown(self):
        self.stack.close()
        self.temp.cleanup()

    def main(self, check, *args):
        with patch.object(comparison, 'проверить', side_effect=check) as mocked:
            return comparison.main([str(self.target), *args]), mocked

    def test_unchanged_directory_checks_once_despite_crlf_worktree(self):
        self.assertNotEqual(self.target.read_bytes(), subprocess.run(
            ['git', 'show', 'HEAD:mod/Module.bsl'], cwd=self.repo, capture_output=True).stdout)
        code, mocked = self.main(lambda path, *scope: [])
        self.assertEqual(code, 0)
        self.assertEqual(mocked.call_count, 1)

    def test_changed_sibling_requires_base_run_with_revision_context(self):
        base_sibling = self.sibling.read_bytes()
        self.sibling.write_bytes('// сосед изменён\r\n'.encode('utf-8'))
        seen = []

        def check(path, *scope):
            path = Path(path)
            seen.append(path)
            if len(seen) == 2:
                seen.append({p.name: p.read_bytes() for p in path.parent.iterdir()})
            return []
        code, mocked = self.main(check)
        self.assertEqual((code, mocked.call_count), (0, 2))
        base_files = seen[2]
        self.assertEqual(base_files, {'Module.bsl': self.target.read_bytes(), 'Second.bsl': base_sibling})
        self.assertFalse(seen[1].parent.exists())

    def test_new_file_has_empty_base(self):
        new = self.module_dir / 'New.bsl'
        new.write_text('// новый\n', encoding='utf-8')
        self.target = new
        code, mocked = self.main(lambda path, *scope: [('WARN', 'Rule', 'сообщение', 'текст', 1)])
        self.assertEqual((code, mocked.call_count), (1, 1))
        self.assertIn('НОВОЕ WARN', self.output.getvalue())

    def test_invalid_revision_fails_before_check(self):
        code, mocked = self.main(lambda path, *scope: [], 'нет-такой-ревизии')
        self.assertEqual(code, 2)
        mocked.assert_not_called()

    def test_base_failure_fails_and_removes_temporary_base(self):
        self.target.write_bytes((CLEAN_MODULE + BAD_PROCEDURE).encode('utf-8'))
        seen = []

        def check(path, *scope):
            seen.append(Path(path))
            if len(seen) == 2:
                raise comparison.СбойПроверки('сбой базы')
            return []
        self.assertEqual(self.main(check)[0], 2)
        self.assertEqual(len(seen), 2)
        self.assertFalse(seen[1].parent.exists())

    def test_only_new_findings_are_reported(self):
        self.target.write_bytes((CLEAN_MODULE + BAD_PROCEDURE).encode('utf-8'))
        old = ('WARN', 'Старое', 'было', 'текст', 3)
        calls = []

        def check(path, *scope):
            calls.append(path)
            if len(calls) == 2:
                return [old]
            # Старая находка сместилась на другую строку: ключ без номера строки совпадает.
            return [('WARN', 'Старое', 'было', 'текст', 9), ('ERROR', 'Новое', 'стало', 'Возврат 1;', 10)]
        code, _ = self.main(check)
        self.assertEqual(code, 1)
        text = self.output.getvalue()
        self.assertIn('НОВОЕ ERROR L10', text)
        self.assertNotIn('Старое', text)
        self.assertNotIn('oscript', text)

    def test_source_dir_base_contains_all_files_of_scope(self):
        dump = self.repo / 'dump'
        module = dump / 'CommonModules' / 'А' / 'Ext' / 'Module.bsl'
        module.parent.mkdir(parents=True)
        module.write_bytes(CLEAN_MODULE.encode('utf-8'))
        (dump / 'Configuration.xml').write_text('<MetaDataObject/>\n', encoding='utf-8')
        (dump / 'CommonModules' / 'А.xml').write_text('<CommonModule/>\n', encoding='utf-8')
        git(self.repo, 'add', '.')
        git(self.repo, 'commit', '-q', '-m', 'выгрузка')
        (dump / 'Configuration.xml').write_text('<MetaDataObject изменён="да"/>\n', encoding='utf-8')
        self.target = module
        seen = []

        def check(path, source_dir=None, standalone=False):
            seen.append((Path(path), source_dir, standalone))
            if len(seen) == 2:
                base = Path(source_dir)
                seen.append(sorted(p.relative_to(base).as_posix() for p in base.rglob('*') if p.is_file()))
                seen.append((base / 'Configuration.xml').read_text(encoding='utf-8'))
            return []
        code, mocked = self.main(check, '--source-dir', str(dump))
        self.assertEqual((code, mocked.call_count), (0, 2))
        self.assertEqual(seen[0][1], str(dump))
        self.assertEqual(seen[2], ['CommonModules/А.xml', 'CommonModules/А/Ext/Module.bsl', 'Configuration.xml'])
        self.assertIn('<MetaDataObject/>', seen[3])
        self.assertEqual(seen[1][0], Path(seen[1][1]) / 'CommonModules' / 'А' / 'Ext' / 'Module.bsl')

    def test_only_git_processes_are_started(self):
        self.sibling.write_bytes('// сосед изменён\r\n'.encode('utf-8'))
        started = []
        real_run = subprocess.run

        def run(cmd, *args, **kwargs):
            started.append(list(cmd))
            return real_run(cmd, *args, **kwargs)
        with patch.object(comparison.subprocess, 'run', side_effect=run):
            code, _ = self.main(lambda path, *scope: [])
        self.assertEqual(code, 0)
        self.assertTrue(started)
        self.assertTrue(all(cmd[0] == 'git' for cmd in started), started)

    def test_check_result_is_parsed_and_incomplete_check_fails(self):
        self.target.write_bytes((CLEAN_MODULE + BAD_PROCEDURE).encode('utf-8'))
        module = {'path': str(self.target), 'exit_code': 2, 'incomplete': None, 'findings': [
            {'severity': 'Error', 'code': 'ProcedureReturnsValue', 'message': 'Процедура содержит "Возврат"',
             'line': 10},
            {'severity': 'Hint', 'code': 'Tag', 'message': 'тег', 'line': 1}]}
        with patch.object(comparison.check_bsl, 'analyze', return_value={'exit_code': 2, 'modules': [module]}):
            findings = comparison.проверить(self.target)
        self.assertEqual(findings, [('ERROR', 'ProcedureReturnsValue', 'Процедура содержит "Возврат"',
                                     'Возврат 1;', 10)])
        broken = dict(module, incomplete='out_of_memory', error='нехватка памяти', exit_code=3)
        with patch.object(comparison.check_bsl, 'analyze', return_value={'exit_code': 3, 'modules': [broken]}):
            with self.assertRaises(comparison.СбойПроверки):
                comparison.проверить(self.target)


class SetupTests(unittest.TestCase):
    """setup_bsl_server.py без JVM: скачивание, идемпотентность, справка, CDS, --check без изменений."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.home = self.root / 'home'
        self.home.mkdir()
        self.stack = contextlib.ExitStack()
        self.output = self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))

    def tearDown(self):
        self.stack.close()
        self.temp.cleanup()

    def release(self, payload=b'jar bytes'):
        jar = self.root / 'release.jar'
        jar.write_bytes(payload)
        sha = setup.sha256(jar)
        for name, value in [('RELEASE_URL', jar.as_uri()), ('RELEASE_SHA256', sha), ('RELEASE_SIZE', len(payload)),
                            ('RELEASE_NAME', 'bsl-language-server-1.0.7-vanteam.1-exec.jar')]:
            self.stack.enter_context(patch.object(setup, name, value))
        return sha

    def test_default_config_is_the_1_1_0_config(self):
        self.assertEqual(setup.sha256(checker.BUNDLED_CONFIG), setup.CONFIG_SHA256)
        self.assertTrue(setup.CONFIG_SHA256.startswith('aa0942c8'))

    def test_download_is_verified_and_done_once(self):
        sha = self.release()
        record, changed = setup.ensure_jar(self.home, None, None)
        self.assertTrue(changed)
        self.assertEqual(record['sha256'], sha)
        self.assertTrue(record['release'])
        jar = self.home / record['jar']
        self.assertEqual(jar.read_bytes(), b'jar bytes')
        with patch.object(setup, 'download', side_effect=AssertionError('повторное скачивание')):
            again, changed = setup.ensure_jar(self.home, {'engine': record}, None)
        self.assertEqual((again, changed), (record, False))

    def test_download_with_wrong_sha_is_rejected(self):
        self.release()
        with patch.object(setup, 'RELEASE_SHA256', '0' * 64):
            with self.assertRaises(setup.SetupError):
                setup.ensure_jar(self.home, None, None)
        self.assertEqual([p for p in self.home.rglob('*') if p.is_file()], [])

    def test_local_jar_other_than_release_is_marked(self):
        self.release()
        jar = self.root / 'bsl-language-server-1.0.8-custom-exec.jar'
        jar.write_bytes(b'custom')
        record, _ = setup.ensure_jar(self.home, None, str(jar))
        self.assertFalse(record['release'])
        self.assertEqual(record['version'], '1.0.8-custom')
        self.assertIn('не совпадает с релизом', self.output.getvalue())

    def test_help_copies_three_files_from_freshest_platform(self):
        bins = []
        for version in ('8.3.24.1500', '8.3.27.1719'):
            bin_dir = self.root / '1cv8' / version / 'bin'
            bin_dir.mkdir(parents=True)
            for name in (*setup.HELP_FILES, 'shcntx_root.hbk', 'shcntx_en.hbk'):
                (bin_dir / name).write_bytes(f'{version}/{name}'.encode())
            bins.append((version, bin_dir))
        with patch.object(setup, 'platform_bins', return_value=list(reversed(bins))):
            record, changed = setup.ensure_help(self.home, None, None, False)
            self.assertTrue(changed)
            help_dir = self.home / record['dir']
            self.assertEqual(record['platform_version'], '8.3.27.1719')
            self.assertEqual(sorted(p.name for p in help_dir.iterdir()), sorted(setup.HELP_FILES))
            for name in setup.HELP_FILES:
                self.assertEqual((help_dir / name).stat().st_mtime_ns, (bins[1][1] / name).stat().st_mtime_ns)
            again, changed = setup.ensure_help(self.home, {'help': record}, None, False)
        self.assertEqual((again, changed), (record, False))
        explicit, _ = setup.ensure_help(self.home, {'help': record}, str(bins[0][1]), False)
        self.assertEqual(explicit['platform_version'], '8.3.24.1500')
        self.assertEqual(setup.ensure_help(self.home, None, None, True), (None, False))

    def test_cds_retraining_reasons(self):
        java = {'fingerprint': {'runtime': '21.0.12'}}
        engine = {'sha256': 'a' * 64}
        (self.home / 'cds').mkdir()
        (self.home / 'cds' / 'x.jsa').write_bytes(b'')
        install = {'cds': {'status': 'ok', 'archive': 'cds/x.jsa', 'jar_sha256': 'a' * 64,
                           'java': {'runtime': '21.0.12'}}}
        self.assertIsNone(setup.cds_needs_training(self.home, install, engine, java, False, False))
        self.assertEqual(setup.cds_needs_training(self.home, None, engine, java, False, False), 'архива ещё нет')
        self.assertEqual(setup.cds_needs_training(self.home, install, {'sha256': 'b' * 64}, java, False, False),
                         'сменился JAR')
        self.assertEqual(setup.cds_needs_training(self.home, install, engine, {'fingerprint': {'runtime': '21.0.13'}},
                                                  False, False), 'сменилась сборка JDK')
        self.assertEqual(setup.cds_needs_training(self.home, install, engine, java, True, False),
                         'указан --retrain-cds')
        self.assertEqual(setup.cds_needs_training(self.home, install, engine, java, False, True),
                         'JAR распакован заново')
        failed = {'cds': dict(install['cds'], status='failed')}
        self.assertEqual(setup.cds_needs_training(self.home, failed, engine, java, False, False),
                         'прошлое обучение не удалось')
        (self.home / 'cds' / 'x.jsa').unlink()
        self.assertEqual(setup.cds_needs_training(self.home, install, engine, java, False, False),
                         'нет файла архива')

    def train(self, share, same=True):
        java_path = make_jdk(self.root / 'jdk')
        java = {'path': str(java_path), 'fingerprint': checker.jvm_fingerprint(java_path)}
        (self.home / 'tmp').mkdir(exist_ok=True)

        def run_fixture(engine, work, extra_jvm=()):
            for option in extra_jvm:
                if option.startswith('-XX:ArchiveClassesAtExit='):
                    Path(option.split('=', 1)[1]).write_bytes(b'archive')
            return {}, '', 1.0
        with patch.object(setup, 'run_fixture', side_effect=run_fixture), \
                patch.object(setup, 'verify_archive', return_value=(
                    {'classes': 100, 'from_archive': int(share * 100), 'share': share}, same)):
            return setup.train_cds(self.home, java, {'sha256': 'a' * 64}, {'jar': 'engine/x.jar'}, None,
                                   self.home / 'cache', self.home / 'tmp')

    def test_cds_below_threshold_is_disabled(self):
        record = self.train(0.5)
        self.assertEqual(record['status'], 'disabled')
        self.assertIn('меньше 80%', record['reason'])
        self.assertEqual(list((self.home / 'cds').glob('*.jsa')), [])
        self.assertIn('работа без CDS', self.output.getvalue())

    def test_cds_with_different_findings_is_disabled(self):
        self.assertEqual(self.train(0.9, same=False)['status'], 'disabled')

    def test_cds_above_threshold_is_kept(self):
        record = self.train(0.9)
        self.assertEqual(record['status'], 'ok')
        self.assertTrue((self.home / record['archive']).is_file())

    def snapshot(self):
        return sorted((p.relative_to(self.home).as_posix(), p.stat().st_size, p.stat().st_mtime_ns)
                      for p in self.home.rglob('*'))

    def test_check_mode_changes_nothing(self):
        java = make_jdk(self.root / 'jdk')
        install = make_install(self.home, java)
        install['engine']['sha256'] = setup.sha256(self.home / install['engine']['jar'])
        for name in setup.HELP_FILES:
            install['help']['files'][name] = {'sha256': setup.sha256(self.home / install['help']['dir'] / name)}
        write_install(self.home, install)
        before = self.snapshot()
        with patch.dict(os.environ, {'VANTEAM_BSL_HOME': str(self.home)}), \
                patch.object(setup, 'platform_bins', return_value=[]):
            code = setup.main(['--check', '--no-run', '--home', str(self.home)])
        self.assertEqual(code, 0, self.output.getvalue())
        self.assertEqual(self.snapshot(), before)
        self.assertIn('Итог: установка исправна', self.output.getvalue())
        (self.home / install['engine']['jar']).write_bytes(b'broken')
        self.assertEqual(setup.main(['--check', '--no-run', '--home', str(self.home)]), 1)

    def test_check_mode_without_install_fails(self):
        self.assertEqual(setup.main(['--check', '--no-run', '--home', str(self.root / 'empty')]), 1)
        self.assertFalse((self.root / 'empty').exists())


if __name__ == '__main__':
    unittest.main()
