# Обновление 0.5.1: проверка реального T11 и исправление стран

В forum_json точные аэропорты ошибочно отклонялись из-за вариантов страны:
например, SVO с Russian Federation при Russia в справочнике. Добавлены десять
правил country_aliases с основанием; они проверяют контекст явного кода и
сохраняются в *_override_json. Повреждённые места остаются нерешёнными.
На всех 13 835 присланных уникальных значениях с их частотами покрытие мест JSON
выросло с 54,66% до 95,38%. Полный пересчёт реальных связей/UTC ещё не выполнен.
Подробности и хеши: [проверка инвентаря](docs/validation/t11_real_inventory_review.md).

Архив содержит весь код T12 0.5.0 и новое исправление T11. Распаковать в корень
проекта с заменой файлов кода; реальные interim/processed в архив не включены.
После этого выполнить:

```powershell
python scripts/run_t12.py --require-all
python scripts/run_t11.py --input data/processed --schedules data/interim/real/timetable_pdf/schedules.parquet
```

Если полный T12 уже успешно выполнен, достаточно второй команды. Готовые
выгрузки readers используются повторно. Для приёмки нужны новые
canonical_report.json/geography_report.json; присланный старый T11 отмечает
incomplete PDF и только reference_only связи. Его нельзя считать окончательно
принятым. Вывод по расписанию при неподтверждённом покрытии запрещён.

# Обновление 0.5.0: T12

Сборка канонических таблиц из готовых результатов readers:

```powershell
python scripts/run_t12.py --require-all
```

Поддерживаются PyCharm Run и `-q` (итог только в файле). Выход:
`data/processed`; отчёт — `canonical_report.json`, итог — `canonical_summary.txt`.
Результаты T11 и checkpoint readers сохраняются. Текущий долгий процесс T11
можно оставить работать; обновление и новый запуск выполняй после его завершения.
Все параметры, правила и ограничения — [docs/transformations.md](docs/transformations.md).
В архив включены последние ускорения T11 и исправление повторного запуска.

# Обновление 0.4.0: T11

Добавлены справочник аэропортов, осторожные связи с расписанием, исторический
UTC и признаки качества. Запуск из корня проекта:

```powershell
python -m pip install tzdata==2026.5
python scripts/run_t11.py
```

Скрипт использует готовые выгрузки в `data/interim/real` (иначе `data/interim`),
сохраняет результат в `data/processed`. Кнопка Run в PyCharm поддерживается.
`-q` полностью отключает успешный вывод в консоль; итог остаётся в файле.
Все параметры, ограничения и подключение T12 — [docs/geography.md](docs/geography.md).
Старые readers и их checkpoint не изменены. Реальные данные для окончательной
приёмки T11 в присланном архиве отсутствуют.

В архиве не было pyproject.toml и uv.lock: pyproject.toml восстановлен по
метаданным существующего wheel с добавлением tzdata. Существующему окружению
достаточно установки tzdata; для новой установки можно использовать pip ниже.

# YoYoFlot: readers T03–T10

Пакет на Python **3.10+**, рекомендуемая и проверенная версия — **3.12**.
Реализованы общий контракт, CLI и семь readers. Чтение источников не объединяет
пассажиров и не вычисляет оценки поведения: это последующие задачи.

**Статус:** T04–T10 проверены полными прогонами шести реальных источников;
Исторический прогон: 79 тестов и 912 сравнений полей с оригиналами проходят. PDF-парсер T03 проверен
локальными фикстурами; работу на выданном PDF подтвердил пользователь, сам файл
недоступен для независимой проверки. Подробности: `docs/implementation_status.md`.

## Установка и проверки

Из корня проекта:

```bash
uv sync --extra test
uv run python -m yoyoflot --help
uv run pytest -q
uv run python scripts/verify_demo.py
```

Альтернатива без uv, работает и в Windows:

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# Linux/macOS: source .venv/bin/activate
python -m pip install -r requirements.txt
python -m pip install --no-deps -e .
python -m yoyoflot --help
python -m pytest -q
```

Базовые зависимости `requirements.txt` экспортированы из прежнего `uv.lock` и содержит версии с условиями для
Python 3.10/3.11/3.12+. После изменения зависимостей:

```bash
uv lock
uv export --extra test --format requirements-txt --no-hashes --no-emit-project --output-file requirements.txt
```

## Искусственный сквозной пример

Фикстуры находятся в `tests/fixtures/`; их ожидаемые количества — в
`expected.json`. `scripts/create_demo.py` воспроизводит новые фикстуры.
Оригинальный `timetable.pdf` сохранён из входного репозитория.

```bash
uv run python -m yoyoflot ingest all --config configs/sources_fixture.yaml --data-dir tests/fixtures --output data/interim/demo --batch-size 2 --chunk-pages 2
uv run python scripts/verify_demo.py
uv run python scripts/validate_interim.py data/interim/demo/forum_json
```

`docs/validation/fixture_runs.json` — результаты отдельных процессов; это
**не** измерения полного исходного датасета.

## Настоящие данные

Поместить выданные файлы в `data/raw/` и сверить пути в `configs/sources.yaml`.
Если внешний архив содержит дополнительный каталог, задать его в `--data-dir`.
Вложенный `YourBoardingPassDotAero.zip` распаковывать не требуется.

```bash
uv run python -m yoyoflot inventory --data-dir data/raw
uv run python -m yoyoflot ingest boarding_csv --data-dir data/raw
uv run python -m yoyoflot ingest forum_json --data-dir data/raw --batch-size 1000
uv run python -m yoyoflot ingest pointz_xml --data-dir data/raw
uv run python -m yoyoflot ingest skyteam_yaml --data-dir data/raw
uv run python -m yoyoflot ingest boardingpass_xlsx --data-dir data/raw
uv run python -m yoyoflot ingest timetable_pdf --data-dir data/raw --chunk-pages 100
```

Конфигурация Sirena подтверждена на выданном файле: UTF-8, позиции в символах,
336 символов на строку, все 20 колонок. Она находится в
`configs/sirena_columns.yaml`; reader проверяет точный заголовок и длины строк.

```bash
uv run python -m yoyoflot ingest sirena_fixed --data-dir data/raw --columns configs/sirena_columns.yaml
uv run python -m yoyoflot ingest all --data-dir data/raw
```

При отдельном пути конфигурация источников не нужна:

```bash
uv run python -m yoyoflot ingest boarding_csv --path /path/to/BoardingData.csv --output data/interim
```

## Выход и ошибки

Каждый источник создаёт Parquet по шести схемам, включая корректные пустые
таблицы, `errors.parquet` и `source_report.json`. XLSX дополнительно создаёт
`workbook_coverage.csv`; PDF — CSV, JSONL, `page_coverage.csv` и исходный текст
страниц `page_text.zip`.

Код выхода 0 означает законченный проход без ошибок уровня `error`; 1 —
ошибку/неполное покрытие/`complete_with_errors`; 2 — пока не реализованную
команду. Предупреждения и пустые записи тоже видны в журнале. `features`,
`train`, `evaluate`, `run` относятся к последующим задачам и явно возвращают 2.
Состояние `complete` описывает технический проход, а не ручную приёмку источника.

Обычные readers записывают результат в временный каталог и публикуют после
успешного завершения. При исключении прежний результат сохраняется, а рядом
остаётся `*.partial/source_report.json` со статусом `failed`.
PDF продолжает работу из соседнего `timetable_pdf.checkpoint/`.
После изменения файла, размера порции, лимита страниц или режима OCR checkpoint
сбрасывается. Для принудительного полного повторного прохода удалить этот
каталог. `--limit-pages` всегда даёт `incomplete`, если охвачена лишь часть PDF.
Страницы с изображениями без текста требуют `--ocr` и отдельно установленный
Tesseract с языковыми данными; его наличие не предполагалось и здесь не проверено.

```bash
uv run python -m yoyoflot audit --rows 50 --output data/interim/timetable_pdf
uv run python -m yoyoflot export data/interim/boarding_csv/identifiers.parquet --output data/export/identifiers
uv run python scripts/validate_interim.py data/interim/boarding_csv
```

Экспорт CSV использует `\N` для null; правила восстановления типов описаны в
`docs/data_contract.md`. Отбор строк в `audit_sheet.csv` не заменяет ручную сверку.

## Основные файлы

- `src/yoyoflot/` — пакет, контракт, происхождение, CLI и readers.
- `configs/` — реальные имена файлов из задания и отдельная конфигурация демо.
- `docs/data_contract.md`, `docs/data_dictionary.md` — общий формат.
- `docs/sources/` — поддерживаемые структуры и ограничения каждого источника.
- `docs/ledger/` — журнал изменений; `docs/validation/` — проверенные результаты.
- `tasks.md`, `tasks/` — исходные задания с честным состоянием реализации.

Старые импорты `readers.boarding_csv` и `readers.timetable_pdf` сохранены для
совместимости; новым кодом используется `yoyoflot.readers`.

## Повторение проверки настоящих источников

Распаковать FrequentFlyerForum-Profiles.zip в `data/raw/`, чтобы там находился
FrequentFlyerForum-Profiles.json. Вложенный ZIP с XLSX оставить архивом. Готовые
реальные таблицы поставляются отдельным архивом результатов; распаковать его
в корень проекта. Исходные данные в архив кода не включены.

```bash
uv run python -m yoyoflot ingest boarding_csv --data-dir data/raw --output data/interim/real --batch-size 3000
# Повторить для forum_json, pointz_xml, sirena_fixed, skyteam_yaml, boardingpass_xlsx.
uv run python scripts/inspect_real_sources.py --data-dir data/raw
uv run python scripts/extend_real_census.py --data-dir data/raw
uv run python scripts/audit_real_sources.py --data-dir data/raw --interim data/interim/real
uv run python scripts/validate_interim.py data/interim/real/boardingpass_xlsx
```

Census/audit-скрипты предназначены именно для выданных файлов; общие readers
поддерживают дополнительные варианты, проверенные регрессионными тестами.
`ingest all` включает PDF и требует его наличия. Для шести имеющихся источников
можно запускать отдельные команды, перечисленные выше.

Исправление Windows/Python 3.10 (0.3.1): `docs/validation/windows_fix.md`.

0.3.2: исправлена очистка XML, check_all_data.py использует изолированные workers.
Описание: docs/validation/windows_xml_fix.md.

Для привычного запуска PDF в PyCharm откройте readers/timetable_pdf.py и нажмите Run.
Настройте PDF_PATH в нём при необходимости. Результат: data/interim/timetable_pdf.
Общий check_all_data.py использует data/interim/real/timetable_pdf.
Исправление 0.3.3: docs/validation/pdf_run_fix.md.

0.3.4: извлечение PDF совместимо с первым рабочим кодом; нечитаемые страницы
учитываются без остановки прохода. Старый Parquet автоматически сохраняется
в *.baseline_v1 и сравнивается с новым результатом. Подробности:
docs/validation/pdf_compatibility_fix.md.


## 0.3.5: скорость PDF и тихий запуск

Убраны компиляция regex и повторный поиск исходного текста для каждой строки,
повторные расчёты схем/дат; checkpoint сохраняется компактным JSON.
Тексты страниц упакованы в page_text.zip вместо тысяч отдельных файлов.
Старые checkpoint с отдельными TXT совместимы. Готовые результаты с совпадающим
входом, параметрами, временем файлов и числом строк используются повторно;
изменённые выгрузки пересобираются из checkpoint. --force пересобирает экспорт.
Это ускорение не является заменой полной проверки check_all_data.py.

Run readers/timetable_pdf.py по умолчанию выводит только итог, тот же текст
сохраняется в data/interim/timetable_pdf/summary.txt. VERBOSE=True или --verbose
включает прогресс. Для check_all_data.py итог находится в
 data/verification/verification_summary.txt, подробности — verification.log;
VERBOSE=True включает журнал также в консоли.

Страницы с coverage.status != ok больше не прерывают проверку экспорта:
CSV/JSONL/Parquet сравниваются по каждой строке. При этом источник получает
review_required, итог DATA_CHECKS_PASSED_REVIEW_REQUIRED только если все проверки
целостности остальных данных также прошли. automatic_checks_passed остаётся
false. Потерянные строки относительно старого Parquet, несовпадающие выгрузки,
ключи/provenance, неполный проход и неверные балансы остаются ошибками.
704 страницы из пользовательского отчёта не признаны успешно разобранными:
точные причины записываются в verification_pdf_pages.json.

Замер на синтетическом PDF: 101 страница, 8000 строк; с профилировщиком
5.856 → 1.747 секунды (3.35 раза); использование готового результата 0.006 секунды.
Все шесть таблиц одинаковы, кроме добавленной ссылки на архив исходного текста.
Доказательство: docs/validation/pdf_performance.json. Полный реальный PDF и
Windows не доступны; время 2–3 минуты на них не подтверждено.
