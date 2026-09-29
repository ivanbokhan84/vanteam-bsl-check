"""Фейковая java для тестов check_bsl: пишет argv в журнал и ведёт себя по FAKE_JAVA_MODE.

  ok                - отчёт json по целям --target, диагностики из FAKE_JAVA_DIAGNOSTICS (JSON-список);
  oom               - как JVM с -XX:+ExitOnOutOfMemoryError: строка Terminating ... и код 3, отчёта нет;
  platform_disabled - строка ERROR форка «platform context is disabled» при коде 0 и полном отчёте.
Запускается через java.cmd рядом (см. test_check_bsl.FakeJavaTests).
"""
import json
import os
import sys
from pathlib import Path

args = sys.argv[1:]
log = os.environ.get('FAKE_JAVA_LOG')
if log:
    with open(log, 'a', encoding='utf-8') as stream:
        stream.write(json.dumps(args, ensure_ascii=False) + '\n')
if '-version' in args:
    print('openjdk version "21.0.12" 2026-07-21 LTS', file=sys.stderr)
    sys.exit(0)
mode = os.environ.get('FAKE_JAVA_MODE', 'ok')
if mode == 'oom':
    print('Terminating due to java.lang.OutOfMemoryError: Java heap space', file=sys.stderr)
    sys.exit(3)
output_dir = Path(args[args.index('--outputDir') + 1])
targets = [args[i + 1] for i, arg in enumerate(args) if arg == '--target']
diagnostics = json.loads(os.environ.get('FAKE_JAVA_DIAGNOSTICS', '[]'))
if mode == 'platform_disabled':
    print('2026-09-29T10:00:00.000+03:00 ERROR 4242 --- [BSL Language Server] [-types-warmup-1] '
          'c.g._.b.l.t.r.BslContextHolder          : Failed to load platform contexts from 1C syntax helper, '
          'platform context is disabled for this workspace: java.lang.OutOfMemoryError: Java heap space')
report = {'fileinfos': [{'path': Path(t).resolve().as_uri(), 'mdoRef': '', 'diagnostics': diagnostics}
                        for t in targets]}
(output_dir / 'bsl-json.json').write_text(json.dumps(report, ensure_ascii=False), encoding='utf-8')
sys.exit(0)
