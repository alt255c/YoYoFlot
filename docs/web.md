# YoYoFlot Sentinel · инструкция по запуску

YoYoFlot Sentinel — локальная веб-морда для просмотра результатов готового
product snapshot. Она показывает топ-50/100/1000, список всей популяции,
карточку профиля, факторы, evidence и карту маршрутов.

Веб-сервис **не запускает тяжёлый raw-прогон из браузера**. Ему нужен уже
собранный и проверенный каталог продукта с `run_manifest.json`.

## 1. Подготовка на Windows

Откройте PowerShell в корне репозитория:

```powershell
cd C:\Users\dv-te\Desktop\DS\yoyoflot
```

Создайте окружение и установите пакет:

```powershell
py -3.12 -m venv .venv
& .\.venv\Scripts\python.exe -m pip install --upgrade pip
& .\.venv\Scripts\python.exe -m pip install -e .
```

Если окружение уже создано, повторять эти команды не нужно.

## 2. Запуск на готовом snapshot

В `PRODUCT_PATH` должен находиться каталог, внутри которого есть:

```text
run_manifest.json
tables\rankings.parquet
tables\persons.parquet
tables\segments.parquet
```

Запуск:

```powershell
$product = 'C:\path\to\source-product'
& .\.venv\Scripts\python.exe -m yoyoflot.product.web --product $product
```

После сообщения с портом откройте:

```text
http://127.0.0.1:8501/
```

Остановить сервер можно сочетанием `Ctrl+C`.

Если порт занят:

```powershell
& .\.venv\Scripts\python.exe -m yoyoflot.product.web `
  --product $product --host 127.0.0.1 --port 8502
```

Тогда адрес будет `http://127.0.0.1:8502/`.

## 3. Запуск на Linux/macOS

```bash
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
python -m yoyoflot.product.web --product /absolute/path/to/source-product
```

Для доступа к серверу из локальной сети можно указать:

```bash
python -m yoyoflot.product.web \
  --product /absolute/path/to/source-product \
  --host 0.0.0.0 --port 8501
```

## 4. Demo-режим

Кнопка **Открыть demo-шпионов** выбирает первые пять профилей из реального
рейтинга активного snapshot. Новые люди, перелёты и метки не создаются. Это
демонстрационное подмножество реального датасета, а не утверждение, что выбранные
люди доказанно являются шпионами.

Можно запустить сразу в demo-режиме:

```powershell
& .\.venv\Scripts\python.exe -m yoyoflot.product.web `
  --product $product --demo
```

Кнопка **Вернуть основной набор** возвращает полный snapshot.

## 5. Загрузка другого продукта

1. Соберите или получите ZIP уже готового product snapshot.
2. Запустите веб-сервис на любом исходном snapshot.
3. В интерфейсе нажмите **Загрузить snapshot**.
4. Выберите ZIP.

Сервер проверит `run_manifest.json`, контрольные суммы, обязательные таблицы,
пути внутри архива и лимиты размера. Raw-архив преподавателя и произвольные
файлы для выполнения через эту кнопку не принимаются.

## 6. Docker

Нужен Docker Desktop с включённым Compose. Путь к product snapshot задаётся
через переменную `YOYO_PRODUCT_PATH`:

```powershell
cd C:\Users\dv-te\Desktop\DS\yoyoflot
$env:YOYO_PRODUCT_PATH = 'C:\path\to\source-product'
docker compose up --build
```

Откройте `http://localhost:8501/`. Остановить контейнер:

```powershell
docker compose down
```

В Linux/macOS:

```bash
YOYO_PRODUCT_PATH=/absolute/path/to/source-product docker compose up --build
```

Snapshot монтируется в контейнер только для чтения. Docker упаковывает веб-сервис
и окружение; он не запускает полный raw-конвейер по кнопке загрузки.

## 7. API для проверки

При работающем сервере можно проверить API из PowerShell:

```powershell
Invoke-RestMethod http://127.0.0.1:8501/api/metadata
Invoke-RestMethod 'http://127.0.0.1:8501/api/top?limit=100'
Invoke-RestMethod 'http://127.0.0.1:8501/api/users?limit=20&offset=0&sort=rank'
Invoke-RestMethod 'http://127.0.0.1:8501/api/profile?id=PERSON_ID'
```

Основные маршруты:

- `GET /api/metadata` — окно наблюдений, размеры, режим и ограничения;
- `GET /api/top?limit=50|100|1000` — рейтинг;
- `GET /api/users?limit=&offset=&q=&eligibility=&sort=` — вся популяция;
- `GET /api/profile?id=...` — факторы, evidence, маршруты и IF;
- `POST /api/upload` — ZIP проверенного snapshot через multipart/form-data;
- `POST /api/demo` и `POST /api/reset` — переключение demo-подмножества.

## 8. Типовые ошибки

| Сообщение | Что проверить |
|---|---|
| `--product is required` | Передан ли параметр `--product` и существует ли каталог |
| `Not a completed supported product` | Есть ли полный `run_manifest.json` и совпадают ли контрольные суммы |
| `ZIP не является проверенным продуктом` | Архив должен содержать готовый snapshot, а не raw-файлы |
| `Address already in use` | Запустите с другим `--port`, например `8502` |
| Demo не включается | Demo строится из активного source snapshot; сначала укажите корректный `--product` |
