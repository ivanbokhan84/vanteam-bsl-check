"""Регресс обвязки BSL LS: сбой не становится успехом, найденная ERROR не становится сбоем.

Запуск из корня релиза: python -m unittest tools/tests/test_check_bsl.py -v
Интеграционные тесты запускают настоящие JAR BSL LS 1.0.7, oscript и git (десятки минут);
пропуск: переменная окружения BSL_TESTS_SKIP_INTEGRATION=1 (такой прогон не является приёмкой).
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

FIXTURES = TOOLS / 'tests' / 'fixtures' / 'bsl_check'
FIND_JAR = checker.find_bsl_ls_jar  # до подмен в setUp
CLEAN_MODULE = ('// Модуль для теста.\r\n//\r\n// Возвращаемое значение:\r\n//  Число - 1.\r\n//\r\n'
                'Функция Один() Экспорт\r\n\tВозврат 1;\r\nКонецФункции\r\n')
BAD_PROCEDURE = 'Процедура Плохая() Экспорт\r\n\tВозврат 1;\r\nКонецПроцедуры\r\n'
UNKNOWN_SYMBOL = ('\r\n// Вызов модуля вне контекста.\r\n//\r\n// Возвращаемое значение:\r\n//  Число - результат.\r\n'
                  '//\r\nФункция Два() Экспорт\r\n\tВозврат Ext_НеизвестныйМодуль.Значение();\r\nКонецФункции\r\n')
# Строки лога настоящего BSL LS 1.0.7 при коде выхода 0 (зонды 28.09.2026).
LOG_WARN = ("2026-09-28T10:12:56.826+03:00  WARN 31768 --- [BSL Language Server] [configuration-0] "
            "c.g._.b.r.common.xstream.ExtendXStream   : Can't read file 'Configuration.xml'")
LOG_ERROR = ("2026-09-28T10:13:29.376+03:00 ERROR 16508 --- [BSL Language Server] [           main] "
             "c.g._.b.l.c.d.ParametersDeserializer     : Can't deserialize parameter configuration")
LOG_INFO = ("2026-09-28T10:09:46.750+03:00  INFO 35664 --- [BSL Language Server] [           main] "
            "c.g._.b.l.reporters.JsonReporter         : JSON report saved to report\\bsl-json.json")
LOG_PLATFORM = ("2026-09-28T09:59:59.969+03:00  INFO 31572 --- [BSL Language Server] [-types-warmup-1] "
                "_.b.l.t.r.PlatformContextProviderFactory : Loaded 2495 platform contexts from 1C syntax helper")


def quiet():
    return contextlib.redirect_stdout(io.StringIO())


class CheckerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / 'source'
        self.source.mkdir()
        self.target = self.source / 'Module.bsl'
        self.target.write_text('// модуль\n', encoding='utf-8')
        self.stack = contextlib.ExitStack()
        self.stack.enter_context(quiet())
        for name, value in [('PROJECT_ROOT', self.root), ('BSL_LS_DIR', self.root),
                            ('BSL_LS_CONFIG', self.root / 'config.json')]:
            self.stack.enter_context(patch.object(checker, name, value))
        self.stack.enter_context(patch.object(checker, 'find_java', return_value='java'))
        self.stack.enter_context(patch.object(checker, 'find_bsl_ls_jar', return_value='test.jar'))
        self.stack.enter_context(patch.object(checker, 'java_version_major', return_value=21))
        self.calls = []

    def tearDown(self):
        self.stack.close()
        self.temp.cleanup()

    def run_report(self, report, code=0, output='', reports=1, quiet=True, **scope):
        """Подменяет JVM: пишет отчёт (dict или функция от каталога анализа) в --outputDir."""
        def run(cmd, **kwargs):
            src_dir = Path(cmd[cmd.index('--srcDir') + 1])
            self.calls.append((cmd, kwargs, sorted(p.relative_to(src_dir).as_posix()
                                                   for p in src_dir.rglob('*')
                                                   if p.suffix in checker.SOURCE_SUFFIXES)))
            data = report(src_dir) if callable(report) else report
            if data is not None:
                for number in range(reports):
                    dest = Path(cmd[cmd.index('--outputDir') + 1]) / f'bsl-json{number}.json'
                    dest.write_text(json.dumps(data), encoding='utf-8')
            return subprocess.CompletedProcess(cmd, code, output, '')
        with patch.object(checker.subprocess, 'run', side_effect=run):
            return checker.run_bsl_ls_check(self.target, quiet=quiet, **scope)

    def report(self, diagnostics=None, path=None, mdo_ref=None):
        info = {'path': str((path or self.target).relative_to(self.root)), 'diagnostics': diagnostics or []}
        if mdo_ref is not None:
            info['mdoRef'] = mdo_ref
        return {'fileinfos': [info]}

    def test_empty_diagnostics_are_success(self):
        self.assertEqual(self.run_report(self.report()), (0, {}))

    def test_real_report_file_uri_is_supported(self):
        report = self.report()
        report['fileinfos'][0]['path'] = self.target.as_uri()
        self.assertEqual(self.run_report(report), (0, {}))

    def test_missing_report_is_incomplete(self):
        self.assertEqual(self.run_report(None)[0], checker.INCOMPLETE)

    def test_ambiguous_reports_are_incomplete(self):
        self.assertEqual(self.run_report(self.report(), reports=2)[0], checker.INCOMPLETE)

    def test_nonzero_exit_is_incomplete_even_with_clean_report(self):
        self.assertEqual(self.run_report(self.report(), code=1)[0], checker.INCOMPLETE)

    def test_engine_failure_messages_are_incomplete(self):
        for marker in checker.ENGINE_FAILURE_MARKERS:
            with self.subTest(marker=marker):
                self.assertEqual(self.run_report(self.report(), output=f'x {marker} y')[0],
                                 checker.INCOMPLETE)

    def test_word_error_alone_is_not_engine_failure(self):
        self.assertEqual(self.run_report(self.report(), output='ERROR counter: 0'), (0, {}))

    def test_other_module_with_same_basename_does_not_match(self):
        report = {'fileinfos': [{'path': 'other/Module.bsl', 'diagnostics': []}]}
        self.assertEqual(self.run_report(report)[0], checker.INCOMPLETE)

    def test_other_module_diagnostics_are_excluded(self):
        report = self.report()
        report['fileinfos'].append({'path': 'other/Module.bsl',
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
        for severity, code in [('Error', 2), ('Warning', 1), ('Information', 0)]:
            with self.subTest(severity=severity):
                self.assertEqual(self.run_report(self.report([{'severity': severity}])),
                                 (code, {severity: 1}))
        self.assertNotEqual(2, checker.INCOMPLETE)

    def test_unknown_or_missing_severity_is_incomplete(self):
        for diagnostic in ({}, {'severity': 1}, {'severity': 'NewSeverity'}, None):
            with self.subTest(diagnostic=diagnostic):
                self.assertEqual(self.run_report(self.report([diagnostic]))[0], checker.INCOMPLETE)

    def test_legacy_severity_names_are_incomplete(self):
        # BSL LS 1.0.7 пишет только severity lsp4j; имена формата 0.24 - чужой формат отчёта.
        for severity in ('Critical', 'Major', 'Minor', 'Info'):
            with self.subTest(severity=severity):
                self.assertEqual(self.run_report(self.report([{'severity': severity}]))[0],
                                 checker.INCOMPLETE)

    def test_engine_log_warn_or_error_is_incomplete(self):
        for line in (LOG_WARN, LOG_ERROR, 'prefix\r\n' + LOG_ERROR + '\r\n'):
            with self.subTest(line=line):
                self.assertEqual(self.run_report(self.report(), output=line)[0], checker.INCOMPLETE)
        self.assertEqual(self.run_report(self.report(), output=LOG_INFO + '\n' + LOG_PLATFORM), (0, {}))
        self.assertEqual(checker.engine_failure(LOG_ERROR), LOG_ERROR)

    def test_old_java_is_incomplete(self):
        with patch.object(checker, 'java_version_major', return_value=17):
            self.assertEqual(self.run_report(self.report())[0], checker.INCOMPLETE)
        self.assertEqual(self.calls, [])

    def test_designer_source_dir_requires_loaded_metadata(self):
        uri = self.target.as_uri()
        self.assertEqual(self.run_report(self.report(mdo_ref=uri), source_dir=str(self.source)), (0, {}))
        (self.source / 'Configuration.xml').write_text('<MetaDataObject/>', encoding='utf-8')
        for mdo_ref in (uri, '', None):
            with self.subTest(mdo_ref=mdo_ref):
                self.assertEqual(self.run_report(self.report(mdo_ref=mdo_ref), source_dir=str(self.source))[0],
                                 checker.INCOMPLETE)
        self.assertEqual(self.run_report(self.report(mdo_ref='CommonModule.Потребитель'),
                                         source_dir=str(self.source)), (0, {}))
        # Без --source-dir metadata не ожидаются: область по умолчанию - только каталог цели.
        self.assertEqual(self.run_report(self.report(mdo_ref=uri)), (0, {}))

    def test_edt_source_dir_requires_loaded_metadata(self):
        (self.source / 'Configuration').mkdir()
        (self.source / 'Configuration' / 'Configuration.mdo').write_text('<mdclass/>', encoding='utf-8')
        self.assertEqual(self.run_report(self.report(mdo_ref=self.target.as_uri()),
                                         source_dir=str(self.source))[0], checker.INCOMPLETE)

    def test_platform_context_source_is_printed(self):
        for output, expected in ((LOG_PLATFORM, 'контекстов: 2495'), ('', 'встроенные описания BSL LS')):
            with self.subTest(expected=expected):
                printed = io.StringIO()
                with contextlib.redirect_stdout(printed):
                    self.assertEqual(self.run_report(self.report(), output=output, quiet=False), (0, {}))
                self.assertIn('Контекст платформы: ' + checker.describe_platform(output), printed.getvalue())
                self.assertIn(expected, printed.getvalue())

    def test_jvm_runs_from_project_root(self):
        self.run_report(self.report())
        cmd, kwargs, _ = self.calls[0]
        self.assertEqual(kwargs['cwd'], self.root)
        self.assertNotIn('--workspaceDir', cmd)
        self.assertIn('-XX:TieredStopAtLevel=1', cmd)
        self.assertTrue(any(arg.startswith('-XX:ActiveProcessorCount=') for arg in cmd))
        self.assertLess(cmd.index('-XX:TieredStopAtLevel=1'), cmd.index('-jar'))

    def test_fork_jar_is_preferred_over_same_upstream_version(self):
        for name in ('bsl-language-server-0.29.0-exec.jar', 'bsl-language-server-1.0.7-exec.jar',
                     'bsl-language-server-1.0.7-vanteam.1-exec.jar'):
            (self.root / name).write_bytes(b'')
        with patch.dict(os.environ, {'BSL_LS_JAR': ''}):
            self.assertEqual(Path(FIND_JAR()).name, 'bsl-language-server-1.0.7-vanteam.1-exec.jar')
        (self.root / 'bsl-language-server-1.0.8-exec.jar').write_bytes(b'')
        with patch.dict(os.environ, {'BSL_LS_JAR': ''}):
            self.assertEqual(Path(FIND_JAR()).name, 'bsl-language-server-1.0.8-exec.jar')

    def test_fork_command_targets_module_and_uses_platform_cache(self):
        fork = 'bsl-language-server-1.0.7-vanteam.1-exec.jar'
        with patch.object(checker, 'find_bsl_ls_jar', return_value=fork), \
                patch.dict(os.environ, {'BSL_LS_CACHE': ''}):
            self.assertEqual(self.run_report(self.report()), (0, {}))
        cmd = self.calls[0][0]
        self.assertEqual(Path(cmd[cmd.index('--target') + 1]), self.target)
        self.assertIn(f'-Dapp.platform-context.cache.path={self.root / "_cache"}', cmd)
        self.assertTrue((self.root / '_cache').is_dir())
        for arg in ('--silent', '-XX:+ExitOnOutOfMemoryError', '-Xmx512m'):
            self.assertIn(arg, cmd)
        self.assertLess(cmd.index('-Dapp.platform-context.cache.path=' + str(self.root / '_cache')),
                        cmd.index('-jar'))

    def test_upstream_command_has_no_target_or_cache(self):
        self.run_report(self.report())
        cmd = self.calls[0][0]
        self.assertNotIn('--target', cmd)
        self.assertFalse(any(arg.startswith('-Dapp.platform-context') for arg in cmd))
        self.assertIn('--silent', cmd)

    def test_heap_follows_scope_size_and_override(self):
        (self.source / 'Second.bsl').write_text('// сосед\n', encoding='utf-8')
        with patch.object(checker, 'LARGE_SCOPE_FILES', 2), patch.dict(os.environ, {'BSL_LS_XMX': ''}):
            self.run_report(self.report())
        self.assertIn('-Xmx1g', self.calls[-1][0])
        with patch.dict(os.environ, {'BSL_LS_XMX': '2g'}):
            self.run_report(self.report())
        self.assertIn('-Xmx2g', self.calls[-1][0])
        self.assertNotIn('-Xmx512m', self.calls[-1][0])

    def test_out_of_memory_exit_is_incomplete(self):
        result = self.run_report(self.report(), code=3,
                                 output='Terminating due to java.lang.OutOfMemoryError: Java heap space')
        self.assertEqual(result[0], checker.INCOMPLETE)

    def test_cds_archive_is_used_only_with_matching_stamp(self):
        base = self.root / 'cds' / 'test'
        base.mkdir(parents=True)
        (base / 'bslls.jsa').write_bytes(b'')
        (base / 'test.jar').write_bytes(b'')
        stamp = {'jar': 'test.jar', 'jar_size': 1}
        (base / 'stamp.json').write_text(json.dumps(stamp), encoding='utf-8')
        with patch.object(checker, 'cds_stamp', return_value=stamp):
            self.assertEqual(self.run_report(self.report()), (0, {}))
        cmd = self.calls[-1][0]
        self.assertIn(f'-XX:SharedArchiveFile={base / "bslls.jsa"}', cmd)
        self.assertEqual(Path(cmd[cmd.index('-jar') + 1]), base / 'test.jar')
        with patch.object(checker, 'cds_stamp', return_value={**stamp, 'jar_size': 2}):
            self.run_report(self.report())
        cmd = self.calls[-1][0]
        self.assertFalse(any(arg.startswith('-XX:SharedArchiveFile') for arg in cmd))
        self.assertEqual(cmd[cmd.index('-jar') + 1], 'test.jar')

    def test_platform_cache_status_is_reported(self):
        hit = LOG_PLATFORM.replace('Loaded 2495 platform contexts from 1C syntax helper',
                                   'Platform context cache hit: D:/cache/platform-context-1.bin (12 ms)')
        self.assertIn('кэш справки: hit', checker.describe_platform(LOG_PLATFORM + '\n' + hit))

    def test_busy_lock_is_incomplete(self):
        @contextlib.contextmanager
        def busy(*args, **kwargs):
            raise TimeoutError('занят')
            yield
        with patch.object(checker, 'analysis_lock', busy):
            self.assertEqual(self.run_report(self.report())[0], checker.INCOMPLETE)
        self.assertEqual(self.calls, [])

    def test_default_scope_is_directory_in_place(self):
        (self.source / 'Second.bsl').write_text('// сосед\n', encoding='utf-8')
        self.assertEqual(self.run_report(self.report()), (0, {}))
        cmd, _, files = self.calls[0]
        self.assertEqual(Path(cmd[cmd.index('--srcDir') + 1]), self.source)
        self.assertEqual(files, ['Module.bsl', 'Second.bsl'])

    def test_nested_directories_are_not_analyzed(self):
        (self.source / 'Second.bsl').write_text('// сосед\n', encoding='utf-8')
        (self.source / 'Описание.txt').write_text('не модуль', encoding='utf-8')
        archive = self.source / '_archive'
        archive.mkdir()
        (archive / 'Old.bsl').write_text('// архив\n', encoding='utf-8')
        result = self.run_report(lambda src: self.report(path=src / 'Module.bsl'))
        self.assertEqual(result, (0, {}))
        cmd, _, files = self.calls[0]
        self.assertNotEqual(Path(cmd[cmd.index('--srcDir') + 1]), self.source)
        self.assertEqual(files, ['Module.bsl', 'Second.bsl'])

    def test_staged_scope_ignores_report_for_original_path(self):
        (self.source / '_archive').mkdir()
        (self.source / '_archive' / 'Old.bsl').write_text('// архив\n', encoding='utf-8')
        self.assertEqual(self.run_report(self.report())[0], checker.INCOMPLETE)

    def test_explicit_scope_is_recursive_and_must_contain_target(self):
        nested = self.source / 'nested'
        nested.mkdir()
        (nested / 'Other.bsl').write_text('// другой\n', encoding='utf-8')
        self.assertEqual(self.run_report(self.report(), source_dir=str(self.root)), (0, {}))
        self.assertEqual(self.calls[0][2], ['source/Module.bsl', 'source/nested/Other.bsl'])
        self.assertEqual(self.run_report(self.report(), source_dir=str(nested))[0],
                         checker.INCOMPLETE)

    def test_explicit_scope_sets_metadata_root_without_changing_source_config(self):
        original = {'configurationRoot': '.', 'diagnostics': {'parameters': {'Typo': False}}}
        checker.BSL_LS_CONFIG.write_text(json.dumps(original), encoding='utf-8')
        seen = {}
        def report(src):
            cmd = self.calls[-1][0]
            seen.update(json.loads(Path(cmd[cmd.index('--configuration') + 1]).read_text(encoding='utf-8')))
            return self.report()
        self.assertEqual(self.run_report(report, source_dir=str(self.source))[0], 0)
        self.assertEqual(seen['configurationRoot'], str(self.source))
        self.assertEqual(seen['diagnostics'], original['diagnostics'])
        self.assertEqual(json.loads(checker.BSL_LS_CONFIG.read_text(encoding='utf-8')), original)

    def test_standalone_analyzes_only_target(self):
        (self.source / 'Second.bsl').write_text('// сосед\n', encoding='utf-8')
        result = self.run_report(lambda src: self.report(path=src / 'Module.bsl'), standalone=True)
        self.assertEqual(result, (0, {}))
        self.assertEqual(self.calls[0][2], ['Module.bsl'])

    def oscript(self, code, stdout):
        with patch.object(checker, 'find_oscript', return_value='oscript'), \
                patch.object(checker.subprocess, 'run', return_value=subprocess.CompletedProcess(
                    [], code, stdout.encode('utf-8'), b'')) as run:
            result = checker.run_oscript_check(self.target, quiet=True)[0]
        self.assertIn('-encoding=utf-8', run.call_args.args[0])
        return result

    def test_oscript_results(self):
        location = '{Модуль X / Ошибка в строке: 4 / Неизвестный символ: Ext_X}'
        self.assertEqual(self.oscript(0, 'No errors.'), 0)
        self.assertEqual(self.oscript(1, location), 2)
        self.assertEqual(self.oscript(1, 'No errors.'), checker.INCOMPLETE)
        self.assertEqual(self.oscript(0, 'Непонятный вывод'), checker.INCOMPLETE)
        self.assertEqual(self.oscript(255, ''), checker.INCOMPLETE)

    def test_missing_oscript_is_incomplete(self):
        with patch.object(checker, 'find_oscript', return_value=None):
            self.assertEqual(checker.run_oscript_check(self.target)[0], checker.INCOMPLETE)

    def test_missing_override_jar_is_not_replaced_by_default(self):
        jar = self.root / 'bsl-language-server-9.9.9-exec.jar'
        jar.write_bytes(b'')
        with patch.dict(os.environ, {'BSL_LS_JAR': str(self.root / 'absent.jar')}):
            self.assertIsNone(FIND_JAR())
        with patch.dict(os.environ, {'BSL_LS_JAR': str(jar)}):
            self.assertEqual(FIND_JAR(), str(jar))
        with patch.dict(os.environ, {'BSL_LS_JAR': ''}):
            self.assertEqual(Path(FIND_JAR()), jar)

    def test_missing_target_file_is_incomplete(self):
        with patch.object(sys, 'argv', ['check', str(self.root / 'absent.bsl'), '--deep']):
            self.assertEqual(checker.main(), checker.INCOMPLETE)


class LockTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.stack = contextlib.ExitStack()
        self.stack.enter_context(quiet())
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


def git(repo, *args):
    subprocess.run(['git', *args], cwd=repo, check=True, capture_output=True)


class ComparisonTests(unittest.TestCase):
    """bsl_new_findings: база из того же контекста каталога, ERROR - результат, не сбой."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp.name).resolve() / 'repo'
        self.module_dir = self.repo / 'mod'
        self.module_dir.mkdir(parents=True)
        git(self.repo, 'init', '-q')
        for key, value in [('core.autocrlf', 'true'), ('user.name', 'Тест'),
                           ('user.email', 'test@example.invalid'), ('commit.gpgsign', 'false')]:
            git(self.repo, 'config', key, value)
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

    def main(self, check, *revision):
        with patch.object(sys, 'argv', ['compare', str(self.target), *revision]), \
                patch.object(comparison, 'проверить', side_effect=check) as mocked:
            return comparison.main(), mocked

    def test_unchanged_directory_checks_once_despite_crlf_worktree(self):
        self.assertNotEqual(self.target.read_bytes(), subprocess.run(
            ['git', 'show', 'HEAD:mod/Module.bsl'], cwd=self.repo, capture_output=True).stdout)
        code, mocked = self.main(lambda path, level: ([], []))
        self.assertEqual(code, 0)
        self.assertEqual(mocked.call_count, 1)
        self.assertEqual(mocked.call_args.args[1], '--all')

    def test_changed_sibling_requires_base_run_with_revision_context(self):
        base_sibling = self.sibling.read_bytes()
        self.sibling.write_bytes('// сосед изменён\r\n'.encode('utf-8'))
        seen = {}
        def check(path, level):
            seen[level] = Path(path)
            if level == '--deep':
                seen['sibling'] = (Path(path).parent / 'Second.bsl').read_bytes()
                seen['target'] = Path(path).read_bytes()
            return ([], [])
        code, mocked = self.main(check)
        self.assertEqual((code, mocked.call_count), (0, 2))
        self.assertEqual(seen['sibling'], base_sibling)
        self.assertEqual(seen['target'], self.target.read_bytes())
        self.assertFalse(seen['--deep'].parent.exists())

    def test_new_file_has_empty_base(self):
        new = self.module_dir / 'New.bsl'
        new.write_text('// новый\n', encoding='utf-8')
        self.target = new
        code, mocked = self.main(lambda path, level: (
            [('WARN', 'Rule', 'сообщение', 'текст', 1)], []))
        self.assertEqual((code, mocked.call_count), (1, 1))
        self.assertIn('НОВОЕ WARN', self.output.getvalue())

    def test_invalid_revision_fails_before_check(self):
        code, mocked = self.main(lambda path, level: ([], []), 'нет-такой-ревизии')
        self.assertEqual(code, 2)
        mocked.assert_not_called()

    def test_base_failure_fails_and_removes_temporary_base(self):
        self.target.write_bytes((CLEAN_MODULE + BAD_PROCEDURE).encode('utf-8'))
        seen = []
        def check(path, level):
            if level == '--deep':
                seen.append(Path(path))
                raise comparison.СбойПроверки('сбой базы')
            return ([], [])
        self.assertEqual(self.main(check)[0], 2)
        self.assertEqual(len(seen), 1)
        self.assertFalse(seen[0].parent.exists())

    def test_only_new_findings_are_reported(self):
        self.target.write_bytes((CLEAN_MODULE + BAD_PROCEDURE).encode('utf-8'))
        old = ('WARN', 'Старое', 'было', 'текст', 3)
        def check(path, level):
            if level == '--deep':
                return ([old], [])
            # Старая находка сместилась на другую строку: ключ без номера строки совпадает.
            return ([('WARN', 'Старое', 'было', 'текст', 9),
                     ('ERROR', 'Новое', 'стало', 'Возврат 1;', 10)], [])
        code, _ = self.main(check)
        self.assertEqual(code, 1)
        text = self.output.getvalue()
        self.assertIn('НОВОЕ ERROR L10', text)
        self.assertNotIn('Старое', text)

    def test_oscript_unknown_symbol_is_incomplete(self):
        code, _ = self.main(lambda path, level: (
            [], ['{Модуль X / Ошибка в строке: 5 / Неизвестный символ: Ext_Y}']))
        self.assertEqual(code, 2)
        self.assertIn('синтаксис после этой строки не проверен', self.output.getvalue())

    def test_oscript_unknown_local_variable_cannot_pass(self):
        code, _ = self.main(lambda path, level: (
            [], ['{Модуль X / Ошибка в строке: 5 / Неизвестный символ: НесуществующаяПеременная}']))
        self.assertEqual(code, 2)

    def test_oscript_real_error_is_error(self):
        code, _ = self.main(lambda path, level: (
            [], ['{Модуль X / Ошибка в строке: 4 / Процедуры не могут возвращать значение}']))
        self.assertEqual(code, 1)
        self.assertIn('oscript ERROR', self.output.getvalue())

    def completed(self, code, stdout):
        return subprocess.CompletedProcess([], code, stdout.encode('utf-8'), b'')

    def test_check_with_error_findings_is_result_not_failure(self):
        self.target.write_bytes((CLEAN_MODULE + BAD_PROCEDURE).encode('utf-8'))
        output = ('      Findings: 2\n'
                  '      ERROR   L10   ProcedureReturnsValue: Процедура содержит "Возврат"\n'
                  '      HINT    L1    UsingServiceTag: тег\n'
                  '      ОШИБКИ синтаксиса:\n'
                  '      | {Модуль X / Ошибка в строке: 10 / Процедуры не могут}\n'
                  '      | \tВозврат 1;\n'
                  '=== РЕЗУЛЬТАТ: ERRORS ===\n')
        with patch.object(comparison.subprocess, 'run', return_value=self.completed(2, output)):
            findings, oscript = comparison.проверить(self.target, '--all')
        self.assertEqual(findings, [('ERROR', 'ProcedureReturnsValue', 'Процедура содержит "Возврат"',
                                     'Возврат 1;', 10)])
        self.assertEqual(len(oscript), 2)

    def test_incomplete_or_unparsed_check_is_failure(self):
        cases = [(3, '=== РЕЗУЛЬТАТ: ПРОВЕРКА НЕ ВЫПОЛНЕНА ===\n'),
                 (0, ''),
                 (0, '=== РЕЗУЛЬТАТ: OK ===\n'),
                 (1, '      Findings: 2\n      WARN    L1    A: б\n=== РЕЗУЛЬТАТ: WARNINGS ===\n')]
        for code, output in cases:
            with self.subTest(code=code, output=output):
                with patch.object(comparison.subprocess, 'run',
                                  return_value=self.completed(code, output)):
                    with self.assertRaises(comparison.СбойПроверки):
                        comparison.проверить(self.target, '--all')


def integration_available():
    return (not os.environ.get('BSL_TESTS_SKIP_INTEGRATION') and checker.find_java()
            and checker.find_bsl_ls_jar() and checker.find_oscript())


@unittest.skipUnless(integration_available(), 'нет Java/JAR/oscript или BSL_TESTS_SKIP_INTEGRATION')
class IntegrationTests(unittest.TestCase):
    """Настоящие JAR и oscript: реальные ошибки BSL, межмодульный контекст с метаданными."""

    def setUp(self):
        base = checker.PROJECT_ROOT / '_temp'
        base.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix='bsl_tests_', dir=base)
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def check(self, *args, cwd):
        env = dict(os.environ, PYTHONIOENCODING='utf-8')
        # Страховка теста длиннее собственного таймаута JVM обёртки: зависание - провал, не пропуск.
        result = subprocess.run([sys.executable, str(TOOLS / 'check_bsl.py'), *map(str, args)],
                                capture_output=True, cwd=cwd, env=env, timeout=checker.BSL_LS_TIMEOUT_SEC + 180)
        return result.returncode, result.stdout.decode('utf-8', 'replace')

    def run_in_process(self, target, config, **scope):
        """run_bsl_ls_check с подменённым конфигом релиза (исходный файл не меняется)."""
        output = io.StringIO()
        with patch.object(checker, 'BSL_LS_CONFIG', config), contextlib.redirect_stdout(output):
            code, _ = checker.run_bsl_ls_check(target, **scope)
        return code, output.getvalue()

    def xmod_copy(self):
        copy = self.root / 'xmod'
        shutil.copytree(FIXTURES / 'xmod', copy)
        return copy

    def test_broken_designer_metadata_is_incomplete(self):
        # BSL LS 1.0.7: WARN mdclasses «Can't read file», код 0, metadata пусты - раньше это был OK.
        xmod = self.xmod_copy()
        configuration = xmod / 'Configuration.xml'
        configuration.write_bytes(configuration.read_bytes()[:400])
        consumer = xmod / 'CommonModules' / 'Потребитель' / 'Ext' / 'Module.bsl'
        code, output = self.check(consumer, '--deep', '--source-dir', xmod, cwd=checker.PROJECT_ROOT)
        self.assertEqual(code, checker.INCOMPLETE, output)
        self.assertIn('Проверка неполная', output)
        self.assertNotIn('РЕЗУЛЬТАТ: OK', output)

    def test_unregistered_module_metadata_is_incomplete(self):
        # Модуль не перечислен в Configuration.xml: BSL LS 1.0.7 без WARN, код 0, mdoRef цели - URI файла,
        # CommonModuleInvalidType пропадает. Без проверки mdoRef итог был бы OK.
        xmod = self.xmod_copy()
        configuration = xmod / 'Configuration.xml'
        text = configuration.read_text(encoding='utf-8')
        configuration.write_text(text.replace('\t\t\t<CommonModule>Потребитель</CommonModule>\n', ''),
                                 encoding='utf-8', newline='')
        self.assertNotIn('Потребитель', configuration.read_text(encoding='utf-8'))
        consumer = xmod / 'CommonModules' / 'Потребитель' / 'Ext' / 'Module.bsl'
        code, output = self.check(consumer, '--deep', '--source-dir', xmod, cwd=checker.PROJECT_ROOT)
        self.assertEqual(code, checker.INCOMPLETE, output)
        self.assertIn('metadata выгрузки', output)
        self.assertNotIn('РЕЗУЛЬТАТ: OK', output)

    def test_invalid_rule_parameter_is_incomplete(self):
        # Строка вместо булева/объекта: ERROR «Can't deserialize parameter configuration» при коде 0.
        config = json.loads(checker.BSL_LS_CONFIG.read_text(encoding='utf-8'))
        config['diagnostics']['parameters']['Typo'] = 'yes'
        config_path = self.root / 'config.json'
        config_path.write_text(json.dumps(config, ensure_ascii=False), encoding='utf-8')
        code, output = self.run_in_process(FIXTURES / 'errors' / 'Module.bsl', config_path)
        self.assertEqual(code, checker.INCOMPLETE, output)
        self.assertIn("Can't deserialize parameter configuration", output)

    @unittest.skipUnless(os.name == 'nt', 'блокировка диапазона байтов для чтения JVM - только Windows')
    def test_locked_source_file_is_incomplete(self):
        import msvcrt
        target = self.root / 'Module.bsl'
        shutil.copy2(FIXTURES / 'errors' / 'Module.bsl', target)
        size = target.stat().st_size
        with target.open('r+b') as stream:
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, size)
            try:
                code, output = self.check(target, '--deep', cwd=checker.PROJECT_ROOT)
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, size)
        self.assertEqual(code, checker.INCOMPLETE, output)
        self.assertNotIn('Findings:', output)

    def test_same_basename_modules_are_matched_by_full_path(self):
        # Два Module.bsl в одной выгрузке: находка потребителя не должна попасть в итог поставщика.
        xmod = self.xmod_copy()
        supplier = xmod / 'CommonModules' / 'Поставщик' / 'Ext' / 'Module.bsl'
        code, output = self.check(supplier, '--deep', '--source-dir', xmod, cwd=checker.PROJECT_ROOT)
        self.assertEqual(code, 2, output)
        self.assertIn('CommonModuleInvalidType', output)
        self.assertNotIn('DeprecatedMethodCall', output)

    def test_working_directory_outside_release(self):
        with tempfile.TemporaryDirectory(prefix='bsl_cwd_') as outside:
            self.assertFalse(Path(outside).resolve().is_relative_to(checker.PROJECT_ROOT))
            code, output = self.check(FIXTURES / 'errors' / 'Module.bsl', '--all', cwd=outside)
        self.assertEqual(code, 2, output)
        self.assertIn('Findings: 3', output)
        self.assertIn('Процедуры не могут возвращать значение', output)

    def test_real_errors_from_other_working_directory(self):
        code, output = self.check(FIXTURES / 'errors' / 'Module.bsl', '--all', cwd=TOOLS)
        self.assertEqual(code, 2, output)
        for rule in ('ProcedureReturnsValue', 'ParseError', 'UnreachableCode'):
            self.assertIn(rule, output)
        self.assertIn('Процедуры не могут возвращать значение', output)
        self.assertIn('Findings: 3', output)

    def test_nested_archive_is_not_analyzed(self):
        shutil.copy2(FIXTURES / 'errors' / 'Module.bsl', self.root / 'Module.bsl')
        (self.root / '_archive').mkdir()
        shutil.copy2(FIXTURES / 'errors' / 'Module.bsl', self.root / '_archive' / 'Old.bsl')
        code, output = self.check(self.root / 'Module.bsl', '--deep', cwd=checker.PROJECT_ROOT)
        self.assertEqual(code, 2, output)
        self.assertIn('без вложенных каталогов, файлов .bsl/.os: 1', output)
        self.assertIn('Findings: 3', output)

    def test_cross_module_context_with_metadata(self):
        config = json.loads(checker.BSL_LS_CONFIG.read_text(encoding='utf-8'))
        config['configurationRoot'] = '.'  # реальный конфиг; исправляет сам --source-dir
        config_path = self.root / 'config.json'
        config_path.write_text(json.dumps(config, ensure_ascii=False), encoding='utf-8')
        consumer = FIXTURES / 'xmod' / 'CommonModules' / 'Потребитель' / 'Ext' / 'Module.bsl'
        results = {}
        for mode, scope in [('context', {'source_dir': str(FIXTURES / 'xmod')}),
                            ('standalone', {'standalone': True})]:
            output = io.StringIO()
            with patch.object(checker, 'BSL_LS_CONFIG', config_path), \
                    contextlib.redirect_stdout(output):
                code, _ = checker.run_bsl_ls_check(consumer, **scope)
            results[mode] = (code, output.getvalue())
        self.assertEqual(results['context'][0], 2, results['context'][1])
        self.assertIn('DeprecatedMethodCall', results['context'][1])
        self.assertIn('CommonModuleInvalidType', results['context'][1])
        self.assertEqual(results['standalone'][0], 0, results['standalone'][1])
        self.assertNotIn('DeprecatedMethodCall', results['standalone'][1])

    def test_new_findings_end_to_end(self):
        repo = self.root / 'repo'
        (repo / 'mod').mkdir(parents=True)
        git(repo, 'init', '-q')
        for key, value in [('core.autocrlf', 'true'), ('user.name', 'Тест'),
                           ('user.email', 'test@example.invalid'), ('commit.gpgsign', 'false')]:
            git(repo, 'config', key, value)
        target = repo / 'mod' / 'Module.bsl'
        target.write_bytes(CLEAN_MODULE.encode('utf-8'))
        git(repo, 'add', '.')
        git(repo, 'commit', '-q', '-m', 'база')
        target.write_bytes((CLEAN_MODULE + BAD_PROCEDURE).encode('utf-8'))
        env = dict(os.environ, PYTHONIOENCODING='utf-8')
        result = subprocess.run([sys.executable, str(TOOLS / 'bsl_new_findings.py'), str(target)],
                                capture_output=True, cwd=repo, env=env, timeout=2 * checker.BSL_LS_TIMEOUT_SEC + 300)
        output = result.stdout.decode('utf-8', 'replace')
        self.assertEqual(result.returncode, 1, output)
        self.assertIn('ProcedureReturnsValue', output)
        self.assertIn('НОВОЕ ERROR', output)
        self.assertIn('oscript ERROR', output)

    def test_new_findings_unknown_symbol_outside_release_is_incomplete(self):
        # Репозиторий и cwd вне релиза; BSL LS без новых ERROR, OneScript - «Неизвестный символ».
        with tempfile.TemporaryDirectory(prefix='bsl_git_') as outside:
            repo = Path(outside).resolve() / 'repo'
            (repo / 'mod').mkdir(parents=True)
            git(repo, 'init', '-q')
            for key, value in [('core.autocrlf', 'true'), ('user.name', 'Тест'),
                               ('user.email', 'test@example.invalid'), ('commit.gpgsign', 'false')]:
                git(repo, 'config', key, value)
            target = repo / 'mod' / 'Module.bsl'
            target.write_bytes(CLEAN_MODULE.encode('utf-8'))
            git(repo, 'add', '.')
            git(repo, 'commit', '-q', '-m', 'база')
            target.write_bytes((CLEAN_MODULE + UNKNOWN_SYMBOL).encode('utf-8'))
            env = dict(os.environ, PYTHONIOENCODING='utf-8')
            result = subprocess.run([sys.executable, str(TOOLS / 'bsl_new_findings.py'), str(target)],
                                    capture_output=True, cwd=repo, env=env,
                                    timeout=2 * checker.BSL_LS_TIMEOUT_SEC + 300)
        output = result.stdout.decode('utf-8', 'replace')
        self.assertEqual(result.returncode, 2, output)
        self.assertIn('Неизвестный символ', output)
        self.assertIn('проверка не выполнена полностью', output)
        self.assertIn('ERROR всего в модуле: 0', output)


if __name__ == '__main__':
    unittest.main()
