# T13, 17.10.2026 — пакет 0.6.0

- Добавлены resolve/{__init__,common,blocking,matching,pipeline,segments,review}.py,
  configs/resolution.yaml, scripts/run_t13.py, audit_t13.py, benchmark_t13.py.
- Документы и карты сравниваются с точным namespace/value. Имена/DOB блокируют
  кандидатов, но самостоятельно не склеивают профили. Билет/PNR не ключ человека.
- Перед объединением проверяется весь компонент: DOB, пересечения документов,
  все варианты имени и предел размера. Неопределённость сохраняется в links,
  conflicts и graph_eligible. person_id — хеш якорного observation_id.
- Дубли поездки требуют одного person_id и совместимых наблюдённых даты/рейса/
  маршрута/времени/билета. Неполная точка требует общего билета и уникального
  совместимого события; date-only не связывает разные часы. Codeshare-правила
  пусты до подтверждения. segment_id — хеш якорного travel_id, все links сохранены.
- Технические проверки: PK/FK, сохранение каждой исходной ссылки, балансы,
  SHA-256 нужных T12/T11 входов до/после. Исходные таблицы и огромная schedule_links
  не перезаписываются; последняя не читается для matching. Ограниченный DuckDB,
  массивы компонентов и порционная запись, OS-lock и откат публикации.
- Добавлены отчёты, summary, heartbeat с текущим этапом, -q, шаблон независимого
  ревью 50/30/20 с дефицитами при нехватке кандидатов. CSV проверяющего не
  перезаписывается. audit_t13 валидирует неизменность доказательств и сохраняет
  разногласия/метрики выборки без повторной сборки. Ревью Кирилла не выдумано.
- Обновлены CLI/version, pyproject, README, task T13/tasks.md, контракт/словарь,
  статус и matching.md. Зависимости прежние; схемы T11/T12 и readers сохранены.
- Тесты/нагрузка — синтетические. Реальных полных Parquet и независимого ревью
  100 пар здесь нет. Статус карточки — реализация готова, приёмка ожидается.

Файлы для коммита:

```powershell
git add -- "src/yoyoflot/resolve/*.py" src/yoyoflot/cli.py src/yoyoflot/__init__.py pyproject.toml
git add -- configs/resolution.yaml scripts/run_t13.py scripts/audit_t13.py scripts/benchmark_t13.py tests/test_matching.py
git add -- docs/matching.md docs/data_contract.md docs/data_dictionary.md docs/implementation_status.md docs/ledger/t13_changes.md "docs/validation/t13_*" tasks/T13.md tasks.md README.md
git commit -m "feat: add T13 identity resolution and journey deduplication"
```

Вычисленные data/processed, редактируемая data/review, temp/lock, benchmark и wheel
не входят в список исходников коммита. Исходный справочник T11 уже был добавлен
в предыдущей задаче. Полные команды проверки и критерии принятия — matching.md.
