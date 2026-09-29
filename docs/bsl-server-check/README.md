# VANTEAM BSL Server check 1.0.0

Самостоятельная проверка модулей 1С (`.bsl`, `.os`) статическим анализатором **BSL Language Server** —
форк [ivanbokhan84/bsl-language-server](https://github.com/ivanbokhan84/bsl-language-server), релиз
[v1.0.7-vanteam.1](https://github.com/ivanbokhan84/bsl-language-server/releases/tag/v1.0.7-vanteam.1).

Только BSL LS. OneScript здесь не вызывается ни прямо, ни косвенно: синтаксис проверяет отдельный
`tools/check_oscript.py` (продукт OneScript). Комбинированный инструмент «оба в одном» — отдельный продукт
[vanteam-bsl-check](https://github.com/ivanbokhan84/vanteam-bsl-check).

## Состав

| Файл | Назначение |
|---|---|
| `check_bsl.py` | проверка модулей; копия кладётся в `tools/` проекта 1С |
| `bsl_new_findings.py` | новые находки против Git-ревизии |
| `setup_bsl_server.py` | установка и проверка движка на машине |
| `config/.bsl-language-server.json` | конфиг BSL LS по умолчанию (как в 1.1.0) |
| `tests/` | 81 модульный и 19 интеграционных тестов (реальные Java 21 и JAR форка) |

## Установка на машину (один раз)

```bat
python setup_bsl_server.py                 rem скачать JAR релиза (SHA-256), распаковать, обучить AppCDS,
                                           rem каталог справки только ru, кэш справки и tmp
python setup_bsl_server.py --check         rem проверка без изменений: файлы, SHA-256, один анализ фикстуры
```

Каталог установки — `VANTEAM_BSL_HOME`, по умолчанию `%LOCALAPPDATA%\vanteam-bsl-server`.
На этой машине: `D:\vanteam\bsl-server` (переменная пользователя задана).
Внутри: JAR и распакованный layout (`engine\`), архив CDS (`cds\`), справка только ru (`help\`), кэш разобранной
справки (`cache\`), `install.json` (версии, SHA, отпечаток JDK). При смене JAR или JDK установщик переобучает CDS;
`check_bsl.py` подключает архив только при совпадении отпечатка.

## Запуск

```bat
python tools/check_bsl.py path/to/Module.bsl --deep            rem как вызывает скилл checkbsl
python tools/check_bsl.py A/Module.bsl A/Other.bsl B/Module.bsl rem одна JVM на область, несколько --target
python tools/check_bsl.py path/to/Module.bsl --source-dir src/cf rem контекст всей выгрузки, отчёт по модулю
python tools/check_bsl.py path/to/Module.bsl --json             rem итог одним JSON
```

`--deep` принимается для совместимости и ничего не меняет. `--all` принимается, печатает строку о
`check_oscript.py` и выполняет только BSL LS.

## Коды возврата

| Код | Значение |
|---|---|
| 0 | нет ошибок и предупреждений |
| 1 | есть Warning, ошибок нет |
| 2 | есть Error |
| 3 | проверка не выполнена полностью: нет движка или Java 21+, таймаут, код JVM не 0 (в том числе нехватка памяти), строка WARN/ERROR в логе движка, нет отчёта или модуля в отчёте, неизвестная severity, не загружены метаданные выгрузки |

При нескольких модулях итог — наибольший код. Код 3 — не успех и не список находок.

## Как запускается движок

- Java 21+: `VANTEAM_BSL_JAVA`, JDK установки, `JAVA_HOME`, PATH.
- `-XX:TieredStopAtLevel=1 -XX:ActiveProcessorCount=min(4, CPU) -XX:+ExitOnOutOfMemoryError`, `--silent`,
  AppCDS распакованного JAR, кэш справки `-Dapp.platform-context.cache.path` из установки.
- Куча: 512m; 1g — если в области больше 150 модулей или 20 МБ исходников либо сами проверяемые модули больше 4 МБ.
  Основание — замеры фазы 2: 596 модулей УТ 10.3 при 512m падают с нехваткой памяти, при 1g проходят;
  67 модулей проходят при 512m. Переопределение — `VANTEAM_BSL_XMX`.
- Справка только ru подставляется в сгенерированную копию конфига через `v8platform.binPath`, если проект сам
  не задал `binPath` и не выключил `v8platform`. `targetVersion` проекта не меняется.
- Одновременно в проекте идёт один анализ; следующий ждёт.

Прочие переменные: `BSL_LS_JAR` (другой fat JAR, без CDS; заданный и отсутствующий — ошибка),
`VANTEAM_BSL_TIMEOUT` (предел одной JVM, секунды).

## Проверено

- Тесты: 81 модульный и 19 интеграционных — все прошли, пропусков нет (29.09.2026).
- Сверка со старой проверкой 1.1.0 (штатный BSL LS 1.0.7) на трёх реальных модулях: находки совпали построчно
  (596, 115, 84), время ×0,29–0,31, CPU ×0,25–0,30. Таблица — в `CHANGELOG.md`.
- Движок: замеры и приёмка форка — `bench\bsl-server\REPORT-phase2.md` проекта BSL Server.
