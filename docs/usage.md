# Запуск на исходном датасете

## Поставленные результаты T13

```bash
cd /workspace/YoYoFlot
git lfs install --local
git lfs pull
uv sync --frozen --extra test
uv run --frozen python scripts/run_t17.py
uv run --frozen python -m yoyoflot serve --product artifacts/source-product
```

Команда T17 строит весь продукт T17–T20 на `data/processed`: правила, граф,
IF и проверку устойчивости. `run_t19.py` — альтернативная точка входа в ту же
атомарную сборку; генератора и отдельной учебной выборки нет. Старые флаги
`--demo`, `--profiles`, `--seed`, `--demo-dir` больше не поддерживаются.

```bash
uv run --frozen python -m yoyoflot train --input data/processed --output artifacts/my-product
uv run --frozen python scripts/run_t18.py --product artifacts/my-product
uv run --frozen python scripts/run_t20.py --product artifacts/my-product
uv run --frozen python -m yoyoflot serve --product artifacts/my-product --port 8501
```

Все пути CLI относительно текущей папки. Обёртки scripts/run_t*.py задают
абсолютные проектные значения по умолчанию и работают из другой папки.
После изменения аналитического кода или конфигурации выбирайте новый output:
снимок старой версии не переписывается. Память DuckDB ограничивается
`--memory-limit 1GB` (это не предел суммарного RSS Python/NumPy); два потока,
дисковый spill, IF максимум 100000 обучающих профилей, пакет scoring 65536.

Если вместо Parquet остался текст `version https://git-lfs.github.com/spec/v1`,
выполните `git lfs pull`. В этой ветке исправлены отсутствовавшие LFS-атрибуты
для persons/segments; содержимое исходных объектов не изменено.

## Полный конвейер с raw

Семь исходников из configs/sources.yaml должны находиться в data/raw.
Скрипт download_data.sh загружает папку преподавателя в data; разместите
оригиналы в data/raw без редактирования содержимого.

```bash
uv run --frozen python -m yoyoflot run --require-all --until build --output artifacts/core
uv run --frozen python -m yoyoflot train --input artifacts/core --output artifacts/full-product
# Эквивалентная оркестрация ядра и продукта:
uv run --frozen python -m yoyoflot run --require-all --until product --output artifacts/full-product
```

T16 сохраняет чтение семи источников → T12 → T11 → T13 → T14, контрольные
суммы, кэш, структурную проверку и неизменяемый снимок. `--from-interim` и
`--from-processed` допускают готовые проверенные этапы; исходные raw тогда
повторно не подтверждаются, что отражается в манифесте. `--allow-source-errors`
разрешает только явно отражённые ограничения парсинга, не обход структуры.
Справочник airports.csv проверяется по reference_manifest.json; координаты
не скачиваются и не геокодируются во время работы приложения.

В текущей облачной среде Google Drive отвечает proxy 403; полный raw-прогон
пока невозможен. Доступные LFS-таблицы позволяют честно строить продукт на
исходном наборе, без имитации отсутствующих сырых файлов/источниковых locators.

## Проверка

```bash
uv run --frozen python -m pytest -q
uv run --frozen python -m yoyoflot evaluate --product artifacts/source-product
```

Сайт не пересчитывает модель при выборе человека. Закройте сервер Ctrl+C;
для новой машины повторно запускается только serve при наличии готового снимка.
Изолированная облачная задача использует существующий checkout; worktree
не создаётся без явного запроса пользователя.

Независимый числовой пересчёт (без генерации данных и без чтения pickle):

```bash
uv run --frozen python scripts/verify_source_product.py --product artifacts/source-product --report data/verification/source-audit.json
```

Он пересчитывает значения факторов по поездкам всех людей топа, проверяет
вклады/порядок, принадлежность evidence, баланс маршрутов, согласованность
баллов всей совокупности и границы процентилей модели.

Прикреплённый в чате Airlines-All.zip не удалось перенести: инструмент
вложений ограничен 32 МиБ. Его состав и SHA-256 пока не проверены. Для полного
raw-прогона нужна доступная прямая ссылка либо размещение архива в среде.
