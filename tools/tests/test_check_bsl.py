"""Регресс обвязки BSL LS: сбой не становится успехом, найденная ERROR не становится сбоем.

Запуск из корня проекта: python -m unittest tools/tests/test_check_bsl.py -v
Интеграционные тесты запускают настоящие JAR BSL LS и oscript (несколько минут);
пропуск: переменная окружения BSL_TESTS_SKIP_INTEGRATION=1.
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

    def run_report(self, report, code=0, output='', reports=1, **scope):
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
            return checker.run_bsl_ls_check(self.target, quiet=True, **scope)

    def report(self, diagnostics=None, path=None):
        return {'fileinfos': [{'path': str((path or self.target).relative_to(self.root)),
                               'diagnostics': diagnostics or []}]}

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

    def test_jvm_runs_from_project_root(self):
        self.run_report(self.report())
        cmd, kwargs, _ = self.calls[0]
        self.assertEqual(kwargs['cwd'], self.root)
        self.assertNotIn('--workspaceDir', cmd)
        self.assertIn('-XX:TieredStopAtLevel=1', cmd)
        self.assertTrue(any(arg.startswith('-XX:ActiveProcessorCount=') for arg in cmd))
        self.assertLess(cmd.index('-XX:TieredStopAtLevel=1'), cmd.index('-jar'))

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
        result = subprocess.run([sys.executable, str(TOOLS / 'check_bsl.py'), *map(str, args)],
                                capture_output=True, cwd=cwd, env=env, timeout=300)
        return result.returncode, result.stdout.decode('utf-8', 'replace')

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
                                capture_output=True, cwd=repo, env=env, timeout=600)
        output = result.stdout.decode('utf-8', 'replace')
        self.assertEqual(result.returncode, 1, output)
        self.assertIn('ProcedureReturnsValue', output)
        self.assertIn('НОВОЕ ERROR', output)
        self.assertIn('oscript ERROR', output)


if __name__ == '__main__':
    unittest.main()
