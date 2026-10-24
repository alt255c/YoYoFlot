# T12, 16.10.2026

Новые файлы: normalize/values.py, normalize/canonical.py,
configs/normalization.yaml, scripts/run_t12.py, scripts/benchmark_t12.py,
tests/test_normalize.py, docs/transformations.md и validation/t12_*.
Обновлены CLI build, версия пакета 0.5.0, контракт/словарь/статус, карточка T12,
tasks.md и README. Общий OS-lock принимает имя задачи и остаётся совместимым T11.

Для передачи T11 добавлено чтение flight_number_normalized; неверная типизация
места/времени не превращается в пропуск для произвольного заполнения расписанием.
Схемы и алгоритмы readers не изменены; режимы namespace и ручные исправления
находятся в коммитируемой конфигурации, а не в локальном CSV.

Canonical 1.0 отдельный от reader 1.0: observations вместо person_observations,
остальные ID и локальные ссылки сохранены; неключевые значения nullable.
Дедупликация только точных повторных чтений PK, несовместимые копии вызывают
ошибку. Реальные результаты/приёмка не заявлены. Команды воспроизведения и
известные ограничения полностью описаны в transformations.md.

Для коммита T12 нужны перечисленные новые файлы и изменения:
src/yoyoflot/cli.py, __init__.py, normalize/locking.py, normalize/airports.py,
normalize/geography.py, tests/test_airports.py, pyproject.toml,
docs/data_contract.md, docs/data_dictionary.md, docs/implementation_status.md,
tasks/T12.md, tasks.md, README.md. Требования зависимостей прежние: DuckDB и
PyArrow уже входили в проект. Выходы data/processed, временные базы/lock и wheel
не коммитить. data/reference относится к T11 и повторно в T12 не переносится.
