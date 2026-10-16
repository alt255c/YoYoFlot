# T11, 16.10.2026

- Новые normalize/airports.py, normalize/geography.py и normalize/__init__.py.
- Новые configs/airports_overrides.yaml (пустой подтверждённый набор),
  data/reference/airports.csv, исходный airports.dat, manifest и ODbL-лицензия.
- CLI airports и scripts/run_t11.py (PyCharm, summary, -q).
- Схемы readers и код разбора исходников не изменены. Все T11-колонки/файлы
  отдельные; канонические таблицы T12 сохраняются.
- Новые semantic regression tests, geography.md, дополнения к контракту и словарю,
  карточке T11 и общему статусу. Не заявлена отсутствующая реальная приёмка.
- Пакет 0.4.0. Восстановлен отсутствующий в присланном архиве pyproject.toml по
  метаданным wheel 0.3.5; добавлен закреплённый tzdata 2026.5 для исторических
  timezone rules и Windows. Старые пины requirements.txt сохранены.

Правила: exact_reference / explicit_override / unique_flight_route_date_days /
historical_iana_utc. Каждая выведенная строка хранит версию правил/справочника,
входные значения и статус; JSON-отчёт сохраняет SHA-256 входов, исправлений,
количества и отдельные доли по каждому source_id.

Коммит задачи: src/yoyoflot/normalize, scripts/run_t11.py, scripts/build_airport_reference.py, tests/test_airports.py,
configs/airports_overrides.yaml, data/reference, docs/geography.md,
docs/validation/t11_*, docs/ledger/t11_changes.md, дополнения docs/data_contract.md,
docs/data_dictionary.md, docs/implementation_status.md, tasks/T11.md, tasks.md,
README.md, src/yoyoflot/cli.py, src/yoyoflot/__init__.py, pyproject.toml,
requirements.txt. Если data/ игнорируется, добавлять только data/reference через
`git add -f data/reference`. Реальные interim/processed и checkpoint не коммитить.
