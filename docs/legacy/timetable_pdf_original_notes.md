# Skyteam_Timetable.pdf (T03)

## Источник
- **Файл**: `data/Skyteam_Timetable.pdf`
- **Страниц**: 27 514
- **Период**: 01 Nov 2018 – 31 Jan 2019

## Шаблоны страниц

### Cover (1 страница)
Обложка с периодом действия: `Covers period: DD Mon YYYY through DD Mon YYYY`

### Table (5 349 страниц)
Таблицы с заголовком `FROM: City, CountryIATA TO: City, CountryIATA` и строками рейсов.

Формат строки (слитный или с переносами):
01 Nov - 31 Jan1234567 08:4011:10MU20493202H30M
01 Nov - 31 Jan 1234567 08:40 11:10 MU2049 320 2H30M

### Continuation (16 214 страниц)
Строки рейсов без заголовка FROM/TO. Секция унаследована с предыдущей страницы (`section_source=carried:N`).

### Consult / Header-only (5 324 страницы)
`Consult your travel agent for details` или только заголовок без строк.

### Notes (618 страниц)
Служебные: контакты авиакомпаний (стр. 2), инструкция (стр. 3), список типов самолётов (стр. 4).

### Blank (8 страниц)
Пустые страницы без текста.

## Поля schedules.csv

| Поле | Тип | Описание |
|---|---|---|
| `schedule_id` | string | `TT-{page}-{row_index}` |
| `flight_number` | string | `SU1234`, `CI8028` |
| `origin` / `destination` | string | IATA-коды (3 буквы) |
| `departure_local` / `arrival_local` | string | `HH:MM` |
| `arrival_day_offset` | int/null | `+N` из PDF (прилёт на следующие сутки) или `null` |
| `days_of_week` | string | `1234567`, `12456` (пробелы удалены) |
| `valid_from` / `valid_to` | string | `YYYY-MM-DD` (год из периода обложки) |
| `source_page` | int | Номер страницы (1-based) |
| `parse_status` | string | `ok` или `row_without_section` |
| `section_source` | string | `page` или `carried:N` (номер страницы-источника) |
| `origin_city` / `dest_city` | string | Название города |
| `origin_country` / `dest_country` | string | Название страны |
| `validity_raw` | string | Исходный период `DD Mon - DD Mon` |
| `days_raw` | string | Исходные дни с пробелами |
| `aircraft` | string | Код типа самолёта (например, `320`, `738`) |
| `travel_time` | string | Длительность полёта `NH MM` |
| `operated_by` | string/null | Название оператора (из `Operated by: Airline Name`) |
| `codeshare_mark` | bool | `true` если рейс со звёздочкой `*` |
| `raw_line` | string | Исходная строка из PDF |

## Ограничения для T11

**КРИТИЧНО: Период расписания (01 Nov 2018 – 31 Jan 2019) НЕ покрывает наблюдения пассажиров (2017-01…2018-01).**

Расписание не может быть использовано как доказанный исторический маршрут для сопоставления с персональными наблюдениями в BoardingData.csv.

### Прочие ограничения
- `arrival_day_offset` берётся только из явного маркера `+N` в шаблоне; без маркера → `null`
- Codeshare: рейс со звёздочкой — маркетинговый, фактический оператор указан в `operated_by`
- Части таблиц без заголовка используют секцию предыдущей страницы (carry-over через чанки)

## Запуск

```powershell
# Полный проход (~2 минуты)
python readers/timetable_pdf.py

# Ручной аудит (50 строк)
python readers/timetable_pdf.py --audit 50

# Отладка конкретной страницы
python readers/timetable_pdf.py --debug-page 1001