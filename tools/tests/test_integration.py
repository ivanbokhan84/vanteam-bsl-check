"""Интеграционные тесты VANTEAM BSL Server check: реальный JAR форка v1.0.7-vanteam.1 и реальная Java.

Нужна установка setup_bsl_server.py (VANTEAM_BSL_HOME или %LOCALAPPDATA%\\vanteam-bsl-server) и git.
Установки нет - тесты падают, а не пропускаются: пропуск интеграционного теста не является успехом.
Явный отказ от прогона - VANTEAM_BSL_TESTS_SKIP_INTEGRATION=1 (такой прогон не является приёмкой).
Временные файлы - в tmp установки (её диск), не в каталоге проекта.

Запуск из каталога checker: python -m unittest tests.test_integration -v   (несколько минут)
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))
import check_bsl as checker  # noqa: E402
import setup_bsl_server as setup  # noqa: E402

FIXTURES = TOOLS / 'tests' / 'fixtures' / 'bsl_check'
CLEAN_MODULE = ('// Модуль для теста.\r\n//\r\n// Возвращаемое значение:\r\n//  Число - 1.\r\n//\r\n'
                'Функция Один() Экспорт\r\n\tВозврат 1;\r\nКонецФункции\r\n')
BAD_PROCEDURE = 'Процедура Плохая() Экспорт\r\n\tВозврат 1;\r\nКонецПроцедуры\r\n'
SKIP = bool(os.environ.get('VANTEAM_BSL_TESTS_SKIP_INTEGRATION'))
HOME = checker.install_home()
# Процессы, которые обёртка вправе запускать; всё остальное (в том числе oscript) - провал.
AUDIT_RUNNER = r'''
import json, runpy, subprocess, sys
log = open(sys.argv[1], 'a', encoding='utf-8')
def hook(event, args):
    if event == 'subprocess.Popen':
        # На Windows args уже собраны в командную строку (list2cmdline).
        command = args[1] if isinstance(args[1], str) else subprocess.list2cmdline([str(a) for a in args[1]])
        log.write(json.dumps([event, command], ensure_ascii=False) + '\n')
    elif event in ('os.system', 'os.startfile', 'os.spawn', 'os.exec', 'os.posix_spawn'):
        log.write(json.dumps([event, repr(args)], ensure_ascii=False) + '\n')
    log.flush()
sys.addaudithook(hook)
sys.argv = sys.argv[2:]
runpy.run_path(sys.argv[0], run_name='__main__')
'''


def git(repo, *args):
    subprocess.run(['git', *args], cwd=repo, check=True, capture_output=True)


def init_repo(repo):
    repo.mkdir(parents=True, exist_ok=True)
    git(repo, 'init', '-q')
    for key, value in [('core.autocrlf', 'true'), ('user.name', 'Тест'),
                       ('user.email', 'test@example.invalid'), ('commit.gpgsign', 'false')]:
        git(repo, 'config', key, value)


class Integration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if SKIP:
            raise unittest.SkipTest('VANTEAM_BSL_TESTS_SKIP_INTEGRATION: интеграционные тесты не выполнены')
        install = checker.load_install(HOME)
        if install is None:
            raise AssertionError(f'нет установки BSL Server в {HOME}: выполните setup_bsl_server.py '
                                 '(пропуск интеграционных тестов не является успехом)')
        cls.install = install
        cls.env = {k: v for k, v in os.environ.items() if k not in ('BSL_LS_JAR', 'VANTEAM_BSL_JAVA',
                                                                    'VANTEAM_BSL_XMX', 'VANTEAM_BSL_TIMEOUT')}
        cls.env.update(VANTEAM_BSL_HOME=str(HOME), PYTHONIOENCODING='utf-8')

    def setUp(self):
        base = HOME / 'tmp'
        base.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix='tests_', dir=base, ignore_cleanup_errors=True)
        self.root = Path(self.temp.name)

    def tearDown(self):
        # Архив CDS JVM создаёт только для чтения: обычная очистка его бы оставила.
        setup.remove_tree(self.root)
        self.temp.cleanup()

    def run_tool(self, script, *args, cwd=None, env=None, timeout=600):
        result = subprocess.run([sys.executable, str(TOOLS / script), *map(str, args)], capture_output=True,
                                cwd=cwd or self.root, env=dict(self.env, **(env or {})), timeout=timeout)
        return result.returncode, result.stdout.decode('utf-8', 'replace') + result.stderr.decode('utf-8', 'replace')

    def check(self, *args, **kwargs):
        return self.run_tool('check_bsl.py', *args, **kwargs)

    def check_json(self, *args, **kwargs):
        result = subprocess.run([sys.executable, str(TOOLS / 'check_bsl.py'), *map(str, args), '--json'],
                                capture_output=True, cwd=kwargs.get('cwd', self.root),
                                env=dict(self.env, **kwargs.get('env', {})), timeout=600)
        return result.returncode, json.loads(result.stdout.decode('utf-8'))

    def xmod_copy(self):
        copy = self.root / 'xmod'
        shutil.copytree(FIXTURES / 'xmod', copy)
        return copy

    def project_with_config(self, config):
        """Проект во временном каталоге: копия check_bsl.py в tools/ и конфиг tools/bsl_ls проекта."""
        project = self.root / 'project'
        (project / 'tools' / 'bsl_ls').mkdir(parents=True)
        shutil.copy2(TOOLS / 'check_bsl.py', project / 'tools' / 'check_bsl.py')
        (project / 'tools' / 'bsl_ls' / checker.CONFIG_NAME).write_text(json.dumps(config, ensure_ascii=False),
                                                                          encoding='utf-8')
        return project

    def run_project(self, project, *args):
        result = subprocess.run([sys.executable, str(project / 'tools' / 'check_bsl.py'), *map(str, args)],
                                capture_output=True, cwd=project, env=self.env, timeout=600)
        return result.returncode, result.stdout.decode('utf-8', 'replace')

    def default_config(self):
        return json.loads(checker.BUNDLED_CONFIG.read_text(encoding='utf-8'))


class RealEngineTests(Integration):
    """Реальные ошибки BSL, межмодульный контекст, несколько модулей, защиты неполной проверки."""

    def test_skill_call_real_errors_code_2(self):
        # Вызов скилла checkbsl: PYTHONIOENCODING=utf-8 python tools/check_bsl.py "<путь>" --deep
        code, output = self.check(FIXTURES / 'errors' / 'Module.bsl', '--deep', cwd=TOOLS)
        self.assertEqual(code, 2, output)
        for rule in ('ProcedureReturnsValue', 'ParseError', 'UnreachableCode'):
            self.assertIn(rule, output)
        self.assertIn('Findings: 3', output)
        self.assertIn('BSL Language Server 1.0.7-vanteam.1', output)
        self.assertIn('распакованный JAR + CDS', output)
        self.assertIn('синтакс-помощник 1С, контекстов:', output)
        self.assertIn('=== РЕЗУЛЬТАТ: ERRORS ===', output)

    def test_working_directory_outside_project_and_module_on_other_drive(self):
        # Модуль на диске установки, корень проекта - на диске поставки: JVM запускается из области.
        module = self.root / 'errors' / 'Module.bsl'
        module.parent.mkdir()
        shutil.copy2(FIXTURES / 'errors' / 'Module.bsl', module)
        code, data = self.check_json(module, cwd=self.root)
        self.assertEqual(code, 2, data['messages'])
        self.assertEqual(data['modules'][0]['counts'], {'Error': 3})
        expected_cwd = checker.PROJECT_ROOT if checker.same_drive(checker.PROJECT_ROOT, module) else module.parent
        self.assertEqual(Path(data['runs'][0]['cwd']), expected_cwd)

    def test_cross_module_context_with_metadata(self):
        consumer = FIXTURES / 'xmod' / 'CommonModules' / 'Потребитель' / 'Ext' / 'Module.bsl'
        code, output = self.check(consumer, '--deep', '--source-dir', FIXTURES / 'xmod')
        self.assertEqual(code, 2, output)
        self.assertIn('DeprecatedMethodCall', output)
        self.assertIn('CommonModuleInvalidType', output)
        code, output = self.check(consumer, '--deep', '--standalone')
        self.assertEqual(code, 0, output)
        self.assertNotIn('DeprecatedMethodCall', output)

    def test_same_basename_modules_are_matched_by_full_path(self):
        xmod = self.xmod_copy()
        supplier = xmod / 'CommonModules' / 'Поставщик' / 'Ext' / 'Module.bsl'
        code, output = self.check(supplier, '--deep', '--source-dir', xmod)
        self.assertEqual(code, 2, output)
        self.assertIn('CommonModuleInvalidType', output)
        self.assertNotIn('DeprecatedMethodCall', output)

    def test_several_modules_one_jvm(self):
        xmod = self.xmod_copy()
        supplier = xmod / 'CommonModules' / 'Поставщик' / 'Ext' / 'Module.bsl'
        consumer = xmod / 'CommonModules' / 'Потребитель' / 'Ext' / 'Module.bsl'
        code, data = self.check_json(supplier, consumer, '--source-dir', xmod)
        self.assertEqual(code, 2, data['messages'])
        self.assertEqual(len(data['runs']), 1)
        self.assertEqual(data['runs'][0]['targets'], [str(supplier), str(consumer)])
        self.assertEqual(data['runs'][0]['command'].count('--target'), 2)
        codes = [{f['code'] for f in m['findings']} for m in data['modules']]
        self.assertNotIn('DeprecatedMethodCall', codes[0])
        self.assertIn('DeprecatedMethodCall', codes[1])
        self.assertEqual([m['mdoRef'] for m in data['modules']],
                         ['CommonModule.Поставщик', 'CommonModule.Потребитель'])
        # Модули разных каталогов без --source-dir - по одной JVM на каталог.
        errors = self.root / 'errors' / 'Module.bsl'
        errors.parent.mkdir()
        shutil.copy2(FIXTURES / 'errors' / 'Module.bsl', errors)
        code, data = self.check_json(errors, supplier, consumer)
        self.assertEqual(code, 2, data['messages'])
        self.assertEqual(len(data['runs']), 3)

    def test_nested_archive_is_not_analyzed(self):
        shutil.copy2(FIXTURES / 'errors' / 'Module.bsl', self.root / 'Module.bsl')
        (self.root / '_archive').mkdir()
        shutil.copy2(FIXTURES / 'errors' / 'Module.bsl', self.root / '_archive' / 'Old.bsl')
        code, output = self.check(self.root / 'Module.bsl', '--deep')
        self.assertEqual(code, 2, output)
        self.assertIn('без вложенных каталогов, файлов .bsl/.os: 1', output)
        self.assertIn('Findings: 3', output)

    def test_broken_designer_metadata_is_incomplete(self):
        xmod = self.xmod_copy()
        configuration = xmod / 'Configuration.xml'
        configuration.write_bytes(configuration.read_bytes()[:400])
        consumer = xmod / 'CommonModules' / 'Потребитель' / 'Ext' / 'Module.bsl'
        code, output = self.check(consumer, '--deep', '--source-dir', xmod)
        self.assertEqual(code, checker.INCOMPLETE, output)
        self.assertIn('Проверка неполная', output)
        self.assertNotIn('РЕЗУЛЬТАТ: OK', output)

    def test_unregistered_module_metadata_is_incomplete(self):
        xmod = self.xmod_copy()
        configuration = xmod / 'Configuration.xml'
        text = configuration.read_text(encoding='utf-8')
        configuration.write_text(text.replace('\t\t\t<CommonModule>Потребитель</CommonModule>\n', ''),
                                 encoding='utf-8', newline='')
        self.assertNotIn('Потребитель', configuration.read_text(encoding='utf-8'))
        consumer = xmod / 'CommonModules' / 'Потребитель' / 'Ext' / 'Module.bsl'
        code, output = self.check(consumer, '--deep', '--source-dir', xmod)
        self.assertEqual(code, checker.INCOMPLETE, output)
        self.assertIn('metadata выгрузки', output)

    def test_invalid_rule_parameter_in_project_config_is_incomplete(self):
        config = self.default_config()
        config['diagnostics']['parameters']['Typo'] = 'yes'
        project = self.project_with_config(config)
        module = project / 'src' / 'Module.bsl'
        module.parent.mkdir()
        shutil.copy2(FIXTURES / 'errors' / 'Module.bsl', module)
        code, output = self.run_project(project, module, '--deep')
        self.assertEqual(code, checker.INCOMPLETE, output)
        self.assertIn("Can't deserialize parameter configuration", output)
        self.assertIn('(проекта)', output)

    def test_project_target_version_is_kept_with_installed_help(self):
        # Режим совместимости проекта 8.2.16: СтрНайти недоступна; справка установки подставлена через binPath.
        config = self.default_config()
        config['v8platform'] = {'targetVersion': '8.2.16'}
        project = self.project_with_config(config)
        module = project / 'src' / 'Module.bsl'
        module.parent.mkdir()
        module.write_text('// Тест.\n//\n// Параметры:\n//  Текст - Строка - текст.\n//\n'
                          '// Возвращаемое значение:\n//  Число - позиция.\n//\n'
                          'Функция Позиция(Текст) Экспорт\n\tВозврат СтрНайти(Текст, "а");\nКонецФункции\n',
                          encoding='utf-8')
        code, output = self.run_project(project, module, '--deep')
        self.assertIn('UnavailableMemberCall', output)
        self.assertIn('только русская справка установки', output)
        self.assertNotEqual(code, checker.INCOMPLETE, output)

    def test_engine_rejects_target_excluded_by_config(self):
        # Цель исключена excludePaths проекта: форк завершает анализ с кодом 1 - проверка не выполнена.
        config = self.default_config()
        config['excludePaths'] = ['Поставщик']
        project = self.project_with_config(config)
        xmod = self.xmod_copy()
        supplier = xmod / 'CommonModules' / 'Поставщик' / 'Ext' / 'Module.bsl'
        code, output = self.run_project(project, supplier, '--source-dir', xmod)
        self.assertEqual(code, checker.INCOMPLETE, output)
        self.assertIn('модуль вне файлов области', output)
        self.assertIn('is not among source files', output)

    def test_target_outside_source_dir_is_incomplete(self):
        xmod = self.xmod_copy()
        code, output = self.check(FIXTURES / 'errors' / 'Module.bsl', '--source-dir', xmod)
        self.assertEqual(code, checker.INCOMPLETE, output)
        self.assertIn('Модуль вне области --source-dir', output)

    def test_out_of_memory_is_incomplete(self):
        code, output = self.check(FIXTURES / 'errors' / 'Module.bsl', env={'VANTEAM_BSL_XMX': '48m'})
        self.assertEqual(code, checker.INCOMPLETE, output)
        self.assertIn('нехватка памяти JVM (-Xmx48m)', output)
        self.assertNotIn('Findings:', output)

    @unittest.skipUnless(os.name == 'nt', 'блокировка диапазона байтов для чтения JVM - только Windows')
    def test_locked_source_file_is_incomplete(self):
        import msvcrt
        target = self.root / 'Module.bsl'
        shutil.copy2(FIXTURES / 'errors' / 'Module.bsl', target)
        size = target.stat().st_size
        with target.open('r+b') as stream:
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, size)
            try:
                code, output = self.check(target, '--deep')
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, size)
        self.assertEqual(code, checker.INCOMPLETE, output)
        self.assertNotIn('Findings:', output)

    def test_no_oscript_is_started(self):
        # Все процессы Python-обёртки - через аудит; фейковый oscript первым в PATH отмечает вызов по имени.
        fake = self.root / 'fake_oscript'
        fake.mkdir()
        marker = self.root / 'oscript-called'
        for name in ('oscript.cmd', 'oscript.bat'):
            (fake / name).write_text(f'@echo called> "{marker}"\r\n', encoding='utf-8')
        log = self.root / 'audit.log'
        env = dict(self.env, PATH=str(fake) + os.pathsep + self.env.get('PATH', ''))
        module = FIXTURES / 'errors' / 'Module.bsl'
        for args in (['check_bsl.py', module, '--all'], ['check_bsl.py', module]):
            result = subprocess.run([sys.executable, '-c', AUDIT_RUNNER, str(log), str(TOOLS / args[0]),
                                     *map(str, args[1:])], capture_output=True, env=env, cwd=self.root,
                                    timeout=600)
            self.assertEqual(result.returncode, 2, result.stdout.decode('utf-8', 'replace'))
        events = [json.loads(line) for line in log.read_text(encoding='utf-8').splitlines()]
        java = self.install['java']['path']
        self.assertEqual(len(events), 2, events)
        for event, command in events:
            self.assertEqual(event, 'subprocess.Popen')
            self.assertTrue(command.startswith((java, subprocess.list2cmdline([java]))), command)
            # Путь проекта может содержать «oscript» (например, Vanteam-BSL-OScript), поэтому проверяется не подстрока,
            # а то, что ни один аргумент не является исполняемым файлом OneScript.
            tokens = [t.strip('"') for t in re.findall(r'"[^"]*"|\S+', command)]
            launched = {Path(t).name.lower() for t in tokens}
            self.assertFalse(launched & {'oscript', 'oscript.exe', 'oscript.cmd', 'oscript.bat'}, command)
        self.assertFalse(marker.exists(), 'вызван oscript')


class ComparisonIntegrationTests(Integration):
    """bsl_new_findings на реальном движке во временном git-репозитории."""

    def test_new_findings_end_to_end(self):
        repo = self.root / 'repo'
        init_repo(repo)
        target = repo / 'mod' / 'Module.bsl'
        target.parent.mkdir()
        target.write_bytes(CLEAN_MODULE.encode('utf-8'))
        (repo / 'mod' / 'Second.bsl').write_bytes('// сосед\r\n'.encode('utf-8'))
        git(repo, 'add', '.')
        git(repo, 'commit', '-q', '-m', 'база')
        code, output = self.run_tool('bsl_new_findings.py', target, cwd=repo)
        self.assertEqual(code, 0, output)
        self.assertIn('базовый прогон не нужен', output)
        target.write_bytes((CLEAN_MODULE + BAD_PROCEDURE).encode('utf-8'))
        code, output = self.run_tool('bsl_new_findings.py', target, cwd=repo)
        self.assertEqual(code, 1, output)
        self.assertIn('НОВОЕ ERROR', output)
        self.assertIn('ProcedureReturnsValue', output)
        self.assertIn('ERROR всего в модуле: 1', output)
        self.assertNotIn('oscript', output.lower())

    def test_new_findings_source_dir_base_has_metadata(self):
        # База собрана из всех файлов выгрузки в ревизии: без XML metadata базовый прогон был бы неполным (код 2).
        repo = self.root / 'repo'
        init_repo(repo)
        xmod = repo / 'xmod'
        shutil.copytree(FIXTURES / 'xmod', xmod)
        git(repo, 'add', '.')
        git(repo, 'commit', '-q', '-m', 'выгрузка')
        consumer = xmod / 'CommonModules' / 'Потребитель' / 'Ext' / 'Module.bsl'
        with consumer.open('ab') as stream:
            stream.write(('\r\n' + BAD_PROCEDURE).encode('utf-8'))
        code, output = self.run_tool('bsl_new_findings.py', consumer, '--source-dir', xmod, cwd=repo)
        self.assertEqual(code, 1, output)
        self.assertIn('База: ревизия HEAD, файлов области: 5', output)
        self.assertIn('НОВОЕ ERROR', output)
        self.assertIn('ProcedureReturnsValue', output)
        self.assertNotIn('НОВОЕ ERROR L1 ', output)  # CommonModuleInvalidType - старая находка
        self.assertNotIn('DeprecatedMethodCall', output)


class SetupIntegrationTests(Integration):
    """setup_bsl_server.py на реальной Java и JAR."""

    def test_check_of_installation_passes(self):
        code, output = self.run_tool('setup_bsl_server.py', '--check', '--home', HOME)
        self.assertEqual(code, 0, output)
        self.assertIn('Итог: установка исправна', output)
        self.assertIn('анализ фикстуры с CDS', output)
        self.assertNotIn('FAIL', output)

    def test_fresh_install_trains_cds_and_second_run_is_idempotent(self):
        home = self.root / 'home'
        jar = HOME / self.install['engine']['jar']
        java = self.install['java']['path']
        started = time.monotonic()
        code, output = self.run_tool('setup_bsl_server.py', '--home', home, '--jar', jar, '--java', java,
                                     timeout=900)
        first = time.monotonic() - started
        self.assertEqual(code, 0, output)
        install = checker.load_install(home)
        self.assertEqual(install['engine']['sha256'], setup.RELEASE_SHA256)
        self.assertEqual(install['cds']['status'], 'ok', output)
        self.assertGreaterEqual(install['cds']['share'], setup.CDS_MIN_SHARE)
        self.assertTrue((home / install['cds']['archive']).is_file())
        if self.install.get('help'):
            self.assertEqual(sorted(p.name for p in (home / install['help']['dir']).iterdir()),
                             sorted(setup.HELP_FILES))
        started = time.monotonic()
        code, output = self.run_tool('setup_bsl_server.py', '--home', home, '--jar', jar, '--java', java)
        second = time.monotonic() - started
        self.assertEqual(code, 0, output)
        for line in ('JAR: без изменений', 'Распаковка: без изменений', 'CDS: без изменений',
                     'install.json: без изменений'):
            self.assertIn(line, output)
        self.assertLess(second, first / 3)
        code, output = self.run_tool('setup_bsl_server.py', '--check', '--home', home)
        self.assertEqual(code, 0, output)
        # Проверка на свежей установке работает.
        code, output = self.check(FIXTURES / 'errors' / 'Module.bsl', env={'VANTEAM_BSL_HOME': str(home)})
        self.assertEqual(code, 2, output)
        self.assertIn('распакованный JAR + CDS', output)


if __name__ == '__main__':
    unittest.main()
